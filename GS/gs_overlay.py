#!/usr/bin/env python3
"""Trạm mặt đất: nhận video + toạ độ mục tiêu từ UAV, vẽ box, nén lại và phát cho máy xem.

  udp 127.0.0.1:5600  RTP H.264 (wfb-ng gs_video)
      → rtph264depay → h264parse → mppvideodec (giải mã phần cứng, NV12)
      → appsink ─(chỉ giữ khung MỚI NHẤT)→ luồng xử lý:
          chép sang buffer DMA của encoder → vẽ box + chữ (numpy, chữ dựng bằng cairo)
      → appsrc → mpph264enc (nén phần cứng)
      → RTSP rtsp://<ip>:8554/uav   (+ tuỳ chọn RTP/UDP tới 1 máy, --udp HOST:PORT)
  udp 0.0.0.0:5700    JSON "uav_track" (~30 gói/s) → giữ gói mới nhất để vẽ
                      (+ tuỳ chọn chuyển tiếp nguyên gói tới laptop, --telem-udp HOST:PORT, để laptop tự vẽ box)

- Xử lý chạy ở luồng riêng, không chặn bộ giải mã: nếu xử lý chậm hơn video thì bỏ khung cũ
  thay vì để khung dồn hàng (dồn hàng = độ trễ tăng dần tới vài giây).
- Không ai xem → không chép/vẽ/nén (gần như không tốn CPU).
- Không có video > 1 s → phát khung đen "NO VIDEO" để máy xem không bị treo hình.
- Không dùng textoverlay: nối textoverlay → mpph264enc trong Python bị kẹt ở PAUSED (đã thử).
"""
import argparse
import collections
import contextlib
import ctypes
import json
import logging
import math
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import cairo
import numpy as np
import gi

# Cho mpph264enc nhận khung 1920x1080 không căn hàng 16 (vstride 1080). Không có biến này nó
# "chuyển sang 1920x1088" cho MỌI khung bằng RGA → RGA lỗi trên máy này → chép bằng CPU,
# chỉ còn ~16 fps (hoặc ra khung trống). Có biến này: dùng thẳng buffer, 30 fps, không RGA.
os.environ.setdefault("GST_MPP_ENC_UNALIGNED_VSTRIDE", "1")

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GstVideo", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import GLib, Gst, GstRtspServer, GstVideo  # noqa: E402

log = logging.getLogger("gs_overlay")

# Màu NV12 (Y, U, V)
GREEN = (150, 44, 21)      # mục tiêu đang thấy
YELLOW = (210, 16, 146)    # mục tiêu đang dự đoán (lost_frames > 0)
CYAN = (170, 166, 16)      # các track khác
WHITE = (235, 128, 128)
NEUTRAL_UV = (128, 128)

TELEM_MAX_AGE = 0.5        # gói toạ độ cũ hơn thế này thì không vẽ box
NO_VIDEO_AFTER = 1.0       # không có khung video quá thời gian này → phát khung "NO VIDEO"
DEFAULT_SIZE = (1920, 1080)
DEFAULT_FPS = 30
COPY_THREADS = 3           # đo: chép khung từ bộ nhớ giải mã 1 luồng ~20 ms, 3 luồng ~9 ms, 4 luồng không nhanh hơn


# ---------------------------------------------------------------------------
# Map Gst.Buffer → numpy không chép
# ---------------------------------------------------------------------------
class _GstMapInfo(ctypes.Structure):
    _fields_ = [("memory", ctypes.c_void_p), ("flags", ctypes.c_int),
                ("data", ctypes.POINTER(ctypes.c_uint8)), ("size", ctypes.c_size_t),
                ("maxsize", ctypes.c_size_t), ("user_data", ctypes.c_void_p * 4),
                ("_gst_reserved", ctypes.c_void_p * 4)]


_libgst = ctypes.CDLL("libgstreamer-1.0.so.0")
_libgst.gst_buffer_map.argtypes = [ctypes.c_void_p, ctypes.POINTER(_GstMapInfo), ctypes.c_int]
_libgst.gst_buffer_map.restype = ctypes.c_int
_libgst.gst_buffer_unmap.argtypes = [ctypes.c_void_p, ctypes.POINTER(_GstMapInfo)]


@contextlib.contextmanager
def mapped(buf, flags):
    """Map Gst.Buffer thành mảng numpy KHÔNG chép.

    Không dùng buf.map() của PyGObject: mỗi lần đọc MapInfo.data nó chép cả khung
    (3 MB, đo được ~40 ms/lần trên buffer của mppvideodec). hash(buf) là con trỏ GstBuffer*
    (PyGObject 3.38; check_mapping() kiểm tra lại lúc khởi động).
    """
    info = _GstMapInfo()
    if not _libgst.gst_buffer_map(hash(buf), ctypes.byref(info), int(flags)):
        raise RuntimeError("gst_buffer_map thất bại")
    try:
        yield np.ctypeslib.as_array(info.data, shape=(info.size,))
    finally:
        _libgst.gst_buffer_unmap(hash(buf), ctypes.byref(info))


def check_mapping():
    """Đảm bảo mẹo hash(buf) = GstBuffer* còn đúng với bản PyGObject đang chạy."""
    pattern = bytes(range(256)) * 16
    buf = Gst.Buffer.new_wrapped(pattern)
    with mapped(buf, Gst.MapFlags.READ) as arr:
        if arr.size != len(pattern) or bytes(arr) != pattern:
            raise RuntimeError("mapped(): hash(Gst.Buffer) không còn là con trỏ GstBuffer*")


def parallel_copy(pool, *pairs):
    """Chép các cặp mảng 2D (dst, src) theo dải hàng, song song.

    Bộ nhớ đầu ra của mppvideodec không được cache nên CPU đọc rất chậm; chia nhiều luồng
    nhanh hơn rõ (np.copyto nhả GIL). Không vẽ thẳng lên buffer giải mã được vì đó là khung
    tham chiếu H.264 của decoder (vẽ vào sẽ "lem" sang các khung sau); RGA trên máy này lỗi
    nên không nhờ phần cứng chép được. Chép bằng NEON ld1 64 byte đã thử: chậm hơn memcpy.
    """
    jobs = []
    for dst, src in pairs:
        step = (len(dst) + COPY_THREADS - 1) // COPY_THREADS
        jobs += [pool.submit(np.copyto, dst[i:i + step], src[i:i + step])
                 for i in range(0, len(dst), step)]
    for j in jobs:
        j.result()


def make_mpp_pool(caps):
    """Lấy buffer pool DMA của mpph264enc (bộ cấp phát mppallocator).

    mpph264enc nhận buffer bộ nhớ thường bằng cách nhờ RGA chép sang bộ nhớ phần cứng;
    trên máy này RGA lỗi ("RgaBlit fail: Invalid argument") và khung nén ra trống (xanh lá).
    Ghi khung thẳng vào buffer của pool này thì encoder dùng trực tiếp, không cần RGA.
    """
    probe = Gst.parse_launch("appsrc name=src ! mpph264enc ! fakesink")
    probe.get_by_name("src").set_property("caps", caps)
    probe.set_state(Gst.State.PAUSED)
    query = Gst.Query.new_allocation(caps, True)
    ok = probe.get_by_name("src").get_static_pad("src").peer_query(query)
    probe.set_state(Gst.State.NULL)
    if not ok or query.get_n_allocation_pools() == 0:
        raise RuntimeError("mpph264enc không đề xuất buffer pool")
    pool, size, _, _ = query.parse_nth_allocation_pool(0)
    cfg = pool.get_config()
    Gst.BufferPool.config_set_params(cfg, caps, size, 4, 0)
    Gst.BufferPool.config_add_option(cfg, GstVideo.BUFFER_POOL_OPTION_VIDEO_META)
    if not pool.set_config(cfg) or not pool.set_active(True):
        raise RuntimeError("không kích hoạt được buffer pool của mpph264enc")
    return pool


def nv12_layout(buf, w, h, info=None):
    """((offset Y, offset UV), (stride Y, stride UV)) của 1 buffer NV12.

    Ưu tiên GstVideoMeta (mppvideodec dùng vstride 1088 cho khung 1080), sau đó VideoInfo từ caps.
    """
    meta = GstVideo.buffer_get_video_meta(buf)
    if meta:
        return (meta.offset[0], meta.offset[1]), (meta.stride[0], meta.stride[1])
    if info is not None:
        return (info.offset[0], info.offset[1]), (info.stride[0], info.stride[1])
    return (0, w * h), (w, w)


def plane_views(arr, buf, w, h, info=None):
    """(Y, UV) dạng numpy 2D trên vùng nhớ đã map của 1 buffer NV12."""
    offs, strides = nv12_layout(buf, w, h, info)
    y = np.ndarray((h, w), np.uint8, arr, offs[0], (strides[0], 1))
    uv = np.ndarray((h // 2, w), np.uint8, arr, offs[1], (strides[1], 1))
    return y, uv


def count_recent(times, now, window=1.0):
    """Số mốc thời gian trong `window` giây qua. list(deque) chạy trọn trong C (giữ GIL) nên an
    toàn khi luồng khác đang append; duyệt deque bằng generator thì có thể văng
    "deque mutated during iteration"."""
    return sum(1 for t in list(times) if now - t <= window)


# ---------------------------------------------------------------------------
# Toạ độ mục tiêu
# ---------------------------------------------------------------------------
def _box(v):
    """Box chuẩn hoá hợp lệ → tuple 4 float, sai định dạng → None."""
    try:
        b = tuple(float(x) for x in v)
    except (TypeError, ValueError):
        return None
    if len(b) != 4 or not all(math.isfinite(x) for x in b):
        return None
    return b


def _track(d):
    """Làm sạch 1 track/target từ JSON (UAV gửi; không tin tuyệt đối định dạng)."""
    if not isinstance(d, dict):
        return None
    box = _box(d.get("box"))
    if box is None:
        return None
    try:
        score = float(d.get("score") or 0.0)
        lost = int(d.get("lost_frames") or 0)
        err = tuple(float(x) for x in (d.get("err") or (0.0, 0.0)))[:2]
        if len(err) != 2:
            err = (0.0, 0.0)
    except (TypeError, ValueError):
        score, lost, err = 0.0, 0, (0.0, 0.0)
    return {"id": d.get("id"), "cls": str(d.get("cls", "?"))[:16], "score": score,
            "box": box, "lost_frames": lost, "err": err}


class TrackReceiver(threading.Thread):
    """Nghe JSON/UDP từ UAV, giữ gói mới nhất (đã làm sạch)."""

    def __init__(self, port, forward=None):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Không SO_REUSEADDR: chạy trùng 2 bản thì bản sau báo lỗi rõ ràng thay vì chia nhau gói.
        self.sock.bind(("0.0.0.0", port))
        self.sock.settimeout(0.5)
        self.lock = threading.Lock()
        self.latest = None
        self.rx_time = 0.0
        self.rx_times = collections.deque(maxlen=200)
        self.bad = 0
        self.forward = None              # (host, port): chuyển tiếp nguyên gói JSON (laptop tự vẽ box)
        self.fwd_errors = 0
        if forward:
            host, fport = forward.rsplit(":", 1)
            self.forward = (host, int(fport))
            self.fwd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            log.info("chuyển tiếp toạ độ tới %s:%s", host, fport)

    def run(self):
        while True:
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            if self.forward is not None:
                try:
                    self.fwd_sock.sendto(data, self.forward)   # gửi trước khi xử lý: laptop nhận sớm nhất
                except OSError:                                 # laptop rút dây / chưa bật → bỏ qua
                    self.fwd_errors += 1
            try:
                raw = json.loads(data)
                if not isinstance(raw, dict) or raw.get("type") != "uav_track":
                    continue
                tracks = raw.get("tracks")
                tracks = [t for t in map(_track, tracks) if t] if isinstance(tracks, list) else []
                pkt = {"detector": str(raw.get("detector", "?"))[:12],
                       "target": _track(raw.get("target")), "tracks": tracks}
            except Exception:              # gói hỏng không được làm chết luồng nhận
                self.bad += 1
                continue
            now = time.monotonic()
            with self.lock:
                self.latest = pkt
                self.rx_time = now
                self.rx_times.append(now)

    def get(self):
        """(gói mới nhất hoặc None nếu quá cũ, tần suất gói/giây trong 1 s qua)."""
        now = time.monotonic()
        with self.lock:
            rate = count_recent(self.rx_times, now)
            if self.latest is None or now - self.rx_time > TELEM_MAX_AGE:
                return None, rate
            return self.latest, rate


# ---------------------------------------------------------------------------
# Vẽ lên NV12
# ---------------------------------------------------------------------------
def draw_rect(y, uv, box, w, h, color, t):
    """Vẽ khung chữ nhật lên NV12. box chuẩn hoá 0..1, t = độ dày (px, chẵn)."""
    x1 = int(min(max(box[0], 0.0), 1.0) * w) & ~1
    y1 = int(min(max(box[1], 0.0), 1.0) * h) & ~1
    x2 = int(min(max(box[2], 0.0), 1.0) * w) & ~1
    y2 = int(min(max(box[3], 0.0), 1.0) * h) & ~1
    if x2 - x1 < 2 * t or y2 - y1 < 2 * t:
        return
    cy, cu, cv = color
    y[y1:y1 + t, x1:x2] = cy
    y[y2 - t:y2, x1:x2] = cy
    y[y1:y2, x1:x1 + t] = cy
    y[y1:y2, x2 - t:x2] = cy
    a, b, c, d, s = x1 // 2, y1 // 2, x2 // 2, y2 // 2, max(t // 2, 1)
    for plane, val in ((uv[..., 0], cu), (uv[..., 1], cv)):
        plane[b:b + s, a:c] = val
        plane[d - s:d, a:c] = val
        plane[b:d, a:a + s] = val
        plane[b:d, c - s:c] = val


class TextRenderer:
    """Dựng chữ thành mặt nạ alpha (numpy uint8) bằng cairo, có cache theo nội dung."""

    def __init__(self, font="DejaVu Sans", size=26):
        self.font, self.size = font, size
        self.cache = collections.OrderedDict()

    def render(self, text):
        mask = self.cache.get(text)
        if mask is not None:
            self.cache.move_to_end(text)
            return mask
        lines = text.split("\n")
        line_h = int(self.size * 1.3)
        probe = cairo.Context(cairo.ImageSurface(cairo.FORMAT_A8, 1, 1))
        probe.select_font_face(self.font, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        probe.set_font_size(self.size)
        width = max(int(probe.text_extents(s).x_advance) for s in lines) + 2
        surf = cairo.ImageSurface(cairo.FORMAT_A8, (width + 3) & ~3, line_h * len(lines))
        ctx = cairo.Context(surf)
        ctx.select_font_face(self.font, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(self.size)
        for i, s in enumerate(lines):
            ctx.move_to(1, i * line_h + self.size)
            ctx.show_text(s)
        surf.flush()
        mask = np.ndarray((surf.get_height(), surf.get_stride()), np.uint8, surf.get_data()).copy()
        self.cache[text] = mask
        if len(self.cache) > 256:
            self.cache.popitem(last=False)
        return mask


TEXT_BG_Y = 40              # nền chữ đặc (tối); chữ trắng Y=235
_TEXT_LUT = (TEXT_BG_Y + (235 - TEXT_BG_Y) * np.arange(256) / 255.0 + 0.5).astype(np.uint8)


def draw_text(y, uv, mask, x, top, bg_uv=NEUTRAL_UV, pad=6):
    """Chữ trắng trên nền đặc tối (chroma bg_uv) tại (x, top), tự kẹp trong khung.

    Nền đặc nên chỉ GHI, không đọc lại khung: y/uv nằm trong buffer DMA không cache của
    encoder, đọc ra rất chậm (nền bán trong suốt từng tốn ~2-3 ms/khung).
    """
    h, w = y.shape
    mask = mask[:max(h - 2 * pad, 0) & ~1, :max(w - 2 * pad, 0) & ~1]   # chữ to hơn khung → cắt
    mh, mw = mask.shape
    if mh == 0 or mw == 0:
        return
    x = int(min(max(x, pad), w - mw - pad)) & ~1
    top = int(min(max(top, pad), h - mh - pad)) & ~1
    y0, y1, x0, x1 = top - pad, top + mh + pad, x - pad, x + mw + pad
    y[y0:top, x0:x1] = TEXT_BG_Y
    y[top + mh:y1, x0:x1] = TEXT_BG_Y
    y[top:top + mh, x0:x] = TEXT_BG_Y
    y[top:top + mh, x + mw:x1] = TEXT_BG_Y
    y[top:top + mh, x:x + mw] = _TEXT_LUT[mask]
    uv[y0 // 2:(y1 + 1) // 2, x0 // 2:x1 // 2] = bg_uv


def draw_crosshair(y, w, h, size=30, t=2):
    cx, cy = w // 2, h // 2
    y[cy - t // 2:cy + t // 2, cx - size:cx - 8] = WHITE[0]
    y[cy - t // 2:cy + t // 2, cx + 8:cx + size] = WHITE[0]
    y[cy - size:cy - 8, cx - t // 2:cx + t // 2] = WHITE[0]
    y[cy + 8:cy + size, cx - t // 2:cx + t // 2] = WHITE[0]


# ---------------------------------------------------------------------------
# Nhánh nén + phát
# ---------------------------------------------------------------------------
ENCODE_TAIL = (
    "queue max-size-buffers=2 leaky=downstream "
    "! mpph264enc name=enc bps={bps} gop={gop} header-mode=each-idr "
    "! h264parse config-interval=-1 "
)


class EncodeBranch:
    """Một nhánh appsrc → mã hoá. Dùng chung cho RTSP và UDP."""

    def __init__(self, bin_, name):
        self.name = name
        self.src = bin_.get_by_name_recurse_up("src")
        self.caps = None
        self.dropped = 0
        self.enc_lat = collections.deque(maxlen=300)
        self.enc = bin_.get_by_name_recurse_up("enc")
        self.gop = self.enc.get_property("gop")
        self.enc.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._on_encoded)

    def force_keyframe(self):
        """Ép encoder ra khung IDR ở khung kế tiếp (client mới vào giữa chừng thấy hình ngay,
        không phải chờ IDR định kỳ với nền xám/nhoè).

        Không dùng sự kiện force-key-unit chuẩn: mpph264enc không đánh dấu khung khoá cho lớp
        GstVideoEncoder ("sync point: 0") nên chỉ lần ép ĐẦU TIÊN có tác dụng, các lần sau bị kẹt
        (đã thử). Đổi thuộc tính gop (N ↔ N+1) thì MPP cấu hình lại và mở GOP mới bằng IDR ngay
        khung kế tiếp — đo được đúng 5/5 lần.
        """
        gop = self.enc.get_property("gop")
        self.enc.set_property("gop", self.gop + 1 if gop == self.gop else self.gop)

    def _on_encoded(self, pad, info):
        """Độ trễ từ lúc đẩy vào appsrc (PTS do appsrc đóng dấu) tới lúc nén xong."""
        buf = info.get_buffer()
        rt = pad.get_parent_element().get_current_running_time()
        if buf.pts != Gst.CLOCK_TIME_NONE and rt != Gst.CLOCK_TIME_NONE and rt >= buf.pts:
            self.enc_lat.append((rt - buf.pts) / 1e9)
        return Gst.PadProbeReturn.OK

    def push(self, buf, caps):
        if caps is not self.caps:
            self.src.set_property("caps", caps)
            self.caps = caps
        # appsrc của GStreamer 1.18 không tự bỏ khung cũ: khi nhánh chưa chạy (RTSP đang bắt tay)
        # khung xếp hàng mãi → video trễ cả giây. Còn khung chờ thì bỏ khung mới.
        if self.src.get_property("current-level-bytes") > 0:
            self.dropped += 1
            return
        self.src.emit("push-buffer", buf)


# ---------------------------------------------------------------------------
class GroundStation:
    def __init__(self, args):
        self.args = args
        self.tracks = TrackReceiver(args.telem_port, args.telem_udp)
        self.branches = []
        self.block = threading.Lock()          # danh sách nhánh output
        self.draw_lock = threading.Lock()      # vẽ/pool/caps: luồng xử lý ↔ watchdog
        self.cond = threading.Condition()      # khung chờ xử lý (chỉ giữ khung mới nhất)
        self.pending = None
        self.skipped = 0                       # khung bỏ vì xử lý chưa xong khung trước
        self.last_frame_time = 0.0
        self.size = DEFAULT_SIZE
        self.fps = (DEFAULT_FPS, 1)
        self.caps = None
        self.out_pool = None
        self.frame_times = collections.deque(maxlen=120)      # khung giải mã xong
        self.out_times = collections.deque(maxlen=120)        # khung đã vẽ + đẩy đi nén
        self.copy_times = collections.deque(maxlen=300)
        self.dec_times = collections.deque(maxlen=300)        # h264parse → bộ giải mã trả khung
        self.wait_times = collections.deque(maxlen=300)       # khung chờ luồng xử lý
        self.parse_times = collections.deque(maxlen=60)       # thời điểm khung vào bộ giải mã (FIFO)
        self.draw_times = collections.deque(maxlen=300)
        self.errors = 0
        self.last_error_log = 0.0
        self.decode = None
        self.stream_size = None
        self.decoder_stale = False
        self.restart_lock = threading.Lock()
        self.restart_pending = False
        self.text = None
        self.text_h = 0
        self.hud_mask = None
        self.hud_time = 0.0
        self.copier = ThreadPoolExecutor(COPY_THREADS)

    # ---------- nhận + giải mã ----------
    def start_decode(self):
        """Tạo pipeline giải mã mới. Khởi động lại = tạo mới: đưa pipeline cũ về NULL rồi PLAYING
        thì nó kẹt ở PAUSED, không ra khung nữa (đã thử)."""
        a = self.args
        raw = ""
        if a.raw_udp:               # chuyển tiếp nguyên gói RTP từ UAV (không giải mã/vẽ/nén) để so độ trễ
            host, port = a.raw_udp.rsplit(":", 1)
            raw = (f"t. ! queue max-size-buffers=200 leaky=downstream "
                   f"! udpsink host={host} port={port} sync=false async=false ")
        desc = (
            f"udpsrc address={a.video_addr} port={a.video_port} buffer-size=4000000 reuse=false "
            "caps=\"application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96\" "
            "! tee name=t "
            f"t. ! queue max-size-buffers=200 leaky=downstream ! rtpjitterbuffer latency={a.jitter_ms} drop-on-latency=true "
            "! rtph264depay ! h264parse name=parse "
            "! mppvideodec ignore-error=true "
            "! video/x-raw,format=NV12 "
            "! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false "
            + raw
        )
        pipe = Gst.parse_launch(desc)
        pipe.get_by_name("sink").connect("new-sample", self.on_sample)
        self.stream_size = None
        self.decoder_stale = False
        parse_src = pipe.get_by_name("parse").get_static_pad("src")
        parse_src.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self.on_parse_event)
        parse_src.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, self.on_parsed)
        bus = pipe.get_bus()
        bus.add_signal_watch()
        if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            bus.remove_signal_watch()
            pipe.set_state(Gst.State.NULL)
            raise RuntimeError(f"không mở được cổng video {a.video_addr}:{a.video_port} "
                               "(đang có chương trình khác dùng?)")
        bus.connect("message::error", self.on_decode_error)
        self.decode = pipe

    def schedule_restart(self, delay_ms):
        """Lên lịch tạo lại pipeline giải mã (gọi được từ mọi luồng; nhiều lần gọi → 1 lần làm)."""
        with self.restart_lock:
            if self.restart_pending:
                return
            self.restart_pending = True
        GLib.timeout_add(delay_ms, self.restart_decode)

    def restart_decode(self):
        """Chạy trên luồng chính (GLib)."""
        with self.restart_lock:
            self.restart_pending = False
        old, self.decode = self.decode, None
        if old is not None:
            old.get_bus().remove_signal_watch()
            old.set_state(Gst.State.NULL)
        try:
            self.start_decode()
        except Exception:
            self.report_error("khởi động lại bộ giải mã")
            self.schedule_restart(1000)                         # thử lại sau 1 s
        return False

    def on_parse_event(self, pad, info):
        """mppvideodec (bản 1.14 của Radxa) KHÔNG đổi caps/bố cục khi luồng đổi độ phân giải giữa
        chừng (đã thử 1080p → 720p: khung 720p bị đọc theo stride 1080p → vỡ hình). h264parse thì
        đọc SPS và báo caps mới → khi đó khởi động lại pipeline giải mã."""
        ev = info.get_event()
        if ev.type == Gst.EventType.CAPS:
            st = ev.parse_caps().get_structure(0)
            ok_w, w = st.get_int("width")
            ok_h, h = st.get_int("height")
            if ok_w and ok_h:
                if self.stream_size is not None and self.stream_size != (w, h):
                    log.info("luồng đổi độ phân giải %dx%d → %dx%d: khởi động lại bộ giải mã",
                             *self.stream_size, w, h)
                    self.decoder_stale = True      # khung ra từ giờ tới lúc tạo lại đều sai bố cục
                    self.schedule_restart(0)
                self.stream_size = (w, h)
        return Gst.PadProbeReturn.OK

    def on_parsed(self, pad, info):
        """Ghi thời điểm từng khung nén vào bộ giải mã để đo độ trễ giải mã.

        Đo theo thứ tự (vào trước ra trước): luồng từ Pi không có B-frame; mppvideodec đóng dấu
        PTS mới cho khung ra (đều 33.3 ms) nên không khớp theo PTS được. h264parse đẩy cả buffer
        lẻ lẫn buffer list (alignment=au → 1 buffer = 1 khung)."""
        now = time.monotonic()
        n = info.get_buffer_list().length() if info.type & Gst.PadProbeType.BUFFER_LIST else 1
        self.parse_times.extend([now] * n)
        return Gst.PadProbeReturn.OK

    def on_decode_error(self, bus, msg):
        err, dbg = msg.parse_error()
        log.error("decode pipeline: %s (%s) → khởi động lại sau 1 s", err.message, dbg)
        self.schedule_restart(1000)

    def on_sample(self, sink):
        """Luồng của bộ giải mã: chỉ nhận khung rồi trả về ngay, không xử lý ở đây."""
        sample = sink.emit("pull-sample")
        if sample is None or self.decoder_stale:
            return Gst.FlowReturn.OK
        now = time.monotonic()
        self.last_frame_time = now
        self.frame_times.append(now)
        try:
            self.dec_times.append(now - self.parse_times.popleft())
        except IndexError:
            pass
        if self.has_output():
            with self.cond:
                if self.pending is not None:
                    self.skipped += 1
                self.pending = (sample, now)
                self.cond.notify()
        return Gst.FlowReturn.OK

    def worker(self):
        """Luồng xử lý: luôn lấy khung mới nhất, khung cũ hơn bị bỏ."""
        while True:
            with self.cond:
                while self.pending is None:
                    self.cond.wait()
                (sample, t_dec), self.pending = self.pending, None
            self.wait_times.append(time.monotonic() - t_dec)
            try:
                self.process(sample)
            except Exception:
                self.report_error("xử lý khung")
            sample = None          # trả buffer cho bộ giải mã ngay, không giữ trong lúc chờ khung sau

    def report_error(self, where):
        self.errors += 1
        now = time.monotonic()
        if now - self.last_error_log > 10:
            self.last_error_log = now
            log.exception("lỗi khi %s (tổng %d lỗi)", where, self.errors)

    def process(self, sample):
        buf = sample.get_buffer()
        info = GstVideo.VideoInfo()
        info.from_caps(sample.get_caps())
        w, h = info.width, info.height
        fps = (info.fps_n, info.fps_d) if info.fps_n > 0 and info.fps_d > 0 else self.fps
        with self.draw_lock:
            if (w, h) != self.size or fps != self.fps or self.out_pool is None:
                self.set_size(w, h, fps)
            ok, out = self.out_pool.acquire_buffer(None)
            if ok != Gst.FlowReturn.OK:
                return
            t0 = time.monotonic()
            with mapped(buf, Gst.MapFlags.READ) as src, \
                    mapped(out, Gst.MapFlags.READ | Gst.MapFlags.WRITE) as dst:
                y, uv = plane_views(dst, out, w, h)
                sy, suv = plane_views(src, buf, w, h, info)
                parallel_copy(self.copier, (y, sy), (uv, suv))
                t1 = time.monotonic()
                self.annotate(y, uv.reshape(h // 2, w // 2, 2), w, h, video_ok=True)
            t2 = time.monotonic()
            self.copy_times.append(t1 - t0)
            self.draw_times.append(t2 - t1)
            self.push(out)
            self.out_times.append(time.monotonic())

    def set_size(self, w, h, fps=None):
        """Gọi khi giữ draw_lock."""
        self.size = (w, h)
        if fps:
            self.fps = fps
        self.caps = Gst.Caps.from_string(
            f"video/x-raw,format=NV12,width={w},height={h},framerate={self.fps[0]}/{self.fps[1]}")
        if self.out_pool is not None:
            self.out_pool.set_active(False)
        self.out_pool = make_mpp_pool(self.caps)
        log.info("video %dx%d @ %d/%d", w, h, self.fps[0], self.fps[1])

    # ---------- vẽ ----------
    def annotate(self, y, uv, w, h, video_ok):
        """Vẽ box, nhãn, tâm ngắm, HUD. Gói toạ độ lỗi không bao giờ làm mất khung video."""
        if self.text is None or self.text_h != h:
            self.text, self.text_h = TextRenderer(size=max(14, h // 40)), h
            self.hud_mask = None
        pkt, rate = self.tracks.get()
        now = time.monotonic()
        vfps = count_recent(self.frame_times, now)
        line1 = (f"VIDEO {vfps:2d} fps" if video_ok else "NO VIDEO") + f" | TELEM {rate:2d} Hz"
        line2 = "NO TELEMETRY"
        try:
            if pkt is not None:
                line1 += f" | DET {pkt['detector']}"
                tgt = pkt["target"]
                tid = tgt["id"] if tgt else None
                for tr in pkt["tracks"]:
                    if tr["id"] != tid:
                        draw_rect(y, uv, tr["box"], w, h, CYAN, 2)
                if tgt:
                    lost = tgt["lost_frames"]
                    color = YELLOW if lost else GREEN
                    b = tgt["box"]
                    draw_rect(y, uv, b, w, h, color, 4)
                    ex, ey = tgt["err"]
                    line2 = (f"TARGET #{tid} {tgt['cls']} {tgt['score']:.2f}"
                             f"  err x={ex:+.2f} y={ey:+.2f}" + (f"  PRED {lost}" if lost else ""))
                    text = f"#{tid} {tgt['cls']} {int(round(tgt['score'] * 100))}%"
                    if lost:
                        text += " (pred)"
                    mask = self.text.render(text)
                    draw_text(y, uv, mask, b[0] * w, b[1] * h - mask.shape[0] - 14, bg_uv=color[1:])
                else:
                    line2 = "NO TARGET"
        except Exception:
            self.report_error("vẽ toạ độ")
            line2 = "TELEMETRY ERROR"
        if video_ok:
            draw_crosshair(y, w, h)
        # Dựng lại chữ HUD tối đa 10 lần/giây (dựng chữ bằng cairo ~4 ms/lần)
        if now - self.hud_time >= 0.1 or self.hud_mask is None:
            self.hud_mask = self.text.render(f"{line1}\n{line2}")
            self.hud_time = now
        draw_text(y, uv, self.hud_mask, 12, 12)

    # ---------- phát ----------
    def has_output(self):
        with self.block:
            return bool(self.branches)

    def push(self, buf):
        with self.block:
            branches = list(self.branches)
        for br in branches:
            br.push(buf, self.caps)

    def add_branch(self, br):
        with self.block:
            self.branches.append(br)
        log.info("output %s bắt đầu", br.name)

    def remove_branch(self, br):
        with self.block:
            if br in self.branches:
                self.branches.remove(br)
        log.info("output %s dừng", br.name)

    def encode_tail(self):
        return ENCODE_TAIL.format(bps=self.args.bitrate, gop=self.args.gop)

    def start_rtsp(self):
        a = self.args
        server = GstRtspServer.RTSPServer()
        server.set_service(str(a.rtsp_port))
        factory = GstRtspServer.RTSPMediaFactory()
        factory.set_launch(
            "( appsrc name=src is-live=true format=time do-timestamp=true "
            f"! {self.encode_tail()} ! rtph264pay name=pay0 pt=96 config-interval=1 )")
        factory.set_shared(True)
        factory.set_latency(0)

        def on_configure(_factory, media):
            br = EncodeBranch(media.get_element(), "rtsp")
            if self.caps is not None:
                br.src.set_property("caps", self.caps)
                br.caps = self.caps
            media.connect("unprepared", lambda _m: self.remove_branch(br))
            self.add_branch(br)

        def on_play(client, _ctx):
            with self.block:
                branches = [b for b in self.branches if b.name == "rtsp"]
            for br in branches:
                br.force_keyframe()
            conn = client.get_connection()
            log.info("client %s xem → ép IDR (%d nhánh)", conn.get_ip() if conn else "?", len(branches))

        factory.connect("media-configure", on_configure)
        server.connect("client-connected", lambda _s, client: client.connect("play-request", on_play))
        server.get_mount_points().add_factory(a.rtsp_path, factory)
        if server.attach(None) == 0:
            raise RuntimeError(f"không mở được cổng RTSP {a.rtsp_port} (đang có chương trình khác dùng?)")
        self.rtsp_server = server
        log.info("RTSP: rtsp://<ip-radxa>:%d%s", a.rtsp_port, a.rtsp_path)

    def start_udp(self):
        host, port = self.args.udp.rsplit(":", 1)
        pipe = Gst.parse_launch(
            "appsrc name=src is-live=true format=time do-timestamp=true "
            f"! {self.encode_tail()} ! rtph264pay pt=96 mtu=1400 config-interval=1 "
            f"! udpsink host={host} port={port} sync=false async=false")
        br = EncodeBranch(pipe, f"udp {host}:{port}")
        pipe.set_state(Gst.State.PLAYING)
        self.udp_pipe = pipe
        self.add_branch(br)

    # ---------- khung "NO VIDEO" + thống kê ----------
    def watchdog(self):
        if time.monotonic() - self.last_frame_time <= NO_VIDEO_AFTER:
            return True
        try:
            with self.draw_lock:
                w, h = self.size
                if self.out_pool is None:
                    self.set_size(w, h)
                if not self.has_output():
                    return True
                ok, out = self.out_pool.acquire_buffer(None)
                if ok != Gst.FlowReturn.OK:
                    return True
                with mapped(out, Gst.MapFlags.READ | Gst.MapFlags.WRITE) as dst:
                    y, uv = plane_views(dst, out, w, h)
                    y[:] = 16
                    uv[:] = 128
                    self.annotate(y, uv.reshape(h // 2, w // 2, 2), w, h, video_ok=False)
                self.push(out)
        except Exception:
            self.report_error("tạo khung NO VIDEO")
        return True

    def stats(self):
        now = time.monotonic()
        vfps = count_recent(self.frame_times, now)
        ofps = count_recent(self.out_times, now)
        _, rate = self.tracks.get()
        msg = f"video vào {vfps} fps, ra {ofps} fps, telem {rate} Hz"
        dec, wait = list(self.dec_times), list(self.wait_times)
        if dec:
            msg += f", giải mã {1000 * sum(dec) / len(dec):.0f} ms (max {1000 * max(dec):.0f})"
            self.dec_times.clear()
        if wait:
            msg += f", chờ xử lý {1000 * sum(wait) / len(wait):.1f} ms"
            self.wait_times.clear()
        if self.copy_times:
            c, d = list(self.copy_times), list(self.draw_times)
            msg += (f", chép {1000 * sum(c) / len(c):.1f} ms, vẽ {1000 * sum(d) / len(d):.1f} ms"
                    f" (max tổng {1000 * max(map(sum, zip(c, d))):.1f} ms), bỏ {self.skipped} khung")
            self.copy_times.clear()
            self.draw_times.clear()
        with self.block:
            for br in self.branches:
                lat = list(br.enc_lat)
                if lat:
                    msg += (f"; {br.name}: nén {1000 * sum(lat) / len(lat):.1f} ms"
                            f" (max {1000 * max(lat):.1f}), bỏ {br.dropped}")
                    br.enc_lat.clear()
        if self.errors or self.tracks.bad or self.tracks.fwd_errors:
            msg += f"; lỗi {self.errors}, gói hỏng {self.tracks.bad}, lỗi chuyển tiếp toạ độ {self.tracks.fwd_errors}"
        log.info(msg)
        return True

    def run(self):
        self.tracks.start()
        threading.Thread(target=self.worker, name="process", daemon=True).start()
        self.start_decode()
        if self.args.rtsp_port:
            self.start_rtsp()
        if self.args.udp:
            self.start_udp()
        GLib.timeout_add(200, self.watchdog)
        GLib.timeout_add_seconds(self.args.stats_every, self.stats)
        GLib.MainLoop().run()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--video-addr", default="127.0.0.1", help="địa chỉ nghe RTP từ wfb-ng")
    p.add_argument("--video-port", type=int, default=5600)
    p.add_argument("--telem-port", type=int, default=5700)
    p.add_argument("--telem-udp", metavar="HOST:PORT",
                   help="chuyển tiếp nguyên gói JSON toạ độ tới 1 máy (laptop tự vẽ box trên luồng thô)")
    p.add_argument("--jitter-ms", type=int, default=5,
                   help="rtpjitterbuffer; wfb-ng đã trả gói đúng thứ tự (sau FEC) nên không cần lớn")
    p.add_argument("--rtsp-port", type=int, default=8554, help="0 = tắt RTSP")
    p.add_argument("--rtsp-path", default="/uav")
    p.add_argument("--udp", metavar="HOST:PORT", help="phát thêm RTP/UDP tới 1 máy (độ trễ thấp nhất)")
    p.add_argument("--raw-udp", metavar="HOST:PORT",
                   help="chuyển tiếp thêm luồng RTP GỐC từ UAV (không box) tới 1 máy — để so độ trễ")
    p.add_argument("--bitrate", type=int, default=8_000_000)
    p.add_argument("--gop", type=int, default=30)
    p.add_argument("--stats-every", type=int, default=10)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Gst.init(None)
    check_mapping()
    try:
        GroundStation(args).run()
    except (OSError, RuntimeError) as e:
        log.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
uav_viewer.py — xem video UAV (H.264 / RTP / UDP) với độ trễ thấp, thay cho VLC.

Kiến trúc: 3 luồng, chỉ giữ khung MỚI NHẤT, không bao giờ xếp hàng dài.
  1. RtpReceiver  — socket UDP → tách RTP → ghép NAL (đơn / STAP-A / FU-A) → khung nén Annex-B
  2. H264Decoder  — PyAV CodecContext (LOW_DELAY, không frame-threading) → VideoFrame
  3. Viewer (UI)  — pygame: vẽ khung giải mã mới nhất + lớp Overlay + HUD
Không dùng demuxer RTP / file SDP của FFmpeg (có hàng đợi sắp xếp lại và bộ đệm riêng),
không đồng bộ theo PTS/đồng hồ: có khung là hiện ngay.

    python uav_viewer.py              # video thô từ UAV (UDP 5600) + box vẽ trên laptop (toạ độ UDP 5700)
Radxa chạy gs_forward.py chuyển tiếp nguyên gói; app tự đăng ký nhận với Radxa (192.168.50.1:5599) mỗi giây.
Phím: F / F11 / nháy đúp: toàn màn hình · H: HUD · B: box · C: chế độ mất gói (auto/conceal/idr) · Esc / Q: thoát
"""

import argparse
import json
import logging
import math
import os
import platform
import queue
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
# toàn màn hình không tự thu nhỏ khi bấm sang cửa sổ khác (vd QGroundControl)
os.environ.setdefault("SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS", "0")
os.environ.setdefault("SDL_VIDEO_CENTERED", "1")

try:
    import av
    import numpy as np
    import pygame
except ImportError as e:
    print(f"Thiếu thư viện ({e}). Cài bằng:  python -m pip install -r requirements.txt")
    sys.exit(1)

START_CODE = b"\x00\x00\x00\x01"
RTP_HEADER = struct.Struct("!BBHII")   # V/P/X/CC, M/PT, seq, timestamp, SSRC
NO_SIGNAL_SEC = 1.0      # không có gói lâu hơn → NO SIGNAL
SSRC_SWITCH_SEC = 0.5    # chỉ nhận SSRC mới khi luồng cũ đã im lặng chừng này (nguồn khởi động lại)
SEQ_RESYNC_GAP = 3000    # seq nhảy xa hơn → coi như nguồn khởi động lại, không tính là mất gói
LATE_RESYNC_RUN = 50     # chừng này gói "đến muộn" liên tiếp → seq đã lùi hẳn (nguồn khởi động lại)
# --- xử lý mất gói, chế độ "auto" (mặc định) ---
AUTO_CONCEAL_MAX_LOST = 20   # 1 lần mất ≤ chừng này gói → vẫn giải mã, decoder che lỗi (không đứng hình)
AUTO_CONCEAL_BUDGET = 60     # tổng gói đã che kể từ IDR gần nhất vượt mức này → bỏ tới IDR (tránh nhoè nặng)
LOST_RESET = 1_000_000       # "số gói mất" giả cho luồng khởi động lại / hàng đợi tràn → luôn chờ IDR
# --- log chẩn đoán ---
SILENCE_LOG_SEC = 0.15   # 2 gói liên tiếp cách nhau lâu hơn → ghi "IM LẶNG"
LATE_FRAME_LOG_SEC = 0.1 # khung tới muộn hơn nhịp phát (RTP timestamp) chừng này → ghi log
FREEZE_LOG_SEC = 0.25    # hình đứng lâu hơn → ghi "ĐỨNG HÌNH" kèm nguyên nhân
UI_STALL_SEC = 0.2       # vòng lặp UI bị chặn lâu hơn → ghi log
IDR_SPARSE_SEC = 1.6     # 2 IDR cách nhau lâu hơn → ghi log (luồng chuẩn: ~1 s)
LOG_EVENTS_PER_SEC = 20  # mỗi loại sự kiện tối đa chừng này dòng / giây
LOG_KEEP_FILES = 30      # giữ chừng này file log mới nhất
# --- box mục tiêu vẽ trên laptop ---
DEFAULT_TRACK_PORT = 5700  # Radxa (gs_forward.py) chuyển tiếp JSON "uav_track" tới đây
DEFAULT_GS = "192.168.50.1"  # trạm mặt đất (Radxa, cắm dây thẳng); app tự đăng ký nhận video với nó
GS_SUBSCRIBE_PORT = 5599     # gs_forward.py: máy xem gửi 1 gói UDP bất kỳ mỗi ~1 s → được gửi video + toạ độ
TRACK_MAX_AGE = 0.5        # gói toạ độ cũ hơn → không vẽ box (giống Radxa)
BOX_GREEN = (40, 230, 40)      # mục tiêu đang thấy
BOX_YELLOW = (255, 210, 0)     # mục tiêu đang dự đoán (lost_frames > 0)
BOX_CYAN = (0, 210, 230)       # các track khác
clock = time.perf_counter  # đồng hồ đơn điệu, dùng chung cho mọi luồng


# ----------------------------------------------------------------------------------------------
# Tiện ích Windows
# ----------------------------------------------------------------------------------------------
def setup_windows_process():
    """DPI-aware (hình nét, toàn màn hình đúng độ phân giải thật), timer 1 ms, ưu tiên tiến trình."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00008000)  # ABOVE_NORMAL_PRIORITY_CLASS
    except Exception:
        pass


def boost_current_thread():
    """Tăng ưu tiên luồng nhận / giải mã để không bị app khác chen ngang (Windows)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetCurrentThread.restype = ctypes.c_void_p
        k32.SetThreadPriority.argtypes = (ctypes.c_void_p, ctypes.c_int)
        k32.SetThreadPriority(k32.GetCurrentThread(), 2)  # THREAD_PRIORITY_HIGHEST
    except Exception:
        pass


def local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


# ----------------------------------------------------------------------------------------------
# Log chẩn đoán
# ----------------------------------------------------------------------------------------------
class EventLog:
    """Log chẩn đoán ra file; mỗi dòng có giờ:phút:giây.mili-giây để đối chiếu với lúc thấy mất hình.
    Ghi bằng luồng riêng: luồng nhận / giải mã / UI không bao giờ phải chờ đĩa."""

    def __init__(self, path=None):
        self.path = path
        self._q = queue.SimpleQueue()
        self._sec = 0
        self._budget = {}
        self._thread = None
        if path:
            self._f = open(path, "a", encoding="utf-8", buffering=1)
            self._thread = threading.Thread(target=self._run, name="log", daemon=True)
            self._thread.start()

    def __call__(self, src, msg, kind=None):
        """Gọi được từ mọi luồng. kind: loại sự kiện hay lặp, tối đa LOG_EVENTS_PER_SEC dòng mỗi giây."""
        if self._thread is None:
            return
        now = time.time()
        if kind is not None:
            sec = int(now)
            if sec != self._sec:
                self._sec, self._budget = sec, {}
            n = self._budget.get(kind, 0) + 1
            self._budget[kind] = n
            if n > LOG_EVENTS_PER_SEC:
                if n > LOG_EVENTS_PER_SEC + 1:
                    return
                msg = f"... (quá nhiều sự kiện '{kind}', bỏ bớt log trong giây này)"
        self._q.put((now, src, msg))

    def _run(self):
        f = self._f
        while True:
            item = self._q.get()
            if item is None:
                break
            t, src, msg = item
            stamp = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"
            try:
                f.write(f"{stamp} [{src}] {msg}\n")
            except Exception:
                pass
        f.close()

    def close(self):
        if self._thread is not None:
            self._q.put(None)
            self._thread.join(2.0)
            self._thread = None


LOG = EventLog()   # mặc định tắt; main() bật khi chạy app


def app_dir():
    return os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))


def default_log_path(port):
    """logs/uav_viewer_<ngày>_<giờ>_<cổng>.log cạnh app (giữ LOG_KEEP_FILES file mới nhất)."""
    name = time.strftime("uav_viewer_%Y%m%d_%H%M%S") + f"_{port}.log"
    for d in (os.path.join(app_dir(), "logs"), os.path.join(tempfile.gettempdir(), "uav_viewer_logs")):
        try:
            os.makedirs(d, exist_ok=True)
            old = sorted(f for f in os.listdir(d) if f.startswith("uav_viewer_") and f.endswith(".log"))
            for f in old[:max(0, len(old) - (LOG_KEEP_FILES - 1))]:
                os.remove(os.path.join(d, f))
            path = os.path.join(d, name)
            open(path, "a").close()      # thử quyền ghi
            return path
        except OSError:
            continue
    return None


# ----------------------------------------------------------------------------------------------
# Thống kê
# ----------------------------------------------------------------------------------------------
class Stats:
    """Bộ đếm dùng chung giữa các luồng (chỉ để hiển thị / log nên không cần khoá)."""
    COUNTERS = ("packets", "bytes", "lost", "late", "bad", "aus", "corrupt_aus", "corrupt_idr",
                "q_dropped", "idr_dropped", "decoded", "dec_errors", "concealed")

    def __init__(self):
        for k in self.COUNTERS:
            setattr(self, k, 0)
        self.foreign = 0          # gói của SSRC lạ chen vào cùng cổng
        self.resets = 0           # số lần luồng khởi động lại (SSRC mới / seq nhảy xa)
        self.dec_ms = []          # thời gian giải mã từng khung (ms), UI lấy ra mỗi giây
        self.max_gap = 0.0        # khoảng lặng dài nhất giữa 2 gói trong giây vừa qua (s)
        self.gap_since_display = 0.0  # khoảng lặng dài nhất kể từ lần hiện khung gần nhất (s)
        self.late_max = 0.0       # khung tới muộn nhất so với nhịp RTP trong giây vừa qua (s)
        self.idr_interval = 0.0   # 2 IDR gần nhất cách nhau: giây, số khung; cỡ IDR (byte)
        self.idr_frames = 0
        self.idr_bytes = 0

    def snapshot(self):
        return {k: getattr(self, k) for k in self.COUNTERS}


def avg_max(values):
    return (sum(values) / len(values), max(values)) if values else (0.0, 0.0)


# ----------------------------------------------------------------------------------------------
# 1. Nhận RTP + tách gói H.264
# ----------------------------------------------------------------------------------------------
class AccessUnit:
    """Một khung nén (access unit) dạng Annex-B, kèm thời điểm nhận và trạng thái mất gói."""
    __slots__ = ("data", "index", "rtp_ts", "t_first", "t_last",
                 "is_idr", "has_sps", "corrupt", "loss_before", "lost")


def _starts_access_unit(p):
    """Gói RTP này có mở đầu một khung không (AUD/SPS/PPS/SEI, hoặc slice có first_mb_in_slice = 0)?
    Dùng để biết phần bị mất nằm ở cuối khung trước hay ở đầu khung này."""
    t = p[0] & 0x1F
    if t == 24:                          # STAP-A: xét NAL đầu tiên bên trong
        p = p[3:]
        if not p:
            return False
        t = p[0] & 0x1F
    elif t == 28:                        # FU-A: phải là mảnh đầu (S = 1)
        if len(p) < 3 or not (p[1] & 0x80):
            return False
        t = p[1] & 0x1F
        p = p[1:]
    if t in (6, 7, 8, 9):
        return True
    # first_mb_in_slice mã ue(v): bằng 0 ⇔ bit đầu tiên của slice header = 1
    return t in (1, 5) and len(p) > 1 and bool(p[1] & 0x80)


class H264Depacketizer:
    """Ghép gói RTP H.264 (RFC 6184, packetization-mode 0/1) thành khung Annex-B.

    - NAL đơn (1–23): thêm start code; STAP-A (24): tách các NAL bên trong;
      FU-A (28): ghép mảnh theo bit S/E, thiếu mảnh → bỏ cả NAL đó.
    - Một khung = các NAL cùng RTP timestamp; xuất NGAY khi gặp marker = 1
      (hoặc khi timestamp đổi nếu gói mang marker bị mất).
    - Không có bộ đệm sắp xếp lại: gói đến muộn / ngược thứ tự bị bỏ (đổi lấy độ trễ thấp).
    - corrupt = khung thiếu dữ liệu; loss_before = có mất gói trước khung này (chuỗi tham chiếu
      có thể đã gãy). Bộ giải mã dựa vào hai cờ này để quyết định bỏ tới IDR.
    """

    def __init__(self, on_au, stats, name="rtp"):
        self.on_au = on_au
        self.stats = stats
        self.name = name             # nhãn trong log (số cổng)
        self.ssrc = None
        self.expected_seq = None
        self.last_pkt_time = 0.0
        self.late_run = 0            # số gói "đến muộn" liên tiếp
        self.pending_loss = True     # lúc đầu chưa có khung tham chiếu nào
        self.pending_lost = LOST_RESET  # số gói mất chưa gán cho khung nào (cộng vào khung kế tiếp)
        self.au_index = 0
        self.foreign_seen = set()
        self._new_au()
        self._reset_timing()

    def _reset_timing(self):
        self.prev_ts = None          # đo khung tới muộn so với nhịp phát (RTP timestamp 90 kHz)
        self.ext_ts = 0
        self.off_min_cur = self.off_min_prev = float("inf")
        self.off_win_t = 0.0
        self.late_ep_n = 0           # đợt khung tới muộn đang diễn ra: số khung, muộn nhất
        self.late_ep_max = 0.0
        self.ts_rate_ok = None       # RTP timestamp có chạy đúng 90 kHz không (None: đang đo)
        self.ts_rate_t0 = 0.0
        self.last_idr_t = 0.0
        self.last_idr_index = 0

    def _new_au(self):
        self.chunks = []
        self.ts = None
        self.t_first = self.t_last = 0.0
        self.corrupt = False
        self.loss_before = False
        self.is_idr = False
        self.has_sps = False
        self.au_lost = 0             # số gói mất trong / ngay trước khung này
        self.fu_start = -1           # vị trí trong chunks của NAL FU-A đang ghép dở (-1: không có)
        self.fu_type = 0

    def _reset_stream(self, ssrc, reason):
        if self.ssrc is None:
            LOG(self.name, f"BẮT ĐẦU NHẬN luồng SSRC 0x{ssrc:08X}")
        else:
            LOG(self.name, f"RESET luồng ({reason}) → chờ IDR")
            self.stats.resets += 1
        self.ssrc = ssrc
        self.expected_seq = None
        self._new_au()
        self._reset_timing()
        self.pending_loss = True
        self.pending_lost = LOST_RESET

    def _close_partial_fu(self):
        """NAL FU-A thiếu mảnh: giữ phần đầu (trước chỗ mất) thành 1 NAL cụt — khi che lỗi, decoder vẫn
        giải được phần trên của khung (luồng từ Pi chỉ có 1 slice/khung); khi bỏ tới IDR thì cả khung bị
        bỏ nên không ảnh hưởng. Các mảnh sau của NAL đó bị bỏ (fu_start < 0)."""
        if self.fu_start >= 0:
            if len(self.chunks) - self.fu_start <= 2:      # chỉ có start code + header, không có dữ liệu
                del self.chunks[self.fu_start:]
            else:
                self._mark(self.fu_type)
            self.fu_start = -1
            self.corrupt = True

    def _finish(self):
        self._close_partial_fu()
        if self.chunks:
            au = AccessUnit()
            au.data = b"".join(self.chunks)
            au.index = self.au_index
            au.rtp_ts = self.ts
            au.t_first = self.t_first
            au.t_last = self.t_last
            au.is_idr = self.is_idr
            au.has_sps = self.has_sps
            au.corrupt = self.corrupt
            au.loss_before = self.loss_before
            au.lost = self.au_lost
            st = self.stats
            st.aus += 1
            if au.corrupt:
                st.corrupt_aus += 1
                if au.is_idr:
                    st.corrupt_idr += 1
            if au.is_idr:
                self._on_idr(au)
            self._timing(au.rtp_ts, au.t_last)
            self.au_index += 1
            self.on_au(au)
        elif self.corrupt or self.loss_before:
            self.pending_loss = True     # khung rỗng vì mất gói → báo cho khung sau
            self.pending_lost += self.au_lost
        self._new_au()

    def _on_idr(self, au):
        st = self.stats
        if self.last_idr_t:
            st.idr_interval = au.t_last - self.last_idr_t
            st.idr_frames = au.index - self.last_idr_index
            if st.idr_interval > IDR_SPARSE_SEC:
                LOG(self.name, f"IDR THƯA: {st.idr_interval:.2f} s ({st.idr_frames} khung) kể từ IDR trước"
                               " (mất khung IDR, hoặc nguồn phát đặt GOP dài)", "idr")
        st.idr_bytes = len(au.data)
        self.last_idr_t = au.t_last
        self.last_idr_index = au.index

    def _timing(self, ts, t):
        """Khung tới muộn bao nhiêu so với nhịp phát (RTP timestamp): lộ ra lúc mạng / nguồn phát bị
        khựng rồi dồn gói tới một lúc, kể cả khi không mất gói nào."""
        if self.prev_ts is None:
            self.ext_ts = 0
            self.off_win_t = t
        else:
            self.ext_ts += ((ts - self.prev_ts + 0x80000000) & 0xFFFFFFFF) - 0x80000000
        self.prev_ts = ts
        if self.ts_rate_ok is None:      # 3 s đầu: kiểm tra nhịp timestamp trước khi tin vào phép đo
            if not self.ts_rate_t0:
                self.ts_rate_t0 = t
                return
            if t - self.ts_rate_t0 < 3.0:
                return
            rate = self.ext_ts / (t - self.ts_rate_t0)
            self.ts_rate_ok = 60000 < rate < 120000
            if not self.ts_rate_ok:
                LOG(self.name, f"RTP timestamp chạy {rate / 1000:.1f} kHz thay vì 90 kHz → bỏ đo 'tới muộn' cho luồng này")
        if not self.ts_rate_ok:
            return
        off = t - self.ext_ts / 90000.0
        if off < self.off_min_cur:
            self.off_min_cur = off
        if t - self.off_win_t > 10.0:    # đường nền trượt 10–20 s: bù trôi đồng hồ giữa 2 máy
            self.off_min_prev, self.off_min_cur, self.off_win_t = self.off_min_cur, off, t
        late = off - min(self.off_min_prev, self.off_min_cur)
        if late > 3.0:                   # timestamp nhảy (nguồn đổi nhịp) → đặt lại đường nền
            self.off_min_cur = self.off_min_prev = off
            return
        st = self.stats
        if late > st.late_max:
            st.late_max = late
        if late >= LATE_FRAME_LOG_SEC:
            self.late_ep_n += 1
            self.late_ep_max = max(self.late_ep_max, late)
        elif self.late_ep_n:
            LOG(self.name, f"KHUNG TỚI MUỘN: {self.late_ep_n} khung liền, muộn nhất "
                           f"{self.late_ep_max * 1000:.0f} ms so với nhịp phát", "latefr")
            self.late_ep_n, self.late_ep_max = 0, 0.0

    def push(self, pkt, now):
        st = self.stats
        n = len(pkt)
        if n < 13:
            st.bad += 1
            return
        b0, b1, seq, ts, ssrc = RTP_HEADER.unpack_from(pkt)
        if (b0 & 0xC0) != 0x80 or 200 <= b1 <= 204:   # không phải RTP v2, hoặc là RTCP lạc vào
            st.bad += 1
            return
        if ssrc != self.ssrc:
            if self.ssrc is not None and now - self.last_pkt_time < SSRC_SWITCH_SEC:
                st.foreign += 1      # luồng lạ chen vào cùng cổng → bỏ qua
                if ssrc not in self.foreign_seen and len(self.foreign_seen) < 16:
                    self.foreign_seen.add(ssrc)
                    LOG(self.name, f"GÓI LẠ: SSRC 0x{ssrc:08X} chen vào (đang nhận 0x{self.ssrc:08X})"
                                   " — có 2 nguồn cùng gửi tới cổng này?")
                return
            self._reset_stream(ssrc, f"SSRC mới 0x{ssrc:08X}")

        # --- số thứ tự: phát hiện mất gói ---
        loss = False
        lost_n = 0
        if self.expected_seq is not None:
            gap = (seq - self.expected_seq) & 0xFFFF
            if gap >= 0x8000:                # gói cũ / trùng / đến muộn → bỏ
                self.late_run += 1
                if self.late_run <= LATE_RESYNC_RUN:
                    st.late += 1
                    LOG(self.name, f"GÓI MUỘN/TRÙNG seq {seq} (đang chờ {self.expected_seq})", "late")
                    return
                self._reset_stream(ssrc, f"seq lùi về {seq}")   # nguồn khởi động lại, giữ nguyên SSRC
            elif gap > SEQ_RESYNC_GAP:
                self._reset_stream(ssrc, f"seq nhảy {gap} gói")
            elif gap:
                st.lost += gap
                loss = True
                lost_n = gap
                LOG(self.name, f"MẤT {gap} gói (seq {self.expected_seq}–{(seq - 1) & 0xFFFF})", "loss")
        self.late_run = 0
        self.expected_seq = (seq + 1) & 0xFFFF
        self.last_pkt_time = now
        st.packets += 1
        st.bytes += n

        # --- payload (bỏ CSRC, extension, padding) ---
        off = 12 + 4 * (b0 & 0x0F)
        if b0 & 0x10:
            if n < off + 4:
                off = n
            else:
                off += 4 + 4 * ((pkt[off + 2] << 8) | pkt[off + 3])
        end = n - pkt[-1] if b0 & 0x20 else n
        if off >= end:
            st.bad += 1
            payload = None
            loss = True                  # coi như mất gói này
            lost_n += 1
        else:
            payload = pkt[off:end]

        # --- ranh giới khung ---
        if self.ts is not None and ts != self.ts:
            if loss:
                self.corrupt = True      # gói cuối (marker) của khung trước có thể đã mất
                self.au_lost += lost_n
            self._finish()
        if self.ts is None:              # gói đầu tiên của khung mới
            self.ts = ts
            self.t_first = now
            self.loss_before = self.pending_loss or loss
            self.au_lost = self.pending_lost + lost_n
            self.pending_loss = False
            self.pending_lost = 0
            if loss and not (payload and _starts_access_unit(payload)):
                self.corrupt = True      # phần đầu khung này nằm trong các gói đã mất
        elif loss:
            self._close_partial_fu()
            self.corrupt = True
            self.au_lost += lost_n
        self.t_last = now
        if payload:
            self._add_payload(payload)
        if b1 & 0x80:                    # marker: gói cuối của khung → xuất ngay, không chờ gói sau
            self._finish()

    def _add_payload(self, p):
        t = p[0] & 0x1F
        if t == 28:                                      # FU-A
            if len(p) < 3:
                self._close_partial_fu()
                self.corrupt = True
                return
            fu = p[1]
            if fu & 0x80:                                # S: mảnh đầu
                self._close_partial_fu()                 # NAL trước còn dở (thiếu mảnh cuối)
                self.fu_start = len(self.chunks)
                self.fu_type = fu & 0x1F
                self.chunks += (START_CODE, bytes(((p[0] & 0xE0) | self.fu_type,)), p[2:])
            elif self.fu_start >= 0:
                self.chunks.append(p[2:])
            else:                                        # mảnh giữa/cuối mà thiếu mảnh đầu → bỏ
                self.corrupt = True
                return
            if fu & 0x40:                                # E: mảnh cuối → NAL hoàn chỉnh
                self._mark(self.fu_type)
                self.fu_start = -1
            return
        self._close_partial_fu()
        if 1 <= t <= 23:                                 # NAL đơn
            self._add_nal(p)
        elif t == 24:                                    # STAP-A: [cỡ 2 byte][NAL] lặp lại
            i, n = 1, len(p)
            while i + 2 < n:
                size = (p[i] << 8) | p[i + 1]
                i += 2
                if size == 0 or i + size > n:
                    self.corrupt = True
                    self.stats.bad += 1
                    break
                self._add_nal(p[i:i + size])
                i += size
        else:                                            # STAP-B / MTAP / FU-B: không dùng ở mode 1
            self.stats.bad += 1

    def _add_nal(self, nal):
        self.chunks.append(START_CODE)
        self.chunks.append(nal)
        self._mark(nal[0] & 0x1F)

    def _mark(self, nal_type):
        if nal_type == 5:
            self.is_idr = True
        elif nal_type == 7:
            self.has_sps = True


class RtpReceiver(threading.Thread):
    """Luồng nhận: socket UDP (SO_RCVBUF lớn) → H264Depacketizer → on_au(khung nén)."""

    def __init__(self, bind, port, rcvbuf, on_au, stats, name=None):
        super().__init__(name="rtp-rx", daemon=True)
        self.stats = stats
        self.name = name or str(port)   # nhãn trong log
        self.source = None               # IP máy đang gửi (cắm dây thẳng Radxa: 192.168.50.1)
        self.stop_evt = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
            except OSError:
                pass
            self.sock.bind((bind, port))
        except OSError:
            self.sock.close()
            raise
        self.sock.settimeout(0.2)
        self.rcvbuf = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        self.depack = H264Depacketizer(on_au, stats, self.name)

    @property
    def last_packet_time(self):
        return self.depack.last_pkt_time

    def run(self):
        boost_current_thread()
        recvfrom, push, st = self.sock.recvfrom, self.depack.push, self.stats
        prev = 0.0
        while not self.stop_evt.is_set():
            try:
                pkt, addr = recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                if self.stop_evt.is_set():
                    break
                time.sleep(0.01)         # vd WSAECONNRESET trên Windows — bỏ qua
                continue
            now = clock()
            if addr[0] != self.source:
                LOG(self.name, f"nhận gói từ {addr[0]}:{addr[1]}"
                               + (f" (trước đó từ {self.source})" if self.source else ""), "src")
                self.source = addr[0]
            if prev:
                gap = now - prev         # khoảng lặng giữa 2 gói liên tiếp
                if gap > st.max_gap:
                    st.max_gap = gap
                if gap > st.gap_since_display:
                    st.gap_since_display = gap
                if gap >= NO_SIGNAL_SEC:
                    LOG(self.name, f"CÓ GÓI LẠI sau {gap:.1f} s mất tín hiệu")
                elif gap >= SILENCE_LOG_SEC:
                    LOG(self.name, f"IM LẶNG {gap * 1000:.0f} ms (không có gói nào tới)", "silence")
            prev = now
            try:
                push(pkt, now)
            except Exception:            # gói dị dạng không được làm chết luồng nhận
                st.bad += 1

    def stop(self):
        self.stop_evt.set()
        try:
            self.sock.close()
        except OSError:
            pass


# ----------------------------------------------------------------------------------------------
# 2. Giải mã
# ----------------------------------------------------------------------------------------------
class AUQueue:
    """Hàng đợi NHỎ giữa luồng nhận và luồng giải mã.
    - Tràn → bỏ hết khung cũ, khung mới mang cờ loss_before → bộ giải mã bỏ tiếp tới IDR
      (bỏ khung giữa chuỗi P rồi giải mã tiếp sẽ ra hình nhoè/xám).
    - IDR nguyên vẹn tới khi còn khung cũ chưa giải mã → bỏ luôn khung cũ (IDR làm mới chuỗi
      tham chiếu) để đuổi kịp thời gian thực."""

    def __init__(self, maxlen, stats):
        self.maxlen = max(1, maxlen)
        self.stats = stats
        self._q = deque()
        self._cond = threading.Condition()

    def __len__(self):
        return len(self._q)

    def put(self, au):
        with self._cond:
            q = self._q
            if q and au.is_idr and not au.corrupt:
                LOG("giải mã", f"IDR tới khi còn {len(q)} khung chưa giải mã → bỏ chúng để đuổi kịp", "qflush")
                self.stats.q_dropped += len(q)
                q.clear()
            elif len(q) >= self.maxlen:
                LOG("giải mã", f"HÀNG ĐỢI TRÀN: bỏ {len(q)} khung chưa giải mã → chờ IDR (máy bận / giải mã chậm?)")
                self.stats.q_dropped += len(q)
                q.clear()
                au.loss_before = True
                au.lost = LOST_RESET     # chuỗi tham chiếu gãy hẳn → phải chờ IDR, không che lỗi được
            q.append(au)
            self._cond.notify()

    def get(self, timeout):
        with self._cond:
            if not self._q:
                self._cond.wait(timeout)
                if not self._q:
                    return None
            return self._q.popleft()


class LatestFrame:
    """Ô chứa đúng 1 khung đã giải mã (mới nhất). Khung chưa kịp vẽ bị khung mới đè — không chờ."""

    def __init__(self):
        self._lock = threading.Lock()
        self._item = None
        self.event = threading.Event()
        self.overwritten = 0

    def put(self, item):
        with self._lock:
            if self._item is not None:
                self.overwritten += 1
            self._item = item
            self.event.set()

    def take(self):
        with self._lock:
            item, self._item = self._item, None
            self.event.clear()
            return item


def enable_low_delay(ctx):
    """Đặt AV_CODEC_FLAG_LOW_DELAY — tương thích nhiều phiên bản PyAV."""
    ok = False
    try:
        ctx.flags = ctx.flags | av.codec.context.Flags.LOW_DELAY
        ok = True
    except Exception:
        try:
            ctx.low_delay = True         # PyAV cũ có thuộc tính riêng cho từng cờ
            ok = True
        except Exception:
            pass
    try:
        opts = dict(ctx.options or {})
        opts["flags"] = "+low_delay"     # dự phòng: áp dụng khi codec được mở (lần decode đầu)
        ctx.options = opts
        ok = True
    except Exception:
        pass
    return ok


LOSS_MODES = ("auto", "conceal", "idr")
LOSS_MODE_TEXT = {
    "auto": "mất ít gói → che lỗi (không đứng hình), mất nhiều → bỏ tới IDR",
    "conceal": "mất gói → luôn che lỗi",
    "idr": "mất gói → bỏ tới IDR kế tiếp (đứng hình ≤ 1 s, không nhoè)",
}


class H264Decoder(threading.Thread):
    """Luồng giải mã: lấy khung nén theo thứ tự. Mất gói, theo loss_mode:
    - "idr": bỏ khung tới IDR kế tiếp (không nhoè, nhưng 1 gói mất = đứng hình tới ~1 s);
    - "conceal": vẫn giải mã, decoder tự che phần thiếu (không đứng hình, có thể nhoè tới IDR);
    - "auto" (mặc định): mất lẻ tẻ (≤ AUTO_CONCEAL_MAX_LOST gói) → che lỗi; mất nhiều / luồng khởi động
      lại / hàng đợi tràn → bỏ tới IDR."""

    def __init__(self, queue, out, stats, loss_mode="auto", threads=1, hwaccel=None):
        super().__init__(name="h264-dec", daemon=True)
        self.queue, self.out, self.stats = queue, out, stats
        self.loss_mode = loss_mode
        self.waiting_idr = True
        self.stop_evt = threading.Event()
        self.desc = "CPU"
        self.ctx = self._create_context(threads, hwaccel)

    def _create_context(self, threads, hwaccel):
        ctx = None
        if hwaccel:
            try:
                from av.codec.hwaccel import HWAccel
                ctx = av.CodecContext.create(
                    "h264", "r", hwaccel=HWAccel(device_type=hwaccel, allow_software_fallback=True))
                self.desc = f"phần cứng {hwaccel}"
            except Exception as e:
                print(f"[giải mã] không bật được hwaccel '{hwaccel}' ({e}) → dùng CPU")
        if ctx is None:
            ctx = av.CodecContext.create("h264", "r")
        try:
            ctx.thread_type = "SLICE"    # KHÔNG dùng FRAME: frame-threading giữ thêm (N−1) khung
        except Exception:
            pass
        try:
            ctx.thread_count = max(1, threads)
        except Exception:
            pass
        if not enable_low_delay(ctx):
            print("[giải mã] cảnh báo: không đặt được cờ LOW_DELAY")
        if threads > 1:
            self.desc += f", {threads} luồng SLICE"
        return ctx

    def stop(self):
        self.stop_evt.set()

    def run(self):
        boost_current_thread()
        ctx, st, q = self.ctx, self.stats, self.queue
        need_idr = True
        wait_t0, wait_n = None, 0        # đang chờ IDR từ lúc nào, đã bỏ bao nhiêu khung
        concealed = 0                    # số gói đã che lỗi kể từ IDR gần nhất (chế độ auto)
        pending = {}                     # au.index → lúc nhận gói cuối (khớp lại qua frame.pts)
        lag_warned = False
        while not self.stop_evt.is_set():
            au = q.get(0.2)
            if au is None:
                continue
            mode = self.loss_mode
            small = au.lost <= AUTO_CONCEAL_MAX_LOST    # mất lẻ tẻ (không phải luồng reset / hàng đợi tràn)
            if (au.corrupt or au.loss_before) and not need_idr:
                if mode == "conceal" or (mode == "auto" and small and concealed + au.lost <= AUTO_CONCEAL_BUDGET):
                    concealed += au.lost
                    st.concealed += 1
                    LOG("giải mã", f"CHE LỖI khung #{au.index}: thiếu {au.lost} gói → vẫn giải mã "
                                   "(không đứng hình; có thể nhoè nhẹ tới IDR kế tiếp)", "conceal")
                else:
                    need_idr = True
                    wait_t0, wait_n = clock(), 0
                    why = ("luồng khởi động lại / hàng đợi tràn" if au.lost >= LOST_RESET else
                           f"mất {au.lost} gói" + (f", đã che {concealed} gói từ IDR trước" if concealed else ""))
                    LOG("giải mã", f"BẮT ĐẦU CHỜ IDR: khung #{au.index} {why}")
            if need_idr:
                if au.is_idr and (not au.corrupt or mode == "conceal" or (mode == "auto" and small)):
                    need_idr = False
                    if wait_t0 is None:
                        LOG("giải mã", f"BẮT ĐẦU GIẢI MÃ từ IDR #{au.index}")
                    else:
                        LOG("giải mã", f"HẾT CHỜ IDR sau {(clock() - wait_t0) * 1000:.0f} ms, đã bỏ {wait_n} khung")
                    wait_t0 = None
                else:
                    st.idr_dropped += 1
                    wait_n += 1
                    self.waiting_idr = True
                    continue
            self.waiting_idr = False
            pkt = av.Packet(au.data)
            pkt.pts = au.index
            pending[au.index] = au.t_last
            t0 = clock()
            try:
                frames = ctx.decode(pkt)
            except Exception as e:       # dữ liệu hỏng → bỏ khung này
                st.dec_errors += 1
                LOG("giải mã", f"LỖI GIẢI MÃ khung #{au.index} ({'IDR' if au.is_idr else 'P'}, "
                               f"{len(au.data)} B): {e}", "decerr")
                if mode != "conceal":
                    need_idr = True
                    wait_t0, wait_n = clock(), 0
                continue
            if au.is_idr:
                concealed = 0 if not au.corrupt else au.lost
            t1 = clock()
            dms = (t1 - t0) * 1000.0
            st.dec_ms.append(dms)
            if dms > 50:
                LOG("giải mã", f"GIẢI MÃ CHẬM {dms:.0f} ms (khung #{au.index}) — CPU bận?", "slowdec")
            for frame in frames:
                idx = frame.pts
                t_rx = pending.pop(idx, au.t_last) if idx is not None else au.t_last
                if idx is not None and idx < au.index and not lag_warned:
                    lag_warned = True
                    print(f"[giải mã] cảnh báo: decoder trả khung trễ {au.index - idx} khung "
                          "(LOW_DELAY không hiệu lực?)")
                st.decoded += 1
                self.out.put((frame, t_rx, t1))
            if len(pending) > 64:
                for k in [k for k in pending if k < au.index - 32]:
                    del pending[k]


# ----------------------------------------------------------------------------------------------
# 3. Hiển thị
# ----------------------------------------------------------------------------------------------
class Overlay:
    """Lớp vẽ chồng lên video. Giai đoạn sau: box mục tiêu từ JSON `uav_track` (xem luồng thô 5601)
    và click chuột chọn mục tiêu gửi về UAV. Toạ độ chuẩn hoá 0..1 theo khung video (xem to_px)."""

    def draw(self, surface, video_rect, now):
        pass

    def on_click(self, nx, ny, button):
        """nx, ny: toạ độ click chuẩn hoá 0..1 trong khung video. Trả True nếu đã xử lý."""
        return False

    def close(self):
        pass

    @staticmethod
    def to_px(video_rect, nx, ny):
        return (video_rect.x + nx * video_rect.w, video_rect.y + ny * video_rect.h)


def _box(v):
    """Box chuẩn hoá x1,y1,x2,y2 hợp lệ → tuple 4 float, sai định dạng → None."""
    try:
        b = tuple(float(x) for x in v)
    except (TypeError, ValueError):
        return None
    if len(b) != 4 or not all(math.isfinite(x) for x in b):
        return None
    return b


def _track(d):
    """Làm sạch 1 track/target trong JSON từ UAV (không tin tuyệt đối định dạng) — giống gs_overlay.py."""
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
    """Nhận JSON "uav_track" (~30 gói/s) mà Radxa chuyển tiếp tới laptop (gs_overlay.py --telem-udp),
    giữ gói mới nhất đã làm sạch."""

    def __init__(self, bind, port):
        super().__init__(name="track-rx", daemon=True)
        self.port = port
        self.stop_evt = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((bind, port))
        except OSError:
            self.sock.close()
            raise
        self.sock.settimeout(0.5)
        self.lock = threading.Lock()
        self.latest = None
        self.rx_time = 0.0
        self.count = 0               # tổng số gói hợp lệ (UI tính Hz mỗi giây)
        self.bad = 0
        self.last_det = None         # trạng thái bộ phát hiện trên UAV ("ok" / "retrying" / "degraded")
        self.last_has_tgt = False

    def run(self):
        prev = 0.0
        while not self.stop_evt.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self.stop_evt.is_set():
                    break
                time.sleep(0.01)
                continue
            now = clock()
            try:
                raw = json.loads(data)
                if not isinstance(raw, dict) or raw.get("type") != "uav_track":
                    continue
                tracks = raw.get("tracks")
                tracks = [t for t in map(_track, tracks) if t] if isinstance(tracks, list) else []
                pkt = {"detector": str(raw.get("detector", "?"))[:12],
                       "target": _track(raw.get("target")), "tracks": tracks}
            except Exception:            # gói hỏng không được làm chết luồng nhận
                self.bad += 1
                LOG("toạ độ", f"gói JSON hỏng: {data[:120]!r}", "trackbad")
                continue
            if not prev:
                LOG("toạ độ", f"BẮT ĐẦU NHẬN toạ độ từ {addr[0]}: {data[:400].decode(errors='replace')}")
            elif now - prev > 1.0:
                LOG("toạ độ", f"CÓ TOẠ ĐỘ LẠI sau {now - prev:.1f} s")
            prev = now
            det, has_tgt = pkt["detector"], pkt["target"] is not None
            if det != self.last_det:
                LOG("toạ độ", f"bộ phát hiện trên UAV: {self.last_det} → {det}"
                              + ("" if det == "ok" else " (Coral lỗi / đang thử lại → không phát hiện mục tiêu)"))
                self.last_det = det
            if has_tgt != self.last_has_tgt:
                t = pkt["target"]
                LOG("toạ độ", f"CÓ MỤC TIÊU #{t['id']} {t['cls']} {t['score']:.2f}" if has_tgt else "MẤT MỤC TIÊU",
                    "target")
                self.last_has_tgt = has_tgt
            with self.lock:
                self.latest, self.rx_time = pkt, now
                self.count += 1

    def get(self, now):
        """Gói mới nhất, hoặc None nếu chưa có / quá cũ (> TRACK_MAX_AGE)."""
        with self.lock:
            if self.latest is None or now - self.rx_time > TRACK_MAX_AGE:
                return None
            return self.latest

    def stop(self):
        self.stop_evt.set()
        try:
            self.sock.close()
        except OSError:
            pass


class GsSubscriber(threading.Thread):
    """Đăng ký nhận video với trạm mặt đất (gs_forward.py trên Radxa): mỗi giây gửi 1 gói UDP nhỏ tới
    GS:5599 → Radxa gửi video + toạ độ về đúng IP của laptop (hết hạn sau 5 s nếu app tắt). Nhờ vậy
    laptop đổi IP không phải sửa gì trên Radxa."""

    def __init__(self, gs_ip, port=GS_SUBSCRIBE_PORT):
        super().__init__(name="gs-sub", daemon=True)
        self.addr = (gs_ip, port)
        self.stop_evt = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def run(self):
        ok = None
        while True:
            try:
                self.sock.sendto(b"uav_viewer", self.addr)
                if ok is not True:
                    LOG("app", f"đăng ký nhận video với trạm mặt đất {self.addr[0]}:{self.addr[1]}")
                ok = True
            except OSError as e:                 # chưa cắm dây / Radxa chưa lên → thử lại sau
                if ok is not False:
                    LOG("app", f"không gửi được gói đăng ký tới {self.addr[0]}:{self.addr[1]}: {e}")
                ok = False
            if self.stop_evt.wait(1.0):
                break

    def stop(self):
        self.stop_evt.set()
        self.sock.close()


class TrackOverlay(Overlay):
    """Vẽ box mục tiêu ngay trên laptop, giống gs_overlay.py trên Radxa: mục tiêu xanh lá (đang thấy)
    / vàng (dự đoán, lost_frames > 0), các track khác xanh lơ, nhãn "#id lớp %", tâm ngắm, dòng
    TELEM / TARGET góc trên trái. Vẽ ở độ phân giải màn hình nên nét sắc; Radxa không phải vẽ + nén lại."""

    def __init__(self, receiver):
        self.rx = receiver
        self.enabled = True
        self.rate = 0.0              # gói toạ độ / giây (Viewer cập nhật mỗi giây)
        self._font = None
        self._font_h = 0
        self._cache = {}

    def _text(self, text, bg):
        key = (text, bg)
        s = self._cache.get(key)
        if s is None:
            t = self._font.render(text, True, (255, 255, 255))
            pad = max(3, t.get_height() // 6)
            s = pygame.Surface((t.get_width() + 2 * pad, t.get_height() + pad))
            s.fill(bg)
            s.blit(t, (pad, pad // 2))
            if len(self._cache) > 256:
                self._cache.clear()
            self._cache[key] = s
        return s

    @staticmethod
    def _rect(vr, box):
        x1, y1, x2, y2 = (min(max(v, 0.0), 1.0) for v in box)
        return pygame.Rect(vr.x + round(x1 * vr.w), vr.y + round(y1 * vr.h),
                           max(1, round((x2 - x1) * vr.w)), max(1, round((y2 - y1) * vr.h)))

    def draw(self, surface, vr, now):
        if not self.enabled or vr.w <= 0 or vr.h <= 0:
            return
        if self._font is None or self._font_h != vr.h:      # chữ tỉ lệ theo cỡ khung (như Radxa: h/40)
            self._font, self._font_h = sys_font(max(13, vr.h // 40), bold=True), vr.h
            self._cache.clear()
        k = vr.h / 1080.0
        thick, thin = max(2, round(4 * k)), max(1, round(2 * k))
        cx, cy, size, gap = vr.centerx, vr.centery, round(30 * k), round(8 * k)   # tâm ngắm
        for a, b in (((cx - size, cy), (cx - gap, cy)), ((cx + gap, cy), (cx + size, cy)),
                     ((cx, cy - size), (cx, cy - gap)), ((cx, cy + gap), (cx, cy + size))):
            pygame.draw.line(surface, (235, 235, 235), a, b, thin)
        pkt = self.rx.get(now)
        line1 = f"TELEM {self.rate:.0f} Hz"
        warn = None
        if pkt is None:
            line2 = "NO TELEMETRY"
        else:
            line1 += f" | DET {pkt['detector']}"
            if pkt["detector"] != "ok":      # UAV tự báo: Coral lỗi / đang thử lại → không có box nào
                warn = f"BỘ PHÁT HIỆN TRÊN UAV LỖI ({pkt['detector']}) — kiểm tra Coral / service uav-tracker trên Pi"
            tgt = pkt["target"]
            tid = tgt["id"] if tgt else None
            for tr in pkt["tracks"]:
                if tr["id"] != tid:
                    pygame.draw.rect(surface, BOX_CYAN, self._rect(vr, tr["box"]), thin)
            if tgt:
                lost = tgt["lost_frames"]
                color = BOX_YELLOW if lost else BOX_GREEN
                r = self._rect(vr, tgt["box"])
                pygame.draw.rect(surface, color, r, thick)
                label = f"#{tid} {tgt['cls']} {round(tgt['score'] * 100)}%" + (" (pred)" if lost else "")
                lab = self._text(label, tuple(c * 2 // 5 for c in color))
                lx = min(max(r.x, vr.x), vr.right - lab.get_width())
                ly = r.y - lab.get_height() - thick
                if ly < vr.y:                                  # sát mép trên → đặt nhãn trong box
                    ly = r.y + thick
                surface.blit(lab, (lx, ly))
                ex, ey = tgt["err"]
                line2 = (f"TARGET #{tid} {tgt['cls']} {tgt['score']:.2f}  err x={ex:+.2f} y={ey:+.2f}"
                         + (f"  PRED {lost}" if lost else ""))
            else:
                line2 = "NO TARGET"
        y = vr.y + round(12 * k)
        for line, bg in ((line1, (25, 25, 25)), (line2, (25, 25, 25)), (warn, (170, 20, 20))):
            if line:
                s = self._text(line, bg)
                surface.blit(s, (vr.x + round(12 * k), y))
                y += s.get_height()

    def close(self):
        self.rx.stop()


def make_scaler(interp):
    """Trả về scale(frame, w, h) → VideoFrame rgb24 đúng cỡ cửa sổ (swscale đổi màu + co ảnh trong
    1 bước). Dùng lại 1 VideoReformatter để SwsContext được cache giữa các khung."""
    probe = av.VideoFrame(64, 48, "yuv420p")
    funcs = []
    try:
        from av.video.reformatter import VideoReformatter
        ref = VideoReformatter()
        funcs.append(lambda f, w, h, **kw: ref.reformat(f, width=w, height=h, format="rgb24", **kw))
    except Exception:
        pass
    funcs.append(lambda f, w, h, **kw: f.reformat(width=w, height=h, format="rgb24", **kw))
    for fn in funcs:
        for kw in ([{"interpolation": interp}] if interp else []) + [{}]:
            try:
                fn(probe, 32, 24, **kw)
            except Exception:
                continue
            return lambda f, w, h, fn=fn, kw=kw: fn(f, w, h, **kw)
    raise RuntimeError("PyAV không chuyển được YUV → RGB")


def sys_font(size, bold=False):
    """Font có dấu tiếng Việt (Segoe UI / Arial trên Windows)."""
    try:
        return pygame.font.SysFont("segoeui,arial,tahoma,dejavusans", size, bold=bold)
    except Exception:
        return pygame.font.Font(None, size)


def fill_bars(screen, x, y, w, h, W, H):
    """Tô đen phần viền ngoài khung video (letterbox) để HUD / chữ cũ không còn sót lại."""
    black = (0, 0, 0)
    if y > 0:
        screen.fill(black, (0, 0, W, y))
    if y + h < H:
        screen.fill(black, (0, y + h, W, H - y - h))
    if x > 0:
        screen.fill(black, (0, y, x, h))
    if x + w < W:
        screen.fill(black, (x + w, y, W - x - w, h))


class Viewer:
    """Luồng chính (UI): chỉ vẽ khung giải mã mới nhất — khung đến muộn bị đè, không chờ."""

    def __init__(self, args, stats, slot, rx, dec, queue, overlays=(), ips=(), monitor=None, track=None):
        self.args, self.stats, self.slot = args, stats, slot
        self.rx, self.dec, self.queue = rx, dec, queue
        self.monitor = monitor           # RtpReceiver cổng phụ chỉ để ghi log so sánh (--monitor-port)
        self._mprev = monitor.stats.snapshot() if monitor else None
        self.track = track               # TrackReceiver (toạ độ mục tiêu) hoặc None
        self._prev_track = 0
        self.overlays = list(overlays)
        self.ips = list(ips)
        self.show_hud = not args.no_hud
        self.running = True
        self.fullscreen = False

        pygame.display.init()
        pygame.font.init()
        info = pygame.display.Info()
        desk_w = info.current_w if info.current_w > 0 else 1920
        desk_h = info.current_h if info.current_h > 0 else 1080
        # cửa sổ mặc định: 16:9 chiếm ~85% màn hình (video 1080p; F / F11 = toàn màn hình, đúng 1:1)
        w = min(1920, int(desk_w * 0.85), int(desk_h * 0.85) * 16 // 9) & ~1
        self.windowed_size = args.size or (w, (w * 9 // 16) & ~1)
        self.font = sys_font(max(14, round(desk_h / 70)))
        self.big_font = sys_font(max(36, round(desk_h / 14)), bold=True)
        kind = " (box vẽ trên laptop)" if track else ""
        pygame.display.set_caption(f"UAV Viewer — UDP :{args.port}{kind}")
        self._set_mode(args.fullscreen)
        self.scale = make_scaler(args.scale)

        self.last_frame = None       # VideoFrame mới nhất (giữ để vẽ lại khi đổi cỡ cửa sổ / NO SIGNAL)
        self.frame_surf = None       # khung đã chuyển RGB + co theo cửa sổ
        self.frame_size = None
        self.res = None
        self.video_rect = pygame.Rect(0, 0, 0, 0)
        self.hud_lines = ["đang chờ dữ liệu..."]
        self.hud_surf = None
        self.ages, self.d2d = [], []
        self.displayed = 0
        self.last_display_t = 0.0
        self._warned = set()
        self._last_click = 0.0
        self._prev = stats.snapshot()
        self._prev_t = clock()
        self._prev_disp = 0
        self._prev_over = 0
        self._disp_snap = stats.snapshot()   # bộ đếm lúc hiện khung gần nhất (để tính lúc đứng hình)
        self._status_title = None
        self._status_since = 0.0
        self.freezes = 0
        self.freeze_total = 0.0
        self.t_start = clock()
        self._redraw_events = {pygame.VIDEORESIZE, pygame.VIDEOEXPOSE}
        for name in ("WINDOWSIZECHANGED", "WINDOWEXPOSED", "WINDOWRESTORED"):
            if hasattr(pygame, name):
                self._redraw_events.add(getattr(pygame, name))

    # --- cửa sổ ---
    def _set_mode(self, fullscreen):
        if fullscreen:   # (0, 0) = độ phân giải desktop → FULLSCREEN_DESKTOP, không đổi mode màn hình
            pygame.display.set_mode((0, 0), pygame.FULLSCREEN, vsync=0)
        else:            # cửa sổ thường: SDL không chờ vsync khi cập nhật
            pygame.display.set_mode(self.windowed_size, pygame.RESIZABLE, vsync=0)
        self.fullscreen = fullscreen
        self.frame_surf = None
        self.shade = None
        self.need_redraw = True

    def toggle_fullscreen(self):
        if not self.fullscreen:
            surf = pygame.display.get_surface()
            if surf is not None:
                self.windowed_size = surf.get_size()
        self._set_mode(not self.fullscreen)
        LOG("hiển thị", "chuyển " + ("toàn màn hình" if self.fullscreen else "cửa sổ thường"))

    def _warn_once(self, msg):
        if msg not in self._warned:
            self._warned.add(msg)
            print(msg)

    # --- vòng lặp chính ---
    def run(self):
        next_roll = clock() + 1.0
        last_draw = 0.0
        prev_loop = clock()
        while self.running:
            self.slot.event.wait(0.02)   # thức ngay khi có khung mới; tối đa 20 ms để xử lý sự kiện
            for ev in pygame.event.get():
                self._on_event(ev)
            t_loop = clock()
            if t_loop - prev_loop > UI_STALL_SEC:   # vd Windows chặn vòng lặp khi kéo / đổi cỡ cửa sổ
                LOG("hiển thị", f"UI BỊ CHẶN {(t_loop - prev_loop) * 1000:.0f} ms "
                                "(kéo / đổi cỡ cửa sổ, máy bận?)", "uistall")
            prev_loop = t_loop
            if not self.running:
                break
            item = self.slot.take()
            now = clock()
            if item is not None or self.need_redraw or now - last_draw > 0.25:
                self._draw(item)
                last_draw = clock()
            if now >= next_roll:
                self._roll_stats(now)
                next_roll = max(next_roll + 1.0, now + 0.5)

    def _on_event(self, ev):
        if ev.type == pygame.QUIT:
            self.running = False
        elif ev.type == pygame.KEYDOWN:
            if ev.key in (pygame.K_ESCAPE, pygame.K_q):
                self.running = False
            elif ev.key in (pygame.K_f, pygame.K_F11):
                self.toggle_fullscreen()
            elif ev.key == pygame.K_h:
                self.show_hud = not self.show_hud
                self.need_redraw = True
            elif ev.key == pygame.K_b:
                for ov in self.overlays:
                    if hasattr(ov, "enabled"):
                        ov.enabled = not ov.enabled
                self.need_redraw = True
            elif ev.key == pygame.K_c:                 # đổi cách xử lý mất gói: auto → conceal → idr
                m = LOSS_MODES[(LOSS_MODES.index(self.dec.loss_mode) + 1) % len(LOSS_MODES)]
                self.dec.loss_mode = m
                print(f"[giải mã] chế độ mất gói: {m} — {LOSS_MODE_TEXT[m]}")
                LOG("giải mã", f"chế độ mất gói: {m} — {LOSS_MODE_TEXT[m]}")
                self.need_redraw = True
        elif ev.type == pygame.MOUSEBUTTONDOWN:
            self._on_click(ev)
        elif ev.type in self._redraw_events:
            self.need_redraw = True

    def _on_click(self, ev):
        if ev.button == 1:
            now = clock()
            if now - self._last_click < 0.35:        # nháy đúp → toàn màn hình
                self._last_click = 0.0
                self.toggle_fullscreen()
                return
            self._last_click = now
        r = self.video_rect
        if r.w > 0 and r.h > 0 and r.collidepoint(ev.pos):
            nx, ny = (ev.pos[0] - r.x) / r.w, (ev.pos[1] - r.y) / r.h
            for ov in self.overlays:
                if ov.on_click(nx, ny, ev.button):
                    break

    # --- vẽ ---
    def _to_surface(self, frame, w, h):
        rgb = self.scale(frame, w, h)
        arr = rgb.to_ndarray()
        if not arr.flags.c_contiguous:
            arr = np.ascontiguousarray(arr)
        try:
            return pygame.image.frombuffer(arr, (w, h), "RGB")
        except (TypeError, ValueError):
            return pygame.image.frombuffer(arr.tobytes(), (w, h), "RGB")

    def _draw(self, item):
        self.need_redraw = False
        screen = pygame.display.get_surface()
        if screen is None:
            return
        W, H = screen.get_size()
        if W < 2 or H < 2:                           # cửa sổ đang thu nhỏ
            return
        now = clock()
        if item is not None:
            if self.last_display_t and now - self.last_display_t >= FREEZE_LOG_SEC:
                self._log_freeze(now - self.last_display_t)
            self._disp_snap = self.stats.snapshot()
            self.stats.gap_since_display = 0.0
            self.last_frame = item[0]
            self.last_display_t = now
        frame = self.last_frame
        if frame is not None:
            fw, fh = frame.width, frame.height
            if (fw, fh) != self.res:
                if self.res:
                    print(f"[video] đổi độ phân giải {self.res[0]}x{self.res[1]} → {fw}x{fh}")
                LOG("hiển thị", f"độ phân giải video {fw}x{fh}")
                self.res = (fw, fh)
            s = min(W / fw, H / fh)                  # giữ tỉ lệ khung hình
            dw = max(2, min(W, int(fw * s + 0.5)))
            dh = max(2, min(H, int(fh * s + 0.5)))
            x, y = (W - dw) // 2, (H - dh) // 2
            if item is not None or self.frame_surf is None or self.frame_size != (dw, dh):
                try:
                    self.frame_surf = self._to_surface(frame, dw, dh)
                    self.frame_size = (dw, dh)
                except Exception as e:
                    self.frame_surf = None
                    self._warn_once(f"[hiển thị] lỗi chuyển khung sang RGB: {e}")
            if self.frame_surf is not None:
                screen.blit(self.frame_surf, (x, y))
            else:
                screen.fill((0, 0, 0), (x, y, dw, dh))
            fill_bars(screen, x, y, dw, dh, W, H)
            self.video_rect = pygame.Rect(x, y, dw, dh)
        else:
            screen.fill((0, 0, 0))
            self.video_rect = pygame.Rect(0, 0, 0, 0)
        for ov in self.overlays:
            ov.draw(screen, self.video_rect, now)
        status = self._status(now)
        title = status[0] if status else None
        if title != self._status_title:
            if self._status_title:
                LOG("hiển thị", f"HẾT '{self._status_title}' sau {now - self._status_since:.1f} s")
            if title:
                LOG("hiển thị", f"TRẠNG THÁI: {title}")
            self._status_title, self._status_since = title, now
        if status:
            self._draw_status(screen, status)
        if self.show_hud:
            self._draw_hud(screen)
        pygame.display.flip()
        if item is not None:
            t = clock()
            self.displayed += 1
            self.ages.append((t - item[1]) * 1000.0)   # tuổi khung: nhận gói cuối → vẽ xong
            self.d2d.append((t - item[2]) * 1000.0)    # giải mã xong → vẽ xong

    def _log_freeze(self, dur):
        """Ghi 1 lần đứng hình kèm số liệu trong lúc đứng → đoán nguyên nhân chính."""
        cur = self.stats.snapshot()
        d = {k: cur[k] - self._disp_snap[k] for k in cur}
        gap = self.stats.gap_since_display
        if gap >= 0.7 * dur:
            cause = f"KHÔNG CÓ GÓI TỚI {gap * 1000:.0f} ms (đường truyền / nguồn phát bị khựng)"
        elif d["lost"] or d["late"] or d["corrupt_aus"]:
            cause = "MẤT GÓI → bỏ khung chờ IDR" if d["idr_dropped"] else "MẤT GÓI"
        elif d["dec_errors"]:
            cause = "LỖI GIẢI MÃ"
        elif d["q_dropped"]:
            cause = "HÀNG ĐỢI TRÀN (máy bận)"
        elif d["aus"] < dur * 15:
            cause = f"NGUỒN PHÁT GỬI THƯA ({d['aus']} khung trong {dur:.2f} s)"
        elif d["decoded"] > 1:
            cause = "UI BỊ CHẶN (kéo / đổi cỡ cửa sổ, máy bận?)"
        else:
            cause = "không rõ"
        self.freezes += 1
        self.freeze_total += dur
        LOG("hiển thị", f"ĐỨNG HÌNH {dur * 1000:.0f} ms → {cause} | trong lúc đứng: nhận {d['packets']} gói / "
                        f"{d['aus']} khung, mất {d['lost']} gói, muộn {d['late']}, khung hỏng {d['corrupt_aus']}, "
                        f"bỏ chờ IDR {d['idr_dropped']}, tràn {d['q_dropped']}, lỗi giải mã {d['dec_errors']}, "
                        f"giải mã ra {d['decoded']}, lặng dài nhất {gap * 1000:.0f} ms")

    def _status(self, now):
        """(tiêu đề, [các dòng phụ]) khi không có hình; None khi video chạy bình thường."""
        last = self.rx.last_packet_time
        port = self.args.port
        if last == 0.0:
            return ("NO SIGNAL", [
                f"đang chờ RTP/H.264 trên UDP :{port} — IP laptop: {', '.join(self.ips) or '?'}",
                f"chưa nhận gói nào: kiểm tra dây tới Radxa ({self.args.gs or 'tắt tự đăng ký'}), service"
                " uav-gs-forward trên Radxa, Windows Firewall phải Allow app này"])
        if now - last > NO_SIGNAL_SEC:
            return ("NO SIGNAL", [f"không có gói {now - last:.0f} s — UDP :{port}"])
        if now - self.last_display_t > NO_SIGNAL_SEC:
            if self.dec.waiting_idr:
                return ("ĐANG CHỜ KHUNG IDR", ["có gói RTP, đang chờ khung khoá để bắt đầu giải mã"])
            return ("KHÔNG CÓ HÌNH", ["có gói UDP nhưng không giải mã được — đúng luồng RTP/H.264?"])
        return None

    def _draw_status(self, screen, status):
        title, lines = status
        W, H = screen.get_size()
        if self.last_frame is not None:              # làm tối khung cuối cùng
            if self.shade is None or self.shade.get_size() != (W, H):
                self.shade = pygame.Surface((W, H), pygame.SRCALPHA)
                self.shade.fill((0, 0, 0, 170))
            screen.blit(self.shade, (0, 0))
        t = self.big_font.render(title, True, (255, 70, 70))
        y = H // 2 - t.get_height()
        screen.blit(t, ((W - t.get_width()) // 2, y))
        y += t.get_height() + 8
        for line in lines:
            s = self.font.render(line, True, (235, 235, 235))
            screen.blit(s, ((W - s.get_width()) // 2, y))
            y += s.get_height() + 2

    def _draw_hud(self, screen):
        if self.hud_surf is None:                    # chỉ render chữ lại khi số liệu đổi (1 lần/giây)
            rows = [self.font.render(t, True, (255, 255, 255)) for t in self.hud_lines]
            pad = 6
            w = max(r.get_width() for r in rows) + 2 * pad
            h = sum(r.get_height() for r in rows) + 2 * pad
            surf = pygame.Surface((w, h), pygame.SRCALPHA)
            surf.fill((0, 0, 0, 150))
            y = pad
            for r in rows:
                surf.blit(r, (pad, y))
                y += r.get_height()
            self.hud_surf = surf
        H = screen.get_height()
        screen.blit(self.hud_surf, (8, H - self.hud_surf.get_height() - 8))

    def _roll_stats(self, now):
        st = self.stats
        cur = st.snapshot()
        d = {k: cur[k] - self._prev[k] for k in cur}
        dt = max(1e-3, now - self._prev_t)
        self._prev, self._prev_t = cur, now
        disp, self._prev_disp = self.displayed - self._prev_disp, self.displayed
        over, self._prev_over = self.slot.overwritten - self._prev_over, self.slot.overwritten
        dec_ms, st.dec_ms = st.dec_ms, []
        ages, self.ages = self.ages, []
        d2d, self.d2d = self.d2d, []
        a_avg, a_max = avg_max(ages)
        c_avg, c_max = avg_max(dec_ms)
        s_avg, _ = avg_max(d2d)
        sent = d["packets"] + d["lost"]
        loss = 100.0 * d["lost"] / sent if sent else 0.0
        total = cur["packets"] + cur["lost"]
        loss_total = 100.0 * cur["lost"] / total if total else 0.0
        res = f"{self.res[0]}x{self.res[1]}" if self.res else "?"
        vr = self.video_rect
        flags = []
        if self.dec.waiting_idr:
            flags.append("CHỜ IDR")
        flags.append(f"mất gói: {self.dec.loss_mode}")
        trk = ""
        if self.track:
            rate, self._prev_track = (self.track.count - self._prev_track) / dt, self.track.count
            for ov in self.overlays:
                if isinstance(ov, TrackOverlay):
                    ov.rate = rate
            flags.append(f"toạ độ {rate:.0f} Hz")
            trk = f" | toạ_độ {rate:.0f} Hz"
        self.hud_lines = [
            f"{res} → {vr.w}x{vr.h}  |  hiển thị {disp / dt:.1f} fps  |  nhận {d['aus'] / dt:.1f} fps"
            f"  |  {d['bytes'] * 8 / dt / 1e6:.2f} Mbit/s",
            f"tuổi khung {a_avg:.1f} / {a_max:.1f} ms (tb / max)  |  giải mã {c_avg:.1f} / {c_max:.1f} ms"
            f"  |  giải mã xong → hiện {s_avg:.1f} ms",
            f"mất gói {loss:.2f}% (tổng {loss_total:.2f}%)  |  bỏ khung {d['q_dropped'] + d['idr_dropped']}"
            f"  |  che lỗi {d['concealed']}  |  đè {over}  |  lỗi giải mã {d['dec_errors']}  |  hàng đợi {len(self.queue)}"
            f"  |  UDP :{self.args.port} từ {self.rx.source or '?'}" + ("  |  " + ", ".join(flags) if flags else ""),
        ]
        self.hud_surf = None
        if self.args.print_stats:
            print("  ||  ".join(self.hud_lines), flush=True)
        LOG(self.rx.name, f"nhận {d['aus'] / dt:.1f} khung/s {d['bytes'] * 8 / dt / 1e6:.2f} Mb/s {d['packets']} gói"
                          f" | mất {d['lost']} muộn {d['late']} hỏng {d['corrupt_aus']}"
                          f" | lặng_max {st.max_gap * 1000:.0f} ms tới_muộn_max {st.late_max * 1000:.0f} ms"
                          f" | hiện {disp / dt:.1f} fps tuổi {a_avg:.1f}/{a_max:.1f} ms giải mã {c_avg:.1f}/{c_max:.1f} ms"
                          f" | bỏ {d['q_dropped'] + d['idr_dropped']} che {d['concealed']} lỗi {d['dec_errors']} đè {over}"
                          f" | IDR {st.idr_interval:.2f} s/{st.idr_frames} khung {st.idr_bytes // 1024} KB{trk}")
        st.max_gap = st.late_max = 0.0
        if self.monitor:
            self._log_monitor(dt)

    def _log_monitor(self, dt):
        """Dòng số liệu của cổng phụ (--monitor-port) để so với cổng đang xem."""
        m = self.monitor
        ms = m.stats
        cur = ms.snapshot()
        d = {k: cur[k] - self._mprev[k] for k in cur}
        self._mprev = cur
        LOG(m.name, f"nhận {d['aus'] / dt:.1f} khung/s {d['bytes'] * 8 / dt / 1e6:.2f} Mb/s {d['packets']} gói"
                    f" | mất {d['lost']} muộn {d['late']} hỏng {d['corrupt_aus']}"
                    f" | lặng_max {ms.max_gap * 1000:.0f} ms tới_muộn_max {ms.late_max * 1000:.0f} ms"
                    f" | IDR {ms.idr_interval:.2f} s/{ms.idr_frames} khung {ms.idr_bytes // 1024} KB")
        ms.max_gap = ms.late_max = 0.0


# ----------------------------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------------------------
def parse_size(text):
    try:
        w, h = (int(v) for v in text.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError("dạng WxH, vd 1280x720")
    return max(160, w), max(120, h)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Xem video UAV (RTP/H.264 qua UDP) với độ trễ thấp — thay cho VLC.")
    p.add_argument("--port", type=int, default=5600,
                   help="cổng UDP nhận video thô từ UAV do Radxa chuyển tiếp (gs_forward.py, mặc định 5600)")
    p.add_argument("--track-port", type=int, default=DEFAULT_TRACK_PORT, metavar="PORT",
                   help=f"cổng nhận JSON toạ độ mục tiêu để vẽ box trên laptop (mặc định {DEFAULT_TRACK_PORT}; 0 = tắt)")
    p.add_argument("--gs", default=DEFAULT_GS, metavar="IP",
                   help=f"IP trạm mặt đất để tự đăng ký nhận video (gửi gói tới cổng {GS_SUBSCRIBE_PORT} mỗi giây; "
                        f"mặc định {DEFAULT_GS}; chuỗi rỗng = tắt)")
    p.add_argument("--bind", default="0.0.0.0", help="địa chỉ nghe (mặc định 0.0.0.0)")
    p.add_argument("--loss-mode", choices=LOSS_MODES, default="auto",
                   help="xử lý mất gói: auto (mặc định: mất ≤ 20 gói → che lỗi, không đứng hình; mất nhiều → "
                        "bỏ tới IDR), conceal (luôn che lỗi), idr (luôn bỏ tới IDR kế tiếp). Phím C để đổi")
    p.add_argument("--conceal", action="store_true", help="= --loss-mode conceal")
    p.add_argument("--fullscreen", action="store_true", help="mở toàn màn hình")
    p.add_argument("--size", type=parse_size, default=None,
                   help="cỡ cửa sổ ban đầu WxH (mặc định ~85%% màn hình, 16:9)")
    p.add_argument("--no-hud", action="store_true", help="tắt HUD lúc khởi động (bật lại bằng phím H)")
    p.add_argument("--scale", default="BILINEAR",
                   choices=["FAST_BILINEAR", "BILINEAR", "BICUBIC", "AREA", "POINT"],
                   help="thuật toán co ảnh của swscale (mặc định BILINEAR)")
    p.add_argument("--threads", type=int, default=1,
                   help="số luồng giải mã kiểu SLICE (không bao giờ dùng frame-threading)")
    p.add_argument("--hwaccel", default=None, metavar="TEN",
                   help="thử giải mã phần cứng, vd d3d11va (PyAV ≥ 14). Mặc định: CPU")
    p.add_argument("--queue", type=int, default=6,
                   help="số khung nén tối đa chờ giải mã; tràn → bỏ tới IDR (mặc định 6)")
    p.add_argument("--rcvbuf", type=int, default=4 << 20, help="SO_RCVBUF của socket, byte (mặc định 4 MB)")
    p.add_argument("--print-stats", action="store_true", help="in số liệu HUD ra console mỗi giây")
    p.add_argument("--verbose", action="store_true", help="hiện log cảnh báo / lỗi của FFmpeg")
    p.add_argument("--log", default=None, metavar="FILE",
                   help="file log chẩn đoán (mặc định: thư mục logs cạnh app, giữ 30 file mới nhất)")
    p.add_argument("--no-log", action="store_true", help="không ghi log chẩn đoán")
    p.add_argument("--monitor-port", type=int, default=None, metavar="PORT",
                   help="nghe thêm 1 cổng chỉ để ghi log so sánh (vd 5601 khi đang xem 5600), không hiển thị")
    return p.parse_args(argv)


def setup_ffmpeg_logs(verbose):
    """Ẩn log lỗi FFmpeg (mỗi lần mất gói decoder in rất nhiều dòng); --verbose để xem."""
    logging.getLogger("libav").setLevel(logging.WARNING if verbose else logging.CRITICAL)
    if verbose:
        logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
        try:
            import av.logging as avlog
            avlog.set_level(avlog.WARNING)
        except Exception:
            pass


def main(argv=None):
    global LOG
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:   # ghi ra file / pipe (vd --print-stats > log.txt): dùng UTF-8 cho đúng tiếng Việt
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = parse_args(argv)
    setup_windows_process()
    sys.setswitchinterval(0.001)     # GIL đổi luồng nhanh hơn → luồng nhận / giải mã không phải chờ lâu
    setup_ffmpeg_logs(args.verbose)
    if not args.no_log:
        path = args.log or default_log_path(args.port)
        if path:
            try:
                LOG = EventLog(path)
            except OSError as e:
                print(f"[cảnh báo] không ghi được log {path}: {e}")

    stats = Stats()
    slot = LatestFrame()
    queue = AUQueue(args.queue, stats)
    try:
        rx = RtpReceiver(args.bind, args.port, args.rcvbuf, queue.put, stats)
    except OSError as e:
        print(f"[lỗi] không mở được UDP {args.bind}:{args.port} — {e}")
        print("       Cổng đang bị chiếm? Hãy đóng VLC / ffplay / app khác đang nghe cổng này.")
        LOG("app", f"không mở được UDP {args.bind}:{args.port}: {e}")
        LOG.close()
        return 2
    mon = None
    if args.monitor_port:
        try:
            mon = RtpReceiver(args.bind, args.monitor_port, args.rcvbuf, lambda au: None, Stats())
        except OSError as e:
            print(f"[cảnh báo] không mở được cổng giám sát {args.monitor_port}: {e}")
    track_port = args.track_port
    track = None
    if track_port:
        try:
            track = TrackReceiver(args.bind, track_port)
        except OSError as e:
            print(f"[cảnh báo] không mở được cổng toạ độ {track_port} ({e}) → không vẽ box trên laptop")
    loss_mode = "conceal" if args.conceal else args.loss_mode
    dec = H264Decoder(queue, slot, stats, loss_mode=loss_mode, threads=args.threads, hwaccel=args.hwaccel)
    ips = local_ips()

    versions = f"Python {platform.python_version()}, PyAV {av.__version__}, pygame {pygame.version.ver}"
    mode = f"mất gói: {loss_mode} ({LOSS_MODE_TEXT[loss_mode]})"
    print(f"UAV Viewer — {versions}")
    print(f"Nghe RTP/H.264 trên UDP {args.bind}:{args.port} (SO_RCVBUF {rx.rcvbuf // 1024} KB) · giải mã {dec.desc}"
          f" · {mode}" + (f" · giám sát thêm cổng {args.monitor_port}" if mon else ""))
    if track:
        print(f"Vẽ box trên laptop: nhận toạ độ UDP :{track_port}")
    sub = GsSubscriber(args.gs) if args.gs else None
    if sub:
        print(f"Tự đăng ký nhận video với trạm mặt đất {args.gs}:{GS_SUBSCRIBE_PORT} (gs_forward.py)")
    print(f"IP laptop: {', '.join(ips) or '?'} — Radxa phải gửi tới IP này (GS_ARGS trong /etc/default/uav-gs-overlay)")
    print("Phím: F/F11/nháy đúp = toàn màn hình · H = HUD · B = box · C = chế độ mất gói · Esc/Q = thoát")
    if LOG.path:
        print(f"Log chẩn đoán: {LOG.path}")
    argv_text = " ".join(sys.argv[1:] if argv is None else argv) or "(không tham số)"
    LOG("app", f"KHỞI ĐỘNG {argv_text} | {versions} | UDP :{args.port}"
               + (f" + giám sát :{args.monitor_port}" if mon else "")
               + (f" + toạ độ :{track_port}" if track else "")
               + f" | IP laptop {', '.join(ips) or '?'} | giải mã {dec.desc}, {mode}")

    overlays = [TrackOverlay(track)] if track else []
    viewer = Viewer(args, stats, slot, rx, dec, queue, overlays=overlays, ips=ips, monitor=mon, track=track)
    rx.start()
    dec.start()
    if mon:
        mon.start()
    if track:
        track.start()
    if sub:
        sub.start()
    try:
        viewer.run()
    except KeyboardInterrupt:
        pass
    finally:
        rx.stop()
        dec.stop()
        if mon:
            mon.stop()
        if sub:
            sub.stop()
        rx.join(1.0)
        dec.join(1.0)
        for ov in viewer.overlays:
            ov.close()
        pygame.quit()
        total = stats.packets + stats.lost
        LOG("app", f"THOÁT sau {(clock() - viewer.t_start) / 60:.1f} phút: nhận {stats.packets} gói, mất {stats.lost}"
                   f" ({100.0 * stats.lost / total if total else 0.0:.2f}%), đứng hình {viewer.freezes} lần"
                   f" (tổng {viewer.freeze_total:.1f} s), reset luồng {stats.resets} lần")
        LOG.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

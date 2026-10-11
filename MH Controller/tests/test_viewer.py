#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kiểm tra uav_viewer.py không cần UAV, không mở cửa sổ (SDL dummy):

    python tests/test_viewer.py

1. Ghép RTP (NAL đơn / STAP-A / FU-A, seq quay vòng) → khung Annex-B đúng từng byte.
2. Giải mã: cờ LOW_DELAY, không frame-threading, đổi độ phân giải 1920x1080 → 1600x900.
3. Mất gói: so 3 chế độ idr / auto / conceal.
4. Chạy cả app qua UDP 127.0.0.1: video + JSON toạ độ giống gs_forward.py, mất 1 gói, mạng khựng,
   kiểm gói đăng ký :5599 (broadcast khi chưa có video, gửi thẳng khi đã có), NO SIGNAL.
Cần libx264 trong PyAV (bản wheel trên PyPI có sẵn) để tạo video thử.
"""
import json
import os
import random
import socket
import struct
import sys
import threading
import time
from fractions import Fraction

os.environ["SDL_VIDEODRIVER"] = "dummy"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "viewer"))

import av  # noqa: E402
import numpy as np  # noqa: E402
import pygame  # noqa: E402
import uav_viewer as uv  # noqa: E402

MTU = 1400
FAILS = []


def check(cond, msg):
    print(("  OK  " if cond else "  LỖI ") + msg)
    if not cond:
        FAILS.append(msg)


# ---------------------------------------------------------------------------------------------
# Tạo video thử + đóng gói RTP giống rtph264pay trên Pi
# ---------------------------------------------------------------------------------------------
def encode(w, h, n):
    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = w, h, "yuv420p"
    enc.time_base, enc.framerate = Fraction(1, 30), Fraction(30, 1)
    enc.gop_size, enc.max_b_frames, enc.bit_rate = 30, 0, 5_000_000
    enc.options = {"preset": "ultrafast", "tune": "zerolatency"}
    base = np.zeros((h, w, 3), np.uint8)
    base[..., 0] = (np.arange(w) * 255 // w)[None, :]
    base[..., 1] = (np.arange(h) * 255 // h)[:, None]
    out = []
    for i in range(n):
        rgb = base.copy()
        x0 = (i * 23) % (w - 200)
        rgb[:, x0:x0 + 200, 2] = 255
        fr = av.VideoFrame.from_ndarray(rgb, format="rgb24")
        fr.pts = i
        out += [bytes(p) for p in enc.encode(fr)]
    out += [bytes(p) for p in enc.encode(None)]
    return out


def split_nals(b):
    starts, i = [], 0
    while (j := b.find(b"\x00\x00\x01", i)) >= 0:
        starts.append(j + 3)
        i = j + 3
    nals = []
    for k, s in enumerate(starts):
        e = starts[k + 1] - 3 if k + 1 < len(starts) else len(b)
        nal = b[s:e]
        nals.append(nal.rstrip(b"\x00") if k + 1 < len(starts) else nal)
    return nals


def packetize(aus, ssrc=0x1234ABCD, seq=65000):
    """Mỗi khung → list gói RTP: NAL nhỏ loại 6–9 gộp STAP-A, NAL lớn → FU-A, còn lại NAL đơn."""
    res, ts = [], 1000

    def stap(group):
        return group[0] if len(group) == 1 else bytes([0x18]) + b"".join(struct.pack("!H", len(x)) + x for x in group)

    for a in aus:
        payloads, small = [], []
        for nal in split_nals(a):
            if (nal[0] & 0x1F) in (6, 7, 8, 9) and len(nal) < 300:
                small.append(nal)
                continue
            if small:
                payloads.append(stap(small))
                small = []
            if len(nal) <= MTU:
                payloads.append(nal)
                continue
            ind, body = (nal[0] & 0xE0) | 28, nal[1:]
            chunks = [body[i:i + MTU - 2] for i in range(0, len(body), MTU - 2)]
            for c, chunk in enumerate(chunks):
                hdr = (0x80 if c == 0 else 0) | (0x40 if c == len(chunks) - 1 else 0) | (nal[0] & 0x1F)
                payloads.append(bytes([ind, hdr]) + chunk)
        if small:
            payloads.append(stap(small))
        pk = []
        for k, p in enumerate(payloads):
            m = 0x80 if k == len(payloads) - 1 else 0
            pk.append(struct.pack("!BBHII", 0x80, m | 96, seq & 0xFFFF, ts, ssrc) + p)
            seq += 1
        res.append(pk)
        ts += 3000
    return res


# ---------------------------------------------------------------------------------------------
print("Tạo video thử…")
AUS = encode(1920, 1080, 90) + encode(1600, 900, 60)
PK = packetize(AUS)
print(f"  {len(AUS)} khung, {sum(map(len, PK))} gói RTP")

print("1. Ghép RTP")
got, st = [], uv.Stats()
dp = uv.H264Depacketizer(got.append, st)
for a in PK:
    for p in a:
        dp.push(p, time.perf_counter())
exp = [b"".join(b"\x00\x00\x00\x01" + n for n in split_nals(a)) for a in AUS]
check(len(got) == len(exp) and all(g.data == e for g, e in zip(got, exp)), f"{len(got)} khung ghép đúng từng byte")
check(st.lost == st.late == st.bad == 0, "không báo nhầm mất gói khi seq quay vòng 65535 → 0")


def run_decode(mode, drop=frozenset(), loss=0.0, seed=7):
    st = uv.Stats()
    slot, q = uv.LatestFrame(), uv.AUQueue(6, st)
    dec = uv.H264Decoder(q, slot, st, loss_mode=mode)
    dec.start()
    dp = uv.H264Depacketizer(q.put, st)
    rnd, sizes, gi = random.Random(seed), [], 0
    for a in PK:
        for p in a:
            gi += 1
            if gi in drop or rnd.random() < loss:
                continue
            dp.push(p, time.perf_counter())
        t0 = time.perf_counter()
        while len(q) and time.perf_counter() - t0 < 1:
            time.sleep(0.0005)
        time.sleep(0.003)
        item = slot.take()
        if item:
            sizes.append((item[0].width, item[0].height))
    time.sleep(0.2)
    dec.stop()
    dec.join(1)
    return st, sizes, dec


print("2. Giải mã")
st, sizes, dec = run_decode("auto")
check(bool(int(dec.ctx.flags) & 0x80000), "cờ LOW_DELAY đang bật")
check(str(dec.ctx.thread_type).endswith("SLICE") and dec.ctx.thread_count == 1, "không dùng frame-threading")
check(st.decoded == len(AUS) and st.dec_errors == 0, f"giải mã {st.decoded}/{len(AUS)} khung")
check(sizes[0] == (1920, 1080) and sizes[-1] == (1600, 900), "tự đổi độ phân giải 1920x1080 → 1600x900")

print("3. Mất gói (lẻ tẻ 3 gói x3 lần, giống wfb-ng 'video rx: 3 packets lost')")
sporadic = {s + d for s in range(300, 3600, 1300) for d in (0, 3, 5)}
res = {m: run_decode(m, drop=sporadic)[0] for m in ("idr", "auto", "conceal")}
for m, s in res.items():
    print(f"     {m:7}: giải mã {s.decoded}/{len(AUS)}, che lỗi {s.concealed}, bỏ chờ IDR {s.idr_dropped}")
check(res["auto"].decoded == len(AUS) and res["auto"].idr_dropped == 0, "auto: mất lẻ tẻ không đứng hình")
check(res["idr"].idr_dropped > 0, "idr: mất gói thì bỏ khung tới IDR")

# ---------------------------------------------------------------------------------------------
print("4. Chạy cả app qua UDP (giả lập gs_forward.py)")
VPORT, TPORT, SPORT = 5698, 5697, 5696
sub_pkts = []          # (thời điểm, ip đích) các gói đăng ký app gửi
log_path = os.path.join(HERE, "test_viewer.log")
if os.path.exists(log_path):
    os.remove(log_path)


def fake_radxa():
    """Nghe gói đăng ký như gs_forward.py, đồng thời phát video + toạ độ."""
    rs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rs.bind(("127.0.0.1", SPORT))
    rs.settimeout(0.05)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def drain():
        while True:
            try:
                d, a = rs.recvfrom(64)
                sub_pkts.append((time.perf_counter(), a[0]))
            except socket.timeout:
                return

    t_end = time.perf_counter() + 2.5          # 2.5 s đầu chưa phát video → app phải đi tìm
    while time.perf_counter() < t_end:
        drain()
    t_video = time.perf_counter()
    for i, a in enumerate(PK):
        target = t_video + i / 30 + (0.6 if i >= 40 else 0)       # khung 40: mạng khựng 600 ms
        while time.perf_counter() < target:
            drain()
        for k, p in enumerate(a):
            if i == 100 and k == 3:                               # khung 100: mất 1 gói
                continue
            tx.sendto(p, ("127.0.0.1", VPORT))
        x = 0.2 + 0.4 * i / len(PK)
        tgt = {"id": 11, "cls": "person", "score": 0.73, "box": [x, 0.3, x + 0.2, 0.8],
               "center": [x + 0.1, 0.55], "err": [x * 2 - 0.8, 0.1], "lost_frames": 2 if 70 <= i < 100 else 0}
        msg = {"type": "uav_track", "seq": i, "sensor_ts_ns": i * 33_333_333, "wall": time.time(),
               "frame": [1920, 1080], "detector": "ok", "target": tgt, "tracks": [tgt]}
        tx.sendto(json.dumps(msg).encode(), ("127.0.0.1", TPORT))
        if i == 50:
            pygame.image.save(pygame.display.get_surface().copy(), os.path.join(HERE, "test_viewer_box.png"))
    fake_radxa.video_end = time.perf_counter()
    end = time.perf_counter() + 2.0                                # hết video → NO SIGNAL
    while time.perf_counter() < end:
        drain()
    pygame.event.post(pygame.event.Event(pygame.QUIT))


# app gửi gói đăng ký tới cổng thật 5599; trong test đổi sang cổng giả để không đụng máy khác
uv.GS_SUBSCRIBE_PORT = SPORT
threading.Thread(target=fake_radxa, daemon=True).start()
rc = uv.main(["--port", str(VPORT), "--track-port", str(TPORT), "--gs", "127.0.0.1", "--size", "1280x720",
              "--log", log_path])
check(rc == 0, "app thoát bình thường")
log = open(log_path, encoding="utf-8").read()
check("BẮT ĐẦU NHẬN toạ độ" in log, "nhận được JSON toạ độ")
check("CHE LỖI khung #100" in log and "CHỜ IDR: khung #100" not in log, "mất 1 gói ở khung #100 được che lỗi, không chờ IDR")
check("ĐỨNG HÌNH" in log and "KHÔNG CÓ GÓI TỚI" in log, "log ghi đúng nguyên nhân khi mạng khựng")
check("NO SIGNAL" in log, "hết video → NO SIGNAL")
before = [t for t, _ in sub_pkts if t < fake_radxa.video_end - 3]
check(len(before) >= 2, f"app gửi gói đăng ký mỗi giây ({len(sub_pkts)} gói tới Radxa giả)")
check("tìm trạm mặt đất" in log and "đăng ký thẳng với 127.0.0.1" in log,
      "chưa có video → tìm (gs + broadcast); có video → đăng ký thẳng")

print()
print("KẾT QUẢ:", "TẤT CẢ ĐẠT" if not FAILS else f"{len(FAILS)} mục LỖI: {FAILS}")
print("Ảnh chụp app lúc chạy:", os.path.join(HERE, "test_viewer_box.png"))
sys.exit(1 if FAILS else 0)

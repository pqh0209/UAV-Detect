#!/usr/bin/env python3
"""Giả lập UAV để thử trạm mặt đất khi chưa có link thật.

Phát giống hệt đầu ra của wfb-ng phía trạm mặt đất:
  - RTP H.264 1920x1080@30 (pt=96) → udp 127.0.0.1:5600
    Hình: dải màu trượt ngang + 1 khối trắng ("người") chạy vòng tròn.
  - JSON "uav_track" 30 gói/s → udp 127.0.0.1:5700
    Box của mục tiêu bám đúng khối trắng (để nhìn là biết overlay khớp hay lệch);
    cứ 4 s lại "mất dấu" 0.5 s để thử khung dự đoán màu vàng.

Khung được ghi thẳng vào buffer DMA của mpph264enc (make_mpp_pool), vì videotestsrc →
mpph264enc trên máy này ra khung trống (RGA lỗi).
"""
import argparse
import json
import math
import os
import socket
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gs_overlay import Gst, GLib, make_mpp_pool, mapped, plane_views  # noqa: E402

W, H, FPS = 1920, 1080, 30
BW, BH = 0.12, 0.35            # kích thước "người" (chuẩn hoá)


def target_at(t):
    return 0.5 + 0.3 * math.cos(t * 0.6), 0.5 + 0.25 * math.sin(t * 0.6)


def send_tracks(host, ports, t0, stop):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq = 0
    while not stop.is_set():
        t = time.monotonic() - t0
        cx, cy = target_at(t)
        lost = int((t % 4.0 - 3.5) * FPS) + 1 if t % 4.0 > 3.5 else 0
        box = [round(cx - BW / 2, 4), round(cy - BH / 2, 4), round(cx + BW / 2, 4), round(cy + BH / 2, 4)]
        target = {"id": 11, "cls": "person", "score": 0.73, "box": box,
                  "center": [round(cx, 4), round(cy, 4)],
                  "err": [round(2 * cx - 1, 4), round(2 * cy - 1, 4)], "lost_frames": lost}
        other = {"id": 12, "cls": "car", "score": 0.61, "box": [0.06, 0.62, 0.30, 0.86], "lost_frames": 0}
        pkt = {"type": "uav_track", "seq": seq, "sensor_ts_ns": int(t * 1e9), "wall": time.time(),
               "frame": [W, H], "detector": "ok", "target": target,
               "tracks": [{k: target[k] for k in ("id", "cls", "score", "box", "lost_frames")}, other]}
        data = json.dumps(pkt).encode()
        for port in ports:
            sock.sendto(data, (host, port))
        seq += 1
        time.sleep(1 / FPS)


def make_bars():
    """Dải màu dọc rộng 2W (Y và UV) để trượt cửa sổ W qua từng khung."""
    colors = [(235, 128, 128), (210, 16, 146), (170, 166, 16), (145, 54, 34),
              (106, 202, 222), (81, 90, 240), (41, 240, 110), (16, 128, 128)]
    bar = (2 * W) // (2 * len(colors)) * 2
    y = np.zeros((H, 2 * W), np.uint8)
    uv = np.zeros((H // 2, 2 * W), np.uint8)
    for i in range(2 * W // bar):
        cy, cu, cv = colors[i % len(colors)]
        y[:, i * bar:(i + 1) * bar] = cy
        uv[:, i * bar:(i + 1) * bar:2] = cu
        uv[:, i * bar + 1:(i + 1) * bar:2] = cv
    y[:, :] = (y.astype(np.int16) * 3 // 4 + 16).astype(np.uint8)   # tối bớt cho khối trắng nổi
    return y, uv


def video_loop(host, port, t0, stop):
    caps = Gst.Caps.from_string(f"video/x-raw,format=NV12,width={W},height={H},framerate={FPS}/1")
    pipe = Gst.parse_launch(
        "appsrc name=src is-live=true format=time do-timestamp=true "
        "! mpph264enc bps=8000000 gop=30 header-mode=each-idr ! h264parse config-interval=-1 "
        f"! rtph264pay pt=96 mtu=1400 config-interval=1 ! udpsink host={host} port={port} sync=false")
    src = pipe.get_by_name("src")
    src.set_property("caps", caps)
    pool = make_mpp_pool(caps)
    pipe.set_state(Gst.State.PLAYING)
    bars_y, bars_uv = make_bars()
    n = 0
    while not stop.is_set():
        t = time.monotonic() - t0
        off = (n * 8) % W & ~1
        ok, buf = pool.acquire_buffer(None)
        with mapped(buf, Gst.MapFlags.READ | Gst.MapFlags.WRITE) as arr:
            y, uv = plane_views(arr, buf, W, H)
            y[:] = bars_y[:, off:off + W]
            uv[:] = bars_uv[:, off:off + W]
            cx, cy = target_at(t)
            x1, x2 = int((cx - BW / 2) * W) & ~1, int((cx + BW / 2) * W) & ~1
            y1, y2 = int((cy - BH / 2) * H) & ~1, int((cy + BH / 2) * H) & ~1
            y[y1:y2, x1:x2] = 235
            uv[y1 // 2:y2 // 2, x1:x2] = 128
        src.emit("push-buffer", buf)
        n += 1
        time.sleep(max(0.0, t0 + n / FPS - time.monotonic()))
    pipe.set_state(Gst.State.NULL)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--video-port", type=int, default=5600)
    p.add_argument("--telem-port", type=int, default=5700)
    p.add_argument("--telem-copy", type=int, action="append", default=[], metavar="PORT",
                   help="gửi thêm bản sao toạ độ tới cổng này (cho công cụ đo độ trễ)")
    p.add_argument("--no-video", action="store_true", help="chỉ gửi toạ độ")
    p.add_argument("--no-telem", action="store_true", help="chỉ gửi video")
    args = p.parse_args()

    Gst.init(None)
    stop = threading.Event()
    t0 = time.monotonic()
    if not args.no_telem:
        threading.Thread(target=send_tracks, args=(args.host, [args.telem_port] + args.telem_copy, t0, stop), daemon=True).start()
    if not args.no_video:
        threading.Thread(target=video_loop, args=(args.host, args.video_port, t0, stop), daemon=True).start()
    print("Đang phát giả lập UAV, Ctrl+C để dừng", flush=True)
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()

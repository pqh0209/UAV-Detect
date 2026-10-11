#!/usr/bin/env python3
"""Đo độ trễ video toàn tuyến khi chạy với test_sender.py (không cần UAV thật).

test_sender.py vẽ 1 khối trắng chạy vòng tròn theo thời gian t của nó và gửi kèm t trong gói
toạ độ. Công cụ này xem video (RTSP của gs_overlay, hoặc thẳng cổng RTP 5600), tìm vị trí
khối trắng trong từng khung → suy ra khung đó được tạo lúc nào → so với "bây giờ" theo đồng hồ
của test_sender (lấy từ gói toạ độ) → độ trễ.

Cách chạy (3 terminal, hoặc nền):
  python3 test_sender.py --telem-copy 5701
  python3 gs_overlay.py
  python3 test_latency.py                          # đo qua RTSP rtsp://127.0.0.1:8554/uav
  python3 test_latency.py --source rtp             # đo thẳng luồng 5600 (không qua gs_overlay;
                                                   #   khi đó tắt gs_overlay vì cùng cổng)
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
from gs_overlay import Gst, GLib, mapped, plane_views  # noqa: E402

PERIOD = 2 * math.pi / 0.6        # chu kỳ vòng tròn trong test_sender.target_at


def block_time(y):
    """Thời điểm (theo t của test_sender, modulo PERIOD) mà khối trắng ở vị trí này."""
    # Chép nguyên hàng (memcpy) rồi mới lấy mẫu: y nằm trong bộ nhớ không cache của bộ giải mã,
    # đọc lẻ từng byte kiểu y[::4, ::4] mất ~30 ms/khung → client không kịp 30 fps → TCP dồn
    # khung → đo ra độ trễ giả (tăng dần).
    rows = np.array(y[::4])
    m = rows[:, ::4] >= 230
    # bỏ nét chữ trắng của HUD/nhãn (mảnh) — chỉ giữ vùng đặc như khối trắng
    m = m & np.roll(m, 2, 0) & np.roll(m, -2, 0) & np.roll(m, 2, 1) & np.roll(m, -2, 1)
    ys, xs = np.nonzero(m)
    if len(xs) < 50:
        return None
    h, w = y.shape
    cx, cy = xs.mean() * 4 / w, ys.mean() * 4 / h
    # target_at: tâm vòng tròn (0.5, 0.5), bán kính 0.3 (ngang) / 0.25 (dọc), tốc độ góc 0.6 rad/s
    ang = math.atan2((cy - 0.5) / 0.25, (cx - 0.5) / 0.3)
    return (ang / 0.6) % PERIOD


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=["rtsp", "rtp"], default="rtsp")
    p.add_argument("--url", default="rtsp://127.0.0.1:8554/uav")
    p.add_argument("--telem-port", type=int, default=5701)
    p.add_argument("--seconds", type=float, default=10)
    args = p.parse_args()

    Gst.init(None)
    clock = {"off": None}          # wall - t của test_sender

    def rx():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", args.telem_port))
        while True:
            pkt = json.loads(s.recv(65535))
            clock["off"] = pkt["wall"] - pkt["sensor_ts_ns"] / 1e9

    threading.Thread(target=rx, daemon=True).start()

    if args.source == "rtsp":
        head = f"rtspsrc location={args.url} latency=0 protocols=tcp ! rtph264depay"
    else:
        head = ("udpsrc address=127.0.0.1 port=5600 caps=\"application/x-rtp,media=video,"
                "clock-rate=90000,encoding-name=H264,payload=96\" ! rtph264depay")
    pipe = Gst.parse_launch(head + " ! h264parse ! mppvideodec ! video/x-raw,format=NV12 "
                            "! appsink name=s emit-signals=true max-buffers=1 drop=true sync=false")
    lags, frames = [], [0]

    def on_sample(sink):
        smp = sink.emit("pull-sample")
        if clock["off"] is None:
            return Gst.FlowReturn.OK
        now = time.time() - clock["off"]
        frames[0] += 1
        caps = smp.get_caps().get_structure(0)
        w, h = caps.get_value("width"), caps.get_value("height")
        buf = smp.get_buffer()
        with mapped(buf, Gst.MapFlags.READ) as arr:
            y, _ = plane_views(arr, buf, w, h)
            tb = block_time(y)
        if tb is not None:
            lag = (now - tb) % PERIOD
            lags.append(lag - PERIOD if lag > PERIOD / 2 else lag)
        return Gst.FlowReturn.OK

    pipe.get_by_name("s").connect("new-sample", on_sample)
    pipe.set_state(Gst.State.PLAYING)
    loop = GLib.MainLoop()

    def done():
        pipe.set_state(Gst.State.NULL)
        v = np.array(lags[15:]) * 1000            # bỏ vài khung đầu lúc mới kết nối
        if len(v):
            p10, p50, p90 = np.percentile(v, [10, 50, 90])
            print(f"{args.source}: {frames[0] / args.seconds:.1f} fps nhận được, độ trễ "
                  f"trung vị {p50:.0f} ms (10%: {p10:.0f} ms, 90%: {p90:.0f} ms, n={len(v)})", flush=True)
        else:
            print("Không đo được (không thấy khối trắng hoặc không có gói toạ độ ở cổng "
                  f"{args.telem_port} — chạy test_sender.py --telem-copy {args.telem_port})", flush=True)
        loop.quit()

    GLib.timeout_add(int(args.seconds * 1000), done)
    loop.run()


if __name__ == "__main__":
    main()

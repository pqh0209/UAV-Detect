#!/usr/bin/env python3
"""Đo độ trễ phía Pi: lúc cảm biến chụp (SensorTimestamp) -> encoder H.264 xả frame ra.

Chạy uav_tracker y như bình thường (mọi tham số truyền thẳng qua), nhưng vá encoder/output
để ghi độ trễ từng frame, sau PROBE_SECS giây in thống kê rồi tự thoát.
Camera chỉ mở được bởi 1 tiến trình -> dừng service trước:

    sudo systemctl stop uav-tracker
    PROBE_SECS=25 .venv/bin/python tools/latency_probe.py [--sensor-mode 1536x864 ...]   # chạy từ gốc repo
    sudo systemctl start uav-tracker

Đã đo (2026-10-11): mode 1536x864 ~40 ms, mode 2304x1296 ~53 ms (trung vị).
"""
import collections
import os
import signal
import statistics
import sys
import threading
import time

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tracker")
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)
import uav_tracker as ut  # noqa: E402

sensor_ts = collections.deque(maxlen=64)
latencies = []
orig_encode = ut.H264Encoder.encode
orig_output = ut.CountingOutput.outputframe


def encode(self, stream, request):
    sensor_ts.append(request.get_metadata().get("SensorTimestamp"))
    return orig_encode(self, stream, request)


def outputframe(self, *args, **kwargs):
    if sensor_ts:   # encoder xả frame theo đúng thứ tự nhận vào
        latencies.append((time.clock_gettime_ns(time.CLOCK_MONOTONIC) - sensor_ts.popleft()) / 1e6)
    return orig_output(self, *args, **kwargs)


def report():
    time.sleep(float(os.environ.get("PROBE_SECS", "20")))
    xs = sorted(latencies[60:])   # bỏ ~2 s đầu
    if xs:
        q = lambda p: xs[int(p * (len(xs) - 1))]
        print(f"[probe] {len(xs)} frame | cảm biến -> encoder: trung vị {statistics.median(xs):.1f} ms, "
              f"p10 {q(.1):.1f}, p90 {q(.9):.1f}, max {xs[-1]:.1f}", flush=True)
    os.kill(os.getpid(), signal.SIGTERM)


ut.H264Encoder.encode = encode
ut.CountingOutput.outputframe = outputframe
threading.Thread(target=report, daemon=True).start()
sys.argv = ["uav_tracker.py"] + sys.argv[1:]
ut.main()

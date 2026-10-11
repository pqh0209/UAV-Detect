#!/usr/bin/env python3
"""
uav_tracker.py — chương trình chạy trên UAV (Raspberry Pi 4 + Coral USB + camera IMX708).

Một tiến trình duy nhất giữ camera (camera chỉ mở được bởi 1 tiến trình) và lấy
2 luồng ảnh cùng lúc từ ISP của Pi:

  main  (1920x1080 YUV420) -> encoder H.264 phần cứng -> gst-launch đóng gói RTP
        -> UDP 127.0.0.1:5602 -> wfb-ng (profile drone) -> sóng xuống mặt đất.
        Video gốc, KHÔNG vẽ box, không đi qua Python nên detect không làm chậm video.

  lores (300x300 YUV420, đúng cỡ đầu vào model) -> Coral Edge TPU (TFLite)
        -> lọc 4 lớp -> SORT tracking -> chọn mục tiêu -> JSON/UDP tới trạm mặt
        đất qua tunnel wfb-ng (drone 10.5.0.2 -> mặt đất 10.5.0.1:5700).

Hai luồng có cùng vùng cắt cảm biến nên toạ độ chuẩn hoá [0..1] áp thẳng lên video.

Tự phục hồi:
  - Coral/TFLite lỗi: video VẪN chạy, detect chuyển "retrying"/"degraded", tự thử
    khởi tạo lại với thời gian chờ tăng dần (5 s -> 60 s) để một Coral hỏng/thiếu
    nguồn không liên tục kéo sụt áp cả hệ thống.
  - Camera/encoder/gst chết hoặc luồng detect đứng: thoát mã 1 để systemd
    (Restart=always) khởi động lại. `kill -USR1 <pid>` in stack mọi luồng ra log.
"""

import argparse
import faulthandler
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import warnings

# numpy trên ARM in cảnh báo "smallest subnormal ... is zero" khi scipy/filterpy import — vô hại
warnings.filterwarnings("ignore", message="The value of the smallest subnormal")

import cv2
import numpy as np
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FileOutput
from tflite_runtime.interpreter import Interpreter, load_delegate

from sort_tracker import Sort

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # tracker/ nằm trong repo
ALLOWED_LABELS = {"person", "car", "motorcycle", "bicycle"}
LORES_SIZE = (300, 300)       # = đầu vào SSD MobileNet V2, khỏi phải resize


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def parse_args():
    p = argparse.ArgumentParser(description="UAV: video -> wfb-ng + Coral detect + SORT tracking")
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--sensor-mode", default="2304x1296",
                   help="mode cảm biến IMX708: 2304x1296 (toàn bộ góc nhìn, ~53 ms cảm biến->encoder) "
                        "hoặc 1536x864 (~67%% vùng giữa, ~40 ms)")
    # MCS1/20MHz + FEC 8/12: trần sóng ~998 gói/s ≈ 7.5 Mbps video.
    # 8 Mbps làm nghẽn (drop 7-10%, trễ 1-2 s); 5 Mbps dùng ~71% sóng, 0 drop.
    p.add_argument("--bitrate", type=int, default=5_000_000)
    p.add_argument("--video-port", type=int, default=5602, help="cổng UDP video của wfb-ng drone")
    p.add_argument("--model", default=os.path.join(REPO, "models", "edgetpu.tflite"))
    p.add_argument("--labels", default=os.path.join(REPO, "models", "labelmap.txt"))
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--max-age", type=int, default=30, help="frame giữ track khi mất dấu (30 = 1 s)")
    p.add_argument("--min-hits", type=int, default=3)
    p.add_argument("--telemetry", action="append", default=None,
                   help="đích gửi toạ độ host:port, dùng nhiều lần được (mặc định 10.5.0.1:5700)")
    p.add_argument("--stats-interval", type=float, default=5.0)
    return p.parse_args()


# ---------------------------------------------------------------- Video ----

class CountingOutput(FileOutput):
    """FileOutput ghi H.264 vào stdin của gst-launch, đồng thời đếm frame đã encode."""

    def __init__(self, fileobj):
        super().__init__(fileobj)
        self.frames = 0
        self.last_frame_time = time.monotonic()

    def outputframe(self, *args, **kwargs):
        self.frames += 1
        self.last_frame_time = time.monotonic()
        super().outputframe(*args, **kwargs)


def start_rtp_sender(port):
    cmd = ["gst-launch-1.0", "-q", "fdsrc", "fd=0", "!", "h264parse", "!",
           "rtph264pay", "config-interval=1", "pt=96", "mtu=1400", "!",
           "udpsink", "host=127.0.0.1", f"port={port}", "sync=false"]
    # bufsize mặc định -> BufferedWriter (picamera2 bắt buộc); FileOutput flush() sau mỗi frame
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


# ------------------------------------------------------------- Detector ----

class Detector:
    """Bọc TFLite Interpreter + Edge TPU, tự khởi tạo lại khi lỗi."""

    RETRY_MIN, RETRY_MAX = 5.0, 60.0

    def __init__(self, model_path, labels_path, threshold):
        self.model_path = model_path
        self.threshold = threshold
        with open(labels_path) as f:
            labels = [l.strip() for l in f]
        if labels and labels[0] == "???":       # labelmap COCO có dòng đầu "???"
            labels = labels[1:]
        self.labels = labels
        self.allowed_ids = {i for i, n in enumerate(labels) if n in ALLOWED_LABELS}
        self.interpreter = None
        self.failures = 0
        self.next_attempt = 0.0
        self.status = "init"

    def _create(self):
        # Đây là TensorFlow Lite Interpreter (tflite_runtime) — runtime suy luận
        # của TensorFlow Lite — được gắn libedgetpu delegate để giao phần tính
        # toán của model cho chip Edge TPU (Coral USB) thay vì chạy trên CPU.
        itp = Interpreter(model_path=self.model_path,
                          experimental_delegates=[load_delegate("libedgetpu.so.1.0")])
        itp.allocate_tensors()
        inp = itp.get_input_details()[0]
        outs = itp.get_output_details()
        # Thứ tự output: model TF1 (đang dùng) boxes/classes/scores; model TF2 khác thứ tự
        idx = (1, 3, 0) if "StatefulPartitionedCall" in outs[0]["name"] else (0, 1, 2)
        self.input_index = inp["index"]
        self.input_size = (int(inp["shape"][2]), int(inp["shape"][1]))   # (w, h)
        self.out_boxes, self.out_classes, self.out_scores = (outs[i]["index"] for i in idx)
        self.interpreter = itp

    def ready(self):
        if self.interpreter is not None:
            return True
        now = time.monotonic()
        if now < self.next_attempt:
            return False
        try:
            self._create()
        except Exception as e:
            self._fail(f"không khởi tạo được interpreter: {e!r}")
            return False
        log(f"[detect] Edge TPU sẵn sàng (sau {self.failures} lần lỗi)")
        self.failures = 0
        self.status = "ok"
        return True

    def _fail(self, why):
        self.interpreter = None
        self.failures += 1
        self.status = "degraded" if self.failures > 3 else "retrying"
        # 5 s cho 3 lần đầu, sau đó gấp đôi tới tối đa 60 s
        wait = min(self.RETRY_MAX, self.RETRY_MIN * 2 ** max(0, self.failures - 3))
        self.next_attempt = time.monotonic() + wait
        # Lỗi lặp lại (Coral rút/hỏng) chỉ log thưa để khỏi tràn log
        if self.failures <= 5 or self.failures % 20 == 0:
            log(f"[detect] LỖI ({self.failures}): {why} -> video vẫn chạy, thử lại sau {wait:.0f}s")

    def detect(self, rgb):
        """rgb: uint8 (h, w, 3) đúng cỡ model. Trả về [(box[x1,y1,x2,y2] chuẩn hoá, score, cls)],
        hoặc None nếu Edge TPU lỗi."""
        try:
            self.interpreter.set_tensor(self.input_index, rgb[np.newaxis])
            self.interpreter.invoke()
            boxes = self.interpreter.get_tensor(self.out_boxes)[0]
            classes = self.interpreter.get_tensor(self.out_classes)[0]
            scores = self.interpreter.get_tensor(self.out_scores)[0]
        except Exception as e:
            self._fail(f"invoke lỗi: {e!r}")
            return None
        dets = []
        for b, c, s in zip(boxes, classes, scores):
            c = int(c)
            if s < self.threshold or s > 1.0 or c not in self.allowed_ids:
                continue
            y1, x1, y2, x2 = (float(v) for v in np.clip(b, 0.0, 1.0))
            if x2 > x1 and y2 > y1:
                dets.append(([x1, y1, x2, y2], float(s), c))
        return dets


def lores_to_rgb(arr, w, h, out_size):
    """Mảng YUV420 của luồng lores (stride có thể > width) -> RGB đúng cỡ model."""
    rgb = cv2.cvtColor(arr, cv2.COLOR_YUV420p2RGB)[:h, :w]
    if (w, h) != out_size:
        rgb = cv2.resize(rgb, out_size)
    return np.ascontiguousarray(rgb)


# ------------------------------------------------------- Tracking / Telemetry ----

class TargetSelector:
    """Tự chọn mục tiêu: track xác nhận xuất hiện sớm nhất (id nhỏ nhất).
    Track bị xoá (mất dấu > max_age frame) -> chọn track xác nhận tiếp theo."""

    def __init__(self, labels):
        self.labels = labels
        self.target_id = None

    def update(self, sort):
        alive = {t.id: t for t in sort.tracks}
        if self.target_id is not None and self.target_id not in alive:
            log(f"[track] mất dấu mục tiêu #{self.target_id} -> chờ chọn mục tiêu mới")
            self.target_id = None
        if self.target_id is None:
            ids = [t.id for t in sort.tracks if t.hits >= sort.min_hits and t.time_since_update == 0]
            if ids:
                self.target_id = min(ids)
                log(f"[track] chọn mục tiêu #{self.target_id} "
                    f"({self.labels[alive[self.target_id].cls]})")
        if self.target_id is None:
            return None
        t = alive[self.target_id]
        x1, y1, x2, y2 = (float(v) for v in t.box)
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        return {
            "id": t.id, "cls": self.labels[t.cls], "score": round(t.score, 3),
            "box": [round(v, 4) for v in (x1, y1, x2, y2)],      # chuẩn hoá 0..1
            "center": [round(cx, 4), round(cy, 4)],
            "err": [round(cx * 2 - 1, 4), round(cy * 2 - 1, 4)],  # lệch tâm khung -1..1, phải/dưới là +
            "lost_frames": t.time_since_update,                    # >0: vị trí dự đoán (Kalman)
        }


class Telemetry:
    """Gửi 1 gói JSON/UDP mỗi frame detect. Socket non-blocking: không bao giờ làm đứng detect."""

    def __init__(self, dests, frame_size, labels):
        self.dests = dests
        self.frame = list(frame_size)
        self.labels = labels
        self.seq = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)

    def send(self, sensor_ts_ns, detector_status, target, tracks):
        self.seq += 1
        msg = {
            "type": "uav_track", "seq": self.seq,
            "sensor_ts_ns": sensor_ts_ns, "wall": round(time.time(), 3),
            "frame": self.frame, "detector": detector_status,
            "target": target,
            "tracks": [{"id": t.id, "cls": self.labels[t.cls], "score": round(t.score, 3),
                        "box": [round(float(v), 4) for v in t.box],
                        "lost_frames": t.time_since_update} for t in tracks[:20]],
        }
        data = json.dumps(msg, separators=(",", ":")).encode()
        for d in self.dests:
            try:
                self.sock.sendto(data, d)
            except OSError:
                pass   # mặt đất chưa kết nối / tunnel chưa lên: bỏ qua


# ------------------------------------------------------------------ Main ----

def cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return f"{int(f.read()) / 1000:.1f}°C"
    except (OSError, ValueError):
        return "?"


def main():
    args = parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    faulthandler.register(signal.SIGUSR1, all_threads=True)   # chẩn đoán treo

    dests = [(h, int(p)) for h, p in (d.rsplit(":", 1) for d in (args.telemetry or ["10.5.0.1:5700"]))]
    detector = Detector(args.model, args.labels, args.threshold)
    new_sort = lambda: Sort(max_age=args.max_age, min_hits=args.min_hits)
    sort = new_sort()
    selector = TargetSelector(detector.labels)
    telemetry = Telemetry(dests, (args.width, args.height), detector.labels)

    sw, sh = (int(v) for v in args.sensor_mode.split("x"))
    frame_us = int(1_000_000 / args.fps)
    picam2 = Picamera2()
    picam2.configure(picam2.create_video_configuration(
        main={"size": (args.width, args.height), "format": "YUV420"},
        lores={"size": LORES_SIZE, "format": "YUV420"},
        sensor={"output_size": (sw, sh), "bit_depth": 10},
        controls={"FrameDurationLimits": (frame_us, frame_us)},
        buffer_count=6))
    lw, lh = picam2.camera_configuration()["lores"]["size"]

    gst = start_rtp_sender(args.video_port)
    output = CountingOutput(gst.stdin)
    picam2.start_recording(H264Encoder(bitrate=args.bitrate, repeat=True, iperiod=int(args.fps)), output)
    log(f"[video] {args.width}x{args.height}@{args.fps:g} H.264 {args.bitrate / 1e6:g}Mbps "
        f"(cảm biến {args.sensor_mode}) -> RTP udp://127.0.0.1:{args.video_port} -> wfb-ng")
    log(f"[telemetry] gửi toạ độ tới {', '.join(f'{h}:{p}' for h, p in dests)}")

    stats = {"det_frames": 0, "det_ms": 0.0, "loop_frames": 0, "frame_errors": 0}

    def detect_loop():
        nonlocal sort
        while not stop.is_set():
            try:
                req = picam2.capture_request()
            except Exception as e:
                log(f"[camera] capture lỗi: {e!r}")
                stop.set()
                return
            try:
                arr = req.make_array("lores")
                ts_ns = req.get_metadata().get("SensorTimestamp")
            finally:
                req.release()
            stats["loop_frames"] += 1

            # Một khung lỗi không được làm chết luồng detect: ghi log, bỏ khung đó.
            try:
                dets = None
                if detector.ready():
                    t0 = time.perf_counter()
                    dets = detector.detect(lores_to_rgb(arr, lw, lh, detector.input_size))
                    if dets is not None:
                        stats["det_ms"] += (time.perf_counter() - t0) * 1000
                        stats["det_frames"] += 1
                # Coral lỗi -> cập nhật rỗng: track già đi và hết hạn sau max_age frame,
                # thay vì gửi mãi một mục tiêu "đóng băng".
                tracks = sort.update(dets or [])
                telemetry.send(ts_ns, detector.status, selector.update(sort), tracks)
            except Exception:
                stats["frame_errors"] += 1
                log(f"[detect] lỗi xử lý khung (lần {stats['frame_errors']}):\n{traceback.format_exc()}")
                if stats["frame_errors"] % 10 == 0:
                    log("[track] lỗi lặp lại -> khởi tạo lại SORT, bỏ mục tiêu hiện tại")
                    sort = new_sort()
                    selector.target_id = None

    th = threading.Thread(target=detect_loop, name="detect", daemon=True)
    th.start()

    # Vòng giám sát: thống kê + phát hiện video/detect chết để systemd khởi động lại
    exit_code = 0
    last_t, last_v, last_d = time.monotonic(), 0, 0
    last_loop, last_loop_t = 0, time.monotonic()
    while not stop.wait(args.stats_interval):
        now = time.monotonic()
        dt = now - last_t
        n_det = stats["det_frames"] - last_d
        dms, stats["det_ms"] = (stats["det_ms"] / n_det if n_det else 0.0), 0.0
        tgt = selector.target_id
        log(f"[stats] video {(output.frames - last_v) / dt:5.1f}fps | detect {n_det / dt:5.1f}fps "
            f"({dms:4.1f}ms) [{detector.status}] "
            f"| tracks {sum(t.time_since_update == 0 for t in sort.tracks)} "
            f"| target {tgt if tgt is not None else '-'} | CPU {cpu_temp()}")
        last_t, last_v, last_d = now, output.frames, stats["det_frames"]

        reason = None
        if gst.poll() is not None:
            reason = f"[video] gst-launch đã thoát (mã {gst.returncode})"
        elif now - output.last_frame_time > 5.0:
            reason = "[video] encoder không ra frame >5s (camera mất tín hiệu? kiểm tra cáp CSI/nguồn)"
        elif not th.is_alive():
            reason = "[camera] luồng detect/camera đã dừng"
        elif stats["loop_frames"] != last_loop:
            last_loop, last_loop_t = stats["loop_frames"], now
        elif now - last_loop_t > 10.0:
            reason = "[detect] luồng detect đứng >10s"
            faulthandler.dump_traceback(all_threads=True)
        if reason:
            log(f"{reason} -> thoát để systemd khởi động lại")
            exit_code = 1
            break

    stop.set()

    # Dọn dẹp có giới hạn thời gian: camera chết thì stop_recording() có thể kẹt -> buộc thoát
    def cleanup():
        try:
            picam2.stop_recording()
        except Exception:
            pass
        try:
            gst.stdin.close()
            gst.terminate()
            gst.wait(timeout=3)
        except Exception:
            gst.kill()

    ct = threading.Thread(target=cleanup, daemon=True)
    ct.start()
    ct.join(timeout=5.0)
    if ct.is_alive():
        try:
            gst.kill()
        except Exception:
            pass
        log(f"Kết thúc (mã {exit_code or 1}, dọn dẹp kẹt >5s -> buộc thoát)")
        os._exit(exit_code or 1)
    log(f"Kết thúc (mã {exit_code})")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

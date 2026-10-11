# UAV-Detect

Camera gắn trên UAV tự nhận diện và bám mục tiêu (người / ô tô / xe máy / xe đạp). Video và toạ độ mục tiêu
được gửi xuống trạm mặt đất qua **wfb-ng** (WiFi broadcast 5.8 GHz). Từ trạm mặt đất, video được xem trên laptop
với độ trễ thấp.

```
UAV (Raspberry Pi 4B)                     GS – trạm mặt đất (Radxa Zero 3W)            APP – laptop Windows
camera IMX708 → H.264 1080p30 ──wfb-ng──▶ chuyển tiếp RTP gốc ─────── UDP 5601 ──────▶ uav_viewer: giải mã,
Coral → SORT → JSON toạ độ ───tunnel────▶ chuyển tiếp JSON ────────── UDP 5602 ──────▶ vẽ box trên laptop
                                          (dự phòng) vẽ box + nén lại ─ UDP 5600 / RTSP
```

| Thư mục | Chạy trên | Nội dung |
|---|---|---|
| [`UAV/`](UAV/) | Raspberry Pi 4B trên UAV | Bám mục tiêu (`uav_tracker.py`, `sort_tracker.py`), cấu hình wfb-ng phía drone |
| [`GS/`](GS/) | Radxa Zero 3W (trạm mặt đất) | `gs_overlay.py` (nhận video + toạ độ, chuyển tiếp tới laptop, vẽ box dự phòng), watchdog wfb-ng, service systemd, cấu hình |
| [`APP/`](APP/) | Laptop Windows | `uav_viewer.py`: xem video độ trễ thấp, vẽ box trên laptop, log chẩn đoán |

Mạng hiện tại (cắm dây thẳng):

| Máy | IP |
|---|---|
| Radxa (USB-Ethernet) | 192.168.50.1 |
| Laptop | 192.168.50.2 |
| Tunnel wfb-ng | UAV 10.5.0.2 ↔ GS 10.5.0.1 |

Khoá wfb-ng (`drone.key`, `gs.key`) **không bao giờ đưa lên repo**; `.gitignore` đã chặn `*.key`.

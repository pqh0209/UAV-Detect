# UAV-Detect

Camera gắn trên UAV tự nhận diện và bám mục tiêu (người / ô tô / xe máy / xe đạp). Video và toạ độ mục tiêu
được gửi xuống trạm mặt đất qua **wfb-ng** (WiFi broadcast 5.8 GHz). Từ trạm mặt đất, video được xem trên laptop
với độ trễ thấp.

```
Pi Air Unit (Raspberry Pi 4B)             Radxa VRX (Radxa Zero 3W)                  MH Controller (laptop Windows)
camera IMX708 → H.264 1080p30 ──wfb-ng──▶ gs_forward: chuyển tiếp RTP ── UDP 5600 ──▶ uav_viewer: giải mã,
Coral → SORT → JSON toạ độ ───tunnel────▶ gs_forward: chuyển tiếp JSON ─ UDP 5700 ──▶ vẽ box trên laptop
                                          ◀── laptop tự đăng ký nhận: UDP 5599 mỗi giây ──
```

| Thư mục | Chạy trên | Nội dung |
|---|---|---|
| [`Pi Air Unit/`](Pi%20Air%20Unit/) | Raspberry Pi 4B trên UAV | Bám mục tiêu (`uav_tracker.py`, `sort_tracker.py`), cấu hình wfb-ng phía drone |
| [`Radxa VRX/`](Radxa%20VRX/) | Radxa Zero 3W (trạm mặt đất) | `gs/gs_forward.py` (chuyển tiếp nguyên vẹn video + toạ độ tới laptop), `gs/wfb_watchdog.py`, service systemd và cấu hình trong `config/` |
| [`MH Controller/`](MH%20Controller/) | Laptop Windows | `uav_viewer.py`: xem video độ trễ thấp, vẽ box trên laptop, log chẩn đoán |

Mạng hiện tại (cắm dây thẳng):

| Máy | IP |
|---|---|
| Radxa (USB-Ethernet) | 192.168.50.1 |
| Laptop | 192.168.50.2 |
| Tunnel wfb-ng | UAV 10.5.0.2 ↔ GS 10.5.0.1 |

Khoá wfb-ng (`drone.key`, `gs.key`) **không bao giờ đưa lên repo**; `.gitignore` đã chặn `*.key`.

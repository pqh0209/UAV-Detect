# UAV — Raspberry Pi 4B

> Thư mục này chưa có code. Code đang nằm trên Pi, cần chép vào đây (xem mục "Cần chép vào").

Service `uav-tracker` chạy một tiến trình giữ camera và lấy 2 luồng từ ISP cùng lúc:

```
Cảm biến IMX708 (mode 1536x864 @30)
  ├─ luồng main 1920x1080 YUV420 → encoder H.264 phần cứng Pi (8 Mbps, IDR mỗi 30 khung, SPS/PPS lặp lại)
  │     → gst rtph264pay (pt=96, mtu=1400) → UDP 127.0.0.1:5602 → wfb-ng → trạm mặt đất
  └─ luồng lores 300x300 → Coral USB (SSD MobileNet V2 COCO, Edge TPU) → lọc 4 lớp
        → SORT tracking → JSON "uav_track" ~30 gói/s → 10.5.0.1:5700 (qua tunnel wfb-ng)
```

- Video gửi xuống là video gốc, không vẽ box. Box được vẽ ở trạm mặt đất hoặc laptop.
- `"detector": "degraded"` trong JSON nghĩa là Coral lỗi. Khi đó video vẫn chạy nhưng không có mục tiêu.
  Kiểm tra bằng `sudo journalctl -u uav-tracker -n 80` và `lsusb` (Coral: `1a6e:089a` / `18d1:9302`).
  Coral phải cắm cổng USB 3.0.

## Cần chép vào

| Trên Pi | Vào repo |
|---|---|
| `~/uav-project/uav-wfb-ng/app/uav_tracker.py`, `sort_tracker.py` (và file khác trong `app/`) | `Pi Air Unit/app/` |
| File service `uav-tracker.service` | `Pi Air Unit/` |
| `/etc/wifibroadcast.cfg` (profile drone) | `Pi Air Unit/config/` |

**Không** chép `/etc/drone.key`.

Giao diện với trạm mặt đất (kênh wfb-ng, tunnel, định dạng JSON): xem [`Radxa VRX/README.md`](../Radxa%20VRX/README.md).
Tài liệu bàn giao cũ có đủ chi tiết phần cứng Pi và các lỗi đã gặp nằm trong lịch sử git
(commit `67383c2`, file `GS/CLAUDE.md`, mục 2–7).

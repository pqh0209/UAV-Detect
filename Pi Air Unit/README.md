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
| `~/uav-project/uav-wfb-ng/app/uav_tracker.py`, `sort_tracker.py` (và file khác trong `app/`) | `UAV/app/` |
| File service `uav-tracker.service` | `UAV/` |
| `/etc/wifibroadcast.cfg` (profile drone) | `UAV/config/` |

**Không** chép `/etc/drone.key`.

Chi tiết phần cứng, cấu hình wfb-ng và các lỗi đã gặp: xem [`GS/CLAUDE.md`](../GS/CLAUDE.md), mục 2–7.

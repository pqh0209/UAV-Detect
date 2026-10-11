# CLAUDE.md — hướng dẫn cho Claude Code / AI agent

Đọc `README.md` để biết kiến trúc, giao diện UAV ↔ mặt đất, số đo và xử lý sự cố.
Thư mục này chứa **phía UAV**. Trong repo chung còn có `Radxa VRX/` (trạm mặt đất, chỉ chuyển tiếp)
và `MH Controller/` (app laptop: giải mã + vẽ box).

## Bối cảnh

- UAV tracking camera:
  - Pi 4B + IMX708 + Coral USB: detect và track người/ô tô/xe máy/xe đạp.
  - Video 1080p30 và toạ độ JSON được gửi qua wfb-ng (RTL8812AU, kênh 165).
- Repo nằm ở `~/uav-project` trên Pi 4B (UAV):
  - `tracker/`: chương trình chạy.
  - `deploy/`: `install.sh` và `deploy/etc/` (giống cây `/etc`).
  - `tools/`: công cụ đo.
  - `models/`: model.
  - `.venv/`: môi trường Python.
- Người dùng nói **tiếng Việt**. Trả lời bằng tiếng Việt; code và comment cũng dùng tiếng Việt như code hiện có.
- Người dùng làm việc qua SSH / VS Code Remote. Máy không có màn hình.
- Chưa có gimbal. Người dùng cầm camera di chuyển để thử.

## Quy tắc làm việc

- Câu hỏi kết thúc bằng "trả lời t trc chưa làm" thì **chỉ trả lời**, không sửa gì.
- **Hỏi trước khi đụng mạng** (NetworkManager, WiFi onboard, regdom, SSH). Người dùng SSH qua WiFi/Ethernet; làm hỏng là mất kết nối.
- **Tắt camera khi không thử**: `sudo systemctl stop uav-tracker` (tránh nóng). Bật lại khi xong.
- Camera chỉ mở được bởi **1 tiến trình**: dừng service trước khi chạy tay `uav_tracker.py`, `tools/latency_probe.py` hoặc `tools/stream_video.sh`, rồi bật lại.
- **Khoá wfb-ng là bí mật**:
  - `/etc/drone.key`, `/etc/gs.key` và bản sao ở `~/wfb-keys/` nằm ngoài repo.
  - Không commit, không in nội dung, không tạo cặp khoá mới. Khoá mới làm mất liên lạc với bên kia.
- Đo thật trước khi kết luận. Đã có số đo nằm trong README thì đừng đoán lại. Báo số liệu kèm cách đo.

## Lệnh hay dùng (Pi)

```bash
sudo journalctl -u uav-tracker -f                 # [stats] mỗi 5 s: fps video/detect, ms detect, mục tiêu, nhiệt
sudo journalctl -u wifibroadcast@drone -f          # wfb-ng; "packets dropped" = sóng nghẽn
python3 tools/wfb_stats.py 15                      # tải sóng (trần ~998 gói/s), drop, hàng đợi UDP 5602
vcgencmd get_throttled; vcgencmd measure_temp      # 0x0 = chưa từng sụt áp/hạ xung từ lúc boot
sudo dmesg -T | grep -iE "undervolt|imx708|usb .*disconnect"
lsusb | grep -iE "18d1|1a6e|0bda"                  # Coral (18d1:9302 sau khi nạp firmware), card WiFi
.venv/bin/python -m py_compile tracker/*.py tools/*.py
sudo systemctl restart uav-tracker                 # sau khi sửa code
```

Cài lại hoặc sửa cấu hình hệ thống bằng `deploy/install.sh` (chạy lại nhiều lần được).
`deploy/etc/<đường dẫn>` phải khớp `/etc/<đường dẫn>`. Sửa một bên thì đồng bộ bên kia.
Service cài từ `deploy/etc/systemd/system/uav-tracker.service` (thay `@REPO@`).

## Bố cục và quyết định thiết kế (đừng đảo ngược khi không đo lại)

- Một tiến trình giữ camera, Picamera2 lấy 2 luồng từ ISP:
  - main 1920x1080 → encoder H.264 phần cứng → `gst-launch` RTP → UDP 5602 → wfb-ng. Luồng này không đi qua Python.
  - lores 300x300 → Coral → SORT → JSON tới `10.5.0.1:5700`.
  - Detect chậm hay lỗi đều **không** được làm chậm video.
- **Bitrate 5 Mbps** vì trần sóng MCS1 + FEC 8/12 chỉ ~7.5 Mbps video. 8 Mbps từng gây drop 7–10% và trễ 1–2 s.
- **Mode cảm biến 2304x1296** để có đủ góc nhìn. 1080p30 đều ở 5 Mbps (ở 8 Mbps chỉ ~27 fps).
- Thư viện: `tflite_runtime` + `load_delegate("libedgetpu.so.1.0")`, không dùng pycoral. `libedgetpu1-std 16.0tf2.16.1`. venv `--system-site-packages` để dùng picamera2 từ apt. `numpy<2`.
- Tự phục hồi:
  - Coral lỗi → video vẫn chạy, thử lại với thời gian chờ tăng dần 5 → 60 s. Thử dồn dập từng kéo sụt áp cả hệ thống.
  - Camera/encoder/gst chết hoặc luồng detect đứng > 10 s → thoát mã 1 để systemd restart.
  - `kill -USR1 <pid>` in stack mọi luồng ra log.
- Toạ độ chuẩn hoá 0..1. Định dạng JSON trong README là **giao diện với mặt đất**: thêm trường thì được, đổi hoặc xoá trường thì phải sửa cả app laptop (`MH Controller/uav_viewer.py`, `TrackReceiver` / `TrackOverlay`).

## Bẫy đã gặp (chi tiết trong README)

- Không chạy `install_gs.sh` (wfb-ng) hay `dkms-install.sh` (driver).
- Region `BO` làm rớt WiFi onboard, nên dùng `VN`.
- `libedgetpu1-max` segfault. PyPI `pycoral` là package khác, không phải của Google.
- `wlan0`/`wlan1` đảo tên giữa các lần boot. Đừng hardcode; wfb-ng chọn card theo driver `rtl88xxau_wfb`.
- GNU `timeout` gửi tín hiệu cho cả process group (giết luôn gst). Dùng `timeout --foreground`.
- Pi không có RTC: giờ hệ thống ngoài hiện trường sai vài ngày. Dùng `seq`/`sensor_ts_ns`.
- Log journald đã lưu ra thẻ (`/etc/systemd/journald.conf.d/99-persistent.conf`), xem được log của lần boot trước bằng `journalctl -b -1`.

## Vấn đề phần cứng đang mở (kiểm tra trước khi nghi phần mềm)

- **Nguồn yếu**: `Undervoltage detected` → Coral/card WiFi rớt USB → wfb-ng và tracker restart dây chuyền (2026-10-11).
- **Cáp CSI chập chờn**: `imx708 error -5`, `Camera frontend has timed out` khi cầm camera di chuyển.
- Mặt đất: card 8812EU phát yếu (5 dBm, nguồn sụt) → chiều GS→UAV mất ~27% gói.

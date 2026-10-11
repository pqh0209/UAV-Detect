# CLAUDE.md — hướng dẫn cho Claude Code / AI agent

Đọc `README.md` để biết cách chạy, giao diện mạng và log. Thư mục này là **app xem video trên laptop Windows**
(trong repo chung: `MH Controller/`). Hai phần còn lại: `Pi Air Unit/` (UAV) và `Radxa VRX/` (trạm mặt đất).

## Bối cảnh

- Người dùng nói tiếng Việt; trả lời và viết comment bằng tiếng Việt.
- Pi gửi video H.264 1080p30 (~5 Mbit/s, RTP pt=96, mtu 1400, IDR mỗi 30 khung, SPS/PPS lặp, không B-frame) và
  JSON `uav_track` ~30 gói/s qua wfb-ng. Radxa (`gs/gs_forward.py`) chuyển tiếp nguyên gói: video → laptop :5600,
  JSON → :5700, tới các IP `--dest` và máy xem tự đăng ký qua UDP :5599 (hết hạn 5 s).
- Mục tiêu của app: **độ trễ thấp nhất**. App chỉ thêm ~5–15 ms (nhận gói cuối → vẽ xong).

## Kiến trúc `viewer/uav_viewer.py` (một file, chia theo mục)

| Phần | Lớp | Ghi chú |
|---|---|---|
| Nhận + tách RTP | `RtpReceiver`, `H264Depacketizer` | Tự tách RTP (NAL đơn / STAP-A / FU-A), **không** dùng demuxer RTP/SDP của FFmpeg (có bộ đệm sắp xếp lại) |
| Giải mã | `AUQueue`, `H264Decoder` | PyAV, cờ LOW_DELAY, **không frame-threading** (giữ thêm N−1 khung). Mất gói theo `--loss-mode` |
| Hiển thị | `LatestFrame`, `Viewer` | pygame; chỉ vẽ khung mới nhất, không chờ, không đồng bộ PTS |
| Toạ độ + box | `TrackReceiver`, `TrackOverlay` | JSON → box. `Overlay.on_click` để dành cho việc chọn mục tiêu |
| Tự đăng ký | `GsSubscriber` | Có video → gửi thẳng tới Radxa; mất video → `--gs` + broadcast mọi mạng |
| Log | `EventLog`, `LOG(...)` | Ghi bằng luồng riêng, không bao giờ chặn luồng nhận / giải mã / UI |

## Quy tắc khi sửa

- Không thêm bộ đệm, hàng đợi dài, hay đồng bộ theo đồng hồ vào đường video.
- JSON `uav_track` là giao diện với Pi (`Pi Air Unit/tracker/uav_tracker.py`): đọc phòng thủ (`_track`), thêm trường
  thì được. Đổi cổng / giao thức thì phải sửa cả `Radxa VRX/gs/gs_forward.py`.
- Sửa xong: chạy `python tests\test_viewer.py` (phải "TẤT CẢ ĐẠT"), rồi `build_exe.ps1` để cập nhật exe.
- Python ở máy người dùng: `H:\Tracking\python.exe` (3.11). Exe đang chạy thì file bị khoá: script build ra thư mục
  tạm rồi mới chép.
- Không đưa lên git: `dist/`, log, khoá wfb-ng (`*.key`).

# GS — trạm mặt đất (Radxa Zero 3W, Debian 11)

Từ 2026-10-11 trạm mặt đất **chỉ chuyển tiếp** (`gs_forward.py`, service `uav-gs-forward`): nhận video RTP từ
wfb-ng và JSON toạ độ từ UAV rồi gửi nguyên gói tới laptop. Laptop tự giải mã và vẽ box (thư mục `APP/`).
`gs_overlay.py` (giải mã, vẽ box, nén lại trên Radxa) được giữ lại làm dự phòng, service của nó đang tắt.

| File | Vai trò | Vị trí trên Radxa |
|---|---|---|
| `gs_forward.py` | Nhận video (127.0.0.1:5600, từ wfb-ng) và toạ độ (0.0.0.0:5700, từ UAV qua tunnel). Gửi tới laptop: video → cổng 5600, toạ độ → 5700. Đích là IP `--dest` và các máy xem tự đăng ký (gửi gói UDP tới cổng 5599 mỗi ~1 s, hết hạn sau 5 s). | `/home/radxa/uav-project/` |
| `uav-gs-forward.service` | Service systemd chạy `gs_forward.py $FWD_ARGS` (**đang dùng**, enabled) | `/etc/systemd/system/` |
| `config/uav-gs-forward.default` | Biến `FWD_ARGS` (`--dest IP` cố định, tuỳ chọn) | `/etc/default/uav-gs-forward` |
| `gs_overlay.py`, `uav-gs-overlay.service`, `config/uav-gs-overlay.default` | Dự phòng: giải mã, vẽ box, nén lại, phát UDP/RTSP; `--raw-udp` / `--telem-udp` chuyển tiếp | đang tắt |
| `wfb_watchdog.py`, `wfb-watchdog.service` | Mất video từ wfb-ng → nạp lại driver card và khởi động lại `wifibroadcast@gs` | `/home/radxa/uav-project/`, `/etc/systemd/system/` |
| `config/wifibroadcast.cfg` | Cấu hình wfb-ng (kênh 165, vùng, công suất) | `/etc/wifibroadcast.cfg` |
| `uav.sdp`, `uav_raw.sdp` | Mở luồng bằng VLC hoặc ffplay | — |
| `test_sender.py`, `test_latency.py` | Giả lập UAV / đo độ trễ | — |
| `CLAUDE.md` | Tài liệu bàn giao toàn hệ thống (UAV + GS) | `/home/radxa/uav-project/` |

Sửa cấu hình xong thì chạy `sudo systemctl restart uav-gs-forward`.

Không đưa lên repo: `/etc/gs.key` (khoá wfb-ng) và driver `src/rtl8812eu` (mã bên thứ ba, cài bằng DKMS).

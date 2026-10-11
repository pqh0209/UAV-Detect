# GS — trạm mặt đất (Radxa Zero 3W, Debian 11)

| File | Vai trò | Vị trí trên Radxa |
|---|---|---|
| `gs_overlay.py` | Nhận RTP H.264 từ wfb-ng (UDP 5600) và JSON toạ độ (UDP 5700). Chuyển tiếp RTP gốc (`--raw-udp`) và JSON (`--telem-udp`) tới laptop. Dự phòng: giải mã, vẽ box, nén lại, phát UDP (`--udp`) và RTSP `:8554/uav` | `/home/radxa/uav-project/` |
| `uav-gs-overlay.service` | Service systemd chạy `gs_overlay.py $GS_ARGS` | `/etc/systemd/system/` |
| `config/uav-gs-overlay.default` | Biến `GS_ARGS` (IP laptop nhận video / toạ độ) | `/etc/default/uav-gs-overlay` |
| `wfb_watchdog.py`, `wfb-watchdog.service` | Mất video từ wfb-ng → nạp lại driver card và khởi động lại `wifibroadcast@gs` | `/home/radxa/uav-project/`, `/etc/systemd/system/` |
| `config/wifibroadcast.cfg` | Cấu hình wfb-ng (kênh 165, vùng, công suất) | `/etc/wifibroadcast.cfg` |
| `uav.sdp`, `uav_raw.sdp` | Mở luồng 5600 / 5601 bằng VLC hoặc ffplay | — |
| `test_sender.py`, `test_latency.py` | Giả lập UAV / đo độ trễ | — |
| `CLAUDE.md` | Tài liệu bàn giao toàn hệ thống (UAV + GS) | `/home/radxa/uav-project/` |

Cấu hình hiện tại (laptop cắm dây thẳng):

```
GS_ARGS="--udp 192.168.50.2:5600 --raw-udp 192.168.50.2:5601 --telem-udp 192.168.50.2:5602"
```

Sửa xong thì chạy `sudo systemctl restart uav-gs-overlay`.

Không đưa lên repo: `/etc/gs.key` (khoá wfb-ng) và driver `src/rtl8812eu` (mã bên thứ ba, cài bằng DKMS).

# UAV Tracking Camera — wfb-ng + Coral Edge TPU

Camera gắn trên UAV tự nhận diện và bám mục tiêu (**người, ô tô, xe máy, xe đạp**). Video 1080p30
và toạ độ mục tiêu được gửi xuống trạm mặt đất qua **[wfb-ng](https://github.com/svpcom/wfb-ng)**
(WiFi broadcast: card RTL8812AU ở chế độ monitor, kênh 5.8 GHz).

- **UAV:** Raspberry Pi 4B + camera IMX708 + Coral USB Accelerator. Video H.264 gửi xuống là video gốc. Detect/tracking chạy song song và chỉ gửi toạ độ.
- **Trạm mặt đất** (thư mục `Radxa VRX/`): Radxa Zero 3W. Nhận video và toạ độ rồi **chuyển tiếp nguyên vẹn** sang laptop (`gs_forward.py`, thêm < 1 ms).
- **Laptop** (thư mục `MH Controller/`): app `uav_viewer` giải mã video độ trễ thấp và **vẽ box** từ toạ độ.
- **Chưa có gimbal.** Toạ độ đã có trường `err` (lệch tâm khung) để sau này điều khiển gimbal.

```
                         UAV (Raspberry Pi 4B)
 IMX708 ─ ISP ─┬─ main 1920x1080 → H.264 phần cứng 5 Mbps → RTP → UDP :5602 ─┐
  (2304x1296)  │                                                            ├─ wfb-ng drone ─ RTL8812AU ─┐
               └─ lores 300x300 → Coral (SSD MobileNet V2) → SORT            │   kênh 165, MCS1, FEC 8/12│
                       → JSON toạ độ → 10.5.0.1:5700 (tunnel wfb-ng) ───────┘                           │
                                                                                                    sóng 5.8 GHz
                         Trạm mặt đất (Radxa Zero 3W)                                                    │
 RTL8812EU ─ wfb-ng gs ─┬─ video RTP → :5600 ─┐                                                          │
                        └─ tunnel 10.5.0.1 → :5700 ─┴─ gs_forward: chuyển tiếp nguyên gói qua Ethernet
                                                         │  (laptop tự đăng ký nhận: UDP :5599 mỗi giây)
                         Laptop (Windows)                ▼
 uav_viewer: video :5600 → giải mã (PyAV, LOW_DELAY) → hiển thị; toạ độ :5700 → vẽ box + HUD
```

## Cấu trúc repo

```
tracker/                     Chương trình chạy trên UAV (service uav-tracker)
  uav_tracker.py             camera 2 luồng: video → wfb-ng; Coral detect + SORT → toạ độ
  sort_tracker.py            SORT (Kalman + Hungarian trên IoU)
models/                      labelmap.txt + download_model.sh (tải edgetpu.tflite, kiểm SHA-256)
deploy/                      Triển khai lên Pi
  install.sh                 cài toàn bộ (gói, driver, wfb-ng, config, venv, service) — chạy lại được
  etc/                       config hệ thống, cây thư mục giống hệt /etc trên Pi
tools/                       Công cụ thử/đo (không cần cho hệ thống chạy)
  wfb_stats.py               đo tải sóng: bitrate, gói/s, drop, hàng đợi
  latency_probe.py           đo trễ cảm biến → encoder
  stream_video.sh            chỉ phát video (không detect) để thử link
requirements.txt             gói Python cho venv (đã ghim phiên bản chạy được)
CLAUDE.md                    hướng dẫn cho Claude Code / AI agent làm việc trên repo
```

## Phần cứng UAV

| Thiết bị | Ghi chú |
|---|---|
| Raspberry Pi 4B 4GB | Raspberry Pi OS Bookworm 64-bit |
| Camera Module 3 (IMX708) | CSI, `dtoverlay=imx708,vcm=off`, **cáp CSI phải cố định chắc** |
| Coral USB Accelerator | **Cắm thẳng cổng USB3 (xanh)**, dây USB3 tốt — qua hub/USB2 thì không nạp được firmware |
| Card WiFi RTL8812AU (`0bda:881a`) | Driver svpcom `88XXau_wfb`, công suất `rtw_tx_pwr_idx_override=30` |
| Nguồn | **5 V ≥ 3 A (khuyên 5 A BEC trên UAV)**, dây ngắn và to — xem mục Xử lý sự cố |

## Cài đặt (UAV)

```bash
git clone <repo> ~/uav-project
~/uav-project/deploy/install.sh ~/wfb-keys/drone.key     # đường dẫn tới drone.key (xem Khoá)
# lắp anten vào card WiFi TRƯỚC khi reboot (wfb-ng phát ngay khi boot)
sudo reboot
```

`install.sh` làm các việc sau:
- Cài gói apt và wfb-ng từ `apt.wfb-ng.org`.
- Cài `libedgetpu1-std` bản 16.0tf2.16.1.
- Build driver RTL8812AU bằng DKMS.
- Chép config vào `/etc`, đặt regdom VN, bật `usb_max_current_enable`.
- Tạo venv và tải model.
- Bật các service `wifibroadcast@drone` và `uav-tracker`.

**Khoá wfb-ng:**
- Chạy `wfb_keygen` **một lần** để tạo cả cặp khoá.
  - `drone.key` → `/etc/drone.key` trên UAV.
  - `gs.key` → `/etc/gs.key` trên trạm mặt đất.
- Chép khoá qua USB/scp, **không bao giờ commit khoá**. Hai bên phải cùng một cặp, nếu không sẽ không giải mã được.

## Vận hành

```bash
sudo journalctl -u uav-tracker -f          # log mỗi 5 s: fps video/detect, thời gian detect, mục tiêu, nhiệt
python3 tools/wfb_stats.py                 # tải sóng: phải < ~80% trần, drop = 0, hàng đợi = 0
vcgencmd get_throttled                     # phải là 0x0 (khác 0 = đã sụt áp / hạ xung)
sudo systemctl stop uav-tracker            # tắt camera khi không thử (đỡ nóng)
ssh pi@10.5.0.2                            # từ trạm mặt đất, qua tunnel wfb-ng
```

Một số tham số thường đổi (`uav_tracker.py --help`):
- `--bitrate`, mặc định 5000000.
- `--sensor-mode`: `2304x1296` (mặc định, đủ góc nhìn) hoặc `1536x864` (crop giữa, trễ ít hơn ~13 ms).
- `--threshold`, `--max-age`, `--telemetry host:port` (dùng nhiều lần được).

## Giao diện UAV ↔ trạm mặt đất (hai bên phải khớp)

| Thông số | Giá trị |
|---|---|
| Kênh | **165 (5825 MHz), 20 MHz** — VN cho phép (dải 5725–5850 MHz) |
| `link_domain` | `"default"` |
| Khoá | `drone.key` (UAV) ↔ `gs.key` (mặt đất), cùng một lần `wfb_keygen` |
| Radio | MCS1, long GI, STBC 1, **LDPC 1** (card thu phải hỗ trợ LDPC: 8812AU/8812EU), FEC video 8/12 |
| Video | RTP H.264 pt=96, mtu 1400, IDR mỗi 30 frame, SPS/PPS lặp lại → mặt đất `udp://127.0.0.1:5600` |
| Tunnel | UAV `10.5.0.2` (`drone-wfb`) ↔ mặt đất `10.5.0.1` (`gs-wfb`) |
| Toạ độ | JSON/UDP tới `10.5.0.1:5700`, 1 gói mỗi frame (~30 gói/s, ~370 byte) |
| MAVLink | có sẵn kênh (UAV nghe 14560, mặt đất 127.0.0.1:14550) — chưa dùng |

Gói toạ độ:

```json
{
  "type": "uav_track", "seq": 481,
  "sensor_ts_ns": 4596107447000,
  "wall": 1791451053.202,
  "frame": [1920, 1080],
  "detector": "ok",
  "target": {
    "id": 11, "cls": "person", "score": 0.73,
    "box": [0.014, 0.0, 0.8724, 0.9887],
    "center": [0.4432, 0.4943],
    "err": [-0.1136, -0.0113],
    "lost_frames": 2
  },
  "tracks": [ {"id": 11, "cls": "person", "score": 0.73, "box": [0.014, 0.0, 0.8724, 0.9887], "lost_frames": 2} ]
}
```

- `box`/`center`: chuẩn hoá 0..1 theo khung video (x1, y1, x2, y2). Luồng detect và video cùng vùng cắt cảm biến nên áp thẳng lên được.
- `err`: lệch tâm khung, −1..1, phải/dưới là dương. Đây là đầu vào cho gimbal.
- `lost_frames > 0`: vị trí dự đoán bằng Kalman (đang tạm mất dấu).
- `detector`: `ok` / `retrying` / `degraded` (Coral lỗi; video vẫn chạy).
- `sensor_ts_ns`: đồng hồ monotonic của Pi. **`wall` không đáng tin ngoài hiện trường**: Pi không có RTC và không có mạng để đồng bộ giờ.
- Mục tiêu được tự chọn: track xác nhận xuất hiện sớm nhất. Mất dấu > 30 frame (1 s) thì chọn track khác. Chưa có lệnh chọn mục tiêu từ mặt đất.
- Toạ độ không đồng bộ chính xác từng frame với video. Mặt đất vẽ gói mới nhất (lệch cỡ vài chục ms).

## Số đo và giới hạn (Pi 4, 2026-10-11)

| Hạng mục | Kết quả |
|---|---|
| Video | 1080p30 đều, cảm biến 2304x1296 (toàn bộ góc nhìn) |
| Detect | 30 fps, ~17 ms/lần (Coral USB3; USB2 ~35 ms) |
| Trễ cảm biến → encoder | ~53 ms (mode 1536x864: ~40 ms) |
| Trần sóng MCS1 / 20 MHz | **~998 gói/s ≈ 11.2 Mbps trên sóng ≈ 7.5 Mbps video** (FEC 8/12) |
| Video 5 Mbps | ~706 gói/s (71% trần), 0 drop, hàng đợi 0 |
| Video 8 Mbps (cũ) | chạm trần: drop 7–10% gói **trước FEC** (không cứu được), hàng đợi 3 MB ≈ trễ thêm 1–2 s |

Muốn chất lượng cao hơn 5 Mbps thì tăng `mcs_index` (MCS2: thêm ~35–40% dung lượng, kém ~2–3 dB độ nhạy). Sau khi đổi phải đo lại bằng `tools/wfb_stats.py`.

Giới hạn phần cứng Pi 4:
- Không có encoder H.265. Encoder H.264 tối đa 1920 px mỗi chiều.
- 1080p56–60 không đạt (~28 fps).
- Muốn 1080p60 hoặc H.265 thì cần board mạnh hơn (ví dụ RK3588).

## Xử lý sự cố

| Triệu chứng | Nguyên nhân đã gặp | Cách xử lý |
|---|---|---|
| `Undervoltage detected`, card WiFi/Coral rớt USB hàng loạt, wfb-ng restart liên tục | Nguồn yếu; Coral nạp firmware kéo dòng mạnh | Nguồn 5 V ≥ 3 A dây ngắn; Coral cắm thẳng USB3; xem `vcgencmd get_throttled` |
| `imx708 ... error -5`, `Camera frontend has timed out`, video 0 fps | Cáp CSI tiếp xúc kém (nhất là khi cầm camera di chuyển) | Cắm lại hai đầu, ép chốt, dán cố định; thay cáp nếu gập |
| Coral `Failed to load delegate`, `detector: degraded` | Coral mất khỏi USB (`lsusb` không có `18d1:9302`) | Rút cắm lại vào cổng xanh; chương trình tự nhận lại (thử lại 5→60 s) |
| Video dưới đất vỡ hình, trễ tăng dần | Bitrate vượt trần sóng | `tools/wfb_stats.py`; hạ `--bitrate` hoặc tăng MCS |
| Mất WiFi onboard/SSH khi wfb-ng chạy | `wifi_region='BO'` (`iw reg set` áp dụng cho mọi card) | Dùng `VN` |
| `wlan0`/`wlan1` đổi tên sau reboot | Thứ tự nhận thiết bị | Bình thường: wfb-ng chọn card theo driver; NetworkManager đã được đặt không quản lý card này |

## Bẫy khi cài đặt

- **Không chạy `install_gs.sh`** của wfb-ng. Nó đặt region BO, ghi đè `/etc/motd`, sửa dhcpcd.
- **Không chạy `dkms-install.sh`** của driver svpcom, vì nó tắt IPv6 toàn hệ thống. Dùng `dkms add/build/install`.
- **`libedgetpu1-max` 16.0 bị segfault** với `tflite-runtime` 2.14, nên dùng `libedgetpu1-std 16.0tf2.16.1`. Repo apt Coral của Google trả 403.
- **Package `pycoral` trên PyPI không phải của Google**, và dự án cũng không cần. Chỉ dùng `tflite-runtime`.
- **Giữ `numpy<2`**: tflite-runtime 2.14 biên dịch với numpy 1.x. OpenCV ≥ 4.11 lại cần numpy 2, nên ghim 4.10.
- **Tên định dạng của Picamera2 bị đảo**: `RGB888` cho ra mảng BGR. Code này dùng YUV420 nên không dính.

## Trạng thái và việc còn mở

- [x] UAV: video 1080p30 → wfb-ng, Coral detect + SORT 30 fps, toạ độ qua tunnel, tự phục hồi bằng systemd.
- [x] Mặt đất (`Radxa VRX/`): wfb-ng gs + `gs_forward.py` chuyển tiếp sang laptop. Bỏ cách cũ vẽ box + nén lại + RTSP trên Radxa.
- [x] Laptop (`MH Controller/`): app xem video độ trễ thấp, vẽ box, che lỗi khi mất gói lẻ tẻ.
- [x] Link thật ở khoảng 1 m: video và toạ độ đã lên.
- [ ] Nguồn: UAV vẫn có sụt áp; card mặt đất phát yếu (5 dBm) → chiều GS→UAV mất ~27% gói.
- [ ] Cáp CSI chập chờn khi cầm camera di chuyển.
- [x] Trễ hiển thị: trước đây 0.6–0.8 s (vẽ lại trên Radxa + VLC). Nay Radxa chỉ chuyển tiếp, app laptop chỉ thêm ~10–20 ms.
- [ ] Thử tầm xa, đo nhiệt card WiFi, bay thử.
- [ ] Kênh lệnh chọn mục tiêu từ mặt đất; gimbal.

Model: SSD MobileNet V2 COCO (Edge TPU) của Google Coral, giấy phép Apache-2.0, được tải về bằng script và không đưa vào repo.

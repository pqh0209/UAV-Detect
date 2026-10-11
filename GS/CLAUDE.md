# Bàn giao dự án: UAV tracking camera — phía UAV đã xong, cần dựng trạm mặt đất

> File này tóm tắt toàn bộ những gì đã làm ở phía UAV (Raspberry Pi 4B) để Claude Code
> trên trạm mặt đất (Radxa Zero 3W) hiểu bối cảnh. Đặt file này làm `CLAUDE.md` trong thư mục dự án
> trên Radxa để Claude Code tự đọc khi mở phiên.
> Cập nhật lần cuối: 2026-10-08. Mục 8 ghi trạng thái trạm mặt đất (Radxa), cập nhật dần.

## 1. Mục tiêu dự án

Camera gắn trên UAV, tự nhận diện và bám theo mục tiêu (người / ô tô / xe máy / xe đạp).
Video và toạ độ mục tiêu được gửi xuống trạm mặt đất qua **wfb-ng** (WiFi broadcast, card
RTL8812AU ở chế độ monitor). Hiện **chưa có gimbal** — người dùng cầm camera di chuyển bằng
tay để test; toạ độ vẫn được gửi xuống đầy đủ để sau này điều khiển gimbal.

Người dùng nói tiếng Việt, điều khiển máy qua SSH / VS Code Remote-SSH (không có màn hình
gắn trực tiếp).

## 2. Phía UAV (Raspberry Pi 4B) — trạng thái: ĐÃ CHẠY

### Phần cứng
| Thiết bị | Chi tiết |
|---|---|
| Máy tính | Raspberry Pi 4B 4GB, Raspberry Pi OS Bookworm 64-bit, kernel 6.12.93+rpt-rpi-v8 |
| Camera | IMX708 (Camera Module 3) qua CSI, dùng Picamera2/libcamera |
| AI | Coral USB Accelerator (Edge TPU), **phải cắm cổng USB 3.0** (đã xác nhận 5000M) |
| Card WiFi wfb-ng | Card no-name chip **Realtek RTL8812AU** (USB ID `0bda:881a`), USB 2.0 |

### Phần mềm / cấu hình
- **Driver card:** `svpcom/rtl8812au` (module `88XXau_wfb`), cài qua DKMS. Interface lên là `wlan1`.
- **wfb-ng 25.1.117** từ repo apt chính thức `apt.wfb-ng.org` (release-25.01), service
  `wifibroadcast@drone` tự chạy khi boot (cần `wifibroadcast.service` cũng enabled).
- **`/etc/wifibroadcast.cfg` phía drone:**
  ```ini
  [common]
  wifi_channel = 165     # 5825 MHz, 20 MHz
  wifi_region = 'VN'
  [drone]
  link_domain = "default"
  [drone_video]
  peer = 'listen://0.0.0.0:5602'
  ```
- **Công suất phát:** `/etc/modprobe.d/wfb.conf` → `options 88XXau_wfb rtw_tx_pwr_idx_override=30`
  (mức "max" wfb-ng khuyến nghị cho 8812AU, thang 0–63). Đã thử tải 90s kèm Coral: không sụt áp.
- **Khoá:** `drone.key` ở `/etc/drone.key` trên Pi. **`gs.key` là cặp tương ứng**, cần chép
  sang trạm mặt đất ở `/etc/gs.key` (người dùng sẽ chép tay, qua kênh an toàn — KHÔNG tạo cặp
  khoá mới ở trạm mặt đất, nếu không sẽ không giải mã được).

### Ứng dụng chạy trên UAV: service `uav-tracker`
Code: `~/uav-project/uav-wfb-ng/app/uav_tracker.py` (+ `sort_tracker.py`). Một tiến trình giữ
camera, lấy 2 luồng từ ISP cùng lúc:

```
Cảm biến IMX708 (mode 1536x864 @30)
  ├─ luồng main 1920x1080 YUV420 → encoder H.264 phần cứng Pi (8 Mbps, IDR mỗi 30 frame,
  │     SPS/PPS lặp lại) → gst rtph264pay (pt=96, mtu=1400) → UDP 127.0.0.1:5602 → wfb-ng
  └─ luồng lores 300x300 → Coral (SSD MobileNet V2 COCO, Edge TPU) → lọc 4 lớp
        → SORT tracking → JSON/UDP → 10.5.0.1:5700 (qua tunnel wfb-ng)
```
- Video gửi xuống là **video gốc, KHÔNG vẽ box**. Box phải vẽ đè ở trạm mặt đất từ toạ độ.
- Số đo thực tế: video **30 fps** đều, detect **30 fps** (~18 ms/lần), toạ độ **30 gói/giây**,
  ~370 byte/gói. Nhiệt Pi ~48°C.
- Tự phục hồi: Coral lỗi → video vẫn chạy, detect tự thử lại; chương trình chết/treo →
  systemd khởi động lại (Restart=always).

## 3. Giao diện giữa UAV và trạm mặt đất (phải khớp)

| Thông số | Giá trị |
|---|---|
| Kênh WiFi | **165 (5825 MHz)**, 20 MHz — bắt buộc giống nhau |
| `link_domain` | **"default"** — bắt buộc giống nhau |
| Khoá | `drone.key` (UAV) ↔ `gs.key` (trạm mặt đất), cùng 1 lần `wfb_keygen` |
| Vùng (`wifi_region`) | UAV dùng `VN`. Trạm mặt đất dùng vùng nào cũng được miễn cho phép kênh 165 (VN và BO đều cho phép) |
| Video ra ở trạm mặt đất | wfb-ng profile `gs` mặc định đẩy **RTP H.264 (pt=96)** tới `udp://127.0.0.1:5600` |
| Tunnel IP | UAV `10.5.0.2` (`drone-wfb`) ↔ trạm mặt đất `10.5.0.1` (`gs-wfb`) |
| Toạ độ mục tiêu | UAV gửi JSON/UDP tới **`10.5.0.1:5700`** (trạm mặt đất lắng nghe cổng 5700) |
| MAVLink | wfb-ng có sẵn kênh (UAV nghe 14560, trạm mặt đất đẩy tới 127.0.0.1:14550) — chưa dùng |

### Định dạng gói toạ độ (1 gói / frame detect, ~30 gói/giây)
```json
{
  "type": "uav_track", "seq": 481,
  "sensor_ts_ns": 4596107447000,     // timestamp cảm biến của frame (ns, đồng hồ Pi)
  "wall": 1791451053.202,            // thời gian thực trên Pi (giây)
  "frame": [1920, 1080],             // kích thước video gửi xuống
  "detector": "ok",                  // "ok" | "retrying" | "degraded" (Coral lỗi)
  "target": {                        // null nếu chưa có mục tiêu
    "id": 11, "cls": "person", "score": 0.73,
    "box": [0.014, 0.0, 0.8724, 0.9887],   // x1,y1,x2,y2 CHUẨN HOÁ 0..1 theo khung video
    "center": [0.4432, 0.4943],
    "err": [-0.1136, -0.0113],       // lệch tâm khung, -1..1 (+ là phải/dưới) — dùng cho gimbal
    "lost_frames": 2                 // >0: đang là vị trí dự đoán (Kalman), chưa thấy lại
  },
  "tracks": [ {"id":11,"cls":"person","score":0.73,"box":[...],"lost_frames":2} ]
}
```
- Toạ độ chuẩn hoá áp thẳng lên video 1920x1080 (luồng detect và video cùng góc nhìn).
- Lớp có thể có: `person`, `car`, `motorcycle`, `bicycle`.
- Mục tiêu hiện được **tự chọn** (track xác nhận xuất hiện sớm nhất); mất dấu > 30 frame
  (1 giây) thì chọn mục tiêu khác. **Chưa có** lệnh chọn mục tiêu từ trạm mặt đất.
- Không có đồng bộ frame-chính-xác giữa gói toạ độ và khung video RTP; overlay ở trạm mặt
  đất nên vẽ gói toạ độ mới nhất (độ lệch cỡ vài chục ms).

## 4. Trạm mặt đất (Radxa Zero 3W) — tiến độ (cập nhật 2026-10-08)

| # | Việc | Trạng thái |
|---|---|---|
| 1 | Card RTL8812EU + driver svpcom `rtl8812eu` (DKMS) | Driver **XONG**. Card **CHẬP CHỜN do phần cứng** (xem mục 8) |
| 2 | Cài wfb-ng 25.1.117, cấu hình `/etc/wifibroadcast.cfg` (kênh 165, VN, `link_domain=default`, txpower 5 dBm) | **XONG, ĐANG CHẠY** (enabled) từ 2026-10-11 + `wfb-watchdog` tự phục hồi |
| 3 | `/etc/gs.key` | **ĐÃ ĐẶT** 2026-10-11 (64 byte, root 600). Khoá công khai UAV trong file: `c50df529…5a5a`; khoá công khai GS suy ra: `5401fb15…1f51` — cần đối chiếu với `/etc/drone.key` trên Pi |
| 4–5 | Hiển thị + vẽ box | **Từ 2026-10-11: LAPTOP vẽ box** (app riêng của người dùng). Radxa chỉ chuyển tiếp nguyên vẹn video RTP + JSON toạ độ bằng `gs_forward.py` (service `uav-gs-forward`). `gs_overlay.py`/`uav-gs-overlay` (vẽ box trên Radxa) đã **disable**, giữ code để dự phòng |
| 6 | Kiểm chứng toàn tuyến với UAV thật | **Video + toạ độ THẬT đã lên RTSP** (2026-10-11, ~1 m): 29 fps, telem 30 Hz, RSSI −48 dBm, 0 mất gói. Chưa thử tầm xa |
| 7 | Kênh lệnh chọn mục tiêu GS → UAV | Chưa |

Phương án hiển thị: ban đầu (c) Radxa vẽ box + nén lại (~60–70 ms, ~1.6 nhân CPU); **từ 2026-10-11 đổi sang
laptop vẽ box** (app riêng trên laptop, Radxa chỉ chuyển tiếp — xem mục 8 "Chuyển tiếp tới laptop").

## 5. Bẫy đã gặp — tránh lặp lại

- **`install_gs.sh` của wfb-ng làm nhiều việc toàn hệ thống:** đặt `wifi_region='BO'`, ghi
  `options cfg80211 ieee80211_regdom=RU` vào `/etc/modprobe.d/wfb.conf`, ghi đè `/etc/motd`,
  thêm `denyinterfaces` vào dhcpcd. Đọc trước, chỉ làm phần cần thiết.
- **`wifi_region = 'BO'` làm rớt WiFi onboard:** lệnh `iw reg set` áp dụng cho MỌI card. Trên Pi,
  BO cấm dải 5170–5250 MHz → `wlan0` (đang ở kênh 44) bị ngắt, mất SSH qua WiFi. Đã đổi sang VN.
  Nếu trạm mặt đất có WiFi onboard đang dùng để SSH, kiểm tra kỹ vùng trước khi đặt.
- **`dkms-install.sh` của driver svpcom tắt IPv6 toàn hệ thống** (ghi vào `/etc/sysctl.conf`).
  Đã cài bằng lệnh `dkms add/build/install` thủ công thay vì chạy script đó.
- **Cài `dkms` kéo theo headers kernel Debian generic không cần thiết** — đã gỡ.
- **wfb-ng chỉ tự nhận card có driver `rtl88xxau_wfb` / `rtl88x2eu`** (script `wfb-nics`), nên
  không đụng WiFi onboard — nhưng vẫn phải lưu ý vùng tần số ở trên.
- Package tên `pycoral` trên PyPI **không phải** Coral của Google (bản 0.2.0, thư viện GIS).
- `libedgetpu1-max 16.0` crash (segfault) với `tflite-runtime 2.14`; đang dùng
  `libedgetpu1-std 16.0tf2.16.1`. Repo apt Coral của Google hiện trả 403.

## 6. Giới hạn phần cứng Pi 4 đã đo (lý do chọn 1080p30)

- Encoder H.264 phần cứng Pi 4: khung tối đa 1920x1920 (không nén được 2304x1296), không có H.265.
- 1080p56: chỉ đạt ~28 fps. 1080p30 từ mode cảm biến 2304x1296: ~27 fps (ISP + encoder quá tải).
  1080p30 từ mode 1536x864: **30 fps đều** → đang dùng.
- Lưu ý: mode 1536x864 chỉ lấy **~67% vùng giữa** cảm biến (góc nhìn hẹp hơn). Muốn đủ góc nhìn:
  mode 2304x1296 + video 1600x900 (đo được 30 fps đều).
- Người dùng định nâng cấp phần cứng sau (ví dụ board RK3588/RK3566 có encoder H.265 1080p60).

## 7. Vấn đề còn mở phía UAV

- Một lần chạy thử bị treo không rõ nguyên nhân khi có người trong khung (chưa tái hiện được).
  Đã gia cố: lỗi một frame chỉ bỏ frame đó; luồng detect đứng >10s → in stack các luồng và
  thoát để systemd khởi động lại. Xem log: `sudo journalctl -u uav-tracker -f`.
- Chưa kiểm tra nhiệt độ card WiFi khi phát công suất 30.

## 8. Trạm mặt đất (Radxa Zero 3W) — chi tiết

### Hệ thống
| Mục | Giá trị |
|---|---|
| OS | Radxa OS dựa trên **Debian 11 bullseye**, arm64. Repo Debian ở `archive.debian.org`. Repo Radxa báo `NO_PUBKEY` (key Radxa hết hạn, có sẵn từ trước, không chặn apt) |
| Kernel | `5.10.160-26-rk356x` (vendor Radxa), headers có sẵn |
| RAM / lưu trữ | 3.7 GiB RAM. **eMMC 29G** là ổ duy nhất (đã rút thẻ SD) |
| sudo | **không cần mật khẩu** (`/etc/sudoers.d/010_radxa-nopasswd`, người dùng tạo 2026-10-08) |
| Nhiệt | ~41–43°C khi chạy overlay |

### Bẫy khởi động: thẻ SD trùng UUID (ĐÃ SỬA)
- Board từng cắm cả eMMC lẫn thẻ SD chứa **cùng image (UUID/PARTUUID trùng hệt)**. U-Boot
  (`u-boot-latest 2023.10`, `boot_targets=mmc1 mmc0…`, mmc1 = SD `fe2b0000`) đọc extlinux.conf
  **trên SD**, còn kernel mount root theo UUID và trúng eMMC → sửa `/boot` trên eMMC không có tác dụng.
- Đã chứng minh bằng dấu `uavboot=emmc` trong cmdline (không xuất hiện khi có SD, xuất hiện khi rút SD).
  **Đã rút SD, đã xoá dấu.** **Không cắm lại thẻ SD đó** (hoặc format nó trước).

### Cổng USB / đấu nối
- **Cổng USB 2.0 OTG Type-C đã ép sang host** bằng overlay `rk3568-dwc3-host.dtbo`
  (`/boot/dtbo/`, dòng `fdtoverlays` trong extlinux.conf, cập nhật bằng `u-boot-update`).
  Vì vậy đã **disable** `radxa-usbnet` và `radxa-adbd` (USB gadget, giờ luôn lỗi).
- Card wfb-ng đấu vào cổng OTG bằng **3 dây D+, D−, GND** (không VBUS, không CC). 5V/GND card
  đấu chung nguồn 5V cấp chân GPIO của board (cục sạc điện thoại).
- Cổng USB 3.0: bộ chuyển Ethernet **RTL8153** (`enx00e04c680009`) — đường SSH. NM connection
  "Wired connection 1": DHCP (192.168.1.75 khi cắm router) **+ IP tĩnh phụ 192.168.50.1/24**,
  `ipv4.dhcp-timeout infinity` (cắm thẳng laptop không có DHCP thì kết nối vẫn giữ, dùng 192.168.50.1).
  Laptop khi cắm thẳng: 192.168.50.2/24, KHÔNG đặt gateway/DNS. RTSP: rtsp://192.168.50.1:8554/uav
- WiFi onboard: `wlan0`, AIC8800 (SDIO), 192.168.1.123, kênh 44 — đường dự phòng.

### Card wfb-ng RTL8812EU — VẤN ĐỀ PHẦN CỨNG CHƯA GIẢI QUYẾT
- USB ID **`0bda:a81a`**, driver **svpcom/rtl8812eu** (`8812eu.ko`, tên driver `rtl88x2eu`), cài DKMS
  thủ công: nguồn ở `~/uav-project/src/rtl8812eu` (đã sửa Makefile: `CONFIG_PLATFORM_ARM64_RPI=y`),
  bản DKMS ở `/usr/src/realtek-rtl88x2eu-5.15.0.1~20230815`. Build ~8 phút. **Không chạy
  `dkms-install.sh`** (nó tắt IPv6 — cùng bẫy với driver 8812au).
- `/etc/modprobe.d/8812eu.conf`: `options 8812eu rtw_regd_src=1 rtw_tx_pwr_by_rate=0 rtw_tx_pwr_lmt_enable=0`.
- Interface: `wlx145d345c6f93`; `wfb-nics` nhận đúng card này.
- **Lỗi:** card rớt khỏi USB liên tục (`error -71`, `disabled by hub (EMI?)`, >100 lần/giờ đầu),
  đặc biệt **mỗi khi bật card lên** (`ip link set up` → nạp firmware/bật RF → card reset, mọi lệnh
  `iw` trả `No such device`). Dồn dập khi CPU/VPU tải nặng. Kết luận: **nguồn sụt và/hoặc dây D+/D−
  rời gây nhiễu** — phải sửa phần cứng (dây ngắn xoắn đôi/cáp USB thật, nguồn 5V ≥3A dây to, tụ
  470–1000 µF sát chân 5V card). Chưa thử được chế độ monitor.
- `/etc/NetworkManager/conf.d/99-wfb-ng-unmanaged.conf`: NM không quản lý card driver `rtl88x2eu`/`rtl88xxau_wfb`.

### wfb-ng
- Repo `deb [signed-by=/usr/share/keyrings/wfb-ng.gpg] https://apt.wfb-ng.org/ bullseye release-25.01`,
  gói `wfb-ng 25.1.117` (đã đọc postinst: chỉ `daemon-reload`, không tạo key).
- `/etc/wifibroadcast.cfg`: `[common] wifi_channel=165, wifi_region='VN'`, `[gs] link_domain="default"`
  (đã kiểm tra parse: video → `connect://127.0.0.1:5600`, tunnel `10.5.0.1/24`, keypair `gs.key`).
  VN cho phép cả kênh 165 lẫn kênh 44 của `wlan0`; regdom hệ thống vốn đã là VN.
- **Chưa enable** `wifibroadcast@gs`. Khi có key + card ổn:
  `sudo systemctl enable --now wifibroadcast@gs` rồi `wfb-cli gs`.

### Ứng dụng trạm mặt đất (thư mục `~/uav-project/`)
| File | Vai trò |
|---|---|
| `gs_overlay.py` | Nhận RTP :5600 + JSON :5700 → mppvideodec → chép sang buffer DMA của encoder → vẽ box/nhãn/HUD/tâm ngắm → mpph264enc (8 Mbps, GOP 30) → RTSP `rtsp://<ip>:8554/uav` (tuỳ chọn `--udp HOST:PORT`) |
| `uav-gs-overlay.service` | bản cài ở `/etc/systemd/system/`, **enabled + đang chạy**, User=radxa, Restart=always. Tham số thêm trong `/etc/default/uav-gs-overlay` (`GS_ARGS`). Từ 2026-10-11: `--udp 192.168.1.62:5600` (laptop; cắm thẳng dây thì đổi 192.168.50.2) — nhánh UDP luôn chạy (~1.6 nhân CPU), RTSP vẫn song song |
| `uav.sdp` | File mô tả luồng RTP/UDP cổng 5600 cho VLC/ffplay trên laptop (QGroundControl tự nhận UDP 5600) |
| `test_sender.py` | Giả lập UAV (video 1080p30 có khối trắng chạy vòng tròn + JSON khớp vị trí) → thử không cần UAV |
| `test_latency.py` | Đo độ trễ toàn tuyến khi chạy với `test_sender.py --telem-copy 5701` |
| `gs_forward.py` + `uav-gs-forward.service` | **ĐANG DÙNG** (enabled). Chuyển tiếp nguyên vẹn: RTP 127.0.0.1:5600 (từ wfb-ng) → laptop:5600; JSON 0.0.0.0:5700 (tunnel) → laptop:5700. Máy nhận = `--dest` trong `/etc/default/uav-gs-forward` (hiện `192.168.1.73`) **và/hoặc tự đăng ký**: máy xem gửi gói UDP bất kỳ tới Radxa cổng **5599** mỗi ~1 s (hết hạn sau 5 s) → không phải sửa Radxa khi laptop đổi IP. ~25% 1 nhân CPU, thêm < 1 ms trễ. Log: `journalctl -u uav-gs-forward -f` |
| `wfb_watchdog.py` + `wfb-watchdog.service` | Giám sát link wfb-ng, tự reset USB / nạp lại driver 8812eu (root, enabled) — xem mục "Link wfb-ng thật" |

Hành vi: không có client xem → không vẽ/nén (gần 0% CPU). Mất video >1 s → phát khung đen
"NO VIDEO". Gói toạ độ cũ >0.5 s → không vẽ box, HUD "NO TELEMETRY"; gói sai định dạng bị loại
(đếm "gói hỏng" trong log), không bao giờ làm mất khung video. Box mục tiêu xanh lá (đang thấy) /
vàng + "(pred)" (`lost_frames>0`), track khác xanh lơ mảnh. Chữ trắng trên nền đặc tối.
Log: `journalctl -u uav-gs-overlay -f` (mỗi 10 s: fps vào/ra, telem Hz, thời gian chép/vẽ/nén,
số khung bỏ, số lỗi, client nào vào xem).

Kiến trúc luồng: appsink (luồng giải mã) chỉ giữ khung MỚI NHẤT rồi trả về ngay → luồng `process`
chép + vẽ + đẩy đi nén. Xử lý chậm thì bỏ khung, không bao giờ chặn bộ giải mã (chặn = gói dồn
trong bộ đệm UDP 4 MB → trễ tăng dần tới vài giây). Chạy trùng 2 bản → bản sau thoát với lỗi cổng bận.

Số đo (giả lập trên chính Radxa, 2026-10-08, bản sau rà soát): vào/ra 30 fps; chép ~13–20 ms
(3 luồng), vẽ ~4 ms, nén ~15 ms; CPU ~1.6 nhân khi có người xem (phần lớn là đọc bộ nhớ không cache —
gần như cố định ~50 ms CPU/khung, thêm luồng chỉ giảm trễ chứ không giảm CPU); nhiệt ~40°C.
Độ trễ toàn tuyến (test_sender tạo khung → client RTSP giải mã xong, loopback, chưa gồm wfb-ng):
**trung vị ~70–76 ms**, 90% < ~100–150 ms. Client vào giữa chừng nhận IDR sau 0–1 khung.
Đổi độ phân giải luồng vào (1080p ↔ 720p) → tự tạo lại bộ giải mã, mất < 1 s hình.

### Bẫy phần mềm video trên RK3566 này (đã đo, tránh lặp lại)
- **RGA hỏng** (`RgaBlit fail: Invalid argument`, `rga_mm: failed to map buffer`), nhiều khả năng do
  RGA2 chỉ địa chỉ 32-bit trên board 4 GB (hoặc librga 1.3.1 lệch driver). Hệ quả: `mppvideodec
  format=/width=/height=` (cần RGA) không ra khung.
- **`mpph264enc` với caps 1920x1080 tự "căn" lên 1088 và chép MỌI khung bằng RGA** → RGA lỗi → hoặc ra
  khung trống (xanh lá, ví dụ `videotestsrc ! mpph264enc`) hoặc chép CPU còn ~16 fps.
  **Sửa: `GST_MPP_ENC_UNALIGNED_VSTRIDE=1`** + đẩy buffer lấy từ pool mà encoder đề xuất
  (allocation query, `mppallocator`) → "using imported buffer", 30 fps, không RGA.
- **`mpph264enc` không đánh dấu khung khoá cho GstVideoEncoder** ("sync point: 0" mọi khung; cờ
  DELTA_UNIT ở đầu ra encoder cũng sai — đọc sau `h264parse` mới đúng) → sự kiện force-key-unit
  chuẩn chỉ có tác dụng LẦN ĐẦU, các lần sau kẹt. **Ép IDR bằng cách đổi thuộc tính `gop` N↔N+1**
  (MPP cấu hình lại → IDR ngay khung kế, 5/5 lần). Dùng khi client RTSP gửi PLAY.
- **`mppvideodec` (gstreamer-rockchip 1.14 Radxa) không đổi caps/GstVideoMeta khi luồng đổi độ
  phân giải giữa chừng** → khung mới bị đọc theo bố cục cũ → vỡ hình. Phát hiện qua caps của
  `h264parse` (đọc SPS) rồi tạo lại pipeline giải mã.
- **Pipeline giải mã đưa về NULL rồi PLAYING thì kẹt ở PAUSED** (không ra khung) → muốn khởi động
  lại phải TẠO pipeline mới (áp dụng cả cho tự phục hồi khi lỗi giải mã).
- **Bộ nhớ đầu ra mppvideodec (và buffer pool encoder) không cache**: CPU đọc cả khung 1080p ~20–40 ms
  (1 luồng), ~9 ms phần Y với 3 luồng (4 luồng không nhanh hơn). Chép bằng NEON `ld1` 64 byte
  CHẬM hơn memcpy (49 vs 20 ms). Không vẽ thẳng lên buffer giải mã (là khung tham chiếu H.264 →
  box "lem" sang khung sau). Vẽ chỉ GHI (nền chữ đặc), tránh đọc lại vùng nhớ này.
  Mọi công cụ đọc khung giải mã cũng dính: lấy mẫu `y[::4, ::4]` đọc lẻ từng byte ~30 ms/khung
  (test_latency.py từng đo ra độ trễ giả 0.8 s vì thế) → chép nguyên hàng trước rồi mới lấy mẫu.
- **PyGObject `buffer.map()`: mỗi lần đọc `mapinfo.data` chép cả khung** (~40 ms) → dùng ctypes
  `gst_buffer_map` (`hash(buf)` = con trỏ GstBuffer*; `check_mapping()` kiểm tra lúc khởi động).
- **`textoverlay → mpph264enc` trong Python kẹt PAUSED** → vẽ chữ bằng pycairo + numpy.
- **`appsrc` (GStreamer 1.18) không tự bỏ khung cũ** → khi RTSP đang bắt tay khung dồn hàng → trễ vài giây.
  Sửa: còn khung chờ trong appsrc (`current-level-bytes>0`) thì bỏ khung mới.
- Duyệt `collections.deque` bằng generator trong lúc luồng khác `append` → "deque mutated during
  iteration" (đo: 98/20000 lần) → chụp bằng `list(deque)` trước (`count_recent`).
- `videotestsrc pattern=ball` chỉ ~10 fps ở 1080p trên A55.
- Đã cài thêm `libc6-dev` (để thử build C); đã nạp `/etc/sysctl.d/98-wifibroadcast.conf`
  (`rmem_max=5000000`, cho bộ đệm UDP 4 MB của udpsrc).

### Link wfb-ng thật (2026-10-11)
- Card RTL8812EU sau khi người dùng sửa đấu nối: đứng yên thì ổn, nhưng **nguồn 5V vẫn sụt khi card bật
  sóng/phát**: đúng giây wfb-ng khởi động, CẢ bộ chuyển Ethernet RTL8153 (bus 8, cổng USB3) lẫn card
  (bus 5) bị rớt USB (3 lần Ethernet trùng giây khởi động wfb); card từng rớt sau ~1 phút chạy tải
  ("Device not responding to setup address", không tự lên lại). Gốc rễ: nguồn (cục sạc điện thoại qua
  chân GPIO) → cần nguồn 5V ≥3A dây to/ngắn, tụ 470–1000 µF sát card.
- **Giảm nhẹ bằng phần mềm:** `wifi_txpower = 500` (8812eu: dBm×100 → 5 dBm) trong `/etc/wifibroadcast.cfg`.
  Sau đó 3 phút liền không rớt USB nào (trước đó ~1 phút). Trạm mặt đất chủ yếu thu nên không ảnh
  hưởng video; tăng lại khi nguồn đã ổn (cho kênh lệnh lên UAV).
- **Driver 8812eu "chết phần thu"** (card vẫn monitor/kênh 165 nhưng 0 gói; dmesg có `WARNING
  rtw_mlmeext_disconnect`, `buffer_cnt = -1`, `cmd queue crashed`) — xảy ra ngẫu nhiên khi wfb-ng cấu hình
  lại card (khởi động/khởi động lại), nhất là ngay sau khi card vừa xuất hiện. Restart wfb-ng KHÔNG chắc
  chữa; chữa được bằng: dừng wfb-ng → `rmmod 8812eu` → `modprobe 8812eu` → chờ ~4 s → start wfb-ng.
  Đừng tự cấu hình card bằng `iw` trước khi wfb-ng chạy.
- **Tunnel kẹt** (từng gặp sau reset USB): gói tunnel tới đều nhưng 100% `dec_err`, `session: None`
  → mất toạ độ dù video vẫn chạy. Chữa như trên.
- **`wfb_watchdog.py` + `wfb-watchdog.service`** (root, enabled): đọc JSON API wfb-ng (127.0.0.1:8103)
  mỗi 2 s. Card mất khỏi USB > 6 s → unbind/bind `fcc00000.dwc3` (đã thử: card lên lại). Video 0 gói
  > 10 s hoặc tunnel không giải mã được > 15 s → nạp lại driver + khởi động lại wfb-ng (đã thử: lên lại
  ~990 gói/s). Giãn cách 20 s → 300 s nếu vẫn hỏng (UAV tắt). Log: `journalctl -u wfb-watchdog -f`.
- **Kênh sóng gần bão hoà:** video UAV chiếm **11 Mbit/s trên sóng ở MCS 1 / 20 MHz** (PHY ~13 Mbit/s,
  FEC 8/12, ~980 gói/s) → ~90% thời gian phát. Hệ quả: ping GS→UAV qua tunnel mất 40–70%, RTT ~155 ms;
  gần như không còn dư địa cho nhiễu/tầm xa. **Cần chỉnh phía UAV**: giảm bitrate video (~4–5 Mbit/s)
  và/hoặc tăng `mcs_index` (2–3; SNR hiện ~25 dB ở gần), hoặc 40 MHz.
- Xem số liệu link: `wfb-cli gs` (cần terminal thật), hoặc JSON API cổng 8103.
- **Độ trễ (2026-10-11, người dùng thấy 0.6 s không có mục tiêu / 0.8 s có mục tiêu trên laptop):**
  đo phía Radxa trên luồng thật: jitterbuffer 30→**5 ms** (đã giảm; wfb-ng trả gói đúng thứ tự),
  giải mã ~7 ms, chờ ~2 ms, chép+vẽ ~22 ms, nén ~15 ms → **Radxa ~50–80 ms**. Gói toạ độ Pi→Radxa
  ~7 ms (đồng hồ hai máy NTP). Lúc đo, video ~7.7 Mbit/s trên sóng (người dùng đã giảm từ 11), ping
  tunnel 17–44 ms → hàng đợi phát UAV không còn là nguyên nhân. ⇒ ~0.5 s còn lại nằm ở **Pi**
  (camera/encoder/chương trình tracker; +0.2 s khi có mục tiêu gợi ý xử lý detect/SORT làm chậm luồng
  video trên Pi) **và/hoặc trình xem trên laptop** (VLC mặc định đệm 1000 ms). Chưa kiểm được phía Pi.
- `gs_overlay` log thêm "giải mã X ms" (đo FIFO: mppvideodec đóng dấu PTS mới cho khung ra nên không
  khớp PTS được; h264parse đẩy cả buffer list → đầu dò phải bắt BUFFER_LIST) và "chờ xử lý".

### Việc tiếp theo
1. Sửa nguồn cho card (vẫn sụt khi phát) rồi tăng lại `wifi_txpower`. Chỉnh bitrate/MCS video phía UAV (kênh gần bão hoà).
2. (XONG 2026-10-11) gs.key đã đặt, wfb-ng đã chạy, video + toạ độ thật đã lên.
3. (XONG 2026-10-11) Radxa có sẵn 192.168.50.1 trên Ethernet; laptop tự đặt 192.168.50.2/24 khi cắm thẳng.
4. Có thể giảm CPU sau này nếu RGA hoạt động (chép khung bằng phần cứng thay vì CPU).

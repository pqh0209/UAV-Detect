# UAV Tracking — Trạm mặt đất (Radxa Zero 3W)

Phần **trạm mặt đất (Ground Station, GS)** của hệ thống camera bám mục tiêu trên UAV.
UAV tự nhận diện và bám theo người / ô tô / xe máy / xe đạp, gửi **video** và **toạ độ mục tiêu**
xuống mặt đất qua **wfb-ng** (WiFi broadcast 5.8 GHz). Radxa Zero 3W nhận sóng, giải mã gói wfb-ng
và **chuyển tiếp nguyên vẹn** video + toạ độ sang laptop; laptop giải mã video và vẽ box mục tiêu.

> Trạng thái (2026-10-11): **đã chạy với UAV thật** — video 1080p30 + toạ độ 30 Hz tới laptop,
> link ổn định ở khoảng cách gần (RSSI ≈ −48 dBm, 0 mất gói). Chưa thử tầm xa.

---

## 1. Toàn cảnh hệ thống

```
┌──────────── UAV (Raspberry Pi 4B — repo khác) ────────────┐
│ Camera IMX708 ─┬─ 1920x1080@30 → H.264 phần cứng → RTP ────┼──┐
│                └─ 300x300 → Coral TPU (SSD MobileNet) →     │  │ wfb-ng, kênh 165 (5825 MHz, 20 MHz)
│                   SORT tracking → JSON toạ độ (tunnel) ─────┼──┤ card RTL8812AU
└────────────────────────────────────────────────────────────┘  │
                                                                 ▼
┌──────────── TRẠM MẶT ĐẤT (Radxa Zero 3W — repo này) ─────────────────────────┐
│ card RTL8812EU (monitor) → wfb-ng profile gs                                   │
│     ├─ video  : RTP H.264 → 127.0.0.1:5600 ─┐                                  │
│     └─ tunnel : gs-wfb 10.5.0.1 ← JSON :5700 ┤→ gs_forward.py ──Ethernet/LAN──┼─┐
│ wfb_watchdog.py: theo dõi link, tự phục hồi card/driver                        │ │
└────────────────────────────────────────────────────────────────────────────────┘ │
                                                                                    ▼
┌──────────── LAPTOP (app riêng — repo khác) ────┐
│ video UDP :5600 → giải mã (độ trễ thấp)          │
│ toạ độ UDP :5700 → vẽ box, HUD                   │
└──────────────────────────────────────────────────┘
```

**Vì sao laptop vẽ box chứ không phải Radxa?** Ban đầu Radxa giải mã → vẽ box → nén lại → phát RTSP
(xem mục 8). Cách đó tốn ~60–70 ms và ~1.6 nhân CPU trên Radxa, còn VLC trên laptop thêm ~300–400 ms
bộ đệm. Chuyển sang: Radxa chỉ chuyển tiếp (< 1 ms), laptop dùng app riêng giải mã + vẽ.

---

## 2. Phần cứng trạm mặt đất

| Thiết bị | Chi tiết |
|---|---|
| Máy tính | **Radxa Zero 3W** (Rockchip RK3566, 4× Cortex-A55, RAM 4 GB, eMMC 29 GB, WiFi onboard AIC8800) |
| Card wfb-ng | **RTL8812EU** (USB ID `0bda:a81a`), USB 2.0, nối vào **cổng USB-C OTG** bằng 3 dây D+, D−, GND |
| Nguồn | 5V từ cục sạc điện thoại → **chân GPIO 5V/GND** của board; 5V/GND của card nối chung nguồn này |
| Mạng | Bộ chuyển USB-C → Ethernet **RTL8153** ở **cổng USB-C 3.0** — đường SSH và đường gửi sang laptop |
| WiFi onboard | AIC8800 (`wlan0`), nối mạng nhà — đường dự phòng |

⚠ **Nguồn hiện chưa đủ khoẻ** (mục 7). Khuyến nghị: nguồn 5V ≥ 3 A, dây to và ngắn, tụ 470–1000 µF
sát chân 5V của card; dây D+/D− ngắn, xoắn đôi (hoặc dùng cáp USB thật).

---

## 3. Nội dung repo

| Đường dẫn | Vai trò | Cài vào / chạy ở |
|---|---|---|
| `gs/gs_forward.py` | Chuyển tiếp nguyên vẹn RTP video và JSON toạ độ tới máy xem (mục 5.1) | service `uav-gs-forward` |
| `gs/wfb_watchdog.py` | Giám sát link wfb-ng, tự reset USB / nạp lại driver (mục 5.2) | service `wfb-watchdog` |
| `config/wifibroadcast.cfg` | Cấu hình wfb-ng phía trạm (kênh, vùng, công suất, link_domain) | `/etc/wifibroadcast.cfg` |
| `config/8812eu.conf` | Tham số module driver RTL8812EU | `/etc/modprobe.d/8812eu.conf` |
| `config/99-wfb-ng-unmanaged.conf` | NetworkManager không đụng vào card wfb-ng | `/etc/NetworkManager/conf.d/` |
| `config/uav-gs-forward.default` | Mẫu tham số máy xem (`FWD_ARGS`) | `/etc/default/uav-gs-forward` |
| `config/uav-gs-forward.service` | systemd unit cho `gs_forward.py` (User=radxa) | `/etc/systemd/system/` |
| `config/wfb-watchdog.service` | systemd unit cho `wfb_watchdog.py` (root) | `/etc/systemd/system/` |
| `CLAUDE.md` | Ghi chú làm việc cho Claude Code (quy tắc, lệnh kiểm tra, bẫy) | — |

Chỉ dùng thư viện chuẩn Python 3.9 (không cần pip). **Không có khoá nào trong repo** (`.gitignore` chặn `*.key`).

---

## 4. Dựng trạm mặt đất từ đầu (đã làm trên máy hiện tại)

Hệ điều hành: **Radxa OS (Debian 11 bullseye, arm64)**, kernel vendor `5.10.160-26-rk356x`, U-Boot
`latest-2023.10`. Headers kernel và `dkms` có sẵn trong image.

### 4.1. Khởi động từ eMMC — KHÔNG cắm thẻ SD chứa cùng image
Board từng cắm cả eMMC lẫn thẻ SD được ghi **cùng một image** (UUID phân vùng trùng hệt). U-Boot ưu
tiên thẻ SD (`boot_targets=mmc1 mmc0`, `mmc1` = SD) nên đọc `extlinux.conf` **trên SD**, còn kernel
mount root theo UUID và trúng eMMC → mọi thay đổi `/boot` trên eMMC (overlay, cmdline) **không có tác
dụng** dù không báo lỗi. Đã chứng minh bằng cách thêm dấu vào cmdline eMMC (không xuất hiện trong
`/proc/cmdline`), rút SD thì xuất hiện. → **Đã rút thẻ SD.**

### 4.2. Cổng USB-C OTG sang chế độ host
Card nối bằng 3 dây (không có chân CC) nên cổng OTG không tự chuyển sang host. Bật overlay có sẵn:
```bash
sudo mv /boot/dtbo/rk3568-dwc3-host.dtbo.disabled /boot/dtbo/rk3568-dwc3-host.dtbo
sudo u-boot-update          # extlinux.conf có dòng: fdtoverlays /boot/dtbo/rk3568-dwc3-host.dtbo
sudo systemctl disable radxa-usbnet radxa-adbd    # USB gadget của Radxa, giờ luôn lỗi
sudo reboot
```
Kiểm tra: `tr -d '\0' < /proc/device-tree/usbdrd/dwc3@fcc00000/dr_mode` → `host`; `lsusb` thấy `0bda:a81a`.

### 4.3. Driver RTL8812EU (svpcom/rtl8812eu, cài DKMS thủ công)
```bash
git clone https://github.com/svpcom/rtl8812eu.git && cd rtl8812eu
git checkout 48e6e44                     # bản đã dùng
sed -i 's/CONFIG_PLATFORM_I386_PC = y/CONFIG_PLATFORM_I386_PC = n/; s/CONFIG_PLATFORM_ARM64_RPI = n/CONFIG_PLATFORM_ARM64_RPI = y/' Makefile
V=5.15.0.1~20230815; sudo mkdir -p /usr/src/realtek-rtl88x2eu-$V && sudo cp -a $(ls -A | grep -v '^.git$') /usr/src/realtek-rtl88x2eu-$V/
sudo dkms add -m realtek-rtl88x2eu -v $V && sudo dkms build -m realtek-rtl88x2eu -v $V && sudo dkms install -m realtek-rtl88x2eu -v $V   # ~8 phút
sudo install -m 644 config/8812eu.conf /etc/modprobe.d/
```
⚠ **Không chạy `dkms-install.sh`** của driver: nó ghi vào `/etc/sysctl.conf` để tắt IPv6 toàn hệ thống.
Kết quả: module `8812eu`, tên driver `rtl88x2eu`, interface `wlx145d345c6f93`.

### 4.4. wfb-ng 25.01 + khoá
```bash
curl -fsSL https://apt.wfb-ng.org/public.asc | sudo gpg --dearmor -o /usr/share/keyrings/wfb-ng.gpg
echo "deb [signed-by=/usr/share/keyrings/wfb-ng.gpg] https://apt.wfb-ng.org/ bullseye release-25.01" | sudo tee /etc/apt/sources.list.d/wfb-ng.list
sudo apt update && sudo apt install wfb-ng          # bản đang dùng: 25.1.117 (giống UAV)
sudo install -m 644 config/wifibroadcast.cfg /etc/wifibroadcast.cfg
sudo install -m 644 config/99-wfb-ng-unmanaged.conf /etc/NetworkManager/conf.d/ && sudo systemctl reload NetworkManager
```
- Gói wfb-ng không tự tạo khoá (postinst chỉ `daemon-reload`). Nó cài `/etc/sysctl.d/98-wifibroadcast.conf`
  (`rmem_max=5000000`).
- ⚠ **Không chạy `install_gs.sh`** của wfb-ng (đặt vùng BO, ghi đè motd/dhcpcd...).
- **Khoá:** chép `gs.key` từ UAV (cặp với `/etc/drone.key` của UAV, cùng một lần `wfb_keygen`) vào
  `/etc/gs.key`, quyền `root:root 600`. **Không tự tạo khoá mới** (sẽ không giải mã được).
  Đối chiếu cặp khoá mà không lộ khoá bí mật — trên mỗi máy in khoá công khai suy ra:
  ```bash
  sudo python3 -c "import ctypes;d=open('/etc/gs.key','rb').read();n=ctypes.CDLL('libsodium.so.23');n.sodium_init();p=ctypes.create_string_buffer(32);n.crypto_scalarmult_base(p,d[:32]);print('GS pub:',p.raw.hex(),'| UAV pub:',d[32:].hex())"
  ```
  (trên UAV làm tương tự với `/etc/drone.key`: khoá công khai suy ra phải bằng "UAV pub" ở trên).
  Cặp hiện tại: UAV `c50df529…5a5a`, GS `5401fb15…1f51`.

### 4.5. Mạng Ethernet
DHCP khi cắm router + IP tĩnh phụ để laptop cắm dây thẳng:
```bash
sudo nmcli con mod "Wired connection 1" +ipv4.addresses 192.168.50.1/24 ipv4.dhcp-timeout infinity
sudo nmcli device reapply enx00e04c680009
```
Laptop cắm thẳng: đặt `192.168.50.2/24`, **không gateway, không DNS** (để Internet vẫn đi WiFi).

### 4.6. Cài service của repo
```bash
cd ~/uav-project
sudo install -m 644 config/uav-gs-forward.default /etc/default/uav-gs-forward   # rồi điền máy xem (mục 6)
sudo install -m 644 config/uav-gs-forward.service config/wfb-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wifibroadcast.service wifibroadcast@gs uav-gs-forward wfb-watchdog
```
File service trỏ tới `/home/radxa/uav-project/gs/` và User `radxa`; để repo chỗ khác thì sửa `ExecStart`/`User`.

---

## 5. Thành phần phần mềm

### 5.1. `gs_forward.py` — chuyển tiếp
| Vào | Ra (mỗi máy xem) |
|---|---|
| UDP `127.0.0.1:5600` — RTP H.264 từ wfb-ng (`gs_video`) | UDP `<máy xem>:5600` (`--video-out-port`) |
| UDP `0.0.0.0:5700` — JSON toạ độ từ UAV qua tunnel | UDP `<máy xem>:5700` (`--telem-out-port`) |
| UDP `0.0.0.0:5599` — gói "đăng ký" từ máy xem | — |

- **Máy xem** = các IP `--dest` cố định **cộng** các máy **tự đăng ký**: máy xem gửi 1 gói UDP bất kỳ
  tới Radxa cổng 5599 mỗi ~1 s → được gửi tới IP nguồn của gói đó; quá 5 s không gửi thì hết hạn.
  Nhờ vậy laptop đổi IP (DHCP, cắm dây thẳng) không phải sửa gì trên Radxa.
- Không giải mã, không sửa gói → thêm < 1 ms trễ. CPU ~25% một nhân (~6% tổng).
- Gửi lỗi (máy xem tắt, rút dây) thì bỏ gói, không dừng. Chạy trùng 2 bản → bản sau báo cổng bận và thoát.
- Log mỗi 60 s: số gói/s video và toạ độ, đang gửi tới đâu, số lỗi.

### 5.2. `wfb_watchdog.py` — tự phục hồi link
Đọc JSON API của wfb-ng (`127.0.0.1:8103`) mỗi 2 s, kiểm tra card qua `/sys/class/net/*/device/driver`.

| Phát hiện | Hành động |
|---|---|
| Card mất khỏi USB > 6 s | unbind/bind bộ điều khiển USB OTG `fcc00000.dwc3` (card enumerate lại) |
| wfb-ng chạy nhưng **0 gói video** > 10 s | dừng wfb-ng → `rmmod 8812eu` → `modprobe 8812eu` → chờ ~4 s → chạy wfb-ng |
| Tunnel có gói nhưng **không giải mã được gói nào** > 15 s | như trên |

Khoảng cách giữa các lần phục hồi tăng dần 20 s → 300 s (UAV tắt cũng cho 0 gói); link khoẻ lại thì về 20 s.
Đã thử: đổi card sang kênh sai, tháo USB bằng phần mềm — cả hai lần link tự lên lại (~1000 gói/s).

### 5.3. wfb-ng (gói hệ thống, profile `gs`)
Cấu hình `config/wifibroadcast.cfg`: kênh 165, vùng `VN`, `wifi_txpower = 500` (5 dBm),
`link_domain = "default"`; còn lại theo mặc định wfb-ng 25.01: video FEC 8/12, MCS 1, 20 MHz, video ra
`127.0.0.1:5600`, tunnel `gs-wfb` 10.5.0.1/24, MAVLink ra `127.0.0.1:14550` (chưa dùng).
Vùng VN cho phép cả kênh 165 lẫn kênh 44 mà WiFi onboard đang dùng (vùng BO sẽ làm rớt WiFi onboard).

---

## 6. Vận hành

| Việc | Lệnh |
|---|---|
| Trạng thái | `systemctl status wifibroadcast@gs uav-gs-forward wfb-watchdog` |
| Log | `journalctl -u uav-gs-forward -f` · `journalctl -u wfb-watchdog -f` · `journalctl -u wifibroadcast@gs -f` |
| Số liệu link (RSSI, mất gói, FEC) | `wfb-cli gs` (cần terminal thật) hoặc JSON API `127.0.0.1:8103` |
| Đặt máy xem cố định | sửa `FWD_ARGS` trong `/etc/default/uav-gs-forward` (vd. `--dest 192.168.1.73`) → `sudo systemctl restart uav-gs-forward` |
| Đổi cổng nhận trên laptop | thêm `--video-out-port N --telem-out-port M` vào `FWD_ARGS` |
| Khởi động lại link | `sudo systemctl restart wifibroadcast@gs` (nếu sau đó 0 gói, watchdog tự nạp lại driver trong ~10 s) |
| Xem nhanh không cần app | VLC/ffplay mở file SDP: `m=video 5600 RTP/AVP 96`, `a=rtpmap:96 H264/90000` (không có box) |

**Xử lý sự cố**

| Triệu chứng | Nguyên nhân thường gặp | Cách xử lý |
|---|---|---|
| Laptop không có hình | IP laptop đổi; firewall Windows chặn; app nghe sai cổng | Dùng tự đăng ký :5599 hoặc sửa `--dest`; cho phép app qua firewall; kiểm tra cổng 5600/5700 |
| `video 0 gói/s` trong log forward | wfb-ng không thu: UAV tắt / card chết phần thu / card rớt USB | Xem `journalctl -u wfb-watchdog`; `lsusb` có `0bda:a81a` không; `dmesg` có `error -71` không |
| Có video, không có toạ độ | Tunnel kẹt khoá phiên, hoặc chương trình tracker trên UAV dừng | Watchdog tự xử lý tunnel kẹt sau 15 s; nếu không, kiểm tra service `uav-tracker` trên UAV |
| `lsusb` không thấy card | Sụt nguồn / dây D+ D− lỏng | Watchdog tự reset USB; nếu vẫn mất: rút cắm nguồn card, kiểm tra dây và nguồn |
| SSH qua Ethernet mất khi wfb-ng khởi động | Sụt nguồn làm cả bộ Ethernet USB rớt | Vào qua WiFi onboard (`radxa@192.168.1.123`); sửa nguồn |

---

## 7. Vấn đề còn tồn tại

1. **Nguồn 5V yếu:** đúng lúc wfb-ng bật card, cả card lẫn bộ Ethernet USB từng rớt USB cùng lúc; card có
   lần không tự lên lại. Giảm nhẹ bằng `wifi_txpower = 500` (trạm chủ yếu thu) + watchdog. Cần sửa phần cứng,
   sau đó tăng lại công suất phát (cần cho kênh lệnh lên UAV).
2. **Driver 8812eu "chết phần thu"** ngẫu nhiên khi wfb-ng cấu hình lại card (dmesg: `rtw_mlmeext_disconnect`,
   `buffer_cnt = -1`, `cmd queue crashed`). Watchdog xử lý; gốc rễ là lỗi driver.
3. **Kênh sóng ít dư địa:** lúc đầu video chiếm 11 Mbit/s trên sóng ở MCS 1 (~90% thời gian phát, ping tunnel
   RTT ~155 ms, mất 40–70%); đã giảm còn ~7.7 Mbit/s (ping 17–44 ms). Muốn tầm xa hơn: giảm bitrate hoặc
   tăng MCS phía UAV.
4. **Chưa có kênh lệnh chọn mục tiêu** từ laptop lên UAV (tunnel đã sẵn: UAV 10.5.0.2; phía UAV chưa có code nhận).

---

## 8. Nhật ký phát triển (2026-10-08 → 2026-10-11)

1. Khảo sát máy; phát hiện boot lai SD/eMMC (mục 4.1) → rút SD.
2. Bật host cho cổng OTG; card RTL8812EU được nhận. Lúc đầu card rớt USB liên tục (dây 3 sợi, nguồn) — người dùng sửa đấu nối.
3. Build driver svpcom/rtl8812eu bằng DKMS; cài wfb-ng 25.01; cấu hình kênh 165 / VN; đặt và đối chiếu `gs.key`.
4. **Giai đoạn 1 — vẽ box trên Radxa** (`gs_overlay.py`, nay đã bỏ): mppvideodec → chép khung → vẽ box/HUD
   bằng numpy + cairo → mpph264enc → RTSP/UDP. Đạt 30 fps, trễ thêm ~60–70 ms, ~1.6 nhân CPU. Gặp và vượt
   qua nhiều lỗi của bộ GStreamer Rockchip trên RK3566 (ghi trong `CLAUDE.md`).
5. Lên sóng thật: video + toạ độ tới laptop. Phát hiện sụt nguồn khi phát → giảm txpower; driver chết phần thu
   → viết `wfb_watchdog.py`.
6. Đo độ trễ: Pi ~130 ms, sóng ~10 ms, Radxa ~60–70 ms (khi vẽ box), VLC thêm ~300–400 ms.
7. **Giai đoạn 2 — laptop vẽ box:** thay `gs_overlay.py` bằng `gs_forward.py` (chuyển tiếp nguyên vẹn,
   tự đăng ký máy xem). Dọn repo chỉ còn file hệ thống đang dùng.

Code giai đoạn 1 và công cụ thử nghiệm (giả lập UAV, đo trễ) được cất ở `~/uav-gs-archive/` trên Radxa, ngoài repo.

---

## 9. Ngoài repo nhưng hệ thống cần (trên máy hiện tại)

| Thứ | Vị trí |
|---|---|
| Khoá wfb-ng | `/etc/gs.key` (root 600) — **không bao giờ đưa lên git** |
| Overlay host cho OTG | `/boot/dtbo/rk3568-dwc3-host.dtbo` + dòng `fdtoverlays` trong `/boot/extlinux/extlinux.conf` |
| Mã nguồn driver DKMS | `/usr/src/realtek-rtl88x2eu-5.15.0.1~20230815/` |
| Repo apt wfb-ng | `/etc/apt/sources.list.d/wfb-ng.list`, `/usr/share/keyrings/wfb-ng.gpg` |
| IP tĩnh phụ Ethernet | NetworkManager connection "Wired connection 1" |
| sudo không mật khẩu | `/etc/sudoers.d/010_radxa-nopasswd` (người dùng tự tạo để Claude Code thao tác) |

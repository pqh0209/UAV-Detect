# CLAUDE.md — Trạm mặt đất UAV tracking (Radxa Zero 3W)

Ghi chú làm việc cho Claude Code trên máy này. Mô tả đầy đủ cho người (kiến trúc, các bước dựng máy,
vận hành, nhật ký): **`README.md`** — đọc nó trước khi làm việc lớn. File này tập trung vào: hệ thống
đang ở trạng thái nào, quy tắc thao tác an toàn, cách kiểm tra nhanh, và các bẫy đã mất công tìm ra.

## Người dùng
- Nói tiếng Việt → trả lời tiếng Việt. Làm việc qua SSH / VS Code Remote-SSH, không có màn hình gắn Radxa.
- Khi người dùng bảo "làm đi" thì tự làm trọn vẹn; chỉ hỏi khi cần quyết định của họ, mật khẩu hoặc thao
  tác tay (cắm/rút, nguồn). `sudo` không cần mật khẩu.
- Có các phiên Claude Code khác: trên **UAV (Pi 4B)** và trên **laptop (Windows, app xem video vẽ box)**.
  Chúng có thể sửa file trên máy này qua SSH → kiểm tra file thay đổi trước khi ghi đè/xoá.

## Dự án và trạng thái (2026-10-11)
UAV tự detect + bám mục tiêu (person/car/motorcycle/bicycle), gửi video + toạ độ qua wfb-ng. Radxa = trạm
mặt đất: nhận wfb-ng, **chuyển tiếp nguyên vẹn** video RTP + JSON toạ độ sang laptop; **laptop giải mã và vẽ box**.
Đã chạy với UAV thật: 1080p30, toạ độ 30 Hz, RSSI ≈ −48 dBm ở gần, 0 mất gói. Chưa thử tầm xa.

```
UAV Pi 4B ──wfb-ng ch165──▶ wlx145d345c6f93 (RTL8812EU) ─ wfb-ng gs ─┬─ RTP → 127.0.0.1:5600 ─┐
                                                                      └─ tunnel gs-wfb 10.5.0.1 ← JSON :5700 ┤
                     gs_forward.py ── Ethernet ──▶ laptop :5600 (video), :5700 (JSON) ◀────────┘
                     (máy xem: --dest cố định + tự đăng ký qua UDP :5599)
```

### Service (đều enabled)
| Service | Chạy | Ghi chú |
|---|---|---|
| `wifibroadcast@gs` (+ `wifibroadcast.service`) | gói wfb-ng 25.1.117 | cấu hình `/etc/wifibroadcast.cfg` (= `config/wifibroadcast.cfg`) |
| `uav-gs-forward` | `gs/gs_forward.py $FWD_ARGS`, User=radxa | `/etc/default/uav-gs-forward`: hiện `--dest 192.168.1.73` (laptop, DHCP) |
| `wfb-watchdog` | `gs/wfb_watchdog.py`, root | tự reset USB / nạp lại driver |

Đã gỡ: `uav-gs-overlay` (giai đoạn Radxa vẽ box). Code cũ + công cụ thử (giả lập UAV, đo trễ) ở
`~/uav-gs-archive/` (ngoài repo, không chạy).

### Cổng mạng
| Cổng | Hướng | Nội dung |
|---|---|---|
| 127.0.0.1:5600 UDP | wfb-ng → gs_forward | RTP H.264 pt=96 (1080p30, ~5–7.5 Mbit/s, SPS/PPS lặp, IDR mỗi 30 khung, không B-frame) |
| 0.0.0.0:5700 UDP | UAV (10.5.0.2, qua tunnel) → gs_forward | JSON `uav_track` ~30 gói/s |
| 0.0.0.0:5599 UDP | laptop → gs_forward | gói bất kỳ mỗi ~1 s = đăng ký nhận (hết hạn 5 s) |
| laptop:5600 / :5700 UDP | gs_forward → laptop | video / JSON (đổi bằng `--video-out-port`, `--telem-out-port`) |
| 127.0.0.1:8103 TCP | wfb-ng | JSON API thống kê (watchdog đọc); 8003 cho `wfb-cli gs` |
| 127.0.0.1:14550 UDP | wfb-ng | MAVLink (chưa dùng) |

Lưu ý: một phiên khác từng cấu hình cho laptop nhận video thô :5601 và JSON :5602 (tới 192.168.50.2).
Nếu laptop không nhận được, hỏi người dùng app đang nghe cổng nào.

### Giao tiếp với UAV (phải khớp)
Kênh 165 (5825 MHz, 20 MHz), `link_domain="default"`, `/etc/gs.key` ↔ `drone.key` của UAV (cùng một lần
`wfb_keygen`; khoá công khai đã đối chiếu: UAV `c50df529…5a5a`, GS `5401fb15…1f51`). Tunnel UAV 10.5.0.2 ↔ GS 10.5.0.1.
JSON toạ độ:
```json
{"type":"uav_track","seq":481,"sensor_ts_ns":4596107447000,"wall":1791451053.202,"frame":[1920,1080],
 "detector":"ok","target":{"id":11,"cls":"person","score":0.73,"box":[0.014,0.0,0.8724,0.9887],
 "center":[0.4432,0.4943],"err":[-0.1136,-0.0113],"lost_frames":2},
 "tracks":[{"id":11,"cls":"person","score":0.73,"box":[...],"lost_frames":2}]}
```
`box` x1,y1,x2,y2 chuẩn hoá 0..1 theo khung video; `err` lệch tâm −1..1 (+ phải/dưới, cho gimbal);
`lost_frames>0` = vị trí dự đoán Kalman; `target` có thể null; `detector` ok|retrying|degraded.
UAV tự chọn mục tiêu; **chưa có** lệnh chọn mục tiêu từ trạm (việc tiếp theo).

## Máy này
| Mục | Giá trị |
|---|---|
| Board | Radxa ZERO 3 (Zero 3W, RK3566, 4 GB RAM), eMMC 29 GB, **không thẻ SD** |
| OS | Radxa OS Debian 11 bullseye arm64, kernel `5.10.160-26-rk356x`, U-Boot `latest-2023.10` |
| Ethernet | `enx00e04c680009` (USB RTL8153, cổng USB3): DHCP 192.168.1.75 + tĩnh 192.168.50.1/24 (NM "Wired connection 1", `dhcp-timeout infinity`). **Đường SSH chính** |
| WiFi onboard | `wlan0` AIC8800, 192.168.1.123, kênh 44 — **đường SSH dự phòng** |
| Card wfb | RTL8812EU `0bda:a81a` ở cổng OTG (host nhờ overlay `rk3568-dwc3-host.dtbo`; bộ điều khiển `fcc00000.dwc3`, bus 5), interface `wlx145d345c6f93`, module `8812eu` (DKMS `realtek-rtl88x2eu/5.15.0.1~20230815`, svpcom/rtl8812eu `48e6e44`) |
| Nguồn | cục sạc điện thoại → chân GPIO 5V; card dùng chung nguồn này (yếu, xem bẫy) |

## Kiểm tra nhanh
```bash
systemctl is-active wifibroadcast@gs uav-gs-forward wfb-watchdog
journalctl -u uav-gs-forward -n 5 -o cat          # "video N gói/s, toạ độ 30 gói/s → <laptop>"
journalctl -u wfb-watchdog -n 5 -o cat
lsusb | grep a81a ; sudo dmesg | grep -cE 'usb 5-1: USB disconnect'    # card còn không / rớt mấy lần
grep -v '^#' /etc/default/uav-gs-forward
```
Số liệu wfb-ng không cần terminal: đọc JSON API `127.0.0.1:8103` (dòng JSON; `type` rx/tx, `id` "video rx" /
"tunnel rx"; `packets.all/dec_ok/dec_err/lost/fec_rec/out` = [trong 1 s, tổng]; `rx_ant_stats` có rssi/snr).

## Quy tắc thao tác (rút từ sự cố thật)
- **Không tự `iw`/`ip link` cấu hình card wfb** khi wfb-ng không chạy rồi bật wfb-ng → dễ làm driver chết
  phần thu. Muốn khởi động lại link: `sudo systemctl restart wifibroadcast@gs`; nếu 0 gói, quy trình chắc
  chắn: stop wfb → `rmmod 8812eu` → `modprobe 8812eu` → chờ ~4 s → start wfb (watchdog tự làm sau ~10 s).
- **Không chạy** `dkms-install.sh` (svpcom; tắt IPv6) và `install_gs.sh` (wfb-ng; đặt vùng BO, ghi đè hệ thống).
- **Không tạo khoá mới** bằng `wfb_keygen`; không in/đưa `/etc/gs.key` lên git hay vào hội thoại (chỉ in khoá công khai).
- **Không đổi `wifi_region` sang BO**: `iw reg set` áp dụng cho mọi card → có thể làm rớt `wlan0`. VN là đúng.
- Thay đổi NetworkManager trên `enx00e04c680009` có thể cắt SSH đang dùng → dùng `nmcli device reapply`,
  và nhớ đường dự phòng `wlan0`.
- Sửa `/boot` chỉ có tác dụng khi KHÔNG có thẻ SD cùng image (xem bẫy khởi động). Sau khi sửa: `sudo u-boot-update`.
- Khởi động wfb-ng / bật phát sóng có thể làm sụt nguồn → Ethernet USB rớt vài giây. Chạy lệnh dài thì cẩn thận.

## Bẫy đã gặp

**Khởi động / USB / nguồn**
- Thẻ SD cùng image với eMMC (UUID trùng): U-Boot `boot_targets=mmc1 mmc0` (mmc1 = SD `fe2b0000`) đọc
  extlinux.conf trên SD, kernel mount root eMMC → sửa `/boot` eMMC vô tác dụng, không báo lỗi. Chứng minh
  bằng dấu thêm vào `/etc/kernel/cmdline` + `u-boot-update` rồi xem `/proc/cmdline`. Đã rút SD.
- Card nối OTG bằng 3 dây (D+, D−, GND; 5V lấy từ nguồn chung) → không có CC → cổng ở chế độ device
  (Radxa OS mặc định dùng OTG làm USB gadget `radxa-usbnet`/`radxa-adbd`) → phải bật overlay host. Đã
  disable 2 service gadget.
- **Nguồn 5V sụt khi card phát**: đúng giây wfb-ng khởi động, cả card (bus 5) lẫn Ethernet RTL8153 (bus 8)
  rớt USB (`error -71`, `disabled by hub (EMI?)`, `Device not responding to setup address`). Giảm nhẹ:
  `wifi_txpower = 500` (5 dBm) → 3 phút không rớt (trước ~1 phút). Gốc rễ phần cứng.

**Driver / wfb-ng**
- 8812eu: `wifi_txpower` = dBm×100 (8812au thì số âm). Cần `rtw_tx_pwr_by_rate=0 rtw_tx_pwr_lmt_enable=0`
  (`config/8812eu.conf`) để wfb-ng đặt được công suất.
- Driver "chết phần thu" khi interface bị cấu hình lại (dmesg `WARNING ... rtw_mlmeext_disconnect`,
  `buffer_cnt = -1`, `cmd queue crashed`): monitor/kênh đúng nhưng 0 gói, sniff raw socket cũng 0.
  Xảy ra ngẫu nhiên, cả với `systemctl restart wifibroadcast@gs`. Chữa: nạp lại module (quy tắc ở trên).
- Card rớt USB rồi không enumerate lại ("unable to enumerate USB device") → unbind/bind
  `/sys/bus/platform/drivers/dwc3` với `fcc00000.dwc3` làm card lên lại (không cần rút cắm).
- Sau reset USB, tunnel nhận gói đủ nhưng 100% `dec_err`, `session: None` (không có khoá phiên) → mất toạ
  độ dù video chạy. Chữa như chết phần thu. Vài chục `Unable to decrypt packet` ngay sau khi wfb-ng khởi
  động là BÌNH THƯỜNG (chưa nhận gói khoá phiên).
- Khi card mất, `WFB_NICS=$(wfb-nics)` rỗng → `wfb-server: error: argument --wlans: expected at least one
  argument`, systemd khởi động lại mỗi 5 s cho tới khi card lên.
- MAC 57:42:xx:xx:xx:xx trên kênh 165 = gói wfb-ng ("WB"); sniff thấy là UAV đang phát.
- Kênh sóng: video FEC 8/12 ở MCS 1/20 MHz (PHY ~13 Mbit/s). Từng 11 Mbit/s trên sóng (~90% airtime →
  ping tunnel RTT 155 ms, mất 40–70%); giờ ~7.7 Mbit/s → ping 17–44 ms.

**Độ trễ (đo 2026-10-11)**: Pi tự đo ~130 ms (cảm biến → gửi gói); sóng + wfb ~10 ms (gói toạ độ
Pi→Radxa ~7 ms, đồng hồ hai máy NTP); gs_forward < 1 ms; WiFi laptop dao động 2–100 ms; VLC thêm
~300–400 ms (đệm, đồng bộ đồng hồ, giải mã đa luồng theo khung, tự nới bộ đệm khi mạng dao động) → lý do
laptop dùng app riêng. Cắm dây thẳng laptop (192.168.50.x) bỏ được dao động WiFi.

**Giai đoạn Radxa vẽ box (đã bỏ; code `~/uav-gs-archive/gs_overlay.py`) — bẫy GStreamer Rockchip trên RK3566,
cần nếu sau này lại xử lý video trên Radxa** (gstreamer1.0-rockchip1 1.14, librockchip-mpp 1.5.0, GStreamer 1.18.5):
- RGA hỏng (`RgaBlit fail: Invalid argument`, `rga_mm: failed to map buffer`; nghi RGA2 32-bit trên board
  4 GB) → mọi thứ cần RGA (đổi format/scale trong `mppvideodec`, chép đầu vào của `mpph264enc`) hỏng.
- `mpph264enc` với 1920x1080 tự "căn" 1088 và chép MỌI khung qua RGA → khung trống (xanh lá, ví dụ
  `videotestsrc ! mpph264enc`) hoặc chép CPU ~16 fps. Sửa: env `GST_MPP_ENC_UNALIGNED_VSTRIDE=1` + ghi
  khung vào buffer lấy từ pool mà encoder đề xuất (allocation query, `mppallocator`) → 30 fps, không RGA.
- `mpph264enc` không đánh dấu khung khoá cho GstVideoEncoder ("sync point: 0") → force-key-unit chỉ có
  tác dụng lần đầu; cờ DELTA_UNIT ở đầu ra encoder sai (đọc sau `h264parse` mới đúng). Ép IDR: đổi thuộc
  tính `gop` N↔N+1 lúc chạy (MPP mở GOP mới ngay khung kế, 5/5 lần).
- `mppvideodec` không cập nhật caps/GstVideoMeta khi luồng đổi độ phân giải → vỡ hình; phát hiện qua caps
  `h264parse` rồi TẠO LẠI pipeline (đưa pipeline về NULL rồi PLAYING thì kẹt PAUSED, không ra khung).
  `mppvideodec` đóng dấu PTS mới cho khung ra (không khớp PTS vào); không giữ khung (giải mã ~7 ms).
- Bộ nhớ ra của decoder / pool encoder KHÔNG cache: CPU đọc khung 1080p ~20–40 ms (3 luồng ~9 ms phần Y;
  NEON ld1 chậm hơn memcpy). Không vẽ thẳng lên buffer giải mã (là khung tham chiếu H.264 → box lem sang
  khung sau). Ghi thì nhanh (~4 ms/khung).
- PyGObject: đọc `mapinfo.data` chép cả khung mỗi lần (~40 ms) → map bằng ctypes `gst_buffer_map`
  (`hash(buf)` = con trỏ `GstBuffer*`). `textoverlay → mpph264enc` trong Python kẹt PAUSED. `appsrc` 1.18
  không tự bỏ khung cũ (RTSP bắt tay → dồn khung, trễ vài giây). `h264parse` đẩy buffer list (đầu dò phải
  bắt BUFFER_LIST). Duyệt `deque` bằng generator khi luồng khác append → "deque mutated during iteration".
- Kết quả giai đoạn đó: 30 fps, Radxa thêm ~60–70 ms, ~1.6 nhân CPU (phần lớn là đọc bộ nhớ không cache).

## Việc tiếp theo
1. Sửa nguồn cho card (≥ 3 A, dây to/ngắn, tụ sát card) rồi tăng lại `wifi_txpower` — cần cho kênh lệnh lên UAV.
2. Kênh lệnh chọn mục tiêu: laptop → Radxa → tunnel → UAV 10.5.0.2 (phía UAV chưa có code nhận).
3. Thử tầm xa; nếu thiếu dư địa sóng: giảm bitrate / tăng MCS phía UAV.
4. Sau này người dùng muốn làm firmware chung cho UAV + Radxa + laptop (chưa làm; sẽ yêu cầu riêng).

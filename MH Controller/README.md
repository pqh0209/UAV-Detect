# UAV Viewer — xem video UAV độ trễ thấp (Windows)

`uav_viewer.py` thay VLC để xem video từ trạm mặt đất Radxa. App tự tách gói RTP/H.264, giải mã bằng PyAV
(LOW_DELAY, không frame-threading), hiển thị bằng pygame và luôn chỉ vẽ khung **mới nhất**. App không giữ bộ đệm
và không đồng bộ theo đồng hồ như VLC.

## 1. Cài đặt

```
python --version                            # cần ≥ 3.9
python -m pip install -r requirements.txt
```

- Nếu `pygame` không cài được (Python quá mới, chưa có bản dựng sẵn) thì cài `python -m pip install pygame-ce`.
  Bản này dùng y hệt `pygame`.
- **Windows Firewall:** lần đầu chạy, Windows sẽ hỏi. Hãy chọn **Allow** cho `python.exe` ở mạng **Private**.
  Nếu lỡ chặn: Windows Security → Firewall & network protection → Allow an app through firewall → tick `python.exe`
  (Private). Không cho phép thì app không nhận được gói nào và chỉ hiện NO SIGNAL.
- Đóng VLC trước khi chạy, vì hai chương trình không nghe chung một cổng được.

## 2. Chạy

```
python uav_viewer.py               # video THÔ từ UAV (UDP 5600) + box VẼ TRÊN LAPTOP (toạ độ UDP 5700)
```

Radxa chạy `gs_forward.py` (service `uav-gs-forward`): chuyển tiếp nguyên gói video và toạ độ, không giải mã,
không vẽ, không nén lại, nên gần như không thêm độ trễ. App **tự đăng ký** với Radxa bằng cách gửi một gói UDP nhỏ
tới `192.168.50.1:5599` mỗi giây. Radxa gửi video về đúng IP đang đăng ký, nên laptop đổi IP cũng không phải sửa gì.

Box trên laptop vẽ giống hệt kiểu Radxa đang vẽ, nhưng nét theo độ phân giải màn hình:
- Mục tiêu đang thấy: box **xanh lá**. Mục tiêu đang dự đoán (`lost_frames > 0`): box **vàng**, nhãn có "(pred)".
- Các track khác: box **xanh lơ**.
- Tâm ngắm ở giữa khung. Góc trên trái có dòng `TELEM Hz | DET` và `TARGET #id lớp điểm err x/y`.
- Toạ độ cũ hơn 0,5 s thì không vẽ box nữa, góc trên trái hiện `NO TELEMETRY`.

| Tuỳ chọn | Ý nghĩa |
|---|---|
| `--port 5600` | Cổng nhận video (mặc định 5600) |
| `--track-port 5700` | Cổng nhận JSON toạ độ (mặc định 5700; `0` = tắt vẽ box) |
| `--gs 192.168.50.1` | IP Radxa để tự đăng ký nhận video (mặc định 192.168.50.1; `--gs ""` = tắt) |
| `--loss-mode auto` | Mặc định. Mất ít gói (≤ 20 gói một lần) thì vẫn giải mã và che lỗi: hình không đứng, có thể nhoè nhẹ một vùng tới khung IDR kế tiếp (≤ 1 s). Mất nhiều gói, luồng khởi động lại hoặc hàng đợi tràn thì bỏ khung chờ IDR. |
| `--loss-mode conceal` (= `--conceal`) | Luôn che lỗi, không bao giờ đứng hình |
| `--loss-mode idr` | Cách cũ: mất 1 gói cũng bỏ khung tới IDR kế tiếp (đứng hình tới ~1 s, không nhoè) |
| `--fullscreen` | Mở toàn màn hình |
| `--size 1600x900` | Cỡ cửa sổ ban đầu (mặc định khoảng 85% màn hình; bấm `F` để xem đúng 1920x1080, 1:1) |
| `--scale FAST_BILINEAR` | Co ảnh nhanh hơn (cho máy yếu) |
| `--threads 4` | Giải mã nhiều luồng kiểu SLICE. Chỉ có ích khi mỗi khung có nhiều slice. |
| `--hwaccel d3d11va` | Thử giải mã bằng GPU (PyAV ≥ 14). Chỉ dùng nếu đo thấy nhanh hơn. |
| `--print-stats` | In số liệu HUD ra console mỗi giây (để lưu log khi đo) |
| `--verbose` | Hiện log lỗi của FFmpeg |

## 3. Phím tắt

| Phím | Chức năng |
|---|---|
| `F` / `F11` / nháy đúp chuột | Bật/tắt toàn màn hình |
| `H` | Bật/tắt HUD |
| `B` | Bật/tắt box mục tiêu vẽ trên laptop |
| `C` | Đổi chế độ xử lý mất gói khi đang chạy: auto → conceal → idr |
| `Esc` / `Q` | Thoát |

## 4. HUD (góc dưới trái, cập nhật mỗi giây)

- `1920x1080 → 1280x720`: độ phân giải video → cỡ đang vẽ trên màn hình.
- `hiển thị` / `nhận` fps, bitrate (Mbit/s).
- `tuổi khung tb / max`: từ lúc nhận gói cuối của khung tới lúc vẽ xong lên màn hình. Đây là độ trễ của riêng app;
  mục tiêu ≤ 30–40 ms.
- `giải mã tb / max`: thời gian giải mã 1 khung. `giải mã xong → hiện`: thời gian chuyển màu, co ảnh và vẽ.
- `mất gói %` (trong 1 s / từ lúc mở app). `bỏ khung` là số khung bỏ do mất gói, chờ IDR hoặc hàng đợi tràn.
  `đè` là số khung giải mã xong nhưng bị khung mới hơn đè trước khi kịp vẽ. `lỗi giải mã` đếm khung decoder từ chối.
- `che lỗi`: số khung thiếu gói nhưng vẫn được giải mã (chế độ auto/conceal).
- `CHỜ IDR`: sau khi mất gói, app đang bỏ khung để chờ khung khoá.

Mất gói lẻ tẻ chủ yếu đến từ sóng UAV → Radxa. Log `wfb_rx` trên Radxa có lúc ghi "video rx: 3 packets lost", là
những gói FEC của wfb-ng không cứu được. Đường dây Radxa → laptop không mất gói.

Không có gói quá 1 s thì app hiện **NO SIGNAL** (không tự tắt); có gói lại là tự chạy tiếp. Đổi độ phân giải giữa
chừng (ví dụ 1600x900) app tự xử lý.

## 5. Đo độ trễ toàn tuyến

1. Mở đồng hồ bấm giờ có mili-giây trên màn hình laptop.
2. Quay camera UAV vào màn hình laptop, đặt cửa sổ app cạnh đồng hồ.
3. Chụp màn hình (`Win+Shift+S` hoặc `PrtSc`) nhiều lần. Hiệu giữa đồng hồ thật và đồng hồ trong video là độ trễ
   toàn tuyến.
4. So sánh với VLC (~500–600 ms). App với video thô qua dây, kỳ vọng khoảng 160 ms (Pi ~130 ms + sóng ~10 ms +
   app ~10–20 ms).

Màn hình 60 Hz cho sai số khoảng ±17 ms mỗi lần đo, nên hãy chụp ít nhất 5 lần rồi lấy trung bình. Con số
`tuổi khung` trên HUD cho biết riêng app chiếm bao nhiêu trong tổng độ trễ đó.

## 6. Thử không cần UAV

Cài ffmpeg (bản "essentials" ở gyan.dev, thêm thư mục `bin` vào PATH), rồi chạy lệnh sau ở một cửa sổ khác:

```
ffmpeg -re -f lavfi -i testsrc2=size=1920x1080:rate=30 -c:v libx264 -preset ultrafast -tune zerolatency -g 30 -bf 0 -b:v 8M -f rtp rtp://127.0.0.1:5600
```

Thử mất gói mà không cần clumsy: cho ffmpeg gửi tới cổng 5610, proxy bỏ ngẫu nhiên 3 % gói rồi chuyển sang 5600.

```
python udp_loss_proxy.py --listen 5610 --to 127.0.0.1:5600 --loss 3
ffmpeg ...(như trên)... -f rtp rtp://127.0.0.1:5610
```

## 7. Xem qua dây Ethernet (cấu hình hiện tại)

| Máy | IP |
|---|---|
| Radxa (cổng USB-Ethernet) | 192.168.50.1 |
| Laptop (card Ethernet, đặt tĩnh 192.168.50.2/24, không gateway/DNS) | 192.168.50.2 |

| Trên Radxa | Giá trị |
|---|---|
| Service đang chạy | `uav-gs-forward` (`gs_forward.py`). `uav-gs-overlay` (vẽ box trên Radxa) đã tắt. |
| Gửi đi | video → laptop:5600, toạ độ → laptop:5700, tới các máy đã đăng ký qua cổng 5599 và các IP `--dest` |
| Cấu hình | `/etc/default/uav-gs-forward`, biến `FWD_ARGS` |

- Dây cắm thẳng là mạng "Unidentified network" (Windows xếp vào **Public**). Firewall phải cho phép `uav_viewer.exe`
  trên Public; hiện đã cho phép.
- Nhờ tự đăng ký, không cần sửa `--dest` trên Radxa khi IP laptop đổi. `--dest` chỉ cần cho máy xem không tự đăng ký
  được (VLC, ffplay).

## 7b. Log chẩn đoán (khi bị mất hình / giật)

Mỗi lần chạy, app ghi một file log vào thư mục `logs\` cạnh app (với exe là `dist\logs\`; giữ 30 file mới nhất).
Mỗi dòng có giờ đến mili-giây, nên khi thấy mất hình chỉ cần nhớ giờ rồi mở log ra tra.

| Dòng log | Ý nghĩa |
|---|---|
| `[5600] nhận ... khung/s ... lặng_max ... tới_muộn_max ...` | Số liệu mỗi giây. `lặng_max` là khoảng trống dài nhất giữa 2 gói; `tới_muộn_max` là độ trễ của khung so với nhịp phát. |
| `ĐỨNG HÌNH X ms → nguyên nhân` | Mỗi lần hình đứng quá 250 ms, kèm nguyên nhân chính: không có gói tới / mất gói → chờ IDR / lỗi giải mã / hàng đợi tràn / nguồn phát gửi thưa / UI bị chặn. |
| `IM LẶNG`, `MẤT N gói`, `RESET luồng`, `NO SIGNAL` | Sự kiện mạng |
| `BẮT ĐẦU CHỜ IDR` / `HẾT CHỜ IDR` | Thời gian hình đứng do bỏ khung sau khi mất gói |

`--monitor-port PORT` nghe thêm một cổng khác chỉ để ghi log so sánh, không hiển thị.

Mất gói trên luồng thô chủ yếu đến từ sóng UAV → Radxa. Đối chiếu với log `wfb_rx` trên Radxa
(`journalctl | grep 'packets lost'`).

Các tham số khác: `--log FILE` để ghi vào file chỉ định, `--no-log` để tắt ghi log.

## 8. File .exe (không cần cài Python)

Bản dựng sẵn nằm trong thư mục `dist\` của máy đã dựng. Repo git không kèm file exe; muốn có exe thì dựng bằng
lệnh ở cuối mục này, hoặc tải ở mục Releases nếu đã đăng.

| File | Dùng để |
|---|---|
| `uav_viewer.exe` | Nháy đúp để xem video kèm box vẽ trên laptop. Tham số dùng như bản Python, ví dụ `uav_viewer.exe --conceal`. |

- Đây là 1 file duy nhất (khoảng 52 MB), chép sang máy Windows 64-bit khác vẫn chạy. Mỗi lần mở mất khoảng 3–4 s để
  giải nén; độ trễ video không bị ảnh hưởng.
- Cửa sổ console đi kèm hiện IP laptop và các cảnh báo, không cần quan tâm nếu không dùng.
- Lần đầu chạy, Firewall hỏi lại cho chính file exe này: chọn **Allow**, tick cả **Public** nếu xem qua dây cắm thẳng.
- Nếu chép sang máy khác qua mạng/USB mà Windows hiện "Windows protected your PC", bấm More info → Run anyway
  (do exe tự đóng gói, không có chữ ký số).

Dựng lại exe sau khi sửa code:

```
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --console --name uav_viewer --collect-all av uav_viewer.py
```

## 9. Cấu trúc code (cho giai đoạn sau)

| Phần | Lớp |
|---|---|
| Nhận + tách RTP | `RtpReceiver`, `H264Depacketizer` |
| Giải mã | `AUQueue`, `H264Decoder` |
| Hiển thị, HUD | `Viewer` |
| Box mục tiêu | `TrackReceiver` (nhận JSON `uav_track`), `TrackOverlay` (vẽ). Còn lại cho giai đoạn sau: `Overlay.on_click` để click chuột chọn mục tiêu |

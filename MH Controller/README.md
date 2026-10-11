# MH Controller — app xem video UAV độ trễ thấp (laptop Windows)

App nhận video H.264 thô từ UAV và toạ độ mục tiêu do trạm mặt đất (Radxa) chuyển tiếp, giải mã với độ trễ thấp
và **vẽ box mục tiêu ngay trên laptop**. Thay cho VLC: không bộ đệm, không đồng bộ theo đồng hồ, luôn chỉ vẽ khung
mới nhất.

```
Pi Air Unit ──wfb-ng 5.8 GHz──▶ Radxa VRX: gs_forward.py (chuyển tiếp nguyên gói, < 1 ms)
                                   │  video RTP H.264 → laptop UDP :5600
                                   │  JSON toạ độ     → laptop UDP :5700
                                   ▲  laptop tự đăng ký: UDP :5599 mỗi giây
                                MH Controller: uav_viewer — tách RTP → giải mã (PyAV) → hiển thị + box (pygame)
```

## Cấu trúc

```
viewer/uav_viewer.py     App (một file): nhận RTP, giải mã, hiển thị, vẽ box, log chẩn đoán
tools/udp_loss_proxy.py  Proxy UDP bỏ ngẫu nhiên gói — thử app khi mất gói
tests/test_viewer.py     Kiểm tra tự động không cần UAV (giả lập Pi + Radxa trên 127.0.0.1)
build_exe.ps1            Đóng gói thành dist\uav_viewer.exe (PyInstaller)
requirements.txt         Thư viện Python
CLAUDE.md                Hướng dẫn cho Claude Code / AI agent
```

## Cài đặt và chạy

```
python -m pip install -r requirements.txt     # Python ≥ 3.9
python viewer\uav_viewer.py                    # hoặc nháy đúp dist\uav_viewer.exe
```

- **Windows Firewall:** lần đầu chạy, chọn **Allow** cho `python.exe` / `uav_viewer.exe`, tick cả **Public**: dây cắm
  thẳng Radxa là mạng "Unidentified network" mà Windows xếp vào Public. Bị chặn thì app chỉ hiện NO SIGNAL.
- Đóng VLC/ffplay đang nghe cổng 5600 trước khi chạy.
- Không cần sửa gì trên Radxa: app **tự đăng ký** nhận video (xem mục Mạng).

## Phím tắt

| Phím | Chức năng |
|---|---|
| `F` / `F11` / nháy đúp chuột | Toàn màn hình (video 1080p hiện đúng 1:1) |
| `H` | Bật/tắt HUD số liệu |
| `B` | Bật/tắt box mục tiêu |
| `C` | Đổi cách xử lý mất gói: auto → conceal → idr |
| `Esc` / `Q` | Thoát |

## Hiển thị

**Box mục tiêu** (từ JSON `uav_track` của Pi):
- Mục tiêu đang thấy: **xanh lá**. Đang dự đoán (`lost_frames > 0`): **vàng**, nhãn có "(pred)". Track khác: **xanh lơ**.
- Góc trên trái: `TELEM Hz | DET` và `TARGET #id lớp điểm err x/y`. Toạ độ cũ hơn 0,5 s thì không vẽ, hiện `NO TELEMETRY`.
- UAV báo `detector` khác `ok` (Coral lỗi): hiện dòng cảnh báo đỏ. Box không hiện vì UAV không gửi mục tiêu.

**HUD** (góc dưới, cập nhật mỗi giây): độ phân giải, fps hiển thị/nhận, bitrate; `tuổi khung` = từ lúc nhận gói cuối
của khung tới lúc vẽ xong (độ trễ riêng của app, thường ~5–15 ms); thời gian giải mã; % mất gói; số khung bỏ / che lỗi.

**Trạng thái:** không có gói quá 1 s → `NO SIGNAL` (không tự tắt, có gói lại thì chạy tiếp). Đổi độ phân giải giữa
chừng app tự xử lý.

## Mất gói

Mất gói lẻ tẻ đến từ sóng UAV → Radxa (wfb-ng có lúc báo `video rx: 3 packets lost`); dây Radxa → laptop không mất gói.

| `--loss-mode` | Khi mất gói |
|---|---|
| `auto` (mặc định) | Mất ≤ 20 gói một lần: vẫn giải mã, decoder che lỗi → **không đứng hình**, có thể nhoè nhẹ tới khung IDR kế tiếp (≤ 1 s). Mất nhiều / luồng khởi động lại / hàng đợi tràn: bỏ khung chờ IDR |
| `conceal` | Luôn che lỗi |
| `idr` | Mất 1 gói cũng bỏ khung tới IDR (đứng hình tới ~1 s, không nhoè) |

## Mạng

| Máy | IP |
|---|---|
| Radxa (cổng Ethernet) | 192.168.50.1 khi cắm dây thẳng (ngoài ra nhận IP DHCP khi cắm router) |
| Laptop (cắm dây thẳng) | đặt tĩnh 192.168.50.2/24, **không gateway, không DNS** (Internet vẫn đi WiFi) |

**Tự đăng ký** với `gs_forward.py`: mỗi giây app gửi một gói UDP nhỏ tới Radxa cổng 5599; Radxa gửi video + toạ độ về
IP nguồn của gói đó (hết hạn sau 5 s khi app tắt).
- Đang nhận video: chỉ gửi thẳng tới Radxa đang phát, nên Radxa chỉ gửi về một đường (không gửi trùng qua WiFi).
- Chưa có / mất video quá 2 s: gửi tới `--gs` (mặc định 192.168.50.1) **và broadcast trên mọi mạng của laptop**,
  nên cắm dây thẳng hay cùng router đều tự tìm được Radxa.

## Tuỳ chọn dòng lệnh

| Tuỳ chọn | Ý nghĩa |
|---|---|
| `--port 5600` / `--track-port 5700` | Cổng nhận video / toạ độ (`--track-port 0` = tắt box) |
| `--gs IP[,IP]` | IP Radxa để đăng ký (mặc định 192.168.50.1). `--no-subscribe`: không tự đăng ký (Radxa đã đặt `--dest`) |
| `--loss-mode auto\|conceal\|idr`, `--conceal` | Xử lý mất gói (xem trên) |
| `--fullscreen`, `--size 1600x900`, `--no-hud` | Cửa sổ lúc mở |
| `--scale FAST_BILINEAR` | Co ảnh nhanh hơn cho máy yếu |
| `--threads N`, `--hwaccel d3d11va` | Giải mã nhiều luồng SLICE / thử GPU (chỉ dùng nếu đo thấy nhanh hơn) |
| `--log FILE`, `--no-log`, `--print-stats`, `--verbose` | Log chẩn đoán, in số liệu ra console, log của FFmpeg |

## Log chẩn đoán

Mỗi lần chạy app ghi một file vào `logs\` cạnh app (exe: `dist\logs\`, giữ 30 file mới nhất). Mỗi dòng có giờ tới
mili-giây: thấy mất hình thì nhớ giờ rồi tra log.

| Dòng | Ý nghĩa |
|---|---|
| `[5600] nhận … lặng_max … tới_muộn_max …` | Số liệu mỗi giây: khoảng trống dài nhất giữa 2 gói, khung tới muộn so với nhịp phát |
| `ĐỨNG HÌNH X ms → nguyên nhân` | Hình đứng > 250 ms kèm nguyên nhân: không có gói / mất gói → chờ IDR / lỗi giải mã / hàng đợi tràn / UI bị chặn |
| `MẤT N gói`, `CHE LỖI`, `CHỜ IDR`, `IM LẶNG`, `RESET luồng`, `NO SIGNAL` | Sự kiện mạng và giải mã |
| `[toạ độ] bộ phát hiện trên UAV: …`, `CÓ / MẤT MỤC TIÊU` | Trạng thái Coral và mục tiêu |
| `[app] đang nhận video từ … / tìm trạm mặt đất` | Trạng thái tự đăng ký |

Đối chiếu mất gói với Radxa: `journalctl | grep 'packets lost'`.

## Kiểm tra và thử không cần UAV

```
python tests\test_viewer.py        # ~1 phút: ghép RTP, giải mã, mất gói, chạy cả app với Pi/Radxa giả lập
```

Thử với luồng thật hơn (cần ffmpeg trong PATH), mất 3 % gói qua proxy:

```
python tools\udp_loss_proxy.py --listen 5610 --to 127.0.0.1:5600 --loss 3
ffmpeg -re -f lavfi -i testsrc2=size=1920x1080:rate=30 -c:v libx264 -preset ultrafast -tune zerolatency -g 30 -bf 0 -b:v 5M -f rtp rtp://127.0.0.1:5610
```

## Đóng gói exe

```
powershell -ExecutionPolicy Bypass -File build_exe.ps1     # → dist\uav_viewer.exe (~52 MB, 1 file)
```

- Chép sang máy Windows 64-bit khác vẫn chạy. Mỗi lần mở mất ~3–4 s giải nén, không ảnh hưởng độ trễ.
- Exe không có chữ ký số: Windows có thể hiện "Windows protected your PC" → More info → Run anyway.
- Giữ đường dẫn `dist\uav_viewer.exe`: luật Firewall đã Allow gắn với đường dẫn này.

## Đo độ trễ toàn tuyến

Mở đồng hồ bấm giờ có mili-giây trên laptop, quay camera UAV vào màn hình, chụp màn hình nhiều lần: hiệu giữa đồng
hồ thật và đồng hồ trong video là độ trễ toàn tuyến. Màn hình 60 Hz sai số ±17 ms/lần, chụp ≥ 5 lần lấy trung bình.
`tuổi khung` trên HUD là phần của riêng app.

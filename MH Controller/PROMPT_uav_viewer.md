# Prompt: viết app xem video UAV độ trễ thấp trên laptop (Windows)

Bạn là Claude Code chạy trên **laptop Windows** của người dùng. Hãy viết một app Python tên
`uav_viewer.py` để xem video từ UAV với **độ trễ thấp nhất có thể**, thay cho VLC (VLC đang trễ
~0.5–0.6 s vì bộ đệm, đồng bộ đồng hồ và giải mã đa luồng giữ khung). Người dùng nói tiếng Việt;
trả lời và viết comment bằng tiếng Việt.

## 1. Bối cảnh hệ thống (đã chạy, đừng sửa nếu không được yêu cầu)

```
Pi 4B (UAV): camera IMX708 → H.264 phần cứng 1920x1080@30, ~7.5 Mbit/s → RTP → wfb-ng (WiFi 5.8 GHz)
   │  + JSON toạ độ mục tiêu ~30 gói/s qua tunnel wfb-ng
   ▼
Radxa Zero 3W (trạm mặt đất, Debian 11, IP LAN 192.168.1.75; khi cắm dây thẳng laptop: 192.168.50.1)
   service uav-gs-overlay (gs_overlay.py): giải mã → VẼ BOX + HUD → nén lại H.264 → gửi tới laptop:
     • RTP/UDP → laptop:5600   (video ĐÃ VẼ BOX — luồng app này cần xem)
     • RTP/UDP → laptop:5601   (video THÔ từ UAV, không box — để so độ trễ)
     • RTSP rtsp://192.168.1.75:8554/uav (dự phòng)
   Cấu hình đích UDP: /etc/default/uav-gs-overlay, biến GS_ARGS
     (hiện: --udp 192.168.1.62:5600 --raw-udp 192.168.1.62:5601; laptop đổi IP thì sửa ở đây
      rồi `sudo systemctl restart uav-gs-overlay`; SSH: radxa@192.168.1.75, sudo không cần mật khẩu)
   ▼
Laptop Windows (IP hiện tại 192.168.1.62, đang qua WiFi; ping tới Radxa 2–100 ms dao động)
```

Đặc điểm luồng video gửi tới laptop (cả 5600 và 5601):
- RTP, payload type 96, H.264 (packetization-mode=1: có NAL đơn, **FU-A**, có thể **STAP-A**), clock 90 kHz.
- 1920x1080 @ 30 fps (có thể đổi độ phân giải giữa chừng, ví dụ 1600x900 — app phải tự thích nghi).
- SPS/PPS lặp lại trước mỗi IDR; IDR mỗi ~30 khung (1 s). Không có B-frame.
- Luồng 5600 là do Radxa nén lại (mpph264enc, High profile, 8 Mbit/s); luồng 5601 là nguyên bản từ Pi.
- Có thể mất gói (WiFi). Không có kênh âm thanh.

Độ trễ đã đo: Pi ~130 ms, sóng + wfb-ng ~10 ms, Radxa vẽ box + nén lại ~60–70 ms (luồng 5600) hoặc ~0 ms
(luồng 5601). Mục tiêu của app: **giải mã + hiển thị ≤ 30–40 ms** sau khi gói cuối của khung tới laptop.

## 2. Yêu cầu chức năng

1. Nghe UDP (mặc định cổng **5600**; tham số `--port 5601` để xem luồng thô). Bind `0.0.0.0`.
2. Hiện video trong cửa sổ (resize được, giữ tỉ lệ; phím `F` hoặc `F11` toàn màn hình, `Esc`/`Q` thoát).
3. HUD nhỏ góc dưới (bật/tắt bằng phím `H`): fps hiển thị, fps nhận, % mất gói RTP, thời gian giải mã,
   "tuổi" khung (từ lúc nhận gói cuối của khung tới lúc vẽ lên màn hình), độ phân giải, bitrate.
4. Không có gói > 1 s → hiện "NO SIGNAL" (không đóng app); có gói lại thì tự chạy tiếp.
5. Đổi độ phân giải giữa chừng → tự xử lý (decoder đọc SPS mới; cửa sổ/scale cập nhật theo).
6. Không crash vì gói hỏng/thiếu; lỗi giải mã thì bỏ khung đó.

## 3. Thiết kế bắt buộc để độ trễ thấp

- **Không dùng `av.open("x.sdp")` / demuxer RTP của FFmpeg** cho đường chính: nó có hàng đợi sắp xếp lại
  (reorder queue) và bộ đệm riêng. Hãy **tự tách gói RTP** (đơn giản): đọc header 12 byte (+CSRC,
  +extension nếu có), lấy seq, timestamp, marker; ghép NAL:
  - NAL đơn (type 1–23): thêm start code `00 00 00 01`.
  - STAP-A (24): tách nhiều NAL bên trong.
  - FU-A (28): ghép mảnh theo start/end bit; mất mảnh giữa chừng → bỏ NAL đó.
  - Một khung = các NAL có cùng RTP timestamp; kết thúc khi **marker bit = 1** (hoặc timestamp đổi).
- Giải mã bằng **PyAV** `av.CodecContext.create("h264", "r")`, đặt `flags = LOW_DELAY`
  (`ctx.flags |= av.codec.context.Flags.LOW_DELAY` hoặc `ctx.options = {"flags": "low_delay"}`),
  **không dùng frame threading** (`thread_type = "SLICE"` hoặc `thread_count = 1`) — frame threading
  giữ thêm (N−1) khung. Đưa cả khung Annex-B vào `ctx.decode(av.Packet(data))`.
  - Tuỳ chọn thử thêm sau: giải mã phần cứng D3D11VA (PyAV ≥ 13 có `hwaccel`) — chỉ dùng nếu đo thấy
    nhanh hơn và không thêm trễ; mặc định giải mã CPU (1080p30 trên laptop chỉ ~5–10 ms).
- **3 luồng, chỉ giữ khung MỚI NHẤT, không bao giờ xếp hàng:**
  1. Luồng nhận: `socket` UDP với `SO_RCVBUF` lớn (~4 MB), tách RTP → khung nén hoàn chỉnh.
  2. Luồng giải mã: lấy khung nén theo thứ tự (hàng đợi nhỏ; nếu đầy thì bỏ cũ NHƯNG phải giữ đúng
     chuỗi tham chiếu: khi bỏ khung thì bỏ tới IDR kế tiếp, nếu không sẽ ra hình nhoè/xám).
  3. Luồng chính (UI): chỉ vẽ khung giải mã mới nhất; khung đến muộn bị đè, không chờ.
- Hiển thị: **pygame** (SDL2). Chuyển YUV→RGB bằng `frame.reformat(width=W_cửa_sổ, height=H_cửa_sổ,
  format="rgb24")` (swscale co ảnh luôn → rẻ hơn), rồi `pygame.image.frombuffer` / `surfarray` → blit →
  `pygame.display.flip()`. Tắt vsync nếu được (`pygame.display.set_mode(..., vsync=0)`) để không chờ
  khung màn hình; đo xem vsync có làm trễ thêm không.
- Không đồng bộ theo PTS/đồng hồ — có khung là hiện ngay.
- Mất gói (seq nhảy): khung bị thiếu mảnh → bỏ; nên bỏ tiếp tới IDR kế tiếp (có tuỳ chọn
  `--conceal` để vẫn giải mã cho decoder tự che lỗi, người dùng tự chọn).

## 4. Đo độ trễ (bắt buộc làm, báo con số cho người dùng)

- Trong app: đo "tuổi" khung = thời điểm hiển thị − thời điểm nhận gói cuối của khung; hiện trên HUD
  (trung bình + max mỗi giây).
- Đo toàn tuyến bằng đồng hồ: người dùng mở đồng hồ bấm giờ có mili-giây trên màn hình laptop, quay
  camera UAV vào màn hình, chụp màn hình có cả đồng hồ thật và đồng hồ trong video → hiệu = độ trễ.
  So: VLC (~500–600 ms hiện tại) vs app (5600) vs app (5601 thô).
- Kỳ vọng: app + luồng 5600 ≈ 230–280 ms qua WiFi (≈ 220 ms nếu cắm dây); app + luồng 5601 ≈ 60 ms thấp hơn.

## 5. Thử không cần UAV (trên laptop)

Cần ffmpeg (tải bản "essentials" ở gyan.dev, thêm thư mục `bin` vào PATH):
```
ffmpeg -re -f lavfi -i testsrc2=size=1920x1080:rate=30 -c:v libx264 -preset ultrafast -tune zerolatency -g 30 -bf 0 -b:v 8M -f rtp rtp://127.0.0.1:5600
```
Thử thêm mất gói: dùng `clumsy` (Windows) hoặc tự viết proxy UDP bỏ ngẫu nhiên 1–5% gói.

## 6. Cài đặt & bàn giao

- Kiểm tra Python (`python --version`, cần ≥ 3.9). `requirements.txt`: `av`, `pygame`, `numpy`.
  Cài: `python -m pip install -r requirements.txt`.
- Windows Firewall: lần đầu chạy phải "Allow" cho python.exe (mạng Private); nếu không sẽ không nhận được gói.
- Viết `README.md` ngắn (tiếng Việt): cài, chạy (`python uav_viewer.py`, `--port 5601`), phím tắt, cách đo trễ.
- Tuỳ chọn cuối: đóng gói `.exe` bằng PyInstaller.
- Laptop đổi IP (DHCP, hoặc cắm dây thẳng Radxa → laptop phải đặt tĩnh 192.168.50.2/24, không gateway/DNS)
  thì phải sửa `GS_ARGS` trên Radxa như mục 1.

## 7. Giai đoạn sau (CHƯA làm, chỉ để thiết kế không chặn đường)

- **Vẽ box ngay trên laptop** (bỏ khâu vẽ + nén lại ~60–70 ms trên Radxa): xem luồng thô 5601 và nhận JSON
  toạ độ. Cần thêm trên Radxa tham số chuyển tiếp toạ độ tới laptop (chưa có). Định dạng JSON (UDP, ~30 gói/s):
  ```json
  {"type":"uav_track","seq":481,"sensor_ts_ns":4596107447000,"wall":1791451053.202,"frame":[1920,1080],
   "detector":"ok","target":{"id":11,"cls":"person","score":0.73,"box":[0.014,0.0,0.8724,0.9887],
   "center":[0.4432,0.4943],"err":[-0.1136,-0.0113],"lost_frames":2},
   "tracks":[{"id":11,"cls":"person","score":0.73,"box":[...],"lost_frames":2}]}
  ```
  `box` = x1,y1,x2,y2 chuẩn hoá 0..1; `lost_frames>0` = vị trí dự đoán; `target` có thể null.
- **Click chuột chọn mục tiêu** → gửi lệnh tới UAV qua tunnel wfb-ng (UAV 10.5.0.2) — phía Pi chưa có code nhận.
Vì vậy hãy tách code thành: nhận RTP / giải mã / hiển thị / lớp vẽ overlay (để sau cắm thêm toạ độ).

#!/bin/bash
# Phát CHỈ video camera -> wfb-ng (UDP 5602), không detect. Dùng để thử link sóng riêng.
# Cùng thông số với tracker/uav_tracker.py: cảm biến 2304x1296 (toàn bộ góc nhìn) -> 1080p30 H.264 5 Mbps.
# 5 Mbps vì trần sóng MCS1 + FEC 8/12 chỉ ~7.5 Mbps video (đo được; 8 Mbps làm nghẽn).
#
# Camera chỉ mở được bởi 1 tiến trình: dừng service trước khi chạy.
#   sudo systemctl stop uav-tracker && ./tools/stream_video.sh
set -euo pipefail

WIDTH=1920
HEIGHT=1080
FPS=30
BITRATE=5000000      # bps
WFB_PORT=5602

if systemctl is-active --quiet uav-tracker; then
    echo "uav-tracker đang giữ camera -> sudo systemctl stop uav-tracker" >&2
    exit 1
fi

rpicam-vid -n -t 0 \
    --mode 2304:1296:10:P --width $WIDTH --height $HEIGHT --framerate $FPS \
    --codec h264 --level 4.1 --bitrate $BITRATE --inline --intra $FPS -o - \
  | gst-launch-1.0 -q fdsrc ! h264parse ! rtph264pay config-interval=1 pt=96 mtu=1400 \
  ! udpsink host=127.0.0.1 port=$WFB_PORT sync=false

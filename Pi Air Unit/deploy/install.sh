#!/bin/bash
# Cài đặt phía UAV trên Raspberry Pi 4 (Raspberry Pi OS Bookworm 64-bit) — chạy lại nhiều lần được.
#
#   ./deploy/install.sh [đường-dẫn-drone.key]      (chạy từ đâu cũng được)
#
# Làm: gói apt, wfb-ng (repo apt chính thức), libedgetpu1-std, driver RTL8812AU (svpcom, DKMS),
# cấu hình hệ thống (wfb-ng, modprobe, NetworkManager, journald, regdom, USB), venv Python,
# model, service systemd. KHÔNG tạo khoá wfb-ng: khoá phải là cặp với trạm mặt đất
# (tạo 1 lần bằng `wfb_keygen` -> drone.key cho UAV, gs.key cho mặt đất).
# Cần reboot sau lần cài đầu (driver, cmdline, config.txt).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ETC="$REPO/deploy/etc"     # cây thư mục giống hệt /etc trên Pi
KEY_SRC="${1:-$HOME/wfb-keys/drone.key}"
SRC_DIR="$HOME/src"
step() { echo; echo "=== $* ==="; }

step "Gói apt"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    git curl dkms python3-venv python3-picamera2 \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad

step "wfb-ng (apt.wfb-ng.org, release-25.01)"
if ! dpkg -s wfb-ng >/dev/null 2>&1; then
    curl -fsSL https://apt.wfb-ng.org/public.asc | sudo gpg --dearmor --yes -o /usr/share/keyrings/wfb-ng.gpg
    echo "deb [signed-by=/usr/share/keyrings/wfb-ng.gpg] https://apt.wfb-ng.org/ $(. /etc/os-release; echo "$VERSION_CODENAME") release-25.01" \
        | sudo tee /etc/apt/sources.list.d/wfb-ng.list >/dev/null
    sudo apt-get update
    # KHÔNG chạy install_gs.sh của wfb-ng: nó đặt region BO (làm rớt WiFi onboard), ghi đè motd, dhcpcd...
    sudo apt-get install -y wfb-ng
fi

step "libedgetpu1-std 16.0tf2.16.1 (repo apt Coral của Google trả 403; bản -max crash với tflite 2.14)"
if ! dpkg -s libedgetpu1-std >/dev/null 2>&1; then
    deb=/tmp/libedgetpu1-std_16.0tf2.16.1-1.bookworm_arm64.deb
    curl -fL -o "$deb" https://github.com/feranick/libedgetpu/releases/download/16.0TF2.16.1-1/libedgetpu1-std_16.0tf2.16.1-1.bookworm_arm64.deb
    sudo apt-get install -y "$deb"
fi

step "Driver RTL8812AU svpcom (DKMS, module 88XXau_wfb)"
if ! dkms status 2>/dev/null | grep -q "realtek-rtl88xxau.*installed"; then
    # Không cài linux-headers-* chung của Debian; Pi OS dùng linux-headers-rpi-v8
    sudo apt-get install -y linux-headers-rpi-v8
    [ -d "$SRC_DIR/rtl8812au" ] || git clone --depth 1 https://github.com/svpcom/rtl8812au.git "$SRC_DIR/rtl8812au"
    ver=$(sed -n 's/^PACKAGE_VERSION="\(.*\)"/\1/p' "$SRC_DIR/rtl8812au/dkms.conf")
    # KHÔNG chạy dkms-install.sh của driver: nó tắt IPv6 toàn hệ thống
    sudo rm -rf "/usr/src/realtek-rtl88xxau-$ver"
    sudo cp -r "$SRC_DIR/rtl8812au" "/usr/src/realtek-rtl88xxau-$ver"
    sudo dkms add -m realtek-rtl88xxau -v "$ver" || true
    sudo dkms build -m realtek-rtl88xxau -v "$ver"
    sudo dkms install -m realtek-rtl88xxau -v "$ver"
fi

step "Khoá wfb-ng /etc/drone.key"
if sudo test -f /etc/drone.key; then
    echo "đã có /etc/drone.key"
elif [ -f "$KEY_SRC" ]; then
    sudo install -m 600 -o root -g root "$KEY_SRC" /etc/drone.key
else
    echo "CHƯA CÓ KHOÁ: chạy 'wfb_keygen' (1 lần cho cả cặp), chép drone.key vào /etc/drone.key (root 600)"
    echo "và gs.key sang trạm mặt đất /etc/gs.key. Không đưa khoá lên git."
fi

step "Cấu hình hệ thống"
for f in wifibroadcast.cfg modprobe.d/wfb.conf NetworkManager/conf.d/99-wfb-ng-unmanaged.conf \
         systemd/journald.conf.d/99-persistent.conf; do
    sudo install -D -m 644 "$ETC/$f" "/etc/$f"
done
sudo nmcli general reload conf 2>/dev/null || true
sudo systemctl restart systemd-journald
# regdom VN ngay từ lúc boot (kênh 165 = 5825 MHz hợp lệ, không đụng kênh WiFi onboard)
grep -q "cfg80211.ieee80211_regdom=" /boot/firmware/cmdline.txt \
    || sudo sed -i '1 s/$/ cfg80211.ieee80211_regdom=VN/' /boot/firmware/cmdline.txt
# Cho phép cổng USB cấp dòng tối đa (Coral + card WiFi)
grep -q "^usb_max_current_enable=1" /boot/firmware/config.txt \
    || echo "usb_max_current_enable=1" | sudo tee -a /boot/firmware/config.txt >/dev/null

step "Python venv + model"
[ -x "$REPO/.venv/bin/python" ] || python3 -m venv --system-site-packages "$REPO/.venv"
"$REPO/.venv/bin/python" -m pip install -q -r "$REPO/requirements.txt"
"$REPO/models/download_model.sh"

step "Service systemd"
sed "s|@REPO@|$REPO|g" "$ETC/systemd/system/uav-tracker.service" | sudo tee /etc/systemd/system/uav-tracker.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable wifibroadcast.service wifibroadcast@drone.service uav-tracker.service

echo
echo "XONG. Lần cài đầu: lắp anten vào card WiFi rồi 'sudo reboot' (wfb-ng phát ngay khi boot)."
echo "Kiểm tra: sudo journalctl -u uav-tracker -f    |    python3 $REPO/tools/wfb_stats.py"

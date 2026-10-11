#!/usr/bin/env python3
"""Giám sát link wfb-ng ở trạm mặt đất và tự phục hồi card RTL8812EU (chạy bằng root).

Hai lỗi đã gặp (2026-10-11):
  1. Card rớt khỏi USB khi chạy tải (error -71, "Device not responding to setup address") và
     không tự lên lại → reset bộ điều khiển USB của cổng OTG (unbind/bind fcc00000.dwc3).
  2. Driver 8812eu đôi khi "chết phần thu" sau khi interface bị cấu hình lại (cảnh báo
     rtw_mlmeext_disconnect trong dmesg): card vẫn ở monitor/kênh 165 nhưng 0 gói. Khởi động lại
     wfb-ng không đủ; phải rmmod/modprobe 8812eu, chờ vài giây rồi mới khởi động wfb-ng.

Số gói đọc từ JSON API của wfb-ng (127.0.0.1:8103, stream "video rx", 1 bản tin/giây).
Nếu UAV đang tắt thì cũng thấy 0 gói → thời gian giữa các lần phục hồi tăng dần (tối đa 5 phút).
"""
import json
import logging
import os
import socket
import subprocess
import time

API = ("127.0.0.1", 8103)
SERVICE = "wifibroadcast@gs"
USB_CTRL = "fcc00000.dwc3"            # bộ điều khiển USB của cổng OTG (cắm card)
DWC3 = "/sys/bus/platform/drivers/dwc3"
CHECK_EVERY = 2.0
ZERO_RX_AFTER = 10.0                  # 0 gói liên tục quá chừng này giây → phục hồi
TUNNEL_STUCK_AFTER = 15.0             # tunnel có gói tới mà không giải mã được gói nào → phục hồi
ABSENT_AFTER = 6.0                    # card mất khỏi USB quá chừng này giây → reset USB
BACKOFF_MIN, BACKOFF_MAX = 20.0, 300.0

log = logging.getLogger("wfb_watchdog")


def sh(cmd, check=False):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode:
        log.warning("lệnh lỗi (%d): %s %s", r.returncode, cmd, r.stderr.strip())
    return r.stdout.strip()


def card_present():
    return bool(sh("wfb-nics"))


def service_active():
    return sh(f"systemctl is-active {SERVICE}") == "active"


def rx_stats(timeout=3.0):
    """{"video rx": packets, "tunnel rx": packets} (số trong 1 s qua) từ wfb-ng, None nếu không đọc được."""
    want, got = ("video rx", "tunnel rx"), {}
    try:
        with socket.create_connection(API, timeout=timeout) as s:
            s.settimeout(timeout)
            buf, t0 = b"", time.monotonic()
            while time.monotonic() - t0 < timeout and len(got) < len(want):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    if m.get("type") == "rx" and m.get("id") in want:
                        got[m["id"]] = {k: v[0] for k, v in m["packets"].items()}
    except OSError:
        return None
    return got if "video rx" in got else None


def reset_usb():
    log.warning("card mất khỏi USB → reset bộ điều khiển USB %s", USB_CTRL)
    if os.path.exists(f"{DWC3}/{USB_CTRL}"):           # đang gắn driver thì mới tháo
        sh(f"echo {USB_CTRL} > {DWC3}/unbind", check=True)
    time.sleep(2)
    sh(f"echo {USB_CTRL} > {DWC3}/bind", check=True)


def reload_driver_and_restart():
    log.warning("wfb-ng không thu được gói → nạp lại driver 8812eu và khởi động lại %s", SERVICE)
    sh(f"systemctl stop {SERVICE}", check=True)
    sh("rmmod 8812eu", check=True)
    time.sleep(2)
    sh("modprobe 8812eu", check=True)
    for _ in range(20):                      # chờ interface xuất hiện (≤10 s)
        if card_present():
            break
        time.sleep(0.5)
    time.sleep(4)                            # cấu hình card quá sớm sau khi nạp driver → thu chết
    sh(f"systemctl start {SERVICE}", check=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log.info("bắt đầu giám sát %s", SERVICE)
    zero_since = absent_since = tunnel_bad_since = None
    last_fix, backoff = 0.0, BACKOFF_MIN
    was_ok = None
    while True:
        now = time.monotonic()
        if not card_present():
            zero_since = None
            absent_since = absent_since or now
            if now - absent_since >= ABSENT_AFTER and now - last_fix >= backoff:
                reset_usb()
                last_fix, backoff = time.monotonic(), min(backoff * 2, BACKOFF_MAX)
                absent_since = None
            time.sleep(CHECK_EVERY)
            continue
        absent_since = None

        st = rx_stats() if service_active() else None
        rx = st["video rx"]["all"] if st else None

        # Đã gặp: sau khi card bị reset, luồng tunnel nhận gói đều nhưng KHÔNG giải mã được gói nào
        # (không nhận được khoá phiên), trong khi video vẫn chạy → mất toạ độ mục tiêu.
        tun = st.get("tunnel rx") if st else None
        if tun and tun.get("all", 0) > 0 and tun.get("dec_ok", 0) == 0:
            tunnel_bad_since = tunnel_bad_since or now
            if now - tunnel_bad_since >= TUNNEL_STUCK_AFTER and now - last_fix >= backoff:
                log.warning("tunnel nhận gói nhưng không giải mã được (không có khoá phiên)")
                reload_driver_and_restart()
                last_fix, backoff = time.monotonic(), min(backoff * 2, BACKOFF_MAX)
                tunnel_bad_since = None
                time.sleep(CHECK_EVERY)
                continue
        else:
            tunnel_bad_since = None

        if rx:                                       # đang thu tốt
            if was_ok is not True:
                log.info("link video OK (%d gói/s)", rx)
            was_ok, zero_since = True, None
            if tunnel_bad_since is None:
                backoff = BACKOFF_MIN
        elif rx == 0:                                # wfb-ng chạy nhưng 0 gói
            if was_ok is not False:
                log.info("wfb-ng chạy nhưng không thu được gói video")
            was_ok = False
            zero_since = zero_since or now
            if now - zero_since >= ZERO_RX_AFTER and now - last_fix >= backoff:
                reload_driver_and_restart()
                last_fix, backoff = time.monotonic(), min(backoff * 2, BACKOFF_MAX)
                zero_since = None
        # rx is None: wfb-ng chưa chạy / đang khởi động → để systemd lo (Restart=on-failure)
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()

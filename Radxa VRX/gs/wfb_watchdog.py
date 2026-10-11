#!/usr/bin/env python3
"""Giám sát link wfb-ng ở trạm mặt đất và tự phục hồi card RTL8812EU (chạy bằng root).

Lỗi đã gặp trên Radxa Zero 3W + RTL8812EU (svpcom/rtl8812eu):
  1. Card rớt khỏi USB khi chạy tải (error -71, "Device not responding to setup address") và không
     tự lên lại → reset bộ điều khiển USB của cổng OTG (unbind/bind fcc00000.dwc3).
  2. Driver "chết phần thu" sau khi interface bị cấu hình lại (dmesg: rtw_mlmeext_disconnect,
     buffer_cnt = -1, cmd queue crashed): card vẫn monitor/kênh đúng nhưng 0 gói. Khởi động lại
     wfb-ng không đủ → rmmod/modprobe 8812eu, chờ vài giây rồi mới khởi động wfb-ng.
  3. Sau khi card bị reset, luồng tunnel nhận gói đều nhưng không giải mã được gói nào (không có
     khoá phiên) → mất toạ độ dù video vẫn chạy. Chữa như (2).

Số liệu lấy từ JSON API của wfb-ng (127.0.0.1:8103, 1 bản tin/giây/luồng). UAV tắt cũng cho 0 gói
→ khoảng cách giữa các lần phục hồi tăng dần 20 s → 5 phút.
"""
import glob
import json
import logging
import os
import socket
import subprocess
import time

API = ("127.0.0.1", 8103)
SERVICE = "wifibroadcast@gs"
DRIVER_MODULE = "8812eu"
WFB_DRIVERS = ("rtl88x2eu", "rtl88xxau_wfb")   # giống script wfb-nics
USB_CTRL = "fcc00000.dwc3"                      # bộ điều khiển USB cổng OTG của Radxa Zero 3W (cắm card)
DWC3 = "/sys/bus/platform/drivers/dwc3"
CHECK_EVERY = 2.0
ZERO_RX_AFTER = 10.0                            # 0 gói video liên tục → phục hồi
TUNNEL_STUCK_AFTER = 15.0                       # tunnel có gói mà không giải mã được → phục hồi
ABSENT_AFTER = 6.0                              # card mất khỏi USB → reset USB
BACKOFF_MIN, BACKOFF_MAX = 20.0, 300.0

log = logging.getLogger("wfb_watchdog")


def run(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        log.warning("lệnh lỗi (%d): %s %s", r.returncode, " ".join(cmd), r.stderr.strip())


def card_present():
    """Có interface mạng nào dùng driver wfb-ng hỗ trợ không (đọc /sys, không gọi tiến trình con)."""
    for link in glob.glob("/sys/class/net/*/device/driver"):
        if os.path.basename(os.path.realpath(link)) in WFB_DRIVERS:
            return True
    return False


def rx_stats(timeout=3.0):
    """{"video rx": {...}, "tunnel rx": {...}} (số liệu 1 s qua), None nếu wfb-ng không chạy."""
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
    if os.path.exists(f"{DWC3}/{USB_CTRL}"):
        with open(f"{DWC3}/unbind", "w") as f:
            f.write(USB_CTRL)
    time.sleep(2)
    with open(f"{DWC3}/bind", "w") as f:
        f.write(USB_CTRL)


def reload_driver_and_restart(reason):
    log.warning("%s → nạp lại driver %s và khởi động lại %s", reason, DRIVER_MODULE, SERVICE)
    run("systemctl", "stop", SERVICE)
    run("rmmod", DRIVER_MODULE)
    time.sleep(2)
    run("modprobe", DRIVER_MODULE)
    for _ in range(20):                         # chờ interface xuất hiện (≤ 10 s)
        if card_present():
            break
        time.sleep(0.5)
    time.sleep(4)                               # cấu hình card quá sớm sau khi nạp driver → thu chết
    run("systemctl", "start", SERVICE)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log.info("bắt đầu giám sát %s", SERVICE)
    zero_since = absent_since = tunnel_bad_since = None
    last_fix, backoff = 0.0, BACKOFF_MIN
    was_ok = None

    def may_fix(now):
        return now - last_fix >= backoff

    while True:
        now = time.monotonic()
        if not card_present():
            zero_since = tunnel_bad_since = None
            absent_since = absent_since or now
            if now - absent_since >= ABSENT_AFTER and may_fix(now):
                reset_usb()
                last_fix, backoff = time.monotonic(), min(backoff * 2, BACKOFF_MAX)
                absent_since = None
            time.sleep(CHECK_EVERY)
            continue
        absent_since = None

        st = rx_stats()                         # None: wfb-ng chưa chạy → để systemd lo
        if st is None:
            time.sleep(CHECK_EVERY)
            continue
        rx = st["video rx"].get("all", 0)
        tun = st.get("tunnel rx") or {}

        if tun.get("all", 0) > 0 and tun.get("dec_ok", 0) == 0:
            tunnel_bad_since = tunnel_bad_since or now
        else:
            tunnel_bad_since = None

        if rx:
            if was_ok is not True:
                log.info("link video OK (%d gói/s)", rx)
            was_ok, zero_since = True, None
        else:
            if was_ok is not False:
                log.info("wfb-ng chạy nhưng không thu được gói video")
            was_ok = False
            zero_since = zero_since or now

        reason = None
        if zero_since and now - zero_since >= ZERO_RX_AFTER:
            reason = "không thu được gói video"
        elif tunnel_bad_since and now - tunnel_bad_since >= TUNNEL_STUCK_AFTER:
            reason = "tunnel nhận gói nhưng không giải mã được (không có khoá phiên)"
        if reason and may_fix(now):
            reload_driver_and_restart(reason)
            last_fix, backoff = time.monotonic(), min(backoff * 2, BACKOFF_MAX)
            zero_since = tunnel_bad_since = None
        elif not reason and rx:
            backoff = BACKOFF_MIN                # link khoẻ → lần lỗi sau phục hồi ngay
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()

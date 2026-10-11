#!/usr/bin/env python3
"""Trạm mặt đất: chuyển tiếp NGUYÊN VẸN video + toạ độ từ UAV tới máy xem (laptop vẽ box).

  udp 127.0.0.1:5600  RTP H.264 từ wfb-ng (gs_video)      → <dest>:<video-out-port> (mặc định 5600)
  udp 0.0.0.0:5700    JSON "uav_track" qua tunnel wfb-ng  → <dest>:<telem-out-port> (mặc định 5700)

Không giải mã / vẽ / nén lại → thêm < 1 ms trễ, gần như không tốn CPU. Gửi được tới nhiều máy.
Thay cho gs_overlay.py (vẽ box trên Radxa) từ 2026-10-11.

Máy nhận: --dest IP cố định, và/hoặc TỰ ĐĂNG KÝ: máy xem gửi 1 gói UDP bất kỳ tới cổng
--subscribe-port (mặc định 5599) mỗi ~1 s → được gửi video/toạ độ tới IP đó (hết hạn sau 5 s không
nhận được gói). Laptop đổi IP (DHCP) không cần sửa gì trên Radxa.
"""
import argparse
import logging
import selectors
import socket
import time

log = logging.getLogger("gs_forward")


def make_rx(addr, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    s.bind((addr, port))          # không SO_REUSEADDR: chạy trùng thì báo lỗi rõ ràng
    s.setblocking(False)
    return s


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dest", action="append", default=[], metavar="IP",
                   help="máy nhận cố định (lặp lại để gửi nhiều máy)")
    p.add_argument("--subscribe-port", type=int, default=5599,
                   help="máy xem gửi gói bất kỳ tới cổng này mỗi ~1 s để được nhận (0 = tắt)")
    p.add_argument("--subscribe-ttl", type=float, default=5.0)
    p.add_argument("--video-in", default="127.0.0.1:5600", help="nơi wfb-ng đẩy RTP video tới")
    p.add_argument("--telem-in", default="0.0.0.0:5700", help="nơi UAV gửi JSON toạ độ tới (qua tunnel)")
    p.add_argument("--video-out-port", type=int, default=5600)
    p.add_argument("--telem-out-port", type=int, default=5700)
    p.add_argument("--stats-every", type=float, default=30)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    va, vp = a.video_in.rsplit(":", 1)
    ta, tp = a.telem_in.rsplit(":", 1)
    rx = {"video": make_rx(va, int(vp)), "telem": make_rx(ta, int(tp))}
    out_port = {"video": a.video_out_port, "telem": a.telem_out_port}
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    tx.setblocking(False)

    sel = selectors.DefaultSelector()
    for name, s in rx.items():
        sel.register(s, selectors.EVENT_READ, name)
    subs = {}                                             # IP → hạn chót (monotonic)
    if a.subscribe_port:
        sub_sock = make_rx("0.0.0.0", a.subscribe_port)
        sel.register(sub_sock, selectors.EVENT_READ, "sub")
    dests = list(a.dest)
    count = {k: 0 for k in rx}
    errors = 0
    last_err_log = 0.0
    log.info("chuyển tiếp video %s → cổng %d, toạ độ %s → cổng %d; máy nhận cố định: %s; tự đăng ký: cổng %s",
             a.video_in, a.video_out_port, a.telem_in, a.telem_out_port,
             ",".join(a.dest) or "không", a.subscribe_port or "tắt")

    next_stats = time.monotonic() + a.stats_every
    while True:
        for key, _ in sel.select(timeout=1.0):
            name, s = key.data, key.fileobj
            if name == "sub":                             # máy xem đăng ký / gia hạn
                while True:
                    try:
                        _, (ip, _port) = s.recvfrom(2048)
                    except BlockingIOError:
                        break
                    if ip not in subs:
                        log.info("máy xem %s đăng ký", ip)
                    subs[ip] = time.monotonic() + a.subscribe_ttl
                dests = sorted(set(a.dest) | set(subs))
                continue
            while True:                                   # xả hết gói đang chờ
                try:
                    data = s.recv(65535)
                except BlockingIOError:
                    break
                count[name] += 1
                for d in dests:
                    try:
                        tx.sendto(data, (d, out_port[name]))
                    except OSError as e:                  # máy xem tắt/đổi mạng: bỏ gói, không dừng
                        errors += 1
                        now = time.monotonic()
                        if now - last_err_log > 10:
                            last_err_log = now
                            log.warning("gửi tới %s lỗi: %s (tổng %d lỗi)", d, e, errors)
        now = time.monotonic()
        expired = [ip for ip, t in subs.items() if t < now]
        for ip in expired:
            del subs[ip]
            log.info("máy xem %s hết hạn đăng ký", ip)
        if expired:
            dests = sorted(set(a.dest) | set(subs))
        if now >= next_stats:
            dt = a.stats_every + (now - next_stats)
            log.info("video %.0f gói/s, toạ độ %.0f gói/s, gửi tới %s, lỗi gửi %d",
                     count["video"] / dt, count["telem"] / dt, ",".join(dests) or "(chưa có máy xem)", errors)
            count = {k: 0 for k in rx}
            next_stats = now + a.stats_every


if __name__ == "__main__":
    main()

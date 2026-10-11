#!/usr/bin/env python3
"""Trạm mặt đất: chuyển tiếp NGUYÊN VẸN video + toạ độ từ UAV tới máy xem (laptop giải mã + vẽ box).

  udp 127.0.0.1:5600  RTP H.264 từ wfb-ng (profile gs, gs_video)  → <máy xem>:5600
  udp 0.0.0.0:5700    JSON "uav_track" từ UAV qua tunnel wfb-ng    → <máy xem>:5700

Không giải mã / vẽ / nén lại → thêm < 1 ms trễ.

Máy xem: --dest IP cố định, và/hoặc TỰ ĐĂNG KÝ: máy xem gửi 1 gói UDP bất kỳ tới cổng
--subscribe-port (mặc định 5599) mỗi ~1 s → được gửi tới IP đó; hết hạn sau --subscribe-ttl giây
không nhận được gói. Laptop đổi IP (DHCP) không cần sửa gì trên Radxa.
"""
import argparse
import logging
import selectors
import socket
import time

log = logging.getLogger("gs_forward")


def udp_rx(addr):
    host, port = addr.rsplit(":", 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    s.bind((host, int(port)))       # không SO_REUSEADDR: chạy trùng 2 bản thì báo lỗi rõ ràng
    s.setblocking(False)
    return s


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dest", action="append", default=[], metavar="IP", help="máy xem cố định (lặp lại được)")
    p.add_argument("--subscribe-port", type=int, default=5599, help="cổng tự đăng ký (0 = tắt)")
    p.add_argument("--subscribe-ttl", type=float, default=5.0)
    p.add_argument("--video-in", default="127.0.0.1:5600")
    p.add_argument("--telem-in", default="0.0.0.0:5700")
    p.add_argument("--video-out-port", type=int, default=5600)
    p.add_argument("--telem-out-port", type=int, default=5700)
    p.add_argument("--stats-every", type=float, default=60)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    sel = selectors.DefaultSelector()
    streams = {"video": (udp_rx(a.video_in), a.video_out_port),
               "telem": (udp_rx(a.telem_in), a.telem_out_port)}
    for name, (s, _) in streams.items():
        sel.register(s, selectors.EVENT_READ, name)
    if a.subscribe_port:
        sel.register(udp_rx(f"0.0.0.0:{a.subscribe_port}"), selectors.EVENT_READ, "sub")
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    tx.setblocking(False)

    subs = {}                                   # IP tự đăng ký → hạn chót (monotonic)
    targets = {}                                # name → [(ip, port), ...] tính sẵn

    def update_targets():
        ips = sorted(set(a.dest) | set(subs))
        for name, (_, port) in streams.items():
            targets[name] = [(ip, port) for ip in ips]
        return ips

    update_targets()
    buf = bytearray(65536)
    view = memoryview(buf)
    count = dict.fromkeys(streams, 0)
    errors = 0
    log.info("video %s → :%d, toạ độ %s → :%d; máy xem cố định: %s; tự đăng ký: cổng %s",
             a.video_in, a.video_out_port, a.telem_in, a.telem_out_port,
             ", ".join(a.dest) or "không", a.subscribe_port or "tắt")

    next_expire = time.monotonic()
    next_stats = next_expire + a.stats_every
    while True:
        for key, _ in sel.select(timeout=1.0):
            s, name = key.fileobj, key.data
            if name == "sub":                   # máy xem đăng ký / gia hạn
                while True:
                    try:
                        _, (ip, _port) = s.recvfrom(64)
                    except BlockingIOError:
                        break
                    if ip not in subs:
                        log.info("máy xem %s đăng ký", ip)
                        subs[ip] = 0
                        update_targets()
                    subs[ip] = time.monotonic() + a.subscribe_ttl
                continue
            dsts = targets[name]
            while True:                         # xả hết gói đang chờ
                try:
                    n = s.recv_into(buf)
                except BlockingIOError:
                    break
                count[name] += 1
                pkt = view[:n]
                for d in dsts:
                    try:
                        tx.sendto(pkt, d)
                    except OSError:             # máy xem tắt / rút dây: bỏ gói, không dừng
                        errors += 1

        now = time.monotonic()
        if now >= next_expire:
            next_expire = now + 1.0
            gone = [ip for ip, t in subs.items() if t < now]
            for ip in gone:
                del subs[ip]
                log.info("máy xem %s hết hạn đăng ký", ip)
            if gone:
                update_targets()
        if now >= next_stats:
            if count["video"] or count["telem"] or errors:
                dt = a.stats_every
                log.info("video %.0f gói/s, toạ độ %.0f gói/s → %s, lỗi gửi %d",
                         count["video"] / dt, count["telem"] / dt,
                         ", ".join(ip for ip, _ in targets["video"]) or "(chưa có máy xem)", errors)
            count = dict.fromkeys(streams, 0)
            errors = 0
            next_stats = now + a.stats_every


if __name__ == "__main__":
    main()

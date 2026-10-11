#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
udp_loss_proxy.py — proxy UDP bỏ ngẫu nhiên gói (thay clumsy) để thử uav_viewer.py khi mất gói.

    python udp_loss_proxy.py --listen 5610 --to 127.0.0.1:5600 --loss 3
    ffmpeg -re -f lavfi -i testsrc2=size=1920x1080:rate=30 -c:v libx264 -preset ultrafast ^
           -tune zerolatency -g 30 -bf 0 -b:v 8M -f rtp rtp://127.0.0.1:5610
"""
import argparse
import random
import socket
import time


def main():
    p = argparse.ArgumentParser(description="Proxy UDP bỏ ngẫu nhiên gói để giả lập mất gói WiFi")
    p.add_argument("--listen", type=int, default=5610, help="cổng nhận (mặc định 5610)")
    p.add_argument("--to", default="127.0.0.1:5600", help="đích host:port (mặc định 127.0.0.1:5600)")
    p.add_argument("--loss", type=float, default=3.0, help="phần trăm gói bị bỏ (mặc định 3)")
    p.add_argument("--burst", type=int, default=1, help="mỗi lần mất thì mất liền N gói (mặc định 1)")
    a = p.parse_args()
    host, port = a.to.rsplit(":", 1)
    dst = (host, int(port))

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    rx.bind(("0.0.0.0", a.listen))
    rx.settimeout(0.5)               # để Ctrl+C dừng được cả khi không có gói
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"UDP :{a.listen} → {host}:{port}, bỏ {a.loss}% gói (burst {a.burst}). Ctrl+C để dừng.")

    total = dropped = skip = 0
    next_print = time.time() + 2.0
    try:
        while True:
            try:
                pkt = rx.recv(65536)
            except socket.timeout:
                continue
            total += 1
            if skip > 0:
                skip -= 1
                dropped += 1
            elif random.random() * 100.0 < a.loss:
                skip = a.burst - 1
                dropped += 1
            else:
                tx.sendto(pkt, dst)
            if time.time() >= next_print:
                print(f"{total} gói, đã bỏ {dropped} ({100.0 * dropped / total:.2f}%)")
                next_print += 2.0
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

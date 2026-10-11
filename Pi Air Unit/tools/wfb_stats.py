#!/usr/bin/env python3
"""Đo luồng video wfb-ng phía drone: bitrate encoder, gói lên sóng, drop, hàng đợi UDP.

Đọc JSON API của wfb-ng (profile drone, cổng 8102) mỗi giây + Recv-Q của socket UDP 5602.
Trần sóng MCS1/20MHz đo được ~998 gói/s: dùng >~80% là sắp nghẽn; drop > 0 hoặc hàng đợi
tăng là đã nghẽn (gói mất trước FEC + trễ tăng) -> hạ --bitrate hoặc tăng mcs_index.

    python3 tools/wfb_stats.py [số giây đo=15] [số giây bỏ qua lúc đầu=0]
"""
import json
import re
import socket
import subprocess
import sys

AIR_CEILING_PPS = 998      # đo trên Pi: MCS1, 20 MHz, long GI, gói ~1400 byte

dur = int(sys.argv[1]) if len(sys.argv) > 1 else 15
skip = int(sys.argv[2]) if len(sys.argv) > 2 else 0


def recv_q(port=5602):
    out = subprocess.run(["ss", "-ulnH", f"sport = :{port}"], capture_output=True, text=True).stdout
    m = re.search(r"UNCONN\s+(\d+)", out)
    return int(m.group(1)) if m else -1


rows, seen = [], 0
with socket.create_connection(("127.0.0.1", 8102), timeout=5) as s:
    for line in s.makefile():
        m = json.loads(line)
        if m.get("type") != "tx" or m["id"] != "video tx":
            continue
        p = m["packets"]
        if p["incoming"][0] == 0:          # chưa có video vào wfb-ng
            continue
        seen += 1
        if seen <= skip:
            continue
        lat = (m["tx_ant_stats"] or [{}])[0].get("lat_avg", 0)
        rows.append((p["incoming"][0], p["incoming_bytes"][0] * 8 / 1e6, p["injected"][0],
                     p["dropped"][0], lat, recv_q()))
        if len(rows) >= dur:
            break

print(f"{'vào gói':>7} {'vào Mbps':>8} {'sóng gói/s':>10} {'drop':>5} {'trễ inject µs':>13} {'hàng đợi B':>11}")
for r in rows:
    print(f"{r[0]:7} {r[1]:8.2f} {r[2]:10} {r[3]:5} {r[4]:13} {r[5]:11}")
n = len(rows)
pps = sum(r[2] for r in rows) / n
drops = sum(r[3] for r in rows)
print(f"\nTB {n}s: encoder {sum(r[1] for r in rows) / n:.2f} Mbps | sóng {pps:.0f} gói/s "
      f"({100 * pps / AIR_CEILING_PPS:.0f}% trần) | drop {drops} gói "
      f"({100 * drops / max(1, sum(r[0] for r in rows)):.1f}%) | hàng đợi max {max(r[5] for r in rows)} B")

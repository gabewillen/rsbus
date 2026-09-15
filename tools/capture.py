#!/usr/bin/env python3
"""capture.py — host-side reader for RSBus capture streams.

Primary source: Waveshare 2-CH CAN HAT (MCP2515 + SN65HVD230) mounted on the
Raspberry Pi via socketcan. `--candump can0` spawns `candump -L can0`
(can-utils) and parses kernel candump-log lines; anything piped to stdin in
the same format is also accepted. Decode/stats primitives live in
`rsbus.capture` so `rsbus monitor` shares this exact behavior.

Usage:
    sudo apt install can-utils          # one-time
    sudo ip link set can0 up type can bitrate 40000 listen-only on
    python tools/capture.py --candump can0 --stats
    python tools/capture.py --candump can0 --out bus.jsonl --quiet
    candump -L can0 | python tools/capture.py --stats

Wire format (candump log lines):
    (ts) <iface> <id>#<hex-data>
    (ts) <iface> <id>#R<dlc>            remote frame
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

# Run from the repo without an installed package:
if __package__ in (None, ""):
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from rsbus.capture import BusStats, parse_candump


def iter_candump(iface: str):
    proc = subprocess.Popen(
        ["candump", "-L", iface],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            yield raw
    finally:
        proc.terminate()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--candump", metavar="IFACE", help="spawn `candump -L IFACE` (e.g. can0)")
    ap.add_argument("--out", help="write a JSONL event log here")
    ap.add_argument("--stats", action="store_true", help="print per-ID stats at exit")
    ap.add_argument("--quiet", action="store_true", help="do not print events live")
    args = ap.parse_args(argv)

    if args.candump:
        stream = iter_candump(args.candump)
    else:
        stream = sys.stdin

    log = open(args.out, "a", encoding="utf8") if args.out else None
    stats = BusStats()
    n_events = 0
    try:
        for raw in stream:
            if not raw or not raw.strip():
                continue
            event = parse_candump(raw)
            if event is None:
                if not args.quiet:
                    print(f"?? {raw.rstrip()}", file=sys.stderr)
                continue
            n_events += 1
            if log:
                log.write(json.dumps(event) + "\n")
                log.flush()
            stats.observe(event)
            if not args.quiet:
                data = event["data"]
                pretty = " ".join(data[i : i + 2] for i in range(0, len(data), 2)) or "-"
                rtr = " rtr" if event["flags"] else ""
                print(
                    f"{event['t']:.6f} {event['iface']} {event['kind']}{rtr} {event['dlc']} {pretty}",
                    file=sys.stderr,
                )
    except KeyboardInterrupt:
        pass
    finally:
        if log:
            log.close()
        if args.stats:
            print(stats.summary(), file=sys.stderr)
        print(f"captured {n_events} events", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

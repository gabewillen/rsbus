"""rsbus — Lennox RSBus (Residential Serial Bus) protocol implementation.

CLI:
    rsbus monitor [--iface can0] [--learn PATH] [--jsonl PATH]
    rsbus node --role device|sc [--iface can0] [--dd 0x...] [--fast] ...

The models in src/rsbus implement the FIG. 12 device startup, the SC
coordinator election (FIG. 13A/13B/13C) and the FIG. 14 Equipment Type
assignment per `docs/rsbus-spec.pdf` (US 2010/0106322 A1), riding the
Waveshare 2-CH CAN HAT socketcan path (docs/REVERSE-ENGINEERING.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time


def _monitor(args) -> int:
    import can

    from . import messages as m

    async def main() -> int:
        from .capture import BusStats, decode_frame

        # 'can*' names mean Linux socketcan; anything else rides python-can's
        # in-process 'virtual' backend, where args.iface is the channel name.
        if args.iface.startswith("can"):
            iface, channel = "socketcan", args.iface
        else:
            iface, channel = "virtual", args.iface
        bus = can.Bus(interface=iface, channel=channel)
        if getattr(args, "message_map", None):
            applied = m.set_message_map(args.message_map)
            print(f"message map override applied: {applied}", file=sys.stderr)
        learn = m.MessageNameLearner(args.learn)
        jsonl = open(args.jsonl, "a") if args.jsonl else None
        stats = BusStats()
        start = time.monotonic()
        count = 0
        try:
            while True:
                frame = await asyncio.to_thread(bus.recv, 0.25)
                if frame is None:
                    if args.duration and time.monotonic() - start >= args.duration:
                        break
                    continue
                count += 1
                event = decode_frame(frame)
                stats.observe(event)
                decoded = m.decode_msg(frame.arbitration_id, bytes(frame.data))
                learn.observe(decoded)
                if not args.quiet:
                    print(m.pretty_decode(m.decode_msg(frame.arbitration_id, bytes(frame.data))))
                if jsonl:
                    jsonl.write(json.dumps(event) + "\n")
                    jsonl.flush()
        except KeyboardInterrupt:
            pass
        finally:
            bus.shutdown()
            learn.flush()
            if jsonl:
                jsonl.close()
            print(f"\n{count} frames captured; learned map flushed", file=sys.stderr)
            if args.stats:
                print(stats.summary(), file=sys.stderr)
        return 0

    return asyncio.run(main())


def _node(args) -> int:
    import contextlib

    from .app import run

    if getattr(args, "message_map", None):
        from . import messages as m
        m.set_message_map(args.message_map)

    dd = int(args.dd, 0)
    run(role=args.role, iface=args.iface, channel=args.channel, dd=dd,
        state_dir=args.state_dir, fast=args.fast,
        spl=args.spl, dpl=args.dpl, prn=args.prn, subnet=args.subnet,
        link_local=args.link_local, et=args.et)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="rsbus", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    mon = sub.add_parser("monitor", help="passive listener with full field decode")
    mon.add_argument("--iface", default="can0")
    mon.add_argument("--learn", help="write learned unknown-MID counts here")
    mon.add_argument("--message-map", help="JSON message-map override (real MID numbering from capture)")
    mon.add_argument("--jsonl", help="raw JSONL event log (one per frame)")
    mon.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = run until Ctrl-C)")
    mon.add_argument("--stats", action="store_true", help="per-ID period stats at exit")
    mon.add_argument("--quiet", action="store_true", help="no live decode lines")
    mon.set_defaults(func=_monitor)

    node = sub.add_parser("node", help="run a participating RSBus node (bench only)")
    node.add_argument("--role", choices=["device", "sc"], default="device")
    node.add_argument("--iface", default="can0", help="python-can interface (socketcan 'can0'/'can1' or 'virtual')")
    node.add_argument("--channel", default=None, help="channel for virtual interface (default: iface)")
    node.add_argument("--dd", default=None, help="device designator hex")
    node.add_argument("--state-dir", default=".rsbus-state")
    node.add_argument("--fast", action="store_true", help="compressed bench timings")
    node.add_argument("--spl", type=int, default=0)
    node.add_argument("--dpl", type=int, default=0)
    node.add_argument("--prn", type=int, default=0)
    node.add_argument("--subnet", type=int, default=0)
    node.add_argument("--message-map", default=None, help="JSON message-map override path (real MID numbering)")
    node.add_argument("--et", type=int, default=None, help="Equipment Type to announce (Table I; default 0x70 = UI class)")
    node.add_argument("--link-local", action="store_true", help="FIG. 13C-1e configuration-mode link-relay stand-in")
    node.set_defaults(func=_node)

    args = ap.parse_args()
    if args.cmd == "node" and args.channel is None:
        args.channel = args.iface if args.iface == "virtual" else args.iface
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()

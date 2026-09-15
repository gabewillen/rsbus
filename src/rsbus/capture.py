"""Shared capture primitives: candump-style frame decode + per-ID stats.

Used by both `rsbus monitor` (interactive, full field decode) and
`tools/capture.py` (the piped candump-log reader). The per-ID period stats
feed the ¶0110 heartbeat three-missed-period alarm analysis after a session.
"""

from __future__ import annotations

import re
from collections import defaultdict

from . import messages as m

CANDUMP_RE = re.compile(
    r"^\((?P<ts>\d+\.\d+)\) (?P<iface>\S+) "
    r"(?P<id>[0-9A-Fa-f]+)#(?P<rest>R[0-8]?|[0-9A-Fa-f]*)$"
)


def decode_frame(frame) -> dict:
    """Unified event dict for one received CAN frame."""
    return {
        "type": "frame",
        "t": float(frame.timestamp or 0.0),
        "iface": frame.channel,
        "kind": "ext" if frame.is_extended_id else "std",
        "flags": [],
        "dlc": len(frame.data),
        "data": bytes(frame.data).hex().upper(),
        "arbitration_id": frame.arbitration_id,
    }


def parse_candump(raw: str):
    """Parse one `candump -L` log line into the unified event dict."""
    m = CANDUMP_RE.match(raw.strip())
    if not m:
        return None
    id_s = m.group("id")
    ext = len(id_s) > 3
    rtr = False
    dlc = 0
    data_hex = ""
    rest = m.group("rest")
    if rest.startswith("R"):
        rtr = True
        dlc = int(rest[1:]) if len(rest) > 1 else 0
    elif rest:
        dlc = len(rest) // 2
        data_hex = rest.upper()
    return {
        "type": "frame",
        "t": float(m.group("ts")),
        "kind": "ext" if ext else "std",
        "iface": m.group("iface"),
        "flags": ["rtr"] if rtr else [],
        "dlc": dlc,
        "data": data_hex,
    }


class BusStats:
    """Per-ID timing aggregates. Gap stats matter for the aSC heartbeat
    alarm rule (alarm when the heartbeat is missing 3x its send period,
    spec ¶0110)."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)
        self.last_t: dict[str, float] = {}
        self.gaps: dict[str, list[float]] = defaultdict(list)

    def observe(self, frame: dict) -> tuple[str, float] | None:
        key = f"{frame['iface']}:{frame['kind']}:{frame['data'][:48]}@{frame['flags']}"
        prev = self.last_t.get(key)
        gap = frame["t"] - prev if prev is not None else None
        self.last_t[key] = frame["t"]
        self.counts[key] += 1
        if gap is not None:
            self.gaps[key].append(gap)
            del self.gaps[key][:-64]
        return (key, gap) if gap is not None else None

    def summary(self) -> str:
        lines = ["== bus stats =="]
        now_t = max((self.last_t.get(k, 0.0) for k in self.last_t), default=0.0)
        for key, n in sorted(self.counts.items(), key=lambda kv: -kv[1]):
            g = self.gaps.get(key) or []
            est = (sum(g) / len(g)) if g else 0.0
            age = now_t - self.last_t.get(key, 0.0)
            lines.append(
                f"  {key:64s} n={n:<6d} period~{est*1e3:8.0f}ms age={age*1e3:8.0f}ms"
            )
        return "\n".join(lines)

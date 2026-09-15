"""Node runtime — per-FFSM instance context shared by all behaviors.

A `NodeRuntime` (subclass of hsm.Instance) carries what every behavior stub
needs: the transport handle, node identity config, RAM tables (SC RAM list,
device RAM list), persisted NVM state (enable/disable, equipment type and
subnet assignments), the frames-ledger, and helpers for addressing peers.

Two instances in play:
- `DeviceRuntime` — a local controller per FIG. 12
- `SubnetRuntime` — a subnet controller per FIG. 13A/13B/13C
- `AssignmentRuntime` — a FIG. 14 child of the aSC (spawned per unknown-ET
  device with `host` pointing at the SubnetRuntime)
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import hsm

from . import messages as m


DEFAULT_LISTEN_MS = 5000
DEFAULT_REPEAT_MS = 5 * 60 * 1000
DEFAULT_HEARTBEAT_PERIOD_MS = 60000
DEFAULT_TAKEOVER_GRACE_MS = 1000
DEFAULT_CFG_CHANGE_STATE_DELAY_MS = 500


def order_number_for_dd(dd: int) -> int:
    """DD → 5-bit order number in class-5 IDs (spec ¶0154: ORDERNUMBER =
    last 5 bits of the DD; class-5 sends it left-shifted one position past
    the TP bit per ¶0185 — messages.build_class5_id handles the slot)."""
    return dd & 0x1F


@dataclass
class NodeConfig:
    role: str = "device"          # 'device' | 'sc'
    dd: int = 0x0A_B0_00_01       # device designator (LSBs feed order numbers)
    subnet: int = 0               # our subnet id
    et: int = 0                   # SC=0, other devices seeded at 0 until assigned
    iface: str = "virtual"
    channel: str = "can0"
    fast: bool = False            # shortened timings (bench/tests)
    listen_ms: int = DEFAULT_LISTEN_MS
    repeat_ms: int = DEFAULT_REPEAT_MS
    heartbeat_period_ms: int = DEFAULT_HEARTBEAT_PERIOD_MS
    takeover_grace_ms: int = DEFAULT_TAKEOVER_GRACE_MS
    cfg_change_state_delay_ms: int = DEFAULT_CFG_CHANGE_STATE_DELAY_MS
    splash_mode: bool = False     # false → CONFIGURATION, true → VERIFICATION mode at aSC layer
    state_dir: str = ".rsbus-state"
    # ordered credential inputs (¶0173-¶0178) for OUR node
    spl: int = 0
    dpl: int = 0
    prn: int = 0
    # CONFIGURATION-mode link-relay stand-in for FIG. 13C-1e row 10
    link_local: bool = False
    # FIG. 13C-1a POST passive variant: re-send SC_STARTUP on the 5-minute
    # timeout instead of resetting (approval decision; default reset).
    passive_resend: bool = False
    # FIG. 14 capacity clamp (Table I example: 32 SCs)
    max_devices: int = 32
    trace: bool = False            # print every TX frame (bench/diagnose)
    et_request: int = 0x70         # the class ET this device announces (Table I):
                                   # 0x70-0x7B = UI (the useful role for HA / app
                                   # integration — class 1 UIG messages give you
                                   # parameter read/write and alert access);
                                   # 0x40+ = comfort sensor; 0x10-0x1F = HVAC equipment
    # TX serialization: sync behaviors queue frames here; a pump drains them.
    tx_queue_level: int = 0

    def listen(self) -> float:
        return (0.2 if self.fast else self.listen_ms / 1000.0)

    def repeat(self) -> float:
        return 8.0 if self.fast else self.repeat_ms / 1000.0

    def heartbeat_period(self) -> float:
        return 0.4 if self.fast else self.heartbeat_period_ms / 1000.0

    def takeover_grace(self) -> float:
        return 0.2 if self.fast else self.takeover_grace_ms / 1000.0

    def change_state_delay(self) -> float:
        return 0.2 if self.fast else self.cfg_change_state_delay_ms / 1000.0


class SCInfo:
    """One entry in the aSC/SC RAM lists (FIG. 13C-5, 8 bytes per SC)."""

    __slots__ = ("dd", "spl", "dpl", "prn", "subnet", "cf1_valid", "cf1_flag", "cf0_flag", "last_seen")

    def __init__(self, dd: int, spl: int = 0, dpl: int = 0, prn: int = 0, subnet: int = 0):
        self.dd = dd
        self.spl = spl
        self.dpl = dpl
        self.prn = prn
        self.subnet = subnet
        self.cf1_valid = False   # set True when that SC's DECLARATION seen (13C-1b row 5)
        self.cf1_flag = 0        # SC sees indoor units on its subnet? (¶0206)
        self.cf0_flag = 0        # SC configured? (¶0204)
        self.last_seen = time.monotonic()

    def credentials(self) -> tuple[int, int, int, int]:
        """Ordered per ¶0173-¶0178: SPL then DPL then PRN then DD."""
        return (self.spl, self.dpl, self.prn, self.dd)


class DeviceInfo:
    __slots__ = ("dd", "et", "subnet", "assigned", "soft_disabled", "last_seen", "cf")

    def __init__(self, dd: int, et: int = 0, subnet: int = 0):
        self.dd = dd
        self.et = et
        self.subnet = subnet
        self.assigned = False
        self.soft_disabled = False
        self.last_seen = time.monotonic()
        self.cf = m.CF3_ENABLED | m.CF4_NOT_SOFT_DISABLED         # enabled/comm


class Persistence:
    """NVM stand-in (JSON file). HARD DISABLED state and assigned ET/subnet
    persist here across resets (¶0136, ¶0155); SOFT DISABLED stays RAM-only
    (¶0137)."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.data: dict = json.loads(self.path.read_text())
        else:
            self.data = {}

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.flush()

    def flush(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))


class NodeRuntime(hsm.Instance):
    """Base runtime wired by NodeApplication (app.py) before Start."""

    def __init__(self) -> None:
        super().__init__()
        self.cfg = NodeConfig()
        self.transport: "Any" = None
        self.nvm = Persistence(Path(self.cfg.state_dir) / f"{self.cfg.role}-{self.cfg.dd:08X}.json")
        # Sync behavior hooks (Entry/Exit/Effect are synchronous) push frames
        # here and the tx pump drains them through the transport.
        self.tx_queue: asyncio.Queue | None = None
        # RAM: cleared on reset semantics
        self.ram: dict[str, object] = {}
        # aSC-side tables
        self.scram: dict[int, SCInfo] = {}
        self.device_ram: dict[int, DeviceInfo] = {}
        # last remembered aSC identity (passive/inactive possession)
        self.last_asc: SCInfo | None = None
        self.last_asc_time: float = 0.0
        self.last_rival_coordinator_time: float = 0.0
        self.assigned_et: int = 0
        self.assigned_subnet: int = 0
        self.soft_disabled: bool = False
        self.shutdown = False

    # ------------------------------------------------------------- address ----
    def is_iu(self, et: int) -> bool:
        """An indoor unit? Furnace/air-handler/evaporator equipment types
        (FIG. 13C-1b rows 2/3 'IF IU' rule). Assignments per Table I for
        Furnace/AirHandler/AC/HP sit in 0x10..0x15."""
        return 0x10 <= et <= 0x1F

    def remember_asc(self, dd: int, spl: int = 0, dpl: int = 0, prn: int = 0, subnet: int = 0) -> None:
        info = self.scram.get(dd) or SCInfo(dd)
        info.spl, info.dpl, info.prn, info.subnet = spl, dpl, prn, subnet
        info.last_seen = time.monotonic()
        self.scram[dd] = info
        self.last_asc = info
        self.last_asc_time = time.monotonic()

    def touch_rival_coordinator(self) -> None:
        self.last_rival_coordinator_time = time.monotonic()

    def best_other_sc(self) -> SCInfo | None:
        ours = (self.cfg.spl if hasattr(self.cfg, "spl") else 0, self.cfg.dpl if hasattr(self.cfg, "dpl") else 0, self.cfg.prn if hasattr(self.cfg, "prn") else 0, self.cfg.dd)
        best = None
        for info in self.scram.values():
            if info.dd == self.cfg.dd:
                continue
            cred = info.credentials()
            if best is None or cred > best.credentials():
                best = info
        return best

    def we_outrank(self, spl: int, dpl: int, prn: int, dd: int) -> bool:
        ours = (self.cfg.spl, self.cfg.dpl, self.cfg.prn, self.cfg.dd)
        return ours > (spl, dpl, prn, dd)

    # ------------------------------------------------------------ transport ----
    def queue_send(self, arb_id: int, data: bytes) -> None:
        """Sync-safe send: push to the TX queue (drained asynchronously by
        the application's pump task)."""
        if self.tx_queue is not None:
            self.tx_queue.put_nowait((arb_id, data))
            if getattr(self.cfg, "trace", False):
                print(f"[trace-put] {arb_id:#08X} {data.hex()}", file=sys.stderr)

    def send_named(self, name: str, fields: dict | None = None, *, ds: int = None, ss: int = None,
                   et: int = None, as_all: int = 0) -> None:
        """Fire-and-forget send keyed by message name. The arb payload et is
        the SOURCE equipment type for device-sourced messages and the
        destination ET for aSC-sourced commands. Callers may override ds/ss;
        defaults: ss = our subnet, ds = our subnet."""
        my_et = self.assigned_et if self.cfg.role == "device" else self.cfg.et
        if ds is None:
            ds = self.cfg.subnet if self.cfg.role == "sc" else self.assigned_subnet or self.cfg.subnet
        if ss is None:
            ss = self.cfg.subnet
        arb, data = m.encode_msg(
            name,
            fields or {},
            ds=ds & 0b11,
            ss=ss & 0b11,
            et=my_et & 0xFF,
            as_all=as_all,
        )
        self.queue_send(arb, data)

    # --------------------------------------------------------------- tasks ----
    def schedule(self, awaitable):
        """Run any coroutine OR awaitable as a fire-and-forget task. hsm.Dispatch
        returns an awaitable (not a coroutine) so both shapes are accepted."""
        if asyncio.iscoroutine(awaitable):
            return asyncio.get_running_loop().create_task(awaitable)

        async def _runner():
            await awaitable

        return asyncio.get_running_loop().create_task(_runner())


class DeviceRuntime(NodeRuntime):
    """A local controller per FIG. 12 (non-SC device)."""


class SubnetRuntime(NodeRuntime):
    """A subnet controller per FIG. 13A/13B/13C (SC role)."""


class AssignmentRuntime(NodeRuntime):
    """A FIG. 14 child machine bound to one unknown-ET device (spawned by
    the aSC's heartbeat_out state)."""

    host: "NodeRuntime | None" = None

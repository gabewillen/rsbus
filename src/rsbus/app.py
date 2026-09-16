"""NodeApplication — wires CAN transport, codecs and FFSMs into a running node.

Two roles (`--role`):
- `device`: a local controller running LocalControllerStartup (FIG. 12).
- `sc`: a subnet controller running SubnetControllerStartup
  (FIG. 13A/13B/13C) with FIG. 14 child machines for unknown-ET devices.

The transport binds one interface (`socketcan can0/can1` on the Pi, or
python-can's `virtual` in-process bus for the integration tests and bench
rehearsals). RX frames are decoded via the FIG. 7-11 codec and re-dispatched
into every started machine in the shared hsm.Context; model-driven TX goes
through a single queue drained into the transport here (the only writable
surface from sync Entry/Effect behaviors).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import time

import weakref

import hsm
from hsm import context as hsm_context

from . import messages as m
from .local_controller import LocalControllerStartup
from .runtime import NodeConfig, NodeRuntime
from .subnet_controller import SubnetControllerStartup
from .transport import CanTransport


class NodeApplication:
    def __init__(self, cfg: NodeConfig):
        self.cfg = cfg
        self.transport = CanTransport(interface=cfg.iface, channel=cfg.channel)
        # The hsm runtime writes each started machine into ctx[Keys.Instances]
        # ONLY as a derived child context; to keep one shared registry across
        # our host machine and any FIG. 14 children we seed the mapping here,
        # then every ChildContext writes into the SAME dict object.
        self._instances = weakref.WeakValueDictionary()
        self.ctx = hsm_context.new_context(
            values={hsm.Keys.Instances: self._instances}
        )
        self.instance: NodeRuntime | None = None
        self._stopped = asyncio.Event()
        self._tx_pump: asyncio.Task | None = None
        self._rx_task: asyncio.Task | None = None
        self._learn = m.MessageNameLearner(None)  # replaced by CLI flag

    # ---------------------------------------------------------------- setup ----
    def _build_instance(self) -> NodeRuntime:
        if self.cfg.role == "sc":
            from .runtime import SubnetRuntime
            inst = SubnetRuntime(cfg=self.cfg)
        else:
            from .runtime import DeviceRuntime
            inst = DeviceRuntime(cfg=self.cfg)
        inst.transport = self.transport
        inst.tx_queue = asyncio.Queue()
        inst.ram = {}
        return inst

    async def start(self) -> None:
        # RX loop first: the FSM behaviors expect the transport live before
        # the machine starts (FIG. 12/13A treat the bus as always-on).
        await self.transport.start()
        # Device role: run the alarm & diagnostics machine alongside the
        # startup FSM (US2010/0106310 — Level 1 diagnostics, alarm log).

        # Bind identity to the runtime BEFORE start so behaviors resolve.
        self.instance = self._build_instance()
        if self.cfg.role == "device":
            model = LocalControllerStartup
        else:
            model = SubnetControllerStartup
        await hsm.Started(
            self.ctx,
            self.instance,
            model,
            hsm.Config(ID=f"{self.cfg.role}-{self.cfg.dd:08X}"),
        )
        # Device role: run the alarm & diagnostics machine alongside the
        # startup FSM (US2010/0106310 — Level 1 diagnostics, alarm log),
        # plus the operations-layer companions (US8463443 — commissioning,
        # parameter-change dialog).
        if self.cfg.role == "device":
            from .diagnostics import DeviceDiagnostics
            from .operations import (DeviceCommissioning,
                                     ParameterChangeDialog)
            from .runtime import DeviceRuntime
            diag_inst = DeviceRuntime(cfg=self.cfg)
            diag_inst.transport = self.transport
            diag_inst.tx_queue = self.instance.tx_queue  # shared pump drains it
            diag_inst.ram = {}
            self._diag_app = _SubApp(diag_inst, DeviceDiagnostics, self.ctx)
            await self._diag_app.start()

            comm_inst = DeviceRuntime(cfg=self.cfg)
            comm_inst.transport = self.transport
            comm_inst.tx_queue = self.instance.tx_queue
            comm_inst.ram = {}
            param_inst = DeviceRuntime(cfg=self.cfg)
            param_inst.transport = self.transport
            param_inst.tx_queue = self.instance.tx_queue
            param_inst.ram = {}
            self._ops_apps = [_SubApp(comm_inst, DeviceCommissioning, self.ctx),
                              _SubApp(param_inst, ParameterChangeDialog, self.ctx)]
            for sub in self._ops_apps:
                await sub.start()

        self._tx_pump = asyncio.create_task(self._drain_tx())
        self._rx_task = asyncio.create_task(self._rx_forward())
        print(f"[node] {self.cfg.role} dd={self.cfg.dd:#010x} on "
              f"{self.cfg.iface}:{self.cfg.channel} started in "
              f"{self.instance.state()}", file=sys.stderr)

    async def stop(self) -> None:
        self._stopped.set()
        if getattr(self, "_diag_app", None):
            await self._diag_app.stop()
        for t in (self._rx_task, self._tx_pump):
            if t:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        if self.instance:
            with contextlib.suppress(Exception):
                await hsm.Stop(self.instance)
        await self.transport.stop()

    # ------------------------------------------------------------- plumbing ----
    async def _drain_tx(self) -> None:
        assert self.instance is not None
        q = self.instance.tx_queue
        while True:
            arb_id, data = await q.get()
            if self.cfg.trace:
                try:
                    d = m.decode_id(arb_id)
                    d["data_hex"] = data.hex().upper()
                    print(f"[trace-tx] {m.pretty_decode(d)} data={data.hex()}", file=sys.stderr)
                except Exception:
                    pass
            await self.transport.send_msg(arb_id, data)
            q.task_done()

    async def _rx_forward(self) -> None:
        """Decode CAN frames -> dispatch hsm events named after the message."""
        while not self._stopped.is_set():
            frame = await self.transport.next_frame(timeout=0.25)
            if frame is None:
                continue
            if self.cfg.trace:
                print(f"[trace-rx] id={frame.arbitration_id:#08X} {bytes(frame.data).hex()}", file=sys.stderr)
            decoded = m.decode_msg(frame.arbitration_id, bytes(frame.data))
            if self._learn is not None:
                self._learn.observe(decoded)
            payload_dict = dict(decoded.get("fields") or {})
            for extra_key in ("dd", "et", "ss", "ds", "c5mid", "c3mid",
                              "spl", "dpl", "prn", "cf", "uiid", "as_all",
                              "al", "src_et"):
                if extra_key in decoded:
                    payload_dict.setdefault(extra_key, decoded[extra_key])
            name = decoded.get("msg") or f"class{decoded['class']}_unknown"
            event_name = name.lower()
            # ASC_CHANGE_STATE aliases SC_CHANGE_STATE in messages.py, so
            # the runtime event stays the wire name; no rename needed.
            try:
                # Dispatch to every started machine in THIS context: the host
                # FSM plus any FIG. 14 child spawned by heartbeat_out.
                await hsm.DispatchAll(self.ctx,
                                      hsm.Event(name=event_name, data=payload_dict))
            except Exception as e:
                print(f"[node] dispatch failed for {event_name}: {e}", file=sys.stderr)

    async def run_forever(self) -> None:
        await self.start()
        try:
            await self._stopped.wait()
        finally:
            final_state = self.instance.state() if self.instance else "?"
            print(f"[node] stopped in state {final_state}", file=sys.stderr)


class _SubApp:
    """A secondary FSM running on the same node config and shared hsm
    Context (used for the device-side diagnostics machine)."""

    def __init__(self, inst, model, shared_ctx):
        self.inst = inst
        self.model = model
        self.ctx = shared_ctx
        self.tx_queue = inst.tx_queue

    async def start(self):
        await hsm.Started(self.ctx, self.inst, self.model,
                          hsm.Config(ID=f"diag-{self.inst.cfg.dd:08X}"))

    async def stop(self):
        with contextlib.suppress(Exception):
            await hsm.Stop(self.inst)


def run(role: str, iface: str, channel: str, dd: int = None, state_dir: str = None,
        fast: bool = False, spl: int = 0, dpl: int = 0, prn: int = 0,
        subnet: int = 0, link_local: bool = False, et: int = None):
    """Synchronous entrypoint used by the CLI."""
    cfg = NodeConfig(role=role, iface=iface, channel=channel,
                     dd=dd if dd is not None else (0x0A000001 if role == "device" else 0x0C000001),
                     state_dir=state_dir or ".rsbus-state",
                     fast=fast, spl=spl, dpl=dpl, prn=prn, subnet=subnet,
                     link_local=link_local,
                     et_request=et if et is not None else 0x70)

    async def _main() -> None:
        app = NodeApplication(cfg)
        loop = asyncio.get_running_loop()
        stop_signals = (signal.SIGINT, signal.SIGTERM)
        for sig in stop_signals:
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, app._stopped.set)

        async def state_watchdog() -> None:
            # Light-touch commentary for the bench: print every state change.
            # Exit naturally when stopped rather than relying on cancel.
            last = ""
            while True:
                try:
                    await asyncio.wait_for(app._stopped.wait(), timeout=1.0)
                    break
                except asyncio.TimeoutError:
                    pass
                if app.instance:
                    now = app.instance.state()
                    if now != last:
                        print(f"[node] state -> {now}", file=sys.stderr)
                        last = now

        watchdog = asyncio.create_task(state_watchdog())
        try:
            await app.run_forever()
        finally:
            # state_watchdog exits on its own once _stopped is set; no cancel
            # needed (and a premature cancel would race its natural exit).
            with contextlib.suppress(Exception):
                await app.stop()

    asyncio.run(_main())

"""socketcan transport for RSBus nodes (Waveshare 2-CH CAN HAT path).

Wraps python-can. The RX thread is driven through a single asyncio task
that polls `bus.recv()`; every frame becomes an `asyncio.Queue` item for
the application dispatcher. `interface="virtual"` is the same stack against
python-can's in-process bus, which is how the integration tests run the
device + SC handshake without hardware.
"""

from __future__ import annotations

import asyncio
import sys

import can

from . import messages


class CanTransport:
    def __init__(self, interface: str = "socketcan", channel: str = "can0"):
        self.interface = interface
        self.channel = channel
        self.bus: can.BusABC = can.Bus(interface=interface, channel=channel)
        self.rx_queue: asyncio.Queue[can.Message] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self.tx_frames = 0
        self.rx_frames = 0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._rx_loop())

    async def _rx_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._closing:
            try:
                msg = await loop.run_in_executor(None, self.bus.recv, 0.05)
            except can.CanError:
                msg = None
            if msg is not None:
                self.rx_frames += 1
                self.rx_queue.put_nowait(msg)
        await asyncio.to_thread(self.bus.shutdown)

    async def send_msg(self, arb_id: int, data: bytes, *, listen_print: bool = False) -> None:
        msg = can.Message(arbitration_id=arb_id, data=data, is_extended_id=True)
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.bus.send, msg)
            self.tx_frames += 1
        except can.CanError as e:
            # A send failure on a passive bus must not kill the node (spec
            # ¶0169: one attempt per scheduled slot; the next scheduled slot
            # triggers a new attempt).
            print(f"[transport] send failed: {e}", file=sys.stderr)

    async def next_frame(self, timeout: float | None = None) -> can.Message | None:
        try:
            return await asyncio.wait_for(self.rx_queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def stop(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        try:
            await asyncio.to_thread(self.bus.shutdown)
        except Exception:
            pass

    def bus_state(self) -> str:
        """('ERROR_ACTIVE', 'ERROR_PASSIVE', 'BUS_OFF') per kernel counters —
        consumed by CANErrorConfinement (¶0083-¶0086)."""
        try:
            return str(self.bus.state)
        except Exception:
            return "ERROR_ACTIVE"

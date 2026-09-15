"""CanTransport — virtual bus send/receive."""

import asyncio

import can
import pytest

from rsbus.transport import CanTransport


async def test_transport_send_and_receive():
    tx = CanTransport(interface="virtual", channel="tt-test")
    rx = CanTransport(interface="virtual", channel="tt-test")
    await tx.start()
    await rx.start()
    await tx.send_msg(0x14204000, b"\x01\x02")
    frame = await rx.next_frame(timeout=2.0)
    assert frame is not None
    assert frame.arbitration_id == 0x14204000
    await tx.stop()
    await rx.stop()


async def test_transport_next_frame_timeout():
    t = CanTransport(interface="virtual", channel="tt-tmo")
    await t.start()
    frame = await t.next_frame(timeout=0.05)
    assert frame is None
    await t.stop()


def test_transport_bus_state():
    t = CanTransport(interface="virtual", channel="tt-state")
    state = t.bus_state()
    # virtual buses may return any state string; just verify no crash
    assert isinstance(state, str)

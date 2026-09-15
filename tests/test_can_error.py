"""CAN fault-confinement FSM — full coverage (¶0083-¶0086)."""

import asyncio

import hsm
import pytest

from rsbus.can_error import CANErrorConfinement
from rsbus.runtime import NodeConfig, NodeRuntime


class CEInst(NodeRuntime):
    pass


def _inst(tmp_path):
    i = CEInst()
    i.cfg = NodeConfig(role="device", dd=0x0A_B0_00_02, iface="virtual",
                       channel="canerr", state_dir=str(tmp_path), fast=True)
    i.ram = {}
    i.tx_queue = asyncio.Queue()
    return i


async def test_starts_in_error_active(tmp_path):
    i = _inst(tmp_path)
    await hsm.Started(None, i, CANErrorConfinement, hsm.Config(ID="ce1"))
    assert i.state().endswith("error_active")
    await hsm.Stop(i)


async def test_error_active_to_error_passive(tmp_path):
    """17 errors × 8 = 136 > 127 → error_passive (¶0084)."""
    i = _inst(tmp_path)
    await hsm.Started(None, i, CANErrorConfinement, hsm.Config(ID="ce-ep"))
    for _ in range(17):
        await hsm.Dispatch(None, i, hsm.Event(name="bus_error", data={"state": "ERROR_ACTIVE"}))
        await asyncio.sleep(0.01)
    tx, _ = i.Get("tx_error_count")
    assert tx > 127
    assert i.state().endswith("error_passive")
    await hsm.Stop(i)


async def test_error_passive_to_bus_off(tmp_path):
    """More errors → tx > 255 → bus_off (¶0085)."""
    i = _inst(tmp_path)
    await hsm.Started(None, i, CANErrorConfinement, hsm.Config(ID="ce-bo"))
    # fast path: 33 errors × 8 = 264 > 255
    for _ in range(33):
        await hsm.Dispatch(None, i, hsm.Event(name="bus_error", data={"state": "ERROR_ACTIVE"}))
        await asyncio.sleep(0.005)
    assert i.state().endswith("bus_off")
    await hsm.Stop(i)


async def test_bus_off_recovers_after_timer(tmp_path):
    """5-minute recovery timer (fast: 1 s) clears the tx count (¶0086)."""
    i = _inst(tmp_path)
    await hsm.Started(None, i, CANErrorConfinement, hsm.Config(ID="ce-rec"))
    for _ in range(40):
        await hsm.Dispatch(None, i, hsm.Event(name="bus_error", data={"state": "ERROR_ACTIVE"}))
        await asyncio.sleep(0.005)
    assert i.state().endswith("bus_off")
    # wait for the 1-second fast-mode recovery timer
    await asyncio.sleep(1.5)
    assert i.state().endswith("error_active")
    tx, _ = i.Get("tx_error_count")
    assert tx == 0  # cleared on reboot (¶0086)
    await hsm.Stop(i)


async def test_rx_error_count_also_triggers_passive(tmp_path):
    """The rx count can independently exceed the lower limit (¶0084)."""
    i = _inst(tmp_path)
    await hsm.Started(None, i, CANErrorConfinement, hsm.Config(ID="ce-rx"))
    for _ in range(17):
        await hsm.Dispatch(None, i, hsm.Event(name="bus_error", data={"state": "ERROR_PASSIVE"}))
        await asyncio.sleep(0.005)
    rx, _ = i.Get("rx_error_count")
    assert rx > 127
    assert i.state().endswith("error_passive")
    await hsm.Stop(i)


async def test_reboot_bus_clears_tx_count(tmp_path):
    """reboot_bus clears the tx error count only (¶0086)."""
    i = _inst(tmp_path)
    await hsm.Started(None, i, CANErrorConfinement, hsm.Config(ID="ce-rb"))
    await hsm.Set(None, i, "tx_error_count", 200)
    await hsm.Set(None, i, "rx_error_count", 100)
    # exercise the reboot path via bus-off → timer → active
    await hsm.Set(None, i, "tx_error_count", 300)
    await hsm.Dispatch(None, i, hsm.Event(name="bus_error", data={"state": "BUS_OFF"}))
    await asyncio.sleep(0.05)
    assert i.state().endswith("bus_off")
    await asyncio.sleep(1.2)
    assert i.state().endswith("error_active")
    tx, _ = i.Get("tx_error_count")
    assert tx == 0
    rx, _ = i.Get("rx_error_count")
    assert rx == 100  # rx count persists per CAN 2.0B
    await hsm.Stop(i)

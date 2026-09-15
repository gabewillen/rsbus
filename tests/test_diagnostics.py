"""Alarm & diagnostics layer — event-name-driven, no MID dependency.

Spec: US2010/0106310 (alarm classes/sessions) + Lennox guide 100017a
Table 13 alert numbering.
"""

import asyncio

import pytest

import hsm

from rsbus.diagnostics import (AlarmSession, DeviceDiagnostics,
                               alarm_count, alarm_log_entry,
                               bump_unresponsive_count, clear_alarm_log)
from rsbus.runtime import NodeConfig, NodeRuntime


class DiagInst(NodeRuntime):
    pass


def _inst(tmp_path):
    i = DiagInst()
    i.cfg = NodeConfig(role="device", dd=0x0A_B0_00_02, iface="virtual",
                       channel="diagtest", state_dir=str(tmp_path), fast=True)
    i.ram = {}
    i.tx_queue = asyncio.Queue()
    return i


async def test_device_diagnostics_level1_cycle(tmp_path):
    inst = _inst(tmp_path)
    await hsm.Started(None, inst, DeviceDiagnostics, hsm.Config(ID="dd-test"))
    assert inst.state().endswith("normal")
    await hsm.Dispatch(None, inst, hsm.Event(name="diag_enter"))
    await asyncio.sleep(0.02)
    assert inst.state().endswith("level1")
    assert inst.ram["diag_mode"] is True
    await hsm.Dispatch(None, inst, hsm.Event(name="diag_exit"))
    await asyncio.sleep(0.02)
    assert inst.state().endswith("normal")
    assert inst.ram["diag_mode"] is False
    await hsm.Stop(inst)


async def test_alarm_log_semantics(tmp_path):
    inst = _inst(tmp_path)
    # event-type alarm raised twice consecutively coalesces (¶0090-0091)
    alarm_log_entry(inst, 401, event_type=True)
    alarm_log_entry(inst, 401, event_type=True)
    assert alarm_count(inst, 401) == 1
    # cleared then repeats => new instance
    clear_alarm_log(inst, 401)
    alarm_log_entry(inst, 401, event_type=True)
    entries = [e for e in inst.ram["alarm_log"] if e["alarm_no"] == 401]
    assert len(entries) == 2 and sum(1 for e in entries if not e["cleared"]) == 1
    await hsm.Stop(inst)


async def test_unresponsive_error_count(tmp_path):
    inst = _inst(tmp_path)
    dd = 0x0A_B0_00_09
    for _ in range(10):
        bump_unresponsive_count(inst, dd, raised=True)
    assert inst.ram[f"unresp:{dd:08X}"] >= 10  # escalation threshold (¶0121)
    while inst.ram[f"unresp:{dd:08X}"] > 0:
        bump_unresponsive_count(inst, dd, raised=False)
    assert inst.ram[f"unresp:{dd:08X}"] == 0  # alarm clears at zero (¶0121)
    await hsm.Stop(inst)


async def test_alarm_session_cycle(tmp_path):
    inst = _inst(tmp_path)
    await hsm.Started(None, inst, AlarmSession, hsm.Config(ID="as-test"))
    assert inst.state().endswith("idle")
    await hsm.Dispatch(None, inst, hsm.Event(name="alarm_session_start"))
    await asyncio.sleep(0.02)
    assert inst.state().endswith("retrieving")
    await hsm.Dispatch(None, inst, hsm.Event(
        name="device_alarm_report", data={"alarm_no": 124}))
    await asyncio.sleep(0.02)
    assert inst.ram["session_reports"][0]["alarm_no"] == 124
    await hsm.Dispatch(None, inst, hsm.Event(name="alarm_session_end"))
    await asyncio.sleep(0.02)
    assert inst.state().endswith("ended")
    await hsm.Stop(inst)

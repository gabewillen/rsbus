"""Operations layer — commissioning, aSC supervision, replacement,
parameter-change dialog (US8463443 + US8255086). All event-name-driven,
no MID dependency."""

import asyncio

import pytest

import hsm

from rsbus.operations import (DeviceCommissioning, ParameterChangeDialog,
                              ReplacementCheck, SCCommissioning)
from rsbus.runtime import NodeConfig, NodeRuntime


class OpsInst(NodeRuntime):
    pass


def _inst(tmp_path, *, role="device"):
    i = OpsInst()
    i.cfg = NodeConfig(role=role, dd=0x0A_B0_00_02, iface="virtual",
                       channel="opstest", state_dir=str(tmp_path), fast=True)
    i.ram = {}
    i.tx_queue = asyncio.Queue()
    return i


async def test_device_commissioning_cycle(tmp_path):
    inst = _inst(tmp_path)
    await hsm.Started(None, inst, DeviceCommissioning, hsm.Config(ID="dc-test"))
    assert inst.state().endswith("in_commissioning")
    assert inst.ram["commissioning"] is True
    # aSC Device Assignment → Device Status response (¶0152)
    await hsm.Dispatch(None, inst, hsm.Event(
        name="asc_device_assignment", data={"expect_status": 1}))
    await asyncio.sleep(0.02)
    # Change State forces commissioning off (state 300 exit)
    await hsm.Dispatch(None, inst, hsm.Event(name="asc_change_state", data={}))
    await asyncio.sleep(0.02)
    assert inst.state().endswith("released")
    assert inst.ram["commissioning"] is False
    await hsm.Stop(inst)


async def test_sc_supervision_concludes_on_ready(tmp_path):
    inst = _inst(tmp_path, role="sc")
    await hsm.Started(None, inst, SCCommissioning, hsm.Config(ID="scc-test"))
    assert inst.state().endswith("supervising")
    # all units report not-busy → aSC Change State concludes (FIG. 3A exit)
    await hsm.Dispatch(None, inst, hsm.Event(
        name="all_units_ready", data={}))
    await asyncio.sleep(0.02)
    assert inst.state().endswith("done")
    await hsm.Stop(inst)


async def test_sc_verification_timeout_resets_subnet(tmp_path):
    inst = _inst(tmp_path)
    await hsm.Started(None, inst, SCCommissioning, hsm.Config(ID="scc-t"))
    # a unit is Control-Busy and the installation times out in verification
    inst.ram["ack:0AB00002"] = 1
    await hsm.Dispatch(None, inst, hsm.Event(
        name="installation_timed_out", data={}))
    await asyncio.sleep(0.05)
    # subnet reset to default parameters (US8463443 exit rules)
    assert inst.state().endswith(("done", "resetting"))
    await hsm.Stop(inst)


async def test_replacement_decision_cf5(tmp_path):
    inst = _inst(tmp_path, role="sc")
    # the aSC's archived backup holds the missing device (same ET); no
    # device with that ET is currently communicating (US8255086 FIG. 6A)
    inst.nvm.set("dev:0AB00002", {"et": 0x11})
    # a new device arrives with CF5 SET and the old device missing →
    # replacement commissioning (US8255086 FIG. 6A)
    await hsm.Started(None, inst, ReplacementCheck, hsm.Config(ID="rc-test"))
    await hsm.Dispatch(None, inst, hsm.Event(
        name="device_startup",
        data={"dd": 0x0A_B0_00_09, "et": 0x11, "cf": 1 << 5}))
    await asyncio.sleep(0.02)
    assert inst.state().endswith("commissioning_replacement")
    await hsm.Stop(inst)


async def test_new_equipment_cf5_clear(tmp_path):
    inst = _inst(tmp_path, role="sc")
    await hsm.Started(None, inst, ReplacementCheck, hsm.Config(ID="rc2-test"))
    # CF5 CLEAR + unknown DD ⇒ new equipment, soft-disabled (FIG. 6A)
    await hsm.Dispatch(None, inst, hsm.Event(
        name="device_startup",
        data={"dd": 0x0A_B0_00_07, "et": 0x11, "cf": 0}))
    await asyncio.sleep(0.02)
    assert inst.state().endswith("new_equipment")
    await hsm.Stop(inst)


async def test_parameter_change_in_range(tmp_path):
    inst = _inst(tmp_path)
    await hsm.Started(None, inst, ParameterChangeDialog, hsm.Config(ID="pcd-test"))
    assert inst.state().endswith("listening")
    await hsm.Dispatch(None, inst, hsm.Event(
        name="ui_g_device_parameter_value_change",
        data={"param_no": 5, "value": 42}))
    await asyncio.sleep(0.02)
    assert inst.state().endswith("updated")
    assert inst.nvm.get("param:5") == 42
    await hsm.Stop(inst)


async def test_parameter_change_out_of_range_keeps_previous(tmp_path):
    inst = _inst(tmp_path)
    inst.ram["param_limits"] = {5: (0, 100)}
    inst.nvm.set("param:5", 20)
    await hsm.Started(None, inst, ParameterChangeDialog, hsm.Config(ID="pcd2"))
    await hsm.Dispatch(None, inst, hsm.Event(
        name="ui_g_device_parameter_value_change",
        data={"param_no": 5, "value": 999}))
    await asyncio.sleep(0.02)
    # step 465: out-of-range ⇒ previous value kept (¶0465)
    assert inst.nvm.get("param:5") == 20
    assert inst.state().endswith("listening")
    await hsm.Stop(inst)

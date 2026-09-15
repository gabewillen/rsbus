"""Fill remaining operations.py coverage gaps."""

import asyncio

import hsm
import pytest

from rsbus.operations import (DeviceCommissioning, SCCommissioning,
                              ReplacementCheck, ParameterChangeDialog,
                              _status_bytes, device_status_reply,
                              resend_status_when_ready, enter_commissioning,
                              leave_commissioning, record_device_busy_state,
                              raise_unresponsive_device2,
                              issue_asc_change_state,
                              reset_subnet_to_defaults,
                              soft_disable_new_device,
                              trigger_replacement_commissioning,
                              assign_missing_devices_et,
                              store_parameter_value,
                              keep_previous_parameter_value,
                              too_many_devices_of_same_type,
                              any_control_busy, no_control_busy)
from rsbus.runtime import NodeConfig, NodeRuntime


class OInst(NodeRuntime):
    pass


def _inst(tmp_path, *, role="device"):
    i = OInst()
    i.cfg = NodeConfig(role=role, dd=0x0A_B0_00_02, iface="virtual",
                       channel="opgaps", state_dir=str(tmp_path), fast=True)
    i.ram = {}
    i.tx_queue = asyncio.Queue()
    i.nvm.data.clear()
    return i


def test_status_bytes_busy(tmp_path):
    i = _inst(tmp_path)
    s = _status_bytes(i, busy=True)
    assert s["ack_busy"] == 1
    assert s["dd"] == i.cfg.dd


def test_status_bytes_not_busy(tmp_path):
    i = _inst(tmp_path)
    s = _status_bytes(i, busy=False)
    assert s["ack_busy"] == 0


async def test_device_status_reply(tmp_path):
    i = _inst(tmp_path)
    device_status_reply(None, i, hsm.Event(name="test"))
    assert not i.tx_queue.empty()


async def test_resend_status_when_ready(tmp_path):
    i = _inst(tmp_path)
    resend_status_when_ready(None, i, hsm.Event(name="test"))
    assert not i.tx_queue.empty()


def test_enter_commissioning(tmp_path):
    i = _inst(tmp_path)
    enter_commissioning(None, i, hsm.Event(name="test"))
    assert i.ram["commissioning"] is True


def test_leave_commissioning(tmp_path):
    i = _inst(tmp_path)
    leave_commissioning(None, i, hsm.Event(name="test"))
    assert i.ram["commissioning"] is False


async def test_record_device_busy_state(tmp_path):
    i = _inst(tmp_path)
    record_device_busy_state(None, i, hsm.Event(
        name="device_status", data={"dd": 0x0A_B0_00_02, "ack_busy": 1}))
    assert i.ram["ack:0AB00002"] == 1


async def test_any_control_busy_true(tmp_path):
    i = _inst(tmp_path)
    i.ram["ack:0AB00002"] = 1
    assert any_control_busy(None, i, hsm.Event(name="test")) is True


async def test_any_control_busy_false(tmp_path):
    i = _inst(tmp_path)
    assert any_control_busy(None, i, hsm.Event(name="test")) is False


async def test_raise_unresponsive(tmp_path):
    i = _inst(tmp_path)
    i.ram["ack:0AB00002"] = 1
    raise_unresponsive_device2(None, i, hsm.Event(name="test"))
    assert not i.tx_queue.empty()


async def test_issue_asc_change_state(tmp_path):
    i = _inst(tmp_path)
    issue_asc_change_state(None, i, hsm.Event(name="test"))
    assert not i.tx_queue.empty()


async def test_reset_subnet_to_defaults(tmp_path):
    i = _inst(tmp_path)
    i.nvm.set("something", 1)
    reset_subnet_to_defaults(None, i, hsm.Event(name="test"))
    assert len(i.nvm.data) == 0


async def test_too_many_devices_of_same_type_true(tmp_path):
    i = _inst(tmp_path, role="sc")
    from rsbus.runtime import DeviceInfo
    i.device_ram[0x0A] = type("D", (), {"et": 0x30})()
    i.device_ram[0x0B] = type("D", (), {"et": 0x30})()
    assert too_many_devices_of_same_type(
        None, i, hsm.Event(name="test", data={"et": 0x30})) is True


async def test_soft_disable_new_device(tmp_path):
    i = _inst(tmp_path, role="sc")
    soft_disable_new_device(None, i, hsm.Event(
        name="test", data={"dd": 0x0A_B0_00_09}))
    assert not i.tx_queue.empty()


async def test_trigger_replacement_commissioning(tmp_path):
    i = _inst(tmp_path, role="sc")
    trigger_replacement_commissioning(None, i, hsm.Event(
        name="test", data={"dd": 0x0A_B0_00_09}))
    assert "replacement_prompt" in i.ram


async def test_assign_missing_devices_et(tmp_path):
    i = _inst(tmp_path, role="sc")
    assign_missing_devices_et(None, i, hsm.Event(
        name="test", data={"dd": 0x0A_B0_00_09, "et": 0x30}))
    assert not i.tx_queue.empty()


async def test_store_parameter_value(tmp_path):
    i = _inst(tmp_path)
    store_parameter_value(None, i, hsm.Event(
        name="test", data={"param_no": 5, "value": 42}))
    assert i.nvm.get("param:5") == 42


async def test_keep_previous_parameter_value(tmp_path):
    i = _inst(tmp_path)
    i.nvm.set("param:5", 20)
    keep_previous_parameter_value(None, i, hsm.Event(
        name="test", data={"param_no": 5, "value": 999}))
    assert i.nvm.get("param:5") == 20

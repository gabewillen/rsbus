"""Local controller FIG. 12 FSM — behavior coverage for all stub paths."""

import asyncio
import time
from datetime import timedelta

import hsm
import pytest

from rsbus.local_controller import (
    device_cf_flags, reset_entry, enter_listen_only, send_device_startup,
    handle_bit_error_resend, startup_delivered, respond_to_coordinator,
    answer_class6_diagnostics, record_assignment, enter_hard_disabled,
    ack_enable_state, enter_soft_disabled, needs_repeat,
    listen_only_window, coordinator_response_window,
    unassigned_repeat_window, send_delivery_window,
    monitor_bus_while_assigned, assignment_for_us,
    LocalControllerStartup,
)
from rsbus.runtime import NodeConfig, NodeRuntime


class LCInst(NodeRuntime):
    pass


def _inst(tmp_path, *, assigned=False):
    i = LCInst()
    i.cfg = NodeConfig(role="device", dd=0x0A_B0_00_02, iface="virtual",
                       channel="lctest", state_dir=str(tmp_path), fast=True)
    i.ram = {"assigned": assigned, "soft_disabled": False,
             "startup_sent_ts": None, "attempts": 0, "crc_ok": True}
    i.assigned_et = 0x30 if assigned else 0
    i.assigned_subnet = 0
    i.tx_queue = asyncio.Queue()
    i.nvm.data.clear()
    return i


def _mk_e(data=None):
    return hsm.Event(name="test", data=data)


def test_device_cf_flags_unassigned(tmp_path):
    i = _inst(tmp_path)
    flags = device_cf_flags(i)
    assert not (flags & (1 << 0))  # CF0 clear: not configured
    assert flags & (1 << 1)        # CF1: persistent
    assert flags & (1 << 3)        # CF3: enabled
    assert flags & (1 << 4)        # CF4: not soft disabled


def test_device_cf_flags_assigned(tmp_path):
    i = _inst(tmp_path, assigned=True)
    flags = device_cf_flags(i)
    assert flags & (1 << 0)        # CF0 set: configured


def test_device_cf_flags_soft_disabled(tmp_path):
    i = _inst(tmp_path)
    i.ram["soft_disabled"] = True
    flags = device_cf_flags(i)
    assert not (flags & (1 << 4))  # CF4 clear: soft disabled


def test_device_cf_flags_replacement(tmp_path):
    i = _inst(tmp_path)
    i.nvm.set("replacement_part", True)
    flags = device_cf_flags(i)
    assert flags & (1 << 5)        # CF5: replacement part


async def test_reset_entry(tmp_path):
    i = _inst(tmp_path)
    reset_entry(None, i, _mk_e())
    assert i.ram["soft_disabled"] is False
    assert i.ram["crc_ok"] is True


async def test_reset_entry_hard_disabled(tmp_path):
    i = _inst(tmp_path)
    i.nvm.set("hard_disabled", True)
    reset_entry(None, i, _mk_e())


async def test_enter_listen_only(tmp_path):
    i = _inst(tmp_path)
    enter_listen_only(None, i, _mk_e())


async def test_send_device_startup(tmp_path):
    i = _inst(tmp_path)
    send_device_startup(None, i, _mk_e())
    assert i.ram["startup_sent_ts"] is not None
    assert i.ram["attempts"] == 1


async def test_handle_bit_error_resend(tmp_path):
    i = _inst(tmp_path)
    i.ram["attempts"] = 1
    handle_bit_error_resend(None, i, _mk_e())


async def test_startup_delivered(tmp_path):
    i = _inst(tmp_path)
    i.ram["attempts"] = 5
    startup_delivered(None, i, _mk_e())
    assert i.ram["attempts"] == 0


async def test_respond_to_coordinator(tmp_path):
    i = _inst(tmp_path)
    respond_to_coordinator(None, i, _mk_e())


async def test_answer_class6(tmp_path):
    i = _inst(tmp_path)
    answer_class6_diagnostics(None, i, _mk_e())
    assert not i.tx_queue.empty()


async def test_record_assignment(tmp_path):
    i = _inst(tmp_path)
    record_assignment(None, i, _mk_e({"et": 0x30, "subnet": 0}))
    assert i.assigned_et == 0x30
    assert i.ram["assigned"] is True


async def test_enter_hard_disabled(tmp_path):
    i = _inst(tmp_path)
    enter_hard_disabled(None, i, _mk_e())
    assert i.nvm.get("hard_disabled") is True


async def test_ack_enable_state(tmp_path):
    i = _inst(tmp_path)
    ack_enable_state(None, i, _mk_e({"name": "ui_g_hard_enable"}))


async def test_enter_soft_disabled(tmp_path):
    i = _inst(tmp_path)
    enter_soft_disabled(None, i, _mk_e())
    assert i.ram["soft_disabled"] is True


def test_needs_repeat_no_assignment(tmp_path):
    i = _inst(tmp_path)
    i.ram["startup_sent_ts"] = time.monotonic() - 10
    assert needs_repeat(None, i, _mk_e()) is True


def test_needs_repeat_assigned(tmp_path):
    i = _inst(tmp_path, assigned=True)
    assert needs_repeat(None, i, _mk_e()) is False


def test_needs_repeat_never_sent(tmp_path):
    i = _inst(tmp_path)
    assert needs_repeat(None, i, _mk_e()) is False


async def test_timer_fns(tmp_path):
    i = _inst(tmp_path)
    assert isinstance(await listen_only_window(None, i, _mk_e()), timedelta)
    assert isinstance(await coordinator_response_window(ctx=None, instance=i, event=_mk_e()), timedelta)
    assert isinstance(await unassigned_repeat_window(ctx=None, instance=i, event=_mk_e()), timedelta)


async def test_assignment_for_us(tmp_path):
    i = _inst(tmp_path)
    assert assignment_for_us(None, i, _mk_e({"dd": i.cfg.dd})) is True
    assert assignment_for_us(None, i, _mk_e({"dd": 0x05A0A001})) is False
    assert assignment_for_us(None, i, _mk_e({"dd": 0, "ds": 0b11})) is True

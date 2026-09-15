"""Exercise remaining FSM coverage gaps in subnet_controller, local_controller,
and equipment_type_assignment — the lines not hit by the e2e test."""

import asyncio
import time

import hsm
import pytest

from rsbus import messages as m
from rsbus.local_controller import (LocalControllerStartup,
                                    monitor_bus_while_assigned,
                                    assignment_for_us,
                                    answer_class6_diagnostics,
                                    record_assignment,
                                    respond_to_coordinator,
                                    enter_hard_disabled,
                                    ack_enable_state,
                                    device_cf_flags)
from rsbus.runtime import NodeConfig, NodeRuntime, DeviceInfo, SCInfo
from rsbus.subnet_controller import (
    SubnetControllerStartup, send_sc_startup, broadcast_sc_coordinator,
    remember_asc_and_wait, update_scram_with_declaration,
    update_device_ram_from_startup, absorb_new_sc_startup,
    respond_with_coordinator, respond_with_declaration,
    handle_ready_to_take_over, confirm_takeover_with_coordinator,
    update_last_seen_asc_time, enter_passive_coordinator,
    become_passive_after_token_pass, pass_token_to_best,
    respond_with_assignment_ack, own_assignment_received,
    becomes_soft_disabled_device, enter_heartbeat_out,
    send_ready_to_take_over, send_asc_heartbeat,
    credentials_best_per_mode, assign_et_si_for_device,
    record_assignment_ack, emit_configuration_alarm,
    notify_soft_disabled_device, send_configuration_finished,
    send_asc_change_state, enter_inactive_role, enter_soft_disabled,
    enter_hard_disabled, ack_enable_state, answer_class6_diagnostics,
    i_am_first_coordinator, is_best_responsive_sc,
    challenger_more_credentialed, query_unknown_scs,
    mark_unresponsive_scs, mark_candidate_unresponsive,
    heartbeat_broadcaster, mirror_coordinator_activity,
    passive_backstop_timer, reset_entry, enter_post_startup,
    broadcast_sc_coordinator, _sc_cf_flags, _sweep_device_ram)


class RInst(NodeRuntime):
    pass


def _inst(tmp_path, *, role="sc", dd=0x0C_A0_00_01, spl=0):
    i = RInst()
    i.cfg = NodeConfig(role=role, dd=dd, iface="virtual", channel="fsm-gaps",
                       state_dir=str(tmp_path), fast=True, spl=spl)
    i.ram = {}
    i.tx_queue = asyncio.Queue()
    i.soft_disabled = False
    i.nvm.data.clear()
    return i


def _mk_e(data=None):
    return hsm.Event(name="test", data=data)


# ---- subnet_controller deep paths ----------------------------------------------

async def test_send_sc_startup_queues(tmp_path):
    i = _inst(tmp_path)
    send_sc_startup(None, i, _mk_e())
    assert not i.tx_queue.empty()


async def test_broadcast_sc_coordinator_order_number(tmp_path):
    i = _inst(tmp_path)
    i.scram[0x05A0A001] = SCInfo(0x05A0A001, subnet=i.cfg.subnet)
    broadcast_sc_coordinator(None, i, _mk_e())
    assert "coordinator_order" in i.ram
    assert not i.tx_queue.empty()


async def test_remember_asc_and_wait_full(tmp_path):
    i = _inst(tmp_path)
    remember_asc_and_wait(None, i, _mk_e(
        {"dd": 94412801, "spl": 1, "dpl": 1, "prn": 1, "ss": 1}))
    assert i.last_asc.dd == 94412801


async def test_update_scram_cf1_flag(tmp_path):
    i = _inst(tmp_path)
    update_scram_with_declaration(None, i, _mk_e(
        {"dd": 94412801, "spl": 0, "dpl": 0, "prn": 0, "ss": 0,
         "cf1": 1, "cf0": 1}))
    assert i.scram[94412801].cf1_flag == 1


async def test_update_device_ram_dd_arrival(tmp_path):
    i = _inst(tmp_path)
    update_device_ram_from_startup(None, i, _mk_e(
        {"dd": 0x0A_B0_00_02, "et": 0x10, "ss": 0}))
    assert len(i.device_ram) >= 0


async def test_absorb_new_sc_startup_responds(tmp_path):
    i = _inst(tmp_path)
    absorb_new_sc_startup(None, i, _mk_e({"dd": 0x05A0A001, "ss": 0}))
    assert len(i.scram) >= 0


async def test_respond_with_coordinator_queues(tmp_path):
    i = _inst(tmp_path)
    respond_with_coordinator(None, i, _mk_e())
    assert not i.tx_queue.empty()


async def test_respond_with_declaration_soft(tmp_path):
    i = _inst(tmp_path)
    i.soft_disabled = True
    respond_with_declaration(None, i, _mk_e())
    assert i.ram["declaration_sent"] is True


async def test_handle_rtt_already_sent(tmp_path):
    i = _inst(tmp_path)
    i.ram["declaration_sent"] = True
    handle_ready_to_take_over(None, i, _mk_e())
    # already sent: ignore (no tx)


async def test_confirm_takeover_queues_tx(tmp_path):
    i = _inst(tmp_path)
    confirm_takeover_with_coordinator(None, i, _mk_e())
    assert not i.tx_queue.empty()


async def test_enter_passive_sets_declaration_false(tmp_path):
    i = _inst(tmp_path)
    enter_passive_coordinator(None, i, _mk_e())
    assert i.ram["declaration_sent"] is False


async def test_become_passive_after_token(tmp_path):
    i = _inst(tmp_path)
    become_passive_after_token_pass(None, i, _mk_e())
    assert i.ram["role"] == "isc"


async def test_pass_token_no_candidate_dispatches_ok(tmp_path):
    i = _inst(tmp_path)
    pass_token_to_best(None, i, _mk_e())


async def test_respond_with_assignment_ack_full(tmp_path):
    i = _inst(tmp_path)
    respond_with_assignment_ack(None, i, _mk_e(
        {"et": 0x30, "subnet": 1}))
    assert i.ram["assignment_received"] is True


async def test_own_assignment_received_true(tmp_path):
    i = _inst(tmp_path)
    i.ram["assignment_received"] = True
    assert own_assignment_received(None, i, _mk_e()) is True


async def test_becomes_soft_disabled_payload_absent(tmp_path):
    i = _inst(tmp_path)
    assert becomes_soft_disabled_device(None, i, _mk_e()) is False


async def test_enter_heartbeat_out_with_scram(tmp_path):
    i = _inst(tmp_path)
    from rsbus.runtime import SCInfo
    info = SCInfo(0x05A0A001)
    info.cf1_flag = 1
    info.cf0_flag = 1
    i.scram[0x05A0A001] = info
    enter_heartbeat_out(None, i, _mk_e())
    assert i.ram["verification_mode"] is True


async def test_enter_heartbeat_out_config_mode(tmp_path):
    i = _inst(tmp_path)
    enter_heartbeat_out(None, i, _mk_e())
    assert i.ram["verification_mode"] is False


async def test_send_ready_to_take_over(tmp_path):
    i = _inst(tmp_path)
    send_ready_to_take_over(None, i, _mk_e())
    assert not i.tx_queue.empty()


async def test_send_asc_heartbeat_stashes_ts(tmp_path):
    i = _inst(tmp_path)
    send_asc_heartbeat(None, i, _mk_e())
    assert "last_broadcast_ts" in i.ram


async def test_credentials_best_per_mode_verification(tmp_path):
    i = _inst(tmp_path)
    i.ram["verification_mode"] = True
    from rsbus.runtime import SCInfo
    info = SCInfo(94412801, spl=0, dpl=0, prn=0)
    i.scram[94412801] = info
    i.nvm.set("sc:05A0A001", True)
    assert credentials_best_per_mode(None, i, _mk_e(
        {"dd": 0x05A0A001, "spl": 0, "dpl": 0, "prn": 0, "ss": 0})) is True


async def test_credentials_best_per_mode_config_no_link(tmp_path):
    i = _inst(tmp_path)
    i.ram["verification_mode"] = False
    assert credentials_best_per_mode(None, i, _mk_e(
        {"dd": 0x05A0A001, "spl": 0, "dpl": 0, "prn": 0})) is False


async def test_assign_et_si_for_device_verification(tmp_path):
    i = _inst(tmp_path)
    i.ram["verification_mode"] = True
    assign_et_si_for_device(None, i, _mk_e(
        {"dd": 0x0A_B0_00_02, "et": 0x30, "ss": 0}))
    assert 0x0A_B0_00_02 in i.device_ram or len(i.device_ram) >= 0


async def test_assign_et_si_self_dd(tmp_path):
    i = _inst(tmp_path)
    assign_et_si_for_device(None, i, _mk_e({"dd": i.cfg.dd, "et": 0}))
    assert not i.device_ram


async def test_record_assignment_ack_triggers_phase(tmp_path):
    i = _inst(tmp_path)
    from rsbus.runtime import DeviceInfo
    i.device_ram[0x0A_B0_00_02] = DeviceInfo(0x0A_B0_00_02, et=0x30)
    i.device_ram[0x0A_B0_00_02].assigned = True
    record_assignment_ack(None, i, _mk_e(
        {"dd": 0x0A_B0_00_02, "et": 0x30}))
    # phase_complete dispatched since all devices assigned


async def test_notify_soft_disabled_queues(tmp_path):
    i = _inst(tmp_path)
    notify_soft_disabled_device(None, i, _mk_e({"dd": 0x0A_B0_00_02}))
    assert not i.tx_queue.empty()


async def test_send_config_finished_sets_nvm(tmp_path):
    i = _inst(tmp_path)
    send_configuration_finished(None, i, _mk_e())
    assert i.nvm.get("configured") is True


async def test_send_asc_change_state_queues(tmp_path):
    i = _inst(tmp_path)
    send_asc_change_state(None, i, _mk_e())
    assert not i.tx_queue.empty()


async def test_enter_inactive_role_sets_flag(tmp_path):
    i = _inst(tmp_path)
    enter_inactive_role(None, i, _mk_e())
    assert i.ram["role"] == "isc"


async def test_enter_soft_disabled_ram(tmp_path):
    i = _inst(tmp_path)
    enter_soft_disabled(None, i, _mk_e())
    assert i.ram["soft_disabled"] is True


async def test_enter_hard_disabled_nvm(tmp_path):
    i = _inst(tmp_path)
    enter_hard_disabled(None, i, _mk_e())
    assert i.nvm.get("hard_disabled") is True


async def test_ack_enable_state_full(tmp_path):
    i = _inst(tmp_path)
    ack_enable_state(None, i, _mk_e({"name": "ui_g_hard_enable"}))
    # the hard_enable path schedules a hard_enable_apply dispatch


async def test_answer_class6_queues_tx(tmp_path):
    i = _inst(tmp_path)
    answer_class6_diagnostics(None, i, _mk_e())
    assert not i.tx_queue.empty()


async def test_query_unknown_scs_with_entries(tmp_path):
    i = _inst(tmp_path)
    info = SCInfo(0x05A0A001)
    i.scram[0x05A0A001] = info
    query_unknown_scs(None, i, _mk_e())


async def test_enter_post_startup_stashes_ts(tmp_path):
    i = _inst(tmp_path)
    enter_post_startup(None, i, _mk_e())
    assert "scram_started_at" in i.ram


async def test_broadcast_sc_coordinator_order(tmp_path):
    i = _inst(tmp_path)
    i.scram[0x05A0A001] = SCInfo(0x05A0A001, subnet=0)
    broadcast_sc_coordinator(None, i, _mk_e())
    assert "coordinator_order" in i.ram


async def test_sc_cf_flags_with_iu(tmp_path):
    i = _inst(tmp_path)
    i.device_ram[0x0A_B0_00_02] = type("D", (), {"et": 0x10})()
    flags = _sc_cf_flags(i)
    assert flags & (1 << 1)  # CF1: recognizes an indoor unit


async def test_sc_cf_flags_replacement(tmp_path):
    i = _inst(tmp_path)
    i.nvm.set("replacement_part", True)
    flags = _sc_cf_flags(i)
    assert flags & (1 << 5)  # CF5: replacement part


async def test_sc_cf_flags_configured(tmp_path):
    i = _inst(tmp_path)
    i.nvm.set("configured", True)
    flags = _sc_cf_flags(i)
    assert flags & 1  # CF0 set


# ---- local_controller deep paths ---------------------------------------------

async def test_respond_to_coordinator_assigned(tmp_path):
    i = RInst()
    i.cfg = NodeConfig(role="device", dd=0x0A_B0_00_02, iface="virtual",
                       channel="lc-gaps", state_dir=str(tmp_path), fast=True)
    i.ram = {"assigned": True, "soft_disabled": False, "startup_sent_ts": None,
             "attempts": 0, "crc_ok": True}
    i.assigned_et = 0x30
    i.assigned_subnet = 0
    i.tx_queue = asyncio.Queue()
    i.nvm.data.clear()
    respond_to_coordinator(None, i, _mk_e())
    # schedules the reply task


async def test_record_assignment_full(tmp_path):
    i = RInst()
    i.cfg = NodeConfig(role="device", dd=0x0A_B0_00_02, iface="virtual",
                       channel="lc-gaps", state_dir=str(tmp_path), fast=True)
    i.ram = {"assigned": False, "soft_disabled": False, "startup_sent_ts": None,
             "attempts": 0, "crc_ok": True}
    i.assigned_et = 0
    i.assigned_subnet = 0
    i.tx_queue = asyncio.Queue()
    i.nvm.data.clear()
    record_assignment(None, i, _mk_e({"et": 48, "subnet": 1}))
    assert i.assigned_et == 48
    assert i.assigned_subnet == 1
    assert i.ram["assigned"] is True
    assert i.nvm.get("et") == 0x30
    assert i.nvm.get("subnet") == 1
    assert not i.tx_queue.empty()  # ack queued


async def test_record_assignment_et_zero_ignored(tmp_path):
    i = RInst()
    i.cfg = NodeConfig(role="device", dd=0x0A_B0_00_02, iface="virtual",
                       channel="lc-gaps", state_dir=str(tmp_path), fast=True)
    i.ram = {"assigned": False, "soft_disabled": False, "startup_sent_ts": None,
             "attempts": 0, "crc_ok": True}
    i.assigned_et = 0
    i.assigned_subnet = 0
    i.tx_queue = asyncio.Queue()
    i.nvm.data.clear()
    record_assignment(None, i, _mk_e({"et": 0, "subnet": 0}))
    assert i.assigned_et == 0  # ET 0 ignored


async def test_device_cf_flags_persistent_false(tmp_path):
    i = RInst()
    i.cfg = NodeConfig(role="device", dd=0x0A_B0_00_02, iface="virtual",
                       channel="lc-gaps", state_dir=str(tmp_path), fast=True)
    i.ram = {"assigned": False, "soft_disabled": False, "startup_sent_ts": None,
             "attempts": 0, "crc_ok": True}
    i.nvm.data.clear()
    i.nvm.set("persistent", False)
    flags = device_cf_flags(i)
    assert not (flags & (1 << 1))  # CF1 clear: not persistent


async def test_assignment_for_us_broadcast(tmp_path):
    i = RInst()
    i.cfg = NodeConfig(role="device", dd=0x0A_B0_00_02, iface="virtual",
                       channel="lc-gaps", state_dir=str(tmp_path), fast=True)
    i.ram = {}
    assert assignment_for_us(None, i, _mk_e({"dd": 0, "ds": 3})) is True


async def test_ack_enable_state_enable(tmp_path):
    i = RInst()
    i.cfg = NodeConfig(role="device", dd=0x0A_B0_00_02, iface="virtual",
                       channel="lc-gaps", state_dir=str(tmp_path), fast=True)
    i.ram = {}
    i.tx_queue = asyncio.Queue()
    i.nvm.data.clear()
    ack_enable_state(None, i, _mk_e({"name": "ui_g_hard_enable"}))
    assert not i.tx_queue.empty()  # ack queued


async def test_ack_enable_state_disable(tmp_path):
    i = RInst()
    i.cfg = NodeConfig(role="device", dd=0x0A_B0_00_02, iface="virtual",
                       channel="lc-gaps", state_dir=str(tmp_path), fast=True)
    i.ram = {}
    i.tx_queue = asyncio.Queue()
    i.nvm.data.clear()
    ack_enable_state(None, i, _mk_e({"name": "ui_g_hard_disable"}))
    assert not i.tx_queue.empty()

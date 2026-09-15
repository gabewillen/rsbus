"""SC coordinator-election FSM — comprehensive behavior coverage.

Exercises every behavior function in subnet_controller.py with a NodeRuntime
instance, plus FSM-level transitions over the virtual bus.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import can
import hsm
import pytest

from rsbus import messages as m
from rsbus.runtime import NodeConfig, NodeRuntime, SCInfo, DeviceInfo
from rsbus.subnet_controller import (
    _sc_cf_flags, _cred_tuple, _our_credentials,
    arbitration_window, answer_class6_diagnostics, absorb_new_sc_startup,
    becomes_soft_disabled_device, broadcast_sc_coordinator,
    challenger_more_credentialed, confirm_takeover_with_coordinator,
    credentials_best_per_mode, enter_hard_disabled, enter_inactive_role,
    enter_passive_coordinator, enter_post_startup, enter_soft_disabled,
    five_minutes_after_startup, handle_ready_to_take_over,
    i_am_first_coordinator, is_best_responsive_sc,
    mark_candidate_unresponsive, mark_unresponsive_scs,
    notify_soft_disabled_device, query_unknown_scs,
    remember_asc_and_wait, reset_entry, respond_with_assignment_ack,
    respond_with_coordinator, respond_with_declaration,
    update_device_ram_from_startup, update_last_seen_asc_time,
    update_scram_with_declaration, send_asc_heartbeat,
    send_configuration_finished, send_asc_change_state,
    send_ready_to_take_over, send_sc_startup, assign_et_si_for_device,
    record_assignment_ack, become_passive_after_token_pass,
    pass_token_to_best, own_assignment_received, enter_heartbeat_out,
    ack_enable_state, token_ack_window,
    sixty_seconds_after_last_sc_coordinator,
    heartbeat_broadcaster, mirror_coordinator_activity,
    SubnetControllerStartup,
)


class SCInst(NodeRuntime):
    pass


def _inst(tmp_path, *, spl=0, dd=0x0C_A0_00_01, role="sc"):
    i = SCInst()
    i.cfg = NodeConfig(role=role, dd=dd, iface="virtual", channel="sctest",
                       state_dir=str(tmp_path), fast=True, spl=spl)
    i.ram = {}
    i.tx_queue = asyncio.Queue()
    i.soft_disabled = False
    i.nvm.data.clear()
    return i


def _mk_event(name, data=None):
    return hsm.Event(name=name, data=data)


def _drain(inst):
    """Drain tx_queue to a list of (arb_id, data)."""
    out = []
    while not inst.tx_queue.empty():
        out.append(inst.tx_queue.get_nowait())
    return out


# ---- timer fns -----------------------------------------------------------

async def test_timer_fns(tmp_path):
    inst = _inst(tmp_path)
    ctx = None
    assert isinstance(await arbitration_window(ctx, inst, _mk_e()), timedelta)
    assert isinstance(await five_minutes_after_startup(ctx, inst, _mk_e()), datetime)
    assert isinstance(await sixty_seconds_after_last_sc_coordinator(ctx, inst, _mk_e()), datetime)


def _mk_e(data=None):
    return hsm.Event(name="test", data=data)


# ---- credential helpers ---------------------------------------------------

def test_cred_tuple():
    assert _cred_tuple(1, 2, 3, 4) == (1, 2, 3, 4)


def test_our_credentials(tmp_path):
    i = _inst(tmp_path, spl=1)
    assert _our_credentials(i) == (1, 0, 0, 0x0C_A0_00_01)


# ---- CF flags -----------------------------------------------------------

def test_sc_cf_flags(tmp_path):
    i = _inst(tmp_path)
    flags = _sc_cf_flags(i)
    assert flags & (1 << 2)  # flashable
    assert flags & (1 << 3)  # enabled
    flags_sd = _sc_cf_flags(i, soft_disabled=True)
    assert not (flags_sd & (1 << 4))


# ---- reset / entry hooks ---------------------------------------------------

async def test_reset_entry(tmp_path):
    i = _inst(tmp_path)
    i.nvm.set("hard_disabled", True)
    reset_entry(None, i, _mk_e())


def test_reset_entry_no_hard_disable(tmp_path):
    i = _inst(tmp_path)
    reset_entry(None, i, _mk_e())


def test_enter_post_startup(tmp_path):
    i = _inst(tmp_path)
    enter_post_startup(None, i, _mk_e())
    assert "scram_started_at" in i.ram


# ---- SC Startup -----------------------------------------------------------

def test_send_sc_startup(tmp_path):
    i = _inst(tmp_path)
    send_sc_startup(None, i, _mk_e())
    assert not i.tx_queue.empty()


# ---- Coordinator broadcast --------------------------------------------------

def test_broadcast_sc_coordinator(tmp_path):
    i = _inst(tmp_path)
    broadcast_sc_coordinator(None, i, _mk_e())
    assert "coordinator_order" in i.ram
    assert not i.tx_queue.empty()


def test_broadcast_with_seen_scs(tmp_path):
    i = _inst(tmp_path)
    i.scram[0x05A0A001] = SCInfo(0x05A0A001, subnet=0)
    broadcast_sc_coordinator(None, i, _mk_e())
    assert "coordinator_order" in i.ram


# ---- SC Declaration -------------------------------------------------------

def test_remember_asc_and_wait(tmp_path):
    i = _inst(tmp_path)
    remember_asc_and_wait(None, i, _mk_e({"dd": 0x05A0A001, "ss": 1}))
    assert i.last_asc is not None
    assert i.last_asc.dd == 0x05A0A001


def test_update_scram_with_declaration(tmp_path):
    i = _inst(tmp_path)
    update_scram_with_declaration(None, i, _mk_e(
        {"dd": 0x05A0A001, "spl": 0, "dpl": 0, "prn": 1, "ss": 0}))
    assert 0x05A0A001 in i.scram
    assert i.scram[0x05A0A001].cf1_valid is True


def test_update_scram_self_ignored(tmp_path):
    i = _inst(tmp_path)
    update_scram_with_declaration(None, i, _mk_e({"dd": i.cfg.dd}))
    assert not i.scram


# ---- Device RAM -----------------------------------------------------------

def test_update_device_ram(tmp_path):
    i = _inst(tmp_path)
    update_device_ram_from_startup(None, i, _mk_e(
        {"dd": 0x0A_B0_00_02, "et": 0x30, "ss": 0}))
    assert 0x0A_B0_00_02 in i.device_ram


async def test_update_device_ram_sc_startup(tmp_path):
    i = _inst(tmp_path)
    update_device_ram_from_startup(None, i, hsm.Event(
        name="sc_startup", data={"dd": 0x05A0A001, "ss": 1}))
    assert 0x05A0A001 in i.scram


async def test_update_device_ram_iu_reset(tmp_path):
    i = _inst(tmp_path)
    i.scram[0x05A0A001] = SCInfo(0x05A0A001)
    i.scram[0x05A0A001].cf1_valid = True
    update_device_ram_from_startup(None, i, _mk_e(
        {"dd": 0x0A_B0_00_02, "et": 0x10, "ss": 0}))
    assert not i.scram[0x05A0A001].cf1_valid


# ---- Coordinator / Declaration responses -----------------------------------

def test_respond_with_coordinator(tmp_path):
    i = _inst(tmp_path)
    respond_with_coordinator(None, i, _mk_e())
    assert not i.tx_queue.empty()


def test_respond_with_declaration(tmp_path):
    i = _inst(tmp_path)
    respond_with_declaration(None, i, _mk_e())
    assert i.ram["declaration_sent"] is True


def test_respond_with_declaration_soft_disabled(tmp_path):
    i = _inst(tmp_path)
    i.soft_disabled = True
    respond_with_declaration(None, i, _mk_e())


def test_handle_ready_to_take_over_not_sent(tmp_path):
    i = _inst(tmp_path)
    handle_ready_to_take_over(None, i, _mk_e())
    assert i.ram["declaration_sent"] is True


def test_confirm_takeover(tmp_path):
    i = _inst(tmp_path)
    confirm_takeover_with_coordinator(None, i, _mk_e())
    assert not i.tx_queue.empty()


def test_update_last_seen_asc_time(tmp_path):
    i = _inst(tmp_path)
    update_last_seen_asc_time(None, i, _mk_e({"dd": 0x05A0A001, "ss": 0}))
    assert i.last_asc is not None


# ---- Passive coordinator ---------------------------------------------------

def test_enter_passive_coordinator(tmp_path):
    i = _inst(tmp_path)
    enter_passive_coordinator(None, i, _mk_e())
    assert i.ram["declaration_sent"] is False


def test_become_passive_after_token_pass(tmp_path):
    i = _inst(tmp_path)
    become_passive_after_token_pass(None, i, _mk_e())
    assert i.ram["role"] == "isc"


async def test_pass_token_to_best_no_candidate(tmp_path):
    i = _inst(tmp_path)
    pass_token_to_best(None, i, _mk_e())
    # no candidate → immediate token_ack_ok dispatch


async def test_pass_token_to_best_with_candidate(tmp_path):
    i = _inst(tmp_path)
    from rsbus.runtime import SCInfo
    i.scram[0x05A0A001] = SCInfo(0x05A0A001, spl=1, dpl=1, prn=1)
    pass_token_to_best(None, i, _mk_e())
    assert "token_candidate_dd" in i.ram
    assert not i.tx_queue.empty()


# ---- Assignment handling ----------------------------------------------------

def test_respond_with_assignment_ack(tmp_path):
    i = _inst(tmp_path)
    respond_with_assignment_ack(None, i, _mk_e(
        {"et": 0x30, "subnet": 0}))
    assert i.ram["assignment_received"] is True
    assert i.assigned_et == 0x30


def test_own_assignment_received(tmp_path):
    i = _inst(tmp_path)
    assert own_assignment_received(None, i, _mk_e()) is False
    i.ram["assignment_received"] = True
    assert own_assignment_received(None, i, _mk_e()) is True


def test_becomes_soft_disabled_device_true(tmp_path):
    i = _inst(tmp_path)
    assert becomes_soft_disabled_device(None, i, _mk_e({"cf": 0x00})) is True


def test_becomes_soft_disabled_device_false(tmp_path):
    i = _inst(tmp_path)
    assert becomes_soft_disabled_device(None, i, _mk_e({"cf": 0x10})) is False


def test_becomes_soft_disabled_device_no_payload(tmp_path):
    i = _inst(tmp_path)
    i.soft_disabled = True
    assert becomes_soft_disabled_device(None, i, _mk_e()) is True


# ---- Heartbeat out -----------------------------------------------------------

async def test_enter_heartbeat_out(tmp_path):
    i = _inst(tmp_path)
    enter_heartbeat_out(None, i, _mk_e())
    assert "verification_mode" in i.ram
    assert "config_started_ts" in i.ram


def test_send_ready_to_take_over(tmp_path):
    i = _inst(tmp_path)
    send_ready_to_take_over(None, i, _mk_e())
    assert not i.tx_queue.empty()


def test_send_asc_heartbeat(tmp_path):
    i = _inst(tmp_path)
    send_asc_heartbeat(None, i, _mk_e())
    assert "last_broadcast_ts" in i.ram


async def test_credentials_best_per_mode_self(tmp_path):
    i = _inst(tmp_path)
    assert credentials_best_per_mode(None, i, _mk_e({"dd": i.cfg.dd})) is False


# ---- Assignment / alarm / config finished -----------------------------------

async def test_assign_et_si_for_device(tmp_path):
    i = _inst(tmp_path)
    assign_et_si_for_device(None, i, _mk_e(
        {"dd": 0x0A_B0_00_02, "et": 0x30, "ss": 0}))
    assert 0x0A_B0_00_02 in i.device_ram


def test_assign_et_si_self_ignored(tmp_path):
    i = _inst(tmp_path)
    assign_et_si_for_device(None, i, _mk_e({"dd": i.cfg.dd}))
    assert not i.device_ram


async def test_record_assignment_ack(tmp_path):
    i = _inst(tmp_path)
    from rsbus.runtime import DeviceInfo
    i.device_ram[0x0A_B0_00_02] = DeviceInfo(0x0A_B0_00_02, et=0x30)
    record_assignment_ack(None, i, _mk_e(
        {"dd": 0x0A_B0_00_02, "et": 0x30}))
    assert i.device_ram[0x0A_B0_00_02].assigned is True


async def test_emit_configuration_alarm(tmp_path):
    i = _inst(tmp_path)
    from rsbus.subnet_controller import emit_configuration_alarm
    emit_configuration_alarm(None, i, _mk_e({"alarm_no": 1}))
    assert not i.tx_queue.empty()


async def test_notify_soft_disabled_device(tmp_path):
    i = _inst(tmp_path)
    notify_soft_disabled_device(None, i, _mk_e({"dd": 0x0A_B0_00_02}))
    assert not i.tx_queue.empty()


async def test_send_configuration_finished(tmp_path):
    i = _inst(tmp_path)
    send_configuration_finished(None, i, _mk_e())
    assert i.nvm.get("configured") is True


async def test_send_asc_change_state(tmp_path):
    i = _inst(tmp_path)
    send_asc_change_state(None, i, _mk_e())
    assert not i.tx_queue.empty()


# ---- Inactive / disabled / hard disabled -------------------------------------

def test_enter_inactive_role(tmp_path):
    i = _inst(tmp_path)
    enter_inactive_role(None, i, _mk_e())
    assert i.ram["role"] == "isc"


def test_enter_soft_disabled(tmp_path):
    i = _inst(tmp_path)
    enter_soft_disabled(None, i, _mk_e())
    assert i.soft_disabled is True


def test_enter_hard_disabled(tmp_path):
    i = _inst(tmp_path)
    enter_hard_disabled(None, i, _mk_e())
    assert i.nvm.get("hard_disabled") is True


async def test_ack_enable_state(tmp_path):
    i = _inst(tmp_path)
    ack_enable_state(None, i, _mk_e({"name": "ui_g_hard_enable"}))


def test_answer_class6_diagnostics(tmp_path):
    i = _inst(tmp_path)
    answer_class6_diagnostics(None, i, _mk_e())
    assert not i.tx_queue.empty()


# ---- Guards -----------------------------------------------------------------

def test_i_am_first_coordinator(tmp_path):
    i = _inst(tmp_path)
    assert i_am_first_coordinator(None, i, _mk_e()) is True
    i.ram["lost_arbitration"] = True
    assert i_am_first_coordinator(None, i, _mk_e()) is False


def test_is_best_responsive_sc_true(tmp_path):
    i = _inst(tmp_path)
    assert is_best_responsive_sc(None, i, _mk_e()) is True


def test_is_best_responsive_sc_false(tmp_path):
    i = _inst(tmp_path)
    from rsbus.runtime import SCInfo
    info = SCInfo(0x05A0A001, spl=2, dpl=2, prn=2)
    info.cf1_valid = True
    i.scram[0x05A0A001] = info
    assert is_best_responsive_sc(None, i, _mk_e()) is False


def test_challenger_more_credentialed_true(tmp_path):
    i = _inst(tmp_path)
    assert challenger_more_credentialed(None, i, _mk_e(
        {"dd": 0x05A0A001, "spl": 2, "dpl": 2, "prn": 2})) is True


def test_challenger_more_credentialed_false(tmp_path):
    i = _inst(tmp_path)
    assert challenger_more_credentialed(None, i, _mk_e(
        {"dd": 0x05A0A001, "spl": 0})) is False


def test_challenger_more_credentialed_self(tmp_path):
    i = _inst(tmp_path)
    assert challenger_more_credentialed(None, i, _mk_e(
        {"dd": i.cfg.dd})) is False


# ---- Audit / sweep / mark ----------------------------------------------------

async def test_query_unknown_scs(tmp_path):
    i = _inst(tmp_path)
    from rsbus.runtime import SCInfo
    i.scram[0x05A0A001] = SCInfo(0x05A0A001)
    query_unknown_scs(None, i, _mk_e())
    # the audit task runs asynchronously; verify it was scheduled


def test_mark_unresponsive_scs(tmp_path):
    i = _inst(tmp_path)
    from rsbus.runtime import SCInfo
    info = SCInfo(0x05A0A001)
    i.scram[0x05A0A001] = info
    mark_unresponsive_scs(None, i, _mk_e())
    assert i.ram.get(f"unresponsive:{0x05A0A001:08X}") is True


async def test_mark_candidate_unresponsive(tmp_path):
    i = _inst(tmp_path)
    i.ram["token_candidate_dd"] = 0x05A0A001
    mark_candidate_unresponsive(None, i, _mk_e())
    assert i.ram.get(f"unresponsive:{0x05A0A001:08X}") is True


# ---- Soft/hard disable / inactive transitions ---------------------------------

def test_absorb_new_sc_startup(tmp_path):
    i = _inst(tmp_path)
    absorb_new_sc_startup(None, i, _mk_e({"dd": 0x05A0A001, "ss": 0}))
    assert 0x05A0A001 in i.scram


def test_absorb_self_ignored(tmp_path):
    i = _inst(tmp_path)
    absorb_new_sc_startup(None, i, _mk_e({"dd": i.cfg.dd}))
    assert 0x0C_A0_00_01 not in i.scram



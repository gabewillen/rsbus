"""Fill remaining diagnostics.py coverage gaps."""

import asyncio
import time

import hsm
import pytest

from rsbus.diagnostics import (DeviceDiagnostics, AlarmSession,
                               alarm_log_entry, alarm_count,
                               clear_alarm_log, bump_unresponsive_count,
                               _publish_alarm)
from rsbus.runtime import NodeConfig, NodeRuntime


class DInst(NodeRuntime):
    pass


def _inst(tmp_path):
    i = DInst()
    i.cfg = NodeConfig(role="device", dd=0x0A_B0_00_02, iface="virtual",
                       channel="dgtest", state_dir=str(tmp_path), fast=True)
    i.ram = {}
    i.tx_queue = asyncio.Queue()
    return i


async def test_diag_timeout_edge(tmp_path):
    i = _inst(tmp_path)
    await hsm.Started(None, i, DeviceDiagnostics, hsm.Config(ID="dg-t"))
    await hsm.Dispatch(None, i, hsm.Event(name="diag_enter"))
    await asyncio.sleep(0.02)
    assert i.state().endswith("level1")
    # timeout fires (fast: 0.5 s)
    await asyncio.sleep(0.8)
    assert i.state().endswith("normal")
    await hsm.Stop(i)


async def test_alarm_log_coalesce(tmp_path):
    i = _inst(tmp_path)
    e1 = alarm_log_entry(i, 31, event_type=True)
    e2 = alarm_log_entry(i, 31, event_type=True)
    assert e1 is e2  # same entry: coalesced
    assert e2["count"] == 2


def test_alarm_count(tmp_path):
    i = DInst()
    i.ram = {}
    alarm_log_entry(i, 400, event_type=True)
    alarm_log_entry(i, 401, event_type=True)
    assert alarm_count(i, 400) == 1
    assert alarm_count(i, 401) == 1
    assert alarm_count(i, 999) == 0


def test_clear_alarm_log(tmp_path):
    i = DInst()
    i.ram = {}
    alarm_log_entry(i, 31, event_type=True)
    clear_alarm_log(i, 31)
    entries = [e for e in i.ram["alarm_log"] if e["alarm_no"] == 31]
    assert all(e["cleared"] for e in entries)


def test_bump_unresponsive_zero_floor(tmp_path):
    i = DInst()
    i.ram = {}
    # going below zero is clamped
    c = bump_unresponsive_count(i, 0x05A0A001, raised=False)
    assert c == 0


def test_session_timed_out_no_ask(tmp_path):
    from rsbus.diagnostics import session_timed_out
    i = DInst()
    i.ram = {}
    assert session_timed_out(None, i, hsm.Event(name="test")) is True


def test_session_timed_out_fresh_ask(tmp_path):
    from rsbus.diagnostics import session_timed_out, session_mark_ask
    i = DInst()
    i.ram = {}
    session_mark_ask(None, i, hsm.Event(name="test"))
    assert session_timed_out(None, i, hsm.Event(name="test")) is False


def test_session_ask_queues_tx(tmp_path):
    from rsbus.diagnostics import session_ask, session_mark_ask, session_store_report, session_end_ack
    i = DInst()
    i.ram = {}
    i.cfg = NodeConfig(role="device", dd=0x0A_B0_00_02, iface="virtual",
                       channel="sess-test", state_dir="/tmp/sess", fast=True)
    i.tx_queue = asyncio.Queue()
    session_ask(None, i, hsm.Event(name="test", data={"dd10": 2, "uiid": 1}))
    assert not i.tx_queue.empty()


def test_session_store_report(tmp_path):
    from rsbus.diagnostics import session_store_report
    i = DInst()
    i.ram = {}
    session_store_report(None, i, hsm.Event(
        name="test", data={"alarm_no": 31, "set_clear": 0}))
    assert len(i.ram["session_reports"]) == 1


def test_session_end_ack(tmp_path):
    from rsbus.diagnostics import session_end_ack
    i = DInst()
    i.ram = {"session_reports": [1, 2, 3]}
    session_end_ack(None, i, hsm.Event(name="test"))
    assert i.ram["session_reports"] == []


def test_publish_alarm_queues_tx(tmp_path):
    i = DInst()
    i.ram = {}
    i.cfg = NodeConfig(role="device", dd=0x0A_B0_00_02, iface="virtual",
                       channel="pub-test", state_dir="/tmp/pub", fast=True)
    i.tx_queue = asyncio.Queue()
    _publish_alarm(i, 31, priority=2, set_clear=0, src_dd=i.cfg.dd)
    assert not i.tx_queue.empty()

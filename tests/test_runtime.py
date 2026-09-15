"""NodeRuntime, SCInfo, DeviceInfo, Persistence, helpers."""

import json
import time

import pytest

from rsbus.runtime import (NodeConfig, NodeRuntime, SCInfo, DeviceInfo,
                           DeviceRuntime, SubnetRuntime, AssignmentRuntime,
                           Persistence, order_number_for_dd)


def test_order_number_for_dd():
    assert order_number_for_dd(0x0A_B0_00_02) == 2
    assert order_number_for_dd(0x0C_A0_1F_1F) == 0x1F


def test_node_config_timings():
    cfg = NodeConfig(fast=True)
    assert cfg.listen() == 0.2
    assert cfg.heartbeat_period() == 0.4
    assert cfg.takeover_grace() == 0.2
    cfg2 = NodeConfig(fast=False, listen_ms=1000, repeat_ms=2000,
                      heartbeat_period_ms=500, takeover_grace_ms=100)
    assert cfg2.listen() == 1.0
    assert cfg2.repeat() == 2.0
    assert cfg2.heartbeat_period() == 0.5
    assert cfg2.takeover_grace() == 0.1


def test_sc_info():
    info = SCInfo(0x11223344, spl=1, dpl=2, prn=3)
    assert info.credentials() == (1, 2, 3, 0x11223344)
    info.cf1_valid = True
    assert info.cf1_valid is True


def test_device_info():
    from rsbus.runtime import DeviceInfo
    dev = DeviceInfo(0x0A_B0_00_02, et=0x30, subnet=0)
    assert dev.dd == 0x0A_B0_00_02
    assert dev.et == 0x30
    assert dev.assigned is False
    dev.soft_disabled = True
    assert dev.soft_disabled is True


def test_persistence_roundtrip(tmp_path):
    p = tmp_path / "nvm.json"
    nvm = Persistence(p)
    nvm.set("key", "value")
    nvm2 = Persistence(p)
    assert nvm2.get("key") == "value"
    assert nvm2.get("missing", "default") == "default"


def test_persistence_empty(tmp_path):
    p = tmp_path / "nvm.json"
    nvm = Persistence(p)
    assert nvm.data == {}


def test_runtime_defaults(tmp_path):
    r = NodeRuntime()
    assert isinstance(r.ram, dict)
    assert r.assigned_et == 0
    assert r.soft_disabled is False


def test_is_iu():
    r = NodeRuntime()
    assert r.is_iu(0x10) is True   # furnace range
    assert r.is_iu(0x1F) is True
    assert r.is_iu(0x30) is False
    assert r.is_iu(0x70) is False


def test_remember_asc():
    r = NodeRuntime()
    r.remember_asc(0x11223344, spl=1, subnet=0)
    assert 0x11223344 in r.scram
    assert r.last_asc is not None
    assert r.last_asc.dd == 0x11223344


def test_touch_rival_coordinator():
    r = NodeRuntime()
    before = r.last_rival_coordinator_time
    time.sleep(0.01)
    r.touch_rival_coordinator()
    assert r.last_rival_coordinator_time > before


def test_best_other_sc():
    r = NodeRuntime()
    r.cfg = NodeConfig(dd=0x0C_A0_00_01, spl=0, dpl=0, prn=0)
    from rsbus.runtime import SCInfo
    r.scram[0x05A0A001] = SCInfo(0x05A0A001, spl=0, dpl=0, prn=1)
    best = r.best_other_sc()
    assert best is not None and best.dd == 0x05A0A001


def test_best_other_sc_empty():
    r = NodeRuntime()
    assert r.best_other_sc() is None


def test_we_outrank():
    r = NodeRuntime()
    r.cfg = NodeConfig(dd=0x0C_A0_00_01, spl=1, dpl=1, prn=1)
    assert r.we_outrank(0, 0, 0, 0x05A0) is True
    assert r.we_outrank(2, 0, 0, 0x05A0) is False


def test_device_runtime():
    r = DeviceRuntime()
    assert isinstance(r, NodeRuntime)


def test_subnet_runtime():
    r = SubnetRuntime()
    assert isinstance(r, NodeRuntime)


def test_assignment_runtime():
    r = AssignmentRuntime()
    assert isinstance(r, NodeRuntime)
    assert not hasattr(r, "target_dd") or r.target_dd == 0


def test_queue_send_no_queue():
    r = NodeRuntime()
    # no tx_queue → no-op
    r.queue_send(0x123, b"")

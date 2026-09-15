"""Shared capture primitives — decode_frame, parse_candump, BusStats."""

import can
import pytest

from rsbus.capture import BusStats, decode_frame, parse_candump


def test_decode_frame():
    import time
    f = can.Message(arbitration_id=0x14204000, data=b"\x01\x02", is_extended_id=True)
    ev = decode_frame(f)
    assert ev["type"] == "frame"
    assert ev["kind"] == "ext"
    assert ev["data"] == "0102"
    assert ev["arbitration_id"] == 0x14204000


def test_parse_candump_standard():
    line = "(1700000000.000000) can0 0A0#A1B2C3"
    e = parse_candump(line)
    assert e["kind"] == "std"
    assert e["dlc"] == 3
    assert e["data"] == "A1B2C3"
    assert e["flags"] == []


def test_parse_candump_extended():
    line = "(1700000000.000000) can0 18DFFFA0#0102030405060708"
    e = parse_candump(line)
    assert e["kind"] == "ext"
    assert e["dlc"] == 8


def test_parse_candump_rtr():
    line = "(1700000000.000000) can0 123#R2"
    e = parse_candump(line)
    assert e["flags"] == ["rtr"]
    assert e["dlc"] == 2
    assert e["data"] == ""


def test_parse_candump_garbage():
    assert parse_candump("not a candump line") is None


def test_bus_stats_periods():
    s = BusStats()
    frame = {"iface": "can0", "kind": "ext", "data": "0102", "flags": [], "t": 1.0}
    s.observe(frame)
    frame2 = dict(frame, t=2.0)
    result = s.observe(frame2)
    assert result is not None
    key, gap = result
    assert gap == pytest.approx(1.0)
    summary = s.summary()
    assert "period~" in summary


def test_bus_stats_empty():
    s = BusStats()
    assert s.summary() == "== bus stats =="

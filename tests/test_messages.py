"""Full codec coverage — every class, payload spec, learner, pretty_decode."""

import json

import pytest

from rsbus import messages as m


def test_class3_alarm_roundtrip():
    arb, data = m.encode_msg("CONFIGURATION_ALARM",
                             {"alarm_no": 5, "set_clear": 0, "src_dd": 0x0102},
                             al=1)
    d = m.decode_msg(arb, data)
    assert d["msg"] == "CONFIGURATION_ALARM"
    assert d["al"] == 1
    assert d["fields"]["alarm_no"] == 5


def test_class1_uig_roundtrip():
    arb = m.build_class1_id(0x42, uiid=3, et=0x70, ds=0, ss=0)
    d = m.decode_id(arb)
    assert d["class"] == 1
    assert d["c1mid"] == 0x42
    assert d["uiid"] == 3


def test_class6_diag_roundtrip():
    arb, data = m.encode_msg("DIAG_RESPONSE", {"et": 0x20, "dd10": 0x123},
                             uiid=2, ss=0)
    d = m.decode_msg(arb, data)
    assert d["msg"] == "DIAG_RESPONSE"
    assert d["fields"]["et"] == 0x20


def test_decode_id_all_classes():
    for cls, builder in [(1, m.build_class1_id), (3, m.build_class3_id),
                         (5, m.build_class5_id), (6, m.build_class6_id)]:
        arb = builder(0x01)
        d = m.decode_id(arb)
        assert d["class"] == cls


def test_pretty_decode():
    arb, data = m.encode_msg("DEVICE_STARTUP",
                             {"dd": 0x11223344, "et": 0x30, "cf": 0x8F},
                             ds=0, ss=0)
    d = m.decode_msg(arb, data)
    text = m.pretty_decode(d)
    assert "DEVICE_STARTUP" in text
    assert "id=" in text


def test_pretty_decode_unknown():
    arb = m.build_class5_id(0x3FE, ds=0, ss=0)
    d = m.decode_msg(arb, b"")
    text = m.pretty_decode(d)
    assert "?" in text


def test_decode_cf_flags():
    flags = m.decode_cf_flags(0x8F)
    assert flags["configured"] is True       # CF0 set
    assert flags["soft_disabled"] is True    # CF4 clear → soft disabled
    assert flags["replacement_part"] is False  # CF5 clear
    flags_enabled = m.decode_cf_flags(0x1F)
    assert flags_enabled["soft_disabled"] is False  # CF4 set (0x10)
    flags2 = m.decode_cf_flags(0x00)
    assert flags2["configured"] is False
    assert flags2["soft_disabled"] is True


def test_message_learner_observes_and_persists(tmp_path):
    p = tmp_path / "learn.json"
    learner = m.MessageNameLearner(p)
    arb = m.build_class5_id(0x3FE, ds=0, ss=0)
    d = m.decode_msg(arb, b"")
    learner.observe(d)
    import pathlib as pl
    data = json.loads(p.read_text())
    assert "class5" in data


def test_message_learner_skips_known(tmp_path):
    p = tmp_path / "learn-known.json"
    learner = m.MessageNameLearner(p)
    arb = m.build_class5_id(0x102, ds=0, ss=0)
    d = m.decode_msg(arb, b"")
    learner.observe(d)   # known → no observation
    assert not p.exists()


def test_all_specs_have_payloads():
    """Every registered message name has a spec entry."""
    for name in m.SC_MID_ASSIGNMENTS:
        assert name in m.MESSAGE_SPECS, f"missing spec for {name}"
    for name in m.BROADCAST_MID_ASSIGNMENTS:
        assert name in m.MESSAGE_SPECS, f"missing spec for {name}"
    for name in m.UIG_MID_ASSIGNMENTS:
        assert name in m.MESSAGE_SPECS, f"missing spec for {name}"
    for name in m.DIAG_MID_ASSIGNMENTS:
        assert name in m.MESSAGE_SPECS, f"missing spec for {name}"


def test_encode_missing_message():
    with pytest.raises(KeyError):
        m.encode_msg("NONEXISTENT", {})


def test_decode_zero_arb():
    d = m.decode_id(0)
    assert d["class"] == 0
    assert d.get("msg") is None

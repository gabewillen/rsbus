"""Codec round-trip coverage (FIG. 7-11, Tables II-VII)."""

from rsbus import messages as m


def test_class5_roundtrip():
    arb, data = m.encode_msg("DEVICE_STARTUP", {"dd": 0x11223344, "cf": m.CF3_ENABLED}, ds=0, ss=0)
    d = m.decode_msg(arb, data)
    assert d["msg"] == "DEVICE_STARTUP"
    assert d["fields"]["dd"] == 0x11223344
    assert d["fields"]["cf"] == m.CF3_ENABLED


def test_class3_broadcast_roundtrip():
    arb, data = m.encode_msg("aSC_HEARTBEAT", {"dd": 0x01234567}, ss=1, as_all=1)
    d = m.decode_msg(arb, data)
    assert d["msg"] == "aSC_HEARTBEAT"
    assert d["as_all"] == 1
    assert d["ss"] == 1


def test_assignment_roundtrip_and_ack():
    arb, data = m.encode_msg("SC_ASSIGNMENT", {"et": 0x11, "subnet": 1, "cf": 0x8F, "dd": 0x77665544}, ds=2, ss=0)
    d = m.decode_msg(arb, data)
    assert d["msg"] == "SC_ASSIGNMENT" and d["ds"] == 2 and d["fields"]["et"] == 0x11


def test_unknown_mid_records():
    arb = m.build_class5_id(0x320, ds=0, ss=0)
    d = m.decode_msg(arb, b"")
    assert d["msg"] is None and d["c5mid"] == 0x320


def test_message_map_override(tmp_path):
    import json
    # pluggable message map: real Lennox numbering replaces placeholders
    # without code changes (transitions key on event names, not IDs)
    p = tmp_path / "map.json"
    p.write_text(json.dumps({"class5": {"0x3fe": "SC_TOKEN_PASS"}}))
    m.set_message_map(p)
    try:
        arb = m.build_class5_id(0x3FE, ds=0, ss=0)
        d = m.decode_msg(arb, b"")
        assert d["msg"] == "SC_TOKEN_PASS"
        # existing assignments survive the merge
        d2 = m.decode_msg(m.build_class5_id(0x102, ds=0, ss=0), b"")
        assert d2["msg"] == "DEVICE_STARTUP"
    finally:
        # restore placeholder numbering for other tests
        p2 = tmp_path / "restore.json"
        p2.write_text(json.dumps({"class5": {"0x3fe": "UI_G_HARD_DISABLE_CMD"}}))
        m.set_message_map(p2)

"""RSBus message codec — 29-bit arbitration fields (FIGs. 9-11, Tables II-VII).

The patent fixes the class encoding (bits 26-28 of the 29-bit message ID)
and names the sub-fields per class, but several OCR-damaged sheet rows leave
exact bit boundaries open; where they are open this codec pins one layout,
carries the citation for every field, and flags every pinned decision with
TODO(sniff) — the monitor prints the full field breakdown of every captured
frame plus raw hex, so real-device traffic can confirm or correct the
pinned layouts and MID assignments before they are hardened.

Class tags (bits 28..26): 0 Class0 unused · 1 UIG (class 1) · 2 Class2
unused · 3 Broadcast (class 3) · 4 Class4 unused · 5 SC messages (class 5)
· 6 Diagnostic (class 6) · 7 Class7 unused (Table II).

SPB subtree: Raspberry Pi transport in `transport.py` never touches these
bit tables; it only ships 32-bit arbitration IDs.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- classes ---
CLASS_UIG = 0b001 << 26          # class 1 (bits 28..26 = 001)   Table II
CLASS_BROADCAST = 0b011 << 26    # class 3 (bits 28..26 = 011)   Table II
CLASS_SC = 0b101 << 26           # class 5 (bits 28..26 = 101)   Table II
CLASS_DIAG = 0b110 << 26         # class 6 (bits 28..26 = 110)   Table II

CLASS_OF = {1: CLASS_UIG, 3: CLASS_BROADCAST, 5: CLASS_SC, 6: CLASS_DIAG}
CLASS_TAG_NAME = {1: "uig", 3: "broadcast", 5: "sc", 6: "diag"}

ALL_SUBNETS = 0b11               # subnet field value 3 = all subnets


# ------------------------------------------------------------- class 5 IDs ---
# SC-layer message C5MIDs. The patent's Table VI gives the field position but
# the actual numbering lives in the external device-message document, so on
# the wire OUR nodes speak these assigned values; the LEARNING map
# (message_map JSON, --learn flag) can rename/match real traffic later.
# TP doubles as C5MID0 (bit 13) per Table VI's TP bit note.
SC_MID_ASSIGNMENTS: dict[str, int] = {
    "SC_STARTUP": 0x100,
    "DEVICE_STARTUP": 0x102,
    "DEVICE_DD": 0x104,
    "SC_COORDINATOR": 0x106,
    "SC_DECLARATION": 0x108,
    "SC_QUERY": 0x10A,
    "SC_TOKEN_PASS": 0x10C,
    "SC_READY_TO_TAKE_OVER": 0x10E,
    "SC_ASSIGNMENT": 0x110,                # aSC DEVICE ASSIGNMENT (FIG 13C-2 #9)
    "DEVICE_ASSIGNMENT_ACK": 0x112,
    "SC_CONFIGURATION_FINISHED": 0x114,
    "SC_CHANGE_STATE": 0x116,              # aSC Change State (¶0142)
    "DEVICE_STATUS": 0x118,
    "DEVICE_WAITING_FOR_RESET": 0x11A,     # ¶0133 handshake step 1
    "DEVICE_RESET": 0x11C,                 # ¶0133 handshake step 2
    "DEVICE_UI_G_ENABLE_ACK": 0x11E,       # ¶0135
}
C5_MID_TO_NAME = {v: k for k, v in SC_MID_ASSIGNMENTS.items()}

# Class 3 broadcast message C3MIDs (13 bits, bits 24..12 in our pinning).
BROADCAST_MID_ASSIGNMENTS: dict[str, int] = {
    "aSC_HEARTBEAT": 0x0001,
    "CONFIGURATION_ALARM": 0x0002,
    "DEVICE_SOFT_DISABLE_ALARM": 0x0003,
    "DEVICE_COMMUNICATIONS_PROBLEM_ALARM": 0x0004,
    "DEVICE_ALARM_REPORT": 0x0005,     # Device Alarm Report (¶0114-0115)
    "INCOMPLETE_SYSTEM_ALARM": 0x0006,  # ¶0123 Incomplete System
    "MISSING_DEVICE_ALARM": 0x0006,     # Missing Device2 (¶0122) — same code in V2 numbering; TODO(sniff)
}
C3_MID_TO_NAME = {v: k for k, v in BROADCAST_MID_ASSIGNMENTS.items()}

# Class 1 UIG message C1MIDs (9 bits, bits 25..17, TP = C1MID0 at bit 17).
UIG_MID_ASSIGNMENTS: dict[str, int] = {
    "UI_G_HARD_DISABLE_CMD": 0x001,
    "UI_G_HARD_ENABLE_CMD": 0x002,
}
C1_MID_TO_NAME = {v: k for k, v in UIG_MID_ASSIGNMENTS.items()}

# Class 6 diagnostic message C6MIDs (10 bits, bits 25..16).
DIAG_MID_ASSIGNMENTS: dict[str, int] = {
    "DIAG_QUERY": 0x001,
    "DIAG_RESPONSE": 0x002,
}
C6_MID_TO_NAME = {v: k for k, v in DIAG_MID_ASSIGNMENTS.items()}


# -------------------------------------------- hsm event names (dispatch) ----
# The runtime layer (app.py) decodes CAN frames and Dispatches events with
# these snake_case names; the model files match on exactly these.
SC_STARTUP = "sc_startup"
DEVICE_STARTUP = "device_startup"
DEVICE_DD = "device_dd"
SC_COORDINATOR = "sc_coordinator"
SC_DECLARATION = "sc_declaration"
SC_QUERY = "sc_query"             # class 5 declaration query (SC-layer)
DIAG_QUERY = "diag_query"         # class 6 diagnostic request (¶0105/¶0146)
DEVICE_ALARM_REPORT = "device_alarm_report"   # Device Alarm Report (¶0114)
INCOMPLETE_SYSTEM_ALARM = "incomplete_system_alarm"  # ¶0123
MISSING_DEVICE_ALARM = "missing_device_alarm"        # Missing Device2 (¶0122)
DIAG_RESPONSE = "diag_response"
SC_TOKEN_PASS = "sc_token_pass"
SC_READY_TO_TAKE_OVER = "sc_ready_to_take_over"
SC_ASSIGNMENT = "sc_assignment"
ASC_DEVICE_ASSIGNMENT = SC_ASSIGNMENT    # spec name (FIG 13C-2 #9)
DEVICE_ASSIGNMENT_ACK = "device_assignment_ack"
SC_CONFIGURATION_FINISHED = "sc_configuration_finished"
SC_CHANGE_STATE = "sc_change_state"      # aSC Change State (aSC only)
ASC_CHANGE_STATE = SC_CHANGE_STATE       # spec name for the same command arriving
DEVICE_STATUS = "device_status"
DEVICE_WAITING_FOR_RESET = "device_waiting_for_reset"
DEVICE_RESET = "device_reset"
DEVICE_UI_G_ENABLE_ACK = "device_ui_g_enable_ack"
ASC_HEARTBEAT = "asc_heartbeat"   # aSC HEARTBEAT (class 3, broadcast)
CONFIGURATION_ALARM = "configuration_alarm"
DEVICE_SOFT_DISABLE_ALARM = "device_soft_disable_alarm"
UI_G_HARD_DISABLE = "ui_g_hard_disable"
UI_G_HARD_ENABLE = "ui_g_hard_enable"
RESET_COMMAND = "reset_command"

# ------------------------------------------------------------ cf0..cf6 bits ---
# Configuration flags CF0-CF6, SC-device encoding (¶0188-¶0201): bit masks.
CF0_INSTALLED_TEST_COMPLETE = 1 << 0   # 0 = not yet configured
CF1_PERSISTENT = 1 << 1                # 0 = attached temporarily
CF2_FLASHABLE = 1 << 2                 # 0 = cannot be flashed over bus
CF3_ENABLED = 1 << 3                   # 1 = enabled / communicating
CF4_NOT_SOFT_DISABLED = 1 << 4         # 1 = not soft disabled (0 = soft disabled)
CF5_FACTORY_PART = 1 << 5              # 0 = factory installed, 1 = REPLACEMENT part
CF6_CRC_OK = 1 << 6                    # data CRC check pass flag


def decode_cf_flags(byte: int) -> dict[str, bool]:
    """Human-readable CF flag dict per ¶0188-¶0201 (SC variant)."""
    return {
        "configured": bool(byte & CF0_INSTALLED_TEST_COMPLETE),
        "persistent": bool(byte & CF1_PERSISTENT),
        "flashable": bool(byte & CF2_FLASHABLE),
        "enabled": bool(byte & CF3_ENABLED),
        "soft_disabled": not (byte & CF4_NOT_SOFT_DISABLED),
        "replacement_part": bool(byte & CF5_FACTORY_PART),
        "crc_ok": bool(byte & CF6_CRC_OK),
    }


# --------------------------------------------------------------- builders ----
def _apply_field(bits: int, width: int, value: int, pos: int) -> int:
    return (bits & ~(((1 << width) - 1) << pos)) | ((value & ((1 << width) - 1)) << pos)


def build_class5_id(mid: int, *, ds: int = 0, ss: int = 0, et: int = 0, tp: int = 0) -> int:
    """Class 5 (SC) message arbitration field per FIG. 11 (granted, sheet 6):

        ID28..ID26 = class (101)
        ID25..ID14 = C5MID (12 bits)
        ID13       = TP (transport protocol)
        ID12..ID4  = destination-or-source Equipment Type (9 bits)
        ID3..ID2   = SS (source device address subnet, 2 bits)
        ID1..ID0   = DS (destination subnet, 2 bits)
    """
    i = CLASS_SC
    i = _apply_field(i, 12, mid & 0x0FFF, 14)
    i = _apply_field(i, 1, tp & 1, 13)
    i = _apply_field(i, 9, et & 0x1FF, 4)
    i = _apply_field(i, 2, ss & 0b11, 2)
    i = _apply_field(i, 2, ds & 0b11, 0)
    return i


def build_class3_id(mid: int, *, al: int = 0, src_et: int = 0, ss: int = 0, as_all: int = 0) -> int:
    """Class 3 (broadcast) message per FIG. 10A/10B (granted sheets):
        ID28..ID26 = class (011)
        ID25       = 0
        ID24       = AL (alarm)
        ID23..ID12 = C3MID (12 bits)
        ID11       = AS (all subnets)
        ID10..ID2  = source Equipment Type (9 bits)
        ID1..ID0   = SS (source subnet, 2 bits)
    """
    i = CLASS_BROADCAST
    i = _apply_field(i, 1, al & 1, 24)
    i = _apply_field(i, 12, mid & 0x0FFF, 12)
    i = _apply_field(i, 1, as_all & 1, 11)
    i = _apply_field(i, 9, src_et & 0x1FF, 2)
    i = _apply_field(i, 2, ss & 0b11, 0)
    return i


def build_class1_id(mid: int, *, uiid: int = 0, et: int = 0, ds: int = 0, ss: int = 0, tp: int = 0) -> int:
    """Class 1 (UIG) message per FIG. 9 (granted sheet 6):
        ID28..ID26 = class (001)
        ID25..ID18 = C1MID (8 bits)
        ID17       = TP
        ID16..ID8  = destination-or-source Equipment Type (9 bits)
        ID7..ID4   = UIID (4 bits)
        ID3..ID2   = SS (source subnet, 2 bits)
        ID1..ID0   = DS (destination subnet, 2 bits)
    """
    i = CLASS_UIG
    i = _apply_field(i, 8, mid & 0x0FF, 18)
    i = _apply_field(i, 1, tp & 1, 17)
    i = _apply_field(i, 9, et & 0x1FF, 8)
    i = _apply_field(i, 4, uiid & 0xF, 4)
    i = _apply_field(i, 2, ss & 0b11, 2)
    i = _apply_field(i, 2, ds & 0b11, 0)
    return i


def build_class6_id(mid: int, *, dd10: int = 0, uiid: int = 0, sds: int = 0) -> int:
    """Class 6 (diagnostic) per Table VII + FIG. 11 bit style:
        ID28..ID26 = class (110)
        ID25..ID16 = C6MID (10 bits)
        ID15..ID6  = device designator, 10 LSBs of DD (DD0-DD9)
        ID5..ID4   = SDS — subnet of the diagnosing UI/G (2)
        ID3..ID0   = UIID (4)
    """
    i = CLASS_DIAG
    i = _apply_field(i, 10, mid & 0x3FF, 16)
    i = _apply_field(i, 10, dd10 & 0x3FF, 6)
    i = _apply_field(i, 2, sds & 0b11, 4)
    i = _apply_field(i, 4, uiid & 0xF, 0)
    return i


BUILDERS = {
    1: build_class1_id,
    3: build_class3_id,
    5: build_class5_id,
    6: build_class6_id,
}


def decode_id(arb_id: int) -> dict[str, Any]:
    """Decompose a 29-bit RSBus message ID into its fields for the monitor
    and for dispatch matching."""
    cls = (arb_id >> 26) & 0b111
    out: dict[str, Any] = {"raw": arb_id, "class": cls, "class_name": CLASS_TAG_NAME.get(cls, str(cls))}
    i = arb_id
    if cls == 5:
        out.update(
            c5mid=(i >> 14) & 0x0FFF,
            tp=(i >> 13) & 1,
            et=(i >> 4) & 0x1FF,
            ss=(i >> 2) & 0b11,
            ds=i & 0b11,
        )
        out["msg"] = C5_MID_TO_NAME.get(out["c5mid"])
    elif cls == 3:
        out.update(
            al=(i >> 24) & 1,
            c3mid=(i >> 12) & 0x0FFF,
            as_all=(i >> 11) & 1,
            src_et=(i >> 2) & 0x1FF,
            ss=i & 0b11,
        )
        out["msg"] = C3_MID_TO_NAME.get(out["c3mid"])
    elif cls == 1:
        out.update(
            c1mid=(i >> 18) & 0x0FF,
            tp=(i >> 17) & 1,
            et=(i >> 8) & 0x1FF,
            uiid=(i >> 4) & 0xF,
            ss=(i >> 2) & 0b11,
            ds=i & 0b11,
        )
        out["msg"] = C1_MID_TO_NAME.get(out["c1mid"])
    elif cls == 6:
        out.update(
            c6mid=(i >> 16) & 0x3FF,
            dd10=(i >> 6) & 0x3FF,
            sds=(i >> 4) & 0b11,
            uiid=i & 0xF,
        )
        out["msg"] = C6_MID_TO_NAME.get(out["c6mid"])
    return out


# ------------------------------------------------------- message payloads ----
@dataclass
class MessageSpec:
    name: str
    cls: int
    mid: int
    payload: tuple[tuple[str, int], ...] = ()


MESSAGE_SPECS: dict[str, MessageSpec] = {}
for name, mid in SC_MID_ASSIGNMENTS.items():
    MESSAGE_SPECS[name] = MessageSpec(
        name=name, cls=5, mid=mid,
        payload={
            # ¶0152: 'The Device Startup message ID is unique to each type of
            # device' — the equipment class travels with the startup message;
            # our stack carries it in the payload's et field (TODO(sniff):
            # confirm the real per-class MID scheme from captured traffic).
            "DEVICE_STARTUP": (("dd", 32), ("et", 8), ("cf", 8)),
            "DEVICE_DD": (("dd", 32), ("et", 8), ("cf", 8)),
            "SC_STARTUP": (("dd", 32),),
            "SC_COORDINATOR": (("dd", 32), ("spl", 4), ("dpl", 8), ("prn", 12), ("cf", 8)),
            "SC_DECLARATION": (("dd", 32), ("spl", 4), ("dpl", 8), ("prn", 12), ("cf", 8)),
            "SC_ASSIGNMENT": (("et", 8), ("subnet", 2), ("cf", 8), ("dd", 32)),
            "DEVICE_ASSIGNMENT_ACK": (("et", 8), ("dd", 32)),
        }.get(name, ()),
    )
for name, mid in BROADCAST_MID_ASSIGNMENTS.items():
    MESSAGE_SPECS[name] = MessageSpec(name=name, cls=3, mid=mid, payload={
        "aSC_HEARTBEAT": (("dd", 32),),
        "CONFIGURATION_ALARM": (("alarm_no", 10), ("src_dd", 32), ("set_clear", 1)),
        "DEVICE_SOFT_DISABLE_ALARM": (("alrm_device_dd", 32),),
        "DEVICE_COMMUNICATIONS_PROBLEM_ALARM": (("src_dd", 32),),
        "DEVICE_ALARM_REPORT": (("alarm_no", 10), ("set_clear", 1), ("priority", 2), ("src_dd", 32)),
        "INCOMPLETE_SYSTEM_ALARM": (("src_dd", 32),),
        "MISSING_DEVICE_ALARM": (("alarm_no", 10), ("missing_et", 8), ("src_dd", 32)),
    }.get(name, ()))
for name, mid in UIG_MID_ASSIGNMENTS.items():
    MESSAGE_SPECS[name] = MessageSpec(name=name, cls=1, mid=mid, payload={
        "UI_G_HARD_DISABLE_CMD": (("et", 8),),
        "UI_G_HARD_ENABLE_CMD": (("et", 8),),
    }.get(name, ()))
for name, mid in DIAG_MID_ASSIGNMENTS.items():
    MESSAGE_SPECS[name] = MessageSpec(name=name, cls=6, mid=mid, payload={
        "DIAG_RESPONSE": (("et", 8), ("dd10", 10)),
    }.get(name, ()))

MESSAGE_SPECS["SC_CHANGE_STATE"] = MessageSpec(name="SC_CHANGE_STATE", cls=5, mid=SC_MID_ASSIGNMENTS["SC_CHANGE_STATE"], payload=(("cf", 8),))
MESSAGE_SPECS["DEVICE_UI_G_ENABLE_ACK"] = MessageSpec(name="DEVICE_UI_G_ENABLE_ACK", cls=5, mid=SC_MID_ASSIGNMENTS["DEVICE_UI_G_ENABLE_ACK"], payload=(("et", 8),))
MESSAGE_SPECS["DIAG_RESPONSE"] = MessageSpec(name="DIAG_RESPONSE", cls=6, mid=DIAG_MID_ASSIGNMENTS["DIAG_RESPONSE"], payload=(("et", 8), ("dd10", 10)))


def encode_msg(name: str, fields: dict[str, int] | None = None, *, ds: int = 0, ss: int = 0, et: int = 0, as_all: int = 0, uiid: int = 0, al: int = 0, tp: int = 0, src_et: int = 0, addr_args: dict | None = None) -> tuple[int, bytes]:
    """Build (arb_id, data) for a named RSBus message. `fields` supplies
    payload values (missing ones default to 0); data is packed MSB-first in
    8-bit chunks in spec's field order, padded to whole bytes."""
    spec = MESSAGE_SPECS[name]
    f = fields or {}
    builder = BUILDERS[spec.cls]
    import inspect
    builder_params = inspect.signature(builder).parameters
    addr_kwargs = dict(ds=ds, ss=ss, et=et, as_all=as_all, uiid=uiid,
                       al=al, tp=tp, src_et=src_et)
    addr_kwargs.update(addr_args or {})
    accepted: dict[str, int] = {"mid": spec.mid}
    for key, value in addr_kwargs.items():
        if key in builder_params:
            accepted[key] = value & 0xFF
    arb = builder(**accepted)
    # pack payload bits MSB-first
    bits = 0
    total = 0
    for fname, width in spec.payload:
        bits = (bits << width) | (int(f.get(fname, 0)) & ((1 << width) - 1))
        total += width
    pad = (-total) % 8
    bits <<= pad
    total += pad
    return arb, bits.to_bytes(total // 8, "big")


# ---------------------------------------------------------------------------
# pluggable message map
# ---------------------------------------------------------------------------
# The FSM layer never sees MIDs: transitions fire on EVENT NAMES (see
# app.py's Dispatch), and the name comes from these tables. So the numeric
# assignments are the ONLY wire-format-specific values in the library, and
# they are deliberately replaceable: a JSON file shaped like
#
#   {"class5": {"0x106": "SC_COORDINATOR"}, "class3": {"0x1": "aSC_HEARTBEAT"}}
#
# merges over the placeholder assignments at startup (set_message_map),
# letting captured real Lennox numbering drop in without code changes.
# rebuild_to_name_map regenerates the reverse lookups after every merge.


def set_message_map(path: str | Path) -> dict:
    """Merge a JSON message-map override into the runtime tables. Returns
    the merged override dict."""
    overrides = json.loads(Path(path).read_text())
    merged: dict[str, dict[str, int]] = {}
    for cls_key, table in overrides.items():
        if not isinstance(table, dict):
            continue
        if cls_key == "class5":
            for mid_s, name in table.items():
                SC_MID_ASSIGNMENTS[name] = int(mid_s, 0)
                merged.setdefault("class5", {})[mid_s] = name
        elif cls_key == "class3":
            for mid_s, name in table.items():
                BROADCAST_MID_ASSIGNMENTS[name] = int(mid_s, 0)
                merged.setdefault("class3", {})[mid_s] = name
        elif cls_key == "class1":
            for mid_s, name in table.items():
                UIG_MID_ASSIGNMENTS[name] = int(mid_s, 0)
                merged.setdefault("class1", {})[mid_s] = name
        elif cls_key == "class6":
            for mid_s, name in table.items():
                DIAG_MID_ASSIGNMENTS[name] = int(mid_s, 0)
                merged.setdefault("class6", {})[mid_s] = name
    _rebuild_name_maps()
    return merged


def _rebuild_name_maps() -> None:
    global C5_MID_TO_NAME, C3_MID_TO_NAME, C1_MID_TO_NAME, C6_MID_TO_NAME
    C5_MID_TO_NAME = {v: k for k, v in SC_MID_ASSIGNMENTS.items()}
    C3_MID_TO_NAME = {v: k for k, v in BROADCAST_MID_ASSIGNMENTS.items()}
    C1_MID_TO_NAME = {v: k for k, v in UIG_MID_ASSIGNMENTS.items()}
    C6_MID_TO_NAME = {v: k for k, v in DIAG_MID_ASSIGNMENTS.items()}


def decode_msg(arb_id: int, data: bytes) -> dict[str, Any]:
    d = decode_id(arb_id)
    d["data_hex"] = data.hex().upper()
    spec = MESSAGE_SPECS.get(d.get("msg") or "")
    if spec and d["class"] == spec.cls:
        # unpack payload bits MSB-first
        raw = int.from_bytes(data, "big") if data else 0
        pos = len(data) * 8
        fields: dict[str, int] = {}
        for fname, width in spec.payload:
            pos -= width
            if pos < 0:
                break
            fields[fname] = (raw >> pos) & ((1 << width) - 1)
        d["fields"] = fields
        d["payload_spec"] = [n for n, _ in spec.payload]
    elif d["class"] in (1, 3, 5, 6):
        d.setdefault("fields", {})
        d.setdefault("payload_spec", [])
    return d


class MessageNameLearner:
    """Stores unknown-MID observations so real bus traffic can rename our
    placeholder message map later (`--learn path`). Thread-safe."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self.counts: dict[str, dict[str, int]] = {}
        self._load()

    def _load(self) -> None:
        if self.path and self.path.exists():
            with self._lock:
                self.counts = json.loads(self.path.read_text())

    def observe(self, d: dict[str, Any]) -> None:
        if d.get("msg"):
            return
        key = f"class{d['class']}"
        sub = self.counts.setdefault(key, {})
        if key == "class5":
            sub_key = f"{d['c5mid']:#x}"
        elif key == "class3":
            sub_key = f"{d['c3mid']:#x}"
        elif key == "class1":
            sub_key = f"{d['c1mid']:#x}"
        elif key == "class6":
            sub_key = f"{d['c6mid']:#x}"
        else:
            sub_key = "0x0"
        with self._lock:
            sub[sub_key] = sub.get(sub_key, 0) + 1
        self.flush()

    def flush(self) -> None:
        if self.path and self.counts:
            self.path.write_text(json.dumps(self.counts, indent=2, sort_keys=True))


def pretty_decode(d: dict[str, Any]) -> str:
    """One-line human-readable decode for the monitor."""
    name = d.get("msg") or "?"
    fields = d.get("fields") or {}
    inner = ",".join(
        f"{k}={v:#x}" if isinstance(v, int) and v > 9 else f"{k}={v}"
        for k, v in fields.items())
    return f"{d['class_name']:9s} {name:28s} id={d['raw']:#08X} data[{d['data_hex']}] {inner}".rstrip()

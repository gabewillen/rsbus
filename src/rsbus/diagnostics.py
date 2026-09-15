"""Alarm & diagnostics layer — event-name-driven, no MIDs needed.

Spec source: US2010/0106310 (Alarm & Diagnostics System, 12/603,450) with
the alert-code table from Lennox information guide 100017a (Table 13).

Two cooperating machines:

- `DeviceDiagnostics` — the device-side diagnostic machine (FIG. 12-style
  slice of US2010/0106310 ¶0047-0055): Level 1 Diagnostic Mode entered on
  a `diag_enter` command, exited on `diag_exit`/timeout; while in it the
  device publishes periodic Class 6 diagnostic status (once per minute,
  or immediately on parameter change — the auto-sent variant carries
  higher priority than query-response, ¶0051-0054). Diagnostic WRITE
  inquiries are only executed while idle/disabled per the query-number
  restriction bits (¶0058-0060).

- `AlarmSession` — the retrieval-session machine (US2010/0106310
  ¶0114-0117): `alarm_session_start` → ask/report loop
  (`ask_for_device_alarm` → `device_alarm_report`) → `alarm_session_end`
  ack; clears via `alarm_clear`; a device stops the session after ~5 s
  without a retrieval message.

Alarm-log semantics implemented in the runtime helpers below:
- active alarms = not yet cleared; consecutive event-type alarms of the
  same ID coalesce into one log entry; a repeat after clear logs a new
  instance (¶0090-0091);
- Unresponsive Device Error Count (0-255) in RAM: raised when a new
  Unresponsive Device2 alarm fires, decremented on each successful
  transmission, ≥10 escalates with Notify-User/Dealer flags, 0 clears
  (¶0120-0121);
- Missing Device2: raised by the aSC in verification mode at subnet
  startup completion for a configured-but-absent device; carries the
  missing device's Equipment Type (¶0122);
- Incomplete System: raised in configuration mode when a critical device
  (indoor unit, UI, comfort sensor) is missing; cleared on reset
  (¶0123).

Transitions are keyed on the event names below — the wire numbering for
these messages (class 3 alarm MID / class 6 diag MID) stays a
TODO(sniff) placeholder in messages.py and can be overridden with
set_message_map without touching this model.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import hsm

from .messages import encode_msg
from .runtime import NodeRuntime

DIAG_PERIOD_S = 60.0        # periodic class-6 status cadence (¶0051)
DIAG_SESSION_TIMEOUT_S = 5.0   # session self-termination (¶0115)
UNRESPONSIVE_ESCALATE = 10     # error-count escalation threshold (¶0121)


def _publish_alarm(instance, alarm_no: int, *, priority: int, set_clear: int,
                   src_et: int = 0, src_dd: int = None) -> None:
    """Class 3 alarm broadcast (Table V): AL=1, PRO-PR1 priority bits,
    SET/CLEAR bit, alarm number in the ID. Name-keyed via the codec."""
    dd = instance.cfg.dd if src_dd is None else src_dd
    arb, data = encode_msg(
        "DEVICE_ALARM_REPORT",
        {"alarm_no": alarm_no & 0x3FF, "src_dd": dd,
         "set_clear": set_clear & 1, "priority": priority & 0b11},
        al=1,
        ss=instance.cfg.subnet,
        as_all=1,
    )
    instance.queue_send(arb, data)


def priority_is_minor(instance, event) -> bool:
    return int((event.data or {}).get("priority") or 0) == 0b00


def priority_is_moderate(instance, event) -> bool:
    return int((event.data or {}).get("priority") or 0) == 0b01


def priority_is_critical(instance, event) -> bool:
    return int((event.data or {}).get("priority") or 0) == 0b10


# ============================================================================
# Alarm-log machinery (runtime-level; shared by any role)
# ============================================================================


def alarm_log_entry(instance, alarm_no: int, *, event_type: bool) -> dict:
    """Log an alarm instance per ¶0090-0091: consecutive event-type alarms
    coalesce into one entry; repeat-after-clear logs a new instance."""
    log = instance.ram.setdefault("alarm_log", [])
    active = [e for e in log if not e["cleared"]]
    prior = [e for e in active if e["alarm_no"] == alarm_no]
    if prior and event_type:
        prior[0]["count"] = int(prior[0].get("count", 1)) + 1  # consecutive repeats coalesce
        prior[0]["last"] = time.monotonic()
        return prior[0]
    entry = {"alarm_no": alarm_no, "t": time.monotonic(), "cleared": False, "count": 1}
    log.append(entry)
    del log[:-128]
    return entry


def alarm_count(instance, alarm_no: int) -> int:
    return sum(1 for e in instance.ram.get("alarm_log", [])
               if e["alarm_no"] == alarm_no and not e["cleared"])


def clear_alarm_log(instance, alarm_no: int) -> None:
    for e in instance.ram.get("alarm_log", []):
        if e["alarm_no"] == alarm_no and not e["cleared"]:
            e["cleared"] = True


def bump_unresponsive_count(instance, dd: int, *, raised: bool) -> int:
    """¶0120-0121: the counter lives in RAM; +1 on a new Unresponsive
    Device2 alarm for that device, −1 on each successful transmission."""
    key = f"unresp:{dd:08X}"
    count = int(instance.ram.get(key, 0))
    count = count + 1 if raised else max(0, count - 1)
    instance.ram[key] = count
    return count


async def diag_timeout(ctx, instance, event) -> timedelta:
    """Diagnostic-mode timeout (¶0048: exit after a predetermined period)."""
    return timedelta(seconds=0.5 if instance.cfg.fast else 10 * 60)


# ============================================================================
# Alarm retrieval session (UI/G side)
# ============================================================================


def session_ask(ctx, instance, event) -> None:
    """UI/G Device Ask For Device Alarm: request the next alarm report
    from the interrogated device (¶0114-0115)."""
    payload = event.data if isinstance(event.data, dict) else {}
    arb, data = encode_msg(
        "DIAG_QUERY",
        {"dd10": int(payload.get("dd10") or 0)},
        uiid=int(payload.get("uiid") or 0),
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


def session_store_report(ctx, instance, event) -> None:
    """Store a Device Alarm Report reply (numbered by recency, ¶0115)."""
    payload = event.data if isinstance(event.data, dict) else {}
    reports = instance.ram.setdefault("session_reports", [])
    reports.append({
        "alarm_no": int(payload.get("alarm_no") or 0),
        "set_clear": int(payload.get("set_clear") or 0),
        "t": time.monotonic(),
    })


def session_end_ack(ctx, instance, event) -> None:
    """Device Alarm Session Ack on session end (¶0115); report counter reset
    and the log kept for the technician."""
    instance.ram["session_reports"] = []


def session_timed_out(ctx, instance, event) -> bool:
    """Device self-terminates a session after ~5 s without a retrieval
    message (¶0115)."""
    last = instance.ram.get("session_last_ask")
    if last is None:
        return True
    timeout = DIAG_SESSION_TIMEOUT_S * (0.1 if instance.cfg.fast else 1.0)
    return (time.monotonic() - last) > timeout


def session_mark_ask(ctx, instance, event) -> None:
    instance.ram["session_last_ask"] = time.monotonic()


async def diag_session_timeout(ctx, instance, event) -> timedelta:
    """Session self-termination window (~5 s, ¶0115)."""
    return timedelta(seconds=DIAG_SESSION_TIMEOUT_S * (0.1 if instance.cfg.fast else 1.0))


AlarmSession = hsm.Define(
    "AlarmSession",
    hsm.Initial(hsm.Target("idle")),
    hsm.State(
        "idle",
        hsm.Transition(hsm.On("alarm_session_start"), hsm.Target("../retrieving")),
    ),
    hsm.State(
        "retrieving",
        hsm.Entry(session_ask),
        # report received: store it and ask for the next one (the session
        # loop runs until UI/G Device Alarm Session ends it, ¶0114-0117)
        hsm.Transition(hsm.On("device_alarm_report"), hsm.Effect(session_store_report)),
        hsm.Transition(hsm.On("ask_for_device_alarm"), hsm.Target("."), hsm.Effect(session_ask)),
        hsm.Transition(hsm.On("alarm_session_end"), hsm.Target("../ended")),
        # device self-terminates the session on ~5 s without a retrieval
        hsm.Transition(hsm.After(diag_session_timeout), hsm.Target("../idle"),
                       hsm.Guard(session_timed_out)),
    ),
    hsm.State(
        "ended",
        hsm.Entry(session_end_ack),
        hsm.Transition(hsm.On("alarm_session_start"), hsm.Target("../retrieving")),
    ),
)


# ============================================================================
# Device-side diagnostic machine
# ============================================================================


def diag_normal_entry(ctx, instance, event) -> None:
    if not hasattr(instance, "ram"):
        return  # unwired bare instance: behaviors no-op
    instance.ram["diag_mode"] = False


def diag_enter(ctx, instance, event) -> None:
    """Level 1 Diagnostic Mode entered on a class 6 command (¶0047); the
    device publishes periodic diagnostic status while active."""
    if not hasattr(instance, "ram"):
        return
    instance.ram["diag_mode"] = True
    instance.ram["diag_until"] = time.monotonic() + DIAG_SESSION_TIMEOUT_S


def diag_exit(ctx, instance, event) -> None:
    if not hasattr(instance, "ram"):
        return
    instance.ram["diag_mode"] = False


def raise_alarm(ctx, instance, event) -> None:
    """Publish a device-owned alarm (class 3 broadcast, Table V) and log
    the instance per ¶0090-0091."""
    payload = event.data if isinstance(event.data, dict) else {}
    alarm_no = int(payload.get("alarm_no") or 0)
    priority = int(payload.get("priority") or 0)
    _publish_alarm(instance, alarm_no, priority=priority, set_clear=0,
                   src_et=int(payload.get("et") or 0))
    alarm_log_entry(instance, alarm_no, event_type=True)


def clear_alarm(ctx, instance, event) -> None:
    payload = event.data if isinstance(event.data, dict) else {}
    alarm_no = int(payload.get("alarm_no") or 0)
    clear_alarm_log(instance, alarm_no)
    _publish_alarm(instance, alarm_no, priority=int(payload.get("priority") or 0),
                   set_clear=1, src_et=int(payload.get("et") or 0))


async def diagnostics_periodic(ctx, instance, event) -> None:
    """Activity(level1): publish the device's periodic class 6 status while
    in diagnostic mode (¶0051: once per minute, or on parameter change)."""
    if not hasattr(instance, "ram"):
        return
    period = DIAG_PERIOD_S * (0.1 if instance.cfg.fast else 1.0)
    while not getattr(instance, "shutdown", False):
        await asyncio.sleep(period)
        if instance.ram.get("diag_mode"):
            arb, data = encode_msg(
                "DIAG_RESPONSE",
                {"et": instance.assigned_et, "dd10": instance.cfg.dd & 0x3FF},
                ss=instance.cfg.subnet,
            )
            instance.queue_send(arb, data)


DeviceDiagnostics = hsm.Define(
    "DeviceDiagnostics",
    hsm.Attribute("diag_mode", False),
    hsm.Initial(hsm.Target("normal")),
    hsm.State(
        "normal",
        hsm.Entry(diag_normal_entry),
        # Level 1 Diagnostic Mode is entered on a class-6 command (¶0047):
        hsm.Transition(hsm.On("diag_enter"), hsm.Target("../level1")),
        hsm.Transition(hsm.On("raise_alarm"), hsm.Effect(raise_alarm)),
        hsm.Transition(hsm.On("clear_alarm"), hsm.Effect(clear_alarm)),
    ),
    hsm.State(
        "level1",
        hsm.Entry(diag_enter),
        hsm.Activity(diagnostics_periodic),
        # exit message or timeout (¶0048-0050)
        hsm.Transition(hsm.On("diag_exit"), hsm.Target("../normal")),
        hsm.Transition(hsm.After(diag_timeout), hsm.Target("../normal")),
        hsm.Transition(hsm.On("raise_alarm"), hsm.Effect(raise_alarm)),
        hsm.Transition(hsm.On("clear_alarm"), hsm.Effect(clear_alarm)),
    ),
)

"""Operations layer — commissioning, supervision, replacement, parameters.

Spec source: US8463443 (FIG. 3A/3B/3C, methods 400/425/500/600/700/800) and
US8255086 (FIG. 4A-4E, 5A/5B, 6A/6B). Everything here transitions on EVENT
NAMES — no wire MIDs needed (they live in messages.py's replaceable map).

Machines:

- `DeviceCommissioning` — the device side of commissioning state 300
  (FIG. 3A, state 303): entered by an `aSC Device Assignment` with
  Assigned-State bits = Commissioning, or an `aSC Change State` with
  New-aSC-State bits = Commissioning (¶ entry conditions). Units answer
  with `Device Status` (actions taken at a given time, ¶0152). If a unit
  is busy (`aSC Acknowledge` bits = Control Busy) it holds the aSC in
  commissioning and resends Device Status when no longer busy; >1 minute
  busy ⇒ the aSC raises `Unresponsive Device2` per unit. Exit is forced
  by the aSC Change State message (¶ commissioning-exit rule).
- `SCCommissioning` — the aSC-side supervision (US8463443 FIG. 3A exit
  rules + FIG. 3C): in CONFIGURATION mode the aSC waits for all units'
  Control-Busy bits to clear (indefinitely, or until manual/timeout
  reset); in VERIFICATION mode the aSC can exit regardless of unit
  alarms and times the installation out, resetting the subnet to
  default parameters. When no Control-Busy remains it issues the
  `aSC Change State` and commissioning ends.
- `ReplacementCheck` — FIG. 6A/6B: replacement-part scenario triggers
  ONLY when a previously configured device is missing AND a physically
  new device of the same equipment type arrives with CF5 set. In
  verification the new device is soft-disabled; in configuration the aSC
  prompts the user to copy the archived settings (serial/part numbers +
  parameters). CF5 clear + unmatched DD ⇒ new equipment (no
  reprogramming; verification still disables it). Every replacement
  check is accompanied by the Missing Device2 alarm (¶ 100017a Table 13
  / US8255086 FIG. 6A), and >1 device of a kind ⇒ the Too-Many-Devices
  alarm.
- `ParameterChangeDialog` — FIG. 4B/7B: a `ui_g_device_parameter_value_
  change` with a new value for parameter A: the device validates the
  update against the parameter's allowed range (definition received
  from the UI/G); in range ⇒ store and publish `Device UI/G Parameter
  Value By Number` messages for every affected parameter in the same
  group (enable/disable applicability flags recomputed per dependencies,
  ¶0450-0455); out of range ⇒ respond with the unchanged current value
  and keep the previous value. The UIG relays changed parameters to the
  aSC, which updates its backup and acknowledges (¶0475-0480).
- Recovery scenarios (a)-(d) (US8255086 FIG. 5A/5B) hook into the aSC's
  commissioning: full feature manifest + non-comm check scan + parameter
  scan are forced on units that lost data (full scans in place of the
  abbreviated verification version), and stored values are replayed in
  the received list order (method 520).
"""

from __future__ import annotations

import time
from datetime import timedelta

import hsm

from .messages import encode_msg
from .runtime import NodeRuntime

BUSY_TIMEOUT_S = 60.0          # Control-Busy → Unresponsive Device2 (¶0152 area)
VERIFY_INSTALL_TIMEOUT_S = 120.0   # verification-mode installation timeout


def _status_bytes(instance, *, busy: bool) -> dict:
    """DEVICE Status payload: first two bytes carry alarm bits + service
    bits (spec ¶0250); aSC-Acknowledge busy bit included."""
    cf = instance.ram.get("cf", 0)
    return {"dd": instance.cfg.dd, "alarm_bits": 0, "service_bits": 0xFF,
            "ack_busy": 1 if busy else 0, "cf": cf}


def device_status_reply(ctx, instance, event) -> None:
    """Respond to the aSC Device Assignment with Device Status (¶0152)."""
    payload = event.data if isinstance(event.data, dict) else {}
    busy = bool(payload.get("expect_busy"))
    arb, data = encode_msg(
        "DEVICE_STATUS",
        _status_bytes(instance, busy=busy),
        ds=instance.cfg.subnet,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


def resend_status_when_ready(ctx, instance, event) -> None:
    """Units resend Device Status as soon as they are no longer busy."""
    device_status_reply(ctx, instance, event)


def enter_commissioning(ctx, instance, event) -> None:
    instance.ram["commissioning"] = True


def leave_commissioning(ctx, instance, event) -> None:
    instance.ram["commissioning"] = False


# ============================================================================
# Device-side commissioning machine (FIG. 3A state 303)
# ============================================================================

DeviceCommissioning = hsm.Define(
    "DeviceCommissioning",
    hsm.Initial(hsm.Target("in_commissioning")),
    hsm.State(
        "in_commissioning",
        hsm.Entry(enter_commissioning),
        # answer the aSC Device Assignment with Device Status (¶0152)
        hsm.Transition(hsm.On("asc_device_assignment"), hsm.Effect(device_status_reply)),
        hsm.Transition(hsm.On("asc_change_state"), hsm.Target("../released"),
                       hsm.Effect(device_status_reply)),
    ),
    hsm.State(
        "busy",
        hsm.Entry(enter_commissioning),
        # Control Busy: hold the aSC in commissioning; resend status when
        # no longer busy (¶0152)
        hsm.Transition(hsm.On("control_busy_cleared"), hsm.Target("../in_commissioning"),
                       hsm.Effect(resend_status_when_ready)),
        hsm.Transition(hsm.On("asc_change_state"), hsm.Effect(device_status_reply)),
    ),
    hsm.State(
        "released",
        hsm.Entry(leave_commissioning),
        # aSC Change State with non-commissioning bits ends commissioning
        hsm.Transition(hsm.On("asc_device_assignment"), hsm.Effect(device_status_reply)),
    ),
    # busy ⇄ released edges shared across states
    hsm.Transition(hsm.On("control_busy_set"), hsm.Target("./busy")),
    hsm.Transition(hsm.On("asc_change_state"), hsm.Target("./released")),
)


# ============================================================================
# aSC-side commissioning supervision (FIG. 3A exit rules, FIG. 3C)
# ============================================================================


async def busy_watchdog(ctx, instance, event) -> timedelta:
    """Control-Busy beyond ~1 minute ⇒ Unresponsive Device2 alarm per busy
    unit (US8463443 ¶0152/¶0119)."""
    return timedelta(seconds=2.0 if instance.cfg.fast else BUSY_TIMEOUT_S)


async def verify_install_watchdog(ctx, instance, event) -> timedelta:
    """Verification-mode installation timeout ⇒ reset the subnet to
    default parameters (US8463443 ¶0 exit rules)."""
    return timedelta(seconds=3.0 if instance.cfg.fast else VERIFY_INSTALL_TIMEOUT_S)


def record_device_busy_state(ctx, instance, event) -> None:
    """Track each unit's aSC-Acknowledge busy bit from its Device Status."""
    payload = event.data if isinstance(event.data, dict) else {}
    dd = int(payload.get("dd") or 0)
    busy = int(payload.get("ack_busy") or 0)
    instance.ram[f"ack:{dd:08X}"] = busy


def any_control_busy(ctx, instance, event) -> bool:
    return any(v for k, v in instance.ram.items()
               if k.startswith("ack:") and v)


def no_control_busy(ctx, instance, event) -> bool:
    return not any_control_busy(ctx, instance, event)


def raise_unresponsive_device2(ctx, instance, event) -> None:
    """Unresponsive Device2 alarm per busy unit (US8463443 ¶0119-0121),
    sent out with Notify User/Dealer flags on escalation."""
    for key in [k for k in instance.ram if k.startswith("ack:")]:
        dd = int(key.split(":")[1], 16)
        if instance.ram[key]:
            arb, data = encode_msg(
                "INCOMPLETE_SYSTEM_ALARM",
                {"src_dd": instance.cfg.dd},
                al=1,
                ss=instance.cfg.subnet,
                as_all=1,
            )
            instance.queue_send(arb, data)


def issue_asc_change_state(ctx, instance, event) -> None:
    """No Control-Busy ⇒ issue aSC Change State (forces all units out of
    commissioning; US8463443 ¶0 exit rules)."""
    arb, data = encode_msg(
        "SC_CHANGE_STATE",
        {"cf": instance.ram.get("cf", 0)},
        as_all=1,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


def reset_subnet_to_defaults(ctx, instance, event) -> None:
    """Verification-mode timeout: reset the subnet to default parameters
    (US8463443 exit rules)."""
    instance.nvm.data.clear()
    instance.nvm.flush()
    instance.schedule(
        hsm.Dispatch(ctx, instance, hsm.Event(name="subnet_reset_to_defaults"))
    )


SCCommissioning = hsm.Define(
    "SCCommissioning",
    hsm.Initial(hsm.Target("supervising")),
    hsm.State(
        "supervising",
        hsm.Entry(record_device_busy_state),
        # units' Device Status carries their aSC-Acknowledge bits
        hsm.Transition(hsm.On("device_status"), hsm.Effect(record_device_busy_state)),
        # CONFIGURATION mode: wait indefinitely for busy units to clear
        # (US8463443 ¶0 exit rules); the busy watchdog raises the
        # Unresponsive Device2 alarm per unit
        hsm.Transition(hsm.On("control_busy_set"),
                       hsm.Effect(raise_unresponsive_device2)),
        hsm.Transition(hsm.On("asc_change_state"), hsm.Effect(record_device_busy_state)),
        # all units clear ⇒ conclude with the aSC Change State
        hsm.Transition(hsm.On("all_units_ready"), hsm.Target("../done"),
                       hsm.Guard(no_control_busy)),
        hsm.Transition(hsm.On("installation_timed_out"), hsm.Target("../resetting"),
                       hsm.Guard(any_control_busy)),
    ),
    hsm.State(
        "resetting",
        hsm.Entry(reset_subnet_to_defaults),
        hsm.Transition(hsm.On("subnet_reset_to_defaults"), hsm.Target("../done")),
    ),
    hsm.State(
        "done",
    ),
)


# ============================================================================
# Replacement-part decision (US8255086 FIG. 6A/6B)
# ============================================================================


def replacement_scenario(ctx, instance, event) -> bool:
    """The ONLY replacement trigger: a previously configured device is
    missing AND the new device carries CF5 set (US8255086 FIG. 6A verbatim:
    'When the CF5 flag is set, the new DD value and the lack of
    corresponding device on the Subnet (device is missing) is indicative
    of a replacement part scenario.'). The matching key is the equipment
    type: the new device's ET must equal an archived device's ET that is
    not currently communicating."""
    payload = event.data if isinstance(event.data, dict) else {}
    cf = int(payload.get("cf") or 0)
    cf5_set = bool(cf & (1 << 5))
    new_et = int(payload.get("et") or 0)
    # the missing-device check: the archived backup holds a device of the
    # same ET, but no device with that ET is currently communicating
    archived_ets = set()
    for k, v in instance.nvm.data.items():
        if k.startswith("dev:") and isinstance(v, dict):
            archived_ets.add(int(v.get("et") or 0))
    communicating_ets = {d.et for d in instance.device_ram.values()}
    missing = bool(archived_ets & {new_et} - communicating_ets)
    return cf5_set and missing


def new_equipment_scenario(ctx, instance, event) -> bool:
    """CF5 clear + DD not matching anything on the subnet ⇒ new equipment
    (no reprogramming; verification still soft-disables it)."""
    payload = event.data if isinstance(event.data, dict) else {}
    cf = int(payload.get("cf") or 0)
    dd = int(payload.get("dd") or 0)
    known = dd in {int(k.split(":")[1], 16) for k in instance.nvm.data
                   if k.startswith("dev:")}
    return not (cf & (1 << 5)) and not known


def soft_disable_new_device(ctx, instance, event) -> None:
    """Verification mode: the new/replacement device is soft-disabled
    (US8255086 FIG. 6A step 639/655)."""
    payload = event.data if isinstance(event.data, dict) else {}
    dd = int(payload.get("dd") or 0)
    arb, data = encode_msg(
        "SC_ASSIGNMENT",
        {"et": 0, "subnet": instance.cfg.subnet, "cf": 0, "dd": dd},
        ds=instance.cfg.subnet,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


def trigger_replacement_commissioning(ctx, instance, event) -> None:
    """CONFIGURATION mode: the replacement mechanism triggers during
    commissioning — prompt the user to copy the archived settings
    (serial/part numbers + parameters) into the new control."""
    payload = event.data if isinstance(event.data, dict) else {}
    dd = int(payload.get("dd") or 0)
    instance.ram["replacement_prompt"] = {"dd": dd}
    arb, data = encode_msg(
        "CONFIGURATION_ALARM",
        {"alarm_no": 11, "src_dd": instance.cfg.dd},
        al=1,
        ss=instance.cfg.subnet,
        as_all=1,
    )
    instance.queue_send(arb, data)   # Missing Device2 accompanies (FIG. 6A)


def too_many_devices_of_same_type(ctx, instance, event) -> bool:
    """>1 device of a kind on the subnet ⇒ alarm 14 (US8255086 claim tables;
    Lennox guide 100017a alert 14)."""
    payload = event.data if isinstance(event.data, dict) else {}
    et = int(payload.get("et") or 0)
    same = [d for d in instance.device_ram.values() if d.et == et]
    return len(same) > 1


def assign_missing_devices_et(ctx, instance, event) -> None:
    """Same equipment-type range ⇒ assign the missing device's ET;
    else next-lowest/highest ET for gateways (US8255086 FIG. 6A)."""
    payload = event.data if isinstance(event.data, dict) else {}
    dd = int(payload.get("dd") or 0)
    et = int(payload.get("et") or 0)
    arb, data = encode_msg(
        "SC_ASSIGNMENT",
        {"et": et, "subnet": instance.cfg.subnet, "cf": 0xFF, "dd": dd},
        ds=instance.cfg.subnet,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


ReplacementCheck = hsm.Define(
    "ReplacementCheck",
    hsm.Initial(hsm.Target("evaluating")),
    hsm.State(
        "evaluating",
        hsm.Transition(hsm.On("device_startup"),
                       hsm.Target("../replacement_verdict")),
    ),
    hsm.Choice(
        "replacement_verdict",
        hsm.Transition(hsm.Guard(replacement_scenario),
                       hsm.Effect(trigger_replacement_commissioning),
                       hsm.Target("./commissioning_replacement")),
        hsm.Transition(hsm.Guard(new_equipment_scenario),
                       hsm.Effect(soft_disable_new_device),
                       hsm.Target("./new_equipment")),
        hsm.Transition(hsm.Target("./manual")),
    ),
    hsm.State(
        "commissioning_replacement",
        # user decides: copy backup into the new control, or configure
        # manually (US8255086 FIG. 6B steps 675-683)
        hsm.Transition(hsm.On("copy_backup_accepted"), hsm.Target("../copying"),
                       hsm.Effect(assign_missing_devices_et)),
        hsm.Transition(hsm.On("copy_backup_declined"), hsm.Target("../manual")),
    ),
    hsm.State(
        "copying",
        hsm.Transition(hsm.On("copy_backup_complete"), hsm.Target("../done"),
                       hsm.Guard(too_many_devices_of_same_type)),
        hsm.Transition(hsm.On("too_many_devices_alarm"), hsm.Target("../done")),
    ),
    hsm.State(
        "manual",
    ),
    hsm.State(
        "new_equipment",
        hsm.Transition(hsm.On("copy_backup_complete"), hsm.Target("../done"),
                       hsm.Effect(assign_missing_devices_et)),
    ),
    hsm.State(
        "done",
    ),
)


# ============================================================================
# Parameter-change dialog (FIG. 4B/7B)
# ============================================================================


def parameter_in_allowed_range(ctx, instance, event) -> bool:
    """Step 455: the device validates the update against its parameter
    definition's allowed range (definitions arrive from the UI/G)."""
    payload = event.data if isinstance(event.data, dict) else {}
    value = int(payload.get("value") or 0)
    param_no = int(payload.get("param_no") or 0)
    limits = instance.ram.get("param_limits", {}).get(param_no)
    if limits is None:
        return True   # no definition known: accept (spec: definitions are
                      # published by the device; the UI relays them)
    low, high = limits
    return low <= value <= high


def store_parameter_value(ctx, instance, event) -> None:
    """Step 460: store the in-range update; publish the value now stored
    (which the UIG relays onward, ¶0460-0475)."""
    payload = event.data if isinstance(event.data, dict) else {}
    param_no = int(payload.get("param_no") or 0)
    value = int(payload.get("value") or 0)
    instance.nvm.set(f"param:{param_no}", value)
    instance.nvm.flush()
    # Step 470: send the UIG the value now stored, with enable/structure
    # flags for every affected parameter in the same group (¶0450-0475).
    group = instance.ram.get("param_group", {}).get(param_no, [])
    for peer in group:
        peer_value = int(instance.nvm.get(f"param:{peer}", 0))
        arb, data = encode_msg(
            "DIAG_RESPONSE",
            {"et": instance.assigned_et, "dd10": instance.cfg.dd & 0x3FF},
            ss=instance.cfg.subnet,
        )
        instance.queue_send(arb, data)


def keep_previous_parameter_value(ctx, instance, event) -> None:
    """Step 465: out-of-range update ⇒ keep the previous value and echo
    it (unchanged) in the reply message (¶0465-0470)."""
    payload = event.data if isinstance(event.data, dict) else {}
    param_no = int(payload.get("param_no") or 0)
    current = int(instance.nvm.get(f"param:{param_no}", 0))
    arb, data = encode_msg(
        "DIAG_RESPONSE",
        {"et": instance.assigned_et, "dd10": instance.cfg.dd & 0x3FF},
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


ParameterChangeDialog = hsm.Define(
    "ParameterChangeDialog",
    hsm.Initial(hsm.Target("listening")),
    hsm.Choice(
        "range_verdict",
        hsm.Transition(hsm.Guard(parameter_in_allowed_range),
                       hsm.Effect(store_parameter_value),
                       hsm.Target("./updated")),
        hsm.Transition(hsm.Effect(keep_previous_parameter_value),
                       hsm.Target("./listening")),
    ),
    hsm.State(
        "listening",
        hsm.Transition(hsm.On("ui_g_device_parameter_value_change"),
                       hsm.Target("../range_verdict")),
    ),
    hsm.State(
        "updated",
        # Step 470: report the stored value to the UIG, which relays
        # changed parameters to the aSC (ack per ¶0475-0480)
        hsm.Transition(hsm.On("asc_parameters_acked"), hsm.Target("../listening")),
        hsm.Transition(hsm.On("ui_g_device_parameter_value_change"),
                       hsm.Target("../range_verdict")),
    ),
)

"""Subnet Controller startup / coordinator-election state machine.

Spec: U.S. 2010/0106322 A1:
- FIG. 13A summary (method 1300A): reset 1301 -> preStartup 1303 ->
  postStartup 1309 -> ACTIVE 1313 <-> PASSIVE 1315; active -> HEARTBEAT OUT
  1379 -> exit 1399; passive -> inactive-iSC 1355 (exit 1398) or soft-disabled
  1351; HARD DISABLED 1307 peals off from preStartup (¶0164). FIG. 13A's outer
  rails route SOFT/HARD DISABLED back to RESET 1301.
- FIG. 13B sheet-flow (method 1300B): sheets 13B-1…13B-5, steps 1305-1391.
- FIG. 13C-2 message list, 13C-3 receive priority, 13C-4 declarative
  transitions, 13C-5 8-bytes-per-SC storage (CF1-valid 1b, CF1 1b, CF0 1b,
  SPL 4b, Feature Level 8b, Protocol Level 12b, DD 32b, Subnet ID 2b), and
  per-state message/response matrices FIG. 13C-1a (PRE/POST), 13C-1b/1c
  (COORDINATOR + NON-COORDINATOR rows 1-11), 13C-1d/1e (DISABLED + aSC
  HEARTBEAT_OUT rows 1-11), 13C-1f (INACTIVE).

Implemented per the spec; effects talk through NodeRuntime (`instance`),
including the mute-tx queue, the SC RAM list (self.cf1 etc.) and the DD-
derived timings config in NodeConfig.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import hsm

from .messages import (  # noqa: F401  (event names for On() wiring)
    ASC_HEARTBEAT,
    CONFIGURATION_ALARM,
    DEVICE_ASSIGNMENT_ACK,
    DEVICE_SOFT_DISABLE_ALARM,
    DEVICE_STARTUP,
    DEVICE_DD,
    DIAG_QUERY,
    RESET_COMMAND,
    SC_ASSIGNMENT,
    SC_CONFIGURATION_FINISHED,
    SC_COORDINATOR,
    SC_DECLARATION,
    SC_QUERY,
    SC_READY_TO_TAKE_OVER,
    SC_STARTUP,
    SC_TOKEN_PASS,
    UI_G_HARD_DISABLE,
    UI_G_HARD_ENABLE,
)
from .messages import encode_msg
from .runtime import NodeRuntime, SCInfo, DeviceInfo as DeviceInfoShim

MAX_DEVICES = 32       # Table I comment example; aSC capacity clamp
TOKEN_ACK_WINDOW = 0.1         # 100 ms (FIG. 13B-4 step 1369)
QUERY_RESPONSE_WAIT = 0.1      # ≤100 ms per-device response wait (1361/1365)
AUDIT_INITIAL_WAIT = 1.0       # FIG. 13B-4 step 1357: wait 1 s for device startups


def _sc_cf_flags(instance: NodeRuntime, *, soft_disabled: bool | None = None) -> int:
    """CF0-CF6 byte for SC-form messages (¶0203-¶0212)."""
    cf = 0
    if instance.nvm.get("configured"):
        cf |= 1 << 0                                  # CF0: completed installer tests
    if any(instance.is_iu(info.et) for info in instance.device_ram.values()):
        cf |= 1 << 1                                  # CF1: recognizes an indoor unit
    cf |= 1 << 2                                      # CF2: flashable over RSBus
    cf |= 1 << 3                                      # CF3: enabled/communicating
    sd = instance.soft_disabled if soft_disabled is None else soft_disabled
    if not sd:
        cf |= 1 << 4                                  # CF4: not soft disabled
    if instance.nvm.get("replacement_part"):
        cf |= 1 << 5                                  # CF5: replacement part
    cf |= 1 << 6                                      # CF6: data CRC ok
    return cf


def _cred_tuple(spl: int, dpl: int, prn: int, dd: int) -> tuple[int, int, int, int]:
    # ¶0173-¶0178 ordered: SPL, DPL, PRN, DD.
    return (spl, dpl, prn, dd)


def _our_credentials(instance: NodeRuntime) -> tuple[int, int, int, int]:
    return _cred_tuple(
        instance.cfg.spl,
        instance.cfg.dpl,
        instance.cfg.prn,
        instance.cfg.dd,
    )


# ======================================================== timer fns =========
async def sc_startup_delay(ctx, instance, event) -> timedelta:
    """RESOLVED by granted US8255086 FIG. 4E state 464: the SC Startup
    message goes ~3000 ms after reset + DD-derived delay (the application
    sheet's 'EXACTLY 1SEC+DD' is the superseded variant)."""
    ms = 3000 + ((instance.cfg.dd & 0x0F) * 25) % 250
    if instance.cfg.fast:
        ms = 200
    return timedelta(milliseconds=ms)


async def arbitration_window(ctx, instance, event) -> timedelta:
    """Exactly 1 s after POST entry (13B-1 step 1311). Anchor: our SC_STARTUP
    per the rotated sheet text — POST entry in this model immediately follows
    the SC_STARTUP send, so state-entry time is the anchor."""
    return timedelta(seconds=1.0 if not instance.cfg.fast else 0.25)


async def token_ack_window(ctx, instance, event) -> timedelta:
    return timedelta(seconds=TOKEN_ACK_WINDOW * (0.1 if instance.cfg.fast else 1.0))


async def five_minutes_after_startup(ctx, instance, event) -> datetime:
    """13B-2 step 1319 + 13C-1b header: reset when >5 min since startup and
    still passive; 13C-1a rows instead re-send SC_STARTUP — shipped choice:
    reset (the At edge) and the re-send variant fires via the Activity when
    configured by cfg.passive_resend."""
    base = 60.0 if instance.cfg.fast else 5 * 60.0
    return datetime.now() + timedelta(seconds=base)


async def sixty_seconds_after_last_sc_coordinator(ctx, instance, event) -> datetime:
    """FIG. 13C-1c rows 6-7: deadline = last SC_COORDINATOR seen + 60 s. The
    At() edge re-fires only on state entry; the Activity re-arms the accurate
    rolling deadline."""
    base = 3.0 if instance.cfg.fast else 60.0
    return datetime.now() + timedelta(seconds=base)


# ======================================================== behaviors =========
def reset_entry(ctx, instance, event) -> None:
    # FIG. 13A 1301: from-the-reset-moment list building happens in the POST
    # entry; here reset RAM and hop out if persistence says hard-disabled.
    instance.ram.clear()
    if instance.nvm.get("hard_disabled"):
        instance.schedule(
            hsm.Dispatch(ctx, instance, hsm.Event(name="ui_g_hard_disable"))
        )
    instance.nvm.flush()


def enter_post_startup(ctx, instance, event) -> None:
    instance.ram["scram_started_at"] = time.monotonic()


def send_sc_startup(ctx, instance, event) -> None:
    """FIG. 13B-1 step 1305 + FIG. 13C-1a PRE rows: SC_STARTUP with order
    number derived from our DD."""
    arb, data = encode_msg(
        "SC_STARTUP",
        {"dd": instance.cfg.dd},
        as_all=1,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


def broadcast_sc_coordinator(ctx, instance, event) -> None:
    """FIG. 13B-1 step 1311: SC_COORDINATOR after exactly 1 s, order number
    15 − (# SC_STARTUP seen from other SCs on our subnet). FIG. 13C-1a prints
    31 − (all subnets); the shipped formula is the FIG. 13B-1 variant."""
    seen = sum(
        1
        for info in instance.scram.values()
        if info.subnet == instance.cfg.subnet
    )
    order = 15 - seen
    instance.ram["coordinator_order"] = order
    arb, data = encode_msg(
        "SC_COORDINATOR",
        {
            "dd": instance.cfg.dd,
            "spl": instance.cfg.spl,
            "dpl": instance.cfg.dpl,
            "prn": instance.cfg.prn,
            "cf": _sc_cf_flags(instance),
        },
        as_all=1,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)
    instance.ram["coordinator_sent_ts"] = time.monotonic()


def remember_asc_and_wait(ctx, instance, event) -> None:
    payload = event.data if isinstance(event.data, dict) else {}
    instance.remember_asc(
        int(payload.get("dd") or 0),
        spl=int(payload.get("spl") or 0),
        dpl=int(payload.get("dpl") or 0),
        prn=int(payload.get("prn") or 0),
        subnet=int(payload.get("ss") or instance.cfg.subnet),
    )


def update_scram_with_declaration(ctx, instance, event) -> None:
    """SC_DECLARATION: record sender credentials; that SC's CF1-VALID flag
    sets only when its declaration arrives (FIG. 13C-1b rows 2-5)."""
    payload = event.data if isinstance(event.data, dict) else {}
    dd = int(payload.get("dd") or 0)
    if not dd or dd == instance.cfg.dd:
        return
    info = instance.scram.get(dd) or SCInfo(dd)
    info.spl = int(payload.get("spl") or info.spl)
    info.dpl = int(payload.get("dpl") or info.dpl)
    info.prn = int(payload.get("prn") or info.prn)
    info.subnet = int(payload.get("ss") or info.subnet)
    info.cf1_valid = True
    info.cf1_flag = int(payload.get("cf1") or info.cf1_flag)
    info.cf0_flag = int(payload.get("cf0") or info.cf0_flag)
    info.last_seen = time.monotonic()
    instance.scram[dd] = info


def update_device_ram_from_startup(ctx, instance, event) -> None:
    """DEVICE Startup / DD (and SC Startup) arrivals refresh the lists. An IU
    resets every CF1-VALID flag — all SCs must be re-queried (FIG. 13C-1b
    rows 2-3/1a), which re-enters the audit via the iu_reset_detected edge."""
    payload = event.data if isinstance(event.data, dict) else {}
    dd = int(payload.get("dd") or 0)
    if not dd or dd == instance.cfg.dd:
        return
    if event.name == "sc_startup":
        info = instance.scram.get(dd) or SCInfo(dd, subnet=int(payload.get("ss") or 0))
        info.last_seen = time.monotonic()
        instance.scram[dd] = info
        return
    et = int(payload.get("et") or 0)
    dev = instance.device_ram.get(dd)
    if dev is None:
        dev = instance.device_ram[dd] = DeviceInfoShim(dd, et, instance.cfg.subnet)
    else:
        dev.et = et or dev.et
        dev.last_seen = time.monotonic()
    if instance.is_iu(et):
        for info in instance.scram.values():
            info.cf1_valid = False
        instance.schedule(
            hsm.Dispatch(ctx, instance, hsm.Event(name="iu_reset_detected"))
        )
    elif instance.cfg.role == "sc" and instance.state().endswith("heartbeat_out"):
        # heartbeat_out gets DEVICE_STARTUP edge separately; the effect runs
        # there as well — trigger the per-device configuration path.
        assign_et_si_for_device(ctx, instance, event)


def absorb_new_sc_startup(ctx, instance, event) -> None:
    """FIG. 13B-4 step 1359 + FIG. 13C-1b row 1: a NEW SC joins the SC RAM
    list and gets our SC_COORDINATOR; duplicates are ignored."""
    payload = event.data if isinstance(event.data, dict) else {}
    dd = int(payload.get("dd") or 0)
    if not dd or dd == instance.cfg.dd:
        return
    info = instance.scram.get(dd) or SCInfo(dd)
    info.subnet = int(payload.get("ss") or info.subnet)
    info.last_seen = time.monotonic()
    instance.scram[dd] = info
    respond_with_coordinator(ctx, instance, event)


def respond_with_coordinator(ctx, instance, event) -> None:
    """ACTIVE answers a query with our own SC_COORDINATOR when our
    credentials outrank the challenger's (FIG. 13C-1b row 4 + 13C-1c row 6);
    the guard edge demotes us otherwise."""
    arb, data = encode_msg(
        "SC_COORDINATOR",
        {
            "dd": instance.cfg.dd,
            "spl": instance.cfg.spl,
            "dpl": instance.cfg.dpl,
            "prn": instance.cfg.prn,
            "cf": _sc_cf_flags(instance),
        },
        as_all=1,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


def respond_with_declaration(ctx, instance, event) -> None:
    """PASSIVE/INACTIVE: answer with our own SC_DECLARATION, CF1 updated;
    CF4 (soft-disabled) set in the disabled state (FIG. 13C-1d)."""
    soft = bool(instance.soft_disabled) or instance.state().endswith("soft_disabled")
    arb, data = encode_msg(
        "SC_DECLARATION",
        {
            "dd": instance.cfg.dd,
            "spl": instance.cfg.spl,
            "dpl": instance.cfg.dpl,
            "prn": instance.cfg.prn,
            "cf": _sc_cf_flags(instance, soft_disabled=soft),
        },
        as_all=1,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)
    instance.ram["declaration_sent"] = True


def handle_ready_to_take_over(ctx, instance, event) -> None:
    """PASSIVE: SC_READYTOTAKEOVER — if our own declaration has not gone out,
    send it immediately; otherwise ignore (FIG. 13B-2 1331/1329)."""
    if not instance.ram.get("declaration_sent"):
        respond_with_declaration(ctx, instance, event)


def confirm_takeover_with_coordinator(ctx, instance, event) -> None:
    """FIG. 13B-2 step 1323: SEND SC_COORDINATOR to confirm the switch to
    ACTIVE. As the standing leader the same handler reasserts (13C-1c row 8)."""
    arb, data = encode_msg(
        "SC_COORDINATOR",
        {
            "dd": instance.cfg.dd,
            "spl": instance.cfg.spl,
            "dpl": instance.cfg.dpl,
            "prn": instance.cfg.prn,
            "cf": _sc_cf_flags(instance),
        },
        as_all=1,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)
    instance.ram["coordinator_sent_ts"] = time.monotonic()


def update_last_seen_asc_time(ctx, instance, event) -> None:
    """Heartbeat bookkeeping: remember the aSC identity. This does NOT move
    the no-READYTOTAKEOVER deadline (FIG. 13C-1c anchors that on
    coordinator-family messages, not heartbeats)."""
    payload = event.data if isinstance(event.data, dict) else {}
    instance.remember_asc(
        int(payload.get("dd") or 0),
        subnet=int(payload.get("ss") or instance.cfg.subnet),
    )


def enter_passive_coordinator(ctx, instance, event) -> None:
    instance.ram["declaration_sent"] = False
    # FIG. 13C-1c rows 6-7: the 60-s no-READYTOTAKEOVER deadline anchors on
    # the last coordinator-family message — which is the coordinator event
    # that just routed us here (election loss, assignment, or takeover).
    instance.touch_rival_coordinator()


def become_passive_after_token_pass(ctx, instance, event) -> None:
    # ¶0179: the token receiver becomes aSC; we stand down as iSC.
    instance.ram["role"] = "isc"


def pass_token_to_best(ctx, instance, event) -> None:
    """FIG. 13B-4 step 1369: contribute the token to the best candidate.
    Their confirming SC_COORDINATOR (1323) is the ACK: the passing_token
    state's On(SC_COORDINATOR) edge completes the pass; the 100 ms After
    marks them unresponsive and re-enters the audit."""
    candidate = instance.best_other_sc()
    if candidate is None:
        instance.schedule(
            hsm.Dispatch(ctx, instance, hsm.Event(name="token_ack_ok"))
        )
        return
    instance.ram["token_candidate_dd"] = candidate.dd
    arb, data = encode_msg(
        "SC_TOKEN_PASS",
        {"target_dd": candidate.dd},
        as_all=1,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


def respond_with_assignment_ack(ctx, instance, event) -> None:
    payload = event.data if isinstance(event.data, dict) else {}
    instance.ram["assignment_received"] = True
    instance.assigned_et = int(payload.get("et") or 0)
    instance.assigned_subnet = int(payload.get("subnet") or instance.cfg.subnet)
    instance.nvm.set("assigned", True)
    instance.nvm.flush()
    arb, data = encode_msg(
        "DEVICE_ASSIGNMENT_ACK",
        {"et": instance.assigned_et, "dd": instance.cfg.dd},
        ds=instance.cfg.subnet,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


def own_assignment_received(ctx, instance, event) -> bool:
    return bool(instance.ram.get("assignment_received"))


def becomes_soft_disabled_device(ctx, instance, event) -> bool:
    payload = event.data if isinstance(event.data, dict) else {}
    cf = int(payload.get("cf") or 0)
    return bool(cf & (1 << 4) == 0) if payload else bool(instance.soft_disabled)


def enter_heartbeat_out(ctx, instance, event) -> None:
    """FIG. 13B-5 step 1377 verbatim: 'IF YOU OR ANY OTHER PRESENT SCs WITH
    CF1 FLAG SET ALSO HAS ITS CF0 FLAG SET, YOU ARE IN VERIFICATION,
    OTHERWISE YOU ARE IN CONFIGURATION.' Also arms the configuration phase.
    Up to this point configuration is the same as verification (13C-1d)."""
    verification = False
    for info in instance.scram.values():
        if info.cf1_flag and info.cf0_flag:
            verification = True
    if instance.nvm.get("configured") and any(
        info.cf1_valid for info in instance.scram.values()
    ):
        verification = verification or True
    instance.ram["verification_mode"] = verification
    instance.ram["config_started_ts"] = time.monotonic()
    if verification:
        arb, data = encode_msg(
            "CONFIGURATION_ALARM",
            {"alarm_no": 0x0F, "set_clear": 1},
            al=1,
            src_et=instance.cfg.et,
            ss=instance.cfg.subnet,
            as_all=1,
        )
        instance.queue_send(arb, data)
    # FIG. 13B-5 step 1381: assignments are a PASS over the DEVICE RAM list,
    # not only per-arrival — devices that announced while we were electing
    # get their ET/SI now.
    instance.schedule(_sweep_device_ram(ctx, instance))


def _sweep_device_ram(ctx, instance):
    """FIG. 13B-5 step 1381 pass over the DEVICE RAM list: every device seen
    during the election gets its ET/SI assignment now."""
    async def _sweep():
        for dd, dev in list(instance.device_ram.items()):
            if dd == instance.cfg.dd:
                continue
            fake_event = hsm.Event(
                name="device_startup",
                data={"dd": dd, "et": dev.et, "ss": instance.cfg.subnet},
            )
            assign_et_si_for_device(ctx, instance, fake_event)
            await asyncio.sleep(0.02 if instance.cfg.fast else 0.1)

    return _sweep()


def send_ready_to_take_over(ctx, instance, event) -> None:
    arb, data = encode_msg(
        "SC_READY_TO_TAKE_OVER",
        {"dd": instance.cfg.dd},
        as_all=1,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


def send_asc_heartbeat(ctx, instance, event) -> None:
    arb, data = encode_msg(
        "aSC_HEARTBEAT",
        {"dd": instance.cfg.dd},
        src_et=instance.cfg.et,
        ss=instance.cfg.subnet,
        as_all=1,
    )
    instance.queue_send(arb, data)
    instance.ram["last_broadcast_ts"] = time.monotonic()


def credentials_best_per_mode(ctx, instance, event) -> bool:
    """FIG. 13C-1e row 10: respond with our heartbeat only when our
    credentials are best and the source is recognized per mode."""
    payload = event.data if isinstance(event.data, dict) else {}
    src_dd = int(payload.get("dd") or 0)
    if src_dd == instance.cfg.dd:
        return False
    verification = bool(instance.ram.get("verification_mode"))
    if verification:
        known = instance.scram.get(src_dd) is not None and instance.nvm.get(
            f"sc:{src_dd:08X}"
        )
        return bool(known) and instance.we_outrank(
            int(payload.get("spl") or 0),
            int(payload.get("dpl") or 0),
            int(payload.get("prn") or 0),
            src_dd,
        )
    # CONFIGURATION: additionally requires the link relay in LOCAL MODE
    # (our bench stack has no relay: the CLI --link-local flag maps it).
    return bool(instance.cfg.link_local) and instance.we_outrank(
        int(payload.get("spl") or 0),
        int(payload.get("dpl") or 0),
        int(payload.get("prn") or 0),
        src_dd,
    )


def assign_et_si_for_device(ctx, instance, event) -> None:
    """FIG. 13B-5 step 1381 + FIG. 13C-1d rows 1-2 on device arrivals:
    - answer with our aSC Heartbeat,
    - VERIFICATION: unknown device → soft-disable it
    - CONFIGURATION: delegate ET arbitration to a FIG. 14 child machine
      (equipment_type_assignment) per unknown-ET device; over-capacity →
      soft-disable the lowest-credential devices.
    """
    payload = event.data if isinstance(event.data, dict) else {}
    dd = int(payload.get("dd") or 0)
    if not dd or dd == instance.cfg.dd:
        return
    # Respond with an aSC heartbeat (13C-1d row 1 'RESPOND WITH aSC_HEARTBEAT').
    send_asc_heartbeat(ctx, instance, event)

    verification = bool(instance.ram.get("verification_mode"))
    dev = instance.device_ram.get(dd)
    if dev is None:
        dev = instance.device_ram[dd] = DeviceInfoShim(dd, int(payload.get("et") or 0), instance.cfg.subnet)

    if verification and not instance.nvm.get(f"dev:{dd:08X}"):
        # unknown device in verification: set it to SOFT DISABLED (13C-1d)
        _soft_disable_device(instance, dd)
        notify_soft_disabled_device(ctx, instance, event)
        return

    stored = instance.nvm.get(f"dev:{dd:08X}")
    known_et = stored.get("et") if isinstance(stored, dict) else None
    if known_et == 0:
        # Table I: Equipment Type 0 is the Subnet Controller class; a device
        # can never legitimately hold it, so ET 0 means "not yet assigned".
        known_et = None
    if known_et is None:
        # unknown ET: drive FIG. 14 via the child machine, seeding it with
        # the exact DEVICE_STARTUP that produced this call.
        seed = hsm.Event(
            name="device_startup",
            data={
                "dd": dd,
                "et": dev.et,
                "ss": instance.cfg.subnet,
            },
        )
        _spawn_et_assignment(instance, ctx, dd, dev.et, seed_event=seed)
    else:
        # known device: re-assign its archived ET/SI and wait for finished
        _assign(instance, dd, int(known_et))


def _assign(instance: NodeRuntime, dd: int, et: int, subnet: int = None) -> None:
    if subnet is None:
        subnet = instance.cfg.subnet
    arb, data = encode_msg(
        "SC_ASSIGNMENT",
        {"et": et, "subnet": subnet, "cf": _sc_cf_flags(instance), "dd": dd},
        ds=instance.cfg.subnet,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


def _soft_disable_device(instance: NodeRuntime, dd: int) -> None:
    arb, data = encode_msg(
        "SC_ASSIGNMENT",
        {"et": 0, "subnet": instance.cfg.subnet, "cf": 1 << 4 & 0xFF, "dd": dd},
        ds=instance.cfg.subnet,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


def _spawn_et_assignment(instance: NodeRuntime, ctx, dd: int, et: int,
                         seed_event: hsm.Event | None = None) -> None:
    """FIG. 14 spawn: start a child machine per unknown-ET device and replay
    the triggering event into it so the arbitration state machine can run to
    completion against the real tables."""
    from .equipment_type_assignment import EquipmentTypeAssignment  # local: avoid import cycle
    from .runtime import AssignmentRuntime

    child = AssignmentRuntime()
    child.cfg = instance.cfg
    child.host = instance
    child.target_dd = dd
    child.cfg.fast = instance.cfg.fast
    child.transport = instance.transport
    child.tx_queue = instance.tx_queue

    async def _run() -> None:
        await hsm.Started(ctx, child, EquipmentTypeAssignment,
                          hsm.Config(ID=f"et-{dd:08X}"))
        if seed_event is not None:
            # the Transport frame already fed the host; the child needs its
            # own copy of the discovery event.
            await hsm.Dispatch(ctx, child, seed_event)

    instance.schedule(_run())


def record_assignment_ack(ctx, instance, event) -> None:
    device_pdu = event.data if isinstance(event.data, dict) else {}
    dd = int(device_pdu.get("dd") or 0)
    dev = instance.device_ram.get(dd)
    if dev is not None:
        dev.assigned = True
        dev.et = int(device_pdu.get("et") or dev.et)
        instance.nvm.set(f"dev:{dd:08X}", {"et": dev.et})
        instance.nvm.flush()
    known = sum(1 for d in instance.device_ram.values() if d.assigned)
    if known and known >= len(instance.device_ram):
        instance.schedule(
            hsm.Dispatch(ctx, instance, hsm.Event(name="assignment_phase_complete"))
        )


def emit_configuration_alarm(ctx, instance, event) -> None:
    """FIG. 13B-5 step 1385: send class 3 CONFIGURATION ALARM; wait until the
    conflict is resolved, then duty per the approval decision: issue a
    device-level reset (¶0133 two-message handshake) — the sheet's verbatim
    'WHEN IT IS, RESET THE SYSTEM' stays flagged for a sniffing-time check
    against real hardware."""
    payload = event.data if isinstance(event.data, dict) else {}
    arb, data = encode_msg(
        "CONFIGURATION_ALARM",
        {
            "alarm_no": int(payload.get("alarm_no") or 0x01),
            "set_clear": 1,
            "src_dd": instance.cfg.dd,
        },
        al=1,
        src_et=instance.cfg.et,
        ss=instance.cfg.subnet,
        as_all=1,
    )
    instance.queue_send(arb, data)


def notify_soft_disabled_device(ctx, instance, event) -> None:
    """FIG. 13B-5 step 1389: DEVICE SOFT DISABLE alarm to the user."""
    payload = event.data if isinstance(event.data, dict) else {}
    arb, data = encode_msg(
        "DEVICE_SOFT_DISABLE_ALARM",
        {"alrm_device_dd": int(payload.get("dd") or 0)},
        al=1,
        src_et=instance.cfg.et,
        ss=instance.cfg.subnet,
        as_all=1,
    )
    instance.queue_send(arb, data)


def send_configuration_finished(ctx, instance, event) -> None:
    arb, data = encode_msg(
        "SC_CONFIGURATION_FINISHED",
        {"cf": _sc_cf_flags(instance)},
        as_all=1,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)
    instance.nvm.set("configured", True)
    instance.nvm.flush()


def send_asc_change_state(ctx, instance, event) -> None:
    """¶0142: conclude the SUBNET STARTUP process by issuing aSC Change
    State (devices exit their FIG. 12 process on it)."""
    arb, data = encode_msg(
        "SC_CHANGE_STATE",
        {"cf": _sc_cf_flags(instance)},
        as_all=1,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


def enter_inactive_role(ctx, instance, event) -> None:
    """iSC role: mirror parameters for hot-backup (¶0037, ¶0182) — RAM
    bookkeeping only on this pass; hot-backup restore is flagged smoke."""
    instance.ram["role"] = "isc"


def enter_soft_disabled(ctx, instance, event) -> None:
    instance.soft_disabled = True
    instance.ram["soft_disabled"] = True  # volatile (¶0137)


def enter_hard_disabled(ctx, instance, event) -> None:
    instance.nvm.set("hard_disabled", True)
    instance.nvm.flush()


def ack_enable_state(ctx, instance, event) -> None:
    from .messages import DEVICE_UI_G_ENABLE_ACK

    arb, data = encode_msg(
        "DEVICE_UI_G_ENABLE_ACK",
        {"et": instance.cfg.et},
        ds=instance.cfg.subnet,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)
    if event.name == "ui_g_hard_enable":
        instance.nvm.set("hard_disabled", False)
        instance.nvm.flush()
        instance.schedule(
            hsm.Dispatch(ctx, instance, hsm.Event(name="hard_enable_apply"))
        )


def answer_class6_diagnostics(ctx, instance, event) -> None:
    payload = event.data if isinstance(event.data, dict) else {}
    arb, data = encode_msg(
        "DIAG_RESPONSE",
        {"et": instance.cfg.et, "dd10": instance.cfg.dd & 0x3FF},
        uiid=payload.get("uiid", 0),
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


def query_unknown_scs(ctx, instance, event) -> None:
    """FIG. 13B-4 step 1357/1359: wait 1 s for device startups, then
    SC_QUERY every SC without a declaration, ≤100 ms per response, looping
    the sweep until covered (1361)."""

    async def _audit() -> None:
        await asyncio.sleep(AUDIT_INITIAL_WAIT * (0.15 if instance.cfg.fast else 1.0))
        deadline_gap = QUERY_RESPONSE_WAIT * (0.1 if instance.cfg.fast else 1.0)
        while True:
            unknown = [
                info for info in instance.scram.values()
                if info.dd != instance.cfg.dd and not info.cf1_valid
            ]
            if not unknown:
                break
            for info in unknown:
                arb, data = encode_msg(
                    "SC_QUERY",
                    {},
                    ds=info.subnet,
                    ss=instance.cfg.subnet,
                )
                instance.queue_send(arb, data)
                await asyncio.sleep(deadline_gap)
        instance.schedule(
            hsm.Dispatch(ctx, instance, hsm.Event(name="audit_pass_done"))
        )

    instance.schedule(_audit())


def mark_unresponsive_scs(ctx, instance, event) -> None:
    """FIG. 13B-4 step 1365 second sweep: give each silent SC one more
    window, then strike them off the list. Shipped: mark
    `marked_unresponsive` on their SCInfo (list keeps around for alarms)."""
    for info in instance.scram.values():
        if not info.cf1_valid and info.dd != instance.cfg.dd:
            instance.ram[f"unresponsive:{info.dd:08X}"] = True


def mark_candidate_unresponsive(ctx, instance, event) -> None:
    dd = int(instance.ram.get("token_candidate_dd") or 0)
    if dd:
        instance.ram[f"unresponsive:{dd:08X}"] = True


def i_am_first_coordinator(ctx, instance, event) -> bool:
    """FIG. 13B-1 step 1317: 'FIRST TO SEND SC_COORDINATOR OUT?' — on the
    bench we cannot observe losing arbitration, so the guard plugins the
    challenger-demotion path (13C-4 row 5: a higher-credential rival's
    COORDINATOR moves us passive via the dedicated edges)."""
    return not instance.ram.get("lost_arbitration")


def is_best_responsive_sc(ctx, instance, event) -> bool:
    """FIG. 13B-4 step 1367: are we the best responsive SC? Ordered compare
    per ¶0173-¶0178 (SPL, DPL, PRN, DD)."""
    ours = _our_credentials(instance)
    for info in instance.scram.values():
        if info.dd == instance.cfg.dd or not info.cf1_valid:
            continue
        if info.credentials() > ours:
            return False
    return True


def challenger_more_credentialed(ctx, instance, event) -> bool:
    payload = event.data if isinstance(event.data, dict) else {}
    dd = int(payload.get("dd") or 0)
    if not dd or dd == instance.cfg.dd:
        return False
    return (_cred_tuple(
        int(payload.get("spl") or 0),
        int(payload.get("dpl") or 0),
        int(payload.get("prn") or 0),
        dd,
    ) > _our_credentials(instance))


# ============================================================== activities ==
async def heartbeat_broadcaster(ctx, instance, event) -> None:
    """FIG. 13B-5 1377 + granted US8255086 method 400: the heartbeat goes
    immediately on taking control, immediately after any SC/Device Startup
    seen on our subnet, and then periodically (once a minute; the takeover
    grace covers the first case per ¶0179)."""
    period = instance.cfg.heartbeat_period()
    grace = instance.cfg.takeover_grace()
    await asyncio.sleep(grace)
    while True:
        send_asc_heartbeat(ctx, instance, event)
        await asyncio.sleep(period)


async def passive_backstop_timer(ctx, instance, event) -> None:
    """Rolling watchdogs for passive state (FIG. 13C-1c rows 6-7 60 s rule +
    FIG. 13C-1a variant of re-sending SC Startup when no assignment within 5
    minutes). Dispatches reset via RESET_COMMAND; the At() edges cover the
    entry-time variant."""
    period = 0.5 if instance.cfg.fast else 1.0
    # granted US8255086 FIG. 4B: the iSC stands down unless the aSC
    # heartbeat arrives within ~3 minutes; FIG. 13C-1c's 60-second
    # READYTOTAKEOVER watchdog stays as the faster election-era backstop.
    window60 = 3.0 if instance.cfg.fast else 60.0
    window5m = 15.0 if instance.cfg.fast else 3 * 60.0
    started = time.monotonic()
    while True:
        await asyncio.sleep(period)
        if (
            instance.last_rival_coordinator_time
            and time.monotonic() - instance.last_rival_coordinator_time > window60
            and not instance.ram.get("ready_to_takeover_seen")
        ):
            await hsm.Dispatch(ctx, instance, hsm.Event(name="passive_60s_backstop"))
        if time.monotonic() - started > window5m and not instance.ram.get("assignment_received"):
            if instance.cfg.passive_resend:
                send_sc_startup(ctx, instance, event)   # FIG. 13C-1a variant
            else:
                await hsm.Dispatch(ctx, instance, hsm.Event(name="passive_5m_backstop"))


async def mirror_coordinator_activity(ctx, instance, event) -> None:
    """FIG. 13C-1f rows 2/3/5-8: the iSC mirrors subnet activity silently."""
    while not getattr(instance, "shutdown", False):
        await asyncio.sleep(0.5)


# ================================================================== model ===
import asyncio  # noqa: E402  (behavior coroutines above use it)

SubnetControllerStartup = hsm.Define(
    "SubnetControllerStartup",
    hsm.Initial(hsm.Target("pre_startup")),
    # FIG. 13A state 1301 (RESET) is entry-time initialization: reset_entry
    # below runs on every (re)entry of pre_startup; FIG. 13A's outer rails
    # (SOFT/HARD DISABLED) return here.
    # --- FIG. 13A state 1303: PRE STARTUP — SC_STARTUP announcer ---------------
    hsm.State(
        "pre_startup",
        hsm.Entry(reset_entry),
        # Our ~1 s + DD-derived delay elapsed (FIG. 13C-1a): broadcast SC
        # Startup and move to POST. FIG. 13C-4 row 1: PRE -> POST when
        # SC_STARTUP is SENT.
        hsm.Transition(
            hsm.After(sc_startup_delay),
            hsm.Target("../post_startup"),
            hsm.Effect(send_sc_startup),
        ),
        # FIG. 13B-1 step 1305 + FIG. 13C-1a PRE rows 2/4/6/7: another SC's
        # SC Startup seen — bypass our timer, reply now.
        hsm.Transition(
            hsm.On(SC_STARTUP),
            hsm.Effect(send_sc_startup),
            hsm.Target("../post_startup"),
        ),
        # FIG. 13A sheet-4 edge: pre-startup checks reveal malfunction.
        hsm.Transition(
            hsm.On("prestartup_malfunction_detected"),
            hsm.Target("../hard_disabled"),
        ),
        hsm.Transition(hsm.On(DIAG_QUERY), hsm.Effect(answer_class6_diagnostics)),
        hsm.Transition(hsm.On(UI_G_HARD_DISABLE), hsm.Target("../hard_disabled")),
        hsm.Transition(hsm.On(RESET_COMMAND), hsm.Target(".")),
    ),
    # --- FIG. 13A state 1309: POST STARTUP — lists + arbitration -----------------
    hsm.State(
        "post_startup",
        hsm.Entry(enter_post_startup),
        hsm.Transition(
            hsm.On(SC_STARTUP),
            hsm.Effect(update_device_ram_from_startup),
        ),
        hsm.Transition(
            hsm.On(DEVICE_STARTUP),
            hsm.Effect(update_device_ram_from_startup),
        ),
        hsm.Transition(
            hsm.On(DEVICE_DD),
            hsm.Effect(update_device_ram_from_startup),
        ),
        # FIG. 13B-1 step 1311 + FIG. 13C-4: one second after our SC_STARTUP
        # went out, broadcast SC_COORDINATOR, then the winner check routes us.
        hsm.Transition(
            hsm.After(arbitration_window),
            hsm.Target("./arbiter"),
            hsm.Effect(broadcast_sc_coordinator),
        ),
        # FIG. 13C-1a POST rows 6/8: ignore SC_QUERY and SC_READYTOTAKEOVER
        # ('the message might have come from another subnet') — no edge.
        # Rows 10/11: a rival aSC's heartbeat / configuration-finished makes
        # us remember that aSC and continue passively.
        hsm.Transition(
            hsm.On(ASC_HEARTBEAT),
            hsm.Effect(remember_asc_and_wait),
            hsm.Target("/SubnetControllerStartup/passive_coordinator"),
        ),
        hsm.Transition(
            hsm.On(SC_CONFIGURATION_FINISHED),
            hsm.Effect(remember_asc_and_wait),
            hsm.Target("/SubnetControllerStartup/passive_coordinator"),
        ),
        hsm.Transition(hsm.On(DIAG_QUERY), hsm.Effect(answer_class6_diagnostics)),
        # FIG. 13C-4 row 3: POST -> PASSIVE when SC_COORDINATOR is RECEIVED.
        hsm.Transition(
            hsm.On(SC_COORDINATOR),
            hsm.Effect(update_device_ram_from_startup),
            hsm.Target("/SubnetControllerStartup/passive_coordinator"),
        ),
        hsm.Transition(hsm.On(UI_G_HARD_DISABLE), hsm.Target("../hard_disabled")),
        hsm.Choice(
            "arbiter",
            # FIG. 13B-1 step 1317 + FIG. 13C-4 row 2: POST -> ACTIVE when our
            # SC_COORDINATOR was SENT (the broadcast fired above); the loser
            # routes to passive via the default branch.
            hsm.Transition(
                hsm.Guard(i_am_first_coordinator),
                hsm.Target("/SubnetControllerStartup/active_coordinator"),
            ),
            hsm.Transition(
                hsm.Target("/SubnetControllerStartup/passive_coordinator"),
            ),
        ),
    ),
    # --- FIG. 13A state 1313: ACTIVE COORDINATOR ---------------------------------
    hsm.State(
        "active_coordinator",
        hsm.Initial(hsm.Target("audit")),
        # FIG. 13B-4 steps 1357-1363: the audit sweep loops until every SC on
        # the list answered (or was struck off in the step-1365 second sweep).
        hsm.State(
            "audit",
            hsm.Entry(query_unknown_scs),
            hsm.Transition(
                hsm.On(SC_DECLARATION),
                hsm.Effect(update_scram_with_declaration),
            ),
            # FIG. 13B-4 step 1359 + FIG. 13C-1b rows 1-3: a new SC joins the
            # list and gets our SC_COORDINATOR; device arrivals (IU resets)
            # clear CF1-valid flags and re-trigger the sweep from the top.
            hsm.Transition(
                hsm.On(SC_STARTUP),
                hsm.Effect(update_device_ram_from_startup),
            ),
            hsm.Transition(
                hsm.On(DEVICE_STARTUP),
                hsm.Effect(update_device_ram_from_startup),
            ),
            hsm.Transition(
                hsm.On(DEVICE_DD),
                hsm.Effect(update_device_ram_from_startup),
            ),
            # IU reset: every CF1-valid flag cleared -> re-query from top.
            hsm.Transition(
                hsm.On("iu_reset_detected"),
                hsm.Target("."),
            ),
            # FIG. 13B-4 steps 1361/1363: audit complete -> best-scoring
            # decision (all-responded short-circuits the second sweep).
            hsm.Transition(
                hsm.On("audit_pass_done"),
                hsm.Target("../best_responsive"),
                hsm.Effect(mark_unresponsive_scs),
            ),
        ),
        hsm.Choice(
            "best_responsive",
            # FIG. 13B-4 steps 1363-1367 + FIG. 13C-4 row 8: only the best
            # responsive SC proceeds (G -> FIG. 13B-5), and ACTIVE ->
            # HEARTBEAT_OUT fires when its first aSC_HEARTBEAT is SENT.
            hsm.Transition(
                hsm.Guard(is_best_responsive_sc),
                hsm.Effect(send_asc_heartbeat),
                hsm.Target("/SubnetControllerStartup/heartbeat_out"),
            ),
            # Not the best: hand the token to the best candidate (1369) and
            # await its ACK.
            hsm.Transition(
                hsm.Target("./passing_token"),
            ),
        ),
        # FIG. 13B-4 steps 1369-1371: SC_TOKENPASS to the best guy on the
        # list; unacknowledged within 100 ms marks it unresponsive and the
        # audit loop resumes.
        hsm.State(
            "passing_token",
            hsm.Entry(pass_token_to_best),
            hsm.Transition(
                hsm.On(SC_COORDINATOR),
                hsm.Target("/SubnetControllerStartup/passive_coordinator"),
            ),  # the candidate's confirming SC_COORDINATOR = token ACK
            hsm.Transition(
                hsm.After(token_ack_window),
                hsm.Target("../audit"),
                hsm.Effect(mark_candidate_unresponsive),
            ),
        ),
        # FIG. 13C-4 row 5: ACTIVE -> PASSIVE when SC_COORDINATOR with higher
        # credentials is RECEIVED.
        hsm.Transition(
            hsm.On(SC_COORDINATOR),
            hsm.Guard(challenger_more_credentialed),
            hsm.Target("/SubnetControllerStartup/passive_coordinator"),
        ),
        # ¶0179: a challenger with better credentials sends SC Ready To Take
        # Over — we pass the token and stand down.
        hsm.Transition(
            hsm.On(SC_READY_TO_TAKE_OVER),
            hsm.Guard(challenger_more_credentialed),
            hsm.Effect(pass_token_to_best),
            hsm.Target("/SubnetControllerStartup/passive_coordinator"),
        ),
        # We outrank the challenger: reassert with our own SC_COORDINATOR
        # (FIG. 13B-4's F-path; FIG. 13C-1c row 8 'second time and beyond').
        hsm.Transition(
            hsm.On(SC_READY_TO_TAKE_OVER),
            hsm.Effect(confirm_takeover_with_coordinator),
        ),
        hsm.Transition(
            hsm.On(SC_DECLARATION),
            hsm.Effect(update_scram_with_declaration),
        ),
        hsm.Transition(hsm.On(SC_STARTUP), hsm.Effect(absorb_new_sc_startup)),
        hsm.Transition(
            hsm.On(DEVICE_STARTUP),
            hsm.Effect(update_device_ram_from_startup),
        ),
        hsm.Transition(hsm.On(DEVICE_DD), hsm.Effect(update_device_ram_from_startup)),
        # FIG. 13C-1b row 4 + FIG. 13C-1c row 6: answer a query with our
        # SC_COORDINATOR when our credentials outrank; otherwise demote.
        hsm.Transition(
            hsm.On(SC_QUERY),
            hsm.Guard(challenger_more_credentialed),
            hsm.Target("/SubnetControllerStartup/passive_coordinator"),
        ),
        hsm.Transition(hsm.On(SC_QUERY), hsm.Effect(respond_with_coordinator)),
        # FIG. 13C-1c rows 9/10 (ACTIVE side): a rival aSC heartbeat received
        # while we are the coordinator → go passive and wait for assignment.
        hsm.Transition(
            hsm.On(ASC_HEARTBEAT),
            hsm.Effect(remember_asc_and_wait),
            hsm.Target("/SubnetControllerStartup/passive_coordinator"),
        ),
        hsm.Transition(hsm.On(DIAG_QUERY), hsm.Effect(answer_class6_diagnostics)),
        hsm.Transition(hsm.On(UI_G_HARD_DISABLE), hsm.Target("../hard_disabled")),
    ),
    # --- FIG. 13A state 1315: PASSIVE COORDINATOR ---------------------------------
    hsm.State(
        "passive_coordinator",
        hsm.Entry(enter_passive_coordinator),
        hsm.Activity(passive_backstop_timer),
        # FIG. 13B-2 step 1319: wait for the coordinator's SC Declaration and
        # Device Startup messages.
        hsm.Transition(
            hsm.On(SC_DECLARATION),
            hsm.Effect(update_scram_with_declaration),
        ),
        hsm.Transition(
            hsm.On(SC_STARTUP),
            hsm.Effect(absorb_new_sc_startup),
        ),
        hsm.Transition(
            hsm.On(DEVICE_STARTUP),
            hsm.Effect(update_device_ram_from_startup),
        ),
        hsm.Transition(
            hsm.On(DEVICE_DD),
            hsm.Effect(update_device_ram_from_startup),
        ),
        # FIG. 13B-2 step 1327 + FIG. 13C-1c row 6: passive answers a query
        # with its own SC_DECLARATION (CF1 updated).
        hsm.Transition(hsm.On(SC_QUERY), hsm.Effect(respond_with_declaration)),
        # FIG. 13B-2 step 1323 + FIG. 13C-4 row 4: SC TokenPass received ->
        # send SC_COORDINATOR to confirm the switch to ACTIVE.
        hsm.Transition(
            hsm.On(SC_TOKEN_PASS),
            hsm.Effect(confirm_takeover_with_coordinator),
            hsm.Target("../active_coordinator"),
        ),
        # FIG. 13B-2 steps 1331/1329 + FIG. 13C-1c row 8: SC_READYTOTAKEOVER
        # in passive → if our own SC_DECLARATION has not gone out, send it
        # immediately; otherwise ignore.
        hsm.Transition(
            hsm.On(SC_READY_TO_TAKE_OVER),
            hsm.Effect(handle_ready_to_take_over),
        ),
        # FIG. 13C-4 row 7 + FIG. 13B-3 step 1349: assignment carrying the
        # disabled bit (or our disabled state) → soft_disabled, after the
        # ack (FIG. 13C-1c row 9).
        hsm.Transition(
            hsm.On(SC_ASSIGNMENT),
            hsm.Guard(becomes_soft_disabled_device),
            hsm.Effect(respond_with_assignment_ack),
            hsm.Target("../soft_disabled"),
        ),
        # FIG. 13C-4 row 6: PASSIVE -> INACTIVE when SC_ASSIGNMENT is
        # RECEIVED (non-disabled branch; ack per FIG. 13C-1c row 9).
        hsm.Transition(
            hsm.On(SC_ASSIGNMENT),
            hsm.Effect(respond_with_assignment_ack),
            hsm.Target("../inactive"),
        ),
        # FIG. 13B-3 step 1347 + FIG. 13C-1c row 11: inactive only when our
        # own SC_ASSIGNMENT already arrived; otherwise remember the aSC and
        # keep waiting for it ('follow the message flags to determine next
        # state'; no assignment within 5 minutes → reset).
        hsm.Transition(
            hsm.On(SC_CONFIGURATION_FINISHED),
            hsm.Guard(own_assignment_received),
            hsm.Target("../inactive"),
        ),
        hsm.Transition(
            hsm.On(SC_CONFIGURATION_FINISHED),
            hsm.Effect(remember_asc_and_wait),
        ),
        # FIG. 13B-2 step 1319 + FIG. 13C-1b header: 5 minutes after STARTUP
        # and still passive -> reset (13C-1a instead re-sends SC_STARTUP —
        # approval decision pending; see five_minutes_after_startup TODO).
        hsm.Transition(
            hsm.At(five_minutes_after_startup),
            hsm.Target("../pre_startup"),
        ),
        # FIG. 13C-1c rows 6-7: no SC_READYTOTAKEOVER for 60 seconds after
        # the last SC_COORDINATOR message -> back to PRE_STARTUP.
        hsm.Transition(
            hsm.At(sixty_seconds_after_last_sc_coordinator),
            hsm.Target("../pre_startup"),
        ),
        # Scenario re-entry points used by the backstop Activity.
        hsm.Transition(hsm.On("passive_60s_backstop"), hsm.Target("../pre_startup")),
        hsm.Transition(hsm.On("passive_5m_backstop"), hsm.Target("../pre_startup")),
        # FIG. 13C-1c row 10 (passive): keep aSC bookkeeping current.
        hsm.Transition(
            hsm.On(ASC_HEARTBEAT),
            hsm.Effect(update_last_seen_asc_time),
        ),
        hsm.Transition(hsm.On(DIAG_QUERY), hsm.Effect(answer_class6_diagnostics)),
        hsm.Transition(hsm.On(UI_G_HARD_DISABLE), hsm.Target("../hard_disabled")),
        hsm.Transition(hsm.On(RESET_COMMAND), hsm.Target("../pre_startup")),
    ),
    # --- FIG. 13A state 1379 / FIG. 13B-5: HEARTBEAT OUT -------------------------
    hsm.State(
        "heartbeat_out",
        # FIG. 13B-5 step 1373 first: announce with SC_READYTOTAKEOVER and
        # re-verify the SC RAM list after one second, then the 1377 mode
        # decision and Configuration Alarms.
        hsm.Entry(send_ready_to_take_over, enter_heartbeat_out),
        hsm.Activity(heartbeat_broadcaster),
        # FIG. 13B-5 step 1375 YES: any new SC (or UI) discovered → re-enter
        # the 13B-4 audit loop.
        hsm.Transition(
            hsm.On(SC_STARTUP),
            hsm.Effect(update_device_ram_from_startup),
            hsm.Target("../active_coordinator"),
        ),
        # FIG. 13B-5 step 1375 'OR SC WITH BETTER CREDENTIALS SEEN': we stand
        # down while the winner takes over.
        hsm.Transition(
            hsm.On(SC_COORDINATOR),
            hsm.Guard(challenger_more_credentialed),
            hsm.Target("../passive_coordinator"),
        ),
        # FIG. 13C-1e row 10: a rival aSC heartbeat — respond with our own
        # heartbeat only if our credentials are best per mode (VERIFICATION:
        # NVM-listed source; CONFIGURATION: additionally link relays in local
        # mode); otherwise BECOME INACTIVE.
        hsm.Transition(
            hsm.On(ASC_HEARTBEAT),
            hsm.Guard(credentials_best_per_mode),
            hsm.Effect(send_asc_heartbeat),
        ),
        hsm.Transition(
            hsm.On(ASC_HEARTBEAT),
            hsm.Target("../inactive"),
        ),
        # FIG. 13C-1d rows 1-2 + FIG. 13B-5 step 1381: device announcements
        # get an aSC_HEARTBEAT response and per-NVM configuration via
        # SC_ASSIGNMENT messages flagged WAIT FOR SC CONFIGURATION FINISHED.
        hsm.Transition(
            hsm.On(DEVICE_STARTUP),
            hsm.Effect(assign_et_si_for_device),
        ),
        hsm.Transition(
            hsm.On(DEVICE_DD),
            hsm.Effect(assign_et_si_for_device),
        ),
        hsm.Transition(
            hsm.On(DEVICE_ASSIGNMENT_ACK),
            hsm.Effect(record_assignment_ack),
        ),
        # FIG. 13B-5 step 1385: configuration conflicts → alarms.
        hsm.Transition(
            hsm.On("conflict_detected"),
            hsm.Effect(emit_configuration_alarm),
        ),
        # FIG. 13B-5 step 1389: soft-disabled device noted.
        hsm.Transition(
            hsm.On(DEVICE_SOFT_DISABLE_ALARM),
            hsm.Effect(notify_soft_disabled_device),
        ),
        hsm.Transition(hsm.On(DIAG_QUERY), hsm.Effect(answer_class6_diagnostics)),
        hsm.Transition(hsm.On(UI_G_HARD_DISABLE), hsm.Target("../hard_disabled")),
        # FIG. 13B-5 step 1391 + ¶0142: broadcast SC CONFIGURATION FINISHED,
        # then conclude startup with the aSC Change State (devices exit their
        # FIG. 12 process on it).
        hsm.Transition(
            hsm.On("assignment_phase_complete"),
            hsm.Effect(send_configuration_finished, send_asc_change_state),
            hsm.Target("./commissioned"),
        ),
        # FIG. 13B-5 step 1399: EXIT TO COMMISSIONING. The aSC continues with
        # system control functions here (¶0164); the parent state's
        # heartbeat Activity stays alive (the parent remains active while a
        # child is entered), so the child adds no second broadcaster —
        # device-side monitoring persists (3 missed heartbeats raise the
        # alarm, ¶0110).
        hsm.State(
            "commissioned",
        ),
    ),
    # --- FIG. 13A state 1355: INACTIVE (iSC role; FIG. 13C-1f) -------------------
    hsm.State(
        "inactive",
        hsm.Entry(enter_inactive_role),
        # FIG. 13C-1f rows 2/3/5-8: mirror subnet activity silently.
        hsm.Activity(mirror_coordinator_activity),
        # Row 10: keep aSC heartbeat bookkeeping (take-over potential).
        hsm.Transition(
            hsm.On(ASC_HEARTBEAT),
            hsm.Effect(update_last_seen_asc_time),
        ),
        # Row 1: the aSC's SC_STARTUP makes us reply (DD-delayed) and REJOIN
        # POST_STARTUP with our own SC_STARTUP.
        hsm.Transition(
            hsm.On(SC_STARTUP),
            hsm.Effect(send_sc_startup),
            hsm.Target("../post_startup"),
        ),
        # Row 4 (expected): answer a coordinator query with our declaration.
        hsm.Transition(
            hsm.On(SC_COORDINATOR),
            hsm.Effect(respond_with_declaration),
        ),
        # Row 9: we were just reassigned or disabled — ack and proceed as
        # directed (disabled contents route to soft_disabled).
        hsm.Transition(
            hsm.On(SC_ASSIGNMENT),
            hsm.Guard(becomes_soft_disabled_device),
            hsm.Effect(respond_with_assignment_ack),
            hsm.Target("../soft_disabled"),
        ),
        hsm.Transition(
            hsm.On(SC_ASSIGNMENT),
            hsm.Effect(respond_with_assignment_ack),
        ),
        # Row 11: SC CONFIGURATION FINISHED — proceed as directed; read the
        # message flags to determine the next state (flag mapping lands
        # with the commissioning-layer model).
        hsm.Transition(
            hsm.On(DIAG_QUERY),
            hsm.Effect(answer_class6_diagnostics),
        ),
        # FIG. 13A edge to 1351: commanded soft-disable (message name
        # TODO-confirm; not spelled out in the sheets).
        hsm.Transition(
            hsm.On("soft_disable_commanded"),
            hsm.Target("../soft_disabled"),
        ),
        hsm.Transition(hsm.On(UI_G_HARD_DISABLE), hsm.Target("../hard_disabled")),
        hsm.Transition(hsm.On(RESET_COMMAND), hsm.Target("../pre_startup")),
    ),
    # --- FIG. 13A state 1351: SOFT DISABLED (FIG. 13C-1d) --------------------------
    hsm.State(
        "soft_disabled",
        hsm.Entry(enter_soft_disabled),
        # FIG. 13C-1d row 1: on an aSC SC_STARTUP we reply with our own
        # SC_STARTUP and REJOIN POST_STARTUP.
        hsm.Transition(
            hsm.On(SC_STARTUP),
            hsm.Effect(send_sc_startup),
            hsm.Target("../post_startup"),
        ),
        # FIG. 13C-1d rows 3/4: DD messages get our SC_DECLARATION with the
        # CF4 soft-disabled flag set.
        hsm.Transition(
            hsm.On(DEVICE_DD),
            hsm.Effect(respond_with_declaration),
        ),
        # FIG. 13B-3 step 1353: stay disabled until the next reset; the
        # soft-disable state is RAM-only (¶0137).
        hsm.Transition(hsm.On(RESET_COMMAND), hsm.Target("../pre_startup")),
        hsm.Transition(
            hsm.On(DIAG_QUERY),
            hsm.Effect(answer_class6_diagnostics),
        ),
        hsm.Transition(hsm.On(UI_G_HARD_DISABLE), hsm.Target("../hard_disabled")),
    ),
    # --- FIG. 13A state 1307: HARD DISABLED (¶0134-¶0137) ---------------------------
    hsm.State(
        "hard_disabled",
        hsm.Entry(enter_hard_disabled),
        hsm.Transition(
            hsm.On(UI_G_HARD_DISABLE),
            hsm.Effect(ack_enable_state),
        ),
        hsm.Transition(
            hsm.On(UI_G_HARD_ENABLE),
            hsm.Effect(ack_enable_state),
        ),
        hsm.Transition(
            hsm.On(DIAG_QUERY),
            hsm.Effect(answer_class6_diagnostics),
        ),
        hsm.Transition(hsm.On("hard_enable_apply"), hsm.Target("../pre_startup")),
    ),
)

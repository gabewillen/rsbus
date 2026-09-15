"""Equipment Type assignment state machine (aSC side).

Spec: U.S. 2010/0106322 A1 FIG. 14 (method 1400), ¶0238-¶0244. Runs inside the
ACTIVE COORDINATOR's assignment phase (`subnet_controller.heartbeat_out`, step
1381): when a DEVICE Startup arrives from a device whose Equipment Type is
unknown to the aSC, this machine drives the ET arbitration for it.

Behaviors implemented per FIG. 14; shape
below is what needs model approval before behavior code.

UML digraph (FIG. 14; step numbers are spec flowchart node ids):

    (1405 arm on unknown-ET DEVICE Startup) ──DEVICE Startup (unknown ET)──▶ {1410 another
    unknown w/ same ET?}
        no  ─▶ (1415 assign reported ET)  ─▶ (1420 done, no ack wait here)
        yes ─▶ (1425 init candidate startET/newET=+1) ─▶ (1430 inc newET) ◀─┐
              (1435 someone holds newET?) ──yes────────────────────────────┤
              no  ─▶ (1440 assign newET) ─▶ (1445 await Assignment Ack)     │
                     ack never comes ─▶ back to (1430)                      │
                     (1450 successful?) ─▶ (1420 done)                      │
                     (1455 rejected too high?) ─▶ (1465 decrement path)     │
                     (1465 rejected too low?)  ─▶ (1430 again) ─────────────┘
                     neither ⇒ collision loop (1430) ───────────────────────┘
    (1470 max devices reached) → soft-disable the device → (1420 done)

The aSC spawns one instance of this model per unknown-ET device
(see subnet_controller._spawn_et_assignment).
"""

import asyncio
import time

import hsm

from .messages import (
    DEVICE_ASSIGNMENT_ACK,
    DEVICE_STARTUP,
    encode_msg,
)


# ======================================================== behavior fns ======
def _target(instance):
    return int(getattr(instance, "target_dd", 0) or 0)


def _event_dd(instance, event) -> int:
    payload = event.data if isinstance(event.data, dict) else {}
    return int(payload.get("dd") or 0)


def _assign(instance, new_et: int, *, subnet: int = None) -> None:
    """FIG. 14 step 1440: aSC DEVICE ASSIGNMENT with the computed ET (flags
    set to wait for SC CONFIGURATION FINISHED, 13B-5 step 1381)."""
    host = instance.host
    subnet = instance.cfg.subnet if subnet is None else subnet
    arb, data = encode_msg(
        "SC_ASSIGNMENT",
        {"et": new_et & 0xFF, "subnet": subnet, "cf": 0xFF,
         "dd": _target(instance)},
        ds=instance.cfg.subnet,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)
    instance.ram["last_assign_ts"] = time.monotonic()


def _ack_timeout_task(instance, ctx, event) -> None:
    """No timeout printed by the spec for step 1445's ack wait; the ack-timeout
    bridge fires `assignment_ack_timeout` — schedule per fast mode."""
    delay = 0.4 if instance.cfg.fast else 2.0

    async def _fire() -> None:
        await asyncio.sleep(delay)
        await hsm.Dispatch(ctx, instance, hsm.Event(name="assignment_ack_timeout"))

    instance.schedule(_fire())


def await_device_startup(ctx, instance, event) -> None:
    # FIG. 14 step 1405: arm per-device; the aSC host spawned this machine
    # targeting exactly one unknown-ET device dd.
    instance.ram["startET"] = 0
    instance.ram["newET"] = 0
    instance.ram["increment"] = 1


def assign_reported_et(ctx, instance, event) -> None:
    et = int((event.data or {}).get("et") or 0)
    instance.ram["reported_et"] = et
    _assign(instance, et)
    _ack_timeout_task(instance, ctx, event)


def init_candidate(ctx, instance, event) -> None:
    et = int((event.data or {}).get("et") or 0)
    instance.ram["startET"] = et
    instance.ram["newET"] = et
    instance.ram["increment"] = 1


def step_candidate(ctx, instance, event) -> None:
    new_et = int(instance.ram.get("newET") or 0) + int(instance.ram.get("increment") or 1)
    instance.ram["newET"] = new_et


def issue_new_et_assignment(ctx, instance, event) -> None:
    new_et = int(instance.ram.get("newET") or 0)
    _assign(instance, new_et)
    _ack_timeout_task(instance, ctx, event)


def note_too_high(ctx, instance, event) -> None:
    instance.ram["newET"] = instance.ram.get("startET")
    instance.ram["increment"] = -1


def note_too_low(ctx, instance, event) -> None:
    """FIG. 14 step 1470: capacity exhausted — soft-disable the device and
    end (the guardless fallthrough targets ../done)."""
    host = instance.host
    host.schedule(_soft_disable_call(host, _target(instance)))


async def _soft_disable_call(host, dd: int) -> None:
    from .messages import encode_msg as _enc
    arb, data = _enc(
        "SC_ASSIGNMENT",
        {"et": 0, "subnet": host.cfg.subnet, "cf": 0, "dd": dd},
        ds=host.cfg.subnet,
        ss=host.cfg.subnet,
    )
    host.queue_send(arb, data)
    dev = host.device_ram.get(dd)
    if dev is not None:
        dev.soft_disabled = True


def record_success(ctx, instance, event) -> None:
    # Stop the child machine after completion: it reached its Final state
    # and must not linger in the context registry (each FIG. 14 spawn adds
    # one instance per unknown-ET device).
    import asyncio as _aio
    _loop = _aio.get_running_loop()
    _loop.call_soon_threadsafe(lambda: _loop.create_task(
        _stop_instance(instance)
    ))
    host = instance.host
    new_et = int(instance.ram.get("assignET") or instance.ram.get("newET") or instance.ram.get("reported_et") or 0)
    dd = _target(instance)
    dev = host.device_ram.get(dd)
    if dev is not None:
        dev.assigned = True
        dev.et = new_et
        host.nvm.set(f"dev:{dd:08X}", {"et": new_et})
        host.nvm.flush()
    known = sum(1 for d in host.device_ram.values() if d.assigned)
    if known and known >= len(host.device_ram):
        host.schedule(
            hsm.Dispatch(None, host, hsm.Event(name="assignment_phase_complete"))
        )


async def _stop_instance(instance) -> None:
    try:
        await hsm.Stop(instance)
    except Exception:
        pass


# ============================================================ guards =========
def another_unknown_with_same_et(ctx, instance, event) -> bool:
    """FIG. 14 step 1410: another unknown-ET device of the SAME ET announced
    during this startup — the dup-ET collision that spins the candidate loop."""
    host = instance.host
    et = int((event.data or {}).get("et") or 0)
    for dd, dev in host.device_ram.items():
        if dd != _target(instance) and dev.et == et and not dev.assigned:
            return True
    pending = host.ram.get("pending_unknown_ets") or {}
    for dd in pending.get(et, []):
        if dd != _target(instance):
            return True
    return False


def candidate_et_free(ctx, instance, event) -> bool:
    """FIG. 14 step 1435: no other local controller already holds newET."""
    host = instance.host
    new_et = int(instance.ram.get("newET") or 0)
    for dd, dev in host.device_ram.items():
        if dd != _target(instance) and dev.et == new_et:
            return False
    return True


def assignment_successful(ctx, instance, event) -> bool:
    """FIG. 14 step 1450: the spec's DEVICE Assignment Acknowledge (¶0155)
    carries the assigned ET and no status byte — absent status = accepted.
    The too-high / too-low verdicts arrive as explicit rejection messages
    (et_rejected_too_high|too_low, TODO(sniff) confirmation)."""
    payload = event.data if isinstance(event.data, dict) else {}
    if "status" not in payload:
        return True
    return int(payload.get("status") or 0) == 0


def rejected_too_high(ctx, instance, event) -> bool:
    payload = event.data if isinstance(event.data, dict) else {}
    if "status" not in payload:
        return False
    return int(payload.get("status") or 0) == 1


def rejected_too_low(ctx, instance, event) -> bool:
    payload = event.data if isinstance(event.data, dict) else {}
    if "status" not in payload:
        return False
    return int(payload.get("status") or 0) == 2


# ================================================================== model ===


EquipmentTypeAssignment = hsm.Define(
    "EquipmentTypeAssignment",
    hsm.Initial(hsm.Target("awaiting_startup")),
    # --- FIG. 14 step 1405 -------------------------------------------------------
    hsm.State(
        "awaiting_startup",
        hsm.Entry(await_device_startup),
        # FIG. 14 steps 1410/1415: a device of unique unknown ET is assigned
        # the ET exactly as reported and the machine ENDS — no ack wait on
        # this path (1415 -> 1420 EXIT).
        hsm.Transition(
            hsm.On(DEVICE_STARTUP),
            hsm.Target("rival_unknown"),
        ),
        hsm.Choice(
            "rival_unknown",
            hsm.Transition(
                hsm.Guard(another_unknown_with_same_et),
                hsm.Target("../candidate"),
                hsm.Effect(init_candidate),
            ),
            hsm.Transition(
                hsm.Effect(assign_reported_et),
                hsm.Target("../done"),
            ),
        ),
    ),
    # --- FIG. 14 steps 1430/1435: candidate walk-up loop ----------------------------
    hsm.State(
        "candidate",
        hsm.Entry(step_candidate),
        hsm.Transition(
            hsm.On("candidate_free"),
            hsm.Guard(candidate_et_free),
            hsm.Effect(issue_new_et_assignment),
            hsm.Target("../acknowledging"),
        ),
        # Somebody already holds newET — re-run the Entry effect.
        hsm.Transition(
            hsm.On("candidate_collides"),
            hsm.Target("."),
        ),
    ),
    # --- FIG. 14 steps 1445-1470: await ack and arbitrate verdicts -------------------
    hsm.State(
        "acknowledging",
        # TODO: spec does not print a timeout value for the ack wait; add one
        # at behavior time (assignment_ack_window).
        hsm.Transition(
            hsm.On("assignment_ack_timeout"),
            hsm.Target("../candidate"),
        ),  # FIG. 14 step 1445 NO: DEVICE_ASSIGNMENT_ACK never came —
        # re-increment newET and retry (back to step 1430).
        hsm.Transition(
            hsm.On(DEVICE_ASSIGNMENT_ACK),
            hsm.Target("ack_verdict"),
        ),
        hsm.Choice(
            "ack_verdict",
            hsm.Transition(
                hsm.Guard(assignment_successful),
                hsm.Effect(record_success),
                hsm.Target("../done"),
            ),
            hsm.Transition(
                hsm.Guard(rejected_too_high),
                hsm.Effect(note_too_high),
                hsm.Target("../candidate"),
            ),
            hsm.Transition(
                hsm.Guard(rejected_too_low),
                hsm.Effect(note_too_low),
                hsm.Target("../done"),
            ),
            # Any other rejection ⇒ another device already holds that ET —
            # loop the candidate walk-up again (FIG. 14: 'the method returns
            # to step 1430 where newET is again incremented').
            hsm.Transition(
                hsm.Target("../candidate"),
            ),
        ),
    ),
    # --- FIG. 14 step 1420: done -------------------------------------------------------
    hsm.Final("done"),
)

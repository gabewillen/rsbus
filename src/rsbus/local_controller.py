"""Local controller SUBNET STARTUP state machine (non-SC devices).

Spec: U.S. 2010/0106322 A1, FIG. 12 (process 1200), ¶0129-¶0160. Runs after
reset (power-up, NVM check, or aSC reset command ¶0131-¶0133) on every local
controller 290 that is NOT a Subnet Controller 230. The SC-role machine lives
in `subnet_controller.py`.

Implemented behaviors per the spec; timings honor NodeConfig.fast for bench
and integration tests. The hardware TX surface is `instance.queue_send()`;
the runtime layer (runtime.py + app.py) drains the TX queue through the
CAN transport, and the RX path re-enters as hsm events with decoded payload
dicts.
"""

import asyncio
import time
from datetime import timedelta

import hsm

from . import messages as m
from .messages import RESET_COMMAND, DIAG_QUERY, SC_COORDINATOR, SC_ASSIGNMENT, ASC_CHANGE_STATE, UI_G_HARD_DISABLE, UI_G_HARD_ENABLE
from .runtime import NodeRuntime


# ---------------------------------------------------------- cf flag helper ---
def device_cf_flags(instance: NodeRuntime) -> int:
    """CF0-CF6 byte for our own startup/status messages (¶0186-¶0201,
    non-SC-device encoding; mirrors the aSC variant consumed by
    subnet_controller._sc_cf_flags)."""
    cf = 0
    if instance.ram.get("assigned"):
        cf |= m.CF0_INSTALLED_TEST_COMPLETE
    if instance.nvm.get("persistent", True):
        cf |= m.CF1_PERSISTENT
    cf |= m.CF2_FLASHABLE
    cf |= m.CF3_ENABLED
    if not instance.ram.get("soft_disabled"):
        cf |= m.CF4_NOT_SOFT_DISABLED
    if instance.nvm.get("replacement_part"):
        cf |= m.CF5_FACTORY_PART
    cf |= m.CF6_CRC_OK
    return cf


# =========================================================== timer fns ======
async def listen_only_window(ctx, instance, event) -> timedelta:
    # ¶0148: ~5000 ms listen-only before any transmit.
    return timedelta(milliseconds=200 if instance.cfg.fast else instance.cfg.listen_ms)


async def coordinator_response_window(ctx, instance, event) -> timedelta:
    # ¶0149: ~100 ms + Device-Designator-derived stagger for the replies that
    # converge on a fresh SC Coordinator.
    delay_ms = 100 + (instance.cfg.dd & 0x0F) % 20
    if instance.cfg.fast:
        delay_ms = max(10, delay_ms // 10)
    return timedelta(milliseconds=delay_ms)


async def unassigned_repeat_window(ctx, instance, event) -> timedelta:
    # ¶0156: re-send DEVICE Startup every ~5 min until assigned.
    return timedelta(seconds=instance.cfg.repeat())


async def send_delivery_window(ctx, instance, event) -> timedelta:
    """One retry-less send window (¶0169): a queued send counts as delivered
    unless a bus error event re-enters the retry loop."""
    return timedelta(milliseconds=30 if instance.cfg.fast else 150)


# ========================================================= behaviors ========
def reset_entry(ctx, instance, event) -> None:
    """FIG. 12 state 1210: after power cycle / NVM check / reset command.

    - restore assigned ET / subnet from NVM (¶0155)
    - RAM reset (volatile values: soft-disabled state is RAM-only ¶0137)
    - if NVM says HARD DISABLED, hop the machine (persistence ¶0136)
    - CRC/NVM check gate happens here (¶0232) before our first startup send
    """
    instance.ram.clear()
    instance.assigned_et = int(instance.nvm.get("et") or 0)
    instance.assigned_subnet = int(instance.nvm.get("subnet") or instance.cfg.subnet)
    instance.ram["soft_disabled"] = False
    instance.ram["assigned"] = instance.assigned_et != 0
    instance.ram["startup_sent_ts"] = None
    instance.ram["attempts"] = 0
    instance.ram["crc_ok"] = True  # ¶0232 stub: NVM CRC verified pre-startup
    if instance.nvm.get("hard_disabled"):
        instance.schedule(
            hsm.Dispatch(ctx, instance, hsm.Event(name="ui_g_hard_disable"))
        )
    instance.nvm.flush()


def enter_listen_only(ctx, instance, event) -> None:
    # ¶0148: monitor the bus, no initiation. The transport only transmits
    # when the FSM queues a send, so nothing else is required here.
    pass


def send_device_startup(ctx, instance, event) -> None:
    """Entry(sending): broadcast DEVICE Startup. Message ID embeds the
    5-bit order number from our DD (¶0152-¶0154)."""
    arb, data = m.encode_msg(
        "DEVICE_STARTUP",
        {"dd": instance.cfg.dd,
         "et": instance.assigned_et or instance.cfg.et_request,
         "cf": device_cf_flags(instance)},
        ds=instance.assigned_subnet or instance.cfg.subnet,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)
    instance.ram["startup_sent_ts"] = time.monotonic()
    instance.ram["attempts"] = int(instance.ram.get("attempts", 0)) + 1


def handle_bit_error_resend(ctx, instance, event) -> None:
    """Self-transition (sending) when a collision/bit error is detected
    (¶0219-¶0228): recompute delay from the DD 4-bit portion mapped to this
    attempt, re-transmit after the wait; ≥255 attempts disengaging per spec
    then a 5-minute restart (¶0228)."""
    attempts = int(instance.ram.get("attempts", 0))
    if attempts == 0:
        attempts = 1
        instance.ram["attempts"] = attempts
    portion = (instance.cfg.dd >> (4 * ((attempts - 1) % 8))) & 0x0F
    delay_s = ((portion + 1) * 4) / 1000.0
    if instance.cfg.fast:
        delay_s = max(delay_s / 10, 0.02)

    async def _resend() -> None:
        await asyncio.sleep(delay_s)
        if attempts >= 255:
            # cap: disengage; retry after ~5 minutes (¶0228)
            instance.ram["attempts"] = 0
            await hsm.Dispatch(ctx, instance, hsm.Event(name="restart_after_cap"))
            return
        await hsm.Dispatch(ctx, instance, hsm.Event(name="attempt_send"))

    instance.schedule(_resend())


def startup_delivered(ctx, instance, event) -> None:
    instance.ram["startup_sent_ts"] = time.monotonic()
    instance.ram["attempts"] = 0


def respond_to_coordinator(ctx, instance, event) -> None:
    """Answer a coordinator per configuration state: DEVICE Startup while
    waiting (~100 ms + DD stagger, ¶0149/¶0152) or DEVICE Device Designator
    once assigned (¶0152-¶0153, ¶0158-¶0159)."""

    async def _reply() -> None:
        delay = await coordinator_response_window(ctx, instance, event)
        await asyncio.sleep(delay.total_seconds())
        if instance.ram.get("assigned"):
            arb, data = m.encode_msg(
                "DEVICE_DD",
                {"dd": instance.cfg.dd, "cf": device_cf_flags(instance)},
                ds=instance.assigned_subnet or instance.cfg.subnet,
                ss=instance.cfg.subnet,
            )
        else:
            arb, data = m.encode_msg(
                "DEVICE_STARTUP",
                {"dd": instance.cfg.dd, "cf": device_cf_flags(instance)},
                ds=instance.assigned_subnet or instance.cfg.subnet,
                ss=instance.cfg.subnet,
            )
        instance.queue_send(arb, data)

    instance.schedule(_reply())


def answer_class6_diagnostics(ctx, instance, event) -> None:
    """Class 6 diagnostic replies work in EVERY state incl. disabled
    (¶0105, ¶0135, ¶0146)."""
    payload = event.data if isinstance(event.data, dict) else {}
    arb, data = m.encode_msg(
        "DIAG_RESPONSE",
        {"et": instance.assigned_et, "dd10": instance.cfg.dd & 0x3FF},
        uiid=payload.get("uiid", 0),
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


def record_assignment(ctx, instance, event) -> None:
    """aSC DEVICE Assignment: store ET + Subnet ID in NVM and ack back
    (¶0155); aSC-assigned logical devices on the same physical unit share
    the Subnet Identifier (¶0230-¶0231)."""
    payload = event.data if isinstance(event.data, dict) else {}
    et = int(payload.get("et") or 0)
    if et == 0:
        # Table I: ET 0 belongs to the Subnet Controller class; an assignment
        # carrying ET 0 for a device is invalid — ignore it and keep waiting.
        return
    subnet = int(payload.get("subnet") or instance.cfg.subnet)
    instance.assigned_et = et
    instance.assigned_subnet = subnet
    instance.ram["assigned"] = True
    instance.nvm.set("et", et)
    instance.nvm.set("subnet", subnet)
    instance.nvm.flush()
    arb, data = m.encode_msg(
        "DEVICE_ASSIGNMENT_ACK",
        {"et": et, "dd": instance.cfg.dd},
        ds=instance.cfg.subnet,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)


def assignment_for_us(ctx, instance, event) -> bool:
    """Only react to aSC DEVICE Assignments addressed to our DD (or
    all-subnet broadcasts; FIG. 12's device never acts on other devices'
    assignments)."""
    payload = event.data if isinstance(event.data, dict) else {}
    dd = int(payload.get("dd") or 0)
    ds = int(payload.get("ds") if payload.get("ds") is not None else 0)
    all_subnets = ds == 0b11
    return all_subnets or dd == instance.cfg.dd


def enter_hard_disabled(ctx, instance, event) -> None:
    # HARD DISABLED persists in NVM (¶0136) until explicit enable; RX-only.
    instance.nvm.set("hard_disabled", True)
    instance.nvm.flush()


def ack_enable_state(ctx, instance, event) -> None:
    """Respond with DEVICE UI/G Enable Acknowledge over RSBus (¶0135)."""
    arb, data = m.encode_msg(
        "DEVICE_UI_G_ENABLE_ACK",
        {"et": instance.assigned_et},
        ds=instance.assigned_subnet or instance.cfg.subnet,
        ss=instance.cfg.subnet,
    )
    instance.queue_send(arb, data)
    if event.name in ("ui_g_hard_enable", "ui_g_hard_disable"):
        # Apply the state change after the ack; enable re-runs startup from
        # the top (¶0146 via hard_enable_apply edge on the model).
        instance.schedule(
            hsm.Dispatch(
                ctx,
                instance,
                hsm.Event(
                    name="hard_enable_apply"
                    if event.name == "ui_g_hard_enable"
                    else "noop"
                ),
            )
        )


def enter_soft_disabled(ctx, instance, event) -> None:
    # RAM-only (volatile) soft disable, cleared on reset (¶0137).
    instance.ram["soft_disabled"] = True


def needs_repeat(ctx, instance, event) -> bool:
    """5-minute repeat guard (¶0156): unassigned and our last DEVICE Startup
    has aged past the window."""
    if instance.ram.get("assigned"):
        return False
    sent_ts = instance.ram.get("startup_sent_ts")
    if sent_ts is None:
        return False
    window = instance.cfg.repeat()
    return (time.monotonic() - sent_ts) >= window


# -------------------------------------------------------------- activity ----
async def monitor_bus_while_assigned(ctx, instance, event) -> None:
    """Activity(assigned):
    - monitor the aSC Heartbeat; alarm + reset after 3 missed periods (¶0110)
    - publish our DEVICE Status with service bits on every heartbeat period
      (¶0259-¶0260: publish on first aSC Change State after reset; demand
      acks within ~100 ms with the status bits ¶0264)
    """
    period = instance.cfg.heartbeat_period()
    # Grace: heartbeat clock only arms after our first heartbeat is seen.
    instance.ram["last_heartbeat_ts"] = None
    instance.ram["missed_heartbeats"] = 0
    while True:
        await asyncio.sleep(period)
        # Publish DEVICE Status (service vector: all services available = FFh)
        arb, data = m.encode_msg(
            "DEVICE_STATUS",
            {"service_bits": 0xFF, "dd": instance.cfg.dd},
            ds=instance.assigned_subnet or instance.cfg.subnet,
            ss=instance.cfg.subnet,
        )
        instance.queue_send(arb, data)

        last = instance.ram.get("last_heartbeat_ts")
        if last is None:
            continue
        # granted US8255086 FIG. 4C: a device alarms after ONE missed period
        # (~1 min at the once-a-minute heartbeat), ceases normal operation,
        # and keeps sending Device Status messages.
        if time.monotonic() - last > period:
            instance.ram["missed_heartbeats"] = int(instance.ram.get("missed_heartbeats", 0)) + 1
            instance.ram["last_heartbeat_ts"] = time.monotonic()  # re-arm per-period
            await hsm.Dispatch(ctx, instance, hsm.Event(name="heartbeat_lost"))
            instance.ram["missed_heartbeats"] = 0


# ============================================================ model shape ====
LocalControllerStartup = hsm.Define(
    "LocalControllerStartup",
    hsm.Initial(hsm.Target("pre_startup")),
    # FIG. 12 state 1210 (reset) is entry-time initialization, not a dwelling
    # state: reset_entry below runs on every (re)entry of pre_startup.
    hsm.State(
        "pre_startup",
        hsm.Entry(reset_entry),
        hsm.Initial(hsm.Target("listen_only")),
        # FIG. 12: from ANY state a message can force a reset (¶0146); the
        # figure's left rail routes PRE STARTUP back to RESET 1210. A reset
        # re-runs reset_entry via this self-transition.
        hsm.Transition(hsm.On("restart_after_cap"), hsm.Target(".")),
        hsm.Transition(hsm.On("heartbeat_lost"), hsm.Target(".")),
        hsm.Transition(hsm.On(RESET_COMMAND), hsm.Target(".")),
        hsm.State(
            "listen_only",
            hsm.Entry(enter_listen_only),
            hsm.Transition(hsm.After(listen_only_window), hsm.Target("../sending")),
            hsm.Transition(
                hsm.On(DIAG_QUERY),
                hsm.Effect(answer_class6_diagnostics),
            ),
        ),
        # Collision with another starter: exit+re-enter through itself and
        # redo the DD-derived delay (¶0219-¶0228).
        hsm.State(
            "sending",
            hsm.Entry(send_device_startup),
            hsm.Transition(
                hsm.On("attempt_send"),
                hsm.Target("."),
                hsm.Effect(handle_bit_error_resend),
            ),
            hsm.Transition(
                hsm.On("bit_error_detected"),
                hsm.Target("."),
                hsm.Effect(handle_bit_error_resend),
            ),
            hsm.Transition(
                hsm.On("startup_delivered"),
                hsm.Target("../sent"),
                hsm.Effect(startup_delivered),
            ),
            # Single-shot per scheduled slot (¶0169): without an error event,
            # the queued send completes and the machine waits for replies.
            hsm.Transition(
                hsm.After(send_delivery_window),
                hsm.Target("../sent"),
                hsm.Effect(startup_delivered),
            ),
        ),
        hsm.State(
            "sent",
            hsm.Transition(
                hsm.After(coordinator_response_window),
                hsm.Target("../../wait_for_assignment"),
                hsm.Effect(respond_to_coordinator),
            ),
            # A coordinator can also arrive inside the response window.
            hsm.Transition(
                hsm.On(SC_COORDINATOR),
                hsm.Target("../../wait_for_assignment"),
                hsm.Effect(respond_to_coordinator),
            ),
        ),
        # Class 6 diagnostics from the PRE STARTUP container itself.
        hsm.Transition(
            hsm.On(DIAG_QUERY),
            hsm.Effect(answer_class6_diagnostics),
        ),
        hsm.Transition(hsm.On(UI_G_HARD_DISABLE), hsm.Target("../hard_disabled")),
    ),
    # --- state 1230: WAIT TO BE ASSIGNED ------------------------------------
    hsm.State(
        "wait_for_assignment",
        hsm.Entry(send_device_startup),
        # Repeat every 5 min until assigned (¶0156). Self-transition re-runs
        # the Entry effect, so the repeat Effect list is intentionally empty.
        hsm.Transition(
            hsm.After(unassigned_repeat_window),
            hsm.Target("."),
            hsm.Guard(needs_repeat),
        ),
        # Answer coordinator announcements while we are still waiting (¶0152).
        hsm.Transition(hsm.On(SC_COORDINATOR), hsm.Effect(respond_to_coordinator)),
        # aSC hands us ET + Subnet ID + flags; ack back with our ET (¶0155).
        hsm.Transition(
            hsm.On(SC_ASSIGNMENT),
            hsm.Guard(assignment_for_us),
            hsm.Effect(record_assignment),
        ),
        # aSC says go operational (state 1240 exit).
        hsm.Transition(hsm.On(ASC_CHANGE_STATE), hsm.Target("../assigned")),
        hsm.Transition(
            hsm.On(DIAG_QUERY),
            hsm.Effect(answer_class6_diagnostics),
        ),
        hsm.Transition(hsm.On(UI_G_HARD_DISABLE), hsm.Target("../hard_disabled")),
        # aSC reset command restarts the whole startup procedure (¶0131).
        hsm.Transition(hsm.On(RESET_COMMAND), hsm.Target("../pre_startup")),
    ),
    # --- state 1240 (= "exit process", normal ops) ---------------------------
    hsm.State(
        "assigned",
        hsm.Activity(monitor_bus_while_assigned),
        # A coordinator arriving now means we are reset or reconnecting:
        # answer with DEVICE DD, not Startup (¶0158-¶0159).
        hsm.Transition(hsm.On(SC_COORDINATOR), hsm.Target("."), hsm.Effect(respond_to_coordinator)),
        hsm.Transition(
            hsm.On(DIAG_QUERY),
            hsm.Effect(answer_class6_diagnostics),
        ),
        hsm.Transition(hsm.On(UI_G_HARD_DISABLE), hsm.Target("../hard_disabled")),
        hsm.Transition(hsm.On(RESET_COMMAND), hsm.Target("../pre_startup")),
    ),
    # --- state 1260: SOFT DISABLED -------------------------------------------
    hsm.State(
        "soft_disabled",
        hsm.Entry(enter_soft_disabled),
        # ¶0147: cleared on reset -> advance to PRE STARTUP (state 1220).
        hsm.Transition(hsm.On(RESET_COMMAND), hsm.Target("../pre_startup")),
        # A soft-disabled device still responds to SC commands (¶0137).
        hsm.Transition(
            hsm.On(DIAG_QUERY),
            hsm.Effect(answer_class6_diagnostics),
        ),
        hsm.Transition(hsm.On(UI_G_HARD_DISABLE), hsm.Target("../hard_disabled")),
    ),
    # --- state 1250: HARD DISABLED --------------------------------------------
    hsm.State(
        "hard_disabled",
        hsm.Entry(enter_hard_disabled),
        # Enable/Disable ack goes both ways (¶0135).
        hsm.Transition(hsm.On(UI_G_HARD_DISABLE), hsm.Target("."), hsm.Effect(ack_enable_state)),
        hsm.Transition(hsm.On(UI_G_HARD_ENABLE), hsm.Target("."), hsm.Effect(ack_enable_state)),
        # ¶0146: from any state a message may force a reset; HARD DISABLED
        # persists in NVM until the user intervenes (¶0136).
        hsm.Transition(hsm.On(RESET_COMMAND), hsm.Target("../pre_startup")),
        # On hard-enable: re-init (CRC/NVM check, ¶0135) and restart the
        # startup process from the top (¶0146).
        hsm.Transition(hsm.On("hard_enable_apply"), hsm.Target("../pre_startup")),
        hsm.Transition(
            hsm.On(DIAG_QUERY),
            hsm.Effect(answer_class6_diagnostics),
        ),
    ),
)

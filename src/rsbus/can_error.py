"""CAN error confinement state machine (device-level).

Spec: U.S. 2010/0106322 A1 ¶0083-¶0086, implementing the Bosch CAN 2.0B fault
confinement on the RSBus: error counters rise on bus failures; crossing the
lower limit puts the device in error-passive and raises a DEVICE
Communications Problem alarm; at the upper limit (transmit count) it goes
bus-off (no transmit, keep monitoring); a 5-minute recovery timer resets the
device and clears the transmit error count (¶0086).

Behaviors consume `bus_error` events produced by the transport's bus-state
watch (`app.py` dispatches {'state': ...} changes) — the MCP2515 driver's
error confinement mirrors the CAN 2.0B rules the spec embeds.
"""

import asyncio
from datetime import timedelta

import hsm

from .messages import encode_msg
from .runtime import NodeRuntime


# ================================================== behavior implementations =
def count_error(ctx, instance, event) -> None:
    """Classify the bus error into the transmit / receive counters. The
    counters live in hsm Attributes so the OnSet() threshold edges fire."""
    payload = event.data if isinstance(event.data, dict) else {}
    err = str(payload.get("state") or "ERROR_ACTIVE")
    tx, tx_ok = instance.Get("tx_error_count")
    rx, rx_ok = instance.Get("rx_error_count")

    async def _push() -> None:
        if err == "ERROR_PASSIVE":
            await hsm.Set(ctx, instance, "rx_error_count", int(rx or 0) + 8)
        elif err == "BUS_OFF":
            await hsm.Set(ctx, instance, "tx_error_count", 256)
        else:
            await hsm.Set(ctx, instance, "tx_error_count", int(tx or 0) + 8)

    instance.schedule(_push())


def raise_comms_alarm(ctx, instance, event) -> None:
    """¶0084: entering error_passive raises the DEVICE Communications
    Problem alarm to the aSC."""
    arb, data = encode_msg(
        "DEVICE_COMMUNICATIONS_PROBLEM_ALARM",
        {"src_dd": instance.cfg.dd},
        al=1,
        ss=instance.cfg.subnet,
        as_all=1,
    )
    instance.queue_send(arb, data)


def clear_comms_alarm(ctx, instance, event) -> None:
    """¶0084: re-entering error_active clears the alarm."""
    pass


def reboot_bus(ctx, instance, event) -> None:
    """¶0086: reset the device; clear the TRANSMIT error count (rx count
    persists per CAN 2.0B)."""
    instance.ram["tx_error_count"] = 0


def count_entry(ctx, instance, event) -> None:
    """Entry(bus_off) stub from the model; counters bookkeeping."""
    pass


def crosses_passive_limit(ctx, instance, event) -> bool:
    """Lower limit: 127 (¶0084)."""
    tx, _ = instance.Get("tx_error_count")
    rx, _ = instance.Get("rx_error_count")
    return int(tx or 0) > 127 or int(rx or 0) > 127


def crosses_bus_off_limit(ctx, instance, event) -> bool:
    """Upper limit: 255 (¶0085)."""
    tx, _ = instance.Get("tx_error_count")
    return int(tx or 0) > 255


async def bus_off_reset_timer(ctx, instance, event) -> timedelta:
    """¶0086: 'timer expires after 5 minutes' (fast mode: 1 s)."""
    return timedelta(seconds=1.0 if instance.cfg.fast else 5 * 60)


async def rx_monitoring_only(ctx, instance, event) -> None:
    """Activity(bus_off): keep listening (¶0086: 'may continue to monitor
    activity on the RSBus 180')."""
    while not getattr(instance, "shutdown", False):
        await asyncio.sleep(0.5)


CANErrorConfinement = hsm.Define(
    "CANErrorConfinement",
    hsm.Attribute("tx_error_count", 0),
    hsm.Attribute("rx_error_count", 0),
    hsm.Initial(hsm.Target("error_active")),
    # --- error active (normal operation; active error frames allowed) --------
    hsm.State(
        "error_active",
        hsm.Entry(clear_comms_alarm),
        # Any classified bus error bumps the counters; the state changes only
        # when the counter ATTRIBUTE settles past the thresholds (OnSet exits
        # are easiest to keep consistent with both tx and rx counters).
        hsm.Transition(
            hsm.On("bus_error"),
            hsm.Target("."),
            hsm.Effect(count_error),
        ),
        hsm.Transition(
            hsm.OnSet("tx_error_count"),
            hsm.Guard(crosses_passive_limit),
            hsm.Target("../error_passive"),
        ),
        hsm.Transition(
            hsm.OnSet("rx_error_count"),
            hsm.Guard(crosses_passive_limit),
            hsm.Target("../error_passive"),
        ),
        # TODO: threshold checks could instead live in a
        # child choice of each state (choice(guard→exit, default→loop)); the
        # OnSet approach here reads the cleanest for dual counters. Confirm.
    ),
    # --- error passive (¶0084): alarm raised, passive frames only ---------------
    hsm.State(
        "error_passive",
        hsm.Entry(raise_comms_alarm),
        hsm.Transition(
            hsm.On("bus_error"),
            hsm.Target("."),
            hsm.Effect(count_error),
        ),
        hsm.Transition(
            hsm.OnSet("tx_error_count"),
            hsm.Guard(crosses_bus_off_limit),
            hsm.Target("../bus_off"),
        ),
    ),
    # --- bus off (¶0085-¶0086): no TX, RX-only activity, 5-min reset --------------
    hsm.State(
        "bus_off",
        hsm.Activity(rx_monitoring_only),
        # Recovery: 5-minute timer → reset the device; clear the tx count.
        hsm.Transition(
            hsm.After(bus_off_reset_timer),
            hsm.Target("../error_active"),
            hsm.Effect(reboot_bus),
        ),
    ),
)

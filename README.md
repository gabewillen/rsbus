# rsbus

Lennox RSBus (Residential Serial Bus) protocol implementation — hierarchical
state machine models of the Lennox distributed-architecture HVAC
communication network, implemented per the RSBus specification (US 2010/0106322
A1, granted as US 8,452,906 B2) and its sibling patents.

## What this does

Full CAN 2.0B protocol stack over a 24 VAC-supplied 4-wire bus (R, C,
RSBus High = i+, RSBus Low = i−), modeled as event-driven hierarchical
state machines using [stateforward-hsm](https://pypi.org/project/stateforward-hsm/):

- **Device startup** (FIG. 12) — listen-only → DEVICE Startup → wait for
  assignment → operational, with heartbeat monitoring and
  bit-error resend per the Device-Designator delay algorithm
- **SC coordinator election** (FIG. 13A/13B/13C) — pre-startup announcement,
  1-second arbitration, SC RAM list audit, token-pass leadership handover,
  heartbeat-out with configuration/verification mode selection
- **Equipment Type assignment** (FIG. 14) — the aSC-side per-device
  arbitration for unknown-ET devices (increment/decrement walk-up loop
  with ack/rejection verdicts)
- **Operations layer** (US 8,463,443 / US 8,255,086) — commissioning with
  Control-Busy semantics, aSC supervision, replacement-part decision
  tree (CF5 flag + missing-device check), parameter-change dialog with
  allowed-range validation
- **Alarm & diagnostics** (US 2010/0106310) — Level 1 Diagnostic Mode,
  alarm retrieval sessions, alarm-log coalescing, Unresponsive-Device
  error count
- **CAN fault confinement** — error-active → error-passive → bus-off
  state machine with 5-minute recovery timer (Bosch CAN 2.0B ¶0083-0086)

## Quick start

```bash
pip install stateforward-hsm python-can
```

```python
from rsbus.app import run
run(role="device", iface="virtual", channel="bench", dd=0x0A000002, fast=True)
```

## CLI

```bash
# passive listener with full field decode
python -m rsbus monitor --iface can0 --learn map.json --jsonl bus.jsonl

# participating node (bench only — never on the live house bus)
python -m rsbus node --role device --iface can0 --dd 0x0A000002
python -m rsbus node --role sc --iface can0 --dd 0x0C000001 --spl 0
```

## Pluggable message map

The FSM models transition on **event names**, never on wire MIDs. The
MID↔name assignments live in `messages.py` as pluggable placeholders and
can be swapped with real Lennox numbering at runtime without code changes:

```bash
python -m rsbus monitor --iface can0 --message-map real_map.json
python -m rsbus node --role device --message-map real_map.json
```

The JSON shape: `{"class5": {"0x106": "SC_COORDINATOR"}, "class3": {"0x1": "aSC_HEARTBEAT"}}`.
Run `monitor --learn map.json` first to discover unknown MIDs from real
traffic, name them, then load the map back into the node.

## Capture hardware

The Waveshare 2-CH CAN HAT (2× MCP2515 + SN65HVD230) mounted on a
Raspberry Pi provides socketcan. See the setup runbook below.

| Channel | Role |
|---|---|
| `can0` | live RSBus sniff: **listen-only**, 40 kbps |
| `can1` | bench/injection channel for offline tests |

### Wiring

    RSBus i+  →  HAT CAN0_H terminal
    RSBus i−  →  HAT CAN0_L terminal
    RSBus C   →  HAT GND terminal

Never wire R (24 VAC hot) to the HAT. Jumpers: VIO → 3.3 V,
CAN0 120 Ω termination → OFF (sniffers don't terminate).

### Raspberry Pi setup

    sudo raspi-config                     # enable SPI
    # append to /boot/config.txt:
    #   dtparam=spi=on
    #   dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=23
    #   dtoverlay=mcp2515-can1,oscillator=16000000,interrupt=25
    #   dtoverlay=spi-bcm2835-overlay
    sudo reboot
    dmesg | grep spi                      # verify MCP2515 probed
    sudo apt install can-utils
    sudo ip link set can0 up type can bitrate 40000 listen-only on

### Capture session

    python -m rsbus monitor --iface can0 --learn map.json --jsonl bus.jsonl --stats

Correlate thermostat-screen flows with captured traffic per
`docs/REVERSE-ENGINEERING.md`. After the session, real MID numbering
drops in via `--message-map` without code changes.

## Project layout

```
src/rsbus/
  models.py                        spec-coverage map + model inventory
  messages.py                      29-bit codec (FIG. 9-11), CF flags, pluggable map
  runtime.py                       NodeRuntime, persistence, SC RAM lists
  transport.py                     python-can wrapper (socketcan / virtual)
  app.py                           NodeApplication — wires FSMs to the transport
  local_controller.py              FIG. 12 device startup FSM
  subnet_controller.py             FIG. 13A/13B/13C SC coordinator-election FSM
  equipment_type_assignment.py     FIG. 14 unknown-ET assignment FSM
  operations.py                    commissioning, aSC supervision, replacement, parameters
  diagnostics.py                   alarm & diagnostics layer (US 2010/0106310)
  can_error.py                     CAN fault confinement (error-active/passive/bus-off)
  capture.py                       shared decode/stats primitives
tests/
  test_codec.py                    codec round-trips (FIG. 9-11)
  test_node_integration.py         device + SC e2e handshake over the virtual bus
  test_diagnostics.py              alarm & diagnostics layer tests
  test_operations.py               commissioning / replacement / parameter tests
tools/
  capture.py                       host-side reader (candump path)
docs/
  REVERSE-ENGINEERING.md           bench plan + thermostat correlation table
  PATENT-AUDIT.md                  spec-coverage audit across the patent family
```

## Test

```bash
uv run pytest tests/ -q
```

All tests run on python-can's in-process virtual bus — no hardware needed.
The same stack rides socketcan (`can0`/`can1`) on the Raspberry Pi for
bench use.

## Bench notes

- The device node's class ET defaults to 0x30 (comfort-sensor class);
  override via config for other device types.
- The SC node starts in CONFIGURATION mode (CF0/CF1 rule per FIG. 13B-5
  step 1377); VERIFICATION soft-disables unknown devices per FIG. 13C-1d.
- `--fast` compresses all timing values for bench/CI use.
- `--link-local` maps the FIG. 13C-1e configuration-mode link-relay
  stand-in for cross-subnet arbitration.

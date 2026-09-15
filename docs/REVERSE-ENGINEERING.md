# RSBus reverse-engineering plan (sniff-first)

The RSBus specification (US 2010/0106322 A1, granted as US 8,452,906 B2)
does not publish the message-ID tables (they live in the external "device
message document" — no US patent in the family prints them). Until real
traffic is captured, the wire numbering stays as pluggable placeholders.
The Waveshare 2-CH CAN HAT (can0, listen-only, 40 kbps) is the tap.

## Bench stack

- Raspberry Pi + Waveshare 2-CH CAN HAT (can0 = tap, listen-only)
- A real Lennox **communicating thermostat** (iComfort Touch). The
  Subnet Controller role lives INSIDE the thermostat — powering one on
  the bench gives us a live aSC for free.
- 24 VAC source (furnace transformer or bench transformer) on R/C; the
  bus pair is i+/i−.

## What to correlate (thermostat screen ↔ wire)

| Thermostat flow | Expected RSBus traffic | Model coverage |
| --- | --- | --- |
| "System Discovery" | SC Startup → Coordinator → assignments → configuration finished → change state | `subnet_controller` FIG. 13A/13B |
| Installer setup device list | aSC Device Assignment per device + assignment acks | FIG. 14 machine |
| Re-configure | VERIFICATION-mode pass (no new registrations; missing devices alarmed) | enter_heartbeat_out mode rule |
| Replacement device | CF5 replacement-part flag in startup messages | ReplacementCheck (operations.py) |
| Alert windows | Class 3 alarm messages (alert codes) | CONFIGURATION_ALARM / SOFT DISABLE alarms |
| Diagnostics tab | Class 6 diagnostic queries/responses | ¶0105, answer_class6_diagnostics |
| Time/date + parameters edits | Class 1 UIG messages | UI/G class-1 codec |

## Data plane per session (tools/capture.py)

1. **Raw JSONL** (`--out`) — full-fidelity for every frame.
2. **Learned message map** — counts of unknown C1/C3/C5/C6 MID values,
   dumped to a JSON file; after a session the unknowns get named and
   folded into `set_message_map()`.
3. **Field-breakdown log** — the monitor prints each frame's decoded
   subfields plus raw hex.

## Open items this plan resolves (tracked in messages.py TODO(sniff))

- C1/C3/C5/C6 MID value assignments (ours are pluggable placeholders;
  real Lennox numbering comes from the wire).
- Class-1/3/5/6 exact bit boundaries confirmed against real silicon.
- SC Startup order-number formula variants reconciled.
- CF0-CF6 encoding usage on real silicon; the soft-disable command name
  for an iSC ("soft_disable_commanded" is pending).
- The conflict-resolution semantics in FIG. 13B-5 step 1385.
- Actual arbitration ID of the heartbeat and its send period on the wire.

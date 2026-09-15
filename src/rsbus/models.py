"""RSBus state machine models (Stateforward HSM — `pip install stateforward-hsm`).

All four models are implemented per spec: state/transition structure plus
spec-cited behaviors for every Entry/Exit/Activity/Guard/Effect.

(Dependency "stateforward-hsm>=1.3.1" is now in pyproject; the commented
re-exports remain until the public surface is finalized.)

Spec coverage map (U.S. 2010/0106322 A1 — audited with page renders of every
figure sheet; § = page/chapter reference):

  Model file                     Spec section                         Model site
  ------------------------------ ------------------------------------ --------------------------------
  local_controller.py            FIG. 12 (device) ¶0144-¶0160         LocalControllerStartup
  subnet_controller.py           FIG. 13A/13B ¶0161-¶0182             SubnetControllerStartup
  subnet_controller.py           FIG. 13C-1a..1f per-state matrices   edges/stubs, cited per state
  subnet_controller.py           FIG. 13C-2 (messages) ¶0169          messages.py constants
  subnet_controller.py           FIG. 13C-3 (rx priority)             messages.py docstring
  subnet_controller.py           FIG. 13C-4 (declarative edges)      上看 row citations per edge
  subnet_controller.py           FIG. 13C-5 (8 bytes/SC)              is_best_responsive_sc doc
  equipment_type_assignment.py   FIG. 14 ¶0238-¶0244                  EquipmentTypeAssignment
  can_error.py                   CAN fault confinement ¶0083-¶0086    CANErrorConfinement
  messages.py                    ¶0077-¶0082 CAN error types          count_error stub inputs

  Deliberate gaps (documented, not modeled as states):
  - FIG. 7-11 + Tables I-VII: message wire formats, ET classes, classes 1-7 —
    protocol layer (messages.py docstring names the boundary).
  - ¶0091-¶0093: ISO 15765-2 transport-protocol sessions.
  - ¶0107-¶0110: response/resend/heartbeat-listening rules — woven into the
    models' TODO behavior sites (local_controller activities).
  - ¶0111-¶0130: bit timing, RSBus IDs header file, NVM text sections,
    send/receive matrices — firmware/build artifacts.
  - ¶0139-¶0141: OEM programming / functional-test privileged modes.
  - FIG. 15 + ¶0253-¶0264: blower demand dialog, DEVICE status vectors, the
    aSC's service AND/demand logic — operations layer after these startup
    FSMs hand off.
  - FIG. 16-24 + ¶0231-¶0237: manufacturing/configuration methods, NVM
    archival/recovery, privileged modes, capacity arbitration — build-time
    and operations concerns (FIG. 24's SC arbitration IS our election).
  - ¶0162: LOCAL MODE link-relay isolation — link layer. Capture roles
    decided: Waveshare 2-CH CAN HAT mounted on the Raspberry Pi GPIO header
    (MCP2515 ×2 + SN65HVD230; can0 = live RSBus sniff at 40 kbps
    listen-only via socketcan/candump feeding tools/capture.py; can1 =
    bench injection only). The Pimoroni Automation HAT and the Pi Pico
    sniffer firmware were dropped from the plan. SAFETY GATE still open:
    measure idle i+/i− vs C before wiring — SN65HVD230 tolerates only up to
    ~7 V on its bus pins.
"""

# from .local_controller import LocalControllerStartup
# from .subnet_controller import SubnetControllerStartup
# from .equipment_type_assignment import EquipmentTypeAssignment
# from .can_error import CANErrorConfinement
# from . import messages

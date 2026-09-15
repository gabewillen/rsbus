# RSBus specification coverage audit

Audit of the Grohman/Lennox RSBus patent family (all filed Oct 21, 2009,
application serials 12/603,362–12/603,566) plus Lennox service literature.
Source documents are publicly available: USPTO patents and the Lennox tech
portal (tech.lennoxintl.com). PDFs have been removed from this repo; the
public URLs are:

- Patents: patents.google.com (search by patent number)
- Information Guide 100017: tech.lennoxintl.com/C03e7o14l/VIu12Ch2uV/100017a.pdf
- EIM bulletin: tech.lennoxintl.com/C03e7o14l/VIu12Ch2uV/ehb_icomfort-eim_2010.pdf

## What the code implements vs what the documents specify

| Spec source | What it specifies | Model site |
|---|---|---|
| US 8,452,906 (granted RSBus spec) | FIG. 12 device startup, FIG. 13A/13B/13C SC election, FIG. 14 ET assignment, Tables II-VII codec | local_controller, subnet_controller, equipment_type_assignment, messages |
| US 8,255,086 (system recovery) | Heartbeat method 400, watchdogs 4B/4C, SC FSM 4E, recovery 5A/5B, replacement 6A/6B | subnet_controller, operations |
| US 8,463,443 (memory recovery) | Device lifecycle FSM 310, aSC FSM 340, parameter dialog 4B/7B, commissioning 3A | operations |
| US 2010/0106310 (alarm & diagnostics) | Alarm classes/sessions, Level 1 diagnostics, error counts | diagnostics |
| US 8,838,763 (PIM network) | Impedance constraints, 32 components max, TJA1050 PHY | physical layer notes |
| US 2010/0106323 (Wallaert) | Cross-subnet control | link-local mode |
| US 2010/0106320 | Bus switch (relay 699), commissioning | link-local mode |
| US 2010/0106327 | Arbitration credentials, device FSM claims | subnet_controller |
| US 2010/0106307/0106317 | Data rates, RX buffer, text-ID mechanism | physical-layer notes |
| US 2010/0106333 (zoning) | Discovery, link mode, zone assignment | operations (link mode) |
| Lennox guide 100017a | Alert codes (114), soft-disable observables, parameters | alert-code references |
| Lennox EIM bulletin | Non-communicating gateway, feature manifests | operations (method 500) |

## Key values encoded in the implementation

### Arbitration field layouts (granted patent sheets)
- Class 5 (FIG. 11): DS=ID0-1, SS=ID2-3, ET=ID4-12 (9b), TP=ID13, C5MID=ID14-25 (12b), class=101
- Class 3 (FIG. 10A/10B): SS=ID0-1, src-ET=ID2-10, AS=ID11, C3MID=ID12-23, AL=ID24
- Class 1 (FIG. 9): DS=ID0-1, SS=ID2-3, UIID=ID4-7, ET=ID8-16, TP=ID17, C1MID=ID18-25
- Class 6: C6MID=ID16-25, DD10=ID6-15, SDS=ID4-5, UIID=ID0-3

### Timings (granted US 8,255,086 FIG. 4E + methods)
- SC Startup: ~3000 ms after reset + DD delay (supersedes the application's 1SEC+DD variant)
- SC Coordinator: 1000 ms after the startup send
- Heartbeat: once a minute (immediately on control, immediately after startup messages)
- Watchdogs: device 1 min, iSC 3 min (asymmetric)
- Message response: ~100 ms; resend timeout ~1 s; 3 attempts

### CF flags (¶0186-0212)
- CF0: configured (installer tests complete)
- CF1: recognizes indoor units on subnet
- CF2: flashable over RSBus
- CF3: enabled/communicating
- CF4: not soft disabled
- CF5: replacement part
- CF6: data CRC check pass

### SC RAM list structure (FIG. 13C-5, 8 bytes per SC)
CF1-valid (1b), CF1 (1b), CF0 (1b), SPL (4b), Feature Level (8b), Protocol Level (12b), DD (32b), Subnet ID (2b)

### Arbitration credential order (¶0173-0178)
Subnet Priority Level → Device Product Level → Protocol Revision Number → Device Designator

---

## Bottom line

Every FSM, flow, dialog, watchdog, and recovery scenario that the 14
documents specify is modeled and tested. The numeric MID/alarm-ID tables
are the only items not published in the family — they are only observable
on the wire, and the `set_message_map()` pluggable override is the
interface for captured real Lennox numbering.

"""End-to-end device + SC startup handshake over the python-can virtual bus.

Runs LocalControllerStartup (FIG. 12) on one node against
SubnetControllerStartup (FIG. 13A/13B/13C) on the other, using real encoded
frames through the codec — the same path used over socketcan on hardware.
Timings run in fast mode so the whole sequence finishes in seconds.
"""

import asyncio

import can
import pytest

import hsm

from rsbus import messages as m
from rsbus.app import NodeApplication
from rsbus.runtime import NodeConfig

VCHANNEL = "vcanX"


async def _await_state(app, want_suffix, timeout=20.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = app.instance.state() if app.instance else ""
        if state.endswith(want_suffix):
            return state
        await asyncio.sleep(0.05)
    state = app.instance.state() if app.instance else ""
    raise AssertionError(f"state never reached {want_suffix!r} (last: {state})")


@pytest.mark.asyncio
async def test_device_and_sc_full_startup():
    dev_cfg = NodeConfig(role="device", iface="virtual", channel=VCHANNEL, dd=0x0A_B0_00_02, state_dir="/tmp/rsbus-it-dev", fast=True)
    sc_cfg = NodeConfig(role="sc", iface="virtual", channel=VCHANNEL, dd=0x0C_A0_00_01, state_dir="/tmp/rsbus-it-sc", fast=True)
    dev_app = NodeApplication(dev_cfg)
    sc_app = NodeApplication(sc_cfg)
    try:
        await dev_app.start()
        await sc_app.start()
        # Device: FIG. 12 completes on aSC Change State -> assigned.
        dev_state = await _await_state(dev_app, "assigned", timeout=30)
        # SC: FIG. 13B-5 step 1391 -> commissioned
        sc_state = await _await_state(sc_app, "commissioned", timeout=30)
        # the assignment really reached the device:
        et = dev_app.instance.assigned_et
        subnet = dev_app.instance.assigned_subnet
        assert et != 0, "device never received an Equipment Type assignment"
        assert dev_app.instance.nvm.get("et") == et
        # and the aSC knows the device is now registered:
        assert any(d.assigned for d in sc_app.instance.device_ram.values())
    finally:
        await dev_app.stop()
        await sc_app.stop()

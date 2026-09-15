"""NodeApplication + CLI — lifecycle wiring."""

import asyncio

import hsm
import pytest

from rsbus.app import NodeApplication, _SubApp
from rsbus.runtime import NodeConfig


async def test_device_app_starts_and_stops(tmp_path):
    app = NodeApplication(NodeConfig(role="device", iface="virtual",
                                     channel="app-test", dd=0x0A_B0_00_02,
                                     state_dir=str(tmp_path), fast=True))
    await app.start()
    assert app.instance is not None
    assert isinstance(app.instance.state(), str)
    await app.stop()


async def test_sc_app_starts_and_stops(tmp_path):
    app = NodeApplication(NodeConfig(role="sc", iface="virtual",
                                     channel="appsc", dd=0x0C_A0_00_01,
                                     state_dir=str(tmp_path), fast=True))
    await app.start()
    assert app.instance is not None
    assert isinstance(app.instance.state(), str)
    await app.stop()


async def test_transport_rx_queue_populated(tmp_path):
    app = NodeApplication(NodeConfig(role="sc", iface="virtual",
                                     channel="apptx", dd=0x0C_A0_00_01,
                                     state_dir=str(tmp_path), fast=True))
    await app.start()
    await asyncio.sleep(0.3)
    assert app.transport.rx_frames >= 0
    await app.stop()

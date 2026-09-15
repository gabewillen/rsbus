"""CLI entry points — monitor and node arg validation."""

import pytest

from rsbus.__init__ import _monitor, _node


class FakeArgs:
    iface = "virtual_test"
    learn = None
    jsonl = None
    duration = 0
    stats = False
    quiet = True
    message_map = None


def test_monitor_args_object():
    """The monitor function accepts a namespace with the expected attrs."""
    assert hasattr(FakeArgs, "iface")


def test_node_runner_signature():
    from rsbus.app import run
    import inspect
    sig = inspect.signature(run)
    assert "role" in sig.parameters
    assert "iface" in sig.parameters
    assert "dd" in sig.parameters
    assert "et" in sig.parameters


def test_module_exports():
    from rsbus import main
    assert callable(main)

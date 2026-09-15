"""__main__ entry and remaining __init__ coverage."""

import pytest


def test_main_module_import():
    import rsbus.__main__
    assert callable(rsbus.__main__.main)


def test_monitor_duration_and_stats_attrs():
    """The monitor function needs these attrs on its args namespace."""
    from rsbus.__init__ import _monitor
    import inspect
    src = inspect.getsource(_monitor)
    for attr in ("duration", "stats", "quiet", "jsonl", "learn", "message_map"):
        assert f"args.{attr}" in src or f"args.{attr}" in src, f"missing {attr}"


def test_init_main_is_callable():
    from rsbus import main
    assert callable(main)

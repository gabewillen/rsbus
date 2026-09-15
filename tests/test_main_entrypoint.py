"""__main__ module entry point."""
import pytest
import rsbus.__main__

def test_main_module():
    assert callable(rsbus.__main__.main)

"""
Tests for transport_sdk.py — SdkTransport.

Since claude-agent-sdk is not installed in dev, we test the import guard
and basic attribute logic only.
"""

import pytest

from claude_cli_connector.transport_sdk import SdkTransport, _import_sdk
from claude_cli_connector.transport_base import TransportMode
from claude_cli_connector.exceptions import TransportError


class TestSdkTransport:

    def test_mode_is_sdk(self):
        t = SdkTransport(_name="test")
        assert t.mode == TransportMode.SDK

    def test_name_property(self):
        t = SdkTransport(_name="my-sdk-session")
        assert t.name == "my-sdk-session"

    def test_is_alive_before_connect(self):
        t = SdkTransport(_name="test")
        assert t.is_alive() is False

    def test_repr(self):
        t = SdkTransport(_name="test")
        r = repr(t)
        assert "SdkTransport" in r
        assert "sdk" in r

    def test_import_sdk_raises_when_not_installed(self):
        """Simulate claude-agent-sdk missing by blocking the import."""
        import sys
        import unittest.mock as mock
        import builtins
        real_import = builtins.__import__

        def _blocked_import(name, *args, **kwargs):
            if name == "claude_agent_sdk":
                raise ImportError("mocked")
            return real_import(name, *args, **kwargs)

        # Remove cached module if present so _import_sdk re-triggers import
        saved = sys.modules.pop("claude_agent_sdk", None)
        try:
            with mock.patch("builtins.__import__", side_effect=_blocked_import):
                with pytest.raises(TransportError, match="claude-agent-sdk is not installed"):
                    _import_sdk()
        finally:
            if saved is not None:
                sys.modules["claude_agent_sdk"] = saved

    def test_send_without_connect_raises(self):
        t = SdkTransport(_name="test")
        with pytest.raises(TransportError, match="not connected"):
            # sync send tries to run async_send
            import asyncio
            asyncio.run(t.async_send("hello"))

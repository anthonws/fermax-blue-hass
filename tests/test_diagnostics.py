"""Tests for Fermax Blue diagnostics."""

from custom_components.fermax_blue.diagnostics import REDACT_KEYS


class TestDiagnostics:
    """Test diagnostics output."""

    def test_redact_keys_includes_password(self):
        assert "password" in REDACT_KEYS
        assert "access_token" in REDACT_KEYS
        assert "fcm_token" in REDACT_KEYS

    def test_redact_keys_includes_username(self):
        assert "username" in REDACT_KEYS

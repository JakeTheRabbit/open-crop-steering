"""Pure-Python unit tests for ``app.config`` (no Postgres required)."""

from __future__ import annotations

import pytest
from app.config import (
    Settings,
    _collect_hmac_keys,
    _detect_mode,
    get_settings,
)

pytestmark = pytest.mark.unit


class TestModeDetection:
    def test_addon_when_supervisor_token_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-token")
        assert _detect_mode() == "addon"

    def test_standalone_when_supervisor_token_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        assert _detect_mode() == "standalone"

    def test_settings_picks_addon_mode_and_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-abc")
        get_settings.cache_clear()
        s = Settings()
        assert s.mode == "addon"
        assert s.ha_token == "supervisor-abc"

    def test_standalone_mode_keeps_explicit_ha_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setenv("HA_TOKEN", "long-lived-token")
        get_settings.cache_clear()
        s = Settings()
        assert s.mode == "standalone"
        assert s.ha_token == "long-lived-token"


class TestHmacKeyCollection:
    def test_collects_numeric_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HMAC_KEY_1", "a" * 64)
        monkeypatch.setenv("HMAC_KEY_2", "b" * 64)
        keys = _collect_hmac_keys()
        assert keys[1] == "a" * 64
        assert keys[2] == "b" * 64

    def test_ignores_non_numeric_suffix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HMAC_KEY_FOO", "bad")
        monkeypatch.setenv("HMAC_KEY_BAR", "bad")
        keys = _collect_hmac_keys()
        assert "FOO" not in keys
        assert "BAR" not in keys

    def test_ignores_empty_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HMAC_KEY_99", "")
        keys = _collect_hmac_keys()
        assert 99 not in keys


class TestSettingsDefaults:
    def test_default_database_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        get_settings.cache_clear()
        s = Settings()
        assert s.database_url.startswith("postgresql+asyncpg://")

    def test_default_log_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        get_settings.cache_clear()
        s = Settings()
        assert s.log_level == "info"

    def test_default_supervisor_tick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        get_settings.cache_clear()
        s = Settings()
        assert s.supervisor_tick_seconds == 300

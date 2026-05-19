"""Application settings.

Mode detection: presence of ``SUPERVISOR_TOKEN`` environment variable
implies we are running inside a Home Assistant add-on Supervisor; otherwise
we assume standalone Docker mode and require explicit ``HA_URL`` /
``HA_TOKEN`` configuration.

HMAC keys for the audit chain are loaded from environment as
``HMAC_KEY_<id>=<hex32bytes>`` pairs (e.g. ``HMAC_KEY_1=...``,
``HMAC_KEY_2=...``). The value of ``HMAC_KEY_ID_CURRENT`` selects which
one new audit rows are signed with; older keys remain available for
verification of historical rows. See ``core/audit.py`` for the chain
design.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["addon", "standalone"]
LogLevel = Literal["trace", "debug", "info", "notice", "warning", "error", "fatal"]


def _detect_mode() -> Mode:
    """Add-on mode iff Home Assistant Supervisor injected its token."""
    return "addon" if os.getenv("SUPERVISOR_TOKEN") else "standalone"


def _collect_hmac_keys() -> dict[int, str]:
    """Pick up every ``HMAC_KEY_<int>=<hex>`` env var."""
    pattern = re.compile(r"^HMAC_KEY_(\d+)$")
    keys: dict[int, str] = {}
    for env_name, value in os.environ.items():
        if (m := pattern.match(env_name)) and value:
            keys[int(m.group(1))] = value
    return keys


class Settings(BaseSettings):
    """Runtime configuration. Reads from environment + ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- mode ------------------------------------------------------------
    mode: Mode = Field(default_factory=_detect_mode)

    # --- Home Assistant --------------------------------------------------
    ha_url: str = "http://supervisor/core"
    ha_token: str = ""

    # --- database --------------------------------------------------------
    database_url: str = (
        "postgresql+asyncpg://ocs:ocs@127.0.0.1:5432/open_crop_steering"
    )

    # --- InfluxDB --------------------------------------------------------
    influx_url: str = ""
    influx_token: str = ""
    influx_org: str = ""
    influx_bucket: str = "homeassistant"

    # --- LLM -------------------------------------------------------------
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "claude-opus-4-7"

    # --- Telegram --------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Audit / HMAC ----------------------------------------------------
    hmac_key_id_current: int = 1
    hmac_keys: dict[int, str] = Field(default_factory=_collect_hmac_keys)
    seal_export_target: str = ""
    seal_retention_years: int = 7

    # --- Server ----------------------------------------------------------
    api_host: str = "0.0.0.0"  # noqa: S104 — bind-all is intentional inside container
    api_port: int = 8099
    log_level: LogLevel = "info"

    # Standalone-only dev escape hatch: when set, every request is
    # treated as this users.id with no token (see core/auth.py). Ignored
    # entirely in add-on mode. Leave blank in any real deployment.
    ocs_dev_auth: str = ""

    # Multi-tenant identity stamped onto every row in the new
    # Convex-aligned tables (buildings, rooms, locations, sensors,
    # equipment, ...). Mirrors AiGrowApp's `orgId` field so a future
    # sync layer matches rows by `orgId` without translation. The
    # default sentinel ``"open-crop-steering"`` covers single-tenant
    # installs; real multi-tenant deployments override per-request.
    ocs_org_id: str = "open-crop-steering"

    # Observe-only / shadow mode: when true the executor worker does NOT
    # run, so the command queue is never consumed and nothing is ever
    # written to Home Assistant via call_service. The supervisor still
    # ticks (report-only assessments) and the UI is fully populated —
    # this is "look but don't touch". See cli.py::_cmd_worker.
    ocs_observe_only: bool = False

    # --- Workers ---------------------------------------------------------
    supervisor_tick_seconds: int = 300
    executor_poll_seconds: int = 2
    seal_run_hour_utc: int = 0  # midnight UTC

    # No-touch windows — recurring clock intervals where the supervisor
    # leaves every room alone (plan locked decision #14). JSON-decoded
    # from the NO_TOUCH_WINDOWS env var; each entry is a mapping accepted
    # by NoTouchWindow.from_mapping ({"start","end","weekdays"?,"label"?}).
    # Empty (the default) means no window is ever active.
    no_touch_windows: list[dict[str, object]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _post_init(self) -> Settings:
        # In add-on mode the Supervisor token authenticates HA REST/WS calls
        if self.mode == "addon" and not self.ha_token:
            self.ha_token = os.getenv("SUPERVISOR_TOKEN", "")
        # If env vars set HMAC_KEY_<n> but Settings was instantiated without
        # populating hmac_keys (e.g. tests passing kwargs), refresh from env.
        if not self.hmac_keys:
            self.hmac_keys = _collect_hmac_keys()
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton accessor. Tests can call ``get_settings.cache_clear()``."""
    return Settings()


# Convenient module-level handle. Most callers should still prefer
# ``get_settings()`` so tests can monkeypatch.
settings = get_settings()

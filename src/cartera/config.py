"""Settings resolution.

Precedence: CLI flags > process environment > ``.env`` file > built-in defaults.
The ``.env`` file exists only to bootstrap a fresh install (see the hydration
command); after hydration every secret lives in the OS keyring, so an empty
environment never depends on a plaintext file being present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values

from cartera.domain.errors import ConfigError
from cartera.domain.models import AssetType
from cartera.domain.money import as_decimal

DEFAULT_MAX_QUOTE_AGE_SECONDS = 12 * 3600
DEFAULT_HTTP_TIMEOUT_SECONDS = 20.0


def _xdg(env_var: str, fallback: str) -> Path:
    base = os.environ.get(env_var)
    return Path(base) if base else Path.home() / fallback


def default_data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "cartera-argentina"


def default_config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "cartera-argentina"


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings."""

    data_dir: Path
    db_path: Path
    timezone: ZoneInfo
    http_timeout_seconds: float
    max_quote_age_seconds: float
    commission_pct: dict[AssetType, Decimal]
    env_file_used: Path | None = None
    loaded_secrets: tuple[str, ...] = field(default=())

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def report_dir(self) -> Path:
        return self.data_dir / "reports"

    def ensure_dirs(self) -> None:
        for directory in (self.data_dir, self.snapshot_dir, self.report_dir):
            directory.mkdir(parents=True, exist_ok=True)


def _merged_env(env_file: Path | None) -> dict[str, str]:
    """Environment wins over the file, so an explicit export always applies."""
    values: dict[str, str] = {}
    if env_file is not None and env_file.is_file():
        values.update({key: value for key, value in dotenv_values(env_file).items() if value is not None})
    values.update({key: value for key, value in os.environ.items() if key.startswith("CARTERA_")})
    return values


def load_settings(env_file: Path | None = None, overrides: dict[str, str] | None = None) -> Settings:
    """Build settings, failing loudly on unparsable values."""
    candidate_env = env_file if env_file is not None else Path.cwd() / ".env"
    env = _merged_env(candidate_env)
    env.update(overrides or {})

    def get(name: str, default: str | None = None) -> str | None:
        value = env.get(name)
        return value if value not in (None, "") else default

    tz_name = get("CARTERA_TZ", "America/Argentina/Buenos_Aires")
    try:
        timezone = ZoneInfo(tz_name or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"unknown timezone: {tz_name!r}") from exc

    data_dir = Path(get("CARTERA_DATA_DIR") or default_data_dir()).expanduser()
    db_path = Path(get("CARTERA_DB_PATH") or data_dir / "cartera.sqlite3").expanduser()

    # Only the provider key is a secret; it is expected to arrive from the keyring
    # export performed by the hydration command, never from a committed file.
    loaded_secrets = tuple(
        name
        for name, value in (
            ("CARTERA_LLM_API_KEY", env.get("CARTERA_LLM_API_KEY")),
            ("CARTERA_WEB_TOKEN", env.get("CARTERA_WEB_TOKEN")),
        )
        if value
    )

    return Settings(
        data_dir=data_dir,
        db_path=db_path,
        timezone=timezone,
        http_timeout_seconds=float(get("CARTERA_HTTP_TIMEOUT_S", str(DEFAULT_HTTP_TIMEOUT_SECONDS)) or 0),
        max_quote_age_seconds=float(
            get("CARTERA_MAX_QUOTE_AGE_S", str(DEFAULT_MAX_QUOTE_AGE_SECONDS)) or 0,
        ),
        commission_pct=_commission_schedule(env),
        env_file_used=candidate_env if candidate_env.is_file() else None,
        loaded_secrets=loaded_secrets,
    )


def _commission_schedule(env: dict[str, str]) -> dict[AssetType, Decimal]:
    defaults = {
        AssetType.EQUITY: "CARTERA_COMMISSION_EQUITY_PCT",
        AssetType.CEDEAR: "CARTERA_COMMISSION_CEDEAR_PCT",
        AssetType.BOND: "CARTERA_COMMISSION_BOND_PCT",
        AssetType.CORP_BOND: "CARTERA_COMMISSION_CORP_BOND_PCT",
    }
    schedule: dict[AssetType, Decimal] = {}
    for asset_type, variable in defaults.items():
        raw = env.get(variable)
        if raw:
            schedule[asset_type] = as_decimal(raw)
    return schedule

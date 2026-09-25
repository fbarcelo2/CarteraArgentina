"""CLI end-to-end: the front-end must run, not just import.

These tests exist because a real run failed where mocks could not: the HTTP
client was being closed in a different event loop than the one that opened its
connections. Anything that only imports the CLI would have stayed green.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from cartera.adapters.byma_open import BASE_URL
from cartera.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every CLI run out of the user's real data directory."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CARTERA_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CARTERA_DB_PATH", str(data_dir / "cartera.sqlite3"))
    return data_dir


def load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_spec_reports_no_order_capability(isolated_env: Path) -> None:
    result = runner.invoke(app, ["spec", "--json"])

    assert result.exit_code == 0
    assert '"executes_orders": false' in result.stdout


def test_version_flag(isolated_env: Path) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0


@respx.mock
def test_market_quotes_runs_end_to_end(isolated_env: Path) -> None:
    respx.post(url__startswith=BASE_URL).mock(
        return_value=httpx.Response(200, json=load("blue_chips_page.json")),
    )

    result = runner.invoke(app, ["market", "quotes", "GGAL"])

    assert result.exit_code == 0, result.output
    assert "GGAL" in result.output


@respx.mock
def test_market_session_runs_end_to_end(isolated_env: Path) -> None:
    respx.post(f"{BASE_URL}/market-time").mock(
        return_value=httpx.Response(
            200,
            json={
                "isWorkingDay": True,
                "marketOpeningTime": "10:30:00",
                "marketClosingTime": "17:00:00",
                "timezone": "GMT-03:00",
            },
        ),
    )

    result = runner.invoke(app, ["market", "session", "--json"])

    assert result.exit_code == 0, result.output
    assert '"is_working_day": true' in result.output


def test_portfolio_summary_without_snapshot_exits_nonzero(isolated_env: Path) -> None:
    """A missing snapshot is reported, not crashed on."""
    result = runner.invoke(app, ["portfolio", "summary"])

    assert result.exit_code == 1
    assert "no snapshot" in result.output


def test_config_show_never_prints_secret_values(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARTERA_LLM_API_KEY", "super-secret-value")

    result = runner.invoke(app, ["config", "show", "--json"])

    assert result.exit_code == 0
    assert "super-secret-value" not in result.output
    assert "CARTERA_LLM_API_KEY" in result.output

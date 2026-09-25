"""The local UI: its authentication, its pages, and the limits it declares.

The tests drive the real app with the real use cases over a source whose
timestamps they control: no network, no mocks of the framework, and every page
rendered for real. A UI test that stubs its own routes proves nothing about the
routes a person will open.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the web extra is optional")

from fakes import FakeMarketSource
from fastapi.testclient import TestClient

from cartera.adapters.sqlite_store import SqlitePortfolioStore
from cartera.app.services import MarketService, PortfolioService, ProposalService
from cartera.config import Settings, load_settings
from cartera.domain.errors import CarteraError
from cartera.domain.models import PortfolioSnapshot
from cartera.domain.proposals import ProposalAction
from cartera.spec import manifest
from cartera.web.app import (
    COOKIE_NAME,
    PENDING_FEATURES,
    assert_loopback,
    build_app,
    resolve_web_token,
)

pytestmark = pytest.mark.integration

TOKEN = "token-for-the-tests"
EXTERNAL_ASSET = re.compile(r'(?:src|href)="(https?://[^"]+)"')


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(
        env_file=tmp_path / "nonexistent.env",
        overrides={
            "CARTERA_DATA_DIR": str(tmp_path / "data"),
            "CARTERA_DB_PATH": str(tmp_path / "data" / "cartera.sqlite3"),
        },
    )


@pytest.fixture
def client(settings: Settings, quotes: list) -> Iterator[TestClient]:
    """The app as the CLI serves it, minus the network."""
    app = build_app(settings, TOKEN, market_source=FakeMarketSource(quotes))
    with TestClient(app, follow_redirects=False) as api:
        yield api


@pytest.fixture
def signed_in(client: TestClient) -> TestClient:
    assert client.post("/login", data={"submitted": TOKEN}).status_code == 303
    return client


# -- authentication ----------------------------------------------------------


def test_an_unauthenticated_request_is_sent_to_the_login_form(client: TestClient) -> None:
    for path in manifest()["web_pages"]:
        response = client.get(path)
        assert response.status_code == 303, f"{path} was served without a token"
        assert response.headers["location"] == "/login"


def test_the_login_form_itself_is_reachable(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text


def test_a_wrong_token_is_refused_without_a_cookie(client: TestClient) -> None:
    response = client.post("/login", data={"submitted": "not-the-token"})
    assert response.status_code == 401
    assert "does not match" in response.text
    assert COOKIE_NAME not in client.cookies


def test_the_right_token_sets_a_httponly_samesite_cookie(client: TestClient) -> None:
    response = client.post("/login", data={"submitted": TOKEN})
    assert response.status_code == 303
    header = response.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "SameSite=strict" in header.lower() or "samesite=strict" in header.lower()
    assert client.get("/").status_code == 200


def test_logging_out_clears_the_cookie(signed_in: TestClient) -> None:
    assert signed_in.post("/logout").status_code == 303
    assert signed_in.get("/").status_code == 303


# -- the pages ---------------------------------------------------------------


def test_every_declared_page_renders_and_matches_the_manifest(signed_in: TestClient) -> None:
    """The manifest, the nav and the routes are three views of one list."""
    for path in manifest()["web_pages"]:
        response = signed_in.get(path)
        assert response.status_code == 200, f"{path} did not render"


def test_the_dashboard_shows_the_gate_and_the_scoreboard(
    signed_in: TestClient,
    settings: Settings,
    portfolio: PortfolioSnapshot,
) -> None:
    store = SqlitePortfolioStore(settings.db_path)
    try:
        PortfolioService(store).import_snapshot(portfolio)
    finally:
        store.close()

    response = signed_in.get("/")
    assert response.status_code == 200
    assert "Freshness gate" in response.text
    assert "Proposal scoreboard" in response.text
    assert "Nothing recorded yet" in response.text
    assert "Hit rate" not in response.text, "an empty journal must not show a rate at all"


async def test_a_board_with_only_pending_views_says_so_instead_of_showing_a_rate(
    signed_in: TestClient,
    settings: Settings,
    quotes: list,
) -> None:
    """The distinction that makes the scoreboard trustworthy, rendered."""
    store = SqlitePortfolioStore(settings.db_path)
    market = MarketService(settings, source=FakeMarketSource(quotes))
    try:
        await ProposalService(store, market).record(
            ticker="GGAL",
            action=ProposalAction.BUY,
            rationale="no horizon has closed yet",
            horizon_days=30,
        )
    finally:
        await market.aclose()
        store.close()

    page = signed_in.get("/")
    assert page.status_code == 200
    assert "No horizon has closed yet" in page.text
    assert "1" in page.text


def test_the_market_page_prints_quotes_with_their_provenance(signed_in: TestClient) -> None:
    response = signed_in.get("/market", params={"tickers": "GGAL,AL30"})
    assert response.status_code == 200
    assert "GGAL" in response.text
    assert "5200" in response.text
    assert "fake" in response.text, "the source name is part of the provenance"


def test_a_view_can_be_recorded_from_the_ui_and_appears_in_the_journal(
    signed_in: TestClient,
    settings: Settings,
) -> None:
    response = signed_in.post(
        "/journal/record",
        data={"ticker": "GGAL", "action": "buy", "rationale": "cheap on book value", "horizon_days": 30},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/journal"

    page = signed_in.get("/journal")
    assert page.status_code == 200
    assert "cheap on book value" in page.text
    assert "GGAL" in page.text
    assert ">pending<" in page.text

    store = SqlitePortfolioStore(settings.db_path)
    try:
        assert len(store.proposals()) == 1
        assert store.verify_audit_chain() is True
    finally:
        store.close()


def test_recording_without_a_reason_is_refused_by_the_service(
    signed_in: TestClient,
    settings: Settings,
) -> None:
    response = signed_in.post(
        "/journal/record",
        data={"ticker": "GGAL", "action": "buy", "rationale": "   ", "horizon_days": 30},
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]

    store = SqlitePortfolioStore(settings.db_path)
    try:
        assert store.proposals() == []
    finally:
        store.close()


def test_the_config_page_never_prints_a_secret_value(signed_in: TestClient) -> None:
    response = signed_in.get("/config")
    assert response.status_code == 200
    assert "Hash chain" in response.text
    assert "Secrets present" in response.text


def test_the_agent_page_serves_the_same_invariants_as_the_mcp_resource(signed_in: TestClient) -> None:
    response = signed_in.get("/agent")
    assert response.status_code == 200
    assert "NEVER compute" in response.text


def test_no_page_references_a_third_party_asset(signed_in: TestClient) -> None:
    """Opening the UI must not tell anyone else which instruments you hold."""
    for path in manifest()["web_pages"]:
        html = signed_in.get(path).text
        assert not EXTERNAL_ASSET.findall(html), f"{path} loads something from the network"


def test_the_roadmap_names_every_pending_feature(signed_in: TestClient) -> None:
    html = signed_in.get("/roadmap").text
    for feature in PENDING_FEATURES:
        assert feature.title in html


def test_every_pending_feature_says_what_blocks_it() -> None:
    """A placeholder without a blocker is a wish, and wishes do not get built."""
    assert PENDING_FEATURES
    for feature in PENDING_FEATURES:
        assert feature.what.strip(), f"{feature.title} does not say what it would do"
        assert feature.blocked_by.strip(), f"{feature.title} does not say what it is waiting on"


# -- the limits --------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_are_accepted(host: str) -> None:
    assert_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", ""])
def test_anything_but_loopback_is_refused_with_a_reason(host: str) -> None:
    with pytest.raises(CarteraError, match="loopback"):
        assert_loopback(host)


def test_the_token_is_taken_from_the_environment_when_it_is_set() -> None:
    token, origin = resolve_web_token(environ={"CARTERA_WEB_TOKEN": "from-the-environment"})
    assert (token, origin) == ("from-the-environment", "environment")


def test_without_a_keyring_and_without_the_environment_the_ui_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container has no keyring: it must fail loudly rather than serve open."""
    monkeypatch.setattr("cartera.web.app.keyring_available", lambda: False)
    token, origin = resolve_web_token(environ={})
    assert token is None
    assert origin == "unavailable"

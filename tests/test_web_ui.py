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

from cartera.adapters.openai_compat import AnalysisBackendError
from cartera.adapters.sqlite_store import SqlitePortfolioStore
from cartera.app.analysis import narratable_figures
from cartera.app.services import MarketService, PortfolioService, ProposalService, ReportService
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


# -- analysis ----------------------------------------------------------------


class FakeAnalysisBackend:
    """A backend the test controls, so the page that calls a model never does.

    The real adapter is exercised against a real HTTP server in test_analysis.py;
    what matters here is what the page does with the answer, including a bad one.
    """

    name = "fake-model"

    def __init__(self, answer: str = "", fail_with: Exception | None = None) -> None:
        self.answer = answer
        self.fail_with = fail_with
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if self.fail_with is not None:
            raise self.fail_with
        return self.answer

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def analysis_backend() -> FakeAnalysisBackend:
    return FakeAnalysisBackend(answer="Nothing has been recorded yet, so there is nothing to score.")


@pytest.fixture
def signed_in_analysis(
    settings: Settings,
    quotes: list,
    portfolio: PortfolioSnapshot,
    analysis_backend: FakeAnalysisBackend,
) -> Iterator[TestClient]:
    """The UI as the CLI serves it, with a portfolio in the store and a fake model."""
    store = SqlitePortfolioStore(settings.db_path)
    try:
        PortfolioService(store).import_snapshot(portfolio)
    finally:
        store.close()

    app = build_app(settings, TOKEN, market_source=FakeMarketSource(quotes), analysis_backend=analysis_backend)
    with TestClient(app, follow_redirects=False) as api:
        assert api.post("/login", data={"submitted": TOKEN}).status_code == 303
        yield api


def test_without_a_model_the_page_explains_what_to_configure(signed_in: TestClient) -> None:
    """No backend is the shipped state, and it has to read as a state, not a fault."""
    page = signed_in.get("/analysis")
    assert page.status_code == 200
    assert "No model configured" in page.text
    assert "CARTERA_LLM_BASE_URL" in page.text
    assert "Narrate the current figures" not in page.text, "there is nothing to press without a backend"


def test_pressing_the_button_without_a_backend_says_so_instead_of_failing(signed_in: TestClient) -> None:
    response = signed_in.post("/analysis", data={"focus": ""})
    assert response.status_code == 400
    assert "No analysis backend is configured" in response.text


def test_reading_the_page_does_not_call_the_model(
    signed_in_analysis: TestClient, analysis_backend: FakeAnalysisBackend
) -> None:
    page = signed_in_analysis.get("/analysis")
    assert page.status_code == 200
    assert "fake-model" in page.text
    assert analysis_backend.calls == []


def test_the_answer_is_rendered_after_the_prompt_carries_the_figures(
    signed_in_analysis: TestClient,
    analysis_backend: FakeAnalysisBackend,
) -> None:
    page = signed_in_analysis.post("/analysis", data={"focus": "concentration"})
    assert page.status_code == 200
    assert "Nothing has been recorded yet" in page.text
    assert "Every number in that answer appears" in page.text

    system_prompt, user_prompt = analysis_backend.calls[-1]
    assert "NEVER compute" in system_prompt
    assert "Concentrate on: concentration" in user_prompt
    assert "generated_at" in user_prompt, "the figures must travel with the request"


def test_a_number_the_model_invented_is_named_on_the_page(
    signed_in_analysis: TestClient,
    analysis_backend: FakeAnalysisBackend,
) -> None:
    """The guard, rendered: a figure with no basis is named instead of smoothed over."""
    analysis_backend.answer = "The portfolio is worth 999999 pesos."
    page = signed_in_analysis.post("/analysis", data={"focus": ""})
    assert page.status_code == 200
    assert "Unverified numbers" in page.text
    assert "999999" in page.text


def test_a_backend_that_is_down_is_a_sentence_not_a_crash(
    signed_in_analysis: TestClient,
    analysis_backend: FakeAnalysisBackend,
) -> None:
    analysis_backend.fail_with = AnalysisBackendError("analysis backend at http://127.0.0.1:9/v1 is unreachable")
    page = signed_in_analysis.post("/analysis", data={"focus": ""})
    assert page.status_code == 200
    assert "unreachable" in page.text
    assert "Traceback" not in page.text


def test_a_reload_shows_the_last_answer_without_asking_again(
    signed_in_analysis: TestClient,
    analysis_backend: FakeAnalysisBackend,
) -> None:
    signed_in_analysis.post("/analysis", data={"focus": ""})
    asked = len(analysis_backend.calls)
    page = signed_in_analysis.get("/analysis")
    assert "Nothing has been recorded yet" in page.text
    assert len(analysis_backend.calls) == asked, "reloading the page must not re-run the model"


def test_credentials_in_the_endpoint_are_never_printed(tmp_path: Path, quotes: list) -> None:
    """The page shows where the figures go, and never the key that opens it."""
    settings = load_settings(
        env_file=tmp_path / "nonexistent.env",
        overrides={
            "CARTERA_DATA_DIR": str(tmp_path / "data"),
            "CARTERA_DB_PATH": str(tmp_path / "data" / "cartera.sqlite3"),
            "CARTERA_LLM_BASE_URL": "https://someone:sup3rsecret@llm.example.com:8443/v1",
        },
    )
    app = build_app(settings, TOKEN, market_source=FakeMarketSource(quotes))
    with TestClient(app, follow_redirects=False) as api:
        api.post("/login", data={"submitted": TOKEN})
        page = api.get("/analysis")
    assert page.status_code == 200
    assert "llm.example.com:8443" in page.text
    assert "sup3rsecret" not in page.text
    assert "someone@" not in page.text


async def test_the_payload_comes_from_the_real_report_and_scoreboard(settings: Settings, quotes: list) -> None:
    """One builder for both surfaces: the CLI and the page cannot narrate different numbers."""
    store = SqlitePortfolioStore(settings.db_path)
    market = MarketService(settings, source=FakeMarketSource(quotes))
    try:
        report = await ReportService(settings, store, market).build()
        board = ProposalService(store, market).scoreboard()
        figures = narratable_figures(report, board)
    finally:
        await market.aclose()
        store.close()
    assert set(figures) == {
        "generated_at",
        "snapshot_as_of",
        "fresh",
        "issues",
        "sources",
        "summary",
        "realized_pnl",
        "liquidation_costs",
        "scoreboard",
    }
    assert figures["scoreboard"], "the scoreboard travels with the figures"
    board_figures = figures["scoreboard"]
    assert isinstance(board_figures, dict)
    assert board_figures["hit_rate_pct"] is None, "an empty journal must not become a 0% rate here either"


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
    """A container has no keyring: it must fail loudly rather than serve open.

    Both sources are forced empty here on purpose. This machine's own keyring does
    hold a token, and a test that leans on it being empty passes for the wrong
    reason until the day it does not.
    """
    monkeypatch.setattr("cartera.web.app.keyring_available", lambda: False)
    monkeypatch.setattr("cartera.web.app.get_secret", lambda _key: None)
    token, origin = resolve_web_token(environ={})
    assert token is None
    assert origin == "unavailable"

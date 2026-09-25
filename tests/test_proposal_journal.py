"""The journal end to end: the store's append-only guarantee and the service rules.

The guarantees under test are the ones that make the scoreboard mean something: a
proposal cannot be edited after the fact, its reference price comes from a fresh
quote fetched at record time, and nothing is scored on a horizon it did not claim.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeMarketSource

from cartera.adapters.sqlite_store import SqlitePortfolioStore
from cartera.app.services import MarketService, ProposalService
from cartera.config import load_settings
from cartera.domain.errors import DomainError, StaleQuoteError, UnknownTickerError
from cartera.domain.models import Quote
from cartera.domain.money import Currency
from cartera.domain.proposals import Proposal, ProposalAction, Verdict

pytestmark = pytest.mark.integration

TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def build_settings(tmp_path: Path):
    return load_settings(
        env_file=tmp_path / "nonexistent.env",
        overrides={
            "CARTERA_DATA_DIR": str(tmp_path / "data"),
            "CARTERA_DB_PATH": str(tmp_path / "data" / "cartera.sqlite3"),
            # Stated rather than inherited: the shipped tolerance is 12 hours (for
            # end-of-day runs), and a test that depends on it would start passing
            # or failing for reasons unrelated to the behaviour under test.
            "CARTERA_MAX_QUOTE_AGE_S": "900",
        },
    )


def quote(ticker: str, price: str, age: timedelta = timedelta(minutes=5)) -> Quote:
    return Quote(
        ticker=ticker,
        price=Decimal(price),
        currency=Currency.ARS,
        as_of=datetime.now(TZ) - age,
        source="fake",
    )


def aged_proposal(days: int, **overrides: object) -> Proposal:
    """A proposal recorded ``days`` ago, so its horizon is long behind it."""
    base: dict[str, object] = {
        "proposal_id": f"p-{days}d",
        "created_at": datetime.now(TZ) - timedelta(days=days),
        "ticker": "GGAL",
        "action": ProposalAction.BUY,
        "rationale": "records were tested before the outcome was known",
        "ref_price": Decimal("5000"),
        "currency": Currency.ARS,
        "horizon_days": 30,
    }
    return Proposal(**{**base, **overrides})  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqlitePortfolioStore]:
    instance = SqlitePortfolioStore(tmp_path / "data" / "cartera.sqlite3")
    yield instance
    instance.close()


# -- the append-only guarantee ----------------------------------------------


def test_a_recorded_proposal_cannot_be_edited(store: SqlitePortfolioStore) -> None:
    """The trigger, not the application, is what forbids rewriting the journal."""
    store.add_proposal(aged_proposal(30))
    with pytest.raises(sqlite3.IntegrityError):
        # The raw connection on purpose: the guarantee lives in the schema, so
        # the test has to reach the schema, not a Python method that could be
        # bypassed by any other client of the same database file.
        store._connection.execute("UPDATE proposals SET ticker = 'AAPL'")


def test_a_recorded_proposal_cannot_be_deleted(store: SqlitePortfolioStore) -> None:
    store.add_proposal(aged_proposal(30))
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute("DELETE FROM proposals")


async def test_outcomes_cannot_be_edited_or_deleted(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    store.add_proposal(aged_proposal(31))
    settings = build_settings(tmp_path)
    market = MarketService(settings, source=fake_source([quote("GGAL", "5500")]))
    await ProposalService(store, market).score()
    assert len(store.outcomes()) == 1

    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute("UPDATE proposal_outcomes SET payload = '{}'")
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute("DELETE FROM proposal_outcomes")


def test_the_journal_round_trips_through_the_store(store: SqlitePortfolioStore) -> None:
    proposal = aged_proposal(30)
    store.add_proposal(proposal)
    assert store.proposals() == [proposal]


# -- recording ---------------------------------------------------------------


async def test_recording_takes_the_price_from_the_feed_not_from_the_caller(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    """The entry price is fetched here: a caller cannot choose it after the fact."""
    settings = build_settings(tmp_path)
    market = MarketService(settings, source=fake_source([quote("GGAL", "6280")]))
    result = await ProposalService(store, market).record(
        ticker="ggal",
        action=ProposalAction.BUY,
        rationale="  acumulando en debilidad  ",
        horizon_days=30,
    )

    assert result.proposal["ticker"] == "GGAL"
    assert result.proposal["ref_price"] == "6280"
    assert result.proposal["rationale"] == "acumulando en debilidad"
    assert result.proposal["horizon_days"] == "30"
    assert len(result.audit_hash) == 64
    assert len(store.proposals()) == 1


async def test_recording_appends_to_the_audit_chain(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    settings = build_settings(tmp_path)
    market = MarketService(settings, source=fake_source([quote("GGAL", "6280")]))
    await ProposalService(store, market).record(
        ticker="GGAL",
        action=ProposalAction.BUY,
        rationale="because",
        horizon_days=30,
    )
    assert store.verify_audit_chain() is True
    assert [entry["action"] for entry in store.audit_entries()] == ["proposal.record"]


async def test_recording_refuses_a_view_without_a_reason(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    settings = build_settings(tmp_path)
    market = MarketService(settings, source=fake_source([quote("GGAL", "6280")]))
    with pytest.raises(DomainError, match="rationale"):
        await ProposalService(store, market).record(
            ticker="GGAL",
            action=ProposalAction.BUY,
            rationale="   ",
            horizon_days=30,
        )


async def test_recording_refuses_a_horizon_shorter_than_a_day(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    settings = build_settings(tmp_path)
    market = MarketService(settings, source=fake_source([quote("GGAL", "6280")]))
    with pytest.raises(DomainError, match="horizon"):
        await ProposalService(store, market).record(
            ticker="GGAL",
            action=ProposalAction.BUY,
            rationale="because",
            horizon_days=0,
        )


async def test_recording_an_unquoted_ticker_fails_loudly(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    settings = build_settings(tmp_path)
    market = MarketService(settings, source=fake_source([quote("GGAL", "6280")]))
    with pytest.raises(UnknownTickerError):
        await ProposalService(store, market).record(
            ticker="NOPE",
            action=ProposalAction.BUY,
            rationale="because",
            horizon_days=30,
        )
    assert store.proposals() == []


async def test_recording_at_a_stale_price_is_refused(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    """An entry price nobody could have traded at would poison the scoreboard."""
    settings = build_settings(tmp_path)
    market = MarketService(settings, source=fake_source([quote("GGAL", "6280", age=timedelta(hours=6))]))
    with pytest.raises(StaleQuoteError):
        await ProposalService(store, market).record(
            ticker="GGAL",
            action=ProposalAction.BUY,
            rationale="because",
            horizon_days=30,
        )
    assert store.proposals() == []


# -- scoring -----------------------------------------------------------------


async def test_a_pending_proposal_is_not_scored_and_not_a_miss(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    store.add_proposal(aged_proposal(2, proposal_id="p-young"))
    settings = build_settings(tmp_path)
    market = MarketService(settings, source=fake_source([quote("GGAL", "5500")]))
    result = await ProposalService(store, market).score()

    assert result.scored == 0
    assert result.pending == 1
    assert store.outcomes() == []
    assert ProposalService(store, market).scoreboard().hit_rate_pct is None


async def test_a_due_proposal_is_scored_once_and_the_run_is_idempotent(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    """Repeating the job must not inflate the journal with duplicate outcomes."""
    store.add_proposal(aged_proposal(31, ref_price=Decimal("5000")))
    settings = build_settings(tmp_path)
    market = MarketService(settings, source=fake_source([quote("GGAL", "5500")]))
    service = ProposalService(store, market)

    first = await service.score()
    second = await service.score()

    assert first.scored == 1
    assert first.outcomes[0]["verdict"] == "hit"
    assert first.outcomes[0]["return_pct"] == "10.00"
    assert second.scored == 0
    assert second.already_scored == 1
    assert len(store.outcomes()) == 1


async def test_rescoring_on_purpose_appends_a_second_outcome(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    store.add_proposal(aged_proposal(31))
    settings = build_settings(tmp_path)
    market = MarketService(settings, source=fake_source([quote("GGAL", "4500")]))
    service = ProposalService(store, market)

    await service.score()
    await service.score(rescore=True)

    assert len(store.outcomes()) == 2
    assert {outcome.verdict for outcome in store.outcomes()} == {Verdict.MISS}


async def test_a_ticker_that_left_the_feed_is_counted_not_crashed(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    """A delisted or renamed ticker leaves the proposal pending, loudly counted."""
    store.add_proposal(aged_proposal(31, ticker="GONE"))
    settings = build_settings(tmp_path)
    market = MarketService(settings, source=fake_source([quote("GGAL", "5500")]))
    result = await ProposalService(store, market).score()

    assert result.scored == 0
    assert result.unpriced == 1
    assert store.outcomes() == []


async def test_scoring_refuses_stale_quotes(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    store.add_proposal(aged_proposal(31))
    settings = build_settings(tmp_path)
    market = MarketService(settings, source=fake_source([quote("GGAL", "5500", age=timedelta(hours=6))]))
    with pytest.raises(StaleQuoteError):
        await ProposalService(store, market).score()

    assert store.outcomes() == []


async def test_scoring_never_calls_the_network_when_nothing_is_due(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    """A journal of young proposals must work with the market down."""
    store.add_proposal(aged_proposal(1, proposal_id="p-young"))
    settings = build_settings(tmp_path)
    source = fake_source(error=RuntimeError("the feed is down"))
    market = MarketService(settings, source=source)
    result = await ProposalService(store, market).score()

    assert result.scored == 0
    assert result.pending == 1


async def test_the_end_to_end_scoreboard_reports_the_hit_rate(
    tmp_path: Path,
    store: SqlitePortfolioStore,
    fake_source: Callable[..., FakeMarketSource],
) -> None:
    store.add_proposal(aged_proposal(31, proposal_id="p-ggal", ticker="GGAL", ref_price=Decimal("5000")))
    store.add_proposal(aged_proposal(31, proposal_id="p-aapl", ticker="AAPL", ref_price=Decimal("10000")))
    store.add_proposal(aged_proposal(3, proposal_id="p-young", ticker="KO"))
    settings = build_settings(tmp_path)
    market = MarketService(
        settings, source=fake_source([quote("GGAL", "5500"), quote("AAPL", "9500"), quote("KO", "72")])
    )
    service = ProposalService(store, market)

    await service.score()
    board = service.scoreboard()

    assert board.total_proposals == 3
    assert board.decided == 2
    assert board.hit_rate_pct == Decimal("50.00")
    assert board.by_verdict == {"hit": 1, "miss": 1, "pending": 1}
    assert board.mean_return_pct == Decimal("2.50")
    assert board.best is not None and board.best["ticker"] == "GGAL"
    assert board.worst is not None and board.worst["ticker"] == "AAPL"
    assert [row["verdict"] for row in service.journal()] == ["hit", "miss", "pending"]

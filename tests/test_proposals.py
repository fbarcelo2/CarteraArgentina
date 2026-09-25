"""Scoring rules for the proposal journal. Pure: no store, no network, no clock.

Every expected figure here is arithmetic that can be checked by hand, because a
scoring rule that is only approximately right is worse than none: it launders a
guess into a statistic.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from cartera.domain.money import Currency
from cartera.domain.proposals import (
    Proposal,
    ProposalAction,
    ProposalOutcome,
    Verdict,
    build_scoreboard,
    evaluate,
    latest_outcomes,
)

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
NOW = datetime(2026, 9, 25, 15, 0, tzinfo=TZ)


def make_proposal(**overrides: object) -> Proposal:
    base: dict[str, object] = {
        "proposal_id": "p-1",
        "created_at": NOW - timedelta(days=30),
        "ticker": "GGAL",
        "action": ProposalAction.BUY,
        "rationale": "trades below its five-year multiple",
        "ref_price": Decimal("5000"),
        "currency": Currency.ARS,
        "horizon_days": 30,
    }
    return Proposal(**{**base, **overrides})  # type: ignore[arg-type]


def make_outcome(**overrides: object) -> ProposalOutcome:
    base: dict[str, object] = {
        "proposal_id": "p-1",
        "evaluated_at": NOW,
        "price_then": Decimal("5000"),
        "price_now": Decimal("5500"),
        "days_elapsed": 30,
        "return_pct": Decimal("10.00"),
        "verdict": Verdict.HIT,
    }
    return ProposalOutcome(**{**base, **overrides})  # type: ignore[arg-type]


def test_buy_that_rose_is_a_hit_with_the_hand_checked_return() -> None:
    outcome = evaluate(make_proposal(), Decimal("5500"), NOW)
    assert outcome.verdict is Verdict.HIT
    assert outcome.return_pct == Decimal("10.00")


def test_buy_that_fell_is_a_miss() -> None:
    outcome = evaluate(make_proposal(), Decimal("4700"), NOW)
    assert outcome.verdict is Verdict.MISS
    assert outcome.return_pct == Decimal("-6.00")


def test_sell_that_fell_is_a_hit_because_the_sign_is_inverted() -> None:
    outcome = evaluate(make_proposal(action=ProposalAction.SELL), Decimal("4700"), NOW)
    assert outcome.verdict is Verdict.HIT
    assert outcome.return_pct == Decimal("6.00")


def test_sell_that_rose_is_a_miss() -> None:
    outcome = evaluate(make_proposal(action=ProposalAction.SELL), Decimal("5500"), NOW)
    assert outcome.verdict is Verdict.MISS
    assert outcome.return_pct == Decimal("-10.00")


def test_unchanged_price_is_flat_not_a_hit() -> None:
    outcome = evaluate(make_proposal(), Decimal("5000"), NOW)
    assert outcome.verdict is Verdict.FLAT
    assert outcome.return_pct == Decimal("0.00")


def test_nothing_is_scored_before_its_horizon_elapses() -> None:
    """A 30-day view is not judged on day 3: that would be a different claim."""
    outcome = evaluate(make_proposal(created_at=NOW - timedelta(days=3)), Decimal("5500"), NOW)
    assert outcome.verdict is Verdict.PENDING
    assert outcome.return_pct is None
    assert outcome.detail is not None and "27d" in outcome.detail


def test_a_hold_cannot_be_scored_by_a_price() -> None:
    outcome = evaluate(make_proposal(action=ProposalAction.HOLD), Decimal("5500"), NOW)
    assert outcome.verdict is Verdict.UNSCORABLE
    assert outcome.return_pct is None


def test_a_zero_reference_price_is_unscorable_instead_of_a_division_error() -> None:
    outcome = evaluate(make_proposal(ref_price=Decimal("0")), Decimal("5500"), NOW)
    assert outcome.verdict is Verdict.UNSCORABLE
    assert outcome.return_pct is None


def test_latest_outcome_per_proposal_wins() -> None:
    older = make_outcome(evaluated_at=NOW - timedelta(days=5), verdict=Verdict.PENDING, return_pct=None)
    newer = make_outcome(evaluated_at=NOW, verdict=Verdict.MISS, return_pct=Decimal("-6.00"))
    latest = latest_outcomes([older, newer])
    assert latest["p-1"].verdict is Verdict.MISS


def test_scoreboard_keeps_pending_out_of_the_hit_rate() -> None:
    """The rate divides decided calls only: a pending view is not a wrong one."""
    proposals = [
        make_proposal(proposal_id="p-hit", ticker="GGAL"),
        make_proposal(proposal_id="p-miss", ticker="AAPL"),
        make_proposal(proposal_id="p-pending", ticker="AL30", created_at=NOW - timedelta(days=2)),
    ]
    outcomes = [
        make_outcome(proposal_id="p-hit", verdict=Verdict.HIT, return_pct=Decimal("10.00")),
        make_outcome(proposal_id="p-miss", verdict=Verdict.MISS, return_pct=Decimal("-4.00")),
    ]
    board = build_scoreboard(proposals, outcomes)

    assert board.total_proposals == 3
    assert board.decided == 2
    assert board.by_verdict == {"pending": 1, "hit": 1, "miss": 1}
    assert board.hit_rate_pct == Decimal("50.00")
    assert board.mean_return_pct == Decimal("3.00")
    assert board.best is not None and board.best["ticker"] == "GGAL"
    assert board.worst is not None and board.worst["return_pct"] == "-4.00"


def test_scoreboard_without_outcomes_reports_no_rate_instead_of_zero() -> None:
    """No data is not a 0% hit rate: reporting one would be a false statement."""
    board = build_scoreboard([make_proposal()], [])
    assert board.decided == 0
    assert board.hit_rate_pct is None
    assert board.mean_return_pct is None
    assert board.by_verdict == {"pending": 1}


def test_an_empty_journal_is_an_empty_board() -> None:
    board = build_scoreboard([], [])
    assert board.total_proposals == 0
    assert board.hit_rate_pct is None
    assert board.best is None

"""The proposal journal: what was proposed, and how it actually turned out.

A proposal is recorded **before** its outcome is known, with the price it was made
at, and it can never be edited afterwards. That is the whole point: a tool whose
past statements cannot be scored can always sound insightful, and one that can be
scored either earns that reputation or loses it. The database triggers enforce the
immutability; this module defines what "scored" means.

Nothing here is a prediction, and nothing here is I/O: the rules are pure so the
scoreboard is reproducible from the same inputs.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from cartera.domain.money import Currency

#: Two decimals is the precision every published figure uses.
CENT = Decimal("0.01")


class ProposalAction(StrEnum):
    """What was proposed. There is no size and no order: this is not a trade."""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class Verdict(StrEnum):
    """How a proposal is scored.

    ``PENDING`` and ``UNSCORABLE`` are deliberately distinct from a miss: a
    proposal whose horizon has not elapsed has no outcome yet, and a hold
    expresses no directional decision, so no price can score it. Counting either
    of them as a failure would make the hit rate meaningless.
    """

    PENDING = "pending"
    HIT = "hit"
    MISS = "miss"
    FLAT = "flat"
    UNSCORABLE = "unscorable"


class Proposal(BaseModel):
    """A recorded view, with everything needed to score it later."""

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    created_at: datetime
    ticker: str
    action: ProposalAction
    rationale: str
    ref_price: Decimal
    currency: Currency
    horizon_days: int
    target_price: Decimal | None = None
    source: str = "human"

    @property
    def horizon_ends_at(self) -> datetime:
        return self.created_at + timedelta(days=self.horizon_days)


class ProposalOutcome(BaseModel):
    """A scoring of one proposal at one moment.

    Outcomes accumulate: the same proposal may be scored at 7 days and again at
    30 days, and both rows are kept. The scoreboard reads the latest, but the
    earlier ones are what make the path visible.
    """

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    evaluated_at: datetime
    price_then: Decimal
    price_now: Decimal
    days_elapsed: int
    return_pct: Decimal | None
    verdict: Verdict
    detail: str | None = None


def evaluate(proposal: Proposal, price_now: Decimal, now: datetime) -> ProposalOutcome:
    """Score one proposal against a current price.

    A buy is right when the price rises, a sell when it falls — so the sign is
    inverted for sells rather than storing negative returns and confusing the
    reader. Nothing is scored before its horizon elapses.
    """
    days_elapsed = max((now - proposal.created_at).days, 0)
    outcome_base = {
        "proposal_id": proposal.proposal_id,
        "evaluated_at": now,
        "price_then": proposal.ref_price,
        "price_now": price_now,
        "days_elapsed": days_elapsed,
    }

    if days_elapsed < proposal.horizon_days:
        remaining = proposal.horizon_days - days_elapsed
        return ProposalOutcome(
            **outcome_base,
            return_pct=None,
            verdict=Verdict.PENDING,
            detail=f"horizon of {proposal.horizon_days}d ends in {remaining}d",
        )

    if proposal.action is ProposalAction.HOLD:
        return ProposalOutcome(
            **outcome_base,
            return_pct=None,
            verdict=Verdict.UNSCORABLE,
            detail="a hold expresses no directional decision, so no price can score it",
        )

    if proposal.ref_price <= 0:
        return ProposalOutcome(
            **outcome_base,
            return_pct=None,
            verdict=Verdict.UNSCORABLE,
            detail=f"reference price {proposal.ref_price} is not positive, so a return is undefined",
        )

    move = (price_now / proposal.ref_price) - Decimal(1)
    if proposal.action is ProposalAction.SELL:
        move = -move
    return_pct = (move * 100).quantize(CENT)

    if return_pct > 0:
        verdict, detail = Verdict.HIT, None
    elif return_pct < 0:
        verdict, detail = Verdict.MISS, None
    else:
        verdict, detail = Verdict.FLAT, "price unchanged over the horizon"

    return ProposalOutcome(**outcome_base, return_pct=return_pct, verdict=verdict, detail=detail)


def latest_outcomes(outcomes: list[ProposalOutcome]) -> dict[str, ProposalOutcome]:
    """The most recent outcome per proposal, by evaluation time."""
    latest: dict[str, ProposalOutcome] = {}
    for outcome in outcomes:
        current = latest.get(outcome.proposal_id)
        if current is None or outcome.evaluated_at >= current.evaluated_at:
            latest[outcome.proposal_id] = outcome
    return latest


class Scoreboard(BaseModel):
    """Aggregate of the journal.

    ``hit_rate_pct`` divides hits by **decided** proposals only (hit, miss or
    flat). Pending and unscorable rows are reported in the counts and excluded
    from the rate, because a rate that moves when nobody made a call is not a
    measure of anything.
    """

    total_proposals: int = 0
    decided: int = 0
    by_verdict: dict[str, int] = Field(default_factory=dict)
    by_action: dict[str, dict[str, str]] = Field(default_factory=dict)
    hit_rate_pct: Decimal | None = None
    mean_return_pct: Decimal | None = None
    best: dict[str, str] | None = None
    worst: dict[str, str] | None = None


def build_scoreboard(proposals: list[Proposal], outcomes: list[ProposalOutcome]) -> Scoreboard:
    """Aggregate proposals and their latest outcomes into a board."""
    by_id = {proposal.proposal_id: proposal for proposal in proposals}
    latest = latest_outcomes(outcomes)

    by_verdict: dict[str, int] = {verdict.value: 0 for verdict in Verdict}
    by_action: dict[str, dict[str, str]] = {}
    returns: list[tuple[str, Decimal]] = []

    for proposal in proposals:
        action_stats = by_action.setdefault(
            proposal.action.value,
            {"proposals": "0", "hits": "0", "misses": "0", "pending": "0", "unscorable": "0"},
        )
        action_stats["proposals"] = str(int(action_stats["proposals"]) + 1)

        outcome = latest.get(proposal.proposal_id)
        verdict_value = (outcome.verdict if outcome else Verdict.PENDING).value
        by_verdict[verdict_value] = by_verdict.get(verdict_value, 0) + 1

        if outcome is None or outcome.verdict is Verdict.PENDING:
            action_stats["pending"] = str(int(action_stats["pending"]) + 1)
        elif outcome.verdict is Verdict.UNSCORABLE:
            action_stats["unscorable"] = str(int(action_stats["unscorable"]) + 1)
        elif outcome.verdict is Verdict.HIT:
            action_stats["hits"] = str(int(action_stats["hits"]) + 1)
        else:
            action_stats["misses"] = str(int(action_stats["misses"]) + 1)

        if outcome is not None and outcome.return_pct is not None:
            returns.append((proposal.proposal_id, outcome.return_pct))

    decided = sum(by_verdict.get(verdict.value, 0) for verdict in (Verdict.HIT, Verdict.MISS, Verdict.FLAT))
    hits = by_verdict.get(Verdict.HIT.value, 0)
    hit_rate = ((Decimal(hits) / Decimal(decided)) * 100).quantize(CENT) if decided else None
    # Guarded, not conditional: dividing by an empty list raises out of Decimal's
    # context, so the "no data" case has to skip the arithmetic entirely.
    mean_return = (
        (sum((value for _, value in returns), Decimal(0)) / Decimal(len(returns))).quantize(CENT) if returns else None
    )

    def describe(proposal_id: str, value: Decimal) -> dict[str, str]:
        proposal = by_id.get(proposal_id)
        return {
            "proposal_id": proposal_id,
            "ticker": proposal.ticker if proposal else "?",
            "action": proposal.action.value if proposal else "?",
            "return_pct": str(value),
        }

    ordered = sorted(returns, key=lambda pair: pair[1])
    return Scoreboard(
        total_proposals=len(proposals),
        decided=decided,
        by_verdict={key: value for key, value in by_verdict.items() if value},
        by_action=by_action,
        hit_rate_pct=hit_rate,
        mean_return_pct=mean_return if returns else None,
        best=describe(*ordered[-1]) if ordered else None,
        worst=describe(*ordered[0]) if ordered else None,
    )

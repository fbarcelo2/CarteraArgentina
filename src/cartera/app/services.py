"""Use cases. Both front-ends (CLI, MCP) call these and only these.

Keeping the logic here is what prevents the classic decay of a tool with several
interfaces: two implementations of the same rule that disagree over time.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from cartera.adapters.byma_open import UNIVERSES, BymaOpenDataSource, default_universes
from cartera.config import Settings
from cartera.domain.errors import (
    CarteraError,
    DomainError,
    DuplicateTransactionError,
    SourceUnavailableError,
    UnknownTickerError,
)
from cartera.domain.metrics import liquidation_costs, realized_pnl, valuate
from cartera.domain.models import PortfolioSnapshot, Quote, Settlement
from cartera.domain.money import Currency
from cartera.domain.proposals import (
    Proposal,
    ProposalAction,
    ProposalOutcome,
    Scoreboard,
    Verdict,
    build_scoreboard,
    evaluate,
    latest_outcomes,
)
from cartera.domain.rules import (
    ValidationIssue,
    assert_fresh,
    check_concentration,
    check_freshness,
    merge_reports,
)
from cartera.ports.market_data import MarketDataSource, MarketSession, Universe
from cartera.ports.store import PortfolioStore, ProposalJournal

#: When a ticker is quoted for several settlement windows, prefer the most
#: immediate one, then the longer ones. Deterministic, and documented.
SETTLEMENT_PREFERENCE: tuple[Settlement, ...] = (Settlement.CI, Settlement.T24, Settlement.T48)


def select_quotes(
    quotes: list[Quote],
    preference: tuple[Settlement, ...] = SETTLEMENT_PREFERENCE,
) -> list[Quote]:
    """One quote per ticker, chosen by settlement preference.

    A source that reports no settlement is treated as the most immediate.
    """
    ranking = {settlement: index for index, settlement in enumerate(preference)}
    best: dict[str, Quote] = {}
    for quote in quotes:
        rank = -1 if quote.settlement is None else ranking.get(quote.settlement, len(preference))
        current = best.get(quote.ticker)
        if current is None:
            best[quote.ticker] = quote
            continue
        current_rank = -1 if current.settlement is None else ranking.get(current.settlement, len(preference))
        if rank < current_rank:
            best[quote.ticker] = quote
    return [best[ticker] for ticker in sorted(best)]


class MarketQuotesResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    quotes: list[Quote]
    sources: list[str]
    session: MarketSession | None = None


class PortfolioImportResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: int
    as_of: str
    new_lots: int
    known_lots: int
    cash: dict[str, str]
    audit_hash: str


class PortfolioSummaryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    as_of: str | None
    label: str | None
    snapshot_source: str | None = None
    lots: int
    positions: list[dict[str, str]] = Field(default_factory=list)
    cash: dict[str, str] = Field(default_factory=dict)


class ReportResult(BaseModel):
    """Output of the report use case.

    ``summary`` is ``None`` whenever a gate failed. That is the point: an empty
    report with reasons beats a complete report built on stale prices.
    """

    model_config = ConfigDict(frozen=True)

    generated_at: str
    fresh: bool
    issues: list[dict[str, str]] = Field(default_factory=list)
    summary: dict[str, object] | None = None
    realized_pnl: dict[str, str] = Field(default_factory=dict)
    liquidation_costs: dict[str, str] = Field(default_factory=dict)
    snapshot_as_of: str | None = None
    sources: list[str] = Field(default_factory=list)


class MarketService:
    """Read-only market data access."""

    def __init__(self, settings: Settings, source: MarketDataSource | None = None) -> None:
        self.settings = settings
        self.source = source or BymaOpenDataSource(timeout_seconds=settings.http_timeout_seconds)

    async def quotes(
        self,
        tickers: list[str],
        universe_keys: list[str] | None = None,
    ) -> MarketQuotesResult:
        universes = self._universes(universe_keys)
        raw = await self.source.fetch_quotes(tickers, universes, settlements=SETTLEMENT_PREFERENCE)
        return MarketQuotesResult(quotes=select_quotes(raw), sources=[self.source.name])

    @staticmethod
    def _universes(universe_keys: list[str] | None) -> list[Universe]:
        if not universe_keys:
            return default_universes()
        unknown = [key for key in universe_keys if key not in UNIVERSES]
        if unknown:
            raise CarteraError(f"unknown universe(s): {', '.join(sorted(unknown))}")
        return [UNIVERSES[key] for key in universe_keys]

    async def session(self) -> MarketSession:
        return await self.source.fetch_session()

    async def aclose(self) -> None:
        await self.source.aclose()


class PortfolioService:
    """Import and inspect the portfolio snapshot in the append-only ledger."""

    def __init__(self, store: PortfolioStore) -> None:
        self.store = store

    def import_snapshot(self, snapshot: PortfolioSnapshot, label: str | None = None) -> PortfolioImportResult:
        """Append a snapshot. Re-importing a known lot is a no-op, not an error.

        Idempotency matters here: users re-export the same snapshot while
        iterating, and a half-applied import is worse than a rejected one.
        """
        stored = snapshot.model_copy(update={"label": label}) if label else snapshot
        new_lots = 0
        known_lots = 0
        for lot in stored.lots:
            try:
                self.store.add_lot(lot)
            except DuplicateTransactionError:
                known_lots += 1
            else:
                new_lots += 1
        snapshot_id = self.store.save_snapshot(stored)
        audit_hash = self.store.record_audit(
            "portfolio.import",
            {
                "snapshot_id": snapshot_id,
                "as_of": stored.as_of.isoformat(),
                "new_lots": new_lots,
                "known_lots": known_lots,
                "label": label,
            },
        )
        return PortfolioImportResult(
            snapshot_id=snapshot_id,
            as_of=stored.as_of.isoformat(),
            new_lots=new_lots,
            known_lots=known_lots,
            cash={currency.value: str(amount) for currency, amount in stored.cash.items()},
            audit_hash=audit_hash,
        )

    def import_file(self, path: Path, label: str | None = None) -> PortfolioImportResult:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        snapshot = PortfolioSnapshot.model_validate(payload)
        return self.import_snapshot(snapshot, label)

    def summary(self) -> PortfolioSummaryResult | None:
        snapshot = self.store.latest_snapshot()
        if snapshot is None:
            return None
        return PortfolioSummaryResult(
            as_of=snapshot.as_of.isoformat(),
            label=snapshot.label,
            snapshot_source=snapshot.source,
            lots=len(snapshot.lots),
            positions=[
                {
                    "ticker": position.ticker,
                    "asset_type": position.asset_type.value,
                    "quantity": str(position.quantity),
                    "cost_basis": str(position.cost_basis),
                    "currency": position.currency.value,
                }
                for position in snapshot.positions()
            ],
            cash={currency.value: str(amount) for currency, amount in snapshot.cash.items()},
        )


class ReportService:
    """Build the gated, deterministic report."""

    def __init__(self, settings: Settings, store: PortfolioStore, market: MarketService) -> None:
        self.settings = settings
        self.store = store
        self.market = market

    async def build(self) -> ReportResult:
        generated_at = datetime.now(self.settings.timezone).isoformat()
        snapshot = self.store.latest_snapshot()
        if snapshot is None:
            return ReportResult(
                generated_at=generated_at,
                fresh=False,
                issues=[
                    self._issue("no_snapshot", "no portfolio snapshot has been imported yet", "error"),
                ],
            )

        tickers = [position.ticker for position in snapshot.positions()]
        try:
            market = await self.market.quotes(tickers)
        except SourceUnavailableError as exc:
            return ReportResult(
                generated_at=generated_at,
                fresh=False,
                snapshot_as_of=snapshot.as_of.isoformat(),
                issues=[self._issue("source_unavailable", str(exc), "error")],
            )

        now = datetime.now(self.settings.timezone)
        freshness = check_freshness(market.quotes, now, self.settings.max_quote_age_seconds)
        if not freshness.ok:
            # Refuse to publish numbers rather than publish wrong ones.
            return ReportResult(
                generated_at=generated_at,
                fresh=False,
                snapshot_as_of=snapshot.as_of.isoformat(),
                sources=market.sources,
                issues=[self._issue(issue.code, issue.detail, issue.severity) for issue in freshness.issues],
            )

        summary = valuate(snapshot, market.quotes, self.settings.commission_pct or None)
        concentration = check_concentration(
            {
                ticker: weight
                for valuation in summary.valuations.values()
                for ticker, weight in valuation.weights.items()
            },
        )
        report = merge_reports(freshness, concentration)
        realized = realized_pnl(self.store.transactions(), self.store.lots())
        costs = liquidation_costs(summary.positions, market.quotes, self.settings.commission_pct or None)

        self.store.record_audit(
            "report.generate",
            {
                "snapshot_as_of": snapshot.as_of.isoformat(),
                "quotes": len(market.quotes),
                "warnings": len(report.warnings),
            },
        )

        return ReportResult(
            generated_at=generated_at,
            fresh=True,
            snapshot_as_of=snapshot.as_of.isoformat(),
            sources=market.sources,
            issues=[self._issue(issue.code, issue.detail, issue.severity) for issue in report.issues],
            summary={
                "valuations": {
                    currency.value: {
                        "market_value": str(valuation.market_value),
                        "cost_basis": str(valuation.cost_basis),
                        "unrealized_pnl": str(valuation.unrealized_pnl),
                        "cash": str(valuation.cash),
                        "total": str(valuation.total),
                        "weights": {ticker: str(weight) for ticker, weight in valuation.weights.items()},
                    }
                    for currency, valuation in summary.valuations.items()
                },
                "positions": [
                    {
                        "ticker": position.ticker,
                        "quantity": str(position.quantity),
                        "cost_basis": str(position.cost_basis),
                        "currency": position.currency.value,
                        "lots": str(position.lot_count),
                    }
                    for position in summary.positions
                ],
                "unpriced_tickers": summary.unpriced_tickers,
                "priced_positions": str(summary.priced_positions),
            },
            realized_pnl={currency.value: str(amount) for currency, amount in realized.items()},
            liquidation_costs={currency.value: str(amount) for currency, amount in costs.items()},
        )

    @staticmethod
    def _issue(code: str, detail: str, severity: str) -> dict[str, str]:
        return {"code": code, "detail": detail, "severity": severity}


#: A verdict that means a call was made and the market answered it.
DECIDED_VERDICTS: tuple[Verdict, ...] = (Verdict.HIT, Verdict.MISS, Verdict.FLAT)


class ProposalRecordResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal: dict[str, str]
    audit_hash: str


class ProposalScoreResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    scored: int = 0
    already_scored: int = 0
    pending: int = 0
    unscorable: int = 0
    unpriced: int = 0
    outcomes: list[dict[str, str]] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class ProposalService:
    """Record views before the fact, score them after it.

    Two rules make the scoreboard worth reading. The reference price comes from a
    fresh quote fetched *inside* this service, never from the caller, so an entry
    price cannot be picked after seeing where the market went. And nothing is
    scored before its horizon elapses, so a proposal cannot be judged on a shorter
    window than the one it claimed.
    """

    def __init__(self, store: ProposalJournal, market: MarketService) -> None:
        self.store = store
        self.market = market

    async def record(
        self,
        *,
        ticker: str,
        action: ProposalAction,
        rationale: str,
        horizon_days: int,
        target_price: Decimal | None = None,
        source: str = "human",
    ) -> ProposalRecordResult:
        """Record a view at the price it is being made at."""
        normalized = ticker.strip().upper()
        if not normalized:
            raise DomainError("a proposal needs a ticker")
        if horizon_days < 1:
            raise DomainError(f"horizon must be at least 1 day, got {horizon_days}")
        if not rationale.strip():
            raise DomainError("a proposal needs a rationale: an unexplained view cannot be reviewed later")

        now = datetime.now(self.market.settings.timezone)
        result = await self.market.quotes([normalized])
        quote = {item.ticker: item for item in result.quotes}.get(normalized)
        if quote is None:
            source_name = result.sources[0] if result.sources else "market source"
            raise UnknownTickerError([normalized], source_name)
        assert_fresh([quote], now, self.market.settings.max_quote_age_seconds)

        proposal = Proposal(
            proposal_id=f"{normalized}-{action.value}-{now:%Y%m%dT%H%M%S}-{uuid4().hex[:6]}",
            created_at=now,
            ticker=normalized,
            action=action,
            rationale=rationale.strip(),
            ref_price=quote.price,
            currency=quote.currency,
            horizon_days=horizon_days,
            target_price=target_price,
            source=source,
        )
        self.store.add_proposal(proposal)
        audit_hash = self.store.record_audit("proposal.record", _proposal_audit_payload(proposal))
        return ProposalRecordResult(proposal=_proposal_payload(proposal), audit_hash=audit_hash)

    async def score(self, *, rescore: bool = False) -> ProposalScoreResult:
        """Score every proposal whose horizon has elapsed.

        A proposal is scored once. Repeating the run does not append duplicates,
        because that would let a thin journal look like a rich one; ``rescore``
        exists for scoring the same proposal again on purpose, at a later point.
        """
        proposals = self.store.proposals()
        if not proposals:
            return ProposalScoreResult()

        now = datetime.now(self.market.settings.timezone)
        latest = latest_outcomes(self.store.outcomes())
        already_scored = 0
        pending = 0
        due: list[Proposal] = []

        for proposal in proposals:
            outcome = latest.get(proposal.proposal_id)
            if outcome is not None and outcome.verdict in DECIDED_VERDICTS and not rescore:
                already_scored += 1
                continue
            if (now - proposal.created_at).days < proposal.horizon_days:
                pending += 1
                continue
            due.append(proposal)

        if not due:
            return ProposalScoreResult(already_scored=already_scored, pending=pending)

        tickers = sorted({proposal.ticker for proposal in due})
        quotes_result = await self.market.quotes(tickers)
        by_ticker = {quote.ticker: quote for quote in quotes_result.quotes}
        present = [by_ticker[ticker] for ticker in tickers if ticker in by_ticker]
        assert_fresh(present, now, self.market.settings.max_quote_age_seconds)

        scored: list[dict[str, str]] = []
        unscorable = 0
        unpriced = 0
        for proposal in due:
            quote = by_ticker.get(proposal.ticker)
            if quote is None:
                unpriced += 1
                continue
            outcome = evaluate(proposal, quote.price, now)
            self.store.add_outcome(outcome)
            if outcome.verdict is Verdict.UNSCORABLE:
                unscorable += 1
            scored.append(_outcome_payload(outcome))

        self.store.record_audit(
            "proposal.score",
            {
                "scored": str(len(scored)),
                "already_scored": str(already_scored),
                "pending": str(pending),
                "unpriced": str(unpriced),
                "rescore": str(rescore),
            },
        )
        return ProposalScoreResult(
            scored=len(scored),
            already_scored=already_scored,
            pending=pending,
            unscorable=unscorable,
            unpriced=unpriced,
            outcomes=scored,
            sources=quotes_result.sources,
        )

    def journal(self) -> list[dict[str, str]]:
        """Every proposal with its latest outcome, for the front-ends."""
        latest = latest_outcomes(self.store.outcomes())
        rows: list[dict[str, str]] = []
        for proposal in self.store.proposals():
            outcome = latest.get(proposal.proposal_id)
            rows.append(
                {
                    **_proposal_payload(proposal),
                    "verdict": outcome.verdict.value if outcome is not None else Verdict.PENDING.value,
                    "return_pct": ("" if outcome is None or outcome.return_pct is None else str(outcome.return_pct)),
                    "evaluated_at": outcome.evaluated_at.isoformat() if outcome is not None else "",
                },
            )
        return rows

    def scoreboard(self) -> Scoreboard:
        """Aggregate the journal. Reads what was already scored; no network."""
        return build_scoreboard(self.store.proposals(), self.store.outcomes())


def _proposal_payload(proposal: Proposal) -> dict[str, str]:
    return {
        "proposal_id": proposal.proposal_id,
        "created_at": proposal.created_at.isoformat(),
        "horizon_ends_at": proposal.horizon_ends_at.isoformat(),
        "ticker": proposal.ticker,
        "action": proposal.action.value,
        "rationale": proposal.rationale,
        "ref_price": str(proposal.ref_price),
        "currency": proposal.currency.value,
        "horizon_days": str(proposal.horizon_days),
        "target_price": "" if proposal.target_price is None else str(proposal.target_price),
        "source": proposal.source,
    }


def _outcome_payload(outcome: ProposalOutcome) -> dict[str, str]:
    return {
        "proposal_id": outcome.proposal_id,
        "evaluated_at": outcome.evaluated_at.isoformat(),
        "price_then": str(outcome.price_then),
        "price_now": str(outcome.price_now),
        "days_elapsed": str(outcome.days_elapsed),
        "return_pct": "" if outcome.return_pct is None else str(outcome.return_pct),
        "verdict": outcome.verdict.value,
        "detail": outcome.detail or "",
    }


def _proposal_audit_payload(proposal: Proposal) -> dict[str, object]:
    return {
        "proposal_id": proposal.proposal_id,
        "ticker": proposal.ticker,
        "action": proposal.action.value,
        "ref_price": str(proposal.ref_price),
        "currency": proposal.currency.value,
        "horizon_days": proposal.horizon_days,
        "source": proposal.source,
    }


def decimal_or_none(value: object) -> Decimal | None:
    """Small helper for front-ends that accept numbers as strings."""
    if value in (None, ""):
        return None
    return Decimal(str(value))


def currency_key(currency: Currency) -> str:
    return currency.value


__all__ = [
    "DECIDED_VERDICTS",
    "SETTLEMENT_PREFERENCE",
    "MarketQuotesResult",
    "MarketService",
    "PortfolioImportResult",
    "PortfolioService",
    "ProposalRecordResult",
    "ProposalScoreResult",
    "ProposalService",
    "ReportResult",
    "ReportService",
    "ValidationIssue",
    "currency_key",
    "decimal_or_none",
    "select_quotes",
]

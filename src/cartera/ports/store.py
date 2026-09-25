"""Ports for persistence and analysis backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cartera.domain.models import Lot, PortfolioSnapshot, Transaction
from cartera.domain.proposals import Proposal, ProposalOutcome


@runtime_checkable
class PortfolioStore(Protocol):
    """Append-only ledger of transactions and lots, plus snapshots.

    The word "append-only" is load-bearing: history is the only way to score a
    past proposal, so no API here overwrites a row.
    """

    def append_transaction(self, transaction: Transaction) -> None: ...

    def transactions(self) -> list[Transaction]: ...

    def add_lot(self, lot: Lot) -> None: ...

    def lots(self) -> list[Lot]: ...

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> int: ...

    def latest_snapshot(self) -> PortfolioSnapshot | None: ...

    def record_audit(self, action: str, payload: dict[str, object]) -> str:
        """Append an audit entry and return its hash."""
        ...

    def verify_audit_chain(self) -> bool: ...


@runtime_checkable
class ProposalJournal(Protocol):
    """Append-only journal of proposals and their scored outcomes.

    ``record_audit`` is part of the contract on purpose: recording a view is a
    decision, and decisions are auditable. Nothing here can update or delete a
    row — the database triggers reject that — so a scoreboard built on it cannot
    be quietly revised.
    """

    def add_proposal(self, proposal: Proposal) -> None: ...

    def proposals(self) -> list[Proposal]: ...

    def add_outcome(self, outcome: ProposalOutcome) -> None: ...

    def outcomes(self) -> list[ProposalOutcome]: ...

    def record_audit(self, action: str, payload: dict[str, object]) -> str: ...


@runtime_checkable
class AnalysisBackend(Protocol):
    """A text-generation backend used to narrate numbers computed elsewhere."""

    name: str

    async def complete(self, system_prompt: str, user_prompt: str) -> str: ...

    async def aclose(self) -> None: ...

"""SQLite storage: append-only ledger, snapshots and a hash-chained audit log.

Two invariants are enforced by the database itself, not by convention:

1. **Append-only.** Triggers abort any ``UPDATE`` or ``DELETE`` on the ledger,
   snapshot and audit tables. History is what makes a past proposal scoreable,
   so nothing is allowed to rewrite it — including this code.
2. **Tamper-evident audit.** Each audit row stores the hash of the previous row,
   so removing or editing an entry breaks ``verify_audit_chain``.

Nothing here is multi-tenant: one installation is one portfolio. Tenant columns
from a SaaS template would be dead weight, so they are deliberately absent.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from cartera.domain.errors import DuplicateTransactionError
from cartera.domain.models import AssetType, Lot, PortfolioSnapshot, Settlement, Transaction, TxKind
from cartera.domain.money import Currency, as_decimal
from cartera.domain.proposals import Proposal, ProposalOutcome

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS transactions (
    tx_id        TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    currency     TEXT NOT NULL,
    amount       TEXT NOT NULL,
    ticker       TEXT,
    asset_type   TEXT,
    lot_id       TEXT,
    quantity     TEXT,
    price        TEXT,
    fees         TEXT NOT NULL DEFAULT '0',
    note         TEXT,
    recorded_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lots (
    lot_id       TEXT PRIMARY KEY,
    ticker       TEXT NOT NULL,
    asset_type   TEXT NOT NULL,
    quantity     TEXT NOT NULL,
    unit_price   TEXT NOT NULL,
    currency     TEXT NOT NULL,
    settlement   TEXT NOT NULL,
    opened_at    TEXT NOT NULL,
    fees         TEXT NOT NULL DEFAULT '0',
    source       TEXT NOT NULL DEFAULT 'snapshot',
    recorded_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of        TEXT NOT NULL,
    label        TEXT,
    source       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    entry_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at  TEXT NOT NULL,
    action       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    prev_hash    TEXT,
    hash         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transactions_occurred_at ON transactions(occurred_at);
CREATE INDEX IF NOT EXISTS idx_lots_ticker ON lots(ticker);
CREATE INDEX IF NOT EXISTS idx_audit_entry ON audit_log(entry_id);

CREATE TABLE IF NOT EXISTS proposals (
    proposal_id  TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    action       TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    payload      TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposal_outcomes (
    outcome_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id  TEXT NOT NULL REFERENCES proposals(proposal_id),
    evaluated_at TEXT NOT NULL,
    payload      TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proposals_ticker ON proposals(ticker);
CREATE INDEX IF NOT EXISTS idx_proposals_created ON proposals(created_at);
CREATE INDEX IF NOT EXISTS idx_outcomes_proposal ON proposal_outcomes(proposal_id);

CREATE TRIGGER IF NOT EXISTS proposals_no_update
BEFORE UPDATE ON proposals
BEGIN
    SELECT RAISE(ABORT, 'proposals is append-only: record a new proposal instead of editing one');
END;

CREATE TRIGGER IF NOT EXISTS proposals_no_delete
BEFORE DELETE ON proposals
BEGIN
    SELECT RAISE(ABORT, 'proposals is append-only: the journal cannot be rewritten');
END;

CREATE TRIGGER IF NOT EXISTS proposal_outcomes_no_update
BEFORE UPDATE ON proposal_outcomes
BEGIN
    SELECT RAISE(ABORT, 'proposal_outcomes is append-only: rescore instead of editing');
END;

CREATE TRIGGER IF NOT EXISTS proposal_outcomes_no_delete
BEFORE DELETE ON proposal_outcomes
BEGIN
    SELECT RAISE(ABORT, 'proposal_outcomes is append-only');
END;

CREATE TRIGGER IF NOT EXISTS transactions_no_update
BEFORE UPDATE ON transactions
BEGIN SELECT RAISE(ABORT, 'append-only ledger: transactions cannot be updated'); END;

CREATE TRIGGER IF NOT EXISTS transactions_no_delete
BEFORE DELETE ON transactions
BEGIN SELECT RAISE(ABORT, 'append-only ledger: transactions cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS lots_no_update
BEFORE UPDATE ON lots
BEGIN SELECT RAISE(ABORT, 'append-only ledger: lots cannot be updated'); END;

CREATE TRIGGER IF NOT EXISTS lots_no_delete
BEFORE DELETE ON lots
BEGIN SELECT RAISE(ABORT, 'append-only ledger: lots cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS snapshots_no_update
BEFORE UPDATE ON snapshots
BEGIN SELECT RAISE(ABORT, 'append-only ledger: snapshots cannot be updated'); END;

CREATE TRIGGER IF NOT EXISTS snapshots_no_delete
BEFORE DELETE ON snapshots
BEGIN SELECT RAISE(ABORT, 'append-only ledger: snapshots cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS audit_no_update
BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'append-only ledger: audit entries cannot be updated'); END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete
BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'append-only ledger: audit entries cannot be deleted'); END;
"""

GENESIS_HASH = "0" * 64


def _now() -> str:
    return datetime.now().astimezone().isoformat()


class SqlitePortfolioStore:
    """Implements ``cartera.ports.PortfolioStore`` on a single SQLite file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)

    def close(self) -> None:
        self._connection.close()

    # -- transactions --------------------------------------------------------

    def append_transaction(self, transaction: Transaction) -> None:
        try:
            self._connection.execute(
                """
                INSERT INTO transactions (tx_id, kind, occurred_at, currency, amount, ticker,
                                          asset_type, lot_id, quantity, price, fees, note, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transaction.tx_id,
                    transaction.kind.value,
                    transaction.occurred_at.isoformat(),
                    transaction.currency.value,
                    str(transaction.amount),
                    transaction.ticker,
                    transaction.asset_type.value if transaction.asset_type else None,
                    transaction.lot_id,
                    None if transaction.quantity is None else str(transaction.quantity),
                    None if transaction.price is None else str(transaction.price),
                    str(transaction.fees),
                    transaction.note,
                    _now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateTransactionError(f"transaction {transaction.tx_id} already exists") from exc

    def transactions(self) -> list[Transaction]:
        rows = self._connection.execute("SELECT * FROM transactions ORDER BY occurred_at, tx_id").fetchall()
        return [
            Transaction(
                tx_id=row["tx_id"],
                kind=TxKind(row["kind"]),
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                currency=Currency(row["currency"]),
                amount=as_decimal(row["amount"]),
                ticker=row["ticker"],
                asset_type=AssetType(row["asset_type"]) if row["asset_type"] else None,
                lot_id=row["lot_id"],
                quantity=None if row["quantity"] is None else as_decimal(row["quantity"]),
                price=None if row["price"] is None else as_decimal(row["price"]),
                fees=as_decimal(row["fees"]),
                note=row["note"],
            )
            for row in rows
        ]

    # -- lots ----------------------------------------------------------------

    def add_lot(self, lot: Lot) -> None:
        try:
            self._connection.execute(
                """
                INSERT INTO lots (lot_id, ticker, asset_type, quantity, unit_price, currency,
                                  settlement, opened_at, fees, source, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lot.lot_id,
                    lot.ticker,
                    lot.asset_type.value,
                    str(lot.quantity),
                    str(lot.unit_price),
                    lot.currency.value,
                    lot.settlement.value,
                    lot.opened_at.isoformat(),
                    str(lot.fees),
                    lot.source,
                    _now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateTransactionError(f"lot {lot.lot_id} already exists") from exc

    def lots(self) -> list[Lot]:
        rows = self._connection.execute("SELECT * FROM lots ORDER BY opened_at, lot_id").fetchall()
        return [
            Lot(
                lot_id=row["lot_id"],
                ticker=row["ticker"],
                asset_type=AssetType(row["asset_type"]),
                quantity=as_decimal(row["quantity"]),
                unit_price=as_decimal(row["unit_price"]),
                currency=Currency(row["currency"]),
                settlement=Settlement(row["settlement"]),
                opened_at=datetime.fromisoformat(row["opened_at"]),
                fees=as_decimal(row["fees"]),
                source=row["source"],
            )
            for row in rows
        ]

    # -- snapshots -----------------------------------------------------------

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> int:
        cursor = self._connection.execute(
            "INSERT INTO snapshots (as_of, label, source, payload, recorded_at) VALUES (?, ?, ?, ?, ?)",
            (
                snapshot.as_of.isoformat(),
                snapshot.label,
                snapshot.source,
                snapshot.model_dump_json(),
                _now(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def latest_snapshot(self) -> PortfolioSnapshot | None:
        row = self._connection.execute(
            "SELECT payload FROM snapshots ORDER BY snapshot_id DESC LIMIT 1",
        ).fetchone()
        if row is None:
            return None
        return PortfolioSnapshot.model_validate_json(row["payload"])

    def snapshot_count(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS total FROM snapshots").fetchone()
        return int(row["total"])

    # -- audit ---------------------------------------------------------------

    def _last_hash(self) -> str:
        row = self._connection.execute("SELECT hash FROM audit_log ORDER BY entry_id DESC LIMIT 1").fetchone()
        return str(row["hash"]) if row else GENESIS_HASH

    @staticmethod
    def _digest(prev_hash: str, occurred_at: str, action: str, payload: dict[str, object]) -> str:
        blob = json.dumps(
            {"prev": prev_hash, "at": occurred_at, "action": action, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def record_audit(self, action: str, payload: dict[str, object]) -> str:
        occurred_at = _now()
        prev_hash = self._last_hash()
        digest = self._digest(prev_hash, occurred_at, action, payload)
        self._connection.execute(
            "INSERT INTO audit_log (occurred_at, action, payload, prev_hash, hash) VALUES (?, ?, ?, ?, ?)",
            (
                occurred_at,
                action,
                json.dumps(payload, sort_keys=True, default=str),
                prev_hash,
                digest,
            ),
        )
        return digest

    def audit_entries(self) -> list[dict[str, object]]:
        rows = self._connection.execute(
            "SELECT entry_id, occurred_at, action, payload, prev_hash, hash FROM audit_log ORDER BY entry_id",
        ).fetchall()
        return [dict(row) for row in rows]

    def verify_audit_chain(self) -> bool:
        """Recompute every digest; any edit or removal breaks the chain."""
        prev_hash = GENESIS_HASH
        for row in self.audit_entries():
            payload = json.loads(str(row["payload"]))
            expected = self._digest(prev_hash, str(row["occurred_at"]), str(row["action"]), payload)
            if expected != row["hash"] or row["prev_hash"] != prev_hash:
                return False
            prev_hash = str(row["hash"])
        return True

    # -- proposal journal ----------------------------------------------------

    def add_proposal(self, proposal: Proposal) -> None:
        self._connection.execute(
            "INSERT INTO proposals (proposal_id, created_at, ticker, action, horizon_days, payload, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                proposal.proposal_id,
                proposal.created_at.isoformat(),
                proposal.ticker,
                proposal.action.value,
                proposal.horizon_days,
                proposal.model_dump_json(),
                _now(),
            ),
        )

    def proposals(self) -> list[Proposal]:
        rows = self._connection.execute("SELECT payload FROM proposals ORDER BY created_at").fetchall()
        return [Proposal.model_validate_json(row["payload"]) for row in rows]

    def add_outcome(self, outcome: ProposalOutcome) -> None:
        self._connection.execute(
            "INSERT INTO proposal_outcomes (proposal_id, evaluated_at, payload, recorded_at) VALUES (?, ?, ?, ?)",
            (outcome.proposal_id, outcome.evaluated_at.isoformat(), outcome.model_dump_json(), _now()),
        )

    def outcomes(self) -> list[ProposalOutcome]:
        rows = self._connection.execute("SELECT payload FROM proposal_outcomes ORDER BY outcome_id").fetchall()
        return [ProposalOutcome.model_validate_json(row["payload"]) for row in rows]

    # -- helpers -------------------------------------------------------------

    def net_cash(self) -> dict[Currency, Decimal]:
        totals: dict[Currency, Decimal] = {}
        for transaction in self.transactions():
            totals[transaction.currency] = totals.get(transaction.currency, Decimal("0")) + transaction.amount
        return dict(totals)

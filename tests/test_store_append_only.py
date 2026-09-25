"""Storage guarantees: append-only enforcement and tamper-evident audit."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cartera.adapters.sqlite_store import SqlitePortfolioStore
from cartera.domain.errors import DuplicateTransactionError
from cartera.domain.models import AssetType, Lot, PortfolioSnapshot, Transaction, TxKind
from cartera.domain.money import Currency

pytestmark = pytest.mark.integration

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
MOMENT = datetime(2026, 9, 25, 15, 0, tzinfo=TZ)


def make_lot(lot_id: str = "lot-1") -> Lot:
    return Lot(
        lot_id=lot_id,
        ticker="GGAL",
        asset_type=AssetType.EQUITY,
        quantity=Decimal("100"),
        unit_price=Decimal("5000"),
        fees=Decimal("1650"),
        currency=Currency.ARS,
        opened_at=MOMENT,
    )


def make_transaction(tx_id: str = "tx-1") -> Transaction:
    return Transaction(
        tx_id=tx_id,
        kind=TxKind.BUY,
        occurred_at=MOMENT,
        currency=Currency.ARS,
        amount=Decimal("-501650"),
        ticker="GGAL",
        asset_type=AssetType.EQUITY,
        lot_id="lot-1",
        quantity=Decimal("100"),
        price=Decimal("5000"),
        fees=Decimal("1650"),
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqlitePortfolioStore]:
    instance = SqlitePortfolioStore(tmp_path / "cartera.sqlite3")
    yield instance
    instance.close()


def test_lot_round_trips_with_exact_decimals(store: SqlitePortfolioStore) -> None:
    store.add_lot(make_lot())
    lots = store.lots()

    assert len(lots) == 1
    assert lots[0].quantity == Decimal("100")
    assert lots[0].unit_price == Decimal("5000")
    assert lots[0].cost_basis == Decimal("501650.00")


def test_transaction_round_trips(store: SqlitePortfolioStore) -> None:
    store.append_transaction(make_transaction())
    stored = store.transactions()

    assert len(stored) == 1
    assert stored[0].kind is TxKind.BUY
    assert stored[0].amount == Decimal("-501650")
    assert stored[0].fees == Decimal("1650")


def test_duplicate_transaction_is_refused(store: SqlitePortfolioStore) -> None:
    store.append_transaction(make_transaction())
    with pytest.raises(DuplicateTransactionError):
        store.append_transaction(make_transaction())


def test_duplicate_lot_is_refused(store: SqlitePortfolioStore) -> None:
    store.add_lot(make_lot())
    with pytest.raises(DuplicateTransactionError):
        store.add_lot(make_lot())


def test_ledger_cannot_be_updated(store: SqlitePortfolioStore) -> None:
    store.append_transaction(make_transaction())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._connection.execute("UPDATE transactions SET amount = '0' WHERE tx_id = 'tx-1'")


def test_ledger_cannot_be_deleted(store: SqlitePortfolioStore) -> None:
    store.append_transaction(make_transaction())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._connection.execute("DELETE FROM transactions WHERE tx_id = 'tx-1'")


def test_lots_cannot_be_rewritten(store: SqlitePortfolioStore) -> None:
    store.add_lot(make_lot())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._connection.execute("UPDATE lots SET quantity = '1' WHERE lot_id = 'lot-1'")


def test_snapshots_are_appended_and_latest_wins(store: SqlitePortfolioStore) -> None:
    first = PortfolioSnapshot(as_of=MOMENT, cash={Currency.ARS: Decimal("1")}, lots=[make_lot()])
    second = PortfolioSnapshot(as_of=MOMENT, cash={Currency.ARS: Decimal("2")}, lots=[], label="after-sale")

    store.save_snapshot(first)
    store.save_snapshot(second)

    assert store.snapshot_count() == 2
    latest = store.latest_snapshot()
    assert latest is not None
    assert latest.cash_of(Currency.ARS) == Decimal("2.00")
    assert latest.label == "after-sale"


def test_audit_chain_verifies(store: SqlitePortfolioStore) -> None:
    store.record_audit("snapshot.import", {"lots": 1})
    store.record_audit("report.generate", {"tickers": ["GGAL"]})

    assert store.verify_audit_chain() is True
    entries = store.audit_entries()
    assert len(entries) == 2
    assert entries[1]["prev_hash"] == entries[0]["hash"]


def test_audit_tampering_breaks_the_chain(store: SqlitePortfolioStore, tmp_path: Path) -> None:
    store.record_audit("snapshot.import", {"lots": 1})
    store.record_audit("report.generate", {"tickers": ["GGAL"]})
    store.close()

    # Simulate an attacker with file access who disables the trigger and edits a row.
    with sqlite3.connect(str(tmp_path / "cartera.sqlite3")) as attacker:
        attacker.execute("DROP TRIGGER audit_no_update")
        attacker.execute("UPDATE audit_log SET payload = '{\"lots\": 999}' WHERE entry_id = 1")
        attacker.commit()

    reopened = SqlitePortfolioStore(tmp_path / "cartera.sqlite3")
    try:
        assert reopened.verify_audit_chain() is False
    finally:
        reopened.close()


def test_net_cash_sums_signed_amounts(store: SqlitePortfolioStore) -> None:
    store.append_transaction(make_transaction("tx-buy"))
    store.append_transaction(
        make_transaction("tx-sell").model_copy(update={"kind": TxKind.SELL, "amount": Decimal("260000")}),
    )

    assert store.net_cash()[Currency.ARS] == Decimal("-241650")

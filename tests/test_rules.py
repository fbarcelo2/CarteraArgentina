"""Freshness and concentration gates: the checks that stop a bad report."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from cartera.domain.errors import StaleQuoteError
from cartera.domain.models import Quote
from cartera.domain.money import Currency
from cartera.domain.rules import assert_fresh, check_concentration, check_freshness, merge_reports

pytestmark = pytest.mark.unit

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
NOW = datetime(2026, 9, 25, 15, 0, tzinfo=TZ)


def quote(minutes_old: float) -> Quote:
    return Quote(
        ticker="GGAL",
        price=Decimal("5200"),
        currency=Currency.ARS,
        as_of=NOW - timedelta(minutes=minutes_old),
        source="test",
    )


def test_fresh_quote_passes() -> None:
    report = check_freshness([quote(30)], NOW)
    assert report.ok
    assert report.issues == ()


def test_stale_quote_is_an_error_not_a_warning() -> None:
    report = check_freshness([quote(13 * 60)], NOW)
    assert not report.ok
    assert report.errors[0].code == "stale_quote"


def test_tolerance_is_configurable() -> None:
    # 30 minutes fits a one-hour tolerance; 120 minutes does not fit a one-minute one.
    assert check_freshness([quote(30)], NOW, max_age_seconds=3600).ok
    assert not check_freshness([quote(120)], NOW, max_age_seconds=60).ok


def test_assert_fresh_raises_with_measured_age() -> None:
    with pytest.raises(StaleQuoteError) as excinfo:
        assert_fresh([quote(60 * 24)], NOW)
    assert excinfo.value.ticker == "GGAL"
    assert excinfo.value.age_seconds > 86_000


def test_concentration_warns_above_threshold() -> None:
    report = check_concentration({"GGAL": Decimal("0.55"), "AL30": Decimal("0.20")})
    assert report.ok  # a warning, not a blocker
    assert len(report.warnings) == 1
    assert "GGAL" in report.warnings[0].detail


def test_concentration_is_silent_below_threshold() -> None:
    assert check_concentration({"GGAL": Decimal("0.29")}).issues == ()


def test_merging_reports_keeps_errors_separate_from_warnings() -> None:
    merged = merge_reports(check_freshness([quote(13 * 60)], NOW), check_concentration({"GGAL": Decimal("0.9")}))
    assert len(merged.errors) == 1
    assert len(merged.warnings) == 1
    assert not merged.ok

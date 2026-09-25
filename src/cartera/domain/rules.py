"""Safety rules applied before any analysis runs.

These are gates, not advice: they decide whether a report may be produced at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from cartera.domain.errors import StaleQuoteError
from cartera.domain.models import Quote
from cartera.domain.money import pct

#: Twelve hours: long enough for an end-of-day run, short enough to catch a
#: source that silently stopped updating.
DEFAULT_MAX_QUOTE_AGE_SECONDS = 12 * 3600

#: Above this share of a currency's market value, one instrument is called out.
DEFAULT_CONCENTRATION_THRESHOLD = Decimal("0.30")


@dataclass(frozen=True)
class ValidationIssue:
    """A problem found while checking inputs. ``severity`` drives the response."""

    code: str
    detail: str
    severity: str = "warning"


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors


def check_freshness(
    quotes: list[Quote],
    now: datetime,
    max_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
) -> ValidationReport:
    """Flag quotes older than the tolerance.

    Stale data is an error, never a warning: an analysis on stale prices is
    worse than no analysis, because it looks authoritative.
    """
    issues: list[ValidationIssue] = []
    for quote in quotes:
        age = quote.age_seconds(now)
        if age > max_age_seconds:
            issues.append(
                ValidationIssue(
                    code="stale_quote",
                    detail=(
                        f"{quote.ticker} priced at {quote.price} {quote.currency} "
                        f"is {age / 3600:.1f}h old (source {quote.source}, tolerance {max_age_seconds / 3600:.1f}h)"
                    ),
                    severity="error",
                ),
            )
        elif age < 0:
            issues.append(
                ValidationIssue(
                    code="future_quote",
                    detail=f"{quote.ticker} is timestamped {-age / 60:.0f} minutes in the future",
                    severity="error",
                ),
            )
    return ValidationReport(issues=tuple(issues))


def assert_fresh(
    quotes: list[Quote],
    now: datetime,
    max_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
) -> None:
    """Raise on the first stale quote, so callers cannot ignore it by accident."""
    report = check_freshness(quotes, now, max_age_seconds)
    for issue in report.errors:
        if issue.code == "stale_quote":
            ticker = issue.detail.split(" ", 1)[0]
            matching = next((quote for quote in quotes if quote.ticker == ticker), None)
            if matching is not None:
                raise StaleQuoteError(ticker, matching.age_seconds(now), max_age_seconds)


def check_concentration(
    weights: dict[str, Decimal],
    threshold: Decimal = DEFAULT_CONCENTRATION_THRESHOLD,
) -> ValidationReport:
    """Flag single-instrument weights above the threshold."""
    issues = [
        ValidationIssue(
            code="concentration",
            detail=f"{ticker} is {pct(weight, Decimal('1')):.1f}% of the priced portfolio",
        )
        for ticker, weight in sorted(weights.items())
        if weight > threshold
    ]
    return ValidationReport(issues=tuple(issues))


def merge_reports(*reports: ValidationReport) -> ValidationReport:
    return ValidationReport(issues=tuple(issue for report in reports for issue in report.issues))

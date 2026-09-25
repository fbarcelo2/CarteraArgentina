"""Narration over figures computed elsewhere, with a guard against invention.

The model's only job here is prose. If its answer contains a number that was not
in the input, the answer is reported as unverified and the offending tokens are
named, because a narrative that quietly invents a figure is worse than no
narrative at all: it launders a guess into something that reads like the output of
a calculator.

The guard is a lint, not a proof. Number formatting is ambiguous across locales —
``6.280`` is six thousand two hundred and eighty here and six point two eight
elsewhere — so it normalises by digits and accepts a match in several forms. What
it cannot trace, it names, and the caller decides.

A second endpoint is available for analysis, and it is treated as less trusted than
the first. The local one may receive the full figures; a remote one may not, and
the code refuses rather than trusting whoever wrote the configuration. What crosses
that boundary is a projection: a payload built by naming the fields it keeps, never
by deleting the fields it remembers to hide.
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from cartera.adapters.openai_compat import OpenAICompatibleBackend
from cartera.app.services import ReportResult
from cartera.config import Settings
from cartera.domain.errors import CarteraError
from cartera.domain.proposals import Scoreboard
from cartera.ports.store import AnalysisBackend
from cartera.security.secrets import get_secret

#: Numbers as a reader writes them: 1.234,56 · 1234.56 · 6.280 · 12 · -4.5.
NUMBER = re.compile(r"-?\d+(?:[.,]\d+)*")

# The names of the keyring entries, not passwords: what bandit looks for cannot be
# written literally here, which is the point.
LOCAL_KEY_SECRET = "CARTERA_LLM_API_KEY"  # nosec B105
REMOTE_KEY_SECRET = "CARTERA_ANALYSIS_LLM_API_KEY"  # nosec B105

SYSTEM_PROMPT = (
    "You write a short, plain-language note about a portfolio, for its owner. "
    "You are given figures that were already computed by tested code. "
    "Rules, in order of importance: "
    "1. NEVER compute, estimate, extrapolate or round a figure. Every number you "
    "write must be one of the numbers you were given, copied exactly. "
    "2. If something is not in the input, say it is not available. Do not infer it. "
    "3. Do not give advice, recommendations, price targets or predictions. Describe "
    "what the figures show and what is unknown. "
    "4. Say when data is stale or missing, and prefer refusing to summarising a "
    "figure whose provenance is not stated. "
    "5. No preamble, no sign-off: three to six sentences of plain prose."
)

#: The analysis prompt, used for the remote endpoint. It reasons about structure
#: rather than describing a portfolio: same arithmetic rules, wider question.
ANALYSIS_SYSTEM_PROMPT = (
    "You analyse the structure of a portfolio for its owner. You are given a "
    "projection of figures that were already computed by tested code: what the book "
    "is exposed to, grouped, with the instruments and the amounts withheld. "
    "Rules, in order of importance: "
    "1. NEVER compute, estimate or round a figure. Every number you write must be "
    "one of the numbers you were given, copied exactly. "
    "2. Reason about what that structure implies: which risks it concentrates, what "
    "would have to happen for each one to matter, and what is not visible from here. "
    "3. Name what the projection withholds rather than guessing it. If a conclusion "
    "needs the instruments or the amounts, say that it cannot be reached from this "
    "input. "
    "4. No buy or sell recommendations, no price targets, no predictions. You may "
    "state a hypothesis and, if you do, state what would falsify it. "
    "5. Plain prose, no preamble and no sign-off. Structure it around the exposures."
)


def _normalise(token: str) -> set[str]:
    """Every form of one numeric token that could fairly be called a match.

    Locale makes separators ambiguous — ``6.280`` is six thousand here and six
    point two eight elsewhere — so both readings are accepted instead of picking
    one and mangling the other: stripping separators outright turned ``628000.00``
    into sixty-two million. The guard is a lint, not a proof: it would rather trace
    a correctly formatted number than flag one, and everything it cannot trace it
    names.
    """
    forms = {token, token.lstrip("-")}
    digits = re.sub(r"\D", "", token)
    if digits:
        forms.add(digits)
        forms.add(digits.lstrip("0") or "0")

    # (1) comma as the thousands separator, (2) dot as the thousands separator.
    for interpretation in (token.replace(",", ""), token.replace(".", "").replace(",", ".")):
        try:
            number = Decimal(interpretation)
        except InvalidOperation:
            continue
        forms.add(str(number))
        forms.add(f"{number.normalize():f}")
        if number == number.to_integral_value():
            forms.add(str(int(number)))
    return forms


def allowed_numbers(figures: object) -> set[str]:
    """Every number the input itself contains, in every form we accept."""
    allowed: set[str] = set()
    for match in NUMBER.finditer(json.dumps(figures, default=str)):
        allowed |= _normalise(match.group())
    return allowed


def unverified_numbers(narrative: str, figures: object) -> list[str]:
    """Numbers in the narrative that cannot be traced back to the input."""
    allowed = allowed_numbers(figures)
    foreign: list[str] = []
    for match in NUMBER.finditer(narrative):
        token = match.group()
        if not (_normalise(token) & allowed):
            foreign.append(token)
    return foreign


def narratable_figures(report: ReportResult, scoreboard: Scoreboard) -> dict[str, object]:
    """The payload the model receives: figures computed here, and nothing else.

    Defined once and shared by the CLI and the UI. Two copies of this dictionary
    would drift, and the guard would end up checking the prose against a set of
    numbers that is not the set the reader was shown.

    Provenance travels with the figures on purpose: the instructions ask the model
    to say when data is stale, and it cannot do that if it is only handed values.
    """
    return {
        "generated_at": report.generated_at,
        "snapshot_as_of": report.snapshot_as_of,
        "fresh": report.fresh,
        "issues": report.issues,
        "sources": report.sources,
        "summary": report.summary,
        "realized_pnl": report.realized_pnl,
        "liquidation_costs": report.liquidation_costs,
        "exposure": report.exposure.model_dump(mode="json") if report.exposure else None,
        "scoreboard": scoreboard.model_dump(mode="json"),
    }


class Scope(StrEnum):
    """Where a request is going, which is what decides how much may travel."""

    LOCAL = "local"
    REMOTE = "remote"


class ProjectionLevel(StrEnum):
    """How much of the figures a request is allowed to disclose."""

    #: Everything: tickers, quantities, cost basis, amounts.
    FULL = "full"
    #: Totals and structure: amounts per currency, asset-type weights, the largest
    #: weight. No ticker, no quantity, no cost basis — not per instrument and not per
    #: currency either: the basis is arithmetic on two totals the analysis already
    #: receives, and it is not what reasoning about the structure needs.
    AGGREGATED = "aggregated"
    #: Shape only: asset-type weights and counts, no absolute amount anywhere.
    STRUCTURAL = "structural"


#: What each scope sends unless the caller asks for something tighter. The remote
#: endpoint is not trusted with the full figures, and asking for them is refused.
LEVEL_FOR_SCOPE: dict[Scope, ProjectionLevel] = {
    Scope.LOCAL: ProjectionLevel.FULL,
    Scope.REMOTE: ProjectionLevel.AGGREGATED,
}


def scope_level(scope: Scope, requested: ProjectionLevel | None = None) -> ProjectionLevel:
    """The level a scope will use, or a refusal when the ask is not safe.

    The refusal is the feature. A configuration mistake that sends a portfolio to a
    third party should be a sentence the user reads, not a silent success they find
    out about later.
    """
    level = requested or LEVEL_FOR_SCOPE[scope]
    if scope is Scope.REMOTE and level is ProjectionLevel.FULL:
        raise CarteraError(
            "refusing to send the full figures to the remote endpoint: they carry every "
            "ticker, every quantity and every cost basis. Use scope 'local' for that, or "
            "level 'aggregated'."
        )
    return level


def _issue_summary(figures: dict[str, object]) -> list[dict[str, object]]:
    """Issues reduced to what they are, without the sentence that may name a ticker."""
    issues = figures.get("issues")
    if not isinstance(issues, list):
        return []
    summary: list[dict[str, object]] = []
    for issue in issues:
        if isinstance(issue, dict):
            summary.append({"code": issue.get("code"), "severity": issue.get("severity")})
    return summary


def _books(figures: dict[str, object]) -> list[dict[str, object]]:
    exposure = figures.get("exposure")
    if not isinstance(exposure, dict):
        return []
    currencies = exposure.get("currencies")
    if not isinstance(currencies, list):
        return []
    return [book for book in currencies if isinstance(book, dict)]


def _book(book: dict[str, object], *, with_amounts: bool) -> dict[str, object]:
    """One currency's exposure: the groups by asset type, and no way back to a ticker."""
    groups: list[dict[str, object]] = []
    by_type = book.get("by_asset_type")
    for group in by_type if isinstance(by_type, list) else []:
        if not isinstance(group, dict):
            continue
        entry: dict[str, object] = {
            "asset_type": group.get("label"),
            "weight_pct": group.get("weight_pct"),
        }
        if with_amounts:
            entry["market_value"] = group.get("market_value")
        groups.append(entry)

    largest = book.get("largest")
    largest_pct: object = largest.get("weight_pct") if isinstance(largest, dict) else None
    by_instrument = book.get("by_instrument")
    unpriced = book.get("unpriced")

    reduced: dict[str, object] = {
        "currency": book.get("currency"),
        "instruments": len(by_instrument) if isinstance(by_instrument, list) else 0,
        "by_asset_type": groups,
        "largest_weight_pct": largest_pct,
        "foreign_pct": book.get("foreign_pct"),
        # A count, never the names: "two instruments have no price" is the fact an
        # analysis can use; which ones is the fact it does not need.
        "unpriced_count": len(unpriced) if isinstance(unpriced, list) else 0,
    }
    if with_amounts:
        reduced["market_value"] = book.get("market_value")
        reduced["cash"] = book.get("cash")
        reduced["total"] = book.get("total")
    return reduced


def _totals(figures: dict[str, object]) -> dict[str, object]:
    """Per-currency totals: size, cash and result, without the weights that name tickers.

    The cost basis is deliberately absent. It is arithmetic on the market value and
    the unrealised result, both of which travel, so shipping it adds no analytical
    value — and the rule for a payload that leaves the machine is the minimum that
    answers the question, not everything that can be derived from it.
    """
    summary = figures.get("summary")
    if not isinstance(summary, dict):
        return {}
    valuations = summary.get("valuations")
    if not isinstance(valuations, dict):
        return {}
    totals: dict[str, object] = {}
    for currency, valuation in valuations.items():
        if not isinstance(valuation, dict):
            continue
        totals[str(currency)] = {key: valuation.get(key) for key in ("market_value", "unrealized_pnl", "cash", "total")}
    return totals


def project(figures: dict[str, object], level: ProjectionLevel) -> dict[str, object]:
    """The part of ``figures`` that a level is allowed to disclose.

    Built by naming what it keeps, not by deleting what it remembers to hide: a
    projection that starts from the full payload discloses whatever it forgets. A
    field added to the figures later is private until someone decides otherwise —
    the failure mode here is a missing disclosure, never a leaked position.
    """
    if level is ProjectionLevel.FULL:
        return figures

    payload: dict[str, object] = {
        "generated_at": figures.get("generated_at"),
        "fresh": figures.get("fresh"),
        "issues": _issue_summary(figures),
        "books": [_book(book, with_amounts=level is ProjectionLevel.AGGREGATED) for book in _books(figures)],
        "scoreboard": figures.get("scoreboard"),
        "exposure_available": bool(_books(figures)),
    }
    if level is ProjectionLevel.AGGREGATED:
        payload["snapshot_as_of"] = figures.get("snapshot_as_of")
        payload["sources"] = figures.get("sources")
        payload["totals"] = _totals(figures)
        payload["realized_pnl"] = figures.get("realized_pnl")
        payload["liquidation_costs"] = figures.get("liquidation_costs")
    return payload


class AnalysisResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    narrative: str
    backend: str
    model: str
    #: False when at least one number in the narrative is not in the input.
    verified: bool
    unverified_numbers: list[str] = Field(default_factory=list)
    #: The projected payload that was actually sent, not the full figures: what the
    #: reader is shown in a preview has to be what left the machine.
    figures: dict[str, object] = Field(default_factory=dict)
    scope: str = Scope.LOCAL.value
    level: str = ProjectionLevel.FULL.value


class NarrativeService:
    """Turns computed figures into prose, and checks the prose against them.

    The projection happens here rather than at the call sites, and the guard runs
    against the projected payload rather than the original figures: checking the
    prose against numbers that were never sent would flag a correctly quoted figure
    and, worse, would validate a disclosure nobody made.
    """

    def __init__(
        self,
        backend: AnalysisBackend,
        model: str = "local",
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self.backend = backend
        self.model = model
        self.system_prompt = system_prompt

    async def narrate(
        self,
        figures: dict[str, object],
        focus: str | None = None,
        *,
        scope: Scope = Scope.LOCAL,
        level: ProjectionLevel | None = None,
    ) -> AnalysisResult:
        chosen = scope_level(scope, level)
        payload = project(figures, chosen)
        request = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        if focus:
            request = f"{request}\n\nConcentrate on: {focus}"

        narrative = await self.backend.complete(self.system_prompt, request)
        foreign = unverified_numbers(narrative, payload)
        return AnalysisResult(
            narrative=narrative,
            backend=self.backend.name,
            model=self.model,
            verified=not foreign,
            unverified_numbers=foreign,
            figures=payload,
            scope=scope.value,
            level=chosen.value,
        )


def backend_from_settings(settings: Settings) -> AnalysisBackend | None:
    """The local backend, or ``None`` when the installation has none.

    ``None`` is the shipped state and not an error: the deterministic core never
    needs a model, and the caller is expected to say so rather than fail.
    """
    if not settings.llm_base_url:
        return None
    api_key = os.environ.get(LOCAL_KEY_SECRET) or get_secret(LOCAL_KEY_SECRET)
    return OpenAICompatibleBackend(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=api_key,
        timeout_seconds=settings.llm_timeout_seconds,
    )


def backend_for_scope(settings: Settings, scope: Scope) -> AnalysisBackend | None:
    """The endpoint for a scope, or ``None`` when that scope has none configured.

    The remote endpoint has its own keyring entry on purpose: a machine can hold a
    local model and a remote one at the same time, and the two credentials should
    not be the same secret.
    """
    if scope is not Scope.REMOTE:
        return backend_from_settings(settings)
    if not settings.analysis_llm_base_url:
        return None
    api_key = os.environ.get(REMOTE_KEY_SECRET) or get_secret(REMOTE_KEY_SECRET)
    return OpenAICompatibleBackend(
        base_url=settings.analysis_llm_base_url,
        model=settings.analysis_llm_model,
        api_key=api_key,
        timeout_seconds=settings.analysis_llm_timeout_seconds,
    )

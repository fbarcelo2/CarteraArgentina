"""Local web UI: a reading surface over the same use cases the CLI and MCP call.

Three decisions shape this module, and each one is a limit rather than a feature.

It binds to loopback only. There is one shared token and no user accounts, which
is adequate for one person on their own machine and inadequate for anything
else; a clear refusal beats a half-secured public surface, and remote access is
an SSH tunnel away.

It ships no third-party assets and no outbound requests, so opening the UI does
not tell anyone which instruments you hold — a real concern for a portfolio tool,
not a theoretical one.

It computes nothing. Every figure on every page comes from the services, exactly
like the MCP persona: the UI formats, it does not derive.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from cartera import __version__
from cartera.adapters.sqlite_store import SqlitePortfolioStore
from cartera.agent import ANALYST_INSTRUCTIONS
from cartera.app.analysis import AnalysisResult, NarrativeService, backend_from_settings, narratable_figures
from cartera.app.services import MarketService, PortfolioService, ProposalService, ReportService
from cartera.config import Settings
from cartera.domain.errors import CarteraError
from cartera.domain.proposals import ProposalAction
from cartera.ports.market_data import MarketDataSource
from cartera.ports.store import AnalysisBackend
from cartera.security.secrets import get_secret, keyring_available, keyring_backend, store_secret

COOKIE_NAME = "cartera_session"

# The name of the keyring entry and of the environment variable that holds the
# token, not a password: what bandit looks for cannot be written literally here.
TOKEN_SECRET_KEY = "CARTERA_WEB_TOKEN"  # nosec B105

#: Anything else is refused with an explanation rather than served insecurely.
LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")

#: Twelve hours, then the token has to be pasted again.
SESSION_SECONDS = 12 * 3600

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

NAV: tuple[tuple[str, str], ...] = (
    ("/", "Dashboard"),
    ("/portfolio", "Portfolio"),
    ("/market", "Market"),
    ("/journal", "Journal"),
    ("/analysis", "Analysis"),
    ("/config", "Config"),
    ("/agent", "Analyst"),
    ("/roadmap", "Not built yet"),
)


@dataclass(frozen=True)
class PendingFeature:
    """A function the project intends to have, and what it needs to exist.

    Declared in Python rather than written straight into a template so the
    roadmap cannot drift from the codebase, and so an unbuilt function reads as
    missing instead of as broken.
    """

    title: str
    what: str
    blocked_by: str


PENDING_FEATURES: tuple[PendingFeature, ...] = (
    PendingFeature(
        title="Broker sync",
        what="Read positions and movements from the broker instead of importing a JSON snapshot by hand.",
        blocked_by=(
            "An account with the official API activated. The adapter ships as a private module: "
            "the public core has no broker credentials, by design."
        ),
    ),
    PendingFeature(
        title="Backtest",
        what="Run a rule over stored prices and compare the result with what the journal claimed.",
        blocked_by="Needs a price series in the store: today a quote is kept per report, not as history.",
    ),
    PendingFeature(
        title="Scheduled scoring and alerts",
        what="Score due proposals on a schedule and notify when a horizon closes or a threshold is crossed.",
        blocked_by="A scheduler (systemd timer or cron) and a notification channel.",
    ),
    PendingFeature(
        title="Snapshot history and charts",
        what="Show how valuation and concentration moved across imported snapshots.",
        blocked_by="Charts need a series view; the append-only ledger already keeps every snapshot.",
    ),
    PendingFeature(
        title="Exports",
        what="Export the report, the journal and the scoreboard to CSV and PDF.",
        blocked_by="Not started, deliberately after the read paths settle.",
    ),
    PendingFeature(
        title="Multi-user access",
        what="Several people, each with their own portfolio.",
        blocked_by=(
            "Out of scope. Single user, loopback, no accounts: exposing this to a network "
            "would need real authentication, not a token in a cookie."
        ),
    ),
)


def resolve_web_token(environ: Mapping[str, str] | None = None) -> tuple[str | None, str]:
    """The UI token and where it came from.

    From the environment (a container or a systemd unit) or from the keyring (a
    laptop). Generated and stored on first use when a keyring exists. Returns
    ``None`` when neither is possible, so the caller can refuse to serve instead
    of serving an open UI.
    """
    environment = os.environ if environ is None else environ
    from_environment = environment.get(TOKEN_SECRET_KEY, "").strip()
    if from_environment:
        return from_environment, "environment"

    from_keyring = get_secret(TOKEN_SECRET_KEY)
    if from_keyring:
        return from_keyring, "keyring"

    if keyring_available():
        generated = secrets.token_urlsafe(32)
        if store_secret(TOKEN_SECRET_KEY, generated).verified:
            return generated, "generated"

    return None, "unavailable"


def assert_loopback(host: str) -> None:
    """Refuse to bind anywhere but loopback.

    The check is here, next to the binding, rather than in the CLI: a front-end
    that forgot it would otherwise be able to expose the journal to a network.
    """
    if host not in LOOPBACK_HOSTS:
        raise CarteraError(
            f"refusing to bind {host!r}: this UI has no user accounts and one shared token. "
            "Keep it on loopback and reach it through an SSH tunnel instead.",
        )


def _endpoint_label(url: str) -> str:
    """The endpoint as it is safe to print: credentials in the URL are stripped.

    Nothing in this configuration is supposed to carry them, which is exactly why a
    page that shows the endpoint should not be the thing that leaks one.
    """
    parsed = urlparse(url)
    if not (parsed.username or parsed.password):
        return url
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def build_app(
    settings: Settings,
    token: str,
    market_source: MarketDataSource | None = None,
    analysis_backend: AnalysisBackend | None = None,
) -> FastAPI:
    """Wire the pages. The token is mandatory: there is no unauthenticated mode.

    ``market_source`` exists so the UI can be exercised without the network: the
    tests drive the real app and the real use cases against a source whose
    timestamps they control, which is the only way to test the freshness gate.

    ``analysis_backend`` is injected for the same reason, one step further: the page
    that talks to a model must never reach the network under test, so the tests hand
    it a backend they control and read the guard's verdict off the rendered page.
    """
    app = FastAPI(
        title="CarteraArgentina",
        version=__version__,
        # No schema endpoints on a surface that is not meant to be consumed as an API.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def render(request: Request, name: str, status_code: int = 200, **extra: Any) -> HTMLResponse:
        payload: dict[str, Any] = {
            "request": request,
            "nav": NAV,
            "version": __version__,
            "pending": PENDING_FEATURES,
            "year": request.url.path,
        }
        payload.update(extra)
        return TEMPLATES.TemplateResponse(request, name, payload, status_code=status_code)

    #: The last narration this process produced, and when. In memory on purpose: a
    #: narration is not a record, and re-running a model on every page load would be
    #: slow and pointless. Restarting the UI forgets it.
    last_analysis: dict[str, Any] = {"result": None, "at": None}

    def analysis_backend_or_none() -> AnalysisBackend | None:
        """The injected backend wins; otherwise the one the settings describe."""
        if analysis_backend is not None:
            return analysis_backend
        return backend_from_settings(settings)

    def analysis_context(error: str | None = None, focus: str = "") -> dict[str, Any]:
        """What the page needs: whether a model exists, and the last answer."""
        if analysis_backend is not None:
            endpoint: str | None = f"injected ({analysis_backend.name})"
        elif settings.llm_base_url:
            endpoint = _endpoint_label(settings.llm_base_url)
        else:
            endpoint = None

        result: AnalysisResult | None = last_analysis["result"]
        return {
            "error": error,
            "focus": focus,
            "backend_endpoint": endpoint,
            "backend_model": settings.llm_model,
            "backend_timeout": settings.llm_timeout_seconds,
            "result": result,
            "ran_at": last_analysis["at"],
            "figures_json": (json.dumps(result.figures, indent=2, ensure_ascii=False, default=str) if result else ""),
        }

    @app.middleware("http")
    async def require_token(request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path.startswith("/static") or path == "/login":
            return await call_next(request)
        supplied = request.cookies.get(COOKIE_NAME, "")
        if not secrets.compare_digest(supplied, token):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    @app.get("/login")
    def login_form(request: Request) -> HTMLResponse:
        return render(request, "login.html", error=None)

    @app.post("/login")
    def login(request: Request, submitted: Annotated[str, Form()] = "") -> Response:
        if not secrets.compare_digest(submitted.strip(), token):
            return render(request, "login.html", status_code=401, error="That token does not match.")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict", max_age=SESSION_SECONDS)
        return response

    @app.post("/logout")
    def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    @app.get("/")
    async def dashboard(request: Request) -> HTMLResponse:
        store = SqlitePortfolioStore(settings.db_path)
        market = MarketService(settings, source=market_source)
        try:
            report = await ReportService(settings, store, market).build()
            board = ProposalService(store, market).scoreboard()
            summary = PortfolioService(store).summary()
        except CarteraError as exc:
            return render(request, "index.html", report=None, board=None, summary=None, error=str(exc))
        finally:
            await market.aclose()
            store.close()
        return render(request, "index.html", report=report, board=board, summary=summary, error=None)

    @app.get("/portfolio")
    def portfolio(request: Request, imported: bool = False, error: str | None = None) -> HTMLResponse:
        store = SqlitePortfolioStore(settings.db_path)
        try:
            summary = PortfolioService(store).summary()
        finally:
            store.close()
        return render(request, "portfolio.html", summary=summary, imported=imported, error=error)

    @app.post("/portfolio/import")
    def portfolio_import(
        request: Request, path: Annotated[str, Form()], label: Annotated[str, Form()] = ""
    ) -> Response:
        store = SqlitePortfolioStore(settings.db_path)
        try:
            PortfolioService(store).import_file(Path(path).expanduser(), label or None)
        except (CarteraError, OSError) as exc:
            store.close()
            return render(request, "portfolio.html", status_code=400, summary=None, imported=False, error=str(exc))
        store.close()
        return RedirectResponse("/portfolio?imported=1", status_code=303)

    @app.get("/market")
    async def market_page(request: Request, tickers: str = "GGAL,AL30") -> HTMLResponse:
        wanted = [item.strip().upper() for item in tickers.split(",") if item.strip()]
        market = MarketService(settings, source=market_source)
        try:
            session = await market.session()
            quotes = await market.quotes(wanted)
        except CarteraError as exc:
            return render(
                request, "market.html", status_code=200, session=None, quotes=None, tickers=tickers, error=str(exc)
            )
        finally:
            await market.aclose()
        return render(request, "market.html", session=session, quotes=quotes, tickers=tickers, error=None)

    @app.get("/journal")
    async def journal(request: Request, error: str | None = None) -> HTMLResponse:
        store = SqlitePortfolioStore(settings.db_path)
        market = MarketService(settings, source=market_source)
        try:
            service = ProposalService(store, market)
            rows = service.journal()
            board = service.scoreboard()
        finally:
            await market.aclose()
            store.close()
        return render(request, "journal.html", rows=rows, board=board, error=error)

    @app.post("/journal/record")
    async def journal_record(
        ticker: Annotated[str, Form()],
        action: Annotated[str, Form()],
        rationale: Annotated[str, Form()],
        horizon_days: Annotated[int, Form()],
    ) -> Response:
        store = SqlitePortfolioStore(settings.db_path)
        market = MarketService(settings, source=market_source)
        try:
            parsed_action = ProposalAction(action.strip().lower())
        except ValueError:
            await market.aclose()
            store.close()
            return RedirectResponse("/journal?error=Unknown+action", status_code=303)
        try:
            await ProposalService(store, market).record(
                ticker=ticker,
                action=parsed_action,
                rationale=rationale,
                horizon_days=horizon_days,
                source="web",
            )
        except CarteraError as exc:
            return RedirectResponse(f"/journal?error={exc}", status_code=303)
        finally:
            await market.aclose()
            store.close()
        return RedirectResponse("/journal", status_code=303)

    @app.get("/analysis")
    def analysis_page(request: Request) -> HTMLResponse:
        return render(request, "analysis.html", **analysis_context())

    @app.post("/analysis")
    async def analysis_run(request: Request, focus: Annotated[str, Form()] = "") -> HTMLResponse:
        """Ask the model about the figures as they stand right now.

        The payload comes from ``narratable_figures``, the same function the CLI and
        the MCP-facing code paths use, so the page cannot narrate a different set of
        numbers than the command line would.
        """
        backend = analysis_backend_or_none()
        if backend is None:
            return render(
                request,
                "analysis.html",
                status_code=400,
                **analysis_context(error="No analysis backend is configured, so there is nothing to ask."),
            )

        wanted = focus.strip()
        store = SqlitePortfolioStore(settings.db_path)
        market = MarketService(settings, source=market_source)
        try:
            report = await ReportService(settings, store, market).build()
            board = ProposalService(store, market).scoreboard()
            result = await NarrativeService(backend, settings.llm_model).narrate(
                narratable_figures(report, board), wanted or None
            )
        except CarteraError as exc:
            # A backend that is down is a sentence, not a 500: the page has to stay
            # readable enough to say which endpoint failed and why.
            return render(request, "analysis.html", **analysis_context(error=str(exc), focus=wanted))
        finally:
            await market.aclose()
            store.close()
            await backend.aclose()

        last_analysis["result"] = result
        last_analysis["at"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        return render(request, "analysis.html", **analysis_context(focus=wanted))

    @app.get("/config")
    def config(request: Request) -> HTMLResponse:
        store = SqlitePortfolioStore(settings.db_path)
        try:
            audit_ok = store.verify_audit_chain()
            entries = len(store.audit_entries())
        finally:
            store.close()
        return render(
            request,
            "config.html",
            settings=settings,
            keyring_backend=keyring_backend(),
            audit_ok=audit_ok,
            audit_entries=entries,
            error=None,
        )

    @app.get("/agent")
    def agent(request: Request) -> HTMLResponse:
        return render(request, "agent.html", instructions=ANALYST_INSTRUCTIONS)

    @app.get("/roadmap")
    def roadmap(request: Request) -> HTMLResponse:
        return render(request, "roadmap.html")

    return app

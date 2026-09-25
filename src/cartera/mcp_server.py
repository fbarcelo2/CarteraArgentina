"""MCP front-end (stdio). Thin: same use cases as the CLI, different transport.

Tool count is deliberately small and orthogonal — eight tools a model can hold in
head, rather than thirty it will misuse. Read ``cartera.spec`` for the manifest.

The SDK used here is ``mcp`` 2.x, where the high-level class is
``mcp.server.mcpserver.MCPServer`` (``FastMCP`` was renamed in 2.0 and the old
import raises with a pointer to the migration guide).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from cartera import __version__
from cartera.adapters.sqlite_store import SqlitePortfolioStore
from cartera.agent import ANALYST_INSTRUCTIONS, review_request
from cartera.app.services import MarketService, PortfolioService, ProposalService, ReportService
from cartera.config import Settings, load_settings
from cartera.domain.errors import CarteraError
from cartera.domain.proposals import ProposalAction
from cartera.security.secrets import keyring_backend
from cartera.spec import manifest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server.mcpserver import MCPServer

INSTRUCTIONS = (
    "Read-only Argentine portfolio analytics. This server cannot place, modify "
    "or cancel orders and holds no broker credentials: it values a portfolio you "
    "supplied and reports market data with its provenance. Every figure is "
    "computed in tested code — narrate those figures, never compute your own. "
    'When a tool answers with {"ok": false} or a report comes back with '
    '"fresh": false, the correct behaviour is to relay the reason, not to '
    "estimate a number."
)


def _settings() -> Settings:
    settings = load_settings()
    settings.ensure_dirs()
    return settings


def _error(exc: Exception) -> dict[str, Any]:
    """Errors are returned as data: a failed gate is a result, not a crash."""
    return {"ok": False, "error": type(exc).__name__, "detail": str(exc)}


def build_server() -> MCPServer:
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError as exc:  # pragma: no cover - optional extra
        raise SystemExit("MCP support is not installed. Run: uv sync --extra mcp") from exc

    server = MCPServer(
        name="cartera-argentina",
        title="CarteraArgentina",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    @server.tool()
    async def market_session() -> dict[str, Any]:
        """Whether the market trades today, with opening and closing times."""
        settings = _settings()
        market = MarketService(settings)
        try:
            session = await market.session()
        except CarteraError as exc:
            return _error(exc)
        finally:
            await market.aclose()
        return {
            "ok": True,
            "is_working_day": session.is_working_day,
            "opens_at": session.opens_at.isoformat() if session.opens_at else None,
            "closes_at": session.closes_at.isoformat() if session.closes_at else None,
            "timezone": session.timezone,
        }

    @server.tool()
    async def market_quotes(
        tickers: Annotated[list[str], Field(description='Tickers such as ["GGAL", "AL30", "AAPL"].')],
        universes: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Optional subset of: blue-chips, general-equity, cedears, public-bonds, corp-bonds, options."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Delayed quotes for specific tickers (read-only, no credentials)."""
        settings = _settings()
        market = MarketService(settings)
        try:
            result = await market.quotes(tickers, universes)
        except CarteraError as exc:
            return _error(exc)
        finally:
            await market.aclose()
        return {
            "ok": True,
            "sources": result.sources,
            "quotes": [
                {
                    "ticker": quote.ticker,
                    "price": str(quote.price),
                    "currency": quote.currency.value,
                    "settlement": quote.settlement.label if quote.settlement else None,
                    "as_of": quote.as_of.isoformat(),
                    "source": quote.source,
                }
                for quote in result.quotes
            ],
        }

    @server.tool()
    def portfolio_import(
        path: Annotated[str, Field(description="Absolute path to a snapshot file the user prepared.")],
        label: Annotated[str | None, Field(description="Optional human label for this snapshot.")] = None,
    ) -> dict[str, Any]:
        """Import a local JSON snapshot into the append-only ledger."""
        settings = _settings()
        store = SqlitePortfolioStore(settings.db_path)
        try:
            result = PortfolioService(store).import_file(Path(path), label)
        except CarteraError as exc:
            return _error(exc)
        finally:
            store.close()
        return {"ok": True, **result.model_dump(mode="json")}

    @server.tool()
    def portfolio_summary() -> dict[str, Any]:
        """Holdings and cash from the latest imported snapshot."""
        settings = _settings()
        store = SqlitePortfolioStore(settings.db_path)
        try:
            summary = PortfolioService(store).summary()
        finally:
            store.close()
        if summary is None:
            return {
                "ok": False,
                "error": "no_snapshot",
                "detail": "no portfolio snapshot has been imported yet",
            }
        return {"ok": True, **summary.model_dump(mode="json")}

    @server.tool()
    async def portfolio_report() -> dict[str, Any]:
        """Deterministic valuation, P&L and gate results for the latest snapshot.

        Returns ``fresh: false`` with reasons instead of numbers whenever the
        quotes are stale or the source is down — never a report on stale prices.
        """
        settings = _settings()
        store = SqlitePortfolioStore(settings.db_path)

        async def run() -> dict[str, Any]:
            market = MarketService(settings)
            try:
                result = await ReportService(settings, store, market).build()
            finally:
                await market.aclose()
            return {"ok": True, **result.model_dump(mode="json")}

        try:
            return await run()
        finally:
            store.close()

    @server.tool()
    def config_show() -> dict[str, Any]:
        """Effective non-secret configuration (never prints secret values)."""
        settings = _settings()
        return {
            "ok": True,
            "data_dir": str(settings.data_dir),
            "db_path": str(settings.db_path),
            "timezone": str(settings.timezone),
            "max_quote_age_seconds": settings.max_quote_age_seconds,
            "commission_pct": {key.value: str(value) for key, value in settings.commission_pct.items()},
            "keyring_backend": keyring_backend(),
            "secrets_present": list(settings.loaded_secrets),
        }

    @server.tool()
    async def proposal_record(
        ticker: Annotated[str, Field(description='Asset the view is about, e.g. "GGAL".')],
        action: Annotated[str, Field(description='One of "buy", "sell" or "hold".')],
        rationale: Annotated[
            str,
            Field(description="Why the view is held. Required: an unexplained view cannot be reviewed later."),
        ],
        horizon_days: Annotated[int, Field(description="Days after which the view may be scored.")],
        target_price: Annotated[
            float | None,
            Field(description="Optional target price, recorded for the reader, never used to score."),
        ] = None,
    ) -> dict[str, Any]:
        """Record a view at today's price so the journal can score it later.

        The reference price is fetched by the server, so a caller cannot choose the
        entry price. Nothing here reaches a broker: this writes a note in your own
        append-only journal, and it is not advice.
        """
        try:
            parsed_action = ProposalAction(action.strip().lower())
        except ValueError:
            return _error(CarteraError(f"unknown action {action!r}: expected buy, sell or hold"))
        settings = _settings()
        store = SqlitePortfolioStore(settings.db_path)
        market = MarketService(settings)
        try:
            result = await ProposalService(store, market).record(
                ticker=ticker,
                action=parsed_action,
                rationale=rationale,
                horizon_days=horizon_days,
                target_price=None if target_price is None else Decimal(str(target_price)),
                source="mcp",
            )
        except CarteraError as exc:
            return _error(exc)
        finally:
            await market.aclose()
            store.close()
        return {"ok": True, "proposal": result.proposal, "audit_hash": result.audit_hash}

    @server.tool()
    async def proposal_scoreboard(
        score_first: Annotated[
            bool,
            Field(description="Score due proposals against current quotes before aggregating."),
        ] = True,
    ) -> dict[str, Any]:
        """The hit rate of everything the journal has claimed, and what is pending."""
        settings = _settings()
        store = SqlitePortfolioStore(settings.db_path)
        market = MarketService(settings)
        service = ProposalService(store, market)
        try:
            run = await service.score() if score_first else None
            board = service.scoreboard().model_dump(mode="json")
        except CarteraError as exc:
            return _error(exc)
        finally:
            await market.aclose()
            store.close()
        return {
            "ok": True,
            "scoreboard": board,
            "run": None if run is None else run.model_dump(mode="json"),
        }

    @server.resource("cartera://spec")
    def capability_manifest() -> str:
        """Machine-readable manifest of what this installation can do."""
        return json.dumps(manifest(), indent=2)

    @server.resource("cartera://agent")
    def analyst_instructions() -> str:
        """The analyst invariants, so any client picks them up without copy-pasting."""
        return ANALYST_INSTRUCTIONS

    @server.prompt()
    def portfolio_review(
        focus: Annotated[
            str | None,
            Field(description='Optional area to concentrate on, e.g. "concentration".'),
        ] = None,
    ) -> list[dict[str, str]]:
        """Instructions to review the latest snapshot, optionally focused on one area."""
        return [
            {"role": "user", "content": ANALYST_INSTRUCTIONS},
            {"role": "user", "content": review_request(focus)},
        ]

    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()


# Kept for tests that want the tool list without starting a transport.
def tool_names() -> list[str]:
    return [tool["name"] for tool in manifest()["mcp_tools"]]

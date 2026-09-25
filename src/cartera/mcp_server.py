"""MCP front-end (stdio). Thin: same use cases as the CLI, different transport.

Tool count is deliberately small and orthogonal — six tools a model can hold in
head, rather than thirty it will misuse. Read ``cartera.spec`` for the manifest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cartera.adapters.sqlite_store import SqlitePortfolioStore
from cartera.app.services import MarketService, PortfolioService, ReportService
from cartera.config import Settings, load_settings
from cartera.domain.errors import CarteraError
from cartera.security.secrets import keyring_backend
from cartera.spec import manifest


def _settings() -> Settings:
    settings = load_settings()
    settings.ensure_dirs()
    return settings


def _error(exc: Exception) -> dict[str, Any]:
    """Errors are returned as data: a failed gate is a result, not a crash."""
    return {"ok": False, "error": type(exc).__name__, "detail": str(exc)}


def build_server() -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - optional extra
        raise SystemExit("MCP support is not installed. Run: uv sync --extra mcp") from exc

    server = FastMCP("cartera-argentina")

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
    async def market_quotes(tickers: list[str], universes: list[str] | None = None) -> dict[str, Any]:
        """Delayed quotes for specific tickers (read-only, no credentials).

        Args:
            tickers: Tickers such as ["GGAL", "AL30", "AAPL"].
            universes: Optional subset of blue-chips, general-equity, cedears,
                public-bonds, corp-bonds, options.
        """
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
    def portfolio_import(path: str, label: str | None = None) -> dict[str, Any]:
        """Import a local JSON snapshot into the append-only ledger.

        Args:
            path: Absolute path to a snapshot file the user prepared.
            label: Optional human label for this snapshot.
        """
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
            return {"ok": False, "error": "no_snapshot", "detail": "no portfolio snapshot has been imported yet"}
        return {"ok": True, **summary.model_dump(mode="json")}

    @server.tool()
    async def portfolio_report() -> dict[str, Any]:
        """Deterministic valuation, P&L and gate results for the latest snapshot.

        Returns ``fresh: false`` with reasons instead of numbers whenever the
        quotes are stale or the source is down — never a report on stale prices.
        """
        settings = _settings()
        store = SqlitePortfolioStore(settings.db_path)
        market = MarketService(settings)
        try:
            result = await ReportService(settings, store, market).build()
        finally:
            await market.aclose()
            store.close()
        return {"ok": True, **result.model_dump(mode="json")}

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

    @server.resource("cartera://spec")
    def capability_manifest() -> str:
        """Machine-readable manifest of what this installation can do."""
        import json

        return json.dumps(manifest(), indent=2)

    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()


# Kept for tests that want the tool list without starting a transport.
def tool_names() -> list[str]:
    return [tool["name"] for tool in manifest()["mcp_tools"]]

"""Command-line front-end. Thin: it translates arguments and prints results."""

from __future__ import annotations

import asyncio
import json as jsonlib
from pathlib import Path
from typing import Annotated

import typer
from dotenv import dotenv_values
from rich.console import Console
from rich.table import Table

from cartera import __version__
from cartera.adapters.sqlite_store import SqlitePortfolioStore
from cartera.app.services import (
    MarketQuotesResult,
    MarketService,
    PortfolioService,
    ProposalRecordResult,
    ProposalScoreResult,
    ProposalService,
    ReportResult,
    ReportService,
    decimal_or_none,
)
from cartera.config import Settings, load_settings
from cartera.domain.errors import CarteraError, DomainError
from cartera.domain.proposals import ProposalAction
from cartera.ports.market_data import MarketSession
from cartera.security.secrets import SECRET_KEYS, keyring_available, keyring_backend, store_secret
from cartera.spec import manifest

app = typer.Typer(help="Read-only portfolio analytics for Argentine capital markets.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect and hydrate configuration.", no_args_is_help=True)
market_app = typer.Typer(help="Market data queries (read-only).", no_args_is_help=True)
portfolio_app = typer.Typer(help="Portfolio snapshot operations.", no_args_is_help=True)
proposal_app = typer.Typer(help="The proposal journal: record views, score them later.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(market_app, name="market")
app.add_typer(portfolio_app, name="portfolio")
app.add_typer(proposal_app, name="proposal")

console = Console()
ENV_OPTION = Annotated[Path | None, typer.Option("--env-file", help="Path to the .env used for bootstrap.")]
JSON_OPTION = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]


def _emit(payload: object, as_json: bool, title: str = "") -> None:
    if as_json:
        console.print_json(jsonlib.dumps(payload, default=str))
        return
    if title:
        console.print(f"[bold]{title}[/bold]")
    console.print(jsonlib.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _settings(env_file: Path | None) -> Settings:
    try:
        settings = load_settings(env_file)
    except CarteraError as exc:
        console.print(f"[red]configuration error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    settings.ensure_dirs()
    return settings


def _guard(operation: str, error: CarteraError) -> None:
    console.print(f"[red]{operation} failed:[/red] {error}")
    raise typer.Exit(code=1) from error


@app.callback(invoke_without_command=True)
def _root(
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    """Root callback.

    ``invoke_without_command`` matters: without it, ``cartera --version`` fails
    with "Missing command" because the group demands a subcommand even when a
    root-level option was given.
    """
    if version:
        console.print(f"cartera-argentina {__version__}")
        raise typer.Exit


# --------------------------------------------------------------------------- app


@app.command()
def doctor(env_file: ENV_OPTION = None, as_json: JSON_OPTION = False) -> None:
    """Check configuration, ledger integrity and data-source health.

    Checks are marked ``required`` or informational: a missing keyring is a fact
    about the environment (containers have no Secret Service), not a failure,
    while a down data source or a broken audit chain is a failure.
    """
    settings = _settings(env_file)
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, detail: str, required: bool = True) -> None:
        checks.append({"check": name, "ok": ok, "required": required, "detail": detail})

    add("data_dir", settings.data_dir.exists(), str(settings.data_dir))
    add("db_path", True, str(settings.db_path))

    store = SqlitePortfolioStore(settings.db_path)
    try:
        add("ledger_readable", True, f"{len(store.transactions())} transactions")
        chain_ok = store.verify_audit_chain()
        add("audit_chain", chain_ok, "hash chain verified" if chain_ok else "CHAIN BROKEN")
        add("snapshots", True, str(store.snapshot_count()))
    finally:
        store.close()

    keyring_detail = keyring_backend() or (
        "unavailable — secrets must be injected at runtime (docker secrets, systemd LoadCredential)"
    )
    add("keyring", keyring_available(), keyring_detail, required=False)

    market = MarketService(settings)

    async def probe() -> tuple[bool, str]:
        try:
            session = await market.session()
            quotes = await market.quotes(["GGAL"])
        except CarteraError as exc:
            return False, str(exc)
        finally:
            # Closed in the same event loop that opened the connections: an
            # httpx pool is bound to its loop and closing it from another one
            # raises "Event loop is closed".
            await market.aclose()
        detail = f"session open={session.is_working_day}, quotes={len(quotes.quotes)}"
        return bool(quotes.quotes), detail

    source_ok, source_detail = asyncio.run(probe())
    add("market_source", source_ok, source_detail)

    ok = all(bool(check["ok"]) for check in checks if check["required"])
    _emit({"checks": checks, "ok": ok}, as_json, "doctor")
    if not ok:
        raise typer.Exit(code=1)


@app.command()
def spec(as_json: JSON_OPTION = False) -> None:
    """Print the capability manifest of this installation."""
    _emit(manifest(), as_json, "capabilities")


@app.command()
def report(env_file: ENV_OPTION = None, as_json: JSON_OPTION = False) -> None:
    """Build the deterministic report for the latest imported snapshot."""
    settings = _settings(env_file)
    store = SqlitePortfolioStore(settings.db_path)

    async def run() -> ReportResult:
        market = MarketService(settings)
        try:
            return await ReportService(settings, store, market).build()
        finally:
            await market.aclose()

    try:
        result = asyncio.run(run())
    finally:
        store.close()

    if as_json:
        console.print_json(result.model_dump_json())
    else:
        if not result.fresh:
            console.print("[yellow]report withheld[/yellow] — gates not satisfied:")
            for issue in result.issues:
                console.print(f"  [{issue['severity']}] {issue['code']}: {issue['detail']}")
            raise typer.Exit(code=1)
        _render_report(result)
    if not result.fresh:
        raise typer.Exit(code=1)


def _render_report(result: ReportResult) -> None:
    summary = result.summary or {}
    raw_valuations = summary.get("valuations") if isinstance(summary, dict) else None
    # The summary is a JSON-shaped mapping, so it is narrowed here rather than
    # trusted: an unguarded .items() on a non-mapping crashes the report render.
    valuations: dict[str, dict[str, str]] = raw_valuations if isinstance(raw_valuations, dict) else {}
    table = Table(title=f"Valuation as of {result.snapshot_as_of}")
    for column in ("currency", "market value", "cost basis", "unrealized", "cash", "total"):
        table.add_column(column)
    for currency, values in valuations.items():
        table.add_row(
            currency,
            values["market_value"],
            values["cost_basis"],
            values["unrealized_pnl"],
            values["cash"],
            values["total"],
        )
    console.print(table)
    if result.realized_pnl:
        console.print(f"realized P&L: {result.realized_pnl}")
    if result.liquidation_costs:
        console.print(f"estimated commission to exit: {result.liquidation_costs}")
    for issue in result.issues:
        console.print(f"[yellow]{issue['severity']}[/yellow] {issue['code']}: {issue['detail']}")


# ------------------------------------------------------------------------ market


@market_app.command("quotes")
def market_quotes(
    tickers: Annotated[list[str], typer.Argument(help="Tickers, e.g. GGAL AL30 AAPL.")],
    universe: Annotated[
        list[str] | None,
        typer.Option("--universe", "-u", help="blue-chips, general-equity, cedears, public-bonds, corp-bonds, options"),
    ] = None,
    env_file: ENV_OPTION = None,
    as_json: JSON_OPTION = False,
) -> None:
    """Fetch delayed quotes. No credentials involved."""
    settings = _settings(env_file)
    wanted = [ticker.upper() for ticker in tickers]

    async def run() -> MarketQuotesResult:
        market = MarketService(settings)
        try:
            return await market.quotes(wanted, universe)
        finally:
            await market.aclose()

    try:
        result = asyncio.run(run())
    except CarteraError as exc:
        _guard("quotes", exc)
        return

    if as_json:
        console.print_json(result.model_dump_json())
        return
    table = Table(title=", ".join(result.sources))
    for column in ("ticker", "price", "ccy", "settlement", "as_of"):
        table.add_column(column)
    for quote in result.quotes:
        table.add_row(
            quote.ticker,
            str(quote.price),
            quote.currency.value,
            quote.settlement.label if quote.settlement else "-",
            quote.as_of.isoformat(timespec="minutes"),
        )
    console.print(table)


@market_app.command("session")
def market_session(env_file: ENV_OPTION = None, as_json: JSON_OPTION = False) -> None:
    """Market calendar for today."""
    settings = _settings(env_file)

    async def run() -> MarketSession:
        market = MarketService(settings)
        try:
            return await market.session()
        finally:
            await market.aclose()

    try:
        session = asyncio.run(run())
    except CarteraError as exc:
        _guard("session", exc)
        return
    _emit(
        {
            "is_working_day": session.is_working_day,
            "opens_at": session.opens_at.isoformat() if session.opens_at else None,
            "closes_at": session.closes_at.isoformat() if session.closes_at else None,
            "timezone": session.timezone,
        },
        as_json,
    )


# --------------------------------------------------------------------- portfolio


@portfolio_app.command("import")
def portfolio_import(
    path: Annotated[Path, typer.Argument(help="JSON snapshot file (see docs/portfolio-snapshot.md).")],
    label: Annotated[str | None, typer.Option("--label", help="Human label for this snapshot.")] = None,
    env_file: ENV_OPTION = None,
    as_json: JSON_OPTION = False,
) -> None:
    """Import a snapshot. Re-importing known lots is a no-op, never a duplicate."""
    settings = _settings(env_file)
    if not path.is_file():
        console.print(f"[red]file not found:[/red] {path}")
        raise typer.Exit(code=2)
    store = SqlitePortfolioStore(settings.db_path)
    try:
        result = PortfolioService(store).import_file(path, label)
    except CarteraError as exc:
        _guard("import", exc)
        return
    finally:
        store.close()
    _emit(result.model_dump(mode="json"), as_json, "imported")


@portfolio_app.command("summary")
def portfolio_summary(env_file: ENV_OPTION = None, as_json: JSON_OPTION = False) -> None:
    """Holdings and cash from the latest imported snapshot."""
    settings = _settings(env_file)
    store = SqlitePortfolioStore(settings.db_path)
    try:
        result = PortfolioService(store).summary()
    finally:
        store.close()
    if result is None:
        console.print("[yellow]no snapshot imported yet[/yellow] — run: cartera portfolio import <file>")
        raise typer.Exit(code=1)
    _emit(result.model_dump(mode="json"), as_json, "portfolio")


# ------------------------------------------------------------------------ config


@config_app.command("show")
def config_show(env_file: ENV_OPTION = None, as_json: JSON_OPTION = False) -> None:
    """Effective non-secret configuration. Secrets are never printed."""
    settings = _settings(env_file)
    _emit(
        {
            "data_dir": str(settings.data_dir),
            "db_path": str(settings.db_path),
            "timezone": str(settings.timezone),
            "http_timeout_seconds": settings.http_timeout_seconds,
            "max_quote_age_seconds": settings.max_quote_age_seconds,
            "commission_pct": {key.value: str(value) for key, value in settings.commission_pct.items()},
            "env_file_used": str(settings.env_file_used) if settings.env_file_used else None,
            "secrets_present_in_env": list(settings.loaded_secrets),
            "keyring_backend": keyring_backend(),
        },
        as_json,
    )


@config_app.command("hydrate")
def config_hydrate(
    env_file: Annotated[Path, typer.Option("--env-file", help="Bootstrap file to read.")] = Path(".env"),
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask before deleting the plaintext file.")] = False,
    keep: Annotated[bool, typer.Option("--keep", help="Never delete the file; only store secrets.")] = False,
) -> None:
    """Move the secrets from .env into the OS keyring, then offer to delete it.

    Why deletion matters: a plaintext file with a credential is the single most
    common way a secret leaks — it gets copied, backed up, committed by mistake
    or read by any process running as your user. The keyring is protected by the
    session, so once the value is safely there, the file is pure downside.

    The file is deleted only after every secret is stored AND read back verified,
    and only with your explicit confirmation. Inside a container there is no
    keyring: hydration then refuses to delete anything and tells you to inject
    secrets at runtime instead, because the file is not the thing protecting them.
    """
    if not env_file.is_file():
        console.print(f"[red]no such file:[/red] {env_file}")
        raise typer.Exit(code=2)

    values = dotenv_values(env_file)
    # An explicit loop, not a comprehension: it narrows str | None to str for both
    # the type checker and a reader. A key present with an empty value is skipped
    # rather than stored, which a `values.get(key)` filter does silently.
    present: list[tuple[str, str]] = []
    for key in SECRET_KEYS:
        value = values.get(key)
        if value:
            present.append((key, value))
    if not present:
        names = ", ".join(SECRET_KEYS)
        console.print(f"[green]nothing to hydrate[/green] — {env_file} has no secret values ({names}).")
        return

    keys = ", ".join(key for key, _ in present)
    console.print(f"found {len(present)} secret value(s) in [bold]{env_file}[/bold]: {keys}")

    if not keyring_available():
        console.print("[red]no OS keyring available[/red] (a container has no Secret Service).")
        console.print("The file was [bold]NOT[/bold] deleted. Inject secrets at runtime instead:")
        console.print("  - docker: env_file outside the repo, or docker secrets")
        console.print("  - systemd: LoadCredential= / EnvironmentFile= from a root-only path")
        console.print("  - CI: the platform's secret store")
        raise typer.Exit(code=3)

    failures = []
    for key, value in present:
        status = store_secret(key, value)
        marker = "[green]ok[/green]" if status.verified else "[red]failed[/red]"
        console.print(f"  {key}: {marker} (backend {status.backend})")
        if not status.verified:
            failures.append(status)

    if failures:
        console.print("[red]not every secret could be verified in the keyring[/red] — file kept intact.")
        raise typer.Exit(code=1)

    console.print("all secrets verified in the keyring.")
    if keep:
        console.print(f"[yellow]--keep set[/yellow]: {env_file} left in place.")
        return
    if not yes and not typer.confirm(f"Delete {env_file} now?", default=True):
        console.print("kept by request.")
        return
    env_file.unlink()
    console.print(f"[green]deleted[/green] {env_file} — secrets now come from the keyring.")


@proposal_app.command("record")
def proposal_record(
    ticker: Annotated[str, typer.Argument(help="Asset the view is about.")],
    action: Annotated[str, typer.Option("--action", help="buy, sell or hold.")],
    rationale: Annotated[
        str, typer.Option("--rationale", help="Why the view is held. Required: an unexplained view cannot be reviewed.")
    ],
    horizon_days: Annotated[int, typer.Option("--horizon", help="Days after which the view may be scored.")],
    target_price: Annotated[
        str | None,
        typer.Option("--target", help="Optional target price, recorded for the reader, never used to score."),
    ] = None,
    env_file: ENV_OPTION = None,
    as_json: JSON_OPTION = False,
) -> None:
    """Record a view now, at today's price, so it can be scored later."""
    settings = _settings(env_file)
    store = SqlitePortfolioStore(settings.db_path)

    try:
        parsed_action = ProposalAction(action.strip().lower())
    except ValueError:
        _guard("proposal record", DomainError(f"unknown action {action!r}: expected buy, sell or hold"))
        return
    try:
        target = decimal_or_none(target_price)
    except (ArithmeticError, ValueError):
        _guard("proposal record", DomainError(f"target price {target_price!r} is not a number"))
        return

    async def run() -> ProposalRecordResult:
        market = MarketService(settings)
        try:
            return await ProposalService(store, market).record(
                ticker=ticker,
                action=parsed_action,
                rationale=rationale,
                horizon_days=horizon_days,
                target_price=target,
            )
        finally:
            await market.aclose()

    try:
        try:
            result = asyncio.run(run())
        except CarteraError as exc:
            _guard("proposal record", exc)
    finally:
        store.close()

    _emit({"proposal": result.proposal, "audit_hash": result.audit_hash}, as_json, "recorded")


@proposal_app.command("list")
def proposal_list(env_file: ENV_OPTION = None, as_json: JSON_OPTION = False) -> None:
    """Every proposal with its latest outcome."""
    settings = _settings(env_file)
    store = SqlitePortfolioStore(settings.db_path)
    try:
        journal = ProposalService(store, MarketService(settings)).journal()
    finally:
        store.close()
    _emit({"count": len(journal), "proposals": journal}, as_json, "journal")


@proposal_app.command("score")
def proposal_score(
    rescore: Annotated[bool, typer.Option("--rescore", help="Score again even if an outcome already exists.")] = False,
    env_file: ENV_OPTION = None,
    as_json: JSON_OPTION = False,
) -> None:
    """Score every proposal whose horizon has elapsed."""
    settings = _settings(env_file)
    store = SqlitePortfolioStore(settings.db_path)

    async def run() -> ProposalScoreResult:
        market = MarketService(settings)
        try:
            return await ProposalService(store, market).score(rescore=rescore)
        finally:
            await market.aclose()

    try:
        try:
            result = asyncio.run(run())
        except CarteraError as exc:
            _guard("proposal score", exc)
    finally:
        store.close()

    _emit(result.model_dump(mode="json"), as_json, "scored")


@proposal_app.command("scoreboard")
def proposal_scoreboard(
    score_first: Annotated[
        bool, typer.Option("--score/--no-score", help="Score due proposals before aggregating.")
    ] = True,
    env_file: ENV_OPTION = None,
    as_json: JSON_OPTION = False,
) -> None:
    """The hit rate of everything this journal has claimed so far."""
    settings = _settings(env_file)
    store = SqlitePortfolioStore(settings.db_path)
    service = ProposalService(store, MarketService(settings))

    async def run() -> dict[str, object]:
        scored = await service.score() if score_first else None
        return {
            "scoreboard": service.scoreboard().model_dump(mode="json"),
            "run": None if scored is None else scored.model_dump(mode="json"),
        }

    try:
        try:
            payload = asyncio.run(run())
        except CarteraError as exc:
            _guard("proposal scoreboard", exc)
    finally:
        store.close()

    _emit(payload, as_json, "scoreboard")


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()

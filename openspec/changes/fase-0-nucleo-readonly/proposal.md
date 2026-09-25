# Change: phase 0 — read-only analytics core

## Why

Establish a portfolio analytics tool that can be published and trusted: no
broker credentials, no scraping of login-protected sites, no order execution,
and metrics that are reproducible instead of plausible.

The project that inspired this one bundled scraping, credentials and LLM
arithmetic into a single unauthenticated HTTP service. Most of its value is
recoverable by keeping the good ideas (lot-level accounting, deterministic fees,
research ingestion) and discarding the risk surface.

## What changes

New project. Nothing exists yet, so this change creates the read-only core:

- Market data ingest from a public, credential-free source (delayed feed).
- Reference FX rates.
- Portfolio snapshot import into an append-only ledger.
- Lot-based metrics: valuation, unrealized/realized P&L, commissions, weights,
  estimated liquidation cost.
- Gates: freshness (stale data withholds a report) and concentration.
- Front-ends: CLI and MCP over stdio, over one shared use-case layer.
- Hash-chained audit log; append-only enforced by database triggers.

## Out of scope

- Any broker adapter, especially anything credential-bearing or scraping-based.
  Official-API adapters are the only publishable kind, and none is in this change.
- Web UI. Planned as a separate change, loopback-only with authentication.
- Analysis backend. The `AnalysisBackend` port exists; no vendor SDK is bundled.
- Order execution. Never; see ADR 0003.

## Risks

| Risk | Mitigation |
| --- | --- |
| The free data source is undocumented and can change or go dark | contract tests pin both response shapes; source outages are a first-class error type; the port allows a paid official source |
| A user mistakes analytics for advice | README, CLI copy and manifest all state read-only, no-advice |
| Scope creep toward an "AI trader" | AGENTS.md invariants; no order capability exists to be extended |

## Acceptance

- `ruff` clean; full test suite green.
- `cartera doctor` passes against the live source (keyring reported as
  informational when absent).
- A report is refused, with the reason, when quotes are stale or the source is down.
- Ledger rejects `UPDATE`/`DELETE`; audit chain detects tampering.

See `tasks.md` for the task list and `../../docs/ARCHITECTURE.md` for the design,
with ADRs in `../../docs/adr/`.

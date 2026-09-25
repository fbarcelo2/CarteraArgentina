# Tasks — phase 0: read-only analytics core

Status: implemented. Validation evidence in the notes column.

## 1. Project scaffolding

- [x] Repository skeleton, `uv` project with pinned lockfile, Python 3.13
- [x] `ruff` config aligned with the project standard rule set
- [x] `pytest.ini` with markers and `asyncio_mode = auto`
- [x] Apache-2.0 license, `README.md`, `SECURITY.md`, `AGENTS.md`
- [x] `.gitignore` excluding `.env`, `data/`, databases and reports

## 2. Pure domain

- [x] Decimal money primitives; floats converted via shortest round-trip repr
- [x] Models: `Lot`, `Transaction`, `Position`, `Quote`, `PortfolioSnapshot`
- [x] Metrics: valuation per currency, cost basis with fees, unrealized and
  realized P&L (lot-specific), commissions, weights, liquidation cost
- [x] Gates: freshness (error, not warning), concentration (warning), report merging
- [x] Unit tests with hand-verifiable figures

## 3. Ports and adapters

- [x] `MarketDataSource`, `PortfolioStore`, `AnalysisBackend` protocols
- [x] Market-data adapter for the public portal; both response shapes handled;
  retry on 5xx and transport errors; clock-time timestamps dated in market tz
- [x] Reference FX adapter (rates modelled separately from instruments)
- [x] SQLite append-only store with triggers and hash-chained audit log
- [x] Contract tests with recorded fixtures; pagination, settlement codes,
  zero prices dropped, source outage mapped to a typed error

## 4. Use cases

- [x] Market service with deterministic settlement preference
- [x] Portfolio import (idempotent per lot), summary
- [x] Report service implementing the refusal behaviour
- [x] Every use case writes an audit entry

## 5. Front-ends

- [x] CLI: `doctor`, `spec`, `market quotes|session`, `portfolio import|summary`,
  `report`, `config show|hydrate`; `--json` everywhere
- [x] MCP server over stdio with six orthogonal tools plus a `cartera://spec` resource
- [x] Capability manifest served by both front-ends

## 6. Secrets

- [x] Keyring hydration with read-back verification
- [x] Deletion offered only after verification, with an explanation and confirmation
- [x] No-keyring path refuses to delete and documents runtime injection

## 7. Validation (P1–P5)

- [x] P1 lint: `ruff check .` clean
- [x] P2 tests: 57 passed
- [x] P3 connectivity: live source answers; `cartera doctor` green
- [x] P4 smoke: import → report against live quotes produced a valuation
- [x] P5 UI: not applicable (no UI in this change)

## 8. Follow-ups (not in this change)

- [ ] Analysis backend adapter (local model first), still narration-only
- [ ] Proposal journal with outcome scoring, to measure hit rate over time
- [ ] Web UI: loopback-only, authenticated from the first commit, no static data mount
- [ ] Official-API broker adapter (the only publishable kind)
- [ ] Encrypted-at-rest secret store for users who reject keyring and env vars

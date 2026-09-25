# Security policy

## Threat model

CarteraArgentina is a **local, single-user** tool. It reads public market data
and a portfolio snapshot you provide, stores results in a local SQLite file, and
prints or serves them back to you. It has no server component in its default
configuration, no user accounts, and no multi-tenant data.

Explicitly out of scope:

- Attacks by other tenants (there are none).
- A remote attacker reaching a network service, because none is exposed by
  default. If you add the optional web UI, bind it to loopback.

## Guarantees, and how they are enforced

| Guarantee | Enforced by |
| --- | --- |
| No order execution capability | there is no such code path; the CLI and MCP surface contain no order tool |
| No broker credentials | no adapter in this repository authenticates anywhere with a user identity |
| Secrets never in the repository | `.env` is gitignored; only `.env.example` ships; secrets go to the OS keyring |
| History cannot be rewritten | SQLite triggers abort `UPDATE`/`DELETE` on the ledger |
| Audit log cannot be edited silently | hash chain, verified by `cartera doctor` and tested |
| Money is exact | `Decimal` throughout; a binary float never touches an amount |
| No report on stale prices | freshness gate returns an error and withholds the report |

## Reporting a vulnerability

Open a private security advisory on the repository (Security → Report a
vulnerability) rather than a public issue. Include the version, the reproducing
input, and the impact. Please do not include real credentials or real portfolio
data in a report.

## Dependency policy

- Dependencies are pinned by `uv.lock`.
- `scan.yml` runs a secret scan and a dependency audit on every push; the build
  fails on high or critical findings.
- Runtime dependencies are deliberately few (`httpx`, `pydantic`,
  `python-dotenv`, `typer`, `rich`). The MCP SDK and the web stack are optional
  extras so a minimal install carries a minimal attack surface.
- No deprecated SDKs are bundled: an analysis backend is behind a port, so the
  project is not tied to any vendor SDK's lifecycle.

## What this project will not add

- Broker adapters that scrape a login-protected site, or that place orders.
- Any data redistribution: market data belongs to its source.
- Telemetry, analytics or phone-home of any kind. Nothing about your portfolio
  leaves your machine unless you explicitly configure an analysis backend.

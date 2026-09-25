# ADR 0003 — Public core, private broker adapters

- Status: accepted
- Date: 2026-09-25

## Context

This project is intended to be published. The thing that inspired it automates
login to a retail broker's website with the user's credentials and browser
automation, because that broker has no public API for retail clients.

Publishing such an adapter would mean publishing a tool whose declared purpose is
circumventing a third party's access controls. That carries three distinct risks:
the broker's terms of service, a takedown claim against the repository, and —
most importantly — the burden landing on the maintainer rather than the user who
chooses to run it.

## Decision

1. The **core is public**: market data ingestion, lot accounting, deterministic
   metrics, gates, CLI and MCP front-ends. Useful to anyone in any market, and
   legally unremarkable.
2. **Broker adapters are private.** They live outside this repository and are
   loaded as plugins. Nothing about credentials, session handling or browser
   automation belongs in the published tree.
3. **The only publishable broker integration is an official documented API**
   (OAuth/token based, with the broker's own terms accepted by the user). Any
   such adapter must live on an official API, never on screen scraping.
4. **No redistribution of market data.** Quotes belong to their source; only
   small synthetic fixtures are committed, and `data/` is gitignored.

## Consequences

- The published project can be recommended without qualification.
- Users who want broker automation must implement and own that decision
  themselves, in their own private repository. The core gives them a clean seam
  (the `MarketDataSource` port) instead of an enabler.
- Issue reports about scraping breakage are out of scope by policy.
- Naming: the public tree must not be named after the broker, and must not imply
  affiliation. Compatibility with an external format is described neutrally.

## Note on naming discipline

Referencing a *data source* we legitimately consume (the exchange's public portal)
is accurate attribution and necessary for provenance. Referencing a *project we
are compatible with* is not: that reads as a fork. The line is: name what you
call, do not name yourself after what you imitate.

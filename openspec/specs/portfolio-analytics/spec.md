# portfolio-analytics

Read-only portfolio analytics: ingest market data, account for lots, compute
deterministic metrics, and gate every report behind freshness and integrity
checks. No order execution, no broker credentials.

## Requirements

### Requirement: No order execution

The system SHALL NOT provide any capability to place, modify or cancel an order,
in any front-end or adapter.

#### Scenario: Capability manifest declares read-only

- WHEN the capability manifest is requested
- THEN it reports `read_only: true` and `executes_orders: false`
- AND the exposed tool list contains no order-related tool

#### Scenario: No credential-bearing adapter ships

- WHEN the repository is inspected for authentication to a login-protected third party
- THEN no adapter performs an authenticated login on behalf of a user

### Requirement: Deterministic metrics

All monetary figures SHALL be computed in code with exact decimal arithmetic and
SHALL be covered by tests. A language model SHALL NOT compute figures.

#### Scenario: Same inputs produce the same output

- WHEN the same snapshot and quotes are valued twice
- THEN every monetary value is identical

#### Scenario: Floats never become money

- WHEN an amount arrives as a JSON number
- THEN it is converted through its shortest round-trip representation
- AND quantised half-up to cents

#### Scenario: Cost basis includes fees

- WHEN a lot is created with fees
- THEN its cost basis is `quantity * unit_price + fees`

### Requirement: Freshness gate

A report SHALL be withheld when any quote used is older than the configured
tolerance, or when the data source is unavailable.

#### Scenario: Stale quote withholds the report

- WHEN the only available quote is older than `max_quote_age_seconds`
- THEN the report contains no valuation
- AND it reports an issue with code `stale_quote` and severity `error`

#### Scenario: Source outage is distinguished from staleness

- WHEN the data source fails to answer
- THEN the issue code is `source_unavailable`, not `stale_quote`

#### Scenario: Unpriced positions are reported, never guessed

- WHEN a held ticker has no quote
- THEN it appears in `unpriced_tickers`
- AND it is not valued at cost

### Requirement: Append-only history

The ledger of lots and transactions SHALL be append-only, and attempts to modify
history SHALL fail at the storage layer.

#### Scenario: Update is rejected by the database

- WHEN an `UPDATE` is issued against the ledger
- THEN the database raises an integrity error

#### Scenario: Re-import is idempotent

- WHEN a snapshot containing already-known lots is imported again
- THEN known lots are counted as known, not duplicated, and no error is raised

### Requirement: Tamper-evident audit log

Audit entries SHALL be hash-chained such that editing or removing an entry
invalidates verification.

#### Scenario: Chain verifies after normal use

- WHEN entries are appended through the supported API
- THEN chain verification succeeds

#### Scenario: Editing an entry breaks verification

- WHEN an existing audit row is modified out of band
- THEN chain verification fails

### Requirement: Market data provenance

Every quote SHALL carry its source and observation time, and a quote SHALL be
dated in the market's timezone when the source reports only a clock time.

#### Scenario: Clock time becomes a timestamp

- WHEN a source reports `tradeHour` without a date
- THEN the quote is dated in the configured market timezone
- AND a clock time ahead of the current moment is dated to the previous session

### Requirement: Secrets are not persisted in plaintext

The system SHALL NOT require a plaintext secret file at runtime, and hydration
SHALL delete the bootstrap file only after each secret is stored and verified.

#### Scenario: Hydration verifies before deleting

- WHEN a secret cannot be read back from the secret store
- THEN the bootstrap file is left intact and the command fails

#### Scenario: No keyring means no deletion

- WHEN no OS keyring is available
- THEN the command refuses to delete the file and explains runtime injection instead

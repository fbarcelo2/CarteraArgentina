"""CarteraArgentina — read-only portfolio analytics for Argentine capital markets.

The package is deliberately layered:

- ``cartera.domain``  pure business rules: lots, positions, metrics. No I/O.
- ``cartera.ports``   Protocols describing what the outside world must provide.
- ``cartera.adapters`` concrete implementations (market data, storage).
- ``cartera.app``     use cases composing domain + ports.
- ``cartera.cli`` / ``cartera.mcp_server`` thin front-ends.

There is no order-execution capability anywhere in this package, by design.
"""

__version__ = "0.1.0"

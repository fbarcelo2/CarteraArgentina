"""The local web surface. Import ``build_app`` from here."""

from cartera.web.app import PENDING_FEATURES, assert_loopback, build_app, resolve_web_token

__all__ = ["PENDING_FEATURES", "assert_loopback", "build_app", "resolve_web_token"]

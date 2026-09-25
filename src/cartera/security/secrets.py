"""Secret storage: OS keyring with an explicit, honest fallback.

Design decision (see docs/adr/0002-secrets-hydration.md): a plaintext ``.env``
may be used **once**, to bootstrap an installation. The hydration command stores
each secret in the OS keyring, **reads it back to verify**, and only then offers
to delete the file. Inside a container there is no Secret Service, so keyring is
unavailable — in that case hydration refuses to delete anything and tells the
operator to inject secrets at runtime, because deleting a mounted file would be
a lie about what is protected.
"""

from __future__ import annotations

from dataclasses import dataclass

SERVICE_NAME = "cartera-argentina"

#: Variables treated as secrets. Everything else in ``.env`` is plain config.
SECRET_KEYS: tuple[str, ...] = (
    "CARTERA_LLM_API_KEY",
    "CARTERA_WEB_TOKEN",
)


@dataclass(frozen=True)
class SecretStatus:
    key: str
    stored: bool
    verified: bool
    backend: str | None
    detail: str | None = None


def _keyring():  # pragma: no cover - depends on host
    try:
        import keyring

        return keyring
    except Exception:
        return None


def keyring_backend() -> str | None:
    """Name of the active keyring backend, or ``None`` when unavailable."""
    keyring = _keyring()
    if keyring is None:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:
        return None
    name = type(backend).__name__
    if "fail" in name.lower() or "null" in name.lower():
        return None
    return name


def keyring_available() -> bool:
    return keyring_backend() is not None


def get_secret(key: str) -> str | None:
    keyring = _keyring()
    if keyring is None:
        return None
    try:
        return keyring.get_password(SERVICE_NAME, key)
    except Exception:
        return None


def _unavailable(key: str) -> SecretStatus:
    """The status for "this host has no usable secret store"."""
    return SecretStatus(
        key=key,
        stored=False,
        verified=False,
        backend=None,
        detail="no OS keyring available (containers have no Secret Service)",
    )


def store_secret(key: str, value: str) -> SecretStatus:
    """Store a secret and verify the round-trip before reporting success.

    The backend name and the module are resolved together and checked in one
    guard: an ``assert`` for narrowing is stripped under ``python -O``, which
    would leave the next line to fail with an unrelated error.
    """
    backend = keyring_backend()
    keyring = _keyring()
    if backend is None or keyring is None:
        return _unavailable(key)

    try:
        keyring.set_password(SERVICE_NAME, key, value)
    except Exception as exc:
        return SecretStatus(key=key, stored=False, verified=False, backend=backend, detail=str(exc))
    read_back = keyring.get_password(SERVICE_NAME, key)
    return SecretStatus(
        key=key,
        stored=True,
        verified=read_back == value,
        backend=backend,
        detail=None if read_back == value else "read-back did not match the stored value",
    )


def delete_secret(key: str) -> bool:
    keyring = _keyring()
    if keyring is None:
        return False
    try:
        keyring.delete_password(SERVICE_NAME, key)
    except Exception:
        return False
    return get_secret(key) is None

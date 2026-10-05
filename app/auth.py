"""Accesso con password per l'uso online.

- La sessione è un cookie firmato con HMAC (HttpOnly, SameSite=Lax, Secure su HTTPS).
- Cambiando APP_PASSWORD tutte le sessioni esistenti decadono.
- Dopo 5 tentativi errati dallo stesso indirizzo, l'accesso è bloccato per 5 minuti.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

from .config import Settings

COOKIE_NAME = "lezionami_sessione"
MAX_FAILURES = 5
LOCK_SECONDS = 300

_random_secret = secrets.token_hex(32)  # usato se SESSION_SECRET non è impostato
_failures: dict[str, tuple[int, float]] = {}
_lock = threading.Lock()


def _secret(settings: Settings) -> bytes:
    return (settings.session_secret or _random_secret).encode()


def _signature(settings: Settings, expiry: int) -> str:
    password_digest = hashlib.sha256(settings.app_password.encode()).hexdigest()
    message = f"{expiry}:{password_digest}".encode()
    return hmac.new(_secret(settings), message, hashlib.sha256).hexdigest()


def make_token(settings: Settings) -> str:
    expiry = int(time.time()) + settings.session_hours * 3600
    return f"{expiry}.{_signature(settings, expiry)}"


def valid_token(settings: Settings, token: str | None) -> bool:
    if not settings.auth_enabled:
        return True
    if not token or "." not in token:
        return False
    raw_expiry, signature = token.split(".", 1)
    try:
        expiry = int(raw_expiry)
    except ValueError:
        return False
    if expiry < time.time():
        return False
    return hmac.compare_digest(signature, _signature(settings, expiry))


def check_password(settings: Settings, candidate: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode()).digest(),
        hashlib.sha256(settings.app_password.encode()).digest(),
    )


def locked_for(client: str) -> int:
    """Secondi di blocco rimanenti per questo indirizzo (0 se non bloccato)."""
    with _lock:
        count, since = _failures.get(client, (0, 0.0))
        if count >= MAX_FAILURES:
            remaining = int(since + LOCK_SECONDS - time.time())
            if remaining > 0:
                return remaining
            _failures.pop(client, None)
    return 0


def register_failure(client: str) -> None:
    with _lock:
        count, _ = _failures.get(client, (0, 0.0))
        _failures[client] = (count + 1, time.time())


def register_success(client: str) -> None:
    with _lock:
        _failures.pop(client, None)

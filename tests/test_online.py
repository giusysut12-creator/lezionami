"""Modalità online: password obbligatoria, sessioni, limiti di utilizzo."""

import os

import pytest
import requests

from app import auth
from tests.conftest import FIXTURES


@pytest.fixture()
def online(app_url, mock_state):
    saved = {k: os.environ.get(k) for k in ("HOST", "APP_PASSWORD")}
    os.environ["HOST"] = "0.0.0.0"
    os.environ["APP_PASSWORD"] = "parola-segreta-di-prova"
    auth._failures.clear()
    yield app_url
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    auth._failures.clear()


def _login(app_url, password="parola-segreta-di-prova"):
    session = requests.Session()
    res = session.post(f"{app_url}/api/login", data={"password": password}, timeout=10)
    return session, res


def test_online_without_password_is_blocked(online):
    os.environ.pop("APP_PASSWORD")
    res = requests.get(f"{online}/", timeout=10)
    assert res.status_code == 503 and "APP_PASSWORD" in res.text
    assert requests.post(f"{online}/api/extract", data={"transcript_text": "x" * 50}, timeout=10).status_code == 503
    assert requests.get(f"{online}/healthz", timeout=10).status_code == 200


def test_requires_login(online):
    res = requests.get(f"{online}/", allow_redirects=False, timeout=10)
    assert res.status_code == 303 and res.headers["location"] == "/login"
    assert requests.get(f"{online}/login", timeout=10).status_code == 200
    res = requests.get(f"{online}/api/config", timeout=10)
    assert res.status_code == 401 and res.json()["login"] is True
    res = requests.post(f"{online}/api/extract", data={"transcript_text": "x" * 50}, timeout=10)
    assert res.status_code == 401


def test_wrong_password_then_login_and_full_flow(online, mock_state):
    from tests.test_app_flow import process

    _, res = _login(online, "sbagliata")
    assert res.status_code == 401 and "non corretta" in res.json()["detail"]
    session, res = _login(online)
    assert res.status_code == 200
    assert "httponly" in res.headers["set-cookie"].lower() and "samesite=lax" in res.headers["set-cookie"].lower()
    config = session.get(f"{online}/api/config", timeout=10).json()
    assert config["auth_enabled"] and config["public_mode"]
    state = process(online, FIXTURES / "breve.txt", session=session)
    assert len(state["final"]["files"]) == 3
    # Senza cookie i passi non sono eseguibili.
    res = requests.post(f"{online}/api/step", json={"step": "inventory", "args": {}, "state": state}, timeout=10)
    assert res.status_code == 401
    session.post(f"{online}/api/logout", timeout=10)
    assert session.get(f"{online}/api/config", timeout=10).status_code == 401


def test_password_change_invalidates_sessions(online):
    session, _ = _login(online)
    assert session.get(f"{online}/api/config", timeout=10).status_code == 200
    os.environ["APP_PASSWORD"] = "nuova-password"
    assert session.get(f"{online}/api/config", timeout=10).status_code == 401


def test_forged_cookie_rejected(online):
    session = requests.Session()
    session.cookies.set(auth.COOKIE_NAME, "9999999999.abc")
    assert session.get(f"{online}/api/config", timeout=10).status_code == 401


def test_vercel_is_detected_as_online(app_url):
    from app.config import get_settings

    os.environ["VERCEL"] = "1"
    try:
        settings = get_settings()
        assert settings.public_mode and settings.on_vercel and settings.max_upload_mb == 4
        os.environ["APP_PASSWORD"] = "x"
        # Chiave di sessione stabile tra istanze serverless anche senza SESSION_SECRET.
        assert get_settings().session_secret == get_settings().session_secret != ""
    finally:
        os.environ.pop("VERCEL")
        os.environ.pop("APP_PASSWORD", None)


def test_lockout_after_repeated_failures(online):
    for _ in range(auth.MAX_FAILURES):
        _login(online, "sbagliata")
    _, res = _login(online)  # anche la password giusta è bloccata per un po'
    assert res.status_code == 429 and "Troppi tentativi" in res.json()["detail"]


def test_local_mode_needs_no_password(app_url):
    assert requests.get(f"{app_url}/api/config", timeout=10).json()["auth_enabled"] is False

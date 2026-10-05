"""Configurazione dell'applicazione, letta da variabili d'ambiente o dal file .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent


def load_dotenv(path: Path = ROOT_DIR / ".env") -> None:
    """Carica un file .env minimale (CHIAVE=valore) senza sovrascrivere l'ambiente."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "si", "sì", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Prezzi indicativi in dollari per milione di token (input, output), usati solo
# per la stima dei costi mostrata nell'interfaccia. Aggiornabili via variabili.
KNOWN_PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
}


@dataclass
class Settings:
    api_key_configured: bool
    model: str
    effort: str
    refusal_fallback: bool
    max_output_tokens: int
    web_search_tool: str
    web_fetch_tool: str
    web_search_max_uses: int
    host: str
    port: int
    max_upload_mb: int
    job_ttl_minutes: int
    single_pass_max_chars: int
    segment_max_chars: int
    lesson_source_max_chars: int
    parallel_requests: int
    show_source_refs: bool
    price_input: float | None
    price_output: float | None


def get_settings() -> Settings:
    model = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5").strip() or "claude-opus-5-5"
    price_in, price_out = KNOWN_PRICES.get(model, (None, None))
    if os.environ.get("PRICE_INPUT_PER_MTOK"):
        price_in = float(os.environ["PRICE_INPUT_PER_MTOK"])
    if os.environ.get("PRICE_OUTPUT_PER_MTOK"):
        price_out = float(os.environ["PRICE_OUTPUT_PER_MTOK"])
    return Settings(
        api_key_configured=bool(
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        ),
        model=model,
        effort=os.environ.get("CLAUDE_EFFORT", "high").strip() or "high",
        refusal_fallback=_bool("CLAUDE_REFUSAL_FALLBACK", True),
        max_output_tokens=_int("CLAUDE_MAX_OUTPUT_TOKENS", 120000),
        web_search_tool=os.environ.get("WEB_SEARCH_TOOL", "web_search_20260209"),
        web_fetch_tool=os.environ.get("WEB_FETCH_TOOL", "web_fetch_20260209"),
        web_search_max_uses=_int("WEB_SEARCH_MAX_USES", 8),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=_int("PORT", 8000),
        max_upload_mb=_int("MAX_UPLOAD_MB", 30),
        job_ttl_minutes=_int("JOB_TTL_MINUTES", 120),
        single_pass_max_chars=_int("SINGLE_PASS_MAX_CHARS", 60000),
        segment_max_chars=_int("SEGMENT_MAX_CHARS", 40000),
        lesson_source_max_chars=_int("LESSON_SOURCE_MAX_CHARS", 600000),
        parallel_requests=max(1, _int("PARALLEL_REQUESTS", 3)),
        show_source_refs=_bool("SHOW_SOURCE_REFS", True),
        price_input=price_in,
        price_output=price_out,
    )

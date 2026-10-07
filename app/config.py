"""Configurazione dell'applicazione, letta da variabili d'ambiente o dal file .env."""

from __future__ import annotations

import hashlib
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
    effort_analysis: str
    step_deadline_seconds: int
    refusal_fallback: bool
    max_output_tokens: int
    host: str
    port: int
    max_upload_mb: int
    single_pass_max_chars: int
    segment_max_chars: int
    lesson_source_max_chars: int
    parallel_requests: int
    lesson_part_topics: int
    show_source_refs: bool
    price_input: float | None
    price_output: float | None
    app_password: str
    session_secret: str
    session_hours: int
    public_mode: bool
    on_vercel: bool
    revise_all_issues: bool

    @property
    def auth_enabled(self) -> bool:
        return bool(self.app_password)


def get_settings() -> Settings:
    on_vercel = bool(os.environ.get("VERCEL"))
    app_password = os.environ.get("APP_PASSWORD", "")
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5").strip() or "claude-sonnet-5-5"
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
        # Ragionamento medio: buon equilibrio tra qualità, tempi e costi.
        effort=os.environ.get("CLAUDE_EFFORT", "medium").strip() or "medium",
        effort_analysis=os.environ.get("CLAUDE_EFFORT_ANALYSIS", "medium").strip() or "medium",
        # Su Vercel ogni passo si ferma da solo prima del limite di 300 secondi.
        step_deadline_seconds=_int("STEP_DEADLINE_SECONDS", 285 if on_vercel else 0),
        refusal_fallback=_bool("CLAUDE_REFUSAL_FALLBACK", True),
        max_output_tokens=_int("CLAUDE_MAX_OUTPUT_TOKENS", 120000),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=_int("PORT", 8000),
        # Su Vercel richiesta e risposta di una funzione non possono superare 4,5 MB.
        max_upload_mb=_int("MAX_UPLOAD_MB", 4 if on_vercel else 30),
        # Soglie pensate perché ogni chiamata resti entro il tempo massimo di una funzione serverless.
        single_pass_max_chars=_int("SINGLE_PASS_MAX_CHARS", 12000),
        segment_max_chars=_int("SEGMENT_MAX_CHARS", 10000),
        lesson_source_max_chars=_int("LESSON_SOURCE_MAX_CHARS", 600000),
        parallel_requests=max(1, _int("PARALLEL_REQUESTS", 3)),
        lesson_part_topics=max(2, _int("LESSON_PART_TOPICS", 3)),
        show_source_refs=_bool("SHOW_SOURCE_REFS", True),
        price_input=price_in,
        price_output=price_out,
        app_password=app_password,
        session_secret=os.environ.get("SESSION_SECRET", "") or _derived_secret(app_password),
        session_hours=max(1, _int("SESSION_HOURS", 12)),
        # Online (Vercel, Render o HOST diverso da localhost) l'app richiede una password.
        public_mode=on_vercel or bool(os.environ.get("RENDER"))
        or os.environ.get("HOST", "127.0.0.1").strip() not in ("127.0.0.1", "localhost", "::1"),
        on_vercel=on_vercel,
        # Revisione automatica: solo errori gravi (predefinito, più economico) o anche problemi medi.
        revise_all_issues=_bool("REVISIONE_COMPLETA", False),
    )


def _derived_secret(app_password: str) -> str:
    """Chiave di firma stabile tra le istanze serverless, se SESSION_SECRET non è impostata."""
    if not app_password:
        return ""
    material = f"lezionami|{app_password}|{os.environ.get('ANTHROPIC_API_KEY', '')}"
    return hashlib.sha256(material.encode()).hexdigest()

"""Integrazione con l'API di Claude (solo backend).

La chiave è letta dall'SDK dalla variabile d'ambiente ANTHROPIC_API_KEY e non
viene mai inviata al frontend né scritta nei log. Nei log finiscono soltanto
fase, durata, numero di token ed eventuale tipo di errore.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field

import anthropic

from .config import Settings

log = logging.getLogger("lezionami.llm")

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    def __init__(self, user_message: str, retryable: bool = True, kind: str = "api"):
        super().__init__(user_message)
        self.user_message = user_message
        self.retryable = retryable
        self.kind = kind


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    calls: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, usage) -> None:
        if usage is None:
            return
        with self.lock:
            self.calls += 1
            self.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.output_tokens += getattr(usage, "output_tokens", 0) or 0
            self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
            self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

    def estimated_cost(self, price_in: float | None, price_out: float | None) -> float | None:
        if price_in is None or price_out is None:
            return None
        return (
            self.input_tokens * price_in
            + self.cache_write_tokens * price_in * 1.25
            + self.cache_read_tokens * price_in * 0.1
            + self.output_tokens * price_out
        ) / 1_000_000

    def as_dict(self) -> dict:
        return {
            "chiamate": self.calls,
            "token_input": self.input_tokens,
            "token_output": self.output_tokens,
            "token_cache_scrittura": self.cache_write_tokens,
            "token_cache_lettura": self.cache_read_tokens,
        }


def friendly_error(exc: Exception) -> LLMError:
    """Traduce le eccezioni dell'SDK in messaggi comprensibili."""
    if isinstance(exc, LLMError):
        return exc
    detail = ""
    if isinstance(exc, anthropic.APIStatusError):
        try:
            body = exc.body if isinstance(exc.body, dict) else {}
            detail = (body.get("error") or {}).get("message", "") or str(exc.message)
        except Exception:
            detail = str(getattr(exc, "message", ""))
    lowered = detail.lower()
    if isinstance(exc, anthropic.AuthenticationError):
        return LLMError("La chiave API non è valida o è stata revocata. Controlla ANTHROPIC_API_KEY nel file .env e riavvia l'applicazione.", retryable=False, kind="auth")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return LLMError("La chiave API non ha i permessi per questa operazione (o la funzione richiesta non è abilitata per l'organizzazione).", retryable=False, kind="permission")
    if isinstance(exc, anthropic.NotFoundError):
        return LLMError("Il modello configurato non è disponibile. Controlla CLAUDE_MODEL nel file .env.", retryable=False, kind="model")
    if isinstance(exc, anthropic.RateLimitError):
        return LLMError("Limite di richieste dell'API raggiunto. Attendi qualche minuto e premi «Riprova».", kind="rate_limit")
    if isinstance(exc, anthropic.BadRequestError):
        if "credit balance" in lowered or "billing" in lowered:
            return LLMError("Credito API insufficiente: ricarica il credito nella Console Anthropic e premi «Riprova».", kind="billing")
        if "prompt is too long" in lowered or "too long" in lowered:
            return LLMError("Il testo è troppo lungo per una singola richiesta. Riduci SEGMENT_MAX_CHARS o LESSON_SOURCE_MAX_CHARS nel file .env.", retryable=False, kind="too_long")
        return LLMError(f"Richiesta rifiutata dall'API: {detail[:300] or 'parametri non validi'}.", retryable=False, kind="bad_request")
    if isinstance(exc, anthropic.APITimeoutError):
        return LLMError("L'API non ha risposto in tempo. Premi «Riprova».", kind="timeout")
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMError("Impossibile contattare l'API di Claude. Verifica la connessione a Internet e premi «Riprova».", kind="connection")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 529 or "overloaded" in lowered:
            return LLMError("Il servizio AI è momentaneamente sovraccarico. Riprova tra poco.", kind="overloaded")
        if exc.status_code >= 500:
            return LLMError(f"Errore temporaneo del servizio AI (codice {exc.status_code}). Premi «Riprova».", kind="server")
        return LLMError(f"Errore dell'API (codice {exc.status_code}): {detail[:300]}", kind="api")
    return LLMError(f"Errore imprevisto durante la chiamata all'API: {type(exc).__name__}.", kind="unknown")


def parse_json_text(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            candidate = text[start:end + 1]
    if candidate is None:
        raise ValueError("nessun JSON nella risposta")
    return json.loads(candidate)


class ClaudeClient:
    def __init__(self, settings: Settings, usage: Usage, deadline: float | None = None):
        self.settings = settings
        self.usage = usage
        # Istante (time.monotonic) oltre il quale la chiamata viene interrotta, per
        # chiudere il passo in modo pulito prima del limite della piattaforma serverless.
        self.deadline = deadline
        self._client: anthropic.Anthropic | None = None
        self._fallback_enabled = settings.refusal_fallback
        self._structured_enabled = True

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            if not self.settings.api_key_configured:
                raise LLMError(
                    "Chiave API non configurata. Copia .env.example in .env, inserisci ANTHROPIC_API_KEY e riavvia l'applicazione.",
                    retryable=False, kind="auth",
                )
            self._client = anthropic.Anthropic(max_retries=3)
        return self._client

    def _base_params(self, system: str, user_content: str | list, max_tokens: int | None,
                     effort: str | None = None) -> dict:
        params: dict = {
            "model": self.settings.model,
            "max_tokens": max_tokens or self.settings.max_output_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user_content}],
        }
        output_config: dict = {}
        effort = effort or self.settings.effort
        if effort and not self.settings.model.startswith("claude-haiku"):
            output_config["effort"] = effort
        if output_config:
            params["output_config"] = output_config
        return params

    def _deadline_error(self) -> LLMError:
        return LLMError(
            f"La risposta dell'AI non è arrivata entro {self.settings.step_deadline_seconds} secondi, "
            "il tempo massimo per passo su questo server. Premi «Riprova»; se si ripete, imposta "
            "CLAUDE_EFFORT=medium oppure riduci SEGMENT_MAX_CHARS nelle variabili d'ambiente.",
            retryable=True, kind="deadline",
        )

    def _consume(self, stream):
        """Legge lo stream; allo scadere del tempo del passo risponde subito con un errore."""
        if self.deadline is None:
            return stream.get_final_message()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise self._deadline_error()
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(stream.get_final_message)
        try:
            return future.result(timeout=remaining)
        except FutureTimeout:
            try:  # interrompe la generazione per non pagare token inutili
                stream.close()
            except Exception:
                pass
            raise self._deadline_error() from None
        finally:
            pool.shutdown(wait=False)

    def _stream(self, params: dict):
        if self._fallback_enabled:
            try:
                with self.client.beta.messages.stream(
                    **params, betas=[FALLBACK_BETA], fallbacks="default"
                ) as stream:
                    return self._consume(stream)
            except anthropic.BadRequestError as exc:
                if "fallback" not in str(exc).lower():
                    raise
                log.warning("fallback lato server non accettato: disattivato per questa sessione")
                self._fallback_enabled = False
        with self.client.messages.stream(**params) as stream:
            return self._consume(stream)

    def _check_stop(self, message, label: str) -> None:
        if message.stop_reason == "refusal":
            raise LLMError(
                f"Il modello ha rifiutato di completare la fase «{label}». Verifica il contenuto caricato e riprova.",
                retryable=True, kind="refusal",
            )
        if message.stop_reason == "max_tokens":
            raise LLMError(
                f"La risposta della fase «{label}» è stata troncata per limite di lunghezza. Aumenta CLAUDE_MAX_OUTPUT_TOKENS e premi «Riprova».",
                retryable=True, kind="max_tokens",
            )

    def call_json(self, system: str, user_content: str | list, schema: dict, label: str,
                  max_tokens: int | None = None, effort: str | None = None) -> dict:
        started = time.monotonic()
        params = self._base_params(system, user_content, max_tokens, effort)
        use_structured = self._structured_enabled
        if use_structured:
            params.setdefault("output_config", {})["format"] = {"type": "json_schema", "schema": schema}
        else:
            instruction = (
                "Rispondi solo con un oggetto JSON valido conforme a questo schema:\n"
                + json.dumps(schema, ensure_ascii=False)
            )
            if isinstance(user_content, list):
                params["messages"][0]["content"] = user_content + [{"type": "text", "text": instruction}]
            else:
                params["messages"][0]["content"] = f"{user_content}\n\n{instruction}"
        try:
            message = self._stream(params)
        except anthropic.BadRequestError as exc:
            text = str(exc).lower()
            if use_structured and ("schema" in text or "output_config" in text or "format" in text):
                log.warning("output strutturati non accettati (%s): ripiego su JSON da prompt", label)
                self._structured_enabled = False
                return self.call_json(system, user_content, schema, label, max_tokens, effort)
            raise friendly_error(exc) from exc
        except LLMError:
            raise
        except Exception as exc:  # errori SDK e di rete
            raise friendly_error(exc) from exc
        self.usage.add(message.usage)
        self._check_stop(message, label)
        text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
        try:
            data = parse_json_text(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise LLMError(
                f"La risposta della fase «{label}» non è in un formato valido. Premi «Riprova».",
                kind="invalid_output",
            ) from exc
        log.info("fase=%s durata=%.1fs token_in=%s token_out=%s", label, time.monotonic() - started,
                 getattr(message.usage, "input_tokens", "?"), getattr(message.usage, "output_tokens", "?"))
        return data

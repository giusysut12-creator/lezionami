"""Server web (FastAPI) senza stato: uso locale oppure online (Vercel, Render...) con password.

Il server non conserva trascrizioni né documenti: il browser invia a ogni passo
i dati necessari e riceve il risultato. Su Vercel l'app è esposta da index.py.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import auth
from .config import APP_DIR, get_settings, load_dotenv
from .extract import SUPPORTED, ExtractionError
from .llm import LLMError
from .steps import StepError, Steps, extract_inputs

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# L'SDK non deve mai scrivere richieste o contenuti nei log.
logging.getLogger("anthropic").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lezionami")

app = FastAPI(title="Lezionami", docs_url=None, redoc_url=None, openapi_url=None)
STATIC_DIR = APP_DIR / "static"
PUBLIC_PATHS = ("/login", "/api/login", "/healthz", "/static/")


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/")


@app.middleware("http")
async def access_control(request: Request, call_next):
    settings = get_settings()
    path = request.url.path
    if not path.startswith(PUBLIC_PATHS):
        if settings.public_mode and not settings.auth_enabled:
            # Online senza password chiunque potrebbe consumare il credito API.
            message = "Configurazione incompleta: imposta la variabile d'ambiente APP_PASSWORD sul server per abilitare l'accesso."
            if _wants_json(request):
                return JSONResponse({"detail": message}, status_code=503)
            return Response(message, status_code=503, media_type="text/plain; charset=utf-8")
        if not auth.valid_token(settings, request.cookies.get(auth.COOKIE_NAME)):
            if _wants_json(request):
                return JSONResponse({"detail": "Sessione scaduta: accedi di nuovo.", "login": True}, status_code=401)
            return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/login", response_model=None)
def login_page(request: Request):
    settings = get_settings()
    if not settings.auth_enabled or auth.valid_token(settings, request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/api/login")
def login(request: Request, password: Annotated[str, Form()] = "") -> Response:
    settings = get_settings()
    if not settings.auth_enabled:
        return JSONResponse({"ok": True})
    client = request.client.host if request.client else "sconosciuto"
    wait = auth.locked_for(client)
    if wait:
        raise HTTPException(429, f"Troppi tentativi errati. Riprova tra {wait // 60 + 1} minuti.")
    if not auth.check_password(settings, password):
        auth.register_failure(client)
        time.sleep(0.5)
        raise HTTPException(401, "Password non corretta.")
    auth.register_success(client)
    response = JSONResponse({"ok": True})
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(auth.COOKIE_NAME, auth.make_token(settings), max_age=settings.session_hours * 3600,
                        httponly=True, samesite="lax", secure=secure, path="/")
    return response


@app.post("/api/logout")
def logout() -> Response:
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


@app.get("/api/config")
def config() -> dict:
    settings = get_settings()
    return {
        "api_key_configured": settings.api_key_configured,
        "auth_enabled": settings.auth_enabled,
        "public_mode": settings.public_mode,
        "model": settings.model,
        "max_upload_mb": settings.max_upload_mb,
        "parallel_requests": settings.parallel_requests,
        "formats": sorted(SUPPORTED),
    }


async def _read_upload(upload: UploadFile, limit: int) -> tuple[str, bytes]:
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"Il file «{upload.filename}» supera il limite di {limit // (1024 * 1024)} MB.")
    return upload.filename or "file", data


@app.post("/api/extract")
async def extract(
    transcript_file: Annotated[UploadFile | None, File()] = None,
    transcript_text: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    lesson_date: Annotated[str, Form()] = "",
    recipient: Annotated[str, Form()] = "",
) -> dict:
    """Passo 1: estrazione e controllo del testo (nessuna chiamata AI)."""
    settings = get_settings()
    limit = settings.max_upload_mb * 1024 * 1024
    transcript = None
    if transcript_file is not None and transcript_file.filename:
        transcript = await _read_upload(transcript_file, limit)
    elif len(transcript_text.strip()) < 20:
        raise HTTPException(400, "Carica un file con la trascrizione oppure incolla il testo (almeno qualche frase).")
    meta = {"title": title, "lesson_date": lesson_date, "recipient": recipient}
    try:
        return extract_inputs(settings, transcript, transcript_text if transcript is None else "", meta)
    except ExtractionError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/step")
def run_step(
    step: Annotated[str, Body()],
    state: Annotated[dict[str, Any], Body()],
    args: Annotated[dict[str, Any] | None, Body()] = None,
) -> Response:
    """Esegue un singolo passo (al massimo una chiamata all'API di Claude)."""
    settings = get_settings()
    started = time.monotonic()
    try:
        output = Steps(settings, state).run(step, args or {})
    except LLMError as exc:
        log.warning("passo=%s errore_api=%s", step, exc.kind)
        return JSONResponse({"detail": exc.user_message, "retryable": exc.retryable, "kind": exc.kind}, status_code=502)
    except StepError as exc:
        return JSONResponse({"detail": exc.message, "retryable": exc.retryable, "kind": exc.kind}, status_code=exc.status)
    except Exception as exc:  # nessun contenuto nei log, solo il tipo di errore
        log.exception("passo=%s errore_interno=%s", step, type(exc).__name__)
        return JSONResponse({"detail": f"Errore interno nel passo «{step}» ({type(exc).__name__}). Premi «Riprova».",
                             "retryable": True, "kind": "internal"}, status_code=500)
    log.info("passo=%s durata=%.1fs", step, time.monotonic() - started)
    return JSONResponse(output)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(HTTPException)
async def http_error(_, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

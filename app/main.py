"""Server web locale (FastAPI)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import APP_DIR, get_settings, load_dotenv
from .extract import SUPPORTED
from .pdf_render import file_name
from .pipeline import JobStore, Upload

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# L'SDK non deve mai scrivere richieste o contenuti nei log.
logging.getLogger("anthropic").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI(title="Lezionami", docs_url=None, redoc_url=None, openapi_url=None)
store = JobStore()
STATIC_DIR = APP_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _cleanup_loop() -> None:
    while True:
        time.sleep(300)
        store.cleanup(get_settings().job_ttl_minutes)


threading.Thread(target=_cleanup_loop, name="cleanup", daemon=True).start()


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def config() -> dict:
    settings = get_settings()
    return {
        "api_key_configured": settings.api_key_configured,
        "model": settings.model,
        "max_upload_mb": settings.max_upload_mb,
        "job_ttl_minutes": settings.job_ttl_minutes,
        "formats": sorted(SUPPORTED),
    }


async def _read_upload(upload: UploadFile, limit: int) -> Upload:
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"Il file «{upload.filename}» supera il limite di {limit // (1024 * 1024)} MB.")
    return Upload(name=upload.filename or "file", data=data)


@app.post("/api/jobs")
async def create_job(
    transcript_file: Annotated[UploadFile | None, File()] = None,
    transcript_text: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    lesson_date: Annotated[str, Form()] = "",
    recipient: Annotated[str, Form()] = "",
    web_search: Annotated[str, Form()] = "false",
    sources: Annotated[list[UploadFile] | None, File()] = None,
) -> dict:
    settings = get_settings()
    limit = settings.max_upload_mb * 1024 * 1024
    transcript_upload = None
    if transcript_file is not None and transcript_file.filename:
        transcript_upload = await _read_upload(transcript_file, limit)
    elif len(transcript_text.strip()) < 20:
        raise HTTPException(400, "Carica un file con la trascrizione oppure incolla il testo (almeno qualche frase).")
    source_uploads = []
    for upload in sources or []:
        if upload.filename:
            source_uploads.append(await _read_upload(upload, limit))
    if len(source_uploads) > 10:
        raise HTTPException(400, "Puoi allegare al massimo 10 fonti aggiuntive.")
    job = store.create(
        settings=settings,
        title=title.strip()[:150],
        lesson_date=lesson_date.strip()[:60],
        recipient=recipient.strip()[:200],
        web_search=web_search.lower() in ("true", "1", "on", "si", "sì"),
        transcript_upload=transcript_upload,
        pasted_text=transcript_text if transcript_upload is None else "",
        source_uploads=source_uploads,
    )
    store.start(job)
    return {"job_id": job.id}


def _job_or_404(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "Elaborazione non trovata o già eliminata (i dati restano in memoria solo temporaneamente).")
    return job


@app.get("/api/jobs/{job_id}")
def job_state(job_id: str) -> dict:
    return _job_or_404(job_id).public_state()


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str) -> dict:
    job = _job_or_404(job_id)
    if job.status != "errore":
        raise HTTPException(409, "L'elaborazione non è in errore.")
    if job.error and not job.error.get("retryable", True) and job.error.get("phase") == "estrazione":
        raise HTTPException(409, "Il file caricato non può essere elaborato: avvia una nuova elaborazione con un file diverso.")
    # Ricarica la configurazione (es. chiave API o modello corretti nel file .env).
    load_dotenv()
    new_settings = get_settings()
    job.settings = new_settings
    store.start(job)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/files/{kind}")
def download(job_id: str, kind: str) -> Response:
    job = _job_or_404(job_id)
    if job.status != "completato":
        raise HTTPException(409, "I documenti non sono ancora pronti.")
    if kind == "zip":
        name = f"Documenti_{file_name('lezione', job.final_title)[len('Lezione_completa_'):-4]}.zip"
        return Response(job.zip_bytes, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    if kind == "report":
        return Response(job.report_text.encode("utf-8"), media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="Report_verifica.txt"'})
    if kind not in job.pdfs:
        raise HTTPException(404, "Documento non trovato.")
    return Response(job.pdfs[kind], media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{file_name(kind, job.final_title)}"'})


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    job = store.get(job_id)
    if job and job.thread and job.thread.is_alive():
        raise HTTPException(409, "Attendi la fine dell'elaborazione prima di eliminare i dati.")
    store.delete(job_id)
    return {"ok": True}


@app.exception_handler(HTTPException)
async def http_error(_, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

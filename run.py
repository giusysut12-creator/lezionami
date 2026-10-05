"""Avvio locale: python run.py  (poi apri http://127.0.0.1:8000)."""

import webbrowser
import threading

import uvicorn

from app.config import get_settings, load_dotenv

if __name__ == "__main__":
    load_dotenv()
    settings = get_settings()
    url = f"http://{'127.0.0.1' if settings.host in ('0.0.0.0', '::') else settings.host}:{settings.port}"
    print(f"\n  Lezionami è in avvio su {url}")
    print(f"  Modello: {settings.model}")
    if not settings.api_key_configured:
        print("  ATTENZIONE: ANTHROPIC_API_KEY non è configurata (vedi il file .env.example).")
    print("  Per chiudere premi CTRL+C.\n")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info", access_log=False)

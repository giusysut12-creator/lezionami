#!/usr/bin/env bash
# Avvio su macOS / Linux: crea l'ambiente virtuale la prima volta, poi avvia l'app.
set -e
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  echo "Prima installazione: creo l'ambiente Python..."
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip >/dev/null
  ./.venv/bin/pip install -r requirements.txt
fi
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "Ho creato il file .env: aprilo, inserisci ANTHROPIC_API_KEY e rilancia ./avvia.sh"
  exit 0
fi
./.venv/bin/python run.py

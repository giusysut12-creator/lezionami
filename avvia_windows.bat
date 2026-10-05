@echo off
REM Avvio su Windows: crea l'ambiente virtuale la prima volta, poi avvia l'app.
cd /d "%~dp0"
if not exist ".venv" (
  echo Prima installazione: creo l'ambiente Python...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist ".env" (
  copy .env.example .env >nul
  echo Ho creato il file .env: aprilo con il Blocco note, inserisci ANTHROPIC_API_KEY e rilancia avvia_windows.bat
  pause
  exit /b 0
)
.venv\Scripts\python run.py
pause

"""Generazione da riga di comando, senza interfaccia web.

Esempio:
    python genera.py tests/fixtures/breve.txt --titolo "Orizzonte Famiglia" --uscita output

Utile per le prove reali con la chiave API: salva i tre PDF, lo ZIP e il report.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from app.config import get_settings, load_dotenv
from app.pdf_render import file_name
from app.pipeline import DOC_KINDS, JobStore, Upload


def main() -> int:
    parser = argparse.ArgumentParser(description="Trasforma una trascrizione in tre PDF.")
    parser.add_argument("trascrizione", type=Path, help="File TXT, DOCX o PDF testuale")
    parser.add_argument("--titolo", default="")
    parser.add_argument("--data", default="")
    parser.add_argument("--destinatario", default="")
    parser.add_argument("--fonte", type=Path, action="append", default=[], help="Documento ufficiale aggiuntivo (ripetibile)")
    parser.add_argument("--web", action="store_true", help="Attiva la verifica su fonti ufficiali online")
    parser.add_argument("--uscita", type=Path, default=Path("output"))
    args = parser.parse_args()

    load_dotenv()
    settings = get_settings()
    store = JobStore()
    job = store.create(
        settings=settings, title=args.titolo, lesson_date=args.data, recipient=args.destinatario,
        web_search=args.web,
        transcript_upload=Upload(args.trascrizione.name, args.trascrizione.read_bytes()),
        pasted_text="", source_uploads=[Upload(p.name, p.read_bytes()) for p in args.fonte],
    )
    print(f"Modello: {settings.model}")
    store.start(job)
    shown: dict[str, str] = {}
    while job.thread.is_alive():
        for phase in job.phases:
            line = f"{phase.status}: {phase.detail}"
            if phase.status != "attesa" and shown.get(phase.key) != line:
                shown[phase.key] = line
                print(f"  [{phase.status:>12}] {phase.label} — {phase.detail}")
        time.sleep(0.5)
    for phase in job.phases:
        line = f"{phase.status}: {phase.detail}"
        if shown.get(phase.key) != line and phase.status != "attesa":
            print(f"  [{phase.status:>12}] {phase.label} — {phase.detail}")
    for warning in job.warnings:
        print(f"  Avviso: {warning}")
    if job.status != "completato":
        print(f"\nERRORE: {job.error['message']}")
        return 1
    args.uscita.mkdir(parents=True, exist_ok=True)
    for kind in DOC_KINDS:
        target = args.uscita / file_name(kind, job.final_title)
        target.write_bytes(job.pdfs[kind])
        print(f"Salvato: {target} ({job.pdf_pages[kind]} pagine)")
    suffix = file_name("lezione", job.final_title)[len("Lezione_completa_"):-4]
    (args.uscita / f"Documenti_{suffix}.zip").write_bytes(job.zip_bytes)
    (args.uscita / f"Report_verifica_{suffix}.txt").write_text(job.report_text, encoding="utf-8")
    cost = job.usage.estimated_cost(settings.price_input, settings.price_output)
    print(f"Chiamate API: {job.usage.calls}" + (f" · costo stimato circa {cost:.2f} USD" if cost is not None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

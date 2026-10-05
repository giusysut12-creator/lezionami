"""Generazione da riga di comando, senza interfaccia web.

Esempio:
    python genera.py tests/fixtures/breve.txt --titolo "Orizzonte Famiglia" --uscita output

Utile per le prove reali con la chiave API: salva i tre PDF, lo ZIP e il report.
"""

from __future__ import annotations

import argparse
import base64
import sys
import zipfile
from pathlib import Path

from app.config import get_settings, load_dotenv
from app.extract import ExtractionError
from app.llm import LLMError
from app.orchestrator import StepFailed, run_all
from app.steps import StepError, Steps, estimated_cost, extract_inputs


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
    print(f"Modello: {settings.model}")
    try:
        state = extract_inputs(
            settings, (args.trascrizione.name, args.trascrizione.read_bytes()), "",
            [(p.name, p.read_bytes()) for p in args.fonte],
            {"title": args.titolo, "lesson_date": args.data, "recipient": args.destinatario, "web_search": args.web},
        )
    except ExtractionError as exc:
        print(f"ERRORE: {exc}")
        return 1
    print(f"  [estrazione] {state['detail']}")
    for warning in state["warnings"]:
        print(f"  Avviso: {warning}")

    def call(step: str, step_args: dict, snapshot: dict) -> dict:
        try:
            return Steps(settings, snapshot).run(step, step_args)
        except LLMError as exc:
            raise StepFailed(step, exc.user_message, exc.retryable) from exc
        except StepError as exc:
            raise StepFailed(step, exc.message, exc.retryable) from exc

    try:
        run_all(call, state, on_event=lambda phase, detail: print(f"  [{phase}] {detail}"),
                parallel=settings.parallel_requests)
    except StepFailed as exc:
        print(f"\nERRORE nel passo «{exc.step}»: {exc.detail}")
        return 1

    final = state["final"]
    args.uscita.mkdir(parents=True, exist_ok=True)
    zip_path = args.uscita / final["zip_name"]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in final["files"]:
            data = base64.b64decode(item["b64"])
            (args.uscita / item["filename"]).write_bytes(data)
            archive.writestr(item["filename"], data)
            print(f"Salvato: {args.uscita / item['filename']} ({item['pages']} pagine)")
        archive.writestr(final["report"]["filename"], final["report"]["text"])
    (args.uscita / final["report"]["filename"]).write_text(final["report"]["text"], encoding="utf-8")
    print(f"Salvato: {zip_path}")
    for warning in final["warnings"]:
        print(f"  Avviso: {warning}")
    for issue in final["issues"]:
        print(f"  Da verificare [{issue['gravita']}] {issue['documento']}: {issue['problema']}")
    usage = state.get("usage", {})
    cost = estimated_cost(usage, settings)
    print(f"Chiamate API: {usage.get('chiamate', 0)}" + (f" · costo stimato circa {cost:.2f} USD" if cost is not None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

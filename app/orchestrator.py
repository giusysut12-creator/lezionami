"""Sequenza dei passi, usata da riga di comando e nei test.

Il browser segue la stessa sequenza in app/static/app.js. Lo stato contiene
i risultati dei passi completati: rilanciando run_all sullo stesso stato si
riparte dal primo passo mancante, senza ripetere quelli già riusciti.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable


class StepFailed(Exception):
    def __init__(self, step: str, detail: str, retryable: bool = True):
        super().__init__(detail)
        self.step = step
        self.detail = detail
        self.retryable = retryable


Call = Callable[[str, dict, dict], dict]
USAGE_KEYS = ("chiamate", "token_input", "token_output", "token_cache_scrittura", "token_cache_lettura")


def _add_usage(state: dict, usage: dict) -> None:
    total = state.setdefault("usage", {k: 0 for k in USAGE_KEYS})
    for key in USAGE_KEYS:
        total[key] = total.get(key, 0) + (usage or {}).get(key, 0)


def _parallel(jobs: list[Callable[[], None]], workers: int) -> None:
    errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for future in [pool.submit(job) for job in jobs]:
            try:
                future.result()
            except Exception as exc:  # i risultati riusciti restano nello stato
                errors.append(exc)
    if errors:
        raise errors[0]


def run_all(call: Call, state: dict, on_event: Callable[[str, str], None] | None = None, parallel: int = 3) -> dict:
    event = on_event or (lambda phase, detail: None)
    lock = threading.Lock()

    def step(name: str, args: dict | None = None) -> dict:
        with lock:  # istantanea coerente dello stato mentre altri passi sono in corso
            snapshot = json.loads(json.dumps(state))
        output = call(name, args or {}, snapshot)
        with lock:
            _add_usage(state, output.get("usage"))
        return output["result"]

    # Inventario
    if not state.get("inventory"):
        plan = state.get("plan") or {}
        if plan.get("mode") == "segmented":
            partials = state.setdefault("inventory_partials", {})
            todo = [p for p in plan["parts"] if p["key"] not in partials]

            def make(part):
                def job():
                    result = step("inventory_part", {"key": part["key"]})
                    with lock:
                        partials[result["key"]] = result["inventory"]
                    event("inventario", f"Segmenti analizzati: {len(partials)} di {len(plan['parts'])}")
                return job

            _parallel([make(p) for p in todo], parallel)
            result = step("inventory_merge")
        else:
            result = step("inventory")
        state["inventory"], state["lesson_plan"] = result["inventory"], result["lesson_plan"]
        event("inventario", result["detail"])

    # Lezione a parti: la prima da sola (scrive la cache dei prompt), poi le altre in parallelo
    parts = state.setdefault("lesson_parts", {})
    plan_parts = state["lesson_plan"]

    def lesson_job(index: int):
        def job():
            result = step("lesson_part", {"index": index})
            with lock:
                parts[result["index"]] = result["doc"]
            event("generazione", f"Lezione: parte {len(parts)} di {len(plan_parts)}")
        return job

    if "0" not in parts:
        lesson_job(0)()
    _parallel([lesson_job(p["index"]) for p in plan_parts if str(p["index"]) not in parts], parallel)

    docs = state.setdefault("docs", {})

    def doc_job(kind: str, name: str):
        def job():
            doc = step(name)["doc"]
            with lock:
                docs[kind] = doc
            event("generazione", f"{kind} generata")
        return job

    _parallel([doc_job(k, n) for k, n in (("guida", "guide"), ("brochure", "brochure")) if k not in docs], parallel)

    # Controllo e revisione
    if not state.get("check"):
        state["check"] = step("check")
        event("controllo", state["check"]["detail"])
    revised = state.setdefault("revised", {})
    to_fix = state["check"]["to_fix"]
    jobs = []
    if to_fix.get("lezione"):
        lesson_revised = revised.setdefault("lezione", {})
        for part in plan_parts:
            key = str(part["index"])
            if key not in lesson_revised:
                def job(key=key):
                    doc = step("revise", {"kind": "lezione", "index": int(key)})["doc"]
                    with lock:
                        lesson_revised[key] = doc
                jobs.append(job)
    for kind in ("guida", "brochure"):
        if to_fix.get(kind) and kind not in revised:
            def job(kind=kind):
                doc = step("revise", {"kind": kind})["doc"]
                with lock:
                    revised[kind] = doc
            jobs.append(job)
    _parallel(jobs, parallel)

    # PDF
    if not state.get("final"):
        state["final"] = step("finalize", {"usage": state.get("usage", {})})
        event("pdf", state["final"]["detail"])
    return state

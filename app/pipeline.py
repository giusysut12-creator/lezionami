"""Orchestrazione delle fasi di elaborazione.

Ogni lavoro (job) vive solo in memoria. Le fasi completate restano in cache
all'interno del job, così «Riprova» riparte dalla fase non riuscita senza
ripetere le chiamate già andate a buon fine.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from . import checks, prompts
from .config import Settings
from .extract import (
    ExtractedDocument, ExtractionError, build_source_passages, build_transcript_passages,
    detect_issues, extract_file, from_pasted_text, render_passages, segment_passages,
)
from .llm import ClaudeClient, LLMError, Usage, parse_json_text
from .pdf_render import DOC_KIND_LABEL, file_name, render_pdf
from .schemas import CHECK_SCHEMA, DOCUMENT_SCHEMA, INVENTORY_SCHEMA

log = logging.getLogger("lezionami.pipeline")

PHASES = [
    ("estrazione", "Estrazione e controllo del testo", 5),
    ("inventario", "Inventario di argomenti, numeri e condizioni", 25),
    ("verifica_web", "Verifica su fonti ufficiali (web)", 8),
    ("generazione", "Generazione dei tre documenti", 37),
    ("controllo", "Controllo di coerenza e copertura", 15),
    ("pdf", "Creazione e verifica dei PDF", 10),
]
DOC_KINDS = ["lezione", "guida", "brochure"]
MONTHS = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
          "settembre", "ottobre", "novembre", "dicembre"]


def italian_date(moment: datetime) -> str:
    return f"{moment.day} {MONTHS[moment.month - 1]} {moment.year}"


@dataclass
class Upload:
    name: str
    data: bytes


@dataclass
class PhaseState:
    key: str
    label: str
    weight: int
    status: str = "attesa"  # attesa | in_corso | completata | errore | saltata | non_riuscita
    detail: str = ""
    progress: float = 0.0


@dataclass
class Job:
    id: str
    settings: Settings
    title: str
    lesson_date: str
    recipient: str
    web_search: bool
    transcript_upload: Upload | None
    pasted_text: str
    source_uploads: list[Upload]
    created_at: float = field(default_factory=time.time)
    status: str = "in_attesa"  # in_attesa | in_corso | completato | errore
    phases: list[PhaseState] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: dict | None = None
    usage: Usage = field(default_factory=Usage)
    # Risultati intermedi (cache delle fasi)
    transcript: ExtractedDocument | None = None
    sources: list[ExtractedDocument] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    segment_inventories: dict[int, dict] = field(default_factory=dict)
    inventory: dict | None = None
    web: dict | None = None
    docs: dict[str, dict] = field(default_factory=dict)
    ai_check: dict | None = None
    revised: dict[str, dict] = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    pdfs: dict[str, bytes] = field(default_factory=dict)
    pdf_pages: dict[str, int] = field(default_factory=dict)
    report_text: str = ""
    zip_bytes: bytes | None = None
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        if not self.phases:
            self.phases = [PhaseState(k, label, w) for k, label, w in PHASES]

    # ------------------------------------------------------------------
    def phase(self, key: str) -> PhaseState:
        return next(p for p in self.phases if p.key == key)

    @property
    def final_title(self) -> str:
        if self.title:
            return self.title
        if self.inventory and self.inventory.get("titolo_proposto"):
            return self.inventory["titolo_proposto"]
        if self.transcript_upload:
            return self.transcript_upload.name.rsplit(".", 1)[0]
        return "Lezione"

    @property
    def final_date(self) -> str:
        if self.lesson_date:
            return self.lesson_date
        return (self.inventory or {}).get("data_lezione_rilevata", "")

    def progress(self) -> int:
        total = sum(p.weight for p in self.phases)
        done = 0.0
        for p in self.phases:
            if p.status in ("completata", "saltata", "non_riuscita"):
                done += p.weight
            elif p.status in ("in_corso", "errore"):
                done += p.weight * min(max(p.progress, 0.0), 1.0)
        return int(round(100 * done / total))

    def public_state(self) -> dict:
        files = []
        if self.status == "completato":
            for kind in DOC_KINDS:
                if kind in self.pdfs:
                    files.append({
                        "kind": kind, "label": DOC_KIND_LABEL[kind],
                        "filename": file_name(kind, self.final_title),
                        "pages": self.pdf_pages.get(kind), "size": len(self.pdfs[kind]),
                    })
        cost = self.usage.estimated_cost(self.settings.price_input, self.settings.price_output)
        return {
            "id": self.id,
            "status": self.status,
            "progress": self.progress(),
            "phases": [
                {"key": p.key, "label": p.label, "status": p.status, "detail": p.detail}
                for p in self.phases
            ],
            "warnings": self.warnings,
            "error": self.error,
            "title": self.final_title if self.inventory or self.title else self.title,
            "files": files,
            "zip_name": f"Documenti_{file_name('lezione', self.final_title)[len('Lezione_completa_'):-4]}.zip",
            "issues": [i for i in self.issues if i["gravita"] in ("alta", "media")][:30],
            "issues_low": len([i for i in self.issues if i["gravita"] == "bassa"]),
            "coverage": self.coverage,
            "web": self._web_public(),
            "usage": {**self.usage.as_dict(), "costo_stimato_usd": round(cost, 3) if cost is not None else None,
                      "modello": self.settings.model},
        }

    def _web_public(self) -> dict | None:
        if not self.web_search:
            return None
        if not self.web:
            return {"stato": "non eseguita"}
        return {
            "stato": self.web.get("stato"),
            "messaggio": self.web.get("messaggio", ""),
            "consultato_il": self.web.get("consultato_il", ""),
            "fonti": self.web.get("fonti", [])[:20],
            "verifiche": len(self.web.get("verifiche", [])),
        }

    def wipe(self) -> None:
        """Elimina dalla memoria testo e risultati del lavoro."""
        self.transcript_upload = None
        self.pasted_text = ""
        self.source_uploads = []
        self.transcript = None
        self.sources = []
        self.segment_inventories = {}
        self.inventory = None
        self.web = None
        self.docs = {}
        self.revised = {}
        self.pdfs = {}
        self.zip_bytes = None
        self.report_text = ""


class Pipeline:
    def __init__(self, job: Job):
        self.job = job
        self.settings = job.settings
        self.llm = ClaudeClient(job.settings, job.usage)
        self.today = italian_date(datetime.now())

    # ------------------------------------------------------------------
    def run(self) -> None:
        job = self.job
        job.status = "in_corso"
        job.error = None
        steps = {
            "estrazione": self.phase_extract,
            "inventario": self.phase_inventory,
            "verifica_web": self.phase_web,
            "generazione": self.phase_generate,
            "controllo": self.phase_check,
            "pdf": self.phase_pdf,
        }
        for phase in job.phases:
            if phase.status in ("completata", "saltata", "non_riuscita"):
                continue
            phase.status = "in_corso"
            started = time.monotonic()
            try:
                steps[phase.key](phase)
                if phase.status == "in_corso":
                    phase.status = "completata"
                phase.progress = 1.0
                log.info("job=%s fase=%s esito=%s durata=%.1fs", job.id, phase.key, phase.status,
                         time.monotonic() - started)
            except ExtractionError as exc:
                self._fail(phase, str(exc), retryable=False)
                return
            except LLMError as exc:
                log.warning("job=%s fase=%s errore_api=%s", job.id, phase.key, exc.kind)
                self._fail(phase, exc.user_message, retryable=exc.retryable)
                return
            except Exception as exc:  # errore interno: nessun contenuto nei log
                log.exception("job=%s fase=%s errore_interno=%s", job.id, phase.key, type(exc).__name__)
                self._fail(phase, f"Errore interno nella fase «{phase.label}» ({type(exc).__name__}). Premi «Riprova»; se si ripete, controlla la console.", retryable=True)
                return
        job.status = "completato"

    def _fail(self, phase: PhaseState, message: str, retryable: bool) -> None:
        phase.status = "errore"
        phase.detail = message
        self.job.status = "errore"
        self.job.error = {"phase": phase.key, "label": phase.label, "message": message, "retryable": retryable}

    # ------------------------------------------------------------------
    # Fase 1
    # ------------------------------------------------------------------
    def phase_extract(self, phase: PhaseState) -> None:
        job = self.job
        if job.transcript_upload is not None:
            doc = extract_file(job.transcript_upload.name, job.transcript_upload.data, "trascrizione")
        else:
            doc = from_pasted_text(job.pasted_text)
        build_transcript_passages(doc)
        job.transcript = doc
        job.warnings.extend(doc.warnings)
        job.signals = detect_issues(doc.text)
        job.warnings.extend(job.signals)

        job.sources = []
        for index, upload in enumerate(job.source_uploads, start=1):
            try:
                source = extract_file(upload.name, upload.data, "fonte")
            except ExtractionError as exc:
                job.warnings.append(f"Fonte aggiuntiva esclusa: {exc}")
                continue
            build_source_passages(source, len(job.sources) + 1)
            job.sources.append(source)
            job.warnings.extend(source.warnings)
        # I file originali non servono più: si liberano subito.
        job.transcript_upload = Upload(job.transcript_upload.name, b"") if job.transcript_upload else None
        job.source_uploads = []
        job.pasted_text = ""
        words = len(doc.text.split())
        phase.detail = (
            f"{words:,}".replace(",", ".") + f" parole, {len(doc.passages)} passaggi"
            + (f"; {len(job.sources)} fonti aggiuntive" if job.sources else "")
        )

    # ------------------------------------------------------------------
    # Blocchi di contesto
    # ------------------------------------------------------------------
    def _header(self) -> str:
        job = self.job
        return prompts.context_header(self.today, job.title, job.lesson_date, job.recipient, job.signals)

    def _transcript_block(self, passages=None, cache: bool = False) -> dict:
        passages = passages if passages is not None else self.job.transcript.passages
        text = "<trascrizione>\n" + render_passages(passages) + "\n</trascrizione>"
        block = {"type": "text", "text": text}
        if cache:
            block["cache_control"] = {"type": "ephemeral"}
        return block

    def _source_blocks(self, passages_by_source=None) -> list[dict]:
        blocks = []
        for index, source in enumerate(self.job.sources, start=1):
            passages = passages_by_source.get(index) if passages_by_source else source.passages
            if not passages:
                continue
            pages = f' pagine="{source.pages_total}"' if source.pages_total else ""
            header = (
                f'<fonte_documentale id="D{index}" file="{source.name}" titolo_metadati="{source.metadata_title}" '
                f'data_metadati="{source.metadata_date}"{pages}>'
            )
            blocks.append({"type": "text", "text": header + "\n" + render_passages(passages) + "\n</fonte_documentale>"})
        return blocks

    def _all_sources_content(self) -> list[dict]:
        blocks = [self._transcript_block()] + self._source_blocks()
        blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
        return blocks

    def _sources_chars(self) -> int:
        total = len(self.job.transcript.text)
        return total + sum(len(s.text) for s in self.job.sources)

    def _json_block(self, tag: str, data, cache: bool = False) -> dict:
        block = {"type": "text", "text": f"<{tag}>\n{json.dumps(data, ensure_ascii=False)}\n</{tag}>"}
        if cache:
            block["cache_control"] = {"type": "ephemeral"}
        return block

    def _web_block(self) -> list[dict]:
        web = self.job.web
        if not web or web.get("stato") != "eseguita" or not web.get("verifiche"):
            note = "Nessuna verifica web disponibile: non dichiarare nulla come verificato online."
            if self.job.web_search and web and web.get("stato") != "eseguita":
                note = f"La verifica web è stata richiesta ma non è riuscita ({web.get('messaggio', '')}). Non dichiarare nulla come verificato online."
            return [{"type": "text", "text": f"<verifiche_web>{note}</verifiche_web>"}]
        return [self._json_block("verifiche_web", {
            "consultate_il": web.get("consultato_il"),
            "avvertenza": "Integrazioni recuperate online: non attribuirle al relatore; cita fonte e data.",
            "verifiche": web.get("verifiche"),
        })]

    # ------------------------------------------------------------------
    # Fase 2: inventario (con segmentazione per testi lunghi)
    # ------------------------------------------------------------------
    def phase_inventory(self, phase: PhaseState) -> None:
        job = self.job
        if job.inventory is not None:
            return
        system = prompts.SYSTEM_BASE
        if self._sources_chars() <= self.settings.single_pass_max_chars:
            phase.detail = "Analisi completa in un unico passaggio"
            content = self._all_sources_content() + [{"type": "text", "text": self._header() + "\n\n" + prompts.INVENTORY_TASK}]
            job.inventory = self.llm.call_json(system, content, INVENTORY_SCHEMA, "inventario")
            phase.detail = self._inventory_summary(job.inventory)
            return

        # Trascrizione lunga: inventario per segmenti + consolidamento.
        segments = segment_passages(job.transcript.passages, self.settings.segment_max_chars)
        tasks: list[tuple[int, list[dict], str]] = []
        for index, segment in enumerate(segments, start=1):
            note = prompts.SEGMENT_NOTE.format(index=index, total=len(segments),
                                               first=segment[0].id, last=segment[-1].id)
            content = [self._transcript_block(segment),
                       {"type": "text", "text": self._header() + "\n\n" + prompts.INVENTORY_TASK + "\n\n" + note}]
            tasks.append((index, content, f"inventario segmento {index}/{len(segments)}"))
        offset = len(segments)
        for s_index, source in enumerate(job.sources, start=1):
            for d_index, chunk in enumerate(segment_passages(source.passages, self.settings.segment_max_chars), start=1):
                key = offset + 1
                offset += 1
                content = self._source_blocks({s_index: chunk}) + [{"type": "text", "text": (
                    self._header() + "\n\n" + prompts.INVENTORY_TASK
                    + f"\n\nStai analizzando la parte {d_index} del documento allegato D{s_index}. "
                      f"Usa identificativi con prefisso S{key}-.")}]
                tasks.append((key, content, f"inventario documento D{s_index} parte {d_index}"))

        pending = [t for t in tasks if t[0] not in job.segment_inventories]
        total = len(tasks)

        def run_task(task):
            key, content, label = task
            result = self.llm.call_json(system, content, INVENTORY_SCHEMA, label)
            with job.lock:
                job.segment_inventories[key] = result
                phase.progress = 0.85 * len(job.segment_inventories) / total
                phase.detail = f"Segmenti analizzati: {len(job.segment_inventories)} di {total}"
            return key

        phase.detail = f"Segmenti analizzati: {len(job.segment_inventories)} di {total}"
        errors: list[Exception] = []
        with ThreadPoolExecutor(max_workers=self.settings.parallel_requests) as pool:
            futures = [pool.submit(run_task, t) for t in pending]
            for future in futures:
                try:
                    future.result()
                except Exception as exc:  # si raccolgono: i segmenti riusciti restano in cache
                    errors.append(exc)
        if errors:
            raise errors[0]

        phase.detail = "Consolidamento degli inventari dei segmenti"
        partials = [job.segment_inventories[key] for key, _, _ in tasks]
        content = [
            self._json_block("inventari_parziali", [
                {"segmento": key, "etichetta": label, "inventario": inv}
                for (key, _, label), inv in zip(tasks, partials)
            ]),
            {"type": "text", "text": self._header() + "\n\n" + prompts.CONSOLIDATION_TASK},
        ]
        consolidated = self.llm.call_json(system, content, INVENTORY_SCHEMA, "consolidamento inventario")
        self._ensure_segment_coverage(consolidated, partials)
        job.inventory = consolidated
        phase.detail = self._inventory_summary(consolidated) + f" (da {total} segmenti)"

    def _ensure_segment_coverage(self, consolidated: dict, partials: list[dict]) -> None:
        """Se il consolidamento ha perso riferimenti a interi segmenti, li reintegra."""
        kept_refs = set()
        for key in ("argomenti", "elementi"):
            for item in consolidated.get(key, []):
                kept_refs.update(item.get("riferimenti", []))
        for partial in partials:
            for key in ("argomenti", "elementi", "criticita", "domande_pubblico"):
                for item in partial.get(key, []):
                    refs = set(item.get("riferimenti", []))
                    if refs and not (refs & kept_refs) and key in ("argomenti", "elementi"):
                        consolidated.setdefault(key, []).append(item)
                        kept_refs.update(refs)

    @staticmethod
    def _inventory_summary(inventory: dict) -> str:
        return (
            f"{len(inventory.get('argomenti', []))} argomenti, {len(inventory.get('elementi', []))} elementi, "
            f"{len(inventory.get('criticita', []))} criticità"
        )

    # ------------------------------------------------------------------
    # Fase facoltativa: verifica web
    # ------------------------------------------------------------------
    def phase_web(self, phase: PhaseState) -> None:
        job = self.job
        if not job.web_search:
            phase.status = "saltata"
            phase.detail = "Disattivata: i documenti si basano su trascrizione e fonti allegate"
            return
        if job.web and job.web.get("stato") == "eseguita":
            return
        inventory = job.inventory or {}
        claims = [
            {"id": e.get("id"), "testo": e.get("testo"), "valore": e.get("valore")}
            for e in inventory.get("elementi", [])
            if e.get("categoria") in ("numero", "condizione", "costo", "garanzia", "fiscalita", "data", "eccezione")
        ][:25]
        content = (
            self._header() + "\n\n" + prompts.WEB_TASK + "\n\n"
            + json.dumps({"prodotti": inventory.get("prodotti", []), "condizioni_da_verificare": claims}, ensure_ascii=False)
        )
        consulted_at = datetime.now().strftime("%d/%m/%Y %H:%M")
        try:
            text, blocks = self.llm.call_web(prompts.SYSTEM_BASE, content, "verifica web")
        except LLMError as exc:
            job.web = {"stato": "non_riuscita", "messaggio": exc.user_message, "consultato_il": consulted_at,
                       "fonti": [], "verifiche": []}
            phase.status = "non_riuscita"
            phase.detail = "Ricerca non riuscita: nessuna condizione è dichiarata verificata"
            job.warnings.append(f"Verifica web non riuscita ({exc.user_message}). I documenti non dichiarano verifiche online.")
            return
        sources, errors = self._consulted_sources(blocks)
        verifications: list[dict] = []
        try:
            parsed = parse_json_text(text)
            consulted_urls = {s["url"] for s in sources}
            for index, item in enumerate(parsed.get("verifiche", []) or [], start=1):
                url = (item.get("fonte_url") or "").strip()
                esito = item.get("esito", "non_trovata")
                if esito in ("confermata", "diversa") and url not in consulted_urls:
                    esito = "non_verificabile"  # fonte non effettivamente consultata
                verifications.append({**item, "id": f"W{index}", "esito": esito})
        except (ValueError, json.JSONDecodeError):
            verifications = []
        if not sources:
            state, message = "non_riuscita", "nessuna fonte consultata" + (f" ({', '.join(errors)})" if errors else "")
        elif not verifications:
            state, message = "non_riuscita", "risultato della verifica non leggibile"
        else:
            state, message = "eseguita", ""
        job.web = {"stato": state, "messaggio": message, "consultato_il": consulted_at,
                   "fonti": sources, "verifiche": verifications if state == "eseguita" else []}
        if state == "eseguita":
            confirmed = sum(1 for v in verifications if v["esito"] == "confermata")
            phase.detail = f"{len(sources)} documenti consultati, {confirmed} condizioni confermate"
        else:
            phase.status = "non_riuscita"
            phase.detail = f"Verifica non riuscita: {message}"
            job.warnings.append(f"Verifica web non riuscita ({message}): nessuna condizione è dichiarata verificata.")

    @staticmethod
    def _consulted_sources(blocks: list) -> tuple[list[dict], list[str]]:
        sources: dict[str, dict] = {}
        errors: list[str] = []
        for block in blocks:
            kind = getattr(block, "type", "")
            content = getattr(block, "content", None)
            if kind == "web_search_tool_result":
                if isinstance(content, list):
                    for result in content:
                        url = getattr(result, "url", "")
                        if url:
                            sources.setdefault(url, {"url": url, "titolo": getattr(result, "title", "") or "",
                                                     "data_pagina": getattr(result, "page_age", "") or "",
                                                     "tipo": "risultato di ricerca"})
                else:
                    errors.append(str(getattr(content, "error_code", "errore")))
            elif kind == "web_fetch_tool_result":
                url = getattr(content, "url", "") if content is not None else ""
                if url and getattr(content, "type", "") == "web_fetch_result":
                    sources[url] = {"url": url, "titolo": sources.get(url, {}).get("titolo", ""),
                                    "data_pagina": sources.get(url, {}).get("data_pagina", ""),
                                    "tipo": "documento letto", "letto_il": getattr(content, "retrieved_at", "") or ""}
                else:
                    errors.append(str(getattr(content, "error_code", "errore")))
        return list(sources.values()), errors

    # ------------------------------------------------------------------
    # Fase 3: generazione
    # ------------------------------------------------------------------
    def _doc_task(self, kind: str) -> str:
        task = {"lezione": prompts.LESSON_TASK, "guida": prompts.GUIDE_TASK, "brochure": prompts.BROCHURE_TASK}[kind]
        extra = []
        job = self.job
        title = job.final_title
        extra.append(f"Titolo del documento: usa «{title}» come base del titolo (puoi aggiungere il tipo di documento).")
        if job.final_date:
            extra.append(f"Data della lezione: {job.final_date}.")
        if kind == "brochure" and job.recipient:
            extra.append(f"Destinatario della brochure: {job.recipient}. Adatta tono ed esempi, senza inventare bisogni specifici.")
        return self._header() + "\n\n" + task + "\n\n" + "\n".join(extra) + "\n\n" + prompts.DOC_FORMAT_NOTE

    def _lesson_content(self) -> list[dict]:
        job = self.job
        if self._sources_chars() <= self.settings.lesson_source_max_chars:
            content = self._all_sources_content()
        else:
            content = [{"type": "text", "text": "<nota>La fonte integrale è troppo lunga per essere allegata: lavora sull'inventario consolidato, che contiene tutti gli elementi con i riferimenti ai passaggi.</nota>"}]
        content += [self._json_block("inventario", job.inventory)] + self._web_block()
        return content

    def phase_generate(self, phase: PhaseState) -> None:
        job = self.job
        system = prompts.SYSTEM_BASE
        if "lezione" not in job.docs:
            phase.detail = "Scrittura della lezione completa"
            content = self._lesson_content() + [{"type": "text", "text": self._doc_task("lezione")}]
            job.docs["lezione"] = checks.normalize_document(
                self.llm.call_json(system, content, DOCUMENT_SCHEMA, "lezione completa"))
        phase.progress = 0.55
        phase.detail = "Scrittura di guida di studio e brochure"
        shared = [self._json_block("inventario", job.inventory)] + self._web_block() + [
            self._json_block("lezione_completa", job.docs["lezione"], cache=True)]

        def generate(kind: str) -> None:
            if kind in job.docs:
                return
            label = "guida di studio" if kind == "guida" else "brochure cliente"
            result = self.llm.call_json(system, shared + [{"type": "text", "text": self._doc_task(kind)}],
                                        DOCUMENT_SCHEMA, label)
            job.docs[kind] = checks.normalize_document(result)

        errors: list[Exception] = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            for future in [pool.submit(generate, k) for k in ("guida", "brochure")]:
                try:
                    future.result()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise errors[0]
        phase.detail = "Lezione, guida e brochure generate"

    # ------------------------------------------------------------------
    # Fase 4: controllo e revisione
    # ------------------------------------------------------------------
    def _source_numbers(self) -> set[float]:
        job = self.job
        texts = [job.transcript.text] + [s.text for s in job.sources]
        if job.web and job.web.get("stato") == "eseguita":
            texts.append(json.dumps(job.web.get("verifiche", []), ensure_ascii=False))
        numbers: set[float] = set()
        for text in texts:
            numbers |= checks.numbers_in(text)
        return numbers

    def _automatic_issues(self, docs: dict[str, dict]) -> tuple[list[dict], dict]:
        job = self.job
        issues: list[dict] = []
        source_numbers = self._source_numbers()
        for kind, doc in docs.items():
            issues += checks.check_arithmetic(doc, kind)
            issues += checks.check_unsupported_numbers(doc, kind, source_numbers)
        issues += checks.check_brochure_terms(docs["brochure"])
        passage_ids = [p.id for p in job.transcript.passages]
        coverage_issues, stats = checks.check_coverage(job.inventory, docs["lezione"], passage_ids)
        issues += coverage_issues
        issues += checks.check_limits(job.signals, docs)
        return issues, stats

    def phase_check(self, phase: PhaseState) -> None:
        job = self.job
        system = prompts.SYSTEM_BASE
        auto_issues, _ = self._automatic_issues(job.docs)
        if job.ai_check is None:
            phase.detail = "Controllo incrociato dei tre documenti"
            content = [
                self._json_block("inventario", job.inventory),
                *self._web_block(),
                self._json_block("lezione_completa", job.docs["lezione"]),
                self._json_block("guida_di_studio", job.docs["guida"]),
                self._json_block("brochure_cliente", job.docs["brochure"]),
                self._json_block("segnalazioni_automatiche", auto_issues),
                {"type": "text", "text": self._header() + "\n\n" + prompts.CHECK_TASK},
            ]
            job.ai_check = self.llm.call_json(system, content, CHECK_SCHEMA, "controllo di coerenza")
        phase.progress = 0.35

        ai_issues = [{**i, "origine": "controllo AI"} for i in job.ai_check.get("problemi", [])]
        for missing in job.ai_check.get("argomenti_non_coperti", []) or []:
            ai_issues.append({"gravita": "media", "documento": "lezione", "posizione": "copertura",
                              "problema": f"Argomento non coperto: {missing}",
                              "correzione": "Aggiungere la trattazione dell'argomento.", "origine": "controllo AI"})
        to_fix = {
            kind: [i for i in ai_issues + auto_issues
                   if i["documento"] == kind and i["gravita"] in ("alta", "media")]
            for kind in DOC_KINDS
        }
        pending = [k for k in DOC_KINDS if to_fix[k] and k not in job.revised]
        if pending:
            phase.detail = "Revisione: " + ", ".join(DOC_KIND_LABEL[k] for k in pending)

        def revise(kind: str) -> None:
            if kind == "lezione":
                base = self._lesson_content()
            else:
                base = [self._json_block("inventario", job.inventory), *self._web_block(),
                        self._json_block("lezione_completa", job.docs["lezione"])]
            content = base + [
                self._json_block("documento_da_rivedere", job.docs[kind]),
                self._json_block("problemi", to_fix[kind]),
                {"type": "text", "text": self._header() + "\n\n" + self._doc_task(kind) + "\n\n" + prompts.REVISION_TASK},
            ]
            label = f"revisione {DOC_KIND_LABEL[kind].lower()}"
            job.revised[kind] = checks.normalize_document(self.llm.call_json(system, content, DOCUMENT_SCHEMA, label))

        errors: list[Exception] = []
        with ThreadPoolExecutor(max_workers=self.settings.parallel_requests) as pool:
            for future in [pool.submit(revise, k) for k in pending]:
                try:
                    future.result()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise errors[0]

        final_docs = {k: job.revised.get(k, job.docs[k]) for k in DOC_KINDS}
        residual, stats = self._automatic_issues(final_docs)
        revised_kinds = set(job.revised)
        # Problemi AI ancora riportati solo per i documenti non revisionati.
        remaining_ai = [i for i in ai_issues if i["documento"] not in revised_kinds]
        job.issues = sorted(residual + remaining_ai, key=lambda i: {"alta": 0, "media": 1, "bassa": 2}[i["gravita"]])
        job.coverage = stats
        fixed = sum(len(to_fix[k]) for k in revised_kinds)
        phase.detail = (
            f"{fixed} segnalazioni corrette in revisione; {len([i for i in job.issues if i['gravita'] != 'bassa'])} da verificare"
            if revised_kinds else "Nessun problema rilevante"
        )

    # ------------------------------------------------------------------
    # Fase 5: PDF
    # ------------------------------------------------------------------
    def phase_pdf(self, phase: PhaseState) -> None:
        from pypdf import PdfReader

        job = self.job
        final_docs = {k: job.revised.get(k, job.docs[k]) for k in DOC_KINDS}
        for index, kind in enumerate(DOC_KINDS):
            doc = final_docs[kind]
            data = render_pdf(doc, kind, show_refs=(kind == "lezione" and self.settings.show_source_refs))
            reader = PdfReader(io.BytesIO(data))
            pages = len(reader.pages)
            extracted = "".join((p.extract_text() or "") for p in reader.pages[:2])
            if pages == 0 or len(extracted.strip()) < 50:
                raise RuntimeError(f"PDF {kind} senza testo selezionabile")
            job.pdfs[kind] = data
            job.pdf_pages[kind] = pages
            phase.progress = (index + 1) / 4
        guide_pages, brochure_pages = job.pdf_pages["guida"], job.pdf_pages["brochure"]
        if guide_pages > 7:
            job.warnings.append(f"La guida di studio è di {guide_pages} pagine, oltre l'indicazione di 3–5: il contenuto è molto ampio.")
        if brochure_pages > 5:
            job.warnings.append(f"La brochure è di {brochure_pages} pagine, oltre l'indicazione di 2–4.")
        job.report_text = self._report(final_docs)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for kind in DOC_KINDS:
                archive.writestr(file_name(kind, job.final_title), job.pdfs[kind])
            archive.writestr(f"Report_verifica_{file_name('lezione', job.final_title)[len('Lezione_completa_'):-4]}.txt",
                             job.report_text)
        job.zip_bytes = buffer.getvalue()
        phase.detail = ", ".join(f"{DOC_KIND_LABEL[k]}: {job.pdf_pages[k]} pag." for k in DOC_KINDS)

    def _report(self, final_docs: dict[str, dict]) -> str:
        job = self.job
        lines = [
            "REPORT DI VERIFICA",
            f"Titolo: {job.final_title}",
            f"Generato il: {datetime.now().strftime('%d/%m/%Y %H:%M')} con il modello {self.settings.model}",
            "",
            "1. FONTE PRINCIPALE",
            f"- {job.transcript.name}: {len(job.transcript.text.split())} parole, {len(job.transcript.passages)} passaggi (T001–T{len(job.transcript.passages):03d}).",
        ]
        if job.signals:
            lines.append("- Segnali rilevati:")
            lines += [f"  · {s}" for s in job.signals]
        lines += ["", "2. FONTI AGGIUNTIVE"]
        if job.sources:
            for index, source in enumerate(job.sources, start=1):
                meta = ", ".join(x for x in [source.metadata_title, source.metadata_date,
                                             f"{source.pages_total} pagine" if source.pages_total else ""] if x)
                lines.append(f"- D{index}: {source.name}" + (f" ({meta})" if meta else ""))
        else:
            lines.append("- Nessuna.")
        lines += ["", "3. VERIFICA WEB"]
        if not job.web_search:
            lines.append("- Non richiesta: nessuna condizione è stata verificata online.")
        elif not job.web or job.web.get("stato") != "eseguita":
            lines.append(f"- Non riuscita ({(job.web or {}).get('messaggio', '')}): nessuna condizione è dichiarata verificata.")
        else:
            lines.append(f"- Eseguita il {job.web['consultato_il']}. Documenti consultati:")
            lines += [f"  · {s.get('titolo') or '(senza titolo)'} — {s['url']}" + (f" (data pagina: {s['data_pagina']})" if s.get("data_pagina") else "") for s in job.web["fonti"]]
            lines.append("- Esiti:")
            lines += [f"  · [{v['id']}] {v.get('affermazione', '')} → {v['esito']}" + (f" ({v.get('fonte_url')})" if v.get("fonte_url") else "") for v in job.web["verifiche"]]
        lines += ["", "4. COPERTURA"]
        lines += [f"- {k.replace('_', ' ')}: {v}" for k, v in job.coverage.items()]
        lines += ["", "5. SEGNALAZIONI RESIDUE"]
        if job.issues:
            for issue in job.issues:
                lines.append(f"- [{issue['gravita']}] {DOC_KIND_LABEL.get(issue['documento'], issue['documento'])} · {issue['posizione']}: {issue['problema']} ({issue['origine']})")
        else:
            lines.append("- Nessuna.")
        if job.warnings:
            lines += ["", "6. AVVISI"] + [f"- {w}" for w in job.warnings]
        cost = job.usage.estimated_cost(self.settings.price_input, self.settings.price_output)
        lines += ["", "7. UTILIZZO API", f"- Chiamate: {job.usage.calls}; token in ingresso: {job.usage.input_tokens + job.usage.cache_write_tokens + job.usage.cache_read_tokens}; token in uscita: {job.usage.output_tokens}"]
        if cost is not None:
            lines.append(f"- Costo stimato: circa {cost:.2f} USD (stima indicativa sui prezzi di listino)")
        lines += ["", "8. INDICE DEI PASSAGGI DELLA TRASCRIZIONE (per verificare i riferimenti «Rif. fonte»)", ""]
        lines += [f"[{p.id}] {p.text}" for p in job.transcript.passages]
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Registro dei lavori in memoria
# ---------------------------------------------------------------------------

class JobStore:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, **kwargs) -> Job:
        job = Job(id=uuid.uuid4().hex, **kwargs)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job:
            job.wipe()
        return job is not None

    def start(self, job: Job) -> None:
        if job.thread and job.thread.is_alive():
            return
        job.status = "in_corso"
        job.thread = threading.Thread(target=Pipeline(job).run, name=f"job-{job.id[:8]}", daemon=True)
        job.thread.start()

    def cleanup(self, ttl_minutes: int) -> None:
        limit = time.time() - ttl_minutes * 60
        with self._lock:
            expired = [j for j in self._jobs.values()
                       if j.created_at < limit and not (j.thread and j.thread.is_alive())]
            for job in expired:
                self._jobs.pop(job.id, None)
                job.wipe()

"""Fasi di elaborazione come passi senza stato.

Ogni passo riceve dal chiamante (browser o riga di comando) lo stato già
prodotto dai passi precedenti e restituisce solo il proprio risultato. Il
server non conserva nulla tra una richiesta e l'altra: è adatto a piattaforme
serverless come Vercel, dove ogni richiesta ha un tempo massimo, e permette di
ripetere solo il passo non riuscito.

Ogni passo contiene al massimo una chiamata all'API di Claude.
"""

from __future__ import annotations

import base64
import io
import json
import math
import re
import time
from datetime import datetime

from . import checks, prompts
from .config import Settings
from .extract import (
    ExtractionError, build_source_passages, build_transcript_passages, detect_issues,
    extract_file, from_pasted_text, segment_passages,
)
from .llm import ClaudeClient, LLMError, Usage, parse_json_text
from .pdf_render import DOC_KIND_LABEL, file_name, render_pdf
from .schemas import CHECK_SCHEMA, CROSSCHECK_SCHEMA, DOCUMENT_SCHEMA, INVENTORY_SCHEMA

DOC_KINDS = ["lezione", "guida", "brochure"]
MONTHS = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
          "settembre", "ottobre", "novembre", "dicembre"]


class StepError(Exception):
    def __init__(self, message: str, status: int = 400, retryable: bool = False, kind: str = "input"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retryable = retryable
        self.kind = kind


def italian_date(moment: datetime) -> str:
    return f"{moment.day} {MONTHS[moment.month - 1]} {moment.year}"


def zip_suffix(title: str) -> str:
    return file_name("lezione", title)[len("Lezione_completa_"):-4]


# ---------------------------------------------------------------------------
# Passo 1: estrazione (nessuna chiamata AI)
# ---------------------------------------------------------------------------

def _passages_dicts(passages) -> list[dict]:
    return [{"id": p.id, "text": p.text, "page": p.page} for p in passages]


def extract_inputs(settings: Settings, transcript: tuple[str, bytes] | None, pasted_text: str,
                   sources: list[tuple[str, bytes]], meta: dict) -> dict:
    """Estrae il testo e prepara lo stato iniziale. Solleva ExtractionError."""
    if transcript is not None:
        doc = extract_file(transcript[0], transcript[1], "trascrizione")
    else:
        doc = from_pasted_text(pasted_text)
    build_transcript_passages(doc)
    warnings = list(doc.warnings)
    signals = detect_issues(doc.text)
    warnings.extend(signals)

    source_docs = []
    for name, data in sources:
        try:
            source = extract_file(name, data, "fonte")
        except ExtractionError as exc:
            warnings.append(f"Fonte aggiuntiva esclusa: {exc}")
            continue
        build_source_passages(source, len(source_docs) + 1)
        source_docs.append(source)
        warnings.extend(source.warnings)

    total_chars = len(doc.text) + sum(len(s.text) for s in source_docs)
    plan: dict = {"mode": "single", "parts": []}
    if total_chars > settings.single_pass_max_chars:
        plan["mode"] = "segmented"
        segments = segment_passages(doc.passages, settings.segment_max_chars)
        for index, segment in enumerate(segments, start=1):
            plan["parts"].append({"key": f"S{index}", "label": f"segmento {index} di {len(segments)}",
                                  "kind": "trascrizione", "ids": [p.id for p in segment]})
        for s_index, source in enumerate(source_docs, start=1):
            for d_index, chunk in enumerate(segment_passages(source.passages, settings.segment_max_chars), start=1):
                key = f"S{len(plan['parts']) + 1}"
                plan["parts"].append({"key": key, "label": f"documento D{s_index} parte {d_index}",
                                      "kind": "fonte", "source": s_index, "ids": [p.id for p in chunk]})

    words = len(doc.text.split())
    return {
        "meta": {
            "title": (meta.get("title") or "").strip()[:150],
            "lesson_date": (meta.get("lesson_date") or "").strip()[:60],
            "recipient": (meta.get("recipient") or "").strip()[:200],
            "web_search": bool(meta.get("web_search")),
            "today": italian_date(datetime.now()),
        },
        "source": {
            "transcript": {"name": doc.name, "words": words, "chars": len(doc.text),
                           "passages": _passages_dicts(doc.passages)},
            "sources": [
                {"index": i, "name": s.name, "metadata_title": s.metadata_title, "metadata_date": s.metadata_date,
                 "pages_total": s.pages_total, "chars": len(s.text), "passages": _passages_dicts(s.passages)}
                for i, s in enumerate(source_docs, start=1)
            ],
            "signals": signals,
        },
        "plan": plan,
        "warnings": warnings,
        "detail": f"{words:,}".replace(",", ".") + f" parole, {len(doc.passages)} passaggi"
                  + (f"; {len(source_docs)} fonti aggiuntive" if source_docs else ""),
    }


# ---------------------------------------------------------------------------
# Piano della lezione a parti (deterministico)
# ---------------------------------------------------------------------------

def lesson_plan(inventory: dict, max_topics: int) -> list[dict]:
    topics = inventory.get("argomenti", []) or []
    elements = inventory.get("elementi", []) or []
    if not topics:
        return [{"index": 0, "topic_ids": [], "orphans": [e.get("id") for e in elements]}]
    weights = {t["id"]: 1 + sum(1 for e in elements if e.get("argomento_id") == t["id"]) for t in topics}
    if len(topics) <= max_topics + 1:
        groups = [[t["id"] for t in topics]]
    else:
        count = math.ceil(len(topics) / max_topics)
        target = sum(weights.values()) / count
        groups, current, weight = [], [], 0
        for topic in topics:
            current.append(topic["id"])
            weight += weights[topic["id"]]
            if weight >= target or len(current) >= max_topics:
                groups.append(current)
                current, weight = [], 0
        if current:
            groups.append(current)
    known = {t["id"] for t in topics}
    orphans = [e.get("id") for e in elements if e.get("argomento_id") not in known]
    plan = [{"index": i, "topic_ids": g, "orphans": []} for i, g in enumerate(groups)]
    plan[-1]["orphans"] = orphans
    return plan


def merge_lesson(parts: dict) -> dict:
    ordered = [parts[k] for k in sorted(parts, key=lambda k: int(k))]
    if not ordered:
        raise StepError("La lezione non è ancora stata generata.")
    first = ordered[0]
    merged = {key: first.get(key, "") for key in ("title", "subtitle", "footer_label", "limits_notice")}
    merged["sections"] = [section for part in ordered for section in part.get("sections", [])]
    return checks.normalize_document(merged)


# ---------------------------------------------------------------------------
# Esecuzione dei passi
# ---------------------------------------------------------------------------

class Steps:
    def __init__(self, settings: Settings, state: dict):
        self.settings = settings
        self.state = state or {}
        self.meta = self.state.get("meta") or {}
        self.source = self.state.get("source") or {}
        self.usage = Usage()
        deadline = time.monotonic() + settings.step_deadline_seconds if settings.step_deadline_seconds else None
        self.llm = ClaudeClient(settings, self.usage, deadline)
        if not self.source.get("transcript"):
            raise StepError("Dati della trascrizione mancanti: ricomincia dal caricamento del file.")

    # --- contesto --------------------------------------------------------
    @property
    def signals(self) -> list[str]:
        return self.source.get("signals", [])

    @property
    def inventory(self) -> dict:
        inventory = self.state.get("inventory")
        if not inventory:
            raise StepError("Inventario mancante: ripeti la fase di inventario.")
        return inventory

    @property
    def final_title(self) -> str:
        if self.meta.get("title"):
            return self.meta["title"]
        inventory = self.state.get("inventory") or {}
        if inventory.get("titolo_proposto"):
            return inventory["titolo_proposto"]
        return self.source["transcript"].get("name", "Lezione").rsplit(".", 1)[0]

    @property
    def final_date(self) -> str:
        return self.meta.get("lesson_date") or (self.state.get("inventory") or {}).get("data_lezione_rilevata", "")

    def _header(self) -> str:
        return prompts.context_header(self.meta.get("today", italian_date(datetime.now())), self.meta.get("title", ""),
                                      self.meta.get("lesson_date", ""), self.meta.get("recipient", ""), self.signals)

    @staticmethod
    def _render(passages: list[dict]) -> str:
        lines = []
        for p in passages:
            page = f" p.{p['page']}" if p.get("page") else ""
            lines.append(f"[{p['id']}{page}] {p['text']}")
        return "\n\n".join(lines)

    def _transcript_block(self, passages: list[dict] | None = None) -> dict:
        passages = passages if passages is not None else self.source["transcript"]["passages"]
        return {"type": "text", "text": "<trascrizione>\n" + self._render(passages) + "\n</trascrizione>"}

    def _source_blocks(self, only: dict[int, list[dict]] | None = None) -> list[dict]:
        blocks = []
        for source in self.source.get("sources", []):
            index = source["index"]
            passages = only.get(index) if only is not None else source["passages"]
            if not passages:
                continue
            pages = f' pagine="{source["pages_total"]}"' if source.get("pages_total") else ""
            header = (f'<fonte_documentale id="D{index}" file="{source["name"]}" titolo_metadati="{source.get("metadata_title", "")}" '
                      f'data_metadati="{source.get("metadata_date", "")}"{pages}>')
            blocks.append({"type": "text", "text": header + "\n" + self._render(passages) + "\n</fonte_documentale>"})
        return blocks

    def _all_sources(self) -> list[dict]:
        blocks = [self._transcript_block()] + self._source_blocks()
        blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
        return blocks

    def _sources_chars(self) -> int:
        return self.source["transcript"].get("chars", 0) + sum(s.get("chars", 0) for s in self.source.get("sources", []))

    @staticmethod
    def _json_block(tag: str, data, cache: bool = False) -> dict:
        block = {"type": "text", "text": f"<{tag}>\n{json.dumps(data, ensure_ascii=False)}\n</{tag}>"}
        if cache:
            block["cache_control"] = {"type": "ephemeral"}
        return block

    def _task(self, text: str) -> dict:
        return {"type": "text", "text": self._header() + "\n\n" + text}

    def _web_block(self) -> list[dict]:
        web = self.state.get("web")
        if not web or web.get("stato") != "eseguita" or not web.get("verifiche"):
            note = "Nessuna verifica web disponibile: non dichiarare nulla come verificato online."
            if self.meta.get("web_search") and web and web.get("stato") != "eseguita":
                note = (f"La verifica web è stata richiesta ma non è riuscita ({web.get('messaggio', '')}). "
                        "Non dichiarare nulla come verificato online.")
            return [{"type": "text", "text": f"<verifiche_web>{note}</verifiche_web>"}]
        return [self._json_block("verifiche_web", {
            "consultate_il": web.get("consultato_il"),
            "avvertenza": "Integrazioni recuperate online: non attribuirle al relatore; cita fonte e data.",
            "verifiche": web.get("verifiche"),
        })]

    def _lesson_base(self) -> list[dict]:
        if self._sources_chars() <= self.settings.lesson_source_max_chars:
            content = self._all_sources()
        else:
            content = [{"type": "text", "text": "<nota>La fonte integrale è troppo lunga per essere allegata: lavora sull'inventario, che contiene tutti gli elementi con i riferimenti ai passaggi.</nota>"}]
        return content + [self._json_block("inventario", self.inventory, cache=True)] + self._web_block()

    def _doc_task(self, kind: str) -> str:
        task = {"lezione": prompts.LESSON_TASK, "guida": prompts.GUIDE_TASK, "brochure": prompts.BROCHURE_TASK}[kind]
        extra = [f"Titolo del documento: usa «{self.final_title}» come base del titolo (puoi aggiungere il tipo di documento)."]
        if self.final_date:
            extra.append(f"Data della lezione: {self.final_date}.")
        if kind == "brochure" and self.meta.get("recipient"):
            extra.append(f"Destinatario della brochure: {self.meta['recipient']}. Adatta tono ed esempi, senza inventare bisogni specifici.")
        return task + "\n\n" + "\n".join(extra) + "\n\n" + prompts.DOC_FORMAT_NOTE

    # --- documenti correnti ---------------------------------------------
    def current_docs(self, use_revised: bool = True) -> dict[str, dict]:
        revised = (self.state.get("revised") or {}) if use_revised else {}
        parts = dict(self.state.get("lesson_parts") or {})
        for key, part in (revised.get("lezione") or {}).items():
            parts[key] = part
        docs = {"lezione": merge_lesson(parts)}
        for kind in ("guida", "brochure"):
            doc = revised.get(kind) or (self.state.get("docs") or {}).get(kind)
            if not doc:
                raise StepError(f"{DOC_KIND_LABEL[kind]} mancante: ripeti la generazione.")
            docs[kind] = doc
        return docs

    # --- passi -----------------------------------------------------------
    def run(self, step: str, args: dict) -> dict:
        handlers = {
            "inventory": self.step_inventory,
            "inventory_part": self.step_inventory_part,
            "inventory_merge": self.step_inventory_merge,
            "web": self.step_web,
            "lesson_part": self.step_lesson_part,
            "guide": lambda a: self.step_document("guida"),
            "brochure": lambda a: self.step_document("brochure"),
            "check": self.step_check,
            "revise": self.step_revise,
            "finalize": self.step_finalize,
        }
        if step not in handlers:
            raise StepError(f"Passo sconosciuto: {step}")
        result = handlers[step](args or {})
        return {"result": result, "usage": self.usage.as_dict()}

    def _with_plan(self, inventory: dict) -> dict:
        return {"inventory": inventory, "lesson_plan": lesson_plan(inventory, self.settings.lesson_part_topics),
                "detail": (f"{len(inventory.get('argomenti', []))} argomenti, {len(inventory.get('elementi', []))} elementi, "
                           f"{len(inventory.get('criticita', []))} criticità")}

    def step_inventory(self, args: dict) -> dict:
        content = self._all_sources() + [self._task(prompts.INVENTORY_TASK)]
        inventory = self.llm.call_json(prompts.SYSTEM_BASE, content, INVENTORY_SCHEMA, "inventario",
                                       effort=self.settings.effort_analysis)
        return self._with_plan(inventory)

    def step_inventory_part(self, args: dict) -> dict:
        plan = self.state.get("plan") or {}
        part = next((p for p in plan.get("parts", []) if p["key"] == args.get("key")), None)
        if part is None:
            raise StepError("Segmento non trovato nel piano di elaborazione.")
        ids = set(part["ids"])
        segments = [p for p in plan["parts"] if p["kind"] == "trascrizione"]
        if part["kind"] == "trascrizione":
            passages = [p for p in self.source["transcript"]["passages"] if p["id"] in ids]
            number = segments.index(part) + 1
            note = prompts.SEGMENT_NOTE.format(index=number, total=len(segments), first=passages[0]["id"], last=passages[-1]["id"])
            note = note.replace(f"S{number}-", f"{part['key']}-")
            content = [self._transcript_block(passages), self._task(prompts.INVENTORY_TASK + "\n\n" + note)]
        else:
            source = next(s for s in self.source.get("sources", []) if s["index"] == part["source"])
            passages = [p for p in source["passages"] if p["id"] in ids]
            note = (f"Stai analizzando la {part['label']} del documento allegato D{part['source']}. "
                    f"Usa identificativi con prefisso {part['key']}-.")
            content = self._source_blocks({part["source"]: passages}) + [self._task(prompts.INVENTORY_TASK + "\n\n" + note)]
        inventory = self.llm.call_json(prompts.SYSTEM_BASE, content, INVENTORY_SCHEMA, f"inventario {part['label']}",
                                       effort=self.settings.effort_analysis)
        return {"key": part["key"], "inventory": inventory}

    def step_inventory_merge(self, args: dict) -> dict:
        plan = self.state.get("plan") or {}
        partials = self.state.get("inventory_partials") or {}
        missing = [p["key"] for p in plan.get("parts", []) if p["key"] not in partials]
        if missing:
            raise StepError(f"Mancano gli inventari dei segmenti: {', '.join(missing)}.")
        merged = merge_inventories([partials[p["key"]] for p in plan["parts"]])
        content = [self._json_block("inventario_unito", merged), self._task(prompts.CROSSCHECK_TASK)]
        cross = self.llm.call_json(prompts.SYSTEM_BASE, content, CROSSCHECK_SCHEMA, "controllo incrociato dei segmenti",
                                   effort=self.settings.effort_analysis)
        apply_crosscheck(merged, cross)
        result = self._with_plan(merged)
        result["detail"] += f" (da {len(partials)} segmenti)"
        return result

    def step_web(self, args: dict) -> dict:
        inventory = self.inventory
        claims = [
            {"id": e.get("id"), "testo": e.get("testo"), "valore": e.get("valore")}
            for e in inventory.get("elementi", [])
            if e.get("categoria") in ("numero", "condizione", "costo", "garanzia", "fiscalita", "data", "eccezione")
        ][:25]
        content = (self._header() + "\n\n" + prompts.WEB_TASK + "\n\n"
                   + json.dumps({"prodotti": inventory.get("prodotti", []), "condizioni_da_verificare": claims}, ensure_ascii=False))
        consulted_at = datetime.now().strftime("%d/%m/%Y %H:%M")
        try:
            text, blocks = self.llm.call_web(prompts.SYSTEM_BASE, content, "verifica web")
        except LLMError as exc:
            return {"stato": "non_riuscita", "messaggio": exc.user_message, "consultato_il": consulted_at,
                    "fonti": [], "verifiche": []}
        sources, errors = consulted_sources(blocks)
        verifications: list[dict] = []
        try:
            parsed = parse_json_text(text)
            consulted_urls = {s["url"] for s in sources}
            for index, item in enumerate(parsed.get("verifiche", []) or [], start=1):
                url = (item.get("fonte_url") or "").strip()
                esito = item.get("esito", "non_trovata")
                if esito in ("confermata", "diversa") and url not in consulted_urls:
                    esito = "non_verificabile"  # la pagina citata non è stata realmente consultata
                verifications.append({**item, "id": f"W{index}", "esito": esito})
        except (ValueError, json.JSONDecodeError):
            verifications = []
        if not sources:
            state, message = "non_riuscita", "nessuna fonte consultata" + (f" ({', '.join(errors)})" if errors else "")
        elif not verifications:
            state, message = "non_riuscita", "risultato della verifica non leggibile"
        else:
            state, message = "eseguita", ""
        return {"stato": state, "messaggio": message, "consultato_il": consulted_at, "fonti": sources,
                "verifiche": verifications if state == "eseguita" else []}

    def _part_context(self, index: int) -> tuple[list[dict], dict]:
        plan = lesson_plan(self.inventory, self.settings.lesson_part_topics)
        if index < 0 or index >= len(plan):
            raise StepError("Parte della lezione non valida.")
        return plan, plan[index]

    def _topic_titles(self, ids: list[str]) -> str:
        titles = {t["id"]: t.get("titolo", "") for t in self.inventory.get("argomenti", [])}
        return "; ".join(f"{i} «{titles.get(i, '')}»" for i in ids) or "nessuno"

    def _part_note(self, plan: list[dict], part: dict) -> str:
        if len(plan) == 1:
            return ""
        others = [p for p in plan if p["index"] != part["index"]]
        topics = self._topic_titles(part["topic_ids"])
        if part["orphans"]:
            topics += f"; inoltre gli elementi dell'inventario non collegati ad argomenti: {', '.join(part['orphans'])}"
        return prompts.LESSON_PART_NOTE.format(
            total=len(plan), number=part["index"] + 1, topics=topics,
            others="; ".join(f"parte {p['index'] + 1}: {self._topic_titles(p['topic_ids'])}" for p in others),
            opening=prompts.LESSON_PART_OPENING if part["index"] == 0 else "",
            closing=prompts.LESSON_PART_CLOSING if part["index"] == len(plan) - 1 else "",
        )

    def step_lesson_part(self, args: dict) -> dict:
        index = int(args.get("index", 0))
        plan, part = self._part_context(index)
        task = self._doc_task("lezione")
        note = self._part_note(plan, part)
        if note:
            task += "\n\n" + note
        label = "lezione completa" + (f" (parte {index + 1} di {len(plan)})" if len(plan) > 1 else "")
        doc = self.llm.call_json(prompts.SYSTEM_BASE, self._lesson_base() + [self._task(task)], DOCUMENT_SCHEMA, label)
        return {"index": str(index), "doc": checks.normalize_document(doc), "parts_total": len(plan)}

    def _shared_with_lesson(self) -> list[dict]:
        lesson = merge_lesson(self.state.get("lesson_parts") or {})
        return [self._json_block("inventario", self.inventory)] + self._web_block() + [
            self._json_block("lezione_completa", lesson, cache=True)]

    def step_document(self, kind: str) -> dict:
        label = "guida di studio" if kind == "guida" else "brochure cliente"
        doc = self.llm.call_json(prompts.SYSTEM_BASE, self._shared_with_lesson() + [self._task(self._doc_task(kind))],
                                 DOCUMENT_SCHEMA, label)
        return {"kind": kind, "doc": checks.normalize_document(doc)}

    # --- controllo -------------------------------------------------------
    def _source_numbers(self) -> set[float]:
        texts = [p["text"] for p in self.source["transcript"]["passages"]]
        for source in self.source.get("sources", []):
            texts += [p["text"] for p in source["passages"]]
        web = self.state.get("web")
        if web and web.get("stato") == "eseguita":
            texts.append(json.dumps(web.get("verifiche", []), ensure_ascii=False))
        numbers: set[float] = set()
        for text in texts:
            numbers |= checks.numbers_in(text)
        return numbers

    def automatic_issues(self, docs: dict[str, dict]) -> tuple[list[dict], dict]:
        issues: list[dict] = []
        source_numbers = self._source_numbers()
        for kind, doc in docs.items():
            issues += checks.check_arithmetic(doc, kind)
            issues += checks.check_unsupported_numbers(doc, kind, source_numbers)
        issues += checks.check_brochure_terms(docs["brochure"])
        passage_ids = [p["id"] for p in self.source["transcript"]["passages"]]
        coverage_issues, stats = checks.check_coverage(self.inventory, docs["lezione"], passage_ids)
        issues += coverage_issues
        issues += checks.check_limits(self.signals, docs)
        return issues, stats

    def step_check(self, args: dict) -> dict:
        docs = self.current_docs(use_revised=False)
        auto_issues, _ = self.automatic_issues(docs)
        content = [
            self._json_block("inventario", self.inventory),
            *self._web_block(),
            self._json_block("lezione_completa", docs["lezione"]),
            self._json_block("guida_di_studio", docs["guida"]),
            self._json_block("brochure_cliente", docs["brochure"]),
            self._json_block("segnalazioni_automatiche", auto_issues),
            self._task(prompts.CHECK_TASK),
        ]
        ai_check = self.llm.call_json(prompts.SYSTEM_BASE, content, CHECK_SCHEMA, "controllo di coerenza",
                                      effort=self.settings.effort_analysis)
        ai_issues = [{**i, "origine": "controllo AI"} for i in ai_check.get("problemi", [])]
        for missing in ai_check.get("argomenti_non_coperti", []) or []:
            ai_issues.append({"gravita": "media", "documento": "lezione", "posizione": "copertura",
                              "problema": f"Argomento non coperto: {missing}",
                              "correzione": "Aggiungere la trattazione dell'argomento.", "origine": "controllo AI"})
        to_fix = {kind: [i for i in ai_issues + auto_issues if i["documento"] == kind and i["gravita"] in ("alta", "media")]
                  for kind in DOC_KINDS}
        fix_count = sum(len(v) for v in to_fix.values())
        return {"ai_issues": ai_issues, "to_fix": to_fix,
                "detail": f"{fix_count} segnalazioni da correggere" if fix_count else "Nessun problema rilevante"}

    def step_revise(self, args: dict) -> dict:
        kind = args.get("kind")
        check = self.state.get("check") or {}
        issues = (check.get("to_fix") or {}).get(kind) or []
        if kind == "lezione":
            index = int(args.get("index", 0))
            plan, part = self._part_context(index)
            original = (self.state.get("lesson_parts") or {}).get(str(index))
            if not original:
                raise StepError("Parte della lezione mancante.")
            base = self._lesson_base()
            task = self._doc_task("lezione") + "\n\n" + prompts.REVISION_TASK
            if len(plan) > 1:
                task += "\n\n" + prompts.REVISION_PART_NOTE.format(number=index + 1, total=len(plan),
                                                                  topics=self._topic_titles(part["topic_ids"]))
            label = "revisione lezione" + (f" (parte {index + 1})" if len(plan) > 1 else "")
        elif kind in ("guida", "brochure"):
            original = (self.state.get("docs") or {}).get(kind)
            if not original:
                raise StepError(f"{DOC_KIND_LABEL[kind]} mancante.")
            base = self._shared_with_lesson()
            task = self._doc_task(kind) + "\n\n" + prompts.REVISION_TASK
            label = f"revisione {DOC_KIND_LABEL[kind].lower()}"
        else:
            raise StepError("Documento da revisionare non valido.")
        content = base + [self._json_block("documento_da_rivedere", original), self._json_block("problemi", issues),
                          self._task(task)]
        doc = checks.normalize_document(self.llm.call_json(prompts.SYSTEM_BASE, content, DOCUMENT_SCHEMA, label))
        return {"kind": kind, "index": str(args.get("index", "")), "doc": doc}

    # --- PDF e report -----------------------------------------------------
    def step_finalize(self, args: dict) -> dict:
        from pypdf import PdfReader

        docs = self.current_docs()
        residual, stats = self.automatic_issues(docs)
        revised = set((self.state.get("revised") or {}).keys())
        ai_issues = (self.state.get("check") or {}).get("ai_issues", [])
        remaining_ai = [i for i in ai_issues if i["documento"] not in revised]
        issues = sorted(residual + remaining_ai, key=lambda i: {"alta": 0, "media": 1, "bassa": 2}[i["gravita"]])
        files, warnings = [], []
        for kind in DOC_KINDS:
            data = render_pdf(docs[kind], kind, show_refs=(kind == "lezione" and self.settings.show_source_refs))
            reader = PdfReader(io.BytesIO(data))
            pages = len(reader.pages)
            extracted = "".join((p.extract_text() or "") for p in reader.pages[:2])
            if pages == 0 or len(extracted.strip()) < 50:
                raise StepError(f"Il PDF «{DOC_KIND_LABEL[kind]}» non contiene testo selezionabile.", status=500, retryable=True)
            files.append({"kind": kind, "label": DOC_KIND_LABEL[kind], "filename": file_name(kind, self.final_title),
                          "pages": pages, "size": len(data), "b64": base64.b64encode(data).decode()})
        pages = {f["kind"]: f["pages"] for f in files}
        if pages["guida"] > 7:
            warnings.append(f"La guida di studio è di {pages['guida']} pagine, oltre l'indicazione di 3–5: il contenuto è molto ampio.")
        if pages["brochure"] > 5:
            warnings.append(f"La brochure è di {pages['brochure']} pagine, oltre l'indicazione di 2–4.")
        suffix = zip_suffix(self.final_title)
        usage = args.get("usage") or {}
        report = self.report(issues, stats, warnings, usage)
        cost = estimated_cost(usage, self.settings) if usage else None
        return {
            "files": files,
            "report": {"filename": f"Report_verifica_{suffix}.txt", "text": report},
            "zip_name": f"Documenti_{suffix}.zip",
            "title": self.final_title,
            "issues": [i for i in issues if i["gravita"] in ("alta", "media")][:30],
            "issues_low": len([i for i in issues if i["gravita"] == "bassa"]),
            "coverage": stats,
            "warnings": warnings,
            "detail": ", ".join(f"{f['label']}: {f['pages']} pag." for f in files),
            "cost_usd": round(cost, 3) if cost is not None else None,
        }

    def report(self, issues: list[dict], coverage: dict, extra_warnings: list[str], usage: dict) -> str:
        transcript = self.source["transcript"]
        passages = transcript["passages"]
        lines = [
            "REPORT DI VERIFICA",
            f"Titolo: {self.final_title}",
            f"Generato il: {datetime.now().strftime('%d/%m/%Y %H:%M')} con il modello {self.settings.model}",
            "",
            "1. FONTE PRINCIPALE",
            f"- {transcript['name']}: {transcript['words']} parole, {len(passages)} passaggi (T001–T{len(passages):03d}).",
        ]
        if self.signals:
            lines.append("- Segnali rilevati:")
            lines += [f"  · {s}" for s in self.signals]
        lines += ["", "2. FONTI AGGIUNTIVE"]
        if self.source.get("sources"):
            for source in self.source["sources"]:
                meta = ", ".join(x for x in [source.get("metadata_title"), source.get("metadata_date"),
                                             f"{source['pages_total']} pagine" if source.get("pages_total") else ""] if x)
                lines.append(f"- D{source['index']}: {source['name']}" + (f" ({meta})" if meta else ""))
        else:
            lines.append("- Nessuna.")
        lines += ["", "3. VERIFICA WEB"]
        web = self.state.get("web")
        if not self.meta.get("web_search"):
            lines.append("- Non richiesta: nessuna condizione è stata verificata online.")
        elif not web or web.get("stato") != "eseguita":
            lines.append(f"- Non riuscita ({(web or {}).get('messaggio', '')}): nessuna condizione è dichiarata verificata.")
        else:
            lines.append(f"- Eseguita il {web['consultato_il']}. Documenti consultati:")
            lines += [f"  · {s.get('titolo') or '(senza titolo)'} — {s['url']}"
                      + (f" (data pagina: {s['data_pagina']})" if s.get("data_pagina") else "") for s in web["fonti"]]
            lines.append("- Esiti:")
            lines += [f"  · [{v['id']}] {v.get('affermazione', '')} → {v['esito']}"
                      + (f" ({v.get('fonte_url')})" if v.get("fonte_url") else "") for v in web["verifiche"]]
        lines += ["", "4. COPERTURA"] + [f"- {k.replace('_', ' ')}: {v}" for k, v in coverage.items()]
        lines += ["", "5. SEGNALAZIONI RESIDUE"]
        if issues:
            lines += [f"- [{i['gravita']}] {DOC_KIND_LABEL.get(i['documento'], i['documento'])} · {i['posizione']}: "
                      f"{i['problema']} ({i['origine']})" for i in issues]
        else:
            lines.append("- Nessuna.")
        warnings = list(self.state.get("warnings") or []) + extra_warnings
        if warnings:
            lines += ["", "6. AVVISI"] + [f"- {w}" for w in warnings]
        if usage:
            total_in = usage.get("token_input", 0) + usage.get("token_cache_scrittura", 0) + usage.get("token_cache_lettura", 0)
            lines += ["", "7. UTILIZZO API",
                      f"- Chiamate: {usage.get('chiamate', 0)}; token in ingresso: {total_in}; token in uscita: {usage.get('token_output', 0)}"]
            cost = estimated_cost(usage, self.settings)
            if cost is not None:
                lines.append(f"- Costo stimato: circa {cost:.2f} USD (stima indicativa sui prezzi di listino)")
        lines += ["", "8. INDICE DEI PASSAGGI DELLA TRASCRIZIONE (per verificare i riferimenti «Rif. fonte»)", ""]
        lines += [f"[{p['id']}] {p['text']}" for p in passages]
        return "\n".join(lines) + "\n"


def estimated_cost(usage: dict, settings: Settings) -> float | None:
    if settings.price_input is None or settings.price_output is None:
        return None
    return (usage.get("token_input", 0) * settings.price_input
            + usage.get("token_cache_scrittura", 0) * settings.price_input * 1.25
            + usage.get("token_cache_lettura", 0) * settings.price_input * 0.1
            + usage.get("token_output", 0) * settings.price_output) / 1_000_000


# ---------------------------------------------------------------------------
# Unione degli inventari dei segmenti (deterministica)
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def merge_inventories(partials: list[dict]) -> dict:
    merged: dict = {key: [] for key in ("relatori", "prodotti", "argomenti", "elementi", "termini", "domande_pubblico",
                                        "criticita", "info_commerciali_interne", "fonti_documentali")}
    merged.update({"titolo_proposto": "", "tema_generale": "", "data_lezione_rilevata": ""})
    topic_map: dict[str, str] = {}
    seen_elements: dict[str, dict] = {}
    for partial in partials:
        for key in ("titolo_proposto", "tema_generale", "data_lezione_rilevata"):
            if not merged[key] and partial.get(key):
                merged[key] = partial[key]
        for topic in partial.get("argomenti", []):
            new_id = f"A{len(merged['argomenti']) + 1}"
            topic_map[topic.get("id", "")] = new_id
            merged["argomenti"].append({**topic, "id": new_id})
        for element in partial.get("elementi", []):
            signature = _norm(element.get("testo", "")) + "|" + _norm(element.get("valore", ""))
            existing = seen_elements.get(signature)
            if existing and set(existing["riferimenti"]) & set(element.get("riferimenti", [])):
                # Stesso elemento letto due volte nel passaggio di sovrapposizione tra segmenti.
                existing["riferimenti"] = sorted(set(existing["riferimenti"]) | set(element.get("riferimenti", [])))
                continue
            new = {**element, "id": f"E{len(merged['elementi']) + 1}",
                   "argomento_id": topic_map.get(element.get("argomento_id", ""), ""),
                   "riferimenti": list(element.get("riferimenti", []))}
            seen_elements[signature] = new
            merged["elementi"].append(new)
        for key, name_field in (("relatori", "nome"), ("prodotti", "nome"), ("termini", "termine"), ("fonti_documentali", "id")):
            names = {_norm(x.get(name_field, "")) for x in merged[key]}
            for item in partial.get(key, []):
                if _norm(item.get(name_field, "")) not in names:
                    merged[key].append(item)
                    names.add(_norm(item.get(name_field, "")))
        for key in ("domande_pubblico", "criticita", "info_commerciali_interne"):
            merged[key] += partial.get(key, [])
    return merged


def apply_crosscheck(merged: dict, cross: dict) -> None:
    for key in ("titolo_proposto", "tema_generale", "data_lezione_rilevata"):
        if cross.get(key):
            merged[key] = cross[key]
    merged["criticita"] += cross.get("criticita", []) or []
    topics = {t["id"]: t for t in merged["argomenti"]}
    for group in cross.get("argomenti_duplicati", []) or []:
        group = [g for g in group if g in topics]
        if len(group) < 2:
            continue
        keep = topics[group[0]]
        for other_id in group[1:]:
            other = topics.pop(other_id)
            keep["riferimenti"] = sorted(set(keep.get("riferimenti", [])) | set(other.get("riferimenti", [])))
            keep["sottopunti"] = list(keep.get("sottopunti", [])) + list(other.get("sottopunti", []))
            for element in merged["elementi"]:
                if element.get("argomento_id") == other_id:
                    element["argomento_id"] = keep["id"]
    merged["argomenti"] = [t for t in merged["argomenti"] if t["id"] in topics]


def consulted_sources(blocks: list) -> tuple[list[dict], list[str]]:
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
                previous = sources.get(url, {})
                sources[url] = {"url": url, "titolo": previous.get("titolo", ""), "data_pagina": previous.get("data_pagina", ""),
                                "tipo": "documento letto", "letto_il": getattr(content, "retrieved_at", "") or ""}
            else:
                errors.append(str(getattr(content, "error_code", "errore")))
    return list(sources.values()), errors

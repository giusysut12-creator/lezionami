"""Controlli automatici che non dipendono dall'AI.

- ricalcolo delle operazioni esplicitate negli esempi;
- numeri dei documenti che non compaiono nelle fonti;
- termini commerciali interni o promesse nella brochure;
- copertura degli argomenti dell'inventario nella lezione;
- dichiarazione dei limiti quando la trascrizione è interrotta.
"""

from __future__ import annotations

import re
from typing import Iterable

DOC_LABELS = {"lezione": "Lezione completa", "guida": "Guida di studio", "brochure": "Brochure cliente"}

NUMBER_RE = re.compile(r"(?<![\w.,])(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?)(?![\w])")


def parse_it_number(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", raw):
        raw = raw.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+,\d+", raw):
        raw = raw.replace(",", ".")
    elif re.fullmatch(r"\d+\.\d+", raw):
        pass  # formato con punto decimale
    elif not re.fullmatch(r"\d+", raw):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def numbers_in(text: str) -> set[float]:
    found: set[float] = set()
    for match in NUMBER_RE.finditer(text):
        value = parse_it_number(match.group(1))
        if value is not None:
            found.add(round(value, 4))
    return found


def block_text(block: dict) -> str:
    parts = [block.get("title", ""), block.get("text", "")]
    parts.extend(block.get("items", []) or [])
    parts.extend(block.get("columns", []) or [])
    for row in block.get("rows", []) or []:
        parts.extend(row)
    return "\n".join(p for p in parts if p)


def document_text(doc: dict, include_examples: bool = True) -> str:
    parts = [doc.get("title", ""), doc.get("subtitle", ""), doc.get("limits_notice", "")]
    for section in doc.get("sections", []):
        parts.append(section.get("heading", ""))
        for block in section.get("blocks", []):
            if not include_examples and block.get("type") == "example":
                continue
            parts.append(block_text(block))
    return "\n".join(p for p in parts if p)


def _issue(severity: str, document: str, position: str, problem: str, fix: str) -> dict:
    return {
        "gravita": severity, "documento": document, "posizione": position,
        "problema": problem, "correzione": fix, "origine": "controllo automatico",
    }


# ---------------------------------------------------------------------------
# Ricalcolo delle operazioni
# ---------------------------------------------------------------------------

_NUM = r"\d[\d.,]*\d|\d"
_UNIT = r"(?:\s*%)?(?:\s*(?:€|euro|eur|punti))?"
_OP = r"(?:\s*[×*·+/:−]\s*|\s+[x\-]\s+)"
CALC_RE = re.compile(
    rf"(?P<lhs>(?:{_NUM}){_UNIT}(?:{_OP}(?:{_NUM}){_UNIT})+)\s*=\s*(?P<rhs>{_NUM})(?P<rhs_pct>\s*%)?",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(rf"({_NUM})(\s*%)?|([×*·+/:−x\-])", re.IGNORECASE)


def _evaluate(lhs: str, percent_as_fraction: bool) -> float | None:
    expression = []
    for match in _TOKEN_RE.finditer(lhs):
        number, percent, operator = match.groups()
        if number:
            value = parse_it_number(number)
            if value is None:
                return None
            if percent and percent_as_fraction:
                value /= 100
            expression.append(repr(value))
        elif operator:
            expression.append({"×": "*", "x": "*", "X": "*", "·": "*", ":": "/", "−": "-"}.get(operator, operator))
    if len(expression) < 3 or len(expression) % 2 == 0:
        return None
    source = " ".join(expression)
    if not re.fullmatch(r"[\d.e+\-*/ ]+", source):
        return None
    try:
        return float(eval(source, {"__builtins__": {}}, {}))  # solo numeri e operatori validati
    except (ZeroDivisionError, SyntaxError, ValueError):
        return None


def _matches(expected: float, stated: float, stated_raw: str) -> bool:
    decimals = len(stated_raw.split(",")[1]) if "," in stated_raw else 0
    tolerance = max(0.5 * 10 ** (-decimals) + 1e-9, abs(stated) * 0.002)
    return abs(expected - stated) <= tolerance


def check_arithmetic(doc: dict, document: str) -> list[dict]:
    issues: list[dict] = []
    for s_index, section in enumerate(doc.get("sections", []), start=1):
        for b_index, block in enumerate(section.get("blocks", []), start=1):
            text = block_text(block)
            for match in CALC_RE.finditer(text):
                before = text[max(0, match.start() - 2):match.start()]
                if "(" in before:
                    continue
                stated_raw = match.group("rhs")
                stated = parse_it_number(stated_raw)
                if stated is None:
                    continue
                candidates = [_evaluate(match.group("lhs"), True), _evaluate(match.group("lhs"), False)]
                candidates = [c for c in candidates if c is not None]
                if not candidates:
                    continue
                if match.group("rhs_pct"):
                    candidates += [c * 100 for c in candidates]
                if any(_matches(c, stated, stated_raw) for c in candidates):
                    continue
                issues.append(_issue(
                    "alta", document,
                    f"sezione {s_index} «{section.get('heading', '')}», blocco {b_index}",
                    f"Calcolo non corretto: «{match.group(0).strip()}» (risultato atteso circa {candidates[0]:.4g}).",
                    "Ricalcolare l'operazione e correggere il risultato o i dati dell'esempio.",
                ))
    return issues


# ---------------------------------------------------------------------------
# Numeri non presenti nelle fonti
# ---------------------------------------------------------------------------

def check_unsupported_numbers(doc: dict, document: str, source_numbers: set[float]) -> list[dict]:
    doc_numbers = numbers_in(document_text(doc, include_examples=False))
    unsupported = sorted(
        n for n in doc_numbers
        if n not in source_numbers and not (n.is_integer() and 0 <= n <= 12)
        and not (n.is_integer() and 1900 <= n <= 2100)
    )
    if not unsupported:
        return []
    shown = ", ".join(f"{n:g}".replace(".", ",") for n in unsupported[:15])
    return [_issue(
        "bassa", document, "intero documento",
        f"Numeri non trovati letteralmente nelle fonti (possono essere calcoli derivati legittimi): {shown}.",
        "Verificare che siano derivati correttamente dalle fonti oppure segnalarli come esempio.",
    )]


# ---------------------------------------------------------------------------
# Brochure: contenuti interni e promesse
# ---------------------------------------------------------------------------

INTERNAL_TERMS = [
    r"provvigion\w*", r"retrocession\w*", r"remunerazione\s+della\s+rete", r"compens\w+\s+(della|alla)\s+rete",
    r"incentiv\w*\s+(commerciali|di\s+vendita|alla\s+rete)", r"obiettiv\w+\s+(di\s+vendita|commerciali)",
    r"\bbudget\b", r"target\s+commerciale", r"cross[- ]?selling", r"up[- ]?selling", r"\bpipeline\b",
    r"\bgara\s+commerciale\b", r"\bcampagna\s+(di\s+)?vendita\b", r"\bportafoglio\s+clienti\b", r"\bupfront\b",
    r"\brecurring\b", r"\bcontest\b",
]
PROMISE_TERMS = [
    r"senza\s+(alcun\s+)?rischi\w*", r"rendimento\s+(garantito|assicurato|certo)", r"guadagno\s+(garantito|sicuro|certo)",
    r"\bsicurissim\w*", r"\bnessun\s+rischio\b", r"\bconviene\s+sempre\b", r"\bcapitale\s+sempre\s+garantito\b",
    r"\bzero\s+rischi\b",
]


def check_brochure_terms(doc: dict) -> list[dict]:
    issues: list[dict] = []
    text = document_text(doc).lower()
    for pattern in INTERNAL_TERMS:
        match = re.search(pattern, text)
        if match:
            issues.append(_issue(
                "alta", "brochure", "testo",
                f"La brochure contiene un riferimento commerciale interno: «{match.group(0)}».",
                "Eliminare remunerazione della rete, obiettivi di vendita e gergo interno dalla brochure.",
            ))
    for pattern in PROMISE_TERMS:
        match = re.search(pattern, text)
        if match:
            issues.append(_issue(
                "alta", "brochure", "testo",
                f"Possibile promessa commerciale non supportata: «{match.group(0)}».",
                "Riformulare senza promesse; indicare garanzie solo se contrattuali e nei loro limiti.",
            ))
    for section in doc.get("sections", []):
        for block in section.get("blocks", []):
            if block.get("type") == "internal":
                issues.append(_issue(
                    "alta", "brochure", f"sezione «{section.get('heading', '')}»",
                    "La brochure contiene un blocco di informazioni interne.",
                    "Rimuovere il blocco interno dalla brochure.",
                ))
    return issues


# ---------------------------------------------------------------------------
# Copertura e limiti
# ---------------------------------------------------------------------------

def _doc_refs(doc: dict) -> set[str]:
    refs: set[str] = set()
    for section in doc.get("sections", []):
        for block in section.get("blocks", []):
            refs.update(r.strip() for r in block.get("refs", []) or [])
    return refs


def _keywords(title: str) -> list[str]:
    stop = {"della", "delle", "degli", "dello", "nella", "nelle", "sulla", "sulle", "come", "cosa", "quando", "perché", "anche", "dopo", "prima", "senza", "tutti", "tutte", "questa", "questo"}
    return [w for w in re.findall(r"[a-zàèéìòù]{5,}", title.lower()) if w not in stop]


def check_coverage(inventory: dict, lesson: dict, passage_ids: list[str]) -> tuple[list[dict], dict]:
    lesson_refs = _doc_refs(lesson)
    lesson_text = document_text(lesson).lower()
    uncovered = []
    for topic in inventory.get("argomenti", []):
        refs = set(topic.get("riferimenti", []))
        if refs & lesson_refs:
            continue
        words = _keywords(topic.get("titolo", ""))
        if words and sum(1 for w in words if w in lesson_text) >= max(1, len(words) // 2):
            continue
        uncovered.append(topic)
    issues = [
        _issue(
            "media", "lezione", "copertura",
            f"L'argomento «{t.get('titolo', '')}» ({', '.join(t.get('riferimenti', [])[:6])}) non sembra trattato nella lezione.",
            "Aggiungere una trattazione dell'argomento basata sui passaggi indicati.",
        )
        for t in uncovered
    ]
    transcript_ids = [p for p in passage_ids if p.startswith("T")]
    cited_transcript = {r for r in lesson_refs if r.startswith("T")}
    tail = set(transcript_ids[int(len(transcript_ids) * 0.8):]) if transcript_ids else set()
    inventory_refs = set()
    for element in inventory.get("elementi", []):
        inventory_refs.update(element.get("riferimenti", []))
    for topic in inventory.get("argomenti", []):
        inventory_refs.update(topic.get("riferimenti", []))
    stats = {
        "argomenti_inventario": len(inventory.get("argomenti", [])),
        "argomenti_non_coperti": len(uncovered),
        "passaggi_trascrizione": len(transcript_ids),
        "passaggi_citati_nella_lezione": len(cited_transcript & set(transcript_ids)),
        "passaggi_finali_citati": len(cited_transcript & tail),
        "elementi_inventario": len(inventory.get("elementi", [])),
    }
    tail_with_content = tail & inventory_refs
    if tail_with_content and not (cited_transcript & tail):
        issues.append(_issue(
            "media", "lezione", "parte finale",
            "La lezione non cita alcun passaggio dell'ultimo quinto della trascrizione, che contiene elementi inventariati.",
            "Verificare che gli argomenti finali siano trattati.",
        ))
    return issues, stats


def check_limits(signals: Iterable[str], docs: dict[str, dict]) -> list[dict]:
    signals = list(signals)
    if not any("interruzione" in s.lower() or "interrotta" in s.lower() for s in signals):
        return []
    issues = []
    for key, doc in docs.items():
        notice = (doc.get("limits_notice") or "").strip()
        if not notice:
            issues.append(_issue(
                "alta", key, "avviso sui limiti",
                "La trascrizione risulta interrotta ma il documento non dichiara il limite della fonte.",
                "Compilare l'avviso sui limiti indicando che la trascrizione è incompleta.",
            ))
    return issues


def normalize_document(doc: dict) -> dict:
    """Ripulisce la struttura: tabelle con celle mancanti, sezioni vuote."""
    sections = []
    for section in doc.get("sections", []) or []:
        blocks = []
        for block in section.get("blocks", []) or []:
            block = dict(block)
            for key in ("title", "text"):
                block[key] = (block.get(key) or "").strip()
            block["items"] = [i.strip() for i in block.get("items", []) or [] if i and i.strip()]
            block["refs"] = [r for r in block.get("refs", []) or [] if r]
            if block.get("type") == "table":
                columns = [c.strip() for c in block.get("columns", []) or []]
                rows = [list(r) for r in block.get("rows", []) or [] if any((c or "").strip() for c in r)]
                width = max([len(columns)] + [len(r) for r in rows]) if (columns or rows) else 0
                if width == 0:
                    continue
                columns = columns + [""] * (width - len(columns))
                rows = [[(c or "").strip() for c in r] + [""] * (width - len(r)) for r in rows]
                block["columns"], block["rows"] = columns, rows
            else:
                block["columns"], block["rows"] = [], []
            if not (block["title"] or block["text"] or block["items"] or block.get("rows")):
                continue
            blocks.append(block)
        if blocks or section.get("heading"):
            sections.append({"heading": (section.get("heading") or "").strip(), "blocks": blocks})
    doc = dict(doc)
    doc["sections"] = sections
    for key in ("title", "subtitle", "footer_label", "limits_notice"):
        doc[key] = (doc.get(key) or "").strip()
    return doc

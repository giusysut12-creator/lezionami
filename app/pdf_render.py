"""Fase 5: creazione dei PDF (A4, testo selezionabile) con ReportLab."""

from __future__ import annotations

import io
import re
import unicodedata
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    CondPageBreak, KeepTogether, LayoutError, ListFlowable, ListItem, Paragraph,
    SimpleDocTemplate, Spacer, Table, TableStyle,
)
from reportlab.lib.fonts import addMapping

from .config import APP_DIR

ACCENT = colors.HexColor("#7975E4")
ACCENT_DARK = colors.HexColor("#4F4BB8")
ACCENT_SOFT = colors.HexColor("#ECEBFB")
ACCENT_FAINT = colors.HexColor("#F6F5FE")
INK = colors.HexColor("#1D1B2E")
MUTED = colors.HexColor("#6B6A7B")
RULE = colors.HexColor("#D9D7F2")
WARN_BG = colors.HexColor("#FFF6E8")
WARN_LINE = colors.HexColor("#E2A23B")
INTERNAL_BG = colors.HexColor("#F1F1F4")
INTERNAL_LINE = colors.HexColor("#8C8AA0")
SOURCE_BG = colors.HexColor("#EEF6F3")
SOURCE_LINE = colors.HexColor("#3E9C7E")

FONT = "DejaVuSans"
FONT_BOLD = "DejaVuSans-Bold"
_fonts_registered = False

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN_X = 20 * mm
MARGIN_TOP = 20 * mm
MARGIN_BOTTOM = 22 * mm
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN_X

DOC_KIND_LABEL = {
    "lezione": "Lezione completa",
    "guida": "Guida di studio",
    "brochure": "Brochure cliente",
}
FILE_PREFIX = {
    "lezione": "Lezione_completa",
    "guida": "Guida_studio",
    "brochure": "Brochure_cliente",
}


def register_fonts() -> None:
    global _fonts_registered
    if _fonts_registered:
        return
    fonts_dir = APP_DIR / "fonts"
    pdfmetrics.registerFont(TTFont(FONT, str(fonts_dir / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(fonts_dir / "DejaVuSans-Bold.ttf")))
    # Nessun corsivo incluso: <i> ricade sul regolare, per non generare errori.
    addMapping(FONT, 0, 0, FONT)
    addMapping(FONT, 1, 0, FONT_BOLD)
    addMapping(FONT, 0, 1, FONT)
    addMapping(FONT, 1, 1, FONT_BOLD)
    _fonts_registered = True


def safe_filename_part(title: str) -> str:
    text = unicodedata.normalize("NFKD", title or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return (text[:60].rstrip("_")) or "Documento"


def file_name(kind: str, title: str) -> str:
    return f"{FILE_PREFIX[kind]}_{safe_filename_part(title)}.pdf"


# ---------------------------------------------------------------------------
# Stili
# ---------------------------------------------------------------------------

def _styles() -> dict[str, ParagraphStyle]:
    register_fonts()
    base = ParagraphStyle("base", fontName=FONT, fontSize=10, leading=14.6, textColor=INK,
                          alignment=TA_LEFT, spaceAfter=6.5)
    return {
        "body": base,
        "title": ParagraphStyle("title", parent=base, fontName=FONT_BOLD, fontSize=23, leading=28,
                                textColor=INK, spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", parent=base, fontSize=10.5, leading=14.5, textColor=MUTED,
                                   spaceAfter=0),
        "kind": ParagraphStyle("kind", parent=base, fontName=FONT_BOLD, fontSize=8.5, leading=11,
                               textColor=ACCENT_DARK, spaceAfter=5),
        "h1": ParagraphStyle("h1", parent=base, fontName=FONT_BOLD, fontSize=15, leading=19,
                             textColor=INK, spaceBefore=14, spaceAfter=7),
        "h2": ParagraphStyle("h2", parent=base, fontName=FONT_BOLD, fontSize=11.2, leading=15,
                             textColor=ACCENT_DARK, spaceBefore=8, spaceAfter=4),
        "list": ParagraphStyle("list", parent=base, spaceAfter=2.5),
        "cell": ParagraphStyle("cell", parent=base, fontSize=8.8, leading=11.8, spaceAfter=0),
        "cell_head": ParagraphStyle("cell_head", parent=base, fontName=FONT_BOLD, fontSize=8.8,
                                    leading=11.8, spaceAfter=0, textColor=INK),
        "box_label": ParagraphStyle("box_label", parent=base, fontName=FONT_BOLD, fontSize=7.8,
                                    leading=10, spaceAfter=2.5, textColor=ACCENT_DARK),
        "box_title": ParagraphStyle("box_title", parent=base, fontName=FONT_BOLD, fontSize=9.8,
                                    leading=13.5, spaceAfter=3),
        "box_body": ParagraphStyle("box_body", parent=base, fontSize=9.4, leading=13.4, spaceAfter=3),
        "refs": ParagraphStyle("refs", parent=base, fontSize=7, leading=9, textColor=MUTED,
                               spaceBefore=-4, spaceAfter=6),
        "qa_q": ParagraphStyle("qa_q", parent=base, fontName=FONT_BOLD, spaceAfter=2),
        "notice": ParagraphStyle("notice", parent=base, fontSize=9, leading=13, textColor=INK, spaceAfter=0),
    }


def inline(text: str) -> str:
    """Testo sicuro per Paragraph: escape XML, **grassetto**, a capo."""
    text = escape(text or "")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = text.replace("**", "")
    return text.replace("\n", "<br/>")


def _refs_text(refs: list[str]) -> str:
    refs = [r for r in refs if r]
    if not refs:
        return ""
    shown = refs[:10]
    more = f" +{len(refs) - 10}" if len(refs) > 10 else ""
    return "Rif. fonte: " + ", ".join(shown) + more


# ---------------------------------------------------------------------------
# Tabelle e riquadri
# ---------------------------------------------------------------------------

def _column_widths(columns: list[str], rows: list[list[str]], total: float) -> list[float]:
    count = len(columns)
    weights = []
    for index in range(count):
        cells = [columns[index]] + [r[index] for r in rows if index < len(r)]
        lengths = [len(c or "") for c in cells]
        longest_word = max((len(w) for c in cells for w in (c or "").split()), default=4)
        avg = sum(lengths) / max(len(lengths), 1)
        weights.append(max(avg, longest_word * 1.4, 6) ** 0.85)
    total_weight = sum(weights) or 1
    widths = [total * w / total_weight for w in weights]
    minimum = min(26 * mm, total / count)
    for _ in range(3):
        deficit = sum(minimum - w for w in widths if w < minimum)
        if deficit <= 0:
            break
        big = [i for i, w in enumerate(widths) if w > minimum]
        big_total = sum(widths[i] for i in big) or 1
        widths = [minimum if w < minimum else w - deficit * (w / big_total) for w in widths]
    return widths


def build_table(block: dict, styles: dict, split_rows: bool = False) -> list:
    columns = block.get("columns", [])
    rows = block.get("rows", [])
    if not columns and not rows:
        return []
    data = []
    has_header = any(c for c in columns)
    if has_header:
        data.append([Paragraph(inline(c), styles["cell_head"]) for c in columns])
    for row in rows:
        data.append([Paragraph(inline(c), styles["cell"]) for c in row])
    widths = _column_widths(columns or rows[0], rows, CONTENT_WIDTH)
    table = Table(data, colWidths=widths, repeatRows=1 if has_header else 0,
                  splitByRow=1, splitInRow=1 if split_rows else 0, hAlign="LEFT")
    commands = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5.5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, RULE),
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
    ]
    start = 1 if has_header else 0
    if has_header:
        commands += [("BACKGROUND", (0, 0), (-1, 0), ACCENT_SOFT),
                     ("LINEBELOW", (0, 0), (-1, 0), 1, ACCENT)]
    for index in range(start, len(data)):
        if (index - start) % 2 == 1:
            commands.append(("BACKGROUND", (0, index), (-1, index), ACCENT_FAINT))
    table.setStyle(TableStyle(commands))
    title = block.get("title")
    flow = [Spacer(1, 2)]
    if title:
        flow.append(Paragraph(inline(title), styles["box_title"]))
    flow += [table, Spacer(1, 8)]
    return flow


BOX_STYLES = {
    "example": ("ESEMPIO DIDATTICO · ipotetico, non tratto dai dati originali", ACCENT_FAINT, ACCENT),
    "note": ("NOTA", ACCENT_FAINT, ACCENT),
    "warning": ("ATTENZIONE · punto incompleto, ambiguo o da verificare", WARN_BG, WARN_LINE),
    "internal": ("USO INTERNO · non destinato al cliente", INTERNAL_BG, INTERNAL_LINE),
    "source_note": ("INTEGRAZIONE DA FONTE ESTERNA · non detta dal relatore", SOURCE_BG, SOURCE_LINE),
}


def build_box(block: dict, styles: dict, kind: str, split_rows: bool = False) -> list:
    label, background, line = BOX_STYLES[block["type"]]
    if kind == "brochure" and block["type"] == "example":
        label = "ESEMPIO · cifre ipotetiche"
    if kind == "brochure" and block["type"] == "source_note":
        label = "DALLA DOCUMENTAZIONE UFFICIALE"
    label_style = ParagraphStyle("lbl", parent=styles["box_label"], textColor=line if block["type"] != "example" and block["type"] != "note" else ACCENT_DARK)
    inner: list = [Paragraph(label, label_style)]
    if block.get("title"):
        inner.append(Paragraph(inline(block["title"]), styles["box_title"]))
    if block.get("text"):
        inner.append(Paragraph(inline(block["text"]), styles["box_body"]))
    if block.get("items"):
        inner.append(_list(block["items"], styles, numbered=False, style=styles["box_body"]))
    if block.get("rows"):
        inner.extend(build_table(block, styles, split_rows=True)[1:-1])
    table = Table([[inner]], colWidths=[CONTENT_WIDTH], splitInRow=1 if split_rows else 0, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), background),
        ("LINEBEFORE", (0, 0), (0, -1), 3, line),
        ("LEFTPADDING", (0, 0), (-1, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [Spacer(1, 3), table, Spacer(1, 9)]


def _list(items: list[str], styles: dict, numbered: bool, style: ParagraphStyle | None = None) -> ListFlowable:
    style = style or styles["list"]
    flow_items = [ListItem(Paragraph(inline(item), style), leftIndent=14) for item in items]
    if numbered:
        return ListFlowable(flow_items, bulletType="1", bulletFormat="%s.", bulletFontName=FONT_BOLD,
                            bulletFontSize=9, bulletColor=ACCENT_DARK, leftIndent=16, spaceAfter=6)
    return ListFlowable(flow_items, bulletType="bullet", start="•", bulletFontName=FONT_BOLD,
                        bulletFontSize=10, bulletColor=ACCENT, leftIndent=14, spaceAfter=6)


def build_block(block: dict, styles: dict, kind: str, show_refs: bool, split_rows: bool) -> list:
    kind_of_block = block.get("type", "paragraph")
    if kind == "brochure" and kind_of_block == "internal":
        return []  # mai contenuti interni nella brochure
    flow: list = []
    if kind_of_block == "subheading":
        title = block.get("title") or block.get("text")
        flow.append(Paragraph(inline(title), styles["h2"]))
        if block.get("title") and block.get("text"):
            flow.append(Paragraph(inline(block["text"]), styles["body"]))
    elif kind_of_block in ("bullets", "numbered"):
        if block.get("title"):
            flow.append(Paragraph(inline(block["title"]), styles["box_title"]))
        if block.get("text"):
            flow.append(Paragraph(inline(block["text"]), styles["body"]))
        if block.get("items"):
            flow.append(_list(block["items"], styles, numbered=kind_of_block == "numbered"))
    elif kind_of_block == "table":
        if block.get("text"):
            flow.append(Paragraph(inline(block["text"]), styles["body"]))
        flow.extend(build_table(block, styles, split_rows=split_rows))
    elif kind_of_block in BOX_STYLES:
        flow.extend(build_box(block, styles, kind, split_rows=split_rows))
    elif kind_of_block == "qa":
        if block.get("title"):
            flow.append(Paragraph(inline(block["title"]), styles["qa_q"]))
        if block.get("text"):
            flow.append(Paragraph(inline(block["text"]), styles["body"]))
    else:
        if block.get("title"):
            flow.append(Paragraph(inline(block["title"]), styles["box_title"]))
        if block.get("text"):
            flow.append(Paragraph(inline(block["text"]), styles["body"]))
        if block.get("items"):
            flow.append(_list(block["items"], styles, numbered=False))
    if show_refs and flow and kind_of_block != "subheading":
        refs = _refs_text(block.get("refs", []))
        if refs:
            flow.append(Paragraph(escape(refs), styles["refs"]))
    return flow


# ---------------------------------------------------------------------------
# Pagina: piè di pagina con numerazione "n / totale"
# ---------------------------------------------------------------------------

def _numbered_canvas_factory(footer_left: str):
    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_states = []

        def showPage(self):
            self._saved_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved_states)
            for state in self._saved_states:
                self.__dict__.update(state)
                self._draw_footer(total)
                super().showPage()
            super().save()

        def _draw_footer(self, total: int):
            self.saveState()
            self.setStrokeColor(RULE)
            self.setLineWidth(0.5)
            y = MARGIN_BOTTOM - 9 * mm
            self.line(MARGIN_X, y + 4.2 * mm, PAGE_WIDTH - MARGIN_X, y + 4.2 * mm)
            self.setFont(FONT, 7.8)
            self.setFillColor(MUTED)
            label = footer_left
            while label and pdfmetrics.stringWidth(label, FONT, 7.8) > CONTENT_WIDTH - 30 * mm:
                label = label[:-2]
            self.drawString(MARGIN_X, y, label)
            self.setFillColor(ACCENT_DARK)
            self.setFont(FONT_BOLD, 7.8)
            self.drawRightString(PAGE_WIDTH - MARGIN_X, y, f"{self._pageNumber} / {total}")
            self.setFillColor(ACCENT)
            self.rect(0, PAGE_HEIGHT - 4, PAGE_WIDTH, 4, stroke=0, fill=1)
            self.restoreState()

    return NumberedCanvas


def _story(doc: dict, kind: str, show_refs: bool, split_rows: bool) -> list:
    styles = _styles()
    story: list = [
        Paragraph(DOC_KIND_LABEL[kind].upper(), styles["kind"]),
        Paragraph(inline(doc.get("title") or DOC_KIND_LABEL[kind]), styles["title"]),
    ]
    if doc.get("subtitle"):
        story.append(Paragraph(inline(doc["subtitle"]), styles["subtitle"]))
    rule = Table([[""]], colWidths=[34 * mm], rowHeights=[2.2])
    rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT)]))
    rule.hAlign = "LEFT"
    story += [Spacer(1, 8), rule, Spacer(1, 10)]
    if doc.get("limits_notice"):
        notice = Table([[Paragraph("<b>Limiti della fonte.</b> " + inline(doc["limits_notice"]), styles["notice"])]],
                       colWidths=[CONTENT_WIDTH], hAlign="LEFT")
        notice.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), WARN_BG),
            ("LINEBEFORE", (0, 0), (0, -1), 3, WARN_LINE),
            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story += [notice, Spacer(1, 6)]

    for index, section in enumerate(doc.get("sections", [])):
        blocks_flow = [build_block(b, styles, kind, show_refs, split_rows) for b in section.get("blocks", [])]
        blocks_flow = [f for f in blocks_flow if f]
        heading = section.get("heading")
        if heading:
            # Il titolo non resta mai da solo a fondo pagina: viaggia con il primo blocco.
            story.append(CondPageBreak(60 * mm if index else 0))
            first = blocks_flow.pop(0) if blocks_flow else []
            story.append(KeepTogether([Paragraph(inline(heading), styles["h1"])] + first))
        for flow in blocks_flow:
            if flow and isinstance(flow[0], Paragraph) and flow[0].style.name == "h2":
                story.append(CondPageBreak(45 * mm))
            if len(flow) <= 3 and not any(isinstance(f, Table) and len(f._cellvalues) > 8 for f in flow):
                story.append(KeepTogether(flow))
            else:
                story.extend(flow)
    return story


def render_pdf(doc: dict, kind: str, show_refs: bool = False) -> bytes:
    footer = doc.get("footer_label") or doc.get("title") or DOC_KIND_LABEL[kind]
    footer = f"{footer} · {DOC_KIND_LABEL[kind]}"
    last_error: Exception | None = None
    for split_rows in (False, True):
        buffer = io.BytesIO()
        template = SimpleDocTemplate(
            buffer, pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X,
            topMargin=MARGIN_TOP, bottomMargin=MARGIN_BOTTOM,
            title=f"{DOC_KIND_LABEL[kind]}: {doc.get('title', '')}",
            author="Lezionami", subject=DOC_KIND_LABEL[kind], creator="Lezionami",
            lang="it-IT",
        )
        try:
            template.build(_story(doc, kind, show_refs, split_rows),
                           canvasmaker=_numbered_canvas_factory(footer))
            return buffer.getvalue()
        except LayoutError as exc:
            # Una riga di tabella più alta di una pagina: si riprova consentendo
            # la divisione all'interno della riga, solo come ultima risorsa.
            last_error = exc
    raise RuntimeError(f"Impaginazione non riuscita: {last_error}")

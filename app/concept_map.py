"""Mappa concettuale in PDF: A4 orizzontale.

Pagina 1: albero numerato (tema centrale → rami → punti → approfondimenti →
sintesi), disegnato dal programma per avere sempre un'impaginazione pulita.
Pagine successive: «Cosa dire per ogni punto della mappa» su due colonne.
"""

from __future__ import annotations

import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, NextPageTemplate, PageBreak, PageTemplate, Paragraph, Spacer,
)

from .pdf_render import FONT, FONT_BOLD, register_fonts

PAGE_W, PAGE_H = landscape(A4)
MARGIN = 14 * mm

BG = colors.HexColor("#F7F3EC")
INK = colors.HexColor("#22253A")
MUTED = colors.HexColor("#6E6A75")
LINE = colors.HexColor("#C9C2B6")
EYEBROW = colors.HexColor("#8A6A2E")
CONCLUSION_BG = colors.HexColor("#FBF1E3")
CONCLUSION_LINE = colors.HexColor("#D9B88A")
CONCLUSION_INK = colors.HexColor("#6B4A12")
# Un colore per ramo: blu notte, viola, terracotta.
BRANCH_COLORS = [colors.HexColor("#22253A"), colors.HexColor("#5B4A9E"), colors.HexColor("#B5532C")]
BRANCH_SOFT = [colors.HexColor("#F1F2F6"), colors.HexColor("#F4F1FA"), colors.HexColor("#FBF0EA")]


def _clean(node) -> dict:
    node = node or {}
    return {"label": (node.get("label") or "").strip(), "text": (node.get("text") or "").strip()}


def number_map(data: dict) -> dict:
    """Normalizza la mappa e assegna i numeri ai nodi, nell'ordine di lettura."""
    branches = []
    for branch in (data.get("branches") or [])[:3]:
        children = []
        for child in (branch.get("children") or [])[:4]:
            item = _clean(child)
            if not item["label"]:
                continue
            detail = _clean(child.get("detail"))
            item["detail"] = detail if detail["label"] else None
            children.append(item)
        item = _clean(branch)
        if item["label"]:
            item["children"] = children
            branches.append(item)
    counter = 1
    root = _clean(data.get("root")) or {"label": data.get("title", ""), "text": ""}
    root["n"] = counter
    for branch in branches:
        counter += 1
        branch["n"] = counter
        for child in branch["children"]:
            counter += 1
            child["n"] = counter
        for child in branch["children"]:
            if child["detail"]:
                counter += 1
                child["detail"]["n"] = counter
    conclusion = _clean(data.get("conclusion"))
    if conclusion["label"]:
        counter += 1
        conclusion["n"] = counter
    else:
        conclusion = None
    return {"eyebrow": (data.get("eyebrow") or "").strip(), "title": (data.get("title") or "").strip(),
            "root": root, "branches": branches, "conclusion": conclusion}


def _style(size: float, color, bold: bool = True, align=TA_CENTER) -> ParagraphStyle:
    return ParagraphStyle("s", fontName=FONT_BOLD if bold else FONT, fontSize=size, leading=size * 1.3,
                          textColor=color, alignment=align)


class MapDiagram(Flowable):
    """Disegna l'albero della mappa occupando l'intera area utile della pagina."""

    def __init__(self, data: dict, width: float, height: float):
        super().__init__()
        self.data = data
        self.width = width
        self.height = height

    def wrap(self, avail_w, avail_h):
        return self.width, self.height

    # -- riquadri -------------------------------------------------------------
    def _para(self, node, size, color):
        text = f"{node['n']} · {escape(node['label'])}"
        return Paragraph(text, _style(size, color))

    def _box_height(self, para, width, pad=8, min_h=0):
        _, h = para.wrap(width - 2 * pad, 1000)
        return max(h + 2 * pad, min_h)

    def _draw_box(self, x, y_top, w, h, para, fill, stroke, dashed=False, radius=6, pad=8):
        c = self.canv
        c.saveState()
        c.setFillColor(fill)
        c.setStrokeColor(stroke)
        c.setLineWidth(1.1)
        if dashed:
            c.setDash(2.5, 2)
        c.roundRect(x, y_top - h, w, h, radius, stroke=1, fill=1)
        c.restoreState()
        _, ph = para.wrap(w - 2 * pad, 1000)
        para.drawOn(c, x + pad, y_top - h / 2 - ph / 2)

    def _line(self, x1, y1, x2, y2):
        c = self.canv
        c.saveState()
        c.setStrokeColor(LINE)
        c.setLineWidth(1)
        c.line(x1, y1, x2, y2)
        c.restoreState()

    def draw(self):
        c = self.canv
        data = self.data
        W, H = self.width, self.height
        top = H

        # Intestazione
        if data["eyebrow"]:
            c.setFont(FONT_BOLD, 7.5)
            c.setFillColor(EYEBROW)
            c.drawCentredString(W / 2, top - 10, data["eyebrow"].upper()[:110])
        title = Paragraph(escape(data["title"]), _style(17, INK))
        _, th = title.wrap(W * 0.8, 200)
        title.drawOn(c, W * 0.1, top - 18 - th)
        y = top - 18 - th - 18

        # Tema centrale
        root = data["root"]
        root_w = min(260, W * 0.4)
        root_p = self._para(root, 10.5, colors.white)
        root_h = self._box_height(root_p, root_w, pad=10, min_h=40)
        self._draw_box(W / 2 - root_w / 2, y, root_w, root_h, root_p, INK, INK, radius=7, pad=10)
        root_bottom = y - root_h

        branches = data["branches"]
        if not branches:
            return
        # Spazio orizzontale proporzionale al numero di punti di ciascun ramo
        gap = 14
        weights = [max(len(b["children"]), 1) for b in branches]
        total = sum(weights)
        usable = W - gap * (len(branches) - 1)
        regions, x = [], 0.0
        for weight in weights:
            width = usable * weight / total
            regions.append((x, width))
            x += width + gap

        # Dimensione del testo in base all'affollamento
        crowded = total > 8
        child_size = 7.6 if crowded else 8.4
        detail_size = 7.0 if crowded else 7.6

        branch_y = root_bottom - 44
        branch_paras, branch_hs = [], []
        for i, branch in enumerate(branches):
            color = BRANCH_COLORS[i % 3]
            bw = min(regions[i][1] * 0.75, 230)
            p = self._para(branch, 9.2, color)
            branch_paras.append((p, bw))
            branch_hs.append(self._box_height(p, bw, min_h=38))
        branch_h = max(branch_hs)

        # Connettori tema centrale → rami
        centers = [rx + rw / 2 for rx, rw in regions]
        bar_y = root_bottom - 22
        self._line(W / 2, root_bottom, W / 2, bar_y)
        if len(centers) > 1:
            self._line(centers[0], bar_y, centers[-1], bar_y)
        for cx in centers:
            self._line(cx, bar_y, cx, branch_y)

        child_y = branch_y - branch_h - 46
        lowest = child_y
        for i, branch in enumerate(branches):
            color, soft = BRANCH_COLORS[i % 3], BRANCH_SOFT[i % 3]
            rx, rw = regions[i]
            cx = centers[i]
            p, bw = branch_paras[i]
            self._draw_box(cx - bw / 2, branch_y, bw, branch_h, p, colors.white, color, radius=6)
            children = branch["children"]
            if not children:
                continue
            k = len(children)
            cgap = 8
            cw = min((rw - cgap * (k - 1)) / k, 150)
            row_w = cw * k + cgap * (k - 1)
            start = rx + (rw - row_w) / 2
            child_centers = [start + j * (cw + cgap) + cw / 2 for j in range(k)]
            paras = [self._para(ch, child_size, color) for ch in children]
            ch_h = max(self._box_height(pp, cw, pad=6, min_h=64) for pp in paras)
            # Connettori ramo → punti
            b_bottom = branch_y - branch_h
            cbar = b_bottom - 23
            self._line(cx, b_bottom, cx, cbar)
            self._line(min(child_centers + [cx]), cbar, max(child_centers + [cx]), cbar)
            for j, child in enumerate(children):
                ccx = child_centers[j]
                self._line(ccx, cbar, ccx, child_y)
                self._draw_box(ccx - cw / 2, child_y, cw, ch_h, paras[j], colors.white, LINE, radius=5, pad=6)
                lowest = min(lowest, child_y - ch_h)
                detail = child["detail"]
                if detail:
                    dp = self._para(detail, detail_size, color)
                    dh = self._box_height(dp, cw, pad=6, min_h=52)
                    d_top = child_y - ch_h - 30
                    self._line(ccx, child_y - ch_h, ccx, d_top)
                    self._draw_box(ccx - cw / 2, d_top, cw, dh, dp, soft, LINE, dashed=True, radius=5, pad=6)
                    lowest = min(lowest, d_top - dh)

        # Sintesi finale
        conclusion = data["conclusion"]
        if conclusion:
            cw = min(300, W * 0.5)
            cp = self._para(conclusion, 9, CONCLUSION_INK)
            chh = self._box_height(cp, cw, pad=10, min_h=40)
            cy = max(lowest - 40, chh + 4)
            self._draw_box(W / 2 - cw / 2, cy, cw, chh, cp, CONCLUSION_BG, CONCLUSION_LINE, radius=7, pad=10)


def _branch_of(data: dict) -> dict[int, int]:
    """Numero del nodo → indice del ramo (per colorare le spiegazioni)."""
    owner = {}
    for i, branch in enumerate(data["branches"]):
        owner[branch["n"]] = i
        for child in branch["children"]:
            owner[child["n"]] = i
            if child["detail"]:
                owner[child["detail"]["n"]] = i
    return owner


def _ordered_nodes(data: dict) -> list[dict]:
    nodes = [data["root"]]
    for branch in data["branches"]:
        nodes.append(branch)
        nodes.extend(branch["children"])
        nodes.extend(ch["detail"] for ch in branch["children"] if ch["detail"])
    if data["conclusion"]:
        nodes.append(data["conclusion"])
    return sorted(nodes, key=lambda n: n["n"])


def render_map_pdf(raw: dict, footer: str) -> bytes:
    register_fonts()
    data = number_map(raw)
    buffer = io.BytesIO()
    content_w = PAGE_W - 2 * MARGIN
    content_h = PAGE_H - 2 * MARGIN - 6 * mm

    def background(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(BG)
        canvas.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
        canvas.setFont(FONT, 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, MARGIN - 4 * mm, footer[:120])
        canvas.drawRightString(PAGE_W - MARGIN, MARGIN - 4 * mm, str(doc.page))
        canvas.restoreState()

    doc = BaseDocTemplate(buffer, pagesize=(PAGE_W, PAGE_H), leftMargin=MARGIN, rightMargin=MARGIN,
                          topMargin=MARGIN, bottomMargin=MARGIN, title=f"Mappa concettuale: {data['title']}",
                          author="Lezionami", creator="Lezionami", lang="it-IT")
    map_frame = Frame(MARGIN, MARGIN + 6 * mm, content_w, content_h, leftPadding=0, rightPadding=0,
                      topPadding=0, bottomPadding=0, id="mappa")
    col_gap = 10 * mm
    col_w = (content_w - col_gap) / 2
    notes_frames = [
        Frame(MARGIN + i * (col_w + col_gap), MARGIN + 6 * mm, col_w, content_h, leftPadding=0, rightPadding=0,
              topPadding=0, bottomPadding=0, id=f"col{i}")
        for i in range(2)
    ]
    doc.addPageTemplates([
        PageTemplate(id="mappa", frames=[map_frame], onPage=background),
        PageTemplate(id="note", frames=notes_frames, onPage=background),
    ])

    owner = _branch_of(data)
    heading = ParagraphStyle("h", fontName=FONT_BOLD, fontSize=13, leading=17, textColor=INK, spaceAfter=8)
    body = ParagraphStyle("b", fontName=FONT, fontSize=8.6, leading=12, textColor=INK, spaceAfter=7)
    story: list = [MapDiagram(data, content_w, content_h - 2), NextPageTemplate("note"), PageBreak(),
                   Paragraph("Cosa dire per ogni punto della mappa "
                             f"<font name='{FONT}' size='8.5' color='#6E6A75'>— i numeri corrispondono ai nodi della pagina precedente</font>",
                             heading),
                   Spacer(1, 2)]
    for node in _ordered_nodes(data):
        index = owner.get(node["n"])
        color = BRANCH_COLORS[index % 3] if index is not None else (CONCLUSION_INK if node is data["conclusion"] else INK)
        label = f"<font name='{FONT_BOLD}' color='#{color.hexval()[2:]}'>{node['n']}. {escape(node['label'])}.</font>"
        story.append(Paragraph(f"{label} {escape(node['text'])}", body))
    doc.build(story)
    return buffer.getvalue()

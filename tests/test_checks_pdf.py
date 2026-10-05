import io
import re

from pypdf import PdfReader

from app import checks
from app.pdf_render import file_name, render_pdf


def _block(type_, text="", title="", items=None, columns=None, rows=None, refs=None):
    return {"type": type_, "title": title, "text": text, "items": items or [], "columns": columns or [],
            "rows": rows or [], "refs": refs or []}


def _doc(blocks, title="Documento di prova impaginazione"):
    return {"title": title, "subtitle": "Sottotitolo", "footer_label": "Prova", "limits_notice": "",
            "sections": [{"heading": "Sezione", "blocks": blocks}]}


def test_arithmetic_check_finds_wrong_example():
    doc = _doc([_block("example", items=["100 € × 0,75% = 0,75 €", "12 × 100 € = 1.300 €"])])
    issues = checks.check_arithmetic(doc, "guida")
    assert len(issues) == 1 and "1.300" in issues[0]["problema"]


def test_arithmetic_check_accepts_weighted_average_and_points():
    doc = _doc([_block("example", items=["60% × 1,35 + 40% × 1,30 = 1,33", "3,27% − 1,33 = 1,94%",
                                         "99,25 × 20% = 19,85", "84 × 100 = 8.400 euro"])])
    assert checks.check_arithmetic(doc, "lezione") == []


def test_brochure_internal_terms_and_promises():
    doc = _doc([_block("paragraph", "La provvigione della rete è del 2% e il rendimento garantito è sicuro."),
                _block("internal", "Obiettivo trimestrale")])
    problems = " ".join(i["problema"] for i in checks.check_brochure_terms(doc))
    assert "provvigione" in problems and "rendimento garantito" in problems and "blocco di informazioni interne" in problems


def test_unsupported_numbers_flagged():
    doc = _doc([_block("paragraph", "Il costo è 1,5% e la penale 7,25%."), _block("example", "Ipotesi: 333 euro")])
    issues = checks.check_unsupported_numbers(doc, "brochure", checks.numbers_in("costo 1,5%"))
    assert issues and "7,25" in issues[0]["problema"] and "333" not in issues[0]["problema"]


def test_limits_required_when_interrupted():
    docs = {"lezione": _doc([]), "guida": {**_doc([]), "limits_notice": "Trascrizione incompleta."}}
    issues = checks.check_limits(["Possibile interruzione: messaggio del servizio"], docs)
    assert [i["documento"] for i in issues] == ["lezione"]


def test_coverage_detects_missing_final_topic():
    inventory = {"argomenti": [
        {"id": "A1", "titolo": "Adesione e requisiti", "riferimenti": ["T001"]},
        {"id": "A2", "titolo": "Trasferimento della posizione", "riferimenti": ["T010"]},
    ], "elementi": [{"riferimenti": ["T010"]}]}
    lesson = _doc([_block("paragraph", "Adesione e requisiti di accesso.", refs=["T001"])])
    issues, stats = checks.check_coverage(inventory, lesson, [f"T{i:03d}" for i in range(1, 11)])
    problems = " ".join(i["problema"] for i in issues)
    assert "Trasferimento" in problems and stats["argomenti_non_coperti"] == 1


def test_normalize_pads_tables():
    doc = checks.normalize_document(_doc([_block("table", columns=["A", "B", "C"], rows=[["1"], ["2", "3", "4", "5"], ["", ""]])]))
    table = doc["sections"][0]["blocks"][0]
    assert table["columns"] == ["A", "B", "C", ""] and all(len(r) == 4 for r in table["rows"]) and len(table["rows"]) == 2


def test_file_names():
    assert file_name("lezione", "Destinazione Domani") == "Lezione_completa_Destinazione_Domani.pdf"
    assert file_name("guida", "Più è meglio: 2026/27") == "Guida_studio_Piu_e_meglio_2026_27.pdf"
    assert file_name("brochure", "") == "Brochure_cliente_Documento.pdf"


def _render(doc, kind="lezione"):
    data = render_pdf(checks.normalize_document(doc), kind, show_refs=True)
    reader = PdfReader(io.BytesIO(data))
    return data, reader


def test_pdf_is_a4_selectable_with_page_numbers():
    long_rows = [[f"Riga {n}", "Testo di cella lungo che deve andare a capo correttamente senza sovrapporsi. " * 2, f"{n},5%"] for n in range(1, 60)]
    doc = _doc([_block("paragraph", "Paragrafo con accenti è à ù e simboli € ≠ → ×. " * 30, refs=["T001"]),
                _block("table", columns=["Voce", "Descrizione", "Valore"], rows=long_rows),
                _block("example", "Ipotesi", "Esempio ipotetico", ["12 × 100 € = 1.200 €"])])
    data, reader = _render(doc)
    assert data.startswith(b"%PDF")
    pages = len(reader.pages)
    assert pages >= 3
    for number, page in enumerate(reader.pages, start=1):
        box = page.mediabox
        assert round(float(box.width)) == 595 and round(float(box.height)) == 842
        text = page.extract_text()
        assert f"{number} / {pages}" in text
    full = "".join(p.extract_text() for p in reader.pages)
    assert "Paragrafo con accenti è à ù" in full and "€" in full
    # Intestazione della tabella ripetuta sulle pagine successive.
    assert sum(1 for p in reader.pages if "Voce" in p.extract_text()) >= 2
    # Ogni riga compare una sola volta (nessuna riga spezzata o duplicata).
    for n in (1, 30, 59):
        assert len(re.findall(rf"Riga {n}\b", full)) == 1


def test_pdf_handles_oversized_table_cell():
    huge = "Cella lunghissima. " * 400
    doc = _doc([_block("table", columns=["A", "B"], rows=[["x", huge], ["y", "breve"]])])
    data, reader = _render(doc)
    assert len(reader.pages) >= 2


def test_brochure_never_renders_internal_blocks():
    doc = _doc([_block("paragraph", "Testo per il cliente."), _block("internal", "SEGRETO INTERNO")])
    _, reader = _render(doc, "brochure")
    assert "SEGRETO INTERNO" not in "".join(p.extract_text() for p in reader.pages)


def test_short_document_not_one_page_per_paragraph():
    doc = {"title": "Breve", "subtitle": "", "footer_label": "", "limits_notice": "",
           "sections": [{"heading": f"Sezione {i}", "blocks": [_block("paragraph", "Testo breve.")]} for i in range(6)]}
    _, reader = _render(doc)
    assert len(reader.pages) == 1

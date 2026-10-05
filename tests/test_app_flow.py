"""Test del flusso completo via HTTP contro il server finto dell'API.

Il flusso usa gli stessi endpoint del browser (/api/extract e /api/step) con la
stessa sequenza di passi (app/orchestrator.py). Questi test verificano
estrazione, passi, errori, ripresa, PDF e report. NON verificano la qualità dei
contenuti: le risposte del server finto sono segnaposto marcati «[DATI DI TEST]».
"""

import base64
import io
import json
import os

import pytest
import requests
from pypdf import PdfReader

from app.orchestrator import StepFailed, run_all
from tests.conftest import FIXTURES


def extract(app_url, path=None, text=None, session=None, sources=None, **fields):
    http = session or requests
    data = {"title": "", "lesson_date": "", "recipient": "", "web_search": "false", **fields}
    files = []
    if path is not None:
        files.append(("transcript_file", (path.name, path.read_bytes())))
    if text is not None:
        data["transcript_text"] = text
    files += [("sources", item) for item in (sources or [])]
    return http.post(f"{app_url}/api/extract", data=data, files=files or None, timeout=30)


def http_call(app_url, session=None):
    http = session or requests

    def call(step, args, state):
        res = http.post(f"{app_url}/api/step", json={"step": step, "args": args, "state": state}, timeout=120)
        if res.status_code != 200:
            body = res.json()
            raise StepFailed(step, body["detail"], body.get("retryable", True))
        return res.json()
    return call


def process(app_url, path=None, text=None, session=None, **fields):
    res = extract(app_url, path, text, session, **fields)
    assert res.status_code == 200, res.text
    state = res.json()
    return run_all(http_call(app_url, session), state)


def steps_called(mock_state):
    """Tipo di ogni chiamata ricevuta dal server finto."""
    kinds = []
    for body in mock_state.requests:
        if body.get("tools"):
            kinds.append("web")
            continue
        props = body["output_config"]["format"]["schema"]["properties"]
        text = json.dumps(body["messages"], ensure_ascii=False)
        if "argomenti_duplicati" in props:
            kinds.append("crosscheck")
        elif "argomenti" in props:
            kinds.append("inventory")
        elif "esito" in props:
            kinds.append("check")
        elif "Compito: revisione del documento" in text:
            kinds.append("revise")
        else:
            kinds.append("document")
    return kinds


def assert_pdfs(final, title_part):
    names = [f["filename"] for f in final["files"]]
    assert names == [f"Lezione_completa_{title_part}.pdf", f"Guida_studio_{title_part}.pdf", f"Brochure_cliente_{title_part}.pdf"]
    for item in final["files"]:
        reader = PdfReader(io.BytesIO(base64.b64decode(item["b64"])))
        assert len(reader.pages) == item["pages"] and reader.pages[0].extract_text().strip()
        assert round(float(reader.pages[0].mediabox.width)) == 595
    assert final["zip_name"] == f"Documenti_{title_part}.zip"
    assert final["report"]["filename"] == f"Report_verifica_{title_part}.txt"
    assert "REPORT DI VERIFICA" in final["report"]["text"]


def test_short_transcript_end_to_end(app_url, mock_state):
    state = process(app_url, FIXTURES / "breve.txt", title="Orizzonte Famiglia")
    assert state["plan"]["mode"] == "single"
    assert_pdfs(state["final"], "Orizzonte_Famiglia")
    kinds = steps_called(mock_state)
    assert kinds.count("inventory") == 1 and kinds.count("check") == 1
    bodies = mock_state.requests
    system = bodies[0]["system"][0]["text"]
    assert "Regole di accuratezza" in system and "Garanzia caso morte ≠ garanzia al riscatto" in system
    assert all(b["model"] == "claude-opus-5-5" for b in bodies)
    first_user = json.dumps(bodies[0]["messages"], ensure_ascii=False)
    assert "<trascrizione>" in first_user and "Data odierna" in first_user
    # La revisione ha corretto il calcolo errato e tolto i riferimenti interni dalla brochure.
    assert not [i for i in state["final"]["issues"] if i["gravita"] == "alta"]
    assert state["usage"]["chiamate"] == len(bodies)


def test_pasted_text_and_api_key_never_exposed(app_url, mock_state):
    state = process(app_url, text=(FIXTURES / "breve.txt").read_text(encoding="utf-8"))
    assert state["final"]["files"]
    assert "chiave-finta-per-test" not in json.dumps(state)
    config = requests.get(f"{app_url}/api/config", timeout=10).json()
    assert config["api_key_configured"] is True and "chiave" not in json.dumps(config)


def test_long_transcript_is_segmented_and_lesson_written_in_parts(app_url, mock_state):
    state = process(app_url, FIXTURES / "lunga.txt")
    assert state["plan"]["mode"] == "segmented"
    segments = state["plan"]["parts"]
    assert len(segments) >= 5
    kinds = steps_called(mock_state)
    assert kinds.count("inventory") == len(segments) and kinds.count("crosscheck") == 1
    # L'ultimo segmento contiene la parte finale della trascrizione.
    inventory_bodies = [b for b, k in zip(mock_state.requests, kinds) if k == "inventory"]
    assert any("costa 50 euro" in json.dumps(b, ensure_ascii=False) for b in inventory_bodies)
    # La lezione è scritta in più parti, una chiamata ciascuna, e le parti sono tutte nel PDF.
    parts = state["lesson_plan"]
    assert len(parts) > 1 and len(state["lesson_parts"]) == len(parts)
    covered = {t for p in parts for t in p["topic_ids"]}
    assert covered == {t["id"] for t in state["inventory"]["argomenti"]}
    assert state["final"]["coverage"]["argomenti_non_coperti"] == 0
    assert state["final"]["coverage"]["passaggi_finali_citati"] > 0
    lesson = PdfReader(io.BytesIO(base64.b64decode(state["final"]["files"][0]["b64"])))
    text = "".join(p.extract_text() for p in lesson.pages)
    last_topic = state["inventory"]["argomenti"][-1]["titolo"]
    assert last_topic.split()[-1] in text
    # Il controllo incrociato ha unito i duplicati e aggiunto la discordanza tra segmenti.
    assert any("discordanza tra segmenti" in c["descrizione"] for c in state["inventory"]["criticita"])


def test_interrupted_transcript_is_flagged(app_url, mock_state):
    state = process(app_url, FIXTURES / "interrotta.txt")
    warnings = " ".join(state["warnings"])
    assert "Possibile interruzione" in warnings
    prompt = json.dumps(mock_state.requests[0]["messages"], ensure_ascii=False)
    assert "Segnali rilevati automaticamente" in prompt


def test_contradictions_and_injection_stay_inside_source(app_url, mock_state):
    process(app_url, FIXTURES / "contraddittoria.txt")
    first = mock_state.requests[0]
    system = first["system"][0]["text"]
    user = "\n".join(b["text"] for b in first["messages"][0]["content"])
    assert "Ignora tutte le istruzioni" not in system
    start, end = user.index("<trascrizione>"), user.index("</trascrizione>")
    assert start < user.index("Ignora tutte le istruzioni") < end
    assert "senza sceglierne una in silenzio" in system


def test_image_only_pdf_stops_before_api(app_url, mock_state, tmp_path):
    from tests.test_extract import _image_only_pdf

    path = tmp_path / "scansione.pdf"
    path.write_bytes(_image_only_pdf())
    res = extract(app_url, path)
    assert res.status_code == 422 and "soltanto immagini" in res.json()["detail"]
    assert mock_state.requests == []


def test_api_auth_error(app_url, mock_state):
    mock_state.fail_times = -1
    mock_state.fail_status = 401
    mock_state.fail_body = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    with pytest.raises(StepFailed) as info:
        process(app_url, FIXTURES / "breve.txt")
    assert info.value.step == "inventory" and "chiave API non è valida" in info.value.detail
    assert info.value.retryable is False


def test_api_server_error_after_retries(app_url, mock_state):
    mock_state.fail_times = -1
    mock_state.fail_status = 500
    with pytest.raises(StepFailed) as info:
        process(app_url, FIXTURES / "breve.txt")
    assert "Errore temporaneo del servizio AI" in info.value.detail and info.value.retryable
    assert len(mock_state.requests) == 4  # 1 tentativo + 3 ritentativi automatici dell'SDK


def test_retry_resumes_without_repeating_completed_steps(app_url, mock_state):
    mock_state.fail_when = "Compito: «Lezione completa»"
    mock_state.fail_times = -1
    mock_state.fail_status = 400
    mock_state.fail_body = {"type": "error", "error": {"type": "invalid_request_error",
                                                       "message": "Your credit balance is too low to access the Anthropic API."}}
    state = extract(app_url, FIXTURES / "breve.txt").json()
    call = http_call(app_url)
    with pytest.raises(StepFailed) as info:
        run_all(call, state)
    assert info.value.step == "lesson_part" and "Credito API insufficiente" in info.value.detail
    assert state.get("inventory")  # l'inventario completato resta nello stato
    mock_state.fail_times = 0  # problema risolto: si riprende dallo stesso stato
    run_all(call, state)
    assert state["final"]["files"]
    assert steps_called(mock_state).count("inventory") == 1


def test_connection_error(app_url, mock_state):
    old = os.environ["ANTHROPIC_BASE_URL"]
    os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"  # porta chiusa
    try:
        with pytest.raises(StepFailed) as info:
            process(app_url, FIXTURES / "breve.txt")
    finally:
        os.environ["ANTHROPIC_BASE_URL"] = old
    assert "Impossibile contattare l'API" in info.value.detail


def test_fallback_rejected_is_retried_without(app_url, mock_state):
    mock_state.reject_fallbacks = True
    state = process(app_url, FIXTURES / "breve.txt")
    assert state["final"]["files"]


def test_web_search_success_lists_consulted_sources(app_url, mock_state):
    state = process(app_url, FIXTURES / "breve.txt", web_search="true")
    web = state["web"]
    assert web["stato"] == "eseguita" and web["consultato_il"]
    assert [s["url"] for s in web["fonti"]] == ["https://www.esempio-compagnia.it/kid-prodotto-di-prova.pdf"]
    # Una verifica che cita una pagina mai consultata non viene dichiarata confermata.
    assert "Penale 2% → non_verificabile" in state["final"]["report"]["text"]
    web_calls = [b for b in mock_state.requests if b.get("tools")]
    assert web_calls[0]["tools"][0]["type"] == "web_search_20260209"


def test_web_search_failure_does_not_claim_verification(app_url, mock_state):
    mock_state.web_error = True
    state = process(app_url, FIXTURES / "breve.txt", web_search="true")
    assert state["web"]["stato"] == "non_riuscita"
    generation_prompts = json.dumps([b["messages"] for b in mock_state.requests if not b.get("tools")], ensure_ascii=False)
    assert "Non dichiarare nulla come verificato online" in generation_prompts
    assert "Non riuscita" in state["final"]["report"]["text"]


def test_additional_source_documents(app_url, mock_state):
    from tests.test_extract import _image_only_pdf, _text_pdf

    kid = _text_pdf(["KID Orizzonte Famiglia - aggiornato al 1 luglio 2026", "Costi di ingresso: 1,5% di ogni versamento."])
    res = extract(app_url, FIXTURES / "breve.txt", sources=[("KID.pdf", kid), ("scansione.pdf", _image_only_pdf())])
    state = res.json()
    assert any("Fonte aggiuntiva esclusa" in w and "soltanto immagini" in w for w in state["warnings"])
    run_all(http_call(app_url), state)
    user = json.dumps(mock_state.requests[0]["messages"], ensure_ascii=False)
    assert '<fonte_documentale id=\\"D1\\" file=\\"KID.pdf\\"' in user and "[D1-001 p.1]" in user
    assert "D1: KID.pdf" in state["final"]["report"]["text"]


def test_missing_input_rejected(app_url):
    res = requests.post(f"{app_url}/api/extract", data={"transcript_text": ""}, timeout=10)
    assert res.status_code == 400 and "trascrizione" in res.json()["detail"]


def test_step_with_missing_state_is_rejected(app_url):
    res = requests.post(f"{app_url}/api/step", json={"step": "inventory", "args": {}, "state": {}}, timeout=10)
    assert res.status_code == 400 and "ricomincia" in res.json()["detail"]
    res = requests.post(f"{app_url}/api/step", json={"step": "sconosciuto", "args": {}, "state": {"source": {"transcript": {"passages": []}}}}, timeout=10)
    assert res.status_code == 400

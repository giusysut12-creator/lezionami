"""Test del flusso completo via HTTP contro il server finto dell'API.

Questi test verificano estrazione, fasi, gestione degli errori, ripresa,
PDF e ZIP. NON verificano la qualità dei contenuti generati dall'AI: le
risposte del server finto sono segnaposto marcati «[DATI DI TEST]».
"""

import io
import json
import os
import time
import zipfile

import requests
from pypdf import PdfReader

from tests.conftest import FIXTURES


def _submit(app_url, path=None, text=None, **fields):
    files = {}
    data = {"title": "", "lesson_date": "", "recipient": "", "web_search": "false", **fields}
    if path is not None:
        files["transcript_file"] = (path.name, path.read_bytes())
    if text is not None:
        data["transcript_text"] = text
    res = requests.post(f"{app_url}/api/jobs", data=data, files=files or None, timeout=10)
    assert res.status_code == 200, res.text
    return res.json()["job_id"]


def _wait(app_url, job_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = requests.get(f"{app_url}/api/jobs/{job_id}", timeout=10).json()
        if state["status"] in ("completato", "errore"):
            return state
        time.sleep(0.3)
    raise AssertionError("timeout")


def _phase(state, key):
    return next(p for p in state["phases"] if p["key"] == key)


def _assert_downloads(app_url, state, title_part):
    names = []
    for item in state["files"]:
        res = requests.get(f"{app_url}/api/jobs/{state['id']}/files/{item['kind']}", timeout=10)
        assert res.status_code == 200 and res.headers["content-type"] == "application/pdf"
        assert item["filename"] in res.headers["content-disposition"]
        reader = PdfReader(io.BytesIO(res.content))
        assert len(reader.pages) == item["pages"] and reader.pages[0].extract_text().strip()
        names.append(item["filename"])
    assert names == [f"Lezione_completa_{title_part}.pdf", f"Guida_studio_{title_part}.pdf", f"Brochure_cliente_{title_part}.pdf"]
    res = requests.get(f"{app_url}/api/jobs/{state['id']}/files/zip", timeout=10)
    assert res.status_code == 200 and res.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(res.content))
    assert set(names) <= set(archive.namelist())
    assert any(n.startswith("Report_verifica_") for n in archive.namelist())


def test_short_transcript_end_to_end(app_url, mock_state):
    job_id = _submit(app_url, FIXTURES / "breve.txt", title="Orizzonte Famiglia")
    state = _wait(app_url, job_id)
    assert state["status"] == "completato", state["error"]
    assert _phase(state, "verifica_web")["status"] == "saltata"
    assert state["progress"] == 100
    _assert_downloads(app_url, state, "Orizzonte_Famiglia")
    # Inventario unico (testo breve), 3 documenti, controllo, revisioni.
    bodies = mock_state.requests
    system = bodies[0]["system"][0]["text"]
    assert "Regole di accuratezza" in system and "Garanzia caso morte ≠ garanzia al riscatto" in system
    assert all(b["model"] == "claude-opus-5-5" for b in bodies)
    assert all(b.get("output_config", {}).get("format", {}).get("type") == "json_schema" for b in bodies)
    first_user = json.dumps(bodies[0]["messages"])
    assert "<trascrizione>" in first_user and "Data odierna" in first_user
    # La revisione ha corretto il calcolo errato e tolto i riferimenti interni dalla brochure.
    assert not [i for i in state["issues"] if i["gravita"] == "alta"]


def test_pasted_text_and_api_key_never_exposed(app_url, mock_state):
    job_id = _submit(app_url, text=(FIXTURES / "breve.txt").read_text(encoding="utf-8"))
    state = _wait(app_url, job_id)
    assert state["status"] == "completato"
    assert "chiave-finta-per-test" not in json.dumps(state)
    config = requests.get(f"{app_url}/api/config", timeout=10).json()
    assert config["api_key_configured"] is True and "chiave" not in json.dumps(config)


def test_long_transcript_is_segmented_and_consolidated(app_url, mock_state):
    job_id = _submit(app_url, FIXTURES / "lunga.txt")
    state = _wait(app_url, job_id, timeout=120)
    assert state["status"] == "completato", state["error"]
    inventory_calls = [b for b in mock_state.requests
                       if "argomenti" in b["output_config"]["format"]["schema"]["properties"]]
    segment_calls = [b for b in inventory_calls if "inventari_parziali" not in json.dumps(b["messages"], ensure_ascii=False)]
    assert len(segment_calls) >= 4
    assert len(inventory_calls) == len(segment_calls) + 1  # + consolidamento
    # L'ultimo segmento contiene la parte finale della trascrizione.
    last_segment = json.dumps(segment_calls, ensure_ascii=False)
    assert "costa 50 euro" in last_segment
    assert "Dati: 0" not in _phase(state, "inventario")["detail"]
    assert "segmenti" in _phase(state, "inventario")["detail"]
    assert state["coverage"]["passaggi_finali_citati"] > 0


def test_interrupted_transcript_is_flagged(app_url, mock_state):
    job_id = _submit(app_url, FIXTURES / "interrotta.txt")
    state = _wait(app_url, job_id)
    assert state["status"] == "completato"
    warnings = " ".join(state["warnings"])
    assert "Possibile interruzione" in warnings
    prompt = json.dumps(mock_state.requests[0]["messages"], ensure_ascii=False)
    assert "Segnali rilevati automaticamente" in prompt


def test_contradictions_and_injection_stay_inside_source(app_url, mock_state):
    job_id = _submit(app_url, FIXTURES / "contraddittoria.txt")
    state = _wait(app_url, job_id)
    assert state["status"] == "completato"
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
    state = _wait(app_url, _submit(app_url, path))
    assert state["status"] == "errore"
    assert "soltanto immagini" in state["error"]["message"] and state["error"]["retryable"] is False
    assert mock_state.requests == []


def test_api_auth_error(app_url, mock_state):
    mock_state.fail_times = -1
    mock_state.fail_status = 401
    mock_state.fail_body = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    state = _wait(app_url, _submit(app_url, FIXTURES / "breve.txt"))
    assert state["status"] == "errore"
    assert state["error"]["phase"] == "inventario"
    assert "chiave API non è valida" in state["error"]["message"]
    assert not state["files"]


def test_api_server_error_after_retries(app_url, mock_state):
    mock_state.fail_times = -1
    mock_state.fail_status = 500
    state = _wait(app_url, _submit(app_url, FIXTURES / "breve.txt"), timeout=120)
    assert state["status"] == "errore"
    assert "Errore temporaneo del servizio AI" in state["error"]["message"] and state["error"]["retryable"]
    assert len(mock_state.requests) == 4  # 1 tentativo + 3 ritentativi automatici dell'SDK


def test_retry_resumes_without_repeating_completed_phases(app_url, mock_state):
    mock_state.fail_when = "Compito: «Lezione completa»"
    mock_state.fail_times = -1
    mock_state.fail_status = 400
    mock_state.fail_body = {"type": "error", "error": {"type": "invalid_request_error",
                                                       "message": "Your credit balance is too low to access the Anthropic API."}}
    job_id = _submit(app_url, FIXTURES / "breve.txt")
    state = _wait(app_url, job_id)
    assert state["status"] == "errore" and state["error"]["phase"] == "generazione"
    assert "Credito API insufficiente" in state["error"]["message"]
    assert _phase(state, "inventario")["status"] == "completata"
    calls_before = len(mock_state.requests)
    inventory_calls_before = sum(1 for b in mock_state.requests if "argomenti" in b["output_config"]["format"]["schema"]["properties"])

    mock_state.fail_times = 0  # il problema è risolto
    assert requests.post(f"{app_url}/api/jobs/{job_id}/retry", timeout=10).status_code == 200
    state = _wait(app_url, job_id)
    assert state["status"] == "completato"
    inventory_calls_after = sum(1 for b in mock_state.requests if "argomenti" in b["output_config"]["format"]["schema"]["properties"])
    assert inventory_calls_after == inventory_calls_before == 1
    assert len(mock_state.requests) > calls_before


def test_connection_error(app_url, mock_state):
    old = os.environ["ANTHROPIC_BASE_URL"]
    os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"  # porta chiusa
    try:
        state = _wait(app_url, _submit(app_url, FIXTURES / "breve.txt"), timeout=120)
    finally:
        os.environ["ANTHROPIC_BASE_URL"] = old
    assert state["status"] == "errore"
    assert "Impossibile contattare l'API" in state["error"]["message"]


def test_fallback_rejected_is_retried_without(app_url, mock_state):
    mock_state.reject_fallbacks = True
    state = _wait(app_url, _submit(app_url, FIXTURES / "breve.txt"))
    assert state["status"] == "completato"


def test_web_search_success_lists_consulted_sources(app_url, mock_state):
    state = _wait(app_url, _submit(app_url, FIXTURES / "breve.txt", web_search="true"))
    assert state["status"] == "completato"
    web = state["web"]
    assert web["stato"] == "eseguita" and web["consultato_il"]
    assert [s["url"] for s in web["fonti"]] == ["https://www.esempio-compagnia.it/kid-prodotto-di-prova.pdf"]
    report = requests.get(f"{app_url}/api/jobs/{state['id']}/files/report", timeout=10).text
    # Una verifica che cita una pagina mai consultata non viene dichiarata confermata.
    assert "Penale 2% → non_verificabile" in report
    web_calls = [b for b in mock_state.requests if b.get("tools")]
    assert web_calls[0]["tools"][0]["type"] == "web_search_20260209"


def test_web_search_failure_does_not_claim_verification(app_url, mock_state):
    mock_state.web_error = True
    state = _wait(app_url, _submit(app_url, FIXTURES / "breve.txt", web_search="true"))
    assert state["status"] == "completato"
    assert _phase(state, "verifica_web")["status"] == "non_riuscita"
    assert state["web"]["stato"] == "non_riuscita"
    generation_prompts = json.dumps([b["messages"] for b in mock_state.requests if not b.get("tools")], ensure_ascii=False)
    assert "Non dichiarare nulla come verificato online" in generation_prompts
    assert "nessuna condizione è dichiarata verificata" in " ".join(state["warnings"])


def test_additional_source_documents(app_url, mock_state, tmp_path):
    from tests.test_extract import _image_only_pdf, _text_pdf

    kid = _text_pdf(["KID Orizzonte Famiglia - aggiornato al 1 luglio 2026", "Costi di ingresso: 1,5% di ogni versamento."])
    files = [
        ("transcript_file", ("breve.txt", (FIXTURES / "breve.txt").read_bytes())),
        ("sources", ("KID.pdf", kid)),
        ("sources", ("scansione.pdf", _image_only_pdf())),
    ]
    res = requests.post(f"{app_url}/api/jobs", data={"web_search": "false"}, files=files, timeout=10)
    state = _wait(app_url, res.json()["job_id"])
    assert state["status"] == "completato"
    assert any("Fonte aggiuntiva esclusa" in w and "soltanto immagini" in w for w in state["warnings"])
    user = json.dumps(mock_state.requests[0]["messages"], ensure_ascii=False)
    assert '<fonte_documentale id=\\"D1\\" file=\\"KID.pdf\\"' in user and "[D1-001 p.1]" in user


def test_delete_job(app_url, mock_state):
    job_id = _submit(app_url, FIXTURES / "breve.txt")
    _wait(app_url, job_id)
    assert requests.delete(f"{app_url}/api/jobs/{job_id}", timeout=10).status_code == 200
    assert requests.get(f"{app_url}/api/jobs/{job_id}", timeout=10).status_code == 404


def test_missing_input_rejected(app_url):
    res = requests.post(f"{app_url}/api/jobs", data={"transcript_text": ""}, timeout=10)
    assert res.status_code == 400 and "trascrizione" in res.json()["detail"]

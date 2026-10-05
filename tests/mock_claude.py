"""Server finto che imita l'endpoint /v1/messages dell'API di Claude (streaming SSE).

SERVE SOLO PER I TEST AUTOMATICI: verifica il flusso dell'applicazione, la
gestione degli errori e la creazione dei PDF senza una chiave API reale.
I contenuti restituiti sono segnaposto marcati «[DATI DI TEST]» e non hanno
alcun valore come risultato dell'elaborazione.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TEST_MARK = "[DATI DI TEST]"


class MockState:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.fail_when: str | None = None      # sottostringa del prompt che provoca l'errore
        self.fail_status = 500
        self.fail_body: dict = {"type": "error", "error": {"type": "api_error", "message": "Internal server error"}}
        self.fail_times = 0                     # quante volte fallire (-1 = sempre)
        self.web_error = False
        self.reject_fallbacks = False


STATE = MockState()


def _all_text(body: dict) -> str:
    texts = []
    system = body.get("system")
    if isinstance(system, list):
        texts += [b.get("text", "") for b in system]
    elif isinstance(system, str):
        texts.append(system)
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts += [b.get("text", "") for b in content if isinstance(b, dict)]
    return "\n".join(texts)


def _user_text(body: dict) -> str:
    texts = []
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts += [b.get("text", "") for b in content if isinstance(b, dict)]
    return "\n".join(texts)


def _tag_json(text: str, tag: str):
    match = re.search(rf"<{tag}>\n(.*?)\n</{tag}>", text, re.DOTALL)
    return json.loads(match.group(1)) if match else None


def _block(type_, text="", title="", items=None, columns=None, rows=None, refs=None):
    return {"type": type_, "title": title, "text": text, "items": items or [], "columns": columns or [],
            "rows": rows or [], "refs": refs or []}


def inventory_response(text: str) -> dict:
    partials = _tag_json(text, "inventari_parziali")
    if partials is not None:  # consolidamento: unisce tutto
        merged = {"argomenti": [], "elementi": [], "criticita": [], "domande_pubblico": []}
        for part in partials:
            for key in merged:
                merged[key] += part["inventario"].get(key, [])
        for index, topic in enumerate(merged["argomenti"], start=1):
            topic["id"] = f"A{index}"
        base = partials[0]["inventario"]
        return {**base, **merged}
    ids = re.findall(r"\[(T\d{3}|D\d+-\d{3})(?: p\.\d+)?\]", text)
    transcript_ids = [i for i in ids if i.startswith("T")] or ids
    topics, elements = [], []
    step = max(1, len(transcript_ids) // 4)
    for index, start in enumerate(range(0, len(transcript_ids), step), start=1):
        refs = transcript_ids[start:start + step]
        topics.append({"id": f"A{index}", "titolo": f"{TEST_MARK} Argomento {refs[0]}", "sintesi": "Segnaposto di test.",
                       "sottopunti": [], "riferimenti": refs})
        elements.append({"id": f"E{index}", "argomento_id": f"A{index}", "categoria": "numero",
                         "natura": "fatto_trascrizione", "testo": f"{TEST_MARK} elemento", "valore": "100",
                         "base_di_calcolo": "", "riferimento_temporale": "", "riferimenti": refs[:1]})
    return {
        "titolo_proposto": "Prodotto di prova", "tema_generale": TEST_MARK, "data_lezione_rilevata": "",
        "relatori": [], "prodotti": [{"nome": "Prodotto di prova", "descrizione": TEST_MARK, "riferimenti": transcript_ids[:1]}],
        "argomenti": topics, "elementi": elements, "termini": [], "domande_pubblico": [],
        "criticita": [{"tipo": "interruzione", "descrizione": TEST_MARK, "riferimenti": transcript_ids[-1:]}],
        "info_commerciali_interne": [], "fonti_documentali": [],
    }


def crosscheck_response(text: str) -> dict:
    merged = _tag_json(text, "inventario_unito") or {}
    ids = [t["id"] for t in merged.get("argomenti", [])]
    return {"titolo_proposto": "Prodotto di prova", "tema_generale": TEST_MARK, "data_lezione_rilevata": "",
            "criticita": [{"tipo": "contraddizione", "descrizione": f"{TEST_MARK} discordanza tra segmenti",
                           "riferimenti": []}],
            "argomenti_duplicati": [ids[:2]] if len(ids) > 3 else []}


def document_response(text: str) -> dict:
    inventory = _tag_json(text, "inventario") or {}
    assigned = re.search(r"SOLO questi argomenti dell'inventario, in modo completo: (.*)", text)
    if assigned:  # lezione scritta a parti: solo gli argomenti assegnati
        wanted = set(re.findall(r"\b(A\d+)\b", assigned.group(1).split("; inoltre")[0]))
        inventory = {**inventory, "argomenti": [t for t in inventory.get("argomenti", []) if t["id"] in wanted]}
    revision = "Compito: revisione del documento" in text
    if "«Lezione completa»" in text:
        kind = "lezione"
    elif "«Guida di studio»" in text:
        kind = "guida"
    else:
        kind = "brochure"
    sections = []
    for topic in inventory.get("argomenti", []):
        sections.append({"heading": topic["titolo"], "blocks": [
            _block("paragraph", f"{TEST_MARK} Testo segnaposto per verificare l'impaginazione del documento «{kind}». " * 4,
                   refs=topic["riferimenti"]),
            _block("table", columns=["Voce", "Valore", "Nota"],
                   rows=[[f"Voce {n}", f"{n * 10}%", f"{TEST_MARK} cella"] for n in range(1, 6)]),
        ]})
    calc = "12 × 100 = 1.200" if revision else "12 × 100 = 1.300"
    sections.append({"heading": "Esempio", "blocks": [_block("example", "Cifre ipotetiche.", "Esempio ipotetico", [calc])]})
    if kind == "brochure" and not revision:
        sections.append({"heading": "Rete", "blocks": [_block("paragraph", "La provvigione della rete è del 2%.")]})
    return {"title": f"{TEST_MARK} {kind}", "subtitle": "Documento segnaposto generato dal server finto",
            "footer_label": "Test", "limits_notice": f"{TEST_MARK} fonte di prova.", "sections": sections}


def check_response() -> dict:
    return {"esito": "ok", "problemi": [], "argomenti_non_coperti": [], "note": TEST_MARK}


def web_blocks() -> list[dict]:
    if STATE.web_error:
        return [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "test"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
             "content": {"type": "web_search_tool_result_error", "error_code": "unavailable"}},
            {"type": "text", "text": "Non è stato possibile completare la ricerca."},
        ]
    url = "https://www.esempio-compagnia.it/kid-prodotto-di-prova.pdf"
    verdict = {"verifiche": [{"affermazione": "Caricamento 1,5%", "esito": "confermata", "dettaglio": TEST_MARK,
                              "fonte_titolo": "KID di prova", "fonte_url": url, "data_documento": "01/01/2026"},
                             {"affermazione": "Penale 2%", "esito": "confermata", "dettaglio": TEST_MARK,
                              "fonte_titolo": "Pagina non consultata", "fonte_url": "https://inventato.example/x",
                              "data_documento": ""}],
               "note": TEST_MARK}
    return [
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "test"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
            {"type": "web_search_result", "url": url, "title": "KID di prova", "encrypted_content": "x", "page_age": "1 gennaio 2026"}]},
        {"type": "text", "text": "```json\n" + json.dumps(verdict, ensure_ascii=False) + "\n```"},
    ]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, status: int, payload: dict):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self.path.startswith("/v1/messages"):
            return self._send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": "not found"}})
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length))
        text = _all_text(body)
        with STATE.lock:
            STATE.requests.append(body)
            STATE.headers.append(dict(self.headers))
            if STATE.reject_fallbacks and "fallbacks" in body:
                return self._send_json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "fallbacks: not supported"}})
            should_fail = STATE.fail_times != 0 and (STATE.fail_when is None or STATE.fail_when in text)
            if should_fail:
                if STATE.fail_times > 0:
                    STATE.fail_times -= 1
                return self._send_json(STATE.fail_status, STATE.fail_body)
        user = _user_text(body)
        if body.get("tools"):
            blocks = web_blocks()
        else:
            schema = (((body.get("output_config") or {}).get("format") or {}).get("schema") or {}).get("properties", {})
            if "argomenti_duplicati" in schema:
                payload = crosscheck_response(user)
            elif "argomenti" in schema:
                payload = inventory_response(user)
            elif "sections" in schema:
                payload = document_response(user)
            else:
                payload = check_response()
            blocks = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
        self._stream(body, blocks)

    def _stream(self, body: dict, blocks: list[dict]):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()

        def event(name: str, payload: dict):
            self.wfile.write(f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())

        event("message_start", {"type": "message_start", "message": {
            "id": "msg_test", "type": "message", "role": "assistant", "model": body.get("model"), "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": len(_all_text(body)) // 4, "output_tokens": 1}}})
        out_tokens = 0
        for index, block in enumerate(blocks):
            if block["type"] == "text":
                event("content_block_start", {"type": "content_block_start", "index": index,
                                              "content_block": {"type": "text", "text": ""}})
                text = block["text"]
                for start in range(0, len(text), 2000):
                    event("content_block_delta", {"type": "content_block_delta", "index": index,
                                                  "delta": {"type": "text_delta", "text": text[start:start + 2000]}})
                out_tokens += len(text) // 4
            elif block["type"] == "server_tool_use":
                event("content_block_start", {"type": "content_block_start", "index": index,
                                              "content_block": {**block, "input": {}}})
                event("content_block_delta", {"type": "content_block_delta", "index": index,
                                              "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
            else:
                event("content_block_start", {"type": "content_block_start", "index": index, "content_block": block})
            event("content_block_stop", {"type": "content_block_stop", "index": index})
        event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                "usage": {"output_tokens": out_tokens}})
        event("message_stop", {"type": "message_stop"})
        self.wfile.flush()


def start(port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

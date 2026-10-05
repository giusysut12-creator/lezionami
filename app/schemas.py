"""Schemi JSON usati con gli output strutturati dell'API di Claude.

Tutti gli oggetti hanno additionalProperties=false e tutti i campi obbligatori,
come richiesto dagli output strutturati. I campi non pertinenti vanno lasciati
vuoti ("" oppure []).
"""

from __future__ import annotations


def _obj(properties: dict, description: str | None = None) -> dict:
    schema = {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }
    if description:
        schema["description"] = description
    return schema


STR = {"type": "string"}
STR_LIST = {"type": "array", "items": {"type": "string"}}
REFS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Identificativi dei passaggi di origine, per esempio T004 oppure D1-012.",
}

# ---------------------------------------------------------------------------
# Inventario
# ---------------------------------------------------------------------------

INVENTORY_SCHEMA = _obj({
    "titolo_proposto": {**STR, "description": "Titolo breve dell'argomento, senza inventare nomi."},
    "tema_generale": STR,
    "data_lezione_rilevata": {**STR, "description": "Data della lezione se esplicita nella fonte, altrimenti stringa vuota."},
    "relatori": {"type": "array", "items": _obj({"nome": STR, "ruolo": STR, "riferimenti": REFS})},
    "prodotti": {"type": "array", "items": _obj({"nome": STR, "descrizione": STR, "riferimenti": REFS})},
    "argomenti": {"type": "array", "items": _obj({
        "id": {**STR, "description": "Identificativo, es. A1, A2..."},
        "titolo": STR,
        "sintesi": STR,
        "sottopunti": STR_LIST,
        "riferimenti": REFS,
    })},
    "elementi": {"type": "array", "items": _obj({
        "id": {**STR, "description": "Identificativo, es. E1, E2..."},
        "argomento_id": STR,
        "categoria": {"type": "string", "enum": [
            "numero", "condizione", "data", "eccezione", "definizione", "procedura",
            "rischio", "costo", "garanzia", "fiscalita", "domanda", "esempio_relatore",
            "opinione", "commerciale_interno", "altro",
        ]},
        "natura": {"type": "string", "enum": [
            "fatto_trascrizione", "opinione_relatore", "indicazione_commerciale_interna",
            "dato_documento_ufficiale",
        ]},
        "testo": {**STR, "description": "Contenuto fedele alla fonte, senza completare frasi interrotte."},
        "valore": {**STR, "description": "Numero o valore esatto come detto nella fonte, oppure vuoto."},
        "base_di_calcolo": {**STR, "description": "A cosa si applica il numero (es. 'sul premio versato', 'annuo sul capitale investito'); vuoto se la fonte non lo dice."},
        "riferimento_temporale": {**STR, "description": "Periodo a cui si riferisce il dato (es. 'dato storico dicembre 2025'), vuoto se assente."},
        "riferimenti": REFS,
    })},
    "termini": {"type": "array", "items": _obj({
        "termine": STR,
        "spiegazione_dalla_fonte": STR,
        "spiegazione_generale": {**STR, "description": "Spiegazione didattica generica, non attribuita al relatore."},
        "riferimenti": REFS,
    })},
    "domande_pubblico": {"type": "array", "items": _obj({
        "domanda": STR,
        "risposta_sintesi": STR,
        "stato": {"type": "string", "enum": ["risposta_completa", "risposta_parziale", "senza_risposta", "interrotta"]},
        "riferimenti": REFS,
    })},
    "criticita": {"type": "array", "items": _obj({
        "tipo": {"type": "string", "enum": [
            "incompleto", "ambiguo", "contraddizione", "interruzione", "numero_incerto",
            "nome_incerto", "dato_storico", "finestra_temporale", "discordanza_fonti", "altro",
        ]},
        "descrizione": STR,
        "riferimenti": REFS,
    })},
    "info_commerciali_interne": {"type": "array", "items": _obj({"testo": STR, "riferimenti": REFS})},
    "fonti_documentali": {"type": "array", "items": _obj({
        "id": {**STR, "description": "Identificativo della fonte, es. D1."},
        "titolo": STR,
        "data": STR,
        "tipo": {**STR, "description": "KID, prospetto, condizioni contrattuali, scheda prodotto, altro."},
        "note": STR,
    })},
})

# ---------------------------------------------------------------------------
# Documenti
# ---------------------------------------------------------------------------

BLOCK_TYPES = [
    "paragraph",       # paragrafo di testo
    "subheading",      # sottotitolo dentro la sezione (usa title)
    "bullets",         # elenco puntato (items)
    "numbered",        # elenco numerato (items)
    "table",           # tabella (columns + rows)
    "example",         # esempio didattico ipotetico, chiaramente distinto dai dati originali
    "note",            # nota informativa
    "warning",         # punto incompleto, ambiguo, contraddittorio o da verificare
    "internal",        # informazione commerciale interna (solo lezione)
    "source_note",     # integrazione da documento ufficiale o da verifica web, con fonte
    "qa",              # domanda (title) e risposta (text)
]

BLOCK_SCHEMA = _obj({
    "type": {"type": "string", "enum": BLOCK_TYPES},
    "title": {**STR, "description": "Titolo del blocco (sottotitolo, titolo esempio, domanda). Vuoto se non serve."},
    "text": {**STR, "description": "Testo del blocco. Ammesso **grassetto**. Vuoto se non serve."},
    "items": {**STR_LIST, "description": "Voci per bullets/numbered; per example i passaggi del calcolo."},
    "columns": {**STR_LIST, "description": "Intestazioni della tabella (2-5 colonne)."},
    "rows": {"type": "array", "items": STR_LIST, "description": "Righe della tabella, stesso numero di celle delle colonne."},
    "refs": REFS,
})

DOCUMENT_SCHEMA = _obj({
    "title": STR,
    "subtitle": STR,
    "footer_label": {**STR, "description": "Etichetta breve a piè di pagina (es. nome del prodotto o del tema)."},
    "limits_notice": {**STR, "description": "Avviso sui limiti della fonte (trascrizione incompleta, integrazioni). Vuoto solo se non ci sono limiti."},
    "sections": {"type": "array", "items": _obj({
        "heading": STR,
        "blocks": {"type": "array", "items": BLOCK_SCHEMA},
    })},
})

# ---------------------------------------------------------------------------
# Controllo di coerenza e copertura
# ---------------------------------------------------------------------------

CHECK_SCHEMA = _obj({
    "esito": {"type": "string", "enum": ["ok", "da_correggere"]},
    "problemi": {"type": "array", "items": _obj({
        "gravita": {"type": "string", "enum": ["alta", "media", "bassa"]},
        "documento": {"type": "string", "enum": ["lezione", "guida", "brochure"]},
        "posizione": STR,
        "problema": STR,
        "correzione": STR,
    })},
    "argomenti_non_coperti": STR_LIST,
    "note": STR,
})

# Lezionami

Applicazione web locale che trasforma la trascrizione di una lezione (TXT, DOCX o PDF testuale, oppure testo incollato) in tre PDF:

1. **Lezione completa** – `Lezione_completa_Titolo.pdf`
2. **Guida di studio** – `Guida_studio_Titolo.pdf`
3. **Brochure cliente** – `Brochure_cliente_Titolo.pdf`

I PDF si scaricano singolarmente o in un unico ZIP, insieme a un report di verifica. L'elaborazione usa l'API di Claude (Anthropic) dal backend. Non servono account dell'app, abbonamenti o database.

---

## 1. Requisiti

| Cosa | Dettagli |
|---|---|
| Python | versione **3.10 o successiva** ([python.org/downloads](https://www.python.org/downloads/)). Su Windows, durante l'installazione spunta «Add python.exe to PATH». |
| Chiave API Anthropic | da creare su [console.anthropic.com](https://console.anthropic.com/) → *API Keys*. Serve credito prepagato o un metodo di pagamento. |
| Connessione Internet | per le chiamate all'API. |

## 2. Installazione e avvio

### Windows
1. Scarica o clona questa cartella.
2. Fai doppio clic su **`avvia_windows.bat`**. La prima volta crea l'ambiente Python e installa le dipendenze (qualche minuto), poi crea il file `.env` e si ferma.
3. Apri `.env` con il Blocco note e incolla la tua chiave dopo `ANTHROPIC_API_KEY=` (senza spazi né virgolette). Salva.
4. Fai di nuovo doppio clic su `avvia_windows.bat`: si apre il browser su **http://127.0.0.1:8000**.

### macOS / Linux
```bash
cd lezionami
./avvia.sh            # prima volta: crea .venv, installa, crea .env e si ferma
# modifica .env e inserisci ANTHROPIC_API_KEY
./avvia.sh            # avvia l'app su http://127.0.0.1:8000
```

### Installazione manuale (qualsiasi sistema)
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # Windows: copy .env.example .env
# inserisci la chiave in .env
python run.py
```

Per chiudere l'app premi `CTRL+C` nella finestra del terminale. Dopo aver modificato `.env`, riavvia l'app.

L'app ascolta solo su `127.0.0.1` (il tuo computer): non è raggiungibile da altri dispositivi né pubblicata online.

## 3. Esempio di utilizzo

1. Apri http://127.0.0.1:8000.
2. In **Trascrizione** carica `tests/fixtures/breve.txt` (trascrizione fittizia inclusa per prova) oppure incolla un testo.
3. Facoltativo: titolo «Orizzonte Famiglia», data, destinatario della brochure (es. «famiglie con figli piccoli»).
4. Facoltativo: aggiungi KID, condizioni o schede prodotto in **Fonti ufficiali**; attiva la **verifica online** solo se ti serve.
5. Premi **Genera i tre PDF** e segui le fasi a schermo.
6. Scarica i tre PDF o **Scarica tutto (ZIP)**. Apri *Controlli eseguiti e punti da verificare* per copertura, segnalazioni residue e costo stimato.
7. Al termine puoi premere **Elimina i dati dal server**.

Dalla riga di comando, senza browser:
```bash
python genera.py tests/fixtures/breve.txt --titolo "Orizzonte Famiglia" --uscita output
python genera.py lezione.docx --fonte KID.pdf --fonte Condizioni.pdf --destinatario "pensionati" --web
```

## 4. Come funziona

| Fase | Cosa fa |
|---|---|
| 1. Estrazione e controllo | Legge TXT/DOCX/PDF. Se un PDF contiene **solo immagini** lo segnala e si ferma, senza chiamare l'API. Rileva interruzioni (es. «Questo file è più lungo di 30 minuti», inviti ad abbonamenti, testo che finisce a metà frase). Divide il testo in passaggi numerati `T001, T002…` (documenti: `D1-001 p.3`). |
| 2. Inventario | Elenca argomenti, numeri con base di calcolo e periodo, condizioni, eccezioni, domande, termini, opinioni, informazioni commerciali interne e criticità. Oltre `SINGLE_PASS_MAX_CHARS` il testo è diviso in segmenti sovrapposti, analizzati in parallelo e poi consolidati; un controllo reintegra elementi persi nel consolidamento, così le parti finali non vanno perse. |
| Verifica web (facoltativa, spenta di default) | Solo fonti ufficiali. Registra le pagine effettivamente consultate e l'orario. Una «conferma» che cita una pagina non consultata viene declassata a «non verificabile»; se la ricerca fallisce nulla è dichiarato verificato e i documenti lo dicono. |
| 3. Generazione | Prima la lezione completa (con fonte integrale + inventario), poi guida e brochure basate su inventario e lezione, per coerenza. |
| 4. Controllo | Un controllo AI incrociato (copertura, coerenza, invenzioni, distinzioni finanziarie, calcoli, contenuti interni nella brochure) più controlli automatici: ricalcolo delle operazioni negli esempi, numeri assenti dalle fonti, termini interni e promesse nella brochure, copertura degli argomenti e della parte finale, dichiarazione dei limiti. I documenti con problemi gravi o medi vengono revisionati una volta. |
| 5. PDF | ReportLab: A4, testo selezionabile, font DejaVu Sans incluso, titoli gerarchici, tabelle con intestazione ripetuta e righe mai spezzate (salvo celle più alte di una pagina), riquadri distinti per esempi ipotetici, avvertenze, uso interno e integrazioni da fonti esterne, numeri di pagina «n / totale». Ogni PDF viene riletto per verificare pagine e testo estraibile. |

Le **regole di accuratezza** richieste (nessuna invenzione, frasi interrotte non completate, dati storici non attualizzati, finestre commerciali scadute, numeri ambigui segnalati, discordanze evidenziate, esempi ipotetici dichiarati e verificati, distinzioni finanziarie e assicurative) sono nel prompt di sistema comune a tutte le fasi: `app/prompts.py`. Il testo caricato viene trattato come dato da analizzare: eventuali istruzioni al suo interno non cambiano il comportamento dell'app.

Se una chiamata all'API fallisce, l'interfaccia mostra un messaggio comprensibile (chiave non valida, credito esaurito, limite di richieste, servizio sovraccarico, rete assente…) e il pulsante **Riprova** riparte dalla fase non riuscita, riusando quelle già completate (anche i singoli segmenti già analizzati). Non viene mai prodotto un documento simulato.

## 5. Configurazione (`.env`)

Le principali variabili (tutte descritte in `.env.example`):

| Variabile | Predefinito | Note |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Obbligatoria. Resta sul server: non arriva mai al browser né nei log. |
| `CLAUDE_MODEL` | `claude-opus-5-5` | Modello configurabile; `claude-sonnet-5-5` costa circa la metà. |
| `CLAUDE_EFFORT` | `high` | `low`/`medium`/`high`/`xhigh`/`max`: più alto = più accurato e più costoso. |
| `CLAUDE_REFUSAL_FALLBACK` | `true` | Se il modello rifiuta una richiesta per i suoi filtri di sicurezza, l'API la ripete automaticamente su un modello alternativo (`fallbacks: "default"`). Mettere `false` per disattivarlo. |
| `SINGLE_PASS_MAX_CHARS` / `SEGMENT_MAX_CHARS` | 60000 / 40000 | Soglie della segmentazione per trascrizioni lunghe. |
| `SHOW_SOURCE_REFS` | `true` | Mostra nella lezione i riferimenti `Rif. fonte: T012…`, verificabili con l'indice dei passaggi nel report. |
| `JOB_TTL_MINUTES` | 120 | Dopo questo tempo i dati vengono cancellati dalla memoria. |

## 6. Servizi esterni e costi

L'unico servizio esterno obbligatorio è l'**API di Claude (Anthropic)**, a consumo, senza canone. Prezzi di listino al momento della scrittura (verifica quelli vigenti su [anthropic.com/pricing](https://www.anthropic.com/pricing)):

| Modello | Input | Output |
|---|---|---|
| Claude Opus 5.5 (predefinito) | 4 $ / milione di token | 20 $ / milione di token |
| Claude Sonnet 5.5 | 2 $ / milione di token | 10 $ / milione di token |

Ordini di grandezza indicativi con Opus 5.5 (variano con lunghezza della fonte, numero di revisioni e livello di effort): trascrizione breve (2–3 pagine) circa 0,30–1 $; un'ora di lezione (circa 9.000 parole) circa 1,5–4 $; registrazioni di più ore o con molti documenti allegati di più. Una parte dei token di input ripetuti viene addebitata a tariffa ridotta grazie alla cache dei prompt. L'interfaccia e il report mostrano, a fine lavoro, token usati e costo stimato.

La **verifica web** facoltativa usa gli strumenti di ricerca e lettura web di Anthropic: oltre ai token delle pagine lette, la ricerca ha un costo per numero di ricerche (secondo il listino vigente). Deve essere abilitata per la tua organizzazione nella Console Anthropic; se non lo è, la fase risulta «non riuscita» e il resto prosegue.

## 7. Privacy

- La trascrizione e i documenti vengono inviati ad Anthropic per l'elaborazione (l'interfaccia lo indica prima dell'invio).
- L'app non salva nulla su disco: file, testi e PDF restano in memoria, vengono cancellati dopo `JOB_TTL_MINUTES`, alla chiusura dell'app o con «Elimina i dati dal server». I file originali vengono scartati subito dopo l'estrazione del testo.
- I log contengono solo fase, durata, token e tipo di errore: mai testi, dati personali o chiavi.
- Il report di verifica incluso nello ZIP contiene l'indice dei passaggi della trascrizione: tienine conto se condividi lo ZIP.

## 8. Verifiche eseguite e prove ancora da fare

**Eseguite senza chiave API** (`python -m pytest`, 39 test, tutti superati). Le chiamate all'AI sono state sostituite da un server finto che imita il protocollo di streaming dell'API (`tests/mock_claude.py`); le sue risposte sono segnaposto marcati «[DATI DI TEST]», quindi questi test verificano il **flusso**, non la qualità dei contenuti:

- trascrizione breve, incollata o da file, con download dei tre PDF e dello ZIP;
- trascrizione lunga (145.000 caratteri): segmentazione in più parti, consolidamento, presenza dell'ultima parte negli inventari;
- trascrizione interrotta: messaggio «più lungo di 30 minuti» rilevato e passato al modello;
- fonte con numeri contraddittori e istruzione nascosta nel testo: resta dentro `<trascrizione>`, non entra nelle istruzioni;
- PDF solo immagini (trascrizione: blocco senza chiamate API; fonte aggiuntiva: esclusa con avviso);
- errori dell'API: chiave non valida (401), credito esaurito (400), errore del server dopo i ritentativi (500), rete assente; **Riprova** riparte senza ripetere l'inventario;
- verifica web riuscita e fallita; estrazione da TXT, DOCX e PDF; impaginazione (A4, numeri di pagina, testo selezionabile, intestazioni ripetute, righe non duplicate, celle enormi);
- interfaccia provata in Chromium da desktop e da smartphone (iPhone 13): nessuno scorrimento orizzontale, nessun errore JavaScript, download funzionanti.

**Da eseguire con una chiave API reale** (non possibili in questo ambiente):

1. `python genera.py tests/fixtures/breve.txt --uscita output/breve`
2. `python genera.py tests/fixtures/lunga.txt --uscita output/lunga`
3. `python genera.py tests/fixtures/interrotta.txt --uscita output/interrotta` – il documento deve dichiarare l'interruzione e non completare la frase sui «danni da acqua condotta»; lo sconto con scadenza 30 aprile 2026 non deve apparire come ancora valido.
4. `python genera.py tests/fixtures/contraddittoria.txt --uscita output/contraddittoria` – devono comparire le discordanze (caricamento 1% / 0,75%; commissione 1,2% / 1,4%), la brochure non deve contenere retrocessioni, obiettivi di vendita né la frase «rendimento garantito al 5%».
5. Una tua trascrizione reale con KID/condizioni allegati, confrontando i PDF con quelli di riferimento.
6. Facoltativo: la stessa prova con la verifica web attiva.

Su queste prove vanno controllati a occhio i PDF renderizzati (impaginazione con contenuti reali, lunghezza della guida 3–5 pagine e della brochure 2–4) e la compatibilità degli schemi di output strutturato con l'account (in caso di rifiuto l'app ripiega automaticamente su JSON richiesto nel prompt).

## 9. Struttura del progetto

```
app/
  main.py         server FastAPI ed endpoint
  pipeline.py     fasi, ripresa dopo errore, report, ZIP
  extract.py      lettura TXT/DOCX/PDF, PDF solo immagini, interruzioni, passaggi
  prompts.py      prompt e regole di accuratezza
  schemas.py      schemi JSON di inventario, documenti e controllo
  llm.py          client Claude, errori comprensibili, verifica web
  checks.py       controlli automatici (calcoli, numeri, brochure, copertura)
  pdf_render.py   impaginazione PDF
  static/         interfaccia (HTML, CSS, JavaScript)
  fonts/          DejaVu Sans (licenza inclusa)
tests/            test automatici, server finto, trascrizioni di prova
run.py            avvio del server locale
genera.py         uso da riga di comando
```

Test: `pip install pytest requests` e poi `python -m pytest`.

## 10. Limiti noti

- Niente OCR: i PDF scansionati vanno convertiti prima in testo. I vecchi `.doc` non sono supportati (salvarli come `.docx`).
- I lavori vivono in memoria: chiudendo l'app si perdono (scarica prima i PDF).
- Il controllo dei calcoli riconosce operazioni scritte in forma esplicita (`12 × 100 € = 1.200 €`); il controllo dei numeri segnala valori assenti dalle fonti ma non può distinguere sempre un calcolo derivato corretto.
- La qualità dei contenuti dipende dal modello: i documenti vanno sempre riletti prima dell'uso, in particolare le parti segnalate nei «punti da verificare».

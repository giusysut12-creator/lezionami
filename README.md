# Lezionami

Applicazione web che trasforma la trascrizione di una lezione (TXT, DOCX o PDF testuale, oppure testo incollato) in tre PDF:

1. **Lezione completa** – `Lezione_completa_Titolo.pdf`
2. **Guida di studio** – `Guida_studio_Titolo.pdf`
3. **Brochure cliente** – `Brochure_cliente_Titolo.pdf`

I PDF si scaricano singolarmente o in un unico ZIP, insieme a un report di verifica. L'elaborazione usa l'API di Claude (Anthropic) dal backend. Funziona **online su Vercel** (protetta da password) oppure sul proprio computer. Non servono database.

---

## 1. Pubblicare online su Vercel

### Cosa serve
- Un account [Vercel](https://vercel.com) collegato a GitHub.
- Una chiave API Anthropic, da [console.anthropic.com](https://console.anthropic.com/) → *API Keys* (con credito o metodo di pagamento).

### Passi
1. **Porta il codice sul branch di produzione.** Vercel pubblica in produzione il branch principale (`main`). Unisci il branch di sviluppo in `main` (con una pull request), oppure in Vercel → *Settings → Git → Production Branch* scegli il branch che contiene l'app.
2. Su Vercel: **Add New… → Project → Import** il repository `lezionami`. Il preset **FastAPI** viene riconosciuto da solo (entrypoint `index.py`): non serve cambiare comandi di build.
3. Prima di premere *Deploy*, apri **Environment Variables** e aggiungi:

   | Nome | Valore |
   |---|---|
   | `ANTHROPIC_API_KEY` | la tua chiave `sk-ant-…` |
   | `APP_PASSWORD` | una password lunga da dare solo a chi deve usare l'app |
   | `CLAUDE_MODEL` *(facoltativo)* | `claude-opus-5-5` (predefinito) oppure `claude-sonnet-5-5`, più economico |

   Senza `APP_PASSWORD` l'app online non si apre: mostra un messaggio di configurazione incompleta, così nessuno può consumare il tuo credito.
4. Premi **Deploy**. Al termine apri l'indirizzo `https://….vercel.app`: compare la pagina di accesso.
5. Controlla in *Settings → Functions* che **Fluid Compute** sia attivo (lo è di norma nei progetti nuovi): serve per avere fino a 300 secondi per passo.
6. Consigliato: nella Console Anthropic imposta un **limite di spesa mensile** (*Settings → Limits*), come protezione aggiuntiva.

Dopo ogni modifica alle variabili d'ambiente serve un nuovo deploy (*Deployments → … → Redeploy*).

### Come funziona su Vercel
Su Vercel ogni richiesta ha un tempo massimo e il server non conserva memoria tra una richiesta e l'altra. Per questo l'elaborazione è divisa in **passi brevi**, ognuno con al massimo una chiamata all'AI, coordinati dal browser:

- il browser conserva i risultati intermedi; se un passo non riesce, **Riprova** riparte da quel passo;
- le trascrizioni lunghe sono analizzate per segmenti e la lezione completa è scritta in più parti, così nessun passo supera il limite;
- il server non salva nulla: trascrizione e PDF restano solo nella pagina del browser (lo ZIP viene creato nel browser).

**Non chiudere né ricaricare la pagina durante l'elaborazione** (il browser lo chiede prima di uscire).

### Limiti di Vercel da conoscere
| Limite | Effetto | Rimedio |
|---|---|---|
| 300 secondi per richiesta (piano Hobby) | Se un passo è più lungo compare un errore «tempo massimo». | Premi *Riprova*; se si ripete, imposta `CLAUDE_EFFORT=medium`, oppure con il piano Pro alza `maxDuration` in `vercel.json` (fino a 800). |
| 4,5 MB per richiesta | File caricati in totale fino a 4 MB. | Per KID e set informativi molto pesanti, caricare solo le pagine utili o la versione testuale. |
| Piano Hobby solo per uso personale non commerciale | Termini d'uso di Vercel. | Per uso aziendale serve il piano Pro. |

Alternative già pronte: **Render** (`render.yaml`, *New → Blueprint*, server sempre attivo da circa 7 $/mese, regione Francoforte) e qualsiasi servizio che esegue container (`Dockerfile`). In entrambi i casi va impostata `APP_PASSWORD`.

## 2. Uso sul proprio computer

Serve Python **3.10 o successivo** ([python.org/downloads](https://www.python.org/downloads/); su Windows spunta «Add python.exe to PATH»).

- **Windows:** doppio clic su `avvia_windows.bat`. La prima volta installa tutto, crea `.env` e si ferma: apri `.env`, inserisci `ANTHROPIC_API_KEY=` e rilancia.
- **macOS / Linux:** `./avvia.sh` (stesso comportamento).
- **Manuale:**
  ```bash
  python -m venv .venv
  # Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
  pip install -r requirements.txt
  cp .env.example .env          # Windows: copy .env.example .env  → inserisci la chiave
  python run.py                 # apre http://127.0.0.1:8000
  ```

In locale (indirizzo `127.0.0.1`) la password non è richiesta. Dalla riga di comando, senza browser:
```bash
python genera.py tests/fixtures/breve.txt --titolo "Orizzonte Famiglia" --uscita output
python genera.py lezione.docx --fonte KID.pdf --fonte Condizioni.pdf --destinatario "pensionati" --web
```

## 3. Esempio di utilizzo

1. Apri l'app (online: inserisci la password).
2. In **Trascrizione** carica `tests/fixtures/breve.txt` (trascrizione fittizia di prova) oppure incolla un testo.
3. Facoltativo: titolo «Orizzonte Famiglia», data, destinatario della brochure (es. «famiglie con figli piccoli»).
4. Facoltativo: allega KID, condizioni o schede prodotto in **Fonti ufficiali**; attiva la **verifica online** solo se serve.
5. Premi **Genera i tre PDF** e segui le fasi.
6. Scarica i tre PDF o **Scarica tutto (ZIP)**; apri *Controlli eseguiti e punti da verificare* per copertura, segnalazioni e costo stimato.

## 4. Le fasi dell'elaborazione

| Fase | Cosa fa |
|---|---|
| 1. Estrazione e controllo | Legge TXT/DOCX/PDF. Un PDF fatto **solo di immagini** viene segnalato e l'elaborazione si ferma senza chiamare l'AI. Rileva interruzioni (es. «Questo file è più lungo di 30 minuti», inviti ad abbonamenti, testo che finisce a metà frase). Divide il testo in passaggi numerati `T001, T002…` (documenti: `D1-001 p.3`). |
| 2. Inventario | Argomenti, numeri con base di calcolo e periodo, condizioni, eccezioni, domande, termini, opinioni, informazioni commerciali interne, criticità. Oltre `SINGLE_PASS_MAX_CHARS` il testo è diviso in segmenti sovrapposti analizzati in parallelo; l'unione è fatta dal programma (nessun elemento viene scartato) e un controllo incrociato AI segnala contraddizioni tra segmenti. |
| Verifica web (facoltativa, spenta) | Solo fonti ufficiali; registra pagine consultate e orario. Una «conferma» che cita una pagina non consultata viene declassata a «non verificabile»; se la ricerca fallisce nulla è dichiarato verificato. |
| 3. Generazione | Lezione completa (fonte integrale + inventario), scritta in più parti se gli argomenti sono molti; poi guida e brochure basate su inventario e lezione, per coerenza. |
| 4. Controllo | Controllo AI incrociato (copertura, coerenza, invenzioni, distinzioni finanziarie, calcoli, contenuti interni nella brochure) e controlli automatici (ricalcolo delle operazioni, numeri assenti dalle fonti, termini interni e promesse nella brochure, copertura, dichiarazione dei limiti). I documenti con problemi gravi o medi vengono revisionati. |
| 5. PDF | A4, testo selezionabile, font DejaVu Sans incluso, tabelle con intestazione ripetuta e righe non spezzate, riquadri per esempi ipotetici, avvertenze, uso interno e fonti esterne, numeri di pagina «n / totale». Ogni PDF viene riletto per verificarne pagine e testo. |

Le **regole di accuratezza** (nessuna invenzione, frasi interrotte non completate, dati storici non attualizzati, finestre commerciali scadute, numeri ambigui segnalati, discordanze evidenziate, esempi ipotetici dichiarati e verificati, distinzioni finanziarie e assicurative) sono nel prompt comune a tutte le fasi: `app/prompts.py`. Il testo caricato è trattato come dato: eventuali istruzioni al suo interno non cambiano il comportamento dell'app.

Gli errori dell'API sono mostrati in modo comprensibile (chiave non valida, credito esaurito, limite di richieste, servizio sovraccarico, rete, tempo massimo). Non viene mai prodotto un documento simulato.

## 5. Configurazione

Variabili d'ambiente (in locale nel file `.env`, su Vercel in *Settings → Environment Variables*). Elenco completo in `.env.example`.

| Variabile | Predefinito | Note |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Obbligatoria. Resta sul server: non arriva mai al browser né nei log. |
| `APP_PASSWORD` | – | Obbligatoria online. Cambiandola, tutte le sessioni aperte decadono. |
| `CLAUDE_MODEL` | `claude-opus-5-5` | `claude-sonnet-5-5` costa circa la metà. |
| `CLAUDE_EFFORT` | `high` | `low`/`medium`/`high`/`xhigh`/`max`: più alto = più accurato, più lento e più costoso. |
| `CLAUDE_REFUSAL_FALLBACK` | `true` | Se il modello rifiuta una richiesta per i suoi filtri di sicurezza, l'API la ripete su un modello alternativo. |
| `SINGLE_PASS_MAX_CHARS` / `SEGMENT_MAX_CHARS` | 30000 / 25000 | Soglie della segmentazione. |
| `LESSON_PART_TOPICS` | 4 | Argomenti per ciascuna parte della lezione completa. |
| `SHOW_SOURCE_REFS` | `true` | Riferimenti `Rif. fonte: T012…` nella lezione, verificabili con l'indice dei passaggi nel report. |
| `SESSION_HOURS` | 12 | Durata dell'accesso. |

## 6. Servizi esterni e costi

| Servizio | Costo |
|---|---|
| **API di Claude (Anthropic)** – obbligatoria | A consumo, senza canone. Opus 5.5: 4 $ / milione di token in ingresso, 20 $ / milione in uscita. Sonnet 5.5: 2 $ / 10 $. Verifica i prezzi vigenti su [anthropic.com/pricing](https://www.anthropic.com/pricing). |
| **Vercel** – solo se pubblichi online | Hobby gratuito (uso personale non commerciale); Pro 20 $/mese per utente per uso aziendale e tempi più lunghi. |
| Ricerca web di Anthropic – facoltativa | Costo per numero di ricerche secondo listino, più i token delle pagine lette. Va abilitata per l'organizzazione nella Console Anthropic. |

Ordini di grandezza indicativi con Opus 5.5: trascrizione breve circa 0,30–1 $; un'ora di lezione (circa 9.000 parole) circa 1,5–4 $; registrazioni di più ore di più. L'app mostra a fine lavoro token usati e costo stimato.

## 7. Privacy

- Testo e documenti vengono inviati al server dell'app e ad Anthropic solo per l'elaborazione (l'interfaccia lo indica).
- Il server non salva nulla su disco né in memoria tra una richiesta e l'altra: i risultati restano nella pagina del browser e spariscono chiudendola o con «Cancella i dati dalla pagina».
- I log contengono solo nome del passo, durata e tipo di errore: mai testi, dati personali o chiavi.
- Il report nello ZIP contiene l'indice dei passaggi della trascrizione: tienine conto se condividi lo ZIP.
- Online l'accesso è protetto da password (cookie firmato, HttpOnly; blocco temporaneo dopo 5 tentativi errati).

## 8. Verifiche eseguite e prove ancora da fare

**Eseguite senza chiave API** (`python -m pytest`, 47 test, tutti superati). Le chiamate all'AI sono sostituite da un server finto che imita il protocollo dell'API (`tests/mock_claude.py`), con risposte segnaposto «[DATI DI TEST]»: i test verificano il **flusso**, non la qualità dei contenuti.

- trascrizione breve, incollata o da file; tre PDF, report e nomi dei file;
- trascrizione lunga (145.000 caratteri): 6 segmenti, unione, controllo incrociato, lezione scritta in più parti con tutti gli argomenti, parte finale presente;
- trascrizione interrotta (messaggio «più lungo di 30 minuti» rilevato e passato al modello);
- fonte contraddittoria con istruzione nascosta: resta dentro `<trascrizione>`, fuori dalle istruzioni;
- PDF solo immagini (trascrizione bloccata senza chiamate API; fonte aggiuntiva esclusa con avviso);
- errori API: chiave non valida, credito esaurito, errore del server dopo i ritentativi, rete assente; ripresa dal passo fallito senza ripetere l'inventario;
- verifica web riuscita e fallita; impaginazione (A4, numeri di pagina, testo selezionabile, intestazioni ripetute, celle enormi);
- modalità online: password obbligatoria, Vercel riconosciuto automaticamente, accesso errato/corretto, cookie falsificato, cambio password, blocco dopo tentativi, uscita;
- browser (Chromium) desktop e iPhone 13 con simulazione Vercel: accesso, elaborazione completa, errore e *Riprova*, download di PDF, report e ZIP (verificato integro), nessuno scorrimento orizzontale né errore JavaScript.

**Da eseguire con chiave API reale:**
1. In locale: `python genera.py tests/fixtures/breve.txt --uscita output/breve`, e lo stesso con `lunga.txt`, `interrotta.txt`, `contraddittoria.txt`.
   - `interrotta.txt`: il documento deve dichiarare l'interruzione, non completare la frase sui «danni da acqua condotta» e non presentare come valido lo sconto scaduto il 30 aprile 2026.
   - `contraddittoria.txt`: devono comparire le discordanze (caricamento 1% / 0,75%; commissione 1,2% / 1,4%); la brochure non deve contenere retrocessioni, obiettivi di vendita né «rendimento garantito al 5%».
2. Su Vercel: una trascrizione breve e una lunga, verificando che nessun passo superi i 300 secondi.
3. Una tua trascrizione reale con KID e condizioni, confrontando i PDF con quelli di riferimento (impaginazione, guida 3–5 pagine, brochure 2–4).

## 9. Struttura del progetto

```
app/
  main.py          server FastAPI, accesso, endpoint (index.py lo espone a Vercel)
  steps.py         passi senza stato: inventario, verifica web, documenti, controllo, PDF
  orchestrator.py  sequenza dei passi per riga di comando e test (il browser usa app.js)
  extract.py       lettura TXT/DOCX/PDF, PDF solo immagini, interruzioni, passaggi
  prompts.py       prompt e regole di accuratezza
  schemas.py       schemi JSON degli output strutturati
  llm.py           client Claude, errori comprensibili, verifica web
  checks.py        controlli automatici (calcoli, numeri, brochure, copertura)
  pdf_render.py    impaginazione PDF
  auth.py          password e sessioni per l'uso online
  static/          interfaccia (HTML, CSS, JavaScript)
  fonts/           DejaVu Sans (licenza inclusa)
tests/             test automatici, server finto dell'API, trascrizioni di prova
vercel.json        durata massima delle funzioni su Vercel
render.yaml, Dockerfile   pubblicazione alternativa
run.py, genera.py  avvio locale e uso da riga di comando
```

Test: `pip install pytest requests` e poi `python -m pytest`.

## 10. Limiti noti

- Niente OCR: i PDF scansionati vanno convertiti prima in testo. I vecchi `.doc` vanno salvati come `.docx`.
- Chiudendo o ricaricando la pagina durante l'elaborazione il lavoro si perde (il server non conserva nulla).
- Online i limiti di tentativi di accesso valgono per singola istanza del server: per limitare la spesa usa anche il limite mensile nella Console Anthropic.
- Il controllo dei calcoli riconosce operazioni scritte in forma esplicita (`12 × 100 € = 1.200 €`); il controllo dei numeri segnala valori assenti dalle fonti ma non distingue sempre un calcolo derivato corretto.
- La qualità dei contenuti dipende dal modello: i documenti vanno riletti prima dell'uso, in particolare i «punti da verificare».

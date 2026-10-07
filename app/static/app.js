"use strict";

/*
 * Il browser coordina l'elaborazione: invia al server un passo alla volta
 * (ognuno con al massimo una chiamata all'AI) e conserva i risultati intermedi.
 * Il server non tiene dati: «Riprova» riparte dal passo non riuscito.
 */

const $ = (sel) => document.querySelector(sel);
const ui = { tab: "file", maxMb: 30, parallel: 3, running: false, urls: [] };
let run = null; // { form, state, phases, error }

const PHASES = [
  ["estrazione", "Estrazione e controllo del testo", 5],
  ["inventario", "Inventario di argomenti, numeri e condizioni", 25],
  ["generazione", "Generazione dei tre documenti", 37],
  ["controllo", "Controllo di coerenza e copertura", 15],
  ["pdf", "Creazione e verifica dei PDF", 10],
];
const STATUS_ICON = { completata: "✓", errore: "!", saltata: "–", non_riuscita: "!", in_corso: "", attesa: "" };
const USAGE_KEYS = ["chiamate", "token_input", "token_output", "token_cache_scrittura", "token_cache_lettura"];
const DOC_LABEL = { lezione: "Lezione completa", guida: "Guida di studio", brochure: "Brochure cliente" };

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Sessione scaduta: torna alla pagina di accesso.
const nativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const res = await nativeFetch(...args);
  if (res.status === 401) {
    const data = await res.clone().json().catch(() => ({}));
    if (data.login) window.location.href = "/login";
  }
  return res;
};

async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    const cfg = await res.json();
    ui.maxMb = cfg.max_upload_mb;
    ui.parallel = cfg.parallel_requests || 3;
    document.querySelectorAll(".maxmb").forEach((el) => (el.textContent = cfg.max_upload_mb));
    $("#keyBanner").hidden = cfg.api_key_configured;
    $("#logoutBtn").hidden = !cfg.auth_enabled;
    $("#serverNote").hidden = !cfg.public_mode;
  } catch (e) {
    /* gli errori espliciti arriveranno all'invio */
  }
}

// --- Modulo ---------------------------------------------------------------------
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    ui.tab = btn.dataset.tab;
    document.querySelectorAll(".tab").forEach((b) => {
      b.classList.toggle("active", b === btn);
      b.setAttribute("aria-selected", b === btn ? "true" : "false");
    });
    $("#panel-file").hidden = ui.tab !== "file";
    $("#panel-text").hidden = ui.tab !== "text";
  });
});

const fileInput = $("#transcriptFile");
const dropzone = $("#dropzone");
function showTranscriptName() {
  const file = fileInput.files[0];
  $("#transcriptName").hidden = !file;
  if (file) $("#transcriptName").textContent = `${file.name} · ${(file.size / 1024).toFixed(0)} KB`;
}
fileInput.addEventListener("change", showTranscriptName);
["dragenter", "dragover"].forEach((ev) => dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("over"); }));
dropzone.addEventListener("drop", (e) => {
  if (e.dataTransfer.files.length) {
    fileInput.files = e.dataTransfer.files;
    showTranscriptName();
  }
});
$("#transcriptText").addEventListener("input", (e) => {
  $("#charCount").textContent = `${e.target.value.length.toLocaleString("it-IT")} caratteri`;
});

function formError(msg) {
  $("#formError").textContent = msg;
  $("#formError").hidden = !msg;
}

function buildForm() {
  const form = new FormData();
  const limit = ui.maxMb * 1024 * 1024;
  let total = 0;
  if (ui.tab === "file") {
    const file = fileInput.files[0];
    if (!file) throw new Error("Seleziona un file con la trascrizione, oppure scegli «Incolla testo».");
    if (!/\.(txt|docx|pdf|md|srt|vtt)$/i.test(file.name)) throw new Error("Formato non supportato: usa TXT, DOCX o PDF testuale.");
    total += file.size;
    form.append("transcript_file", file);
  } else {
    const text = $("#transcriptText").value.trim();
    if (text.length < 20) throw new Error("Incolla il testo della trascrizione.");
    total += new Blob([text]).size;
    form.append("transcript_text", text);
  }
  for (const name of ["title", "lesson_date", "recipient"]) form.append(name, $(`[name=${name}]`).value.trim());
  if (total > limit) throw new Error(`Il file supera ${ui.maxMb} MB, il limite di questo server.`);
  return form;
}

$("#jobForm").addEventListener("submit", (e) => {
  e.preventDefault();
  formError("");
  let form;
  try {
    form = buildForm();
  } catch (err) {
    return formError(err.message);
  }
  run = { form, state: null, phases: {}, error: null };
  PHASES.forEach(([key]) => (run.phases[key] = { status: "attesa", detail: "", done: 0, total: 0 }));
  $("#jobForm").hidden = true;
  $("#progressCard").hidden = false;
  $("#resultCard").hidden = true;
  window.scrollTo({ top: 0, behavior: "smooth" });
  execute();
});

// --- Chiamate al server ---------------------------------------------------------
class StepError extends Error {
  constructor(message, retryable = true) {
    super(message);
    this.retryable = retryable;
  }
}

async function errorFrom(res, seconds) {
  const data = await res.json().catch(() => null);
  if (data && data.detail) return new StepError(data.detail, data.retryable !== false && res.status !== 422);
  if (res.status === 504) {
    if (seconds < 75) {
      return new StepError(`Il server ha interrotto il passo dopo ${seconds} secondi: il limite attivo sembra di 60 secondi. Su Vercel attiva Fluid Compute (Settings → Functions), poi rifai il deploy e premi «Riprova».`);
    }
    return new StepError(`Il passo ha superato il tempo massimo consentito dal server (interrotto dopo ${seconds} secondi). Premi «Riprova»; se si ripete, imposta CLAUDE_EFFORT=medium oppure, con un piano Pro, aumenta maxDuration in vercel.json.`);
  }
  if (res.status === 413) return new StepError("I dati inviati superano il limite di dimensione del server (4,5 MB su Vercel). Usa un file più piccolo o la versione testuale (TXT).", false);
  return new StepError(`Errore del server (codice ${res.status}, dopo ${seconds} secondi). Premi «Riprova».`);
}

async function post(url, body, isJson) {
  let res;
  const started = Date.now();
  try {
    res = await fetch(url, isJson
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : { method: "POST", body });
  } catch (err) {
    throw new StepError("Connessione al server interrotta. Controlla la rete e premi «Riprova».");
  }
  if (!res.ok) throw await errorFrom(res, Math.round((Date.now() - started) / 1000));
  return res.json();
}

// Dati da inviare per ciascun passo (il resto dello stato resta nel browser).
const STEP_KEYS = {
  inventory: ["meta", "source", "plan"],
  inventory_part: ["meta", "source", "plan"],
  inventory_merge: ["meta", "source", "plan", "inventory_partials"],
  lesson_part: ["meta", "source", "inventory"],
  guide: ["meta", "source", "inventory", "lesson_parts"],
  brochure: ["meta", "source", "inventory", "lesson_parts"],
  check: ["meta", "source", "inventory", "lesson_parts", "docs"],
  revise: ["meta", "source", "inventory", "lesson_parts", "docs", "check"],
  finalize: ["meta", "source", "inventory", "lesson_parts", "docs", "check", "revised", "warnings"],
};

async function step(name, args = {}) {
  const state = {};
  for (const key of STEP_KEYS[name]) if (run.state[key] !== undefined) state[key] = run.state[key];
  const output = await post("/api/step", { step: name, args, state }, true);
  const usage = (run.state.usage ||= Object.fromEntries(USAGE_KEYS.map((k) => [k, 0])));
  for (const key of USAGE_KEYS) usage[key] += (output.usage || {})[key] || 0;
  return output.result;
}

async function pool(tasks, limit) {
  const errors = [];
  let next = 0;
  async function worker() {
    while (next < tasks.length) {
      const task = tasks[next++];
      try { await task(); } catch (err) { errors.push(err); }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, tasks.length) }, worker));
  if (errors.length) throw errors[0];
}

function setPhase(key, status, detail) {
  const phase = run.phases[key];
  if (status) phase.status = status;
  if (detail !== undefined) phase.detail = detail;
  renderProgress();
}

function tick(key, done, total, detail) {
  const phase = run.phases[key];
  phase.done = done;
  phase.total = total;
  setPhase(key, "in_corso", detail);
}

// --- Sequenza dei passi -----------------------------------------------------------
async function execute() {
  if (ui.running) return;
  ui.running = true;
  run.error = null;
  $("#errorBox").hidden = true;
  let current = "estrazione";
  try {
    const s = () => run.state;
    if (!run.state) {
      setPhase(current, "in_corso", "Lettura del file");
      const extracted = await post("/api/extract", run.form, false);
      run.state = { ...extracted, warnings: extracted.warnings || [] };
      setPhase(current, "completata", extracted.detail);
    }

    current = "inventario";
    if (!s().inventory) {
      const plan = s().plan;
      if (plan.mode === "segmented") {
        const partials = (s().inventory_partials ||= {});
        const total = plan.parts.length;
        tick(current, Object.keys(partials).length, total + 1, `Segmenti analizzati: ${Object.keys(partials).length} di ${total}`);
        await pool(plan.parts.filter((p) => !partials[p.key]).map((p) => async () => {
          const result = await step("inventory_part", { key: p.key });
          partials[result.key] = result.inventory;
          tick(current, Object.keys(partials).length, total + 1, `Segmenti analizzati: ${Object.keys(partials).length} di ${total}`);
        }), ui.parallel);
        setPhase(current, "in_corso", "Unione dei segmenti e controllo incrociato");
        const merged = await step("inventory_merge");
        Object.assign(s(), { inventory: merged.inventory, lesson_plan: merged.lesson_plan });
        setPhase(current, "completata", merged.detail);
      } else {
        setPhase(current, "in_corso", "Analisi completa in un unico passaggio");
        const result = await step("inventory");
        Object.assign(s(), { inventory: result.inventory, lesson_plan: result.lesson_plan });
        setPhase(current, "completata", result.detail);
      }
    }

    current = "generazione";
    const parts = (s().lesson_parts ||= {});
    const docs = (s().docs ||= {});
    const plan = s().lesson_plan;
    const totalDocs = plan.length + 2;
    const progressGen = (detail) => tick(current, Object.keys(parts).length + Object.keys(docs).length, totalDocs, detail);
    const lessonTask = (index) => async () => {
      const result = await step("lesson_part", { index });
      parts[result.index] = result.doc;
      progressGen(plan.length > 1 ? `Lezione completa: parte ${Object.keys(parts).length} di ${plan.length}` : "Lezione completa scritta");
    };
    progressGen(plan.length > 1 ? `Scrittura della lezione completa in ${plan.length} parti` : "Scrittura della lezione completa");
    if (!parts["0"]) await lessonTask(0)();
    await pool(plan.filter((p) => !parts[String(p.index)]).map((p) => lessonTask(p.index)), ui.parallel);
    progressGen("Scrittura di guida di studio e brochure");
    await pool([["guida", "guide"], ["brochure", "brochure"]].filter(([k]) => !docs[k]).map(([kind, name]) => async () => {
      docs[kind] = (await step(name)).doc;
      progressGen(`${DOC_LABEL[kind]} pronta`);
    }), 2);
    setPhase(current, "completata", "Lezione, guida e brochure generate");

    current = "controllo";
    if (!s().check) {
      setPhase(current, "in_corso", "Controllo incrociato dei tre documenti");
      s().check = await step("check");
    }
    const toFix = s().check.to_fix;
    const revised = (s().revised ||= {});
    const tasks = [];
    if ((toFix.lezione || []).length) {
      const lr = (revised.lezione ||= {});
      plan.filter((p) => !lr[String(p.index)]).forEach((p) => tasks.push(async () => {
        lr[String(p.index)] = (await step("revise", { kind: "lezione", index: p.index })).doc;
      }));
    }
    ["guida", "brochure"].filter((k) => (toFix[k] || []).length && !revised[k]).forEach((kind) => tasks.push(async () => {
      revised[kind] = (await step("revise", { kind })).doc;
    }));
    if (tasks.length) {
      const names = Object.keys(toFix).filter((k) => toFix[k].length).map((k) => DOC_LABEL[k]).join(", ");
      setPhase(current, "in_corso", `Revisione: ${names}`);
      await pool(tasks, ui.parallel);
    }
    setPhase(current, "completata", s().check.detail + (tasks.length ? " · revisione completata" : ""));

    current = "pdf";
    if (!s().final) {
      setPhase(current, "in_corso", "Impaginazione e verifica dei PDF");
      s().final = await step("finalize", { usage: s().usage });
    }
    setPhase(current, "completata", s().final.detail);
    renderResults();
  } catch (err) {
    run.error = { phase: current, message: err.message, retryable: err.retryable !== false };
    setPhase(current, "errore", err.message);
  } finally {
    ui.running = false;
  }
}

// --- Avanzamento ------------------------------------------------------------------
function progressPercent() {
  const total = PHASES.reduce((a, [, , w]) => a + w, 0);
  let done = 0;
  for (const [key, , weight] of PHASES) {
    const p = run.phases[key];
    if (["completata", "saltata", "non_riuscita"].includes(p.status)) done += weight;
    else if (p.total) done += weight * Math.min(p.done / p.total, 0.95);
  }
  return Math.round((100 * done) / total);
}

function renderProgress() {
  const pct = progressPercent();
  $("#progressBar").style.width = `${pct}%`;
  $("#progressPct").textContent = `${pct}%`;
  $("#phaseList").innerHTML = PHASES.map(([key, label]) => {
    const p = run.phases[key];
    return `<li class="ph-${p.status}"><span class="ph-icon">${STATUS_ICON[p.status] ?? ""}</span>
      <div><div class="ph-label">${escapeHtml(label)}</div>${p.detail ? `<div class="ph-detail">${escapeHtml(p.detail)}</div>` : ""}</div></li>`;
  }).join("");
  const warnings = (run.state && run.state.warnings) || [];
  const finalWarnings = (run.state && run.state.final && run.state.final.warnings) || [];
  $("#warningList").innerHTML = [...warnings, ...finalWarnings].map((w) => `<li>${escapeHtml(w)}</li>`).join("");
  const done = run.state && run.state.final;
  if (run.error) {
    $("#progressTitle").textContent = "Elaborazione interrotta";
    $("#progressStep").className = "step";
    $("#progressStep").textContent = "!";
    const label = PHASES.find(([k]) => k === run.error.phase)[1];
    $("#errorBox").hidden = false;
    $("#errorTitle").textContent = `Errore nella fase «${label}»`;
    $("#errorMsg").textContent = run.error.message;
    $("#retryBtn").hidden = !run.error.retryable;
  } else if (done) {
    $("#progressTitle").textContent = "Elaborazione completata";
    $("#progressStep").className = "step step-done";
    $("#progressStep").textContent = "✓";
    $("#progressNote").textContent = "";
  } else {
    $("#errorBox").hidden = true;
    $("#progressTitle").textContent = "Elaborazione in corso";
    $("#progressStep").className = "step step-live";
    $("#progressStep").textContent = "•";
  }
}

$("#retryBtn").addEventListener("click", () => execute());

window.addEventListener("beforeunload", (e) => {
  if (ui.running || (run && run.state && !run.state.final)) {
    e.preventDefault();
    e.returnValue = "";
  }
});

// --- Risultati e download ------------------------------------------------------------
function b64ToBytes(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) crc = CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

// ZIP minimale (file memorizzati senza compressione: i PDF sono già compressi).
function makeZip(entries) {
  const encoder = new TextEncoder();
  const chunks = [];
  const central = [];
  let offset = 0;
  for (const { name, data } of entries) {
    const nameBytes = encoder.encode(name);
    const crc = crc32(data);
    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true); local.setUint16(4, 20, true); local.setUint16(6, 0x0800, true);
    local.setUint16(8, 0, true); local.setUint16(10, 0, true); local.setUint16(12, 0x21, true);
    local.setUint32(14, crc, true); local.setUint32(18, data.length, true); local.setUint32(22, data.length, true);
    local.setUint16(26, nameBytes.length, true); local.setUint16(28, 0, true);
    chunks.push(new Uint8Array(local.buffer), nameBytes, data);
    const entry = new DataView(new ArrayBuffer(46));
    entry.setUint32(0, 0x02014b50, true); entry.setUint16(4, 20, true); entry.setUint16(6, 20, true);
    entry.setUint16(8, 0x0800, true); entry.setUint16(10, 0, true); entry.setUint16(12, 0, true); entry.setUint16(14, 0x21, true);
    entry.setUint32(16, crc, true); entry.setUint32(20, data.length, true); entry.setUint32(24, data.length, true);
    entry.setUint16(28, nameBytes.length, true); entry.setUint32(42, offset, true);
    central.push(new Uint8Array(entry.buffer), nameBytes);
    offset += 30 + nameBytes.length + data.length;
  }
  const centralSize = central.reduce((a, c) => a + c.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true); end.setUint16(8, entries.length, true); end.setUint16(10, entries.length, true);
  end.setUint32(12, centralSize, true); end.setUint32(16, offset, true);
  return new Blob([...chunks, ...central, new Uint8Array(end.buffer)], { type: "application/zip" });
}

function objectUrl(blob) {
  const url = URL.createObjectURL(blob);
  ui.urls.push(url);
  return url;
}

function renderResults() {
  const final = run.state.final;
  $("#resultCard").hidden = false;
  const pdfs = final.files.map((f) => ({ ...f, bytes: b64ToBytes(f.b64) }));
  $("#downloads").innerHTML = pdfs.map((f) => `
    <a class="dl" href="${objectUrl(new Blob([f.bytes], { type: "application/pdf" }))}" download="${escapeHtml(f.filename)}">
      <span class="dl-icon">PDF</span>
      <div><b>${escapeHtml(f.label)}</b><br><span>${escapeHtml(f.filename)}</span><br><em>${f.pages} pagine · Scarica</em></div>
    </a>`).join("");
  const report = new TextEncoder().encode(final.report.text);
  const zip = makeZip([...pdfs.map((f) => ({ name: f.filename, data: f.bytes })), { name: final.report.filename, data: report }]);
  $("#zipLink").href = objectUrl(zip);
  $("#zipLink").setAttribute("download", final.zip_name);
  $("#reportLink").href = objectUrl(new Blob([report], { type: "text/plain;charset=utf-8" }));
  $("#reportLink").setAttribute("download", final.report.filename);

  const parts = [];
  const cov = final.coverage || {};
  parts.push(`<h3>Copertura della lezione</h3><dl class="kv">
    <dt>Argomenti inventariati</dt><dd>${cov.argomenti_inventario ?? "–"}</dd>
    <dt>Argomenti non coperti</dt><dd>${cov.argomenti_non_coperti ?? "–"}</dd>
    <dt>Passaggi citati</dt><dd>${cov.passaggi_citati_nella_lezione ?? "–"} di ${cov.passaggi_trascrizione ?? "–"}</dd>
  </dl>`);
  if (final.issues.length) {
    parts.push("<h3>Punti da verificare</h3><ul>" + final.issues.map((i) =>
      `<li><span class="sev sev-${i.gravita}">${i.gravita}</span><b>${escapeHtml(DOC_LABEL[i.documento] || i.documento)}</b> · ${escapeHtml(i.problema)}</li>`).join("") + "</ul>");
  } else {
    parts.push(`<h3>Punti da verificare</h3><p class="small">Nessun problema rilevante dopo la revisione.${final.issues_low ? ` ${final.issues_low} segnalazioni minori nel report.` : ""}</p>`);
  }
  const u = run.state.usage || {};
  const tokensIn = (u.token_input || 0) + (u.token_cache_scrittura || 0) + (u.token_cache_lettura || 0);
  parts.push(`<h3>Utilizzo API</h3><p class="small">Modello ${escapeHtml(final.model || "")} · ${u.chiamate || 0} chiamate · ${tokensIn.toLocaleString("it-IT")} token in ingresso, ${(u.token_output || 0).toLocaleString("it-IT")} in uscita${final.cost_usd != null ? ` · costo stimato circa ${final.cost_usd.toLocaleString("it-IT")} USD` : ""}.</p>`);
  $("#checksContent").innerHTML = parts.join("");
  renderProgress();
}

function resetUi() {
  ui.urls.forEach((url) => URL.revokeObjectURL(url));
  ui.urls = [];
  run = null;
  $("#jobForm").hidden = false;
  $("#progressCard").hidden = true;
  $("#resultCard").hidden = true;
  $("#errorBox").hidden = true;
  $("#progressNote").textContent = "Non chiudere né ricaricare la pagina durante l'elaborazione.";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

$("#newBtn").addEventListener("click", resetUi);
$("#restartBtn").addEventListener("click", resetUi);
$("#deleteBtn").addEventListener("click", () => {
  if (!confirm("Cancellare da questa pagina la trascrizione e i documenti generati? Scarica prima i file che ti servono.")) return;
  resetUi();
});
$("#logoutBtn").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" }).catch(() => {});
  window.location.href = "/login";
});

loadConfig();

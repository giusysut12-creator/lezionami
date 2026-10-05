"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { jobId: null, timer: null, sources: [], tab: "file", maxMb: 30 };

const STATUS_ICON = { completata: "✓", errore: "!", saltata: "–", non_riuscita: "!", in_corso: "", attesa: "" };

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    const cfg = await res.json();
    state.maxMb = cfg.max_upload_mb;
    document.querySelectorAll(".maxmb").forEach((el) => (el.textContent = cfg.max_upload_mb));
    $("#ttl").textContent = cfg.job_ttl_minutes;
    $("#keyBanner").hidden = cfg.api_key_configured;
    $("#modelChip").textContent = cfg.model;
    $("#modelChip").hidden = false;
  } catch (e) {
    /* il server risponderà con errori espliciti all'invio */
  }
}

// --- Tab file / testo --------------------------------------------------------
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.tab = btn.dataset.tab;
    document.querySelectorAll(".tab").forEach((b) => {
      b.classList.toggle("active", b === btn);
      b.setAttribute("aria-selected", b === btn ? "true" : "false");
    });
    $("#panel-file").hidden = state.tab !== "file";
    $("#panel-text").hidden = state.tab !== "text";
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
  const n = e.target.value.length;
  $("#charCount").textContent = `${n.toLocaleString("it-IT")} caratteri`;
});

// --- Fonti aggiuntive ----------------------------------------------------------
function renderSources() {
  const list = $("#sourceList");
  list.innerHTML = "";
  state.sources.forEach((file, index) => {
    const li = document.createElement("li");
    li.innerHTML = `<span>${escapeHtml(file.name)} <span class="muted">· ${(file.size / 1024).toFixed(0)} KB</span></span>`;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Rimuovi";
    btn.addEventListener("click", () => { state.sources.splice(index, 1); renderSources(); });
    li.appendChild(btn);
    list.appendChild(li);
  });
}
$("#sourceFiles").addEventListener("change", (e) => {
  for (const file of e.target.files) state.sources.push(file);
  e.target.value = "";
  renderSources();
});

// --- Invio --------------------------------------------------------------------
function formError(msg) {
  $("#formError").textContent = msg;
  $("#formError").hidden = !msg;
}

$("#jobForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  formError("");
  const form = new FormData();
  const limit = state.maxMb * 1024 * 1024;
  if (state.tab === "file") {
    const file = fileInput.files[0];
    if (!file) return formError("Seleziona un file con la trascrizione, oppure scegli «Incolla testo».");
    if (!/\.(txt|docx|pdf|md|srt|vtt)$/i.test(file.name)) return formError("Formato non supportato: usa TXT, DOCX o PDF testuale.");
    if (file.size > limit) return formError(`Il file supera ${state.maxMb} MB.`);
    form.append("transcript_file", file);
  } else {
    const text = $("#transcriptText").value.trim();
    if (text.length < 20) return formError("Incolla il testo della trascrizione.");
    form.append("transcript_text", text);
  }
  for (const name of ["title", "lesson_date", "recipient"]) {
    form.append(name, $(`[name=${name}]`).value.trim());
  }
  form.append("web_search", $("#webSearch").checked ? "true" : "false");
  for (const file of state.sources) {
    if (file.size > limit) return formError(`Il documento «${file.name}» supera ${state.maxMb} MB.`);
    form.append("sources", file);
  }

  $("#submitBtn").disabled = true;
  $("#submitBtn").textContent = "Invio in corso…";
  try {
    const res = await fetch("/api/jobs", { method: "POST", body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Invio non riuscito.");
    state.jobId = data.job_id;
    $("#jobForm").hidden = true;
    $("#progressCard").hidden = false;
    $("#resultCard").hidden = true;
    window.scrollTo({ top: 0, behavior: "smooth" });
    poll();
  } catch (err) {
    formError(err.message === "Failed to fetch" ? "Il server locale non risponde: verifica che l'applicazione sia avviata." : err.message);
  } finally {
    $("#submitBtn").disabled = false;
    $("#submitBtn").textContent = "Genera i tre PDF";
  }
});

// --- Avanzamento --------------------------------------------------------------
async function poll() {
  clearTimeout(state.timer);
  if (!state.jobId) return;
  try {
    const res = await fetch(`/api/jobs/${state.jobId}`);
    const job = await res.json();
    if (!res.ok) throw new Error(job.detail || "Elaborazione non trovata.");
    renderJob(job);
    if (job.status === "in_corso" || job.status === "in_attesa") state.timer = setTimeout(poll, 1500);
  } catch (err) {
    showError("Collegamento interrotto", err.message === "Failed to fetch" ? "Il server locale non risponde. Riavvia l'applicazione." : err.message, false);
  }
}

function renderJob(job) {
  $("#progressBar").style.width = `${job.progress}%`;
  $("#progressPct").textContent = `${job.progress}%`;
  const list = $("#phaseList");
  list.innerHTML = job.phases.map((p) => `
    <li class="ph-${p.status}">
      <span class="ph-icon">${STATUS_ICON[p.status] ?? ""}</span>
      <div><div class="ph-label">${escapeHtml(p.label)}</div>${p.detail ? `<div class="ph-detail">${escapeHtml(p.detail)}</div>` : ""}</div>
    </li>`).join("");
  $("#warningList").innerHTML = (job.warnings || []).map((w) => `<li>${escapeHtml(w)}</li>`).join("");

  if (job.status === "errore") {
    $("#progressTitle").textContent = "Elaborazione interrotta";
    $("#progressStep").className = "step";
    $("#progressStep").textContent = "!";
    showError(`Errore nella fase «${job.error.label}»`, job.error.message, job.error.retryable);
  } else {
    $("#errorBox").hidden = true;
    $("#progressStep").className = "step step-live";
    $("#progressStep").textContent = "•";
    $("#progressTitle").textContent = "Elaborazione in corso";
  }
  if (job.status === "completato") {
    $("#progressTitle").textContent = "Elaborazione completata";
    $("#progressStep").className = "step step-done";
    $("#progressStep").textContent = "✓";
    $("#progressNote").textContent = "";
    renderResults(job);
  }
}

function showError(title, message, retryable) {
  $("#errorBox").hidden = false;
  $("#errorTitle").textContent = title;
  $("#errorMsg").textContent = message;
  $("#retryBtn").hidden = !retryable;
}

$("#retryBtn").addEventListener("click", async () => {
  $("#retryBtn").disabled = true;
  try {
    const res = await fetch(`/api/jobs/${state.jobId}/retry`, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Impossibile riprovare.");
    $("#errorBox").hidden = true;
    poll();
  } catch (err) {
    $("#errorMsg").textContent = err.message;
  } finally {
    $("#retryBtn").disabled = false;
  }
});

// --- Risultati ----------------------------------------------------------------
function renderResults(job) {
  const card = $("#resultCard");
  card.hidden = false;
  const base = `/api/jobs/${job.id}/files`;
  $("#downloads").innerHTML = job.files.map((f) => `
    <a class="dl" href="${base}/${f.kind}" download="${escapeHtml(f.filename)}">
      <span class="dl-icon">PDF</span>
      <div><b>${escapeHtml(f.label)}</b><br><span>${escapeHtml(f.filename)}</span><br><em>${f.pages} pagine · Scarica</em></div>
    </a>`).join("");
  $("#zipLink").href = `${base}/zip`;
  $("#zipLink").setAttribute("download", job.zip_name);
  $("#reportLink").href = `${base}/report`;

  const parts = [];
  const cov = job.coverage || {};
  parts.push(`<h3>Copertura della lezione</h3><dl class="kv">
    <dt>Argomenti inventariati</dt><dd>${cov.argomenti_inventario ?? "–"}</dd>
    <dt>Argomenti non coperti</dt><dd>${cov.argomenti_non_coperti ?? "–"}</dd>
    <dt>Passaggi citati</dt><dd>${cov.passaggi_citati_nella_lezione ?? "–"} di ${cov.passaggi_trascrizione ?? "–"}</dd>
  </dl>`);
  if (job.web) {
    const w = job.web;
    let html = `<h3>Verifica web</h3>`;
    if (w.stato === "eseguita") {
      html += `<p class="small">Eseguita il ${escapeHtml(w.consultato_il)} · ${w.verifiche} condizioni controllate.</p><ul>` +
        w.fonti.map((s) => `<li><a href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(s.titolo || s.url)}</a>${s.data_pagina ? ` <span class="muted">(${escapeHtml(s.data_pagina)})</span>` : ""}</li>`).join("") + `</ul>`;
    } else {
      html += `<p class="small">Non riuscita${w.messaggio ? `: ${escapeHtml(w.messaggio)}` : ""}. Nessuna condizione è dichiarata verificata nei documenti.</p>`;
    }
    parts.push(html);
  }
  if (job.issues.length) {
    parts.push(`<h3>Punti da verificare</h3><ul>` + job.issues.map((i) =>
      `<li><span class="sev sev-${i.gravita}">${i.gravita}</span><b>${escapeHtml(labelOf(i.documento))}</b> · ${escapeHtml(i.problema)}</li>`).join("") + `</ul>`);
  } else {
    parts.push(`<h3>Punti da verificare</h3><p class="small">Nessun problema rilevante dopo la revisione.${job.issues_low ? ` ${job.issues_low} segnalazioni minori nel report.` : ""}</p>`);
  }
  const u = job.usage || {};
  parts.push(`<h3>Utilizzo API</h3><p class="small">Modello ${escapeHtml(u.modello)} · ${u.chiamate} chiamate · ${(u.token_input + u.token_cache_scrittura + u.token_cache_lettura).toLocaleString("it-IT")} token in ingresso, ${u.token_output.toLocaleString("it-IT")} in uscita${u.costo_stimato_usd != null ? ` · costo stimato circa ${u.costo_stimato_usd.toLocaleString("it-IT")} USD` : ""}.</p>`);
  $("#checksContent").innerHTML = parts.join("");
}

function labelOf(doc) {
  return { lezione: "Lezione completa", guida: "Guida di studio", brochure: "Brochure cliente" }[doc] || doc;
}

function resetUi() {
  clearTimeout(state.timer);
  state.jobId = null;
  $("#jobForm").hidden = false;
  $("#progressCard").hidden = true;
  $("#resultCard").hidden = true;
  $("#errorBox").hidden = true;
  $("#progressNote").textContent = "Le lezioni lunghe possono richiedere diversi minuti.";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

$("#newBtn").addEventListener("click", resetUi);
$("#restartBtn").addEventListener("click", async () => {
  if (state.jobId) await fetch(`/api/jobs/${state.jobId}`, { method: "DELETE" }).catch(() => {});
  resetUi();
});
$("#deleteBtn").addEventListener("click", async () => {
  if (!state.jobId) return;
  if (!confirm("Eliminare dal server la trascrizione e i documenti generati? Scarica prima i file che ti servono.")) return;
  const res = await fetch(`/api/jobs/${state.jobId}`, { method: "DELETE" });
  if (res.ok) {
    resetUi();
    formError("");
    alert("Dati eliminati dal server.");
  }
});

loadConfig();

/* Flashcard AI - logica di interfaccia.
   Vanilla JS, nessuna dipendenza esterna. */

"use strict";

const KEY_STORE = "flashcard.apiKey";
const MODEL_STORE = "flashcard.model";
const MAX_STORE = "flashcard.maxCards";

const TYPE_LABELS = {
  cloze: "Cloze",
  definition: "Definizione",
  section: "Sezione",
  summary: "Riassunto",
};

const SOURCE_LABELS = {
  pdf_page: "Pagina",
  docx_section: "Sezione",
  docx_table: "Tabella",
  pptx_slide: "Slide",
};

const state = {
  decks: [],
  deckId: null,
  documentId: null,
  preview: [],
  card: null,
  flipped: false,
  typeFilter: "all",
  listMode: false,
  serverKey: document.body.dataset.serverKey === "1",
  maxUploadMb: 25,
};

const $ = (id) => document.getElementById(id);

const el = {
  dropOverlay: $("dropOverlay"),
  dropzone: $("dropzone"),
  fileInput: $("fileInput"),
  dropMaxMb: $("dropMaxMb"),
  aiBadge: $("aiBadge"),
  aiBadgeText: $("aiBadgeText"),
  settingsBtn: $("settingsBtn"),
  settingsPanel: $("settingsPanel"),
  apiKeyInput: $("apiKeyInput"),
  modelInput: $("modelInput"),
  maxCardsInput: $("maxCardsInput"),
  saveKeyBtn: $("saveKeyBtn"),
  clearKeyBtn: $("clearKeyBtn"),
  storageNote: $("storageNote"),
  uploadPanel: $("uploadPanel"),
  reviewPanel: $("reviewPanel"),
  reviewMeta: $("reviewMeta"),
  typeFilters: $("typeFilters"),
  previewList: $("previewList"),
  regenBtn: $("regenBtn"),
  selectAllBtn: $("selectAllBtn"),
  selectNoneBtn: $("selectNoneBtn"),
  saveDeckBtn: $("saveDeckBtn"),
  deckList: $("deckList"),
  docList: $("docList"),
  refreshDecksBtn: $("refreshDecksBtn"),
  studyEmpty: $("studyEmpty"),
  studyView: $("studyView"),
  studyDeckName: $("studyDeckName"),
  studyProgress: $("studyProgress"),
  statsRow: $("statsRow"),
  flashcard: $("flashcard"),
  frontText: $("frontText"),
  frontTag: $("frontTag"),
  backText: $("backText"),
  backTag: $("backTag"),
  backHint: $("backHint"),
  studyActions: $("studyActions"),
  cardActions: $("cardActions"),
  explainBtn: $("explainBtn"),
  explainBox: $("explainBox"),
  srcBadge: $("srcBadge"),
  listModeBtn: $("listModeBtn"),
  listView: $("listView"),
  cardList: $("cardList"),
  toast: $("toast"),
  busy: $("busy"),
  busyText: $("busyText"),
};

/* ---------- utilita ---------- */

let toastTimer = null;

function toast(message, kind = "") {
  el.toast.textContent = message;
  el.toast.className = "toast" + (kind ? " " + kind : "");
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.toast.hidden = true;
  }, kind === "err" ? 6000 : 3200);
}

function busy(on, text = "Lavoro...") {
  if (on) {
    el.busyText.textContent = text;
    el.busy.hidden = false;
  } else {
    el.busy.hidden = true;
  }
}

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return (bytes / 1024 ** index).toFixed(index ? 1 : 0) + " " + units[index];
}

function fmtInterval(days) {
  const minutes = days * 1440;
  if (minutes < 60) return `${Math.max(1, Math.round(minutes))} min`;
  if (minutes < 1440) return `${Math.round(minutes / 60)} h`;
  if (minutes < 43200) return `${Math.round(minutes / 1440)} g`;
  return `${Math.round(minutes / 43200)} mesi`;
}

function apiKey() {
  return localStorage.getItem(KEY_STORE) || "";
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers);
  const key = apiKey();
  if (key) headers["X-Gemini-Key"] = key;
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(path, Object.assign({}, options, { headers }));

  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }

  if (!response.ok) {
    throw new Error(payload.error || `Errore ${response.status}`);
  }
  return payload;
}

/* ---------- stato AI ---------- */

function refreshAiBadge() {
  const key = apiKey();
  const on = Boolean(key || state.serverKey);
  el.aiBadge.className = "ai-badge " + (on ? "on" : "off");
  el.aiBadgeText.textContent = on
    ? key ? "Gemini attivo" : "Gemini dal server"
    : "Solo generatore locale";
  el.aiBadge.title = on
    ? "Le card usano Gemini e puoi chiedere spiegazioni."
    : "Nessuna chiave API: le card sono generate in locale e il bottone spiega mostra la fonte.";
}

function currentModel() {
  return el.modelInput.value.trim();
}

/* ---------- impostazioni ---------- */

function loadSettings() {
  el.apiKeyInput.value = apiKey();
  el.modelInput.value = localStorage.getItem(MODEL_STORE) || "gemini-2.5-flash";
  el.maxCardsInput.value = localStorage.getItem(MAX_STORE) || "40";
  refreshAiBadge();
}

function saveSettings() {
  const key = el.apiKeyInput.value.trim();
  if (key) localStorage.setItem(KEY_STORE, key);
  else localStorage.removeItem(KEY_STORE);

  localStorage.setItem(MODEL_STORE, el.modelInput.value.trim());
  localStorage.setItem(MAX_STORE, el.maxCardsInput.value.trim() || "40");
  refreshAiBadge();
}

function toggleSettings(force) {
  const open = force ?? el.settingsPanel.hidden;
  el.settingsPanel.hidden = !open;
  el.settingsBtn.setAttribute("aria-expanded", String(open));
}

/* ---------- upload e drag & drop ---------- */

async function handleFiles(fileList) {
  const file = fileList && fileList[0];
  if (!file) return;

  const ext = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  if (![".pdf", ".docx", ".pptx"].includes(ext)) {
    toast("Formato non supportato: carica PDF, DOCX o PPTX.", "err");
    return;
  }
  if (file.size > state.maxUploadMb * 1024 * 1024) {
    toast(`Il file supera il limite di ${state.maxUploadMb} MB.`, "err");
    return;
  }

  const form = new FormData();
  form.append("file", file);

  busy(true, "Leggo il documento...");
  try {
    const result = await api("/api/documents", { method: "POST", body: form });
    toast(`Letto: ${result.document.word_count} parole.`, "ok");
    await generateCards(result.document.id);
    // il documento resta nella lista anche senza card salvate, cosi' si
    // puo' rigenerare o cancellare in un secondo momento
    await loadDocuments();
  } catch (error) {
    toast(error.message, "err");
  } finally {
    busy(false);
    el.fileInput.value = "";
  }
}

/* ---------- documenti caricati ---------- */

async function loadDocuments() {
  try {
    const result = await api("/api/documents");
    renderDocuments(result.documents || []);
  } catch {
    /* la lista documenti e' accessoria: un errore qui non blocca lo studio */
  }
}

function renderDocuments(documents) {
  if (!documents.length) {
    el.docList.innerHTML = `<li class="empty-decks">Nessun documento caricato.</li>`;
    return;
  }

  el.docList.innerHTML = documents
    .map(
      (doc) => `
      <li class="doc-item" data-doc="${esc(doc.id)}">
        <div class="deck-info">
          <div class="deck-name">${esc(doc.filename)}</div>
          <div class="deck-meta">${doc.word_count} parole &middot; ${fmtBytes(doc.size_bytes)}</div>
        </div>
        <button class="deck-del" data-docdel="${esc(doc.id)}" title="Elimina documento"
                aria-label="Elimina documento">&times;</button>
      </li>`
    )
    .join("");
}

function setupDragAndDrop() {
  // il contatore serve perche' dragenter e dragleave scattano anche quando il
  // puntatore passa sui figli: senza questo l'overlay lampeggerebbe
  let depth = 0;

  const show = () => {
    depth = 0;
    el.dropOverlay.classList.add("active");
  };
  const hide = () => {
    depth = 0;
    el.dropOverlay.classList.remove("active");
  };

  window.addEventListener("dragenter", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth += 1;
    show();
  });

  window.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });

  window.addEventListener("dragleave", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth -= 1;
    if (depth <= 0) hide();
  });

  window.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    hide();
    handleFiles(event.dataTransfer.files);
  });

  el.dropzone.addEventListener("click", () => el.fileInput.click());
  el.dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      el.fileInput.click();
    }
  });
  el.fileInput.addEventListener("change", (event) => handleFiles(event.target.files));
}

function hasFiles(event) {
  const types = event.dataTransfer && event.dataTransfer.types;
  return Boolean(types && Array.prototype.includes.call(types, "Files"));
}

/* ---------- generazione e revisione ---------- */

async function generateCards(documentId) {
  busy(true, apiKey() || state.serverKey ? "Gemini sta creando le card..." : "Creo le card in locale...");

  const query = currentModel() ? `?model=${encodeURIComponent(currentModel())}` : "";
  try {
    const result = await api(`/api/documents/${documentId}/cards${query}`, {
      method: "POST",
      body: JSON.stringify({ max_cards: Number(el.maxCardsInput.value) || 40 }),
    });

    state.documentId = documentId;
    state.preview = (result.cards || []).map((card, index) => ({
      key: `c${index}`,
      card,
      kept: true,
    }));

    if (!state.preview.length) {
      toast("Il documento non ha prodotto card. Prova con un testo piu' lungo.", "err");
      el.reviewPanel.hidden = true;
      return;
    }

    renderReview(result.generator);
    el.reviewPanel.hidden = false;
    el.reviewPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    toast(error.message, "err");
  } finally {
    busy(false);
  }
}

function renderReview(generator) {
  const label = generator === "gemini" ? "generate con Gemini" : "generate in locale";
  el.reviewMeta.textContent = `${state.preview.length} card ${label}`;
  renderTypeFilters();
  renderPreviewList();
}

function renderTypeFilters() {
  const counts = state.preview.reduce((acc, item) => {
    const type = item.card.card_type || "definition";
    acc[type] = (acc[type] || 0) + 1;
    return acc;
  }, {});

  const options = [["all", state.preview.length]];
  for (const [type, count] of Object.entries(counts)) {
    options.push([type, count]);
  }

  el.typeFilters.innerHTML = options
    .map(
      ([type, count]) =>
        `<button class="chip${state.typeFilter === type ? " active" : ""}" data-type="${esc(type)}">${
          type === "all" ? "Tutte" : TYPE_LABELS[type] || type
        } (${count})</button>`
    )
    .join("");
}

function renderPreviewList() {
  const visible = state.preview.filter(
    (item) => state.typeFilter === "all" || (item.card.card_type || "definition") === state.typeFilter
  );

  if (!visible.length) {
    el.previewList.innerHTML = `<li class="empty-decks">Nessuna card di questo tipo.</li>`;
    return;
  }

  el.previewList.innerHTML = visible
    .map((item) => {
      const card = item.card;
      const type = card.card_type || "definition";
      return `
      <li class="preview-item${item.kept ? " checked" : " discarded"}" data-key="${item.key}">
        <input type="checkbox" ${item.kept ? "checked" : ""}
               aria-label="Tieni questa card" data-toggle="${item.key}">
        <div class="preview-body">
          <p class="preview-q">${esc(card.question)}</p>
          <p class="preview-a">${esc(card.answer)}</p>
          <span class="tag">${esc(TYPE_LABELS[type] || type)}</span>
        </div>
      </li>`;
    })
    .join("");
}

function toggleCard(key, kept) {
  const item = state.preview.find((entry) => entry.key === key);
  if (item) item.kept = kept;
  renderPreviewList();
}

async function saveDeck() {
  const chosen = state.preview.filter((item) => item.kept).map((item) => item.card);
  if (!chosen.length) {
    toast("Seleziona almeno una card.", "err");
    return;
  }

  busy(true, "Salvo il mazzo...");
  try {
    const result = await api(`/api/documents/${state.documentId}/decks`, {
      method: "POST",
      body: JSON.stringify({ cards: chosen }),
    });
    toast(`Mazzo salvato: ${result.card_count} card.`, "ok");
    el.reviewPanel.hidden = true;
    state.preview = [];
    await loadDecks();
    openDeck(result.deck.id);
  } catch (error) {
    toast(error.message, "err");
  } finally {
    busy(false);
  }
}

/* ---------- mazzi ---------- */

async function loadDecks() {
  try {
    const result = await api("/api/decks");
    state.decks = result.decks || [];
    renderDecks();
  } catch (error) {
    toast(error.message, "err");
  }
}

function renderDecks() {
  if (!state.decks.length) {
    el.deckList.innerHTML = `<li class="empty-decks">Nessun mazzo. Carica un documento.</li>`;
    return;
  }

  el.deckList.innerHTML = state.decks
    .map((deck) => {
      const name = deck.source_filename || deck.name;
      const due = deck.due_now || 0;
      return `
      <li class="deck-item${deck.id === state.deckId ? " active" : ""}" data-deck="${esc(deck.id)}"
          tabindex="0" role="button">
        <div class="deck-info">
          <div class="deck-name">${esc(name)}</div>
          <div class="deck-meta">${deck.card_count} card</div>
        </div>
        <span class="due-pill${due ? " ready" : ""}">${due ? due + " da ripassare" : "in giorno"}</span>
        <button class="deck-del" data-del="${esc(deck.id)}" title="Elimina mazzo" aria-label="Elimina mazzo">&times;</button>
      </li>`;
    })
    .join("");
}

async function openDeck(deckId) {
  state.deckId = deckId;
  state.listMode = false;
  el.listView.hidden = true;
  el.studyView.hidden = false;
  el.studyEmpty.hidden = true;
  renderDecks();

  try {
    const result = await api(`/api/decks/${deckId}`);
    el.studyDeckName.textContent = result.deck.name || "Mazzo";
    renderStats(result.stats);
    await loadCardList();
    await nextCard();
  } catch (error) {
    toast(error.message, "err");
  }
}

function renderStats(stats) {
  const cells = [
    ["Card", stats.total_cards, ""],
    ["Da ripassare", stats.due_now, stats.due_now ? "warn" : ""],
    ["Ripassate", stats.reviews, ""],
    ["Corrette", stats.accuracy + "%", stats.accuracy >= 70 ? "good" : stats.accuracy ? "warn" : ""],
  ];

  el.statsRow.innerHTML = cells
    .map(
      ([label, value, cls]) => `
      <div class="stat">
        <div class="stat-label">${label}</div>
        <div class="stat-value ${cls}">${esc(value)}</div>
      </div>`
    )
    .join("");
}

async function loadCardList() {
  try {
    const result = await api(`/api/decks/${state.deckId}/cards`);
    const cards = result.cards || [];
    if (!cards.length) {
      el.cardList.innerHTML = `<li class="empty-decks">Questo mazzo e' vuoto.</li>`;
      return;
    }

    el.cardList.innerHTML = cards
      .map((card) => {
        const state_ =
          card.repetitions === 0 ? "weak" : card.lapses >= 2 ? "weak" : card.repetitions >= 2 ? "known" : "";
        const label = state_ === "known" ? "Conosciuta" : state_ === "weak" ? "Da ripassare" : "In corso";
        return `
        <li class="list-card">
          <div class="list-card-body">
            <p class="list-q">${esc(card.question)}</p>
            <p class="list-a">${esc(card.answer)}</p>
            <div class="list-meta">
              <span class="chip-rep ${state_}">${label}</span>
              <span>${TYPE_LABELS[card.card_type] || card.card_type}</span>
              ${card.source_page ? `<span>${SOURCE_LABELS[card.source_kind] || "Fonte"} ${card.source_page}</span>` : ""}
              <span>${fmtInterval(card.interval_days)}</span>
              <button class="deck-del" data-delcard="${esc(card.id)}" title="Elimina card" aria-label="Elimina card">&times;</button>
            </div>
          </div>
        </li>`;
      })
      .join("");
  } catch (error) {
    toast(error.message, "err");
  }
}

/* ---------- studio ---------- */

async function nextCard() {
  hideExplain();
  state.flipped = false;
  el.flashcard.classList.remove("flipped");
  el.studyActions.hidden = true;
  el.cardActions.hidden = true;

  try {
    const result = await api(`/api/decks/${state.deckId}/next`);
    if (!result.card) {
      state.card = null;
      el.frontText.textContent = "Deck svuotato";
      el.backText.textContent = "Nessuna card da ripassare. Vedi l'elenco completo per rivedere quelle che conosci gia'.";
      el.frontTag.textContent = "Tutto fatto";
      el.backTag.textContent = "Prossima revisione";
      el.backHint.textContent = "";
      el.srcBadge.textContent = "";
      el.studyProgress.textContent = "Nessuna card in scadenza";
      return;
    }

    state.card = result.card;
    const card = result.card;
    el.frontText.textContent = card.question;
    el.backText.textContent = card.answer;
    el.frontTag.textContent = TYPE_LABELS[card.card_type] || "Domanda";
    el.backTag.textContent = card.answer.length > 220 ? "Risposta" : "Risposta";
    el.backHint.textContent = card.source_page
      ? `${SOURCE_LABELS[card.source_kind] || "Fonte"} ${card.source_page}`
      : "";

    el.srcBadge.textContent = card.source_page
      ? `fonte: ${SOURCE_LABELS[card.source_kind] || "documento"} ${card.source_page}`
      : "";

    el.studyProgress.textContent = `${card.repetitions} ripassi, prossima tra ${fmtInterval(card.interval_days)}`;
  } catch (error) {
    toast(error.message, "err");
  }
}

function flip() {
  if (!state.card) return;
  state.flipped = !state.flipped;
  el.flashcard.classList.toggle("flipped", state.flipped);
  el.studyActions.hidden = !state.flipped;
  if (state.flipped) {
    el.cardActions.hidden = false;
  } else {
    el.studyActions.hidden = true;
  }
}

async function grade(value) {
  if (!state.card) return;
  const cardId = state.card.id;

  try {
    const result = await api(`/api/cards/${cardId}/review`, {
      method: "POST",
      body: JSON.stringify({ grade: value }),
    });
    // la risposta dello studente serve alla spiegazione AI
    state.lastGrade = value;
    state.lastAnswer = state.card.answer;
    renderStats(result.stats);
    await nextCard();
  } catch (error) {
    toast(error.message, "err");
  }
}

async function explain() {
  if (!state.card) return;
  el.explainBtn.disabled = true;
  el.explainBox.hidden = false;
  el.explainBox.className = "explain-box";
  el.explainBox.innerHTML = `<p class="explain-body">Sto preparando la spiegazione...</p>`;

  try {
    const result = await api(`/api/cards/${state.card.id}/explain`, {
      method: "POST",
      body: JSON.stringify({ user_answer: state.lastAnswer || "" }),
    });
    renderExplain(result);
  } catch (error) {
    el.explainBox.className = "explain-box error";
    el.explainBox.innerHTML = `<p class="explain-body">${esc(error.message)}</p>`;
  } finally {
    el.explainBtn.disabled = false;
  }
}

function renderExplain(result) {
  const glossary = (result.glossary || [])
    .map((entry) => `<li><b>${esc(entry.term)}</b> — ${esc(entry.definition)}</li>`)
    .join("");

  const sourceBlock = result.source_text
    ? `<div class="explain-title"><span>${esc(result.source_label || "Fonte")} ${
        result.source_page ? result.source_page : ""
      }</span></div>
       <div class="source-text">${esc(result.source_text)}</div>`
    : "";

  if (result.mode === "ai") {
    const esitoClass =
      (result.esito || "").toLowerCase().includes("errata") ? "error" : "";
    el.explainBox.className = "explain-box " + esitoClass;
    el.explainBox.innerHTML = `
      <div class="explain-title"><span>Spiegazione AI</span><span>${esc(result.model || "")}</span></div>
      <p class="explain-body">${esc(result.spiegazione || "")}</p>
      ${result.concetto_chiave ? `<div class="explain-key">${esc(result.concetto_chiave)}</div>` : ""}
      ${result.suggerimento ? `<p class="explain-tip">${esc(result.suggerimento)}</p>` : ""}
      ${glossary ? `<ul class="glossary">${glossary}</ul>` : ""}
      ${sourceBlock}`;
  } else {
    el.explainBox.className = "explain-box source";
    el.explainBox.innerHTML = `
      <div class="explain-title"><span>Fonte</span></div>
      <p class="explain-body">${esc(result.message || "")}</p>
      ${sourceBlock}
      ${glossary ? `<ul class="glossary">${glossary}</ul>` : ""}`;
  }
}

function hideExplain() {
  el.explainBox.hidden = true;
  el.explainBox.innerHTML = "";
}

function toggleListMode() {
  state.listMode = !state.listMode;
  el.listView.hidden = !state.listMode;
  el.studyView.hidden = state.listMode;
  el.listModeBtn.textContent = state.listMode ? "Torna allo studio" : "Tutte le card";
  if (state.listMode) loadCardList();
}

/* ---------- eventi ---------- */

function bindEvents() {
  el.settingsBtn.addEventListener("click", () => toggleSettings());
  el.saveKeyBtn.addEventListener("click", () => {
    saveSettings();
    toast(apiKey() ? "Chiave salvata nel browser." : "Chiave rimossa.", "ok");
    toggleSettings(false);
  });
  el.clearKeyBtn.addEventListener("click", () => {
    el.apiKeyInput.value = "";
    saveSettings();
    toast("Chiave rimossa dal browser.");
  });

  document.addEventListener("click", (event) => {
    if (event.target === el.apiKeyInput || el.settingsPanel.contains(event.target)) return;
    if (el.settingsBtn.contains(event.target)) return;
    if (!el.settingsPanel.hidden) toggleSettings(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !el.settingsPanel.hidden) toggleSettings(false);
  });

  el.flashcard.addEventListener("click", flip);
  el.flashcard.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      flip();
    }
  });

  el.rightBtn.addEventListener("click", () => grade("giusta"));
  el.wrongBtn.addEventListener("click", () => grade("sbagliata"));
  el.againBtn.addEventListener("click", () => grade("ancora"));
  el.explainBtn.addEventListener("click", explain);
  el.listModeBtn.addEventListener("click", toggleListMode);
  el.refreshDecksBtn.addEventListener("click", () => {
  loadDecks();
  loadDocuments();
});

  el.typeFilters.addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip) return;
    state.typeFilter = chip.dataset.type;
    renderTypeFilters();
    renderPreviewList();
  });

  el.previewList.addEventListener("click", (event) => {
    const checkbox = event.target.closest("[data-toggle]");
    if (checkbox) {
      toggleCard(checkbox.dataset.toggle, checkbox.checked);
    }
  });

  el.selectAllBtn.addEventListener("click", () => {
    state.preview.forEach((item) => {
      item.kept = true;
    });
    renderPreviewList();
  });

  el.selectNoneBtn.addEventListener("click", () => {
    state.preview.forEach((item) => {
      item.kept = false;
    });
    renderPreviewList();
  });

  el.regenBtn.addEventListener("click", () => {
    if (state.documentId) generateCards(state.documentId);
  });

  el.saveDeckBtn.addEventListener("click", saveDeck);

  el.deckList.addEventListener("click", async (event) => {
    const del = event.target.closest("[data-del]");
    if (del) {
      event.stopPropagation();
      const id = del.dataset.del;
      const deck = state.decks.find((d) => d.id === id);
      if (!confirm(`Eliminare il mazzo "${deck ? deck.source_filename || deck.name : ""}"?`)) return;
      try {
        await api(`/api/decks/${id}`, { method: "DELETE" });
        if (state.deckId === id) {
          state.deckId = null;
          state.card = null;
          el.studyView.hidden = true;
          el.studyEmpty.hidden = false;
        }
        toast("Mazzo eliminato.");
        await loadDecks();
      } catch (error) {
        toast(error.message, "err");
      }
      return;
    }

    const item = event.target.closest("[data-deck]");
    if (item) openDeck(item.dataset.deck);
  });

  el.deckList.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const item = event.target.closest("[data-deck]");
    if (item) {
      event.preventDefault();
      openDeck(item.dataset.deck);
    }
  });

  el.cardList.addEventListener("click", async (event) => {
    const del = event.target.closest("[data-delcard]");
    if (!del) return;
    try {
      await api(`/api/cards/${del.dataset.delcard}`, { method: "DELETE" });
      toast("Card eliminata.");
      await loadCardList();
    } catch (error) {
      toast(error.message, "err");
    }
  });

  el.docList.addEventListener("click", async (event) => {
    const del = event.target.closest("[data-docdel]");
    if (!del) {
      return;
    }
    if (!confirm("Eliminare il documento e tutti i suoi mazzi?")) return;
    try {
      await api(`/api/documents/${del.dataset.docdel}`, { method: "DELETE" });
      toast("Documento eliminato.");
      await Promise.all([loadDocuments(), loadDecks()]);
    } catch (error) {
      toast(error.message, "err");
    }
  });
}

/* ---------- avvio ---------- */

async function init() {
  loadSettings();
  bindEvents();
  setupDragAndDrop();

  try {
    const health = await api("/api/health");
    state.serverKey = health.ai_available && !apiKey();
    state.maxUploadMb = health.max_upload_mb || 25;
    el.dropMaxMb.textContent = state.maxUploadMb;
    el.storageNote.textContent =
      "I documenti e i progressi sono salvati sul server in data/. Su molti hosting gratuiti " +
      "quel disco viene azzerato a ogni deploy: se usi una piattaforma del genere, scarica " +
      "i tuoi documenti prima di aspettarti che restino.";
  } catch {
    /* se il health check fallisce l'app funziona lo stesso */
  }

  refreshAiBadge();
  await Promise.all([loadDecks(), loadDocuments()]);
}

init();
"use strict";

// --- Internationalization -------------------------------------------------

const MONTHS = {
  de: ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
       "September", "Oktober", "November", "Dezember"],
  en: ["January", "February", "March", "April", "May", "June", "July", "August",
       "September", "October", "November", "December"],
};

const I18N = {
  de: {
    income: "Einnahmen",
    expenses: "Ausgaben",
    balance: "Saldo",
    analysis: "Analyse",
    byCategory: "Ausgaben nach Kategorie",
    trendTitle: "Verlauf (Einnahmen / Ausgaben)",
    importTitle: "Transaktionen importieren",
    importBtn: "CSV importieren",
    budgetsTitle: "Kategorie-Budgets",
    monthlyLimit: "Monatslimit €",
    set: "Setzen",
    spendingTitle: "Ausgaben",
    delete: "Löschen",
    edit: "Bearbeiten",
    save: "Speichern",
    cancel: "Abbrechen",
    unclassified: "Nicht kategorisiert",
    transfers: "Buchungen",
    noExpenses: "Noch keine Ausgaben in diesem Zeitraum.",
    noBudgets: "Keine Budgets festgelegt.",
    overBudget: "über Budget",
    importing: "Wird importiert ...",
    importResult: "{imported} importiert, {skipped} Duplikat(e) übersprungen (von {parsed} erkannt).",
    classifying: "Kategorisiere Ausgaben mit KI … ({count} übrig)",
    unclassifiedNote: "{count} Ausgabe(n) noch nicht kategorisiert (läuft Ollama?).",
    spendingLabel: "Ausgaben (€)",
    txForCategory: "Transaktionen – {category}",
    close: "Schließen",
    fullYear: "Ganzes Jahr",
    yearToDate: "Bisher dieses Jahr",
    noIncome: "Noch keine Einnahmen in diesem Zeitraum.",
    chatTitle: "Finanz-Chat",
    chatSubtitle: "Fragen zu deinen Finanzen – lokal von Ollama beantwortet.",
    chatPlaceholder: "Frage zu deinen Finanzen …",
    chatSend: "Senden",
    chatEmpty: "Stelle eine Frage zu deinen Ausgaben – oder bitte um eine Korrektur, z. B. „Buche die REWE-Zahlung vom 3. auf den 5. Juni um“.",
    chatThinking: "Denkt nach …",
    chatError: "Ollama ist nicht erreichbar. Läuft der lokale Server?",
    chatProposed: "Vorgeschlagene Änderung:",
    chatUpdateAction: "Transaktion ändern",
    chatDeleteAction: "Transaktion löschen",
    chatApply: "Übernehmen",
    chatApplied: "Übernommen ✓",
    chatApplyError: "Konnte nicht übernommen werden.",
    fieldDate: "Datum",
    fieldAmount: "Betrag",
    fieldDescription: "Bezeichnung",
    fieldCategory: "Kategorie",
  },
  en: {
    income: "Income",
    expenses: "Expenses",
    balance: "Balance",
    analysis: "Analysis",
    byCategory: "Spending by category",
    trendTitle: "Trend (income / expenses)",
    importTitle: "Import transactions",
    importBtn: "Import CSV",
    budgetsTitle: "Category budgets",
    monthlyLimit: "Monthly limit €",
    set: "Set",
    spendingTitle: "Spending",
    delete: "Delete",
    edit: "Edit",
    save: "Save",
    cancel: "Cancel",
    unclassified: "Unclassified",
    transfers: "transfers",
    noExpenses: "No expenses in this period yet.",
    noBudgets: "No budgets set.",
    overBudget: "over budget",
    importing: "Importing ...",
    importResult: "Imported {imported}, skipped {skipped} duplicate(s) (of {parsed} parsed).",
    classifying: "Categorizing spending with AI … ({count} left)",
    unclassifiedNote: "{count} expense(s) not categorized yet (is Ollama running?).",
    spendingLabel: "Spending (€)",
    txForCategory: "Transactions – {category}",
    close: "Close",
    fullYear: "Full year",
    yearToDate: "Year to date",
    noIncome: "No income in this period yet.",
    chatTitle: "Finance chat",
    chatSubtitle: "Ask about your finances – answered locally by Ollama.",
    chatPlaceholder: "Ask about your finances …",
    chatSend: "Send",
    chatEmpty: "Ask a question about your spending – or request a fix, e.g. “move the REWE payment from the 3rd to the 5th of June”.",
    chatThinking: "Thinking …",
    chatError: "Ollama is unreachable. Is the local server running?",
    chatProposed: "Proposed change:",
    chatUpdateAction: "Edit transaction",
    chatDeleteAction: "Delete transaction",
    chatApply: "Apply",
    chatApplied: "Applied ✓",
    chatApplyError: "Could not apply.",
    fieldDate: "Date",
    fieldAmount: "Amount",
    fieldDescription: "Label",
    fieldCategory: "Category",
  },
};

let lang = localStorage.getItem("lang") || "de";

function t(key, params = {}) {
  let str = (I18N[lang] && I18N[lang][key]) || I18N.en[key] || key;
  for (const [k, v] of Object.entries(params)) {
    str = str.replace(`{${k}}`, v);
  }
  return str;
}

const euro = new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR" });
const el = (id) => document.getElementById(id);
const monthSelect = el("month-select");
const yearSelect = el("year-select");

// Cache of the latest fetched data so charts can re-render on language change.
let lastTrend = null;
let lastCategories = null;

// Latest month (1–12) that has data, per year ("YYYY" -> month number). Drives
// the date picker's whole-period choice: a year whose data reaches December is
// offered as a "full year", one that stops earlier as "year to date".
let yearLatestMonth = new Map();

// Background-classification polling state. Classification runs server-side and
// can take minutes per period on a slow local model; we re-fetch the summary
// until everything is categorized, and stop only when the server reports the
// classifier actually failed (Ollama unreachable).
let classifyPollTimer = null;
const CLASSIFY_POLL_MS = 6000;

function applyTranslations() {
  document.documentElement.lang = lang;
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  });
  document.querySelectorAll("#lang-switch button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.lang === lang);
  });
  populateMonthOptions();
}

function setLang(next) {
  lang = next;
  localStorage.setItem("lang", lang);
  applyTranslations();
  refreshAll();
  renderChat();
  if (lastTrend) renderTrendChart(lastTrend);
  if (lastCategories) renderCategoryChart(lastCategories);
}

// --- Date picker ----------------------------------------------------------

// The single whole-period choice offered for the selected year: a full
// calendar year once its data reaches December, otherwise year-to-date through
// the latest month present. Years with no data yet fall back to year-to-date.
function wholePeriodOption() {
  const year = yearSelect.value || String(new Date().getFullYear());
  if (yearLatestMonth.get(year) === 12) return ["year", "fullYear"];
  return ["ytd", "yearToDate"];
}

function populateMonthOptions() {
  const selected = monthSelect.value;
  monthSelect.innerHTML = "";
  // The whole-period choice precedes the individual months and reflects the
  // selected year's data coverage (full year vs. year to date).
  const [wpValue, wpKey] = wholePeriodOption();
  const wpOpt = document.createElement("option");
  wpOpt.value = wpValue;
  wpOpt.textContent = t(wpKey);
  monthSelect.appendChild(wpOpt);
  MONTHS[lang].forEach((name, i) => {
    const opt = document.createElement("option");
    opt.value = String(i + 1).padStart(2, "0");
    opt.textContent = name;
    monthSelect.appendChild(opt);
  });
  // Restore the prior selection, mapping a previous whole-period choice onto
  // whichever one ("year"/"ytd") this year now offers so the view is kept.
  if (selected === "ytd" || selected === "year") {
    monthSelect.value = wpValue;
  } else if (selected) {
    monthSelect.value = selected;
  }
}

// Refresh the per-year data coverage that drives the whole-period choice.
async function loadDateCoverage() {
  try {
    const { months } = await api("/api/months");
    yearLatestMonth = new Map();
    for (const m of months) {
      const year = m.slice(0, 4);
      const month = parseInt(m.slice(5, 7), 10);
      if (month > (yearLatestMonth.get(year) || 0)) yearLatestMonth.set(year, month);
    }
  } catch (_) {
    /* leave coverage empty; the picker falls back to year-to-date */
  }
}

function populateYearOptions() {
  const now = new Date().getFullYear();
  // Show every year from the current one back to the earliest that has data, so
  // empty years never clutter the picker. Falls back to the current year alone
  // when no coverage has loaded yet (setMonthYear still adds any missing year).
  const dataYears = [...yearLatestMonth.keys()].map(Number);
  const earliest = dataYears.length ? Math.min(now, ...dataYears) : now;
  yearSelect.innerHTML = "";
  for (let y = now; y >= earliest; y--) {
    const opt = document.createElement("option");
    opt.value = String(y);
    opt.textContent = String(y);
    yearSelect.appendChild(opt);
  }
}

function setMonthYear(iso) {
  // iso is "YYYY-MM"
  const [year, month] = iso.split("-");
  if (![...yearSelect.options].some((o) => o.value === year)) {
    const opt = document.createElement("option");
    opt.value = year;
    opt.textContent = year;
    yearSelect.appendChild(opt);
  }
  yearSelect.value = year;
  monthSelect.value = month;
}

// The period passed to the API: "YYYY-MM" for a month, "YYYY" for a full year,
// or "YYYY-ytd" for the year up to today.
function currentPeriod() {
  const year = yearSelect.value || String(new Date().getFullYear());
  const month = monthSelect.value;
  if (month === "year") return year;
  if (month === "ytd") return `${year}-ytd`;
  return `${year}-${month}`;
}

// Human-readable label for the selected period, e.g. "März 2025", "2025" or
// "Bisher dieses Jahr 2025", used in headings that follow the date picker.
function periodLabel() {
  const year = yearSelect.value || String(new Date().getFullYear());
  const month = monthSelect.value;
  if (month === "year") return year;
  if (month === "ytd") return `${t("yearToDate")} ${year}`;
  return `${MONTHS[lang][parseInt(month, 10) - 1]} ${year}`;
}

// --- API ------------------------------------------------------------------

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Request failed (${res.status})`);
  }
  return res.status === 204 ? null : res.json();
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// --- Rendering ------------------------------------------------------------

async function loadSummary(isPoll = false) {
  // A fresh (non-poll) load starts a new classification watch.
  if (classifyPollTimer) {
    clearTimeout(classifyPollTimer);
    classifyPollTimer = null;
  }

  const period = currentPeriod();
  const summary = await api(`/api/summary?period=${period}`);
  el("card-income").textContent = euro.format(summary.income);
  el("card-expense").textContent = euro.format(summary.expense);
  el("card-balance").textContent = euro.format(summary.balance);
  // Heading follows the date picker instead of a fixed "this month".
  el("spending-title").textContent = `${t("spendingTitle")} – ${periodLabel()}`;

  // Categories from the summary are the single source for both the breakdown
  // list and the chart; chart wants an `amount` field, so derive it here.
  lastCategories = summary.categories.map((c) => ({
    category: c.category,
    amount: c.spent,
    transactions: c.transactions || [],
  }));
  if (lastCategories.length) {
    renderCategoryChart(lastCategories);
  }

  updateClassifyStatus(summary.unclassified_count, summary.classifier, period, isPoll);

  const breakdown = el("category-breakdown");
  breakdown.innerHTML = "";
  if (!summary.categories.length) {
    breakdown.innerHTML = `<li class="meta">${t("noExpenses")}</li>`;
  }
  for (const cat of summary.categories) {
    const li = document.createElement("li");
    // Each row drills into its transactions on click, so it reads as a button.
    li.className =
      "flex cursor-pointer flex-col items-stretch border-b border-line py-2 text-[0.9rem] hover:bg-panel-soft";
    li.addEventListener("click", () => showCategoryDetail(cat));
    const pct = cat.limit ? Math.min(100, (cat.spent / cat.limit) * 100) : 0;
    const over = cat.limit !== null && cat.spent > cat.limit;
    const limitText = cat.limit !== null
      ? ` / ${euro.format(cat.limit)}${over ? ` (${t("overBudget")})` : ""}`
      : "";
    li.innerHTML = `
      <div class="flex items-baseline justify-between gap-3">
        <span class="min-w-0 truncate">${escapeHtml(cat.category)}</span>
        <span class="flex-none tabular-nums">${euro.format(cat.spent)}${limitText}</span>
      </div>
      ${cat.limit !== null ? `<div class="mt-1.5 h-1.5 rounded-[3px] ${over ? "bg-expense" : "bg-accent"}" style="width:${pct}%"></div>` : ""}
    `;
    breakdown.appendChild(li);
  }

  renderIncomeBreakdown(summary.income_groups || []);
}

// The smaller side column listing the period's income, aggregated per sender.
// A sender with several transfers shows one summed line that expands into its
// individual transactions; a lone transfer shows as a plain line.
function renderIncomeBreakdown(groups) {
  const list = el("income-breakdown");
  list.innerHTML = "";
  if (!groups.length) {
    list.innerHTML = `<li class="meta">${t("noIncome")}</li>`;
    return;
  }
  for (const group of groups) {
    const li = document.createElement("li");
    li.className = "border-b border-line py-2 text-[0.85rem]";
    if (group.count > 1) {
      renderIncomeGroup(li, group);
    } else {
      li.appendChild(incomeRow(group.transactions[0]));
    }
    list.appendChild(li);
  }
}

// One income transaction line (description + date, amount on the right).
function incomeRow(tx) {
  const row = document.createElement("div");
  row.className = "flex items-baseline justify-between gap-3";
  row.innerHTML = `
    <span class="flex min-w-0 flex-1 flex-col">
      <span class="truncate" title="${escapeHtml(tx.description || "")}">${escapeHtml(tx.description || "—")}</span>
      <span class="text-xs text-muted">${escapeHtml(tx.date)}</span>
    </span>
    <span class="flex-none tabular-nums text-income">${euro.format(tx.amount)}</span>`;
  return row;
}

// A collapsed sender total that toggles its individual transfers on click.
function renderIncomeGroup(li, group) {
  li.innerHTML = `
    <div class="flex cursor-pointer items-baseline justify-between gap-3 hover:opacity-90">
      <span class="flex min-w-0 flex-1 items-baseline gap-1.5">
        <span class="tx-caret flex-none text-muted">▸</span>
        <span class="flex min-w-0 flex-col">
          <span class="truncate" title="${escapeHtml(group.sender)}">${escapeHtml(group.sender)}</span>
          <span class="text-xs text-muted">${group.count} ${t("transfers")}</span>
        </span>
      </span>
      <span class="flex-none tabular-nums text-income">${euro.format(group.total)}</span>
    </div>
    <ul class="tx-sublist m-0 mt-2 hidden list-none border-l border-line pl-3"></ul>`;
  const header = li.querySelector("div");
  const caret = li.querySelector(".tx-caret");
  const sublist = li.querySelector(".tx-sublist");
  for (const tx of group.transactions) {
    const sli = document.createElement("li");
    sli.className = "py-1";
    sli.appendChild(incomeRow(tx));
    sublist.appendChild(sli);
  }
  header.addEventListener("click", () => {
    const expanded = !sublist.classList.toggle("hidden");
    caret.textContent = expanded ? "▾" : "▸";
  });
}

// Drive the "classifying…" indicator and the background-classification poll.
// While work remains we re-fetch the summary so categories fill in live. We
// keep polling through slow batches (which can take minutes) and stop only when
// the server reports the classifier failed with none running — i.e. Ollama is
// unreachable. The failed flag is ignored on a fresh load so a just-scheduled
// pass always gets a chance.
function updateClassifyStatus(remaining, classifier, period, isPoll) {
  const status = el("classify-status");
  if (!remaining) {
    status.textContent = "";
    return;
  }

  if (isPoll && classifier && classifier.failed && !classifier.running) {
    status.textContent = t("unclassifiedNote", { count: remaining });
    return; // stop polling; Ollama appears unreachable
  }

  status.textContent = t("classifying", { count: remaining });
  classifyPollTimer = setTimeout(() => {
    if (currentPeriod() === period) loadSummary(true);
  }, CLASSIFY_POLL_MS);
}

async function loadBudgets() {
  const budgets = await api("/api/budgets");
  const list = el("budget-list");
  list.innerHTML = "";
  if (!budgets.length) {
    list.innerHTML = `<li class="meta">${t("noBudgets")}</li>`;
  }
  for (const b of budgets) {
    const li = document.createElement("li");
    li.className = "flex justify-between gap-2 border-b border-line py-2 text-[0.9rem]";
    li.innerHTML = `<span>${escapeHtml(b.category)}</span>
      <span>${euro.format(b.monthly_limit)}
      <button class="del cursor-pointer border-0 bg-transparent px-1.5 text-muted hover:text-expense" data-category="${escapeHtml(b.category)}">${t("delete")}</button></span>`;
    list.appendChild(li);
  }
}

// The canonical spending categories, cached so the per-transaction edit form
// can offer the same vocabulary as budgets and AI classification.
let standardCategories = [];

// Populate the budget category dropdown from the canonical category list so
// budgets always use the same vocabulary as the AI classification.
async function loadCategories() {
  const { categories } = await api("/api/categories");
  standardCategories = categories;
  const select = el("budget-category");
  select.innerHTML = "";
  for (const c of categories) {
    const opt = document.createElement("option");
    opt.value = c;
    opt.textContent = c;
    select.appendChild(opt);
  }
}

// The trend follows the selected period (see the backend ``/api/trend``), so it
// is re-fetched whenever the month/year changes rather than fixed on today.
async function loadTrend() {
  const { trend } = await api(`/api/trend?period=${currentPeriod()}`);
  lastTrend = trend;
  renderTrendChart(trend);
}

async function refreshAll() {
  await Promise.all([loadSummary(), loadBudgets(), loadTrend()]);
}

// --- Charts ---------------------------------------------------------------

const PALETTE = [
  "#38bdf8", "#f87171", "#4ade80", "#fbbf24", "#a78bfa", "#fb7185",
  "#34d399", "#f472b6", "#60a5fa", "#facc15", "#c084fc", "#2dd4bf", "#94a3b8",
];

let trendChart = null;
let categoryChart = null;
// Signature of the data currently drawn in the category chart. The background
// classification poll re-fetches the summary every few seconds, but the
// category totals usually haven't changed between ticks. Without this guard
// each poll would destroy and rebuild the chart, replaying the bar animation
// (and closing any open detail panel) for no reason — the "random reload" the
// chart appeared to do. We only redraw when the data or language differs.
let categorySig = null;

const eurAxis = (value) => `${value} €`;
const gridColor = "rgba(148, 163, 184, 0.15)";
const tickColor = "#94a3b8";

function renderTrendChart(trend) {
  const ctx = el("trend-chart");
  if (trendChart) trendChart.destroy();
  trendChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: trend.map((d) => d.month),
      datasets: [
        {
          label: t("income"),
          data: trend.map((d) => d.income),
          borderColor: "#4ade80",
          backgroundColor: "rgba(74, 222, 128, 0.15)",
          tension: 0.3,
          fill: true,
        },
        {
          label: t("expenses"),
          data: trend.map((d) => d.expense),
          borderColor: "#f87171",
          backgroundColor: "rgba(248, 113, 113, 0.15)",
          tension: 0.3,
          fill: true,
        },
      ],
    },
    options: {
      responsive: true,
      plugins: { legend: { labels: { color: tickColor } } },
      scales: {
        x: { ticks: { color: tickColor }, grid: { color: gridColor } },
        y: { ticks: { color: tickColor, callback: eurAxis }, grid: { color: gridColor } },
      },
    },
  });
}

function renderCategoryChart(categories) {
  const sig = JSON.stringify([
    lang,
    categories.map((c) => [c.category, c.amount]),
  ]);
  // Nothing changed since the last draw (a poll tick) — leave the existing
  // chart and any open detail panel exactly as they are.
  if (categoryChart && sig === categorySig) return;
  categorySig = sig;

  const ctx = el("category-chart");
  if (categoryChart) categoryChart.destroy();
  hideCategoryDetail();
  categoryChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels: categories.map((c) => c.category),
      datasets: [
        {
          label: t("spendingLabel"),
          data: categories.map((c) => c.amount),
          backgroundColor: categories.map((_, i) => PALETTE[i % PALETTE.length]),
        },
      ],
    },
    options: {
      responsive: true,
      onClick: (_event, elements) => {
        if (elements.length) showCategoryDetail(categories[elements[0].index]);
      },
      onHover: (event, elements) => {
        event.native.target.style.cursor = elements.length ? "pointer" : "default";
      },
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: tickColor }, grid: { color: gridColor } },
        y: { ticks: { color: tickColor, callback: eurAxis }, grid: { color: gridColor } },
      },
    },
  });
}

// The category whose detail panel is open, so an edit/delete can re-fetch the
// summary and then re-render the same panel with fresh data.
let openCategoryName = null;

function hideCategoryDetail() {
  el("category-detail").classList.add("hidden");
  openCategoryName = null;
}

function showCategoryDetail(category) {
  openCategoryName = category.category;
  const panel = el("category-detail");
  el("category-detail-title").textContent =
    t("txForCategory", { category: category.category });
  const list = el("category-detail-list");
  list.innerHTML = "";
  const transactions = category.transactions || [];
  if (!transactions.length) {
    list.innerHTML = `<li class="meta">${t("noExpenses")}</li>`;
  }
  for (const tx of transactions) {
    const li = document.createElement("li");
    li.className = "border-b border-line py-1.5 text-[0.85rem]";
    renderTxRow(li, tx, category.category);
    list.appendChild(li);
  }
  panel.classList.remove("hidden");
  // The panel lives up in the Analyse section, but the click may come from the
  // breakdown list further down, so bring it into view.
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// A single read-only transaction row with Edit / Delete actions.
function renderTxRow(li, tx, categoryName) {
  li.innerHTML = `
    <div class="flex items-center justify-between gap-3">
      <span class="min-w-0 flex-1 truncate">
        <span class="text-muted">${escapeHtml(tx.date)}</span>
        ${escapeHtml(tx.description || "—")}
      </span>
      <span class="flex flex-none items-center gap-2">
        <span class="tabular-nums">${euro.format(tx.amount)}</span>
        <button type="button" class="tx-edit cursor-pointer border-0 bg-transparent px-1 text-muted hover:text-accent">${t("edit")}</button>
        <button type="button" class="tx-del cursor-pointer border-0 bg-transparent px-1 text-muted hover:text-expense">${t("delete")}</button>
      </span>
    </div>`;
  li.querySelector(".tx-edit").addEventListener("click",
    () => renderTxEditRow(li, tx, categoryName));
  li.querySelector(".tx-del").addEventListener("click",
    () => deleteTx(tx.id, categoryName));
}

// The same row turned into an inline date / description / category / amount form.
function renderTxEditRow(li, tx, categoryName) {
  // Preselect the row's current bucket; "" is the Unclassified option, chosen
  // when the bucket isn't a canonical category so an amount-only edit doesn't
  // accidentally classify the row.
  const current = standardCategories.includes(categoryName) ? categoryName : "";
  const options = [`<option value=""${current === "" ? " selected" : ""}>${t("unclassified")}</option>`]
    .concat(standardCategories.map((c) =>
      `<option value="${escapeHtml(c)}"${c === current ? " selected" : ""}>${escapeHtml(c)}</option>`))
    .join("");
  li.innerHTML = `
    <form class="flex flex-wrap items-center gap-2">
      <input type="date" class="field flex-none px-1.5 py-1 text-xs" value="${escapeHtml(tx.date)}" required />
      <input type="text" class="field min-w-0 flex-1 px-1.5 py-1 text-xs" value="${escapeHtml(tx.description || "")}" maxlength="200" />
      <select class="tx-cat field flex-none px-1.5 py-1 text-xs">${options}</select>
      <input type="number" class="field w-24 flex-none px-1.5 py-1 text-xs" value="${tx.amount}" min="0" step="0.01" required />
      <button type="submit" class="btn flex-none px-2 py-1 text-xs">${t("save")}</button>
      <button type="button" class="tx-cancel flex-none cursor-pointer border-0 bg-transparent px-1 text-xs text-muted hover:text-ink">${t("cancel")}</button>
    </form>`;
  const form = li.querySelector("form");
  const dateIn = form.querySelector('input[type="date"]');
  const descIn = form.querySelector('input[type="text"]');
  const amountIn = form.querySelector('input[type="number"]');
  const catIn = form.querySelector(".tx-cat");
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    saveTx(tx.id, {
      date: dateIn.value,
      description: descIn.value.trim(),
      amount: parseFloat(amountIn.value),
      category: catIn.value,
    }, categoryName);
  });
  form.querySelector(".tx-cancel").addEventListener("click",
    () => renderTxRow(li, tx, categoryName));
}

async function saveTx(id, body, categoryName) {
  await api(`/api/transactions/${id}`, { method: "PUT", body: JSON.stringify(body) });
  await loadSummary();
  reopenCategoryDetail(categoryName);
}

async function deleteTx(id, categoryName) {
  await api(`/api/transactions/${id}`, { method: "DELETE" });
  await loadSummary();
  reopenCategoryDetail(categoryName);
}

// Re-open the detail panel for a category after the summary was re-fetched, so
// edits/deletes show fresh data. If the category no longer has any rows (its
// last transaction was deleted) the panel just closes.
function reopenCategoryDetail(name) {
  const cat = (lastCategories || []).find((c) => c.category === name);
  if (cat) showCategoryDetail(cat);
  else hideCategoryDetail();
}

// --- Finance chat ---------------------------------------------------------

// The running conversation. Each entry is {role, content} plus, for assistant
// turns that propose fixes, an `actions` array; a transient "thinking" bubble
// carries `pending: true` and is swapped out for the real reply on arrival.
const chatMessages = [];

// Which change key maps to which translated field label / snapshot field.
const CHAT_FIELDS = {
  date: "fieldDate",
  amount: "fieldAmount",
  description: "fieldDescription",
  category: "fieldCategory",
};

function formatChatField(key, value) {
  if (key === "amount") return euro.format(Number(value) || 0);
  if (key === "category") return value ? value : t("unclassified");
  return value === undefined || value === null || value === "" ? "—" : String(value);
}

function renderChat() {
  const box = el("chat-messages");
  box.innerHTML = "";
  if (!chatMessages.length) {
    box.innerHTML = `<p class="meta">${escapeHtml(t("chatEmpty"))}</p>`;
    return;
  }
  for (const msg of chatMessages) box.appendChild(renderChatMessage(msg));
  box.scrollTop = box.scrollHeight;
}

function renderChatMessage(msg) {
  const isUser = msg.role === "user";
  const wrap = document.createElement("div");
  wrap.className = `flex flex-col ${isUser ? "items-end" : "items-start"}`;

  const bubble = document.createElement("div");
  bubble.className = isUser
    ? "max-w-[85%] whitespace-pre-wrap rounded-lg bg-accent px-3 py-2 text-sm text-[#06283d]"
    : `max-w-[85%] whitespace-pre-wrap rounded-lg bg-panel-soft px-3 py-2 text-sm ${msg.pending ? "text-muted" : "text-ink"}`;
  bubble.textContent = msg.content;
  wrap.appendChild(bubble);

  for (const action of msg.actions || []) {
    wrap.appendChild(renderActionCard(action));
  }
  return wrap;
}

// A proposed fix rendered as a confirmable card: what it touches, the concrete
// before → after changes, and an Apply button that only then writes to the DB.
function renderActionCard(action) {
  const card = document.createElement("div");
  card.className = "mt-2 w-[85%] rounded-lg border border-line bg-panel px-3 py-2 text-sm";
  const cur = action.current || {};
  const title = action.type === "delete" ? t("chatDeleteAction") : t("chatUpdateAction");

  let body = `<div class="text-muted mb-1">${escapeHtml(cur.description || "—")} · ${escapeHtml(cur.date || "")} · ${euro.format(cur.amount || 0)}</div>`;
  if (action.type === "update") {
    body += Object.entries(action.changes || {}).map(([key, value]) =>
      `<div><span class="text-muted">${escapeHtml(t(CHAT_FIELDS[key] || key))}:</span> `
      + `${escapeHtml(formatChatField(key, cur[key]))} → `
      + `<span class="text-ink">${escapeHtml(formatChatField(key, value))}</span></div>`
    ).join("");
  }

  card.innerHTML = `<div class="mb-1 font-semibold">${escapeHtml(title)}</div>${body}`;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn mt-2 px-2 py-1 text-xs disabled:cursor-default disabled:opacity-60";
  btn.textContent = action.applied ? t("chatApplied") : t("chatApply");
  btn.disabled = !!action.applied;
  btn.addEventListener("click", () => applyChatAction(action, btn, card));
  card.appendChild(btn);
  return card;
}

async function applyChatAction(action, btn, card) {
  btn.disabled = true;
  card.querySelector(".chat-apply-error")?.remove();
  try {
    await api("/api/chat/apply", {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    action.applied = true;
    btn.textContent = t("chatApplied");
    // The ledger changed, so refresh the cards, charts and breakdowns.
    await refreshAll();
  } catch (_) {
    btn.disabled = false;
    const err = document.createElement("div");
    err.className = "chat-apply-error mt-1 text-xs text-expense";
    err.textContent = t("chatApplyError");
    card.appendChild(err);
  }
}

async function sendChat(text) {
  chatMessages.push({ role: "user", content: text });
  const thinking = { role: "assistant", content: t("chatThinking"), pending: true };
  chatMessages.push(thinking);
  renderChat();

  try {
    const payload = {
      messages: chatMessages
        .filter((m) => !m.pending)
        .map((m) => ({ role: m.role, content: m.content })),
      period: currentPeriod(),
    };
    const res = await api("/api/chat", { method: "POST", body: JSON.stringify(payload) });
    const reply = (res.reply || "").trim()
      || (res.actions && res.actions.length ? t("chatProposed") : "…");
    replaceMessage(thinking, { role: "assistant", content: reply, actions: res.actions || [] });
  } catch (_) {
    replaceMessage(thinking, { role: "assistant", content: t("chatError") });
  }
  renderChat();
}

function replaceMessage(target, next) {
  const idx = chatMessages.indexOf(target);
  if (idx !== -1) chatMessages.splice(idx, 1, next);
  else chatMessages.push(next);
}

// --- Events ---------------------------------------------------------------

el("lang-switch").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-lang]");
  if (btn && btn.dataset.lang !== lang) setLang(btn.dataset.lang);
});

el("budget-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await api("/api/budgets", {
    method: "POST",
    body: JSON.stringify({
      category: el("budget-category").value.trim(),
      monthly_limit: parseFloat(el("budget-limit").value),
    }),
  });
  e.target.reset();
  await refreshAll();
});

document.addEventListener("click", async (e) => {
  const budgetBtn = e.target.closest(".budget-list button.del");
  if (budgetBtn) {
    await api(`/api/budgets/${encodeURIComponent(budgetBtn.dataset.category)}`, { method: "DELETE" });
    await refreshAll();
    return;
  }
});

el("import-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fileInput = el("import-file");
  const status = el("import-status");
  if (!fileInput.files.length) return;
  status.textContent = t("importing");
  try {
    const formData = new FormData();
    formData.append("file", fileInput.files[0]);
    const res = await fetch("/api/import/csv", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Import failed");
    status.textContent = t("importResult", data);
    e.target.reset();
    // Imported rows may extend coverage (a new earliest year, or a year that
    // now reaches December), so refresh coverage and rebuild the year picker
    // and whole-period choice before jumping to the freshly imported data.
    await loadDateCoverage();
    populateYearOptions();
    // Jump to the latest month so the freshly imported data is visible.
    try {
      const { month } = await api("/api/latest-month");
      if (month) setMonthYear(month);
    } catch (_) { /* ignore */ }
    populateMonthOptions();
    await refreshAll();
  } catch (err) {
    status.textContent = err.message;
  }
});

el("category-detail-close").addEventListener("click", hideCategoryDetail);

el("chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = el("chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  input.style.height = "auto";
  sendChat(text);
});

// Enter sends, Shift+Enter inserts a newline; the box grows with its content.
el("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    el("chat-form").requestSubmit();
  }
});
el("chat-input").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = `${Math.min(e.target.scrollHeight, 160)}px`;
});

function onMonthYearChange() {
  // loadSummary classifies (if needed), renders the chart and breakdown;
  // loadTrend re-anchors the trend line on the newly selected period.
  loadSummary();
  loadTrend();
}

monthSelect.addEventListener("change", onMonthYearChange);
yearSelect.addEventListener("change", () => {
  // The whole-period choice depends on the year's coverage, so rebuild the
  // month options before reloading the data for the new selection.
  populateMonthOptions();
  onMonthYearChange();
});

// --- Init -----------------------------------------------------------------

async function init() {
  applyTranslations();
  loadCategories();
  // Coverage drives which years have data (the year picker) and the full-year
  // vs. year-to-date choice (the month options), so load it before both.
  await loadDateCoverage();
  populateYearOptions();
  setMonthYear(new Date().toISOString().slice(0, 7));
  try {
    const { month } = await api("/api/latest-month");
    if (month) setMonthYear(month);
  } catch (_) {
    /* fall back to the current month */
  }
  populateMonthOptions();
  refreshAll();
  renderChat();
}

init();

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
    importTitle: "Aus Postbank importieren",
    importBtn: "CSV importieren",
    budgetsTitle: "Kategorie-Budgets",
    monthlyLimit: "Monatslimit €",
    set: "Setzen",
    spendingTitle: "Ausgaben",
    delete: "Löschen",
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
  },
  en: {
    income: "Income",
    expenses: "Expenses",
    balance: "Balance",
    analysis: "Analysis",
    byCategory: "Spending by category",
    trendTitle: "Trend (income / expenses)",
    importTitle: "Import from Postbank",
    importBtn: "Import CSV",
    budgetsTitle: "Category budgets",
    monthlyLimit: "Monthly limit €",
    set: "Set",
    spendingTitle: "Spending",
    delete: "Delete",
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
  if (lastTrend) renderTrendChart(lastTrend);
  if (lastCategories) renderCategoryChart(lastCategories);
}

// --- Date picker ----------------------------------------------------------

function populateMonthOptions() {
  const selected = monthSelect.value;
  monthSelect.innerHTML = "";
  // Whole-period choices precede the individual months.
  for (const [value, key] of [["ytd", "yearToDate"], ["year", "fullYear"]]) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = t(key);
    monthSelect.appendChild(opt);
  }
  MONTHS[lang].forEach((name, i) => {
    const opt = document.createElement("option");
    opt.value = String(i + 1).padStart(2, "0");
    opt.textContent = name;
    monthSelect.appendChild(opt);
  });
  if (selected) monthSelect.value = selected;
}

function populateYearOptions() {
  const now = new Date().getFullYear();
  yearSelect.innerHTML = "";
  for (let y = now; y >= now - 6; y--) {
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
    li.className = "flex flex-col items-stretch border-b border-line py-2 text-[0.9rem]";
    const pct = cat.limit ? Math.min(100, (cat.spent / cat.limit) * 100) : 0;
    const over = cat.limit !== null && cat.spent > cat.limit;
    const limitText = cat.limit !== null
      ? ` / ${euro.format(cat.limit)}${over ? ` (${t("overBudget")})` : ""}`
      : "";
    li.innerHTML = `
      <div class="flex justify-between"><span>${escapeHtml(cat.category)}</span>
        <span>${euro.format(cat.spent)}${limitText}</span></div>
      ${cat.limit !== null ? `<div class="mt-1.5 h-1.5 rounded-[3px] ${over ? "bg-expense" : "bg-accent"}" style="width:${pct}%"></div>` : ""}
    `;
    breakdown.appendChild(li);
  }
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

// Populate the budget category dropdown from the canonical category list so
// budgets always use the same vocabulary as the AI classification.
async function loadCategories() {
  const { categories } = await api("/api/categories");
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

function hideCategoryDetail() {
  el("category-detail").classList.add("hidden");
}

function showCategoryDetail(category) {
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
    li.className = "flex justify-between gap-3 border-b border-line py-1.5 text-[0.85rem]";
    li.innerHTML = `
      <span class="min-w-0 flex-1 truncate">
        <span class="text-muted">${escapeHtml(tx.date)}</span>
        ${escapeHtml(tx.description || "—")}
      </span>
      <span class="flex-none">${euro.format(tx.amount)}</span>`;
    list.appendChild(li);
  }
  panel.classList.remove("hidden");
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
    const res = await fetch("/api/import/postbank", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Import failed");
    status.textContent = t("importResult", data);
    e.target.reset();
    // Jump to the latest month so the freshly imported data is visible.
    try {
      const { month } = await api("/api/latest-month");
      if (month) setMonthYear(month);
    } catch (_) { /* ignore */ }
    await refreshAll();
  } catch (err) {
    status.textContent = err.message;
  }
});

el("category-detail-close").addEventListener("click", hideCategoryDetail);

function onMonthYearChange() {
  // loadSummary classifies (if needed), renders the chart and breakdown;
  // loadTrend re-anchors the trend line on the newly selected period.
  loadSummary();
  loadTrend();
}

monthSelect.addEventListener("change", onMonthYearChange);
yearSelect.addEventListener("change", onMonthYearChange);

// --- Init -----------------------------------------------------------------

async function init() {
  populateYearOptions();
  applyTranslations();
  loadCategories();
  setMonthYear(new Date().toISOString().slice(0, 7));
  try {
    const { month } = await api("/api/latest-month");
    if (month) setMonthYear(month);
  } catch (_) {
    /* fall back to the current month */
  }
  refreshAll();
}

init();

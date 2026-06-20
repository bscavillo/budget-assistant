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
    analyze: "Ausgaben mit KI kategorisieren",
    byCategory: "Ausgaben nach Kategorie",
    trendTitle: "Verlauf (Einnahmen / Ausgaben)",
    importTitle: "Aus Postbank importieren",
    importBtn: "CSV importieren",
    budgetsTitle: "Kategorie-Budgets",
    category: "Kategorie",
    monthlyLimit: "Monatslimit €",
    set: "Setzen",
    spendingTitle: "Ausgaben diesen Monat",
    delete: "Löschen",
    noExpenses: "Noch keine Ausgaben diesen Monat.",
    noBudgets: "Keine Budgets festgelegt.",
    overBudget: "über Budget",
    importing: "Wird importiert ...",
    importResult: "{imported} importiert, {skipped} Duplikat(e) übersprungen (von {parsed} erkannt).",
    analyzing: "Kategorisiere Ausgaben mit KI (kann eine Minute dauern) ...",
    analyzeDone: "Kategorisierung abgeschlossen.",
    noChartData: "Keine Daten zum Anzeigen.",
    spendingLabel: "Ausgaben (€)",
  },
  en: {
    income: "Income",
    expenses: "Expenses",
    balance: "Balance",
    analysis: "Analysis",
    analyze: "Categorize spending with AI",
    byCategory: "Spending by category",
    trendTitle: "Trend (income / expenses)",
    importTitle: "Import from Postbank",
    importBtn: "Import CSV",
    budgetsTitle: "Category budgets",
    category: "Category",
    monthlyLimit: "Monthly limit €",
    set: "Set",
    spendingTitle: "This month's spending",
    delete: "Delete",
    noExpenses: "No expenses this month yet.",
    noBudgets: "No budgets set.",
    overBudget: "over budget",
    importing: "Importing ...",
    importResult: "Imported {imported}, skipped {skipped} duplicate(s) (of {parsed} parsed).",
    analyzing: "Categorizing spending with AI (this can take a minute) ...",
    analyzeDone: "Categorization complete.",
    noChartData: "No data to display.",
    spendingLabel: "Spending (€)",
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

function currentMonth() {
  if (monthSelect.value && yearSelect.value) {
    return `${yearSelect.value}-${monthSelect.value}`;
  }
  return new Date().toISOString().slice(0, 7);
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

async function loadSummary() {
  const summary = await api(`/api/summary?month=${currentMonth()}`);
  el("card-income").textContent = euro.format(summary.income);
  el("card-expense").textContent = euro.format(summary.expense);
  el("card-balance").textContent = euro.format(summary.balance);

  const breakdown = el("category-breakdown");
  breakdown.innerHTML = "";
  if (!summary.categories.length) {
    breakdown.innerHTML = `<li class="meta">${t("noExpenses")}</li>`;
  }
  for (const cat of summary.categories) {
    const li = document.createElement("li");
    const pct = cat.limit ? Math.min(100, (cat.spent / cat.limit) * 100) : 0;
    const over = cat.limit !== null && cat.spent > cat.limit;
    const limitText = cat.limit !== null
      ? ` / ${euro.format(cat.limit)}${over ? ` (${t("overBudget")})` : ""}`
      : "";
    li.innerHTML = `
      <div class="line"><span>${escapeHtml(cat.category)}</span>
        <span>${euro.format(cat.spent)}${limitText}</span></div>
      ${cat.limit !== null ? `<div class="bar ${over ? "over" : ""}" style="width:${pct}%"></div>` : ""}
    `;
    breakdown.appendChild(li);
  }
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
    li.innerHTML = `<span>${escapeHtml(b.category)}</span>
      <span>${euro.format(b.monthly_limit)}
      <button class="del" data-category="${escapeHtml(b.category)}">${t("delete")}</button></span>`;
    list.appendChild(li);
  }
  refreshCategoryList(budgets);
}

function refreshCategoryList(budgets) {
  const datalist = el("category-list");
  datalist.innerHTML = "";
  budgets.map((b) => b.category).sort().forEach((c) => {
    const opt = document.createElement("option");
    opt.value = c;
    datalist.appendChild(opt);
  });
}

async function loadTrend() {
  const { trend } = await api("/api/trend?months=6");
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
  const ctx = el("category-chart");
  if (categoryChart) categoryChart.destroy();
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
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: tickColor }, grid: { color: gridColor } },
        y: { ticks: { color: tickColor, callback: eurAxis }, grid: { color: gridColor } },
      },
    },
  });
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

el("analyze-btn").addEventListener("click", async () => {
  const status = el("analyze-status");
  status.textContent = t("analyzing");
  try {
    const data = await api(`/api/categorized-spending?month=${currentMonth()}`);
    lastCategories = data.categories;
    if (!data.categories.length) {
      status.textContent = t("noChartData");
    } else {
      renderCategoryChart(data.categories);
      status.textContent = data.warning || t("analyzeDone");
    }
  } catch (err) {
    status.textContent = err.message;
  }
});

monthSelect.addEventListener("change", loadSummary);
yearSelect.addEventListener("change", loadSummary);

// --- Init -----------------------------------------------------------------

async function init() {
  populateYearOptions();
  applyTranslations();
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

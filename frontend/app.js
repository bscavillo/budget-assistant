"use strict";

// --- Internationalization -------------------------------------------------

const I18N = {
  de: {
    month: "Monat",
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
    transactions: "Transaktionen",
    aiTitle: "KI-Assistent",
    insightsBtn: "Einblicke für diesen Monat",
    aiPlaceholder: "Stelle eine Frage oder hole dir Einblicke zu deinem Budget.",
    askPlaceholder: "z. B. Wie kann ich bei Lebensmitteln sparen?",
    ask: "Fragen",
    noTransactions: "Noch keine Transaktionen.",
    noExpenses: "Noch keine Ausgaben diesen Monat.",
    noBudgets: "Keine Budgets festgelegt.",
    overBudget: "über Budget",
    delete: "Löschen",
    importing: "Wird importiert ...",
    importResult: "{imported} importiert, {skipped} Duplikat(e) übersprungen (von {parsed} erkannt).",
    thinking: "Denke nach ...",
    analyzing: "Kategorisiere Ausgaben mit KI (kann eine Minute dauern) ...",
    analyzeDone: "Kategorisierung abgeschlossen.",
    noChartData: "Keine Daten zum Anzeigen.",
    spendingLabel: "Ausgaben (€)",
  },
  en: {
    month: "Month",
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
    transactions: "Transactions",
    aiTitle: "AI assistant",
    insightsBtn: "Insights for this month",
    aiPlaceholder: "Ask a question or get insights about your budget.",
    askPlaceholder: "e.g. How can I save on groceries?",
    ask: "Ask",
    noTransactions: "No transactions yet.",
    noExpenses: "No expenses this month yet.",
    noBudgets: "No budgets set.",
    overBudget: "over budget",
    delete: "Delete",
    importing: "Importing ...",
    importResult: "Imported {imported}, skipped {skipped} duplicate(s) (of {parsed} parsed).",
    thinking: "Thinking ...",
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
const monthInput = el("month");

// Cache of the latest fetched data so charts/lists can re-render on language change.
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
}

function setLang(next) {
  lang = next;
  localStorage.setItem("lang", lang);
  applyTranslations();
  refreshAll();
  if (lastTrend) renderTrendChart(lastTrend);
  if (lastCategories) renderCategoryChart(lastCategories);
}

// --- API ------------------------------------------------------------------

function currentMonth() {
  return monthInput.value || new Date().toISOString().slice(0, 7);
}

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

async function loadTransactions() {
  const txs = await api("/api/transactions");
  const list = el("tx-list");
  list.innerHTML = "";
  if (!txs.length) {
    list.innerHTML = `<li class="meta">${t("noTransactions")}</li>`;
  }
  const categories = new Set();
  for (const tx of txs) {
    categories.add(tx.category);
    const li = document.createElement("li");
    li.innerHTML = `
      <div>
        <div>${escapeHtml(tx.category)} <span class="meta">${escapeHtml(tx.description || "")}</span></div>
        <div class="meta">${tx.date}</div>
      </div>
      <div style="display:flex;align-items:center;gap:.5rem">
        <span class="amount ${tx.type}">${tx.type === "expense" ? "-" : "+"}${euro.format(tx.amount)}</span>
        <button class="del" data-id="${tx.id}" title="${t("delete")}">${t("delete")}</button>
      </div>`;
    list.appendChild(li);
  }
  refreshCategoryList(categories);
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
}

function refreshCategoryList(extra = new Set()) {
  api("/api/budgets").then((budgets) => {
    const all = new Set(extra);
    budgets.forEach((b) => all.add(b.category));
    const datalist = el("category-list");
    datalist.innerHTML = "";
    [...all].sort().forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c;
      datalist.appendChild(opt);
    });
  });
}

async function loadTrend() {
  const { trend } = await api("/api/trend?months=6");
  lastTrend = trend;
  renderTrendChart(trend);
}

async function refreshAll() {
  await Promise.all([loadSummary(), loadTransactions(), loadBudgets(), loadTrend()]);
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
  const txBtn = e.target.closest(".tx-list button.del");
  if (txBtn) {
    await api(`/api/transactions/${txBtn.dataset.id}`, { method: "DELETE" });
    await refreshAll();
    return;
  }
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

el("insights-btn").addEventListener("click", async () => {
  const out = el("ai-output");
  out.classList.add("loading");
  out.textContent = t("thinking");
  try {
    const { insights } = await api(`/api/insights?month=${currentMonth()}`);
    out.textContent = insights;
  } catch (err) {
    out.textContent = err.message;
  } finally {
    out.classList.remove("loading");
  }
});

el("ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = el("ask-input").value.trim();
  if (!question) return;
  const out = el("ai-output");
  out.classList.add("loading");
  out.textContent = t("thinking");
  try {
    const { answer } = await api("/api/ask", {
      method: "POST",
      body: JSON.stringify({ question, month: currentMonth() }),
    });
    out.textContent = answer;
  } catch (err) {
    out.textContent = err.message;
  } finally {
    out.classList.remove("loading");
  }
});

monthInput.addEventListener("change", () => {
  loadSummary();
});

// --- Init -----------------------------------------------------------------

async function init() {
  applyTranslations();
  // Default the month picker to the most recent month that has data.
  monthInput.value = new Date().toISOString().slice(0, 7);
  try {
    const { month } = await api("/api/latest-month");
    if (month) monthInput.value = month;
  } catch (_) {
    /* fall back to the current month */
  }
  refreshAll();
}

init();

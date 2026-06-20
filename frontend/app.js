"use strict";

const euro = new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR" });

const el = (id) => document.getElementById(id);
const monthInput = el("month");

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

// --- Rendering ------------------------------------------------------------

async function loadSummary() {
  const summary = await api(`/api/summary?month=${currentMonth()}`);
  el("card-income").textContent = euro.format(summary.income);
  el("card-expense").textContent = euro.format(summary.expense);
  el("card-balance").textContent = euro.format(summary.balance);

  const breakdown = el("category-breakdown");
  breakdown.innerHTML = "";
  if (!summary.categories.length) {
    breakdown.innerHTML = '<li class="meta">No expenses this month yet.</li>';
  }
  for (const cat of summary.categories) {
    const li = document.createElement("li");
    const pct = cat.limit ? Math.min(100, (cat.spent / cat.limit) * 100) : 0;
    const over = cat.limit !== null && cat.spent > cat.limit;
    const limitText = cat.limit !== null
      ? ` / ${euro.format(cat.limit)}${over ? " (over budget)" : ""}`
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
    list.innerHTML = '<li class="meta">No transactions yet.</li>';
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
        <button class="del" data-id="${tx.id}" title="Delete">Delete</button>
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
    list.innerHTML = '<li class="meta">No budgets set.</li>';
  }
  for (const b of budgets) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${escapeHtml(b.category)}</span>
      <span>${euro.format(b.monthly_limit)}
      <button class="del" data-category="${escapeHtml(b.category)}">Delete</button></span>`;
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

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function refreshAll() {
  await Promise.all([loadSummary(), loadTransactions(), loadBudgets()]);
}

// --- Events ---------------------------------------------------------------

el("tx-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    type: el("tx-type").value,
    date: el("tx-date").value,
    amount: parseFloat(el("tx-amount").value),
    category: el("tx-category").value.trim(),
    description: el("tx-description").value.trim(),
  };
  await api("/api/transactions", { method: "POST", body: JSON.stringify(payload) });
  e.target.reset();
  el("tx-date").value = new Date().toISOString().slice(0, 10);
  await refreshAll();
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

el("suggest-btn").addEventListener("click", async () => {
  const description = el("tx-description").value.trim();
  if (!description) {
    alert("Enter a description first so the AI can suggest a category.");
    return;
  }
  const btn = el("suggest-btn");
  btn.textContent = "...";
  try {
    const { category } = await api("/api/categorize", {
      method: "POST",
      body: JSON.stringify({ description }),
    });
    el("tx-category").value = category;
  } finally {
    btn.textContent = "AI";
  }
});

el("import-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fileInput = el("import-file");
  const status = el("import-status");
  if (!fileInput.files.length) return;
  status.textContent = "Importing...";
  try {
    const formData = new FormData();
    formData.append("file", fileInput.files[0]);
    const res = await fetch("/api/import/postbank", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Import failed");
    status.textContent =
      `Imported ${data.imported}, skipped ${data.skipped} duplicate(s) of ${data.parsed} parsed.`;
    e.target.reset();
    await refreshAll();
  } catch (err) {
    status.textContent = err.message;
  }
});

el("insights-btn").addEventListener("click", async () => {
  const out = el("ai-output");
  out.classList.add("loading");
  out.textContent = "Thinking…";
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
  out.textContent = "Thinking…";
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

monthInput.addEventListener("change", loadSummary);

// --- Init -----------------------------------------------------------------

monthInput.value = new Date().toISOString().slice(0, 7);
el("tx-date").value = new Date().toISOString().slice(0, 10);
refreshAll();

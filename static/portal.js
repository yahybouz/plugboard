/* ── Plugboard Client Portal ────────────────────────────────────────────── */
"use strict";

// ── API client ───────────────────────────────────────────────────────────────

function getKey() { return sessionStorage.getItem("client_key"); }

async function api(method, path) {
  const key = getKey();
  const res = await fetch(path, {
    method,
    headers: { "Authorization": "Bearer " + key, "Content-Type": "application/json" },
  });
  if (res.status === 401) { logout(); throw new Error("Unauthorized"); }
  return res;
}

function logout() {
  sessionStorage.removeItem("client_key");
  location.reload();
}

function usd(n) { return "$" + Number(n || 0).toFixed(4); }
function fmt(n) { return Number(n || 0).toLocaleString(); }

// ── DOM helpers ──────────────────────────────────────────────────────────────

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) Object.entries(attrs).forEach(function (pair) {
    const k = pair[0], v = pair[1];
    if (k === "className") el.className = v;
    else el.setAttribute(k, v);
  });
  children.flat().forEach(function (c) {
    if (c == null) return;
    el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  });
  return el;
}

function buildTable(headers, rows) {
  return h("div", { className: "table-wrap" },
    h("table", null,
      h("thead", null, h("tr", null, ...headers.map(function (hd) { return h("th", null, hd); }))),
      h("tbody", null, ...rows)
    )
  );
}

function statCard(label, value, color) {
  return h("div", { className: "card stat-card" },
    h("div", { className: "stat-label" }, label),
    h("div", { className: "stat-value " + (color || "") }, value),
  );
}

// ── Auth ─────────────────────────────────────────────────────────────────────

const loginModal = document.getElementById("login-modal");
const loginForm = document.getElementById("login-form");
const loginError = document.getElementById("login-error");
const appEl = document.getElementById("app");

async function tryLogin(key) {
  sessionStorage.setItem("client_key", key);
  try {
    const res = await api("GET", "/usage/me");
    if (!res.ok) throw new Error();
    return true;
  } catch (e) { console.debug("client key check failed:", e);
    sessionStorage.removeItem("client_key");
    return false;
  }
}

loginForm.addEventListener("submit", async function (e) {
  e.preventDefault();
  loginError.classList.add("hidden");
  const key = document.getElementById("login-key").value.trim();
  if (!key) return;
  const ok = await tryLogin(key);
  if (ok) { showApp(); }
  else { loginError.textContent = "Invalid API key."; loginError.classList.remove("hidden"); }
});

function showApp() {
  loginModal.classList.add("hidden");
  appEl.classList.remove("hidden");
  loadPortal();
}

// auto-login if key in sessionStorage
(async function () {
  const key = getKey();
  if (key) {
    try {
      const res = await fetch("/usage/me", { headers: { "Authorization": "Bearer " + key } });
      if (res.ok) { showApp(); return; }
    } catch (e) { console.debug("session probe failed:", e); }
  }
  loginModal.classList.remove("hidden");
})();

document.getElementById("logout-btn").addEventListener("click", logout);

// ── Portal data loading ──────────────────────────────────────────────────────

async function loadPortal() {
  const cardsEl = document.getElementById("summary-cards");
  const byModelEl = document.getElementById("by-model");
  const byDayEl = document.getElementById("by-day");
  const spendingContainer = document.getElementById("spending-bar-container");
  const spendingBar = document.getElementById("spending-bar");

  cardsEl.innerHTML = "Loading...";
  byModelEl.innerHTML = "";
  byDayEl.innerHTML = "";
  spendingContainer.classList.add("hidden");

  try {
    const [meRes, infoRes] = await Promise.all([
      api("GET", "/usage/me"),
      api("GET", "/v1/me"),
    ]);
    const meData = await meRes.json();
    const infoData = await infoRes.json();

    const summary = meData.summary || {};
    const entries = meData.entries || [];

    // Client badge
    document.getElementById("client-badge").textContent = infoData.client_id || meData.client_id || "unknown";

    // Summary cards
    cardsEl.innerHTML = "";
    cardsEl.appendChild(statCard("Requests", fmt(summary.total_entries || 0), "accent"));
    cardsEl.appendChild(statCard("Total Tokens", fmt(summary.total_tokens || 0), ""));
    cardsEl.appendChild(statCard("Cost (month)", usd(summary.total_cost_usd || 0), "orange"));

    // Spending limit
    const limit = infoData.monthly_limit_usd || 0;
    if (limit > 0) {
      const remaining = infoData.remaining_usd != null ? infoData.remaining_usd : limit;
      const pct = Math.min(100, ((summary.total_cost_usd || 0) / limit) * 100);
      let colorClass = "green";
      if (pct >= 100) colorClass = "red";
      else if (pct >= 80) colorClass = "orange";
      spendingContainer.classList.remove("hidden");
      spendingBar.innerHTML = "";
      const bar = h("div", { className: "spending-bar" },
        h("div", {
          className: "spending-fill " + colorClass,
          style: "width:" + pct.toFixed(1) + "%",
        }, pct.toFixed(1) + "%")
      );
      spendingBar.appendChild(bar);
      spendingBar.appendChild(
        h("p", { className: "muted" },
          usd(summary.total_cost_usd || 0) + " / " + usd(limit) +
          " (" + usd(remaining) + " remaining)")
      );
      cardsEl.appendChild(statCard("Remaining", usd(remaining), remaining > 0 ? "green" : "red"));
    }

    // By model table
    const bm = summary.by_model || {};
    const modelNames = Object.keys(bm);
    if (modelNames.length) {
      const rows = modelNames.map(function (m) {
        return h("tr", null,
          h("td", null, h("code", null, m)),
          h("td", null, fmt(bm[m].entries)),
          h("td", null, fmt(bm[m].tokens)),
          h("td", null, usd(bm[m].cost_usd)),
        );
      });
      byModelEl.appendChild(buildTable(["Model", "Requests", "Tokens", "Cost"], rows));
    } else {
      byModelEl.innerHTML = '<p class="muted">No usage data for this month.</p>';
    }

    // By day aggregation
    const byDay = {};
    entries.forEach(function (e) {
      const day = (e.ts || "").slice(0, 10);
      if (!day) return;
      const d = byDay[day] = byDay[day] || { requests: 0, tokens: 0, cost: 0 };
      d.requests++;
      d.tokens += e.total_tokens || 0;
      d.cost += e.cost_usd || 0;
    });
    const days = Object.keys(byDay).sort(function (a, b) { return a.localeCompare(b); });

    // Chart.js bar chart
    renderDailyChart(days, byDay);

    // By day table
    if (days.length) {
      const dayRows = days.map(function (day) {
        return h("tr", null,
          h("td", null, day),
          h("td", null, fmt(byDay[day].requests)),
          h("td", null, fmt(byDay[day].tokens)),
          h("td", null, usd(byDay[day].cost)),
        );
      });
      byDayEl.appendChild(buildTable(["Date", "Requests", "Tokens", "Cost"], dayRows));
    } else {
      byDayEl.innerHTML = '<p class="muted">No daily data yet.</p>';
    }

  } catch (err) { console.warn("portal data load failed:", err);
    cardsEl.innerHTML = '<p class="error">Failed to load portal data.</p>';
  }
}

// ── Chart.js ────────────────────────────────────────────────────────────────

let _chart = null;

function renderDailyChart(days, byDay) {
  const canvas = document.getElementById("usage-chart");
  if (!canvas || typeof Chart === "undefined") return;

  if (_chart) { _chart.destroy(); _chart = null; }

  if (!days.length) {
    document.getElementById("chart-container").innerHTML = '<p class="muted">No chart data yet.</p>';
    return;
  }

  const costs = days.map(function (d) { return +(byDay[d].cost || 0).toFixed(4); });
  const requests = days.map(function (d) { return byDay[d].requests || 0; });

  _chart = new Chart(canvas.getContext("2d"), {
    type: "bar",
    data: {
      labels: days.map(function (d) { return d.slice(5); }),  // MM-DD
      datasets: [
        {
          label: "Cost ($)",
          data: costs,
          backgroundColor: "rgba(59,130,246,0.7)",
          yAxisID: "y",
          order: 2,
        },
        {
          label: "Requests",
          data: requests,
          type: "line",
          borderColor: "rgba(251,191,36,0.9)",
          backgroundColor: "rgba(251,191,36,0.1)",
          yAxisID: "y1",
          tension: 0.3,
          pointRadius: 3,
          order: 1,
        }
      ],
    },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: "#ccc" } },
      },
      scales: {
        x: { ticks: { color: "#999" }, grid: { color: "rgba(255,255,255,0.05)" } },
        y: {
          type: "linear", position: "left",
          title: { display: true, text: "Cost ($)", color: "#3b82f6" },
          ticks: { color: "#3b82f6" },
          grid: { color: "rgba(255,255,255,0.05)" },
        },
        y1: {
          type: "linear", position: "right",
          title: { display: true, text: "Requests", color: "#fbbf24" },
          ticks: { color: "#fbbf24" },
          grid: { drawOnChartArea: false },
        },
      },
    },
  });
}

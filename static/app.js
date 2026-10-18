/* ── Plugboard Admin Dashboard ──────────────────────────────────────────── */
"use strict";

// ── Toast notifications ─────────────────────────────────────────────────────

function toast(msg, type) {
  type = type || "success";
  var container = document.getElementById("toast-container");
  var el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(function () { el.style.opacity = "0"; setTimeout(function () { el.remove(); }, 300); }, 3500);
}

// ── API client ──────────────────────────────────────────────────────────────

async function api(method, path, body) {
  var opts = { method: method, headers: {} };
  if (body !== undefined) opts.headers["Content-Type"] = "application/json";
  if (body !== undefined) opts.body = JSON.stringify(body);
  var res = await fetch(path, opts);
  if (res.status === 401) { logout(); throw new Error("Unauthorized"); }
  return res;
}

async function logout(explicit) {
  try {
    await fetch("/auth/admin/logout", { method: "POST" });
  } catch (e) { /* ignore */ }
  // An explicit logout must not be undone by local auto-login.
  location.href = explicit ? "/login?logged_out=1" : "/login";
}

function currentMonth() {
  var d = new Date();
  return d.getFullYear() + "_" + String(d.getMonth() + 1).padStart(2, "0");
}
function toApiMonth(htmlMonth) { return htmlMonth.replace("-", "_"); }
function toHtmlMonth(apiMonth) { return apiMonth.replace("_", "-"); }
function usd(n) { return "$" + Number(n || 0).toFixed(4); }
function fmt(n) { return Number(n || 0).toLocaleString(); }

// ── Auth ────────────────────────────────────────────────────────────────────

var loginModal = document.getElementById("login-modal");
var loginForm = document.getElementById("login-form");
var loginError = document.getElementById("login-error");
var appEl = document.getElementById("app");

async function tryLogin(key) {
  try {
    var res = await fetch("/auth/admin/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: key }),
    });
    return res.ok;
  } catch (e) {
    return false;
  }
}

loginForm.addEventListener("submit", async function (e) {
  e.preventDefault();
  loginError.classList.add("hidden");
  var key = document.getElementById("login-key").value.trim();
  if (!key) return;
  var ok = await tryLogin(key);
  if (ok) {
    showApp();
  } else {
    loginError.textContent = "Invalid admin key.";
    loginError.classList.remove("hidden");
  }
});

function showApp() {
  loginModal.classList.add("hidden");
  appEl.classList.remove("hidden");
  refreshVersionBadge();
  initTabs();
}

// Update the sidebar version badge from /health (public, no auth required).
// Runs independently of loadOverview so the badge is correct even when the
// admin endpoints fetched there fail (auth issues, network blip).
async function refreshVersionBadge() {
  try {
    var res = await fetch("/health");
    if (!res.ok) return;
    var health = await res.json();
    var badge = document.getElementById("version-badge");
    if (badge && health.version) badge.textContent = "v" + health.version;
  } catch (e) {
    /* leave the hardcoded fallback in place */
  }
}

// auto-login if secure admin session cookie already exists
(async function () {
  try {
    var res = await fetch("/admin/keys");
    if (res.ok) { showApp(); return; }
  } catch (e) {
    /* ignore */
  }
  loginModal.classList.remove("hidden");
})();

document.getElementById("logout-btn").addEventListener("click", function () { logout(true); });

// Mobile menu
var mobileBtn = document.getElementById("mobile-menu-btn");
if (mobileBtn) {
  mobileBtn.addEventListener("click", function () {
    document.getElementById("sidebar").classList.toggle("open");
  });
}

// ── Tab / Sidebar routing ───────────────────────────────────────────────────

var TAB_LOADERS = { overview: loadOverview, keys: loadKeys, usage: loadUsage, providers: loadProviders, connections: loadConnections, pricing: loadPricing, monitoring: loadMonitoring, jobs: loadJobs, ollama: loadOllama, playground: loadPlayground };
var navLinks = document.querySelectorAll(".nav-link");
var pages = document.querySelectorAll(".page");

function switchTab(name) {
  navLinks.forEach(function (t) { t.classList.toggle("active", t.dataset.tab === name); });
  pages.forEach(function (p) { p.classList.toggle("active", p.id === "page-" + name); });
  if (TAB_LOADERS[name]) TAB_LOADERS[name]();
  // Close mobile sidebar
  document.getElementById("sidebar").classList.remove("open");
}

function initTabs() {
  navLinks.forEach(function (t) {
    t.addEventListener("click", function (e) {
      e.preventDefault();
      location.hash = t.dataset.tab;
    });
  });
  window.addEventListener("hashchange", function () { switchTab(location.hash.slice(1) || "overview"); });
  switchTab(location.hash.slice(1) || "overview");
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function h(tag, attrs) {
  var children = Array.prototype.slice.call(arguments, 2);
  var el = document.createElement(tag);
  if (attrs) Object.keys(attrs).forEach(function (k) {
    var v = attrs[k];
    if (k === "className") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, v);
  });
  children.flat().forEach(function (c) {
    if (c == null) return;
    el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  });
  return el;
}

function table(headers, rows) {
  return h("div", { className: "table-wrap" },
    h("table", null,
      h("thead", null, h("tr", null, headers.map(function (hd) { return h("th", null, hd); }))),
      h("tbody", null, rows)
    )
  );
}

function skeleton(count) {
  var els = [];
  for (var i = 0; i < count; i++) els.push(h("div", { className: "skeleton skeleton-card", style: "margin-bottom:0.75rem;" }));
  return h("div", null, els);
}

function emptyState(icon, text) {
  return h("div", { className: "empty-state" },
    h("div", { className: "empty-icon" }, icon),
    h("p", null, text)
  );
}

// In-page confirmation. window.confirm() is unreliable: embedded browsers and
// some pop-up blockers answer "cancel" without showing anything, so a delete
// button silently did nothing. Resolves to true only on an explicit OK.
function confirmAction(msg, okLabel) {
  return new Promise(function (resolve) {
    var previous = document.activeElement;
    var overlay = document.createElement("div");
    overlay.className = "modal confirm-modal";
    var box = document.createElement("div");
    box.className = "modal-box confirm-box";
    box.setAttribute("role", "alertdialog");
    box.setAttribute("aria-modal", "true");
    var text = document.createElement("p");
    text.className = "confirm-text";
    text.id = "confirm-text-" + Date.now();
    text.textContent = msg;
    box.setAttribute("aria-describedby", text.id);
    var cancel = document.createElement("button");
    cancel.type = "button"; cancel.className = "btn"; cancel.textContent = "Cancel";
    var ok = document.createElement("button");
    ok.type = "button"; ok.className = "btn btn-danger"; ok.textContent = okLabel || "Confirm";
    var actions = document.createElement("div");
    actions.className = "btn-group confirm-actions";
    actions.appendChild(cancel); actions.appendChild(ok);
    box.appendChild(text); box.appendChild(actions);
    overlay.appendChild(box);
    function close(answer) {
      document.removeEventListener("keydown", onKey, true);
      overlay.remove();
      if (previous && previous.focus) previous.focus();
      resolve(answer);
    }
    function onKey(ev) {
      if (ev.key === "Escape") { ev.preventDefault(); close(false); }
      else if (ev.key === "Tab") {  // keep focus inside the dialog
        ev.preventDefault();
        (document.activeElement === ok ? cancel : ok).focus();
      }
    }
    cancel.addEventListener("click", function () { close(false); });
    ok.addEventListener("click", function () { close(true); });
    overlay.addEventListener("click", function (ev) { if (ev.target === overlay) close(false); });
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(overlay);
    cancel.focus();
  });
}

// ── Overview ────────────────────────────────────────────────────────────────

async function loadOverview() {
  var container = document.getElementById("overview-cards");
  var metricsContainer = document.getElementById("metrics-table-container");
  container.innerHTML = "";
  container.appendChild(skeleton(4));
  metricsContainer.innerHTML = "";

  try {
    var results = await Promise.all([
      api("GET", "/health"),
      api("GET", "/admin/usage/" + currentMonth()),
      api("GET", "/metrics"),
    ]);
    var health = await results[0].json();
    var usage = await results[1].json();
    var metrics = await results[2].json();

    document.getElementById("version-badge").textContent = "v" + health.version;

    var summary = usage.summary || {};
    container.innerHTML = "";
    container.appendChild(statCard("⬤", "Status", health.ok ? "Online" : "DOWN", health.ok ? "green" : "red"));
    container.appendChild(statCard("⚙", "Providers", String(health.providers), "accent"));
    container.appendChild(statCard("📈", "Requests", fmt(summary.total_entries || 0), "purple"));
    container.appendChild(statCard("💰", "Cost (month)", usd(summary.total_cost_usd || 0), "orange"));

    // Metrics table
    var pids = Object.keys(metrics);
    if (pids.length) {
      var rows = pids.map(function (pid) {
        return h("tr", null,
          h("td", null, h("code", null, pid)),
          h("td", null, fmt(metrics[pid].requests)),
          h("td", null, fmt(metrics[pid].errors)),
          h("td", null, (metrics[pid].latency_avg || 0).toFixed(3) + "s"),
        );
      });
      metricsContainer.appendChild(table(["Provider", "Requests", "Errors", "Avg Latency"], rows));
    } else {
      metricsContainer.appendChild(emptyState("—", "No metrics recorded yet."));
    }
  } catch (err) {
    container.innerHTML = "";
    container.appendChild(h("div", { className: "alert alert-error" }, "Failed to load overview."));
  }
}

function statCard(icon, label, value, color) {
  var card = h("div", { className: "card stat-card stat-" + (color || "accent") },
    h("div", { className: "stat-value " + (color || "") }, value),
    h("div", { className: "stat-label" }, label),
  );
  return card;
}

// ── API Keys ────────────────────────────────────────────────────────────────

var createKeyForm = document.getElementById("create-key-form");
var newKeyAlert = document.getElementById("new-key-alert");

document.getElementById("btn-create-key").addEventListener("click", function () {
  createKeyForm.classList.toggle("hidden");
  newKeyAlert.classList.add("hidden");
});
document.getElementById("btn-cancel-key").addEventListener("click", function () { createKeyForm.classList.add("hidden"); });

document.getElementById("btn-submit-key").addEventListener("click", async function () {
  var clientId = document.getElementById("key-client-id").value.trim();
  if (!clientId) return;
  var label = document.getElementById("key-label").value.trim();
  var limit = parseFloat(document.getElementById("key-limit").value) || 0;
  var rpm = parseInt(document.getElementById("key-rpm").value) || 0;
  try {
    var res = await api("POST", "/admin/keys", { client_id: clientId, label: label, monthly_limit_usd: limit, rpm_limit: rpm });
    var body = await res.json();
    document.getElementById("new-key-value").textContent = body.key;
    newKeyAlert.classList.remove("hidden");
    createKeyForm.classList.add("hidden");
    document.getElementById("key-client-id").value = "";
    document.getElementById("key-label").value = "";
    document.getElementById("key-limit").value = "";
    document.getElementById("key-rpm").value = "";
    toast("API key created for " + clientId, "success");
    loadKeys();
  } catch (e) { toast("Failed to create key", "error"); }
});

document.getElementById("btn-copy-key").addEventListener("click", function () {
  navigator.clipboard.writeText(document.getElementById("new-key-value").textContent);
  toast("Key copied to clipboard", "success");
});

async function loadKeys() {
  var container = document.getElementById("keys-table-container");
  container.innerHTML = "";
  container.appendChild(skeleton(3));
  try {
    var res = await api("GET", "/admin/keys");
    var keys = await res.json();
    container.innerHTML = "";
    if (!keys.length) { container.appendChild(emptyState("—", "No API keys yet. Create one to get started.")); return; }
    var rows = keys.map(function (k) {
      return h("tr", null,
        h("td", null, h("strong", null, k.client_id)),
        h("td", null, k.label || "—"),
        h("td", null, h("code", null, k.key_display || k.key_prefix + "***")),
        h("td", null, k.is_active
          ? h("span", { className: "badge badge-green" }, "active")
          : h("span", { className: "badge badge-red" }, "revoked")),
        h("td", null, k.monthly_limit_usd ? usd(k.monthly_limit_usd) : h("span", { className: "muted" }, "Unlimited")),
        h("td", null, k.rpm_limit ? k.rpm_limit + "/min" : h("span", { className: "muted" }, "—")),
        h("td", null, k.created_at ? k.created_at.slice(0, 10) : "—"),
        h("td", null, h("div", { className: "btn-group" },
          k.is_active ? h("button", { className: "btn btn-sm", onClick: function () { revokeKey(k.key_id); } }, "Revoke") : null,
          h("button", { className: "btn btn-sm btn-danger", onClick: function () { deleteKey(k.key_id); } }, "Delete"),
        )),
      );
    });
    container.appendChild(table(["Client", "Label", "Key", "Status", "Limit", "RPM", "Created", "Actions"], rows));
  } catch (e) { container.innerHTML = ""; container.appendChild(h("div", { className: "alert alert-error" }, "Failed to load keys.")); }
}

async function revokeKey(keyId) {
  if (!await confirmAction("Revoke this key?")) return;
  await api("POST", "/admin/keys/" + keyId + "/revoke");
  toast("Key revoked", "success");
  loadKeys();
}
async function deleteKey(keyId) {
  if (!await confirmAction("Permanently delete this key?")) return;
  await api("DELETE", "/admin/keys/" + keyId);
  toast("Key deleted", "success");
  loadKeys();
}

// ── Usage & Billing ─────────────────────────────────────────────────────────

var usageMonthInput = document.getElementById("usage-month");
usageMonthInput.value = toHtmlMonth(currentMonth());
usageMonthInput.addEventListener("change", loadUsage);

document.getElementById("btn-export-csv").addEventListener("click", async function () {
  var month = toApiMonth(usageMonthInput.value);
  try {
    var res = await fetch("/admin/usage/" + month + "/export?format=csv");
    if (!res.ok) throw new Error("Export failed");
    var blob = await res.blob();
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url; a.download = "usage_" + month + ".csv"; a.click();
    URL.revokeObjectURL(url);
    toast("CSV exported", "success");
  } catch (err) { toast("Export failed: " + err.message, "error"); }
});

async function loadUsage() {
  var month = toApiMonth(usageMonthInput.value);
  var cardsEl = document.getElementById("usage-summary-cards");
  var clientEl = document.getElementById("usage-by-client");
  var modelEl = document.getElementById("usage-by-model");
  cardsEl.innerHTML = ""; cardsEl.appendChild(skeleton(4));
  clientEl.innerHTML = ""; modelEl.innerHTML = "";

  try {
    var res = await api("GET", "/admin/usage/" + month);
    var data = await res.json();
    var s = data.summary || {};

    cardsEl.innerHTML = "";
    cardsEl.appendChild(statCard("📈", "Requests", fmt(s.total_entries || 0), "accent"));
    cardsEl.appendChild(statCard("💬", "Prompt Tokens", fmt(s.total_prompt_tokens || 0), ""));
    cardsEl.appendChild(statCard("💬", "Completion Tokens", fmt(s.total_completion_tokens || 0), ""));
    cardsEl.appendChild(statCard("💰", "Total Cost", usd(s.total_cost_usd || 0), "orange"));

    // By client
    var byClient = {};
    (data.entries || []).forEach(function (e) {
      var c = byClient[e.client_id] = byClient[e.client_id] || { entries: 0, tokens: 0, cost: 0 };
      c.entries++; c.tokens += e.total_tokens || 0; c.cost += e.cost_usd || 0;
    });
    var clientIds = Object.keys(byClient);
    if (clientIds.length) {
      var rows = clientIds.map(function (cid) {
        return h("tr", null,
          h("td", null, h("strong", null, cid)),
          h("td", null, fmt(byClient[cid].entries)),
          h("td", null, fmt(byClient[cid].tokens)),
          h("td", null, usd(byClient[cid].cost)),
        );
      });
      clientEl.appendChild(table(["Client", "Requests", "Tokens", "Cost"], rows));
    } else {
      clientEl.appendChild(emptyState("—", "No usage data for this month."));
    }

    // By model
    var bm = s.by_model || {};
    var modelNames = Object.keys(bm);
    if (modelNames.length) {
      var mrows = modelNames.map(function (m) {
        return h("tr", null,
          h("td", null, h("code", null, m)),
          h("td", null, fmt(bm[m].entries)),
          h("td", null, fmt(bm[m].tokens)),
          h("td", null, usd(bm[m].cost_usd)),
        );
      });
      modelEl.appendChild(table(["Model", "Requests", "Tokens", "Cost"], mrows));
    }

    // Recent requests, newest first, with the grid entry that priced them
    var recentEl = document.getElementById("usage-recent");
    recentEl.innerHTML = "";
    var recentData = data;
    var perPage = data.limit || 100;
    if ((data.total || 0) > perPage) {
      var lastPage = Math.ceil(data.total / perPage);
      var lastRes = await api("GET", "/admin/usage/" + month + "?page=" + lastPage + "&limit=" + perPage);
      if (lastRes.ok) recentData = await lastRes.json();
    }
    var recent = (recentData.entries || []).slice(-20).reverse();
    if (recent.length) {
      recentEl.appendChild(table(["Time", "Client", "Model", "Served by", "Price key", "Source", "Cost"], recent.map(function (e) {
        return h("tr", null,
          h("td", null, (e.ts || "").slice(0, 19).replace("T", " ")),
          h("td", null, e.client_id || "—"),
          h("td", null, h("code", null, e.model || "—")),
          h("td", null, h("code", null, e.provider_id || "—")),
          h("td", null, e.price_key ? h("code", null, e.price_key) : h("span", { className: "muted" }, "not priced")),
          h("td", null, e.price_source ? h("span", { className: "badge badge-muted" }, e.price_source) : "—"),
          h("td", null, usd(e.cost_usd)),
        );
      })));
    } else {
      recentEl.appendChild(emptyState("—", "No requests this month."));
    }
  } catch (e) { cardsEl.innerHTML = ""; cardsEl.appendChild(h("div", { className: "alert alert-error" }, "Failed to load usage.")); }
}

// ── Providers ───────────────────────────────────────────────────────────────

var providerFormContainer = document.getElementById("provider-form-container");
var providerForm = document.getElementById("provider-form");

document.getElementById("btn-add-provider").addEventListener("click", function () {
  providerFormContainer.classList.remove("hidden");
  providerForm.reset();
  document.getElementById("pf-active").checked = true;
  document.getElementById("pf-timeout").value = "60";
  document.getElementById("pf-edit-id").value = "";
  document.getElementById("pf-submit-btn").textContent = "Create";
  document.getElementById("pf-form-title").textContent = "Add Provider";
  document.getElementById("pf-token").placeholder = "Bearer token";
  document.getElementById("pf-token").required = true;
  document.getElementById("pf-auto-discover").checked = false;
  document.getElementById("pf-pinned").value = "";
});
document.getElementById("btn-cancel-provider").addEventListener("click", function () { providerFormContainer.classList.add("hidden"); });

// Direct provider presets wizard (API key + OAuth via CLI)
var directProviderForm = document.getElementById("direct-provider-form");
var _directPresets = [];

async function _loadDirectPresets() {
  if (_directPresets.length) return;
  try {
    var res = await api("GET", "/admin/direct-providers/presets");
    var data = await res.json();
    _directPresets = data.presets || [];
    _fillPresetSelect("");
    var select = document.getElementById("dp-preset");
    select.addEventListener("change", _renderPresetView);
    var search = document.getElementById("dp-search");
    search.addEventListener("input", function () {
      _fillPresetSelect(search.value.trim().toLowerCase());
    });
  } catch (e) { /* ignore */ }
}

function _fillPresetSelect(query) {
  var select = document.getElementById("dp-preset");
  var previous = select.value;
  select.innerHTML = "";
  // Group by auth type; catalog presets come from models.dev and are unverified.
  var groups = [
    ["sub", "CLI subprocess (OpenClaw-style) — recommended"],
    ["broker", "OAuth (via dashboard)"],
    ["api", "Built-in · API key"],
    ["catalog", "Catalog · unverified (models.dev)"],
    ["cli", "OAuth (via CLI token file)"],
    ["roadmap", "Roadmap (not yet available)"],
  ];
  var byGroup = {};
  groups.forEach(function (g) { var og = document.createElement("optgroup"); og.label = g[1]; byGroup[g[0]] = og; });
  var shown = 0;
  _directPresets.forEach(function (p) {
    var label = p.name + (p.catalog ? " · unverified" : "");
    if (query && (label + " " + p.key + " " + (p.base_url || "")).toLowerCase().indexOf(query) < 0) return;
    var opt = document.createElement("option");
    opt.value = p.key;
    opt.textContent = label;
    var group = p.status === "roadmap" ? "roadmap"
      : _isSubprocessPreset(p) ? "sub"
      : _isBrokerPreset(p) ? "broker"
      : _isCliPreset(p) ? "cli"
      : p.catalog ? "catalog" : "api";
    if (group === "roadmap") opt.disabled = true;
    byGroup[group].appendChild(opt);
    shown++;
  });
  groups.forEach(function (g) { if (byGroup[g[0]].children.length) select.appendChild(byGroup[g[0]]); });
  var count = document.getElementById("dp-search-count");
  count.textContent = query ? shown + " provider" + (shown === 1 ? "" : "s") + " match" : _directPresets.length + " providers available";
  if (previous && Array.prototype.some.call(select.options, function (o) { return o.value === previous; })) select.value = previous;
  if (!shown) {
    document.getElementById("dp-setup-guide").innerHTML = '<p class="muted">No provider matches. Add it as a custom endpoint with <strong>+ Add Custom Provider</strong>.</p>';
    document.getElementById("btn-dp-create").disabled = true;
    document.getElementById("btn-dp-test").disabled = true;
    return;
  }
  document.getElementById("btn-dp-test").disabled = false;
  if (select.value !== previous) _renderPresetView();
}

function _isCliPreset(preset) {
  return preset && preset.auth_source && preset.auth_source.indexOf("cli:") === 0;
}

function _isBrokerPreset(preset) {
  return preset && preset.auth_source && preset.auth_source.indexOf("broker:") === 0;
}

function _isSubprocessPreset(preset) {
  return preset && preset.auth_source && preset.auth_source.indexOf("subprocess:") === 0;
}

function _isOAuthPreset(preset) {
  return _isCliPreset(preset) || _isBrokerPreset(preset) || _isSubprocessPreset(preset);
}

function _copyToClipboard(text, btn) {
  navigator.clipboard.writeText(text).then(function () {
    var orig = btn.textContent;
    btn.textContent = "Copied!";
    setTimeout(function () { btn.textContent = orig; }, 1500);
  });
}

function _renderSetupSteps(preset) {
  if (!preset.setup_steps || !preset.setup_steps.length) return "";
  var html = '<div style="background:var(--bg-secondary); border:1px solid var(--border-strong); border-radius:8px; padding:0.75rem 1rem;">';
  html += '<div style="font-weight:600; font-size:0.85rem; margin-bottom:0.5rem; color:var(--text);">Setup guide</div>';
  html += '<ol style="margin:0; padding-left:1.25rem; font-size:0.85rem; line-height:1.5;">';
  preset.setup_steps.forEach(function (step, i) {
    html += '<li style="margin-bottom:0.6rem;">';
    html += '<strong>' + _escape(step.title) + '</strong>';
    if (step.description) {
      html += '<div style="color:var(--text-muted); font-size:0.8rem; margin-top:0.15rem;">' + _escape(step.description) + '</div>';
    }
    if (step.link) {
      html += '<div style="margin-top:0.2rem;"><a href="' + _escape(_safeHref(step.link)) + '" target="_blank" rel="noopener noreferrer" style="color:var(--accent-dark); font-size:0.8rem;">' + _escape(step.link) + ' →</a></div>';
    }
    if (step.command) {
      var cmdId = "dp-cmd-" + i;
      html += '<div style="margin-top:0.3rem; display:flex; gap:0.4rem; align-items:center;">';
      html += '<code id="' + cmdId + '" style="flex:1; background:var(--code-bg); color:var(--code-text); padding:0.35rem 0.6rem; border-radius:4px; font-size:0.78rem; font-family:var(--mono);">' + _escape(step.command) + '</code>';
      html += '<button type="button" data-cmd="' + _escape(step.command) + '" class="btn btn-ghost btn-sm dp-copy-btn" style="font-size:0.75rem; padding:0.2rem 0.5rem;">Copy</button>';
      html += '</div>';
    }
    html += '</li>';
  });
  html += '</ol></div>';
  return html;
}

function _escape(s) {
  if (s == null) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function _safeHref(s) {
  if (s == null) return "#";
  try {
    var u = new URL(String(s), window.location.href);
    if (u.protocol !== "http:" && u.protocol !== "https:") return "#";
    return u.toString();
  } catch (e) {
    return "#";
  }
}

async function _renderPresetView() {
  var key = document.getElementById("dp-preset").value;
  var preset = _directPresets.find(function (p) { return p.key === key; });
  var guideEl = document.getElementById("dp-setup-guide");
  var apiKeyInput = document.getElementById("dp-api-key");
  var checkBtn = document.getElementById("btn-dp-check");
  var createBtn = document.getElementById("btn-dp-create");
  var statusEl = document.getElementById("dp-status");
  if (!preset) { guideEl.innerHTML = ""; return; }

  // Header info
  var header = '';
  if (preset.base_url) header += '<div style="font-size:0.75rem; color:var(--text-muted); margin-bottom:0.5rem;">Endpoint : <code>' + _escape(preset.base_url) + '</code>';
  if (preset.default_model) header += ' · Default model : <code>' + _escape(preset.default_model) + '</code>';
  if (preset.base_url) header += '</div>';
  if (preset.reason) header += '<div style="background:var(--orange-bg); border:1px solid var(--orange-border); color:var(--orange-text); padding:0.5rem 0.75rem; border-radius:6px; margin-bottom:0.5rem; font-size:0.85rem;">⚠️ ' + _escape(preset.reason) + '</div>';

  // Setup steps
  guideEl.innerHTML = header + _renderSetupSteps(preset);

  // Wire up copy buttons
  guideEl.querySelectorAll(".dp-copy-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      _copyToClipboard(btn.getAttribute("data-cmd"), btn);
    });
  });

  statusEl.innerHTML = "";

  if (preset.status === "roadmap") {
    apiKeyInput.style.display = "none";
    checkBtn.style.display = "none";
    createBtn.textContent = "Not available";
    createBtn.disabled = true;
  } else if (_isSubprocessPreset(preset)) {
    // Subprocess CLI — OpenClaw-style spawn. Verify the binary is installed.
    apiKeyInput.style.display = "none";
    apiKeyInput.value = "";
    checkBtn.style.display = "";
    checkBtn.textContent = "Verify CLI";
    createBtn.textContent = "Create (use local CLI)";
    createBtn.disabled = true;
    await _checkCLI(preset);  // GET /admin/direct-providers/{key}/check handles subprocess
  } else if (_isBrokerPreset(preset)) {
    // OAuth broker — plugboard handles the flow directly
    apiKeyInput.style.display = "none";
    apiKeyInput.value = "";
    checkBtn.style.display = "none";
    var label = preset.name.indexOf("Google") >= 0 ? "Google"
              : preset.name.indexOf("GitHub") >= 0 || preset.name.indexOf("Copilot") >= 0 ? "GitHub"
              : preset.name.indexOf("ChatGPT") >= 0 || preset.name.indexOf("OpenAI") >= 0 ? "OpenAI"
              : preset.oauth_source || "Provider";
    createBtn.textContent = "Connect with " + label;
    createBtn.disabled = false;
    await _checkBrokerConnection(preset);
  } else if (_isCliPreset(preset)) {
    // OAuth: hide API key, show Check CLI button
    apiKeyInput.style.display = "none";
    apiKeyInput.value = "";
    checkBtn.style.display = "";
    checkBtn.textContent = "Check CLI";
    createBtn.textContent = "Create (use CLI token)";
    createBtn.disabled = true;  // require check first
    // Auto-check on preset selection
    await _checkCLI(preset);
  } else {
    // API key
    apiKeyInput.style.display = "";
    apiKeyInput.placeholder = "API key (paste here)";
    checkBtn.style.display = "none";
    createBtn.textContent = "Test and create";
    createBtn.disabled = false;
  }
  var apiKeyPreset = preset.status !== "roadmap" && !_isOAuthPreset(preset);
  document.getElementById("btn-dp-test").style.display = apiKeyPreset ? "" : "none";
  document.getElementById("dp-test-token-row").style.display = apiKeyPreset ? "" : "none";
  document.getElementById("dp-test-token").checked = false;
  _dpTested = null;
  if (preset.catalog) {
    guideEl.insertAdjacentHTML("afterbegin", '<div class="alert alert-warning small" style="margin-bottom:0.5rem;">Community preset from models.dev, not verified by Plugboard. Test the key before relying on it.</div>');
  }
}

var _dpTested = null;  // {key, ok} for the preset/key pair last tested

var _TEST_LABELS = {
  ok: "Connected",
  ok_no_models: "Key valid, but the provider lists no model",
  invalid_key: "Invalid key",
  not_openai_compatible: "Not OpenAI-compatible",
  unreachable: "Unreachable",
};

async function _testDirectProvider() {
  var presetKey = document.getElementById("dp-preset").value;
  var apiKey = document.getElementById("dp-api-key").value.trim();
  var statusEl = document.getElementById("dp-status");
  if (!apiKey) { statusEl.innerHTML = '<span class="text-red">Enter an API key first.</span>'; return null; }
  statusEl.innerHTML = '<span class="muted">Testing the connection…</span>';
  var testBtn = document.getElementById("btn-dp-test");
  var createBtn = document.getElementById("btn-dp-create");
  testBtn.disabled = true;
  createBtn.disabled = true;
  try {
    var res = await api("POST", "/admin/direct-providers/test", {
      preset_key: presetKey, api_key: apiKey, send_test_token: document.getElementById("dp-test-token").checked,
    });
    var r = await res.json();
    if (!res.ok) throw new Error(r.detail || ("HTTP " + res.status));
    var ok = r.status === "ok" || r.status === "ok_no_models";
    var label = _TEST_LABELS[r.status] || r.status;
    var html = '<span class="' + (r.status === "ok" ? "text-green" : ok ? "text-orange" : "text-red") + '">' +
      (ok ? "✓ " : "✗ ") + _escape(label) + '</span> <span class="muted">' + _escape(r.message || "") + '</span>';
    if (r.completion_status != null) html += ' <span class="muted">· test token: HTTP ' + _escape(r.completion_status) + '</span>';
    if (r.docs) html += ' <span class="muted small">See ' + _escape(r.docs) + '</span>';
    if (!ok) html += ' <button type="button" class="btn btn-sm" id="dp-save-anyway">Save anyway</button>';
    statusEl.innerHTML = html;
    var anyway = document.getElementById("dp-save-anyway");
    if (anyway) anyway.addEventListener("click", function () { _createDirectProvider(true); });
    _dpTested = { key: presetKey + "|" + apiKey, ok: ok };
    return ok;
  } catch (e) {
    statusEl.innerHTML = '<span class="text-red">✗ Test failed: ' + _escape(e.message) + '</span> <button type="button" class="btn btn-sm" id="dp-save-anyway">Save anyway</button>';
    document.getElementById("dp-save-anyway").addEventListener("click", function () { _createDirectProvider(true); });
    return false;
  } finally {
    testBtn.disabled = false;
    createBtn.disabled = false;
  }
}

async function _checkBrokerConnection(preset) {
  // Display existing connection status if already connected.
  var statusEl = document.getElementById("dp-status");
  var createBtn = document.getElementById("btn-dp-create");
  try {
    var res = await api("GET", "/admin/oauth/connections");
    var data = await res.json();
    var match = (data.connections || []).find(function (c) {
      return c.source === preset.oauth_source;
    });
    if (match) {
      statusEl.innerHTML = '<span style="color:var(--green);">✓ Already connected as <strong>'
        + _escape(match.account) + '</strong></span>'
        + ' <button type="button" class="btn btn-ghost btn-sm" id="dp-reconnect-btn" style="margin-left:0.5rem;">Reconnect</button>';
      createBtn.textContent = "Provider ready";
      createBtn.disabled = false;
      var reconnectBtn = document.getElementById("dp-reconnect-btn");
      if (reconnectBtn) {
        reconnectBtn.addEventListener("click", function () { _startBrokerFlow(preset); });
      }
    } else {
      statusEl.innerHTML = '<span style="color:var(--text-muted);">Not connected yet. Click the button to start.</span>';
    }
  } catch (e) { /* ignore */ }
}

async function _checkCLI(preset) {
  var statusEl = document.getElementById("dp-status");
  var createBtn = document.getElementById("btn-dp-create");
  statusEl.innerHTML = '<span style="color:var(--text-muted);">Checking CLI...</span>';
  try {
    var res = await api("GET", "/admin/direct-providers/" + preset.key + "/check");
    var status = await res.json();
    if (status.ok) {
      var label;
      if (status.auth_type === "subprocess") {
        label = '✓ CLI found: <code>' + _escape(status.binary || status.kind)
              + '</code>' + (status.version ? ' (' + _escape(status.version) + ')' : '');
      } else {
        label = '✓ Logged in as <strong>' + _escape(status.account || "user") + '</strong>';
      }
      statusEl.innerHTML = '<span style="color:var(--green);">' + label + '</span>';
      createBtn.disabled = false;
    } else {
      statusEl.innerHTML = '<span style="color:var(--red);">✗ ' + _escape(status.error || "Not available") + '</span>';
      createBtn.disabled = true;
    }
  } catch (e) {
    statusEl.innerHTML = '<span style="color:var(--red);">✗ Check failed: ' + _escape(e.message) + '</span>';
    createBtn.disabled = true;
  }
}

document.getElementById("btn-add-direct-provider").addEventListener("click", async function () {
  directProviderForm.style.display = "block";
  document.getElementById("dp-api-key").value = "";
  document.getElementById("dp-status").innerHTML = "";
  await _loadDirectPresets();
});

document.getElementById("btn-dp-cancel").addEventListener("click", function () {
  directProviderForm.style.display = "none";
});

document.getElementById("btn-dp-check").addEventListener("click", async function () {
  var presetKey = document.getElementById("dp-preset").value;
  var preset = _directPresets.find(function (p) { return p.key === presetKey; });
  if (preset) await _checkCLI(preset);
});

document.getElementById("btn-dp-test").addEventListener("click", _testDirectProvider);

document.getElementById("btn-dp-create").addEventListener("click", function () { _createDirectProvider(false); });

async function _createDirectProvider(skipTest) {
  var presetKey = document.getElementById("dp-preset").value;
  var preset = _directPresets.find(function (p) { return p.key === presetKey; });
  var apiKey = document.getElementById("dp-api-key").value.trim();
  if (!presetKey) {
    toast("Select a provider", "error");
    return;
  }
  // Broker preset: trigger OAuth flow directly from dashboard
  if (_isBrokerPreset(preset)) {
    await _startBrokerFlow(preset);
    return;
  }
  var isOauth = _isOAuthPreset(preset);
  if (!isOauth && !apiKey) {
    toast("Enter an API key", "error");
    return;
  }
  // API-key presets are tested first; a failed test offers "Save anyway".
  if (!isOauth && !skipTest) {
    var tested = _dpTested && _dpTested.key === presetKey + "|" + apiKey ? _dpTested.ok : await _testDirectProvider();
    if (!tested) return;
  }
  try {
    var res = await api("POST", "/admin/direct-providers/" + presetKey, { api_key: isOauth ? "" : apiKey });
    if (res.status === 201) {
      var data = await res.json();
      toast("Provider created: " + data.name, "success");
      directProviderForm.style.display = "none";
      _markDiscovering(data.id, (data.models || []).length);
      loadProviders();
    } else {
      var err = await res.json();
      toast("Error: " + (err.detail || res.status), "error");
    }
  } catch (e) {
    toast("Creation failed: " + e.message, "error");
  }
}

// A provider created from a preset discovers its models in the background.
var _discovering = {};  // id -> {since, count}

function _markDiscovering(id, count) {
  if (!id) return;
  _discovering[id] = { since: Date.now(), count: count };
  [3000, 8000, 20000].forEach(function (ms) { setTimeout(loadProviders, ms); });
}

// ── Broker OAuth flows ──────────────────────────────────────────────────────

var _devicePollTimer = null;
var _oauthMsgHandler = null;

async function _startBrokerFlow(preset) {
  var statusEl = document.getElementById("dp-status");
  statusEl.innerHTML = '<span style="color:var(--text-muted);">Starting OAuth flow...</span>';
  try {
    var res = await api("POST", "/admin/oauth/start", { preset: preset.key });
    if (!res.ok) {
      var err = await res.json();
      throw new Error(err.detail || ("HTTP " + res.status));
    }
    var data = await res.json();
    if (data.flow === "authorization_code") {
      _runPopupFlow(preset, data);
    } else if (data.flow === "device_code") {
      _runDeviceFlow(preset, data);
    } else {
      throw new Error("Unknown flow type: " + data.flow);
    }
  } catch (e) {
    statusEl.innerHTML = '<span style="color:var(--red);">✗ ' + _escape(e.message) + '</span>';
    toast("OAuth start failed: " + e.message, "error");
  }
}

function _runPopupFlow(preset, data) {
  var statusEl = document.getElementById("dp-status");
  // noreferrer strips Referer; noopener is intentionally omitted so the parent
  // can still receive postMessage from the popup for the OAuth handshake.
  var popup = window.open(data.auth_url, "oauth_" + preset.oauth_source,
                          "width=600,height=720,left=200,top=100,noreferrer");
  if (!popup) {
    statusEl.innerHTML = '<span style="color:var(--red);">✗ Popup blocked. Allow popups for this site.</span>';
    toast("Popup blocked — allow popups and retry", "error");
    return;
  }
  statusEl.innerHTML = '<span style="color:var(--text-muted);">Waiting for authorization in the popup window...</span>';

  // Clean up any previous listener
  if (_oauthMsgHandler) window.removeEventListener("message", _oauthMsgHandler);

  _oauthMsgHandler = function (event) {
    if (event.origin !== window.location.origin) return;
    if (!event.data || typeof event.data !== "object") return;
    if (event.data.type === "oauth-success"
        && event.data.source === preset.oauth_source) {
      window.removeEventListener("message", _oauthMsgHandler);
      _oauthMsgHandler = null;
      statusEl.innerHTML = '<span style="color:var(--green);">✓ Connected as <strong>'
        + _escape(event.data.account) + '</strong></span>';
      toast("Connected: " + event.data.account, "success");
      directProviderForm.style.display = "none";
      loadProviders();
      if (typeof loadConnections === "function") loadConnections();
    } else if (event.data.type === "oauth-error"
               && event.data.source === preset.oauth_source) {
      window.removeEventListener("message", _oauthMsgHandler);
      _oauthMsgHandler = null;
      statusEl.innerHTML = '<span style="color:var(--red);">✗ OAuth failed: '
        + _escape(event.data.error) + '</span>';
      toast("OAuth failed: " + event.data.error, "error");
    }
  };
  window.addEventListener("message", _oauthMsgHandler);
}

function _runDeviceFlow(preset, data) {
  var modal = document.getElementById("device-flow-modal");
  var codeEl = document.getElementById("device-code");
  var openBtn = document.getElementById("device-open-btn");
  var copyBtn = document.getElementById("device-copy-btn");
  var cancelBtn = document.getElementById("device-cancel-btn");
  var deviceStatus = document.getElementById("device-status");

  codeEl.textContent = data.user_code || "----";
  deviceStatus.textContent = "Waiting for authorization...";
  modal.style.display = "flex";
  modal.classList.remove("hidden");

  var verificationUri = data.verification_uri || "https://github.com/login/device";
  openBtn.onclick = function () { window.open(verificationUri, "_blank"); };
  copyBtn.onclick = function () { _copyToClipboard(data.user_code, copyBtn); };
  cancelBtn.onclick = function () { _closeDeviceFlow(); };

  var interval = Math.max((data.interval || 5) * 1000, 2000);
  var state = data.state;
  var preset_key = preset.key;

  if (_devicePollTimer) clearInterval(_devicePollTimer);
  _devicePollTimer = setInterval(async function () {
    try {
      var res = await api("GET", "/admin/oauth/device/poll/" + state);
      if (!res.ok) return; // keep polling on transient errors
      var d = await res.json();
      if (d.status === "success") {
        _closeDeviceFlow();
        toast("Connected: " + (d.account || "user"), "success");
        directProviderForm.style.display = "none";
        loadProviders();
        if (typeof loadConnections === "function") loadConnections();
      } else if (d.status === "denied" || d.status === "expired") {
        _closeDeviceFlow();
        toast("OAuth " + d.status + ": " + (d.error || ""), "error");
      } else {
        deviceStatus.textContent = "Waiting for authorization... (" + d.status + ")";
      }
    } catch (e) { /* keep polling */ }
  }, interval);
}

function _closeDeviceFlow() {
  var modal = document.getElementById("device-flow-modal");
  modal.style.display = "none";
  modal.classList.add("hidden");
  if (_devicePollTimer) {
    clearInterval(_devicePollTimer);
    _devicePollTimer = null;
  }
}

providerForm.addEventListener("submit", async function (e) {
  e.preventDefault();
  var editId = document.getElementById("pf-edit-id").value;
  var body = {
    name: document.getElementById("pf-name").value.trim(),
    base_url: document.getElementById("pf-url").value.trim(),
    default_model: document.getElementById("pf-model").value.trim(),
    is_active: document.getElementById("pf-active").checked,
    timeout: parseFloat(document.getElementById("pf-timeout").value) || 60,
  };
  var tokenVal = document.getElementById("pf-token").value.trim();

  if (editId) {
    if (tokenVal) body.bearer_token = tokenVal;
    body.auto_discover = document.getElementById("pf-auto-discover").checked;
    body.pinned_models = document.getElementById("pf-pinned").value.split(",")
      .map(function (x) { return x.trim(); }).filter(Boolean);
    var upd = await api("PUT", "/providers/" + editId, body);
    if (!upd.ok) { var uerr = await upd.json(); toast("Update failed: " + (uerr.detail || upd.status), "error"); return; }
    toast("Provider updated", "success");
  } else {
    body.bearer_token = tokenVal;
    await api("POST", "/providers", body);
    toast("Provider created", "success");
  }
  providerFormContainer.classList.add("hidden");
  loadProviders();
});

async function loadProviders() {
  var container = document.getElementById("providers-table-container");
  container.innerHTML = ""; container.appendChild(skeleton(3));
  try {
    var res = await api("GET", "/providers");
    var providers = await res.json();
    var changes = {};
    try {
      var sres = await api("GET", "/admin/models/scheduler/status");
      if (sres.ok) changes = ((await sres.json()).last_changes || {}).providers || {};
    } catch (e) { /* status is optional */ }
    container.innerHTML = "";
    if (!providers.length) { container.appendChild(emptyState("—", "No providers configured.")); return; }
    var rows = providers.map(function (p) {
      var count = (p.models || []).length;
      var disc = _discovering[p.id];
      if (disc && (count !== disc.count || Date.now() - disc.since > 30000)) { delete _discovering[p.id]; disc = null; }
      var ch = changes[p.id] || {};
      var pending = Object.keys(ch.pending_removal || {});
      var modelsCell = h("td", null,
        disc ? h("span", { className: "badge badge-accent" }, "Discovering…") : (count ? count + " models" : "—"),
        p.auto_discover ? h("div", { className: "muted small" }, "auto-refresh") : null,
        pending.length ? h("div", { className: "muted small", title: pending.join(", ") }, pending.length + " missing, removed after the next refresh") : null,
        (ch.removed || []).length ? h("div", { className: "muted small", title: ch.removed.join(", ") }, ch.removed.length + " removed at last refresh") : null,
        ch.error ? h("div", { className: "text-red small" }, ch.error) : null);
      return h("tr", null,
        h("td", null, h("strong", null, p.name), h("br"), h("code", null, p.id),
          p.preset_key && p.preset_key.indexOf("catalog-") === 0 ? h("div", null, h("span", { className: "badge badge-orange" }, "catalog · unverified")) : null),
        h("td", null, h("code", null, p.base_url)),
        h("td", null, p.default_model || "—"),
        h("td", null, (p.timeout || 60) + "s"),
        h("td", null, p.is_active
          ? h("span", { className: "badge badge-green" }, "active")
          : h("span", { className: "badge badge-red" }, "inactive")),
        h("td", { id: "health-" + p.id }, "—"),
        modelsCell,
        h("td", null, h("div", { className: "btn-group" },
          h("button", { className: "btn btn-sm btn-primary", onClick: function () { discoverModels(p.id); } }, "Discover"),
          h("button", { className: "btn btn-sm", onClick: function () { checkProviderHealth(p.id); } }, "Ping"),
          h("button", { className: "btn btn-sm", onClick: function () { editProvider(p); } }, "Edit"),
          h("button", { className: "btn btn-sm btn-danger", onClick: function () { deleteProvider(p.id); } }, "Del"),
        )),
      );
    });
    container.appendChild(table(["Name / ID", "Base URL", "Default Model", "Timeout", "Status", "Health", "Models", "Actions"], rows));
  } catch (e) { container.innerHTML = ""; container.appendChild(h("div", { className: "alert alert-error" }, "Failed to load providers.")); }
}

function editProvider(p) {
  providerFormContainer.classList.remove("hidden");
  document.getElementById("pf-edit-id").value = p.id;
  document.getElementById("pf-name").value = p.name;
  document.getElementById("pf-url").value = p.base_url;
  document.getElementById("pf-model").value = p.default_model || "";
  document.getElementById("pf-active").checked = p.is_active;
  document.getElementById("pf-timeout").value = p.timeout || 60;
  document.getElementById("pf-auto-discover").checked = p.auto_discover === true;
  document.getElementById("pf-pinned").value = (p.pinned_models || []).join(", ");
  document.getElementById("pf-token").value = "";
  document.getElementById("pf-token").placeholder = "Leave empty to keep current";
  document.getElementById("pf-token").required = false;
  document.getElementById("pf-submit-btn").textContent = "Update";
  document.getElementById("pf-form-title").textContent = "Edit Provider";
}

async function deleteProvider(id) {
  if (!await confirmAction("Delete provider " + id + "?")) return;
  await api("DELETE", "/providers/" + id);
  toast("Provider deleted", "success");
  loadProviders();
}

async function discoverModels(id) {
  try {
    // Preview first: a manual discover removes missing models at once.
    var preview = await api("POST", "/providers/" + encodeURIComponent(id) + "/discover?dry_run=true");
    if (!preview.ok) { var perr = await preview.json(); toast("Discovery failed: " + (perr.detail || "Error"), "error"); return; }
    var pdata = await preview.json();
    var removed = pdata.removed || [];
    if (pdata.removals_allowed === false) toast("The provider's model API did not answer: models can be added, none will be removed.", "success");
    if (removed.length && !await confirmAction(
      "Discovery found " + pdata.discovered + " models. It will remove " + removed.length + " model(s) from " + id + ":\n\n" +
      removed.slice(0, 15).join("\n") + (removed.length > 15 ? "\n…" : "") + "\n\nContinue?")) return;
    var res = await api("POST", "/providers/" + encodeURIComponent(id) + "/discover");
    if (!res.ok) { var err = await res.json(); toast("Discovery failed: " + (err.detail || "Error"), "error"); return; }
    var data = await res.json();
    toast("Discovered " + data.discovered + " models (+" + (data.added || []).length + ", −" + (data.removed || []).length + ")", "success");
    loadProviders();
  } catch (err) { toast("Discovery error: " + err.message, "error"); }
}

async function checkProviderHealth(id) {
  var cell = document.getElementById("health-" + id);
  if (cell) cell.textContent = "...";
  try {
    var res = await api("GET", "/providers/" + id + "/health");
    var data = await res.json();
    if (cell) {
      cell.innerHTML = "";
      cell.appendChild(
        data.ok
          ? h("span", { className: "badge badge-green" }, "OK " + (data.status || ""))
          : h("span", { className: "badge badge-red" }, "DOWN")
      );
    }
  } catch (e) { if (cell) { cell.innerHTML = ""; cell.appendChild(h("span", { className: "badge badge-red" }, "ERROR")); } }
}

// ── Connections (OAuth broker) tab ──────────────────────────────────────────

async function loadConnections() {
  var container = document.getElementById("connections-table-container");
  if (!container) return;
  container.innerHTML = "";
  container.appendChild(skeleton(2));
  try {
    var res = await api("GET", "/admin/oauth/connections");
    var data = await res.json();
    var conns = data.connections || [];
    container.innerHTML = "";
    if (!conns.length) {
      container.appendChild(emptyState("—", "No OAuth connections. Go to Providers → Add Direct Provider and pick an 'OAuth via dashboard' preset."));
      return;
    }
    var rows = conns.map(function (c) {
      var expiresTxt = "—";
      if (c.expires_at) {
        var diff = c.expires_at - (Date.now() / 1000);
        if (diff > 0) {
          var mins = Math.round(diff / 60);
          expiresTxt = mins > 60 ? Math.round(mins / 60) + "h" : mins + "min";
        } else {
          expiresTxt = "expired (auto-refresh)";
        }
      }
      return h("tr", null,
        h("td", null, h("strong", null, c.source)),
        h("td", null, c.account || "—"),
        h("td", null, c.scope ? h("code", null, c.scope.slice(0, 50) + (c.scope.length > 50 ? "…" : "")) : "—"),
        h("td", null, expiresTxt),
        h("td", null, c.updated_at || "—"),
        h("td", null, h("div", { className: "btn-group" },
          h("button", {
            className: "btn btn-sm btn-danger",
            onClick: function () { disconnectOAuth(c.source); }
          }, "Disconnect"),
        )),
      );
    });
    container.appendChild(table(
      ["Source", "Account", "Scope", "Expires in", "Updated", "Actions"], rows));
  } catch (e) {
    container.innerHTML = "";
    container.appendChild(h("div", { className: "alert alert-error" }, "Failed to load connections: " + e.message));
  }
}

async function disconnectOAuth(source) {
  if (!await confirmAction("Disconnect " + source + "? Broker providers using this source will stop working until you reconnect.")) return;
  try {
    var res = await api("DELETE", "/admin/oauth/" + source);
    if (res.status === 204) {
      toast("Disconnected " + source, "success");
      loadConnections();
      loadProviders();
    } else {
      var err = await res.json();
      toast("Disconnect failed: " + (err.detail || res.status), "error");
    }
  } catch (e) { toast("Disconnect error: " + e.message, "error"); }
}

var _refreshConnBtn = document.getElementById("btn-refresh-connections");
if (_refreshConnBtn) _refreshConnBtn.addEventListener("click", loadConnections);

// ── Pricing ─────────────────────────────────────────────────────────────────
// The Pricing page and the auto-update panels live in auto_update.js.

function loadPricing() {
  if (typeof window.loadPricingPage === "function") return window.loadPricingPage();
}

// ── Monitoring ──────────────────────────────────────────────────────────────

document.getElementById("btn-refresh-monitoring").addEventListener("click", loadMonitoring);

async function loadMonitoring() {
  if (typeof window.renderAutoUpdate === "function") {
    window.renderAutoUpdate(document.getElementById("monitoring-auto-update"), { changesEl: document.getElementById("monitoring-changes") });
  }
  var summaryEl = document.getElementById("monitoring-summary");
  var providersEl = document.getElementById("monitoring-providers");
  var historyEl = document.getElementById("monitoring-history");
  summaryEl.innerHTML = ""; summaryEl.appendChild(skeleton(3));
  providersEl.innerHTML = ""; historyEl.innerHTML = "";

  try {
    var res = await api("GET", "/admin/monitoring");
    var data = await res.json();
    var s = data.summary || {};

    // Summary cards
    summaryEl.innerHTML = "";
    summaryEl.appendChild(statCard("", "Providers", String(s.total_providers || 0), "accent"));
    summaryEl.appendChild(statCard("", "Healthy", String(s.healthy || 0), "green"));
    summaryEl.appendChild(statCard("", "Unhealthy", String(s.unhealthy || 0), s.unhealthy > 0 ? "red" : "green"));

    // Provider details
    if (data.providers && data.providers.length) {
      var rows = data.providers.map(function (p) {
        var health = p.health || {};
        var met = p.metrics || {};
        var uptime24 = p.uptime_24h || {};
        var statusBadge = health.ok
          ? h("span", { className: "badge badge-green" }, "healthy")
          : h("span", { className: "badge badge-red" }, health.error ? "down" : "error");
        var uptimeStr = uptime24.uptime_pct != null ? uptime24.uptime_pct + "%" : "—";
        var latencyStr = health.latency_ms ? health.latency_ms + "ms" : "—";
        var errRate = met.error_rate_pct != null ? met.error_rate_pct + "%" : "—";

        return h("tr", null,
          h("td", null, h("strong", null, p.name || p.id), h("br"), h("code", null, p.id)),
          h("td", null, statusBadge),
          h("td", null, latencyStr),
          h("td", null, fmt(met.requests || 0)),
          h("td", null, errRate),
          h("td", null, uptimeStr),
          h("td", null, String(p.models_count || 0)),
        );
      });
      providersEl.appendChild(table(
        ["Provider", "Status", "Latency", "Requests", "Error Rate", "Uptime 24h", "Models"],
        rows
      ));
    } else {
      providersEl.appendChild(emptyState("—", "No providers configured."));
    }

    // History
    var histRes = await api("GET", "/admin/monitoring/history?limit=20");
    var histData = await histRes.json();
    var entries = histData.entries || [];
    if (entries.length) {
      var hrows = entries.map(function (e) {
        var statusBadge = e.is_healthy
          ? h("span", { className: "badge badge-green" }, "OK")
          : h("span", { className: "badge badge-red" }, "DOWN");
        return h("tr", null,
          h("td", null, e.ts ? e.ts.slice(0, 19).replace("T", " ") : "—"),
          h("td", null, h("code", null, e.provider_id)),
          h("td", null, statusBadge),
          h("td", null, e.latency_ms ? e.latency_ms.toFixed(0) + "ms" : "—"),
          h("td", null, e.error || "—"),
        );
      });
      historyEl.appendChild(table(["Time", "Provider", "Status", "Latency", "Error"], hrows));
    } else {
      historyEl.appendChild(emptyState("—", "No monitoring history yet."));
    }

  } catch (err) {
    summaryEl.innerHTML = "";
    summaryEl.appendChild(h("div", { className: "alert alert-error" }, "Failed to load monitoring: " + err.message));
  }
}

// ── Jobs ────────────────────────────────────────────────────────────────────

async function loadJobs() {
  var listEl = document.getElementById("jobs-list");
  var logEl = document.getElementById("job-log-container");
  listEl.innerHTML = ""; listEl.appendChild(skeleton(2));
  logEl.innerHTML = "";

  try {
    var res = await api("GET", "/admin/jobs");
    var jobs = await res.json();
    listEl.innerHTML = "";

    if (!jobs.length) { listEl.appendChild(emptyState("—", "No background jobs.")); return; }

    var rows = jobs.map(function (j) {
      var statusBadge;
      if (j.last_status === "success" || j.last_status === "running") statusBadge = h("span", { className: "badge badge-green" }, j.last_status);
      else if (j.last_status === "error" || j.last_status === "stopped") statusBadge = h("span", { className: "badge badge-red" }, j.last_status);
      else if (j.last_status === "stale") statusBadge = h("span", { className: "badge badge-orange" }, "stale");
      else statusBadge = h("span", { className: "badge badge-muted" }, j.last_status || "never run");

      return h("tr", null,
        h("td", null, h("strong", null, j.name), h("br"), h("span", { className: "muted" }, j.description)),
        h("td", null, h("code", null, j.schedule)),
        h("td", null, j.next_run || "—"),
        h("td", null, j.last_run || "—"),
        h("td", null, statusBadge),
        h("td", null, h("div", { className: "btn-group" },
          (j.next_run === "continuous"
            ? h("button", { className: "btn btn-sm btn-primary", disabled: "true", title: "Runs continuously; it cannot be started by hand" }, "Run now")
            : h("button", { className: "btn btn-sm btn-primary", onClick: function () { runJob(j.id); } }, "Run now")),
          h("button", { className: "btn btn-sm", onClick: function () { viewJobLog(j.id); } }, "Details"),
        )),
      );
    });
    listEl.appendChild(table(["Job", "Schedule", "Next run", "Last run", "Status", "Actions"], rows));

  } catch (err) {
    listEl.innerHTML = "";
    listEl.appendChild(h("div", { className: "alert alert-error" }, "Failed to load jobs: " + err.message));
  }
}

async function runJob(jobId) {
  
  try {
    var res = await api("POST", "/admin/jobs/" + jobId + "/run");
    var data = await res.json();
    // FastAPI errors come back as {detail}; job-level failures as {status, error}.
    // Read all shapes so a 404/500 shows the real reason instead of "undefined".
    var jobName = data.name || jobId;
    var errMsg = data.detail || data.error || data.errors || "";
    if (res.ok && (data.status === "started" || data.status === "running")) {
      toast(jobName + (data.status === "started" ? " started" : " is already running"), "success");
    } else {
      toast(jobName + " failed: " + (errMsg || "unknown error"), "error");
    }
    loadJobs();
    // Show output in log area
    var logEl = document.getElementById("job-log-container");
    logEl.innerHTML = "";
    logEl.appendChild(h("div", { className: "card" },
      h("div", { className: "card-header" }, h("span", { className: "card-title" }, jobName + " — Output")),
      h("pre", { style: "font-family:var(--mono);font-size:0.8rem;white-space:pre-wrap;max-height:400px;overflow:auto;color:var(--text-secondary);margin:0;" }, data.output || errMsg || "No output")
    ));
  } catch (err) { toast("Failed to run job: " + err.message, "error"); }
}

async function viewJobLog(jobId) {
  var logEl = document.getElementById("job-log-container");
  logEl.innerHTML = ""; logEl.appendChild(skeleton(3));
  try {
    var res = await api("GET", "/admin/jobs/" + jobId + "/log?lines=100");
    var data = await res.json();
    logEl.innerHTML = "";
    logEl.appendChild(h("div", { className: "card" },
      h("div", { className: "card-header" },
        h("span", { className: "card-title" }, data.name + " — last result"),
        h("span", { className: "badge badge-" + (data.status === "success" ? "green" : data.status === "error" ? "red" : "muted") }, data.status || "unknown"),
      ),
      h("pre", { style: "font-family:var(--mono);font-size:0.78rem;white-space:pre-wrap;max-height:500px;overflow:auto;color:var(--text-secondary);margin:0;padding:1rem;background:var(--bg);border-radius:8px;" }, data.output || "No result yet.")
    ));
  } catch (err) {
    logEl.innerHTML = "";
    logEl.appendChild(h("div", { className: "alert alert-error" }, "Failed to load log: " + err.message));
  }
}


// ── Ollama ──────────────────────────────────────────────────────────────────

var _ollamaInitDone = false;

function _initOllamaEvents() {
  if (_ollamaInitDone) return;
  _ollamaInitDone = true;
  var pullForm = document.getElementById("ollama-pull-form");
  document.getElementById("btn-pull-model").addEventListener("click", function () {
    pullForm.style.display = pullForm.style.display === "none" ? "block" : "none";
  });
  document.getElementById("btn-cancel-pull").addEventListener("click", function () {
    pullForm.style.display = "none";
    document.getElementById("ollama-pull-progress").style.display = "none";
    document.getElementById("ollama-model-name").value = "";
  });
  document.getElementById("btn-start-pull").addEventListener("click", function () {
    var name = document.getElementById("ollama-model-name").value.trim();
    if (name) pullOllamaModel(name);
  });
  document.getElementById("btn-refresh-ollama").addEventListener("click", loadOllama);
}

async function loadOllama() {
  _initOllamaEvents();
  await loadOllamaStatus();
  await loadOllamaModels();
}

async function loadOllamaStatus() {
  var el = document.getElementById("ollama-status");
  try {
    var res = await api("GET", "/admin/ollama/status");
    var d = await res.json();
    el.innerHTML = "";
    el.appendChild(h("div", { className: "stat-card" },
      h("div", { className: "stat-label" }, "Status"),
      h("div", { className: "stat-value" }, d.available ? "Online" : "Offline"),
      h("span", { className: "badge badge-" + (d.available ? "green" : "red") }, d.available ? "connected" : "unreachable")
    ));
    el.appendChild(h("div", { className: "stat-card" },
      h("div", { className: "stat-label" }, "URL"),
      h("div", { className: "stat-value", style: "font-size:0.85rem;" }, d.url)
    ));
    el.appendChild(h("div", { className: "stat-card" },
      h("div", { className: "stat-label" }, "Provider"),
      h("div", { className: "stat-value" }, d.provider_id || "not registered"),
      h("span", { className: "badge badge-muted" }, d.provider_type || "n/a")
    ));
  } catch (err) {
    el.innerHTML = "";
    el.appendChild(h("div", { className: "alert alert-error" }, "Failed to load Ollama status: " + err.message));
  }
}

function _formatSize(bytes) {
  if (!bytes) return "-";
  var gb = bytes / (1024 * 1024 * 1024);
  if (gb >= 1) return gb.toFixed(1) + " GB";
  var mb = bytes / (1024 * 1024);
  return mb.toFixed(0) + " MB";
}

async function loadOllamaModels() {
  var el = document.getElementById("ollama-models-container");
  try {
    var res = await api("GET", "/admin/ollama/models");
    var data = await res.json();
    var models = data.models || [];
    if (models.length === 0) {
      el.innerHTML = "";
      el.appendChild(h("div", { className: "muted", style: "padding:1rem;" }, "No models installed. Use \"+ Pull Model\" to download one."));
      return;
    }
    var table = h("table", { className: "data-table" },
      h("thead", null,
        h("tr", null,
          h("th", null, "Model"),
          h("th", null, "Size"),
          h("th", null, "Modified"),
          h("th", null, "Actions")
        )
      )
    );
    var tbody = h("tbody");
    models.forEach(function (m) {
      var name = m.name || m.model || "-";
      var size = _formatSize(m.size);
      var modified = m.modified_at ? new Date(m.modified_at).toLocaleDateString() : "-";
      tbody.appendChild(h("tr", null,
        h("td", null,
          h("span", { style: "font-weight:600;" }, name)
        ),
        h("td", null, size),
        h("td", null, modified),
        h("td", null,
          h("button", { className: "btn btn-ghost btn-sm", onClick: function () { showOllamaModelInfo(name); } }, "Info"),
          h("button", { className: "btn btn-ghost btn-sm", style: "color:var(--danger);", onClick: function () { deleteOllamaModel(name); } }, "Delete")
        )
      ));
    });
    table.appendChild(tbody);
    el.innerHTML = "";
    el.appendChild(table);
  } catch (err) {
    el.innerHTML = "";
    el.appendChild(h("div", { className: "alert alert-error" }, "Failed to load models: " + err.message));
  }
}

async function pullOllamaModel(name) {
  var progressEl = document.getElementById("ollama-pull-progress");
  var fillEl = document.getElementById("ollama-progress-fill");
  var statusEl = document.getElementById("ollama-pull-status");
  progressEl.style.display = "block";
  fillEl.style.width = "0%";
  statusEl.textContent = "Starting pull for " + name + "...";

  try {
    var res = await fetch("/admin/ollama/models/pull", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name }),
    });
    var reader = res.body.getReader();
    var decoder = new TextDecoder();
    var buf = "";
    while (true) {
      var result = await reader.read();
      var pullDone = result.done;
      // Flush + buffer partial NDJSON lines across chunks (same defect as the
      // _pgSend SSE parser): the final {"status":"complete"} can arrive without
      // a trailing newline and would otherwise be dropped, freezing the bar.
      buf += pullDone ? decoder.decode() : decoder.decode(result.value, { stream: true });
      var lines = buf.split("\n");
      buf = pullDone ? "" : lines.pop();
      for (var i = 0; i < lines.length; i++) {
        if (!lines[i].trim()) continue;
        try {
          var progress = JSON.parse(lines[i]);
          statusEl.textContent = progress.status || "";
          if (progress.total && progress.completed) {
            var pct = Math.round((progress.completed / progress.total) * 100);
            fillEl.style.width = pct + "%";
          }
          if (progress.status === "complete") {
            fillEl.style.width = "100%";
            statusEl.textContent = "Model " + name + " pulled successfully!";
            toast("Model " + name + " pulled", "success");
          }
          if (progress.status === "error") {
            statusEl.textContent = "Error: " + (progress.error || "unknown");
            toast("Pull failed: " + (progress.error || "unknown"), "error");
          }
        } catch (e) { /* skip invalid JSON */ }
      }
      if (pullDone) break;
    }
    await loadOllamaModels();
  } catch (err) {
    statusEl.textContent = "Pull failed: " + err.message;
    toast("Pull failed: " + err.message, "error");
  }
}

async function deleteOllamaModel(name) {
  if (!await confirmAction("Delete model '" + name + "' from Ollama? This cannot be undone.")) return;
  try {
    var res = await api("DELETE", "/admin/ollama/models/" + encodeURIComponent(name));
    if (res.status === 204) {
      toast("Model " + name + " deleted", "success");
      await loadOllamaModels();
    } else {
      var data = await res.json();
      toast("Delete failed: " + (data.detail || "unknown error"), "error");
    }
  } catch (err) {
    toast("Delete failed: " + err.message, "error");
  }
}

async function showOllamaModelInfo(name) {
  try {
    var res = await api("GET", "/admin/ollama/models/" + encodeURIComponent(name));
    var data = await res.json();
    var details = data.details || {};
    var info = "Model: " + name + "\n";
    info += "Format: " + (details.format || "-") + "\n";
    info += "Family: " + (details.family || "-") + "\n";
    info += "Parameters: " + (details.parameter_size || "-") + "\n";
    info += "Quantization: " + (details.quantization_level || "-") + "\n";
    if (data.modelfile) info += "\nModelfile:\n" + data.modelfile.slice(0, 500);
    alert(info);
  } catch (err) {
    toast("Failed to get model info: " + err.message, "error");
  }
}


// ── Playground (Chat) ───────────────────────────────────────────────────────

var _pgInitDone = false;
var _pgHistory = [];  // {role, content}

function _initPlaygroundEvents() {
  if (_pgInitDone) return;
  _pgInitDone = true;

  document.getElementById("btn-send-chat").addEventListener("click", _pgSend);
  document.getElementById("btn-clear-chat").addEventListener("click", _pgClear);
  document.getElementById("pg-input").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      _pgSend();
    }
  });

  document.getElementById("btn-check-internet").addEventListener("click", async function () {
    var modelSelect = document.getElementById("pg-model");
    var modelId = modelSelect.value;
    if (!modelId) {
      toast("Please select a model first.", "error");
      return;
    }

    var btn = document.getElementById("btn-check-internet");
    var oldText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "⏳ Probing...";

    toast("Checking internet access for " + modelId + "...", "success");

    try {
      var res = await api("POST", "/admin/models/check-internet", { model_id: modelId, persist: true });
      var data = await res.json();

      if (data.status === "quarantined") {
        toast("Model not found and has been quarantined. Reloading selector...", "error");
        await loadPlayground();
      } else if (data.status === "online" || data.internet_access) {
        toast("Verified: Model has internet access! 🌐", "success");
        await loadPlayground();
        document.getElementById("pg-model").value = modelId;
      } else {
        toast("Verified: Model has no internet access.", "warning");
        await loadPlayground();
        document.getElementById("pg-model").value = modelId;
      }
    } catch (err) {
      toast("Failed to check internet access: " + err.message, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  });

  document.getElementById("btn-check-internet-all").addEventListener("click", async function () {
    var btn = document.getElementById("btn-check-internet-all");
    var oldText = btn.textContent;
    var statusEl = document.getElementById("pg-status");
    var currentSelection = document.getElementById("pg-model").value;

    if (!await confirmAction("Probe ALL models for live internet access? This calls each model with a real prompt — may take a few minutes and consume some quota. Persistence: results saved to providers.json.")) return;

    btn.disabled = true;
    btn.textContent = "⏳ Probing all...";
    statusEl.textContent = "Bulk internet check in progress...";

    try {
      var res = await api("POST", "/admin/models/check-internet/all", { persist: true });
      var data = await res.json();
      var online = data.online_count || 0;
      var offline = data.offline_count || 0;
      var failed = data.failed_count || 0;
      var total = data.total || 0;
      var elapsed = data.elapsed_s ? (" in " + data.elapsed_s.toFixed(1) + "s") : "";
      toast("Bulk check done" + elapsed + ": " + online + " 🌐 online / " + offline + " offline / " + failed + " failed (total " + total + ")", "success");
      statusEl.textContent = "Last bulk check: " + online + " online / " + offline + " offline / " + failed + " failed";
      // Log per-model results to console for verification (user requested ability to check correctness)
      console.table((data.results || []).map(function (r) {
        return { model: r.model_id, internet: r.internet_access, status: r.status, error: r.error || "" };
      }));
      await loadPlayground();
      if (currentSelection) document.getElementById("pg-model").value = currentSelection;
    } catch (err) {
      toast("Bulk check failed: " + err.message, "error");
      statusEl.textContent = "Bulk check failed: " + err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  });
}

async function loadPlayground() {
  _initPlaygroundEvents();
  // Load models into selector
  var select = document.getElementById("pg-model");
  try {
    var res = await api("GET", "/v1/models");
    var data = await res.json();
    var models = (data.data || data || []);
    select.innerHTML = "";
    // Group by provider
    var groups = {};
    models.forEach(function (m) {
      var owner = m.owned_by || m.provider || "unknown";
      if (!groups[owner]) groups[owner] = [];
      groups[owner].push(m);
    });
    Object.keys(groups).sort().forEach(function (owner) {
      var optgroup = document.createElement("optgroup");
      optgroup.label = owner;
      groups[owner].forEach(function (m) {
        var opt = document.createElement("option");
        opt.value = m.id;
        // Short display name
        var name = m.id;
        if (name.includes(":")) name = name.split(":").slice(1).join(":");

        // Single internet-access icon: 🌐 = online model, 💻 = local/offline model
        var icon = m.internet_access ? "🌐 " : "💻 ";

        opt.textContent = icon + name;

        // Tooltip: probed timestamp + reason if offline
        var titleParts = [];
        if (m.last_probed_at) {
          var ts = m.last_probed_at.replace("T", " ").replace(/\.\d+Z$/, " UTC").replace("Z", " UTC");
          titleParts.push("Probed at " + ts);
        } else {
          titleParts.push("Never probed");
        }
        if (m.connected === false && m.reason) {
          titleParts.push("Reason: " + m.reason);
        }
        opt.title = titleParts.join(" — ");

        optgroup.appendChild(opt);
      });
      select.appendChild(optgroup);
    });
  } catch (err) {
    select.innerHTML = "<option value=''>Failed to load models</option>";
  }
}

function _pgAddMessage(role, content) {
  var container = document.getElementById("pg-messages");
  // Remove placeholder
  var placeholder = container.querySelector(".muted");
  if (placeholder) placeholder.remove();

  var bubble = document.createElement("div");
  bubble.style.cssText = role === "user"
    ? "align-self:flex-end; background:var(--accent); color:var(--on-accent); border-radius:12px 12px 2px 12px; padding:0.6rem 0.9rem; max-width:75%; white-space:pre-wrap; font-size:0.9rem;"
    : "align-self:flex-start; background:var(--bg-card); border:1px solid var(--border-strong); border-radius:12px 12px 12px 2px; padding:0.6rem 0.9rem; max-width:75%; white-space:pre-wrap; font-size:0.9rem;";
  bubble.textContent = content;
  bubble.dataset.role = role;
  bubble.dataset.testid = "message-bubble";  // live-test UI selector (C9 / P4-05)
  container.appendChild(bubble);
  container.scrollTop = container.scrollHeight;
  return bubble;
}

async function _pgSend() {
  var input = document.getElementById("pg-input");
  var model = document.getElementById("pg-model").value;
  var message = input.value.trim();
  if (!message || !model) return;

  var useStream = document.getElementById("pg-stream").checked;
  var temperature = parseFloat(document.getElementById("pg-temp").value) || 0.7;
  var maxTokens = parseInt(document.getElementById("pg-max-tokens").value) || 2048;
  var statusEl = document.getElementById("pg-status");

  // Add user message
  _pgHistory.push({ role: "user", content: message });
  _pgAddMessage("user", message);
  input.value = "";

  // Add assistant placeholder
  var assistantBubble = _pgAddMessage("assistant", "...");
  statusEl.textContent = "Sending to " + model + "...";
  statusEl.dataset.state = "sending";  // live-test completion hook (C9)

  var body = {
    model: model,
    messages: _pgHistory.slice(),
    temperature: temperature,
    max_tokens: maxTokens,
    stream: useStream,
  };

  var t0 = performance.now();

  if (useStream) {
    // Streaming response
    try {
      var res = await fetch("/v1/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.status === 401) { logout(); throw new Error("Unauthorized"); }
      if (!res.ok) {
        var errData = await res.json().catch(function () { return { detail: res.statusText }; });
        assistantBubble.textContent = "Error: " + (errData.detail || res.status);
        statusEl.textContent = "Error " + res.status;
        statusEl.dataset.state = "error";  // C9
        return;
      }
      var reader = res.body.getReader();
      var decoder = new TextDecoder();
      var fullContent = "";
      var fullReasoning = "";
      var streamUsage = null;
      var finishReason = null;
      assistantBubble.textContent = "";

      // Thinking models (Ollama qwen3.5/kimi/glm/deepseek, OpenAI o1 family)
      // emit chain-of-thought in delta.reasoning before delta.content. We
      // render reasoning in a <details> block placed BEFORE the response
      // bubble so the user sees activity while the model is "thinking".
      var thinkingEl = null;
      var thinkingContent = null;
      function _ensureThinkingPanel() {
        if (thinkingEl) return;
        thinkingEl = document.createElement("details");
        thinkingEl.open = true; // open while streaming, will collapse on done
        thinkingEl.style.cssText = "align-self:flex-start; max-width:75%; margin-bottom:0.25rem; background:var(--bg-secondary); border:1px solid var(--border-strong); border-radius:10px; padding:0.35rem 0.6rem; font-size:0.8rem; color:var(--text-secondary);";
        var summary = document.createElement("summary");
        summary.style.cssText = "cursor:pointer; user-select:none; font-weight:500;";
        summary.textContent = "🧠 Thinking…";
        thinkingEl.appendChild(summary);
        thinkingContent = document.createElement("div");
        thinkingContent.style.cssText = "white-space:pre-wrap; margin-top:0.4rem; font-family:ui-monospace,monospace; font-size:0.75rem; max-height:300px; overflow-y:auto;";
        thinkingEl.appendChild(thinkingContent);
        assistantBubble.parentNode.insertBefore(thinkingEl, assistantBubble);
      }

      var sseBuffer = "";
      while (true) {
        var result = await reader.read();
        var streamDone = result.done;
        // Accumulate, then split into COMPLETE lines only — keep the trailing
        // partial line buffered across reads. An SSE "data:" line can be split
        // across TCP chunks; without this buffering the partial line fails
        // JSON.parse and its content delta is silently dropped, corrupting the
        // rendered output (e.g. only the tail of a fast model's answer survives).
        sseBuffer += streamDone ? decoder.decode() : decoder.decode(result.value, { stream: true });
        var lines = sseBuffer.split("\n");
        sseBuffer = streamDone ? "" : lines.pop();
        for (var i = 0; i < lines.length; i++) {
          var line = lines[i].trim();
          if (!line || !line.startsWith("data: ")) continue;
          var payload = line.slice(6);
          if (payload === "[DONE]") continue;
          try {
            var parsed = JSON.parse(payload);
            var choice = parsed.choices && parsed.choices[0];
            var delta = choice && choice.delta;
            if (delta && delta.reasoning) {
              fullReasoning += delta.reasoning;
              _ensureThinkingPanel();
              thinkingContent.textContent = fullReasoning;
              thinkingContent.scrollTop = thinkingContent.scrollHeight;
            }
            if (delta && delta.content) {
              fullContent += delta.content;
              assistantBubble.textContent = fullContent;
              document.getElementById("pg-messages").scrollTop = document.getElementById("pg-messages").scrollHeight;
            }
            if (choice && choice.finish_reason) finishReason = choice.finish_reason;
            // OpenAI-style include_usage: final chunk carries usage block
            if (parsed.usage) streamUsage = parsed.usage;
          } catch (e) { /* skip invalid SSE */ }
        }
        if (streamDone) break;
      }

      // Collapse the thinking panel once streaming is done — keep it
      // available for inspection but out of the way.
      if (thinkingEl) {
        thinkingEl.open = false;
        var rTokens = (streamUsage && streamUsage.reasoning_tokens) || "?";
        thinkingEl.querySelector("summary").textContent = "🧠 Thinking (" + rTokens + " tokens) (click to expand)";
      }

      // Truncation guard (Codex critic finding): a thinking model that
      // burns the whole token budget on reasoning hits finish_reason=length
      // with content="" — without this warning the user sees an empty
      // bubble and assumes the proxy is broken.
      if (!fullContent) {
        assistantBubble.dataset.contentEmpty = "1";  // C9 — real content was empty (before cosmetic sentinel)
        if (finishReason === "length" && fullReasoning) {
          fullContent = "⚠️ Response truncated before any content: the model spent its whole token budget on reasoning. Raise `Max:` (try 4096 or 8192) and retry.";
        } else {
          fullContent = "(empty response)";
        }
        assistantBubble.textContent = fullContent;
      }
      if (finishReason) assistantBubble.dataset.finishReason = finishReason;  // C9
      _pgHistory.push({ role: "assistant", content: fullContent });
      var elapsed = ((performance.now() - t0) / 1000).toFixed(1);
      var tokens = (streamUsage && streamUsage.completion_tokens)
                 || res.headers.get("X-Completion-Tokens") || "?";
      var cost;
      if (streamUsage && typeof streamUsage.cost_usd === "number") {
        cost = streamUsage.cost_usd.toFixed(6);
      } else {
        cost = res.headers.get("X-Cost-USD") || "?";
      }
      statusEl.textContent = elapsed + "s | " + tokens + " tokens | $" + cost;
      statusEl.dataset.state = "done";  // C9
    } catch (err) {
      assistantBubble.textContent = "Error: " + err.message;
      statusEl.textContent = "Connection error";
      statusEl.dataset.state = "error";  // C9
    }
  } else {
    // Non-streaming
    try {
      var res = await api("POST", "/v1/chat/completions", body);
      var data = await res.json();
      var elapsed = ((performance.now() - t0) / 1000).toFixed(1);

      if (!res.ok) {
        assistantBubble.textContent = "Error: " + (data.detail || res.status);
        statusEl.textContent = "Error " + res.status;
        statusEl.dataset.state = "error";  // C9
        return;
      }

      var choice0 = data.choices && data.choices[0];
      var msg = choice0 && choice0.message;
      var content = msg && msg.content;
      var reasoning = msg && msg.reasoning;
      var finishReasonNS = choice0 && choice0.finish_reason;

      // Render reasoning in a collapsed <details> above the bubble — same
      // UX as the streaming path so thinking models behave consistently.
      if (reasoning) {
        var thinkEl = document.createElement("details");
        thinkEl.style.cssText = "align-self:flex-start; max-width:75%; margin-bottom:0.25rem; background:var(--bg-secondary); border:1px solid var(--border-strong); border-radius:10px; padding:0.35rem 0.6rem; font-size:0.8rem; color:var(--text-secondary);";
        var sumEl = document.createElement("summary");
        sumEl.style.cssText = "cursor:pointer; user-select:none; font-weight:500;";
        sumEl.textContent = "🧠 Thinking (click to expand)";
        thinkEl.appendChild(sumEl);
        var contentEl = document.createElement("div");
        contentEl.style.cssText = "white-space:pre-wrap; margin-top:0.4rem; font-family:ui-monospace,monospace; font-size:0.75rem; max-height:300px; overflow-y:auto;";
        contentEl.textContent = reasoning;
        thinkEl.appendChild(contentEl);
        assistantBubble.parentNode.insertBefore(thinkEl, assistantBubble);
      }

      if (!content) {
        assistantBubble.dataset.contentEmpty = "1";  // C9 — real content was empty (before cosmetic sentinel)
        if (finishReasonNS === "length" && reasoning) {
          content = "⚠️ Response truncated before any content: the model spent its whole token budget on reasoning. Raise `Max:` (try 4096 or 8192) and retry.";
        } else {
          content = "(empty response)";
        }
      }
      if (finishReasonNS) assistantBubble.dataset.finishReason = finishReasonNS;  // C9
      assistantBubble.textContent = content;
      _pgHistory.push({ role: "assistant", content: content });

      var billing = data.billing || {};
      var cost = billing.cost_usd !== undefined ? "$" + billing.cost_usd.toFixed(4) : "?";
      var tokens = (billing.completion_tokens || "?");
      statusEl.textContent = elapsed + "s | " + tokens + " tokens | " + cost;
      statusEl.dataset.state = "done";  // C9
    } catch (err) {
      assistantBubble.textContent = "Error: " + err.message;
      statusEl.textContent = "Connection error";
      statusEl.dataset.state = "error";  // C9
    }
  }
}

function _pgClear() {
  _pgHistory = [];
  var container = document.getElementById("pg-messages");
  container.innerHTML = '<div class="muted" style="text-align:center; padding:2rem;">Select a model and start chatting.</div>';
  document.getElementById("pg-status").textContent = "";
}

/* ── Pricing page, review queue and auto-update panels ──────────────────────
   Uses the helpers defined in app.js: h, api, table, toast, emptyState,
   skeleton, fmt, toApiMonth, toHtmlMonth, currentMonth, confirmAction. */
"use strict";

(function () {
  var PAGE_SIZE = 50;

  // ── Small helpers ─────────────────────────────────────────────────────────

  function ago(iso) {
    if (!iso) return "never";
    var t = Date.parse(iso);
    if (isNaN(t)) return iso;
    var s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 90) return s + " s ago";
    var m = Math.round(s / 60);
    if (m < 90) return m + " min ago";
    var hr = Math.round(m / 60);
    if (hr < 48) return hr + " h ago";
    return Math.round(hr / 24) + " days ago";
  }

  function price(p) {
    if (!p) return "—";
    return "$" + Number(p.input_price).toFixed(4) + " / $" + Number(p.output_price).toFixed(4);
  }

  function badge(text, kind) {
    return h("span", { className: "badge badge-" + (kind || "muted") }, text);
  }

  function sourceChip(source) {
    if (!source) return badge("no source", "muted");
    var kind = source === "manual" ? "accent" : source === "llm" ? "orange" : "muted";
    return badge(source, kind);
  }

  async function readError(res) {
    try {
      var body = await res.json();
      var d = body.detail;
      if (d && typeof d === "object") return d.detail || JSON.stringify(d);
      return d || ("HTTP " + res.status);
    } catch (e) {
      return "HTTP " + res.status;
    }
  }

  // ── Auto-update panel (Pricing banner and Monitoring card) ────────────────

  function schedulerBadge(s) {
    if (!s || s.error) return badge("not running", "muted");
    if (s.disabled) return badge("disabled", "muted");
    if (s.running_since) return badge("running", "accent");
    if (s.cb_state === "OPEN") return badge("paused after failures", "red");
    if (s.stale) return badge("stale", "orange");
    if (s.last_error) return badge("last run failed", "red");
    if (!s.last_success_at) return badge("waiting for first run", "muted");
    return badge("ok", "green");
  }

  function cadence(s) {
    if (!s || s.refresh_interval_h == null) return "";
    return s.refresh_interval_h > 0 ? "every " + s.refresh_interval_h + " h" : "daily at " + s.daily_hour_utc + ":00 UTC";
  }

  function catalogBadge(c) {
    var st = (c && c.state) || "unavailable";
    var kind = st === "ok" ? "green" : st === "off" ? "muted" : st === "stale" ? "orange" : "red";
    return badge("catalog " + st, kind);
  }

  async function renderAutoUpdate(container, opts) {
    if (!container) return;
    opts = opts || {};
    container.innerHTML = "";
    container.appendChild(skeleton(1));
    var data;
    try {
      var res = await api("GET", "/admin/auto-update/status?journal=20");
      if (!res.ok) throw new Error(await readError(res));
      data = await res.json();
    } catch (e) {
      container.innerHTML = "";
      container.appendChild(h("div", { className: "alert alert-error" }, "Auto-update status unavailable: " + e.message));
      return;
    }
    container.innerHTML = "";
    var p = data.pricing || {};
    var m = data.models || {};
    var lastChanges = m.last_changes || {};
    var changedProviders = Object.keys(lastChanges.providers || {});
    var errors = lastChanges.errors || [];
    var running = Boolean(p.running_since || m.running_since);

    var runBtn = h("button", { className: "btn btn-sm btn-primary", type: "button" }, running ? "Running…" : "Refresh now");
    if (running) runBtn.disabled = true;
    var status = h("span", { className: "muted small", role: "status" });
    runBtn.addEventListener("click", async function () {
      runBtn.disabled = true;
      runBtn.textContent = "Starting…";
      try {
        var r = await api("POST", "/admin/auto-update/run");
        if (r.status === 409) {
          var body = await r.json();
          var since = body.running_since;
          status.textContent = "Already running since " + ago(since) + ".";
        } else if (!r.ok) {
          status.textContent = "Could not start: " + (await readError(r));
        } else {
          status.textContent = "Started. Prices and model lists are refreshing in the background.";
          runBtn.textContent = "Running…";
          setTimeout(function () { renderAutoUpdate(container, opts); }, 4000);
          return;
        }
      } catch (e) {
        status.textContent = "Could not start: " + e.message;
      }
      runBtn.textContent = "Refresh now";
      runBtn.disabled = false;
    });

    var pricesLine = h("div", { className: "au-row" },
      h("strong", null, "Prices"), schedulerBadge(p),
      h("span", { className: "muted" }, "updated " + ago(p.last_success_at) + (cadence(p) ? " · " + cadence(p) : "")));
    var modelsLine = h("div", { className: "au-row" },
      h("strong", null, "Model lists"), schedulerBadge(m),
      h("span", { className: "muted" }, "refreshed " + ago(m.last_success_at) + (cadence(m) ? " · " + cadence(m) : "")),
      changedProviders.length ? h("span", null, changedProviders.length + " provider(s) changed") : null,
      errors.length ? badge(errors.length + " error(s)", "red") : null);
    var catalogLine = h("div", { className: "au-row" },
      h("strong", null, "Catalog"), catalogBadge(data.catalog),
      data.catalog && data.catalog.providers != null ? h("span", { className: "muted" }, data.catalog.providers + " providers") : null,
      data.catalog && data.catalog.last_error ? h("span", { className: "muted" }, data.catalog.last_error) : null);
    var pending = data.pending_suggestions || 0;
    var reviewLine = h("div", { className: "au-row" },
      h("strong", null, "Review"),
      pending ? badge(pending + " price change(s) waiting", "orange") : badge("nothing waiting", "green"),
      pending ? h("span", { className: "muted" }, "Price changes that need your OK (big moves, conflicting sources, prices read by a model, estimates). Apply or reject them below.") : null);

    var card = h("div", { className: "card au-card" },
      h("div", { className: "card-header" }, h("span", { className: "card-title" }, "Automatic updates"), h("div", { className: "btn-group" }, runBtn)),
      pricesLine, modelsLine, catalogLine, reviewLine, status);
    if (errors.length) {
      card.appendChild(h("ul", { className: "au-errors" }, errors.map(function (e) {
        return h("li", null, h("code", null, e.provider), " " + e.error);
      })));
    }
    container.appendChild(card);

    if (opts.changesEl) renderChanges(opts.changesEl, data.recent_changes || []);
  }

  function renderChanges(el, changes) {
    el.innerHTML = "";
    if (!changes.length) { el.appendChild(emptyState("—", "No automatic change recorded yet.")); return; }
    var rows = changes.slice().reverse().map(function (c) {
      var what;
      if (c.event === "models.changed") {
        what = [
          (c.added || []).length ? "+" + c.added.length + " " + c.added.slice(0, 5).join(", ") : null,
          (c.removed || []).length ? "−" + c.removed.length + " " + c.removed.slice(0, 5).join(", ") : null,
          c.truncated ? c.truncated + " more changes (see journal)" : null,
        ].filter(Boolean).join(" · ");
      } else {
        what = c.truncated ? c.truncated + " more price changes (see journal)"
          : (c.model || "") + ": " + price(c.old) + " → " + price(c.new) + " (" + c.pct + "%, " + c.status + ")";
      }
      return h("tr", null,
        h("td", null, (c.at || "").replace("T", " ").replace("Z", "")),
        h("td", null, badge(c.event === "models.changed" ? "models" : "price", c.event === "models.changed" ? "accent" : "orange")),
        h("td", null, h("code", null, c.provider || "—")),
        h("td", null, what));
    });
    el.appendChild(table(["When", "Type", "Provider", "Change"], rows));
  }

  // ── Review queue ──────────────────────────────────────────────────────────

  var reviewStatus = h("div", { className: "muted small", role: "status", "aria-live": "polite" });

  async function renderReview(container, onApplied) {
    if (reviewStatus.parentNode !== container.parentNode) container.parentNode.insertBefore(reviewStatus, container.nextSibling);
    container.innerHTML = "";
    container.appendChild(skeleton(1));
    var items;
    try {
      var res = await api("GET", "/admin/pricing/suggestions");
      if (!res.ok) throw new Error(await readError(res));
      items = (await res.json()).items || [];
    } catch (e) {
      container.innerHTML = "";
      container.appendChild(h("div", { className: "alert alert-error" }, "Review queue unavailable: " + e.message));
      return;
    }
    container.innerHTML = "";
    if (!items.length) {
      container.appendChild(h("p", { className: "muted" }, "Nothing to review. Price changes above the drift threshold and prices read by a model wait here before they are billed. A model with no price is billed at the price of the closest version of its family and listed here until you confirm it."));
      return;
    }
    var selected = {};
    var status = reviewStatus;
    var busy = false;

    async function act(ids, action) {
      if (busy) return;
      busy = true;
      container.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
      status.textContent = (action === "apply" ? "Applying…" : "Rejecting…");
      var applied = false;
      try {
        var res = ids.length === 1
          ? await api("POST", "/admin/pricing/suggestions/" + encodeURIComponent(ids[0]) + "/" + action)
          : await api("POST", "/admin/pricing/suggestions/" + action, { ids: ids });
        if (res.status === 409) {
          status.textContent = "Not done: " + (await readError(res)) + ". The list was reloaded.";
        } else if (!res.ok) {
          status.textContent = "Failed: " + (await readError(res));
        } else {
          var body = await res.json();
          var errs = body.errors || [];
          var done = body.done ? body.done.length : 1;
          applied = action === "apply" && done > 0;
          status.textContent = (action === "apply" ? "Applied " : "Rejected ") + done + "." +
            (action === "reject" ? " A rejected change comes back only if the source proposes a different price." : "") +
            (errs.length ? " " + errs.length + " could not be processed (changed meanwhile)." : "");
        }
      } catch (e) {
        status.textContent = "Failed: " + e.message;
      }
      if (applied && onApplied) onApplied();
      else renderReview(container, onApplied);
    }

    var rows = items.map(function (s) {
      var box = h("input", { type: "checkbox", "aria-label": "Select " + s.model });
      box.addEventListener("change", function () { selected[s.id] = box.checked; });
      var label = s.type === "llm" ? "LLM-extracted"
        : s.type === "estimate" ? "Estimated (nearest model)"
        : (s.current ? "Price change" : "Sources disagree") + (s.pct != null ? " " + s.pct + "%" : "");
      return h("tr", null,
        h("td", null, box),
        h("td", null, badge(label, s.type === "llm" || s.type === "estimate" ? "orange" : "accent")),
        h("td", null, h("code", null, s.price_key || s.model)),
        h("td", null, s.current ? price(s.current) : h("span", { className: "muted" }, "no price yet")),
        h("td", null, h("strong", null, price(s.proposed))),
        h("td", null, s.source || "—", s.reason ? h("div", { className: "muted small" }, s.reason) : null),
        h("td", null, ago(s.created_at), h("div", { className: "muted small" }, s.month)),
        h("td", null, h("div", { className: "btn-group" },
          h("button", { className: "btn btn-sm btn-primary", type: "button", onClick: function () { act([s.id], "apply"); } }, "Apply"),
          h("button", { className: "btn btn-sm", type: "button", onClick: function () { act([s.id], "reject"); } }, "Reject"))));
    });
    function chosen() { return Object.keys(selected).filter(function (k) { return selected[k]; }); }
    var bulk = h("div", { className: "btn-group", style: "margin-top:0.5rem;" },
      h("button", { className: "btn btn-sm", type: "button", onClick: function () {
        var ids = chosen(); if (!ids.length) { status.textContent = "Select at least one row."; return; } act(ids, "apply");
      } }, "Apply selected"),
      h("button", { className: "btn btn-sm", type: "button", onClick: function () {
        var ids = chosen(); if (!ids.length) { status.textContent = "Select at least one row."; return; } act(ids, "reject");
      } }, "Reject selected"));
    var all = h("input", { type: "checkbox", "aria-label": "Select all" });
    all.addEventListener("change", function () {
      container.querySelectorAll("tbody input[type=checkbox]").forEach(function (b) {
        b.checked = all.checked;
        b.dispatchEvent(new Event("change"));
      });
    });
    var wrap = table(["", "Type", "Price key", "Current", "Proposed", "Source", "Waiting", "Actions"], rows);
    wrap.querySelector("th").appendChild(all);
    container.appendChild(wrap);
    container.appendChild(bulk);
  }

  // ── Prices read by a model (read-only status) ─────────────────────────────

  async function renderLlmStatus() {
    var stateEl = document.getElementById("pricing-llm-state");
    var body = document.getElementById("pricing-llm-body");
    if (!stateEl || !body) return;
    body.innerHTML = "";
    var st;
    try {
      var res = await api("GET", "/admin/pricing/llm/status");
      if (!res.ok) throw new Error(await readError(res));
      st = await res.json();
    } catch (e) {
      stateEl.textContent = "status unavailable";
      return;
    }
    stateEl.textContent = st.enabled ? "on · $" + Number(st.spent_today_usd || 0).toFixed(4) + " of $" + Number(st.max_usd_per_day).toFixed(2) + " today" : "off";
    if (!st.enabled) {
      body.appendChild(h("p", { className: "muted" },
        "Off. With PLUGBOARD_LLM_PRICING=1, Plugboard reads the pricing page of providers whose models have no price, once a day, and queues what a model finds there for your review. Nothing is billed until you apply it."));
      return;
    }
    var rep = st.last_report || {};
    body.appendChild(h("div", { className: "au-row" }, h("strong", null, "Model"), h("span", null, st.provider + " · " + st.model)));
    body.appendChild(h("div", { className: "au-row" }, h("strong", null, "Last pass"),
      h("span", null, st.last_run ? ago(st.last_run) + " · " + (rep.calls || 0) + " call(s), " + ((rep.suggested || []).length) + " suggestion(s)" : "never"),
      rep.skipped ? h("span", { className: "muted" }, rep.skipped) : null));
    var reasons = rep.reasons || {};
    var names = Object.keys(reasons).sort(function (a, b) { return a.localeCompare(b); });
    if (names.length) {
      body.appendChild(table(["Model without a price", "Why, and what to do"], names.map(function (m) {
        return h("tr", null, h("td", null, h("code", null, m)), h("td", null, reasons[m]));
      })));
    }
  }

  // ── Price grid ────────────────────────────────────────────────────────────

  var state = {
    month: currentMonth(),
    grid: { models: {}, models_by_provider: {} },
    missing: [],
    edits: {},       // "space|model" -> {input_price, output_price}
    expanded: {},
    page: 0,
  };

  var monthInput = document.getElementById("pricing-month");
  var searchInput = document.getElementById("pricing-search");
  var sourceFilter = document.getElementById("pricing-source-filter");
  var missingOnly = document.getElementById("pricing-missing");
  var saveBtn = document.getElementById("btn-save-pricing");
  var saveStatus = document.getElementById("pricing-save-status");

  monthInput.value = toHtmlMonth(state.month);
  monthInput.addEventListener("change", function () { state.page = 0; loadPricingPage(); });
  var searchTimer = null;
  searchInput.addEventListener("input", function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () { state.page = 0; renderGrid(); }, 150);
  });
  sourceFilter.addEventListener("change", function () { state.page = 0; renderGrid(); });
  missingOnly.addEventListener("change", function () { state.page = 0; renderGrid(); });

  function editKey(space, model) { return (space || "") + "|" + model; }

  function providerPrices(model) {
    var out = [];
    var spaces = state.grid.models_by_provider || {};
    Object.keys(spaces).sort(function (a, b) { return a.localeCompare(b); }).forEach(function (space) {
      if (spaces[space] && spaces[space][model]) out.push({ space: space, entry: spaces[space][model] });
    });
    return out;
  }

  function rowsToShow() {
    var models = state.grid.models || {};
    var spaces = state.grid.models_by_provider || {};
    var names = {};
    Object.keys(models).forEach(function (m) { names[m] = true; });
    Object.keys(spaces).forEach(function (sp) { Object.keys(spaces[sp] || {}).forEach(function (m) { names[m] = true; }); });
    var list;
    if (missingOnly.checked) {
      list = state.missing.map(function (id) { return { model: id, missing: true }; });
    } else {
      list = Object.keys(names).sort(function (a, b) { return a.localeCompare(b); }).map(function (m) { return { model: m, entry: models[m] || null }; });
    }
    var q = searchInput.value.trim().toLowerCase();
    if (q) list = list.filter(function (r) { return r.model.toLowerCase().indexOf(q) >= 0; });
    var src = sourceFilter.value;
    if (src && !missingOnly.checked) {
      list = list.filter(function (r) {
        var own = r.entry ? (r.entry.source || "none") : null;
        if (own === src) return true;
        return providerPrices(r.model).some(function (pp) { return (pp.entry.source || "none") === src; });
      });
    }
    return list;
  }

  function priceInput(space, model, field, value) {
    var key = editKey(space, model);
    var current = state.edits[key] ? state.edits[key][field] : value;
    var input = h("input", {
      type: "number", className: "pricing-input", step: "0.0001", min: "0", max: "1000",
      value: current == null ? "" : current,
      "aria-label": (space ? space + " " : "") + model + " " + (field === "input_price" ? "input" : "output") + " price per million tokens",
    });
    input.addEventListener("change", function () {
      var v = parseFloat(input.value);
      if (isNaN(v) || v < 0 || v > 1000) {
        input.setAttribute("aria-invalid", "true");
        input.setAttribute("aria-describedby", "pricing-save-status");
        delete state.edits[key];
        updateSaveButton();
        saveStatus.textContent = "Prices must be between 0 and 1000 USD per 1M tokens.";
        return;
      }
      input.removeAttribute("aria-invalid");
      var base = state.edits[key] || {
        input_price: field === "input_price" ? v : Number((state.lookup(space, model) || {}).input_price || 0),
        output_price: field === "output_price" ? v : Number((state.lookup(space, model) || {}).output_price || 0),
      };
      base[field] = v;
      state.edits[key] = base;
      updateSaveButton();
    });
    return input;
  }

  state.lookup = function (space, model) {
    if (!space) return (state.grid.models || {})[model];
    return ((state.grid.models_by_provider || {})[space] || {})[model];
  };

  function revertButton(space, model) {
    return h("button", { className: "btn btn-sm", type: "button", onClick: async function (ev) {
      if (!await confirmAction("Remove the manual price of " + (space ? space + " · " : "") + model + "? The automatic price will apply again.")) return;
      var btn = ev.currentTarget;
      btn.disabled = true;
      try {
        var res = await api("POST", "/admin/pricing/" + state.month + "/revert", { model: model, provider: space || "" });
        if (!res.ok) { toast("Revert failed: " + (await readError(res)), "error"); btn.disabled = false; return; }
        var body = await res.json();
        toast(body.entry ? "Back to automatic: " + price(body.entry) : "Manual price removed; no source prices this model yet.", "success");
        loadPricingPage();
      } catch (e) {
        toast("Revert failed: " + e.message, "error");
        btn.disabled = false;
      }
    } }, "Revert to automatic");
  }

  function gridRow(r) {
    var cells;
    if (r.missing) {
      var routed = r.model;
      var suffix = routed.indexOf(":") >= 0 ? routed.slice(routed.indexOf(":") + 1) : routed;
      return h("tr", null,
        h("td", null, h("code", null, routed), h("div", { className: "muted small" }, "advertised, no price: billed $0")),
        h("td", null, priceInput("", suffix, "input_price", null)),
        h("td", null, priceInput("", suffix, "output_price", null)),
        h("td", null, badge("missing", "red")),
        h("td", null, ""));
    }
    var pps = providerPrices(r.model);
    var toggle = null;
    if (pps.length) {
      var open = Boolean(state.expanded[r.model]);
      toggle = h("button", { className: "btn btn-ghost btn-sm", type: "button", "aria-expanded": String(open),
        "data-toggle": r.model,
        onClick: function () { state.expanded[r.model] = !open; renderGrid({ toggle: r.model }); } },
        (open ? "▾ " : "▸ ") + pps.length + " provider price" + (pps.length > 1 ? "s" : ""));
    }
    var e = r.entry;
    cells = [
      h("td", null, h("code", null, r.model), toggle ? h("div", null, toggle) : null),
      h("td", null, e ? priceInput("", r.model, "input_price", e.input_price) : h("span", { className: "muted" }, "provider prices only")),
      h("td", null, e ? priceInput("", r.model, "output_price", e.output_price) : ""),
      h("td", null, e ? sourceChip(e.source) : ""),
      h("td", null, e && e.source === "manual" ? revertButton("", r.model) : ""),
    ];
    var rows = [h("tr", null, cells)];
    if (pps.length && state.expanded[r.model]) {
      pps.forEach(function (pp) {
        rows.push(h("tr", { className: "subrow" },
          h("td", null, h("span", { className: "muted" }, "via "), h("code", null, pp.space)),
          h("td", null, priceInput(pp.space, r.model, "input_price", pp.entry.input_price)),
          h("td", null, priceInput(pp.space, r.model, "output_price", pp.entry.output_price)),
          h("td", null, sourceChip(pp.entry.source)),
          h("td", null, pp.entry.source === "manual" ? revertButton(pp.space, r.model) : "")));
      });
    }
    return rows;
  }

  function renderGrid(focus) {
    var container = document.getElementById("pricing-table-container");
    var pager = document.getElementById("pricing-pager");
    var countEl = document.getElementById("pricing-count");
    var list = rowsToShow();
    var pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
    if (state.page >= pages) state.page = pages - 1;
    var start = state.page * PAGE_SIZE;
    var slice = list.slice(start, start + PAGE_SIZE);
    container.innerHTML = "";
    pager.innerHTML = "";
    countEl.textContent = list.length ? "Showing " + (start + 1) + "–" + (start + slice.length) + " of " + fmt(list.length) : "";
    if (!list.length) {
      container.appendChild(emptyState("—", missingOnly.checked ? "Every advertised model has a price." : "No model matches."));
      return;
    }
    var rows = [];
    slice.forEach(function (r) { rows = rows.concat(gridRow(r)); });
    container.appendChild(table(["Model", "Input $/1M", "Output $/1M", "Source", ""], rows));
    if (pages > 1) {
      var prev = h("button", { className: "btn btn-sm", type: "button", "data-pager": "prev", onClick: function () { state.page--; renderGrid({ pager: "prev" }); } }, "‹ Previous");
      var next = h("button", { className: "btn btn-sm", type: "button", "data-pager": "next", onClick: function () { state.page++; renderGrid({ pager: "next" }); } }, "Next ›");
      if (state.page === 0) prev.disabled = true;
      if (state.page >= pages - 1) next.disabled = true;
      pager.appendChild(prev);
      pager.appendChild(h("span", { className: "muted" }, "Page " + (state.page + 1) + " of " + pages));
      pager.appendChild(next);
    }
    // Re-rendering replaces the clicked control: give keyboard focus back.
    if (focus && focus.toggle) {
      var t = Array.prototype.find.call(container.querySelectorAll("[data-toggle]"), function (b) { return b.getAttribute("data-toggle") === focus.toggle; });
      if (t) t.focus();
    } else if (focus && focus.pager) {
      var target = pager.querySelector('[data-pager="' + focus.pager + '"]:not([disabled])') || pager.querySelector("[data-pager]:not([disabled])");
      if (target) target.focus();
    }
  }

  function updateSaveButton() {
    var n = Object.keys(state.edits).length;
    saveBtn.disabled = n === 0;
    saveBtn.textContent = n ? "Save " + n + " change" + (n > 1 ? "s" : "") : "Save changes";
    saveStatus.textContent = n ? "Saved prices are marked manual and no source replaces them." : "";
  }

  document.getElementById("btn-add-pricing-row").addEventListener("click", function () {
    var name = document.getElementById("new-model-name").value.trim();
    var space = document.getElementById("new-model-provider").value.trim();
    var inp = parseFloat(document.getElementById("new-model-input").value);
    var out = parseFloat(document.getElementById("new-model-output").value);
    if (!name || isNaN(inp) || isNaN(out) || inp < 0 || out < 0 || inp > 1000 || out > 1000) {
      saveStatus.textContent = "Give a model name and two prices between 0 and 1000.";
      return;
    }
    state.edits[editKey(space, name)] = { input_price: inp, output_price: out };
    if (space) {
      state.grid.models_by_provider = state.grid.models_by_provider || {};
      state.grid.models_by_provider[space] = state.grid.models_by_provider[space] || {};
      state.grid.models_by_provider[space][name] = { input_price: inp, output_price: out };
      state.expanded[name] = true;
    } else {
      state.grid.models[name] = { input_price: inp, output_price: out };
    }
    ["new-model-name", "new-model-provider", "new-model-input", "new-model-output"].forEach(function (id) { document.getElementById(id).value = ""; });
    searchInput.value = name;
    missingOnly.checked = false;
    state.page = 0;
    renderGrid();
    updateSaveButton();
  });

  saveBtn.addEventListener("click", async function () {
    var body = { models: {}, models_by_provider: {} };
    Object.keys(state.edits).forEach(function (key) {
      var cut = key.indexOf("|");
      var space = key.slice(0, cut);
      var model = key.slice(cut + 1);
      if (space) {
        body.models_by_provider[space] = body.models_by_provider[space] || {};
        body.models_by_provider[space][model] = state.edits[key];
      } else {
        body.models[model] = state.edits[key];
      }
    });
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    try {
      var res = await api("PATCH", "/admin/pricing/" + state.month, body);
      if (!res.ok) {
        saveStatus.textContent = "Not saved: " + (await readError(res));
        updateSaveButton();
        return;
      }
    } catch (e) {
      saveStatus.textContent = "Not saved: " + e.message;
      updateSaveButton();
      return;
    }
    toast("Prices saved for " + state.month, "success");
    loadPricingPage();
  });

  async function loadPricingPage() {
    state.month = toApiMonth(monthInput.value);
    state.edits = {};
    updateSaveButton();
    var container = document.getElementById("pricing-table-container");
    var noteEl = document.getElementById("pricing-note");
    var actionsEl = document.getElementById("pricing-actions");
    container.innerHTML = "";
    container.appendChild(skeleton(3));
    noteEl.textContent = "";
    renderAutoUpdate(document.getElementById("pricing-auto-update"));
    renderReview(document.getElementById("pricing-review"), loadPricingPage);
    renderLlmStatus();
    try {
      var res = await api("GET", "/admin/pricing/" + state.month);
      state.grid = res.status === 404 ? { models: {}, models_by_provider: {} } : await res.json();
      state.grid.models = state.grid.models || {};
      state.grid.models_by_provider = state.grid.models_by_provider || {};
      if (state.grid.note) noteEl.textContent = state.grid.note;
      state.missing = [];
      if (state.month === currentMonth()) {
        try {
          var cov = await api("GET", "/admin/coverage/pricing");
          if (cov.ok) state.missing = (await cov.json()).missing || [];
        } catch (e) { /* coverage is optional */ }
      }
      missingOnly.disabled = state.month !== currentMonth();
      missingOnly.parentElement.title = missingOnly.disabled ? "Only for the current month" : "";
      actionsEl.classList.remove("hidden");
      renderGrid();
    } catch (e) {
      container.innerHTML = "";
      container.appendChild(h("div", { className: "alert alert-error" }, "Failed to load pricing: " + e.message));
    }
  }

  window.loadPricingPage = loadPricingPage;
  window.renderAutoUpdate = renderAutoUpdate;
})();

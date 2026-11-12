/* Catalog history (PB-PROV-02): models each provider gained or lost, newest first.
 *
 * Data: GET /admin/catalog/history (admin session). Provider and model names
 * come from the providers' own model lists: they are only ever written as text
 * through h(), never as HTML. Uses the helpers of app.js: h, api, skeleton,
 * emptyState.
 */
(function () {
  "use strict";

  var PAGE = 50;        // entries per request ("Load more" adds as many)
  var SHOWN = 10;       // names shown before "+N more"
  var DEBOUNCE_MS = 400;
  var DAY_MS = 86400000;
  var view = null;      // filters of the list on screen; "Load more" keeps them
  var shown = 0;        // entries on screen
  var seq = 0;          // the latest request; older answers are dropped
  var busy = false;
  var typing = null;
  var typed = "";       // model text of the last load (a trailing space is not a change)

  function el(id) { return document.getElementById(id); }

  function modelText() { return el("ch-model").value.trim(); }

  // The filters as the boxes show them now, frozen for one list.
  function currentFilters() {
    var days = el("ch-period").value;
    return {
      provider: el("ch-provider").value,
      since: days ? new Date(Date.now() - Number(days) * DAY_MS).toISOString().slice(0, 10) : "",
      model: modelText(),
    };
  }

  function query(filters, offset) {
    var params = new URLSearchParams();
    if (filters.provider) params.set("provider", filters.provider);
    if (filters.since) params.set("since", filters.since);
    if (filters.model) params.set("model", filters.model);
    params.set("limit", String(PAGE));
    params.set("offset", String(offset));
    return "/admin/catalog/history?" + params.toString();
  }

  async function errorText(res) {
    try {
      var detail = (await res.json()).detail;
      return typeof detail === "string" ? detail : "HTTP " + res.status;
    } catch (e) {
      return "HTTP " + res.status;
    }
  }

  function when(at) {
    return at ? at.slice(0, 16).replace("T", " ") + " UTC" : "date unknown";
  }

  function chips(names, needle) {
    return names.map(function (name) {
      var hit = needle && name.toLowerCase().indexOf(needle) !== -1;
      return h("code", hit ? { className: "ch-match" } : null, name);
    });
  }

  // One badge and its names: the first SHOWN, the rest behind "+N more".
  function group(names, label, kind, needle) {
    if (!names.length) return null;
    var all = chips(names, needle);
    var rest = all.length > SHOWN
      ? h("details", null,
          h("summary", null, "+" + (all.length - SHOWN) + " more"),
          h("div", { className: "ch-row" }, all.slice(SHOWN)))
      : null;
    return h("div", { className: "ch-row" },
      h("span", { className: "badge badge-" + kind }, label), all.slice(0, SHOWN), rest);
  }

  function card(entry, needle) {
    return h("div", { className: "card ch-entry" },
      h("div", { className: "ch-row" },
        h("span", { className: "muted small" }, when(entry.at)),
        h("code", null, entry.provider)),
      group(entry.added, "+" + entry.added.length + " added", "green", needle),
      group(entry.removed, "−" + entry.removed.length + " removed", "red", needle));
  }

  function fillProviders(providers) {
    var select = el("ch-provider");
    var current = select.value;
    var options = [h("option", { value: "" }, "All providers")];
    providers.forEach(function (p) { options.push(h("option", { value: p }, p)); });
    if (current && providers.indexOf(current) === -1) options.push(h("option", { value: current }, current));
    select.replaceChildren.apply(select, options);
    select.value = current;
  }

  function footer(retention) {
    var cap = Number(retention.max_lines).toLocaleString("en-US");
    var text = retention.days
      ? "Kept for " + retention.days + " days, at most " + cap + " changes"
      : "Kept until the " + cap + "-change cap";
    if (retention.oldest) text += " · oldest: " + retention.oldest.slice(0, 10);
    el("ch-footer").textContent = text;
  }

  function setBusy(on) {
    busy = on;
    el("ch-list").setAttribute("aria-busy", on ? "true" : "false");
    el("ch-more").setAttribute("aria-disabled", on ? "true" : "false");
  }

  async function load(append) {
    if (append && busy) return;
    var mine = ++seq;
    var list = el("ch-list");
    var more = el("ch-more");
    var status = el("ch-status");
    var moreHadFocus = document.activeElement === more;
    if (!append) {
      view = currentFilters();
      typed = view.model;
      shown = 0;
      list.replaceChildren(skeleton(2));
      status.textContent = "Loading…";
    }
    var filters = view;
    var needle = filters.model.toLowerCase();
    var anyFilter = Boolean(filters.provider || filters.since || filters.model);
    setBusy(true);
    var body;
    try {
      var res = await api("GET", query(filters, shown));
      if (!res.ok) throw new Error(await errorText(res));
      body = await res.json();
    } catch (e) {
      if (mine !== seq) return;
      setBusy(false);
      if (!append) list.replaceChildren();
      list.appendChild(h("div", { className: "alert alert-error" }, "Catalog history unavailable: " + e.message));
      status.textContent = "Catalog history unavailable. Use Refresh to try again.";
      more.style.display = "none";
      return;
    }
    if (mine !== seq) return; // a newer request replaced this one
    setBusy(false);
    if (!append) list.replaceChildren();
    var added = body.entries.map(function (entry) { return card(entry, needle); });
    added.forEach(function (node) { list.appendChild(node); });
    shown += body.entries.length;
    if (!body.total) {
      var empty = anyFilter ? "No model change matches these filters." : "No model change recorded yet.";
      list.appendChild(emptyState("—", empty));
      status.textContent = empty;
    } else {
      status.textContent = shown + " of " + body.total + " changes";
    }
    var left = body.total - shown;
    more.textContent = "Load more (" + left + " left)";
    more.style.display = left > 0 && body.entries.length ? "" : "none";
    // Keep keyboard focus: on the button, or on the first new card once it is gone.
    if (append && moreHadFocus && added.length && more.style.display === "none") {
      added[0].setAttribute("tabindex", "-1");
      added[0].focus();
    }
    fillProviders(body.providers || []);
    footer(body.retention || {});
  }

  el("ch-provider").addEventListener("change", function () { load(false); });
  el("ch-period").addEventListener("change", function () { load(false); });
  el("ch-model").addEventListener("input", function () {
    clearTimeout(typing);
    typing = setTimeout(function () {
      if (modelText() !== typed) load(false);
    }, DEBOUNCE_MS);
  });
  el("ch-more").addEventListener("click", function () { load(true); });
  el("ch-refresh").addEventListener("click", function () { load(false); });

  window.loadCatalogHistoryPage = function () { load(false); };
  // Opened straight on #catalog-history after the tab switch already ran.
  if (el("page-catalog-history").classList.contains("active")) load(false);
})();

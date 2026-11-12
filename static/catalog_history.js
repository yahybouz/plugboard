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
  var shown = 0;        // entries on screen
  var seq = 0;          // the latest request; older answers are dropped
  var typing = null;

  function el(id) { return document.getElementById(id); }

  function modelText() { return el("ch-model").value.trim(); }

  function filtered() {
    return Boolean(el("ch-provider").value || el("ch-period").value || modelText());
  }

  function query(offset) {
    var params = new URLSearchParams();
    var days = el("ch-period").value;
    if (el("ch-provider").value) params.set("provider", el("ch-provider").value);
    if (days) params.set("since", new Date(Date.now() - Number(days) * DAY_MS).toISOString().slice(0, 10));
    if (modelText()) params.set("model", modelText());
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

  async function load(append) {
    var mine = ++seq;
    var list = el("ch-list");
    var more = el("ch-more");
    if (!append) {
      shown = 0;
      list.replaceChildren();
      list.appendChild(skeleton(2));
    }
    more.disabled = true;
    var body;
    try {
      var res = await api("GET", query(shown));
      if (!res.ok) throw new Error(await errorText(res));
      body = await res.json();
    } catch (e) {
      if (mine !== seq) return;
      if (!append) list.replaceChildren();
      list.appendChild(h("div", { className: "alert alert-error" }, "Catalog history unavailable: " + e.message));
      more.style.display = "none";
      return;
    }
    if (mine !== seq) return; // a newer request replaced this one
    if (!append) list.replaceChildren();
    var needle = modelText().toLowerCase();
    body.entries.forEach(function (entry) { list.appendChild(card(entry, needle)); });
    shown += body.entries.length;
    if (!body.total) {
      list.appendChild(emptyState("—", filtered() ? "No model change matches these filters." : "No model change recorded yet."));
    }
    var left = body.total - shown;
    more.textContent = "Load more (" + left + " left)";
    more.style.display = left > 0 && body.entries.length ? "" : "none";
    more.disabled = false;
    fillProviders(body.providers || []);
    footer(body.retention || {});
  }

  el("ch-provider").addEventListener("change", function () { load(false); });
  el("ch-period").addEventListener("change", function () { load(false); });
  el("ch-model").addEventListener("input", function () {
    clearTimeout(typing);
    typing = setTimeout(function () { load(false); }, DEBOUNCE_MS);
  });
  el("ch-more").addEventListener("click", function () { load(true); });

  window.loadCatalogHistoryPage = function () { load(false); };
})();

/* Live requests (PLU-13): "is it running or did it break?"
 *
 * - a pill in the corner: number of calls in flight, click for the list
 *   (provider, model, surface, elapsed, phase) and the last outcomes;
 * - a ticker for the Playground status line while a call is running.
 * Data: GET /v1/inflight (admin session: everything; client key: own calls).
 * Names come from callers: they are only ever written with textContent.
 */
(function () {
  "use strict";

  // The gateway allows 60 requests a minute per client address, for every route:
  // idle polling stays at 6 a minute, a running Playground call adds 20.
  var POLL_MS = 10000;
  var BUSY_POLL_MS = 3000;
  var PHASES = {
    waiting_slot: "queued (gateway busy)",
    running: "calling the provider",
    waiting_provider: "waiting for a free provider",
    cli_queue: "waiting for a CLI slot",
    cli_running: "CLI running",
    cli_parse: "reading the answer",
  };

  function phaseLabel(p) { return PHASES[p] || p || ""; }

  async function fetchLive() {
    var res = await fetch("/v1/inflight", { headers: { "X-Plugboard-Surface": "playground" } });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  function el(tag, text, cls) {
    var n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }

  function row(parts, state) {
    var r = el("div", null, "live-row");
    if (state) r.dataset.state = state;
    parts.forEach(function (t) { r.appendChild(el("span", t)); });
    return r;
  }

  var pill, panel, timer = null, last = null, stopped = false;

  function render() {
    if (!pill) return;
    var n = last ? last.active_count : 0;
    pill.textContent = n + " in flight";
    pill.dataset.count = String(n);
    pill.dataset.slow = last && last.active.some(function (e) { return e.slow; }) ? "1" : "0";
    if (panel.hidden) return;
    panel.textContent = "";
    panel.appendChild(el("div", "Running now", "live-title"));
    if (!last || !last.active.length) panel.appendChild(el("div", "Nothing running.", "live-empty"));
    (last ? last.active : []).forEach(function (e) {
      panel.appendChild(row([
        e.provider || "-", e.model || "-", e.surface,
        e.elapsed_s + " s", phaseLabel(e.phase) + (e.slow ? " (slow)" : ""),
      ], e.slow ? "slow" : "running"));
    });
    panel.appendChild(el("div", "Last results", "live-title"));
    (last ? last.recent : []).slice(0, 10).forEach(function (e) {
      panel.appendChild(row([
        e.provider || "-", e.model || "-", e.surface,
        e.elapsed_s + " s", (e.outcome || "?") + (e.status ? " " + e.status : ""),
      ], e.outcome));
    });
  }

  async function poll() {
    if (stopped || document.hidden) return;
    try {
      last = await fetchLive();
      render();
    } catch (err) {
      if (String(err.message).indexOf("401") !== -1 || String(err.message).indexOf("403") !== -1) {
        stopped = true; clearInterval(timer);
      }
    }
  }

  function mount() {
    var css = el("style", [
      "#live-pill{position:fixed;right:12px;bottom:12px;z-index:900;padding:4px 10px;border-radius:999px;",
      "font-size:12px;cursor:pointer;background:var(--bg-secondary,#151922);color:var(--text-secondary,#aab);",
      "border:1px solid var(--border-strong,#334);}",
      "#live-pill[data-count]:not([data-count='0']){color:#fff;border-color:#4c9;}",
      "#live-pill[data-slow='1']{border-color:#e90;}",
      "#live-panel{position:fixed;right:12px;bottom:46px;z-index:900;max-width:min(560px,94vw);max-height:60vh;",
      "overflow:auto;padding:8px 10px;border-radius:10px;font-size:12px;background:var(--bg-secondary,#151922);",
      "color:var(--text,#dde);border:1px solid var(--border-strong,#334);}",
      "#live-panel[hidden]{display:none;}",
      ".live-title{font-weight:600;margin:6px 0 2px;}.live-empty{opacity:.7;}",
      ".live-row{display:flex;gap:8px;flex-wrap:wrap;padding:2px 0;}",
      ".live-row[data-state='slow']{color:#e90;}.live-row[data-state='error']{color:#f66;}",
    ].join(""));
    document.head.appendChild(css);
    pill = el("button", "0 in flight");
    pill.id = "live-pill";
    pill.type = "button";
    pill.setAttribute("data-testid", "live-pill");
    panel = el("div");
    panel.id = "live-panel";
    panel.hidden = true;
    panel.setAttribute("data-testid", "live-panel");
    pill.addEventListener("click", function () { panel.hidden = !panel.hidden; poll().then(render); });
    document.body.appendChild(panel);
    document.body.appendChild(pill);
    timer = setInterval(poll, POLL_MS);
    poll();
  }

  // Playground status line: elapsed time + server-side phase while the call runs.
  function ticker(statusEl, model) {
    var t0 = performance.now(), phase = "", provider = "";
    var tick = setInterval(function () {
      if (statusEl.dataset.state !== "sending") { clearInterval(tick); clearInterval(poll2); return; }
      var secs = Math.floor((performance.now() - t0) / 1000);
      statusEl.textContent = "Running " + model + " - " + secs + " s" +
        (phase ? " - " + phase : "") + (provider ? " (" + provider + ")" : "");
    }, 500);
    var poll2 = setInterval(async function () {
      try {
        var d = await fetchLive();
        last = d; render();
        var mine = d.active.filter(function (e) { return e.surface === "playground" && e.model === model; })[0];
        if (mine) { phase = phaseLabel(mine.phase) + (mine.slow ? " (slow, still running)" : ""); provider = mine.provider || ""; }
      } catch (e) { /* the status line keeps counting */ }
    }, BUSY_POLL_MS);
  }

  window.plugboardLive = { ticker: ticker, phaseLabel: phaseLabel };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();

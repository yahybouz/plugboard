/* /suivi — Kanban of Plugboard tickets.
 * No inline script (strict CSP); every value from the API is set with
 * textContent, never as HTML. Status changes use a <select>, so the board
 * works with touch and keyboard (no drag and drop). */
(function () {
  "use strict";

  const LABELS = {
    a_trier: "À trier", pret: "Prêt", en_cours: "En cours", en_revue: "En revue",
    termine: "Terminé", bloque: "Bloqué", abandonne: "Abandonné",
  };
  const TYPE_LABELS = { fonction: "Fonction", bug: "Bug", dette: "Dette", securite: "Sécurité" };
  const PROOF_LABELS = { commande: "Commande", commit: "Commit", ci: "CI", chemin: "Chemin", lien: "Lien" };
  const NEEDS_REASON = ["bloque", "abandonne"];
  const WIDE = window.matchMedia("(min-width: 768px)");

  const board = document.getElementById("suivi-board");
  const msg = document.getElementById("suivi-msg");
  let statuts = Object.keys(LABELS);
  let typesPreuve = Object.keys(PROOF_LABELS);

  function say(text, isError) {
    msg.textContent = text || "";
    msg.classList.toggle("is-error", !!isError);
  }

  async function api(method, path, body) {
    const opts = { method: method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    if (res.status === 401) {
      location.href = "/login?next=/suivi";
      throw new Error("Session expirée");
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      let detail = data && data.detail;
      if (Array.isArray(detail)) detail = detail.map(function (d) { return d.msg; }).join(" ; ");
      throw new Error(detail || ("Erreur " + res.status));
    }
    return data;
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function option(value, label, selected) {
    const o = el("option", "", label);
    o.value = value;
    if (selected) o.selected = true;
    return o;
  }

  function statusSelect(ticket) {
    const label = el("label", "suivi-field");
    label.appendChild(el("span", "suivi-field-name", "Statut"));
    const select = el("select", "suivi-status");
    select.setAttribute("data-testid", "suivi-status-" + ticket.id);
    statuts.forEach(function (s) { select.appendChild(option(s, LABELS[s] || s, s === ticket.statut)); });
    select.addEventListener("change", function () { move(ticket, select); });
    label.appendChild(select);
    return label;
  }

  function proofs(ticket) {
    const box = el("details", "suivi-proofs");
    box.appendChild(el("summary", "", "Preuves (" + ticket.preuves.length + ")"));
    const list = el("ul", "suivi-proof-list");
    ticket.preuves.forEach(function (p) {
      list.appendChild(el("li", "", (PROOF_LABELS[p.type] || p.type) + " : " + p.valeur));
    });
    box.appendChild(list);
    const form = el("form", "suivi-proof-form");
    const type = el("select");
    type.setAttribute("aria-label", "Type de preuve");
    typesPreuve.forEach(function (t) { type.appendChild(option(t, PROOF_LABELS[t] || t, t === "ci")); });
    const value = el("input");
    value.type = "text";
    value.required = true;
    value.maxLength = 500;
    value.placeholder = "Ex. : ci journal plugboard <sha> : vert";
    value.setAttribute("aria-label", "Preuve");
    value.setAttribute("data-testid", "suivi-proof-" + ticket.id);
    const add = el("button", "suivi-btn suivi-btn-small", "Ajouter");
    add.type = "submit";
    form.append(type, value, add);
    form.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      try {
        await api("POST", "/admin/suivi/tickets/" + ticket.id + "/preuves", { type: type.value, valeur: value.value });
        say("Preuve ajoutée au ticket #" + ticket.id + ".");
        await load();
      } catch (e) { say(e.message, true); }
    });
    box.appendChild(form);
    return box;
  }

  function card(ticket) {
    const li = el("li", "suivi-card");
    li.setAttribute("data-id", String(ticket.id));
    const title = el("p", "suivi-card-title");
    title.append(el("span", "suivi-card-id", "#" + ticket.id + " "), document.createTextNode(ticket.titre));
    li.appendChild(title);
    const meta = el("p", "suivi-card-meta");
    meta.append(
      el("span", "suivi-tag suivi-prio-" + ticket.priorite, ticket.priorite),
      el("span", "suivi-tag", TYPE_LABELS[ticket.type] || ticket.type),
      el("span", "suivi-tag suivi-tag-muted", ticket.source)
    );
    li.appendChild(meta);
    if (ticket.description) li.appendChild(el("p", "suivi-card-desc", ticket.description));
    if (ticket.motif) li.appendChild(el("p", "suivi-card-motif", "Motif : " + ticket.motif));
    li.appendChild(statusSelect(ticket));
    li.appendChild(proofs(ticket));
    return li;
  }

  function render(tickets) {
    board.textContent = "";
    statuts.forEach(function (s) {
      const items = tickets.filter(function (t) { return t.statut === s; });
      const col = el("details", "suivi-col");
      col.setAttribute("data-statut", s);
      col.open = WIDE.matches || items.length > 0;
      const summary = el("summary", "suivi-col-head");
      summary.append(el("h2", "", LABELS[s] || s), el("span", "suivi-count", String(items.length)));
      col.appendChild(summary);
      const list = el("ul", "suivi-col-list");
      items.forEach(function (t) { list.appendChild(card(t)); });
      if (!items.length) list.appendChild(el("li", "suivi-empty", "Aucun ticket"));
      col.appendChild(list);
      board.appendChild(col);
    });
    board.setAttribute("aria-busy", "false");
  }

  async function load() {
    const data = await api("GET", "/admin/suivi/tickets");
    statuts = data.statuts || statuts;
    typesPreuve = data.types_preuve || typesPreuve;
    render(data.tickets || []);
  }

  async function move(ticket, select) {
    const target = select.value;
    const body = { statut: target };
    if (NEEDS_REASON.indexOf(target) !== -1) {
      const reason = window.prompt("Motif (obligatoire) pour « " + (LABELS[target] || target) + " » :", "");
      if (!reason || !reason.trim()) {
        select.value = ticket.statut;
        say("Changement annulé : un motif est obligatoire.", true);
        return;
      }
      body.motif = reason.trim();
    }
    try {
      await api("PATCH", "/admin/suivi/tickets/" + ticket.id, body);
      say("Ticket #" + ticket.id + " → " + (LABELS[target] || target) + ".");
      await load();
    } catch (e) {
      select.value = ticket.statut;
      say(e.message, true);
    }
  }

  document.getElementById("suivi-new").addEventListener("submit", async function (ev) {
    ev.preventDefault();
    const titre = document.getElementById("new-titre");
    try {
      const t = await api("POST", "/admin/suivi/tickets", {
        titre: titre.value,
        priorite: document.getElementById("new-priorite").value,
        type: document.getElementById("new-type").value,
      });
      titre.value = "";
      say("Ticket #" + t.id + " créé.");
      await load();
    } catch (e) { say(e.message, true); }
  });

  document.getElementById("suivi-import").addEventListener("click", async function (ev) {
    const button = ev.currentTarget;
    button.disabled = true;
    try {
      const c = await api("POST", "/admin/suivi/import");
      say("Import : " + c.crees + " créé(s), " + c.mis_a_jour + " passé(s) en revue, " + c.inchanges + " inchangé(s).");
      await load();
    } catch (e) { say(e.message, true); }
    finally { button.disabled = false; }
  });

  load().catch(function (e) { say(e.message, true); });
})();

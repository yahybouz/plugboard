/* People (PB-AUTH-01): personal dashboard keys, one per person, each with its
 * own authenticator secret.
 *
 * Data: GET and POST /admin/people, DELETE /admin/people/{id} (admin session).
 * A new key, its secret and its otpauth URI are shown once, as text, and
 * cleared from the page by "I have stored them" or by leaving the tab. They
 * never go to Web Storage, the URL or the console. Names are only ever
 * written as text, through h() or textContent, never as HTML; each one sits
 * next to its key prefix and its status, so two look-alike names stay apart.
 * Uses the helpers of app.js: api, h, table, toast, confirmAction,
 * emptyState, skeleton, _errorDetail.
 */
(function () {
  "use strict";

  var BADGES = { pending: "badge-orange", active: "badge-green", revoked: "badge-red" };
  var SHOWN_ONCE = ["new-person-key", "new-person-secret", "new-person-uri"];
  var people = {};    // person id -> its row of the last list, for "by" columns
  var seq = 0;        // the latest list request; older answers are dropped
  var creating = false;

  function el(id) { return document.getElementById(id); }

  // Clear the key, the secret and the URI from the page and hide them.
  function forget() {
    SHOWN_ONCE.forEach(function (id) { el(id).textContent = ""; });
    el("new-person-alert").classList.add("hidden");
  }

  function prefix(person) {
    return person.key_prefix ? person.key_prefix + "…" : person.id;
  }

  // A name with its key prefix: what tells two look-alike names apart.
  function label(person) {
    return person.name + " (" + prefix(person) + ")";
  }

  function when(at) {
    return at.slice(0, 16).replace("T", " ") + " UTC";
  }

  function actor(by) {
    if (by === "admin-key") return "ADMIN_KEY";
    if (by && by.indexOf("person:") === 0) {
      var id = by.slice("person:".length);
      return people[id] ? label(people[id]) : id;
    }
    return by || "";
  }

  // A date, and who acted when the record says so.
  function dated(at, by) {
    if (!at) return h("span", { className: "muted" }, "—");
    var who = by ? actor(by) : "";
    return h("div", null, when(at), who ? h("div", { className: "muted small" }, "by " + who) : null);
  }

  function row(person) {
    var status = person.status || "pending";
    var revoke = status === "revoked" ? null : h("button", {
      className: "btn btn-sm btn-danger",
      type: "button",
      "aria-label": "Revoke the key of " + person.name,
      onClick: function () { revokePerson(person); },
    }, "Revoke");
    return h("tr", null,
      h("td", null, h("strong", null, person.name)),
      h("td", null, h("code", null, prefix(person))),
      h("td", null, h("span", { className: "badge " + (BADGES[status] || "badge-orange") }, status)),
      h("td", null, dated(person.created_at, person.created_by)),
      h("td", null, person.last_used_at ? when(person.last_used_at) : h("span", { className: "muted" }, "—")),
      h("td", null, dated(person.revoked_at, person.revoked_by)),
      h("td", null, h("div", { className: "btn-group" }, revoke)));
  }

  async function load() {
    var mine = ++seq;
    var box = el("people-table-container");
    box.replaceChildren(skeleton(2));
    var body;
    try {
      var res = await api("GET", "/admin/people");
      if (!res.ok) throw new Error(await _errorDetail(res));
      body = await res.json();
    } catch (e) {
      if (mine !== seq) return;
      box.replaceChildren(h("div", { className: "alert alert-error" }, "People unavailable: " + e.message));
      return;
    }
    if (mine !== seq) return; // a newer request replaced this one
    var list = body.people || [];
    people = {};
    list.forEach(function (person) { people[person.id] = person; });
    if (!list.length) {
      box.replaceChildren(emptyState("—", "No personal key yet. Create one for each person who signs in to this dashboard."));
      return;
    }
    box.replaceChildren(table(
      ["Name", "Key", "Status", "Created", "Last used", "Revoked", "Actions"],
      list.map(row)));
  }

  async function revokePerson(person) {
    if (!await confirmAction("Revoke the key of " + label(person) + "? Their sessions end at once.", "Revoke")) return;
    try {
      var res = await api("DELETE", "/admin/people/" + encodeURIComponent(person.id));
      if (!res.ok) { toast("Could not revoke the key: " + await _errorDetail(res), "error"); return; }
    } catch (e) {
      toast("Could not revoke the key: " + e.message, "error");
      return;
    }
    toast("Key of " + person.name + " revoked", "success");
    load();
  }

  async function createPerson() {
    var input = el("person-name");
    var name = input.value.trim();
    if (!name) { input.focus(); return; }
    if (creating) return;
    creating = true;
    el("btn-submit-person").disabled = true;
    try {
      var res = await api("POST", "/admin/people", { name: name });
      if (!res.ok) { toast(await _errorDetail(res), "error"); return; }
      var made = await res.json();
      el("new-person-key").textContent = made.key;
      el("new-person-secret").textContent = made.totp_secret;
      el("new-person-uri").textContent = made.otpauth_uri;
      el("new-person-alert").classList.remove("hidden");
      el("create-person-form").classList.add("hidden");
      input.value = "";
      toast("Key created for " + made.person.name, "success");
      load();
    } catch (e) {
      toast("Could not create the key: " + e.message, "error");
    } finally {
      creating = false;
      el("btn-submit-person").disabled = false;
    }
  }

  // Copy one value shown once. Where the browser refuses the clipboard
  // (a page served over plain HTTP), select it for a manual copy instead.
  function copy(id) {
    var code = el(id);
    if (!code.textContent) return;
    function byHand() {
      window.getSelection().selectAllChildren(code);
      toast("The browser blocked the copy: the text is selected, copy it by hand.", "error");
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(code.textContent).then(function () { toast("Copied", "success"); }, byHand);
    } else {
      byHand();
    }
  }

  el("btn-create-person").addEventListener("click", function () {
    forget();
    el("create-person-form").classList.toggle("hidden");
    if (!el("create-person-form").classList.contains("hidden")) el("person-name").focus();
  });
  el("btn-cancel-person").addEventListener("click", function () { el("create-person-form").classList.add("hidden"); });
  el("btn-submit-person").addEventListener("click", createPerson);
  el("person-name").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") { ev.preventDefault(); createPerson(); }
  });
  el("btn-person-copied").addEventListener("click", forget);
  el("new-person-alert").addEventListener("click", function (ev) {
    var button = ev.target.closest("[data-copy]");
    if (button) copy(button.getAttribute("data-copy"));
  });
  // Leaving the tab clears what was shown once.
  window.addEventListener("hashchange", function () {
    if (location.hash.slice(1) !== "people") forget();
  });

  window.loadPeoplePage = function () { forget(); load(); };
  // Opened straight on #people after the tab switch already ran.
  if (el("page-people").classList.contains("active")) window.loadPeoplePage();
})();

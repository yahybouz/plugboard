# Outils MCP d'administration (PB-MCP-02) — plan d'exécution

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deux outils MCP, `add_provider` et `refresh_catalog`, utilisables en stdio seulement, derrière un interrupteur, qui passent par les routes admin de la passerelle et laissent une trace d'audit `mcp`.

**Architecture:** Le serveur MCP n'ajoute aucune logique métier : un garde (`_admin_gate`), un client HTTP admin (`_admin_request`) et des limites en mémoire, puis `POST /providers` et `POST /providers/{id}/discover` font tout le travail (validation, épinglage DNS, écriture, audit, webhook, cache). La passerelle ne change que pour noter l'acteur `mcp` et auditer les refus de cet acteur.

**Tech Stack:** Python 3.12, FastMCP (`mcp` 1.30.0), aiohttp (client et faux serveur sur `127.0.0.1:0`), FastAPI, httpx `ASGITransport`, pytest-asyncio (`asyncio_mode = auto`).

**Spec:** `docs/superpowers/specs/2026-11-12-mcp-admin-tools-design.md` — ce plan part des réponses **recommandées** Q1 à Q7. Autre choix de YB : Q1-2 ou Q2-3 → plan à refaire (approche C) ; Q2-2 → retirer l'interrupteur (tâche 1) ; Q3-2 → supprimer la tâche 2 ; Q4-2 → paramètre `egress_guard` (tâche 3) ; Q5-2/3 → tâches 1 et 4 ; Q6-2 → `is_active=True` (tâche 3) ; Q7-2 → une tâche de plus.

## Global Constraints

- Atelier unique : `/srv/dev/worktrees/plugboard/mcp-admin`, branche `feat/mcp-admin-tools`. Rien hors de l'atelier ; aucune commande `board`.
- Tests ciblés, un pytest à la fois, jamais tout `tests/`. Commande notée `$PYTEST` :
  `cd /srv/dev/worktrees/plugboard/mcp-admin && TMPDIR=/tmp/claude-1001/-srv-dev-plugboard/a5d91e11-1edb-4748-99e2-79712c823769/scratchpad/mcp-admin PLUGBOARD_SKIP_DOTENV=1 PYTHONDONTWRITEBYTECODE=1 /srv/dev/plugboard/.venv/bin/python -m pytest <fichiers> -q -p no:cacheprovider`
- Contrat MCP régénéré avec le venv principal dans toute tâche qui ajoute un outil, sinon `tests/test_contract_without_fastapi.py` échoue. Commande notée `$CONTRACT` :
  `cd /srv/dev/worktrees/plugboard/mcp-admin && S=/tmp/claude-1001/-srv-dev-plugboard/a5d91e11-1edb-4748-99e2-79712c823769/scratchpad/mcp-admin && TMPDIR=$S PLUGBOARD_DATA=$S/data PLUGBOARD_SKIP_DOTENV=1 PYTHONDONTWRITEBYTECODE=1 /srv/dev/plugboard/.venv/bin/python scripts/export_contract.py --mcp`
- `plugboard/mcp_server.py` n'importe jamais `plugboard.app` ni `fastapi`. `CONTRACT_VERSION` reste `1.5.0`. Ne pas toucher `VERSION` ni `plugboard/__init__.py`.
- Valeurs fixes : interrupteur `PLUGBOARD_MCP_ADMIN_TOOLS` (`"1"` seulement) ; 5 `add_provider` par 3600 s glissantes, refus compris ; par fournisseur, 1 appel par 60 s et par mode (aperçu, écriture) ; un rafraîchissement à la fois ; délai HTTP admin `max(1, min(_TIMEOUT, 120))` s ; redirections refusées.
- Messages des outils : anglais, préfixe `Error: ` ajouté par l'outil, jamais par les helpers. Textes exacts :
  - réseau : `<tool> is not available over a network MCP transport: PLUGBOARD_ADMIN_KEY is only loaded for stdio.`
  - interrupteur absent : `MCP admin tools are off: set PLUGBOARD_MCP_ADMIN_TOOLS=1 and PLUGBOARD_ADMIN_KEY (stdio only)`
  - pas de clé admin : `<tool> needs PLUGBOARD_ADMIN_KEY (stdio only)`
  - 401 avec `Admin MFA required` : `the gateway enforces admin MFA: MCP admin tools cannot sign in, use the dashboard`
  - autre 401 : `the gateway refused PLUGBOARD_ADMIN_KEY`
  - passerelle injoignable : `could not reach the gateway (<type(exc).__name__>)`
  - autre refus : `HTTP <status>: <detail>` (`detail` de la réponse, `json.dumps` s'il n'est pas une chaîne, 300 caractères au plus)
  - limite de création : `add_provider limit reached (5 per hour): retry in <n>s`
  - délai de rafraîchissement : `catalog of '<id>' was refreshed less than 60 s ago: retry in <n>s`
  - rafraîchissement en cours : `a catalog refresh is already running`
  - fournisseur CLI : `'<id>' is a CLI provider: its refresh runs model probes, refresh it from the dashboard`
- La clé du fournisseur (`api_key`) n'apparaît jamais dans une réponse d'outil, un détail d'audit ou un journal ; toute erreur de `add_provider` passe par `_scrub(text, api_key)`.
- Clés et hôtes de test synthétiques (`sk-synthetic-provider-key`, `adminkey`, `*.test`, `pirate.example`). DNS toujours simulé (`plugboard.llm_pricing._resolve_public`). Aucune inférence, aucun appel hors `127.0.0.1`, jamais l'appli UAT `127.0.0.1:18765`.
- Tout état global isolé par monkeypatch : `audit._DB_PATH`, fichier des fournisseurs (fixtures `_providers_file` / `_empty_providers_file`), `ratelimit._LIMIT`, état des limites MCP.
- Doc en anglais ; CHANGELOG sous `## [Unreleased]`. Conventional Commits, un commit par tâche, lignes d'attribution de la session.
- Lint : `/srv/dev/plugboard/.venv/bin/ruff check plugboard/ tests/` et `ruff format --check plugboard/ tests/`.

## Review Focus

1. Passerelle arrêtée pendant `add_provider` : `could not reach the gateway (...)`, rien de créé. Tests aux tâches 1 et 3.
2. Nom déjà pris (`"Test Provider"` → `test-provider-1`) : la réponse donne l'identifiant renvoyé par la passerelle, pas le nom passé en minuscules. Test à la tâche 3.
3. MFA imposée et clé admin fausse donnent deux messages différents. Tests à la tâche 3.
4. `provider_id` avec `/`, `..`, `?` (`"../admin"`) : encodé (`quote(..., safe="")`), jamais une autre route admin. Test à la tâche 4.
5. `base_url` avec `/` final (`https://api.acme.test/v1/`) : la réponse montre l'URL enregistrée, sans `/` final. Test à la tâche 3.

---

### Task 1: Garde, client HTTP admin et limites du serveur MCP

**Files:**
- Modify: `plugboard/mcp_server.py` — configuration (~ligne 53), `configure_network_transport` (~126), nouveau bloc « Admin tools » après `_registry_client` (~365)
- Create: `tests/test_mcp_admin_tools.py`

**Interfaces:**
- Consumes: `_auth_headers(admin=True, json_body=...)`, `_get_http_session()`, `_ADMIN_KEY`, `_NETWORK_TRANSPORT`, `_TIMEOUT`.
- Produces:
  - `_ADMIN_TOOLS: bool` (lu à l'import : `os.environ.get("PLUGBOARD_MCP_ADMIN_TOOLS", "").strip() == "1"`) ; le garde teste le réseau avant lui, `configure_network_transport` ne change pas.
  - `class AdminToolError(Exception)` — `str(exc)` est le message, sans `Error: `.
  - `def _admin_gate(tool: str) -> None` — dans cet ordre : réseau, interrupteur, clé ; aucun appel HTTP.
  - `async def _admin_request(method: str, path: str, body: dict | None = None) -> tuple[int, Any]` — `(status, json)` ; corps non JSON → `{"detail": text[:300]}` ; `aiohttp.ClientError` ou `asyncio.TimeoutError` → `AdminToolError("could not reach the gateway (...)")`.
  - `def _gateway_error(status: int, payload: Any) -> str` et `def _scrub(text: str, secret: str) -> str` (`secret` vide : texte inchangé ; sinon chaque occurrence → `***`).
  - Limites : `_clock = time.monotonic`, `_create_calls: collections.deque[float]`, `_refresh_last: dict[tuple[str, bool], float]`, `_refresh_lock: asyncio.Lock` ; `def _take_create_slot() -> None`, `def _take_refresh_slot(provider_id: str, dry_run: bool) -> None` (lèvent `AdminToolError`, comptent l'essai dès qu'il passe).
  - `def _cap_sdk_message_logging() -> None` — logger `mcp.server.lowlevel.server` : niveau `NOTSET` ou `< INFO` → `INFO` ; appelé à l'import si `_ADMIN_TOOLS`.
  - Fixture de test `admin_on(monkeypatch)` : `_ADMIN_TOOLS=True`, `_ADMIN_KEY="adminkey"`, `_NETWORK_TRANSPORT=False`, limites remises à neuf (`deque()`, `{}`, `asyncio.Lock()`), `_clock` remplacé par `clock = {"now": 1000.0}`.

- [ ] **Step 1: Écrire les tests qui échouent**

```python
def test_gate_refuses_network_first(admin_on, monkeypatch):
    monkeypatch.setattr(mcp_server, "_NETWORK_TRANSPORT", True)
    with pytest.raises(mcp_server.AdminToolError, match="only loaded for stdio"):
        mcp_server._admin_gate("add_provider")

def test_gate_refuses_without_switch(admin_on, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ADMIN_TOOLS", False)
    with pytest.raises(mcp_server.AdminToolError, match="PLUGBOARD_MCP_ADMIN_TOOLS=1"):
        mcp_server._admin_gate("add_provider")

def test_gate_refuses_without_admin_key(admin_on, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ADMIN_KEY", "")
    with pytest.raises(mcp_server.AdminToolError, match="add_provider needs PLUGBOARD_ADMIN_KEY"):
        mcp_server._admin_gate("add_provider")

async def test_admin_request_sends_admin_key_and_mcp_surface(admin_on, fake_gateway):
    # fake_gateway : aiohttp web.Application sur 127.0.0.1:0, _BASE_URL pointé dessus,
    # note les en-têtes reçus et répond {"ok": True}
    assert await mcp_server._admin_request("POST", "/providers", {"a": 1}) == (200, {"ok": True})
    assert fake_gateway["headers"]["Authorization"] == "Bearer adminkey"
    assert fake_gateway["headers"]["X-Plugboard-Surface"] == "mcp"

async def test_admin_request_does_not_follow_redirects(admin_on, fake_gateway):
    fake_gateway["redirect"] = "/elsewhere"
    status, _ = await mcp_server._admin_request("GET", "/providers")
    assert status == 302 and fake_gateway["hits"] == ["/providers"]

async def test_admin_request_unreachable_gateway(admin_on, monkeypatch):
    monkeypatch.setattr(mcp_server, "_BASE_URL", "http://127.0.0.1:9")
    with pytest.raises(mcp_server.AdminToolError, match="could not reach the gateway"):
        await mcp_server._admin_request("GET", "/providers")

def test_gateway_error_messages():
    assert "enforces admin MFA" in mcp_server._gateway_error(401, {"detail": "Admin MFA required: sign in, or send X"})
    assert mcp_server._gateway_error(401, {"detail": "Admin key required"}) == "the gateway refused PLUGBOARD_ADMIN_KEY"
    assert mcp_server._gateway_error(422, {"detail": [{"loc": ["x"]}]}) == 'HTTP 422: [{"loc": ["x"]}]'
    assert len(mcp_server._gateway_error(400, {"detail": "x" * 1000})) <= len("HTTP 400: ") + 300

def test_scrub_removes_every_copy_of_the_key():
    assert mcp_server._scrub("a sk-k b sk-k", "sk-k") == "a *** b ***"
    assert mcp_server._scrub("abc", "") == "abc"

def test_sixth_creation_in_an_hour_is_refused(admin_on):
    for _ in range(5):
        mcp_server._take_create_slot()
    with pytest.raises(mcp_server.AdminToolError, match=r"limit reached \(5 per hour\): retry in 3600s"):
        mcp_server._take_create_slot()
    admin_on["now"] += 3601
    mcp_server._take_create_slot()

def test_refresh_slot_is_per_provider_and_mode(admin_on):
    mcp_server._take_refresh_slot("p", dry_run=True)
    mcp_server._take_refresh_slot("p", dry_run=False)
    mcp_server._take_refresh_slot("q", dry_run=False)
    with pytest.raises(mcp_server.AdminToolError, match="less than 60 s ago: retry in 60s"):
        mcp_server._take_refresh_slot("p", dry_run=False)
    admin_on["now"] += 61
    mcp_server._take_refresh_slot("p", dry_run=False)

@pytest.mark.parametrize("before,after", [(logging.DEBUG, logging.INFO), (logging.NOTSET, logging.INFO), (logging.WARNING, logging.WARNING)])
def test_sdk_message_logging_is_capped(before, after):
    lg = logging.getLogger("mcp.server.lowlevel.server")
    old = lg.level
    try:
        lg.setLevel(before)
        mcp_server._cap_sdk_message_logging()
        assert lg.level == after
    finally:
        lg.setLevel(old)
```

`retry in <n>s` : `n = int(reste) + 1` borné à la fenêtre (3600 au premier refus, 60 pour le rafraîchissement), comme `admin_models_refresh`.

- [ ] **Step 2: Lancer, constater l'échec** — `$PYTEST tests/test_mcp_admin_tools.py` → FAIL (`AttributeError: ... '_admin_gate'`).
- [ ] **Step 3: Implémenter** les interfaces ci-dessus dans `plugboard/mcp_server.py` ; `_admin_request` lit au plus 1 Mio de corps, `allow_redirects=False`.
- [ ] **Step 4: Relancer** — `$PYTEST tests/test_mcp_admin_tools.py tests/test_mcp_client_identity.py tests/test_inherited_auth_v7.py` → PASS ; ruff propre.
- [ ] **Step 5: Commit** — `feat(mcp): gate, admin client and limits for MCP admin tools`

### Task 2: La passerelle note l'acteur `mcp` et audite ses refus

**Files:**
- Modify: `plugboard/app.py` — `add_provider` (~3771), `discover_provider_models` (~3918), `_require_guard_allows` (~4343), nouveau `_admin_actor` à côté
- Create: `tests/test_provider_audit_actor.py`

**Interfaces:**
- Consumes: `_live.normalize_surface`, `_live.SURFACE_HEADER` (`plugboard/live_requests.py`), `_audit` (`audit.log_action`).
- Produces:
  - `def _admin_actor(request: Request) -> str` — `"mcp"` si la surface normalisée vaut `mcp`, sinon `"admin"`.
  - `_require_guard_allows(provider_id, base_url, *, actor: str = "admin")`.
  - Actions d'audit, acteur passé partout : `provider.create`, `provider.domain.rejected`, `provider.egress.rejected`, `provider.discover` (inchangées) ; pour l'acteur `mcp` seulement : `provider.create.refused` (refus 400, détails `{"name", "base_url", "status", "reason"}`), `provider.discover.refused` (tout `HTTPException`, détails `{"status", "reason"}`), `provider.discover.preview` (`dry_run=true` réussi, détails comme `provider.discover`).

- [ ] **Step 1: Écrire les tests qui échouent** (fixtures `admin_client`, `audit_db` comme `tests/test_egress_guard_api.py`, DNS simulé)

```python
MCP = {"Authorization": "Bearer adminkey", "X-Plugboard-Surface": "mcp"}
DASH = {"Authorization": "Bearer adminkey"}

async def test_create_from_mcp_is_audited_as_mcp(admin_client, audit_db, public):
    r = await admin_client.post("/providers", headers=MCP, json={"name": "Acme", "base_url": "https://api.acme.test/v1", "bearer_token": "sk-synthetic-provider-key", "egress_guard": True})
    assert r.status_code == 201
    e = audit_mod.get_audit_log(action="provider.create")[0]
    assert e["actor"] == "mcp" and e["target_id"] == "acme"

async def test_dashboard_create_is_unchanged(admin_client, audit_db, public):
    await admin_client.post("/providers", headers=DASH, json={"name": "Acme", "base_url": "https://api.acme.test/v1", "bearer_token": "k"})
    assert audit_mod.get_audit_log(action="provider.create")[0]["actor"] == "admin"

async def test_mcp_egress_refusal_is_audited_as_mcp(admin_client, audit_db, private):
    r = await admin_client.post("/providers", headers=MCP, json={"name": "In", "base_url": "http://10.0.0.5/v1", "bearer_token": "k", "egress_guard": True})
    assert r.status_code == 422
    assert audit_mod.get_audit_log(action="provider.egress.rejected")[0]["actor"] == "mcp"

async def test_mcp_value_error_refusal_is_audited(admin_client, audit_db, public):
    r = await admin_client.post("/providers", headers=MCP, json={"name": "OpenClaw", "base_url": "https://x.test", "bearer_token": "k", "egress_guard": True})
    assert r.status_code == 400
    e = audit_mod.get_audit_log(action="provider.create.refused")[0]
    assert e["actor"] == "mcp" and json.loads(e["details"])["status"] == 400

async def test_dashboard_value_error_is_not_audited(admin_client, audit_db, public):
    await admin_client.post("/providers", headers=DASH, json={"name": "OpenClaw", "base_url": "https://x.test", "bearer_token": "k"})
    assert audit_mod.get_audit_log(action="provider.create.refused") == []

async def test_mcp_discover_refusal_preview_and_write_are_audited(admin_client, audit_db, monkeypatch):
    async def found(provider):
        return [{"id": "m1"}]
    monkeypatch.setattr("plugboard.app.discover_models", found)
    assert (await admin_client.post("/providers/nope/discover", headers=MCP)).status_code == 404
    await admin_client.post("/providers/test-provider/discover?dry_run=true", headers=MCP)
    await admin_client.post("/providers/test-provider/discover", headers=MCP)
    assert audit_mod.get_audit_log(action="provider.discover.refused")[0]["target_id"] == "nope"
    assert audit_mod.get_audit_log(action="provider.discover.preview")[0]["actor"] == "mcp"
    assert audit_mod.get_audit_log(action="provider.discover")[0]["actor"] == "mcp"

@pytest.mark.parametrize("dns", ["public", "private"])
async def test_provider_key_never_reaches_the_audit(admin_client, audit_db, request, dns):
    request.getfixturevalue(dns)  # création réussie (public) ou refusée (private)
    await admin_client.post("/providers", headers=MCP, json={"name": "K", "base_url": "https://k.test", "bearer_token": "sk-synthetic-provider-key", "egress_guard": True})
    assert "sk-synthetic-provider-key" not in json.dumps(audit_mod.get_audit_log(limit=500))
```

- [ ] **Step 2: Lancer, constater l'échec** — `$PYTEST tests/test_provider_audit_actor.py` → FAIL (`actor == "admin"`).
- [ ] **Step 3: Implémenter** : `request: Request` ajouté aux deux routes ; corps de `discover_provider_models` déplacé dans `async def _discover(provider_id: str, dry_run: bool, actor: str) -> dict`, la route attrape `HTTPException`, audite si `actor == "mcp"`, relance.
- [ ] **Step 4: Relancer** — `$PYTEST tests/test_provider_audit_actor.py`, puis `$PYTEST tests/test_egress_guard_api.py`, puis `$PYTEST tests/test_audit_e2e.py`, puis `$PYTEST tests/test_review_fixes.py` → PASS ; ruff propre.
- [ ] **Step 5: Commit** — `feat(audit): record MCP admin calls and their refusals as actor mcp`

### Task 3: Outil `add_provider`

**Files:**
- Modify: `plugboard/mcp_server.py` — après `provider_status` (~ligne 831) ; docstring du module (liste des outils)
- Modify: `tests/test_mcp_admin_tools.py`
- Modify: `docs/api/mcp.json` (régénéré)

**Interfaces:**
- Consumes: tâche 1 (`_admin_gate`, `_admin_request`, `_gateway_error`, `_scrub`, `_take_create_slot`, `AdminToolError`).
- Produces: `@mcp.tool() async def add_provider(name: str, base_url: str, api_key: str, default_model: str = "") -> str` — ordre : garde, limite, `POST /providers` avec `{"name", "base_url", "bearer_token": api_key, "default_model", "is_active": False, "egress_guard": True}`. Réussite (201) : `Provider '<id>' created, inactive, public addresses only (<base_url>). Run refresh_catalog('<id>'), then activate it in the dashboard (Providers).` — `<id>` et `<base_url>` lus dans la réponse.
- Fixture `gateway(admin_on, _empty_providers_file, _admin_key, audit_db, monkeypatch)` : remplace `mcp_server._admin_request` par un relais vers l'app FastAPI réelle (`httpx.AsyncClient(transport=ASGITransport(app=app))`, en-têtes `mcp_server._auth_headers(admin=True, json_body=body is not None)`), note `(method, path)` dans `gateway["calls"]` ; `ratelimit._LIMIT = 0`. Helper `direct(body) -> tuple[int, Any]` : même app, en-têtes admin sans surface.

- [ ] **Step 1: Écrire les tests qui échouent**

```python
async def test_add_provider_creates_an_inactive_guarded_provider(gateway, public):
    text = await mcp_server.add_provider("Acme", "https://api.acme.test/v1/", "sk-synthetic-provider-key")
    assert text.startswith("Provider 'acme' created, inactive") and "(https://api.acme.test/v1)" in text
    p = config.get_provider("acme")
    assert p["is_active"] is False and p["egress_guard"] is True
    assert "sk-synthetic-provider-key" not in text

async def test_add_provider_returns_the_gateway_id_on_a_name_clash(gateway, public):
    await mcp_server.add_provider("Acme", "https://api.acme.test/v1", "k1")
    assert "'acme-1'" in await mcp_server.add_provider("Acme", "https://api.acme.test/v1", "k2")

@pytest.mark.parametrize("off", ["network", "switch", "key"])
async def test_add_provider_refused_without_rights_and_without_a_call(gateway, monkeypatch, off):
    monkeypatch.setattr(mcp_server, *{"network": ("_NETWORK_TRANSPORT", True), "switch": ("_ADMIN_TOOLS", False), "key": ("_ADMIN_KEY", "")}[off])
    assert (await mcp_server.add_provider("Acme", "https://api.acme.test/v1", "k")).startswith("Error: ")
    assert gateway["calls"] == []

@pytest.mark.parametrize("name,url,resolver,env", [
    ("In", "http://10.0.0.5/v1", "private", None),             # 422 non public
    ("Ftp", "ftp://files.test", "public", None),               # 422 http(s) exigé
    ("Gone", "https://gone.test", "unresolvable", None),       # 503
    ("Out", "https://api.other.test", "public", "api.acme.test"),  # 400 domaine hors liste
    ("OpenClaw", "https://x.test", "public", None),            # 400 connecteur retiré
])
async def test_add_provider_gives_the_admin_api_refusal(gateway, monkeypatch, request, name, url, resolver, env):
    ...  # active le résolveur DNS ; env → PLUGBOARD_PROVIDER_DOMAINS_ALLOWED
    status, payload = await gateway["direct"]({"name": name, "base_url": url, "bearer_token": "k", "is_active": False, "egress_guard": True})
    assert await mcp_server.add_provider(name, url, "k") == "Error: " + mcp_server._gateway_error(status, payload)
    assert status in (400, 422, 503) and config.load_providers() == []

async def test_key_is_scrubbed_from_a_refusal(gateway, monkeypatch):
    async def echo(method, path, body=None):
        return 422, {"detail": f"bad key {body['bearer_token']}"}
    monkeypatch.setattr(mcp_server, "_admin_request", echo)
    text = await mcp_server.add_provider("A", "https://a.test", "sk-synthetic-provider-key")
    assert "sk-synthetic-provider-key" not in text and "***" in text

async def test_add_provider_with_enforced_mfa(gateway, monkeypatch):
    monkeypatch.setattr("plugboard.admin_mfa.is_required", lambda: True)
    assert "enforces admin MFA" in await mcp_server.add_provider("A", "https://a.test", "k")

async def test_add_provider_with_a_wrong_admin_key(gateway, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ADMIN_KEY", "wrong-admin-key")
    assert await mcp_server.add_provider("A", "https://a.test", "k") == "Error: the gateway refused PLUGBOARD_ADMIN_KEY"

async def test_add_provider_gateway_down(admin_on, monkeypatch):
    monkeypatch.setattr(mcp_server, "_BASE_URL", "http://127.0.0.1:9")
    assert (await mcp_server.add_provider("A", "https://a.test", "k")).startswith("Error: could not reach the gateway")

async def test_sixth_add_provider_is_refused_before_the_gateway(gateway, private):
    for i in range(5):
        await mcp_server.add_provider(f"P{i}", "http://10.0.0.5", "k")
    assert "limit reached" in await mcp_server.add_provider("P5", "http://10.0.0.5", "k")
    assert len(gateway["calls"]) == 5
```

Le résolveur « unresolvable » lève `UnsafeURL("cannot resolve gone.test: synthetic")` ; « public » renvoie `[]` ; « private » lève `UnsafeURL("... resolves to a non-public address (10.0.0.5)")`.

- [ ] **Step 2: Lancer, constater l'échec** — `$PYTEST tests/test_mcp_admin_tools.py` → FAIL (`AttributeError: ... 'add_provider'`).
- [ ] **Step 3: Implémenter** `add_provider` ; erreurs : `"Error: " + _scrub(message, api_key)`.
- [ ] **Step 4: Relancer** — `$PYTEST tests/test_mcp_admin_tools.py` → PASS ; `$CONTRACT` ; `$PYTEST tests/test_contract_without_fastapi.py` → PASS ; ruff propre.
- [ ] **Step 5: Commit** — `feat(mcp): add_provider tool, stdio only, inactive and guarded`

### Task 4: Outil `refresh_catalog`

**Files:**
- Modify: `plugboard/mcp_server.py` — après `add_provider` ; docstring du module
- Modify: `tests/test_mcp_admin_tools.py`
- Modify: `docs/api/mcp.json` (régénéré)

**Interfaces:**
- Consumes: tâche 1 ; relais de la fixture `gateway` (tâche 3).
- Fixtures de test : `gateway_with_providers` (le relais, sur `_providers_file` : `test-provider`, `other-provider`, plus `cli-x` à `auth_source: "subprocess:agy"`) ; `found_m1` (`plugboard.app.discover_models` renvoie `[{"id": "m1"}]`, compte ses appels dans `found_m1["calls"]`) ; `blocking_discovery` (pose `started`, attend `release`, puis renvoie `[{"id": "m1"}]`) ; `untrusted_discovery` (`router._discovery_trusted.set(False)`, puis `[{"id": "m1"}]`).
- Produces: `@mcp.tool() async def refresh_catalog(provider_id: str, dry_run: bool = False) -> str` — ordre : garde ; `GET /providers`, refus si l'entrée de même `id` a un `auth_source` commençant par `subprocess:` ou `cli:` (id absent : on continue, la passerelle répond 404) ; `_take_refresh_slot` ; `_refresh_lock` pris sans attendre (`locked()` → refus) ; `POST /providers/<quote(provider_id, safe="")>/discover` (+ `?dry_run=true`). Réussite : `Catalog of '<id>': <discovered> models; added: <a, b | none>; removed: <c | none>`, plus ` (preview, nothing written)` si aperçu, plus `; removals skipped: the provider's list is not trusted` si `removals_allowed` est faux.

- [ ] **Step 1: Écrire les tests qui échouent** (`plugboard.app.discover_models` simulé, comme `tests/test_review_fixes.py:428`)

```python
async def test_refresh_writes_the_catalog(gateway_with_providers, found_m1):
    text = await mcp_server.refresh_catalog("test-provider")
    assert text.startswith("Catalog of 'test-provider': 1 models; added: m1")
    assert [m["id"] for m in config.get_provider("test-provider")["models"]] == ["m1"]

async def test_preview_writes_nothing(gateway_with_providers, found_m1):
    assert "(preview, nothing written)" in await mcp_server.refresh_catalog("test-provider", dry_run=True)
    assert not config.get_provider("test-provider").get("models")

async def test_unknown_provider_gives_the_admin_api_refusal(gateway_with_providers):
    assert await mcp_server.refresh_catalog("nope") == "Error: HTTP 404: Provider 'nope' not found"

async def test_path_characters_are_encoded(gateway_with_providers):
    text = await mcp_server.refresh_catalog("../admin")
    assert ("POST", "/providers/..%2Fadmin/discover") in gateway_with_providers["calls"]
    assert text.startswith("Error: HTTP 404")

async def test_cli_provider_is_refused_before_discovery(gateway_with_providers, found_m1):
    assert "is a CLI provider" in await mcp_server.refresh_catalog("cli-x")
    assert found_m1["calls"] == 0

async def test_second_write_within_a_minute_is_refused(gateway_with_providers, found_m1, admin_on):
    await mcp_server.refresh_catalog("test-provider", dry_run=True)
    await mcp_server.refresh_catalog("test-provider")
    assert "less than 60 s ago" in await mcp_server.refresh_catalog("test-provider")
    admin_on["now"] += 61
    assert not (await mcp_server.refresh_catalog("test-provider")).startswith("Error")

async def test_one_refresh_at_a_time(gateway_with_providers, blocking_discovery):
    first = asyncio.create_task(mcp_server.refresh_catalog("test-provider"))
    await blocking_discovery["started"].wait()
    assert await mcp_server.refresh_catalog("other-provider") == "Error: a catalog refresh is already running"
    blocking_discovery["release"].set()
    assert (await first).startswith("Catalog of")

async def test_untrusted_list_says_removals_were_skipped(gateway_with_providers, untrusted_discovery):
    assert "removals skipped" in await mcp_server.refresh_catalog("test-provider")

@pytest.mark.parametrize("off", ["network", "switch", "key"])
async def test_refresh_refused_without_rights_and_without_a_call(gateway_with_providers, monkeypatch, off):
    ...  # comme la tâche 3
    assert gateway_with_providers["calls"] == []
```

- [ ] **Step 2: Lancer, constater l'échec** — `$PYTEST tests/test_mcp_admin_tools.py` → FAIL (`AttributeError: ... 'refresh_catalog'`).
- [ ] **Step 3: Implémenter** `refresh_catalog`.
- [ ] **Step 4: Relancer** — `$PYTEST tests/test_mcp_admin_tools.py` → PASS ; `$CONTRACT` ; `$PYTEST tests/test_contract_without_fastapi.py` → PASS ; ruff propre.
- [ ] **Step 5: Commit** — `feat(mcp): refresh_catalog tool with per-provider cooldown`

### Task 5: Documentation, CHANGELOG, vérification

**Files:**
- Modify: `docs/guide/mcp.md` — table « Configure » (`PLUGBOARD_ADMIN_KEY` : « `provider_status` and the admin tools » ; nouvelle ligne `PLUGBOARD_MCP_ADMIN_TOOLS`), puce « Network transports » (les outils admin refusent aussi), table « Tools » (2 lignes), nouvelle section `## Admin tools (stdio only)`
- Modify: `scripts/run_mcp.py` — docstring (variables)
- Modify: `CHANGELOG.md` — `### Added` sous `## [Unreleased]`

- [ ] **Step 1: Section `## Admin tools (stdio only)`**, une phrase par point : activation (`PLUGBOARD_MCP_ADMIN_TOOLS=1` + `PLUGBOARD_ADMIN_KEY`, stdio) ; refus si MFA admin imposée ; `add_provider` crée un fournisseur **inactif**, « Public addresses only » imposé, à activer dans le tableau de bord ; mêmes refus que `POST /providers` ; `refresh_catalog` = bouton Discover, `dry_run` pour un aperçu, fournisseurs CLI refusés ; limites (5/heure, 60 s par fournisseur et par mode, un à la fois) ; audit acteur `mcp` (réussites, aperçus, refus ; l'en-tête est déclaratif) ; la clé passe par le contexte de l'agent, n'est jamais renvoyée, et le serveur ne journalise pas les messages MCP en DEBUG ; pas d'outil de modification ni de suppression.
- [ ] **Step 2: CHANGELOG** — une entrée `**MCP admin tools: add_provider and refresh_catalog**` qui reprend ces points en trois phrases, lien `docs/guide/mcp.md#admin-tools-stdio-only`, et « MCP contract 1.5.0 ».
- [ ] **Step 3: Vérifier** — `$PYTEST` successivement sur `tests/test_mcp_admin_tools.py`, `tests/test_provider_audit_actor.py`, `tests/test_mcp_client_identity.py`, `tests/test_inherited_auth_v7.py`, `tests/test_run_mcp_legacy_names.py`, `tests/test_egress_guard_api.py`, `tests/test_audit_e2e.py`, `tests/test_contract_without_fastapi.py` → PASS ; `$CONTRACT --check` → à jour ; ruff check et format propres ; `make secret-scan` → aucune fuite.
- [ ] **Step 4: Commit** — `docs(mcp): document the MCP admin tools`

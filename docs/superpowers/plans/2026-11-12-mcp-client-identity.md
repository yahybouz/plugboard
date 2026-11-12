# Identité du client MCP tirée de sa clé (PB-MCP-01) — plan d'exécution

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Les outils MCP du registre de mises à jour agissent pour le client de `PLUGBOARD_API_KEY`, jamais pour un client nommé par l'appelant.

**Architecture:** Une fonction `_caller_client_id()` demande `GET /v1/me` à la passerelle avec la clé client du processus et renvoie son `client_id` ; `_registry_client(claimed)` y ajoute le contrôle de l'argument facultatif `client_id`. Les cinq points d'entrée du registre l'appellent en premier, avant toute lecture du registre ou de la base `client_updates.db`. Aucun nouveau module, aucune nouvelle route de passerelle.

**Tech Stack:** Python 3.12, FastMCP (`mcp` 1.30.0), aiohttp (faux serveur de passerelle sur `127.0.0.1:0`), httpx `ASGITransport`, uvicorn, pytest-asyncio (`asyncio_mode = auto`).

**Spec:** `docs/superpowers/specs/2026-11-12-mcp-client-identity-design.md` — ce plan part des réponses **recommandées** Q1 à Q6. Si YB en choisit une autre, ajuster les tâches citées : Q1 → tâche 1 ; Q2, Q3 → tâches 1 à 3 ; Q4 → tâche 4 (supprimée si option 2 ou 3) ; Q5, Q6 → tâche 2.

## Global Constraints

- Atelier unique : `/srv/dev/worktrees/plugboard/mcp-identity`, branche `fix/mcp-client-identity`. Rien hors de l'atelier.
- Tests ciblés, un pytest à la fois, jamais tout `tests/`. Commande notée `$PYTEST` :
  `cd /srv/dev/worktrees/plugboard/mcp-identity && TMPDIR=/tmp/claude-1001/-srv-dev-plugboard/a5d91e11-1edb-4748-99e2-79712c823769/scratchpad/mcp-identity PLUGBOARD_SKIP_DOTENV=1 PYTHONDONTWRITEBYTECODE=1 /srv/dev/plugboard/.venv/bin/python -m pytest <fichiers> -q -p no:cacheprovider`
- Contrat MCP régénéré avec le venv principal (pas de `.venv-mcp`), dans toute tâche qui change une signature ou une docstring d'outil, sinon `tests/test_contract_without_fastapi.py::test_mcp_contract_check_runs_without_fastapi` échoue. Commande notée `$CONTRACT` :
  `cd /srv/dev/worktrees/plugboard/mcp-identity && S=/tmp/claude-1001/-srv-dev-plugboard/a5d91e11-1edb-4748-99e2-79712c823769/scratchpad/mcp-identity && TMPDIR=$S PLUGBOARD_DATA=$S/data PLUGBOARD_SKIP_DOTENV=1 PYTHONDONTWRITEBYTECODE=1 /srv/dev/plugboard/.venv/bin/python scripts/export_contract.py --mcp`
- `CONTRACT_VERSION` reste `1.5.0`. Ne pas toucher `VERSION` ni `plugboard/__init__.py`.
- Messages renvoyés par les outils : en anglais, préfixe `Error: `. Textes exacts :
  - pas de clé : `the update registry needs PLUGBOARD_API_KEY: updates are those of that key's client`
  - passerelle en erreur : `could not identify the client of PLUGBOARD_API_KEY (<result["error"][:120]>)`
  - pas de client : `PLUGBOARD_API_KEY does not identify a client`
  - clé admin : `PLUGBOARD_API_KEY is the admin key: give the MCP server a client key`
  - argument différent : `client_id '<claimed>' is not the client of this key ('<client>'): leave client_id out`
  - mise à jour d'un autre client : le message existant `prompt '<id>' not found in registry`, identique au cas inexistant.
- Clés, clients et jetons de test synthétiques uniquement (`pb_synthetic_alpha`, `alpha`, `beta`). Aucun appel réseau hors `127.0.0.1`, aucune inférence, jamais l'appli UAT `127.0.0.1:18765`.
- Doc en anglais ; CHANGELOG sous `## [Unreleased]`. Conventional Commits, un commit par tâche, avec les lignes d'attribution de la session.
- Lint : `/srv/dev/plugboard/.venv/bin/ruff check plugboard/ tests/` et `ruff format --check plugboard/ tests/`.

## Review Focus

1. Passerelle injoignable pendant `mark_applied` : erreur, aucune ligne écrite. Test dans la tâche 3.
2. Clé révoquée entre deux appels : l'appel suivant est refusé (pas de cache). Test dans la tâche 1.
3. Identité refusée alors que le registre manque : le refus sort avant le message « registry is missing », qui donne le chemin du fichier sur le serveur. Test dans la tâche 2.
4. Ancien agent qui appelle `mark_applied` par nom avec son propre `client_id` : l'appel passe. Test dans la tâche 3.
5. `client_id` entouré d'espaces (`" alpha "`) accepté ; casse différente (`"ALPHA"`) refusée, car la passerelle compare exactement. Test dans la tâche 1.

---

### Task 1: Résoudre le client de la clé

**Files:**
- Modify: `plugboard/mcp_server.py` — nouveau bloc après `_embedding_request` (~ligne 311)
- Create: `tests/test_mcp_client_identity.py`

**Interfaces:**
- Consumes: `_api_get(path: str, admin: bool = False) -> dict[str, Any]` (existant, envoie `PLUGBOARD_API_KEY`), `_API_KEY`.
- Produces:
  - `class CallerIdentityError(Exception)` — `str(exc)` est le message à montrer, sans le préfixe `Error: `.
  - `async def _caller_client_id() -> str` — lève `CallerIdentityError` (pas de clé : aucun appel HTTP ; erreur de `_api_get` ; `client_id` absent, vide ou non `str` ; `"admin"`).
  - `async def _registry_client(claimed: str = "") -> str` — `claimed.strip()` vide ou égal au client : renvoie le client ; sinon lève `CallerIdentityError`.
  - Fixture `as_client(monkeypatch)` dans le fichier de test : remplace `_api_get` par un faux qui note `(path, admin)` dans `state["calls"]` et répond à `/v1/me` par `{"error": state["error"]}` si `state["error"]` est posé, sinon `{"client_id": state["client_id"]}` ; pose `_API_KEY = "pb_synthetic_alpha"` ; `state = {"client_id": "alpha", "error": None, "calls": []}`.

- [ ] **Step 1: Écrire les tests qui échouent**

```python
async def test_identity_comes_from_v1_me_with_the_client_key(as_client):
    assert await mcp_server._caller_client_id() == "alpha"
    assert as_client["calls"] == [("/v1/me", False)]

async def test_no_key_is_refused_without_calling_the_gateway(as_client, monkeypatch):
    monkeypatch.setattr(mcp_server, "_API_KEY", "")
    with pytest.raises(mcp_server.CallerIdentityError, match="needs PLUGBOARD_API_KEY"):
        await mcp_server._caller_client_id()
    assert as_client["calls"] == []

async def test_gateway_error_is_refused(as_client):
    as_client["error"] = "HTTP 401: Invalid API key"
    with pytest.raises(mcp_server.CallerIdentityError, match="HTTP 401"):
        await mcp_server._caller_client_id()

@pytest.mark.parametrize("value", [None, "", 42])
async def test_key_without_client_is_refused(as_client, value):
    as_client["client_id"] = value
    with pytest.raises(mcp_server.CallerIdentityError, match="does not identify a client"):
        await mcp_server._caller_client_id()

async def test_admin_identity_is_refused(as_client):
    as_client["client_id"] = "admin"
    with pytest.raises(mcp_server.CallerIdentityError, match="is the admin key"):
        await mcp_server._caller_client_id()

async def test_revoked_key_is_refused_on_the_next_call(as_client):
    assert await mcp_server._caller_client_id() == "alpha"
    as_client["error"] = "HTTP 401: Invalid API key"
    with pytest.raises(mcp_server.CallerIdentityError):
        await mcp_server._caller_client_id()
    assert as_client["calls"] == [("/v1/me", False), ("/v1/me", False)]

@pytest.mark.parametrize("claimed", ["", "   ", "alpha", " alpha "])
async def test_own_or_absent_client_id_is_accepted(as_client, claimed):
    assert await mcp_server._registry_client(claimed) == "alpha"

@pytest.mark.parametrize("claimed", ["beta", "ALPHA"])
async def test_another_client_id_is_refused(as_client, claimed):
    with pytest.raises(mcp_server.CallerIdentityError, match="leave client_id out"):
        await mcp_server._registry_client(claimed)
```

- [ ] **Step 2: Lancer, constater l'échec** — `$PYTEST tests/test_mcp_client_identity.py` → FAIL (`AttributeError: ... has no attribute '_caller_client_id'`).
- [ ] **Step 3: Implémenter** `CallerIdentityError`, `_caller_client_id()` et `_registry_client()` dans `plugboard/mcp_server.py` avec les messages exacts des Global Constraints. Pas de cache.
- [ ] **Step 4: Relancer** — `$PYTEST tests/test_mcp_client_identity.py` → PASS.
- [ ] **Step 5: Commit** — `git add plugboard/mcp_server.py tests/test_mcp_client_identity.py && git commit -m "feat(mcp): resolve the calling key's client through /v1/me"`

### Task 2: Outils de lecture du registre liés à la clé

**Files:**
- Modify: `plugboard/mcp_server.py:912-970` (`check_updates`), `:973-1032` (`get_update_prompt`), `:1089-1115` (`list_applied`), `:1118-1142` (`resource_pending_updates`)
- Modify: `docs/api/mcp.json` (régénéré par `$CONTRACT`)
- Test: `tests/test_mcp_client_identity.py`

**Interfaces:**
- Consumes: `_registry_client(claimed: str = "") -> str`, `_caller_client_id() -> str`, `CallerIdentityError` (tâche 1).
- Produces:
  - `async def check_updates(client_id: str = "") -> str`
  - `async def list_applied(client_id: str = "") -> str`
  - `async def get_update_prompt(prompt_id: str) -> str`
  - `async def resource_pending_updates() -> str` — description `Pending updates for the client of this server's key.`
  - Fixture `registry(tmp_path, monkeypatch)` : écrit `registry.json` avec trois mises à jour complètes (`id`, `version`, `date`, `impact`, `clients`, `title`, `summary`, `changes`) — `CP-A` (`["alpha"]`), `CP-B` (`["beta"]`, titre `Beta secret step`), `CP-AB` (`["alpha", "beta"]`) — et pose `_REGISTRY_PATH`, `_DATA_DIR = tmp_path`, `_TRACKING_DB = tmp_path / "client_updates.db"`, `_TRACKING_SCHEMA_READY = False`. Helpers `_seed(client_id, prompt_id)` (insère via `_tracking_conn()`) et `_rows() -> set[tuple[str, str]]`.

Chaque outil appelle d'abord `_registry_client(client_id)` (ou `_caller_client_id()` pour `get_update_prompt` et la ressource) ; sur `CallerIdentityError` il renvoie `f"Error: {exc}"` sans charger le registre. `get_update_prompt` renvoie `not found` si l'identité n'est pas dans `prompt["clients"]`, remplace la ligne `**Clients:**` par `**Client:** <client>` et finit par ``After applying, call: `mark_applied("<id>")` ``. Docstrings : retirer « Your project identifier », dire que le client est celui de `PLUGBOARD_API_KEY` et que `client_id` est facultatif et doit être ce client.

- [ ] **Step 1: Écrire les tests qui échouent**

```python
async def test_check_updates_lists_only_the_key_client(as_client, registry):
    out = await mcp_server.check_updates()
    assert "`CP-A`" in out and "`CP-AB`" in out and "`CP-B`" not in out

async def test_check_updates_refuses_another_client_id(as_client, registry):
    out = await mcp_server.check_updates("beta")
    assert out.startswith("Error:") and "`CP-B`" not in out and "Beta secret step" not in out

async def test_check_updates_accepts_its_own_client_id(as_client, registry):
    assert await mcp_server.check_updates("alpha") == await mcp_server.check_updates()

async def test_list_applied_shows_only_the_key_client(as_client, registry):
    _seed("alpha", "CP-A"); _seed("beta", "CP-B")
    assert "CP-A" in (await mcp_server.list_applied()) and "CP-B" not in (await mcp_server.list_applied())
    refused = await mcp_server.list_applied("beta")
    assert refused.startswith("Error:") and "CP-B" not in refused

async def test_get_update_prompt_hides_other_clients_updates(as_client, registry):
    other = await mcp_server.get_update_prompt("CP-B")
    missing = await mcp_server.get_update_prompt("CP-NONE")
    assert other == missing.replace("CP-NONE", "CP-B")
    assert "Beta secret step" not in other

async def test_get_update_prompt_names_only_the_caller(as_client, registry):
    out = await mcp_server.get_update_prompt("CP-AB")
    assert "**Client:** alpha" in out and "beta" not in out
    assert 'mark_applied("CP-AB")' in out

async def test_pending_resource_shows_only_the_key_client(as_client, registry):
    out = await mcp_server.resource_pending_updates()
    assert "`CP-A`" in out and "beta" not in out and "`CP-B`" not in out

@pytest.mark.parametrize("call", [
    lambda: mcp_server.check_updates(), lambda: mcp_server.list_applied(),
    lambda: mcp_server.get_update_prompt("CP-A"), lambda: mcp_server.resource_pending_updates(),
])
async def test_refused_identity_reads_nothing(as_client, registry, monkeypatch, call):
    as_client["client_id"] = "admin"
    monkeypatch.setattr(mcp_server, "_load_registry", lambda: pytest.fail("registry read"))
    monkeypatch.setattr(mcp_server, "_tracking_conn", lambda: pytest.fail("database read"))
    assert (await call()).startswith("Error:")
```

- [ ] **Step 2: Lancer, constater l'échec** — `$PYTEST tests/test_mcp_client_identity.py` → FAIL (`check_updates() missing 1 required positional argument` et sorties qui contiennent `CP-B`/`beta`).
- [ ] **Step 3: Implémenter** les quatre changements décrits ci-dessus.
- [ ] **Step 4: Régénérer le contrat** — `$CONTRACT` → `Wrote docs/api/mcp.json` ; `git diff docs/api/mcp.json` ne montre que les docstrings, la description de la ressource et `required` sans `client_id`.
- [ ] **Step 5: Relancer** — `$PYTEST tests/test_mcp_client_identity.py tests/test_contract_without_fastapi.py tests/test_registry_path.py` → PASS.
- [ ] **Step 6: Commit** — `git commit -m "fix(mcp): registry reads act for the calling key's client only"` (fichiers : `plugboard/mcp_server.py`, `tests/test_mcp_client_identity.py`, `docs/api/mcp.json`).

### Task 3: `mark_applied` lié à la clé

**Files:**
- Modify: `plugboard/mcp_server.py:1035-1086`, `docs/api/mcp.json` (`$CONTRACT`)
- Test: `tests/test_mcp_client_identity.py`

**Interfaces:**
- Consumes: `_registry_client`, `CallerIdentityError` (tâche 1) ; fixtures `as_client`, `registry`, `_rows()` (tâches 1-2).
- Produces: `async def mark_applied(prompt_id: str, notes: str = "", client_id: str = "") -> str` (ordre changé : les appels MCP passent par nom). Mise à jour qui ne vise pas le client : `Error: prompt '<id>' not found in registry`. Fin du message « still pending » : ``Use `check_updates()` to see them.`` Helper de test `_call(name, args) -> str` : `await mcp_server.mcp.call_tool(name, args)`, texte du premier bloc de contenu (gérer la forme tuple `(content, structured)`).

- [ ] **Step 1: Écrire les tests qui échouent**

```python
async def test_mark_applied_records_for_the_key_client(as_client, registry):
    await mcp_server.mark_applied("CP-AB")
    assert _rows() == {("alpha", "CP-AB")}

async def test_mark_applied_refuses_another_client_id(as_client, registry):
    assert (await mcp_server.mark_applied("CP-AB", client_id="beta")).startswith("Error:")
    assert _rows() == set()

async def test_mark_applied_on_another_clients_update_is_not_found(as_client, registry):
    assert await mcp_server.mark_applied("CP-B") == "Error: prompt 'CP-B' not found in registry"
    assert _rows() == set()

async def test_mark_applied_writes_nothing_when_the_gateway_is_down(as_client, registry):
    as_client["error"] = "HTTP client error: Cannot connect to host 127.0.0.1:9"
    assert (await mcp_server.mark_applied("CP-A")).startswith("Error:")
    assert _rows() == set()

async def test_old_by_name_call_with_own_client_id_still_works(as_client, registry):
    out = await _call("mark_applied", {"client_id": "alpha", "prompt_id": "CP-A", "notes": "n"})
    assert "Marked `CP-A`" in out and _rows() == {("alpha", "CP-A")}
    refused = await _call("mark_applied", {"client_id": "beta", "prompt_id": "CP-AB"})
    assert refused.startswith("Error:") and _rows() == {("alpha", "CP-A")}
```

- [ ] **Step 2: Lancer, constater l'échec** — `$PYTEST tests/test_mcp_client_identity.py` → FAIL.
- [ ] **Step 3: Implémenter** la nouvelle signature ; `_registry_client(client_id)` en premier, avant `_load_registry()`.
- [ ] **Step 4: Régénérer le contrat** — `$CONTRACT` ; `required` de `mark_applied` vaut `["prompt_id"]`.
- [ ] **Step 5: Relancer** — `$PYTEST tests/test_mcp_client_identity.py tests/test_contract_without_fastapi.py tests/test_data_permissions.py` → PASS.
- [ ] **Step 6: Commit** — `git commit -m "fix(mcp): mark_applied writes for the calling key's client only"`

### Task 4: `inflight_requests` refuse la clé admin (Q4, option 1)

**Files:**
- Modify: `plugboard/mcp_server.py:685-711`, `docs/api/mcp.json` (`$CONTRACT`)
- Modify: `tests/test_live_requests.py:259-294` (le faux `_api_get` répond aussi à `/v1/me`)
- Test: `tests/test_mcp_client_identity.py`

**Interfaces:**
- Consumes: `_caller_client_id()`, `CallerIdentityError` (tâche 1).
- Produces: `inflight_requests()` appelle `_caller_client_id()` avant `/v1/inflight` ; docstring : « Only the calls of this key's client are listed. Refused when PLUGBOARD_API_KEY is the admin key. »

- [ ] **Step 1: Écrire le test qui échoue, adapter l'existant**

```python
async def test_inflight_refuses_the_admin_identity(as_client):
    as_client["client_id"] = "admin"
    assert (await mcp_server.inflight_requests()).startswith("Error:")
    assert as_client["calls"] == [("/v1/me", False)]
```

Dans `tests/test_live_requests.py::test_mcp_tool_shows_running_and_finished_calls`, le faux renvoie `{"client_id": "c1"}` pour `/v1/me`, et l'assertion devient `seen == [("/v1/me", False), ("/v1/inflight", False)]`.

- [ ] **Step 2: Lancer, constater l'échec** — `$PYTEST tests/test_mcp_client_identity.py tests/test_live_requests.py` → FAIL.
- [ ] **Step 3: Implémenter** ; `$CONTRACT`.
- [ ] **Step 4: Relancer** — `$PYTEST tests/test_mcp_client_identity.py tests/test_live_requests.py tests/test_contract_without_fastapi.py` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "fix(mcp): inflight_requests refuses an admin key in PLUGBOARD_API_KEY"`

### Task 5: Preuve sur les trois transports

**Files:**
- Test: `tests/test_mcp_client_identity.py` (fin de fichier)

**Interfaces:**
- Consumes: le comportement des tâches 2-3 ; motifs existants : faux serveur aiohttp (`tests/test_mcp_decisions.py:25-56`), import neuf + `configure_network_transport(TOKEN)` (`tests/test_spec_v7_residuals_parent.py:238-245`), JSON-RPC sur `streamable_http_app()` (`:264-297`).
- Produces (dans le fichier de test) :
  - fixture `fake_gateway` : aiohttp sur `127.0.0.1:0`, `GET /v1/me` → `{"client_id": "alpha"}` pour `Bearer pb_synthetic_alpha`, sinon `401 {"detail": "Invalid API key"}` ; renvoie l'URL de base.
  - fixture `net_data(tmp_path)` : `registry.json` (mêmes trois mises à jour) et `client_updates.db` contenant la seule ligne `("beta", "CP-B")`.
  - `async def _scenario(call: Callable[[str, dict], Awaitable[str]], db: Path) -> None` : en tant qu'`alpha`, `check_updates {"client_id": "beta"}` refusé sans `` `CP-B` `` ; `check_updates {}` montre `` `CP-A` `` sans `` `CP-B` `` ; `mark_applied {"prompt_id": "CP-B"}` → `not found` ; `mark_applied {"client_id": "beta", "prompt_id": "CP-AB"}` refusé ; `list_applied {"client_id": "beta"}` refusé sans `CP-B` ; à la fin, les lignes de `db` valent `{("beta", "CP-B")}`.

- [ ] **Step 1: Écrire les trois tests**
  - `test_stdio_caller_cannot_reach_another_client` : `mcp.client.stdio.stdio_client(StdioServerParameters(command=sys.executable, args=[str(ROOT / "scripts/run_mcp.py")], env={PATH, HOME=tmp, TMPDIR, PLUGBOARD_URL=fake_gateway, PLUGBOARD_API_KEY="pb_synthetic_alpha", PLUGBOARD_DATA=<net_data>, PLUGBOARD_REGISTRY_PATH=<registry.json>, PLUGBOARD_SKIP_DOTENV="1", PYTHONDONTWRITEBYTECODE="1"}))` + `ClientSession.initialize()` ; `call` = texte de `session.call_tool(name, args).content[0]`.
  - `test_http_caller_cannot_reach_another_client` : import neuf, `configure_network_transport(TOKEN)`, attributs `_BASE_URL`, `_API_KEY`, `_HTTP_SESSION=None`, `_REGISTRY_PATH`, `_DATA_DIR`, `_TRACKING_DB`, `_TRACKING_SCHEMA_READY=False` posés par monkeypatch ; dans `mcp.session_manager.run()`, httpx `ASGITransport` : `initialize` (bearer `TOKEN`, en-tête `mcp-session-id` relu), `notifications/initialized`, puis `tools/call` ; `call` lit la ligne `data:` de la réponse.
  - `test_sse_caller_cannot_reach_another_client` : même préparation ; `uvicorn.Server(uvicorn.Config(ms.mcp.sse_app(), host="127.0.0.1", port=0, log_level="warning"))` lancé en tâche, port lu dans `server.servers[0].sockets[0]` ; `mcp.client.sse.sse_client(f"http://127.0.0.1:{port}/sse", headers={"Authorization": f"Bearer {TOKEN}"})` + `ClientSession` ; `server.should_exit = True` dans un `finally`.
- [ ] **Step 2: Contrôle de non-vacuité** — modifier temporairement `_registry_client` dans `plugboard/mcp_server.py` pour qu'il renvoie `claimed.strip() or client` (l'ancien comportement ; un monkeypatch n'atteindrait pas le sous-processus stdio), lancer `$PYTEST tests/test_mcp_client_identity.py -k caller_cannot_reach` → les trois tests **échouent** ; puis `git checkout -- plugboard/mcp_server.py` (le fichier n'a pas d'autre changement non commité à ce stade).
- [ ] **Step 3: Lancer** — `$PYTEST tests/test_mcp_client_identity.py` → PASS. Chaque test arrête lui-même son serveur uvicorn, son sous-processus et le faux serveur de passerelle (`finally`).
- [ ] **Step 4: Commit** — `git commit -m "test(mcp): a caller cannot reach another client over stdio, SSE or HTTP"`

### Task 6: Documentation

**Files:**
- Modify: `docs/guide/mcp.md:83-86` (tableau), `:113` (registre), `:145-150` (fin du registre), section « Network transports » (`:41-48`)
- Modify: `CHANGELOG.md` — nouvelle section `### Security` sous `## [Unreleased]`, avant `### Added`
- Modify: `TODOS.md:111-122` — retirer l'entrée « MCP client identity from the key »

**Interfaces:** aucune.

- [ ] **Step 1: Guide** — tableau : `check_updates` et `list_applied` → arguments `client_id` (optional) ; `mark_applied` → `prompt_id`, `notes`, `client_id` (optional) ; `inflight_requests` : ajouter « refused with the admin key ». Remplacer le dernier paragraphe du registre par :
  > The registry tools act for the client of `PLUGBOARD_API_KEY`, which the server asks the gateway for (`GET /v1/me`) at each call: a caller only sees, reads and marks its own client's updates, and `updates://pending` lists only those. `client_id` is optional; a value that names another client is refused. Without a client key (no key, the admin key, or a key the gateway refuses) the registry tools answer an error. `clients` in the registry must hold the client IDs of your keys.
  
  Ajouter à « Network transports » : `- Every caller holding the token acts as the client of PLUGBOARD_API_KEY: run one MCP server, with its own key and token, per client.` Dans l'introduction du registre : `calls check_updates` sans argument.
- [ ] **Step 2: CHANGELOG** :
  > ### Security
  >
  > - **MCP update registry tools act for the calling key's client only**: `check_updates`, `list_applied`, `mark_applied`, `get_update_prompt` and the `updates://pending` resource took the client from a `client_id` argument, so any caller could read another client's pending updates and their instructions, or mark them applied in its place. The client now comes from `PLUGBOARD_API_KEY`, through `GET /v1/me` at each call, over stdio, SSE and HTTP. `client_id` becomes optional and is refused when it names another client; an update for another client answers *not found*, and no other client's name is shown. The registry tools, and `inflight_requests`, refuse an MCP server started without a client key or with the admin key. **Upgrade:** `clients` in `registry.json` must hold the client IDs of your keys; an update recorded as applied under another name shows as pending again ([guide](docs/guide/mcp.md#update-registry)).
- [ ] **Step 3: Vérifier** — `grep -n "not tied to the caller" docs/guide/mcp.md` → aucune ligne ; `grep -n "MCP client identity" TODOS.md` → aucune ligne.
- [ ] **Step 4: Commit** — `git commit -m "docs(mcp): registry tools act for the key's client"`

### Task 7: Vérification finale

- [ ] **Step 1: Tests ciblés** — `$PYTEST tests/test_mcp_client_identity.py tests/test_live_requests.py tests/test_mcp_decisions.py tests/test_mcp_embeddings.py tests/test_openclaw_removal_mcp.py tests/test_registry_path.py tests/test_run_mcp_legacy_names.py tests/test_spec_v7_residuals_parent.py tests/test_contract_without_fastapi.py tests/test_data_permissions.py` → tout PASS, sortie collée dans le rapport.
- [ ] **Step 2: Contrat** — `$CONTRACT` suivi de `git status --porcelain docs/api/` → vide (déjà à jour).
- [ ] **Step 3: Lint et secrets** — `ruff check plugboard/ tests/`, `ruff format --check plugboard/ tests/`, `make secret-scan` → propres.
- [ ] **Step 4: Nettoyage** — supprimer le dossier scratch `/tmp/claude-1001/-srv-dev-plugboard/a5d91e11-1edb-4748-99e2-79712c823769/scratchpad/mcp-identity` (sans `rm -rf` : `find <dossier> -delete`), puis `git status --porcelain` → vide.

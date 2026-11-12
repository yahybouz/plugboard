# Interrupteur Ollama (PLU-12) — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Couper ou rallumer Ollama à chaud pour l'API, le MCP et le Playground : coupé, aucun modèle `ollama:` listé et aucun appel vers Ollama ni vers un autre fournisseur à sa place ; rallumé, tout revient sans redémarrage.

**Architecture:** L'interrupteur est le champ `is_active` du fournisseur `ollama` (déjà respecté par les listes, les embeddings et les sondes). Le routeur gagne `disabled_route()`, qui reconnaît un modèle appartenant seulement à un fournisseur désactivé ; le chat, le préflight, `forward()` et `forward_stream()` le refusent en 503 `provider_disabled` sans rien appeler. La page Ollama gagne un interrupteur (`GET`/`PUT /admin/ollama/switch`), audité.

**Tech Stack:** Python 3.12, FastAPI, aiohttp, pytest (`asyncio_mode = auto`), aioresponses, httpx `ASGITransport`, JS sans framework (`static/app.js`).

**Spec:** `docs/superpowers/specs/2026-11-12-ollama-switch-design.md` — ce plan suit les **réponses recommandées** (1) aux questions 1 à 8. Si YB en choisit une autre, voir « Variantes » en fin de plan.

## Global Constraints

- Interrupteur = `is_active` du fournisseur d'id `ollama` (`plugboard.ollama._PROVIDER_ID`), écrit par `config.update_provider` (donc `atomic_write_json`). Aucun nouveau fichier dans `data/`.
- Refus : HTTP **503**, corps exact `{"detail": "Provider '<id>' is turned off by the administrator; nothing was called.", "code": "provider_disabled", "hint": "Pick another model from GET /v1/models, or ask the administrator to turn the provider back on."}`, sans `Retry-After`. Code préflight : `provider_disabled`.
- Ordre d'admission du chat : 403 de la clé (calculé sur l'identité du vrai propriétaire) → 503 `provider_disabled` → 404 `model_not_found` → 402 → cache → appel.
- Audit : action `ollama.switch`, `target_type="provider"`, `target_id="ollama"`, `details=json.dumps({"from": bool, "to": bool, "in_flight": int, "waiting": int})`.
- Contrat d'API : `CONTRACT_VERSION` `1.5.0` → `1.6.0`. Ne pas toucher `VERSION` ni `plugboard/__init__.py`. Aucun changement dans `plugboard/mcp_server.py` (PB-MCP-01 y travaille).
- Textes de l'interface : « Available to the API, MCP and Playground » ; bandeau « Ollama is off: the API, the MCP server and the Playground do not use it. ».
- Code, messages, interface, docs en anglais ; aucune donnée personnelle, aucune vraie clé, aucun nom de client.
- **Aucun appel réel à Ollama** : dans les tests et l'instance locale, `base_url` et `OLLAMA_URL` = `http://127.0.0.1:9` (port fermé), jamais 11434 ni 11435 ; `plugboard.ollama.check_available` et `plugboard.ollama.list_models` toujours remplacés par des faux.
- Tests ciblés seulement, un pytest à la fois, jamais tout `tests/`. Depuis la racine de l'atelier :
  `PYTEST="env TMPDIR=/tmp/claude-1001/-srv-dev-plugboard/a5d91e11-1edb-4748-99e2-79712c823769/scratchpad/ollama-switch PLUGBOARD_SKIP_DOTENV=1 PYTHONDONTWRITEBYTECODE=1 /srv/dev/plugboard/.venv/bin/python -m pytest -q -p no:cacheprovider"`
- Tout test qui touche un état global (`PROVIDERS_FILE`, caches du routeur, clés, audit, usage, `provider_admission`) l'isole par `monkeypatch` (helper `_isolated_providers` de `tests/test_model_not_found.py`, `audit._DB_PATH`, `usage._DB_PATH`).
- Conventional Commits, portée `ollama` (ou `router`, `api`, `dashboard`), un commit par tâche, lignes d'attribution de la session en cours. Pas de `git push`, pas de `ci`, pas de `board`.

## Review Focus

- Un fournisseur actif **sans liste de modèles** peut servir n'importe quel nom : un nom nu d'Ollama (`qwen3:8b`) continue d'y être routé, seul le préfixe explicite `ollama:` est refusé → `test_bare_name_goes_on_when_an_active_provider_has_no_list` (Task 1).
- Une clé limitée autorisée à `ollama:qwen3:8b` doit recevoir 503, pas un 403 qui nomme un autre fournisseur ; une clé non autorisée garde son 403 → `test_allowed_key_gets_503_not_403`, `test_limited_key_gets_403_first` (Task 2).
- Une réponse en cache d'un modèle Ollama ne doit pas sortir quand c'est coupé → `test_cache_is_not_read_when_disabled` (Task 2).
- Une requête en file d'attente du créneau Ollama ne doit pas appeler Ollama après la coupure, et son créneau doit être rendu → `test_waiter_is_refused_after_the_switch` (Task 3).
- Préfixes imbriqués : `ollama` coupé ne doit pas toucher `ollama-gpu` actif → `test_longer_active_prefix_wins` (Task 1).

---

### Task 1: `router.disabled_route` et `ProviderDisabledError`

**Files:**
- Modify: `plugboard/router.py` (classe à côté de `ProvidersBusyError` `:127` ; fonction après `is_unknown_model` `:516-557`)
- Test: `tests/test_ollama_switch.py` (nouveau)

**Interfaces — Produces:**
- `class ProviderDisabledError(RuntimeError)` avec `provider_id: str` (`__init__(self, provider_id: str)`, message `Provider '<id>' is turned off`).
- `disabled_route(model_str: str) -> Optional[tuple[Dict[str, Any], str]]` : `(fournisseur inactif, nom natif)` quand le modèle n'appartient qu'à un fournisseur désactivé, sinon `None`. Règles, dans l'ordre :
  1. non-chaîne ou vide → `None` ; fournisseurs retirés (`is_removed_provider`) ignorés ;
  2. `registered_model_owner(model_str, tous les ids)` trouve un propriétaire : `None` s'il est actif, sinon `(lui, model_str[len(id)+1:])` ;
  3. sans préfixe : `None` si un fournisseur actif n'a pas de liste (`_listed_models` vide), ou si un actif le connaît (forme `_loose_model_name` sur `_listed_models` + `default_model` + `_family_aliases`, ou recherche `_clean_model_id` comme `is_unknown_model`) ; sinon le premier fournisseur **inactif** qui le connaît sous forme lâche, avec **son** nom natif listé ; sinon `None`.
  `is_unknown_model` reste inchangé.

- [ ] **Step 1: Écrire les tests qui échouent**

Fournisseurs du fichier (réutilisés par les tâches suivantes) : `OLLAMA = {"id": "ollama", "name": "Ollama", "base_url": "http://127.0.0.1:9", "bearer_token": "", "default_model": "", "provider_type": "sovereign", "models": [{"id": "qwen3:8b"}, {"id": "llama3:latest"}], "is_active": False}`, `ACME` et `OPEN` comme dans `tests/test_model_not_found.py`, `BETA = {**ACME, "id": "beta-cloud", "name": "Beta Cloud", "base_url": "http://beta.test", "models": [{"id": "beta-1"}], "extra_model_ids": [], "default_model": "beta-1"}`. Fixture `off` = `_isolated_providers(tmp_path, monkeypatch, [OLLAMA, ACME, BETA])` ; autouse : `check_available` et `list_models` de `plugboard.ollama` remplacés par des faux qui lèvent `AssertionError("no real Ollama call")`.

```python
@pytest.mark.parametrize(("model", "native"), [
    ("ollama:qwen3:8b", "qwen3:8b"), ("qwen3:8b", "qwen3:8b"),
    ("llama3", "llama3:latest"), (" LLAMA3:latest ", "llama3:latest"),
    ("ollama:not-listed", "not-listed"),
])
def test_a_model_of_a_disabled_provider_is_recognised(off, model, native):
    provider, got = router.disabled_route(model)
    assert (provider["id"], got) == ("ollama", native)

@pytest.mark.parametrize("model", ["acme-large", "acme-direct:x", "nope:model-x", "made-up", ""])
def test_other_models_are_not_disabled(off, model):
    assert router.disabled_route(model) is None

def test_nothing_is_disabled_when_ollama_is_on(tmp_path, monkeypatch):
    # OLLAMA with is_active True: "ollama:qwen3:8b" and "qwen3:8b" -> None

def test_bare_name_goes_on_when_an_active_provider_has_no_list(tmp_path, monkeypatch):
    # [OLLAMA, ACME, OPEN]: "qwen3:8b" -> None ; "ollama:qwen3:8b" -> ("ollama", "qwen3:8b")

def test_longer_active_prefix_wins(tmp_path, monkeypatch):
    # [OLLAMA, {**OLLAMA, "id": "ollama-gpu", "name": "Ollama GPU", "is_active": True}, ACME]
    # "ollama-gpu:qwen3:8b" -> None ; "qwen3:8b" -> None (an active provider lists it)

def test_only_provider_disabled(tmp_path, monkeypatch):
    # [OLLAMA] seul : "ollama:qwen3:8b" et "qwen3:8b" -> ("ollama", "qwen3:8b")

def test_unknown_model_rule_is_unchanged(off):
    assert router.is_unknown_model("ollama:qwen3:8b") is False
    assert router.is_unknown_model("nope:model-x") is True
```

- [ ] **Step 2: Lancer, voir l'échec** — `$PYTEST tests/test_ollama_switch.py` → `AttributeError: ... has no attribute 'disabled_route'`.
- [ ] **Step 3: Implémenter `ProviderDisabledError` et `disabled_route` dans `plugboard/router.py`** selon les règles ci-dessus ; lecture du registre par `load_providers()` du module `router` (que les tests remplacent).
- [ ] **Step 4: Lancer** — `$PYTEST tests/test_ollama_switch.py tests/test_model_not_found.py` → tout passe.
- [ ] **Step 5: Commit** — `fix(router): recognise a model that only a disabled provider serves (PLU-12)`.

### Task 2: chat — 503 `provider_disabled`, rien appelé, pas de cache

**Files:**
- Modify: `plugboard/app.py` (`_check_model_access` `:1644-1652` ; nouvelle `_provider_disabled_response` à côté de `_providers_busy_response` `:1807` ; `chat_completions` `:2055-2063`)
- Test: `tests/test_ollama_switch.py`

**Interfaces:**
- Consumes: `router.disabled_route`, `router.ProviderDisabledError` (Task 1).
- Produces: `_route_for_guard(model_str: str) -> tuple[Dict[str, Any], str]` = `disabled_route(model_str) or resolve_provider_and_model(model_str)` ; `_provider_disabled_response(provider_id: str) -> JSONResponse` (corps exact des Global Constraints). `_check_model_access` et la garde de clé du chat utilisent `_route_for_guard`.

- [ ] **Step 1: Écrire les tests qui échouent** — fixture `http` copiée de `tests/test_model_not_found.py:216-247` (clé client réelle, `forward`/`forward_stream` remplacés par des enregistreurs) sur `off`.

```python
@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
@pytest.mark.parametrize("model", ["ollama:qwen3:8b", "qwen3:8b"])
@pytest.mark.parametrize("stream", [False, True])
async def test_chat_gets_503_and_calls_nobody(off, http, path, model, stream):
    r = await client.post(path, headers=auth, json=_chat(model, stream=stream))
    assert r.status_code == 503
    assert r.json() == {"detail": "Provider 'ollama' is turned off by the administrator; nothing was called.",
                        "code": "provider_disabled",
                        "hint": "Pick another model from GET /v1/models, or ask the administrator to turn the provider back on."}
    assert "retry-after" not in r.headers
    assert calls == []

async def test_playground_gets_the_same_503(off, http):
    # admin key + header X-Plugboard-Surface: playground -> 503, calls == []

async def test_limited_key_gets_403_first(off, http):
    # key allowed_models ["acme-direct:acme-large"] -> "ollama:qwen3:8b" gives 403 naming 'ollama:qwen3:8b'

async def test_allowed_key_gets_503_not_403(off, http):
    # key allowed_models ["ollama:qwen3:8b"] -> 503 provider_disabled

async def test_budgeted_key_gets_503_not_402(off, http):
    # key with monthly budget, model without price -> 503

async def test_cache_is_not_read_when_disabled(off, http, monkeypatch):
    # plugboard.cache.get_cached replaced by a recorder -> never called

async def test_chat_is_routed_again_once_turned_on(off, http):
    # rewrite providers.json with OLLAMA is_active True + router.invalidate_models_cache()
    # -> calls == ["ollama:qwen3:8b"]
```

- [ ] **Step 2: Lancer, voir l'échec** — `$PYTEST tests/test_ollama_switch.py -k "chat or playground or key or cache"` → statuts 502/403 au lieu de 503.
- [ ] **Step 3: Implémenter** `_route_for_guard`, `_provider_disabled_response` ; dans `chat_completions`, la garde de clé appelle `route_guard(*_route_for_guard(model))`, puis `if (route := disabled_route(model)): return _provider_disabled_response(route[0]["id"])`, avant `is_unknown_model`.
- [ ] **Step 4: Lancer** — `$PYTEST tests/test_ollama_switch.py tests/test_model_not_found.py tests/test_allowed_models_published_id.py` → tout passe.
- [ ] **Step 5: Commit** — `fix(api): answer 503 provider_disabled for a model of a disabled provider (PLU-12)`.

### Task 3: routeur — refus au cœur et file d'attente

**Files:**
- Modify: `plugboard/router.py` (`forward` `:733-830`, `forward_stream` `:1123-1159`) ; `plugboard/app.py` (les trois `isinstance(exc, ProvidersBusyError)` `:2249`, `:2436`, `:2580` gagnent `ProviderDisabledError` → `_provider_disabled_response(exc.provider_id)`)
- Test: `tests/test_ollama_switch_router.py` (nouveau ; importe `capped_a`, `_body`, `_until`, `_calls`, `URL_A`, `ONLY_A` depuis `tests/test_router_admission.py`)

**Interfaces:**
- Consumes: Task 1, Task 2 (`_provider_disabled_response`).
- Produces: `forward` et `forward_stream` lèvent `ProviderDisabledError` (a) au départ si `disabled_route(model)` ; (b) après une attente, si le fournisseur du créneau accordé est devenu inactif : créneau rendu, ses candidats retirés, `ProviderDisabledError(pid)` s'il ne reste aucun candidat. Helper `_still_active(provider_id: str) -> bool` (relit `load_providers()`).

- [ ] **Step 1: Écrire les tests qui échouent**

```python
async def test_forward_refuses_a_disabled_model(capped_a, _providers_file):
    # A inactive -> pytest.raises(router.ProviderDisabledError) on forward(_body(ONLY_A)); 0 POST on URL_A

async def test_forward_stream_refuses_a_disabled_model(capped_a, _providers_file):
    # same with forward_stream, before any chunk

async def test_waiter_is_refused_after_the_switch(capped_a, _providers_file, monkeypatch):
    monkeypatch.setattr(provider_admission, "WAIT_S", 2.0)
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        task = asyncio.create_task(router.forward(_body(ONLY_A)))
        await _until(lambda: provider_admission.waiting("test-provider") == 1)
        # rewrite providers.json with capped_a is_active False; router.invalidate_models_cache()
        held.release()
        with pytest.raises(router.ProviderDisabledError):
            await asyncio.wait_for(task, 2)
        assert _calls(mock, URL_A) == 0
    assert provider_admission._active == {}

async def test_stream_waiter_is_refused_after_the_switch(...):  # same with forward_stream

async def test_running_call_finishes_after_the_switch(capped_a, _providers_file):
    # a stream already holding its slot keeps yielding its chunks after the switch

async def test_chat_maps_a_late_refusal_to_503(client, capped_a, _providers_file, monkeypatch):
    # forward replaced by one raising ProviderDisabledError("test-provider") -> 503 provider_disabled
```

- [ ] **Step 2: Lancer, voir l'échec** — `$PYTEST tests/test_ollama_switch_router.py` → pas d'exception / appel fait.
- [ ] **Step 3: Implémenter** les contrôles (a) et (b) et le mappage dans `app.py`.
- [ ] **Step 4: Lancer** — `$PYTEST tests/test_ollama_switch_router.py` puis `$PYTEST tests/test_router_admission.py` → tout passe.
- [ ] **Step 5: Commit** — `fix(router): never call a disabled provider, even after a wait (PLU-12)`.

### Task 4: préflight et contrat d'API

**Files:**
- Modify: `plugboard/app.py` (`_preflight_refusal` `:2732-2774` ; `preflight` `:2817-2830`) ; `plugboard/api_contract.py` (`CONTRACT_VERSION` `:33`, `PreflightResult.error` `:353-372`, `ErrorBody.code` `:495-502`, réponse `"503"` `:579-580`) ; `docs/api/openapi.json` (régénéré) ; `docs/api/README.md` (ligne 503 `:161`, codes du préflight)
- Test: `tests/test_ollama_switch.py`

**Interfaces:**
- Consumes: Task 1, Task 2 (`_check_model_access` via `_route_for_guard`), Task 3 (`ProviderDisabledError`).
- Produces: code préflight `provider_disabled`, placé après `forbidden_model` et avant `model_not_found` ; `except ProviderDisabledError: error = "provider_disabled"` autour du ping.

- [ ] **Step 1: Écrire les tests qui échouent** — fixture `pinged` copiée de `tests/test_model_not_found.py:385-400`.

```python
@pytest.mark.parametrize("caller", ["client key", "admin key"])
async def test_preflight_answers_provider_disabled_without_a_ping(off, pinged, caller):
    result = await _preflight(client, auth, "ollama", "qwen3:8b")   # helper of test_model_not_found
    assert (result["ok"], result["error"]) == (False, "provider_disabled")
    assert calls == [] and _usage_rows() == []

def test_contract_documents_provider_disabled():
    assert api_contract.CONTRACT_VERSION == "1.6.0"
    assert "provider_disabled" in typing.get_args(api_contract.PreflightResult.model_fields["error"].annotation.__args__[0])
    # and "provider_disabled" appears in the 503 description and in ErrorBody.code's description
```

- [ ] **Step 2: Lancer, voir l'échec** — `$PYTEST tests/test_ollama_switch.py -k "preflight or contract"`.
- [ ] **Step 3: Implémenter** ; régénérer : `env PLUGBOARD_SKIP_DOTENV=1 /srv/dev/plugboard/.venv/bin/python scripts/export_contract.py` puis `... scripts/export_contract.py --check` (sortie sans écart).
- [ ] **Step 4: Lancer** — `$PYTEST tests/test_ollama_switch.py`, puis `$PYTEST tests/test_api_contract.py`, puis `$PYTEST tests/test_model_not_found.py` → tout passe.
- [ ] **Step 5: Commit** — `feat(api): preflight reports provider_disabled; API contract 1.6.0 (PLU-12)`.

### Task 5: plus aucun appel de fond vers un fournisseur coupé

**Files:**
- Modify: `plugboard/ollama.py` (`bootstrap_ollama` `:314-360`) ; `plugboard/semantic_cache.py` (`_embed` `:674-678`)
- Test: `tests/test_ollama_switch_admin.py` (nouveau)

**Interfaces — Produces:**
- `bootstrap_ollama()` : si `_OLLAMA_ENABLED != "false"` et que le fournisseur `ollama` existe avec `is_active` faux → `log.info("Ollama is turned off: boot check and model sync skipped")`, renvoie l'enregistrement, **aucun** appel (`check_available`, `list_models`).
- `_embed()` : fournisseur inactif → `None` sans requête (repli MinHash).

- [ ] **Step 1: Écrire les tests qui échouent**

```python
async def test_boot_skips_a_disabled_ollama(tmp_path, monkeypatch):
    # providers.json [OLLAMA inactive]; ollama._OLLAMA_ENABLED = "true";
    # check_available / list_models fakes record calls
    record = await ollama.bootstrap_ollama()
    assert record["id"] == "ollama" and record["is_active"] is False and calls == []

async def test_boot_still_syncs_an_enabled_ollama(tmp_path, monkeypatch):
    # same with is_active True: check_available called once, list_models called once

async def test_semantic_embed_skips_a_disabled_provider(tmp_path, monkeypatch):
    # env PLUGBOARD_SEMANTIC_CACHE=1, PLUGBOARD_SEMANTIC_EMBED=1,
    # PLUGBOARD_EMBED_PROVIDER_ID=ollama, PLUGBOARD_EMBED_MODEL=nomic-embed-text;
    # netguard.client_session replaced by a fake that fails the test if opened
    assert await semantic_cache._embed("a prompt long enough to embed") is None
```

- [ ] **Step 2: Lancer, voir l'échec** — `$PYTEST tests/test_ollama_switch_admin.py`.
- [ ] **Step 3: Implémenter** les deux gardes.
- [ ] **Step 4: Lancer** — `$PYTEST tests/test_ollama_switch_admin.py`, puis `$PYTEST tests/test_ollama.py`, puis `$PYTEST tests/test_semantic_cache.py` → tout passe.
- [ ] **Step 5: Commit** — `fix(ollama): no boot probe or semantic-cache embedding while Ollama is off (PLU-12)`.

### Task 6: l'interrupteur — logique, routes admin, audit, doc

**Files:**
- Modify: `plugboard/ollama.py` (nouvelle section « Switch ») ; `plugboard/app.py` (routes après `/admin/ollama/settings` `:4704` ; audit de `patch_provider` `:3840-3845`) ; `docs/guide/providers-and-subscriptions.md` (section Ollama `:137-147`) ; `docs/guide/configuration.md` (ligne `OLLAMA_ENABLED` `:113`)
- Test: `tests/test_ollama_switch_admin.py`

**Interfaces:**
- Produces, dans `plugboard/ollama.py` :
  - `switch_state() -> Dict[str, Any]` = `{"provider_id": "ollama", "registered": bool, "enabled": bool (False si non enregistré), "in_flight": provider_admission.in_flight("ollama"), "waiting": provider_admission.waiting("ollama")}`.
  - `async def set_enabled(enabled: bool) -> Dict[str, Any]` : `LookupError` si non enregistré ; `update_provider("ollama", {"is_active": enabled})` ; `router.invalidate_models_cache()` ; si on passe de coupé à allumé, `await _sync_models(provider)` (erreurs tolérées, déjà journalisées) ; renvoie `{"before": bool, **switch_state()}`.
- Produces, dans `plugboard/app.py` : `class OllamaSwitchUpdate(BaseModel)` (`extra="forbid"`, `strict=True`, `enabled: bool`) ; `GET /admin/ollama/switch` → `switch_state()` ; `PUT /admin/ollama/switch` → `set_enabled`, audit `ollama.switch` (Global Constraints), 404 `"Ollama provider is not registered (see OLLAMA_URL and OLLAMA_ENABLED)"` si `LookupError`, renvoie `switch_state()`. `patch_provider` ajoute `"is_active": <valeur>` à ses `details` quand le champ est présent.

- [ ] **Step 1: Écrire les tests qui échouent** — fixture `admin_client` de `tests/conftest.py` (`ADMIN = {"Authorization": "Bearer adminkey"}`), fixtures `off` et `http` importées de `tests/test_ollama_switch.py`, providers `[OLLAMA actif, ACME]`, `audit._DB_PATH` dans `tmp_path`, faux `list_models` qui renvoie `[{"name": "qwen3:8b"}, {"name": "llama3:latest"}]` et compte ses appels.

```python
async def test_switch_off_persists_and_hides_the_models(admin_client, ...):
    r = await admin_client.put("/admin/ollama/switch", json={"enabled": False}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert json.loads(pfile.read_text())["providers"][0]["is_active"] is False
    ids = [m["id"] for m in (await admin_client.get("/v1/models", headers=ADMIN)).json()["data"]]
    assert not any(i.startswith("ollama:") for i in ids)

async def test_switch_on_resyncs_once_and_lists_again(admin_client, ...):
    # off then on: list_models fake called exactly once; /v1/models lists "ollama:qwen3:8b"

async def test_each_switch_is_audited(admin_client, ...):
    (entry,) = audit.get_audit_log(action="ollama.switch")
    assert (entry["target_type"], entry["target_id"]) == ("provider", "ollama")
    assert json.loads(entry["details"]) == {"from": True, "to": False, "in_flight": 0, "waiting": 0}

async def test_switch_reports_calls_in_flight(admin_client, ...):
    # hold provider_admission.try_acquire(OLLAMA) -> GET gives in_flight 1

async def test_switch_404_when_ollama_is_not_registered(admin_client, ...):
@pytest.mark.parametrize("body", [{}, {"enabled": "no"}, {"enabled": False, "x": 1}])
async def test_switch_put_is_strict(admin_client, body):            # 422
async def test_switch_needs_the_admin_key(off, http):  # client key of Task 2's `http`: GET and PUT -> 401 or 403
async def test_provider_update_audit_keeps_the_is_active_value(admin_client, ...):
    # PUT /providers/ollama {"is_active": False} -> provider.update details has "is_active": false
```

- [ ] **Step 2: Lancer, voir l'échec** — `$PYTEST tests/test_ollama_switch_admin.py -k switch` → 404/405 sur la route.
- [ ] **Step 3: Implémenter** ; doc : paragraphe **Ollama switch** dans `providers-and-subscriptions.md` (où, effet sur chaque surface, 503 `provider_disabled`, appels en cours et en attente, audit, persistance, page toujours utilisable) ; `configuration.md` : `OLLAMA_ENABLED` « registration at startup only; `false` does not turn off a provider already registered: use the switch on the Ollama page ».
- [ ] **Step 4: Lancer** — `$PYTEST tests/test_ollama_switch_admin.py`, puis `$PYTEST tests/test_ollama_downloads_guard.py` → tout passe.
- [ ] **Step 5: Commit** — `feat(ollama): switch Ollama on or off at runtime from the admin API (PLU-12)`.

### Task 7: tableau de bord — interrupteur sur la page Ollama

**Files:**
- Modify: `static/index.html` (section Ollama, au-dessus de `id="ollama-status"` `:386`) ; `static/app.js` (`initOllama` `:1425-1450`, `loadOllama`)
- Test: `tests/test_ollama_switch_dashboard.py` (nouveau, sur le modèle de `tests/test_egress_guard_dashboard.py`)

**Interfaces — Consumes:** `GET`/`PUT /admin/ollama/switch` (Task 6). **Produces:** case `id="ollama-enabled"` `data-testid="ollama-enabled"`, ligne `id="ollama-enabled-hint"` (appels en cours / en attente), bandeau `id="ollama-off-banner"` ; `loadOllamaSwitch()` appelée par `loadOllama` ; changement → `api("PUT", "/admin/ollama/switch", { enabled: … })` puis rechargement ; case grisée si `registered` faux.

- [ ] **Step 1: Écrire les tests qui échouent**

```python
def test_ollama_page_has_the_switch():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="ollama-enabled"' in html and 'id="ollama-off-banner"' in html
    assert "Available to the API, MCP and Playground" in html
    assert "Ollama is off: the API, the MCP server and the Playground do not use it." in html

def test_app_js_reads_and_sets_the_switch():
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert 'api("GET", "/admin/ollama/switch")' in js
    assert 'api("PUT", "/admin/ollama/switch", { enabled:' in js

def test_app_js_is_valid():  # node --check, skip if node absent
```

- [ ] **Step 2: Lancer, voir l'échec** — `$PYTEST tests/test_ollama_switch_dashboard.py`.
- [ ] **Step 3: Implémenter** (structure de la section inchangée ailleurs : PB-DASH-02 retouche `style.css` et les libellés).
- [ ] **Step 4: Lancer** — `$PYTEST tests/test_ollama_switch_dashboard.py`, puis `$PYTEST tests/test_egress_guard_dashboard.py` → tout passe.
- [ ] **Step 5: Vérification visuelle** — instance locale : `PLUGBOARD_DATA=<scratch>/data` (providers.json avec `OLLAMA` à `http://127.0.0.1:9`), `OLLAMA_URL=http://127.0.0.1:9`, `HOST=127.0.0.1`, `PORT` libre (vérifié par `ss -ltn`), `PLUGBOARD_LOCAL_AUTO_LOGIN=1`, `/srv/dev/plugboard/.venv/bin/python run.py` en arrière-plan ; MCP playwright : page Ollama à 390 px et 1280 px, interrupteur coupé puis rallumé, bandeau visible puis absent ; captures dans le scratch ; instance arrêtée à la fin.
- [ ] **Step 6: Commit** — `feat(dashboard): Ollama switch on the Ollama page (PLU-12)`.

### Task 8: bout en bout « coupé puis rallumé sans redémarrage », MCP compris ; CHANGELOG

**Files:**
- Test: `tests/test_ollama_switch_e2e.py` (nouveau)
- Modify: `CHANGELOG.md` (sous `## [Unreleased]`, `### Added` et `### Fixed`)

**Interfaces — Consumes:** tout ce qui précède ; `mcp_server.list_models()`, `mcp_server.chat(model=, message=)`, `mcp_server.embed(input=, model=)` (inchangés).

- [ ] **Step 1: Écrire le test** — fixtures : clé client réelle ; `forward`/`forward_stream` enregistreurs ; `PLUGBOARD_EMBEDDING_MODELS=ollama:qwen3-embedding:0.6b` ; passerelle aiohttp en boucle locale (modèle `gateway` de `tests/test_mcp_embeddings.py`) qui **relaie** chaque requête vers l'application via `httpx.AsyncClient(transport=ASGITransport(app=app_module.app))` ; `mcp_server._BASE_URL` pointé dessus, `_API_KEY` = la clé client, `_HTTP_SESSION = None` ; `pytest.importorskip("mcp")`.

```python
async def test_switch_off_then_on_without_restart(...):
    await admin.put("/admin/ollama/switch", json={"enabled": False}, headers=ADMIN)
    assert not [m for m in await _model_ids(client) if m.startswith("ollama:")]
    assert (await client.post("/v1/chat/completions", json=_chat("ollama:qwen3:8b"), headers=auth)).status_code == 503
    assert (await client.post("/v1/embeddings", json={"model": "ollama:qwen3-embedding:0.6b", "input": "hi"}, headers=auth)).status_code == 503
    assert (await _preflight(client, auth, "ollama", "qwen3:8b"))["error"] == "provider_disabled"
    assert "ollama:" not in await mcp_server.list_models()
    assert "provider_disabled" in await mcp_server.chat(model="ollama:qwen3:8b", message="hi")
    assert "engine_unavailable" in await mcp_server.embed(input="hi", model="ollama:qwen3-embedding:0.6b")
    assert calls == []
    await admin.put("/admin/ollama/switch", json={"enabled": True}, headers=ADMIN)
    assert "ollama:qwen3:8b" in await _model_ids(client)
    await client.post("/v1/chat/completions", json=_chat("ollama:qwen3:8b"), headers=auth)
    assert calls == ["ollama:qwen3:8b"]
    emb = (await client.get("/v1/embeddings/models", headers=auth)).json()["data"]
    assert emb[0]["available"] is True
```

- [ ] **Step 2: Lancer** — `$PYTEST tests/test_ollama_switch_e2e.py` → passe (sinon corriger dans la tâche concernée, nouveau commit).
- [ ] **Step 3: CHANGELOG** — `### Added` : « **Ollama switch** » (page Ollama, `GET`/`PUT /admin/ollama/switch`, effet sur API, MCP, Playground, embeddings, préflight, appels en cours et en attente, audit `ollama.switch`, API contract 1.6.0) ; `### Fixed` : « A request for a model of a disabled provider (`ollama:…`, or a name only it lists) was sent to every other active provider; it now gets 503 `provider_disabled` and nothing is called. »
- [ ] **Step 4: Vérification finale** — `$PYTEST` sur chacun des fichiers `tests/test_ollama_switch*.py`, `tests/test_model_not_found.py`, `tests/test_router_admission.py`, `tests/test_embeddings_api.py`, `tests/test_api_contract.py` (un à la fois) ; `/srv/dev/plugboard/.venv/bin/ruff check plugboard/ tests/` ; `/srv/dev/plugboard/.venv/bin/ruff format --check plugboard/ tests/` ; `make secret-scan` ; sorties collées dans le rapport.
- [ ] **Step 5: Commit** — `test(ollama): switch off then on without restart across API, MCP and Playground (PLU-12)`.

## Variantes (si YB ne prend pas la recommandation)

- Q2 = 404 : `_provider_disabled_response` renvoie `_model_not_found_response` ; code préflight `model_not_found` ; Task 4 sans changement de contrat.
- Q3 = Ollama seulement : `disabled_route` ne renvoie que le fournisseur d'id `ollama` ; les autres fournisseurs inactifs gardent le routage actuel (trou connu, à documenter).
- Q5 = tout ce qui est admis va au bout : supprimer le contrôle (b) de la Task 3 et `test_*waiter_is_refused*`.
- Q7 = page figée : les routes `/admin/ollama/{status,models,cloud-catalog}` et le téléchargement renvoient 409 quand c'est coupé (tâche supplémentaire après la Task 6).
- Q8 = `false` verrouille : `set_enabled` lève 409 si `OLLAMA_ENABLED=false`, et `bootstrap_ollama` passe `is_active` à faux (tâche supplémentaire).

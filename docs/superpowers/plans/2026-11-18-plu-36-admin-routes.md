# Routes d'administration hors de `app.py` (PLU-36) — plan d'exécution

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sortir 92 routes d'administration de `plugboard/app.py` vers onze modules de `plugboard/admin_routes/`, sans aucun changement de comportement (D-67).

**Architecture:** Chaque module expose `register(app: FastAPI, core: ModuleType) -> SimpleNamespace` ; ses handlers y sont déclarés avec `@app.<méthode>` (pas d'`APIRouter` : FastAPI 0.143.3 les garde derrière un `_IncludedRouter`, ce qui change la table et aveugle deux tests). `app.py` appelle `register(app, _THIS_MODULE)` à la place de chaque bloc ; `core` est l'objet module `plugboard.app` qui enregistre, si bien que tout point de patch se lit encore au moment de l'appel dans ce module, même après une réimportation. Une garde écrite d'abord fige la table des routes, le document OpenAPI admin et les effets d'import de `672118e`.

**Tech Stack:** Python 3.12, FastAPI 0.143.3 / Starlette 1.8, pytest (`asyncio_mode = auto`), httpx `ASGITransport`, ruff.

**Spec:** `docs/superpowers/specs/2026-11-18-plu-36-admin-routes-design.md` (D-67, fait foi ; règles R1–R9 et tableau des points de patch).

## Global Constraints

- Atelier : `/srv/dev/worktrees/plugboard/plu36`, branche `refactor/plu-36-admin-routes` (base `672118e`). Rien hors de l'atelier. Interdits : `git commit --amend`, rebase, reset, push. Un commit **nouveau** par tâche, Conventional Commits, message terminé par `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Comportement inchangé : mêmes routes, même ordre, mêmes dépendances, mêmes réponses, même document OpenAPI admin, mêmes effets d'import.
- **Aucun test existant modifié, assoupli, sauté ni supprimé.** Contrôle : `git diff --name-status 672118e -- tests/ | grep -v '^A'` ne sort rien.
- Après la tâche 1, `tests/route_snapshot.py`, `tests/test_admin_routes_guard.py` et `tests/fixtures/admin_routes_snapshot.json` ne changent plus, et la garde reste verte à chaque tâche.
- `CONTRACT_VERSION` reste `"3.0.1"` ; `docs/api/openapi.json` et `docs/api/mcp.json` ne changent pas.
- Tests ciblés seulement pendant le lot (règle YB du 18/11) ; une seule validation complète, à la section « Jalon ». Commandes :
  - `PY=/srv/dev/plugboard/.venv/bin/python` ; `RUFF=/srv/dev/plugboard/.venv/bin/ruff`
  - `PYTEST="env PLUGBOARD_SKIP_DOTENV=1 $PY -m pytest -q -p no:cacheprovider"` (le `tests/conftest.py` donne à chaque processus son propre `PLUGBOARD_DATA` temporaire)
  - Les listes de fichiers sont **triées** et contiennent toujours `tests/test_audit_e2e.py` : sa réimportation de `plugboard.app` (E-25) a lieu au même rang que dans la suite complète.
- Lint de chaque tâche : `$RUFF check plugboard/ tests/` puis `$RUFF format --check plugboard/ tests/` (la CI exige les deux).
- Nettoyage après chaque passe : rien à garder dans `/tmp` ; supprimer les dossiers temporaires créés à la main.

## Review Focus

- **Réimportation de `plugboard.app`** (E-25, puis alias de module figés dans les fichiers collectés avant) : un handler de l'ancien module doit lire les points de patch de l'ancien module. Épinglé par `test_reexported_probe_reads_the_module_that_registered_it` (tâche 2).
- **État de module écrit par une route** : `admin_models_refresh` doit écrire `_LAST_REFRESH_AT` sur `plugboard.app`, sinon le délai de 60 s ne bloque plus rien. Épinglé par `test_refresh_stamp_is_written_on_the_app_module` (tâche 3).
- **Routes cachées de l'OpenAPI** (`/login`, `/auth/admin/*`, `/admin/mfa*`, `/oauth/callback/{source}`) : leurs paramètres et dépendances ne se voient que dans la table des routes. Épinglé par la garde (paramètres et arbre des dépendances, tâche 1).
- **Journaux** : un module déplacé qui journaliserait sous un autre logger changerait `source_service` et les filtres des exploitants. Épinglé par `test_moved_code_logs_under_the_app_logger` (tâche 1).
- **Route publique sans garde** : `/oauth/callback/{source}` n'a pas `_require_admin` et ne doit pas en gagner (ni les autres en perdre). Épinglé par l'arbre des dépendances de la garde (tâche 1).

---

## Structure des fichiers

| Fichier | Rôle | Tâche |
|---|---|---|
| `tests/route_snapshot.py` | Calcule table des routes, OpenAPI admin, effets d'import ; écrit le gabarit (`python -m tests.route_snapshot`) | 1 |
| `tests/fixtures/admin_routes_snapshot.json` | Gabarit figé sur `672118e` | 1 |
| `tests/test_admin_routes_guard.py` | Compare le présent au gabarit | 1 |
| `tests/test_admin_routes_split.py` | Invariants du découpage ; deux tests de comportement ajoutés en tâches 2 et 3 | 1, 2, 3 |
| `plugboard/admin_routes/__init__.py` | Paquet, docstring du contrat (n'importe aucun sous-module) | 2 |
| `plugboard/admin_routes/model_probes.py` … `auth.py` | Un module par bloc contigu de routes | 2–12 |
| `plugboard/app.py` | Blocs remplacés par `register()` ; ré-exports ; imports inutiles retirés | 2–13 |
| `docs/ARCHITECTURE.md`, `CHANGELOG.md` | Où vivent les routes d'administration | 13 |

## Procédure commune de déplacement (tâches 2 à 12)

Chaque tâche de module applique ces étapes ; la tâche donne ce qui lui est propre (bloc, noms, câblage, tests). Les numéros de ligne sont ceux de `672118e`, pour s'orienter : repérer les blocs par leurs handlers.

- **P1 — En-tête du module** : docstring (domaine, « moved verbatim from `plugboard/app.py` (PLU-36) », rappel que `core` est le module `plugboard.app` qui enregistre et que les points de patch se lisent par `core.`), `from __future__ import annotations`, imports, `log = logging.getLogger("plugboard.app")` si le bloc journalise.
- **P2 — Niveau module** : les éléments listés « niveau module » de la tâche (modèles Pydantic, constantes, aides sans `core`), texte verbatim. Tout nom employé dans une annotation de handler doit être une globale du module (R5).
- **P3 — `register()`** : `def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:` puis, dans l'ordre du fichier d'origine, les fermetures listées par la tâche (handlers et aides qui lisent `core`) : décorateur, signature, docstring et corps verbatim, avec `Depends(core._require_admin)` et `core.<nom>` pour chaque nom de la ligne « via `core.` » de la tâche. Un point de patch qui part (`check_model_internet_access`, `check_all_models_internet_access`, `_test_openai_endpoint`, `_schedule_initial_discovery`) est toujours défini dans `register()`, même s'il ne lit pas `core`, puis ré-exporté. Fin : `return SimpleNamespace(<noms ré-exportés>)` (vide : `SimpleNamespace()`).
- **P4 — Imports du module** : tout autre nom libre vient d'un import de même forme que dans `app.py` (R6) ; jamais `plugboard.app`, `sys.modules` ni `import_module`.
- **P5 — `app.py`** : supprimer les éléments déplacés ; laisser en place ceux que la tâche liste « reste » ; à la place de la **dernière route** du bloc, écrire le câblage donné par la tâche ; ajouter l'import du module à côté de `from plugboard.admin_people_api import build_router as build_people_router` ; retirer les imports devenus inutiles (`ruff` F401), **sauf** un point de patch (liste `PATCH_POINTS` de `tests/test_admin_routes_split.py`), gardé avec `# noqa: F401  (patch point read through core, PLU-36)`.
- **P6 — Lint** : `$RUFF format plugboard/admin_routes/ plugboard/app.py`, puis `$RUFF check plugboard/ tests/` et `$RUFF format --check plugboard/ tests/` : 0 erreur.
- **P7 — Relecture du déplacement** : `git diff --color-moved=dimmed-zebra --color-moved-ws=allow-indentation-change -- plugboard/` ; les seules lignes non grisées sont les `core.`, le câblage, les imports et le reformatage.

---

### Task 1: Garde — table des routes, OpenAPI admin et effets d'import figés

**Files:**
- Create: `tests/route_snapshot.py`, `tests/test_admin_routes_guard.py`, `tests/test_admin_routes_split.py`, `tests/fixtures/admin_routes_snapshot.json` (généré)

**Interfaces:**
- Consumes: `plugboard.app.app` tel qu'à `672118e`.
- Produces: `route_table(app) -> list[dict]`, `admin_openapi(app) -> dict`, `import_effects() -> dict`, `FIXTURE: Path` dans `tests/route_snapshot.py` ; constantes `PATCH_POINTS`, `READ_SURFACE` dans `tests/test_admin_routes_split.py` (lues par P5).

- [ ] **Step 1: Écrire `tests/route_snapshot.py`**

Docstring : « Snapshot of what the admin routes split must not change (PLU-36) ». Le calcul est l'algorithme ci-dessous (prototypé sur `672118e` : 122 entrées, résultat identique sur deux processus) :

```python
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "admin_routes_snapshot.json"
_PLUGBOARD_PATH = re.compile(r"\bplugboard(?:\.\w+)*\.(\w+)")


def _type_name(t) -> str | None:
    """A type's name without its module path: a model may change module."""
    if t is None:
        return None
    t = getattr(t, "value", t)  # fastapi DefaultPlaceholder
    text = t.__qualname__ if isinstance(t, type) else repr(t)
    if " at 0x" in text:
        raise ValueError(f"no stable name for {text}")
    return _PLUGBOARD_PATH.sub(r"\1", text)


def _call_name(call) -> str:
    return f"{call.__module__}.{call.__qualname__}"


def _dependencies(dependant) -> list:
    return [[_call_name(d.call), _dependencies(d)] for d in dependant.dependencies]


def _params(dependant) -> list:
    out = []
    for where in ("path", "query", "header", "cookie", "body"):
        for f in getattr(dependant, f"{where}_params"):
            out.append([where, f.name, f.alias, f.field_info.is_required(),
                        _type_name(f.field_info.annotation)])
    return out


def _route(route) -> dict:
    if not isinstance(route, APIRoute):
        inner = getattr(route, "original_router", None)
        if inner is not None:
            return {"kind": "included_router", "routes": [_route(r) for r in inner.routes]}
        return {"kind": type(route).__name__, "path": getattr(route, "path", None)}
    return {
        "kind": "api", "path": route.path, "methods": sorted(route.methods),
        "name": route.name, "coroutine": inspect.iscoroutinefunction(route.endpoint),
        "response_model": _type_name(route.response_model),
        "status_code": route.status_code,
        "response_class": _type_name(route.response_class),
        "include_in_schema": route.include_in_schema, "summary": route.summary,
        "description": route.description, "tags": list(route.tags or []),
        "deprecated": route.deprecated, "operation_id": route.operation_id,
        "unique_id": route.unique_id,
        "dependencies": _dependencies(route.dependant),
        "params": _params(route.dependant),
    }


def route_table(app) -> list:
    return [_route(r) for r in app.routes]


def admin_openapi(app) -> dict:
    doc = copy.deepcopy(app.openapi())
    doc["info"]["version"] = "<version>"  # a release must not break the guard
    return json.loads(json.dumps(doc, sort_keys=True))
```

`import_effects()` lance `[sys.executable, "-c", _PROBE]` (cwd `ROOT`, `timeout=120`, `check=True`) dans un `tempfile.TemporaryDirectory(prefix="plu36-import-")` `tmp`, avec **seulement** cet environnement : `PATH` (repris), `HOME=tmp`, `PLUGBOARD_DATA=tmp/data`, `CODEX_HOME=tmp/codex`, `PLUGBOARD_SKIP_DOTENV=1`, `PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC=0`, `API_KEY=testkey-conftest`, `ADMIN_KEY=adminkey-conftest`. `_PROBE` importe `plugboard.app` puis imprime une ligne `EFFECTS {json}` avec `modules` (noms triés de `sys.modules` commençant par `plugboard`, sauf `plugboard.admin_routes*`) et `data_files` (chemins triés relatifs à `PLUGBOARD_DATA`). Retour : `{"logs": [[level, logger, msg], …], "modules": …, "data_files": …}` ; chaque ligne de stdout+stderr qui commence par `{` est un enregistrement JSON du journal, toute autre ligne hors `EFFECTS` donne `["raw", "", ligne]`.

`main()` (bloc `if __name__ == "__main__":`) : `import tests.conftest  # noqa: F401` (même environnement que la suite), puis écrit `FIXTURE` avec `{"routes": …, "openapi": …, "import_effects": …}` en `json.dumps(..., indent=1, sort_keys=True, ensure_ascii=False) + "\n"`.

- [ ] **Step 2: Écrire `tests/test_admin_routes_guard.py`**

```python
"""The admin routes split changes nothing (PLU-36): same route table, same
admin OpenAPI document, same import effects as 672118e."""

import json

from tests import route_snapshot


def _expected(key):
    return json.loads(route_snapshot.FIXTURE.read_text(encoding="utf-8"))[key]


def test_route_table_is_unchanged():
    from plugboard.app import app

    assert route_snapshot.route_table(app) == _expected("routes")


def test_admin_openapi_document_is_unchanged():
    from plugboard.app import app

    assert route_snapshot.admin_openapi(app) == _expected("openapi")


def test_import_has_the_same_effects():
    assert route_snapshot.import_effects() == _expected("import_effects")
```

- [ ] **Step 3: Lancer la garde sans gabarit**

Run: `$PYTEST tests/test_admin_routes_guard.py`
Expected: 3 FAIL, `FileNotFoundError` sur `admin_routes_snapshot.json`.

- [ ] **Step 4: Écrire le gabarit sur le code actuel**

Run: `git -C /srv/dev/worktrees/plugboard/plu36 diff --quiet 672118e -- plugboard/ && cd /srv/dev/worktrees/plugboard/plu36 && $PY -m tests.route_snapshot`
Expected: `tests/fixtures/admin_routes_snapshot.json` créé ; `routes` a 122 entrées (118 `api`, 4 `included_router`) ; `import_effects.logs` = les deux lignes `INFO plugboard.app` (`auth.mode=ENFORCED api_key_set=True`, `auth.auto_login mode=auto local_install=False`) ; `data_files` = `[".encryption_key"]`.

- [ ] **Step 5: Lancer la garde**

Run: `$PYTEST tests/test_admin_routes_guard.py`
Expected: 3 passed.

- [ ] **Step 6: Vérifier que la garde voit un changement d'ordre**

Run: `cd /srv/dev/worktrees/plugboard/plu36 && $PY -c "import tests.conftest, json; from tests import route_snapshot as s; from plugboard.app import app; r = app.router.routes; r[5], r[6] = r[6], r[5]; assert s.route_table(app) != json.loads(s.FIXTURE.read_text())['routes']; print('guard sees the swap')"`
Expected: `guard sees the swap` (permutation en mémoire seulement, aucun fichier touché).

- [ ] **Step 7: Écrire `tests/test_admin_routes_split.py`**

Docstring : « Invariants of the admin routes split out of app.py (PLU-36) ». Imports : `ast`, `importlib`, `inspect`, `pkgutil`, `sys`, `pathlib.Path`, `fastapi.routing.APIRoute`. Constantes (inventaire AST de `tests/` à `672118e`, voir la conception) :

```python
PATCH_POINTS = frozenset({
    "_ADMIN_KEY", "_API_KEY", "_BUDGET_RESERVE_TOKENS", "_BULK_INTERNET_CONCURRENCY",
    "_CACHE_RESPONSE_FLOOR_MS", "_DEV_MODE", "_INFLIGHT_CHAT_MAX", "_INFLIGHT_WAIT_S",
    "_LAST_REFRESH_AT", "_MAX_BODY_BYTES", "_MODEL_REFRESH_COOLDOWN_S",
    "_REFUSAL_AUDIT_MAX_KEYS", "_REFUSAL_AUDIT_WINDOW_S", "_STATIC_DIR",
    "_STATIC_DIR_RESOLVED", "_admin_request_auth", "_apply_key_record", "_audit",
    "_bill_streaming", "_check_and_alert", "_check_price_known",
    "_check_stored_secrets", "_client_address", "_get_request_key_record",
    "_hold_budget", "_inflight_chat_active", "_inflight_chat_sem", "_is_admin_key",
    "_is_admin_session", "_model_is_compatible", "_model_route_guard",
    "_record_session_turn", "_require_public_host", "_schedule_initial_discovery",
    "_send_spending_alert", "_served_by", "_test_openai_endpoint", "_usage",
    "check_all_models_internet_access", "check_model_internet_access", "cli_refusal",
    "discover_models", "forward", "forward_stream", "get_key_record", "get_provider",
    "invalidate_models_cache", "list_models", "load_providers",
    "resolve_provider_and_model", "update_provider",
})
# Read on plugboard.app by the tests (directly or as "module:attr") and by
# decision_api.py / embeddings_api.py: these names must stay reachable there.
READ_SURFACE = frozenset({
    "HTTPException", "KEY_PREFIXES", "_ADMIN_COOKIE_NAME", "_ADMIN_SESSION_MAX_AGE",
    "_BULK_INTERNET_PER_MODEL_TIMEOUT_S", "_BodySizeLimitMiddleware",
    "_LOGIN_HTML_TEMPLATE", "_MAX_HELD", "_ModelNotAllowed", "_RequestIdMiddleware",
    "_auto_login_mode", "_background_tasks", "_bill_completion", "_billing_target",
    "_bind_host", "_check_dashboard_auth", "_check_model_access",
    "_check_spending_limit", "_cli_error_response", "_complete_events",
    "_create_admin_session_token", "_default_monthly_limit_usd", "_default_rpm_limit",
    "_done_event_at", "_estimate_call_cost", "_get_inflight_sem", "_host_header_name",
    "_inflight_chat_dep", "_is_loopback_address", "_issue_local_login_token",
    "_lifespan", "_model_not_allowed_handler", "_model_not_served_response",
    "_pad_cache_hit_response", "_providers_busy_response", "_quota_error_response",
    "_refusal_audit_seen", "_reject_comment_valued_secrets", "_require_admin",
    "_require_admin_key_outside_dev", "_require_mfa_step_up", "_resolve_client",
    "_spending_scope", "_suggestion_action", "_take_budget_hold",
    "_value_error_handler", "_warn_trusted_proxies", "_warned_auto_login_values",
    "admin_catalog_history", "admin_models_refresh", "app", "chat_completions",
    "get_providers", "health_detailed", "log", "models", "models_v2",
    "oauth_disconnect", "trusted_proxy_warning",
})
```

Tests (chacun passe sur `672118e`, les quatre premiers à vide tant que le paquet n'existe pas) :

| Test | Assertion |
|---|---|
| `test_no_admin_route_module_binds_a_patch_point` | pour chaque module de `plugboard.admin_routes` (`pkgutil.iter_modules`, liste vide si `ModuleNotFoundError`) : `PATCH_POINTS & set(vars(module)) == set()` |
| `test_admin_route_modules_never_import_the_app_module` | par AST de chaque `plugboard/admin_routes/*.py` : aucun `import plugboard.app`, `from plugboard.app import …`, `from plugboard import app`, attribut `sys.modules`, appel `import_module`/`__import__` (la chaîne `"plugboard.app"` reste permise : nom du logger, docstrings) |
| `test_each_module_registers_through_register` | `list(inspect.signature(module.register).parameters) == ["app", "core"]` |
| `test_moved_code_logs_under_the_app_logger` | `module.log is plugboard.app.log` pour tout module qui a `log` |
| `test_names_read_on_the_app_module_are_still_there` | `sorted(n for n in PATCH_POINTS \| READ_SURFACE if not hasattr(app_module, n)) == []` |
| `test_reexported_handlers_are_the_registered_endpoints` | avec `endpoints = {r.name: r.endpoint for r in app_module.app.routes if isinstance(r, APIRoute)}` : pour chaque nom de `(PATCH_POINTS \| READ_SURFACE) & endpoints.keys()`, `getattr(app_module, nom) is endpoints[nom]` |

`app_module` : `importlib.import_module("plugboard.app")` dans chaque test (le module courant).

- [ ] **Step 8: Lancer garde, invariants et les tests qui parcourent les routes**

Run: `$PYTEST tests/test_admin_csrf_same_site.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_chat_admission.py tests/test_spec_v7_residuals_parent.py`
Expected: tout passe (base de comparaison des tâches suivantes).

- [ ] **Step 9: Mesurer le temps d'import de référence**

Run (5 fois) : `cd /srv/dev/worktrees/plugboard/plu36 && T=$(mktemp -d) && env -i PATH=/usr/bin:/bin HOME=$T PLUGBOARD_DATA=$T/data PLUGBOARD_SKIP_DOTENV=1 API_KEY=k ADMIN_KEY=adminkey-conftest PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC=0 $PY -X importtime -c 'import plugboard.app' 2>&1 | grep -E '\| plugboard.app$'; rm -rf "$T"`
Expected: une ligne par passe ; noter la médiane du cumul (µs) dans le corps du commit.

- [ ] **Step 10: Lint puis commit**

`$RUFF check plugboard/ tests/` et `$RUFF format --check plugboard/ tests/` : 0 erreur.

```bash
git add tests/route_snapshot.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/fixtures/admin_routes_snapshot.json
git commit -m "test(admin): freeze the route table, admin OpenAPI and import effects (PLU-36)" -m "Import time of plugboard.app (median of 5, -X importtime): <N> us." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Module `model_probes` — disjoncteurs et sondes d'accès internet

**Files:**
- Create: `plugboard/admin_routes/__init__.py`, `plugboard/admin_routes/model_probes.py`
- Modify: `plugboard/app.py` (bloc 4284–4667 ; `import sys` ; `_THIS_MODULE`)
- Test: `tests/test_admin_routes_split.py` (ajout d'un test)

**Interfaces:**
- Consumes: `core` = module `plugboard.app`.
- Produces: `model_probes.register(app, core) -> SimpleNamespace(check_model_internet_access, check_all_models_internet_access)` ; `_THIS_MODULE` dans `app.py` (utilisé par toutes les tâches suivantes).

- [ ] **Step 1: Écrire le test de réimportation** dans `tests/test_admin_routes_split.py`

```python
async def test_reexported_probe_reads_the_module_that_registered_it(monkeypatch):
    """After plugboard.app is imported again (E-25 does it), the first module's
    bulk probe still reads the first module's patch points (PLU-36)."""
    import plugboard

    first = importlib.import_module("plugboard.app")
    monkeypatch.setitem(sys.modules, "plugboard.app", first)  # restored at teardown
    monkeypatch.setattr(plugboard, "app", first)
    del sys.modules["plugboard.app"]
    second = importlib.import_module("plugboard.app")
    assert second is not first

    probed = []

    async def _list_models():
        return [{"id": "anthropic-direct:claude-haiku-5-5",
                 "owned_by": "anthropic-direct", "provider": "anthropic-direct"}]

    async def _probe(body, _=None):
        probed.append(body["model_id"])
        return {"model_id": body["model_id"], "internet_access": False, "status": "ok"}

    monkeypatch.setattr(first, "list_models", _list_models)
    monkeypatch.setattr(first, "load_providers", lambda: [{
        "id": "anthropic-direct", "name": "Anthropic Direct",
        "base_url": "https://api.anthropic.com", "provider_type": "cloud"}])
    monkeypatch.setattr(first, "check_model_internet_access", _probe)
    await first.check_all_models_internet_access({}, _=None)
    assert probed == ["anthropic-direct:claude-haiku-5-5"]
```

- [ ] **Step 2: Le lancer sur le code actuel (caractérisation)**

Run: `$PYTEST tests/test_admin_routes_split.py`
Expected: PASS (le comportement à garder).

- [ ] **Step 3: Créer le paquet et le module** (P1–P4)

`plugboard/admin_routes/__init__.py` : docstring seule (le contrat `register(app, core)`, R3, R5, R7, renvoi à la conception) ; aucun import.
`model_probes.py` — routes, dans l'ordre : `GET /admin/circuits` `get_circuits` ; `POST /admin/circuits/{model}/reset` `reset_circuit_endpoint` ; `POST /admin/models/check-internet` `check_model_internet_access` ; `POST /admin/models/check-internet/all` `check_all_models_internet_access`.
- Niveau module : rien.
- Dans `register()` : les 4 handlers et `_without_local_ollama_models`.
- Via `core.` : `_require_admin`, `_audit`, `_BULK_INTERNET_CONCURRENCY`, `_BULK_INTERNET_PER_MODEL_TIMEOUT_S`, `_BULK_INTERNET_LOCK`, `check_model_internet_access` (l'appel de la vérification groupée, l. 4599), `forward`, `invalidate_models_cache`, `list_models`, `load_providers`, `resolve_provider_and_model`, `update_provider`.
- Reste dans `app.py`, à sa place : `_BULK_INTERNET_CONCURRENCY`, `_BULK_INTERNET_PER_MODEL_TIMEOUT_S`, `_BULK_INTERNET_LOCK` et leur commentaire.

- [ ] **Step 4: Câbler `app.py`** (P5)

Ajouter `import sys` aux imports standard ; juste après `app.include_router(embeddings_router)` :

```python
# The module object handed to plugboard.admin_routes.*.register (PLU-36): each
# import of this file binds its own admin handlers to itself.
_THIS_MODULE = sys.modules[__name__]
```

À la place de la dernière route du bloc (après les constantes `_BULK_INTERNET_*`) :

```python
# -- /admin/circuits, /admin/models/check-internet: admin_routes/model_probes.py
_model_probes = _admin_model_probes.register(app, _THIS_MODULE)
check_model_internet_access = _model_probes.check_model_internet_access
check_all_models_internet_access = _model_probes.check_all_models_internet_access
```

Import : `from plugboard.admin_routes import model_probes as _admin_model_probes`.

- [ ] **Step 5: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 6: Tests ciblés**

Run: `$PYTEST tests/test_admin_csrf_same_site.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_agy_account_catalog.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_bug102_quarantine_provider_scope.py tests/test_bug105_quarantine_lift_and_timeout.py tests/test_bug127_internet_check_quarantine.py tests/test_chat_admission.py tests/test_circuit_breaker_provider_faults.py tests/test_e2e_full_coverage.py tests/test_env_numbers_at_start.py tests/test_internet_access.py tests/test_internet_check_local_models.py tests/test_lifespan_boot_tasks.py tests/test_model_availability.py tests/test_router_admission.py tests/test_spec_v7_residuals_parent.py`
Expected: tout passe, garde comprise.

- [ ] **Step 7: Relire (P7) puis commit**

```bash
git add plugboard/admin_routes/__init__.py plugboard/admin_routes/model_probes.py plugboard/app.py tests/test_admin_routes_split.py
git commit -m "refactor(admin): move the circuit and internet-probe routes to admin_routes/model_probes.py (PLU-36)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Module `monitoring` — cache, quotas, surveillance, watchdog, état des modèles, audit

**Files:**
- Create: `plugboard/admin_routes/monitoring.py`
- Modify: `plugboard/app.py` (bloc 4673–4969)
- Test: `tests/test_admin_routes_split.py` (ajout d'un test)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE` (tâche 2).
- Produces: `monitoring.register(app, core) -> SimpleNamespace(admin_models_refresh)`.

- [ ] **Step 1: Écrire le test de l'horodatage**

```python
async def test_refresh_stamp_is_written_on_the_app_module(monkeypatch):
    """The refresh cooldown reads and writes _LAST_REFRESH_AT on plugboard.app
    (PLU-36): a second refresh within the cooldown answers 429."""
    app_module = importlib.import_module("plugboard.app")
    import plugboard.model_availability as availability

    async def _probes():
        return {"last_run_at": "now", "models": {"m": {}}}

    monkeypatch.setattr(availability, "run_connectivity_probes", _probes)
    monkeypatch.setattr(app_module, "_LAST_REFRESH_AT", 0.0)
    monkeypatch.setattr(app_module, "_MODEL_REFRESH_COOLDOWN_S", 60.0)
    monkeypatch.setattr(app_module, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "invalidate_models_cache", lambda: None)

    first = await app_module.admin_models_refresh(_=None)
    assert first["model_count"] == 1
    assert app_module._LAST_REFRESH_AT > 0
    second = await app_module.admin_models_refresh(_=None)
    assert second.status_code == 429
```

- [ ] **Step 2: Le lancer sur le code actuel** — Run: `$PYTEST tests/test_admin_routes_split.py` — Expected: PASS.

- [ ] **Step 3: Créer `monitoring.py`** (P1–P4)

Routes, dans l'ordre : `GET /admin/cache/stats` `cache_stats` ; `DELETE /admin/cache` `clear_cache_endpoint` ; `GET /admin/claude-quota` `admin_claude_quota` ; `GET /admin/monitoring` `admin_monitoring` ; `GET /admin/monitoring/history` `admin_monitoring_history` ; `GET /admin/watchdog` `admin_watchdog_status` ; `POST /admin/watchdog/start` `admin_watchdog_start` ; `POST /admin/watchdog/stop` `admin_watchdog_stop` ; `POST /admin/watchdog/{provider_id}/restart` `admin_watchdog_restart` ; `GET /admin/watchdog/events` `admin_watchdog_events` ; `GET /admin/watchdog/restarts` `admin_watchdog_restart_stats` ; `GET /admin/models/status` `admin_models_status` ; `POST /admin/models/refresh` `admin_models_refresh` ; `GET /admin/audit` `admin_audit_log` ; `GET /metrics/prometheus` `prometheus_metrics`.
- Niveau module : `_MODEL_REFRESH_TIMEOUT_S`, `_REASON_NORMALISATION`, `_normalise_reason`.
- Dans `register()` : les 15 handlers.
- Via `core.` : `_require_admin`, `_audit`, `invalidate_models_cache`, `_MODEL_REFRESH_LOCK`, `_MODEL_REFRESH_COOLDOWN_S`, `_LAST_REFRESH_AT`. R4 : supprimer `global _LAST_REFRESH_AT` ; lire `core._LAST_REFRESH_AT`, écrire `core._LAST_REFRESH_AT = time.monotonic()`.
- Reste dans `app.py`, à sa place : `_MODEL_REFRESH_LOCK`, `_MODEL_REFRESH_COOLDOWN_S`, `_LAST_REFRESH_AT`.

- [ ] **Step 4: Câbler `app.py`** (P5)

```python
# -- /admin/cache ... /admin/audit, /metrics/prometheus: admin_routes/monitoring.py
_monitoring = _admin_monitoring.register(app, _THIS_MODULE)
admin_models_refresh = _monitoring.admin_models_refresh
```

Import : `from plugboard.admin_routes import monitoring as _admin_monitoring`.

- [ ] **Step 5: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 6: Tests ciblés**

Run: `$PYTEST tests/test_admin_csrf_same_site.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_agy_account_catalog.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_audit_principal.py tests/test_boot_no_inference.py tests/test_bug102_quarantine_provider_scope.py tests/test_bug143_quota_excerpt_redaction.py tests/test_chat_admission.py tests/test_deploy_services_enabled.py tests/test_discovery_reasons.py tests/test_discovery_root_cause.py tests/test_docs_release_4145.py tests/test_docs_release_4146.py tests/test_e2e_dashboard.py tests/test_e2e_full_coverage.py tests/test_e2e_functional.py tests/test_e2e_subprocess.py tests/test_env_numbers_at_start.py tests/test_metrics_per_model.py tests/test_model_availability.py tests/test_monitoring.py tests/test_monitoring_p0.py tests/test_monitoring_snapshots.py tests/test_ovh_catalog_jobs.py tests/test_phase8.py tests/test_plu67_claude_quota_watch.py tests/test_plu72_existing_claude_provider.py tests/test_security_tnr.py tests/test_spec_v7_residuals_parent.py tests/test_watchdog.py`
Expected: tout passe.

- [ ] **Step 7: Relire (P7) puis commit**

```bash
git add plugboard/admin_routes/monitoring.py plugboard/app.py tests/test_admin_routes_split.py
git commit -m "refactor(admin): move the monitoring, watchdog and model-status routes to admin_routes/monitoring.py (PLU-36)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Module `providers` — `/providers`

**Files:**
- Create: `plugboard/admin_routes/providers.py`
- Modify: `plugboard/app.py` (bloc 4975–5398)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE` ; garde de sortie restée dans `app.py` (`_require_parsable_base_url`, `_check_guard_on_edit`, `_require_guard_allows`).
- Produces: `providers.register(app, core) -> SimpleNamespace(get_providers)`.

- [ ] **Step 1: Créer `providers.py`** (P1–P4)

Routes, dans l'ordre : `GET /providers` `get_providers` ; `POST /providers` (201) `add_provider` ; `PUT /providers/{provider_id}` `patch_provider` ; `DELETE /providers/{provider_id}` (204) `remove_provider` ; `GET /providers/{provider_id}/health` `provider_health` ; `POST /providers/{provider_id}/discover` `discover_provider_models`.
- Niveau module : `ProviderCreate`, `ProviderUpdate`, `_mask`, `_admin_actor`, `_refuse_unreadable_secret`, `_no_models_detail`.
- Dans `register()` : les 6 handlers et `_discover_provider`.
- Via `core.` : `_require_admin`, `_audit`, `_require_parsable_base_url`, `_check_guard_on_edit`, `_require_guard_allows`, `discover_models`, `get_provider`, `invalidate_models_cache`, `load_providers`, `update_provider`.
- Reste dans `app.py`, à sa place : `_require_parsable_base_url` (garde partagée avec la tâche 6).

- [ ] **Step 2: Câbler `app.py`** (P5)

```python
# -- /providers: admin_routes/providers.py
_providers = _admin_providers.register(app, _THIS_MODULE)
get_providers = _providers.get_providers
```

Import : `from plugboard.admin_routes import providers as _admin_providers`.

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_csrf_same_site.py tests/test_admin_input_guards.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_api.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_auto_refresh.py tests/test_billing_usage.py tests/test_chat_admission.py tests/test_discovery_reasons.py tests/test_discovery_root_cause.py tests/test_e2e_dashboard.py tests/test_e2e_full_coverage.py tests/test_e2e_functional.py tests/test_e2e_subprocess.py tests/test_egress_guard_api.py tests/test_egress_guard_dashboard.py tests/test_health_and_jobs.py tests/test_inherited_auth_parent.py tests/test_inherited_auth_v7.py tests/test_keyless_public_bind.py tests/test_mcp_admin_tools.py tests/test_ollama.py tests/test_ollama_off_by_config.py tests/test_ollama_switch_admin.py tests/test_openclaw_removal_api.py tests/test_phase4.py tests/test_plu72_existing_claude_provider.py tests/test_provider_audit_actor.py tests/test_review_fixes.py tests/test_router_admission.py tests/test_secret_unreadable.py tests/test_security_audit_2026_04.py tests/test_security_tnr.py tests/test_smoke_consumers.py tests/test_spec_v7_residuals_parent.py tests/test_write_validation.py`
Expected: tout passe.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/admin_routes/providers.py plugboard/app.py
git commit -m "refactor(admin): move the /providers routes to admin_routes/providers.py (PLU-36)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Module `oauth` — courtier OAuth

**Files:**
- Create: `plugboard/admin_routes/oauth.py`
- Modify: `plugboard/app.py` (bloc 5404–5633)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE`.
- Produces: `oauth.register(app, core) -> SimpleNamespace(oauth_disconnect)`.

- [ ] **Step 1: Créer `oauth.py`** (P1–P4)

Routes, dans l'ordre : `POST /admin/oauth/start` `oauth_start` ; `GET /oauth/callback/{source}` (`include_in_schema=False`, **sans** `_require_admin`, verbatim) `oauth_callback` ; `GET /admin/oauth/device/poll/{state}` `oauth_device_poll` ; `GET /admin/oauth/connections` `oauth_connections` ; `DELETE /admin/oauth/{source}` (204) `oauth_disconnect`.
- Niveau module : `OAuthStartRequest`, `_slugify_for_broker`.
- Dans `register()` : les 5 handlers et `_ensure_broker_provider`.
- Via `core.` : `_require_admin`, `_audit`, `get_provider`, `invalidate_models_cache`.

- [ ] **Step 2: Câbler `app.py`** (P5)

```python
# -- /admin/oauth, /oauth/callback: admin_routes/oauth.py
_oauth = _admin_oauth.register(app, _THIS_MODULE)
oauth_disconnect = _oauth.oauth_disconnect
```

Import : `from plugboard.admin_routes import oauth as _admin_oauth`.

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_csrf_same_site.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_chat_admission.py tests/test_e2e_dashboard.py tests/test_inherited_auth_parent.py tests/test_inherited_auth_v7.py tests/test_oauth_broker.py tests/test_security_audit_2026_04.py tests/test_spec_v7_residuals_parent.py`
Expected: tout passe.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/admin_routes/oauth.py plugboard/app.py
git commit -m "refactor(admin): move the OAuth broker routes to admin_routes/oauth.py (PLU-36)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Module `direct_providers` — fournisseurs directs

**Files:**
- Create: `plugboard/admin_routes/direct_providers.py`
- Modify: `plugboard/app.py` (bloc 5639–6066)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE` ; garde de sortie et `_background_tasks` restés dans `app.py`.
- Produces: `direct_providers.register(app, core) -> SimpleNamespace(_test_openai_endpoint, _schedule_initial_discovery)`.

- [ ] **Step 1: Créer `direct_providers.py`** (P1–P4)

Routes, dans l'ordre : `GET /admin/direct-providers/presets` `list_direct_presets` ; `POST /admin/direct-providers/test` `test_direct_provider` ; `GET /admin/direct-providers/{preset_key}/check` `check_direct_preset` ; `POST /admin/direct-providers/{preset_key}` (201) `create_direct_provider`.
- Niveau module : `DirectProviderCreate`, `DirectProviderTest`, `_TEST_DOCS`, `_docs_link`.
- Dans `register()` : `_test_openai_endpoint` (point de patch : jamais lié au niveau du module, voir P3), les 4 handlers et `_schedule_initial_discovery`.
- Via `core.` : `_require_admin`, `_audit`, `_background_tasks`, `_require_parsable_base_url`, `_require_public_host`, `_require_guard_allows`, `_schedule_initial_discovery` (appel depuis `create_direct_provider`), `_test_openai_endpoint` (appel depuis `test_direct_provider`), `discover_models`, `get_provider`, `invalidate_models_cache`, `update_provider`.
- Reste dans `app.py`, à leur place : `_require_public_host`, `_check_guard_on_edit`, `_require_guard_allows` (garde partagée, sous un commentaire `# -- Egress guard shared by /providers and /admin/direct-providers`), `_background_tasks` (lu aussi par `_aclose_quietly`).

- [ ] **Step 2: Câbler `app.py`** (P5)

```python
# -- /admin/direct-providers: admin_routes/direct_providers.py
_direct_providers = _admin_direct_providers.register(app, _THIS_MODULE)
_test_openai_endpoint = _direct_providers._test_openai_endpoint
_schedule_initial_discovery = _direct_providers._schedule_initial_discovery
```

Import : `from plugboard.admin_routes import direct_providers as _admin_direct_providers`.

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_csrf_same_site.py tests/test_admin_input_guards.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_audit_principal.py tests/test_chat_admission.py tests/test_cli_auth.py tests/test_cli_subprocess.py tests/test_decision_v2_fixes.py tests/test_direct_providers.py tests/test_docs_base_url.py tests/test_e2e_dashboard.py tests/test_e2e_subprocess.py tests/test_egress_guard_api.py tests/test_exposure_and_estimates.py tests/test_models_dev.py tests/test_openclaw_removal_api.py tests/test_review_fixes.py tests/test_security_tnr.py tests/test_spec_v7_residuals_parent.py`
Expected: tout passe.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/admin_routes/direct_providers.py plugboard/app.py
git commit -m "refactor(admin): move the direct-provider routes to admin_routes/direct_providers.py (PLU-36)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Module `ollama` — réglages, bascule et modèles Ollama

**Files:**
- Create: `plugboard/admin_routes/ollama.py`
- Modify: `plugboard/app.py` (bloc 6072–6331)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE`.
- Produces: `ollama.register(app, core) -> SimpleNamespace()` (rien à ré-exporter).

- [ ] **Step 1: Créer `ollama.py`** (P1–P4)

Routes, dans l'ordre : `GET /admin/ollama/settings` `ollama_get_settings` ; `PUT /admin/ollama/settings` `ollama_put_settings` ; `GET /admin/ollama/switch` `ollama_get_switch` ; `PUT /admin/ollama/switch` `ollama_put_switch` ; `GET /admin/ollama/cloud-catalog` `ollama_cloud_catalog` ; `GET /admin/ollama/status` `ollama_status` ; `GET /admin/ollama/models` `ollama_list_models` ; `POST /admin/ollama/models/pull` `ollama_pull_model` ; `DELETE /admin/ollama/models/{name:path}` (204) `ollama_delete_model` ; `GET /admin/ollama/models/{name:path}` `ollama_model_info`.
- Niveau module : `OllamaPullRequest`, `OllamaSettingsUpdate`, `OllamaSwitchUpdate`.
- Dans `register()` : les 10 handlers.
- Via `core.` : `_require_admin`, `_audit`, `get_provider`.
- Le module s'appelle `plugboard.admin_routes.ollama` : les imports locaux existants (`from plugboard import ollama_admin`, `plugboard.ollama`…) restent absolus et verbatim.

- [ ] **Step 2: Câbler `app.py`** (P5)

```python
# -- /admin/ollama: admin_routes/ollama.py
_admin_ollama.register(app, _THIS_MODULE)
```

Import : `from plugboard.admin_routes import ollama as _admin_ollama`.

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_csrf_same_site.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_chat_admission.py tests/test_e2e_dashboard.py tests/test_ollama.py tests/test_ollama_downloads_guard.py tests/test_ollama_off_by_config.py tests/test_ollama_switch_admin.py tests/test_ollama_switch_dashboard.py tests/test_ollama_switch_e2e.py tests/test_spec_v7_residuals_parent.py`
Expected: tout passe.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/admin_routes/ollama.py plugboard/app.py
git commit -m "refactor(admin): move the Ollama admin routes to admin_routes/ollama.py (PLU-36)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Module `keys` — clés client et organisations

**Files:**
- Create: `plugboard/admin_routes/keys.py`
- Modify: `plugboard/app.py` (bloc 6337–6576)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE`.
- Produces: `keys.register(app, core) -> SimpleNamespace(_default_rpm_limit, _default_monthly_limit_usd)`.

- [ ] **Step 1: Créer `keys.py`** (P1–P4)

Routes, dans l'ordre : `GET /admin/keys` `list_api_keys` ; `POST /admin/keys` (201) `create_api_key` ; `GET /admin/keys/client/{client_id}` `list_keys_by_client` ; `POST /admin/keys/{key_id}/revoke` `revoke_api_key` ; `DELETE /admin/keys/{key_id}` (204) `delete_api_key` ; `POST /admin/keys/{key_id}/rotate` (201) `rotate_api_key` ; `GET /admin/orgs` `list_orgs` ; `GET /admin/orgs/{org_id}/keys` `list_org_keys` ; `GET /admin/orgs/{org_id}/usage/{month}` `org_usage`.
- Niveau module : `KeyCreate`, `_default_rpm_limit`, `_default_monthly_limit_usd`, `_admin_key_view`.
- Dans `register()` : les 9 handlers et `_allowed_model_refusal`.
- Via `core.` : `_require_admin`, `_audit`, `_month_param`, `_usage`, `load_providers`.

- [ ] **Step 2: Câbler `app.py`** (P5)

```python
# -- /admin/keys, /admin/orgs: admin_routes/keys.py
_keys = _admin_keys.register(app, _THIS_MODULE)
_default_rpm_limit = _keys._default_rpm_limit
_default_monthly_limit_usd = _keys._default_monthly_limit_usd
```

Import : `from plugboard.admin_routes import keys as _admin_keys`.

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_csrf_same_site.py tests/test_admin_input_guards.py tests/test_admin_people_login.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_audit_principal.py tests/test_auth_residuals_v7b.py tests/test_billing_usage.py tests/test_chat_admission.py tests/test_decision_api.py tests/test_e2e_dashboard.py tests/test_e2e_full_coverage.py tests/test_e2e_functional.py tests/test_env_numbers_at_start.py tests/test_final_audit_parent.py tests/test_final_audit_v9.py tests/test_inherited_auth_parent.py tests/test_inherited_auth_v7.py tests/test_live_requests.py tests/test_monitoring.py tests/test_ratelimit_identity.py tests/test_security_audit_2026_04.py tests/test_security_tnr.py tests/test_session_logout_all.py tests/test_spec_v7_residuals_parent.py tests/test_write_validation.py`
Expected: tout passe.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/admin_routes/keys.py plugboard/app.py
git commit -m "refactor(admin): move the client key and org routes to admin_routes/keys.py (PLU-36)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Module `jobs` — tâches planifiées

**Files:**
- Create: `plugboard/admin_routes/jobs.py`
- Modify: `plugboard/app.py` (bloc 6582–6617)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE`.
- Produces: `jobs.register(app, core) -> SimpleNamespace()`.

- [ ] **Step 1: Créer `jobs.py`** (P1–P4)

Routes, dans l'ordre : `GET /admin/jobs` `list_jobs` ; `GET /admin/jobs/{job_id}/log` `job_log` ; `POST /admin/jobs/{job_id}/run` `run_job_endpoint`. Niveau module : rien. Dans `register()` : les 3 handlers. Via `core.` : `_require_admin`, `_audit`.

- [ ] **Step 2: Câbler `app.py`** (P5)

```python
# -- /admin/jobs: admin_routes/jobs.py
_admin_jobs.register(app, _THIS_MODULE)
```

Import : `from plugboard.admin_routes import jobs as _admin_jobs`.

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_csrf_same_site.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_chat_admission.py tests/test_e2e_dashboard.py tests/test_e2e_full_coverage.py tests/test_health_and_jobs.py tests/test_recette_services.py tests/test_spec_v7_residuals_parent.py`
Expected: tout passe.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/admin_routes/jobs.py plugboard/app.py
git commit -m "refactor(admin): move the job routes to admin_routes/jobs.py (PLU-36)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Module `pricing` — grilles, suggestions, planificateurs, mise à jour du catalogue, couverture

**Files:**
- Create: `plugboard/admin_routes/pricing.py`
- Modify: `plugboard/app.py` (bloc 6623–7117)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE`.
- Produces: `pricing.register(app, core) -> SimpleNamespace(_suggestion_action, admin_catalog_history)`.

- [ ] **Step 1: Créer `pricing.py`** (P1–P4)

Routes, dans l'ordre (l'ordre compte : `PUT /admin/pricing/{month}` est la dernière) : `GET /admin/pricing/llm/status` `llm_pricing_status` ; `POST /admin/pricing/llm/run` `llm_pricing_run` ; `GET /admin/pricing/suggestions` `list_pricing_suggestions` ; `POST /admin/pricing/suggestions/apply` `apply_pricing_suggestions` ; `POST /admin/pricing/suggestions/reject` `reject_pricing_suggestions` ; `POST /admin/pricing/suggestions/{sid}/apply` `apply_pricing_suggestion` ; `POST /admin/pricing/suggestions/{sid}/reject` `reject_pricing_suggestion` ; `PATCH /admin/pricing/{month}` `patch_pricing` ; `POST /admin/pricing/{month}/revert` `revert_pricing` ; `GET /admin/pricing/{month}` `get_pricing` ; `POST /admin/pricing/scrape` `scrape_pricing` ; `GET /admin/pricing/scheduler/status` `pricing_scheduler_status` ; `GET /admin/models/scheduler/status` `models_scheduler_status` ; `GET /admin/auto-update/status` `auto_update_status` ; `GET /admin/catalog/history` `admin_catalog_history` (**`def`**, pas `async def`) ; `POST /admin/auto-update/run` (202) `auto_update_run` ; `GET /admin/coverage/pricing` `pricing_coverage` ; `GET /admin/coverage/benchmarks` `benchmarks_coverage` ; `PUT /admin/pricing/{month}` `upsert_pricing`.
- Niveau module : `PricingUpsert`, `PricingPatch`, `PricingRevert`, `SuggestionIds`, `_load_grid_for_edit`, `_manual_rows`.
- Dans `register()` : les 19 handlers, `_check_month`, `_suggestion_action`, `_bulk_suggestions`.
- Via `core.` : `_require_admin`, `_audit`, `_MONTH_RE`, `_background_tasks`, `_month_param`, `list_models`, `load_providers`.

- [ ] **Step 2: Câbler `app.py`** (P5)

```python
# -- /admin/pricing, schedulers, auto-update, catalog history, coverage:
#    admin_routes/pricing.py
_pricing = _admin_pricing.register(app, _THIS_MODULE)
_suggestion_action = _pricing._suggestion_action
admin_catalog_history = _pricing.admin_catalog_history
```

Import : `from plugboard.admin_routes import pricing as _admin_pricing`.

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_csrf_same_site.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_audit_principal.py tests/test_auto_refresh.py tests/test_billing_usage.py tests/test_catalog_history.py tests/test_chat_admission.py tests/test_e2e_dashboard.py tests/test_e2e_full_coverage.py tests/test_e2e_functional.py tests/test_llm_pricing.py tests/test_month_param.py tests/test_price_bench_coverage.py tests/test_pricing_merge.py tests/test_pricing_scheduler.py tests/test_provider_pricing.py tests/test_review_fixes.py tests/test_security_tnr.py tests/test_spec_v7_residuals_parent.py`
Expected: tout passe.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/admin_routes/pricing.py plugboard/app.py
git commit -m "refactor(admin): move the pricing and catalogue upkeep routes to admin_routes/pricing.py (PLU-36)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Module `usage` — consommation et rétention

**Files:**
- Create: `plugboard/admin_routes/usage.py`
- Modify: `plugboard/app.py` (bloc 7123–7229)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE`.
- Produces: `usage.register(app, core) -> SimpleNamespace()`.

- [ ] **Step 1: Créer `usage.py`** (P1–P4)

Routes, dans l'ordre (l'ordre compte : `/{month}/export` et `/{month}/key/{key_id}` avant `/{month}/{client_id}`) : `GET /admin/usage/{month}` `admin_usage_month` ; `DELETE /admin/usage/{month}` (204) `delete_usage_month` ; `GET /admin/usage/{month}/export` `export_usage` ; `GET /admin/usage/{month}/key/{key_id}` `admin_usage_by_key` ; `POST /admin/usage/purge` `purge_old_usage` ; `GET /admin/retention` `admin_retention` ; `POST /admin/usage/migrate` `migrate_usage` ; `GET /admin/usage/{month}/{client_id}` `admin_usage_client`. Niveau module : rien. Dans `register()` : les 8 handlers. Via `core.` : `_require_admin`, `_month_param`, `_usage`.

- [ ] **Step 2: Câbler `app.py`** (P5)

```python
# -- /admin/usage, /admin/retention: admin_routes/usage.py
_admin_usage.register(app, _THIS_MODULE)
```

Import : `from plugboard.admin_routes import usage as _admin_usage`.

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_csrf_same_site.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_billing_usage.py tests/test_chat_admission.py tests/test_e2e_full_coverage.py tests/test_e2e_functional.py tests/test_maintenance_wiring.py tests/test_month_param.py tests/test_retention.py tests/test_security_tnr.py tests/test_spec_v7_residuals_parent.py tests/test_usage_purge_route.py`
Expected: tout passe.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/admin_routes/usage.py plugboard/app.py
git commit -m "refactor(admin): move the usage and retention routes to admin_routes/usage.py (PLU-36)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Module `auth` — connexion, déconnexion et MFA de l'admin

**Files:**
- Create: `plugboard/admin_routes/auth.py`
- Modify: `plugboard/app.py` (bloc 7598–8078 ; `_LOGIN_HTML_TEMPLATE` 7289–7362 ; `AdminLogin`, `AdminMfaSetupRequest`, `AdminMfaVerify` 7365–7381 ; `_safe_next` 8130–8143)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE` ; cœur d'authentification et aides de connexion automatique restés dans `app.py`.
- Produces: `auth.register(app, core) -> SimpleNamespace(_LOGIN_HTML_TEMPLATE)`.

- [ ] **Step 1: Créer `auth.py`** (P1–P4)

Routes, dans l'ordre : `GET /login` `login_page` ; `POST /auth/admin/login` `admin_login` ; `POST /auth/admin/logout` (204) `admin_logout` ; `POST /auth/admin/logout-all` (204) `admin_logout_all` ; `GET /admin/mfa/status` `admin_mfa_status` ; `POST /admin/mfa/begin` `admin_mfa_begin` ; `POST /admin/mfa/setup` (204) `admin_mfa_setup` ; `POST /admin/mfa/verify` (204) `admin_mfa_verify` ; `DELETE /admin/mfa` (204) `admin_mfa_disable` — toutes `include_in_schema=False`, verbatim.
- Niveau module : `_LOGIN_HTML_TEMPLATE`, `AdminLogin`, `AdminMfaSetupRequest`, `AdminMfaVerify`, `_safe_next`.
- Dans `register()` : les 9 handlers, `_person_login`, `_session_response`, `_session_record_author`, `_refuse_person_principal`.
- Via `core.` : `_require_admin`, `_audit`, `_ADMIN_COOKIE_NAME`, `_ADMIN_KEY_PRINCIPAL`, `_ADMIN_SCOPE`, `_ADMIN_SESSION_MAX_AGE`, `_LOOPBACK_HOSTS`, `_SUSPICIOUS_REFUSALS`, `_address_scope`, `_admin_session_nonce`, `_admin_session_principal`, `_auto_login_refusal`, `_client_address`, `_create_admin_session_token`, `_effective_admin_key`, `_host_header_name`, `_is_admin_key`, `_local_login_proof`, `_person_principal`, `_person_record_is_sound`, `_person_scope`, `_prove_person_code`, `_require_mfa_step_up`, `_set_principal`, `_should_audit_refusal`, `_should_use_secure_cookie`.
- Reste dans `app.py`, à sa place : tout le bloc 7405–7595 (connexion automatique : état, constantes patchées, ligne de journal émise à l'import), `_check_dashboard_auth`, `_is_safe_static_path`, `_STATIC_DIR*`, `_PUBLIC_DASHBOARD_HTML`, et les routes `/`, `/portal`, `/dashboard*`, `/suivi`.

- [ ] **Step 2: Câbler `app.py`** (P5)

```python
# -- /login, /auth/admin/*, /admin/mfa: admin_routes/auth.py
_auth = _admin_auth.register(app, _THIS_MODULE)
_LOGIN_HTML_TEMPLATE = _auth._LOGIN_HTML_TEMPLATE
```

Import : `from plugboard.admin_routes import auth as _admin_auth`.

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_csrf_same_site.py tests/test_admin_people_api.py tests/test_admin_people_auth.py tests/test_admin_people_dashboard.py tests/test_admin_people_login.py tests/test_admin_people_mfa_routes.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_admin_session_edges.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_audit_principal.py tests/test_auth_auto_login.py tests/test_auth_residuals_v7b.py tests/test_catalog_history.py tests/test_chat_admission.py tests/test_e2e_dashboard.py tests/test_e2e_full_coverage.py tests/test_e2e_subprocess.py tests/test_final_audit_v9.py tests/test_inherited_auth_parent.py tests/test_inherited_auth_v7.py tests/test_login_mobile_sol.py tests/test_monitoring.py tests/test_proxy_client_ip.py tests/test_ratelimit_identity.py tests/test_readiness_visibility.py tests/test_security_audit_2026_04.py tests/test_security_hardening.py tests/test_session_logout_all.py tests/test_spec_v7_residuals_parent.py tests/test_suivi_api.py`
Expected: tout passe.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/admin_routes/auth.py plugboard/app.py
git commit -m "refactor(admin): move the admin sign-in, sign-out and MFA routes to admin_routes/auth.py (PLU-36)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Clôture — documentation, mesures et contrôles du lot

**Files:**
- Modify: `plugboard/app.py` (docstring de tête seulement), `docs/ARCHITECTURE.md`, `CHANGELOG.md`

**Interfaces:**
- Consumes: tâches 1 à 12.
- Produces: rien pour le code.

- [ ] **Step 1: Documentation**

- `plugboard/app.py`, docstring de tête : une ligne après la liste des routes : « Admin routes are declared by `plugboard/admin_routes/` (one module per area, registered in place by `register(app, core)`, PLU-36). »
- `docs/ARCHITECTURE.md`, tableau « Modules » : ligne `app.py` → « HTTP API, authentication, limits, caching and billing glue, dashboard and login pages ; registers the admin routes » ; nouvelle ligne `admin_routes/` → « Admin endpoints, one module per area (model probes, monitoring, providers, OAuth, direct providers, Ollama, keys, jobs, pricing, usage, sign-in and MFA), each declared on the app by `register(app, core)` ».
- `CHANGELOG.md`, sous `## [Unreleased]`, section `### Changed` (la créer après `### Added` si absente) : « **Admin routes moved out of `app.py`** (PLU-36): one module per area under `plugboard/admin_routes/`; routes, answers and the admin OpenAPI document are unchanged. »

- [ ] **Step 2: Contrôles du lot**

Run, depuis `/srv/dev/worktrees/plugboard/plu36` :
- `wc -l plugboard/app.py` → Expected: entre 4 750 et 4 950 (≈ 4 850).
- `git diff --name-status 672118e -- tests/ | grep -v '^A'` → Expected: aucune sortie.
- `git diff --exit-code $(git log --format=%H --grep='freeze the route table' -1) -- tests/route_snapshot.py tests/test_admin_routes_guard.py tests/fixtures/admin_routes_snapshot.json` → Expected: code 0.
- `grep -n 'CONTRACT_VERSION = ' plugboard/api_contract.py` → Expected: `"3.0.1"`.
- `T=$(mktemp -d) && PLUGBOARD_DATA=$T PLUGBOARD_SKIP_DOTENV=1 $PY scripts/export_contract.py --check && PLUGBOARD_DATA=$T PLUGBOARD_SKIP_DOTENV=1 $PY scripts/export_contract.py --mcp --check; rm -rf "$T"` → Expected: `docs/api/openapi.json: up to date`, `docs/api/mcp.json: up to date`.
- Temps d'import : la commande de la tâche 1, step 9, 5 fois → Expected: médiane ≤ référence + 10 %.

- [ ] **Step 3: Tests ciblés, lint**

Run: `$PYTEST tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_api_contract.py tests/test_cli_versions_single_source.py tests/test_docs_admin_people.py tests/test_docs_release_4143.py tests/test_docs_release_4144.py tests/test_docs_release_4145.py tests/test_docs_release_4146.py tests/test_no_remote_ci.py tests/test_plu72_existing_claude_provider.py` puis `$RUFF check plugboard/ tests/` et `$RUFF format --check plugboard/ tests/`.
Expected: tout passe ; 0 erreur.

- [ ] **Step 4: Commit**

```bash
git add plugboard/app.py docs/ARCHITECTURE.md CHANGELOG.md
git commit -m "docs(admin): say where the admin routes live (PLU-36)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Jalon — validation complète unique, puis une CI

À lancer une fois, sur le dernier commit du lot, depuis `/srv/dev/worktrees/plugboard/plu36`.

1. **Charge** : `uptime` (charge < 10), `free -g`, `ci etat` (aucune CI `plugboard` en cours). Les quatre blocs Python comptent pour une suite lourde, le couloir e2e pour la seconde (au plus deux).
2. **Lancement simultané** :

```bash
cd /srv/dev/worktrees/plugboard/plu36
PY=/srv/dev/plugboard/.venv/bin/python; RUFF=/srv/dev/plugboard/.venv/bin/ruff
J=$(mktemp -d)
find tests -maxdepth 1 -name 'test_*.py' | sort > "$J/files"
split -n l/4 -d "$J/files" "$J/bloc_"            # 4 blocs contigus, ordre conservé
printf 'tests/integration\ntests/live_model_tests\n' >> "$J/bloc_03"
for b in 00 01 02 03; do
  mkdir -p "$J/data_$b"
  ( PLUGBOARD_DATA="$J/data_$b" PLUGBOARD_SKIP_DOTENV=1 \
      "$PY" -m pytest -q -p no:cacheprovider $(cat "$J/bloc_$b") \
      > "$J/pytest_$b.log" 2>&1; echo $? > "$J/rc_$b" ) &
done
( "$RUFF" check plugboard/ tests/ && "$RUFF" format --check plugboard/ tests/ ) \
  > "$J/ruff.log" 2>&1; echo $? > "$J/rc_ruff" &
( mkdir -p "$J/data_contract" && export PLUGBOARD_DATA="$J/data_contract" PLUGBOARD_SKIP_DOTENV=1 \
  && "$PY" scripts/export_contract.py --check && "$PY" scripts/export_contract.py --mcp --check ) \
  > "$J/contract.log" 2>&1; echo $? > "$J/rc_contract" &
# Couloir e2e isolé (Playwright n'est pas dans .venv) : son venv, son PLUGBOARD_DATA,
# un port libre choisi par tests/e2e/conftest.py.
( uv venv --quiet --python 3.12 "$J/e2e-venv" \
  && uv pip install --quiet --python "$J/e2e-venv/bin/python" --require-hashes -r requirements-dev.lock \
  && uv pip install --quiet --python "$J/e2e-venv/bin/python" playwright==1.56.0 \
  && mkdir -p "$J/data_e2e" \
  && PLUGBOARD_DATA="$J/data_e2e" PLUGBOARD_SKIP_DOTENV=1 PLUGBOARD_RUN_E2E=1 \
     "$J/e2e-venv/bin/python" -m pytest tests/e2e -q -p no:cacheprovider ) \
  > "$J/e2e.log" 2>&1; echo $? > "$J/rc_e2e" &
wait
grep -H . "$J"/rc_*                                # tous à 0
tail -n 3 "$J"/pytest_*.log "$J"/e2e.log
```

   Expected : chaque `rc_*` vaut `0` ; aucun `failed` ni `error` dans les journaux. Si Chromium manque au couloir e2e : `"$J/e2e-venv/bin/python" -m playwright install chromium` puis relancer ce seul couloir.
3. **En cas d'échec** : corriger, puis rejouer **seulement** les nœuds en échec (`$PYTEST <fichier>::<test>` relevés dans le journal du bloc), la commande ruff ou le couloir concerné ; jamais un test assoupli ou sauté.
4. **Une seule CI**, une fois tout vert : `ci lancer` (depuis l'atelier, arbre exact du dernier commit), suivi par `ci etat` et `ci journal plugboard <sha>`. Rouge par le produit : `board bug ajouter "…"`, corriger, rejouer la partie fautive, puis relancer la CI une fois.
5. **Nettoyage** : `rm -rf "$J"` ; rien d'autre à garder.
6. **Board** : `board ticket statut PLU-36 revue` (depuis `/srv/dev/worktrees/plugboard/pilotage-board`) avec le SHA et le lien du rapport CI.

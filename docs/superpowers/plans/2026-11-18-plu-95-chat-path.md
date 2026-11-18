# Chemin du chat hors de `app.py` (PLU-95) — plan d'exécution

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sortir de `plugboard/app.py` le chemin du chat (13 routes client et leurs aides : budget, prix, facturation, réponses d'erreur, flux SSE, ≈ 2 340 lignes) vers huit modules de `plugboard/chat_path/`, sans aucun changement de comportement ; `app.py` passe de 4 827 à ≈ 2 520 lignes.

**Architecture:** Le contrat de PLU-36 : chaque module expose `register(app: FastAPI, core: ModuleType) -> SimpleNamespace` ; ses routes y sont déclarées avec `@app.<méthode>` (aucun `include_router`), et toute aide qui lit un point de patch ou un nom resté dans `app.py` y est une fermeture qui lit `core.<nom>` à l'appel. Le code pur reste au niveau du module. `app.py` appelle `register(app, _THIS_MODULE)` à la place de chaque bloc et ré-exporte les noms lus sur `plugboard.app`. Une garde écrite d'abord fige les routes client et leur place, le contrat client, les gestionnaires d'exception et les middlewares de `c3e4cea` ; la garde PLU-36 continue de figer la table complète, l'OpenAPI admin et les effets d'import.

**Tech Stack:** Python 3.12, FastAPI 0.143.3 / Starlette 1.8, pytest (`asyncio_mode = auto`), httpx `ASGITransport`, ruff.

**Spec:** `docs/superpowers/specs/2026-11-18-plu-95-chat-path-design.md` (fait foi : règles R2 étendue, R3, R10–R12, tableau des points de patch, questions Q1–Q3). Les règles R1–R9 sont celles de `docs/superpowers/specs/2026-11-18-plu-36-admin-routes-design.md`.

**Décisions (D-69) :** Q1 (une ligne de `tests/route_snapshot.py`), Q2 (tâche 9) et Q3 (cible ≈ 2 520, cœur d'authentification dans un autre ticket) décidées comme recommandé ; les variantes « Q1/Q2 = non » des tâches sont sans objet. Ruling R-P1 du préflight : `_ClientGone` et `_unless_client_gone` restent ré-exportés en permanence (lus par `tests/test_inflight_disconnect.py`) ; les tâches 7 et 10 ne les retirent ni ne les cherchent.

## Global Constraints

- Atelier : `/srv/dev/worktrees/plugboard/plu95`, branche `refactor/plu-95-chat-path` (base `c3e4cea`). Rien hors de l'atelier. Interdits : `git commit --amend`, rebase, reset, push. Un commit **nouveau** par tâche, Conventional Commits, message terminé par `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Comportement inchangé : mêmes routes, même ordre, mêmes dépendances, mêmes réponses, mêmes OpenAPI (admin et client), mêmes gestionnaires d'exception et middlewares, mêmes effets d'import.
- **Aucun test existant modifié, assoupli, sauté ni supprimé**, sauf la ligne de `tests/route_snapshot.py` de la tâche 1 (Q1). Contrôle : `git diff --name-status c3e4cea -- tests/ | grep -v '^A'` ne sort que `M	tests/route_snapshot.py`.
- Après la tâche 1, ne changent plus : `tests/route_snapshot.py`, `tests/chat_path_snapshot.py`, `tests/test_chat_path_guard.py`, `tests/fixtures/chat_path_snapshot.json`, `tests/fixtures/admin_routes_snapshot.json`, `tests/test_admin_routes_guard.py`. `tests/test_chat_path_split.py` ne change qu'à la tâche 10 (une assertion). Les deux gardes restent vertes à chaque tâche.
- `CONTRACT_VERSION` reste `"3.0.1"` ; `git diff c3e4cea -- docs/api/ plugboard/api_contract.py` reste vide.
- Tests ciblés seulement pendant le lot (règle YB du 18/11) ; une seule validation complète, à la section « Jalon », hors des tâches. Commandes :
  - `PY=/srv/dev/plugboard/.venv/bin/python` ; `RUFF=/srv/dev/plugboard/.venv/bin/ruff`
  - `PYTEST="env PLUGBOARD_SKIP_DOTENV=1 $PY -m pytest -q -p no:cacheprovider"` (le `tests/conftest.py` donne à chaque processus son propre `PLUGBOARD_DATA` temporaire)
  - Les listes de fichiers sont **triées** et contiennent toujours `tests/test_audit_e2e.py` (sa réimportation de `plugboard.app` au même rang que dans la suite complète) et les deux gardes.
- Lint de chaque tâche : `$RUFF check plugboard/ tests/` puis `$RUFF format --check plugboard/ tests/` (la CI exige les deux).
- Nettoyage après chaque passe : rien à garder dans `/tmp` ; supprimer les dossiers temporaires créés à la main.

## Review Focus

- **Réimportation de `plugboard.app`** (E-25) : une fermeture du premier module lit les points de patch du premier module. Épinglé par les trois tests de caractérisation de la tâche 1 (`_billing_target`, `_check_model_access`, `POST /v1/chat/completions`).
- **Point de patch lu dans son propre module** : `_check_model_access` et `_model_is_compatible` appellent `core._model_route_guard` ; `chat_completions` appelle `core._record_session_turn` ; `_charge_answer` appelle `core._send_spending_alert`. Un appel direct passerait les tests du module et casserait les patchs des tests (`test_model_access_security.py`, `test_budget_holds.py`…).
- **`_stream_and_bill`** reste imbriqué dans `chat_completions`, verbatim ; ses appels à `_bill_streaming`, `_send_spending_alert`, `_record_session_turn` et `_aclose_quietly` passent par `core.`.
- **Classes d'exception restées dans `app.py`** : le code déplacé lève et attrape `core._UsageUnavailable`, `core._ModelNotAllowed`, `core._InvalidChatRequest`, `core._SessionBusy`, `core._ChatOverloaded` ; leurs gestionnaires et leur ordre sont figés par la garde.
- **Lecteurs de production** : `decision_api.py` et `embeddings_api.py` importent `_spending_scope`, `_check_spending_limit` et `_quota_error_response` de `plugboard.app` à l'appel ; ces noms restent ré-exportés.
- **Couverture miroir** : `test_chat_usage_is_priced_and_written_in_one_place` ne lit que `app.py` et `router.py` ; `test_chat_path_prices_and_writes_usage_only_through_chat_billing` refait la vérification sur le paquet.

---

## Structure des fichiers

| Fichier | Rôle | Tâche |
|---|---|---|
| `tests/route_snapshot.py` | Sonde des effets d'import : ignore aussi `plugboard.chat_path*` (Q1, une ligne) | 1 |
| `tests/chat_path_snapshot.py` | Calcule routes client, contrat client, gestionnaires d'exception, middlewares ; écrit le gabarit (`python -m tests.chat_path_snapshot`) | 1 |
| `tests/fixtures/chat_path_snapshot.json` | Gabarit figé sur `c3e4cea` | 1 |
| `tests/test_chat_path_guard.py` | Compare le présent au gabarit | 1 |
| `tests/test_chat_path_split.py` | Invariants du paquet, couverture miroir, trois tests de réimportation | 1, 10 |
| `plugboard/chat_path/__init__.py` | Paquet, docstring du contrat (n'importe aucun sous-module) | 2 |
| `plugboard/chat_path/streaming.py` … `account.py` | Un module par sujet | 2–9 |
| `plugboard/app.py` | Blocs remplacés par `register()` ; ré-exports ; imports inutiles retirés | 2–10 |
| `docs/ARCHITECTURE.md`, `CHANGELOG.md` | Où vit le chemin du chat ; la nouvelle garde | 10 |

## Procédure commune de déplacement (tâches 2 à 9)

Chaque tâche de module applique ces étapes ; la tâche donne ce qui lui est propre. Les numéros de ligne sont ceux de `c3e4cea`, pour s'orienter : repérer les blocs par leurs noms.

- **P1 — En-tête du module** : docstring (sujet, « moved verbatim from `plugboard/app.py` (PLU-95), behaviour unchanged », rappel que `core` est le module `plugboard.app` qui enregistre et que les points de patch et les noms restés dans `app.py` se lisent par `core.`), `from __future__ import annotations`, imports, `log = logging.getLogger("plugboard.app")` si le module journalise.
- **P2 — Niveau module** : les éléments listés « niveau module », texte verbatim. Tout nom employé dans une annotation de handler (`Request`, `Optional`, modèles Pydantic) est une globale du module (R5 de PLU-36).
- **P3 — `register()`** : `def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:` puis, dans l'ordre du fichier d'origine, les fermetures listées (décorateur, signature, docstring et corps verbatim), avec `Depends(core.<dépendance>)` et `core.<nom>` pour chaque nom de la ligne « via `core.` », **y compris** un point de patch défini dans le même `register()` (R3). Fin : `return SimpleNamespace(<fermetures ré-exportées>)`.
- **P4 — Imports du module** : tout autre nom libre vient d'un import de même forme que dans `app.py` (R6) ; un nom de niveau module d'un autre module du paquet s'importe `from plugboard.chat_path.<module> import <nom>` (R10) ; jamais `plugboard.app`, `sys.modules`, `import_module`, ni import relatif. N'importer au niveau module que des modules qu'`app.py` importe déjà au niveau module (R11) ; les imports locaux des fonctions restent locaux.
- **P5 — `app.py`** : supprimer les éléments déplacés ; laisser en place ceux que la tâche liste « reste » ; à l'endroit indiqué, écrire le câblage donné par la tâche ; ajouter le module au bloc `from plugboard.chat_path import (…)` (créé tâche 2, **dernier** import de tête, juste avant `setup_logging()`), noms triés ; retirer les ré-exports provisoires que la tâche nomme ; retirer les imports devenus inutiles (`ruff` F401), **sauf** un point de patch (`PATCH_POINTS` de `tests/test_admin_routes_split.py`), gardé avec `# noqa: F401  (patch point read through core, PLU-95)`.
- **P6 — Lint** : `$RUFF format plugboard/chat_path/ plugboard/app.py`, puis `$RUFF check plugboard/ tests/` et `$RUFF format --check plugboard/ tests/` : 0 erreur (F821 signale tout nom oublié).
- **P7 — Relecture du déplacement** : `git diff --color-moved=dimmed-zebra --color-moved-ws=allow-indentation-change -- plugboard/` ; les seules lignes non grisées sont les `core.`, le câblage, les imports, les ré-exports et le reformatage.

---

### Task 1: Garde — routes client, contrat client, gestionnaires d'exception et middlewares figés

**Files:**
- Modify: `tests/route_snapshot.py` (une ligne de `_PROBE`, Q1)
- Create: `tests/chat_path_snapshot.py`, `tests/test_chat_path_guard.py`, `tests/test_chat_path_split.py`, `tests/fixtures/chat_path_snapshot.json` (généré)

**Interfaces:**
- Consumes: `plugboard.app.app` tel qu'à `c3e4cea` ; `route_table`, `_type_name` de `tests/route_snapshot.py` ; `PATCH_POINTS`, `_app_imports` de `tests/test_admin_routes_split.py`.
- Produces: `client_routes(app)`, `client_contract()`, `exception_handlers(app)`, `middleware(app)`, `FIXTURE` dans `tests/chat_path_snapshot.py` ; `EXPECTED_MODULES` dans `tests/test_chat_path_split.py`.

- [ ] **Step 1: Ligne de base**

Run: `git -C /srv/dev/worktrees/plugboard/plu95 diff --quiet c3e4cea -- plugboard/ tests/ && cd /srv/dev/worktrees/plugboard/plu95 && $PYTEST tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_chat_admission.py tests/test_chat_errors_and_billing.py tests/test_spec_v7_residuals_parent.py`
Expected: tout passe (sinon s'arrêter : la base n'est pas saine).

- [ ] **Step 2: La sonde de la garde PLU-36 ignore le nouveau paquet (Q1)**

Dans `_PROBE` de `tests/route_snapshot.py`, remplacer
`if name.startswith("plugboard") and not name.startswith("plugboard.admin_routes")`
par
`if name.startswith("plugboard") and not name.startswith(("plugboard.admin_routes", "plugboard.chat_path"))`
(ruff format peut couper la ligne ; aucun autre changement, gabarit non régénéré).

Run: `$PYTEST tests/test_admin_routes_guard.py` — Expected: 3 passed.

*Variante Q1 = non :* sauter ce step ; chaque tâche 2 à 9 régénère alors `tests/fixtures/admin_routes_snapshot.json` (`$PY -m tests.route_snapshot`) après le câblage et vérifie que `git diff` du gabarit n'ajoute que des lignes `plugboard.chat_path…` dans `import_effects.modules` ; la contrainte « ne change plus » ne vaut plus pour ce gabarit.

- [ ] **Step 3: Écrire `tests/chat_path_snapshot.py`**

```python
"""Snapshot of what moving the chat path out of app.py must not change (PLU-95).

The client routes of the route table with their place, the client API
contract, the exception handlers and the middleware stack are frozen in
``tests/fixtures/chat_path_snapshot.json`` and compared by
``tests/test_chat_path_guard.py``. The fixture is written once on the code
before the move, with ``python -m tests.chat_path_snapshot``. The whole route
table, the admin OpenAPI document and the import effects stay frozen by the
PLU-36 guard (``tests/route_snapshot.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

from tests import route_snapshot

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "chat_path_snapshot.json"
CLIENT_PREFIXES = ("/v1/", "/v2/", "/health", "/usage/")
ALSO_FROZEN = frozenset({"/admin/inflight"})  # declared among the client routes


def _paths(row: dict) -> list:
    if row["kind"] == "included_router":
        return [p for inner in row["routes"] for p in _paths(inner)]
    return [row.get("path") or ""]


def client_routes(app) -> list:
    """[place in app.routes, row] of every entry that serves a client path."""
    return [
        [place, row]
        for place, row in enumerate(route_snapshot.route_table(app))
        if any(p.startswith(CLIENT_PREFIXES) or p in ALSO_FROZEN for p in _paths(row))
    ]


def client_contract() -> dict:
    """The client API contract, version included (CONTRACT_VERSION)."""
    from plugboard.api_contract import build_openapi

    return json.loads(json.dumps(build_openapi(), sort_keys=True))


def _qualified(obj) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


def exception_handlers(app) -> list:
    """[exception class or status code, handler], in registration order."""
    return [
        [key if isinstance(key, int) else _qualified(key), _qualified(handler)]
        for key, handler in app.exception_handlers.items()
    ]


def middleware(app) -> list:
    """The user middleware stack as Starlette keeps it (outermost first)."""
    return [
        [
            _qualified(m.cls),
            json.loads(json.dumps([list(m.args), m.kwargs], sort_keys=True, default=repr)),
        ]
        for m in app.user_middleware
    ]


def main() -> None:
    import tests.conftest  # noqa: F401  (same environment as the suite)
    from plugboard.app import app

    snapshot = {
        "client_routes": client_routes(app),
        "client_contract": client_contract(),
        "exception_handlers": exception_handlers(app),
        "middleware": middleware(app),
    }
    FIXTURE.write_text(
        json.dumps(snapshot, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Écrire `tests/test_chat_path_guard.py`**

```python
"""Moving the chat path out of app.py changes nothing (PLU-95): same client
routes in the same places, same client contract, same exception handlers and
middleware stack as c3e4cea."""

import json

from tests import chat_path_snapshot


def _expected(key):
    return json.loads(chat_path_snapshot.FIXTURE.read_text(encoding="utf-8"))[key]


def test_client_routes_are_unchanged():
    from plugboard.app import app

    assert chat_path_snapshot.client_routes(app) == _expected("client_routes")


def test_client_contract_is_unchanged():
    assert chat_path_snapshot.client_contract() == _expected("client_contract")


def test_exception_handlers_are_unchanged():
    from plugboard.app import app

    assert chat_path_snapshot.exception_handlers(app) == _expected("exception_handlers")


def test_middleware_stack_is_unchanged():
    from plugboard.app import app

    assert chat_path_snapshot.middleware(app) == _expected("middleware")
```

- [ ] **Step 5: Lancer la garde sans gabarit**

Run: `$PYTEST tests/test_chat_path_guard.py`
Expected: 4 FAIL, `FileNotFoundError` sur `chat_path_snapshot.json`.

- [ ] **Step 6: Écrire le gabarit sur le code actuel**

Run: `git -C /srv/dev/worktrees/plugboard/plu95 diff --quiet c3e4cea -- plugboard/ && cd /srv/dev/worktrees/plugboard/plu95 && $PY -m tests.chat_path_snapshot`
Expected: `tests/fixtures/chat_path_snapshot.json` créé ; `client_routes` a 19 entrées (places 0 et 1 : routeurs `decision` et `embeddings` ; 2 à 17 : de `/v1/chat/completions` à `/v1/me/keys` ; 102 : `/v1/openapi.json`) ; `client_contract.info.version` = `"3.0.1"`, 19 chemins ; `exception_handlers` = 8 entrées, dans l'ordre `HTTPException`, `RequestValidationError`, `WebSocketRequestValidationError`, `ValueError`, `plugboard.app._BusyRefusal`, `plugboard.app._UsageUnavailable`, `plugboard.app._ModelNotAllowed`, `plugboard.app._InvalidChatRequest` ; `middleware` = 6 entrées : `CORSMiddleware`, `_RateLimitMiddleware`, `_RequestIdMiddleware`, `_BodySizeLimitMiddleware`, `_SecurityHeadersMiddleware`, `_KeylessLoopbackOnlyMiddleware`.

- [ ] **Step 7: Lancer la garde, puis vérifier qu'elle voit un changement**

Run: `$PYTEST tests/test_chat_path_guard.py` — Expected: 4 passed.

Run: `cd /srv/dev/worktrees/plugboard/plu95 && $PY -c "import tests.conftest, json; from tests import chat_path_snapshot as s; from plugboard.app import app; f = json.loads(s.FIXTURE.read_text()); r = app.router.routes; r[2], r[3] = r[3], r[2]; assert s.client_routes(app) != f['client_routes']; m = app.user_middleware; m[1], m[2] = m[2], m[1]; assert s.middleware(app) != f['middleware']; print('guard sees both swaps')"`
Expected: `guard sees both swaps` (permutations en mémoire seulement, aucun fichier touché).

- [ ] **Step 8: Écrire `tests/test_chat_path_split.py`**

Docstring : « Invariants of moving the chat path out of app.py (PLU-95) ». Contenu :

```python
import ast
import copy
import importlib
import inspect
import pkgutil
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tests.factories import FAKE_COMPLETION
from tests.test_admin_routes_split import PATCH_POINTS, _app_imports

ROOT = Path(__file__).resolve().parents[1]
CHAT_PATH_DIR = ROOT / "plugboard" / "chat_path"
CHAT_PATH_PACKAGE = "plugboard.chat_path"
APP_MODULE = "plugboard.app"
# The modules of the design (Q2 = no: without "account").
EXPECTED_MODULES = frozenset(
    {"account", "billing", "catalogue", "completions", "limits", "responses",
     "streaming", "v2"}
)


def _app_module():
    return importlib.import_module(APP_MODULE)


def _chat_path_modules() -> list:
    """Every submodule of plugboard.chat_path; none before the move."""
    try:
        package = importlib.import_module(CHAT_PATH_PACKAGE)
    except ModuleNotFoundError as exc:
        if exc.name != CHAT_PATH_PACKAGE:
            raise
        return []
    return [
        importlib.import_module(f"{CHAT_PATH_PACKAGE}.{info.name}")
        for info in pkgutil.iter_modules(package.__path__)
    ]
```

| Test | Assertion |
|---|---|
| `test_no_chat_path_module_binds_a_patch_point` | `{m.__name__: sorted(PATCH_POINTS & set(vars(m))) …}` sans entrée non vide |
| `test_chat_path_modules_never_import_the_app_module` | pour chaque `CHAT_PATH_DIR.glob("*.py")` trié : `_app_imports(tree)` plus `[(n.lineno, "relative import") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level]` est vide |
| `test_each_chat_path_module_registers_through_register` | `list(inspect.signature(m.register).parameters) == ["app", "core"]` |
| `test_chat_path_code_logs_under_the_app_logger` | `m.log is _app_module().log` pour tout module qui a `log` |
| `test_chat_path_modules_are_designed_ones` | `found <= EXPECTED_MODULES` (tâche 10 : `==`), `found` = noms courts des modules |
| `test_chat_path_prices_and_writes_usage_only_through_chat_billing` | miroir de `test_chat_usage_is_priced_and_written_in_one_place` : dans chaque `CHAT_PATH_DIR.glob("*.py")`, aucun `ast.Call` dont le nom (attribut ou identifiant) est `append_usage` ou `calculate_cost`, et `"def _replay_completion_as_sse"` absent du texte |

Puis les tests de caractérisation (verts sur `c3e4cea` ; si la mise en place doit être ajustée pour l'environnement de test, ajuster la mise en place, jamais l'assertion) :

```python
@pytest.fixture
def two_imports(monkeypatch):
    """plugboard.app imported twice, as E-25 does (PLU-36's pattern): the
    first module object is put back in sys.modules at teardown."""
    import plugboard

    first = importlib.import_module(APP_MODULE)
    monkeypatch.setitem(sys.modules, APP_MODULE, first)  # restored at teardown
    monkeypatch.setattr(plugboard, "app", first)
    del sys.modules[APP_MODULE]
    second = importlib.import_module(APP_MODULE)
    assert second is not first
    return first, second


def test_billing_target_reads_the_module_that_registered_it(two_imports, monkeypatch):
    first, second = two_imports
    monkeypatch.setattr(first, "_served_by", lambda: ({"id": "first"}, "m-first"))
    monkeypatch.setattr(second, "_served_by", lambda: ({"id": "second"}, "m-second"))
    assert first._billing_target({"model": "x"}) == ({"id": "first"}, "m-first")


def test_model_access_reads_the_module_that_registered_it(two_imports, monkeypatch):
    first, second = two_imports

    def _guard_raising(name):
        def _guard(provider, native):
            raise RuntimeError(name)

        return lambda *a, **k: _guard

    for module, name in ((first, "first"), (second, "second")):
        monkeypatch.setattr(module, "_model_route_guard", _guard_raising(name))
        monkeypatch.setattr(
            module, "resolve_provider_and_model", lambda m: ({"id": "p"}, "m")
        )
    with pytest.raises(RuntimeError, match="^first$"):
        first._check_model_access("acme", "p:m")


async def test_chat_of_the_first_import_calls_its_own_forward(
    _providers_file, two_imports, monkeypatch
):
    """POST /v1/chat/completions on the first module's app reaches the first
    module's forward, not the second's (PLU-95)."""
    import plugboard.ratelimit as rl

    first, second = two_imports
    calls = []

    def _forward_of(name):
        async def _forward(body, **_kwargs):
            calls.append(name)
            return copy.deepcopy(FAKE_COMPLETION)

        return _forward

    monkeypatch.setattr(rl, "_LIMIT", 0)
    for module, name in ((first, "first"), (second, "second")):
        monkeypatch.setattr(module, "_API_KEY", "testkey")
        monkeypatch.setattr(module, "_ADMIN_KEY", "testkey")
        monkeypatch.setattr(module, "forward", _forward_of(name))
    async with AsyncClient(
        transport=ASGITransport(app=first.app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer testkey"},
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "Hi"}],
                "cache": False,
            },
        )
    assert resp.status_code == 200, resp.text
    assert calls == ["first"]
```

*Variante Q2 = non :* retirer `"account"` d'`EXPECTED_MODULES`.

- [ ] **Step 9: Lancer gardes, invariants et tests qui parcourent les routes**

Run: `$PYTEST tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_chat_admission.py tests/test_chat_errors_and_billing.py tests/test_chat_path_guard.py tests/test_chat_path_split.py tests/test_spec_v7_residuals_parent.py`
Expected: tout passe, dont les trois tests de caractérisation (base de comparaison des tâches suivantes).

- [ ] **Step 10: Mesurer le temps d'import de référence**

Run (5 fois) : `cd /srv/dev/worktrees/plugboard/plu95 && T=$(mktemp -d) && env -i PATH=/usr/bin:/bin HOME=$T PLUGBOARD_DATA=$T/data PLUGBOARD_SKIP_DOTENV=1 API_KEY=k ADMIN_KEY=adminkey-conftest PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC=0 $PY -X importtime -c 'import plugboard.app' 2>&1 | grep -E '\| plugboard.app$'; rm -rf "$T"`
Expected: une ligne par passe ; noter la médiane du cumul (µs) dans le corps du commit.

- [ ] **Step 11: Lint puis commit**

`$RUFF check plugboard/ tests/` et `$RUFF format --check plugboard/ tests/` : 0 erreur.

```bash
git add tests/route_snapshot.py tests/chat_path_snapshot.py tests/test_chat_path_guard.py tests/test_chat_path_split.py tests/fixtures/chat_path_snapshot.json
git commit -m "test(chat): freeze the client routes, client contract, exception handlers and middleware (PLU-95)" -m "The PLU-36 import probe also ignores plugboard.chat_path (Q1). Import time of plugboard.app (median of 5, -X importtime): <N> us." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Module `streaming` — raccroché du client, événements SSE, fermeture d'un flux amont

**Files:**
- Create: `plugboard/chat_path/__init__.py`, `plugboard/chat_path/streaming.py`
- Modify: `plugboard/app.py` (bloc 909–1044 ; `_aclose_quietly` 2295–2318 ; bloc d'import)

**Interfaces:**
- Consumes: `core` = module `plugboard.app` ; `_THIS_MODULE` (ligne 682).
- Produces: `streaming.register(app, core) -> SimpleNamespace(_aclose_quietly)` ; niveau module : `_ClientGone`, `_unless_client_gone`, `_client_gone_response`, `_complete_events`, `_done_event_at`, `_stream_cut_event`, `_MAX_HELD` (importés par `completions` et `catalogue`, R10).

- [ ] **Step 1: Créer le paquet et le module** (P1–P4)

`plugboard/chat_path/__init__.py` : docstring seule (le contrat `register(app, core)` ; R3 ; R10 ; R11 ; logger `plugboard.app` ; renvoi à la conception) ; aucun import.
`streaming.py` :
- Niveau module : `_CLIENT_CLOSED_REQUEST`, `_T`, `_ClientGone`, `_unless_client_gone`, `_EVENT_ENDS`, `_DONE`, `_MAX_HELD` (et leurs commentaires), `_done_event_at`, `_complete_events`, `_stream_cut_event`, `_client_gone_response`, avec le commentaire de section « Client hang-up while the upstream works (BUG-30) ».
- Dans `register()` : `_aclose_quietly`.
- Via `core.` : `_background_tasks` (trois occurrences).
- Imports : `asyncio as _asyncio`, `json`, `logging`, `ModuleType`, `SimpleNamespace`, `Any`, `Awaitable`, `TypeVar`, `FastAPI`, `Request`, `Response` (de `fastapi.responses`).

- [ ] **Step 2: Câbler `app.py`** (P5)

Dernier import de tête, après `from plugboard.tokenizer import (…)` :

```python
from plugboard.chat_path import streaming as _cp_streaming
```

À la place du bloc 909–1044 (entre `_inflight_chat_dep` et « One turn at a time per session ») :

```python
# -- Client hang-up and SSE events: chat_path/streaming.py ---------------------
_streaming_ns = _cp_streaming.register(app, _THIS_MODULE)
_aclose_quietly = _streaming_ns._aclose_quietly
_MAX_HELD = _cp_streaming._MAX_HELD
_complete_events = _cp_streaming._complete_events
_done_event_at = _cp_streaming._done_event_at
# Read by code still in app.py: removed by tasks 6 and 7 (PLU-95).
_stream_cut_event = _cp_streaming._stream_cut_event
_ClientGone = _cp_streaming._ClientGone
_unless_client_gone = _cp_streaming._unless_client_gone
_client_gone_response = _cp_streaming._client_gone_response
```

Supprimer `_aclose_quietly` (2295–2318) ; le titre « -- Streaming billing » reste au-dessus de `_bill_streaming`.

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_bug129_preflight_routes.py tests/test_chat_admission.py tests/test_chat_errors_and_billing.py tests/test_chat_path_guard.py tests/test_chat_path_split.py tests/test_cli_stream.py tests/test_cli_stream_route.py tests/test_inflight_disconnect.py tests/test_spec_v7_residuals_parent.py tests/test_stream_fallback.py tests/test_stream_interrupted_billing.py tests/test_stream_usage_injection.py tests/test_streaming.py`
Expected: tout passe, gardes comprises.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/chat_path/__init__.py plugboard/chat_path/streaming.py plugboard/app.py
git commit -m "refactor(chat): move the client hang-up and SSE helpers to chat_path/streaming.py (PLU-95)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Module `responses` — réponses au client et réponses d'erreur

**Files:**
- Create: `plugboard/chat_path/responses.py`
- Modify: `plugboard/app.py` (bloc 2607–3013)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE`.
- Produces: `responses.register(app, core) -> SimpleNamespace(_provider_disabled_response, _chat_error_response)` ; niveau module : `_for_caller`, `_retry_after_headers`, `_quota_error_response`, `_providers_busy_response`, `_unsupported_on_cli_response`, `_replay_completion_as_sse`, `_cli_error_response`, `_model_not_found_response`, `_model_not_served_response`.

- [ ] **Step 1: Créer `responses.py`** (P1–P4)

- Niveau module : `_for_caller`, `_quota_detail`, `_retry_after_headers`, `_quota_error_response`, `_providers_busy_response`, `_unsupported_on_cli_response`, `_replay_completion_as_sse` (et son commentaire), `_provider_http_error_response`, `_cli_error_response`, `MODEL_NOT_SUPPORTED_HINT`, `MODEL_NOT_FOUND_HINT`, `_model_not_found_response`, `_model_not_served_response`.
- Dans `register()` : `_provider_disabled_response`, `_chat_error_response` (il appelle `_provider_disabled_response`, fermeture du même `register()`, directement).
- Via `core.` : `get_provider`.
- Imports : `logging`, `secrets`, `ModuleType`, `SimpleNamespace`, `Any`, `Dict`, `Optional`, `FastAPI`, `JSONResponse`, `from plugboard import chat_billing`, `from plugboard.metrics import record_busy_refusal`, `from plugboard.cli_auth import AuthError`, `from plugboard.cli_limits import UNSUPPORTED_ON_CLI`, `from plugboard.cli_subprocess import CliSubprocessError, ModelNotServedError`, `from plugboard.router import ProviderDisabledError, ProviderHTTPError, ProviderNetworkError, ProvidersBusyError, ProviderQuotaError, ProviderTimeoutError, UnsupportedOnCliError` ; l'import local `from plugboard import ollama` reste dans `_provider_disabled_response`.

- [ ] **Step 2: Câbler `app.py`** (P5)

Ajouter `responses as _cp_responses` au bloc d'import. À la place du bloc 2607–3013 (le titre « -- /v1/chat/completions » reste au-dessus) :

```python
# -- Answers to the client, error responses: chat_path/responses.py -----------
_responses_ns = _cp_responses.register(app, _THIS_MODULE)
_provider_disabled_response = _responses_ns._provider_disabled_response
_chat_error_response = _responses_ns._chat_error_response
_retry_after_headers = _cp_responses._retry_after_headers
_quota_error_response = _cp_responses._quota_error_response
_providers_busy_response = _cp_responses._providers_busy_response
_cli_error_response = _cp_responses._cli_error_response
_model_not_served_response = _cp_responses._model_not_served_response
# Read by chat_completions while it is in app.py: removed by task 6 (PLU-95).
_for_caller = _cp_responses._for_caller
_model_not_found_response = _cp_responses._model_not_found_response
_unsupported_on_cli_response = _cp_responses._unsupported_on_cli_response
_replay_completion_as_sse = _cp_responses._replay_completion_as_sse
```

`get_provider` reste importé (point de patch, `noqa` s'il n'a plus d'usage direct).

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_agy_cage.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_bug120_cli_model_id.py tests/test_chat_admission.py tests/test_chat_cap.py tests/test_chat_errors_and_billing.py tests/test_chat_path_guard.py tests/test_chat_path_split.py tests/test_cli_prompt_too_long.py tests/test_cli_unsupported_400.py tests/test_client_error_codes.py tests/test_decision_api.py tests/test_embeddings_api.py tests/test_http_provider_error_body.py tests/test_mcp_error_parity.py tests/test_mcp_tool_errors.py tests/test_model_not_found.py tests/test_ollama_off_by_config.py tests/test_provider_network_error.py tests/test_quota_exhausted_429.py tests/test_spec_v7_residuals_parent.py tests/test_stream_cli_errors.py`
Expected: tout passe.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/chat_path/responses.py plugboard/app.py
git commit -m "refactor(chat): move the chat answers and error responses to chat_path/responses.py (PLU-95)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Module `limits` — plafond mensuel, réserve de budget, prix connu, RPM, accès aux modèles

**Files:**
- Create: `plugboard/chat_path/limits.py`
- Modify: `plugboard/app.py` (blocs 1985–2289 sauf ce qui reste, 2484–2562)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE`.
- Produces: `limits.register(app, core) -> SimpleNamespace(_check_spending_limit, _estimate_call_cost, _take_budget_hold, _hold_budget, _check_price_known, _check_model_access, _model_route_guard, _model_is_compatible, _guard_accepts)` ; niveau module : `_spending_scope`, `_check_client_rpm` (importé par `completions` et `catalogue`, R10).

- [ ] **Step 1: Créer `limits.py`** (P1–P4)

- Niveau module : `_spending_scope`, `_BUDGET_IMAGE_TOKENS` (et son commentaire), `_whole_count`, `_answers_asked`, `_prompt_estimate`, `_UNPRICED_DETAIL`, `_check_client_rpm`.
- Dans `register()`, dans l'ordre d'origine : `_check_spending_limit`, `_completion_cap`, `_estimate_call_cost`, `_take_budget_hold`, `_hold_budget`, `_check_price_known`, `_check_model_access`, `_model_route_guard`, `_model_is_compatible`, `_guard_accepts`.
- Via `core.` : `_UsageUnavailable` (lever et attraper), `_ModelNotAllowed` (dans la garde imbriquée de `_model_route_guard`), `_BUDGET_RESERVE_TOKENS` (`_completion_cap`), `_usage`, `resolve_provider_and_model` (`_check_model_access`, `_guard_accepts`), `_model_route_guard` (dans `_check_model_access` et `_model_is_compatible`, R3).
- Imports : `asyncio as _asyncio`, `json`, `logging`, `math`, `secrets`, `ModuleType`, `SimpleNamespace`, `Any`, `Callable`, `Dict`, `List`, `Optional`, `FastAPI`, `HTTPException`, `Request`, `from plugboard import billing as _billing`, `from plugboard import chat_billing`, `from plugboard.apikeys import load_keys`, `from plugboard.ratelimit import check_client_rate_limit`, `from plugboard.cli_subprocess import ModelNotServedError`, `from plugboard.router import chat_routes, disabled_route`, `from plugboard.tokenizer import count_tokens, estimate_prompt_tokens` ; les imports locaux (`_resolve_all_providers`, `router as _router`, `model_access_matches`) restent locaux.
- Reste dans `app.py`, à sa place : le titre « -- Spending limit », `_BUDGET_RESERVE_TOKENS` et son commentaire, `_USAGE_RETRY_AFTER_S`, `_UsageUnavailable`, `_usage_unavailable_handler`, `_keep_hold` (tâche 5), le bloc de facturation (tâche 5), le titre « -- Model access check », `_ModelNotAllowed`, `_model_not_allowed_handler`.

- [ ] **Step 2: Câbler `app.py`** (P5)

Ajouter `limits as _cp_limits` au bloc d'import. À la place de `_check_model_access` … `_guard_accepts` (après `_model_not_allowed_handler`) :

```python
# -- Spending limit, budget holds, price, rpm, model access: chat_path/limits.py
_limits_ns = _cp_limits.register(app, _THIS_MODULE)
_check_spending_limit = _limits_ns._check_spending_limit
_estimate_call_cost = _limits_ns._estimate_call_cost
_take_budget_hold = _limits_ns._take_budget_hold
_hold_budget = _limits_ns._hold_budget
_check_price_known = _limits_ns._check_price_known
_check_model_access = _limits_ns._check_model_access
_model_route_guard = _limits_ns._model_route_guard
_model_is_compatible = _limits_ns._model_is_compatible
_guard_accepts = _limits_ns._guard_accepts
_spending_scope = _cp_limits._spending_scope
# Read by code still in app.py: removed by task 7 (PLU-95).
_check_client_rpm = _cp_limits._check_client_rpm
```

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_agy_cage.py tests/test_alerts_provider_faults.py tests/test_allowed_models_published_id.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_bench_007_008_009.py tests/test_budget_holds.py tests/test_bug104_agy_tool_refusal_not_a_fault.py tests/test_bug90_provider_403_not_forbidden_model.py tests/test_chat_admission.py tests/test_chat_path_guard.py tests/test_chat_path_split.py tests/test_chat_request_bounds.py tests/test_circuit_breaker_provider_faults.py tests/test_cli_model_name.py tests/test_cli_prompt_too_long.py tests/test_cli_stream_route.py tests/test_cli_terminal_error_exit0.py tests/test_cli_unsupported_400.py tests/test_client_catalogue_callable.py tests/test_codex_subscription_model.py tests/test_decision_api.py tests/test_docs_allowed_models.py tests/test_egress_guard_routing.py tests/test_embeddings_api.py tests/test_env_numbers_at_start.py tests/test_http_provider_error_body.py tests/test_model_access_security.py tests/test_model_authorization_codex.py tests/test_no_retry_context_too_long.py tests/test_provider_network_error.py tests/test_publication_order_v4.py tests/test_quota_exhausted_429.py tests/test_route_pauses.py tests/test_router_admission.py tests/test_rpm_limit_scope.py tests/test_spec_v7_residuals_parent.py tests/test_stream_cli_errors.py tests/test_stream_fallback.py`
Expected: tout passe, dont `test_model_access_reads_the_module_that_registered_it`.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/chat_path/limits.py plugboard/app.py
git commit -m "refactor(chat): move the spending, budget, price, rpm and model access checks to chat_path/limits.py (PLU-95)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Module `billing` — facturation d'un flux et d'une réponse, alerte de dépense

**Files:**
- Create: `plugboard/chat_path/billing.py`
- Modify: `plugboard/app.py` (`_keep_hold` 2231–2237 ; 2292–2466 ; 2565–2601)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE`.
- Produces: `billing.register(app, core) -> SimpleNamespace(_bill_streaming, _bill_completion, _charge_answer, _billing_target, _send_spending_alert)` ; niveau module : `_keep_hold` (importé par `completions` et `catalogue`, R10).

- [ ] **Step 1: Créer `billing.py`** (P1–P4)

- Niveau module : `_keep_hold`.
- Dans `register()`, dans l'ordre d'origine : `_bill_streaming`, `_bill_completion`, `_charge_answer`, `_billing_target`, `_send_spending_alert`.
- Via `core.` : `_served_by`, `resolve_provider_and_model` (`_billing_target`), `_usage`, `_check_and_alert` (`_send_spending_alert`), `_send_spending_alert` (dans `_charge_answer`, R3).
- Imports : `asyncio as _asyncio`, `logging`, `ModuleType`, `SimpleNamespace`, `Any`, `Dict`, `Optional`, `FastAPI`, `Request`, `from plugboard import chat_billing`, `from plugboard.apikeys import load_keys`, `from plugboard.router import usage_reported as _usage_reported`, `from plugboard.tokenizer import count_tokens, estimate_prompt_tokens` ; l'import local `from plugboard.alerts import in_background` et `import json as _json` restent locaux.

- [ ] **Step 2: Câbler `app.py`** (P5)

Ajouter `billing as _cp_billing` au bloc d'import. Supprimer `_keep_hold`, le titre « -- Streaming billing » et le bloc `_bill_streaming` … `_billing_target`, le titre « -- Spending alert helper » et `_send_spending_alert`. Juste après le câblage de `limits` :

```python
# -- Billing of an answer, spending alert: chat_path/billing.py --------------
_billing_ns = _cp_billing.register(app, _THIS_MODULE)
_bill_streaming = _billing_ns._bill_streaming
_bill_completion = _billing_ns._bill_completion
_charge_answer = _billing_ns._charge_answer
_billing_target = _billing_ns._billing_target
_send_spending_alert = _billing_ns._send_spending_alert
# Read by code still in app.py: removed by task 7 (PLU-95).
_keep_hold = _cp_billing._keep_hold
```

`_served_by` et `_check_and_alert` restent importés (points de patch, `noqa`).

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_agy_usage.py tests/test_alerts.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_billing_usage.py tests/test_budget_holds.py tests/test_chat_admission.py tests/test_chat_errors_and_billing.py tests/test_chat_path_guard.py tests/test_chat_path_split.py tests/test_cli_stream_route.py tests/test_decision_api.py tests/test_io_off_loop.py tests/test_provider_pricing.py tests/test_spec_v7_residuals_parent.py tests/test_stream_fallback.py tests/test_stream_interrupted_billing.py tests/test_stream_usage_injection.py tests/test_streaming.py`
Expected: tout passe, dont `test_billing_target_reads_the_module_that_registered_it` et `test_chat_path_prices_and_writes_usage_only_through_chat_billing`.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/chat_path/billing.py plugboard/app.py
git commit -m "refactor(chat): move the chat billing and spending alert to chat_path/billing.py (PLU-95)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Module `completions` — `POST /v1/chat/completions`, validation, tour de session

**Files:**
- Create: `plugboard/chat_path/completions.py`
- Modify: `plugboard/app.py` (1070–1118 ; 3032–3113 ; route 3129–3743)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE` ; ré-exports des tâches 2 à 5.
- Produces: `completions.register(app, core) -> SimpleNamespace(_record_session_turn, chat_completions)`.

- [ ] **Step 1: Créer `completions.py`** (P1–P4)

Route : `POST /v1/chat/completions` `chat_completions` (décorateur verbatim, `dependencies=[Depends(core._resolve_client), Depends(core._refuse_anonymous_chat), Depends(core._inflight_chat_dep), Depends(core._session_turn_dep)]`, paramètre `client_id: Optional[str] = Depends(core._resolve_client)`), son générateur imbriqué `_stream_and_bill` compris, verbatim.
- Niveau module : `_shown_role`.
- Dans `register()`, dans l'ordre d'origine : `_take_session_turn`, `_record_session_turn`, `_require_output_caps`, `_require_chat_messages`, `_live_note`, `chat_completions`.
- Via `core.` : `_resolve_client`, `_refuse_anonymous_chat`, `_inflight_chat_dep`, `_session_turn_dep`, `_InvalidChatRequest`, `_SessionBusy`, `_pad_cache_hit_response`, `_strict_tenant_mode` (exception à R6 : `plugboard.cache_common` n'est importé par `app.py` qu'à la ligne 310, après `setup_logging()` ; l'importer en tête de `completions.py` le chargerait plus tôt, R11), `_get_request_key_record`, `cli_refusal`, `forward`, `forward_stream`, `load_providers`, `resolve_provider_and_model`, `_check_spending_limit`, `_model_route_guard`, `_check_price_known`, `_hold_budget`, `_charge_answer`, `_bill_streaming`, `_send_spending_alert`, `_record_session_turn` (R3), `_provider_disabled_response`, `_chat_error_response`, `_aclose_quietly`.
- Imports du paquet (R10) : de `streaming` : `_ClientGone`, `_client_gone_response`, `_complete_events`, `_done_event_at`, `_stream_cut_event`, `_unless_client_gone` ; de `responses` : `_for_caller`, `_model_not_found_response`, `_model_not_served_response`, `_replay_completion_as_sse`, `_unsupported_on_cli_response` ; de `limits` : `_check_client_rpm` ; de `billing` : `_keep_hold`.
- Autres imports : `asyncio as _asyncio`, `functools`, `logging`, `time`, `ModuleType`, `SimpleNamespace`, `Any`, `Dict`, `List`, `Optional`, `Depends`, `FastAPI`, `HTTPException`, `Request`, `status`, `JSONResponse`, `StreamingResponse`, `from plugboard import chat_billing`, `from plugboard import live_requests as _live`, `from plugboard import request_wait as _request_wait`, `from plugboard.storage import to_thread_to_the_end as _to_thread_to_the_end`, `from plugboard.api_contract import CHAT_ROLES, MAX_OUTPUT_TOKENS_CAP`, `from plugboard.legacy_connectors import REMOVED_CONNECTOR_ERROR, require_supported_model, require_supported_provider`, `from plugboard.decisions import is_decision_model`, `from plugboard.router import disabled_route, is_unknown_model, is_unserved_cli_model, usage_reported as _usage_reported` ; les imports locaux (session, cache, cache sémantique, `cli_subprocess`, `json as _json_mod`) restent locaux.
- Reste dans `app.py`, à sa place : `_session_turn_dep`, `_InvalidChatRequest`, `_invalid_chat_request_handler`, `_refuse_anonymous_chat`, le titre « -- /v1/chat/completions ».

- [ ] **Step 2: Câbler `app.py`** (P5)

Ajouter `completions as _cp_completions` au bloc d'import. À la place de la route (après `_refuse_anonymous_chat`) :

```python
# -- POST /v1/chat/completions: chat_path/completions.py ----------------------
_completions_ns = _cp_completions.register(app, _THIS_MODULE)
_record_session_turn = _completions_ns._record_session_turn
chat_completions = _completions_ns.chat_completions
```

Retirer les ré-exports provisoires `_stream_cut_event`, `_for_caller`, `_model_not_found_response`, `_unsupported_on_cli_response`, `_replay_completion_as_sse`. `forward_stream` et `cli_refusal` restent importés (points de patch, `noqa`).

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_agy_usage.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_billing_usage.py tests/test_budget_holds.py tests/test_bug102_quarantine_provider_scope.py tests/test_bug104_agy_tool_refusal_not_a_fault.py tests/test_bug105_quarantine_lift_and_timeout.py tests/test_bug120_cli_model_id.py tests/test_bug90_provider_403_not_forbidden_model.py tests/test_call_outcome_threshold.py tests/test_chat_admission.py tests/test_chat_bad_body_and_cache.py tests/test_chat_cap.py tests/test_chat_errors_and_billing.py tests/test_chat_messages_validation.py tests/test_chat_path_guard.py tests/test_chat_path_split.py tests/test_chat_request_bounds.py tests/test_circuit_breaker_provider_faults.py tests/test_cli_stream_route.py tests/test_cli_unsupported_400.py tests/test_cli_unsupported_routing.py tests/test_client_catalogue_callable.py tests/test_codex_subscription_model.py tests/test_decision_review_fixes.py tests/test_decision_routing_v5.py tests/test_egress_guard_routing.py tests/test_env_numbers_at_start.py tests/test_exposure_and_estimates.py tests/test_inflight_disconnect.py tests/test_inherited_runtime_v6.py tests/test_live_requests.py tests/test_metrics_per_model.py tests/test_model_access_security.py tests/test_model_not_found.py tests/test_ollama_off_by_config.py tests/test_ollama_switch.py tests/test_ollama_switch_e2e.py tests/test_ollama_switch_router.py tests/test_openclaw_removal_review.py tests/test_openclaw_removal_routing.py tests/test_openclaw_spec_parent.py tests/test_plu67_claude_quota_watch.py tests/test_publication_collision_parent.py tests/test_route_pauses.py tests/test_router_admission.py tests/test_session_store.py tests/test_session_turns.py tests/test_spec_v7_residuals_parent.py tests/test_stream_cli_errors.py tests/test_stream_fallback.py tests/test_stream_interrupted_billing.py tests/test_stream_usage_injection.py tests/test_streaming.py tests/test_tnr_sec001_cache_isolation.py tests/test_tnr_sec001_missing_client_id.py`
Expected: tout passe, dont `test_chat_of_the_first_import_calls_its_own_forward`.

- [ ] **Step 5: Relire (P7) puis commit**

La relecture `--color-moved` porte sur ~750 lignes : vérifier en particulier que `_stream_and_bill` est inchangé hors préfixes `core.`.

```bash
git add plugboard/chat_path/completions.py plugboard/app.py
git commit -m "refactor(chat): move POST /v1/chat/completions to chat_path/completions.py (PLU-95)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Module `catalogue` — liste des modèles, preflight, fiche d'un modèle

**Files:**
- Create: `plugboard/chat_path/catalogue.py`
- Modify: `plugboard/app.py` (bloc 3746–4066)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE`.
- Produces: `catalogue.register(app, core) -> SimpleNamespace(models, _models_for_key)`.

- [ ] **Step 1: Créer `catalogue.py`** (P1–P4)

Routes, dans l'ordre : `GET /v1/models` `models` ; `POST /v1/preflight` `preflight` (`dependencies=[Depends(core._resolve_client), Depends(core._inflight_chat_dep)]`) ; `GET /v1/models/benchmarks` `models_with_benchmarks` ; `GET /v1/models/{model_id:path}/info` `model_info`.
- Niveau module : `_PROBE_FAILURE_MAX_AGE_S` (et son commentaire), `_probe_age`, `_not_callable_now`, `_PreflightItem`, `_PREFLIGHT_MAX_ITEMS`, `_PreflightRequest` (annotation de `preflight` : niveau module obligatoire, R5).
- Dans `register()` : `_models_for_key`, `models`, `_preflight_refusal`, `preflight`, `models_with_benchmarks`, `model_info`.
- Via `core.` : `_resolve_client`, `_inflight_chat_dep`, `_get_request_key_record`, `_model_route_guard`, `_guard_accepts`, `list_models`, `load_providers`, `forward`, `resolve_provider_and_model`, `_check_spending_limit`, `_check_model_access`, `_check_price_known`, `_take_budget_hold`, `_bill_completion`, `_UsageUnavailable`, `_ChatOverloaded`.
- Imports du paquet (R10) : `_ClientGone`, `_client_gone_response`, `_unless_client_gone` (`streaming`) ; `_check_client_rpm` (`limits`) ; `_keep_hold` (`billing`).
- Autres imports : `asyncio as _asyncio`, `logging`, `time`, `datetime`, `timezone`, `ModuleType`, `SimpleNamespace`, `Any`, `Dict`, `List`, `Optional`, `Depends`, `FastAPI`, `HTTPException`, `Request`, `BaseModel`, `Field`, `from plugboard import chat_billing`, `from plugboard.cli_subprocess import ModelNotServedError`, `from plugboard.decisions import is_decision_model`, `from plugboard.router import ProviderDisabledError, ProvidersBusyError, chat_routes, disabled_route, is_unknown_model, is_unserved_cli_model` ; les imports locaux (`call_outcome`, `model_identity`, `legacy_connectors`, `benchmarks`) restent locaux.

- [ ] **Step 2: Câbler `app.py`** (P5)

Ajouter `catalogue as _cp_catalogue` au bloc d'import. À la place du bloc (titre « -- /v1/models » compris) :

```python
# -- /v1/models, /v1/preflight, model info: chat_path/catalogue.py -----------
_catalogue_ns = _cp_catalogue.register(app, _THIS_MODULE)
models = _catalogue_ns.models
_models_for_key = _catalogue_ns._models_for_key
```

Retirer les ré-exports provisoires `_ClientGone`, `_unless_client_gone`, `_client_gone_response`, `_keep_hold`, `_check_client_rpm`. `list_models` reste importé (point de patch, `noqa` s'il n'a plus d'usage direct).

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_input_guards.py tests/test_admin_people_api.py tests/test_admin_people_scope.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_allowed_models_published_id.py tests/test_api.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_bench_007_008_009.py tests/test_benchmarks.py tests/test_billing_usage.py tests/test_budget_holds.py tests/test_bug120_cli_model_id.py tests/test_bug129_preflight_routes.py tests/test_chat_admission.py tests/test_chat_path_guard.py tests/test_chat_path_split.py tests/test_client_catalogue_callable.py tests/test_decision_api.py tests/test_decision_v2_fixes.py tests/test_e2e_dashboard.py tests/test_e2e_full_coverage.py tests/test_e2e_functional.py tests/test_e2e_subprocess.py tests/test_egress_guard_api.py tests/test_inflight_disconnect.py tests/test_inherited_runtime_parent.py tests/test_inherited_runtime_v6.py tests/test_keyless_public_bind.py tests/test_mcp_error_parity.py tests/test_mcp_stdio_real.py tests/test_metrics_per_model.py tests/test_model_not_found.py tests/test_netguard.py tests/test_ollama_off_by_config.py tests/test_ollama_switch.py tests/test_ollama_switch_admin.py tests/test_ollama_switch_e2e.py tests/test_openclaw_removal_mcp.py tests/test_openclaw_spec_parent.py tests/test_plu72_existing_claude_provider.py tests/test_publication_order_v4.py tests/test_ratelimit_identity.py tests/test_readiness.py tests/test_recette_acces.py tests/test_recette_passerelle.py tests/test_router_admission.py tests/test_rpm_limit_scope.py tests/test_security_tnr.py tests/test_smoke_consumers.py tests/test_smoke_models_env.py tests/test_smoke_models_redirect.py tests/test_spec_v7_residuals_parent.py`
Expected: tout passe.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/chat_path/catalogue.py plugboard/app.py
git commit -m "refactor(chat): move the model list, preflight and model info routes to chat_path/catalogue.py (PLU-95)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Module `v2` — alias `/v2`

**Files:**
- Create: `plugboard/chat_path/v2.py`
- Modify: `plugboard/app.py` (bloc 4068–4107)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE` ; `chat_completions` (tâche 6) et `_models_for_key` (tâche 7) ré-exportés.
- Produces: `v2.register(app, core) -> SimpleNamespace(models_v2)`.

- [ ] **Step 1: Créer `v2.py`** (P1–P4)

Routes, dans l'ordre : `POST /v2/chat/completions` `chat_completions_v2` (mêmes quatre dépendances que la route v1, par `core.`) ; `GET /v2/models` `models_v2` ; `GET /v2/models/benchmarks` `models_benchmarks_v2`.
- Niveau module : le commentaire de section « /v2/ aliases (API versioning) ».
- Dans `register()` : les 3 handlers.
- Via `core.` : `_resolve_client`, `_refuse_anonymous_chat`, `_inflight_chat_dep`, `_session_turn_dep`, `chat_completions`, `_models_for_key`.
- Imports : `ModuleType`, `SimpleNamespace`, `Optional`, `Depends`, `FastAPI`, `Request` ; l'import local de `benchmarks` reste local.

- [ ] **Step 2: Câbler `app.py`** (P5)

Ajouter `v2 as _cp_v2` au bloc d'import. À la place du bloc :

```python
# -- /v2 aliases: chat_path/v2.py ----------------------------------------------
_v2_ns = _cp_v2.register(app, _THIS_MODULE)
models_v2 = _v2_ns.models_v2
```

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_input_guards.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_allowed_models_published_id.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_bench_007_008_009.py tests/test_budget_holds.py tests/test_bug120_cli_model_id.py tests/test_chat_admission.py tests/test_chat_bad_body_and_cache.py tests/test_chat_errors_and_billing.py tests/test_chat_messages_validation.py tests/test_chat_path_guard.py tests/test_chat_path_split.py tests/test_chat_request_bounds.py tests/test_client_catalogue_callable.py tests/test_e2e_full_coverage.py tests/test_e2e_functional.py tests/test_final_audit_v9.py tests/test_inflight_disconnect.py tests/test_inherited_auth_parent.py tests/test_inherited_auth_v7.py tests/test_metrics_per_model.py tests/test_model_not_found.py tests/test_ollama_off_by_config.py tests/test_ollama_switch.py tests/test_ollama_switch_e2e.py tests/test_publication_order_v4.py tests/test_router_admission.py tests/test_rpm_limit_scope.py tests/test_session_turns.py tests/test_spec_v7_residuals_parent.py`
Expected: tout passe.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/chat_path/v2.py plugboard/app.py
git commit -m "refactor(chat): move the /v2 aliases to chat_path/v2.py (PLU-95)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Module `account` — routes du compte client (Q2)

*Variante Q2 = non : tâche sautée.*

**Files:**
- Create: `plugboard/chat_path/account.py`
- Modify: `plugboard/app.py` (bloc 4175–4273)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE`.
- Produces: `account.register(app, core) -> SimpleNamespace()` (aucun nom lu ailleurs).

- [ ] **Step 1: Créer `account.py`** (P1–P4)

Routes, dans l'ordre : `GET /v1/inflight` `inflight_me` ; `GET /admin/inflight` `inflight_admin` (`dependencies=[Depends(core._require_admin)]`) ; `GET /usage/me` `usage_me` ; `GET /v1/me` `client_info` ; `GET /v1/me/keys` `client_keys`.
- Niveau module : rien.
- Dans `register()` : les 5 handlers.
- Via `core.` : `_resolve_client`, `_require_admin`, `_get_request_key_record`, `_usage`.
- Imports : `ModuleType`, `SimpleNamespace`, `Any`, `Dict`, `Optional`, `Depends`, `FastAPI`, `HTTPException`, `Request`, `from plugboard import billing as _billing`, `from plugboard import live_requests as _live`, `from plugboard.apikeys import get_keys_by_client, load_keys, mask_key`.
- Reste dans `app.py` : `/health`, `/health/ready`, `/health/detailed` au-dessus ; `/metrics` en dessous.

- [ ] **Step 2: Câbler `app.py`** (P5)

Ajouter `account as _cp_account` au bloc d'import. À la place du bloc (titres « -- /usage/me » et « -- /v1/me » compris) :

```python
# -- /v1/inflight, /admin/inflight, /usage/me, /v1/me: chat_path/account.py --
_cp_account.register(app, _THIS_MODULE)
```

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST tests/test_admin_input_guards.py tests/test_admin_people_scope.py tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_billing_usage.py tests/test_chat_admission.py tests/test_chat_path_guard.py tests/test_chat_path_split.py tests/test_coverage_gaps.py tests/test_e2e_full_coverage.py tests/test_e2e_functional.py tests/test_live_requests.py tests/test_mcp_client_identity.py tests/test_mcp_stdio_real.py tests/test_recette_acces.py tests/test_review_fixes.py tests/test_rpm_limit_scope.py tests/test_smoke_consumers.py tests/test_spec_v7_residuals_parent.py`
Expected: tout passe.

- [ ] **Step 5: Relire (P7) puis commit**

```bash
git add plugboard/chat_path/account.py plugboard/app.py
git commit -m "refactor(chat): move the client account routes to chat_path/account.py (PLU-95)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Clôture — documentation, invariant final et contrôles du lot

**Files:**
- Modify: `plugboard/app.py` (docstring de tête seulement), `docs/ARCHITECTURE.md`, `CHANGELOG.md`, `tests/test_chat_path_split.py` (une assertion)

**Interfaces:**
- Consumes: tâches 1 à 9.
- Produces: rien pour le code.

- [ ] **Step 1: Invariant final**

Dans `test_chat_path_modules_are_designed_ones`, remplacer `found <= EXPECTED_MODULES` par `sorted(found) == sorted(EXPECTED_MODULES)` (docstring : « Exactly the modules of the design: none missing, none extra »).

- [ ] **Step 2: Documentation**

- `plugboard/app.py`, docstring de tête, après la ligne PLU-36 : « The chat path (`/v1/chat/completions`, `/v1/models`, `/v1/preflight`, `/v2/*`, `/v1/inflight`, `/usage/me`, `/v1/me`) and its limits, billing and error answers are declared by `plugboard/chat_path/` (registered in place by `register(app, core)`, PLU-95). »
- `docs/ARCHITECTURE.md`, tableau « Modules » : ligne `app.py` → « Authentication, middlewares, chat admission (concurrency cap, session turn), health, contract and dashboard pages; registers the admin routes and the chat path » ; nouvelle ligne `chat_path/` → « Chat path, one module per subject (client hang-up and SSE, answers and errors, spending and model access checks, billing, chat completions, model catalogue and preflight, `/v2` aliases, client account), each declared on the app by `register(app, core)` ». Section « Route table guard » : un paragraphe sur `tests/test_chat_path_guard.py` (routes client et leur place, contrat client, gestionnaires d'exception, middlewares ; même règle : on régénère par `.venv/bin/python -m tests.chat_path_snapshot` seulement pour un changement voulu, diff relu), et préciser que les modules de `plugboard/admin_routes/` et `plugboard/chat_path/` ne comptent pas dans les effets d'import.
- `CHANGELOG.md`, sous `## [Unreleased]`, section `### Changed` : « **Chat path moved out of `app.py`** (PLU-95): one module per subject under `plugboard/chat_path/`; routes, answers, the client contract and both OpenAPI documents are unchanged. »

- [ ] **Step 3: Contrôles du lot**

Run, depuis `/srv/dev/worktrees/plugboard/plu95` :
- `wc -l plugboard/app.py` → Expected: entre 2 450 et 2 600 (≈ 2 520 ; Q2 = non : 2 550–2 700).
- `git diff --name-status c3e4cea -- tests/ | grep -v '^A'` → Expected: `M	tests/route_snapshot.py` seulement (Q1 = non : `M	tests/fixtures/admin_routes_snapshot.json` à la place).
- `git diff --exit-code $(git log --format=%H --grep='freeze the client routes' -1) -- tests/route_snapshot.py tests/chat_path_snapshot.py tests/test_chat_path_guard.py tests/fixtures/chat_path_snapshot.json tests/fixtures/admin_routes_snapshot.json tests/test_admin_routes_guard.py` → Expected: code 0.
- `grep -nE '^(_stream_cut_event|_ClientGone|_unless_client_gone|_client_gone_response|_for_caller|_model_not_found_response|_unsupported_on_cli_response|_replay_completion_as_sse|_check_client_rpm|_keep_hold) = ' plugboard/app.py` → Expected: aucune sortie (ré-exports provisoires retirés).
- `grep -n 'CONTRACT_VERSION = ' plugboard/api_contract.py` → Expected: `"3.0.1"` ; `git diff --quiet c3e4cea -- docs/api/ plugboard/api_contract.py` → code 0.
- `T=$(mktemp -d) && PLUGBOARD_DATA=$T PLUGBOARD_SKIP_DOTENV=1 $PY scripts/export_contract.py --check && PLUGBOARD_DATA=$T PLUGBOARD_SKIP_DOTENV=1 $PY scripts/export_contract.py --mcp --check; rm -rf "$T"` → Expected: `docs/api/openapi.json: up to date`, `docs/api/mcp.json: up to date`.
- Temps d'import : la commande de la tâche 1, step 10, 5 fois → Expected: médiane ≤ référence + 10 %.

- [ ] **Step 4: Tests ciblés, lint**

Run: `$PYTEST tests/test_admin_routes_guard.py tests/test_admin_routes_split.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_chat_errors_and_billing.py tests/test_chat_path_guard.py tests/test_chat_path_split.py tests/test_cli_versions_single_source.py tests/test_docs_admin_people.py tests/test_docs_links.py tests/test_docs_release_4143.py tests/test_docs_release_4144.py tests/test_docs_release_4145.py tests/test_docs_release_4146.py tests/test_no_remote_ci.py tests/test_plu72_existing_claude_provider.py` puis `$RUFF check plugboard/ tests/` et `$RUFF format --check plugboard/ tests/`.
Expected: tout passe ; 0 erreur.

- [ ] **Step 5: Commit**

```bash
git add plugboard/app.py docs/ARCHITECTURE.md CHANGELOG.md tests/test_chat_path_split.py
git commit -m "docs(chat): say where the chat path lives (PLU-95)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Jalon — validation complète unique, puis une CI (hors tâches)

À lancer une fois, sur le dernier commit du lot, depuis `/srv/dev/worktrees/plugboard/plu95`, avec la procédure du jalon de PLU-36 (`docs/superpowers/plans/2026-11-18-plu-36-admin-routes.md`, section « Jalon »), chemins adaptés à `plu95` :

1. **Charge** : `uptime` (charge < 10), `free -g`, `ci etat` (aucune CI `plugboard` en cours) ; au plus deux suites lourdes à la fois.
2. **Lancement simultané** : quatre blocs Python contigus (`split -n l/4` sur la liste triée de `tests/test_*.py`, `tests/integration` et `tests/live_model_tests` ajoutés au dernier), chacun avec son `PLUGBOARD_DATA` ; `ruff check` + `ruff format --check` ; `export_contract.py --check` et `--mcp --check` ; le couloir e2e isolé (son venv, son `PLUGBOARD_DATA`, son port). Expected : chaque code de retour vaut 0.
3. **En cas d'échec** : corriger, rejouer **seulement** les nœuds en échec ; jamais un test assoupli ou sauté.
4. **Une seule CI**, une fois tout vert : `ci lancer` depuis l'atelier, suivi `ci etat`, `ci journal plugboard <sha>`. Rouge par le produit : `board bug ajouter "…"`, corriger, rejouer la partie fautive, relancer la CI une fois.
5. **Nettoyage** : supprimer le dossier temporaire du jalon ; rien d'autre à garder.
6. **Board** : `board ticket statut PLU-95 revue` (depuis `/srv/dev/worktrees/plugboard/pilotage-board`) avec le SHA et le lien du rapport CI.

# Cœur d'authentification hors de `app.py` (PLU-97) — plan d'exécution

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sortir de `plugboard/app.py` le cœur d'authentification qui se trouve derrière les dépendances FastAPI (principal et compteurs, clé admin et sessions, refus inter-sites, authentification admin et clés personnelles, ré-authentification MFA, connexion automatique locale, ≈ 675 lignes) vers quatre modules de `plugboard/auth_core/`, sans aucun changement de comportement ; `_resolve_client` et `_require_admin` restent dans `app.py`, même objet, même nom ; `app.py` passe de 2 540 à ≈ 1 920 lignes.

**Architecture:** Le contrat de PLU-36 et PLU-95 : chaque module expose `register(app: FastAPI, core: ModuleType) -> SimpleNamespace` (aucune route ici) ; toute fonction qui lit `core` est une fermeture de `register()`, liée au module `plugboard.app` qui l'enregistre. Règle propre à PLU-97 (R13) : tout appel d'une fonction et toute lecture d'une valeur définie ou ré-exportée par `app.py` passent par `core.<nom>`, même dans le même module. `app.py` appelle `register(app, _THIS_MODULE)` à la place de chaque bloc et ré-exporte **chaque** nom déplacé (R14). Deux gardes écrites d'abord figent, sur `919f01f`, l'identité et le nom des dépendances, la surface de `plugboard.app`, l'ordre de chargement à l'import et une table de décisions d'authentification ; les gardes PLU-36 et PLU-95 restent en place.

**Tech Stack:** Python 3.12, FastAPI 0.143.3 / Starlette 1.8.0, pytest (`asyncio_mode = auto`), httpx `ASGITransport`, ruff.

**Spec:** `docs/superpowers/specs/2026-11-18-plu-97-auth-core-design.md` (fait foi : R2 étendue, R11-bis, R13, R14, tableau des points de patch, questions Q1–Q4). R1–R9 : `docs/superpowers/specs/2026-11-18-plu-36-admin-routes-design.md` ; R10–R12 : `docs/superpowers/specs/2026-11-18-plu-95-chat-path-design.md`.

**Décisions :** à prendre (Q1–Q4). Le plan suit les recommandations ; les variantes « Q1 = non » et « Q3 = non » sont indiquées aux tâches concernées. Q2 = B ou C demanderait un autre plan.

## Global Constraints

- Atelier : `/srv/dev/worktrees/plugboard/plu97`, branche `refactor/plu-97-auth-core` (base de code `919f01f` ; le commit de cette conception ne touche que `docs/superpowers/`). Rien hors de l'atelier. Interdits : `git commit --amend`, rebase, reset, push, `rm -rf` hors d'un dossier temporaire créé par la tâche, accès à l'UAT ou à la prod. Un commit **nouveau** par tâche, Conventional Commits, message terminé par `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Une correction demandée par une relecture est un **nouveau** commit.
- Comportement inchangé : mêmes routes et dépendances (objet et nom qualifié), mêmes réponses, mêmes décisions d'authentification, mêmes entrées d'audit, mêmes compteurs, mêmes OpenAPI, mêmes gestionnaires d'exception et middlewares, mêmes effets d'import (modules, ordre, journaux).
- **Aucun test existant modifié, assoupli, sauté ni supprimé**, sauf la ligne de `tests/route_snapshot.py` de la tâche 1 (Q1). Contrôle : `git diff --name-status 919f01f -- tests/ | grep -v '^A'` ne sort que `M	tests/route_snapshot.py`.
- Après la tâche 2, ne changent plus : `tests/route_snapshot.py`, `tests/auth_core_snapshot.py`, `tests/auth_decisions.py`, `tests/test_auth_core_guard.py`, `tests/fixtures/auth_core_snapshot.json`, `tests/fixtures/auth_decisions.json`, et (déjà figés) `tests/fixtures/admin_routes_snapshot.json`, `tests/fixtures/chat_path_snapshot.json`. `tests/test_auth_core_split.py` ne change qu'à la tâche 7 (une assertion). Toutes les gardes restent vertes à chaque tâche.
- Aucun changement hors `plugboard/app.py`, `plugboard/auth_core/`, `tests/` (fichiers nouveaux + la ligne Q1), `docs/ARCHITECTURE.md`, `CHANGELOG.md` : `git diff --quiet 919f01f -- plugboard/admin_routes/ plugboard/chat_path/ plugboard/api_contract.py docs/api/` vaut 0 à chaque tâche. `CONTRACT_VERSION` reste `"3.0.1"`.
- **Relectures en Opus** (code de sécurité) : après chaque tâche, deux relectures par des sous-agents `claude-opus-5-5`, jamais Sonnet : (1) conformité à la conception et au plan (verbatim, R13, R14, câblage, listes « via `core.` »), sur `git diff --color-moved=dimmed-zebra --color-moved-ws=allow-indentation-change HEAD~1 -- plugboard/` ; (2) sécurité et qualité (comparaisons en temps constant, ordre des contrôles, chemins de refus, journaux sans secret). Un constat bloquant se corrige par un nouveau commit et se relit à nouveau. Relecture finale Opus de toute la branche (`git diff 919f01f..HEAD`) avant le jalon. Fable seulement pour arbitrer deux relectures Opus contradictoires.
- Tests ciblés seulement pendant le lot (règle YB du 18/11) ; une seule validation complète, à la section « Jalon », hors des tâches. Commandes :
  - `PY=/srv/dev/plugboard/.venv/bin/python` ; `RUFF=/srv/dev/plugboard/.venv/bin/ruff`
  - `PYTEST="env PLUGBOARD_SKIP_DOTENV=1 $PY -m pytest -q -p no:cacheprovider"` (le `tests/conftest.py` donne à chaque processus son propre `PLUGBOARD_DATA` temporaire)
  - Gardes (créées tâches 1 et 2) :
    `GUARDS="tests/test_admin_routes_guard.py tests/test_auth_core_guard.py tests/test_auth_core_split.py tests/test_chat_path_guard.py"`
  - Fichiers qui patchent les noms lus par `core.` dans le code déplacé (22, conception, tableau des points de patch) :
    `PATCHERS="tests/test_admin_csrf_same_site.py tests/test_admin_people_auth.py tests/test_admin_people_login.py tests/test_admin_people_mfa_routes.py tests/test_admin_people_scope.py tests/test_admin_routes_split.py tests/test_audit_principal.py tests/test_auth_auto_login.py tests/test_bug120_cli_model_id.py tests/test_chat_path_split.py tests/test_decision_parent_regressions.py tests/test_keyless_public_bind.py tests/test_login_mobile_sol.py tests/test_model_not_found.py tests/test_ollama_off_by_config.py tests/test_ollama_switch.py tests/test_ollama_switch_e2e.py tests/test_openclaw_spec_parent.py tests/test_plu72_existing_claude_provider.py tests/test_ratelimit_identity.py tests/test_resolve_client.py tests/test_spec_v7_residuals_parent.py"`
  - Tests d'authentification, de session, de MFA, de clés personnelles, de limite et de mandataires, plus les tests qui parcourent les routes ou réimportent l'app (42) :
    `AUTH_TESTS="tests/test_admin_input_guards.py tests/test_admin_mfa_matching_step.py tests/test_admin_people_api.py tests/test_admin_people_contract.py tests/test_admin_people_dashboard.py tests/test_admin_people_names.py tests/test_admin_people_redaction.py tests/test_admin_people_rekey.py tests/test_admin_people_store.py tests/test_admin_session_edges.py tests/test_api_contract.py tests/test_audit_e2e.py tests/test_auth_residuals_v7b.py tests/test_billing_usage.py tests/test_catalog_history.py tests/test_chat_admission.py tests/test_docs_admin_people.py tests/test_e2e_dashboard.py tests/test_e2e_subprocess.py tests/test_env_numbers_at_start.py tests/test_envcompat.py tests/test_final_audit_parent.py tests/test_final_audit_v9.py tests/test_inherited_auth_parent.py tests/test_inherited_auth_v7.py tests/test_mcp_admin_tools.py tests/test_monitoring.py tests/test_oauth_broker.py tests/test_ollama_downloads_guard.py tests/test_proxy_client_ip.py tests/test_readiness_visibility.py tests/test_recette_services.py tests/test_review_fixes.py tests/test_secrets_rekey.py tests/test_security_audit_2026_04.py tests/test_security_hardening.py tests/test_security_review_parent.py tests/test_security_tnr.py tests/test_session_logout_all.py tests/test_tnr_sec001_cache_isolation.py tests/test_tnr_sec001_missing_client_id.py tests/test_trusted_proxies.py"`
  - Passe ciblée d'une tâche : `$PYTEST $(printf '%s\n' $GUARDS $PATCHERS $AUTH_TESTS | sort -u)` — liste **triée** (`tests/test_audit_e2e.py`, qui réimporte `plugboard.app`, garde son rang), plus les fichiers propres à la tâche. Un module du paquet est sur le chemin de **chaque** requête admin et client : la liste est la même pour toutes les tâches de déplacement.
- Lint de chaque tâche : `$RUFF check plugboard/ tests/` puis `$RUFF format --check plugboard/ tests/` (la CI exige les deux).
- Nettoyage après chaque passe : rien à garder dans `/tmp` ; supprimer les dossiers temporaires créés à la main.

## Review Focus

- **Identité des dépendances** : `_resolve_client` et `_require_admin` ne bougent pas ; tout `Depends` de `plugboard.app` est `getattr(plugboard.app, qualname)` (garde, tâche 1). `tests/test_spec_v7_residuals_parent.py` (`appmod._require_admin in calls`), `tests/test_review_fixes.py` et `tests/test_tnr_sec001_missing_client_id.py` (`dependency_overrides`) l'exercent.
- **Liaison tardive (R13)** : chaque appel d'une fonction du cœur passe par `core.`, même dans son module : `_key_scope` → `core._is_admin_key`, `core._admin_session_nonce`, `core._admin_session_principal`, `core._person_scope` ; `_is_admin_session` → `core._admin_session_principal` ; `_prove_person_code` → `core._client_address`, `core._audit` ; `_effective_admin_key` → `core._ADMIN_KEY`. Un appel direct passerait la suite d'aujourd'hui et casserait un patch de demain ; le test `LATE_BOUND` le refuse.
- **Réimportation de `plugboard.app`** (E-25) : une fermeture du premier module lit la clé admin et l'état du premier module. Épinglé par quatre tests de caractérisation (tâche 1).
- **État** : `_refusal_audit_seen`, `_warned_auto_login_values`, `_REFUSAL_AUDIT_WINDOW_S`, `_REFUSAL_AUDIT_MAX_KEYS` restent dans `app.py`, lus `core.` ; les compteurs de verrouillage restent dans `plugboard.ratelimit` (import local inchangé).
- **Code sensible verbatim** : `hmac.compare_digest` (deux sites), la signature et l'expiration des sessions, l'ordre des contrôles de `_admin_request_auth`, `_person_request_auth`, `_prove_person_code`, `_auto_login_refusal`. Seuls les préfixes `core.`, l'indentation et `ruff format` changent (R8).
- **Effets d'import** : la ligne `auth.auto_login mode=… local_install=…` reste émise par `app.py`, après le câblage d'`auto_login` ; `admin_people` et `admin_sessions` restent importés en tête (R11-bis) ; ordre de chargement figé.

---

## Structure des fichiers

| Fichier | Rôle | Tâche |
|---|---|---|
| `tests/route_snapshot.py` | Sonde des effets d'import : ignore aussi `plugboard.auth_core*` (Q1, une ligne) | 1 |
| `tests/auth_core_snapshot.py` | Calcule dépendances par route, surface de `plugboard.app`, ordre de chargement à l'import ; écrit le gabarit (`python -m tests.auth_core_snapshot`) | 1 |
| `tests/fixtures/auth_core_snapshot.json` | Gabarit figé sur `919f01f` | 1 |
| `tests/test_auth_core_guard.py` | Compare le présent aux deux gabarits ; identité des dépendances | 1, 2 |
| `tests/test_auth_core_split.py` | Invariants du paquet, R13, R14, quatre tests de réimportation | 1, 7 |
| `tests/auth_decisions.py` | Table de décisions d'authentification ; écrit son gabarit (`python -m tests.auth_decisions`) | 2 |
| `tests/fixtures/auth_decisions.json` | Gabarit figé sur `919f01f` | 2 |
| `plugboard/auth_core/__init__.py` | Paquet, docstring du contrat (n'importe aucun sous-module) | 3 |
| `plugboard/auth_core/scopes.py`, `sessions.py`, `admin.py`, `auto_login.py` | Un module par sujet | 3–6 |
| `plugboard/app.py` | Blocs remplacés par `register()` ; 42 ré-exports ; imports de la bibliothèque standard devenus inutiles retirés | 3–7 |
| `docs/ARCHITECTURE.md`, `CHANGELOG.md` | Où vit le cœur d'authentification ; la nouvelle garde | 7 |

## Procédure commune de déplacement (tâches 3 à 6)

Chaque tâche de module applique ces étapes ; la tâche donne ce qui lui est propre. Les numéros de ligne sont ceux de `919f01f`, pour s'orienter : repérer les blocs par leurs noms.

- **P1 — En-tête du module** : docstring (sujet ; « moved verbatim from `plugboard/app.py` (PLU-97), behaviour unchanged » ; `core` est le module `plugboard.app` qui enregistre ; tout nom défini ou ré-exporté par `app.py` se lit `core.<nom>` à l'appel, même une fonction du même module, R13), `from __future__ import annotations`, imports, `log = logging.getLogger("plugboard.app")` si le module journalise.
- **P2 — Niveau module** : les éléments listés « niveau module », texte et commentaires verbatim. Tout nom employé dans une annotation (`Request`, `Optional`, `Dict`, `Any`, `Tuple`) est une globale du module (R5).
- **P3 — `register()`** : `def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:` puis, dans l'ordre du fichier d'origine, les fermetures listées (signature, docstring et corps verbatim), avec `core.<nom>` pour chaque nom de la ligne « via `core.` ». Fin : `return SimpleNamespace(<fermetures>)`, sous la forme `nom=nom` (seule lecture nue tolérée par l'invariant).
- **P4 — Imports du module** : tout autre nom libre vient d'un import de même forme que dans `app.py` (R6) ; une constante d'un autre module du paquet s'importe `from plugboard.auth_core.<module> import <nom>` (R10) ; jamais `plugboard.app`, `sys.modules`, `import_module`, ni import relatif. Au niveau module, seulement des modules déjà importés par `app.py` au niveau module : `plugboard.admin_people`, `plugboard.admin_sessions`, `plugboard.audit` (`as _audit_mod`), `plugboard.apikeys` (R11). Les imports locaux (`admin_mfa`, `plugboard.ratelimit`, `local_login`, `urllib.parse`) restent locaux, à leur place.
- **P5 — `app.py`** : supprimer les éléments déplacés ; laisser en place ceux que la tâche liste « reste » ; à l'endroit indiqué, écrire le câblage donné ; ajouter le module au bloc `from plugboard.auth_core import (…)` (créé tâche 3, **dernier** import de tête, après celui de `chat_path`, juste avant `setup_logging()`), noms triés ; retirer les imports de la **bibliothèque standard** devenus inutiles (`ruff` F401) ; garder un import `plugboard` devenu inutile avec `# noqa: F401  (import order kept, PLU-97)` (R11-bis) et un point de patch avec `# noqa: F401  (patch point read through core, PLU-97)`.
- **P6 — Lint** : `$RUFF format plugboard/auth_core/ plugboard/app.py`, puis `$RUFF check plugboard/ tests/` et `$RUFF format --check plugboard/ tests/` : 0 erreur (F821 signale tout nom oublié).
- **P7 — Relecture du déplacement** : `git diff --color-moved=dimmed-zebra --color-moved-ws=allow-indentation-change -- plugboard/` ; les seules lignes non grisées sont les `core.`, le câblage, les imports, les ré-exports et le reformatage. Puis les deux relectures Opus (contraintes globales).

---

### Task 1: Garde de structure — dépendances, surface de `plugboard.app`, ordre d'import ; invariants et réimportation

**Files:**
- Modify: `tests/route_snapshot.py` (une ligne de `_PROBE`, Q1)
- Create: `tests/auth_core_snapshot.py`, `tests/test_auth_core_guard.py`, `tests/test_auth_core_split.py`, `tests/fixtures/auth_core_snapshot.json` (généré)

**Interfaces:**
- Consumes: `plugboard.app` tel qu'à `919f01f` ; `PATCH_POINTS` de `tests/test_admin_routes_split.py` ; `two_imports`, `_bare_patch_points`, `_app_imports` (via `tests/test_chat_path_split.py` et `tests/test_admin_routes_split.py`).
- Produces: `dependency_calls(app)`, `all_dependency_calls(app)`, `app_surface()`, `import_order()`, `FIXTURE` dans `tests/auth_core_snapshot.py` ; `EXPECTED_MODULES`, `LATE_BOUND`, `MOVED` dans `tests/test_auth_core_split.py`.

- [ ] **Step 1: Ligne de base**

Run: `git -C /srv/dev/worktrees/plugboard/plu97 diff --quiet 919f01f -- plugboard/ tests/ && cd /srv/dev/worktrees/plugboard/plu97 && $PYTEST $(printf '%s\n' tests/test_admin_routes_guard.py tests/test_chat_path_guard.py $PATCHERS $AUTH_TESTS | sort -u)`
Expected: tout passe (sinon s'arrêter : la base n'est pas saine).

- [ ] **Step 2: La sonde de la garde PLU-36 ignore le nouveau paquet (Q1)**

Dans `_PROBE` de `tests/route_snapshot.py`, remplacer
`not name.startswith(("plugboard.admin_routes", "plugboard.chat_path"))`
par
`not name.startswith(("plugboard.admin_routes", "plugboard.chat_path", "plugboard.auth_core"))`
(aucun autre changement ; gabarit non régénéré).

Run: `$PYTEST tests/test_admin_routes_guard.py` — Expected: 3 passed.

*Variante Q1 = non :* sauter ce step ; chaque tâche 3 à 6 régénère `tests/fixtures/admin_routes_snapshot.json` (`$PY -m tests.route_snapshot`) après le câblage et vérifie que `git diff` du gabarit n'ajoute que des lignes `plugboard.auth_core…` dans `import_effects.modules` ; la contrainte « ne change plus » ne vaut plus pour ce gabarit.

- [ ] **Step 3: Écrire `tests/auth_core_snapshot.py`**

```python
"""Snapshot of what moving the auth core out of app.py must not change (PLU-97).

The dependencies of every route (by qualified name), the names plugboard.app
exposes and the order in which a cold import loads the plugboard modules are
frozen in ``tests/fixtures/auth_core_snapshot.json`` and compared by
``tests/test_auth_core_guard.py``. The fixture is written once on the code
before the move, with ``python -m tests.auth_core_snapshot``. The route table,
the admin OpenAPI document and the sorted import effects stay frozen by the
PLU-36 guard; the client routes, exception handlers and middleware by the
PLU-95 guard.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

from fastapi.routing import APIRoute

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "auth_core_snapshot.json"
# Packages whose modules register code on the app: not import effects.
REGISTERING = ("plugboard.admin_routes", "plugboard.chat_path", "plugboard.auth_core")

_PROBE = f"""
import json
import sys

import plugboard.app  # noqa: F401

order = [
    name
    for name in sys.modules
    if name.startswith("plugboard") and not name.startswith({REGISTERING!r})
]
print("ORDER " + json.dumps(order), flush=True)
"""


def _qualified(call) -> str:
    return f"{call.__module__}.{call.__qualname__}"


def _nested(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _nested(d)


def _entries(routes, prefix=""):
    """(path, callables) of every route and include context, routers walked."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            ctx = route.include_context
            yield ctx.prefix, [d.dependency for d in ctx.dependencies]
            yield from _entries(inner.routes, ctx.prefix)
        elif isinstance(route, APIRoute):
            yield prefix + route.path, list(_nested(route.dependant))


def all_dependency_calls(app) -> list:
    """Every dependency callable of the app, nested and include-level ones."""
    return [call for _, calls in _entries(app.routes) for call in calls]


def dependency_calls(app) -> list:
    """[path, [qualified name of each dependency, in order]] per entry."""
    return [[path, [_qualified(c) for c in calls]] for path, calls in _entries(app.routes)]


def app_surface() -> list:
    """Names of plugboard.app that are neither modules nor typing aliases."""
    import plugboard.app as app_module

    return sorted(
        name
        for name, value in vars(app_module).items()
        if not name.startswith("__")
        and not isinstance(value, types.ModuleType)
        and getattr(value, "__module__", None) != "typing"
    )


def import_order() -> list:
    """The plugboard modules a cold import loads, in load order."""
    with tempfile.TemporaryDirectory(prefix="plu97-import-") as tmp:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": tmp,
            "PLUGBOARD_DATA": str(Path(tmp) / "data"),
            "CODEX_HOME": str(Path(tmp) / "codex"),
            "PLUGBOARD_SKIP_DOTENV": "1",
            "PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC": "0",
            "API_KEY": "testkey-conftest",
            "ADMIN_KEY": "adminkey-conftest",
        }
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    for line in proc.stdout.splitlines():
        if line.startswith("ORDER "):
            return json.loads(line[len("ORDER ") :])
    raise AssertionError("the import probe printed no ORDER line")


def main() -> None:
    import tests.conftest  # noqa: F401  (same environment as the suite)
    from plugboard.app import app

    snapshot = {
        "dependency_calls": dependency_calls(app),
        "app_surface": app_surface(),
        "import_order": import_order(),
    }
    FIXTURE.write_text(
        json.dumps(snapshot, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Écrire `tests/test_auth_core_guard.py`**

```python
"""Moving the auth core out of app.py changes nothing (PLU-97): the same
dependencies on every route, by name and by object; no name gone from
plugboard.app; the same plugboard modules loaded at import, in the same order
as 919f01f."""

import json

from tests import auth_core_snapshot

KNOWN_DEPENDENCIES = {"_require_admin", "_resolve_client"}


def _expected(key):
    return json.loads(auth_core_snapshot.FIXTURE.read_text(encoding="utf-8"))[key]


def test_route_dependencies_are_unchanged():
    from plugboard.app import app

    assert auth_core_snapshot.dependency_calls(app) == _expected("dependency_calls")


def test_dependencies_of_the_app_module_are_its_own_objects():
    """Every dependency of plugboard.app is the object the module exposes under
    that name: dependency_overrides and identity checks keep working."""
    import plugboard.app as app_module

    calls = auth_core_snapshot.all_dependency_calls(app_module.app)
    own = [c for c in calls if c.__module__ == "plugboard.app"]
    assert KNOWN_DEPENDENCIES <= {c.__qualname__ for c in own}
    wrong = sorted(
        {c.__qualname__ for c in own if getattr(app_module, c.__qualname__, None) is not c}
    )
    assert wrong == []


def test_no_name_left_plugboard_app():
    gone = sorted(set(_expected("app_surface")) - set(auth_core_snapshot.app_surface()))
    assert gone == []


def test_import_order_is_unchanged():
    assert auth_core_snapshot.import_order() == _expected("import_order")
```

- [ ] **Step 5: Lancer la garde sans gabarit**

Run: `$PYTEST tests/test_auth_core_guard.py`
Expected: 3 FAIL (`FileNotFoundError` sur `auth_core_snapshot.json`), `test_dependencies_of_the_app_module_are_its_own_objects` passe.

- [ ] **Step 6: Écrire le gabarit sur le code actuel**

Run: `git -C /srv/dev/worktrees/plugboard/plu97 diff --quiet 919f01f -- plugboard/ && cd /srv/dev/worktrees/plugboard/plu97 && $PY -m tests.auth_core_snapshot`
Expected: `tests/fixtures/auth_core_snapshot.json` créé ; `dependency_calls` nomme `plugboard.app._require_admin` 102 fois et `plugboard.app._resolve_client` 17 fois (même compte que `admin_routes_snapshot.json`) ; `app_surface` contient `_resolve_client`, `_require_admin`, `_key_scope`, `_auto_login_refusal`, `_refusal_audit_seen` ; `import_order` commence par `plugboard`, contient `plugboard.admin_people` et `plugboard.admin_sessions` avant `plugboard.admin_people_api`, et aucun nom `plugboard.admin_routes`/`chat_path`/`auth_core`.

- [ ] **Step 7: Lancer la garde, puis vérifier qu'elle voit un changement**

Run: `$PYTEST tests/test_auth_core_guard.py` — Expected: 4 passed.

Run: `cd /srv/dev/worktrees/plugboard/plu97 && $PY -c "import tests.conftest, json; from tests import auth_core_snapshot as s; import plugboard.app as m; f = json.loads(s.FIXTURE.read_text()); r = m.app.router.routes; r[2], r[3] = r[3], r[2]; assert s.dependency_calls(m.app) != f['dependency_calls']; del m._key_scope; assert '_key_scope' in set(f['app_surface']) - set(s.app_surface()); print('guard sees both changes')"`
Expected: `guard sees both changes` (changements en mémoire seulement, aucun fichier touché).

- [ ] **Step 8: Écrire `tests/test_auth_core_split.py`**

Docstring : « Invariants of moving the auth core out of app.py (PLU-97) ». Contenu :

```python
import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from tests.test_admin_routes_split import PATCH_POINTS, _app_imports
from tests.test_chat_path_split import _bare_patch_points, two_imports  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
AUTH_CORE_DIR = ROOT / "plugboard" / "auth_core"
AUTH_CORE_PACKAGE = "plugboard.auth_core"
APP_MODULE = "plugboard.app"
EXPECTED_MODULES = frozenset({"admin", "auto_login", "scopes", "sessions"})
# Every name moved, by module: re-exported by app.py, always (R14).
MOVED = {
    "scopes": (
        "_ADMIN_SCOPE", "_ADMIN_KEY_PRINCIPAL", "_person_principal", "_person_scope",
        "_set_principal", "_client_address", "_address_scope", "_key_scope",
        "_person_bearer_scope", "_rate_limit_scope",
    ),
    "sessions": (
        "_STATE_CHANGING_METHODS", "_CROSS_SITE_REFUSED", "_origin_is_this_host",
        "_effective_admin_key", "_is_admin_key", "_create_admin_session_token",
        "_admin_session_nonce", "_admin_session_principal", "_is_admin_session",
        "_is_cross_origin_browser_request", "_refuse_cross_site_session",
        "_should_use_secure_cookie",
    ),
    "admin": (
        "_ADMIN_TOTP_HEADER", "_person_record_is_sound", "_admin_request_auth",
        "_person_request_auth", "_prove_person_code", "_require_mfa_step_up",
    ),
    "auto_login": (
        "_LOOPBACK_HOSTS", "_FLAG_ON", "_FLAG_OFF", "_PROXY_HEADERS",
        "_LOCAL_NAVIGATION_SITES", "_SUSPICIOUS_REFUSALS", "_host_header_name",
        "_is_loopback_address", "_bind_host", "_local_login_proof",
        "_should_audit_refusal", "_auto_login_mode", "_auto_login_refusal",
        "_issue_local_login_token",
    ),
}
# Names defined in (or re-exported by) app.py that the auth core reads: always
# as core.<name>, even a function of the same module (R13). The moved
# functions, and the values that stay in app.py; constants are not listed.
LATE_BOUND = PATCH_POINTS | frozenset(
    {
        "_person_principal", "_person_scope", "_set_principal", "_client_address",
        "_address_scope", "_key_scope", "_person_bearer_scope", "_rate_limit_scope",
        "_origin_is_this_host", "_effective_admin_key", "_is_admin_key",
        "_create_admin_session_token", "_admin_session_nonce",
        "_admin_session_principal", "_is_admin_session",
        "_is_cross_origin_browser_request", "_refuse_cross_site_session",
        "_should_use_secure_cookie", "_person_record_is_sound",
        "_admin_request_auth", "_person_request_auth", "_prove_person_code",
        "_require_mfa_step_up", "_host_header_name", "_is_loopback_address",
        "_bind_host", "_local_login_proof", "_should_audit_refusal",
        "_auto_login_mode", "_auto_login_refusal", "_issue_local_login_token",
        "_ADMIN_COOKIE_NAME", "_ADMIN_SESSION_MAX_AGE", "_COOKIE_SECURE_MODE",
        "_refusal_audit_seen", "_warned_auto_login_values",
    }
)
FIRST_KEY = "first-admin-key-0123456789"  # synthetic
SECOND_KEY = "second-admin-key-0123456789"  # synthetic


def _app_module():
    return importlib.import_module(APP_MODULE)


def _auth_core_modules() -> list:
    """Every submodule of plugboard.auth_core; none before the move."""
    try:
        package = importlib.import_module(AUTH_CORE_PACKAGE)
    except ModuleNotFoundError as exc:
        if exc.name != AUTH_CORE_PACKAGE:
            raise
        return []
    return [
        importlib.import_module(f"{AUTH_CORE_PACKAGE}.{info.name}")
        for info in pkgutil.iter_modules(package.__path__)
    ]


def _short_name(module) -> str:
    return module.__name__.rsplit(".", 1)[-1]


def _request(bearer: str = "", peer: str = "192.0.2.10") -> Request:
    headers = [(b"host", b"testserver")]
    if bearer:
        headers.append((b"authorization", f"Bearer {bearer}".encode("latin-1")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "query_string": b"",
            "client": (peer, 50000),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
        }
    )
```

| Test | Assertion |
|---|---|
| `test_no_auth_core_module_binds_a_patch_point` | `{m.__name__: sorted(PATCH_POINTS & set(vars(m))) …}` sans entrée non vide |
| `test_auth_core_modules_never_import_the_app_module` | pour chaque `AUTH_CORE_DIR.glob("*.py")` trié : `_app_imports(tree)` plus les `ast.ImportFrom` avec `level` est vide |
| `test_each_auth_core_module_registers_through_register` | `list(inspect.signature(m.register).parameters) == ["app", "core"]` |
| `test_auth_core_code_logs_under_the_app_logger` | `m.log is _app_module().log` pour tout module qui a `log` |
| `test_auth_core_modules_are_designed_ones` | `found <= EXPECTED_MODULES` (tâche 7 : `==`) |
| `test_auth_core_reads_app_names_only_through_core` | `names = frozenset(n for n in LATE_BOUND if hasattr(app, n))` ; pour chaque fichier du paquet, `_bare_patch_points(tree, names) == []` (R13 ; docstring : « a bare name would miss a patch of plugboard.app and, after a re-import, read another module ») |
| `test_every_moved_name_is_on_the_app_module` | pour chaque module trouvé `m` et chaque `n` de `MOVED[_short_name(m)]` : `getattr(app, n) is getattr(m, n)` si `n in vars(m)`, sinon `getattr(app, n) is getattr(getattr(app, f"_ac_{_short_name(m)}_ns"), n)` (R14) |
| `test_no_auth_core_module_touches_the_app` | dans chaque fichier du paquet, aucun `ast.Call` dont `func` est un `ast.Attribute` sur le nom `app` avec `attr` dans `{"get", "post", "put", "patch", "delete", "api_route", "include_router", "add_middleware", "middleware", "exception_handler"}` (décorateurs compris) : le paquet ne déclare ni route, ni middleware, ni gestionnaire |

Puis les tests de caractérisation (verts sur `919f01f` ; si la mise en place doit être ajustée pour l'environnement de test, ajuster la mise en place, jamais l'assertion) :

```python
@pytest.fixture
def two_admin_keys(two_imports, monkeypatch, tmp_path):  # noqa: F811
    """Two imports of plugboard.app, each with its own admin key, MFA off."""
    from plugboard import admin_mfa

    first, second = two_imports
    monkeypatch.setattr(admin_mfa, "_MFA_FILE", tmp_path / "admin_mfa.json")
    monkeypatch.delenv("PLUGBOARD_ADMIN_MFA_REQUIRED", raising=False)
    monkeypatch.setattr(first, "_ADMIN_KEY", FIRST_KEY)
    monkeypatch.setattr(second, "_ADMIN_KEY", SECOND_KEY)
    return first, second


def test_admin_auth_reads_the_module_that_registered_it(two_admin_keys):
    first, _ = two_admin_keys
    assert first._admin_request_auth(_request(FIRST_KEY)) == "ok"
    assert first._admin_request_auth(_request(SECOND_KEY)) == "denied"


def test_rate_limit_scope_reads_the_module_that_registered_it(two_admin_keys):
    first, _ = two_admin_keys
    assert first._rate_limit_scope(_request(FIRST_KEY)) == "key:admin"
    assert first._rate_limit_scope(_request(SECOND_KEY)) == "ip:192.0.2.10"


def test_refusal_audit_state_is_the_registering_module_s(two_imports, monkeypatch):  # noqa: F811
    """The refusal audit reads the state of plugboard.app at call time: a
    patched dict is the one filled, and the other import's stays empty."""
    first, second = two_imports
    monkeypatch.setattr(first, "_refusal_audit_seen", {})
    monkeypatch.setattr(second, "_refusal_audit_seen", {})
    assert first._should_audit_refusal("192.0.2.10", "proxied") is True
    assert first._should_audit_refusal("192.0.2.10", "proxied") is False
    assert list(first._refusal_audit_seen) == [("192.0.2.10", "proxied")]
    assert second._refusal_audit_seen == {}


async def test_admin_route_of_the_first_import_takes_its_own_admin_key(
    two_admin_keys, monkeypatch
):
    import plugboard.ratelimit as rl

    first, _ = two_admin_keys
    monkeypatch.setattr(rl, "_LIMIT", 0)
    async with AsyncClient(
        transport=ASGITransport(app=first.app), base_url="http://test"
    ) as client:
        ok = await client.get("/metrics", headers={"Authorization": f"Bearer {FIRST_KEY}"})
        refused = await client.get(
            "/metrics", headers={"Authorization": f"Bearer {SECOND_KEY}"}
        )
    assert ok.status_code == 200, ok.text
    assert refused.status_code == 401
```

Si l'import de la fixture `two_imports` depuis `tests/test_chat_path_split.py` gêne la collecte (fixture dupliquée), la recopier telle quelle dans le fichier (mise en place).

- [ ] **Step 9: Lancer gardes, invariants et tests qui parcourent les routes**

Run: `$PYTEST $(printf '%s\n' tests/test_admin_routes_guard.py tests/test_auth_core_guard.py tests/test_auth_core_split.py tests/test_chat_path_guard.py $PATCHERS $AUTH_TESTS | sort -u)`
Expected: tout passe, dont les quatre tests de caractérisation (base de comparaison des tâches suivantes) ; les invariants passent à vide (aucun module).

- [ ] **Step 10: Mesurer le temps d'import de référence**

Run (5 fois) : `cd /srv/dev/worktrees/plugboard/plu97 && T=$(mktemp -d) && env -i PATH=/usr/bin:/bin HOME=$T PLUGBOARD_DATA=$T/data PLUGBOARD_SKIP_DOTENV=1 API_KEY=k ADMIN_KEY=adminkey-conftest PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC=0 $PY -X importtime -c 'import plugboard.app' 2>&1 | grep -E '\| plugboard.app$'; rm -rf "$T"`
Expected: une ligne par passe ; noter la médiane du cumul (µs) dans le corps du commit.

- [ ] **Step 11: Lint, relectures Opus, commit**

`$RUFF check plugboard/ tests/` et `$RUFF format --check plugboard/ tests/` : 0 erreur.

```bash
git add tests/route_snapshot.py tests/auth_core_snapshot.py tests/test_auth_core_guard.py tests/test_auth_core_split.py tests/fixtures/auth_core_snapshot.json
git commit -m "test(auth): freeze the route dependencies, app surface and import order (PLU-97)" -m "The PLU-36 import probe also ignores plugboard.auth_core (Q1). Import time of plugboard.app (median of 5, -X importtime): <N> us." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Garde de décisions — table d'authentification figée avant tout déplacement (Q3)

*Variante Q3 = non : tâche sautée ; retirer `tests/auth_decisions.py` et `tests/fixtures/auth_decisions.json` des contraintes globales.*

**Files:**
- Create: `tests/auth_decisions.py`, `tests/fixtures/auth_decisions.json` (généré)
- Modify: `tests/test_auth_core_guard.py` (un test ajouté, fichier créé à la tâche 1)

**Interfaces:**
- Consumes: `plugboard.app` à `919f01f` ; `tests/people_helpers.py` (`isolate`, `new_person`, `code_now`, `wrong`) ; la mise en place des clés client de `tests/test_resolve_client.py` (`isolated_app`, `apikeys.create_key`) ; l'enrôlement MFA de `tests/test_spec_v7_residuals_parent.py` (`_enforce`, `_code`, horloge figée de la fixture `adm`).
- Produces: `CASES`, `FUNCTIONS`, `run_case(case)`, `run_all()`, `FIXTURE` dans `tests/auth_decisions.py`.

- [ ] **Step 1: Écrire `tests/auth_decisions.py`**

Squelette (le constructeur complète `_credentials`, `_request` et `_normalize` avec les aides citées ; il ne change ni `FUNCTIONS` ni les cas de la matrice) :

```python
"""Authentication decisions of the auth core, frozen before the move (PLU-97).

Every case builds a synthetic request; keys, TOTP codes and sessions are made
by the code under test in private files (no real value anywhere). For each
function of FUNCTIONS it records what the function decides (value, or HTTP
status and detail; admin principal; key id) and the audit actions written,
each (case, function) in a fresh world. Written once on 919f01f with
``python -m tests.auth_decisions``, compared by
``tests/test_auth_core_guard.py``; never regenerated during PLU-97.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from tests import people_helpers

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "auth_decisions.json"
ADMIN_KEY = "adminkey-decisions-0123456789"  # synthetic
API_KEY = "apikey-decisions-0123456789"  # synthetic
FUNCTIONS = (
    "_key_scope",
    "_person_bearer_scope",
    "_rate_limit_scope",
    "_resolve_client",
    "_admin_request_auth",
    "_require_admin",
    "_check_dashboard_auth",
    "_refuse_cross_site_session",
    "_require_mfa_step_up",
    "_auto_login_refusal",
)


@dataclasses.dataclass(frozen=True)
class Case:
    name: str
    keys: str = "both"  # "both" | "no_api_key" | "no_admin_key" | "none"
    mfa: str = "off"  # "off" | "required" (admin secret enrolled, flag set)
    bearer: str = ""  # "" | "admin" | "client" | "client_revoked" | "client_unknown"
    #                   | "garbage" | "person" | "person_revoked"
    totp: str = ""  # "" | "admin" | "admin_wrong" | "person" | "person_wrong"
    cookie: str = ""  # "" | "admin" | "admin_no_mfa" | "person" | "person_revoked"
    #                   | "forged" | "expired" | "unregistered"
    method: str = "GET"
    headers: tuple = ()  # extra (name, value): Sec-Fetch-*, Origin, proxy headers
    peer: str = "127.0.0.1"
    host: str = "localhost:8000"
    query: str = ""
    auto_login: str = ""  # PLUGBOARD_LOCAL_AUTO_LOGIN ("" = unset)
    bind_host: str = "127.0.0.1"  # PLUGBOARD_BIND_HOST
    repeat: int = 1  # calls in a row (lockout cases)


CASES = (
    # One Case(...) per row of the matrix in the plan (task 2), in its order.
)


@contextlib.contextmanager
def _world(case: Case):
    """Private stores, a fixed clock, the case's keys and environment, and a
    recorder in place of plugboard.app._audit."""
    import plugboard.app as app_module

    with (
        pytest.MonkeyPatch.context() as mp,
        tempfile.TemporaryDirectory(prefix="plu97-auth-") as tmp,
    ):
        steps = people_helpers.isolate(Path(tmp), mp)
        env = next(steps)
        audit: list = []
        mp.setattr(app_module, "_API_KEY", API_KEY if case.keys in ("both", "no_admin_key") else "")
        mp.setattr(app_module, "_ADMIN_KEY", ADMIN_KEY if case.keys in ("both", "no_api_key") else "")
        mp.setattr(app_module, "_audit", lambda action, **kw: audit.append(action))
        mp.setattr(app_module, "_refusal_audit_seen", {})
        mp.setattr(app_module, "_warned_auto_login_values", set())
        credentials = _credentials(case, env, mp, Path(tmp))
        try:
            yield app_module, credentials, audit
        finally:
            next(steps, None)


def _decide(app_module, name: str, request) -> list:
    try:
        value = getattr(app_module, name)(request)
    except HTTPException as exc:
        outcome = ["http", exc.status_code, exc.detail]
    else:
        outcome = ["ok", value]
    state = request.state
    outcome.append(
        [getattr(state, "key_id", None), getattr(state, "admin_principal", None)]
    )
    return outcome


def run_case(case: Case) -> dict:
    out = {}
    for name in FUNCTIONS:
        with _world(case) as (app_module, credentials, audit):
            results = [
                _decide(app_module, name, _request(case, credentials))
                for _ in range(case.repeat)
            ]
            out[name] = _normalize(
                {"results": results, "audit": list(audit)}, credentials
            )
    return out


def run_all() -> dict:
    return {case.name: run_case(case) for case in CASES}


def main() -> None:
    import tests.conftest  # noqa: F401  (same environment as the suite)

    FIXTURE.write_text(
        json.dumps(run_all(), indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
```

À écrire par le constructeur, sans toucher au reste :
- `_credentials(case, env, mp, tmp)` : magasin de clés client privé (comme `isolated_app`), une clé client active (`client_id` `client-a`), une révoquée, une inconnue (préfixe de clé valide, absente du magasin) ; si le cas l'exige, une personne (`people_helpers.new_person`) et, pour `person_revoked`, sa révocation ; MFA admin enrôlé et `PLUGBOARD_ADMIN_MFA_REQUIRED=1` pour `mfa="required"` (horloge figée comme `adm` de `tests/test_spec_v7_residuals_parent.py`) ; codes TOTP valides et faux (`people_helpers.code_now`, `wrong`) ; cookies : session de la clé admin (`_create_admin_session_token(mfa=…)`), session d'une personne, falsifiée (dernier caractère de la signature changé), expirée (signée avec un `issued_at` plus vieux que `_ADMIN_SESSION_MAX_AGE`), non enregistrée (bien signée, nonce inconnu du magasin) ; variables `PLUGBOARD_LOCAL_AUTO_LOGIN` (supprimée si vide) et `PLUGBOARD_BIND_HOST`.
- `_request(case, credentials)` : une `starlette.requests.Request` neuve à chaque appel (portée `http`, `method`, `path` `/admin/x`, en-têtes `host`, `authorization`, `x-plugboard-admin-totp`, `cookie`, `case.headers`, `query_string`, `client=(case.peer, 50000)`, `server=("127.0.0.1", 8000)`).
- `_normalize(value, credentials)` : remplace dans le JSON toute valeur générée (clés, codes, jetons, `key_id`, identifiant de personne) par un repère (`<client-key-id>`, `<person>`…) ; le gabarit ne doit contenir **aucune** valeur aléatoire ni secrète (deux générations successives donnent le même fichier).

Matrice (≈ 50 cas, tous évalués par les 10 fonctions). En-têtes de navigation locale `NAV` = `(("sec-fetch-site", "none"), ("sec-fetch-mode", "navigate"))`.

| # | `name` | Réglages (champs non nuls) |
|---|---|---|
| 1 | `no_credentials` | — |
| 2 | `admin_key` | `bearer="admin"` |
| 3 | `client_key` | `bearer="client"` |
| 4 | `client_key_revoked` | `bearer="client_revoked"` |
| 5 | `client_key_unknown` | `bearer="client_unknown"` |
| 6 | `garbage_bearer` | `bearer="garbage"` |
| 7 | `no_api_key_anonymous` | `keys="no_api_key"` |
| 8 | `no_api_key_client_key` | `keys="no_api_key", bearer="client"` |
| 9 | `no_api_key_client_unknown` | `keys="no_api_key", bearer="client_unknown"` |
| 10 | `no_api_key_admin_key` | `keys="no_api_key", bearer="admin"` |
| 11 | `keyless_anonymous` | `keys="none"` |
| 12 | `no_admin_key_admin_bearer` | `keys="no_admin_key", bearer="admin"` |
| 13 | `admin_session` | `cookie="admin"` |
| 14 | `admin_session_forged` | `cookie="forged"` |
| 15 | `admin_session_expired` | `cookie="expired"` |
| 16 | `admin_session_unregistered` | `cookie="unregistered"` |
| 17 | `client_key_over_admin_session` | `bearer="client", cookie="admin"` |
| 18 | `person_session` | `cookie="person"` |
| 19 | `person_session_after_revocation` | `cookie="person_revoked"` |
| 20 | `mfa_session_without_mfa` | `mfa="required", cookie="admin_no_mfa"` |
| 21 | `mfa_session_with_mfa` | `mfa="required", cookie="admin"` |
| 22 | `mfa_admin_key_no_code` | `mfa="required", bearer="admin"` |
| 23 | `mfa_admin_key_good_code` | `mfa="required", bearer="admin", totp="admin"` |
| 24 | `mfa_admin_key_wrong_code` | `mfa="required", bearer="admin", totp="admin_wrong"` |
| 25 | `mfa_admin_key_lockout` | `mfa="required", bearer="admin", totp="admin_wrong", repeat=7` |
| 26 | `mfa_step_up_session_no_code` | `mfa="required", cookie="admin"` (lu par `_require_mfa_step_up`) |
| 27 | `mfa_step_up_wrong_code` | `mfa="required", cookie="admin", totp="admin_wrong"` |
| 28 | `person_key_no_code` | `bearer="person"` |
| 29 | `person_key_good_code` | `bearer="person", totp="person"` |
| 30 | `person_key_wrong_code` | `bearer="person", totp="person_wrong"` |
| 31 | `person_key_lockout` | `bearer="person", totp="person_wrong", repeat=7` |
| 32 | `person_key_revoked` | `bearer="person_revoked", totp="person"` |
| 33 | `person_key_without_admin_key` | `keys="no_admin_key", bearer="person", totp="person"` |
| 34 | `person_key_admin_code` | `mfa="required", bearer="person", totp="admin"` |
| 35 | `session_post_cross_site` | `cookie="admin", method="POST", headers=(("sec-fetch-site", "cross-site"),)` |
| 36 | `session_post_same_site` | `cookie="admin", method="POST", headers=(("sec-fetch-site", "same-site"),)` |
| 37 | `session_post_same_origin` | `cookie="admin", method="POST", headers=(("sec-fetch-site", "same-origin"),)` |
| 38 | `session_post_origin_other_host` | `cookie="admin", method="POST", headers=(("origin", "http://other.example"),)` |
| 39 | `session_post_origin_this_host` | `cookie="admin", method="POST", headers=(("origin", "http://localhost:8000"),)` |
| 40 | `admin_key_post_cross_site` | `bearer="admin", method="POST", headers=(("sec-fetch-site", "cross-site"),)` |
| 41 | `auto_login_local_navigation` | `headers=NAV` |
| 42 | `auto_login_off` | `headers=NAV, auto_login="0"` |
| 43 | `auto_login_unknown_value` | `headers=NAV, auto_login="maybe", repeat=2` |
| 44 | `auto_login_public_bind` | `headers=NAV, bind_host="0.0.0.0"` |
| 45 | `auto_login_on_public_bind` | `headers=NAV, auto_login="1", bind_host="0.0.0.0"` |
| 46 | `auto_login_proxied` | `headers=NAV + (("x-forwarded-for", "192.0.2.20"),)` |
| 47 | `auto_login_remote_peer` | `headers=NAV, peer="192.0.2.10"` |
| 48 | `auto_login_remote_host` | `headers=NAV, host="plugboard.example"` |
| 49 | `auto_login_not_navigation` | `headers=(("sec-fetch-site", "none"), ("sec-fetch-mode", "cors"))` |
| 50 | `auto_login_logged_out` | `headers=NAV, query="logged_out=1"` |
| 51 | `auto_login_mfa_required` | `headers=NAV, mfa="required"` |
| 52 | `auto_login_no_admin_key` | `headers=NAV, keys="no_admin_key"` |

`repeat=7` dépasse `plugboard.ratelimit._AUTH_LIMIT` (5) : les résultats successifs montrent le passage au verrouillage. Budget : `run_all()` < 60 s ; au-delà, un monde par cas (compteurs, audit et état remis à zéro entre les fonctions), décision notée dans le corps du commit.

- [ ] **Step 2: Ajouter le test à la garde**

Dans `tests/test_auth_core_guard.py`, ajouter `from tests import auth_decisions` et :

```python
def test_auth_decisions_are_unchanged():
    """The same answer, principal, key id and audit actions as 919f01f for
    every case of the matrix (PLU-97)."""
    expected = json.loads(auth_decisions.FIXTURE.read_text(encoding="utf-8"))
    assert auth_decisions.run_all() == expected
```

Run: `$PYTEST tests/test_auth_core_guard.py` — Expected: 1 FAIL (`FileNotFoundError`), 4 passed.

- [ ] **Step 3: Écrire le gabarit, deux fois**

Run: `git -C /srv/dev/worktrees/plugboard/plu97 diff --quiet 919f01f -- plugboard/ && cd /srv/dev/worktrees/plugboard/plu97 && D=$(mktemp -d) && $PY -m tests.auth_decisions && cp tests/fixtures/auth_decisions.json "$D/first.json" && $PY -m tests.auth_decisions && cmp "$D/first.json" tests/fixtures/auth_decisions.json; echo "rc=$?"; rm -rf "$D"`
Expected: `rc=0` (deux générations identiques : le gabarit est déterministe). Relire le gabarit : aucun secret, aucune valeur aléatoire ; au moins un `["http", 401, …]`, un `["http", 403, "Cross-site request refused"]`, un `["http", 429, …]`, un `"locked"`, un refus de connexion automatique par raison (`disabled`, `not_local_install`, `proxied`, `non_loopback_peer`, `non_loopback_host`, `not_navigation`, `logged_out`, `mfa_required`, `no_admin_key`) et des actions d'audit (`auth.mfa_failed`, `auth.person.mfa_failed`, `admin.person.confirmed`, `admin.mfa.step_up_missing`, `admin.mfa.step_up_failed`). Supprimer le dossier temporaire.

- [ ] **Step 4: Lancer la garde, puis vérifier qu'elle voit un changement**

Run: `$PYTEST tests/test_auth_core_guard.py` — Expected: 5 passed.

Run: `cd /srv/dev/worktrees/plugboard/plu97 && $PY -c "import tests.conftest, json; from tests import auth_decisions as d; import plugboard.app as m; f = json.loads(d.FIXTURE.read_text()); m._ADMIN_SESSION_MAX_AGE = -1; assert d.run_all() != f; print('guard sees a session change')"`
Expected: `guard sees a session change` (en mémoire seulement).

- [ ] **Step 5: Tests ciblés, lint, relectures Opus, commit**

Run: `$PYTEST $(printf '%s\n' $GUARDS $PATCHERS $AUTH_TESTS | sort -u)` — Expected: tout passe.
Lint : 0 erreur.

```bash
git add tests/auth_decisions.py tests/fixtures/auth_decisions.json tests/test_auth_core_guard.py
git commit -m "test(auth): freeze the authentication decisions before the move (PLU-97)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Module `scopes` — principal d'audit, adresse du client, compteurs de limite et de verrouillage

**Files:**
- Create: `plugboard/auth_core/__init__.py`, `plugboard/auth_core/scopes.py`
- Modify: `plugboard/app.py` (bloc 1040–1155 ; bloc d'import)

**Interfaces:**
- Consumes: `core` = module `plugboard.app` ; `_THIS_MODULE` (l. 659).
- Produces: `scopes.register(app, core) -> SimpleNamespace(_client_address, _address_scope, _key_scope, _person_bearer_scope, _rate_limit_scope)` ; niveau module : `_ADMIN_SCOPE`, `_ADMIN_KEY_PRINCIPAL` (importés par `sessions` et `admin`, R10), `_person_principal`, `_person_scope`, `_set_principal`.

- [ ] **Step 1: Créer le paquet et le module** (P1–P4)

`plugboard/auth_core/__init__.py` : docstring seule (le contrat `register(app, core)` ; R13 : tout nom défini ou ré-exporté par `app.py` se lit `core.<nom>`, même une fonction du même module ; R14 : `app.py` ré-exporte chaque nom ; logger `plugboard.app` ; jamais d'import de `plugboard.app` ; les dépendances `_resolve_client` et `_require_admin` restent dans `app.py` ; renvoi à la conception) ; aucun import.
`scopes.py` :
- Niveau module : `_ADMIN_SCOPE`, `_ADMIN_KEY_PRINCIPAL`, `_person_principal`, `_person_scope` (lit `_ADMIN_SCOPE` du module), `_set_principal`.
- Dans `register()`, dans l'ordre d'origine : `_client_address` (point de patch : fermeture, R5), `_address_scope`, `_key_scope`, `_person_bearer_scope`, `_rate_limit_scope`.
- Via `core.` : `_client_address` (dans `_address_scope`, R13), `_is_admin_key`, `get_key_record`, `_ADMIN_COOKIE_NAME`, `_admin_session_nonce`, `_admin_session_principal`, `_person_scope`, `_effective_admin_key`, `_key_scope`, `_person_bearer_scope`, `_address_scope`.
- Direct : `_ADMIN_SCOPE`, `_ADMIN_KEY_PRINCIPAL` (constantes du module), `KEY_PREFIXES`, `admin_people.*`, `_audit_mod.set_principal`.
- Imports : `ModuleType`, `SimpleNamespace`, `Any`, `Dict`, `FastAPI`, `Request`, `from plugboard import admin_people`, `from plugboard import audit as _audit_mod`, `from plugboard.apikeys import KEY_PREFIXES`.

- [ ] **Step 2: Câbler `app.py`** (P5)

Dernier import de tête, après le bloc `from plugboard.chat_path import (…)` :

```python
from plugboard.auth_core import scopes as _ac_scopes
```

À la place du bloc 1040–1155 (entre `_RequestIdMiddleware` et `_RateLimitMiddleware`) :

```python
# -- Audit principal, limit and lockout scopes: auth_core/scopes.py ---------
_ac_scopes_ns = _ac_scopes.register(app, _THIS_MODULE)
_ADMIN_SCOPE = _ac_scopes._ADMIN_SCOPE
_ADMIN_KEY_PRINCIPAL = _ac_scopes._ADMIN_KEY_PRINCIPAL
_person_principal = _ac_scopes._person_principal
_person_scope = _ac_scopes._person_scope
_set_principal = _ac_scopes._set_principal
_client_address = _ac_scopes_ns._client_address
_address_scope = _ac_scopes_ns._address_scope
_key_scope = _ac_scopes_ns._key_scope
_person_bearer_scope = _ac_scopes_ns._person_bearer_scope
_rate_limit_scope = _ac_scopes_ns._rate_limit_scope
```

`get_key_record` et `KEY_PREFIXES` restent importés (lus par `_resolve_client`). `admin_people` reste utilisé (sessions, admin).

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST $(printf '%s\n' $GUARDS $PATCHERS $AUTH_TESTS | sort -u)`
Expected: tout passe, gardes, table de décisions et `test_rate_limit_scope_reads_the_module_that_registered_it` comprises.

- [ ] **Step 5: Relire (P7, relectures Opus) puis commit**

```bash
git add plugboard/auth_core/__init__.py plugboard/auth_core/scopes.py plugboard/app.py
git commit -m "refactor(auth): move the audit principal and the limit scopes to auth_core/scopes.py (PLU-97)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Module `sessions` — clé admin, sessions signées, refus inter-sites, cookie sécurisé

**Files:**
- Create: `plugboard/auth_core/sessions.py`
- Modify: `plugboard/app.py` (bloc 1245–1442 ; imports)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE` ; `_ADMIN_KEY_PRINCIPAL` de `scopes` (R10).
- Produces: `sessions.register(app, core) -> SimpleNamespace(_effective_admin_key, _is_admin_key, _create_admin_session_token, _admin_session_nonce, _admin_session_principal, _is_admin_session, _is_cross_origin_browser_request, _refuse_cross_site_session, _should_use_secure_cookie)` ; niveau module : `_STATE_CHANGING_METHODS`, `_CROSS_SITE_REFUSED`, `_origin_is_this_host`.

- [ ] **Step 1: Créer `sessions.py`** (P1–P4)

- Niveau module : le commentaire PB-SEC-06, `_STATE_CHANGING_METHODS`, `_CROSS_SITE_REFUSED`, `_origin_is_this_host` (son import local `urlsplit` reste local).
- Dans `register()`, dans l'ordre d'origine : `_effective_admin_key`, `_is_admin_key`, `_create_admin_session_token`, `_admin_session_nonce`, `_admin_session_principal`, `_is_admin_session`, `_is_cross_origin_browser_request`, `_refuse_cross_site_session`, `_should_use_secure_cookie`.
- Via `core.` : `_ADMIN_KEY` (`_effective_admin_key`), `_effective_admin_key` (dans `_is_admin_key`, `_create_admin_session_token`, `_admin_session_nonce`), `_ADMIN_SESSION_MAX_AGE`, `_admin_session_nonce` (dans `_admin_session_principal`), `_admin_session_principal` (dans `_is_admin_session`), `_person_principal`, `_origin_is_this_host`, `_is_cross_origin_browser_request`, `_is_admin_key`, `_is_admin_session`, `_ADMIN_COOKIE_NAME` (dans `_refuse_cross_site_session`), `_COOKIE_SECURE_MODE`.
- Direct : `_ADMIN_KEY_PRINCIPAL` (`from plugboard.auth_core.scopes import _ADMIN_KEY_PRINCIPAL`), `_STATE_CHANGING_METHODS`, `_CROSS_SITE_REFUSED`, `hmac`, `hashlib`, `secrets`, `time`, `admin_sessions`, `admin_people`, `log`.
- Imports : `hashlib`, `hmac`, `logging`, `secrets`, `time`, `ModuleType`, `SimpleNamespace`, `Any`, `Dict`, `Optional`, `Tuple`, `FastAPI`, `HTTPException`, `Request`, `status`, `from plugboard import admin_people`, `from plugboard import admin_sessions`, `from plugboard.auth_core.scopes import _ADMIN_KEY_PRINCIPAL` ; l'import local `from plugboard import admin_mfa as _mfa` reste dans `_create_admin_session_token` et `_admin_session_principal`.

- [ ] **Step 2: Câbler `app.py`** (P5)

Ajouter `sessions as _ac_sessions` au bloc d'import (qui devient `from plugboard.auth_core import (scopes as _ac_scopes, sessions as _ac_sessions)`, mis en forme par ruff). Sous le titre « -- Auth » (qui reste), à la place du bloc 1245–1442 :

```python
# -- Admin key, signed sessions, cross-site refusal: auth_core/sessions.py ---
_ac_sessions_ns = _ac_sessions.register(app, _THIS_MODULE)
_STATE_CHANGING_METHODS = _ac_sessions._STATE_CHANGING_METHODS
_CROSS_SITE_REFUSED = _ac_sessions._CROSS_SITE_REFUSED
_origin_is_this_host = _ac_sessions._origin_is_this_host
_effective_admin_key = _ac_sessions_ns._effective_admin_key
_is_admin_key = _ac_sessions_ns._is_admin_key
_create_admin_session_token = _ac_sessions_ns._create_admin_session_token
_admin_session_nonce = _ac_sessions_ns._admin_session_nonce
_admin_session_principal = _ac_sessions_ns._admin_session_principal
_is_admin_session = _ac_sessions_ns._is_admin_session
_is_cross_origin_browser_request = _ac_sessions_ns._is_cross_origin_browser_request
_refuse_cross_site_session = _ac_sessions_ns._refuse_cross_site_session
_should_use_secure_cookie = _ac_sessions_ns._should_use_secure_cookie
```

Imports : retirer `hashlib`, `hmac` et `Tuple` (bibliothèque standard, plus d'usage) ; `from plugboard import admin_sessions` reste avec `# noqa: F401  (import order kept, PLU-97)` (R11-bis).

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST $(printf '%s\n' $GUARDS $PATCHERS $AUTH_TESTS | sort -u)`
Expected: tout passe, dont `test_admin_auth_reads_the_module_that_registered_it`, la table de décisions (sessions falsifiées, expirées, non enregistrées, inter-sites) et `test_import_order_is_unchanged`.

- [ ] **Step 5: Relire (P7, relectures Opus) puis commit**

Vérifier en particulier : `grep -c "hmac.compare_digest" plugboard/auth_core/sessions.py` → 2 ; `grep -c "compare_digest" plugboard/app.py` → 0.

```bash
git add plugboard/auth_core/sessions.py plugboard/app.py
git commit -m "refactor(auth): move the admin key, signed sessions and cross-site refusal to auth_core/sessions.py (PLU-97)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Module `admin` — authentification admin d'une requête, code d'une personne, ré-authentification MFA

**Files:**
- Create: `plugboard/auth_core/admin.py`
- Modify: `plugboard/app.py` (bloc 1570–1751 ; imports)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE` ; `_ADMIN_SCOPE`, `_ADMIN_KEY_PRINCIPAL` de `scopes` (R10).
- Produces: `admin.register(app, core) -> SimpleNamespace(_admin_request_auth, _person_request_auth, _prove_person_code, _require_mfa_step_up)` ; niveau module : `_ADMIN_TOTP_HEADER`, `_person_record_is_sound`.

- [ ] **Step 1: Créer `admin.py`** (P1–P4)

- Niveau module : `_ADMIN_TOTP_HEADER`, `_person_record_is_sound`.
- Dans `register()`, dans l'ordre d'origine : `_admin_request_auth`, `_person_request_auth`, `_prove_person_code`, `_require_mfa_step_up`.
- Via `core.` : `_admin_session_principal`, `_ADMIN_COOKIE_NAME`, `_set_principal`, `_is_admin_key`, `_person_request_auth` (dans `_admin_request_auth`, R13), `_client_address`, `_audit`, `_effective_admin_key`, `_person_record_is_sound`, `_prove_person_code` (dans `_person_request_auth`, R13), `_person_principal`, `_person_scope`.
- Direct : `_ADMIN_SCOPE`, `_ADMIN_KEY_PRINCIPAL` (`from plugboard.auth_core.scopes import …`), `_ADMIN_TOTP_HEADER`, `admin_people.*`, `log`.
- Imports : `logging`, `ModuleType`, `SimpleNamespace`, `Any`, `Dict`, `Optional`, `FastAPI`, `HTTPException`, `Request`, `status`, `from plugboard import admin_people`, `from plugboard.auth_core.scopes import _ADMIN_KEY_PRINCIPAL, _ADMIN_SCOPE` ; les imports locaux (`admin_mfa as _mfa`, `from plugboard.ratelimit import check_auth_rate_limit, record_auth_failure`) restent à leur place.
- Reste dans `app.py`, à sa place : `_require_admin`, juste après le câblage.

- [ ] **Step 2: Câbler `app.py`** (P5)

Ajouter `admin as _ac_admin` au bloc d'import. Entre `_resolve_client` et `_require_admin` :

```python
# -- Admin request auth, person codes, MFA step-up: auth_core/admin.py -------
_ac_admin_ns = _ac_admin.register(app, _THIS_MODULE)
_ADMIN_TOTP_HEADER = _ac_admin._ADMIN_TOTP_HEADER
_person_record_is_sound = _ac_admin._person_record_is_sound
_admin_request_auth = _ac_admin_ns._admin_request_auth
_person_request_auth = _ac_admin_ns._person_request_auth
_prove_person_code = _ac_admin_ns._prove_person_code
_require_mfa_step_up = _ac_admin_ns._require_mfa_step_up
```

`from plugboard import admin_people` n'a plus d'usage : il reste avec `# noqa: F401  (import order kept, PLU-97)` (R11-bis). `_audit` reste importé sans `noqa` (point de patch, encore appelé par `_require_guard_allows`, l. 2144).

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST $(printf '%s\n' $GUARDS $PATCHERS $AUTH_TESTS | sort -u)`
Expected: tout passe, dont la table de décisions (MFA, clés personnelles, verrouillage, ré-authentification), `tests/test_audit_principal.py` (patch de `_admin_request_auth`) et `tests/test_admin_people_mfa_routes.py` (patch de `_client_address`, lu par `_prove_person_code`).

- [ ] **Step 5: Relire (P7, relectures Opus) puis commit**

```bash
git add plugboard/auth_core/admin.py plugboard/app.py
git commit -m "refactor(auth): move the admin request authentication and MFA step-up to auth_core/admin.py (PLU-97)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Module `auto_login` — connexion automatique locale

**Files:**
- Create: `plugboard/auth_core/auto_login.py`
- Modify: `plugboard/app.py` (bloc 2267–2458)

**Interfaces:**
- Consumes: `core`, `_THIS_MODULE`.
- Produces: `auto_login.register(app, core) -> SimpleNamespace(_should_audit_refusal, _auto_login_mode, _auto_login_refusal, _issue_local_login_token)` ; niveau module : `_LOOPBACK_HOSTS`, `_FLAG_ON`, `_FLAG_OFF`, `_PROXY_HEADERS`, `_LOCAL_NAVIGATION_SITES`, `_SUSPICIOUS_REFUSALS`, `_host_header_name`, `_is_loopback_address`, `_bind_host`, `_local_login_proof`.

- [ ] **Step 1: Créer `auto_login.py`** (P1–P4)

- Niveau module, avec leurs commentaires : `_LOOPBACK_HOSTS`, `_FLAG_ON`, `_FLAG_OFF`, `_PROXY_HEADERS`, `_LOCAL_NAVIGATION_SITES`, `_SUSPICIOUS_REFUSALS` (le commentaire « Refusals that mean someone claimed to be local… » part avec lui) ; `_host_header_name`, `_is_loopback_address`, `_bind_host`, `_local_login_proof` (import local `local_login` à sa place).
- Dans `register()`, dans l'ordre d'origine : `_should_audit_refusal`, `_auto_login_mode`, `_auto_login_refusal`, `_issue_local_login_token`.
- Via `core.` : `_refusal_audit_seen`, `_REFUSAL_AUDIT_WINDOW_S`, `_REFUSAL_AUDIT_MAX_KEYS` (`_should_audit_refusal`), `_warned_auto_login_values` (`_auto_login_mode`, mutation en place), `_auto_login_mode` (dans `_auto_login_refusal` et `_issue_local_login_token`, R13), `_effective_admin_key`, `_is_loopback_address`, `_bind_host`, `_host_header_name`.
- Direct : les six constantes du module, `os`, `time`, `ipaddress`, `log`.
- Imports : `ipaddress`, `logging`, `os`, `time`, `ModuleType`, `SimpleNamespace`, `FastAPI`, `Request` ; les imports locaux (`admin_mfa as _mfa`, `local_login`) restent à leur place.
- Reste dans `app.py`, à sa place et dans son ordre : `_REFUSAL_AUDIT_WINDOW_S`, `_REFUSAL_AUDIT_MAX_KEYS`, `_refusal_audit_seen`, `_warned_auto_login_values` (au-dessus du câblage) ; l'appel `log.info("auth.auto_login mode=%s local_install=%s", …)` (au-dessous du câblage, texte inchangé).

- [ ] **Step 2: Câbler `app.py`** (P5)

Ajouter `auto_login as _ac_auto_login` au bloc d'import. À la place du bloc, après `root()` :

```python
# The refusal audit window, its state and the values already warned about stay
# here (patched by the tests, one copy per import of app.py):
# auth_core/auto_login.py reads them through core (PLU-97).
_REFUSAL_AUDIT_WINDOW_S = 600.0
_REFUSAL_AUDIT_MAX_KEYS = 256
_refusal_audit_seen: Dict[tuple, float] = {}
_warned_auto_login_values: set = set()


# -- Local auto-login: auth_core/auto_login.py ---------------------------------
_ac_auto_login_ns = _ac_auto_login.register(app, _THIS_MODULE)
_LOOPBACK_HOSTS = _ac_auto_login._LOOPBACK_HOSTS
_FLAG_ON = _ac_auto_login._FLAG_ON
_FLAG_OFF = _ac_auto_login._FLAG_OFF
_PROXY_HEADERS = _ac_auto_login._PROXY_HEADERS
_LOCAL_NAVIGATION_SITES = _ac_auto_login._LOCAL_NAVIGATION_SITES
_SUSPICIOUS_REFUSALS = _ac_auto_login._SUSPICIOUS_REFUSALS
_host_header_name = _ac_auto_login._host_header_name
_is_loopback_address = _ac_auto_login._is_loopback_address
_bind_host = _ac_auto_login._bind_host
_local_login_proof = _ac_auto_login._local_login_proof
_should_audit_refusal = _ac_auto_login_ns._should_audit_refusal
_auto_login_mode = _ac_auto_login_ns._auto_login_mode
_auto_login_refusal = _ac_auto_login_ns._auto_login_refusal
_issue_local_login_token = _ac_auto_login_ns._issue_local_login_token


log.info(
    "auth.auto_login mode=%s local_install=%s",
    _auto_login_mode(),
    _is_loopback_address(_bind_host()),
)
```

`ipaddress` reste importé dans `app.py` (`_arrived_on_loopback`).

- [ ] **Step 3: Lint** (P6) — Expected: 0 erreur.

- [ ] **Step 4: Tests ciblés**

Run: `$PYTEST $(printf '%s\n' $GUARDS $PATCHERS $AUTH_TESTS | sort -u)`
Expected: tout passe, dont `tests/test_auth_auto_login.py` (patchs de `_REFUSAL_AUDIT_*`, `_audit`, `.clear()` de l'état), `test_refusal_audit_state_is_the_registering_module_s`, la table de décisions (cas 41–52) et la garde PLU-36 (`import_effects` : même ligne `auth.auto_login …`, même place).

- [ ] **Step 5: Relire (P7, relectures Opus) puis commit**

```bash
git add plugboard/auth_core/auto_login.py plugboard/app.py
git commit -m "refactor(auth): move the local auto-login helpers to auth_core/auto_login.py (PLU-97)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Clôture — documentation, invariant final et contrôles du lot

**Files:**
- Modify: `plugboard/app.py` (docstring de tête seulement), `docs/ARCHITECTURE.md`, `CHANGELOG.md`, `tests/test_auth_core_split.py` (une assertion)

**Interfaces:**
- Consumes: tâches 1 à 6.
- Produces: rien pour le code.

- [ ] **Step 1: Invariant final**

Dans `test_auth_core_modules_are_designed_ones`, remplacer `found <= EXPECTED_MODULES` par `sorted(found) == sorted(EXPECTED_MODULES)` (docstring : « Exactly the modules of the design: none missing, none extra »).

- [ ] **Step 2: Documentation**

- `plugboard/app.py`, docstring de tête, après le paragraphe PLU-95 : « The authentication core behind the `_resolve_client` and `_require_admin` dependencies (audit principal and limit scopes, admin key and sessions, cross-site refusal, admin and personal-key authentication, MFA step-up, local auto-login) lives in `plugboard/auth_core/` (registered in place by `register(app, core)`, PLU-97); both dependencies stay here. »
- `docs/ARCHITECTURE.md` : schéma, ligne `app.py` → « auth dependencies (client key, admin key/session) · middlewares · chat admission (concurrency cap, session turn) · health · dashboard pages », nouvelle ligne `auth_core/` → « admin key and sessions · personal keys and MFA · limit scopes · local auto-login » ; tableau « Modules », ligne `app.py` → « Authentication dependencies (`_resolve_client`, `_require_admin`), middlewares, chat admission (concurrency cap, session turn), health, contract and dashboard pages; registers the admin routes, the chat path and the auth core » ; nouvelle ligne `auth_core/` → « Authentication core behind those dependencies, one module per subject (audit principal and limit scopes, admin key and sessions with the cross-site refusal, admin and personal-key request authentication with MFA step-up, local auto-login), each declared by `register(app, core)` ». Section « Route table guard » : un paragraphe sur `tests/test_auth_core_guard.py` (dépendances de chaque route par nom et par objet, surface de `plugboard.app`, ordre de chargement à l'import, table de décisions d'authentification ; même règle : régénérer par `.venv/bin/python -m tests.auth_core_snapshot` ou `-m tests.auth_decisions` seulement pour un changement voulu, diff relu) ; ajouter `plugboard/auth_core/` à la liste des modules qui ne comptent pas dans les effets d'import.
- `CHANGELOG.md`, sous `## [Unreleased]`, section `### Changed` : « **Authentication core moved out of `app.py`** (PLU-97): one module per subject under `plugboard/auth_core/`; the `_resolve_client` and `_require_admin` dependencies stay in `app.py`; every authentication decision, route, answer and both OpenAPI documents are unchanged. »

- [ ] **Step 3: Contrôles du lot**

Run, depuis `/srv/dev/worktrees/plugboard/plu97` :
- `wc -l plugboard/app.py` → Expected: entre 1 880 et 1 980 (≈ 1 920).
- `wc -l plugboard/auth_core/*.py` → Expected: total ≈ 855 (ordre de grandeur ; un écart de plus de 15 % s'explique dans le corps du commit).
- `grep -nE '^def (_resolve_client|_require_admin|_get_request_key_record|_apply_key_record|_check_dashboard_auth)\(' plugboard/app.py` → Expected: 5 lignes (les dépendances et leur voisinage n'ont pas bougé).
- `git diff --name-status 919f01f -- tests/ | grep -v '^A'` → Expected: `M	tests/route_snapshot.py` seulement (Q1 = non : `M	tests/fixtures/admin_routes_snapshot.json` à la place).
- `git diff --exit-code $(git log --format=%H --grep='freeze the authentication decisions' -1) -- tests/route_snapshot.py tests/auth_core_snapshot.py tests/auth_decisions.py tests/test_auth_core_guard.py tests/fixtures/auth_core_snapshot.json tests/fixtures/auth_decisions.json tests/fixtures/admin_routes_snapshot.json tests/fixtures/chat_path_snapshot.json` → Expected: code 0.
- `git diff --quiet 919f01f -- plugboard/admin_routes/ plugboard/chat_path/ plugboard/api_contract.py docs/api/` → Expected: code 0 ; `grep -n 'CONTRACT_VERSION = ' plugboard/api_contract.py` → `"3.0.1"`.
- `grep -rn "compare_digest" plugboard/app.py plugboard/auth_core/` → Expected: deux lignes, toutes deux dans `plugboard/auth_core/sessions.py`.
- `T=$(mktemp -d) && PLUGBOARD_DATA=$T PLUGBOARD_SKIP_DOTENV=1 $PY scripts/export_contract.py --check && PLUGBOARD_DATA=$T PLUGBOARD_SKIP_DOTENV=1 $PY scripts/export_contract.py --mcp --check; rm -rf "$T"` → Expected: `docs/api/openapi.json: up to date`, `docs/api/mcp.json: up to date`.
- Temps d'import : la commande de la tâche 1, step 10, 5 fois → Expected: médiane ≤ référence + 10 %.

- [ ] **Step 4: Tests ciblés, lint**

Run: `$PYTEST $(printf '%s\n' $GUARDS $PATCHERS $AUTH_TESTS tests/test_cli_versions_single_source.py tests/test_docs_links.py tests/test_no_remote_ci.py | sort -u)` puis `$RUFF check plugboard/ tests/` et `$RUFF format --check plugboard/ tests/`.
Expected: tout passe ; 0 erreur.

- [ ] **Step 5: Commit**

```bash
git add plugboard/app.py docs/ARCHITECTURE.md CHANGELOG.md tests/test_auth_core_split.py
git commit -m "docs(auth): say where the auth core lives (PLU-97)" -m "Final invariant: exactly the designed auth_core modules. app.py: <N> lines. Import time of plugboard.app (median of 5, -X importtime): <N> us." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Relecture finale Opus de la branche**

Un sous-agent `claude-opus-5-5`, contexte : la conception, ce plan, `git diff 919f01f..HEAD`. Mandat : tout écart de comportement (décision, ordre des contrôles, journal, audit, compteur, effet d'import), tout appel du cœur qui ne passe pas par `core.`, tout nom de `plugboard.app` disparu. Constat bloquant : nouveau commit, puis passe ciblée et relecture du correctif. Fable seulement pour arbitrer deux relectures Opus contradictoires.

---

## Jalon — validation complète unique, puis une CI (hors tâches)

À lancer une fois, sur le dernier commit du lot, depuis `/srv/dev/worktrees/plugboard/plu97`, avec la procédure du jalon de PLU-36 (`docs/superpowers/plans/2026-11-18-plu-36-admin-routes.md`, section « Jalon »), chemins adaptés à `plu97` :

1. **Charge** : `uptime` (charge < 10), `free -g`, `ci etat` (aucune CI `plugboard` en cours) ; au plus deux suites lourdes à la fois (les quatre blocs Python comptent pour une, le couloir e2e pour la seconde).
2. **Lancement simultané** : quatre blocs Python contigus (`split -n l/4` sur la liste triée de `tests/test_*.py`, `tests/integration` et `tests/live_model_tests` ajoutés au dernier), chacun avec son `PLUGBOARD_DATA` ; `ruff check` + `ruff format --check` ; `export_contract.py --check` et `--mcp --check` ; le couloir e2e isolé (son venv, son `PLUGBOARD_DATA`, son port), qui couvre `tests/e2e/test_logout_all.py` et `tests/e2e/test_people.py`. Expected : chaque code de retour vaut 0.
3. **En cas d'échec** : corriger par un nouveau commit, rejouer **seulement** les nœuds en échec ; jamais un test assoupli ou sauté.
4. **Une seule CI**, une fois tout vert : `ci lancer` depuis l'atelier, suivi `ci etat`, `ci journal plugboard <sha>`. Rouge par le produit : `board bug ajouter "…"`, corriger, rejouer la partie fautive, relancer la CI une fois.
5. **Nettoyage** : supprimer le dossier temporaire du jalon ; rien d'autre à garder.
6. **Board** : `board ticket statut PLU-97 revue` (depuis `/srv/dev/worktrees/plugboard/pilotage-board`) avec le SHA et le lien du rapport CI.

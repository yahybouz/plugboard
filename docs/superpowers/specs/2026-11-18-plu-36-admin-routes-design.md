# Sortir les routes d'administration de `app.py` (PLU-36) — conception

Date : 2026-11-18. Ticket : PLU-36. Décision du propriétaire : D-67.
Base : `main` à `672118e`, branche `refactor/plu-36-admin-routes`.

## Intention

`plugboard/app.py` fait 8 172 lignes : 118 routes `@app.<méthode>`, dont 81 sous
`/admin`, mêlées au chemin de chat, à l'authentification et aux pages. Le ticket
demande d'en sortir les routes d'administration, **sans aucun changement de
comportement**, pour qu'un domaine d'administration se lise et se relise dans un
fichier à lui.

## Décidé par le propriétaire (D-67)

- Comportement strictement inchangé : mêmes routes, même ordre, mêmes réponses,
  mêmes dépendances, même document OpenAPI admin, même import.
- La suite de tests existante est le filet ; aucun test existant n'est modifié,
  assoupli, sauté ni supprimé.
- Tests ciblés pendant la construction ; **une seule** validation complète à la
  fin du lot, puis une seule CI (règle YB du 18/11).

## Objectifs

- Chaque domaine d'administration dans son module sous `plugboard/admin_routes/`.
- Un garde-fou écrit **avant** tout déplacement, figé sur le code actuel : table
  des routes, document OpenAPI admin, effets de l'import.
- `CONTRACT_VERSION` (`3.0.1`), `docs/api/openapi.json` et `docs/api/mcp.json`
  inchangés.

## Non-objectifs

- Toute correction, renommage de route, de handler ou de message.
- Le chemin de chat (`chat_completions` et ses aides), le cœur d'authentification
  (`_require_admin`, `_resolve_client`, sessions), les middlewares, le `lifespan`,
  les pages (`/`, `/dashboard`, `/docs`…) : ils restent dans `app.py`.
- Changer un test existant pour suivre le code.

## Anti-objectifs (un échec même si « ça marche »)

- Une route qui change de place dans la table, de dépendance ou de signature.
- Un test qui passe encore mais ne vérifie plus rien (couverture perdue en silence).
- Un `monkeypatch` de `plugboard.app.<nom>` qui n'atteint plus le code déplacé.
- Une importation circulaire, ou un effet de bord nouveau à l'import.

## Constats (mesurés sur `672118e`)

1. **FastAPI 0.143.3 garde les routeurs inclus derrière un `_IncludedRouter`.**
   `app.routes` compte 122 entrées : 118 `APIRoute` et 4 `_IncludedRouter`
   (`decision`, `embeddings`, `suivi`, `people`).
2. **Deux tests ne parcourent que le premier niveau de `app.routes`.**
   `tests/test_spec_v7_residuals_parent.py::test_8_every_admin_like_route_requires_admin`
   et `::test_8_bearer_alone_refused_on_all_admin_routes` ignorent toute entrée
   sans `dependant`. Une route passée dans un `APIRouter` inclus disparaîtrait de
   leur inventaire : ils resteraient verts sans plus la vérifier.
3. **Les tests patchent 51 noms sur `plugboard.app`** (308 endroits, 87 fichiers ;
   `setattr`, `patch.object`, cibles texte `"plugboard.app.X"`). 20 d'entre eux
   sont lus par du code à déplacer (tableau plus bas). 56 autres noms sont lus
   directement, et 6 constantes sont lues **par leur nom** dans un
   sous-processus (`tests/test_env_numbers_at_start.py`, `module:attr`, dont 4
   déjà patchées). Le code produit lit aussi `_spending_scope`,
   `_check_spending_limit` et `_quota_error_response` sur `plugboard.app`
   (`decision_api.py`, `embeddings_api.py`) : ils ne bougent pas.
4. **La suite réimporte `plugboard.app`.**
   `tests/test_audit_e2e.py::TestStrictTenantBootGuard::test_e25_strict_tenant_with_empty_api_key_raises`
   retire `plugboard.app` de `sys.modules` et le réimporte. Les fichiers de test
   collectés avant gardent l'**ancien** objet module (`from plugboard import app
   as app_module` en tête de fichier) ; ceux qui importent dans une fixture
   reçoivent le **nouveau**. Exemple : `tests/test_internet_check_local_models.py`
   (trié après `test_audit_e2e.py`) patche l'ancien module puis appelle
   `app_module.check_all_models_internet_access`, qui doit lire l'ancien.
   Aujourd'hui chaque handler lit les globales du module qui l'a défini : la
   conception doit garder exactement cela.
5. Aucun code ne dépend de l'identité d'un endpoint (`__module__`, `__qualname__`,
   `scope["endpoint"]`) ; le logger `plugboard.app` donne `source_service` aux
   journaux ; `app.py` utilise `from __future__ import annotations`.
6. L'import de `plugboard.app` écrit deux lignes `INFO` (`auth.mode=…`,
   `auth.auto_login mode=…`), crée `data/.encryption_key` et charge 46 modules
   `plugboard.*` (~1,3 s).

## Approches

| Option | Principe | Verdict |
|---|---|---|
| A — `APIRouter` par domaine, `app.include_router` | Le modèle de `admin_people_api` | **Rejetée** : ajoute un `_IncludedRouter` par module (constat 1), les deux tests du constat 2 perdent la couverture des routes déplacées, et la table des routes change de forme |
| B — handlers au niveau module, noms de `app.py` lus par `sys.modules["plugboard.app"]` | Pas de fermetures | **Rejetée** : après la réimportation (constat 4), un handler de l'ancien module lirait le nouveau ; `test_internet_check_local_models.py` échouerait dans une passe complète |
| **C — retenue** : `register(app, core)` par domaine | Chaque module déclare ses routes avec `@app.<méthode>` dans une fonction `register(app, core)` que `app.py` appelle à la place du bloc ; `core` est **l'objet module `plugboard.app` qui enregistre** | Table des routes identique (mêmes `APIRoute`, même niveau, même ordre) ; chaque import de `app.py` lie ses propres handlers à lui-même, comme aujourd'hui |

## Conception

### Le paquet `plugboard/admin_routes/`

Un module par bloc contigu de routes (l'ordre d'enregistrement doit rester
celui d'aujourd'hui, FastAPI essayant les routes dans l'ordre) :

| Module | Routes (nb) | Bloc à `672118e` |
|---|---|---|
| `model_probes.py` | `/admin/circuits*`, `/admin/models/check-internet*` (4) | 4284–4667 |
| `monitoring.py` | cache, `claude-quota`, `monitoring*`, `watchdog*`, `models/status`, `models/refresh`, `audit`, `/metrics/prometheus` (15) | 4673–4969 |
| `providers.py` | `/providers*` (6) | 4975–5398 |
| `oauth.py` | `/admin/oauth/*`, `/oauth/callback/{source}` (5) | 5404–5633 |
| `direct_providers.py` | `/admin/direct-providers/*` (4) | 5639–6066 |
| `ollama.py` | `/admin/ollama/*` (10) | 6072–6331 |
| `keys.py` | `/admin/keys*`, `/admin/orgs*` (9) | 6337–6576 |
| `jobs.py` | `/admin/jobs*` (3) | 6582–6617 |
| `pricing.py` | `/admin/pricing*`, `models/scheduler`, `auto-update*`, `catalog/history`, `coverage/*` (19) | 6623–7117 |
| `usage.py` | `/admin/usage*`, `/admin/retention` (8) | 7123–7229 |
| `auth.py` | `/login`, `/auth/admin/*`, `/admin/mfa*` (9) | 7598–8078 (+ modèles 7365–7381, gabarit 7289–7362, `_safe_next`) |

92 routes déplacées. Restent dans `app.py` : `/admin/inflight` et `/metrics`
(trois lignes chacune, entre des routes client), les routes client et santé,
`/v1/openapi.json`, `/openapi.json`, `/docs`, `/redoc`, `/`, `/portal`,
`/dashboard*`, `/suivi`, et les quatre routeurs déjà inclus.

### Le contrat d'un module

```python
def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    """Declare this block's routes on ``app``, in their original order."""
```

- Les handlers sont définis **dans** `register()` avec `@app.<méthode>(...)`,
  arguments du décorateur, signature, docstring et corps verbatim.
- `register()` rend un `SimpleNamespace` des seuls noms que `app.py` ré-exporte.
- Le module ne fait jamais `import plugboard.app` ni `sys.modules[...]` : pas
  d'import circulaire, et `core` est le seul accès à `app.py`.

### Règles de déplacement

- **R1 — Part** : les handlers du bloc, ses modèles Pydantic, ses constantes et
  aides qui ne servent qu'au bloc.
- **R2 — Reste dans `app.py`**, même s'il ne sert qu'à un bloc :
  l'état mutable et les verrous (`_BULK_INTERNET_LOCK`, `_MODEL_REFRESH_LOCK`,
  `_LAST_REFRESH_AT`, `_background_tasks`) ; les constantes lues de
  l'environnement à l'import (`_BULK_INTERNET_CONCURRENCY`,
  `_BULK_INTERNET_PER_MODEL_TIMEOUT_S`, `_MODEL_REFRESH_COOLDOWN_S`, lues sur
  `plugboard.app` par `test_env_numbers_at_start.py`) ; les aides partagées par
  deux modules (garde de sortie : `_require_parsable_base_url`,
  `_require_public_host`, `_check_guard_on_edit`, `_require_guard_allows`) ; tout
  ce que le code restant utilise ; les effets d'import et les aides de connexion
  automatique (`_should_audit_refusal`, `_auto_login_refusal`…).
- **R3 — `core.<nom>`** : dans le code déplacé, tout nom resté dans `app.py`, et
  tout nom de la liste des points de patch, s'écrit `core.<nom>`, lu au moment de
  l'appel dans le module qui a enregistré la route (y compris
  `Depends(core._require_admin)`, évalué une fois à l'enregistrement : la
  dépendance reste `plugboard.app._require_admin`).
- **R4 — État écrit** : `global X` devient `core.X = …` (un seul cas :
  `_LAST_REFRESH_AT` dans `admin_models_refresh`).
- **R5 — Fermeture ou module** : une fonction déplacée qui lit `core` est définie
  dans `register()` ; sinon au niveau du module, sauf un point de patch, toujours
  défini dans `register()` (un module déplacé ne lie aucun point de patch à son
  niveau, ce qu'un test vérifie). Tout nom employé dans une
  annotation (modèles Pydantic, `Request`, `Optional`…) est au niveau du module :
  FastAPI résout les annotations-chaînes dans `__globals__` du module
  (`fastapi.dependencies.utils.get_typed_signature`), jamais dans la fermeture.
- **R6 — Autres imports** : un nom qu'`app.py` importait d'un autre module, s'il
  n'est pas un point de patch, est importé par le nouveau module **sous la même
  forme** (`from x import y` reste tel quel ; `_cfg_mod.attr` reste un accès par
  le module). Les imports locaux dans les fonctions ne bougent pas.
- **R7 — Journal** : `log = logging.getLogger("plugboard.app")` dans chaque
  module : même objet logger, même `source_service`.
- **R8 — Verbatim** : seuls changements tolérés dans le code déplacé : préfixes
  `core.`, indentation, R4, mise en forme `ruff format`.
- **R9 — Câblage** : dans `app.py`, l'appel `register()` prend la place de la
  dernière route du bloc ; ce qui reste du bloc (R2) demeure au-dessus, dans
  son ordre. `app.py` ré-exporte (`nom = _ns.nom`) chaque nom que les tests ou
  d'autres modules lisent sur `plugboard.app` et qui l'a quitté ; il retire les
  imports devenus inutiles, sauf les points de patch, gardés avec
  `# noqa: F401` et un commentaire.

### Points de patch touchés par le code déplacé

| Nom (défini à `672118e`) | Sites de test | Lu par | Traitement |
|---|---|---|---|
| `_BULK_INTERNET_CONCURRENCY` (l. 4495) | 1 + lecture `module:attr` | `model_probes` | Reste (R2) ; `core.` |
| `_LAST_REFRESH_AT` (l. 4831) | 1 | `monitoring` | Reste (R2) ; lu et écrit par `core.` (R4) |
| `_MODEL_REFRESH_COOLDOWN_S` (l. 4828) | 1 + lecture `module:attr` | `monitoring` | Reste (R2) ; `core.` |
| `_audit` (import, l. 95) | 3 | tous sauf `usage` | Import gardé ; `core._audit` |
| `_client_address` (l. 1261) | 1 | `auth` | Reste ; `core.` |
| `_is_admin_key` (l. 1449) | 2 | `auth` | Reste ; `core.` |
| `_require_public_host` (l. 5677) | 1 | `direct_providers` | Reste (garde partagée) ; `core.` |
| `_schedule_initial_discovery` (l. 6045) | 1 | `direct_providers` | Part (fermeture) ; ré-exporté ; appelé par `core.` |
| `_test_openai_endpoint` (l. 5759) | 2 | `direct_providers` | Part (dans `register()`) ; ré-exporté ; appelé par `core.` |
| `_usage` (import, l. 93) | 1 | `keys`, `usage` | Import gardé ; `core._usage` |
| `check_all_models_internet_access` (l. 4524) | 2 | `_lifespan` (reste) | Part ; ré-exporté (le `lifespan` le lit sur `app.py`) |
| `check_model_internet_access` (l. 4308) | 2 | `model_probes` | Part ; ré-exporté ; appelé par `core.` depuis la vérification groupée |
| `discover_models` (import) | 6 | `providers`, `direct_providers` | Import gardé ; `core.` |
| `forward` (import) | 36 | `model_probes` | Import gardé ; `core.` |
| `get_provider` (import) | 1 | `providers`, `oauth`, `direct_providers`, `ollama` | Import gardé ; `core.` |
| `invalidate_models_cache` (import) | 2 | `model_probes`, `monitoring`, `providers`, `oauth`, `direct_providers` | Import gardé ; `core.` |
| `list_models` (import) | 4 | `model_probes`, `pricing` | Import gardé ; `core.` |
| `load_providers` (import) | 11 | `model_probes`, `providers`, `keys`, `pricing` | Import gardé ; `core.` |
| `resolve_provider_and_model` (import) | 15 | `model_probes` | Import gardé ; `core.` |
| `update_provider` (import) | 3 | `model_probes`, `providers`, `direct_providers` | Import gardé ; `core.` |

Les 31 autres points de patch ne sont lus que par du code qui reste dans
`app.py` ; ils ne bougent pas : `_ADMIN_KEY`, `_API_KEY`,
`_BUDGET_RESERVE_TOKENS`, `_CACHE_RESPONSE_FLOOR_MS`, `_DEV_MODE`,
`_INFLIGHT_CHAT_MAX`, `_INFLIGHT_WAIT_S`, `_MAX_BODY_BYTES`,
`_REFUSAL_AUDIT_MAX_KEYS`, `_REFUSAL_AUDIT_WINDOW_S`, `_STATIC_DIR`,
`_STATIC_DIR_RESOLVED`, `_admin_request_auth`, `_apply_key_record`,
`_bill_streaming`, `_check_and_alert`, `_check_price_known`,
`_check_stored_secrets`, `_get_request_key_record`, `_hold_budget`,
`_inflight_chat_active`, `_inflight_chat_sem`, `_is_admin_session`,
`_model_is_compatible`, `_model_route_guard`, `_record_session_turn`,
`_send_spending_alert`, `_served_by`, `cli_refusal`, `forward_stream`,
`get_key_record`.

Noms seulement **lus** par les tests et qui partent, donc ré-exportés :
`admin_models_refresh`, `get_providers`, `oauth_disconnect`,
`_default_rpm_limit`, `_default_monthly_limit_usd`, `_suggestion_action`,
`admin_catalog_history` (synchrone : le reste), `_LOGIN_HTML_TEMPLATE`.

### Garde-fou (écrit en premier, figé sur `672118e`)

- `tests/route_snapshot.py` calcule : la table des routes à plat et dans l'ordre
  (type d'entrée, chemin, méthodes, nom, synchrone ou non, `response_model`,
  `status_code`, `response_class`, `include_in_schema`, résumé, description,
  `unique_id`, arbre des dépendances par nom qualifié, paramètres par lieu, nom,
  alias, obligation et type) ; le document `app.openapi()` (version remplacée
  par `<version>`) ; les effets d'un import à froid dans un sous-processus
  (lignes de journal, fichiers créés sous `PLUGBOARD_DATA`, modules
  `plugboard.*` chargés hors `plugboard.admin_routes`). Les types sont nommés
  sans leur chemin de module : un modèle qui change de module ne change pas la
  table, un modèle qui change de forme la change (et le document OpenAPI).
- `tests/fixtures/admin_routes_snapshot.json`, écrit une fois sur le code
  actuel, jamais régénéré pendant le lot.
- `tests/test_admin_routes_guard.py` compare les trois.
- `tests/test_admin_routes_split.py` vérifie les invariants du découpage : aucun
  module de `plugboard.admin_routes` ne lie un point de patch au niveau module ni
  n'importe `plugboard.app` ; chacun expose `register(app, core)` et, s'il
  journalise, le logger de `plugboard.app` ; tout nom que les tests ou d'autres
  modules lisent sur `plugboard.app` y existe encore ; un handler ré-exporté est
  l'endpoint enregistré. Les tâches y ajoutent deux tests de comportement
  (réimportation, horodatage du rafraîchissement).

### Ce qui reste dans `app.py`, et pourquoi

| Reste | Pourquoi |
|---|---|
| Imports et points de patch | Les tests patchent 51 noms sur `plugboard.app` |
| Constantes de configuration et contrôles à l'import (clés, mode strict, CORS, `.env`) | Patchés, et effets d'import à garder dans le même ordre |
| `_lifespan`, création de `app`, 5 gestionnaires d'exception, 5 middlewares | Ordre de démarrage et d'enregistrement |
| Cœur d'authentification (`_require_admin`, `_resolve_client`, sessions, personnes, MFA pas-à-pas) | Dépendances de toutes les routes, très patchées |
| Chemin de chat (admission, budget, facturation, réponses d'erreur, `chat_completions`, `preflight`, listes de modèles) | API client, hors du ticket ; nombreux points de patch |
| Routes client et santé, `/metrics`, `/admin/inflight`, docs et pages | Hors administration, ou trois lignes entre des routes client |
| État et aides partagés de l'administration (R2) | Patchés, lus par nom, ou partagés |

**Taille attendue de `app.py` : ≈ 4 850 lignes** (8 172 − ~3 370 déplacées
+ ~70 de câblage − ~25 d'imports devenus inutiles ; fourchette 4 750–4 950).
La cible du ticket (~2 500) suppose de sortir aussi le chemin de chat
(~2 000 lignes) et le cœur d'authentification, qui ne sont pas des routes
d'administration : à traiter dans un lot suivant si le propriétaire le veut.

## Risques et parades

| Risque | Parade |
|---|---|
| Une annotation résolue dans le mauvais espace de noms | R5 ; la garde compare types des paramètres et document OpenAPI ; `ruff` F821 |
| Un point de patch lu en direct par un module déplacé | Test d'invariant (aucun point de patch lié au niveau module) ; R3 |
| Un handler lié au mauvais objet module après réimportation | `register(app, core)` ; test de réimportation (tâche 2) ; `test_audit_e2e.py` en tête de chaque passe ciblée |
| Ordre des routes modifié | R9 ; la garde compare l'ordre complet |
| Couverture perdue en silence | Aucun `include_router` nouveau : la garde fige le nombre et la forme des entrées de `app.routes` |
| Effets d'import modifiés | La garde compare journaux, fichiers et modules d'un import à froid |

## Validation

Pendant le lot : garde, invariants et tests du domaine touché, `ruff` sur les
fichiers touchés. À la fin, une seule validation complète (quatre blocs Python
parallèles, `ruff`, contrats, couloir e2e), puis un seul `ci lancer`.

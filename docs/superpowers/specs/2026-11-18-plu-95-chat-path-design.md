# Sortir le chemin du chat de `app.py` (PLU-95) — conception

Date : 2026-11-18. Ticket : PLU-95. Décision du propriétaire : D-69, Q1, Q2 et Q3 décidées
comme recommandé (questions en fin de document).
Base : `main` à `c3e4cea`, branche `refactor/plu-95-chat-path`. Modèle suivi : PLU-36
(`docs/superpowers/specs/2026-11-18-plu-36-admin-routes-design.md`, D-67).

## Intention

PLU-36 a sorti les routes d'administration : `plugboard/app.py` est passé de 8 172 à
4 827 lignes. Le plus gros bloc restant est le chemin du chat : `POST /v1/chat/completions`
(une fonction de 615 lignes), ses voisines client (`/v1/models`, `/v1/preflight`, `/v2/*`,
`/v1/me`…) et les aides qu'elles appellent (budget, prix, facturation, réponses d'erreur,
flux SSE). PLU-95 les sort de la même façon, `register(app, core)`, **sans aucun changement
de comportement**, pour approcher la cible de 2 500 lignes.

## Repris de PLU-36 (D-67), sans changement

- Comportement strictement inchangé : mêmes routes, même ordre, mêmes réponses, mêmes
  dépendances, même document OpenAPI (admin et client), mêmes effets d'import.
- La suite existante est le filet ; aucun test existant n'est assoupli, sauté ni supprimé
  (une seule exception demandée, Q1).
- Tests ciblés pendant la construction ; une seule validation complète et une seule CI en
  fin de lot (règle YB du 18/11).
- Le contrat `register(app, core) -> SimpleNamespace` et les règles R1 à R9 de PLU-36.

## Objectifs

- Le chemin du chat dans un paquet `plugboard/chat_path/`, un module par sujet.
- Un garde-fou écrit **avant** tout déplacement, figé sur `c3e4cea` : routes client et leur
  place, contrat client (`build_openapi()`, donc `CONTRACT_VERSION`), gestionnaires
  d'exception, pile des middlewares. Le garde-fou PLU-36 (table complète, OpenAPI admin,
  effets d'import) reste en place et vert.
- `CONTRACT_VERSION` (`3.0.1`), `docs/api/openapi.json`, `docs/api/mcp.json` et
  `docs/api/README.md` inchangés.
- `app.py` ≈ 2 520 lignes (fourchette 2 450–2 600).

## Non-objectifs

- Toute correction, renommage de route, de fonction, de message ou de code d'erreur.
- Découper `chat_completions` (615 lignes) ou sortir son générateur imbriqué
  `_stream_and_bill` : déplacés tels quels, à traiter dans un ticket à part s'il le faut.
- Le cœur d'authentification (`_resolve_client`, `_require_admin`, sessions, ~750 lignes),
  les middlewares, le plafond de requêtes en vol, le `lifespan`, la connexion automatique,
  `/health*`, `/metrics`, `/v1/openapi.json`, les docs et les pages.
- Changer un test existant pour suivre le code.

## Anti-objectifs (un échec même si « ça marche »)

- Une route qui change de place, de dépendance, de paramètre ou de signature.
- Un `monkeypatch` de `plugboard.app.<nom>` qui n'atteint plus le code déplacé.
- Un test qui passe encore mais ne vérifie plus rien (constat 9).
- Un gestionnaire d'exception ou un middleware qui change d'ordre, de classe ou de module.
- Un import circulaire, un module `plugboard` chargé en plus ou dans un autre ordre à
  l'import, une ligne de journal en plus ou en moins.

## Constats (mesurés sur `c3e4cea`)

1. **Le chemin du chat occupe cinq zones d'`app.py`** (≈ 2 340 lignes déplaçables) :

   | Zone | Lignes | Contenu |
   |---|---|---|
   | Raccroché et SSE | 909–1044 | `_ClientGone`, `_unless_client_gone`, `_client_gone_response`, `_done_event_at`, `_complete_events`, `_stream_cut_event`, `_MAX_HELD` |
   | Tour de session | 1070–1118 | `_take_session_turn`, `_record_session_turn` (la dépendance `_session_turn_dep`, 1050, reste) |
   | Budget, prix, facturation, accès, erreurs | 1985–3127 | `_spending_scope` … `_model_not_served_response`, validation des messages, `_live_note` |
   | Routes du chat | 3129–4107 | `chat_completions`, `/v1/models`, `/v1/preflight`, `/v1/models/benchmarks`, `/v1/models/{id}/info`, `/v2/*` |
   | Routes du compte client | 4175–4273 | `/v1/inflight`, `/admin/inflight`, `/usage/me`, `/v1/me`, `/v1/me/keys` |

2. **Table des routes** : 122 entrées (118 `APIRoute`, 4 `_IncludedRouter`). Les routes
   client sont aux places 0–1 (routeurs `decision`, `embeddings`), 2–17 (du chat à
   `/v1/me/keys`) et 102 (`/v1/openapi.json`). FastAPI 0.143.3, Starlette 1.8.0.
3. **La garde PLU-36 nomme chaque dépendance par `module.qualname`**
   (`tests/route_snapshot.py::_call_name`). `_resolve_client`, `_refuse_anonymous_chat`,
   `_inflight_chat_dep`, `_session_turn_dep` et `_require_admin` doivent donc rester dans
   `app.py` : déplacées, elles changeraient de nom dans la table.
4. **Ce que la garde PLU-36 ne voit pas** : les 8 gestionnaires d'exception
   (`HTTPException`, `RequestValidationError`, `WebSocketRequestValidationError`,
   `ValueError`, `_BusyRefusal`, `_UsageUnavailable`, `_ModelNotAllowed`,
   `_InvalidChatRequest`, dans cet ordre), les 6 middlewares (`CORSMiddleware`,
   `_RateLimitMiddleware`, `_RequestIdMiddleware`, `_BodySizeLimitMiddleware`,
   `_SecurityHeadersMiddleware`, `_KeylessLoopbackOnlyMiddleware`), et le contrat client
   (`api_contract.build_openapi()`, version 3.0.1, 19 chemins), qui ne dépend pas d'`app.py`
   mais que `tests/test_api_contract.py` compare aux routes.
5. **Les tests patchent 51 noms sur `plugboard.app`** (319 endroits, 89 fichiers ; inventaire
   AST de `tests/`, identique à `PATCH_POINTS` de `tests/test_admin_routes_split.py`).
   **19 de ces noms sont lus par le code à déplacer : 131 endroits, 55 fichiers** (tableau
   plus bas). 7 partent avec le code (fermetures ré-exportées), 12 restent dans `app.py`.
6. **Surface lue** : 17 noms du chemin du chat sont lus sur `plugboard.app` par les tests
   (`READ_SURFACE`) ou par le code : `decision_api.py` et `embeddings_api.py` font, à l'appel,
   `from plugboard.app import _spending_scope / _check_spending_limit / _quota_error_response`.
   Seul `_retry_after_headers` est appelé par du code qui reste (deux gestionnaires
   d'exception).
7. **Réimportation** : `tests/test_audit_e2e.py` (E-25) réimporte `plugboard.app` ; chaque
   handler doit lire les globales du module qui l'a enregistré (constat 4 de PLU-36).
8. **Effets d'import** : la sonde de la garde PLU-36 (`_PROBE` de `tests/route_snapshot.py`)
   n'exclut que `plugboard.admin_routes*`. Un nouveau paquet chargé à l'import la fait
   échouer (question Q1).
9. **Couverture qui se perdrait en silence** :
   `tests/test_chat_errors_and_billing.py::test_chat_usage_is_priced_and_written_in_one_place`
   ne lit que `plugboard/app.py` et `plugboard/router.py`. Le code de facturation déplacé
   sortirait de son champ ; le nouveau test d'invariants refait la même vérification sur
   `plugboard/chat_path/`.
10. Aucune instruction `global`, aucune lecture d'environnement et aucune référence à `app`
    hors décorateurs dans le code à déplacer. Les journaux JSON portent le nom du logger, pas
    le module (`logging_config.py`) ; aucun test n'inspecte `__module__` d'une fonction du
    chat ; l'image copie `plugboard/` en entier (`Dockerfile`, ligne 81).

## Approches

| Option | Principe | Verdict |
|---|---|---|
| A — `APIRouter` par sujet, `app.include_router` | Le modèle de `decision_api` | **Rejetée** : un `_IncludedRouter` par module change la forme de la table, et deux tests de `test_spec_v7_residuals_parent.py` qui ne parcourent que le premier niveau perdraient la couverture (constat 2 de PLU-36) |
| B — fonctions au niveau module, paramètres explicites (`forward`, `load_providers`… passés en argument) | Injection de dépendances | **Rejetée** : change les signatures que 17 noms lus par les tests exposent, et les 131 patchs de `plugboard.app` n'atteindraient plus rien |
| C — fonctions au niveau module qui lisent `sys.modules["plugboard.app"]` | Pas de fermetures | **Rejetée** : après la réimportation (constat 7), l'ancien module lirait le nouveau |
| **D — retenue** : `register(app, core)` pour **chaque** module, routes et aides | Les routes et toute aide qui lit un point de patch ou un nom resté dans `app.py` sont des fermetures de `register()`, liées au module qui les enregistre ; le code pur reste au niveau du module | Table identique ; chaque import d'`app.py` lie ses propres fermetures, comme aujourd'hui ; les patchs passent par `core.<nom>` |

Compromis de D : environ 30 ré-exports `nom = _ns.nom` dans `app.py`, et la discipline
« point de patch toujours lu par `core.` » à tenir sur ~2 300 lignes. Parades : test
d'invariants (aucun point de patch lié au niveau module), `ruff` F821 (un nom oublié est une
erreur de lint, pas une surprise à l'exécution), relecture `--color-moved`.

## Conception

### Le paquet `plugboard/chat_path/`

| Module | Contenu | Routes | Lignes quittant `app.py` | Taille estimée |
|---|---|---|---|---|
| `streaming.py` | Raccroché du client, événements SSE, fermeture d'un flux amont | — | 167 | ≈ 200 |
| `responses.py` | Réponses au client : facture visible, quota, `providers_busy`, `provider_disabled`, CLI, erreurs HTTP, `model_not_found` | — | 412 | ≈ 445 |
| `limits.py` | Plafond mensuel, estimation et réserve de budget, prix connu, RPM, accès aux modèles | — | 345 | ≈ 390 |
| `billing.py` | Facturation d'un flux et d'une réponse entière, alerte de dépense | — | 196 | ≈ 230 |
| `completions.py` | Validation des messages, tour de session, `POST /v1/chat/completions` | 1 | 752 | ≈ 810 |
| `catalogue.py` | `/v1/models`, `/v1/preflight`, `/v1/models/benchmarks`, `/v1/models/{id}/info` | 4 | 323 | ≈ 360 |
| `v2.py` | `/v2/chat/completions`, `/v2/models`, `/v2/models/benchmarks` | 3 | 41 | ≈ 65 |
| `account.py` | `/v1/inflight`, `/admin/inflight`, `/usage/me`, `/v1/me`, `/v1/me/keys` (Q2) | 5 | 101 | ≈ 125 |
| **Total** | | **13** | **≈ 2 340** | **≈ 2 625** |

Les quatre modules d'aides n'ont pas de route : ils suivent le même contrat
`register(app, core)` (l'argument `app` n'y sert pas) pour que toute fermeture soit liée au
module qui l'enregistre.

### Règles de déplacement

Les règles R1 à R9 de PLU-36 s'appliquent telles quelles, avec ces précisions :

- **R2 (étendue) — Reste dans `app.py`** : toute dépendance FastAPI (constat 3) ; tout
  `@app.exception_handler` **et sa classe d'exception** (`_BusyRefusal`, `_ChatOverloaded`,
  `_SessionBusy`, `_UsageUnavailable`, `_ModelNotAllowed`, `_InvalidChatRequest`) ; le
  plafond de requêtes en vol (`_INFLIGHT_*`, `_inflight_chat_sem`, `_inflight_chat_active`,
  `_get_inflight_sem`, `_ChatSlot`) ; les constantes lues de l'environnement
  (`_BUDGET_RESERVE_TOKENS`, `_USAGE_RETRY_AFTER_S`, `_MAX_BODY_BYTES`,
  `_CACHE_RESPONSE_FLOOR_MS`) ; `_pad_cache_hit_response` ; `_background_tasks`.
- **R3 (rappel)** : un point de patch se lit **toujours** `core.<nom>`, même quand il est
  défini dans le même `register()` (`_record_session_turn` dans `completions.py`,
  `_model_route_guard` dans `limits.py`).
- **R10 — Entre modules du paquet** : un nom défini **au niveau module** s'importe
  directement (`from plugboard.chat_path.streaming import _ClientGone`) ; une **fermeture**
  se lit `core.<nom>` (son ré-export dans `app.py`). Pas d'import relatif.
- **R11 — Ordre des imports** : `app.py` importe le paquet par **un seul** bloc
  `from plugboard.chat_path import (…)`, placé en **dernier** des imports de tête (après
  `plugboard.tokenizer`, avant `setup_logging()`). Un module du paquet n'importe au niveau
  module que des modules qu'`app.py` importe déjà au niveau module ; les imports locaux des
  fonctions restent locaux. Ainsi aucun module `plugboard` n'est chargé en plus ni plus tôt.
  Seule exception relevée : `plugboard.cache_common`, qu'`app.py` n'importe qu'à la ligne 310
  (après `setup_logging()`) ; `completions.py` lit donc `core._strict_tenant_mode`.
  **Écart admis, plus tard** (revue finale, m1) : `plugboard.chat_billing`, qu'`app.py`
  n'importe plus (`chat_path/billing.py` l'importe), passe de la 21ᵉ à la 42ᵉ place des
  modules `plugboard` chargés à l'import. Mesure de la revue finale (import à froid sur
  `c3e4cea` et `cd4d996`) : mêmes 46 modules, mêmes journaux, stderr identique ; seul
  écart, cette place. Ce n'est pas l'anti-objectif « dans un autre ordre », qui vise un
  ordre qui change ce qu'on observe à l'import : `chat_billing` n'a aucun effet à l'import
  (un logger, des constantes), ses dépendances (`billing`, `usage`, `tokenizer`) sont déjà
  chargées et `setup_logging()` vient après dans les deux cas. Plus tard n'est admis que
  pour un module sans effet d'import ; la sonde (`tests/route_snapshot.py`) trie les noms
  et ne verrait pas l'écart si `chat_billing` en gagnait un.
- **R12 — Câblage d'un module d'aides** : son appel `register()` prend la place de son bloc ;
  ce qui reste du bloc (R2) demeure au-dessus, dans son ordre. Un nom encore utilisé par du
  code non déplacé d'`app.py` est ré-exporté **provisoirement** ; la tâche qui déplace son
  dernier utilisateur retire le ré-export (le plan les liste).

### Points de patch lus par le code déplacé (19 noms, 131 endroits)

| Nom | Endroits / fichiers | Lu par | Traitement |
|---|---|---|---|
| `forward` (import) | 37 / 22 | `completions`, `catalogue` | Import gardé ; `core.` |
| `forward_stream` (import) | 22 / 14 | `completions` | Import gardé (`noqa`) ; `core.` |
| `_check_price_known` | 20 / 19 | `completions`, `catalogue` | Part (fermeture de `limits`) ; ré-exporté ; `core.` |
| `resolve_provider_and_model` (import) | 15 / 9 | `limits`, `billing`, `completions`, `catalogue` | Import gardé ; `core.` |
| `load_providers` (import) | 13 / 9 | `completions`, `catalogue` | Import gardé ; `core.` |
| `list_models` (import) | 5 / 4 | `catalogue` | Import gardé ; `core.` |
| `_send_spending_alert` | 3 / 2 | `billing`, `completions` | Part (fermeture de `billing`) ; ré-exporté ; `core.` |
| `_BUDGET_RESERVE_TOKENS` | 2 / 1 | `limits` (`_completion_cap`) | Reste (R2) ; `core.` |
| `_get_request_key_record` | 2 / 2 | `completions`, `catalogue`, `account` | Reste (authentification) ; `core.` |
| `_served_by` (import) | 2 / 1 | `billing` (`_billing_target`) | Import gardé (`noqa`) ; `core.` |
| `cli_refusal` (import) | 2 / 1 | `completions` | Import gardé (`noqa`) ; `core.` |
| `_bill_streaming` | 1 / 1 | `completions` | Part (fermeture de `billing`) ; ré-exporté ; `core.` |
| `_check_and_alert` (import) | 1 / 1 | `billing` | Import gardé (`noqa`) ; `core.` |
| `_hold_budget` | 1 / 1 | `completions` | Part (fermeture de `limits`) ; ré-exporté ; `core.` |
| `_model_is_compatible` | 1 / 1 | aucun appelant | Part (fermeture de `limits`) ; ré-exporté |
| `_model_route_guard` | 1 / 1 | `limits`, `completions`, `catalogue` | Part (fermeture de `limits`) ; ré-exporté ; `core.` partout |
| `_record_session_turn` | 1 / 1 | `completions` | Part (fermeture de `completions`) ; ré-exporté ; `core.` |
| `_usage` (import) | 1 / 1 | `limits`, `billing`, `account` | Import gardé ; `core._usage` |
| `get_provider` (import) | 1 / 1 | `responses` (`_provider_disabled_response`) | Import gardé (`noqa`) ; `core.` |

Les 32 autres points de patch (`_API_KEY`, `_ADMIN_KEY`, `_DEV_MODE`,
`_CACHE_RESPONSE_FLOOR_MS`, `_INFLIGHT_*`, `_inflight_chat_*`, `_MAX_BODY_BYTES`, les noms
d'administration…) ne sont lus que par du code qui reste ou par `admin_routes/`.

### Noms ré-exportés par `app.py` (33, permanents)

- Lus par les tests ou le code : `_MAX_HELD`, `_complete_events`, `_done_event_at`,
  `_retry_after_headers`, `_quota_error_response`, `_providers_busy_response`,
  `_cli_error_response`, `_model_not_served_response`, `_spending_scope`,
  `_check_spending_limit`, `_estimate_call_cost`, `_take_budget_hold`, `_check_model_access`,
  `_bill_completion`, `_billing_target`, `chat_completions`, `models`, `models_v2`.
- Points de patch partis : `_check_price_known`, `_hold_budget`, `_model_route_guard`,
  `_model_is_compatible`, `_bill_streaming`, `_send_spending_alert`, `_record_session_turn`.
- Fermetures lues par un autre module du paquet (R10) : `_aclose_quietly`,
  `_provider_disabled_response`, `_chat_error_response`, `_guard_accepts`, `_charge_answer`,
  `_models_for_key`.
- Lus par `tests/test_inflight_disconnect.py`, qui ne peut pas changer (ruling R-P1 du
  préflight) : `_ClientGone`, `_unless_client_gone`.

### Câblage et ordre

- Les routes déplacées sont enregistrées à la place de leur bloc :
  `completions` (place 2), `catalogue` (3–6), `v2` (7–9), puis `/health*` restent dans
  `app.py` (10–12), puis `account` (13–17), puis `/metrics`. Aucun `include_router` nouveau.
- Les gestionnaires d'exception et les middlewares ne bougent pas : leur ordre est figé par la
  nouvelle garde.
- `_THIS_MODULE` (ligne 682) existe avant tout appel `register()`.

### Garde-fou (tâche 1, avant tout déplacement, figé sur `c3e4cea`)

- `tests/chat_path_snapshot.py` calcule : `client_routes(app)` — `[place, ligne]` de chaque
  entrée de `app.routes` qui sert un chemin `/v1/`, `/v2/`, `/health`, `/usage/` (ou
  `/admin/inflight`), la ligne étant celle de `route_snapshot.route_table` (méthodes,
  dépendances par nom qualifié, paramètres, modèles…) ; `client_contract()` —
  `api_contract.build_openapi()` complet, version comprise ; `exception_handlers(app)` —
  `[classe ou code, gestionnaire]` qualifiés, dans l'ordre ; `middleware(app)` — classe
  qualifiée, arguments et options, dans l'ordre.
- `tests/fixtures/chat_path_snapshot.json`, écrit une fois sur `c3e4cea`, jamais régénéré
  pendant le lot ; `tests/test_chat_path_guard.py` compare les quatre.
- `tests/test_chat_path_split.py` vérifie les invariants : aucun module du paquet ne lie un
  point de patch au niveau module ni n'importe `plugboard.app` (ni import relatif) ; chacun
  expose `register(app, core)` et journalise sous le logger de `plugboard.app` ; seuls les
  modules de la conception existent ; aucun module du paquet n'appelle `append_usage` ni
  `calculate_cost` et aucun ne définit `_replay_completion_as_sse` (constat 9). Trois tests
  de caractérisation, verts sur `c3e4cea` : après réimportation, `_billing_target`,
  `_check_model_access` et `POST /v1/chat/completions` du premier module lisent les points
  de patch du premier module.
- Le garde-fou PLU-36 continue de figer la table complète, l'OpenAPI admin et les effets
  d'import ; seule sa sonde apprend à ignorer `plugboard.chat_path*` (Q1).

### Ce qui reste dans `app.py`, et pourquoi

| Reste | Pourquoi |
|---|---|
| Imports, configuration lue à l'import, `_lifespan`, création de `app` | Effets d'import et ordre de démarrage |
| Gestionnaires d'exception et leurs classes ; middlewares | Ordre d'enregistrement ; classes propres à chaque import |
| Plafond en vol, `_inflight_chat_dep`, `_session_turn_dep`, `_refuse_anonymous_chat` | Dépendances nommées par la garde ; état patché par les tests et par `conftest.py` |
| Cœur d'authentification et sessions | Dépendance de toutes les routes ; très patché (hors ticket) |
| `/health*`, `/metrics`, `/v1/openapi.json`, docs, pages, connexion automatique | Hors chemin du chat |
| Ré-exports et câblage | Surface lue par les tests et par `decision_api` / `embeddings_api` |

**Taille attendue d'`app.py` : ≈ 2 520 lignes** (4 827 − ~2 340 déplacées + ~60 de
câblage − ~30 d'imports devenus inutiles ; fourchette 2 450–2 600). Sans `account.py`
(Q2) : ≈ 2 620. Descendre nettement sous 2 500 suppose de sortir le cœur
d'authentification (~750 lignes) : un autre ticket (Q3).

## Risques et parades

| Risque | Parade |
|---|---|
| Un point de patch lu en direct par une fermeture ou une aide du module | R3 ; test d'invariants ; listes « via `core.` » du plan ; tests ciblés qui patchent ces noms |
| Une aide classée « niveau module » qui lit un nom resté dans `app.py` | `ruff` F821 la refuse ; R5 de PLU-36 |
| Un handler ou une aide lié au mauvais module après réimportation | `register(app, core)` ; trois tests de caractérisation (tâche 1) ; `test_audit_e2e.py` dans chaque passe ciblée |
| Une dépendance déplacée (son nom change dans la table) | R2 étendue ; garde PLU-36 (dépendances par nom qualifié) |
| Ordre des routes, des gestionnaires d'exception ou des middlewares modifié | Garde PLU-36 (table complète) ; nouvelle garde (gestionnaires, middlewares) |
| Un module `plugboard` chargé plus tôt, une ligne de journal déplacée | R11 ; garde PLU-36 (`import_effects`) |
| Couverture perdue en silence (constat 9, routes cachées) | Test d'invariants miroir ; garde des routes client |
| Contrat client modifié | Nouvelle garde (`build_openapi()` complet) ; `tests/test_api_contract.py` ; `export_contract.py --check` |
| Diff de `completions.py` trop gros pour être relu | Relecture `git diff --color-moved=dimmed-zebra --color-moved-ws=allow-indentation-change` : seules les lignes `core.`, imports et câblage ressortent |
| Temps d'import accru (huit fichiers de plus) | Mesuré tâche 1 et en clôture : médiane ≤ référence + 10 % |

## Validation

Pendant le lot : les deux gardes, les invariants, les tests du sujet touché et `ruff` sur
`plugboard/` et `tests/`. À la fin, une seule validation complète (quatre blocs Python
parallèles, `ruff`, contrats, couloir e2e), puis un seul `ci lancer`.

## Ce qui reste au constructeur

- Classer chaque nom libre du bloc (module, fermeture, `core.`) : le plan donne les listes ;
  `ruff` F821 et le test d'invariants tranchent les oublis.
- Rendre verts sur `c3e4cea` les trois tests de caractérisation (tâche 1) en ajustant leur
  mise en place si besoin, jamais leur assertion.
- Mesurer le temps d'import de référence et final, et la taille finale d'`app.py`.

## Questions au propriétaire

- **Q1 — Sonde de la garde PLU-36.** Recommandé : modifier **une ligne** de
  `tests/route_snapshot.py` (`_PROBE` ignore aussi `plugboard.chat_path*`), gabarit
  `admin_routes_snapshot.json` inchangé. Sinon : régénérer ce gabarit à chaque tâche qui
  ajoute un module (huit fois), ce qui l'expose à masquer un vrai changement.
- **Q2 — Routes du compte client** (`/v1/inflight`, `/admin/inflight`, `/usage/me`,
  `/v1/me`, `/v1/me/keys`). Recommandé : les sortir (`account.py`, tâche 9, ≈ 2 520
  lignes). Sinon : tâche 9 sautée, `app.py` ≈ 2 620.
- **Q3 — Cible de 2 500 lignes.** Recommandé : accepter ≈ 2 520 pour PLU-95 et ouvrir un
  ticket pour le cœur d'authentification. Sinon : élargir PLU-95, au prix d'un risque bien
  plus grand (dépendances de toutes les routes).

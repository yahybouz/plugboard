# Sortir le cœur d'authentification de `app.py` (PLU-97) — conception

Date : 2026-11-18. Ticket : PLU-97. Décision du propriétaire : à prendre (questions Q1 à Q4
en fin de document).
Base : `main` à `919f01f`, branche `refactor/plu-97-auth-core`. Modèles suivis : PLU-36
(`docs/superpowers/specs/2026-11-18-plu-36-admin-routes-design.md`, D-67) et PLU-95
(`docs/superpowers/specs/2026-11-18-plu-95-chat-path-design.md`, D-69), y compris ce que
leur construction a appris (R-P1 à R-P4, revue finale m1).

## Intention

Après PLU-36 et PLU-95, `plugboard/app.py` compte 2 540 lignes. Le plus gros bloc restant
est le cœur d'authentification : qui appelle (principal d'audit, compteurs de limite et de
verrouillage), clé d'administration et sessions signées, refus des requêtes inter-sites,
authentification admin par clé, TOTP ou clé personnelle (PB-AUTH-01), preuve du code
d'une personne, ré-authentification avant un changement de MFA, connexion automatique
locale. PLU-97 sort ce code de la même façon, `register(app, core)`, **sans aucun
changement de comportement**.

C'est du code de sécurité : un écart de comportement, même invisible pour la suite de
tests, est un **défaut critique**. La conception privilégie donc ce qui garde chaque appel
exactement là où il atterrit aujourd'hui, au prix de quelques lignes de plus.

## Repris de PLU-36 et PLU-95, sans changement

- Comportement strictement inchangé : mêmes routes, même ordre, mêmes réponses, mêmes
  dépendances (même objet, même nom qualifié), même document OpenAPI (admin et client),
  mêmes gestionnaires d'exception, mêmes middlewares, mêmes effets d'import.
- La suite existante est le filet ; aucun test existant n'est assoupli, sauté ni supprimé
  (une seule exception demandée, Q1).
- Tests ciblés pendant la construction ; une seule validation complète et une seule CI en
  fin de lot (règle YB du 18/11).
- Le contrat `register(app, core) -> SimpleNamespace`, les règles R1 à R9 de PLU-36 et
  R10 à R12 de PLU-95, l'invariant « aucun point de patch lu sans `core.` » (R-P3).

## Objectifs

- Le cœur d'authentification dans un paquet `plugboard/auth_core/`, un module par sujet.
- Les dépendances FastAPI (`_resolve_client`, `_require_admin`) restent **le même objet,
  au même nom qualifié** (`plugboard.app._resolve_client`…) : garde PLU-36, garde PLU-95,
  `app.dependency_overrides[...]` et les tests qui comparent `d.call` ne bougent pas.
- Deux garde-fous écrits **avant** tout déplacement, figés sur `919f01f` :
  une garde de structure (identité des dépendances, surface de `plugboard.app`, ordre de
  chargement des modules à l'import) et une **table de décisions d'authentification**
  (qui est accepté, refusé, verrouillé, audité, pour une matrice de requêtes synthétiques).
- `CONTRACT_VERSION` (`3.0.1`), `docs/api/openapi.json`, `docs/api/mcp.json` inchangés.
- `app.py` ≈ 1 920 lignes (fourchette 1 880–1 980).

## Non-objectifs

- Toute correction, simplification ou renommage, même d'un défaut repéré en route (le
  noter au board, `board bug ajouter`, et le traiter dans un autre ticket).
- Déplacer les dépendances FastAPI `_resolve_client` et `_require_admin` (Q2), ni
  `_refuse_anonymous_chat`, `_inflight_chat_dep`, `_session_turn_dep` (admission du chat).
- La résolution du client : `_get_request_key_record`, `_apply_key_record`,
  `_resolve_client` (≈ 124 lignes, deux points de patch et la dépendance : rien à gagner
  à les séparer).
- Les middlewares (`_SecurityHeadersMiddleware`, `_BodySizeLimitMiddleware`,
  `_RequestIdMiddleware`, `_RateLimitMiddleware`, `_KeylessLoopbackOnlyMiddleware`, la
  CSP) : figés par classe qualifiée par la garde PLU-95.
- Les contrôles de démarrage (secrets valant un commentaire, force d'`ADMIN_KEY`,
  `AUTH_MODE`, combinaison mode strict sans `API_KEY`, CORS, mandataires de confiance,
  secrets stockés illisibles ; ≈ 200 lignes) : ils s'exécutent avant que `app` et
  `_THIS_MODULE` existent (Q4).
- `_check_dashboard_auth` (6 lignes, partie des pages du tableau de bord).
- Changer un test existant pour suivre le code.

## Anti-objectifs (un échec même si « ça marche »)

- Une dépendance qui change d'objet, de module ou de nom qualifié ; un
  `dependency_overrides` qui ne s'applique plus.
- Un `monkeypatch` de `plugboard.app.<nom>` qui n'atteint plus un appelant qu'il atteint
  aujourd'hui.
- Une requête acceptée qui était refusée, ou l'inverse ; un autre code HTTP, un autre
  `detail`, une autre entrée d'audit, un compteur de verrouillage qui compte autrement.
- Un état (dictionnaire de refus audités, valeurs déjà signalées) dupliqué entre deux
  imports d'`app.py`, ou partagé là où il ne l'était pas.
- Un module `plugboard` chargé en plus, plus tôt ou **plus tard** à l'import ; une ligne de
  journal en plus, en moins ou déplacée.
- Un test qui passe encore mais ne vérifie plus rien.

## Constats (mesurés sur `919f01f`)

1. **Le cœur d'authentification occupe quatre zones d'`app.py`** (≈ 675 lignes
   déplaçables), entourant ce qui doit rester :

   | Zone | Lignes | Contenu | Sort |
   |---|---|---|---|
   | Principal et compteurs | 1040–1155 | `_ADMIN_SCOPE`, `_ADMIN_KEY_PRINCIPAL`, `_person_principal`, `_person_scope`, `_set_principal`, `_client_address`, `_address_scope`, `_key_scope`, `_person_bearer_scope`, `_rate_limit_scope` | Part |
   | Middlewares, mode sans clé | 1157–1239 | `_RateLimitMiddleware`, `_KeylessLoopbackOnlyMiddleware`, `add_middleware` | Reste |
   | Clé admin, sessions, inter-sites | 1245–1442 | `_effective_admin_key`, `_is_admin_key`, `_create_admin_session_token`, `_admin_session_nonce`, `_admin_session_principal`, `_is_admin_session`, `_STATE_CHANGING_METHODS`, `_CROSS_SITE_REFUSED`, `_origin_is_this_host`, `_is_cross_origin_browser_request`, `_refuse_cross_site_session`, `_should_use_secure_cookie` | Part |
   | Résolution du client | 1445–1567 | `_get_request_key_record`, `_apply_key_record`, `_resolve_client` | Reste |
   | Authentification admin | 1570–1751 | `_ADMIN_TOTP_HEADER`, `_admin_request_auth`, `_person_request_auth`, `_person_record_is_sound`, `_prove_person_code`, `_require_mfa_step_up` | Part |
   | Dépendance admin | 1753–1784 | `_require_admin` | Reste |
   | Connexion automatique | 2267–2458 | `_LOOPBACK_HOSTS` … `_issue_local_login_token` | Part, sauf l'état, deux constantes patchées et la ligne de journal d'import |

2. **Les dépendances sont figées par nom qualifié et par identité.** Le gabarit PLU-36
   (`tests/fixtures/admin_routes_snapshot.json`, `_call_name` = `module.qualname`) nomme
   `plugboard.app._require_admin` 102 fois, `plugboard.app._resolve_client` 17 fois ; les
   lignes `client_routes` du gabarit PLU-95 répètent ces noms. L'identité est lue par :
   `tests/test_spec_v7_residuals_parent.py::_admin_routes` (`appmod._require_admin in
   calls`), trois `dependency_overrides` (`tests/test_review_fixes.py` ×2 avec
   `app_mod._resolve_client`, `tests/test_tnr_sec001_missing_client_id.py` avec
   `from plugboard.app import _resolve_client`), `_refuse_anonymous_chat` dont le défaut
   `Depends(_resolve_client)` est évalué à sa définition (l. 1891), `build_suivi_router(
   _require_admin)` et `build_people_router(_require_admin, …)` (l. 2516–2519), qui
   reçoivent l'objet à l'import, et `Depends(core._require_admin)` (90 sites dans
   `admin_routes/` et `chat_path/account.py`) / `Depends(core._resolve_client)` (15 sites
   dans `chat_path/`), évalués une fois à l'enregistrement.
3. **Points de patch** : l'inventaire AST de `tests/` trouve les 51 noms de
   `PATCH_POINTS` (`tests/test_admin_routes_split.py`), 253 sites, 85 fichiers (méthode :
   `setattr`/`delattr`/`patch.object` sur un alias du module, chaînes `"plugboard.app.X"`,
   affectations d'attribut ; PLU-95 comptait 319 sites avec une autre méthode, même
   ensemble de noms). **Neuf sont lus par le code à déplacer : 66 sites, 33 fichiers**
   (tableau plus bas ; chiffres corrigés en tâche 7, la première version disait 34 sites,
   22 fichiers). Quatre partent avec le code (`_client_address`, `_is_admin_key`,
   `_is_admin_session`, `_admin_request_auth`) ; cinq restent dans `app.py`.
4. **Liaison tardive aujourd'hui** : dans `app.py`, chaque appel d'une fonction
   d'authentification à une autre est une lecture des globales de `plugboard.app` au moment
   de l'appel. Un patch de `plugboard.app._is_admin_session` atteint donc
   `_resolve_client`, `_refuse_cross_site_session`, `_key_scope`… Exemples réels :
   `_client_address` patché (`test_admin_people_mfa_routes.py`) doit atteindre
   `_prove_person_code` ; `_admin_request_auth` patché (`test_audit_principal.py`) doit
   atteindre `_require_admin` ; `_ADMIN_KEY` patché (27 fichiers) doit atteindre
   `_effective_admin_key` par toute la chaîne.
5. **Surface lue** : les tests lisent sur `plugboard.app` sept noms qui partent
   (`_auto_login_mode`, `_bind_host`, `_create_admin_session_token`, `_host_header_name`,
   `_is_loopback_address`, `_issue_local_login_token`, `_require_mfa_step_up`) et des noms
   qui restent (`_ADMIN_COOKIE_NAME`, `_ADMIN_SESSION_MAX_AGE`, `_refusal_audit_seen`,
   `_warned_auto_login_values`, `_check_dashboard_auth`, `_resolve_client`,
   `_require_admin`, `KEY_PREFIXES`) : tous figurent dans `READ_SURFACE`, que
   `test_names_read_on_the_app_module_are_still_there` vérifie.
   `admin_routes/auth.py` lit 26 noms d'authentification par `core.` (65 lectures).
   **Aucun module de production** hors `app.py`, `admin_routes/` et `chat_path/` n'importe
   un nom d'authentification de `plugboard.app` (grep : `decision_api.py` et
   `embeddings_api.py` n'en importent que des aides de budget ; `mcp_server.py` a ses
   propres `_API_KEY`/`_ADMIN_KEY` ; `admin_people_api.py` a sa propre constante
   `_ADMIN_KEY_PRINCIPAL`, de même valeur, non touchée).
6. **État** : dans `app.py`, `_refusal_audit_seen` (dict), `_warned_auto_login_values`
   (set), `_keyless_refusal_logged` (booléen `global`, middleware) ; les constantes
   `_REFUSAL_AUDIT_WINDOW_S` et `_REFUSAL_AUDIT_MAX_KEYS` sont des points de patch. Tout le
   reste vit hors d'`app.py` : compteurs de verrouillage et de limite dans
   `plugboard.ratelimit` (`_auth_buckets`, `_lockouts`, `_buckets`), sessions dans
   `plugboard.admin_sessions`, second facteur dans `plugboard.admin_mfa`, clés
   personnelles dans `plugboard.admin_people`, principal de la requête (contextvar) dans
   `plugboard.audit`. Ces modules ne sont pas réimportés quand `app.py` l'est
   (`tests/test_audit_e2e.py`, E-25) : un seul exemplaire, comme aujourd'hui.
7. **Aucune instruction `global`** dans le code à déplacer ; `_auto_login_mode` modifie
   `_warned_auto_login_values` en place. Lectures d'environnement à l'appel seulement
   (`_bind_host`, `_auto_login_mode`). Imports locaux : `admin_mfa` (×5),
   `plugboard.ratelimit` (×3), `local_login` (×2), `urllib.parse` (×1).
8. **Effets d'import** : deux lignes de journal, `auth.mode=…` (l. 295) puis
   `auth.auto_login mode=… local_install=…` (l. 2412), qui appelle `_auto_login_mode()` et
   `_is_loopback_address(_bind_host())`. Le gabarit PLU-36 les fige, dans cet ordre.
9. **Imports qui deviendraient inutiles** : `hashlib`, `hmac`, `Tuple` (sans effet), et
   `admin_people`, `admin_sessions`, deux modules `plugboard` qu'`app.py` charge en tête
   (l. 81–82). Les retirer les ferait charger plus tard (par `admin_people_api` ou
   `admin_routes.auth`) : l'écart m1 de PLU-95, ici évitable (R11-bis).
10. **La sonde de la garde PLU-36** (`_PROBE` de `tests/route_snapshot.py`) n'ignore que
    `plugboard.admin_routes*` et `plugboard.chat_path*` : un nouveau paquet chargé à
    l'import la fait échouer (Q1). Aucun nom de paquet honnête n'y échappe sans toucher
    un test (un sous-paquet d'`admin_routes/` ou de `chat_path/` casse leur
    `EXPECTED_MODULES` ; un nom comme `admin_routes_auth` exploiterait le préfixe :
    rejeté).
11. Aucun test ne lit le texte d'`app.py` pour l'authentification ; aucun n'inspecte
    `__module__` d'une fonction d'authentification hors des gardes ; l'image copie
    `plugboard/` en entier.

## Approches

| Option | Principe | Verdict |
|---|---|---|
| **A — retenue : la porte reste, ce qui est derrière part** | `_resolve_client` et `_require_admin` restent dans `app.py`, verbatim. Tout le reste du cœur part dans `plugboard/auth_core/`, `register(app, core)`, fermetures liées au module qui enregistre, chaque appel vers une fonction d'`app.py` lu `core.<nom>` (R13) | Gardes PLU-36 et PLU-95 inchangées ; identité des dépendances triviale ; ≈ 675 lignes sortent |
| B — coquilles de délégation | Les deux dépendances restent dans `app.py` mais leur corps devient `return _ns._resolve_client_impl(request)` | Même nom, même objet, ≈ 110 lignes de plus sortent ; mais la porte ne se lit plus d'un tenant, une pile d'appel de plus dans chaque requête, deux fonctions pour une : gain faible pour du code de sécurité (Q2) |
| C — les dépendances partent | `_resolve_client = _ns._resolve_client` ré-exporté **avant** tout `Depends` qui le lit (l'ordre actuel le permet : enregistrement à la place du bloc, avant l. 1891, `chat_path`, `admin_routes`, `suivi`, `people`) ; même objet, overrides intacts | **Rejetée** : le nom qualifié devient `plugboard.auth_core.….register.<locals>._resolve_client` ; il faudrait régénérer les deux gabarits « jamais régénérés » (≈ 140 lignes de dépendances) pour suivre le code, ou maquiller `__module__`/`__qualname__`, ce qui aveugle la garde et fausse les traces |
| D — liaison tardive sur les seuls points de patch (R3 de PLU-95 tel quel) | Seuls les 51 noms de `PATCH_POINTS` passent par `core.` ; les autres appels internes sont directs | **Rejetée pour ce ticket** : un patch futur de `plugboard.app._is_admin_session` (ou un correctif à chaud) n'atteindrait plus les appelants déplacés ; écart invisible aujourd'hui, réel demain. R13 coûte ≈ 60 préfixes `core.` |

Réponse à la question posée : **oui, les dépendances pourraient sortir sans casser
l'identité** (ré-export du même objet, enregistré à la place du bloc, donc avant le premier
`Depends` qui le lit), **mais pas sans changer la garde** (nom qualifié). A les garde ; le
reste du cœur sort sans toucher aucune des deux gardes.

## Conception

### Le paquet `plugboard/auth_core/`

| Module | Contenu | Lignes quittant `app.py` | Taille estimée |
|---|---|---|---|
| `scopes.py` | Principal d'audit, adresse du client, compteurs de limite et de verrouillage (`key:…`, `key:admin:<id>`, `ip:…`) | 116 | ≈ 150 |
| `sessions.py` | Clé admin, sessions signées et révocables, refus inter-sites (PB-SEC-06), cookie sécurisé | 198 | ≈ 245 |
| `admin.py` | Authentification admin d'une requête (clé + TOTP, clé personnelle, session), preuve du code d'une personne, ré-authentification avant un changement de MFA | 182 | ≈ 215 |
| `auto_login.py` | Connexion automatique locale : refus et leur audit, hôte de liaison, preuve locale, jeton de connexion | 180 | ≈ 215 |
| `__init__.py` | Docstring du contrat, aucun import | — | ≈ 30 |
| **Total** | | **≈ 675** | **≈ 855** |

Aucun module ne déclare de route : ils suivent quand même `register(app, core)` (l'argument
`app` n'y sert pas) pour que toute fermeture soit liée au module qui l'enregistre.

### Ce qui part, ce qui reste

| Nom | Module | Forme | Pourquoi |
|---|---|---|---|
| `_ADMIN_SCOPE`, `_ADMIN_KEY_PRINCIPAL` | `scopes` | niveau module | Constantes ; lues par `admin`, `sessions` (R10) et `admin_routes/auth.py` (`core.`) |
| `_person_principal`, `_person_scope`, `_set_principal` | `scopes` | niveau module | Pures (`_set_principal` appelle `plugboard.audit`) |
| `_client_address` | `scopes` | `register()` | Point de patch (R5) |
| `_address_scope`, `_key_scope`, `_person_bearer_scope`, `_rate_limit_scope` | `scopes` | `register()` | Appellent d'autres fonctions du cœur (R13) |
| `_STATE_CHANGING_METHODS`, `_CROSS_SITE_REFUSED`, `_origin_is_this_host` | `sessions` | niveau module | Constantes ; fonction pure |
| `_effective_admin_key` … `_should_use_secure_cookie` (9) | `sessions` | `register()` | Lisent `_ADMIN_KEY`, `_ADMIN_SESSION_MAX_AGE`, `_COOKIE_SECURE_MODE`, `_ADMIN_COOKIE_NAME` (restés) ou d'autres fonctions |
| `_ADMIN_TOTP_HEADER`, `_person_record_is_sound` | `admin` | niveau module | Constante ; pure |
| `_admin_request_auth`, `_person_request_auth`, `_prove_person_code`, `_require_mfa_step_up` | `admin` | `register()` | Point de patch ou appels du cœur |
| `_LOOPBACK_HOSTS`, `_FLAG_ON`, `_FLAG_OFF`, `_PROXY_HEADERS`, `_LOCAL_NAVIGATION_SITES`, `_SUSPICIOUS_REFUSALS` | `auto_login` | niveau module | Constantes (et leurs commentaires) |
| `_host_header_name`, `_is_loopback_address`, `_bind_host`, `_local_login_proof` | `auto_login` | niveau module | Pures (environnement lu à l'appel ; import local de `local_login`) |
| `_should_audit_refusal`, `_auto_login_mode`, `_auto_login_refusal`, `_issue_local_login_token` | `auto_login` | `register()` | Lisent l'état ou les constantes restés dans `app.py` |
| `_API_KEY`, `_ADMIN_KEY`, `_DEV_MODE`, `_ADMIN_COOKIE_NAME`, `_ADMIN_SESSION_MAX_AGE`, `_COOKIE_SECURE_MODE`, `AUTH_MODE` | `app.py` | — | Configuration lue à l'import, points de patch (R2) |
| `_REFUSAL_AUDIT_WINDOW_S`, `_REFUSAL_AUDIT_MAX_KEYS`, `_refusal_audit_seen`, `_warned_auto_login_values` | `app.py` | — | Points de patch et état mutable (R2) |
| `log.info("auth.auto_login …")` | `app.py` | — | Effet d'import, émis par `app.py` après le câblage d'`auto_login` |
| `_get_request_key_record`, `_apply_key_record`, `_resolve_client`, `_require_admin`, `_check_dashboard_auth`, `_refuse_anonymous_chat` | `app.py` | — | Dépendances et leur voisinage (Q2) |

### Règles de déplacement

R1 à R9 (PLU-36) et R10 à R12 (PLU-95) s'appliquent, avec ces précisions :

- **R2 (étendue)** — Reste dans `app.py` : les dépendances FastAPI et la résolution du
  client ; toute valeur lue de l'environnement à l'import ; tout point de patch qui est une
  **valeur** (pas une fonction) ; tout état mutable ; toute ligne de journal émise à
  l'import ; les middlewares et le mode sans clé.
- **R13 — Liaison tardive intégrale** (propre à PLU-97) : dans `auth_core/`, **tout appel
  d'une fonction et toute lecture d'une valeur définie dans `app.py` ou ré-exportée par
  lui passent par `core.<nom>`**, y compris une fonction du même module ou du même
  `register()` (`_key_scope` appelle `core._person_scope`, `_is_admin_session` appelle
  `core._admin_session_principal`). Seules exceptions : les constantes de niveau module du
  paquet (lues directement dans leur module, importées ailleurs par R10), les imports
  d'autres modules (R6) et `log` (R7). Conséquence : une fonction qui en appelle une
  autre du cœur est une fermeture (R5). Raison : aujourd'hui chacun de ces appels est une
  lecture des globales de `plugboard.app` ; la garder telle quelle préserve tout patch,
  présent ou futur, et lie chaque import d'`app.py` à lui-même (constat 4).
  Vérifié par un test d'invariants (liste `LATE_BOUND`, plan, tâche 1).
- **Exception R13 assumée** (préflight n° 14, écrite en tâche 7) : sont lus directement,
  sans `core.`, et un patch de leur nom sur `plugboard.app` n'atteint donc plus le code
  déplacé :
  - les constantes du paquet (`_ADMIN_SCOPE`, `_ADMIN_KEY_PRINCIPAL`,
    `_ADMIN_TOTP_HEADER`, `_STATE_CHANGING_METHODS`, `_CROSS_SITE_REFUSED`,
    `_LOOPBACK_HOSTS`, `_FLAG_ON`, `_FLAG_OFF`, `_PROXY_HEADERS`,
    `_LOCAL_NAVIGATION_SITES`, `_SUSPICIOUS_REFUSALS`) et `KEY_PREFIXES` (importé de
    `plugboard.apikeys`). Avant le déplacement, un patch de
    `plugboard.app._ADMIN_SCOPE` atteignait `_key_scope` et `_admin_request_auth` ;
    `admin_routes/auth.py`, lui, les lit toujours par `core.` ;
  - les modules `plugboard.audit` (`_audit_mod`, dans `scopes`), `plugboard.admin_people`
    (dans `scopes`, `sessions` et `admin`) et `plugboard.admin_sessions` (dans
    `sessions`), importés par chaque module du paquet (P4) : un patch de
    `plugboard.app._audit_mod`, `plugboard.app.admin_people` ou
    `plugboard.app.admin_sessions` n'atteindrait plus ce code (relevé par les relectures
    des tâches 3 à 5). Un patch d'un attribut **de ces modules**
    (`admin_people.check_code`…) l'atteint, comme avant.

  Aucun test ne patche aujourd'hui ces noms sur `plugboard.app` (vérifié en tâche 7). Les
  ensembles mutables (`_LOOPBACK_HOSTS`, `_FLAG_ON`, `_FLAG_OFF`, `_PROXY_HEADERS`…)
  deviennent communs à deux imports d'`app.py` : ils ne doivent jamais être modifiés à
  l'exécution (l'invariant refuse toute méthode qui les change en place). De même, les
  fonctions de niveau module de `scopes` (`_person_principal`, `_person_scope`,
  `_set_principal`) — et les autres fonctions pures de niveau module du paquet — sont
  partagées entre deux imports de l'app : elles ne gardent aucun état et ne lisent rien
  de `plugboard.app`, ce qui rend ce partage sans effet.
- **R11-bis — Imports de tête** : aucun import de tête d'`app.py` qui charge un module
  `plugboard` n'est retiré, même devenu inutile (`admin_people`, `admin_sessions`) ; il
  reste avec `# noqa: F401  (import order kept, PLU-97)`. Les imports de la bibliothèque
  standard devenus inutiles (`hashlib`, `hmac`, `typing.Tuple`) sont retirés. L'ordre de
  chargement des modules `plugboard` à l'import est figé par la nouvelle garde (non trié,
  contrairement à la sonde PLU-36).
- **R12 (rappel)** — Câblage d'un module : son `register()` prend la place de son bloc ;
  ce qui reste du bloc (R2) demeure au même endroit, dans son ordre.
- **R14 — Ré-exports permanents** : **chaque** nom déplacé (42) est ré-exporté par
  `app.py`, au câblage de son module : R13 l'exige (le paquet le lit par `core.`), et
  `plugboard.app` garde ainsi exactement les mêmes noms qu'avant. Aucun ré-export
  provisoire.

### Points de patch lus par le code déplacé (9 noms, 66 sites, 33 fichiers)

| Nom | Sites / fichiers | Lu par | Traitement |
|---|---|---|---|
| `_ADMIN_KEY` | 49 / 27 | `sessions` (`_effective_admin_key`) | Reste ; `core.` |
| `_audit` (import) | 4 / 4 | `admin` (`_admin_request_auth`, `_prove_person_code`, `_require_mfa_step_up`) | Import gardé ; `core.` |
| `get_key_record` (import) | 3 / 3 | `scopes` (`_key_scope`) | Import gardé (encore lu par `_resolve_client`) ; `core.` |
| `_REFUSAL_AUDIT_WINDOW_S` | 2 / 1 | `auto_login` (`_should_audit_refusal`) | Reste ; `core.` |
| `_REFUSAL_AUDIT_MAX_KEYS` | 2 / 1 | `auto_login` (`_should_audit_refusal`) | Reste ; `core.` |
| `_is_admin_key` | 2 / 2 | `scopes`, `sessions`, `admin` ; `_resolve_client` (reste) | Part (fermeture de `sessions`) ; ré-exporté ; `core.` |
| `_is_admin_session` | 2 / 2 | `sessions` ; `_resolve_client` (reste) | Part (fermeture de `sessions`) ; ré-exporté ; `core.` |
| `_client_address` | 1 / 1 | `scopes`, `admin` ; `admin_routes/auth.py` | Part (fermeture de `scopes`) ; ré-exporté ; `core.` |
| `_admin_request_auth` | 1 / 1 | `_require_admin`, `_check_dashboard_auth` (restent) | Part (fermeture d'`admin`) ; ré-exporté |

Fichiers : `test_admin_csrf_same_site`, `test_admin_people_auth`, `test_admin_people_login`,
`test_admin_people_mfa_routes`, `test_admin_people_scope`, `test_admin_routes_split`,
`test_audit_principal`, `test_auth_auto_login`, `test_bug120_cli_model_id`,
`test_chat_path_split`, `test_decision_parent_regressions`, `test_keyless_public_bind`,
`test_login_mobile_sol`, `test_model_not_found`, `test_ollama_off_by_config`,
`test_ollama_switch`, `test_ollama_switch_e2e`, `test_openclaw_spec_parent`,
`test_plu72_existing_claude_provider`, `test_ratelimit_identity`, `test_resolve_client`,
`test_spec_v7_residuals_parent` (les 22 de la première version), plus `test_api`,
`test_e2e_full_coverage`, `test_e2e_functional`, `test_live_requests` et
`test_stream_interrupted_billing` (préflight, n° 13) : les 27 de `PATCHERS`. Les six
autres sont `test_admin_people_api`, `test_audit_e2e`, `test_readiness_visibility`,
`test_security_hardening`, `test_security_tnr` (dans `AUTH_TESTS`) et `conftest.py`.
Tous font partie de chaque passe ciblée. Méthode du décompte (tâche 7, sur les tests de
`919f01f`) : `setattr`/`delattr`/`patch.object` sur un alias de `plugboard.app` (import,
`import_module`, `sys.modules`, ou module réimporté par une fixture), chaînes
`"plugboard.app.X"` et affectations d'attribut.

### Câblage et ordre

- Bloc d'import unique, **dernier** des imports de tête, après celui de `chat_path`, avant
  `setup_logging()` (R11) : `from plugboard.auth_core import (admin as _ac_admin,
  auto_login as _ac_auto_login, scopes as _ac_scopes, sessions as _ac_sessions)`. Les
  modules du paquet n'importent au niveau module que `plugboard.admin_people`,
  `plugboard.admin_sessions`, `plugboard.audit` et `plugboard.apikeys`, déjà chargés par
  `app.py` (l. 81–96) ; `admin_mfa`, `ratelimit`, `local_login` restent des imports locaux.
- Place de chaque `register()` : `scopes` à la l. 1040 (après `_RequestIdMiddleware`, avant
  `_RateLimitMiddleware`, qui lit `_rate_limit_scope` à l'appel) ; `sessions` sous le titre
  « -- Auth » (l. 1242) ; `admin` entre `_resolve_client` et `_require_admin` ;
  `auto_login` à la place de son bloc, après `root()` : l'état resté **au-dessus** du
  câblage (il existe avant toute fermeture ; ordre du plan, préflight n° 15), puis le
  câblage, puis la ligne de journal d'import. Rien n'est appelé entre l'état et le
  câblage : aucun effet observable. `_THIS_MODULE` (l. 659) existe avant le premier.
- Aucune route ajoutée, retirée ni déplacée ; aucun gestionnaire d'exception ; aucun
  middleware touché. `_require_admin` et `_resolve_client` restent définis avant tout
  `Depends` qui les lit (inchangé).

### Garde-fous (tâches 1 et 2, avant tout déplacement, figés sur `919f01f`)

- **Structure** (`tests/auth_core_snapshot.py`, `tests/fixtures/auth_core_snapshot.json`,
  `tests/test_auth_core_guard.py`) : `dependency_calls(app)` — pour chaque dépendance de
  chaque route (routeurs inclus, contexte d'inclusion compris), son nom qualifié, et un
  test qui vérifie que tout `d.call` de `plugboard.app` **est** `getattr(plugboard.app,
  qualname)` ; `app_surface()` — les noms non-module de `vars(plugboard.app)` (on vérifie
  « aucun ne disparaît ») ; `import_order()` — la liste **ordonnée** des modules
  `plugboard` chargés par un import à froid, hors `admin_routes*`, `chat_path*`,
  `auth_core*`.
- **Décisions** (`tests/auth_decisions.py`, `tests/fixtures/auth_decisions.json`, comparé
  par le même fichier de garde) : une matrice d'≈ 50 requêtes synthétiques (sans clé, clé
  admin avec ou sans TOTP, mauvais code jusqu'au verrouillage, clé client valide, révoquée
  ou inconnue, clé personnelle avec ou sans code, révoquée, session valide, falsifiée,
  expirée, d'avant la MFA, ouverte par une personne puis révoquée, POST inter-sites,
  connexion automatique dans chaque cas de refus) × les fonctions `_resolve_client`,
  `_require_admin`, `_admin_request_auth`, `_check_dashboard_auth`, `_key_scope`,
  `_person_bearer_scope`, `_rate_limit_scope`, `_refuse_cross_site_session`,
  `_auto_login_refusal`, `_require_mfa_step_up` : résultat (identifiant, code HTTP,
  `detail`, principal), actions d'audit écrites. Valeurs normalisées : aucun secret, aucun
  identifiant aléatoire dans le gabarit.
- **Invariants** (`tests/test_auth_core_split.py`) : ceux de `test_chat_path_split.py`
  appliqués au paquet (aucun point de patch lié au niveau module, aucun import de
  `plugboard.app` ni import relatif, `register(app, core)`, logger de `plugboard.app`,
  modules de la conception), plus R13 (aucune lecture nue d'un nom de `LATE_BOUND`) et
  R14 (chaque nom déplacé est sur `plugboard.app` et y est l'objet que le module fournit).
  Quatre tests de caractérisation, verts sur `919f01f` : après réimportation,
  `_admin_request_auth`, `_rate_limit_scope`, `_should_audit_refusal` et `GET /metrics` du
  premier module lisent la clé admin et l'état du premier module.
- Les gardes PLU-36 et PLU-95 restent en place et vertes ; seule la sonde PLU-36 apprend
  à ignorer `plugboard.auth_core*` (Q1).

### Ce qui reste dans `app.py`, et pourquoi

| Reste | Pourquoi |
|---|---|
| Configuration d'authentification lue à l'import, contrôles de démarrage | Effets d'import, exceptions à l'import, avant `app` (Q4) |
| `_resolve_client`, `_require_admin` et la résolution du client | Dépendances figées par nom et identité (constat 2, Q2) |
| Middlewares, mode sans clé, CSP | Figés par classe qualifiée (garde PLU-95) |
| État et constantes patchées de la connexion automatique, sa ligne de journal | R2 ; effet d'import |
| `_check_dashboard_auth`, `_refuse_anonymous_chat` | Pages du tableau de bord ; admission du chat |
| 42 ré-exports et le câblage | R13, R14 |

**Taille attendue d'`app.py` : ≈ 1 920 lignes** (2 540 − ≈ 675 déplacées + ≈ 56 de
câblage et de ré-exports + ≈ 3 d'imports ; fourchette 1 880–1 980).

## Risques et parades

| Risque | Parade |
|---|---|
| Une décision d'authentification change (accès accordé ou refusé autrement) | Table de décisions figée (tâche 2) ; 68 fichiers de tests d'authentification à chaque passe (4 gardes, 22 + 42 du plan ; 73 avec les 5 ajoutés à `PATCHERS`) ; relecture Opus de chaque tâche |
| Un appel interne devenu direct : un patch n'atteint plus un appelant | R13 ; test `LATE_BOUND` ; les 33 fichiers qui patchent ces noms à chaque passe |
| Une fermeture liée au mauvais module après réimportation | `register(app, core)` ; quatre tests de caractérisation ; `test_audit_e2e.py` dans chaque passe |
| État dupliqué ou partagé (refus audités, valeurs signalées) | R2 : l'état reste dans `app.py`, lu par `core.` ; test `_should_audit_refusal` après réimportation |
| Une dépendance change de nom ou d'objet | Elles ne bougent pas ; garde PLU-36 ; test d'identité de la nouvelle garde |
| Un nom disparaît de `plugboard.app` | R14 ; `app_surface()` ; `test_names_read_on_the_app_module_are_still_there` |
| Ordre de chargement des modules ou des journaux à l'import | R11, R11-bis ; `import_order()` ; `import_effects` de la garde PLU-36 |
| Comparaison en temps constant perdue (`hmac.compare_digest`) | R8 verbatim ; relecture `--color-moved` : seules les lignes `core.` ressortent |
| Annotation de fonction résolue dans le mauvais espace de noms | R5 : `Request`, `Optional`, `Dict`, `Any`, `Tuple` au niveau module |
| Temps d'import accru | Mesuré tâche 1 et en clôture : médiane ≤ référence + 10 % |

## Validation

Pendant le lot : les trois gardes, les invariants, les fichiers qui patchent les noms lus
par `core.` et les tests d'authentification (liste du plan), `ruff` sur `plugboard/` et
`tests/`. Chaque tâche est relue par Opus (conformité à la conception, puis qualité et
sécurité). À la fin, une seule validation complète (quatre blocs Python parallèles,
`ruff`, contrats, couloir e2e), une relecture finale Opus de la branche, puis un seul
`ci lancer`.

## Ce qui reste au constructeur

- Écrire les cas de la table de décisions à partir de la matrice du plan, avec les aides
  existantes (`tests/people_helpers.py`, `isolated_app` de `tests/test_resolve_client.py`,
  enrôlement MFA de `tests/test_spec_v7_residuals_parent.py`) ; la rendre déterministe
  (horloge figée, valeurs normalisées) et la générer sur `919f01f`.
- Rendre verts sur `919f01f` les tests de caractérisation en ajustant leur mise en place
  si besoin, jamais leur assertion.
- Classer chaque nom libre de chaque bloc (niveau module, fermeture, `core.`) : le plan
  donne les listes ; `ruff` F821 et les tests d'invariants tranchent les oublis.
- Mesurer le temps d'import de référence et final, et la taille finale d'`app.py`.

## Questions au propriétaire

- **Q1 — Sonde de la garde PLU-36.** Recommandé : modifier **une ligne** de
  `tests/route_snapshot.py` (`_PROBE` ignore aussi `plugboard.auth_core`), gabarit
  inchangé, comme Q1 de PLU-95. Sinon : régénérer `admin_routes_snapshot.json` à chacune
  des quatre tâches qui ajoutent un module, ce qui l'expose à masquer un vrai changement.
- **Q2 — Dépendances `_resolve_client` et `_require_admin`.** Recommandé : elles restent
  dans `app.py`, verbatim, avec la résolution du client (approche A, ≈ 1 920 lignes).
  Sinon : coquilles de délégation (B, ≈ 1 810 lignes, gardes inchangées, une pile d'appel
  de plus) ; ou les déplacer et régénérer les deux gabarits (C, déconseillé).
- **Q3 — Table de décisions d'authentification (tâche 2).** Recommandé : oui, écrite et
  figée avant tout déplacement. Sinon : s'en remettre à la suite existante (≈ 68 fichiers
  d'authentification), qui ne compare pas des sorties figées.
- **Q4 — Contrôles de démarrage** (≈ 200 lignes). Recommandé : hors PLU-97, ticket à part
  (fonctions pures dans un module, appels laissés dans `app.py` à leur place). Sinon :
  élargir PLU-97, au prix de toucher l'ordre des exceptions et des journaux à l'import.

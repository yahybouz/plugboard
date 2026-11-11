# Épinglage DNS des fournisseurs protégés — conception

**Statut : VALIDÉE par YB le 2026-11-11** (ticket PLU-7 ; D-16 : concevoir avant tout code ;
décisions D-17 à D-20 ci-dessous, périmètre **élargi** par D-18). Base : `f1a0db6` (4.6.0).
Origine : revue PB-DASH-06. Plan : `docs/superpowers/plans/2026-11-11-catalog-dns-pinning.md`.

## Intention

La `base_url` d'un fournisseur créé depuis le catalogue models.dev vient d'un flux tiers.
Elle n'est contrôlée qu'à la création et au test. Si son DNS change ensuite (rebinding),
Plugboard envoie la clé et les prompts vers une adresse interne (127.0.0.1, 10.x,
169.254.169.254…). Le contrôle doit porter sur **chaque connexion**, pour tous les
fournisseurs **protégés** : catalogue, préréglages cloud intégrés, et tout fournisseur
manuel dont l'admin coche la case (D-18).

## Décisions de YB (2026-11-11)

| N° | Sujet | Décision |
|---|---|---|
| D-17 | Refus | La requête est refusée et passe au candidat suivant ; 502 s'il n'y en a pas. Audit et alerte au plus une fois par 5 min et par fournisseur. Le fournisseur reste actif et repart seul quand le DNS redevient public. Le disjoncteur ne compte pas ce refus. |
| D-18 | Périmètre (élargi) | Protégés : (a) fournisseurs du catalogue (`preset_key` `catalog-*`) ; (b) préréglages cloud intégrés (`plugboard/direct_providers.py`) ; (c) tout fournisseur créé à la main dont la case « adresses publiques seulement » est cochée, case visible et modifiable dans le tableau de bord, **décochée par défaut**. Les fournisseurs locaux (Ollama sur le réseau privé, `sovereign`) ne sont jamais protégés par défaut. |
| D-19 | Édition | Un `base_url` modifié à la main (`PUT /providers/{id}`) sur un fournisseur protégé doit être public, sinon 422 à l'édition. |
| D-20 | Proxy | Les fournisseurs protégés ignorent `HTTPS_PROXY` (`trust_env=False`) : ils sortent en direct et Plugboard vérifie lui-même l'adresse résolue. Sur un serveur sans accès direct à internet, ils ne fonctionnent pas : à documenter. |

Choix pris dans le cadre de ces décisions (réversibles) :

- Pour (a) et (b), la garde est **imposée** : case cochée et grisée, `egress_guard: false`
  refusé en 422. C'est la lecture de D-18 (a)(b) et du non-but « pas d'interrupteur ».
- La règle de D-19 vaut aussi quand la case est cochée sur un fournisseur existant et à la
  création manuelle case cochée : l'adresse visée doit être publique (422 sinon). Cocher la
  case sur un Ollama local est donc refusé.
- Pas de contrôle DNS à la **création** d'un préréglage intégré : ses URL sont écrites dans
  le code, pas lues d'un flux tiers ; la garde de connexion suffit. Le catalogue garde le
  sien (`_require_public_host`).

## Existant (vérifié au code)

- `_require_public_host` (`app.py:3858`) refuse un nom dont **une seule** réponse n'est
  pas publique (`llm_pricing.ip_is_public`). Appelé à la création et au test d'un préréglage
  du catalogue seulement (`app.py:4016-4022`, `4107-4109`). Le test (`_test_openai_endpoint`,
  `app.py:3909-3921`) se connecte via `_PinnedResolver`, mais **suit les redirections** et le
  résolveur n'est pas appelé pour une IP littérale : `Location: http://10.0.0.5/` passe.
- `preset_key` est écrit par `create_from_preset` depuis 4.0.0 (`direct_providers.py:839`,
  `858`), y compris pour les fournisseurs OAuth créés par `_ensure_broker_provider`
  (`app.py:3804-3827`). La `base_url` reste **modifiable** par `PUT /providers/{id}`
  (`app.py:3409`, aucun contrôle public). `POST /providers` (`app.py:3368`) n'en a aucun
  non plus. Modèles : `ProviderCreate` / `ProviderUpdate` (`app.py:3323-3352`).
- Préréglages intégrés (`direct_providers.PRESETS`) : à `base_url` HTTP(S) publique —
  `anthropic`, `openai`, `gemini`, `github-models`, `chatgpt-oauth`, `gemini-oauth`,
  `github-copilot-oauth`, `github-models-oauth`, `github-broker`, `copilot-broker` ; à
  pseudo-URL `subprocess://` — `agy-`, `gemini-`, `codex-`, `claude-cli-subprocess`, que le
  routeur court-circuite avant tout HTTP (`router.py:666`) ; `copilot` et `claude-pro-oauth`
  sont `roadmap`, non créables (`direct_providers.py:820-824`).
- Ollama est inscrit `provider_type="sovereign"` sans `preset_key` (`ollama.py:350-357`) ;
  les outils Ollama (tirage, suppression, `/api/generate`) visent `OLLAMA_URL`
  (`ollama.py:26`), réglage de l'opérateur, pas la `base_url` d'un fournisseur.
- Appelants qui se connectent à la `base_url` d'un fournisseur : pool partagé
  `http_pool.get_session()` (`trust_env=True`, `http_pool.py:57`, cache DNS 300 s) pour
  `router.forward` (`658-661`, session prise **avant** la boucle de repli), `forward_stream`
  (`1065-1067`, un seul candidat, pas de repli), `discover_models` (`2017-2019`),
  `model_availability.discover_openai_models` (`964-966`) ; session propre (`trust_env`
  par défaut `False`) pour `health_probe._probe_http` (`86`), `provider_watchdog`
  `_sovereign_health` (`232`), `_shallow_health` (`323`, chaque cycle, sans clé),
  `_deep_health` (`360`), `semantic_cache._embed` (`698`). Les préréglages intégrés passent
  donc **aujourd'hui** par `HTTPS_PROXY` ; le test de connexion, non.
- **Pas** concernés : `monitoring.check_model_availability` (vise Plugboard lui-même),
  `embeddings_api` (moteur local), `mcp_server`, `ollama.py`, `ollama_admin.py`.
- Tableau de bord : formulaire `static/index.html:254-284` ; remise à zéro
  `static/app.js:485-497` ; envoi `1035-1062` — le résultat de `POST /providers` n'est pas
  lu, « Provider created » s'affiche même sur un 4xx ; édition `1110-1125` ; badge
  catalogue `1092`.
- aiohttp 3.14.5 / aiohappyeyeballs 2.7.1 (épinglés), vérifiés dans context7 et la
  source : `TCPConnector(resolver=, socket_factory=)` (3.12+) ; `socket_factory` reçoit
  chaque `AddrInfoType` avant `sock_connect` ; un résolveur passé au connecteur n'est pas
  fermé par lui (`_resolver_owner=False`) ; une `OSError` du résolveur devient
  `ClientConnectorDNSError` (sous-classe de `ClientConnectorError`) et **n'est pas mise en
  cache** ; plusieurs échecs aux messages différents deviennent une `OSError` « Multiple
  exceptions » (type perdu) ; `ClientConnectorError.os_error` / `.host` ; `trust_env`
  vaut `False` par défaut. `DefaultResolver` est ici `ThreadedResolver` (pas d'aiodns).

## Buts, non-buts, contraintes

- **Buts** : aucune connexion d'un fournisseur protégé vers une adresse non publique
  (cache DNS, IP littérale, redirection) ; rien ne change pour Ollama local ni pour un
  fournisseur manuel dont la case reste décochée (proxy compris) ; fournisseurs existants
  couverts sans réécrire `providers.json`.
- **Non-buts** : pare-feu de sortie dans le code ; hôte public compromis (TLS reste la
  défense) ; flux models.dev ; interrupteur global pour couper la garde.
- **Contraintes** : pool mutualisé (PERF-2) ; aucune dépendance ; aiohttp 3.14.5.

## Approches

1. **Contrôle à chaque connexion**, connecteur « public seulement » choisi par
   fournisseur. 1a résolveur filtrant seul : aveugle aux IP littérales. 1b
   `socket_factory` seul : couvre tout, mais accepte un nom mi-public mi-privé (règle
   différente de la création), perd le type d'erreur dans « Multiple exceptions » et
   garde l'adresse refusée 300 s en cache. **1c = 1a + 1b sur le même connecteur.**
2. **Épinglage des IP à la création** : panne dès qu'un CDN change d'adresse, migration,
   un pool par fournisseur. Écartée.
3. **Liste de noms autorisés / pare-feu** : une liste n'arrête pas le rebinding ; le
   pare-feu est hors dépôt. Documenté en défense en profondeur.

## Conception retenue : 1c

Le résolveur refuse le nom dès qu'une réponse n'est pas publique (même règle qu'à la
création), rien n'est mis en cache, la reprise est immédiate et l'erreur nomme l'hôte. Le
`socket_factory` reste le filet des IP littérales et des redirections ; il n'y voit qu'une
adresse, donc le type reste. Rejeu local (aiohttp 3.14.5) : nom → 127.0.0.1 + ::1, nom
mixte, littéral 127.0.0.1 donnent tous `os_error = EgressRefused`, 0 requête servie,
résolveur rappelé après refus.

### Qui est protégé (D-18)

- `netguard.guard_is_forced(p)` : `preset_key` commence par `models_dev.PRESET_PREFIX`
  (`catalog-`), **ou** `preset_key` est une clé de `direct_providers.PRESETS` dont la
  `base_url` commence par `http://` ou `https://` (les dix préréglages listés plus haut ;
  les `subprocess://` en sont exclus d'office).
- `netguard.is_protected(p)` : `guard_is_forced(p)`, ou `p.get("egress_guard") is True`
  et `base_url` HTTP(S). Champ absent = `False`.
- Nouveau champ de fournisseur **`egress_guard: bool`**, écrit seulement pour un
  fournisseur non imposé (`config.create_provider(..., egress_guard=False)` ne l'écrit que
  s'il vaut `True` ; `PUT` l'écrit tel quel).

### API

- `GET /providers` (et les réponses de `POST`/`PUT`, via `_mask`) : `egress_guard` =
  protection **effective** (`is_protected`), `egress_guard_forced` = `guard_is_forced`.
- `POST /providers` : `ProviderCreate.egress_guard: bool = False`. À `True` : `base_url`
  HTTP(S) obligatoire (422) puis `_require_public_host(base_url, label="Provider base_url")`
  (422 non public, 503 nom introuvable), audit `provider.egress.rejected` sur refus.
- `PUT /providers/{id}` : `ProviderUpdate.egress_guard: Optional[bool] = None`.
  1. Fournisseur imposé + `egress_guard: false` → 422 ; `true` → ignoré (non écrit).
  2. Protection après édition = imposée, ou `egress_guard` de la requête, sinon celui
     enregistré.
  3. Si protégé après édition **et** (la `base_url` change, comparée sans `/` final, **ou**
     la case passe de décochée à cochée) : même contrôle que `POST` sur la nouvelle
     `base_url` (ou l'actuelle). Une `base_url` renvoyée inchangée ne déclenche aucune
     résolution (le formulaire la renvoie toujours).
- `_require_public_host(base_url, *, label="Catalog preset")` : le libellé entre dans le
  message (« `<label>` refused: … » / « `<label>` host unreachable: … ») ; appelants
  existants inchangés.

### Tableau de bord (`static/index.html`, `static/app.js`)

- Case `pf-egress-guard` « Public addresses only » sous « Refresh the model list
  automatically », avec l'aide `pf-egress-guard-hint` : refuse les adresses privées,
  loopback et link-local à chaque connexion ; ignore `HTTPS_PROXY` (accès direct à internet
  requis) ; à laisser décochée pour Ollama ou un serveur local.
- Ajout : case décochée et active. Édition : `checked = p.egress_guard === true`,
  `disabled = p.egress_guard_forced === true`, aide « Always on for catalog and built-in
  cloud presets. » si imposée. Envoi : `body.egress_guard` en création comme en édition.
- La création lit enfin la réponse de `POST /providers` : toast d'erreur avec `detail` sur
  un 4xx/5xx, formulaire laissé ouvert.
- Tableau : badge « public only » sur une ligne protégée.

### Migration des fournisseurs existants

- Aucune réécriture de `providers.json` : la protection se calcule à la lecture
  (`preset_key`, `egress_guard`). Dès la mise à jour, catalogue et préréglages intégrés sont
  protégés ; fournisseurs manuels et Ollama restent tels quels (champ absent = décoché).
- Cas limite : un préréglage dont la `base_url` a été modifiée à la main vers une adresse
  interne avant la mise à jour est refusé dès la première requête ; l'alerte de D-17 le
  nomme. Remède documenté : le recréer en fournisseur manuel (case décochée).
- Derrière un proxy obligatoire, les préréglages intégrés cessent de fonctionner (D-20) ;
  remède documenté : fournisseur manuel vers la même URL, case décochée (il garde le proxy).

### Modules

- **`plugboard/netguard.py`** : `ip_is_public` (déplacée avec `_CGNAT`, `_METADATA`,
  `_NAT64*`, réexportée par `llm_pricing`), `EgressRefused(OSError)` levée en
  `EgressRefused(errno.EACCES, "non-public address refused")`,
  `PublicOnlyResolver(inner=None)` (`DefaultResolver()` par défaut ; `close()` ferme
  `inner`), `refuse_non_public(addr_info) -> socket.socket`, `public_connector(*,
  resolver=None, inner=None, **kw)`, `client_session(public_only, **kw)` (public :
  connecteur public + `trust_env=False` ; sinon `aiohttp.ClientSession(**kw)` inchangé),
  `guard_is_forced(p)`, `is_protected(p)`, `is_egress_refusal(exc)`,
  `report_refusal(provider_id, host, source)` (voir Refus).
- **`http_pool.py`** : `get_session()` **garde sa signature** (une dizaine de tests la
  remplacent par un faux sans argument) ; `get_public_session()` (mêmes limites et TTL,
  `trust_env=False`, D-20) ; `session_for(provider)` appelle l'une ou l'autre ;
  `close_session()` ferme les deux et le résolveur (non détenu par le connecteur).
- **Appelants** :
  - `router.forward` : `session = await session_for(provider)` **dans** la boucle des
    candidats, après le court-circuit `subprocess:` ; `forward_stream`, `discover_models`,
    `discover_openai_models` : `session_for`.
  - Sessions propres : elles **restent propres** (pas de singleton, pas de proxy ajouté) et
    passent `netguard.client_session(public_only)`. `_probe_http(url, headers, timeout_s,
    *, public_only=False)` — `probe_provider` ne passe `public_only=True` que pour un
    fournisseur protégé (des faux de test n'acceptent que `(url, headers)`) ;
    `_sovereign_health`, `_shallow_health`, `_deep_health` reçoivent `public_only`, calculé
    une fois par `ping_provider` ; `_embed` le calcule sur son fournisseur.
  - `_test_openai_endpoint(..., *, public_only=False)` : à `True`, connecteur public (avec
    `_PinnedResolver` comme résolveur s'il y a des adresses épinglées) et
    `allow_redirects=False`. `test_direct_provider` le met à `True` pour un préréglage du
    catalogue ou intégré ; l'URL libre reste inchangée.
- **Refus** (D-17 ; `ClientConnectorError` dont `os_error` est `EgressRefused`, testé
  **avant** le traitement générique d'`aiohttp.ClientError`) :
  - `forward` : pas de nouvel essai ; `provider_fault=False` (disjoncteur intact) ; pas de
    `record_provider_failure` (pas de webhook `provider_down`) ; erreur comptée dans les
    métriques ; **repli vers le candidat suivant**, sans risque puisque rien n'est parti.
  - `forward_stream` : pas de `cb_failure` ; il n'a qu'un candidat (comme pour toute autre
    erreur réseau), donc 502 directement.
  - Erreur levée : `router.ProviderEgressRefused(ProviderNetworkError)`, attribut `host`,
    message anglais `[<id>] egress refused: <host> resolves to a non-public address`. Le
    mappage existant (`app.py:1593-1631`) donne 502 ; l'admin voit ce texte, un client le
    message générique avec la référence du journal.
  - Sentinelle et sondes : résultat `ok=False`, `error="egress_refused"`, `host` ; la
    sentinelle ne lance ni redémarrage ni `record_provider_failure` pour ce motif ;
    `ping_provider` et `probe_provider` appellent `report_refusal`.
  - `netguard.report_refusal(provider_id, host, source) -> bool` : en mémoire, **jamais
    attendu sur le chemin de la requête**. Premier refus par fournisseur et par fenêtre de
    300 s : `log.error`, puis tâche de fond `asyncio.to_thread(audit.log_action,
    "provider.egress_refused", actor="system", target_type="provider", ...)` et
    `alerts.send_egress_refused_alert` (webhook `type: "provider_egress_refused"`, même URL
    que les alertes). Refus suivants de la fenêtre : `log.debug` seulement. Rien n'est
    désactivé : la connexion suivante refait le contrôle.
- Docs : `providers-and-subscriptions.md` (case, fournisseurs protégés, remèdes),
  `deployment.md` (réseau sortant : accès direct requis, filtrage de sortie conseillé),
  `CHANGELOG.md` (section Security d'Unreleased).

### Ce que l'élargissement change par rapport à la version catalogue seul

- Sélection : `is_catalog_provider` devient `guard_is_forced` / `is_protected`.
- Nouveau champ `egress_guard`, nouveaux champs de réponse `egress_guard` et
  `egress_guard_forced`, règles `POST`/`PUT` étendues aux préréglages intégrés et aux
  manuels cochés.
- Fichiers en plus : `config.py`, `static/index.html`, `static/app.js`, `alerts.py`,
  `semantic_cache.py` (déjà prévu), tests du tableau de bord.
- Comportement : les préréglages intégrés n'utilisent plus `HTTPS_PROXY` (D-20) ; le test
  de connexion d'un préréglage intégré ne suit plus les redirections.

## Tests prévus

1. `refuse_non_public` / `ip_is_public` : refus de 127.0.0.1, 10.0.0.1, 172.17.0.1,
   169.254.169.254, 100.64.0.1, 198.18.0.1, ::1, ::ffff:10.0.0.1, 64:ff9b::a00:1,
   2002:a00:1::1, fe80::1%eth0 ; accord pour 1.1.1.1, ::ffff:1.1.1.1, 2606:4700::1111.
2. Rebinding (résolveur interne factice injecté dans `PublicOnlyResolver`) : nom →
   127.0.0.1 + ::1, puis nom mixte : `EgressRefused`, 0 appel au serveur ; résolveur
   rappelé après un refus ; même requête sans la garde : 200.
3. IP littérale par la session publique : refus, 0 appel. Test de connexion protégé :
   redirection non suivie, 0 appel à la cible.
4. `guard_is_forced` / `is_protected` / `session_for` : imposé pour `catalog-x`, `openai`,
   `anthropic`, `github-broker`, `chatgpt-oauth` ; non protégé pour `""`,
   `agy-cli-subprocess`, un manuel, Ollama `sovereign` ; protégé non imposé pour un manuel
   `egress_guard: true` ; `forward` à deux candidats (protégé puis manuel) prend la bonne
   session pour chacun ; les faux `get_session()` sans argument interceptent toujours.
5. Refus : repli vers le candidat suivant, disjoncteur et `record_provider_failure` non
   appelés, 502 (texte admin / texte client), une seule entrée d'audit par fenêtre et
   boucle non bloquée ; sentinelle sans redémarrage ; sondes `egress_refused`.
6. API : `PUT` d'une `base_url` interne sur `catalog-x` et sur `openai` : 422 ; `POST` et
   `PUT` d'un manuel coché vers une adresse interne : 422 ; cocher la case sur Ollama
   local : 422 ; `egress_guard: false` sur un imposé : 422 ; `base_url` renvoyée inchangée
   : aucune résolution ; case sur une URL `subprocess://` : 422 ; `GET /providers` expose
   `egress_guard` et `egress_guard_forced`.
7. Tableau de bord : présence de `pf-egress-guard`, envoi de `egress_guard`, case grisée si
   imposée, erreur de `POST /providers` affichée ; `node --check static/app.js`.
8. Non-régression ciblée : `test_models_dev.py`, `test_llm_pricing.py`, `test_http_pool.py`,
   `test_provider_network_error.py`, `test_watchdog.py`, `test_ollama.py`,
   `test_auto_refresh.py`, `test_exposure_and_estimates.py`, `test_semantic_cache.py`,
   `test_openclaw_removal_supervision.py`, `test_decision_v2_fixes.py`.

## Questions pour YB

Aucune : D-17 à D-20 tranchent les quatre questions de la version révisée.

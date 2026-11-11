# Épinglage DNS des fournisseurs du catalogue — conception

**Statut : PROPOSÉE, révisée après revue, à valider par YB** (ticket PLU-7 ; D-16 :
concevoir avant tout code). Base : `f1a0db6` (4.6.0). Origine : revue PB-DASH-06.

## Intention

La `base_url` d'un fournisseur créé depuis le catalogue models.dev vient d'un flux tiers.
Elle n'est contrôlée qu'à la création et au test. Si son DNS change ensuite (rebinding),
Plugboard envoie la clé et les prompts vers une adresse interne (127.0.0.1, 10.x,
169.254.169.254…). Le contrôle doit porter sur **chaque connexion**.

## Existant (vérifié au code)

- `_require_public_host` (`app.py:3858`) refuse un nom dont **une seule** réponse n'est
  pas publique (`llm_pricing.ip_is_public`). Appelé à la création et au test. Le test
  (`_test_openai_endpoint`, `app.py:3909-3921`) se connecte via `_PinnedResolver`, mais
  **suit les redirections** et le résolveur n'est pas appelé pour une IP littérale :
  `Location: http://10.0.0.5/` passe.
- Le fournisseur garde `preset_key = "catalog-<id>"`, mais sa `base_url` reste
  **modifiable** par `PUT /providers/{id}` (`app.py:3409`, aucun contrôle public).
- Appelants qui se connectent à la `base_url` d'un fournisseur : pool partagé
  `http_pool.get_session()` (`trust_env=True`, cache DNS 300 s) pour `router.forward`
  (`658-661`, session prise **avant** la boucle de repli), `forward_stream` (`1065-1067`),
  `discover_models` (`2017-2019`), `model_availability` (`964-966`) ; session propre
  (`trust_env=False`) pour `health_probe._probe_http` (`86`), `provider_watchdog`
  `_sovereign_health` (`232`), `_shallow_health` (`323`, chaque cycle, sans clé),
  `_deep_health` (`360`), `semantic_cache._embed` (`698`).
- **Pas** concernés : `monitoring.check_model_availability` (vise Plugboard lui-même,
  `localhost:8765`, aucun appelant hors tests), `embeddings_api` (moteur local seulement),
  `mcp_server`, `ollama.py`.
- aiohttp 3.14.5 / aiohappyeyeballs 2.7.1 (épinglés), vérifiés dans context7 et la
  source : `socket_factory` reçoit chaque `AddrInfoType` avant `sock_connect` ; une
  `OSError` du résolveur devient `ClientConnectorDNSError` et **n'est pas mise en cache** ;
  plusieurs échecs aux messages différents deviennent une `OSError` « Multiple
  exceptions » (type perdu) ; `ClientConnectorError.host` donne le nom.

## Buts, non-buts, contraintes

- **Buts** : aucune connexion d'un fournisseur du catalogue vers une adresse non publique
  (cache DNS, IP littérale, redirection) ; rien ne change pour Ollama local, les
  fournisseurs manuels ni leur usage du proxy ; fournisseurs existants couverts sans
  migration.
- **Non-buts** : pare-feu de sortie dans le code ; hôte public compromis (TLS reste la
  défense) ; flux models.dev ; interrupteur pour couper la garde.
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
   pare-feu est hors dépôt. À documenter en défense en profondeur.

## Recommandation : 1c (révisée ; c'était 1b)

1b seul a les trois défauts relevés en revue. Avec 1c, le résolveur refuse le nom dès
qu'une réponse n'est pas publique (même règle qu'à la création), rien n'est mis en cache,
la reprise est immédiate et l'erreur nomme l'hôte. Le `socket_factory` reste le filet
des IP littérales et des redirections ; il n'y voit qu'une adresse, donc le type reste.
Rejeu local (aiohttp 3.14.5) : nom → 127.0.0.1 + ::1, nom mixte, littéral 127.0.0.1
donnent tous `os_error = EgressRefused`, 0 requête servie, résolveur rappelé après refus.

- **`plugboard/netguard.py`** : `ip_is_public` (déplacée, réexportée par `llm_pricing`),
  `EgressRefused(OSError)` levée en `EgressRefused(errno.EACCES, "non-public address
  refused")`, `PublicOnlyResolver(inner=DefaultResolver())`, `refuse_non_public(addr_info)`,
  `public_connector(**kw)`, `client_session(public_only, **kw)` (connecteur public si
  demandé, `trust_env` inchangé à False), `is_catalog_provider(p)` (préfixe
  `models_dev.PRESET_PREFIX`), `report_refusal(...)` (voir Refus).
- **`http_pool.py`** : `get_session()` **garde sa signature** (une dizaine de tests la
  remplacent par un faux sans argument) ; `get_public_session()` (mêmes limites et TTL,
  `trust_env=False` selon Q1) ; `session_for(provider)` appelle l'une ou l'autre ;
  `close_session()` ferme les deux et le résolveur (non détenu par le connecteur).
- **Appelants** :
  - `router.forward` : `session = await session_for(provider)` **dans** la boucle des
    candidats ; `forward_stream`, `discover_models`, `model_availability` : `session_for`.
  - Sessions propres : elles **restent propres** (pas d'`async with` sur un singleton,
    pas de proxy ajouté) et passent `netguard.client_session(public_only)`. Signatures :
    `_probe_http(url, headers, *, public_only=False)` ; `_sovereign_health`,
    `_shallow_health`, `_deep_health` reçoivent `public_only`, calculé une fois par
    `ping_provider` / `probe_provider` ; `_embed` le calcule sur son fournisseur.
  - `_test_openai_endpoint` : le connecteur épinglé reçoit aussi `refuse_non_public`, et
    `allow_redirects=False` (la clé ne suit plus une redirection).
  - `patch_provider` : fournisseur `catalog-` + `base_url` dans la requête →
    `_require_public_host` (422 / 503), selon Q4.
- **Refus** (`ClientConnectorError` dont `os_error` est `EgressRefused`, testé **avant**
  `aiohttp.ClientError`) :
  - `forward` : pas de nouvel essai ; `provider_fault=False` (disjoncteur intact) ; pas de
    `record_provider_failure` (pas de webhook `provider_down`) ; erreur comptée dans les
    métriques ; **repli vers le candidat suivant**, sans risque puisque rien n'est parti.
    `forward_stream` : idem, sans `cb_failure`.
  - Erreur levée : `ProviderEgressRefused(ProviderNetworkError)`, message anglais
    `[<id>] egress refused: <host> resolves to a non-public address`. Le mappage existant
    (`app.py:1593-1631`) donne 502 ; l'admin voit ce texte, un client le message
    générique avec la référence du journal.
  - Sentinelle et sondes : résultat `ok=False`, `error="egress_refused"` ; la sentinelle
    ne lance pas de redémarrage pour ce motif.
  - `netguard.report_refusal(provider_id, host, source)` : en mémoire, **jamais attendu
    sur le chemin de la requête**. Premier refus par fournisseur et par fenêtre de 300 s :
    `log.error`, puis tâche de fond `asyncio.to_thread(audit.log_action,
    "provider.egress_refused", actor="system", target_type="provider", ...)` et webhook
    `provider_egress_refused` (même URL que les alertes). Refus suivants de la fenêtre :
    `log.debug` seulement.
- **Ollama local** et **fournisseurs manuels** : pas de `catalog-`, sessions inchangées.
- Docs : `providers-and-subscriptions.md`, `deployment.md` (filtrage de sortie conseillé),
  `CHANGELOG.md`.

## Tests prévus

1. `refuse_non_public` / `ip_is_public` : refus de 127.0.0.1, 10.0.0.1, 172.17.0.1,
   169.254.169.254, 100.64.0.1, 198.18.0.1, ::1, ::ffff:10.0.0.1, 64:ff9b::a00:1,
   2002:a00:1::1, fe80::1%eth0 ; accord pour 1.1.1.1, ::ffff:1.1.1.1, 2606:4700::1111.
2. Rebinding (résolveur interne factice injecté dans `PublicOnlyResolver`) : nom →
   127.0.0.1 + ::1, puis nom mixte : `EgressRefused`, 0 appel au serveur ; résolveur
   rappelé après un refus ; même requête par la session partagée : 200.
3. IP littérale par la session publique : refus, 0 appel. Test de connexion : redirection
   vers `http://127.0.0.1:<port>/` non suivie, 0 appel.
4. `session_for` : publique pour `catalog-x`, partagée pour `""`, `openai`, Ollama ;
   `forward` à deux candidats (catalogue puis manuel) prend la bonne session pour chacun ;
   les faux `get_session()` sans argument interceptent toujours.
5. Refus : repli vers le candidat suivant, disjoncteur et `record_provider_failure` non
   appelés, 502 (texte admin / texte client), une seule entrée d'audit par fenêtre et
   boucle non bloquée ; `PUT` d'une `base_url` interne sur `catalog-x` : 422.
6. Non-régression : `test_models_dev.py`, `test_llm_pricing.py`, tests Ollama, `make test`.

## Questions pour YB

1. **Proxy (`HTTPS_PROXY`)** : 1) le catalogue ignore le proxy (`trust_env=False`),
   comme le test de connexion aujourd'hui — recommandé ; 2) il passe par le proxy :
   c'est le proxy qui résout le nom, donc la garde ne protège plus rien.
2. **Refus** : 1) requête refusée avec repli, 502 si aucun repli, audit et alerte une
   fois par 5 min, fournisseur toujours actif, reprise seule dès que le DNS redevient
   public — recommandé ; 2) en plus, désactiver le fournisseur au premier refus.
3. **Périmètre** (titre de PLU-7 ; D-16 ne le fixe pas) : 1) fournisseurs du catalogue
   seulement — recommandé ; 2) aussi les préréglages intégrés en nuage (OpenAI,
   Anthropic…) ; 3) en plus, une case à cocher par fournisseur manuel.
4. **`base_url` modifiée à la main sur un fournisseur du catalogue** : 1) contrôle
   public à l'édition, refus 422 — recommandé ; 2) l'édition retire `catalog-` (le
   fournisseur devient manuel et perd la tarification models.dev, `billing.py:290`) ;
   3) seulement documenter (502 à chaque usage).

# Épinglage DNS des fournisseurs du catalogue — conception

**Statut : PROPOSÉE, à valider par YB** (ticket PLU-7, décision D-16 : concevoir et
faire valider avant tout code). Base : `f1a0db6` (4.6.0). Origine : retour de revue
PB-DASH-06.

## Intention

L'URL de base d'un fournisseur créé depuis le catalogue models.dev vient d'un flux
tiers. Elle est contrôlée à la création et au test seulement. Si son DNS change
ensuite (rebinding), Plugboard enverra la clé du fournisseur et les prompts vers une
adresse interne (127.0.0.1, 10.x, 169.254.169.254…). Le contrôle doit porter sur
**chaque connexion**, pas sur le nom une fois pour toutes.

## Existant (vérifié au code)

- `_require_public_host` (`plugboard/app.py`) appelle `llm_pricing._resolve_public` :
  le nom est refusé si une seule réponse n'est pas publique (`ip_is_public` : privé,
  boucle locale, lien local, métadonnées, CGNAT, NAT64, IPv4 encapsulé). Appelé par
  `create_direct_provider` (adresses jetées ensuite) et `test_direct_provider` (le test
  se connecte aux adresses vérifiées via `_PinnedResolver`).
- Le fournisseur créé garde `preset_key = "catalog-<id>"` (`models_dev.PRESET_PREFIX`)
  et une `base_url` figée (`direct_providers.create_from_preset`).
- À l'usage, rien ne revérifie : `router.forward`, `forward_stream` et la découverte des
  modèles, ainsi que `model_availability`, passent par `http_pool.get_session()`
  (résolveur par défaut, cache DNS 300 s, `trust_env=True`) ; `health_probe._probe_http`,
  `provider_watchdog._deep_health`, `monitoring.check_model_availability` et
  `semantic_cache` ouvrent leur propre session.
- aiohttp 3.14.5 (épinglé, `requirements.in`), vérifié dans context7 et dans la source :
  `AbstractResolver.resolve(host, port, family)` rend des `ResolveResult` ; le résolveur
  **n'est pas appelé** pour une IP littérale (`TCPConnector._resolve_host`) ni pendant
  `ttl_dns_cache` ; `socket_factory` (depuis 3.12) reçoit chaque `AddrInfoType` juste avant
  `sock_connect`, pour toute nouvelle connexion ; une `OSError` levée là fait passer
  aiohappyeyeballs à l'adresse suivante, puis aiohttp lève `ClientConnectorError`.

## Buts, non-buts, contraintes

- **Buts** : aucune connexion d'un fournisseur du catalogue vers une adresse non
  publique, quel que soit le chemin (cache DNS, IP littérale, redirection) ; aucun
  changement pour Ollama local ni les fournisseurs créés à la main ; les fournisseurs du
  catalogue déjà créés sont couverts sans migration.
- **Non-buts** : pare-feu de sortie dans le code ; protection contre un hôte public
  compromis (TLS et vérification du nom restent la défense) ; flux models.dev lui-même ;
  interrupteur pour désactiver la garde.
- **Contraintes** : garder le pool partagé (PERF-2, pas de session par requête) ; aucune
  dépendance nouvelle ; Python 3.12, aiohttp 3.14.5.

## Approches

1. **Contrôle à chaque connexion**, sur une seconde session mutualisée « publique
   seulement », choisie par fournisseur. Variante 1a : résolveur filtrant (enveloppe
   `DefaultResolver`, refuse toute réponse non publique) ; simple, l'erreur nomme l'hôte,
   mais **aveugle aux IP littérales** (redirection `Location: http://10.0.0.5/`).
   Variante 1b : `socket_factory` qui refuse toute adresse non publique ; contrôle
   l'adresse exacte connectée, sans fenêtre entre contrôle et connexion, couvre cache,
   littéraux et redirections. Coût : un second pool, l'erreur ne voit que l'IP.
2. **Épinglage à la création** : stocker les IP vérifiées dans l'enregistrement et se
   connecter via `_PinnedResolver` (existant), un connecteur par fournisseur. Aucun DNS à
   l'usage, mais panne dès qu'un fournisseur change d'IP légitimement (CDN, anycast)
   jusqu'à un ré-épinglage, qui a besoin… du contrôle de l'approche 1 ; migration des
   fournisseurs existants ; N pools.
3. **Liste d'autorisation** : rendre `PLUGBOARD_PROVIDER_DOMAINS_ALLOWED` obligatoire
   pour le catalogue, ou filtrer la sortie au pare-feu. Une liste de noms n'arrête pas le
   rebinding (un nom autorisé peut résoudre en 10.x) et annule l'intérêt du catalogue ;
   le pare-feu est solide mais hors dépôt et propre à chaque installation : à documenter
   comme défense en profondeur, pas comme correctif.

## Recommandation : approche 1, variante 1b

- `plugboard/http_pool.py` : second singleton construit avec
  `TCPConnector(socket_factory=_public_only_socket, …)` ; `get_session(public_only=False)` ;
  `session_for(provider)` ; `close_session()` ferme les deux. `_public_only_socket` lève
  `EgressRefused(OSError)` si `ip_is_public(addr_info[4][0])` est faux, avec un message
  constant (sans l'IP) : aiohappyeyeballs ne relance l'exception d'origine, donc son
  type, que si toutes les tentatives ont le même message.
- `ip_is_public` sort de `llm_pricing` vers un petit module commun (ex. `netguard.py`),
  réexporté pour ne rien casser (choix du constructeur).
- `is_catalog_provider(p)` : `preset_key` commence par `models_dev.PRESET_PREFIX`.
- Appelants qui envoient la clé d'un fournisseur : `router.py` (forward, forward_stream,
  découverte), `model_availability.py`, `health_probe.py`, `provider_watchdog.py`,
  `monitoring.py`, `semantic_cache.py` passent par `session_for` / `public_only`.
- Erreur : le routeur reconnaît `EgressRefused` (dans `ClientConnectorError.os_error`),
  répond 502 « l'hôte du fournisseur résout vers une adresse non publique », journal et
  audit `provider.egress_refused` (suite selon Q2). Résolveur filtrant 1a en plus pour
  nommer l'hôte : choix du constructeur.
- Inchangés : `_require_public_host` à la création et au test (refus 422 précoce),
  `_test_openai_endpoint`, `plugboard/ollama.py`, `direct_providers.py` hormis l'aide.
- **Ollama local** (`bootstrap_ollama`, UAT `172.17.0.1:11435`) et **fournisseurs créés
  à la main** (point d'accès personnalisé, préréglages intégrés) : pas de `catalog-`,
  session partagée inchangée, le réseau local reste permis.
- Docs : `providers-and-subscriptions.md`, `deployment.md` (filtrage de sortie
  conseillé), `CHANGELOG.md`.

## Tests prévus

1. `_public_only_socket` refuse 127.0.0.1, 10.0.0.1, 169.254.169.254, 100.64.0.1, ::1,
   ::ffff:10.0.0.1, 64:ff9b::a00:1, fe80::1%eth0 ; accepte 1.1.1.1 et 2606:4700::1111.
2. Rebinding : serveur de test sur 127.0.0.1 qui compte les appels, résolveur factice
   `api.rebind.test → 127.0.0.1` : session publique refusée (`EgressRefused`, 0 appel) ;
   même requête par la session partagée : 200 (le local marche toujours).
3. IP littérale `http://127.0.0.1:<port>/` par la session publique : refusée, 0 appel
   (chemin qui contourne le résolveur, donc aussi celui d'une redirection).
4. `session_for` : publique pour `catalog-x`, partagée pour `""`, `openai`, Ollama ; chaque
   appelant listé l'utilise.
5. Routeur : 502 et entrée d'audit ; disjoncteur selon Q2. Non-régression :
   `tests/test_models_dev.py`, `tests/test_llm_pricing.py`, tests Ollama, `make test`.

## Questions pour YB

- **Q1 — Proxy (`HTTPS_PROXY`)** : 1) les fournisseurs du catalogue ignorent le proxy
  (`trust_env=False`), comme le test aujourd'hui — recommandé ; 2) ils l'utilisent et
  l'adresse du proxy est exemptée : la garde repose alors sur le proxy.
- **Q2 — Refus** : 1) la requête est refusée (502, audit), le fournisseur reste actif et
  repart seul quand le DNS redevient public, le disjoncteur ne compte pas ce refus —
  recommandé ; 2) en plus, désactiver le fournisseur au premier refus et alerter.
- **Q3 — Périmètre** : 1) fournisseurs du catalogue seulement (D-16) — recommandé ;
  2) aussi les préréglages intégrés en nuage (OpenAI, Anthropic…) ; 3) en plus, une
  option par fournisseur créé à la main.

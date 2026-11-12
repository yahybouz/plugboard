# Outils MCP d'administration (PB-MCP-02) — conception

Statut : proposée le 2026-11-12, en attente de la validation de YB.
Base : `fix/mcp-client-identity` à `81a0a85` (PB-MCP-01, avec la 4.9.0 et PLU-13).

## But

Deux outils MCP, `add_provider` et `refresh_catalog`, pour qu'un agent de code ajoute un
fournisseur compatible OpenAI et rafraîchisse son catalogue sans le tableau de bord (fiche
TODOS « MCP admin tools for providers and catalog »).

## Ce qui existe

- `POST /providers` (`plugboard/app.py:3771`), admin : refuse un hôte hors
  `PLUGBOARD_PROVIDER_DOMAINS_ALLOWED` (400) ; case « Public addresses only » cochée, refuse une
  URL non http(s) ou un hôte non public (422, 503 si introuvable), refus audités. Case
  décochée par défaut : `http://127.0.0.1` ou `http://169.254.169.254` passent.
- `POST /providers/{id}/discover` (`app.py:3918`) : écrit le catalogue, retraits immédiats,
  aucun délai entre deux appels ; un fournisseur CLI peut y sonder chaque modèle.
- Fournisseur protégé : chaque connexion est vérifiée (épinglage DNS, `plugboard/netguard.py`).
- Une seule identité admin : `ADMIN_KEY` ou une session. MFA imposée : la clé seule reçoit 401.
- Serveur MCP : `--sse`/`--http` ne chargent jamais la clé admin ; `provider_status` refuse.
- Un fournisseur **actif** est candidat pour tout modèle non préfixé, quel que soit le client
  (`plugboard/router.py:645`, `_resolve_all_providers`).

## Modèle de menace (STRIDE)

Appelants : l'opérateur qui lance le serveur stdio (il a déjà la clé admin, ce n'est pas
l'adversaire) ; l'agent de code, qu'un texte lu peut piéger (injection de prompt) ; un appelant
réseau muni du jeton MCP.

| Risque | Scénario | Parade |
|---|---|---|
| S | un porteur du jeton MCP réseau agit en admin | outils refusés en réseau (inchangé) |
| T | agent piégé : ajoute `https://pirate.example`, actif, qui reçoit les prompts non préfixés de tous les clients | fournisseur créé **inactif**, l'admin l'active |
| T | changer l'URL d'un fournisseur existant envoie sa clé ailleurs | aucun outil de modification ni de suppression |
| I | SSRF : URL interne (`127.0.0.1`, `10.x`, `169.254.169.254`) | case « adresses publiques » **imposée** ; DNS vérifié à chaque connexion |
| I | la clé du fournisseur ressort (réponse, erreur, audit, journal) | jamais renvoyée ni auditée, effacée des erreurs ; journal du SDK MCP plafonné à INFO (en DEBUG il écrit tout le message reçu, `mcp/server/lowlevel/server.py:682`) |
| R | une action MCP ressemble à une action du tableau de bord | audit à la passerelle, acteur `mcp`, refus compris |
| D | boucle d'agent : créations en rafale, rafraîchissements en boucle, sondes CLI | 5 créations/heure, 1 rafraîchissement/minute par fournisseur, un à la fois, CLI refusés |
| E | un réglage existant (clé admin pour `provider_status`) gagne l'écriture sans le savoir | interrupteur `PLUGBOARD_MCP_ADMIN_TOOLS=1` |

Résiduels : en stdio, qui a la clé admin peut tout faire au `curl` ; les limites arrêtent un
agent qui s'emballe, pas l'opérateur. Un refus « resolves to a non-public address (10.0.0.5) »
révèle qu'un nom interne existe. L'acteur `mcp` vient d'un en-tête : il distingue, il ne prouve
pas. La clé passe forcément par le contexte de l'agent.

## Approches

| Approche | Pour | Contre |
|---|---|---|
| **A. L'outil appelle la route admin (retenue)** | mêmes validations, audit, webhook, cache, épinglage DNS, par construction ; passerelle en Docker possible | clé admin dans le processus MCP ; hors service si la MFA admin est imposée |
| B. Réimplémenter en local (`config.create_provider`) | pas d'appel HTTP | exige `ENCRYPTION_KEY` et le même `data/` ; cache de la passerelle non vidé ; validation recopiée ; le serveur MCP n'importe pas FastAPI (`test_contract_without_fastapi`) |
| C. Clé admin dédiée à portée limitée | réseau possible, compatible MFA | touche le cœur de l'authentification : conception à part |

## Comportement retenu (réponses recommandées)

1. `add_provider(name, base_url, api_key, default_model="")` et
   `refresh_catalog(provider_id, dry_run=False)`, en stdio seulement.
2. Utilisables seulement avec `PLUGBOARD_MCP_ADMIN_TOOLS=1` et `PLUGBOARD_ADMIN_KEY` ; sinon
   refus clair, sans appel. MFA imposée : refus qui renvoie au tableau de bord.
3. `add_provider` appelle `POST /providers` avec `is_active=false` et `egress_guard=true` ;
   la réponse donne l'identifiant, jamais la clé.
4. `refresh_catalog` appelle `POST /providers/{id}/discover` ; refuse un fournisseur CLI
   (`subprocess:`, `cli:`).
5. Limites en mémoire du processus MCP : 5 `add_provider` par heure glissante (refus compris) ;
   par fournisseur, 1 aperçu et 1 écriture par minute ; un rafraîchissement à la fois.
6. Passerelle : les deux routes notent l'acteur `mcp` quand `X-Plugboard-Surface: mcp` est
   présent, et auditent alors aussi refus et aperçus. Le tableau de bord ne change pas.

Zone partagée : `plugboard/app.py`, dans les deux routes fournisseurs et `_require_guard_allows`
seulement (ni routeur, ni Ollama, ni `static/`) ; conflit éventuel avec PLU-12 réglé à
l'assemblage.

## Questions pour YB

**Q1 — Transports ?** 1. stdio seulement (recommandé : en réseau, tout porteur du jeton MCP
serait admin ; l'engagement du CHANGELOG reste vrai). 2. Aussi en réseau (exige Q2-3).

**Q2 — Authentification ?** 1. Clé admin existante + interrupteur
`PLUGBOARD_MCP_ADMIN_TOOLS=1`, refus si MFA imposée (recommandé : aucun nouveau secret, aucune
retouche de l'authentification). 2. Clé admin seule. 3. Clé admin dédiée (ticket à part).

**Q3 — Audit ?** 1. À la passerelle, acteur `mcp`, réussites, aperçus et refus (recommandé :
une seule source, lisible dans `/admin/audit`). 2. Réussites seulement, acteur `admin`.

**Q4 — Validation ?** 1. La route `POST /providers` elle-même, case « adresses publiques »
imposée (recommandé : mêmes refus par construction, pas de SSRF). 2. Case au choix de l'agent
(permet un Ollama local, rouvre le SSRF).

**Q5 — Débit ?** 1. Limites du point 5 dans le processus MCP, CLI refusés (recommandé :
arrête une boucle d'agent sans toucher la passerelle). 2. Délai côté passerelle sur
`/discover`, tableau de bord compris. 3. Aucune.

**Q6 — Fournisseur créé actif ?** 1. Inactif, l'admin l'active d'un clic (recommandé : un
agent piégé ne détourne pas les appels des autres clients). 2. Actif, comme au tableau de bord.

**Q7 — Préréglages du catalogue (`POST /admin/direct-providers/{key}`) ?** 1. Pas dans ce
ticket (recommandé : une seule route, moins de surface). 2. Aussi, sauf CLI et OAuth.

## Hors ticket

Modifier ou supprimer un fournisseur par MCP ; outils en réseau et clé admin dédiée ;
préréglages (si Q7-1). Constat à part : `POST /providers` accepte un nom qui donne un
identifiant vide (`"!!!"`, `config.py:195` et `330`), lu au code, non exécuté.

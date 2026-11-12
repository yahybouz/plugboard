# Identité du client MCP tirée de sa clé (PB-MCP-01) — conception

Statut : proposée le 2026-11-12, en attente de la validation de YB.
Base : `sprint/recette-uat` à `dde9e53` (4.9.0 + PLU-13).

## Problème

Le serveur MCP est un processus qui parle à la passerelle avec **une** clé client,
`PLUGBOARD_API_KEY`. Les outils du registre de mises à jour, eux, prennent le client
dans un argument `client_id` écrit librement par l'appelant. N'importe quel appelant
peut donc lire les mises à jour en attente d'un autre client, lire leurs instructions
complètes, et les marquer « appliquées » à sa place (elles disparaissent chez lui).

## Outils concernés (`plugboard/mcp_server.py`)

| Outil | Ligne | Aujourd'hui | Après |
|---|---|---|---|
| `check_updates(client_id)` | 912 | attentes du client donné | client de la clé |
| `list_applied(client_id)` | 1089 | historique du client donné | client de la clé |
| `mark_applied(client_id, prompt_id, notes)` | 1035 | écrit pour le client donné | client de la clé |
| `get_update_prompt(prompt_id)` | 973 | toute mise à jour, avec la liste de tous ses clients | seulement si elle vise le client de la clé |
| ressource `updates://pending` | 1118 | attentes de **tous** les clients | client de la clé seulement |

Déjà liés à la clé, inchangés : `check_usage` (644, `/v1/me`), `chat`, `chat_stream`,
`embed`, `decide`, `list_*`. `provider_status` (714) : clé admin, stdio seulement.

## Contrôle de `inflight_requests` (PLU-13, ligne 685)

Pas de fuite avec une clé client : l'outil ne prend aucun argument et la passerelle
filtre par le client de la clé (`plugboard/app.py:2971-2980`,
`plugboard/live_requests.py:161-162`). Deux réserves :

- si `PLUGBOARD_API_KEY` contient la **clé admin**, l'outil montre les appels de tous
  les clients avec leur fournisseur ; en `--sse`/`--http`, à tout porteur du jeton MCP ;
- le filtre est par client, pas par clé : un client à plusieurs clés voit les appels de
  toutes ses clés (le contrat dit « the calling key's » : imprécis, pas une fuite).

## Comportement retenu (réponses recommandées ci-dessous)

1. Le client est le `client_id` que renvoie `GET /v1/me` avec `PLUGBOARD_API_KEY`,
   demandé à chaque appel d'un outil du registre (pas de cache).
2. Refus avec un message clair, **avant** toute lecture du registre ou de la base, si :
   pas de clé ; passerelle injoignable ou clé refusée ; clé sans client ; clé admin.
3. `client_id` devient facultatif : absent ou égal au client de la clé, l'appel passe ;
   différent, il est refusé.
4. Une mise à jour d'un autre client reçoit la même réponse qu'une mise à jour
   inexistante ; aucun nom d'autre client n'est plus affiché.
5. Même règle en stdio, `--sse` et `--http` : l'identité est celle du processus. En
   réseau, tous les porteurs du jeton MCP sont le même client : un serveur MCP par client.

## Approches envisagées

| Approche | Pour | Contre |
|---|---|---|
| **A. `GET /v1/me` (retenue)** | une seule source (les clés de la passerelle) ; marche sur les 3 transports et sur une autre machine ; clé révoquée refusée aussitôt | un appel HTTP de plus ; le registre ne répond plus passerelle arrêtée |
| B. Lire le fichier des clés en local (stdio) | aucun appel HTTP | impossible en réseau ou sur une autre machine ; recopie la logique d'authentification (expiration, révocation) |
| C. Variable `PLUGBOARD_MCP_CLIENT_ID` | trivial | déclarée à la main, pas tirée de la clé |
| D. Chaque appelant réseau envoie sa clé `pb_`, relayée | plusieurs clients par serveur | interdit par la spec MCP : « MCP servers MUST NOT accept any tokens that were not explicitly issued for the MCP server » ; gros chantier, hors ticket |

## Compatibilité

- Agent qui passe son propre `client_id`, ou rien : inchangé.
- Agent qui passait un autre `client_id` : refusé (c'est le correctif).
- Serveur MCP sans `PLUGBOARD_API_KEY` : outils du registre refusés (rupture voulue).
- Registre : `clients` doit désormais égaler le `client_id` des clés (la doc disait
  « should match »). Un nom différent se corrige dans `registry.json` ; les mises à jour
  marquées sous l'ancien nom redeviennent en attente. Note de montée dans le CHANGELOG.
- Contrat `docs/api/mcp.json` : `client_id` passe de requis à facultatif (compatible).
  Il se régénère avec le venv principal (mcp 1.30.0, même version que
  `requirements-mcp.lock` ; `export_contract.py --mcp --check` y passe aujourd'hui) :
  `.venv-mcp` n'est pas nécessaire. `CONTRACT_VERSION` reste 1.5.0 (pas encore publiée).

## Hors ticket

Identité par appelant en réseau (approche D) ; outils MCP d'administration (PB-MCP-02) ;
texte « the calling key's » du contrat `/v1/inflight` (à corriger à part).

## Questions pour YB

**Q1 — Comment trouver le client de la clé ?**
1. `GET /v1/me` à chaque appel, sans cache (recommandé : une seule source, révocation
   immédiate ; ces outils servent en début de session, c'est rare).
2. La même chose avec un cache de 60 s.
3. Lecture locale du fichier des clés, en stdio seulement.

**Q2 — Que faire si l'appelant fournit encore `client_id` ?**
1. Facultatif, refusé s'il diffère (recommandé : un agent mal réglé l'apprend, au lieu
   de voir « rien en attente » sans comprendre).
2. Retiré de l'outil, ignoré en silence.
3. Gardé et ignoré.

**Q3 — Serveur MCP lancé sans clé client ?**
1. Outils du registre refusés (recommandé : un repli sur l'argument rouvre la faille).
2. Repli sur l'argument en stdio seulement.

**Q4 — Clé admin mise dans `PLUGBOARD_API_KEY` ?**
1. Refusée par les outils du registre et par `inflight_requests`, sur tous les
   transports (recommandé : le serveur MCP est documenté avec une clé client ; l'admin a
   le tableau de bord et `/admin/inflight`).
2. Refusée par les outils du registre seulement.
3. Rien.

**Q5 — Ressource `updates://pending` (aujourd'hui : tous les clients) ?**
1. Limitée au client de la clé (recommandé : la même règle partout).
2. Vue de tous les clients gardée en stdio avec clé admin.
3. Supprimée.

**Q6 — `get_update_prompt` sur une mise à jour d'un autre client ?**
1. Même réponse que « introuvable » (recommandé : ne confirme pas qu'un identifiant
   existe chez un autre client).
2. Refus explicite « ne vous concerne pas ».

# Répartiteur de charge, règles simples (PLU-9 lot 1, PLU-8) — conception

Statut : conception courte, périmètre fixé par YB le 11/11 (D-30). Base : 4.8.0,
`e2320a3`. Étude : `2026-11-11-etude-repartiteur-charge.md`.

## Intention

Les outils de YB passent surtout par des API cloud et des abonnements CLI, qui
gèrent leur propre charge ; chaque requête est indépendante. Il faut donc des
règles simples : ne pas noyer un fournisseur local, ne pas insister sur une route
qui a dit d'attendre, compter toute la charge, et ne plus perdre un tour de
session.

## Périmètre (et seulement lui)

1. **Plafond par fournisseur (D-22).** Local (Ollama, `provider_type` `sovereign`,
   adresse loopback ou privée) : 1 requête à la fois par défaut. Autres : sans
   plafond, sauf champ `max_concurrency` du fournisseur. Les CLI gardent
   `PLUGBOARD_SUBPROCESS_LIMIT` (4 par type), sans autre plafond par défaut.
2. **Fournisseur plein (D-23).** La requête passe au candidat suivant qui sert ce
   modèle. Si tous sont pleins : attente bornée (10 s), le premier créneau libéré
   la prend (file FIFO), puis `503` avec `Retry-After`. Un fournisseur plein n'est
   pas en panne : ni disjoncteur, ni métrique d'erreur, ni alerte `provider_down`.
3. **Pause après 429.** Durée = `Retry-After` ; sans durée : 30 s, puis 1 min,
   puis 5 min (429 consécutifs sur la route, remis à zéro au succès). Un 429 dont
   le `Retry-After` dépasse le prochain délai prévu n'est plus réessayé sur place.
   Même pause pour un quota CLI (`retry_after` lu dans son message). Une route
   (`fournisseur:modèle`, clé du disjoncteur) en pause ou au disjoncteur ouvert
   passe en fin de liste, pauses triées par fin ; elle reste essayée.
4. **Compteur en cours complet** : un bail par requête sur toutes les voies (HTTP,
   flux, CLI), rendu dans un `finally`. Il remplace `_IN_FLIGHT_BY_PROVIDER`
   (`router.py:290`, seule voie comptée aujourd'hui : `router.py:840`) et nourrit
   le score existant (`PLUGBOARD_LB_INFLIGHT_WEIGHT`, formule inchangée).
5. **Sessions (D-24).** Quand le client envoie un identifiant de session, les
   tours de cette session passent l'un après l'autre : le second attend la fin du
   premier (pas de 409, pas de délai propre) et voit sa réponse ; s'il se
   déconnecte en attendant, il quitte la file. Les tours streamés (HTTP et CLI)
   sont enregistrés. Sans identifiant, rien ne change.

**Hors périmètre** : score sur fenêtre récente, priorités et poids, filtre par
catalogue, repli des flux, pools de comptes CLI, tableau de bord de charge,
plafond par clé client, tout réglage d'Ollama lui-même.

## Décisions

- **D-22** plafond : local 1, autres sans plafond, réglable par fournisseur.
- **D-23** plein : suivant, sinon attente 10 s puis 503.
- **D-24** session : le second tour attend, sans 409.
- **D-30** règles simples, pas de lot 2 ni 3 pour l'instant.
- **D-7** inchangée : le disjoncteur ne compte que les pannes du fournisseur.
- Supposé (réversible) : le verrou suit le même déclencheur que le stockage de
  session existant, en-tête `X-LLM-Session-ID` **ou** champ `session_id`
  (documentés comme équivalents), et seulement pour un client identifié.

## Réglages

| Nom | Défaut | Effet |
|---|---|---|
| `PLUGBOARD_LOCAL_PROVIDER_MAX_INFLIGHT` | `1` | Plafond d'un fournisseur local sans `max_concurrency`. `0` : sans plafond (comportement 4.8.0). |
| `PLUGBOARD_PROVIDER_WAIT_S` | `10` | Attente quand tous les candidats sont pleins. `0` : 503 tout de suite. |
| champ `max_concurrency` (fournisseur) | absent | Entier ≥ 0, prioritaire ; `0` = sans plafond. `providers.json` ou `PUT /providers/{id}`. |

Constantes : paliers 30/60/300 s, `Retry-After: 5` du 503. Inchangés :
`PLUGBOARD_MAX_INFLIGHT_CHAT` et `PLUGBOARD_INFLIGHT_WAIT_S` (appliqués avant),
`PLUGBOARD_SUBPROCESS_LIMIT`, `PLUGBOARD_RETRY_DELAYS` (5xx et 429 courts),
`PLUGBOARD_LB_*`, `PLUGBOARD_SESSION_*`. Les anciens noms `LLM_ENABLER_` sont
recopiés par `envcompat` comme pour tout réglage.

## Ce que voient les clients

- `503` `{"detail", "code": "providers_busy", "retry_after": 5}` et
  `Retry-After: 5` quand tous les candidats sont restés pleins (`/v1` et `/v2`,
  streamé ou non). Contrat d'API 1.4.0 (valeur `code` ajoutée).
- Jusqu'à 10 s de latence de plus sur un fournisseur local occupé.
- `429` : même forme ; arrive tout de suite quand le `Retry-After` dépasse le
  prochain délai (au lieu de ~22 s d'essais).
- Session : un tour concurrent attend et voit le précédent ; client parti : `499`
  dans le journal, rien facturé ni enregistré. Les tours streamés complets sont
  enregistrés.
- Nom de modèle nu : l'ordre peut changer (flux et CLI comptent, routes en pause
  ou disjoncteur ouvert en dernier).
- Inchangé : `404 model_not_found` (avant toute prise de créneau), `403` de
  `route_guard` vérifié sur chaque candidat avant son créneau, libération à la
  déconnexion (BUG-30, `_unless_client_gone`).

## Risques

- **Défaut local à 1** : un Ollama recevait des requêtes parallèles ; il les
  sérialise désormais. À écrire dans le CHANGELOG ; `PLUGBOARD_LOCAL_PROVIDER_MAX_INFLIGHT=0` revient à 4.8.0.
- **Classement local** : Ollama Cloud (`https://ollama.com`) n'est pas local ;
  une adresse Tailscale (CGNAT) non plus : régler `max_concurrency`.
- **Créneau jamais rendu** : flux abandonné sans fermeture (rendu au ramasse-miettes),
  annulation pendant une attente, créneau accordé au moment d'une annulation.
  Bail idempotent, tests dédiés.
- **Attente et plafond global** : une requête qui attend (fournisseur ou tour de
  session) garde son créneau `PLUGBOARD_MAX_INFLIGHT_CHAT`. L'attente de session
  n'a pas de borne propre (au plus la durée du premier tour, 900 s pour un CLI).
- **Appels internes** : preflight, sonde de santé et passe de prix passent par
  `forward()` : ils attendent ou reçoivent « saturé » comme un client.
- **Un seul processus** : compteurs, pauses et verrous en mémoire, non partagés
  entre répliques.
- **Conflits avec PLU-7** (`feat/catalog-dns-pinning`) : tête de boucle de
  `forward()` (même zone que `session_for`), tête et voie HTTP de
  `forward_stream()`, fin de `ProviderUpdate` (`egress_guard`), `[Unreleased]`
  du CHANGELOG. Faire le lot après la fusion de PLU-7, rebase d'abord.

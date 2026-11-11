# Étude PLU-9 — Répartiteur de charge entre fournisseurs (avec le plafond PLU-8)

Statut : étude, à valider par YB. Date : 2026-11-11.
Base lue : Plugboard 4.8.0, commit `43a8487` (branche `docs/etude-repartiteur-charge`).
Aucun code produit modifié, aucune inférence lancée.

## En bref

- Plugboard ne répartit la charge que pour un nom de modèle **sans préfixe**
  fournisseur. Le compteur « en cours » ne voit que les appels HTTP non streamés.
  Il n'existe **aucun plafond par fournisseur**. Un 429 est réessayé environ
  22 s sur le même fournisseur, sans tenir compte de son `Retry-After`. Un tour
  de session peut être perdu.
- Hermes et OpenClaw font les mêmes quatre choix : une pause par compte, de la
  durée annoncée par le fournisseur ; la rotation des comptes avant de changer
  de modèle ; un plafond de concurrence (bail chez Hermes, files *lanes* chez
  OpenClaw) ; un seul tour à la fois par session.
- Recommandation : **approche A** (admission par fournisseur et pauses
  fiables, ce qui couvre PLU-8), puis **B** (score), puis **C** (pools de
  comptes) dans une étude à part.

## 1. Ce que fait Plugboard 4.8.0

| Mécanisme | Où | Comportement constaté |
|---|---|---|
| Candidats | `router.py:395-442`, `639-648` | `fournisseur:modèle` donne un seul candidat, sans répartition (`418-421`). Un nom nu prend tous les fournisseurs actifs (`644`), triés par score (`645-647`). |
| Score | `router.py:296-345` | `latence_moy × (1 + taux_erreur × 5,0) × (1 + en_cours × 0,35)`, le plus bas gagne (`PLUGBOARD_LB_ERROR_RATE_WEIGHT`, `PLUGBOARD_LB_INFLIGHT_WEIGHT`). |
| Compteur en cours | `router.py:303`, `319-330`, `841` | `_IN_FLIGHT_BY_PROVIDER` n'entoure que la boucle HTTP non streamée de `forward()`. |
| Repli | `router.py:698-969` | Les candidats sont essayés dans l'ordre. HTTP : un essai, plus `PLUGBOARD_RETRY_DELAYS` (2, 5 et 15 s) sur 429, 500, 502, 503 et 529 (`55`, `842-878`). CLI : pas de nouvel essai, on passe au suivant (`788-818`). |
| Flux HTTP | `router.py:972-1184` | Un seul fournisseur (`990`), pas de repli, pas compté en cours. Les flux CLI passent par `forward()` (`app.py:2102-2119`). |
| Disjoncteur | `circuit_breaker.py:4-9`, `28-30`, `125-151` | Clé `fournisseur:modèle`. Il s'ouvre après 3 échecs et passe en demi-ouvert après 60 s. Il ne sert qu'à l'information : `forward()` ne refuse ni ne déclasse une route ouverte (`router.py:722-726`). |
| Plafond global | `app.py:583-648` | `PLUGBOARD_MAX_INFLIGHT_CHAT` (0 = désactivé), attente `PLUGBOARD_INFLIGHT_WAIT_S` (0), puis 503 avec `Retry-After: 5`. Le créneau est gardé pendant tout le flux. |
| Plafond CLI | `cli_subprocess.py:116-133`, `1700` | Un sémaphore par type de CLI, `PLUGBOARD_SUBPROCESS_LIMIT` (4), attente sans limite. Il est partagé par tous les fournisseurs du même type. |
| Limites par clé | `app.py:1344-1362`, `1399-1414` ; `ratelimit.py:55-67` | Budget mensuel (429) et `rpm_limit` sur 60 s, en mémoire. Rien sur la concurrence par clé. |
| Quotas | `router.py:59-72`, `147-179` ; `cli_subprocess.py:1122-1202` ; `app.py:1697-1734` | Un 429 HTTP donne `ProviderQuotaError.retry_after`. Un quota CLI est lu dans son message (« try again at 6:00 PM »). Ces deux informations servent seulement à la réponse au client, jamais au routage. |
| Sessions | `session_store.py:156-204` ; `app.py:1930-1938`, `2490-2508` | L'historique est ajouté avant l'appel, l'échange est enregistré après, par comparaison de préfixe. |

Constats :

- **C1. Pas de plafond par fournisseur (PLU-8).** Un Ollama local ou un compte
  d'abonnement peut recevoir toutes les requêtes en même temps.
- **C2. Compteur en cours incomplet.** Flux et CLI ne passent pas par
  `_track_in_flight` : le score ignore la charge la plus lourde.
- **C3. Score sur toute la vie du processus, par fournisseur seulement**
  (`metrics.py:9-24`). Une vieille panne pèse toujours ; les modèles lents et
  rapides d'un même fournisseur sont mélangés.
- **C4. Candidats non filtrés.** Avec un nom nu, un fournisseur qui ne sert pas
  le modèle peut passer en tête et coûter un appel (`router.py:644`).
- **C5. 429 et quotas.** Trois nouveaux essais quel que soit le `Retry-After`
  (`router.py:842-878`), aucune pause retenue : la requête suivante
  recommence. Un CLI à court de quota est relancé à chaque requête.
- **C6. Le disjoncteur ne change pas l'ordre.**
- **C7. Pas de repli pour un flux HTTP**, même avant le premier octet.
- **C8. Tour de session perdu.** Vérifié avec un script jetable sur le vrai
  `session_store` (dossier temporaire supprimé, `PLUGBOARD_SESSION_MAX_TURNS=6`) :
  - deux prompts simultanés sur un historique plein : le tour A disparaît
    (`['h2','a2','h3','a3','B','rB']`), car la comparaison de préfixe
    (`session_store.py:194-201`) échoue après la coupe (`112-113`) ;
  - loin du plafond, rien n'est perdu, mais B a répondu sans voir A ;
  - deux processus sur le même `sessions.db` : la dernière écriture gagne ;
  - un tour streamé n'est jamais enregistré : `record_exchange` n'est appelé
    que dans la voie non streamée (`app.py:2490-2508`).

Un seul processus uvicorn (`run.py:90-96`) : des compteurs en mémoire suffisent
tant qu'on ne lance pas plusieurs répliques.

## 2. Hermes Agent et OpenClaw (sources primaires, lues le 11/11/2026)

**Hermes Agent** (Nous Research). Le cache local `~/.hermes` n'a pas servi de source.

- **Pool d'identifiants par fournisseur** (`agent/credential_pool.py`) :
  stratégies `fill_first` (défaut, ordre `priority`), `round_robin`,
  `least_used`, `random`.
- **Réaction selon la cause** (`EXHAUSTED_TTL_*`) : 429 → un nouvel essai, puis
  rotation et pause 1 h ; limite d'usage d'abonnement → rotation sans essai ;
  402 → rotation, 1 h ; 401 → rafraîchir l'OAuth, sinon 5 min ; compte seul →
  60 s au plus. Un `reset_at` annoncé remplace ces durées. Chez Anthropic, la
  pause pour 429 ne vaut que pour le modèle concerné.
- **Bail de concurrence** (`acquire_lease`/`release_lease`,
  `DEFAULT_MAX_CONCURRENT_PER_CREDENTIAL = 1`) : le compte le moins chargé sous
  le plafond ; si tous sont pleins, le moins chargé quand même (plafond souple,
  jamais bloquant). Mutations sous verrou.
- **Repli entre fournisseurs** (`fallback_providers`) après le pool, tour par
  tour : le principal est retenté au tour suivant, sauf si son reset annoncé
  n'est pas passé ; sinon pause exponentielle de 60 s à 4 h. La doc prévient :
  changer de compte ou de modèle fait relire tout le prompt au plein tarif.
- **Un seul agent actif par session** : un message reçu pendant un tour est mis
  en file ou interrompt (`busy_input_mode`), jamais lancé en parallèle.

**OpenClaw.**

- **Deux étages** : rotation des profils d'authentification du fournisseur,
  puis modèle de repli (`agents.defaults.model.fallbacks`).
- **Ordre** : `auth.order` s'il existe ; sinon OAuth, puis jeton, puis clé API,
  le moins récemment utilisé d'abord ; profils en pause en fin de liste, triés
  par fin de pause.
- **Pauses** : « OpenClaw n'invente pas de pause de débit » : exactement la
  durée annoncée (`Retry-After`, `retry-after-ms`, « try again in 6s »,
  `x-ratelimit-reset-*`) ; sans durée, le tour passe au suivant mais le profil
  reste disponible. Autres pannes : 30 s, 1 min, puis 5 min. Crédit épuisé :
  10 min. Pause possible pour un seul modèle (`cooldownModel`). Attente sur
  `Retry-After` plafonnée à 60 s, puis bascule.
- **Concurrence** : file FIFO par *lane* plafonnée (`drainLane` démarre tant
  que `activeTasks < maxConcurrent`, `src/process/command-queue.ts`) ; *lane*
  `main` plafonnée par `agents.defaults.maxConcurrent`. Une boîte par session
  n'accorde qu'un tour à la fois. Tous candidats saturés : le premier libéré
  prend la main.
- **Adhérence** : profil attaché à la session (cache de prompt chaud) ; après un
  repli pour 429, la session garde le repli 10 min d'inactivité, 30 min au plus.
- **Limite annoncée** : état en mémoire, pas de coordination entre processus.

## 3. Approches possibles

**A. Admission par fournisseur et pauses fiables (PLU-8).**

- Un champ `max_concurrency` par fournisseur (0 = sans plafond) et un défaut
  global `PLUGBOARD_PROVIDER_MAX_INFLIGHT`.
- Un compteur de baux unique, pris sur toutes les voies (HTTP, flux, CLI). Il
  remplace `_IN_FLIGHT_BY_PROVIDER`.
- Fournisseur plein : on passe au candidat suivant, comme le bail d'Hermes.
- Tous les candidats pleins : on attend le premier créneau libéré parmi eux
  (file FIFO bornée, comme les *lanes* d'OpenClaw), au plus
  `PLUGBOARD_PROVIDER_QUEUE_WAIT_S`. Ensuite, réponse 503 avec `Retry-After`.
- Pauses par route :
  - 429 ou quota avec une durée annoncée : pause de cette durée exacte, sans
    nouvel essai si elle dépasse le prochain délai prévu.
  - Sans durée annoncée : 30 s, puis 1 min, puis 5 min.
  - Une route en pause ou dont le disjoncteur est ouvert passe en fin de liste.
- Sessions : un verrou par session (un tour à la fois), et enregistrement des
  tours streamés.

**B. Répartition pondérée par charge et latence.** Score sur une fenêtre
récente par `fournisseur:modèle` ; candidats filtrés par le catalogue (C4) ;
`priority`, `weight` et stratégie (`fill_first`, `round_robin`, `least_used`) ;
repli d'un flux HTTP avant le premier octet (C7).

**C. Pools de comptes et équité.** Plusieurs abonnements d'un même CLI (un
`CLAUDE_CONFIG_DIR` ou `CODEX_HOME` par fournisseur ; aujourd'hui
`complete(kind, body)` ne reçoit pas le fournisseur,
`cli_subprocess.py:1621-1646`), rotation sur quota, adhérence de la session à
un compte, plafond de concurrence par clé client.

| Approche | Couvre | Effort | Risque | Compromis |
|---|---|---|---|---|
| A | C1, C2, C5, C6, C8 et PLU-8 | moyen | faible | Peu de réglages et compatible. Ne corrige ni le score ni le flux sans repli. |
| B | C3, C4, C7 | moyen | moyen | Meilleure répartition réelle. L'ordre des fournisseurs change pour tous. |
| C | Capacité des abonnements, équité | élevé | élevé | Touche aux identifiants et à l'isolation des CLI, et aux conditions d'utilisation des abonnements. |

## 4. Recommandation

Faire A tout de suite (lot 1, qui clôt PLU-8), puis B (lot 2). C demande une
étude séparée et une décision de YB.

**Repris d'OpenClaw, et pourquoi :**

- La pause dure exactement le temps annoncé, jamais un temps inventé. On ne
  bloque pas un fournisseur une heure pour un 429 d'une seconde.
- Les paliers 30 s, 1 min, 5 min quand rien n'est annoncé.
- Les routes en pause passent en fin de liste, triées par fin de pause.
- La file FIFO plafonnée (`drainLane`).
- Un seul tour à la fois par session : c'est ce qui corrige C8.
- Plus tard, l'adhérence de session, pour garder le cache de prompt.

**Repris d'Hermes, et pourquoi :**

- Le bail souple : passer au fournisseur le moins chargé plutôt que bloquer la
  requête.
- La table « cause → réaction » (429, quota d'abonnement, 402, 401).
- Le repli qui tient compte du reset annoncé.
- Plus tard, les stratégies `fill_first`, `round_robin` et `least_used`.

**Non repris :**

- Le budget de 16 essais d'OpenClaw : trop long pour une passerelle dont les
  clients ont leurs propres délais.
- La pause d'une heure d'Hermes quand rien n'est annoncé.
- Les modes *steer* et *collect* : ils relèvent de l'agent, pas de la
  passerelle.

## 5. Compatibilité des réglages

| Réglage | Après le changement |
|---|---|
| `PLUGBOARD_MAX_INFLIGHT_CHAT`, `PLUGBOARD_INFLIGHT_WAIT_S` | Inchangés. Ils s'appliquent avant le plafond par fournisseur. |
| `PLUGBOARD_SUBPROCESS_LIMIT` | Inchangé : il protège la machine, par type de CLI. Le plafond réel d'un fournisseur CLI est le plus petit des deux. |
| `PLUGBOARD_LB_INFLIGHT_WEIGHT`, `PLUGBOARD_LB_ERROR_RATE_WEIGHT` | Même nom et même formule. Les flux et les CLI comptent désormais comme requêtes en cours (à écrire dans le CHANGELOG). Le lot 2 ajoute `PLUGBOARD_LB_WINDOW_S`. |
| `PLUGBOARD_RETRY_DELAYS` | Inchangé pour les 5xx. Un 429 dont le `Retry-After` dépasse le prochain délai n'est plus réessayé sur place. |
| `rpm_limit`, budget mensuel | Inchangés. |
| Fournisseurs existants | Sans `max_concurrency`, pas de plafond : comportement de 4.8.0, sauf si YB décide d'un défaut. |
| Réponses au client | Les 429 et 503 gardent leur forme et leur `Retry-After`. Nouveau cas : 503 « fournisseurs saturés ». |
| Sessions | Même API. Un deuxième prompt simultané attend son tour (ou reçoit 409, selon la décision de YB). |

## 6. Risques

- **Bail jamais libéré** si un client part (BUG-30) ou si une tâche est
  annulée. Libération dans un `finally`, avec un test dédié.
- **Attente trop longue.** L'attente est bornée et le plafond global reste en
  place.
- **Tous les candidats en pause.** Réponse 429 immédiate avec le temps restant,
  sans appeler le fournisseur, sauf s'il reste 2 s ou moins.
- **Ordre visible qui change (lot 2).** À annoncer. Les valeurs par défaut
  doivent redonner l'ordre de 4.8.0.
- **Plusieurs répliques.** Les compteurs en mémoire ne sont pas partagés (même
  limite qu'OpenClaw). À documenter.
- **Cache de prompt.** Faire passer les tours d'une même session par des
  comptes différents coûte une relecture complète. D'où l'adhérence de
  session, au lot 3.

## 7. Tests prévus

Pas d'inférence réelle : de faux fournisseurs HTTP (serveur aiohttp de test)
et de faux CLI.

- **Plafond** : N+1 appels simultanés sur un fournisseur plafonné à N → le
  N+1ᵉ part au suivant ; seul candidat → attente, puis 503 avec `Retry-After`.
- **Bail libéré** après succès, erreur, délai dépassé, client parti, fin de flux.
- **429 avec `Retry-After: 120`** : pas de nouvel essai, pause 120 s en fin de
  liste, retour en tête ensuite. Même test pour un quota CLI ; sans durée :
  30 s, 1 min, 5 min.
- **Compteur en cours** pour les flux HTTP et les CLI ; **disjoncteur ouvert**
  en fin de liste.
- **Session** : deux prompts simultanés → les deux tours sont gardés et le
  second a vu le premier (le scénario C8 devient un test de régression) ; un
  tour streamé est enregistré.
- **Lot 2** : une erreur ancienne sort de la fenêtre, filtre par catalogue,
  repli d'un flux avant le premier octet.
- **Non-régression** : `tests/test_router.py`, `test_router_network_fallback.py`,
  `test_inflight_disconnect.py`, `test_session_store.py`,
  `test_circuit_breaker_*.py` restent verts sans nouveau réglage.

## 8. Découpe en tickets

| # | Titre | Contenu | Priorité | Lot |
|---|---|---|---|---|
| 1 | Plafond de requêtes simultanées par fournisseur, avec attente bornée | `max_concurrency` et défaut global. Un fournisseur plein passe la main au suivant. Si tous sont pleins, attente puis 503. Clôt PLU-8. | P1 | 1 |
| 2 | Compter les requêtes en cours sur toutes les voies : HTTP, flux et CLI | Un compteur de baux unique, libéré dans un `finally`. | P1 | 1 |
| 3 | 429 et quota : mettre le fournisseur en pause selon Retry-After | La durée annoncée est respectée, sans nouvel essai inutile. Sinon 30 s, 1 min, 5 min. | P1 | 1 |
| 4 | Session : un seul tour à la fois, et enregistrer les tours streamés | Verrou par session. L'ajout ne dépend plus d'une comparaison de préfixe. | P1 | 1 |
| 5 | Route en pause ou disjoncteur ouvert : la placer en fin de liste | Tri par fin de pause. Si tout est en pause, 429 immédiat. | P2 | 1 |
| 6 | Ne proposer que les fournisseurs qui servent le modèle demandé | Filtre par catalogue pour un nom de modèle nu. | P2 | 2 |
| 7 | Score de répartition sur une fenêtre récente, par fournisseur et modèle | Moyenne glissante, réglage `PLUGBOARD_LB_WINDOW_S`. | P2 | 2 |
| 8 | Priorité, poids et stratégie de choix par fournisseur | `fill_first`, `round_robin`, `least_used`. Les valeurs par défaut gardent l'ordre de 4.8.0. | P2 | 2 |
| 9 | Flux HTTP : passer au suivant si l'erreur arrive avant le premier octet | Le repli s'arrête dès que le premier octet est envoyé. | P2 | 2 |
| 10 | Tableau de bord : charge, file d'attente et pauses par fournisseur | Ajout à `/admin/circuits` (`app.py:2903`) et au tableau de bord. | P3 | 2 |
| 11 | Étude : pools de comptes CLI et adhérence de session à un compte | Un dossier de connexion par fournisseur. Vérifier les conditions d'utilisation. | P3 | 3 |
| 12 | Plafond de requêtes simultanées par clé client | Équité entre clients d'une même passerelle. | P3 | 3 |

## 9. Questions pour YB

1. **Plafond par défaut ?**
   1. (recommandé) Désactivé (0), sauf pour les fournisseurs locaux (Ollama,
      llama.cpp) : 1 par défaut.
   2. Une seule valeur pour tous, par exemple 4.
   3. Aucun défaut : réglage à la main seulement.
2. **Que faire quand un fournisseur est plein ?**
   1. (recommandé) Passer au suivant. Si tous sont pleins, attendre 10 s au
      plus, puis 503.
   2. Toujours attendre son tour.
   3. 503 tout de suite.
3. **Quelle pause après un 429 sans durée annoncée ?**
   1. (recommandé) 30 s, puis 1 min, puis 5 min (règle d'OpenClaw).
   2. 1 h (règle d'Hermes).
   3. Aucune pause.
4. **Deux prompts en même temps dans une même session ?**
   1. (recommandé) Le second attend la fin du premier, 30 s au plus, puis 409.
   2. 409 tout de suite.
   3. Laisser comme aujourd'hui (un tour peut être perdu).
5. **Pools de comptes d'abonnement CLI (lot 3) ?**
   1. (recommandé) Étude séparée après les lots 1 et 2.
   2. Dans ce chantier.
   3. Abandon.
6. **Versions ?**
   1. (recommandé) Lot 1 en 4.9.0, lot 2 en 4.10.0.
   2. Les deux lots dans la même version.

## Sources

- OpenClaw, *Model failover* :
  https://docs.openclaw.ai/concepts/model-failover
- OpenClaw, *Command queue* : https://docs.openclaw.ai/concepts/queue
- OpenClaw, source de la file (`drainLane`, `enqueueCommandInLane`,
  `setCommandLaneConcurrency`) :
  https://github.com/openclaw/openclaw/blob/main/src/process/command-queue.ts
- Hermes Agent, *Credential Pools* :
  https://hermes-agent.nousresearch.com/docs/user-guide/features/credential-pools
- Hermes Agent, *Fallback Providers* :
  https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers
- Hermes Agent, *Gateway Internals* :
  https://hermes-agent.nousresearch.com/docs/developer-guide/gateway-internals
- Hermes Agent, source du pool (`STRATEGY_*`, `EXHAUSTED_TTL_*`,
  `DEFAULT_MAX_CONCURRENT_PER_CREDENTIAL`, `acquire_lease`) :
  https://github.com/NousResearch/hermes-agent/blob/main/agent/credential_pool.py
- Hermes Agent, file des messages en session occupée :
  https://github.com/NousResearch/hermes-agent/issues/14762
- Plugboard : `plugboard/router.py`, `circuit_breaker.py`, `app.py`,
  `cli_subprocess.py`, `session_store.py`, `metrics.py`, `ratelimit.py`,
  `legacy_connectors.py:14-15` (ancien connecteur OpenClaw retiré) et
  `cli_subprocess.py:1-21`, `947-955` (reprise du modèle `cli-backend`
  d'OpenClaw).

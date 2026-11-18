# ADR-039 : outils (function calling) émulés sur les routes CLI d'abonnement

## Statut

Proposé (19/11/2026). Décision de principe D-77 (YB via Hermes) ; cette note en fixe la conception.

## 1. Contexte et décision

- Aujourd'hui, une requête dont seules des routes CLI (`auth_source` `subprocess:*`) servent le modèle et qui porte `tools` ou un `tool_choice` autre que `none`/`auto` reçoit 400 `unsupported_on_cli` (`plugboard/cli_limits.py:53-75`, `router.cli_refusal`, `chat_path/completions.py:325-327`).
- D-77 : la passerelle accepte `tools` / `tool_choice` sur une route CLI **si l'option d'instance est activée ET si la clé porte le droit**. Elle traduit les outils en consigne texte (protocole JSON), puis analyse la réponse en `tool_calls` OpenAI.
- **Écart assumé à l'ADR-038** (étude P0, « Arbitrage » et §c : « routes au jeton obligatoires pour les agents à outils ; le protocole texte reste aux maquettes »). L'écart est borné :
  - désactivé par défaut, réservé aux clés d'agents internes désignées par l'admin ;
  - seuls `tools`, `tool_choice` et `parallel_tool_calls` changent ; `functions`, `function_call`, `response_format` structuré et les parties non texte restent refusés ;
  - liste de routes mixte (CLI + API/Ollama) : **inchangée**, les routes CLI sont écartées et la route native sert les outils ;
  - la passerelle n'exécute aucun outil ; aucune route facturée au jeton n'est ouverte (D-74 reste à YB).

## 2. Activation

- Variable `PLUGBOARD_CLI_TOOLS_EMULATION` : vide (défaut) = coupé, `1` = permis. Lue par `envcompat.get` à chaque requête, comme `PLUGBOARD_SEMANTIC_CACHE`.
- Droit par clé : booléen `allow_cli_tools` du record (`plugboard/apikeys.py`), `false` par défaut, posé par l'admin à la création (`POST /admin/keys`, `KeyCreate`) et recopié par `rotate_key`. Pas de route de modification en v1 : on émet une nouvelle clé.
- La clé admin n'a pas de record : jamais éligible.
- Les **deux** conditions sont nécessaires. Sinon, le refus actuel est rendu à l'identique : 400, même `code`, même `unsupported`, même `detail`.
- Avec les deux, `unsupported` ne liste plus que ce qui reste refusé (par ex. `content:image_url`) ; liste vide = requête servie.

## 3. Protocole

Nouveau module pur `plugboard/cli_tools.py` (sans import du routeur ni de l'app), appelé par le routeur juste avant `cli_subprocess.complete` : il rend un corps **texte seul** (rôles `system`/`user`/`assistant`), donc `messages_to_prompt` ne change pas.

- **Consigne** : un message `system` ajouté en tête. Il liste chaque outil (`name`, `description`, `parameters` en JSON compact) et exige une réponse qui soit **un seul objet JSON, sans autre texte** :
  - `{"tool_calls": [{"name": "<outil>", "arguments": {…}}]}` pour appeler ;
  - `{"final": "<texte>"}` pour répondre.
- **`tool_choice`** :
  - `auto` ou absent : l'un ou l'autre ;
  - `required` : `tool_calls` exigé ;
  - outil nommé : seul cet outil est présenté, et l'appel est exigé (nom absent de `tools` : 400 `invalid_request`) ;
  - `none` : ni outils ni protocole dans la consigne ; la réponse texte est rendue telle quelle.
- **Historique** :
  - un `assistant.tool_calls` devient un tour `assistant` contenant l'objet `{"tool_calls": …}` du protocole, ce qui redonne au modèle son propre format ;
  - un message `role: tool` devient un tour `user` : `TOOL_RESULT ` suivi de `{"id", "name", "content"}` encodé par `json.dumps`. L'échappement empêche une sortie d'outil de forger un tour `[System]` ou `[User]`.
- **`parallel_tool_calls`** : `true` (défaut OpenAI) = plusieurs appels permis ; `false` = la consigne dit « un seul appel » et la réponse en porte au plus un.

## 4. Analyse de la réponse

- Texte accepté : l'objet JSON seul, après retrait des espaces et d'**une** clôture Markdown ` ```json ` facultative. Pas d'heuristique « du premier `{` au dernier `}` » (contrairement à la maquette P0-4) : un échec doit rester visible.
- Validation des arguments contre le `parameters` de l'outil, sous-ensemble maison et sans dépendance nouvelle : `type`, `properties`, `required`, `enum`, `items`, `additionalProperties: false`, sur une profondeur de 8 au plus. Les autres mots-clés sont ignorés, pas appliqués.
- Succès : `message.content: null`, `tool_calls[i]` = `{"id": "call_" + 24 hex (uuid4), "type": "function", "function": {"name", "arguments": <chaîne JSON>}}`, `finish_reason: "tool_calls"`. Avec `{"final"}` : `content` = texte, `finish_reason: "stop"`.
- **Échec d'analyse** : **erreur explicite 502, `code` `cli_tool_call_invalid`**, `detail` écrit par la passerelle (raison, nom de l'outil), `error_ref`. Pas de relance ; rien n'est facturé, comme pour tout 502.
  - Cas : texte non JSON, clé inconnue, outil non déclaré, arguments invalides, appel manquant sous `required` ou outil nommé, plusieurs appels avec `parallel_tool_calls: false`.
  - Pourquoi pas une relance : elle consommerait du quota d'abonnement en silence et masquerait le taux d'échec que le critère de retrait (§10) doit mesurer. L'agent appelant a ses budgets et peut relancer lui-même.
  - Pourquoi pas la réponse en texte : c'est la déviation silencieuse relevée par P0-4 (« un modèle qui dévie produit une réponse finale, sans appel d'outil »).
- **Streaming : bufferisé**. Une requête `stream: true` émulée prend le chemin existant des CLI qui ne streament pas (`chat_path/completions.py:471-500`) : `forward()` sans streaming, puis `chat_billing.replay_as_sse`, qui rejoue déjà `tool_calls` et `finish_reason` (`tests/test_cli_stream_fallback_tool_calls.py`). Un flux en direct (claude, agy) enverrait le JSON brut au client avant l'analyse. Coût : pas de premier mot anticipé.

## 5. Sécurité

- Les arguments viennent du modèle : **non fiables**. La passerelle ne les exécute jamais ; elle les rend à l'appelant, qui exécute et contrôle. La validation de schéma est un garde-fou de forme, pas une autorisation.
- **Aucun outil n'est exécuté par la passerelle**, et la CLI n'en reçoit aucun : son argv ne change pas (claude garde `--tools ""` à `cli_subprocess.py:484`, codex et agy gardent leur cage).
- Injection par les sorties d'outil (messages `tool`) : elles sont encodées en JSON et la consigne les désigne comme des données. Cette barrière est **faible** ; la vraie reste chez l'appelant.
- Lien avec P0-2 :
  - M-13 : un appel vers un outil non déclaré est refusé (502), en seconde barrière derrière le refus par le worker, qu'il ne remplace pas ;
  - M-14 : rien ne change, permissions d'outils et `appels_max` restent au worker.
- Bornes, contrôlées avant tout appel (400 `invalid_request`, rien compté) : 32 outils au plus ; 32 Kio de `tools` sérialisés ; 64 Kio par message `tool`. La réponse du modèle est analysée sur 256 Kio au plus, au-delà : 502.
- Journaux : une ligne `cli tools: provider=… tools=N outcome=calls:K|final|invalid:<raison>`, sans noms d'outils, sans arguments, sans sortie d'outil, sans texte du modèle. Rien de plus qu'aujourd'hui en clair.

## 6. Coût et usage

- Jetons d'entrée estimés sur les messages **traduits** (consigne comprise). Aujourd'hui, `router.py:1446` estime sur `body["messages"]` : la tâche 2 passe au corps traduit.
- Jetons de sortie estimés sur le texte brut du modèle. L'usage déclaré par agy reste prioritaire, comme aujourd'hui.
- Coût = grille de la route, comme toute route CLI ; budget, `rpm_limit` et cache sont inchangés (`tools` est déjà dans la clé de cache, `cache.py:129`). Rien n'est facturé au jeton chez un fournisseur.

## 7. Contrat d'API

- Règle de `docs/api/README.md` (« Versioning ») : ajout = mineure. Ici, on ajoute un champ accepté sous condition (`tools`, `tool_choice`, `parallel_tool_calls`), un `code` `cli_tool_call_invalid` (502), et une liste `unsupported` plus courte pour une clé autorisée. Rien n'est retiré ni ne change de sens pour un appelant sans droit.
- **`CONTRACT_VERSION` 3.0.1 → 3.1.0** (`plugboard/api_contract.py:44`), puis `make contract` (`docs/api/openapi.json`, `docs/api/mcp.json`).
- Le champ `allow_cli_tools` relève de l'API admin, hors contrat.

## 8. Tests (TDD, faux fournisseur)

Aucune CLI réelle : `cli_subprocess.complete` est remplacé par un faux qui rend un texte choisi et enregistre le corps reçu.

1. Option coupée, clé avec droit : 400 identique à aujourd'hui, faux non appelé.
2. Option active, clé sans droit : même 400.
3. Option active, clé admin : même 400.
4. Appel simple : `tool_calls[0]`, `arguments` en chaîne JSON, id `call_…`, `finish_reason: tool_calls`.
5. Appels multiples ; puis avec `parallel_tool_calls: false`, 502.
6. `tool_choice` nommé : seul cet outil est dans la consigne ; une réponse `final` donne 502. `required` : même chose. `none` : texte rendu tel quel.
7. Réponse non JSON, et JSON avec texte autour : 502 `cli_tool_call_invalid`, non facturé.
8. Arguments invalides (type, `required`, `enum`, propriété en trop) et outil non déclaré : 502.
9. Historique avec `assistant.tool_calls` et `role: tool` : prompt attendu, sortie échappée (pas de `\n[System]` forgé).
10. Streaming : SSE avec `tool_calls` et `finish_reason`, faux appelé sans `on_delta`.
11. Jetons : `prompt_tokens` inclut la consigne.
12. Bornes : 33 outils, `tools` trop gros, message `tool` trop gros : 400, faux non appelé.
13. Liste mixte CLI + HTTP : la route HTTP sert, comme aujourd'hui.
14. Journal : ni argument ni sortie d'outil dans `caplog`.

Fichiers : `tests/test_cli_tools.py` (module pur : 4-9, 12), `tests/test_cli_tools_route.py` (1-3, 5, 7, 10-11, 13-14), `tests/test_cli_unsupported_400.py` (cas 1 ajouté), `tests/test_api_contract.py`.

## 9. Découpage

### Task 1: module pur de traduction et d'analyse
`plugboard/cli_tools.py` (consigne, historique, analyse, validation, bornes), `tests/test_cli_tools.py`.

### Task 2: droit par clé et branchement au routeur
- Fichiers : `plugboard/apikeys.py` (`allow_cli_tools` dans `_new_key`, `create_key`, `rotate_key`), `plugboard/admin_routes/keys.py` (`KeyCreate`).
- `plugboard/cli_limits.py` et `plugboard/router.py` : `cli_refusal` et `forward` reçoivent `cli_tools`, le corps est traduit avant `complete`, la réponse est analysée, et l'usage est estimé sur le corps traduit.
- `plugboard/chat_path/completions.py` (calcul du droit, contournement du flux) et `plugboard/chat_path/responses.py` (502 `cli_tool_call_invalid`).
- Tests : `tests/test_cli_tools_route.py`, `tests/test_cli_unsupported_400.py`.

### Task 3: contrat et documentation
`plugboard/api_contract.py` (descriptions, code, 3.1.0), `docs/api/openapi.json`, `docs/api/mcp.json` (`make contract`), `docs/api/README.md`, `docs/guide/configuration.md`, `docs/guide/providers-and-subscriptions.md`, `docs/adr/README.md` (index), `CHANGELOG.md`.

## 10. Hors périmètre et retrait

- Hors périmètre : `functions`/`function_call` hérités ; `response_format` structuré ; streaming en direct des appels ; relance automatique ; exécution d'outil par la passerelle ; route admin de modification d'une clé ; routes au jeton (D-74).
- **Critère de retrait** (on supprime l'émulation et l'on revient au 400) dès que l'un est vrai :
  - D-74 ouvre des routes au jeton et les agents internes y sont passés ;
  - le taux de `cli_tool_call_invalid` sur les appels émulés dépasse **5 % sur 200 appels** au moins (7 jours glissants, lu dans les journaux) ;
  - une faille d'injection passe par l'émulation alors qu'une route native l'aurait arrêtée.

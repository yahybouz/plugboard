# ADR-039 : outils (function calling) émulés sur les routes CLI d'abonnement

## Statut

Accepté (19/11/2026, D-79). Décision de principe D-77 ; cette note en fixe la conception.

## 1. Contexte et décision

- Aujourd'hui, une requête dont seules des routes CLI (`auth_source` `subprocess:*`) servent le modèle et qui porte `tools` ou un `tool_choice` autre que `none`/`auto` reçoit 400 `unsupported_on_cli` (`plugboard/cli_limits.py:53-75`, `router.cli_refusal`, `chat_path/completions.py:325-327`).
- D-77 : la passerelle accepte `tools` / `tool_choice` sur une route CLI **si l'option d'instance est activée ET si la clé porte le droit**. Elle traduit les outils en consigne texte (protocole JSON), puis analyse la réponse en `tool_calls` OpenAI.
- **Écart assumé à l'ADR-038** (étude P0, « Arbitrage » et §c : « routes au jeton obligatoires pour les agents à outils ; le protocole texte reste aux maquettes »). L'écart est borné :
  - désactivé par défaut, réservé aux clés d'agents internes désignées par l'admin ;
  - **désactivé en production** tant qu'une décision contraire n'est pas prise (P0-1 §c : « texte interdit en prod ») : UAT et agents internes seulement ;
  - seuls `tools`, `tool_choice` et `parallel_tool_calls` changent ; `functions`, `function_call`, `response_format` structuré et les parties non texte restent refusés ;
  - liste de routes mixte (CLI + API/Ollama) : **inchangée**, les routes CLI sont écartées et la route native sert les outils ;
  - la passerelle n'exécute aucun outil ; aucune route facturée au jeton n'est ouverte (D-74 reste ouverte).

## 2. Activation

- Variable `PLUGBOARD_CLI_TOOLS_EMULATION` : vide (défaut) = coupé, `1` = permis. Lue par `envcompat.get("CLI_TOOLS_EMULATION")` à chaque requête, donc avec le nom hérité `LLM_ENABLER_` (`PLUGBOARD_SEMANTIC_CACHE`, lui, est lu par `os.environ.get`, `semantic_cache.py:121`).
- Droit par clé : champ `allow_cli_tools` du record (`plugboard/apikeys.py`), posé par l'admin à la création (`POST /admin/keys`, `KeyCreate`) et recopié par `rotate_key`. Seul `allow_cli_tools is True` ouvre le droit ; pas de route de modification en v1, on émet une nouvelle clé.
- La clé admin n'a pas de record : jamais éligible.
- Les **deux** conditions sont nécessaires. Sinon, le refus actuel est rendu à l'identique : 400, même `code`, même `unsupported`, même `detail`.
- Le droit passe par un paramètre `cli_tools=False` par défaut (`cli_refusal`, `_candidates_for`, `forward`). Seul le chemin chat le pose : `forward_stream`, les sondes du catalogue (`model_availability.py`) et `admin_routes/model_probes.py` refusent comme aujourd'hui.
- Avec les deux conditions, `unsupported` ne liste plus que ce qui reste refusé (par ex. `content:image_url`) ; liste vide = requête servie.
- `session_id` avec outils émulés : 400 `invalid_request` en v1 (`_record_session_turn` ne sait pas rejouer des `tool_calls`).

## 3. Protocole

Nouveau module pur `plugboard/cli_tools.py` (sans import du routeur ni de l'app), appelé par le routeur juste avant `cli_subprocess.complete` : il rend un corps **texte seul** (rôles `system`/`user`/`assistant`), donc `messages_to_prompt` ne change pas.

- La traduction ne s'applique **que si l'émulation est engagée** (`tools` non vide, option et droit présents). Un historique avec `role: tool` mais sans `tools` suit le chemin actuel.
- **Consigne** : un message `system` ajouté en tête. Elle donne la liste des outils sous forme d'un tableau JSON (`json.dumps` de `name`, `description`, `parameters`), jamais en texte libre. Elle exige une réponse qui soit **un seul objet JSON, sans autre texte** :
  - `{"tool_calls": [{"name": "<outil>", "arguments": {…}}]}` pour appeler ;
  - `{"final": "<texte>"}` pour répondre.
- **`tool_choice`** :
  - `auto` ou absent : l'un ou l'autre ;
  - `required` : `tool_calls` exigé ;
  - outil nommé : seul cet outil est présenté, et l'appel est exigé (nom absent de `tools` : 400 `invalid_request`) ;
  - `none` : ni outils ni protocole dans la consigne ; la réponse texte est rendue telle quelle.
- **Historique** :
  - un `assistant.tool_calls` devient un tour `assistant` contenant l'objet `{"tool_calls": …}` du protocole, encodé par `json.dumps` ; le texte que ce tour portait aussi est gardé, en tour `assistant` juste avant ;
  - un message `role: tool` devient un tour `user` : `TOOL_RESULT ` suivi de `{"id", "name", "content"}` encodé par `json.dumps`. Le `name` est retrouvé par `tool_call_id` dans le dernier `assistant.tool_calls`, sans tour `user` entre les deux ; un id inconnu donne 400 `invalid_request`. L'échappement empêche une sortie d'outil de forger un tour `[System]` ou `[User]`.
- **`parallel_tool_calls`** : `true` (défaut OpenAI) = plusieurs appels permis ; `false` = la consigne dit « un seul appel » et la réponse en porte au plus un.

## 4. Analyse de la réponse

- Texte accepté : l'objet JSON seul, après retrait des espaces et d'**une** clôture Markdown ` ```json ` facultative. Pas d'heuristique « du premier `{` au dernier `}` » (contrairement à la maquette P0-4) : un échec doit rester visible.
- Validation des arguments contre le `parameters` de l'outil, sous-ensemble maison et sans dépendance nouvelle : `type`, `properties`, `required`, `enum`, `items`, `additionalProperties: false`, sur une profondeur de 8 au plus. Les autres mots-clés sont ignorés, pas appliqués.
- Succès : `message.content: null`, `tool_calls[i]` = `{"id": "call_" + 24 hex (uuid4), "type": "function", "function": {"name", "arguments": <chaîne JSON>}}`, `finish_reason: "tool_calls"`. Avec `{"final"}` : `content` = texte, `finish_reason: "stop"`.
- **Échec d'analyse** : exception dédiée `CliToolCallError` (dans `cli_tools.py`, hors de `CliSubprocessError`).
  - Cas : texte non JSON, clé inconnue, outil non déclaré, arguments invalides, appel manquant sous `required` ou outil nommé, plusieurs appels avec `parallel_tool_calls: false`.
  - Dans `forward`, elle est relancée **comme `ModelNotServedError`** (`router.py:1465-1468`), avant le `except Exception` générique. Donc : pas de `cb_failure`, pas d'alerte `provider_down`, pas de métrique d'erreur, pas de quarantaine, et pas de `continue` vers la route CLI suivante.
  - Réponse : **502, `code` `cli_tool_call_invalid`**, `detail` écrit par la passerelle (raison, nom de l'outil), `error_ref`, en-tête **`x-should-retry: false`**. `_chat_error_response` la reconnaît avant le test « timeout », à côté de `ModelNotServedError` (`chat_path/responses.py:413-416`). Rien n'est facturé.
  - Les SDK OpenAI relancent les 5xx : les agents internes règlent `max_retries=0`, et l'en-tête couvre les SDK qui le lisent.
  - Pourquoi pas une relance : elle consommerait du quota d'abonnement en silence et masquerait le taux d'échec que le critère de retrait (§10) doit mesurer. L'agent appelant a ses budgets et peut relancer lui-même.
  - Pourquoi pas la réponse en texte : c'est la déviation silencieuse relevée par P0-4 (« un modèle qui dévie produit une réponse finale, sans appel d'outil »).
- **Streaming : bufferisé**. Une requête `stream: true` émulée prend toujours le chemin des CLI qui ne streament pas (`chat_path/completions.py:471-500`), y compris pour claude et agy : l'émulation force ce contournement, elles ne passent donc pas par `forward_stream`. Le chemin appelle `forward()` sans streaming, puis `chat_billing.replay_as_sse`, qui rejoue déjà `tool_calls` et `finish_reason` (`tests/test_cli_stream_fallback_tool_calls.py`). Coût : pas de premier mot anticipé.

## 5. Sécurité

- Les arguments viennent du modèle : **non fiables**. La passerelle ne les exécute jamais ; elle les rend à l'appelant, qui exécute et contrôle. La validation de schéma est un garde-fou de forme, pas une autorisation.
- **Aucun outil n'est exécuté par la passerelle**, et la CLI n'en reçoit aucun : son argv ne change pas (claude garde `--tools ""` à `cli_subprocess.py:484`, codex et agy gardent leur cage).
- La consigne « système » **n'a pas le rang d'un prompt système**. `messages_to_prompt` aplatit tout en `[System]`/`[User]`, dans le prompt utilisateur, sous le prompt système propre à la CLI. Le modèle peut donc la suivre moins bien qu'un vrai `system`.
- Injection par les sorties d'outil (messages `tool`) : elles sont encodées en JSON et la consigne les désigne comme des données. Cette barrière est **faible** ; la vraie reste chez l'appelant.
- Les messages `user` ne sont **pas** échappés : ils peuvent forger un `[System]`, comme aujourd'hui sur toute route CLI. Seule une ligne qui commence par `TOOL_RESULT` y reçoit la marque `[user text]` en tête, que la consigne explique, pour qu'un texte de l'utilisateur ne passe pas pour une sortie d'outil.
- Un message `user` peut aussi écrire le format de réponse ou pousser le modèle à appeler un outil : le contrôle de la passerelle porte sur l'outil déclaré et son schéma, jamais sur l'intention de l'appel, qui reste à juger par l'appelant.
- Les **descriptions d'outils** de l'appelant entrent dans la consigne : un outil empoisonné (description qui donne des ordres) est un vecteur d'injection. La sérialisation JSON limite la forge de tours, pas l'effet des ordres. La liste des outils est à la charge de l'appelant.
- Lien avec P0-2 :
  - M-13 : un appel vers un outil non déclaré est refusé (502), en seconde barrière derrière le refus par le worker, qu'il ne remplace pas ;
  - M-14 : rien ne change, permissions d'outils et `appels_max` restent au worker.
- Bornes, contrôlées avant tout appel (400 `invalid_request`, rien compté) :
  - 32 outils au plus, et 32 Kio de `tools` sérialisés ;
  - 64 Kio par message `tool`, 256 Kio pour l'ensemble des messages `tool` ;
  - réponse du modèle analysée sur 256 Kio au plus ; au-delà, 502.
- Journaux : une ligne `cli tools: provider=… tools=N outcome=calls:K|final|invalid:<raison>`. Elle ne porte ni nom d'outil, ni argument, ni sortie d'outil, ni texte du modèle. Le message de `CliToolCallError` ne porte que la raison ; le nom d'outil ne va que dans le `detail` rendu au client. Rien de plus qu'aujourd'hui en clair.

## 6. Coût et usage

- Jetons d'entrée estimés sur les messages **traduits** (consigne comprise), à deux endroits :
  - la retenue de budget `_hold_budget` (`chat_path/limits.py:249`), qui estime sur `cli_tools.to_text_body(body)` ; la traduction est pure, donc identique à celle du routeur ;
  - l'usage après l'appel : aujourd'hui, `router.py:1446` estime sur `body["messages"]`.
- Jetons de sortie estimés sur le texte brut du modèle. L'usage déclaré par agy reste prioritaire, comme aujourd'hui.
- Coût = grille de la route, comme toute route CLI ; budget, `rpm_limit` et cache sont inchangés (`tools` est déjà dans la clé de cache, `cache.py:129`). Rien n'est facturé au jeton chez un fournisseur.

## 7. Contrat d'API

- Règle de `docs/api/README.md` (« Versioning ») : un ajout donne une version mineure. Ici, on ajoute :
  - le champ optionnel `parallel_tool_calls`, absent de `api_contract.py` ;
  - `tools` et `tool_choice` acceptés sous condition ;
  - le `code` `cli_tool_call_invalid` (502) et l'en-tête `x-should-retry` ;
  - une liste `unsupported` plus courte pour une clé autorisée.
- Rien n'est retiré ni ne change de sens pour un appelant sans droit.
- **`CONTRACT_VERSION` 3.0.1 → 3.1.0** (`plugboard/api_contract.py:44`), puis `make contract` (`docs/api/openapi.json`, `docs/api/mcp.json`).
- Le champ `allow_cli_tools` relève de l'API admin, hors contrat.

## 8. Tests (TDD, faux fournisseur)

Aucune CLI réelle : `cli_subprocess.complete` est remplacé par un faux qui rend un texte choisi et enregistre le corps reçu.

1. Option coupée, clé avec droit : 400 identique à aujourd'hui, faux non appelé.
2. Option active : clé sans droit, clé admin, ou `allow_cli_tools` non booléen (`"true"`, `1`) : même 400.
3. `rotate_key` recopie `allow_cli_tools`.
4. Appel simple : `tool_calls[0]`, `arguments` en chaîne JSON, id `call_…`, `finish_reason: tool_calls`.
5. Appels multiples ; puis avec `parallel_tool_calls: false`, 502.
6. `tool_choice` nommé : seul cet outil est dans la consigne ; une réponse `final` donne 502. `required` : même chose. `none` : texte rendu tel quel.
7. Réponse non JSON, et JSON avec texte autour : 502 `cli_tool_call_invalid`, `x-should-retry: false`, non facturé.
8. Arguments invalides (type, `required`, `enum`, propriété en trop) et outil non déclaré : 502.
9. Deux routes CLI servent le modèle et la première répond mal : 502, la seconde n'est pas appelée, ni `cb_failure` ni alerte.
10. Historique avec `assistant.tool_calls` et `role: tool` : prompt attendu, `name` retrouvé par `tool_call_id`, sortie échappée (pas de `\n[System]` forgé). Un id inconnu donne 400.
11. Streaming, faux appelé sans `on_delta` : SSE avec `tool_calls` et `finish_reason`, pour un faux claude et un faux agy, qui ne passent pas par `forward_stream`.
12. Sans le drapeau `cli_tools`, `forward_stream`, les sondes du catalogue et `model_probes` refusent toujours.
13. Une réponse est mise en cache, puis l'option est coupée : 400, sans HIT de cache.
14. Jetons : `prompt_tokens` et la retenue de budget incluent la consigne.
15. Bornes : 33 outils, `tools` trop gros, message `tool` trop gros, total `tool` trop gros : 400, faux non appelé. `session_id` avec outils : 400.
16. Liste mixte CLI + HTTP : la route HTTP sert, comme aujourd'hui.
17. Journal : ni nom d'outil, ni argument, ni sortie d'outil dans `caplog`.

Fichiers de test :
- `tests/test_cli_tools.py` (module pur) : 4-8, 10, 15 ;
- `tests/test_cli_tools_keys.py` : 2-3 ;
- `tests/test_cli_tools_route.py` : 1, 9, 11-14, 16-17 ;
- `tests/test_cli_unsupported_400.py` : cas 1 ajouté ;
- `tests/test_api_contract.py`.

## 9. Découpage

### Task 1: module pur de traduction et d'analyse
`plugboard/cli_tools.py` (consigne, historique, analyse, validation, bornes, `CliToolCallError`), `tests/test_cli_tools.py`.

### Task 2a: droit de clé et rotation
`plugboard/apikeys.py` (`allow_cli_tools` dans `_new_key`, `create_key`, `rotate_key`), `plugboard/admin_routes/keys.py` (`KeyCreate`), `tests/test_cli_tools_keys.py`.

### Task 2b: routeur, chemin chat, réponses
- `plugboard/cli_limits.py` et `plugboard/router.py` : paramètre `cli_tools`, traduction avant `complete`, analyse, `CliToolCallError` relancée sans disjoncteur ni repli, usage sur le corps traduit.
- `plugboard/chat_path/completions.py` (droit, contournement du flux, refus de `session_id`), `plugboard/chat_path/limits.py` (`_hold_budget`), `plugboard/chat_path/responses.py` (502 et en-tête).
- Tests : `tests/test_cli_tools_route.py`, `tests/test_cli_unsupported_400.py`.

### Task 3: contrat et documentation
`plugboard/api_contract.py` (`parallel_tool_calls`, descriptions, code, en-tête, 3.1.0), `docs/api/openapi.json`, `docs/api/mcp.json` (`make contract`), `docs/api/README.md`, `docs/guide/configuration.md`, `docs/guide/providers-and-subscriptions.md`, `docs/adr/README.md` (index), `CHANGELOG.md`.

## 10. Hors périmètre et retrait

- Hors périmètre :
  - `functions`/`function_call` hérités, et `response_format` structuré ;
  - streaming en direct des appels, relance automatique, `session_id` ;
  - exécution d'outil par la passerelle ;
  - route admin de modification d'une clé ;
  - activation en production, et routes au jeton (D-74).
- **Critère de retrait** (on supprime l'émulation et l'on revient au 400) dès que l'un est vrai :
  - D-74 ouvre des routes au jeton et les agents internes y sont passés ;
  - le taux de `cli_tool_call_invalid` sur les appels émulés dépasse **5 % sur 200 appels** au moins (7 jours glissants, lu dans les journaux) ;
  - une faille d'injection passe par l'émulation alors qu'une route native l'aurait arrêtée.
- **Limites connues** :
  - une réponse hors protocole (502 `cli_tool_call_invalid`) compte comme un succès dans la métrique de latence de la route (`metrics.record`, sans erreur), puisque la CLI a bien répondu ; le taux d'échec du critère de retrait se lit donc dans les journaux (`outcome=invalid:…`), pas dans les métriques de la route.

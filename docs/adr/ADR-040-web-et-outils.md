# ADR-040 : accès web et outils dans une même requête

## Statut

Accepté (19/11/2026), release 4.15.0, qui réunit l'accès web par clé (PLU-158) et les outils émulés sur les routes CLI (ADR-039, PLU-157).

## Contexte

- Une requête web (`X-Plugboard-Web: 1` ou `"plugboard": {"web": true}`) n'est servie que par une route CLI claude. Le CLI reçoit alors `--tools WebSearch,WebFetch` et `--allowedTools WebSearch,WebFetch`, rien d'autre (`plugboard/web_access.py`).
- L'ADR-039 traduit les `tools` du client en consigne texte et relit la réponse en `tool_calls`. C'est le client qui exécute ses outils.
- Les deux mécanismes ont été construits séparément. Réunis, une requête pourrait porter à la fois `tools` et la demande web. Le CLI recevrait alors deux protocoles d'outils dans le même tour, et le comportement ne serait ni défini ni testé.

## Décision

- Une requête qui demande le web **et** déclare des `tools` (liste non vide) est refusée : **400 `web_with_tools_unsupported`**, et rien n'est appelé. Le refus vaut quels que soient l'option `PLUGBOARD_CLI_TOOLS_EMULATION` et le droit `allow_cli_tools` de la clé.
- Ordre des refus de la porte web (`plugboard/web_gate.py`) : 400 `web_bad_request`, puis 403 `web_not_allowed`, puis 400 `web_with_tools_unsupported`, puis 400 `web_unavailable`, puis 429 `web_busy`. Une clé sans droit web reçoit donc toujours son 403, même si elle déclare des outils.
- La porte web s'exécute avant le refus `unsupported_on_cli`. Le refus d'une requête web avec outils ne dépend donc pas de l'émulation.
- Pas de slot web pris, rien de compté. Le refus est compté dans `GET /admin/web` (`refused_with_tools`).
- Contrat client 3.2.0 : le code est listé dans la réponse 400 de `/v1/chat/completions` et de `/v2/chat/completions`.

## Conséquences

- Un agent qui veut le web et ses propres outils fait deux appels : l'un avec le web, sans outils ; l'autre avec ses outils, sans le web.
- Lever cette limite demanderait de définir comment les `tool_calls` émulés et les outils web du CLI se partagent les tours et le budget `web_max_uses`. Ce serait une nouvelle décision, avec son propre ADR.

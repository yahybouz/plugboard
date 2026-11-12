# Interrupteur Ollama (PLU-12) — conception

**Statut : VALIDÉE par YB le 2026-11-12 (D-38 : recommandations 1 de Q1 à Q8 ; corrige aussi BUG-44).** Base : `dde9e53` (`sprint/recette-uat`, 4.9.0 + PLU-13).
Plan : `docs/superpowers/plans/2026-11-12-ollama-switch.md` (écrit à partir des réponses recommandées).

## Demande

Rendre Ollama disponible ou non pour toute l'API, le MCP et le Playground, sans redéploiement.
Aujourd'hui : `OLLAMA_ENABLED` au démarrage, et la case « Active » du fournisseur `ollama`
(page Fournisseurs → Modifier, `PUT /providers/ollama`), qui écrit `is_active` dans `data/providers.json`.

## Constat au code : ce que fait déjà `is_active=false`

| Surface | État | Preuve |
|---|---|---|
| `/v1/models`, `/v2/models` (donc MCP `list_models`, liste du Playground) | Couvert | `router.py:1810-1812` ; test `test_list_models_inactive_provider_excluded` vert |
| Chat `/v1` et `/v2`, flux ou non (donc MCP `chat`, Playground) | **Trou grave** | `router.py:421-444`, `:645-653`, `:469` : le modèle d'un fournisseur inactif part vers **tous les autres fournisseurs actifs**. Prouvé par script : `ollama:qwen3:8b`, `qwen3:8b`, `llama3` → `acme-direct` puis `beta-cloud`. Le prompt destiné au local sort chez un tiers. `is_unknown_model` (`router.py:533-534`) le tient pour « connu » : pas de 404 |
| Réponse en cache | Trou | `app.py:2102` : le cache est lu après l'admission ; une réponse Ollama en cache serait servie |
| `/v1/embeddings` (MCP `embed`) | Couvert | `embeddings_api.py:168-169` : 503 « Embedding provider unavailable », aucun appel ; `/v1/embeddings/models` dit `available: false` ; test `ollama-off` vert |
| Préflight `/v1/preflight` | **Trou** | `app.py:2797-2812` : aucun refus, le ping part vers les autres fournisseurs (et il est facturé) |
| MCP | Hérite de l'API | `mcp_server.py:401, 481, 604` : tout passe par l'API HTTP ; aucun code MCP à changer |
| Page Ollama | Trou (usage) | Aucun interrupteur ; `app.py:4753-4766` affiche seulement `is_active`. La case « Active » existe, mais sur la page Fournisseurs ; son audit `provider.update` ne garde que le nom du champ, pas la valeur (`app.py:3840-3845`) |
| Watchdog, sondes, tarifs, `/health/detailed` | Couvert | `provider_watchdog.py:464`, `model_availability.py:490, 814, 1071`, `llm_pricing.py:337`, `app.py:2952` |
| Démarrage | Petit trou | `ollama.py:323-347` : sonde Ollama et resynchronise sa liste même si le fournisseur est coupé |
| Cache sémantique (option) | Petit trou | `semantic_cache.py:674` : appelle le fournisseur d'embeddings sans regarder `is_active` |
| File d'attente Ollama (1 requête à la fois) | Trou | `provider_admission.py:140-173` : une requête en attente obtient son créneau après la coupure et appelle Ollama |
| `OLLAMA_ENABLED` | Ambigu | `ollama.py:29, 323` : ne joue qu'au démarrage ; `false` empêche l'enregistrement mais **n'éteint pas** un fournisseur déjà enregistré |

Conclusion : la coupure existe et persiste déjà ; le ticket se réduit à **combler 5 trous**
(chat, préflight, file d'attente, démarrage, cache sémantique) et à **rendre l'interrupteur visible
et audité** sur la page Ollama.

## Approches

1. **Recommandée — l'interrupteur, c'est `is_active` du fournisseur `ollama`.** Une seule vérité,
   déjà respectée par les listes, les embeddings et les sondes, écrite par `update_provider`
   (`atomic_write_json`), qui survit aux redémarrages. La case « Active » de la page Fournisseurs
   reste synchronisée. On comble les trous dans le routeur (un contrôle, toutes surfaces).
2. Réglage séparé dans `data/ollama_settings.json` : survit à la suppression du fournisseur,
   mais deux vérités à croiser sur chaque surface ; on rouvre ce que `is_active` couvre déjà.
3. Supprimer puis recréer le fournisseur : perd `max_concurrency`, prix et indicateurs. Rejetée.

## Questions pour YB

1. **Où est la bascule ?** (1, recommandé) Un interrupteur « Available to the API, MCP and
   Playground » en tête de la page Ollama, avec `GET`/`PUT /admin/ollama/switch`, qui écrit
   `is_active` du fournisseur `ollama` ; raison : une seule vérité, rien à migrer.
   (2) Fichier séparé `ollama_settings.json`. (3) Seulement la case existante de la page Fournisseurs.
2. **Réponse à un appel `ollama:…` quand c'est coupé ?** (1, recommandé) **503**,
   `code: provider_disabled`, `detail` « Provider 'ollama' is turned off by the administrator;
   nothing was called. », `hint` « Pick another model from GET /v1/models… », rien appelé, pas de
   cache ; raison : c'est temporaire et côté serveur, comme `providers_busy` et les embeddings
   (déjà 503). (2) 404 `model_not_found` : fait croire à une faute de frappe. (3) 403 : c'est le
   code des droits d'une clé. Une clé qui n'a pas droit au modèle garde son 403 en premier.
3. **Portée du refus ?** (1, recommandé) Tout fournisseur désactivé : le même trou existe pour
   chacun, un seul contrôle le ferme. (2) Ollama seulement.
4. **Embeddings ?** (1, recommandé) Coupés avec le chat, déjà le cas (503, aucun appel), message
   inchangé ; le cache sémantique cesse aussi d'appeler un fournisseur coupé (repli MinHash).
   (2) Un second interrupteur pour les embeddings.
5. **Requêtes déjà parties ?** (1, recommandé) Celles qui tournent sur Ollama vont au bout (pas de
   flux coupé net) ; celles qui attendent leur créneau reçoivent le 503 à leur tour, sans appel ;
   l'interrupteur affiche le nombre en cours et en attente. (2) Tout ce qui est admis va au bout,
   file comprise. (3) Annuler aussi celles en cours.
6. **Audit ?** (1, recommandé) Une ligne `ollama.switch` par `PUT` : ancien et nouvel état,
   appels en cours et en attente ; et `provider.update` note aussi la valeur de `is_active`.
   (2) Seulement la nouvelle route.
7. **Page Ollama et tâches de fond quand c'est coupé ?** (1, recommandé) La page reste utilisable
   (état, liste, téléchargement, suppression : gestes admin explicites) avec un bandeau « coupé » ;
   plus aucune tâche de fond n'appelle Ollama (le démarrage saute sa sonde) ; au rallumage, une
   seule resynchronisation de la liste des modèles. (2) Page figée, plus aucun appel au démon.
8. **`OLLAMA_ENABLED` ?** (1, recommandé) Garder son rôle (enregistrement au démarrage) et le
   documenter ; l'interrupteur fait foi à chaud. (2) `false` force et verrouille l'interrupteur.

## Choix pris seul (réversibles)

- L'interrupteur vise le fournisseur `ollama` (celui de `OLLAMA_URL`) ; un autre fournisseur
  Ollama ajouté à la main garde sa case « Active ». Les modèles Ollama Cloud (`:cloud`) passent par
  le même démon : ils sont coupés aussi.
- Contrat d'API 1.5.0 → 1.6.0 (ajout de `provider_disabled`). Pas d'outil MCP pour basculer
  (relève de PB-MCP-02). Aucun changement dans `mcp_server.py` (PB-MCP-01 y travaille).

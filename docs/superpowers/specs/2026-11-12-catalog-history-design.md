# Historique du catalogue par fournisseur (PB-PROV-02) — conception

**Statut : à valider par YB.** Base `dde9e53` (`sprint/recette-uat`, 4.9.0 + PLU-13).
Fiche : `TODOS.md` « Catalog changelog view » (P3, taille M).
Plan : `docs/superpowers/plans/2026-11-12-catalog-history.md`, écrit sur les réponses recommandées.

## Intention

Un opérateur veut savoir, pour un fournisseur, quels modèles sont apparus ou ont disparu,
et quand. Aujourd'hui, Monitoring montre les 20 derniers changements, tous types mêlés,
sans filtre ni suite.

## Existant (vérifié au code)

- **Journal** : `data/catalog/changes.jsonl`, une ligne JSON par changement, datée `at`
  (UTC, à la seconde) — `plugboard/change_events.py:89` et `:130`.
- **Taille** : les 1 000 dernières lignes, tous types confondus (`JOURNAL_MAX_LINES`,
  `change_events.py:24`, coupe `:57`). **Aucune durée** : la conservation dépend du volume.
  Chaque ajout relit et réécrit tout le fichier sous verrou (`:41-57`).
- **Trois types** : `models.changed` `{provider, added[], removed[]}` (rafraîchissement
  auto `model_availability.py:1138-1142`, bouton Discover `app.py:3988-3994`) ;
  `pricing.changed` (`pricing_scraper.py:1062-1093`, `provider` seulement pour un prix
  de fournisseur) ; `price.estimated` (`billing.py:816-821`, `space` au lieu de `provider`).
- **Non journalisés** : le premier remplissage d'un fournisseur ; l'origine (auto ou bouton).
- **Date d'un retrait** : celle où Plugboard le confirme, après `PLUGBOARD_MODEL_REMOVAL_PASSES`
  (2) passes sans le modèle (`model_diff.py:182-188`), pas celle du premier oubli du
  fournisseur. Un retrait par le bouton Discover est daté tout de suite (`:178-181`).
- Le webhook `catalog.changed` cité par la fiche (`TODOS.md:155`) n'existe pas : c'est
  `models.changed`.
- **Lecture** : `read_journal(limit)` (`change_events.py:60-70`) →
  `GET /admin/auto-update/status?journal=20` (`app.py:5355-5372`, 200 au plus) → carte
  « Recent automatic changes » (`static/auto_update.js:155-188`, `static/index.html:479-480`).
- **Données** : identifiants de fournisseurs et de modèles, prix, dates. Aucune clé,
  aucun client, aucune adresse IP, aucun prompt. Les noms de modèles viennent des
  fournisseurs (texte tiers) : affichage en texte seulement (`h()`, `static/app.js:177`).
- **Volume réel : non mesuré** (aucun journal sur l'instance de dev, l'UAT est hors de
  portée). Rafraîchissement des listes toutes les 6 h par défaut (`intervals.py:16`).

## Approches

| | Approche | Pour | Contre |
|---|---|---|---|
| **A (reco)** | Lire le journal existant, route admin paginée, onglet dédié | une seule source, ni migration ni reprise, webhooks inchangés | le fichier est relu à chaque page (quelques Mo au plus, hors boucle) |
| B | Base SQLite dédiée écrite à côté du journal | requêtes indexées, longue durée | 2e stockage, reprise de l'existant, double écriture pour quelques milliers de lignes |
| C | Navigateur seul : `journal=200` et filtre local | aucun code serveur | ni pagination ni filtre serveur, 200 lignes au plus : ne répond pas au ticket |

## Conception retenue (approche A, réponses recommandées)

1. **Conservation** : 365 jours (`PLUGBOARD_CHANGE_JOURNAL_DAYS`, `0` = sans limite d'âge)
   et 10 000 lignes au plus, en filet. Une ligne sans date lisible n'est retirée que par
   le plafond. La purge a lieu à l'écriture d'un changement (mon choix : sans nouveau
   changement, une ligne plus vieille reste visible jusqu'au suivant). Monitoring et
   webhooks ne changent pas.
2. **Route** `GET /admin/catalog/history`, admin seulement (`Depends(_require_admin)`),
   gestionnaire synchrone (pool de threads, comme `suivi_api.py`).
   - Paramètres : `provider` (id exact), `since`, `until` (dates `AAAA-MM-JJ` UTC, bornes
     incluses), `model` (texte, sans casse), `limit` 1–200 (50), `offset` ≥ 0.
     Invalide, ou `since` après `until` → `422`.
   - Réponse : `{"entries": [{"at", "provider", "added", "removed"}], "total", "limit",
     "offset", "providers": [...], "retention": {"days", "max_lines", "oldest"}}`, plus
     récent d'abord. Seuls ces champs sortent du journal.
   - `providers` : ceux des lignes `models.changed`, y compris supprimés depuis. Un
     fournisseur inconnu donne une liste vide (`200`), pas `404`.
   - Avec `model` (Q6) : listes complètes et comptes intacts, noms correspondants placés
     en tête de `added` et `removed`. Une ligne sans date lisible est écartée dès que
     `since` ou `until` est donné.
3. **Page** : onglet **Catalog history** (section Operations, `#catalog-history`) et lien
   **Full history** sous « Recent automatic changes » de Monitoring. Code dans
   `static/catalog_history.js` (comme `live_requests.js`).
   - Coordination (décision de l'orchestrateur) : assemblé **avant** PB-DASH-02. Une
     seule entrée dans la barre latérale (`index.html`) et dans `TAB_LOADERS`
     (`app.js`) ; le moins de classes possible, sur les variables existantes, groupées
     en fin de `style.css` sous un commentaire « catalog history ». PB-DASH-02 les
     reprend à l'assemblage.
   - Filtres : Provider (All + `providers`), Period (7, 30, 90 jours, 1 an, All ; défaut
     All), Model (texte).
   - Une carte par changement : date UTC et fournisseur ; « +3 added » en vert,
     « −1 removed » en rouge ; 10 noms puis « +N more » dépliable ; noms longs coupés.
     Avec un filtre Model, les noms correspondants sont en tête et surlignés.
   - **Load more** (50 de plus) plutôt que des numéros de page. Liste vide : message
     clair. Pied : durée de conservation et date de la plus ancienne ligne.
   - **iPhone (390 px)** : filtres empilés pleine largeur, aucun défilement horizontal,
     cibles tactiles de 44 px. À 1280 px : filtres sur une ligne. Textes en anglais.
4. **Hors périmètre** : prix (Q3), origine du changement (Q5), premier remplissage,
   création ou suppression d'un fournisseur (déjà au journal d'audit), export CSV,
   contrat `docs/api/openapi.json` (routes admin hors contrat, `api_contract.py:13-15`).

## Tests et preuves

- `change_events` : filtres, ordre, pagination, total, lignes illisibles ignorées, purge
  par âge, plafond, ancien test du plafond toujours vert.
- Route : `401` sans clé et avec une clé client, `200` admin ; chaque filtre ;
  `limit`, `offset`, `total` ; `422` ; aucun champ hors liste.
- Tableau de bord : onglet et script servis, route ajoutée à `test_e2e_dashboard.py`.
- Navigateur réel (MCP playwright) : instance locale `127.0.0.1:18801`, données fictives,
  tâches de fond et Ollama coupés, captures à 390 et 1280 px, console sans erreur.

## Risques

- Fichier plus gros (10 000 lignes, environ 2 à 3 Mo, estimé) réécrit à chaque ajout,
  sur la boucle comme aujourd'hui ; ajouts rares (une fois par passe de 6 h, bouton
  Discover, première estimation d'un prix).
- Pagination par décalage : une ligne arrivée entre deux pages décale d'un cran (doublon
  possible en bas de liste). Acceptable pour un historique admin.

## Questions pour YB

1. **Conservation** — 1) **365 jours + plafond 10 000 lignes, réglable** (reco : un an
   répond à « depuis quand ? », taille bornée) ; 2) 90 jours ; 3) garder 1 000 lignes
   tous types (rien à changer, mais durée inconnue).
2. **Emplacement** — 1) **onglet Catalog history + lien depuis Monitoring** (reco :
   Monitoring est déjà long sur iPhone, l'onglet a une adresse directe) ; 2) section dans
   Monitoring ; 3) bouton History dans le détail d'un fournisseur.
3. **Contenu** — 1) **modèles ajoutés et retirés seulement** (reco : c'est la fiche ; les
   prix ont déjà Pricing › Review et la carte Monitoring) ; 2) modèles et prix, avec un
   filtre de type.
4. **Filtre par modèle** — 1) **oui, recherche dans les noms** (reco : répond à « quand X
   a-t-il disparu ? », coût faible) ; 2) non, fournisseur et période seulement.
5. **Origine** (rafraîchissement auto ou bouton Discover) — 1) **non pour l'instant**
   (reco : le journal ne la note pas, l'ajouter change le webhook `models.changed`, et le
   journal d'audit garde déjà `provider.discover`) ; 2) oui, à partir de maintenant.
6. **Le filtre Model raccourcit-il les listes ?** — 1) **non : listes complètes, noms
   correspondants en tête et surlignés, comptes gardés** (reco : « −40 removed » reste
   vrai, et le nom cherché ne se cache plus derrière « +30 more ») ; 2) oui : seulement
   les noms correspondants (toujours visibles, mais les comptes ne décrivent plus tout le
   changement).

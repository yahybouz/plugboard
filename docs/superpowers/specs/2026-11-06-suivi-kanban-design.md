# Page `/suivi` et Kanban des tickets Plugboard — conception

**Statut : PROPOSITION, NON APPROUVÉE.** Aucune implémentation tant que YB n'a pas
choisi une approche et approuvé ce document (porte Superpowers `brainstorming`,
étapes 1 à 3). Demande d'origine : registre #674 (6 novembre 2026), rattachée à
#673 ; constat #675/#678 : « Nouveau Kanban Plugboard à concevoir et faire
approuver sous Superpowers ».

## Ce que YB a demandé

- Une page à part pour consulter l'avancement, même pratique pour Plugboard,
  Argos et Motorya (Volya a déjà un board généré).
- Pour Plugboard, plutôt un Kanban, avec des tickets à remonter.
- Utiliser Superpowers pour Plugboard.

## Existant (base `d37adfb`, 4.4.0)

- Tableau de bord : `static/index.html` + `static/app.js`, onglets par ancre
  (`TAB_LOADERS`, `#overview` … `#jobs`). Responsive déjà présent
  (`@media` 768/640/480 px, `pointer: coarse`).
- Accès : `GET /dashboard/{path}` sert tout HTML de `static/` derrière
  `_check_dashboard_auth` (redirection `/login?next=…`), sauf `portal.html`.
  Les API d'administration dépendent de `_require_admin` (session admin,
  cookie `SameSite=Strict`, MFA éventuelle).
- Persistance locale : SQLite par domaine sous `data/` (`audit.db`,
  `monitoring.db`, `cache.db`…), écriture atomique JSON dans `storage.py`,
  journal `audit.log_action`.
- Superpowers : plugin 7.0.1 installé. Ses plans/specs Plugboard existent
  seulement en traces locales `.atelier/traces/*/docs/superpowers/{plans,specs}`
  (cases `- [ ]` / `- [x]`, sections `### Task N`) ; aucun
  `docs/superpowers/` versionné avant ce document.
- Arriéré : `TODOS.md` (entrées What/Why/Effort/Priority).
- Aucun Kanban, aucune route `/suivi`, aucun modèle de ticket.

## Approches (à choisir par YB)

1. **Recommandée — `/suivi` dans Plugboard, Kanban persistant.** Page séparée
   `/suivi` servie par l'application existante, derrière la même session admin.
   Tickets en SQLite `data/suivi.db`, API `/admin/suivi/*`. Les plans
   Superpowers et `TODOS.md` sont **importés** comme tickets (lecture seule de
   la source, aucune modification). Les boards Argos/Motorya/Volya restent dans
   leurs projets ; `/suivi` ne montre que Plugboard. Coût : M. Pas de seconde
   gestion de comptes.
2. **Portail central quatre projets.** Une entrée unique pour les quatre
   boards. Coût : service et contrôle d'accès transversaux, couplage entre
   projets ; hors du périmètre Plugboard seul.
3. **Page statique générée.** Export HTML depuis plans/`TODOS.md`. Simple à
   consulter, mais impossible de créer ou déplacer un ticket durablement.

Choix : **en attente de YB.**

## Conception détaillée (si approche 1)

### Page

- `GET /suivi` → même garde que `/dashboard` (`_check_dashboard_auth`,
  sinon `/login?next=/suivi`). Fichiers `static/suivi.html`, `suivi.js`,
  `suivi.css` réutilisant `style.css`/`theme.js`. Lien depuis la barre
  latérale du tableau de bord ; pas un onglet de plus.
- Mobile (≥ 360 px) : colonnes empilées en accordéon ou défilement horizontal
  à ancrage ; cibles ≥ 44 px ; le déplacement se fait par un sélecteur de
  statut (pas uniquement par glisser-déposer, inutilisable au tactile et au
  clavier).

### Colonnes et cycle de vie

`a_trier` → `pret` → `en_cours` → `en_revue` → `termine`, plus `bloque`
(motif obligatoire) et `abandonne` (motif obligatoire). Transitions libres
entre colonnes actives ; `termine` exige au moins une preuve.

### Ticket

`id`, `titre`, `description`, `statut`, `priorite` (P0–P3), `type`
(bug/fonction/dette/securite), `source` (`manuel`, `superpowers-plan`,
`todos`), `source_ref` (chemin + ancre de tâche, pour dédupliquer), `preuves`
(liste : commande + code retour, commit, lien CI, chemin), `cree_le`,
`maj_le`, `auteur`. Historique des transitions append-only.

### Remontée des tickets (Superpowers)

- Import explicite (`POST /admin/suivi/import`, bouton « Importer ») depuis :
  `docs/superpowers/plans/*.md` du dépôt (une tâche `### Task N` ou case
  `- [ ]` = un ticket), `docs/superpowers/specs/*.md` (un ticket « à
  planifier » par spec non suivie), `TODOS.md` (une entrée = un ticket).
- Idempotent : clé `source_ref`. Une case cochée dans le plan propose le
  passage à `en_revue`, jamais `termine` sans preuve.
- Les traces `.atelier/` ne sont pas importées par défaut (privées, non
  versionnées).
- Créer un ticket **ne lance aucun agent** ni job.

### API

`GET /admin/suivi/tickets`, `POST /admin/suivi/tickets`,
`PATCH /admin/suivi/tickets/{id}`, `POST /admin/suivi/tickets/{id}/preuves`,
`POST /admin/suivi/import`. Toutes sous `_require_admin` ; écritures
journalisées via `audit.log_action`. Pas d'endpoint public ni MCP dans cette
version.

### Ce qui n'est pas inclus

Portail multi-projets, synchronisation Linear/GitHub/GitLab, publication du
board sur le VPS, notifications, exécution automatique d'agents.

## Tests prévus

- Unitaires : parseurs plan/`TODOS.md`, idempotence d'import, règles
  `termine`/`bloque`/`abandonne`.
- API : 401/redirection sans session ; création/déplacement/preuve avec
  session admin ; une opération permise appariée à chaque refus.
- E2E Playwright (`tests/e2e`) : desktop et 390×844, déplacement par
  sélecteur, absence de débordement horizontal, a11y (`test_a11y.py`).

## Points à faire approuver

1. Approche (1, 2 ou 3).
2. Accès : admin authentifié uniquement (proposé).
3. Colonnes et cycle de vie ci-dessus.
4. Sources importées : plans/specs versionnés + `TODOS.md` ; `.atelier/` exclu.
5. « Terminé » exige une preuve.
6. Base de branche : `d37adfb` (4.4.0, non fusionnée dans `main` 4.3.0).

Après approbation : plan `superpowers:writing-plans` dans
`docs/superpowers/plans/`, puis implémentation TDD dans ce worktree.

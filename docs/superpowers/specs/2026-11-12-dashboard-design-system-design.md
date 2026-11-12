# Charte graphique du tableau de bord — conception

**Statut : EN ATTENTE de YB** (ticket PB-DASH-02, P3). Base : `dde9e53` (`sprint/recette-uat`).
Plan : `docs/superpowers/plans/2026-11-12-dashboard-design-system.md` (écrit sur les réponses
recommandées ci-dessous).

## Intention

Le tableau de bord n'a pas de charte écrite : chaque nouvelle page reprend des valeurs au hasard
et les libellés mélangent les langues. On écrit `DESIGN.md` (jetons de couleur, de texte et
d'espacement) et on passe le tableau de bord dessus, **sans rien changer à son comportement**.
Réussite : même allure qu'aujourd'hui, mais une seule source de valeurs, et un test qui empêche
les valeurs en dur de revenir.

## Existant (mesuré au code, `static/`)

| Sujet | Constat |
|---|---|
| Couleurs | 50 variables dans `style.css`, thèmes sombre et clair. 69 couleurs en dur ailleurs : 29 dans la scène 3D, 11 dans le graphique du portail (`portal.js:244-278`, bleu et jaune hors palette), 11 dans la pastille des appels en cours (`live_requests.js:88-99`), 6 dans `style.css` (fond de fenêtre, logo), 7 dans `index.html`, 5 dans `live3d.js`. |
| Variables cassées | `--danger` (`app.js:1623`, bouton *Delete* d'Ollama jamais rouge) et `--text-primary` (`live_requests.js:95`) ne sont définies nulle part. |
| Tailles de texte | 94 tailles en dur, **27 valeurs différentes** (de 9 px à 37 px) : `style.css` 45, `index.html` 19, `app.js` 18, `suivi.css` 10, `live_requests.js` 2. |
| Espacements | 214 marges, retraits et écarts en dur, **32 valeurs différentes** : `style.css` 92, `index.html` 58, `app.js` 31, `suivi.css` 26, autres 7. |
| Styles en ligne | 70 attributs `style=` dans `index.html`, 37 dans `app.js` (+ 8 `cssText`). |
| Portail client | `portal.html` utilise `.header-left` et `.overview-grid`, absentes de `style.css` : en-tête et chiffres sans mise en page. Thème sombre imposé. |
| Libellés | 41 libellés français : page `/suivi` entière (`suivi.html` 12, `suivi.js` 28) et l'entrée de menu « Suivi » (`index.html:99`). Côté anglais : ~31 titres en *Title Case* contre des titres en casse de phrase, « catalogue » (3 : `index.html:415`, `app.js:1515`, `app.js:1545`) contre « catalog » partout ailleurs, « Logout » à côté de « Log out everywhere », « Endpoint : » à la française (`app.js:703-704`), « ... » et « … » mêlés. |

## Proposition

**Couleurs** : la palette actuelle (« encre et champagne », deux thèmes) reste telle quelle. On
corrige les deux variables cassées (`--red`, `--text`), on ajoute `--overlay` (fond des fenêtres)
et `--logo-bg`, et le graphique du portail prend ses couleurs dans la palette (`--accent`,
`--sapphire`, `--border`, `--text-muted`). La scène 3D garde ses couleurs fixes : elle reste
sombre dans les deux thèmes ; exception écrite dans `DESIGN.md`.

**Texte** : 7 tailles au lieu de 27, calées sur les valeurs les plus fréquentes (écart ≤ 1,5 px).

| Jeton | Valeur | Usage |
|---|---|---|
| `--fs-label` | 0.72rem (10,8 px) | étiquettes en capitales, badges, en-têtes de tableau |
| `--fs-xs` | 0.78rem (11,7 px) | aides, métadonnées, code |
| `--fs-sm` | 0.875rem (13,1 px) | boutons, champs, tableaux, menus |
| `--fs-md` | 1rem (15 px) | texte courant, titres de carte |
| `--fs-lg` | 1.3rem (19,5 px) | sous-titres, nom de marque |
| `--fs-xl` | 1.75rem (26 px) | titres de page (empattement) |
| `--fs-2xl` | 2.1rem (31,5 px) | chiffres clés, titre de connexion |

Graisses `--fw-regular|medium|semibold|bold` (400 à 700) ; le 550 des boutons devient 600, ce
que le navigateur affiche déjà.

**Espacements** : une échelle en quarts de rem, `--space-1` (0.25rem) à `--space-12` (3rem) :
1, 2, 3, 4, 5, 6, 8, 10, 12. Chaque valeur va au cran le plus proche. Les retraits internes des
contrôles gardent leurs valeurs exactes via des jetons de composant (`--pad-control`,
`--pad-input`, `--pad-badge`, `--pad-cell`…) : aucun bouton ni champ ne change de hauteur.
Rayons : on ajoute `--radius-xs` (4 px), `--radius-control` (8 px) et `--radius-pill`.

**Styles en ligne** : déplacés vers une douzaine de classes nommées dans `style.css`
(`.hint`, `.field-row`, `.setup-guide`, `.chat-bubble`, `.device-flow`…). Restent en ligne,
parce que le JS s'en sert : les `display:none` initiaux, les bascules `.style.display` et
`.style.width`, les `--c` de la légende 3D.

**Libellés** : tout le produit en anglais américain ; casse de phrase pour titres, boutons et
menus (sauf noms propres et sigles : API, CLI, GitHub, Ollama) ; pas d'espace avant « : » ;
« … » pour un état en cours (le « ... » seul de la bulle d'attente, attendu par un test, reste) ;
« Log out ».

**Garde-fou** : un test statique (`tests/test_dashboard_design_tokens.py`) échoue si une variable
utilisée n'est pas définie, si une taille, un espacement ou une couleur est écrit en dur hors des
exceptions listées, si `DESIGN.md` oublie un jeton, ou si un libellé français revient.

## Périmètre

Touchés : `static/style.css`, `suivi.css`, `index.html`, `app.js`, `portal.html`, `portal.js`,
`suivi.html`, `suivi.js`, `live_requests.js`, `auto_update.js` (libellés), nouveau `DESIGN.md`,
le test ci-dessus, `tests/e2e/test_suivi.py` (libellés), `CHANGELOG.md`, `CONTRIBUTING.md`, et
les ~30 mentions de libellés renommés dans `README.md`, `docs/guide/` et `docs/runbooks/`.
**Inchangés** : identifiants, `data-testid`, structure et champs des formulaires (dont la case
*Public addresses only*), gestionnaires JS, appels et valeurs d'API (statuts de `/suivi`), URL,
code Python, et tous les textes vérifiés par les tests (*Log out everywhere*, *Revoke*,
*Run now*, *Already connected as*…).

## Approches envisagées

1. **Consolider en place** (recommandée) : jetons dans `style.css`, valeurs ramenées au cran le
   plus proche, styles en ligne déplacés en classes. Même allure, une seule source. Gros diff
   sur `index.html` et `app.js`, donc conflits possibles à l'assemblage (voir Q5).
2. **Jetons dans les feuilles CSS seulement** : diff petit, pas de conflit, mais ~140 valeurs
   en ligne restent en dur ; la charte n'est pas vraiment adoptée.
3. **Refonte** (nouvelle palette, échelle stricte, ou bibliothèque type Open Props) :
   changement visible, nouvelle dépendance, hors du « aucun changement de comportement ».

## Questions pour YB

1. **Palette** : la garder telle quelle (recommandé : déjà en variables, contrastes AA dans les
   deux thèmes ; la retoucher serait un changement visible hors ticket) ou la retoucher ?
2. **Tailles et espacements** : 7 tailles et 9 crans calés sur l'existant (recommandé : écart
   ≤ 1,5 px, rien ne saute à l'œil) ou une échelle mathématique stricte (plus régulière, mais
   titres, chiffres et boutons bougent) ?
3. **Page `/suivi` en français** : la passer en anglais, menu « Tickets » (recommandé : règle
   produit, 41 libellés ; les valeurs de l'API ne changent pas ; les messages d'erreur du
   serveur, `plugboard/suivi.py`, restent français pour un ticket à part) ou la garder en
   français comme exception écrite (c'est ton outil interne) ?
4. **Portail client** : l'aligner sur le tableau de bord (recommandé : son en-tête et ses
   chiffres n'ont aujourd'hui aucune mise en page ; graphique dans la palette ; il suit le thème
   du système comme le reste) ou ne toucher qu'à ses libellés ?
5. **Page Ollama**, modifiée en parallèle par PLU-12 (Monitoring, touché par PB-PROV-02, n'a
   aucun style en ligne : seul `style.css` le change) : son nettoyage dans un commit à part, en
   dernier (recommandé : l'assemblage le rejoue ou l'écarte sans toucher au reste) ou hors de
   ce ticket ?
6. **Polices Google Fonts** : les laisser (recommandé : les embarquer, c'est des fichiers
   binaires, une licence et la CSP à revoir, mieux dans un ticket à part) ou les embarquer
   maintenant ?

## Choix faits seul (réversibles)

Anglais américain ; `DESIGN.md` en anglais à la racine ; jetons inutilisés (`--purple`,
`--teal`, `--sapphire`) gardés comme couleurs de séries de graphique ; tailles d'icône (vide,
logo) hors échelle ; la skill de consultation design de gstack n'est pas installée sur ce
serveur, la conception s'en passe.

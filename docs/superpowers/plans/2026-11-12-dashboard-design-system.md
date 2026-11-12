# Charte graphique du tableau de bord (PB-DASH-02) — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Le tableau de bord, le portail client et `/suivi` n'utilisent plus que les jetons de `DESIGN.md`, avec des libellés anglais cohérents, sans aucun changement de comportement.

**Architecture:** Jetons ajoutés au bloc `:root` de `static/style.css` ; valeurs en dur ramenées au cran le plus proche ; styles en ligne de `index.html` et des scripts déplacés en classes nommées de `style.css`. Un test statique (`tests/test_dashboard_design_tokens.py`) pilote chaque tâche (rouge, puis vert) et garde la règle ensuite.

**Tech Stack:** CSS sans préprocesseur, JS sans framework, Chart.js vendu (`static/vendor/chartjs`), pytest, node (`--check`), MCP playwright pour les captures.

**Spec:** `docs/superpowers/specs/2026-11-12-dashboard-design-system-design.md` — **plan écrit sur les réponses recommandées Q1 à Q6** ; si YB répond autrement, ajuster : Q3 « garder le français » supprime la tâche 9 ; Q4 « libellés seulement » réduit la tâche 7 à ses libellés ; Q5 « hors ticket » supprime la tâche 10 et garde l'exclusion Ollama du test.

## Global Constraints

- Aucun changement de comportement : identifiants, `data-testid`, `name`, ordre et champs des formulaires (dont `pf-egress-guard`), gestionnaires, appels et valeurs d'API, URL, code Python inchangés.
- Restent en ligne : `display:none` initiaux, bascules JS `.style.display` / `.style.width` / `.style.opacity`, `--c:#…` de la légende 3D (`index.html:141-145`), `position:absolute` du sprite SVG (`index.html:15`).
- Pièges de structure : le message d'accueil du Playground garde la classe `muted` (`_pgAddMessage` le retire via `querySelector(".muted")`) et aucune bulle ne la prend ; `_docsHtml` n'est pas touché (`test_docs_base_url` exige `<a href=` en tête) ; `.dp-copy-btn` reste ; les `"..."` seuls d'`app.js:1209` (cellule santé) et `app.js:1947` (bulle d'attente, attendue par `tests/e2e/test_playground_chat.py:92`) restent.
- Textes vérifiés par les tests, inchangés : *Public addresses only*, *Always on for catalog and built-in cloud presets.*, *Leave off for Ollama or another local server.*, *Refused: non-public address*, *public only*, *Log out everywhere*, *Revoke*, *Confirm*, *Run now*, *Provider watchdog*, *Add*, *Ollama offline*, *available*, *active*, *revoked*, *Edit*, *Already connected as*, *Not connected yet*, *Jobs*, *Providers*, *N models*.
- Jetons exacts (dans `:root`, communs aux deux thèmes) :
  `--fs-root: 15px; --fs-label: 0.72rem; --fs-xs: 0.78rem; --fs-sm: 0.875rem; --fs-md: 1rem; --fs-lg: 1.3rem; --fs-xl: 1.75rem; --fs-2xl: 2.1rem; --fs-icon: 2.5rem;`
  `--fw-regular: 400; --fw-medium: 500; --fw-semibold: 600; --fw-bold: 700;`
  `--space-1: 0.25rem; --space-2: 0.5rem; --space-3: 0.75rem; --space-4: 1rem; --space-5: 1.25rem; --space-6: 1.5rem; --space-8: 2rem; --space-10: 2.5rem; --space-12: 3rem;`
  `--pad-control: 0.45rem 0.9rem; --pad-control-sm: 0.3rem 0.65rem; --pad-input: 0.5rem 0.8rem; --pad-badge: 0.2rem 0.6rem; --pad-chip: 0.15rem 0.45rem; --pad-cell: 0.75rem 1rem;`
  `--radius-xs: 4px; --radius-control: 8px; --radius-pill: 999px;` (existants gardés : `--radius-sm` 6px, `--radius` 10px, `--radius-lg` 14px, `--radius-xl` 18px)
  `--overlay: rgba(5,6,9,0.6); --logo-bg: radial-gradient(120% 120% at 30% 20%, #2a241a 0%, #120f0a 70%); --inset-highlight: rgba(255,255,255,0.18);`
- Correspondance des valeurs (cran le plus proche) :
  tailles 0.6–0.72rem → `--fs-label` · 0.75–0.82rem, 12px → `--fs-xs` · 0.85–0.9rem → `--fs-sm` · 0.95–1.05rem → `--fs-md` · 1.25–1.45rem → `--fs-lg` · 1.75rem → `--fs-xl` · 2–2.1rem → `--fs-2xl` · `html` 15px → `--fs-root` · `.empty-icon` → `--fs-icon` ;
  graisses 550 → `--fw-semibold` ;
  espacements ≤ 0.35rem, 2px, 4px → `--space-1` · 0.4–0.6rem, 6px, 8px → `--space-2` · 0.65–0.85rem, 10px, 12px → `--space-3` · 0.9–1.1rem → `--space-4` · 1.15–1.35rem → `--space-5` · 1.5–1.75rem → `--space-6` · 2rem → `--space-8` · 2.5rem → `--space-10` · 3rem → `--space-12` ; retraits internes : `.btn` → `--pad-control`, `.btn-sm` → `--pad-control-sm`, champs → `--pad-input`, `.badge`, `.suivi-count` → `--pad-badge`, `td code` → `--pad-chip`, `td`, `th` → `--pad-cell` ;
  rayons 4px → `--radius-xs` · 6–7px → `--radius-sm` · 8–9px → `--radius-control` · 10px → `--radius` · 12–14px → `--radius-lg` · 18px → `--radius-xl` · 999px → `--radius-pill` ; 2px, 3px, 50% restent.
- Marqueurs de `style.css` lus par le test : `/* design-tokens: start */` … `/* design-tokens: end */` autour des trois blocs de thème ; `/* design-tokens: exception start (live 3D scene, dark in both themes) */` … `/* design-tokens: exception end */` autour du bloc « Live routing (3D) ».
- Libellés : anglais américain, casse de phrase (sauf noms propres et sigles), pas d'espace avant « : », « … » pour un état en cours, « Log out ». `DESIGN.md`, CHANGELOG et docs en anglais. Aucune donnée personnelle, aucun nom de client.
- Variables : `SCRATCH` = dossier scratch de la session qui exécute ; `PYTEST="env TMPDIR=$SCRATCH PLUGBOARD_SKIP_DOTENV=1 PYTHONDONTWRITEBYTECODE=1 /srv/dev/plugboard/.venv/bin/python -m pytest -q -p no:cacheprovider"`, lancé depuis la racine de l'atelier, un fichier à la fois, jamais tout `tests/`.
- Commits Conventional Commits, portée `dashboard`, un par tâche, lignes d'attribution de la session. Pas de `git push`, pas de `ci`, pas de `board`, pas de `rm -rf`, pas de `docker`, aucune inférence.

## Review Focus

- Thème clair : chaque classe nouvelle n'utilise que des variables de couleur, sinon un bloc reste sombre sur fond « papier » → captures claires de la tâche 11 (vue d'ensemble, Providers, `/suivi`, portail) comparées aux captures d'avant.
- Écran tactile : la règle `@media (pointer: coarse)` (cibles de 44 px) doit encore s'appliquer aux contrôles déplacés en classes ; ils gardent donc leurs classes `btn`/`btn-sm` (`.pg-check-btn`, `.setup-guide-copy` s'y ajoutent, ne les remplacent pas) → `test_touch_rule_still_covers_controls` (tâche 3, test d'épinglage : déjà vert, il doit le rester).
- Playground : premier message envoyé, l'accueil disparaît, la bulle d'attente `...` est remplacée → vérifié à la main tâche 5 (sans envoyer de vrai message : vérifier au code que `muted` et `"..."` sont intacts) + `test_playground_keeps_its_js_hooks` (tâche 5).
- Fenêtre GitHub (*device flow*) : cachée au chargement, centrée quand elle s'ouvre (`style.display = "flex"` en JS) → elle garde son `display:none` en ligne ; capture forcée en tâche 11 via `browser_evaluate` sur `#device-flow-modal`.
- Portail en thème clair : le graphique lit ses couleurs au rendu ; un changement de thème après coup ne le redessine pas (accepté, écrit dans `DESIGN.md`) → capture claire du portail en tâche 11.

---

### Task 1: Captures « avant » (aucun commit)

**Files:** aucun dans le dépôt ; tout dans `$SCRATCH`.

- [ ] **Step 1: Préparer une instance isolée**

Dans `$SCRATCH/data` : graine d'un fournisseur comme `tests/e2e/conftest.py:_seed_provider`, mais `name='Demo provider'`, `base_url='http://127.0.0.1:9'`, modèles `demo-model-a`, `demo-model-b` (jamais `11434`, c'est l'Ollama de l'UAT). Clé d'admin et `ENCRYPTION_KEY` (Fernet) générées pour l'occasion, jamais écrites dans le dépôt.

- [ ] **Step 2: Lancer l'instance en tâche de fond**

```bash
cd /srv/dev/worktrees/plugboard/dashboard-design && env PLUGBOARD_DATA=$SCRATCH/data PLUGBOARD_SKIP_DOTENV=1 \
  HOST=127.0.0.1 PORT=18802 RELOAD=false ADMIN_KEY=$ADMIN ENCRYPTION_KEY=$ENC RATE_LIMIT=100000 \
  PRICING_SCHEDULER_DISABLED=1 MODEL_AVAILABILITY_SCHEDULER_DISABLED=1 PLUGBOARD_PROBE_ON_BOOT=false \
  PLUGBOARD_INTERNET_CHECK_ON_BOOT=0 OLLAMA_URL=http://127.0.0.1:9 /srv/dev/plugboard/.venv/bin/python run.py
```
Attendu : `http://127.0.0.1:18802/dashboard` répond. Puis, par l'API locale avec `$ADMIN` : trois tickets `/admin/suivi/tickets` (un `a_trier`, un `en_cours`, un `bloque` avec motif) ; une clé client créée depuis l'onglet API keys (pour le portail).

- [ ] **Step 3: Captures**

MCP playwright, pleine page, dans `$SCRATCH/captures/avant/<page>-<largeur>-<thème>.png` : les 9 onglets (`#overview` … `#monitoring`), `/suivi`, le portail connecté — à 390 et 1280 px en thème sombre (22 captures), plus vue d'ensemble, Providers, `/suivi`, portail à 1280 px en thème clair (`localStorage["plugboard-theme"]="light"` puis rechargement ; 4 captures). Ne jamais envoyer de message dans le Playground. Laisser l'instance tourner pour la tâche 11, ou l'arrêter et la relancer à l'identique.

### Task 2: Jetons, variables cassées, `DESIGN.md` et début du test

**Files:**
- Create: `tests/test_dashboard_design_tokens.py`, `DESIGN.md`
- Modify: `static/style.css:5-116` (jetons + marqueurs), `static/app.js:1623` (`--danger` → `--red`), `static/live_requests.js:95` (`--text-primary` → `--text`), `CONTRIBUTING.md` (une ligne : « Dashboard changes follow `DESIGN.md`; `tests/test_dashboard_design_tokens.py` keeps raw values out. »)

**Interfaces — Produces (dans le fichier de test, réutilisés par les tâches 3 à 10) :**
- `ROOT`, `STATIC`, `CSS_FILES = ("style.css", "suivi.css")`, `_read(name: str) -> str`.
- `_root_tokens() -> set[str]` : propriétés définies entre `design-tokens: start` et `design-tokens: end` de `style.css`.
- `_checked_css(name: str) -> str` : la feuille sans commentaires ni régions marquées (start/end et exception start/end).
- `_declarations(css: str) -> list[tuple[str, str]]` : couples `(propriété, valeur)` via `([\w-]+)\s*:\s*([^;{}]+)`.
- `COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(")`.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_css_variable_used_is_defined():
    defined = set()
    for name in (*CSS_FILES, "index.html"):  # index.html: the --c of the 3D legend
        defined |= set(VAR_DEF.findall(_read(name)))
    missing = {}
    for path in sorted(STATIC.glob("*.*")):
        if path.suffix in {".css", ".js", ".html"}:
            for var in VAR_USE.findall(path.read_text(encoding="utf-8")):
                if var not in defined:
                    missing.setdefault(var, []).append(path.name)
    assert missing == {}

def test_design_md_documents_every_token():
    design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
    assert sorted(t for t in _root_tokens() if f"`{t}`" not in design) == []
    assert NEW_TOKENS <= _root_tokens()
```
`VAR_USE = re.compile(r"var\(\s*(--[\w-]+)")`, `VAR_DEF = re.compile(r"(?<![\w-])(--[\w-]+)\s*:")`, `NEW_TOKENS` = les noms des jetons des Global Constraints.

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_dashboard_design_tokens.py` : `missing == {'--danger': ['app.js'], '--text-primary': ['live_requests.js']}` et `FileNotFoundError: DESIGN.md`.

- [ ] **Step 3: Implement** — jetons des Global Constraints ajoutés au premier bloc `:root` ; marqueurs `design-tokens: start/end` autour des lignes 5-116 ; les deux variables corrigées ; `DESIGN.md` en anglais : principes (une seule source, pas de valeur en dur, deux thèmes), tableaux couleurs (nom, sombre, clair, usage), texte (familles, tailles, graisses), espacements et retraits, rayons et ombres, composants (bouton, badge, carte, tableau, champ, alerte, quels jetons), exceptions (scène 3D, icônes, styles en ligne gardés et pourquoi, graphique du portail lu au rendu), règles des libellés, « ajouter une page » (liste de contrôle), le test garde-fou. Chaque jeton cité entre accents graves.

- [ ] **Step 4: Run, expect PASS** — même commande, 2 passed.

- [ ] **Step 5: Commit** — `feat(dashboard): add design tokens and DESIGN.md`

### Task 3: Feuilles de style sur les jetons

**Files:** Modify `static/style.css` (hors blocs de jetons), `static/suivi.css` ; Test : même fichier.

**Interfaces — Produces:** `_violates(prop: str, value: str) -> bool` ; marqueurs d'exception autour du bloc 3D (`style.css:483-552`).

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("name", CSS_FILES)
def test_stylesheet_uses_tokens(name):
    bad = [f"{p}: {v}" for p, v in _declarations(_checked_css(name)) if _violates(p, v)]
    assert bad == []

@pytest.mark.parametrize("name", CSS_FILES)
def test_stylesheet_has_no_raw_colour(name):
    assert [v for _, v in _declarations(_checked_css(name)) if COLOUR.search(v)] == []

def test_touch_rule_still_covers_controls():
    rule = re.search(r"@media \(pointer: coarse\)\s*\{(.*?)\n\}", _read("style.css"), re.S).group(1)
    for selector in (".btn", "input", "select", ".nav-link"):
        assert selector in rule
```
`_violates` : `font-size` ∈ {`var(--fs-*)`, `inherit`} ; `font-weight` ∈ {`var(--fw-*)`, `inherit`} ; chaque partie de `padding*`, `margin*`, `gap`, `row-gap`, `column-gap` ∈ {`0`, `auto`, `var(--space-*)`, `var(--pad-*)`, `var(--sidebar-w)`} ; chaque partie de `border-radius` ∈ {`0`, `2px`, `3px`, `50%`, `var(--radius*)`} ; `!important` ignoré.

- [ ] **Step 2: Run, expect FAIL** — plus d'une centaine de déclarations listées pour `style.css`, plusieurs dizaines pour `suivi.css`, 6 couleurs dans `style.css`.

- [ ] **Step 3: Implement** — chaque valeur remplacée selon la table des Global Constraints ; `.modal` → `var(--overlay)`, les deux logos → `var(--logo-bg)`, `.btn-primary` → `var(--inset-highlight)` ; marqueurs d'exception autour du bloc « Live routing (3D) ». Rien d'autre ne change dans les sélecteurs.

- [ ] **Step 4: Run, expect PASS**, puis `$PYTEST tests/test_e2e_dashboard.py` et `$PYTEST tests/test_monitoring.py` verts.

- [ ] **Step 5: Commit** — `refactor(dashboard): move the stylesheets onto the design tokens`

### Task 4: Styles en ligne → classes (connexion, clés, fournisseurs, prix, tâches)

**Files:**
- Modify: `static/index.html:34-44, 227-252, 298-310, 350-359` ; `static/app.js:204, 615-640, 691-780, 820-1063, 1397, 1414` ; `static/auto_update.js:265` ; `static/style.css` (nouvelles classes en fin de fichier, section « Components moved from inline styles »).
- Test : même fichier.

**Interfaces — Produces (classes, toutes sur jetons) :**
utilitaires `.mt-1 .mt-2 .mt-3 .mt-4 .mb-2 .mb-3 .mb-4 .ml-1 .ml-2` (marge = `--space-N`), `.fw-semibold`, `.text-muted` (couleur seule, pas `.muted`), `.text-xs`, `.row` (flex, centré, écart `--space-2`), `.row-between`, `.grow` (`flex: 1`) ; composants `.card-body` (`--space-5`), `.field-label`, `.login-head`, `.setup-guide`, `.setup-guide-title`, `.setup-guide-steps`, `.setup-guide-step`, `.setup-guide-desc`, `.setup-guide-link`, `.setup-guide-cmd`, `.setup-guide-copy`, `.preset-endpoint`, `.device-code`, `.device-status`, `.pricing-new-name` (200px), `.pricing-new-provider` (170px), `.pricing-new-price` (120px), `.log-pre`. La fenêtre GitHub prend `.modal-box` ; le bandeau `preset.reason` (`app.js:706`) prend `alert alert-warning small`.
- Test : `PENDING_HTML_SECTIONS = ("page-playground", "page-ollama")`, `PENDING_JS = (("function _initOllamaEvents", "function _initPlaygroundEvents"), ("function _initPlaygroundEvents", None))`, `_without_pending_html(html) -> str`, `_without_pending_js(js) -> str`, `_inline_declarations(text) -> list[str]` (contenu de `style="…"`, `style=\"…\"` et `style: "…"`, découpé sur `;`, espaces retirés).

- [ ] **Step 1: Write the failing tests**

```python
def test_index_inline_styles_only_where_js_needs_them():
    html = _without_pending_html(_read("index.html"))
    allowed = re.compile(r"^(display:\s*none|--c:\s*#[0-9a-fA-F]{6}|position:\s*absolute)$")
    assert [d for d in _inline_declarations(html) if not allowed.match(d)] == []

@pytest.mark.parametrize("name", ["app.js", "auto_update.js"])
def test_scripts_inline_styles_only_where_js_needs_them(name):
    js = _without_pending_js(_read(name))
    assert "cssText" not in js
    assert [d for d in _inline_declarations(js) if not re.match(r"^(display|width)\s*:", d)] == []
```

- [ ] **Step 2: Run, expect FAIL** — plusieurs dizaines de déclarations listées.
- [ ] **Step 3: Implement** — remplacer chaque attribut par les classes ci-dessus ; un `style="display:none; margin-bottom:1rem;"` devient `class="… mb-4" style="display:none"` ; les spans d'état `color:var(--green|red|text-muted)` → `text-green`, `text-red`, `text-muted`. Garder l'ordre des attributs `href`/`target`/`rel`.
- [ ] **Step 4: Run, expect PASS**, puis `$PYTEST tests/test_egress_guard_dashboard.py`, `tests/test_docs_base_url.py`, `tests/test_security_audit_2026_04.py` (un à la fois) verts et `node --check static/app.js static/auto_update.js`.
- [ ] **Step 5: Commit** — `refactor(dashboard): replace inline styles with token classes`

### Task 5: Playground

**Files:** Modify `static/index.html:429-460`, `static/app.js:1888-1921, 1995-2010, 2110-2125, 2157`, `static/style.css` ; Test : même fichier (retirer `"page-playground"` et la plage Playground des constantes `PENDING_*`).

**Interfaces — Produces:** `.pg-layout` (colonne, `height: calc(100vh - 120px)`), `.pg-toolbar`, `.pg-select`, `.pg-check-btn`, `.pg-option`, `.pg-num`, `.pg-messages`, `.pg-empty`, `.pg-composer`, `.pg-input`, `.pg-send`, `.pg-status`, `.chat-bubble`, `.chat-bubble-user`, `.chat-bubble-assistant` (rayons `var(--radius-lg)` avec le coin 2px), `.chat-divider`, `.chat-thinking`, `.chat-thinking-summary`, `.chat-thinking-body`.

- [ ] **Step 1: Write the failing test**

```python
def test_playground_keeps_its_js_hooks():
    js, html = _read("app.js"), _read("index.html")
    assert 'container.querySelector(".muted")' in js
    assert re.search(r'class="muted[^"]*pg-empty|class="pg-empty[^"]*muted', html)
    assert '_pgAddMessage("assistant", "...")' in js
    assert 'bubble.dataset.testid = "message-bubble"' in js
```
plus la suppression des entrées Playground de `PENDING_*`.
- [ ] **Step 2: Run, expect FAIL** — les tests d'inline de la tâche 4 listent le Playground (dont 8 `cssText`).
- [ ] **Step 3: Implement** — `cssText` → `className` ; aucune bulle ne reçoit `muted`.
- [ ] **Step 4: Run, expect PASS** + `node --check static/app.js`.
- [ ] **Step 5: Commit** — `refactor(dashboard): move the Playground onto token classes`

### Task 6: Pastille des appels en cours

**Files:** Modify `static/live_requests.js:86-99` (supprimer l'élément `<style>` injecté), `static/style.css` (section « Live requests » : `#live-pill`, `#live-panel`, `.live-title`, `.live-empty`, `.live-row`, états `slow`/`error` → `--orange-text`/`--red`, compte non nul → `--green` et `--text`) ; Test : même fichier.

**Interfaces — Produces:** `JS_COLOUR_FREE = ["app.js", "auto_update.js", "live_requests.js"]` (la tâche 7 y ajoute `portal.js`).

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("name", JS_COLOUR_FREE)
def test_scripts_have_no_raw_colour(name):
    assert COLOUR.findall(_read(name)) == []

def test_live_requests_styles_live_in_the_stylesheet():
    assert 'el("style"' not in _read("live_requests.js")
    assert "#live-pill" in _read("style.css") and "#live-panel" in _read("style.css")
```
- [ ] **Step 2: Run, expect FAIL** (11 couleurs, `<style>` injecté).
- [ ] **Step 3: Implement** — mêmes sélecteurs, mêmes positions (`right`/`bottom` sur `--space-3`), tailles `--fs-xs`.
- [ ] **Step 4: Run, expect PASS** + `$PYTEST tests/test_live_requests.py::test_dashboard_polling_stays_under_the_per_address_rate_limit`.
- [ ] **Step 5: Commit** — `refactor(dashboard): style the in-flight pill from the stylesheet`

### Task 7: Portail client aligné

**Files:** Modify `static/portal.html`, `static/portal.js:236-280` (+ libellés), `static/style.css` (`.portal-head`, `.portal-head-left`, `.portal-main`, `.chart-card`) ; Test : même fichier.

**Interfaces — Produces:** `_cssVar(name: string) -> string` dans `portal.js` (`getComputedStyle(document.documentElement).getPropertyValue(name).trim()`) ; couleurs du graphique : barres `--accent`, ligne `--sapphire`, légende et graduations `--text-muted`, grille `--border`.

- [ ] **Step 1: Write the failing tests**

```python
def test_portal_classes_exist_in_stylesheet():
    css = _read("style.css")
    classes = {c for a in re.findall(r'class="([^"]+)"', _read("portal.html")) for c in a.split()}
    assert sorted(c for c in classes if not re.search(r"\." + re.escape(c) + r"(?![\w-])", css)) == []

def test_portal_follows_the_theme_and_has_no_inline_style():
    html = _read("portal.html")
    assert "data-theme=" not in html and 'style="' not in html
    assert html.index("/dashboard/theme.js") < html.index("/dashboard/style.css")
```
et `"portal.js"` ajouté à `JS_COLOUR_FREE` et au paramétrage de `test_scripts_inline_styles_only_where_js_needs_them`.
- [ ] **Step 2: Run, expect FAIL** (`header-left`, `overview-grid` absentes ; thème imposé ; 11 couleurs).
- [ ] **Step 3: Implement** — `<header class="portal-head">`, `<div class="portal-head-left">`, `<main class="portal-main">`, `overview-grid` → `stats-grid`, `#chart-container` → `class="card chart-card"` ; `theme.js?v=2` avant la feuille ; libellés : *Client portal*, *Plugboard — My usage*, *Spending limit*, *Usage by model*, *Daily usage*, *Daily breakdown*, *Log out*, *Total tokens*, *Loading…*, *pb_…*. Les `id` ne changent pas.
- [ ] **Step 4: Run, expect PASS** + `$PYTEST tests/test_e2e_dashboard.py` (portail sans CDN) + `node --check static/portal.js`.
- [ ] **Step 5: Commit** — `feat(dashboard): give the client portal the dashboard layout and theme`

### Task 8: Règles des libellés (tableau de bord et docs)

**Files:** Modify `static/index.html`, `static/app.js`, `README.md:68-69`, `docs/guide/getting-started.md:63-103`, `docs/guide/clients-and-keys.md:24-174`, `docs/guide/providers-and-subscriptions.md:59-159`, `docs/guide/pricing.md:164`, `docs/guide/monitoring.md:27-37`, `docs/guide/troubleshooting.md:55-80`, `docs/runbooks/provider-down.md:139` ; Test : même fichier.

Renommages (ancien → nouveau, à l'identique dans les docs qui les citent) : *API Keys* → *API keys* · *Usage & Billing* → *Usage & billing* · *Create API Key* → *Create API key* · *Create Key* → *Create key* · *RPM Limit* → *RPM limit* · *Admin Key* → *Admin key* · *Metrics by Provider* → *Metrics by provider* · *By Client* / *By Model* → *By client* / *By model* · *Add Direct Provider* → *Add direct provider* · *Add Provider* / *Edit Provider* → *Add provider* / *Edit provider* · *Bearer Token* → *Bearer token* · *Default Model* → *Default model* · *Connect via GitHub Device Flow* → *Connect via GitHub device flow* · *Job Log* → *Job log* · *Avg Latency* → *Avg latency* · *Prompt Tokens* / *Completion Tokens* / *Total Cost* → casse de phrase · *Error Rate* → *Error rate* · *Logout* → *Log out* · `Endpoint :` / `Default model :` → sans espace · `mot...` et `+ "..."` → `…` (sauf les deux `"..."` seuls des Global Constraints).

**Interfaces — Produces:** `LABEL_FILES`, `FORBIDDEN_LABELS` (tuple des anciens libellés ci-dessus, avec `Usage &amp; Billing` sous sa forme HTML), `FRENCH = re.compile(r"[àâçéèêëîïôûùüÿœÀÂÇÉÈÊËÎÏÔÛÙÜŸŒ]")`, `ELLIPSIS = re.compile(r'\w\.\.\.|\+\s*"\.\.\."')`.

- [ ] **Step 1: Write the failing test**

```python
LABEL_FILES = ("index.html", "app.js", "portal.html", "portal.js", "auto_update.js", "live_requests.js")

@pytest.mark.parametrize("name", LABEL_FILES)
def test_labels_follow_the_copy_rules(name):
    text = _without_pending_js(_without_pending_html(_read(name)))
    assert [label for label in FORBIDDEN_LABELS if label in text] == []
    assert ELLIPSIS.findall(text) == []
    assert FRENCH.findall(text) == []
```
- [ ] **Step 2: Run, expect FAIL** (index.html et app.js ; le portail est déjà vert depuis la tâche 7).
- [ ] **Step 3: Implement** — table ci-dessus, puis `grep -rn` des anciens libellés dans `README.md docs/` : aucun reste.
- [ ] **Step 4: Run, expect PASS** + `$PYTEST tests/test_egress_guard_dashboard.py` + `node --check static/app.js`.
- [ ] **Step 5: Commit** — `fix(dashboard): one wording rule for every label`

### Task 9: `/suivi` en anglais

**Files:** Modify `static/suivi.html`, `static/suivi.js`, `static/index.html:99`, `tests/e2e/test_suivi.py:45,58,61,92`, `docs/guide/monitoring.md:40-50` ; Test : même fichier (`"suivi.html"`, `"suivi.js"` ajoutés à `LABEL_FILES`).

Textes : `lang="en"` ; *Tickets — Plugboard* ; *← Dashboard* ; *Ticket board* ; *Import* ; *New ticket* ; *Ticket title* ; *Priority* ; types *Feature*, *Bug*, *Debt*, *Security* ; *Create* ; statuts *Triage*, *Ready*, *In progress*, *In review*, *Done*, *Blocked*, *Abandoned* ; preuves *Command*, *Commit*, *CI*, *Path*, *Link* ; *Session expired* ; *Error* ; *Status* ; *Evidence (n)* ; *Evidence type* ; *e.g. ci journal plugboard <sha>: green* ; *Evidence* ; *Add* ; *Evidence added to ticket #n.* ; *Reason: * ; *No tickets* ; *Reason (required) for “X”:* ; *Change canceled: a reason is required.* ; *Ticket #n created.* ; *Import: a created, b moved to review, c unchanged.* ; menu *Tickets*. Valeurs d'option et clés (`a_trier`, `fonction`…) inchangées. E2E : *En cours* → *In progress*, *Preuve ajoutée* → *Evidence added*, *Terminé* → *Done*, lien *Suivi* → *Tickets*.

- [ ] **Step 1: Write the failing test**

```python
def test_suivi_is_in_english():
    assert '<html lang="en">' in _read("suivi.html")
    words = re.compile(r"\b(Suivi|Importer|Nouveau|Titre|Créer|Aucun|Statut|Motif|Preuves?|Dette|Fonction|Chemin|Lien|Commande)\b")
    for name in ("suivi.html", "suivi.js", "index.html"):
        assert words.findall(_read(name)) == []
```
- [ ] **Step 2: Run, expect FAIL**.
- [ ] **Step 3: Implement** — textes ci-dessus ; les messages d'erreur du serveur (`plugboard/suivi.py`) ne sont pas touchés.
- [ ] **Step 4: Run, expect PASS** + `$PYTEST tests/test_suivi_api.py` + `node --check static/suivi.js`.
- [ ] **Step 5: Commit** — `fix(dashboard): show the ticket board in English`

### Task 10: Page Ollama (commit à part, en dernier)

**Files:** Modify `static/index.html:377-421`, `static/app.js:1427-1741` (seulement styles en ligne et libellés), `static/style.css` (`.card-body-sm` `--space-4`, `.progress`, `.progress-fill`, `.empty-note`, `.stat-value-sm`) ; Test : même fichier (supprimer `PENDING_HTML_SECTIONS`, `PENDING_JS` et leurs appels ; ajouter à `FORBIDDEN_LABELS` *Sovereign Provider*, *Pull Model*, *Installed Models*, *Cloud Catalogue*, `catalogue is empty`, `cloud catalogue:`).

Libellés : *Ollama — sovereign provider*, *+ Pull model*, *Pull model*, *Installed models*, *Cloud catalog*, *The public cloud catalog is empty or unreachable.*, *Failed to load the cloud catalog: *, *Use "+ Pull model" to download one.*, *Starting pull for x…*. Bouton *Delete* : `btn btn-ghost btn-sm text-red`.

- [ ] **Step 1: Write the failing test** — suppression des exclusions (les tests des tâches 4 et 8 couvrent alors la page).
- [ ] **Step 2: Run, expect FAIL** (inline et libellés de la page Ollama).
- [ ] **Step 3: Implement** — mêmes règles que la tâche 4 ; la barre de progression garde `style="width:0%"` (bascule JS).
- [ ] **Step 4: Run, expect PASS** + `node --check static/app.js`.
- [ ] **Step 5: Commit** — `refactor(dashboard): move the Ollama page onto token classes`

### Task 11: Versions d'actifs, CHANGELOG, vérification, captures « après »

**Files:** Modify `static/index.html:10-11,488-491`, `static/suivi.html:8-10,38`, `static/portal.html` (`?v=` : `style.css` 12, `app.js` 23, `auto_update.js` 6, `live_requests.js` 3, `portal.js` 5, `suivi.js` 2, `suivi.css` 2), `CHANGELOG.md` (sous `## [Unreleased]`, `### Changed`).

- [ ] **Step 1: CHANGELOG** — une entrée : `DESIGN.md` et ses jetons (27 tailles → 7, 32 espacements → 9 crans), le test garde-fou, la règle des libellés, `/suivi` en anglais (lien *Tickets*), le portail aligné et qui suit le thème du système, le bouton *Delete* d'Ollama rouge, aucun changement de comportement.
- [ ] **Step 2: Tests ciblés, un à la fois** — `tests/test_dashboard_design_tokens.py`, `test_egress_guard_dashboard.py`, `test_e2e_dashboard.py`, `test_docs_base_url.py`, `test_security_audit_2026_04.py`, `test_suivi_api.py`, `test_monitoring.py`, `test_auth_auto_login.py`, `test_docker_image_content.py`, `test_audit_e2e.py`, `test_live_requests.py::test_dashboard_polling_stays_under_the_per_address_rate_limit`. Attendu : tous verts, sorties collées au rapport. `tests/e2e/` ne tourne pas ici (paquet Python `playwright` absent du venv, axe-core à télécharger) : le dire.
- [ ] **Step 3: Contrôles** — `node --check` sur chaque JS modifié ; `/srv/dev/plugboard/.venv/bin/ruff check tests/test_dashboard_design_tokens.py` et `ruff format --check` ; `make secret-scan`. Attendu : propres.
- [ ] **Step 4: Captures « après »** — mêmes 26 vues que la tâche 1 dans `$SCRATCH/captures/apres/`, plus la fenêtre GitHub ouverte de force (`browser_evaluate` : `document.getElementById("device-flow-modal").style.display="flex"`). Comparer deux à deux : seuls écarts admis, ±2 px et les libellés ; console sans erreur (`browser_console_messages`). Tout écart plus grand : corriger avant de conclure.
- [ ] **Step 5: Arrêt et nettoyage** — arrêter l'instance, garder les captures jusqu'à l'accord de la session principale puis supprimer `$SCRATCH` (`find "$SCRATCH" -delete`) et `.ruff_cache` ; `git status --porcelain` vide.
- [ ] **Step 6: Commit** — `docs(dashboard): changelog and asset versions for the design system`

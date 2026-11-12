# Historique du catalogue par fournisseur (PB-PROV-02) — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un onglet du tableau de bord et une route admin paginée montrent, par fournisseur et par période, les modèles ajoutés ou retirés, sur un journal gardé 365 jours.

**Architecture:** Le journal existant `data/catalog/changes.jsonl` reste la seule source : `change_events` gagne une purge par âge et une fonction de requête ; `app.py` expose `GET /admin/catalog/history` (gestionnaire synchrone, donc hors boucle) ; un nouveau fichier `static/catalog_history.js` dessine l'onglet, branché par une ligne dans `app.js`.

**Tech Stack:** Python 3.12, FastAPI, pytest (`asyncio_mode = auto`, fixtures `admin_client` de `tests/conftest.py`), JS sans framework (helpers `h`, `api`, `emptyState`, `skeleton` de `static/app.js`), MCP playwright pour le contrôle visuel.

**Spec:** `docs/superpowers/specs/2026-11-12-catalog-history-design.md` — **à valider par YB** ; ce plan suit les réponses recommandées (option 1 de Q1 à Q6). Si YB choisit autrement, reprendre les tâches touchées avant de construire.

## Global Constraints

- Atelier `/srv/dev/worktrees/plugboard/catalog-history`, branche `feat/catalog-history`. Rien hors de l'atelier.
- Tests ciblés seulement, un pytest à la fois, jamais tout `tests/`. Commande, depuis l'atelier :
  `PYTEST="env TMPDIR=/tmp/claude-1001/-srv-dev-plugboard/a5d91e11-1edb-4748-99e2-79712c823769/scratchpad/catalog-history PLUGBOARD_SKIP_DOTENV=1 PYTHONDONTWRITEBYTECODE=1 /srv/dev/plugboard/.venv/bin/python -m pytest -q -p no:cacheprovider"`
- Chaque test qui lit ou écrit le journal pointe `PLUGBOARD_DATA` vers `tmp_path` (monkeypatch) et retire `PLUGBOARD_CHANGE_JOURNAL_DAYS` de l'environnement s'il ne le fixe pas.
- Valeurs : `PLUGBOARD_CHANGE_JOURNAL_DAYS` défaut `365`, `0` = sans limite d'âge, valeur invalide ou négative = défaut ; `JOURNAL_MAX_LINES = 10000` ; `limit` 1–200, défaut 50 ; `offset` ≥ 0 ; dates `AAAA-MM-JJ` UTC, bornes incluses.
- Seul l'événement `models.changed` entre dans l'historique ; une entrée exposée n'a que `at`, `provider`, `added`, `removed`.
- Interface et doc en anglais. Libellés exacts : onglet et titre `Catalog history` ; filtres `Provider` (`All providers` + liste), `Period` (`All time`, `Last 7 days`, `Last 30 days`, `Last 90 days`, `Last year`), `Model` (placeholder `Search a model name`) ; lien Monitoring `Full history` ; bouton `Load more` ; vides `No model change recorded yet.` / `No model change matches these filters.` ; erreur `Catalog history unavailable: <message>`.
- Données venues des fournisseurs écrites en texte seulement (`h()`, `textContent`), jamais `innerHTML` avec une donnée.
- Coordination (décision de l'orchestrateur) : PB-PROV-02 est assemblé avant PB-DASH-02. Une seule entrée dans la barre latérale de `index.html` et dans `TAB_LOADERS` ; CSS sur les variables existantes de `static/style.css` seulement, le moins de classes possible, préfixées `ch-`, groupées en fin de fichier sous `/* ── Catalog history (PB-PROV-02) ── */`.
- Ne touche ni `VERSION` ni `plugboard/__init__.py`. CHANGELOG sous `## [Unreleased]` › `### Added`.
- Conventional Commits en anglais, un commit par tâche, terminé par les lignes d'attribution de la session en cours. Ni `git push`, ni `glab`, ni `ci`, ni `docker`, ni commande `board`.
- Aucune vraie clé, aucun nom de client, aucune donnée personnelle dans le code, les tests, la doc, les données de démonstration et les commits.

## Review Focus

- Installation neuve sans journal : `200`, liste vide, `providers: []`, `oldest: null` → `test_history_without_journal` (Task 2).
- Ligne illisible, d'un autre type, sans fournisseur ou avec `added` qui n'est pas une liste : ignorée ou normalisée, jamais un `500` → `test_history_skips_bad_lines` (Task 2).
- Nom cherché au 35e rang d'une entrée de 40 noms : placé en tête, donc visible avant « +30 more », et surligné ; comptes inchangés → `test_history_puts_matching_names_first` (Task 2) et contrôle navigateur (Task 5).
- Nom de modèle hostile (`<img src=x onerror=alert(1)>`) affiché tel quel, sans balise créée → `test_catalog_history_js_writes_text_only` (Task 4) et contrôle navigateur (Task 5).
- Entrée de 40 noms et nom de 120 caractères sur iPhone : repliée après 10, aucun défilement horizontal à 390 px → contrôle navigateur (Task 5).

---

### Task 1: Journal gardé 365 jours

**Files:**
- Modify: `plugboard/change_events.py:1-9` (docstring), `:24` (plafond), `:41-57` (`append_journal`)
- Modify: `docs/guide/configuration.md` (table des webhooks, après `PLUGBOARD_WEBHOOK_MAX_PER_RUN`), `docs/guide/monitoring.md:119`
- Test: `tests/test_catalog_history.py` (nouveau)

**Interfaces — Produces:**
- `JOURNAL_MAX_LINES = 10000`
- `retention_days() -> int` : `envcompat.get_int("PLUGBOARD_CHANGE_JOURNAL_DAYS", default=365, minimum=0)`.
- `append_journal(entries: List[Dict[str, Any]], now: Optional[datetime] = None) -> None` : avant la coupe au plafond, retire les lignes dont `at` est lisible et antérieur à `now - retention_days()` jours (`now` par défaut : UTC courant). Une ligne sans `at` lisible n'est retirée que par le plafond. `retention_days() == 0` : aucune purge par âge. Les appelants actuels ne changent pas.
- `_parse_at(value: Any) -> Optional[datetime]` (UTC, `None` si illisible), réutilisée en Task 2.

- [ ] **Step 1: Write the failing tests**

```python
NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)

def test_append_drops_lines_older_than_retention(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", "30")
    change_events.append_journal(
        [{"at": "2026-10-01T00:00:00Z", "n": 1}, {"at": "2026-10-20T00:00:00Z", "n": 2}], now=NOW)
    change_events.append_journal([{"at": "2026-11-12T00:00:00Z", "n": 3}], now=NOW)
    assert [e["n"] for e in change_events.read_journal(10)] == [2, 3]

def test_append_keeps_lines_without_a_date(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", "1")
    change_events.append_journal([{"n": 1}, {"at": "not a date", "n": 2}], now=NOW)
    assert [e["n"] for e in change_events.read_journal(10)] == [1, 2]

def test_zero_days_keeps_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", "0")
    change_events.append_journal([{"at": "2020-01-01T00:00:00Z", "n": 1}], now=NOW)
    assert len(change_events.read_journal(10)) == 1

@pytest.mark.parametrize("value", [None, "-5", "abc"])
def test_retention_defaults_to_365_days_and_10000_lines(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", raising=False)
    else:
        monkeypatch.setenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", value)
    assert change_events.retention_days() == 365
    assert change_events.JOURNAL_MAX_LINES == 10000
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_catalog_history.py` → `TypeError: append_journal() got an unexpected keyword argument 'now'` et `AttributeError: ... 'retention_days'`.
- [ ] **Step 3: Implement** `retention_days`, `_parse_at` (`datetime.fromisoformat`, `Z` accepté, sans fuseau = UTC) et la purge dans `append_journal` ; docstring du module : « kept for `PLUGBOARD_CHANGE_JOURNAL_DAYS` days (365), at most 10 000 lines ».
- [ ] **Step 4: Docs** — `configuration.md` : ligne `| \`PLUGBOARD_CHANGE_JOURNAL_DAYS\` | \`365\` | Days the change journal (\`data/catalog/changes.jsonl\`) keeps a change; \`0\` keeps them until the 10 000-line cap. |` ; `monitoring.md:119` : « … which keeps changes for `PLUGBOARD_CHANGE_JOURNAL_DAYS` days (365) and at most 10 000 lines; old lines are dropped when a new change is written. »
- [ ] **Step 5: Run, expect PASS** — `$PYTEST tests/test_catalog_history.py tests/test_auto_refresh.py` → tout passe (`test_journal_is_bounded` compris).
- [ ] **Step 6: Commit** — `feat(catalog): keep the change journal for 365 days`

### Task 2: Requête de l'historique des modèles

**Files:**
- Modify: `plugboard/change_events.py` (après `read_journal`)
- Test: `tests/test_catalog_history.py`

**Interfaces:**
- Consumes: `_parse_at` (Task 1).
- Produces: `query_model_history(*, provider: Optional[str] = None, since: Optional[date] = None, until: Optional[date] = None, model: Optional[str] = None, limit: int = 50, offset: int = 0) -> Dict[str, Any]` → `{"entries": List[{"at": str, "provider": str, "added": List[str], "removed": List[str]}], "total": int, "providers": List[str], "oldest": Optional[str]}`.
  - Lit tout le fichier ; garde les lignes JSON objet avec `event == "models.changed"` et `provider` chaîne non vide ; `added`/`removed` non listes → `[]`, éléments convertis en `str`.
  - `providers` (triés, uniques) et `oldest` (plus petite `at` lisible) portent sur toutes ces lignes, **sans** les filtres.
  - Filtres : `provider` égalité exacte ; `since` → `at >= since 00:00:00Z` ; `until` → `at < (until + 1 jour) 00:00:00Z` ; une `at` illisible est exclue dès qu'un filtre de date est donné ; `model` → sous-chaîne sans casse dans `added + removed`. L'entrée sort entière (Q6) : `added` et `removed` gardent tous leurs noms, ceux qui correspondent placés en tête (ordre d'origine conservé de part et d'autre). Sans `model`, ordre d'origine.
  - Pas de filtre d'âge à la lecture : la purge reste à l'écriture (Task 1).
  - Ordre : plus récent d'abord = ordre inverse du fichier. `total` = nombre après filtres ; `entries` = tranche `[offset : offset + limit]`.

- [ ] **Step 1: Write the failing tests** — fixture `journal(monkeypatch, tmp_path)` qui écrit, dans cet ordre, ces lignes (plus une ligne `not json` et `{"event": "models.changed"}`) dans `tmp_path / "catalog" / "changes.jsonl"` :

```python
LINES = [
    {"at": "2026-01-05T10:00:00Z", "event": "models.changed", "provider": "provider-a", "added": ["alpha-1"], "removed": []},
    {"at": "2026-03-01T00:00:00Z", "event": "pricing.changed", "provider": "provider-a", "model": "alpha-1"},
    {"at": "2026-06-30T23:59:59Z", "event": "models.changed", "provider": "provider-b", "added": "oops", "removed": ["Beta-Large"], "extra": "hidden"},
    {"at": "2026-07-01T00:00:00Z", "event": "models.changed", "provider": "old-provider", "added": ["gamma"], "removed": ["zeta", "alpha-1"]},
]

def test_history_lists_model_changes_newest_first(journal):
    out = change_events.query_model_history()
    assert [e["provider"] for e in out["entries"]] == ["old-provider", "provider-b", "provider-a"]
    assert out["total"] == 3
    assert out["providers"] == ["old-provider", "provider-a", "provider-b"]
    assert out["oldest"] == "2026-01-05T10:00:00Z"

def test_history_skips_bad_lines(journal):
    entries = change_events.query_model_history()["entries"]
    assert all(set(e) == {"at", "provider", "added", "removed"} for e in entries)
    assert entries[1] == {"at": "2026-06-30T23:59:59Z", "provider": "provider-b", "added": [], "removed": ["Beta-Large"]}

def test_history_filters_by_provider(journal):
    out = change_events.query_model_history(provider="provider-b")
    assert out["total"] == 1 and out["providers"] == ["old-provider", "provider-a", "provider-b"]

def test_history_until_is_inclusive(journal):
    out = change_events.query_model_history(until=date(2026, 6, 30))
    assert [e["provider"] for e in out["entries"]] == ["provider-b", "provider-a"]

def test_history_since(journal):
    assert [e["provider"] for e in change_events.query_model_history(since=date(2026, 7, 1))["entries"]] == ["old-provider"]

def test_history_filters_by_model_without_case(journal):
    assert [e["provider"] for e in change_events.query_model_history(model="beta")["entries"]] == ["provider-b"]
    assert [e["provider"] for e in change_events.query_model_history(model="ALPHA")["entries"]] == ["old-provider", "provider-a"]

def test_history_puts_matching_names_first(journal):
    assert change_events.query_model_history()["entries"][0]["removed"] == ["zeta", "alpha-1"]
    first = change_events.query_model_history(model="alpha")["entries"][0]
    assert first["removed"] == ["alpha-1", "zeta"] and first["added"] == ["gamma"]

def test_history_paginates(journal):
    out = change_events.query_model_history(limit=1, offset=1)
    assert [e["provider"] for e in out["entries"]] == ["provider-b"] and out["total"] == 3

def test_history_without_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    assert change_events.query_model_history() == {"entries": [], "total": 0, "providers": [], "oldest": None}
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_catalog_history.py -k history` → `AttributeError: ... 'query_model_history'`.
- [ ] **Step 3: Implement `query_model_history`** comme décrit dans Interfaces.
- [ ] **Step 4: Run, expect PASS** — `$PYTEST tests/test_catalog_history.py`.
- [ ] **Step 5: Commit** — `feat(catalog): query the model change history per provider and period`

### Task 3: Route `GET /admin/catalog/history`

**Files:**
- Modify: `plugboard/app.py` (juste après `auto_update_status`, `app.py:5355-5372`)
- Modify: `tests/test_security_tnr.py:244` (ajouter `("GET", "/admin/catalog/history")`), `tests/test_e2e_dashboard.py:97-108` (ajouter `"/admin/catalog/history?limit=50"`)
- Modify: `docs/guide/monitoring.md` (nouvelle section `## Catalog history` après « Automatic updates at a glance »), `CHANGELOG.md` (`## [Unreleased]` › `### Added`)
- Test: `tests/test_catalog_history.py`

**Interfaces:**
- Consumes: `query_model_history`, `retention_days`, `JOURNAL_MAX_LINES` (Tasks 1–2).
- Produces: `def admin_catalog_history(provider: Optional[str] = Query(None, max_length=200), since: Optional[date] = None, until: Optional[date] = None, model: Optional[str] = Query(None, max_length=200), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), _: None = Depends(_require_admin))` — **`def`, pas `async def`** (pool de threads). `since > until` → `HTTPException(422, "since is after until")`. Réponse : `{"entries", "total", "limit", "offset", "providers", "retention": {"days": retention_days(), "max_lines": JOURNAL_MAX_LINES, "oldest": ...}}`. Chaîne vide pour `provider` ou `model` = pas de filtre.

- [ ] **Step 1: Write the failing tests**

```python
ADMIN = {"Authorization": "Bearer adminkey"}
URL = "/admin/catalog/history"

async def test_history_route_requires_admin(admin_client, journal):
    assert (await admin_client.get(URL)).status_code == 401
    assert (await admin_client.get(URL, headers={"Authorization": "Bearer testkey"})).status_code == 401

async def test_history_route_returns_a_page(admin_client, journal):
    body = (await admin_client.get(URL + "?limit=1", headers=ADMIN)).json()
    assert set(body) == {"entries", "total", "limit", "offset", "providers", "retention"}
    assert body["total"] == 3 and body["limit"] == 1 and body["offset"] == 0
    assert [e["provider"] for e in body["entries"]] == ["old-provider"]
    assert body["retention"] == {"days": 365, "max_lines": 10000, "oldest": "2026-01-05T10:00:00Z"}

@pytest.mark.parametrize("query, providers", [
    ("provider=provider-b", ["provider-b"]),
    ("since=2026-07-01", ["old-provider"]),
    ("until=2026-06-30", ["provider-b", "provider-a"]),
    ("model=beta", ["provider-b"]),
    ("provider=&model=", ["old-provider", "provider-b", "provider-a"]),
])
async def test_history_route_passes_filters(admin_client, journal, query, providers):
    body = (await admin_client.get(f"{URL}?{query}", headers=ADMIN)).json()
    assert [e["provider"] for e in body["entries"]] == providers

@pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1", "since=2026-13-01",
                                   "until=yesterday", "since=2026-07-02&until=2026-07-01"])
async def test_history_route_rejects_bad_parameters(admin_client, journal, query):
    assert (await admin_client.get(f"{URL}?{query}", headers=ADMIN)).status_code == 422

def test_history_handler_runs_off_the_event_loop():
    from plugboard import app as app_module
    assert not inspect.iscoroutinefunction(app_module.admin_catalog_history)
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_catalog_history.py -k route` → `404` au lieu de `401`/`200`.
- [ ] **Step 3: Implement the route** (signature ci-dessus ; `date` importé de `datetime`).
- [ ] **Step 4: Docs** — `monitoring.md` : section `## Catalog history` : à quoi elle sert, l'exemple `curl -s "http://localhost:8765/admin/catalog/history?provider=<id>&since=2026-11-01&limit=50" -H "Authorization: Bearer $ADMIN_KEY"`, les paramètres, la forme de la réponse, la conservation (lien vers la variable), « first model list of a provider is not recorded ». `CHANGELOG.md` : une entrée **Catalog history per provider** (route, filtres, conservation 365 jours, `PLUGBOARD_CHANGE_JOURNAL_DAYS`, plafond 10 000) ; l'onglet sera ajouté à la même entrée en Task 4.
- [ ] **Step 5: Run, expect PASS** — `$PYTEST tests/test_catalog_history.py tests/test_security_tnr.py tests/test_e2e_dashboard.py tests/test_spec_v7_residuals_parent.py`.
- [ ] **Step 6: Commit** — `feat(catalog): admin route for the per-provider model history`

### Task 4: Onglet « Catalog history » du tableau de bord

**Files:**
- Create: `static/catalog_history.js`
- Modify: `static/index.html` (symbole `i-history` après `i-clock` `:26` ; lien de nav après Monitoring `:92-94` ; section `page-catalog-history` après `page-monitoring` ; lien `Full history` dans le titre « Recent automatic changes » `:479` ; `<script src="/dashboard/catalog_history.js?v=1">` après `auto_update.js` ; `app.js?v=22` → `v=23`)
- Modify: `static/app.js:151` (`TAB_LOADERS` : `"catalog-history": loadCatalogHistory`) et, près de `loadPricing` (`:1244`), `function loadCatalogHistory()` qui appelle `window.loadCatalogHistoryPage` s'il existe
- Modify: `static/style.css` (bloc `ch-` en fin de fichier), `docs/guide/monitoring.md:31-40` (ligne `Catalog history` du tableau des pages), `CHANGELOG.md` (même entrée)
- Test: `tests/test_catalog_history.py`

**Interfaces:**
- Consumes: la route de Task 3 ; helpers globaux `h`, `api`, `emptyState`, `skeleton` de `app.js`.
- Produces: `window.loadCatalogHistoryPage()` (recharge depuis `offset=0` avec les filtres en cours).

Comportement (décisions du plan, libellés dans Global Constraints) :
- Section : `topbar` (`<h2>Catalog history</h2>`), `div.ch-filters` avec `select#ch-provider`, `select#ch-period` (valeurs `""`, `7`, `30`, `90`, `365` ; défaut `""`), `input#ch-model` `type="search"` ; `div#ch-list` ; `button#ch-more` ; `p#ch-footer`.
- Période N → `since` = date UTC de `maintenant − N jours` (`toISOString().slice(0, 10)`). Changer un filtre repart de `offset=0` ; `#ch-model` attend 400 ms après la dernière frappe ; une requête à la fois (la réponse d'une requête périmée est ignorée).
- `#ch-provider` est reconstruit à chaque réponse depuis `providers`, en gardant le choix courant.
- Carte `div.ch-entry` : `YYYY-MM-DD HH:MM UTC` et `<code>` du fournisseur ; puis, s'il y en a, badge vert `+N added` et badge rouge `−N removed` (classes `badge-green` / `badge-red`), chacun suivi des 10 premiers noms en `<code>`, le reste dans `<details><summary>+M more</summary>`.
- Filtre Model posé : chaque nom qui contient le texte (sans casse) reçoit la classe `ch-match` (fond `var(--accent-bg)`, texte `var(--accent)`) ; la route les a déjà mis en tête.
- `Load more (K left)` visible tant que `offset + affichés < total` ; il ajoute 50 entrées sous les autres.
- `total == 0` : `No model change matches these filters.` si un filtre est posé, sinon `No model change recorded yet.` (via `emptyState`). Réponse en erreur : `div.alert.alert-error` avec `Catalog history unavailable: <message>`.
- Pied : `Kept for 365 days, at most 10,000 changes · oldest: 2026-01-05` ; `days == 0` → `Kept until the 10,000-change cap`.
- CSS : `.ch-filters` en flex `wrap`, enfants `flex: 1 1 100%` sous 768 px et `0 1 auto` au-delà ; contrôles et bouton `min-height: 44px` ; `.ch-entry code { overflow-wrap: anywhere; }`.

- [ ] **Step 1: Write the failing tests**

```python
STATIC = Path(__file__).resolve().parent.parent / "static"

async def test_dashboard_has_the_catalog_history_tab(admin_client):
    await admin_client.post("/auth/admin/login", json={"key": "adminkey"})
    shell = (await admin_client.get("/dashboard")).text
    assert 'data-tab="catalog-history"' in shell and 'id="page-catalog-history"' in shell
    assert shell.count('href="#catalog-history"') >= 2  # nav + Monitoring "Full history"
    assert "/dashboard/catalog_history.js" in shell
    js = await admin_client.get("/dashboard/catalog_history.js")
    assert js.status_code == 200 and "/admin/catalog/history" in js.text

def test_app_js_routes_the_tab():
    assert '"catalog-history": loadCatalogHistory' in (STATIC / "app.js").read_text()

def test_catalog_history_js_writes_text_only():
    js = (STATIC / "catalog_history.js").read_text()
    assert re.findall(r'innerHTML\s*=\s*(?!"")', js) == []
    assert "insertAdjacentHTML" not in js and "outerHTML" not in js
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_catalog_history.py -k "tab or js"`.
- [ ] **Step 3: Implement** `index.html`, `app.js`, `catalog_history.js`, bloc CSS, doc et CHANGELOG.
- [ ] **Step 4: Run, expect PASS** — `$PYTEST tests/test_catalog_history.py tests/test_e2e_dashboard.py` ; `node --check static/catalog_history.js` si `node` est présent (sinon le dire).
- [ ] **Step 5: Commit** — `feat(dashboard): catalog history tab with provider, period and model filters`

### Task 5: Contrôle dans un vrai navigateur

**Files:**
- Scratch seulement : `$SCR/seed_history.py`, `$SCR/data/`, captures (`SCR=/tmp/claude-1001/-srv-dev-plugboard/a5d91e11-1edb-4748-99e2-79712c823769/scratchpad/catalog-history`)
- Modify si défaut : fichiers de Task 4 (test rouge d'abord quand le défaut se teste en pytest)
- Modify: `TODOS.md` (fiche « Catalog changelog view » déplacée sous `## Completed` avec une ligne **Done:**)

- [ ] **Step 1: Seed** — `seed_history.py` écrit `$SCR/data/catalog/changes.jsonl` : 120 lignes `models.changed` fictives sur 400 jours (`provider-a`, `provider-b`, `old-provider`), dont une à 40 noms retirés (le 35e : `needle-model-9`), une avec un nom de 120 caractères, une avec `<img src=x onerror=alert(1)>`, et 10 lignes `pricing.changed` intercalées.
- [ ] **Step 2: Start** (arrière-plan, sans `make dev` qui recharge et ouvre un navigateur) :
  `cd <atelier> && HOST=127.0.0.1 PORT=18801 PLUGBOARD_DATA=$SCR/data PLUGBOARD_SKIP_DOTENV=1 ADMIN_KEY=$(openssl rand -hex 24) API_KEY=$(openssl rand -hex 24) PRICING_SCHEDULER_DISABLED=1 MODEL_AVAILABILITY_SCHEDULER_DISABLED=1 PLUGBOARD_CATALOG=off OLLAMA_ENABLED=false RELOAD=false /srv/dev/plugboard/.venv/bin/python run.py`
  Garder la clé admin dans une variable du shell, jamais dans un fichier ni un rapport. Attendu : `curl -s http://127.0.0.1:18801/health` → `"ok": true`.
- [ ] **Step 3: Check at 390 × 844** (MCP playwright) : ouvrir `http://127.0.0.1:18801/` (connexion automatique locale : `HOST=127.0.0.1` + `run.py`, sans saisir la clé ; voir `docs/guide/troubleshooting.md:48`), puis `#catalog-history`. Attendu : 50 cartes, `Load more (70 left)` ; `document.documentElement.scrollWidth <= innerWidth` ; aucune balise `img` dans `#page-catalog-history` et le nom hostile visible en texte ; carte à 40 noms repliée `+30 more` ; Model `needle` : `needle-model-9` premier nom visible, surligné (`ch-match`), badge toujours `−40 removed` ; filtres Provider, `Last 30 days`, Model `alpha` donnent les bons comptes ; `Full history` depuis Monitoring ouvre l'onglet ; capture.
- [ ] **Step 4: Check at 1280 × 800** : filtres sur une ligne, capture ; console sans erreur (`browser_console_messages`).
- [ ] **Step 5: Stop** l'instance (kill du PID), vérifier que le port 18801 est libre.
- [ ] **Step 6: TODOS.md** — fiche déplacée sous `## Completed`, ligne **Done:** (route, onglet, 365 jours).
- [ ] **Step 7: Commit** — `docs(todos): mark the catalog changelog view done` (plus un `fix(dashboard): …` par défaut corrigé).

### Task 6: Vérification finale

- [ ] **Step 1: Tests ciblés, un à la fois** — `$PYTEST tests/test_catalog_history.py tests/test_auto_refresh.py tests/test_off_loop_io.py` puis `$PYTEST tests/test_e2e_dashboard.py tests/test_security_tnr.py tests/test_spec_v7_residuals_parent.py tests/test_admin_csrf_same_site.py tests/test_monitoring.py`. Attendu : 0 échec.
- [ ] **Step 2: Lint** — `/srv/dev/plugboard/.venv/bin/ruff check plugboard/ tests/` et `/srv/dev/plugboard/.venv/bin/ruff format --check plugboard/ tests/` → propres.
- [ ] **Step 3: Secrets** — `make secret-scan` → `no leaks found`.
- [ ] **Step 4: Nettoyage** — supprimer `$SCR`, `.ruff_cache`, `.playwright-mcp` éventuel ; `git status --porcelain` vide.
- [ ] **Step 5: Rapport** — branche, SHA de tête, commits, sorties réelles des étapes 1 à 3, captures décrites, décisions prises seul, points non vérifiés.

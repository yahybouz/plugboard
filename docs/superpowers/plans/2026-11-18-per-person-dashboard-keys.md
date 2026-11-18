# Clés de tableau de bord par personne (PB-AUTH-01) — plan d'exécution

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Chaque personne reçoit sa propre clé de tableau de bord (`pba_…`) et son propre secret TOTP, nommés, révocables un par un et nommés dans l'audit ; `ADMIN_KEY` reste la clé de secours, inchangée.

**Architecture:** Un magasin JSON `data/admin_people.json` (`plugboard/admin_people.py`) à côté de `admin_mfa` et `admin_sessions`. L'auth admin de `plugboard/app.py` teste la session, puis `ADMIN_KEY`, puis une clé personnelle qui exige toujours un code de **son** secret ; les sessions enregistrent la personne et sa `generation`. Les routes `/admin/people` vivent dans un routeur à part (`plugboard/admin_people_api.py`, modèle `suivi_api`). L'identité de l'auteur (« principal ») voyage dans un `ContextVar` posé par requête et s'écrit dans deux nouvelles colonnes de l'audit.

**Tech Stack:** Python 3.12, FastAPI/Starlette, httpx `ASGITransport`, pytest-asyncio (`asyncio_mode = auto`), SQLite (audit), Fernet (`plugboard.config._encrypt_token`), JS sans framework (`static/`).

**Spec:** `docs/superpowers/specs/2026-11-18-per-person-dashboard-keys-design.md` (D-63, fait foi).

## Choix du constructeur

Les « Builder's call » de la conception, et les points que le code impose de trancher :

1. **Clé en attente : n'expire pas.** Elle reste `pending` jusqu'à son premier code accepté (connexion ou en-tête), et tout admin peut la révoquer. Pas de quatrième statut : la liste garde `pending`, `active`, `revoked`.
2. **Où voyage l'acteur, et colonnes d'audit.** La colonne `actor` existante garde son sens (la surface : `admin`, `mcp`, `system`) : `tests/test_provider_audit_actor.py` et `tests/test_mcp_admin_tools.py` attendent `admin`/`mcp` et ne doivent pas changer. L'acteur de la conception s'écrit dans deux colonnes ajoutées : `principal` (`admin-key` ou `person:<id>`) et `principal_name` (nom de la personne, figé à l'écriture : l'audit se lit seul, même si le magasin est perdu). Transport : `request.state.admin_principal` pour les routes, et un `ContextVar` (un dict par requête, posé par `_RequestIdMiddleware`, rempli par l'auth, lu par `audit.log_action` quand `principal` n'est pas passé). Vérifié : le dict est partagé avec le pool de threads (`run_in_threadpool`), donc les routes `def` et les dépendances `def` le voient.
3. **Entrées existantes sans acteur.** Migration `ALTER TABLE … ADD COLUMN … DEFAULT ''` : elles se lisent `principal: ""`, comme toute entrée écrite hors d'une requête admin authentifiée (système, clé inconnue). La doc dit « vide = non enregistré ».
4. **QR : aucun.** Pas de nouvelle dépendance ni de CDN (seuls `chartjs` et `three` sont dans `static/vendor`) : la clé, le secret et l'URI `otpauth://` sont affichés en texte, chacun avec **Copy**, puis un bouton « I have stored them » efface les trois valeurs de la page.
5. **Plan client (`/v1/*`) : une clé personnelle n'y vaut jamais** (contrairement à `ADMIN_KEY`, inchangé). Rien à coder (`_resolve_client` ne connaît que `ADMIN_KEY` et les clés client), un test l'épingle.
6. **`/health/ready` et limite globale.** Une clé `pba_` valide sans code est comptée sous `key:admin:<id>` pour la limite globale (BUG-119 : une clé valide a son propre compteur), mais n'affiche pas `checks` (corps minimal, comme sans clé : anti-objectif « aucune clé personnelle ne fonctionne sans son code »). Une session ouverte par une personne affiche `checks` et compte sous `key:admin:<id>`.
7. **Verrou par adresse.** Une clé personnelle valide passe le verrou de l'adresse, comme `ADMIN_KEY` (BUG-119 : derrière le proxy, n'importe qui verrouillerait tout le monde) ; ses mauvais codes comptent sous `key:admin:<id>` seulement. Une clé `pba_` inconnue compte contre l'adresse à la connexion et est seulement refusée sur l'en-tête, comme une mauvaise `ADMIN_KEY` aujourd'hui.
8. **Routes `/admin/mfa` (begin, setup, verify, DELETE) refusées en 403 à une personne** ; `GET /admin/mfa/status` reste permis. Elles gèrent le second facteur de `ADMIN_KEY` : sans ce refus, les mauvais codes de pas-à-pas d'une personne verrouilleraient `key:admin` (anti-objectif), et une personne pourrait enrôler le facteur de la clé de secours. Ce n'est pas un rôle : aucune autre route n'est restreinte.
9. **Sessions des personnes et MFA globale.** Elles gardent la vérification de `mfa_generation` : enrôler, tourner ou retirer la MFA de `ADMIN_KEY`, ou « Log out everywhere », coupe aussi les sessions des personnes (« toutes les sessions admin », comportement documenté).
10. **Noms.** Espaces de bord retirés ; 1 à 64 caractères ; `str.isprintable()` ; uniques sans tenir compte de la casse (`casefold`) parmi les clés non révoquées ; le nom d'une clé révoquée peut resservir.
11. **`last_used_at`** est mis à jour quand un code est accepté (connexion ou en-tête), pas à chaque requête de session (pas d'écriture disque par clic).
12. **`POST /admin/people` refuse en 503 sans `ADMIN_KEY`**, même auth entièrement coupée : une clé créée là ne pourrait jamais servir (les sessions sont signées avec `ADMIN_KEY`).
13. **Contrat d'API.** Les routes admin sont hors du contrat client (`docs/api/README.md` l.12, `tests/test_api_contract.py`). Les routes `/admin/people` sont documentées par le `/openapi.json` admin ; `CONTRACT_VERSION` reste `3.0.1`, aucun export régénéré (décision de l'orchestrateur du 18/11, la conception est corrigée dans le même sens).
14. **Rotation de `ENCRYPTION_KEY`** (`plugboard/secrets_rekey.py`) re-chiffre aussi `admin_people.json` : sinon une rotation rendrait tous les codes personnels illisibles.
15. **Journaux.** `plugboard/log_redact.py` masque les clés `pba_…` (le motif `secret=` couvre déjà l'URI `otpauth://`, vérifié).
16. **Fichiers.** Magasin `plugboard/admin_people.py`, routes `plugboard/admin_people_api.py`, page `static/people.js` (modèle `catalog_history.js`). Champ `confirmed_at` ajouté à l'enregistrement (date de confirmation, affichée).
17. **Révocation idempotente** : `200` et la vue à chaque appel ; audit `admin.person.revoked` au premier seulement. Identifiant hors `^ap_[0-9a-f]{12}$` → `404` sans lecture.

## Global Constraints

- Atelier : `/srv/dev/worktrees/plugboard/pbauth01`, branche `feat/pb-auth-01-per-person-keys`. Rien hors de l'atelier, aucune autre branche. Interdits : `git commit --amend`, rebase, reset, push. Un commit par tâche (Conventional Commits, lignes d'attribution de la session).
- Tests ciblés seulement (règle YB du 18/11) : fichiers de la tâche et voisins nommés. Jamais `tests/` entier ni la CI dans une tâche. Commande notée `$PYTEST` :
  `cd /srv/dev/worktrees/plugboard/pbauth01 && PLUGBOARD_SKIP_DOTENV=1 /srv/dev/plugboard/.venv/bin/python -m pytest -q -p no:cacheprovider <fichiers>`
- Lint de chaque tâche, sur ses fichiers : `/srv/dev/plugboard/.venv/bin/ruff check <fichiers>` et `/srv/dev/plugboard/.venv/bin/ruff format --check <fichiers>`.
- Tests existants : **aucun modifié**, sauf l'ajout de `"people"` à `DASHBOARD_TABS` (`tests/e2e/conftest.py`, tâche 15). Ils doivent rester verts.
- Valeurs fixes (de la conception, recopiées) :
  - clé brute `pba_` + `secrets.token_urlsafe(32)` ; `key_prefix` = ses 10 premiers caractères ; `key_hash` = SHA-256 hex, comparé par `hmac.compare_digest` ;
  - identifiant `ap_` + 12 hex (`secrets.token_hex(6)`) ; `generation` = `secrets.token_hex(16)` ;
  - fichier `data/admin_people.json` écrit par `plugboard.storage.atomic_write_json(..., mode=0o600)`, forme `{"people": [ … ]}` ;
  - secret chiffré comme `admin_mfa` (`_encrypt_token`, préfixe `enc:` exigé à la lecture) ; URI `admin_mfa.provisioning_uri(secret, account=<nom>)` (issuer `Plugboard`) ;
  - en-tête `X-Plugboard-Admin-TOTP` ; portée de verrou `key:admin:<id>` ;
  - principals `admin-key` et `person:<id>` ;
  - actions d'audit `admin.person.created`, `admin.person.confirmed`, `admin.person.revoked`, `auth.person.mfa_failed` (`target_type="admin_person"`, `target_id=<id>`).
- Messages d'erreur exacts (anglais) :
  - 422 : `Name must be 1 to 64 printable characters`
  - 409 : `A live personal key already has this name`
  - 404 : `Personal key not found`
  - 503 magasin : `The personal key store cannot be read or written: see the server log`
  - 503 sans `ADMIN_KEY` : `Personal keys need ADMIN_KEY`
  - 403 MFA : `The second factor of ADMIN_KEY is managed with ADMIN_KEY`
- Aucune clé, aucun secret, aucun hash dans une réponse (hors la création), un journal, l'audit, le magasin en clair. Fichier illisible : erreur journalisée par son **type** seulement, jamais un 500.
- Données de test synthétiques uniquement : noms `person-one`, `person-two`, `Zoé Été` ; clients `acme-test` ; clés et secrets générés par le code testé. Aucun nom réel, aucune clé réelle ; aucune clé créée sur l'UAT ni en production.
- Ne pas toucher `VERSION` ni `plugboard/__init__.py`. Doc en anglais ; CHANGELOG sous `## [Unreleased]`.
- Nettoyage : tout dossier temporaire créé par une commande (`mktemp -d`) est supprimé dans la même commande.

## Review Focus

1. **Variantes d'un même nom** — `" Person-One "` puis `"person-one"` : 409 ; `"two\nlines"`, 65 caractères : 422 ; `"Zoé Été"` accepté. Tests à la tâche 2.
2. **Une personne se révoque elle-même depuis sa session** — `DELETE` répond 200, la requête suivante avec le même cookie reçoit 401 (le tableau de bord renvoie alors vers `/login`). Test à la tâche 10.
3. **Magasin supprimé ou corrompu pendant des sessions ouvertes** — sessions et clés des personnes refusées (401, jamais 500), session et clé `ADMIN_KEY` toujours acceptées, rien du contenu du fichier dans le journal. Test à la tâche 8.
4. **Rotation de `ENCRYPTION_KEY`** — après `scripts/rotate_encryption_key.py`, les secrets TOTP personnels se déchiffrent avec la nouvelle clé ; un secret illisible arrête la rotation. Tests à la tâche 12.
5. **Même code réutilisé** — un code accepté à la connexion est refusé sur l'en-tête au même pas ; une autre personne, au même pas, se connecte avec son propre code. Test à la tâche 8.

---

### Task 1: `admin_mfa.matching_step`, le calcul TOTP partagé

**Files:**
- Modify: `plugboard/admin_mfa.py` (ajout après `setup_code_step`, ~l.218 ; `verify` ne change pas)
- Create: `tests/test_admin_mfa_matching_step.py`

**Interfaces:**
- Consumes: `_b32_normalize`, `_hotp`, `_DIGITS`, `_PERIOD`, `_WINDOW` (même module).
- Produces: `matching_step(secret: str, code: str, at: Optional[float] = None) -> Optional[int]` — le pas (dans ±`_WINDOW`) dont le code est `code`, sinon `None` (code mal formé, secret non base32 : `None`, sans exception ni journal).

- [ ] **Step 1: Write the failing test**

```python
"""admin_mfa.matching_step: the TOTP step a code belongs to (PB-AUTH-01)."""

import pytest

from plugboard import admin_mfa

SECRET = admin_mfa.generate_secret()
T = 1_800_000_000.0


def test_matching_step_finds_the_current_step():
    code = admin_mfa._generate_code(SECRET, at=T)
    assert admin_mfa.matching_step(SECRET, code, at=T) == int(T // 30)


def test_matching_step_tolerates_one_step_of_skew():
    previous = admin_mfa._generate_code(SECRET, at=T - 30)
    assert admin_mfa.matching_step(SECRET, previous, at=T) == int(T // 30) - 1


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", " 12345"])
def test_matching_step_refuses_malformed_codes(code):
    assert admin_mfa.matching_step(SECRET, code, at=T) is None


def test_matching_step_refuses_an_old_code():
    old = admin_mfa._generate_code(SECRET, at=T - 90)
    assert admin_mfa.matching_step(SECRET, old, at=T) is None


def test_matching_step_refuses_a_secret_that_is_not_base32():
    assert admin_mfa.matching_step("not base32 !", "123456", at=T) is None
```

- [ ] **Step 2: Run it to see it fail**

Run: `$PYTEST tests/test_admin_mfa_matching_step.py`
Expected: FAIL, `AttributeError: module 'plugboard.admin_mfa' has no attribute 'matching_step'`.

- [ ] **Step 3: Implement `matching_step` in `plugboard/admin_mfa.py`**

Même boucle que `verify` (pas `t_now - _WINDOW` à `t_now + _WINDOW`, `hmac.compare_digest`), sans `_consume_step`. Ne pas réécrire `verify` (comportement de `ADMIN_KEY` intouché). Mettre la fonction dans la liste « Public surface » de la docstring du module.

- [ ] **Step 4: Run the tests and the MFA neighbours**

Run: `$PYTEST tests/test_admin_mfa_matching_step.py tests/test_inherited_auth_v7.py tests/test_auth_residuals_v7b.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugboard/admin_mfa.py tests/test_admin_mfa_matching_step.py
git commit -m "feat(auth): expose the TOTP step a code matches (PB-AUTH-01)"
```

---

### Task 2: Magasin `admin_people` — création, liste, noms, fichier illisible

**Files:**
- Create: `plugboard/admin_people.py`
- Create: `tests/people_helpers.py`
- Create: `tests/test_admin_people_store.py`

**Interfaces:**
- Consumes: `admin_mfa.generate_secret()`, `admin_mfa.provisioning_uri(secret, account=name)`, `config._encrypt_token`, `storage.atomic_write_json`, `storage.data_dir`.
- Produces (module `plugboard.admin_people`, `import time` au niveau module, `time.time()` réservé aux pas TOTP — les tests le remplacent) :
  - `KEY_PREFIX = "pba_"`, `NAME_MAX = 64`, `ID_PATTERN = re.compile(r"ap_[0-9a-f]{12}")`
  - `VIEW_FIELDS = frozenset({"id", "name", "key_prefix", "status", "created_at", "created_by", "confirmed_at", "last_used_at", "revoked_at", "revoked_by"})`
  - `class PersonError(ValueError)` avec l'attribut `code` (`"invalid_name"` | `"name_taken"`) ; message = texte 422/409 des contraintes globales.
  - `class PeopleStoreUnreadable(RuntimeError)`
  - `_path() -> Path` (= `data_dir() / "admin_people.json"`, appelé à chaque accès), `_load() -> tuple[list[dict], bool]` (liste, lisible), `_save(people: list[dict]) -> None`, `_LOCK = threading.Lock()`
  - `create(name: str, *, created_by: str) -> dict` → `{"person": <vue>, "key": <brute>, "totp_secret": <base32>, "otpauth_uri": <uri>}`
  - `list_people() -> list[dict]` (vues, ordre de création ; lève `PeopleStoreUnreadable`)
  - `public_view(record: dict) -> dict` (exactement `VIEW_FIELDS`), `status_of(record: dict) -> str` (`revoked` si `revoked_at`, `active` si `totp_confirmed`, sinon `pending`)
  - Enregistrement stocké : `id, name, key_hash, key_prefix, totp_secret_enc, totp_confirmed (False), last_step (None), generation, created_at, created_by, confirmed_at, last_used_at, revoked_at, revoked_by` ; dates `datetime.now(timezone.utc).isoformat(timespec="seconds")`.
  - `tests/people_helpers.py` : `isolate`, `new_person`, `code_now`, `wrong`, `next_step`, `bearer`, `login`, `keys_status`, constantes `TOTP`, `ADMIN`, `COOKIE` (code ci-dessous, utilisé par toutes les tâches suivantes).

- [ ] **Step 1: Write the helpers and the failing tests**

`tests/people_helpers.py` (complet : infrastructure partagée) :

```python
"""Isolation and helpers for the personal dashboard key tests (PB-AUTH-01).

Synthetic values only: names such as ``person-one``; keys and TOTP secrets
are generated by the code under test. ``isolate`` is a generator: each test
module wraps it in its own fixture (no imported fixture, no lint F811).
"""

from __future__ import annotations

import types
from typing import Optional

TOTP = "X-Plugboard-Admin-TOTP"
ADMIN = {"Authorization": "Bearer adminkey"}
COOKIE = "plugboard_admin_session"


def isolate(tmp_path, monkeypatch):
    """People, sessions, admin MFA and audit on private files; a fixed TOTP clock."""
    from plugboard import admin_mfa, admin_people, admin_sessions, audit, ratelimit

    clock = types.SimpleNamespace(t=1_800_000_000.0)
    store = tmp_path / "admin_people.json"
    monkeypatch.setattr(admin_people, "_path", lambda: store)
    monkeypatch.setattr(
        admin_people, "time", types.SimpleNamespace(time=lambda: clock.t)
    )
    monkeypatch.setattr(
        admin_sessions, "_path", lambda: tmp_path / "admin_sessions.json"
    )
    monkeypatch.setattr(admin_mfa, "_MFA_FILE", tmp_path / "admin_mfa.json")
    monkeypatch.setattr(audit, "_DB_PATH", tmp_path / "audit.db")
    monkeypatch.delenv("PLUGBOARD_ADMIN_MFA_REQUIRED", raising=False)
    tables = (ratelimit._auth_buckets, ratelimit._lockouts)
    for table in tables:
        table.clear()
    yield types.SimpleNamespace(dir=tmp_path, clock=clock, store=store)
    for table in tables:
        table.clear()


def new_person(name: str = "person-one") -> dict:
    from plugboard import admin_people

    return admin_people.create(name, created_by="admin-key")


def code_now(env, made: dict) -> str:
    from plugboard import admin_mfa

    return admin_mfa._generate_code(made["totp_secret"], at=env.clock.t)


def wrong(code: str) -> str:
    return str((int(code) + 1) % 1_000_000).zfill(6)


def next_step(env) -> None:
    env.clock.t += 30


def bearer(made: dict, code: Optional[str] = None) -> dict:
    headers = {"Authorization": f"Bearer {made['key']}"}
    if code:
        headers[TOTP] = code
    return headers


async def login(client, key: str, totp: Optional[str] = None) -> dict:
    client.cookies.clear()
    body = {"key": key} if totp is None else {"key": key, "totp_code": totp}
    resp = await client.post("/auth/admin/login", json=body)
    assert resp.status_code == 204, resp.text
    client.cookies.clear()
    value = resp.headers["set-cookie"].split(f"{COOKIE}=", 1)[1].split(";", 1)[0]
    return {"Cookie": f"{COOKIE}={value}"}


async def keys_status(client, headers: dict) -> int:
    client.cookies.clear()
    return (await client.get("/admin/keys", headers=headers)).status_code
```

`tests/test_admin_people_store.py` :

```python
"""The personal dashboard key store (PB-AUTH-01)."""

import hashlib
import json
import os
import re
import stat

import pytest

from plugboard import admin_people
from tests import people_helpers as ph


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield from ph.isolate(tmp_path, monkeypatch)


def test_create_returns_the_key_and_the_secret_once(env):
    made = ph.new_person()
    assert made["key"].startswith("pba_") and len(made["key"]) >= 40
    assert made["otpauth_uri"].startswith(
        "otpauth://totp/Plugboard%3Aperson-one?secret=" + made["totp_secret"]
    )
    assert "issuer=Plugboard" in made["otpauth_uri"]
    view = made["person"]
    assert set(view) == admin_people.VIEW_FIELDS
    assert re.fullmatch(r"ap_[0-9a-f]{12}", view["id"])
    assert (view["name"], view["status"], view["created_by"]) == (
        "person-one", "pending", "admin-key",
    )
    assert view["key_prefix"] == made["key"][:10]


def test_the_store_holds_neither_the_key_nor_the_secret(env):
    made = ph.new_person()
    text = env.store.read_text()
    assert made["key"] not in text and made["totp_secret"] not in text
    record = json.loads(text)["people"][0]
    assert record["key_hash"] == hashlib.sha256(made["key"].encode()).hexdigest()
    assert record["totp_secret_enc"].startswith("enc:")
    assert stat.S_IMODE(os.stat(env.store).st_mode) == 0o600


def test_the_list_never_shows_a_hash_or_a_secret(env):
    made = ph.new_person()
    listed = admin_people.list_people()
    assert [p["id"] for p in listed] == [made["person"]["id"]]
    assert all(set(p) == admin_people.VIEW_FIELDS for p in listed)
    dumped = json.dumps(listed)
    assert "key_hash" not in dumped and "totp_secret" not in dumped
    assert made["key"] not in dumped and made["totp_secret"] not in dumped


@pytest.mark.parametrize(
    "name", ["", "   ", "x" * 65, "two\nlines", "tab\there", "nul\x00"]
)
def test_invalid_names_are_refused(env, name):
    with pytest.raises(admin_people.PersonError) as err:
        admin_people.create(name, created_by="admin-key")
    assert err.value.code == "invalid_name"
    assert str(err.value) == "Name must be 1 to 64 printable characters"
    assert not env.store.exists()


def test_a_name_is_trimmed_and_unique_whatever_its_case(env):
    admin_people.create("  Person-One ", created_by="admin-key")
    with pytest.raises(admin_people.PersonError) as err:
        admin_people.create("person-one", created_by="admin-key")
    assert err.value.code == "name_taken"
    assert [p["name"] for p in admin_people.list_people()] == ["Person-One"]


def test_printable_unicode_names_are_accepted(env):
    assert ph.new_person("Zoé Été")["person"]["name"] == "Zoé Été"


@pytest.mark.parametrize("content", ["{not json", "[]", '{"people": "x"}'])
def test_an_unreadable_store_refuses_create_and_list(env, content, caplog):
    env.store.write_text(content)
    with pytest.raises(admin_people.PeopleStoreUnreadable):
        admin_people.list_people()
    with pytest.raises(admin_people.PeopleStoreUnreadable):
        admin_people.create("person-one", created_by="admin-key")
    assert env.store.read_text() == content
    assert "not json" not in caplog.text
```

- [ ] **Step 2: Run it to see it fail**

Run: `$PYTEST tests/test_admin_people_store.py`
Expected: FAIL à la collecte, `ImportError: cannot import name 'admin_people' from 'plugboard'`.

- [ ] **Step 3: Implement the store in `plugboard/admin_people.py`**

Docstring de module : rôle, chemin, forme, règle « un fichier illisible refuse toute clé personnelle ». `_load` : fichier absent → `([], True)` ; `OSError`/`ValueError` → `log.error("Failed to read %s: %s", path, type(exc).__name__)` et `([], False)` ; racine non dict ou `people` non liste → `log.error(... "not a list of people")` et `([], False)` ; sinon les éléments dict. `create` : valider le nom (après `strip`) avant toute lecture, puis sous `_LOCK` : `_load` (illisible → `PeopleStoreUnreadable`), unicité `casefold` parmi `revoked_at is None`, identifiant unique, `_save`. `list_people` : illisible → `PeopleStoreUnreadable`.

- [ ] **Step 4: Run the tests**

Run: `$PYTEST tests/test_admin_people_store.py tests/test_storage.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugboard/admin_people.py tests/people_helpers.py tests/test_admin_people_store.py
git commit -m "feat(auth): store named personal dashboard keys (PB-AUTH-01)"
```

---

### Task 3: Retrouver une clé et vérifier le code de **sa** personne

**Files:**
- Modify: `plugboard/admin_people.py`
- Test: `tests/test_admin_people_store.py` (ajout)

**Interfaces:**
- Consumes: `admin_mfa.matching_step` (tâche 1), `config._decrypt_token`, `config.SecretUnreadable`.
- Produces:
  - `find_by_key(raw_key: str) -> Optional[dict]` — l'enregistrement **non révoqué** dont `key_hash` correspond (comparaison `hmac.compare_digest` sur chaque enregistrement, sans sortie anticipée) ; `None` si préfixe ≠ `pba_`, inconnue, révoquée ou magasin illisible.
  - `get(person_id: str) -> Optional[dict]` — l'enregistrement brut (révoqué compris) ; `None` si absent, identifiant hors `ID_PATTERN` ou magasin illisible.
  - `verify_code(person_id: str, code: str) -> str` — `"confirmed"` (premier code accepté : `totp_confirmed=True`, `confirmed_at`), `"ok"`, ou `"refused"` (code mal formé, faux, rejoué ou d'un pas antérieur au dernier accepté, personne révoquée ou absente, magasin illisible, secret sans préfixe `enc:` ou indéchiffrable, échec d'écriture). Sous `_LOCK` ; pas = `admin_mfa.matching_step(secret, code, at=time.time())` ; refus si `step <= last_step` (`log.warning("Personal TOTP code replayed ...")` avec l'identifiant, jamais le code) ; enregistre `last_step` et `last_used_at` **avant** de répondre ; une exception d'écriture → `log.error(type)` et `"refused"`.

- [ ] **Step 1: Write the failing tests** (à la suite de `tests/test_admin_people_store.py`)

```python
def test_find_by_key_returns_the_live_record_only(env):
    made = ph.new_person()
    assert admin_people.find_by_key(made["key"])["id"] == made["person"]["id"]
    assert admin_people.find_by_key(made["key"] + "x") is None
    assert admin_people.find_by_key("adminkey") is None


def test_the_first_code_confirms_then_each_code_is_single_use(env):
    made = ph.new_person()
    pid, first = made["person"]["id"], ph.code_now(env, made)
    assert admin_people.verify_code(pid, first) == "confirmed"
    view = admin_people.list_people()[0]
    assert view["status"] == "active" and view["confirmed_at"] and view["last_used_at"]
    assert admin_people.verify_code(pid, first) == "refused"
    ph.next_step(env)
    assert admin_people.verify_code(pid, ph.code_now(env, made)) == "ok"


def test_an_older_code_is_refused_after_a_newer_one(env):
    made = ph.new_person()
    pid, older = made["person"]["id"], ph.code_now(env, made)
    ph.next_step(env)
    assert admin_people.verify_code(pid, ph.code_now(env, made)) == "confirmed"
    assert admin_people.verify_code(pid, older) == "refused"


@pytest.mark.parametrize("bad", ["", "12345", "abcdef"])
def test_a_wrong_or_malformed_code_is_refused(env, bad):
    made = ph.new_person()
    pid = made["person"]["id"]
    assert admin_people.verify_code(pid, bad) == "refused"
    assert admin_people.verify_code(pid, ph.wrong(ph.code_now(env, made))) == "refused"
    assert admin_people.list_people()[0]["status"] == "pending"


def test_each_person_has_their_own_replay_state(env):
    one, two = ph.new_person("person-one"), ph.new_person("person-two")
    assert admin_people.verify_code(one["person"]["id"], ph.code_now(env, one)) == "confirmed"
    assert admin_people.verify_code(two["person"]["id"], ph.code_now(env, two)) == "confirmed"


def test_an_unreadable_store_refuses_keys_and_codes(env):
    made = ph.new_person()
    pid = made["person"]["id"]
    env.store.write_text("{not json")
    assert admin_people.find_by_key(made["key"]) is None
    assert admin_people.get(pid) is None
    assert admin_people.verify_code(pid, ph.code_now(env, made)) == "refused"


@pytest.mark.parametrize("stored", ["enc:not-a-fernet-token", "JBSWY3DPEHPK3PXP"])
def test_a_secret_that_cannot_be_decrypted_refuses_codes(env, stored):
    made = ph.new_person()
    data = json.loads(env.store.read_text())
    data["people"][0]["totp_secret_enc"] = stored
    env.store.write_text(json.dumps(data))
    assert admin_people.verify_code(made["person"]["id"], ph.code_now(env, made)) == "refused"
```

- [ ] **Step 2: Run them to see them fail**

Run: `$PYTEST tests/test_admin_people_store.py`
Expected: FAIL, `AttributeError: module 'plugboard.admin_people' has no attribute 'find_by_key'`.

- [ ] **Step 3: Implement `find_by_key`, `get`, `verify_code`** (interfaces ci-dessus)

- [ ] **Step 4: Run the tests**

Run: `$PYTEST tests/test_admin_people_store.py tests/test_admin_mfa_matching_step.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugboard/admin_people.py tests/test_admin_people_store.py
git commit -m "feat(auth): check a personal key and its own single-use TOTP code (PB-AUTH-01)"
```

---

### Task 4: Révoquer une clé et couper ses sessions

**Files:**
- Modify: `plugboard/admin_people.py`, `plugboard/admin_sessions.py` (`register`, nouvelle `revoke_person`)
- Test: `tests/test_admin_people_store.py` (ajout)

**Interfaces:**
- Consumes: `admin_sessions._load/_save/_LOCK`.
- Produces:
  - `admin_sessions.register(nonce, issued_at, *, mfa, max_age, mfa_generation=None, person_id: Optional[str] = None, person_generation: Optional[str] = None) -> None` — les clés `person_id` et `person_generation` ne sont écrites **que** si `person_id` est donné (les sessions `ADMIN_KEY` gardent exactement leur forme actuelle).
  - `admin_sessions.revoke_person(person_id: str) -> int` — retire les sessions de cette personne, renvoie leur nombre ; n'écrit rien quand il n'y en a aucune (magasin illisible : `_load` donne `{}`, donc `0`, sans exception).
  - `admin_people.revoke(person_id: str, *, revoked_by: str) -> Optional[tuple[dict, bool]]` — `None` si identifiant hors `ID_PATTERN` ou inconnu ; sinon `(vue, changé)`. Au premier appel : `revoked_at`, `revoked_by`, nouvelle `generation`, `_save`. Toujours ensuite : `admin_sessions.revoke_person(person_id)`, dont un `OSError`/`StoreUnreadableError` est journalisé (type) et avalé (les sessions sont déjà refusées par le statut). Magasin illisible → `PeopleStoreUnreadable`.

- [ ] **Step 1: Write the failing tests** (à la suite de `tests/test_admin_people_store.py` ; les deux imports montent en tête du fichier, sinon `ruff` E402)

```python
import time as _time

from plugboard import admin_sessions


def _session(nonce, **person):
    admin_sessions.register(nonce, int(_time.time()), mfa=bool(person), max_age=3600, **person)


def test_revoke_marks_the_record_changes_its_generation_and_is_idempotent(env):
    made = ph.new_person()
    pid = made["person"]["id"]
    before = admin_people.get(pid)["generation"]
    view, changed = admin_people.revoke(pid, revoked_by="admin-key")
    assert changed and view["status"] == "revoked"
    assert view["revoked_by"] == "admin-key" and view["revoked_at"]
    assert admin_people.get(pid)["generation"] != before
    assert admin_people.find_by_key(made["key"]) is None
    assert admin_people.verify_code(pid, ph.code_now(env, made)) == "refused"
    again, changed = admin_people.revoke(pid, revoked_by="person:ap_000000000000")
    assert not changed and again["revoked_by"] == "admin-key"


@pytest.mark.parametrize("pid", ["ap_000000000000", "../admin", "ap_XYZ", ""])
def test_revoking_an_unknown_or_malformed_id_returns_none(env, pid):
    ph.new_person()
    assert admin_people.revoke(pid, revoked_by="admin-key") is None


def test_a_revoked_name_can_be_given_again(env):
    first = ph.new_person()
    admin_people.revoke(first["person"]["id"], revoked_by="admin-key")
    assert ph.new_person()["person"]["status"] == "pending"


def test_an_unreadable_store_refuses_a_revocation(env):
    pid = ph.new_person()["person"]["id"]
    env.store.write_text("{not json")
    with pytest.raises(admin_people.PeopleStoreUnreadable):
        admin_people.revoke(pid, revoked_by="admin-key")


def test_sessions_record_the_person_and_revoke_person_drops_only_theirs(env):
    _session("n-admin")
    _session("n-one", person_id="ap_aaaaaaaaaaaa", person_generation="g1")
    _session("n-two", person_id="ap_bbbbbbbbbbbb", person_generation="g2")
    one = admin_sessions.lookup("n-one")
    assert (one["person_id"], one["person_generation"]) == ("ap_aaaaaaaaaaaa", "g1")
    assert "person_id" not in admin_sessions.lookup("n-admin")
    assert admin_sessions.revoke_person("ap_aaaaaaaaaaaa") == 1
    assert admin_sessions.lookup("n-one") is None
    assert admin_sessions.lookup("n-two") and admin_sessions.lookup("n-admin")


def test_revoke_cuts_the_person_sessions(env):
    pid = ph.new_person()["person"]["id"]
    _session("n-one", person_id=pid, person_generation="g")
    admin_people.revoke(pid, revoked_by="admin-key")
    assert admin_sessions.lookup("n-one") is None


def test_revoke_person_on_an_unreadable_session_store_does_not_raise(env):
    (env.dir / "admin_sessions.json").write_text("{bad")
    assert admin_sessions.revoke_person("ap_aaaaaaaaaaaa") == 0
```

- [ ] **Step 2: Run them to see them fail**

Run: `$PYTEST tests/test_admin_people_store.py`
Expected: FAIL, `TypeError: register() got an unexpected keyword argument 'person_id'`.

- [ ] **Step 3: Implement** `register` (kwargs), `revoke_person`, `admin_people.revoke` (interfaces ci-dessus). Docstring de `admin_sessions` : une phrase sur `person_id`/`person_generation`.

- [ ] **Step 4: Run the tests and the session neighbours**

Run: `$PYTEST tests/test_admin_people_store.py tests/test_session_logout_all.py tests/test_secrets_rekey.py tests/test_auth_residuals_v7b.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugboard/admin_people.py plugboard/admin_sessions.py tests/test_admin_people_store.py
git commit -m "feat(auth): revoke a personal key and cut its sessions (PB-AUTH-01)"
```

---

### Task 5: Le principal dans l'audit

**Files:**
- Modify: `plugboard/audit.py`
- Create: `tests/test_audit_principal.py`

**Interfaces:**
- Produces (module `plugboard.audit`) :
  - colonnes `principal TEXT NOT NULL DEFAULT ''` et `principal_name TEXT NOT NULL DEFAULT ''` dans `_CREATE_TABLE` et `_INSERT` ; `_set_up` ajoute les colonnes absentes d'une base existante (`PRAGMA table_info(audit_log)` puis `ALTER TABLE audit_log ADD COLUMN …`), en ignorant une `sqlite3.OperationalError` « duplicate column » (deux processus qui migrent ensemble) ;
  - `_principal_var: ContextVar[Optional[Dict[str, str]]]` (défaut `None`) ;
  - `begin_request() -> contextvars.Token` (pose un dict vide), `end_request(token) -> None`, `set_principal(principal: str, name: str = "") -> None` (remplit le dict courant ; sans dict, ne fait rien), `current_principal() -> Tuple[str, str]` (`("", "")` sans dict) ;
  - `log_action(action, actor="admin", target_type="", target_id="", details="", ip="", principal: Optional[str] = None, principal_name: Optional[str] = None) -> Dict[str, Any]` — `principal` absent : celui de `current_principal()` ; `principal` donné sans nom : nom `""`. L'entrée rendue porte les deux champs.

- [ ] **Step 1: Write the failing tests**

```python
"""Audit entries name the admin credential behind them (PB-AUTH-01)."""

import asyncio
import sqlite3

import pytest

from plugboard import audit

OLD_TABLE = """CREATE TABLE audit_log (id TEXT PRIMARY KEY, ts TEXT NOT NULL,
    action TEXT NOT NULL, actor TEXT NOT NULL DEFAULT 'admin',
    target_type TEXT NOT NULL DEFAULT '', target_id TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '')"""


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_DB_PATH", tmp_path / "audit.db")


def test_an_entry_outside_a_request_has_an_empty_principal(audit_db):
    entry = audit.log_action("x.test")
    assert (entry["principal"], entry["principal_name"]) == ("", "")
    assert audit.get_audit_log()[0]["principal"] == ""


def test_the_request_principal_is_recorded_then_forgotten(audit_db):
    token = audit.begin_request()
    try:
        audit.set_principal("person:ap_0123456789ab", "person-one")
        audit.log_action("x.test")
    finally:
        audit.end_request(token)
    row = audit.get_audit_log(action="x.test")[0]
    assert (row["principal"], row["principal_name"]) == (
        "person:ap_0123456789ab", "person-one",
    )
    audit.log_action("y.test")
    assert audit.get_audit_log(action="y.test")[0]["principal"] == ""


def test_an_explicit_principal_wins(audit_db):
    token = audit.begin_request()
    try:
        audit.set_principal("person:ap_0123456789ab", "person-one")
        entry = audit.log_action("x.test", principal="admin-key")
    finally:
        audit.end_request(token)
    assert (entry["principal"], entry["principal_name"]) == ("admin-key", "")


def test_set_principal_outside_a_request_does_nothing(audit_db):
    audit.set_principal("admin-key")
    assert audit.current_principal() == ("", "")


async def test_a_worker_thread_fills_the_request_principal(audit_db):
    token = audit.begin_request()
    try:
        await asyncio.to_thread(audit.set_principal, "admin-key", "")
        audit.log_action("z.test")
    finally:
        audit.end_request(token)
    assert audit.get_audit_log(action="z.test")[0]["principal"] == "admin-key"


def test_an_old_database_gains_the_columns(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(OLD_TABLE)
        conn.execute(
            "INSERT INTO audit_log (id, ts, action) VALUES ('old1', '2026-01-01T00:00:00.000Z', 'key.create')"
        )
    conn.close()
    monkeypatch.setattr(audit, "_DB_PATH", db)
    audit.log_action("new.test", principal="admin-key")
    rows = {r["action"]: r for r in audit.get_audit_log()}
    assert (rows["key.create"]["principal"], rows["key.create"]["principal_name"]) == ("", "")
    assert rows["new.test"]["principal"] == "admin-key"
```

- [ ] **Step 2: Run them to see them fail**

Run: `$PYTEST tests/test_audit_principal.py`
Expected: FAIL, `KeyError: 'principal'` puis `AttributeError: ... 'begin_request'`.

- [ ] **Step 3: Implement** (interfaces ci-dessus ; docstring du module : les deux colonnes et le sens de `""`).

- [ ] **Step 4: Run the tests and the audit neighbours**

Run: `$PYTEST tests/test_audit_principal.py tests/test_audit_concurrent.py tests/test_sqlite_wal_setup.py tests/test_provider_audit_actor.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugboard/audit.py tests/test_audit_principal.py
git commit -m "feat(audit): record the admin principal of each entry (PB-AUTH-01)"
```

---

### Task 6: Une clé personnelle sur l'API admin, toujours avec son code

**Files:**
- Modify: `plugboard/app.py` — constantes et aides près de `_ADMIN_SCOPE` (~l.1230), `_admin_request_auth` (~l.1667-1701), nouvelle `_person_request_auth` juste après ; import `from plugboard import admin_people` et `from plugboard import audit as _audit_mod` en tête (~l.84-91)
- Create: `tests/test_admin_people_auth.py`

**Interfaces:**
- Consumes: `admin_people.KEY_PREFIX`, `find_by_key`, `verify_code` ; `audit.set_principal` ; `ratelimit.check_auth_rate_limit`, `record_auth_failure`.
- Produces (dans `plugboard/app.py`) :
  - `_ADMIN_KEY_PRINCIPAL = "admin-key"`
  - `_person_principal(person: Dict[str, Any]) -> str` → `"person:" + person["id"]`
  - `_person_scope(person_id: str) -> str` → `f"{_ADMIN_SCOPE}:{person_id}"`
  - `_set_principal(request: Request, principal: str, name: str = "") -> None` — `request.state.admin_principal`, `request.state.admin_principal_name`, puis `_audit_mod.set_principal(principal, name)`.
  - `_person_request_auth(request: Request, token: str) -> str` — mêmes valeurs que `_admin_request_auth` : `find_by_key` → `None` : `"denied"` (rien compté) ; en-tête vide : `"mfa_required"` ; `check_auth_rate_limit(scope)` faux : `"locked"` ; `verify_code` → `"refused"` : `record_auth_failure(scope)`, audit `auth.person.mfa_failed` (`target_type="admin_person"`, `target_id=id`, `details=f"header IP: {ip}, locked={locked}"`, `ip=ip`, `principal=` et `principal_name=` de la personne passés explicitement), `"mfa_required"` ; `"confirmed"` : audit `admin.person.confirmed` (`details="via=header"`) ; succès : `_set_principal(...)`, `"ok"`. **Ne pose jamais `request.state.admin_mfa_verified`** (réservé au code de `ADMIN_KEY`).
  - `_admin_request_auth` : après la vérification de session, `if token.startswith(admin_people.KEY_PREFIX) and not _is_admin_key(token): return _person_request_auth(request, token)` ; une `ADMIN_KEY` reconnue appelle `_set_principal(request, _ADMIN_KEY_PRINCIPAL)` avant la branche MFA. Rien d'autre ne change.

- [ ] **Step 1: Write the failing tests**

```python
"""A personal key on the admin API, always with its own TOTP code (PB-AUTH-01)."""

import json

import pytest

from plugboard import admin_mfa, admin_people, audit, ratelimit
from tests import people_helpers as ph


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield from ph.isolate(tmp_path, monkeypatch)


async def test_a_personal_key_without_its_code_is_refused_even_without_global_mfa(admin_client, env):
    made = ph.new_person()
    assert not admin_mfa.is_required()
    assert await ph.keys_status(admin_client, ph.bearer(made)) == 401
    assert await ph.keys_status(admin_client, ph.ADMIN) == 200


async def test_its_code_makes_it_admin_and_confirms_the_key(admin_client, env):
    made = ph.new_person()
    pid = made["person"]["id"]
    assert await ph.keys_status(admin_client, ph.bearer(made, ph.code_now(env, made))) == 200
    assert admin_people.list_people()[0]["status"] == "active"
    confirmed = audit.get_audit_log(action="admin.person.confirmed")
    assert [e["target_id"] for e in confirmed] == [pid]


async def test_a_code_of_another_secret_is_refused(admin_client, env):
    one, two = ph.new_person("person-one"), ph.new_person("person-two")
    assert await ph.keys_status(admin_client, ph.bearer(one, ph.code_now(env, two))) == 401
    secret = admin_mfa.generate_secret()
    admin_mfa.enroll(secret)
    admin_code = admin_mfa._generate_code(secret, at=env.clock.t)
    assert await ph.keys_status(admin_client, ph.bearer(one, admin_code)) == 401


async def test_a_code_is_single_use_on_the_header(admin_client, env):
    made = ph.new_person()
    headers = ph.bearer(made, ph.code_now(env, made))
    assert await ph.keys_status(admin_client, headers) == 200
    assert await ph.keys_status(admin_client, headers) == 401


async def test_an_unknown_personal_key_is_denied_and_not_counted(admin_client, env):
    headers = {"Authorization": "Bearer pba_" + "A" * 43, ph.TOTP: "123456"}
    assert await ph.keys_status(admin_client, headers) == 401
    assert not ratelimit._auth_buckets and not ratelimit._lockouts


async def test_a_revoked_key_with_a_valid_code_is_refused(admin_client, env):
    made = ph.new_person()
    admin_people.revoke(made["person"]["id"], revoked_by="admin-key")
    assert await ph.keys_status(admin_client, ph.bearer(made, ph.code_now(env, made))) == 401


async def test_wrong_codes_lock_that_person_only(admin_client, env, monkeypatch):
    one, two = ph.new_person("person-one"), ph.new_person("person-two")
    pid = one["person"]["id"]
    bad = ph.wrong(ph.code_now(env, one))
    for _ in range(5):
        assert await ph.keys_status(admin_client, ph.bearer(one, bad)) == 401
    assert await ph.keys_status(admin_client, ph.bearer(one, ph.code_now(env, one))) == 429
    assert await ph.keys_status(admin_client, ph.bearer(two, ph.code_now(env, two))) == 200
    secret = admin_mfa.generate_secret()
    admin_mfa.enroll(secret)
    monkeypatch.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    admin = {**ph.ADMIN, ph.TOTP: admin_mfa._generate_code(secret)}
    assert await ph.keys_status(admin_client, admin) == 200
    assert set(ratelimit._lockouts) == {f"key:admin:{pid}"}
    failed = audit.get_audit_log(action="auth.person.mfa_failed")
    assert len(failed) == 5 and {e["target_id"] for e in failed} == {pid}
    assert {e["principal"] for e in failed} == {f"person:{pid}"}
    assert bad not in json.dumps(failed)
```

- [ ] **Step 2: Run them to see them fail**

Run: `$PYTEST tests/test_admin_people_auth.py`
Expected: FAIL — `test_its_code_makes_it_admin_and_confirms_the_key` reçoit 401 au lieu de 200 (les autres peuvent déjà passer : une clé inconnue est refusée).

- [ ] **Step 3: Implement** les aides et la branche personnelle (interfaces ci-dessus).

- [ ] **Step 4: Run the tests and the auth neighbours**

Run: `$PYTEST tests/test_admin_people_auth.py tests/test_inherited_auth_v7.py tests/test_auth_residuals_v7b.py tests/test_ratelimit_identity.py tests/test_admin_csrf_same_site.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugboard/app.py tests/test_admin_people_auth.py
git commit -m "feat(auth): accept a personal key with its own TOTP on admin routes (PB-AUTH-01)"
```

---

### Task 7: Le principal voyage jusqu'à l'audit de chaque requête

**Files:**
- Modify: `plugboard/app.py` — `_RequestIdMiddleware.dispatch` (~l.1213-1227)
- Test: `tests/test_audit_principal.py` (ajout)

**Interfaces:**
- Consumes: `audit.begin_request` / `end_request` (tâche 5), `_set_principal` (tâche 6).
- Produces: chaque requête HTTP ouvre son dict de principal (`begin_request()` après `_request_id_var.set`, `end_request` dans le `finally`) ; tout `_audit(...)` écrit pendant une requête admin authentifiée porte `principal`/`principal_name`, y compris depuis une route `def` (pool de threads).

- [ ] **Step 1: Write the failing tests** (à la suite de `tests/test_audit_principal.py` ; l'import monte en tête du fichier, sinon `ruff` E402)

```python
from tests import people_helpers as ph


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield from ph.isolate(tmp_path, monkeypatch)


async def test_an_admin_key_action_names_the_admin_key(admin_client, env):
    admin_client.cookies.clear()
    r = await admin_client.post("/admin/keys", json={"client_id": "acme-test"}, headers=ph.ADMIN)
    assert r.status_code == 201
    entry = audit.get_audit_log(action="key.create")[0]
    assert (entry["actor"], entry["principal"], entry["principal_name"]) == ("admin", "admin-key", "")


async def test_a_personal_key_action_names_the_person(admin_client, env):
    made = ph.new_person()
    pid = made["person"]["id"]
    admin_client.cookies.clear()
    r = await admin_client.post(
        "/admin/keys", json={"client_id": "acme-test"},
        headers=ph.bearer(made, ph.code_now(env, made)),
    )
    assert r.status_code == 201
    entry = audit.get_audit_log(action="key.create")[0]
    assert (entry["principal"], entry["principal_name"]) == (f"person:{pid}", "person-one")


async def test_a_sync_route_names_the_person_too(admin_client, env, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_DATA", str(env.dir / "data"))
    made = ph.new_person()
    admin_client.cookies.clear()
    r = await admin_client.post(
        "/admin/suivi/tickets", json={"titre": "ticket de test"},
        headers=ph.bearer(made, ph.code_now(env, made)),
    )
    assert r.status_code == 201
    entry = audit.get_audit_log(action="suivi.ticket.create")[0]
    assert entry["principal"] == f"person:{made['person']['id']}"


async def test_the_principal_does_not_leak_into_the_next_request(admin_client, env):
    made = ph.new_person()
    assert await ph.keys_status(admin_client, ph.bearer(made, ph.code_now(env, made))) == 200
    r = await admin_client.post("/auth/admin/login", json={"key": "not-the-key"})
    assert r.status_code == 401
    assert audit.get_audit_log(action="auth.login_failed")[0]["principal"] == ""
```

- [ ] **Step 2: Run them to see them fail**

Run: `$PYTEST tests/test_audit_principal.py`
Expected: FAIL — `principal` vaut `""` au lieu de `admin-key` / `person:<id>`.

- [ ] **Step 3: Implement** l'ouverture et la fermeture du dict dans `_RequestIdMiddleware.dispatch`.

- [ ] **Step 4: Run the tests and the neighbours**

Run: `$PYTEST tests/test_audit_principal.py tests/test_provider_audit_actor.py tests/test_suivi_api.py tests/test_mcp_admin_tools.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugboard/app.py tests/test_audit_principal.py
git commit -m "feat(audit): carry the admin principal through each request (PB-AUTH-01)"
```

---

### Task 8: Connexion par clé personnelle, sessions liées à la personne

**Files:**
- Modify: `plugboard/app.py` — `_create_admin_session_token` (~l.1388), `_is_admin_session` (~l.1444) remplacée par `_admin_session_principal` + `_is_admin_session`, branche session de `_admin_request_auth`, `admin_login` (~l.7451-7556) + nouvelles `_person_login` et `_session_response`, audit `auth.auto_login` de `login_page` (~l.7410)
- Create: `tests/test_admin_people_login.py`

**Interfaces:**
- Consumes: `admin_people.find_by_key`, `get`, `verify_code` ; `admin_sessions.register(..., person_id=, person_generation=)` ; `_person_scope`, `_set_principal`, `_person_principal`.
- Produces :
  - `_create_admin_session_token(mfa: bool = False, person: Optional[Dict[str, Any]] = None) -> str` — avec `person` : `mfa=True`, `person_id`, `person_generation=person["generation"]` ; sans : inchangé.
  - `_admin_session_principal(token: str) -> Optional[Tuple[str, str]]` — `("admin-key", "")` pour une session `ADMIN_KEY` valide (mêmes règles qu'aujourd'hui : signature, enregistrée, `mfa_generation`, `mfa or not is_required()`), `(f"person:{id}", nom)` pour une session de personne valide (mêmes règles, plus : `admin_people.get(id)` non `None`, non révoquée, `generation == record["person_generation"]`), sinon `None`.
  - `_is_admin_session(token: str) -> bool` → `_admin_session_principal(token) is not None` (signature inchangée pour ses appelants).
  - `_admin_request_auth` : une session valide appelle `_set_principal(request, *principal)` avant `"ok"`.
  - `_session_response(request: Request, session_token: str) -> Response` — le `204` avec le cookie (code actuel de fin de `admin_login`, déplacé tel quel, `delete_cookie("llm_admin_key")` compris).
  - `_person_login(request: Request, body: AdminLogin, person: Dict[str, Any]) -> Response` — code absent : `401 {"detail": "MFA code required", "mfa_required": True}` ; portée verrouillée : audit `auth.login_blocked` (`target_type="admin_person"`, `target_id=id`, `details=f"personal key locked out (MFA), IP: {ip}"`), `429` + `Retry-After` restant ; code refusé : `record_auth_failure(scope)`, audit `auth.person.mfa_failed` (`details=f"IP: {ip}, locked={locked}"`), `429` + `Retry-After: 900` si verrouillé, sinon `401 Invalid MFA code` ; `"confirmed"` : audit `admin.person.confirmed` (`details="via=login"`) ; puis session, audit `auth.login_success` (principal de la personne), `_session_response`.
  - `admin_login` : `person = None if is_admin_key or not token.startswith(admin_people.KEY_PREFIX) else admin_people.find_by_key(token)` ; `known = is_admin_key or person is not None` remplace `is_admin_key` dans les deux gardes d'adresse ; puis `if person is not None: return _person_login(request, body, person)` ; une `ADMIN_KEY` reconnue appelle `_set_principal(request, _ADMIN_KEY_PRINCIPAL)` ; le reste du chemin `ADMIN_KEY` ne change pas (sauf l'appel à `_session_response`).
  - `login_page` : `_audit("auth.auto_login", …, principal=_ADMIN_KEY_PRINCIPAL)`.

- [ ] **Step 1: Write the failing tests**

```python
"""Sign in with a personal key; its sessions die with it (PB-AUTH-01)."""

import json

import pytest

from plugboard import admin_people, audit, ratelimit
from tests import people_helpers as ph


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield from ph.isolate(tmp_path, monkeypatch)


def _sessions(env):
    return json.loads((env.dir / "admin_sessions.json").read_text())["sessions"]


async def test_login_with_a_personal_key_asks_for_its_code(admin_client, env):
    made = ph.new_person()
    r = await admin_client.post("/auth/admin/login", json={"key": made["key"]})
    assert r.status_code == 401
    assert r.json() == {"detail": "MFA code required", "mfa_required": True}
    assert "set-cookie" not in r.headers


async def test_its_code_opens_a_session_bound_to_the_person(admin_client, env):
    made = ph.new_person()
    pid = made["person"]["id"]
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    assert await ph.keys_status(admin_client, cookie) == 200
    record = next(iter(_sessions(env).values()))
    assert record["person_id"] == pid and record["mfa"] is True
    assert record["person_generation"] == admin_people.get(pid)["generation"]
    assert audit.get_audit_log(action="auth.login_success")[0]["principal"] == f"person:{pid}"
    admin_client.cookies.clear()
    await admin_client.post("/admin/keys", json={"client_id": "acme-test"}, headers=cookie)
    assert audit.get_audit_log(action="key.create")[0]["principal"] == f"person:{pid}"


async def test_admin_key_login_and_sessions_keep_their_shape(admin_client, env):
    cookie = await ph.login(admin_client, "adminkey")
    assert await ph.keys_status(admin_client, cookie) == 200
    assert "person_id" not in next(iter(_sessions(env).values()))
    assert audit.get_audit_log(action="auth.login_success")[0]["principal"] == "admin-key"


async def test_an_unknown_personal_key_counts_against_the_address(admin_client, env):
    r = await admin_client.post(
        "/auth/admin/login", json={"key": "pba_" + "B" * 43, "totp_code": "123456"}
    )
    assert r.status_code == 401 and r.json()["detail"] == "Admin key required"
    assert list(ratelimit._auth_buckets) == ["ip:127.0.0.1"]


async def test_a_valid_personal_key_passes_an_address_lockout(admin_client, env):
    for _ in range(5):
        await admin_client.post("/auth/admin/login", json={"key": "not-the-key"})
    assert not ratelimit.check_auth_rate_limit("ip:127.0.0.1")
    made = ph.new_person()
    await ph.login(admin_client, made["key"], ph.code_now(env, made))


async def test_wrong_login_codes_lock_that_person_only(admin_client, env):
    one, two = ph.new_person("person-one"), ph.new_person("person-two")
    bad = {"key": one["key"], "totp_code": ph.wrong(ph.code_now(env, one))}
    codes = [(await admin_client.post("/auth/admin/login", json=bad)).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 401, 429]
    right = {"key": one["key"], "totp_code": ph.code_now(env, one)}
    assert (await admin_client.post("/auth/admin/login", json=right)).status_code == 429
    await ph.login(admin_client, two["key"], ph.code_now(env, two))
    await ph.login(admin_client, "adminkey")
    assert set(ratelimit._lockouts) == {f"key:admin:{one['person']['id']}"}


async def test_a_code_used_at_login_is_refused_on_the_header(admin_client, env):
    one, two = ph.new_person("person-one"), ph.new_person("person-two")
    code = ph.code_now(env, one)
    await ph.login(admin_client, one["key"], code)
    assert await ph.keys_status(admin_client, ph.bearer(one, code)) == 401
    await ph.login(admin_client, two["key"], ph.code_now(env, two))


async def test_revoking_the_person_cuts_the_session_at_once(admin_client, env):
    made = ph.new_person()
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    admin_people.revoke(made["person"]["id"], revoked_by="admin-key")
    assert await ph.keys_status(admin_client, cookie) == 401
    admin_client.cookies.clear()
    page = await admin_client.get("/dashboard", headers=cookie)
    assert page.status_code in (302, 307) and "/login" in page.headers["location"]


async def test_a_session_from_another_generation_is_refused(admin_client, env):
    made = ph.new_person()
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    data = json.loads(env.store.read_text())
    data["people"][0]["generation"] = "0" * 32
    env.store.write_text(json.dumps(data))
    assert await ph.keys_status(admin_client, cookie) == 401


async def test_a_revoked_key_cannot_sign_in(admin_client, env):
    made = ph.new_person()
    admin_people.revoke(made["person"]["id"], revoked_by="admin-key")
    r = await admin_client.post(
        "/auth/admin/login", json={"key": made["key"], "totp_code": ph.code_now(env, made)}
    )
    assert r.status_code == 401


async def test_an_unreadable_store_refuses_people_but_not_the_admin_key(admin_client, env, caplog):
    made = ph.new_person()
    person = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    admin = await ph.login(admin_client, "adminkey")
    env.store.write_text("{not json")
    ph.next_step(env)
    assert await ph.keys_status(admin_client, person) == 401
    r = await admin_client.post(
        "/auth/admin/login", json={"key": made["key"], "totp_code": ph.code_now(env, made)}
    )
    assert r.status_code == 401
    assert await ph.keys_status(admin_client, admin) == 200
    assert await ph.keys_status(admin_client, ph.ADMIN) == 200
    assert "not json" not in caplog.text


async def test_a_person_session_is_refused_cross_site(admin_client, env):
    made = ph.new_person()
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    admin_client.cookies.clear()
    r = await admin_client.post(
        "/admin/keys", json={"client_id": "acme-test"},
        headers={**cookie, "Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403
```

- [ ] **Step 2: Run them to see them fail**

Run: `$PYTEST tests/test_admin_people_login.py`
Expected: FAIL — la connexion par clé personnelle répond `401 Admin key required` au lieu de `mfa_required` / `204`.

- [ ] **Step 3: Implement** (interfaces ci-dessus).

- [ ] **Step 4: Run the tests and the login/session neighbours**

Run: `$PYTEST tests/test_admin_people_login.py tests/test_admin_people_auth.py tests/test_session_logout_all.py tests/test_auth_auto_login.py tests/test_inherited_auth_v7.py tests/test_auth_residuals_v7b.py tests/test_ratelimit_identity.py tests/test_proxy_client_ip.py tests/test_admin_csrf_same_site.py tests/test_resolve_client.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugboard/app.py tests/test_admin_people_login.py
git commit -m "feat(auth): sign in with a personal key, bind sessions to the person (PB-AUTH-01)"
```

---

### Task 9: Limite globale, `/health/ready` et plan client

**Files:**
- Modify: `plugboard/app.py` — `_key_scope` (~l.1249-1275), nouvelle `_person_bearer_scope`, `_rate_limit_scope` (~l.1278-1286)
- Create: `tests/test_admin_people_scope.py`

**Interfaces:**
- Consumes: `_admin_session_principal` (tâche 8), `_person_scope`, `admin_people.find_by_key`.
- Produces :
  - `_key_scope(request)` : branche cookie → `_ADMIN_SCOPE` pour `admin-key`, `_person_scope(id)` pour une personne (la vérification de signature `_admin_session_nonce` reste en premier) ; la branche bearer ne change pas (une `pba_` nue n'y compte pas : pas de `checks`).
  - `_person_bearer_scope(request: Request) -> str` — `pba_` valide (et ≠ `ADMIN_KEY`) → `_person_scope(id)`, sinon `""`.
  - `_rate_limit_scope(request)` → `_key_scope(request) or _person_bearer_scope(request) or _address_scope(request)`.

- [ ] **Step 1: Write the failing tests**

```python
"""Personal keys in the global limit, /health/ready and the client plane (PB-AUTH-01)."""

import pytest
from httpx import ASGITransport, AsyncClient

from plugboard import admin_people, provider_watchdog, readiness, usage
from tests import people_helpers as ph

PROXY = "172.18.0.1"


@pytest.fixture
def env(tmp_path, monkeypatch, _providers_file, _admin_key):
    """Real global limit (60/min), readiness isolated as in test_readiness_visibility."""
    import plugboard.ratelimit as rl

    monkeypatch.setattr(rl, "_LIMIT", 60)
    rl._buckets.clear()
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(usage, "_schema_ready", {})
    monkeypatch.setattr(
        provider_watchdog, "get_watchdog_status",
        lambda: {"running": True, "check_interval_s": 30, "providers": {}},
    )
    readiness.reset_cache()
    yield from ph.isolate(tmp_path, monkeypatch)
    rl._buckets.clear()


def _client():
    from plugboard.app import app

    return AsyncClient(transport=ASGITransport(app=app, client=(PROXY, 40000)), base_url="http://test")


async def test_a_valid_personal_key_has_its_own_bucket(env):
    import plugboard.ratelimit as rl

    made = ph.new_person()
    async with _client() as c:
        assert (await c.get("/health", headers=ph.bearer(made))).status_code == 200
    assert len(rl._buckets[f"key:admin:{made['person']['id']}"]) == 1
    assert f"ip:{PROXY}" not in rl._buckets


@pytest.mark.parametrize("revoked", [True, False])
async def test_a_revoked_or_unknown_personal_key_stays_on_the_address(env, revoked):
    import plugboard.ratelimit as rl

    made = ph.new_person()
    key = made["key"] if revoked else "pba_" + "C" * 43
    if revoked:
        admin_people.revoke(made["person"]["id"], revoked_by="admin-key")
    async with _client() as c:
        await c.get("/health", headers={"Authorization": f"Bearer {key}"})
    assert set(rl._buckets) == {f"ip:{PROXY}"}


async def test_a_person_session_counts_under_the_person(env):
    import plugboard.ratelimit as rl

    made = ph.new_person()
    async with _client() as c:
        cookie = await ph.login(c, made["key"], ph.code_now(env, made))
        assert (await c.get("/health", headers=cookie)).status_code == 200
    assert len(rl._buckets[f"key:admin:{made['person']['id']}"]) == 1


async def test_ready_checks_need_a_session_not_a_bare_personal_key(env):
    made = ph.new_person()
    async with _client() as c:
        bare = await c.get("/health/ready", headers=ph.bearer(made))
        assert set(bare.json()) == {"ready", "version"}
        cookie = await ph.login(c, made["key"], ph.code_now(env, made))
        c.cookies.clear()
        assert "checks" in (await c.get("/health/ready", headers=cookie)).json()
        admin_people.revoke(made["person"]["id"], revoked_by="admin-key")
        c.cookies.clear()
        assert set((await c.get("/health/ready", headers=cookie)).json()) == {"ready", "version"}


async def test_a_personal_key_is_refused_on_the_client_plane(env):
    made = ph.new_person()
    async with _client() as c:
        r = await c.get("/v1/models", headers=ph.bearer(made, ph.code_now(env, made)))
    assert r.status_code == 401
```

- [ ] **Step 2: Run them to see them fail**

Run: `$PYTEST tests/test_admin_people_scope.py`
Expected: FAIL — `KeyError: 'key:admin:ap_…'` (la clé personnelle compte encore sur l'adresse) et la session de personne compte sous `key:admin`.

- [ ] **Step 3: Implement** (interfaces ci-dessus ; docstrings de `_key_scope` et `_rate_limit_scope` : une phrase sur les clés personnelles).

- [ ] **Step 4: Run the tests and the neighbours**

Run: `$PYTEST tests/test_admin_people_scope.py tests/test_ratelimit_identity.py tests/test_readiness_visibility.py tests/test_rpm_limit_scope.py tests/test_proxy_client_ip.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugboard/app.py tests/test_admin_people_scope.py
git commit -m "feat(auth): count personal keys in their own bucket, no checks without a code (PB-AUTH-01)"
```

---

### Task 10: Routes `/admin/people` — créer, lister, révoquer

**Files:**
- Create: `plugboard/admin_people_api.py`
- Modify: `plugboard/app.py` — `app.include_router(...)` à côté de `build_suivi_router` (~l.7756) ; import du routeur (~l.132)
- Create: `tests/test_admin_people_api.py`

**Interfaces:**
- Consumes: `admin_people.create/list_people/revoke`, `PersonError`, `PeopleStoreUnreadable`, `storage.StoreUnreadableError`, `audit.log_action`, `request.state.admin_principal` (défaut `"admin-key"` quand l'auth est coupée).
- Produces: `build_router(require_admin: Callable[[Request], None], admin_key_set: Callable[[], bool]) -> APIRouter` — préfixe `/admin/people`, `tags=["admin-people"]`, `dependencies=[Depends(require_admin)]`, routes **`def`** (fichiers, comme `suivi_api`) :
  - `POST ""` (201) corps `PersonCreate(name: str = Field(max_length=256))`, `extra="forbid"` → `JSONResponse({"person", "key", "totp_secret", "otpauth_uri"}, status_code=201, headers={"Cache-Control": "no-store"})` ; `admin_key_set()` faux → 503 ; `PersonError` → 422 / 409 (son message) ; `PeopleStoreUnreadable`, `StoreUnreadableError`, `OSError` → 503 (message des contraintes) ; audit `admin.person.created` (`details=<nom>`).
  - `GET ""` → `{"people": [vues]}` ; illisible → 503.
  - `DELETE "/{person_id}"` → la vue (200) ; `None` → 404 ; illisible ou non inscriptible → 503 ; audit `admin.person.revoked` (`details=<nom>`) si `changed`.
  - `created_by` / `revoked_by` = `request.state.admin_principal`.
  - Inclusion : `from plugboard.admin_people_api import build_router as build_people_router` (à côté de l'import de `build_suivi_router`), puis `app.include_router(build_people_router(_require_admin, lambda: bool(_effective_admin_key())))`.

- [ ] **Step 1: Write the failing tests**

```python
"""Create, list and revoke personal dashboard keys over the admin API (PB-AUTH-01)."""

import json
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from plugboard import admin_people, audit
from tests import people_helpers as ph


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield from ph.isolate(tmp_path, monkeypatch)


async def _create(client, name="person-one", headers=ph.ADMIN):
    client.cookies.clear()
    return await client.post("/admin/people", json={"name": name}, headers=headers)


async def test_creation_answers_the_key_and_secret_once(admin_client, env):
    r = await _create(admin_client)
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {"person", "key", "totp_secret", "otpauth_uri"}
    assert (body["person"]["status"], body["person"]["created_by"]) == ("pending", "admin-key")
    listed = (await admin_client.get("/admin/people", headers=ph.ADMIN)).json()
    assert set(listed) == {"people"}
    assert [p["id"] for p in listed["people"]] == [body["person"]["id"]]
    text = json.dumps(listed)
    assert body["key"] not in text and body["totp_secret"] not in text and "key_hash" not in text


async def test_a_person_creates_and_revokes_keys(admin_client, env):
    made = ph.new_person()
    pid = made["person"]["id"]
    r = await _create(admin_client, "person-two", ph.bearer(made, ph.code_now(env, made)))
    assert r.json()["person"]["created_by"] == f"person:{pid}"
    ph.next_step(env)
    admin_client.cookies.clear()
    gone = await admin_client.delete(
        f"/admin/people/{r.json()['person']['id']}", headers=ph.bearer(made, ph.code_now(env, made))
    )
    assert gone.status_code == 200 and gone.json()["revoked_by"] == f"person:{pid}"


@pytest.mark.parametrize("name,status", [("two\nlines", 422), ("x" * 65, 422), ("", 422)])
async def test_invalid_names_are_422(admin_client, env, name, status):
    r = await _create(admin_client, name)
    assert r.status_code == status
    assert r.json()["detail"] == "Name must be 1 to 64 printable characters"


async def test_a_duplicate_name_is_409(admin_client, env):
    await _create(admin_client)
    r = await _create(admin_client, " PERSON-ONE ")
    assert r.status_code == 409 and r.json()["detail"] == "A live personal key already has this name"


async def test_revoke_is_idempotent_and_audited_once(admin_client, env):
    pid = (await _create(admin_client)).json()["person"]["id"]
    for _ in range(2):
        r = await admin_client.delete(f"/admin/people/{pid}", headers=ph.ADMIN)
        assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert len(audit.get_audit_log(action="admin.person.revoked")) == 1


@pytest.mark.parametrize("pid", ["ap_000000000000", "nope"])
async def test_revoking_an_unknown_id_is_404(admin_client, env, pid):
    r = await admin_client.delete(f"/admin/people/{pid}", headers=ph.ADMIN)
    assert r.status_code == 404 and r.json()["detail"] == "Personal key not found"


async def test_a_revoked_person_is_refused_everywhere(admin_client, env):
    made = ph.new_person()
    pid = made["person"]["id"]
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    assert (await admin_client.delete(f"/admin/people/{pid}", headers=ph.ADMIN)).status_code == 200
    assert await ph.keys_status(admin_client, cookie) == 401
    ph.next_step(env)
    assert await ph.keys_status(admin_client, ph.bearer(made, ph.code_now(env, made))) == 401
    ph.next_step(env)
    r = await admin_client.post(
        "/auth/admin/login", json={"key": made["key"], "totp_code": ph.code_now(env, made)}
    )
    assert r.status_code == 401
    admin_client.cookies.clear()
    assert (await admin_client.get("/v1/models", headers=cookie)).status_code == 401
    page = await admin_client.get("/dashboard", headers=cookie)
    assert page.status_code in (302, 307) and "/login" in page.headers["location"]


async def test_a_person_can_revoke_themselves(admin_client, env):
    made = ph.new_person()
    pid = made["person"]["id"]
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    admin_client.cookies.clear()
    r = await admin_client.delete(f"/admin/people/{pid}", headers=cookie)
    assert r.status_code == 200 and r.json()["revoked_by"] == f"person:{pid}"
    assert await ph.keys_status(admin_client, cookie) == 401


async def test_no_key_or_secret_is_readable_after_creation(admin_client, env, caplog):
    caplog.set_level(logging.DEBUG)
    body = (await _create(admin_client)).json()
    pid = body["person"]["id"]
    made = {"key": body["key"], "totp_secret": body["totp_secret"]}
    await ph.login(admin_client, body["key"], ph.code_now(env, made))
    ph.next_step(env)
    await ph.keys_status(admin_client, ph.bearer(made, ph.code_now(env, made)))
    listed = (await admin_client.get("/admin/people", headers=ph.ADMIN)).text
    await admin_client.delete(f"/admin/people/{pid}", headers=ph.ADMIN)
    blobs = [
        json.dumps(audit.get_audit_log(limit=1000)), caplog.text, listed,
        env.store.read_text(), (env.dir / "admin_sessions.json").read_text(),
    ]
    for blob in blobs:
        assert body["key"] not in blob and body["totp_secret"] not in blob
    created = audit.get_audit_log(action="admin.person.created")[0]
    assert (created["target_type"], created["target_id"], created["details"]) == (
        "admin_person", pid, "person-one",
    )


async def test_the_routes_need_an_admin(admin_client, env):
    admin_client.cookies.clear()
    assert (await admin_client.get("/admin/people")).status_code == 401
    assert (await admin_client.post("/admin/people", json={"name": "person-one"})).status_code == 401


async def test_without_admin_key_the_routes_answer_503(admin_client, env, monkeypatch):
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "")
    admin_client.cookies.clear()
    assert (await admin_client.get("/admin/people")).status_code == 503
    assert (await admin_client.post("/admin/people", json={"name": "person-one"})).status_code == 503


async def test_with_auth_fully_off_creation_is_refused(env, monkeypatch, _providers_file):
    from plugboard.app import app

    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "")
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        r = await c.post("/admin/people", json={"name": "person-one"})
    assert r.status_code == 503 and r.json()["detail"] == "Personal keys need ADMIN_KEY"


async def test_an_unreadable_store_answers_503_never_500(admin_client, env):
    env.store.write_text("{not json")
    detail = "The personal key store cannot be read or written: see the server log"
    for r in (
        await admin_client.get("/admin/people", headers=ph.ADMIN),
        await _create(admin_client),
        await admin_client.delete("/admin/people/ap_000000000000", headers=ph.ADMIN),
    ):
        assert r.status_code == 503 and r.json()["detail"] == detail
    assert await ph.keys_status(admin_client, ph.ADMIN) == 200
```

Si `test_with_auth_fully_off_creation_is_refused` reçoit 403 du middleware « keyless » (adresse serveur non loopback dans `ASGITransport`), corriger le test en passant `ASGITransport(app=app)` à une app enveloppée qui fixe `scope["server"] = ("127.0.0.1", 8765)` — jamais en relâchant l'assertion 503.

- [ ] **Step 2: Run them to see them fail**

Run: `$PYTEST tests/test_admin_people_api.py`
Expected: FAIL, `404 Not Found` sur `/admin/people`.

- [ ] **Step 3: Implement** `plugboard/admin_people_api.py` et l'inclusion (interfaces ci-dessus).

- [ ] **Step 4: Run the tests and the neighbours**

Run: `$PYTEST tests/test_admin_people_api.py tests/test_admin_people_login.py tests/test_suivi_api.py tests/test_api_contract.py tests/test_admin_input_guards.py`
Expected: all PASS (`test_api_contract.py` reste vert : les routes sont derrière `_require_admin`).

- [ ] **Step 5: Commit**

```bash
git add plugboard/admin_people_api.py plugboard/app.py tests/test_admin_people_api.py
git commit -m "feat(auth): create, list and revoke personal keys over /admin/people (PB-AUTH-01)"
```

---

### Task 11: Le second facteur de `ADMIN_KEY` reste à `ADMIN_KEY`

**Files:**
- Modify: `plugboard/app.py` — nouvelle `_refuse_person_principal`, appelée en tête de `admin_mfa_begin`, `admin_mfa_setup`, `admin_mfa_verify`, `admin_mfa_disable` (~l.7600-7690 ; ajouter `request: Request` à `begin` et `verify`)
- Create: `tests/test_admin_people_mfa_routes.py`

**Interfaces:**
- Consumes: `request.state.admin_principal`.
- Produces: `_refuse_person_principal(request: Request) -> None` — 403 `The second factor of ADMIN_KEY is managed with ADMIN_KEY` quand `getattr(request.state, "admin_principal", _ADMIN_KEY_PRINCIPAL) != _ADMIN_KEY_PRINCIPAL`. Appelée **avant** `_require_mfa_step_up`, donc aucun code d'une personne ne compte sur `key:admin`.

- [ ] **Step 1: Write the failing tests**

```python
"""A person never manages, nor locks, the second factor of ADMIN_KEY (PB-AUTH-01)."""

import pytest

from plugboard import admin_mfa, ratelimit
from tests import people_helpers as ph

ROUTES = [
    ("POST", "/admin/mfa/begin", None),
    ("POST", "/admin/mfa/setup", {"secret": "JBSWY3DPEHPK3PXP", "code": "123456", "current_code": "000000"}),
    ("POST", "/admin/mfa/verify", {"code": "000000"}),
    ("DELETE", "/admin/mfa", None),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield from ph.isolate(tmp_path, monkeypatch)


@pytest.mark.parametrize("enrolled", [True, False])
@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_a_person_cannot_touch_the_admin_key_factor(admin_client, env, enrolled, method, path, body):
    if enrolled:
        admin_mfa.enroll(admin_mfa.generate_secret())
    made = ph.new_person()
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    admin_client.cookies.clear()
    r = await admin_client.request(method, path, json=body, headers={**cookie, ph.TOTP: "000000"})
    assert r.status_code == 403
    assert r.json()["detail"] == "The second factor of ADMIN_KEY is managed with ADMIN_KEY"
    assert admin_mfa.is_enrolled() is enrolled
    assert "key:admin" not in ratelimit._auth_buckets


async def test_a_person_may_read_the_mfa_status(admin_client, env):
    made = ph.new_person()
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    admin_client.cookies.clear()
    assert (await admin_client.get("/admin/mfa/status", headers=cookie)).status_code == 200
```

- [ ] **Step 2: Run them to see them fail**

Run: `$PYTEST tests/test_admin_people_mfa_routes.py`
Expected: FAIL — `/admin/mfa/begin` répond 200 à une personne.

- [ ] **Step 3: Implement** `_refuse_person_principal` et ses quatre appels.

- [ ] **Step 4: Run the tests and the MFA neighbours**

Run: `$PYTEST tests/test_admin_people_mfa_routes.py tests/test_inherited_auth_v7.py tests/test_auth_residuals_v7b.py tests/test_spec_v7_residuals_parent.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugboard/app.py tests/test_admin_people_mfa_routes.py
git commit -m "feat(auth): keep the ADMIN_KEY second factor out of personal keys' reach (PB-AUTH-01)"
```

---

### Task 12: La rotation de `ENCRYPTION_KEY` couvre les secrets personnels

**Files:**
- Modify: `plugboard/secrets_rekey.py` — docstring, constante `ADMIN_PEOPLE = "admin_people.json"`, `rotate` (~l.175-240)
- Create: `tests/test_admin_people_rekey.py`

**Interfaces:**
- Consumes: `_read_json`, `_Converter`, `_backup` (même module).
- Produces: `rotate` lit `admin_people.json` ; pour chaque personne portant `totp_secret_enc` : `convert(valeur, f"{ADMIN_PEOPLE}: person {id}")` ; le fichier entre dans la sauvegarde et est réécrit (`atomic_write_json(..., mode=0o600)`) avec les autres. Rien d'autre ne change (fichier absent : ignoré).

- [ ] **Step 1: Write the failing tests**

```python
"""ENCRYPTION_KEY rotation re-encrypts the personal TOTP secrets (PB-AUTH-01)."""

import json
import os
import stat

from cryptography.fernet import Fernet

from plugboard import secrets_rekey
from tests.test_secrets_rekey import NEW, OLD, _dec, _enc

PID = "ap_0123456789ab"


def _seed(data, value):
    data.mkdir(parents=True, exist_ok=True)
    (data / "admin_people.json").write_text(json.dumps({"people": [
        {"id": PID, "name": "person-one", "key_hash": "0" * 64, "totp_secret_enc": value}
    ]}))


def test_rotation_re_encrypts_personal_secrets(tmp_path):
    data = tmp_path / "data"
    _seed(data, _enc(OLD, "JBSWY3DPEHPK3PXP"))
    report = secrets_rekey.rotate(data, OLD, NEW)
    assert report.ok and report.written and report.rotated == 1
    stored = json.loads((data / "admin_people.json").read_text())["people"][0]
    assert _dec(NEW, stored["totp_secret_enc"]) == "JBSWY3DPEHPK3PXP"
    assert stored["key_hash"] == "0" * 64
    assert (report.backup_dir / "admin_people.json").exists()
    assert stat.S_IMODE(os.stat(data / "admin_people.json").st_mode) == 0o600


def test_an_unreadable_personal_secret_stops_the_rotation(tmp_path):
    data = tmp_path / "data"
    third = Fernet.generate_key().decode()
    _seed(data, _enc(third, "JBSWY3DPEHPK3PXP"))
    before = (data / "admin_people.json").read_text()
    report = secrets_rekey.rotate(data, OLD, NEW)
    assert not report.ok and report.unreadable == [f"admin_people.json: person {PID}"]
    assert (data / "admin_people.json").read_text() == before
```

- [ ] **Step 2: Run them to see them fail**

Run: `$PYTEST tests/test_admin_people_rekey.py`
Expected: FAIL — `report.rotated == 0`.

- [ ] **Step 3: Implement** (interfaces ci-dessus).

- [ ] **Step 4: Run the tests and the neighbours**

Run: `$PYTEST tests/test_admin_people_rekey.py tests/test_secrets_rekey.py tests/test_secret_unreadable.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugboard/secrets_rekey.py tests/test_admin_people_rekey.py
git commit -m "feat(security): rotate the personal TOTP secrets with ENCRYPTION_KEY (PB-AUTH-01)"
```

---

### Task 13: Les journaux masquent les clés personnelles

**Files:**
- Modify: `plugboard/log_redact.py` — `_PATTERNS`, après les motifs `plugboard` (~l.107-116)
- Create: `tests/test_admin_people_redaction.py`

**Interfaces:**
- Produces: entrée `("plugboard", re.compile(r"\bpba_[A-Za-z0-9_-]{32,}"), "pba_***REDACTED***")`.

- [ ] **Step 1: Write the failing tests**

```python
"""Logs never show a personal dashboard key nor its TOTP secret (PB-AUTH-01)."""

import secrets

from plugboard import admin_mfa
from plugboard.log_redact import redact


def test_a_personal_key_is_masked():
    key = "pba_" + secrets.token_urlsafe(32)
    out = redact(f"refused bearer {key} from 203.0.113.9")
    assert key not in out and "pba_***REDACTED***" in out


def test_the_otpauth_secret_is_masked():
    secret = admin_mfa.generate_secret()
    uri = admin_mfa.provisioning_uri(secret, account="person-one")
    assert secret not in redact(f"uri {uri}")
```

- [ ] **Step 2: Run them to see the first fail**

Run: `$PYTEST tests/test_admin_people_redaction.py`
Expected: `test_a_personal_key_is_masked` FAIL (clé en clair) ; `test_the_otpauth_secret_is_masked` passe déjà (motif `secret=`, vérifié pendant la rédaction du plan).

- [ ] **Step 3: Implement** le motif.

- [ ] **Step 4: Run the tests and the redaction neighbours**

Run: `$PYTEST tests/test_admin_people_redaction.py tests/test_log_redact.py tests/test_bug143_redaction_bounded_time.py tests/test_bug143_quota_excerpt_redaction.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugboard/log_redact.py tests/test_admin_people_redaction.py
git commit -m "feat(security): mask personal dashboard keys in logs (PB-AUTH-01)"
```

---

### Task 14: Les routes `/admin/people` restent hors du contrat client

**Files:**
- Create: `tests/test_admin_people_contract.py`
- Aucun changement de `plugboard/api_contract.py` ni de `docs/api/*.json` (décision de l'orchestrateur, 18/11 : les routes admin ne sont pas dans le contrat client, `docs/api/README.md` l.12 ; `CONTRACT_VERSION` reste `3.0.1`).

**Interfaces:**
- Consumes: routes de la tâche 10.
- Produces: rien de nouveau ; la preuve que le contrat client ne bouge pas.

- [ ] **Step 1: Write the tests**

```python
"""The people routes: admin schema only, outside the client contract (PB-AUTH-01)."""

from plugboard import api_contract


def test_people_routes_are_in_the_admin_schema_only():
    from plugboard.app import app

    paths = app.openapi()["paths"]
    assert {"get", "post"} <= set(paths["/admin/people"])
    assert "delete" in paths["/admin/people/{person_id}"]
    people = {("GET", "/admin/people"), ("POST", "/admin/people"), ("DELETE", "/admin/people/{person_id}")}
    assert not people & api_contract.PUBLIC_ROUTES


def test_the_client_contract_version_does_not_move():
    assert api_contract.CONTRACT_VERSION == "3.0.1"
```

- [ ] **Step 2: Run the tests and the export checks**

Run: `$PYTEST tests/test_admin_people_contract.py tests/test_api_contract.py tests/test_contract_without_fastapi.py`, puis `cd /srv/dev/worktrees/plugboard/pbauth01 && PLUGBOARD_SKIP_DOTENV=1 /srv/dev/plugboard/.venv/bin/python scripts/export_contract.py --check && PLUGBOARD_SKIP_DOTENV=1 /srv/dev/plugboard/.venv/bin/python scripts/export_contract.py --mcp --check`
Expected: all PASS ; `docs/api/openapi.json: up to date`, `docs/api/mcp.json: up to date` (si un export n'est plus à jour, une route admin a fui dans le contrat : corriger la route, jamais l'export).

- [ ] **Step 3: Commit**

```bash
git add tests/test_admin_people_contract.py
git commit -m "test(api): the personal key routes stay out of the client contract (PB-AUTH-01)"
```

---

### Task 15: Tableau de bord — page People et écran de connexion

**Files:**
- Create: `static/people.js`
- Modify: `static/index.html` — lien de navigation après « API keys » (~l.69-71), section `page-people` après `page-keys` (~l.198), `<script src="/dashboard/people.js?v=1">` après `catalog_history.js`, `app.js?v=29` → `?v=30`
- Modify: `static/app.js` — `TAB_LOADERS` (~l.151) gagne `people: loadPeople` ; `function loadPeople()` à côté de `loadCatalogHistory` (~l.1286), même forme (`window.loadPeoplePage`)
- Modify: `plugboard/app.py` — `_LOGIN_HTML_TEMPLATE` (~l.7110-7113) : sous-titre `Enter your admin key or your personal key`, placeholder `Admin or personal key`
- Modify: `tests/e2e/conftest.py` — `"people"` ajouté à `DASHBOARD_TABS` après `"keys"` (seule modification permise d'un test existant)
- Create: `tests/test_admin_people_dashboard.py`, `tests/e2e/test_people.py`

**Interfaces:**
- Consumes: `GET/POST /admin/people`, `DELETE /admin/people/{id}` ; aides globales de `app.js` : `api`, `h`, `table`, `toast`, `confirmAction`, `emptyState`, `skeleton`, `_errorDetail`.
- Produces (identifiants HTML) : `#page-people`, `#btn-create-person`, `#create-person-form` (`hidden`), `#person-name`, `#btn-submit-person`, `#btn-cancel-person`, `#new-person-alert` (`hidden`) contenant `#new-person-key`, `#new-person-secret`, `#new-person-uri` (chacun un `<code>` suivi d'un bouton `Copy` portant `data-copy="<id>"`) et `#btn-person-copied` (« I have stored them »), `#people-table-container`.
- Comportement de `people.js` (IIFE, `"use strict"`, texte seulement via `h()`/`textContent`, comme `catalog_history.js`) :
  - tableau `Name | Key | Status | Created | Last used | Revoked | Actions` ; badges `pending` → `badge-orange`, `active` → `badge-green`, `revoked` → `badge-red` ; « par » : `admin-key` affiché `ADMIN_KEY`, `person:<id>` affiché par le nom de la liste (sinon l'id) ; bouton `Revoke` pour les clés non révoquées, après `confirmAction("Revoke the key of <name>? Their sessions end at once.", "Revoke")`.
  - création : `POST`, en cas d'erreur `toast(await _errorDetail(res), "error")` et rien d'affiché ; en cas de succès les trois valeurs dans leurs `<code>`, l'alerte visible, le formulaire caché, la liste rechargée.
  - `#btn-person-copied` vide les trois `textContent` et recache l'alerte ; changer d'onglet (rechargement de la page) fait de même.
  - jamais `localStorage`/`sessionStorage`, jamais `innerHTML` sauf `= ""`.
  - `window.loadPeoplePage` défini ; si `#page-people` est déjà actif au chargement, charger.

- [ ] **Step 1: Write the failing tests**

`tests/test_admin_people_dashboard.py` :

```python
"""The People page of the dashboard and the login screen (PB-AUTH-01). Static checks."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"


async def test_the_dashboard_has_the_people_tab(admin_client):
    admin_client.cookies.clear()
    await admin_client.post("/auth/admin/login", json={"key": "adminkey"})
    shell = (await admin_client.get("/dashboard")).text
    assert 'data-tab="people"' in shell and 'id="page-people"' in shell
    assert "/dashboard/people.js?v=" in shell and "/dashboard/app.js?v=30" in shell
    for element in ("new-person-key", "new-person-secret", "new-person-uri", "btn-person-copied"):
        assert f'id="{element}"' in shell
    js = await admin_client.get("/dashboard/people.js")
    assert js.status_code == 200 and "/admin/people" in js.text


def test_app_js_routes_the_people_tab():
    app_js = (STATIC / "app.js").read_text()
    assert "people: loadPeople" in app_js and "window.loadPeoplePage" in app_js


def test_people_js_writes_text_only_and_keeps_nothing():
    js = (STATIC / "people.js").read_text()
    assert "localStorage" not in js and "sessionStorage" not in js
    assert all(v.strip() == '""' for v in re.findall(r"innerHTML\s*=\s*([^;]+);", js))
    assert "btn-person-copied" in js and "confirmAction(" in js


def test_the_login_screen_names_personal_keys():
    from plugboard.app import _LOGIN_HTML_TEMPLATE

    assert "Enter your admin key or your personal key" in _LOGIN_HTML_TEMPLATE
    assert 'placeholder="Admin or personal key"' in _LOGIN_HTML_TEMPLATE
```

`tests/e2e/test_people.py` (navigateur, exécuté au jalon) :

```python
"""E2E — People tab: create a personal key, reveal it once, sign in with it, revoke it."""

from __future__ import annotations

import pytest

from plugboard import admin_mfa

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_create_reveal_once_sign_in_and_revoke(dashboard, browser, e2e_server):
    page = dashboard.page
    await dashboard.goto_tab("people")
    await page.click("#btn-create-person")
    await page.fill("#person-name", "e2e-person-one")
    await page.click("#btn-submit-person")
    await page.wait_for_selector("#new-person-alert:not(.hidden)", timeout=10_000)
    key = (await page.text_content("#new-person-key")) or ""
    secret = (await page.text_content("#new-person-secret")) or ""
    assert key.startswith("pba_") and secret
    assert (await page.text_content("#new-person-uri") or "").startswith("otpauth://totp/")
    await page.click("#btn-person-copied")
    await page.wait_for_selector("#new-person-alert.hidden")
    assert (await page.text_content("#new-person-key")) == ""
    row = page.locator("#people-table-container table tbody tr", has_text="e2e-person-one")
    await row.locator(".badge-orange", has_text="pending").wait_for(timeout=10_000)

    context = await browser.new_context()
    login = await context.new_page()
    await login.goto(f"{e2e_server}/login?logged_out=1")
    await login.fill("#k", key)
    await login.click("#b")
    await login.wait_for_selector("#m:visible")
    await login.fill("#m", admin_mfa._generate_code(secret))
    await login.click("#b")
    await login.wait_for_selector("#app:not(.hidden)", timeout=15_000)
    await context.close()

    await page.reload()
    await dashboard.goto_tab("people")
    row = page.locator("#people-table-container table tbody tr", has_text="e2e-person-one")
    await row.locator(".badge-green", has_text="active").wait_for(timeout=10_000)
    await row.get_by_role("button", name="Revoke").click()
    await page.locator(".confirm-modal").get_by_role("button", name="Revoke").click()
    await row.locator(".badge-red", has_text="revoked").wait_for(timeout=10_000)
    assert dashboard.js_errors == [], dashboard.js_errors
```

- [ ] **Step 2: Run the static tests to see them fail**

Run: `$PYTEST tests/test_admin_people_dashboard.py`
Expected: FAIL — `data-tab="people"` absent.

- [ ] **Step 3: Implement** la page, `people.js`, le routage dans `app.js`, l'écran de connexion et `DASHBOARD_TABS` (interfaces ci-dessus ; style existant : `card form-card`, `alert alert-success`, `key-display`, `btn btn-sm`).

- [ ] **Step 4: Run the static tests and the dashboard neighbours**

Run: `$PYTEST tests/test_admin_people_dashboard.py tests/test_admin_mobile_cards.py tests/test_admin_mobile_header.py tests/test_dashboard_design_tokens.py tests/test_catalog_history.py tests/test_login_mobile_sol.py tests/test_e2e_dashboard.py`
Expected: all PASS (les cas qui exigent un navigateur restent ignorés : Playwright n'est pas dans `/srv/dev/plugboard/.venv`). `tests/e2e/test_people.py` et la suite `tests/e2e` (dont `people` dans `DASHBOARD_TABS` : smoke, a11y, nav mobile) tournent **au jalon** ; ne pas l'écrire « vérifié » avant.

- [ ] **Step 5: Commit**

```bash
git add static/people.js static/index.html static/app.js plugboard/app.py tests/e2e/conftest.py tests/test_admin_people_dashboard.py tests/e2e/test_people.py
git commit -m "feat(dashboard): People page and personal key sign-in (PB-AUTH-01)"
```

---

### Task 16: Documentation, CHANGELOG, TODOS

**Files:**
- Modify: `docs/guide/monitoring.md` — phrase d'en-tête (l.5) ; nouvelle section `### Personal dashboard keys` après `### Admin API with MFA` ; section `## Admin audit log` (l.238)
- Modify: `docs/guide/getting-started.md` — « Open the dashboard » (login : `ADMIN_KEY` ou clé personnelle), tableau des pages (ligne People), tableau « Where your data lives » (`admin_people.json`)
- Modify: `docs/ARCHITECTURE.md:105` (liste des fichiers JSON : `admin_people.json`), `SECURITY.md` (une puce après « Admin MFA »)
- Modify: `CHANGELOG.md` (`## [Unreleased]`), `TODOS.md` (retirer `### Per-person dashboard keys` et le titre `## Auth` devenu vide)
- Create: `tests/test_docs_admin_people.py`

**Interfaces:**
- Contenu exigé de `### Personal dashboard keys` : créer (`curl -X POST /admin/people -d '{"name": …}'` avec `ADMIN_KEY` ou une session), la réponse unique (clé `pba_…`, secret, URI), comment la remettre à la personne (canal séparé pour la clé et le secret, ajout du secret dans une application d'authentification, premier code = confirmation, statut `pending` → `active`), se connecter (écran de connexion : clé puis code) ou appeler l'API (`Authorization: Bearer pba_…` + `X-Plugboard-Admin-TOTP`, toujours), lister, révoquer (`DELETE /admin/people/<id>`, sessions coupées à l'instant, idempotent), perte d'authenticator (révoquer et recréer), verrou (5 mauvais codes en une minute verrouillent cette clé 15 minutes, pas les autres), ce qu'une clé personnelle ne fait pas (`/v1/*`, routes `/admin/mfa` sauf `status`), effet d'un changement de MFA de `ADMIN_KEY` ou de « Log out everywhere » (toutes les sessions), store illisible (clés personnelles refusées, `ADMIN_KEY` intact).
- `## Admin audit log` : les champs `principal` (`admin-key`, `person:<id>`, vide = non enregistré : entrée antérieure, système ou requête non authentifiée) et `principal_name` ; les quatre nouvelles actions.
- CHANGELOG `[Unreleased]` : `### Added` (PB-AUTH-01, D-63 : clés, routes, page People, audit `principal`), `### Security` (TOTP propre exigé partout, verrou par personne, routes `/admin/mfa` réservées à `ADMIN_KEY`, masquage `pba_`, rotation `ENCRYPTION_KEY`), ligne `**API contract**: unchanged (3.0.1)` (routes admin documentées par le `/openapi.json` admin, hors contrat client), ligne `**Not verified**:` (remplie par l'exécutant avec ce qu'il n'a pas pu lancer).

- [ ] **Step 1: Write the failing tests**

```python
"""Docs of the personal dashboard keys (PB-AUTH-01)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_the_monitoring_guide_documents_personal_keys():
    text = _read("docs/guide/monitoring.md")
    assert "### Personal dashboard keys" in text
    for needle in ("/admin/people", "DELETE /admin/people/", "X-Plugboard-Admin-TOTP", "pba_", "principal"):
        assert needle in text


def test_the_data_table_lists_the_store():
    assert "`admin_people.json`" in _read("docs/guide/getting-started.md")


def test_the_todo_entry_is_gone():
    assert "### Per-person dashboard keys" not in _read("TODOS.md")


def test_unreleased_names_the_feature_and_the_contract():
    unreleased = _read("CHANGELOG.md").split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert "PB-AUTH-01" in unreleased and "3.1.0" not in unreleased
```

- [ ] **Step 2: Run them to see them fail**

Run: `$PYTEST tests/test_docs_admin_people.py`
Expected: FAIL — `### Personal dashboard keys` absent.

- [ ] **Step 3: Write the docs** (contenu exigé ci-dessus ; anglais ; aucun nom réel, exemples `person-one`, `<id>`).

- [ ] **Step 4: Run the tests and the docs neighbours**

Run: `$PYTEST tests/test_docs_admin_people.py tests/test_config_docs_coverage.py tests/test_docs_release_4144.py tests/test_runbooks_production.py`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/guide/monitoring.md docs/guide/getting-started.md docs/ARCHITECTURE.md SECURITY.md CHANGELOG.md TODOS.md tests/test_docs_admin_people.py
git commit -m "docs(auth): personal dashboard keys, how to hand one over and revoke it (PB-AUTH-01)"
```

---

## Jalon : validation complète (orchestrateur, après la tâche 16)

Une seule fois, avant la merge request (règle YB du 18/11) :

1. Charge : `uptime`, `free -g`, `ci etat` ; au plus 2 suites lourdes ensemble tant que la charge reste sous 10.
2. En parallèle : lint (`ruff check plugboard tests`, `ruff format --check plugboard tests`), `scripts/export_contract.py --check` et `--mcp --check`, `make secret-scan` ; bloc Python A `tests/test_[a-l]*.py` et bloc Python B `tests/test_[m-z]*.py` (deux suites lourdes) ; puis le couloir e2e `make test-e2e` (Playwright + Chromium, données et port propres), qui exécute `tests/e2e/test_people.py` et les onglets de `DASHBOARD_TABS`.
3. Échec : corriger, rejouer **seulement** ce qui a échoué, puis une dernière passe complète.
4. `ci lancer` une fois sur le commit final.
5. Nettoyage : sorties temporaires et `.playwright-mcp` supprimés.

# Répartiteur de charge, règles simples (PLU-9 lot 1, PLU-8) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Plafonner les requêtes simultanées par fournisseur (local à 1), passer au candidat suivant ou attendre 10 s puis `503 providers_busy`, mettre une route en pause après un 429, compter toute la charge, et faire passer les tours d'une même session l'un après l'autre.

**Architecture:** Deux petits modules en mémoire : `provider_admission` (plafond, baux, file d'attente FIFO bornée) et `route_pauses` (pauses après 429). `router.forward()` parcourt ses candidats avec une file (`deque`) au lieu d'une boucle `for`, sans ré-indenter le corps ; `forward_stream()` devient une enveloppe qui prend le bail et délègue à `_stream_route()` (l'ancien corps). Les sessions passent par `session_turns` (un `asyncio.Lock` par session) et une dépendance FastAPI à `yield` qui rend le tour après l'envoi de la réponse.

**Tech Stack:** Python 3.12, asyncio, FastAPI 0.143 (dépendance à `yield`, scope `"request"` : sortie après la réponse), aiohttp, tests pytest-asyncio (`asyncio_mode = auto`), `aioresponses`, httpx `AsyncClient`.

**Spec:** `docs/superpowers/specs/2026-11-11-repartiteur-charge-design.md` (étude : `docs/superpowers/specs/2026-11-11-etude-repartiteur-charge.md`).

## Global Constraints

- Réglages : `PLUGBOARD_LOCAL_PROVIDER_MAX_INFLIGHT` (entier, défaut `1`, minimum `0`, `0` = sans plafond) ; `PLUGBOARD_PROVIDER_WAIT_S` (réel, défaut `10`, minimum `0`, `0` = 503 tout de suite). Lus à l'import par `envcompat.get_int` / `get_float`.
- Champ fournisseur `max_concurrency` : entier ≥ 0 (pas un booléen), prioritaire ; `0` = sans plafond ; absent ou invalide = défaut selon le type.
- Local = `provider_type == "sovereign"`, ou `base_url` loopback / privée / `localhost` (`billing._is_local_host`), ou Ollama (`billing._is_ollama`) en `http://`. Un CLI (`auth_source` `subprocess:`) n'est jamais local.
- Pauses sans durée : `30`, `60`, `300` s (429 consécutifs sur la route ; remis à zéro au succès). Clé de route = `router._circuit_key(provider, real_model)`.
- Saturé : exception `router.ProvidersBusyError`, réponse `503`, `Retry-After: 5`, corps `{"detail", "code": "providers_busy", "retry_after": 5}`. Contrat d'API `1.4.0`.
- Inchangés : `PLUGBOARD_MAX_INFLIGHT_CHAT`, `PLUGBOARD_INFLIGHT_WAIT_S`, `PLUGBOARD_SUBPROCESS_LIMIT`, `PLUGBOARD_RETRY_DELAYS` (5xx), `PLUGBOARD_LB_*`, `PLUGBOARD_SESSION_*`, la règle du disjoncteur (D-7 : un fournisseur plein n'est ni une panne ni une erreur de métrique), `route_guard` appelé sur chaque candidat **avant** son bail, `404 model_not_found` avant tout bail, `_unless_client_gone` (BUG-30).
- Sessions : seulement avec un identifiant (`X-LLM-Session-ID` ou `session_id` du corps) et un client ; pas de 409 ; aucun nouveau réglage.
- Tests sans réseau ni inférence : `aioresponses`, faux CLI par `monkeypatch.setattr(cli_subprocess, "complete", fake)`. Jamais tout `tests/`.
- Commande de test, notée `PYTEST` ci-dessous, lancée depuis `/srv/dev/worktrees/plugboard/etude-lb` :
  `env TMPDIR=/tmp/claude-1001/-srv-dev-plugboard/a5d91e11-1edb-4748-99e2-79712c823769/scratchpad/ PLUGBOARD_SKIP_DOTENV=1 /srv/dev/plugboard/.venv/bin/python -m pytest -q -p no:cacheprovider <fichiers>`
- Avant chaque commit : `/srv/dev/plugboard/.venv/bin/ruff format <fichiers modifiés>` puis `/srv/dev/plugboard/.venv/bin/ruff check plugboard/ tests/`.
- Docs en anglais, entrées CHANGELOG sous `## [Unreleased]` (sections `Added` / `Changed` / `Fixed`). Conventional Commits, un commit par tâche, message terminé par les lignes d'attribution de la session qui exécute. Ni `git push`, ni `ci`, ni docker.
- **Prérequis PLU-7** : si `feat/catalog-dns-pinning` est fusionnée dans `main`, rebaser `feat/repartiteur-charge` dessus avant la tâche 2. Zones de conflit : ligne `from plugboard import envcompat, metrics` du routeur (`netguard`), tête de la boucle de `forward()` (`session_for`), voie HTTP de `forward_stream()`, fin de `ProviderUpdate` (`egress_guard`), `[Unreleased]`. `ProviderEgressRefused` reste une `ProviderNetworkError` (502) ; `ProvidersBusyError` n'en hérite pas.

## Review Focus

1. Client qui raccroche au milieu d'un flux HTTP (après le premier fragment) : le bail revient. Test : tâche 2, `test_stream_closed_early_frees_its_slot`, et une assertion ajoutée à `tests/test_inflight_disconnect.py`.
2. Plafond abaissé (`max_concurrency` modifié) pendant que des requêtes tournent : la file ne dépasse pas le nouveau plafond. Test : tâche 1, `test_lowered_cap_is_respected_when_slots_come_back`.
3. Ollama Cloud (`https://ollama.com`) n'est pas plafonné ; un Ollama joint par un nom Docker (`http://ollama:11434`) l'est. Test : tâche 1, paramètres de `test_local_providers_are_capped_at_one_by_default` et `test_other_providers_have_no_cap_by_default`.
4. Une route préfixée seule et en pause reçoit quand même la requête (la pause ne fait que reclasser). Test : tâche 4, `test_paused_single_route_is_still_called`.
5. Même identifiant de session sous deux clés clientes : aucune attente croisée. Test : tâche 5, `test_other_sessions_and_other_clients_do_not_wait`.

## Fichiers

| Fichier | Rôle |
|---|---|
| `plugboard/provider_admission.py` (nouveau) | Plafond par fournisseur, baux, attente FIFO bornée |
| `plugboard/route_pauses.py` (nouveau) | Pauses par route après 429 |
| `plugboard/session_turns.py` (nouveau) | Un tour à la fois par `(client_id, session_id)` |
| `plugboard/router.py` | Baux sur toutes les voies, file des candidats, ordre, règle 429, `ProvidersBusyError` |
| `plugboard/app.py` | Réponse 503, dépendance de session, tours streamés enregistrés, champ `max_concurrency` |
| `plugboard/api_contract.py`, `docs/api/openapi.json`, `docs/api/mcp.json` | Contrat 1.4.0 |
| `tests/conftest.py` | Remise à zéro des baux, pauses et disjoncteurs à chaque test |
| Docs | `docs/guide/configuration.md`, `clients-and-keys.md`, `providers-and-subscriptions.md`, `troubleshooting.md`, `docs/api/README.md`, `docs/ARCHITECTURE.md`, `CHANGELOG.md` |

---

### Task 1 : module `provider_admission`

**Files:**
- Create: `plugboard/provider_admission.py`
- Create: `tests/test_provider_admission.py`
- Modify: `tests/conftest.py` (nouvelle fixture autouse, après `_restore_process_state`)
- Modify: `docs/guide/configuration.md` (table « Routing », après `PLUGBOARD_LB_INFLIGHT_WEIGHT`)

**Interfaces:**
- Consumes: `plugboard.billing._is_local_host(base_url: str) -> bool`, `plugboard.billing._is_ollama(provider) -> bool`, `envcompat.get_int/get_float`.
- Produces :
  - `LOCAL_DEFAULT: int`, `WAIT_S: float`
  - `is_local(provider: dict) -> bool`, `max_inflight(provider: dict) -> int` (0 = sans plafond)
  - `in_flight(provider_id: str) -> int`, `waiting(provider_id: str) -> int` (attentes vivantes)
  - `try_acquire(provider: dict) -> Optional[Lease]`
  - `async wait_acquire(providers: Sequence[dict], timeout: float) -> Optional[Lease]`
  - `class Lease` : `provider_id: str`, `release() -> None` (idempotent), gestionnaire de contexte synchrone (`with lease:` rend le bail)
  - `reset() -> None` ; état `_active: Dict[str, int]`, `_caps: Dict[str, int]`, `_waiters: Dict[str, Deque[asyncio.Future]]`

- [ ] **Step 1 : écrire les tests qui échouent** (`tests/test_provider_admission.py`)

```python
import asyncio

import pytest

from plugboard import provider_admission as pa

LOCAL = {"id": "ollama", "base_url": "http://127.0.0.1:11434"}
CLOUD = {"id": "openai", "base_url": "https://api.openai.com"}


@pytest.mark.parametrize("provider", [
    {"id": "x", "base_url": "https://llm.example", "provider_type": "sovereign"},
    {"id": "x", "base_url": "http://127.0.0.1:8080"},
    {"id": "x", "base_url": "http://localhost:1234"},
    {"id": "x", "base_url": "http://192.168.1.20:8000"},
    {"id": "x", "base_url": "http://[::1]:11434"},
    {"id": "ollama-box", "base_url": "http://ollama:11434"},
    {"id": "gpu", "base_url": "http://gpu.lan:11434"},
], ids=["sovereign", "loopback", "localhost", "private", "ipv6-loopback", "ollama-dns", "ollama-port"])
def test_local_providers_are_capped_at_one_by_default(provider):
    assert pa.is_local(provider)
    assert pa.max_inflight(provider) == 1


@pytest.mark.parametrize("provider", [
    CLOUD,
    {"id": "ollama-cloud", "base_url": "https://ollama.com"},
    {"id": "cli", "base_url": "subprocess://claude", "auth_source": "subprocess:claude"},
    {"id": "x", "base_url": "http://127.0.0.1.nip.io:8080"},
], ids=["cloud", "ollama-cloud", "cli", "dns-lookalike"])
def test_other_providers_have_no_cap_by_default(provider):
    assert not pa.is_local(provider)
    assert pa.max_inflight(provider) == 0


def test_max_concurrency_field_wins_over_the_default():
    assert pa.max_inflight({**CLOUD, "max_concurrency": 3}) == 3
    assert pa.max_inflight({**LOCAL, "max_concurrency": 0}) == 0
    assert pa.max_inflight({**LOCAL, "max_concurrency": 4}) == 4


@pytest.mark.parametrize("bad", ["2", True, -1, 1.5, None])
def test_invalid_max_concurrency_keeps_the_default(bad):
    assert pa.max_inflight({**LOCAL, "max_concurrency": bad}) == 1
    assert pa.max_inflight({**CLOUD, "max_concurrency": bad}) == 0


def test_local_default_follows_the_setting(monkeypatch):
    monkeypatch.setattr(pa, "LOCAL_DEFAULT", 2)
    assert pa.max_inflight(LOCAL) == 2
    monkeypatch.setattr(pa, "LOCAL_DEFAULT", 0)
    assert pa.max_inflight(LOCAL) == 0


def test_capped_provider_refuses_a_lease_until_one_is_released():
    first = pa.try_acquire(LOCAL)
    assert first is not None and first.provider_id == "ollama"
    assert pa.try_acquire(LOCAL) is None
    assert pa.in_flight("ollama") == 1
    first.release()
    first.release()  # idempotent
    assert pa.in_flight("ollama") == 0 and pa._active == {}
    pa.try_acquire(LOCAL).release()


def test_uncapped_provider_counts_every_lease():
    leases = [pa.try_acquire(CLOUD) for _ in range(3)]
    assert all(leases) and pa.in_flight("openai") == 3
    with leases[0]:
        pass
    assert pa.in_flight("openai") == 2
    for lease in leases[1:]:
        lease.release()
    assert pa._active == {}


async def test_freed_slot_goes_to_the_waiter_not_to_a_newcomer():
    held = pa.try_acquire(LOCAL)
    waiter = asyncio.create_task(pa.wait_acquire([LOCAL], 5))
    await asyncio.sleep(0)
    assert pa.waiting("ollama") == 1
    assert pa.try_acquire(LOCAL) is None
    held.release()
    lease = await waiter
    assert lease.provider_id == "ollama" and pa.in_flight("ollama") == 1
    lease.release()
    assert pa._active == {}


async def test_waiters_are_served_in_arrival_order():
    held = pa.try_acquire(LOCAL)
    first = asyncio.create_task(pa.wait_acquire([LOCAL], 5))
    await asyncio.sleep(0)
    second = asyncio.create_task(pa.wait_acquire([LOCAL], 5))
    await asyncio.sleep(0)
    held.release()
    lease = await first
    assert not second.done()
    lease.release()
    (await second).release()
    assert pa._active == {}


async def test_wait_gives_up_after_its_timeout():
    held = pa.try_acquire(LOCAL)
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await pa.wait_acquire([LOCAL], 0.05) is None
    assert loop.time() - started >= 0.05
    assert pa.waiting("ollama") == 0
    held.release()
    assert pa.in_flight("ollama") == 0


async def test_zero_timeout_does_not_wait():
    held = pa.try_acquire(LOCAL)
    assert await pa.wait_acquire([LOCAL], 0) is None
    held.release()


async def test_cancelled_waiter_leaves_the_queue():
    held = pa.try_acquire(LOCAL)
    waiter = asyncio.create_task(pa.wait_acquire([LOCAL], 5))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    held.release()
    assert pa.in_flight("ollama") == 0


async def test_slot_granted_while_the_waiter_is_cancelled_is_given_back():
    held = pa.try_acquire(LOCAL)
    waiter = asyncio.create_task(pa.wait_acquire([LOCAL], 5))
    await asyncio.sleep(0)
    held.release()  # hands the slot to the waiter...
    waiter.cancel()  # ...which is cancelled before it runs
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert pa.in_flight("ollama") == 0


async def test_waiter_on_two_providers_takes_the_first_freed():
    other = {"id": "llama", "base_url": "http://10.0.0.5:8080"}
    a, b = pa.try_acquire(LOCAL), pa.try_acquire(other)
    waiter = asyncio.create_task(pa.wait_acquire([LOCAL, other], 5))
    await asyncio.sleep(0)
    b.release()
    lease = await waiter
    assert lease.provider_id == "llama"
    a.release()
    assert pa.in_flight("ollama") == 0  # not handed to the served waiter
    lease.release()


async def test_lowered_cap_is_respected_when_slots_come_back():
    roomy = {**LOCAL, "max_concurrency": 2}
    one, two = pa.try_acquire(roomy), pa.try_acquire(roomy)
    tight = {**LOCAL, "max_concurrency": 1}
    waiter = asyncio.create_task(pa.wait_acquire([tight], 5))
    await asyncio.sleep(0)
    one.release()  # 1 in flight = the new cap
    await asyncio.sleep(0)
    assert not waiter.done() and pa.in_flight("ollama") == 1
    two.release()
    lease = await waiter
    assert pa.in_flight("ollama") == 1
    lease.release()
```

- [ ] **Step 2 : vérifier l'échec** — `PYTEST tests/test_provider_admission.py` → erreur `ImportError: cannot import name 'provider_admission'`.

- [ ] **Step 3 : écrire `plugboard/provider_admission.py`**

Docstring du module : plafond par fournisseur (PLU-9, D-22/D-23), un bail par requête en cours sur toutes les voies, état en mémoire d'un seul processus. `LOCAL_DEFAULT = envcompat.get_int("PLUGBOARD_LOCAL_PROVIDER_MAX_INFLIGHT", default=1, minimum=0)`, `WAIT_S = envcompat.get_float("PLUGBOARD_PROVIDER_WAIT_S", default=10.0, minimum=0.0)`. `max_inflight` : `max_concurrency` s'il est un `int` (pas un `bool`) ≥ 0, sinon `LOCAL_DEFAULT if is_local(provider) else 0`. `is_local` : règle des Global Constraints. `in_flight`, `waiting`, `reset`, `Lease` (`__slots__`, `_held`) se déduisent des tests. Le cœur, qui fixe l'ordre FIFO et les courses avec l'annulation :

```python
def _live(pid: str) -> Optional[Deque["asyncio.Future[str]"]]:
    """The waiters of ``pid`` still waiting, oldest first (done ones dropped)."""
    queue = _waiters.get(pid)
    while queue and queue[0].done():
        queue.popleft()
    if not queue:
        _waiters.pop(pid, None)
        return None
    return queue


def _wake(pid: str) -> None:
    """Hand the free slots of ``pid`` to its oldest waiters."""
    cap = _caps.get(pid, 0)
    while (queue := _live(pid)) and (cap == 0 or _active.get(pid, 0) < cap):
        _active[pid] = _active.get(pid, 0) + 1
        queue.popleft().set_result(pid)


def _release(pid: str) -> None:
    left = _active.get(pid, 0) - 1
    if left > 0:
        _active[pid] = left
    else:
        _active.pop(pid, None)
    _wake(pid)


def try_acquire(provider):
    pid = provider["id"]
    cap = _caps[pid] = max_inflight(provider)
    if cap and (_active.get(pid, 0) >= cap or _live(pid)):
        return None  # full, or others queued first
    _active[pid] = _active.get(pid, 0) + 1
    return Lease(pid)


async def wait_acquire(providers, timeout):
    if timeout <= 0 or not providers:
        return None
    fut = asyncio.get_running_loop().create_future()
    for provider in providers:
        _caps[provider["id"]] = max_inflight(provider)
        _waiters.setdefault(provider["id"], deque()).append(fut)
    for provider in providers:
        if not fut.done():
            _wake(provider["id"])
    try:
        async with asyncio.timeout(timeout):
            pid = await fut
    except BaseException as exc:
        if fut.done() and not fut.cancelled():
            _release(fut.result())  # granted as we gave up: pass it on
        else:
            fut.cancel()
        for provider in providers:
            _live(provider["id"])
        if isinstance(exc, TimeoutError):
            return None
        raise
    return Lease(pid)
```

- [ ] **Step 4 : remise à zéro dans `tests/conftest.py`**

```python
@pytest.fixture(autouse=True)
def _reset_load_state():
    """Provider leases start empty in every test (PLU-9)."""
    from plugboard import provider_admission

    provider_admission.reset()
    yield
    provider_admission.reset()
```

- [ ] **Step 5 : documenter** dans `docs/guide/configuration.md`, table « Routing », deux lignes : `PLUGBOARD_LOCAL_PROVIDER_MAX_INFLIGHT` | `1` | concurrent requests a local provider (Ollama, `sovereign`, loopback or private address) takes when its record sets no `max_concurrency`; `0` means no cap. `PLUGBOARD_PROVIDER_WAIT_S` | `10` | how long a request waits when every candidate provider is at its cap, before `503 providers_busy`; `0` answers at once.

- [ ] **Step 6 : vérifier** — `PYTEST tests/test_provider_admission.py tests/test_config_docs_coverage.py` → tout passe.

- [ ] **Step 7 : commit** — `git add plugboard/provider_admission.py tests/test_provider_admission.py tests/conftest.py docs/guide/configuration.md` ; `git commit -m "feat(lb): provider concurrency leases with a bounded FIFO wait"`.

---

### Task 2 : un bail sur chaque voie, fournisseur plein → suivant

**Files:**
- Modify: `plugboard/router.py` — import (`:32`, plus `from collections import deque`), `ProvidersBusyError` après `ProviderNetworkError` (`:97-106`), suppression de `_IN_FLIGHT_BY_PROVIDER` (`:303`) et `_track_in_flight` (`:319-330`), `_provider_lb_score` (`:333-345`), `forward()` (`:734-969`), `forward_stream()` (`:972-1000`)
- Modify (ligne `monkeypatch.setattr(router, "_IN_FLIGHT_BY_PROVIDER", {})` supprimée, la fixture autouse suffit) : `tests/test_alerts_provider_faults.py:52`, `tests/test_cli_terminal_error_exit0.py:101`, `tests/test_circuit_breaker_provider_faults.py:45`, `tests/test_no_retry_context_too_long.py:58`, `tests/test_circuit_breaker_route_key.py:34`, `tests/test_quota_exhausted_429.py:47,695`, `tests/test_router_network_fallback.py:45`
- Modify: `tests/test_router.py:147-151,170-174` → `monkeypatch.setattr(provider_admission, "_active", {...mêmes valeurs...})`
- Modify: `tests/test_coverage_gaps.py:210-233` — supprimer `test_track_in_flight_cleanup` et `test_track_in_flight_nested` (code retiré, couvert par la tâche 1)
- Modify: `tests/test_inflight_disconnect.py` — dans `test_hang_up_before_http_answer_frees_slot_and_cancels_upstream`, ajouter `assert provider_admission._active == {}`
- Create: `tests/test_router_admission.py`
- Modify: `docs/guide/configuration.md` (ligne `PLUGBOARD_LB_INFLIGHT_WEIGHT`), `CHANGELOG.md`

**Interfaces:**
- Consumes: tâche 1 (`try_acquire`, `in_flight`, `Lease`).
- Produces :
  - `class ProvidersBusyError(RuntimeError)` : `provider_ids: List[str]`, attribut de classe `retry_after = 5`, message `"All candidate providers are busy: <ids séparés par ', '>"` (sans le mot « timeout »).
  - `forward()` : variables locales `queue: deque`, `busy: List[tuple]` (la tâche 3 y branche l'attente).
  - `forward_stream(body, timeout=_TIMEOUT, *, route_guard=None)` (signature inchangée) et `async def _stream_route(body: Dict[str, Any], provider: Dict[str, Any], real_model: str, timeout: float) -> AsyncIterator[bytes]`.

- [ ] **Step 1 : écrire les tests qui échouent** (`tests/test_router_admission.py`)

```python
import asyncio
import json

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from plugboard import circuit_breaker, cli_subprocess, provider_admission, router
from tests.factories import FAKE_COMPLETION, FAKE_STREAM_BODY, PROVIDER_A, PROVIDER_B

URL_A = "http://fake-llm.test/v1/chat/completions"
URL_B = "http://other-llm.test/v1/chat/completions"
BARE = "fake:model-v1"  # both providers are candidates
ONLY_A = "test-provider:fake:model-v1"
CLI = {"id": "pbtest-cli", "name": "CLI test", "base_url": "subprocess://claude",
       "bearer_token": "(via CLI)", "default_model": "model-x",
       "auth_source": "subprocess:claude", "is_active": True,
       "created_at": "2026-01-01T00:00:00+00:00"}


@pytest.fixture
def capped_a(_providers_file, monkeypatch):
    """test-provider takes one request at a time and comes first."""
    a = {**PROVIDER_A, "max_concurrency": 1}
    _providers_file.write_text(json.dumps({"providers": [a, PROVIDER_B, CLI]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(router.metrics, "get_stats", lambda: {
        "test-provider": {"latency_avg": 0.1, "requests": 10, "errors": 0},
        "other-provider": {"latency_avg": 2.0, "requests": 10, "errors": 0}})
    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    monkeypatch.setattr(provider_admission, "WAIT_S", 0.0)
    monkeypatch.setattr(circuit_breaker, "_circuits", {})
    return a


def _calls(mock, url):
    return len(mock.requests.get(("POST", URL(url)), []))


def _body(model, **extra):
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}


async def _until(predicate, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never met")


async def test_full_provider_hands_the_request_to_the_next(capped_a):
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        mock.post(URL_B, payload=FAKE_COMPLETION, repeat=True)
        await router.forward(_body(BARE))
        assert _calls(mock, URL_A) == 0 and _calls(mock, URL_B) == 1
    assert router.served_by()[0]["id"] == "other-provider"
    assert circuit_breaker._circuits[ONLY_A].failures == 0  # full is not a failure (D-7)
    held.release()
    assert provider_admission._active == {}


async def test_every_candidate_full_raises_providers_busy(capped_a):
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        with pytest.raises(router.ProvidersBusyError) as err:
            await router.forward(_body(ONLY_A))
        assert _calls(mock, URL_A) == 0
    assert err.value.provider_ids == ["test-provider"]
    assert err.value.retry_after == 5
    held.release()


@pytest.mark.parametrize("answer", [
    {"payload": FAKE_COMPLETION},
    {"status": 500, "body": "down"},
    {"exception": asyncio.TimeoutError()},
    {"exception": aiohttp.ClientConnectionError("refused")},
], ids=["ok", "http-500", "timeout", "network"])
async def test_slot_is_released_whatever_the_outcome(capped_a, answer):
    with aioresponses() as mock:
        mock.post(URL_A, repeat=True, **answer)
        try:
            await router.forward(_body(ONLY_A))
        except Exception:
            pass
    assert provider_admission._active == {}


async def test_cli_call_counts_in_flight_and_frees_its_slot(capped_a, monkeypatch):
    gate = asyncio.Event()

    async def fake_complete(kind, body, *, timeout_s=None):
        await gate.wait()
        return {**FAKE_COMPLETION}

    monkeypatch.setattr(cli_subprocess, "complete", fake_complete)
    task = asyncio.create_task(router.forward(_body("pbtest-cli:model-x")))
    await _until(lambda: provider_admission.in_flight("pbtest-cli") == 1)
    gate.set()
    await task
    assert provider_admission._active == {}


async def test_stream_holds_its_slot_until_the_last_chunk(capped_a):
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        gen = router.forward_stream(_body(ONLY_A, stream=True))
        assert await gen.__anext__()
        assert provider_admission.in_flight("test-provider") == 1
        async for _ in gen:
            pass
    assert provider_admission._active == {}


async def test_stream_closed_early_frees_its_slot(capped_a):
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        gen = router.forward_stream(_body(ONLY_A, stream=True))
        await gen.__anext__()
        await gen.aclose()
    assert provider_admission._active == {}


async def test_full_stream_route_raises_providers_busy(capped_a):
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        with pytest.raises(router.ProvidersBusyError):
            await router.forward_stream(_body(ONLY_A, stream=True)).__anext__()
        assert _calls(mock, URL_A) == 0
    held.release()


async def test_refused_route_is_checked_before_its_slot(capped_a):
    held = provider_admission.try_acquire(capped_a)  # the only slot is taken

    def guard(provider, model):
        raise PermissionError("not allowed")

    with pytest.raises(PermissionError):  # not ProvidersBusyError
        await router.forward(_body(ONLY_A), route_guard=guard)
    with pytest.raises(PermissionError):
        await router.forward_stream(_body(ONLY_A, stream=True), route_guard=guard).__anext__()
    held.release()
    assert provider_admission._active == {}
```

- [ ] **Step 2 : vérifier l'échec** — `PYTEST tests/test_router_admission.py` → `AttributeError: module 'plugboard.router' has no attribute 'ProvidersBusyError'` et `test_full_provider_hands_the_request_to_the_next` qui appelle A.

- [ ] **Step 3 : `router.py`, compteur et exception** — `_provider_lb_score` lit `provider_admission.in_flight(provider_id)` ; supprimer `_IN_FLIGHT_BY_PROVIDER` et `_track_in_flight` (et `contextmanager` / `Iterator` s'ils ne servent plus) ; ajouter `ProvidersBusyError` (docstring : tous les candidats sont restés au plafond, D-23 ; ni disjoncteur, ni métrique d'erreur, ni alerte).

- [ ] **Step 4 : `forward()`, file des candidats sans ré-indenter le corps.** Remplacer seulement la tête de boucle et les deux points d'admission :

```python
    queue = deque(candidates)
    busy: List[tuple] = []
    while queue:
        provider, real_model = queue.popleft()
        if route_guard is not None:
            route_guard(provider, real_model)
        ...
        if auth_source.startswith("subprocess:"):
            lease = provider_admission.try_acquire(provider)
            if lease is None:
                busy.append((provider, real_model))
                continue
            try:
                ...  # inchangé
            except Exception as exc:
                ...  # inchangé, finit par continue
            finally:
                lease.release()
        ...  # auth, headers, payload, log : inchangés
        lease = provider_admission.try_acquire(provider)
        if lease is None:
            busy.append((provider, real_model))
            continue
        with lease:  # remplace `with _track_in_flight(provider["id"]):`, même indentation
            for attempt, delay in enumerate([0] + _DELAYS):
                ...
    if busy:
        raise ProvidersBusyError([p["id"] for p, _ in busy])
    raise last_err or RuntimeError("All providers failed")
```

- [ ] **Step 5 : `forward_stream()` devient une enveloppe.** Renommer l'ancien corps en `_stream_route(body, provider, real_model, timeout)` en retirant ses lignes de résolution (`model_str`, `resolve_provider_and_model`, `route_guard`) ; tout le reste (disjoncteur, voie CLI, voie HTTP) reste en place. Nouvelle `forward_stream` : `resolve_provider_and_model`, puis `route_guard`, puis `lease = provider_admission.try_acquire(provider)` ; `None` → `raise ProvidersBusyError([provider["id"]])` ; sinon `try: async with contextlib.aclosing(_stream_route(...)) as chunks: async for chunk in chunks: yield chunk` / `finally: lease.release()`.

- [ ] **Step 6 : mettre à jour les tests existants** listés dans **Files** (lignes `_IN_FLIGHT_BY_PROVIDER`, `_track_in_flight`, assertion `provider_admission._active == {}` dans `test_inflight_disconnect.py`).

- [ ] **Step 7 : docs et CHANGELOG** — `configuration.md` : la ligne `PLUGBOARD_LB_INFLIGHT_WEIGHT` dit que streamed requests and CLI calls count as in flight. `CHANGELOG.md` `[Unreleased]` : **Added** — a local provider (Ollama, `sovereign`, loopback or private `base_url`) takes one request at a time by default (`PLUGBOARD_LOCAL_PROVIDER_MAX_INFLIGHT`, `0` restores 4.8.0) and any provider takes its `max_concurrency`; a full provider hands a request without provider prefix to the next candidate. **Changed** — the in-flight count of the load-balancing score now includes streamed requests and CLI calls, so the order of candidates for a model without prefix can change.

- [ ] **Step 8 : vérifier** — `PYTEST tests/test_router_admission.py tests/test_provider_admission.py tests/test_router.py tests/test_router_network_fallback.py tests/test_inflight_disconnect.py tests/test_coverage_gaps.py tests/test_quota_exhausted_429.py tests/test_alerts_provider_faults.py tests/test_cli_terminal_error_exit0.py tests/test_circuit_breaker_provider_faults.py tests/test_circuit_breaker_route_key.py tests/test_no_retry_context_too_long.py tests/test_config_docs_coverage.py` → tout passe ; `grep -rn "_IN_FLIGHT_BY_PROVIDER\|_track_in_flight" plugboard tests` → rien.

- [ ] **Step 9 : commit** — `git commit -m "feat(lb): cap concurrent requests per provider on every route"`.

---

### Task 3 : attente bornée, `503 providers_busy`, champ `max_concurrency`

**Files:**
- Modify: `plugboard/router.py` — `forward()` (boucle de la tâche 2), `forward_stream()` (prise du bail)
- Modify: `plugboard/app.py` — `_providers_busy_response` après `_quota_error_response` (`:1717-1733`) ; branchement en tête des trois chaînes `except Exception as exc:` (`:2125` flux CLI, `:2307` flux HTTP, `:2444` non streamé) ; `ProviderUpdate` (`:3540-3557`)
- Modify: `plugboard/api_contract.py` — `CONTRACT_VERSION` (`:33`), `ErrorResponse` (`:446-462`), `_CHAT_ERRORS["503"]` (`:536`) ; régénérer `docs/api/openapi.json` ; `docs/api/mcp.json:3`
- Modify: `tests/test_router_admission.py`
- Modify: `docs/guide/clients-and-keys.md`, `docs/guide/providers-and-subscriptions.md`, `docs/guide/troubleshooting.md:90`, `docs/api/README.md`, `docs/ARCHITECTURE.md`, `CHANGELOG.md`

**Interfaces:**
- Consumes: tâche 1 (`wait_acquire`, `waiting`, `WAIT_S`), tâche 2 (`ProvidersBusyError`, `queue`, `busy`).
- Produces : `app._providers_busy_response(exc: ProvidersBusyError, client_id: Optional[str]) -> JSONResponse` ; `ProviderUpdate.max_concurrency: Optional[int] = Field(default=None, ge=0, le=1000)`.

- [ ] **Step 1 : écrire les tests qui échouent** (ajouts à `tests/test_router_admission.py`)

```python
AUTH = {"Authorization": "Bearer testkey"}


async def test_request_waits_for_a_slot_then_is_served(capped_a, monkeypatch):
    monkeypatch.setattr(provider_admission, "WAIT_S", 2.0)
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        task = asyncio.create_task(router.forward(_body(ONLY_A)))
        await _until(lambda: provider_admission.waiting("test-provider") == 1)
        assert _calls(mock, URL_A) == 0
        held.release()
        await asyncio.wait_for(task, 2)
        assert _calls(mock, URL_A) == 1
    assert provider_admission._active == {}


async def test_wait_is_bounded_then_providers_busy(capped_a, monkeypatch):
    monkeypatch.setattr(provider_admission, "WAIT_S", 0.1)
    held = provider_admission.try_acquire(capped_a)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(router.ProvidersBusyError):
        await router.forward(_body(ONLY_A))
    assert 0.1 <= loop.time() - started < 2
    held.release()


async def test_wait_covers_every_full_candidate(capped_a, _providers_file, monkeypatch):
    b = {**PROVIDER_B, "max_concurrency": 1}
    _providers_file.write_text(json.dumps({"providers": [capped_a, b]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(provider_admission, "WAIT_S", 2.0)
    held_a, held_b = provider_admission.try_acquire(capped_a), provider_admission.try_acquire(b)
    with aioresponses() as mock:
        mock.post(URL_B, payload=FAKE_COMPLETION, repeat=True)
        task = asyncio.create_task(router.forward(_body(BARE)))
        await _until(lambda: provider_admission.waiting("other-provider") == 1)
        held_b.release()
        await asyncio.wait_for(task, 2)
    assert router.served_by()[0]["id"] == "other-provider"
    held_a.release()
    assert provider_admission._active == {}


async def test_streamed_request_waits_for_its_slot(capped_a, monkeypatch):
    monkeypatch.setattr(provider_admission, "WAIT_S", 2.0)
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        gen = router.forward_stream(_body(ONLY_A, stream=True))
        first = asyncio.ensure_future(gen.__anext__())
        await _until(lambda: provider_admission.waiting("test-provider") == 1)
        held.release()
        assert await asyncio.wait_for(first, 2)
        await gen.aclose()
    assert provider_admission._active == {}


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_chat_answers_503_providers_busy(client, capped_a, path, stream):
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        resp = await client.post(path, headers=AUTH, json=_body(ONLY_A, stream=stream, cache=False))
        assert _calls(mock, URL_A) == 0
    held.release()
    assert resp.status_code == 503, resp.text
    assert resp.headers["Retry-After"] == "5"
    body = resp.json()
    assert body["code"] == "providers_busy" and body["retry_after"] == 5
    assert body["detail"].startswith("Every provider that serves this model is busy")


async def test_client_leaving_while_waiting_frees_its_place(client, capped_a, monkeypatch):
    from tests.test_inflight_disconnect import _Abandoned

    monkeypatch.setattr(provider_admission, "WAIT_S", 5.0)
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        gone = _Abandoned("/v1/chat/completions", _body(ONLY_A, cache=False))
        await _until(lambda: provider_admission.waiting("test-provider") == 1)
        await gone.hang_up()
        assert gone.status() == 499
        held.release()
        assert provider_admission._active == {}  # not handed to the gone request
        assert _calls(mock, URL_A) == 0
    await gone.close()


async def test_max_concurrency_is_a_provider_setting(client):
    from plugboard.config import get_provider

    resp = await client.put("/providers/test-provider", headers=AUTH, json={"max_concurrency": 2})
    assert resp.status_code == 200, resp.text
    assert resp.json()["max_concurrency"] == 2
    assert provider_admission.max_inflight(get_provider("test-provider")) == 2
    bad = await client.put("/providers/test-provider", headers=AUTH, json={"max_concurrency": -1})
    assert bad.status_code == 422
```

- [ ] **Step 2 : vérifier l'échec** — `PYTEST tests/test_router_admission.py` → les tests d'attente lèvent `ProvidersBusyError` tout de suite, le test HTTP reçoit `502`, le `PUT` ignore le champ.

- [ ] **Step 3 : attente dans `forward()`.** La boucle devient `while queue or busy:` ; en tête, quand `queue` est vide : échéance unique `deadline = loop.time() + provider_admission.WAIT_S` (posée à la première attente, partagée par les suivantes), `granted = await provider_admission.wait_acquire([p for p, _ in busy], deadline - loop.time())` ; `None` → `break` ; sinon remettre `busy` dans `queue`, le candidat de `granted.provider_id` en premier, et vider `busy`. Les deux points d'admission prennent `granted` quand il correspond au fournisseur, sinon `try_acquire` (petite fonction interne `_take(provider)` avec `nonlocal granted`). Après la boucle : `busy` non vide → `ProvidersBusyError` (les candidats restants sont tous pleins, même si un autre a échoué avant).

- [ ] **Step 4 : attente dans `forward_stream()`** — `lease = provider_admission.try_acquire(provider) or await provider_admission.wait_acquire([provider], provider_admission.WAIT_S)`.

- [ ] **Step 5 : `app.py`.** `_providers_busy_response` : `503`, en-tête `Retry-After: str(exc.retry_after)`, corps `{"detail": "Every provider that serves this model is busy: retry in 5 seconds.", "code": "providers_busy", "retry_after": 5}` ; pour `client_id == "admin"`, `detail` ajoute `" Busy providers: <ids>."`. Dans les trois chaînes, `if isinstance(exc, ProvidersBusyError): return _providers_busy_response(exc, client_id)` en premier. Importer `ProvidersBusyError` avec les autres erreurs du routeur. `ProviderUpdate.max_concurrency` comme dans **Interfaces** (rien d'autre : `update_provider` écrit déjà tout champ reçu).

- [ ] **Step 6 : contrat d'API.** `CONTRACT_VERSION = "1.4.0"` ; description de `ErrorResponse.code` : `model_not_found (chat, 404), providers_busy (chat, 503).` et la docstring en parle ; `_CHAT_ERRORS["503"]` : `"Provider timeout (retry_after), too many concurrent requests, or every candidate provider at its concurrency cap (code providers_busy, Retry-After 5)."`. Puis `/srv/dev/plugboard/.venv/bin/python scripts/export_contract.py` (écrit `docs/api/openapi.json`) et, à la main, `"contract_version": "1.4.0"` en ligne 3 de `docs/api/mcp.json` (`.venv-mcp` absent sur l'UAT).

- [ ] **Step 7 : docs.** `clients-and-keys.md` : dans « How limits are enforced », un paragraphe après la liste : *provider concurrency cap* (local à 1, `max_concurrency`, candidat suivant pour un modèle sans préfixe, attente `PLUGBOARD_PROVIDER_WAIT_S`, `503` `code` `providers_busy`, `Retry-After: 5`, un client qui raccroche quitte la file) ; ligne `503` de la table « Errors ». `docs/api/README.md` : ligne `503` de la table (`code` `providers_busy`, attendre `Retry-After`). `troubleshooting.md:90` : la cause « every provider for the model at its cap (`providers_busy`) ». `providers-and-subscriptions.md` : nouvelle section `## Concurrent requests per provider` avant `## Add providers from \`.env\` or scripts` (règle locale, `max_concurrency` par `PUT /providers/{id}` ou `providers.json`, `0` sans plafond, Ollama Cloud et adresses CGNAT non locales, CLI plafonnés par `PLUGBOARD_SUBPROCESS_LIMIT`). `ARCHITECTURE.md` étape 7 : une puce « each candidate takes a slot of its provider ; a full one passes to the next, all full waits then 503 ». `CHANGELOG.md` **Added** : bounded wait and `503 providers_busy`, API contract 1.4.0, `max_concurrency` editable by `PUT /providers/{id}`.

- [ ] **Step 8 : vérifier** — `PYTEST tests/test_router_admission.py tests/test_api_contract.py tests/test_contract_without_fastapi.py tests/test_api.py tests/test_inflight_disconnect.py tests/test_docs_links.py tests/test_config_docs_coverage.py` → tout passe ; `/srv/dev/plugboard/.venv/bin/python scripts/export_contract.py --check` → `up to date` ; `grep -n contract_version docs/api/mcp.json` → `1.4.0`.

- [ ] **Step 9 : commit** — `git commit -m "feat(lb): wait for a free provider, then 503 providers_busy"`.

---

### Task 4 : pause après 429, routes en pause ou disjoncteur ouvert en dernier

**Files:**
- Create: `plugboard/route_pauses.py`, `tests/test_route_pauses.py`
- Modify: `plugboard/router.py` — import de `CliSubprocessError` (`:35`) ; `_retry_quota_here` et `_order_routes` avant `forward()` ; `forward()` : lecture des disjoncteurs (`:725-726`), branche 429 (`:861-877`), après la boucle d'essais, `cb_success` (`:782`, `:899`), branche `except` CLI (`:788-818`) ; `_stream_route` : `except` CLI, `cb_success` (`:1060`, `:1165`), `raise _http_error` (`:1164`)
- Modify: `tests/conftest.py` (fixture `_reset_load_state`)
- Modify: `docs/guide/configuration.md` (ligne `PLUGBOARD_RETRY_DELAYS`), `docs/ARCHITECTURE.md` (étape 7), `CHANGELOG.md`

**Interfaces:**
- Consumes: `router._circuit_key`, `check_circuit(key) -> {"allowed", "state"}`, `ProviderQuotaError.retry_after`, `CliSubprocessError.quota/.retry_after`.
- Produces :
  - `route_pauses.STEPS_S = (30, 60, 300)`, `_now = time.monotonic` (remplaçable en test)
  - `pause(route: str, retry_after: Optional[int]) -> float` (secondes posées), `clear(route: str) -> None`, `paused_for(route: str) -> float` (0.0 sans pause), `reset() -> None` ; état `_pauses: Dict[str, Tuple[float, int]]` (fin, 429 consécutifs)
  - `router._retry_quota_here(retry_after: Optional[int], attempt: int) -> bool` : vrai s'il reste un délai `_DELAYS[attempt]` et que `retry_after` est `None` ou ≤ ce délai
  - `router._order_routes(candidates: List[tuple], open_routes: Set[str]) -> List[tuple]` : tri stable sur `(en_pause_ou_ouverte, temps_de_pause_restant, rang_d_origine)`

- [ ] **Step 1 : écrire les tests qui échouent** (`tests/test_route_pauses.py`)

```python
import json

import pytest
from aioresponses import aioresponses

from plugboard import circuit_breaker, cli_subprocess, route_pauses, router
from plugboard.cli_subprocess import CliSubprocessError
from tests.factories import FAKE_COMPLETION, PROVIDER_A, PROVIDER_B
from tests.test_router_admission import BARE, CLI, ONLY_A, URL_A, URL_B, _body, _calls


@pytest.fixture
def two_providers(_providers_file, monkeypatch):
    """test-provider first, other-provider next, a CLI; no caps, no retry delay."""
    _providers_file.write_text(json.dumps({"providers": [PROVIDER_A, PROVIDER_B, CLI]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(router.metrics, "get_stats", lambda: {
        "test-provider": {"latency_avg": 0.1, "requests": 10, "errors": 0},
        "other-provider": {"latency_avg": 2.0, "requests": 10, "errors": 0}})
    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    monkeypatch.setattr(circuit_breaker, "_circuits", {})


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(route_pauses, "_now", lambda: now[0])
    return now


def test_pause_follows_retry_after(clock):
    assert route_pauses.pause("p:m", 120) == 120
    clock[0] += 100
    assert route_pauses.paused_for("p:m") == pytest.approx(20)
    clock[0] += 21
    assert route_pauses.paused_for("p:m") == 0.0


def test_pause_without_retry_after_grows_30_60_300(clock):
    assert [route_pauses.pause("p:m", None) for _ in range(4)] == [30, 60, 300, 300]


def test_success_resets_the_steps(clock):
    route_pauses.pause("p:m", None)
    route_pauses.pause("p:m", None)
    route_pauses.clear("p:m")
    assert route_pauses.paused_for("p:m") == 0.0
    assert route_pauses.pause("p:m", None) == 30


def test_retry_quota_here_rule(monkeypatch):
    monkeypatch.setattr(router, "_DELAYS", [2, 5, 15])
    assert router._retry_quota_here(None, 0) is True
    assert router._retry_quota_here(2, 0) is True
    assert router._retry_quota_here(3, 0) is False
    assert router._retry_quota_here(5, 1) is True
    assert router._retry_quota_here(1, 3) is False  # no delay left


def test_paused_or_open_routes_go_last(clock):
    a, b, c, d = ({"id": x} for x in "abcd")
    route_pauses.pause("a:m", 100)
    route_pauses.pause("b:m", 10)
    ordered = router._order_routes([(a, "m"), (b, "m"), (c, "m"), (d, "m")], {"c:m"})
    assert [p["id"] for p, _ in ordered] == ["d", "c", "b", "a"]


async def test_long_retry_after_is_not_retried_and_pauses_the_route(two_providers):
    with aioresponses() as mock:
        mock.post(URL_A, status=429, body="slow down", headers={"Retry-After": "120"}, repeat=True)
        mock.post(URL_B, payload=FAKE_COMPLETION, repeat=True)
        await router.forward(_body(BARE))
        assert _calls(mock, URL_A) == 1  # no retry on the spot
        assert route_pauses.paused_for(ONLY_A) == pytest.approx(120, abs=1)
        await router.forward(_body(BARE))
        assert _calls(mock, URL_A) == 1 and _calls(mock, URL_B) == 2  # A went last


async def test_short_retry_after_is_retried_on_the_spot(two_providers):
    with aioresponses() as mock:
        mock.post(URL_A, status=429, body="slow down", headers={"Retry-After": "0"})
        mock.post(URL_A, payload=FAKE_COMPLETION)
        await router.forward(_body(ONLY_A))
        assert _calls(mock, URL_A) == 2
    assert route_pauses.paused_for(ONLY_A) == 0.0


async def test_429_without_retry_after_is_retried_as_before_then_paused(two_providers):
    with aioresponses() as mock:
        mock.post(URL_A, status=429, body="slow down", repeat=True)
        mock.post(URL_B, payload=FAKE_COMPLETION, repeat=True)
        await router.forward(_body(BARE))
        assert _calls(mock, URL_A) == 4  # 1 + PLUGBOARD_RETRY_DELAYS
    assert route_pauses.paused_for(ONLY_A) == pytest.approx(30, abs=1)


async def test_route_with_open_circuit_goes_last(two_providers):
    for _ in range(3):
        circuit_breaker.record_failure(ONLY_A)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        mock.post(URL_B, payload=FAKE_COMPLETION, repeat=True)
        await router.forward(_body(BARE))
        assert _calls(mock, URL_A) == 0 and _calls(mock, URL_B) == 1


async def test_paused_single_route_is_still_called(two_providers):
    route_pauses.pause(ONLY_A, 300)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        await router.forward(_body(ONLY_A))
        assert _calls(mock, URL_A) == 1
    assert route_pauses.paused_for(ONLY_A) == 0.0


async def test_cli_quota_pauses_the_route(two_providers, monkeypatch):
    async def quota(kind, body, *, timeout_s=None):
        raise CliSubprocessError("usage limit", quota=True, retry_after=600,
                                 provider_name="claude", model_id="model-x")

    monkeypatch.setattr(cli_subprocess, "complete", quota)
    with pytest.raises(CliSubprocessError):
        await router.forward(_body("pbtest-cli:model-x"))
    assert route_pauses.paused_for("pbtest-cli:model-x") == pytest.approx(600, abs=1)


async def test_streamed_429_pauses_the_route(two_providers):
    with aioresponses() as mock:
        mock.post(URL_A, status=429, body="slow down", headers={"Retry-After": "90"})
        with pytest.raises(router.ProviderQuotaError):
            await router.forward_stream(_body(ONLY_A, stream=True)).__anext__()
    assert route_pauses.paused_for(ONLY_A) == pytest.approx(90, abs=1)
```

- [ ] **Step 2 : vérifier l'échec** — `PYTEST tests/test_route_pauses.py` → `ImportError: cannot import name 'route_pauses'`.

- [ ] **Step 3 : `plugboard/route_pauses.py`** selon **Interfaces** : `pause` compte un 429 de plus sur la route, pose `retry_after` s'il est donné, sinon `STEPS_S[min(compte, 3) - 1]`, et renvoie la durée posée ; `clear` oublie la route. Docstring : la pause ne fait que reclasser la route (D-23 règle 3), elle ne refuse jamais une requête.

- [ ] **Step 4 : `router.py`.**
  - Disjoncteurs : la boucle `check_circuit` garde ses lectures et collecte `open_routes = {clé : not check_circuit(clé)["allowed"]}` ; puis `candidates = _order_routes(candidates, open_routes)` avant `deque(candidates)`. Commentaire mis à jour : un disjoncteur ouvert ne refuse rien, il place la route en dernier.
  - Branche retryable : après le test `request_rejected`, `if isinstance(last_err, ProviderQuotaError) and not _retry_quota_here(last_err.retry_after, attempt): break`.
  - Après le bloc `with lease:` : `if isinstance(last_err, ProviderQuotaError) and last_err.provider_id == provider["id"]: route_pauses.pause(_circuit_key(provider, real_model), last_err.retry_after)`.
  - Branche `except` CLI de `forward()` et de `_stream_route()` (dans leurs `try` qui avalent les erreurs) : `if isinstance(exc, CliSubprocessError) and exc.quota: route_pauses.pause(clé, exc.retry_after)`.
  - `_stream_route()` HTTP : `err = _http_error(...)`, `if isinstance(err, ProviderQuotaError): route_pauses.pause(circuit_key, err.retry_after)`, `raise err`.
  - À côté de chaque `cb_success(...)` (quatre endroits) : `route_pauses.clear(clé)`.

- [ ] **Step 5 : `tests/conftest.py`** — `_reset_load_state` reçoit `monkeypatch`, appelle aussi `route_pauses.reset()` avant et après, et `monkeypatch.setattr(circuit_breaker, "_circuits", {})` : un disjoncteur laissé ouvert par un test ne doit plus reclasser les routes du suivant.

- [ ] **Step 6 : docs et CHANGELOG.** `configuration.md`, ligne `PLUGBOARD_RETRY_DELAYS` : a `429` whose `Retry-After` exceeds the next delay is not retried on the spot ; after a final `429` the route is paused for `Retry-After`, else 30 s, 1 min, then 5 min, and is tried last. `ARCHITECTURE.md` étape 7 : la puce du disjoncteur dit « tried last » au lieu de « still sent first ». `CHANGELOG.md` : **Added** pause par route après un `429` HTTP ou un quota CLI, routes en pause ou à disjoncteur ouvert en dernier ; **Changed** un `429` dont le `Retry-After` dépasse le prochain délai n'est plus réessayé sur place (le client ou le candidat suivant l'a tout de suite, au lieu d'environ 22 s).

- [ ] **Step 7 : vérifier** — `PYTEST tests/test_route_pauses.py tests/test_router_admission.py tests/test_router.py tests/test_quota_exhausted_429.py tests/test_circuit_breaker_route_key.py tests/test_circuit_breaker_provider_faults.py tests/test_alerts_provider_faults.py tests/test_router_network_fallback.py tests/test_no_retry_context_too_long.py tests/test_http_provider_error_body.py tests/test_jobs_circuits.py tests/test_e2e_full_coverage.py` → tout passe.

- [ ] **Step 8 : commit** — `git commit -m "feat(lb): pause a route after a 429 and try it last"`.

---

### Task 5 : un tour à la fois par session, tours streamés enregistrés

**Files:**
- Create: `plugboard/session_turns.py`, `tests/test_session_turns.py`
- Modify: `plugboard/app.py` — `_session_turn_dep` et `_take_session_turn` après `_client_gone_response` (`:716-722`) ; décorateurs `/v1/chat/completions` (`:1873`) et `/v2/chat/completions` (`:2732`) ; fusion de session (`:1935-1938`) ; `_record_session_turn` remplace le bloc `:2492-2509` ; appel dans la voie CLI streamée (après `content = ...`, `:2246`) et dans `_stream_and_bill` (après `bill = _bill_streaming(...)`, `:2394`) ; `_bill_streaming` (`:1446-1508`) renvoie aussi `"text"`
- Modify: `docs/guide/clients-and-keys.md` (option `session_id`), `docs/api/README.md` (puce `session_id`), `docs/ARCHITECTURE.md` (étapes 4 et 9), `CHANGELOG.md`

**Interfaces:**
- Produces :
  - `session_turns.acquire(client_id: str, session_id: str) -> Awaitable[Callable[[], None]]` : attend son tour (FIFO, `asyncio.Lock`), renvoie une fonction de libération idempotente ; annulable pendant l'attente
  - `session_turns.waiting(client_id: str, session_id: str) -> int` (tours en attente derrière celui en cours) ; état `_turns: Dict[Tuple[str, str], List]` (`[verrou, utilisateurs]`, entrée supprimée au dernier)
  - `app._session_turn_dep(request)` (dépendance à `yield`, scope par défaut) : appelle `request.state.session_turn_release` s'il existe, après l'envoi de la réponse
  - `app._take_session_turn(request, client_id, session_id) -> None` : `request.state.session_turn_release = await session_turns.acquire(...)` (la libération est rangée dès l'obtention, jamais perdue par un `_ClientGone`)
  - `app._record_session_turn(client_id: Optional[str], session_id: Optional[str], messages: List[dict], reply: str) -> None` (non bloquant, journalise `session record error (non-fatal)`)

- [ ] **Step 1 : écrire les tests qui échouent** (`tests/test_session_turns.py`)

```python
import asyncio
import json

import pytest
from aioresponses import CallbackResult, aioresponses

from plugboard import session_turns
from tests.factories import FAKE_COMPLETION, FAKE_STREAM_BODY

AUTH = {"Authorization": "Bearer testkey"}  # admin: client_id "admin"
URL_A = "http://fake-llm.test/v1/chat/completions"
MODEL = "test-provider:fake:model-v1"


# ── session_turns ───────────────────────────────────────────────────────────

async def test_turns_of_one_session_run_one_after_the_other():
    release = await session_turns.acquire("c", "s")
    second = asyncio.create_task(session_turns.acquire("c", "s"))
    await asyncio.sleep(0)
    assert not second.done() and session_turns.waiting("c", "s") == 1
    release()
    (await second)()
    assert session_turns._turns == {}


async def test_waiting_turns_keep_their_order():
    release = await session_turns.acquire("c", "s")
    second = asyncio.create_task(session_turns.acquire("c", "s"))
    await asyncio.sleep(0)
    third = asyncio.create_task(session_turns.acquire("c", "s"))
    await asyncio.sleep(0)
    release()
    release_second = await second
    assert not third.done()
    release_second()
    (await third)()


async def test_other_sessions_and_other_clients_do_not_wait():
    release = await session_turns.acquire("c", "s")
    other_session = await asyncio.wait_for(session_turns.acquire("c", "t"), 0.1)
    other_client = await asyncio.wait_for(session_turns.acquire("d", "s"), 0.1)
    for done in (release, other_session, other_client):
        done()
    assert session_turns._turns == {}


async def test_cancelled_wait_leaves_the_queue():
    release = await session_turns.acquire("c", "s")
    second = asyncio.create_task(session_turns.acquire("c", "s"))
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    release()
    release()  # idempotent
    assert session_turns._turns == {}


# ── chat route ──────────────────────────────────────────────────────────────

@pytest.fixture
def session_db(tmp_path, monkeypatch):
    from plugboard import session_store

    monkeypatch.setattr(session_store, "_DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_INITIALISED", False)
    return session_store


def _answer(text):
    return {**FAKE_COMPLETION, "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": text}}]}


class _Upstream:
    """Fake provider: records each prompt; the first answer waits for `gate`."""

    def __init__(self):
        self.prompts, self.gate = [], asyncio.Event()

    async def __call__(self, url, **kwargs):
        self.prompts.append(kwargs["json"]["messages"])
        n = len(self.prompts)
        if n == 1:
            await self.gate.wait()
        return CallbackResult(payload=_answer(f"answer {n}"))


def _turn(text, sid=None, how="header", **extra):
    body = {"model": MODEL, "messages": [{"role": "user", "content": text}], "cache": False, **extra}
    headers = dict(AUTH)
    if sid and how == "header":
        headers["X-LLM-Session-ID"] = sid
    if sid and how == "body":
        body["session_id"] = sid
    return headers, body


async def _until(predicate, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never met")


async def _two_turns(client, upstream, path, first, second, *, waits):
    one = asyncio.create_task(client.post(path, headers=first[0], json=first[1]))
    await _until(lambda: len(upstream.prompts) == 1)
    two = asyncio.create_task(client.post(path, headers=second[0], json=second[1]))
    if waits:
        await asyncio.sleep(0.2)
        assert len(upstream.prompts) == 1  # the second turn waits
    else:
        await _until(lambda: len(upstream.prompts) == 2)
    upstream.gate.set()
    return await asyncio.gather(one, two)


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
@pytest.mark.parametrize("how", ["header", "body"])
async def test_second_turn_waits_for_the_first_and_sees_it(client, session_db, path, how):
    upstream = _Upstream()
    with aioresponses() as mock:
        mock.post(URL_A, callback=upstream, repeat=True)
        r1, r2 = await _two_turns(client, upstream, path, _turn("A", "s1", how),
                                  _turn("B", "s1", how), waits=True)
    assert r1.status_code == r2.status_code == 200
    assert upstream.prompts[1] == [
        {"role": "user", "content": "A"},
        {"role": "assistant", "content": "answer 1"},
        {"role": "user", "content": "B"},
    ]
    assert session_db.load_session("admin", "s1")[-1] == {"role": "assistant", "content": "answer 2"}
    assert session_turns._turns == {}


async def test_requests_without_session_are_not_serialised(client, session_db):
    upstream = _Upstream()
    with aioresponses() as mock:
        mock.post(URL_A, callback=upstream, repeat=True)
        await _two_turns(client, upstream, "/v1/chat/completions", _turn("A"), _turn("B"), waits=False)
    assert upstream.prompts[1] == [{"role": "user", "content": "B"}]


async def test_concurrent_turns_on_a_full_history_keep_both(client, session_db, monkeypatch):
    monkeypatch.setattr(session_db, "_MAX_TURNS", 6)
    history = [{"role": r, "content": f"{r}{i}"} for i in range(3) for r in ("user", "assistant")]
    session_db.save_session("admin", "full", history)
    upstream = _Upstream()
    with aioresponses() as mock:
        mock.post(URL_A, callback=upstream, repeat=True)
        await _two_turns(client, upstream, "/v1/chat/completions", _turn("A", "full"),
                         _turn("B", "full"), waits=True)
    assert [m["content"] for m in session_db.load_session("admin", "full")[-4:]] == [
        "A", "answer 1", "B", "answer 2"]


async def test_client_leaving_while_waiting_for_its_turn_leaves_the_queue(client, session_db):
    from tests.test_inflight_disconnect import _Abandoned

    upstream = _Upstream()
    with aioresponses() as mock:
        mock.post(URL_A, callback=upstream, repeat=True)
        headers, body = _turn("A", "s2")
        first = asyncio.create_task(client.post("/v1/chat/completions", headers=headers, json=body))
        await _until(lambda: len(upstream.prompts) == 1)
        gone = _Abandoned("/v1/chat/completions", _turn("B")[1], {"X-LLM-Session-ID": "s2"})
        await _until(lambda: session_turns.waiting("admin", "s2") == 1)
        await gone.hang_up()
        assert gone.status() == 499
        upstream.gate.set()
        assert (await first).status_code == 200
        await gone.close()
    assert len(upstream.prompts) == 1
    assert [m["content"] for m in session_db.load_session("admin", "s2")] == ["A", "answer 1"]
    assert session_turns._turns == {}


async def test_streamed_http_turn_is_recorded(client, session_db):
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        headers, body = _turn("A", "s3", stream=True)
        resp = await client.post("/v1/chat/completions", headers=headers, json=body)
    assert resp.status_code == 200
    assert session_db.load_session("admin", "s3") == [
        {"role": "user", "content": "A"}, {"role": "assistant", "content": "Hello!"}]
    assert session_turns._turns == {}


async def test_streamed_cli_turn_is_recorded(client, session_db, _providers_file, monkeypatch):
    from plugboard import cli_subprocess, router
    from tests.test_router_admission import CLI

    _providers_file.write_text(json.dumps({"providers": [CLI]}))
    router.invalidate_models_cache()

    async def fake_complete(kind, body, *, timeout_s=None):
        return _answer("from the CLI")

    monkeypatch.setattr(cli_subprocess, "complete", fake_complete)
    headers, body = _turn("A", "s4", stream=True, model="pbtest-cli:model-x")
    resp = await client.post("/v1/chat/completions", headers=headers, json=body)
    assert resp.status_code == 200
    assert session_db.load_session("admin", "s4")[-1] == {"role": "assistant", "content": "from the CLI"}
```

- [ ] **Step 2 : vérifier l'échec** — `PYTEST tests/test_session_turns.py` → `ImportError: cannot import name 'session_turns'`.

- [ ] **Step 3 : `plugboard/session_turns.py`** selon **Interfaces** : `_turns[(client_id, session_id)] = [asyncio.Lock(), utilisateurs]` créé à la demande ; `acquire` incrémente les utilisateurs, attend le verrou (équitable), décrémente et supprime l'entrée si l'attente est annulée ; la libération rend le verrou une seule fois et supprime l'entrée au dernier utilisateur. Docstring : D-24, un seul processus, verrou par client et par session.

- [ ] **Step 4 : `app.py`.**
  - Les deux routes chat : `dependencies=[Depends(_inflight_chat_dep), Depends(_session_turn_dep)]`.
  - Fusion (`:1935`) : avant `merge_history_into_body`, `await _unless_client_gone(request, _take_session_turn(request, client_id, session_id))` ; `_ClientGone` → `return _client_gone_response(body.get("model", "?"))`.
  - `_bill_streaming` ajoute `"text": completion_text` à son dict (non envoyé au client).
  - `_record_session_turn` : le bloc `:2492-2509` devient un appel ; même appel dans la voie CLI streamée avec `content`, et dans `_stream_and_bill` quand le flux est complet et `bill` existe : `_record_session_turn(cid, session_id, list(req_body.get("messages") or []), bill["text"])`. Un flux interrompu n'est pas enregistré.

- [ ] **Step 5 : docs et CHANGELOG.** `clients-and-keys.md`, option `session_id` : turns of one session run one after the other (a concurrent turn waits for the previous one and sees it, a client that hangs up while waiting leaves the queue), streamed turns are kept too ; same for the `X-LLM-Session-ID` header. `docs/api/README.md` : même phrase courte. `ARCHITECTURE.md` étapes 4 et 9 : le tour de session est pris avant la fusion et rendu après la réponse. `CHANGELOG.md` **Fixed** : deux tours simultanés d'une session pouvaient perdre le premier (historique plein) ou répondre sans le voir ; un tour streamé n'était jamais enregistré.

- [ ] **Step 6 : vérifier** — `PYTEST tests/test_session_turns.py tests/test_session_store.py tests/test_inflight_disconnect.py tests/test_api.py tests/test_api_contract.py tests/test_cli_subprocess.py tests/test_quota_exhausted_429.py` → tout passe.

- [ ] **Step 7 : commit** — `git commit -m "fix(sessions): one turn at a time per session, streamed turns recorded"`.

---

### Task 6 : contrôle final

**Files:** aucun nouveau.

- [ ] **Step 1 : non-régression ciblée** — `PYTEST tests/test_provider_admission.py tests/test_router_admission.py tests/test_route_pauses.py tests/test_session_turns.py tests/test_router.py tests/test_router_network_fallback.py tests/test_inflight_disconnect.py tests/test_session_store.py tests/test_circuit_breaker_route_key.py tests/test_circuit_breaker_provider_faults.py tests/test_quota_exhausted_429.py tests/test_no_retry_context_too_long.py tests/test_model_not_found.py tests/test_model_access_security.py tests/test_api.py tests/test_api_contract.py tests/test_config_docs_coverage.py tests/test_docs_links.py` → tout passe.
- [ ] **Step 2 : lint et contrat** — `/srv/dev/plugboard/.venv/bin/ruff check plugboard/ tests/` et `ruff format --check plugboard/ tests/` → propres ; `scripts/export_contract.py --check` → `up to date`.
- [ ] **Step 3 : état** — `git status --porcelain` vide ; le scratchpad `TMPDIR` ne contient plus rien de ces tests.

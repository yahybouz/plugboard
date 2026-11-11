# Épinglage DNS des fournisseurs protégés (PLU-7) — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Aucune connexion d'un fournisseur protégé (catalogue, préréglage cloud intégré, manuel coché) vers une adresse non publique, contrôlée à chaque connexion.

**Architecture:** Nouveau module `plugboard/netguard.py` : résolveur filtrant + `socket_factory` sur un même `TCPConnector` (approche 1c), règles « qui est protégé », compte rendu des refus. `http_pool` ajoute une session publique sans proxy et `session_for(provider)` ; chaque appelant qui joint la `base_url` d'un fournisseur choisit sa session par fournisseur. Nouveau champ `egress_guard`, règles `POST`/`PUT` (D-19), case dans le tableau de bord.

**Tech Stack:** Python 3.12, FastAPI, aiohttp 3.14.5, aiohappyeyeballs 2.7.1, pytest (`asyncio_mode = auto`), aioresponses, JS sans framework (`static/app.js`).

**Spec:** `docs/superpowers/specs/2026-11-11-catalog-dns-pinning-design.md` (validée par YB le 2026-11-11, décisions D-17 à D-20).

## Global Constraints

- Aucune nouvelle dépendance ; aiohttp 3.14.5 (`TCPConnector(resolver=, socket_factory=)`, `aiohttp.AddrInfoType`).
- `http_pool.get_session()` garde sa signature (des tests la remplacent par un faux sans argument).
- Session des fournisseurs protégés : `trust_env=False` (D-20). Session partagée inchangée : `trust_env=True`.
- Champ fournisseur `egress_guard: bool`, absent = `False` ; réponses : `egress_guard` (effectif) et `egress_guard_forced`.
- Textes exacts : erreur `[<id>] egress refused: <host> resolves to a non-public address` ; code `egress_refused` ; audit `provider.egress_refused` (actor `system`) pour un refus de connexion, `provider.egress.rejected` pour un refus à l'édition ; webhook `type: "provider_egress_refused"` ; fenêtre de compte rendu 300 s par fournisseur.
- Code, messages, interface et docs en anglais ; aucune donnée personnelle, aucune vraie clé, aucun nom de client.
- Tests ciblés seulement, jamais tout `tests/`. Dans chaque commande, depuis la racine de l'atelier :
  `PYTEST="env TMPDIR=/tmp/claude-1001/-srv-dev-plugboard/a5d91e11-1edb-4748-99e2-79712c823769/scratchpad PLUGBOARD_SKIP_DOTENV=1 /srv/dev/plugboard/.venv/bin/python -m pytest -q -p no:cacheprovider"`
- Conventional Commits, portée `security` ; chaque message se termine par les lignes d'attribution de la session en cours. Pas de `git push`, pas de `ci`.

## Review Focus

- Le formulaire d'édition renvoie toujours la `base_url` : une `base_url` inchangée sur un fournisseur imposé ne doit déclencher aucune résolution (DNS en panne = 200) → `test_put_unchanged_base_url_makes_no_lookup` (Task 10).
- Un nom qui répond à la fois public et privé est refusé, jamais « couru » → `test_mixed_answer_is_refused` (Task 2).
- Une rafale de refus du même fournisseur ne produit qu'un audit et une alerte par 5 min, sans bloquer la boucle → `test_report_refusal_once_per_window`, `test_report_refusal_does_not_block` (Task 4).
- Les faux existants (`get_session()` sans argument, `_probe_http(url, headers)`) interceptent toujours → `test_session_for_uses_patched_get_session` (Task 5), `test_probe_provider_passes_public_only_only_when_protected` (Task 8) + non-régression (Task 14).
- `HTTPS_PROXY` défini : la session publique l'ignore, la partagée le garde → `test_public_session_ignores_proxy_env` (Task 5).

---

### Task 1: `netguard` — classification d'adresse et filet de socket

**Files:**
- Create: `plugboard/netguard.py`
- Modify: `plugboard/llm_pricing.py:56-57`, `:123-143` (déplacer `_CGNAT`, `_METADATA`, `_NAT64`, `_NAT64_LOCAL`, `ip_is_public` ; importer `ip_is_public` depuis `netguard` ; retirer `import ipaddress` s'il devient inutile)
- Test: `tests/test_netguard.py`

**Interfaces — Produces:**
- `ip_is_public(address: str) -> bool` (corps actuel, inchangé ; `llm_pricing.ip_is_public` reste importable).
- `class EgressRefused(OSError)` ; levée comme `EgressRefused(errno.EACCES, "non-public address refused")`.
- `refuse_non_public(addr_info: aiohttp.AddrInfoType) -> socket.socket` : lit `sockaddr[0]` via `ip_is_public` (lookup global du module, pour qu'un test puisse le remplacer) ; refus → `EgressRefused` sans créer de socket ; sinon `socket.socket(family=, type=, proto=)`.
- `EGRESS_REFUSED = "egress_refused"`.

- [ ] **Step 1: Write the failing tests**

```python
NON_PUBLIC = ["127.0.0.1", "10.0.0.1", "172.17.0.1", "169.254.169.254", "100.64.0.1",
              "198.18.0.1", "::1", "::ffff:10.0.0.1", "64:ff9b::a00:1", "2002:a00:1::1",
              "fe80::1%eth0"]
PUBLIC = ["1.1.1.1", "::ffff:1.1.1.1", "2606:4700::1111"]

@pytest.mark.parametrize("address", NON_PUBLIC)
def test_non_public_is_refused(address):
    assert netguard.ip_is_public(address) is False
    with pytest.raises(netguard.EgressRefused) as exc:
        netguard.refuse_non_public((socket.AF_INET6 if ":" in address else socket.AF_INET,
                                    socket.SOCK_STREAM, 6, "", (address, 443)))
    assert exc.value.errno == errno.EACCES

@pytest.mark.parametrize("address", PUBLIC)
def test_public_gets_a_socket(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sock = netguard.refuse_non_public((family, socket.SOCK_STREAM, 6, "", (address, 443)))
    try:
        assert sock.family == family
    finally:
        sock.close()

def test_llm_pricing_reexports_ip_is_public():
    from plugboard import llm_pricing
    assert llm_pricing.ip_is_public is netguard.ip_is_public
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_netguard.py` → `ModuleNotFoundError: plugboard.netguard`.
- [ ] **Step 3: Create `netguard.py` with the interfaces above; move the constants and `ip_is_public` out of `llm_pricing`.**
- [ ] **Step 4: Run, expect PASS** — `$PYTEST tests/test_netguard.py tests/test_llm_pricing.py` → all pass (`test_ip_is_public` included).
- [ ] **Step 5: Commit** — `git add plugboard/netguard.py plugboard/llm_pricing.py tests/test_netguard.py && git commit -m "feat(security): add netguard address check and socket guard"`

### Task 2: `netguard` — résolveur filtrant et connecteur public

**Files:** Modify `plugboard/netguard.py` ; Test `tests/test_netguard.py`.

**Interfaces — Produces:**
- `class PublicOnlyResolver(aiohttp.abc.AbstractResolver)` : `__init__(self, inner: AbstractResolver | None = None)` (`aiohttp.resolver.DefaultResolver()` si `None`, créé dans `__init__`) ; `async resolve(host, port=0, family=socket.AF_INET) -> list[ResolveResult]` délègue à `inner` puis lève `EgressRefused(errno.EACCES, "non-public address refused")` dès qu'**un** `host` de réponse n'est pas public ; `async close()` ferme `inner`.
- `public_connector(*, resolver: AbstractResolver | None = None, inner: AbstractResolver | None = None, **kw) -> aiohttp.TCPConnector` : `TCPConnector(resolver=resolver or PublicOnlyResolver(inner), socket_factory=refuse_non_public, **kw)`.
- `client_session(public_only: bool, **kw) -> aiohttp.ClientSession` : public → `kw.setdefault("connector", public_connector())`, `kw["trust_env"] = False` ; sinon `aiohttp.ClientSession(**kw)` sans rien ajouter (appel via l'attribut du module `aiohttp`, que des tests remplacent).
- `is_egress_refusal(exc: BaseException) -> bool` : `ClientConnectorError` dont `os_error` est un `EgressRefused`.

- [ ] **Step 1: Write the failing tests** — fixture `upstream` : serveur `aiohttp.web` sur `127.0.0.1:0` (modèle : `tests/test_http_provider_error_body.py:52-82`) qui compte les appels de `GET /v1/models` et répond `{"data": []}` ; résolveur factice `FakeResolver(answers)` qui compte ses appels et renvoie des `ResolveResult` (`hostname`, `host`, `port`, `family`, `proto=0`, `flags=socket.AI_NUMERICHOST`).

```python
async def test_rebinding_to_loopback_is_refused(upstream):
    fake = FakeResolver(["127.0.0.1", "::1"])
    async with netguard.client_session(True, connector=netguard.public_connector(inner=fake)) as s:
        for _ in range(2):
            with pytest.raises(aiohttp.ClientConnectorError) as exc:
                await s.get(f"http://rebind.test:{upstream.port}/v1/models")
            assert netguard.is_egress_refusal(exc.value)
            assert exc.value.host == "rebind.test"
    assert upstream.calls == 0 and fake.calls == 2   # refusal never cached

async def test_mixed_answer_is_refused(upstream):
    fake = FakeResolver(["1.1.1.1", "127.0.0.1"])
    ...same request... assert netguard.is_egress_refusal(exc.value) and upstream.calls == 0

async def test_same_request_without_guard_reaches_server(upstream):
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=FakeResolver(["127.0.0.1"]))) as s:
        async with s.get(f"http://rebind.test:{upstream.port}/v1/models") as r:
            assert r.status == 200
    assert upstream.calls == 1

async def test_ip_literal_is_refused_by_socket_factory(upstream):
    async with netguard.client_session(True) as s:
        with pytest.raises(aiohttp.ClientConnectorError) as exc:
            await s.get(f"http://127.0.0.1:{upstream.port}/v1/models")
    assert netguard.is_egress_refusal(exc.value) and upstream.calls == 0

async def test_client_session_public_ignores_proxy_and_plain_is_untouched():
    async with netguard.client_session(True) as s:
        assert s.trust_env is False and s.connector._socket_factory is netguard.refuse_non_public
    async with netguard.client_session(False) as s:
        assert s.connector._socket_factory is None
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_netguard.py -k "rebinding or mixed or literal or without_guard or client_session"` → `AttributeError: ... PublicOnlyResolver`.
- [ ] **Step 3: Implement the interfaces above in `netguard.py`.**
- [ ] **Step 4: Run, expect PASS** — même commande.
- [ ] **Step 5: Commit** — `git commit -m "feat(security): refuse non-public addresses at resolve and connect time"` (fichiers : `plugboard/netguard.py tests/test_netguard.py`).

### Task 3: `netguard` — qui est protégé (D-18)

**Files:** Modify `plugboard/netguard.py` ; Test `tests/test_netguard.py`.

**Interfaces — Produces:**
- `guard_is_forced(provider: Mapping[str, Any]) -> bool` : `preset_key` commence par `models_dev.PRESET_PREFIX`, ou `preset_key` ∈ `direct_providers.PRESETS` et la `base_url` **du préréglage** commence par `http://`/`https://`. Imports de `models_dev` et `direct_providers` faits dans la fonction (pas de cycle).
- `is_protected(provider: Mapping[str, Any]) -> bool` : `guard_is_forced(p)` ou (`p.get("egress_guard") is True` et `base_url` HTTP(S)).

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.parametrize("provider,forced,protected", [
    ({"preset_key": "catalog-acme", "base_url": "https://api.acme.test/v1"}, True, True),
    ({"preset_key": "openai", "base_url": "https://api.openai.com/v1"}, True, True),
    ({"preset_key": "anthropic", "base_url": "https://api.anthropic.com/v1"}, True, True),
    ({"preset_key": "github-broker", "base_url": "https://models.github.ai/inference"}, True, True),
    ({"preset_key": "chatgpt-oauth", "base_url": "https://chatgpt.com/backend-api/codex"}, True, True),
    ({"preset_key": "agy-cli-subprocess", "base_url": "subprocess://agy"}, False, False),
    ({"preset_key": "", "base_url": "https://x.test"}, False, False),
    ({"base_url": "http://fake-llm.test"}, False, False),
    ({"base_url": "http://localhost:11434", "provider_type": "sovereign"}, False, False),
    ({"base_url": "https://x.test", "egress_guard": True}, False, True),
    ({"base_url": "subprocess://agy", "egress_guard": True}, False, False),
])
def test_who_is_protected(provider, forced, protected):
    assert netguard.guard_is_forced(provider) is forced
    assert netguard.is_protected(provider) is protected
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_netguard.py::test_who_is_protected` → `AttributeError`.
- [ ] **Step 3: Implement both functions.**
- [ ] **Step 4: Run, expect PASS** — même commande.
- [ ] **Step 5: Commit** — `git commit -m "feat(security): decide which providers get the egress guard"`.

### Task 4: compte rendu des refus (D-17) et alerte

**Files:** Modify `plugboard/netguard.py`, `plugboard/alerts.py` (après `_send_provider_alert`) ; Test `tests/test_netguard.py`.

**Interfaces — Produces:**
- `alerts.send_egress_refused_alert(provider_id: str, host: str) -> None` (async) : rien sans `_WEBHOOK_URL` ; sinon POST `{"type": "provider_egress_refused", "provider_id", "host", "message": "Provider '<id>': <host> resolves to a non-public address; requests are refused"}`, mêmes délai et journalisation que `_send_provider_alert`.
- `netguard.REPORT_WINDOW_S = 300.0`, `_clock = time.monotonic` (remplaçable en test), `_last_report: dict[str, float]`, `_tasks: set[asyncio.Task]`.
- `netguard.report_refusal(provider_id: str, host: str, source: str) -> bool` (synchrone) : premier refus du fournisseur dans la fenêtre → `log.error`, tâche de fond (référence gardée dans `_tasks`) qui fait `await asyncio.to_thread(audit.log_action, "provider.egress_refused", actor="system", target_type="provider", target_id=provider_id, details=json.dumps({"host": host, "source": source}))` puis `await alerts.send_egress_refused_alert(provider_id, host)` (exceptions → `log.warning`) ; renvoie `True`. Sinon `log.debug`, renvoie `False`. Jamais attendu par l'appelant.

- [ ] **Step 1: Write the failing tests** — fixture autouse qui vide `netguard._last_report` ; `audit_mod._DB_PATH` vers `tmp_path / "audit.db"` (modèle `tests/test_security_audit_2026_04.py:748`) ; `alerts.send_egress_refused_alert` remplacé par un faux qui enregistre.

```python
async def test_report_refusal_once_per_window(monkeypatch, tmp_path, sent):
    now = [1000.0]
    monkeypatch.setattr(netguard, "_clock", lambda: now[0])
    assert netguard.report_refusal("acme", "api.acme.test", "forward") is True
    assert netguard.report_refusal("acme", "api.acme.test", "forward") is False
    assert netguard.report_refusal("other", "o.test", "forward") is True
    now[0] += 301
    assert netguard.report_refusal("acme", "api.acme.test", "watchdog") is True
    await asyncio.gather(*list(netguard._tasks))
    assert len(audit_mod.get_audit_log(action="provider.egress_refused")) == 3
    assert sent == [("acme", "api.acme.test"), ("other", "o.test"), ("acme", "api.acme.test")]

async def test_report_refusal_does_not_block(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_mod, "log_action", lambda *a, **k: time.sleep(0.3))
    t0 = time.perf_counter()
    netguard.report_refusal("slow", "s.test", "forward")
    assert time.perf_counter() - t0 < 0.05
    await asyncio.gather(*list(netguard._tasks))

async def test_egress_alert_payload(monkeypatch):
    monkeypatch.setattr(alerts, "_WEBHOOK_URL", "http://hook.test/alerts")
    with aioresponses() as m:
        m.post("http://hook.test/alerts", status=200)
        await alerts.send_egress_refused_alert("acme", "api.acme.test")
        (call,) = m.requests[("POST", URL("http://hook.test/alerts"))]
    assert call.kwargs["json"]["type"] == "provider_egress_refused"
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_netguard.py -k "report or alert"` → `AttributeError: report_refusal`.
- [ ] **Step 3: Implement `report_refusal` and `send_egress_refused_alert`.**
- [ ] **Step 4: Run, expect PASS** — même commande.
- [ ] **Step 5: Commit** — `git commit -m "feat(security): audit and alert egress refusals at most every 5 minutes"`.

### Task 5: `http_pool` — session publique et `session_for`

**Files:** Modify `plugboard/http_pool.py` (après `get_session`, et `close_session`) ; Test `tests/test_http_pool.py`.

**Interfaces — Consumes:** `netguard.PublicOnlyResolver`, `public_connector`, `is_protected`.
**Produces:**
- `async get_public_session() -> aiohttp.ClientSession` : singleton `_public_session`, même verrou `_lock` et même double vérification que `get_session` ; `_public_resolver = netguard.PublicOnlyResolver()` ; connecteur `public_connector(resolver=_public_resolver, limit=_CONN_LIMIT, limit_per_host=_CONN_PER_HOST, ttl_dns_cache=_DNS_TTL)` ; `raise_for_status=False`, `trust_env=False`.
- `async session_for(provider: Mapping[str, Any]) -> aiohttp.ClientSession` : `get_public_session()` si `netguard.is_protected(provider)`, sinon `get_session()` (nom global du module, donc un faux posé sur `http_pool.get_session` intercepte).
- `close_session()` ferme aussi `_public_session` puis `await _public_resolver.close()` et les remet à `None`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_public_session_ignores_proxy_env(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    pub, shared = await http_pool.get_public_session(), await http_pool.get_session()
    assert pub is not shared and pub is await http_pool.get_public_session()
    assert pub.trust_env is False and shared.trust_env is True
    assert pub.connector._socket_factory is netguard.refuse_non_public
    assert isinstance(pub.connector._resolver, netguard.PublicOnlyResolver)

async def test_session_for_picks_by_provider():
    assert await http_pool.session_for({"preset_key": "catalog-x", "base_url": "https://a.test"}) is await http_pool.get_public_session()
    assert await http_pool.session_for({"base_url": "http://x.test"}) is await http_pool.get_session()

async def test_session_for_uses_patched_get_session(monkeypatch):
    sentinel = object()
    async def fake():
        return sentinel
    monkeypatch.setattr(http_pool, "get_session", fake)
    assert await http_pool.session_for({"base_url": "http://x.test"}) is sentinel

async def test_close_session_closes_both():
    pub, shared = await http_pool.get_public_session(), await http_pool.get_session()
    await http_pool.close_session()
    assert pub.closed and shared.closed
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_http_pool.py` → `AttributeError: get_public_session`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS** — `$PYTEST tests/test_http_pool.py`.
- [ ] **Step 5: Commit** — `git commit -m "feat(security): add a direct public-only session to the HTTP pool"`.

### Task 6: `router.forward` — session par candidat, refus sans disjoncteur

**Files:** Modify `plugboard/router.py:96-106` (nouvelle classe), `:656-661`, `:841-866`, `:868-883` ; Test `tests/test_egress_guard_routing.py`.

**Interfaces — Consumes:** `http_pool.session_for`, `netguard.is_egress_refusal`, `netguard.report_refusal`.
**Produces:** `class ProviderEgressRefused(ProviderNetworkError)` : `__init__(self, provider_id: str, host: str)`, attribut `host`, message `f"[{provider_id}] egress refused: {host} resolves to a non-public address"`.

- [ ] **Step 1: Write the failing tests** — fixtures partagées par les Tasks 6 à 8 dans `tests/test_egress_guard_routing.py` : `closed_port` (modèle `_closed_port`, `tests/test_provider_network_error.py:26-30`) ; `upstream` (serveur local, `POST /v1/chat/completions` → `FAKE_COMPLETION`, compte les appels) ; `client_key` (copie de `tests/test_provider_network_error.py:49-59`) ; `recorders` (objet aux listes `cb_failures`, `provider_failures`, `reports`, remplies par des faux posés sur `plugboard.circuit_breaker.record_failure`, `plugboard.router.record_provider_failure` (async) et `plugboard.netguard.report_refusal`) ; `acme = {**PROVIDER_A, "id": "acme", "preset_key": "catalog-acme", "base_url": f"http://127.0.0.1:{port}"}`, `plain = {**PROVIDER_A, "id": "plain", "base_url": même URL}`.

```python
async def test_forward_falls_back_after_refusal(upstream, monkeypatch, recorders):
    monkeypatch.setattr(router, "_resolve_all_providers", lambda m: [(acme, "m"), (plain, "m")])
    result = await router.forward({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert result == FAKE_COMPLETION and upstream.calls == 1
    assert router.served_by()[0]["id"] == "plain"
    assert recorders.cb_failures == [] and recorders.provider_failures == []
    assert recorders.reports == [("acme", "127.0.0.1", "forward")]

async def test_refusal_gives_502_admin_text_and_client_reference(client, client_key, protected_unreachable):
    # protected_unreachable : providers.json = [acme sur 127.0.0.1:<port fermé>] (modèle tests/test_provider_network_error.py:34-46)
    chat = {"model": "acme:fake:model-v1", "messages": [{"role": "user", "content": "hi"}], "cache": False}
    admin = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer testkey"}, json=chat)
    assert admin.status_code == 502 and "egress refused: 127.0.0.1" in admin.json()["detail"]
    user = await client.post("/v1/chat/completions", headers=client_key, json=chat)
    body = user.json()
    assert user.status_code == 502 and "egress" not in body["detail"] and "127.0.0.1" not in body["detail"]
    assert body["error_ref"] in body["detail"]
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_egress_guard_routing.py` → 502 avec « network error » / `upstream.calls == 2`.
- [ ] **Step 3: Implement** — retirer `session = await _get_http_session()` d'avant la boucle ; `session = await session_for(provider)` juste après le bloc `subprocess:`, `session_for` importé dans la fonction comme `get_session` aujourd'hui (un faux posé sur `http_pool.session_for` doit intercepter) ; `egress_refused = False` par candidat ; en tête du `except (aiohttp.ClientError, ValueError)` : si `netguard.is_egress_refusal(exc)` → métrique d'erreur, `last_err = ProviderEgressRefused(provider["id"], exc.host)`, `netguard.report_refusal(provider["id"], exc.host, "forward")`, `provider_fault = False`, `provider_failed = True`, `egress_refused = True` (puis le `break` existant) ; après la boucle, `record_provider_failure` seulement si `not egress_refused`.
- [ ] **Step 4: Run, expect PASS** — `$PYTEST tests/test_egress_guard_routing.py tests/test_provider_network_error.py tests/test_http_provider_error_body.py`.
- [ ] **Step 5: Commit** — `git commit -m "feat(security): refuse non-public egress in forward and fall back"`.

### Task 7: flux et découverte — `forward_stream`, `discover_models`, `discover_openai_models`

**Files:** Modify `plugboard/router.py:1064-1067`, `:1094-1101`, `:2017-2019` ; `plugboard/model_availability.py:964-966` ; Test `tests/test_egress_guard_routing.py`.

**Interfaces — Consumes:** `session_for`, `ProviderEgressRefused`, `netguard.is_egress_refusal`, `report_refusal`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_stream_refusal_gives_502_without_breaker(client, client_key, protected_unreachable, recorders):
    chat = {..., "stream": True}
    resp = await client.post("/v1/chat/completions", headers=client_key, json=chat)
    assert resp.status_code == 502 and "127.0.0.1" not in resp.text
    assert recorders.cb_failures == [] and recorders.reports[0][2] == "stream"

async def test_discovery_paths_use_session_for(monkeypatch):
    seen = []
    class _Called(Exception): ...
    async def fake(provider):
        seen.append(provider["id"]); raise _Called
    monkeypatch.setattr(http_pool, "session_for", fake)
    with pytest.raises(_Called):
        await router.discover_models(acme)
    entries, reason = await model_availability.discover_openai_models(acme)
    assert entries is None and seen == ["acme", "acme"]
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_egress_guard_routing.py -k "stream or discovery"`.
- [ ] **Step 3: Implement** — les trois appelants passent par `session_for(provider)` ; dans `forward_stream`, en tête du `except aiohttp.ClientError` : refus → `report_refusal(provider["id"], exc.host, "stream")`, `raise ProviderEgressRefused(provider["id"], exc.host) from exc`, sans `cb_failure`.
- [ ] **Step 4: Run, expect PASS** — `$PYTEST tests/test_egress_guard_routing.py tests/test_auto_refresh.py tests/test_model_availability.py`.
- [ ] **Step 5: Commit** — `git commit -m "feat(security): guard streams and model discovery of protected providers"`.

### Task 8: sondes, sentinelle et embeddings

**Files:** Modify `plugboard/health_probe.py:81-133`, `plugboard/provider_watchdog.py:228-365` et `:451-495`, `plugboard/semantic_cache.py:696-716` ; Test `tests/test_egress_guard_routing.py`.

**Interfaces — Consumes:** `netguard.client_session`, `is_protected`, `is_egress_refusal`, `report_refusal`, `EGRESS_REFUSED`.
**Produces:**
- `_probe_http(url, headers, timeout_s=5.0, *, public_only=False)` ; sur refus : `{"ok": False, "error": "egress_refused", "host": exc.host, "url": url, "latency_ms": …}`.
- `probe_provider` : `_probe_http(url, headers, public_only=True)` **seulement** si protégé (sinon l'appel actuel `_probe_http(url, headers)`, des faux n'acceptent que deux arguments) ; sur `egress_refused` : `report_refusal(provider.get("id", ""), result["host"], "health")`.
- `_sovereign_health(base_url, *, public_only=False)`, `_shallow_health(base_url, *, public_only=False)`, `_deep_health(base_url, bearer_token, model="", *, public_only=False)` : même forme d'erreur ; `ping_provider` calcule `public_only = netguard.is_protected(provider)` une fois, après le court-circuit `subprocess:`.
- `_watchdog_loop` : `result["error"] == EGRESS_REFUSED` → `report_refusal(pid, result.get("host", ""), "watchdog")`, état et journal comme un échec, **puis `continue`** (ni redémarrage ni `record_provider_failure`).
- `_embed` : `netguard.client_session(netguard.is_protected(provider), timeout=timeout)` ; refus → `report_refusal(provider_id, exc.host, "embed")`, renvoie `None`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_probe_http_public_only_refuses_loopback(closed_port):
    r = await health_probe._probe_http(f"http://127.0.0.1:{closed_port}/health", {}, public_only=True)
    assert r["ok"] is False and r["error"] == "egress_refused" and r["host"] == "127.0.0.1"

async def test_probe_provider_passes_public_only_only_when_protected(monkeypatch, recorders):
    calls = []
    async def http(url, headers, **kw):
        calls.append(kw); return {"ok": False, "error": "egress_refused", "host": "h"}
    monkeypatch.setattr(health_probe, "_probe_http", http)
    await health_probe.probe_provider({"id": "acme", "preset_key": "catalog-acme", "base_url": "https://a.test"})
    await health_probe.probe_provider({"id": "plain", "base_url": "https://p.test"})
    assert calls == [{"public_only": True}, {}] and recorders.reports == [("acme", "h", "health")]

async def test_shallow_health_public_only_refuses_loopback(closed_port):
    r = await provider_watchdog._shallow_health(f"http://127.0.0.1:{closed_port}", public_only=True)
    assert r["error"] == "egress_refused"

async def test_watchdog_skips_restart_on_egress_refusal(monkeypatch, recorders):
    # load_providers -> [acme actif] ; _FAILURE_THRESHOLD=1 ; _CHECK_INTERVAL=0 ;
    # ping_provider factice : met wd._running=False et renvoie {"ok": False, "error": "egress_refused", "host": "h", "check": "shallow"} ;
    # restart_provider et record_provider_failure -> pytest.fail
    await provider_watchdog._watchdog_loop()
    assert recorders.reports == [("acme", "h", "watchdog")]

async def test_embed_refusal_is_reported(monkeypatch, closed_port, recorders):
    # embed_is_enabled -> True ; PLUGBOARD_EMBED_PROVIDER_ID=acme ; PLUGBOARD_EMBED_MODEL=e ;
    # plugboard.config.get_provider -> acme sur 127.0.0.1:closed_port
    assert await semantic_cache._embed("hello") is None
    assert recorders.reports == [("acme", "127.0.0.1", "embed")]
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_egress_guard_routing.py -k "probe or health or watchdog or embed"`.
- [ ] **Step 3: Implement as listed.**
- [ ] **Step 4: Run, expect PASS** — `$PYTEST tests/test_egress_guard_routing.py tests/test_watchdog.py tests/test_ollama.py tests/test_openclaw_removal_supervision.py tests/test_semantic_cache.py tests/test_decision_v2_fixes.py`.
- [ ] **Step 5: Commit** — `git commit -m "feat(security): guard health probes, watchdog and embeddings"`.

### Task 9: champ `egress_guard`, création manuelle et réponses (D-18 c)

**Files:** Modify `plugboard/config.py:285-325` ; `plugboard/app.py:3323-3360` (modèles, `_mask`), `:3368-3406` (`add_provider`), `:3858-3876` (`_require_public_host`) ; Test `tests/test_egress_guard_api.py`.

**Interfaces — Produces:**
- `config.create_provider(..., egress_guard: bool = False)` : écrit `"egress_guard": True` dans le record seulement si vrai.
- `ProviderCreate.egress_guard: bool = False` ; `ProviderUpdate.egress_guard: Optional[bool] = None`.
- `_mask(p)` ajoute `egress_guard = netguard.is_protected(p)` et `egress_guard_forced = netguard.guard_is_forced(p)`.
- `_require_public_host(base_url, *, label="Catalog preset")` : messages `f"{label} refused: {exc}"` (422), `f"{label} host unreachable: {exc}"` (503).
- `async _require_guard_allows(provider_id: str, base_url: str) -> None` (app.py) : `base_url` non HTTP(S) → 422 `"The egress guard needs an http(s) base_url"` ; puis `_require_public_host(base_url, label="Provider base_url")` ; tout refus est audité `provider.egress.rejected` (`details` JSON `base_url`, `reason`) avant d'être relevé.
- `add_provider` : si `body.egress_guard`, `await _require_guard_allows("", body.base_url)` avant `create_provider(..., egress_guard=body.egress_guard)`.

- [ ] **Step 1: Write the failing tests** — `private` : fixture qui pose sur `plugboard.llm_pricing._resolve_public` une coroutine levant `UnsafeURL("<host> resolves to a non-public address (10.0.0.5)")` ; `public` : coroutine renvoyant `[]` ; `no_lookup` : coroutine qui appelle `pytest.fail`.

```python
def test_create_provider_stores_egress_guard_only_when_set(_empty_providers_file):
    assert "egress_guard" not in config.create_provider(name="A", base_url="https://a.test", bearer_token="k")
    assert config.create_provider(name="B", base_url="https://b.test", bearer_token="k", egress_guard=True)["egress_guard"] is True
    assert config.get_provider("b")["egress_guard"] is True

async def test_post_guarded_provider_needs_public_url(client, private, audit_db):
    r = await client.post("/providers", headers=AUTH, json={"name": "Lan", "base_url": "http://10.0.0.5:8000", "bearer_token": "k", "egress_guard": True})
    assert r.status_code == 422 and "non-public" in r.json()["detail"] and config.get_provider("lan") is None
    assert audit_mod.get_audit_log(action="provider.egress.rejected")

async def test_post_guarded_provider_on_public_url(client, public):
    r = await client.post("/providers", headers=AUTH, json={"name": "Pub", "base_url": "https://pub.test/v1", "bearer_token": "k", "egress_guard": True})
    assert r.status_code == 201 and r.json()["egress_guard"] is True and r.json()["egress_guard_forced"] is False

async def test_post_unguarded_provider_makes_no_lookup(client, no_lookup):
    r = await client.post("/providers", headers=AUTH, json={"name": "Lan2", "base_url": "http://10.0.0.6", "bearer_token": "k"})
    assert r.status_code == 201 and r.json()["egress_guard"] is False

async def test_post_guard_on_non_http_url_is_422(client, no_lookup):
    r = await client.post("/providers", headers=AUTH, json={"name": "S", "base_url": "subprocess://agy", "bearer_token": "k", "egress_guard": True})
    assert r.status_code == 422

async def test_get_providers_exposes_effective_guard(client, _providers_file):
    # providers.json : catalog-x (catalog-acme), openai (preset openai), manual, guarded (egress_guard True), ollama (sovereign, localhost)
    got = {p["id"]: (p["egress_guard"], p["egress_guard_forced"]) for p in (await client.get("/providers", headers=AUTH)).json()}
    assert got == {"catalog-x": (True, True), "openai": (True, True), "manual": (False, False),
                   "guarded": (True, False), "ollama": (False, False)}
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_egress_guard_api.py`.
- [ ] **Step 3: Implement as listed.**
- [ ] **Step 4: Run, expect PASS** — `$PYTEST tests/test_egress_guard_api.py tests/test_models_dev.py tests/test_security_audit_2026_04.py`.
- [ ] **Step 5: Commit** — `git commit -m "feat(security): add the egress_guard provider field"`.

### Task 10: édition d'un fournisseur protégé (D-19)

**Files:** Modify `plugboard/app.py:3409-3448` (`patch_provider`) ; Test `tests/test_egress_guard_api.py`.

**Interfaces — Consumes:** `_require_guard_allows`, `netguard.guard_is_forced`, `netguard.is_protected`.

Règle, avant `update_provider` (provider inconnu → 404 existant) :
1. imposé + `egress_guard is False` → 422 `"The egress guard is always on for catalog and built-in cloud presets"` ; imposé + `True` → clé retirée de `updates`.
2. `protected_after = forced or updates.get("egress_guard", before.get("egress_guard") is True)`.
3. `url_changed` = nouvelle `base_url` ≠ ancienne, toutes deux sans `/` final ; `turned_on = protected_after and not netguard.is_protected(before)`.
4. `protected_after and (url_changed or turned_on)` → `await _require_guard_allows(provider_id, new_or_current_base_url)`.

- [ ] **Step 1: Write the failing tests** — fixtures : `forced_provider` écrit dans `providers.json` un fournisseur `acme` de `base_url` `https://api.acme.test/v1` avec le `preset_key` du paramètre (défaut `catalog-acme`) et renvoie `"acme"` ; `ollama_provider` écrit `PROVIDER_OLLAMA` (`tests/factories.py`) ; `private`, `public`, `no_lookup` de la Task 9.

```python
@pytest.mark.parametrize("preset_key", ["catalog-acme", "openai"])
async def test_put_internal_base_url_on_forced_provider_is_422(client, private, forced_provider, preset_key):
    r = await client.put(f"/providers/{forced_provider}", headers=AUTH, json={"base_url": "http://10.0.0.5"})
    assert r.status_code == 422 and config.get_provider(forced_provider)["base_url"] == "https://api.acme.test/v1"

async def test_put_unchanged_base_url_makes_no_lookup(client, no_lookup, forced_provider):
    r = await client.put(f"/providers/{forced_provider}", headers=AUTH,
                         json={"base_url": "https://api.acme.test/v1/", "name": "Renamed", "egress_guard": True})
    assert r.status_code == 200 and "egress_guard" not in config.get_provider(forced_provider)

async def test_put_cannot_turn_off_forced_guard(client, forced_provider):
    assert (await client.put(f"/providers/{forced_provider}", headers=AUTH, json={"egress_guard": False})).status_code == 422

async def test_put_checking_guard_on_local_ollama_is_422(client, ollama_provider):
    # vraie résolution de "localhost" (127.0.0.1 / ::1), pas de faux
    r = await client.put("/providers/ollama", headers=AUTH, json={"egress_guard": True})
    assert r.status_code == 422 and "egress_guard" not in config.get_provider("ollama")

async def test_put_manual_guard_toggle(client, public, monkeypatch):
    assert (await client.put("/providers/test-provider", headers=AUTH, json={"egress_guard": True})).json()["egress_guard"] is True
    assert (await client.put("/providers/test-provider", headers=AUTH, json={"egress_guard": False})).status_code == 200
    async def _fail(*a):
        pytest.fail("no lookup once the guard is off")
    monkeypatch.setattr("plugboard.llm_pricing._resolve_public", _fail)
    assert (await client.put("/providers/test-provider", headers=AUTH, json={"base_url": "http://10.0.0.5"})).status_code == 200
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_egress_guard_api.py -k put`.
- [ ] **Step 3: Implement the rule above.**
- [ ] **Step 4: Run, expect PASS** — `$PYTEST tests/test_egress_guard_api.py tests/test_auto_refresh.py tests/test_security_audit_2026_04.py`.
- [ ] **Step 5: Commit** — `git commit -m "feat(security): require a public base_url when a protected provider is edited"`.

### Task 11: test de connexion protégé

**Files:** Modify `plugboard/app.py:3879-3990` (`_test_openai_endpoint`), `:4003-4039` (`test_direct_provider`) ; Test `tests/test_egress_guard_api.py`.

**Interfaces — Produces:**
- `_test_openai_endpoint(base_url, api_key, send_test_token, pinned=None, preset=None, *, public_only=False)`. `public_only=True` : session `netguard.client_session(True, timeout=timeout, connector=netguard.public_connector(resolver=_PinnedResolver(host, pinned) if pinned else None))`, `allow_redirects=False` sur le `GET /models` et le `POST` d'essai ; refus → `{"status": "egress_refused", "message": f"Refused: {host} resolves to a non-public address.", "docs": _TEST_DOCS}`. `public_only=False` : code actuel inchangé (des faux de `aiohttp.ClientSession` n'acceptent pas `allow_redirects`).
- `test_direct_provider` : `public_only = preset is not None and netguard.guard_is_forced({"preset_key": body.preset_key})`, un seul appel avec `pinned=pinned, preset=preset, public_only=public_only`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_protected_connection_test_does_not_follow_redirects(redirecting_server, monkeypatch):
    # serveur : GET /v1/models -> 302 Location /internal/models ; GET /internal/models compte ses appels
    monkeypatch.setattr(netguard, "ip_is_public", lambda a: True)
    r = await app_mod._test_openai_endpoint(f"http://127.0.0.1:{redirecting_server.port}/v1", "k", False, public_only=True)
    assert r["status"] == "not_openai_compatible" and "HTTP 302" in r["message"]
    assert redirecting_server.internal_calls == 0

async def test_protected_connection_test_reports_egress_refused(closed_port):
    r = await app_mod._test_openai_endpoint(f"http://127.0.0.1:{closed_port}/v1", "k", False, public_only=True)
    assert r["status"] == "egress_refused"

async def test_direct_test_is_public_only_for_presets(client, monkeypatch):
    seen = []
    async def fake(*a, **kw):
        seen.append(kw["public_only"]); return {"status": "ok"}
    monkeypatch.setattr(app_mod, "_test_openai_endpoint", fake)
    await client.post("/admin/direct-providers/test", headers=AUTH, json={"preset_key": "openai", "api_key": "k"})
    await client.post("/admin/direct-providers/test", headers=AUTH, json={"base_url": "https://custom.test/v1", "api_key": "k"})
    assert seen == [True, False]
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_egress_guard_api.py -k "connection_test or direct_test"`.
- [ ] **Step 3: Implement as listed.**
- [ ] **Step 4: Run, expect PASS** — `$PYTEST tests/test_egress_guard_api.py tests/test_models_dev.py tests/test_exposure_and_estimates.py tests/test_decision_v2_fixes.py tests/test_review_fixes.py`.
- [ ] **Step 5: Commit** — `git commit -m "feat(security): keep the connection test of protected presets on public addresses"`.

### Task 12: tableau de bord — case « Public addresses only »

**Files:** Modify `static/index.html:268-277` (nouvelle `form-row` après celle de `pf-auto-discover`), `static/app.js:485-497` (ajout), `:733-740` (`_TEST_LABELS`), `:1035-1062` (envoi), `:1086-1093` (badge), `:1110-1125` (édition) ; Test `tests/test_egress_guard_dashboard.py`.

Textes exacts :
- libellé : `Public addresses only` ;
- aide `pf-egress-guard-hint` : `Refuses private, loopback and link-local addresses at every connection. Ignores HTTPS_PROXY: the server needs direct internet access. Leave off for Ollama or another local server.` ;
- aide si imposée : `Always on for catalog and built-in cloud presets.` ;
- badge : `public only` (classe `badge`) ; `_TEST_LABELS.egress_refused = "Refused: non-public address"`.

Comportement : ajout → case décochée, active, aide par défaut ; édition → `checked = p.egress_guard === true`, `disabled = p.egress_guard_forced === true` ; envoi → `body.egress_guard = document.getElementById("pf-egress-guard").checked;` en création et en édition ; création → `const cres = await api("POST", "/providers", body); if (!cres.ok) { … toast("Create failed: " + (detail || status), "error"); return; }`.

- [ ] **Step 1: Write the failing tests**

```python
ROOT = Path(__file__).resolve().parent.parent

def test_form_has_egress_guard_checkbox():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="pf-egress-guard"' in html and 'id="pf-egress-guard-hint"' in html
    assert "Public addresses only" in html

def test_app_js_sends_and_shows_the_guard():
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert 'body.egress_guard = document.getElementById("pf-egress-guard").checked' in js
    assert "p.egress_guard_forced === true" in js
    assert "Always on for catalog and built-in cloud presets." in js
    assert re.search(r'const cres = await api\("POST", "/providers", body\);\s*if \(!cres\.ok\)', js)
    assert "egress_refused:" in js

def test_app_js_parses():
    subprocess.run(["node", "--check", str(ROOT / "static" / "app.js")], check=True)
```

- [ ] **Step 2: Run, expect FAIL** — `$PYTEST tests/test_egress_guard_dashboard.py`.
- [ ] **Step 3: Implement the markup and script changes above.**
- [ ] **Step 4: Run, expect PASS** — `$PYTEST tests/test_egress_guard_dashboard.py tests/test_e2e_dashboard.py tests/test_security_audit_2026_04.py`.
- [ ] **Step 5: Commit** — `git commit -m "feat(security): add the public-addresses-only checkbox to the provider form"`.

### Task 13: documentation et changelog

**Files:** Modify `docs/guide/providers-and-subscriptions.md` (nouvelle sous-section `### Public addresses only` après `### Custom endpoint` ; une ligne dans `### Any provider from the models.dev catalog` et dans le tableau de `### Test the connection` pour `Refused: non-public address`), `docs/guide/deployment.md` (nouvelle section `## Outbound network`), `CHANGELOG.md` (`### Security` sous `## [Unreleased]`).

Contenu à couvrir (une phrase chacun) : qui est protégé (catalogue, préréglages intégrés à URL HTTP(S), manuels cochés ; Ollama jamais par défaut) ; contrôle à chaque connexion (DNS, IP littérale, redirection) ; refus → candidat suivant, 502 sinon, audit `provider.egress_refused` et webhook `provider_egress_refused` au plus toutes les 5 min, fournisseur laissé actif ; édition : `base_url` publique exigée (422) ; ignore `HTTPS_PROXY` → accès direct requis ; remèdes : préréglage pointé vers une adresse interne avant la mise à jour → le recréer en fournisseur manuel ; proxy obligatoire → fournisseur manuel vers la même URL, case décochée ; filtrage de sortie (pare-feu) conseillé en défense en profondeur.

- [ ] **Step 1: Write the three edits.**
- [ ] **Step 2: Verify** — `grep -c "egress" CHANGELOG.md docs/guide/providers-and-subscriptions.md docs/guide/deployment.md` → chaque fichier ≥ 1 ; `grep -n "HTTPS_PROXY" docs/guide/deployment.md` → au moins une ligne dans `## Outbound network`.
- [ ] **Step 3: Commit** — `git commit -m "docs(security): document the egress guard and its proxy limit"`.

### Task 14: vérification finale ciblée

- [ ] **Step 1: Lint** — `/srv/dev/plugboard/.venv/bin/python -m ruff check plugboard tests && /srv/dev/plugboard/.venv/bin/python -m ruff format --check plugboard tests` → aucune erreur.
- [ ] **Step 2: Suites ciblées** — `$PYTEST tests/test_netguard.py tests/test_http_pool.py tests/test_egress_guard_routing.py tests/test_egress_guard_api.py tests/test_egress_guard_dashboard.py tests/test_models_dev.py tests/test_llm_pricing.py tests/test_provider_network_error.py tests/test_http_provider_error_body.py tests/test_watchdog.py tests/test_ollama.py tests/test_auto_refresh.py tests/test_exposure_and_estimates.py tests/test_semantic_cache.py tests/test_openclaw_removal_supervision.py tests/test_decision_v2_fixes.py tests/test_security_audit_2026_04.py tests/test_e2e_dashboard.py` → tout passe, 0 échec.
- [ ] **Step 3: Faux de session existants** — `$PYTEST tests/test_decision_routing_v5.py tests/test_direct_identity_v2.py tests/test_openclaw_spec_parent.py tests/test_openclaw_removal_routing.py tests/test_publication_collision_parent.py tests/test_decision_parent_regressions.py tests/test_decision_review_fixes.py` → tout passe (ils remplacent `get_session()` sans argument).
- [ ] **Step 4: Atelier propre** — `git status --porcelain` → vide ; supprimer ce que les tests ont laissé dans le dossier `TMPDIR` de la session.

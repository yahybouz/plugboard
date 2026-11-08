"""/admin/suivi API and the /suivi page: admin only, rules surfaced as HTTP."""

from __future__ import annotations

import pytest

from plugboard import audit as audit_mod

ADMIN = {"Authorization": "Bearer adminkey"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path / "data"))
    root = tmp_path / "repo"
    (root / "docs/superpowers/plans").mkdir(parents=True)
    (root / "docs/superpowers/plans/p.md").write_text(
        "# P Implementation Plan\n\n### Task 1: One\n\n- [x] done\n"
    )
    (root / "TODOS.md").write_text("# TODOS\n\n## Security\n\n### Revoke\n")
    monkeypatch.setattr("plugboard.suivi_api.PROJECT_ROOT", root)
    return root


async def _create(client, titre="Ticket", **extra):
    resp = await client.post(
        "/admin/suivi/tickets", json={"titre": titre, **extra}, headers=ADMIN
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/admin/suivi/tickets", None),
        ("POST", "/admin/suivi/tickets", {"titre": "x"}),
        ("PATCH", "/admin/suivi/tickets/1", {"statut": "pret"}),
        ("POST", "/admin/suivi/tickets/1/preuves", {"type": "ci", "valeur": "v"}),
        ("POST", "/admin/suivi/import", None),
    ],
)
async def test_every_endpoint_needs_admin(admin_client, repo, method, path, body):
    await _create(admin_client)  # ticket 1 exists, so 401 is about auth only
    kwargs = {"json": body} if body is not None else {}
    refused = await admin_client.request(method, path, **kwargs)
    assert refused.status_code == 401
    allowed = await admin_client.request(method, path, headers=ADMIN, **kwargs)
    assert allowed.status_code in (200, 201), allowed.text


async def test_create_list_move(admin_client, repo):
    t = await _create(admin_client, "Fix login", priorite="P1", type="bug")
    assert (t["statut"], t["priorite"], t["type"], t["source"]) == (
        "a_trier",
        "P1",
        "bug",
        "manuel",
    )
    resp = await admin_client.patch(
        f"/admin/suivi/tickets/{t['id']}", json={"statut": "en_cours"}, headers=ADMIN
    )
    assert resp.status_code == 200 and resp.json()["statut"] == "en_cours"
    listing = (await admin_client.get("/admin/suivi/tickets", headers=ADMIN)).json()
    assert [x["id"] for x in listing["tickets"]] == [t["id"]]
    assert listing["statuts"][0] == "a_trier" and "termine" in listing["statuts"]
    assert "ci" in listing["types_preuve"]
    actions = {e["action"] for e in audit_mod.get_audit_log(limit=20)}
    assert {"suivi.ticket.create", "suivi.ticket.update"} <= actions


async def test_patch_termine_needs_proof(admin_client, repo):
    t = await _create(admin_client)
    url = f"/admin/suivi/tickets/{t['id']}"
    refused = await admin_client.patch(url, json={"statut": "termine"}, headers=ADMIN)
    assert refused.status_code == 409
    assert refused.json()["code"] == "preuve_requise"
    assert "preuve" in refused.json()["detail"]
    proof = await admin_client.post(
        url + "/preuves",
        json={"type": "commit", "valeur": "24fab88"},
        headers=ADMIN,
    )
    assert proof.status_code == 201
    done = await admin_client.patch(url, json={"statut": "termine"}, headers=ADMIN)
    assert done.status_code == 200 and done.json()["statut"] == "termine"


async def test_blocking_needs_reason(admin_client, repo):
    t = await _create(admin_client)
    url = f"/admin/suivi/tickets/{t['id']}"
    refused = await admin_client.patch(url, json={"statut": "bloque"}, headers=ADMIN)
    assert refused.status_code == 409 and refused.json()["code"] == "motif_requis"
    ok = await admin_client.patch(
        url, json={"statut": "bloque", "motif": "admin"}, headers=ADMIN
    )
    assert ok.status_code == 200 and ok.json()["motif"] == "admin"


async def test_unknown_ticket_and_bad_bodies(admin_client, repo):
    missing = await admin_client.patch(
        "/admin/suivi/tickets/999", json={"statut": "pret"}, headers=ADMIN
    )
    assert missing.status_code == 404 and missing.json()["code"] == "introuvable"
    for body in (
        {"titre": ""},
        {"titre": "x", "priorite": "P9"},
        {"titre": "x", "id": 3},
    ):
        resp = await admin_client.post("/admin/suivi/tickets", json=body, headers=ADMIN)
        assert resp.status_code == 422, body
    t = await _create(admin_client)
    resp = await admin_client.patch(
        f"/admin/suivi/tickets/{t['id']}", json={"statut": "done"}, headers=ADMIN
    )
    assert resp.status_code == 422


async def test_import_endpoint(admin_client, repo):
    first = await admin_client.post("/admin/suivi/import", headers=ADMIN)
    assert first.status_code == 200
    assert first.json() == {"crees": 2, "mis_a_jour": 0, "inchanges": 0}
    again = await admin_client.post("/admin/suivi/import", headers=ADMIN)
    assert again.json() == {"crees": 0, "mis_a_jour": 0, "inchanges": 2}
    assert audit_mod.get_audit_log(action="suivi.import", limit=5)


async def test_suivi_page_requires_session(admin_client):
    signed_out = await admin_client.get("/suivi", follow_redirects=False)
    assert signed_out.status_code in (302, 307)
    assert signed_out.headers["location"] == "/login?next=/suivi"
    signed_in = await admin_client.get("/suivi", headers=ADMIN)
    assert signed_in.status_code == 200
    assert "/dashboard/suivi.js" in signed_in.text

"""Docs of the personal dashboard keys (PB-AUTH-01)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_the_monitoring_guide_documents_personal_keys():
    text = _read("docs/guide/monitoring.md")
    assert "### Personal dashboard keys" in text
    for needle in (
        "/admin/people",
        "DELETE /admin/people/",
        "X-Plugboard-Admin-TOTP",
        "pba_",
        "principal",
    ):
        assert needle in text


def test_the_data_table_lists_the_store():
    assert "`admin_people.json`" in _read("docs/guide/getting-started.md")


def test_the_todo_entry_is_gone():
    assert "### Per-person dashboard keys" not in _read("TODOS.md")


def test_unreleased_names_the_feature_and_the_contract():
    unreleased = (
        _read("CHANGELOG.md").split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    )
    assert "PB-AUTH-01" in unreleased and "3.1.0" not in unreleased

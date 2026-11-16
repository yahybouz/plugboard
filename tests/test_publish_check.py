"""PB-REL-01: identity, secrets and private terms checked before publication.

The repository is public (GitLab and GitHub). ``ci/publish_check.py`` checks a
range of commits before it is sent:

1. author **and** committer of each commit are an allowed identity
   (``ci/publish-identities.txt``, extended by a private file);
2. gitleaks runs on the same range (argv checked with a fake gitleaks);
3. the private terms (``private/scan-terms.txt``, never committed) are looked
   for in the added lines, the messages, the file names and the identities;
   a finding names the commit, the file, the line and the term's line in the
   terms file, never the term itself. No terms file: an explicit failure,
   unless ``--allow-missing-terms``.

Each test builds a scratch git repository; no network, no real gitleaks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ci" / "publish_check.py"
HERMES = ("Hermes UAT", "hermes@uat.local")
OTHER = ("Someone Else", "someone@example.com")
# Fictitious terms, as in private/scan-terms.txt.example.
TERM = "Acme-Confidential"
REGEX_LINE = r"re:zz-?internal-host\b"
REGEX_HIT = "zzinternal-host"


def _git_env(home: Path, author=HERMES, committer=HERMES) -> dict:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_") and k not in ("CI_GIT_DIR", "PLUGBOARD_SCAN_TERMS")
    }
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        HOME=str(home),
        GIT_AUTHOR_NAME=author[0],
        GIT_AUTHOR_EMAIL=author[1],
        GIT_COMMITTER_NAME=committer[0],
        GIT_COMMITTER_EMAIL=committer[1],
    )
    return env


class Repo:
    def __init__(self, path: Path, home: Path):
        self.path, self.home = path, home
        path.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.commit({"README.md": "hello\n", ".gitleaks.toml": "# config\n"}, "init")
        self.git("checkout", "-q", "-b", "work")

    def git(self, *args, author=HERMES, committer=HERMES) -> str:
        done = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            env=_git_env(self.home, author, committer),
            check=True,
        )
        return done.stdout.strip()

    def commit(self, files: dict, message: str, author=HERMES, committer=HERMES):
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            self.git("add", "--", name)
        self.git("commit", "-q", "-m", message, author=author, committer=committer)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path / "repo", tmp_path)


@pytest.fixture
def terms(tmp_path):
    path = tmp_path / "scan-terms.txt"
    path.write_text(f"# private terms\n\n{TERM}\n{REGEX_LINE}\n", encoding="utf-8")
    return path


@pytest.fixture
def gitleaks(tmp_path):
    """A fake gitleaks that records its argv and exits with FAKE_GITLEAKS_EXIT."""
    record = tmp_path / "gitleaks-argv.json"
    exe = tmp_path / "bin" / "gitleaks"
    exe.parent.mkdir()
    exe.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys\n"
        f"open({str(record)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.exit(int(os.environ.get('FAKE_GITLEAKS_EXIT', '0')))\n",
        encoding="utf-8",
    )
    exe.chmod(0o755)
    return _FakeGitleaks(exe, record)


class _FakeGitleaks:
    def __init__(self, exe: Path, record: Path):
        self.exe, self.record = exe, record

    def __str__(self) -> str:
        return str(self.exe)

    def calls(self) -> list:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]


def run(repo: Repo, *args, env_extra=None, gitleaks=None, terms=None):
    env = _git_env(repo.home)
    env.update(env_extra or {})
    argv = [sys.executable, str(SCRIPT), "--repo", str(repo.path), *args]
    if gitleaks is not None:
        argv += ["--gitleaks", str(gitleaks)]
    if terms is not None:
        argv += ["--terms", str(terms)]
    done = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
    return done.returncode, done.stdout + done.stderr


def _assert_term_hidden(out: str):
    low = out.lower()
    assert TERM.lower() not in low
    assert REGEX_HIT not in low and "internal-host" not in low


# -- identities -------------------------------------------------------------


def test_a_clean_range_passes_and_gitleaks_gets_the_range(repo, terms, gitleaks):
    base = repo.git("rev-parse", "main")
    head = repo.commit({"a.txt": "nothing private\n"}, "feat: a")

    code, out = run(repo, "--base", "main", gitleaks=gitleaks, terms=terms)

    assert code == 0, out
    assert gitleaks.calls() == [
        [
            "git",
            "--no-banner",
            "--redact",
            "--exit-code",
            "1",
            "--config",
            ".gitleaks.toml",
            f"--log-opts={base}..{head}",
            ".",
        ]
    ]


def test_explicit_range(repo, terms, gitleaks):
    first = repo.commit({"a.txt": "1\n"}, "one")
    second = repo.commit({"b.txt": "2\n"}, "two")

    code, out = run(
        repo, "--range", f"{first}..{second}", gitleaks=gitleaks, terms=terms
    )

    assert code == 0, out
    assert gitleaks.calls()[0][-2] == f"--log-opts={first}..{second}"


@pytest.mark.parametrize("role", ["author", "committer"])
def test_a_foreign_identity_fails_and_is_not_printed(repo, terms, gitleaks, role):
    who = {"author": OTHER} if role == "author" else {"committer": OTHER}
    sha = repo.commit({"a.txt": "x\n"}, "feat: a", **who)

    code, out = run(repo, "--base", "main", gitleaks=gitleaks, terms=terms)

    assert code == 1, out
    assert sha[:12] in out
    assert ("auteur" if role == "author" else "committer") in out
    assert OTHER[0] not in out and OTHER[1] not in out


def test_show_identities_prints_them_for_a_local_run(repo, terms, gitleaks):
    repo.commit({"a.txt": "x\n"}, "feat: a", author=OTHER)
    code, out = run(
        repo, "--base", "main", "--show-identities", gitleaks=gitleaks, terms=terms
    )
    assert code == 1
    assert f"{OTHER[0]} <{OTHER[1]}>" in out


def test_an_identity_from_the_allowed_list_passes(repo, terms, gitleaks, tmp_path):
    repo.commit({"a.txt": "x\n"}, "feat: a", author=OTHER)
    extra = tmp_path / "ids.txt"
    extra.write_text(f"# extra\n{OTHER[0]} <{OTHER[1].upper()}>\n", encoding="utf-8")

    code, out = run(
        repo,
        "--base",
        "main",
        env_extra={"PLUGBOARD_PUBLISH_IDENTITIES": str(extra)},
        gitleaks=gitleaks,
        terms=terms,
    )

    assert code == 0, out


def test_the_versioned_list_allows_hermes_only():
    ids = (ROOT / "ci" / "publish-identities.txt").read_text(encoding="utf-8")
    lines = [x.strip() for x in ids.splitlines() if x.strip() and not x.startswith("#")]
    assert lines == ["Hermes UAT <hermes@uat.local>"]


# -- private terms ----------------------------------------------------------


def test_a_term_in_an_added_line(repo, terms, gitleaks):
    sha = repo.commit(
        {"docs/note.md": "line one\nline two\nsee acme-confidential here\n"}, "docs"
    )

    code, out = run(repo, "--base", "main", gitleaks=gitleaks, terms=terms)

    assert code == 1, out
    assert sha[:12] in out
    assert "docs/note.md" in out and "ligne 3" in out
    assert "terme n°1" in out and "ligne 3 du fichier de termes" in out
    _assert_term_hidden(out)


def test_a_regex_term(repo, terms, gitleaks):
    repo.commit({"a.txt": f"host {REGEX_HIT.upper()} up\n"}, "feat")
    code, out = run(repo, "--base", "main", gitleaks=gitleaks, terms=terms)
    assert code == 1, out
    assert "terme n°2" in out and "ligne 4 du fichier de termes" in out
    _assert_term_hidden(out)


def test_a_term_in_a_commit_message(repo, terms, gitleaks):
    sha = repo.commit({"a.txt": "x\n"}, f"fix: thing\n\nAsked by {TERM}.")
    code, out = run(repo, "--base", "main", gitleaks=gitleaks, terms=terms)
    assert code == 1, out
    assert sha[:12] in out and "message" in out and "ligne 3" in out
    _assert_term_hidden(out)


def test_a_term_in_a_file_name_is_not_printed(repo, terms, gitleaks):
    sha = repo.commit({f"notes/{TERM.lower()}-plan.md": "x\n"}, "docs")
    code, out = run(repo, "--base", "main", gitleaks=gitleaks, terms=terms)
    assert code == 1, out
    assert sha[:12] in out and "nom de fichier" in out
    _assert_term_hidden(out)


def test_a_term_in_an_identity(repo, terms, gitleaks, tmp_path):
    who = (f"{TERM} bot", "bot@example.com")
    extra = tmp_path / "ids.txt"
    extra.write_text(f"{who[0]} <{who[1]}>\n", encoding="utf-8")
    repo.commit({"a.txt": "x\n"}, "feat", author=who)
    code, out = run(
        repo,
        "--base",
        "main",
        "--identities",
        str(extra),
        gitleaks=gitleaks,
        terms=terms,
    )
    assert code == 1, out
    assert "identité" in out
    _assert_term_hidden(out)


def test_missing_terms_file_fails_explicitly(repo, gitleaks, tmp_path):
    repo.commit({"a.txt": "x\n"}, "feat")
    code, out = run(
        repo, "--base", "main", gitleaks=gitleaks, terms=tmp_path / "absent.txt"
    )
    assert code == 2, out
    assert "termes" in out


def test_no_terms_file_anywhere_fails_unless_allowed(repo, gitleaks):
    repo.commit({"a.txt": "x\n"}, "feat")
    code, out = run(repo, "--base", "main", gitleaks=gitleaks)
    assert code == 2, out
    assert "private/scan-terms.txt" in out

    code, out = run(repo, "--base", "main", "--allow-missing-terms", gitleaks=gitleaks)
    assert code == 0, out
    assert "NON vérifiés" in out


def test_an_empty_terms_file_counts_as_missing(repo, gitleaks, tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("# only a comment\n\n", encoding="utf-8")
    code, out = run(repo, "--base", "main", gitleaks=gitleaks, terms=empty)
    assert code == 2, out


def test_terms_found_next_to_the_source_repository(repo, gitleaks, tmp_path):
    """The CI runs in a throwaway export: the terms file is read from the
    ``private/`` folder beside the source repository's ``.git`` (CI_GIT_DIR)."""
    source = Repo(tmp_path / "source", tmp_path)
    (source.path / "private").mkdir()
    (source.path / "private" / "scan-terms.txt").write_text(TERM + "\n")
    repo.commit({"a.txt": f"{TERM}\n"}, "feat")

    code, out = run(
        repo,
        "--base",
        "main",
        env_extra={"CI_GIT_DIR": str(source.path / ".git")},
        gitleaks=gitleaks,
    )

    assert code == 1, out
    assert "terme n°1" in out
    _assert_term_hidden(out)


def test_terms_from_the_environment_variable(repo, gitleaks, terms):
    repo.commit({"a.txt": f"{TERM}\n"}, "feat")
    code, out = run(
        repo,
        "--base",
        "main",
        env_extra={"PLUGBOARD_SCAN_TERMS": str(terms)},
        gitleaks=gitleaks,
    )
    assert code == 1, out


def test_an_invalid_expression_is_named_by_its_line_only(repo, gitleaks, tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_text("ok-term\nre:secret(unclosed\n", encoding="utf-8")
    code, out = run(repo, "--base", "main", gitleaks=gitleaks, terms=bad)
    assert code == 2, out
    assert "ligne 2" in out and "secret(unclosed" not in out


def test_on_the_base_itself_the_tree_is_scanned(repo, gitleaks, terms):
    repo.git("checkout", "-q", "main")
    repo.commit({"tree.txt": f"x\n{TERM}\n"}, "on main")
    code, out = run(repo, "--base", "main", gitleaks=gitleaks, terms=terms)
    assert code == 1, out
    assert "tree.txt" in out and "ligne 2" in out
    assert gitleaks.calls()[0][0] == "dir"
    _assert_term_hidden(out)


# -- gitleaks ---------------------------------------------------------------


def test_a_gitleaks_finding_fails(repo, terms, gitleaks):
    repo.commit({"a.txt": "x\n"}, "feat")
    code, out = run(
        repo,
        "--base",
        "main",
        env_extra={"FAKE_GITLEAKS_EXIT": "1"},
        gitleaks=gitleaks,
        terms=terms,
    )
    assert code == 1, out
    assert "gitleaks" in out


def test_gitleaks_missing_fails_unless_skipped(repo, terms, tmp_path):
    repo.commit({"a.txt": "x\n"}, "feat")
    code, out = run(
        repo, "--base", "main", gitleaks=tmp_path / "no-gitleaks", terms=terms
    )
    assert code == 2, out

    code, out = run(repo, "--base", "main", "--skip-gitleaks", terms=terms)
    assert code == 0, out


# -- repository wiring ------------------------------------------------------


def test_private_is_ignored_except_the_example():
    def ignored(path):
        return (
            subprocess.run(
                ["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", path],
                capture_output=True,
            ).returncode
            == 0
        )

    assert ignored("private/scan-terms.txt")
    assert ignored("private/publish-identities.txt")
    assert not ignored("private/scan-terms.txt.example")


def test_the_example_terms_are_fictitious_and_parse():
    sys.path.insert(0, str(ROOT / "ci"))
    try:
        import publish_check
    finally:
        sys.path.pop(0)
    example = ROOT / "private" / "scan-terms.txt.example"
    found = publish_check.load_terms(example)
    assert len(found) >= 2
    text = example.read_text(encoding="utf-8")
    assert "re:" in text and "#" in text


def test_the_ci_runs_the_check_after_gitleaks():
    pipeline = json.loads((ROOT / "ci" / "pipeline.json").read_text(encoding="utf-8"))
    ids = [s["id"] for s in pipeline["steps"]]
    assert ids.index("publish-check") == ids.index("gitleaks") + 1
    step = pipeline["steps"][ids.index("publish-check")]
    assert step["argv"][:3] == ["{python}", "-B", "ci/publish_check.py"]
    assert step["argv"][3:5] == ["--base", "main"]
    assert "--allow-missing-terms" not in step["argv"]
    assert "PLUGBOARD_SCAN_TERMS" in pipeline["env_passthrough"]
    assert "CI_GIT_DIR" in pipeline["env_passthrough"]


def test_the_release_runbook():
    text = (ROOT / "docs" / "runbooks" / "release-publish.md").read_text(
        encoding="utf-8"
    )
    for needle in (
        "private/scan-terms.txt",
        "ci/publish_check.py",
        "--range",
        "GitLab",
        "GitHub",
    ):
        assert needle in text, needle
    assert text.index("GitLab") < text.index("GitHub")
    index = (ROOT / "docs" / "runbooks" / "README.md").read_text(encoding="utf-8")
    assert "release-publish.md" in index

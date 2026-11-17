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


# -- every form of diff (BUG-146) ---------------------------------------------
# An added line is read from the hunk it belongs to, whatever it looks like:
# no forbidden term may pass because a line or a file name resembles a header.


def _check(repo, *extra, env_extra=None, terms=None):
    return run(
        repo,
        "--base",
        "main",
        "--skip-gitleaks",
        *extra,
        env_extra=env_extra,
        terms=terms,
    )


@pytest.mark.parametrize(
    "lead",
    [
        "++",
        "++ b/other.txt",
        "+++ ",
        "+++ b/other.txt",
        "+++ /dev/null",
        "--",
        "---",
        "--- a/other.txt",
        "@@",
        "@@ -1 +1 @@",
        "diff --git a/x b/x",
        "\\ No newline at end of file",
        "index 0000000..1111111",
    ],
)
def test_an_added_line_that_looks_like_a_header(repo, terms, lead):
    """The line itself and every later added line of the file are read."""
    sha = repo.commit({"doc.txt": f"ok\n{lead} {TERM}\n{lead}\nlater {TERM}\n"}, "docs")

    code, out = _check(repo, terms=terms)

    assert code == 1, out
    assert sha[:12] in out
    assert "fichier doc.txt, ligne 2" in out
    assert "fichier doc.txt, ligne 4" in out
    assert "2 constat(s)" in out
    _assert_term_hidden(out)


def test_a_removed_line_that_looks_like_a_header(repo, terms):
    repo.commit({"doc.txt": "-- a/x\n++ b/y\nkeep\n"}, "base lines")
    repo.commit({"doc.txt": f"keep\n{TERM}\n"}, "replace")

    code, out = _check(repo, terms=terms)

    assert code == 1, out
    assert "fichier doc.txt, ligne 2" in out and "1 constat(s)" in out


@pytest.mark.parametrize(
    "name",
    [
        'we"ird.txt',
        "with space.txt",
        "with\ttab.txt",
        "back\\slash.txt",
        "new\nline.txt",
        "café-日本.txt",
        "-- a/dash.txt",
        "b/nested.txt",
    ],
)
def test_a_file_name_git_quotes_or_escapes(repo, terms, name):
    sha = repo.commit({name: f"one\n{TERM}\n"}, "docs")

    code, out = _check(repo, terms=terms)

    assert code == 1, out
    assert sha[:12] in out
    assert f"fichier {name}, ligne 2" in out
    assert "1 constat(s)" in out
    _assert_term_hidden(out)


def test_a_rename_with_a_change(repo, terms):
    repo.commit({"old.txt": "a\nb\nc\nd\ne\n"}, "base")
    repo.git("mv", "old.txt", "new name.txt")
    sha = repo.commit({"new name.txt": f"a\nb\n{TERM}\nc\nd\ne\n"}, "rename")

    code, out = _check(repo, terms=terms)

    assert code == 1, out
    assert sha[:12] in out and "fichier new name.txt, ligne 3" in out
    assert "1 constat(s)" in out


def test_a_new_file_and_a_file_deleted_then_recreated(repo, terms):
    repo.commit({"gone.txt": "safe\n"}, "add")
    repo.git("rm", "-q", "gone.txt")
    repo.git("commit", "-q", "-m", "delete")
    again = repo.commit({"gone.txt": f"{TERM}\n"}, "recreate")
    fresh = repo.commit({"fresh.txt": f"x\ny\n{TERM}\n"}, "new")

    code, out = _check(repo, terms=terms)

    assert code == 1, out
    assert f"commit {again[:12]}, fichier gone.txt, ligne 1" in out
    assert f"commit {fresh[:12]}, fichier fresh.txt, ligne 3" in out
    assert "2 constat(s)" in out


def test_a_file_of_the_base_deleted_then_recreated(repo, terms):
    """Deleted on the base, recreated on the branch: the whole content is new."""
    repo.git("checkout", "-q", "main")
    repo.commit({"page.txt": "a\n"}, "add on main")
    repo.git("checkout", "-q", "work")
    repo.git("merge", "-q", "main")
    repo.git("rm", "-q", "page.txt")
    repo.git("commit", "-q", "-m", "delete")
    sha = repo.commit({"page.txt": f"a\n{TERM}\n"}, "recreate")

    code, out = _check(repo, terms=terms)

    assert code == 1, out
    assert f"commit {sha[:12]}, fichier page.txt, ligne 2" in out


def test_a_mode_change(repo, terms):
    repo.commit({"run.sh": "#!/bin/sh\necho ok\n"}, "script")
    (repo.path / "run.sh").chmod(0o755)
    repo.git("add", "run.sh")
    repo.git("commit", "-q", "-m", "chmod only")
    (repo.path / "run.sh").chmod(0o644)
    sha = repo.commit({"run.sh": f"#!/bin/sh\necho ok\necho {TERM}\n"}, "chmod back")

    code, out = _check(repo, terms=terms)

    assert code == 1, out
    assert f"commit {sha[:12]}, fichier run.sh, ligne 3" in out
    assert "1 constat(s)" in out


def test_a_file_turned_into_a_symlink_whose_target_has_a_term(repo, terms):
    repo.commit({"link": "plain\n"}, "file")
    (repo.path / "link").unlink()
    (repo.path / "link").symlink_to(f"/opt/{TERM}")
    repo.git("add", "link")
    sha = repo.commit({}, "symlink")

    code, out = _check(repo, terms=terms)

    assert code == 1, out
    assert f"commit {sha[:12]}, fichier link, ligne 1" in out


def test_no_newline_at_end_of_file(repo, terms):
    repo.commit({"tail.txt": "a\nb"}, "no final newline")
    sha = repo.commit({"tail.txt": f"a\nb\nc {TERM}"}, "append")

    code, out = _check(repo, terms=terms)

    assert code == 1, out
    assert f"commit {sha[:12]}, fichier tail.txt, ligne 3" in out
    assert "1 constat(s)" in out


def test_a_term_introduced_by_a_merge_resolution(repo, terms):
    repo.commit({"conf.txt": "value\nend\n"}, "base value")
    repo.git("checkout", "-q", "-b", "side")
    repo.commit({"conf.txt": "side value\nend\n"}, "side")
    repo.git("checkout", "-q", "work")
    repo.commit({"conf.txt": "work value\nend\n"}, "work")
    proc = subprocess.run(
        ["git", "-C", str(repo.path), "merge", "-q", "side"],
        capture_output=True,
        env=_git_env(repo.home),
    )
    assert proc.returncode != 0  # conflict, resolved by hand
    (repo.path / "conf.txt").write_text(f"{TERM} value\nend\n", encoding="utf-8")
    repo.git("add", "conf.txt")
    repo.git("commit", "-q", "--no-edit")
    merge = repo.git("rev-parse", "HEAD")

    code, out = _check(repo, terms=terms)

    assert code == 1, out
    assert f"commit {merge[:12]}, fichier conf.txt, ligne 1" in out
    assert "1 constat(s)" in out


def test_a_term_in_a_quoted_file_name_is_masked(repo, terms):
    sha = repo.commit({f'odd "{TERM}".txt': f"x\n{TERM}\n"}, "docs")

    code, out = _check(repo, terms=terms)

    assert code == 1, out
    assert f"commit {sha[:12]}, nom de fichier" in out
    assert "(chemin masqué : il contient un terme), ligne 2" in out
    _assert_term_hidden(out)


def test_git_config_and_attributes_do_not_change_the_reading(repo, terms, tmp_path):
    """A user or repository setting that changes git's output changes nothing."""
    sub = repo.path / "sub"
    sub.mkdir()
    (sub / "keep.txt").write_text("x\n", encoding="utf-8")
    hide = tmp_path / "hide.sh"
    hide.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hide.chmod(0o755)
    repo.commit({"sub/keep.txt": "x\n"}, "sub")
    files = {
        ".gitattributes": "*.bin binary\n*.md -diff\n*.txt diff=hide\n",
        "a.bin": f"{TERM}\n",
        "b.md": f"title\n{TERM}\n",
        "c.txt": f"x\ny\n{TERM}\n",
        'café "q".cfg': f"{TERM}\n",
    }
    sha = repo.commit(files, "docs")
    for key, value in (
        ("core.quotePath", "true"),
        ("color.ui", "always"),
        ("color.diff", "always"),
        ("diff.noprefix", "true"),
        ("diff.mnemonicPrefix", "true"),
        ("diff.relative", "true"),
        ("diff.suppressBlankEmpty", "true"),
        ("diff.renames", "copies"),
        ("diff.external", str(hide)),
        ("diff.hide.textconv", str(hide)),
        ("diff.hide.command", str(hide)),
        ("diff.submodule", "log"),
        ("core.attributesFile", str(tmp_path / "none")),
    ):
        repo.git("config", key, value)

    code, out = run(
        repo,
        "--base",
        "main",
        "--skip-gitleaks",
        env_extra={
            "GIT_DIFF_OPTS": "--unified=5",
            "GIT_EXTERNAL_DIFF": str(hide),
        },
        terms=terms,
    )
    # Run from a sub-directory: diff.relative must not hide the rest of the tree.
    in_sub = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(sub), "--base", "main"]
        + ["--skip-gitleaks", "--terms", str(terms)],
        capture_output=True,
        text=True,
        env=_git_env(repo.home),
        timeout=60,
    )
    out_sub = in_sub.stdout + in_sub.stderr

    assert code == 1, out
    for where in (
        "fichier a.bin, ligne 1",
        "fichier b.md, ligne 2",
        "fichier c.txt, ligne 3",
        'fichier café "q".cfg, ligne 1',
    ):
        assert f"commit {sha[:12]}, {where}" in out, where
    assert "4 constat(s)" in out
    assert in_sub.returncode == 1, out_sub
    assert "4 constat(s)" in out_sub


def test_line_numbers_follow_the_new_file_across_hunks(repo, terms):
    old = "".join(f"l{i}\n" for i in range(1, 21))
    repo.commit({"long.txt": old}, "base")
    new = old.replace("l3\n", f"l3\n{TERM}\n").replace("l15\n", f"{TERM} 15\n")
    new = new.replace("l20\n", f"l20\n++ {TERM}\n")
    sha = repo.commit({"long.txt": new}, "edits")

    code, out = _check(repo, terms=terms)

    assert code == 1, out
    for line in (4, 16, 22):
        assert f"commit {sha[:12]}, fichier long.txt, ligne {line}" in out, line
    assert "3 constat(s)" in out


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


def test_private_is_ignored_except_the_example(tmp_path):
    # The CI runs on an export of the commit, not a git checkout: the rules
    # of the repository's .gitignore are checked in a scratch repository.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(
        (ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8"
    )

    def ignored(path):
        proc = subprocess.run(
            ["git", "-C", str(tmp_path), "check-ignore", "-q", "--no-index", path],
            capture_output=True,
            text=True,
        )
        assert proc.returncode in (0, 1), proc.stderr
        return proc.returncode == 0

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

#!/usr/bin/env python3
"""Contrôles avant publication (PB-REL-01) — stdlib uniquement, Linux et macOS.

Le dépôt est public (GitLab et GitHub). Sur une plage de commits :

1. identités : auteur ET committer de chaque commit figurent dans
   ``ci/publish-identities.txt`` (versionné) ou dans une liste privée
   (``private/publish-identities.txt``, ``PLUGBOARD_PUBLISH_IDENTITIES``,
   ``--identities``) ; une identité refusée est masquée, sauf
   ``--show-identities`` (poste local, jamais en CI) ;
2. gitleaks sur la même plage (``--skip-gitleaks`` quand l'étape gitleaks de
   la CI vient de la balayer) ;
3. termes privés : ``private/scan-terms.txt`` (jamais versionné) cherché
   dans les lignes ajoutées, les messages, les noms de fichiers et les
   identités. Un constat donne le commit, le fichier, la ligne et le numéro
   du terme (sa ligne dans le fichier de termes), jamais le terme ; un chemin
   qui contient un terme est masqué.

Fichier de termes : une expression par ligne, ``#`` en tête = commentaire,
lignes vides ignorées, insensible à la casse. Une ligne est un texte exact
(comme ``grep -F`` dans ``scripts/pre-push``) ; préfixée par ``re:``, une
expression régulière Python. Recherché dans cet ordre : ``--terms``,
``PLUGBOARD_SCAN_TERMS``, ``private/scan-terms.txt`` à la racine du dépôt
contrôlé, puis dans le dossier du dépôt source (celui de ``CI_GIT_DIR`` en
CI, sinon celui du ``.git`` commun : un worktree lit le fichier du dépôt
principal). Absent ou vide : échec (code 2), sauf ``--allow-missing-terms``,
qui le dit en clair.

Plage : ``--base REF`` (merge-base(HEAD, REF)..HEAD, comme l'étape gitleaks ;
plage vide = arbre de HEAD balayé), ``--range A..B``, ou ``--tree``.
Codes : 0 = publiable, 1 = constat, 2 = contrôle impossible.

  python3 ci/publish_check.py --base main
  python3 ci/publish_check.py --range origin/main..release/4.14.5
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PREFIX = "publish_check :"
HERE = Path(__file__).resolve().parent
VERSIONED_IDENTITIES = HERE / "publish-identities.txt"
TERMS_NAME = "scan-terms.txt"
IDS_NAME = "publish-identities.txt"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class Stop(Exception):
    """Contrôle impossible (code 2)."""


def say(msg: str) -> None:
    print(f"{PREFIX} {msg}", flush=True)


# --------------------------------------------------------------------------- git
def git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.quotepath=off", *args],
        capture_output=True,
        check=False,
    )
    if check and done.returncode:
        raise Stop(f"git {args[0]} en échec : {done.stderr.decode(errors='replace')}")
    return done.stdout.decode("utf-8", errors="replace")


def resolve(repo: Path, ref: str) -> str:
    sha = git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
    if not sha.strip():
        raise Stop(f"référence introuvable : {ref}")
    return sha.strip()


# --------------------------------------------------------------------------- lookup
def private_dirs(repo: Path) -> list[Path]:
    """``private/`` du dépôt contrôlé, puis celui du dépôt source."""
    dirs = []
    top = git(repo, "rev-parse", "--show-toplevel", check=False).strip()
    if top:
        dirs.append(Path(top) / "private")
    ci_git = os.environ.get("CI_GIT_DIR")
    if ci_git:
        p = Path(ci_git).resolve()
        dirs.append((p if (p / ".git").exists() else p.parent) / "private")
    common = git(
        repo, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False
    ).strip()
    if common:
        dirs.append(Path(common).parent / "private")
    unique: list[Path] = []
    for d in dirs:
        if d not in unique:
            unique.append(d)
    return unique


def find_terms(repo: Path, explicit: str | None) -> Path | None:
    chosen = explicit or os.environ.get("PLUGBOARD_SCAN_TERMS")
    if chosen:
        path = Path(chosen)
        if not path.is_file():
            raise Stop(f"fichier de termes introuvable : {path}")
        return path
    for d in private_dirs(repo):
        if (d / TERMS_NAME).is_file():
            return d / TERMS_NAME
    return None


def load_terms(path: Path) -> list[tuple[int, int, re.Pattern]]:
    """[(numéro du terme, ligne dans le fichier, motif)]. Jamais le texte."""
    terms = []
    lines = path.read_text(encoding="utf-8").replace("\r", "").splitlines()
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            try:
                pattern = re.compile(line[3:], re.IGNORECASE)
            except re.error:
                raise Stop(f"expression invalide ligne {lineno} du fichier de termes")
        else:
            pattern = re.compile(re.escape(line), re.IGNORECASE)
        terms.append((len(terms) + 1, lineno, pattern))
    return terms


def load_identities(repo: Path, extra: list[str]) -> set[str]:
    # Obligatoires : la liste versionnée et celles nommées explicitement.
    required = [VERSIONED_IDENTITIES] + [Path(x) for x in extra]
    if os.environ.get("PLUGBOARD_PUBLISH_IDENTITIES"):
        required.append(Path(os.environ["PLUGBOARD_PUBLISH_IDENTITIES"]))
    for f in required:
        if not f.is_file():
            raise Stop(f"liste d'identités introuvable : {f}")
    optional = [d / IDS_NAME for d in private_dirs(repo)]
    allowed = set()
    for f in required + [f for f in optional if f.is_file()]:
        for raw in f.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                allowed.add(norm_identity(line))
    return allowed


def norm_identity(text: str) -> str:
    m = re.fullmatch(r"\s*(.*?)\s*<\s*(.*?)\s*>\s*", text)
    if not m:
        return text.strip()
    return f"{m.group(1)} <{m.group(2).lower()}>"


# --------------------------------------------------------------------------- scan
class Report:
    def __init__(self, terms):
        self.terms = terms
        self.findings = 0

    def hits(self, text: str):
        return [(n, line) for n, line, pat in self.terms if pat.search(text)]

    def shown_path(self, path: str) -> str:
        return "(chemin masqué : il contient un terme)" if self.hits(path) else path

    def term(self, n: int, line: int, where: str) -> None:
        self.findings += 1
        say(f"terme n°{n} (ligne {line} du fichier de termes) : {where}")

    def scan_text(self, text: str, where) -> None:
        for lineno, line in enumerate(text.splitlines(), 1):
            for n, tl in self.hits(line):
                self.term(n, tl, where(lineno))


def commit_fields(repo: Path, sha: str) -> dict:
    raw = git(repo, "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B", sha)
    an, ae, cn, ce, body = raw.split("\x00", 4)
    return {"author": (an, ae), "committer": (cn, ce), "message": body.rstrip("\n")}


def added_lines(repo: Path, sha: str):
    """(chemin, numéro de ligne, texte) des lignes ajoutées par le commit, et
    les chemins touchés (premier parent ; arbre vide pour une racine)."""
    parent = git(repo, "rev-parse", "--verify", "-q", f"{sha}^1", check=False).strip()
    base = parent or EMPTY_TREE
    names = git(repo, "diff", "--name-only", "--no-renames", "-z", base, sha)
    paths = [p for p in names.split("\x00") if p]
    diff = git(
        repo,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--unified=0",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        base,
        sha,
    )
    lines = []
    path, n = None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            m = re.match(r"@@ -\S+ \+(\d+)", line)
            n = int(m.group(1)) if m else 0
        elif line.startswith("+") and path is not None:
            lines.append((path, n, line[1:]))
            n += 1
    return paths, lines


def scan_commit(repo: Path, sha: str, report: Report, allowed: set, show: bool) -> int:
    bad = 0
    fields = commit_fields(repo, sha)
    short = sha[:12]
    for role, label in (("author", "auteur"), ("committer", "committer")):
        name, email = fields[role]
        ident = f"{name} <{email}>"
        if norm_identity(ident) not in allowed:
            bad += 1
            shown = ident if show else "masquée (--show-identities pour la voir)"
            say(f"identité non autorisée : commit {short}, {label} {shown}")
        for n, tl in report.hits(ident):
            report.term(n, tl, f"commit {short}, identité {label}")
    report.scan_text(fields["message"], lambda i: f"commit {short}, message, ligne {i}")
    paths, lines = added_lines(repo, sha)
    for i, path in enumerate(paths, 1):
        for n, tl in report.hits(path):
            report.term(n, tl, f"commit {short}, nom de fichier n°{i} du commit")
    for path, lineno, text in lines:
        for n, tl in report.hits(text):
            report.term(
                n,
                tl,
                f"commit {short}, fichier {report.shown_path(path)}, ligne {lineno}",
            )
    return bad


def scan_tree(repo: Path, head: str, report: Report) -> None:
    listing = git(repo, "ls-tree", "-r", "-z", "--full-tree", head)
    blobs = []
    for entry in listing.split("\x00"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        _mode, kind, oid = meta.split()
        for n, tl in report.hits(path):
            report.term(n, tl, f"arbre {head[:12]}, nom de fichier masqué")
        if kind == "blob":
            blobs.append((oid, path))
    if not blobs:
        return
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input=b"".join(f"{oid}\n".encode() for oid, _ in blobs),
        capture_output=True,
        check=True,
    )
    out, pos = proc.stdout, 0
    for _oid, path in blobs:
        end = out.index(b"\n", pos)
        size = int(out[pos:end].split()[2])
        data = out[end + 1 : end + 1 + size]
        pos = end + 1 + size + 1
        if b"\x00" in data[:8000]:
            continue  # binaire
        where = f"arbre {head[:12]}, fichier {report.shown_path(path)}"
        report.scan_text(
            data.decode("utf-8", errors="replace"), lambda i: f"{where}, ligne {i}"
        )


# --------------------------------------------------------------------------- gitleaks
def run_gitleaks(repo: Path, exe: str, log_opts: str | None) -> int:
    argv = [exe, "git" if log_opts else "dir", "--no-banner", "--redact"]
    argv += ["--exit-code", "1"]
    if (repo / ".gitleaks.toml").is_file():
        argv += ["--config", ".gitleaks.toml"]
    if log_opts:
        argv.append(f"--log-opts={log_opts}")
    argv.append(".")
    return subprocess.run(argv, cwd=repo, check=False).returncode


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--repo", default=".", help="dépôt contrôlé (défaut : .)")
    plage = p.add_mutually_exclusive_group(required=True)
    plage.add_argument("--base", help="plage merge-base(HEAD, BASE)..HEAD")
    plage.add_argument("--range", dest="plage", help="plage A..B")
    plage.add_argument("--tree", action="store_true", help="arbre de HEAD entier")
    p.add_argument("--terms", help="fichier de termes (sinon recherche documentée)")
    p.add_argument(
        "--allow-missing-terms",
        action="store_true",
        help="sans fichier de termes, avertir au lieu d'échouer",
    )
    p.add_argument("--identities", action="append", default=[])
    p.add_argument("--show-identities", action="store_true")
    p.add_argument("--gitleaks", help="exécutable gitleaks (défaut : PATH)")
    p.add_argument("--skip-gitleaks", action="store_true")
    a = p.parse_args(argv)
    repo = Path(a.repo).resolve()
    try:
        return check(a, repo)
    except Stop as exc:
        say(f"contrôle impossible : {exc}")
        return 2


def check(a, repo: Path) -> int:
    # Plage
    head = resolve(repo, "HEAD")
    commits: list[str] = []
    log_opts = None
    if a.base:
        mb = git(repo, "merge-base", "HEAD", resolve(repo, a.base)).strip()
        commits = git(repo, "rev-list", "--reverse", f"{mb}..{head}").split()
        if commits:
            log_opts = f"{mb}..{head}"
            say(f"plage {mb[:12]}..{head[:12]} ({len(commits)} commits)")
        else:
            say(f"aucun commit propre à la branche : arbre de {head[:12]} balayé")
    elif a.plage:
        if ".." not in a.plage:
            raise Stop("--range attend A..B")
        left, right = a.plage.split("..", 1)
        left, head = resolve(repo, left), resolve(repo, right or "HEAD")
        commits = git(repo, "rev-list", "--reverse", f"{left}..{head}").split()
        log_opts = f"{left}..{head}"
        say(f"plage {left[:12]}..{head[:12]} ({len(commits)} commits)")
    else:
        say(f"arbre de {head[:12]} balayé")
    tree_mode = not a.plage and not commits

    # Préalables : rien ne passe en vert faute de fichier ou d'outil.
    terms_path = find_terms(repo, a.terms)
    terms = load_terms(terms_path) if terms_path else []
    if not terms and not a.allow_missing_terms:
        where = ", ".join(str(d / TERMS_NAME) for d in private_dirs(repo))
        raise Stop(
            "aucun terme privé : créer private/scan-terms.txt "
            f"(cherché : {where}) ou PLUGBOARD_SCAN_TERMS ; "
            "--allow-missing-terms pour passer outre, en le disant"
        )
    exe = None
    if not a.skip_gitleaks:
        exe = a.gitleaks or shutil.which("gitleaks")
        if not exe or not (Path(exe).is_file() and os.access(exe, os.X_OK)):
            raise Stop(
                "gitleaks introuvable (--gitleaks, PATH) ; --skip-gitleaks si déjà lancé"
            )
    allowed = load_identities(repo, a.identities)

    report = Report(terms)
    bad_ids = 0
    for sha in commits:
        bad_ids += scan_commit(repo, sha, report, allowed, a.show_identities)
    if tree_mode and terms:
        scan_tree(repo, head, report)

    leaks = 0
    if exe:
        leaks = run_gitleaks(repo, exe, None if tree_mode else log_opts)
        if leaks:
            say(f"gitleaks : constat ou erreur (code {leaks})")
    else:
        say("gitleaks non relancé (--skip-gitleaks)")

    if terms:
        say(f"termes privés : {len(terms)} cherchés, {report.findings} constat(s)")
    else:
        say("AVERTISSEMENT : termes privés NON vérifiés (--allow-missing-terms)")
    if bad_ids or report.findings or leaks:
        say(
            f"publication REFUSÉE : {bad_ids} identité(s), "
            f"{report.findings} terme(s), gitleaks {'KO' if leaks else 'OK'}"
        )
        return 1
    say(f"publication possible : {len(commits)} commit(s) contrôlé(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

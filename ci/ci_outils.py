#!/usr/bin/env python3
"""Outils de la CI locale (qd-ci) — stdlib uniquement, Linux et macOS.

Sous-commandes :
  git-depot      fait du dossier courant (export qd-ci) un dépôt Git du commit EXACT
                 CI_COMMIT_SHA, objets lus en lecture seule dans CI_GIT_DIR (alternates),
                 arbre vérifié identique ; --ref NOM recrée des branches de base.
  services       démarre des conteneurs jetables (PostgreSQL, Redis) sur 127.0.0.1:<port
                 aléatoire>, exporte les variables demandées, lance la commande puis
                 détruit les conteneurs (finally + SIGTERM).
  gitleaks       secrets : `gitleaks git` sur base..HEAD si dépôt Git, sinon `gitleaks dir`.
  image-docker   docker build d'une cible + contrôles (utilisateur, healthcheck, commandes
                 lancées dans l'image sans réseau, ex. `claude --version`, exécutables du
                 PATH comparés à une liste versionnée), image retirée.

Docker : DOCKER_HOST de l'appelant (env_passthrough) ; sur l'UAT, le Docker rootless d'agent.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets as _secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time

DOCKER = os.environ.get("CI_DOCKER", "docker")


def _run(argv, **kw):
    return subprocess.run(argv, check=False, **kw)


def _die(msg: str, code: int = 2) -> None:
    print(f"ci_outils : {msg}", file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------- git-depot
def cmd_git_depot(args) -> int:
    git_dir = os.environ.get("CI_GIT_DIR")
    sha = os.environ.get("CI_COMMIT_SHA")
    if not git_dir or not sha:
        _die("CI_GIT_DIR et CI_COMMIT_SHA requis (env_passthrough)", 125)
    git_dir = os.path.abspath(git_dir)
    if os.path.isdir(os.path.join(git_dir, ".git")):
        git_dir = os.path.join(git_dir, ".git")
    common = _run(["git", "--git-dir", git_dir, "rev-parse", "--git-common-dir"],
                  capture_output=True, text=True).stdout.strip()
    if common:
        git_dir = common if os.path.isabs(common) else os.path.normpath(os.path.join(git_dir, common))
    objects = os.path.join(git_dir, "objects")
    if not os.path.isdir(objects):
        _die(f"objets Git introuvables : {objects}")
    if os.path.exists(".git"):
        _die(".git existe déjà dans l'export")
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    steps = [
        ["git", "init", "-q"],
        ["git", "config", "user.name", "CI locale"],
        ["git", "config", "user.email", "ci@localhost"],
        ["git", "config", "core.fileMode", "true"],
    ]
    for argv in steps:
        if _run(argv, env=env).returncode:
            _die("git init en échec", 1)
    with open(".git/objects/info/alternates", "w", encoding="utf-8") as fh:
        fh.write(objects + "\n")
    commit = _run(["git", "rev-parse", "--verify", f"{sha}^{{commit}}"], env=env,
                  capture_output=True, text=True).stdout.strip()
    if not commit:
        _die(f"commit {sha} introuvable dans {git_dir}", 1)
    branch = args.branche or "ci-locale"
    for argv in (["git", "update-ref", f"refs/heads/{branch}", commit],
                 ["git", "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
                 ["git", "read-tree", commit]):
        if _run(argv, env=env).returncode:
            _die("préparation de l'index en échec", 1)
    for ref in args.ref or []:
        val = _run(["git", "--git-dir", git_dir, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                   capture_output=True, text=True, env=env).stdout.strip()
        if val and ref != branch:
            _run(["git", "update-ref", f"refs/heads/{ref}", val], env=env)
            print(f"base {ref} = {val}")
    _run(["git", "update-index", "-q", "--refresh"], env=env, capture_output=True)
    # Arbre exporté == arbre du commit (fichiers suivis) : sinon refus.
    diff = _run(["git", "diff", "--quiet", commit, "--"], env=env)
    if diff.returncode:
        _run(["git", "diff", "--stat", commit, "--"], env=env)
        _die("l'export ne correspond pas au commit", 1)
    print(f"dépôt Git du commit exact {commit} (objets en lecture seule : {objects})")
    return 0


# --------------------------------------------------------------------------- services
def _port_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _attendre(argv, timeout: float, quoi: str) -> None:
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        if _run(argv, capture_output=True).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"{quoi} non prêt après {timeout:.0f}s")


def cmd_services(args) -> int:
    if not args.commande or args.commande[0] != "--":
        _die("usage : services [options] -- commande ...")
    commande = args.commande[1:]
    if not commande:
        _die("commande vide")
    run_id = f"ci-{os.getpid()}-{_secrets.token_hex(3)}"
    conteneurs: list[str] = []
    valeurs: dict[str, str] = {}

    def nettoyer(*_):
        for nom in conteneurs:
            _run([DOCKER, "rm", "-fv", nom], capture_output=True)
        conteneurs.clear()

    def sur_signal(signum, _frame):
        nettoyer()
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, sur_signal)
    signal.signal(signal.SIGINT, sur_signal)
    try:
        if args.postgres:
            nom = f"{run_id}-pg"
            mdp = _secrets.token_hex(16)
            port = _port_libre()
            argv = [DOCKER, "run", "-d", "--rm", "--name", nom, "--label", "ci.locale=1",
                    "-p", f"127.0.0.1:{port}:5432", "-e", f"POSTGRES_PASSWORD={mdp}",
                    "-e", f"POSTGRES_USER={args.pg_user}", "-e", f"POSTGRES_DB={args.pg_db}",
                    "--tmpfs", "/var/lib/postgresql:rw", "--shm-size", "256m"]
            if args.pull_never:
                argv.insert(3, "--pull=never")
            r = _run(argv + [args.postgres], capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(f"docker run postgres : {r.stderr.strip()[-400:]}")
            conteneurs.append(nom)
            _attendre([DOCKER, "exec", nom, "pg_isready", "-h", "127.0.0.1", "-U", args.pg_user,
                       "-d", args.pg_db], 90, "PostgreSQL")
            # pg_isready peut répondre pendant l'init : vérifier une vraie requête TCP.
            _attendre([DOCKER, "exec", "-e", f"PGPASSWORD={mdp}", nom, "psql", "-h", "127.0.0.1", "-U",
                       args.pg_user, "-d", args.pg_db, "-tAc", "select 1"], 60, "PostgreSQL (requête)")
            for db in args.pg_extra_db or []:
                _run([DOCKER, "exec", nom, "createdb", "-U", args.pg_user, db], capture_output=True)
            valeurs.update(pg_port=str(port), pg_user=args.pg_user, pg_password=mdp,
                           pg_db=args.pg_db, pg_container=nom)
            print(f"PostgreSQL jetable {args.postgres} sur 127.0.0.1:{port} ({nom})")
        if args.redis:
            nom = f"{run_id}-redis"
            port = _port_libre()
            r = _run([DOCKER, "run", "-d", "--rm", "--name", nom, "--label", "ci.locale=1",
                      "-p", f"127.0.0.1:{port}:6379", args.redis], capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(f"docker run redis : {r.stderr.strip()[-400:]}")
            conteneurs.append(nom)
            _attendre([DOCKER, "exec", nom, "redis-cli", "ping"], 60, "Redis")
            valeurs.update(redis_port=str(port), redis_container=nom)
            print(f"Redis jetable {args.redis} sur 127.0.0.1:{port} ({nom})")
        env = dict(os.environ)
        for spec in args.env or []:
            nom, _, modele = spec.partition("=")
            # Gabarit <nom> (les accolades sont réservées aux placeholders qd-ci).
            env[nom] = re.sub(r"<(\w+)>", lambda m: valeurs[m.group(1)], modele)
        if args.env_file:
            os.makedirs(os.path.dirname(os.path.abspath(args.env_file)), exist_ok=True)
            fd = os.open(args.env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for spec in args.env or []:
                    nom = spec.partition("=")[0]
                    fh.write(f"{nom}={env[nom]}\n")
        sys.stdout.flush()
        return subprocess.call(commande, env=env)
    except RuntimeError as exc:
        print(f"ci_outils services : {exc}", file=sys.stderr)
        return 125
    finally:
        nettoyer()
        reste = _run([DOCKER, "ps", "-aq", "--filter", f"name={run_id}"], capture_output=True, text=True)
        if reste.stdout.strip():
            print("ci_outils services : conteneurs résiduels !", file=sys.stderr)


# --------------------------------------------------------------------------- gitleaks
def cmd_gitleaks(args) -> int:
    exe = shutil.which("gitleaks")
    if not exe:
        _die("gitleaks absent du PATH", 125)
    base = ["--no-banner", "--redact", "--exit-code", "1"]
    if args.config and os.path.exists(args.config):
        base += ["--config", args.config]
    if os.path.isdir(".git") and args.base:
        mb = _run(["git", "merge-base", "HEAD", args.base], capture_output=True, text=True).stdout.strip()
        if mb:
            n = _run(["git", "rev-list", "--count", f"{mb}..HEAD"], capture_output=True, text=True).stdout.strip()
            print(f"gitleaks git : {mb[:12]}..HEAD ({n} commits)")
            if n == "0":
                print("aucun commit propre à la branche : balayage de l'arbre")
            else:
                return _run([exe, "git", *base, f"--log-opts={mb}..HEAD", "."]).returncode
    print("gitleaks dir : arbre exact du commit")
    return _run([exe, "dir", *base, "."]).returncode


# --------------------------------------------------------------------------- image-docker
def cmd_image_docker(args) -> int:
    tag = f"ci-locale/{args.nom}:{os.environ.get('CI_COMMIT_SHA', 'local')[:12]}"
    # Restes d'une exécution tuée (SIGKILL au délai de l'étape : pas de finally).
    _nettoyer_verifications(tag)
    _run([DOCKER, "image", "rm", "-f", tag], capture_output=True)
    # Journal complet : un RUN en échec (npm ETARGET, 429, réseau) doit montrer sa cause.
    argv = [DOCKER, "build", "--progress=plain", "-t", tag, "-f", args.dockerfile]
    if args.target:
        argv += ["--target", args.target]
    argv.append(".")
    r = _run(argv)
    if r.returncode:
        return r.returncode
    try:
        rc = 0
        if args.user is not None:
            u = _run([DOCKER, "image", "inspect", tag, "--format", "{{.Config.User}}"],
                     capture_output=True, text=True).stdout.strip()
            print(f"utilisateur de l'image : {u!r}")
            if u != args.user:
                print(f"attendu {args.user!r}", file=sys.stderr)
                rc = 1
        if args.healthcheck:
            h = _run([DOCKER, "image", "inspect", tag, "--format", "{{json .Config.Healthcheck.Test}}"],
                     capture_output=True, text=True).stdout.strip()
            print(f"healthcheck : {h}")
            if args.healthcheck not in h:
                rc = 1
        commandes = list(args.verifier or [])
        if args.verifier_amd64:
            arch = _run([DOCKER, "image", "inspect", tag, "--format", "{{.Architecture}}"],
                        capture_output=True, text=True).stdout.strip()
            if arch == "amd64":
                commandes += args.verifier_amd64
            elif not arch:
                print("architecture de l'image illisible : contrôles amd64 impossibles", file=sys.stderr)
                rc = 1
            else:
                for cmd in args.verifier_amd64:
                    print(f"non vérifié sur {arch} (amd64 seulement) : {cmd}")
        for cmd in commandes:
            if _verifier(tag, cmd, args.delai_verifier):
                rc = 1
        if args.executables_path and _comparer_path(tag, args.executables_path, args.delai_verifier):
            rc = 1
        return rc
    finally:
        _run([DOCKER, "image", "rm", tag], capture_output=True)


def _nettoyer_verifications(tag: str) -> None:
    """Retire les conteneurs de contrôle de cette image restés d'une exécution tuée."""
    reste = _run([DOCKER, "ps", "-aq", "--filter", f"label=ci.locale.image={tag}"],
                 capture_output=True, text=True).stdout.split()
    if reste:
        _run([DOCKER, "rm", "-f", *reste], capture_output=True)


def _verifier(tag: str, cmd: str, delai: float) -> int:
    """Lance `cmd` dans l'image (utilisateur de l'image, sans réseau) ; 0 si succès."""
    nom = f"ci-verif-{os.getpid()}-{_secrets.token_hex(3)}"
    argv = [DOCKER, "run", "--rm", "--name", nom, "--label", f"ci.locale.image={tag}",
            "--network", "none", tag, *shlex.split(cmd)]
    try:
        r = _run(argv, capture_output=True, text=True, timeout=delai)
    except subprocess.TimeoutExpired:
        _run([DOCKER, "rm", "-f", nom], capture_output=True)
        print(f"ÉCHEC {cmd} : pas de réponse en {delai:.0f} s", file=sys.stderr)
        return 1
    sortie = (r.stdout or "").strip().splitlines()
    if r.returncode:
        detail = (r.stderr or r.stdout or "").strip()[-400:]
        print(f"ÉCHEC {cmd} (code {r.returncode}) : {detail}", file=sys.stderr)
        return 1
    print(f"OK {cmd} : {sortie[0] if sortie else '(sans sortie)'}")
    return 0


# Exécutables du PATH de l'image (PLU-71) : chaque fichier exécutable par
# l'utilisateur de l'image dans chaque dossier de son PATH, sous le chemin réel
# du dossier (/bin et /usr/bin, liés, ne comptent qu'une fois après tri).
PATH_EXECUTABLES_SCRIPT = (
    'IFS=:; for d in $PATH; do [ -d "$d" ] || continue; r=$(cd "$d" && pwd -P) || continue; '
    'for f in "$d"/* "$d"/.[!.]*; do if [ -f "$f" ] && [ -x "$f" ]; then '
    'printf \'%s/%s\\n\' "$r" "${f##*/}"; fi; done; done'
)


def _liste_attendue(chemin: str) -> list[str]:
    """Lignes de la liste versionnée, sans commentaires ni lignes vides."""
    with open(chemin, encoding="utf-8") as fh:
        return [ligne.strip() for ligne in fh if ligne.strip() and not ligne.lstrip().startswith("#")]


def _comparer_path(tag: str, chemin: str, delai: float) -> int:
    """Compare les exécutables du PATH de l'image à `chemin` ; 0 si identiques.

    Un exécutable nouveau sur le PATH (le bwrap d'INC-4) ou disparu fait
    échouer l'étape tant que la liste n'est pas mise à jour dans le même
    changement. Liste encore vide : la liste réelle est imprimée, l'étape échoue.
    """
    nom = f"ci-verif-{os.getpid()}-{_secrets.token_hex(3)}"
    argv = [DOCKER, "run", "--rm", "--name", nom, "--label", f"ci.locale.image={tag}",
            "--network", "none", tag, "sh", "-c", PATH_EXECUTABLES_SCRIPT]
    try:
        r = _run(argv, capture_output=True, text=True, timeout=delai)
    except subprocess.TimeoutExpired:
        _run([DOCKER, "rm", "-f", nom], capture_output=True)
        print(f"ÉCHEC exécutables du PATH : pas de réponse en {delai:.0f} s", file=sys.stderr)
        return 1
    if r.returncode:
        detail = (r.stderr or r.stdout or "").strip()[-400:]
        print(f"ÉCHEC exécutables du PATH (code {r.returncode}) : {detail}", file=sys.stderr)
        return 1
    reels = sorted(set((r.stdout or "").split("\n")) - {""})
    attendus = _liste_attendue(chemin)
    if not attendus:
        print(f"Exécutables du PATH de l'image ({len(reels)}) :")
        for ligne in reels:
            print(ligne)
        print(f"ÉCHEC {chemin} ne liste encore aucun exécutable : relire la liste "
              "ci-dessus, la copier dans ce fichier et la versionner.", file=sys.stderr)
        return 1
    nouveaux = sorted(set(reels) - set(attendus))
    partis = sorted(set(attendus) - set(reels))
    if nouveaux or partis:
        print(f"ÉCHEC exécutables du PATH différents de {chemin} : mettre la liste à jour "
              "dans le même changement (INC-4 : un bwrap sur le PATH).", file=sys.stderr)
        for ligne in nouveaux:
            print(f"+ {ligne}", file=sys.stderr)
        for ligne in partis:
            print(f"- {ligne}", file=sys.stderr)
        return 1
    print(f"OK PATH : {len(reels)} exécutables, comme {chemin}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("git-depot")
    g.add_argument("--ref", action="append", help="branche de base à recréer (répétable)")
    g.add_argument("--branche", help="nom de la branche locale (défaut ci-locale)")
    s = sub.add_parser("services")
    s.add_argument("--postgres", help="image PostgreSQL (tag ou digest)")
    s.add_argument("--pg-user", default="ci")
    s.add_argument("--pg-db", default="ci")
    s.add_argument("--pg-extra-db", action="append")
    s.add_argument("--redis", help="image Redis")
    s.add_argument("--pull-never", action="store_true")
    s.add_argument("--env", action="append", help="NOM=modèle, ex. URL=postgresql://<pg_user>:<pg_password>@127.0.0.1:<pg_port>/<pg_db>")
    s.add_argument("--env-file", help="écrit aussi les variables dans ce fichier (0600)")
    s.add_argument("commande", nargs=argparse.REMAINDER)
    k = sub.add_parser("gitleaks")
    k.add_argument("--base")
    k.add_argument("--config")
    i = sub.add_parser("image-docker")
    i.add_argument("--nom", required=True)
    i.add_argument("--dockerfile", default="Dockerfile")
    i.add_argument("--target")
    i.add_argument("--user")
    i.add_argument("--healthcheck")
    i.add_argument("--verifier", action="append",
                   help="commande lancée dans l'image sans réseau, doit sortir en 0 (répétable)")
    i.add_argument("--verifier-amd64", action="append",
                   help="comme --verifier, seulement si l'image est amd64 (répétable)")
    i.add_argument("--delai-verifier", type=float, default=120.0,
                   help="secondes accordées à chaque commande (défaut 120)")
    i.add_argument("--executables-path",
                   help="liste versionnée des exécutables du PATH de l'image (PLU-71)")
    a = p.parse_args()
    if a.cmd == "image-docker":
        if a.delai_verifier <= 0:
            i.error("--delai-verifier doit être positif")
        if a.executables_path and not os.path.isfile(a.executables_path):
            i.error(f"liste des exécutables du PATH introuvable : {a.executables_path}")
        for cmd in (a.verifier or []) + (a.verifier_amd64 or []):
            try:
                mots = shlex.split(cmd)
            except ValueError as exc:
                i.error(f"commande illisible {cmd!r} : {exc}")
            if not mots:
                i.error("commande de vérification vide")
    return {"git-depot": cmd_git_depot, "services": cmd_services, "gitleaks": cmd_gitleaks,
            "image-docker": cmd_image_docker}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Rotate ENCRYPTION_KEY: re-encrypt every stored secret under a new key.

Stop Plugboard first: a running server keeps the old key in memory.

    make rotate-encryption-key ARGS="--new-key-file ~/plugboard-new.key --generate --dry-run"
    make rotate-encryption-key ARGS="--new-key-file ~/plugboard-new.key --generate"

``--generate`` creates the key file (mode 0600) when it does not exist yet;
the key itself is never printed. The current key is the one the server uses
(ENCRYPTION_KEY from the environment or .env, else data/.encryption_key).
Provider tokens, OAuth credentials, the admin MFA secret and the personal
TOTP secrets (data/admin_people.json) are re-encrypted after a backup in
data/backups/rekey-<time>/; nothing is written if one of them cannot be read.
A completed rotation also signs out every dashboard session and forgets the
browsers remembered for local auto-login. Then:

- ENCRYPTION_KEY set: put the content of the key file in ENCRYPTION_KEY
  (.env or secret manager) and start Plugboard;
- ENCRYPTION_KEY empty: data/.encryption_key is replaced by the new key.

Exit codes: 0 done (or dry run), 1 a secret could not be read and nothing
was written, 2 usage error.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Let the script run straight from a checkout without pip install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet  # noqa: E402

from plugboard import admin_sessions, config, local_login, secrets_rekey  # noqa: E402
from plugboard.storage import atomic_write_text, data_dir  # noqa: E402


def _new_key(path: Path, generate: bool) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    if not generate:
        raise FileNotFoundError(f"{path} does not exist (add --generate to create it)")
    key = Fernet.generate_key().decode("ascii")
    atomic_write_text(path, key + "\n", mode=0o600)
    print(f"New key written to {path} (mode 0600).")
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--new-key-file",
        required=True,
        help="File holding the new key (one Fernet key).",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Create the key file with a fresh key when it does not exist.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check that every secret can be read; write nothing.",
    )
    args = parser.parse_args(argv)

    path = Path(args.new_key_file).expanduser()
    try:
        new_key = _new_key(path, args.generate)
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if not secrets_rekey.is_strong_key(new_key):
        print(
            f"Error: {path} does not hold a Fernet key; use --generate with a new path.",
            file=sys.stderr,
        )
        return 2
    old_key = config._ENCRYPTION_KEY
    if new_key == old_key:
        print("Error: the new key is the current key.", file=sys.stderr)
        return 2

    data = data_dir()
    from_env = bool(os.environ.get("ENCRYPTION_KEY", "").strip())
    report = secrets_rekey.rotate(data, old_key, new_key, dry_run=args.dry_run)
    print(
        f"Secrets: {report.rotated} to re-encrypt, {report.encrypted_plain} "
        f"stored in plain text, {report.already_new} already on the new key."
    )
    if not report.ok:
        print("Cannot be read with the current key nor the new one:", file=sys.stderr)
        for where in report.unreadable:
            print(f"  - {where}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1
    if args.dry_run:
        print("Dry run: nothing was written.")
        return 0

    print(f"Done. Previous files saved in {report.backup_dir}")
    try:
        # Browsers remembered for local auto-login would sign straight back in.
        local_login.forget_devices()
        admin_sessions.revoke_all()
    except OSError as exc:
        # The secrets are already rewritten: the rotation still succeeded.
        print(
            f"Warning: dashboard sessions could not be signed out ({exc}). "
            'Use "Log out everywhere" in the dashboard once Plugboard restarts.',
            file=sys.stderr,
        )
    else:
        print(
            "Every dashboard session was signed out: log in again once "
            "Plugboard restarts."
        )
    if from_env:
        print(
            f"Next: set ENCRYPTION_KEY to the content of {path} (in .env or your "
            "secret manager), then start Plugboard. Keep the previous key until "
            f"the server reads its providers, then delete {path}."
        )
    else:
        key_file = secrets_rekey.replace_key_file(data, new_key)
        print(
            f"{key_file} now holds the new key (the previous one is in the backup). "
            f"Start Plugboard, then delete {path}."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""CLI bootstrap for admin TOTP MFA (TM-001).

Generates a fresh TOTP secret, writes it (Fernet-encrypted) to
``data/admin_mfa.json``, and prints the matching ``otpauth://`` URI.
Paste the URI into an authenticator app (1Password / Authy / Google
Authenticator / …) or reconstruct a QR from it manually.

Usage:
    make admin-mfa-setup
    .venv/bin/python scripts/admin_mfa_setup.py --rotate   # replace an existing secret

Once enrolled, set ``PLUGBOARD_ADMIN_MFA_REQUIRED=1`` in your env and
restart the server to enforce the second factor at admin login.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Let the script run straight from a checkout without pip install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugboard import admin_mfa  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="Replace an existing secret without prompting.",
    )
    parser.add_argument(
        "--account",
        default="admin",
        help="Label shown inside the authenticator app (default: admin).",
    )
    args = parser.parse_args()

    if admin_mfa.is_enrolled() and not args.rotate:
        print(
            "An admin MFA secret is already enrolled. "
            "Re-run with --rotate to overwrite it (invalidates the old secret).",
            file=sys.stderr,
        )
        return 1

    secret = admin_mfa.generate_secret()
    uri = admin_mfa.provisioning_uri(secret, account=args.account)
    admin_mfa.enroll(secret)

    print("✓ Admin MFA secret enrolled.\n")
    print("Paste this URI into your authenticator app (or render it as a QR):")
    print()
    print(f"  {uri}")
    print()
    print("Secret (base32, for manual entry):")
    print(f"  {secret}")
    print()
    print(
        "To enforce the second factor at login, set\n"
        "  PLUGBOARD_ADMIN_MFA_REQUIRED=1\n"
        "in your env and restart the server.\n"
    )
    print(
        f"The secret is stored Fernet-encrypted at {admin_mfa._MFA_FILE} (mode 0600)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

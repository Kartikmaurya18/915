"""Operator commands. Admin rights are granted only here, offline - never via an
HTTP request or an email allow-list, either of which could be raced or abused by
whoever registers an address first.

    python -m src.auth.cli make-admin  someone@example.com
    python -m src.auth.cli remove-admin someone@example.com
"""

from __future__ import annotations

import argparse
import sys

from src.auth import service
from src.db.session import session_scope


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="src.auth.cli")
    parser.add_argument("command", choices=["make-admin", "remove-admin"])
    parser.add_argument("email")
    args = parser.parse_args(argv)
    role = "admin" if args.command == "make-admin" else "user"
    try:
        with session_scope() as session:
            user = service.set_role(session, args.email, role)
            print(f"{user.email} is now '{user.role}'.")
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

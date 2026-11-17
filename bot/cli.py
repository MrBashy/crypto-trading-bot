"""Command-line helpers.

    python -m bot.cli set-password            # prompts for a new dashboard password
    python -m bot.cli set-password --generate # creates a random one and prints it
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from . import secrets_store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("set-password", help="set the dashboard password")
    sp.add_argument("--generate", action="store_true", help="generate a random password and print it")
    args = parser.parse_args(argv)

    if args.command == "set-password":
        if args.generate:
            password = secrets.token_urlsafe(12)
            secrets_store.set_password(password)
            print(f"Dashboard password set to: {password}")
            print("Write it down - it is not stored anywhere in readable form. You can change it in the dashboard.")
            return 0
        first = getpass.getpass("New dashboard password (min 8 characters): ")
        if getpass.getpass("Repeat password: ") != first:
            print("Passwords don't match.", file=sys.stderr)
            return 1
        try:
            secrets_store.set_password(first)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print("Password set. Any browser that was logged in to the dashboard has been logged out.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

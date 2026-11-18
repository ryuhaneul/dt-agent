#!/usr/bin/env python3
"""Return GitHub credentials through Git's credential protocol, never in errors."""

from __future__ import annotations

import os
import sys

from _secrets import SecretsError, load_secrets


def main() -> int:
    try:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            load_secrets("GH_TOKEN", "GITHUB_TOKEN")
            token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    except SecretsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if token:
        print("username=x-access-token")
        print(f"password={token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

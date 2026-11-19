#!/usr/bin/env python3
"""Show the secrets store state without returning any stored names or values."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).absolute().parent.parent))
from _secrets import SecretsError, secret_status as read_store_status


def main() -> int:
    try:
        metadata = read_store_status()
        # Rebuild the public enum and numbers instead of forwarding RPC values.
        state = next(
            state for state in ("unconfigured", "locked", "unlocked")
            if state == metadata["state"]
        )
        remaining = metadata["remaining_seconds"]
        public_status = {
            "state": state,
            "ttl_seconds": int(metadata["ttl_seconds"]),
            "remaining_seconds": float(remaining) if remaining is not None else None,
        }
        print(json.dumps(public_status))
        return 0
    except SecretsError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())

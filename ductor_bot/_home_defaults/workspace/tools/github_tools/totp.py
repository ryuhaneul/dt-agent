#!/usr/bin/env python3
"""Print the current six-digit one-time code (RFC 6238, SHA-1, 30 s) for a stored seed.

Usage:
    python3 totp.py SECRET_KEY

SECRET_KEY names an entry of the environment or the secrets store that holds the base32 seed
(for example MY_TOTP_SEED). Only the code is printed, never the seed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import struct
import sys
import time
from collections.abc import Callable

USAGE = "Usage: python3 totp.py SECRET_KEY"


def totp(secret: str, when: float | None = None, step: int = 30, digits: int = 6) -> str:
    """The one-time code for a base32 *secret* at *when* (now by default), SHA-1."""
    cleaned = secret.replace(" ", "").upper()
    cleaned += "=" * ((8 - len(cleaned) % 8) % 8)
    key = base64.b32decode(cleaned)
    counter = int((time.time() if when is None else when) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    number = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % (10**digits)).zfill(digits)


def _tools_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_parser() -> Callable[..., tuple[dict[str, str | bool], list[str]]]:
    if _tools_dir() not in sys.path:
        sys.path.insert(0, _tools_dir())
    from _tool_shared import parse_tool_args

    return parse_tool_args


def _load_secrets() -> Callable[..., dict[str, str]]:
    if _tools_dir() not in sys.path:
        sys.path.insert(0, _tools_dir())
    from _secrets import load_secrets

    return load_secrets


def main(argv: list[str]) -> int:
    _, args = _load_parser()(
        argv,
        usage=USAGE,
        help_text=(__doc__ or "").strip(),
        min_positional=1,
        max_positional=1,
    )
    key = args[0]
    _load_secrets()(key)
    seed = os.environ.get(key, "")
    if not seed:
        print(f"Error: secret {key} is not set in the environment or the store", file=sys.stderr)
        return 1
    try:
        print(totp(seed))
    except (binascii.Error, ValueError):
        print(f"Error: secret {key} is not a valid base32 seed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

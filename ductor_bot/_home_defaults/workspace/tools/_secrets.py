"""Load credentials from the shared secrets store when a tool needs them.

The store is a plain ``KEY=VALUE`` file (``secrets.env``) that is never injected into every
CLI run, so a tool asks for exactly the keys it uses and then reads them from ``os.environ``.

Usage::

    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _secrets import load_secrets

    load_secrets("MY_API_KEY")  # name the keys you need
    token = os.environ["MY_API_KEY"]

Rules:

* Name the keys. A call without keys loads the whole store; it exists for older tools only.
* A value that is already set in the process environment wins over the store.
* One line is ``KEY=VALUE``. Lines that start with ``#`` and empty values are skipped, matching
  quotes around a value are removed, and ``KEY=$OTHER_KEY`` takes the value of ``OTHER_KEY``
  from the same file. An ``export`` prefix is not understood.
* The file is the one named by ``DUCTOR_SECRETS_FILE``. The framework sets it to the main home's
  ``secrets.env`` for every CLI run unless you set it yourself; without it the store is
  ``/ductor/secrets.env``.
* A missing or unreadable file gives an empty result and no error. Values are never printed.
"""

from __future__ import annotations

import os
from pathlib import Path

_PATH_VARIABLE = "DUCTOR_SECRETS_FILE"
_DEFAULT_PATH = "/ductor/secrets.env"


def _store_path() -> Path:
    return Path(os.environ.get(_PATH_VARIABLE) or _DEFAULT_PATH).expanduser()


def _parse(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, UnicodeError):
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    for key, value in list(values.items()):
        if value.startswith("$") and value[1:] in values:
            values[key] = values[value[1:]]
    return values


def load_secrets(*keys: str) -> dict[str, str]:
    """Put the named secrets into ``os.environ`` and return the ones that were loaded.

    Keys that already have a value in the environment, or that are missing or empty in the
    store, are left alone. Without keys every entry of the store is considered.
    """
    store = _parse(_store_path())
    loaded: dict[str, str] = {}
    for key in keys or tuple(store):
        if os.environ.get(key):
            continue
        value = store.get(key)
        if value:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def get_secret(key: str, default: str | None = None) -> str | None:
    """Return one secret: the environment first, then the store, then *default*."""
    value = os.environ.get(key)
    if value:
        return value
    return _parse(_store_path()).get(key, default)

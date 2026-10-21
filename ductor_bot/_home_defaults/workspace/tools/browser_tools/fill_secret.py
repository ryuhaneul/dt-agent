#!/usr/bin/env python3
"""Fill a form field in the browser container with a stored secret.

Usage:
    python3 fill_secret.py --origin ORIGIN CSS_SELECTOR SECRET_KEY

SECRET_KEY names an entry in the secrets store only (for example MY_PASSWORD).
The value travels only in the request body to the browser container; it is not part of any
command line, and neither the output nor an error message contains it. On success the tool
prints the length of the field value that was set, as JSON.

ORIGIN must be an HTTPS origin without a path and exactly match location.origin
(for example https://example.com). Only secure contexts can be filled.
Pages with disabled certificate verification are refused.

Put "--" before a selector that starts with "-".
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

USAGE = "Usage: python3 fill_secret.py --origin ORIGIN CSS_SELECTOR SECRET_KEY"

_SCRIPT = (
    "(() => {"
    " if (!(window.isSecureContext === true) || location.origin !== %s) return -2;"
    " const el = document.querySelector(%s);"
    " if (!el) return -1;"
    " el.value = %s;"
    " el.dispatchEvent(new Event('input', {bubbles: true}));"
    " el.dispatchEvent(new Event('change', {bubbles: true}));"
    " return el.value.length;"
    "})()"
)


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
    from _secrets import _parse, _store_path

    return lambda *_keys: _parse(_store_path())


def _post(expression: str) -> str:
    """Send the expression through the browser tool, which adds the browser token."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import browser

    return browser._post("/evaluate", {"expression": expression, "insecure": False})


def mask(text: str, value: str) -> str:
    """Remove the value, also in the forms a JSON or JavaScript message would carry it."""
    forms = {
        value,
        json.dumps(value),
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
    }
    for form in sorted(forms, key=len, reverse=True):
        if form:
            text = text.replace(form, "***")
    return text


def _outcome(response: str, value: str) -> tuple[bool, dict[str, Any]]:
    try:
        payload = json.loads(response)
    except ValueError:
        return False, {"error": mask(response, value)[:300]}
    if not isinstance(payload, dict):
        return False, {"error": "unexpected response from the browser container"}
    if "error" in payload:
        message = mask(str(payload["error"]), value)[:300]
        extra = {
            k: mask(str(v), value)[:300] for k, v in payload.items() if k in ("hint", "status")
        }
        return False, {"error": message, **extra}
    length = payload.get("result")
    if not isinstance(length, int) or isinstance(length, bool):
        return False, {"error": "unexpected response from the browser container"}
    if length == -2:
        return False, {
            "error": "the page origin changed or the context is not secure; nothing was filled"
        }
    if length < 0:
        return False, {"error": "no element matches the selector"}
    return True, {"result": length}


def _origin_error(response: str, origin: str, value: str) -> str | None:
    try:
        payload = json.loads(response)
    except ValueError:
        return mask(response, value)[:300]
    if not isinstance(payload, dict):
        return "unexpected response from the browser container"
    if payload.get("insecure"):
        return "refusing to fill a page with disabled certificate verification"
    if "error" in payload:
        return mask(str(payload["error"]), value)[:300]
    if payload.get("result") != origin:
        return mask(f"browser origin {payload.get('result')!r} does not match {origin!r}", value)
    return None


def _valid_origin(origin: str) -> bool:
    try:
        parts = urlsplit(origin)
        _ = parts.port
    except ValueError:
        return False
    return bool(
        origin.startswith("https://")
        and parts.hostname
        and parts.username is None
        and parts.password is None
        and not parts.path
        and not parts.query
        and not parts.fragment
        and not any(char.isspace() for char in origin)
    )


def main(argv: list[str]) -> int:
    options, args = _load_parser()(
        argv,
        usage=USAGE,
        help_text=(__doc__ or "").strip(),
        valued=("--origin",),
        min_positional=2,
        max_positional=2,
    )
    origin = str(options.get("--origin", ""))
    if not _valid_origin(origin):
        print("Error: --origin must be an HTTPS origin without a path", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 1
    selector, key = args
    value = _load_secrets()(key).get(key, "")
    if not value:
        print(json.dumps({"error": f"secret {key} is not set in the secrets store"}))
        return 1
    error = _origin_error(_post("location.origin"), origin, value)
    if error is not None:
        print(json.dumps({"error": error}))
        return 1
    expression = _SCRIPT % (json.dumps(origin), json.dumps(selector), json.dumps(value))
    ok, report = _outcome(_post(expression), value)
    print(json.dumps(report))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

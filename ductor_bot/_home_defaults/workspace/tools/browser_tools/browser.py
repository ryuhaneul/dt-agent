#!/usr/bin/env python3
"""Browser control tool — wrapper for the ductor-browser container API.

Usage:
    python3 tools/browser_tools/browser.py navigate [--insecure] <url>
    python3 tools/browser_tools/browser.py snapshot
    python3 tools/browser_tools/browser.py act <action> <ref> [value]
    python3 tools/browser_tools/browser.py screenshot [--full]
    python3 tools/browser_tools/browser.py evaluate <expression>
    python3 tools/browser_tools/browser.py health

Quote an argument that contains spaces (an expression, a value). Put ``--`` before an
argument that starts with ``-``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable

BROWSER_URL = os.getenv("BROWSER_URL", "http://ductor-browser:18800")
BROWSER_TOKEN = os.getenv("BROWSER_TOKEN", "")


def _http_error(error: urllib.error.HTTPError) -> str:
    body = error.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {"error": body}
    if not isinstance(payload, dict):
        payload = {"body": payload}
    payload["status"] = error.code
    return json.dumps(payload)


def _post(path: str, data: dict) -> str:
    headers = {"Content-Type": "application/json"}
    if BROWSER_TOKEN:
        headers["X-Browser-Token"] = BROWSER_TOKEN
    req = urllib.request.Request(
        f"{BROWSER_URL}{path}",
        data=json.dumps(data).encode(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "image/" in content_type:
                fd, out_path = tempfile.mkstemp(
                    suffix=".png", prefix="browser_",
                )
                with os.fdopen(fd, "wb") as f:
                    f.write(resp.read())
                return json.dumps({"path": out_path})
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        return _http_error(e)
    except urllib.error.URLError as e:
        return json.dumps({
            "error": f"Browser container unreachable: {e.reason}",
            "hint": "Is the ductor-browser container running? "
                    "Check: docker compose ps browser",
        })
    except TimeoutError:
        return json.dumps({
            "error": "Browser request timed out (30s)",
            "hint": "The page may be too slow to load. Try again.",
        })


def _get(path: str) -> str:
    req = urllib.request.Request(f"{BROWSER_URL}{path}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        return _http_error(e)
    except urllib.error.URLError as e:
        return json.dumps({
            "error": f"Browser container unreachable: {e.reason}",
            "hint": "Is the ductor-browser container running? "
                    "Check: docker compose ps browser",
        })


# command -> (flags, minimum positional arguments, maximum positional arguments)
_COMMANDS: dict[str, tuple[tuple[str, ...], int, int]] = {
    "navigate": (("--insecure",), 1, 1),
    "snapshot": ((), 0, 0),
    "act": ((), 2, 3),
    "screenshot": (("--full",), 0, 0),
    "evaluate": ((), 1, 1),
    "health": ((), 0, 0),
}


def _load_parser() -> Callable[..., tuple[dict[str, str | bool], list[str]]]:
    tools_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if tools_root not in sys.path:
        sys.path.insert(0, tools_root)
    from _tool_shared import parse_tool_args

    return parse_tool_args


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd in ("-h", "--help"):
        print((__doc__ or "").strip())
        sys.exit(0)
    if cmd.startswith("-"):
        print(f"Error: unknown option '{cmd}'", file=sys.stderr)
        print((__doc__ or "").strip(), file=sys.stderr)
        sys.exit(1)

    if cmd in _COMMANDS:
        flags, minimum, maximum = _COMMANDS[cmd]
        options, args = _load_parser()(
            sys.argv[2:],
            usage=f"Usage: browser.py {cmd} [options] <arguments> (see browser.py --help)",
            help_text=(__doc__ or "").strip(),
            flags=flags,
            min_positional=minimum,
            max_positional=maximum,
        )

    if cmd == "navigate":
        print(_post("/navigate", {"url": args[0], "insecure": bool(options.get("--insecure"))}))

    elif cmd == "snapshot":
        print(_post("/snapshot", {}))

    elif cmd == "act":
        data = {"action": args[0], "ref": int(args[1])}
        if len(args) > 2:
            data["value"] = args[2]
        print(_post("/act", data))

    elif cmd == "screenshot":
        print(_post("/screenshot", {"fullPage": bool(options.get("--full"))}))

    elif cmd == "evaluate":
        print(_post("/evaluate", {"expression": args[0]}))

    elif cmd == "health":
        print(_get("/health"))

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()

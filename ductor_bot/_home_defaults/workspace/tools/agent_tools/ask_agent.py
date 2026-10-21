#!/usr/bin/env python3
"""Send a synchronous message to another agent via the InterAgentBus.

Blocks until the sub-agent responds. The response is returned as stdout
to the calling CLI process (your tool output).

The response ALWAYS comes back to YOU (the calling agent). There is no way
to make the sub-agent reply in its own Telegram chat via this tool.

Uses the internal localhost HTTP API to communicate with the bus.
Environment variables DUCTOR_AGENT_NAME, DUCTOR_INTERAGENT_PORT, and
DUCTOR_INTERAGENT_HOST are automatically set by the DT Agent framework.

Usage:
    python3 ask_agent.py [--new] [--lane LANE] TARGET_AGENT "Your message here"
    python3 ask_agent.py [options] TARGET_AGENT -- "-a message that starts with a dash"

Options:
    --new        Start a fresh session, discarding any prior inter-agent context
                 with the recipient.
    --lane LANE  Use a separate parallel session with the recipient (1-12 characters
                 of a-z, 0-9 and "-", not starting with "-"; upper case is lowered).
                 Different lanes run in parallel sessions; without a lane the
                 usual single session is used.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
import os
import sys
import urllib.request
import urllib.error
from collections.abc import Callable


def _origin_context() -> tuple[str, str, str]:
    """Return the (chat_id, topic_id, transport) this reply should be routed to.

    While handling an inter-agent request the turn is anchored to this agent's
    own chat, but the requesting thread travels alongside in ``DUCTOR_ORIGIN_*``.
    Prefer it so the reply lands where the request came from.
    """
    if os.environ.get("DUCTOR_ORIGIN_CHAT_ID", "") not in {"", "0"}:
        return (
            os.environ.get("DUCTOR_ORIGIN_CHAT_ID", ""),
            os.environ.get("DUCTOR_ORIGIN_TOPIC_ID", ""),
            os.environ.get("DUCTOR_ORIGIN_TRANSPORT", ""),
        )
    return (
        os.environ.get("DUCTOR_CHAT_ID", ""),
        os.environ.get("DUCTOR_TOPIC_ID", ""),
        os.environ.get("DUCTOR_TRANSPORT", ""),
    )


def _receipt_line(result: dict[str, object] | None = None) -> str:
    """Keep metadata separate from response/error bodies, with legacy UTC fallback."""
    context = (result or {}).get("received_context")
    if isinstance(context, str) and context.startswith("[Received:") and "\n" not in context:
        return context
    stamp = (result or {}).get("received_at")
    try:
        moment = datetime.fromtimestamp(float(stamp), timezone.utc) if stamp is not None else datetime.now(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        moment = datetime.now(timezone.utc)
    return f"[Received: {moment.strftime('%Y-%m-%d %H:%M:%S %Z')}]"


def _load_parser() -> Callable[..., tuple[dict[str, str | bool], list[str]]]:
    tools_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if tools_root not in sys.path:
        sys.path.insert(0, tools_root)
    from _tool_shared import parse_tool_args

    return parse_tool_args


def main() -> None:
    options, args = _load_parser()(
        sys.argv[1:],
        usage='Usage: python3 ask_agent.py [--new] [--lane LANE] TARGET_AGENT "message"',
        help_text=(__doc__ or "").strip(),
        flags=("--new",),
        valued=("--lane",),
        min_positional=2,
        max_positional=2,
    )
    new_session = bool(options.get("--new"))
    lane = str(options.get("--lane", ""))

    target = args[0]
    message = args[1]
    port = os.environ.get("DUCTOR_INTERAGENT_PORT", "8799")
    host = os.environ.get("DUCTOR_INTERAGENT_HOST", "127.0.0.1")
    sender = os.environ.get("DUCTOR_AGENT_NAME", "unknown")

    url = f"http://{host}:{port}/interagent/send"
    body: dict[str, object] = {"from": sender, "to": target, "message": message}
    if new_session:
        body["new_session"] = True
    if lane:
        body["lane"] = lane
    if os.environ.get("DUCTOR_IA_TURN") == "1":
        body["ia_turn"] = True
    if delegation_id := os.environ.get("DUCTOR_DELEGATION_ID"):
        body["delegation_id"] = delegation_id
    chat_id, topic_id, transport = _origin_context()
    if chat_id and chat_id != "0":
        body["chat_id"] = int(chat_id)
    if chat_id and chat_id != "0" and topic_id:
        body["topic_id"] = int(topic_id)
    if transport:
        body["transport"] = transport
    if session_kind := os.environ.get("DUCTOR_SESSION_KIND"):
        body["source_session_kind"] = session_kind
    if session_name := os.environ.get("DUCTOR_SESSION_NAME"):
        body["source_session_name"] = session_name
    if session_id := os.environ.get("DUCTOR_SESSION_ID"):
        body["source_session_id"] = session_id
    payload = json.dumps(body).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        print(_receipt_line(), file=sys.stderr)
        print(f"Error: Cannot reach inter-agent API at {url}: {e}", file=sys.stderr)
        print(
            "Make sure the DT Agent supervisor is running with multi-agent support.", file=sys.stderr
        )
        sys.exit(1)
    except Exception as e:
        print(_receipt_line(), file=sys.stderr)
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if result.get("success"):
        print(_receipt_line(result))
        if result.get("queued"):
            print(
                f"[Queued: waited behind {result.get('ahead', 0)} earlier request(s) "
                "for this session]",
                file=sys.stderr,
            )
        print(result.get("text", ""))
    else:
        print(_receipt_line(result), file=sys.stderr)
        error = result.get("error", "Unknown error")
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

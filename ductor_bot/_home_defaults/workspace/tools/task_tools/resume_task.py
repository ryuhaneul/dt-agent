#!/usr/bin/env python3
"""Resume a completed background task with a follow-up prompt.

The follow-up runs in the SAME CLI session as the original task, so the
task agent already has full context from its previous work.  The task
resumes on the original provider/model regardless of the current chat
provider.

Usage:
    python3 resume_task.py TASK_ID "your follow-up prompt"
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable


def _load_shared() -> tuple[object, object, object]:
    tools_dir = os.path.dirname(__file__)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from _shared import detect_agent_name, get_api_url, post_json

    return get_api_url, post_json, detect_agent_name


def _load_parser() -> Callable[..., tuple[dict[str, str | bool], list[str]]]:
    tools_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if tools_root not in sys.path:
        sys.path.insert(0, tools_root)
    from _tool_shared import parse_tool_args

    return parse_tool_args


def main() -> None:
    get_api_url, post_json, detect_agent_name = _load_shared()
    _, args = _load_parser()(
        sys.argv[1:],
        usage='Usage: python3 resume_task.py TASK_ID "follow-up prompt"',
        help_text=(__doc__ or "").strip(),
        min_positional=2,
        max_positional=2,
    )

    task_id = args[0]
    prompt = args[1]
    sender = detect_agent_name()

    body: dict[str, object] = {"task_id": task_id, "prompt": prompt, "from": sender}
    # Route the resumed run's result/questions to the chat/topic that asked for it,
    # even if the task was created from another topic (same convention as create_task.py).
    chat_id = os.environ.get("DUCTOR_CHAT_ID", "")
    topic_id = os.environ.get("DUCTOR_TOPIC_ID", "")
    if chat_id:
        body["chat_id"] = int(chat_id)
        if topic_id:
            body["topic_id"] = int(topic_id)

    url = get_api_url("/tasks/resume")
    if transport := os.environ.get("DUCTOR_TRANSPORT"):
        body["transport"] = transport
    if session_kind := os.environ.get("DUCTOR_SESSION_KIND"):
        body["source_session_kind"] = session_kind
    if session_id := os.environ.get("DUCTOR_SESSION_ID"):
        body["source_session_id"] = session_id

    result = post_json(url, body, timeout=10)

    if result.get("success"):
        print(
            f"Task '{task_id}' resumed. The result will be delivered back to this chat/topic when ready."
        )
    else:
        error = result.get("error", "Unknown error")
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

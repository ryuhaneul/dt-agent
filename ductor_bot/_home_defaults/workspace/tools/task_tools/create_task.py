#!/usr/bin/env python3
"""Create a background task that runs autonomously.

The task runs in the background and delivers its result back to your chat
when complete. You formulate the prompt with all necessary context — the
task agent does NOT have access to the conversation history.

Usage:
    python3 create_task.py [options] "Your task description here"
    python3 create_task.py [options] -- "-a prompt that starts with a dash"

Options:
    --name NAME        Human-readable task name (e.g. "Flight Search Paris")
    --provider PROV    Override provider (claude, codex, gemini, grok)
    --model MODEL      Override model (opus, sonnet, flash, etc.)
    --thinking LEVEL   Reasoning effort for codex (low, medium, high)
    --priority LEVEL   Scheduling priority (interactive|background|batch)
                       interactive bypasses the concurrency cap so user
                       follow-ups stay responsive; background (default)
                       and batch respect it.

Environment variables DUCTOR_AGENT_NAME and DUCTOR_INTERAGENT_PORT are
automatically set by the DT Agent framework.
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


_VALID_PRIORITIES = ("interactive", "background", "batch")


def main() -> None:
    get_api_url, post_json, detect_agent_name = _load_shared()
    options, args = _load_parser()(
        sys.argv[1:],
        usage=(
            "Usage: python3 create_task.py [--name NAME] [--provider P] "
            '[--model M] [--thinking L] [--priority LEVEL] "prompt"'
        ),
        help_text=(__doc__ or "").strip(),
        valued=("--name", "--provider", "--model", "--thinking", "--priority"),
        min_positional=1,
        max_positional=1,
    )
    name = str(options.get("--name", ""))
    provider = str(options.get("--provider", ""))
    model = str(options.get("--model", ""))
    thinking = str(options.get("--thinking", ""))
    priority = str(options.get("--priority", ""))
    if priority and priority not in _VALID_PRIORITIES:
        print(
            f"Error: unknown priority '{priority}'. Choose one of: {', '.join(_VALID_PRIORITIES)}",
            file=sys.stderr,
        )
        sys.exit(1)

    prompt = args[0]
    sender = detect_agent_name()

    url = get_api_url("/tasks/create")
    body: dict[str, object] = {"from": sender, "prompt": prompt}
    if name:
        body["name"] = name
    if provider:
        body["provider"] = provider
    if model:
        body["model"] = model
    if thinking:
        body["thinking"] = thinking
    if priority:
        body["priority"] = priority

    # Propagate sender context so task results route back to the originating chat/topic
    chat_id = os.environ.get("DUCTOR_CHAT_ID", "")
    topic_id = os.environ.get("DUCTOR_TOPIC_ID", "")
    if chat_id:
        body["chat_id"] = int(chat_id)
    if topic_id:
        body["topic_id"] = int(topic_id)

    if transport := os.environ.get("DUCTOR_TRANSPORT"):
        body["transport"] = transport
    if session_kind := os.environ.get("DUCTOR_SESSION_KIND"):
        body["source_session_kind"] = session_kind
    if session_id := os.environ.get("DUCTOR_SESSION_ID"):
        body["source_session_id"] = session_id

    result = post_json(url, body, timeout=10)

    if result.get("success"):
        task_id = result.get("task_id", "unknown")
        display = f"'{name}'" if name else task_id
        print(
            f"Background task {display} created (task_id: {task_id}). "
            f"The result will be delivered back to your chat when ready."
        )
    else:
        error = result.get("error", "Unknown error")
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

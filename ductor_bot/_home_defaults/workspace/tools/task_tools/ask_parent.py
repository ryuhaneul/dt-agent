#!/usr/bin/env python3
"""Forward a question to the parent agent from within a running task.

Run this command with a timeout of at least 300 seconds (300000 ms). It waits
up to about five minutes.
- Output starting with "Answer from parent:" contains an answer: continue with it.
- Output starting with "Question forwarded", a failure, or a timeout means:
  finish what you can, put the unresolved question in your final result, and end
  this run. The parent will resume you with the answer after the run ends.
Record each question and any answer in TASKMEMORY.md and your final result.

Usage:
    python3 ask_parent.py "Your question here"

Environment variable DUCTOR_TASK_ID is automatically set by the framework
when running inside a background task.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable


def _load_shared() -> tuple[object, object]:
    tools_dir = os.path.dirname(__file__)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from _shared import get_api_url, post_json

    return get_api_url, post_json


def _load_parser() -> Callable[..., tuple[dict[str, str | bool], list[str]]]:
    tools_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if tools_root not in sys.path:
        sys.path.insert(0, tools_root)
    from _tool_shared import parse_tool_args

    return parse_tool_args


def main() -> None:
    get_api_url, post_json = _load_shared()
    _, args = _load_parser()(
        sys.argv[1:],
        usage='Usage: python3 ask_parent.py "your question"',
        help_text=(__doc__ or "").strip(),
        min_positional=1,
        max_positional=1,
    )

    question = args[0]
    task_id = os.environ.get("DUCTOR_TASK_ID", "")

    if not task_id:
        print(
            "Error: DUCTOR_TASK_ID not set. This tool can only be used inside a background task.",
            file=sys.stderr,
        )
        sys.exit(1)

    url = get_api_url("/tasks/ask_parent")
    result = post_json(url, {"task_id": task_id, "question": question}, timeout=285)

    if result.get("success"):
        print(result.get("answer", "Question forwarded."))
    else:
        error = result.get("error", "Unknown error")
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

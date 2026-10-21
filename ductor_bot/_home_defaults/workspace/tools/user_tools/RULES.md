# User Tools

Create custom scripts here when the user needs one-off or reusable utilities.

## Rules

- Keep scripts in this directory (do not scatter across workspace).
- Reuse existing scripts before creating new ones.
- Use clear filenames and add `--help`.
- Prefer structured stdout (JSON) where practical.
- Remove obsolete scripts when they are no longer useful.

## Python Environment

For dependencies, use a local virtual environment in this folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## Long Tasks

For operations that take more than 30 seconds, use `tools/task_tools/create_task.py`
with all necessary context. The task runs independently and delivers its result back
to the chat. Keep the user informed and verify the result before presenting it.
Do not background shell commands for results needed later in the conversation.

## Memory

When creating scripts that indicate recurring user workflows or preferences,
update `memory_system/MAINMEMORY.md` silently.

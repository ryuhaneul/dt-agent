# Background Tasks Directory

This directory contains folders for active and completed background tasks.
Each subfolder holds a task's metadata (TASKMEMORY.md, rule files).

**Do not manually edit or create task folders here.**

## Managing tasks

Use the tools in `tools/task_tools/`:

- **Create**: `python3 tools/task_tools/create_task.py --name "..." "prompt"`
- **List**: `python3 tools/task_tools/list_tasks.py`
- **Cancel**: `python3 tools/task_tools/cancel_task.py TASK_ID`
- **Resume**: `python3 tools/task_tools/resume_task.py TASK_ID "follow-up"`

See the `tools/task_tools/` rules file (`CLAUDE.md`, `AGENTS.md` or `GEMINI.md`) for full documentation.

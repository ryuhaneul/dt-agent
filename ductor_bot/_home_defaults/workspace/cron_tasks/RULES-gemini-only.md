# Cron Tasks

This directory contains isolated task folders used by scheduled jobs.
For cron tool commands (add/edit/remove/list), see `tools/cron_tools/GEMINI.md`.

## Creating and Editing Cron Jobs

Follow the creation and editing procedure in `tools/cron_tools/GEMINI.md`.

## Important Context

Each cron run starts a fresh agent session in `cron_tasks/<task-folder>/`.
That sub-agent has no Telegram chat history and no main-session context.

## Task Folder Structure

```text
cron_tasks/<name>/
  GEMINI.md            # fixed task rules (do not edit)
  TASK_DESCRIPTION.md  # task instructions (edit this)
  <name>_MEMORY.md     # task-local memory
  scripts/             # task-specific helpers
```

## Editing Rules

- Edit behavior in `TASK_DESCRIPTION.md`.
- Keep jobs edited in place (`cron_edit.py`), do not recreate unless required.
- Do not edit task-folder `GEMINI.md` manually.
- Do not manually delete task folders; use `cron_remove.py`.

## Memory During Setup

While creating/editing cron or webhook-triggered tasks, update
`memory_system/MAINMEMORY.md` with user preference signals and inferred interests. Report memory saves, edits, or deletions in this reply: one line naming files and changes. Reads need no report.

## Per-Task Execution Overrides

For provider, model, reasoning effort, and CLI parameter overrides, follow `tools/cron_tools/GEMINI.md`.

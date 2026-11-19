# Cron Tasks

This directory contains isolated task folders used by scheduled jobs.
For cron tool commands (add/edit/remove/list), see `tools/cron_tools/CLAUDE.md`.

## Creating and Editing Cron Jobs

Follow the creation and editing procedure in `tools/cron_tools/CLAUDE.md`.

## Important Context

Each cron run starts a fresh agent session in `cron_tasks/<task-folder>/`.
That sub-agent has no Telegram chat history and no main-session context.

## Task Folder Structure

```text
cron_tasks/<name>/
  CLAUDE.md            # fixed task rules (do not edit)
  TASK_DESCRIPTION.md  # task instructions (edit this)
  <name>_MEMORY.md     # task-local memory
  scripts/             # task-specific helpers
```

## Editing Rules

- Edit behavior in `TASK_DESCRIPTION.md`.
- Keep jobs edited in place (`cron_edit.py`), do not recreate unless required.
- Do not edit task-folder `CLAUDE.md` manually.
- Do not manually delete task folders; use `cron_remove.py`.

## Memory During Setup

While creating/editing cron or webhook-triggered tasks, update
`memory_system/MAINMEMORY.md` silently with user preference signals and inferred interests.

## Per-Task Execution Overrides

For provider, model, reasoning effort, and CLI parameter overrides, follow `tools/cron_tools/CLAUDE.md`.

## Secrets and master keys

- Tool credentials in `secrets.env` may be encrypted with a master key. Provider startup keys in `.env` are separate and are not encrypted by this feature.
- Use `load_secrets("KEY")` from `tools/_secrets.py` for only the needed names; an existing environment value takes precedence. Never read/source `secrets.env`, its `.enc` file, or plaintext copies directly.
- Check `python3 tools/secret_tools/secret_status.py` before a task that needs secrets. It reports `unconfigured`, `locked`, or `unlocked`, TTL and remaining time without values. Restart always locks a configured store; TTL expiry locks it too; TTL 0 means unlimited until restart or manual lock.
- `SECRETS_LOCKED` means stop the secret-dependent work and report the code. Ask the owner to use `/masterkey unlock` in the main Telegram bot's private chat or unlock in the webchat modal. Do not bypass the lock, search for plaintext/backups, inspect process memory, or retrieve authentication tokens from files or other processes.
- `SECRETS_RATE_LIMITED` means wait for `retry_after`; do not loop, retry aggressively, or bypass the shared limit. `SECRET_NOT_SET` means the requested name is missing; never invent a value.
- Never ask for, accept, or process a master key in ordinary conversation, prompts, task files, or tool arguments. Master-key input belongs only in the `/masterkey` private input flow or the webchat modal. If locked, request the unlock action, not the key itself.
- Never cat, print, log or bulk-source credential files, including `.env`; inspect names without values.
- Never print, log, mirror, or persist secret values or master/derived keys in chat, memory, task descriptions, scripts, results, or temporary files. Use only needed values inside the consuming process. Key-name inventories and status tools must never fetch values.

## Secret-dependent cron jobs

After writing the task description and scripts, run as the final creation/editing step:

```bash
python3 tools/cron_tools/cron_check_secrets.py "<job-name>"
```

The check scans TASK_DESCRIPTION.md and task scripts for static secret-use evidence and returns names only. It may miss dynamic names, imported/outside helpers or external commands, and can produce false positives. An empty result is not proof that the task needs no secrets: also review the assignment yourself.
If this job needs secrets, the completion report MUST warn the owner that a configured store will lock on restart, manual lock or TTL expiry and the job's secret-dependent work will fail with SECRETS_LOCKED until the owner unlocks via `/masterkey unlock` or the webchat modal. Do not promise unattended success while locked. Do not put values or master keys in TASK_DESCRIPTION.md or scripts.

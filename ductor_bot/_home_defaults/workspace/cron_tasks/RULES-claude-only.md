# Cron Tasks

This directory contains isolated task folders used by scheduled jobs.
For cron tool commands (add/edit/remove/list), see `tools/cron_tools/CLAUDE.md`.

## ⚠️ MANDATORY WORKFLOW: Creating Cron Jobs

**CRITICAL: When creating a new cron job, you MUST ALWAYS ask the user these questions:**

1. **Which model?** (`--model <name>`)
   - Options: `haiku` (fast), `sonnet` (balanced), `opus` (most capable)
   - Default if user doesn't specify: Use global config model

**YOU MUST present these options to the user and wait for their answers BEFORE calling cron_add.py!**

**Advanced: CLI Parameters**
If the user explicitly requests additional CLI flags, use `--cli-parameters '<json-array>'`.
DO NOT suggest this proactively - only use if the user asks for it.

**Example conversation flow:**

User: "Create a cron job to check weather every 3 minutes"

You: "I'll create a cron job to check weather every 3 minutes. Let me configure the execution:

**Model**: Which Claude model should execute this task?
   - `haiku` (fast and cost-effective)
   - `sonnet` (balanced performance)
   - `opus` (most capable, highest quality)

Please specify your choice, or I'll use the global config default."

[Wait for user response, then call cron_add.py with appropriate flags]

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

Each cron task can override global config settings in `cron_jobs.json`:

- `model`: Model name (optional, defaults to global config). See `../config/CLAUDE.md` for full model list.
- `cli_parameters`: List of additional CLI flags (optional, advanced users only)

**Fallback behavior:**
- If a field is `null` or missing, the global config value is used
- This allows per-task customization while maintaining global defaults

**Example:**
```json
{
  "id": "daily-summary",
  "schedule": "0 8 * * *",
  "task_folder": "summary",
  "agent_instruction": "Summarize daily reports",
  "model": "opus"
}
```

**Use cases:**
- High-capability tasks: `"model": "opus"`
- Cost-effective tasks: `"model": "haiku"`
- Advanced CLI flags: `"cli_parameters": [...]` (only if user explicitly requests)

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

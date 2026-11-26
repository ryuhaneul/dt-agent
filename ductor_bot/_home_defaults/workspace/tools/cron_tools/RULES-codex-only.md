# Cron Tools (Codex Only)

Scripts for creating, editing, listing, and removing scheduled jobs.

## ⚠️ MANDATORY: Ask Before Creating Jobs

When the user requests a new cron job, do not create it straight away. Go through the seven settings in
"Explain Each Setting Before Creating or Editing a Job" below: ask for the ones the user has not given,
say which value you intend for the rest, and wait for the user's confirmation.

**Choosing the model and reasoning effort (setting 4):**

- Do not offer a fixed model list from memory; the available models change.
- **Codex**: the available model IDs are cached in `config/codex_models.json` under the main `DUCTOR_HOME` (`~/.ductor/config/codex_models.json` by default); the `/model` chat command shows the same list. Read the file instead of guessing an ID.
- **Reasoning effort**: `--reasoning-effort low|medium|high|xhigh` (the tool's allowed values) applies to this job's CLI run; `medium` is not passed to the Codex CLI (its own default applies). If omitted, the global config or the model default is used.

Do NOT suggest `--cli-parameters` proactively. Only mention it exists if the user asks.

## Mandatory Rules

1. Use these scripts for cron lifecycle actions.
2. Do not manually edit `cron_jobs.json` for normal operations.
3. Do not manually delete `cron_tasks/` folders.
4. Run `cron_list.py` before `cron_remove.py` and use exact job IDs.

## Timezone (Critical)

Before creating time-based jobs:

1. Run `python3 tools/cron_tools/cron_time.py`.
2. If `user_timezone` is empty, ask the user. Set the main default in the main home's
   `config/config.json` or set the sub-agent override in the main home's `agents.json`.
3. Tell the user to run `/restart` after timezone edits.

Runtime timezone resolution is:
job override (`--timezone`) -> `user_timezone` -> host timezone -> UTC.
Set `user_timezone` explicitly for predictable user-facing schedules.

## Core Commands

### Create Job (WITH MODEL AND REASONING SELECTION)

```bash
# Ask the user which model and reasoning effort, then:
python3 tools/cron_tools/cron_add.py \
  --name "job-name" \
  --title "Job Title" \
  --description "What this job does" \
  --schedule "0 9 * * *" \
  --model <model-id> \
  --reasoning-effort high
```

**Available parameters:**
- `--model` - Model choice (optional, uses global config if omitted)
- `--reasoning-effort` - Reasoning effort: `low`, `medium`, `high`, `xhigh` (optional, uses global config or model default if omitted)
- `--cli-parameters` - Advanced: JSON array of CLI flags (only if user explicitly requests)

### List Jobs

```bash
python3 tools/cron_tools/cron_list.py
```

### Edit Job

```bash
python3 tools/cron_tools/cron_edit.py "exact-job-id" --schedule "30 8 * * *"
python3 tools/cron_tools/cron_edit.py "exact-job-id" --timezone "Europe/Berlin"
python3 tools/cron_tools/cron_edit.py "exact-job-id" --model <model-id>
python3 tools/cron_tools/cron_edit.py "exact-job-id" --reasoning-effort xhigh
python3 tools/cron_tools/cron_edit.py "exact-job-id" --enable
python3 tools/cron_tools/cron_edit.py "exact-job-id" --disable
```

### Remove Job

```bash
python3 tools/cron_tools/cron_remove.py "exact-job-id"
```

Use `cron_edit.py` for in-place updates (title/description/schedule/timezone/model/reasoning_effort/enabled).

## Explain Each Setting Before Creating or Editing a Job

Before you run `cron_add.py` or `cron_edit.py`, tell the user, briefly and one line per item, what
each setting means and which value you intend, and get their confirmation. Do not just list flags.

1. **Schedule and timezone**: when it runs, in which timezone the hours are read.
2. **Target chat/topic**: where the result goes. A job is bound to the chat/topic it is created in.
3. **delivery_mode** (the three differ in what the topic's session sees and what it costs):
   - `separate`: result as a plain message; the session does not see it (no extra model turn).
   - `inject` (the default for a job created in a chat/topic): result injected into the session,
     which replies; one extra model turn of that session per run; the user can ask follow-ups.
   - `in_session`: no separate run; the session does the job itself with its own model; one turn
     of that session per run.
4. **provider / model / reasoning effort**: which model runs the job (in `in_session` the topic
   session's own model is used instead).
5. **Quiet hours**: hours in which the job does not run.
6. **silent_on_success**: no result message on success (not applied in `inject` / `in_session`).
7. **Dependency**: jobs sharing a dependency run one at a time.

Both tools print a `settings_summary` (setting, value, one-line meaning) after the change; relay it
to the user. A job without a chat (broadcast) is `separate`; jobs created before `delivery_mode`
existed also stay `separate` until changed with `cron_edit.py --delivery-mode`.

## Delivery Mode (where the result goes)

`--delivery-mode` on `cron_add.py` / `cron_edit.py` (default `inject` for a job created in a chat/topic,
`separate` without a chat; jobs that already existed stay `separate`):

- `separate`: a separate run; the result arrives as a message in the job's chat/topic.
- `inject`: still a separate run, but its result is injected into that chat/topic's **active session**
  and the session's reply is what gets delivered. The user can then ask follow-ups about it.
  `--silent-on-success` does not apply (the session is told).
- `in_session`: no separate run. The job's instruction (plus `TASK_DESCRIPTION.md` and the task
  folder path) runs as a turn of that session, and its reply is delivered. The session's own
  provider/model are used (the job's `--provider/--model/--reasoning-effort` are ignored).
  `--silent-on-success` does not apply.

Both modes need a chat: create the job from the chat/topic whose session should get it (a broadcast
job without a chat is refused). They take turns with the user's messages in that session: a job
waits for a running user turn, and a message sent during the job's turn waits for it. With no
active session a new one is created and kept for that chat/topic.

If the injected turn does not complete (timeout, error, empty reply): `inject` sends the job's own
result as a plain message instead; `in_session` records `error:injection`, sends only a short
note, and does not re-run the job or resend a reply. The status reflects whether the injected
turn's reply was delivered, not whether the work was good.

## Task Content

Each job owns `cron_tasks/<name>/TASK_DESCRIPTION.md`.
Edit that file to change task behavior.
Do not edit task-folder `AGENTS.md` manually.

To run the agent only when there is work to do, add `scripts/preflight.py` in the
job's task folder. When there is nothing to do, print `HEARTBEAT_OK` as the last
line of stdout and exit with code 0. The script's time limit is set by
`cron_preflight.timeout_seconds` (15 seconds by default).

## After Cron Setup

Update `memory_system/MAINMEMORY.md` with inferred preference signals
from the user's requested automation (not just "created job"). Report memory saves, edits, or deletions in this reply: one line naming files and changes. Reads need no report.

## Pitfalls

- IDs are sanitized (lowercase + hyphens).
- Prefer exact IDs from `cron_list.py` output.
- Run any tool without args for its built-in tutorial.

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

## Automatic compaction

`cron_add.py --autocompact VALUE` and `cron_edit.py JOB --autocompact VALUE` accept `inherit`, `default`, `off`, or an integer from 100000 to 1000000 tokens. Shorthand such as `500k`, `0.5m`, and `500,000` is accepted. Without the option, creation copies the creating topic's own setting from `DUCTOR_AUTOCOMPACT` and uses `inherit` when `DUCTOR_AUTOCOMPACT` is absent. Later topic changes do not change the saved setting. `inherit` uses the global value at run time. Editing without the option keeps the stored value. The saved value also overrides the target topic for `in_session` runs. Codex cannot apply `off`; Grok and Gemini do not apply autocompact overrides.

# Cron Tools (Claude Only)

Scripts for creating, editing, listing, and removing scheduled jobs.

## ⚠️ MANDATORY: Ask Before Creating Jobs

When the user requests a new cron job, do not create it straight away. Go through the seven settings in
"Explain Each Setting Before Creating or Editing a Job" below: ask for the ones the user has not given,
say which value you intend for the rest, and wait for the user's confirmation.

**Choosing the model and reasoning effort (setting 4):**

- Do not offer a fixed model list from memory; the available models change.
- **Claude**: use a model alias or full model name that the installed Claude Code CLI accepts. The `/model` chat command shows the models the bot offers.
- **Reasoning effort**: `--reasoning-effort low|medium|high|xhigh` (the tool's allowed values) applies to this job's CLI run. If omitted, the global config or the model default is used.

Do NOT suggest `--cli-parameters` proactively. Only mention it exists if the user asks.

## Mandatory Rules

1. Use these scripts for cron lifecycle actions.
2. Do not manually edit `cron_jobs.json` for normal operations.
3. Do not manually delete `cron_tasks/` folders.
4. Run `cron_list.py` before `cron_remove.py` and use exact job IDs.

## Timezone (Critical)

Before creating time-based jobs:

1. Run `python3 tools/cron_tools/cron_time.py`.
2. If `user_timezone` is empty, ask the user and set it in `~/.ductor/config/config.json`.
3. Tell the user to run `/restart` after timezone edits.

Runtime timezone resolution is:
job override (`--timezone`) -> `user_timezone` -> host timezone -> UTC.
Set `user_timezone` explicitly for predictable user-facing schedules.

## Core Commands

### Create Job (WITH MODEL SELECTION)

```bash
# Ask the user which model, then:
python3 tools/cron_tools/cron_add.py \
  --name "job-name" \
  --title "Job Title" \
  --description "What this job does" \
  --schedule "0 9 * * *" \
  --model sonnet
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
python3 tools/cron_tools/cron_edit.py "exact-job-id" --model opus
python3 tools/cron_tools/cron_edit.py "exact-job-id" --enable
python3 tools/cron_tools/cron_edit.py "exact-job-id" --disable
```

### Remove Job

```bash
python3 tools/cron_tools/cron_remove.py "exact-job-id"
```

Use `cron_edit.py` for in-place updates (title/description/schedule/timezone/model/enabled).

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
Do not edit task-folder `CLAUDE.md` manually.

## After Cron Setup

Update `memory_system/MAINMEMORY.md` silently with inferred preference signals
from the user's requested automation (not just "created job").

## Pitfalls

- IDs are sanitized (lowercase + hyphens).
- Prefer exact IDs from `cron_list.py` output.
- Run any tool without args for its built-in tutorial.

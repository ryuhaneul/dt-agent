# cron/

In-process cron scheduling with JSON persistence and one-shot CLI execution.

## Files

- `manager.py`: `CronJob`, `CronManager` CRUD/persistence
- `jobs_file.py`: locked read-modify-write of `cron_jobs.json`, shared by the manager and the tools (`infra/file_lock.py` holds the lock)
- `observer.py`: `CronObserver` scheduling, watcher, execution pipeline
- `execution.py`: provider command builders, result parsing, one-shot subprocess helper
- `dependency_queue.py`: shared dependency locks (cron + webhook cron_task)
- `infra/task_runner.py` (shared): folder checks + one-shot task execution for cron/webhook/background

## Cron job model

Core fields:

- `id`, `title`, `description`, `schedule`
- `task_folder`, `agent_instruction`, `enabled`
- `timezone` (optional per-job IANA override)
- `created_at`, `last_run_at`, `last_run_status`

Routing fields:

- `chat_id` (default `0`) -- target chat for result delivery
- `topic_id` (default `None`) -- optional forum topic within target chat
- `transport` (default `"tg"`) -- transport identifier (`"tg"` or `"mx"`)

Routing fields are editable in place via the `cron_edit` tool
(`--chat-id`, `--topic-id`, `--transport`, `--clear-topic-id`) to redirect an
existing job to a different chat or topic.

Execution overrides:

- `provider`
- `model`
- `reasoning_effort`
- `cli_parameters`

Scheduling guards:

- `quiet_start`, `quiet_end`
- `dependency`

Delivery:

- `silent_on_success` (default `false`) -- suppress result delivery when the run succeeds; errors are still delivered
- `delivery_mode` (a field missing in the file reads as `separate`; `cron_add.py` creates new jobs with a chat as `inject`) -- `separate` | `inject` | `in_session`; see [delivery modes](#delivery-modes-inject-and-in_session)

## Persistence

File: `~/.ductor/cron_jobs.json`

- format: `{ "jobs": [...] }` (a bare list is read too and written back as the object form)
- atomic writes via temp+replace
- every writer takes an exclusive `flock` on the fixed companion file `cron_jobs.json.lock` (never replaced, so the lock does not move with the data file), reads the data file again inside the lock, changes only its own fields and saves atomically. The lock is held for the short synchronous section only, never across CLI or network waits.
  - tools (`cron_add.py`, `cron_edit.py`, `cron_remove.py`, through `save_jobs`) write job *definitions*: the job they added or edited keeps the run-status fields (`last_run_*`, `last_delivery_*`, `failed_transports`, `last_result_text`, `delivery_retry_attempts`, `next_delivery_retry_at`) the file holds at that moment, a rename carries them to the new id, and all other jobs are left as the file has them
  - `CronManager` writes (`add_job`, `remove_job`, `set_enabled`, `set_all_enabled`, `update_run_status`, `update_delivery_retry`) change only the fields they own, so a definition saved by a tool in the meantime survives a run-status write. `update_delivery_retry` compares `expected_text` with the result the file holds now. Unknown fields in a job are kept.
  - the manager's job list is the file as of its last write or load, and job objects callers already hold are updated in place
  - waiting for the lock is limited to 5 s. A tool then exits with an error and changes nothing; a run-status write of the manager logs a warning, keeps the change in memory and applies it with the next write; a definition write raises `LockTimeoutError`. A file that cannot be parsed is not overwritten.
  - `CronManager.take_external_change()` tells whether a write merged job definitions that had not been loaded; the observer then leaves the watcher's mtime baseline as it is (instead of refreshing it), so the watcher still reloads and reschedules.
- every status write logs `Cron job <id> status=<status> recorded` at INFO
- `cron_add.py` and `cron_edit.py` check `--schedule` with `cronsim` (the scheduler's own parser, also a never-matching expression) before anything is created or renamed; an invalid expression prints an error and exits 1

## Observer lifecycle

`start()`:

1. schedule enabled jobs
2. start mtime watcher loop

Watcher:

- polls file mtime every 5s
- on change: reload + full reschedule

A scheduled job waits for its slot (sleep and wall-clock check) and then runs. If its task is cancelled while it waits (a reschedule), `Cron job <id> rescheduled while waiting for its slot` is logged at DEBUG; if it is cancelled while the job runs, `Cron job <id> cancelled during execution` is a WARNING. The two are told apart by the stage of the task itself, not by membership in the set of running jobs.

`reschedule_now()` is used by interactive cron toggles and updates mtime baseline first to avoid watcher race.

## Execution path

When a job fires:

1. acquire dependency lock when configured
2. quiet-hour gate (only when `job.quiet_*` is set; no fallback to global heartbeat quiet hours)
3. resolve/validate task folder (`workspace/cron_tasks/<task_folder>`)
4. resolve `TaskExecutionConfig` via `resolve_cli_config(...)`
5. enrich prompt with `<task_folder>_MEMORY.md` instructions
6. build provider command (`build_cmd`)
7. execute one-shot subprocess with timeout
8. parse provider output
9. invoke optional result callback when the execution path reaches callback emission
10. update run status (`last_run_status`, `last_run_at`)
11. schedule next occurrence

A run that ends in an exception (anything but a cancellation) is turned into one failed run: it is recorded as `error:exception` and a short notice with the job title and the exception type (never the message, stack or output) is delivered as a plain message to the job's chat/topic, or broadcast for a job without a chat. In `inject` and `in_session` mode no session turn is made for it. `silent_on_success` does not apply. A notice that cannot be sent is kept and resent by the delivery retry; the job is not run again. The notice goes to the chat/topic (or broadcast) and carries the title the run started with, also when the job is deleted or given another destination while it runs; the failed run is then not recorded on a job that now has a different destination, so nothing preserved is resent there. A cancellation (a user stop, a shutdown, a reschedule by the file watcher) sends nothing, and a run that already attempted its delivery gets no second notice.

## Command builders (`execution.py`)

Supported providers:

- Claude
- Codex
- Gemini
- Grok

Examples:

- Claude: `claude -p --output-format json ... --no-session-persistence` (prompt passed via stdin)
- Codex: `codex exec --json ... -- -` (prompt passed via stdin)
- Gemini: `gemini -p "" --output-format json --include-directories . ...` (prompt passed via stdin)
- Grok: `grok --output-format json ... --prompt-file <path>` (prompt always in a private 0600 temporary file, removed after the run)

Claude prints one JSON object; with `--verbose` in the job's `cli_parameters` it prints an array of events. The parser takes the `result` of the last `result` event, or without one the text of the last assistant message; any other JSON value gives an empty result. The same parser serves webhook `cron_task` runs, which use the same one-shot runner (background tasks use the interactive CLI service and do not go through it).

`reasoning_effort` (`low`, `medium`, `high`, `xhigh` in the tools) is passed to Claude (`--effort`), Codex (`model_reasoning_effort`, not for `medium`) and Grok (`--reasoning-effort`); Gemini ignores it.

No provider receives the task prompt as an argv value, so it does not show up in the process list (`ps`). Without a stdin prompt the child's stdin is `DEVNULL` (Grok); otherwise the prompt is piped and the pipe is closed after it.

`bypassPermissions` behavior:

- Codex: `--dangerously-bypass-approvals-and-sandbox`
- Gemini: `--approval-mode yolo`

## Status values

Typical values:

- `success`
- `error:folder_missing`
- `error:cli_not_found_claude`
- `error:cli_not_found_codex`
- `error:cli_not_found_gemini`
- `error:cli_not_found_grok`
- `success:preflight` (optional preflight handles the job without CLI execution)
- `error:timeout`
- `error:exit_<code>`
- `error:exception` (the run raised an exception; see the execution path above)

Quiet-hour skips are silent:

- no `last_run_status` update
- no result callback

Folder-missing nuance:

- `error:folder_missing` updates `last_run_status`
- no result callback is emitted for that path

Silent-on-success nuance:

- when `silent_on_success` is set and the run succeeds, `last_run_status` is updated but no result callback is emitted
- `silent_on_success` suppresses successful-result callbacks only; failures that reach the delivery path are not suppressed by that flag. `error:folder_missing` records status without a result callback, and delivery itself can fail

## Result routing

Cron results are delivered through `MessageBus` using `Envelope` objects built by `bus/adapters.py::from_cron_result(...)`.

- **UNICAST**: when `chat_id` is non-zero, the result is delivered to that specific chat/topic on the matching transport.
- **BROADCAST**: when `chat_id` is `0` (default), the result is broadcast to all authorized users.

Fallback behavior (Telegram):

- a raised `TelegramAPIError` triggers a fallback notice to that bot's first allowed user (`allowed_user_ids[0]`); the original route remains failed
- a send failure swallowed as a false return is recorded for retry without that exception-driven fallback

Fallback behavior (Matrix):

- if the target room cannot be resolved, the result falls back to broadcast across all allowed rooms.

Execution and delivery status are distinct. Failed delivery retains result text
and retry state in `CronJob`; `cron_delivery_retry` controls resend sweeps without
rerunning the CLI. Broadcast retries can target only failed transports, but
partial recipient failure within a broadcast can still duplicate output.
Transport acknowledgement lines are stripped by `bus/cron_sanitize.py`; a
success consisting only of those lines is not sent.

## Delivery modes (`inject` and `in_session`)

By default a cron job runs in a separate one-shot CLI session and its result arrives in the chat/topic as a message; the topic's own session never saw it, so the agent had to read it again when the user answered. Two optional modes connect the job to that session. Both need a chat (`chat_id` != 0): create the job from the chat/topic whose session should receive it. A broadcast job, or a stored value that is not one of the three, runs as `separate` (a warning is logged; the file is not changed).

**Defaults.** Reading is unchanged: a job whose file has no `delivery_mode` is `separate`, so existing jobs keep their behavior (no extra model turn per run) until one is switched with `cron_edit.py <id> --delivery-mode inject`. Creating differs: `cron_add.py` without `--delivery-mode` writes `inject` for a job created in a chat/topic (it needs that chat; the value is stored explicitly) and `separate` for a job without a chat (broadcast), with a note in the output instead of an error. Asking explicitly for `inject` or `in_session` without a chat is refused, and `cron_edit.py` refuses to remove the chat (`--chat-id 0`) of a job in one of those modes.

| `delivery_mode` | What runs | What the topic gets |
|---|---|---|
| `separate` (default) | separate run, as before | the result as a `TASK: <title>` message |
| `inject` | separate run, as before | the result is injected into the chat/topic's active session (a turn of the user's own session, not an isolated one); the **session's reply** is delivered with the usual `TASK: <title>` header, and no raw result message is sent |
| `in_session` | **no separate run**: the job's `agent_instruction`, the path and body of `cron_tasks/<name>/TASK_DESCRIPTION.md` and the task folder path are injected as a turn of that session | the session's reply (same header) |

- **inject prompt**: title, job id, status (a failed run is marked `FAILED` and the session is asked to tell the user what failed), the run time (shown in `user_timezone`) and the result. A result over 6000 characters is cut; the whole result is written to `cron_tasks/<name>/LAST_RESULT.md` and the prompt names that path. If the injected turn does not complete (an exception, a timeout, a CLI error or an empty reply), the job's own result is delivered to the same chat/topic as a plain message instead (nothing is lost; the delivery error field notes `injection incomplete`), and if that message cannot be sent either, the whole result is kept for the delivery retry.
- **in_session prompt**: the session is told that the scheduler runs the job now in this conversation (the time is shown in `user_timezone`); the instruction, the `<name>_MEMORY.md` hint, the task folder and `TASK_DESCRIPTION.md` follow.
- **Session order**: the injected turn takes the same session lock as user turns (`MessageBus` lock for the chat/topic). A job waits for a running user turn, and a message the user sends during the job's turn waits for it. With no active session a new one is created for exactly that transport/chat/topic and the id the injected turn returns is stored there, so the next user turn and the next job resume it (a new session carries MAINMEMORY). The same applies to task-result and other non-isolated injections into a chat/topic without a session; isolated injections create none.
- **Model**: the turn runs on the topic session's provider/model/effort. In `in_session` the job's `provider`/`model`/`reasoning_effort` are not applied (a warning is logged once per job and process); in `inject` they only select the separate run, as before.
- **`silent_on_success`**: not applied in either mode (the session is meant to hear about the run, and its reply is delivered).
- **`last_run_status` / delivery**: `inject` records the separate run's status as before and the delivery status of the injected reply; `in_session` records `success` when the injected turn ran and its reply was delivered, `error:injection` when it did not (`last_delivery_status: failed`, no result text kept). The status follows the delivery of the injected turn's reply, not the quality of the work: a turn that timed out, ended in an error or gave an empty reply is `error:injection`, a short note about the failure is sent instead of a result, and nothing is resent or re-run later (a reply that was produced but could not be sent is not kept either). Re-run the job manually if needed. `cron_delivery_retry` keeps its contract: a preserved result is resent as a plain message, never injected again.
- **Unchanged**: schedule, quiet hours, `enabled`, the optional preflight gate and the `dependency` queue apply before/around the run as for `separate` (for `in_session` the dependency queue is held while the turn runs). `error:folder_missing` is recorded as before.
- **Tools**: `cron_add.py --delivery-mode …`, `cron_edit.py <id> --delivery-mode …` and `cron_list.py` (shows the mode). Both `cron_add.py` and `cron_edit.py` print a `settings_summary` (schedule/timezone, target, delivery_mode, model, quiet hours, `silent_on_success`, dependency — each with its value and a one-line meaning) that the agent relays to the user; the cron tool rules tell the agent to explain these settings and get the user's confirmation before creating or editing a job.

Not covered by tests with real components: a real Telegram topic and real model replies (the tests use a fake transport and a fake session injector).

## Environment variables

The CLI subprocess receives routing context via environment variables:

- `DUCTOR_CHAT_ID` -- current chat ID (set when `chat_id` is non-zero)
- `DUCTOR_TOPIC_ID` -- current topic ID (set when `topic_id` is non-None)
- `DUCTOR_TRANSPORT` -- transport identifier (`"tg"` or `"mx"`)

`CronObserver` builds each job's routing values; `infra/task_runner.py` normalizes
them after parent, command and home-dotenv environments are merged. Stale
parent routing is removed. Broadcast jobs have no `DUCTOR_CHAT_ID` and carry
`DUCTOR_RESULT_ORIGIN=cron-broadcast` plus `DUCTOR_BROADCAST_REF=<job_id>`.

The `cron_add.py` tool script auto-reads these env vars to populate the job's routing fields, so jobs created from within a chat/topic automatically route results back to that location.

## Timezone resolution

Per-job scheduling resolution:

1. `CronJob.timezone`
2. global `user_timezone`
3. host timezone
4. UTC fallback

Cron expressions are evaluated in resolved local wall-clock time.

## Dependency queue

Shared queue key behavior:

- same dependency key -> FIFO serialization
- different/no key -> parallel execution
- shared with webhook `cron_task` runs

## Telegram interaction

`/cron` uses interactive selector (`crn:*` callbacks):

- paging
- refresh
- per-job enable/disable
- bulk all-on/all-off

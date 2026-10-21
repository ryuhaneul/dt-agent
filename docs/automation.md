# Automation Quickstart

Paths written as `~/.ductor` below denote the default host home. Inside the container, main uses `/ductor`; sub-agent state lives under `<main_home>/agents/<name>`.

DT Agent automation systems:

| System | Trigger | Execution Context | Output |
|---|---|---|---|
| Sessions (`/session`) | user command | named session (persistent) | chat notification |
| Delegated tasks (`TaskHub`) | task tool scripts (`/tasks/*` API) | shared background task runtime | chat notification + parent-session injection |
| Cron jobs | schedule | isolated task folder | chat result |
| Webhooks | HTTP POST | wake or isolated `cron_task` | chat result |
| Heartbeat | interval | active main session | chat alert (non-ACK only) |
| Cleanup | daily hour | filesystem maintenance | no chat message |

## Named sessions (`/session`)

`/session <prompt>` starts a named background session. The chat is free immediately; a message is sent when the task completes. Sessions persist and support follow-ups.

Key properties:

- auto-generates memorable compact names (e.g. `swiftfox`, `tallnewt`)
- supports provider isolation: `/session @codex <prompt>`
- follow-up in foreground: `@session-name <message>`
- follow-up in background: `/session @session-name <message>`
- session management: `/sessions` (list, end, end all)
- `/stop` cancels all sessions for the chat
- `/stop_all` on the main agent also aborts active work across other agents (sub-agent fallback is local-only)
- max 5 concurrent tasks, max 10 user-created sessions per chat
- `/status` shows active background tasks
- `/session` background timeout uses `config.timeouts.background`

Ordinary inter-agent recipient sessions and question fallback sessions are not created through `/session`. With origin context,
`interagent_session_name()` produces `ia.<sender-slug>.t<topic-or-0>.x<hash>`:
the eight-character SHA-256 suffix includes the sender, chat ID, and topic ID.
Calls without a source chat retain legacy `ia-<sender>`. Use the reported exact
name for follow-ups; do not guess it. Group topics and private-chat topics remain
separate scopes.

Synchronous replies return to the calling tool. Async results are injected into
the calling agent's original chat/topic session for that agent to process and
report. Inter-agent turns reject new async delegation. During sync delegation, the server also rejects questions to main; put questions in the final report. During async delegation, synchronous questions to main are allowed.
Headless agents can receive delegated work and return
synchronous answers, but cannot originate async calls or receive async results.
Receipt notices default off (`interagent_notify_received=false`), are controlled
by the receiving agent, and are suppressed by `--silent`. Messenger sub-agents
use their own bot for notices; headless sub-agents have no notification bot.
Interrupted persisted async work reports an interrupted result after restart;
it is not blindly resubmitted. Delivery is not exactly once: see the
[known limits](CHANGES.md#7-known-limitations).

During async delegation, synchronous sub→main questions use the original chat/topic’s current active session under the shared session lock, with a private answer back to the sub. Named sources and unconfirmed background sources use a separate Named Session immediately, preserving source metadata and avoiding unrelated foreground conversations. A 60s lock wait or unavailable resumable foreground source also uses that fallback. Synchronous delegation instructs the sub to put questions in its final report; a verified sync-delegation question to main is rejected before main executes. Continue work, leaving unknown values blank or stating assumptions. Unidentifiable provenance still uses the separate session. The header provides confirmed history locations to recover context. Context-free messages and rejection of async reverse/onward calls are unchanged. See [question routing](modules/multiagent.md#questions-during-delegation).

A headless worker has no user chat: main delegates and reports its results. An agent can create one with `python3 tools/agent_tools/create_agent.py --name NAME --transport headless` (optional `--provider`, `--model`) inside a framework CLI turn. Use a Telegram/Matrix sub when users need direct conversation. Headless result notices identify the processing session without offering continuation in a recipient chat.

Restart behavior:

- active asyncio task objects are lost on restart,
- persisted named sessions can still be resumed,
- startup recovery plans foreground notifications and eligible named-session resumes for both Telegram-primary and Matrix-primary startup; interrupted inter-agent work follows its separate persistence recovery path

Status values for named-session runs: `ok`, `error:cli_not_found`, `error:timeout`, `error:cli`, `error:internal`, `aborted`.

## Delegated tasks (`TaskHub`, `/tasks`)

Delegated tasks are separate from `/session`:

- persisted in `~/.ductor/tasks.json`
- task folders in `~/.ductor/workspace/tasks/<task_id>/`
- managed via `/tasks` command (running/waiting/finished + cancel/cleanup controls)
- created/resumed/cancelled/deleted through task tools (`tools/task_tools/*.py`) over `InternalAgentAPI /tasks/*`
- timeout source: `config.tasks.timeout_seconds`

Result flow:

- task completion/failure is posted to the chat
- result is injected into parent agent's current active session (`handle_task_result`)
- task questions (`ask_parent.py`) from a task started in a verified foreground conversation are first answered privately in that conversation; other or unanswered questions are posted and injected into the parent session (see `docs/modules/tasks.md`)
- forum-topic tasks route back to the originating topic via `thread_id` / `DUCTOR_TOPIC_ID`

## Cron jobs

Cron jobs run in `~/.ductor/workspace/cron_tasks/<task_folder>/`.

Each run is a fresh one-shot subprocess in the task folder. It does not reuse the main chat session.

Typical task folder:

```text
~/.ductor/workspace/cron_tasks/weather-report/
  CLAUDE.md
  AGENTS.md
  TASK_DESCRIPTION.md
  weather-report_MEMORY.md
  scripts/
```

Rule files are kept in sync automatically (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`) based on newest mtime per directory.

Rule-file sync behavior (all workspace directories, recursive):

- files: `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`
- source of truth per directory: newest file by mtime
- sync runs at init (`sync_rule_files`) and continuously (`watch_rule_files`, every 10s)
- result: edits are propagated to older existing sibling rule files
- missing siblings are generally not auto-created, except cron task folders where `ensure_task_rule_files(...)` backfills missing provider rule files

Runtime behavior:

1. Skip disabled jobs and task-level quiet hours.
2. When `cron_preflight.enabled`, run task-folder `scripts/preflight.py` if present.
   Exit 0 with the last stdout line equal to `skip_marker` skips the agent and
   records `success:preflight`; missing, failed, or timed-out preflight runs fail open.
3. Acquire the optional shared dependency lock, validate the task folder, and
   resolve execution overrides.
4. Build a `claude`, `codex`, `gemini`, or `grok` command and run it with `cli_timeout`.
5. Parse output and deliver to the originating `chat_id`/`topic_id`/`transport`,
   or broadcast when no chat ID was recorded. A cascading broadcast fallback
   applies only when the target transport is not registered; a registered
   transport's delivery failure does not trigger that fallback.
6. Persist execution/delivery status. With `cron_delivery_retry.enabled`, retry
   preserved failed delivery without rerunning the agent (default disabled,
   interval 300 seconds, max 12 attempts). Broadcast partial failures can
   retransmit to recipients who already received the result; see the known limits.


Per-job override fields in `cron_jobs.json`:

```json
{
  "provider": "gemini",
  "model": "gemini-2.5-pro",
  "reasoning_effort": null,
  "cli_parameters": ["--debug"],
  "quiet_start": 22,
  "quiet_end": 7,
  "dependency": "nightly-reports",
  "silent_on_success": true
}
```

Notes:

- `reasoning_effort` is validated and used for supported Claude, Codex, and Grok models; Gemini drops it.
- task `cli_parameters` are appended after the global provider-specific args, so task-level flags win when the underlying CLI accepts repeated options.
- cron status includes `error:cli_not_found_<provider>` for missing provider binaries.
- `error:folder_missing` updates `last_run_status` but does not emit a result callback.
- quiet-hour skips do not emit result callbacks and do not update `last_run_status`.
- `silent_on_success` (default `false`) suppresses the result callback when the run succeeds (status `success`) but still updates `last_run_status`; failures are always delivered.
- routing fields (`chat_id`, `topic_id`, `transport`) can be redirected on an existing job via the `cron_edit` tool (`--chat-id`, `--topic-id`, `--transport`, `--clear-topic-id`); `cron_add` seeds them from the creating chat/topic.

## Webhooks

Server route: `POST /hooks/{hook_id}`

Validation order:

1. rate limit
2. content type (`application/json`)
3. JSON object body
4. hook exists
5. hook enabled
6. auth (`bearer` or `hmac`)
7. accept and dispatch asynchronously (`202`)

Modes:

- `wake`: inject rendered prompt into active Telegram chat flow
- `cron_task`: run isolated one-shot execution in task folder

Prompt payload is wrapped with safety markers before execution.

`cron_task` mode supports the same override/quiet/dependency fields as cron jobs.

Quiet-hour behavior in `cron_task` mode:

- only `hook.quiet_start` / `hook.quiet_end` are considered
- no fallback to global heartbeat quiet hours

Typical status values:

- `success`
- `error:no_response`
- `error:no_task_folder`
- `error:folder_missing`
- `error:cli_not_found_claude`
- `error:cli_not_found_codex`
- `error:cli_not_found_gemini`
- `error:cli_not_found_grok`
- `error:timeout`
- `error:exit_<code>`
- `skipped:quiet_hours`

Current transport limitation:

- webhook `wake` depends on a configured wake handler and is currently wired by Telegram startup only
- Matrix-primary setups do not provide that handler right now, so `wake` returns `error:no_wake_handler`
- for Matrix-only deployments, use `cron_task` mode instead of `wake`

## Heartbeat

Heartbeat runs only when `heartbeat.enabled=true`. Quiet hours default to disabled (`quiet_start=0`, `quiet_end=0`) in both the model and example configuration; equal start/end hours mean no quiet window. Recent-activity cooldown and the other session/target gates still apply. Group targets inherit each unset quiet-hour field; explicit per-target windows remain effective. Cron and webhook quiet hours use only their own settings.

Observer behavior:

- interval loop (`interval_minutes`)
- quiet-hour suppression in `user_timezone`
- busy-chat skip via `ProcessRegistry.has_active`
- stale process cleanup hook before each tick

`heartbeat_flow` behavior:

- uses read-only active session lookup,
- skips if no active session or resumable session ID,
- preserves that active session's provider/model/reasoning effort (falling back to the configured model only when the stored model is empty),
- enforces cooldown via `last_active`,
- sends heartbeat prompt with `resume_session`,
- suppresses pure ACK token responses,
- updates session metrics only for non-ACK alerts.

With `suspend_on_ack=true` (default true in the model and example configuration),
ACK-only targets rest until the next accepted user conversation in that session.
Accepted task/inter-agent bus activity or a changed provider/session-ID/message-count
marker also wakes that exact key; failed/skipped turns do not suspend. `auto_targets=true` (default true in the model and example configuration) discovers allowed
recent Telegram DM and group-topic sessions within `auto_target_window_hours` (default 168).
Each chat/topic retains its own `SessionKey` scope; tracked/recovered sessions can remain
eligible outside the window. Disallowed sessions and untracked sessions outside the window
are excluded. With `auto_targets=false`, only allowed-user DM roots and enabled manual
targets are collected. Together, the defaults check each eligible session/group topic
active within the last 7 days and let it rest after an ACK until the next conversation.

Default ACK token: `HEARTBEAT_OK`.

Default prompt asks the model to review memory + cron context and either send something useful or respond exactly with `HEARTBEAT_OK`.

### Group targets

`group_targets` allows heartbeat to run in specific group chats/topics in addition to private user chats. Each target supports per-target overrides for prompt, ack token, interval, and quiet hours.

```json
{
  "heartbeat": {
    "enabled": true,
    "interval_minutes": 30,
    "quiet_start": 0,
    "quiet_end": 0,
    "group_targets": [
      {
        "chat_id": -1001234567890,
        "topic_id": 42,
        "interval_minutes": 60,
        "prompt": "Check project status and report if anything needs attention.",
        "quiet_start": 23,
        "quiet_end": 6
      },
      {
        "chat_id": -1009876543210
      }
    ]
  }
}
```

Per-target override fields (all optional, fall back to global heartbeat values when unset):

- `prompt`
- `ack_token`
- `interval_minutes`
- `quiet_start` / `quiet_end`

Lifecycle note:

- heartbeat/cleanup config values hot-reload
- heartbeat reload resets suspend state and starts/stops its observer when `enabled` changes
- cleanup has no start/stop reload hook: enabling it after a disabled startup requires restart; an existing loop reads updated values and skips cycles while disabled

## Cleanup

Cleanup runs once per day at `cleanup.check_hour` (in `user_timezone`), checked hourly.

Deletes old files (recursive) from:

- `workspace/telegram_files/` (Telegram media)
- `workspace/matrix_files/` (Matrix media)
- `workspace/output_to_user/`
- `workspace/api_files/`

Retention windows:

- `cleanup.media_files_days`
- `cleanup.output_to_user_days`
- `cleanup.api_files_days`

Cleanup also prunes empty subdirectories after deletion, so dated upload folders are removed once empty.

## Config blocks

```json
{
  "heartbeat": {
    "enabled": false,
    "interval_minutes": 30,
    "cooldown_minutes": 5,
    "suspend_on_ack": true,
    "auto_targets": true,
    "quiet_start": 0,
    "quiet_end": 0,
    "ack_token": "HEARTBEAT_OK"
  },
  "cleanup": {
    "enabled": true,
    "media_files_days": 30,
    "output_to_user_days": 30,
    "api_files_days": 30,
    "check_hour": 3
  },
  "webhooks": {
    "enabled": false,
    "host": "127.0.0.1",
    "port": 8742,
    "rate_limit_per_minute": 30
  }
}
```

Cron jobs and webhook entries are stored in `cron_jobs.json` / `webhooks.json`.

Sources: [named-session naming](../ductor_bot/session/named.py),
[inter-agent bus](../ductor_bot/multiagent/bus.py),
[cron observer](../ductor_bot/cron/observer.py),
[delivery bus](../ductor_bot/bus/bus.py),
[heartbeat observer](../ductor_bot/heartbeat/observer.py),
[Matrix recovery](../ductor_bot/messenger/matrix/startup.py).

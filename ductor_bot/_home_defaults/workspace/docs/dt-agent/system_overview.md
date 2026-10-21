# System Overview

Fastest end-to-end mental model for DT Agent (the restructured ductor fork).
The tracked source is installed directly into the bot image; see the
deployment model (`docs/fork_deploy_model.md`) and change list (`docs/CHANGES.md`).
Paths written as `~/.ductor` mean the default host home; substitute `DUCTOR_HOME`.
Inside the container, main uses `/ductor`.

## 1) Runtime shape

One Python process hosts:

- main agent stack (always)
- optional sub-agent stacks from `~/.ductor/agents.json`
- shared `AgentSupervisor`
- shared `InterAgentBus`
- shared internal HTTP bridge (`InternalAgentAPI`, default port `8799`, configurable via `interagent_port`)
- shared `TaskHub` when `tasks.enabled=true`

Each agent stack contains:

- Transport bot (`TelegramBot`, `MatrixBot`, `SlackBot`, or sub-only `HeadlessBot`, selected via
  `config.transport`; `MultiBotAdapter` enables parallel
  multi-transport execution when `config.transports` lists
  multiple entries)
- `Orchestrator` (routing + flows)
- `CLIService` (provider wrappers)

## 2) Primary message path

```text
Telegram:                                 Matrix:
  Telegram update                           Matrix sync event
  -> AuthMiddleware                         -> room/user allowlist check
  -> SequentialMiddleware                   -> MatrixBot handler
  -> bot handlers                           -> Orchestrator.handle_message(_streaming)
  -> Orchestrator.handle_message(_streaming)-> CLIService
  -> CLIService                             -> provider subprocess
  -> provider subprocess                    -> Matrix room message
  -> Telegram response (stream edits)
```

Notes:

- `/stop` and `/stop_all` are middleware/bot-level abort paths (not orchestrator command dispatch).
- `/new` resets the configured default-provider bucket for the active session key.
- `/reset` resets the currently active provider bucket for the active session key.
- Telegram groups: both `allowed_group_ids` and `allowed_user_ids` must allow the message.
- `group_mention_only` behavior differs by transport:
  - Telegram: mention/reply gating only (no auth bypass)
  - Matrix non-DM rooms: user allowlist check is bypassed; room allowlist + mention/reply are used as the gate

Foreground user conversations recheck the **resolved session provider** authentication immediately before CLI execution. Text, media and prompt-button turns, including named-session foreground follow-ups, return localized host-repository `./scripts/login.sh <provider>` instructions and do not run the CLI when unauthenticated. Authentication changes are reread on the next user turn; Gemini can use its configured API key. Telegram, Matrix, Slack and direct API use these shared orchestrator flows. Inter-agent, cron and webhook task execution paths are unchanged by this foreground check.

Actual allowlist denials in Telegram, Matrix and Slack emit INFO `Allowlist rejection chat_type=... chat_id=... user_id=... topic_id=...`, containing metadata only, never message content. The common `RejectionLog` helper limits each retained chat/user pair to one entry per 600 seconds (topics share that pair’s limit). Each logger instance retains up to 1,024 pairs and evicts the oldest when full; an evicted pair can log again. This does not change each transport’s authorization/mention policy or its handling of non-message events.

Telegram known slash-command buttons enter normal typed-command routing with the same filters; unknown slash commands remain AI prompts. Questions during verified synchronous delegation are rejected by the bus before orchestrator invocation or session creation. During asynchronous delegation, a foreground source privately resumes the original active session under the user/result lock and waits up to 60 seconds for the shared lock before fallback; Named/unconfirmed or unavailable sources use a separate source-header session. Unknown legacy provenance keeps the separate-session path. See [delegation questions](modules/multiagent.md#questions-during-delegation).

## 3) Session identity model

Session identity is transport-aware via `SessionKey(transport, chat_id, topic_id)`.

- Telegram messages with `is_topic_message` false: `transport="tg"`, `topic_id=None`, even if `message_thread_id` is present
- Telegram topic messages (group or private) with `is_topic_message` true: `transport="tg"`, `topic_id=message_thread_id` (`ductor_bot/messenger/telegram/topic.py:37`)
- Matrix rooms: `transport="mx"`, `chat_id=<deterministic room-int>`
- API channels: auth currently uses default `transport="tg"`, `topic_id=channel_id`.
  Equal numeric chat/topic IDs can share Telegram state. CLI request metadata
  still identifies API origin; see the API/Telegram limit (`docs/CHANGES.md`).

Persistence key format in `sessions.json`:

- legacy flat: `"<chat_id>"` / `"<chat_id>:<topic_id>"` (still accepted on parse)
- current: `"<transport>:<chat_id>"` or `"<transport>:<chat_id>:<topic_id>"`

This separates explicitly transport-keyed conversations and topic/provider scopes while accepting legacy keys. The API default-key exception above remains.

## 4) Background and delivery model

Observers run in-process (cron, webhook, heartbeat, cleanup, background sessions, model caches, config watcher, rule/skill sync).

All observer/task/inter-agent results now flow through `bus/`:

- wrap to `Envelope` (`bus/adapters.py`)
- route via `MessageBus`
- optional lock + optional injection into active session
- deliver through the matching registered messenger transport
- inter-agent async results are injected into the calling agent's original
  chat/topic, then processed/reported by that agent; they do not fall back to
  another transport when the recorded one is unavailable
- source-aware IA sessions include sender/chat/topic scope; context-free calls
  retain `ia-<sender>`. Async onward calls during IA turns are rejected;
  headless agents may receive work but cannot originate async calls or receive
  async results
- receipt notices default off (`interagent_notify_received=false`); when enabled,
  the receiving messenger agent uses its own bot and `--silent` suppresses them
- interrupted persisted IA work reports an interrupted result after restart;
  retries can duplicate output under the recorded double-failure limits

Streaming nuance:

- when the provider stream emits `CompactBoundaryEvent`, the orchestrator can mark the session for post-turn memory maintenance
- after a successful streaming turn, `MemoryFlusher` may run a silent flush and then an LLM-driven compaction pass against `MAINMEMORY.md`

Primary transport ingress, `MessageBus` injection, active-session delegation questions and `ApiServer` share the orchestrator `LockPool`. A standalone `ApiServer` without a supplied pool creates its own. Lock keys remain `(chat_id, topic_id)`.

## 5) Optional direct API path

When `api.enabled=true` and PyNaCl is installed:

```text
/ws
  -> plaintext auth frame (token + e2e_pk + optional chat_id/channel_id)
  -> auth_ok
  -> encrypted frames (NaCl Box)
  -> orchestrator streaming callbacks
```

HTTP endpoints:

- `GET /health`
- `GET /files?path=...` (Bearer token + root checks)
- `POST /upload` (Bearer token, multipart)

## 6) Internal localhost bridge

`InternalAgentAPI` endpoints for CLI tool scripts:

- `/interagent/send`
- `/interagent/send_async`
- `/interagent/agents`
- `/interagent/health`
- `/tasks/create`
- `/tasks/resume`
- `/tasks/ask_parent`
- `/tasks/list`
- `/tasks/cancel`
- `/tasks/delete`

Ownership checks are enforced for resume/cancel/delete when `from=<agent>` is supplied.

## 7) Key runtime files (`~/.ductor`)

- `config/config.json`
- `sessions.json`
- `named_sessions.json`
- `tasks.json`
- `chat_activity.json`
- `cron_jobs.json`
- `webhooks.json`
- `agents.json`
- `startup_state.json`
- `inflight_turns.json`
- `SHAREDMEMORY.md`
- `logs/agent.log`
- `.env` (provider keys read when a CLI starts), `.env.shared` (browser token/timezone)
- `secrets.env` (tool credentials, mode 0600; loaded on demand by `tools/_secrets.py`, not injected)
- `.cli-auth/` (CLI credentials and persistent Claude state)
- `interagent/inflight/`, `interagent/undelivered/` (durable async recovery/retry state)
- `workspace/` (rules, tools, files, tasks, cron_tasks, skills)

Sub-agent home: `<main_home>/agents/<name>/` with its own config/workspace/session files. Edit `agents.json` as the configuration source; generated sub config is runtime output.

## 8) Where to read code first

1. `ductor_bot/__main__.py` (entrypoint + config/load/run)
2. `ductor_bot/cli_commands/` (actual CLI subcommand logic)
3. `ductor_bot/multiagent/supervisor.py` (always-on runtime wrapper)
4. `ductor_bot/messenger/telegram/app.py` + `messenger/telegram/startup.py` (Telegram), `ductor_bot/messenger/matrix/bot.py` (Matrix)
5. `ductor_bot/orchestrator/core.py` + `orchestrator/lifecycle.py`
6. `ductor_bot/bus/*` (unified delivery/injection)
7. `ductor_bot/tasks/hub.py` + `tasks/registry.py`
8. `ductor_bot/cli/service.py` and provider wrappers

## 9) Command surface (high level)

Chat commands (Telegram and Matrix):

- `/new`, `/reset`, `/stop`, `/stop_all`, `/interrupt`, `/model`, `/effort`, `/status`, `/memory`, `/session`, `/sessions`, `/tasks`, `/cron`, `/showfiles`, `/info`, `/help`, `/diagnose`
- `/start` and `/restart` are also supported. Telegram-only utility commands: `/where`, `/leave` (work but are not in command popup)
- Matrix uses `!` prefix by default (e.g. `!help`, `!status`); `/` also works but may conflict with Element's built-in commands

Main-agent only (chat commands):

- Telegram: `/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`
- Matrix: `!agents`, `!agent_start`, `!agent_stop`, `!agent_restart` (`/` prefix also supported)

Available on all agents:

- Telegram: `/agent_commands`
- Matrix: `!agent_commands` (`/` prefix also supported)

CLI:

- `ductor`, `ductor status`, `ductor help`, `ductor --version`
- `ductor agents list/add/remove`

## 10) Container runtime

The bot and provider CLIs run in the same Docker Compose bot container.
The host `DUCTOR_HOME` is mounted at `/ductor`. Use
`scripts/onboard.sh` to prepare the home, then
`docker compose up -d --build` to start. See
[installation](installation.md) for operations. The browser is a separate service
on `ductor-net`; the base stack has no published host ports or Docker socket.
The bot runs with the onboarding account's UID/GID (`DUCTOR_UID` and `DUCTOR_GID`,
each defaulting to 1000), `init: true`, restart policy `unless-stopped`,
and measured 30-second stop grace. API/webhook host ports appear only through the
onboarding-generated override. Rebuild/update replace services and can interrupt
work.

Sources: session key (`ductor_bot/session/key.py`),
API construction (`ductor_bot/api/server.py`),
inter-agent bus (`ductor_bot/multiagent/bus.py`),
commands (`ductor_bot/commands.py`), Compose (`compose.yaml`).

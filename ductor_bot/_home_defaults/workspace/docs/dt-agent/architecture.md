# Architecture

## Runtime Overview

The checkout is the image build input: one tracked `ductor_bot/` tree is installed
by `docker/ductor-bot/Dockerfile`. Provider CLIs execute in the bot container,
with a separate browser service; there is no nested CLI sandbox or Docker socket
mount. The host `DUCTOR_HOME` maps to `/ductor`; sub-agents use
`<main_home>/agents/<name>`. Host-default `~/.ductor` notation below is configurable.
See deployment model (`docs/fork_deploy_model.md`) and all changes (`docs/CHANGES.md`).

DT Agent supports multiple messaging transports. The `transport` config
field (`"telegram"`, `"matrix"`, or `"slack"`) selects a single ingress/delivery
layer via a transport registry (`messenger/registry.py`). The
`transports` list field enables parallel multi-transport execution
(e.g. `["telegram", "matrix"]`); when empty it falls back to the
single `transport` value.

```text
Telegram path:                          Matrix path:
  Telegram Update                         Matrix sync event
  -> aiogram Dispatcher/Router            -> matrix-nio callback
  -> AuthMiddleware                       -> room/user allowlist check
  -> SequentialMiddleware                 -> MatrixBot handler
  -> TelegramBot handler                  -> Orchestrator
  -> Orchestrator                         -> CLIService
  -> CLIService                           -> provider subprocess
  -> provider subprocess                  -> Matrix room message
  -> Telegram message (stream edits)

Background/async results (both transports):
  -> Observer/TaskHub/InterAgentBus callback
  -> bus.adapters -> Envelope
  -> MessageBus
  -> optional lock + optional session injection
  -> transport-specific delivery (TelegramTransport or MatrixTransport)
```

When `transports` lists more than one entry, `MultiBotAdapter`
starts all transports in parallel and exposes a unified
`BotProtocol` to the orchestrator.

Direct API path (`api.enabled=true`) uses `ApiServer` and calls orchestrator streaming callbacks directly.

### Transport dispatch

`messenger/registry.py` maps `config.transport` to a bot factory:

- `"telegram"` -> `TelegramBot` (aiogram)
- `"matrix"` -> `MatrixBot` (matrix-nio)
- `"slack"` -> `SlackBot` (Socket Mode)
- `"headless"` -> `HeadlessBot` (delegation-only sub-agents; rejected for main)

All implement `BotProtocol`. A messenger transport also needs its optional
image dependencies, credentials, allowlist policy and delivery implementation;
a factory entry alone is not sufficient. The diagram above details Telegram
and Matrix, not every supported transport.

## Startup Flow

### `ductor` entry (`ductor_bot/__main__.py`)

1. parse CLI args and dispatch command (implementation in `cli_commands/*`)
2. default run path:
   - `_is_configured()` only checks the minimal onboarding gate for active transports
   - Telegram gate: non-placeholder token + non-empty `allowed_user_ids`
   - Matrix gate: non-empty `homeserver` + non-empty `user_id`
   - Slack gate: non-empty bot/app tokens
   - deeper active-transport validation happens in `_validate_*_config()`
   - if not configured: print container onboarding instructions and exit 1;
     `scripts/onboard.sh` is the separate host setup path
   - `start_bot()` acquires the nonblocking home lock (`bot.lock`) before loading config or writing startup state; if the configured home resolves to a different directory, it locks that home too
   - load/deep-merge config (`load_config()`)
   - initialize workspace (`init_workspace(paths)`)
   - run supervisor via `run_bot(config)` (transport-agnostic)
3. With the home lock held, `run_bot()` overwrites `bot.pid` with its own PID and starts `AgentSupervisor`; a PID left by another container does not prevent startup

### Supervisor startup (`multiagent/supervisor.py`)

1. start `InterAgentBus`
2. start `InternalAgentAPI`
3. optional shared `TaskHub` (`tasks.enabled=true`)
4. create/start main `AgentStack`
5. wait up to 120 seconds for main readiness (`_main_ready`); async-result delivery separately checks current generation/readiness
6. load/start sub-agents from `agents.json`
7. start `SharedKnowledgeSync`
8. start `agents.json` watcher and persisted-result redelivery scan
9. block on main completion and return its exit code

### Bot startup (Telegram: `messenger/telegram/startup.py`, Matrix: `messenger/matrix/startup.py`)

Telegram startup:

1. create orchestrator (`Orchestrator.create(...)`)
2. initialize chat tracker (`chat_activity.json`)
3. seed `TopicNameCache` from persisted sessions and wire topic name resolver into `SessionManager`
4. consume restart sentinel
5. wire observers to message bus (`orch.wire_observers_to_bus(...)`)
6. register config hot-reload callback for auth/group updates
7. startup classification (`first_start`/`service_restart`/`system_reboot`) + startup notification policy
8. recovery planning (`inflight_turns.json` + recovered named sessions)
9. sync Telegram commands and start restart watcher
10. run group audit immediately + start periodic 24h audit loop

Matrix startup also consumes restart state, classifies/notifies startup, and plans foreground/named-session recovery. It uses matrix-nio's `AsyncClient` sync loop instead of aiogram polling. Secondary transports sharing an orchestrator skip the primary initialization/recovery sequence.

### Orchestrator factory (`orchestrator/lifecycle.py`)

1. resolve paths and set process-wide `DUCTOR_HOME` only for main
2. inject runtime environment note into workspace rule files
3. instantiate `Orchestrator`
4. check provider auth and apply provider availability
5. initialize installed-provider model caches and task observers
6. start eligible observers + rule/skill watchers (headless skips messenger automation)
7. optional API server startup
8. start config reloader

## Command Ownership and Routing

Bot-level handlers (`messenger/telegram/app.py`):

- dedicated transport handlers: `/start`, `/help`, `/info`, `/showfiles`, `/stop`, `/stop_all`, `/restart`, `/new`, `/session`, `/sessions`, `/tasks`, `/agent_commands`
- forwarded through `_on_command`: `/status`, `/memory`, `/model`, `/effort`, `/cron`, `/diagnose`, `/reset`
- `/interrupt` uses the middleware/message abort path; `/where` and `/leave` use message utilities
- main-agent-only handlers: `/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`

Matrix command ownership (`messenger/matrix/bot.py`):

- direct transport commands: `!stop`, `!stop_all`, `!interrupt`, `!restart`, `!new`, `!reset`, `!help`, `!start`, `!info`, `!session`, `!showfiles`, `!agent_commands`
- orchestrator-routed commands: `!status`, `!model`, `!effort`, `!memory`, `!cron`, `!diagnose`, `!sessions`, `!tasks`
- main-agent-only multi-agent commands: `!agents`, `!agent_start`, `!agent_stop`, `!agent_restart` (`/` prefix also supported)

Orchestrator command registry (`orchestrator/commands.py`):

- `/new`, `/reset`, `/status`, `/model`, `/effort`, `/memory`, `/cron`, `/diagnose`, `/sessions`, `/tasks`
- multi-agent commands are registered at runtime by supervisor hook

Abort behavior:

- `/stop` and `/stop_all` are handled before normal lock routing
- main-agent `/stop_all` uses supervisor callback to abort across all stacks

Quick-command bypass (`SequentialMiddleware`):

- `/status`, `/memory`, `/cron`, `/diagnose`, `/model`, `/showfiles`, `/sessions`, `/tasks`, `/where`, `/leave`

## Session and Topic Model

Sessions are keyed by `SessionKey(transport, chat_id, topic_id)`.

- Telegram topic messages (including private-chat topics) are isolated from each other and from the base chat only when `message.is_topic_message` is true: `topic_id = message.message_thread_id if message.is_topic_message else None`. A thread ID alone does not select topic scope (`ductor_bot/messenger/telegram/topic.py:37`).
- Matrix rooms use `transport="mx"` with deterministic int room mapping
- API auth currently constructs the default `transport="tg"` key with optional
  `channel_id -> topic_id`. It can share Telegram state at the same numeric IDs;
  API CLI turns carry `request_transport="api"` for origin metadata. Session
  storage and CLI origin metadata are distinct here.
- `sessions.json` remains backward-compatible with legacy unprefixed keys
- topic names are cached from forum topic events and shown in `/status` and `/sessions`
- `/new @topicname` resets a specific topic session without switching to that topic

Provider isolation inside a session:

- each session has provider-local buckets (`provider_sessions`)
- switching provider/model preserves other provider buckets
- `/new` resets the configured default-provider bucket
- `/reset` resets the currently active provider bucket

Per-topic `/model` behavior:

- inside a topic, model/provider switch updates that topic session only
- global config (`config.json` / `agents.json`) is updated only outside topic scope

`/new` behavior:

- `/new` kills active work for the current chat/topic and resets the provider bucket tied to `config.model`
- it does **not** clear every provider bucket and does not target the last temporary `/model` switch automatically

## Flow Details

### Normal and streaming flows (`orchestrator/flows.py`)

1. resolve runtime target (provider/model)
2. resolve session by `SessionKey`
3. new session: append `MAINMEMORY.md` (+ agent roster context if available)
4. apply message hooks
5. build `AgentRequest` with `topic_id`
6. persist in-flight foreground turn (`InflightTracker.begin`)
7. execute CLI (`execute` or `execute_streaming`)
8. session recovery (single retry) on:
   - SIGKILL
   - invalid resumed session
9. on streaming compaction boundaries: mark session for post-turn memory flush/compaction
10. update session metrics and ID on success
11. after successful streaming turn: optional silent memory flush + compaction
12. clear inflight marker in `finally`

Gemini safeguard:

- if Gemini is in API-key mode and `gemini_api_key` is empty/null, flow returns warning text and skips CLI execution.

Successful empty-result safeguard:

- tool-only successful turns no longer disappear silently; `_finish_normal(...)` substitutes a neutral status line when the provider returns an empty success payload

### Heartbeat flow

- read-only active-session lookup (no create)
- skips when no session/session ID or cooldown not reached; execution preserves the active session's provider, model, and reasoning effort
- executes prompt with session resume
- suppresses pure ACK responses
- updates session only for non-ACK alerts

### Named sessions (`/session`)

- `BackgroundObserver` executes named session turns asynchronously
- follow-up support:
  - foreground: `@session-name <message>`
  - background: `/session @session-name <message>`
- `/sessions` interactive management via selector callbacks

### Delegated tasks (`TaskHub`)

- shared registry: `~/.ductor/tasks.json`
- folders: `<parent_agent_home>/workspace/tasks/<task_id>/` (registry remains shared at the main home)
- endpoints via internal API (`/tasks/*`)
- topic-aware routing: task results/questions retain `thread_id` and are injected back into originating topic session
- task tools receive `DUCTOR_CHAT_ID` and optional `DUCTOR_TOPIC_ID`
- single-task permanent delete: `/tasks/delete` + `TaskRegistry.delete()`

## MessageBus and Delivery

`MessageBus` replaces fragmented delivery paths.

- `Envelope` captures origin, lock mode, injection requirements, delivery mode
- observers are wired in one call: `ObserverManager.wire_to_bus(...)`
- Telegram transport formatting is centralized in `messenger/telegram/transport.py`
- one orchestrator `LockPool` is shared by primary transport ingress, `MessageBus` injection, active-session delegation questions and `ApiServer`, preventing lock drift across middleware, background delivery and API turns; a standalone `ApiServer` without a supplied pool creates its own, and lock keys remain `(chat_id, topic_id)`
- Transport-aware delivery: each `Envelope` carries a `transport` field; UNICAST
  targets matching transports. Non-inter-agent origins may broadcast through
  another transport only when the requested one is unregistered. Registered
  transport delivery failures do not cascade. Inter-agent results never use
  this cross-transport fallback.
- Inter-agent results require successful session injection before final delivery;
  unavailable recipients or delivery failures are preserved for retry. This is
  not an exactly-once guarantee; see known limits (`docs/CHANGES.md`).

Foreground user conversations recheck the **resolved session provider** authentication immediately before CLI execution. Text, media and prompt-button turns, including named-session foreground follow-ups, return localized host-repository `./scripts/login.sh <provider>` instructions and do not run the CLI when unauthenticated. Authentication changes are reread on the next user turn; Gemini can use its configured API key. Telegram, Matrix, Slack and direct API use these shared orchestrator flows. Inter-agent, cron and webhook task execution paths are unchanged by this foreground check.

Actual allowlist denials in Telegram, Matrix and Slack emit INFO `Allowlist rejection chat_type=... chat_id=... user_id=... topic_id=...`, containing metadata only, never message content. The common `RejectionLog` helper limits each retained chat/user pair to one entry per 600 seconds (topics share that pair’s limit). Each logger instance retains up to 1,024 pairs and evicts the oldest when full; an evicted pair can log again. This does not change each transport’s authorization/mention policy or its handling of non-message events.

## Callback Query Routing

Special callback namespaces:

- `mq:*` queue cancel
- `acs:*` main-only Claude account controls
- `usg:refresh` read-only usage refresh on main and sub-agent bots
- `ms:*` model selector
- `crn:*` cron selector
- `nsc:*` session selector
- `tsc:*` task selector
- `ns:*` named-session follow-up
- `ms:e:*` reasoning-effort selector (part of the `ms:*` namespace)
- `sf:*` / `sf!` file browser

Telegram recognized slash-command buttons are propagated through the typed-message command handlers and existing auth/mention filters after preserving the selected label; unknown slash commands remain AI prompts.

Selector callbacks use transport-agnostic selector types (`Button`, `ButtonGrid`, `SelectorResponse`) from `orchestrator/selectors/models.py`. The main Telegram bot exposes `/accounts` (`acs:` callbacks) for Claude slot ordering and cooldown clearing, sharing `cli/claude_slots.py` with host management options. Each slot also shows its saved Claude usage with capture and reset times. Both account entry points check the existing main-only policy. Config and cooldown mutations reload the latest files under companion-file locks. Priority changes apply to main within seconds and to sub-agents at their next start; cooldown clearing applies to all on the next call.

Telegram `/usage` is available on all agent bots through normal and quick command routing under the existing message/callback allowlists. It reads shared Claude snapshots without changing files, sends a fixed-width view with a Codex loading line, then edits the same message after querying Codex. `usg:refresh` rereads the snapshot and requeries Codex before a single in-place edit. Unchanged-message responses are silently ignored by the shared selector editor. Claude values come from normal runs, Codex from on-demand app-server queries.

## API Architecture

`ApiServer` (`api/server.py`) provides:

- websocket auth + E2E (`type=auth`, `token`, `e2e_pk`)
- optional auth-time session overrides:
  - `chat_id` (positive int; otherwise falls back to configured default)
  - `channel_id` (positive int maps to `SessionKey.topic_id`; invalid values become `None`)
- encrypted message streaming events (`text_delta`, `tool_activity`, `system_status`, `result`)
- encrypted abort
- bearer-auth HTTP endpoints (`/files`, `/upload`)

## Restart and Shutdown

Restart triggers:

- `/restart` sentinel + exit code `42`
- external restart marker file
- main-agent restart propagates to process/container level (Compose restart policy)
- sub-agent restart rebuilds only that stack in-process
- SIGINT/SIGTERM cancel `run_bot`, await `stop_all`, and release the PID lock;
  Compose uses `init: true` and measured `stop_grace_period: 30s`

Shutdown (`orchestrator/lifecycle.shutdown`):

1. kill active CLI processes
2. stop API server
3. cleanup managed skill links
4. stop observers + config reloader + cache observers + watchers

## Workspace Seeding Model

Source: `ductor_bot/_home_defaults/`.

Zone rules (`workspace/init.py`):

- Zone 2 overwrite:
  - `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`
  - tool scripts under `workspace/tools/` (root `_tool_shared.py`) and `workspace/tools/{cron,webhook,agent,task,memory,media,browser}_tools/*.py`; a user-edited copy is backed up to `.py.bak` (never overwriting an earlier backup) before replacement, see `modules/workspace.md`
- Zone 3 seed-once for other files
- `RULES*.md` templates are selected/deployed by `RulesSelector`

Rule sync:

- recursive mtime sync for `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`
- task-folder provider rules backfilled by `ensure_task_rule_files(...)`

## Multi-Agent Notes

- sub-agents are full stacks with own workspace/session files; messenger subs select transports from their own usable credentials, while headless subs need no bot
- all stacks share one event loop, inter-agent bus, and optional shared task hub
- async inter-agent results are injected via bus envelopes
- ordinary source-aware recipient/fallback inter-agent sessions use `ia.<sender-slug>.t<topic-or-0>.x<hash>`;
  the hash includes sender/chat/topic. Only context-free calls use `ia-<sender>`
- async-delegation sub→main synchronous questions resume the original chat/topic’s **current active session**, privately, under the shared user/result lock; a Named/unconfirmed background source, 60s wait or unavailable source falls back to a separate source-header session
- sync delegation instructs the sub to put questions in the final report; the bus rejects verified sync-delegation questions to main before orchestrator invocation/session creation. Unknown provenance retains the separate-session path. Context-free messages keep their previous path; [details](modules/multiagent.md#questions-during-delegation)
- provider changes reset that inter-agent named session and surface a switch notice
- sync returns to the calling tool; async injects the result into the calling
  agent's original chat/topic for that agent to process and report
- async onward delegation from an inter-agent turn is rejected; synchronous questions to main are allowed during async delegation and rejected during verified sync delegation
- headless agents cannot originate async calls or receive async results
- explicit `reply_to` selects a result receiver; async receipt notices use the
  receiving agent's own bot, require `interagent_notify_received=true` (default
  false), and remain suppressed by `silent`

Sources for current structure: entrypoint (`ductor_bot/__main__.py`),
transport factories (`ductor_bot/messenger/registry.py`),
supervisor (`ductor_bot/multiagent/supervisor.py`),
orchestrator lifecycle (`ductor_bot/orchestrator/lifecycle.py`),
API auth/session key (`ductor_bot/api/server.py`),
delivery bus (`ductor_bot/bus/bus.py`), workspace zones (`ductor_bot/workspace/init.py`).

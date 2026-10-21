# orchestrator/

Central routing layer between ingress transports (Telegram/Matrix/Slack/API) and CLI execution.

Queued user metadata is separate from user text during command/directive routing and normal/foreground Named request preparation. AgentRequest.cli_prompt prepends it only in the CLI gateway, preserving display/state previews. See [queued user message context](messenger.md#queued-user-message-context).

## Files

- `orchestrator/core.py`: `Orchestrator` class, command dispatch, integrations, thin delegations
- `orchestrator/lifecycle.py`: async factory/startup/shutdown helpers
- `orchestrator/observers.py`: `ObserverManager` for all observer lifecycle wiring (including `memory_lint.MemoryLintObserver`; non-headless agents only; Telegram startup sets its report handler through `Orchestrator.set_memory_lint_report_handler`)
- `orchestrator/providers.py`: `ProviderManager` (provider auth/model resolution/metadata)
- `orchestrator/injection.py`: shared session injection for inter-agent and task flows
- `orchestrator/registry.py`: `CommandRegistry`, `OrchestratorResult`
- `orchestrator/commands.py`: slash-command handlers
- `orchestrator/flows.py`: normal/streaming/named-session/heartbeat flows
- `orchestrator/directives.py`: leading `@...` parser
- `orchestrator/hooks.py`: message hooks (`PERSONA_SETUP`, `MAINMEMORY_REMINDER`, delegation hints)
- `orchestrator/memory_flush.py`: silent pre-compaction memory flush + optional `MAINMEMORY.md` compaction
- `orchestrator/selectors/*`: model/cron/session/task selector modules + selector types

## Why it was split

Recent refactors moved startup/provider/observer concerns out of `core.py`:

- lifecycle logic -> `lifecycle.py`
- provider/model state -> `providers.py`
- observer orchestration -> `observers.py`
- selector UI modules -> `orchestrator/selectors/` (transport-agnostic)

Result: smaller `core.py` with clearer responsibilities.

## Startup (`Orchestrator.create` -> `lifecycle.create_orchestrator`)

High-level steps:

1. resolve paths + set main-agent `DUCTOR_HOME`
2. inject runtime notices into existing workspace rule files
3. instantiate `Orchestrator` (sessions, CLI service, hooks, optional memory flusher)
4. detect provider authentication and initialize Gemini state
5. initialize/start caches for installed providers
6. initialize/start observers; headless skips cron, webhook and heartbeat
7. start rule/skill watchers
8. start the optional direct API server
9. start config reloader

## Routing entry points

- `handle_message(key, text)`
- `handle_message_streaming(key, text, callbacks...)`

Common path:

1. clear abort marker for chat
2. suspicious input detection (log warning only)
3. command registry dispatch
4. directive parsing
5. normal or streaming flow

`is_chat_busy(chat_id, topic_id=None)` checks whether a CLI process is running. When `topic_id` is provided, only processes for that specific topic are considered busy; otherwise any process for the chat qualifies.

## `OrchestratorResult` metadata

`OrchestratorResult` carries optional metadata fields populated after CLI execution:

- `model_name`: resolved model identifier used for the turn
- `duration_ms`: wall-clock execution time in milliseconds

## Command registry

Registered command handlers:

- `/new`, `/reset`, `/status`, `/model`, `/effort`, `/memory`, `/cron`, `/diagnose`, `/sessions`, `/tasks`

`/new` resets only the currently configured provider bucket for the active `SessionKey`.
Other provider buckets for the same chat/topic remain intact.

`/reset` resets the currently active provider bucket for the active `SessionKey`.

Telegram `/model` bypasses its sequential queue; the selector shows current
model information while that key is busy. Matrix orchestrator commands acquire
the room lock and can wait behind active work.

Runtime main-agent registration:

- `/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`

Not orchestrator-owned:

- `/stop`, `/stop_all` (middleware/bot layer)
- Telegram `/usage`: read-only on main and sub-agent bots, bypassing the sequential queue; Claude shows the last saved run values, Codex is queried on command or refresh, and Grok automatic queries are unsupported

## Provider/model management (`ProviderManager`)

Responsibilities:

- authenticated provider set
- runtime model->provider resolution
- known-model IDs for directive parsing
- API provider metadata for auth responses

Directive resolution supports:

- model directives (`@opus`, `@flash`, cache-backed IDs)

## Selector subsystem (`orchestrator/selectors/`)

Selectors are transport-agnostic and return `SelectorResponse` with abstract button types.

Modules:

- `model_selector.py` (`ms:*`)
- `cron_selector.py` (`crn:*`)
- `session_selector.py` (`nsc:*`)
- `task_selector.py` (`tsc:*`)
- `account_selector.py` (`acs:*`): main Telegram bot only; Claude authentication, slot priority and cooldown management through `cli/claude_slots.py`. Both command and callback entry points enforce the existing main-only policy; stale buttons refresh without changing files. Each slot includes a saved usage summary with capture and reset times
- `usage_view.py` (`usg:refresh`): usage bars and compact account summaries. Telegram sends saved Claude values with a Codex loading line, then edits the same fixed-width message with queried Codex values and a refresh button. Refresh rereads Claude values and requeries Codex, editing the same message once; unchanged-message errors are ignored
- shared models/utilities in `selectors/models.py`, `selectors/utils.py`

Important model-selector behavior:

- topic-scoped `/model` changes only the active topic session
- non-topic `/model` updates config defaults (and sub-agent `agents.json` when relevant)

## Flow behavior (`flows.py`)

### Normal/streaming

- session resolution via `SessionKey`
- provider-isolated session buckets
- system prompt append built by `build_append_prompt(...)`: `MAINMEMORY.md` and the agent roster on a new session, plus the `append_system_prompt_files` block on every turn (default `PERSONA.md`, `BEHAVIOR.md`; a persona still carrying `<!-- PERSONA_UNSET -->` is skipped). For a **Claude** turn with `claude_system_prompt_snapshot: "off"` (default) and a CLI that supports it, MAINMEMORY, roster and file blocks are assembled on **every** turn, because the CLI renders the system prompt anew each turn; otherwise only new sessions carry MAINMEMORY. See [Claude system prompt snapshot](cli.md#claude-system-prompt-snapshot)
- message hooks on the user text: `PERSONA_SETUP` (first-run setup hint while `PERSONA.md` carries the unset marker: new session's first turn, then every 6th message; stops when the marker is removed; disabled by `persona_setup_prompt: false`), `MAINMEMORY_REMINDER` (every 6th message; keep MAINMEMORY to essentials plus an index, details in topic files, save what matters regardless of the model in `memory_system/`, and use a model's built-in memory only for notes that apply to that model alone), `DELEGATION_BRIEF` (new session) and `DELEGATION_REMINDER`. Hook text is part of the AI input only: in-flight turn previews and the recovery notices built from them keep the user's own text. User Named sessions (foreground and background, new and follow-up turns) get the same first-run hint under the same rules (first turn, then every 6th message, off with `persona_setup_prompt: false` or once the marker is removed); inter-agent and result injections do not
- in-flight foreground turn tracking
- `CompactBoundaryEvent` marks a pre-compaction boundary; after a successful user turn the optional `MemoryFlusher` may run a silent follow-up flush turn and, when configured, a compaction turn
- single automatic recovery retry on SIGKILL/invalid resumed session
- stale-session detection currently covers CLI errors such as `invalid session`, `session not found`, and `no conversation found`
- session update on success
- successful tool-only / empty turns are converted into a neutral visible status line instead of disappearing

### Named session flow

- named session registry lookup/resume
- foreground follow-up support (`@name <msg>`)
- appended prompt: the file blocks only (never MAINMEMORY), as before; for Claude with the snapshot off the request also carries the snapshot flag. The shared `build_append_prompt(..., include_mainmemory_policy=False)` assembles it

### Heartbeat flow

- read-only session lookup
- requires a resumable active session and preserves its provider/model/reasoning effort (empty stored model falls back to the configured model)
- cooldown checks
- ACK-token suppression

## Injection paths (`injection.py`)

Shared helper `_inject_prompt(...)` backs public `Orchestrator.inject_prompt(...)`.
A non-isolated injection continues the user's active session, so its appended prompt is assembled like a normal conversation turn (`build_append_prompt`; for Claude with the snapshot off: MAINMEMORY, roster and file blocks every turn, plus the snapshot flag; a call without an active session starts a new session and carries MAINMEMORY). An isolated injection (a separate session) keeps the file blocks only and gets just the flag. Inter-agent turns and background tasks also only get the flag; their append policy is unchanged.
`MessageBus` adapters turn asynchronous inter-agent results and task
results/questions into injection prompts. The removed
`handle_async_interagent_result`, `handle_task_result` and `handle_task_question`
methods are not the current API.

All injection paths respect `topic_id` when provided.

Inter-agent ingress also lives in `injection.py`:

- ordinary recipient turns and question fallbacks use `interagent_session_name`, which produces sender/chat/topic-scoped `ia.<slug>.t<topic-or-0>.x<hash8>` names when origin context is present, with `.l<lane>` appended for a lane request; legacy `ia-<sender>` remains only without a source chat and without a lane. The whole Named-session part of the turn runs inside the bus admission (see [lanes](multiagent.md#lanes-admission-and-the-time-contract)); the CLI process label is `interagent:<session_name>`
- ordinary recipient/fallback Named Session IA turns carry their original chat/topic/transport and `DUCTOR_IA_TURN=1`; asynchronous onward calls are rejected
- every work request a sub-agent receives (synchronous or asynchronous, from main or another sub) gets the fixed `[Delegated work rules]` block (`DELEGATED_WORK_RULES`) after the request: finish inside the current turn, do not hand off to a background task, and ask for a split if it cannot finish within the limit. A request with a lane gets one more line in that block (lane name, other lanes may run concurrently in the same workspace, repository changes in a lane-specific git worktree, shared memory files not edited unless instructed). The block of an unlaned request is unchanged. It coexists with the synchronous-delegation note and is never added for delegation questions, for main as recipient, or for async result injections
- if the provider CLI rejects a stored inter-agent session after an update/cache clear, the orchestrator ends that named session, retries once with a fresh session, and surfaces a visible recovery notice back to the caller

Async delegation questions use `_answer_async_delegation_question()` and `_inject_prompt()` in the current source session without chat delivery. The shared `MessageBus.lock_pool` serializes them with user turns/results; a 60s wait triggers the existing Named Session plus the source header. The bus rejects verified synchronous-delegation questions to main before this handler executes; unknown-mode questions retain the fallback, and context-free messages keep their earlier path. See [delegation questions](multiagent.md#questions-during-delegation).

Foreground user conversations recheck the **resolved session provider** authentication immediately before CLI execution. Text, media and prompt-button turns, including named-session foreground follow-ups, return localized host-repository `./scripts/login.sh <provider>` instructions and do not run the CLI when unauthenticated. Authentication changes are reread on the next user turn; Gemini can use its configured API key. Telegram, Matrix, Slack and direct API use these shared orchestrator flows. Inter-agent, cron and webhook task execution paths are unchanged by this foreground check.

## Interrupted foreground turns (shutdown recovery)

A foreground conversation turn is recorded in `inflight_turns.json` while it runs, one record per chat and topic (the key is the chat id, or `<chat>:<topic>` in a topic; the stored preview is the user's own text, never hook text; files from before the topic key read as topic-less records). The record is normally removed when the turn ends. When the **shutdown** cuts a turn off, the record is kept and the next start announces it once:

- **Mark first.** The orchestrator's inflight tracker is marked as shutting down at the earliest shutdown point, before any task is cancelled or CLI process killed: the SIGTERM/SIGINT handler (`__main__.py`), `AgentSupervisor.stop_all`, `stop_agent` (including the sub restart exit), the main restart exit, and `/restart` (restart sentinel/marker handling in `infra/restart.py`; a marker found while starting up is not a shutdown). `lifecycle.shutdown` marks as well, before it kills the CLIs.
- **Keep condition.** The turn's `finally` skips `complete()` only when all hold: the turn did not finish (no final answer), the user did not stop it (`/stop` and interrupt marks are snapshotted when the shutdown is marked, so the kill of the shutdown is not mistaken for a user stop; a `/stop` of one topic covers only that topic's turn, a stop of the whole chat covers all of its topics), and the orchestrator is shutting down. Normal completion, ordinary errors and `/stop` still clear the record.
- **Next start.** The Telegram, Matrix and Slack startup recovery plans the notice from the record (one per chat and topic, including private chats, groups and forum topics; only records without a chat id, 0, are skipped), sends one "interrupted request" notice with the user's original text, then clears the file (a second normal start announces nothing). The turn is not re-run.

| Kind | Interrupted by a normal shutdown | Next start | Status |
|---|---|---|---|
| Foreground conversation turn | inflight record kept | one notice (user's original text only), record consumed, no automatic re-run | new |
| Inter-agent async request | durable record (existing) | "interrupted by restart" notice (existing) with session name and lane shown, no automatic re-run | display added |
| Normal Named background session | existing | notice, then automatic resume (existing) | unchanged |
| `/stop`, normal completion, ordinary error | record removed | no notice | unchanged |

A crash (no orderly shutdown) leaves the record as before. Sub-agents behave the same: a sub's foreground turn cut off by `stop_agent` or its restart is announced in its own chat after it starts again.

## Observer and bus wiring

`Orchestrator.wire_observers_to_bus(bus, wake_handler=...)`:

- delegates to `ObserverManager.wire_to_bus(...)`
- sets bus injector to orchestrator
- replaces old per-observer setter scatter

Observer manager owns lifecycle for:

- background, cron, webhook, heartbeat, cleanup
- config reloader
- rule sync watcher
- skill sync watcher

## Config hot-reload impact

`_on_config_hot_reload(...)` updates orchestrator-owned runtime services for hot fields:

- CLI defaults (`model`, `provider`, `max_turns`, `max_budget_usd`, `permission_mode`, `reasoning_effort`, provider CLI args)
- provider known-model cache refresh when `model` changes
- i18n re-init when `language` changes
- heartbeat observer start/stop when `heartbeat.enabled` flips
- external bot callback for transport-owned hot fields such as auth/group updates

`ConfigReloader` classifies additional top-level fields as hot globally, but fields such as `notifications`, `transcription`, and memory-maintenance settings still require restart today because they are not emitted as orchestrator hot updates.

Restart-required fields are surfaced via reloader callback logging.

## API integration

`lifecycle.start_api_server(...)`:

- generates API token when missing
- computes default chat fallback from `api.chat_id` or first allowed user
- wires streaming message handler, abort handler, file context, provider metadata, active-state getter

`ApiServer` supports auth payload `channel_id` -> topic-aware `SessionKey`.

## Shutdown

`lifecycle.shutdown(...)` performs:

0. mark the inflight tracker as shutting down (`begin_shutdown`; see [interrupted foreground turns](#interrupted-foreground-turns-shutdown-recovery))
1. kill active CLI processes
2. stop API server (if running)
3. cleanup managed skill links
4. stop observers/reloader/cache/watchers

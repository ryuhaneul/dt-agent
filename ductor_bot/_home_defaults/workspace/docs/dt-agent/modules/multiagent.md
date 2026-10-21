# multiagent/

Multi-agent runtime: run multiple independent DT Agent agents in one process.

## Files

- `multiagent/supervisor.py`: `AgentSupervisor` lifecycle, health, crash recovery, agents watcher
- `multiagent/stack.py`: `AgentStack` container (config + bot + orchestrator)
- `multiagent/bus.py`: `InterAgentBus` (sync + async inter-agent messaging)
- `multiagent/session_gate.py`: `SessionGateRegistry` (per-session FIFO admission + per-agent execution slots, owned by the bus)
- `multiagent/internal_api.py`: localhost HTTP bridge for tool scripts (`/interagent/*`, `/tasks/*`)
- `multiagent/models.py`: `SubAgentConfig`, merge helpers
- `multiagent/registry.py`: `agents.json` read/write
- `multiagent/health.py`: per-agent health model
- `multiagent/shared_knowledge.py`: shared knowledge sync (`SHAREDMEMORY.md`)
- `multiagent/commands.py`: Telegram commands (`/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`)

## Runtime model

```text
AgentSupervisor
  +-- AgentStack "main"
  +-- AgentStack "sub-*" (0..n)
  +-- InterAgentBus
  +-- InternalAgentAPI (localhost bridge)
  +-- optional TaskHub (shared)
  +-- SharedKnowledgeSync
  +-- FileWatcher(agents.json)
```

Each stack has its own workspace and sessions. Messenger credentials are optional for headless sub-agents; all stacks share process/event-loop infrastructure.

## Startup sequence (`AgentSupervisor.start`)

1. start inter-agent bus
2. start internal API
3. optional shared task hub
4. create/start main stack
5. wait for main readiness
6. start sub-agents from `agents.json`
7. start shared knowledge sync
8. start `agents.json` watcher

## Dynamic agent changes

Watcher polls `agents.json` every 5s.

- added entry -> validate its name and start the sub-agent
- removed entry -> stop sub-agent
- restart triggers for running sub-agents:
  - `transport` or `transports` changed
  - Telegram identity changed (`telegram_token`)
  - Matrix identity changed (`homeserver`, `user_id`, `password`, `access_token`, `device_id`, or `store_path`)
- other field changes do not auto-restart running agent

## Crash/restart policy

Per agent `_supervised_run` behavior:

- clean exit -> stop
- exit code `42`:
  - main -> propagate full process/service restart
  - sub-agent -> in-process hot-reload
- main fatal crash -> terminate supervisor; transient connection/timeout or Telegram network/server errors -> retry indefinitely with 5/10/20/40/60-second capped backoff
- sub-agent crash -> up to 5 retries with exponential backoff, then leave health `crashed` until manual action

## Sub-agent config (`agents.json`)

Minimal entry:

```json
{
  "name": "coder",
  "telegram_token": "123456:ABC...",
  "allowed_user_ids": [12345678],
  "allowed_group_ids": [],
  "provider": "codex",
  "model": "o3"
}
```

Merge behavior:

- base: main `AgentConfig`
- override: non-null `SubAgentConfig` fields (nested blocks replace, rather than deep-merge, the main block)
- `exclude_none=True` retains the default non-null sub-agent heartbeat block, so it replaces main heartbeat settings even when omitted from the entry.
- sub heartbeat defaults disabled; the recipient inherits `interagent_notify_received` unless explicitly overridden
- `memory_lint` is the exception to inheritance: a sub never takes main's value; it gets the framework defaults (`mode: "structure"`) plus the optional partial `memory_lint` object of its own `agents.json` entry (for example `"memory_lint": {"mode": "full"}`), and the supervisor mirrors the effective value into the sub's `config.json`. Applying a changed value to a running sub needs `/agent_restart <name>`. See [MemoryLintConfig](../config.md#memorylintconfig)
- always forced:
  - `ductor_home=<main_home>/agents/<name>/` (default host main home `~/.ductor`)
  - sub-agent `telegram_token`, `allowed_user_ids`, `allowed_group_ids`
  - `api.enabled=false` unless explicitly provided for sub-agent

## Sub-agent transport selection and delegation-only mode

Names must match `^[a-z0-9][a-z0-9._-]{0,63}$`, cannot contain `..`, and
cannot be `main`. The supervisor skips invalid entries and notifies the main
agent.

An explicit `transport` selects only that transport and requires that
sub-agent's Telegram or complete Matrix credentials. Explicit `headless` needs
none. Without `transport`, the sub-agent's own Telegram token and Matrix
credentials select one or both; with neither, the agent is headless. Main
credentials and transport lists are not inherited for this selection. Slack
is not supported for sub-agents.

Headless agents have no chat output: cron, heartbeat, and webhook observers
do not start. Main can delegate to headless agents synchronously or
asynchronously and receives results in its own chat. Headless agents can
delegate synchronously but cannot send async requests or create background
tasks.

Headless result notices name the processing session without suggesting continuation in a recipient chat; bot-backed recipient notices retain the continuation instruction. The bus derives this distinction from registered SubAgentConfig.transport on delivery, including retries and recovered records. Old records without this optional flag remain readable. Headless workers have no direct user chat. Users ask main to delegate synchronously/asynchronously, and async reports return through main to the original chat/topic. From a framework-created CLI turn an agent can run `python3 tools/agent_tools/create_agent.py --name NAME --transport headless` with optional `--provider` and `--model`. Choose Telegram/Matrix for a directly addressable sub bot.

## Shared vs isolated

Isolated per sub-agent:

- transport credentials and auth (Telegram token, Matrix account, or none for headless)
- workspace and files under `<main_home>/agents/<name>/`
- `sessions.json`, `named_sessions.json`, cron/webhook state

Shared across process:

- `InterAgentBus`
- `InternalAgentAPI`
- optional shared `TaskHub`
- central log file (`~/.ductor/logs/agent.log`)
- shared knowledge source (`~/.ductor/SHAREDMEMORY.md`)

## Inter-agent communication

### In-memory bus

- sync: waits for target response (`send`)
- async: returns task ID immediately (`send_async`)

Ordinary recipient processing and question fallback use `interagent_session_name(sender, chat_id, topic_id, lane)`: with source context, `ia.<sender-slug>.t<topic-or-0>.x<hash8>` (prefix truncated when needed); without a source chat and without a lane, legacy `ia-<sender>`; with a lane, `.l<lane>` is appended (see [lanes](#lanes-admission-and-the-time-contract)). The hash includes sender/chat/topic. Continue the exact reported name rather than guessing it.

Async send metadata currently supports:

- `new_session`
- `lane` (optional parallel session, see below)
- `summary`
- `chat_id` / `topic_id` / `transport` for result routing back to the original chat/topic (including Telegram private-chat topics)
- `result_origin` / `broadcast_ref` for cron broadcast correlation
- `reply_to` to deliver the final async result to a specific agent instead of the sender field
- `silent` to suppress the recipient-side "async task received" notification

Provider-switch safeguard:

- if recipient provider changed since the prior sender/chat/topic-scoped session, old session is ended and recreated
- provider-switch notice is surfaced back to sender side
- when `reply_to` is set, async result handler lookup uses that agent name instead of `sender`
- recipient receipt notices default off (`interagent_notify_received=false`); opting in on the recipient uses its own notification service
- `silent=true` or `reply_to` suppresses notices even when enabled; an explicit origin uses `notify_origin` with no main-bot or DM fallback

### Lanes, admission and the time contract

**Lane.** One sender can run several requests to the same recipient in parallel by giving each its own lane: `--lane NAME` on `ask_agent.py` / `ask_agent_async.py`, the `lane` field of `POST /interagent/send` and `/interagent/send_async`, or the `lane` argument of a direct bus call. A lane name is 1-12 characters of `a-z`, `0-9` and `-`, not starting with `-`; upper case is lowered; anything else is rejected before any session is created (HTTP 400 / `Error:` from the tool). An empty lane means "no lane".

- **Name.** A laned request always uses the scoped form `ia.<sender-slug>.t<topic>.x<hash8>.l<lane>` (without a source chat the source counts as chat 0 / topic 0). A request without a lane keeps the name it always had, byte for byte. Different lanes are different Named sessions and run in parallel; the same lane resumes the same session.
- **No inheritance.** A request sent from inside a laned turn without `--lane` uses the unlaned session, not its parent's lane. The lane is a validated session selector, not an authentication or agent selector.
- **Shared workspace.** Lanes of one sub share its workspace, its memory files and the provider authentication (Claude slots, Codex/Gemini homes) and rate limits. There is no per-lane isolation of those.

**Admission.** The bus owns one `SessionGateRegistry` that an agent restart never replaces. A request first reserves its place, later waits for its turn and then runs:

1. The reservation key is (receiving agent, stored chat, Named session name). Within one session reservations are served strictly first-in first-out, and a session never runs two requests at once (no two `--resume` of one session id).
2. After its session turn a request takes an execution slot: at most `interagent.max_parallel_sessions` (default 4; the receiving agent's effective value, see [`InteragentConfig`](../config.md#interagentconfig)) sessions of one agent run at the same time. A slot is never held while waiting for a session turn.
3. The place is reserved when the request is accepted: for async right after the durable record is written (a burst accepted in one tick keeps its order), for sync after the acceptance checks and before the first wait. A delegation question first tries the original session; only a fallback to a Named session reserves.
4. Everything that touches the session is inside the admission: Named lookup/creation, `--new` and provider switch, the re-read of the latest session id after admission, CLI execution including the stale-session retry, and saving the answer/session id. The place is released before the result callback; cancellation (while waiting for the turn, for a slot, running, or in the retry) returns the place exactly once.
5. The manual `@ia.<name>` resume of a stored inter-agent session (foreground, streaming and background) goes through the same gate. User-created Named sessions are unchanged: a running one still refuses a second request.

**Time contract.**

| What | Limit | Notes |
|---|---|---|
| Execution | 3600 s (async) | counted from admission (session turn + slot), not from acceptance |
| Waiting | `interagent.max_queue_wait_seconds` (default 3600 s) | queue wait + preparation (receipt notice, provenance capture) + wait for a restarting recipient; exceeded: the request ends without starting, with the error `wait time exceeded, not started` (distinct from an execution timeout) |
| Sync request | one 300 s budget | wait, question fallback, the 60 s question lock and execution all consume it |

The total time of an async request can therefore exceed one hour; that is normal under this contract. The receipt timestamp, `task.timestamp` and the failure ledger are unchanged; the wait is added only to the `Waited` field of the AI input (not to the original text or previews), and the result's `elapsed` keeps its meaning.

**Accepting without waiting.** `POST /interagent/send_async` answers at once with the task id, `queued` (true when other requests were ahead when the place was reserved) and `ahead` (how many were ahead; an estimate when the wait is for an execution slot). `ask_agent_async.py` prints `Queued: N earlier request(s) must finish first; ...`; a synchronous `ask_agent.py` that had to wait prints `[Queued: waited behind N earlier request(s) ...]`.

**Sync self-cycle and questions.** A synchronous request from an agent to its own current Named session (same agent, same session name) would wait for itself, so it is rejected at once with a clear error and nothing is reserved. Deeper cycles (A waits for B while B waits for A) are not detected; they end by timeout (sync: the 300 s budget). Questions are matched by the per-request delegation id, never by a global "current lane". The existing restrictions are unchanged: questions to main during a sync delegation are rejected, async reverse/onward calls from an inter-agent turn are rejected, and a headless sender cannot send async.

**Restart generation.** A waiting request keeps only the recipient's name and looks up the current ready stack at admission. While `stop_agent()` / a restart is in progress it waits as "restarting" and runs on the new stack once that is registered; only a permanently removed recipient ends with `recipient agent gone` (`RECIPIENT_GONE`). Because the gate belongs to the bus, the old stack's CLI cleanup and release finish before the new stack can run the same session id. Every restart path (`stop_agent`, the sub restart exit, the crash rebuild) first marks the old stack unavailable, stops its CLIs, then waits (`InterAgentBus.drain_agent`, up to 30 s) for the request handlers that were already running on it, whose last answer and session id they still save; only then is the old stack's Named registry closed (a later write of that generation is skipped with a warning, so it can never overwrite the sessions file of the new generation) and the new stack created, which reads the file afterwards. A handler that does not end within the 30 s loses its final session id write (logged); requests still waiting keep their place and run on the new stack.

**Ending.** Ending a Named session (`nsc:end`) stops both its `ns:<name>` and `interagent:<name>` processes, clears both abort marks and cancels the session's waiting reservations, so it is neither recreated later nor given a late answer; other lanes and recipients are untouched. The idle cleanup of inter-agent sessions skips sessions with waiting reservations. Inter-agent subprocesses carry the label `interagent:<session_name>` and, as before, a topic `/stop` also reaches them (the label is not in the preserved prefixes).

**Records.** Result JSON is unchanged except that its existing `session_name` field is now filled in every result (success, failure, execution timeout, wait timeout, cancel); the lane is the fifth part of that name. The accepted-request record gets the optional keys `lane` and `session_name`; records without them read as unlaned, and the previous product still reads records with them (keys are only added). The "interrupted by restart" notice, receipts, result headers and failure headers show the full session name and lane.

**Delegated-work block.** A laned request's `[Delegated work rules]` block gets one more line: the lane name, that other lanes may run concurrently in the same workspace, that repository changes belong in a git worktree dedicated to the lane, and that shared memory files are not edited unless instructed (put that content in the report). The block of an unlaned request is byte-identical to before; questions and answers get no block.

**Recommended pattern for parallel implementation.** One lane per work package, one git worktree per lane, a file split so that no two lanes edit the same file, and one owner (the delegating agent or one lane) for the final integration: merging, resolving conflicts and running the full tests. The software enforces none of this; the lane line above only reminds the sub.

### Questions during delegation

The receiving turn carries `DUCTOR_DELEGATION_ID`; the bus matches it against the active delegation registry (the actual task ID for async work), rather than trusting caller-supplied chat/topic provenance. CLI requests explicitly carry foreground/Named/background kind and Named name through `DUCTOR_SESSION_KIND`/`DUCTOR_SESSION_NAME` and both tools’ source payload fields into the server delegation snapshot. This is independent of the provider session ID: a new Named first turn remains Named, while a foreground first turn waits for the shared lock before checking its current resumable ID. Missing kind and ID cannot establish foreground provenance and use the dedicated fallback. Main’s synchronous delegation adds an instruction to put questions in the final report and not send question messages during the work. The server rejects verified sync-delegation questions to main before invoking its orchestrator or creating a session. Continue working; leave unknown values blank or state assumptions instead of inventing them.

During **asynchronous** delegation, a sub’s synchronous `ask_agent.py main` question resumes the **current active source chat/topic session**, including its current provider/model/effort and provider session ID. It uses the same topic-aware `MessageBus.lock_pool` as user turns and async result injection. The answer returns only to the sub; it is not posted to the user chat. API user sessions retain their legacy Telegram storage key while CLI origin remains `api`; both provenance capture and question routing use that same storage-key conversion. Startup shares the pool before the API starts.

The lock wait is capped at **60 seconds**, leaving up to 240 seconds of the existing 300-second synchronous bus deadline for the answer. The overall deadline still cancels a slow active-session or fallback execution; the wait is not an additional deadline. A Named source or unconfirmed background source immediately uses the existing separate Named Session; it never resumes an unrelated foreground conversation. If the foreground lock wait expires, the source session cannot be resumed, or delegated provenance cannot be identified, main also uses that fallback. That fallback keeps its source header: original chat/topic IDs and topic name when known, source session kind/Named name/key/provider ID, task/request ID, a safe preview up to 200 characters, and confirmed transcript/metadata paths with advice to consult them. Unknown fields are marked unconfirmed. Request previews are checked before truncation and again when building the header; recognized credential patterns (including Slack tokens, authentication URLs, Bearer/Basic authorization, GitHub tokens, AWS access-key IDs and JWTs) omit the entire preview. This pattern check is not a guarantee of detecting arbitrary secrets. A context-free sub-chat message keeps the earlier Named Session behavior.

Async reverse/onward calls during delegated turns remain rejected. Both agent tools send DUCTOR_DELEGATION_ID as delegation_id. The bus verifies mode against its active registry and the sender, rather than accepting a caller-supplied mode. A verified sync-delegation question to main fails immediately with the shared no-question instruction; ask_agent.py prints it unchanged and exits 1. An async send during that same sync delegation fails with the identical instruction; async/unknown modes retain the existing async-send refusal. Missing or unmatched IDs remain unknown and preserve prior routing. Session locking serializes active-session questions with user turns, result injection and other questions; timeout/cancellation releases only the acquired lock. Sources: delegation context (`ductor_bot/multiagent/delegation.py`), bus (`ductor_bot/multiagent/bus.py`), session injection (`ductor_bot/orchestrator/injection.py`).


### AI input receipt timestamps

Requests and allowed delegation questions carry the bus intake time only in
AgentRequest.received_context. A synchronous send captures its own receipt,
rather than inheriting the parent user turn's time. Async execution uses the
existing task.timestamp captured on acceptance, including time before its
recipient notification. Async response, failure and result-lost objects/durable records retain their first
received_at, including callback retry and record-based recovery. Legacy records
without it use the validated record updated_at as a stable approximation. Actual
session-lock acquisition, including FIFO predecessors, freezes elapsed wait before
wake/file preparation; it does not keep counting until CLI delivery.
The prefix is separate from stored prompt/previews and never enters the system prompt.
Synchronous answers (including delegation-question answers), refusals, errors and
timeouts also return received_at/received_context separately from text/error. The bus
captures response arrival in the calling agent's current timezone; the async API uses
the same public metadata method for acknowledgments and refusal/error replies.
ask_agent.py and ask_agent_async.py print the receipt line before the unchanged answer
or Error line. Legacy/network replies fall back to UTC receipt metadata. Error body,
exit 1, main-not-invoked/no-session behavior and chat silence are retained. Both
scripts remain Zone 2 framework files copied to every agent workspace on initialization.
Source-session routing and its 60-second wait cap are unchanged.
See [receipt formats](messenger.md#ai-input-receipt-context).

### Local HTTP bridge for tool scripts

`InternalAgentAPI` runs on `config.interagent_port` (default `8799`):

- binds to `127.0.0.1:<port>` inside the bot container

Inter-agent endpoints:

- `POST /interagent/send`
- `POST /interagent/send_async`
- `GET /interagent/agents`
- `GET /interagent/health`

Both send endpoints accept an optional `lane` (see [lanes](#lanes-admission-and-the-time-contract)).
`POST /interagent/send_async` accepts the same routing fields as `AsyncSendOptions`,
including `reply_to` and `silent`, and returns `queued` and `ahead` with the task id. It requires a registered result receiver
(`reply_to` or `from`) with an async result handler. A headless sender is
rejected even with `reply_to` set; use synchronous `ask_agent.py` instead.

Incoming delegated turns reject asynchronous onward calls (`ia_turn=true`),
including messenger sub-agents; verified sync-delegation questions to main are also rejected. During async delegation, synchronous questions to main remain allowed.
Async results must be injected into the caller session before delivery in the
original transport/chat/topic. Accepted requests and results are durably tracked
under `<agent_home>/interagent/{inflight,undelivered}`; failed delivery is retried,
and restart recovery reports interrupted work. See known limits (`docs/CHANGES.md`) for duplicate-delivery cases.

Task endpoints (shared hub):

- `POST /tasks/create`
- `POST /tasks/resume`
- `POST /tasks/ask_parent`
- `GET /tasks/list`
- `POST /tasks/cancel`
- `POST /tasks/delete`

Ownership checks apply for resume/cancel/delete when `from=<agent>` is present.

`POST /tasks/create` also accepts `priority=interactive|background|batch`.
Create/resume carry caller provenance (`transport`, `source_session_kind`,
`source_session_id`). Eligible running-task questions use that parent's matching
foreground conversation privately; questions needing the user or another fallback
are handed off to chat for an answer through resume after the run ends.
`/tasks/ask_parent` waits within a 270-second server budget (285-second HTTP timeout);
task tools need a command timeout of at least 300 seconds.
See [task questions and limits](tasks.md#questions-during-a-running-task).
This path is separate from inter-agent delegation questions and Named fallback.

## TaskHub integration

When enabled, supervisor wires each stack into shared `TaskHub`:

- per-agent CLI service
- per-agent paths (`tasks_dir`)
- task result callback
- public task question callback
- private `answer_task_question` callback for every stack, main and sub-agent
- agent primary chat ID mapping

Messaging agents can submit tasks while preserving owner routing. The HTTP
bridge rejects task creation by delegation-only headless agents.

Priority behavior is shared across agents:

- `interactive` bypasses the per-chat concurrency cap
- `background` is the default
- `batch` keeps the same cap semantics as `background` but documents low-urgency work explicitly

## Shared knowledge sync

`SharedKnowledgeSync` watches `~/.ductor/SHAREDMEMORY.md` and mirrors content into each agent's `MAINMEMORY.md` block.

Legacy HTML marker format is migrated to current block markers when rewritten.

## Chat and CLI commands

Main-agent chat commands:

- Telegram: `/agents`, `/agent_start <name>`, `/agent_stop <name>`, `/agent_restart <name>`
- Matrix: `!agents`, `!agent_start <name>`, `!agent_stop <name>`, `!agent_restart <name>` (`/` prefix also works)

CLI:

- `ductor agents`
- `ductor agents list`
- `ductor agents add <name>`
- `ductor agents remove <name>`

`ductor agents list` fetches live health from internal API when main bot is running.

Important CLI nuance:

- `ductor agents add <name>` currently prompts for Telegram token/user/group data only.
- Matrix sub-agents are supported by the runtime and merge logic, but are created via manual `agents.json` editing or the bundled `create_agent.py --transport matrix` tool script.
- Delegation-only sub-agents use `create_agent.py --name helper --transport headless`; no messenger credentials are needed.
- `create_agent.py` also takes `--persona`/`--persona-file` and `--behavior`/`--behavior-file`, written to the new agent's workspace `PERSONA.md`/`BEHAVIOR.md` before `agents.json` is updated (so the watcher's workspace init cannot seed the blank template first); existing files are kept. Before anything is written (and before `agents.json` is updated) the tool refuses a symlinked `agents/`, agent directory, `workspace/` or target file that resolves outside `<main_home>/agents/<name>/workspace`, including broken external symlinks. Main asks the user for these before creating a sub; a headless sub has no chat, so they must be passed at creation. Without `--persona` a bot-backed sub starts from the blank template and the first-run setup hint takes over.
- Every work request a sub receives carries the `[Delegated work rules]` block (see [orchestrator](orchestrator.md#injection-paths-injectionpy)).

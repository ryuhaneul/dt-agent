# cli/


## Files

- `types.py`: `AgentRequest`, `AgentResponse`, `CLIResponse`
- `base.py`: `BaseCLI`, `CLIConfig`, subprocess helpers
- `service.py`: `CLIService` gateway for orchestrator
- `executor.py`: shared subprocess lifecycle helpers for provider wrappers
- `timeout_controller.py`: configurable timeout warnings + activity-based extension controller
- `model_cache.py`: shared base classes for provider model-cache persistence and refresh observers
- `claude_provider.py`: Claude subprocess wrapper
- `codex_provider.py`: Codex subprocess wrapper
- `gemini_provider.py`: Gemini subprocess wrapper
- `grok_provider.py`, `grok_events.py`: Grok Build wrapper and stream parser
- `claude_accounts.py`, `slot_rotation.py`: Claude slot authentication/cooldown rotation
- `claude_slots.py`: shared slot overview, priority changes and cooldown clearing; JSON CLI used by host scripts
- `claude_features.py`: one-time `claude --help` detection of `--system-prompt-snapshot`
- `_log_redact.py`: command/env-assignment redaction helpers
- `stream_events.py`: normalized stream events + Claude stream parser
- `codex_events.py`: Codex JSONL parser
- `gemini_events.py`: Gemini NDJSON + JSON parser
- `coalescer.py`: streaming text coalescing buffer used by bot streaming dispatch
- `gemini_utils.py`: Gemini CLI discovery, trusted folder, model discovery helpers
- `codex_discovery.py`: Codex model discovery via `codex app-server` JSON-RPC
- `process_registry.py`: subprocess tracking/abort/kill
- `auth.py`: provider auth detection
- `param_resolver.py`: task override resolution for cron/webhook one-shot runs
- `codex_cache.py`, `codex_cache_observer.py`: Codex model cache + observer
- `gemini_cache.py`, `gemini_cache_observer.py`: Gemini model cache + observer
- `grok_cache.py`, `grok_cache_observer.py`, `grok_discovery.py`: Grok model cache, observer and discovery

## Execution path

1. Orchestrator builds `AgentRequest`.
2. `CLIService._make_cli()` resolves model/provider.
3. `CLIServiceConfig` injects provider-specific global CLI args.
4. `create_cli()` selects provider wrapper.
5. provider executes subprocess and returns `CLIResponse`.
6. service converts to `AgentResponse`.

AI-bound user and selected bus inputs carry receipt/wait metadata in
AgentRequest.received_context or queue_context, separately from prompt.
CLIService sends cli_prompt in non-streaming, streaming and fallback calls;
display/state/log previews retain prompt. A queued-user block includes its own
receipt and takes precedence over a separate receipt line. Neither field
changes the system prompt. See [receipt context](messenger.md#ai-input-receipt-context)
for formats, timestamp sources and exclusions. Lock-wait metadata is frozen on acquisition,
so later preparation does not increase it. Agent tools additionally print response receipt
metadata as a separate first line of stdout for success or stderr for failure, before the
unchanged answer/Error body. Server replies use the calling agent's current timezone;
legacy/network replies use UTC. The API keeps received_at/received_context out of text/error.
These tool return lines are AI input and are not posted as user chat messages; the synchronous-question refusal
exit 1 and server-side no-invocation/no-session behavior are unchanged.

Environment variables injected into CLI subprocesses:

- `DUCTOR_CHAT_ID`
- `DUCTOR_TOPIC_ID` (when set)
- `DUCTOR_TRANSPORT` (active transport identifier, e.g. `"tg"`, `"mx"`)
- `DUCTOR_AGENT_NAME`
- `DUCTOR_INTERAGENT_PORT`
- `DUCTOR_HOME`
- `DUCTOR_SHARED_MEMORY_PATH`
- `DUCTOR_TRANSCRIBE_COMMAND` / `DUCTOR_VIDEO_TRANSCRIBE_COMMAND` when external transcription hooks are configured

Provider CLI subprocesses also receive `DUCTOR_AGENT_ROLE` (`main`
or `sub`). They execute in the bot container. Framework identity/routing
values override inherited, home-dotenv and provider-specific extras.
`DUCTOR_ORIGIN_CHAT_ID`, `DUCTOR_ORIGIN_TOPIC_ID`, `DUCTOR_ORIGIN_TRANSPORT`
retain the requesting chat/topic for IA turns; `DUCTOR_IA_TURN=1` marks such a
turn. Unset optional route fields are removed from the child environment.

## Main-chat CLI parameters

Configured globally in `config.json`:

- `cli_parameters.claude`
- `cli_parameters.codex`
- `cli_parameters.gemini`
- `cli_parameters.grok`

`CLIService` forwards them per provider.

## Task execution resolution (`param_resolver.py`)

Used by cron and webhook `cron_task` runs.

- input: `TaskOverrides(provider, model, reasoning_effort, cli_parameters)`
- output: immutable `TaskExecutionConfig`
- supported one-shot providers: `claude`, `codex`, `gemini`, `grok`
- validation:
  - Claude model in `CLAUDE_MODELS`
  - Codex model validated against `CodexModelCache`
  - Gemini aliases are accepted; other IDs must match the discovered registry when populated, or `gemini-*` / `auto-gemini-*` patterns when it is empty
  - Grok model validated against known IDs or the `grok-*` prefix
- Claude and Grok effort use their static support lists; Codex effort is validated against model-cache support; Gemini drops effort. Unsupported explicit overrides for Claude/Grok or cached Codex models raise `DuctorError`.
- task `cli_parameters` are appended after the global provider-specific args

## Streaming model

Normalized events in `stream_events.py` include:

- `AssistantTextDelta`
- `ToolUseEvent`
- `ToolResultEvent`
- `ThinkingEvent`
- `SystemStatusEvent`
- `CompactBoundaryEvent`
- `SystemInitEvent`
- `ResultEvent`

`CLIService.execute_streaming()` behavior:

- routes deltas/events to callbacks,
- forwards `CompactBoundaryEvent` through `on_compact_boundary` so the orchestrator can trigger silent memory-flush follow-up work,
- checks chat-wide and topic-specific abort markers on each event,
- if stream fails or lacks final result event:
  - aborted -> empty result,
  - non-error with accumulated text -> use accumulated text,
  - else retry non-streaming and mark `stream_fallback=True`.

Timeout behavior in current production paths:

- provider wrappers accept both `timeout_seconds` and `timeout_controller`, and pass both into executor helpers.
- `SubprocessSpec.timeout_controller` is used when orchestrator flows build a controller (`flows._make_timeout_controller`), including foreground, foreground named-session/follow-up and heartbeat execution.
- when no controller is supplied, executor falls back to plain `asyncio.timeout(...)`.
- timeout-only paths include cron/webhook one-shot runs, background `/session` workers and inter-agent turns. Task-result/task-question/session-injection turns also use timeout-only execution.

Status-callback nuance:

- `TimeoutController` warning/extension callbacks are not currently wired to emit `SystemStatusEvent`s, so UI labels like `timeout_warning`/`timeout_extended` depend on future callback wiring.

`messenger/telegram/message_dispatch.py` wraps delta delivery with `StreamCoalescer` (`coalescer.py`) so Telegram edits flush at readable boundaries (paragraph/sentence/idle/full flush).

Session recovery is orchestrator-managed (`flows._recover_session`), not CLIService-managed.

Recovery triggers handled in orchestrator flows:

- SIGKILL termination (`returncode == -SIGKILL`)
- invalid resumed session (`"invalid session"` / `"session not found"` from provider CLI)

## Provider specifics

### Claude

- non-streaming uses `--output-format json`
- streaming uses `--output-format stream-json`
- respects `--max-turns`, `--max-budget-usd`, `--effort`, session resume/continue
- configured `claude_accounts` enable rate-limit rotation with bot-home cooldowns shared across project and title calls; account order and user timezone changes apply on reload
- the main Telegram bot exposes `/accounts` for authenticated-slot status, ordering and cooldown clearing. `logout.sh claude --prefer SLOT`, `--priority A,B,...` and `--clear-cooldown SLOT|all` delegate to the same module through the running container; short names resolve to `claude-NAME` (`default` resolves to `claude`). Invalid slots or incomplete/duplicate priority lists leave files unchanged
- `/accounts` also shows a saved Claude usage line per slot, with capture and reset times
- `login.sh` without a provider continues through all providers and returns status 1 if any authentication or slot setup fails; an all-success summary returns 0. Provider-specific login retains its authentication-based exit status.
- `logout.sh claude --list` reads authentication, configured priority and remaining cooldowns on the host without requiring a container. Priority changes reach main within seconds and sub-agents at their next start; cooldown clearing applies to all on the next call
- config and cooldown read-modify-write transactions lock companion files and reload the latest data before changing it. Login registration defaults to Y (empty input or non-TTY registers; n/no declines), merging newly authenticated slots into the latest list
- the system and append-system prompts are always written to private (0600) temporary files and passed as `--system-prompt-file` / `--append-system-prompt-file` (regardless of size); the user prompt is sent over stdin on every OS, with no `--` separator or prompt argument. None of these texts appear in the process list (`ps`), and the temp files are removed after each attempt, including slot-rotation retries and failures
- one-shot cron/webhook-style runs (`cron/execution.py`) follow the same rule: the Claude and Codex task prompts go over stdin (Codex via `-`) and the Grok prompt through a 0600 `--prompt-file`, so none of them appears in argv

### Account usage

Telegram `/usage` is read-only and available on main and sub-agent bots under their existing allowlists. Claude usage is the last `rate_limit_event` received during normal CLI runs, including streaming, non-streaming and cron, webhook and background one-shot calls; it is not actively queried by this command. `cli/claude_usage.py` merges slot snapshots in `_claude-usage.json` beside the shared cooldown file, with a companion lock and atomic private writes. Buffered non-streaming and one-shot events use the process start time; streaming events use their line arrival time. Window values and slot status retain the newest capture time when runs finish out of order. Missing values are shown as unavailable, with capture times on stored values.

Codex uses `cli/codex_usage.py` to query `codex app-server` through the current `CODEX_HOME` login. Each command or refresh reads account limits with a 15-second deadline, sharing an in-flight query, reusing completed results for ten seconds, and terminating its launcher and descendant server processes afterward. The response uses the non-null primary and secondary windows; results and account identifiers are not stored. Missing login, timeouts and malformed responses are displayed separately.

The fixed-width view shows ten-cell bars and server percentages, including values above 100%. Reset times use `user_timezone`; a passed reset is labeled as reset rather than displayed as a new server value. A Codex window without a reset time omits that time. `/accounts` remains main-only and adds the same saved Claude values without bars.

### Claude transcript retention

The standard image installs `/etc/claude-code/managed-settings.d/10-dt-agent-retention.json` with `cleanupPeriodDays: 36500`, covering every in-container Claude invocation regardless of slot (bot calls, streaming, one-shot tasks, sub-agents, login and direct execution). Higher-priority managed sources such as organization server policies may supersede this local policy; administrators can deliberately override the value with a later-named drop-in in the same directory ([how Claude Code combines managed sources](https://code.claude.com/docs/en/managed-settings#how-claude-code-combines-managed-sources)).

### Claude system prompt snapshot

Claude Code records its system prompt at session start and at compaction (`--system-prompt-snapshot`, default **on**), so a resumed turn keeps the old text even when `MAINMEMORY.md`, `PERSONA.md` or `BEHAVIOR.md` changed. DT Agent therefore runs Claude turns with `--system-prompt-snapshot off`, which renders the system prompt anew on every request, and assembles the appended parts on every turn:

- **Setting:** `claude_system_prompt_snapshot` (`"off"` default, `"on"`; restart-required; subs inherit and may override, see [config](../config.md#agentconfig-ductor_botconfigpy)). `"on"` omits the flag and goes back to the old policy (MAINMEMORY and roster on new sessions only), without a product rollback.
- **Which turns:** decided on the **effective provider** of the request (a request override wins over the configured one, `CLIService.resolve_provider`), only when it is Claude. The flag is added in `CLIService._make_cli` through the provider parameters (a user-given `--system-prompt-snapshot` parameter is not doubled). Foreground and result-injection turns also assemble MAINMEMORY + agent roster + the `append_system_prompt_files` blocks every turn; named sessions keep their policy (file blocks only) and share the assembling helper; inter-agent turns and background tasks only get the flag; cron/webhook one-shot runs and topic title generation are unchanged.
- **Detection:** the first Claude turn runs `claude --help` once per process (no model call, 5 s limit) and caches whether the flag is listed. If the CLI lacks the flag, or the probe fails (missing binary, non-zero exit, timeout), the flag is omitted, one warning is logged, and the assembling falls back to the old new-session policy so that nothing is dropped.
- **Other providers are unchanged:** Codex receives the context after the user prompt, Gemini through its system temporary file, Grok through `--rules`; none of them gets the flag, and no prompt text appears in the process list (files/stdin as before).
- **Cost:** with the flag off the request is rendered every time; an unchanged text keeps the prompt-cache hit, and only the first request after the text changed pays one cache rewrite. Real usage figures are not asserted here.

### Codex

- fresh runs use `codex exec --json --color never --skip-git-repo-check`
- resumed runs use `codex exec resume [--json] -- <session_id>` and do not go through the same `--color never --skip-git-repo-check` path
- sandbox/approval flag selection from `permission_mode`
- reasoning effort via `-c model_reasoning_effort=...`
- `continue_session=True` is ignored for Codex

### Gemini

- command via `gemini` (or `node <index.js>` when resolved)
- non-streaming `--output-format json`, streaming `--output-format stream-json`
- permission bypass maps to `--approval-mode yolo`
- always includes `--include-directories .`
- trusts workspace path in `~/.gemini/trustedFolders.json`
- may inject `GEMINI_API_KEY` from the DT Agent config when Gemini settings indicate API-key mode and no env key is set

### Grok

- `grok --output-format json` or `stream-json`, with model and permission options
- bypass uses `--always-approve`; effort uses `--reasoning-effort`
- supports `--resume` and `--continue`; the user prompt always goes through a private (0600) temporary `--prompt-file`, never argv, so it is not visible in the process list. `--system-prompt-override` and `--rules` have no file form in the Grok CLI and still travel as argv values

Foreground user conversations recheck the **resolved session provider** authentication immediately before CLI execution. Text, media and prompt-button turns, including named-session foreground follow-ups, return localized host-repository `./scripts/login.sh <provider>` instructions and do not run the CLI when unauthenticated. Authentication changes are reread on the next user turn; Gemini can use its configured API key. Telegram, Matrix, Slack and direct API use these shared orchestrator flows. Inter-agent, cron and webhook task execution paths are unchanged by this foreground check.

## Auth detection (`auth.py`)

Statuses: `AUTHENTICATED`, `INSTALLED`, `NOT_FOUND`.

- Claude: `~/.claude/.credentials.json`
- Claude first scans other `claude`/`claude-*` slots under `$DUCTOR_HOME/.cli-auth/` for `.credentials.json` or `credentials.json`; fallback paths then use `ANTHROPIC_API_KEY`, then `claude auth status`
- Codex: `$CODEX_HOME/auth.json`
- Codex fallback paths: `OPENAI_API_KEY`; install markers: `version.json` or `config.toml`
- Gemini:
  - CLI presence (`find_gemini_cli`)
  - OAuth creds (`~/.gemini/oauth_creds.json`)
  - env/.env/API-key/Vertex markers
  - `settings.json` selected auth mode
  - optional fallback to `~/.ductor/config/config.json` `gemini_api_key`
- after changing a Gemini key in the host home `.env`, run `docker compose up -d` to recreate the container before retrying login

- Grok: `$GROK_HOME/auth.json` (default `~/.grok`), then `XAI_API_KEY`, then authenticated `grok models`; binary/config markers indicate installation

## Model caches

### Codex cache

- file: `~/.ductor/config/codex_models.json`
- discovery source: `discover_codex_models()` (`codex_discovery.py`) via `codex app-server` (`initialize` + `model/list`)
- loaded on startup with force refresh
- hourly refresh loop

### Gemini cache

- file: `~/.ductor/config/gemini_models.json`
- startup forces discovery; hourly checks can reuse a fresh cache (24-hour age threshold)
- refresh callback updates runtime Gemini model registry (`set_gemini_models`)

### Grok cache

- file: `~/.ductor/config/grok_models.json`
- discovery source: `grok_discovery.py`
- startup forces discovery; hourly checks reuse fresh cache when possible
- refresh callback updates the runtime Grok model registry

Caches are started only for installed providers. Static Claude IDs remain
available to directives/API metadata.

## Process registry

`ProcessRegistry` provides:

- registration/unregistration by chat with optional `topic_id` tracking
- `has_active(chat_id, topic_id=None)`: when `topic_id` is given, only processes for that specific topic are considered active; otherwise any process for the chat qualifies
- abort markers (`was_aborted`, `clear_abort`)
- `kill_all(chat_id)`
- `kill_for_task(task_id)` for background-task cancellation
- stale wall-clock cleanup (`kill_stale(max_age_seconds)`, run by the heartbeat with `cli_timeout * 2`)
  - a process registers the longest its request lets it run (`limit_seconds`: the request timeout, or a timeout controller's timeout plus all its extensions)
  - labels `task:`, `ns:`, `interagent:` and `cron:` that registered a limit are stale after twice that limit; every other label (the foreground turn `main`, `dm-topic-title`, memory jobs) and a limit-less one use the default age
  - Gemini registers its own process and passes the same limit; whatever provider ran the call, `ProcessRegistry` remembers the (chat, label) of a stale kill and `CLIService.execute` / `execute_streaming` set `AgentResponse.stale_killed` from it (cleared when a run of that label starts)
  - the process is marked `stale_killed` before it is killed; the executor copies the mark to `CLIResponse.stale_killed` (or to the final `ResultEvent` when streaming), `CLIService` copies it to `AgentResponse.stale_killed`, and `TaskHub` reports the task as `failed`

`TaskHub` uses `kill_for_task(task_id)` before cancelling the asyncio task so streaming subprocess pipes unblock cleanly.

Windows uses process-tree termination (`taskkill /F /T`) to avoid orphaned child processes.

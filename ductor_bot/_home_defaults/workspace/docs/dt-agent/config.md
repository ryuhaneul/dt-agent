# Configuration

Runtime config file: `/ductor/config/config.json` inside the bot container (host `<resolved host home>/config/config.json`). The runtime default for `ductor_home` is `/ductor`; the example and onboarding output also use `/ductor`. Keep the container path in this field; select the host mount with `DUCTOR_HOME` in repository `.env`. Login/logout/update read that key automatically unless environment `DUCTOR_HOME` overrides it; absent both, they use `~/.ductor`.

Seed source: `<repo>/config.example.json` (source checkout) or packaged fallback `ductor_bot/_config_example.json` (installed mode).

## Config Creation

Run `scripts/onboard.sh` from the repository, then `docker compose up -d --build`. First installation starts from the example; reruns use the saved config as the basis and add missing defaults recursively. The script prompts only when both stdin and stdout are TTYs and `--no-prompt` is absent. First noninteractive installs require a token file and allowed user IDs; missing required inputs exit 2 before writes, while reruns can reuse saved credentials and IDs. Schema defaults below can differ from onboarding/example values. Onboarding timezone precedence is `--tz` → `ONBOARD_TZ` → saved `user_timezone` on reruns → detected host timezone → `UTC`; system detection tries a ZoneInfo-valid `TZ`, `/etc/timezone`, then the `/etc/localtime` symlink path after `zoneinfo/`; the interactive prompt displays the selected default. Language precedence is `--language` → `ONBOARD_LANGUAGE` → saved `language` on reruns → supported host locale → `en`. See [installation](installation.md).

## Load & Merge Behavior

Config is merged in two places:

1. `ductor_bot/__main__.py::load_config()`
   - creates config on first start (copy from `config.example.json` or Pydantic defaults),
   - deep-merges runtime file with `AgentConfig` defaults,
   - writes back when keys were added or transport/Gemini-key normalization changed the file.
2. `ductor_bot/workspace/init.py::_smart_merge_config()`
   - shallow merge `{**defaults, **existing}` with `config.example.json`,
   - preserves existing user top-level keys,
   - fills missing top-level keys from `config.example.json`.

Normalization detail:

- onboarding and runtime config load normalize `gemini_api_key` default to string `"null"` in persisted JSON for backward compatibility.
- `AgentConfig` validator converts null-like text (`""`, `"null"`, `"none"`) to `None` at runtime.

Runtime edits persisted through config helpers include `/model` changes (model/provider/reasoning), webhook token auto-generation, and API token auto-generation.

API config persistence note:

- `load_config()` intentionally does not auto-add the `api` block during default deep-merge (beta gating).
- To enable the optional API, add an `api` block to `config.json` and ensure the image contains PyNaCl. An empty token is generated on API startup.

Onboarding reruns use the saved config as their basis. With --force, supplied values are updated, other settings remain unchanged, and only missing example keys are added recursively. Groups change only through --allowed-groups/ONBOARD_ALLOWED_GROUPS; Enter keeps saved question defaults, including the hidden Telegram token. The env-file replacement policy is unchanged. Delete DUCTOR_HOME/config/config.json and rerun to reset configuration.

Onboarding language precedence is `--language` → `ONBOARD_LANGUAGE` → saved language on reruns → the first nonempty server locale (`LC_ALL`, `LC_MESSAGES`, `LANG`) → `en`. It extracts the language part, for example `ko_KR.UTF-8` → `ko`; an unsupported server locale falls back to `en`. Supported bundled codes are `de`, `en`, `es`, `fr`, `id`, `ko`, `nl`, `pt`, `ru`. Unsupported explicit options/environment/question answers fail before writes. Question 5 displays the saved or detected default; Enter retains it; the decided language also sets the onboarding questions, errors, completion notice and the `onboard.sh` preflight messages (commands, options, codes and paths stay untranslated) as well as command-line errors and the `--help` text, and answering question 5 changes the texts that follow. `config.language` controls bot UI messages. Existing config is retained without `--force`, and the language line of the completion notice reports the saved value. Option names are read in full: an abbreviation such as `--lang=ko` is an unknown-argument error in the decided language (exit 2, nothing written).

## External API Secrets and Environment

The host home chosen in repository `.env` (or overridden by nonempty environment `DUCTOR_HOME`, default `~/.ductor`) maps to `/ductor` in the container.
Its private `.env` holds the provider keys the CLIs read when they start; credentials that
tools take while they run are kept in `secrets.env` in the same directory (see
[Tool credentials](#tool-credentials-secretsenv)). The repository
`.env` selects Compose settings such as `DUCTOR_HOME` and `DUCTOR_EXTRAS`;
the supported feature extras are `matrix`, `slack`, and `api`. openssh-client
(`ssh`, `scp`) is always installed; paramiko is absent. The removed `ssh`
extra fails the build, and retired onboarding SSH options/environment inputs
fail with `ssh extra was removed; openssh-client is included in the image by default`
(Dockerfile (`docker/ductor-bot/Dockerfile`), onboarding (`scripts/onboard.py`)).
PyNaCl stays in the `api` extra for API encryption.
The home `.env.shared` supplies the browser token and timezone to both services.
Onboarding uses a private umask, creates `.cli-auth` with mode `0700`, and
writes generated files with mode `0600`.

Standard dotenv syntax for the runtime loader of the home `.env`:

```env
ANTHROPIC_API_KEY=SET_IN_PRIVATE_ENV
OPENAI_API_KEY=SET_IN_PRIVATE_ENV
export MY_VAR="quoted value"
```

Compose also loads the whole file into the container environment, so keep credentials that
only tools need out of it (below).

`cli/executor.py::build_subprocess_env()` starts with the process environment,
loads the current agent home's `.env` to fill missing keys, then applies explicit
provider overrides and framework routing/identity values. It does not load an
untrusted project directory's `.env`. Gemini's configured API key is a fallback
only when no usable environment key exists and API-key auth mode is selected.
The runtime dotenv parser accepts quotes and `export`, but does not expand `$VAR`.

Home-file edits invalidate the runtime mtime cache on the next CLI invocation.
They cannot replace values already inherited from Compose. For edits to files
loaded by Compose (`.env` or `.env.shared`) or to repository Compose variables,
recreate the services with `docker compose up -d` (add `--build` when build
settings changed); a simple container restart keeps the old environment.
Sub-agents use their own home file but also inherit the container environment.

### Tool credentials: `secrets.env`

Credentials that a tool needs while it runs (a repository token, a form password, a
one-time-code seed) belong in `secrets.env` at the root of the main home, not in `.env`.
Onboarding creates it empty with mode `0600` when it is missing and never changes an existing
one. Sub-agents use the main home's file.

Values are not injected as environment variables, but an agent running as the same user can read the file; this protects against accidental exposure only.

- The file holds plain `KEY=VALUE` lines (`#` comments; no `export`). `KEY=$OTHER_KEY` takes the
  value of `OTHER_KEY` from the same file.
- It is not injected into CLI runs. The framework gives every run the path in
  `DUCTOR_SECRETS_FILE` unless the process environment, the home `.env` or the caller already
  sets it. A tool asks for exactly the keys it uses:

  ```python
  from _secrets import load_secrets  # tools/_secrets.py

  load_secrets("MY_API_KEY")
  ```

- A value that is already in the environment wins over the file.
- Never `cat`, print or log the file; look at key names only.

Sources: environment builder (`ductor_bot/cli/executor.py`),
dotenv loader (`ductor_bot/infra/env_secrets.py`),
Gemini provider (`ductor_bot/cli/gemini_provider.py`),
onboarding (`scripts/onboard.py`), Compose (`compose.yaml`).

Host paths written as `~/.ductor` below mean the default home; substitute the configured `DUCTOR_HOME`. For sub-agents, use `<main_home>/agents/<name>`. CLI-auth skill paths are mapped into `.cli-auth` by the container entrypoint.

## `AgentConfig` (`ductor_bot/config.py`)

| Field | Type | Default | Notes |
|---|---|---|---|
| `log_level` | `str` | `"INFO"` | Applied at startup unless CLI `--verbose` is used |
| `provider` | `str` | `"claude"` | Default provider |
| `model` | `str` | `"opus"` | Default model ID |
| `ductor_home` | `str` | `"/ductor"` | Runtime home inside the container |
| `idle_timeout_minutes` | `int` | `1440` | Session freshness idle timeout (`0` disables idle expiry) |
| `session_age_warning_hours` | `int` | `12` | Adds `/new` reminder after threshold (every 10 messages) |
| `daily_reset_hour` | `int` | `4` | Daily reset boundary hour in `user_timezone` |
| `daily_reset_enabled` | `bool` | `false` | Enables daily session reset checks |
| `user_timezone` | `str` | `""` | IANA timezone used by cron/heartbeat/cleanup/session reset and queued-message send-time advisories |
| `language` | `str` | `"en"` | Bot UI language for commands, status and chat system messages; schema default en, onboarding selects locale/explicit code and also speaks it |
| `max_budget_usd` | `float \| None` | `None` | Passed to Claude CLI |
| `max_turns` | `int \| None` | `None` | Passed to Claude CLI |
| `max_session_messages` | `int \| None` | `None` | Session rollover limit |
| `permission_mode` | `str` | `"bypassPermissions"` | Provider sandbox/approval mode |
| `cli_timeout` | `float` | `1800.0` | Legacy/global timeout. Still used by cron/webhook `cron_task`, inter-agent turns, stale-process heartbeat cleanup, and as fallback for unknown timeout paths |
| `reasoning_effort` | `str` | `"medium"` | Default reasoning level for providers that support effort |
| `file_access` | `str` | `"all"` | File access scope (`all`, `home`, `workspace`) for file sends and API `GET /files`; unknown values fall back to workspace-only |
| `gemini_api_key` | `str \| None` | `None` | Config fallback key injected for Gemini API-key mode |
| `transport` | `str` | `"telegram"` | Main messaging transport: `"telegram"`, `"matrix"`, or `"slack"`; `"headless"` is sub-agent only |
| `transports` | `list[str]` | `[]` | List of transports to run in parallel (e.g. `["telegram", "matrix"]`). When empty, falls back to single `transport` value. |
| `telegram_token` | `str` | `""` | Telegram bot token (required when `transport=telegram`). Onboarding trims surrounding whitespace and rejects internal whitespace or a token outside ASCII digits:letters/digits/underscore/hyphen, without echoing the value. |
| `telegram_dm_topic_auto_name` | `bool` | `true` | Telegram-only once-per-topic naming of confirmed implicit DM topics from received user messages, alongside the conversation; main and bot-backed subs. Subject-only output; a null or rejected model title names the topic from the start of the message at once, while a plain greeting or a failed rename shares up to three message opportunities. Hot-reloaded; no onboarding question. User-edited/custom, group and unknown-origin topics are excluded. |
| `allowed_user_ids` | `list[int]` | `[]` | Telegram user allowlist (applies in both private and group chats) |
| `allowed_group_ids` | `list[int]` | `[]` | Telegram group allowlist (which groups the bot can operate in; default `[]` = no groups, fail-closed). In groups, both the group and the user must be allowlisted |
| `allowed_channel_ids` | `list[int]` | `[]` | Telegram channel allowlist for join/audit behavior; unauthorized channels are auto-left |
| `group_mention_only` | `bool` | `false` | Mention/reply gating in group rooms. Telegram: filter only (no auth bypass). Matrix: in non-DM rooms this bypasses `allowed_users` and uses room + mention/reply as gate |
| `matrix` | `MatrixConfig` | see below | Matrix homeserver connection (required when `transport=matrix`) |
| `slack` | `SlackConfig` | empty tokens/lists | Socket Mode `bot_token`, `app_token`, `allowed_channels`, `allowed_users`; requires the Slack image extra |
| `append_system_prompt_files` | `list[str]` | `["PERSONA.md", "BEHAVIOR.md"]` | Additional system prompt files; a `PERSONA.md` that still contains its unset marker (`<!-- PERSONA_UNSET -->`) is skipped |
| `persona_setup_prompt` | `bool` | `true` | While `PERSONA.md` carries the unset marker, add a first-run setup hint to user turns (first turn of a session, then every 6th message); hot-reloadable. Off = never ask |
| `project_roots` | `dict[str, str]` | `{}` | Topic name, topic ID, or `chat_id:topic_id` mapped to a project path |
| `claude_accounts` | `list[str]` | `[]` | Claude authentication slots |
| `cron_delivery_retry` | `CronDeliveryRetryConfig` | disabled, interval `300`, max attempts `12` | Retry preserved delivery without rerunning the agent |
| `cron_preflight` | `CronPreflightConfig` | disabled, timeout `15.0`, marker `HEARTBEAT_OK` | Task-local deterministic preflight gate |
| `streaming` | `StreamingConfig` | see below | Streaming tuning |
| `heartbeat` | `HeartbeatConfig` | see below | Background heartbeat config |
| `memory_flush` | `MemoryFlushConfig` | see below | Silent pre-compaction memory flush after streaming compact boundaries |
| `memory_reflection` | `MemoryReflectionConfig` | see below | Optional periodic memory reflection hook |
| `memory_lint` | `MemoryLintConfig` | see below | Periodic framework lint of `memory_system/` (`mode` structure/full/off); hot-reloadable |
| `memory_compaction` | `MemoryCompactionConfig` | see below | LLM-driven `MAINMEMORY.md` compaction policy |
| `cleanup` | `CleanupConfig` | see below | Daily file-retention cleanup |
| `webhooks` | `WebhookConfig` | see below | Webhook HTTP server config |
| `api` | `ApiConfig` | see below | Direct WebSocket API server config |
| `cli_parameters` | `CLIParametersConfig` | see below | Provider-specific extra CLI flags |
| `image` | `ImageConfig` | see below | Incoming image processing settings |
| `timeouts` | `TimeoutConfig` | see below | Path-specific timeout policy (`normal`, `background`, `subagent`) |
| `tasks` | `TasksConfig` | see below | Delegated background task system (`TaskHub`) |
| `scene` | `SceneConfig` | see below | Scene indicators and technical footer |
| `notifications` | `NotificationsConfig` | see below | Targeted startup notification routing |
| `transcription` | `TranscriptionConfig` | see below | External audio/video transcription command hooks |
| `skills` | `SkillsConfig` | see below | Cross-tool skill sync toggles (global + per-provider) |
| `interagent_port` | `int` | `8799` | Port for internal localhost API (`InternalAgentAPI`) |
| `interagent_notify_received` | `bool` | `false` | Opt in to async task receipt notices on the receiving agent; `--silent` still suppresses them |
| `interagent` | `InteragentConfig` | see below | Limits of inter-agent request handling (parallel sessions, queue wait); restart-required |
| `claude_system_prompt_snapshot` | `"off"` or `"on"` | `"off"` | `"off"`: Claude renders its system prompt anew every turn (`--system-prompt-snapshot off`) and MAINMEMORY, roster and file blocks are assembled every turn; `"on"`: no flag, MAINMEMORY only on new sessions. Needs a Claude CLI that supports the flag (else omitted with one warning); restart-required; see [Claude system prompt snapshot](modules/cli.md#claude-system-prompt-snapshot) |

### Multi-transport behavior

When `transports` is empty (default), the single `transport` value
is used. When `transports` contains multiple entries (e.g.
`["telegram", "matrix"]`), `MultiBotAdapter` starts all listed
transports in parallel and `transport` is auto-set to the first
entry. A model validator normalizes both fields at load time so
they stay consistent.

## `InteragentConfig`

Limits of inter-agent request handling. A sub inherits main's values and may override single keys; see [lanes and the time contract](modules/multiagent.md#lanes-admission-and-the-time-contract).

| Key | Type | Default | Description |
|---|---|---|---|
| `max_parallel_sessions` | `int` (>= 1) | `4` | How many inter-agent sessions (lanes) of one **receiving** agent run at the same time; further sessions wait for a slot |
| `max_queue_wait_seconds` | `float` (> 0) | `3600` | Longest wait of one request (queue + preparation + wait for a restarting recipient) before it ends without starting; the execution limit (3600 s for async) counts separately from admission. Sync requests keep their own 300 s budget |

A changed value is applied after a restart (the running gate is not resized by hot reload; a (re)started agent registers its value on the same slot pool).

## `MatrixConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `homeserver` | `str` | `""` | Matrix homeserver URL (e.g. `https://matrix.org`) |
| `user_id` | `str` | `""` | Bot user ID (e.g. `@ductor:matrix.org`) |
| `password` | `str` | `""` | Password for initial login |
| `access_token` | `str` | `""` | Optional manual restore source; runtime normally persists credentials in the Matrix store |
| `device_id` | `str` | `""` | Optional manual restore source paired with `access_token` |
| `allowed_rooms` | `list[str]` | `[]` | Room IDs or aliases the bot may operate in |
| `allowed_users` | `list[str]` | `[]` | Matrix user IDs allowed to interact |
| `store_path` | `str` | `"matrix_store"` | Credential/room-mapping store directory, relative to the current agent home |

Notes:

- first successful login persists credentials to `<agent_home>/<store_path>/credentials.json` (mode `0o600`), not back into `config.json`
- when `access_token` and `device_id` are explicitly present in `config.json`, runtime restores from them and also mirrors them into the credentials store
- `store_path` must stay inside the agent home; absolute paths, `..`, and symlink components are rejected.
- The current client is constructed as `AsyncClient(homeserver, user_id)` without E2EE configuration; encrypted-room support is not implemented by this deployment.
- Empty `allowed_rooms` or `allowed_users` lists impose no restriction on that dimension. In non-DM rooms, `group_mention_only` bypasses the user list and applies the room + mention/reply gate; it does not bypass the room list.

## `SlackConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `bot_token` | `str` | `""` | Slack bot token (Socket Mode); keep it in the private config, never in logs |
| `app_token` | `str` | `""` | Slack app-level token used for Socket Mode |
| `allowed_channels` | `list[str]` | `[]` | Channel IDs the bot answers in; empty = no channel restriction (DMs always allowed) |
| `allowed_users` | `list[str]` | `[]` | Slack user IDs allowed to interact; empty = no user restriction |

Requires the Slack image extra; the first allowed channel is the default chat for background output.

## `CronDeliveryRetryConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Retry delivery of a preserved cron result without rerunning the agent |
| `interval_seconds` | `int` (>= 1) | `300` | Seconds between delivery retry attempts for one job |
| `max_attempts` | `int` (>= 1) | `12` | Maximum retry attempts per preserved result; after that retrying stops and the result stays preserved with its failed delivery status |

## `CronPreflightConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Run the task folder's `scripts/preflight.py` before the cron agent; missing/failed/timed-out preflight fails open (the agent runs) |
| `timeout_seconds` | `float` (> 0) | `15.0` | Time limit for the preflight script |
| `skip_marker` | `str` | `"HEARTBEAT_OK"` | Exit 0 with this as the last stdout line skips the agent run (recorded as `success:preflight`) |

## `CLIParametersConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `claude` | `list[str]` | `[]` | Extra args appended to Claude CLI command |
| `codex` | `list[str]` | `[]` | Extra args appended to Codex CLI command |
| `gemini` | `list[str]` | `[]` | Extra args appended to Gemini CLI command |
| `grok` | `list[str]` | `[]` | Extra args appended to Grok CLI command |

Used by `CLIServiceConfig` for main-chat calls.

Argument shape note:

- each list element is passed as one CLI argument; do not combine multiple shell flags into one string

Automation note:

- cron/webhook `cron_task` runs merge global provider-specific `cli_parameters` first, then task-level `cli_parameters` from `cron_jobs.json` / `webhooks.json`.

## `TimeoutConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `normal` | `float` | `600.0` | Default timeout for foreground chat turns (`normal` / `normal_streaming`) |
| `background` | `float` | `1800.0` | Timeout for named background sessions (`BackgroundObserver`) |
| `subagent` | `float` | `3600.0` | Reserved timeout bucket for sub-agent-specific paths |
| `warning_intervals` | `list[float]` | `[60.0, 10.0]` | Warning thresholds for `TimeoutController` |
| `extend_on_activity` | `bool` | `true` | Enables deadline extension when subprocess output is active |
| `activity_extension` | `float` | `120.0` | Seconds added per granted extension |
| `max_extensions` | `int` | `3` | Maximum activity-based extensions |

Runtime sync behavior:

- `AgentConfig` keeps backward compatibility with `cli_timeout`.
- If `cli_timeout != 600.0` and `timeouts.normal` is still default, runtime validation copies `cli_timeout` into `timeouts.normal`.
- A `timeouts.normal` value other than `600.0` wins. Explicitly setting it to `600.0` still triggers the compatibility copy; an empty `AgentConfig` therefore resolves normal timeout to `1800.0`.

Current execution-path usage:

- foreground chat turns: `resolve_timeout(config, "normal")` -> `timeouts.normal`
- named background sessions (`/session`): `timeouts.background`
- delegated background tasks (`TaskHub`): `tasks.timeout_seconds`
- cron + webhook `cron_task`: still `config.cli_timeout`
- inter-agent turns: still `config.cli_timeout`
- stale-process cleanup threshold: `config.cli_timeout * 2`

Implementation status note:

- `cli/timeout_controller.py` and warning/extension config are implemented and tested.
- provider wrappers and executor support `TimeoutController` in production paths.
- normal/streaming/named-session/heartbeat flows create controllers via `flows._make_timeout_controller(...)`.
- timeout warning/extension callbacks are not yet wired to Telegram/API system-status output, so user-visible timeout status labels are not emitted by default.

## `TasksConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `true` | Enables shared delegated task system (`TaskHub`) |
| `max_parallel` | `int` | `5` | Max concurrent running tasks per chat in `TaskHub` |
| `timeout_seconds` | `float` | `3600.0` | Timeout per delegated task run |
| `finished_retention_hours` | `int` | `168` | Age-based finished-task retention |
| `finished_keep_last` | `int` | `100` | Maximum finished tasks retained; age and count limits apply independently, so fewer than this count may remain |

Finished entries older than `finished_retention_hours` are removed even when among the newest `finished_keep_last`. Retention removes both registry entries and their task folders; running/waiting tasks are excluded. A nonpositive value disables that particular limit.

## Task-Level Automation Overrides

Stored outside `config.json` in:

- `~/.ductor/cron_jobs.json` (`CronJob`)
- `~/.ductor/webhooks.json` (`WebhookEntry`, `cron_task` mode)

Common per-task fields:

- execution: `provider`, `model`, `reasoning_effort`, `cli_parameters`
- scheduling guards: `quiet_start`, `quiet_end`, `dependency`

Cron-only field:

- `timezone` (per-job timezone override)

Behavior notes:

- missing execution fields fall back to global config via `resolve_cli_config()`,
- `dependency` is global across cron + webhook `cron_task` runs (shared `DependencyQueue`),
- quiet-hour checks run only when per-task quiet fields are set (no fallback to global heartbeat quiet settings).

## `StreamingConfig`

| Field | Type | Default |
|---|---|---|
| `enabled` | `bool` | `true` |
| `min_chars` | `int` | `200` |
| `max_chars` | `int` | `4000` |
| `idle_ms` | `int` | `800` |
| `edit_interval_seconds` | `float` | `2.0` |
| `max_edit_failures` | `int` | `3` |
| `append_mode` | `bool` | `false` |
| `sentence_break` | `bool` | `true` |
| `show_reasoning_stream` | `bool` | `false` |
| `show_tool_progress` | `bool` | `true` |
| `show_thinking_indicator` | `bool` | `true` |

- `show_reasoning_stream`: when the provider exposes reasoning/thinking blocks, stream them as separate Telegram messages instead of degrading them to a plain status indicator.
- `show_tool_progress`: show live Telegram tool activity messages during streaming turns.
- `show_thinking_indicator`: keep the lighter Telegram `THINKING` status indicator when no reasoning stream is being shown.

## `HeartbeatConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master toggle |
| `interval_minutes` | `int` | `30` | Loop interval |
| `cooldown_minutes` | `int` | `5` | Skip if user active recently |
| `quiet_start` | `int` | `0` | Quiet start hour in `user_timezone` |
| `quiet_end` | `int` | `0` | Quiet end hour in `user_timezone` |
| `prompt` | `str` | default prompt | Multiline default prompt references `MAINMEMORY.md` and `cron_tasks/` |
| `ack_token` | `str` | `"HEARTBEAT_OK"` | Suppression token |
| `group_targets` | `list[HeartbeatTarget]` | placeholder list | Runtime default is one disabled placeholder target so new configs show the expected shape immediately |
| `suspend_on_ack` | `bool` | `true` | ACK-only targets rest until accepted user/task/bus activity or a changed session marker |
| `auto_targets` | `bool` | `true` | Discover allowed recent Telegram sessions as separate chat/topic targets |
| `auto_target_window_hours` | `int` | `168` | Recency window for automatic targets |

Quiet hours are disabled by default in both `HeartbeatConfig` and `config.example.json`: `quiet_start=quiet_end=0`. Equal start/end hours mean no quiet window. Explicit saved windows still apply; the recent-activity cooldown and other heartbeat gates are unchanged. Cron/webhook quiet windows are independent.

ACK dormancy is enabled by default (`suspend_on_ack=true`) in the model and example configuration. Together with automatic targeting, each eligible session/group topic active within the last 7 days is checked separately; if it only answers `HEARTBEAT_OK`, it rests until the next conversation. Accepted task/inter-agent bus activity or a changed provider/session-ID/message-count marker also wakes that exact session key. Failed/skipped turns do not suspend. Global and explicit target ACK tokens must be nonblank when this setting is enabled; the default `HEARTBEAT_OK` is valid. Explicit `suspend_on_ack=false` retains repeated checks.

Automatic targets are enabled by default in the model and example configuration. Allowed Telegram DM and group-topic sessions with a resumable session ID are discovered within the 168-hour activity window; already tracked/recovered sessions may remain eligible outside it. Disabled manual entries opt their exact chat/topic out. With `auto_targets=false`, the observer selects allowed-user DM roots plus enabled manual targets instead.

### `HeartbeatTarget`

Each entry in `group_targets` identifies a specific group chat (and optional topic) to send heartbeat checks to. All optional fields override the global `HeartbeatConfig` when set; unset fields fall back to global values.

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `enabled` | `bool` | no | `true` | Target-level master toggle |
| `transport` | `str` | no | `"tg"` | Target transport |
| `chat_id` | `int \| None` | no | `None` | Target chat ID; a target without an ID is inert |
| `topic_id` | `int \| None` | no | `None` | Optional forum topic ID within the group |
| `prompt` | `str \| None` | no | `None` | Per-target prompt override (falls back to global `prompt`) |
| `ack_token` | `str \| None` | no | `None` | Per-target suppression token (falls back to global `ack_token`) |
| `interval_minutes` | `int \| None` | no | `None` | Per-target interval override (falls back to global `interval_minutes`) |
| `quiet_start` | `int \| None` | no | `None` | Per-target quiet-hour start (falls back to global `quiet_start`) |
| `quiet_end` | `int \| None` | no | `None` | Per-target quiet-hour end (falls back to global `quiet_end`) |

## `MemoryFlushConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `true` | Enables the post-stream silent flush pipeline |
| `flush_prompt` | `str` | default prompt | Prompt appended as a silent follow-up turn to capture durable facts before memory compaction |
| `dedup_seconds` | `int` | `300` | In-memory dedup window per `SessionKey` to avoid repeated flushes on back-to-back compact boundaries |

Runtime behavior:

- `CompactBoundaryEvent` from the provider stream marks the session for flush
- after the user-visible streaming turn succeeds, `MemoryFlusher.maybe_flush(...)` resumes the same CLI session silently
- errors are logged and swallowed; memory maintenance never blocks the user reply

## `MemoryReflectionConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Registers the reflection hook only when enabled |
| `every_n_messages` | `int` | `10` | Hook cadence; fires on the N-th outbound prompt for a session |
| `prompt` | `str` | default prompt | Silent reflection prompt appended through `MessageHookRegistry` |

Runtime behavior:

- implemented as a normal message hook, not as a background observer
- complements the always-on `MAINMEMORY_REMINDER` hook rather than replacing it

## `MemoryLintConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `mode` | `"structure"`, `"full"`, `"off"` | `"structure"` | `structure`: script check; an agent turn only on problems, which also makes the safe fixes. `full`: check, then always one agent turn per period (semantic check, monthly stale check, fixes, report). `off`: nothing |
| `every_hours` | `int` (>= 1) | `168` | Period between checks; the first check happens one period after the home is first seen (state in `memory_lint_state.json`). Ignored while `schedule` is set |
| `schedule` | `str` or unset | unset | Cron expression (same syntax as cron jobs, e.g. `0 9 * * 1` = Mondays 09:00) read in the agent's `user_timezone`; replaces `every_hours`. A run is due when a scheduled time has passed since the last run; several missed times run once; the first time the home is seen the lint waits for the next scheduled time. An invalid expression is logged once and the lint does not run until it is fixed |
| `report` | `"topic"`, `"dm"`, `"off"` | `"topic"` | Where a report turn is delivered; `off` runs nothing, like `mode: "off"` |
| `provider` | `str` or unset | unset | Provider for the lint turn (`claude`, `codex`, `gemini`, `grok`); applied in topic mode only (see below) |
| `model` | `str` or unset | unset | Model for the lint turn; without `provider` the provider follows the model. Validated like a cron job's `model` |
| `reasoning_effort` | `str` or unset | unset | Reasoning effort for the lint turn; validated like a cron job's |

Empty strings count as unset. The former `enabled` key no longer exists. A saved `"enabled": false` is read as `"mode": "off"` (an explicit `mode` wins), and an INFO line `memory_lint.enabled is false and no mode is set: read as mode=off` is logged each time the configuration is loaded or hot-reloaded; `"enabled": true` or no key means the default mode.

Runtime behavior:

- a framework observer runs `tools/memory_tools/memory_lint.py check --json` in the agent workspace
- **`structure`**: a clean wiki starts **no agent turn** (no cost) and logs `Memory lint structure check: 0 problems` at INFO. On problems, Telegram bots run one agent turn whose input is the report ([Weekly memory lint] prompt: fix what is safe, never delete without asking, then summarize for the user) in the first allowed user's chat
- **`full`**: every period one agent turn is started, **even when the wiki is clean**, so each period costs one model turn. Its input is a `[Memory lint - full check]` header, the structural result (the problem list, "no structural problems", or "structural check failed to run" when the script could not run), a stale-check flag and the instructions from `tools/memory_tools/LINT_FULL.md`. The stale check is flagged for the first run of each calendar month (month taken in `user_timezone`; recorded in `memory_lint_state.json` as `last_stale_month` before the turn starts) and skipped, with a note in the report, for the other runs of that month
- `LINT_FULL.md` is a user-owned file: it is seeded once when missing and never overwritten by updates, so later improvements of the framework's default instructions do not reach an existing copy (delete it and restart to get the current default). A missing, empty or unreadable file is replaced by the built-in default text with a warning in the log
- every lint turn prompt (also for `structure`) carries a framework rule that suggestions stay within maintaining the memory wiki (no work outside it, such as restoring test or working files); the `full` prompt adds, before the instructions, that the pages being judged must be read again in this run and conclusions of earlier runs are not reused as evidence. Both come from the framework, not from `LINT_FULL.md`, which stays yours. Whether the model follows them is not covered by tests
- the lint turn hides the session id of whatever the task logged before (the log context is restored afterwards); the turn's own session id is shown once it is known
- the default instructions only allow three mechanical fixes (a broken link with one clear target, a missing `updated`, missing frontmatter); everything else is reported as a decision for the user
- `topic` + a bot whose private chat is in topic mode (`getMe.has_topics_enabled`): the turn runs in a dedicated "Memory Lint" topic (named in the configured `language` when it is created; an existing topic is reused by its stored id and never renamed), created once via `createForumTopic` with its thread id persisted; if delivery to it fails (topic deleted) it is re-created once, and when the answer reaches the new topic the report session (same session id, all provider buckets) moves there under that topic's lock, so the next lint turn continues it; a session the new topic already has is never replaced. Otherwise, or with `dm`, the turn runs in the plain DM
- each Telegram bot (main and bot-backed subs) lints its own workspace for its own user; headless agents and Matrix/Slack transports are not reported to. A sub does **not** inherit this section from main: it uses the defaults plus its own optional `memory_lint` entry in `agents.json` (see [sub-agent merge behavior](#sub-agent-runtime-merge-behavior))
- `provider` / `model` / `reasoning_effort` (all optional, for `structure` and `full`): **only in the dedicated "Memory Lint" topic** (`report: topic` and a bot in topic mode) the session of that topic is pointed at these values before every lint turn, also right after the topic was re-created. They win over a `/model` change made in that topic: the next lint turn sets them again. Parts that are not configured keep the topic session's current value (for example only `reasoning_effort`). **In the plain DM nothing is applied** (a warning is logged once) because that would change the model of your main DM session. Invalid values are logged and the turn runs with the session as it is
- all settings are hot-reloadable (the observer reads them on every tick)

## `MemoryCompactionConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `true` | Enables LLM-driven compaction after a flush when file size threshold is exceeded |
| `trigger_lines` | `int` | `70` | `MAINMEMORY.md` line-count threshold that makes compaction eligible |
| `target_lines` | `int` | `40` | Target post-compaction size used in the prompt template |
| `preserve_recency_days` | `int` | `14` | Recent entries to preserve verbatim during compaction |
| `prompt` | `str` | default prompt template | Formatted at runtime with `target_lines` and `preserve_days` |

Compaction runs only after a successful flush and reuses the same provider session as the user turn.

## `ImageConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `max_dimension` | `int` | `2000` | Maximum width/height in pixels; images exceeding this are resized proportionally |
| `output_format` | `str` | `"webp"` | Target image format (e.g. `webp`, `jpeg`, `png`) |
| `quality` | `int` | `85` | Compression quality for lossy formats (WebP, JPEG) |

The Telegram, Matrix and API call sites currently invoke `process_image(dest)`
without passing these settings. Incoming images therefore use the helper defaults
above; changing `config.image` does not currently change processing. See
[files module](modules/files.md).

## `SceneConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `seen_reaction` | `bool` | `false` | Enables "seen" indicator on incoming messages (Telegram: emoji reaction, Matrix: read receipt) |
| `status_reaction` | `bool` | `true` | Telegram-only stage tracker on the user message while the turn runs; when enabled it wins over `seen_reaction` so both do not fight over the same emoji slot |
| `technical_footer` | `bool` | `false` | Appends a model/time footer to agent responses (token counts and cost are not shown) |

## `NotificationsConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `startup_targets` | `list[NotificationTarget]` | `[]` | When non-empty and containing at least one enabled target with `chat_id`, startup notices are routed only there |

### `NotificationTarget`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `true` | Target-level toggle |
| `chat_id` | `int \| None` | `None` | Telegram chat ID or Matrix room-mapped int |
| `topic_id` | `int \| None` | `None` | Telegram forum topic; ignored by Matrix |

Behavior notes:

- empty target lists preserve the old broadcast-to-all behavior
- Telegram and Matrix support targeted startup notices; self-upgrade notifications were removed.
- Async delegation receipt notices are separate: the receiving agent must enable `interagent_notify_received` and have its own notification transport. `--silent` suppresses them even when enabled.

## `TranscriptionConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `audio_command` | `str` | `""` | When set, exported as `DUCTOR_TRANSCRIBE_COMMAND` for `tools/media_tools/transcribe_audio.py` |
| `video_command` | `str` | `""` | When set, exported as `DUCTOR_VIDEO_TRANSCRIBE_COMMAND` for `tools/media_tools/process_video.py` |

Empty strings keep the bundled fallback chain intact:

- audio: external hook -> OpenAI Whisper API -> local `whisper` CLI -> `whisper.cpp`
- video: external hook -> existing built-in video transcription path

## `CleanupConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `true` | Master toggle |
| `media_files_days` | `int` | `30` | Retention for media files (telegram + matrix) |
| `output_to_user_days` | `int` | `30` | Retention in `workspace/output_to_user/` |
| `api_files_days` | `int` | `30` | Retention in `workspace/api_files/` |
| `check_hour` | `int` | `3` | Local hour in `user_timezone` for cleanup run |

Cleanup implementation detail:

- cleanup is recursive (`_delete_old_files` walks nested files via `rglob("*")`),
- after file deletion, empty subdirectories are pruned,
- dated upload folders (`.../YYYY-MM-DD/...`) are cleaned when contained files exceed retention and directories become empty.

## `WebhookConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master toggle |
| `host` | `str` | `"127.0.0.1"` | Bind address (localhost by default) |
| `port` | `int` | `8742` | HTTP server port |
| `token` | `str` | `""` | Global bearer fallback token (auto-generated when webhooks start) |
| `max_body_bytes` | `int` | `262144` | Max request body size |
| `rate_limit_per_minute` | `int` | `30` | Sliding-window rate limit |

## `ApiConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master toggle |
| `host` | `str` | `"0.0.0.0"` | Bind address |
| `port` | `int` | `8741` | API HTTP/WebSocket port |
| `token` | `str` | `""` | Bearer/WebSocket auth token (generated and persisted at API startup when empty) |
| `chat_id` | `int` | `0` | Default API session chat (`0` means fallback to first `allowed_user_ids` entry, else `1`) |
| `allow_public` | `bool` | `false` | Suppresses Tailscale-not-detected warning |

Runtime note (`Orchestrator._start_api_server` + `ApiServer._authenticate`):

- `config.api.chat_id` is used via truthiness (`0` falls back),
- fallback default comes from first `allowed_user_ids` entry (fallback `1`),
- per-connection auth payload may override via:
  - `{"type":"auth","chat_id":...}` (positive int),
  - optional `channel_id` (positive int) for per-channel session isolation (`SessionKey.topic_id`),
- clients can override only for that connection; persisted default stays in config.

## `SkillsConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `sync_enabled` | `bool` | `true` | Global toggle for cross-tool skill sync; when `false`, `sync_skills` returns early and no sync runs |
| `sync` | `SkillSyncProviders` | see below | Per-provider sync toggles |

### `SkillSyncProviders`

| Field | Type | Default | Notes |
|---|---|---|---|
| `claude` | `bool` | `true` | Include `~/.claude/skills` in cross-tool sync |
| `codex` | `bool` | `true` | Include `~/.codex/skills` (or `$CODEX_HOME/skills`) in cross-tool sync |
| `gemini` | `bool` | `true` | Include `~/.gemini/skills` in cross-tool sync |
| `grok` | `bool` | `true` | Include `$GROK_HOME/skills` (default `~/.grok/skills`) in cross-tool sync |

Toggles are read live from `config.json` on each skill-sync tick (independent of `ConfigReloader`), so changes take effect within one sync interval without restart. A disabled provider is dropped from the sync, so its skill dir is neither linked into nor used as a source; existing DT Agent-created links are not actively removed (cleared on shutdown cleanup or manually).

## Runtime hot-reload (`config_reload.py`)

`Orchestrator.create()` starts `ConfigReloader`, which polls `config.json` every 5 seconds, validates it with `AgentConfig`, diffs top-level fields, and applies safe changes without restart.

Hot-reloadable top-level fields:

- `model`, `provider`, `reasoning_effort`
- `cli_timeout`, `max_budget_usd`, `max_turns`, `max_session_messages`
- `idle_timeout_minutes`, `session_age_warning_hours`, `daily_reset_hour`, `daily_reset_enabled`
- `permission_mode`, `file_access`, `user_timezone`
- `streaming`, `heartbeat`, `cleanup`, `cli_parameters`, `project_roots`, `claude_accounts`, `scene`, `image`, `language`
- `allowed_user_ids`, `allowed_group_ids`, `group_mention_only`
- `telegram_dm_topic_auto_name`, `persona_setup_prompt`, `memory_lint`

Bundled config rules now match `ConfigReloader`: the removed `docker` key is absent from restart-required instructions. `allowed_channel_ids`, `api`, `append_system_prompt_files`, `claude_system_prompt_snapshot`, `cron_delivery_retry`, `cron_preflight`, `ductor_home`, `gemini_api_key`, `interagent`, `interagent_notify_received`, `interagent_port`, `log_level`, `matrix`, `memory_compaction`, `memory_flush`, `memory_reflection`, `notifications`, `skills`, `slack`, `tasks`, `telegram_token`, `timeouts`, `transcription`, `transport`, `transports`, `webhooks`, and unknown/new keys require `/restart`. This is an application restart, not a Compose rebuild.

Current non-hot fields that often surprise people:

- `allowed_channel_ids` exists on `AgentConfig` but is not currently classified as hot-reloadable by `ConfigReloader`, so channel allowlist changes still require restart
- `notifications`, `transcription`, `timeouts`, `tasks`, `interagent`, `claude_system_prompt_snapshot` and `interagent_notify_received` are restart-required

Observer lifecycle caveat:

- heartbeat/cleanup values hot-reload into config
- heartbeat reload resets suspend state and starts/stops its observer when `enabled` changes
- cleanup has no start/stop reload hook: enabling it after a disabled startup requires restart; an existing loop reads updated values and skips cycles while disabled

Restart-required top-level fields:

- `transport`, `transports`, `telegram_token`, `matrix`, `slack`
- `api`, `webhooks`
- `ductor_home`, `log_level`, `gemini_api_key`, `notifications`, `transcription`, `timeouts`, `tasks`, `memory_flush`, `memory_reflection`, `memory_compaction`, `skills`, `append_system_prompt_files`, `cron_delivery_retry`, `cron_preflight`, `allowed_channel_ids`, `interagent_port`, `interagent_notify_received`, `interagent`, `claude_system_prompt_snapshot`

Restart classification is computed from `AgentConfig` top-level schema fields.

## Model Resolution

`ModelRegistry` (`ductor_bot/config.py`):

- Claude models are hardcoded: `haiku`, `sonnet`, `sonnet[1m]`, `opus`, `opus[1m]`, and `fable` (Claude CLI strips the `[1m]` suffix and sets the 1M-context beta header internally).
- Gemini aliases are hardcoded: `auto`, `pro`, `flash`, `flash-lite`.
- Runtime Gemini models are discovered from local Gemini CLI files at startup.
- Grok models are discovered through a model cache, with hardcoded fallback IDs.
- Provider resolution (`provider_for(model_id)`):
  - Claude when in `CLAUDE_MODELS` or when model looks like `claude-*`,
  - Gemini when in aliases/discovered set or when model looks like `gemini-*`/`auto-gemini-*`,
  - Grok when in its known model set or when the model starts with `grok-`,
  - otherwise Codex.

## Timezone Resolution

`resolve_user_timezone(configured)` in `ductor_bot/config.py`:

1. valid configured IANA timezone,
2. `$TZ` env var,
3. host system detection:
   - Windows: local datetime tzinfo,
   - POSIX: `/etc/localtime` symlink,
4. fallback `UTC`.

Returns `ZoneInfo`, using UTC as the final fallback (the package includes the `tzdata` dependency). Used by cron scheduling, session daily-reset checks, heartbeat quiet hours, and cleanup scheduling.

## `reasoning_effort`

Supported effort values depend on the provider/model: Claude uses `low`,
`medium`, `high`, `xhigh`, `max`; Grok also accepts `none` and `minimal`.
Codex uses the model's discovered supported efforts. Gemini drops effort overrides.
Main-chat selectors validate against that provider/model. Automation
`resolve_cli_config()` carries supported Claude, Codex, and Grok efforts;
an explicit unsupported override raises an error, while an unsupported global
fallback is dropped. Provider wrappers then emit their corresponding CLI flag.

Sources: model definitions (`ductor_bot/config.py`),
automation resolver (`ductor_bot/cli/param_resolver.py`),
command builders (`ductor_bot/cron/execution.py`).

## Codex Model Cache

Path: `~/.ductor/config/codex_models.json`.

Behavior:

- loaded at orchestrator startup (`CodexCacheObserver.start()`),
- startup load is forced refresh (`force_refresh=True`),
- checked hourly in background,
- `load_or_refresh()` uses cache if `<24h` old, otherwise re-discovers via Codex app server,
- consumed by `/model` wizard, `resolve_cli_config()` for cron/webhook validation, and `/diagnose` output.

## Gemini Model Cache

Path: `~/.ductor/config/gemini_models.json`.

Behavior:

- loaded at orchestrator startup (`GeminiCacheObserver.start()`),
- startup load uses cached data when fresh and refreshes only when stale/missing,
- refreshed hourly in background,
- refresh callback updates runtime Gemini model registry (`set_gemini_models(...)`) used by directives and model selector.


Grok model metadata is likewise cached in `config/grok_models.json` under the
current agent home and refreshed by `GrokCacheObserver`.

## `agents.json` (Multi-Agent Registry)

Path: `~/.ductor/agents.json`.

Top-level JSON array of `SubAgentConfig` objects. Each entry defines a sub-agent that runs alongside the main agent.

Managed via:

- `ductor agents add <name>` (interactive CLI, currently Telegram-focused)
- `ductor agents remove <name>` (CLI)
- `create_agent.py` / `remove_agent.py` tool scripts (from within a CLI session)
- manual file editing (auto-detected by `FileWatcher`)

### `SubAgentConfig` fields

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `name` | `str` | yes | | `^[a-z0-9][a-z0-9._-]{0,63}$`; no `..` or `main` |
| `transport` | `str` | no | inferred | Explicit `"telegram"`, `"matrix"`, or `"headless"` takes priority |
| `telegram_token` | `str` | conditional | | Required when `transport=telegram` |
| `matrix` | `MatrixConfig` | conditional | | Required when `transport=matrix` |
| `allowed_user_ids` | `list[int]` | no | `[]` | Telegram user allowlist |
| `allowed_group_ids` | `list[int]` | no | `[]` | Telegram group allowlist |
| `group_mention_only` | `bool` | no | inherited | Mention/reply gating toggle (transport-specific behavior) |
| `provider` | `str` | no | inherited | Default provider |
| `model` | `str` | no | inherited | Default model |
| `log_level` | `str` | no | inherited | |
| `idle_timeout_minutes` | `int` | no | inherited | |
| `session_age_warning_hours` | `int` | no | inherited | |
| `daily_reset_hour` | `int` | no | inherited | |
| `daily_reset_enabled` | `bool` | no | inherited | |
| `max_budget_usd` | `float` | no | inherited | |
| `max_turns` | `int` | no | inherited | |
| `max_session_messages` | `int` | no | inherited | |
| `permission_mode` | `str` | no | inherited | |
| `cli_timeout` | `float` | no | inherited | |
| `reasoning_effort` | `str` | no | inherited | |
| `file_access` | `str` | no | inherited | |
| `streaming` | `StreamingConfig` | no | inherited | |
| `heartbeat` | `HeartbeatConfig` | no | disabled | Omitted: disabled defaults; explicit `null`: inherit main |
| `cleanup` | `CleanupConfig` | no | inherited | |
| `webhooks` | `WebhookConfig` | no | inherited | |
| `api` | `ApiConfig` | no | disabled | Disabled by default for sub-agents |
| `cli_parameters` | `CLIParametersConfig` | no | inherited | |
| `user_timezone` | `str` | no | inherited | |
| `interagent_notify_received` | `bool` | no | inherited (`false` on main by default) | Async task receipt notices; `--silent` always suppresses |
| `interagent` | [`InteragentConfig`](#interagentconfig), partial | no | inherited from main, key by key | Only the keys given here differ from main's values; the effective values are written into the sub's own `config.json` (other keys are left as they are) |
| `claude_system_prompt_snapshot` | `"off"` or `"on"` | no | inherited | Per-sub override of the Claude system prompt snapshot mode; mirrored into the sub's `config.json` |
| `memory_lint` | [`MemoryLintConfig`](#memorylintconfig), partial | no | **not inherited**: framework defaults (`mode: "structure"`) | Periodic memory lint of this sub's own workspace; only the keys given here differ from the defaults |

"inherited" means the value comes from the main agent's `config.json` when omitted.
For receipt notices, a sub-agent can opt in alone with
`{"name": "messenger", "telegram_token": "SET_IN_PRIVATE_REGISTRY", "allowed_user_ids": [12345678], "interagent_notify_received": true}` in `agents.json`.
The receiving agent's merged value controls notices on the server; disabling
notices leaves delegation/result delivery intact and accepts `--silent` as a no-op.

Transport selection is separate: an explicit `transport` is used only when
the sub-agent has its own usable credentials (except `headless`, which
requires none). An explicit messenger transport without usable own credentials fails validation.
When transport is omitted, its own Telegram token and complete Matrix credentials
select one or both transports. With neither, it is headless.
Main transports are not inherited; Slack sub-agents are unsupported.

Timeout nuance:

- `SubAgentConfig` currently has no dedicated `timeouts` field.
- `SubAgentConfig` currently has no dedicated `tasks` field.
- `SubAgentConfig` also has no dedicated `scene`, `notifications`, `transcription`, `language`, or `allowed_channel_ids` fields.
- sub-agents inherit the main agent `timeouts` block through merge base.
- sub-agents inherit the main agent `tasks` block through merge base.
- the same inheritance currently applies to `scene`, `notifications`, `transcription`, `language`, and `allowed_channel_ids`.

Example:

```json
[
  {
    "name": "researcher",
    "telegram_token": "123456:ABC...",
    "allowed_user_ids": [12345678],
    "provider": "claude",
    "model": "sonnet"
  },
  {
    "name": "coder",
    "transport": "matrix",
    "matrix": {
      "homeserver": "https://matrix.example.com",
      "user_id": "@coder:example.com",
      "password": "...",
      "allowed_rooms": ["!room:example.com"],
      "allowed_users": ["@user:example.com"]
    },
    "provider": "codex",
    "reasoning_effort": "high"
  }
]
```

A headless entry has no direct chat or bot credentials. Users ask main to delegate synchronously/asynchronously; async reports return through main to the original chat/topic. For agent-side creation use `python3 tools/agent_tools/create_agent.py --name NAME --transport headless` with optional `--provider` and `--model` inside a framework CLI turn. Choose Telegram/Matrix instead when a direct sub chat is required. See [delegation question routing](modules/multiagent.md#questions-during-delegation).

### Sub-agent runtime merge behavior

`merge_sub_agent_config(main, sub, agent_home)` builds the effective sub-agent `AgentConfig` with this priority:

1. main agent config (`config.json`) as base
2. explicit non-null overrides from `agents.json` (highest priority)

`interagent` is merged key by key over main's value, and `claude_system_prompt_snapshot` is inherited or overridden as a whole. Both take effect after a restart; like other non-token fields, a changed value in `agents.json` does not restart a running sub (use `/agent_restart <name>`).

**Exception: `memory_lint` is never taken from main.** A sub's effective `memory_lint` is the framework default plus the keys given in its own `agents.json` entry (all optional, partial), so a main set to `full`, `off`, a `schedule` or a `model` does not reach any sub: a sub without the field runs the default `structure` lint (an agent turn only when the check finds problems). Example: `{"name": "coder", "telegram_token": "SET_IN_PRIVATE_REGISTRY", "allowed_user_ids": [12345678], "memory_lint": {"mode": "full", "schedule": "0 9 * * 1"}}`. Values are validated like any other `agents.json` field (an invalid `mode` excludes that entry with a diagnostic); `schedule`, `model` and the like follow the rules of [`MemoryLintConfig`](#memorylintconfig). The supervisor also writes the effective value into the sub's own `config.json` (next to provider, model and effort) so that a later change of that file does not revert it. Like other non-token fields, a changed `memory_lint` in `agents.json` does not restart a running sub; use `/agent_restart <name>` (or restart the bot) to apply it.

Then it always forces:

- `ductor_home = agent_home`, supplied as `<main_home>/agents/<name>` by the supervisor (normally `/ductor/agents/<name>` in the container)
- selected `transport`/`transports` and the sub-agent's own Telegram token and allowlists; its Matrix block is used when provided
- `api.enabled = false` when no explicit `api` block is provided

Notes:

- there is no extra persisted runtime config layer for sub-agents in merge order
- `/model` changes in a sub-agent chat are written back to `agents.json`, so restart/reload uses the updated values from that registry file

### `agents.json` watcher behavior

`AgentSupervisor` watches `agents.json` (mtime poll every 5s):

- new entry -> start sub-agent
- removed entry -> stop sub-agent
- restart triggers for running agents:
  - selected `transport` or `transports` changed
  - Telegram identity changed (`telegram_token`)
  - Matrix identity changed (`homeserver`, `user_id`, `password`, `access_token`, `device_id`, or `store_path`)
- other field changes currently do not auto-restart running agents

For non-token field updates on a running agent, use `/agent_restart <name>` (or restart the bot) to apply them immediately.

Source of the schema/default tables: config.py (`ductor_bot/config.py`).
Sub-agent merge and transport rules: models.py (`ductor_bot/multiagent/models.py`);
watcher restart rules: supervisor.py (`ductor_bot/multiagent/supervisor.py`);
hot-reload classification: config_reload.py (`ductor_bot/config_reload.py`).

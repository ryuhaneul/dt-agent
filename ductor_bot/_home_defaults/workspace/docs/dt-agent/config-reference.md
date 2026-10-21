# Configuration reference (generated)

Every key of `config/config.json` (`AgentConfig`), nested keys included. Generated from the
code by `scripts/build_agent_docs.py`; the Note column comes from `config.md`, which has the
full explanation. **Hot reload**: `hot` = applied within seconds without a restart; `restart`
= needs `/restart` (application restart, not a Compose rebuild). Nested keys follow their
top-level key.

| Key | Type | Default | Hot reload | Note |
|---|---|---|---|---|
| `log_level` | `str` | `"INFO"` | restart | Applied at startup unless CLI `--verbose` is used |
| `provider` | `str` | `"claude"` | hot | Default provider |
| `model` | `str` | `"opus"` | hot | Default model ID |
| `ductor_home` | `str` | `"/ductor"` | restart | Runtime home inside the container |
| `idle_timeout_minutes` | `int` | `1440` | hot | Session freshness idle timeout (`0` disables idle expiry) |
| `session_age_warning_hours` | `int` | `12` | hot | Adds `/new` reminder after threshold (every 10 messages) |
| `daily_reset_hour` | `int` | `4` | hot | Daily reset boundary hour in `user_timezone` |
| `daily_reset_enabled` | `bool` | `false` | hot | Enables daily session reset checks |
| `max_budget_usd` | `float or null` | `null` | hot | Passed to Claude CLI |
| `max_turns` | `int or null` | `null` | hot | Passed to Claude CLI |
| `max_session_messages` | `int or null` | `null` | hot | Session rollover limit |
| `permission_mode` | `str` | `"bypassPermissions"` | hot | Provider sandbox/approval mode |
| `cli_timeout` | `float` | `1800.0` | hot | Legacy/global timeout. Still used by cron/webhook `cron_task`, inter-agent turns, stale-process heartbeat cleanup, and as fallback for unknown timeout paths |
| `reasoning_effort` | `str` | `"medium"` | hot | Default reasoning level for providers that support effort |
| `file_access` | `str` | `"all"` | hot | File access scope (`all`, `home`, `workspace`) for file sends and API `GET /files`; unknown values fall back to workspace-only |
| `append_system_prompt_files` | `list[str]` | `["PERSONA.md", "BEHAVIOR.md"]` | restart | Additional system prompt files; a `PERSONA.md` that still contains its unset marker (`<!-- PERSONA_UNSET -->`) is skipped |
| `persona_setup_prompt` | `bool` | `true` | hot | While `PERSONA.md` carries the unset marker, add a first-run setup hint to user turns (first turn of a session, then every 6th message); hot-reloadable. Off = never ask |
| `project_roots` | `dict[str, str]` | `{}` | hot | Topic name, topic ID, or `chat_id:topic_id` mapped to a project path |
| `gemini_api_key` | `str or null` | `null` | restart | Config fallback key injected for Gemini API-key mode |
| `claude_accounts` | `list[str]` | `[]` | hot | Claude authentication slots |
| `streaming` | `StreamingConfig` | `(nested keys below)` | hot | Streaming tuning |
| `streaming.enabled` | `bool` | `true` | hot | `true` |
| `streaming.min_chars` | `int` | `200` | hot | `200` |
| `streaming.max_chars` | `int` | `4000` | hot | `4000` |
| `streaming.idle_ms` | `int` | `800` | hot | `800` |
| `streaming.edit_interval_seconds` | `float` | `2.0` | hot | `2.0` |
| `streaming.max_edit_failures` | `int` | `3` | hot | `3` |
| `streaming.append_mode` | `bool` | `false` | hot | `false` |
| `streaming.sentence_break` | `bool` | `true` | hot | `true` |
| `streaming.show_reasoning_stream` | `bool` | `false` | hot | `false` |
| `streaming.show_tool_progress` | `bool` | `true` | hot | `true` |
| `streaming.show_thinking_indicator` | `bool` | `true` | hot | `true` |
| `heartbeat` | `HeartbeatConfig` | `(nested keys below)` | hot | Background heartbeat config |
| `heartbeat.enabled` | `bool` | `false` | hot | Master toggle |
| `heartbeat.interval_minutes` | `int` | `30` | hot | Loop interval |
| `heartbeat.cooldown_minutes` | `int` | `5` | hot | Skip if user active recently |
| `heartbeat.quiet_start` | `int` | `0` | hot | Quiet start hour in `user_timezone` |
| `heartbeat.quiet_end` | `int` | `0` | hot | Quiet end hour in `user_timezone` |
| `heartbeat.prompt` | `str` | `"You are running as a background heartbeat check. Review ...` | hot | Multiline default prompt references `MAINMEMORY.md` and `cron_tasks/` |
| `heartbeat.ack_token` | `str` | `"HEARTBEAT_OK"` | hot | Suppression token |
| `heartbeat.suspend_on_ack` | `bool` | `true` | hot | ACK-only targets rest until accepted user/task/bus activity or a changed session marker |
| `heartbeat.auto_targets` | `bool` | `true` | hot | Discover allowed recent Telegram sessions as separate chat/topic targets |
| `heartbeat.auto_target_window_hours` | `int` | `168` | hot | Recency window for automatic targets |
| `heartbeat.group_targets` | `list[HeartbeatTarget]` | `[1 default item(s)]` | hot | Runtime default is one disabled placeholder target so new configs show the expected shape immediately |
| `heartbeat.group_targets.enabled` | `bool` | `true` | hot | Target-level master toggle |
| `heartbeat.group_targets.transport` | `str` | `"tg"` | hot | Target transport |
| `heartbeat.group_targets.chat_id` | `int or null` | `null` | hot | Target chat ID; a target without an ID is inert |
| `heartbeat.group_targets.topic_id` | `int or null` | `null` | hot | Optional forum topic ID within the group |
| `heartbeat.group_targets.prompt` | `str or null` | `null` | hot | Per-target prompt override (falls back to global `prompt`) |
| `heartbeat.group_targets.ack_token` | `str or null` | `null` | hot | Per-target suppression token (falls back to global `ack_token`) |
| `heartbeat.group_targets.interval_minutes` | `int or null` | `null` | hot | Per-target interval override (falls back to global `interval_minutes`) |
| `heartbeat.group_targets.quiet_start` | `int or null` | `null` | hot | Per-target quiet-hour start (falls back to global `quiet_start`) |
| `heartbeat.group_targets.quiet_end` | `int or null` | `null` | hot | Per-target quiet-hour end (falls back to global `quiet_end`) |
| `cleanup` | `CleanupConfig` | `(nested keys below)` | hot | Daily file-retention cleanup |
| `cleanup.enabled` | `bool` | `true` | hot | Master toggle |
| `cleanup.media_files_days` | `int` | `30` | hot | Retention for media files (telegram + matrix) |
| `cleanup.output_to_user_days` | `int` | `30` | hot | Retention in `workspace/output_to_user/` |
| `cleanup.api_files_days` | `int` | `30` | hot | Retention in `workspace/api_files/` |
| `cleanup.check_hour` | `int` | `3` | hot | Local hour in `user_timezone` for cleanup run |
| `memory_flush` | `MemoryFlushConfig` | `(nested keys below)` | restart | Silent pre-compaction memory flush after streaming compact boundaries |
| `memory_flush.enabled` | `bool` | `true` | restart | Enables the post-stream silent flush pipeline |
| `memory_flush.flush_prompt` | `str` | `"## PRE-COMPACTION MEMORY FLUSH\nThe conversation context...` | restart | Prompt appended as a silent follow-up turn to capture durable facts before memory compaction |
| `memory_flush.dedup_seconds` | `int` | `300` | restart | In-memory dedup window per `SessionKey` to avoid repeated flushes on back-to-back compact boundaries |
| `memory_reflection` | `MemoryReflectionConfig` | `(nested keys below)` | restart | Optional periodic memory reflection hook |
| `memory_reflection.enabled` | `bool` | `false` | restart | Registers the reflection hook only when enabled |
| `memory_reflection.every_n_messages` | `int` | `10` | restart | Hook cadence; fires on the N-th outbound prompt for a session |
| `memory_reflection.prompt` | `str` | `"## MEMORY REFLECTION\nReview the last several messages i...` | restart | Silent reflection prompt appended through `MessageHookRegistry` |
| `memory_lint` | `MemoryLintConfig` | `(nested keys below)` | hot | Periodic framework lint of `memory_system/` (`mode` structure/full/off); hot-reloadable |
| `memory_lint.mode` | `"structure" or "full" or "off"` | `"structure"` | hot | `structure`: script check; an agent turn only on problems, which also makes the safe fixes. `full`: check, then always one agent turn per period (semantic check, monthly stale check, fixes, report). `off`: nothing |
| `memory_lint.every_hours` | `int` | `168` | hot | Period between checks; the first check happens one period after the home is first seen (state in `memory_lint_state.json`). Ignored while `schedule` is set |
| `memory_lint.schedule` | `str or null` | `null` | hot | Cron expression (same syntax as cron jobs, e.g. `0 9 * * 1` = Mondays 09:00) read in the agent's `user_timezone`; replaces `every_hours`. A run is due when a scheduled time has passed since the last run; several misse... |
| `memory_lint.report` | `"topic" or "dm" or "off"` | `"topic"` | hot | Where a report turn is delivered; `off` runs nothing, like `mode: "off"` |
| `memory_lint.provider` | `str or null` | `null` | hot | Provider for the lint turn (`claude`, `codex`, `gemini`, `grok`); applied in topic mode only (see below) |
| `memory_lint.model` | `str or null` | `null` | hot | Model for the lint turn; without `provider` the provider follows the model. Validated like a cron job's `model` |
| `memory_lint.reasoning_effort` | `str or null` | `null` | hot | Reasoning effort for the lint turn; validated like a cron job's |
| `memory_compaction` | `MemoryCompactionConfig` | `(nested keys below)` | restart | LLM-driven `MAINMEMORY.md` compaction policy |
| `memory_compaction.enabled` | `bool` | `true` | restart | Enables LLM-driven compaction after a flush when file size threshold is exceeded |
| `memory_compaction.trigger_lines` | `int` | `70` | restart | `MAINMEMORY.md` line-count threshold that makes compaction eligible |
| `memory_compaction.target_lines` | `int` | `40` | restart | Target post-compaction size used in the prompt template |
| `memory_compaction.preserve_recency_days` | `int` | `14` | restart | Recent entries to preserve verbatim during compaction |
| `memory_compaction.prompt` | `str` | `"## MEMORY COMPACTION\nmemory_system/MAINMEMORY.md has gr...` | restart | Formatted at runtime with `target_lines` and `preserve_days` |
| `webhooks` | `WebhookConfig` | `(nested keys below)` | restart | Webhook HTTP server config |
| `webhooks.enabled` | `bool` | `false` | restart | Master toggle |
| `webhooks.host` | `str` | `"127.0.0.1"` | restart | Bind address (localhost by default) |
| `webhooks.port` | `int` | `8742` | restart | HTTP server port |
| `webhooks.token` | `str` | `""` | restart | Global bearer fallback token (auto-generated when webhooks start) |
| `webhooks.max_body_bytes` | `int` | `262144` | restart | Max request body size |
| `webhooks.rate_limit_per_minute` | `int` | `30` | restart | Sliding-window rate limit |
| `api` | `ApiConfig` | `(nested keys below)` | restart | Direct WebSocket API server config |
| `api.enabled` | `bool` | `false` | restart | Master toggle |
| `api.host` | `str` | `"0.0.0.0"` | restart | Bind address |
| `api.port` | `int` | `8741` | restart | API HTTP/WebSocket port |
| `api.token` | `str` | `""` | restart | Bearer/WebSocket auth token (generated and persisted at API startup when empty) |
| `api.chat_id` | `int` | `0` | restart | Default API session chat (`0` means fallback to first `allowed_user_ids` entry, else `1`) |
| `api.allow_public` | `bool` | `false` | restart | Suppresses Tailscale-not-detected warning |
| `cli_parameters` | `CLIParametersConfig` | `(nested keys below)` | hot | Provider-specific extra CLI flags |
| `cli_parameters.claude` | `list[str]` | `[]` | hot | Extra args appended to Claude CLI command |
| `cli_parameters.codex` | `list[str]` | `[]` | hot | Extra args appended to Codex CLI command |
| `cli_parameters.gemini` | `list[str]` | `[]` | hot | Extra args appended to Gemini CLI command |
| `cli_parameters.grok` | `list[str]` | `[]` | hot | Extra args appended to Grok CLI command |
| `image` | `ImageConfig` | `(nested keys below)` | hot | Incoming image processing settings |
| `image.max_dimension` | `int` | `2000` | hot | Maximum width/height in pixels; images exceeding this are resized proportionally |
| `image.output_format` | `str` | `"webp"` | hot | Target image format (e.g. `webp`, `jpeg`, `png`) |
| `image.quality` | `int` | `85` | hot | Compression quality for lossy formats (WebP, JPEG) |
| `timeouts` | `TimeoutConfig` | `(nested keys below)` | restart | Path-specific timeout policy (`normal`, `background`, `subagent`) |
| `timeouts.normal` | `float` | `600.0` | restart | Default timeout for foreground chat turns (`normal` / `normal_streaming`) |
| `timeouts.background` | `float` | `1800.0` | restart | Timeout for named background sessions (`BackgroundObserver`) |
| `timeouts.subagent` | `float` | `3600.0` | restart | Reserved timeout bucket for sub-agent-specific paths |
| `timeouts.warning_intervals` | `list[float]` | `[60.0, 10.0]` | restart | Warning thresholds for `TimeoutController` |
| `timeouts.extend_on_activity` | `bool` | `true` | restart | Enables deadline extension when subprocess output is active |
| `timeouts.activity_extension` | `float` | `120.0` | restart | Seconds added per granted extension |
| `timeouts.max_extensions` | `int` | `3` | restart | Maximum activity-based extensions |
| `tasks` | `TasksConfig` | `(nested keys below)` | restart | Delegated background task system (`TaskHub`) |
| `tasks.enabled` | `bool` | `true` | restart | Enables shared delegated task system (`TaskHub`) |
| `tasks.max_parallel` | `int` | `5` | restart | Max concurrent running tasks per chat in `TaskHub` |
| `tasks.timeout_seconds` | `float` | `3600.0` | restart | Timeout per delegated task run |
| `tasks.finished_retention_hours` | `int` | `168` | restart | Age-based finished-task retention |
| `tasks.finished_keep_last` | `int` | `100` | restart | Maximum finished tasks retained; age and count limits apply independently, so fewer than this count may remain |
| `cron_delivery_retry` | `CronDeliveryRetryConfig` | `(nested keys below)` | restart | Retry preserved delivery without rerunning the agent |
| `cron_delivery_retry.enabled` | `bool` | `false` | restart | Retry delivery of a preserved cron result without rerunning the agent |
| `cron_delivery_retry.interval_seconds` | `int` | `300` | restart | Seconds between delivery retry attempts for one job |
| `cron_delivery_retry.max_attempts` | `int` | `12` | restart | Maximum retry attempts per preserved result; after that retrying stops and the result stays preserved with its failed delivery status |
| `cron_preflight` | `CronPreflightConfig` | `(nested keys below)` | restart | Task-local deterministic preflight gate |
| `cron_preflight.enabled` | `bool` | `false` | restart | Run the task folder's `scripts/preflight.py` before the cron agent; missing/failed/timed-out preflight fails open (the agent runs) |
| `cron_preflight.timeout_seconds` | `float` | `15.0` | restart | Time limit for the preflight script |
| `cron_preflight.skip_marker` | `str` | `"HEARTBEAT_OK"` | restart | Exit 0 with this as the last stdout line skips the agent run (recorded as `success:preflight`) |
| `scene` | `SceneConfig` | `(nested keys below)` | hot | Scene indicators and technical footer |
| `scene.seen_reaction` | `bool` | `false` | hot | Enables "seen" indicator on incoming messages (Telegram: emoji reaction, Matrix: read receipt) |
| `scene.status_reaction` | `bool` | `true` | hot | Telegram-only stage tracker on the user message while the turn runs; when enabled it wins over `seen_reaction` so both do not fight over the same emoji slot |
| `scene.technical_footer` | `bool` | `false` | hot | Appends a model/time footer to agent responses (token counts and cost are not shown) |
| `notifications` | `NotificationsConfig` | `(nested keys below)` | restart | Targeted startup notification routing |
| `notifications.startup_targets` | `list[NotificationTarget]` | `[]` | restart | When non-empty and containing at least one enabled target with `chat_id`, startup notices are routed only there |
| `notifications.startup_targets.enabled` | `bool` | `true` | restart | Target-level toggle |
| `notifications.startup_targets.chat_id` | `int or null` | `null` | restart | Telegram chat ID or Matrix room-mapped int |
| `notifications.startup_targets.topic_id` | `int or null` | `null` | restart | Telegram forum topic; ignored by Matrix |
| `transcription` | `TranscriptionConfig` | `(nested keys below)` | restart | External audio/video transcription command hooks |
| `transcription.audio_command` | `str` | `""` | restart | When set, exported as `DUCTOR_TRANSCRIBE_COMMAND` for `tools/media_tools/transcribe_audio.py` |
| `transcription.video_command` | `str` | `""` | restart | When set, exported as `DUCTOR_VIDEO_TRANSCRIBE_COMMAND` for `tools/media_tools/process_video.py` |
| `skills` | `SkillsConfig` | `(nested keys below)` | restart | Cross-tool skill sync toggles (global + per-provider) |
| `skills.sync_enabled` | `bool` | `true` | restart | Global toggle for cross-tool skill sync; when `false`, `sync_skills` returns early and no sync runs |
| `skills.sync` | `SkillSyncProviders` | `(nested keys below)` | restart | Per-provider sync toggles |
| `skills.sync.claude` | `bool` | `true` | restart | Include `~/.claude/skills` in cross-tool sync |
| `skills.sync.codex` | `bool` | `true` | restart | Include `~/.codex/skills` (or `$CODEX_HOME/skills`) in cross-tool sync |
| `skills.sync.gemini` | `bool` | `true` | restart | Include `~/.gemini/skills` in cross-tool sync |
| `skills.sync.grok` | `bool` | `true` | restart | Include `$GROK_HOME/skills` (default `~/.grok/skills`) in cross-tool sync |
| `user_timezone` | `str` | `""` | hot | IANA timezone used by cron/heartbeat/cleanup/session reset and queued-message send-time advisories |
| `language` | `str` | `"en"` | hot | Bot UI language for commands, status and chat system messages; schema default en, onboarding selects locale/explicit code and also speaks it |
| `group_mention_only` | `bool` | `false` | hot | Mention/reply gating in group rooms. Telegram: filter only (no auth bypass). Matrix: in non-DM rooms this bypasses `allowed_users` and uses room + mention/reply as gate |
| `interagent_port` | `int` | `8799` | restart | Port for internal localhost API (`InternalAgentAPI`) |
| `interagent_notify_received` | `bool` | `false` | restart | Opt in to async task receipt notices on the receiving agent; `--silent` still suppresses them |
| `interagent` | `InteragentConfig` | `(nested keys below)` | restart | Limits of inter-agent request handling (parallel sessions, queue wait); restart-required |
| `interagent.max_parallel_sessions` | `int` | `4` | restart | How many inter-agent sessions (lanes) of one **receiving** agent run at the same time; further sessions wait for a slot |
| `interagent.max_queue_wait_seconds` | `float` | `3600.0` | restart | Longest wait of one request (queue + preparation + wait for a restarting recipient) before it ends without starting; the execution limit (3600 s for async) counts separately from admission. Sync requests keep their ow... |
| `claude_system_prompt_snapshot` | `"off" or "on"` | `"off"` | restart | `"off"`: Claude renders its system prompt anew every turn (`--system-prompt-snapshot off`) and MAINMEMORY, roster and file blocks are assembled every turn; `"on"`: no flag, MAINMEMORY only on new sessions. Needs a Cla... |
| `transport` | `str` | `"telegram"` | restart | Main messaging transport: `"telegram"`, `"matrix"`, or `"slack"`; `"headless"` is sub-agent only |
| `transports` | `list[str]` | `[]` | restart | List of transports to run in parallel (e.g. `["telegram", "matrix"]`). When empty, falls back to single `transport` value. |
| `telegram_token` | `str` | `""` | restart | Telegram bot token (required when `transport=telegram`). Onboarding trims surrounding whitespace and rejects internal whitespace or a token outside ASCII digits:letters/digits/underscore/hyphen, without echoing the va... |
| `telegram_dm_topic_auto_name` | `bool` | `true` | hot | Telegram-only once-per-topic naming of confirmed implicit DM topics from received user messages, alongside the conversation; main and bot-backed subs. Subject-only output; a null or rejected model title names the topi... |
| `allowed_user_ids` | `list[int]` | `[]` | hot | Telegram user allowlist (applies in both private and group chats) |
| `allowed_group_ids` | `list[int]` | `[]` | hot | Telegram group allowlist (which groups the bot can operate in; default `[]` = no groups, fail-closed). In groups, both the group and the user must be allowlisted |
| `allowed_channel_ids` | `list[int]` | `[]` | restart | Telegram channel allowlist for join/audit behavior; unauthorized channels are auto-left |
| `matrix` | `MatrixConfig` | `(nested keys below)` | restart | Matrix homeserver connection (required when `transport=matrix`) |
| `matrix.homeserver` | `str` | `""` | restart | Matrix homeserver URL (e.g. `https://matrix.org`) |
| `matrix.user_id` | `str` | `""` | restart | Bot user ID (e.g. `@ductor:matrix.org`) |
| `matrix.password` | `str` | `""` | restart | Password for initial login |
| `matrix.access_token` | `str` | `""` | restart | Optional manual restore source; runtime normally persists credentials in the Matrix store |
| `matrix.device_id` | `str` | `""` | restart | Optional manual restore source paired with `access_token` |
| `matrix.allowed_rooms` | `list[str]` | `[]` | restart | Room IDs or aliases the bot may operate in |
| `matrix.allowed_users` | `list[str]` | `[]` | restart | Matrix user IDs allowed to interact |
| `matrix.store_path` | `str` | `"matrix_store"` | restart | Credential/room-mapping store directory, relative to the current agent home |
| `slack` | `SlackConfig` | `(nested keys below)` | restart | Socket Mode `bot_token`, `app_token`, `allowed_channels`, `allowed_users`; requires the Slack image extra |
| `slack.bot_token` | `str` | `""` | restart | Slack bot token (Socket Mode); keep it in the private config, never in logs |
| `slack.app_token` | `str` | `""` | restart | Slack app-level token used for Socket Mode |
| `slack.allowed_channels` | `list[str]` | `[]` | restart | Channel IDs the bot answers in; empty = no channel restriction (DMs always allowed) |
| `slack.allowed_users` | `list[str]` | `[]` | restart | Slack user IDs allowed to interact; empty = no user restriction |

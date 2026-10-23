# Config Directory

Runtime config lives here: `config.json`.
Edit only when the user asks for behavior changes.

## Safe Edit Workflow

1. Change only requested keys.
2. Preserve unrelated values and structure.
3. Never expose secrets (`telegram_token`, webhook tokens) in chat output.
4. Keep valid JSON.
5. Hot-reloadable settings take effect within seconds. These settings require `/restart`: `allowed_channel_ids`, `api`, `append_system_prompt_files`, `claude_system_prompt_snapshot`, `cron_delivery_retry`, `cron_preflight`, `ductor_home`, `gemini_api_key`, `interagent`, `interagent_notify_received`, `interagent_port`, `log_level`, `matrix`, `memory_compaction`, `memory_flush`, `memory_reflection`, `notifications`, `skills`, `slack`, `tasks`, `telegram_token`, `timeouts`, `transcription`, `transport`, `transports`, `webhooks`. Unknown/new keys also require restart until explicitly made hot-reloadable.

## Important Key Groups

### Model and Provider

- `provider`: `claude`, `codex`, `gemini`, or `grok`
- `model`: default model id
  - Claude models: `haiku`, `sonnet`, `opus`, `fable`
  - Codex models:
    - `gpt-5.5` - Newest/recommended frontier agentic coding model
    - `gpt-5.4` - Frontier agentic coding model
    - `gpt-5.4-mini` - Smaller, faster frontier coding model
    - `gpt-5.3-codex-spark` - Codex-optimized coding model
  - Gemini models:
    - `gemini-2.5-pro` - Balanced, most capable
    - `gemini-2.5-flash` - Fast and cost-effective
    - `gemini-3-pro-preview` - Next-gen preview
    - `gemini-3.1-pro-preview` - Latest pro preview
    - `gemini-3.1-flash-lite` - Lightweight fast preview
    - `gemini-3.5-flash` - Latest fast model
- `reasoning_effort`: reasoning depth for Codex and Claude
  - Codex: `low`, `medium`, `high`, `xhigh`
  - Claude: `low`, `medium`, `high`, `xhigh`, `max`
  - Change it for the active provider with the `/effort` command
- `permission_mode`: CLI permission behavior

### Time and Scheduling

- `user_timezone`: IANA timezone string (for example `Europe/Berlin`)
- `daily_reset_hour`: session reset boundary (in `user_timezone`)
- `heartbeat.quiet_start`, `heartbeat.quiet_end`: quiet hours (in `user_timezone`)
- `cleanup.check_hour`: daily cleanup hour (in `user_timezone`, not UTC)

If `user_timezone` is empty, runtime falls back to host timezone, then UTC.
For user-facing schedules, set `user_timezone` explicitly.

### Limits and Runtime

- `cli_timeout`
- `idle_timeout_minutes`
- `max_turns`, `max_budget_usd`, `max_session_messages`

### Streaming

- `streaming.enabled`
- `streaming.min_chars`, `streaming.max_chars`
- `streaming.idle_ms`, `streaming.edit_interval_seconds`
- `streaming.append_mode`, `streaming.sentence_break`

### Webhooks

- `webhooks.enabled`
- `webhooks.host`, `webhooks.port`
- `webhooks.token`
- `webhooks.max_body_bytes`, `webhooks.rate_limit_per_minute`

### Cleanup

- `cleanup.enabled`
- `cleanup.media_files_days`
- `cleanup.output_to_user_days`
- `cleanup.check_hour`

### System Prompt

- `append_system_prompt_files`: list of workspace-relative filenames whose
  contents are appended to the system prompt on every agent-driven turn
  (default `["PERSONA.md", "BEHAVIOR.md"]`; e.g. `["GLOSSARY.md", "STYLE_GUIDE.md"]`).
  An empty list injects nothing.
  Each agent reads its own workspace copy; missing files are skipped.

### File Sending Scope

- `file_access` controls what can be sent via `<file:...>`:
  - `all` (default)
  - `home`
  - `workspace`

### CLI Parameters

- `cli_parameters.claude`: List of extra CLI flags for Claude main agent
- `cli_parameters.codex`: List of extra CLI flags for Codex main agent (e.g., `[]`)
- `cli_parameters.gemini`: List of extra CLI flags for Gemini main agent

These parameters are appended to every CLI invocation for the respective provider.
Parameters are inserted before the `--` separator in commands.

**Example:**
```json
{
  "cli_parameters": {
    "codex": [],
    "gemini": ["--sandbox"]
  }
}
```

### Language

- `language`: UI language for bot messages — `en`, `de`, `nl`, `fr`, `ru`, `es`, `pt`
- Hot-reloadable: change without restart.

### Image Processing

- `image.max_dimension`: max width/height in pixels (default `2000`). Images larger than this are resized.
- `image.output_format`: target format — `webp` (default), `png`, or `jpeg`
- `image.quality`: compression quality 1-100 (default `85`)
- Incoming images from all transports are auto-converted after download.
- Hot-reloadable.

### Scene Indicators

- `scene.seen_reaction`: `true`/`false` (default `false`) — show a 👀 reaction on received messages (Telegram: emoji reaction, Matrix: read receipt)
- `scene.technical_footer`: `true`/`false` (default `false`) — append model name and duration to the final response
- Hot-reloadable.

### Heartbeat

- `heartbeat.enabled`: `true`/`false` — master switch
- `heartbeat.interval_minutes`: global tick interval for private chats (default `30`)
- `heartbeat.cooldown_minutes`: min gap between heartbeats per chat
- `heartbeat.quiet_start`, `heartbeat.quiet_end`: quiet hours (0-23, in `user_timezone`)
- `heartbeat.prompt`: the prompt sent to the agent during heartbeat
- `heartbeat.ack_token`: token the agent replies with when nothing to report (default `HEARTBEAT_OK`)
- `heartbeat.suspend_on_ack`: pause the exact transport/chat/topic after ACK until the Orchestrator accepts a user message for it (default `true`; requires non-blank global and target ACK tokens)
- `heartbeat.group_targets`: list of per-group/topic heartbeat targets, each with:
  - `enabled`: `true`/`false` — toggle this target without removing it
  - `chat_id`: Telegram group ID (use `null` for template entries that are skipped)
  - `topic_id`: forum topic ID within the group (or `null` for the whole group)
  - `prompt`: override prompt for this target (or `null` to use global)
  - `ack_token`: override ack token (or `null` to use global)
  - `interval_minutes`: independent interval for this target (or `null` to use global). Each target with its own interval runs in its own loop, independent of the global tick.
  - `quiet_start`, `quiet_end`: override quiet hours (or `null` to use global)

**Example:**
```json
{
  "heartbeat": {
    "enabled": true,
    "interval_minutes": 30,
    "group_targets": [
      {
        "enabled": true,
        "chat_id": -1001234567890,
        "topic_id": 5,
        "prompt": "Check server status and report anomalies.",
        "interval_minutes": 5
      },
      {
        "enabled": false,
        "chat_id": -1009876543210,
        "prompt": "Paused target — enable when ready."
      }
    ]
  }
}
```

Heartbeat is hot-reloadable. Changing `enabled` from false to true starts the observer without restart.

### Timeouts

- `timeouts.normal`: max seconds for a normal CLI call (default `600`)
- `timeouts.background`: max seconds for background tasks (default `1800`)
- `timeouts.subagent`: max seconds for sub-agent tasks (default `3600`)
- `timeouts.extend_on_activity`: auto-extend timeout when tool activity detected
- `timeouts.activity_extension`: seconds to extend per activity burst
- `timeouts.max_extensions`: max number of auto-extensions

### Tasks

- `tasks.enabled`: `true`/`false` — enable background task system
- `tasks.max_parallel`: max concurrent background tasks (default `5`)
- `tasks.timeout_seconds`: default timeout per task

### Access Control

- `allowed_user_ids`
- `allowed_group_ids`
- `group_mention_only`: when `true`, bot only responds in groups when @mentioned or replied to (applies to both text and media messages)
- `telegram_token`

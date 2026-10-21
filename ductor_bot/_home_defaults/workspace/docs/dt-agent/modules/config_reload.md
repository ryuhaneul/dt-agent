# config_reload.py

Centralized runtime config hot-reload watcher.

## File

- `ductor_bot/config_reload.py`: `ConfigReloader`, config diff/classification helpers

## Purpose

Avoid unnecessary restarts when safe `config.json` fields change.

`ConfigReloader` polls `~/.ductor/config/config.json` every 5 seconds, validates with `AgentConfig`, diffs top-level schema fields, and:

- applies hot-reloadable fields immediately,
- logs restart-required field changes through callback.

## Public pieces

- `diff_configs(old, new) -> dict[field, (old, new)]`
- `classify_changes(changes) -> (hot_values, restart_fields)`
- `ConfigReloader.start() / stop()`

## Hot-reloadable fields

- `model`, `provider`, `reasoning_effort`
- `cli_timeout`, `max_budget_usd`, `max_turns`, `max_session_messages`
- `idle_timeout_minutes`, `session_age_warning_hours`, `daily_reset_hour`, `daily_reset_enabled`
- `permission_mode`, `file_access`, `user_timezone`
- `streaming`, `heartbeat`, `cleanup`, `cli_parameters`, `claude_accounts`
- `project_roots`, `scene`, `image`, `language`
- `allowed_user_ids`, `allowed_group_ids`, `group_mention_only`
- `telegram_dm_topic_auto_name`, `persona_setup_prompt`, `memory_lint`

Important runtime nuance:

- `heartbeat` and `cleanup` values are hot-applied to their shared config objects.
- heartbeat reload resets suspend state and starts/stops its observer when `enabled` changes.
- cleanup has no start/stop reload hook: if disabled at startup, enabling it requires restart; an existing loop reads updated values and skips cycles while disabled.
- `user_timezone` reaches task instruction receipts too: TaskHub reads each agent's live `AgentConfig.user_timezone` (main fallback included) when it builds a receipt, so a hot-reloaded value applies to the next task create/resume/parent answer without recreating the agent stack.

## Restart-required fields

- transport/auth: `transport`, `transports`, `telegram_token`, `matrix`
- runtime topology: `api`, `webhooks`, `interagent_port`
- environment/core: `ductor_home`, `log_level`, `gemini_api_key`
- timeout/task policy: `timeouts`, `tasks`, `cron_delivery_retry`
- receipt notification: `interagent_notify_received` (default false)
- inter-agent limits and Claude prompt refresh: `interagent` (`max_parallel_sessions`, `max_queue_wait_seconds`), `claude_system_prompt_snapshot` (applied at the next start; a changed value only logs the restart-required warning)
- other non-hot fields include `slack`, `allowed_channel_ids`, `transcription`, `append_system_prompt_files`, `skills`
- there is no top-level `sync_enabled` field; use `skills.sync_enabled` and `skills.sync.{claude,codex,gemini,grok}`. ConfigReloader classifies the top-level `skills` block as restart-required, but the skill-sync toggles are read directly from `config.json` on every sync tick and take effect without a restart (see [skill system](skill_system.md))
- classification is schema-based over `AgentConfig` top-level fields: any changed top-level field not in the hot-reloadable set is reported as restart-required.

Timeout note:

- `timeouts.*` updates currently require restart.
- `tasks.*` updates currently require restart.
- runtime hot-reload still applies `cli_timeout` changes immediately.

## Wiring

`Orchestrator.create()` creates and starts the reloader with:

- `on_hot_reload` -> `Orchestrator._on_config_hot_reload(...)`
- `on_restart_needed` -> warning logger callback

`Orchestrator.shutdown()` stops it via `orchestrator/lifecycle.shutdown(...)` -> `ObserverManager.stop_all()`.

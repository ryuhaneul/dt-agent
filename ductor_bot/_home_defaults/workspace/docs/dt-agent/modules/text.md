# text/

Shared user-facing response text helpers used across bot and orchestrator layers.

## Files

- `text/response_format.py`: formatter helpers, status/error text builders, startup/recovery text

## Public API (`response_format.py`)

- `SEP`: shared section separator
- `fmt(*blocks)`: join non-empty blocks with blank lines
- `normalize_tool_name(name)`: normalizes shell-related tool names (`bash`, `powershell`, `cmd`, `sh`, `zsh`, `shell`) to `"Shell"` for display; `ToolSearch`/`SearchTool` become `"Search"`, `WebFetch` becomes `"Web fetch"`, and `WebSearch` becomes `"Web search"`; other names remain unchanged
- `classify_cli_error(raw)`
- `session_error_text(model, cli_detail="")`
- `new_session_text(provider)`
- `stop_text(killed: bool, provider)`
- `timeout_error_text(model, timeout_seconds)`
- `format_technical_footer(model_name, duration_ms)`: formats a `---`-separated footer line with the model name and the duration (when provided); token counts and cost are not shown
- `show_task_error(error)`, `show_run_result(status, text)`, `show_run_status(status)`: render the fixed reasons and results the product writes (task failure reasons, failed cron/webhook/background run texts, run statuses) in the configured language when a person sees them. They translate only a whole-text match and return anything else unchanged; stored values and agent input stay English
- `show_crash_error(text)`: renders the two Matrix login errors the product raises (exact text with the `RuntimeError: ` type prefix) in the configured language, keeping user ids, homeserver, configuration keys and the server's error text; any other error is returned unchanged. `system_status_label(status)`: the status line for a system status event (Telegram and Matrix streaming)
- `show_task_status`, `show_session_status`, `show_agent_status`, `show_agent_role`, `show_auth_status`: render the fixed status and role values of list screens, `/status` and `/diagnose`; only known values are translated, anything else is returned unchanged. `format_short_duration(seconds)` and `format_uptime(seconds)` render elapsed times with the `age.*` unit keys. `errors.ProductError(kind, english, **params)` is a `ValueError` whose `str()` stays English and whose `shown()` is the translated `product_error.<kind>` text.
- `show_cli_error(text)`: renders the fixed texts the product writes when a provider gives no message (`Codex failed before producing a final response.`, `Unknown Gemini error`, `Gemini exited with code N`, `Grok error`, `CLI error`) when the whole text matches; any other text is returned unchanged.
- `show_uptime(text)`: renders the uptime text of the internal API (`45s` / `2m` / `1.5h` / `2.0d`) with the `age.*` unit keys when the whole text has that form; any other text is returned unchanged.
- `startup_notification_text(kind)`
- `recovery_notification_text(kind, prompt_preview, session_name="")`

## Integration points

- `messenger/telegram/handlers.py`: `/new`, `/stop`
- `messenger/telegram/app.py`: help/info/restart and various user-facing blocks
- `messenger/telegram/file_browser.py`, `messenger/telegram/welcome.py`
- `orchestrator/commands.py`: `/status`, `/memory`, `/diagnose`, etc.
- `orchestrator/flows.py`: session error rendering and timeout-facing text
- `orchestrator/selectors/cron_selector.py`: cron selector text blocks
- `messenger/telegram/message_dispatch.py`: maps timeout status labels when emitted, appends technical footer when `config.scene.technical_footer=true`
- `messenger/matrix/bot.py`: appends technical footer for Matrix responses
- `messenger/telegram/edit_streaming.py`, `messenger/telegram/streaming.py`, `messenger/matrix/streaming.py`: use `normalize_tool_name()` for tool activity rendering
- `api/server.py`: uses `normalize_tool_name()` for `tool_activity` WebSocket events

## Behavior notes

- error hints are pattern-based and intentionally conservative
- session error text explicitly states that session context is preserved
- this module exposes `timeout_error_text`; `TimeoutController` warning/extension callbacks are not currently wired to system-status events

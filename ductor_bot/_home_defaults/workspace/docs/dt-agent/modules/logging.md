# logging (cross-cutting)

Context-aware logging infrastructure used by all runtime modules.

## Files

- `ductor_bot/log_context.py`: `ContextVar` state (`agent_name`, `operation`, `chat_id`, `topic`, `session_id`) + `ContextFilter`.
- `ductor_bot/logging_config.py`: root logger setup (console + rotating file via queue listener).

## Context Model

`ContextFilter` injects `record.ctx` into every log line as:

```text
[agent_name:operation:chat_id:topic:session_id_8]
```

Missing values are omitted.

Operation codes currently used:

- `msg`: incoming chat message (`SequentialMiddleware` / `MatrixBot`)
- `cb`: callback query (`TelegramBot._on_callback_query` / Matrix button handler)
- `cron`: cron execution (`CronObserver._execute_job`)
- `hb`: heartbeat run (`HeartbeatObserver._run_for_chat`)
- `wh`: webhook request / webhook wake dispatch
- `api`: direct API WebSocket session/message handling (`ApiServer._session_loop`, `_route_text_message`)
- `ia-async`: async inter-agent result processing (`TelegramBot.on_async_interagent_result`)
- `task`: task result/question delivery paths (`TelegramBot.on_task_result`, `on_task_question`)

`set_log_context()` updates the context for the current async task; child tasks inherit current context.

Multi-agent note:

- `AgentSupervisor` sets `agent_name` context (`main`, sub-agent name), so all agents share one log file with per-line agent attribution.

## Output Sinks

`setup_logging()` configures:

- colored console logs (`stderr`)
- rotating file logs in `~/.ductor/logs/agent.log` (`5MB`, `3` backups)

File logging uses `QueueHandler` + `QueueListener` so file I/O does not block the event loop.
`QueueListener` is stopped on reconfiguration and via `atexit` shutdown hook.

## Runtime Control

- default level is `INFO`
- `--verbose` forces `DEBUG`
- `config.log_level` is applied at startup when `--verbose` is not set
- `setup_logging(..., log_dir=None)` skips file logging (console only)
- noisy libraries are pinned to `WARNING` (`httpx`, `httpcore`, `telegram`, `telegram.ext`)

CLI command logs use `cli/_log_redact.py`: non-allowlisted environment assignment
values become `***`, while route/identity diagnostics remain visible. This is
command logging redaction, not a general filter for every log message.

Exceptions logged by `aiogram.dispatcher` retain only their type names, with exception messages and tracebacks removed from console and file output.

## Conventions in Codebase

- loggers are module-local: `logger = logging.getLogger(__name__)`
- log calls use lazy `%` formatting (not f-strings)
- exceptions use `logger.exception(...)` in `except` blocks

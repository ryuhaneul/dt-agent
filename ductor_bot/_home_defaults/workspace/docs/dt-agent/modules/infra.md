# Runtime infrastructure

`ductor_bot/infra/` contains PID locking, restart markers, recovery state,
atomic persistence, environment loading, and shared observer/task helpers.

## Files

- `pidlock.py`: process lock under the bot home
- `restart.py`: restart sentinel and exit code 42
- `inflight.py`, `recovery.py`, `startup_state.py`, `boot_id.py`:
  startup and interrupted-work state
- `json_store.py`, `atomic_io.py`, `fs.py`: atomic file operations
- `env_secrets.py`: user-defined environment values from the bot home
- `base_observer.py`, `base_task_observer.py`, `task_runner.py`:
  automation support
- `platform.py`, `process_tree.py`: subprocess helpers

## Restart recovery notices

`RecoveryPlanner` turns interrupted foreground turns (in-flight records, never older than twice the normal timeout and never of a group chat) and recovered named sessions into `RecoveryAction`s. Each action carries `topic_id` (`InflightTurn.topic_id`, `NamedSession.topic_id`). The primary Telegram startup sends the notice with `notify_origin(<its transport>, chat_id, text, topic_id=...)`, so it arrives in the topic the work ran in, and starts a named session's automatic follow-up in that topic. If a topic notice cannot be sent and the chat is the private chat of an allowed user, it is sent once more without the topic; for a group nothing else is tried (no group root, no `notify_all`). A success is logged as `Recovery notice sent kind=… chat=… topic=… fallback=…` (no text), a failure as a warning, and a failing notice never stops the startup. The consumption of the records is unchanged: the primary transport handles all of them and clears the in-flight records afterwards. Matrix and Slack startups send their notices as before. For a bot with several transports, the interrupted turns of the secondary transports are announced by the primary one as well (known limitation).

The Compose stack provides the container lifecycle. The bot and its provider
CLI subprocesses run inside the same bot container. State persists in the
host home mounted at `/ductor`. Paths written as `~/.ductor` in module
references denote the default host home; substitute the configured `DUCTOR_HOME`.
Sub-agents use `<main_home>/agents/<name>` and maintain their own runtime state.

`start_bot()` acquires the nonblocking home lock (`bot.lock`) before loading
config or writing startup state; another process holding that lock causes
refusal. If the configured home resolves to a different directory, it also
locks that home. The entrypoint leaves `bot.pid` intact. With the lock held, `run_bot()`
overwrites it with its own PID without checking whether the previous PID is
alive. SIGINT/SIGTERM cancel the supervisor run, await `stop_all()` and release
the lock. One bot container per `DUCTOR_HOME` is the supported deployment model.

Environment values loaded by `env_secrets.py` are passed to provider
subprocesses. Existing process environment values take precedence. Changes
to the home `.env` file are re-read on mtime changes for subsequent CLI
invocations only for keys not already in the process environment. Compose
`env_file` values already loaded into the container require container recreation
to change. Framework route/identity fields are normalized separately.

Credentials that tools load on demand are not part of this environment. Every CLI run only
gets `DUCTOR_SECRETS_FILE`, the path of the main home's `secrets.env`, unless the process
environment, the home `.env` or the caller already sets one.

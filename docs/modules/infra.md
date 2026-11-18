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
- `file_lock.py`: companion-file locks for concurrent history, import and credential updates
- `secret_store.py`: shared tool credential store, also used by web password/TOTP administration
- `base_observer.py`, `base_task_observer.py`, `task_runner.py`:
  automation support
- `platform.py`, `process_tree.py`: subprocess helpers

Optional history and web components reuse these atomic-write and locking helpers.
Their state lives under `DuctorPaths.chat_log_dir` and `webchat_dir`;
[recording retention](chat_log.md#retention-and-manual-cleanup) is separate from
the general cleanup observer.

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
gets `DUCTOR_SECRETS_FILE`, the path of the main home's tool store, unless the process
environment, the home `.env` or the caller already sets one.

## Encrypted tool secrets

`secret_vault.py` serializes vault operations with one asyncio lock. Setup migrates plaintext
only on success; atomic writes contain ciphertext, never a plaintext temporary copy. The
`secrets.env.enc` JSON envelope uses AES-256-GCM, a fresh nonce, and authenticated header/AAD
(version, KDF parameters, salt and nonce). The 32-byte key is derived with scrypt (N=32768,
r=8, p=1); authentication failure never includes a submitted key or value in its exception.
`secret_runtime.py` owns this vault for the supervisor process and persists only the non-secret
TTL in `config/secret_vault.json`. A restart drops unlocked material and creates a fresh tool token.

`InternalAgentAPI` checks `X-Ductor-Secrets-Token` with constant-time comparison; missing or wrong
tokens return 403. `GET /secrets/status` returns state/TTL/remaining time without names or values.
`GET /secrets/keys` returns names only while unlocked (423 `SECRETS_LOCKED` otherwise).
`GET /secrets/{key}` returns one requested value while unlocked; missing names use 404
`SECRET_NOT_SET`, locked access 423 `SECRETS_LOCKED`, and rate limits 429 with `retry_after`.
These responses use `Cache-Control: no-store`. Tokens are provided only through child process
environments, not the web bridge token file. A same-UID process can inspect other bot-owned
processes; the token is not an isolation boundary against that UID.

`tools/_secrets.py` preserves environment precedence and uses this API after setup; it never
falls back to plaintext when the `.enc` marker exists. Before setup it keeps legacy plaintext
reads. Its `secret_keys()` inventory helper reads only name fields or requests the names-only
API, never calls the value loader and never injects values. `tools/secret_tools/secret_status.py`
provides the safe state probe. See [configuration](../config.md#tool-credentials-secretsenv).

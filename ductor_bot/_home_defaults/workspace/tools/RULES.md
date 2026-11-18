# Tools Directory

This is the navigation index for workspace tools.

## Global Rules

- Prefer these tool scripts over manual JSON/file surgery.
- Run with `python3`.
- Quote every argument that contains spaces (a prompt, a message); put `--` before text that starts with `-`. `-h`/`--help` prints the usage and does nothing else; an unknown option or a surplus argument is an error and nothing runs.
- Normal successful runs are JSON-oriented; tutorial/help output may be plain text.
- Open the matching subfolder `CLAUDE.md` before non-trivial changes.

## Routing

- recurring tasks / schedules -> `cron_tools/CLAUDE.md`
- incoming HTTP triggers -> `webhook_tools/CLAUDE.md`
- file/media processing -> `media_tools/CLAUDE.md`
- sub-agent management (create/remove/list/ask) -> `agent_tools/CLAUDE.md`
- background tasks (delegate, list, cancel) -> the `task_tools/` rules file (`CLAUDE.md`, `AGENTS.md` or `GEMINI.md`)
- questions about DT Agent itself (usage, settings, commands, structure) -> `../docs/dt-agent/INDEX.md` first, then `python3 tools/memory_tools/memory_search.py --docs "question"`
- memory wiki search / lint -> the `memory_tools/` rules file (`CLAUDE.md`, `AGENTS.md` or `GEMINI.md`)
- custom user scripts -> `user_tools/CLAUDE.md`
- web browsing / screenshots -> `browser_tools/CLAUDE.md`
- GitHub over HTTPS (push with a stored token, git credential helper, one-time codes) -> `github_tools/CLAUDE.md`

- secret-store state (no values) -> `python3 tools/secret_tools/secret_status.py`

## Secrets and API Keys

Two files, by who reads the value:

- `$DUCTOR_HOME/.env`: provider keys that a CLI reads when it starts. Its values are injected into
  every CLI run. Standard dotenv syntax:

  ```env
  ANTHROPIC_API_KEY=SET_IN_PRIVATE_ENV
  export MY_VAR="quoted value"
  ```

- `~/.ductor/secrets.env` (mode 0600): credentials that a tool needs while it runs. It is not
  injected into CLI runs. The framework gives every run the path in `DUCTOR_SECRETS_FILE`
  (sub-agents use the main home's file) and a script asks for the keys it uses:

  ```python
  import sys
  sys.path.insert(0, "<workspace>/tools")
  from _secrets import load_secrets

  load_secrets("MY_API_KEY")  # name the keys; then read os.environ["MY_API_KEY"]
  ```

  Before master-key setup the store is plaintext `KEY=VALUE` (`#` comments; no `export`); `KEY=$OTHER_KEY` takes a value from that store. After setup the bot serves requested values from its in-memory vault. `load_secrets` preserves environment precedence and adds only requested, missing values to the consuming script's environment. Never bulk-source either file.

## Secrets and master keys

- Tool credentials in `secrets.env` may be encrypted with a master key. Provider startup keys in `.env` are separate and are not encrypted by this feature.
- Use `load_secrets("KEY")` from `tools/_secrets.py` for only the needed names; an existing environment value takes precedence. Never read/source `secrets.env`, its `.enc` file, or plaintext copies directly.
- Check `python3 tools/secret_tools/secret_status.py` before a task that needs secrets. It reports `unconfigured`, `locked`, or `unlocked`, TTL and remaining time without values. Restart always locks a configured store; TTL expiry locks it too; TTL 0 means unlimited until restart or manual lock.
- `SECRETS_LOCKED` means stop the secret-dependent work and report the code. Ask the owner to use `/masterkey unlock` in the main Telegram bot's private chat or unlock in the webchat modal. Do not bypass the lock, search for plaintext/backups, inspect process memory, or retrieve authentication tokens from files or other processes.
- `SECRETS_RATE_LIMITED` means wait for `retry_after`; do not loop, retry aggressively, or bypass the shared limit. `SECRET_NOT_SET` means the requested name is missing; never invent a value.
- Never ask for, accept, or process a master key in ordinary conversation, prompts, task files, or tool arguments. Master-key input belongs only in the `/masterkey` private input flow or the webchat modal. If locked, request the unlock action, not the key itself.
- Never cat, print, log or bulk-source credential files, including `.env`; inspect names without values.
- Never print, log, mirror, or persist secret values or master/derived keys in chat, memory, task descriptions, scripts, results, or temporary files. Use only needed values inside the consuming process. Key-name inventories and status tools must never fetch values.

## Bot Restart

To restart the bot (e.g. after config changes or recovery):

```bash
touch "$DUCTOR_HOME/restart-requested"
```

The bot picks up this marker within seconds and restarts cleanly.
No tool script needed — just create the file.

## Output and Memory

- Save user deliverables in `../output_to_user/`.
- Update `../memory_system/MAINMEMORY.md` silently for durable user facts/preferences.

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

- `secrets.env` may be master-key encrypted; `.env` is separate. Use `load_secrets("KEY")`; never read/source the store directly.
- Check `python3 tools/secret_tools/secret_status.py` for state and expiry.
- On `SECRETS_LOCKED`, stop the affected work and ask the owner for `/masterkey unlock` or webchat unlock. Never bypass the lock or search plaintext, backups, process memory or tokens.
- Never print or save secret values/keys. Never ask for or accept a master key in conversation; input belongs only in `/masterkey` private replies or webchat modals.
- Respect `SECRETS_RATE_LIMITED` and `retry_after`. Details: `docs/dt-agent/config.md`, **Tool credentials**.

## Output and Memory

- Save user deliverables in `../output_to_user/`.
- Update `../memory_system/MAINMEMORY.md` for durable user facts/preferences. Report memory saves, edits, or deletions in this reply: one line naming files and changes. Reads need no report.

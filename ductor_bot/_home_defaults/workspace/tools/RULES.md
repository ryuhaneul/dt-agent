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

  Values are not injected as environment variables, but an agent running as the same user can read the file; this protects against accidental exposure only.

  The file holds plain `KEY=VALUE` lines (`#` comments; no `export`). `KEY=$OTHER_KEY` takes the
  value of `OTHER_KEY` from the same file.

Existing environment variables are never overridden by values from either file. Never print,
`cat` or log a secrets file; look at key names only.

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

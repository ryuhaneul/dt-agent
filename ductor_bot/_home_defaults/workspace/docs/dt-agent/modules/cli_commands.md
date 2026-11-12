# CLI commands

`ductor_bot/__main__.py` dispatches the current CLI surface.
`cli_commands/lifecycle.py` starts the bot; `status.py` prints status and
usage; `agents.py` manages sub-agent registry entries. History and web administration
are implemented in `chat_log/cli.py` and `webchat/cli.py`.

| Command | Behavior |
|---|---|
| `ductor` | Start the configured bot |
| `ductor status` | Show runtime status and paths |
| `ductor help` or `ductor --help` | Show usage |
| `ductor --version` | Show package version |
| `ductor agents list/add/remove` | Manage sub-agent entries |
| `ductor chat-log status` | Show recording validation, history counts and queue state |
| `ductor chat-log import [--dry-run] [--undo]` | Import Claude/Codex transcripts or remove imported entries; actual import/undo requires active recording |
| `ductor chat-log prune --older-than N [--backups] [--dry-run]` | Prune old history and terminal/quarantined queue files; optionally web edit backups |
| `ductor webchat set-password` | Set a password interactively (at least 12 characters) and revoke web sessions |
| `ductor webchat reset-totp` / `clear-credentials` | Reset enrollment or remove password/TOTP credentials and revoke sessions |
| `ductor webchat check-config [--json]` | Validate history/web settings; exit 1 for validation errors, 0 otherwise |

History/web CLI usage errors exit 2. Manual prune works even when recording is off;
use `--dry-run` before deleting data. Credential changes take effect without a bot
restart. See [history](chat_log.md) and [web chat](webchat.md) for contracts and
`check-config --json` output. The host gateway helper is `scripts/webchat.py`,
not a `ductor` subcommand.

An unknown top-level argument prints usage and exits with status 2. Unknown
`agents` subcommands instead print the agent help and return normally; bare
`ductor agents` defaults to `list`. `agents add <name>` is an interactive
Telegram-sub setup helper; Matrix/headless definitions use `agents.json`. Starting with no
configuration prints the onboard instruction and exits with status 1.
The texts of `status`, `help` and `agents` (and the not-configured notice) use the `language` of the config in
`DUCTOR_HOME`; when the config is missing or unreadable they are English. Paths, command names and agent names are
not translated.
The bot process normally runs through Docker Compose; see
[installation](../installation.md). Chat commands such as `/stop` and
`/restart` are separate from CLI arguments.

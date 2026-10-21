# CLI commands

`ductor_bot/__main__.py` dispatches the current CLI surface.
`cli_commands/lifecycle.py` starts the bot; `status.py` prints status and
usage; `agents.py` manages sub-agent registry entries.

| Command | Behavior |
|---|---|
| `ductor` | Start the configured bot |
| `ductor status` | Show runtime status and paths |
| `ductor help` or `ductor --help` | Show usage |
| `ductor --version` | Show package version |
| `ductor agents list/add/remove` | Manage sub-agent entries |

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

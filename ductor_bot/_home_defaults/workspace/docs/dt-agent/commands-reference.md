# Commands reference

One place for every command surface of DT Agent. Settings are in [config-reference.md](config-reference.md); how things work is in [INDEX.md](INDEX.md).

## Chat commands

Typed in the chat (Telegram `/cmd`; Matrix also `!cmd`). They are chat messages, not shell commands. Which handler runs a command is fixed in code (`ductor_bot/messenger/commands.py`): direct (transport), orchestrator, or multi-agent (main only, needs the supervisor).

| Command | Purpose |
|---|---|
| `/start` | Welcome text with auth status and quick-start buttons |
| `/help` | Command overview |
| `/info` | Version and project info |
| `/new` | Reset the configured default-provider session for this chat/topic |
| `/reset` | Reset the currently active provider's session |
| `/stop` | Abort the running turn |
| `/interrupt` | Soft interrupt (SIGINT) of the active CLI turn |
| `/stop_all` | Abort work across all agents (supervisor) |
| `/model [name]` | Select model/provider; without an argument shows the selector |
| `/effort` | Select the provider's reasoning effort |
| `/status` | Session, model and runtime status |
| `/usage` | Telegram, all bots: read-only usage; Claude shows the last value from normal runs, Codex is queried on request, Grok is unsupported |
| `/accounts` | Telegram, main only: manage Claude slots and show each slot's last saved usage |
| `/memory` | Show main memory |
| `/session [prompt]` | Start a named background session (optional `@provider/model` directive); no argument shows help |
| `/sessions` | List and manage named sessions |
| `/tasks` | Background task list and management |
| `/cron` | Scheduled job overview |
| `/agent_commands` | Explain the multi-agent system and list its commands |
| `/showfiles` | Browse workspace files |
| `/diagnose` | Runtime diagnostics |
| `/restart` | Restart this agent (interrupts running work) |
| `/where` | Telegram group management: show where the bot is a member |
| `/leave` | Telegram group management: leave a group |
| `/agents` | Main only: agent overview |
| `/agent_start NAME` | Main only: start a configured agent |
| `/agent_stop NAME` | Main only: stop an agent |
| `/agent_restart NAME` | Main only: restart an agent |

Details: [modules/bot.md](modules/bot.md) (Telegram), [modules/orchestrator.md](modules/orchestrator.md), [modules/multiagent.md](modules/multiagent.md).

## ductor CLI

Inside the bot container: `ductor` (no arguments) starts the configured bot, `ductor --help` / `ductor help` prints usage, `ductor --version` prints the version, `-v` / `--verbose` turns on verbose logging, `ductor status` shows bot status, paths and agents, and `ductor agents [list|add NAME|remove NAME]` manages sub-agents (the add scaffold is interactive and Telegram-focused; edit `agents.json` for headless or Matrix agents). Never start a second no-argument `ductor` process against an active home. Details: [modules/cli_commands.md](modules/cli_commands.md).

## Host scripts (operator only)

These run on the host in the repository checkout, not inside the agent container; an agent cannot run them. Running host scripts is the operator's job: ask the operator to run them.

| Script | Purpose |
|---|---|
| `scripts/onboard.sh` / `onboard.py` | First-time (or repeat) setup: writes config, env files, Compose override; every option is in the onboarding options table of `installation.md` |
| `scripts/login.sh [provider]` | Log a provider CLI (claude, codex, gemini, grok) in inside the running container; `claude [slot]` for extra Claude accounts |
| `scripts/logout.sh [provider]` | Log out; Claude slot options `--list`, `--disable SLOT`, `--remove SLOT` |
| `scripts/status.sh` | Container and CLI-auth probes |
| `scripts/rebuild.sh` | Rebuild images and recreate containers without pulling source |
| `scripts/update.sh` | Fast-forward the checkout, rebuild and swap containers |
| `scripts/verify-clean-clone.sh` | Isolated verification of a committed ref (developer tool) |

Details: [installation.md](installation.md).

## Workspace tools

Run from the agent workspace with `python3 tools/<dir>/<script>`; each directory has a `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` with full usage. Output is mostly JSON.

| Directory | Scripts | Purpose |
|---|---|---|
| `tools/cron_tools/` | `cron_add.py`, `cron_edit.py`, `cron_list.py`, `cron_remove.py`, `cron_time.py` | Scheduled jobs and their task folders |
| `tools/webhook_tools/` | `webhook_add.py`, `webhook_edit.py`, `webhook_list.py`, `webhook_remove.py`, `webhook_rotate_token.py`, `webhook_test.py` | Incoming HTTP triggers |
| `tools/task_tools/` | `create_task.py`, `resume_task.py`, `list_tasks.py`, `cancel_task.py`, `delete_task.py`, `ask_parent.py` | Background tasks (delegate long work, follow up, ask the parent) |
| `tools/agent_tools/` | `ask_agent.py`, `ask_agent_async.py`, `create_agent.py`, `list_agents.py`, `remove_agent.py`, `edit_shared_knowledge.py` | Sub-agents, delegation and shared knowledge (`create_agent.py`, `remove_agent.py`, `list_agents.py` are management tools operated from main: an operating convention, not a separate caller permission check) |
| `tools/memory_tools/` | `memory_search.py`, `memory_lint.py`, `memory_parser.py` | Memory wiki search and lint; `memory_search.py --docs` searches this reference |
| `tools/media_tools/` | `file_info.py`, `list_files.py`, `process_video.py`, `read_document.py`, `transcribe_audio.py` | Received media and documents |
| `tools/browser_tools/` | `browser.py`, `fill_secret.py` | Browser container control (screenshots, browsing); `fill_secret.py` fills a form field from a stored secret |
| `tools/github_tools/` | `totp.py` | One-time codes from a stored seed; `tools/git_push.py` and `tools/git_credential_from_ductor_env.sh` push to GitHub over HTTPS with a stored token |
| `tools/user_tools/` | your own scripts | User-owned, never overwritten |

Tool scripts are framework-managed: edits to bundled tool scripts are replaced on update (a `.bak` copy is kept). Put your own scripts in `tools/user_tools/`.

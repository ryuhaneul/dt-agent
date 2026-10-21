[한국어](README.ko.md) · English

# DT Agent

DT Agent is built on the core behavior of [PleasePrompto/ductor](https://github.com/PleasePrompto/ductor). So many changes tuned to a personal environment and preferences accumulated that it became a separate repository. If you like these changes, use DT Agent; if not, or if you are unsure, start with the original ductor.

All code changed or added relative to the original ductor was written by AI.

ductor keeps framework-level restrictions on agent behavior to a minimum, so results depend heavily on the model's own judgment. Using a high-end model is strongly recommended.

The author has long run Claude Opus as the main agent and GPT Sol as sub-agents, and much of this repository is likely tuned for that combination.

Internal package, environment-variable, path and container names (`ductor_bot`, `DUCTOR_HOME`, `~/.ductor`, `ductor-bot` and similar) are kept unchanged for compatibility with the original.

DT Agent is a modified fork maintained at [github.com/ryuhaneul/dt-agent](https://github.com/ryuhaneul/dt-agent), not an official upstream distribution. It is based on ductor v0.20.1. Original copyright: PleasePrompto, 2025–2026; MIT license.

Provider CLIs run in the bot container; Chromium runs in a separate browser container. DT Agent also changes delegation routing, interruption/recovery, onboarding, optional dependencies and safe diagnostics. See the complete [change list](docs/CHANGES.md).

## 1. Requirements and a clean server

Use a Linux server with Docker Engine, the **Compose plugin v2 or newer**, Git, Bash, Python **3.9+** for host onboarding and a timezone database. Bot/development Python is **3.11+**, installed in the image. The bot image is Debian Bookworm/Python 3.11 with Node.js 24; the browser image is Playwright `v1.59.1-noble`. Linux x86-64 is the deployment path documented here; other architectures/OSes need an independent image/CLI compatibility check.

The image user is built with the onboarding account’s UID/GID, recorded as `DUCTOR_UID` and `DUCTOR_GID` in the repository root `.env` (both default to 1000 when absent). Use a non-root login account with permission to use Docker. Rebuild the image when changing accounts. Do not run onboarding as root.

No fixed minimum for RAM or disk is specified. Allow storage for two images, image-build layers, workspace/auth/logs and backups. Check free space before builds and rebuilds.

### Ubuntu 24.04

For a fresh Ubuntu 24.04 server, install the host packages below; install Docker Engine and `docker-compose-plugin` using the [official Docker Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/) and [Compose plugin instructions](https://docs.docker.com/compose/install/linux/). Those vendor instructions cover repository keys, daemon setup and user permissions. Complete them before onboarding. Choose the Ubuntu or RHEL-family procedure below for your host.

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv tzdata ca-certificates curl
id -u
df -h .
```

If Docker is not installed, the following Ubuntu APT-repository setup supplies Engine and its Compose plugin; it follows the vendor pages linked above. Use a fresh supported Ubuntu host, then configure Docker permission for your login user and reconnect before running the checks.

```bash
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
UBUNTU_SUITE=$(. /etc/os-release; printf '%s' "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
MACHINE_ARCH=$(dpkg --print-architecture)
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' "$MACHINE_ARCH" "$UBUNTU_SUITE" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
# Reconnect your login session before continuing; Docker access grants host control.
docker version
docker compose version
```

### RHEL family: Rocky, Alma and RHEL 8/9/10

Follow the RPM repository procedure in [Docker’s official RHEL installation guide](https://docs.docker.com/engine/install/rhel/). [Rocky’s official guide](https://docs.rockylinux.org/gemstones/containers/docker/) uses the same `linux/rhel/docker-ce.repo`. Install the host packages and check that `python3` is 3.9 or newer. RHEL-family Python includes `venv`; a separate `python3-venv` package is not needed. If the host defaults to older Python, install the distribution’s Python 3.9+ and ensure this shell’s `python3` invokes it before continuing.

```bash
sudo dnf install git python3 tzdata ca-certificates curl
python3 --version
id -u
df -h .
```

On RHEL-family 8, the default Python can be older than 3.9. [Red Hat documents the parallel `python39` package](https://developers.redhat.com/blog/install-python3-rhel). The following creates an isolated host-script environment without changing the system Python; activate it again in each new shell before onboarding or other host Python scripts. Onboarding uses the standard library, so this environment does not need pip.

```bash
# RHEL-family 8: use this if the default python3 is older than 3.9.
sudo dnf install python39
python3.9 -m venv --without-pip "$HOME/.local/share/ductor-host-venv"
source "$HOME/.local/share/ductor-host-venv/bin/activate"
python3 --version
```

The removal command below also affects existing Podman and runc use; check affected workloads before running it on a server that uses them. Grant Docker access, reconnect your login session, and run the checks.

```bash
sudo dnf remove docker docker-client docker-client-latest docker-common docker-latest docker-latest-logrotate docker-logrotate docker-engine podman runc
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo dnf install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
# Reconnect your login session before continuing; Docker access grants host control.
docker version
docker compose version
```

Prepare a Telegram bot token and your numeric Telegram user ID. In a group, you need both the group ID and the user allowlist; for forum topics give the main bot the necessary topic permissions. At least one provider needs a valid account/login or supported API-key authentication: Claude Code, Codex, Gemini CLI or Grok. These CLIs are installed by the image; subscriptions/usage and OAuth authorization are your responsibility. Internet access is needed for image/package downloads, messaging and provider APIs. Optional Matrix needs a homeserver, bot user, password **or** access token plus device ID, and permitted rooms/users. Slack needs its own bot/app tokens and channel/user allowlist. Onboarding currently requires Telegram even when adding Matrix/Slack.

## 2. Clone, onboard, build, authenticate and talk

Clone the repository as your deployment user and run subsequent Compose commands from the repository root.

```bash
git clone https://github.com/ryuhaneul/dt-agent.git ductor
cd ductor
git rev-parse HEAD
./scripts/onboard.sh
```

`git rev-parse HEAD` is a read-only query that records the exact installed version; it changes nothing. Keep the SHA for issue reports and as a rollback reference after an update.

Interactive onboarding runs only when stdin **and** stdout are TTYs. It asks for the host home (default `~/.ductor`), Telegram token (hidden input), allowed user IDs, timezone (default **detected host system timezone, UTC on failure**), bot UI language (server locale default, unsupported locale falls back to `en`), extra transports, API/webhook and bind address. Groups are supplied with `--allowed-groups`; they are not an interactive question. Secret input is trimmed at both ends. Telegram tokens must have an ASCII numeric prefix, a colon and a nonempty letters/digits/underscore/hyphen suffix; internal whitespace and other characters are rejected before files are written, without echoing the token. On first installation, blank token/user inputs are invalid and non-TTY execution requires real inputs. On reruns, saved values are the question defaults; Enter keeps them, including the hidden token. Noninteractive reruns reuse saved credentials and user IDs when no replacement is supplied.

For noninteractive setup, create a private token file without putting the token in shell history, then supply its **path**. Replace `123456789` with your real numeric user ID.

```bash
umask 077
read -r -s -p "Telegram bot token: " BOT_TOKEN; printf "\n"
printf "%s\n" "$BOT_TOKEN" > "$HOME/ductor-token"
unset BOT_TOKEN
chmod 600 "$HOME/ductor-token"
INSTALL_HOME="$HOME/.ductor"
./scripts/onboard.sh --no-prompt --home "$INSTALL_HOME" \
  --telegram-token-file "$HOME/ductor-token" --allowed-users 123456789 \
  --allowed-groups=-1001234567890 --tz UTC --language en --transport none \
  --no-api --no-webhook --bind 127.0.0.1
```

Onboarding saves the chosen host home in repository `.env`. Login/logout/update read that `DUCTOR_HOME` key automatically; no shell export is needed. A nonempty environment DUCTOR_HOME overrides the saved value; without either, the default is `~/.ductor`. The config value inside the bot stays `/ductor`, not the host path.

Choose **one** onboarding route above. The unattended example permits the example group; omit that option for DM-only operation on a new home. On reruns, omitted group options keep the saved groups; an explicit empty --allowed-groups value clears them. Onboarding creates root `.env` (Compose settings), home `config/config.json` (including messaging credentials), home `.env` (provider-key placeholders), home `secrets.env` (an empty store for tool credentials, created only when missing), home `.env.shared` (generated browser token and TZ), `.cli-auth/` and `logs/`. Enabled API/webhook listeners generate root `compose.override.yml`; disabled listeners remove that generated override. Files are private (0600); `.cli-auth/` is 0700. It does not build, start, log in, or seed the full workspace. Existing files are kept unless `--force` is used, but permissions may be tightened; existing secret symlinks are rejected. The exception is `secrets.env`: when it already exists, even as a symlink or with looser permissions, it is left exactly as it is, with or without `--force`. `--force` updates the existing config from supplied options/environment values or interactive answers, preserving other values, including groups, heartbeat settings, listener ports and custom keys. Missing example keys are added recursively without overwriting saved values. Groups change only with --allowed-groups/ONBOARD_ALLOWED_GROUPS; there is no group question. Back up before --force. To reset configuration, delete DUCTOR_HOME/config/config.json and rerun onboarding.

Onboarding finishes with the following next steps (the login command uses the provider in the saved config; the default is Claude; the example selected `en`):

```text
Bot language: en
Onboarding configuration ready. Next steps:
  docker compose up -d --build
  docker compose logs --tail=100 ductor-bot
  Check for Bot online before provider login.
  ./scripts/login.sh claude
  ./scripts/status.sh
To reset configuration, delete DUCTOR_HOME/config/config.json and rerun onboarding.
```

Run the listed commands from the repository root in that order. Wait for `Bot online` in the logs before provider login; the browser must become healthy before the bot starts. Complete the chosen CLI's OAuth/device flow in the **running** bot container, then check status. If needed, follow logs with `docker compose logs -f ductor-bot`.

Use Ctrl-C to stop log following; it does not stop the bot. In your allowlisted Telegram private chat, press **Start**, send `/status`, then a normal message such as “Say hello and tell me which model you are using.” For a group, add the main bot, set the actual group/user allowlists, and send a message in the intended topic. DM topics also keep their topic identity where Telegram provides it. Authenticate Codex/Gemini/Grok only if used; change the model/provider with `/model` or config and confirm with `/status`.

### First conversation: persona and behavior

Each agent workspace gets two seed-once files (never overwritten once they exist): `PERSONA.md`, a blank form (name, role, how to address you, tone, response language) marked as unset, and `BEHAVIOR.md`, a short set of essential always-on principles. Both are added to the system prompt (`append_system_prompt_files`); a persona that is still unset is not injected. While `PERSONA.md` carries its unset marker, the framework adds a short, invisible setup hint to your messages (first turn of a session, then every sixth message): the agent asks about itself and about you, fills in `PERSONA.md`, removes the marker, saves facts about you in `memory_system/` and adds to `BEHAVIOR.md` only what must always hold. Turn the hint off with `"persona_setup_prompt": false` (hot-reloaded). With the Claude CLI, edits to `PERSONA.md`, `BEHAVIOR.md` and `MAINMEMORY.md` reach the very next turn (`claude_system_prompt_snapshot: "off"`, the default; `"on"` brings back the old behavior where a resumed session keeps the text it started with, and other providers keep their own timing; [details](docs/modules/cli.md#claude-system-prompt-snapshot)). `MAINMEMORY.md` stays a short list of essentials plus an index; details go into topic files under `memory_system/`. Anything that matters regardless of the model is saved there; a model's built-in memory (e.g. Claude auto-memory) is only for notes that apply to that model alone. Every agent workspace also carries a framework-managed reference of DT Agent itself (usage, every config key, commands, structure) in `docs/dt-agent/` (start at `INDEX.md`; search it with `memory_search.py --docs`); it is regenerated by `python3 scripts/build_agent_docs.py` and replaced on every update. Agents look pages up with `tools/memory_tools/memory_search.py` and check the wiki with `memory_lint.py check` after editing it. Every period (`memory_lint`: `mode`, `every_hours` 168, `report` `topic`/`dm`/`off`) the framework runs the same check itself. With `mode` `structure` (the default) a clean wiki costs nothing, and when the check finds problems your Telegram bot runs one agent turn that fixes what is safe and tells you the rest. With `full` it runs one agent turn every period, even for a clean wiki, that also looks for duplicates and contradictions, checks once a month for stale rules and reports; its instructions are the file `tools/memory_tools/LINT_FULL.md`, which you own and which updates never overwrite. `off` runs nothing. Turns run in a dedicated "Memory Lint" topic if the bot's private chat is in topic mode, otherwise in the DM.

## 3. All onboarding options and extra transports

| Option | Behavior |
| --- | --- |
| `-h`, `--help` | Show argparse help; wrapper first checks Python/Docker/Compose. |
| `--no-prompt` | Disable TTY questions; required Telegram token/users still enforced; saved values can be reused. |
| `--home PATH` | Host persistent home; expands ~ and resolves to an absolute path. |
| `--telegram-token-file FILE` | Read and trim one nonempty line; validate the same Telegram token format as hidden input. Never pass a token literal as an option. |
| `--allowed-users IDS`, `--allowed-groups IDS` | Comma-separated numeric IDs; users required, groups optional. Use = for negative group lists. |
| `--tz ZONE` | IANA timezone; omitted preserves the saved value; on new installs detect system timezone, falling back to UTC. |
| `--language CODE` | Bot UI language; explicit code must be supported. Saved value on reruns; server locale default on new installs, then en. |
| `--transport none|matrix|slack` | Repeatable extra transports; Telegram retained. none cannot combine with extras. |
| `--matrix-homeserver URL`, `--matrix-user-id ID` | Required when adding Matrix. |
| `--matrix-password-file FILE`, `--matrix-access-token-file FILE` | Exactly one when adding Matrix; credentials stay in private config. |
| `--matrix-device-id ID` | Required with Matrix access token. |
| `--matrix-allowed-rooms IDS`, `--matrix-allowed-users IDS` | Comma-separated strings; at least one allowlist must be nonempty. |
| `--slack-bot-token-file FILE`, `--slack-app-token-file FILE` | Both required when adding Slack. |
| `--slack-allowed-users IDS`, `--slack-allowed-channels IDS` | Comma-separated strings; at least one allowlist required. |
| `--api` / `--no-api`, `--webhook` / `--no-webhook` | Enable/disable listeners; default disabled. API also adds api build extra. |
| `--bind IP` | Numeric bind IP for published API/webhook ports; default 127.0.0.1. |
| `--force` | Allow file updates. Config preserves unprovided values and fills missing defaults; env files can still be replaced. |

Precedence is explicit arguments → environment → default/TTY question. Environment equivalents are `DUCTOR_HOME`; `ONBOARD_TELEGRAM_TOKEN_FILE`, `ONBOARD_ALLOWED_USERS`, `ONBOARD_ALLOWED_GROUPS`, `ONBOARD_TZ`, `ONBOARD_LANGUAGE`, `ONBOARD_TRANSPORTS`; `ONBOARD_MATRIX_HOMESERVER`, `ONBOARD_MATRIX_USER_ID`, `ONBOARD_MATRIX_PASSWORD_FILE`, `ONBOARD_MATRIX_ACCESS_TOKEN_FILE`, `ONBOARD_MATRIX_DEVICE_ID`, `ONBOARD_MATRIX_ALLOWED_ROOMS`, `ONBOARD_MATRIX_ALLOWED_USERS`; `ONBOARD_SLACK_BOT_TOKEN_FILE`, `ONBOARD_SLACK_APP_TOKEN_FILE`, `ONBOARD_SLACK_ALLOWED_USERS`, `ONBOARD_SLACK_ALLOWED_CHANNELS`; `ONBOARD_API`, `ONBOARD_WEBHOOK`, `ONBOARD_BIND`. Boolean env values accept true/false, yes/no, 1/0, y/n. Transports are comma-separated in the environment.

Timezone precedence is `--tz` → `ONBOARD_TZ` → saved timezone on reruns → host system timezone → `UTC`; system detection tries a ZoneInfo-valid `TZ`, `/etc/timezone`, then the `/etc/localtime` symlink path after `zoneinfo/`. The interactive question displays the saved or detected default.

Language precedence is `--language` → `ONBOARD_LANGUAGE` → saved language on reruns → server locale → `en`. Locale detection uses the first nonempty `LC_ALL`, `LC_MESSAGES`, or `LANG`, extracts the language part (for example, `ko_KR.UTF-8` → `ko`), and falls back to `en` if unsupported. Bundled languages are `de`, `en`, `es`, `fr`, `id`, `ko`, `nl`, `pt`, `ru`; an unsupported explicit option/environment/question answer exits with an error before writes. Question 5 displays the saved or detected default; Enter retains it. The decided language applies to the bot UI messages and also to the onboarding questions, errors and completion notice and to the preflight messages of `onboard.sh` (python3, docker or compose missing, root execution or Python older than 3.9); answering question 5 changes the texts that follow. Commands, options, language codes and paths are not translated, while command-line errors and the `--help` output use the chosen language. Existing config is preserved without `--force`, so the completion notice reports its saved language. Option names are read in full: an abbreviation such as `--lang=ko` is an unknown-argument error in the decided language (exit 2, nothing written).

Add Matrix on a **new** home with the following example (replace all example IDs/paths). It adds Matrix alongside Telegram. To run Matrix only afterwards, edit config `transports` to `["matrix"]` and `transport` to `"matrix"`. Access-token mode additionally requires a matching `--matrix-device-id`. Room/user authorization differs from Telegram; consult [Matrix setup](docs/matrix-setup.md) and current [config](docs/config.md).

```bash
./scripts/onboard.sh --no-prompt --home "$HOME/.ductor-matrix" \
  --telegram-token-file "$HOME/ductor-token" --allowed-users 123456789 \
  --transport matrix --matrix-homeserver https://matrix.example.org \
  --matrix-user-id @ductor:matrix.example.org \
  --matrix-password-file "$HOME/matrix-password" \
  --matrix-allowed-rooms '!room:matrix.example.org'
docker compose up -d --build
```

On existing homes, changing onboarding arguments alone does not overwrite saved config/root env. Edit the saved config and root `DUCTOR_EXTRAS` together; rerun onboarding without `--force` to regenerate ports from saved config. To apply supplied config changes, use --force after backing up env files; omitted settings and saved listener ports are preserved. Changing extras requires a rebuild. API defaults to 8741 and webhook to 8742; only enabled main listeners are published. Their authentication tokens start empty and are generated/persisted when the enabled listeners start; inspect the saved private config locally or set private `api.token` / `webhooks.token` explicitly. Follow their authorization/exposure rules in config docs. Browser token is separate.

## 4. Operator scripts and provider slots

These are host scripts. Run from the repository root. `login.sh` addresses the Compose service; `logout.sh`/`status.sh` use `DUCTOR_CONTAINER` (default `ductor-bot`). `update.sh` uses fixed `ductor-bot`. Login/logout/update resolve host home as nonempty environment `DUCTOR_HOME` → the repository-root `.env` key (relative to the script location, with quotes handled) → `~/.ductor`. They read only that key and never source the env file. `_home.sh` is their internal shared helper, not an operator command. Scripts without an option parser do **not** have a functional `--help` flag: status/rebuild/update execute their normal operation even if passed extra arguments.

| Script / interface | Behavior and example |
| --- | --- |
| `onboard.sh` / `onboard.py` | Wrapper prerequisites / private config writer; options in §3. Example: `./scripts/onboard.sh --help`. |
| `login.sh [provider]` | No args: Claude → Codex → Gemini → Grok; failed credential checks warn and continue, with a final summary. Providers claude/codex/gemini/grok; -h/--help/help. Example: `./scripts/login.sh codex`. |
| `login.sh claude [slot]` | Default/default/claude → claude; alt → claude-alt; full claude-* accepted. --list/-l/list lists slots (creates shared dir); --repair/repair migrates noncredential entries to shared symlinks with backups; claude -h/--help shows help. Example: `./scripts/login.sh claude alt`. |
| `logout.sh [provider]` | No args logs out all; claude/codex/gemini/grok, -h/--help. Claude --list (authentication, priority and remaining cooldown; no container needed), --prefer SLOT, --priority A,B,..., --clear-cooldown SLOT or all (management requires a running container), --disable SLOT (config only), --remove SLOT or bare SLOT (config + renamed credential backups). Example: `./scripts/logout.sh claude --disable alt`. Codex removes stored auth files; Gemini removes `oauth_creds.json` and prints key-removal and restart instructions. |
| `status.sh` | No parsed options; container + CLI-auth probes. Example: `./scripts/status.sh`. Gemini probe checks API-key env or the OAuth credential file. |
| `rebuild.sh` | No parsed options; build --pull with CACHEBUST, up -d, ps. No source pull. Example: `./scripts/rebuild.sh`. Replaces containers and refreshes mutable CLI versions. |
| `update.sh` | No parsed options; tracked-clean check, fetch origin, ff-only pull of tracking branch, prompts, rebuild/swap. Example: `./scripts/update.sh`. Can leave pulled source with old running image after declining swap; no automatic rollback. Asks again right before the swap when a turn or task is running; declining keeps the new image built and the old container running. |

Authenticate each provider separately when needed. Gemini starts an interactive CLI; when `GEMINI_API_KEY` or `GOOGLE_API_KEY` is set in the container, the OAuth file check is skipped. Google OAuth on a remote server may require SSH loopback forwarding as shown by that CLI. Grok uses device auth; Codex script invokes `codex login --device-auth`. Claude alternate slots share project sessions and noncredential settings with the default slot; credentials remain separate. After successful login with at least two authenticated Claude slots, the script proposes an ordered `claude_accounts` list, for example `["claude", "claude-alt"]`. The registration prompt defaults to Y: empty input, y/yes, and non-TTY execution merge newly authenticated slots into the latest config.json list; n/no skips registration. Other settings and existing file permissions are preserved. A failed update exits nonzero without the Updated notice. Priority changes apply to main within seconds and to sub-agents on their next start; cooldown clearing applies to all on the next call. `--disable`/`--remove` edits are atomic and the account list is re-read for subsequent calls; existing calls keep their current slot list.

The standard image installs `/etc/claude-code/managed-settings.d/10-dt-agent-retention.json` with `cleanupPeriodDays: 36500`, covering every in-container Claude invocation regardless of slot (bot calls, streaming, one-shot tasks, sub-agents, login and direct execution). Higher-priority managed sources such as organization server policies may supersede this local policy; administrators can deliberately override the value with a later-named drop-in in the same directory ([how Claude Code combines managed sources](https://code.claude.com/docs/en/managed-settings#how-claude-code-combines-managed-sources)).
Conversation history keeps accumulating under `DUCTOR_HOME` and uses disk space; `CleanupObserver` does not remove it.
Because this is a managed setting, user settings cannot shorten retention; add a later-named drop-in and rebuild the image to use a shorter period.

```bash
./scripts/login.sh claude --list
./scripts/login.sh claude alt
./scripts/login.sh codex
./scripts/login.sh gemini
./scripts/login.sh grok
./scripts/logout.sh --help
```

The in-container product CLI has no arguments (start configured bot), --help/help, --version, --verbose, status, and agents [list|add NAME|remove NAME]. The add scaffold is Telegram-focused and interactive; agents.json supports headless/Matrix definitions directly. Never launch a second no-argument ductor process against the active home just to inspect it.

```bash
docker compose exec ductor-bot ductor --help
docker compose exec ductor-bot ductor --version
docker compose exec ductor-bot ductor status
docker compose exec ductor-bot ductor agents list
```

## 5. Chat commands: Telegram and Matrix

These are chat messages, **not shell commands**. Matrix accepts !command as well as /command. UI details differ: Telegram buttons and Matrix reactions/text menus. `/interrupt` is intercepted by Telegram middleware rather than a normal registered handler. Main-only agent lifecycle commands need a running supervisor.

| Chat command | Purpose |
| --- | --- |
| `/start` | Start/help; Telegram Start button. |
| `/help` | Command help. |
| `/info` | System/workspace information. |
| `/new` | Reset configured default-provider session for this chat/topic. |
| `/reset` | Reset currently active provider session. |
| `/stop` | Abort running work in this topic and discard the messages waiting behind it (the reply says how many). |
| `/interrupt` | Soft interrupt (SIGINT) of active CLI work. |
| `/stop_all` | Abort work across agents via supervisor where available. |
| `/model [name]` | Select model/provider; no argument shows selection UI. |
| `/accounts` | Main Telegram bot only: view Claude slot authentication, priority, cooldowns and saved usage; reorder slots or clear cooldowns. |
| `/usage` | All Telegram agent bots: read-only usage. Claude shows the last values received during normal runs; Codex is queried on command or refresh; Grok automatic queries are unsupported. |
| `/effort` | Select provider reasoning effort. |
| `/status` | Current session/model/runtime status. |
| `/secret` | Main Telegram bot only, in an allowed user’s private chat: set, list, delete or cancel tool secrets; send values as replies to the input request. |
| `/memory` | Read main memory. |
| `/session [prompt]` | Submit a named background session; optional @provider/model/session directive; no args shows help. |
| `/sessions` | List/manage named sessions. |
| `/tasks` | Background task list/management. |
| `/cron` | Scheduled job overview. |
| `/agent_commands` | Show configured agent command menu. |
| `/showfiles` | Browse workspace files. |
| `/diagnose` | Runtime diagnostics. |
| `/restart` | Restart this agent; interrupts work. |
| `/agents` | Main only: agent overview. |
| `/agent_start NAME` | Main only: start a configured agent. |
| `/agent_stop NAME` | Main only: stop an agent. |
| `/agent_restart NAME` | Main only: restart an agent. |

Telegram buttons containing a recognized ductor slash command, such as `/restart` or `/status`, run the same handler as typing that command, including auth, bot-address/mention and command filters. The chosen-button label is retained. Unknown slash commands remain AI prompts. Text, media and prompt buttons recheck the session’s selected provider authentication before running its CLI; missing auth returns localized `./scripts/login.sh <provider>` instructions. Login is detected on the next user turn without a restart. This check also applies to Matrix, Slack and direct API user turns; inter-agent and cron execution do not use it.

Named sessions also accept `@<exact-session-name> message`. Inter-agent session names are sender/chat/topic scoped; copy the reported name or obtain it with `/sessions` rather than guessing. Matrix maps rooms to numeric session IDs internally. Sessions isolate transport/chat/topic and provider buckets, API and Telegram sessions of the same chat ID can be shared (see the [known limits](#12-documentation-known-limits-and-license)). For Telegram group/private-chat messages, topic scope uses `message_thread_id` only when `is_topic_message` is true; otherwise `topic_id` is `None`, even if a thread ID is present.

## 6. Repository, containers and persistent home

```text
ductor_bot/           # tracked Python package
  __main__.py         # bot CLI entry point
  cli_commands/       # help/status/agents CLI
  cli/                # provider subprocesses, auth, process registry
  messenger/          # Telegram / Matrix / Slack transports
  orchestrator/       # turns, commands, lifecycle
  bus/                # envelopes and async delivery
  multiagent/         # supervisor, registry, inter-agent bus/API
  session/            # transport/chat/topic and named sessions
  tasks/, background/ # background tasks and named-session work
  cron/, webhook/, heartbeat/, cleanup/
  workspace/          # path model, seeding, rule/skill sync
  infra/, security/, files/, text/, api/, i18n/
  _home_defaults/     # bundled home/workspace templates
docker/               # ductor-bot, browser, env templates
scripts/              # host operators and isolated verifiers
tests/                # product tests
docs/                 # configuration and module docs
compose.yaml          # two-service runtime
config.example.json   # onboarding input template
pyproject.toml        # package/dependencies/extras
justfile              # developer checks
```

Compose creates `ductor-bot`, `ductor-browser`, and the explicitly named `ductor-net`. The bot uses `init: true`, `restart: unless-stopped` and `stop_grace_period: 30s`. Browser uses the same restart policy and HTTP health check on internal port 18800. Browser has no host port, host home mount or Docker socket. Bot bind-mounts host `DUCTOR_HOME` at `/ductor`, loads home `.env` and `.env.shared`; browser loads only `.env.shared`. Base Compose has **no published host ports**. Onboarding override publishes enabled main API/webhook ports with loopback default. The internal inter-agent API uses localhost port 8799.

The bot image includes openssh-client (`ssh` and `scp`) by default. Python paramiko is not installed; PyNaCl remains an `api` extra dependency.

There is no nested CLI sandbox/Docker socket mount and no host service installation. The bot container can modify everything mounted into `/ductor`; its configured default permission mode bypasses provider approval prompts. Keep one bot container per home. Because container/network names are fixed, a project name alone does not isolate another copy: to run a separate copy you need overrides that rename the containers, network and images.

```text
$DUCTOR_HOME/                     # host; mounted as /ductor
  config/config.json              # main settings and messaging credentials
  .env                            # provider keys read when a CLI starts
  secrets.env                     # tool credentials (0600); loaded on demand, not injected
  .env.shared                     # BROWSER_TOKEN and TZ; both services
  .cli-auth/                      # persistent CLI auth (0700 at onboarding)
    claude/, claude-*/            # independent credentials, shared settings
    _shared-projects/, claude.json
    codex/, gemini/, grok/
  agents.json                     # JSON array of sub-agent definitions
  agents/<name>/                  # independent sub-agent home
    config/config.json, workspace/, logs/
  workspace/                      # seeded at runtime
    tools/, skills/, memory_system/, cron_tasks/, tasks/
    output_to_user/, telegram_files/, matrix_files/, api_files/
  logs/agent.log
  sessions.json, named_sessions.json, tasks.json
  cron_jobs.json, webhooks.json, startup_state.json
  inflight_turns.json, chat_activity.json, heartbeat_state.json
  interagent/inflight/, interagent/undelivered/
  SHAREDMEMORY.md, bot.pid, restart-requested
```

Not every file exists immediately: startup and feature use create them. Main `DuctorPaths` resolves `/ductor`; sub homes are `/ductor/agents/<name>`. CLI account credentials remain in the container-wide auth environment; separate sub workspace/state does not mean a separate paid provider account. Runtime seeds user-owned templates once, while framework-managed rule files and tool scripts are overwritten/synchronized; keep custom work outside managed tools. Back up the home and deployment settings before changes.

## 7. Multi-agent configuration and delegation

Main and sub-agents run under the same supervisor/container. Put a JSON **array** in `$DUCTOR_HOME/agents.json`; the example below has a botless helper and a messenger sub with its own Telegram bot. Replace the messenger token privately; it is an illustrative placeholder, not a working token. Set file mode 0600. A sub inherits main defaults plus non-null registry overrides; credentials/allowlists are its own, not inherited Telegram access. Its generated `agents/<name>/config/config.json` is runtime output; edit `agents.json` as the source.

```json
[
  {"name": "helper", "transport": "headless", "provider": "claude", "model": "sonnet"},
  {"name": "messenger", "transport": "telegram",
   "telegram_token": "REPLACE_PRIVATELY", "allowed_user_ids": [123456789],
   "allowed_group_ids": [], "interagent_notify_received": false}
]
```

Choose **headless** for a worker used only through main’s synchronous/asynchronous delegation: it has its own workspace but no bot token or chat, so users cannot talk to it directly. Choose Telegram/Matrix for a sub with its own direct chat. An agent can create the worker from its framework CLI turn using:

```text
python3 tools/agent_tools/create_agent.py --name helper --transport headless \
  --provider claude --model sonnet
```

`--provider` and `--model` are optional; this is an internal agent tool, not a host setup command. Async results return through main to the requesting chat/topic.

Before creating a sub, main asks you for its role, how it should address you, tone/language and any essential rules, and passes them with `--persona`/`--persona-file` (and, only if needed, `--behavior`/`--behavior-file`). Without `--persona` a bot-backed sub starts from the blank template and asks in its first chat; a headless worker has no chat, so main must collect this at creation. Existing files are never overwritten. Every delegation request a sub receives carries a short block of delegated-work rules (finish inside this turn, no background task hand-off, split if it cannot finish in an hour); a request with a lane adds one line (other lanes run concurrently, repository changes in a lane-specific git worktree, shared memory files only on instruction). Questions and answers get no block.

Start the messenger bot once in your private chat. Headless result notices name the processing session without suggesting a nonexistent recipient chat. A delegation-only sub does not need membership in the main bot's group; its group allowlist can remain empty. If receipt notices are enabled and the sub is expected to post them into a group, its **own** bot must be able to access that group; main never sends them on its behalf. Sub transport is selected from its own usable Telegram/Matrix credentials; explicit invalid transport excludes that sub. An implicit sub without credentials is headless. Headless agents can receive delegated work and answer synchronous requests; they cannot originate async calls or serve as async-result receivers.

Agent tools run **inside a framework-created CLI turn**, where agent identity and source chat/topic/transport are provided automatically. These examples are relative to that agent's `workspace/`, not host maintenance commands. Sync returns tool output to the calling agent; async returns a task ID, then delivers the result to the calling main's original chat/topic, injects it into main's session and lets main process/report it. Normal delegation does not redirect final output to a sub DM.

```text
python3 tools/agent_tools/ask_agent.py [--new] [--lane LANE] TARGET "message"
python3 tools/agent_tools/ask_agent_async.py [--new] [--lane LANE] [--summary "description"] \
  [--reply-to AGENT] [--silent] TARGET "message"
```

Square brackets denote optional arguments; do not type them literally. `--new` resets the recipient named session. `--lane LANE` (1-12 characters of `a-z`, `0-9`, `-`) runs a request in its own parallel session of the same recipient; requests of one lane run in order, different lanes run in parallel up to `interagent.max_parallel_sessions` (default 4), and a busy recipient queues the request instead of rejecting it. Lanes share the recipient's workspace and login, so give each its own files and git worktree and keep one owner for the final merge ([lanes and limits](docs/modules/multiagent.md#lanes-admission-and-the-time-contract)). Async `--reply-to` explicitly chooses another agent receiver and should not be used for ordinary main-origin delegation. **Async reverse/nested calls during an inter-agent turn are rejected**; return your result to main instead.

When main delegates synchronously, the sub must put questions in its final report; the server rejects questions sent to main. Do not invent unknown values: leave them blank or state assumptions. During an asynchronous delegation, a synchronous question to main is answered, where possible, in the current session of the original chat/topic; the answer goes only to the sub and is not posted to the chat. If the source is a Named session, cannot be identified, or the session lock is not obtained within 60 seconds, a separate session with source information handles it. See [routing details](docs/modules/multiagent.md#questions-during-delegation).

`interagent_notify_received` is **false by default** and decided on the receiving server. To opt in for one sub, set its registry field true; to inherit a main true setting, omit the sub override. Restart to apply this setting. False suppresses all receipt notices; `--silent` remains valid and harmless. If true, the receiving sub sends its receipt notice to the origin chat with its own bot, and `--silent` suppresses it for that call. Result delivery is not affected.

On restart, Telegram messages that arrived while the bot was down, and messages that were only waiting behind a running turn, are processed after the restart (a message older than 24 hours is skipped with a short notice, and one you cancelled with a stop command is not brought back); persisted inflight work is reported as interrupted or recovered from a stored completed result; unready receivers retain results for retry/readiness redelivery. This is not an exactly-once guarantee: see [known limits](#12-documentation-known-limits-and-license).

DM topics with a confirmed implicit name get a short noun-phrase title (for example “Server health check”) generated from the received user message, in parallel with the conversation. It applies to main and bot-backed sub-agents; `telegram_dm_topic_auto_name` defaults to true, hot-reloads and can be set to false. If the model gives no usable title (null, an answer, a result-like phrase), the safe start of your own message names the topic and naming ends there; a plain greeting or a failed rename waits for a later message, up to three messages in total. Errors, timeouts or missing authentication use the same message-prefix → topic-ID fallback; user-named/edited topics and groups are skipped. See [DM topic naming](docs/modules/bot.md#automatic-dm-topic-names).

AI-bound user messages (including button choices), inter-agent requests/results/questions, task instructions (creation/resume, including parent answers), results/questions and agent-tool replies carry `[Received: …]` in `user_timezone`. Actual session-lock waits add `[Waited: …]`; a queued user message keeps its receipt inside the existing queue block without duplication. A Telegram message or media that reached the bot at least 10 seconds after it was sent also carries `[Sent: …]` right after `[Received: …]` (same timezone); button choices, Matrix and Slack do not. The first receipt survives async-result retries/recovery. These are AI-input metadata, separate from chat text, session previews, command logs and system prompts; provider transcripts still contain the actual input. It is not added to heartbeat, cron or webhook prompts.

When a user message waits behind an active session turn, Telegram, Matrix and Slack give the agent a short queue notice with the send time in `user_timezone`, lock-wait duration and queue position at arrival. The notice warns that the message may concern the earlier work rather than the latest reply. It is added only to CLI input; it is not added to commands, buttons, cancelled entries or messages that did not wait, and the user text shown in queue indicators, session previews and command logs does not change. See [queued message context](docs/modules/messenger.md#queued-user-message-context).

## 8. Configuration, secrets and safe diagnostics

Main config is `$DUCTOR_HOME/config/config.json`. Refer to [config schema](docs/config.md), [example](config.example.json) and `ductor_bot/config.py`; onboarding copies the **example**, so schema defaults and a newly onboarded home can differ. In particular model is `opus` in schema versus `sonnet` in onboarding; legacy cli_timeout is 1800s in schema versus 600s in the example; onboarding timezone is detected from the host, falling back to UTC.

| Key(s) | Meaning |
| --- | --- |
| `provider/model/reasoning_effort` | Default CLI/model/effort; use /model and /effort for session changes. |
| `allowed_user_ids/allowed_group_ids/group_mention_only` | Default empty group allowlist; group and user must both be authorized. Mention gate is not an auth bypass. |
| `transports/matrix/slack` | Enable selected transports and their own allowlists; build corresponding extras. |
| `telegram_dm_topic_auto_name` | true default; hot-reloaded, one final implicit DM topic rename; subject generation begins on reception. |
| `interagent_notify_received` | false default; receiving-side receipt switch; restart required. |
| `claude_accounts` | Ordered authenticated account slots; credentials remain under .cli-auth. |
| `timeouts/cli_timeout/tasks` | Per-path CLI limits and task behavior; do not assume global timeout covers every path identically. |
| `streaming/heartbeat/cleanup` | Streaming output, periodic checks and retention. Heartbeat enabled by example; manual groups seeded empty. To disable heartbeat, set `heartbeat.enabled` to `false` in `config.json`. Automatic targets default on: allowed Telegram DM/group-topic sessions active within the last 7 days are selected separately by their last user activity (tracked/recovered targets may remain eligible later). With suspend_on_ack on by default, an ACK-only session rests until its next conversation; accepted user, task or bus activity also wakes it (a changed user-activity count counts too). Memory lint reports and cron results alone do not make a topic a heartbeat target or wake it. Quiet hours default to off (0/0); recent-activity cooldown and session gates still apply. |
| `cron_delivery_retry/cron_preflight` | Optional cron retry (off, 300s, 12 attempts) / deterministic skip gate (off, 15s, HEARTBEAT_OK). |
| cron job `delivery_mode` | Set per cron job by the cron tools (not in `config.json`): `separate` (a separate run; the result arrives as a message; jobs with no delivery mode set), `inject` (default for a job created in a chat/topic: the result is injected into that topic's session and the session's reply is delivered) or `in_session` (no separate run; the instruction runs as a turn of that session). The two need a chat; the agent explains each setting before creating or editing a job. See [cron delivery modes](docs/modules/cron.md#delivery-modes-inject-and-in_session). |
| `api/webhooks` | Opt-in listeners; auth tokens and exposure restrictions separate from browser auth. |
| `file_access/permission_mode` | Schema defaults all/bypassPermissions; provider has mounted-home access. |
| `language/user_timezone` | Schema language default en; onboarding selects a supported server locale or explicit --language/ONBOARD_LANGUAGE. Set ko for Korean UI. IANA timezone for automation. |
| `notifications.startup_targets` | Optional explicit startup destinations. |

Operate the containers with Compose. Changing a listener port requires refreshing the generated override; changing transport/extras requires matching build dependencies. Some config fields hot-reload; structural and receipt-setting changes should use restart.

Root `.env` contains **Compose settings**, not provider secrets. Home `.env` supplies the provider keys the CLIs read at start; home `secrets.env` holds credentials that tools load on demand and that are never injected into CLI runs; `.env.shared` contains the shared browser bearer token and TZ and is read by both services. Messaging/API/webhook tokens are in private config (sub bot tokens in private agents.json), OAuth credentials in `.cli-auth`. Keep secrets/config/auth outside Git and use 0600/0700.

Provider keys for CLI subprocesses are read from the configured home, not a project working directory. Tool credentials are read from `secrets.env` through `tools/_secrets.py` (the framework passes its path in `DUCTOR_SECRETS_FILE`); a value already in the environment wins. Existing process environment values take precedence, so changing home `.env` cannot override a key already loaded into the container by Compose. Use `docker compose up -d` to recreate with changed env files when needed; `docker compose restart` alone does not reload container environment. The internal dotenv parser supports quotes/export but does not expand `$VARIABLE` substitutions; use final literal values. Never source or print whole secret files for diagnosis.

Values are not injected as environment variables, but an agent running as the same user can read the file; this protects against accidental exposure only.

Malformed definitions/merge errors log safe item/field/exception metadata; environment-assignment masking covers configured sensitive names. This is not a guarantee that arbitrary user prompts or provider output cannot contain a secret. Validate logs before sharing; browser `/health` is unauthenticated but other browser operations require the shared bearer token.

## 9. Operation, updates, rebuilds and rollback

```bash
docker compose ps
docker compose logs --tail=100 ductor-bot
docker compose logs --tail=100 browser
docker compose stop
docker compose up -d
./scripts/rebuild.sh
./scripts/update.sh
```

These are separate operations, not a sequence to run blindly. Stop/up keeps persistent home; `down` removes stack containers/network but not the bind-mounted home. Rebuild refreshes source/dependencies/CLI image layers and swaps containers; update first fetches/pulls source. Both can interrupt running turns/tasks. The home persists, but persistence is not a promise of exactly-once delivery. Save the current Git SHA, image IDs, root env/override and a private home backup before any swap; read `inflight_turns.json`/task status or use `/tasks` before stopping.

For a graceful application restart, send `/restart` or create the marker below. It interrupts work and does not rebuild the image or refresh Compose env.

```bash
touch /absolute/path/to/ductor-home/restart-requested
```

Rollback must restore a previous source/image **and** compatible deployment settings/home state. The scripts have no automatic rollback. Before updating, retain a private pre-update backup and known-good images, stop new writers before restoring state, and validate Compose/auth/startup afterwards. Checking out an older Git ref alone does not restore CLI versions or runtime data.

## 10. Troubleshooting

| Symptom | Check / recovery |
| --- | --- |
| onboard prerequisite / input failure | Install Docker + Compose v2 and Python; missing non-TTY token/users is exit 2. Run onboard.py --help for parser-only help. |
| permission denied at /ductor | Check access by the image user (the onboarding account's `DUCTOR_UID`/`DUCTOR_GID`) to private home/auth/config; root-created 0600 files are not readable by the bot. |
| browser unhealthy / bot waiting | Read browser logs; empty BROWSER_TOKEN makes server exit before listening. Shared env must exist and match both services. |
| main exit 3 / sub excluded | Missing selected matrix/slack/api dependency: set supported comma-separated DUCTOR_EXTRAS and rebuild. Main dependency errors fail startup; sub errors exclude only that sub. |
| Telegram token whitespace / format error | Onboarding trims leading/trailing whitespace and rejects internal whitespace or invalid format without displaying the token. Correct the private input file or paste again. Existing saved files are retained without --force: correct the private config locally or back up before rerunning with --force. Do not share tokens or unmasked config. |
| Bot online but no chat response | Check real token/login, user/group allowlists, intended topic and provider auth; allowlisting only the group is insufficient. |
| message rejected by allowlist | Telegram/Matrix/Slack log `Allowlist rejection chat_type=... chat_id=... user_id=... topic_id=...` at INFO for actual allowlist denials, without message content. Cached chat/user pairs log at most once per 10 minutes; the helper retains up to 1,024 pairs and evicts older entries. Check the transport’s allowlist/mention policy; repeated silence may be rate-limited in logs. |
| localized provider login warning | From the host repository run `./scripts/login.sh <provider>` for the provider named in the reply. The next user turn rechecks auth; the blocked turn did not run the CLI. |
| /start language unexpected | Check saved config language. Set --language/ONBOARD_LANGUAGE and use --force to update saved config; server locale is a default for new installs. Existing config is kept without --force. |
| command button treated unexpectedly | Recognized Telegram ductor commands use normal command handlers and filters; unknown slash commands still go to AI. |
| sub question lacks topic context | Async-delegation questions resume main’s original active topic session when available. Named/background sources, unknown provenance or a 60s lock wait use the separate session with source header; inspect confirmed history locations there. |
| saved settings ignore new onboarding flags | Existing files preserved. Edit saved config/env or back up and intentionally use --force. Unspecified config keys and group IDs are kept even with --force; update override and rebuild extras. |
| Gemini status says not authenticated | status.sh checks `GEMINI_API_KEY` / `GOOGLE_API_KEY` in the container or the Gemini OAuth credential file. Check that the selected authentication method is configured; do not paste credentials. |
| Claude account list unchanged | With at least two authenticated slots, registration defaults to Y: empty input or non-TTY execution registers slots; n/no skips it. Edit claude_accounts manually if desired. Both real projects dirs still refuse automatic merge; resolve with backups first. |
| async reverse call refused | Expected during IA turns/headless-origin async. During sync delegation, questions to main are also rejected; include them in the final report. During async delegation, ask main synchronously if needed. |
| receipt absent / sub no longer healthy | Receipt default is off. If opted in, sub bot needs origin-chat access. Naturally stopped sub may require /agent_stop then /agent_start; see known limits. |

## 11. Development and isolated verification scripts

Local development installs the checked-out package in a virtual environment, separate from the bot home. Requires Python 3.11+, optional `uv`, and `just` **1.42.0+** for the justfile. Runtime is still Compose. `just check` runs the checks CI fails on (type check, translation keys, docs bundle, `scripts/tests`, shell syntax) plus sequential tests; `just lint` runs ruff for information only; `just fix` modifies files; `just test [args]` runs sequential tests; `just test-parallel [args]` opts into xdist and is not guaranteed parallel-safe. Private `_i18n`, `_docs`, `_scripts`, `_types`, `_test` recipes supply those checks.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m compileall -q ductor_bot
# With uv and just installed:
just check
just test tests/session
```

The `verify-*` files in `scripts/` are isolated verification tools for maintaining the repository and are not needed for installation or normal use. Do not run them against the home or token of a running bot. The `scripts/tests/` unit checks run with `python3 -m unittest discover -s scripts/tests`.

## 12. Documentation, known limits and license

| Document | Contents |
| --- | --- |
| [docs/CHANGES.md](docs/CHANGES.md) | Complete change list. |
| [docs/config.md](docs/config.md) | Configuration schema/registry details; use source if older prose disagrees. |
| [docs/installation.md](docs/installation.md) | Companion installation instructions. |
| [docs/matrix-setup.md](docs/matrix-setup.md) | Matrix rooms, login, store and transport specifics. |
| [docs/architecture.md](docs/architecture.md) | Module/runtime design. |
| [docs/system_overview.md](docs/system_overview.md) | System flow overview. |
| [docs/developer_quickstart.md](docs/developer_quickstart.md) | Development starting points. |
| [docs/automation.md](docs/automation.md) | Cron, webhook and background task systems. |
| [docs/fork_deploy_model.md](docs/fork_deploy_model.md) | Fork deployment/source model. |
| [docs/UPSTREAM.md](docs/UPSTREAM.md) | Manual upstream integration workflow. |
| [docs/README.md](docs/README.md) | Documentation index and module links. |

Known limits are recorded in [CHANGES §7](docs/CHANGES.md#7-known-limitations). Unfinished button markers: a valid file marker after an unclosed outer button marker can be treated as top-level and uploaded. Parent symbolic links of config/auth paths may be followed, and a race remains between the link check and chmod. A naturally exited sub-agent only stops reporting health while its registration stays, so `/agent_stop` followed by `/agent_start` may be needed. A failed file read/move combined with a restart can deliver the same result again (delivery is not exactly-once). If an inter-agent broadcast fails for some recipients, the whole broadcast is sent again and recipients that already received it may get a duplicate. An enabled receipt notice fails to send when the sub-agent’s bot cannot reach the origin chat (result delivery is separate). API and Telegram sessions of the same chat ID can be shared. If the source is a Named session, cannot be identified, or the session lock is not obtained within 60 seconds, the question is handled in a separate session.

The project is licensed under [MIT](LICENSE), retaining copyright © 2025–2026 PleasePrompto.

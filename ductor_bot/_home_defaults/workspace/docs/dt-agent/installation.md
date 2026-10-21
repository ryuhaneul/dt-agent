# Installation

A new host needs Git, Bash, Python 3.9+ for onboarding, Docker Engine, and the Docker Compose plugin
v2 or newer. The account running these commands must be able to use Docker.
The bot home on the host is mounted at `/ductor` in the bot container.
The image user is built with the onboarding account's UID/GID (`DUCTOR_UID` and
`DUCTOR_GID`, each defaulting to 1000); ensure that account can read/write the home.
Every onboarding option is listed in [Onboarding options](#onboarding-options) below. Host scripts such as `scripts/onboard.sh` are run by the operator on the host; an agent in the container asks the operator to run them.

## Start a new installation

1. Clone the repository:

   ```bash
   git clone https://github.com/ryuhaneul/dt-agent.git ductor
   cd ductor
   ```

2. Run onboarding. It asks for a Telegram bot token, allowed user IDs, and
   timezone and bot UI language, then writes the host home and Compose environment files. Secret input is trimmed; Telegram tokens must match ASCII digits:letters/digits/underscore/hyphen, with a nonempty suffix and no internal whitespace. Invalid tokens stop onboarding without displaying their value:

   ```bash
   ./scripts/onboard.sh
   ```

   For an unattended setup, put the bot token in a file and provide the
   required values without exposing the token as a command argument:

   ```bash
   ./scripts/onboard.sh --no-prompt --telegram-token-file /path/to/token-file --allowed-users 123456789
   ```

   Use `--home DIR` to choose another host home. Login/logout/update automatically read the repository-root `.env` DUCTOR_HOME key; no export is needed. Nonempty environment DUCTOR_HOME overrides that saved key, then the fallback is ~/.ductor. Only that key is parsed, with quotes handled; the env file is never sourced. Timezone precedence is `--tz` → `ONBOARD_TZ` → saved `user_timezone` on reruns → detected host timezone → `UTC`; system detection tries a ZoneInfo-valid `TZ`, `/etc/timezone`, then the `/etc/localtime` symlink path after `zoneinfo/`. The interactive question displays the selected default. Language precedence is `--language` → `ONBOARD_LANGUAGE` → saved `language` on reruns → supported host locale → `en`. First installation starts from the example; reruns use saved config as the basis and add missing defaults recursively. `--api` and `--webhook`
   enable those listeners; `--bind ADDR` chooses their host bind address
   (default `127.0.0.1`). Onboarding publishes ports only for enabled
   **main-agent** listeners in the ignored `compose.override.yml`.
   Sub-agent webhooks remain inside the container. Re-running onboarding
   preserves existing files unless `--force` is given. To change a listener
   port, edit the saved main `config/config.json` and rerun onboarding
   without `--force`; this refreshes `compose.override.yml` from the saved
   settings. With --force, onboarding uses the saved config as its basis and updates
   only supplied options/environment values or interactive answers. It preserves
   groups, heartbeat settings, listener ports, credentials and custom keys, and
   recursively adds only missing example defaults. Enter keeps saved question
   defaults; the saved Telegram token stays hidden and blank input retains it.
   Groups change only with --allowed-groups/ONBOARD_ALLOWED_GROUPS, not a question.
   Env-file force/replacement and permission rules are unchanged; back up first.
   To reset configuration, delete DUCTOR_HOME/config/config.json and rerun onboarding.

3. Build and start:

   ```bash
   docker compose up -d --build
   docker compose logs --tail=100 ductor-bot
   ```

   Keep one bot container per `DUCTOR_HOME`. Sharing a home between bot
   containers is unsupported.

4. Check for `Bot online` in the logs, then authenticate the provider selected in saved config (default Claude), and check status. This matches the onboarding completion notice:

   ```bash
   ./scripts/login.sh claude
   ./scripts/status.sh
   ```

   Use codex/gemini/grok instead if that is the provider in your config. Use only the providers you need. CLI auth persists in the mounted
   `.cli-auth` directory. Gemini can also use a configured API key.

Onboarding language precedence is `--language` → `ONBOARD_LANGUAGE` → saved language on reruns → the first nonempty server locale (`LC_ALL`, `LC_MESSAGES`, `LANG`) → `en`. It extracts the language part, for example `ko_KR.UTF-8` → `ko`; an unsupported server locale falls back to `en`. Supported bundled codes are `de`, `en`, `es`, `fr`, `id`, `ko`, `nl`, `pt`, `ru`. Unsupported explicit options/environment/question answers fail before writes. Question 5 displays the saved or detected default; Enter retains it; the decided language also sets the onboarding questions, errors, completion notice and the `onboard.sh` preflight messages (commands, options, codes and paths stay untranslated); `login.sh`, `logout.sh`, `status.sh`, `update.sh` and `rebuild.sh` use the same order without `--language` (`ONBOARD_LANGUAGE`, the language saved in the host home's config, the server locale, `en`) as well as command-line errors and the `--help` text, and answering question 5 changes the texts that follow. `config.language` controls bot UI messages. Existing config is retained without `--force`, and the language line of the completion notice reports the saved value. Option names are read in full: an abbreviation such as `--lang=ko` is an unknown-argument error in the decided language (exit 2, nothing written).

## Onboarding options

`scripts/onboard.sh` (a wrapper that checks Python, Docker and Compose before calling `scripts/onboard.py`) accepts the options below. Run `./scripts/onboard.sh --help` for the parser-only help. Precedence is explicit argument → environment variable → default or interactive question.

| Option | Environment | Behavior |
|---|---|---|
| `-h`, `--help` | | Show argparse help. |
| `--no-prompt` | | Disable TTY questions; the Telegram token file and allowed users are still required; saved values can be reused on reruns. |
| `--home PATH` | `DUCTOR_HOME` | Host persistent home; expands `~` and resolves to an absolute path. |
| `--telegram-token-file FILE` | `ONBOARD_TELEGRAM_TOKEN_FILE` | Read and trim one nonempty line; validated like the hidden interactive input. Never pass a token literal as an option. |
| `--allowed-users IDS` | `ONBOARD_ALLOWED_USERS` | Comma-separated numeric Telegram user IDs; required. |
| `--allowed-groups IDS` | `ONBOARD_ALLOWED_GROUPS` | Comma-separated numeric group IDs; optional. Use `=` for negative IDs. An explicit empty value clears saved groups. |
| `--tz ZONE` | `ONBOARD_TZ` | IANA timezone; on reruns the saved value, then the detected host timezone, then `UTC`. |
| `--language CODE` | `ONBOARD_LANGUAGE` | Bot UI language; must be a bundled code (`de`, `en`, `es`, `fr`, `id`, `ko`, `nl`, `pt`, `ru`). Saved value on reruns, then the server locale, then `en`. |
| `--transport none\|matrix\|slack` | `ONBOARD_TRANSPORTS` (comma-separated) | Repeatable extra transports; Telegram is kept. `none` cannot be combined with another value. |
| `--matrix-homeserver URL` | `ONBOARD_MATRIX_HOMESERVER` | Required when adding Matrix. |
| `--matrix-user-id ID` | `ONBOARD_MATRIX_USER_ID` | Required when adding Matrix. |
| `--matrix-password-file FILE` | `ONBOARD_MATRIX_PASSWORD_FILE` | Without a saved Matrix password or access token, exactly one of the password file or access-token file is required (both together are rejected); a rerun can reuse the saved credential and omit both. Credentials stay in the private config. |
| `--matrix-access-token-file FILE` | `ONBOARD_MATRIX_ACCESS_TOKEN_FILE` | Same condition as the password file above; giving one replaces the saved credential of the other kind. |
| `--matrix-device-id ID` | `ONBOARD_MATRIX_DEVICE_ID` | Required together with a Matrix access token. |
| `--matrix-allowed-rooms IDS` | `ONBOARD_MATRIX_ALLOWED_ROOMS` | Comma-separated room IDs; at least one Matrix allowlist (rooms or users) must be nonempty. |
| `--matrix-allowed-users IDS` | `ONBOARD_MATRIX_ALLOWED_USERS` | Comma-separated Matrix user IDs; see the rooms option. |
| `--slack-bot-token-file FILE` | `ONBOARD_SLACK_BOT_TOKEN_FILE` | Required when adding Slack unless a bot token is already saved; a rerun can reuse the saved token and omit the file. |
| `--slack-app-token-file FILE` | `ONBOARD_SLACK_APP_TOKEN_FILE` | Required when adding Slack unless an app token is already saved; a rerun can reuse the saved token and omit the file. |
| `--slack-allowed-users IDS` | `ONBOARD_SLACK_ALLOWED_USERS` | Comma-separated; at least one Slack allowlist (users or channels) is required. |
| `--slack-allowed-channels IDS` | `ONBOARD_SLACK_ALLOWED_CHANNELS` | Comma-separated; see the users option. |
| `--api` / `--no-api` | `ONBOARD_API` | Enable or disable the WebSocket API listener (default disabled); enabling it also adds the `api` build extra. |
| `--webhook` / `--no-webhook` | `ONBOARD_WEBHOOK` | Enable or disable the webhook listener (default disabled). |
| `--bind IP` | `ONBOARD_BIND` | Numeric bind IP for the published API and webhook ports; default `127.0.0.1`. |
| `--force` | | Allow file updates; config keeps values you did not supply and gains missing defaults, env files can still be replaced. Back up first. |

Boolean environment values accept `true`/`false`, `yes`/`no`, `1`/`0`, `y`/`n`. The removed `--ssh`/`--no-ssh`/`ONBOARD_SSH` inputs fail with a nonzero exit (the image includes `ssh` and `scp` by default).

## Configuration

The host config is `<resolved host home>/config/config.json`; login/logout/update resolve that home from nonempty environment DUCTOR_HOME, repository `.env`, then `~/.ductor`. Inside
the container, `AgentConfig.ductor_home` defaults to `/ductor`.
The example config sets `ductor_home` to `/ductor`. Keep this container path
in the generated `config.json`.

The optional direct WebSocket API uses the `api` block in `config.json`.
Set `api.enabled` there after adding PyNaCl to the image. If its token is
empty, the runtime generates and persists one at API startup. The default
image does not include the API extra. See [API](modules/api.md) for the
protocol and [configuration](config.md) for fields.

Onboarding writes repository `.env` (Compose inputs), home `.env` (optional
provider keys), home `.env.shared` (browser token/timezone), home `secrets.env` (an empty
store for tool credentials, created only when missing), and
`config/config.json`; generated files use mode `0600` and `.cli-auth` uses `0700`.
Enable image extras through onboarding (`--transport matrix`, `--transport slack`,
`--api`, as needed). Matrix/Slack are additional transports during
onboarding; Telegram credentials are still required. See [Matrix setup](matrix-setup.md).

The bot image always installs openssh-client (`ssh`, `scp`). `ssh` is no longer
a Python extra; `DUCTOR_EXTRAS=ssh` fails the build. Onboarding `--ssh`,
`--no-ssh`, or a present `ONBOARD_SSH` fail nonzero with
`ssh extra was removed; openssh-client is included in the image by default`. PyNaCl remains optional through `api`.
Sources: Dockerfile (`docker/ductor-bot/Dockerfile`),
onboarding (`scripts/onboard.py`).

Runtime restart-required config changes need a bot restart. Compose environment
changes need `docker compose up -d` to recreate affected services; a simple
container restart retains the old environment. Runtime home `.env` edits take
effect at the next CLI invocation only for keys not already present in the
container environment. See [environment precedence](config.md#external-api-secrets-and-environment).

## Operation

```bash
docker compose logs -f ductor-bot
docker compose ps
./scripts/status.sh
docker compose up -d --build
docker compose down
```

`docker compose down` stops the stack. The mounted `DUCTOR_HOME` directory
is retained. For a source update, inspect the upstream changes, then rebuild
and restart the stack. `scripts/update.sh` and `scripts/rebuild.sh` are
available for their documented maintenance flows; review their effects
before using them with active work. Rebuild/update recreate services and can interrupt running work. Keep a reviewed ref and home backup for rollback.

## Troubleshooting

- Telegram token whitespace/format error: onboarding trims leading/trailing whitespace and rejects internal whitespace or invalid format without echoing the token. Correct the private input file; existing saved config is kept without --force, so correct it privately or back up before rerunning with --force.
- Bot did not start: inspect `docker compose ps` and
  `docker compose logs ductor-bot`.
- Bot cannot answer: check transport credentials and allowlists in
  `config.json`, then run `./scripts/status.sh`. Actual allowlist denials are logged at INFO without content, at most once per retained chat/user pair per 10 minutes (bounded cache: up to 1,024 pairs). A missing provider login produces localized instructions before any user-turn CLI call; from the host repository run `./scripts/login.sh <provider>` and retry.
- Provider cannot run: authenticate that provider through
  `./scripts/login.sh <provider>` and inspect the bot logs.
- Unexpected `/start` language: check saved `config.language`. Use `--language`/`ONBOARD_LANGUAGE` with --force to update saved config; existing config is kept without `--force`.
- Telegram command buttons: known DT Agent slash commands use typed-command handlers/filters; unknown slash commands stay AI prompts.
- Sub question has separate context: see [delegation questions](modules/multiagent.md#questions-during-delegation) for the 60s async-delegation active-session wait and source-header fallback. Verified synchronous-delegation questions to main are rejected by the server; include questions in the final report.

- Webhooks cannot arrive: configure the webhook listener and publish or
  proxy the required port; the default Compose file does not publish it.

Sources: onboarding options/output (`scripts/onboard.py`),
login (`scripts/login.sh`), Compose (`compose.yaml`),
image (`docker/ductor-bot/Dockerfile`), rebuild (`scripts/rebuild.sh`),
update (`scripts/update.sh`).

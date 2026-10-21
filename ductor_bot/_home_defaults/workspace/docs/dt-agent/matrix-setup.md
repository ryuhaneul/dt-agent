# Matrix setup

Matrix is an optional transport. The base bot image does not currently include
`matrix-nio`; build a bot image with the `ductor[matrix]` package extra before
enabling it. Docker Engine and Compose v2 are required for this deployment.

1. Prepare the home with `./scripts/onboard.sh --transport matrix` and supply
   the Matrix homeserver, bot user, a password file (or access-token file plus
   device ID), and at least one room/user allowlist. This selects the `matrix`
   image extra in repository `.env`. Onboarding still requires Telegram inputs.
   For an existing home, include `matrix` in `DUCTOR_EXTRAS` in repository `.env`
   before rebuilding (preserve any other required extras).
2. Edit `<host home selected during onboarding>/config/config.json`. The selected home is saved as `DUCTOR_HOME` in repository `.env`; a nonempty exported `DUCTOR_HOME` overrides it. Do not substitute `~/.ductor` merely because the shell variable is unset.
3. Rebuild and start with `docker compose up -d --build`.
4. Check `docker compose logs ductor-bot`.

For Matrix alongside Telegram, set:

```json
{
  "transport": "telegram",
  "transports": ["telegram", "matrix"],
  "matrix": {
    "homeserver": "https://matrix.example.invalid",
    "user_id": "@bot:example.invalid",
    "password": "SET_IN_PRIVATE_CONFIG",
    "allowed_rooms": [],
    "allowed_users": ["@you:example.invalid"],
    "store_path": "matrix_store"
  }
}
```

For Matrix alone, set `"transport": "matrix"` and
`"transports": ["matrix"]`. Use a dedicated Matrix account.
The first login saves mode-`0600` credentials to
`<agent_home>/<store_path>/credentials.json`. An explicit access token must be
paired with a device ID; the stored credentials can also be reused. `store_path`
must be relative, contained in the home, and free of symlink components.

Matrix commands accept both `!` and `/`. Expect `Matrix bot online` in the logs.
The current `AsyncClient` setup does not configure E2EE; use unencrypted rooms.
Empty allowlists do not restrict that dimension, so configure explicit room and
user lists. `group_mention_only` in non-DM rooms bypasses the user list but
retains the room gate. Matrix-primary startup also runs recovery planning.
See [Matrix runtime](modules/matrix.md) for routing details and the
full option reference (`README.md`) for host scripts.

Sources: Matrix client/auth (`ductor_bot/messenger/matrix/bot.py`),
credentials (`ductor_bot/messenger/matrix/credentials.py`),
store validation (`ductor_bot/config.py`),
startup/recovery (`ductor_bot/messenger/matrix/startup.py`),
onboarding (`scripts/onboard.py`).

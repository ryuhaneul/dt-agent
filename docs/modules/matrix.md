# messenger/matrix/

Matrix/Element transport layer (`matrix-nio`): message handling, streaming, buttons, formatting, credentials.

Alternative to Telegram via `config.transport = "matrix"`, or run in parallel with Telegram via `config.transports`.
For shared messenger protocols and the transport registry, see
[messenger.md](messenger.md).

Text and media dispatch that waits for its session lock supplies the same [queued user message context](messenger.md#queued-user-message-context) as Telegram and Slack. It uses server_timestamp for send time; command/button paths and cancelled tasks keep existing behavior.

## Files

- `messenger/matrix/bot.py`: `MatrixBot` class implementing `BotProtocol`; message ingestion, command routing, authorization, streaming
- `messenger/matrix/transport.py`: `MatrixTransport` adapter for `MessageBus`; maps envelopes to Matrix room messages
- `messenger/matrix/sender.py`: message formatting and sending; Markdown→HTML, file upload, message splitting, redaction helpers
- `messenger/matrix/credentials.py`: login flow (saved credentials → config token → password login)
- `messenger/matrix/id_map.py`: bidirectional `room_id` ↔ `int` mapping (deterministic SHA256)
- `messenger/matrix/buttons.py`: reaction-based button replacement; emoji digits + numbered text fallback
- `messenger/matrix/formatting.py`: Markdown → Matrix HTML conversion
- `messenger/matrix/typing.py`: typing indicator context manager with periodic keep-alive (5s interval)
- `messenger/matrix/media.py`: incoming media handler; downloads files from homeserver, builds agent prompts via `files/prompt.py`
- `messenger/matrix/startup.py`: Matrix-specific startup (orchestrator, observers, restart sentinel)

## Incoming media

Matrix media (images, audio, video, files) is handled by `messenger/matrix/media.py`:

1. `MatrixBot` registers `_on_media` callbacks for `RoomMessageImage`, `RoomMessageAudio`, `RoomMessageVideo`, `RoomMessageFile`
2. `resolve_matrix_media()` downloads the file via `client.download(mxc=...)` to `workspace/matrix_files/YYYY-MM-DD/`
3. A transport-agnostic prompt is built via `files/prompt.py` (`build_media_prompt`) and injected into the conversation

Files are auto-cleaned by `CleanupObserver` using the same retention as Telegram files.

## Streaming

Matrix uses **segment-based streaming**: text is buffered and flushed as separate messages at tool/system boundaries.

- `MatrixStreamEditor.on_delta()`: accumulates text into buffer
- `MatrixStreamEditor.on_tool()`: flushes buffer as message; tool markers are suppressed (not sent to chat)
- `MatrixStreamEditor.on_system()`: flushes buffer; status markers are also suppressed
- Final segment gets button extraction via `ButtonTracker`

### Typing indicator

`MatrixTypingContext` runs a background keep-alive task that re-sends the typing notification every 5 seconds. This is necessary because:

1. Matrix clients (Element) clear the indicator when the bot sends a message
2. The server expires it after 30 seconds

The keep-alive ensures the indicator stays visible throughout the entire response, even when intermediate messages (reasoning segments, tool tags) are sent.

## Buttons

Matrix lacks inline keyboards. Workaround:

- Emoji digit reactions (1️⃣–🔟) on selector messages
- Numbered text list as visual fallback
- Text input matching (`1`, `2`, etc.) for clients without reaction support
- One active button set per room

## Authorization

Actual allowlist denials log only chat/user/thread metadata at INFO, once per retained chat/user pair per 10 minutes; existing room/DM/mention policy remains unchanged. Foreground conversation provider authentication is rechecked in shared orchestrator flows before CLI execution, with localized host `./scripts/login.sh <provider>` instructions on failure. See [shared diagnostics](messenger.md#allowlist-diagnostics-and-provider-login).

- **Room-level**: `allowed_rooms` filter (empty = all rooms)
- **User-level**: `allowed_users` filter (empty = all users)
- **Group mention-only**: in multi-user rooms, bot responds only to @mentions or replies to its own messages
- **Important nuance**: when `group_mention_only=true` in non-DM rooms, `allowed_users` is bypassed and room allowlist + mention/reply become the effective gate
- Auto-join allowed rooms on invite; reject + leave unauthorized rooms

## Command routing

Commands use `!` or `/` prefixes. Shared orchestrator commands are available,
while Telegram-only group utilities such as `/where` and `/leave` are absent:

- Transport-level: `!stop`, `!stop_all`, `!interrupt`, `!restart`, `!new`, `!help`, `!start`, `!info`, `!session`, `!showfiles`, `!agent_commands`
- Orchestrator-routed: `!status`, `!model`, `!effort`, `!memory`, `!cron`, `!diagnose`, `!sessions`, `!tasks`, `!reset`
- Main-agent only: `!agents`, `!agent_start`, `!agent_stop`, `!agent_restart`

## Setup

Configure Matrix in `/ductor/config/config.json` and run the bot through
Docker Compose. The default image requires the optional `matrix-nio`
dependency before Matrix can start. See [Matrix setup](../matrix-setup.md).
`ductor agents add` scaffolds Telegram sub-agents; Matrix sub-agents use
`agents.json` or the bundled agent tool scripts.

## Configuration

```toml
# pyproject.toml
[project.optional-dependencies]
matrix = ["matrix-nio>=0.25.0"]
```

```json
{
  "transport": "matrix",
  "matrix": {
    "homeserver": "https://matrix.example.com",
    "user_id": "@bot:matrix.example.com",
    "password": "...",
    "allowed_rooms": [],
    "allowed_users": ["@user:matrix.example.com"],
    "store_path": "matrix_store"
  }
}
```

### Credential flow

1. On first start, the bot logs in with the `password` and saves credentials to `~/.ductor/<store_path>/credentials.json` (mode `0o600`)
2. On subsequent starts, a saved token is restored only when its user/account identity matches the configured user and, when recorded, homeserver; password is then unnecessary
3. If `access_token` and `device_id` are both set in config instead of password, they are used directly and mirrored into the credentials store

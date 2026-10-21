# api/

Direct WebSocket API server for non-Telegram clients.

## Purpose

Provides transport-independent access to the same orchestrator/session system used by Telegram.

- E2E encrypted WebSocket session (NaCl Box)
- authenticated file download/upload endpoints
- shared session model (`SessionKey`) with optional channel isolation

## Files

- `api/crypto.py`: E2E session (`E2ESession`)
- `api/server.py`: WebSocket + HTTP handlers, auth, streaming dispatch, file endpoints

## Enable in a container deployment

Configure the `api` block in `/ductor/config/config.json` after building
the bot image with PyNaCl. Set `api.enabled=true`; when `api.token` is
empty, the runtime generates and saves a token at startup. The default image
build does not include optional `api` dependencies unless `DUCTOR_EXTRAS` selects
them, and the default Compose file does not publish the API port.
`scripts/onboard.sh --api` selects the extra and generates the port override.
See [installation](../installation.md).

## Config (`config.api`)

| Field | Default | Description |
|---|---|---|
| `enabled` | `false` | start API server |
| `host` | `0.0.0.0` | bind address |
| `port` | `8741` | API port |
| `token` | `""` | auth token (generated at API startup when empty) |
| `chat_id` | `0` | default chat scope (`0` -> fallback to first `allowed_user_ids`, else `1`) |
| `allow_public` | `false` | suppress tailscale warning |

## Auth and E2E handshake

Endpoint: `ws://<host>:<port>/ws`

Client first frame (plaintext):

```json
{"type":"auth","token":"...","e2e_pk":"...","chat_id":123,"channel_id":77}
```

Required:

- `type=auth`
- valid `token`
- valid `e2e_pk` (base64 Curve25519 public key)

Optional session scope:

- `chat_id`: positive int; missing/invalid values use the configured default
- `channel_id`: positive int mapped to `SessionKey.topic_id`; missing/invalid values become `None`

Server responds (last plaintext frame):

```json
{"type":"auth_ok","chat_id":123,"channel_id":77,"e2e_pk":"...","providers":[...]}
```

After `auth_ok`, all frames are encrypted.

## Session identity in API

API auth constructs `SessionKey(chat_id=chat_id, topic_id=channel_id)` with
the default `transport="tg"`. Equal numeric chat/topic IDs can share Telegram
state. CLI request metadata separately carries `request_transport="api"`.
See the API/TG limit (`docs/CHANGES.md`).

- `topic_id` is populated from `channel_id` in auth payload
- without `channel_id`, session is chat-scoped only

This allows multiple API channels to maintain isolated contexts under one `chat_id`.

## Encrypted message flow

Client message:

```json
{"type":"message","text":"..."}
```

Server streaming events:

- `text_delta`
- `tool_activity`
- `system_status`
- final `result` (`text`, `stream_fallback`, optional `files`)

Abort:

- client sends `{"type":"abort"}` or text message `/stop`
- server returns `abort_ok` with kill count

Current scope nuance:

- API abort is currently chat-scoped, not channel-scoped, because the abort path kills active work by `chat_id`

## HTTP endpoints

- `GET /health` (no auth)
- `GET /files?path=...` (Bearer token + allowed-root checks)
- `POST /upload` (Bearer token + multipart)

Upload target:

- `~/.ductor/workspace/api_files/YYYY-MM-DD/...`

## File safety model

`GET /files` path checks use `file_access` mapping:

- `all` -> unrestricted
- `home` -> home-root limited
- `workspace` -> `~/.ductor/workspace` limited

MIME and file-tag parsing share helpers from `ductor_bot/files/`.

## Error model

Auth-phase errors are plaintext (`auth_timeout`, `auth_required`, `auth_failed`).
Session-phase errors are encrypted (`decrypt_failed`, `empty`, `unknown_type`, `no_handler`, `internal_error`).

HTTP file endpoints currently reject missing/invalid Bearer authentication with
401 and use 403 for downloads outside allowed roots.

## Wiring

`orchestrator/lifecycle.start_api_server(...)` wires:

- message handler -> `Orchestrator.handle_message_streaming` with `request_transport="api"`
- session locks -> the primary bot/MessageBus shared pool, supplied before the server starts; active-session delegation questions use this same pool. Legacy `tg` session identity is retained with `api` CLI origin
- abort handler -> `Orchestrator.abort`
- file context (allowed roots, upload dir, workspace)
- provider metadata and active state getter

Foreground conversations use the shared fresh provider-auth check before CLI execution, with localized host-repository `./scripts/login.sh <provider>` instructions if missing; no CLI executes for that blocked turn. See [foreground auth](orchestrator.md).

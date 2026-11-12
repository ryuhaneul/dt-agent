# Emergency web chat

The optional web UI provides main-agent Telegram topic conversations during a
Telegram connection outage. Enable `chat_log.enabled` and `webchat.enabled`
(both default to `false`), configure access and restart the bot. Web activation
requires recording; `chat_log_required` rejects web startup when recording is off.
See [installation](../installation.md#emergency-web-chat-optional) and
[configuration](../config.md#webchatconfig).

## Components and activation

The `webchat` Compose profile runs the web app, NPMplus gateway and CrowdSec.
The web app serves static assets, browser API and sessions on its loopback-published
port. The bot starts a separate internal bridge at `/webchat/v1`, protected by
`X-Dtw-Bridge-Token`. The token is stored in
`$DUCTOR_HOME/webchat/share/bridge.token` with mode 0600 and mounted read-only
into the web container. Configure and restart the bot before running
`python3 scripts/webchat.py up` so the bridge can create this token. If it is
missing, empty or whitespace-only, the helper exits immediately with
`bridge_token_missing` and a restart instruction. If the token cannot be read,
`bridge_token_unreadable` advises checking that `DUCTOR_UID` matches the host UID.
Gateway credentials live in `webchat/gateway/` as private files.

Web activation performs local orchestrator startup before Telegram connection
attempts. Telegram retries use 5, 10, 20, 40 and then 60-second delays.
Startup hooks run once. Shutdown closes the bridge, recording runtime and orchestrator
in that order. Recording can remain active after the web services are stopped.

Direct exposure requires `allowed_ips` and ACME email; use a public domain such as
`chat.example.com` and example client range `203.0.113.0/24` when adapting the
configuration. Behind a reverse proxy, configure `trusted_proxies`, an explicit
internal IPv4 `bind_address`, and client allowlisting at the trusted ingress.
The pinned NPMplus release binds both HTTP and HTTPS listeners to that address
in `behind_proxy` mode; it does not support `DISABLE_HTTPS`. The outer proxy
terminates TLS and forwards to the configured HTTP port. See the installation
example for the required address and trust settings. If certificate setup fails,
the helper attempts to remove a proxy host created by that invocation. If removal
fails, it reports the certificate error plus cleanup failure and retries removal
of the marked host before creating a replacement on the next run. Other preexisting
hosts are preserved. The client IP exception for ACME uses the normalized URI and
only the `/.well-known/acme-challenge/` prefix; allowlist denial runs before the
forced-HTTPS ACME exception. This bundled NPMplus instance is dedicated to web chat:
the allowlist denial applies to every forced-HTTPS host on the instance. Do not add
other hosts to it.

## Authentication

Set the password with `ductor webchat set-password` (at least 12 characters).
A scrypt hash and TOTP seed are stored through the bot's secret store.
After password verification, a preauthentication token lasts 300 seconds and is bound
to the credential generation at issuance. Credential changes invalidate it.
First setup accepts a new QR code or an existing Base32 seed; confirmation saves it.
TOTP uses six digits, a 30-second period, a one-step clock window and replay checks.
Five failed attempts per client IP or 20 globally within 15 minutes lock login.
The global lock also blocks correct credentials until it expires, so the login
page is meant to be reachable only from the gateway allowlist or a trusted proxy.
Browser sessions have a 12-hour absolute lifetime and a two-hour idle timeout.
Cookies are Secure, HttpOnly and SameSite=Strict. Mutations use `X-Dtw-Client`
and `X-Dtw-Csrf`; authentication failures return 403.

`ductor webchat reset-totp` resets enrollment. `clear-credentials` removes the
password hash and TOTP seed. Credential maintenance and TOTP enrollment rotate a
persistent nonce in `webchat/auth_state.json` (0600), revoking prior sessions even
when the same seed is enrolled again. This state file also stores TOTP usage records.
If it cannot be written, the TOTP login step is rejected with 503 to prevent code
reuse. If the file is missing or corrupt and cannot be written, the password step and
authenticated requests also receive 503. Once writing is restored, a new nonce invalidates all
existing sessions. No bot restart is required. WebSocket
credential checks are cached per session for 10 seconds and refreshed on bridge
recovery. An unavailable bridge permits a socket with an offline status frame;
private frames require successful validation. Failed validation closes the socket
so the browser can reconnect and resync. Logout clears the local session and
cookies even while the bridge is unavailable.

## Topics, turns and queue

Web turns share the target Telegram `tg:` session key and `LockPool` lock.
Stopping a turn targets only that chat/topic with `kill_by_chat_topic`. Abort and
topic mutations use a 45-second bridge request timeout. Message POSTs use the same
limit because they also accept `/stop`; normal sends return without waiting for
the turn to finish.
Topic creation, rename and deletion call Telegram's forum topic API; deletion
also removes the exact session, history and queued deliveries for that topic.
The **Imported history** group covers topics with zero live entries and activity
older than 30 days. Real imported topics remain usable and manageable.
**Unknown topic** is a read-only history group.

`GET /outbox` returns `{ "items": [...] }` containing web mirrors and buffered
bus deliveries, including public origin/title metadata. See
[recording and recovery](chat_log.md) for ordering, chunk splitting and the
possible duplicate chunk after an interrupted Telegram response.

## Bridge contracts

The browser app relays bridge JSON statuses and error codes. Bridge routes use
`/webchat/v1`; browser routes use `/api`. Errors use `{ "error": "code" }`.

| Operation | Status and response |
|---|---|
| Delete topic while its session lock remains held beyond 30 seconds | 409 `topic_busy`; retry after the topic finishes stopping |
| Send to or abort Unknown topic | 409 `read_only`; select a real topic |
| Send a normal message | 202 `{ "turn_id": "..." }` |
| Send `/stop` (trimmed, case-insensitive) | 202 `{ "killed": 0 }` (integer process count) |
| Abort endpoint | 200 `{ "killed": 0 }` (integer process count) |
| Another web turn is running in the same topic | 409 `web_turn_running` |
| Telegram topic operation fails | 503 `telegram_unavailable` |
| Bot bridge is unreachable for a private REST relay | 503 `bot_unreachable` |
| Stale file base hash | 409 `conflict` |
| Malformed existing MAINMEMORY shared block | 409 `shared_block_invalid` |
| Invalid file body, UTF-8, or client-supplied shared markers | 400 `bad_request` |
| File content exceeds 262144 UTF-8 bytes, including preserved blocks | 413 `too_large` |
| Unapproved, missing or unsafe file path | 404 `not_found` |

`ductor webchat check-config --json` prints exactly this structure, with every
`WebchatConfig` field in `webchat` and error-code strings in `errors`:

```json
{
  "errors": [],
  "chat_log": {"enabled": true},
  "webchat": {
    "enabled": true,
    "exposure": "direct",
    "domain": "chat.example.com",
    "allowed_ips": ["203.0.113.0/24"],
    "trusted_proxies": [],
    "bind_address": "127.0.0.1",
    "http_port": 80,
    "https_port": 443,
    "npm_admin_port": 81,
    "acme_email": "admin@example.com",
    "web_port": 8744,
    "bridge_port": 8743,
    "crowdsec_lapi_port": 18080,
    "crowdsec_appsec_port": 17422,
    "default_chat_id": 0
  }
}
```

The `webchat` object includes all configuration fields; credential values are
stored separately. The example assumes an existing allowed Telegram user. Exit status is 1 for validation errors,
0 otherwise. `scripts/webchat.py` consumes this structure for its preflight.

## File editing

Only the main agent's `PERSONA.md`, `BEHAVIOR.md`, `MAINMEMORY.md` and
`SHAREDMEMORY.md` are editable. Paths come from `DuctorPaths`. Directory links
inside the canonical home are permitted; file aliases and paths outside that home
are rejected. Listing metadata includes `name`, full-byte SHA-256, byte `size`
and UTC ISO-8601 `mtime`.

`GET /files/{name}` returns `content`, `sha256` and `shared_block`. MAINMEMORY's
modern and legacy shared blocks are hidden from the editable content; its hash
still covers the whole file. `PUT /files/{name}` accepts `{content, base_sha256}`.
It checks the latest hash, preserves existing shared blocks and saves atomically
after backing up the original bytes. SHAREDMEMORY content is stripped and stored
with one trailing newline, compatible with the shared-knowledge editor. Client
content containing modern or legacy shared-block markers is rejected with
400 `bad_request` for both memory files before backups or writes.
The response contains the stored `sha256` and relative `backup` path:
`backups/YYYY-MM-DD/<name>.HHMMSS`, using Asia/Seoul time and a numeric suffix
for collisions. Reload after a conflict and review changes again before saving.

Per-file locks serialize web saves and the latest bytes are checked before replace.
A noncooperating external writer, including shared-memory synchronization, can
still race after that check and before replacement. Coordinate external edits
with web saves and retain backups for recovery. This limitation also applies to
MAINMEMORY saves, which perform a single comparison and atomic replacement.

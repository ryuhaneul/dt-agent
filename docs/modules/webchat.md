# Web chat

The optional web UI provides main-agent Telegram topic conversations during a
Telegram connection outage. Enable `chat_log.enabled` and `webchat.enabled`
(both default to `false`), configure access and restart the bot. Web activation
requires recording; `chat_log_required` rejects web startup when recording is off.
See [installation](../installation.md#web-chat-optional) and
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

The header globe selects one of nine web display languages, applied immediately and saved
in browser localStorage across reloads; bot and Telegram language settings stay unchanged.
The header Rename dialog updates the current topic name while keeping the conversation open.

## Authentication

Set the password with `ductor webchat set-password` (at least 12 characters).
`webchat/credentials.json` (0600, atomic writes) stores a password hash and encrypted TOTP
record `{salt, nonce, ciphertext}`. A separate scrypt salt derives the TOTP encryption key from
the web password; the secrets-store master key is not involved. Login works while the vault
is locked. After password verification, decrypted seed and derived key live only in the
preauthentication context (not the raw password), with the same 300-second lifetime; success,
final failure, logout and expiry discard them. Initial enrollment encrypts using that context.
When the new file is absent, legacy secrets.env credentials still work; the next successful
TOTP login/enrollment writes and verifies the encrypted file before atomically removing both
legacy entries. Migration failure keeps the old source and does not reject a successful login.
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

Logged-in users can choose **Change password** from the header account menu.
Enter the current password, a new password of at least 12 characters, its confirmation,
and a fresh TOTP code. A successful change logs out all other sessions and closes
their WebSockets; the current window receives a new session cookie and CSRF token
and stays logged in. Resetting TOTP and recovering a forgotten password still require the CLI.

`ductor webchat set-password` does not need the old password: it writes a new hash and
**discards TOTP**, so the next login must enroll it again. `reset-totp` deletes the seed without
asking for the password. `clear-credentials` removes new credentials and legacy entries.
After migration, these standalone edits work even while the tool vault is locked: they update
only web credentials and session state, without decrypting or rewriting the tool store.
Before migration they also clean up the relevant legacy entries through the existing writer;
an operation that actually needs an unavailable encrypted store still fails closed.
Authenticated web password change
decrypts the seed with the current password and atomically reencrypts it under the new password.
Credential maintenance and TOTP enrollment rotate a
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

## CLI provider login

Logged-in users can choose **CLI login** from the header account menu to authenticate
Claude, Codex or Grok through the installed provider CLI. Select a provider
or an existing Claude slot, or add a named Claude slot, then confirm before starting.
The dialog opens authentication links in a new tab, copies device codes and accepts
a pasted authorization code when needed. If prompt detection fails, expand the raw
output and use the one-line input. Display-language changes preserve the job and typed input.
Gemini web CLI login is unsupported because personal-account Gemini CLI OAuth ended on 2026-06-18.

Only one login job can run at a time across all providers and browser sessions.
Only the browser session that started a job can read its output, send input or cancel
it; other sessions see that a login is in progress. No additional password or TOTP
confirmation is required after web login. Cancel stops the job; jobs also time out
after 10 minutes. A new Claude slot is added to `claude_accounts` only after successful
login, and existing Claude slots can be authenticated again without being recreated.
If a newly authenticated provider was not available when the bot started, the dialog
instructs you to restart the bot before using it.

**Codex clears the existing login as soon as authentication starts; cancelling does
not restore it.** The start confirmation explains the provider's replacement behavior.

Login input and output stay out of DT Agent's application logs, conversation history,
durable outbox and Telegram mirrors. Output is held only in a bounded in-memory buffer
and is discarded 10 minutes after the job ends. Input echoed by the CLI remains visible
in the owner-only raw output; one-time codes are not masked. This guarantee does not cover
log files written by the native provider CLI itself; this feature does not manage those files.

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

## Web commands

The web bridge supports the same 29 command names as the main Telegram bot.
Names ignore leading whitespace, case and an `@bot` suffix; arguments retain
their original case. Unknown slash commands remain normal messages.
`GET /commands?lang=ko` returns `{ "commands": [{ "name": "new", "description": "..." }] }`.
The language must be one of the nine supported locales; omitting it uses the bot
language without changing Telegram's language.

Known commands execute outside the normal streaming turn. Inputs are recorded
with source `web`, role `user` and `kind="command"`; results use role `bot`.
Their Telegram mirrors use the existing durable outbox. Input mirrors say
"Command executed: /command" instead of the normal web-message prefix. Selector
callback data, URL buttons and keyboard rows survive queuing and restart.
The JSON response contains `text` and `buttons`; callback and URL buttons use
`{text, callback_data}` and `{text, url}`, respectively.

Read-only quick commands and interrupt/stop controls can run while a turn is
active. State-changing commands such as `/new`, `/reset`, `/session` and
`/agent_start` return `409 web_turn_running` when their session is busy; Telegram
queues those commands instead. `/new @topic` also checks and locks its target
topic. `/stop_all` discards Telegram's pending messages and this chat's waiting
web turns before using the existing process-kill path.

`/session` uses the existing named background-session submission path. A normal
`@session-name` follow-up remains a web turn in the current topic. File-browser
file requests likewise continue through that topic's normal turn path.
`/restart` saves notification state, records and queues the result, then writes
the restart marker; any pending mirror resumes after restart.

`/secret`, including bot suffixes and additional arguments, is intercepted before
turn submission or recording of raw command text. Web dialogs support listing,
setting and deleting secrets, with overwrite/delete confirmation and a two-minute
input window. Values go directly to `POST /api/topics/{tid}/secret/value`;
confirmations use `POST /api/topics/{tid}/secret/confirm`. Only sanitized command
text and result messages are recorded and mirrored. Values and input tokens never
enter conversation history, the outbox, mirrors, logs or CLI input.

## Master-key and secret dialogs

The header lock badge (emphasized while locked) and account menu open the **Master key** /
**Manage secrets** modal. Setup, unlock, key change, expiry including unlimited, and manual lock
use the same process vault as Telegram. Master-key fields use password inputs with
`autocomplete=off`. Before setup, the secrets tab still lists/adds/updates/deletes plaintext
secrets and recommends encryption; value viewing is disabled until setup. The existing chat
`/secret` input dialog remains available.

Lists contain names only. **View value** requires the master key again and returns one value;
copying is explicit. It is removed from the DOM after 30 seconds, on close/Esc, route navigation,
logout or a vault invalidation. Late responses cannot reopen a closed dialog. Keys and values
never enter chat history, mirrors, drafts, local storage or debug/access logs. The browser APIs
use JSON bodies with a 64 KiB limit and `Cache-Control: no-store` on every new response.

Browser prefix `/api` and bridge prefix `/webchat/v1` share these resources:

| Method / resource | Body or result |
|---|---|
| `GET /masterkey/status` | `{state, ttl_seconds, remaining_seconds}` |
| `POST /masterkey/setup` | `{master_key, confirm_key}` |
| `POST /masterkey/unlock` | `{master_key}` |
| `POST /masterkey/lock` | `{}` |
| `POST /masterkey/change` | `{current_key, new_key, confirm_key}` |
| `POST /masterkey/ttl` | `{ttl_seconds}` (0 = unlimited) |
| `GET /secrets/keys` | `{keys: [names]}`; no values |
| `POST /secrets/set` | `{key, value}` |
| `POST /secrets/delete` | `{key}` |
| `POST /secrets/reveal` | `{key, master_key}` → `{value: one_value}` |

All browser routes require the existing session/client/CSRF checks; bridge routes require their
bridge token. Wrong master keys return **422 `MASTER_KEY_INVALID`**, not session-expiry 403.
Shared backoff returns 429 `SECRETS_RATE_LIMITED` with `retry_after`; locked operations use
409 `SECRETS_LOCKED`; setup blocked by legacy web credentials uses
409 `WEB_AUTH_MIGRATION_REQUIRED`. `MASTER_KEY_TOO_SHORT` and `MASTER_KEY_MISMATCH` use 400.
The existing WebSocket forwards only `{type: "secrets_changed"}` invalidations for setup,
unlock, lock/expiry, TTL, key change and secret writes; open clients requery status/names.
No value is included in these events.

## Bridge contracts

The browser app relays bridge JSON statuses and error codes. Bridge routes use
`/webchat/v1`; browser routes use `/api`. Errors use `{ "error": "code" }`.

| Operation | Status and response |
|---|---|
| Delete topic while its session lock remains held beyond 30 seconds | 409 `topic_busy`; retry after the topic finishes stopping |
| Send to or abort Unknown topic | 409 `read_only`; select a real topic |
| List commands (`GET /commands`, optional allowed `lang`) | 200 `{ "commands": [...] }`; invalid language: 400 `bad_request` |
| Execute a supported command | 200 `{ "text": "...", "buttons": [...] }` (`buttons` may be null) |
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
  "credentials": {"storage": "encrypted", "password_configured": true, "totp_configured": true, "migration_required": false},
  "webchat": {
    "enabled": true,
    "exposure": "direct",
    "domain": "chat.example.com",
    "allowed_ips": ["203.0.113.0/24"],
    "trusted_proxies": [],
    "bind_address": "127.0.0.1",
    "direct_bind_address": "",
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

Credential diagnostics use `storage: encrypted|legacy|unconfigured`; a corrupt new credential file adds `credentials_invalid` to `errors`. The report never contains the hash, seed or values.

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

On the configured web chat domain, binary upload chunk PUTs and POST requests
to `/api/topics/{tid}/messages` and `/api/topics/{tid}/buttons` skip CrowdSec
AppSec when the original request URI matches the path exactly, with no query
string. The IP allowlist, CrowdSec IP decisions and the app's session and CSRF
checks still apply. Upload creation, status, deletion and completion continue
through AppSec.

After a successful container swap, `scripts/update.sh` runs
`python3 scripts/webchat.py up` when the repository's `.env` lists `webchat` in
`COMPOSE_PROFILES`. Rerun that command manually on the first upgrade with
`update.sh` from a version that predates this step, because the older script
running that upgrade lacks the gateway refresh step. Also rerun it manually
after upgrading without `update.sh`.

After sign-in, the chat header displays the installed release version (hidden at widths of 760px and below; use `/version` there).
The header globe selects one of nine web display languages, applied immediately and saved
in browser localStorage across reloads; bot and Telegram language settings stay unchanged.
The header Rename dialog updates the current topic name while keeping the conversation open.
Clicking outside a dialog follows the same cancellation behavior as pressing Esc.

In the conversation, use **Copy code** at the top right of a code block to copy
its contents. This works in your messages and in agent responses as they stream.

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
Five failed attempts from one client IP within 15 minutes lock login for that IP.
Incorrect authentication codes submitted after the password is accepted count across
all client IPs. This applies to the login code step and to password changes after the
current password is accepted. Twenty such failures within 15 minutes lock login for
all client IPs. Password verification failures count only toward the per-IP lock.
The login page is meant to be reachable only from the gateway allowlist or a trusted proxy.
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

Open the slot’s three-dot menu and confirm **Log out** for an OAuth slot or **Disconnect key** for a connected API key slot. The same actions are available in the main Telegram bot’s `/accounts`. A logged-out slot keeps its priority and is skipped during normal rotation until you log in again. If every slot is marked expired, the top-priority slot is still tried once. Running turns on the default `claude` slot can end with an authentication error if you log out at the same time. Usage for the in-flight turn may be recorded again when it finishes. Deleting a slot, logging out of it or disconnecting its API key clears its saved usage record.

The slot list identifies entries with `kind: "oauth"`, `kind: "api_key"` or `kind: "api_key_detached"`. The API key badge shows whether the key is connected. Disconnecting leaves the stored key and helper script in place. Reconnect the key on the host by restoring the value from the slot’s `api-key-helper.detached` file to `apiKeyHelper` in that slot’s `settings.json`, or run `./scripts/login.sh claude-api` if the slot is named `claude-api`. Web OAuth requests for the reserved names `api`/`claude-api` or an existing connected or disconnected API key slot return `api_key_slot` before a login job or filesystem change. Shared-setting repair preserves the API slot’s regular settings file and helper. To set up a new API key slot, store the key with `/secret set ANTHROPIC_CREDIT_API_KEY` in the bot’s private chat, then run `./scripts/login.sh claude-api` on the host. `/accounts` and `/usage` also identify API key slots. See [setup and billing](../installation.md#claude-api-key-slot): credits apply to the key's Console organization, and an inherited `ANTHROPIC_API_KEY` in the bot's `.env` makes all slots use API billing. Encrypted registration warns and skips key-name verification; calls fail while the store is locked or the key is missing.

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

## Provider usage

The header shows Claude slots in priority order and highlights the next visible
slot in rotation. It shows the stored five-hour and seven-day usage on separate
lines when both windows are available. When the next chip and overflow button
need more room, the chips show durations and percentages without bars.

The Codex chip follows the Claude slots when an observation is available. When
chips fold into the overflow menu, Claude slots take priority over Codex. Open
any chip or the overflow button to see every available window with its usage,
reset time and capture time. API-key slots have no OAuth usage data. The header
hides OAuth slots without credentials and slots with disconnected API keys.

Use the up and down buttons in the CLI login dialog or the header usage popover
to swap a ranked Claude slot with the adjacent ranked slot you can see. In the
usage popover, a slot hidden between two visible slots keeps its position, while
a one-step move in `/accounts` swaps with the hidden slot. If the order changed since
the dialog or popover last loaded it, the browser reloads the order so you can
retry the swap. If that reload fails after a 409 conflict, the screen keeps
showing the old order, the server rejects another swap from that order, and
the new order appears once a reload succeeds.

The popover shows when the browser last fetched state and when each usage
snapshot was captured. State refreshes on login, reconnect, when the page
becomes visible again, when a web turn ends, and when a new Codex usage
observation is stored. Use the refresh button to read the latest stored usage.

State also carries the last successful Codex usage observation held by the bot.
Codex chat turns request a refresh after completion in the background, and
`/usage` also updates the observation on success. A failed query leaves the last
successful value available. Turn-end refreshes share pending queries but always
request fresh data once a query finishes. Reading state uses the stored value
and does not start a query.

## Topics, turns and queue

Web turns share the target Telegram `tg:` session key and `LockPool` lock.
Stopping a turn targets only that chat/topic with `kill_by_chat_topic`. Abort and
topic mutations use a 45-second bridge request timeout. Message POSTs use the same
limit because they also accept `/stop`; normal sends return without waiting for
the turn to finish.
The answering indicator follows activity in the selected topic, including the general conversation.
If a topic list request fails right after a web turn ends, or while reconnect
recovery is still waiting for its own request, the indicator or a finished turn's
streaming bubble can be wrong until the next event or refresh. While the indicator
is shown only from a snapshot, rechecks stop after about two minutes.
Topic creation, rename and deletion call Telegram's forum topic API; deletion
also removes the exact session, history and queued deliveries for that topic.
The **Imported history** group covers topics with zero live entries and activity
older than 30 days. Real imported topics remain usable and manageable.
**Unknown topic** is a read-only history group.

The topic sidebar uses the unread counts returned by `GET /topics`. With a
saved read cursor, the count includes each sequence after that cursor once.
Without a saved cursor, it includes every history row. Read cursors are shared
across web clients. Reading a topic or marking all topics as read hides the
affected badges immediately while the read state is saved. If the server cannot
provide an unread count, the browser shows the last confirmed count and retries.
If saving the read state fails and a later topic-list or reconnect request
fails before the list recovers, the error notice can appear twice.

Badge refreshes after new entries or read-state changes normally start within
150 ms. Reconnects and topic-list events request the list immediately.
After a reconnect, a successful topic list update lets the browser reload the
open conversation, even if the reconnect's own list request fails. A failed
history reload uses the existing retry delays to try again.
A reconnect that overlaps an ongoing history recovery may request the same
history twice.
A continuous stream can delay the start up to one second when no read write or
list request is in progress. If new changes keep arriving faster than responses,
the badges may only catch up when the stream settles. A history lock on one topic
can delay updates for the entire list. The browser retries failed updates up to
four times at intervals of 1, 2, 4, and 8 seconds (15 seconds total in retry
delays, plus request time). When those retries run out, the last confirmed
counts remain until the next change, reconnect, or return to the tab.

Webchat preserves line breaks inside paragraphs, list items, and escaped HTML
text in user messages.
Webchat displays code blocks and inline code from new Telegram text messages.
Webchat shows the original message when a backtick appears outside Telegram
code, including in a reply quote, when code entities touch or overlap, when
inline code contains a line break, when a backslash immediately precedes a code
entity, when Telegram code formatting covers only part of a URL, or when the
text outside code has an unmatched opening angle bracket before a code entity,
as in an HTML tag or a comparison such as `a < b`.
Copying the whole message uses the original recorded text.

`GET /outbox` returns `{ "items": [...] }` containing web mirrors and buffered
bus deliveries, including public origin/title metadata. See
[recording and recovery](chat_log.md) for ordering, chunk splitting and the
possible duplicate chunk after an interrupted Telegram response.

### Tool activity

Web chat shows tool names for turns sent from the browser. Supported Claude and
Codex built-in tools can show one expandable detail per call. Gemini, Grok, MCP
and unclassified tools show names only.

Set `webchat.tool_details` in `config.json` and restart to apply changes.

- `"off"` (default) shows tool names in live turns and skips generating
  details or retaining completed activity.
- `"memory"` shows masked details and keeps completed activity in process
  memory until restart. `webchat.tool_details_memory_limit` sets the number
  of retained replies (default 100, range 1–5000). The oldest replies are
  evicted first. Deleting a topic removes its retained activity. Each reply
  holds up to 64 KiB of serialized activity, so memory use grows with the
  limit (a few MiB at 100, up to hundreds of MiB at 5000).
- `"file"` shows masked details and saves completed activity in the reply's
  history sidecar. It preserves the behavior of v1.5.6.

Upgrading from v1.5.6 defaults to `"off"`. Choose `"memory"` or `"file"` to
keep showing details. Switching away from `"file"` hides previously saved
activity from history. The saved activity remains on disk and is visible
again in `"file"` mode. A tab that already received details can retain them
until it closes.

This setting controls only what web chat shows and retains. With
`log_level` set to `DEBUG`, the bot log records the start of each provider
stream line in every mode, which can include tool input.

The bridge finds registered values and recognized token patterns in
control-stripped text, merges overlapping or adjacent matches, and masks them
before cutting details to 2,000 characters. This applies to values typed
directly into a command as well. Empty masked details are omitted. A match
for a registered value shorter than four characters suppresses the affected
detail or replaces a tool name from the provider with `tool`. A vault error
has the same effect. Fixed built-in tool names remain visible when the vault
is locked or a registered value overlaps their spelling. If vault lookup
returns no vault, provider-supplied names receive token-pattern masking and
details are omitted. A stored value made entirely of asterisks can coincide
with the `***` mask.

Unregistered secrets outside the recognized patterns can remain visible.
Classic GitHub token prefixes require at least 20 alphanumeric characters.
An OpenAI-style `sk-` prefix immediately after an ASCII letter or digit is
outside the token pattern. An unclosed PEM private key block is masked
through the end of the text.

- WebSocket `tool` events carry `tid`, `turn_id`, `seq`, `name` and an optional
  `detail` object with `kind`, `text` and `truncated`. The original tool
  parameters stay on the server.
- `GET /topics` includes `activities` on each turn snapshot alongside the
  existing `activity` indicator. Each list item contains `type`, `name`, `seq`,
  `offset` and optional `detail`. The offset counts Unicode code points in the
  streamed answer when the call occurred.

Each turn retains up to 200 activity items and 64 KiB of serialized activity.
The bridge removes the oldest items above the count limit first. If the byte
limit is still exceeded, it removes the oldest details, then the oldest items
as needed. Live events still go out in full, and a tab keeps the activity it
has already received when it reconnects.

In `"memory"` and `"file"` modes, `GET /topics/{tid}/messages` includes
retained activity as `activity` on the recorded reply. A turn without tool
calls or a reply has no activity in message history. See
[recording and recovery](chat_log.md#runtime-and-storage) for sidecar and
rollback behavior.

Reloading during a turn restores the activity list from the snapshot. Because
the browser lacks the previously streamed answer text, restored rows appear
before newly received text. If the completed reply has retained activity, the
browser uses the saved offsets with the full reply to restore the completion
summary. A completed summary restored from history omits the elapsed time.

## Web commands

The web bridge supports the same 31 command names as the main Telegram bot.
Names ignore leading whitespace, case and an `@bot` suffix; arguments retain
their original case. Unknown slash commands remain normal messages.
`GET /commands?lang=ko` returns `{ "commands": [{ "name": "new", "description": "..." }] }`.
The language must be one of the nine supported locales; omitting it uses the bot
language without changing Telegram's language.

Known commands execute outside the normal streaming turn. Inputs are recorded
with source `web`, role `user` and `kind="command"`; results use role `bot`.
Their Telegram mirrors use the existing durable outbox. Input mirrors say
"Command executed: /command" instead of the normal web-message prefix. If a web
command button's selection is shown on the original Telegram message, the bridge
skips the separate input mirror and still mirrors the result. Selector callback
data, URL buttons and keyboard rows survive queuing and restart.
The JSON response contains `text` and `buttons`; callback and URL buttons use
`{text, callback_data}` and `{text, url}`, respectively.

Read-only quick commands and interrupt/stop controls can run while a turn is
active. A quick command such as `/status` preserves the stop status of a turn
that is still ending. State-changing commands such as `/new`, `/reset`, `/session` and
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
confirmations use `POST /api/topics/{tid}/secret/confirm`. The bridge records
sanitized command text and result messages. The same command-button rule applies
to Telegram mirrors. Values and input tokens never enter conversation history,
the outbox, mirrors, logs or CLI input.

## Cron menu

Open **Cron** in the chat header or send `/cron` in the web composer. The modal
lists registered jobs with their schedule, last run, destination, delivery mode
and autocompact setting. Select a job to read its instruction and script names.
On screens up to 768 px wide, the list and detail use separate views.
Telegram `/cron` keeps its existing selector.

Choose **Create cron** to enter a name, instruction, schedule and IANA timezone.
The four presets cover daily, weekdays, selected weekdays and every 1–23 hours;
use a direct five-field cron expression for other schedules. The server previews
the next three runs after a 300 ms pause. It searches up to five years, so annual
schedules show three runs; schedules with fewer matches can still be saved.
The timezone defaults to `user_timezone`. The destination is the topic from which
the job is created and is determined by the server. Delivery defaults to `inject`
when a chat is available.

Autocompact initially copies the creation topic's stored setting. If that topic
follows the global setting, the job stores `null` and continues following global
changes. Choose global inheritance, the provider default, off, or 100k–1M tokens;
a blank token input uses 500k. Existing provider, model and effort overrides remain
unchanged when editing a job.

**Edit cron** changes the instruction, schedule, timezone, delivery mode and
autocompact. The instruction is limited to 256 KiB. A concurrent instruction edit
returns 409: reload the job before saving again. Successful or rolled-back body
updates retain the ten most recent instruction backups in the task's `.history/`.
The list switch enables or disables scheduling. **Delete** asks for confirmation,
moves the task folder into `cron_tasks/.archive/<id>-<timestamp>`, then removes the
registry entry. If the registry write fails, the folder is restored.

Creation, editing and detail responses include a static secret-use check of the
instruction and scripts. A warning lists key names only and does not block saving.
Secret-dependent runs can fail while an encrypted secret store is locked; unlock
it before execution. Static checks can miss dynamic names, imports and external
commands, and comments can produce false positives. No warning does not guarantee
that the task uses no secrets. This check does not mask values: task files must
never contain secret values or master keys.

## Attachments

Use the attachment button beside the message input to select multiple files, drop
files onto the composer, or paste images/files from the clipboard. Clipboard file
support depends on what the browser and source application expose. Review the
attachment tray before sending: it shows names, sizes and image thumbnails; remove
an item with its remove button. A message can contain attachments without text.
Slash commands do not accept attachments. The input grows with new lines up to 40%
of the visible height; the attachment tray scrolls independently.

Each image has its own processing menu. **Original** is the default and uploads
the selected file's bytes unchanged. Processing happens in the browser only when
selected. The options show the estimated output file size and, for supported
images within the processing limits, the original pixel dimensions. Resize
modes also show the resulting pixel dimensions when a smaller file is produced.
If processing yields a file as large as or larger than the original, the options
show the original size and dimensions with a notice. The original is sent
unchanged.

| Choice | Uploaded result |
|---|---|
| Original | Original bytes, format and resolution |
| Resize | Scale both dimensions to 80% (default), 50%, or 25%. Encode JPEG inputs only as JPEG. For other inputs, keep the smaller of the original format (PNG for formats other than PNG or WebP) and lossless WebP if supported |
| Compress | Convert to WebP at the selected quality, retaining the dimensions |
| Resize + compress | Apply both the selected scale and WebP quality |

Browser processing accepts inputs up to 32 MiB and 32 million decoded pixels.
Larger files can still be sent as originals within the configured upload limit.
Canvas processing produces a still image; choose Original to retain animation.
Cancelling the processing menu keeps the previous selection and result.

### Limits and interrupted transfers

Open **Account → Attachment settings** to set the maximum size per file in MiB
and maximum files per message. The defaults are 2048 MiB (2 GiB) and 10 files.
The dialog shows the administrator's server maxima, rejects values outside them,
and saves shared web-chat preferences in `webchat/attach_settings.json`.
New selections and upload attempts fetch the current limits; the server also
validates size, ownership, topic and attachment count. Administrator hard caps are
`webchat.attach_max_file_bytes` and `webchat.attach_max_files_per_message`.
Preferences are clamped to those caps; changing the administrator config requires
a restart, while saving preferences in the dialog does not.

Uploads are sequential chunks, with per-file progress showing bytes acknowledged
by the server. Hash calculation and final verification have separate states.
**Resume upload** after a retryable failure or **Cancel transfer** uses the
server's received offset in the same tab, so already accepted chunks are skipped.
If final verification rejects the file type, the attachment shows the reason
and can be removed. Cancellation keeps the draft text and attachments. Removing
an unfinished attachment requests cancellation of its server session. Refreshing or closing the tab discards the
in-memory files and resume state; this is not cross-tab or reload recovery.
If message submission was not confirmed after uploading, check the conversation
before retrying. Cancellation/removal is disabled while submitting the message.

`webchat.upload_chunk_bytes` defaults to 8388608 bytes (8 MiB), configurable from
1 to 8 MiB. Binary chunks use `application/octet-stream`; the web app streams them
to the bridge rather than buffering a whole file. Set proxy/WAF request limits
for the chosen chunk size and verify the deployed ingress separately. The server
verifies the full SHA-256 before completion. It determines the stored MIME type
and extension from the file contents. A specific browser MIME type that differs
from the detected type is rejected if either type is an image type. The server
reserves disk space for remaining file bytes and chunk staging while preserving
a 1 GiB free-space floor. Inadequate space rejects or stops the transfer.
Incomplete sessions expire after 24 hours of inactivity. Their private temporary
files live in `workspace/webchat_files/.incoming/`;
completed files use UTC date directories and random names with verified extensions
under `workspace/webchat_files/`. Display names never select the storage path.
Completed uploads that have not been sent expire 24 hours after completion,
including the exact 24-hour boundary, regardless of `cleanup.enabled`. This is a
maximum retention limit: the general `cleanup.media_files_days` policy also
applies to unsent completed files and may delete them earlier when its configured
period is shorter. The upload cleanup cycle removes expired files and private metadata. Removing an unused
completed draft from the tray also deletes its server-side file and metadata.

Sent attachments follow `cleanup.media_files_days` (30 days by default), using
each file's modification time just like Telegram media. This daily cleanup runs
only when `cleanup.enabled` is true. The general cleanup excludes `.incoming/`;
the upload cleanup cycle manages it and removes metadata for deleted originals.
The separate attachment index remains so history can show **Deleted after the
retention period**. Expired cards have no download or original-image action;
download and preview URLs return `410 attachment_expired` rather than a server
error. Cached previews are unavailable once their original has been removed.

The agent receives a short notice explaining the configured retention period
(or that automatic cleanup is disabled). If you ask the agent to keep a file, it
copies the attachment to a location inside the workspace that is outside all
cleanup-managed directories. Attachments in the temporary folder remain subject
to the retention policy.

### Message cards, original images and Telegram mirrors

Message cards show name, size, MIME type and a download action. Safe images also
show a server-generated thumbnail (longest edge 512px); thumbnails do not replace
or modify the stored original. Click an image to view its original in a modal,
fit it to the screen or zoom to 200%, open it in a new tab, or download it.
Esc closes the modal and restores focus to the image control. When a thumbnail
cannot be generated, a safe image can still offer View original. Unavailable files
or damaged attachment metadata do not prevent the message text from loading.
Existing Telegram incoming-file records and bot `<file:...>` records use the same
cards, including records created before the separate attachment index existed.

Web messages are mirrored to the same Telegram topic through the durable outbox:
text first, then attachments in selection order. JPEG/PNG/WebP/GIF are sent as
photos; other formats are documents. The current sender uses a 10000000-byte
photo limit and a 50000000-byte document limit. Larger attachments stay available
in web chat and are mirrored as a localized notice containing their name and size.
Telegram's other media restrictions can still reject an otherwise small file.
Existing outbox retry rules apply; missing files use the unavailable-file skip path.

### Download boundary and API

Downloads, thumbnails and originals require an authenticated browser session.
The server derives files from the message's attachment index or supported legacy
record, rather than accepting a browser-supplied filesystem path. Eligible locations
are `workspace/webchat_files/`, `workspace/telegram_files/`, and paths recorded in
bot entries using `<file:...>` tags (which can be outside the workspace). An arbitrary
path typed by a user does not grant access. Only regular files are served; parent
traversal and symlink components are rejected. This web-specific boundary does not
change other transports' `file_access` policy.

Downloads default to `Content-Disposition: attachment`. Inline viewing requires
verified safe raster image content and an explicit inline allowance; SVG/HTML and
other inline-prohibited files remain downloads. Responses use
`X-Content-Type-Options: nosniff` and private caching. Range requests and streamed
responses allow downloads without buffering the entire file in the web app.
Attachment metadata is stored separately in `webchat/attachments.jsonl`, keyed by
message ID; the history format and stored text remain unchanged. The agent receives
`[INCOMING FILE]` blocks with source Web and stored paths.

Browser resources below use the `/api` prefix; the bridge uses `/webchat/v1`:

| Method / resource | Purpose |
|---|---|
| `GET /attach-settings` | Effective limits, server maxima and chunk size |
| `PUT /attach-settings` | Save `max_file_bytes` and `max_files_per_message` |
| `POST /topics/{tid}/uploads` | Start with display `name`, declared `size` and candidate `mime` |
| `GET /topics/{tid}/uploads/{upload_id}` | Get acknowledged `offset` for resume |
| `PUT /topics/{tid}/uploads/{upload_id}` | Stream a binary chunk at the `Upload-Offset` header |
| `POST /topics/{tid}/uploads/{upload_id}/complete` | Verify the supplied `sha256` and complete |
| `DELETE /topics/{tid}/uploads/{upload_id}` | Delete an unfinished or completed unused upload; used uploads are rejected |
| `POST /topics/{tid}/messages` | Send `text`, `client_id` and optional `attachment_ids`; completed uploads are single-use and scoped to the same session/topic |
| `GET /topics/{tid}/messages/{entry_id}/attachments` | List authorized attachment metadata and URLs |
| `GET /topics/{tid}/messages/{entry_id}/attachments/{attachment_id}` | Download; `?inline=1` requests a permitted original image |
| `GET /topics/{tid}/messages/{entry_id}/attachments/{attachment_id}/preview` | Retrieve an available thumbnail |

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
| `POST /masterkey/ttl` | `{ttl_seconds, master_key?}` (0 = unlimited); key required to extend a configured finite TTL or switch it to unlimited |
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
No value is included in these events. The expiry form adds a password field with
`autocomplete=off` only when extending a configured policy; reducing it needs no key.
`MASTER_KEY_REQUIRED` reports missing confirmation without counting a failed key attempt.
Malformed vault settings keep the bot running but block a configured vault with
`INVALID_VAULT_CONFIG`; status then adds `reason` with that code. Repair or delete
`config/secret_vault.json` and restart to recover; leave the encrypted store intact.

## Bridge contracts

The browser app relays bridge JSON statuses and error codes. Bridge routes use
`/webchat/v1`; browser routes use `/api`. Errors use `{ "error": "code" }`.

| Operation | Status and response |
|---|---|
| Read state (`GET /state`) | 200 with `telegram`, `outbox`, `claude` (`{next, slots}` or `null`), and `codex` (`{captured_at, windows}` or `null`). The browser relay adds `bot`, `web_sessions`, and `version`. |
| Swap Claude slots (`POST /cli-login/slots/{slot}/swap/{other}`) | Send JSON with `view` (`login` or `usage`) and `expected` (the full ordered list of ranked Claude slots shown in that view). 200 `{"ok":true}`; 409 `slot_order_changed` if the order differs, including if a slot was deleted; 400 `invalid_swap` if the slots are not adjacent in the matching order; 400 `bad_request` for an invalid body. |
| Delete topic while its session lock remains held beyond 30 seconds | 409 `topic_busy`; retry after the topic finishes stopping |
| Send to or abort Unknown topic | 409 `read_only`; select a real topic |
| List topics (`GET /topics`) | 200 with `topics`, including each topic’s `seen` cursor, `unread` count, and `busy_kind` (`task` for background tasks only, `answering` for other busy topics, or `null` when idle). If the stored read state cannot be read, `seen` and `unread` are `null`. A history lock timeout returns 503 `history_busy`. |
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
    "default_chat_id": 0,
    "attach_max_file_bytes": 2147483648,
    "attach_max_files_per_message": 10,
    "upload_chunk_bytes": 8388608,
    "tool_details": "off",
    "tool_details_memory_limit": 100
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
MAINMEMORY web saves and shared-memory synchronization share a file lock.
A noncooperating external writer can still race after that check and before
replacement. Coordinate external edits with web saves and retain backups for
recovery. This limitation also applies to MAINMEMORY saves, which perform a
single comparison and atomic replacement.

Credential diagnostics use `storage: encrypted|legacy|unconfigured`; a corrupt new credential file adds `credentials_invalid` to `errors`. The report never contains the hash, seed or values.

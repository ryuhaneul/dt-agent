# messenger/telegram/

Telegram interface layer (`aiogram`): handlers, middleware, callback routing, streaming UX, startup lifecycle.

For the Matrix transport equivalent, see [matrix.md](matrix.md).
For shared messenger protocols and the transport registry, see
[messenger.md](messenger.md).

## Files

- `messenger/telegram/app.py`: `TelegramBot` class, handler registration, callback routing, group management commands
- `messenger/telegram/startup.py`: startup sequence (orchestrator creation, bus wiring, recovery, sentinels)
- `messenger/telegram/callbacks.py`: shared selector callback helpers (`SelectorResponse` editing)
- `messenger/telegram/middleware.py`: `AuthMiddleware`, `SequentialMiddleware`, queue controls, quick-command bypass
- `messenger/telegram/message_dispatch.py`: shared streaming/non-streaming execution paths
- `messenger/telegram/handlers.py`: command helper handlers (`/new`, `/stop`, generic command path)
- `messenger/telegram/chat_tracker.py`: persisted group chat activity (`chat_activity.json`) for `/where` and group audits
- `messenger/telegram/topic.py`: topic/session key helpers + topic-name cache
- `messenger/telegram/topic_naming.py`: bounded implicit DM topic naming, isolated title generation and persistent attempt flags
- `messenger/telegram/file_browser.py`, `messenger/telegram/sender.py`, `messenger/telegram/media.py`, `messenger/telegram/welcome.py`, `messenger/telegram/formatting.py`, `messenger/telegram/typing.py`
- `messenger/telegram/transport.py`: Telegram transport adapter for `MessageBus`

## Command ownership

Bot-level handlers:

- dedicated transport handlers: `/start`, `/help`, `/info`, `/showfiles`, `/stop`, `/stop_all`, `/restart`, `/new`, `/session`, `/sessions`, `/tasks`, `/agent_commands`
- `/reset` is forwarded through `_on_command` to the orchestrator
- main-agent only: `/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`
- hidden but supported: `/where`, `/leave` (not in Telegram command popup)

Immediate middleware-handled command path (pre-lock, no normal dispatch):

- `/stop`, `/stop_all` and `/interrupt` (also `!interrupt` and the `@botname` suffix); a bare word such as `esc` is an ordinary message

Orchestrator-routed commands:

- `/status`, `/memory`, `/model`, `/effort`, `/cron`, `/diagnose`, `/reset` are forwarded by `_on_command`
- `/sessions` and `/tasks` have dedicated transport handlers that invoke the corresponding orchestrator selectors

## Middleware behavior

### `AuthMiddleware`

- private chats: requires `user_id in allowed_user_ids`
- groups/supergroups: requires both
  - `group_id in allowed_group_ids`
  - `user_id in allowed_user_ids`
- optional rejected-group callback feeds `ChatTracker`

`group_mention_only` is not auth. It is applied later as message-content gating.

Media messages in groups also respect `group_mention_only`:

- when `group_mention_only=false`: media is always processed in authorized groups (no mention required)
- when `group_mention_only=true`: media requires a mention or reply addressing the bot (same gate as text messages)

This is checked in `_resolve_text()` via `is_media_addressed(message, bot_id, bot_username)`.

Actual allowlist denials in Telegram, Matrix and Slack emit INFO `Allowlist rejection chat_type=... chat_id=... user_id=... topic_id=...`, containing metadata only, never message content (the Telegram bot's own messages and service messages, such as a topic it created or renamed, are dropped without this log once the bot id is known). The common `RejectionLog` helper limits each retained chat/user pair to one entry per 600 seconds (topics share that pair’s limit). Each logger instance retains up to 1,024 pairs and evicts the oldest when full; an evicted pair can log again. This does not change each transport’s authorization/mention policy or its handling of non-message events.

Foreground user conversations recheck the **resolved session provider** authentication immediately before CLI execution. Text, media and prompt-button turns, including named-session foreground follow-ups, return localized host-repository `./scripts/login.sh <provider>` instructions and do not run the CLI when unauthenticated. Authentication changes are reread on the next user turn; Gemini can use its configured API key. Telegram, Matrix, Slack and direct API use these shared orchestrator flows. Inter-agent, cron and webhook task execution paths are unchanged by this foreground check.

### `SequentialMiddleware`

Flow order:

1. interrupt/abort checks before lock (`/interrupt`, `/stop_all`, `/stop`, abort phrases)
2. quick-command bypass
3. dedupe by `chat_id:message_id`
4. queue indicator when lock is busy (`mq:<entry_id>` cancel callback)
5. lock by `SessionKey.lock_key` (topic-aware)

Quick commands:

- `/status`, `/memory`, `/cron`, `/diagnose`, `/model`, `/showfiles`, `/sessions`, `/tasks`, `/where`, `/leave`

Queued text and media retain their original display text. Once the lock is acquired, middleware makes send time, elapsed wait and arrival position available only to that receiving task; normal/Named prompt assembly adds the advisory at CLI dispatch. Slash commands, command callbacks and cancelled entries are excluded. See [queued user message context](messenger.md#queued-user-message-context).

Queue APIs:

- `is_busy(chat_id)`
- `has_pending(chat_id)`
- `cancel_entry(chat_id, entry_id)`
- `drain_pending(chat_id)`

## Topic support

`get_session_key(message)` returns:

- `SessionKey(chat_id, topic_id=message_thread_id)` when `message.is_topic_message` is true (group or private-chat topics)
- `SessionKey(chat_id, topic_id=None)` otherwise, even if `message_thread_id` is present

The Telegram selector is `topic_id = message.message_thread_id if message.is_topic_message else None`; a thread ID alone does not select a topic session. Source: `ductor_bot/messenger/telegram/topic.py:37`.

Implications:

- Telegram topic messages have distinct session keys (including private-chat topics); API callers can still share the same numeric Telegram key as described in [api.md](api.md)
- locks are topic-aware
- `/new` and `/model` operate per topic when called inside a topic
- `/new @topicname` resets a specific topic session by cached name lookup

`TopicNameCache` is seeded from persisted sessions at startup and updated from forum topic create/edit events.

### Automatic DM topic names

`telegram_dm_topic_auto_name` defaults to true and hot-reloads. Main and bot-backed sub-agents name only their own private-chat topics for which a `forum_topic_created` service message explicitly says `is_name_implicit=true`. A missing/false flag or missing creation event never establishes eligibility; group topics are excluded. Any observed `forum_topic_edited` event, including icon-only edits, makes the topic user-owned. Topic naming uses `is_name_implicit` and `edit_forum_topic(chat_id, message_thread_id, name)`.

On receipt, `SequentialMiddleware` observes private-topic creation/edit ownership and schedules naming after dedup, before waiting on the conversation lock. This makes an observed user edit win even while its service handler is queued. `_on_message` also supports direct dispatch without double scheduling; group/command/button paths do not start naming. Plain text or a media caption is the only title input: reply context, downloaded media prompts and conversation answers are excluded. The title task runs independently of reply delivery and may rename before the first reply. It uses the active session provider, with Claude haiku or the active model for other providers, low effort and one turn. The CLI factory/send path uses a temporary working directory without resume/continue and without SessionManager writes.

The prompt is independently written, informed by Hermes' subject-naming rules rather than copied code/prompt text. It requests a noun phrase in config.language, no answers/outcomes/status, preserves technical terms/numbers/filenames, and asks for JSON with a title or null. It aims for about 12 characters, abbreviates tool, product or project names of three or more words to Latin capital initials (counting Hangul transliterations as the English words they spell), keeps one- or two-word names, technical terms, numbers, filenames and error codes exact, drops generic words and keeps at most one action word; the 24-character limit is unchanged. Short legacy plain-title output remains accepted. Malformed JSON, overlong output and null are rejected before truncation. Known outcome/answer patterns and unsupported echoes of the prompt examples make the model title unusable; the topic is then named from the start of the user message (see below). Title cleaning starts with one NFKC standardization, then removes combining Mn/Me marks, invisible/format/control characters and Hangul filler characters; normal decomposed Hangul composes before mark filtering. The standardized string supplies checks and the final cleaned 24-character API candidate, including fallback titles. Comparison removes paired outer decoration. The existing English contraction is checked before apostrophe removal, so ordinary ill/Ill words and filenames are not contractions. Answer/echo filters are model-output quality safeguards, not attack defenses: artificial Unicode combinations can evade this heuristic, and users can edit an odd title. No all-language semantic guarantee is added. Credential protection remains separately enforced on the final title. Known greetings/empty media captions do not call the model. Each received eligible message consumes one of three opportunities, including messages arriving while a generation is pending; there is at most one title task per topic. Reaching the limit prevents new generation; a valid result already in flight can still name the topic after a burst of later messages. A delayed creation service handler cannot replace a newer cached DM name, so name-based project roots retain the renamed topic. An observed user edit remains authoritative, including edits received during the rename API wait. These pattern guards do not prove semantic topic quality for every language/output.

Generation/auth has a 20s deadline; API application has a separate 10s limit. Subprocess cleanup can extend the generation task until exit is reaped. Errors/timeouts/unauthenticated providers use the cleaned received-message prefix, or a neutral topic ID when no safe prefix remains. A null or rejected model title (null, unparsable, too long, result-like, answer-like, greeting-like or an example echo) takes the same fallback and ends naming; it logs INFO `Topic auto-name: model title rejected reason=<code>; using message prefix` with the reason code only, never the title or the message. A credential-looking title or message is not used. Titles stay one line, up to 24 characters, with quotes, common emoji and controls removed.

Credential detection reuses `safe_preview` before truncation for user-message input and generated titles, and again after Unicode normalization and the final 24-character cut. The API boundary rechecks the generated candidate and the message-prefix fallback; an unsafe candidate is discarded, with a neutral topic ID if no safe fallback remains. This pattern check does not detect every arbitrary secret. The naming handler logs neither excerpts/titles nor exception payloads; existing provider diagnostic logging still applies. Each API error produces one metadata-only log, then permits fresh generation on the next user message within the same three-message budget; there is no immediate retry, backoff or retry_after wait.

Each bot persists only topic eligibility, received-message counts and consumed/attempted flags under its own `<agent_home>/dm_topic_naming.json`, using existing atomic JSON helpers. It saves the message count before scheduling and reserves an API attempt before application. Plain greetings and API exception/timeout share the same three-message limit; a null or rejected model title does not retry because the message prefix names the topic at once. Confirmed API failure restores eligibility only if the topic remains attempted, naming is enabled and shutdown has not started; exhaustion marks it seen. Observed user edits are retained, disabling stops retry and shutdown cancellation never retries. The next eligible user message generates a new title; success ends naming. The cache/project-root name changes only after API success. A timed-out Telegram call may have applied remotely before its response was lost; no exact-once remote guarantee is added. Unknown topics are consumed conservatively on first reception; a delayed creation event does not reopen them. Corrupt/unwritable state fails closed. Previously eligible but unconsumed/retryable topics are skipped after restart without resetting their saved count: an edit while the bot was offline cannot be ruled out from the old creation event. Disabling the feature before application or a user edit observed during generation suppresses the pending rename. A topic first handled while disabled is not backfilled when re-enabled. Bot shutdown blocks new title work before its first await, even if the namer has not been created yet. Namer stop also closes intake before collecting its owned tasks; concurrent stop calls do not recancel cleanup. Pending tasks are cancelled and awaited before return, including temporary-directory cleanup. Cancellation of stop itself waits for the same owned cleanup before propagating. External generation deadlines and shutdown cancellation force-kill the owned provider subprocess tree only while its returncode is unknown. Process exit/reaping is shielded from repeated owner cancellation; registry removal and temporary working-directory cleanup follow completed reaping, before the original cancellation propagates. Generation runs inside the naming task with an asyncio timeout, so stop awaits that cleanup even if it arrives during deadline cleanup. This wait has no separate fixed cleanup deadline. Gemini handles this in its non-streaming wrapper; Claude, Codex and Grok share the one-shot executor cleanup. Streaming execution is unchanged. Telegram has no conditional rename operation: an edit not yet delivered to the bot cannot be detected by this local guard.

## Group management (`/where`, `/leave`, audits)

`ChatTracker` stores activity in `~/.ductor/chat_activity.json`.

- join/reject/leave events are persisted for both groups and channels
- `/where` shows active/rejected/left tracked chats, including channels
- `/leave <group_id>` lets an authorized user force leave
- startup and periodic (24h) audits auto-leave groups no longer in `allowed_group_ids` and channels no longer in `allowed_channel_ids`
- auth hot-reload (`allowed_group_ids`) triggers immediate audit task
- `allowed_channel_ids` is supported by the bot runtime and audit path, but config hot-reload does not currently emit that field, so channel allowlist changes still need restart in practice

## Reply context (`build_reply_prompt`, `prepend_reply_to_media`)

When a user uses Telegram's native reply gesture, `_resolve_text()` prepends the
cited message to the prompt. Both helpers share `_cited_reply_text()`, which
prefers the user-selected quote fragment (Bot API 7.0+, `message.quote`) over the
full replied-to body (`reply_to_message.text` or `.caption`).

Text replies (`build_reply_prompt`) wrap both parts with explicit labels:

```text
The user is replying to this quoted message:
> <quoted text>

The user's message:
<user input>
```

Media replies (`prepend_reply_to_media`) — where the reply is a voice / photo /
video / ... attachment instead of text — prepend the quote plus an attachment-type
note ahead of the `[INCOMING FILE]` block, so the agent knows the voicemail/image
is the reply to the quote (and can transcribe it with that context):

```text
The user is replying to this quoted message:
> <quoted text>

Their reply is a voice message (the attached file below).

[INCOMING FILE]
...
```

Both return the body unchanged when the message is not a reply or the cited
message has no text (forum-topic service messages).

## Message dispatch (`message_dispatch.py`)

### Non-streaming

`run_non_streaming_message()`:

- typing context
- `orchestrator.handle_message()`
- `send_rich()`

### Streaming

`run_streaming_message()`:

- stream editor + `StreamCoalescer`
- optional `ReactionTracker` on the original Telegram message when `scene.status_reaction=true`
- forward text/tool/system callbacks
- finalize editor
- fallback rules:
  - stream fallback or empty stream -> `send_rich(full_text)`
  - otherwise send only extracted files from final text

Reaction behavior:

- `ReactionTracker` maps thinking/system/tool stages to Telegram-safe emoji and dedups consecutive identical states
- reactions are best-effort and are always cleared at the end of the turn
- when `scene.status_reaction=false` and `scene.seen_reaction=true`, the app falls back to the older one-shot seen reaction instead

## Callback routing

Special callback namespaces:

- `mq:*` queue cancel
- `ms:*` model selector, including `ms:e:*` reasoning-effort choices
- `crn:*` cron selector
- `nsc:*` session selector
- `tsc:*` task selector
- `ns:*` named-session follow-up callbacks
- `sf:*` / `sf!` file browser

After selector routing and chosen-button marking, a recognized DT Agent slash command is propagated as a typed message through the same command handlers, auth, bot-address/mention filters and quick/abort middleware. Unknown slash commands remain normal AI prompts. This includes `/restart`, `/status` and main/sub-specific command classification; it does not bypass command filters.

Selector callbacks use shared helpers in `messenger/telegram/callbacks.py` and selector response types from `orchestrator/selectors/models.py`.

## Observer and task integration

The bot no longer owns fragmented `deliver_*` handlers.

Current model:

- startup wires observer outputs to `MessageBus` via `orch.wire_observers_to_bus(...)`
- async inter-agent and task callbacks convert results through `bus/adapters.py`
- `MessageBus` handles lock/injection/delivery using shared `LockPool`
- inter-agent async results require successful injection into the receiving caller session before reporting in its original chat/topic; they do not fall back to other transports
- receipt notices use the receiving agent's own bot only when `interagent_notify_received=true` (default false); `--silent` still suppresses them

Webhook wake path:

- acquires lock for target chat
- runs orchestrator message flow
- submits final wake result envelope for delivery

Current limitation:

- Telegram startup wires the webhook wake handler
- Matrix startup currently does not, so webhook `wake` is effectively Telegram-only right now

## Startup lifecycle (`messenger/telegram/startup.py`)

Startup performs, in order:

1. orchestrator creation
2. topic cache seeding and resolver wiring
3. restart sentinel handling
4. observer-to-bus wiring
5. startup-kind detection and startup notification policy
6. recovery planner actions
7. command sync + restart marker watcher
8. group audit startup + periodic audit loop

Notification routing during startup is transport-owned:

- startup/restart notices use `notifications.startup_targets` when configured, otherwise they broadcast to allowed users

## File safety

Outbound file sends enforce `file_access` via `files.allowed_roots.resolve_allowed_roots(...)`.
`messenger/telegram/sender.py` uses shared MIME/tag helpers from `files/`.

# session/

Session lifecycle and persistence with provider isolation and topic/channel-aware keys.

## Files

- `session/key.py`: `SessionKey(transport, chat_id, topic_id)`
- `session/manager.py`: `ProviderSessionData`, `SessionData`, `SessionManager`
- `session/named.py`: `NamedSession`, `NamedSessionRegistry`

## Session identity (`SessionKey`)

`SessionKey` is transport-aware:

- Telegram messages with `is_topic_message` false -> `SessionKey("tg", chat_id, None)`, even if `message_thread_id` is present
- Telegram group/private-chat messages with `is_topic_message` true -> `SessionKey("tg", chat_id, message_thread_id)`
- Matrix rooms -> `SessionKey("mx", mapped_room_int, None)`
- API channel scope -> `SessionKey(chat_id=chat_id, topic_id=channel_id)` uses the default `"tg"` transport and may share Telegram state for equal numeric IDs; CLI origin metadata is separately `"api"`

Telegram uses `topic_id = message.message_thread_id if message.is_topic_message else None`; a thread ID alone does not select a topic session (`ductor_bot/messenger/telegram/topic.py:37`).

Persistence key (`storage_key`) format:

- legacy accepted on parse: `"<chat_id>"` or `"<chat_id>:<topic_id>"`
- current: `"<transport>:<chat_id>"` or `"<transport>:<chat_id>:<topic_id>"`

Parsing is backward-compatible (`SessionKey.parse`).

## `SessionData` model

Fields:

- `transport`, `chat_id`
- `topic_id` (optional)
- `topic_name` (optional cached display name)
- `provider`, `model`, `reasoning_effort` (active target for this session key)
- `created_at`, `last_active`
- `activity_at`, `activity_turns`: when and how many successful turns counted as user activity (user messages and commands, API, task and inter-agent results, heartbeat alerts); lint reports and cron `inject` / `in_session` turns update `last_active` and `message_count` but not these. Only completed turns update them: a timed-out or failed turn keeps its stored session id and message count but is no activity, whatever started it. A new session stores `null` / `0`; a stored record without the keys takes them from `last_active` / `message_count` once. The heartbeat uses them for target selection, cooldown and dormancy
- `provider_sessions: dict[str, ProviderSessionData]`

Provider bucket (`ProviderSessionData`):

- `session_id`
- `message_count`

Compatibility behavior:

- legacy flat session fields are migrated into provider buckets
- token and cost figures written by older versions (flat or in a bucket) are ignored when read and gone after the next save; the tracking was removed because it was wrong (it added the CLI's cumulative session cost on every turn and left cache tokens out), and an accurate rework was judged not worth it
- legacy storage keys are still accepted

## `SessionManager` API

- `resolve_session(key, provider=None, model=None, preserve_existing_target=False)`
- `get_active(key)`
- `list_active_for_chat(chat_id)`
- `list_all()`
- `reset_provider_session(key, provider, model)`
- `update_session(session, *, counts_as_activity=True)`
- `move_session(src_key, dst_key) -> bool`: moves the whole record (every provider bucket, target and activity fields; only transport/chat/topic change) to another key under the persistence lock. It does nothing and returns `False` when the destination already has a session id in any provider bucket (an empty shell is replaced) or the source does not exist. Named sessions and in-flight records are not moved. The memory lint report uses it when its topic was re-created, so the report session continues in the new topic. After a move the old key exists again only if something still writes through a session object of the old key (`update_session`, `preserve_session_identity`)
- `sync_session_target(session, provider=None, model=None, reasoning_effort=None)`
- `set_topic_name_resolver(resolver)`

## Freshness and rollover

Session freshness checks include:

- `max_session_messages`
- idle timeout (`idle_timeout_minutes`, `0` disables)
- daily reset boundary (`daily_reset_enabled`, `daily_reset_hour`, `user_timezone`)
- timestamp validity

Stale sessions are replaced on next `resolve_session(...)` call.

## Provider/model switching behavior

Switching model/provider for a key:

- updates active target for that key
- keeps other provider buckets intact
- `is_new=True` only if target provider bucket lacks `session_id`

This enables seamless return to previously used provider buckets.

Consumer-facing nuance:

- `/model` changes the active target for the current `SessionKey`
- `/new` does not wipe every bucket; the orchestrator resets only the bucket for the provider currently resolved from `config.model`
- temporary per-turn directives do not change what `/new` resets later unless the session target itself was updated

## Topic name integration

`SessionManager` can resolve and backfill topic names through a callback:

- `set_topic_name_resolver((chat_id, topic_id) -> str)`
- used by bot startup with `TopicNameCache`
- persisted `topic_name` improves `/status` and `/sessions` readability

Telegram implicit DM topic naming updates `TopicNameCache` after a successful API rename. Its fresh title call does not resume the conversation or update provider session state; only separate per-bot eligibility/attempt flags persist. See [automatic DM topic names](bot.md#automatic-dm-topic-names).

## Named sessions (`NamedSessionRegistry`)

Purpose:

- background `/session` registry
- deterministic inter-agent sessions (`ia.<slug>.t<topic-or-0>.x<hash8>` with source context; legacy `ia-<sender>` without it)

Model fields:

- `name`, `chat_id`, `topic_id`, `transport`, `provider`, `model`, `reasoning_effort`
- `session_id`, `prompt_preview`, `status`, `created_at`, `message_count`, `last_prompt`

Status values:

- `running`
- `idle`
- `ended`

Behavior:

- user-created cap: `MAX_SESSIONS_PER_CHAT = 10`
- persisted `running` entries are downgraded to `idle` on load
- recovered-running sessions are tracked for startup recovery
- ordinary inter-agent recipient turns and question fallbacks use `interagent_session_name(sender, source_chat_id, source_topic_id)`; source chat/topic changes produce distinct names
- synchronous questions to main during async delegation resume the source chat/topic’s current active `SessionManager` session under the shared lock; they are not a new Named Session unless the 60s wait expires or the source cannot be resumed. Verified sync-delegation questions to main are rejected before session creation; unknown-provenance questions retain the separate source-header session; see [question routing](multiagent.md#questions-during-delegation)
- the separate inter-agent cap is `MAX_INTERAGENT_SESSIONS_PER_CHAT = 32`
- stale CLI session IDs on those named sessions are retried once with a fresh session after update/cache-clear style failures

## Persistence

Paths below use the host default home; inside the default image the prefix is
`/ductor`, and a sub-agent uses `<main_home>/agents/<name>`.

- sessions: `~/.ductor/sessions.json`
- named sessions: `~/.ductor/named_sessions.json`

Storage is JSON + atomic write helpers (`atomic_json_save`).
I/O runs in worker threads (`asyncio.to_thread`).

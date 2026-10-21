# heartbeat/

Periodic proactive check loop for active sessions. Quiet hours default to disabled (`quiet_start=0`, `quiet_end=0`); equal hours mean no quiet window. Unset per-target quiet fields inherit these global values, while explicit windows and existing session/activity gates remain effective.

## Files

- `heartbeat/observer.py`: `HeartbeatObserver` loop lifecycle, target selection, gating and durable state
- `heartbeat/models.py`: `HeartbeatStatus` and typed `HeartbeatOutcome` (`ack`, `alert`, `skipped`, `failed`)

## Public API (`HeartbeatObserver`)

- `set_result_handler(handler)`
- `set_heartbeat_handler(handler)`
- `set_busy_check(check)`
- `set_stale_cleanup(cleanup)`
- `set_chat_validator(validator)`
- `set_state_path(path)` / `set_session_source(list_all, get_active)`
- `notify_activity(key)` / `reset_suspend_state()`
- `start()`
- `stop()`

Helper:

- `utils/quiet_hours.py::check_quiet_hour(...)` is the primary runtime helper used by the observer
- `utils/quiet_hours.py::is_quiet_hour(...)` remains the lower-level predicate

## Runtime flow

1. observer loop sleeps `interval_minutes`
2. reconcile durable state, clean stale CLI processes, and collect enabled manual targets plus eligible automatic targets:
   - with `auto_targets=false` (explicit opt-out), add private `allowed_user_ids` keys
   - with `auto_targets=true` (default), discover allowed Telegram session keys (including topics) whose last **user activity** (`activity_at`) is within the activity window; tracked/recovered keys remain eligible. A session that so far only had system turns (`activity_at` is null) is never selected, also not by the recovery scan
   - disabled manual entries opt their exact key out
3. skip target execution during global quiet hours (`user_timezone`)
4. apply route allowlist, dormant/quarantine/backoff, per-target quiet hours, topic-aware busy and in-flight gates
5. execute heartbeat handler and process its typed outcome; only alerts or quarantine notices are delivered

Manual targets with an explicit interval use independent loops and their own
quiet-hour settings; the global quiet-hour gate above applies to the global tick.

`set_stale_cleanup(...)` is called before tick processing and is wired to `ProcessRegistry.kill_stale(...)`.

## Orchestrator contract

Observer delegates heartbeats to `Orchestrator.handle_heartbeat(...)` (`heartbeat_flow`):

- read-only active session lookup
- requires existing resumable session
- preserve active session provider/model/reasoning effort; only an empty stored model falls back to the configured model
- the heartbeat request follows the same attachment rules as a normal turn: the configured persona/behavior file blocks (`append_system_prompt_files`) are attached, and MAINMEMORY with the agent roster too on a Claude session whose system prompt is refreshed every turn (`claude_system_prompt_snapshot: "off"`); other providers and the snapshot `"on"` mode keep the usual resumed-session policy
- cooldown enforcement via `session.activity_at` (a session without user activity is skipped as `no_user_activity`)
- ACK-token suppression; failed/skipped turns do not update the session
- session metrics update only for alert responses

`suspend_on_ack=true` (default true in the model and example configuration) makes a key dormant after an ACK. The
dormancy is judged on the session's user-activity count (`activity_turns`, state key `acked_activity`), not on the
provider or session ID, so a system turn that switches the provider or replaces the session ID does not wake it. The
physical marker (provider, session ID, message count) is still stored as `acked`; an older state without
`acked_activity` is converted once from it when the provider and session ID still match.
With automatic targets enabled by default,
each eligible session/group topic with user activity in the last 7 days is checked separately;
an ACK-only response lets it rest until the next conversation. Accepted user messages and commands (including `/new`,
`/new @topic`, `/reset` and `/model`) and task/inter-agent bus activity wake ACK/backoff state.

What is **not** user activity: the memory lint report turn and the cron `inject` / `in_session` turns. They are
recorded as session messages, but they neither select a topic as a heartbeat target, nor take part in the cooldown, nor
wake a dormant topic. The activity fields (`activity_at`, `activity_turns`) of a session move only on successful turns
that count as activity; a session record that has no such keys (older records) takes them from its last turn once.
The heartbeat's own alert turn, task and inter-agent results, API and webhook turns count as before. Only a turn that completed counts: a timed-out or failed turn (a user turn, a lint report, a task, inter-agent or cron injection) is still stored with its session id, cost and message count, but never moves the activity fields, so it neither wakes a dormant topic through the activity count nor makes a new system-only session a target. Invalid CLI sessions are quarantined until their session
ID changes; other failed outcomes receive bounded exponential backoff. State is
atomic JSON in `<agent_home>/heartbeat_state.json`; config fingerprints and the
initialization marker participate in startup reconciliation.

## Delivery model

Heartbeat results are wrapped as envelopes and delivered through `MessageBus` -> active transport adapters (`TelegramTransport` / `MatrixTransport`).

No direct `TelegramBot._on_heartbeat_result` callback path exists anymore.

## Per-target configuration (`group_targets`)

The observer supports explicit chat/topic targets via `heartbeat.group_targets`,
in addition to private-user keys or automatic session discovery. Each target
is a `HeartbeatTarget` with `enabled` (default true), `transport` (default `"tg"`),
`chat_id`, and optional `topic_id`. `auto_targets` defaults true and its
`auto_target_window_hours` defaults to 168. Headless agents do not start the
heartbeat observer.

### Per-target overrides

Each `HeartbeatTarget` can override global heartbeat settings. Unset fields fall back to the global `HeartbeatConfig` values:

- `prompt` -- custom heartbeat prompt for this target
- `ack_token` -- custom suppression token
- `interval_minutes` -- independent tick interval (checked separately from the global loop)
- `quiet_start` / `quiet_end` -- per-target quiet hours

Settings are resolved at tick time via `_resolve_target_settings(target)`.

### Target validation

Group targets are validated before each heartbeat tick using `set_chat_validator(validator)`. The validator checks whether the bot can still access the target chat (e.g. via Telegram `getChat` API).

Successful accessibility checks are cached for 1 hour (`_VALIDATION_TTL = 3600`).
Failed checks log a warning and skip the current attempt; failures are not cached,
so the next attempt can revalidate immediately.

User targets (`allowed_user_ids`) are not validated -- only `group_targets` entries go through this check.

### Config example

```json
{
  "heartbeat": {
    "enabled": true,
    "interval_minutes": 30,
    "auto_targets": true,
    "suspend_on_ack": true,
    "prompt": "Global heartbeat prompt...",
    "ack_token": "HEARTBEAT_OK",
    "quiet_start": 0,
    "quiet_end": 0,
    "group_targets": [
      {
        "chat_id": -1001234567890,
        "topic_id": 42,
        "prompt": "Custom prompt for this group topic",
        "interval_minutes": 60
      },
      {
        "chat_id": -1009876543210
      }
    ]
  }
}
```

The first target overrides `prompt` and `interval_minutes`; the second uses all global defaults.

# messenger/

Transport abstraction layer: protocols, transport behavior, registry, and
multi-transport adapter. The root protocols and registry are
transport-agnostic. Concrete transports live in `messenger/telegram/`,
`messenger/matrix/`, `messenger/slack/`, and `messenger/headless.py`.

For transport-specific details see [bot.md](bot.md) (Telegram) and
[matrix.md](matrix.md) (Matrix).

## Files

| File | Purpose |
|---|---|
| `messenger/__init__.py` | Public re-exports for protocols, command classification, send options, multi-transport helpers, and bot factory |
| `messenger/commands.py` | Shared direct/orchestrator/multi-agent command sets + `classify_command()` |
| `messenger/callback_router.py` | Shared callback-data dispatch helpers for selector/button routing |
| `messenger/protocol.py` | `BotProtocol` — runtime-checkable interface every transport implements |
| `messenger/registry.py` | `create_bot()` factory + `_TRANSPORT_FACTORIES` dispatch table |
| `messenger/notifications.py` | `NotificationService` protocol + `CompositeNotificationService` fan-out |
| `messenger/send_opts.py` | Base send-option model shared by transport senders |
| `messenger/multi.py` | `MultiBotAdapter` — multi-transport facade behind `BotProtocol` |

## BotProtocol

`BotProtocol` (`protocol.py`) is a `typing.Protocol` decorated with
`@runtime_checkable`. The supervisor, `AgentStack`, and
`InterAgentBus` depend **only** on this protocol, never on
transport-specific classes.

Required surface:

| Member | Kind | Description |
|---|---|---|
| `orchestrator` | property | Current `Orchestrator` (or `None` before startup) |
| `config` | property | `AgentConfig` |
| `notification_service` | property | `NotificationService` |
| `run()` | async | Start event loop, block until shutdown, return exit code |
| `shutdown(budget=None)` | async | Graceful teardown; every wait is capped by the shared shutdown budget |
| `request_stop()` | method | Ask `run()` to return on its own without waiting; `True` when supported, `False` when the bot can only be cancelled |
| `register_startup_hook(hook)` | method | Callback invoked after orchestrator creation |
| `set_abort_all_callback(cb)` | method | Multi-agent abort injection point |
| `on_async_interagent_result(result)` | async | Deliver async inter-agent result |
| `on_task_result(result)` | async | Deliver background task completion |
| `on_task_question(...)` | async | Deliver background task question |
| `file_roots(paths)` | method | Allowed root directories for file sends |

`TelegramBot`, `MatrixBot`, `SlackBot` and `HeadlessBot` implement this protocol.
The async inter-agent result callback returns a `DeliveryOutcome`.

## Stopping a bot

`request_stop()` is non-blocking. The supervisor calls it first when it stops a sub-agent and cancels the bot's task only if `run()` does not return in time (see `supervisor.md`, "Stopping a sub-agent").

| Bot | `request_stop()` |
|---|---|
| Telegram | Signals the dispatcher to end polling; also valid before polling started (`run()` then returns right after its startup). Returns `True`. |
| Slack | Cancels the socket task, which makes `run()` return. `False` when the socket task is not running. |
| Headless | Sets its stop event. Returns `True`. |
| Matrix | Not supported (returns `False`): the sync loop runs in the task that called `run()`, so that task is cancelled by the supervisor instead. |
| MultiBotAdapter | Asks every bot; `True` only if all of them support it. |

### Telegram polling ownership

`TelegramBot.run()` owns the polling task. aiogram's `start_polling` starts its polling task and a stop-signal waiter as child tasks and only waits for them, so cancelling `start_polling` leaves those children running: the old bot keeps calling `getUpdates` and collides with the next instance of the same bot. To prevent that, `TelegramBot` runs `start_polling` as its own task behind `asyncio.shield`, with a `Dispatcher` subclass (`telegram/polling.py`) that records the polling children. When `run()` is cancelled it stops polling and waits for it before the cancellation goes on:

1. Signal the dispatcher to stop and wait up to 5 s (capped by the shutdown budget), so aiogram ends polling through its own shutdown path.
2. If the task is still alive, cancel the recorded polling children, signal again and wait up to 2 s.
3. If it is still alive, cancel the polling task itself (this also ends the orphaned stop-signal waiter) and wait up to 2 s.
4. Cancel any polling child that appeared in the meantime.

`start_polling` runs with `close_bot_session=False`: the bot session stays open after polling ended, so replies that are still being sent can go out. `shutdown()` closes it as its last step.

`run()` is a no-op when stop was requested before it started. Cancelling during the startup calls before polling creates no polling task. The startup `delete_webhook` and `getUpdates(offset=-1)` calls are unchanged.

### Shutdown order (Telegram)

`TelegramBot.shutdown(budget)` is the one place that fixes the order; `/restart`, the restart marker, a termination signal, the supervisor's `stop_all` and the stop or restart of a single sub-agent all end there. One `ShutdownBudget` (25 s) is created when a stop starts and is shared by every step and by sequential sub-agent stops; each step waits at most `min(its limit, time left)` and a used-up budget moves straight to the next step.

1. Stop receiving: polling ends (see above). Normally it already ended when `run()` returned or was cancelled. From the first moment of the stop (a stop request, the restart command, the restart marker, a cancelled `run()`, `shutdown()` itself, or the orchestrator's shutdown mark) no more update is started: an update that waits for the per-session lock when the stop frees it, and one that arrives later, is left alone and stays pending in the journal (it is not counted as cancelled by the user), so the next start replays it once. The orchestrator also refuses a new message once its shutdown has begun. Turns that already run keep their in-flight record and their reply drain. The topic-name tasks are told to stop at the same time; their cleanup gets at most the time left (up to 5 s) after polling has stopped, and what is not done by then goes on in the background without holding the shutdown.
2. Wait for the handlers that are sending the reply of a finished turn: at most `min(8 s, left)`. A handler counts from the moment its turn ended until the reply (final stream edit, text and attachments) is sent. Running CLI turns and the handler of `/restart` itself are not waited for.
3. Run the orchestrator's pre-shutdown hooks (`register_pre_shutdown_hook`). The CLI processes are still alive here. No hook is registered yet; the call point is what matters, `Orchestrator.shutdown()` runs the hooks too if nobody did before, once.
4. `Orchestrator.shutdown()`: marks the turns as cut off, kills the CLI processes (SIGTERM grace and exit wait capped by the budget), stops the API server and the observers (capped).
5. Remaining update handlers get at most `min(2 s, left)` to end; handlers of the killed turns record the interruption through the existing in-flight path.
6. `delete_webhook` (capped), then the bot session is closed.

Matrix, Slack and the headless bot accept the budget and pass it to `Orchestrator.shutdown()` but keep their previous order (no reply wait). In a multi-transport agent the primary bot's shutdown runs first and kills the CLI processes of the shared orchestrator, so replies of the other transports are not waited for.

### Updates that arrive during a restart (Telegram)

Telegram keeps updates the bot has not confirmed. The bot used to throw them away at start (`delete_webhook(drop_pending_updates=True)` and a `getUpdates(offset=-1)` flush), and a message that was only waiting behind a running turn was lost when the process ended. Now both survive:

- **Journal.** Each agent keeps one file, `telegram_update_state.json`, in its home. `pending` holds updates that were received and not started (the update itself, its arrival time, chat and topic); `handled` holds the ids of updates that were started (kept 25 hours). An id is in one of them only; if a file shows an id in both, `handled` wins. The file is replaced atomically, so moving an id from `pending` to `handled` is one rename. A file that cannot be read (not valid UTF-8, or not valid JSON) is kept byte for byte as `telegram_update_state.json.corrupt-<time>`, the journal starts empty and one warning is logged.
- **Record before confirmation.** `PollingDispatcher` wraps the library's update reader. Every message update is saved to `pending` before it is handed on; the next `getUpdates`, which confirms the batch to the server, only goes out after the reader has been through the whole batch. An id that is `handled`, or already given to processing in this run, is not handed on again. A button press is saved as `handled` at once and never replayed (the button has expired).
- **Start of processing.** An update counts as started when it gets the per-session lock (`SequentialMiddleware`). `JournalMiddleware`, the outermost message middleware, then moves it to `handled`. Updates that never take the lock (rejected, unaddressed in a group, answered by an abort or quick command, failed) count as started when they end. `/restart` is moved before it runs, so it never runs twice. A waiting message that the user cancels (the queue indicator's cancel button, or a stop command that discards the queue) is moved when it is cancelled, so a restart does not bring it back. An update that is cut off while it waits because of a shutdown is not moved and is replayed.
- **Start order.** `run()` calls `delete_webhook(drop_pending_updates=False)` and no longer flushes with `offset=-1` (the orphaned poller that made the flush necessary is gone, see "Telegram polling ownership"). It registers `_replay_update_journal` as the last `startup` handler, after the bot's own startup (orchestrator, recovery) and the hooks the supervisor registered, and only once. The replay feeds the pending updates, oldest id first, through the same processing as polling (a task each, not awaited), with their original arrival time as the receipt; the message text is not changed, and the sent time of the message is shown as before. Replayed messages pass the allowlist and the other middleware again.
- **Too old.** At replay, a pending update received more than 24 hours ago is not replayed: it is moved to `handled` and each chat or topic with such updates gets one short notice (the count, no message text) through the bot's notification path, in the same topic. A failed notice is logged as a warning and not retried.
- **Same id again.** A server that hands out an update again after the restart (it was never confirmed) does not run it a second time: it is skipped when it was started, and when it is the one being replayed.

Only Telegram uses this journal, and only message updates are kept (other update kinds are processed as before and not replayed); Matrix and Slack are unchanged. When the journal file cannot be saved (disk full, read-only) nothing is lost: a received update is not handed on, a warning is logged and the save is tried again with a growing pause (up to 30 s) while the reader waits, so the server is not told the update was received before it is on disk; an update that cannot be marked as started is not run (it stays pending and is tried again at the next start). The journal in memory changes only after a save worked.

### Telegram secret input (`/secret`)

The main bot accepts `/secret` only from allowed users in a one-to-one private
chat. Sub-agent bots reject the command; groups and other users cannot store
secrets. Buttons recheck the same authorization before acting.
The command word and optional `@botname` suffix are case-insensitive; prefixes
such as `/secrets` are ordinary text.
Replies and input requests stay in the originating thread, including DM topics and group topics.

| Command | Behavior |
|---|---|
| `/secret` | Show usage. |
| `/secret set KEY` | Request a value; an existing key requires overwrite confirmation first. |
| `/secret list` | Show sorted key names only, never values. |
| `/secret delete KEY` | Ask for confirmation, then delete the key. |
| `/secret cancel` | Cancel the current input request. |

Input has two steps: send `/secret set KEY`, then send the value as a reply to
the bot's ForceReply request, whose first line is `🔐 secret-input`. Never put
the value in the command. Extra arguments after the key in `set` or `delete`
are refused and the command message is deleted if possible. A request lasts
120 seconds; a newer request replaces the same user's earlier request. While
waiting, any text message from that user, including text starting with `/`, is
consumed as input even without a reply, except an exact `/secret` command.
For 120 seconds after expiry, text without a reply that does not
start with `/` is deleted without saving, and the user is asked to start again.
This grace period does not survive restart. Pending requests resume after restart
until their original expiry. Replies to marked requests are always intercepted
and are refused once the request is missing or expired. Rejected values leave
the request open for the remaining time.

Keys match `^[A-Z][A-Z0-9_]{0,55}$` (at most 56 ASCII characters). Values must
be text of 1 through 4096 characters. Empty values, NUL, any line boundary
recognized by Python `str.splitlines()`, whitespace at either end, a single
or double quote at either end, and a leading `$` are rejected. Photos and
files are refused when sent as replies to the input request. Values are
written without quotes to the shared tool secrets store with mode 0600;
`DUCTOR_SECRETS_FILE` selects the store using the same precedence as the CLI
environment. If the main home's `.env` also contains the key, the saved
notice warns that CLI provider keys still use `.env`.

Secret commands and intercepted value messages are consumed before journal
admission and normal handlers: only their update ids are marked handled.
Their values do not enter the update journal, logs, orchestrator or CLI.
Secret-input logs contain key names, result codes, lengths and exception
type names only. Saved notices show the key and length, never the value.

Before trying to delete an intercepted message, the bot records its message id
in a guarded list. A separate task tries deletion up to three times with pauses of 1 and
2 seconds, without holding the input lock. If all attempts fail it asks the user to delete the message manually.
Replies or quotes whose reply target is guarded are intercepted, refused and
kept out of normal processing, and the new reply is also deleted. The bot retries deleting the original message
only if it has not already deleted it successfully. Successful deletion marks
the id as deleted but keeps it guarded for 48 hours from its initial recording,
protecting quotes already present in the same received batch.
The main home's `secret_input_state.json` (mode 0600) stores request metadata
and guarded message ids with timestamps and deletion status, never values.
Guarded ids and unexpired pending requests survive restart. Entries at least
48 hours old are removed when metadata is read or written. If saving a guarded
id or deletion status fails, it retries up to five times with pauses of 0.5, 1,
2 and 4 seconds, logs the failure and returns. Secret store operations run in
worker threads so file locks do not block the event loop.

Limits remain: values pass through Telegram's servers before deletion, and
users can forward or copy them. A value sent while the bot is offline can
reach ordinary processing if the request has expired and the user removed
the reply association. An unreadable or malformed state file starts an empty
state (the original is preserved as `.corrupt-<time>` when possible), losing
the previous guarded list; later quotes of those messages cannot be
intercepted through that list. Quote protection also ends after the guarded
id reaches its 48-hour retention limit.

### Stopping a topic (`/stop`)

Only commands stop work: `/stop`, `/stop_all` and `/interrupt` (also with an `@botname` suffix; `!interrupt` as well). A bare word such as "stop", "wait", "cancel" or "esc" is an ordinary message in Telegram. In Slack a bare `stop`, `stop_all` or `interrupt` is an ordinary message too (other bare commands such as `status` still work); Matrix needs the `!` or `/` prefix for every command.

`/stop` discards the messages that wait in the **same chat and topic** (a chat without topics: the messages without a topic), then kills the topic's running CLI process; the reply names the number of discarded messages when it is not zero (`stop.discarded`). The discard runs first, because the kill ends the running turn and frees the session lock, which would start the next waiting message at once. All targeted messages are cancelled and marked as dealt with in the update journal (so a restart does not bring them back) before any "discarded" indicator is edited: editing sends over the network, and a running turn that ends meanwhile frees the lock for a message that was not cancelled yet. The number in the reply counts the messages cancelled before they started. The reply states that the process ended and, only when messages were cancelled, adds a line with their number. `/stop_all` discards the waiting messages of the whole chat (also before the kill) and stops all agents. Matrix keeps its own queue, which `/stop` empties chat-wide before the kill; Slack has no separate queue, its `/stop` kills the running turn and the next waiting message starts as before.

### Telegram button choices while a conversation is busy

Ordinary AI-bound buttons share the message queue and its session lock. While the
lock is busy, the bot replies to the button's original message in the same topic
with `notice.queue_waiting` and the existing `mq:` cancel button. The queue preview
is the clicked label (up to 40 characters), not the original bot message. Taking
its turn deletes the indicator before execution. Cancelling the entry or discarding
its topic with `/stop` prevents execution; other topics keep their entries.

The same `(chat_id, bot message_id, callback data)` is reserved in memory before
marking the selected choice. A repeated click while waiting or executing is
answered without another mark, queue entry or execution. Completion, cancellation,
errors and shutdown release the reservation; a later click can run again. Distinct
buttons and messages remain separate choices. Special callbacks keep their routing.
A user-cancelled button keeps its entry and reservation until it acquires the lock, so repeated clicks are suppressed until that cleanup finishes, just as for queued messages.
Known slash-command buttons still forward through message ingress, including its
callback-ID dedup, quick-command bypass and abort handling.

A button blocked by shutdown never executes. Waiting tasks clean their entries and
indicators when they exit; as with queued messages, an indicator can remain if the
task is cancelled while its deletion request is still in flight. Callbacks remain handled on receipt and are never
replayed after restart; the user-message journal policy is unchanged. Callback dedup
state is not persisted.

## Transport behavior

The current package has no `MessengerCapabilities` model or capability presets.
Streaming, button, file and seen-indicator behavior is implemented by each
concrete transport. Telegram uses editable streams and inline keyboards; Matrix
uses segmented streaming and reaction-based selectors. `HeadlessBot` has no
messenger output or registered delivery adapter.

## Queued user message context

`messenger/queue_context.py` wraps the existing per-session FIFO lock without changing transport routing or cancellation policies. Telegram's middleware, Matrix text/media dispatch and Slack user-event dispatch use acquire_input_lock to freeze actual acquisition waits in the receipt. QueuedMessageLock also retains a known queue notice if the lock was busy at arrival. Every pending user acquisition is registered in the existing position counter, including a user behind an external bus FIFO waiter while locked() is false; acquisition/discard removes it idempotently. Position is the arrival position among user-message waits for that same lock, not a chat-wide/global sequence or a count of unrelated background lock users. Duration measures the actual acquisition and is frozen before later processing. Indicator/preparation time before acquisition is excluded; a known queue notice can remain with zero acquired wait if the lock was released during the indicator. No new lock or scheduling mechanism is added.

The send timestamp is Telegram message.date, Matrix server_timestamp (milliseconds) or Slack ts (seconds), formatted with config.user_timezone. A missing/invalid timestamp is shown as unknown, not guessed. A task-local context is limited to its receiving task, so newly spawned background work does not inherit this advisory. Its separate receipt does retain the acquired wait: user-started Named background requests receive Received plus Waited, without a queue block. Commands, command callbacks and cancelled Telegram entries are excluded. Ordinary Telegram AI-bound button choices use the same queue context, with an unknown send timestamp: the bot message date is not the click time. Matrix drain cancellation still prevents queued execution; Slack retains its existing lock-wait behavior without adding queue indicators or cancel controls.

Normal and foreground Named flows carry the advisory separately in AgentRequest.queue_context. The bot-received timestamp is added inside that same block, without an additional receipt line outside it. User text remains unchanged for command/directive routing, Named last_prompt, inflight prompt previews and screen output. CLIService uses AgentRequest.cli_prompt to prepend it only at send/send_streaming, including streaming fallback. Existing provider conversation transcripts naturally contain the actual CLI input; display previews are separate. format_cli_cmd removes the framework block from command-log previews without modifying executable argv. The agent may still mention context in its generated answer; the framework does not post the notice itself.

The fixed English prompt block is:

```text
[Queued user message]
[Received: 2026-10-05 09:15:02 KST]
Sent at: 2026-10-05 09:15:00 KST
Waited: 1m 15s
Queue position at arrival: 2
This message waited while the previous task was in progress. It may not be a reaction to the immediately preceding reply; determine which task it relates to.
[/Queued user message]
```

The queue block adds no setting, stored queue history or message-body logging. It covers user-message and ordinary Telegram button-choice lock waits; the separate receipt context below also covers selected bus inputs.


## AI input receipt context

Arrival is captured before Telegram authentication/serialization, Matrix text/media/reaction handling and Slack user-event handling await further processing. AI-bound button choices and user-started Named background turns keep the same arrival; forwarding a Telegram command callback through message middleware preserves its original receipt. Commands that never call AI and cancelled inputs produce no annotated AI request.

Unqueued input receives one line in config.user_timezone:

```text
[Received: 2026-10-05 21:30:12 KST]
original user text or media prompt
```

If an existing lock actually made a non-queue-block input wait, it receives:

```text
[Received: 2026-10-05 21:30:12 KST] [Waited: 75s]
original prompt
```

Arrival uses the bot/bus intake time, not message.date/server_timestamp/Slack ts or the eventual CLI delivery time. A Telegram user message or media whose `message.date` is at least 10 seconds earlier than the arrival also gets the send time, right after Received and before Waited, in the same timezone and format: `[Received: 2026-10-05 21:30:12 KST] [Sent: 2026-10-05 21:12:40 KST]`. The send time is taken from real user messages only: a callback's message is the bot's older message and is not used, and Matrix and Slack do not add it. The queue block already states `Sent at`, so its Received line never carries the bracket form (the send time appears once); command previews strip all receipt line shapes. The existing queue block keeps the transport send time separately as Sent at. Wait durations measure monotonic time around the actual lock acquisition and freeze on acquisition (or wait cancellation), including FIFO predecessors just after release. Later wake hooks, file preparation and execution do not add to that wait. Sub-millisecond acquisition overhead is suppressed; displayed receipt waits use whole seconds. The user queue uses the same acquisition helper, including external-bus FIFO predecessors while the lock appears unlocked. The queue block retains its minute/second display and user arrival position; its Received line is not duplicated by the receipt prefix. A Named background child preserves the finalized wait in its receipt while the parent-only queue block stays excluded. No new lock, scheduling policy or queue persistence is introduced.

infra/input_context.py carries receipt metadata separately through the existing execution tasks. AgentRequest.received_context and queue_context are composed only by cli_prompt, preserving user text, routing, Named last_prompt, inflight/session previews and screen output. The queue block takes precedence, so its received timestamp is never duplicated. Command previews strip both framework forms without changing actual argv. System prompts and their caches are unchanged. Actual provider transcripts naturally contain the CLI input, and the model can mention timing in its answer.

The same receipt format covers inter-agent requests, async responses/failures/result-lost reports, task results and public ask_parent handoffs, and allowed delegation questions. MessageBus uses Envelope.created_at before waiting for its lock; asynchronous request execution retains the existing AsyncInterAgentTask.timestamp, captured when the bus accepted the request. Async results carry their first received_at through the result object and durable result record, so callback retries and record-based recovery do not retime them. Legacy result records without this field use their validated updated_at as a stable approximation, not an invented original receipt. TaskHub questions retain first intake in last_question_received_at and snapshot it before scheduling public delivery; old question records without a timestamp freeze their first recovery receipt when delivered. Eligible running-task questions use private orchestrator injection instead of a public envelope; the reply goes to the task. Unanswered questions use the captured chat/topic/transport after the private lock is released, and a late private reply is never added to that handoff. See [task questions and limits](tasks.md#questions-during-a-running-task). The receiving agent's timezone formats injection metadata. Agent-tool replies have their own response-arrival metadata, formatted in the calling agent's current timezone, separately from text/error; legacy/network replies use a safe UTC receipt fallback. Both tools print the receipt before the unchanged answer or Error line, preserving failure exit codes without posting it to chat. For inter-agent delegation questions, the 60-second wait, source-session routing and Named fallback remain; verified synchronous-question rejection still happens before an AI call. Heartbeat, cron and webhook inputs are excluded, even inside an inherited receipt scope. API user-message ingress is unchanged.

## Transport Registry

`create_bot()` (`registry.py`) is the single entry point for bot
construction. It inspects `config.is_multi_transport`:

- **Single transport**: looks up the transport name in
  `_TRANSPORT_FACTORIES` and calls the matching factory.
- **Multi transport**: returns a `MultiBotAdapter` wrapping all
  configured transports.

`_TRANSPORT_FACTORIES` is a `dict[str, _Factory]` mapping transport
names to lazy-import factory functions:

```python
_TRANSPORT_FACTORIES: dict[str, _Factory] = {
    "telegram": _create_telegram,
    "matrix": _create_matrix,
    "slack": _create_slack,
    "headless": _create_headless,
}
```

Each factory accepts `(config, *, agent_name, bus, lock_pool)` and
returns a `BotProtocol`. Imports are deferred inside the factory body
so that unused transports do not need their dependencies installed.

Raises `ValueError` for unknown transport names.

## NotificationService

`NotificationService` (`notifications.py`) is a runtime-checkable
protocol with `transport_name` and three methods:

- `notify_origin(transport, chat_id, text, *, topic_id=None)` — send through the matching service to exactly the origin thread; report success/failure without fallback.
- `notify(chat_id, text)` — send to a specific chat/room.
- `notify_all(text)` — broadcast to all authorized users/rooms.

Telegram, Matrix and Slack notification services implement this protocol;
headless uses a no-output implementation. The supervisor and bus use it without
knowing which transport is active.

Transport-specific startup routing sits above this protocol:

- Telegram routes startup notices through `notifications.startup_targets` when that list contains enabled targets
- Matrix routes startup/restart notices through `notify_startup()` and `notifications.startup_targets`

### CompositeNotificationService

`CompositeNotificationService` fans out calls to multiple underlying
services. It holds a `list[NotificationService]` and iterates
sequentially on both `notify()` and `notify_all()`. `notify_origin()` selects
only the matching `transport_name`; a missing service returns false.

Used by `MultiBotAdapter` to aggregate all transports' notification
services.

## Multi-Transport Mode

`MultiBotAdapter` (`multi.py`) wraps multiple transport bots behind
a single `BotProtocol` facade. It is returned by `create_bot()` when
`config.is_multi_transport` is true.

### Construction

1. Creates a shared `LockPool` and `MessageBus`.
2. Iterates `config.transports` and calls `_create_single_bot()` for
   each, injecting the shared bus and lock pool.
3. First bot becomes the **primary**; the rest are **secondaries**.
4. Builds a `CompositeNotificationService` from all bots.

### Startup sequence (`run()`)

1. Registers a startup hook on the primary that sets an
   `asyncio.Event`.
2. Launches the primary bot as an `asyncio.Task`.
3. Waits for the orchestrator-ready event.
4. Injects the primary's orchestrator into all secondary bots.
5. Launches secondary bots as tasks.
6. `asyncio.wait(FIRST_COMPLETED)` — when any bot finishes, the
   rest are cancelled.
7. Returns the exit code from the first completed bot (e.g. `42`
   for restart).

When `run()` itself is cancelled (also while it waits for the primary's
startup), every bot task started so far is cancelled and awaited before the
cancellation goes on, so each bot can stop its own polling.

### Delegation rules

| Method | Delegation |
|---|---|
| `orchestrator` | primary |
| `config` | own `_config` |
| `notification_service` | `CompositeNotificationService` |
| `register_startup_hook` | primary |
| `set_abort_all_callback` | all bots |
| `on_async_interagent_result` | matching origin transport only; originless cron broadcast uses primary |
| `on_task_result` | all bots |
| `on_task_question` | matching available `tg`/`mx`/`sl` bot; otherwise all bots |
| `file_roots` | primary |
| `shutdown` | all bots |
| `request_stop` | all bots; `True` only if every bot supports it |

### Shared resources

All bots in a `MultiBotAdapter` share:

- `MessageBus` — single instance for cross-transport envelope delivery
- `LockPool` — single instance for per-chat locking
- `Orchestrator` — created by the primary, injected into secondaries

## Adding a New Transport

1. **Create the sub-package** `messenger/<name>/` with at least a bot
   module implementing `BotProtocol`.

2. **Implement transport behavior** (streaming, selectors, files and
   notifications) directly in the new transport, following its platform APIs.

3. **Add a factory** in `registry.py`:

   ```python
   def _create_discord(
       config: AgentConfig,
       *,
       agent_name: str,
       bus: MessageBus | None,
       lock_pool: LockPool | None,
   ) -> BotProtocol:
       from ductor_bot.messenger.discord.bot import DiscordBot
       return DiscordBot(config, agent_name=agent_name,
                         bus=bus, lock_pool=lock_pool)

   _TRANSPORT_FACTORIES["discord"] = _create_discord
   ```

4. **Implement `NotificationService`** for the transport so
   `CompositeNotificationService` can include it.

5. **Add a `MessageBus` transport adapter** (`transport.py`) that maps
   `Envelope` objects to the transport's native send API.

6. **Guard the dependency** behind an optional extra in
   `pyproject.toml` and use deferred imports in the factory so the
   package is not required unless the transport is selected.

7. **Add config fields** to `AgentConfig` for the new transport
   (credentials, allowed users/rooms, etc.).

8. **Write tests** — mock the transport client and verify the bot
   satisfies `isinstance(bot, BotProtocol)`.

## Allowlist diagnostics and provider login

Actual allowlist denials in Telegram, Matrix and Slack emit INFO `Allowlist rejection chat_type=... chat_id=... user_id=... topic_id=...`, containing metadata only, never message content (the Telegram bot's own messages and service messages are dropped without this log once the bot id is known). The common `RejectionLog` helper limits each retained chat/user pair to one entry per 600 seconds (topics share that pair’s limit). Each logger instance retains up to 1,024 pairs and evicts the oldest when full; an evicted pair can log again. This does not change each transport’s authorization/mention policy or its handling of non-message events.

Foreground user conversations recheck the **resolved session provider** authentication immediately before CLI execution. Text, media and prompt-button turns, including named-session foreground follow-ups, return localized host-repository `./scripts/login.sh <provider>` instructions and do not run the CLI when unauthenticated. Authentication changes are reread on the next user turn; Gemini can use its configured API key. Telegram, Matrix, Slack and direct API use these shared orchestrator flows. Inter-agent, cron and webhook task execution paths are unchanged by this foreground check.

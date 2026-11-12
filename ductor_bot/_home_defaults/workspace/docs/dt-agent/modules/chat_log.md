# Conversation history and recovery queue

`chat_log.enabled` enables the main Telegram agent's conversation history,
Telegram link monitor and durable delivery queue. It defaults to `false` and
requires a bot restart after changes. It operates independently of the web UI.
See [installation](../installation.md#conversation-history-and-recovery-queue-optional)
and [configuration](../config.md#chatlogconfig).

## Runtime and storage

`ChatLogRuntime.start(bot)` starts once after the orchestrator is available and
stops before orchestrator shutdown. `recorder.py` records user input, completed
responses and public bus events. `history.py` appends JSONL entries under
`$DUCTOR_HOME/chat_log/history/`, with stable entry IDs and per-topic sequence
numbers. Topics use the same Telegram session identity as normal chat.
Deleting a topic removes its history; the empty companion lock file remains
to preserve safe concurrent access.

`outbox.py` persists pending items and completed/failed delivery state under
`$DUCTOR_HOME/chat_log/outbox/`. `link.py` probes Telegram every 30 seconds,
keeps a 30-minute status strip and drains pending deliveries in creation order.
A transient failure pauses draining; permanent failures move to failed state.
After five unsuccessful attempts while the link probe succeeds, an item moves
to failed state so later items can proceed. Restart resumes saved chunk progress.
The web queue view includes pending items and failures from the last seven days,
with public `origin`, `title` and a preview of up to 80 characters.

## Behavior when enabled

Failed bus deliveries during a Telegram outage are retained: task result notices,
task questions and replies, heartbeat alerts, cron, webhook and background results.
New deliveries join an existing pending queue so recovery preserves order.
Web messages and replies use the same queue for their Telegram mirrors.
Public task questions are recorded from their question text; other bus events
use public result content; a task result without injection records status/title
without the raw worker result. Heartbeat recording follows the alert path.
Inter-agent results can be recorded, but keep their existing inter-agent retry
path rather than this outbox. Normal Telegram foreground replies and other direct
sends are not made durable by enabling recording. Telegram-keyed direct API turns
can also be recorded with source `api`; Matrix/Slack turns are excluded.

Buffered delivery uses `send_rich_progress_detailed`, tracking completed chunks.
Long messages can split at different positions from the default `send_rich` path.
After recovery, transmission continues at the saved chunk boundary. If Telegram
accepted a chunk but its response was interrupted, that one chunk can be sent twice.
Local storage errors pause delivery until the next retry without consuming Telegram's
failure budget; acknowledged chunks remain in memory until their progress is saved.
A temporary queue read error pauses delivery to preserve order and counts as pending
when deciding whether new deliveries must queue. Web queries skip unreadable files.
Invalid queue files are moved to `outbox/corrupt/` and excluded from pending counts;
if quarantine fails, the confirmed invalid item is logged and skipped.
Once an enqueue atomically replaces its item file, later sync, cleanup or notification
errors are logged and the saved item is returned successfully.

With recording enabled and web disabled, initial startup still follows the normal
Telegram connection sequence. If Telegram is already unreachable at startup,
the orchestrator and recording runtime start when that sequence succeeds.

## Operator commands

Run inside the bot container:

```bash
ductor chat-log status
ductor chat-log import --dry-run
ductor chat-log import
ductor chat-log import --undo
```

`status` reports activation, validation errors, history file/entry counts and
pending/failed queue counts, unreadable queue files and quarantined files (exit status 0). Import reads Claude and Codex
transcripts, strips receipt/hook annotations, maps sessions to topics and assigns
negative sequence numbers before live history. A repeated import adds only new
entries. Each import orders its entries by timestamp across Claude and Codex
and matches live duplicates one-to-one within the same topic and role. User bodies
must match after normalization; bot live bodies may start with the transcript body,
after removing the fixed named-session tag. Timestamped pairs within ten minutes
are matched from smallest time difference first, breaking ties by transcript order,
then live sequence order. Undated transcript entries are matched last against
remaining live entries in sequence order.
Recording gaps can still be imported. Import references are kept in
a separate state file that retention does not remove, so pruned imports stay removed.
When history retention is enabled, entries older than the cutoff are not imported;
undated entries inherit the preceding timestamp in the same transcript for this check.
Entries with no preceding timestamp remain eligible.
Entries found
by a later import keep their own order after earlier imports; existing sequence
numbers are not reassigned. Unmapped entries appear in the read-only
**Unknown topic** group.
`--undo` removes imported history and clears saved import references so transcripts
can be imported again, while retaining live entries; combine it with
`--dry-run` to preview the removal count. Actual import/undo requires active recording.

## Retention and manual cleanup

Automatic history and web edit backup cleanup defaults to off. Set
`chat_log.retention_days` and/or `chat_log.backup_retention_days` to positive
integers and restart to enable it while recording is active. Each setting is
independent; `0` or `null` disables that category. Cleanup runs at startup and
then every 24 hours. Completed and quarantined outbox items retain automatic
seven-day cleanup, regardless of these settings. Completed items use `updated`;
quarantined files use their modification time. Unreadable files are logged and
skipped without stopping other cleanup or delivery.

History cleanup removes individual lines strictly older than the cutoff,
using their timezone-aware timestamps. Lines with missing or invalid timestamps
remain. Empty topic files are deleted; companion `.lock` files and atomic `.seq`
sequence checkpoints remain so subsequent live entries keep increasing. Deleting
a topic also deletes its sequence checkpoint. A topic cleanup failure is counted
and logged while cleanup of other topics and backups continues.
Cleanup shares history writers' locks and replaces retained content atomically.
Pending queue items, including partial transmission progress, are never removed.
Backups use the creation time encoded by `YYYY-MM-DD/<name>.HHMMSS[.N]` in KST,
so an old source modification time cannot expire a fresh backup. Unrecognized
backup paths are preserved with one warning per pass. Empty date folders are
removed after actual cleanup.

Preview manual cleanup before running it:

```bash
ductor chat-log prune --older-than 30 --dry-run
ductor chat-log prune --older-than 30
ductor chat-log prune --older-than 30 --backups --dry-run
ductor chat-log prune --older-than 30 --backups
```

`N` must be a positive integer. Manual cleanup includes history lines and
completed outbox items (`sent`, `dropped`, `failed`) and quarantined files;
`--backups` also includes
web file edit backups. It works even when recording or automatic cleanup is off.
The preview reports eligible history entries/files, completed queue items,
backups and bytes without changing files or creating locks. Actual cleanup
reports the same counts for removed content, plus topic cleanup failures. A preview is a snapshot; ongoing
recording or queue transitions can change the eventual totals.

## Disabling and recovery

Check pending counts before disabling. Set `webchat.enabled=false` first if
necessary, then `chat_log.enabled=false`, and restart the bot. Existing history
and queue data remain on disk. Re-enabling resumes pending deliveries.
Back up the home before manually discarding queue files or history.

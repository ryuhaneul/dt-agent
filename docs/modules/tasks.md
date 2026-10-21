# tasks/

Delegated background task system (`TaskHub`) for long-running autonomous work.

## Files

- `tasks/hub.py`: task lifecycle (submit/run/resume/question/cancel/shutdown)
- `tasks/registry.py`: persistent registry + task-folder seeding + cleanup/delete
- `tasks/models.py`: `TaskSubmit`, `TaskEntry`, `TaskInFlight`, `TaskResult`
- `orchestrator/selectors/task_selector.py`: `/tasks` UI callbacks (`tsc:*`)
- `_home_defaults/workspace/tools/task_tools/*.py`: CLI tools (`create`, `resume`, `ask_parent`, `list`, `cancel`, `delete`)

## Purpose

Run long work asynchronously. A private task-question turn temporarily occupies its parent topic; other topics use their own session locks.

High-level flow:

1. create (`/tasks/create`)
2. execute (`TaskHub._run`)
3. optional question (`/tasks/ask_parent`)
4. optional resume (`/tasks/resume`)
5. result delivery + parent-session injection
6. optional permanent deletion (`/tasks/delete`)

A resume received while the previous run is delivering its result starts after that run ends; a resume during actual execution is rejected.

Task done/failed result injection uses the result envelope's arrival time before
MessageBus lock waiting. For public question handoffs, TaskHub snapshots ask_parent intake before scheduling delivery
and persists last_question_received_at; the question adapter carries that same timestamp,
including later delivery of the saved question. Legacy task files remain readable;
missing/invalid question timestamps mean unknown (0), with first recovery receipt frozen
and saved on delivery. This adds metadata, not a new automatic question-replay mechanism.
Only AI input receives the receipt line and actual acquisition wait, frozen before later
preparation; task prompt/question text previews and chat delivery remain unchanged.
Cancelled/timeout notifications that do not call AI receive no AI annotation.
See [receipt context](messenger.md#ai-input-receipt-context).

## Task instruction receipts

TaskHub snapshots submit/resume intake before validation and folder work. Initial
instructions, follow-ups and parent answers returned through resume receive the same
`[Received: …]` line in the executing agent's `AgentConfig.user_timezone`.
AgentSupervisor supplies getters rather than string snapshots: the main fallback reads
the main config when TaskHub is constructed, and each agent registers a getter over its
live `AgentConfig` with its CLI service. A hot-reloaded `user_timezone` therefore applies
to the next task create/resume/parent answer for that agent, main or sub, without
recreating the stack. Receipt formatting does not infer timezone from CLIService private
configuration. TaskHub also accepts a plain zone string; missing/non-string or invalid
timezone settings use the existing UTC fallback.
Actual scheduling delay from spawn to run entry adds `[Waited: …]` on that line;
sub-millisecond overhead is omitted, and later prompt/file preparation is excluded.
The existing max_parallel limit rejects excess background submissions immediately;
this change adds no concurrency queue or retry scheduler.

AgentRequest.received_context is combined only by cli_prompt. Original instructions,
prompt_preview, original_prompt, seeded task files, registry text, system prompts,
logs and chat displays retain their original text. Numeric instruction_received_at
and optional instruction_waited_seconds are persisted separately in TaskEntry.
Replaying the same saved instruction keeps its receipt and already measured wait;
a newly accepted resume is a distinct instruction with a fresh intake and reset wait.
Registry loading still downgrades stale running tasks to failed and does not replay
work automatically. Legacy/malformed receipt fields are unknown; first recovery
receipt is frozen and saved on demand instead of treating task creation as the
unknown follow-up intake. The formatting helper's existing default behavior is
unchanged. Cron, webhook, heartbeat and automatic memory/cleanup inputs are outside
this change.

## Persistence and folders

The shared registry is `<main_home>/tasks.json` (default host home
`~/.ductor`). Main task folders are `<main_home>/workspace/tasks/<task_id>/`.
Sub-agent task folders use `<main_home>/agents/<name>/workspace/tasks/<task_id>/`;
`TaskEntry.tasks_dir` records that per-agent location.

Task folder seeds include:

- `TASKMEMORY.md`
- `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`

Startup/maintenance behavior:

- stale `running` entries -> downgraded to `failed`
- orphan entries/folders are cleaned
- finished-task history is pruned by `finished_retention_hours` and `finished_keep_last` (below)
- maintenance runs on its first pass after startup and then every 5 hours

Finished-history retention: age and count are independent maximum limits. A finished
entry older than `finished_retention_hours` is removed even when it is among the newest
`finished_keep_last`, so fewer than `finished_keep_last` may remain; entries beyond the
newest `finished_keep_last` are removed even when young. A nonpositive value disables
that limit. Pruning deletes the registry entry and its task folder; running and waiting
tasks are never pruned.

Task entries and task folders (including `TASKMEMORY.md`) are working storage, not an archive:
results that must be kept belong outside the task folder (for example `output_to_user/`, the
project folder or `memory_system/`). In `/tasks`, "Delete Finished" (`tsc:cleanup`) shows a
confirmation first; only `tsc:cleanup_yes` removes the entries and task folders of the chat's
finished tasks, and `tsc:r` cancels.

## Config (`AgentConfig.tasks`)

- `enabled`
- `max_parallel` (per chat)
- `timeout_seconds`
- `finished_retention_hours` (default `168`)
- `finished_keep_last` (default `100`)

## Execution model (`TaskHub`)

`submit(TaskSubmit)`:

- resolves chat ID from `parent_agent` mapping when missing
- creates registry entry and folder
- appends mandatory task rules suffix
- normalizes `priority` to `interactive|background|batch`
- enforces `max_parallel` only for non-`interactive` tasks
- spawns async execution

`_run(...)`:

- builds `AgentRequest` with `process_label=task:<task_id>`
- applies provider/model overrides when supplied
- persists resolved provider/model on first run
- updates status:
  - `done`
  - `waiting` (an unanswered question was handed off)
  - `failed`
  - `cancelled`

Error exits -9/-15/137/143 are classified as `cancelled`, even without a recorded user cancel. Stale-process cleanup (`AgentResponse.stale_killed`) is instead `failed` with the error `stale process killed after <n>s`. A failed result is delivered to the parent session like any other failure; a cancelled one is not injected. Only a recorded, accepted cancel suppresses an unanswered-question handoff, as described below.

Tasks cut off by a stop or restart:

- `TaskHub.begin_shutdown(agent)` is registered as a pre-shutdown hook of every wired agent (it runs before the stop kills that agent's CLI processes); `TaskHub.shutdown()` marks all agents. A new `set_result_handler` call (the agent was started again) clears the mark of that agent.
- While the mark is set, a task of that agent that the stop cuts off is stored as `failed` with the error `interrupted by restart` and `notify_pending=true`, and nothing is delivered: both when the CLI process was killed (exit 143/137) and when the asyncio task is cancelled by `shutdown()`. A task whose finished result is already being delivered is left as it is, and a user cancel (mark not set) stays `cancelled`.
- A task found `running` when the registry loads (an unclean stop) becomes `failed` ("Bot restarted while task was running") and is `notify_pending` too.
- `notify_pending` and `notify_attempts` are optional fields of the registry entry; older files without them load as not pending. Automatic retention (`cleanup_finished_retention`) never removes a pending task; an explicit delete does. Resuming a task clears both fields.

Notice after the next start:

- When an agent starts, the supervisor wires its result handler first and then starts one background task for that agent (`TaskHub.report_interrupted`) that goes through its `failed` + `notify_pending` tasks, oldest first. The start does not wait for it (the notice may wait for the parent session); stopping the agent cancels it, and the tasks are tried again at the next start.
- Each notice is a failed task-result envelope that is submitted to the agent's message bus directly, with the transport set to the agent's **primary** transport (`BotProtocol.notify_task_interrupted`: Telegram `tg`, Matrix `mx`, Slack `sl`; a multi-transport agent uses its primary bot's transport; a headless agent cannot be told). The bus injects the usual "task failed" turn into the parent session (task name, "interrupted by restart") and delivers the answer to the task's chat and topic (`thread_id`). A resume hint is added only when the task has a session and a provider.
- The notice counts as told only when the original envelope was delivered (`DeliveryOutcome.delivered`). A bus fallback to another transport is a failure. A told task gets `notify_pending=false`; a failed try adds one to `notify_attempts` and keeps the task pending for the next start; the third failed try clears the flag with a warning.
- The normal task-result callbacks are unchanged. Known limit: a multi-transport agent is told through its primary transport, not through the transport the task was started from.

## Questions during a running task

Run `python3 tools/task_tools/ask_parent.py "your question"` with a command timeout
of at least **300 seconds** (300000 ms). Wait up to about five minutes:
the server budget is 270 seconds from TaskHub intake, including the task's
question-lock wait, the parent session-lock wait (at most 60 seconds or the
remaining budget), and private execution. The tool's HTTP timeout is 285 seconds.
Eligible private-answer attempts for the same task are serialized; waiting does not reset a question's
intake deadline.

For an eligible question, TaskHub calls the parent orchestrator's
`answer_task_question` in the matching active conversation under the shared
session lock. Main and sub-agent parents use the same path. This private turn
does not use MessageBus or streaming callbacks to post a reply to chat.
The parent answers only; when it needs the user's decision or approval, or
information it does not have, it replies with a line containing only `NEEDS_USER`.

The task follows the tool output:

- `Answer from parent:`: continue in the same running task with that answer.
  A successfully answered task can finish as `done`; asking alone does not make it `waiting`.
- `Question forwarded...`, command failure, or timeout: finish what is possible,
  include the unresolved question in the final result, and end the run.
  The parent answers with `resume_task.py` after the run has finished.
- Record every question and any answer in `TASKMEMORY.md` and the final result.

An ineligible source, unavailable or busy parent, session mismatch, error,
deadline, empty reply or `NEEDS_USER` uses the public handoff. A connection lost
before private execution prevents that execution. If a private answer arrives
after the task's connection is gone, the public handoff contains only the original
question; the late private answer is never attached. Question-result logs contain
metadata, not the question or answer text.

### Unanswered-question handoff

TaskHub captures the original execution, full question and destination before
waiting. It schedules at most one public handoff for that question, after the
private answerer has returned and released the parent session lock.

| Original execution at handoff | State and delivery |
|---|---|
| Still running, before final status | Mark that execution's question pending; a successful finish becomes `waiting`; hand off to the captured destination. |
| Finalized as `done`, including while final result delivery is pending | Persist `waiting`; later final delivery does not restore `done`; hand off once. |
| Finalized as `waiting` | Keep the state and hand off once. |
| Finalized as `failed` | Keep `failed` (already resumable) and hand off once. |
| Finalized as `cancelled`, with an accepted explicit cancel | Keep the state and skip the handoff. |
| Finalized as `cancelled`, without an accepted explicit cancel | Keep the state and hand off once. |
| A newer run has started | Leave its state, pending flag and destination untouched; hand off to the old destination with `asked during an earlier run; the task has since been resumed`. |
| The registry entry was deleted or pruned | Do not recreate it and do not hand off; log `handoff=skipped_deleted`. |

The public question has a task-name/id header and instructions to resume after
that run finishes. It can arrive before the task has finished: a running task
cannot be resumed. A normal reply to the injected public question goes to the
user chat; only the follow-up sent through `resume_task.py` reaches the task.
The question's receipt follows the usual public-delivery input rules.

### Limits

- Immediate answers require `origin_session_kind == "foreground"`, a nonempty
  origin transport and chat, and a nonempty origin provider-session ID matching the
  currently active session. Delivery routing alone does not prove conversation identity.
- A task from a new conversation's first turn can be bound when that successful
  attempt first establishes its session ID. Tasks from an earlier attempt or turn,
  an errored first turn, or a restart that left them unbound do not borrow another
  active conversation. `/new` or a switch to a different provider-session ID
  causes handoff; returning to the matching original ID permits a private attempt.
- Named, background, inter-agent, cron and nested-task origins, and legacy or
  unconfirmed provenance, use public handoff.
- While a private answer holds the parent topic's shared lock, user messages
  for that topic wait.
- The answer-only instruction is a **prompt rule**, not a reduction of execution
  permissions. The private reply is returned to the task, not posted as a chat reply
  by the question handler. An undelivered late reply stays in the parent conversation.

## Resume behavior

- allowed from `done|failed|cancelled|waiting`
- requires stored `session_id` and provider
- keeps same `task_id` and folder
- rejects resume while the task's existing execution is still running, even if its
  persisted state already appears resumable

Cancellation behavior:

- `TaskHub.cancel()` and `cancel_all()` first run the registered subprocess termination ladder via `ProcessRegistry.kill_for_task(task_id)` (SIGTERM, grace, SIGKILL, then bounded reaping), then cancel and await the asyncio task, which avoids hanging streaming pipes
- reaping waits at most 5s per process and logs a warning on timeout before returning, so this ordering does not guarantee that every process has exited in every failure case

## Topic-aware routing

Tasks preserve topic context:

- `TaskEntry.chat_id` / `TaskEntry.thread_id` store the current routing target (origin topic/thread at creation)
- `create_task.py` forwards `DUCTOR_CHAT_ID` and `DUCTOR_TOPIC_ID` to `/tasks/create`
- create and resume also send `DUCTOR_TRANSPORT`, `DUCTOR_SESSION_KIND` and
  `DUCTOR_SESSION_ID` as `transport`, `source_session_kind` and `source_session_id`.
  The registry stores `origin_transport`, `origin_session_kind` and
  `origin_session_id`. Missing/empty values remain empty; a nonempty unrecognized
  kind becomes `unknown`. Transport aliases `telegram` and `matrix` become `tg`
  and `mx`; other values, including `api`, are kept.
- `resume_task.py` forwards the same variables to `/tasks/resume`. When a chat is supplied, `TaskHub.resume()` moves the entry's `chat_id`/`thread_id` to the requesting chat/topic (a chat without topic means no topic) in the same persisted update that marks the task running, so that run's result, its `ask_parent` questions, the parent's answers and sub-tasks it spawns go there and the routing survives a restart. Resuming from the original topic is unchanged, a request without chat information keeps the stored routing, and a rejected resume changes nothing. Registries written before this field existed load with `thread_id=None`
- every accepted resume replaces all three origin fields with the new request's
  values; absent values clear old provenance even when stored chat routing is kept
- result/public-question envelopes map `thread_id -> topic_id`; an unanswered question
  captured before a resume keeps its earlier destination
- multi-transport question callbacks select one available `tg`, `mx` or `sl` bot;
  empty, unknown or unavailable selections retain the existing fanout behavior
- parent-session injection resumes the correct topic session

## InternalAgentAPI endpoints

- `POST /tasks/create`
- `POST /tasks/resume`
- `POST /tasks/ask_parent`
- `GET /tasks/list`
- `POST /tasks/cancel`
- `POST /tasks/delete`

Behavior details:

- no task hub attached -> `503` for mutating endpoints (`/tasks/list` returns empty list)
- `/tasks/list?from=<agent>` filters by task owner
- ownership checks for `/tasks/resume`, `/tasks/cancel`, `/tasks/delete` when `from` is provided
- `/tasks/delete` only deletes finished tasks (`done|failed|cancelled`)
- `/tasks/create` accepts `priority`; unknown values coerce to `background`
- `/tasks/ask_parent` keeps the JSON `success` / `answer` contract: `answer` is
  `Answer from parent:\n<reply>` or the existing `Question forwarded...` message.
  It checks whether the request transport is still connected; an already closed
  connection skips private execution. Waiting is asynchronous and does not block
  other HTTP requests.
- `/tasks/create` rejects headless senders even when a shared hub is available

## Registry deletion semantics

`TaskRegistry.delete(task_id)`:

- returns `False` if task is missing or not in a finished state
- removes both registry entry and task folder
- resolves folder path before entry removal (prevents per-agent folder resolution bug)

Bulk cleanup path:

- `cleanup_finished(chat_id=None)` removes all finished tasks

## Telegram UX (`/tasks`)

`/tasks` is quick-command routed and renders selector UI:

- sections: Running, Waiting for answer, Finished
- callbacks (`tsc:*`): refresh, cancel one, cancel all, delete finished
- if disabled: `Task system is not enabled.`

## Tool scripts

From task agent context:

- `create_task.py`
- `resume_task.py`
- `ask_parent.py`
- `list_tasks.py`
- `cancel_task.py`
- `delete_task.py`

`create_task.py` supports `--priority interactive|background|batch`.

`delete_task.py TASK_ID` performs permanent removal of one finished task via `/tasks/delete`.

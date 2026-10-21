# multiagent/supervisor.py

In-process multi-agent supervisor (`AgentSupervisor`) for main agent + optional sub-agents.

## File

- `ductor_bot/multiagent/supervisor.py`

## Purpose

`run_bot()` always starts `AgentSupervisor`.

- main agent always runs under supervision
- sub-agents are loaded from `~/.ductor/agents.json`
- crash/restart policy is handled per agent task inside one asyncio process

## Startup lifecycle

`AgentSupervisor.start()`:

1. start `InterAgentBus`
2. start `InternalAgentAPI` on `config.interagent_port` (default `8799`): `127.0.0.1:<port>` inside the bot container
3. if `tasks.enabled=true`: create shared `TaskHub` (`~/.ductor/tasks.json` + `~/.ductor/workspace/tasks/`) and attach it to `InternalAgentAPI`
4. create/start main `AgentStack`
5. wait for main startup readiness (`_main_ready`) before sub-agent startup; the timeout is 120 seconds
6. load + start sub-agents from `agents.json`
7. start `SharedKnowledgeSync` (`SHAREDMEMORY.md` -> agent memories)
8. start `agents.json` watcher and the 300-second parked-result/redelivery scan
9. wait for main agent completion and return its exit code; a readiness timeout logs a warning and still starts sub-agents

## Supervision policy

Each agent runs in `_supervised_run(...)` with health tracking.

- normal exit: task ends
- exit code `42`:
  - sub-agent: in-process restart (stack rebuild)
  - main agent: propagate restart to process/service runtime
- main fatal crash: supervisor exits with failure; transient connection/timeout or Telegram network/server errors retry indefinitely, with backoff capped at 60 seconds
- sub-agent crash: exponential backoff restarts (max 5 retries), then leave health as `crashed` until manual restart

## Dynamic agent registry

`FileWatcher` polls `agents.json` (5s).

- added entry: start sub-agent
- removed entry: stop sub-agent
- restart triggers for running sub-agents:
  - `transport` changed
  - `transports` changed
  - Telegram identity changed (`telegram_token`)
  - Matrix identity changed (`homeserver`, `user_id`, `password`, `access_token`, `device_id`, or `store_path`)
- other config field changes in `agents.json` currently do not trigger auto-restart

### Lifecycle lock

Every sub-agent start, stop, restart and rebuild runs under one lock (`_agents_lock`): the `agents.json` watcher, `/agent_start`, `/agent_stop`, `/agent_restart`, the initial start, a sub-agent's own restart request or crash rebuild, and `stop_all()`. Operating tradeoff: sub-agent starts and stops are serialized globally, so a slow start delays the others.

- A name that is already running is not started again (`already running`, no change of the agent's state); nothing starts after `stop_all()` began.
- `stop_all()` takes the lock once and reads the running names under it, so a restart that is in progress finishes first and its new stack is stopped too.
- A sub-agent's own task takes the lock for its restart/rebuild. Waiting for the lock is cancellable: a stop or restart that gets the lock first cancels that task instead of waiting for it, so the old stack is not run again. A lock holder never waits for its own task.
- A failing stack shutdown is logged as a warning (exception type only) and does not stop the stop.
- A new agent task starts with an empty log context; it does not inherit the context of the command or watcher that started it.
- Real double polling of one bot token is not covered by the tests (it needs a real Telegram bot); the polling task of a stopped bot is covered against a loopback Bot API (see `messenger.md`, "Stopping a bot").

### Stopping a sub-agent

`_stop_agent_locked(name, budget=None)` ends the agent's supervised task in two steps, each wait capped by a shutdown budget (`infra/shutdown_budget.py`: a monotonic deadline, 25 s by default, `remaining()` and `cap(n) = min(n, remaining)`):

1. The name goes into `_stopping_names` and the bot is asked to stop with `request_stop()`. A bot that supports it (the call returns exactly `True`) gets `min(5 s, budget)` to return from `run()` on its own. Bots that cannot do this (Matrix, or any stack whose `request_stop()` does not return `True`) skip the wait and are cancelled at once.
2. A task that is still running is cancelled and awaited for at most `min(10 s, budget)`, but at least one second even when the budget is used up. A cancelled Telegram bot stops its own polling before the cancellation reaches the supervisor (see `messenger.md`). A task that does not end in time is logged and left behind; the stop goes on.

While the name is in `_stopping_names`, `_supervised_run` returns when `run()` returns (also with the restart code 42) or fails, without taking the lifecycle lock: the stopper holds the lock while it waits for that task, so a rebuild there would deadlock and would start the agent again.

`stop_all()` creates one budget when it starts and uses it for everything: the replay scan, the agents watcher, shared knowledge, replay cancellation, the wait for the lifecycle lock (if the lock is still held when the time is up, the agents are stopped without it), every sub-agent stop, the main agent's task (cancelled and awaited for `min(10 s, time left)`, at least one second), the stack shutdowns, the task hub (cancellation and result delivery) and the internal API. Several sub-agents therefore share one deadline instead of each getting a full wait. A sub-agent's own restart (hot reload) and a single stop create their own budget when they start. The budget goes on into `AgentStack.shutdown(budget)` and `BotProtocol.shutdown(budget)`, where the bot's shutdown order (see `messenger.md`, "Shutdown order") uses it; the request-handler drain after the stack shutdown is capped by it as well.

## Orchestrator hook injection

During bot startup, supervisor injects hooks into each agent dispatcher.

- sets `orch._supervisor`
- on main agent: registers multi-agent commands (`/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`)
- on main agent: wires `/stop_all` / `!stop_all` callback (`BotProtocol.set_abort_all_callback(...)`) to `AgentSupervisor.abort_all_agents()`
- when task hub is active: wires `TaskHub` into each orchestrator (per-agent CLI service, result/question callbacks, primary chat ID mapping)

## Cross-Agent Abort

`abort_all_agents()` is the supervisor callback behind the main bot's `/stop_all` command.

- iterates all agent stacks and kills active CLI processes
- cancels chat-scoped background tasks on each stack
- cancels in-flight async inter-agent tasks on the shared bus
- cancels in-flight shared task-hub tasks across all agent chat IDs
- returns aggregated kill/cancel count to the calling bot handler

## Shutdown

`stop_all()` order:

1. stop the redelivery scan, watcher/shared-knowledge sync, and pending replay tasks
2. cancel in-flight async inter-agent tasks (durable records remain available for recovery)
3. stop sub-agents
4. stop main agent
5. shutdown task hub (if enabled)
6. stop internal API

This file gives coding agents a current map of the repository.

## Project Overview

It runs in a Docker Compose bot container, can expose Telegram, Matrix, or an optional direct WebSocket API, and keeps state under `/ductor` (the mounted host `DUCTOR_HOME`).

Stack:

- Python 3.11+
- aiogram 3.x (Telegram)
- matrix-nio (Matrix, optional extra)
- aiohttp (webhook server, internal API, optional direct API)
- Pydantic 2.x
- asyncio

## Development and deployment

Docker Engine and Compose v2 are required for the runtime. From a clone:

```bash
./scripts/onboard.sh
docker compose up -d --build
docker compose logs -f ductor-bot
```

For local code checks, use a virtual environment for `pytest` and
`compileall`; run the bot with Compose. Onboarding prompts only when both
stdin and stdout are TTYs and `--no-prompt` is absent. A first noninteractive
install requires a Telegram token file and allowed user IDs; missing required
inputs exit 2 before writes. Reruns may reuse saved credentials and user IDs.

## Runtime Flow

```text
Telegram:
  Update -> AuthMiddleware -> SequentialMiddleware -> TelegramBot
  -> Orchestrator -> CLIService -> provider subprocess -> Telegram delivery

Matrix:
  sync event -> MatrixBot auth/room checks -> Orchestrator
  -> CLIService -> provider subprocess -> Matrix delivery

API (optional):
  /ws auth (token + e2e_pk + optional chat_id/channel_id)
  -> encrypted frames -> Orchestrator streaming -> encrypted result events
```

Background and async delivery:

```text
Observer / TaskHub / InterAgentBus callback
  -> bus.adapters -> Envelope -> MessageBus
  -> optional shared lock + optional session injection
  -> registered transport adapters (TelegramTransport / MatrixTransport)
```

## Module Map

| Module | Purpose |
|---|---|
| `cli_commands/` | CLI startup, agents, status, and help commands |
| `messenger/` | transport protocol, capabilities, notifications, registry, multi-transport adapter |
| `messenger/telegram/` | Telegram transport: middleware, handlers, startup, callback routing, file/media UX |
| `messenger/matrix/` | Matrix transport: sync loop, auth, segment streaming, reaction buttons, media |
| `orchestrator/` | command routing, directives/hooks, flows, provider/session/task wiring, lifecycle split |
| `bus/` | unified `Envelope`, `MessageBus`, shared `LockPool`, delivery adapters |
| `cli/` | provider wrappers, stream parsing, auth detection, model caches, process registry |
| `session/` | `SessionKey(transport, chat_id, topic_id)`, provider-isolated session buckets, named sessions |
| `tasks/` | delegated background task runtime (`TaskHub`) and persistent registry |
| `background/` | named background session execution for `/session` |
| `multiagent/` | supervisor, inter-agent bus, internal localhost API bridge, shared knowledge sync |
| `api/` | optional direct WebSocket API and authenticated file endpoints |
| `cron/`, `webhook/`, `heartbeat/`, `cleanup/` | in-process automation observers |
| `workspace/` | `/ductor` path model, seeding, rule deployment/sync, skill sync |
| `infra/` | PID lock, restart/recovery state, and shared runtime helpers |
| `files/` | shared file/path safety, MIME detection, image processing (`image_processor.py`: resize/convert incoming images) |
| `security/`, `text/` | prompt safety, formatting helpers |

## Key Runtime Patterns

- `DuctorPaths` in `workspace/paths.py` is the single source of truth for runtime paths.
- Session identity is `SessionKey(transport, chat_id, topic_id)` across Telegram chats/topics, Matrix rooms (mapped int), and API channel isolation.
- `/new` resets the configured default-provider bucket for the active session key.
- `/reset` resets the currently active provider bucket for the active session key.
- `MessageBus` is the single async delivery path for observers, task callbacks, webhook wake results, and async inter-agent responses. Delivery is transport-aware: UNICAST envelopes route to the matching transport only, with cascading fallback when the target transport is unavailable.
- User ingress, `MessageBus` injection and `ApiServer` share the orchestrator `LockPool`. `ApiServer` receives that pool at startup and locks by the session key.
- Workspace init is zone-based:
  - Zone 2 overwrite: `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, framework-managed tool scripts
  - Zone 3 seed-once: user-owned files
- Rule sync is mtime-based for sibling `CLAUDE.md` / `AGENTS.md` / `GEMINI.md`; cron task folders additionally get missing rule backfill.
- Skill sync links workspace skills with provider CLI skill directories inside the bot container.
- `ductor agents add` is a Telegram-focused scaffold; Matrix sub-agents are supported through `agents.json` or the bundled agent tool scripts.

## Background Systems

All run as in-process asyncio tasks:

- `BackgroundObserver` (named sessions)
- `CronObserver` (results route UNICAST when job has `chat_id`, BROADCAST otherwise)
- `WebhookObserver`
- `HeartbeatObserver` (supports `group_targets` with per-target overrides)
- `CleanupObserver`
- `CodexCacheObserver`
- `GeminiCacheObserver`
- config reloader
- rule sync watcher
- skill sync watcher

## CLI Surface

`ductor` starts a configured bot. `ductor status`, `ductor help`,
`ductor --help`, `ductor --version`, and `ductor agents` are available.
Use `docker compose` to start, stop, and rebuild the runtime container.
See [installation](docs/installation.md).

## Key Data Files (`/ductor`)

- `config/config.json`
- `.env`
- `secrets.env`
- `sessions.json`
- `named_sessions.json`
- `tasks.json`
- `cron_jobs.json`
- `webhooks.json`
- `agents.json`
- `startup_state.json`
- `inflight_turns.json`
- `chat_activity.json`
- `SHAREDMEMORY.md`
- `logs/agent.log`
- `workspace/`

## Conventions

- `asyncio_mode = "auto"` in tests
- line length 100
- mypy strict mode (`mypy ductor_bot/` must report no findings)
- ruff strict lint profile (reported by CI, not a gate)
- CI gates on mypy, the full pytest run, `python -m ductor_bot.i18n.check`, `python scripts/build_agent_docs.py --check`, `scripts/tests` (Python 3.9 and 3.11) and `bash -n` of the tracked shell scripts
- config deep-merge adds new defaults without dropping user keys
- supervisor restart code is `42`

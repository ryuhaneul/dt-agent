# DT Agent reference for agents

Framework-managed reference for how DT Agent works, its settings, commands and tools. It is replaced on every update: do not edit it, and keep your own notes in `memory_system/`.

**Fastest way to the right page:** find your goal in the table below, open the file, jump to the section. Otherwise search: `python3 tools/memory_tools/memory_search.py --docs "your question"`, then read only the pages it points to. Every setting is in [config-reference.md](config-reference.md); every command in [commands-reference.md](commands-reference.md).

## Where to look

| I want to ... | File | Section |
|---|---|---|
| know what a config key does, its type, default, whether it needs a restart | [config-reference.md](config-reference.md) | one row per key (nested keys included) |
| understand a config block in depth | [config.md](config.md) | `AgentConfig` and one section per nested block |
| change settings safely (file location, merge, hot reload) | [config.md](config.md) | [Config Creation](config.md#config-creation), [Load & Merge Behavior](config.md#load--merge-behavior) |
| know which settings apply live and which need `/restart` | [config-reference.md](config-reference.md) | "Hot reload" column; [modules/config_reload.md](modules/config_reload.md) |
| use API keys and environment variables | [config.md](config.md) | [External API Secrets and Environment](config.md#external-api-secrets-and-environment) |
| pick models, providers, reasoning effort | [config.md](config.md) | [Model Resolution](config.md#model-resolution) |
| run a chat command (`/new`, `/model`, `/status`, ...) | [commands-reference.md](commands-reference.md) | Chat commands |
| use the `ductor` CLI or the host scripts (login, update, rebuild) | [commands-reference.md](commands-reference.md) | ductor CLI, Host scripts |
| find the right tool script (cron, webhook, tasks, agents, memory, media, browser) | [commands-reference.md](commands-reference.md) | Workspace tools |
| schedule recurring work (cron) | [automation.md](automation.md) | [Cron jobs](automation.md#cron-jobs); [modules/cron.md](modules/cron.md) |
| receive HTTP triggers (webhooks) | [automation.md](automation.md) | [Webhooks](automation.md#webhooks); [modules/webhook.md](modules/webhook.md) |
| set up heartbeat check-ins | [automation.md](automation.md) | [Heartbeat](automation.md#heartbeat); [modules/heartbeat.md](modules/heartbeat.md) |
| delegate long work to background tasks | [modules/tasks.md](modules/tasks.md) | [Execution model](modules/tasks.md#execution-model-taskhub); [automation.md](automation.md) |
| run work in named background sessions | [automation.md](automation.md) | [Named sessions](automation.md#named-sessions-session) |
| create or use sub-agents, delegate to them | [modules/multiagent.md](modules/multiagent.md) | [Inter-agent communication](modules/multiagent.md#inter-agent-communication); `agents.json` in [config.md](config.md) |
| manage memory (MAINMEMORY, topic pages, lint, persona) | [modules/workspace.md](modules/workspace.md) | memory wiki tools, `PERSONA.md`/`BEHAVIOR.md`; [config.md](config.md) `MemoryLintConfig` |
| understand sessions, resets, topics per chat | [modules/session.md](modules/session.md) | [Session identity](modules/session.md#session-identity-sessionkey); [architecture.md](architecture.md) |
| work with Telegram topics, groups, buttons | [modules/bot.md](modules/bot.md) | [Topic support](modules/bot.md#topic-support); `telegram_dm_topic_auto_name` |
| set up or troubleshoot Matrix / Slack | [matrix-setup.md](matrix-setup.md), [modules/matrix.md](modules/matrix.md) | `MatrixConfig`, `SlackConfig` in [config.md](config.md) |
| handle received files, media, cleanup | [modules/files.md](modules/files.md), [modules/cleanup.md](modules/cleanup.md) | `CleanupConfig` in [config.md](config.md) |
| tune streaming and message appearance | [config.md](config.md) | `StreamingConfig`, `SceneConfig` |
| sync skills between providers | [modules/skill_system.md](modules/skill_system.md) | [Configuration](modules/skill_system.md#configuration) |
| understand the whole architecture | [architecture.md](architecture.md), [system_overview.md](system_overview.md) | [Runtime Overview](architecture.md#runtime-overview) |
| see how a module works (orchestrator, bus, supervisor, cli, api) | [modules/orchestrator.md](modules/orchestrator.md), [modules/bus.md](modules/bus.md), [modules/supervisor.md](modules/supervisor.md), [modules/cli.md](modules/cli.md), [modules/api.md](modules/api.md) | Files, then the topic |
| check logging, diagnostics, security rules | [modules/logging.md](modules/logging.md), [modules/security.md](modules/security.md) | `/diagnose` in [commands-reference.md](commands-reference.md) |
| install or troubleshoot the deployment (operator) | [installation.md](installation.md) | [Troubleshooting](installation.md#troubleshooting) |

## Notes

- Paths such as `~/.ductor` mean the host home; inside the container the main agent's home is `/ductor` and sub-agents live under `/ductor/agents/<name>`.
- These pages describe the framework. Facts about the user and their projects belong in `memory_system/`, not here.

# Agent Tools

Tools for inter-agent communication and shared knowledge.
Agent creation and removal are only available on the main agent.

## How users interact with sub-agents

Each sub-agent has its own workspace and memory. Choose its interface to match the task:

- **Bot-backed (Telegram or Matrix)**: its own chat supports direct conversations
  with the user, plus synchronous or asynchronous delegation from main.
- **Delegation-only (`headless`)**: no bot token or messaging account, no chat,
  and no direct user conversations. Main uses it through synchronous or
  asynchronous delegation. Async results return through main to the originating
  chat/topic. Choose this for workers the user only needs through main.

After creating a bot-backed sub-agent, explain how to open its chat. After
creating a headless sub-agent, explain that the user asks main to delegate to it;
never suggest opening a headless agent's chat. Internal tool commands are for
YOU to run, not for the user.

## Available Tools (internal, not user-facing)

| Tool | Purpose | Availability |
|------|---------|-------------|
| `ask_agent.py` | Ask a sub-agent a question (sync, blocks until response) | All agents |
| `ask_agent_async.py` | Give a sub-agent a task (async, response comes back to YOU) | All agents |
| `edit_shared_knowledge.py` | View or edit SHAREDMEMORY.md (synced to all agents) | All agents |
| `create_agent.py` | Create a new sub-agent (writes to `agents.json`, auto-detected) | Main only |
| `remove_agent.py` | Remove a sub-agent from the registry | Main only |
| `list_agents.py` | List all sub-agents and their configuration | Main only |

## How agent-to-agent communication works

**The response ALWAYS comes back to the calling agent.** There is no way
to make a sub-agent reply directly in its own chat via these tools.

    You (main) → send task to sub-agent → sub-agent processes → response returns to YOU

**Never tell the user** that a sub-agent will "answer in its own chat" or
"respond directly to the user" — that is not how these tools work.

## Creating Sub-Agents

**Before creating a sub-agent, ask the user** what it should be like: its role, how it should
address the user, its tone/style and language, and any essential rules it must always follow.
Pass the answers with `--persona` / `--persona-file` (PERSONA.md: name, role, address form,
tone, language) and, only when needed, `--behavior` / `--behavior-file` (BEHAVIOR.md: essential
always-on principles, as few as possible — never task-specific or delegation-only rules).
A bot-backed sub-agent created without `--persona` starts from the blank template and asks the
user about itself in its first chat. A **headless** agent has no chat and nobody to ask, so
main must collect this from the user *before* creating it and pass it at creation.
Existing PERSONA.md / BEHAVIOR.md files are never overwritten.

When creating a sub-agent:

1. Choose a descriptive lowercase name (no spaces, e.g. `finanzius`, `researcher`)
2. Choose the transport: **Telegram**, **Matrix**, or delegation-only **headless**
3. Use **specific model names**, not provider names:
   - Claude: `opus`, `sonnet`, `haiku`, `fable`
   - Codex: `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex-spark` (check `config/codex_models.json`)
   - Gemini: `gemini-2.5-pro`, `gemini-2.5-flash` (check `config/gemini_models.json`)
4. Provider is `claude`, `openai`/`codex`, `gemini`, or `grok`
5. The workspace is created automatically under `agents/<name>/`
6. The sub-agent starts automatically within seconds (FileWatcher)

**IMPORTANT:** Never use `codex`, `gemini`, or `grok` as model names — those are providers.
The `--model` must be a specific model ID from the lists above.

### Delegation-Only Agent (`headless`)

No bot token, messaging account, or allowlist is required. Provider and model
flags are optional; supply them to choose a worker's execution target.

```bash
python3 tools/agent_tools/create_agent.py --name worker --transport headless
python3 tools/agent_tools/create_agent.py --name reviewer --transport headless \
  --provider claude --model sonnet
```

Main sends work using `ask_agent.py` (sync) or `ask_agent_async.py` (async).
The result comes back to main; asynchronous results retain the original chat
and topic. The worker has its own workspace/memory, but the user cannot chat
with it directly. Use Telegram/Matrix instead when a separate direct chat is wanted.

### Telegram Agent

Requires a bot token from @BotFather and Telegram user IDs (integers).

```bash
python3 tools/agent_tools/create_agent.py \
  --name "agent-name" \
  --token "BOT_TOKEN" \
  --users "USER_ID1,USER_ID2" \
  --provider claude \
  --model opus \
  --description "Short agent description for the join notification"
```

### Matrix Agent

Requires a Matrix account (user ID, homeserver URL). Password is optional
at creation time — it can be added to `agents.json` later before starting.

```bash
python3 tools/agent_tools/create_agent.py \
  --name "matrix-agent" \
  --transport matrix \
  --homeserver "https://matrix.example.com" \
  --user-id "@bot:matrix.example.com" \
  --allowed-users "@alice:example.com,@bob:example.com" \
  --provider claude \
  --model opus \
  --description "Short agent description"
```

**Matrix-specific flags:**

| Flag | Required | Description |
|------|----------|-------------|
| `--transport matrix` | Yes* | Selects Matrix transport (*auto-detected if `--homeserver` present) |
| `--homeserver` | Yes | Homeserver URL (must be HTTPS) |
| `--user-id` | Yes | Bot's Matrix user ID (`@localpart:domain`) |
| `--password` | No | Account password for initial login (token saved after first login) |
| `--allowed-users` | No | Comma-separated Matrix user IDs to allow (empty = all users) |
| `--allowed-rooms` | No | Comma-separated room IDs (`!id:srv`) or aliases (`#name:srv`). Empty = all rooms |

**Note:** `--token`/`--users` (Telegram) and `--homeserver`/`--user-id`/`--password`
(Matrix) are mutually exclusive. The script rejects conflicting flags.

**Credentials flow:** The bot needs either `password` or `access_token` in its
config to log in. On first login with a password, the bot saves an `access_token`
to `matrix_store/credentials.json`. After that, the password is no longer needed.

### Persona options

| Flag | Description |
|------|-------------|
| `--persona TEXT` / `--persona-file PATH` | Content for the agent's `PERSONA.md` (identity, address form, tone, language) |
| `--behavior TEXT` / `--behavior-file PATH` | Content for the agent's `BEHAVIOR.md` (essential always-on principles only) |

```bash
python3 tools/agent_tools/create_agent.py --name reviewer --transport headless \
  --persona "You are Reviewer, a strict code reviewer. Address the user as 'boss'. Terse; answer in English."
```

### Join Notification (`--description`)

**Provide `--description` for bot-backed sub-agents.** A headless agent has no
chat where a join notification can be sent or pinned. This text is
saved as `workspace/JOIN_NOTIFICATION.md` and **automatically sent + pinned**
when the user first opens the bot chat or the bot joins a room.

Write a compact message (max ~500 chars) that tells the user:
1. What this agent does (one sentence)
2. Key commands: `/new` (fresh context), `/stop` (cancel), `/model` (switch), `/help`
3. How to start

Example:
```
--description "I'm your code review assistant. Send me code or a repo link and I'll review it.

**Commands**
- /new — Fresh conversation
- /stop — Cancel running task
- /model — Switch AI model
- /help — All commands

Send any message to begin."
```

To update an existing agent's notification, edit
`~/.ductor/agents/<name>/workspace/JOIN_NOTIFICATION.md` directly.

## Matrix Bot Account Setup

A Matrix agent needs a dedicated Matrix account. There are several ways
to create one, depending on the homeserver setup.

### Option 1: Public or Managed Homeserver

Register a new account through the homeserver's web interface or via
Element/another Matrix client. Use a descriptive username like
`ductor-bot` or `my-assistant`.

- Go to the homeserver's registration page or use Element → "Create Account"
- Choose a username and password
- Note down the full user ID: `@username:homeserver.domain`
- Note down the homeserver URL: `https://homeserver.domain`

### Option 2: Self-Hosted Synapse

Use the built-in registration tool or the Admin API.

**CLI registration:**
```bash
register_new_matrix_user -u botname -p password \
  -c /path/to/homeserver.yaml http://localhost:8008
```

### After Account Creation

1. You have: **homeserver URL**, **user ID** (`@bot:server`), **password**
2. Create the agent with `create_agent.py --transport matrix ...`
3. On first start, the bot logs in with the password and saves an access token
4. The password is not used again after the initial login

## Inter-Agent Communication

Each agent has its own memory, workspace, and session — they are independent.
Two modes are available:

### Synchronous (blocking)

Use `ask_agent.py` for quick lookups or simple questions. Your CLI turn
blocks until the sub-agent responds. The response is returned to you
directly as tool output.

```bash
python3 tools/agent_tools/ask_agent.py "agent-name" "Quick question"
```

When receiving a synchronous delegation from main, do not send questions to
main during the work: main is waiting for your final response. Put questions
in that final report. The server rejects questions to main during synchronous
delegation. Continue the work; do not invent missing values, and leave them
empty or state assumptions.

### Asynchronous (long-running tasks)

Use `ask_agent_async.py` for tasks that take longer. Returns immediately
with a task_id. The sub-agent's response is delivered back to **your**
chat (the calling agent's chat) when ready.

```bash
python3 tools/agent_tools/ask_agent_async.py "agent-name" "Complex request that takes time"
```

Use async for code generation, analysis, research, or anything that may
take more than a few seconds. Use sync for quick lookups.

Async receipt notices default to off (`interagent_notify_received: false`); the receiving agent can enable them in main config or its `agents.json` override, and `--silent` suppresses them without affecting delegation even when already off.

During an asynchronous delegation, ask main synchronously with `ask_agent.py`
when clarification is necessary. The intended answering context is the session
that delegated the task; a dedicated fallback includes its source information.
Do not use `ask_agent_async.py` during a delegated turn; it is rejected.

### Starting a fresh session (`--new`)

By default, follow-up messages to the same agent resume the existing
inter-agent session (context is preserved). To start a completely new
task with no prior context, use the `--new` flag:

```bash
python3 tools/agent_tools/ask_agent_async.py --new "agent-name" "Brand new task"
python3 tools/agent_tools/ask_agent.py --new "agent-name" "Brand new question"
```

### Parallel requests to the same agent (`--lane`)

By default all your requests to one agent share one inter-agent session and
run one after another. To run independent pieces of work in parallel, give
each its own lane (1-12 characters of `a-z`, `0-9`, `-`, not starting with `-`):

```bash
python3 tools/agent_tools/ask_agent_async.py --lane api "agent-name" "Implement the API part"
python3 tools/agent_tools/ask_agent_async.py --lane ui "agent-name" "Implement the UI part"
python3 tools/agent_tools/ask_agent.py --lane review "agent-name" "Review the diff"
```

- The same lane resumes the same session; a different lane is a separate session. A request without `--lane` uses the normal session (a parent turn's lane is not inherited).
- Lanes share the recipient's workspace, memory files and provider login. Give each lane its own part of the files (no two lanes editing the same file), tell it to work in its own git worktree, and keep one owner for the final merge and full test run.
- If the recipient is busy the request waits its turn (FIFO per session, a limit on parallel sessions per agent); the async tool then prints `Queued: N earlier request(s) ...`. A synchronous request to your own current session is rejected.

### Named Sessions for inter-agent work

With source chat/topic context, each inter-agent conversation creates a
**Named Session** on the recipient agent with a sender/chat/topic-scoped name
(for example, `ia.main.t12345.xab12cd34`). Follow-up messages from the same
chat/topic reuse that session. Calls without source context use the legacy
`ia-{sender}` name.

These sessions:
- Persist across messages and survive bot restarts
- Run in the background, independently of foreground turns
- Bot-backed recipients expose them in `/sessions`; headless agents have no direct chat

Async responses report the exact session name. For bot-backed recipients,
you can continue directly in their chat using the reported name exactly: `@<session-name> <message>`.
Headless recipients have no chat: main continues through agent tools, reusing
the source chat/topic context. Synchronous responses do not include the session
name; use async or, for bot-backed recipients, their `/sessions` list. Do not
guess session names.

## Shared Knowledge

`SHAREDMEMORY.md` contains knowledge shared across all agents. Changes are
automatically synced into every agent's MAINMEMORY.md via the supervisor.

```bash
# View current shared knowledge
python3 tools/agent_tools/edit_shared_knowledge.py --show

# Append a fact
python3 tools/agent_tools/edit_shared_knowledge.py --append "New shared fact"

# Replace entire content
python3 tools/agent_tools/edit_shared_knowledge.py --set "Full new content"
```

When you learn something that is relevant to ALL agents (server facts, user
preferences, infrastructure changes), update shared knowledge instead of
only your own MAINMEMORY.md.

## Removing Sub-Agents

Removing a sub-agent stops its bot but **preserves its workspace**.
The workspace can be reused if the agent is re-created with the same name.

```bash
python3 tools/agent_tools/remove_agent.py "agent-name"
```

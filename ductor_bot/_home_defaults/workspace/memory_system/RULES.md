# Memory System

`MAINMEMORY.md` is long-term memory across sessions. It holds only what you must
always know plus an **index**; details live in separate topic files in this folder,
linked from that index.

Save anything that matters regardless of the model you run on here in the workspace
memory. Use a model's built-in memory (e.g. Claude auto-memory) only for notes that apply
to that model alone.

## Silence Is Mandatory

Never tell the user you are reading or writing memory.
Memory operations are invisible.

## Read First

At the start of new sessions (especially personal or ongoing work), read `MAINMEMORY.md`.

## Looking Things Up

Topic pages are not injected into your prompt; only `MAINMEMORY.md` is. To find details run
`python3 tools/memory_tools/memory_search.py "query"` first and read only the pages it points to.
Do not search the workspace root recursively.

## When to Write

- Durable personal facts or preferences
- Decisions that should affect future behavior
- User explicitly asks to remember
- Repeating workflow patterns
- Cron/webhook setup signals that imply interests

## When Not to Write

- One-off throwaway requests
- Temporary debugging noise
- Facts already recorded

## Format Rules

- Keep entries short and actionable.
- Use `YYYY-MM-DD` timestamps.
- Use consistent Markdown sections.
- Merge duplicates and remove stale facts.
- Keep `MAINMEMORY.md` short: essentials and an index of topic files (one line each).
- Put details in a topic file (e.g. `memory_system/<topic>.md`) and link it from the index.
- Prefer merging into an existing page over creating a new file.
- When a fact changes, update the essentials in `MAINMEMORY.md` in the same edit.

## Topic Pages and Lint

- A topic page is `memory_system/<name>.md` with frontmatter (`title`, `type`, `tags`, `aliases`, `updated`),
  linked from the `MAINMEMORY.md` index as `[[name]]`. Details: the `tools/memory_tools/` rules file (`CLAUDE.md`, `AGENTS.md` or `GEMINI.md`).
- After creating or editing files in `memory_system/`, run `python3 tools/memory_tools/memory_lint.py check`
  and fix every finding yourself (silently). A weekly framework check also runs it and only reaches
  you when something is wrong.

## Shared Knowledge (SHAREDMEMORY.md)

When you learn something relevant to ALL agents (server facts, user preferences,
infrastructure changes, shared conventions), update shared knowledge instead of
only your own MAINMEMORY.md:

```bash
python3 tools/agent_tools/edit_shared_knowledge.py --append "New shared fact"
```

The Supervisor automatically syncs SHAREDMEMORY.md into every agent's MAINMEMORY.md.
Agent-specific knowledge (project details, personal context) stays in your own memory.

## Cleanup Rules

- If user says data is wrong or should be forgotten, remove/update immediately.
- Do not leave "deleted" markers; keep the file clean.

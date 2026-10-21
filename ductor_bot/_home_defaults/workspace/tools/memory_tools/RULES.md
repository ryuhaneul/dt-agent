# Memory Tools

Read-only helpers for the `memory_system/` wiki. Standard library only; run with `python3`.
Paths default to this workspace's `memory_system/` (override with `--root PATH`).

| Tool | Purpose |
|------|---------|
| `memory_search.py "query"` | Find pages by keyword, frontmatter-aware (title/basename 30, tags/aliases 20, other frontmatter 8, body 3 per hit). `-n N` limits results, `--json` for machine output. `--docs` searches the bundled DT Agent reference (`docs/dt-agent/`) instead of the memory wiki. |
| `memory_lint.py check` | Report structural problems; exit 0 = clean, 1 = problems, 2 = bad usage. `--json` for machine output. Never edits anything. |
| `memory_parser.py` | Shared parser used by both (not run directly). |
| `LINT_FULL.md` | Instructions for the framework's `memory_lint.mode: full` turn (not a tool). You own this file: it is created once and never overwritten by updates; edit it to change what a full check does. |

## Looking things up

`MAINMEMORY.md` is injected at the start of a session; topic pages are **not**. When you need
details, run `python3 tools/memory_tools/memory_search.py "query"` first, then read only the
pages it points to. Do not search the workspace root recursively and do not guess grep keywords.

## After you edit memory_system

Run `python3 tools/memory_tools/memory_lint.py check` after creating or editing pages in
`memory_system/`, and fix every finding yourself (silently -- memory work is invisible to the
user). Do not delete content just to satisfy the lint: merge or correct it instead.

## Page conventions the lint expects

- Topic page = `memory_system/<name>.md` starting with frontmatter:

  ```
  ---
  title: Home network
  type: reference        # e.g. reference, project, feedback
  tags: [network, home]
  aliases: [LAN, router]
  updated: 2026-01-31    # YYYY-MM-DD
  links: [[other_page]]
  ---
  ```
- Link pages with `[[basename]]` (no extension); keep basenames unique.
- `MAINMEMORY.md` has no frontmatter and holds essentials plus an index: one line per topic or
  hub page, linked as `[[basename]]`. A page is reported if it cannot be reached from MAINMEMORY
  by following `[[links]]` directly or through linked pages (such as hubs).
- Keep `MAINMEMORY.md` small (default limits: 100 lines / 12 KiB, shared-knowledge block excluded).
- Never edit the block between `--- SHARED KNOWLEDGE START ---` and `--- SHARED KNOWLEDGE END ---`.

## Findings

missing/malformed frontmatter, broken or ambiguous `[[links]]`, pages not reachable from the MAINMEMORY
index through `[[links]]`, MAINMEMORY references to files that do not exist, oversized MAINMEMORY, duplicate
titles or basenames, and `type: project` pages not updated for over 183 days (refresh the
page after checking it is still true, or change its `type`).

The framework also runs this check periodically (`memory_lint` in config). In the default
`structure` mode it only starts an agent turn when it finds problems; in `full` mode it starts
one every period and the turn follows `LINT_FULL.md`; `off` runs nothing. Optional `schedule` (cron expression) and `provider`/`model`/`reasoning_effort` are described in the config reference.

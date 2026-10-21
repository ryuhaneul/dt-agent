# workspace/

Workspace/home-directory management for `~/.ductor`.

## Files

- `paths.py`: immutable `DuctorPaths` + `resolve_paths()`
- `init.py`: workspace init pipeline, zone copy rules, rule sync, runtime notice injection
- `rules_selector.py`: auth-aware RULES template selection/deployment + stale file cleanup
- `cron_tasks.py`: detect/backfill cron-task provider rule files; folder CRUD lives in deployed `tools/cron_tools/` scripts
- `skill_sync.py`: cross-tool skill sync and bundled-skill sync
- `loader.py`: safe file readers

## `DuctorPaths`

Paths are derived from the configured `ductor_home`. The paths below use the
host default `~/.ductor`; inside the default image replace that prefix with
`/ductor`, and for a sub-agent with `<main_home>/agents/<name>`.

Important runtime paths:

- `ductor_home`: `/ductor` (container default; host DUCTOR_HOME mount)
- `workspace`: `~/.ductor/workspace`
- `config_path`: `~/.ductor/config/config.json`
- `sessions_path`: `~/.ductor/sessions.json`
- `named_sessions_path`: `~/.ductor/named_sessions.json`
- `env_file`: `~/.ductor/.env`
- `tasks_registry_path`: `~/.ductor/tasks.json`
- `chat_activity_path`: `~/.ductor/chat_activity.json`
- `startup_state_path`: `~/.ductor/startup_state.json`
- `inflight_turns_path`: `~/.ductor/inflight_turns.json`
- `cron_jobs_path`: `~/.ductor/cron_jobs.json`
- `webhooks_path`: `~/.ductor/webhooks.json`
- `logs_dir`: `~/.ductor/logs`
- `cron_tasks_dir`: `~/.ductor/workspace/cron_tasks`
- `tasks_dir`: `~/.ductor/workspace/tasks`
- `api_files_dir`: `~/.ductor/workspace/api_files`
- `skills_dir`: `~/.ductor/workspace/skills`
- `bundled_skills_dir`: package `_home_defaults/workspace/skills`

## `init_workspace()` order

1. migrate legacy `workspace/tasks -> workspace/cron_tasks`
2. `sync_bundled_skills(paths)`
3. `_sync_home_defaults(paths)`
4. ensure required directories
5. `RulesSelector(paths).deploy_rules()`
6. `ensure_task_rule_files(paths.cron_tasks_dir)`
7. `sync_rule_files(paths.workspace)`
8. `_smart_merge_config(paths)`
9. `_clean_orphan_symlinks(paths)`
10. `sync_skills(paths)`

Idempotent by design (called from multiple startup paths).

Directory creation note:

- `workspace/api_files/` is not in `_REQUIRED_DIRS`; it is created lazily on first API upload via `prepare_destination(...)`.
- `workspace/tasks/` is part of `_REQUIRED_DIRS` and always created (used by shared `TaskHub` task folders).
- sub-agent homes do not create `logs/` by default; all agents write to the main home log file `~/.ductor/logs/agent.log`.

## Zone copy rules (`_walk_and_copy`)

### Zone 2 (always overwritten)

- `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`
- `.py` files in:
  - `workspace/tools/cron_tools/`
  - `workspace/tools/webhook_tools/`
  - `workspace/tools/agent_tools/`
  - `workspace/tools/task_tools/`
  - `workspace/tools/media_tools/`

The framework `.py` files directly in `workspace/tools/` and in the directories above (also `workspace/tools/browser_tools/` and `workspace/tools/memory_tools/`) are replaced on every start. The home directory keeps `.zone2_installed.json`, the SHA-256 of each framework file installed last time. A file that equals its recorded hash is the framework's own and is replaced without a backup. Any other content is a user edit (or, for an install made before the record existed, unknown) and is first moved to `<name>.py.bak`. A backup is never replaced: when `<name>.py.bak` exists, the next one gets `<name>.py.bak.<local timestamp>` (with a counter if needed), so a saved edit survives any number of updates. When a file cannot be compared or saved first, it is left unchanged with a warning and the update is tried again at the next start.

Special case:

- when copying a template `CLAUDE.md`, init also writes mirrored `AGENTS.md` and `GEMINI.md` in same target dir.

### Zone 3 (seed once)

- all other files: copied only when missing.

Skipped template files (handled by `RulesSelector`):

- `RULES.md`
- `RULES-claude-only.md`
- `RULES-codex-only.md`
- `RULES-gemini-only.md`
- `RULES-all-clis.md`

## RULES deployment (`rules_selector.py`)

Template variants:

- `RULES.md`
- `RULES-claude-only.md`
- `RULES-codex-only.md`
- `RULES-gemini-only.md`
- `RULES-all-clis.md`

Variant selection:

- `all-clis` when 2+ providers are authenticated
- `codex-only` when only Codex or only Grok
- `gemini-only` when only Gemini
- otherwise `claude-only`

Deployment outputs per authenticated provider:

- Claude -> `CLAUDE.md`
- Codex and/or Grok -> `AGENTS.md`
- Gemini -> `GEMINI.md`

rule-file names above.

Cleanup removes stale provider files for unauthenticated providers, except inside `workspace/cron_tasks/` (user-owned task rules).

## Rule sync (`sync_rule_files`)

Recursive mtime-based sync for:

- `CLAUDE.md`
- `AGENTS.md`
- `GEMINI.md`

Per directory:

- pick newest existing file
- copy to outdated existing siblings
- missing siblings are generally not created, except task-folder backfill via `ensure_task_rule_files(...)`

Background watcher: `watch_rule_files(workspace, interval=10s)`.

Watcher detail per cycle:

1. `ensure_task_rule_files(cron_tasks_dir)` backfills missing provider rule files in existing task folders.
2. `sync_rule_files(workspace)` propagates newest content to older sibling rule files.

## Runtime environment injection

`inject_runtime_environment(paths)` appends transport instructions and two named sections to each existing workspace rule file (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`):

- `## Multi-Agent Identity` (main/sub-agent context + communication hints, including bot-backed versus headless creation/chat guidance and main-mediated results)
- container runtime notice (`/ductor` mount)

The memory wiki tools live in `tools/memory_tools/` (`memory_parser.py`, `memory_search.py`, `memory_lint.py`, `RULES.md`; Zone 2, framework-managed): frontmatter-aware keyword search, and a read-only lint (frontmatter, broken/ambiguous `[[links]]`, pages not reachable from the MAINMEMORY index through `[[links]]`, MAINMEMORY size limits 100 lines / 12 KiB without the shared-knowledge block, duplicate titles/basenames, stale `type: project` pages after 183 days; exit 0 clean, 1 problems, 2 usage; `--json`). Agents run the lint after editing `memory_system/`; see [MemoryLintConfig](../config.md#memorylintconfig) for the periodic framework run (`mode` structure/full/off; optional `schedule` cron expression and `provider`/`model`/`reasoning_effort` for the lint turn in the Memory Lint topic). In `full` mode the agent turn follows `tools/memory_tools/LINT_FULL.md`, a user-owned file seeded once and never overwritten (framework updates of its default text do not reach an existing copy).

`BEHAVIOR.md` (a short default of essential always-on principles) and `PERSONA.md` (a blank form carrying the `<!-- PERSONA_UNSET -->` marker) are seed-once Zone 3 files: created only when missing in a new home or new sub-agent workspace, never overwritten or updated afterwards. `workspace/loader.py` skips `PERSONA.md` while it still carries the marker when building the appended system-prompt block (other appended files may mention the marker freely) and exposes `persona_is_unset()` for the first-run hint. The memory templates (`memory_system/RULES.md`, `MAINMEMORY.md` header) state that MAINMEMORY holds essentials plus an index with details in topic files.

Generated main identity and agent-tool rules distinguish bot-backed subs from headless workers with no direct chat or bot token. They document `create_agent.py --name NAME --transport headless` with optional provider/model, direct users to delegate through main, and explain main-mediated async results. Sync-delegation questions belong in the final report and the server rejects questions to main during that work. Rules and generated sub identity tell the sub to continue work without inventing unknown values, leaving them blank or stating assumptions; async-delegation synchronous questions to main use the originating active session or the source-header fallback. See [question routing](multiagent.md#questions-during-delegation).

Duplicate prevention: injection is skipped when either marker already exists (`## Multi-Agent Identity` or `## Runtime Environment`).

## Cron task folders (`cron_tasks.py`)

The deployed `cron_add.py` tool uses `_create_task_folder(...)` to create:

```text
cron_tasks/<safe_name>/
  CLAUDE.md    # only if Claude authenticated
  AGENTS.md    # if Codex and/or Grok authenticated
  GEMINI.md    # only if Gemini authenticated
  TASK_DESCRIPTION.md
  <safe_name>_MEMORY.md
  scripts/
```

Rule file selection is based on which provider rule files exist in the parent `cron_tasks/` directory (deployed by `RulesSelector` during workspace init). Falls back to `CLAUDE.md` when no parent rule files are found.

Path traversal protection is enforced for create/delete operations.

`sync_rule_files()` itself only updates already-existing rule files by mtime. Missing task-folder rule files are created by `ensure_task_rule_files(...)` in init and watcher cycles.

## Skill sync summary

`sync_skills()` syncs between:

- `~/.ductor/workspace/skills`
- `~/.claude/skills`
- `~/.codex/skills`
- `~/.gemini/skills`
- `$GROK_HOME/skills` (default `~/.grok/skills`)

Skill synchronization uses symlinks/junctions inside the bot container.

See `docs/modules/skill_system.md`.

## Loader helpers

- `read_file(path) -> str | None`
- `read_mainmemory(paths) -> str`

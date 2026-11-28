"""Cron definitions and task workspaces shared by the runtime and CLI tools."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import shlex
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim, CronSimError

from ductor_bot.cli.autocompact import validate_autocompact
from ductor_bot.cron.jobs_file import read_jobs_data, write_jobs_data
from ductor_bot.cron.manager import DELIVERY_MODES
from ductor_bot.infra.atomic_io import atomic_bytes_save, atomic_text_save
from ductor_bot.infra.file_lock import exclusive_file_lock

DEFAULT_INSTRUCTION = (
    "Read through TASK_DESCRIPTION.md and carry it out. Stay focused and complete the task neatly."
)
RULE_FILENAMES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")
BODY_LIMIT = 1024 * 1024


_MODE_MEANING = {
    "separate": "a separate run; the result arrives as a plain message (the topic's session "
    "does not see it)",
    "inject": "a separate run; its result is injected into the topic's session, which replies "
    "(one extra model turn of that session per run)",
    "in_session": "no separate run: the instruction runs as a turn of the topic's session, with "
    "that session's own model (one model turn of that session per run)",
}


def describe_job(job: dict[str, Any], timezone: str) -> list[dict[str, str]]:
    """Per-setting value and one-line meaning, for the agent to tell the user."""
    mode = str(job.get("delivery_mode", "separate"))
    chat = job.get("chat_id", 0)
    topic = job.get("topic_id")
    if chat:
        target = f"chat {chat}" + (f", topic {topic}" if topic is not None else ", main thread")
        target += f" ({job.get('transport', 'tg')})"
    else:
        target = "broadcast to all users"
    model_parts = [
        f"{label}={job[key]}"
        for key, label in (
            ("provider", "provider"),
            ("model", "model"),
            ("reasoning_effort", "effort"),
        )
        if job.get(key)
    ]
    quiet = (
        f"{job['quiet_start']}:00-{job['quiet_end']}:00"
        if job.get("quiet_start") is not None and job.get("quiet_end") is not None
        else "none (this job has no quiet hours; the heartbeat quiet hours do not apply to cron)"
    )
    return [
        {
            "setting": "schedule",
            "value": f"{job.get('schedule')} ({timezone or 'UTC, no user_timezone set'})",
            "meaning": "when the job runs; the hours are read in this timezone",
        },
        {
            "setting": "target",
            "value": target,
            "meaning": "where the result goes (the chat/topic the job was created in)",
        },
        {
            "setting": "delivery_mode",
            "value": mode,
            "meaning": _MODE_MEANING.get(mode, "unknown mode (runs as separate)"),
        },
        {
            "setting": "model",
            "value": ", ".join(model_parts) or "global config defaults",
            "meaning": (
                "ignored in in_session mode: the topic session's own model is used"
                if mode == "in_session"
                else "which CLI/model runs the job"
            ),
        },
        {
            "setting": "quiet_hours",
            "value": quiet,
            "meaning": "the job does not run during these hours",
        },
        {
            "setting": "silent_on_success",
            "value": str(bool(job.get("silent_on_success", False))).lower(),
            "meaning": (
                "no result message when a run succeeds (errors still arrive); has no effect in "
                "inject and in_session mode"
            ),
        },
        {
            "setting": "dependency",
            "value": str(job.get("dependency") or "none"),
            "meaning": "jobs with the same dependency run one at a time",
        },
    ]


def render_cron_task_claude_md(name: str, ductor_home: Path) -> str:
    """Render fixed CLAUDE.md/AGENTS.md content for a cron task folder."""
    return f"""\
# Your Mission

You are an **automated agent**. You run on a schedule with NO human interaction.
Complete your task autonomously and update memory when done.

## Workflow

1. **Read** `{name}_MEMORY.md` first -- it contains context from previous runs.
2. **Read** the whole `TASK_DESCRIPTION.md`!
3. **Follow the assignment** in `TASK_DESCRIPTION.md`!
4. Perform the task conscientiously!
5. **Update** `{name}_MEMORY.md` with the current date/time and what you did.

## Rules

- Stay focused on this task only. Do not deviate.
- Do not modify files outside this task folder unless explicitly instructed by the user or the assigned TASK_DESCRIPTION.md.
- Check whether all scripts are already present in the script folder! (IF they are needed!)
- Use `.venv` for Python dependencies: `source .venv/bin/activate`

## Secrets and master keys

- Tool credentials in `secrets.env` may be encrypted with a master key. Provider startup keys in `.env` are separate and are not encrypted by this feature.
- Use `load_secrets("KEY")` from `{ductor_home / "workspace" / "tools"}/_secrets.py` for only the needed names; an existing environment value takes precedence. Never read/source `secrets.env`, its `.enc` file, or plaintext copies directly.
- Check `python3 {ductor_home / "workspace" / "tools"}/secret_tools/secret_status.py` before a task that needs secrets. It reports `unconfigured`, `locked`, or `unlocked`, TTL and remaining time without values. Restart always locks a configured store; TTL expiry locks it too; TTL 0 means unlimited until restart or manual lock.
- `SECRETS_LOCKED` means stop the secret-dependent work and report the code. Ask the owner to use `/masterkey unlock` in the main Telegram bot's private chat or unlock in the webchat modal. Do not bypass the lock, search for plaintext/backups, inspect process memory, or retrieve authentication tokens from files or other processes.
- `SECRETS_RATE_LIMITED` means wait for `retry_after`; do not loop, retry aggressively, or bypass the shared limit. `SECRET_NOT_SET` means the requested name is missing; never invent a value.
- Never ask for, accept, or process a master key in ordinary conversation, prompts, task files, or tool arguments. Master-key input belongs only in the `/masterkey` private input flow or the webchat modal. If locked, request the unlock action, not the key itself.
- Never cat, print, log or bulk-source credential files, including `.env`; inspect names without values.
- Never print, log, mirror, or persist secret values or master/derived keys in chat, memory, task descriptions, scripts, results, or temporary files. Use only needed values inside the consuming process. Key-name inventories and status tools must never fetch values.

- When creating or editing a secret-dependent schedule, run the cron checker last and include the locked-run failure warning in the completion report.

## Important

You provide the final answer after the task is completed in a pleasant, concise,
and well-formatted manner.
"""


def render_task_description_md(title: str, description: str) -> str:
    """Render the TASK_DESCRIPTION.md template for a cron task."""
    return f"""\
# {title}

## Goal

{description}

## Assignment

(Detailed instructions for completing this task. Be specific and actionable.)

## Output

(What should the final result look like? Format, content, destination.)
"""


def sanitize_name(raw: str) -> str:
    """Lowercase and normalize a name to ``[a-z0-9-]``."""
    slug = re.sub(r"[^a-z0-9-]", "-", raw.lower())
    return re.sub(r"-{2,}", "-", slug).strip("-")


def schedule_problem(schedule: str) -> str:
    """Why the scheduler cannot accept an expression, or an empty string."""
    try:
        next(CronSim(schedule, datetime(2000, 1, 1, tzinfo=UTC)))
    except CronSimError as exc:
        return f"Invalid cron expression {schedule!r}: {exc}"
    except StopIteration:
        return f"Cron expression {schedule!r} never matches"
    return ""


def validate_timezone(timezone: str) -> ZoneInfo:
    """Resolve an IANA timezone; reject unknown names and filesystem paths."""
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Invalid IANA timezone: {timezone!r}") from exc


def next_runs(
    expr: str,
    tz: str,
    n: int = 3,
    horizon: timedelta = timedelta(days=366 * 5),
    *,
    now: datetime | None = None,
) -> list[datetime]:
    """Find up to *n* occurrences within at most five calendar years."""
    zone = validate_timezone(tz)
    start = (now or datetime.now(UTC)).astimezone(zone)
    try:
        maximum = start.replace(year=start.year + 5)
    except ValueError:  # February 29 at a non-leap-year endpoint.
        maximum = start.replace(year=start.year + 5, day=28)
    end = min(start + horizon, maximum)
    iterator = CronSim(expr, start)
    result: list[datetime] = []
    for _ in range(n):
        try:
            occurrence = next(iterator)
        except StopIteration:
            break
        if occurrence > end:
            break
        result.append(occurrence)
    return result


def schedule_summary(expr: str) -> str:
    """Return cronsim's human-readable schedule description."""
    return str(CronSim(expr, datetime(2000, 1, 1, tzinfo=UTC)).explain())


def safe_task_dir(task_folder: str, tasks_dir: Path) -> Path:
    """Resolve and validate a task folder under the supplied cron task root."""
    folder = (tasks_dir / task_folder).resolve()
    if not folder.is_relative_to(tasks_dir.resolve()):
        raise ValueError(f"Path traversal blocked: {task_folder!r} resolves outside cron_tasks")
    return folder


def detect_rule_filenames(tasks_dir: Path) -> list[str]:
    """Use the authenticated providers' parent rules, defaulting to CLAUDE.md."""
    return [name for name in RULE_FILENAMES if (tasks_dir / name).is_file()] or ["CLAUDE.md"]


def create_task_folder(
    name: str,
    body: str,
    *,
    tasks_dir: Path,
    ductor_home: Path,
    folder: Path | None = None,
) -> Path:
    """Create task rules, the supplied assignment, memory and scripts directory."""
    task_dir = folder if folder is not None else safe_task_dir(name, tasks_dir)
    task_dir.mkdir(parents=True, exist_ok=False)
    rule_content = render_cron_task_claude_md(name, ductor_home)
    for filename in detect_rule_filenames(tasks_dir):
        (task_dir / filename).write_text(rule_content, encoding="utf-8")
    (task_dir / "TASK_DESCRIPTION.md").write_text(body, encoding="utf-8")
    (task_dir / f"{name}_MEMORY.md").write_text(f"# {name} Memory\n", encoding="utf-8")
    (task_dir / "scripts").mkdir(exist_ok=True)
    return task_dir


_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,55}\Z")
_SECRET_NAME = re.compile(r"(?:^|_)(?:KEY|TOKEN|PASSWORD|PASS|SECRET|SEED|CREDENTIALS?)\Z")
_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".cache"}
_EXTENSIONS = {".py", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".ps1"}
_LIMITATION = (
    "Static evidence only: dynamic names, imports, outside files and external commands may be "
    "missed; comments or unused code may cause false positives. Review the assignment even "
    "when no evidence is found. Unknown names may be provided by the environment instead of "
    "the tool store. No secret values are requested or returned."
)
_WARNING = (
    "If this job needs secrets, report that restart, manual lock or TTL expiry locks a "
    "configured store and secret-dependent runs fail with SECRETS_LOCKED until the owner "
    "uses /masterkey unlock or webchat unlock. Never put values or master keys in task files."
)


def secret_candidate(name: object, *, explicit: bool = False) -> str | None:
    if isinstance(name, str) and _NAME.fullmatch(name) and (explicit or _SECRET_NAME.search(name)):
        return name
    return None


def _python(source: str) -> set[str]:
    names: set[str] = set()
    for part in ast.walk(ast.parse(source)):
        if isinstance(part, ast.Name) and (name := secret_candidate(part.id)):
            names.add(name)
        if isinstance(part, ast.Call):
            function = part.func
            called = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else ""
            )
            if called == "load_secrets" or (
                called == "get"
                and isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Attribute)
                and function.value.attr == "environ"
            ):
                for argument in part.args if called == "load_secrets" else part.args[:1]:
                    if isinstance(argument, ast.Constant) and (
                        name := secret_candidate(argument.value, explicit=True)
                    ):
                        names.add(name)
        if (
            isinstance(part, ast.Subscript)
            and isinstance(part.value, ast.Attribute)
            and part.value.attr == "environ"
            and isinstance(part.slice, ast.Constant)
            and (name := secret_candidate(part.slice.value, explicit=True))
        ):
            names.add(name)
    return names


def _commands(source: str) -> set[str]:  # noqa: C901, PLR0912 -- moved CLI scanner
    names: set[str] = set()
    for line in source.splitlines():
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        initial = (
            words[1:] if words and words[0] in {"export", "readonly", "local", "declare"} else words
        )
        for word in initial:
            if "=" not in word:
                break
            if name := secret_candidate(word.split("=", 1)[0]):
                names.add(name)
        command = Path(words[0]).name if words else ""
        tool = words[1] if command.startswith("python") and len(words) > 1 else command
        if Path(tool).name in {"fill_secret.py", "fill_secret"}:
            if name := secret_candidate(words[-1], explicit=True):
                names.add(name)
            for index, word in enumerate(words):
                if word in {"--env", "--key"} and index + 1 < len(words):
                    if name := secret_candidate(words[index + 1], explicit=True):
                        names.add(name)
                elif word.startswith(("--env=", "--key=")) and (
                    name := secret_candidate(word.split("=", 1)[1], explicit=True)
                ):
                    names.add(name)
        # Single-quoted literals do not expand shell environment references.
        expandable = re.sub(r"'[^']*'", "", line)
        for match in re.finditer(r"\$(?:\{)?([A-Z][A-Z0-9_]{0,55})\b", expandable):
            if name := secret_candidate(match[1]):
                names.add(name)
    return names


def read_description(path: Path, limit: int) -> tuple[int, bytes | None]:
    """Return the byte size and read the description only within the limit."""
    size = path.stat().st_size
    if size > limit:
        return size, None
    with path.open("rb") as stream:
        original = stream.read(limit)
        if len(original) == limit:
            size = os.fstat(stream.fileno()).st_size
            if size > limit:
                return size, None
    return len(original), original


def scan_secrets(  # noqa: C901, PLR0912 -- moved CLI scanner
    folder: Path,
    declared: set[str],
    inventory: set[str] | None,
    *,
    body: str | None = None,
    body_limit: int | None = None,
) -> dict[str, object]:
    """Inspect task/scripts, optionally substituting an unsaved assignment body.

    The override lets web clients check a proposed create/edit before saving it.
    A byte limit can be applied to the stored description alone.
    No inventory values or source excerpts are returned.
    """
    names: set[str] = set()
    indicators: set[str] = set()
    skipped = 0
    skipped_file_reasons: dict[str, str] = {}
    files = [folder / "TASK_DESCRIPTION.md"]
    for directory, dirs, children in os.walk(folder / "scripts", followlinks=False):
        skipped += sum(name not in _SKIP and (Path(directory) / name).is_symlink() for name in dirs)
        dirs[:] = [
            name for name in dirs if name not in _SKIP and not (Path(directory) / name).is_symlink()
        ]
        files.extend(
            Path(directory) / name for name in children if Path(name).suffix in _EXTENSIONS
        )
    for file in files:
        if file.is_symlink() or not file.resolve().is_relative_to(folder):
            skipped += 1
            continue
        try:
            is_description = file.name == "TASK_DESCRIPTION.md"
            if is_description and body is not None:
                source = body
            elif is_description and body_limit is not None:
                _, description = read_description(file, body_limit)
                if description is None:
                    skipped += 1
                    skipped_file_reasons[file.name] = "too_large"
                    continue
                source = description.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            else:
                source = file.read_text(encoding="utf-8")
            indicators.update(
                marker for marker in ("load_secrets", "fill_secret") if marker in source
            )
            names.update(_python(source) if file.suffix == ".py" else _commands(source))
            if file.name == "TASK_DESCRIPTION.md":
                for match in re.finditer(r"\bload_secrets\s*\([^\n)]*\)", source):
                    try:
                        names.update(_python(match[0]))
                    except SyntaxError:
                        continue
            # Only match already declared/inventoried names in free prose, never arbitrary literals.
            for name in declared | (inventory or set()):
                if re.search(r"(?<![A-Z0-9_])" + re.escape(name) + r"(?![A-Z0-9_])", source):
                    names.add(name)
        except (OSError, UnicodeError, SyntaxError):
            skipped += 1
    result: dict[str, object] = {
        "requires_secrets": bool(names or indicators),
        "keys": sorted(names),
        "indicators": sorted(indicators),
        "inventory_available": inventory is not None,
        "skipped_files": skipped,
        "warning": _WARNING if names or indicators else None,
        "limitations": [_LIMITATION],
    }
    if skipped_file_reasons:
        result["skipped_file_reasons"] = skipped_file_reasons
    if inventory is not None:
        result.update(known=sorted(names & inventory), unknown=sorted(names - inventory))
    return result


class DefinitionConflictError(ValueError):
    """The id or task body changed since the caller last read it."""


def body_sha256(body: str) -> str:
    """Hash the UTF-8 task body for optimistic concurrency checks."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _validate_body(body: str) -> None:
    if len(body.encode("utf-8")) > BODY_LIMIT:
        raise ValueError(f"Task description exceeds {BODY_LIMIT // 1024} KiB")


def _validate_job(job: dict[str, Any]) -> None:
    if not isinstance(job.get("title"), str) or not job["title"].strip():
        raise ValueError("Title must not be empty")
    if not isinstance(job.get("schedule"), str):
        raise TypeError("Schedule must be a cron expression")
    if problem := schedule_problem(job["schedule"]):
        raise ValueError(problem)
    if job.get("timezone"):
        validate_timezone(job["timezone"])
    if not isinstance(job.get("enabled", True), bool):
        raise TypeError("Enabled must be a boolean")
    mode = job.get("delivery_mode", "separate")
    if mode not in DELIVERY_MODES:
        raise ValueError(f"Unknown delivery mode: {mode!r}")
    if mode != "separate" and not job.get("chat_id"):
        raise ValueError(f"delivery_mode {mode!r} needs a chat")
    if job.get("autocompact") is not None:
        validate_autocompact(job["autocompact"])


def _job_by_id(data: dict[str, Any], job_id: str) -> dict[str, Any]:
    for job in data["jobs"]:
        if job.get("id") == job_id:
            return dict(job)
    raise KeyError(job_id)


def _workspace_job_dir(job: dict[str, Any], tasks_dir: Path) -> Path:
    name = str(job.get("task_folder", job["id"]))
    # Web lifecycle operations never address the root or internal dot directories.
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        raise ValueError("Invalid task folder")
    folder = tasks_dir / name
    if folder.is_symlink():
        raise ValueError("Linked task folders cannot be modified")
    return safe_task_dir(name, tasks_dir)


def create_job(
    jobs_path: Path,
    tasks_dir: Path,
    job: dict[str, Any],
    body: str,
    *,
    ductor_home: Path,
) -> dict[str, Any]:
    """Publish a complete task folder before enabling its JSON definition.

    CLI add retains its original template workflow. This entry point takes the
    final assignment, for clients such as the web form.
    """
    definition = dict(job)
    name = definition.get("id")
    if not isinstance(name, str) or not name or sanitize_name(name) != name:
        raise ValueError("Invalid job id")
    definition.update(
        task_folder=name,
        agent_instruction=DEFAULT_INSTRUCTION,
        enabled=True,
        created_at=datetime.now(UTC).isoformat(),
    )
    definition.setdefault("description", "")
    definition.setdefault("last_run_at", None)
    definition.setdefault("last_run_status", None)
    _validate_job(definition)
    _validate_body(body)
    tasks_dir.mkdir(parents=True, exist_ok=True)
    # The CLI also holds the registry lock while claiming a task name.
    with exclusive_file_lock(tasks_dir / ".definitions"), exclusive_file_lock(jobs_path):
        data = read_jobs_data(jobs_path)
        if any(entry.get("id") == name for entry in data["jobs"]):
            raise DefinitionConflictError(f"Job {name!r} already exists")
        final = tasks_dir / name
        if final.exists() or final.is_symlink():
            raise DefinitionConflictError(f"Folder {name!r} already exists")
        final = safe_task_dir(name, tasks_dir)
        staging = tasks_dir / f".staging-{name}-{uuid4().hex}"
        published = False
        try:
            create_task_folder(
                name, body, tasks_dir=tasks_dir, ductor_home=ductor_home, folder=staging
            )
            staging.rename(final)
            published = True
            data["jobs"].append(definition)
            write_jobs_data(jobs_path, data)
        except BaseException:
            cleanup = final if published else staging
            if cleanup.exists():
                shutil.rmtree(cleanup)
            raise
    return definition


def cleanup_staging(tasks_dir: Path) -> None:
    """Remove abandoned creation folders at startup, preserving archives/history."""
    if not tasks_dir.is_dir():
        return
    with exclusive_file_lock(tasks_dir / ".definitions"):
        for folder in tasks_dir.glob(".staging-*"):
            if folder.is_dir() and not folder.is_symlink():
                shutil.rmtree(folder)


def _kst_stamp() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d-%H%M%S-%f")


def _save_body(folder: Path, body: str, expected_sha256: str | None) -> tuple[Path, bytes]:
    path = folder / "TASK_DESCRIPTION.md"
    if path.is_symlink():
        raise ValueError("Linked task descriptions cannot be modified")
    previous = path.read_bytes()
    if expected_sha256 is None or hashlib.sha256(previous).hexdigest() != expected_sha256:
        raise DefinitionConflictError("Task description changed; reload before saving")
    history = folder / ".history"
    if history.is_symlink():
        raise ValueError("Linked task history cannot be modified")
    history.mkdir(exist_ok=True)
    backup = history / f"{_kst_stamp()}.md"
    atomic_bytes_save(backup, previous)
    if path.is_symlink() or path.read_bytes() != previous:
        backup.unlink()
        raise DefinitionConflictError("Task description changed; reload before saving")
    # Bound history before replacing the body, even if the replacement fails.
    for old in sorted(history.glob("*.md"), reverse=True)[10:]:
        old.unlink()
    atomic_text_save(path, body)
    return path, previous


def update_job(  # noqa: PLR0913
    jobs_path: Path,
    tasks_dir: Path,
    job_id: str,
    fields: dict[str, Any],
    *,
    body: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Update editable fields and optionally a body protected by its SHA-256."""
    allowed = {"title", "schedule", "timezone", "delivery_mode", "autocompact", "enabled"}
    if fields.keys() - allowed:
        raise ValueError("Unsupported job fields")
    if body is not None:
        _validate_body(body)
    # Hold the registry lock through the file change, save and any rollback.
    with exclusive_file_lock(tasks_dir / ".definitions"), exclusive_file_lock(jobs_path):
        data = read_jobs_data(jobs_path)
        job = _job_by_id(data, job_id)
        job.update(fields)
        # Allow a broken stored definition to be disabled without editing it.
        if body is not None or fields.keys() != {"enabled"} or fields["enabled"] is not False:
            _validate_job(job)
        folder = _workspace_job_dir(job, tasks_dir)
        data["jobs"] = [job if entry.get("id") == job_id else entry for entry in data["jobs"]]
        saved_body = _save_body(folder, body, expected_sha256) if body is not None else None
        try:
            write_jobs_data(jobs_path, data)
        except BaseException:
            if saved_body is not None:
                atomic_bytes_save(*saved_body)
            raise
        return job


def archive_job(jobs_path: Path, tasks_dir: Path, job_id: str) -> Path | None:
    """Archive any existing task folder and restore it if removing the definition fails."""
    if not job_id or job_id.startswith(".") or "/" in job_id or "\\" in job_id:
        raise ValueError("Invalid job id")
    with exclusive_file_lock(tasks_dir / ".definitions"), exclusive_file_lock(jobs_path):
        data = read_jobs_data(jobs_path)
        job = _job_by_id(data, job_id)
        folder = _workspace_job_dir(job, tasks_dir)
        data["jobs"] = [entry for entry in data["jobs"] if entry.get("id") != job_id]
        destination = None
        if folder.exists():
            archive = tasks_dir / ".archive"
            if archive.is_symlink():
                raise ValueError("Linked archive directories cannot be modified")
            archive.mkdir(exist_ok=True)
            destination = archive / f"{job_id}-{_kst_stamp()}"
            folder.rename(destination)
        try:
            write_jobs_data(jobs_path, data)
        except BaseException:
            if destination is not None:
                destination.rename(folder)
            raise
    return destination

"""Agent-local cron editing through the authenticated web bridge."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from ductor_bot.chat_log.ids import from_tid
from ductor_bot.cli.autocompact import (
    parse_autocompact_input,
    resolve_autocompact,
    stored_autocompact,
)
from ductor_bot.config import resolve_user_timezone
from ductor_bot.cron import definitions
from ductor_bot.cron.jobs_file import read_jobs_data
from ductor_bot.cron.manager import DELIVERY_MODES
from ductor_bot.infra.atomic_io import complete_write
from ductor_bot.infra.input_context import input_lock
from ductor_bot.infra.secret_runtime import vault_list_keys
from ductor_bot.infra.secret_store import store_path
from ductor_bot.infra.secret_vault import VaultError

if TYPE_CHECKING:
    from ductor_bot.messenger.telegram.app import TelegramBot
    from ductor_bot.session.key import SessionKey
    from ductor_bot.webchat.topics import WebTopics
    from ductor_bot.webchat.turns import WebTurns

_PREFIX = "/webchat/v1/topics/{tid}/cron"
_EDITABLE = {"title", "schedule", "timezone", "delivery_mode", "autocompact", "enabled"}
_ROUTING = {"chat_id", "topic_id", "transport"}


class _InvalidError(ValueError):
    def __init__(self, field: str, code: str, status: int = 400) -> None:
        self.field, self.code, self.status = field, code, status
        super().__init__(code)


def _response(value: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(value, status=status, headers={"Cache-Control": "no-store"})


async def _body(request: web.Request) -> dict[str, Any]:
    if request.method not in {"POST", "PUT"}:
        return {}
    try:
        data = await request.json()
    except (ValueError, UnicodeError):
        raise _InvalidError("request", "invalid_json") from None
    if not isinstance(data, dict):
        raise _InvalidError("request", "invalid_json")
    return data


def _text(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str):
        raise _InvalidError(field, "required")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _InvalidError(field, "invalid_text") from None
    return value


def _content(data: dict[str, Any]) -> str:
    content = _text(data, "content")
    size = len(content.encode("utf-8"))
    if size > definitions.BODY_LIMIT:
        raise _InvalidError("content", "too_large", 413)
    return content


def _fields(data: dict[str, Any]) -> dict[str, Any]:  # noqa: C901
    fields = {key: value for key, value in data.items() if key in _EDITABLE}
    for field in ("title", "schedule", "timezone"):
        if field in fields:
            fields[field] = _text(fields, field).strip()
    if "title" in fields and not fields["title"]:
        raise _InvalidError("title", "required")
    if "schedule" in fields:
        try:
            problem = definitions.schedule_problem(fields["schedule"])
        except (ValueError, OverflowError):
            raise _InvalidError("schedule", "invalid_schedule") from None
        if problem:
            raise _InvalidError("schedule", "invalid_schedule")
    if fields.get("timezone"):
        try:
            definitions.validate_timezone(fields["timezone"])
        except ValueError:
            raise _InvalidError("timezone", "invalid_timezone") from None
    if "enabled" in fields and type(fields["enabled"]) is not bool:
        raise _InvalidError("enabled", "invalid_boolean")
    if "delivery_mode" in fields and fields["delivery_mode"] not in DELIVERY_MODES:
        raise _InvalidError("delivery_mode", "invalid_delivery_mode")
    if fields.get("autocompact") is not None:
        try:
            fields["autocompact"] = parse_autocompact_input(fields["autocompact"])
        except ValueError:
            raise _InvalidError("autocompact", "invalid_autocompact") from None
    return fields


def _job(jobs: list[dict[str, Any]], job_id: str) -> dict[str, Any]:
    for job in jobs:
        if job.get("id") == job_id:
            return job
    raise KeyError(job_id)


def _folder(tasks: Path, job: dict[str, Any]) -> Path:
    name = str(job.get("task_folder", job["id"]))
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        raise KeyError(job["id"])
    logical = tasks / name
    if logical.is_symlink():
        raise KeyError(job["id"])
    folder = definitions.safe_task_dir(name, tasks)
    if not folder.is_dir() or (folder / "TASK_DESCRIPTION.md").is_symlink():
        raise KeyError(job["id"])
    return folder


def _detail(tasks: Path, job: dict[str, Any], inventory: set[str] | None) -> dict[str, Any]:
    folder = _folder(tasks, job)
    with (folder / "TASK_DESCRIPTION.md").open("rb") as stream:
        original = stream.read(definitions.BODY_LIMIT + 1)
    if len(original) > definitions.BODY_LIMIT:
        raise _InvalidError("content", "too_large", 413)
    scripts: list[str] = []
    walk = (
        () if (folder / "scripts").is_symlink() else os.walk(folder / "scripts", followlinks=False)
    )
    for directory, dirs, files in walk:
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        scripts.extend(
            str((Path(directory) / name).relative_to(folder / "scripts"))
            for name in files
            if not (Path(directory) / name).is_symlink()
        )
    return {
        "content": original.decode("utf-8"),
        "sha256": hashlib.sha256(original).hexdigest(),
        "scripts": sorted(scripts),
        "secret_warning": definitions.scan_secrets(folder, set(), inventory),
    }


class _CronAPI:
    def __init__(self, bot: TelegramBot, topics: WebTopics, turns: WebTurns) -> None:
        self.bot, self.topics, self.turns = bot, topics, turns

    async def startup(self, _app: web.Application) -> None:
        await complete_write(definitions.cleanup_staging, self.bot._orch.paths.cron_tasks_dir)

    async def inventory(self) -> set[str] | None:
        """Match the CLI checker's best-effort inventory without requesting values."""
        try:
            return set(await vault_list_keys(store_path(self.bot._orch.paths.ductor_home)))
        except (VaultError, OSError):
            return None

    def timezone(self, job: dict[str, Any]) -> str:
        return str(resolve_user_timezone(job.get("timezone") or self.bot.config.user_timezone))

    async def metadata(self, job: dict[str, Any]) -> dict[str, Any]:
        value = stored_autocompact(job.get("autocompact"), str(job["id"]), inherit=True)
        chat, topic, transport = (
            job.get("chat_id", 0),
            job.get("topic_id"),
            job.get("transport", "tg"),
        )
        target = "broadcast"
        if chat:
            target = f"chat {chat}" + (f", topic {topic}" if topic is not None else "")
            if transport == "tg":
                tid = str(chat) + (f".{topic}" if topic is not None else "")
                target = next(
                    (item["name"] for item in await self.topics.list() if item["tid"] == tid),
                    target,
                )
        return {
            "id": job["id"],
            "title": job.get("title", ""),
            "schedule": job["schedule"],
            "schedule_summary": definitions.schedule_summary(job["schedule"]),
            "timezone": self.timezone(job),
            "enabled": job.get("enabled", True),
            "last_run_at": job.get("last_run_at"),
            "last_run_status": job.get("last_run_status"),
            "target": {"chat_id": chat, "topic_id": topic, "transport": transport, "name": target},
            "delivery_mode": job.get("delivery_mode", "separate"),
            "autocompact": value,
            "effective_autocompact": resolve_autocompact(value, self.bot._orch._config.autocompact),
        }

    async def defaults(self, key: SessionKey) -> dict[str, Any]:
        session = await self.bot._orch._sessions.get_active(key)
        return {
            "timezone": self.bot.config.user_timezone,
            "delivery_mode": "inject" if key.chat_id else "separate",
            "autocompact": session.autocompact if session else None,
        }

    async def preview(self, data: dict[str, Any]) -> dict[str, Any]:
        if data.keys() - {"schedule", "timezone"}:
            raise _InvalidError("request", "unsupported_fields")
        _text(data, "schedule")
        fields = _fields(data)
        timezone = self.timezone(fields)
        runs = await asyncio.to_thread(definitions.next_runs, fields["schedule"], timezone)
        return {
            "valid": True,
            "schedule_summary": definitions.schedule_summary(fields["schedule"]),
            "timezone": timezone,
            "next_runs": [run.isoformat() for run in runs],
        }

    async def create(self, data: dict[str, Any], key: SessionKey) -> dict[str, Any]:
        if data.keys() - (_EDITABLE | _ROUTING | {"id", "content"}):
            raise _InvalidError("request", "unsupported_fields")
        name = definitions.sanitize_name(_text(data, "id").strip())
        if not name:
            raise _InvalidError("id", "invalid_id")
        for field in ("title", "schedule"):
            _text(data, field)
        content = _content(data)
        fields = {**await self.defaults(key), **_fields(data)}
        fields.update(id=name, chat_id=key.chat_id, topic_id=key.topic_id, transport=key.transport)
        if fields["delivery_mode"] != "separate" and not key.chat_id:
            raise _InvalidError("delivery_mode", "chat_required")
        paths = self.bot._orch.paths
        warning = await asyncio.to_thread(
            definitions.scan_secrets,
            paths.cron_tasks_dir / name,
            set(),
            await self.inventory(),
            body=content,
        )
        job = await complete_write(
            definitions.create_job,
            paths.cron_jobs_path,
            paths.cron_tasks_dir,
            fields,
            content,
            ductor_home=paths.ductor_home,
        )
        return {"job": job, "secret_warning": warning}

    async def update(self, job: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        if data.keys() - (_EDITABLE | _ROUTING | {"content", "base_sha256"}):
            raise _InvalidError("request", "unsupported_fields")
        fields = _fields(data)
        if fields.get(
            "delivery_mode", job.get("delivery_mode", "separate")
        ) != "separate" and not job.get("chat_id"):
            raise _InvalidError("delivery_mode", "chat_required")
        content = _content(data) if "content" in data else None
        base = _text(data, "base_sha256") if content is not None else None
        paths = self.bot._orch.paths
        folder = _folder(paths.cron_tasks_dir, job)
        warning = await asyncio.to_thread(
            definitions.scan_secrets, folder, set(), await self.inventory(), body=content
        )
        saved = await complete_write(
            definitions.update_job,
            paths.cron_jobs_path,
            paths.cron_tasks_dir,
            str(job["id"]),
            fields,
            body=content,
            expected_sha256=base,
        )
        return {"job": saved, "secret_warning": warning}

    async def toggle(self, job: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        if data.keys() - {"enabled"}:
            raise _InvalidError("request", "unsupported_fields")
        fields = _fields({"enabled": data.get("enabled", not job.get("enabled", True))})
        paths = self.bot._orch.paths
        warning = None
        try:
            folder = _folder(paths.cron_tasks_dir, job)
        except KeyError:
            pass  # A broken workspace must still be possible to disable.
        else:
            warning = await asyncio.to_thread(
                definitions.scan_secrets, folder, set(), await self.inventory()
            )
        saved = await complete_write(
            definitions.update_job,
            paths.cron_jobs_path,
            paths.cron_tasks_dir,
            str(job["id"]),
            fields,
        )
        return {"job": saved, "secret_warning": warning}

    async def dispatch(
        self, request: web.Request, key: SessionKey, data: dict[str, Any]
    ) -> web.Response:
        paths = self.bot._orch.paths
        if request.path.endswith("/preview") and request.method == "POST":
            return _response(await self.preview(data))
        if request.method == "POST" and not request.match_info.get("id"):
            result, status = await self.create(data, key), 201
        else:
            jobs = (await asyncio.to_thread(read_jobs_data, paths.cron_jobs_path))["jobs"]
            if request.method == "GET" and not request.match_info.get("id"):
                return _response(
                    {
                        "jobs": [await self.metadata(job) for job in jobs],
                        "defaults": await self.defaults(key),
                    }
                )
            job = _job(jobs, request.match_info["id"])
            if request.method == "GET":
                detail = await asyncio.to_thread(
                    _detail, paths.cron_tasks_dir, job, await self.inventory()
                )
                return _response({"job": {**await self.metadata(job), **detail}})
            if request.method == "DELETE":
                await complete_write(
                    definitions.archive_job,
                    paths.cron_jobs_path,
                    paths.cron_tasks_dir,
                    str(job["id"]),
                )
                result = {"ok": True, "id": job["id"]}
            elif request.path.endswith("/toggle"):
                result = await self.toggle(job, data)
            else:
                result = await self.update(job, data)
            status = 200
        await asyncio.to_thread(self.bot._orch._cron_manager.reload)
        observer = self.bot._orch._observers.cron
        if observer is not None:
            observer.request_reschedule()
        if "job" in result:
            saved = result["job"]
            result["job"] = await self.metadata(saved)
            if request.method == "PUT":
                result["job"].update(
                    await asyncio.to_thread(
                        _detail, paths.cron_tasks_dir, saved, await self.inventory()
                    )
                )
        return _response(result, status)

    async def handle(self, request: web.Request) -> web.Response:  # noqa: PLR0911
        tid = request.match_info["tid"]
        if tid == "unknown":
            return _response({"error": "read_only"}, 409)
        try:
            # A 256 KiB UTF-8 body can occupy six times as much escaped JSON.
            request = request.clone(client_max_size=2 * 1024 * 1024)
            data = await _body(request)
            async with self.topics.gate(tid):
                if tid in self.turns.blocked or await self.topics.lookup(tid) is None:
                    return _response({"error": "unknown_topic"}, 404)
                key = from_tid(tid)
                async with input_lock(self.bot._sequential.get_lock(key.lock_key)):
                    return await self.dispatch(request, key, data)
        except _InvalidError as exc:
            result: dict[str, Any] = {"error": "validation", "fields": {exc.field: exc.code}}
            if request.path.endswith("/preview"):
                result["valid"] = False
            return _response(result, exc.status)
        except definitions.DefinitionConflictError:
            return _response({"error": "conflict"}, 409)
        except KeyError:
            return _response({"error": "not_found"}, 404)
        except FileNotFoundError:
            return _response(
                {"error": "not_found" if request.method == "GET" else "save_failed"},
                404 if request.method == "GET" else 503,
            )
        except web.HTTPRequestEntityTooLarge:
            return _response({"error": "too_large"}, 413)
        except (OSError, ValueError, TypeError):
            return _response({"error": "save_failed"}, 503)


def register_cron_routes(
    app: web.Application, bot: TelegramBot, topics: WebTopics, turns: WebTurns
) -> None:
    api = _CronAPI(bot, topics, turns)
    app.on_startup.append(api.startup)
    app.router.add_get(_PREFIX, api.handle, allow_head=False)
    app.router.add_post(_PREFIX, api.handle)
    app.router.add_post(_PREFIX + "/preview", api.handle)
    app.router.add_get(_PREFIX + "/{id}", api.handle, allow_head=False)
    app.router.add_put(_PREFIX + "/{id}", api.handle)
    app.router.add_delete(_PREFIX + "/{id}", api.handle)
    app.router.add_post(_PREFIX + "/{id}/toggle", api.handle)

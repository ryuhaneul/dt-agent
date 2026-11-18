"""Private vault operations outside conversations, mirrors, and persistent web state."""

from __future__ import annotations

import hmac
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from ductor_bot.infra.secret_runtime import get_secret_vault
from ductor_bot.infra.secret_store import validate_key
from ductor_bot.infra.secret_vault import SecretVault, VaultError

if TYPE_CHECKING:
    from ductor_bot.chat_log.runtime import ChatLogRuntime

_PREFIX = "/webchat/v1"
_BODY_LIMIT = 64 * 1024
_FIELDS: dict[str, dict[str, type[object]]] = {
    "setup": {"master_key": str, "confirm_key": str},
    "unlock": {"master_key": str},
    "lock": {},
    "change": {"current_key": str, "new_key": str, "confirm_key": str},
    "ttl": {"ttl_seconds": int},
    "set": {"key": str, "value": str},
    "delete": {"key": str},
    "reveal": {"key": str, "master_key": str},
}


def _response(data: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


def _failure(exc: VaultError) -> web.Response:
    code = "MASTER_KEY_INVALID" if exc.code == "INVALID_MASTER_KEY" else exc.code
    status = {
        "MASTER_KEY_INVALID": 422,
        "SECRETS_LOCKED": 409,
        "WEB_AUTH_MIGRATION_REQUIRED": 409,
        "SECRETS_RATE_LIMITED": 429,
    }.get(code, 400)
    result: dict[str, Any] = {"error": code}
    if exc.retry_after is not None:
        result["retry_after"] = exc.retry_after
    return _response(result, status)


async def _body(request: web.Request, action: str) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise ValueError("bad_request")
    data = await request.json()
    fields = _FIELDS[action]
    if not isinstance(data, dict) or set(data) != set(fields):
        raise ValueError("bad_request")
    if any(type(data[field]) is not expected for field, expected in fields.items()):
        raise ValueError("bad_request")
    return data


def _confirm(key: str, confirmation: str) -> None:
    if not hmac.compare_digest(key.encode("utf-8"), confirmation.encode("utf-8")):
        raise VaultError("MASTER_KEY_MISMATCH")


async def _operate(vault: SecretVault, action: str, data: dict[str, Any]) -> dict[str, Any]:
    if "key" in data and validate_key(data["key"]):
        raise VaultError("KEY_INVALID")
    if action == "setup":
        _confirm(data["master_key"], data["confirm_key"])
        await vault.setup(data["master_key"])
    elif action == "unlock":
        await vault.unlock(data["master_key"])
    elif action == "lock":
        await vault.lock()
    elif action == "change":
        _confirm(data["new_key"], data["confirm_key"])
        await vault.change_key(data["current_key"], data["new_key"])
    elif action == "ttl":
        await vault.set_ttl(data["ttl_seconds"])
    elif action == "set":
        await vault.set(data["key"], data["value"])
    elif action == "delete":
        return {"ok": True, "deleted": await vault.delete(data["key"])}
    elif action == "reveal":
        # A freshly authenticated, single value is returned only in this response.
        return {"value": await vault.reveal(data["key"], data["master_key"])}
    return {"ok": True}


def register_vault_routes(  # noqa: C901
    app: web.Application, path: Path, runtime: ChatLogRuntime
) -> None:
    """Bridge-token protection is installed by create_bridge_app, before these handlers."""
    subscribed: SecretVault | None = None

    def changed() -> None:
        runtime.events.publish({"type": "secrets_changed"})

    async def connect(_app: web.Application) -> None:
        nonlocal subscribed
        try:
            subscribed = get_secret_vault(path)
        except VaultError:
            return
        if subscribed is not None:
            subscribed.add_change_listener(changed)

    async def disconnect(_app: web.Application) -> None:
        if subscribed is not None:
            subscribed.remove_change_listener(changed)

    async def handle(request: web.Request) -> web.Response:
        request = request.clone(client_max_size=_BODY_LIMIT)
        try:
            if request.method == "GET" and request.can_read_body:
                await request.read()
                return _response({"error": "bad_request"}, 400)
            data = (
                await _body(request, request.match_info["action"])
                if request.method == "POST"
                else {}
            )
            vault = get_secret_vault(path)
            if vault is None:
                return _response({"error": "SECRETS_UNAVAILABLE"}, 503)
            if request.method == "GET":
                result = (
                    asdict(await vault.status())
                    if request.path.endswith("/status")
                    else {"keys": await vault.list_keys()}
                )
            else:
                result = await _operate(vault, request.match_info["action"], data)
            return _response(result)
        except web.HTTPRequestEntityTooLarge:
            return _response({"error": "too_large"}, 413)
        except (ValueError, UnicodeError, LookupError):
            return _response({"error": "bad_request"}, 400)
        except VaultError as exc:
            return _failure(exc)

    async def headers(request: web.Request, response: web.StreamResponse) -> None:
        if request.path.startswith((f"{_PREFIX}/masterkey/", f"{_PREFIX}/secrets/")):
            response.headers["Cache-Control"] = "no-store"

    app.on_startup.append(connect)
    app.on_cleanup.append(disconnect)
    app.on_response_prepare.append(headers)
    app.router.add_get(f"{_PREFIX}/masterkey/status", handle)
    app.router.add_get(f"{_PREFIX}/secrets/keys", handle)
    app.router.add_post(f"{_PREFIX}/masterkey/{{action:setup|unlock|lock|change|ttl}}", handle)
    app.router.add_post(f"{_PREFIX}/secrets/{{action:set|delete|reveal}}", handle)

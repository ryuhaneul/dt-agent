"""Agent-local automation compaction state shared by selectors and the web bridge."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from ductor_bot.cli.autocompact import resolve_autocompact, validate_autocompact
from ductor_bot.cron.manager import CronManager
from ductor_bot.webhook.manager import WebhookManager

if TYPE_CHECKING:
    from ductor_bot.config import AutocompactValue
    from ductor_bot.orchestrator.core import Orchestrator


def automation_state(orch: Orchestrator) -> dict[str, Any]:
    """Return fresh public metadata only; never serialize prompts or webhook credentials."""
    global_value = orch._config.autocompact
    jobs = CronManager(jobs_path=orch.paths.cron_jobs_path).list_jobs()
    hooks = WebhookManager(hooks_path=orch.paths.webhooks_path).list_hooks()
    items: list[dict[str, Any]] = [
        {
            "kind": "cron",
            "id": entry.id,
            "name": entry.title,
            "schedule": entry.schedule,
            "enabled": entry.enabled,
            "value": entry.autocompact,
            "effective_value": resolve_autocompact(entry.autocompact, global_value),
        }
        for entry in jobs
    ]
    items.extend(
        {
            "kind": "webhook",
            "id": hook.id,
            "name": hook.title,
            "schedule": "",
            "enabled": hook.enabled,
            "value": hook.autocompact,
            "effective_value": resolve_autocompact(hook.autocompact, global_value),
        }
        for hook in hooks
    )
    return {"global_value": global_value, "items": items}


def automation_token(item_id: str) -> str:
    return hashlib.sha256(item_id.encode()).hexdigest()[:16]


def automation_target(orch: Orchestrator, kind: str, token: str) -> dict[str, Any] | None:
    matches = [
        item
        for item in automation_state(orch)["items"]
        if item["kind"] == kind and automation_token(item["id"]) == token
    ]
    return matches[0] if len(matches) == 1 else None


def save_automation(
    orch: Orchestrator, kind: str, item_id: str, value: AutocompactValue | None
) -> bool:
    """Validate before writing, reusing each registry's existing save operation."""
    if kind not in {"cron", "webhook"}:
        raise ValueError("Unknown automation kind")
    concrete = validate_autocompact(value) if value is not None else None
    if kind == "cron":
        try:
            return orch._cron_manager.set_autocompact(item_id, concrete)
        except OSError:
            orch._cron_manager.reload()
            raise
    orch._webhook_manager.reload()
    try:
        return orch._webhook_manager.update_hook(item_id, autocompact=concrete)
    except OSError:
        orch._webhook_manager.reload()
        raise

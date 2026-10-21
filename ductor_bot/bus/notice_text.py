"""Texts of the notices a transport sends on its own, shared by Matrix and Slack.

They use the same keys as the Telegram notices, so the same notice reads the same in every
transport. Stored values and agent input are never changed here; only what a person reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ductor_bot.bus.failure_reason import display_reason
from ductor_bot.i18n import t
from ductor_bot.text.response_format import (
    SEP,
    fmt,
    show_run_result,
    show_run_status,
    show_task_error,
)

if TYPE_CHECKING:
    from ductor_bot.bus.envelope import Envelope


def background_text(env: Envelope) -> str:
    """Result of a background session or task."""
    elapsed = t("age.s", n=f"{env.elapsed_seconds:.0f}")
    if env.session_name:
        name = env.session_name
        if env.status == "aborted":
            return fmt(t("notice.session_cancelled", name=name), SEP, f"_{env.prompt_preview}_")
        if env.is_error:
            body = (
                show_run_result(env.status, env.result_text[:2000])
                if env.result_text
                else t("notice.no_output")
            )
            return fmt(t("notice.session_failed", name=name, elapsed=elapsed), SEP, body)
        return fmt(
            t("notice.session_complete", name=name, elapsed=elapsed),
            SEP,
            env.result_text or t("notice.no_output"),
        )
    task_id = env.metadata.get("task_id", "?")
    if env.status == "aborted":
        return fmt(
            t("notice.bg_cancelled"),
            SEP,
            t("notice.bg_cancelled_body", task_id=task_id, prompt=env.prompt_preview),
        )
    if env.is_error:
        output = (
            show_run_result(env.status, env.result_text[:2000])
            if env.result_text
            else t("notice.no_output")
        )
        return fmt(
            t("notice.bg_failed", elapsed=elapsed),
            SEP,
            t(
                "notice.bg_failed_body",
                task_id=task_id,
                status=show_run_status(env.status),
                prompt=env.prompt_preview,
            )
            + f"\n\n{output}",
        )
    return fmt(
        t("notice.bg_complete", elapsed=elapsed), SEP, env.result_text or t("notice.no_output")
    )


def interagent_failed_text(env: Envelope) -> str:
    """The notice for an inter-agent request that failed."""
    session_info = t("notice.ia_session", name=env.session_name) if env.session_name else ""
    return t(
        "notice.ia_failed",
        agent=env.metadata.get("recipient", "?"),
        session=session_info,
        error=(
            display_reason(str(env.metadata["error"]))
            if "error" in env.metadata
            else t("notice.unknown")
        ),
        request=env.prompt_preview,
    )


def task_result_note(env: Envelope) -> str:
    """The notice for a finished task (empty when the status has none)."""
    name = env.metadata.get("name", env.metadata.get("task_id", "?"))
    if env.status == "done":
        duration = t("age.s", n=f"{env.elapsed_seconds:.0f}")
        target = f"{env.provider}/{env.model}" if env.provider else ""
        detail = f"{duration}, {target}" if target else duration
        return t("notice.task_done", name=name, detail=detail)
    if env.status == "cancelled":
        return t("notice.task_cancelled", name=name)
    if env.status == "failed":
        reason = (
            show_task_error(str(env.metadata["error"]))
            if "error" in env.metadata
            else t("notice.unknown")
        )
        return t("notice.task_failed", name=name, reason=reason)
    return ""


def task_question_note(env: Envelope) -> str:
    """The notice that a task asks a question."""
    return t("notice.task_question", task_id=env.metadata.get("task_id", "?"), question=env.prompt)


def cron_body(status: str, result: str) -> str:
    """The result of a cron or webhook run; its status when it has no text."""
    return show_run_result(status, result) if result else f"_{show_run_status(status)}_"


def cron_text(env: Envelope, clean_result: str) -> str:
    """A cron result with its header."""
    title = env.metadata.get("title", "?")
    return f"{t('notice.task_header', title=title)}\n\n{cron_body(env.status, clean_result)}"


def webhook_cron_text(env: Envelope) -> str:
    """A webhook cron result with its header."""
    title = env.metadata.get("hook_title", "?")
    return f"{t('notice.webhook_header', title=title)}\n\n{cron_body(env.status, env.result_text)}"

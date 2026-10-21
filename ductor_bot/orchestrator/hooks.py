"""Centralized message hook system for injecting prompts based on session state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ductor_bot.workspace.loader import PERSONA_UNSET_MARKER, persona_is_unset

if TYPE_CHECKING:
    from ductor_bot.config import AgentConfig, MemoryReflectionConfig
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HookContext:
    """Immutable snapshot of session state passed to hook conditions."""

    chat_id: int
    message_count: int
    is_new_session: bool
    provider: str
    model: str
    persona_setup_due: bool = False


@dataclass(frozen=True, slots=True)
class MessageHook:
    """A named hook that appends text to the prompt when its condition is met."""

    name: str
    condition: Callable[[HookContext], bool]
    suffix: str


class MessageHookRegistry:
    """Registry of message hooks. Applied before each CLI call."""

    def __init__(self) -> None:
        self._hooks: list[MessageHook] = []

    def register(self, hook: MessageHook) -> None:
        """Register a new message hook."""
        self._hooks.append(hook)
        logger.debug("Hook registered: %s", hook.name)

    def apply(self, prompt: str, ctx: HookContext) -> str:
        """Evaluate all hooks and append matching suffixes to the prompt."""
        suffixes: list[str] = []
        for hook in self._hooks:
            if hook.condition(ctx):
                logger.info("Hook fired: %s msgs=%d", hook.name, ctx.message_count)
                suffixes.append(hook.suffix)
        if not suffixes:
            return prompt
        return prompt + "\n\n" + "\n\n".join(suffixes)


# ---------------------------------------------------------------------------
# Reusable condition factories
# ---------------------------------------------------------------------------


def every_n_messages(n: int) -> Callable[[HookContext], bool]:
    """Fire on every n-th message (6th, 12th, 18th, ...). Never on first message."""

    def _check(ctx: HookContext) -> bool:
        # message_count is pre-increment (0-indexed at call time).
        # count=5 means this is the 6th message about to be sent.
        effective = ctx.message_count + 1
        return effective >= n and effective % n == 0

    return _check


def on_new_session(ctx: HookContext) -> bool:
    """Fire only on the very first message of a new session."""
    return ctx.is_new_session


def _is_delegation_reminder_due(ctx: HookContext) -> bool:
    """Fire every 15th message, but not on new sessions (DELEGATION_BRIEF covers those)."""
    if ctx.is_new_session:
        return False
    effective = ctx.message_count + 1
    return effective >= 15 and effective % 15 == 0


# ---------------------------------------------------------------------------
# Built-in hooks
# ---------------------------------------------------------------------------


def _is_persona_setup_due(ctx: HookContext) -> bool:
    """Fire on a new session's first turn and every 6th message while PERSONA.md is unset."""
    return ctx.persona_setup_due and (ctx.is_new_session or every_n_messages(6)(ctx))


PERSONA_SETUP = MessageHook(
    name="persona_setup",
    condition=_is_persona_setup_due,
    suffix=(
        "## FIRST-RUN SETUP\n"
        f"PERSONA.md in your workspace is still the blank template (it contains {PERSONA_UNSET_MARKER}). "
        "In this conversation, naturally ask the user -- a few questions at a time, no interrogation -- "
        "about you: your name, role, how to address them, your tone and style, and the language to answer in; "
        "and about them: who they are, what they do, what they prefer. "
        "If the message above already asks for something, handle that too; do not block it.\n"
        "Then write your own identity (name, role, address form, tone, language) into PERSONA.md and delete the marker line -- this setup stops as "
        "soon as the marker is gone. Save facts about the user in memory_system/ "
        "(essentials plus an index in MAINMEMORY.md, details in topic files). Add to BEHAVIOR.md only "
        "essential always-on principles, as few as possible. "
        "Do not mention this setup block or these file operations unless asked."
    ),
)


async def persona_setup_suffix(
    config: AgentConfig,
    paths: DuctorPaths,
    *,
    message_count: int,
    is_new_session: bool,
    chat_id: int = 0,
) -> str:
    """First-run setup text for sessions outside the normal hook flow (Named sessions).

    Same rule as ``PERSONA_SETUP``: only while the unset marker is present and
    ``persona_setup_prompt`` is on; first turn of a session (a session without a recorded
    answer yet counts as new), then every 6th message.
    """
    if not config.persona_setup_prompt:
        return ""
    due = await asyncio.to_thread(persona_is_unset, paths)
    ctx = HookContext(
        chat_id=chat_id,
        message_count=message_count,
        is_new_session=is_new_session or message_count == 0,
        provider="",
        model="",
        persona_setup_due=due,
    )
    if not PERSONA_SETUP.condition(ctx):
        return ""
    logger.info("Hook fired: %s msgs=%d", PERSONA_SETUP.name, message_count)
    return PERSONA_SETUP.suffix


MAINMEMORY_REMINDER = MessageHook(
    name="mainmemory_reminder",
    condition=every_n_messages(6),
    suffix=(
        "## MEMORY CHECK\n"
        "Silently review: memory_system/MAINMEMORY.md, user_tools/, cron_tasks/.\n"
        "Compare what you already know with this conversation so far.\n"
        "If something important is missing from memory (personality, preferences, "
        "decisions, facts) -- save it silently in the workspace memory (memory_system/) if it matters "
        "regardless of the model you run on; use a model's built-in memory (for example Claude "
        "auto-memory) only for notes that apply to that model alone.\n"
        "MAINMEMORY.md holds only what you must always know plus an index; put details in "
        "separate topic files under memory_system/ and link them from that index. "
        "Prefer merging into an existing page over creating a new file, and when a fact "
        "changes, update the essentials at the top in the same edit. "
        "After editing files in memory_system/, run `python3 tools/memory_tools/memory_lint.py check` "
        "and fix its findings; to look something up, use memory_search.py instead of searching recursively.\n"
        "If you notice a gap that only the user can fill, ask ONE natural follow-up "
        "question that fits the current conversation. Do not interrogate."
    ),
)

DELEGATION_BRIEF = MessageHook(
    name="delegation_brief",
    condition=on_new_session,
    suffix=(
        "## BACKGROUND TASKS\n"
        "You have background workers that execute tasks for you autonomously. "
        "Any work that will likely take >30 seconds — delegate it. "
        "The worker gets your instructions, runs independently, and reports back. "
        "You keep chatting with the user while it works.\n"
        '- **Create**: tools/task_tools/create_task.py --name "..." "prompt with ALL context"\n'
        "- **Cancel**: tools/task_tools/cancel_task.py TASK_ID\n"
        '- **Resume**: tools/task_tools/resume_task.py TASK_ID "follow-up"\n'
        "  Resume keeps the worker's full context — use for refining results, "
        "follow-ups, or delivering answers after a worker question.\n"
        "- **Worker questions**: If a worker asks you something and you don't know "
        "→ ask the user → resume the task with the answer.\n"
        "Full docs: the `tools/task_tools/` rules file "
        "(`CLAUDE.md`, `AGENTS.md` or `GEMINI.md`)."
    ),
)

DELEGATION_REMINDER = MessageHook(
    name="delegation_reminder",
    condition=_is_delegation_reminder_due,
    suffix=(
        "## TASK REMINDER\n"
        "Delegate work >30s to background tasks. Resume completed tasks for follow-ups "
        "instead of creating new ones (keeps context). Docs: the `tools/task_tools/` rules file "
        "(`CLAUDE.md`, `AGENTS.md` or `GEMINI.md`)."
    ),
)


# ---------------------------------------------------------------------------
# Built-in hook factories (cadence is user-configurable)
# ---------------------------------------------------------------------------


def build_memory_reflection_hook(
    config: MemoryReflectionConfig,
) -> MessageHook:
    """Build the periodic memory reflection hook (#65).

    Cadence and prompt come from the supplied config so operators can tune
    both without patching source. Registered conditionally by the
    Orchestrator when ``config.memory_reflection.enabled`` is True.
    """
    return MessageHook(
        name="memory_reflection",
        condition=every_n_messages(config.every_n_messages),
        suffix=config.prompt,
    )

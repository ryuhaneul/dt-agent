"""Shared formatting primitives for command response text."""

from __future__ import annotations

import re

from ductor_bot.i18n import t

SEP = "\u2500\u2500\u2500"

_SHELL_TOOLS = frozenset({"bash", "powershell", "cmd", "sh", "zsh", "shell"})
_TOOL_LABELS = {
    "toolsearch": "Search",
    "searchtool": "Search",
    "webfetch": "Web fetch",
    "websearch": "Web search",
}


def normalize_tool_name(name: str) -> str:
    """Normalize shell-related tool names to 'Shell' for display."""
    lower = name.lower()
    if lower in _SHELL_TOOLS:
        return "Shell"
    return _TOOL_LABELS.get(lower, name)


_RESUME_TAIL = re.compile(
    r"interrupted by restart\n\nTo continue this task's conversation, use:\n"
    r"(?P<command>python3 tools/task_tools/resume_task\.py \S+ \"your follow-up\")"
)
_TIMEOUT_REASON = re.compile(r"Timeout after (?P<secs>\d+)s")
_STALE_REASON = re.compile(r"stale process killed after (?P<secs>\d+)s")


def show_task_error(error: str) -> str:
    """The reason of a failed task for a person, in the configured language.

    Only the fixed reasons the product writes are translated, and only when the whole text
    matches; the resume command line, task ids and anything else (an agent's or a CLI's own
    words) stay as they are. The stored error and the text the parent agent gets stay English.
    """
    if error == "interrupted by restart":
        return t("task_error.interrupted")
    resume = _RESUME_TAIL.fullmatch(error)
    if resume:
        return (
            f"{t('task_error.interrupted')}\n\n{t('task_error.resume_hint')}\n{resume['command']}"
        )
    if error == "Bot restarted while task was running":
        return t("task_error.bot_restarted")
    if error == "CLI error":
        return t("task_error.cli_error")
    for pattern, key in (
        (_TIMEOUT_REASON, "task_error.timeout"),
        (_STALE_REASON, "task_error.stale_killed"),
    ):
        found = pattern.fullmatch(error)
        if found:
            return t(key, secs=found["secs"])
    return error


# Fixed result texts the product writes for cron, webhook and background runs: the status they
# come with, the whole text, and the message that renders them.
_RUN_FORMS = tuple(
    (prefix, re.compile(pattern, re.DOTALL), key)
    for prefix, pattern, key in (
        ("error:cli_not_found", r"\[(?P<provider>.+) CLI not found\]", "run.cli_not_found"),
        ("error:timeout", r"\[(?P<label>.+) timed out after (?P<secs>\d+)s\]", "run.timed_out"),
        (
            "error:exception",
            r"Cron job '(?P<title>.*)' failed with an error \((?P<error_type>\w+)\)\.",
            "run.cron_failed",
        ),
        (
            "error:injection",
            r"Cron job '(?P<title>.*)' could not run in this session\.",
            "run.cron_no_session",
        ),
    )
)
_EXIT_STATUS = re.compile(r"error:exit_(?P<code>-?\d+)")
_STATUS_LABELS = (
    ("success", "run.status_success"),
    ("aborted", "run.status_aborted"),
    ("error:timeout", "run.status_timeout"),
    ("error:cli_not_found", "run.status_cli_not_found"),
    ("error:exception", "run.status_exception"),
    ("error:injection", "run.status_injection"),
)


def show_run_result(status: str, text: str) -> str:
    """A cron, webhook or background result for a person, in the configured language.

    Only the fixed texts the product writes for a failed run are translated, and only when the
    status and the whole text both match; output of the CLI or the agent stays as it is. The
    stored result and the text the agent gets stay English.
    """
    for prefix, pattern, key in _RUN_FORMS:
        if status.startswith(prefix):
            found = pattern.fullmatch(text)
            if found:
                return t(key, **found.groupdict())
    return text


def show_run_status(status: str) -> str:
    """A run status for a person; a status that is not a known one is shown as it is."""
    exit_status = _EXIT_STATUS.fullmatch(status)
    if exit_status:
        return t("run.status_exit", code=exit_status["code"])
    for prefix, key in _STATUS_LABELS:
        if status == prefix or (prefix.startswith("error:") and status.startswith(prefix)):
            return t(key)
    return status


_SYSTEM_LABEL_KEYS = {
    "thinking": "stream.label_thinking",
    "compacting": "stream.label_compacting",
    "recovering": "stream.label_recovering",
    "timeout_warning": "stream.label_timeout_warning",
    "timeout_extended": "stream.label_timeout_extended",
}


def system_status_label(status: str | None) -> str | None:
    """The status line for a system status event, in the configured language."""
    key = _SYSTEM_LABEL_KEYS.get(status or "")
    return t(key) if key else None


# The two Matrix login errors the product raises (``matrix/credentials.py``), as the supervisor
# stores them: the exception type, then the exact text.
_MATRIX_NO_CREDENTIALS = re.compile(
    r"RuntimeError: Matrix AUTH FAILED for (?P<user_id>.*)\n"
    r"  No access_token, device_id, or password configured\.\n"
    r"  Set 'password' in the matrix section of config\.json\."
)
_MATRIX_LOGIN_FAILED = re.compile(
    r"RuntimeError: Matrix AUTH FAILED for (?P<user_id>.*)\n"
    r"  Homeserver: (?P<homeserver>.*)\n"
    r"  Error: (?P<error>.*)\n"
    r"  Check your credentials in config\.json \(matrix section\)\.",
    re.DOTALL,
)


def show_crash_error(text: str) -> str:
    """A stored crash error for a person, in the configured language.

    Only the two Matrix login errors the product writes are translated, and only when the whole
    text matches; user ids, the homeserver, configuration keys and the server's own error text
    are put in as they are. Any other error (an external crash text) is returned unchanged.
    The stored text and the logs stay English.
    """
    for pattern, key in (
        (_MATRIX_NO_CREDENTIALS, "crash.matrix_no_credentials"),
        (_MATRIX_LOGIN_FAILED, "crash.matrix_login_failed"),
    ):
        found = pattern.fullmatch(text)
        if found:
            return t(key, **found.groupdict())
    return text


_GEMINI_EXITED = re.compile(r"Gemini exited with code (?P<code>-?\d+)")
_CLI_ERROR_KEYS = {
    "Codex failed before producing a final response.": "cli_error.codex_no_final",
    "Unknown Gemini error": "cli_error.gemini_unknown",
    "Grok error": "cli_error.grok_error",
    "CLI error": "task_error.cli_error",
}


def show_cli_error(text: str) -> str:
    """A provider error for a person, in the configured language.

    Only the fixed texts the product writes when a provider gives no message of its own are
    translated, and only when the whole text matches; the provider's own words and any other
    text are returned unchanged. The stored result and the text the agent gets stay English.
    """
    key = _CLI_ERROR_KEYS.get(text)
    if key:
        return t(key)
    found = _GEMINI_EXITED.fullmatch(text)
    if found:
        return t("cli_error.gemini_exited", code=found["code"])
    return text


def format_short_duration(seconds: float) -> str:
    """Elapsed seconds as a compact text in the configured language (45s / 2m / 3h / 1d)."""
    if seconds < 60:
        return t("age.s", n=int(seconds))
    if seconds < 3600:
        return t("age.m", n=int(seconds / 60))
    if seconds < 86400:
        return t("age.h", n=int(seconds / 3600))
    return t("age.d", n=int(seconds / 86400))


def format_uptime(seconds: float) -> str:
    """An agent's uptime in the configured language (same steps as ``AgentHealth.uptime_human``)."""
    if seconds < 60:
        return t("age.s", n=f"{seconds:.0f}")
    if seconds < 3600:
        return t("age.m", n=f"{seconds / 60:.0f}")
    hours = seconds / 3600
    if hours < 24:
        return t("age.h", n=f"{hours:.1f}")
    return t("age.d", n=f"{hours / 24:.1f}")


_UPTIME_TEXT = re.compile(r"(?P<n>\d+(?:\.\d+)?)(?P<unit>[smhd])")


def show_uptime(text: str) -> str:
    """An uptime text of the internal API (``45s`` / ``2m`` / ``1.5h`` / ``2.0d``) for a person.

    Only that exact form is translated; any other text is returned unchanged.
    """
    found = _UPTIME_TEXT.fullmatch(text)
    return t(f"age.{found['unit']}", n=found["n"]) if found else text


_KNOWN_LABELS = {
    "task_status": ("running", "waiting", "done", "failed", "cancelled"),
    "session_status": ("running", "idle", "ended"),
    "agent_status": ("running", "starting", "crashed", "stopped"),
    "auth_status": ("authenticated", "installed", "not_found"),
}


def _label(section: str, value: str) -> str:
    """The label of a known stored value; a value that is not known is shown as it is."""
    return t(f"{section}.{value}") if value in _KNOWN_LABELS[section] else value


def show_task_status(status: str) -> str:
    """A background task status (running / waiting / done / failed / cancelled) for a person."""
    return _label("task_status", status)


def show_session_status(status: str) -> str:
    """A named session status (running / idle / ended) for a person."""
    return _label("session_status", status)


def show_agent_status(status: str) -> str:
    """An agent status (running / starting / crashed / stopped) for a person."""
    return _label("agent_status", status)


def show_agent_role(name: str) -> str:
    """The role of an agent: the main agent or a sub-agent."""
    return t("agent_role.main") if name == "main" else t("agent_role.sub")


def show_auth_status(status: str) -> str:
    """A provider authentication status for a person."""
    return _label("auth_status", status)


def fmt(*blocks: str) -> str:
    """Join non-empty blocks with double newlines."""
    return "\n\n".join(b for b in blocks if b)


# Known CLI error patterns -> user-friendly short explanation.
_AUTH_PATTERNS = (
    "401",
    "unauthorized",
    "authentication",
    "signing in again",
    "sign in again",
    "token has been",
)
_RATE_PATTERNS = (
    "429",
    "rate limit",
    "too many requests",
    "quota exceeded",
    # Codex wording for usage-cap exhaustion (e.g. "You've hit your usage limit. …").
    "usage limit",
    "upgrade to pro",
    "hit your",
)
_CONTEXT_PATTERNS = ("context length", "token limit", "maximum context", "too long")


def classify_cli_error(raw: str) -> str | None:
    """Return a user-facing hint for known CLI error patterns, or None."""
    lower = raw.lower()
    if any(p in lower for p in _AUTH_PATTERNS):
        return t("session.error_auth")
    if any(p in lower for p in _RATE_PATTERNS):
        return t("session.error_rate")
    if any(p in lower for p in _CONTEXT_PATTERNS):
        return t("session.error_context")
    return None


def session_error_text(model: str, cli_detail: str = "") -> str:
    """Build the error message shown to the user on CLI failure."""
    base = fmt(t("session.error_header"), SEP, t("session.error_body", model=model))
    hint = classify_cli_error(cli_detail) if cli_detail else None
    if hint:
        return fmt(base, t("session.error_cause", hint=hint))
    if cli_detail:
        # Show first meaningful line, truncated.
        detail = show_cli_error(cli_detail.strip().split("\n")[0])[:200]
        return fmt(base, t("session.error_detail", detail=detail))
    return base


def timeout_error_text(model: str, timeout_seconds: float) -> str:
    """Build the error message shown when the CLI times out."""
    minutes = int(timeout_seconds / 60)
    return fmt(
        t("timeout.error_header"),
        SEP,
        t("timeout.error_body", model=model, minutes=minutes),
    )


def new_session_text(provider: str) -> str:
    """Build /new response for provider-local reset."""
    provider_label = {
        "claude": "Claude",
        "codex": "Codex",
        "gemini": "Gemini",
        "grok": "Grok Build",
    }.get(provider.lower(), provider)
    return fmt(
        t("session.reset_header"),
        SEP,
        t("session.reset_body", provider=provider_label),
    )


def stop_text(killed: bool, provider: str, discarded: int = 0) -> str:
    """Build the /stop response; *discarded* is the number of queued messages that were dropped."""
    body = t("stop.killed", provider=provider) if killed else t("stop.nothing")
    if discarded:
        body = f"{body}\n{t('stop.discarded', count=discarded)}"
    return fmt(t("stop.header"), SEP, body)


# -- Startup lifecycle messages --


def startup_notification_text(kind: str) -> str:
    """Notification text for startup events.

    Only ``first_start`` and ``system_reboot`` produce output.
    ``service_restart`` is silent (handled by the existing sentinel system).
    """
    if kind == "first_start":
        return fmt(t("startup.first_start_header"), SEP, t("startup.first_start_body"))
    if kind == "system_reboot":
        return fmt(t("startup.reboot_header"), SEP, t("startup.reboot_body"))
    return ""


# -- Auto-recovery messages --


def format_technical_footer(model_name: str, duration_ms: float | None) -> str:
    """Format the model and the duration as a footer line.

    The previous token/cost tracking was wrong: it added the CLI's cumulative session cost on
    every turn and left cache tokens out of the token count. An accurate rework was judged not
    worth it, so the tracking was removed.
    """
    parts = [t("footer.model", name=model_name)]
    if duration_ms is not None:
        secs = duration_ms / 1000
        parts.append(t("footer.time", secs=f"{secs:.1f}"))
    return "\n---\n" + " | ".join(parts)


def recovery_notification_text(
    kind: str,
    prompt_preview: str,
    session_name: str = "",
) -> str:
    """Notification that interrupted work is being recovered."""
    preview = prompt_preview[:80] + ("…" if len(prompt_preview) > 80 else "")
    if kind == "named_session":
        return fmt(
            t("recovery.named_header"),
            SEP,
            t("recovery.named_body", session=session_name, preview=preview),
        )
    return fmt(
        t("recovery.interrupted_header"),
        SEP,
        t("recovery.interrupted_body", preview=preview),
    )

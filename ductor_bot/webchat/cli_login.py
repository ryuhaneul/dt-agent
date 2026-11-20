"""Memory-only, owner-scoped CLI login jobs. Never log terminal output or input."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import hashlib
import json
import os
import pty
import re
import signal
import sys
import termios
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from ductor_bot.cli.auth import AuthResult, AuthStatus, check_provider_auth
from ductor_bot.cli.claude_accounts import container_slot_config_dir
from ductor_bot.webchat.cli_login_slots import (
    ClaudeLoginSlots,
    PreparedClaudeSlot,
)

Provider = Literal["claude", "codex", "grok"]
JobState = Literal["running", "succeeded", "failed", "cancelled", "timed_out"]
PROVIDERS: tuple[Provider, ...] = ("claude", "codex", "grok")
_COMMANDS = {
    "claude": ("claude", "auth", "login"),
    "codex": ("codex", "login", "--device-auth"),
    "grok": ("grok", "login", "--device-auth"),
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CODE = re.compile(
    r"(?:one-time code[^\n]*|Confirm this code in your browser:)\s*\n\s*([^\s│]+)", re.IGNORECASE
)
_URL = re.compile(r"https://[^\s<>\"'│]+")
_LOGIN_HOSTS = {"claude": "claude.ai", "codex": "auth.openai.com", "grok": "accounts.x.ai"}
_LIMIT = 64 * 1024
_PROBE = """import json, sys
from ductor_bot.cli.auth import check_provider_auth
print(json.dumps({"status": check_provider_auth(sys.argv[1]).status.value}))
"""


class CliLoginError(Exception):
    """HTTP-independent error for the bridge to map without exposing terminal data."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class _TerminalText:
    """Incremental escape removal, including OSC hyperlinks and split sequences."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._state = "text"

    def feed(self, data: bytes) -> str:  # noqa: C901 -- terminal escape state machine
        result: list[str] = []
        for char in self._decoder.decode(data):
            if self._state == "esc":
                self._state = {"[": "csi", "]": "osc", "P": "osc", "^": "osc", "_": "osc"}.get(
                    char, "text"
                )
            elif self._state == "csi":
                if "@" <= char <= "~":
                    self._state = "text"
            elif self._state == "osc":
                if char == "\x07":
                    self._state = "text"
                elif char == "\x1b":
                    self._state = "osc_end"
            elif self._state == "osc_end":
                self._state = "text" if char == "\\" else "osc"
            elif char == "\x1b":
                self._state = "esc"
            elif char in "\n\t" or not _CONTROL.match(char):
                result.append(char)
        return "".join(result)


def parse_output(provider: str, text: str, *, inputs: Collection[str] = ()) -> dict[str, object]:
    """Extract display hints; unknown prompts still permit raw line input."""
    # Terminal-width wraps can split an OAuth URL. Continue only nonblank,
    # unindented URL-looking lines; never join the next prose/prompt line.
    joined = re.sub(r"(?<=[\w%=&?./~+-])\n(?=[\w%=&?./~+-]+(?:\n|$))", "", text)
    urls = list(dict.fromkeys(url.rstrip(".,)") for url in _URL.findall(joined)))
    allowed_urls = []
    for url in urls:
        with contextlib.suppress(ValueError):
            parsed = urlsplit(url)
            if (
                parsed.hostname == _LOGIN_HOSTS.get(provider)
                and parsed.username is None
                and "\\" not in url
                and not any(url in value for value in inputs)
            ):
                allowed_urls.append(url)
    code = _CODE.search(text) if provider in {"codex", "grok"} else None
    needs_input = re.search(r"Paste code", text, re.IGNORECASE) is not None
    return {
        "urls": allowed_urls,
        "code": code.group(1) if code else None,
        "needs_input": needs_input,
    }


def _fingerprint(paths: tuple[Path, ...]) -> tuple[bytes | None, ...]:
    result: list[bytes | None] = []
    for path in paths:
        try:
            result.append(hashlib.sha256(path.read_bytes()).digest())
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


def _child_terminal() -> None:
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


@dataclass
class _Job:
    id: str
    provider: Provider
    owner: str = field(repr=False)
    slot: str | None = None
    new_slot: bool = False
    prepared_slot: PreparedClaudeSlot | None = field(default=None, repr=False)
    state: JobState = "running"
    reason: str | None = None
    started: float = 0
    finished: float | None = None
    output: deque[tuple[int, str]] = field(default_factory=deque, repr=False)
    output_bytes: int = 0
    seq: int = 0
    # Completion signals must survive ring-buffer eviction.
    success_signal: bool = False
    submitted_inputs: set[str] = field(default_factory=set, repr=False)
    input_echoes: deque[str] = field(default_factory=deque, repr=False)
    signal_text: str = field(default="", repr=False)
    signal_pending: str = field(default="", repr=False)
    parser: _TerminalText = field(default_factory=_TerminalText, repr=False)
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    master: int | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    stop_state: Literal["cancelled", "timed_out"] | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    decision_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def append(self, data: bytes) -> None:
        text = self.parser.feed(data)
        if not text:
            return
        self.seq += 1
        encoded = text.encode()
        if len(encoded) > _LIMIT:
            text = encoded[-_LIMIT:].decode("utf-8", "ignore")
        self.output.append((self.seq, text))
        self.output_bytes += len(text.encode())
        while self.output_bytes > _LIMIT:
            seq, oldest = self.output.popleft()
            excess = self.output_bytes - _LIMIT
            retained = oldest.encode()[excess:].decode("utf-8", "ignore")
            self.output_bytes -= len(oldest.encode())
            if retained:
                self.output.appendleft((seq, retained))
                self.output_bytes += len(retained.encode())
        # Keep PTY/readline input echoes visible, but out of completion detection.
        self.signal_pending += text
        clean = ""
        while self.input_echoes:
            match = min(
                (
                    (self.signal_pending.find(echo), echo)
                    for echo in self.input_echoes
                    if echo in self.signal_pending
                ),
                key=lambda item: (item[0], -len(item[1])),
                default=None,
            )
            if match is not None:
                index, echo = match
                clean += self.signal_pending[:index]
                self.signal_pending = self.signal_pending[index + len(echo) :]
                self.input_echoes.remove(echo)
                continue
            # An echo may be split at any byte boundary by the PTY/readline.
            held = next(
                (
                    n
                    for n in range(
                        min(max(map(len, self.input_echoes)), len(self.signal_pending)), 0, -1
                    )
                    if any(
                        self.signal_pending.endswith(echo[:n]) and n <= len(echo)
                        for echo in self.input_echoes
                    )
                ),
                0,
            )
            end = len(self.signal_pending) - held
            clean += self.signal_pending[:end]
            self.signal_pending = self.signal_pending[end:]
            break
        else:
            clean += self.signal_pending
            self.signal_pending = ""
        combined = self.signal_text + clean
        self.signal_text = combined[-128:]
        marker = {"claude": "Login successful", "codex": "Successfully logged in"}.get(
            self.provider
        )
        if marker and marker in combined:
            self.success_signal = True


class CliLoginManager:
    """One global job per bot, with nonblocking bridge-facing methods.

    ``start``, ``get``, ``send_input``, ``cancel`` and ``status`` never await a
    process or an auth probe. ``close`` is awaited during bot shutdown only.
    Authentication is cached and refreshed in worker threads.
    """

    def __init__(
        self,
        *,
        startup_providers: frozenset[str],
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        auth_checker: Callable[[str], AuthResult] = check_provider_auth,
        slots: ClaudeLoginSlots | None = None,
    ) -> None:
        self._startup_providers = startup_providers
        self._env = dict(os.environ if env is None else env)
        self._clock = clock
        self._auth_checker = auth_checker
        self._slots = slots
        self._jobs: dict[str, _Job] = {}
        self._active: str | None = None
        self._closed = False
        self._auth: dict[str, AuthResult] = {}
        self._refresh_task: asyncio.Task[None] | None = None
        self._auth_pending: list[str] = []
        self._auth_invalidated: set[str] = set()
        self._probe_process: asyncio.subprocess.Process | None = None
        self._expiry: dict[str, asyncio.TimerHandle] = {}

    def start(
        self, provider: str, owner: str, *, slot: str | None = None, new_slot: bool = False
    ) -> str:
        """Reserve the singleton immediately and schedule fixed-argv PTY startup."""
        self._purge()
        if self._closed:
            raise CliLoginError(409, "login_closed")
        if self._active is not None:
            raise CliLoginError(409, "login_running")
        if provider not in PROVIDERS or not owner:
            raise CliLoginError(400, "invalid_login")
        if provider == "claude":
            if slot in {"api", "claude-api"}:
                raise CliLoginError(400, "api_key_slot")
            if self._slots is not None:
                try:
                    slot = self._slots.resolve_oauth_slot(slot)
                except ValueError as exc:
                    code = "api_key_slot" if str(exc) == "api_key_slot" else "invalid_slot"
                    raise CliLoginError(400, code) from None
            elif (
                new_slot
                or not slot
                or not re.fullmatch(r"claude(?:-[a-z0-9][a-z0-9_-]*)?", slot)
                or slot == "claude-slots"
            ):
                raise CliLoginError(400, "invalid_slot")
        elif slot is not None or new_slot:
            raise CliLoginError(400, "invalid_slot")
        job = _Job(
            uuid.uuid4().hex,
            PROVIDERS[PROVIDERS.index(provider)],
            owner,
            slot=slot,
            new_slot=new_slot,
            started=self._clock(),
        )
        self._schedule(job)
        return job.id

    def _schedule(self, job: _Job) -> None:
        with contextlib.ExitStack() as reservation:
            if job.provider == "claude" and self._slots is not None:
                assert job.slot is not None
                if not reservation.enter_context(self._slots.reserve(job.slot)):
                    raise CliLoginError(409, "slot_busy")
            self._jobs[job.id] = job
            self._active = job.id
            job.task = asyncio.create_task(self._run(job), name="cli-login")
            held = reservation.pop_all()
            job.task.add_done_callback(lambda _task: held.close())

    def get(self, job_id: str, owner: str, *, after: int = 0) -> dict[str, object]:
        job = self._owned(job_id, owner)
        if after < 0:
            raise CliLoginError(400, "invalid_sequence")
        text = "".join(chunk for _, chunk in job.output)
        state: JobState = job.state if job.finished is not None else "running"
        return {
            "job_id": job.id,
            "provider": job.provider,
            "slot": job.slot,
            "state": state,
            "reason": job.reason if job.finished is not None else None,
            "seq": job.seq,
            "output": [{"seq": seq, "text": chunk} for seq, chunk in job.output if seq > after],
            **parse_output(job.provider, text, inputs=job.submitted_inputs),
            "restart_required": state == "succeeded"
            and job.provider not in self._startup_providers,
        }

    def status(
        self, owner: str, *, active_owners: frozenset[str] | None = None
    ) -> dict[str, object]:
        """Return cached auth and owner-filtered active identity; schedule refresh."""
        self._purge()
        self.refresh_auth()
        active = self._jobs.get(self._active or "")
        if active and active_owners is not None and active.owner not in active_owners:
            active.owner = owner
        return {
            "providers": {
                p: self._auth[p].status.value
                if p in self._auth and p not in self._auth_invalidated
                else None
                for p in PROVIDERS
            },
            "running": active is not None,
            "provider": active.provider if active else None,
            "job_id": active.id if active and active.owner == owner else None,
        }

    def refresh_auth(self, provider: str | None = None) -> None:
        """Queue completion checks even if a slower status refresh is in flight."""
        if self._closed:
            return
        idle = self._refresh_task is None or self._refresh_task.done()
        if provider is not None:
            if provider not in PROVIDERS:
                raise CliLoginError(400, "invalid_login")
            self._auth_invalidated.add(provider)
            if provider not in self._auth_pending:
                self._auth_pending.append(provider)
        elif idle:
            self._auth_pending = list(PROVIDERS)
        if idle:
            self._refresh_task = asyncio.create_task(self._refresh_auth(), name="cli-login-auth")

    async def _refresh_auth(self) -> None:
        while self._auth_pending and not self._closed:
            provider = self._auth_pending.pop(0)
            # Probe exceptions can contain credentials; never log their details.
            with contextlib.suppress(Exception):
                if self._auth_checker is check_provider_auth:
                    try:
                        self._auth[provider] = await self._probe_auth(provider)
                    except Exception:
                        self._auth.pop(provider, None)
                        raise
                else:
                    self._auth[provider] = await asyncio.to_thread(self._auth_checker, provider)
            if provider not in self._auth_pending:
                self._auth_invalidated.discard(provider)

    async def _probe_auth(self, provider: str) -> AuthResult:
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _PROBE,
                provider,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        )
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            process = await spawning
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            raise
        self._probe_process = process
        try:
            output, _ = await process.communicate()
            if process.returncode != 0:
                raise ValueError("auth_probe_failed")
            data = json.loads(output)
            return AuthResult(provider, AuthStatus(data["status"]))
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            self._probe_process = None

    def cancel_owner(self, owner: str) -> None:
        """Cancel jobs belonging to sessions being revoked."""
        for job in list(self._jobs.values()):
            if job.owner == owner and job.finished is None:
                self.cancel(job.id, owner)

    def send_input(self, job_id: str, owner: str, kind: str, value: str) -> None:
        job = self._owned(job_id, owner)
        original = value
        if kind == "line":
            value = _CONTROL.sub("", value)
            if not 1 <= len(value) <= 4096:
                raise CliLoginError(400, "invalid_input")
            payload = (value + "\r").encode()
        else:
            raise CliLoginError(400, "invalid_input")
        if job.state != "running" or job.stop_state is not None or job.master is None:
            raise CliLoginError(409, "input_unavailable")
        try:
            job.submitted_inputs.update((original, value))
            job.input_echoes.append(value)
            # Nonblocking writes; the PTY is drained on this same event loop.
            if os.write(job.master, payload) != len(payload):
                raise CliLoginError(409, "input_unavailable")
        except OSError:
            raise CliLoginError(409, "input_unavailable") from None

    def cancel(self, job_id: str, owner: str) -> None:
        job = self._owned(job_id, owner)
        with job.decision_lock:
            if job.state == "running":
                job.stop_state = "cancelled"
                job.stop.set()

    async def close(self) -> None:
        """Stop only owned process groups and reap leaders."""
        self._closed = True
        self._auth_pending.clear()
        if self._refresh_task:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
        for handle in self._expiry.values():
            handle.cancel()
        for job in self._jobs.values():
            with job.decision_lock:
                if job.state == "running":
                    job.stop_state = "cancelled"
                    job.stop.set()
        await asyncio.gather(
            *(j.task for j in self._jobs.values() if j.task), return_exceptions=True
        )
        self._jobs.clear()
        self._expiry.clear()

    def _owned(self, job_id: str, owner: str) -> _Job:
        self._purge()
        job = self._jobs.get(job_id)
        if job is None or job.owner != owner:
            raise CliLoginError(404, "login_not_found")
        return job

    def _expire(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)
        handle = self._expiry.pop(job_id, None)
        if handle:
            handle.cancel()

    def _purge(self) -> None:
        for job_id, job in list(self._jobs.items()):
            if job.finished is not None and self._clock() - job.finished >= 600:
                self._expire(job_id)

    def _credentials(self, job: _Job, env: Mapping[str, str]) -> tuple[Path, ...]:
        home = Path(env.get("HOME", str(Path.home())))
        if job.provider == "claude":
            folder = Path(env["CLAUDE_CONFIG_DIR"])
            return (folder / ".credentials.json", folder / "credentials.json")
        key = f"{job.provider.upper()}_HOME"
        return (Path(env.get(key, str(home / f".{job.provider}"))) / "auth.json",)

    def _environment(self, job: _Job) -> dict[str, str]:
        env = self._env.copy()
        if job.provider == "claude":
            assert job.slot is not None
            env["CLAUDE_CONFIG_DIR"] = container_slot_config_dir(job.slot)
        return env

    async def _run(self, job: _Job) -> None:  # noqa: C901, PLR0912, PLR0915
        slave: int | None = None
        loop = asyncio.get_running_loop()
        try:
            if job.provider == "claude" and self._slots is not None:
                assert job.slot is not None
                job.prepared_slot = await asyncio.to_thread(
                    self._slots.prepare, job.slot, new_slot=job.new_slot
                )
            if job.stop.is_set():
                job.state = job.stop_state or "cancelled"
                job.reason = job.state
                return
            if self._clock() - job.started >= 600:
                job.state = "timed_out"
                job.reason = "timed_out"
                return
            env = self._environment(job)
            paths = self._credentials(job, env)
            before = _fingerprint(paths)
            master, slave = pty.openpty()
            job.master = master
            os.set_blocking(master, False)
            attrs = termios.tcgetattr(slave)
            attrs[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            job.process = await asyncio.create_subprocess_exec(
                *_COMMANDS[job.provider],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                preexec_fn=_child_terminal,
            )
            os.close(slave)
            slave = None
            loop.add_reader(master, self._read, job)
            while True:
                if job.stop_state is not None:
                    job.state = job.stop_state
                    job.reason = job.stop_state
                    break
                if self._clock() - job.started >= 600:
                    job.stop_state = "timed_out"
                    job.state = "timed_out"
                    job.reason = "timed_out"
                    break
                self._read(job)
                rc = job.process.returncode
                current = _fingerprint(paths)
                changed = any(
                    now is not None and now != old for now, old in zip(current, before, strict=True)
                )
                if rc is not None:
                    self._read(job)
                    success = changed and rc == 0 and (job.provider == "grok" or job.success_signal)
                    job.state = "succeeded" if success else "failed"
                    job.reason = None if success else "login_incomplete"
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(job.stop.wait(), timeout=0.05)
        except Exception:
            if job.stop_state is not None:
                job.state = job.stop_state
                job.reason = job.stop_state
            elif job.state != "timed_out":
                job.state = "failed"
                job.reason = "login_failed"
        finally:
            if slave is not None:
                os.close(slave)
            await self._terminate(job)
            if job.master is not None:
                loop.remove_reader(job.master)
                os.close(job.master)
                job.master = None
            if self._slots is not None and job.prepared_slot is not None:
                try:
                    await asyncio.to_thread(
                        self._slots.finish, job.prepared_slot, succeeded=job.state == "succeeded"
                    )
                except Exception:
                    job.state = "failed"
                    job.reason = "slot_finalize_failed"
            job.finished = self._clock()
            if self._active == job.id:
                self._active = None
            if not self._closed:
                self._expiry[job.id] = loop.call_later(600, self._expire, job.id)
            self.refresh_auth(job.provider)

    @staticmethod
    def _read(job: _Job) -> None:
        assert job.master is not None
        for _ in range(16):
            try:
                data = os.read(job.master, 16384)
            except BlockingIOError:
                return
            except OSError:
                asyncio.get_running_loop().remove_reader(job.master)
                return
            if not data:
                asyncio.get_running_loop().remove_reader(job.master)
                return
            job.append(data)

    @staticmethod
    async def _terminate(job: _Job) -> int | None:
        process = job.process
        if process is None:
            return None
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        # Waiting for the leader alone can miss its descendants.
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        returncode = await process.wait()
        job.process = None
        return returncode

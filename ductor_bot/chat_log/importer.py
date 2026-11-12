"""Import foreground and named Claude/Codex transcripts into chat history."""

from __future__ import annotations

import heapq
import json
import logging
import os
import re
import unicodedata
from bisect import bisect_left
from collections import defaultdict, deque
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ductor_bot.chat_log.history import Entry, HistoryStore
from ductor_bot.chat_log.ids import history_filename, to_tid
from ductor_bot.config import AgentConfig
from ductor_bot.heartbeat.models import HeartbeatStatus
from ductor_bot.infra.atomic_io import atomic_text_save
from ductor_bot.infra.file_lock import exclusive_file_lock
from ductor_bot.messenger.queue_context import without_queue_context
from ductor_bot.orchestrator import hooks
from ductor_bot.orchestrator.flows import _classify_heartbeat_response
from ductor_bot.session.key import SessionKey
from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)
# Whitespace-folded separator prepended by flows._session_age_note().
_SESSION_AGE_SEPARATOR = " --- "
_NAMED_TAG = re.compile(r"^\*\*\[[^\]\r\n]+ \| (?:claude|codex|gemini)\]\*\*\n")
# build_interagent_injection_prompt() uses lowercase "from" before the sender.
_INJECTION = re.compile(
    r"^\[(?:[A-Z][A-Z0-9 _-]{2,}(?::|\])"
    r"|ASYNC INTER-AGENT (?:RESPONSE|FAILURE|RESULT LOST) from ')"
)


@dataclass(frozen=True, slots=True)
class ImportFile:
    path: Path
    provider: str
    session_id: str
    tid: str
    entries: tuple[Entry, ...]
    live_refs: tuple[tuple[str, str, int], ...] = ()
    candidates: tuple[Entry, ...] = ()


def _json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open("rb") as stream:
        for line_no, line in enumerate(stream, 1):
            try:
                row = json.loads(line.decode("utf-8"))
            except ValueError:
                logger.warning("Skipping corrupt transcript line %d", line_no)
                continue
            if isinstance(row, dict):
                yield line_no, row


def _mappings(paths: DuctorPaths) -> dict[tuple[str, str], str]:
    mappings = {}
    for raw_key, data in _json_object(paths.sessions_path).items():
        key = SessionKey.parse(raw_key)
        if key.transport != "tg":
            continue
        for provider, bucket in data.get("provider_sessions", {}).items():
            if provider in {"claude", "codex"} and bucket.get("session_id"):
                mappings[(provider, bucket["session_id"])] = to_tid(key)
    named = _json_object(paths.named_sessions_path)
    for data in named.get("sessions", []):
        provider = data.get("provider")
        if data.get("transport", "tg") != "tg" or provider not in {"claude", "codex"}:
            continue
        if data.get("session_id"):
            key = SessionKey.telegram(data["chat_id"], data.get("topic_id"))
            mappings.setdefault((provider, data["session_id"]), to_tid(key))
    return mappings


class TranscriptImporter:
    def __init__(self, paths: DuctorPaths, config: AgentConfig) -> None:
        self.paths = paths
        self.config = config
        self.history = HistoryStore(paths.chat_log_dir / "history")
        self.state_path = paths.chat_log_dir / "import_refs.json"
        self._suffixes = [
            value.suffix for value in vars(hooks).values() if isinstance(value, hooks.MessageHook)
        ] + [config.memory_reflection.prompt]
        self._heartbeats = {config.heartbeat.prompt: config.heartbeat.ack_token}
        self._heartbeats.update(
            {
                target.prompt or config.heartbeat.prompt: target.ack_token
                or config.heartbeat.ack_token
                for target in config.heartbeat.group_targets
            }
        )

    def _clean_user(self, text: str) -> tuple[str | None, str | None]:
        text = without_queue_context(text)
        # Hooks are appended in registration order, sometimes several at once.
        changed = True
        while changed:
            changed = False
            for suffix in self._suffixes:
                if suffix and text.endswith("\n\n" + suffix):
                    text = text[: -len("\n\n" + suffix)]
                    changed = True
        text = text.strip()
        ack = self._heartbeats.get(text)
        if not text or ack is not None or _INJECTION.match(text) or text.startswith("<"):
            return None, ack
        return text, None

    def _entry(  # noqa: PLR0913, PLR0917 -- source identity and message metadata
        self, provider: str, sid: str, line: int, row: dict[str, Any], role: str, text: str
    ) -> Entry:
        return Entry(
            role="user" if role == "user" else "bot",
            source="import",
            text=text,
            imported=True,
            import_ref={"provider": provider, "session_id": sid, "line": line},
            ts=row.get("timestamp"),
        )

    def _claude(self, path: Path, sid: str) -> list[Entry]:  # noqa: C901 -- transcript state machine
        entries: list[Entry] = []
        response: list[str] = []
        response_line = 0
        response_row: dict[str, Any] = {}
        ack: str | None = None

        def finish() -> None:
            text = "\n\n".join(response)
            if text and (
                ack is None
                or _classify_heartbeat_response(text, ack).status is not HeartbeatStatus.ACK
            ):
                entries.append(self._entry("claude", sid, response_line, response_row, "bot", text))
            response.clear()

        for line, row in _rows(path):
            if row.get("isSidechain"):
                continue
            content = row.get("message", {}).get("content")
            if row.get("type") == "user" and isinstance(content, str):
                finish()
                text, ack = self._clean_user(content)
                if text is not None:
                    entries.append(self._entry("claude", sid, line, row, "user", text))
            elif row.get("type") == "assistant" and isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_use":
                        response.clear()
                    elif block.get("type") == "text" and block.get("text"):
                        response.append(block["text"])
                        response_line, response_row = line, row
        finish()
        return entries

    def _codex(self, path: Path, sid: str) -> list[Entry]:
        entries = []
        ack: str | None = None
        for line, row in _rows(path):
            if row.get("type") != "event_msg":
                continue
            payload = row.get("payload", {})
            if payload.get("type") == "user_message" and isinstance(payload.get("message"), str):
                text, ack = self._clean_user(payload["message"])
                if text is not None:
                    entries.append(self._entry("codex", sid, line, row, "user", text))
            elif payload.get("type") == "task_complete":
                text = payload.get("last_agent_message")
                if (
                    isinstance(text, str)
                    and text
                    and (
                        ack is None
                        or _classify_heartbeat_response(text, ack).status is not HeartbeatStatus.ACK
                    )
                ):
                    entries.append(self._entry("codex", sid, line, row, "bot", text))
        return entries

    def _sources(self) -> Iterator[tuple[Path, str, str, str]]:
        mappings = _mappings(self.paths)
        auth = self.paths.ductor_home / ".cli-auth"
        encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(self.paths.workspace))
        seen: set[tuple[str, str]] = set()
        for root in (auth / "_shared-projects", auth / "claude" / "projects"):
            for path in sorted(root.glob("*/*.jsonl")):
                identity = ("claude", path.stem)
                tid = mappings.get(identity)
                if identity in seen or (tid is None and path.parent.name != encoded):
                    continue
                seen.add(identity)
                yield path, "claude", path.stem, tid or "unknown"
        for path in sorted((auth / "codex" / "sessions").glob("**/rollout-*.jsonl")):
            meta: dict[str, Any] = next(
                (
                    row.get("payload", {})
                    for _, row in _rows(path)
                    if row.get("type") == "session_meta"
                ),
                {},
            )
            sid = meta.get("id")
            if not isinstance(sid, str) or not sid or ("codex", sid) in seen:
                continue
            tid = mappings.get(("codex", sid))
            if tid is None and meta.get("cwd") != str(self.paths.workspace):
                continue
            seen.add(("codex", sid))
            yield path, "codex", sid, tid or "unknown"

    def _saved_refs(self) -> set[tuple[str, str, int]]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except ValueError as exc:
            logger.warning("Ignoring invalid import reference ledger: %s", type(exc).__name__)
            return set()
        if not isinstance(data, list) or any(
            not isinstance(ref, list)
            or len(ref) != 3
            or not isinstance(ref[0], str)
            or not isinstance(ref[1], str)
            or type(ref[2]) is not int
            for ref in data
        ):
            logger.warning("Ignoring invalid import reference ledger: invalid format")
            return set()
        return {(provider, sid, line) for provider, sid, line in data}

    def _eligible(
        self, entries: list[Entry], existing: list[Entry], live_refs: set[tuple[str, str, int]]
    ) -> Iterator[tuple[datetime, Entry]]:
        days = self.config.chat_log.retention_days
        cutoff = datetime.now(UTC) - timedelta(days=days) if days else None
        for effective, entry in _ordered(entries, existing):
            if (
                cutoff is not None
                and effective != datetime.min.replace(tzinfo=UTC)
                and effective < cutoff
            ):
                continue
            if _ref(entry) not in live_refs:
                yield effective, entry

    def plan(self) -> list[ImportFile]:
        existing = {
            tid: self.history._read(self.history.directory / history_filename(tid))
            for tid in self.history.tids()
        }
        live_indexes = {tid: _LiveIndex(entries) for tid, entries in existing.items()}
        existing_refs = {
            _ref(entry)
            for entries in existing.values()
            for entry in entries
            if entry.import_ref is not None
        }
        refs = self._saved_refs() | existing_refs
        sources = []
        candidates: dict[str, list[Entry]] = defaultdict(list)
        for path, provider, sid, tid in self._sources():
            parsed = self._claude(path, sid) if provider == "claude" else self._codex(path, sid)
            sources.append((path, provider, sid, tid, parsed))
            candidates[tid].extend(entry for entry in parsed if _ref(entry) not in existing_refs)
        matched_refs = {
            tid: {_ref(candidates[tid][i]) for i in index.matches(candidates[tid])}
            for tid, index in live_indexes.items()
        }
        result = []
        for path, provider, sid, tid, parsed in sources:
            live_refs = tuple(
                _ref(entry) for entry in parsed if _ref(entry) in matched_refs.get(tid, set())
            )
            entries = []
            for _, entry in self._eligible(parsed, existing.get(tid, []), set(live_refs)):
                ref = _ref(entry)
                if ref not in refs:
                    entries.append(entry)
                    refs.add(ref)
            result.append(
                ImportFile(path, provider, sid, tid, tuple(entries), live_refs, tuple(parsed))
            )
        return result

    def run(self, *, dry_run: bool = False) -> list[ImportFile]:
        if dry_run:
            return self.plan()
        with exclusive_file_lock(self.state_path):
            return self._run()

    def _run(self) -> list[ImportFile]:
        files = self.plan()
        consumed = self._saved_refs()
        consumed.update(ref for file in files for ref in file.live_refs)
        # Adopt references written by earlier versions before any retention pass.
        for tid in self.history.tids():
            consumed.update(
                _ref(entry)
                for entry in self.history._read(self.history._path(tid))
                if entry.import_ref is not None
            )
        atomic_text_save(self.state_path, json.dumps(sorted(consumed)) + "\n")
        grouped: dict[str, list[Entry]] = defaultdict(list)
        candidates: dict[str, list[Entry]] = defaultdict(list)
        for file in files:
            grouped[file.tid].extend(file.entries)
            candidates[file.tid].extend(file.candidates or file.entries)
        saved_refs = set()
        for tid, entries in grouped.items():
            if not entries:
                continue
            path = self.history.directory / history_filename(tid)
            with exclusive_file_lock(path):
                existing = self.history._read(path)
                existing_refs = {_ref(entry) for entry in existing if entry.import_ref is not None}
                refs = consumed | existing_refs
                seq = max((entry.seq for entry in existing if entry.imported), default=-1_000_000)
                # Recheck live duplicates under the recorder lock. Stable sorting
                # keeps undated entries directly after their timestamp anchor.
                live_index = _LiveIndex(existing)
                # Ledger-only lines still reserve their earlier live matches;
                # stored imports must not consume newly recorded live entries.
                match_candidates = [
                    entry for entry in candidates[tid] if _ref(entry) not in existing_refs
                ]
                live_refs = {
                    _ref(match_candidates[i]) for i in live_index.matches(match_candidates)
                }
                timed = sorted(
                    self._eligible(candidates[tid], existing, live_refs), key=lambda item: item[0]
                )
                planned_refs = {_ref(entry) for entry in entries}
                pending = [
                    entry
                    for _, entry in timed
                    if _ref(entry) in planned_refs and _ref(entry) not in refs
                ]
                if seq + len(pending) >= 0:
                    raise ValueError("Imported history exceeds the negative sequence range")
                with path.open("ab+") as stream:
                    if stream.tell():
                        stream.seek(-1, os.SEEK_END)
                        if stream.read(1) != b"\n":
                            stream.write(b"\n")
                    for entry in pending:
                        seq += 1
                        stream.write(
                            (
                                json.dumps(asdict(replace(entry, seq=seq)), ensure_ascii=False)
                                + "\n"
                            ).encode("utf-8")
                        )
                        saved_refs.add(_ref(entry))
                    stream.flush()
                    os.fsync(stream.fileno())
                consumed.update(_ref(entry) for entry in pending)
                consumed.update(live_refs)
                atomic_text_save(self.state_path, json.dumps(sorted(consumed)) + "\n")
        return [
            replace(
                file, entries=tuple(entry for entry in file.entries if _ref(entry) in saved_refs)
            )
            for file in files
        ]


def _ref(entry: Entry) -> tuple[str, str, int]:
    ref = entry.import_ref or {}
    return str(ref.get("provider", "")), str(ref.get("session_id", "")), int(ref.get("line", 0))


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _ordered(entries: list[Entry], existing: list[Entry]) -> Iterator[tuple[datetime, Entry]]:
    previous: dict[tuple[str, str], datetime | None] = {}
    effective_times = {}
    # Already imported lines can still anchor newly appended undated lines.
    anchored = [entry for entry in existing if entry.import_ref is not None] + entries
    for entry in sorted(anchored, key=_ref):
        provider, sid, _ = _ref(entry)
        identity = (provider, sid)
        ts = _time(entry.ts)
        if ts is not None:
            previous[identity] = ts
        effective_times[_ref(entry)] = previous.get(identity)
    for entry in entries:
        effective = effective_times[_ref(entry)]
        yield effective or datetime.min.replace(tzinfo=UTC), entry


@dataclass(slots=True)
class _MatchNode:
    stamp: datetime
    transcript: bool
    ordinals: deque[int]
    previous: int | None = None
    following: int | None = None
    active: bool = True


class _LiveIndex:
    def __init__(self, entries: list[Entry]) -> None:
        self.live: dict[tuple[str, str], list[tuple[datetime | None, int]]] = defaultdict(list)
        for ordinal, entry in enumerate(entries):
            if not entry.imported:
                self.live[self._key(entry, live=True)].append((_time(entry.ts), ordinal))
        self.bot_bodies = sorted(body for role, body in self.live if role == "bot")
        self.buckets: dict[tuple[str, str], list[tuple[datetime | None, int]]] = {}
        self.nodes: list[_MatchNode] = []
        self.heap: list[tuple[timedelta, int, int, int, int]] = []
        self.used: set[int] = set()

    @staticmethod
    def _key(entry: Entry, *, live: bool = False) -> tuple[str, str]:
        text = (
            _NAMED_TAG.sub("", entry.text, count=1) if live and entry.role == "bot" else entry.text
        )
        return entry.role, unicodedata.normalize("NFC", " ".join(text.split()))

    def _bucket(self, key: tuple[str, str]) -> list[tuple[datetime | None, int]]:
        if key not in self.buckets:
            role, body = key
            if role != "bot":
                self.buckets[key] = self.live.get(key, [])
            else:
                candidates = list(self.live.get(key, [])) if body else []
                prefix = body + _SESSION_AGE_SEPARATOR
                pos = bisect_left(self.bot_bodies, prefix)
                while (
                    body and pos < len(self.bot_bodies) and self.bot_bodies[pos].startswith(prefix)
                ):
                    candidates.extend(self.live[(role, self.bot_bodies[pos])])
                    pos += 1
                self.buckets[key] = sorted(candidates, key=lambda item: item[1])
        return self.buckets[key]

    def _push(self, left: int | None, right: int | None) -> None:
        if left is None or right is None:
            return
        first, second = self.nodes[left], self.nodes[right]
        if not first.ordinals or not second.ordinals or first.transcript == second.transcript:
            return
        distance = second.stamp - first.stamp
        if distance > timedelta(minutes=10):
            return
        transcript, live = (first, second) if first.transcript else (second, first)
        heapq.heappush(self.heap, (distance, transcript.ordinals[0], live.ordinals[0], left, right))

    def _refresh(self, index: int) -> None:
        node = self.nodes[index]
        if not node.active:
            return
        if not node.transcript:
            while node.ordinals and node.ordinals[0] in self.used:
                node.ordinals.popleft()
        if node.ordinals:
            self._push(node.previous, index)
            self._push(index, node.following)
        else:
            node.active = False
            if node.previous is not None:
                self.nodes[node.previous].following = node.following
            if node.following is not None:
                self.nodes[node.following].previous = node.previous
            self._push(node.previous, node.following)

    def _timeline(
        self, transcripts: list[tuple[datetime | None, int]], key: tuple[str, str]
    ) -> None:
        groups: dict[tuple[datetime, bool], deque[int]] = defaultdict(deque)
        for is_transcript, entries in ((True, transcripts), (False, self._bucket(key))):
            for stamp, ordinal in entries:
                if stamp is not None:
                    groups[(stamp, is_transcript)].append(ordinal)
        previous = None
        for (stamp, is_transcript), ordinals in sorted(groups.items()):
            index = len(self.nodes)
            self.nodes.append(_MatchNode(stamp, is_transcript, ordinals, previous))
            if previous is not None:
                self.nodes[previous].following = index
                self._push(previous, index)
            previous = index

    def _dated(self) -> set[int]:
        matched = set()
        while self.heap:
            _, transcript_ordinal, live_ordinal, left, right = heapq.heappop(self.heap)
            first, second = self.nodes[left], self.nodes[right]
            if not first.active or not second.active or first.following != right:
                continue
            ti, li = (left, right) if first.transcript else (right, left)
            transcript, live = self.nodes[ti], self.nodes[li]
            if live.ordinals[0] in self.used:
                self._refresh(li)
                continue
            if transcript.ordinals[0] != transcript_ordinal or live.ordinals[0] != live_ordinal:
                continue
            matched.add(transcript.ordinals.popleft())
            self.used.add(live_ordinal)
            self._refresh(ti)
            self._refresh(li)
        return matched

    def matches(self, entries: list[Entry]) -> set[int]:
        grouped: dict[tuple[str, str], list[tuple[datetime | None, int]]] = defaultdict(list)
        undated = []
        for ordinal, entry in enumerate(entries):
            key = self._key(entry)
            stamp = _time(entry.ts)
            grouped[key].append((stamp, ordinal))
            if stamp is None:
                undated.append((ordinal, key))
        # The nearest opposite-side pair is adjacent in a sorted time line.
        # Equal-time entries share a node and consume original order, avoiding
        # the quadratic candidate product for repeated text and timestamps.
        for key, transcripts in grouped.items():
            self._timeline(transcripts, key)
        matched = self._dated()
        remaining: dict[tuple[str, str], deque[int]] = {}
        for ordinal, key in undated:
            if key not in remaining:
                remaining[key] = deque(i for _, i in self._bucket(key))
            candidates = remaining[key]
            while candidates and candidates[0] in self.used:
                candidates.popleft()
            if candidates:
                self.used.add(candidates.popleft())
                matched.add(ordinal)
        return matched

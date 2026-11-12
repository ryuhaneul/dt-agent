# ruff: noqa: INP001
from __future__ import annotations

import json
import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from ductor_bot.bus.adapters import build_interagent_injection_prompt
from ductor_bot.chat_log.cli import cmd_chat_log
from ductor_bot.chat_log.history import Entry, HistoryStore
from ductor_bot.chat_log.importer import TranscriptImporter
from ductor_bot.config import AgentConfig
from ductor_bot.multiagent.bus import AsyncInterAgentResult
from ductor_bot.orchestrator import hooks
from ductor_bot.workspace.paths import DuctorPaths

FIXTURES = Path(__file__).parent / "fixtures"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def add_rows(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


@pytest.fixture
def setup_import(tmp_path: Path) -> tuple[DuctorPaths, AgentConfig]:
    paths = DuctorPaths(tmp_path)
    config = AgentConfig(
        chat_log={"enabled": True},
        memory_reflection={"prompt": "Synthetic reflection"},
        heartbeat={
            "prompt": "Synthetic heartbeat",
            "ack_token": "SYNTHETIC_OK",
            "group_targets": [
                {"chat_id": 111, "prompt": "Synthetic target heartbeat", "ack_token": "TARGET_OK"}
            ],
        },
    )
    write_json(
        paths.sessions_path,
        {
            "tg:111:7": {
                "provider_sessions": {
                    "claude": {"session_id": "synthetic-claude-a"},
                    "gemini": {"session_id": "synthetic-gemini"},
                }
            },
            "mx:222": {"provider_sessions": {"claude": {"session_id": "synthetic-other"}}},
        },
    )
    write_json(
        paths.named_sessions_path,
        {
            "sessions": [
                {
                    "transport": "tg",
                    "chat_id": 111,
                    "topic_id": 8,
                    "provider": "codex",
                    "session_id": "synthetic-codex-a",
                },
                {
                    "transport": "mx",
                    "chat_id": 222,
                    "provider": "codex",
                    "session_id": "synthetic-other",
                },
            ]
        },
    )
    claude = (
        tmp_path / ".cli-auth" / "_shared-projects" / "-example-mapped" / "synthetic-claude-a.jsonl"
    )
    claude.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "claude.jsonl", claude)
    codex = (
        tmp_path
        / ".cli-auth"
        / "codex"
        / "sessions"
        / "2020"
        / "rollout-example-synthetic-codex-a.jsonl"
    )
    codex.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "codex.jsonl", codex)
    write_json(paths.config_path, config.model_dump(mode="json"))
    return paths, config


def test_parsing_mapping_and_no_dry_run_writes(setup_import) -> None:
    paths, config = setup_import
    files = TranscriptImporter(paths, config).run(dry_run=True)
    assert not paths.chat_log_dir.exists()
    by_tid = {file.tid: file.entries for file in files}
    assert [entry.text for entry in by_tid["111.7"]] == [
        "Synthetic question",
        "Synthetic final one\n\nSynthetic final two",
        "Synthetic injected reply",
        "Synthetic environment reply",
    ]
    assert [entry.import_ref["line"] for entry in by_tid["111.7"]] == [1, 6, 10, 12]
    assert by_tid["111.7"][1].ts == "2020-01-01T00:00:03Z"
    assert by_tid["111.7"][2].ts is None
    assert [entry.text for entry in by_tid["111.8"]] == [
        "Synthetic Codex question",
        "Synthetic Codex answer",
        "Synthetic injected Codex reply",
    ]
    assert all(
        entry.source == "import" and entry.imported for file in files for entry in file.entries
    )


def test_unknown_only_main_workspace_and_duplicate_roots(setup_import) -> None:
    paths, config = setup_import
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(paths.workspace))
    root = paths.ductor_home / ".cli-auth"
    unknown = root / "_shared-projects" / encoded / "synthetic-unknown.jsonl"
    unknown.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "claude.jsonl", unknown)
    duplicate = root / "claude" / "projects" / "-example-duplicate" / "synthetic-claude-a.jsonl"
    duplicate.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "claude.jsonl", duplicate)
    unrelated = duplicate.with_name("synthetic-other.jsonl")
    shutil.copyfile(FIXTURES / "claude.jsonl", unrelated)
    rollout = root / "codex" / "sessions" / "rollout-example-synthetic-unknown.jsonl"
    add_rows(
        rollout,
        [
            {
                "type": "session_meta",
                "payload": {"id": "synthetic-unknown", "cwd": str(paths.workspace)},
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Synthetic unknown"},
            },
        ],
    )
    outside = rollout.with_name("rollout-example-synthetic-outside.jsonl")
    add_rows(
        outside,
        [
            {
                "type": "session_meta",
                "payload": {"id": "synthetic-outside", "cwd": "/example/other"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Synthetic outside"},
            },
        ],
    )
    files = TranscriptImporter(paths, config).plan()
    assert [(file.provider, file.tid) for file in files].count(("claude", "111.7")) == 1
    assert sum(file.tid == "unknown" for file in files) == 2
    assert not any(file.session_id in {"synthetic-other", "synthetic-outside"} for file in files)


def test_suffixes_queue_context_and_heartbeat_filter(setup_import) -> None:
    paths, config = setup_import
    importer = TranscriptImporter(paths, config)
    suffixes = [
        value.suffix for value in vars(hooks).values() if isinstance(value, hooks.MessageHook)
    ] + [config.memory_reflection.prompt]
    assert importer._clean_user("Synthetic body\n\n" + "\n\n".join(suffixes)) == (
        "Synthetic body",
        None,
    )
    # Match the framework wait block without copying any real transcript.
    from ductor_bot.messenger import queue_context

    waiting = (
        f"{queue_context._START}\nSynthetic queue metadata\n{queue_context._END}\n\nSynthetic body"
    )
    assert importer._clean_user(waiting) == ("Synthetic body", None)
    assert importer._clean_user("Synthetic body\n\nAlmost a hook") == (
        "Synthetic body\n\nAlmost a hook",
        None,
    )
    source = next(file.path for file in importer.plan() if file.provider == "claude")
    add_rows(
        source,
        [
            {"type": "user", "message": {"content": "Synthetic heartbeat"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "SYNTHETIC_OK"}]},
            },
            {"type": "user", "message": {"content": "Synthetic target heartbeat"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "TARGET_OK"}]}},
            {"type": "user", "message": {"content": "Synthetic heartbeat"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Synthetic alert"}]},
            },
        ],
    )
    entries = next(file.entries for file in importer.plan() if file.provider == "claude")
    assert entries[-1].text == "Synthetic alert"
    assert not any(
        "heartbeat" in entry.text or entry.text in {"SYNTHETIC_OK", "TARGET_OK"}
        for entry in entries
    )


async def test_idempotency_live_order_resume_and_undo(setup_import) -> None:
    paths, config = setup_import
    history = HistoryStore(paths.chat_log_dir / "history")
    live = await history.append(
        "111.7", Entry(role="user", source="telegram", text="Synthetic live")
    )
    importer = TranscriptImporter(paths, config)
    assert sum(len(file.entries) for file in importer.run()) == 7
    assert sum(len(file.entries) for file in importer.run()) == 0
    saved = await history.read("111.7")
    assert [entry.seq for entry in saved] == [-999999, -999998, -999997, -999996, 1]
    assert [entry.text for entry in saved[:2]] == [
        "Synthetic question",
        "Synthetic final one\n\nSynthetic final two",
    ]
    assert saved[-1] == live
    source = next(file.path for file in importer.plan() if file.provider == "claude")
    add_rows(source, [{"type": "user", "message": {"content": "Synthetic later"}}])
    assert sum(len(file.entries) for file in importer.run()) == 1
    assert (await history.read("111.7"))[-2].seq == -999995
    assert (
        await history.append(
            "111.7", Entry(role="bot", source="telegram", text="Synthetic live reply")
        )
    ).seq == 2
    await history.append(
        "unknown",
        Entry(role="user", source="import", text="Synthetic unknown", imported=True, seq=-1),
    )
    assert await history.remove_imported() == 9
    assert not (history.directory / "unknown.jsonl").exists()
    assert all(not entry.imported for entry in await history.read("111.7"))
    assert [entry.text for entry in await history.read("111.7")] == [
        "Synthetic live",
        "Synthetic live reply",
    ]


@pytest.mark.parametrize("args", [["import"], ["import", "--undo"]])
def test_disabled_no_writes(setup_import, monkeypatch, args) -> None:
    paths, config = setup_import
    config.chat_log.enabled = False
    write_json(paths.config_path, config.model_dump(mode="json"))
    monkeypatch.setenv("DUCTOR_HOME", str(paths.ductor_home))
    with pytest.raises(SystemExit) as exc:
        cmd_chat_log(["chat-log", *args])
    assert exc.value.code == 1
    assert not paths.chat_log_dir.exists()
    cmd_chat_log(["import", "--dry-run"])
    assert not paths.chat_log_dir.exists()


def test_invalid_config_rejected_and_cli_import_undo(setup_import, monkeypatch) -> None:
    paths, config = setup_import
    monkeypatch.setenv("DUCTOR_HOME", str(paths.ductor_home))
    config.transport = "matrix"
    config.transports = ["matrix"]
    write_json(paths.config_path, config.model_dump(mode="json"))
    with pytest.raises(SystemExit) as exc:
        cmd_chat_log(["import"])
    assert exc.value.code == 1
    assert not paths.chat_log_dir.exists()
    config.transport = "telegram"
    config.transports = ["telegram"]
    write_json(paths.config_path, config.model_dump(mode="json"))
    cmd_chat_log(["chat-log", "import"])
    files_before = {
        path.name: path.read_bytes() for path in paths.chat_log_dir.rglob("*") if path.is_file()
    }
    cmd_chat_log(["import", "--undo", "--dry-run"])
    assert files_before == {
        path.name: path.read_bytes() for path in paths.chat_log_dir.rglob("*") if path.is_file()
    }
    cmd_chat_log(["import", "--undo"])
    assert all(
        not json.loads(line)["imported"]
        for path in (paths.chat_log_dir / "history").glob("*.jsonl")
        for line in path.read_text().splitlines()
    )
    assert json.loads((paths.chat_log_dir / "import_refs.json").read_text()) == []
    assert sum(len(file.entries) for file in TranscriptImporter(paths, config).run()) == 7


@pytest.mark.parametrize(
    "args",
    [[], ["status", "--bad"], ["unknown-cmd"], ["import", "--bad"], ["import", "--undo", "--undo"]],
)
def test_cli_usage(args) -> None:
    with pytest.raises(SystemExit) as exc:
        cmd_chat_log(args)
    assert exc.value.code == 2


def test_concurrent_importers_do_not_duplicate(setup_import) -> None:
    from concurrent.futures import ThreadPoolExecutor

    paths, config = setup_import
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: TranscriptImporter(paths, config).run(), range(2)))
    assert sum(len(file.entries) for result in results for file in result) == 7
    rows = [
        json.loads(line)
        for path in (paths.chat_log_dir / "history").glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert len(rows) == 7


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize(
    ("success", "failure_kind"), [(True, ""), (False, ""), (False, "result_lost")]
)
def test_framework_interagent_injections_are_not_user_history(
    tmp_path, provider, success, failure_kind
):
    paths = DuctorPaths(tmp_path)
    sid = "synthetic-session"
    write_json(
        paths.sessions_path, {"tg:42:7": {"provider_sessions": {provider: {"session_id": sid}}}}
    )
    prompt = build_interagent_injection_prompt(
        AsyncInterAgentResult(
            task_id="synthetic-task",
            sender="main",
            recipient="worker",
            message_preview="Synthetic request",
            result_text="Synthetic worker result",
            success=success,
            failure_kind=failure_kind,
        ),
        agent_name="main",
        transport_label="Telegram",
    )
    if provider == "claude":
        source = tmp_path / ".cli-auth/_shared-projects/project" / f"{sid}.jsonl"
        rows = [
            {"type": "user", "message": {"content": prompt}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Visible reply"}]},
            },
        ]
    else:
        source = tmp_path / ".cli-auth/codex/sessions/rollout-synthetic.jsonl"
        rows = [
            {"type": "session_meta", "payload": {"id": sid, "cwd": str(paths.workspace)}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": prompt}},
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": "Visible reply"},
            },
        ]
    source.parent.mkdir(parents=True)
    add_rows(source, rows)
    planned = TranscriptImporter(paths, AgentConfig()).run(dry_run=True)
    entries = [entry for file in planned for entry in file.entries]
    assert [(entry.role, entry.text) for entry in entries] == [("bot", "Visible reply")]


async def test_import_merges_provider_files_by_time(tmp_path) -> None:
    paths = DuctorPaths(tmp_path)
    write_json(
        paths.sessions_path,
        {
            "tg:42:7": {
                "provider_sessions": {
                    "claude": {"session_id": "a-later"},
                    "codex": {"session_id": "codex"},
                }
            }
        },
    )
    write_json(
        paths.named_sessions_path,
        {
            "sessions": [
                {
                    "transport": "tg",
                    "chat_id": 42,
                    "topic_id": 7,
                    "provider": "claude",
                    "session_id": "z-earlier",
                }
            ]
        },
    )
    root = tmp_path / ".cli-auth/_shared-projects/project"
    root.mkdir(parents=True)
    add_rows(
        root / "a-later.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2020-01-01T09:00:03+09:00",
                "message": {"content": "Claude later"},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Undated after later"}]},
            },
        ],
    )
    add_rows(
        root / "z-earlier.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2020-01-01T00:00:01Z",
                "message": {"content": "Claude earlier"},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Undated after earlier"}]},
            },
        ],
    )
    codex = tmp_path / ".cli-auth/codex/sessions/rollout-codex.jsonl"
    codex.parent.mkdir(parents=True)
    add_rows(
        codex,
        [
            {"type": "session_meta", "payload": {"id": "codex"}},
            {
                "type": "event_msg",
                "timestamp": "2020-01-01T00:00:00Z",
                "payload": {"type": "user_message", "message": "Codex first"},
            },
            {
                "type": "event_msg",
                "timestamp": "2020-01-01T00:00:02Z",
                "payload": {"type": "task_complete", "last_agent_message": "Codex middle"},
            },
        ],
    )
    importer = TranscriptImporter(paths, AgentConfig())
    importer.run()
    saved = await importer.history.read("42.7")
    assert [entry.text for entry in saved] == [
        "Codex first",
        "Claude earlier",
        "Undated after earlier",
        "Codex middle",
        "Claude later",
        "Undated after later",
    ]
    assert saved[2].ts is None
    assert sum(len(file.entries) for file in importer.run()) == 0


async def test_import_excludes_live_duplicates_but_keeps_recording_gaps(
    tmp_path, monkeypatch
) -> None:
    paths = DuctorPaths(tmp_path)
    write_json(
        paths.sessions_path,
        {
            "tg:42:7": {
                "provider_sessions": {"claude": {"session_id": "synthetic"}},
            }
        },
    )
    source = tmp_path / ".cli-auth/_shared-projects/project/synthetic.jsonl"
    source.parent.mkdir(parents=True)
    add_rows(
        source,
        [
            {"type": "user", "message": {"content": "Leading undated"}},
            {"type": "user", "timestamp": "2020-01-01T00:00:00Z", "message": {"content": "Q old"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "A old undated"}]},
            },
            {"type": "user", "timestamp": "2020-01-01T00:00:01Z", "message": {"content": "Q live"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "A live"}]}},
            {
                "type": "user",
                "timestamp": "2020-01-01T00:00:03Z",
                "message": {"content": "Later interval"},
            },
        ],
    )
    importer = TranscriptImporter(paths, AgentConfig())
    # Live messages in another topic do not count as duplicates.
    await importer.history.append(
        "42.8", Entry(role="user", source="telegram", text="Other topic", ts="2019-01-01T00:00:00Z")
    )
    assert sum(len(file.entries) for file in importer.plan()) == 6
    live = [
        await importer.history.append(
            "42.7",
            Entry(role="user", source="telegram", text="Q live", ts="2020-01-01T09:00:01+09:00"),
        ),
        await importer.history.append(
            "42.7", Entry(role="bot", source="telegram", text="A live", ts="2020-01-01T00:00:02Z")
        ),
    ]
    expected = ["Leading undated", "Q old", "A old undated", "Later interval"]
    assert [entry.text for file in importer.plan() for entry in file.entries] == expected
    # Simulate a recorder write after planning: run must recheck under the file lock.
    unfiltered = TranscriptImporter(paths, AgentConfig())
    monkeypatch.setattr(
        unfiltered,
        "plan",
        lambda: [
            replace(file, entries=tuple(unfiltered._claude(file.path, file.session_id)))
            for file in importer.plan()
        ],
    )
    assert sum(len(file.entries) for file in unfiltered.run()) == 4
    saved = await importer.history.read("42.7")
    assert [entry.text for entry in saved] == [*expected, "Q live", "A live"]
    assert saved[-2:] == live
    assert sum(len(file.entries) for file in importer.run()) == 0


@pytest.mark.parametrize("live_copy", [False, True])
async def test_pruned_import_refs_remain_consumed(setup_import, live_copy) -> None:
    from ductor_bot.chat_log.outbox import Outbox
    from ductor_bot.chat_log.prune import RetentionPruner

    paths, config = setup_import
    importer = TranscriptImporter(paths, config)
    if live_copy:
        await importer.history.append(
            "111.7", Entry("user", "telegram", "Synthetic question", ts="2020-01-01T00:00:00Z")
        )
    assert sum(len(file.entries) for file in importer.run()) == (6 if live_copy else 7)
    result = await RetentionPruner(
        importer.history, Outbox(paths.chat_log_dir / "outbox"), paths.webchat_dir / "backups"
    ).prune(history_days=1)
    assert result.entries > 0
    assert sum(len(file.entries) for file in TranscriptImporter(paths, config).run()) == 0


def test_truncated_multibyte_transcript_line_is_skipped(setup_import) -> None:
    paths, config = setup_import
    importer = TranscriptImporter(paths, config)
    source = next(file.path for file in importer.plan() if file.provider == "claude")
    with source.open("ab") as stream:
        stream.write(b'{"message":"' + "€".encode()[:2] + b"\n")
    add_rows(source, [{"type": "user", "message": {"content": "After damaged line"}}])
    assert sum(len(file.entries) for file in importer.run()) == 8
    assert sum(len(file.entries) for file in importer.run()) == 0


@pytest.mark.parametrize(
    ("timestamp", "role", "tid", "duplicate"),
    [
        (None, "user", "111.7", True),
        ("2020-01-01T00:10:00Z", "user", "111.7", True),
        ("2019-12-31T23:50:00Z", "user", "111.7", True),
        ("2020-01-01T00:10:01Z", "user", "111.7", False),
        (None, "bot", "111.7", False),
        (None, "user", "111.8", False),
    ],
)
async def test_live_duplicates_match_normalized_body_role_topic_and_time(
    setup_import, timestamp, role, tid, duplicate
) -> None:
    paths, config = setup_import
    importer = TranscriptImporter(paths, config)
    source = next(file.path for file in importer.plan() if file.provider == "claude")
    source.write_text("")
    add_rows(
        source, [{"type": "user", "timestamp": timestamp, "message": {"content": "Café body"}}]
    )
    await importer.history.append(
        tid, Entry(role, "telegram", "  Cafe\u0301  body\n", ts="2020-01-01T00:00:00Z")
    )
    planned = next(file.entries for file in importer.plan() if file.provider == "claude")
    assert len(planned) == (0 if duplicate else 1)


def test_import_respects_retention_cutoff_and_keeps_undated(setup_import) -> None:
    import time_machine

    paths, config = setup_import
    config.chat_log.retention_days = 7
    importer = TranscriptImporter(paths, config)
    source = next(file.path for file in importer.plan() if file.provider == "claude")
    source.write_text("")
    add_rows(
        source,
        [
            {
                "type": "user",
                "timestamp": "2020-01-01T00:00:00Z",
                "message": {"content": "Too old"},
            },
            {
                "type": "user",
                "timestamp": "2020-01-02T00:00:00Z",
                "message": {"content": "Boundary"},
            },
            {"type": "user", "message": {"content": "No timestamp"}},
        ],
    )
    with time_machine.travel("2020-01-09T00:00:00Z", tick=False):
        saved = next(file.entries for file in importer.run() if file.provider == "claude")
        assert [entry.text for entry in saved] == ["Boundary", "No timestamp"]
        assert sum(len(file.entries) for file in importer.run()) == 0


async def test_status_skips_unreadable_and_corrupt_queue_files(setup_import, capsys) -> None:
    from unittest.mock import patch

    from ductor_bot.chat_log.outbox import Outbox

    paths, config = setup_import
    outbox = Outbox(paths.chat_log_dir / "outbox")
    item = await outbox.enqueue(kind="bus", chat_id=1, text="broken")
    path = next((outbox.directory / "pending").glob("*.json"))
    data = json.loads(path.read_text())
    data["unknown"] = True
    path.write_text(json.dumps(data))
    from ductor_bot.chat_log.cli import _print_chat_log_status
    from ductor_bot.i18n import init

    init("en")
    _print_chat_log_status(config, paths)
    assert "unreadable: 1" in capsys.readouterr().out
    with patch.object(Outbox, "_load", side_effect=OSError("temporary")):
        _print_chat_log_status(config, paths)
    assert "unreadable: 1" in capsys.readouterr().out
    assert path.exists()
    assert item.state == "pending"


@pytest.mark.parametrize("role", ["user", "bot"])
def test_large_import_bounds_live_duplicate_work(tmp_path, monkeypatch, role) -> None:
    from dataclasses import asdict

    from ductor_bot.chat_log import importer as module

    paths = DuctorPaths(tmp_path)
    write_json(
        paths.sessions_path,
        {"tg:42:7": {"provider_sessions": {"claude": {"session_id": "synthetic"}}}},
    )
    importer = TranscriptImporter(paths, AgentConfig())
    count = 3000
    history_path = importer.history._path("42.7")
    history_path.parent.mkdir(parents=True, exist_ok=True)
    add_rows(
        history_path,
        [
            asdict(
                Entry(
                    role,
                    "telegram",
                    "Shared body" + (f" --- notice {i}" if role == "bot" else ""),
                    seq=i + 1,
                    ts="2020-01-02T00:00:00Z",
                )
            )
            for i in range(count)
        ],
    )
    source = tmp_path / ".cli-auth/_shared-projects/project/synthetic.jsonl"
    source.parent.mkdir(parents=True)
    rows = []
    for _ in range(count):
        if role == "bot":
            # Separate assistant responses using filtered user boundaries.
            rows.append({"type": "user", "message": {"content": "<internal>"}})
        rows.append(
            {
                "type": "user" if role == "user" else "assistant",
                "timestamp": "2020-01-01T00:00:00Z",
                "message": {
                    "content": "Shared body"
                    if role == "user"
                    else [
                        {"type": "text", "text": "Shared body"},
                    ]
                },
            }
        )
    add_rows(source, rows)
    normalizations = matches = prefix_checks = 0
    normalize = module.unicodedata.normalize
    match = module._LiveIndex.matches

    class CountedBody(str):
        __slots__ = ()

        def startswith(self, prefix, *args):
            nonlocal prefix_checks
            prefix_checks += 1
            return super().startswith(prefix, *args)

    def counted_normalize(form, text):
        nonlocal normalizations
        normalizations += 1
        return CountedBody(normalize(form, text))

    def counted_match(index, entries):
        nonlocal matches
        matches += len(entries)
        return match(index, entries)

    monkeypatch.setattr(module.unicodedata, "normalize", counted_normalize)
    monkeypatch.setattr(module._LiveIndex, "matches", counted_match)
    assert sum(len(file.entries) for file in importer.run()) == count
    # Planning and locked recheck each normalize existing/imported bodies once;
    # a cross-product scan would monopolize the recorder's shared topic lock.
    assert normalizations <= 4 * count
    assert matches == 2 * count
    assert prefix_checks <= 2 * count


@pytest.mark.parametrize(
    "timestamps",
    [
        ["2020-01-01T00:00:00Z", "2020-01-01T00:06:00Z"],
        [None, None, None],
    ],
)
async def test_live_duplicate_matching_is_one_to_one(setup_import, timestamps):
    paths, config = setup_import
    importer = TranscriptImporter(paths, config)
    source = next(file.path for file in importer.plan() if file.provider == "claude")
    source.write_text("")
    add_rows(
        source,
        [{"type": "user", "timestamp": ts, "message": {"content": "ok"}} for ts in timestamps],
    )
    await importer.history.append(
        "111.7",
        Entry(
            "user",
            "telegram",
            "ok",
            ts="2020-01-01T00:06:00Z" if timestamps[0] else "2020-01-04T00:00:00Z",
        ),
    )
    planned = next(file for file in importer.plan() if file.provider == "claude")
    assert len(planned.live_refs) == 1
    assert len(planned.entries) == len(timestamps) - 1
    assert planned.entries[0].import_ref["line"] == (1 if timestamps[0] else 2)
    saved = next(file for file in importer.run() if file.provider == "claude")
    assert len(saved.entries) == len(timestamps) - 1
    assert sum(len(file.entries) for file in importer.run()) == 0
    add_rows(source, [{"type": "user", "message": {"content": "ok"}}])
    assert sum(len(file.entries) for file in importer.run()) == 1


@pytest.mark.parametrize(
    "live_text",
    [
        "Café body",
        "**[synthetic-session | claude]**\nCafe\u0301 body",
    ],
)
async def test_bot_live_duplicate_allows_appended_notice_and_named_tag(setup_import, live_text):
    from ductor_bot.orchestrator.flows import _session_age_note
    from ductor_bot.session.manager import SessionData

    note = _session_age_note(
        SessionData(chat_id=111, created_at="2020-01-01T00:00:00Z", message_count=10),
        warning_hours=1,
    )
    assert note
    live_text += note
    paths, config = setup_import
    importer = TranscriptImporter(paths, config)
    source = next(file.path for file in importer.plan() if file.provider == "claude")
    source.write_text("")
    add_rows(
        source,
        [
            {
                "type": "assistant",
                "timestamp": "2020-01-01T00:00:00Z",
                "message": {"content": [{"type": "text", "text": "Café body"}]},
            }
        ],
    )
    await importer.history.append(
        "111.7",
        Entry(
            "bot",
            "telegram",
            live_text,
            ts="2020-01-01T00:00:01Z",
        ),
    )
    assert next(file for file in importer.run() if file.provider == "claude").entries == ()


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"[",
        b"{}",
        b"null",
        b"[1]",
        b'[["claude", "synthetic", "bad"]]',
        b'[["claude", [], 1]]',
        b"\xff",
    ],
)
async def test_corrupt_import_refs_falls_back_to_existing_history(setup_import, caplog, content):
    paths, config = setup_import
    importer = TranscriptImporter(paths, config)
    assert sum(len(file.entries) for file in importer.run()) == 7
    importer.state_path.write_bytes(content)
    assert sum(len(file.entries) for file in importer.run(dry_run=True)) == 0
    assert importer.state_path.read_bytes() == content
    assert sum(len(file.entries) for file in importer.run()) == 0
    assert "Ignoring invalid import reference ledger" in caplog.text


def test_retention_uses_previous_timestamp_for_undated_entries(setup_import):
    import time_machine

    paths, config = setup_import
    config.chat_log.retention_days = 7
    importer = TranscriptImporter(paths, config)
    source = next(file.path for file in importer.plan() if file.provider == "claude")
    source.write_text("")
    add_rows(
        source,
        [
            {"type": "user", "message": {"content": "Leading undated"}},
            {"type": "user", "timestamp": "2020-01-01T00:00:00Z", "message": {"content": "Old"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Old undated"}]},
            },
            {
                "type": "user",
                "timestamp": "2020-01-02T00:00:00Z",
                "message": {"content": "Boundary"},
            },
            {"type": "user", "message": {"content": "Fresh undated"}},
        ],
    )
    with time_machine.travel("2020-01-09T00:00:00Z", tick=False):
        planned = next(file for file in importer.plan() if file.provider == "claude")
        assert [entry.text for entry in planned.entries] == [
            "Leading undated",
            "Boundary",
            "Fresh undated",
        ]
        saved = next(file for file in importer.run() if file.provider == "claude")
        assert [entry.text for entry in saved.entries] == [
            "Leading undated",
            "Boundary",
            "Fresh undated",
        ]


@pytest.mark.parametrize("role", ["user", "bot"])
def test_live_index_uses_global_nearest_then_undated_sequence(role):
    from ductor_bot.chat_log.importer import _LiveIndex

    live = [
        Entry(
            role, "telegram", "ok --- suffix" if role == "bot" else "ok", ts="2020-01-01T00:06:00Z"
        ),
        Entry(
            role, "telegram", "ok --- notice" if role == "bot" else "ok", ts="2020-01-01T00:00:00Z"
        ),
    ]
    entries = [
        Entry(role, "import", "ok", ts=None),
        Entry(role, "import", "ok", ts="2020-01-01T00:06:00Z"),
        Entry(role, "import", "ok", ts="2020-01-01T00:00:00Z"),
    ]
    assert _LiveIndex(live).matches(entries) == {1, 2}
    # Equal distances choose transcript order, then live sequence order.
    entries = [
        Entry(role, "import", "ok", ts="2020-01-01T00:03:00Z"),
        Entry(
            role, "import", "ok --- suffix" if role == "bot" else "ok", ts="2020-01-01T00:06:00Z"
        ),
    ]
    assert _LiveIndex(live).matches(entries) == {0, 1}


def test_overlapping_bot_prefixes_do_not_reuse_live_entry():
    from ductor_bot.chat_log.importer import _LiveIndex

    live = [Entry("bot", "telegram", "Answer --- extended --- notice", ts="2020-01-01T00:00:00Z")]
    entries = [
        Entry("bot", "import", "Answer", ts="2020-01-01T00:00:00Z"),
        Entry("bot", "import", "Answer --- extended", ts=None),
        Entry("bot", "import", "Answer --- extended", ts="2020-01-01T00:00:00Z"),
    ]
    assert _LiveIndex(live).matches(entries) == {0}


def test_locked_recheck_consumes_new_live_entry_only_once(setup_import, monkeypatch):
    paths, config = setup_import
    importer = TranscriptImporter(paths, config)
    source = next(file.path for file in importer.plan() if file.provider == "claude")
    source.write_text("")
    add_rows(source, [{"type": "user", "message": {"content": "ok"}} for _ in range(3)])
    plan = importer.plan

    def record_after_planning():
        files = plan()
        importer.history._append("111.7", Entry("user", "telegram", "ok"))
        return files

    with monkeypatch.context() as context:
        context.setattr(importer, "plan", record_after_planning)
        saved = next(file for file in importer.run() if file.provider == "claude")
    assert len(saved.entries) == 2
    assert sum(len(file.entries) for file in importer.run()) == 0


@pytest.mark.parametrize(
    "timestamps",
    [
        ["2020-01-01T00:00:00Z", "2020-01-01T00:06:00Z"],
        ["2020-01-01T00:06:00Z", "2020-01-01T00:00:00Z"],
    ],
)
def test_global_nearest_ties_use_transcript_then_live_order(timestamps):
    from ductor_bot.chat_log.importer import _LiveIndex

    live = [Entry("user", "telegram", "ok", ts=stamp) for stamp in timestamps]
    transcript = [Entry("user", "import", "ok", ts="2020-01-01T00:03:00Z")]
    index = _LiveIndex(live)
    assert index.matches(transcript) == {0}
    assert index.used == {0}
    index = _LiveIndex([Entry("user", "telegram", "ok", ts="2020-01-01T00:03:00Z")])
    transcript = [Entry("user", "import", "ok", ts=stamp) for stamp in timestamps]
    assert index.matches(transcript) == {0}


async def test_global_nearest_matching_spans_transcripts_in_same_topic(setup_import):
    paths, config = setup_import
    importer = TranscriptImporter(paths, config)
    source = next(file.path for file in importer.plan() if file.provider == "claude")
    source.write_text("")
    add_rows(
        source,
        [{"type": "user", "timestamp": "2020-01-01T00:00:00Z", "message": {"content": "ok"}}],
    )
    later = source.with_name("z-synthetic.jsonl")
    add_rows(
        later, [{"type": "user", "timestamp": "2020-01-01T00:06:00Z", "message": {"content": "ok"}}]
    )
    named = json.loads(paths.named_sessions_path.read_text())
    named["sessions"].append(
        {"chat_id": 111, "topic_id": 7, "provider": "claude", "session_id": "z-synthetic"}
    )
    write_json(paths.named_sessions_path, named)
    await importer.history.append(
        "111.7", Entry("user", "telegram", "ok", ts="2020-01-01T00:06:00Z")
    )
    files = {file.path: file for file in importer.run()}
    assert [entry.ts for entry in files[source].entries] == ["2020-01-01T00:00:00Z"]
    assert files[later].entries == ()
    assert sum(len(file.entries) for file in importer.run()) == 0


@pytest.mark.parametrize("failed_read", [1, 2])
@pytest.mark.parametrize("error", [OSError(5, "I/O error"), PermissionError(13, "denied")])
async def test_unreadable_import_refs_preserves_pruned_ledger(
    setup_import, monkeypatch, failed_read, error
):
    from ductor_bot.chat_log.outbox import Outbox
    from ductor_bot.chat_log.prune import RetentionPruner

    paths, config = setup_import
    importer = TranscriptImporter(paths, config)
    importer.run()
    await RetentionPruner(
        importer.history, Outbox(paths.chat_log_dir / "outbox"), paths.webchat_dir / "backups"
    ).prune(history_days=1)
    before = importer.state_path.read_bytes()
    assert json.loads(before)
    history_before = {
        path: path.read_bytes() for path in importer.history.directory.glob("*.jsonl")
    }
    read_text = Path.read_text
    reads = 0

    def fail_ledger_read(path, *args, **kwargs):
        nonlocal reads
        if path == importer.state_path:
            reads += 1
            if reads == failed_read:
                raise error
        return read_text(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "read_text", fail_ledger_read)
        with pytest.raises(type(error)):
            importer.run()
    assert reads == failed_read
    assert importer.state_path.read_bytes() == before
    assert {
        path: path.read_bytes() for path in importer.history.directory.glob("*.jsonl")
    } == history_before
    assert sum(len(file.entries) for file in importer.run()) == 0


@pytest.mark.parametrize("known_from", ["history", "both"])
@pytest.mark.parametrize("late_live", [False, True])
def test_consumed_transcript_does_not_reserve_new_live(
    setup_import, monkeypatch, known_from, late_live
):
    paths, config = setup_import
    importer = TranscriptImporter(paths, config)
    source = next(file.path for file in importer.plan() if file.provider == "claude")
    source.write_text("")
    row = {"type": "user", "message": {"content": "ok"}}
    add_rows(source, [row])
    importer.run()
    if known_from == "history":
        importer.state_path.write_text("[]\n")
    add_rows(source, [row])
    live = Entry("user", "telegram", "ok")
    plan = importer.plan

    def record_after_planning():
        files = plan()
        importer.history._append("111.7", live)
        return files

    if not late_live:
        importer.history._append("111.7", live)
        assert sum(len(file.entries) for file in importer.plan()) == 0
    with monkeypatch.context() as context:
        if late_live:
            context.setattr(importer, "plan", record_after_planning)
        assert sum(len(file.entries) for file in importer.run()) == 0
    saved = importer.history._read(importer.history._path("111.7"))
    assert len(saved) == 2
    assert sum(entry.id == live.id for entry in saved) == 1
    assert sum(len(file.entries) for file in importer.run()) == 0


@pytest.mark.parametrize(
    ("reply", "timestamp", "live_text"),
    [
        ("Yes", "2020-01-01T00:00:00Z", "Yes, but only after checking"),
        ("OK", None, "OK, here is a background result"),
    ],
)
async def test_short_bot_reply_is_not_consumed_by_unrelated_live_prefix(
    setup_import, reply, timestamp, live_text
):
    paths, config = setup_import
    importer = TranscriptImporter(paths, config)
    source = next(file.path for file in importer.plan() if file.provider == "claude")
    source.write_text("")
    add_rows(
        source,
        [
            {
                "type": "assistant",
                "timestamp": timestamp,
                "message": {"content": [{"type": "text", "text": reply}]},
            }
        ],
    )
    await importer.history.append(
        "111.7", Entry("bot", "telegram", live_text, ts="2020-01-01T00:05:00Z")
    )
    planned = next(file for file in importer.plan() if file.provider == "claude")
    assert [entry.text for entry in planned.entries] == [reply]
    assert planned.live_refs == ()
    saved = next(file for file in importer.run() if file.provider == "claude")
    assert [entry.text for entry in saved.entries] == [reply]
    history = await importer.history.read("111.7")
    assert [entry.text for entry in history if entry.imported] == [reply]
    assert sum(len(file.entries) for file in importer.run()) == 0

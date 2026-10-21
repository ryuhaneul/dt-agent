"""The private failure ledger counts each task once across delegation retries."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from ductor_bot.multiagent import failure_ledger
from ductor_bot.multiagent.bus import AsyncInterAgentResult
from ductor_bot.multiagent.failure_ledger import FailureLedger, failure_key


def _result(
    task_id: str,
    *,
    recipient: str = "developer",
    result_origin: str = "",
    original_message: str = "  Fix   THIS request  ",
) -> AsyncInterAgentResult:
    return AsyncInterAgentResult(
        task_id=task_id,
        sender="main",
        recipient=recipient,
        message_preview="fix",
        result_text="",
        success=False,
        original_message=original_message,
        chat_id=123,
        topic_id=7,
        result_origin=result_origin,
    )


async def test_retry_same_task_does_not_increment_or_extend_window(tmp_path: Path) -> None:
    ledger = FailureLedger(tmp_path)
    result = _result("task-1")
    assert await ledger.record_failure("main", result) == 1
    before = json.loads(ledger.path.read_text())
    assert await ledger.record_failure("main", result) == 1
    assert json.loads(ledger.path.read_text()) == before
    assert await FailureLedger(tmp_path).record_failure("main", result) == 1


async def test_other_recipient_same_request_counts_and_success_resets(tmp_path: Path) -> None:
    ledger = FailureLedger(tmp_path)
    assert await ledger.record_failure("main", _result("task-1")) == 1
    assert await ledger.record_failure("main", _result("task-2", recipient="reviewer")) == 2
    await ledger.clear_success("main", _result("task-3", recipient="reviewer"))
    assert json.loads(ledger.path.read_text()) == {}
    assert await ledger.record_failure("main", _result("task-4")) == 1


async def test_message_normalization_and_target_are_part_of_key(tmp_path: Path) -> None:
    ledger = FailureLedger(tmp_path)
    first = _result("task-1")
    same_text = _result("task-2", original_message="fix this REQUEST")
    assert failure_key("main", first) == failure_key("main", same_text)
    assert failure_key("other", first) != failure_key("main", first)
    assert await ledger.record_failure("main", first) == 1
    assert await ledger.record_failure("main", same_text) == 2


async def test_expired_entry_starts_a_new_window(tmp_path: Path) -> None:
    ledger = FailureLedger(tmp_path)
    assert await ledger.record_failure("main", _result("task-1")) == 1
    data = json.loads(ledger.path.read_text())
    key = next(iter(data))
    data[key]["last_at"] = time.time() - 25 * 60 * 60
    ledger.path.write_text(json.dumps(data))
    # A new process would load the externally changed file once.
    ledger = FailureLedger(tmp_path)
    assert await ledger.record_failure("main", _result("task-2")) == 1
    fresh = json.loads(ledger.path.read_text())[key]
    assert fresh["task_ids"] == ["task-2"]


async def test_file_and_directory_are_private(tmp_path: Path) -> None:
    ledger = FailureLedger(tmp_path)
    await ledger.record_failure("main", _result("task-1"))
    assert ledger.path.stat().st_mode & 0o777 == 0o600
    assert ledger.path.parent.stat().st_mode & 0o777 == 0o700


async def test_result_origin_separates_request_series(tmp_path: Path) -> None:
    ledger = FailureLedger(tmp_path)
    assert await ledger.record_failure("main", _result("task-1")) == 1
    assert (
        await ledger.record_failure("main", _result("task-2", result_origin="cron-broadcast")) == 1
    )
    assert len(json.loads(ledger.path.read_text())) == 2


async def test_concurrent_failures_do_not_lose_counts(tmp_path: Path) -> None:
    ledger = FailureLedger(tmp_path)
    counts = await asyncio.gather(
        *(ledger.record_failure("main", _result(f"task-{index}")) for index in range(40))
    )
    assert sorted(counts) == list(range(1, 41))
    entry = next(iter(json.loads(ledger.path.read_text()).values()))
    assert entry["count"] == 40
    assert len(entry["task_ids"]) == 40


@pytest.mark.parametrize("contents", ["", "not-json", "[]", '{"bad": null}'])
async def test_invalid_ledger_content_recovers(tmp_path: Path, contents: str) -> None:
    ledger = FailureLedger(tmp_path)
    ledger.path.parent.mkdir(parents=True)
    ledger.path.write_text(contents)
    assert await ledger.record_failure("main", _result("task-1")) == 1
    assert len(json.loads(ledger.path.read_text())) == 1


async def test_invalid_utf8_file_recovers(tmp_path: Path) -> None:
    ledger = FailureLedger(tmp_path)
    ledger.path.parent.mkdir(parents=True)
    ledger.path.write_bytes(b"\xff")
    assert await ledger.record_failure("main", _result("task-1")) == 1


async def test_read_prunes_expired_entries_even_when_other_task_is_duplicate(
    tmp_path: Path,
) -> None:
    ledger = FailureLedger(tmp_path)
    active = _result("task-1")
    expired = _result("task-2", result_origin="cron-broadcast")
    await ledger.record_failure("main", active)
    await ledger.record_failure("main", expired)
    data = json.loads(ledger.path.read_text())
    data[failure_key("main", expired)]["last_at"] = time.time() - 25 * 60 * 60
    ledger.path.write_text(json.dumps(data))

    ledger = FailureLedger(tmp_path)
    assert await ledger.record_failure("main", active) == 1
    assert list(json.loads(ledger.path.read_text())) == [failure_key("main", active)]


async def test_save_failure_keeps_count_in_memory_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ledger = FailureLedger(tmp_path)
    save = failure_ledger.atomic_text_save

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(failure_ledger, "atomic_text_save", fail_save)
    with caplog.at_level("ERROR", logger=failure_ledger.__name__):
        assert await ledger.record_failure("main", _result("task-1")) == 1
        assert await ledger.record_failure("main", _result("task-2", recipient="reviewer")) == 2
    assert caplog.text.count("Cannot save async failure ledger") == 2
    assert str(ledger.path) in caplog.text
    assert "OSError" in caplog.text

    monkeypatch.setattr(failure_ledger, "atomic_text_save", save)
    assert await ledger.record_failure("main", _result("task-3")) == 3
    entry = json.loads(ledger.path.read_text())[failure_key("main", _result("task-1"))]
    assert entry["count"] == 3
    assert entry["task_ids"] == ["task-1", "task-2", "task-3"]


async def test_success_during_save_failure_clears_memory_and_later_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = FailureLedger(tmp_path)
    first = _result("task-1")
    assert await ledger.record_failure("main", first) == 1
    save = failure_ledger.atomic_text_save

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(failure_ledger, "atomic_text_save", fail_save)
    assert await ledger.record_failure("main", _result("task-2")) == 2
    await ledger.clear_success("main", _result("task-3"))
    # The last persisted file still has this series until a save succeeds.
    assert failure_key("main", first) in json.loads(ledger.path.read_text())

    monkeypatch.setattr(failure_ledger, "atomic_text_save", save)
    other = _result("task-4", result_origin="another-origin")
    assert await ledger.record_failure("main", other) == 1
    data = json.loads(ledger.path.read_text())
    assert failure_key("main", first) not in data
    assert list(data) == [failure_key("main", other)]


async def test_in_memory_ttl_expires_without_rereading_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = FailureLedger(tmp_path)
    start = time.time()
    monkeypatch.setattr(failure_ledger.time, "time", lambda: start)
    assert await ledger.record_failure("main", _result("task-1")) == 1
    monkeypatch.setattr(failure_ledger.time, "time", lambda: start + 25 * 60 * 60)
    assert await ledger.record_failure("main", _result("task-2")) == 1
    entry = json.loads(ledger.path.read_text())[failure_key("main", _result("task-2"))]
    assert entry["task_ids"] == ["task-2"]


async def test_bus_delivery_tracks_failures_and_clears_on_success(tmp_path: Path) -> None:
    from ductor_bot.bus.adapters import build_interagent_injection_prompt
    from ductor_bot.bus.envelope import DeliveryOutcome
    from ductor_bot.multiagent.bus import InterAgentBus

    bus = InterAgentBus()
    bus._failure_ledger = FailureLedger(tmp_path)
    prompts: list[str] = []

    async def receive(result: AsyncInterAgentResult) -> DeliveryOutcome:
        prompts.append(
            build_interagent_injection_prompt(
                result, agent_name="main", transport_label="Telegram chat"
            )
        )
        return DeliveryOutcome(ok=True, injected=True, delivered=True)

    bus.set_async_result_handler("main", receive)
    first = _result("task-1")
    second = _result("task-2", recipient="reviewer")
    await bus._deliver_async_result(first)
    await bus._deliver_async_result(first)  # delivery retry counts once
    await bus._deliver_async_result(second)
    assert first.failure_count == 1
    assert second.failure_count == 2
    assert "Failures of this request in the last 24 hours: 2" in prompts[-1]
    assert "report the failure to the user" in prompts[-1]

    success = _result("task-3")
    success.success = True
    await bus._deliver_async_result(success)
    assert json.loads((tmp_path / "interagent" / "failures.json").read_text()) == {}

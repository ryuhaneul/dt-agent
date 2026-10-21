"""Timezone delegation for one-shot task execution."""

from __future__ import annotations

import pytest

from ductor_bot.infra.task_runner import _resolve_tz


@pytest.mark.parametrize("configured", ["", "invalid/timezone"])
def test_timezone_uses_host_setting(monkeypatch: pytest.MonkeyPatch, configured: str) -> None:
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert _resolve_tz(configured).key == "Asia/Tokyo"

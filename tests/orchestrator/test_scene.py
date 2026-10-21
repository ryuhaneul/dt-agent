"""Tests for scene features: OrchestratorResult metadata and footer appending."""

from __future__ import annotations

import pytest

from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import SceneConfig
from ductor_bot.messenger.telegram.message_dispatch import _build_footer
from ductor_bot.orchestrator.flows import _finish_normal
from ductor_bot.orchestrator.registry import OrchestratorResult


class TestOrchestratorResultMetadata:
    def test_defaults(self) -> None:
        r = OrchestratorResult(text="hello")
        assert r.model_name is None
        assert r.duration_ms is None

    def test_explicit_metadata(self) -> None:
        r = OrchestratorResult(
            text="hello",
            model_name="opus",
            duration_ms=3000.0,
        )
        assert r.model_name == "opus"
        assert r.duration_ms == 3000.0


class TestFinishNormalMetadata:
    def test_populates_metadata(self) -> None:
        response = AgentResponse(
            result="Agent output",
            duration_ms=2500.0,
        )
        result = _finish_normal(response, model_name="opus")
        assert result.model_name == "opus"
        assert result.duration_ms == 2500.0

    def test_error_response_no_metadata(self) -> None:
        response = AgentResponse(result="fail", is_error=True)
        result = _finish_normal(response, model_name="opus")
        assert result.model_name is None


class TestBuildFooter:
    def test_footer_built_when_enabled(self) -> None:
        scene = SceneConfig(technical_footer=True)
        result = OrchestratorResult(
            text="Hello",
            model_name="opus",
            duration_ms=3000.0,
        )
        footer = _build_footer(result, scene)
        assert "Model: opus" in footer
        assert "Time: 3.0s" in footer
        assert "Token" not in footer
        assert "Cost" not in footer

    def test_footer_empty_when_disabled(self) -> None:
        scene = SceneConfig(technical_footer=False)
        result = OrchestratorResult(text="Hello", model_name="opus")
        assert _build_footer(result, scene) == ""

    def test_footer_empty_without_model(self) -> None:
        scene = SceneConfig(technical_footer=True)
        result = OrchestratorResult(text="Hello")
        assert _build_footer(result, scene) == ""

    def test_footer_empty_with_none_config(self) -> None:
        result = OrchestratorResult(text="Hello", model_name="opus")
        assert _build_footer(result, None) == ""


class TestTransportFooters:
    """Matrix and Slack append the same model-and-time footer as Telegram."""

    @staticmethod
    def _stub() -> object:
        from types import SimpleNamespace

        return SimpleNamespace(_config=SimpleNamespace(scene=SceneConfig(technical_footer=True)))

    @pytest.mark.parametrize(
        ("module", "owner", "attribute"),
        [
            ("nio", "ductor_bot.messenger.matrix.bot", "MatrixBot"),
            ("slack_bolt", "ductor_bot.messenger.slack.bot", "SlackBot"),
        ],
    )
    def test_footer_has_model_and_time_only(self, module: str, owner: str, attribute: str) -> None:
        pytest.importorskip(module)
        import importlib

        bot_class = getattr(importlib.import_module(owner), attribute)
        result = OrchestratorResult(text="Hello", model_name="opus", duration_ms=1500.0)
        bot_class._maybe_append_footer(self._stub(), result)
        assert result.text == "Hello\n---\nModel: opus | Time: 1.5s"

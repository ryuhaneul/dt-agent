"""Tests for CLI types: AgentRequest, AgentResponse, CLIResponse."""

from __future__ import annotations

from ductor_bot.cli.types import AgentRequest, AgentResponse, CLIResponse, task_id_from_label

# -- task_id_from_label --


def test_task_id_from_label_extracts_id() -> None:
    assert task_id_from_label("task:abc123") == "abc123"


def test_task_id_from_label_non_task_labels() -> None:
    for label in ("main", "ns:build", "interagent:worker", "memory_flush", ""):
        assert task_id_from_label(label) == ""


# -- CLIResponse --


def test_cli_response_defaults() -> None:
    r = CLIResponse()
    assert r.result == ""
    assert r.is_error is False
    assert r.session_id is None
    assert r.timed_out is False


# -- AgentRequest --


def test_agent_request_defaults() -> None:
    req = AgentRequest(prompt="hello")
    assert req.prompt == "hello"
    assert req.system_prompt is None
    assert req.append_system_prompt is None
    assert req.model_override is None
    assert req.provider_override is None
    assert req.chat_id == 0
    assert req.process_label == "main"
    assert req.resume_session is None
    assert req.continue_session is False
    assert req.timeout_seconds is None


def test_agent_request_is_frozen() -> None:
    req = AgentRequest(prompt="hello")
    try:
        req.prompt = "changed"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        msg = "AgentRequest should be frozen"
        raise AssertionError(msg)


def test_agent_request_with_overrides() -> None:
    req = AgentRequest(
        prompt="do stuff",
        model_override="sonnet",
        provider_override="codex",
        chat_id=42,
        process_label="worker",
    )
    assert req.model_override == "sonnet"
    assert req.provider_override == "codex"
    assert req.chat_id == 42
    assert req.process_label == "worker"


# -- AgentResponse --


def test_agent_response_defaults() -> None:
    resp = AgentResponse(result="done")
    assert resp.result == "done"
    assert resp.session_id is None
    assert resp.is_error is False
    assert resp.timed_out is False
    assert resp.stream_fallback is False


def test_agent_response_is_frozen() -> None:
    resp = AgentResponse(result="done")
    try:
        resp.result = "changed"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        msg = "AgentResponse should be frozen"
        raise AssertionError(msg)


def test_agent_response_with_values() -> None:
    resp = AgentResponse(
        result="answer",
        session_id="abc-123",
        stream_fallback=True,
    )
    assert resp.session_id == "abc-123"
    assert resp.stream_fallback is True

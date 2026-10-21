"""Bundled transcription tools invoke external commands through a synthetic Python shim."""

from __future__ import annotations

import importlib.util
import shlex
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

_TOOLS_DIR = (
    Path(__file__).resolve().parents[2]
    / "ductor_bot"
    / "_home_defaults"
    / "workspace"
    / "tools"
    / "media_tools"
)


def _load_tool(name: str, filename: str) -> Any:
    """Import a bundled tool script as a module."""
    path = _TOOLS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def audio_module() -> Any:
    return _load_tool("_ductor_test_transcribe_audio", "transcribe_audio.py")


@pytest.fixture
def video_module() -> Any:
    return _load_tool("_ductor_test_process_video", "process_video.py")


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    """Write a small Python script and return its path."""
    script = tmp_path / name
    script.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
    return script


def _python_command(script: Path) -> str:
    """Return a shell-style command safe for paths containing spaces."""
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


def test_external_transcribe_unset_is_skip(
    audio_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DUCTOR_TRANSCRIBE_COMMAND", raising=False)
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"fake")

    result = audio_module._transcribe_external(audio)

    assert "transcript" not in result
    assert "error" in result


def test_external_transcribe_returns_transcript_json(
    audio_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _write_script(
        tmp_path,
        "fake_transcribe.py",
        """
        import json, sys
        print(json.dumps({"transcript": "hello world", "language": "en"}))
        """,
    )
    monkeypatch.setenv("DUCTOR_TRANSCRIBE_COMMAND", _python_command(script))
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"fake")

    result = audio_module._transcribe_external(audio)

    assert result["transcript"] == "hello world"
    assert result["language"] == "en"
    assert result["method"] == "external"


def test_external_transcribe_plaintext_stdout(
    audio_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _write_script(
        tmp_path,
        "plain_transcribe.py",
        """
        import sys
        print("just the transcript, no json")
        """,
    )
    monkeypatch.setenv("DUCTOR_TRANSCRIBE_COMMAND", _python_command(script))
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"fake")

    result = audio_module._transcribe_external(audio)

    assert result["transcript"] == "just the transcript, no json"
    assert result["method"] == "external"


def test_external_transcribe_nonzero_falls_through(
    audio_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _write_script(
        tmp_path,
        "bad_transcribe.py",
        """
        import sys
        sys.stderr.write("intentional failure\\n")
        sys.exit(2)
        """,
    )
    monkeypatch.setenv("DUCTOR_TRANSCRIBE_COMMAND", _python_command(script))
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"fake")

    result = audio_module._transcribe_external(audio)

    assert "transcript" not in result
    assert "error" in result


def test_external_transcribe_missing_binary_falls_through(
    audio_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DUCTOR_TRANSCRIBE_COMMAND", "/nonexistent/path/to/missing-bin")
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"fake")

    result = audio_module._transcribe_external(audio)

    assert "transcript" not in result
    assert "error" in result


def test_video_external_transcribe_returns_text(
    video_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _write_script(
        tmp_path,
        "vid_transcribe.py",
        """
        import sys
        print("video transcript result")
        """,
    )
    monkeypatch.setenv("DUCTOR_VIDEO_TRANSCRIBE_COMMAND", _python_command(script))
    audio = tmp_path / "x.ogg"
    audio.write_bytes(b"fake")

    result = video_module._transcribe_external(audio)

    assert result == "video transcript result"


def test_video_external_transcribe_unset_returns_none(
    video_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DUCTOR_VIDEO_TRANSCRIBE_COMMAND", raising=False)
    audio = tmp_path / "x.ogg"
    audio.write_bytes(b"fake")

    assert video_module._transcribe_external(audio) is None


def test_video_external_transcribe_nonzero_returns_none(
    video_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _write_script(
        tmp_path,
        "vid_fail.py",
        """
        import sys
        sys.exit(2)
        """,
    )
    monkeypatch.setenv("DUCTOR_VIDEO_TRANSCRIBE_COMMAND", _python_command(script))
    audio = tmp_path / "x.ogg"
    audio.write_bytes(b"fake")

    assert video_module._transcribe_external(audio) is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))

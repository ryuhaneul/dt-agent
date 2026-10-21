"""PERSONA.md / BEHAVIOR.md ship as seed-once templates and are never overwritten."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ductor_bot.workspace.init import init_workspace
from ductor_bot.workspace.loader import (
    PERSONA_UNSET_MARKER,
    build_appended_files_block,
    persona_is_unset,
)
from ductor_bot.workspace.paths import DuctorPaths

_DEFAULTS = Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults/workspace"


def _fresh(tmp_path: Path) -> DuctorPaths:
    paths = DuctorPaths(ductor_home=tmp_path / "home")
    init_workspace(paths)
    return paths


def test_new_install_seeds_both_templates(tmp_path: Path) -> None:
    paths = _fresh(tmp_path)
    behavior = paths.workspace / "BEHAVIOR.md"
    persona = paths.workspace / "PERSONA.md"
    assert behavior.read_text(encoding="utf-8") == (_DEFAULTS / "BEHAVIOR.md").read_text(
        encoding="utf-8"
    )
    assert persona.read_text(encoding="utf-8").count(PERSONA_UNSET_MARKER) == 1
    assert persona_is_unset(paths) is True


def test_behavior_template_is_minimal_and_has_the_approved_start() -> None:
    text = (_DEFAULTS / "BEHAVIOR.md").read_text(encoding="utf-8")
    assert text.startswith("# Behavior Rules\n\nKeep this file short:")
    assert text.count("\n- ") == 8  # eight essential principles, nothing delegation-specific
    assert "delegat" not in text.lower().replace("delegate long-running work", "")


def test_existing_user_files_are_never_overwritten(tmp_path: Path) -> None:
    paths = _fresh(tmp_path)
    (paths.workspace / "PERSONA.md").write_text("# Mine\nName: Ari\n", encoding="utf-8")
    (paths.workspace / "BEHAVIOR.md").write_text("- my rule\n", encoding="utf-8")
    init_workspace(paths)
    init_workspace(paths)
    assert (paths.workspace / "PERSONA.md").read_text(encoding="utf-8") == "# Mine\nName: Ari\n"
    assert (paths.workspace / "BEHAVIOR.md").read_text(encoding="utf-8") == "- my rule\n"
    assert persona_is_unset(paths) is False


def test_unset_persona_is_not_injected_but_behavior_is(tmp_path: Path) -> None:
    paths = _fresh(tmp_path)
    block = asyncio.run(build_appended_files_block(paths, ["PERSONA.md", "BEHAVIOR.md"]))
    assert block is not None
    assert "# Behavior Rules" in block
    assert "# Persona" not in block
    persona = paths.workspace / "PERSONA.md"
    persona.write_text("# Persona\nName: Ari\n", encoding="utf-8")
    block = asyncio.run(build_appended_files_block(paths, ["PERSONA.md", "BEHAVIOR.md"]))
    assert block is not None
    assert block.index("Name: Ari") < block.index("# Behavior Rules")


def test_memory_templates_state_the_index_principle(tmp_path: Path) -> None:
    paths = _fresh(tmp_path)
    main = paths.mainmemory_path.read_text(encoding="utf-8")
    assert "always-needed essentials plus an index" in main
    assert len([ln for ln in main.splitlines() if ln.startswith(">")]) == 1  # 1-2 line header
    rules = " ".join((_DEFAULTS / "memory_system" / "RULES.md").read_text(encoding="utf-8").split())
    assert "regardless of the model" in rules
    assert "only for notes that apply to that model alone" in rules
    assert "never in a model" not in rules
    assert "index" in rules
    assert "existing page" in rules


def test_marker_is_only_looked_for_in_the_persona_file(tmp_path: Path) -> None:
    paths = _fresh(tmp_path)
    (paths.workspace / "PERSONA.md").write_text("# Persona\nName: Ari\n", encoding="utf-8")
    (paths.workspace / "BEHAVIOR.md").write_text(
        f"- Never write the marker {PERSONA_UNSET_MARKER} into PERSONA.md by hand.\n",
        encoding="utf-8",
    )
    (paths.workspace / "EXTRA.md").write_text(
        f"Example: {PERSONA_UNSET_MARKER}\n", encoding="utf-8"
    )
    block = asyncio.run(
        build_appended_files_block(paths, ["PERSONA.md", "BEHAVIOR.md", "EXTRA.md"])
    )
    assert block is not None
    assert "Name: Ari" in block
    assert "Never write the marker" in block
    assert "Example:" in block
    # while PERSONA.md itself still carries the marker it is skipped
    (paths.workspace / "PERSONA.md").write_text(f"{PERSONA_UNSET_MARKER}\n# P\n", encoding="utf-8")
    block = asyncio.run(
        build_appended_files_block(paths, ["PERSONA.md", "BEHAVIOR.md", "EXTRA.md"])
    )
    assert block is not None
    assert "# P" not in block
    assert "Never write the marker" in block

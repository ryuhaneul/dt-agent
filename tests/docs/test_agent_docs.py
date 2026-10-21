"""The agent-facing DT Agent reference bundle stays complete, current and navigable."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ductor_bot.config import AgentConfig
from ductor_bot.messenger.commands import (
    DIRECT_COMMANDS,
    MULTIAGENT_COMMANDS,
    ORCHESTRATOR_COMMANDS,
)
from ductor_bot.workspace.init import init_workspace
from ductor_bot.workspace.paths import DuctorPaths

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "build_agent_docs.py"
DEFAULTS = REPO / "ductor_bot" / "_home_defaults" / "workspace"
BUNDLE = DEFAULTS / "docs" / "dt-agent"

pytestmark = pytest.mark.skipif(
    not (REPO / "docs" / "config.md").is_file(), reason="needs the repository docs/ tree"
)


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_agent_docs_under_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


def _slug(heading: str) -> str:
    text = heading.strip().lower().replace("`", "")
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            fence = not fence
        match = None if fence else re.match(r"#{1,6}\s+(.*)", line)
        if match:
            slug = _slug(re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", match.group(1)))
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def test_bundle_matches_docs_and_code() -> None:
    """Fails when docs/, AgentConfig or config_reload changed without rebuilding the bundle."""
    assert builder.main(["--check"]) == 0, "run: python3 scripts/build_agent_docs.py"


def test_history_and_migration_docs_are_not_bundled() -> None:
    names = {p.relative_to(BUNDLE).as_posix() for p in BUNDLE.rglob("*.md")}
    for excluded in ("CHANGES.md", "UPSTREAM.md"):
        assert excluded not in names
    assert {"INDEX.md", "config-reference.md", "commands-reference.md", "config.md"} <= names


def test_config_reference_lists_every_key_with_a_note() -> None:
    rows = {}
    for line in (BUNDLE / "config-reference.md").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\| `([^`]+)` \| `[^`]*` \| `[^`]*` \| (hot|restart) \| (.*) \|$", line)
        if match:
            rows[match.group(1)] = (match.group(2), match.group(3).strip())
    expected = {str(r["path"]) for r in builder.walk_fields(AgentConfig)}
    assert set(rows) == expected
    assert len(rows) >= 150
    assert [k for k, (_, note) in rows.items() if not note] == []
    assert rows["memory_lint.every_hours"][0] == "hot"
    assert rows["transport"][0] == "restart"
    assert rows["heartbeat.group_targets.chat_id"][0] == "hot"  # nested key follows its parent


def test_config_reference_defaults_come_from_the_model() -> None:
    text = (BUNDLE / "config-reference.md").read_text(encoding="utf-8")
    cfg = AgentConfig()
    assert f"| `memory_lint.every_hours` | `int` | `{cfg.memory_lint.every_hours}` |" in text
    assert f"| `persona_setup_prompt` | `bool` | `{json.dumps(cfg.persona_setup_prompt)}` |" in text
    assert '| `memory_lint.report` | `"topic" or "dm" or "off"` |' in text


def test_config_md_documents_every_config_field() -> None:
    """Every AgentConfig field (nested included) needs a row in its model's config.md table."""
    notes = builder.config_doc_notes((REPO / "docs" / "config.md").read_text(encoding="utf-8"))
    missing = [
        str(r["path"])
        for r in builder.walk_fields(AgentConfig)
        if (str(r["model"]), str(r["name"])) not in notes
    ]
    assert missing == [], f"add rows to docs/config.md for: {missing}"


def test_all_bundle_links_and_anchors_resolve() -> None:
    errors: list[str] = []
    for page in sorted(BUNDLE.rglob("*.md")):
        text = re.sub(r"```.*?```", "", page.read_text(encoding="utf-8"), flags=re.DOTALL)
        text = re.sub(r"`[^`\n]*`", "", text)
        for match in re.finditer(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)", text):
            target = match.group(1)
            if re.match(r"[a-z][a-z0-9+.-]*:", target):
                continue
            path_part, _, anchor = target.partition("#")
            dest = (page.parent / path_part).resolve() if path_part else page
            if not dest.is_file():
                errors.append(f"{page.relative_to(BUNDLE)} -> {target}")
            elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
                errors.append(f"{page.relative_to(BUNDLE)} -> {target} (anchor)")
    assert errors == []


def test_index_covers_the_main_topics() -> None:
    index = (BUNDLE / "INDEX.md").read_text(encoding="utf-8")
    for topic in (
        "config key",
        "restart",
        "chat command",
        "host scripts",
        "tool script",
        "cron",
        "webhooks",
        "heartbeat",
        "background tasks",
        "sub-agents",
        "memory",
        "topics",
        "Matrix",
        "troubleshoot",
        "architecture",
    ):
        assert topic.lower() in index.lower(), topic
    linked = set(re.findall(r"\]\(([^)#\s]+\.md)", index))
    assert {"config-reference.md", "commands-reference.md", "automation.md"} <= linked
    assert "memory_search.py --docs" in index


def test_commands_reference_matches_the_code() -> None:
    text = (BUNDLE / "commands-reference.md").read_text(encoding="utf-8")
    commands = DIRECT_COMMANDS | ORCHESTRATOR_COMMANDS | MULTIAGENT_COMMANDS | {"where", "leave"}
    assert [c for c in sorted(commands) if f"`/{c}" not in text] == []
    tool_scripts = [
        p.name
        for p in (DEFAULTS / "tools").glob("*/*.py")
        if not p.name.startswith("_") and p.parent.name != "user_tools"
    ]
    assert [s for s in sorted(tool_scripts) if f"`{s}`" not in text] == []
    scripts = [p.name for p in (REPO / "scripts").glob("*.sh") if not p.name.startswith("_")]
    assert [s for s in sorted(scripts) if f"scripts/{s}" not in text] == []
    for cli in ("ductor status", "ductor agents", "ductor --version"):
        assert cli in text


def test_workspace_rules_point_at_the_reference_without_inlining_it() -> None:
    workspace_rules = (DEFAULTS / "RULES.md").read_text(encoding="utf-8")
    tools_rules = (DEFAULTS / "tools" / "RULES.md").read_text(encoding="utf-8")
    for text in (workspace_rules, tools_rules):
        assert "docs/dt-agent/INDEX.md" in text
        assert "memory_search.py --docs" in text
    assert len(workspace_rules) < 6200  # a pointer, not the manual


def test_search_docs_flag_finds_pages(tmp_path: Path) -> None:
    paths = DuctorPaths(ductor_home=tmp_path / "home")
    init_workspace(paths)
    tool = paths.workspace / "tools" / "memory_tools" / "memory_search.py"
    result = subprocess.run(
        [sys.executable, str(tool), "--docs", "--json", "heartbeat", "quiet", "hours"],
        capture_output=True,
        text=True,
        check=False,
        cwd=paths.workspace,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
    found = [r["path"] for r in json.loads(result.stdout)["results"]]
    assert any(p in found[:5] for p in ("automation.md", "config.md", "config-reference.md"))
    assert "INDEX.md" not in found[:1]
    # the title falls back to the page's first heading
    heading = subprocess.run(
        [sys.executable, str(tool), "--docs", "--json", "commands", "reference"],
        capture_output=True,
        text=True,
        check=False,
        cwd=paths.workspace,
    )
    top = [r["path"] for r in json.loads(heading.stdout)["results"][:3]]
    assert "commands-reference.md" in top


class TestWorkspaceSync:
    def _paths(self, tmp_path: Path) -> DuctorPaths:
        paths = DuctorPaths(ductor_home=tmp_path / "home")
        init_workspace(paths)
        return paths

    def test_installed_and_kept_current(self, tmp_path: Path) -> None:
        paths = self._paths(tmp_path)
        installed = paths.workspace / "docs" / "dt-agent"
        assert (installed / "INDEX.md").read_bytes() == (BUNDLE / "INDEX.md").read_bytes()
        assert (installed / "modules" / "cron.md").is_file()
        index = installed / "INDEX.md"
        index.write_text("stale or hand-edited\n", encoding="utf-8")
        init_workspace(paths)  # what every start does
        assert index.read_bytes() == (BUNDLE / "INDEX.md").read_bytes()

    def test_files_no_longer_shipped_are_removed_user_files_stay(self, tmp_path: Path) -> None:
        paths = self._paths(tmp_path)
        docs = paths.workspace / "docs"
        stale = docs / "dt-agent" / "modules" / "removed_page.md"
        stale.write_text("old\n", encoding="utf-8")
        empty_dir = docs / "dt-agent" / "gone"
        empty_dir.mkdir()
        (empty_dir / "x.md").write_text("old\n", encoding="utf-8")
        mine = docs / "my_notes.md"
        mine.write_text("user file\n", encoding="utf-8")
        other = docs / "project" / "plan.md"
        other.parent.mkdir()
        other.write_text("user plan\n", encoding="utf-8")
        init_workspace(paths)
        assert not stale.exists()
        assert not empty_dir.exists()
        assert mine.read_text() == "user file\n"
        assert other.read_text() == "user plan\n"

    def test_sub_agent_workspaces_get_the_bundle_too(self, tmp_path: Path) -> None:
        main = tmp_path / "home"
        sub = DuctorPaths(ductor_home=main / "agents" / "helper")
        init_workspace(sub)
        assert (sub.workspace / "docs" / "dt-agent" / "INDEX.md").is_file()

    @pytest.mark.parametrize("link", ["workspace", "docs"])
    def test_symlinked_ancestor_never_prunes_external_files(
        self, tmp_path: Path, link: str
    ) -> None:
        paths = self._paths(tmp_path)
        home = paths.ductor_home
        outside = tmp_path / "external"
        target = home / "workspace" if link == "workspace" else home / "workspace" / "docs"
        target.rename(outside)
        target.symlink_to(outside, target_is_directory=True)
        managed = outside / "dt-agent" if link == "docs" else outside / "docs" / "dt-agent"
        stale = managed / "modules" / "removed_page.md"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"external user bytes\n")
        snapshot = {p: p.read_bytes() for p in outside.rglob("*") if p.is_file()}
        init_workspace(paths)
        assert {p: p.read_bytes() for p in outside.rglob("*") if p.is_file()} == snapshot
        assert stale.read_bytes() == b"external user bytes\n"

    def test_symlink_inside_the_managed_tree_is_removed_not_followed(self, tmp_path: Path) -> None:
        paths = self._paths(tmp_path)
        managed = paths.workspace / "docs" / "dt-agent"
        outside = tmp_path / "external"
        outside.mkdir()
        (outside / "keep.md").write_text("external\n", encoding="utf-8")
        (managed / "stray-dir-link").symlink_to(outside, target_is_directory=True)
        (managed / "stray-file-link.md").symlink_to(outside / "keep.md")
        init_workspace(paths)
        assert not (managed / "stray-dir-link").is_symlink()
        assert not (managed / "stray-file-link.md").is_symlink()
        assert (outside / "keep.md").read_text() == "external\n"

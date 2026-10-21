"""memory_search / memory_lint tools shipped in the workspace defaults."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = (
    Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults/workspace/tools/memory_tools"
)


def _run(tool: str, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOLS / tool), "--root", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _page(root: Path, name: str, body: str = "Body.\n", **fm: str) -> None:
    head = "".join(f"{k}: {v}\n" for k, v in fm.items())
    text = f"---\n{head}---\n{body}" if fm else body
    (root / name).write_text(text, encoding="utf-8")


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "memory_system"
    root.mkdir()
    (root / "MAINMEMORY.md").write_text(
        "# Main Memory\n\n## Index\n- [[home_network]] -- LAN and router\n- [[server]] -- box\n",
        encoding="utf-8",
    )
    _page(
        root,
        "home_network.md",
        "Router notes. See [[server]].\n",
        title="Home network",
        type="reference",
        tags="[network]",
        aliases="[LAN, router]",
        updated="2026-01-31",
    )
    _page(root, "server.md", "Machine.\n", title="Server", type="reference", updated="2026-01-31")
    return root


def _issues(root: Path, *args: str) -> dict[str, list[str]]:
    result = _run("memory_lint.py", root, "check", "--json", *args)
    assert result.returncode in (0, 1), result.stderr
    return json.loads(result.stdout)["issues"]


class TestLint:
    def test_clean_wiki_exits_zero(self, wiki: Path) -> None:
        result = _run("memory_lint.py", wiki, "check")
        assert result.returncode == 0
        assert "No problems found." in result.stdout
        data = json.loads(_run("memory_lint.py", wiki, "check", "--json").stdout)
        assert data["ok"] is True
        assert data["problem_count"] == 0
        assert data["limits"]["max_main_lines"] == 100
        assert data["limits"]["max_main_bytes"] == 12288

    def test_missing_and_malformed_frontmatter(self, wiki: Path) -> None:
        _page(wiki, "bare.md", "no frontmatter\n")
        (wiki / "unclosed.md").write_text("---\ntitle: X\nbody\n", encoding="utf-8")
        _page(wiki, "notitle.md", type="reference")
        _page(wiki, "baddate.md", title="Bad date", updated="31/01/2026")
        found = _issues(wiki)
        assert found["missing_frontmatter"] == ["bare.md"]
        joined = "\n".join(found["bad_frontmatter"])
        assert "unclosed.md: frontmatter block is not closed" in joined
        assert "notitle.md: frontmatter has no title" in joined
        assert "baddate.md: updated '31/01/2026'" in joined

    def test_broken_and_ambiguous_links(self, wiki: Path) -> None:
        _page(wiki, "a.md", "See [[nowhere]] and [[dup]].\n", title="A", updated="2026-01-01")
        (wiki / "sub").mkdir()
        _page(wiki, "dup.md", title="Dup one", updated="2026-01-01")
        _page(wiki / "sub", "dup.md", title="Dup two", updated="2026-01-01")
        found = _issues(wiki)
        assert "a.md: [[nowhere]]" in found["broken_links"]
        assert any(item.startswith("a.md: [[dup]] ->") for item in found["ambiguous_links"])
        assert found["duplicate_basenames"] == ["dup: dup.md, sub/dup.md"]

    def test_links_in_code_and_aliases_and_paths(self, wiki: Path) -> None:
        _page(
            wiki,
            "b.md",
            "`[[ignored]]`\n```\n[[ignored2]]\n```\n[[LAN]] [[home_network#Section|label]] [[server.md]]\n",
            title="B",
            updated="2026-01-01",
        )
        assert _issues(wiki)["broken_links"] == []

    def test_page_missing_from_index_and_missing_referenced_file(self, wiki: Path) -> None:
        _page(wiki, "extra.md", title="Extra", updated="2026-01-01")
        main = wiki / "MAINMEMORY.md"
        main.write_text(
            main.read_text() + "- see `ghost.md` and `extra_ref.md`\n", encoding="utf-8"
        )
        found = _issues(wiki)
        assert found["not_in_index"] == ["extra.md"]
        assert found["index_missing_files"] == ["extra_ref.md", "ghost.md"]

    def test_index_reaches_pages_through_hub_chain(self, wiki: Path) -> None:
        (wiki / "MAINMEMORY.md").write_text("[[hub]]\n", encoding="utf-8")
        _page(wiki, "hub.md", "[[page]]\n", title="Hub")
        _page(wiki, "page.md", "[[subpage]]\n", title="Page")
        _page(wiki, "subpage.md", title="Subpage")
        # Existing fixture pages are also reachable through the hub.
        hub = wiki / "hub.md"
        hub.write_text(hub.read_text() + "[[home_network]]\n", encoding="utf-8")
        assert _issues(wiki)["not_in_index"] == []

    def test_unreachable_cycle_stays_outside_index(self, wiki: Path) -> None:
        _page(wiki, "island_one.md", "[[island_two]]\n", title="Island one")
        _page(wiki, "island_two.md", "[[island_one]]\n", title="Island two")
        assert _issues(wiki)["not_in_index"] == ["island_one.md", "island_two.md"]

    def test_reachable_cycle_finishes(self, wiki: Path) -> None:
        _page(wiki, "server.md", "[[home_network]]\n", title="Server")
        assert _issues(wiki)["not_in_index"] == []

    def test_hub_ambiguous_link_reaches_all_candidates(self, wiki: Path) -> None:
        main = wiki / "MAINMEMORY.md"
        main.write_text(main.read_text() + "[[hub]]\n", encoding="utf-8")
        _page(wiki, "hub.md", "[[dup]]\n", title="Hub")
        (wiki / "sub").mkdir()
        _page(wiki, "dup.md", title="First duplicate")
        _page(wiki / "sub", "dup.md", title="Second duplicate")
        found = _issues(wiki)
        assert found["not_in_index"] == []
        assert found["ambiguous_links"] == ["hub.md: [[dup]] -> dup.md, sub/dup.md"]

    def test_index_reaches_page_through_frontmatter_links(self, wiki: Path) -> None:
        _page(wiki, "server.md", title="Server", links="[[extra]]")
        _page(wiki, "extra.md", title="Extra")
        assert _issues(wiki)["not_in_index"] == []

    def test_mainmemory_size_limits_and_shared_block_excluded(self, wiki: Path) -> None:
        main = wiki / "MAINMEMORY.md"
        shared = (
            "--- SHARED KNOWLEDGE START ---\n" + "fact\n" * 500 + "--- SHARED KNOWLEDGE END ---\n"
        )
        main.write_text(main.read_text() + shared, encoding="utf-8")
        assert _issues(wiki)["mainmemory_oversize"] == []
        main.write_text(main.read_text() + "- filler\n" * 120, encoding="utf-8")
        assert len(_issues(wiki)["mainmemory_oversize"]) == 1
        assert (
            _issues(wiki, "--max-main-lines", "1000", "--max-main-bytes", "1000000")[
                "mainmemory_oversize"
            ]
            == []
        )

    def test_missing_mainmemory(self, wiki: Path) -> None:
        (wiki / "MAINMEMORY.md").unlink()
        assert _issues(wiki)["mainmemory_missing"] == ["MAINMEMORY.md"]

    def test_duplicate_titles(self, wiki: Path) -> None:
        _page(wiki, "other.md", title="server", updated="2026-01-01")
        assert len(_issues(wiki)["duplicate_titles"]) == 1

    def test_stale_project_pages_only(self, wiki: Path) -> None:
        _page(wiki, "old_proj.md", title="Old", type="project", updated="2020-01-01")
        _page(wiki, "old_ref.md", title="OldRef", type="reference", updated="2020-01-01")
        found = _issues(wiki)
        assert [s.split(" ")[0] for s in found["stale_project_pages"]] == ["old_proj.md"]
        assert _issues(wiki, "--stale-days", "100000")["stale_project_pages"] == []

    def test_framework_rule_files_and_hidden_dirs_are_not_pages(self, wiki: Path) -> None:
        for name in ("CLAUDE.md", "AGENTS.md", "GEMINI.md", "RULES.md"):
            (wiki / name).write_text("rules, no frontmatter [[nothing]]\n", encoding="utf-8")
        (wiki / ".hidden").mkdir()
        (wiki / ".hidden" / "x.md").write_text("[[nothing]]\n", encoding="utf-8")
        assert _run("memory_lint.py", wiki, "check").returncode == 0

    def test_problems_exit_one_with_readable_summary(self, wiki: Path) -> None:
        _page(wiki, "bare.md", "x\n")
        result = _run("memory_lint.py", wiki, "check")
        assert result.returncode == 1
        assert "Problems:" in result.stdout
        assert "Pages without frontmatter: 1" in result.stdout

    def test_root_option_works_before_and_after_the_subcommand(self, wiki: Path) -> None:
        after = subprocess.run(
            [sys.executable, str(TOOLS / "memory_lint.py"), "check", "--root", str(wiki)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert after.returncode == 0
        assert _run("memory_lint.py", wiki, "check").returncode == 0

    def test_bad_root_exits_two(self, tmp_path: Path) -> None:
        assert _run("memory_lint.py", tmp_path / "missing", "check").returncode == 2

    def test_shipped_template_memory_is_clean(self, tmp_path: Path) -> None:
        template = TOOLS.parent.parent / "memory_system"
        assert _run("memory_lint.py", template, "check").returncode == 0


class TestSearch:
    def test_frontmatter_weighting_and_alias_match(self, wiki: Path) -> None:
        _page(
            wiki,
            "bodyonly.md",
            "router router router mentioned a lot in the body\n",
            title="Misc",
            updated="2026-01-01",
        )
        result = _run("memory_search.py", wiki, "router", "--json")
        assert result.returncode == 0
        paths = [r["path"] for r in json.loads(result.stdout)["results"]]
        assert paths[0] == "home_network.md"  # alias hit beats a body-only page
        assert "bodyonly.md" in paths

    def test_title_beats_body_and_all_terms_bonus(self, wiki: Path) -> None:
        data = json.loads(_run("memory_search.py", wiki, "home", "network", "--json").stdout)
        assert data["results"][0]["path"] == "home_network.md"
        assert data["results"][0]["score"] >= 30 + 30 + 20 + 25

    def test_limit_text_output_and_no_hits(self, wiki: Path) -> None:
        result = _run("memory_search.py", wiki, "server", "-n", "1")
        assert result.returncode == 0
        assert result.stdout.count("\n1. ") + result.stdout.startswith("1. ") >= 0
        assert "Results:" in result.stdout
        miss = _run("memory_search.py", wiki, "zzzznotthere")
        assert "Results: 0" in miss.stdout

    def test_bad_root(self, tmp_path: Path) -> None:
        assert _run("memory_search.py", tmp_path / "nope", "x").returncode == 2


class TestLinksAndCode:
    def _outside(self, tmp_path: Path) -> Path:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("---\ntitle: Secret\n---\nouterterm\n", encoding="utf-8")
        return outside

    def test_links_outside_or_broken_are_skipped_and_reported(
        self, wiki: Path, tmp_path: Path
    ) -> None:
        outside = self._outside(tmp_path)
        (wiki / "escape.md").symlink_to(outside / "secret.md")
        (wiki / "escape_dir").symlink_to(outside, target_is_directory=True)
        (wiki / "broken.md").symlink_to(tmp_path / "nowhere.md")
        result = _run("memory_lint.py", wiki, "check", "--json")
        assert result.returncode == 1, result.stderr
        assert "Traceback" not in result.stderr
        skipped = " ".join(json.loads(result.stdout)["issues"]["skipped_files"])
        assert "escape.md" in skipped
        assert "broken.md" in skipped
        assert "escape_dir" in skipped
        hit = _run("memory_search.py", wiki, "outerterm", "--json")
        assert hit.returncode == 0, hit.stderr
        assert json.loads(hit.stdout)["results"] == []
        other = json.loads(_run("memory_search.py", wiki, "server", "--json").stdout)["results"]
        assert other  # remaining pages stay searchable

    def test_links_inside_the_root_and_a_linked_root_are_fine(
        self, wiki: Path, tmp_path: Path
    ) -> None:
        (wiki / "alias.md").symlink_to(wiki / "server.md")
        assert _issues(wiki)["skipped_files"] == []
        linked_root = tmp_path / "root-link"
        linked_root.symlink_to(wiki, target_is_directory=True)
        assert _issues(linked_root)["skipped_files"] == []
        assert _run("memory_search.py", linked_root, "server", "--json").returncode == 0

    def test_code_terms_are_searchable_but_links_in_code_stay_ignored(self, wiki: Path) -> None:
        _page(
            wiki,
            "tools.md",
            "Use `memory_lint` and `persona_setup_prompt` here.\n\n"
            "```\ncreate_agent.py --docs\n```\n\nExample `[[not_a_link]]`.\n",
            title="Tools",
            updated="2026-01-31",
        )
        for term, snippet in (
            ("memory_lint", "memory_lint"),
            ("persona_setup_prompt", "persona_setup_prompt"),
            ("create_agent.py", "create_agent.py"),
        ):
            data = json.loads(_run("memory_search.py", wiki, term, "--json").stdout)
            hit = [r for r in data["results"] if r["path"] == "tools.md"]
            assert hit, term
            assert snippet in json.dumps(hit[0]), term
        assert "not_a_link" not in " ".join(_issues(wiki)["broken_links"])


def test_tool_sources_contain_no_personal_or_legacy_values() -> None:
    forbidden = (
        "/ductor/workspace",
        "claude-import",
        "metadata.json",
        "build-metadata",
    )
    for path in TOOLS.iterdir():
        if path.suffix not in {".py", ".md"}:
            continue
        text = path.read_text(encoding="utf-8").lower()
        assert not [word for word in forbidden if word in text], path.name
        assert all(ord(ch) < 128 for ch in text), f"{path.name} has non-ASCII text"

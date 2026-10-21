#!/usr/bin/env python3
"""Check the memory_system wiki for structural problems (read-only).

Usage:
    python3 tools/memory_tools/memory_lint.py check [--json] [--root PATH]
        [--max-main-lines N] [--max-main-bytes N] [--stale-days N]

Exit code 0 = no problems, 1 = problems found, 2 = usage/root error.
Never edits or deletes anything; fixing is up to the caller.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path

from memory_parser import (
    DEFAULT_MEMORY_ROOT,
    MAINMEMORY,
    Note,
    Resolver,
    collect_notes,
    strip_shared_blocks,
)

DEFAULT_MAX_MAIN_LINES = 100
DEFAULT_MAX_MAIN_BYTES = 12 * 1024
DEFAULT_STALE_DAYS = 183

MD_REF_RE = re.compile(r"`([\w./-]+\.md)`")

CATEGORIES: dict[str, str] = {
    "mainmemory_missing": "MAINMEMORY.md is missing",
    "mainmemory_oversize": "MAINMEMORY.md is too large (keep essentials plus an index)",
    "missing_frontmatter": "Pages without frontmatter",
    "bad_frontmatter": "Malformed frontmatter (unclosed, no title, bad updated date, bad lists)",
    "broken_links": "Broken [[links]]",
    "ambiguous_links": "Ambiguous [[links]]",
    "not_in_index": "Pages not reachable from the MAINMEMORY index",
    "index_missing_files": "MAINMEMORY references to .md files that do not exist",
    "duplicate_titles": "Pages sharing a title",
    "duplicate_basenames": "Pages sharing a file name",
    "stale_project_pages": "Project pages not updated for a long time",
    "skipped_files": "Files skipped (link outside the root, broken link, unreadable)",
}


def _index_targets(main: Note, resolver: Resolver) -> tuple[set[str], list[str]]:
    """Pages reachable from MAINMEMORY, plus referenced .md files that do not exist."""
    targets: set[str] = set()
    for link in main.links:
        result = resolver.resolve(link, main)
        if result.status == "resolved":
            targets.add(result.relative_path)
        elif result.status == "ambiguous":
            targets.update(result.candidates)
    missing: list[str] = []
    text = strip_shared_blocks(main.raw_text)
    workspace = main.path.parent.parent
    for ref in sorted(set(MD_REF_RE.findall(text))):
        rel = ref.removeprefix("memory_system/")
        if rel in resolver.by_relative:
            targets.add(rel)
        elif not (main.path.parent / rel).exists() and not (workspace / ref).exists():
            missing.append(ref)
    pending = list(targets)
    visited: set[str] = set()
    while pending:
        relative_path = pending.pop()
        if relative_path in visited:
            continue
        visited.add(relative_path)
        note = resolver.by_relative[relative_path]
        for link in note.links:
            result = resolver.resolve(link, note)
            if result.status == "resolved":
                linked = (result.relative_path,)
            elif result.status == "ambiguous":
                linked = result.candidates
            else:
                continue
            for target in linked:
                if target not in targets:
                    targets.add(target)
                    pending.append(target)
    return targets, missing


def _frontmatter_problems(note: Note) -> list[str]:
    if not note.frontmatter_closed:
        return [f"{note.relative_path}: frontmatter block is not closed with ---"]
    problems: list[str] = []
    if not note.title:
        problems.append(f"{note.relative_path}: frontmatter has no title")
    updated = str(note.frontmatter.get("updated", "")).strip()
    if updated:
        try:
            datetime.strptime(updated, "%Y-%m-%d")
        except ValueError:
            problems.append(f"{note.relative_path}: updated '{updated}' is not YYYY-MM-DD")
    for key in ("tags", "aliases"):
        if key in note.frontmatter and not isinstance(note.frontmatter[key], list):
            problems.append(f"{note.relative_path}: {key} should be a list")
    return problems


def check(root: Path, *, max_lines: int, max_bytes: int, stale_days: int) -> dict[str, object]:
    skipped: list[str] = []
    notes = collect_notes(root, skipped)
    resolver = Resolver(notes)
    issues: dict[str, list[str]] = {key: [] for key in CATEGORIES}
    main = next((n for n in notes if n.relative_path == MAINMEMORY), None)
    pages = [n for n in notes if n.relative_path != MAINMEMORY]

    if main is None:
        issues["mainmemory_missing"].append(MAINMEMORY)
    else:
        text = strip_shared_blocks(main.raw_text)
        lines, size = len(text.splitlines()), len(text.encode("utf-8"))
        if lines > max_lines or size > max_bytes:
            issues["mainmemory_oversize"].append(
                f"{MAINMEMORY}: {lines} lines / {size} bytes (limits {max_lines} / {max_bytes}); "
                "move details into topic files and keep an index"
            )
        targets, missing = _index_targets(main, resolver)
        issues["index_missing_files"] = missing
        issues["not_in_index"] = sorted(
            n.relative_path for n in pages if n.relative_path not in targets
        )

    titles: dict[str, list[str]] = {}
    today = date.today()
    for note in pages:
        if not note.has_frontmatter:
            issues["missing_frontmatter"].append(note.relative_path)
        else:
            issues["bad_frontmatter"].extend(_frontmatter_problems(note))
        if note.title:
            titles.setdefault(note.title.casefold(), []).append(note.relative_path)
        if str(note.frontmatter.get("type", "")).strip() == "project":
            updated = str(note.frontmatter.get("updated", "")).strip()
            try:
                age = (today - datetime.strptime(updated, "%Y-%m-%d").date()).days
            except ValueError:
                continue
            if age > stale_days:
                issues["stale_project_pages"].append(
                    f"{note.relative_path} (updated {updated}, {age} days ago)"
                )
    for note in notes:
        for link in note.links:
            result = resolver.resolve(link, note)
            if result.status == "missing":
                issues["broken_links"].append(f"{note.relative_path}: [[{link.link}]]")
            elif result.status == "ambiguous":
                issues["ambiguous_links"].append(
                    f"{note.relative_path}: [[{link.link}]] -> {', '.join(result.candidates)}"
                )
    issues["duplicate_titles"] = [
        f"'{key}': {', '.join(paths)}" for key, paths in sorted(titles.items()) if len(paths) > 1
    ]
    issues["duplicate_basenames"] = [
        f"{stem}: {', '.join(paths)}"
        for stem, paths in sorted(resolver.duplicate_basenames().items())
    ]
    issues["skipped_files"] = skipped
    issues = {key: sorted(set(values)) for key, values in issues.items()}
    count = sum(len(values) for values in issues.values())
    return {
        "ok": count == 0,
        "root": str(root),
        "nodes": len(notes),
        "problem_count": count,
        "limits": {
            "max_main_lines": max_lines,
            "max_main_bytes": max_bytes,
            "stale_days": stale_days,
        },
        "issues": issues,
    }


def render(report: dict[str, object]) -> str:
    issues: dict[str, list[str]] = report["issues"]  # type: ignore[assignment]
    lines = [
        "Memory lint report",
        f"Root: {report['root']}",
        f"Pages scanned: {report['nodes']}",
        f"Problems: {report['problem_count']}",
    ]
    for key, description in CATEGORIES.items():
        values = issues.get(key, [])
        if values:
            lines += ["", f"{description}: {len(values)}"]
            lines += [f"  - {value}" for value in values]
    if report["ok"]:
        lines += ["", "No problems found."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lint the memory_system wiki (read-only).")
    parser.add_argument("--root", default=str(DEFAULT_MEMORY_ROOT), help="memory_system root")
    sub = parser.add_subparsers(dest="command", required=True)
    check_cmd = sub.add_parser("check", help="report problems; exit 1 when any are found")
    check_cmd.add_argument("--root", dest="check_root", default=None, help="memory_system root")
    check_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    check_cmd.add_argument("--max-main-lines", type=int, default=DEFAULT_MAX_MAIN_LINES)
    check_cmd.add_argument("--max-main-bytes", type=int, default=DEFAULT_MAX_MAIN_BYTES)
    check_cmd.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS)
    args = parser.parse_args(argv)

    root = Path(args.check_root or args.root).resolve()
    if not root.is_dir():
        print(f"Error: memory root not found: {root}")
        return 2
    report = check(
        root,
        max_lines=args.max_main_lines,
        max_bytes=args.max_main_bytes,
        stale_days=args.stale_days,
    )
    print(json.dumps(report, ensure_ascii=False) if args.json else render(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

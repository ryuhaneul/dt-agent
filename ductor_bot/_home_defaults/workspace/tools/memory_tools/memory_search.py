#!/usr/bin/env python3
"""Search memory_system pages by keyword (frontmatter-aware).

Usage:
    python3 tools/memory_tools/memory_search.py "home network" [-n 10] [--json] [--root PATH]
    python3 tools/memory_tools/memory_search.py --docs "how do I change heartbeat settings"

Scoring per term: title/basename 30, tags 20, aliases 20, other frontmatter 8, body 3 per
hit (max 10 hits); +10 per matched term, +25 when every term matched. Read only the
pages the results point to; do not grep the workspace recursively.

``--docs`` searches the bundled DT Agent reference (``docs/dt-agent/``: usage, settings,
commands, structure) instead of ``memory_system/``; there a page's first ``# heading`` counts
as its title.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from memory_parser import DEFAULT_MEMORY_ROOT, Note, collect_notes

DOCS_ROOT = DEFAULT_MEMORY_ROOT.parent / "docs" / "dt-agent"


def _heading(note: Note) -> str:
    for line in note.body_text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def line_snippets(text: str, terms: list[str], limit: int = 2) -> list[str]:
    snippets: list[str] = []
    for line in text.splitlines():
        compact = " ".join(line.split())
        if compact and any(term in compact.lower() for term in terms):
            snippets.append(compact[:220])
        if len(snippets) >= limit:
            break
    return snippets


def score_note(note: Note, terms: list[str]) -> tuple[int, list[str]]:
    title_blob = f"{note.file_name} {note.title} {_heading(note)}".lower()
    tags_blob = " ".join(note.tags).lower()
    aliases_blob = " ".join(note.aliases).lower()
    frontmatter_blob = note.raw_frontmatter_text.lower()
    body_blob = note.raw_body_text.lower()
    score = 0
    matched = 0
    for term in terms:
        term_score = 0
        if term in title_blob:
            term_score += 30
        if term in tags_blob:
            term_score += 20
        if term in aliases_blob:
            term_score += 20
        if term in frontmatter_blob:
            term_score += 8
        term_score += min(body_blob.count(term), 10) * 3
        if term_score:
            matched += 1
            score += term_score
    if matched:
        score += matched * 10
    if matched == len(terms):
        score += 25
    return score, line_snippets(f"{note.raw_frontmatter_text}\n{note.raw_body_text}", terms)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Search memory_system markdown pages.")
    parser.add_argument("keywords", nargs="+", help="one or more search keywords")
    parser.add_argument("-n", "--limit", type=int, default=10, help="max results (default 10)")
    parser.add_argument("--root", default=None, help="root to search (default: memory_system)")
    parser.add_argument(
        "--docs", action="store_true", help="search the bundled DT Agent reference (docs/dt-agent)"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else (DOCS_ROOT if args.docs else DEFAULT_MEMORY_ROOT)
    if not root.is_dir():
        print(f"Error: search root not found: {root}", flush=True)
        return 2
    terms = [t.lower() for keyword in args.keywords for t in keyword.split()]
    results = []
    for note in collect_notes(root):
        score, snippets = score_note(note, terms)
        if score > 0:
            results.append({"path": note.relative_path, "score": score, "snippets": snippets})
    results.sort(key=lambda r: (-r["score"], r["path"]))

    if args.json:
        print(json.dumps({"terms": terms, "results": results[: args.limit]}, ensure_ascii=False))
        return 0
    print(f"Search terms: {' '.join(terms)}")
    print(f"Results: {len(results)}")
    for idx, item in enumerate(results[: args.limit], start=1):
        print(f"{idx}. {item['path']} (score {item['score']})")
        for snippet in item["snippets"]:
            print(f"   {snippet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

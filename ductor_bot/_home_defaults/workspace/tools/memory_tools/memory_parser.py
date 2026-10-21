#!/usr/bin/env python3
"""Shared read-only parser for the memory_system wiki (used by memory_search / memory_lint).

Standard library only. Pages are Markdown files under ``memory_system/`` with an optional
``---`` frontmatter block (title, type, tags, aliases, updated, links) and ``[[basename]]``
wiki links.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# <workspace>/tools/memory_tools/memory_parser.py -> <workspace>/memory_system
DEFAULT_MEMORY_ROOT = Path(__file__).resolve().parents[2] / "memory_system"
MAINMEMORY = "MAINMEMORY.md"
# Framework-managed rule files are not wiki pages.
EXCLUDED_FILENAMES = frozenset({"CLAUDE.md", "AGENTS.md", "GEMINI.md", "RULES.md"})

SHARED_START = "--- SHARED KNOWLEDGE START ---"
SHARED_END = "--- SHARED KNOWLEDGE END ---"

WIKILINK_RE = re.compile(r"!?\[\[([^\]\n]+)\]\]")
KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")
BLOCK_ITEM_RE = re.compile(r"^\s*-\s*(.*)$")


@dataclass(frozen=True)
class WikiLink:
    link: str
    target: str


@dataclass
class Note:
    path: Path
    relative_path: str
    file_name: str
    raw_text: str
    scan_text: str
    has_frontmatter: bool
    frontmatter_closed: bool
    frontmatter_text: str
    body_text: str
    # Text as written (code spans/blocks and shared blocks kept) for keyword search.
    raw_frontmatter_text: str = ""
    raw_body_text: str = ""
    frontmatter: dict[str, object] = field(default_factory=dict)
    links: list[WikiLink] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return str(self.frontmatter.get("title", "")).strip()


def strip_shared_blocks(text: str) -> str:
    """Drop the supervisor-managed shared-knowledge block (never edited by agents)."""
    kept: list[str] = []
    in_shared = False
    for line in text.splitlines(keepends=True):
        if SHARED_START in line:
            in_shared = True
        elif SHARED_END in line:
            in_shared = False
        elif not in_shared:
            kept.append(line)
    return "".join(kept)


def _strip_fenced_code(text: str) -> str:
    kept: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            kept.append(line)
    return "".join(kept)


def _strip_inline_code(text: str) -> str:
    return re.sub(r"`[^`\n]*`", " ", text)


def make_scan_text(text: str) -> str:
    """Text used for link extraction: no shared block, fenced code or inline code."""
    return _strip_inline_code(_strip_fenced_code(strip_shared_blocks(text)))


def split_frontmatter(text: str) -> tuple[bool, bool, str, str]:
    """Return ``(has_frontmatter, closed, frontmatter_text, body)``."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return False, False, "", text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return True, True, "".join(lines[1:idx]), "".join(lines[idx + 1 :])
    return True, False, "", text


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_scalar(value: str) -> object:
    value = value.strip()
    if value.startswith("[") and value.endswith("]") and not value.startswith("[["):
        inner = value[1:-1].strip()
        return [_strip_quotes(p.strip()) for p in inner.split(",") if p.strip()] if inner else []
    return _strip_quotes(value)


def parse_frontmatter(frontmatter_text: str) -> dict[str, object]:
    """Parse the small YAML subset used by wiki pages (scalars, inline and block lists)."""
    parsed: dict[str, object] = {}
    lines = frontmatter_text.splitlines()
    i = 0
    while i < len(lines):
        match = KEY_RE.match(lines[i])
        if not match:
            i += 1
            continue
        key, value = match.group(1), match.group(2).strip()
        i += 1
        if value:
            parsed[key] = _parse_scalar(value)
            continue
        items: list[str] = []
        while i < len(lines) and not KEY_RE.match(lines[i]):
            item = BLOCK_ITEM_RE.match(lines[i])
            if item:
                items.append(_strip_quotes(item.group(1).strip()))
            i += 1
        parsed[key] = items
    return parsed


def values_as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def parse_wikilink(raw: str) -> WikiLink:
    """``[[target#anchor|label]]`` -> link target ``target`` (no ``.md``)."""
    link = raw.strip()
    target = link.split("|", 1)[0]
    for sep in ("#", "^"):
        target = target.split(sep, 1)[0]
    target = target.strip()
    if target.endswith(".md"):
        target = target[:-3]
    return WikiLink(link=link, target=target)


def extract_wikilinks(text: str) -> list[WikiLink]:
    return [parse_wikilink(m.group(1)) for m in WIKILINK_RE.finditer(text)]


def _frontmatter_links(frontmatter_text: str) -> list[WikiLink]:
    return extract_wikilinks(_strip_inline_code(frontmatter_text)) if frontmatter_text else []


def _markdown_files(root: Path, skipped: list[str]) -> list[Path]:
    """Markdown files under *root* without following directory links; a file link counts only
    when it resolves to a regular file inside *root*. Others are reported in *skipped*."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if not name.endswith(".md") or name.startswith(".") or name in EXCLUDED_FILENAMES:
                continue
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                target = path.resolve()
                if not target.is_file() or not target.is_relative_to(root):
                    skipped.append(f"{rel}: link outside the root or broken")
                    continue
            found.append(path)
        for name in dirnames:
            if (Path(dirpath) / name).is_symlink():
                skipped.append(
                    f"{(Path(dirpath) / name).relative_to(root).as_posix()}/: directory link not followed"
                )
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def collect_notes(root: Path = DEFAULT_MEMORY_ROOT, skipped: list[str] | None = None) -> list[Note]:
    """Read every wiki page under *root* (excluding hidden dirs and framework rule files).

    Only regular files that live inside *root* are read; broken or outside links and
    unreadable files are left out and described in *skipped* (when given).
    """
    root = root.resolve()
    skipped = skipped if skipped is not None else []
    notes: list[Note] = []
    for path in _markdown_files(root, skipped):
        rel = path.relative_to(root)
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            skipped.append(f"{rel.as_posix()}: unreadable ({exc.__class__.__name__})")
            continue
        scan = make_scan_text(raw)
        has_fm, closed, fm_text, body = split_frontmatter(scan)
        _raw_has, _raw_closed, raw_fm, raw_body = split_frontmatter(raw)
        frontmatter = parse_frontmatter(fm_text) if closed else {}
        notes.append(
            Note(
                path=path,
                relative_path=rel.as_posix(),
                file_name=path.stem,
                raw_text=raw,
                scan_text=scan,
                has_frontmatter=has_fm,
                frontmatter_closed=closed,
                frontmatter_text=fm_text,
                body_text=body,
                raw_frontmatter_text=raw_fm,
                raw_body_text=raw_body,
                frontmatter=frontmatter,
                links=extract_wikilinks(body) + _frontmatter_links(fm_text),
                aliases=values_as_list(frontmatter.get("aliases")),
                tags=[t.lstrip("#").lower() for t in values_as_list(frontmatter.get("tags"))],
            )
        )
    return notes


@dataclass(frozen=True)
class Resolved:
    status: str  # "resolved" | "missing" | "ambiguous"
    relative_path: str = ""
    candidates: tuple[str, ...] = ()


class Resolver:
    """Resolve ``[[basename]]`` / ``[[dir/name]]`` / alias links to pages."""

    def __init__(self, notes: list[Note]) -> None:
        self.by_relative = {n.relative_path: n for n in notes}
        self.by_stem: dict[str, list[Note]] = {}
        self.by_alias: dict[str, list[Note]] = {}
        for note in notes:
            self.by_stem.setdefault(note.file_name, []).append(note)
            for alias in note.aliases:
                self.by_alias.setdefault(alias.strip(), []).append(note)

    def duplicate_basenames(self) -> dict[str, list[str]]:
        return {
            stem: sorted(n.relative_path for n in group)
            for stem, group in self.by_stem.items()
            if len(group) > 1
        }

    def resolve(self, link: WikiLink, source: Note | None = None) -> Resolved:
        target = link.target.lstrip("/")
        if not target and source is not None:
            return Resolved("resolved", source.relative_path)
        if "/" in target:
            rel = target if target.endswith(".md") else f"{target}.md"
            return Resolved("resolved", rel) if rel in self.by_relative else Resolved("missing")
        found: dict[str, Note] = {}
        for note in self.by_stem.get(target, []) + self.by_alias.get(target, []):
            found[note.relative_path] = note
        if len(found) == 1:
            return Resolved("resolved", next(iter(found)))
        if found:
            return Resolved("ambiguous", candidates=tuple(sorted(found)))
        return Resolved("missing")

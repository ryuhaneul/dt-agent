#!/usr/bin/env python3
"""Build the agent-facing DT Agent reference bundle shipped inside the package.

Writes ``ductor_bot/_home_defaults/workspace/docs/dt-agent/`` (synced into every agent
workspace as framework-managed files):

* a copy of the user/module/config docs from ``docs/`` (history and migration records are
  left out, links that leave the bundle are turned into plain text),
* ``config-reference.md`` generated from the ``AgentConfig`` model (every key, nested keys
  included: type, default, hot-reload behavior, one-line note taken from ``docs/config.md``).

``INDEX.md`` and ``commands-reference.md`` are maintained by hand in the bundle directory.

Usage (repository root, development environment)::

    python3 scripts/build_agent_docs.py            # rewrite the bundle
    python3 scripts/build_agent_docs.py --check    # exit 1 when the bundle is stale
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import types
import typing
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DOCS = REPO / "docs"
BUNDLE = REPO / "ductor_bot" / "_home_defaults" / "workspace" / "docs" / "dt-agent"
HAND_WRITTEN = frozenset({"INDEX.md", "commands-reference.md"})
GENERATED_CONFIG_REFERENCE = "config-reference.md"

# docs/ pages that describe usage, structure and configuration (not history or migration).
INCLUDED = (
    "architecture.md",
    "automation.md",
    "config.md",
    "installation.md",
    "matrix-setup.md",
    "system_overview.md",
)
INCLUDED_DIRS = ("modules",)

_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)")
_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|(.*)\|\s*$")
_HEADING_RE = re.compile(r"^#{2,4}\s+`([^`]+)`")
_NOTE_MAX = 220


# -- docs bundle -----------------------------------------------------------------------


def bundle_sources() -> list[Path]:
    files = [DOCS / name for name in INCLUDED]
    for directory in INCLUDED_DIRS:
        files += sorted((DOCS / directory).glob("*.md"))
    return files


def _rewrite_links(text: str, source: Path, included: set[Path]) -> str:
    """Keep links between bundled pages; turn every other link into plain text."""

    def repl(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        if re.match(r"[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
            return match.group(0)  # URL or in-page anchor
        path_part = target.split("#", 1)[0]
        resolved = (source.parent / path_part).resolve()
        if resolved in included:
            return match.group(0)
        try:
            shown = resolved.relative_to(REPO).as_posix()
        except ValueError:
            shown = path_part
        return label if label.strip("` ") == shown else f"{label} (`{shown}`)"

    return _LINK_RE.sub(repl, text)


def build_bundle() -> dict[str, str]:
    """Return ``{relative path inside the bundle: content}`` for generated + copied files."""
    sources = bundle_sources()
    included = {p.resolve() for p in sources}
    out: dict[str, str] = {}
    for path in sources:
        rel = path.relative_to(DOCS).as_posix()
        out[rel] = _rewrite_links(path.read_text(encoding="utf-8"), path, included)
    out[GENERATED_CONFIG_REFERENCE] = render_config_reference()
    return out


# -- config reference -------------------------------------------------------------------


def _models_in(annotation: object) -> list[type]:
    from pydantic import BaseModel

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type] = []
    for arg in typing.get_args(annotation):
        found += _models_in(arg)
    return found


def type_label(annotation: object) -> str:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Literal:
        return " or ".join(json.dumps(a) for a in args)
    if origin in (typing.Union, types.UnionType):
        return " or ".join("null" if a is type(None) else type_label(a) for a in args)
    if origin is not None:
        return (
            f"{getattr(origin, '__name__', str(origin))}[{', '.join(type_label(a) for a in args)}]"
        )
    if annotation is type(None):
        return "null"
    return getattr(annotation, "__name__", str(annotation))


def default_label(field: object) -> str:
    from pydantic_core import PydanticUndefined

    default = field.default  # type: ignore[attr-defined]
    if default is PydanticUndefined:
        factory = field.default_factory  # type: ignore[attr-defined]
        if factory is None:
            return "(required)"
        default = factory()
    if hasattr(type(default), "model_fields"):
        return "(nested keys below)"
    if isinstance(default, list) and any(hasattr(type(item), "model_fields") for item in default):
        return f"[{len(default)} default item(s)]"
    text = json.dumps(default, ensure_ascii=False, default=str)
    return text if len(text) <= 60 else text[:57] + "..."


def walk_fields(
    model: type, prefix: str = "", seen: tuple[type, ...] = ()
) -> list[dict[str, object]]:
    """Flatten *model* into ``{path, model, name, annotation, field}`` records."""
    rows: list[dict[str, object]] = []
    for name, field in model.model_fields.items():  # type: ignore[attr-defined]
        rows.append(
            {
                "path": f"{prefix}{name}",
                "model": model.__name__,
                "name": name,
                "annotation": field.annotation,
                "field": field,
            }
        )
        for sub in _models_in(field.annotation):
            if sub not in seen:
                rows += walk_fields(sub, f"{prefix}{name}.", (*seen, model))
    return rows


def config_doc_notes(text: str) -> dict[tuple[str, str], str]:
    """``{(model name, key): note}`` from the tables of ``docs/config.md``."""
    notes: dict[tuple[str, str], str] = {}
    model = ""
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            model = heading.group(1).split()[0]
            continue
        row = _ROW_RE.match(line)
        if row and model:
            cells = [c.strip() for c in row.group(2).split("|")]
            note = cells[-1] if cells else ""
            notes.setdefault((model, row.group(1)), note)
    return notes


def documented_keys(config_md: str) -> set[tuple[str, str]]:
    return set(config_doc_notes(config_md))


def _one_line(note: str) -> str:
    note = " ".join(note.replace("|", "/").split())
    return note if len(note) <= _NOTE_MAX else note[: _NOTE_MAX - 3] + "..."


def render_config_reference() -> str:
    from ductor_bot.config import AgentConfig
    from ductor_bot.config_reload import _HOT_RELOADABLE

    notes = config_doc_notes((DOCS / "config.md").read_text(encoding="utf-8"))
    lines = [
        "# Configuration reference (generated)",
        "",
        "Every key of `config/config.json` (`AgentConfig`), nested keys included. Generated from the",
        "code by `scripts/build_agent_docs.py`; the Note column comes from `config.md`, which has the",
        "full explanation. **Hot reload**: `hot` = applied within seconds without a restart; `restart`",
        "= needs `/restart` (application restart, not a Compose rebuild). Nested keys follow their",
        "top-level key.",
        "",
        "| Key | Type | Default | Hot reload | Note |",
        "|---|---|---|---|---|",
    ]
    for row in walk_fields(AgentConfig):
        path = str(row["path"])
        top = path.split(".", 1)[0]
        hot = "hot" if top in _HOT_RELOADABLE else "restart"
        note = _one_line(notes.get((str(row["model"]), str(row["name"])), ""))
        lines.append(
            f"| `{path}` | `{type_label(row['annotation'])}` | `{default_label(row['field'])}` "
            f"| {hot} | {note} |"
        )
    return "\n".join(lines) + "\n"


# -- driver -----------------------------------------------------------------------------


def current_bundle() -> dict[str, str]:
    if not BUNDLE.is_dir():
        return {}
    return {
        p.relative_to(BUNDLE).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(BUNDLE.rglob("*.md"))
        if p.relative_to(BUNDLE).as_posix() not in HAND_WRITTEN
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the bundle is stale")
    args = parser.parse_args(argv)
    wanted = build_bundle()
    have = current_bundle()
    if args.check:
        stale = sorted(k for k in wanted.keys() | have.keys() if wanted.get(k) != have.get(k))
        for key in stale:
            print(f"stale: {key}")
        return 1 if stale else 0
    for rel in sorted(have.keys() - wanted.keys()):
        (BUNDLE / rel).unlink()
    for rel, text in wanted.items():
        target = BUNDLE / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    print(f"wrote {len(wanted)} files to {BUNDLE.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""i18n completeness checker.

Produces a single-pane-of-glass report of translation gaps across all locales,
using English as the source of truth.

Usage:
    python -m ductor_bot.i18n.check
    python -m ductor_bot.i18n.check --root /path/to/i18n
    python -m ductor_bot.i18n.check --quiet  # exit-code only, no output

Exit codes:
    0 — every non-en locale has every en key, matching placeholders, no stale keys
    1 — at least one gap exists
    2 — invocation error (bad path, missing en locale, ...)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from ductor_bot.i18n import LANGUAGES
from ductor_bot.i18n.loader import _load_toml

_DEFAULT_ROOT = Path(__file__).resolve().parent
_DOMAINS: tuple[str, ...] = ("chat", "cli", "commands", "wizard")
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(text))


@dataclass(frozen=True)
class PlaceholderMismatch:
    """One key whose en and locale placeholder sets differ."""

    key: str
    en_only: frozenset[str]
    locale_only: frozenset[str]


@dataclass
class DomainReport:
    """Gaps for a single (locale, domain) cell."""

    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    placeholder_mismatches: list[PlaceholderMismatch] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.missing and not self.extra and not self.placeholder_mismatches

    @property
    def total_issues(self) -> int:
        return len(self.missing) + len(self.extra) + len(self.placeholder_mismatches)


@dataclass
class LocaleReport:
    """Gaps for a single locale across all domains."""

    locale: str
    domains: dict[str, DomainReport] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return all(d.clean for d in self.domains.values())

    @property
    def total_issues(self) -> int:
        return sum(d.total_issues for d in self.domains.values())


@dataclass
class Report:
    """Full cross-locale report."""

    root: Path
    locales: list[LocaleReport] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return all(loc.clean for loc in self.locales)


def _load_domain(root: Path, locale: str, domain: str) -> dict[str, str]:
    """Load one {locale}/{domain}.toml, flattened to dotted keys."""
    return _load_toml(root / locale / f"{domain}.toml")


def _compare_domain(en: dict[str, str], tr: dict[str, str]) -> DomainReport:
    en_keys = set(en)
    tr_keys = set(tr)
    report = DomainReport(
        missing=sorted(en_keys - tr_keys),
        extra=sorted(tr_keys - en_keys),
    )
    for key in sorted(en_keys & tr_keys):
        en_ph = _placeholders(en[key])
        tr_ph = _placeholders(tr[key])
        if en_ph != tr_ph:
            report.placeholder_mismatches.append(
                PlaceholderMismatch(
                    key=key,
                    en_only=frozenset(en_ph - tr_ph),
                    locale_only=frozenset(tr_ph - en_ph),
                )
            )
    return report


def build_report(root: Path = _DEFAULT_ROOT, locales: list[str] | None = None) -> Report:
    """Compare every non-en locale against en across all domains."""
    if not (root / "en").is_dir():
        msg = f"English source-of-truth locale not found under {root}"
        raise FileNotFoundError(msg)
    target_locales = (
        locales if locales is not None else [lang for lang in LANGUAGES if lang != "en"]
    )
    en_data = {domain: _load_domain(root, "en", domain) for domain in _DOMAINS}
    report = Report(root=root)
    for locale in target_locales:
        if locale == "en":
            continue
        loc_report = LocaleReport(locale=locale)
        for domain in _DOMAINS:
            tr = _load_domain(root, locale, domain)
            loc_report.domains[domain] = _compare_domain(en_data[domain], tr)
        report.locales.append(loc_report)
    return report


def _format_matrix(report: Report) -> list[str]:
    """Render the summary matrix rows."""
    lines: list[str] = [
        "## Summary matrix (missing + extra + placeholder mismatches)",
        "",
        "| locale | " + " | ".join(_DOMAINS) + " | total |",
        "|" + "|".join(["---"] * (len(_DOMAINS) + 2)) + "|",
    ]
    for loc in report.locales:
        cells = [str(loc.domains[d].total_issues) for d in _DOMAINS]
        lines.append(f"| {loc.locale} | " + " | ".join(cells) + f" | {loc.total_issues} |")
    lines.append("")
    return lines


def _format_domain_detail(domain: str, locale: str, d: DomainReport) -> list[str]:
    """Render the per-domain detail block for one locale."""
    lines: list[str] = [f"**{domain}.toml** - {d.total_issues} issue(s)", ""]
    if d.missing:
        lines.append(f"- missing ({len(d.missing)}):")
        lines.extend(f"    - `{k}`" for k in d.missing)
    if d.extra:
        lines.append(f"- extra / stale ({len(d.extra)}):")
        lines.extend(f"    - `{k}`" for k in d.extra)
    if d.placeholder_mismatches:
        lines.append(f"- placeholder mismatch ({len(d.placeholder_mismatches)}):")
        for pm in d.placeholder_mismatches:
            only_en = ", ".join(f"{{{p}}}" for p in sorted(pm.en_only)) or "-"
            only_loc = ", ".join(f"{{{p}}}" for p in sorted(pm.locale_only)) or "-"
            lines.append(f"    - `{pm.key}`: en-only {only_en} | {locale}-only {only_loc}")
    lines.append("")
    return lines


def _format_locale_detail(loc: LocaleReport) -> list[str]:
    """Render the detail block for one locale (all its non-clean domains)."""
    lines: list[str] = [f"### {loc.locale}", ""]
    for domain in _DOMAINS:
        d = loc.domains[domain]
        if d.clean:
            continue
        lines.extend(_format_domain_detail(domain, loc.locale, d))
    return lines


def format_report(report: Report) -> str:
    """Render the report as a Markdown-style document."""
    lines: list[str] = [
        "# i18n completeness report",
        "",
        f"Source of truth: `en`  -  Root: `{report.root}`",
        "",
    ]
    lines.extend(_format_matrix(report))

    if report.clean:
        lines.append("All locales fully synced with en. No gaps.")
        lines.append("")
        return "\n".join(lines)

    lines.append("## Details")
    lines.append("")
    for loc in report.locales:
        if loc.clean:
            continue
        lines.extend(_format_locale_detail(loc))
    return "\n".join(lines)


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ductor_bot.i18n.check",
        description="Report translation gaps across all locales (en = source of truth).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=_DEFAULT_ROOT,
        help="Directory containing per-locale subfolders (default: ductor_bot/i18n).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress output; use exit code only.",
    )
    args = parser.parse_args(argv)
    stream = out if out is not None else sys.stdout

    try:
        report = build_report(root=args.root)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        print(format_report(report), file=stream)

    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())

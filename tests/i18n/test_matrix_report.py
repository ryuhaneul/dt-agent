"""Tests for the i18n completeness checker (ductor_bot.i18n.check)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from ductor_bot.i18n.check import (
    DomainReport,
    LocaleReport,
    Report,
    _compare_domain,
    _placeholders,
    build_report,
    format_report,
    main,
)

# -- _placeholders -------------------------------------------------------------


def test_placeholders_none() -> None:
    assert _placeholders("plain text") == set()


def test_placeholders_multiple() -> None:
    assert _placeholders("hello {name}, you have {count} msgs") == {"name", "count"}


def test_placeholders_duplicate_deduped() -> None:
    assert _placeholders("{x} {x} {y}") == {"x", "y"}


# -- _compare_domain -----------------------------------------------------------


def test_compare_domain_identical() -> None:
    en = {"a": "hi", "b": "hello {name}"}
    tr = {"a": "hola", "b": "hola {name}"}
    report = _compare_domain(en, tr)
    assert report.clean
    assert report.total_issues == 0


def test_compare_domain_missing_key() -> None:
    en = {"a": "hi", "b": "bye"}
    tr = {"a": "hola"}
    report = _compare_domain(en, tr)
    assert report.missing == ["b"]
    assert not report.extra
    assert not report.placeholder_mismatches


def test_compare_domain_extra_key() -> None:
    en = {"a": "hi"}
    tr = {"a": "hola", "stale": "garbage"}
    report = _compare_domain(en, tr)
    assert not report.missing
    assert report.extra == ["stale"]


def test_compare_domain_placeholder_mismatch() -> None:
    en = {"a": "hi {name}"}
    tr = {"a": "hola {nombre}"}
    report = _compare_domain(en, tr)
    assert len(report.placeholder_mismatches) == 1
    pm = report.placeholder_mismatches[0]
    assert pm.key == "a"
    assert pm.en_only == frozenset({"name"})
    assert pm.locale_only == frozenset({"nombre"})


def test_compare_domain_placeholder_matching_even_if_reordered() -> None:
    en = {"a": "{x} then {y}"}
    tr = {"a": "{y} primero, luego {x}"}
    report = _compare_domain(en, tr)
    assert report.clean


# -- build_report over live tree ----------------------------------------------


def test_build_report_live_tree_clean() -> None:
    """The live repo tree MUST be fully synced with en. This is the golden gate."""
    report = build_report()
    assert report.clean, format_report(report)


def test_build_report_covers_all_non_en_locales() -> None:
    """All non-en locales from LANGUAGES must appear in the report."""
    from ductor_bot.i18n import LANGUAGES

    report = build_report()
    expected = {lang for lang in LANGUAGES if lang != "en"}
    assert {loc.locale for loc in report.locales} == expected


def test_build_report_missing_en_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_report(root=tmp_path)


# -- build_report with synthetic tree (known-gap scenario) ---------------------


def _write_locale(root: Path, locale: str, files: dict[str, str]) -> None:
    d = root / locale
    d.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (d / f"{name}.toml").write_text(content, encoding="utf-8")


def test_build_report_detects_synthetic_gaps(tmp_path: Path) -> None:
    _write_locale(
        tmp_path,
        "en",
        {
            "chat": 'greeting = "Hello {name}"\nfarewell = "Bye"',
            "cli": 'start = "Starting"',
            "commands": 'help = "Show help"',
            "wizard": 'step = "Step {n}"',
        },
    )
    _write_locale(
        tmp_path,
        "xx",
        {
            # chat: missing 'farewell', placeholder mismatch on 'greeting'
            "chat": 'greeting = "Hola {nombre}"',
            # cli: extra/stale key
            "cli": 'start = "Arrancando"\nstale_key = "garbage"',
            # commands: fully synced
            "commands": 'help = "Mostrar ayuda"',
            # wizard: missing entirely (empty)
            "wizard": "",
        },
    )
    report = build_report(root=tmp_path, locales=["xx"])
    assert not report.clean
    assert len(report.locales) == 1
    loc = report.locales[0]
    assert loc.locale == "xx"

    chat = loc.domains["chat"]
    assert chat.missing == ["farewell"]
    assert not chat.extra
    assert len(chat.placeholder_mismatches) == 1
    assert chat.placeholder_mismatches[0].key == "greeting"

    cli = loc.domains["cli"]
    assert cli.extra == ["stale_key"]
    assert not cli.missing

    cmds = loc.domains["commands"]
    assert cmds.clean

    wizard = loc.domains["wizard"]
    assert wizard.missing == ["step"]


def test_build_report_locale_filter_respected(tmp_path: Path) -> None:
    _write_locale(tmp_path, "en", {"chat": 'a = "x"'})
    _write_locale(tmp_path, "only", {"chat": 'a = "x"'})
    report = build_report(root=tmp_path, locales=["only"])
    assert [loc.locale for loc in report.locales] == ["only"]


def test_build_report_skips_en_in_locales_arg(tmp_path: Path) -> None:
    _write_locale(tmp_path, "en", {"chat": 'a = "x"'})
    report = build_report(root=tmp_path, locales=["en"])
    assert report.locales == []


# -- format_report -------------------------------------------------------------


def test_format_report_clean_tree_says_synced(tmp_path: Path) -> None:
    _write_locale(tmp_path, "en", {"chat": 'a = "x"'})
    _write_locale(tmp_path, "de", {"chat": 'a = "y"'})
    report = build_report(root=tmp_path, locales=["de"])
    output = format_report(report)
    assert "All locales fully synced with en" in output
    assert "| de |" in output


def test_format_report_details_list_gaps(tmp_path: Path) -> None:
    _write_locale(tmp_path, "en", {"chat": 'a = "x"\nb = "y {foo}"'})
    _write_locale(tmp_path, "de", {"chat": 'a = "x"\nb = "y {bar}"\nstale = "z"'})
    report = build_report(root=tmp_path, locales=["de"])
    output = format_report(report)
    assert "### de" in output
    assert "**chat.toml**" in output
    assert "stale" in output
    assert "`b`" in output  # placeholder mismatch line references key
    assert "{foo}" in output
    assert "{bar}" in output


# -- main() CLI entrypoint -----------------------------------------------------


def test_main_returns_zero_on_clean_tree() -> None:
    buf = io.StringIO()
    rc = main(argv=[], out=buf)
    assert rc == 0
    assert "i18n completeness report" in buf.getvalue()


def test_main_returns_one_on_gaps(tmp_path: Path) -> None:
    _write_locale(tmp_path, "en", {"chat": 'a = "x"\nb = "y"'})
    _write_locale(tmp_path, "de", {"chat": 'a = "x"'})
    # Trick: ductor_bot.i18n.LANGUAGES has many locales; use --root and rely on
    # the fact that only 'de' exists here (others yield empty dicts -> all keys
    # reported missing for every other language). We still expect rc=1.
    buf = io.StringIO()
    rc = main(argv=["--root", str(tmp_path)], out=buf)
    assert rc == 1


def test_main_quiet_suppresses_output(tmp_path: Path) -> None:
    _write_locale(tmp_path, "en", {"chat": 'a = "x"'})
    _write_locale(tmp_path, "de", {"chat": 'a = "x"'})
    # Only de present; others will be reported as fully missing but we don't care
    # here — we're asserting that --quiet suppresses stdout.
    buf = io.StringIO()
    main(argv=["--root", str(tmp_path), "--quiet"], out=buf)
    assert buf.getvalue() == ""


def test_main_bad_root_returns_two(tmp_path: Path) -> None:
    empty = tmp_path / "no_en_here"
    empty.mkdir()
    buf = io.StringIO()
    rc = main(argv=["--root", str(empty)], out=buf)
    assert rc == 2


# -- Dataclasses: clean / total_issues invariants -----------------------------


def test_domain_report_clean_and_counts() -> None:
    d = DomainReport()
    assert d.clean
    assert d.total_issues == 0
    d.missing.append("k")
    assert not d.clean
    assert d.total_issues == 1


def test_locale_report_aggregates() -> None:
    loc = LocaleReport(locale="xx")
    loc.domains["chat"] = DomainReport(missing=["a", "b"])
    loc.domains["cli"] = DomainReport()
    assert not loc.clean
    assert loc.total_issues == 2


def test_report_clean_requires_all_clean() -> None:
    r = Report(root=Path("/tmp"))
    r.locales.append(LocaleReport(locale="xx"))
    r.locales[0].domains["chat"] = DomainReport()
    assert r.clean
    r.locales[0].domains["chat"].missing.append("k")
    assert not r.clean

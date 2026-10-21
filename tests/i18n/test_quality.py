"""Language-table checks for the translation files (see docs/modules/i18n.md)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "ductor_bot" / "i18n"
LOCALES = ["de", "es", "fr", "id", "ko", "nl", "pt", "ru"]
ALL_LOCALES = ["en", *LOCALES]

# One term for "reasoning effort" per locale, used by all six keys below.
EFFORT_TERM = {
    "en": "reasoning effort",
    "de": "denkaufwand",
    "es": "esfuerzo de razonamiento",
    "fr": "effort de raisonnement",
    "id": "upaya penalaran",
    "ko": "추론 강도",
    "nl": "redeneerinzet",
    "pt": "esforço de raciocínio",
    "ru": "уровень рассуждений",
}
EFFORT_KEYS = [
    ("chat", "model.thinking_level"),
    ("chat", "model.reasoning_line"),
    ("chat", "model.effort_updated"),
    ("chat", "status.effort_line"),
    ("chat", "effort.select"),
    ("commands", "bot.effort"),
]
# Sentences that used to be left in English in some locales.
TRANSLATED_SENTENCES = [
    ("chat", "session.recovery_failed", ["de", "es", "fr", "id", "nl", "pt", "ru"]),
    ("chat", "recovery.named_header", ["de", "fr", "nl", "pt"]),
    ("commands", "bot.restart", ["id"]),
    ("chat", "welcome.hint_info", ["nl"]),
    ("cli", "help.ductor", LOCALES),
]
# Second person: informal for every locale except id (Anda). A bot instruction must not use
# the formal form of the locale; ``welcome.prompt_*`` / ``welcome.btn_*`` are spoken by the
# user to the agent and are excluded.
FORMAL = {
    "fr": re.compile(r"\b(vous|votre|vos)\b|\b(Exécutez|Choisissez|Démarrez|Lancez)\b"),
    "ru": re.compile(r"\b[Вв]ы\b|\b[Вв]аш\w*\b|\b(Выберите|Запустите|Выполните|Используйте)\b"),
    "ko": re.compile(r"(습니다|십시오|합니다|입니다|됩니다|니까)"),
    "id": re.compile(r"\b[Kk]amu\b"),
}
USER_SPEECH = re.compile(r"^welcome\.(prompt_|btn_)")


def _flat(data: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out.update(_flat(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


def _load(locale: str, domain: str) -> dict[str, str]:
    with (ROOT / locale / f"{domain}.toml").open("rb") as handle:
        return _flat(tomllib.load(handle))


@pytest.mark.parametrize("locale", ALL_LOCALES)
def test_reasoning_effort_uses_one_term(locale: str) -> None:
    for domain, key in EFFORT_KEYS:
        value = _load(locale, domain)[key].casefold()
        assert EFFORT_TERM[locale] in value, f"{locale} {domain}:{key}: {value!r}"


@pytest.mark.parametrize(("domain", "key", "locales"), TRANSLATED_SENTENCES)
def test_sentences_are_translated(domain: str, key: str, locales: list[str]) -> None:
    english = _load("en", domain)[key]
    for locale in locales:
        assert _load(locale, domain)[key] != english, f"{locale} {domain}:{key}"


@pytest.mark.parametrize("locale", sorted(FORMAL))
def test_bot_text_keeps_the_locale_register(locale: str) -> None:
    pattern = FORMAL[locale]
    for domain in ("chat", "cli", "commands"):
        for key, value in _load(locale, domain).items():
            if USER_SPEECH.match(key):
                continue
            assert not pattern.search(value), f"{locale} {domain}:{key}: {value!r}"


def test_tool_help_and_guides_use_the_reasoning_effort_term() -> None:
    """The agent-facing tool help and guides name the setting the same way as the product."""
    home = ROOT.parent / "_home_defaults" / "workspace"
    stale = [
        str(path.relative_to(home))
        for folder in ("tools", "cron_tasks")
        for path in sorted((home / folder).rglob("*"))
        if path.suffix in {".py", ".md"}
        and re.search(r"(?<![-\w])thinking level", path.read_text(encoding="utf-8").casefold())
    ]
    assert stale == []

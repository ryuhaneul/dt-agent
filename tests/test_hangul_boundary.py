"""Korean text may appear only in the places listed here; everything else is English.

Product code, comments, logs, agent prompts, docs and scripts are English. Korean is kept where it
is the content: translations, the Korean README, tests that assert Korean output or use Korean
input samples, and a few functional data lists. The check covers every tracked text file; a
Unicode escape that spells Hangul counts as Hangul.

What is checked, and what is not:

* A marked region is accepted only for a (file, purpose) pair listed in ``REGION_ALLOWED``; a
  marker in any other file, or with another purpose, is itself a violation.
* A file listed in ``TEST_ALLOWED`` or ``DOC_ALLOWED`` is accepted as a whole: Hangul in any string,
  docstring or text there passes, whatever it is used for (this test cannot tell an assertion on
  Korean output from other Korean text). The one narrowing is that Hangul in a Python comment of a
  test file is still a violation; comments are English.

This file writes every Hangul range as an escape so that it contains no Hangul itself. It is
skipped by the scan (``SELF``) because it has to name the ranges and its samples spell the
region markers.
"""

from __future__ import annotations

import fnmatch
import io
import os
import re
import subprocess
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Hangul Jamo, Compatibility Jamo, Jamo Extended-A/B and Syllables.
HANGUL = re.compile("[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7a3\ud7b0-\ud7ff]")
# A written-out escape (\uXXXX, \UXXXXXXXX, \N{HANGUL ...}) for one of those characters.
ESCAPE = re.compile(
    r"\\u(?:11[0-9a-fA-F]{2}|31[3-8][0-9a-fA-F]|a9[67][0-9a-fA-F]|[aA][c-fC-F][0-9a-fA-F]{2}"
    r"|[b-cB-C][0-9a-fA-F]{3}|[dD][0-7][0-9a-fA-F]{2}|[dD][7][b-fB-F][0-9a-fA-F])"
    r"|\\U0000(?:11[0-9a-fA-F]{2}|31[3-8][0-9a-fA-F]|a9[67][0-9a-fA-F]|[aA][c-fC-F][0-9a-fA-F]{2}"
    r"|[b-cB-C][0-9a-fA-F]{3}|[dD][0-7][0-9a-fA-F]{2})"
    r"|\\N\{HANGUL",
)
BEGIN = re.compile(r"#\s*hangul-allowed-begin:\s*(\S.*)")
END = re.compile(r"#\s*hangul-allowed-end\b")

SELF = "tests/test_hangul_boundary.py"
BINARY_SUFFIXES = {".jpeg", ".jpg", ".png", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".lock"}

# Whole files that are Korean by purpose.
FILE_ALLOWED = {
    "ductor_bot/i18n/ko/*.toml": "Korean translations",
    "scripts/onboard_i18n/ko.json": "Korean onboarding texts",
    "README.ko.md": "Korean README",
}
# Marked regions: (file, purpose written after the begin marker).
REGION_ALLOWED = {
    (
        "ductor_bot/bus/failure_reason.py",
        "reasons stored by earlier versions, read as the English ones",
    ),
    ("ductor_bot/i18n/__init__.py", "the language's own name"),
    (
        "ductor_bot/messenger/telegram/topic_naming.py",
        "Korean examples, verdict words and greetings that the title filter knows",
    ),
    (
        "ductor_bot/messenger/telegram/topic_naming.py",
        "invisible Hangul filler characters are dropped from titles",
    ),
    ("ductor_bot/messenger/telegram/topic_naming.py", "Korean label prefix"),
    ("ductor_bot/messenger/telegram/topic_naming.py", "Korean acknowledgements are not topics"),
    ("scripts/onboard.sh", "Korean preflight messages of onboard.sh"),
    ("scripts/_home.sh", "Korean messages of _home.sh"),
    ("scripts/login.sh", "Korean messages of login.sh"),
    ("scripts/logout.sh", "Korean messages of logout.sh"),
    ("scripts/rebuild.sh", "Korean messages of rebuild.sh"),
    ("scripts/status.sh", "Korean messages of status.sh"),
    ("scripts/update.sh", "Korean messages of update.sh"),
}
# Single lines: (file, text that must be on the line).
LINE_ALLOWED = {
    ("README.md", "[\ud55c\uad6d\uc5b4](README.ko.md)"): "language switch link label",
}
# Documents that quote Korean wording as their content.
DOC_ALLOWED = {
    "docs/modules/i18n.md": "language table: Korean terms and register",
    "ductor_bot/_home_defaults/workspace/docs/dt-agent/modules/i18n.md": "bundled copy of the above",
}
# Tests that assert Korean output or use Korean sample input (they are not product text).
TEST_ALLOWED = {
    "tests/bus/test_failure_reason.py": "reasons stored by earlier versions, Korean display",
    "tests/cli/test_claude_provider.py": "Korean slot-switch and cooldown prefixes",
    "tests/cli/test_slot_rotation.py": "Korean slot-switch prefix",
    "tests/cli/test_log_redact.py": "non-ASCII secret masking sample",
    "tests/heartbeat/test_execution.py": "Korean heartbeat stop notice",
    "tests/i18n/test_quality.py": "Korean terms and register checks",
    "tests/infra/test_agent_reason.py": "Korean rendering of exclusion reasons",
    "tests/orchestrator/test_commands.py": "Korean labels that /status must not show",
    "tests/messenger/telegram/test_edit_streaming.py": "Korean completion summary",
    "tests/messenger/telegram/test_memory_lint_report.py": "Korean topic name",
    "tests/messenger/telegram/test_sender.py": "Korean selector prompt (locale output)",
    "tests/messenger/telegram/test_topic_naming*.py": "Korean topic-title input samples and verdicts",
    "scripts/tests/test_home.py": "non-ASCII home path sample",
    "scripts/tests/test_login.py": "non-ASCII config value sample",
    "scripts/tests/test_onboard_preflight.py": "Korean preflight output",
    "scripts/tests/test_onboard_messages.py": "checks that only the Korean message file has Hangul",
}
ALLOWED_PATTERNS = {**FILE_ALLOWED, **DOC_ALLOWED, **TEST_ALLOWED}


def _matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern)


def _read(root: Path, path: str) -> str | None:
    file = root / path
    if file.suffix in BINARY_SUFFIXES or not file.is_file():
        return None
    data = file.read_bytes()
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _comment_violations(path: str, text: str) -> list[str]:
    """Hangul in the comments of a Python test file."""
    found = []
    try:
        found.extend(
            f"{path}:{token.start[0]}: Hangul in a comment: {token.string[:60]}"
            for token in tokenize.generate_tokens(io.StringIO(text).readline)
            if token.type == tokenize.COMMENT
            and (HANGUL.search(token.string) or ESCAPE.search(token.string))
        )
    except (tokenize.TokenError, IndentationError, SyntaxError):
        found.append(f"{path}: cannot read the comments")
    return found


def find_violations(root: Path, tracked: list[str]) -> list[str]:  # noqa: C901, PLR0912
    """Every Hangul character or Hangul escape outside the allowed places."""
    found = []
    for path in tracked:
        text = _read(root, path)
        if text is None or path == SELF:
            continue
        if any(_matches(path, p) for p in ALLOWED_PATTERNS):
            if path.endswith(".py") and any(_matches(path, p) for p in TEST_ALLOWED):
                found.extend(_comment_violations(path, text))
            continue
        inside = False
        begin_line = 0
        for number, line in enumerate(text.splitlines(), 1):
            begun = BEGIN.search(line)
            if begun:
                if inside:
                    found.append(f"{path}:{number}: allowed region begins inside another one")
                if (path, begun.group(1).strip()) not in REGION_ALLOWED:
                    found.append(
                        f"{path}:{number}: allowed region is not approved: {begun.group(1)[:60]}"
                    )
                inside, begin_line = True, number
                continue
            if END.search(line):
                if not inside:
                    found.append(f"{path}:{number}: allowed region ends without a beginning")
                inside = False
                continue
            hit = HANGUL.search(line) or ESCAPE.search(line)
            if not hit or inside:
                continue
            if any(file == path and fragment in line for file, fragment in LINE_ALLOWED):
                continue
            found.append(f"{path}:{number}: {line.strip()[:80]}")
        if inside:
            found.append(f"{path}:{begin_line}: allowed region is never closed")
    return found


def _tracked() -> list[str]:
    env = {
        name: value for name, value in os.environ.items()
        if name not in {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"}
        and not name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, env=env, capture_output=True, check=False)
    if result.returncode != 0:
        pytest.skip("not a git checkout")
    return [name for name in result.stdout.decode().split("\0") if name]


def test_no_korean_outside_the_allowed_places() -> None:
    assert find_violations(ROOT, _tracked()) == []


def test_every_allowance_is_still_needed() -> None:
    tracked = _tracked()
    for pattern, reason in ALLOWED_PATTERNS.items():
        hits = [p for p in tracked if _matches(p, pattern)]
        assert hits, f"{pattern} ({reason}) matches no tracked file"
        assert any(
            (text := _read(ROOT, p)) and (HANGUL.search(text) or ESCAPE.search(text)) for p in hits
        ), f"{pattern} ({reason}) has no Korean any more; remove the allowance"
    for (file, fragment), reason in LINE_ALLOWED.items():
        text = _read(ROOT, file) or ""
        assert fragment in text, f"{file}: {reason} - line gone; remove the allowance"
    for file, purpose in REGION_ALLOWED:
        text = _read(ROOT, file) or ""
        assert any(
            (m := BEGIN.search(line)) and m.group(1).strip() == purpose
            for line in text.splitlines()
        ), f"{file}: region '{purpose}' is gone; remove the allowance"


# -- the check itself: allowed and forbidden samples ---------------------------------------------


def _tree(tmp_path: Path, files: dict[str, str]) -> list[str]:
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    return list(files)


KOREAN = "\ud55c\uad6d\uc5b4"
PREFLIGHT = "Korean preflight messages of onboard.sh"


def test_allowed_places_pass(tmp_path: Path) -> None:
    tracked = _tree(
        tmp_path,
        {
            "ductor_bot/i18n/ko/chat.toml": f'a = "{KOREAN}"\n',
            "scripts/onboard_i18n/ko.json": f'{{"a": "{KOREAN}"}}\n',
            "README.ko.md": f"{KOREAN}\n",
            "README.md": f"[{KOREAN}](README.ko.md) \u00b7 English\n",
            "scripts/onboard.sh": f"# hangul-allowed-begin: {PREFLIGHT}\necho {KOREAN}\n# hangul-allowed-end\n",
            "scripts/tests/test_home.py": f'x = "{KOREAN}"  # English comment\n',
            "tests/messenger/telegram/test_topic_naming_x.py": f'x = "{KOREAN}"\n',
            "tests/test_other.py": "x = 1\n",
        },
    )
    assert find_violations(tmp_path, tracked) == []


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("ductor_bot/a.py", f'MESSAGE = "{KOREAN}"\n'),
        ("ductor_bot/b.py", f"# {KOREAN}\n"),
        ("ductor_bot/c.py", f'logger.info("{KOREAN}")\n'),
        ("docs/guide.md", f"{KOREAN}\n"),
        ("scripts/run.sh", f"echo {KOREAN}\n"),
        ("tests/test_new.py", f'assert "{KOREAN}"\n'),
        ("README.md", f"{KOREAN} text without the link\n"),
        ("ductor_bot/d.py", 'TEXT = "\\uc548\\ub155"\n'),
        ("ductor_bot/e.py", 'TEXT = "\\U0000d55c"\n'),
        ("ductor_bot/f.py", 'TEXT = "\\N{HANGUL SYLLABLE GA}"\n'),
        ("ductor_bot/g.json", '{"a": "\\ud55c"}\n'),
        ("ductor_bot/h.py", "# hangul-allowed-begin: x\n# never closed\n"),
        ("ductor_bot/i.py", "# hangul-allowed-end\n"),
        ("ductor_bot/j.py", f"# hangul-allowed-begin: x\n# hangul-allowed-end\nx = '{KOREAN}'\n"),
        ("ductor_bot/k.py", "# hangul-allowed-begin:\nx = 1\n# hangul-allowed-end\ny = '\ud55c'\n"),
        # A region marker only counts for an approved file and purpose.
        (
            "ductor_bot/l.py",
            f"# hangul-allowed-begin: {PREFLIGHT}\nx = '{KOREAN}'\n# hangul-allowed-end\n",
        ),
        (
            "scripts/onboard.sh",
            f"# hangul-allowed-begin: some other purpose\necho {KOREAN}\n# hangul-allowed-end\n",
        ),
        # Allowed test files may hold Korean strings, not Korean comments.
        ("scripts/tests/test_home.py", f"x = 1  # {KOREAN}\n"),
        ("tests/cli/test_log_redact.py", f"# {KOREAN}\ndef test_x(): ...\n"),
    ],
)
def test_forbidden_samples_fail(tmp_path: Path, name: str, text: str) -> None:
    tracked = _tree(tmp_path, {name: text})
    assert find_violations(tmp_path, tracked), name


def test_binary_and_non_utf8_files_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "image.png").write_bytes(KOREAN.encode())
    (tmp_path / "data.bin").write_bytes(b"\0" + KOREAN.encode())
    (tmp_path / "latin.txt").write_bytes(b"\xff\xfe")
    assert find_violations(tmp_path, ["image.png", "data.bin", "latin.txt"]) == []

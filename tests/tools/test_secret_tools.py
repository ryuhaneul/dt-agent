"""The secret-aware tools: GitHub-only push and credential helper, form fill, one-time codes."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import ModuleType

import pytest

DEFAULTS = Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults"
TOOLS = DEFAULTS / "workspace/tools"
GIT_PUSH = TOOLS / "git_push.py"
HELPER = TOOLS / "git_credential_from_ductor_env.sh"
FILL = TOOLS / "browser_tools/fill_secret.py"
TOTP = TOOLS / "github_tools/totp.py"
REAL_GIT = shutil.which("git") or "git"

TOKEN = "dummy-github-token-value"
OTHER_TOKEN = "dummy-other-token-value"
SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # the RFC 6238 test key "12345678901234567890"
FORM_VALUE = "dummy-form\"value'x"
BROWSER_TOKEN = "dummy-browser-token"
SECRET_ENV_NAMES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "DUCTOR_SECRETS_FILE",
    "BROWSER_URL",
    "BROWSER_TOKEN",
)
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}


@pytest.fixture(autouse=True)
def isolated_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name in {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"} or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            monkeypatch.delenv(name)


def _base_env(home: Path, store: Path | None = None, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV_NAMES}
    env.update(HOME=str(home), GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    env.update(GIT_IDENTITY)
    if store is not None:
        env["DUCTOR_SECRETS_FILE"] = str(store)
    env.update(extra)
    return env


def _run(
    command: list[str], env: dict[str, str], *, cwd: Path, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        env=env,
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _store(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# -- git_push.py ------------------------------------------------------------------------------

FAKE_GIT = r"""#!/usr/bin/env python3
# Stands in for git: only the push is faked, nothing touches the network.
import json, os, pathlib, subprocess, sys
record = pathlib.Path(__RECORD__)
real_git = __REAL_GIT__
args = sys.argv[1:]
if "push" not in args:
    os.execv(real_git, [real_git, *args])
(record / "argv").write_text("\n".join(args) + "\n")
askpass = os.environ["GIT_ASKPASS"]
(record / "askpass_path").write_text(askpass)
for name, prompt in [
    ("user", "Username for 'https://github.com': "),
    ("password", "Password for 'https://x-access-token@github.com': "),
    ("other", "Password for 'https://evil.invalid': "),
    ("lookalike", "Username for 'https://github.com.evil.invalid': "),
]:
    result = subprocess.run([askpass, prompt], capture_output=True, text=True)
    (record / name).write_text(result.stdout if result.returncode == 0 else "REFUSED\n")
(record / "prompt").write_text(os.environ["GIT_TERMINAL_PROMPT"] + "\n")
options = args[args.index("-c"):args.index("push")]
matched = {}
for key in ["http.sslVerify", "http.proxy"]:
    result = subprocess.run([real_git, *options, "config", "--get-urlmatch", key, args[-2]],
                            capture_output=True, text=True)
    matched[key] = result.stdout.strip() if result.returncode == 0 else None
(record / "matched").write_text(json.dumps(matched))
print("remote: token " + (record / "password").read_text().strip())
rc = record / "push_rc"
sys.exit(int(rc.read_text()) if rc.exists() else 0)
"""

@pytest.fixture
def push_env(tmp_path: Path) -> dict[str, object]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    record = tmp_path / "record"
    record.mkdir()
    fake.write_text(FAKE_GIT.replace("__RECORD__", repr(str(record))).replace("__REAL_GIT__", repr(REAL_GIT)), encoding="utf-8")
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    store = _store(tmp_path / "secrets.env", f"GITHUB_TOKEN={TOKEN}\n")
    env = _base_env(
        home,
        store,
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    )
    return {"env": env, "record": record, "tmp": tmp_path, "store": store}


def _repo(tmp: Path, url: str, push_url: str | None = None) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        **GIT_IDENTITY,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    for args in (
        ["init", "-q", "-b", "main"],
        ["commit", "-q", "--allow-empty", "-m", "start"],
        ["remote", "add", "origin", url],
    ):
        subprocess.run([REAL_GIT, "-C", str(repo), *args], env=env, check=True, capture_output=True)
    if push_url:
        subprocess.run(
            [REAL_GIT, "-C", str(repo), "remote", "set-url", "--push", "origin", push_url],
            env=env,
            check=True,
            capture_output=True,
        )
    return repo


ALLOWED_URLS = [
    "https://github.com/owner/repo.git",
    "https://github.com/owner/repo",
]
REJECTED_URLS = [
    "https://github.com.evil.invalid/owner/repo.git",
    "https://evil.invalid/github.com/owner/repo.git",
    "https://notgithub.com/owner/repo.git",
    "https://gist.github.com/owner/repo.git",
    "http://github.com/owner/repo.git",
    "git@github.com:owner/repo.git",
    "ssh://git@github.com/owner/repo.git",
    "git://github.com/owner/repo.git",
    "https://other.example.invalid/owner/repo.git",
    "https://user:pass@github.com/owner/repo.git",
    "https://user@github.com/owner/repo.git",
    "https://github.com@evil.invalid/owner/repo.git",
    "https://github.com:8443/owner/repo.git",
    "https://github.com:443/owner/repo.git",
    "https://GitHub.com/owner/repo.git",
    "/srv/local/repo.git",
]


class TestGitPush:
    @pytest.mark.parametrize("url", ALLOWED_URLS)
    def test_github_https_url_gets_the_token_without_argv(self, push_env: dict, url: str) -> None:
        repo = _repo(push_env["tmp"], url)
        result = _run(
            [sys.executable, str(GIT_PUSH), str(repo)], push_env["env"], cwd=push_env["tmp"]
        )
        record: Path = push_env["record"]
        assert result.returncode == 0, result.stderr
        argv = (record / "argv").read_text()
        assert TOKEN not in argv
        sha = _run([REAL_GIT, "-C", str(repo), "rev-parse", "HEAD"], push_env["env"], cwd=push_env["tmp"]).stdout.strip()
        assert f"push\n--no-verify\n--no-recurse-submodules\n{url}\n{sha}:refs/heads/main" in argv
        assert "credential.helper=" in argv
        assert (record / "user").read_text().strip() == "x-access-token"
        assert (record / "password").read_text().strip() == TOKEN
        assert (record / "other").read_text().strip() == "REFUSED"
        assert (record / "lookalike").read_text().strip() == "REFUSED"
        assert (record / "prompt").read_text().strip() == "0"
        assert not Path((record / "askpass_path").read_text()).exists()
        assert TOKEN not in result.stdout + result.stderr
        assert "<token>" in result.stdout

    @pytest.mark.parametrize("url", REJECTED_URLS)
    def test_other_targets_get_no_token_and_no_push(self, push_env: dict, url: str) -> None:
        repo = _repo(push_env["tmp"], url)
        result = _run(
            [sys.executable, str(GIT_PUSH), str(repo)], push_env["env"], cwd=push_env["tmp"]
        )
        assert result.returncode != 0
        assert not (push_env["record"] / "argv").exists()
        assert TOKEN not in result.stdout + result.stderr

    def test_the_push_url_is_what_counts(self, push_env: dict) -> None:
        repo = _repo(
            push_env["tmp"],
            "https://github.com/owner/repo.git",
            "https://evil.invalid/owner/repo.git",
        )
        result = _run(
            [sys.executable, str(GIT_PUSH), str(repo)], push_env["env"], cwd=push_env["tmp"]
        )
        assert result.returncode != 0
        assert not (push_env["record"] / "argv").exists()

    def test_exit_code_of_git_is_kept_and_branch_is_passed(self, push_env: dict) -> None:
        repo = _repo(push_env["tmp"], "https://github.com/owner/repo.git")
        env = push_env["env"]
        _run([REAL_GIT, "-C", str(repo), "branch", "feature"], env, cwd=push_env["tmp"])
        (push_env["record"] / "push_rc").write_text("3")
        result = _run(
            [sys.executable, str(GIT_PUSH), str(repo), "feature"], env, cwd=push_env["tmp"]
        )
        assert result.returncode == 3
        sha = _run([REAL_GIT, "-C", str(repo), "rev-parse", "refs/heads/feature"], env, cwd=push_env["tmp"]).stdout.strip()
        assert f"https://github.com/owner/repo.git\n{sha}:refs/heads/feature" in (push_env["record"] / "argv").read_text()

    def test_token_order_and_missing_token(self, push_env: dict) -> None:
        repo = _repo(push_env["tmp"], "https://github.com/owner/repo.git")
        _store(push_env["store"], f"GITHUB_TOKEN={OTHER_TOKEN}\nGH_TOKEN={TOKEN}\n")
        result = _run(
            [sys.executable, str(GIT_PUSH), str(repo)], push_env["env"], cwd=push_env["tmp"]
        )
        assert result.returncode == 0
        assert (push_env["record"] / "password").read_text().strip() == TOKEN
        shutil.rmtree(push_env["record"])
        push_env["record"].mkdir()
        _store(push_env["store"], "# empty\n")
        result = _run(
            [sys.executable, str(GIT_PUSH), str(repo)], push_env["env"], cwd=push_env["tmp"]
        )
        assert result.returncode != 0
        assert not (push_env["record"] / "argv").exists()

    def test_environment_comes_before_the_store(self, push_env: dict) -> None:
        repo = _repo(push_env["tmp"], "https://github.com/owner/repo.git")
        _store(push_env["store"], f"GH_TOKEN={OTHER_TOKEN}\n")
        push_env["env"]["GITHUB_TOKEN"] = TOKEN
        result = _run(
            [sys.executable, str(GIT_PUSH), str(repo)], push_env["env"], cwd=push_env["tmp"]
        )
        assert result.returncode == 0, result.stderr
        assert (push_env["record"] / "password").read_text().strip() == TOKEN

    def test_a_branch_cannot_look_like_an_option(self, push_env: dict) -> None:
        repo = _repo(push_env["tmp"], "https://github.com/owner/repo.git")
        result = _run(
            [sys.executable, str(GIT_PUSH), str(repo), "--force"],
            push_env["env"],
            cwd=push_env["tmp"],
        )
        assert result.returncode != 0
        assert not (push_env["record"] / "argv").exists()

    def test_not_a_repository(self, push_env: dict) -> None:
        result = _run(
            [sys.executable, str(GIT_PUSH), str(push_env["tmp"] / "nothing")],
            push_env["env"],
            cwd=push_env["tmp"],
        )
        assert result.returncode != 0


# -- the git credential helper ----------------------------------------------------------------


def _helper(
    tmp: Path, operation: str, request: str, store_text: str | None = None, **environment: str
) -> subprocess.CompletedProcess[str]:
    store = tmp / "helper-store.env"
    if store_text is None:
        store.unlink(missing_ok=True)
    else:
        _store(store, store_text)
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    env = _base_env(home, store, PATH=os.environ["PATH"], **environment)
    return _run(["sh", str(HELPER), operation], env, cwd=tmp, stdin=request)


class TestCredentialHelper:
    GET_REQUESTS = [
        ("protocol=https\nhost=github.com\n\n", True),
        ("protocol=https\nhost=github.com\npath=owner/repo.git\nusername=someone\n\n", True),
        ("protocol=https\r\nhost=github.com\r\n\r\n", True),
        ("protocol=https\nhost=github.com\n", True),  # no blank line at the end
        ("protocol=http\nhost=github.com\n\n", False),
        ("protocol=https\nhost=github.com.evil.invalid\n\n", False),
        ("protocol=https\nhost=evil.invalid\n\n", False),
        ("protocol=https\nhost=gist.github.com\n\n", False),
        ("protocol=https\nhost=github.com:8443\n\n", False),
        ("protocol=ssh\nhost=github.com\n\n", False),
        ("host=github.com\n\n", False),
        ("protocol=https\n\n", False),
        ("", False),
    ]

    @pytest.mark.parametrize(("request_text", "answers"), GET_REQUESTS)
    def test_get_answers_for_github_https_only(
        self, tmp_path: Path, request_text: str, answers: bool
    ) -> None:
        result = _helper(tmp_path, "get", request_text, f"GH_TOKEN={TOKEN}\n")
        assert result.returncode == 0
        if answers:
            assert result.stdout == f"username=x-access-token\npassword={TOKEN}\n"
        else:
            assert result.stdout == ""
            assert TOKEN not in result.stderr

    @pytest.mark.parametrize("operation", ["store", "erase", "unknown", ""])
    def test_other_operations_do_nothing(self, tmp_path: Path, operation: str) -> None:
        result = _helper(
            tmp_path, operation, "protocol=https\nhost=github.com\n\n", f"GH_TOKEN={TOKEN}\n"
        )
        assert (result.returncode, result.stdout) == (0, "")

    def test_help_has_no_side_effects(self, tmp_path: Path) -> None:
        for flag in ("--help", "-h"):
            result = _helper(tmp_path, flag, "", f"GH_TOKEN={TOKEN}\n")
            assert result.returncode == 0
            assert "credential helper" in result.stdout.lower()
            assert TOKEN not in result.stdout + result.stderr

    def test_a_non_empty_environment_credential_wins(self, tmp_path: Path) -> None:
        request = "protocol=https\nhost=github.com\n\n"
        store = f"GH_TOKEN={OTHER_TOKEN}\nGITHUB_TOKEN={OTHER_TOKEN}\n"
        assert (
            f"password={TOKEN}\n" in _helper(tmp_path, "get", request, store, GH_TOKEN=TOKEN).stdout
        )
        assert (
            f"password={TOKEN}\n"
            in _helper(tmp_path, "get", request, store, GITHUB_TOKEN=TOKEN).stdout
        )
        assert (
            f"password={OTHER_TOKEN}\n"
            in _helper(tmp_path, "get", request, store, GH_TOKEN="").stdout
        )

    def test_store_forms(self, tmp_path: Path) -> None:
        request = "protocol=https\nhost=github.com\n\n"
        for text, expected in (
            (f'GH_TOKEN="{TOKEN}"\n', TOKEN),
            (f"GITHUB_TOKEN='{TOKEN}'\n", TOKEN),
            (f"  GH_TOKEN = {TOKEN}  \r\n", TOKEN),
            (f"GITHUB_TOKEN={OTHER_TOKEN}\nGH_TOKEN={TOKEN}\n", TOKEN),  # GH_TOKEN comes first
            (f"# GH_TOKEN={OTHER_TOKEN}\nGITHUB_TOKEN={TOKEN}\n", TOKEN),
            (f"GH_TOKEN=\nGITHUB_TOKEN={TOKEN}\n", TOKEN),
            ("GH_TOKEN=a=b\n", "a=b"),
        ):
            out = _helper(tmp_path, "get", request, text).stdout
            assert out == f"username=x-access-token\npassword={expected}\n", text

    REFERENCE_STORES = [
        f"GH_TOKEN=$GITHUB_TOKEN\nGITHUB_TOKEN={TOKEN}\n",
        f"GITHUB_TOKEN={TOKEN}\nGH_TOKEN=$GITHUB_TOKEN\n",
        f'GH_TOKEN="$GITHUB_TOKEN"\nGITHUB_TOKEN={TOKEN}\n',
        f"GH_TOKEN=$MY_API_KEY\nMY_API_KEY={TOKEN}\n",
        f"MY_API_KEY={TOKEN}\nGITHUB_TOKEN=$MY_API_KEY\n",
        "GH_TOKEN=$NOT_THERE\n",
        f"GH_TOKEN=$A\nA=$B\nB={TOKEN}\n",
        f"B={TOKEN}\nA=$B\nGH_TOKEN=$A\n",
        "GH_TOKEN=$GH_TOKEN\n",
        "GH_TOKEN=$\n",
        f"GH_TOKEN=\nGITHUB_TOKEN=$GH_TOKEN\nOTHER={TOKEN}\n",
        f"GH_TOKEN=a$GITHUB_TOKEN\nGITHUB_TOKEN={TOKEN}\n",
        f"GH_TOKEN={OTHER_TOKEN}\nGH_TOKEN={TOKEN}\n",
        f"GH_TOKEN={TOKEN}\nGH_TOKEN=\nGITHUB_TOKEN={OTHER_TOKEN}\n",
        f"GITHUB_TOKEN={OTHER_TOKEN}\nGH_TOKEN=$GITHUB_TOKEN\nGITHUB_TOKEN={TOKEN}\n",
        f"GH_TOKEN={OTHER_TOKEN}\nGITHUB_TOKEN={OTHER_TOKEN}\nGITHUB_TOKEN={TOKEN}\nGH_TOKEN=\n",
        f'GH_TOKEN=""{TOKEN}""\n',
        f"GH_TOKEN=''{TOKEN}''\n",
        f"GH_TOKEN=\"'{TOKEN}'\"\n",
        f"GH_TOKEN='\"{TOKEN}\"'\n",
        f'GH_TOKEN="{TOKEN}\n',
        f"GH_TOKEN={TOKEN}'\n",
        'GH_TOKEN=" spaced "\n',
        "GH_TOKEN=\"\"\nGITHUB_TOKEN=''\n",
    ]

    @pytest.mark.parametrize("text", REFERENCE_STORES)
    def test_references_resolve_as_the_loader_does(self, tmp_path: Path, text: str) -> None:
        spec = importlib.util.spec_from_file_location("_secrets_parity", TOOLS / "_secrets.py")
        assert spec is not None
        assert spec.loader is not None
        loader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loader)
        store = _store(tmp_path / "parity.env", text)
        saved = dict(os.environ)
        try:
            for name in (*SECRET_ENV_NAMES, "MY_API_KEY", "A", "B", "OTHER", "NOT_THERE"):
                os.environ.pop(name, None)
            os.environ["DUCTOR_SECRETS_FILE"] = str(store)
            loaded = loader.load_secrets("GH_TOKEN", "GITHUB_TOKEN")
        finally:
            os.environ.clear()
            os.environ.update(saved)
        expected = loaded.get("GH_TOKEN") or loaded.get("GITHUB_TOKEN") or ""
        out = _helper(tmp_path, "get", "protocol=https\nhost=github.com\n\n", text).stdout
        assert out == (f"username=x-access-token\npassword={expected}\n" if expected else "")

    @pytest.mark.parametrize("text", REFERENCE_STORES[:5])
    def test_a_reference_reaches_the_token(self, tmp_path: Path, text: str) -> None:
        out = _helper(tmp_path, "get", "protocol=https\nhost=github.com\n\n", text).stdout
        assert out == f"username=x-access-token\npassword={TOKEN}\n"

    def test_an_environment_credential_still_wins_over_a_reference(self, tmp_path: Path) -> None:
        text = f"GH_TOKEN=$GITHUB_TOKEN\nGITHUB_TOKEN={TOKEN}\n"
        out = _helper(
            tmp_path, "get", "protocol=https\nhost=github.com\n\n", text, GH_TOKEN=OTHER_TOKEN
        ).stdout
        assert out == f"username=x-access-token\npassword={OTHER_TOKEN}\n"

    def test_a_value_is_never_evaluated(self, tmp_path: Path) -> None:
        marker = tmp_path / "evaluated"
        for value in (
            f"$(touch {marker})",
            f"`touch {marker}`",
            f"$(touch {marker}) $GITHUB_TOKEN",
        ):
            text = f"GH_TOKEN={value}\nGITHUB_TOKEN={TOKEN}\n"
            out = _helper(tmp_path, "get", "protocol=https\nhost=github.com\n\n", text).stdout
            assert out == f"username=x-access-token\npassword={value}\n"
            assert not marker.exists()

    @pytest.mark.parametrize("text", [None, "", "OTHER=1\n", "GH_TOKEN=\n", "NOT_GH_TOKEN=x\n"])
    def test_nothing_to_answer_with(self, tmp_path: Path, text: str | None) -> None:
        result = _helper(tmp_path, "get", "protocol=https\nhost=github.com\n\n", text)
        assert (result.returncode, result.stdout) == (0, "")

    def test_git_credential_fill(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        env = _base_env(
            home,
            tmp_path / "absent.env",
            PATH=os.environ["PATH"],
            GH_TOKEN=TOKEN,
            GIT_TERMINAL_PROMPT="0",
        )
        command = [REAL_GIT, "-c", f"credential.helper={HELPER}", "credential", "fill"]
        github = _run(command, env, cwd=tmp_path, stdin="protocol=https\nhost=github.com\n\n")
        assert github.returncode == 0, github.stderr
        assert "username=x-access-token" in github.stdout
        assert f"password={TOKEN}" in github.stdout
        other = _run(command, env, cwd=tmp_path, stdin="protocol=https\nhost=evil.invalid\n\n")
        assert other.returncode != 0
        assert TOKEN not in other.stdout + other.stderr

    def test_git_credential_fill_resolves_a_store_reference(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        store = _store(tmp_path / "fill.env", f"GH_TOKEN=$GITHUB_TOKEN\nGITHUB_TOKEN={TOKEN}\n")
        env = _base_env(home, store, PATH=os.environ["PATH"], GIT_TERMINAL_PROMPT="0")
        command = [REAL_GIT, "-c", f"credential.helper={HELPER}", "credential", "fill"]
        github = _run(command, env, cwd=tmp_path, stdin="protocol=https\nhost=github.com\n\n")
        assert github.returncode == 0, github.stderr
        assert "username=x-access-token" in github.stdout
        assert f"password={TOKEN}\n" in github.stdout


# -- fill_secret.py ---------------------------------------------------------------------------


class _Browser(BaseHTTPRequestHandler):
    mode = "ok"
    requests: list[dict]

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(
            {"path": self.path, "token": self.headers.get("X-Browser-Token"), "body": body}
        )
        if self.headers.get("X-Browser-Token") != BROWSER_TOKEN:
            status, payload = 401, {"error": "unauthorized"}
        elif body["expression"] == "location.origin":
            status, payload = 200, {"result": "https://example.com", "insecure": False}
        elif type(self).mode == "missing":
            status, payload = 200, {"result": -1}
        elif type(self).mode == "error":
            status, payload = 500, {"error": "page.evaluate failed near: " + body["expression"]}
        elif type(self).mode == "junk":
            status, payload = 200, {"result": "unexpected"}
        else:
            status, payload = 200, {"result": len(FORM_VALUE)}
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture
def browser() -> Iterator[type[_Browser]]:
    _Browser.requests = []
    _Browser.mode = "ok"
    server = HTTPServer(("127.0.0.1", 0), _Browser)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Browser.address = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    yield _Browser
    server.shutdown()
    thread.join(timeout=5)


def _fill(
    tmp: Path,
    browser: type[_Browser],
    *args: str,
    store_text: str | None = None,
    token: str = BROWSER_TOKEN,
) -> subprocess.CompletedProcess[str]:
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    store = _store(
        tmp / "fill-store.env",
        store_text if store_text is not None else f"MY_FORM_SECRET={FORM_VALUE}\n",
    )
    env = _base_env(
        home, store, PATH=os.environ["PATH"], BROWSER_URL=browser.address, BROWSER_TOKEN=token
    )  # type: ignore[attr-defined]
    return _run([sys.executable, str(FILL), "--origin", "https://example.com", *args], env, cwd=tmp)


class TestFillSecret:
    def test_success_reports_a_length_and_no_value(
        self, tmp_path: Path, browser: type[_Browser]
    ) -> None:
        result = _fill(tmp_path, browser, "input[name=password]", "MY_FORM_SECRET")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"result": len(FORM_VALUE)}
        assert FORM_VALUE not in result.stdout + result.stderr
        origin_request, request = browser.requests
        assert origin_request["body"] == {"expression": "location.origin", "insecure": False}
        assert request["path"] == "/evaluate"
        assert request["token"] == BROWSER_TOKEN
        assert json.dumps(FORM_VALUE) in request["body"]["expression"]
        assert "input[name=password]" in request["body"]["expression"]

    @pytest.mark.parametrize("mode", ["error", "missing", "junk"])
    def test_failures_do_not_leak_the_value(
        self, tmp_path: Path, browser: type[_Browser], mode: str
    ) -> None:
        browser.mode = mode
        result = _fill(tmp_path, browser, "#field", "MY_FORM_SECRET")
        assert result.returncode != 0
        everything = result.stdout + result.stderr
        assert FORM_VALUE not in everything
        assert json.dumps(FORM_VALUE)[1:-1] not in everything
        assert "error" in json.loads(result.stdout)

    def test_rejected_token_and_unreachable_browser(
        self, tmp_path: Path, browser: type[_Browser]
    ) -> None:
        wrong = _fill(tmp_path, browser, "#field", "MY_FORM_SECRET", token="dummy-wrong-token")
        assert wrong.returncode != 0
        assert FORM_VALUE not in wrong.stdout + wrong.stderr
        browser.address = "http://127.0.0.1:9"  # type: ignore[attr-defined]
        unreachable = _fill(tmp_path, browser, "#field", "MY_FORM_SECRET")
        assert unreachable.returncode != 0
        assert FORM_VALUE not in unreachable.stdout + unreachable.stderr

    def test_missing_secret_sends_nothing(self, tmp_path: Path, browser: type[_Browser]) -> None:
        result = _fill(tmp_path, browser, "#field", "MY_FORM_SECRET", store_text="OTHER=1\n")
        assert result.returncode != 0
        assert "MY_FORM_SECRET" in result.stdout
        assert browser.requests == []

    def test_selector_starting_with_a_dash_needs_separator(
        self, tmp_path: Path, browser: type[_Browser]
    ) -> None:
        assert _fill(tmp_path, browser, "-x", "MY_FORM_SECRET").returncode != 0
        assert browser.requests == []
        assert _fill(tmp_path, browser, "--", "-x", "MY_FORM_SECRET").returncode == 0


# -- totp.py ----------------------------------------------------------------------------------


@pytest.fixture
def totp_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_totp_under_test", TOTP)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# RFC 6238 Appendix B, SHA-1: time -> 8-digit code. The tool prints the last six digits.
RFC_SHA1 = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


class TestTotp:
    @pytest.mark.parametrize(("moment", "eight"), RFC_SHA1)
    def test_rfc_vectors_last_six_digits(
        self, totp_module: ModuleType, moment: int, eight: str
    ) -> None:
        assert totp_module.totp(SEED, moment) == eight[-6:]
        assert totp_module.totp(SEED, moment, digits=8) == eight

    def test_leading_zeros_are_kept(self, totp_module: ModuleType) -> None:
        assert totp_module.totp(SEED, 1111111109) == "081804"
        assert totp_module.totp(SEED, 1234567890) == "005924"

    def test_seed_spelling(self, totp_module: ModuleType) -> None:
        spaced = " ".join(SEED[i : i + 4] for i in range(0, len(SEED), 4)).lower()
        assert totp_module.totp(spaced, 59) == "287082"

    def _run(
        self, tmp: Path, *args: str, store_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        home = tmp / "home"
        home.mkdir(exist_ok=True)
        store = _store(
            tmp / "totp-store.env",
            store_text if store_text is not None else f"MY_TOTP_SEED={SEED}\n",
        )
        env = _base_env(home, store, PATH=os.environ["PATH"])
        return _run([sys.executable, str(TOTP), *args], env, cwd=tmp)

    def test_cli_prints_only_the_current_code(
        self, tmp_path: Path, totp_module: ModuleType
    ) -> None:
        before = totp_module.totp(SEED, time.time())
        result = self._run(tmp_path, "MY_TOTP_SEED")
        after = totp_module.totp(SEED, time.time())
        assert result.returncode == 0, result.stderr
        assert re.fullmatch(r"\d{6}\n", result.stdout)
        assert result.stdout.strip() in (before, after)
        assert SEED not in result.stdout + result.stderr

    def test_missing_and_invalid_seed(self, tmp_path: Path) -> None:
        missing = self._run(tmp_path, "MY_TOTP_SEED", store_text="OTHER=1\n")
        assert missing.returncode != 0
        invalid = self._run(tmp_path, "MY_TOTP_SEED", store_text="MY_TOTP_SEED=not*base32\n")
        assert invalid.returncode != 0
        assert "not*base32" not in invalid.stdout + invalid.stderr


# -- all four tools -----------------------------------------------------------------------------


class TestHelp:
    def test_help_does_nothing_else(self, tmp_path: Path, browser: type[_Browser]) -> None:
        work = tmp_path / "work"
        work.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        record = tmp_path / "record"
        record.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        spy = bin_dir / "git"
        spy.write_text('#!/bin/sh\necho called >> "$RECORD/git"\n', encoding="utf-8")
        spy.chmod(0o755)
        store = _store(
            tmp_path / "store.env", f"GH_TOKEN={TOKEN}\nMY_FORM_SECRET=x\nMY_TOTP_SEED={SEED}\n"
        )
        env = _base_env(
            home,
            store,
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            RECORD=str(record),
            BROWSER_URL=browser.address,  # type: ignore[attr-defined]
            BROWSER_TOKEN=BROWSER_TOKEN,
        )
        commands = [
            [sys.executable, str(GIT_PUSH), "--help"],
            [sys.executable, str(GIT_PUSH), "-h", "repo"],
            [sys.executable, str(FILL), "--help"],
            [sys.executable, str(TOTP), "-h"],
            ["sh", str(HELPER), "--help"],
        ]
        for command in commands:
            result = _run(command, env, cwd=work)
            assert result.returncode == 0, command
            assert result.stdout.strip(), command
            assert TOKEN not in result.stdout
        assert list(work.iterdir()) == []
        assert list(record.iterdir()) == []
        assert browser.requests == []


# -- how an update treats them (the real zone rules) ----------------------------------------------


def test_zone_rules_for_the_new_files(tmp_path: Path) -> None:
    from ductor_bot.workspace.init import _walk_and_copy

    home = tmp_path / "home"
    _walk_and_copy(DEFAULTS, home)
    deployed = home / "workspace/tools"
    for name in (
        "git_push.py",
        "browser_tools/fill_secret.py",
        "github_tools/totp.py",
        "git_credential_from_ductor_env.sh",
    ):
        assert (deployed / name).is_file(), name
        (deployed / name).write_text("# my edit\n", encoding="utf-8")

    _walk_and_copy(DEFAULTS, home)

    assert (deployed / "git_push.py").read_text() != "# my edit\n"  # framework-managed
    assert (deployed / "browser_tools/fill_secret.py").read_text() != "# my edit\n"
    assert (deployed / "git_push.py.bak").read_text() == "# my edit\n"  # the edit was saved
    assert (deployed / "browser_tools/fill_secret.py.bak").read_text() == "# my edit\n"
    assert (deployed / "github_tools/totp.py").read_text() == "# my edit\n"  # seeded once
    assert (deployed / "git_credential_from_ductor_env.sh").read_text() == "# my edit\n"
    assert not list(deployed.glob("github_tools/*.bak*"))


def test_helper_script_is_shipped_executable() -> None:
    assert os.access(HELPER, os.X_OK)


@pytest.fixture
def fill_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_fill_under_test", FILL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("mode", ["match", "mismatch", "insecure", "changed", "missing_origin"])
def test_fill_requires_matching_secure_origin(fill_module, monkeypatch, capsys, mode):
    origin = "https://example.com"
    requests = []
    monkeypatch.setenv("MY_FORM_SECRET", FORM_VALUE)
    monkeypatch.setattr(fill_module, "_load_secrets", lambda: lambda *_keys: {"MY_FORM_SECRET": FORM_VALUE})

    def post(expression):
        requests.append(expression)
        if expression == "location.origin":
            return json.dumps({"result": "https://other.invalid" if mode == "mismatch" else origin,
                               "insecure": mode == "insecure"})
        return json.dumps({"result": -2 if mode == "changed" else len(FORM_VALUE)})

    monkeypatch.setattr(fill_module, "_post", post)
    args = ["#field", "MY_FORM_SECRET"]
    if mode != "missing_origin":
        args = ["--origin", origin, *args]
    result = fill_module.main(args)
    output = capsys.readouterr()
    assert FORM_VALUE not in output.out + output.err
    if mode == "missing_origin":
        assert result == 1
        assert "Usage:" in output.err
        assert requests == []
    elif mode in {"mismatch", "insecure"}:
        assert result == 1
        assert requests == ["location.origin"]
        assert FORM_VALUE not in "".join(requests)
        assert json.dumps(FORM_VALUE) not in "".join(requests)
    else:
        assert requests[0] == "location.origin"
        assert len(requests) == 2
        assert f"if (!(window.isSecureContext === true) || location.origin !== {json.dumps(origin)}) return -2;" in requests[1]
        assert requests[1].index("location.origin") < requests[1].index("document.querySelector")
        assert json.dumps(FORM_VALUE) in requests[1]
        assert result == (0 if mode == "match" else 1)
        if mode == "changed":
            assert "origin" in json.loads(output.out)["error"]


@pytest.fixture
def git_push_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_git_push_under_test", GIT_PUSH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_push_does_not_execute_repository_hook(tmp_path, git_push_module, monkeypatch):
    repo = _repo(tmp_path, ALLOWED_URLS[0])
    remote = tmp_path / "remote.git"
    subprocess.run([REAL_GIT, "init", "--bare", str(remote)], check=True, capture_output=True)
    hook = repo / ".git/hooks/pre-push"
    hook.write_text("#!/bin/sh\nprintf ran > hook-ran\n")
    hook.chmod(0o755)

    def git(args, repo, env=None):
        # The verified public URL is replaced only at this fake execution boundary.
        # All push traffic stays in the temporary local bare repository.
        if "push" in args:
            args = [str(remote) if arg == ALLOWED_URLS[0] else arg for arg in args]
        return subprocess.run([REAL_GIT, "-C", repo, *args], env=env,
                              capture_output=True, text=True, check=False)

    monkeypatch.setattr(git_push_module, "_git", git)
    monkeypatch.setattr(git_push_module, "_token", lambda: TOKEN)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    assert git_push_module.main([str(repo)]) == 0
    assert not (repo / "hook-ran").exists()
    assert subprocess.run([REAL_GIT, "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
                          capture_output=True, check=False).returncode == 0
    source_sha = subprocess.check_output([REAL_GIT, "-C", str(repo), "rev-parse", "refs/heads/main"], text=True).strip()
    tracking_sha = subprocess.check_output([REAL_GIT, "-C", str(repo), "rev-parse", "refs/remotes/origin/main"], text=True).strip()
    assert tracking_sha == source_sha


def test_push_overrides_url_specific_tls_and_proxy_config(push_env):
    url = ALLOWED_URLS[0]
    repo = _repo(push_env["tmp"], url)
    for key, value in [("http.https://github.com/.sslVerify", "false"),
                       ("http.https://github.com/.proxy", "http://proxy.invalid:8888")]:
        subprocess.run([REAL_GIT, "-C", str(repo), "config", key, value],
                       env=push_env["env"], capture_output=True, check=True)
    result = _run([sys.executable, str(GIT_PUSH), str(repo)], push_env["env"], cwd=push_env["tmp"])
    assert result.returncode == 0, result.stderr
    assert json.loads((push_env["record"] / "matched").read_text()) == {"http.sslVerify": "true", "http.proxy": ""}


def test_fill_pins_both_requests_to_secure_page(fill_module, monkeypatch):
    requests = []
    browser = ModuleType("browser")

    def post(path, body):
        requests.append((path, body))
        return json.dumps({"result": "https://example.com" if len(requests) == 1
                           else len(FORM_VALUE), "insecure": False})

    browser._post = post
    monkeypatch.setitem(sys.modules, "browser", browser)
    monkeypatch.setenv("MY_FORM_SECRET", FORM_VALUE)
    monkeypatch.setattr(fill_module, "_load_secrets", lambda: lambda *_keys: {"MY_FORM_SECRET": FORM_VALUE})
    assert fill_module.main(["--origin", "https://example.com", "#field", "MY_FORM_SECRET"]) == 0
    assert len(requests) == 2
    for path, body in requests:
        assert path == "/evaluate"
        assert body["insecure"] is False


@pytest.mark.parametrize("valid_second", [True, False])
def test_push_checks_and_protects_all_push_urls(tmp_path, git_push_module, monkeypatch, valid_second):
    urls = [ALLOWED_URLS[0], "https://github.com/owner/second.git" if valid_second
            else "https://other.invalid/owner/second.git"]
    repo = _repo(tmp_path, urls[0])
    env = _base_env(tmp_path)
    remotes = [tmp_path / "first.git", tmp_path / "second.git"]
    for url, remote in zip(urls, remotes, strict=True):
        subprocess.run([REAL_GIT, "init", "--bare", str(remote)], env=env,
                       check=True, capture_output=True)
        for key, value in [("remote.origin.pushurl", url),
                           (f"http.{url}.proxy", "http://proxy.invalid:8888")]:
            subprocess.run([REAL_GIT, "-C", str(repo), "config", "--add", key, value],
                           env=env, check=True, capture_output=True)
    subprocess.run([REAL_GIT, "-C", str(repo), "config", "http.https://github.com/.sslVerify", "false"],
                   env=env, check=True, capture_output=True)
    pushes = []

    def git(args, repo, env=None):
        if "push" in args:
            pushes.append(args)
            index = urls.index(args[-2])
            local_args = [str(remotes[index]) if arg == urls[index] else arg for arg in args]
            options = args[:args.index("push")]
            for key, expected in [("http.sslVerify", "true"), ("http.proxy", "")]:
                matched = subprocess.run([REAL_GIT, "-C", repo, *options, "config", "--get-urlmatch", key, urls[index]],
                                         env=env, capture_output=True, text=True, check=False)
                assert matched.returncode == 0
                assert matched.stdout.strip() == expected
            return subprocess.run([REAL_GIT, "-C", repo, *local_args], env=env,
                                  capture_output=True, text=True, check=False)
        return subprocess.run([REAL_GIT, "-C", repo, *args], env=env,
                              capture_output=True, text=True, check=False)

    monkeypatch.setattr(git_push_module, "_git", git)
    monkeypatch.setattr(git_push_module, "_token", lambda: TOKEN)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    result = git_push_module.main([str(repo)])
    if not valid_second:
        assert result == 1
        assert pushes == []
        return
    assert result == 0
    assert len(pushes) == len(urls)
    assert [args[-2] for args in pushes] == urls
    for args in pushes:
        assert "--no-verify" in args
        assert "--no-recurse-submodules" in args
    for remote in remotes:
        assert subprocess.run([REAL_GIT, "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
                              env=env, capture_output=True, check=False).returncode == 0



@pytest.mark.parametrize("origin", ["http://example.com", "null", "https://a.com/path"])
def test_fill_rejects_non_https_origins_before_browser(fill_module, monkeypatch, origin):
    calls = []
    monkeypatch.setenv("MY_FORM_SECRET", FORM_VALUE)
    monkeypatch.setattr(
        fill_module,
        "_post",
        lambda expression: (
            calls.append(expression)
            or json.dumps({"result": "https://example.com", "insecure": False})
        ),
    )
    assert fill_module.main(["--origin", origin, "#field", "MY_FORM_SECRET"]) == 1
    assert calls == []


def test_fill_ignores_environment_only_values(fill_module, monkeypatch):
    calls = []
    monkeypatch.setenv("MY_FORM_SECRET", FORM_VALUE)
    monkeypatch.setattr(fill_module, "_load_secrets", lambda: lambda *_keys: {})
    monkeypatch.setattr(
        fill_module,
        "_post",
        lambda expression: (
            calls.append(expression)
            or json.dumps({"result": "https://example.com", "insecure": False})
        ),
    )
    assert fill_module.main(["--origin", "https://example.com", "#field", "MY_FORM_SECRET"]) == 1
    assert calls == []


@pytest.mark.parametrize("secure", [False, True])
def test_fill_prefers_store_and_checks_secure_context_atomically(
    fill_module, monkeypatch, tmp_path, capsys, *, secure
):
    requests = []
    store = tmp_path / "store.env"
    store.write_text("MY_FORM_SECRET=stored-only-value\n")
    monkeypatch.setenv("DUCTOR_SECRETS_FILE", str(store))
    monkeypatch.setenv("MY_FORM_SECRET", "environment-only-value")

    def post(expression):
        requests.append(expression)
        if expression == "location.origin":
            return json.dumps({"result": "https://example.com", "insecure": False})
        assert "window.isSecureContext === true" in expression
        assert expression.index("isSecureContext") < expression.index("document.querySelector")
        assert json.dumps("stored-only-value") in expression
        assert "environment-only-value" not in expression
        return json.dumps({"result": len("stored-only-value") if secure else -2})

    monkeypatch.setattr(fill_module, "_post", post)
    assert fill_module.main(["--origin", "https://example.com", "#field", "MY_FORM_SECRET"]) == (
        0 if secure else 1
    )
    assert len(requests) == 2
    output = capsys.readouterr()
    assert "stored-only-value" not in output.out + output.err

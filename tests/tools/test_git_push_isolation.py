"""Push isolation against disposable loopback proxy and TLS listeners."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

TOOL = Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults/workspace/tools/git_push.py"
TOKEN = "test-only-push-token"
REAL_GIT = shutil.which("git") or "git"


def git(repo, *args):
    return subprocess.run(
        [REAL_GIT, "-C", str(repo), *args], capture_output=True, text=True, check=True, timeout=20
    )


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    """Generate test-only TLS credentials outside the checkout."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required for loopback TLS certificates")
    directory = tmp_path_factory.mktemp("git-push-certificates")
    extensions = directory / "server.ext"
    extensions.write_text(
        "subjectAltName=IP:127.0.0.1\n"
        "basicConstraints=CA:FALSE\n"
        "keyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
    )
    commands = [
        [
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "7300",
            "-subj",
            "/CN=Test-only Git Push CA",
            "-keyout",
            "test-ca-key.pem",
            "-out",
            "ca.pem",
        ],
        [
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=Test-only Loopback Server",
            "-keyout",
            "test-server-key.pem",
            "-out",
            "server.csr",
        ],
        [
            "x509",
            "-req",
            "-in",
            "server.csr",
            "-CA",
            "ca.pem",
            "-CAkey",
            "test-ca-key.pem",
            "-set_serial",
            "2",
            "-days",
            "7300",
            "-extfile",
            "server.ext",
            "-out",
            "server.pem",
        ],
    ]
    for command in commands:
        subprocess.run(
            [openssl, *command],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
    return directory


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(
            ("GIT_", "GH_", "GITHUB_", "SSL_CERT_", "CURL_", "XDG_")
        ) or name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    spec = importlib.util.spec_from_file_location("isolated_push_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Only URL validation is replaced; the tool still runs real Git and askpass.
    monkeypatch.setattr(
        module,
        "is_github_https_url",
        lambda url: (
            urlsplit(url).scheme == "https"
            and urlsplit(url).hostname == "127.0.0.1"
            and urlsplit(url).username is None
            and urlsplit(url).password is None
        ),
    )
    record = tmp_path / "pushes.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "git"
    wrapper.write_text(
        f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
        "if 'push' in sys.argv[1:]:\n"
        "    bare = pathlib.Path(os.environ.get('GIT_DIR', '/missing'))\n"
        "    alternate = bare / 'objects/info/alternates'\n"
        "    shallow = bare / 'shallow'\n"
        f"    with open({str(record)!r}, 'a') as log:\n"
        "        log.write(json.dumps({'args': sys.argv[1:], 'keys': sorted(os.environ), "
        "'git_dir': str(bare), 'home': os.environ.get('HOME'), "
        "'alternates': alternate.read_text() if alternate.exists() else '', "
        "'shallow': shallow.read_text() if shallow.exists() else ''}) + '\\n')\n"
        f"os.execv({REAL_GIT!r}, [{REAL_GIT!r}, *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
    )
    return module, repo, record


@pytest.fixture
def listeners(certificates):
    class TLSHandler(BaseHTTPRequestHandler):
        requests = []
        authorization = []

        def do_GET(self):
            self.requests.append(self.path)
            header = self.headers.get("Authorization")
            if header:
                self.authorization.append(header)
            self.send_response(403 if header else 401)
            self.send_header("WWW-Authenticate", 'Basic realm="test-only"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    class ProxyHandler(BaseHTTPRequestHandler):
        connections = []

        def do_CONNECT(self):
            self.connections.append(self.path)
            self.send_error(502)

        def do_GET(self):
            self.connections.append(self.path)
            self.send_error(502)

        def log_message(self, *args):
            pass

    tls = ThreadingHTTPServer(("127.0.0.1", 0), TLSHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificates / "server.pem", certificates / "test-server-key.pem")
    tls.socket = context.wrap_socket(tls.socket, server_side=True)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    threads = [
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        for s in (tls, proxy)
    ]
    for thread in threads:
        thread.start()
    try:
        yield (
            f"https://127.0.0.1:{tls.server_port}/repo.git",
            f"http://127.0.0.1:{proxy.server_port}",
            TLSHandler,
            ProxyHandler,
        )
    finally:
        for server in (tls, proxy):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


CASES = [
    "remote.origin.proxy",
    "http.proxy",
    "url.proxy",
    "http.sslCAInfo",
    "http.sslVerify",
    "push.recurseSubmodules",
    "GIT_SSL_NO_VERIFY",
    "GIT_SSL_CAINFO",
    "SSL_CERT_FILE",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "combined",
]


@pytest.mark.parametrize("setting", CASES)
def test_untrusted_settings_cannot_reach_proxy_or_tls(
    sandbox, listeners, monkeypatch, tmp_path, setting, *, certificates
):
    module, repo, record = sandbox
    url, proxy, tls_handler, proxy_handler = listeners
    git(repo, "remote", "add", "origin", url)
    ca = str(certificates / "ca.pem")
    config = {
        "remote.origin.proxy": proxy,
        "http.proxy": proxy,
        f"http.{url}.proxy": proxy,
        "http.sslCAInfo": ca,
        "http.sslVerify": "false",
        "push.recurseSubmodules": "on-demand",
    }
    global_config = tmp_path / "global.config"
    global_config.write_text(f"[http]\nsslCAInfo = {ca}\n")
    environment = {
        "GIT_SSL_NO_VERIFY": "1",
        "GIT_SSL_CAINFO": ca,
        "SSL_CERT_FILE": ca,
        "HTTPS_PROXY": proxy,
        "ALL_PROXY": proxy,
        "GIT_CONFIG_GLOBAL": str(global_config),
    }
    for key, value in config.items():
        if setting in {key, "combined"} or (setting == "url.proxy" and key == f"http.{url}.proxy"):
            git(repo, "config", key, value)
    for key, value in environment.items():
        if setting in {key, "combined"}:
            monkeypatch.setenv(key, value)
    if setting in {"GIT_CONFIG_COUNT", "combined"}:
        for key, value in {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.sslVerify",
            "GIT_CONFIG_VALUE_0": "false",
        }.items():
            monkeypatch.setenv(key, value)
    assert module.main([str(repo), "main"]) != 0
    assert proxy_handler.connections == []
    assert tls_handler.authorization == []
    # TLS must fail before even the unauthenticated HTTP request, independently of askpass.
    assert tls_handler.requests == []
    (attempt,) = records(record)
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "HOME",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_TERMINAL_PROMPT",
        "GIT_ASKPASS",
        "DUCTOR_PUSH_TOKEN",
        "GIT_DIR",
    }
    assert set(attempt["keys"]) <= allowed
    assert attempt["alternates"] == str(repo / ".git/objects") + "\n"
    assert not Path(attempt["git_dir"]).exists()
    assert not Path(attempt["home"]).exists()


def test_tls_listener_detects_authorization_when_ca_is_trusted(sandbox, listeners, certificates):
    _, repo, _ = sandbox
    url, _, tls_handler, _ = listeners
    # Fake credentials are supplied directly only in this positive control.
    credential_url = url.replace("https://", "https://x-access-token:test-only-push-token@")
    result = subprocess.run(
        [
            REAL_GIT,
            "-C",
            str(repo),
            "-c",
            f"http.sslCAInfo={certificates / 'ca.pem'}",
            "push",
            credential_url,
            "main",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode != 0  # The fixture records credentials, then refuses the push.
    assert tls_handler.authorization


@pytest.mark.parametrize("branch", ["+main", ":main", "HEAD:main", "a:b", "missing", "-main"])
def test_only_existing_local_branch_names_can_push(sandbox, listeners, branch):
    module, repo, record = sandbox
    url, _, tls_handler, proxy_handler = listeners
    git(repo, "remote", "add", "origin", url)
    assert module.main([str(repo), branch]) != 0
    assert records(record) == []
    assert tls_handler.requests == []
    assert proxy_handler.connections == []


def test_linked_shallow_worktree_uses_common_objects(sandbox, listeners, tmp_path):
    module, repo, record = sandbox
    url, _, _, _ = listeners
    git(repo, "remote", "add", "origin", url)
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / ".git/shallow").write_text(sha + "\n")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "topic", str(linked))
    assert module.main([str(linked), "topic"]) != 0
    (attempt,) = records(record)
    assert attempt["alternates"] == str(repo / ".git/objects") + "\n"
    assert attempt["shallow"] == sha + "\n"


@pytest.fixture
def local_push(sandbox, monkeypatch, tmp_path):
    module, repo, record = sandbox
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(repo, "remote", "add", "origin", remote.as_uri())
    monkeypatch.setattr(module, "is_github_https_url", lambda url: url == remote.as_uri())
    return module, repo, record, remote


def test_source_git_uses_one_credential_free_environment(local_push, monkeypatch):
    module, repo, _record, _remote = local_push
    for name in ("GITHUB_TOKEN", "DUCTOR_PUSH_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS"):
        monkeypatch.setenv(name, "test-only-inherited-value")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(repo))
    observed = []
    original = module._git

    def run(args, directory, env=None):
        if directory == str(repo):
            child = os.environ if env is None else env
            observed.append(
                (
                    args,
                    set(child),
                    child.get("GIT_NO_LAZY_FETCH"),
                    child.get("GIT_TERMINAL_PROMPT"),
                    child.get("GIT_CONFIG_VALUE_0"),
                    id(env),
                )
            )
        return original(args, directory, env)

    monkeypatch.setattr(module, "_git", run)
    assert module.main([str(repo)]) == 0
    assert observed
    assert all(
        not (keys & {"GH_TOKEN", "GITHUB_TOKEN", "DUCTOR_PUSH_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS"})
        for _, keys, *_rest in observed
    )
    assert all(
        lazy == "1" and prompt == "0" and allowed == str(repo)
        for _args, _keys, lazy, prompt, allowed, _identity in observed
    )
    assert len({row[-1] for row in observed}) == 1
    assert all(
        args[:4] == ["-c", "credential.helper=", "-c", "core.hooksPath=/dev/null"]
        for args, *_rest in observed
    )
    assert any("update-ref" in args for args, *_rest in observed)
    assert any("remote.origin.fetch" in args for args, *_rest in observed)


@pytest.mark.parametrize(
    ("specs", "expected"),
    [
        (["+refs/heads/*:refs/remotes/origin/*"], "refs/remotes/origin/main"),
        (["+refs/heads/*:refs/remotes/custom/*"], "refs/remotes/custom/main"),
        (["+refs/heads/main:refs/remotes/exact/main"], "refs/remotes/exact/main"),
        (["refs/heads/main:refs/remotes/plain/main"], "refs/remotes/plain/main"),
        (["+refs/heads/other:refs/remotes/origin/other"], None),
        ([], None),
        (["+refs/heads/*:refs/remotes/origin/*", "^refs/heads/main"], "refs/remotes/origin/main"),
        (["^refs/heads/ma*", "+refs/heads/*:refs/remotes/origin/*"], "refs/remotes/origin/main"),
        (["^refs/heads/other", "+refs/heads/*:refs/remotes/origin/*"], "refs/remotes/origin/main"),
        (
            ["+refs/heads/*:refs/remotes/first/*", "+refs/heads/main:refs/remotes/second/main"],
            "refs/remotes/first/main",
        ),
        (["+refs/heads/ma*:refs/remotes/custom/de*"], "refs/remotes/custom/dein"),
    ],
)
def test_tracking_ref_follows_fetch_refspecs(local_push, specs, expected):
    module, repo, record, remote = local_push
    git(repo, "config", "--unset-all", "remote.origin.fetch")
    for spec in specs:
        git(repo, "config", "--add", "remote.origin.fetch", spec)
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert module.main([str(repo), "main"]) == 0
    assert git(remote, "rev-parse", "refs/heads/main").stdout.strip() == sha
    tracked = git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes/").stdout.splitlines()
    assert tracked == ([] if expected is None else [expected])
    if expected is not None:
        assert git(repo, "rev-parse", expected).stdout.strip() == sha
    assert len(records(record)) == 1


def test_tracking_failure_warns_after_successful_push(local_push, capsys):
    module, repo, record, remote = local_push
    ref = "refs/remotes/origin/main"
    lock = repo / ".git" / (ref + ".lock")
    lock.parent.mkdir(parents=True)
    lock.touch()
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert module.main([str(repo), "main"]) == 0
    output = capsys.readouterr()
    assert "Warning:" in output.err
    assert ref in output.err
    assert TOKEN not in output.out + output.err
    assert git(remote, "rev-parse", "refs/heads/main").stdout.strip() == sha
    assert len(records(record)) == 1


@pytest.mark.parametrize("lazy_guard", [True, False])
def test_missing_promisor_commit_never_sends_credentials(
    sandbox, listeners, certificates, monkeypatch, tmp_path, *, lazy_guard
):
    module, repo, record = sandbox
    url, proxy, tls_handler, proxy_handler = listeners
    git(repo, "remote", "add", "origin", url)
    git(repo, "config", "http.sslCAInfo", str(certificates / "ca.pem"))
    if lazy_guard:
        git(repo, "config", "remote.origin.proxy", proxy)
    marker = tmp_path / "helper-called"
    git(
        repo,
        "config",
        "credential.helper",
        f'!f() {{ touch "{marker}"; printf "username=x-access-token\\npassword=%s\\n" "$GH_TOKEN"; }}; f',
    )
    git(repo, "config", "extensions.partialClone", "origin")
    git(repo, "config", "remote.origin.promisor", "true")
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / ".git/objects" / sha[:2] / sha[2:]).unlink()
    original = module._git

    def run(args, directory, env=None):
        if not lazy_guard and env is not None:
            env = {key: value for key, value in env.items() if key != "GIT_NO_LAZY_FETCH"}
        return original(args, directory, env)

    monkeypatch.setattr(module, "_git", run)
    assert module.main([str(repo), "main"]) != 0
    assert records(record) == []
    assert tls_handler.authorization == []
    assert not marker.exists()
    if lazy_guard:
        assert proxy_handler.connections == []
        assert tls_handler.requests == []
    else:
        assert tls_handler.requests


def test_tracking_failure_masks_token_in_warning(local_push, capsys):
    module, repo, record, remote = local_push
    ref = f"refs/remotes/{TOKEN}/main"
    git(repo, "config", "remote.origin.fetch", f"+refs/heads/main:{ref}")
    lock = repo / ".git" / (ref + ".lock")
    lock.parent.mkdir(parents=True)
    lock.touch()
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert module.main([str(repo), "main"]) == 0
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert "<token>" in output.err
    assert "Warning:" in output.err
    assert git(remote, "rev-parse", "refs/heads/main").stdout.strip() == sha
    assert len(records(record)) == 1


@pytest.mark.parametrize(
    "specs",
    [
        ["+refs/heads/*:refs/remotes/origin/*", "^refs/heads/main"],
        ["+refs/heads/*:refs/remotes/origin/*", "^refs/heads/ma*"],
        ["^refs/heads/main", "+refs/heads/*:refs/remotes/origin/*"],
        ["+refs/heads/*:refs/remotes/origin/*", "^refs/heads/topic"],
    ],
)
def test_negative_fetch_refspec_tracking_matches_native_push(local_push, specs):
    module, repo, record, remote = local_push
    git(repo, "config", "--unset-all", "remote.origin.fetch")
    for spec in specs:
        git(repo, "config", "--add", "remote.origin.fetch", spec)
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "push", "origin", "main")
    native = git(repo, "for-each-ref", "--format=%(refname):%(objectname)", "refs/remotes/")
    assert native.stdout.splitlines() == [f"refs/remotes/origin/main:{sha}"]
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")

    assert module.main([str(repo), "main"]) == 0
    tracked = git(repo, "for-each-ref", "--format=%(refname):%(objectname)", "refs/remotes/")
    assert tracked.stdout == native.stdout
    assert git(remote, "rev-parse", "refs/heads/main").stdout.strip() == sha
    assert len(records(record)) == 1

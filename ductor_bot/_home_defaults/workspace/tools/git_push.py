#!/usr/bin/env python3
"""Push a branch to GitHub over HTTPS without putting the token on a command line.

Usage:
    python3 git_push.py REPO [BRANCH]

REPO is a local git checkout. BRANCH must be a local branch name and defaults to the checked-out
branch; refspecs are not accepted. The token is the first
non-empty value of GH_TOKEN in the environment, GITHUB_TOKEN in the environment, GH_TOKEN in the
secrets store, GITHUB_TOKEN in the secrets store. It is handed to git through a one-shot askpass
helper and the environment, never through arguments.

The token is only used when the push URL of "origin" is https://github.com/... without
credentials in it. For any other remote the tool stops before git is asked to push.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from urllib.parse import urlsplit

USAGE = "Usage: python3 git_push.py REPO [BRANCH]"
TOKEN_NAMES = ("GH_TOKEN", "GITHUB_TOKEN")
GITHUB_HOST = "github.com"

# git asks for the user name and the password in two calls and names the URL in the prompt. Only
# the prompts for https://github.com are answered; any other prompt gets no answer.
ASKPASS_BODY = """#!/bin/sh
case "$1" in
  "Username for 'https://github.com'"*) printf '%s\\n' "x-access-token" ;;
  "Password for 'https://x-access-token@github.com'"*) printf '%s\\n' "$DUCTOR_PUSH_TOKEN" ;;
  "Password for 'https://github.com'"*) printf '%s\\n' "$DUCTOR_PUSH_TOKEN" ;;
  *) exit 1 ;;
esac
"""


def is_github_https_url(url: str) -> bool:
    """True only for ``https://github.com/...``: no user information, no port, no other spelling."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme == "https" and parts.netloc == GITHUB_HOST


def _load_secrets() -> Callable[..., dict[str, str]]:
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from _secrets import load_secrets

    return load_secrets


def _load_parser() -> Callable[..., tuple[dict[str, str | bool], list[str]]]:
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from _tool_shared import parse_tool_args

    return parse_tool_args


def _git(
    args: list[str], repo: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", "-C", repo, *args],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _fail(message: str) -> int:
    print(f"Error: {message}", file=sys.stderr)
    return 1


def _environment_token() -> str:
    for name in TOKEN_NAMES:
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def _token() -> str:
    token = _environment_token()
    if not token:
        _load_secrets()(*TOKEN_NAMES)
        token = _environment_token()
    return token


def _report_result(result: subprocess.CompletedProcess[str], token: str) -> int:
    output = (result.stdout + result.stderr).replace(token, "<token>").strip()
    if output:
        print(output)
    return result.returncode


def _isolated_push(
    repo: str, urls: list[str], branch: str, sha: str, common: str, token: str
) -> int:
    workdir = tempfile.mkdtemp(prefix="gitpush-")
    try:
        askpass = os.path.join(workdir, "askpass.sh")
        with open(askpass, "w", encoding="utf-8") as handle:
            handle.write(ASKPASS_BODY)
        os.chmod(askpass, stat.S_IRWXU)
        env = {
            name: os.environ[name]
            for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES")
            if name in os.environ
        }
        env.update(
            HOME=workdir,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_TERMINAL_PROMPT="0",
            GIT_ASKPASS=askpass,
            DUCTOR_PUSH_TOKEN=token,
        )
        bare = os.path.join(workdir, "repo.git")
        initialized = _git(["init", "--bare", bare], workdir, env)
        if initialized.returncode != 0:
            return _report_result(initialized, token)
        with open(
            os.path.join(bare, "objects", "info", "alternates"), "w", encoding="utf-8"
        ) as handle:
            handle.write(os.path.join(common, "objects") + "\n")
        shallow = os.path.join(common, "shallow")
        if os.path.isfile(shallow):
            shutil.copyfile(shallow, os.path.join(bare, "shallow"))
        push_env = {**env, "GIT_DIR": bare}
        for url in urls:
            result = _git(
                [
                    "-c",
                    "credential.helper=",
                    "-c",
                    "http.followRedirects=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "http.sslVerify=true",
                    "-c",
                    "http.proxy=",
                    "-c",
                    "push.recurseSubmodules=no",
                    "push",
                    "--no-verify",
                    "--no-recurse-submodules",
                    url,
                    f"{sha}:refs/heads/{branch}",
                ],
                workdir,
                push_env,
            )
            if _report_result(result, token) != 0:
                return result.returncode
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _source_git(
    args: list[str], repo: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return _git(["-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", *args], repo, env)


def _tracking_ref(ref: str, refspecs: list[str]) -> str | None:
    tracked = None
    for refspec in refspecs:
        if refspec.startswith("^"):
            continue
        body = refspec.removeprefix("+")
        source, separator, target = body.partition(":")
        if source.count("*") > 1:
            continue
        matched = re.fullmatch(re.escape(source).replace(r"\*", "(.*)"), ref)
        if matched is None:
            continue
        if tracked is None and separator and target and source.count("*") == target.count("*"):
            tracked = target.replace("*", matched[1]) if "*" in source else target
    return tracked


def main(argv: list[str]) -> int:
    _, args = _load_parser()(
        argv,
        usage=USAGE,
        help_text=(__doc__ or "").strip(),
        min_positional=1,
        max_positional=2,
    )
    repo = os.path.abspath(args[0])
    branch = args[1] if len(args) > 1 else None

    source_env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {"GH_TOKEN", "GITHUB_TOKEN", "DUCTOR_PUSH_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS"}
    }
    source_env.update(GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")

    if _source_git(["rev-parse", "--git-dir"], repo, source_env).returncode != 0:
        return _fail(f"not a git repository: {repo}")

    push = _source_git(["remote", "get-url", "--push", "--all", "origin"], repo, source_env)
    urls = push.stdout.splitlines()
    if push.returncode != 0 or not urls:
        return _fail("the repository has no 'origin' remote")
    if not all(is_github_https_url(url) for url in urls):
        return _fail(
            "origin is not an https://github.com/... URL without credentials; "
            "the token is only used for GitHub over HTTPS and nothing was pushed"
        )

    if branch is None:
        branch = _source_git(["rev-parse", "--abbrev-ref", "HEAD"], repo, source_env).stdout.strip()
        if not branch or branch == "HEAD":
            return _fail("the current branch is unknown; pass the branch name")
    if branch.startswith(("-", "+")) or ":" in branch:
        return _fail("pass a local branch name, not an option or refspec")
    ref = f"refs/heads/{branch}"
    if _source_git(["check-ref-format", ref], repo, source_env).returncode != 0:
        return _fail("pass a valid local branch name")
    resolved = _source_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], repo, source_env)
    if resolved.returncode != 0:
        return _fail("the local branch does not exist or does not point to a commit")
    common = _source_git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], repo, source_env
    )
    if common.returncode != 0:
        return _fail("could not resolve the repository object directory")

    token = _token()
    if not token:
        return _fail("no GH_TOKEN or GITHUB_TOKEN is set in the environment or the secrets store")

    sha = resolved.stdout.strip()
    result = _isolated_push(repo, urls, branch, sha, common.stdout.strip(), token)
    if result != 0:
        return result
    mappings = _source_git(["config", "--get-all", "remote.origin.fetch"], repo, source_env)
    tracked = _tracking_ref(ref, mappings.stdout.splitlines())
    if tracked is not None:
        updated = _source_git(["update-ref", tracked, sha], repo, source_env)
        if updated.returncode != 0:
            print(
                f"Warning: push succeeded but could not update tracking ref {tracked}".replace(
                    token, "<token>"
                ),
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

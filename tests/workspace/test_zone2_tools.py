"""Test that framework tools in cron_tools/ and webhook_tools/ are Zone 2."""

from pathlib import Path

import pytest

from ductor_bot.workspace.init import _walk_and_copy


@pytest.fixture
def temp_workspace(tmp_path: Path):
    """Create a temporary workspace structure."""
    home_defaults = tmp_path / "home_defaults"
    ductor_home = tmp_path / "ductor_home"

    # Create framework tool structure
    cron_tools = home_defaults / "workspace" / "tools" / "cron_tools"
    webhook_tools = home_defaults / "workspace" / "tools" / "webhook_tools"
    user_tools = home_defaults / "workspace" / "tools" / "user_tools"

    for d in [cron_tools, webhook_tools, user_tools]:
        d.mkdir(parents=True)

    return home_defaults, ductor_home


def test_cron_tools_py_files_are_zone2(temp_workspace):
    """Test that .py files in tools/cron_tools/ are always overwritten (Zone 2)."""
    home_defaults, ductor_home = temp_workspace

    # Create initial tool file
    cron_tools = home_defaults / "workspace" / "tools" / "cron_tools"
    tool_file = cron_tools / "cron_add.py"
    tool_file.write_text("# version 1")

    # First sync - should seed the file
    _walk_and_copy(home_defaults, ductor_home)

    deployed_tool = ductor_home / "workspace" / "tools" / "cron_tools" / "cron_add.py"
    assert deployed_tool.exists()
    assert deployed_tool.read_text() == "# version 1"

    # Update source file
    tool_file.write_text("# version 2 - UPDATED")

    # Second sync - should overwrite (Zone 2 behavior)
    _walk_and_copy(home_defaults, ductor_home)

    # File should be updated
    assert deployed_tool.read_text() == "# version 2 - UPDATED"


def test_webhook_tools_py_files_are_zone2(temp_workspace):
    """Test that .py files in tools/webhook_tools/ are always overwritten (Zone 2)."""
    home_defaults, ductor_home = temp_workspace

    # Create initial tool file
    webhook_tools = home_defaults / "workspace" / "tools" / "webhook_tools"
    tool_file = webhook_tools / "webhook_add.py"
    tool_file.write_text("# version 1")

    # First sync
    _walk_and_copy(home_defaults, ductor_home)

    deployed_tool = ductor_home / "workspace" / "tools" / "webhook_tools" / "webhook_add.py"
    assert deployed_tool.exists()
    assert deployed_tool.read_text() == "# version 1"

    # Update source
    tool_file.write_text("# version 2 - UPDATED")

    # Second sync - should overwrite
    _walk_and_copy(home_defaults, ductor_home)

    # File should be updated
    assert deployed_tool.read_text() == "# version 2 - UPDATED"


def test_user_tools_py_files_are_zone3(temp_workspace):
    """Test that .py files in tools/user_tools/ are NOT overwritten (Zone 3)."""
    home_defaults, ductor_home = temp_workspace

    # Create initial user tool
    user_tools = home_defaults / "workspace" / "tools" / "user_tools"
    tool_file = user_tools / "custom_tool.py"
    tool_file.write_text("# version 1")

    # First sync - should seed
    _walk_and_copy(home_defaults, ductor_home)

    deployed_tool = ductor_home / "workspace" / "tools" / "user_tools" / "custom_tool.py"
    assert deployed_tool.exists()
    assert deployed_tool.read_text() == "# version 1"

    # User modifies the file
    deployed_tool.write_text("# user's custom version")

    # Update source (framework tries to update)
    tool_file.write_text("# version 2 - FRAMEWORK UPDATE")

    # Second sync - should NOT overwrite (Zone 3 behavior)
    _walk_and_copy(home_defaults, ductor_home)

    # File should still have user's version
    assert deployed_tool.read_text() == "# user's custom version"


def test_non_py_files_in_tool_dirs_are_zone3(temp_workspace):
    """Test that non-.py files in framework tool dirs are still Zone 3."""
    home_defaults, ductor_home = temp_workspace

    # Create a non-.py file in cron_tools
    cron_tools = home_defaults / "workspace" / "tools" / "cron_tools"
    config_file = cron_tools / "config.json"
    config_file.write_text('{"version": 1}')

    # First sync
    _walk_and_copy(home_defaults, ductor_home)

    deployed_config = ductor_home / "workspace" / "tools" / "cron_tools" / "config.json"
    assert deployed_config.exists()
    assert deployed_config.read_text() == '{"version": 1}'

    # User modifies the config
    deployed_config.write_text('{"version": 1, "user_setting": true}')

    # Update source
    config_file.write_text('{"version": 2}')

    # Second sync - should NOT overwrite non-.py files (Zone 3)
    _walk_and_copy(home_defaults, ductor_home)

    # Should keep user's version
    assert deployed_config.read_text() == '{"version": 1, "user_setting": true}'


def test_shared_py_is_also_zone2(temp_workspace):
    """Test that _shared.py in tool dirs is also Zone 2 (framework file)."""
    home_defaults, ductor_home = temp_workspace

    # Create _shared.py
    cron_tools = home_defaults / "workspace" / "tools" / "cron_tools"
    shared_file = cron_tools / "_shared.py"
    shared_file.write_text("# shared utils v1")

    # First sync
    _walk_and_copy(home_defaults, ductor_home)

    deployed_shared = ductor_home / "workspace" / "tools" / "cron_tools" / "_shared.py"
    assert deployed_shared.read_text() == "# shared utils v1"

    # Update source
    shared_file.write_text("# shared utils v2 - UPDATED")

    # Second sync - should overwrite
    _walk_and_copy(home_defaults, ductor_home)

    # Should be updated
    assert deployed_shared.read_text() == "# shared utils v2 - UPDATED"


def test_root_tool_shared_is_zone2_and_user_tools_stay_zone3(temp_workspace):
    """The argument parser module in tools/ follows framework updates; user scripts do not."""
    home_defaults, ductor_home = temp_workspace
    tools = home_defaults / "workspace" / "tools"
    (tools / "_tool_shared.py").write_text("# parser v1")
    (tools / "user_tools" / "mine.py").write_text("# seed")

    _walk_and_copy(home_defaults, ductor_home)
    deployed = ductor_home / "workspace" / "tools"
    (deployed / "user_tools" / "mine.py").write_text("# edited by the user")
    (tools / "_tool_shared.py").write_text("# parser v2")
    (tools / "user_tools" / "mine.py").write_text("# seed v2")

    _walk_and_copy(home_defaults, ductor_home)

    assert (deployed / "_tool_shared.py").read_text() == "# parser v2"
    assert (deployed / "user_tools" / "mine.py").read_text() == "# edited by the user"


def test_browser_tools_py_is_zone2_and_a_user_edit_is_kept_as_backup(temp_workspace):
    """browser.py follows framework updates; an edited copy is saved next to the new one."""
    home_defaults, ductor_home = temp_workspace
    browser_tools = home_defaults / "workspace" / "tools" / "browser_tools"
    browser_tools.mkdir()
    source = browser_tools / "browser.py"
    source.write_text("# browser v1")

    _walk_and_copy(home_defaults, ductor_home)
    deployed = ductor_home / "workspace" / "tools" / "browser_tools" / "browser.py"
    assert deployed.read_text() == "# browser v1"

    source.write_text("# browser v2")
    _walk_and_copy(home_defaults, ductor_home)
    assert deployed.read_text() == "# browser v2"  # replaced on every start

    deployed.with_suffix(".py.bak").unlink(missing_ok=True)
    deployed.write_text("# edited by the user")
    source.write_text("# browser v3")
    _walk_and_copy(home_defaults, ductor_home)
    assert deployed.read_text() == "# browser v3"
    assert deployed.with_suffix(".py.bak").read_text() == "# edited by the user"


def test_browser_tools_other_files_and_user_tools_keep_their_rules(temp_workspace):
    """Only ``.py`` files of browser_tools are Zone 2; user_tools stays Zone 3."""
    home_defaults, ductor_home = temp_workspace
    tools = home_defaults / "workspace" / "tools"
    (tools / "browser_tools").mkdir()
    (tools / "browser_tools" / "notes.txt").write_text("seed")
    (tools / "user_tools" / "mine.py").write_text("# seed")

    _walk_and_copy(home_defaults, ductor_home)
    deployed = ductor_home / "workspace" / "tools"
    (deployed / "browser_tools" / "notes.txt").write_text("edited")
    (deployed / "user_tools" / "mine.py").write_text("# edited")
    (tools / "browser_tools" / "notes.txt").write_text("seed v2")
    (tools / "user_tools" / "mine.py").write_text("# seed v2")

    _walk_and_copy(home_defaults, ductor_home)

    assert (deployed / "browser_tools" / "notes.txt").read_text() == "edited"
    assert (deployed / "user_tools" / "mine.py").read_text() == "# edited"


# -- a saved user edit survives any number of later framework updates ---------------------------

ZONE2_FILES = [
    "_tool_shared.py",
    "browser_tools/browser.py",
    "cron_tools/cron_add.py",
    "agent_tools/ask_agent.py",
]


@pytest.fixture
def installed(tmp_path: Path):
    """A home that has the framework v1 of every file in ZONE2_FILES installed."""
    home_defaults = tmp_path / "home_defaults"
    ductor_home = tmp_path / "ductor_home"
    tools = home_defaults / "workspace" / "tools"
    for rel in ZONE2_FILES:
        (tools / rel).parent.mkdir(parents=True, exist_ok=True)
        (tools / rel).write_text("# framework v1")
    _walk_and_copy(home_defaults, ductor_home)
    return tools, ductor_home / "workspace" / "tools", home_defaults, ductor_home


def _release(tools: Path, version: str) -> None:
    for rel in ZONE2_FILES:
        (tools / rel).write_text(f"# framework {version}")


def _backups(deployed: Path, rel: str) -> dict[str, str]:
    target = deployed / rel
    return {p.name: p.read_text() for p in target.parent.glob(target.name + ".bak*")}


@pytest.mark.parametrize("rel", ZONE2_FILES)
def test_user_edit_is_still_there_after_two_later_updates(installed, rel):
    tools, deployed, home_defaults, ductor_home = installed
    (deployed / rel).write_text("# my own change")

    _release(tools, "v2")
    _walk_and_copy(home_defaults, ductor_home)
    _release(tools, "v3")
    _walk_and_copy(home_defaults, ductor_home)
    _release(tools, "v4")
    _walk_and_copy(home_defaults, ductor_home)

    assert (deployed / rel).read_text() == "# framework v4"
    name = Path(rel).name
    assert _backups(deployed, rel) == {f"{name}.bak": "# my own change"}


@pytest.mark.parametrize("rel", ZONE2_FILES)
def test_untouched_framework_file_is_replaced_without_backups(installed, rel):
    tools, deployed, home_defaults, ductor_home = installed

    for version in ("v2", "v3", "v4"):
        _release(tools, version)
        _walk_and_copy(home_defaults, ductor_home)

    assert (deployed / rel).read_text() == "# framework v4"
    assert _backups(deployed, rel) == {}


@pytest.mark.parametrize("rel", ZONE2_FILES)
def test_restart_with_the_same_version_makes_no_backup(installed, rel):
    tools, deployed, home_defaults, ductor_home = installed
    (deployed / rel).write_text("# my own change")
    _release(tools, "v2")
    _walk_and_copy(home_defaults, ductor_home)
    before = _backups(deployed, rel)

    for _ in range(3):
        _walk_and_copy(home_defaults, ductor_home)

    assert _backups(deployed, rel) == before
    assert (deployed / rel).read_text() == "# framework v2"


@pytest.mark.parametrize("rel", ZONE2_FILES)
def test_a_second_edit_never_replaces_the_first_backup(installed, rel):
    tools, deployed, home_defaults, ductor_home = installed
    name = Path(rel).name
    (deployed / rel).write_text("# first edit")
    _release(tools, "v2")
    _walk_and_copy(home_defaults, ductor_home)
    (deployed / rel).write_text("# second edit")
    _release(tools, "v3")
    _walk_and_copy(home_defaults, ductor_home)

    backups = _backups(deployed, rel)

    assert backups[f"{name}.bak"] == "# first edit"
    assert sorted(backups.values()) == ["# first edit", "# second edit"]
    assert (deployed / rel).read_text() == "# framework v3"


@pytest.mark.parametrize("rel", ZONE2_FILES)
def test_install_without_a_record_keeps_the_old_file_once(installed, rel):
    tools, deployed, home_defaults, ductor_home = installed
    (ductor_home / ".zone2_installed.json").unlink()  # an install from before the record
    (deployed / rel).write_text("# my own change")

    _release(tools, "v2")
    _walk_and_copy(home_defaults, ductor_home)
    _release(tools, "v3")
    _walk_and_copy(home_defaults, ductor_home)

    assert (deployed / rel).read_text() == "# framework v3"
    assert list(_backups(deployed, rel).values()) == ["# my own change"]
    assert (ductor_home / ".zone2_installed.json").is_file()


def test_unreadable_record_is_treated_as_missing(installed):
    tools, deployed, home_defaults, ductor_home = installed
    (ductor_home / ".zone2_installed.json").write_text("not json")
    (deployed / "_tool_shared.py").write_text("# my own change")
    _release(tools, "v2")

    _walk_and_copy(home_defaults, ductor_home)

    assert (deployed / "_tool_shared.py").read_text() == "# framework v2"
    assert list(_backups(deployed, "_tool_shared.py").values()) == ["# my own change"]


# Exact pre-vault release bytes (dcfbcde), retained without requiring Git at test runtime.
_LEGACY_GIT_HELPER = r"""#!/bin/sh
# Git credential helper: answers with a GitHub token for https://github.com only.
#
# Register it with:  git config --global credential.helper '!/path/to/git_credential_from_ductor_env.sh'
#
# The token comes from GH_TOKEN or GITHUB_TOKEN in the environment (a non-empty value there
# wins) and otherwise from the secrets store named by DUCTOR_SECRETS_FILE (default
# /ductor/secrets.env). Only those two keys are read from the file; it is never sourced. A value
# that is another key of the same file (KEY=$OTHER_KEY) takes that key's value, one step and in file
# order, as tools/_secrets.py does; a reference to a key that is not there stays as written.
# The file is read the way that loader reads it: the last of a repeated key wins and every leading and
# trailing quote of a value is removed.
# Reading the file here keeps the helper fast and free of Python on every git call.
set -eu

usage() {
    cat <<'EOF'
Usage: git_credential_from_ductor_env.sh get|store|erase

Git runs this as a credential helper. "get" reads the request (protocol=, host=) from stdin
and, for https://github.com only, prints username=x-access-token and password=<token>. For any
other request it prints nothing. "store" and "erase" do nothing.
EOF
}

case "${1:-}" in
    -h | --help)
        usage
        exit 0
        ;;
    get) ;;
    *) exit 0 ;;
esac

protocol=""
host=""
while IFS= read -r line; do
    line=${line%"$(printf '\r')"}
    [ -n "$line" ] || break
    case "$line" in
        protocol=*) protocol=${line#protocol=} ;;
        host=*) host=${line#host=} ;;
    esac
done

[ "$protocol" = "https" ] && [ "$host" = "github.com" ] || exit 0

store_value() {
    awk -v wanted="$1" '
        {
            line = $0
            sub(/\r$/, "", line)
            eq = index(line, "=")
            if (eq == 0) next
            key = substr(line, 1, eq - 1)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
            if (key == "" || substr(key, 1, 1) == "#") next
            value = substr(line, eq + 1)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
            gsub(/^"+|"+$/, "", value)
            gsub(/^\047+|\047+$/, "", value)
            if (!(key in values)) order[++count] = key
            values[key] = value
        }
        END {
            for (i = 1; i <= count; i++) {
                key = order[i]
                ref = substr(values[key], 2)
                if (substr(values[key], 1, 1) == "$" && ref in values) values[key] = values[ref]
            }
            if (wanted in values) print values[wanted]
        }
    ' "${DUCTOR_SECRETS_FILE:-/ductor/secrets.env}" 2>/dev/null || true
}

token=${GH_TOKEN:-}
[ -n "$token" ] || token=${GITHUB_TOKEN:-}
[ -n "$token" ] || token=$(store_value GH_TOKEN)
[ -n "$token" ] || token=$(store_value GITHUB_TOKEN)
[ -n "$token" ] || exit 0

printf 'username=x-access-token\n'
printf 'password=%s\n' "$token"
"""


def test_original_git_shell_helper_is_upgraded_atomically(tmp_path, caplog):
    from ductor_bot.workspace import init as workspace_init

    defaults = Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults"
    home = tmp_path / "home"
    relative = Path("workspace/tools/git_credential_from_ductor_env.sh")
    installed = home / relative
    installed.parent.mkdir(parents=True)
    installed.write_text(_LEGACY_GIT_HELPER)
    installed.chmod(0o755)
    assert workspace_init._digest(installed) in workspace_init._LEGACY_GIT_HELPER_HASHES
    _walk_and_copy(defaults, home)
    assert installed.read_bytes() == (defaults / relative).read_bytes()
    assert installed.stat().st_mode & 0o777 == 0o755
    assert (installed.parent / "git_credential_from_ductor_env.py").is_file()
    assert (installed.parent / "_secrets.py").is_file()
    before = installed.stat().st_mtime_ns
    caplog.clear()
    _walk_and_copy(defaults, home)
    assert installed.stat().st_mtime_ns == before
    assert "Git credential helper left unchanged" not in caplog.text


@pytest.mark.parametrize("kind", ["edited", "symlink", "dangling"])
def test_customized_git_shell_helper_is_preserved_with_warning(tmp_path, caplog, kind):
    defaults = Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults"
    home = tmp_path / "home"
    installed = home / "workspace/tools/git_credential_from_ductor_env.sh"
    installed.parent.mkdir(parents=True)
    if kind == "edited":
        installed.write_text(_LEGACY_GIT_HELPER + "# synthetic-user-edit\n")
        before = installed.read_bytes(), installed.stat().st_mtime_ns
    else:
        source = tmp_path / "original.sh"
        if kind == "symlink":
            source.write_text(_LEGACY_GIT_HELPER)
        installed.symlink_to(source)
    _walk_and_copy(defaults, home)
    if kind == "edited":
        assert installed.read_bytes() == before[0]
        assert installed.stat().st_mtime_ns == before[1]
    else:
        assert installed.is_symlink()
        assert source.exists() == (kind == "symlink")
        if source.exists():
            assert source.read_text() == _LEGACY_GIT_HELPER
    assert "Git credential helper left unchanged" in caplog.text
    assert "customized" in caplog.text
    assert "synthetic-user-edit" not in caplog.text

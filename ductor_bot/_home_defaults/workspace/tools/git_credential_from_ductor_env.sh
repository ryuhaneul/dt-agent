#!/bin/sh
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
# The Python helper shares plaintext parsing and encrypted RPC with tools/_secrets.py.
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

exec python3 "$(dirname "$0")/git_credential_from_ductor_env.py"

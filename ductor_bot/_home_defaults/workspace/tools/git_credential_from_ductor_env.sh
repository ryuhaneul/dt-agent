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

#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: $0 <git-ref> [--mode offline|live] [--live-token-file FILE] [--repo PATH] [--keep] [--stage config|offline|build-extras|extras-exclusion|live-lifecycle|live-webhook-api|live-sub-agents]" >&2
}

if (( $# == 0 )) || [[ $1 == --* ]]; then
    usage
    exit 2
fi
REF=$1
shift
MODE=offline
STAGE=offline
LIVE_SRC=
REPO=
KEEP=0
while (( $# )); do
    case $1 in
        --mode|--live-token-file|--repo|--stage)
            if (( $# < 2 )); then usage; exit 2; fi
            case $1 in
                --mode) MODE=$2 ;;
                --live-token-file) LIVE_SRC=$2 ;;
                --repo) REPO=$2 ;;
                --stage) STAGE=$2 ;;
            esac
            shift 2
            ;;
        --keep) KEEP=1; shift ;;
        *) usage; exit 2 ;;
    esac
done
case $MODE in offline|live) ;; *) usage; exit 2 ;; esac
case $STAGE in config|offline|build-extras|extras-exclusion|live-lifecycle|live-webhook-api|live-sub-agents) ;; *) usage; exit 2 ;; esac
if [[ $MODE == live && $KEEP == 1 ]]; then
    echo "FAIL: --keep is forbidden in live mode" >&2
    exit 2
fi
if [[ $MODE == live && ( -z $LIVE_SRC || ! -f $LIVE_SRC ) ]]; then
    echo "FAIL: live mode requires --live-token-file" >&2
    exit 2
fi
if [[ -z $REPO ]]; then
    REPO=$(git rev-parse --show-toplevel)
fi
REPO=$(git -C "$REPO" rev-parse --show-toplevel)
if [[ -n $(git -C "$REPO" status --porcelain) ]]; then
    echo "FAIL: repository has uncommitted changes" >&2
    exit 3
fi
if [[ $STAGE != config && $STAGE != offline && $STAGE != build-extras && $STAGE != extras-exclusion && $STAGE != live-lifecycle && $STAGE != live-webhook-api && $STAGE != live-sub-agents ]]; then
    echo "FAIL: stage $STAGE is not implemented" >&2
    exit 2
fi
if [[ ( $STAGE == live-lifecycle || $STAGE == live-webhook-api || $STAGE == live-sub-agents ) && $MODE != live ]]; then
    echo "FAIL: $STAGE requires --mode live" >&2
    exit 2
fi
EXPECTED=$(git -C "$REPO" rev-parse --verify "$REF^{commit}")

umask 077
TMP=$(mktemp -d /tmp/ductor-verify.XXXXXX)
chmod 700 "$TMP"
P="ductorverify$(date +%s)$$"

dc() {
    local files=(-f "$TMP/repo/compose.yaml")
    if [[ -f $TMP/repo/compose.override.yml ]]; then
        files+=(-f "$TMP/repo/compose.override.yml")
    fi
    docker compose --project-directory "$TMP/repo" -p "$P" \
        --env-file "$TMP/repo/.env" "${files[@]}" \
        -f "$TMP/verify.override.yml" "$@"
}

recreate_bot() {
    dc down -v --remove-orphans || return 1
    local rc=0
    dc up -d ductor-bot || rc=$?
    if [[ $STAGE == extras-exclusion ]]; then
        extras_exclusion_capture_events || return 1
    fi
    return "$rc"
}

cleanup_actions() {
    local failed=0 project tag images remaining
    local projects=("$P" "$P-matrix-extra" "$P-no-extra-subs" "$P-u027" "$P-u077")
    local tags=("$P-ductor-bot:latest" "$P-browser:latest"
                "$P-matrix-extra-ductor-bot:latest" "$P-matrix-extra-browser:latest" "$P-webhook-api-enabled:latest"
                "ductorverify-no-extra-subs:$P"
                "$P-u027-ductor-bot:latest" "$P-u027-browser:latest"
                "$P-u077-ductor-bot:latest" "$P-u077-browser:latest")
    if [[ -f $TMP/repo/compose.yaml && -f $TMP/repo/.env && -f $TMP/verify.override.yml ]]; then
        dc down -v --remove-orphans || { echo "FAIL cleanup: compose down $P" >&2; failed=1; }
    fi
    if [[ -f $TMP/verify-matrix-extra.override.yml && -f $TMP/env-matrix-extra ]]; then
        docker compose --project-directory "$TMP/repo" -p "$P-matrix-extra" \
            --env-file "$TMP/env-matrix-extra" -f "$TMP/repo/compose.yaml" \
            -f "$TMP/verify-matrix-extra.override.yml" down -v --remove-orphans ||
            { echo "FAIL cleanup: compose down $P-matrix-extra" >&2; failed=1; }
    fi
    if [[ -f $TMP/verify-no-extra-subs.override.yml && -f $TMP/env-no-extra-subs ]]; then
        docker compose --project-directory "$TMP/repo" -p "$P-no-extra-subs" \
            --env-file "$TMP/env-no-extra-subs" -f "$TMP/repo/compose.yaml" \
            -f "$TMP/verify-no-extra-subs.override.yml" down -v --remove-orphans ||
            { echo "FAIL cleanup: compose down $P-no-extra-subs" >&2; failed=1; }
    fi
    local suffix
    for suffix in u027 u077; do
        if [[ -f $TMP/verify-$suffix.override.yml && -f $TMP/repo-$suffix/.env ]]; then
            dc_umask "$suffix" down -v --remove-orphans ||
                { echo "FAIL cleanup: compose down $P-$suffix" >&2; failed=1; }
        fi
    done
    if ! images=$(docker image ls --format '{{.Repository}}:{{.Tag}}'); then
        echo "FAIL cleanup: image listing" >&2
        failed=1
        images=
    fi
    for tag in "${tags[@]}"; do
        if grep -Fxq -- "$tag" <<<"$images"; then
            docker image rm -f "$tag" ||
                { echo "FAIL cleanup: image removal $tag" >&2; failed=1; }
        fi
    done

    for project in "${projects[@]}"; do
        for kind in container network volume; do
            case $kind in
                container) remaining=$(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.ID}}') ;;
                network) remaining=$(docker network ls --filter "label=com.docker.compose.project=$project" --format '{{.ID}}') ;;
                volume) remaining=$(docker volume ls --filter "label=com.docker.compose.project=$project" --format '{{.Name}}') ;;
            esac
            if (( $? != 0 )); then
                echo "FAIL cleanup: $project $kind listing" >&2
                failed=1
            elif [[ -n $remaining ]]; then
                echo "FAIL cleanup: $project $kind remains" >&2
                failed=1
            fi
        done
    done
    if ! images=$(docker image ls --format '{{.Repository}}:{{.Tag}}'); then
        echo "FAIL cleanup: image listing after removal" >&2
        failed=1
    else
        for tag in "${tags[@]}"; do
            if grep -Fxq -- "$tag" <<<"$images"; then
                echo "FAIL cleanup: image remains $tag" >&2
                failed=1
            fi
        done
    fi
    return "$failed"
}

cleanup() {
    local rc=$? cleanup_rc=0 log_rc=0 check_rc=0
    local -a statuses
    trap - EXIT INT TERM
    set +e
    cleanup_actions 2>&1 | tee -a "$TMP/verify.log"
    statuses=("${PIPESTATUS[@]}")
    cleanup_rc=${statuses[0]}
    log_rc=${statuses[1]}
    if (( cleanup_rc != 0 || log_rc != 0 )); then
        if (( log_rc != 0 )); then echo "FAIL cleanup: verification log" >&2; fi
        if (( rc == 0 )); then rc=1; fi
    fi
    if [[ $KEEP == 1 ]]; then
        echo "Kept path: $TMP (fake tokens only; remove it with rm -rf after checking)" | tee -a "$TMP/verify.log"
        statuses=("${PIPESTATUS[@]}")
        if (( statuses[1] != 0 && rc == 0 )); then rc=1; fi
    fi
    check_log_secrets 2>&1 | tee -a "$TMP/verify.log"
    statuses=("${PIPESTATUS[@]}")
    check_rc=${statuses[0]}
    log_rc=${statuses[1]}
    if (( check_rc != 0 || log_rc != 0 )); then
        if (( log_rc != 0 )); then echo "FAIL cleanup: verification log" >&2; fi
        if (( rc == 0 )); then rc=1; fi
    fi
    if [[ $KEEP != 1 ]]; then
        rm -rf -- "$TMP"
        if [[ -e $TMP ]]; then
            echo "FAIL cleanup: $TMP" >&2
            if (( rc == 0 )); then rc=1; fi
        fi
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

prepare() {
    (
        umask 022
        # Keep the private temp parent at 0700; normal clone modes avoid masking image permission bugs.
        git clone --no-local "$REPO" "$TMP/repo"
        git -C "$TMP/repo" checkout --detach "$REF"
    )
    if [[ $(git -C "$TMP/repo" rev-parse HEAD) != "$EXPECTED" ]]; then
        echo "FAIL: checked-out ref differs from source" >&2
        return 1
    fi
    if [[ $STAGE == live-lifecycle && ${VERIFY_REMOVE_PID_CLEANUP-} == 1 ]]; then
        python3 - "$TMP/repo/docker/ductor-bot/entrypoint.sh" <<'PYPIDMUTATE'
from pathlib import Path
import sys
path = Path(sys.argv[1])
content = path.read_text()
line = "rm -f /ductor/bot.pid || exit 1\n"
if content.count(line) != 1:
    raise SystemExit("FAIL (restart injection) PID cleanup line not found once")
path.write_text(content.replace(line, "", 1))
PYPIDMUTATE
        echo "OBSERVED (restart injection) isolated clone PID cleanup removed"
    fi

    if [[ -n ${DOCKER_HOST+x} ]]; then
        echo "DOCKER_HOST: set"
    else
        echo "DOCKER_HOST: unset"
    fi
    if [[ -n ${DOCKER_CONTEXT+x} ]]; then
        echo "DOCKER_CONTEXT: set"
    else
        echo "DOCKER_CONTEXT: unset"
    fi
    local docker_context docker_daemon
    docker_context=$(docker context show)
    docker_daemon=$(docker info --format '{{.Name}}')
    echo "Docker context: $docker_context"
    echo "Docker daemon: $docker_daemon"

    local token_file
    if [[ $MODE == live ]]; then
        token_file="$TMP/live-token"
        install -m 600 "$LIVE_SRC" "$token_file"
    else
        token_file="$TMP/fake-token"
        printf '%s\n' '123456:FAKE_TOKEN_FOR_VERIFY_ONLY' > "$token_file"
        chmod 600 "$token_file"
    fi
    local allowed_users=1
    local -a allowed_groups=()
    local -a onboard_features=()
    if [[ $STAGE == live-webhook-api ]]; then onboard_features=(--webhook --api); fi
    (
        cd "$TMP/repo"
        ./scripts/onboard.sh "${onboard_features[@]}" --no-prompt --home "$TMP/home" \
            --telegram-token-file "$token_file" --allowed-users "$allowed_users" "${allowed_groups[@]}" --tz UTC
    )
    printf 'VERIFY_PROVIDER_PROBE=1\n' >> "$TMP/home/.env"

    cat > "$TMP/verify.override.yml" <<YAML
services:
  ductor-bot:
    container_name: ${P}-bot
    restart: "no"
  browser:
    container_name: ${P}-browser
    restart: "no"
networks:
  ductor-net:
    name: ${P}-net
YAML
}

assert_config() {
    dc config --format json > "$TMP/cfg.json"
    dc config --no-env-resolution --format json > "$TMP/cfg-paths.json"
    python3 - "$TMP/cfg.json" "$TMP/cfg-paths.json" "$TMP/repo" "$TMP" "$P" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text())
path_config = json.loads(Path(sys.argv[2]).read_text())
repo = Path(sys.argv[3]).resolve()
tmp = Path(sys.argv[4]).resolve()
project = sys.argv[5]

def require(ok, message):
    if not ok:
        raise SystemExit("FAIL config: " + message)

services = config.get("services", {})
require(set(services) == {"ductor-bot", "browser"}, "service names")
networks = config.get("networks", {})
require(set(networks) == {"ductor-net"}, "network keys")
require(all(value["name"].startswith(project) for value in networks.values()), "network names")
for name, service in services.items():
    context = Path(service["build"]["context"]).resolve()
    require(context.is_relative_to(repo), name + " build context")
    require(service.get("restart") == "no", name + " restart")
    require(service.get("container_name", "").startswith(project), name + " container name")
    require(set(service.get("networks", {})) <= set(networks), name + " networks")
    for volume in service.get("volumes", []):
        require(Path(volume["source"]).resolve().is_relative_to(tmp), name + " volume")
    env_files = path_config["services"][name].get("env_file", [])
    require(bool(env_files), name + " env_file missing")
    for entry in env_files:
        path = entry["path"] if isinstance(entry, dict) else entry
        require(Path(path).resolve().is_relative_to(tmp), name + " env_file")
print("PASS config isolation")
PY
}

check_log_secrets() {
    local token_file="$TMP/fake-token"
    if [[ $MODE == live ]]; then token_file="$TMP/live-token"; fi
    # Errors before token creation cannot contain a generated or copied token.
    if [[ ! -f $token_file ]]; then return 0; fi
    local token browser_token=
    token=$(cat "$token_file") || return 1
    if [[ -f $TMP/home/.env.shared ]]; then
        browser_token=$(python3 - "$TMP/home/.env.shared" <<'PYCODE'
import json
import sys
from pathlib import Path
for line in Path(sys.argv[1]).read_text().splitlines():
    if line.startswith("BROWSER_TOKEN="):
        print(json.loads(line.partition("=")[2]))
        break
PYCODE
) || return 1
    fi
    if [[ -z $token ]]; then
        echo "FAIL: token self-check could not read token" >&2
        return 1
    fi
    if grep -qF -- "$token" "$TMP/verify.log" || \
       { [[ -n $browser_token ]] && grep -qF -- "$browser_token" "$TMP/verify.log"; }; then
        echo "FAIL: token appeared in verification log" >&2
        return 1
    fi
    echo "PASS token log check"
}


EXPECTED_AUTH_FAIL_RE='TelegramUnauthorizedError: Telegram server says - Unauthorized'

dc_alt() {
    local project=$1 env_file=$2 override=$3
    shift 3
    docker compose --project-directory "$TMP/repo" -p "$project" \
        --env-file "$env_file" -f "$TMP/repo/compose.yaml" \
        -f "$override" "$@"
}

make_alt_home() {
    local suffix=$1 home="$TMP/home-$1" env_file="$TMP/env-$1"
    local override="$TMP/verify-$1.override.yml" project="$P-$1"
    (
        cd "$TMP/repo"
        ./scripts/onboard.sh --no-prompt --home "$home" \
            --telegram-token-file "$TMP/fake-token" --allowed-users 1 --tz UTC
    )
    python3 - "$TMP/repo/.env" "$env_file" "$home" <<'PY'
import json
import sys
from pathlib import Path
source, target, home = map(Path, sys.argv[1:])
lines = source.read_text().splitlines()
lines = ["DUCTOR_HOME=" + json.dumps(str(home)) if line.startswith("DUCTOR_HOME=") else line for line in lines]
target.write_text("\n".join(lines) + "\n")
PY
    cat > "$override" <<YAML
services:
  ductor-bot:
    container_name: ${project}-bot
    restart: "no"
  browser:
    container_name: ${project}-browser
    restart: "no"
networks:
  ductor-net:
    name: ${project}-net
YAML
}


dc_umask() {
    local suffix=$1 repo="$TMP/repo-$1" project="$P-$1"
    shift
    docker compose --project-directory "$repo" -p "$project" \
        --env-file "$repo/.env" -f "$repo/compose.yaml" \
        -f "$TMP/verify-$suffix.override.yml" "$@"
}

verify_restricted_umasks() {
    local mode suffix repo project browser_state i failed=0 bot_started
    for mode in 027 077; do
        suffix="u$mode"
        repo="$TMP/repo-$suffix"
        project="$P-$suffix"
        (
            umask "$mode"
            git clone --no-local "$REPO" "$repo"
            git -C "$repo" checkout --detach "$REF"
        )
        if [[ $(git -C "$repo" rev-parse HEAD) != "$EXPECTED" ]]; then
            echo "FAIL (umask $mode) clone ref"; failed=1; continue
        fi
        (
            cd "$repo"
            ./scripts/onboard.sh --no-prompt --home "$TMP/home-$suffix" \
                --telegram-token-file "$TMP/fake-token" --allowed-users 1 --tz UTC
        )
        cat > "$TMP/verify-$suffix.override.yml" <<YAML
services:
  ductor-bot:
    container_name: ${project}-bot
    restart: "no"
  browser:
    container_name: ${project}-browser
    restart: "no"
networks:
  ductor-net:
    name: ${project}-net
YAML
        if ! dc_umask "$suffix" build >"$TMP/$suffix-build.log" 2>&1; then
            echo "FAIL (umask $mode) build"; failed=1; continue
        fi
        if ! docker run --rm --name "$project-inspect-bot" --entrypoint sh \
            "$project-ductor-bot:latest" -c \
            'ls -l /usr/local/bin/entrypoint.sh /usr/local/lib/python3.11/site-packages/ductor_bot/__init__.py' \
            >"$TMP/$suffix-bot-files.log" 2>&1; then
            echo "FAIL (umask $mode) bot image files"; failed=1; continue
        fi
        if ! docker run --rm --name "$project-inspect-browser" --entrypoint sh \
            "$project-browser:latest" -c 'ls -ld /app /app/server.js /app/package.json' \
            >"$TMP/$suffix-browser-files.log" 2>&1; then
            echo "FAIL (umask $mode) browser image files"; failed=1; continue
        fi
        echo "OBSERVED (umask $mode) bot entrypoint: $(grep '/usr/local/bin/entrypoint.sh' "$TMP/$suffix-bot-files.log")"
        echo "OBSERVED (umask $mode) browser server: $(grep '/app/server.js' "$TMP/$suffix-browser-files.log")"
        if ! dc_umask "$suffix" up -d >"$TMP/$suffix-up.log" 2>&1; then
            echo "FAIL (umask $mode) compose up"; failed=1; continue
        fi
        browser_state=$(docker inspect -f '{{.State.Health.Status}}' "$project-browser")
        bot_started=0
        for ((i=0; i<20; i++)); do
            dc_umask "$suffix" logs --no-color ductor-bot >"$TMP/$suffix-bot.log" 2>&1
            if grep -Fq 'Starting Telegram bot (aiogram, long-polling)...' "$TMP/$suffix-bot.log"; then
                bot_started=1
                break
            fi
            sleep 1
        done
        if [[ $browser_state != healthy || $bot_started != 1 ]]; then
            echo "FAIL (umask $mode) browser=$browser_state Telegram_start=$bot_started"
            failed=1
        else
            echo "PASS (umask $mode) browser healthy; Telegram startup marker"
        fi
    done
    return "$failed"
}


offline_checks() {
    dc up -d
    docker inspect -f '{{json .State.Health.Log}}' "$P-browser" > "$TMP/health.json"
    docker inspect -f '{{.State.StartedAt}}' "$P-bot" > "$TMP/bot-start.txt"
    python3 - "$TMP/health.json" "$TMP/bot-start.txt" <<'PY' || { echo "FAIL (browser order) browser health order"; return 1; }
import datetime
import json
import re
import sys
from pathlib import Path
events = json.loads(Path(sys.argv[1]).read_text())
start = Path(sys.argv[2]).read_text().strip()
def parse(value):
    value = re.sub(r"(\.\d{6})\d+", r"\1", value)
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
first = next((event for event in events if event["ExitCode"] == 0), None)
if first is None or not parse(first["End"]) < parse(start):
    raise SystemExit(1)
PY
    echo "PASS (browser order) browser healthy before bot start"

    local i
    for ((i=0; i<90; i++)); do
        dc logs --no-color ductor-bot > "$TMP/bot.log" 2>&1
        if grep -Fq 'Starting Telegram bot (aiogram, long-polling)...' "$TMP/bot.log"; then break; fi
        sleep 1
    done
    if ! grep -Fq 'Starting Telegram bot (aiogram, long-polling)...' "$TMP/bot.log"; then
        echo "FAIL (startup marker) Telegram startup marker absent"; return 1
    fi
    echo "PASS (startup marker) Telegram startup marker"

    for ((i=0; i<20; i++)); do
        dc logs --no-color ductor-bot > "$TMP/bot.log" 2>&1
        if grep -Eq "$EXPECTED_AUTH_FAIL_RE" "$TMP/bot.log"; then break; fi
        sleep 1
    done
    if grep -Fq 'Polling allowed_updates=' "$TMP/bot.log" ||
       grep -Fq 'Bot online:' "$TMP/bot.log" ||
       ! grep -Eq "$EXPECTED_AUTH_FAIL_RE" "$TMP/bot.log"; then
        echo "FAIL (auth failure) expected authentication failure"; return 1
    fi
    echo "PASS (auth failure) expected authentication failure"
    local bot_running bot_exit retry_lines
    for ((i=0; i<10; i++)); do
        bot_running=$(docker inspect -f '{{.State.Running}}' "$P-bot")
        if [[ $bot_running == false ]]; then break; fi
        sleep 1
    done
    bot_exit=$(docker inspect -f '{{.State.ExitCode}}' "$P-bot")
    retry_lines=$(grep -Eic 'retry|restarting|restart' "$TMP/bot.log" || true)
    echo "OBSERVED (auth failure) auth_pattern=$EXPECTED_AUTH_FAIL_RE"
    echo "OBSERVED (auth failure) bot_running=$bot_running bot_exit=$bot_exit retry_lines=$retry_lines"

    local health_code snapshot_code b_rc
    health_code=$(docker exec "$P-browser" curl -s -o /dev/null -w '%{http_code}' http://localhost:18800/health)
    snapshot_code=$(docker exec "$P-browser" curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:18800/snapshot)
    if docker run --rm --name "$P-browser-tokenless" -e BROWSER_TOKEN= "$P-browser:latest" >"$TMP/b.log" 2>&1; then b_rc=0; else b_rc=$?; fi
    if [[ $health_code != 200 || $snapshot_code != 401 || $b_rc == 0 ]] ||
       ! grep -Fq 'BROWSER_TOKEN is required' "$TMP/b.log"; then
        echo "FAIL (browser auth) browser health/auth"; return 1
    fi
    echo "PASS (browser auth) browser health=200 snapshot=401 empty-token rejected"

    local bot_keys browser_keys hb hw
    bot_keys=$(dc run --rm --no-deps --entrypoint python3 ductor-bot -c \
        'import os; print("\n".join(sorted(k for k in os.environ if k in ("BROWSER_TOKEN","TZ","VERIFY_PROVIDER_PROBE"))))')
    browser_keys=$(dc run --rm --no-deps --entrypoint node browser -e \
        'for (const k of ["BROWSER_TOKEN","TZ","VERIFY_PROVIDER_PROBE"]) if (k in process.env) console.log(k)')
    if [[ $bot_keys != $'BROWSER_TOKEN\nTZ\nVERIFY_PROVIDER_PROBE' ||
          $browser_keys != $'BROWSER_TOKEN\nTZ' ]]; then
        echo "FAIL (env isolation) environment key isolation"; return 1
    fi
    hb=$(dc run --rm --no-deps --entrypoint python3 ductor-bot -c \
        'import os,hashlib;print(hashlib.sha256(os.environ.get("BROWSER_TOKEN","").encode()).hexdigest())')
    hw=$(dc run --rm --no-deps --entrypoint node browser -e \
        'console.log(require("crypto").createHash("sha256").update(process.env.BROWSER_TOKEN || "").digest("hex"))')
    if [[ ${#hb} != 64 || ${#hw} != 64 || $hb != "$hw" ]]; then
        echo "FAIL (env isolation) shared token mismatch"; return 1
    fi
    echo "PASS (env isolation) env isolation; bot_len=64 browser_len=64 same=true"

    if ! python3 - "$TMP/home/logs" "$TMP/bot-start.txt" <<'PY'
import datetime
import re
import sys
from pathlib import Path
logs = Path(sys.argv[1])
started = Path(sys.argv[2]).read_text().strip()
started = re.sub(r"(\.\d{6})\d+", r"\1", started)
start = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
if not logs.is_dir() or not any(datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc) >= start for path in logs.iterdir() if path.is_file()):
    raise SystemExit(1)
PY
    then echo "FAIL (isolated logs) host logs path or mtime"; return 1; fi
    if [[ $(dc run --rm --no-deps --entrypoint sh ductor-bot -c \
        'test ! -e /home/ductor/.ductor && echo NO_HOME_STATE') != NO_HOME_STATE ]]; then
        echo "FAIL (isolated logs) baked home state"; return 1
    fi
    echo "PASS (isolated logs) logs in isolated home; no baked home state"

    local process_attrs init_cmd
    process_attrs=$(dc run --rm --no-deps --entrypoint sh ductor-bot -c \
        'command -v ps; locale 2>&1 | grep -c "LC_ALL=C.UTF-8"')
    if [[ $process_attrs != *'/ps'* || $process_attrs != *$'\n1' ]]; then
        echo "FAIL (image attributes) process or locale attributes"; return 1
    fi
    init_cmd=$(dc run --rm --no-deps --entrypoint cat ductor-bot /proc/1/cmdline | tr '\0' ' ')
    if [[ $init_cmd == *docker-init* ]]; then
        echo "OBSERVED (image attributes) compose run init applied"
    elif [[ $(docker inspect -f '{{.HostConfig.Init}}' "$P-bot") == true ]]; then
        echo "OBSERVED (image attributes) compose run init absent; service init=true"
    else
        echo "FAIL (image attributes) init unavailable"; return 1
    fi
    echo "PASS (image attributes) image and process attributes"

    make_alt_home matrix-extra
    python3 - "$TMP/home-matrix-extra/config/config.json" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
config = json.loads(path.read_text())
config["transports"] = ["telegram", "matrix"]
config["matrix"].update({
    "homeserver": "https://example.invalid",
    "user_id": "@probe:example.invalid",
    "password": "fake-password",
    "allowed_rooms": ["!probe:example.invalid"],
})
path.write_text(json.dumps(config, indent=2) + "\n")
PY
    docker tag "$P-ductor-bot:latest" "$P-matrix-extra-ductor-bot:latest"
    local matrix_extra_rc
    if dc_alt "$P-matrix-extra" "$TMP/env-matrix-extra" "$TMP/verify-matrix-extra.override.yml" run --rm --no-deps ductor-bot >"$TMP/matrix-extra-missing.log" 2>&1; then matrix_extra_rc=0; else matrix_extra_rc=$?; fi
    if [[ $matrix_extra_rc != 3 ]] || ! grep -Fq 'DUCTOR_EXTRAS' "$TMP/matrix-extra-missing.log"; then
        echo "FAIL (matrix extra) missing Matrix dependency exit"; return 1
    fi
    python3 - "$TMP/env-matrix-extra" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
lines = path.read_text().splitlines()
lines = ['DUCTOR_EXTRAS="matrix"' if line.startswith("DUCTOR_EXTRAS=") else line for line in lines]
path.write_text("\n".join(lines) + "\n")
PY
    dc_alt "$P-matrix-extra" "$TMP/env-matrix-extra" "$TMP/verify-matrix-extra.override.yml" build ductor-bot >"$TMP/matrix-extra-build.log" 2>&1
    if timeout -s TERM 45 docker compose --project-directory "$TMP/repo" -p "$P-matrix-extra" \
        --env-file "$TMP/env-matrix-extra" -f "$TMP/repo/compose.yaml" -f "$TMP/verify-matrix-extra.override.yml" \
        run --rm --no-deps ductor-bot >"$TMP/matrix-extra-installed.log" 2>&1; then matrix_extra_rc=0; else matrix_extra_rc=$?; fi
    if [[ $matrix_extra_rc == 3 ]] ||
       ! grep -Fq 'Starting Telegram bot (aiogram, long-polling)...' "$TMP/matrix-extra-installed.log"; then
        echo "FAIL (matrix extra) Matrix extra startup"; return 1
    fi
    echo "PASS (matrix extra) missing extra exit=3; installed extra reaches Telegram"
    echo "OBSERVED (matrix extra) installed_exit=$matrix_extra_rc"

    docker build -f "$TMP/repo/docker/ductor-bot/Dockerfile" --build-arg DUCTOR_EXTRAS= \
        -t "ductorverify-no-extra-subs:$P" "$TMP/repo" >"$TMP/no-extra-subs-build.log" 2>&1
    if [[ $(docker run --rm --name "$P-no-extra-subs-extras-check" --entrypoint sh "ductorverify-no-extra-subs:$P" -c \
        'printf "[%s]" "$DUCTOR_BUILD_EXTRAS"') != '[]' ]]; then
        echo "FAIL (no-extra subs) no-extra image"; return 1
    fi
    if [[ ${VERIFY_FAIL_AT-} == no-extra-subs ]]; then
        echo "FAIL (no-extra subs) injected failure"; return 1
    fi
    make_alt_home no-extra-subs
    python3 - "$TMP/env-no-extra-subs" "$TMP/home-no-extra-subs/agents.json" "$TMP/verify-no-extra-subs.override.yml" "ductorverify-no-extra-subs:$P" <<'PY'
import json
import sys
from pathlib import Path
env_file, agents_file, override, image = map(Path, sys.argv[1:])
lines = env_file.read_text().splitlines()
lines = ['DUCTOR_EXTRAS=""' if line.startswith("DUCTOR_EXTRAS=") else line for line in lines]
env_file.write_text("\n".join(lines) + "\n")
agents_file.write_text(json.dumps([
    {"name": "missing-nio", "transport": "matrix",
     "matrix": {"homeserver": "https://example.invalid", "user_id": "@sub:example.invalid",
                "password": "fake-password", "allowed_rooms": ["!probe:example.invalid"]}},
    {"name": "invalid-merge", "transport": "matrix"}
]) + "\n")
override.write_text(override.read_text().replace('    restart: "no"', f'    image: {image}\n    restart: "no"', 1))
PY
    dc_alt "$P-no-extra-subs" "$TMP/env-no-extra-subs" "$TMP/verify-no-extra-subs.override.yml" config --format json >"$TMP/no-extra-subs-config.json"
    if ! python3 - "$TMP/no-extra-subs-config.json" "ductorverify-no-extra-subs:$P" <<'PYCODE'
import json
import sys
from pathlib import Path
service = json.loads(Path(sys.argv[1]).read_text())["services"]["ductor-bot"]
if service.get("image") != sys.argv[2]:
    raise SystemExit(1)
PYCODE
    then echo "FAIL (no-extra subs) image selection"; return 1; fi
    local no_extra_subs_rc
    if dc_alt "$P-no-extra-subs" "$TMP/env-no-extra-subs" "$TMP/verify-no-extra-subs.override.yml" \
        run --rm --no-deps --pull never ductor-bot >"$TMP/no-extra-subs.log" 2>&1; then no_extra_subs_rc=0; else no_extra_subs_rc=$?; fi
    if [[ $no_extra_subs_rc == 3 ]] ||
       ! grep -Eq "Sub-agent 'missing-nio' (excluded|not started)" "$TMP/no-extra-subs.log" ||
       ! grep -Eq "Sub-agent 'item #1' (excluded|not started)" "$TMP/no-extra-subs.log" ||
       ! grep -Fq 'Starting Telegram bot (aiogram, long-polling)...' "$TMP/no-extra-subs.log"; then
        echo "FAIL (no-extra subs) sub-agent isolation"; return 1
    fi
    echo "PASS (no-extra subs) both sub-agent exclusions and Telegram startup marker observed"
    echo "OBSERVED (no-extra subs) run_exit=$no_extra_subs_rc"
    verify_restricted_umasks
}


build_extras_checks() {
    local name extra expected observed ports i
    for name in base api; do
        case $name in
            base)
                extra=
                expected='nio=False slack_bolt=False nacl=False paramiko=False'
                ;;
            api)
                extra=api
                expected='nio=False slack_bolt=False nacl=True paramiko=False'
                (
                    cd "$TMP/repo"
                    ./scripts/onboard.sh --no-prompt --home "$TMP/home" \
                        --telegram-token-file "$TMP/fake-token" --allowed-users 1 \
                        --tz UTC --api --force
                )
                ;;
        esac
        python3 - "$TMP/repo/.env" "$extra" <<'PYENV'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
lines = path.read_text().splitlines()
lines = ["DUCTOR_EXTRAS=" + json.dumps(sys.argv[2]) if line.startswith("DUCTOR_EXTRAS=")
         else line for line in lines]
path.write_text("\n".join(lines) + "\n")
PYENV
        if ! dc build ductor-bot >"$TMP/extras-$name-build.log" 2>&1; then
            echo "FAIL (extras $name) build"
            return 1
        fi
        if ! observed=$(dc run --rm --no-deps --pull never --name "$P-probe-$name" \
            --entrypoint python3 ductor-bot -c \
            'import importlib.util; print(" ".join(f"{n}={importlib.util.find_spec(n) is not None}" for n in ("nio", "slack_bolt", "nacl", "paramiko")))' \
            2>"$TMP/extras-$name-probe.err"); then
            echo "FAIL (extras $name) module probe"
            return 1
        fi
        echo "OBSERVED (extras $name) $observed"
        if [[ $observed != "$expected" ]]; then
            echo "FAIL (extras $name) module set"
            return 1
        fi

        if [[ $name == base ]]; then
            if ! dc run --rm --no-deps --pull never --name "$P-probe-openssh" \
                --entrypoint ssh ductor-bot -V >"$TMP/extras-base-ssh.log" 2>&1; then
                echo "FAIL (extras base) openssh-client missing"; return 1
            fi
            if dc run --rm --no-deps --pull never --name "$P-probe-paramiko" \
                --entrypoint python3 ductor-bot -c 'import paramiko' \
                >"$TMP/extras-base-paramiko.log" 2>&1; then
                echo "FAIL (extras base) paramiko installed"; return 1
            fi
            if ! grep -Fq "ModuleNotFoundError: No module named 'paramiko'" "$TMP/extras-base-paramiko.log"; then
                echo "FAIL (extras base) unexpected paramiko import failure"; return 1
            fi
            echo "PASS (extras base) ssh -V succeeds and paramiko import is unavailable"
            dc config --format json >"$TMP/extras-base-config.json"
            if ! python3 - "$TMP/extras-base-config.json" <<'PYCONFIG'
import json
import sys
from pathlib import Path
service = json.loads(Path(sys.argv[1]).read_text())["services"]["ductor-bot"]
if service.get("ports"):
    raise SystemExit(1)
PYCONFIG
            then echo "FAIL (extras base) config ports"; return 1; fi
            if [[ -e $TMP/repo/compose.override.yml ]]; then
                echo "FAIL (extras base) unexpected port override"
                return 1
            fi
            if ! recreate_bot >"$TMP/extras-base-up.log" 2>&1; then
                echo "FAIL (extras base) compose up"
                return 1
            fi
            ports=$(docker port "$P-bot" 2>/dev/null || true)
            if [[ -n $ports ]]; then
                echo "FAIL (extras base) published container ports"
                return 1
            fi
            for ((i=0; i<20; i++)); do
                dc logs --no-color ductor-bot >"$TMP/extras-base-bot.log" 2>&1
                if grep -Fq 'Starting Telegram bot (aiogram, long-polling)...' "$TMP/extras-base-bot.log"; then
                    break
                fi
                sleep 1
            done
            if ! grep -Fq 'Starting Telegram bot (aiogram, long-polling)...' "$TMP/extras-base-bot.log" ||
               grep -Fq 'API server listening on ' "$TMP/extras-base-bot.log"; then
                echo "FAIL (extras base) bot startup or API log"
                return 1
            fi
            echo "OBSERVED (extras base) config_ports=none docker_port=none override=absent api_start=absent"
        fi
        if [[ $name == api ]]; then
            dc config --format json >"$TMP/extras-api-config.json"
            if ! python3 - "$TMP/extras-api-config.json" <<'PYCONFIG'
import json
import sys
from pathlib import Path
ports = json.loads(Path(sys.argv[1]).read_text())["services"]["ductor-bot"].get("ports", [])
if len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1" or \
        str(ports[0].get("published")) != "8741" or ports[0].get("target") != 8741:
    raise SystemExit(1)
PYCONFIG
            then echo "FAIL (extras api) config ports"; return 1; fi
            echo "OBSERVED (extras api) config_ports=127.0.0.1:8741:8741"
        fi
        echo "PASS (extras $name)"
    done
    if dc build --build-arg DUCTOR_EXTRAS=ssh ductor-bot \
        >"$TMP/extras-removed-ssh-build.log" 2>&1; then
        echo "FAIL (removed ssh extra) build accepted"; return 1
    fi
    if ! grep -Fq 'ssh extra was removed; openssh-client is included in the image by default' \
        "$TMP/extras-removed-ssh-build.log"; then
        echo "FAIL (removed ssh extra) rejection message missing"; return 1
    fi
    echo "PASS (removed ssh extra) build rejected with migration message"
}


extras_exclusion_extras() {
    python3 - "$TMP/repo/.env" "$1" <<'PYENV'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
lines = path.read_text().splitlines()
lines = ["DUCTOR_EXTRAS=" + json.dumps(sys.argv[2]) if line.startswith("DUCTOR_EXTRAS=")
         else line for line in lines]
path.write_text("\n".join(lines) + "\n")
PYENV
}

extras_exclusion_capture_events() {
    local until
    until=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
    docker events --since "$EXTRAS_EXCLUSION_EVENT_START" --until "$until" \
        --filter type=container --filter event=create --filter event=rename \
        --filter "label=com.docker.compose.project=$P" \
        --format '{{json .}}' >>"$TMP/extras-exclusion-events.jsonl"
}

extras_exclusion_assert_events() {
    python3 - "$TMP/extras-exclusion-events.jsonl" <<'PYEVENT'
import json
import sys
from pathlib import Path
events = {}
for line in Path(sys.argv[1]).read_text().splitlines():
    event = json.loads(line)
    attrs = event["Actor"]["Attributes"]
    key = (event["timeNano"], event["Actor"]["ID"], event["Action"], attrs.get("name"))
    events[key] = event
created = renamed = invalid = 0
for event in events.values():
    attrs = event["Actor"]["Attributes"]
    created += event["Action"] == "create"
    renamed += event["Action"] == "rename"
    names = [attrs.get("name")]
    if event["Action"] == "rename":
        names.append(attrs.get("oldName"))
    invalid += sum(not name or not name.lstrip("/").startswith("ductorverify") for name in names)
print(f"OBSERVED (extras-exclusion events) create={created} rename={renamed} invalid={invalid}")
if not created or invalid:
    raise SystemExit(1)
PYEVENT
}

extras_exclusion_probe() {
    local name=$1 expected=$2 observed
    if ! observed=$(dc run --rm --no-deps --pull never --name "$P-probe-$name" \
        --entrypoint python3 ductor-bot -c \
        'import importlib.util; print(" ".join(f"{n}={importlib.util.find_spec(n) is not None}" for n in ("nio", "slack_bolt", "slack_sdk")))' \
        2>"$TMP/extras-exclusion-$name-probe.err"); then
        echo "FAIL (extras-exclusion $name) module probe"
        return 1
    fi
    extras_exclusion_capture_events || return 1
    echo "OBSERVED (extras-exclusion $name) $observed"
    if [[ $observed != "$expected" ]]; then
        echo "FAIL (extras-exclusion $name) module set"
        return 1
    fi
}

extras_exclusion_agents() {
    python3 - "$TMP/home/agents.json" "$1" <<'PYAGENTS'
import json
import sys
from pathlib import Path
path, kind = Path(sys.argv[1]), sys.argv[2]
matrix = {
    "homeserver": "https://matrix.invalid",
    "user_id": "@probe:matrix.invalid",
    "password": "fake-password",
    "allowed_rooms": ["!probe:matrix.invalid"],
}
if kind == "matrix":
    agents = [{"name": "mx-probe", "matrix": matrix}]
elif kind == "matrix_without_block":
    agents = [{"name": "mx-probe"}]
elif kind == "declared":
    agents = [{"name": "mx-declared", "transport": "matrix"}]
elif kind == "declared_with_block":
    agents = [{"name": "mx-declared", "transport": "matrix", "matrix": matrix}]
elif kind == "headless":
    agents = [{"name": "headless-probe"}]
elif kind == "headless_with_block":
    agents = [{"name": "headless-probe", "matrix": matrix}]
else:
    raise SystemExit("unknown agent fixture")
path.write_text(json.dumps(agents) + "\n")
PYAGENTS
}

extras_exclusion_logs() {
    local name=$1 marker=$2 i
    for ((i=0; i<20; i++)); do
        dc logs --no-color ductor-bot >"$TMP/extras-exclusion-$name-bot.log" 2>&1
        if grep -Fq "$marker" "$TMP/extras-exclusion-$name-bot.log"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

extras_exclusion_matrix_assert() {
    local log=$1
    grep -Eq 'AgentStack created: name=mx-probe.*transport=matrix' "$log"
}

extras_exclusion_declared_assert() {
    local log=$1
    grep -Fq "Sub-agent 'item #0' excluded:" "$log" &&
        ! grep -Fq 'AgentStack created: name=mx-declared' "$log"
}

extras_exclusion_headless_assert() {
    local log=$1
    grep -Eq 'AgentStack created: name=headless-probe.*transport=headless' "$log" &&
        grep -Fq "Sub-agent 'headless-probe' started" "$log"
}

extras_exclusion_webhook_port_assert() {
    python3 - "$1" <<'PYPORT'
import json
import sys
from pathlib import Path
ports = json.loads(Path(sys.argv[1]).read_text())["services"]["ductor-bot"].get("ports", [])
if len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1" or \
        str(ports[0].get("published")) != "8742" or ports[0].get("target") != 8742:
    raise SystemExit(1)
PYPORT
}

extras_exclusion_checks() {
    local failed=0 port_map
    EXTRAS_EXCLUSION_EVENT_START=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
    : >"$TMP/extras-exclusion-events.jsonl"
    dc build browser >"$TMP/extras-exclusion-browser-build.log" 2>&1

    extras_exclusion_extras matrix
    if [[ ${VERIFY_FAIL_AT-} == matrix_missing ]]; then extras_exclusion_extras ""; fi
    if ! dc build ductor-bot >"$TMP/extras-exclusion-matrix-build.log" 2>&1; then
        echo "FAIL (extras-exclusion matrix) build"; return 1
    fi
    extras_exclusion_probe matrix 'nio=True slack_bolt=False slack_sdk=False' || return 1
    echo "PASS (extras-exclusion matrix)"
    if extras_exclusion_probe matrix 'nio=False slack_bolt=True slack_sdk=False' >"$TMP/extras-exclusion-matrix-injection.log"; then
        echo "FAIL (extras-exclusion matrix injection) wrong module expectation accepted"; failed=1
    else
        echo "OBSERVED (extras-exclusion matrix injection) wrong module expectation exit=1"
    fi

    extras_exclusion_agents matrix
    if ! recreate_bot >"$TMP/extras-exclusion-matrix-up.log" 2>&1; then
        echo "FAIL (extras-exclusion matrix-sub) compose up"; return 1
    fi
    extras_exclusion_logs matrix 'AgentStack created: name=mx-probe' || true
    if ! extras_exclusion_matrix_assert "$TMP/extras-exclusion-matrix-bot.log"; then
        echo "FAIL (extras-exclusion matrix-sub) startup precheck or marker"; failed=1
    else
        echo "PASS (extras-exclusion matrix-sub) named stack transport=matrix"
    fi
    extras_exclusion_agents matrix_without_block
    if ! recreate_bot >"$TMP/extras-exclusion-matrix-injection-up.log" 2>&1; then
        echo "FAIL (extras-exclusion matrix-sub injection) compose up"; return 1
    fi
    extras_exclusion_logs matrix-injection 'AgentStack created: name=mx-probe' || true
    if extras_exclusion_matrix_assert "$TMP/extras-exclusion-matrix-injection-bot.log"; then
        echo "FAIL (extras-exclusion matrix-sub injection) missing block accepted"; failed=1
    else
        echo "OBSERVED (extras-exclusion matrix-sub injection) missing block predicate exit=1"
    fi

    extras_exclusion_agents declared
    if ! recreate_bot >"$TMP/extras-exclusion-declared-up.log" 2>&1; then
        echo "FAIL (extras-exclusion declared) compose up"; return 1
    fi
    extras_exclusion_logs declared "Sub-agent 'item #0' excluded:" || true
    if ! extras_exclusion_declared_assert "$TMP/extras-exclusion-declared-bot.log"; then
        echo "FAIL (extras-exclusion declared) named exclusion or unexpected stack"; failed=1
    else
        echo "PASS (extras-exclusion declared) named exclusion; no stack"
    fi
    extras_exclusion_agents declared_with_block
    if ! recreate_bot >"$TMP/extras-exclusion-declared-injection-up.log" 2>&1; then
        echo "FAIL (extras-exclusion declared injection) compose up"; return 1
    fi
    extras_exclusion_logs declared-injection "Sub-agent 'mx-declared' started" || true
    if extras_exclusion_declared_assert "$TMP/extras-exclusion-declared-injection-bot.log"; then
        echo "FAIL (extras-exclusion declared injection) valid block accepted as excluded"; failed=1
    else
        echo "OBSERVED (extras-exclusion declared injection) valid block predicate exit=1"
    fi

    extras_exclusion_agents headless
    if ! recreate_bot >"$TMP/extras-exclusion-headless-up.log" 2>&1; then
        echo "FAIL (extras-exclusion headless) compose up"; return 1
    fi
    extras_exclusion_logs headless "Sub-agent 'headless-probe' started" || true
    if ! extras_exclusion_headless_assert "$TMP/extras-exclusion-headless-bot.log"; then
        echo "FAIL (extras-exclusion headless) stack or started marker"; failed=1
    else
        echo "PASS (extras-exclusion headless) named stack transport=headless; sub started"
    fi
    extras_exclusion_agents headless_with_block
    if ! recreate_bot >"$TMP/extras-exclusion-headless-injection-up.log" 2>&1; then
        echo "FAIL (extras-exclusion headless injection) compose up"; return 1
    fi
    extras_exclusion_logs headless-injection "Sub-agent 'headless-probe' started" || true
    if extras_exclusion_headless_assert "$TMP/extras-exclusion-headless-injection-bot.log"; then
        echo "FAIL (extras-exclusion headless injection) matrix block accepted as headless"; failed=1
    else
        echo "OBSERVED (extras-exclusion headless injection) matrix block predicate exit=1"
    fi

    extras_exclusion_extras slack
    if ! dc build ductor-bot >"$TMP/extras-exclusion-slack-build.log" 2>&1; then
        echo "FAIL (extras-exclusion slack) build"; return 1
    fi
    extras_exclusion_probe slack 'nio=False slack_bolt=True slack_sdk=True' || return 1
    echo "PASS (extras-exclusion slack)"
    if extras_exclusion_probe slack 'nio=True slack_bolt=False slack_sdk=False' >"$TMP/extras-exclusion-slack-injection.log"; then
        echo "FAIL (extras-exclusion slack injection) wrong module expectation accepted"; failed=1
    else
        echo "OBSERVED (extras-exclusion slack injection) wrong module expectation exit=1"
    fi

    dc config --format json >"$TMP/extras-exclusion-webhook-off-config.json"
    if extras_exclusion_webhook_port_assert "$TMP/extras-exclusion-webhook-off-config.json"; then
        echo "FAIL (extras-exclusion webhook injection) disabled webhook exposed port"; failed=1
    else
        echo "OBSERVED (extras-exclusion webhook injection) disabled port predicate exit=1"
    fi
    (
        cd "$TMP/repo"
        ./scripts/onboard.sh --no-prompt --home "$TMP/home" \
            --telegram-token-file "$TMP/fake-token" --allowed-users 1 \
            --tz UTC --webhook --force
    )
    extras_exclusion_extras ""
    if ! dc build ductor-bot >"$TMP/extras-exclusion-webhook-build.log" 2>&1; then
        echo "FAIL (extras-exclusion webhook) build"; return 1
    fi
    dc config --format json >"$TMP/extras-exclusion-webhook-config.json"
    if ! extras_exclusion_webhook_port_assert "$TMP/extras-exclusion-webhook-config.json"; then
        echo "FAIL (extras-exclusion webhook) config ports"; return 1
    fi
    if ! recreate_bot >"$TMP/extras-exclusion-webhook-up.log" 2>&1; then
        echo "FAIL (extras-exclusion webhook) compose up"; return 1
    fi
    port_map=$(docker port "$P-bot" 8742/tcp)
    if [[ $port_map != '127.0.0.1:8742' ]]; then
        echo "FAIL (extras-exclusion webhook) published container port"
        failed=1
    else
        echo "PASS (extras-exclusion webhook) config and docker port 127.0.0.1:8742:8742"
    fi
    extras_exclusion_capture_events || return 1
    if ! extras_exclusion_assert_events; then
        echo "FAIL (extras-exclusion events) non-isolated container name"; failed=1
    else
        echo "PASS (extras-exclusion events) all create and rename names isolated"
    fi
    return "$failed"
}


live_online_log() {
    local since=$1 log=$2 i
    for ((i=0; i<90; i++)); do
        docker logs --since "$since" "$P-bot" >"$log" 2>&1 || return 1
        if grep -Fq 'Bot online: @' "$log"; then
            [[ $(docker inspect -f '{{.State.Running}}' "$P-bot") == true ]]
            return
        fi
        sleep 1
    done
    return 1
}

live_pid_alive() {
    local pid=$1
    [[ $pid =~ ^[0-9]+$ ]] &&
        [[ $(docker exec "$P-bot" sh -c 'kill -0 "$1" 2>/dev/null && echo ALIVE' sh "$pid") == ALIVE ]]
}

live_restart_assert() {
    local before_started=$1 before_host_pid=$2 command_time=$3
    local after_started=$4 after_host_pid=$5 bot_pid=$6 log=$7 pidfile=$8
    python3 - "$before_started" "$before_host_pid" "$command_time" \
        "$after_started" "$after_host_pid" "$bot_pid" "$log" "$pidfile" <<'PYRESTART'
from datetime import datetime, timezone
from pathlib import Path
import sys

before_started, before_host, command_time, after_started, after_host, bot_pid, log_path, pid_path = sys.argv[1:]
def timestamp(value):
    return datetime.strptime(value[:26], "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc).timestamp()

log = Path(log_path).read_text()
pidfile = Path(pid_path)
checks = {
    "started_after_command": timestamp(after_started) > timestamp(command_time),
    "started_changed": timestamp(after_started) > timestamp(before_started),
    "host_pid_changed": before_host.isdecimal() and after_host.isdecimal()
        and int(after_host) > 0 and after_host != before_host,
    "bot_pid_numeric": bot_pid.isdecimal(),
    "lock_marker": f"PID lock acquired (pid={bot_pid})" in log,
    "no_stale_file": "Stale PID file found" not in log,
    "no_stale_current": "Stale PID file for current process" not in log,
    "pidfile_matches": pidfile.read_text().strip() == bot_pid,
    "pidfile_mtime": pidfile.stat().st_mtime >= timestamp(after_started),
}
if not all(checks.values()):
    print("OBSERVED (restart predicate) " + " ".join(f"{key}={value}" for key, value in checks.items()))
raise SystemExit(0 if all(checks.values()) else 1)
PYRESTART
    local result=$?
    (( result == 0 )) || return 1
    live_pid_alive "$bot_pid"
}

live_seeded_pid_assert() {
    local pid=$1 started=$2 since=$3 log=$4 pidfile=$5
    python3 - "$pid" "$started" "$since" "$log" "$pidfile" <<'PYSEEDED'
from datetime import datetime, timezone
from pathlib import Path
import sys

pid, started, since, log_path, pid_path = sys.argv[1:]
def timestamp(value):
    return datetime.strptime(value[:26], "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc).timestamp()

log = Path(log_path).read_text()
pidfile = Path(pid_path)
checks = {
    "started_after_seed": timestamp(started) >= timestamp(since),
    "pid_numeric": pid.isdecimal(),
    "lock_marker": f"PID lock acquired (pid={pid})" in log,
    "no_stale_file": "Stale PID file found" not in log,
    "no_stale_current": "Stale PID file for current process" not in log,
    "no_duplicate": "Another bot instance is already running" not in log,
    "pidfile_matches": pidfile.read_text().strip() == pid,
    "pidfile_mtime": pidfile.stat().st_mtime >= timestamp(started),
}
if not all(checks.values()):
    print("OBSERVED (restart seeded predicate) " + " ".join(f"{key}={value}" for key, value in checks.items()))
raise SystemExit(0 if all(checks.values()) else 1)
PYSEEDED
    local result=$?
    (( result == 0 )) || return 1
    live_pid_alive "$pid"
}

live_capture_events() {
    local until
    until=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
    docker events --since "$LIVE_EVENT_START" --until "$until" \
        --filter type=container --filter event=create --filter event=rename \
        --filter "label=com.docker.compose.project=$P" \
        --format '{{json .}}' >>"$TMP/live-events.jsonl"
}

live_assert_events() {
    python3 - "$TMP/live-events.jsonl" "${LIVE_EVENT_LABEL:-live-lifecycle}" <<'PYEVENT'
import json
import sys
from pathlib import Path
events = {}
for line in Path(sys.argv[1]).read_text().splitlines():
    event = json.loads(line)
    attrs = event["Actor"]["Attributes"]
    key = (event["timeNano"], event["Actor"]["ID"], event["Action"], attrs.get("name"))
    events[key] = event
created = renamed = invalid = 0
for event in events.values():
    attrs = event["Actor"]["Attributes"]
    created += event["Action"] == "create"
    renamed += event["Action"] == "rename"
    names = [attrs.get("name")]
    if event["Action"] == "rename":
        names.append(attrs.get("oldName"))
    invalid += sum(not name or not name.lstrip("/").startswith("ductorverify") for name in names)
print(f"OBSERVED ({sys.argv[2]} events) create={created} rename={renamed} invalid={invalid}")
if created < 2 or invalid:
    raise SystemExit(1)
PYEVENT
}

live_grace_assert() {
    local elapsed_ms=$1 exit_code=$2 grace=$3
    (( elapsed_ms < grace * 1000 )) && [[ $exit_code != 137 ]]
}

live_lifecycle_checks() {
    local since pid before after dup_rc rc exit_code elapsed_ms max_ms=0 grace expected_grace
    local start_ms end_ms i before_started before_host_pid after_started after_host_pid
    local seed_case seed_pid seeded_pid seeded_started
    LIVE_EVENT_START=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
    : >"$TMP/live-events.jsonl"
    dc build >"$TMP/live-build.log" 2>&1 || { echo "FAIL (online marker) build"; return 1; }
    since=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
    dc up -d ductor-bot >"$TMP/live-up.log" 2>&1 || { echo "FAIL (online marker) compose up"; return 1; }
    live_capture_events || return 1
    if ! live_online_log "$since" "$TMP/live-online.log"; then
        echo "FAIL (online marker) Bot online marker within 90s"; return 1
    fi
    echo "PASS (online marker) Bot online marker within 90s"
    sed '/Bot online: @/d' "$TMP/live-online.log" >"$TMP/live-online-injected.log"
    if grep -Fq 'Bot online: @' "$TMP/live-online-injected.log"; then
        echo "FAIL (online marker injection) missing marker accepted"; return 1
    fi
    echo "OBSERVED (online marker injection) missing marker predicate exit=1"

    pid=$(docker exec "$P-bot" cat /ductor/bot.pid)
    if ! live_pid_alive "$pid"; then echo "FAIL (duplicate start) bot PID not alive"; return 1; fi
    if timeout 15 docker exec "$P-bot" ductor >"$TMP/live-duplicate.log" 2>&1; then dup_rc=0; else dup_rc=$?; fi
    if [[ $dup_rc == 0 ]] ||
       ! grep -Fq 'Another bot instance is already running' "$TMP/live-duplicate.log" ||
       ! live_pid_alive "$pid"; then
        echo "FAIL (duplicate start) duplicate process or original PID"; return 1
    fi
    echo "PASS (duplicate start) numeric PID alive; duplicate rejected; original alive"
    if live_pid_alive 99999999; then
        echo "FAIL (duplicate start injection) nonexistent PID accepted"; return 1
    fi
    echo "OBSERVED (duplicate start injection) nonexistent PID predicate exit=1"

    before=$pid
    before_started=$(docker inspect -f '{{.State.StartedAt}}' "$P-bot")
    before_host_pid=$(docker inspect -f '{{.State.Pid}}' "$P-bot")
    since=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
    dc restart ductor-bot >"$TMP/live-restart.log" 2>&1 || { echo "FAIL (restart lock) compose restart"; return 1; }
    live_capture_events || return 1
    if ! live_online_log "$since" "$TMP/live-restarted.log"; then
        echo "FAIL (restart lock) Bot online after restart within 90s"; return 1
    fi
    after_started=$(docker inspect -f '{{.State.StartedAt}}' "$P-bot")
    after_host_pid=$(docker inspect -f '{{.State.Pid}}' "$P-bot")
    after=$(docker exec "$P-bot" cat /ductor/bot.pid)
    if ! live_restart_assert "$before_started" "$before_host_pid" "$since" \
        "$after_started" "$after_host_pid" "$after" "$TMP/live-restarted.log" "$TMP/home/bot.pid"; then
        echo "FAIL (restart lock) restart timestamp, host PID, lock log, pidfile or liveness"; return 1
    fi
    echo "PASS (restart lock) restarted online; host PID replaced; lock reacquired; pidfile current; no stale marker"

    if live_restart_assert "$before_started" "$before_host_pid" "$since" \
        "$before_started" "$before_host_pid" "$after" "$TMP/live-restarted.log" "$TMP/home/bot.pid"; then
        echo "FAIL (restart injection) old StartedAt and host PID accepted"; return 1
    fi
    echo "OBSERVED (restart injection) old StartedAt and host PID predicate exit=1"
    if live_restart_assert "$before_started" "$before_host_pid" "$since" \
        "$before_started" "$after_host_pid" "$after" "$TMP/live-restarted.log" "$TMP/home/bot.pid"; then
        echo "FAIL (restart injection) old StartedAt accepted"; return 1
    fi
    echo "OBSERVED (restart injection) old StartedAt predicate exit=1"
    if live_restart_assert "$before_started" "$before_host_pid" "$since" \
        "$after_started" "$before_host_pid" "$after" "$TMP/live-restarted.log" "$TMP/home/bot.pid"; then
        echo "FAIL (restart injection) old host PID accepted"; return 1
    fi
    echo "OBSERVED (restart injection) old host PID predicate exit=1"
    sed '/PID lock acquired (pid=/d' "$TMP/live-restarted.log" >"$TMP/live-no-lock.log"
    if live_restart_assert "$before_started" "$before_host_pid" "$since" \
        "$after_started" "$after_host_pid" "$after" "$TMP/live-no-lock.log" "$TMP/home/bot.pid"; then
        echo "FAIL (restart injection) missing lock marker accepted"; return 1
    fi
    echo "OBSERVED (restart injection) missing lock marker predicate exit=1"
    printf '%s\n' 'Stale PID file found' >>"$TMP/live-restarted.log"
    if live_restart_assert "$before_started" "$before_host_pid" "$since" \
        "$after_started" "$after_host_pid" "$after" "$TMP/live-restarted.log" "$TMP/home/bot.pid"; then
        echo "FAIL (restart injection) stale marker accepted"; return 1
    fi
    echo "OBSERVED (restart injection) stale marker predicate exit=1"
    sed -i '$d' "$TMP/live-restarted.log"
    printf '%s\n' 'Stale PID file for current process' >>"$TMP/live-restarted.log"
    if live_restart_assert "$before_started" "$before_host_pid" "$since" \
        "$after_started" "$after_host_pid" "$after" "$TMP/live-restarted.log" "$TMP/home/bot.pid"; then
        echo "FAIL (restart injection) current-process stale marker accepted"; return 1
    fi
    echo "OBSERVED (restart injection) current-process stale marker predicate exit=1"
    sed -i '$d' "$TMP/live-restarted.log"
    cp "$TMP/home/bot.pid" "$TMP/live-old-pid"
    touch -d '2000-01-01 UTC' "$TMP/live-old-pid"
    if live_restart_assert "$before_started" "$before_host_pid" "$since" \
        "$after_started" "$after_host_pid" "$after" "$TMP/live-restarted.log" "$TMP/live-old-pid"; then
        echo "FAIL (restart injection) old pidfile mtime accepted"; return 1
    fi
    echo "OBSERVED (restart injection) old pidfile mtime predicate exit=1"
    printf '%s\n' '99999999' >"$TMP/live-wrong-pid"
    if live_restart_assert "$before_started" "$before_host_pid" "$since" \
        "$after_started" "$after_host_pid" "$after" "$TMP/live-restarted.log" "$TMP/live-wrong-pid"; then
        echo "FAIL (restart injection) mismatched pidfile accepted"; return 1
    fi
    echo "OBSERVED (restart injection) mismatched pidfile predicate exit=1"

    local seed_attempts attempt
    for seed_case in same missing pid1; do
        seed_attempts=1
        [[ $seed_case == same ]] && seed_attempts=3
        for ((attempt=1; attempt<=seed_attempts; attempt++)); do
            if [[ $seed_case == same ]]; then
                # The earlier restart used another path and may leave another PID number, so
                # calibrate with the same stop and start that the seeded run uses.
                dc stop ductor-bot >"$TMP/live-seeded-same-calibrate-stop.log" 2>&1 ||
                    { echo "FAIL (restart seeded same) calibration stop"; return 1; }
                since=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
                dc start ductor-bot >"$TMP/live-seeded-same-calibrate-start.log" 2>&1 ||
                    { echo "FAIL (restart seeded same) calibration start"; return 1; }
                live_capture_events || return 1
                if ! live_online_log "$since" "$TMP/live-seeded-same-calibrate-online.log"; then
                    echo "FAIL (restart seeded same) calibration Bot online marker within 90s"; return 1
                fi
                after=$(docker exec "$P-bot" cat /ductor/bot.pid)
            fi
            dc stop ductor-bot >"$TMP/live-seeded-$seed_case-stop.log" 2>&1 ||
                { echo "FAIL (restart seeded $seed_case) stop"; return 1; }
            case $seed_case in
                same) seed_pid=$after ;;
                missing) seed_pid=99999 ;;
                pid1) seed_pid=1 ;;
            esac
            printf '%s\n' "$seed_pid" >"$TMP/home/bot.pid"
            chmod 600 "$TMP/home/bot.pid"
            touch -d '2000-01-01 UTC' "$TMP/home/bot.pid"
            since=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
            dc start ductor-bot >"$TMP/live-seeded-$seed_case-start.log" 2>&1 ||
                { echo "FAIL (restart seeded $seed_case) start"; return 1; }
            live_capture_events || return 1
            if ! live_online_log "$since" "$TMP/live-seeded-$seed_case-online.log"; then
                echo "FAIL (restart seeded $seed_case) Bot online marker within 90s"; return 1
            fi
            seeded_started=$(docker inspect -f '{{.State.StartedAt}}' "$P-bot")
            seeded_pid=$(docker exec "$P-bot" cat /ductor/bot.pid)
            if [[ $seed_case == same && $seeded_pid != "$seed_pid" ]]; then
                if (( attempt < seed_attempts )); then continue; fi
                echo "SKIP (restart seeded same) premise not met: PID differs across identical stop+start (seeded=$seed_pid got=$seeded_pid)"
                break
            fi
            if ! live_seeded_pid_assert "$seeded_pid" "$seeded_started" "$since" \
                "$TMP/live-seeded-$seed_case-online.log" "$TMP/home/bot.pid"; then
                echo "FAIL (restart seeded $seed_case) lock, PID file, liveness or stale marker"; return 1
            fi
            echo "PASS (restart seeded $seed_case) online; fresh PID lock and file; no stale or duplicate marker"
            break
        done
        after=$seeded_pid
    done

    for ((i=1; i<=3; i++)); do
        start_ms=$(date +%s%3N)
        if { time dc stop -t 300 ductor-bot >"$TMP/live-stop-$i.log" 2>&1; } 2>"$TMP/live-stop-$i-time.log"; then rc=0; else rc=$?; fi
        end_ms=$(date +%s%3N)
        elapsed_ms=$((end_ms - start_ms))
        exit_code=$(docker inspect -f '{{.State.ExitCode}}' "$P-bot")
        live_capture_events || return 1
        echo "OBSERVED (stop timing stop $i) elapsed_ms=$elapsed_ms stop_rc=$rc bot_exit=$exit_code"
        if (( rc != 0 )) || [[ $exit_code == 137 ]]; then
            echo "FAIL (stop timing stop $i)"; return 1
        fi
        if (( elapsed_ms > max_ms )); then max_ms=$elapsed_ms; fi
        since=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
        dc up -d ductor-bot >"$TMP/live-reup-$i.log" 2>&1 || { echo "FAIL (stop timing reup $i)"; return 1; }
        live_capture_events || return 1
        if ! live_online_log "$since" "$TMP/live-reup-$i-online.log"; then
            echo "FAIL (stop timing online $i)"; return 1
        fi
    done
    expected_grace=$(((2 * max_ms + 999) / 1000))
    if (( expected_grace < 30 )); then expected_grace=30; fi
    grace=$(sed -nE 's/^[[:space:]]*stop_grace_period: ([0-9]+)s.*/\1/p' "$TMP/repo/compose.yaml")
    echo "OBSERVED (stop timing) max_ms=$max_ms provisional_grace_s=$expected_grace compose_grace_s=$grace"
    if [[ -z $grace || $grace != "$expected_grace" ]]; then
        echo "FAIL (stop grace) measured grace differs from compose"; return 1
    fi
    start_ms=$(date +%s%3N)
    if { time dc stop ductor-bot >"$TMP/live-k-stop.log" 2>&1; } 2>"$TMP/live-k-time.log"; then rc=0; else rc=$?; fi
    end_ms=$(date +%s%3N)
    elapsed_ms=$((end_ms - start_ms))
    exit_code=$(docker inspect -f '{{.State.ExitCode}}' "$P-bot")
    live_capture_events || return 1
    echo "OBSERVED (stop grace) elapsed_ms=$elapsed_ms stop_rc=$rc bot_exit=$exit_code grace_s=$grace"
    if (( rc != 0 )) || ! live_grace_assert "$elapsed_ms" "$exit_code" "$grace"; then
        echo "FAIL (stop grace) stop beyond grace or SIGKILL"; return 1
    fi
    echo "PASS (stop grace) stop within measured grace; no SIGKILL"
    if live_grace_assert "$((grace * 1000))" "$exit_code" "$grace"; then
        echo "FAIL (stop grace injection) grace-boundary stop accepted"; return 1
    fi
    echo "OBSERVED (stop grace injection) grace-boundary predicate exit=1"
    if ! live_assert_events; then
        echo "FAIL (live-lifecycle events) non-isolated container name"; return 1
    fi
    echo "PASS (live-lifecycle events) all create and rename names isolated"
}

live_webhook_api_config_assert() {
    local config=$1 mode=$2
    python3 - "$config" "$mode" <<'PYPORTS'
import json
import sys
from pathlib import Path
ports = json.loads(Path(sys.argv[1]).read_text())["services"]["ductor-bot"].get("ports", [])
if sys.argv[2] == "disabled":
    ok = not ports
else:
    actual = {(p.get("host_ip"), str(p.get("published")), p.get("target"), p.get("protocol")) for p in ports}
    expected = {("127.0.0.1", str(port), port, "tcp") for port in (8742, 8741)}
    ok = len(ports) == 2 and actual == expected
raise SystemExit(0 if ok else 1)
PYPORTS
}

live_webhook_api_disabled_assert() {
    local config=$1 mapping=$2
    live_webhook_api_config_assert "$config" disabled && [[ ! -s $mapping ]]
}

live_webhook_api_http_assert() {
    local port=$1 code
    if ! code=$(curl -s --connect-timeout 1 --max-time 2 -o /dev/null \
        -w '%{http_code}' "http://127.0.0.1:$port/health"); then
        return 1
    fi
    # HTTP error responses still demonstrate a connection to the listener.
    [[ $code =~ ^[1-5][0-9][0-9]$ ]] || return 1
    echo "OBSERVED (webhook-api HTTP) port=$port path=/health code=$code"
}

live_webhook_api_checks() {
    local original_stat deadline port ready=0
    original_stat=$(stat -c '%a %s %Y' "$LIVE_SRC")
    LIVE_EVENT_LABEL=live-webhook-api
    LIVE_EVENT_START=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
    : >"$TMP/live-events.jsonl"
    # Bind probes are only a preflight conflict check, never a disabled-port predicate.
    python3 - <<'PYBIND'
import socket
for port in (8742, 8741):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            raise SystemExit(f"FAIL (webhook-api preflight) host port {port} occupied") from None
print("PASS (webhook-api preflight) host ports 8742 and 8741 available")
PYBIND
    echo "OBSERVED (webhook-api routes) webhook/server.py:53 GET /health; api/server.py:242 GET /health"
    dc config --format json >"$TMP/webhook-api-enabled-config.json"
    if ! live_webhook_api_config_assert "$TMP/webhook-api-enabled-config.json" enabled; then
        echo "FAIL (webhook-api enabled) compose port configuration"; return 1
    fi
    python3 - "$TMP/webhook-api-enabled-config.json" "$TMP/webhook-api-wrong-port.json" <<'PYINJECT'
import json
import sys
from pathlib import Path
config = json.loads(Path(sys.argv[1]).read_text())
config["services"]["ductor-bot"]["ports"][0]["published"] = "18742"
Path(sys.argv[2]).write_text(json.dumps(config))
PYINJECT
    if live_webhook_api_config_assert "$TMP/webhook-api-wrong-port.json" enabled; then
        echo "FAIL (webhook-api enabled injection) wrong published port accepted"; return 1
    fi
    echo "OBSERVED (webhook-api enabled injection) wrong published port predicate exit=1"
    dc up -d --build >"$TMP/webhook-api-enabled-up.log" 2>&1 ||
        { echo "FAIL (webhook-api enabled) compose up --build"; return 1; }
    live_capture_events || return 1
    docker port "$P-bot" >"$TMP/webhook-api-enabled-port-map"
    deadline=$((SECONDS + 90))
    while (( SECONDS < deadline )); do
        if live_webhook_api_http_assert 8742 >"$TMP/webhook-api-webhook-http.log" && \
           live_webhook_api_http_assert 8741 >"$TMP/webhook-api-api-http.log" && (( SECONDS <= deadline )); then
            ready=1
            break
        fi
        sleep 1
    done
    if (( ! ready )); then
        echo "FAIL (webhook-api enabled) host HTTP connection within 90s"; return 1
    fi
    cat "$TMP/webhook-api-webhook-http.log" "$TMP/webhook-api-api-http.log"
    echo "PASS (webhook-api enabled) host HTTP connections 127.0.0.1:8742/health and 127.0.0.1:8741/health"
    : >"$TMP/webhook-api-empty-port-map"
    if live_webhook_api_disabled_assert "$TMP/webhook-api-enabled-config.json" "$TMP/webhook-api-empty-port-map"; then
        echo "FAIL (webhook-api disabled injection) published compose ports accepted"; return 1
    fi
    echo "OBSERVED (webhook-api disabled injection) published compose ports predicate exit=1"

    docker tag "$P-ductor-bot:latest" "$P-webhook-api-enabled:latest"
    # Stop the polling bot and remove its container before preparing the next configuration.
    dc down -v --remove-orphans >"$TMP/webhook-api-enabled-down.log" 2>&1 || return 1
    if [[ -n $(docker ps -a --filter "label=com.docker.compose.project=$P" --format '{{.ID}}') ]]; then
        echo "FAIL (webhook-api sequential) previous project containers remain"; return 1
    fi
    echo "PASS (webhook-api sequential) enabled project down before disabled startup"
    (
        cd "$TMP/repo"
        ./scripts/onboard.sh --no-prompt --force --home "$TMP/home" \
            --telegram-token-file "$TMP/live-token" --allowed-users 1 --tz UTC \
            --no-webhook --no-api
    )
    printf 'VERIFY_PROVIDER_PROBE=1\n' >>"$TMP/home/.env"
    dc config --format json >"$TMP/webhook-api-disabled-config.json"
    dc up -d --build >"$TMP/webhook-api-disabled-up.log" 2>&1 ||
        { echo "FAIL (webhook-api disabled) compose up --build"; return 1; }
    live_capture_events || return 1
    docker port "$P-bot" >"$TMP/webhook-api-disabled-port-map"
    if ! live_webhook_api_disabled_assert "$TMP/webhook-api-disabled-config.json" "$TMP/webhook-api-disabled-port-map"; then
        echo "FAIL (webhook-api disabled) compose or container ports published"; return 1
    fi
    echo "PASS (webhook-api disabled) compose bot ports absent; docker port output empty"
    if live_webhook_api_disabled_assert "$TMP/webhook-api-disabled-config.json" "$TMP/webhook-api-enabled-port-map"; then
        echo "FAIL (webhook-api disabled injection) published container ports accepted"; return 1
    fi
    echo "OBSERVED (webhook-api disabled injection) published container ports predicate exit=1"
    if live_webhook_api_config_assert "$TMP/webhook-api-disabled-config.json" enabled; then
        echo "FAIL (webhook-api enabled injection) disabled compose ports accepted"; return 1
    fi
    echo "OBSERVED (webhook-api enabled injection) disabled compose ports predicate exit=1"
    for port in 8742 8741; do
        if live_webhook_api_http_assert "$port"; then
            echo "FAIL (webhook-api enabled injection) disabled listener $port connected"; return 1
        fi
        echo "OBSERVED (webhook-api enabled injection) disabled listener $port HTTP predicate exit=1"
    done
    live_capture_events || return 1
    if ! live_assert_events; then
        echo "FAIL (live-webhook-api events) non-isolated container name"; return 1
    fi
    echo "PASS (live-webhook-api events) all create and rename names isolated"
    if [[ $(stat -c '%a %s %Y' "$LIVE_SRC") != "$original_stat" ]]; then
        echo "FAIL (webhook-api token) original stat changed"; return 1
    fi
    echo "PASS (webhook-api token) original stat unchanged: $original_stat"
}

live_sub_agents_assert() {
    local mode=$1 log=$2 agents=$3 snapshot=$4 definitions=$5
    python3 - "$mode" "$log" "$agents" "$snapshot" "$definitions" <<'PYSUBCHECK'
from datetime import datetime
import json
import re
import sys
from pathlib import Path
mode, log_path, agents_path, snapshot_path, definitions_path = sys.argv[1:]
def timestamp(value):
    # Python 3.9 on the host accepts microseconds, while Docker emits nanoseconds.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
lines = []
for line in Path(log_path).read_text().splitlines():
    stamp, _, body = line.partition(" ")
    try:
        lines.append((timestamp(stamp), body))
    except ValueError:
        continue
names = json.loads(Path(agents_path).read_text()).get("agents", [])
definitions = json.loads(Path(definitions_path).read_text())
snapshot = json.loads(Path(snapshot_path).read_text())
# Compare at the same microsecond resolution as the parsed log timestamps.
observed_at = round(snapshot["observed_at"], 6)
headless = [t for t, body in lines if "[delegate-probe]" in body and "Headless agent: cron/heartbeat/webhook disabled" in body]
started = [t for t, body in lines if "Sub-agent 'delegate-probe' started" in body]
restored = [t for t, body in lines if "[matrix-probe]" in body and "Restored Matrix login from config token" in body]
connection_failed = [t for t, body in lines if "[matrix-probe]" in body and "Timed out, sleeping for" in body]
excluded = [t for t, body in lines if "Sub-agent 'item #2' excluded:" in body]
stack_created = any("AgentStack created: name=matrix-invalid " in body for _, body in lines)
headless_def, matrix_def, invalid_def = definitions
checks = {
    "s": bool(headless and started and "delegate-probe" in names)
        and headless_def == {"name": "delegate-probe"},
    "matrix": bool(restored and connection_failed)
        and min(connection_failed) >= min(restored)
        and "transport" not in matrix_def and isinstance(matrix_def.get("matrix"), dict)
        and matrix_def["matrix"].get("homeserver") == "https://matrix.invalid",
    "excluded": bool(excluded) and not stack_created and "matrix-invalid" not in names
        and invalid_def == {"name": "matrix-invalid", "transport": "matrix"},
}
first_processed_at = max(min(connection_failed, default=float("inf")),
                         min(excluded, default=float("inf")))
latest_processed_at = max([*connection_failed, *excluded], default=float("inf"))
online_times = [t for t, body in lines if "Bot online: @" in body and t <= observed_at]
online_at = min(online_times, default=float("inf"))
# Retry timestamps must never move the beginning of the main-failure check.
fatal = any(online_at <= t <= observed_at and re.search(
    r"Main agent crashed \(fatal\)|Agent 'main' exceeded max retries|CRITICAL.*\[main\]", body
) for t, body in lines)
checks["alive"] = bool(connection_failed and excluded and online_times) and not fatal \
    and snapshot.get("running") is True and snapshot.get("alive") == "ALIVE" \
    and str(snapshot.get("pid", "")).isdecimal() and int(snapshot["pid"]) > 0 \
    and observed_at > latest_processed_at
ok = all(checks.values()) if mode == "all" else checks[mode]
if ok and mode == "all":
    print("OBSERVED (headless sub) delegate-probe headless marker and started marker; registered in GET /interagent/agents")
    print("OBSERVED (matrix probe) matrix-probe: login_or_restore config-token restoration; expected connection-failure retry")
    print("OBSERVED (matrix excluded) item #2 excluded; matrix-invalid stack absent and API registration absent")
    print(f"OBSERVED (matrix main alive) online_at={online_at:.6f} first_processed_at={first_processed_at:.6f} latest_processed_at={latest_processed_at:.6f} observed_at={observed_at:.6f} running=true pid={snapshot['pid']} ALIVE; no main fatal marker from online through observation")
if not ok and mode == "all":
    print("OBSERVED (sub-agents predicate) " + " ".join(f"{key}={value}" for key, value in checks.items()))
    for _, body in lines:
        if any(marker in body for marker in ("Headless agent: cron/heartbeat/webhook disabled",
            "Restored Matrix login from config token", "Sub-agent '", "Timed out, sleeping for")):
            print("OBSERVED (sub-agents marker) " + body)
raise SystemExit(0 if ok else 1)
PYSUBCHECK
}

LIVE_SECRET_PICKED=VERIFY_STORE_PICKED
LIVE_SECRET_OTHER=VERIFY_STORE_OTHER
LIVE_SECRET_SHA=

live_secrets_seed() {
    local home=$TMP/home store value
    store=$home/secrets.env
    # Onboarding created the store; synthetic entries go in before the bot starts.
    if [[ ! -f $store || $(stat -c '%a' "$store") != 600 ]] || grep -Eqv '^(#|$)' "$store"; then
        echo "FAIL (secrets store) onboarding did not leave a private secrets.env without keys"; return 1
    fi
    echo "PASS (secrets store) onboarding created secrets.env with mode 600 and no keys"
    value="synthetic-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    printf '%s=%s\n%s=%s\n' "$LIVE_SECRET_PICKED" "$value" "$LIVE_SECRET_OTHER" "$value-other" >>"$store"
    LIVE_SECRET_SHA=$(printf '%s' "$value" | sha256sum | cut -d' ' -f1)
}

live_secrets_assert() {
    python3 - "$1" "$LIVE_SECRET_PICKED" "$LIVE_SECRET_OTHER" <<'PYSECRETSCHECK'
import json
import sys
from pathlib import Path
report = json.loads(Path(sys.argv[1]).read_text())
picked, other = sys.argv[2:4]
main_home, sub_home = "/ductor", "/ductor/agents/delegate-probe"
cases = {"main-interactive": main_home, "sub-interactive": sub_home,
         "main-oneshot": main_home, "sub-oneshot": sub_home}
ok = report.get("sub_home_store") is False
for name, home in cases.items():
    run = report.get(name) or {}
    good = run.get("path") == main_home + "/secrets.env" and run.get("home") == home \
        and run.get("before") == [] and run.get("loaded") == [picked] \
        and run.get("after") == [picked] and run.get("value_ok") is True
    ok = ok and good
    if ok:
        print(f"OBSERVED (secrets {name}) DUCTOR_SECRETS_FILE={run['path']} DUCTOR_HOME={run['home']}; "
              "no store key before the pick; load_secrets gave the picked key only; other key unset")
raise SystemExit(0 if ok else 1)
PYSECRETSCHECK
}

live_secrets_probe() {
    docker exec -i -e VERIFY_PICKED="$LIVE_SECRET_PICKED" -e VERIFY_OTHER="$LIVE_SECRET_OTHER" \
        -e VERIFY_SHA="$LIVE_SECRET_SHA" "$P-bot" python - <<'PYSECRETSPROBE' >"$TMP/secrets-report.json"
import asyncio
import json
import os
import stat
import sys
from pathlib import Path

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.executor import SubprocessSpec, run_oneshot_subprocess
from ductor_bot.cli.param_resolver import TaskExecutionConfig
from ductor_bot.cli.types import CLIResponse
from ductor_bot.infra.task_runner import TaskRunOptions, run_oneshot_task

CHILD = '''\
import hashlib, json, os, sys
picked, other = os.environ["VERIFY_PICKED"], os.environ["VERIFY_OTHER"]
report = {"path": os.environ.get("DUCTOR_SECRETS_FILE"), "home": os.environ.get("DUCTOR_HOME"),
          "before": [n for n in (picked, other) if n in os.environ]}
sys.path.insert(0, sys.argv[1])
from _secrets import load_secrets
report["loaded"] = sorted(load_secrets(picked))
report["after"] = [n for n in (picked, other) if n in os.environ]
digest = hashlib.sha256(os.environ.get(picked, "").encode()).hexdigest()
report["value_ok"] = digest == os.environ["VERIFY_SHA"]
print(json.dumps(report))
'''
BIN = Path("/tmp/verify-secrets-bin")
BIN.mkdir(exist_ok=True)
# A stand-in provider CLI: it runs the same probe with whatever environment it was given.
(BIN / "claude").write_text('#!/bin/sh\nexec "%s" -c "$VERIFY_CHILD" "$VERIFY_TOOLS"\n' % sys.executable)
(BIN / "claude").chmod(0o755)
os.environ["VERIFY_CHILD"] = CHILD
os.environ["PATH"] = f"{BIN}:{os.environ['PATH']}"
MAIN = Path("/ductor")
SUB = MAIN / "agents" / "delegate-probe"


def parse(stdout: bytes, stderr: bytes, returncode: int | None) -> CLIResponse:
    return CLIResponse(result=stdout.decode(), stderr=stderr.decode()[:300], returncode=returncode)


async def interactive(name: str, home: Path) -> dict:
    os.environ["VERIFY_TOOLS"] = str(home / "workspace" / "tools")
    config = CLIConfig(ductor_home=home, working_dir=home, agent_name=name)
    spec = SubprocessSpec(exec_cmd=[sys.executable, "-c", CHILD, os.environ["VERIFY_TOOLS"]],
                          use_cwd=str(home), prompt="", timeout_seconds=60)
    return json.loads((await run_oneshot_subprocess(config, spec, parse)).result)


async def oneshot(home: Path) -> dict:
    os.environ["VERIFY_TOOLS"] = str(home / "workspace" / "tools")
    execution = TaskExecutionConfig(provider="claude", model="verify", reasoning_effort="",
                                    cli_parameters=[], permission_mode="bypassPermissions",
                                    working_dir=str(home), file_access="all")
    options = TaskRunOptions(cwd=home, timeout_seconds=60, timeout_label="verify", ductor_home=home)
    result = await run_oneshot_task(execution, "", options)
    return json.loads(result.execution.stdout)


async def main() -> dict:
    return {
        "main-interactive": await interactive("main", MAIN),
        "sub-interactive": await interactive("delegate-probe", SUB),
        "main-oneshot": await oneshot(MAIN),
        "sub-oneshot": await oneshot(SUB),
        "sub_home_store": (SUB / "secrets.env").exists(),
    }


print(json.dumps(asyncio.run(main())))
PYSECRETSPROBE
}

live_secrets_checks() {
    local mutation
    docker exec "$P-bot" sh -c 'tr "\0" "\n" </proc/$(cat /ductor/bot.pid)/environ | sed "s/=.*//"' \
        >"$TMP/secrets-bot-env-names.txt" || { echo "FAIL (secrets not injected) bot environment unreadable"; return 1; }
    if ! grep -Fxq VERIFY_PROVIDER_PROBE "$TMP/secrets-bot-env-names.txt" ||
       grep -Eq '^VERIFY_STORE_' "$TMP/secrets-bot-env-names.txt"; then
        echo "FAIL (secrets not injected) store keys in the bot environment, or the .env control missing"; return 1
    fi
    echo "PASS (secrets not injected) bot environment holds the .env control key and neither store key"
    live_secrets_probe || { echo "FAIL (secrets framework env) probe in the bot container"; return 1; }
    if ! live_secrets_assert "$TMP/secrets-report.json" >"$TMP/secrets-assert.log"; then
        echo "FAIL (secrets framework env) CLI environment or tool pick differs"; return 1
    fi
    cat "$TMP/secrets-assert.log"
    echo "PASS (secrets framework env) interactive and one-shot CLI environments for main and sub-agent runs carry no store key; DUCTOR_SECRETS_FILE names the main store"
    echo "PASS (secrets tool pick) load_secrets returned only the picked key in all four runs; the sub-agent home has no store"
    python3 - "$TMP" "$LIVE_SECRET_PICKED" "$LIVE_SECRET_OTHER" <<'PYSECRETSINJECT'
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
picked, other = sys.argv[2:4]
report = json.loads((root / "secrets-report.json").read_text())
def write(name, edit):
    data = json.loads(json.dumps(report))
    edit(data)
    (root / f"secrets-{name}.json").write_text(json.dumps(data))
write("no-path", lambda d: d["sub-oneshot"].update(path=None))
write("wrong-path", lambda d: d["main-interactive"].update(path="/ductor/agents/delegate-probe/secrets.env"))
write("injected", lambda d: d["sub-interactive"].update(before=[picked]))
write("both-loaded", lambda d: d["main-oneshot"].update(after=[picked, other]))
write("sub-store", lambda d: d.update(sub_home_store=True))
PYSECRETSINJECT
    for mutation in no-path wrong-path injected both-loaded sub-store; do
        if live_secrets_assert "$TMP/secrets-$mutation.json" >/dev/null; then
            echo "FAIL (secrets injection) $mutation accepted"; return 1
        fi
        echo "OBSERVED (secrets injection) $mutation predicate exit=1"
    done
}

live_sub_agents_checks() {
    local original_stat deadline ready=0 pid
    original_stat=$(stat -c '%a %s %Y' "$LIVE_SRC")
    LIVE_EVENT_LABEL=live-sub-agents
    LIVE_EVENT_START=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
    : >"$TMP/live-events.jsonl"
    extras_exclusion_extras matrix
    python3 - "$TMP/home/agents.json" <<'PYSUBAGENTS'
import json
import sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps([
    {"name": "delegate-probe"},
    {"name": "matrix-probe", "matrix": {
        "homeserver": "https://matrix.invalid", "user_id": "@probe:matrix.invalid",
        "access_token": "SYNTHETIC_MATRIX_VERIFY_TOKEN", "device_id": "SYNTHETIC_DEVICE",
    }},
    {"name": "matrix-invalid", "transport": "matrix"},
]))
PYSUBAGENTS
    echo "OBSERVED (matrix source) bot.py:246 calls login_or_restore; credentials.py:73 logs config-token restoration"
    live_secrets_seed || return 1
    dc up -d --build >"$TMP/sub-agents-up.log" 2>&1 ||
        { echo "FAIL (sub-agents) compose up --build"; return 1; }
    live_capture_events || return 1
    deadline=$((SECONDS + 90))
    while (( SECONDS < deadline )); do
        docker logs --timestamps "$P-bot" >"$TMP/sub-agents-bot.log" 2>&1 || return 1
        if docker exec "$P-bot" curl -s --max-time 2 --fail \
            http://127.0.0.1:8799/interagent/agents >"$TMP/sub-agents-agents.json"; then
            pid=$(docker exec "$P-bot" cat /ductor/bot.pid) || return 1
            docker inspect -f '{{.State.Running}}' "$P-bot" >"$TMP/sub-agents-running"
            docker exec "$P-bot" sh -c 'kill -0 "$1" 2>/dev/null && echo ALIVE' sh "$pid" >"$TMP/sub-agents-alive" || return 1
            python3 - "$TMP" "$pid" <<'PYSUBSNAP'
import json
import sys
import time
from pathlib import Path
root = Path(sys.argv[1])
(root / "sub-agents-snapshot.json").write_text(json.dumps({
    "running": (root / "sub-agents-running").read_text().strip() == "true",
    "alive": (root / "sub-agents-alive").read_text().strip(), "pid": sys.argv[2],
    "observed_at": time.time(),
}))
PYSUBSNAP
            # Inspect logs through the liveness observation, including any later main failure.
            docker logs --timestamps "$P-bot" >"$TMP/sub-agents-bot.log" 2>&1 || return 1
            if live_sub_agents_assert all "$TMP/sub-agents-bot.log" "$TMP/sub-agents-agents.json" \
                "$TMP/sub-agents-snapshot.json" "$TMP/home/agents.json" >"$TMP/sub-agents-results.log" \
                && (( SECONDS <= deadline )); then
                ready=1
                break
            fi
        fi
        sleep 1
    done
    if (( ! ready )); then
        if [[ -f $TMP/sub-agents-results.log ]]; then cat "$TMP/sub-agents-results.log"; fi
        echo "FAIL (sub-agents) required markers, registration, exclusion or main liveness within 90s"; return 1
    fi
    cat "$TMP/sub-agents-results.log"
    echo "PASS (headless sub) credential-free headless sub-agent started and API-registered within 90s"
    echo "PASS (matrix login) automatic Matrix login stage and expected connection failure; item #2 excluded without stack; main alive afterwards"
    docker exec "$P-bot" python -c \
        'import inspect, nio.client.async_client; lines=inspect.getsourcelines(nio.client.async_client.AsyncClient._send); print("OBSERVED (matrix retry source) nio/client/async_client.py:" + str(next(i for i,l in enumerate(lines[0],lines[1]) if "Timed out, sleeping for" in l)) + " ClientConnectionError/TimeoutError retry")'
    python3 - "$TMP" <<'PYSUBINJECT'
from datetime import datetime, timezone
import json
import re
import sys
from pathlib import Path
root = Path(sys.argv[1])
log = (root / "sub-agents-bot.log").read_text()
(root / "sub-agents-no-headless.log").write_text("\n".join(l for l in log.splitlines() if "Headless agent: cron/heartbeat/webhook disabled" not in l))
(root / "sub-agents-no-login.log").write_text("\n".join(l for l in log.splitlines() if "Restored Matrix login from config token" not in l))
(root / "sub-agents-no-connection-failure.log").write_text("\n".join(l for l in log.splitlines() if "Timed out, sleeping for" not in l))
(root / "sub-agents-wrong-index.log").write_text(log.replace("Sub-agent 'item #2' excluded:", "Sub-agent 'item #1' excluded:"))
names = json.loads((root / "sub-agents-agents.json").read_text())
names["agents"].remove("delegate-probe")
(root / "sub-agents-missing-agent.json").write_text(json.dumps(names))
definitions = json.loads((root / "home/agents.json").read_text())
definitions[1].pop("matrix")
(root / "sub-agents-no-matrix.json").write_text(json.dumps(definitions))
definitions = json.loads((root / "home/agents.json").read_text())
definitions[2]["matrix"] = definitions[1]["matrix"]
(root / "sub-agents-valid-matrix.json").write_text(json.dumps(definitions))
snapshot = json.loads((root / "sub-agents-snapshot.json").read_text())
processing = [datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1",
    l.split(" ", 1)[0]).replace("Z", "+00:00")).timestamp()
    for l in log.splitlines() if "Sub-agent 'item #2' excluded:" in l
    or ("[matrix-probe]" in l and "Timed out, sleeping for" in l)]
snapshot["observed_at"] = max(processing) - 0.000001
(root / "sub-agents-too-early.json").write_text(json.dumps(snapshot))
snapshot = json.loads((root / "sub-agents-snapshot.json").read_text())
snapshot["running"] = False
(root / "sub-agents-stopped.json").write_text(json.dumps(snapshot))
snapshot = json.loads((root / "sub-agents-snapshot.json").read_text())
stamp = datetime.fromtimestamp(snapshot["observed_at"], timezone.utc).isoformat()
(root / "sub-agents-fatal.log").write_text(log + "\n" + stamp + " Main agent crashed (fatal), terminating supervisor: SYNTHETIC_FAILURE\n")
# Reproduce the temporal layouts independently of the live clock.
base = datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp()
def timeline_line(seconds, body):
    stamp = datetime.fromtimestamp(base + seconds, timezone.utc).isoformat()
    return stamp + " " + body
healthy_lines = [
    timeline_line(0, "INFO [main] Bot online: @SYNTHETIC_BOT"),
    timeline_line(1, "ERROR Sub-agent 'item #2' excluded: SYNTHETIC_REASON"),
    timeline_line(2, "INFO [matrix-probe] Restored Matrix login from config token"),
    timeline_line(3, "WARNING [matrix-probe] Timed out, sleeping for 2s"),
    timeline_line(4, "INFO [delegate-probe] Headless agent: cron/heartbeat/webhook disabled"),
    timeline_line(4.5, "INFO Sub-agent 'delegate-probe' started"),
    timeline_line(8, "WARNING [matrix-probe] Timed out, sleeping for 4s"),
]
(root / "sub-agents-timeline-healthy.log").write_text("\n".join(healthy_lines) + "\n")
snapshot = json.loads((root / "sub-agents-snapshot.json").read_text())
snapshot["observed_at"] = base + 10
(root / "sub-agents-timeline-snapshot.json").write_text(json.dumps(snapshot))
for name, at in (("before-processing", 0.5), ("before-matrix", 2.5),
                 ("between-retries", 5), ("before-observation", 9)):
    records = [*healthy_lines, timeline_line(at,
        "ERROR [main] Main agent crashed (fatal), terminating supervisor: SYNTHETIC_FAILURE")]
    records.sort()
    (root / f"sub-agents-timeline-{name}.log").write_text("\n".join(records) + "\n")
PYSUBINJECT
    local mode mutation log agents snapshot definitions
    for mutation in no-headless missing-agent no-login no-connection-failure no-matrix wrong-index valid-matrix too-early stopped fatal; do
        log="$TMP/sub-agents-bot.log"; agents="$TMP/sub-agents-agents.json"
        snapshot="$TMP/sub-agents-snapshot.json"; definitions="$TMP/home/agents.json"
        case $mutation in
            no-headless) mode=s; log="$TMP/sub-agents-no-headless.log" ;;
            missing-agent) mode=s; agents="$TMP/sub-agents-missing-agent.json" ;;
            no-login|no-connection-failure) mode=matrix; log="$TMP/sub-agents-$mutation.log" ;;
            no-matrix) mode=matrix; definitions="$TMP/sub-agents-no-matrix.json" ;;
            wrong-index) mode=excluded; log="$TMP/sub-agents-wrong-index.log" ;;
            valid-matrix) mode=excluded; definitions="$TMP/sub-agents-valid-matrix.json" ;;
            too-early|stopped) mode=alive; snapshot="$TMP/sub-agents-$mutation.json" ;;
            fatal) mode=alive; log="$TMP/sub-agents-fatal.log" ;;
        esac
        if live_sub_agents_assert "$mode" "$log" "$agents" "$snapshot" "$definitions"; then
            echo "FAIL ($mode injection) $mutation accepted"; return 1
        fi
        echo "OBSERVED ($mode injection) $mutation predicate exit=1"
    done
    if ! live_sub_agents_assert all "$TMP/sub-agents-timeline-healthy.log" "$TMP/sub-agents-agents.json" \
        "$TMP/sub-agents-timeline-snapshot.json" "$TMP/home/agents.json" >"$TMP/sub-agents-timeline-healthy.assert"; then
        echo "FAIL (alive timeline control) healthy synthetic layout rejected"; return 1
    fi
    echo "OBSERVED (alive timeline control) healthy synthetic layout predicate exit=0"
    for mutation in before-processing before-matrix between-retries before-observation; do
        if live_sub_agents_assert all "$TMP/sub-agents-timeline-$mutation.log" "$TMP/sub-agents-agents.json" \
            "$TMP/sub-agents-timeline-snapshot.json" "$TMP/home/agents.json" >"$TMP/sub-agents-timeline-$mutation.assert"; then
            echo "FAIL (alive timeline injection) $mutation accepted"; return 1
        fi
        echo "OBSERVED (alive timeline injection) $mutation predicate exit=1"
    done
    live_capture_events || return 1
    if ! live_assert_events; then
        echo "FAIL (live-sub-agents events) non-isolated container name"; return 1
    fi
    echo "PASS (live-sub-agents events) all create and rename names isolated"
    live_secrets_checks || return 1
    if [[ $(stat -c '%a %s %Y' "$LIVE_SRC") != "$original_stat" ]]; then
        echo "FAIL (sub-agents token) original stat changed"; return 1
    fi
    echo "PASS (sub-agents token) original stat unchanged: $original_stat"
}

unset DUCTOR_HOME DUCTOR_EXTRAS DUCTOR_WEBHOOK_BIND DUCTOR_API_BIND BROWSER_TOKEN TZ
while IFS= read -r variable; do
    unset "$variable"
done < <(env | sed -n 's/^\(COMPOSE_[A-Z_]*\)=.*/\1/p')

set +e
(
    set -euo pipefail
    prepare
    assert_config
    if [[ $STAGE == live-lifecycle ]]; then
        live_lifecycle_checks
    elif [[ $STAGE == live-sub-agents ]]; then
        live_sub_agents_checks
    elif [[ $STAGE == live-webhook-api ]]; then
        live_webhook_api_checks
    elif [[ $STAGE == build-extras ]]; then
        dc build browser
        build_extras_checks
    elif [[ $STAGE == extras-exclusion ]]; then
        extras_exclusion_checks
    else
        dc build
        if [[ $STAGE == offline ]]; then
            offline_checks
        else
            echo "PASS config stage"
        fi
    fi
) 2>&1 | tee "$TMP/verify.log"
run_rc=${PIPESTATUS[0]}
set -e
exit "$run_rc"

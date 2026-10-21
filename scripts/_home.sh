# Shared helpers of the host scripts. Source this file; it only defines functions.

# Language of the messages. The order is the same for every host script: an explicit value
# (onboard.sh --language), ONBOARD_LANGUAGE, the language saved in the host home's
# config/config.json, the system locale (C and POSIX mean English), then English. A value
# that is not a bundled language is skipped. Python may be missing, so this is plain bash.
supported_language() {
    case "$1" in de | en | es | fr | id | ko | nl | pt | ru) return 0 ;; *) return 1 ;; esac
}

saved_language() {
    local home="${1:-${DUCTOR_HOME-}}" file content
    home="${home:-${HOME-}/.ductor}"
    home="${home/#\~/${HOME-}}"
    file="$home/config/config.json"
    # Like onboard.py, never follow a symbolic link to read a configuration.
    if [[ -f $file && ! -L $file ]] && content="$(<"$file")" 2>/dev/null; then
        local pattern='"language"[[:space:]]*:[[:space:]]*"([^"]*)"'
        if [[ $content =~ $pattern ]]; then
            printf '%s' "${BASH_REMATCH[1]}"
        fi
    fi
}

locale_language() {
    local value="" key
    for key in LC_ALL LC_MESSAGES LANG; do
        if [[ -n ${!key-} ]]; then
            value="${!key}"
            break
        fi
    done
    value="${value%%.*}"
    value="${value%%@*}"
    value="${value//-/_}"
    value="${value%%_*}"
    printf '%s' "${value,,}"
}

# host_language [explicit-language] [home]: the language code to use.
host_language() {
    local candidate
    for candidate in "${1-}" "${ONBOARD_LANGUAGE-}" "$(saved_language "${2-}")" \
        "$(locale_language)"; do
        if supported_language "$candidate"; then
            printf '%s' "$candidate"
            return
        fi
    done
    printf 'en'
}

# fill TEMPLATE [VALUE...]: put VALUE n in place of {n}. One pass, so a value is never rescanned.
fill() {
    local rest="$1" out="" before marker number
    local -a values=("${@:2}")
    while [[ $rest == *"{"[1-9]"}"* ]]; do
        before="${rest%%"{"[1-9]"}"*}"
        marker="${rest:${#before}:3}"
        number="${marker:1:1}"
        out+="$before${values[number - 1]-}"
        rest="${rest:$((${#before} + 3))}"
    done
    printf '%s' "$out$rest"
}

# host_template TABLE KEY: the message KEY of TABLE in HOST_LANGUAGE (English if unset).
host_template() {
    "${1}_text_${HOST_LANGUAGE:-en}" "$2"
}

# host_text TABLE KEY [VALUE...]: the message with its values filled in.
host_text() {
    local table="$1" key="$2"
    shift 2
    fill "$(host_template "$table" "$key")" "$@"
}

# Resolve only the host home key; never execute the repository .env.
resolve_ductor_home() {
    local resolved status=0
    resolved="$(python3 - "$1/.env" <<'PY'
import json
import os
from pathlib import Path
import shlex
import sys

home = os.environ.get("DUCTOR_HOME", "")
if not home:
    path = Path(sys.argv[1])
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        for line in lines:
            key, separator, value = line.strip().partition("=")
            if separator and key.strip() == "DUCTOR_HOME":
                value = value.strip()
                if value.startswith('"'):
                    # Onboarding writes JSON-quoted values, including escaped Unicode.
                    try:
                        home = json.loads(value)
                    except json.JSONDecodeError:
                        parts = shlex.split(value, comments=True)
                        home = parts[0] if len(parts) == 1 else None
                elif value.startswith("'"):
                    parts = shlex.split(value, comments=True)
                    home = parts[0] if len(parts) == 1 else None
                else:
                    home = value.split(" #", 1)[0].rstrip()
                if not isinstance(home, str) or any(c in home for c in "\r\n\x00"):
                    raise ValueError("invalid home")
    except (OSError, UnicodeError, ValueError):
        sys.exit(2)
print(os.path.expanduser(home or str(Path.home() / ".ductor")))
PY
    )" || status=$?
    if ((status == 2)); then
        # The reason is a product message; the language comes from the environment only here.
        HOST_LANGUAGE="${HOST_LANGUAGE:-$(host_language)}" host_text home home_unreadable >&2
        printf '\n' >&2
        return 2
    elif ((status != 0)); then
        return "$status"
    fi
    printf '%s\n' "$resolved"
}

home_text_en() {
    case "$1" in
        home_unreadable) printf '%s' $'Cannot read DUCTOR_HOME from repository .env; check its format' ;;
    esac
}

home_text_de() {
    case "$1" in
        home_unreadable) printf '%s' $'DUCTOR_HOME lässt sich nicht aus der .env des Repositorys lesen; prüfe das Format' ;;
    esac
}

home_text_es() {
    case "$1" in
        home_unreadable) printf '%s' $'No se puede leer DUCTOR_HOME del .env del repositorio; revisa su formato' ;;
    esac
}

home_text_fr() {
    case "$1" in
        home_unreadable) printf '%s' $'Impossible de lire DUCTOR_HOME dans le .env du dépôt ; vérifie son format' ;;
    esac
}

home_text_id() {
    case "$1" in
        home_unreadable) printf '%s' $'Tidak dapat membaca DUCTOR_HOME dari .env repositori; periksa formatnya' ;;
    esac
}

home_text_nl() {
    case "$1" in
        home_unreadable) printf '%s' $'DUCTOR_HOME kan niet uit de .env van de repository worden gelezen; controleer het formaat' ;;
    esac
}

home_text_pt() {
    case "$1" in
        home_unreadable) printf '%s' $'Não foi possível ler DUCTOR_HOME do .env do repositório; verifique o formato' ;;
    esac
}

home_text_ru() {
    case "$1" in
        home_unreadable) printf '%s' $'Не удаётся прочитать DUCTOR_HOME из .env репозитория; проверь его формат' ;;
    esac
}

# hangul-allowed-begin: Korean messages of _home.sh

home_text_ko() {
    case "$1" in
        home_unreadable) printf '%s' $'저장소 .env에서 DUCTOR_HOME을 읽을 수 없어요. 형식을 확인하세요' ;;
    esac
}

# hangul-allowed-end

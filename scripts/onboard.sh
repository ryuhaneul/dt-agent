#!/usr/bin/env bash
set -euo pipefail

# Language of the preflight messages. Python may be missing at this point, so the language is
# decided in bash (scripts/_home.sh), in the same order as scripts/onboard.py: --language,
# ONBOARD_LANGUAGE, the language saved in DUCTOR_HOME/config/config.json, the system locale
# (C and POSIX mean English), then English. A value that is not a bundled language is skipped.
# Only shell builtins are used before the checks: the PATH may hold nothing else.
script_path="${BASH_SOURCE[0]}"
SCRIPT_DIR="."
[[ $script_path == */* ]] && SCRIPT_DIR="${script_path%/*}"
SCRIPT_DIR="$(cd "$SCRIPT_DIR" && pwd)"
source "$SCRIPT_DIR/_home.sh"

decide_language() {
    local option_language="" option_home="" index args=("$@")
    for ((index = 0; index < ${#args[@]}; index++)); do
        case "${args[index]}" in
            --language) option_language="${args[index + 1]-}" ;;
            --language=*) option_language="${args[index]#--language=}" ;;
            --home) option_home="${args[index + 1]-}" ;;
            --home=*) option_home="${args[index]#--home=}" ;;
        esac
    done
    host_language "$option_language" "$option_home"
}

# Preflight problems: python3, docker, compose, uid, python_version.
preflight_text() {
    local language="$1" problem="$2"
    if [[ $language == ko ]]; then
        preflight_text_ko "$problem"
        return
    fi
    case "$language:$problem" in
        de:python3) echo "python3 wird benötigt; installiere Python 3, bevor du das Onboarding startest" ;;
        de:docker) echo "docker wird benötigt; installiere die Docker Engine, bevor du das Onboarding startest" ;;
        de:compose) echo "docker compose v2 oder neuer wird benötigt" ;;
        de:uid) echo "Onboarding als root wird nicht unterstützt. Der Image-Benutzer erhält die UID/GID des Onboarding-Kontos." ;;
        de:python_version) echo "Python 3.9 oder neuer wird benötigt. Aktuelle Version: $PREFLIGHT_VALUE" ;;
        es:python3) echo "se necesita python3; instala Python 3 antes de la incorporación" ;;
        es:docker) echo "se necesita docker; instala Docker Engine antes de la incorporación" ;;
        es:compose) echo "se necesita docker compose v2 o posterior" ;;
        es:uid) echo "No se admite la incorporación como root. El usuario de la imagen usa el UID/GID de la cuenta de incorporación." ;;
        es:python_version) echo "Se necesita Python 3.9 o posterior. Versión actual: $PREFLIGHT_VALUE" ;;
        fr:python3) echo "python3 est requis ; installe Python 3 avant l'onboarding" ;;
        fr:docker) echo "docker est requis ; installe Docker Engine avant l'onboarding" ;;
        fr:compose) echo "docker compose v2 ou plus récent est requis" ;;
        fr:uid) echo "L’onboarding en root n’est pas pris en charge. L’utilisateur de l’image utilise les UID/GID du compte d’onboarding." ;;
        fr:python_version) echo "Python 3.9 ou plus récent est requis. Version actuelle : $PREFLIGHT_VALUE" ;;
        id:python3) echo "python3 diperlukan; instal Python 3 sebelum onboarding" ;;
        id:docker) echo "docker diperlukan; instal Docker Engine sebelum onboarding" ;;
        id:compose) echo "docker compose v2 atau yang lebih baru diperlukan" ;;
        id:uid) echo "Onboarding sebagai root tidak didukung. Pengguna image memakai UID/GID akun onboarding." ;;
        id:python_version) echo "Python 3.9 atau yang lebih baru diperlukan. Versi saat ini: $PREFLIGHT_VALUE" ;;
        nl:python3) echo "python3 is vereist; installeer Python 3 vóór de onboarding" ;;
        nl:docker) echo "docker is vereist; installeer Docker Engine vóór de onboarding" ;;
        nl:compose) echo "docker compose v2 of nieuwer is vereist" ;;
        nl:uid) echo "Onboarding als root wordt niet ondersteund. De imagegebruiker krijgt de UID/GID van het onboardingaccount." ;;
        nl:python_version) echo "Python 3.9 of nieuwer is vereist. Huidige versie: $PREFLIGHT_VALUE" ;;
        pt:python3) echo "python3 é necessário; instale o Python 3 antes do onboarding" ;;
        pt:docker) echo "docker é necessário; instale o Docker Engine antes do onboarding" ;;
        pt:compose) echo "é necessário docker compose v2 ou mais recente" ;;
        pt:uid) echo "A integração como root não é suportada. O usuário da imagem usa o UID/GID da conta de integração." ;;
        pt:python_version) echo "É necessário Python 3.9 ou mais recente. Versão atual: $PREFLIGHT_VALUE" ;;
        ru:python3) echo "нужен python3; установи Python 3 перед онбордингом" ;;
        ru:docker) echo "нужен docker; установи Docker Engine перед онбордингом" ;;
        ru:compose) echo "нужен docker compose v2 или новее" ;;
        ru:uid) echo "Онбординг от root не поддерживается. Пользователь образа получает UID/GID учётной записи онбординга." ;;
        ru:python_version) echo "Нужен Python 3.9 или новее. Текущая версия: $PREFLIGHT_VALUE" ;;
        *:python3) echo "python3 is required; install Python 3 before onboarding" ;;
        *:docker) echo "docker is required; install Docker Engine before onboarding" ;;
        *:compose) echo "docker compose v2 or newer is required" ;;
        *:uid) echo "Onboarding as root is not supported. The image user is built with the onboarding account UID/GID." ;;
        *:python_version) echo "Python 3.9 or newer is required. Current version: $PREFLIGHT_VALUE" ;;
    esac
}

# hangul-allowed-begin: Korean preflight messages of onboard.sh
preflight_text_ko() {
    case "$1" in
        python3) echo "python3가 필요해요. 온보딩 전에 Python 3를 설치하세요" ;;
        docker) echo "docker가 필요해요. 온보딩 전에 Docker Engine을 설치하세요" ;;
        compose) echo "docker compose v2 이상이 필요해요" ;;
        uid) echo "root로 온보딩할 수 없어요. 이미지 사용자는 온보딩 계정의 UID/GID로 빌드돼요." ;;
        python_version) echo "Python 3.9 이상이 필요해요. 현재 버전: $PREFLIGHT_VALUE" ;;
    esac
}
# hangul-allowed-end

preflight_fail() {
    echo "onboard: $(preflight_text "$(decide_language "$@")" "$PREFLIGHT_PROBLEM")" >&2
    exit 1
}

if ! command -v python3 >/dev/null 2>&1; then
    PREFLIGHT_PROBLEM=python3 preflight_fail "$@"
fi
for option in "$@"; do
    case "$option" in
        -h|--help) exec python3 "$SCRIPT_DIR/onboard.py" "$@" ;;
    esac
done
if ! command -v docker >/dev/null 2>&1; then
    PREFLIGHT_PROBLEM=docker preflight_fail "$@"
fi
compose_version="$(docker compose version --short 2>/dev/null || true)"
compose_major="${compose_version#v}"
compose_major="${compose_major%%.*}"
if [[ ! "$compose_major" =~ ^[0-9]+$ ]] || ((compose_major < 2)); then
    PREFLIGHT_PROBLEM=compose preflight_fail "$@"
fi

current_uid="$(id -u)"
if [[ $current_uid == 0 ]]; then
    PREFLIGHT_PROBLEM=uid PREFLIGHT_VALUE="$current_uid" preflight_fail "$@"
fi
if ! current_python_version="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3]))); sys.exit(0 if sys.version_info >= (3, 9) else 1)')"; then
    PREFLIGHT_PROBLEM=python_version PREFLIGHT_VALUE="$current_python_version" preflight_fail "$@"
fi

exec python3 "$SCRIPT_DIR/onboard.py" "$@"

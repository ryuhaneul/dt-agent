#!/usr/bin/env bash
# Check CLI auth status inside the ductor-bot container.

set -euo pipefail

CONTAINER="${DUCTOR_CONTAINER:-ductor-bot}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_home.sh"

# Messages for a person are in the language of scripts/_home.sh (stored setting, locale, else
# English). Commands, options, paths, container names and the output of docker and git stay as
# they are.
# The host home is only read for its saved language; an unreadable repository .env is not an error here.
HOST_LANGUAGE="$(host_language "" "$(resolve_ductor_home "$(dirname "$SCRIPT_DIR")" 2>/dev/null || true)")"
say() { printf '%s\n' "$(host_text status "$@")"; }
say_err() { say "$@" >&2; }

status_text_en() {
    case "$1" in
        err_container) printf '%s' $'Error: container \'{1}\' not found. Run \'docker compose up -d\' first.' ;;
        h_container) printf '%s' $'=== Container Status ===' ;;
        h_auth) printf '%s' $'=== CLI Auth ===' ;;
        state_auth) printf '%s' $'authenticated' ;;
        state_not) printf '%s' $'not authenticated' ;;
        state_auth_key) printf '%s' $'authenticated (API key)' ;;
    esac
}

status_text_de() {
    case "$1" in
        err_container) printf '%s' $'Fehler: Container \'{1}\' nicht gefunden. Starte zuerst \'docker compose up -d\'.' ;;
        h_container) printf '%s' $'=== Container-Status ===' ;;
        h_auth) printf '%s' $'=== CLI-Anmeldung ===' ;;
        state_auth) printf '%s' $'angemeldet' ;;
        state_not) printf '%s' $'nicht angemeldet' ;;
        state_auth_key) printf '%s' $'angemeldet (API-Schlüssel)' ;;
    esac
}

status_text_es() {
    case "$1" in
        err_container) printf '%s' $'Error: no se encontró el contenedor \'{1}\'. Ejecuta primero \'docker compose up -d\'.' ;;
        h_container) printf '%s' $'=== Estado del contenedor ===' ;;
        h_auth) printf '%s' $'=== Autenticación de las CLI ===' ;;
        state_auth) printf '%s' $'autenticado' ;;
        state_not) printf '%s' $'no autenticado' ;;
        state_auth_key) printf '%s' $'autenticado (clave de API)' ;;
    esac
}

status_text_fr() {
    case "$1" in
        err_container) printf '%s' $'Erreur : conteneur \'{1}\' introuvable. Lance d\'abord \'docker compose up -d\'.' ;;
        h_container) printf '%s' $'=== État du conteneur ===' ;;
        h_auth) printf '%s' $'=== Connexion des CLI ===' ;;
        state_auth) printf '%s' $'connecté' ;;
        state_not) printf '%s' $'non connecté' ;;
        state_auth_key) printf '%s' $'connecté (clé API)' ;;
    esac
}

status_text_id() {
    case "$1" in
        err_container) printf '%s' $'Kesalahan: kontainer \'{1}\' tidak ditemukan. Jalankan \'docker compose up -d\' terlebih dahulu.' ;;
        h_container) printf '%s' $'=== Status kontainer ===' ;;
        h_auth) printf '%s' $'=== Autentikasi CLI ===' ;;
        state_auth) printf '%s' $'terautentikasi' ;;
        state_not) printf '%s' $'belum terautentikasi' ;;
        state_auth_key) printf '%s' $'terautentikasi (kunci API)' ;;
    esac
}

status_text_nl() {
    case "$1" in
        err_container) printf '%s' $'Fout: container \'{1}\' niet gevonden. Voer eerst \'docker compose up -d\' uit.' ;;
        h_container) printf '%s' $'=== Containerstatus ===' ;;
        h_auth) printf '%s' $'=== CLI-aanmelding ===' ;;
        state_auth) printf '%s' $'aangemeld' ;;
        state_not) printf '%s' $'niet aangemeld' ;;
        state_auth_key) printf '%s' $'aangemeld (API-sleutel)' ;;
    esac
}

status_text_pt() {
    case "$1" in
        err_container) printf '%s' $'Erro: contêiner \'{1}\' não encontrado. Execute \'docker compose up -d\' primeiro.' ;;
        h_container) printf '%s' $'=== Estado do contêiner ===' ;;
        h_auth) printf '%s' $'=== Autenticação das CLIs ===' ;;
        state_auth) printf '%s' $'autenticado' ;;
        state_not) printf '%s' $'não autenticado' ;;
        state_auth_key) printf '%s' $'autenticado (chave de API)' ;;
    esac
}

status_text_ru() {
    case "$1" in
        err_container) printf '%s' $'Ошибка: контейнер \'{1}\' не найден. Сначала выполни \'docker compose up -d\'.' ;;
        h_container) printf '%s' $'=== Состояние контейнера ===' ;;
        h_auth) printf '%s' $'=== Вход в CLI ===' ;;
        state_auth) printf '%s' $'выполнен вход' ;;
        state_not) printf '%s' $'вход не выполнен' ;;
        state_auth_key) printf '%s' $'выполнен вход (ключ API)' ;;
    esac
}

# hangul-allowed-begin: Korean messages of status.sh

status_text_ko() {
    case "$1" in
        err_container) printf '%s' $'오류: \'{1}\' 컨테이너를 찾을 수 없어요. 먼저 \'docker compose up -d\'를 실행하세요.' ;;
        h_container) printf '%s' $'=== 컨테이너 상태 ===' ;;
        h_auth) printf '%s' $'=== CLI 인증 ===' ;;
        state_auth) printf '%s' $'인증됨' ;;
        state_not) printf '%s' $'인증 안 됨' ;;
        state_auth_key) printf '%s' $'인증됨 (API 키)' ;;
    esac
}

# hangul-allowed-end

# The container prints English words for the probes; they are shown in the chosen language.
probe_state() {
    case "$1" in
        "authenticated") say state_auth ;;
        "not authenticated") say state_not ;;
        "authenticated (API key)") say state_auth_key ;;
        *) printf '%s\n' "$1" ;;
    esac
}

if ! docker inspect "$CONTAINER" &>/dev/null; then
    say err_container "$CONTAINER"
    exit 1
fi

say h_container
cd "$(dirname "$SCRIPT_DIR")"
docker compose ps 2>/dev/null || docker ps --filter "name=$CONTAINER" --format "table {{.Names}}\t{{.Status}}"

echo ""
say h_auth
while IFS= read -r slot; do
    printf 'Claude (%s): ' "$slot"
    docker exec -e "CLAUDE_CONFIG_DIR=/ductor/.cli-auth/$slot" "$CONTAINER" claude auth status 2>/dev/null && echo "" || say state_not
done < <(docker exec "$CONTAINER" python3 -c '
import json
try:
    with open("/ductor/config/config.json") as file:
        accounts = json.load(file).get("claude_accounts") or ["claude"]
except FileNotFoundError:
    accounts = ["claude"]
for slot in accounts:
    print(slot)
')
echo -n "Codex:  "
probe_state "$(docker exec "$CONTAINER" sh -c '[ -f /ductor/.cli-auth/codex/auth.json ] && echo "authenticated" || echo "not authenticated"')"
echo -n "Gemini: "
probe_state "$(docker exec "$CONTAINER" sh -c 'if [ -n "${GEMINI_API_KEY:-}${GOOGLE_API_KEY:-}" ]; then
    echo "authenticated (API key)"
elif [ -f /ductor/.cli-auth/gemini/oauth_creds.json ]; then
    echo "authenticated"
else
    echo "not authenticated"
fi')"
echo -n "Grok:   "
probe_state "$(docker exec "$CONTAINER" sh -c '[ -f /ductor/.cli-auth/grok/auth.json ] && echo "authenticated" || echo "not authenticated"')"

#!/usr/bin/env bash
# Rebuild ductor containers without pulling new source.
#
# Use when:
#   - Dockerfile / compose.yaml changed
#   - You want to pick up new CLI tool versions (claude-code, codex, gemini-cli, grok)
#   - Dependencies in pyproject.toml changed
#
# Does NOT pull code — run scripts/update.sh for that.

set -euo pipefail

cd "$(dirname "$0")/.."

source "./scripts/_home.sh"
# Messages for a person are in the language of scripts/_home.sh (stored setting, locale, else
# English). Commands, options, paths, container names and the output of docker and git stay as
# they are.
# The host home is only read for its saved language; an unreadable repository .env is not an error here.
HOST_LANGUAGE="$(host_language "" "$(resolve_ductor_home "$PWD" 2>/dev/null || true)")"
say() { printf '%s\n' "$(host_text rebuild "$@")"; }
say_err() { say "$@" >&2; }

rebuild_text_en() {
    case "$1" in
        h_building_images) printf '%s' $'=== Building images ===' ;;
        h_recreating) printf '%s' $'=== Recreating containers ===' ;;
        h_status) printf '%s' $'=== Status ===' ;;
    esac
}

rebuild_text_de() {
    case "$1" in
        h_building_images) printf '%s' $'=== Images werden gebaut ===' ;;
        h_recreating) printf '%s' $'=== Container werden neu erstellt ===' ;;
        h_status) printf '%s' $'=== Status ===' ;;
    esac
}

rebuild_text_es() {
    case "$1" in
        h_building_images) printf '%s' $'=== Construyendo imágenes ===' ;;
        h_recreating) printf '%s' $'=== Recreando contenedores ===' ;;
        h_status) printf '%s' $'=== Estado ===' ;;
    esac
}

rebuild_text_fr() {
    case "$1" in
        h_building_images) printf '%s' $'=== Construction des images ===' ;;
        h_recreating) printf '%s' $'=== Recréation des conteneurs ===' ;;
        h_status) printf '%s' $'=== État ===' ;;
    esac
}

rebuild_text_id() {
    case "$1" in
        h_building_images) printf '%s' $'=== Membangun image ===' ;;
        h_recreating) printf '%s' $'=== Membuat ulang kontainer ===' ;;
        h_status) printf '%s' $'=== Status ===' ;;
    esac
}

rebuild_text_nl() {
    case "$1" in
        h_building_images) printf '%s' $'=== Images bouwen ===' ;;
        h_recreating) printf '%s' $'=== Containers opnieuw aanmaken ===' ;;
        h_status) printf '%s' $'=== Status ===' ;;
    esac
}

rebuild_text_pt() {
    case "$1" in
        h_building_images) printf '%s' $'=== Construindo imagens ===' ;;
        h_recreating) printf '%s' $'=== Recriando contêineres ===' ;;
        h_status) printf '%s' $'=== Estado ===' ;;
    esac
}

rebuild_text_ru() {
    case "$1" in
        h_building_images) printf '%s' $'=== Сборка образов ===' ;;
        h_recreating) printf '%s' $'=== Пересоздание контейнеров ===' ;;
        h_status) printf '%s' $'=== Состояние ===' ;;
    esac
}

# hangul-allowed-begin: Korean messages of rebuild.sh

rebuild_text_ko() {
    case "$1" in
        h_building_images) printf '%s' $'=== 이미지 빌드 중 ===' ;;
        h_recreating) printf '%s' $'=== 컨테이너 재생성 중 ===' ;;
        h_status) printf '%s' $'=== 상태 ===' ;;
    esac
}

# hangul-allowed-end

say h_building_images
docker compose build --pull --build-arg CACHEBUST="$(date +%s)"

echo ""
say h_recreating
docker compose up -d

echo ""
say h_status
docker compose ps

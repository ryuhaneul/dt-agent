#!/usr/bin/env bash
# Update ductor: git pull + rebuild + restart.
#
# Safety notes:
#   - Running container keeps using the baked image until swap — git pull
#     alone is safe during operation.
#   - `docker compose build` builds a new image without touching the running
#     container (no downtime during build).
#   - `docker compose up -d` swaps the container (seconds of downtime).
#     In-flight background tasks are killed; Telegram messages queue up on
#     Telegram's side and are redelivered after startup.
#   - Session state (sessions.json, tasks.json, cron_jobs.json, .cli-auth/)
#     is persisted via the /ductor volume, so nothing is lost.
#
# This script warns if there are in-flight tasks and asks for confirmation
# before the container swap.

set -euo pipefail

cd "$(dirname "$0")/.."

CONTAINER="ductor-bot"
source "./scripts/_home.sh"
DUCTOR_HOME="$(resolve_ductor_home "$PWD")"

# Messages for a person are in the language of scripts/_home.sh (stored setting, locale, else
# English). Commands, options, paths, container names and the output of docker and git stay as
# they are.
HOST_LANGUAGE="$(host_language "" "$DUCTOR_HOME")"
say() { printf '%s\n' "$(host_text update "$@")"; }
say_err() { say "$@" >&2; }

update_text_en() {
    case "$1" in
        err_local_changes) printf '%s' $'Error: local changes detected. Commit or stash before updating.' ;;
        h_current) printf '%s' $'=== Current HEAD ===' ;;
        h_pulling) printf '%s' $'=== Pulling upstream ===' ;;
        up_to_date) printf '%s' $'Already up to date.' ;;
        prompt_rebuild) printf '%s' $'Rebuild anyway? [y/N] ' ;;
        new_commits) printf '%s' $'New commits:' ;;
        warn_inflight) printf '%s' $'WARN: {1} in-flight turn(s) will be interrupted.' ;;
        warn_tasks) printf '%s' $'WARN: {1} background task(s) will be killed (can be resumed after restart).' ;;
        prompt_proceed) printf '%s' $'Proceed with rebuild and restart? [y/N] ' ;;
        aborted) printf '%s' $'Aborted. Code pulled but container not rebuilt.' ;;
        prompt_swap) printf '%s' $'Swap now? Running work will be interrupted. [y/N] ' ;;
        aborted_built) printf '%s' $'The new image is built and the running container is unchanged. Run docker compose up -d later to swap.' ;;
        h_building) printf '%s' $'=== Building new image ===' ;;
        h_swapping) printf '%s' $'=== Swapping container ===' ;;
        h_status) printf '%s' $'=== Status ===' ;;
    esac
}

update_text_de() {
    case "$1" in
        err_local_changes) printf '%s' $'Fehler: Lokale Änderungen gefunden. Committe oder stashe sie vor dem Update.' ;;
        h_current) printf '%s' $'=== Aktueller HEAD ===' ;;
        h_pulling) printf '%s' $'=== Änderungen werden geholt ===' ;;
        up_to_date) printf '%s' $'Bereits auf dem neuesten Stand.' ;;
        prompt_rebuild) printf '%s' $'Trotzdem neu bauen? [y/N] ' ;;
        new_commits) printf '%s' $'Neue Commits:' ;;
        warn_inflight) printf '%s' $'WARNUNG: {1} laufende Antwort(en) werden unterbrochen.' ;;
        warn_tasks) printf '%s' $'WARNUNG: {1} Hintergrundaufgabe(n) werden beendet (nach dem Neustart fortsetzbar).' ;;
        prompt_proceed) printf '%s' $'Neu bauen und neu starten? [y/N] ' ;;
        aborted) printf '%s' $'Abgebrochen. Code geholt, aber Container nicht neu gebaut.' ;;
        prompt_swap) printf '%s' $'Jetzt austauschen? Laufende Arbeit wird unterbrochen. [y/N] ' ;;
        aborted_built) printf '%s' $'Das neue Image ist gebaut und der laufende Container bleibt unverändert. Tausche ihn später mit docker compose up -d aus.' ;;
        h_building) printf '%s' $'=== Neues Image wird gebaut ===' ;;
        h_swapping) printf '%s' $'=== Container wird ausgetauscht ===' ;;
        h_status) printf '%s' $'=== Status ===' ;;
    esac
}

update_text_es() {
    case "$1" in
        err_local_changes) printf '%s' $'Error: hay cambios locales. Haz commit o stash antes de actualizar.' ;;
        h_current) printf '%s' $'=== HEAD actual ===' ;;
        h_pulling) printf '%s' $'=== Descargando cambios ===' ;;
        up_to_date) printf '%s' $'Ya está actualizado.' ;;
        prompt_rebuild) printf '%s' $'¿Reconstruir de todos modos? [y/N] ' ;;
        new_commits) printf '%s' $'Commits nuevos:' ;;
        warn_inflight) printf '%s' $'AVISO: {1} turno(s) en curso se interrumpirán.' ;;
        warn_tasks) printf '%s' $'AVISO: {1} tarea(s) en segundo plano se terminarán (se pueden reanudar tras el reinicio).' ;;
        prompt_proceed) printf '%s' $'¿Continuar con la reconstrucción y el reinicio? [y/N] ' ;;
        aborted) printf '%s' $'Cancelado. Código descargado, pero el contenedor no se reconstruyó.' ;;
        prompt_swap) printf '%s' $'¿Sustituir ahora? El trabajo en curso se interrumpirá. [y/N] ' ;;
        aborted_built) printf '%s' $'La imagen nueva está construida y el contenedor en ejecución sigue igual. Ejecuta docker compose up -d más tarde para sustituirlo.' ;;
        h_building) printf '%s' $'=== Construyendo la imagen nueva ===' ;;
        h_swapping) printf '%s' $'=== Sustituyendo el contenedor ===' ;;
        h_status) printf '%s' $'=== Estado ===' ;;
    esac
}

update_text_fr() {
    case "$1" in
        err_local_changes) printf '%s' $'Erreur : modifications locales détectées. Valide-les ou mets-les de côté avant la mise à jour.' ;;
        h_current) printf '%s' $'=== HEAD actuel ===' ;;
        h_pulling) printf '%s' $'=== Récupération des changements ===' ;;
        up_to_date) printf '%s' $'Déjà à jour.' ;;
        prompt_rebuild) printf '%s' $'Reconstruire quand même ? [y/N] ' ;;
        new_commits) printf '%s' $'Nouveaux commits :' ;;
        warn_inflight) printf '%s' $'ATTENTION : {1} tour(s) en cours seront interrompus.' ;;
        warn_tasks) printf '%s' $'ATTENTION : {1} tâche(s) de fond seront arrêtées (reprise possible après le redémarrage).' ;;
        prompt_proceed) printf '%s' $'Reconstruire et redémarrer ? [y/N] ' ;;
        aborted) printf '%s' $'Annulé. Code récupéré mais conteneur non reconstruit.' ;;
        prompt_swap) printf '%s' $'Remplacer maintenant ? Le travail en cours sera interrompu. [y/N] ' ;;
        aborted_built) printf '%s' $'La nouvelle image est construite et le conteneur en cours reste inchangé. Lance docker compose up -d plus tard pour le remplacer.' ;;
        h_building) printf '%s' $'=== Construction de la nouvelle image ===' ;;
        h_swapping) printf '%s' $'=== Remplacement du conteneur ===' ;;
        h_status) printf '%s' $'=== État ===' ;;
    esac
}

update_text_id() {
    case "$1" in
        err_local_changes) printf '%s' $'Kesalahan: ada perubahan lokal. Commit atau stash sebelum memperbarui.' ;;
        h_current) printf '%s' $'=== HEAD saat ini ===' ;;
        h_pulling) printf '%s' $'=== Mengambil pembaruan ===' ;;
        up_to_date) printf '%s' $'Sudah yang terbaru.' ;;
        prompt_rebuild) printf '%s' $'Tetap bangun ulang? [y/N] ' ;;
        new_commits) printf '%s' $'Commit baru:' ;;
        warn_inflight) printf '%s' $'PERINGATAN: {1} giliran yang sedang berjalan akan terputus.' ;;
        warn_tasks) printf '%s' $'PERINGATAN: {1} tugas latar belakang akan dihentikan (dapat dilanjutkan setelah restart).' ;;
        prompt_proceed) printf '%s' $'Lanjutkan bangun ulang dan restart? [y/N] ' ;;
        aborted) printf '%s' $'Dibatalkan. Kode sudah diambil, tetapi kontainer belum dibangun ulang.' ;;
        prompt_swap) printf '%s' $'Ganti sekarang? Pekerjaan yang sedang berjalan akan terputus. [y/N] ' ;;
        aborted_built) printf '%s' $'Image baru sudah dibangun dan kontainer yang berjalan tetap seperti semula. Jalankan docker compose up -d nanti untuk menggantinya.' ;;
        h_building) printf '%s' $'=== Membangun image baru ===' ;;
        h_swapping) printf '%s' $'=== Mengganti kontainer ===' ;;
        h_status) printf '%s' $'=== Status ===' ;;
    esac
}

update_text_nl() {
    case "$1" in
        err_local_changes) printf '%s' $'Fout: lokale wijzigingen gevonden. Commit of stash ze vóór de update.' ;;
        h_current) printf '%s' $'=== Huidige HEAD ===' ;;
        h_pulling) printf '%s' $'=== Wijzigingen ophalen ===' ;;
        up_to_date) printf '%s' $'Al up-to-date.' ;;
        prompt_rebuild) printf '%s' $'Toch opnieuw bouwen? [y/N] ' ;;
        new_commits) printf '%s' $'Nieuwe commits:' ;;
        warn_inflight) printf '%s' $'WAARSCHUWING: {1} lopende beurt(en) worden onderbroken.' ;;
        warn_tasks) printf '%s' $'WAARSCHUWING: {1} achtergrondtaak/-taken worden beëindigd (hervatbaar na de herstart).' ;;
        prompt_proceed) printf '%s' $'Doorgaan met opnieuw bouwen en herstarten? [y/N] ' ;;
        aborted) printf '%s' $'Afgebroken. Code opgehaald, maar container niet opnieuw gebouwd.' ;;
        prompt_swap) printf '%s' $'Nu vervangen? Lopend werk wordt onderbroken. [y/N] ' ;;
        aborted_built) printf '%s' $'Het nieuwe image is gebouwd en de draaiende container blijft ongewijzigd. Voer later docker compose up -d uit om hem te vervangen.' ;;
        h_building) printf '%s' $'=== Nieuw image bouwen ===' ;;
        h_swapping) printf '%s' $'=== Container vervangen ===' ;;
        h_status) printf '%s' $'=== Status ===' ;;
    esac
}

update_text_pt() {
    case "$1" in
        err_local_changes) printf '%s' $'Erro: há alterações locais. Faça commit ou stash antes de atualizar.' ;;
        h_current) printf '%s' $'=== HEAD atual ===' ;;
        h_pulling) printf '%s' $'=== Baixando alterações ===' ;;
        up_to_date) printf '%s' $'Já está atualizado.' ;;
        prompt_rebuild) printf '%s' $'Reconstruir mesmo assim? [y/N] ' ;;
        new_commits) printf '%s' $'Novos commits:' ;;
        warn_inflight) printf '%s' $'AVISO: {1} turno(s) em andamento serão interrompidos.' ;;
        warn_tasks) printf '%s' $'AVISO: {1} tarefa(s) em segundo plano serão encerradas (podem ser retomadas após o reinício).' ;;
        prompt_proceed) printf '%s' $'Continuar com a reconstrução e o reinício? [y/N] ' ;;
        aborted) printf '%s' $'Cancelado. Código baixado, mas o contêiner não foi reconstruído.' ;;
        prompt_swap) printf '%s' $'Trocar agora? O trabalho em andamento será interrompido. [y/N] ' ;;
        aborted_built) printf '%s' $'A nova imagem está construída e o contêiner em execução permanece igual. Execute docker compose up -d mais tarde para trocá-lo.' ;;
        h_building) printf '%s' $'=== Construindo a nova imagem ===' ;;
        h_swapping) printf '%s' $'=== Trocando o contêiner ===' ;;
        h_status) printf '%s' $'=== Estado ===' ;;
    esac
}

update_text_ru() {
    case "$1" in
        err_local_changes) printf '%s' $'Ошибка: есть локальные изменения. Закоммить или спрячь их перед обновлением.' ;;
        h_current) printf '%s' $'=== Текущий HEAD ===' ;;
        h_pulling) printf '%s' $'=== Получение изменений ===' ;;
        up_to_date) printf '%s' $'Уже актуально.' ;;
        prompt_rebuild) printf '%s' $'Всё равно пересобрать? [y/N] ' ;;
        new_commits) printf '%s' $'Новые коммиты:' ;;
        warn_inflight) printf '%s' $'ВНИМАНИЕ: активных ходов будет прервано: {1}.' ;;
        warn_tasks) printf '%s' $'ВНИМАНИЕ: фоновых задач будет остановлено: {1} (после перезапуска их можно возобновить).' ;;
        prompt_proceed) printf '%s' $'Продолжить пересборку и перезапуск? [y/N] ' ;;
        aborted) printf '%s' $'Отменено. Код получен, но контейнер не пересобран.' ;;
        prompt_swap) printf '%s' $'Заменить сейчас? Текущая работа будет прервана. [y/N] ' ;;
        aborted_built) printf '%s' $'Новый образ собран, а работающий контейнер остаётся прежним. Позже выполни docker compose up -d для замены.' ;;
        h_building) printf '%s' $'=== Сборка нового образа ===' ;;
        h_swapping) printf '%s' $'=== Замена контейнера ===' ;;
        h_status) printf '%s' $'=== Состояние ===' ;;
    esac
}

# hangul-allowed-begin: Korean messages of update.sh

update_text_ko() {
    case "$1" in
        err_local_changes) printf '%s' $'오류: 로컬 변경 사항이 있어요. 업데이트 전에 커밋하거나 stash 하세요.' ;;
        h_current) printf '%s' $'=== 현재 HEAD ===' ;;
        h_pulling) printf '%s' $'=== 업스트림에서 가져오는 중 ===' ;;
        up_to_date) printf '%s' $'이미 최신이에요.' ;;
        prompt_rebuild) printf '%s' $'그래도 다시 빌드할까요? [y/N] ' ;;
        new_commits) printf '%s' $'새 커밋:' ;;
        warn_inflight) printf '%s' $'경고: 진행 중인 턴 {1}개가 중단돼요.' ;;
        warn_tasks) printf '%s' $'경고: 백그라운드 작업 {1}개가 종료돼요 (재시작 후 이어서 실행할 수 있어요).' ;;
        prompt_proceed) printf '%s' $'다시 빌드하고 재시작할까요? [y/N] ' ;;
        aborted) printf '%s' $'중단했어요. 코드는 가져왔지만 컨테이너는 다시 빌드하지 않았어요.' ;;
        prompt_swap) printf '%s' $'지금 교체할까요? 진행 중인 작업이 끊겨요 [y/N] ' ;;
        aborted_built) printf '%s' $'새 이미지는 빌드됐고 실행 중인 컨테이너는 그대로예요. 나중에 docker compose up -d 로 교체하세요.' ;;
        h_building) printf '%s' $'=== 새 이미지 빌드 중 ===' ;;
        h_swapping) printf '%s' $'=== 컨테이너 교체 중 ===' ;;
        h_status) printf '%s' $'=== 상태 ===' ;;
    esac
}

# hangul-allowed-end


count_inflight_turns() {
    python3 - "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as file:
        data = json.load(file)
    turns = data.get("turns", data) if isinstance(data, dict) else data
    print(len(turns) if isinstance(turns, (list, dict)) else 0)
except Exception:
    print(0)
PY
}

count_running_tasks() {
    python3 - "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as file:
        data = json.load(file)
    tasks = data.get('tasks', data) if isinstance(data, dict) else data
    running = [t for t in tasks if isinstance(t, dict) and t.get('status') == 'running']
    print(len(running))
except Exception:
    print(0)
PY
}

# 1. Check clean working tree
if ! git diff --quiet || ! git diff --cached --quiet; then
    say err_local_changes
    git status --short
    exit 1
fi

say h_current
git log -1 --oneline

echo ""
say h_pulling
git fetch origin
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse @{u})

if [ "$LOCAL" = "$REMOTE" ]; then
    say up_to_date
    read -rp "$(host_text update prompt_rebuild)" yn
    [[ "$yn" =~ ^[Yy]$ ]] || exit 0
else
    say new_commits
    git log --oneline "$LOCAL..$REMOTE"
    echo ""
    git pull --ff-only
fi

# 2. Warn about in-flight tasks
if docker inspect "$CONTAINER" &>/dev/null; then
    INFLIGHT_FILE="$DUCTOR_HOME/inflight_turns.json"
    TASKS_FILE="$DUCTOR_HOME/tasks.json"

    if [ -f "$INFLIGHT_FILE" ]; then
        INFLIGHT_COUNT=$(count_inflight_turns "$INFLIGHT_FILE")
        if [ "$INFLIGHT_COUNT" -gt 0 ]; then
            echo ""
            say warn_inflight "$INFLIGHT_COUNT"
        fi
    fi

    if [ -f "$TASKS_FILE" ]; then
        RUNNING_TASKS=$(count_running_tasks "$TASKS_FILE")
        if [ "$RUNNING_TASKS" -gt 0 ]; then
            say warn_tasks "$RUNNING_TASKS"
        fi
    fi

    echo ""
    read -rp "$(host_text update prompt_proceed)" yn
    [[ "$yn" =~ ^[Yy]$ ]] || { say aborted; exit 0; }
fi

# 3. Build + swap
echo ""
say h_building
docker compose build --pull --build-arg CACHEBUST="$(date +%s)"

if docker inspect "$CONTAINER" &>/dev/null; then
    INFLIGHT_COUNT=$(count_inflight_turns "$DUCTOR_HOME/inflight_turns.json")
    RUNNING_TASKS=$(count_running_tasks "$DUCTOR_HOME/tasks.json")
    if [ "$INFLIGHT_COUNT" -gt 0 ] || [ "$RUNNING_TASKS" -gt 0 ]; then
        echo ""
        if [ "$INFLIGHT_COUNT" -gt 0 ]; then
            say warn_inflight "$INFLIGHT_COUNT"
        fi
        if [ "$RUNNING_TASKS" -gt 0 ]; then
            say warn_tasks "$RUNNING_TASKS"
        fi
        read -rp "$(host_text update prompt_swap)" yn
        [[ "$yn" =~ ^[Yy]$ ]] || { say aborted_built; exit 0; }
    fi
fi

echo ""
say h_swapping
docker compose up -d

echo ""
say h_status
docker compose ps

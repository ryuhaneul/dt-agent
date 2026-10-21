#!/usr/bin/env bash
# Login to CLI providers inside the ductor-bot container.
#
# Usage:
#   ./scripts/login.sh                        # login to all available providers
#   ./scripts/login.sh claude                 # login to Claude Code default slot
#   ./scripts/login.sh claude <name>          # login to extra Claude slot (e.g. alt, work)
#   ./scripts/login.sh claude --list          # show configured Claude slots + auth state
#   ./scripts/login.sh claude --repair        # re-share settings/skills/plugins from default slot to every alt slot
#   ./scripts/login.sh codex                  # login to Codex only
#   ./scripts/login.sh gemini                 # login to Gemini only
#   ./scripts/login.sh grok                   # login to Grok only
#   ./scripts/login.sh -h | --help            # show this help
#
# Multi-account Claude (backup subscriptions):
#
#   1. ./scripts/login.sh claude              # first Max/Pro account → default slot
#   2. ./scripts/login.sh claude alt          # second account → "claude-alt" slot
#   3. add to ${DUCTOR_HOME:-~/.ductor}/config/config.json:
#        "claude_accounts": ["claude", "claude-alt"]
#   4. ductor auto-falls back to "claude-alt" when "claude" hits its 5-hour cap.
#
# Each slot gets its own dir under ${DUCTOR_HOME}/.cli-auth/, but all slots
# share the same projects/ (sessions) via a symlink so session continuity is
# preserved across accounts.

set -euo pipefail

# Address the bot by its docker compose *service* name, not a container name.
# A container_name override (e.g. "ductor-bot-staging") would otherwise break
# raw "docker exec ductor-bot". Running through "docker compose" against this
# repo's compose file resolves the service regardless of the container's name.
SERVICE="ductor-bot"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
dc() { docker compose --project-directory "$PROJECT_DIR" "$@"; }
source "$SCRIPT_DIR/_home.sh"
DUCTOR_HOME_HOST="$(resolve_ductor_home "$PROJECT_DIR")"
AUTH_DIR_HOST="${DUCTOR_HOME_HOST}/.cli-auth"
SHARED_PROJECTS_HOST="${AUTH_DIR_HOST}/_shared-projects"

# Messages for a person are in the language of scripts/_home.sh (stored setting, locale, else
# English). Commands, options, paths, provider names and the internal slot state stay as they are.
HOST_LANGUAGE="$(host_language "" "$DUCTOR_HOME_HOST")"
say() { printf '%s\n' "$(host_text login "$@")"; }
say_err() { say "$@" >&2; }

login_text_en() {
    case "$1" in
        err_slot_name) printf '%s' $'Invalid Claude slot name; slots is reserved.' ;;
        summary_title) printf '%s' $'Login summary:' ;;
        summary_ok) printf '%s' $'OK' ;;
        summary_failed) printf '%s' $'Failed' ;;
        slots_title) printf '%s' $'=== Claude Code Slots ===' ;;
        no_slots) printf '%s' $'  (no slots found — run ./scripts/login.sh claude to create the default)' ;;
        state_auth) printf '%s' $'authenticated' ;;
        state_not) printf '%s' $'not authenticated' ;;
        default_marker) printf '%s' $'(default)' ;;
        unknown_provider) printf '%s' $'Unknown provider: {1}' ;;
        login_help_title) printf '%s' $'Login to CLI providers inside the ductor-bot container.' ;;
        usage) printf '%s' $'Usage:' ;;
        h_login_all) printf '%s' $'login to all available providers' ;;
        h_login_claude) printf '%s' $'login to Claude Code default slot' ;;
        h_login_claude_slot) printf '%s' $'login to extra Claude slot (e.g. alt, work)' ;;
        h_login_list) printf '%s' $'show configured Claude slots + auth state' ;;
        h_repair) printf '%s' $'re-share settings/skills/plugins from default slot to every alt slot' ;;
        h_login_codex) printf '%s' $'login to Codex only' ;;
        h_login_gemini) printf '%s' $'login to Gemini only' ;;
        h_login_grok) printf '%s' $'login to Grok only' ;;
        h_help) printf '%s' $'show this help' ;;
        h_multi_title) printf '%s' $'Multi-account Claude (backup subscriptions):' ;;
        h_step1) printf '%s' $'first Max/Pro account → default slot' ;;
        h_step2) printf '%s' $'second account → "claude-alt" slot' ;;
        h_step3) printf '%s' $'add to {1}/config/config.json:' ;;
        h_step4) printf '%s' $'ductor auto-falls back to "claude-alt" when "claude" hits its 5-hour cap.' ;;
        h_share) printf '%s' $'Each slot gets its own dir under {1}/.cli-auth/, but all slots\nshare the same projects/ (sessions) via a symlink so session continuity is\npreserved across accounts.' ;;
        err_no_service) printf '%s' $'Error: service \'{1}\' is not running. Run \'docker compose up -d\' first.' ;;
        err_shared_notdir) printf '%s' $'Error: \'{1}\' exists and is not a directory.\n       Move it aside and re-run ./scripts/login.sh {2}.' ;;
        err_both) printf '%s' $'Error: both \'{1}\' (real dir) and \'{2}\'\n       already hold session data. Refusing to auto-merge.\n       Merge the two directories manually, then rerun login.sh.' ;;
        migrating) printf '%s' $'Migrating legacy sessions: {1} → {2}' ;;
        note_untouched) printf '%s' $'Note: \'{1}\' exists and is not a symlink/dir — leaving untouched.' ;;
        tip_fallback) printf '%s' $'Tip: add \'claude_accounts\' to config.json to enable auto-fallback.' ;;
        multi_detected) printf '%s' $'=== Multi-slot Claude detected ({1} authenticated) ===' ;;
        cfg_head) printf '%s' $'config.json claude_accounts:' ;;
        cfg_current) printf '%s' $'  current:  {1}' ;;
        cfg_proposed) printf '%s' $'  proposed: {1}' ;;
        cfg_empty) printf '%s' $'(empty)' ;;
        no_tty) printf '%s' $'(stdin is not a TTY — applying the default update automatically)' ;;
        prompt_update) printf '%s' $'Update config.json with the proposed list? [Y/n] ' ;;
        updated) printf '%s' $'Updated. Restart the bot to apply:' ;;
        skipped) printf '%s' $'Skipped. You can edit config.json manually later.' ;;
        stale) printf '%s' $'  replacing stale symlink: {1}' ;;
        migrating_entry) printf '%s' $'  migrating {1} → symlink (backup under .pre-share-backup/{2}/)' ;;
        login_default) printf '%s' $'=== Claude Code Login (default slot: claude) ===' ;;
        login_slot) printf '%s' $'=== Claude Code Login (slot: {1}) ===' ;;
        warn_nocreds) printf '%s' $'Warning: login finished but no credentials.json found in {1}.\n         Re-run and complete the OAuth flow in your browser.' ;;
        warn_provider_nocreds) printf '%s' $'Warning: login finished but no credentials were found in {1}. Re-run and complete sign-in.' ;;
        ok_slot) printf '%s' $'OK: slot \'{1}\' is authenticated.' ;;
        repairing) printf '%s' $'=== Repairing slot: {1} ===' ;;
        none_alt) printf '%s' $'No alt slots found under {1}.' ;;
        repaired) printf '%s' $'Repaired {1} slot(s).' ;;
        codex_login) printf '%s' $'=== Codex Device Login ===' ;;
        gemini_login) printf '%s' $'=== Gemini CLI Login ===' ;;
        gemini_launch) printf '%s' $'Launching Gemini CLI. On first run it prompts for auth method.' ;;
        gemini_google) printf '%s' $'  - \'Login with Google\': OAuth via loopback (needs SSH port-forward if remote).' ;;
        gemini_apikey) printf '%s' $'  - \'Use Gemini API Key\': put GEMINI_API_KEY in {1}/.env and exit here.' ;;
        gemini_recreate) printf '%s' $'  After editing {1}/.env, run docker compose up -d to recreate the container.' ;;
        gemini_reauth) printf '%s' $'To re-auth later, type /auth inside the gemini session.' ;;
    esac
}

login_text_de() {
    case "$1" in
        err_slot_name) printf '%s' $'Ungültiger Claude-Slotname; slots ist reserviert.' ;;
        summary_title) printf '%s' $'Anmeldeübersicht:' ;;
        summary_ok) printf '%s' $'OK' ;;
        summary_failed) printf '%s' $'Fehlgeschlagen' ;;
        slots_title) printf '%s' $'=== Claude-Code-Slots ===' ;;
        no_slots) printf '%s' $'  (keine Slots gefunden — führe ./scripts/login.sh claude aus, um den Standard-Slot anzulegen)' ;;
        state_auth) printf '%s' $'angemeldet' ;;
        state_not) printf '%s' $'nicht angemeldet' ;;
        default_marker) printf '%s' $'(Standard)' ;;
        unknown_provider) printf '%s' $'Unbekannter Anbieter: {1}' ;;
        login_help_title) printf '%s' $'Bei CLI-Anbietern im ductor-bot-Container anmelden.' ;;
        usage) printf '%s' $'Verwendung:' ;;
        h_login_all) printf '%s' $'bei allen verfügbaren Anbietern anmelden' ;;
        h_login_claude) printf '%s' $'am Standard-Slot von Claude Code anmelden' ;;
        h_login_claude_slot) printf '%s' $'an einem zusätzlichen Claude-Slot anmelden (z. B. alt, work)' ;;
        h_login_list) printf '%s' $'konfigurierte Claude-Slots + Anmeldestatus zeigen' ;;
        h_repair) printf '%s' $'Einstellungen/Skills/Plugins vom Standard-Slot erneut an jeden alt-Slot teilen' ;;
        h_login_codex) printf '%s' $'nur bei Codex anmelden' ;;
        h_login_gemini) printf '%s' $'nur bei Gemini anmelden' ;;
        h_login_grok) printf '%s' $'nur bei Grok anmelden' ;;
        h_help) printf '%s' $'diese Hilfe zeigen' ;;
        h_multi_title) printf '%s' $'Claude mit mehreren Konten (Reserve-Abos):' ;;
        h_step1) printf '%s' $'erstes Max/Pro-Konto → Standard-Slot' ;;
        h_step2) printf '%s' $'zweites Konto → Slot "claude-alt"' ;;
        h_step3) printf '%s' $'in {1}/config/config.json eintragen:' ;;
        h_step4) printf '%s' $'ductor wechselt automatisch zu "claude-alt", wenn "claude" sein 5-Stunden-Limit erreicht.' ;;
        h_share) printf '%s' $'Jeder Slot hat ein eigenes Verzeichnis unter {1}/.cli-auth/, aber alle Slots\nteilen dasselbe projects/ (Sitzungen) über einen Symlink, damit die Sitzungen\nüber Konten hinweg erhalten bleiben.' ;;
        err_no_service) printf '%s' $'Fehler: Der Dienst \'{1}\' läuft nicht. Starte zuerst \'docker compose up -d\'.' ;;
        err_shared_notdir) printf '%s' $'Fehler: \'{1}\' existiert und ist kein Verzeichnis.\n       Verschiebe es und führe ./scripts/login.sh {2} erneut aus.' ;;
        err_both) printf '%s' $'Fehler: Sowohl \'{1}\' (echtes Verzeichnis) als auch \'{2}\'\n       enthalten bereits Sitzungsdaten. Automatisches Zusammenführen wird abgelehnt.\n       Führe die beiden Verzeichnisse von Hand zusammen und starte login.sh erneut.' ;;
        migrating) printf '%s' $'Alte Sitzungen werden verschoben: {1} → {2}' ;;
        note_untouched) printf '%s' $'Hinweis: \'{1}\' existiert und ist kein Symlink/Verzeichnis — bleibt unverändert.' ;;
        tip_fallback) printf '%s' $'Tipp: Trage \'claude_accounts\' in config.json ein, um den automatischen Wechsel zu aktivieren.' ;;
        multi_detected) printf '%s' $'=== Mehrere Claude-Slots erkannt ({1} angemeldet) ===' ;;
        cfg_head) printf '%s' $'config.json claude_accounts:' ;;
        cfg_current) printf '%s' $'  aktuell:  {1}' ;;
        cfg_proposed) printf '%s' $'  Vorschlag: {1}' ;;
        cfg_empty) printf '%s' $'(leer)' ;;
        no_tty) printf '%s' $'(stdin ist kein TTY — Standardaktualisierung wird automatisch angewendet)' ;;
        prompt_update) printf '%s' $'config.json mit der vorgeschlagenen Liste aktualisieren? [Y/n] ' ;;
        updated) printf '%s' $'Aktualisiert. Starte den Bot neu, damit es wirkt:' ;;
        skipped) printf '%s' $'Übersprungen. Du kannst config.json später von Hand bearbeiten.' ;;
        stale) printf '%s' $'  veralteter Symlink wird ersetzt: {1}' ;;
        migrating_entry) printf '%s' $'  {1} wird zu einem Symlink (Sicherung unter .pre-share-backup/{2}/)' ;;
        login_default) printf '%s' $'=== Claude-Code-Anmeldung (Standard-Slot: claude) ===' ;;
        login_slot) printf '%s' $'=== Claude-Code-Anmeldung (Slot: {1}) ===' ;;
        warn_nocreds) printf '%s' $'Warnung: Die Anmeldung ist beendet, aber in {1} wurde keine credentials.json gefunden.\n         Starte erneut und schließe den OAuth-Ablauf im Browser ab.' ;;
        warn_provider_nocreds) printf '%s' $'Warnung: Die Anmeldung ist beendet, aber in {1} wurden keine Anmeldedaten gefunden. Starte erneut und schließe die Anmeldung ab.' ;;
        ok_slot) printf '%s' $'OK: Slot \'{1}\' ist angemeldet.' ;;
        repairing) printf '%s' $'=== Slot wird repariert: {1} ===' ;;
        none_alt) printf '%s' $'Keine alt-Slots unter {1} gefunden.' ;;
        repaired) printf '%s' $'{1} Slot(s) repariert.' ;;
        codex_login) printf '%s' $'=== Codex-Geräteanmeldung ===' ;;
        gemini_login) printf '%s' $'=== Gemini-CLI-Anmeldung ===' ;;
        gemini_launch) printf '%s' $'Gemini CLI wird gestartet. Beim ersten Start fragt es nach der Anmeldemethode.' ;;
        gemini_google) printf '%s' $'  - \'Login with Google\': OAuth über Loopback (bei Remote-Rechnern SSH-Portweiterleitung nötig).' ;;
        gemini_apikey) printf '%s' $'  - \'Use Gemini API Key\': trage GEMINI_API_KEY in {1}/.env ein und beende hier.' ;;
        gemini_recreate) printf '%s' $'  Nach dem Bearbeiten von {1}/.env den Container mit docker compose up -d neu erstellen.' ;;
        gemini_reauth) printf '%s' $'Für eine spätere Neuanmeldung gib /auth in der Gemini-Sitzung ein.' ;;
    esac
}

login_text_es() {
    case "$1" in
        err_slot_name) printf '%s' $'Nombre de slot Claude no válido; slots está reservado.' ;;
        summary_title) printf '%s' $'Resumen de inicio de sesión:' ;;
        summary_ok) printf '%s' $'OK' ;;
        summary_failed) printf '%s' $'Falló' ;;
        slots_title) printf '%s' $'=== Slots de Claude Code ===' ;;
        no_slots) printf '%s' $'  (no se encontraron slots — ejecuta ./scripts/login.sh claude para crear el predeterminado)' ;;
        state_auth) printf '%s' $'autenticado' ;;
        state_not) printf '%s' $'no autenticado' ;;
        default_marker) printf '%s' $'(predeterminado)' ;;
        unknown_provider) printf '%s' $'Proveedor desconocido: {1}' ;;
        login_help_title) printf '%s' $'Iniciar sesión en los proveedores de CLI dentro del contenedor ductor-bot.' ;;
        usage) printf '%s' $'Uso:' ;;
        h_login_all) printf '%s' $'iniciar sesión en todos los proveedores disponibles' ;;
        h_login_claude) printf '%s' $'iniciar sesión en el slot predeterminado de Claude Code' ;;
        h_login_claude_slot) printf '%s' $'iniciar sesión en un slot de Claude adicional (p. ej. alt, work)' ;;
        h_login_list) printf '%s' $'mostrar los slots de Claude configurados + estado de autenticación' ;;
        h_repair) printf '%s' $'volver a compartir ajustes/skills/plugins del slot predeterminado con cada slot alt' ;;
        h_login_codex) printf '%s' $'iniciar sesión solo en Codex' ;;
        h_login_gemini) printf '%s' $'iniciar sesión solo en Gemini' ;;
        h_login_grok) printf '%s' $'iniciar sesión solo en Grok' ;;
        h_help) printf '%s' $'mostrar esta ayuda' ;;
        h_multi_title) printf '%s' $'Claude con varias cuentas (suscripciones de respaldo):' ;;
        h_step1) printf '%s' $'primera cuenta Max/Pro → slot predeterminado' ;;
        h_step2) printf '%s' $'segunda cuenta → slot "claude-alt"' ;;
        h_step3) printf '%s' $'añade a {1}/config/config.json:' ;;
        h_step4) printf '%s' $'ductor cambia automáticamente a "claude-alt" cuando "claude" llega a su límite de 5 horas.' ;;
        h_share) printf '%s' $'Cada slot tiene su propio directorio bajo {1}/.cli-auth/, pero todos los slots\ncomparten el mismo projects/ (sesiones) mediante un enlace simbólico para que las\nsesiones continúen entre cuentas.' ;;
        err_no_service) printf '%s' $'Error: el servicio \'{1}\' no está en ejecución. Ejecuta primero \'docker compose up -d\'.' ;;
        err_shared_notdir) printf '%s' $'Error: \'{1}\' existe y no es un directorio.\n       Apártalo y vuelve a ejecutar ./scripts/login.sh {2}.' ;;
        err_both) printf '%s' $'Error: tanto \'{1}\' (directorio real) como \'{2}\'\n       ya contienen datos de sesión. Se rechaza la fusión automática.\n       Fusiona los dos directorios a mano y vuelve a ejecutar login.sh.' ;;
        migrating) printf '%s' $'Migrando sesiones antiguas: {1} → {2}' ;;
        note_untouched) printf '%s' $'Nota: \'{1}\' existe y no es un enlace simbólico ni un directorio — se deja intacto.' ;;
        tip_fallback) printf '%s' $'Consejo: añade \'claude_accounts\' a config.json para activar el cambio automático.' ;;
        multi_detected) printf '%s' $'=== Varios slots de Claude detectados ({1} autenticados) ===' ;;
        cfg_head) printf '%s' $'config.json claude_accounts:' ;;
        cfg_current) printf '%s' $'  actual:    {1}' ;;
        cfg_proposed) printf '%s' $'  propuesto: {1}' ;;
        cfg_empty) printf '%s' $'(vacío)' ;;
        no_tty) printf '%s' $'(stdin no es un TTY — se aplica automáticamente la actualización predeterminada)' ;;
        prompt_update) printf '%s' $'¿Actualizar config.json con la lista propuesta? [Y/n] ' ;;
        updated) printf '%s' $'Actualizado. Reinicia el bot para aplicarlo:' ;;
        skipped) printf '%s' $'Omitido. Puedes editar config.json a mano más tarde.' ;;
        stale) printf '%s' $'  sustituyendo un enlace simbólico obsoleto: {1}' ;;
        migrating_entry) printf '%s' $'  {1} pasa a ser un enlace simbólico (copia en .pre-share-backup/{2}/)' ;;
        login_default) printf '%s' $'=== Inicio de sesión en Claude Code (slot predeterminado: claude) ===' ;;
        login_slot) printf '%s' $'=== Inicio de sesión en Claude Code (slot: {1}) ===' ;;
        warn_nocreds) printf '%s' $'Aviso: el inicio de sesión terminó pero no se encontró credentials.json en {1}.\n       Vuelve a ejecutarlo y completa el flujo OAuth en tu navegador.' ;;
        warn_provider_nocreds) printf '%s' $'Aviso: el inicio de sesión terminó, pero no se encontraron credenciales en {1}. Vuelve a ejecutarlo y completa el inicio de sesión.' ;;
        ok_slot) printf '%s' $'OK: el slot \'{1}\' está autenticado.' ;;
        repairing) printf '%s' $'=== Reparando slot: {1} ===' ;;
        none_alt) printf '%s' $'No se encontraron slots alt en {1}.' ;;
        repaired) printf '%s' $'{1} slot(s) reparado(s).' ;;
        codex_login) printf '%s' $'=== Inicio de sesión en Codex mediante dispositivo ===' ;;
        gemini_login) printf '%s' $'=== Inicio de sesión en Gemini CLI ===' ;;
        gemini_launch) printf '%s' $'Iniciando Gemini CLI. En la primera ejecución pregunta el método de autenticación.' ;;
        gemini_google) printf '%s' $'  - \'Login with Google\': OAuth por loopback (requiere reenvío de puerto SSH si es remoto).' ;;
        gemini_apikey) printf '%s' $'  - \'Use Gemini API Key\': pon GEMINI_API_KEY en {1}/.env y sal aquí.' ;;
        gemini_recreate) printf '%s' $'  Tras editar {1}/.env, ejecuta docker compose up -d para recrear el contenedor.' ;;
        gemini_reauth) printf '%s' $'Para volver a autenticarte más tarde, escribe /auth dentro de la sesión de gemini.' ;;
    esac
}

login_text_fr() {
    case "$1" in
        err_slot_name) printf '%s' $'Nom de slot Claude invalide ; slots est réservé.' ;;
        summary_title) printf '%s' $'Résumé des connexions :' ;;
        summary_ok) printf '%s' $'OK' ;;
        summary_failed) printf '%s' $'Échec' ;;
        slots_title) printf '%s' $'=== Slots Claude Code ===' ;;
        no_slots) printf '%s' $'  (aucun slot trouvé — lance ./scripts/login.sh claude pour créer celui par défaut)' ;;
        state_auth) printf '%s' $'connecté' ;;
        state_not) printf '%s' $'non connecté' ;;
        default_marker) printf '%s' $'(par défaut)' ;;
        unknown_provider) printf '%s' $'Fournisseur inconnu : {1}' ;;
        login_help_title) printf '%s' $'Se connecter aux fournisseurs CLI dans le conteneur ductor-bot.' ;;
        usage) printf '%s' $'Utilisation :' ;;
        h_login_all) printf '%s' $'se connecter à tous les fournisseurs disponibles' ;;
        h_login_claude) printf '%s' $'se connecter au slot par défaut de Claude Code' ;;
        h_login_claude_slot) printf '%s' $'se connecter à un slot Claude supplémentaire (p. ex. alt, work)' ;;
        h_login_list) printf '%s' $'afficher les slots Claude configurés + état de connexion' ;;
        h_repair) printf '%s' $'repartager réglages/skills/plugins du slot par défaut vers chaque slot alt' ;;
        h_login_codex) printf '%s' $'se connecter à Codex uniquement' ;;
        h_login_gemini) printf '%s' $'se connecter à Gemini uniquement' ;;
        h_login_grok) printf '%s' $'se connecter à Grok uniquement' ;;
        h_help) printf '%s' $'afficher cette aide' ;;
        h_multi_title) printf '%s' $'Claude multi-comptes (abonnements de secours) :' ;;
        h_step1) printf '%s' $'premier compte Max/Pro → slot par défaut' ;;
        h_step2) printf '%s' $'second compte → slot "claude-alt"' ;;
        h_step3) printf '%s' $'ajoute dans {1}/config/config.json :' ;;
        h_step4) printf '%s' $'ductor bascule automatiquement sur "claude-alt" quand "claude" atteint sa limite de 5 heures.' ;;
        h_share) printf '%s' $'Chaque slot a son propre dossier sous {1}/.cli-auth/, mais tous les slots\npartagent le même projects/ (sessions) via un lien symbolique, pour que les\nsessions se poursuivent d\'un compte à l\'autre.' ;;
        err_no_service) printf '%s' $'Erreur : le service \'{1}\' ne tourne pas. Lance d\'abord \'docker compose up -d\'.' ;;
        err_shared_notdir) printf '%s' $'Erreur : \'{1}\' existe et n\'est pas un dossier.\n       Déplace-le puis relance ./scripts/login.sh {2}.' ;;
        err_both) printf '%s' $'Erreur : \'{1}\' (vrai dossier) et \'{2}\'\n       contiennent déjà des données de session. Fusion automatique refusée.\n       Fusionne les deux dossiers à la main, puis relance login.sh.' ;;
        migrating) printf '%s' $'Migration des anciennes sessions : {1} → {2}' ;;
        note_untouched) printf '%s' $'Remarque : \'{1}\' existe et n\'est ni un lien symbolique ni un dossier — laissé tel quel.' ;;
        tip_fallback) printf '%s' $'Astuce : ajoute \'claude_accounts\' dans config.json pour activer le basculement automatique.' ;;
        multi_detected) printf '%s' $'=== Plusieurs slots Claude détectés ({1} connectés) ===' ;;
        cfg_head) printf '%s' $'config.json claude_accounts :' ;;
        cfg_current) printf '%s' $'  actuel :  {1}' ;;
        cfg_proposed) printf '%s' $'  proposé : {1}' ;;
        cfg_empty) printf '%s' $'(vide)' ;;
        no_tty) printf '%s' $'(stdin n\'est pas un TTY — application automatique de la mise à jour par défaut)' ;;
        prompt_update) printf '%s' $'Mettre à jour config.json avec la liste proposée ? [Y/n] ' ;;
        updated) printf '%s' $'Mis à jour. Redémarre le bot pour appliquer :' ;;
        skipped) printf '%s' $'Ignoré. Tu pourras modifier config.json à la main plus tard.' ;;
        stale) printf '%s' $'  remplacement d\'un lien symbolique obsolète : {1}' ;;
        migrating_entry) printf '%s' $'  {1} devient un lien symbolique (sauvegarde sous .pre-share-backup/{2}/)' ;;
        login_default) printf '%s' $'=== Connexion Claude Code (slot par défaut : claude) ===' ;;
        login_slot) printf '%s' $'=== Connexion Claude Code (slot : {1}) ===' ;;
        warn_nocreds) printf '%s' $'Attention : la connexion est terminée mais aucun credentials.json n\'a été trouvé dans {1}.\n         Relance et termine le flux OAuth dans ton navigateur.' ;;
        warn_provider_nocreds) printf '%s' $'Attention : la connexion est terminée, mais aucun identifiant de connexion n’a été trouvé dans {1}. Relance et termine la connexion.' ;;
        ok_slot) printf '%s' $'OK : le slot \'{1}\' est connecté.' ;;
        repairing) printf '%s' $'=== Réparation du slot : {1} ===' ;;
        none_alt) printf '%s' $'Aucun slot alt trouvé sous {1}.' ;;
        repaired) printf '%s' $'{1} slot(s) réparé(s).' ;;
        codex_login) printf '%s' $'=== Connexion Codex par appareil ===' ;;
        gemini_login) printf '%s' $'=== Connexion Gemini CLI ===' ;;
        gemini_launch) printf '%s' $'Lancement de Gemini CLI. Au premier démarrage, il demande la méthode d\'authentification.' ;;
        gemini_google) printf '%s' $'  - \'Login with Google\' : OAuth via loopback (redirection de port SSH nécessaire à distance).' ;;
        gemini_apikey) printf '%s' $'  - \'Use Gemini API Key\' : mets GEMINI_API_KEY dans {1}/.env et quitte ici.' ;;
        gemini_recreate) printf '%s' $'  Après modification de {1}/.env, lance docker compose up -d pour recréer le conteneur.' ;;
        gemini_reauth) printf '%s' $'Pour te reconnecter plus tard, tape /auth dans la session gemini.' ;;
    esac
}

login_text_id() {
    case "$1" in
        err_slot_name) printf '%s' $'Nama slot Claude tidak valid; slots dicadangkan.' ;;
        summary_title) printf '%s' $'Ringkasan login:' ;;
        summary_ok) printf '%s' $'OK' ;;
        summary_failed) printf '%s' $'Gagal' ;;
        slots_title) printf '%s' $'=== Slot Claude Code ===' ;;
        no_slots) printf '%s' $'  (slot tidak ditemukan — jalankan ./scripts/login.sh claude untuk membuat slot default)' ;;
        state_auth) printf '%s' $'terautentikasi' ;;
        state_not) printf '%s' $'belum terautentikasi' ;;
        default_marker) printf '%s' $'(default)' ;;
        unknown_provider) printf '%s' $'Penyedia tidak dikenal: {1}' ;;
        login_help_title) printf '%s' $'Masuk ke penyedia CLI di dalam kontainer ductor-bot.' ;;
        usage) printf '%s' $'Penggunaan:' ;;
        h_login_all) printf '%s' $'masuk ke semua penyedia yang tersedia' ;;
        h_login_claude) printf '%s' $'masuk ke slot default Claude Code' ;;
        h_login_claude_slot) printf '%s' $'masuk ke slot Claude tambahan (mis. alt, work)' ;;
        h_login_list) printf '%s' $'tampilkan slot Claude yang dikonfigurasi + status autentikasi' ;;
        h_repair) printf '%s' $'bagikan ulang pengaturan/skill/plugin dari slot default ke setiap slot alt' ;;
        h_login_codex) printf '%s' $'masuk ke Codex saja' ;;
        h_login_gemini) printf '%s' $'masuk ke Gemini saja' ;;
        h_login_grok) printf '%s' $'masuk ke Grok saja' ;;
        h_help) printf '%s' $'tampilkan bantuan ini' ;;
        h_multi_title) printf '%s' $'Claude multi-akun (langganan cadangan):' ;;
        h_step1) printf '%s' $'akun Max/Pro pertama → slot default' ;;
        h_step2) printf '%s' $'akun kedua → slot "claude-alt"' ;;
        h_step3) printf '%s' $'tambahkan ke {1}/config/config.json:' ;;
        h_step4) printf '%s' $'ductor otomatis beralih ke "claude-alt" saat "claude" mencapai batas 5 jam.' ;;
        h_share) printf '%s' $'Setiap slot punya direktori sendiri di {1}/.cli-auth/, tetapi semua slot\nberbagi projects/ (sesi) yang sama lewat symlink agar sesi tetap\nberlanjut antar akun.' ;;
        err_no_service) printf '%s' $'Kesalahan: layanan \'{1}\' tidak berjalan. Jalankan \'docker compose up -d\' terlebih dahulu.' ;;
        err_shared_notdir) printf '%s' $'Kesalahan: \'{1}\' ada dan bukan direktori.\n           Pindahkan dulu, lalu jalankan ulang ./scripts/login.sh {2}.' ;;
        err_both) printf '%s' $'Kesalahan: \'{1}\' (direktori asli) dan \'{2}\'\n           sama-sama sudah berisi data sesi. Penggabungan otomatis ditolak.\n           Gabungkan kedua direktori secara manual, lalu jalankan ulang login.sh.' ;;
        migrating) printf '%s' $'Memindahkan sesi lama: {1} → {2}' ;;
        note_untouched) printf '%s' $'Catatan: \'{1}\' ada dan bukan symlink/direktori — dibiarkan apa adanya.' ;;
        tip_fallback) printf '%s' $'Tips: tambahkan \'claude_accounts\' ke config.json untuk mengaktifkan peralihan otomatis.' ;;
        multi_detected) printf '%s' $'=== Beberapa slot Claude terdeteksi ({1} terautentikasi) ===' ;;
        cfg_head) printf '%s' $'config.json claude_accounts:' ;;
        cfg_current) printf '%s' $'  saat ini: {1}' ;;
        cfg_proposed) printf '%s' $'  usulan:   {1}' ;;
        cfg_empty) printf '%s' $'(kosong)' ;;
        no_tty) printf '%s' $'(stdin bukan TTY — pembaruan bawaan diterapkan otomatis)' ;;
        prompt_update) printf '%s' $'Perbarui config.json dengan daftar yang diusulkan? [Y/n] ' ;;
        updated) printf '%s' $'Diperbarui. Mulai ulang bot agar berlaku:' ;;
        skipped) printf '%s' $'Dilewati. Anda dapat mengedit config.json secara manual nanti.' ;;
        stale) printf '%s' $'  mengganti symlink usang: {1}' ;;
        migrating_entry) printf '%s' $'  {1} dijadikan symlink (cadangan di .pre-share-backup/{2}/)' ;;
        login_default) printf '%s' $'=== Masuk ke Claude Code (slot default: claude) ===' ;;
        login_slot) printf '%s' $'=== Masuk ke Claude Code (slot: {1}) ===' ;;
        warn_nocreds) printf '%s' $'Peringatan: proses masuk selesai tetapi credentials.json tidak ditemukan di {1}.\n           Jalankan ulang dan selesaikan alur OAuth di browser Anda.' ;;
        warn_provider_nocreds) printf '%s' $'Peringatan: proses masuk selesai, tetapi kredensial tidak ditemukan di {1}. Jalankan ulang dan selesaikan proses masuk.' ;;
        ok_slot) printf '%s' $'OK: slot \'{1}\' terautentikasi.' ;;
        repairing) printf '%s' $'=== Memperbaiki slot: {1} ===' ;;
        none_alt) printf '%s' $'Tidak ada slot alt di {1}.' ;;
        repaired) printf '%s' $'{1} slot diperbaiki.' ;;
        codex_login) printf '%s' $'=== Masuk ke Codex melalui perangkat ===' ;;
        gemini_login) printf '%s' $'=== Masuk ke Gemini CLI ===' ;;
        gemini_launch) printf '%s' $'Menjalankan Gemini CLI. Pada eksekusi pertama ia menanyakan metode autentikasi.' ;;
        gemini_google) printf '%s' $'  - \'Login with Google\': OAuth lewat loopback (perlu port-forward SSH jika remote).' ;;
        gemini_apikey) printf '%s' $'  - \'Use Gemini API Key\': taruh GEMINI_API_KEY di {1}/.env lalu keluar dari sini.' ;;
        gemini_recreate) printf '%s' $'  Setelah mengedit {1}/.env, jalankan docker compose up -d untuk membuat ulang container.' ;;
        gemini_reauth) printf '%s' $'Untuk autentikasi ulang nanti, ketik /auth di dalam sesi gemini.' ;;
    esac
}

login_text_nl() {
    case "$1" in
        err_slot_name) printf '%s' $'Ongeldige Claude-slotnaam; slots is gereserveerd.' ;;
        summary_title) printf '%s' $'Aanmeldingsoverzicht:' ;;
        summary_ok) printf '%s' $'OK' ;;
        summary_failed) printf '%s' $'Mislukt' ;;
        slots_title) printf '%s' $'=== Claude Code-slots ===' ;;
        no_slots) printf '%s' $'  (geen slots gevonden — voer ./scripts/login.sh claude uit om de standaard aan te maken)' ;;
        state_auth) printf '%s' $'aangemeld' ;;
        state_not) printf '%s' $'niet aangemeld' ;;
        default_marker) printf '%s' $'(standaard)' ;;
        unknown_provider) printf '%s' $'Onbekende provider: {1}' ;;
        login_help_title) printf '%s' $'Aanmelden bij CLI-providers in de ductor-bot-container.' ;;
        usage) printf '%s' $'Gebruik:' ;;
        h_login_all) printf '%s' $'aanmelden bij alle beschikbare providers' ;;
        h_login_claude) printf '%s' $'aanmelden bij het standaardslot van Claude Code' ;;
        h_login_claude_slot) printf '%s' $'aanmelden bij een extra Claude-slot (bijv. alt, work)' ;;
        h_login_list) printf '%s' $'ingestelde Claude-slots + aanmeldstatus tonen' ;;
        h_repair) printf '%s' $'instellingen/skills/plugins van het standaardslot opnieuw delen met elk alt-slot' ;;
        h_login_codex) printf '%s' $'alleen aanmelden bij Codex' ;;
        h_login_gemini) printf '%s' $'alleen aanmelden bij Gemini' ;;
        h_login_grok) printf '%s' $'alleen aanmelden bij Grok' ;;
        h_help) printf '%s' $'deze hulp tonen' ;;
        h_multi_title) printf '%s' $'Claude met meerdere accounts (reserveabonnementen):' ;;
        h_step1) printf '%s' $'eerste Max/Pro-account → standaardslot' ;;
        h_step2) printf '%s' $'tweede account → slot "claude-alt"' ;;
        h_step3) printf '%s' $'voeg toe aan {1}/config/config.json:' ;;
        h_step4) printf '%s' $'ductor schakelt automatisch over op "claude-alt" als "claude" zijn limiet van 5 uur bereikt.' ;;
        h_share) printf '%s' $'Elk slot krijgt een eigen map onder {1}/.cli-auth/, maar alle slots\ndelen dezelfde projects/ (sessies) via een symlink, zodat sessies\ndoorlopen over accounts heen.' ;;
        err_no_service) printf '%s' $'Fout: de service \'{1}\' draait niet. Voer eerst \'docker compose up -d\' uit.' ;;
        err_shared_notdir) printf '%s' $'Fout: \'{1}\' bestaat en is geen map.\n       Verplaats het en voer ./scripts/login.sh {2} opnieuw uit.' ;;
        err_both) printf '%s' $'Fout: zowel \'{1}\' (echte map) als \'{2}\'\n       bevatten al sessiegegevens. Automatisch samenvoegen wordt geweigerd.\n       Voeg de twee mappen handmatig samen en voer login.sh opnieuw uit.' ;;
        migrating) printf '%s' $'Oude sessies worden verplaatst: {1} → {2}' ;;
        note_untouched) printf '%s' $'Let op: \'{1}\' bestaat en is geen symlink of map — blijft ongewijzigd.' ;;
        tip_fallback) printf '%s' $'Tip: voeg \'claude_accounts\' toe aan config.json om automatisch overschakelen te activeren.' ;;
        multi_detected) printf '%s' $'=== Meerdere Claude-slots gevonden ({1} aangemeld) ===' ;;
        cfg_head) printf '%s' $'config.json claude_accounts:' ;;
        cfg_current) printf '%s' $'  huidig:   {1}' ;;
        cfg_proposed) printf '%s' $'  voorstel: {1}' ;;
        cfg_empty) printf '%s' $'(leeg)' ;;
        no_tty) printf '%s' $'(stdin is geen TTY — standaardupdate wordt automatisch toegepast)' ;;
        prompt_update) printf '%s' $'config.json bijwerken met de voorgestelde lijst? [Y/n] ' ;;
        updated) printf '%s' $'Bijgewerkt. Herstart de bot om het toe te passen:' ;;
        skipped) printf '%s' $'Overgeslagen. Je kunt config.json later handmatig aanpassen.' ;;
        stale) printf '%s' $'  verouderde symlink wordt vervangen: {1}' ;;
        migrating_entry) printf '%s' $'  {1} wordt een symlink (back-up onder .pre-share-backup/{2}/)' ;;
        login_default) printf '%s' $'=== Claude Code-aanmelding (standaardslot: claude) ===' ;;
        login_slot) printf '%s' $'=== Claude Code-aanmelding (slot: {1}) ===' ;;
        warn_nocreds) printf '%s' $'Waarschuwing: aanmelden is klaar, maar er is geen credentials.json gevonden in {1}.\n             Voer opnieuw uit en rond de OAuth-stap in je browser af.' ;;
        warn_provider_nocreds) printf '%s' $'Waarschuwing: aanmelden is klaar, maar er zijn geen aanmeldgegevens gevonden in {1}. Voer opnieuw uit en rond het aanmelden af.' ;;
        ok_slot) printf '%s' $'OK: slot \'{1}\' is aangemeld.' ;;
        repairing) printf '%s' $'=== Slot wordt hersteld: {1} ===' ;;
        none_alt) printf '%s' $'Geen alt-slots gevonden onder {1}.' ;;
        repaired) printf '%s' $'{1} slot(s) hersteld.' ;;
        codex_login) printf '%s' $'=== Codex-apparaataanmelding ===' ;;
        gemini_login) printf '%s' $'=== Gemini CLI-aanmelding ===' ;;
        gemini_launch) printf '%s' $'Gemini CLI wordt gestart. Bij de eerste keer vraagt het naar de aanmeldmethode.' ;;
        gemini_google) printf '%s' $'  - \'Login with Google\': OAuth via loopback (SSH-poortdoorverwijzing nodig op afstand).' ;;
        gemini_apikey) printf '%s' $'  - \'Use Gemini API Key\': zet GEMINI_API_KEY in {1}/.env en sluit hier af.' ;;
        gemini_recreate) printf '%s' $'  Voer na het bewerken van {1}/.env docker compose up -d uit om de container opnieuw aan te maken.' ;;
        gemini_reauth) printf '%s' $'Om later opnieuw aan te melden, typ je /auth in de gemini-sessie.' ;;
    esac
}

login_text_pt() {
    case "$1" in
        err_slot_name) printf '%s' $'Nome de slot Claude inválido; slots é reservado.' ;;
        summary_title) printf '%s' $'Resumo de login:' ;;
        summary_ok) printf '%s' $'OK' ;;
        summary_failed) printf '%s' $'Falhou' ;;
        slots_title) printf '%s' $'=== Slots do Claude Code ===' ;;
        no_slots) printf '%s' $'  (nenhum slot encontrado — execute ./scripts/login.sh claude para criar o padrão)' ;;
        state_auth) printf '%s' $'autenticado' ;;
        state_not) printf '%s' $'não autenticado' ;;
        default_marker) printf '%s' $'(padrão)' ;;
        unknown_provider) printf '%s' $'Provedor desconhecido: {1}' ;;
        login_help_title) printf '%s' $'Entrar nos provedores de CLI dentro do contêiner ductor-bot.' ;;
        usage) printf '%s' $'Uso:' ;;
        h_login_all) printf '%s' $'entrar em todos os provedores disponíveis' ;;
        h_login_claude) printf '%s' $'entrar no slot padrão do Claude Code' ;;
        h_login_claude_slot) printf '%s' $'entrar em um slot extra do Claude (ex.: alt, work)' ;;
        h_login_list) printf '%s' $'mostrar os slots do Claude configurados + estado de autenticação' ;;
        h_repair) printf '%s' $'compartilhar de novo configurações/skills/plugins do slot padrão com cada slot alt' ;;
        h_login_codex) printf '%s' $'entrar somente no Codex' ;;
        h_login_gemini) printf '%s' $'entrar somente no Gemini' ;;
        h_login_grok) printf '%s' $'entrar somente no Grok' ;;
        h_help) printf '%s' $'mostrar esta ajuda' ;;
        h_multi_title) printf '%s' $'Claude com várias contas (assinaturas de reserva):' ;;
        h_step1) printf '%s' $'primeira conta Max/Pro → slot padrão' ;;
        h_step2) printf '%s' $'segunda conta → slot "claude-alt"' ;;
        h_step3) printf '%s' $'adicione em {1}/config/config.json:' ;;
        h_step4) printf '%s' $'o ductor troca automaticamente para "claude-alt" quando "claude" atinge o limite de 5 horas.' ;;
        h_share) printf '%s' $'Cada slot tem seu próprio diretório em {1}/.cli-auth/, mas todos os slots\ncompartilham o mesmo projects/ (sessões) por um link simbólico, para que as\nsessões continuem entre contas.' ;;
        err_no_service) printf '%s' $'Erro: o serviço \'{1}\' não está rodando. Execute \'docker compose up -d\' primeiro.' ;;
        err_shared_notdir) printf '%s' $'Erro: \'{1}\' existe e não é um diretório.\n      Mova-o para outro lugar e execute ./scripts/login.sh {2} de novo.' ;;
        err_both) printf '%s' $'Erro: tanto \'{1}\' (diretório real) quanto \'{2}\'\n      já têm dados de sessão. A mesclagem automática foi recusada.\n      Mescle os dois diretórios manualmente e execute login.sh de novo.' ;;
        migrating) printf '%s' $'Migrando sessões antigas: {1} → {2}' ;;
        note_untouched) printf '%s' $'Nota: \'{1}\' existe e não é um link simbólico nem um diretório — deixado como está.' ;;
        tip_fallback) printf '%s' $'Dica: adicione \'claude_accounts\' ao config.json para ativar a troca automática.' ;;
        multi_detected) printf '%s' $'=== Vários slots do Claude detectados ({1} autenticados) ===' ;;
        cfg_head) printf '%s' $'config.json claude_accounts:' ;;
        cfg_current) printf '%s' $'  atual:     {1}' ;;
        cfg_proposed) printf '%s' $'  proposta:  {1}' ;;
        cfg_empty) printf '%s' $'(vazio)' ;;
        no_tty) printf '%s' $'(stdin não é um TTY — aplicando automaticamente a atualização padrão)' ;;
        prompt_update) printf '%s' $'Atualizar o config.json com a lista proposta? [Y/n] ' ;;
        updated) printf '%s' $'Atualizado. Reinicie o bot para aplicar:' ;;
        skipped) printf '%s' $'Ignorado. Você pode editar o config.json manualmente depois.' ;;
        stale) printf '%s' $'  substituindo link simbólico antigo: {1}' ;;
        migrating_entry) printf '%s' $'  {1} virando link simbólico (backup em .pre-share-backup/{2}/)' ;;
        login_default) printf '%s' $'=== Login do Claude Code (slot padrão: claude) ===' ;;
        login_slot) printf '%s' $'=== Login do Claude Code (slot: {1}) ===' ;;
        warn_nocreds) printf '%s' $'Aviso: o login terminou, mas nenhum credentials.json foi encontrado em {1}.\n       Execute de novo e conclua o fluxo OAuth no navegador.' ;;
        warn_provider_nocreds) printf '%s' $'Aviso: o login terminou, mas nenhuma credencial foi encontrada em {1}. Execute de novo e conclua o login.' ;;
        ok_slot) printf '%s' $'OK: o slot \'{1}\' está autenticado.' ;;
        repairing) printf '%s' $'=== Reparando slot: {1} ===' ;;
        none_alt) printf '%s' $'Nenhum slot alt encontrado em {1}.' ;;
        repaired) printf '%s' $'{1} slot(s) reparado(s).' ;;
        codex_login) printf '%s' $'=== Login do Codex por dispositivo ===' ;;
        gemini_login) printf '%s' $'=== Login do Gemini CLI ===' ;;
        gemini_launch) printf '%s' $'Iniciando o Gemini CLI. Na primeira execução ele pergunta o método de autenticação.' ;;
        gemini_google) printf '%s' $'  - \'Login with Google\': OAuth por loopback (precisa de redirecionamento de porta SSH se for remoto).' ;;
        gemini_apikey) printf '%s' $'  - \'Use Gemini API Key\': coloque GEMINI_API_KEY em {1}/.env e saia daqui.' ;;
        gemini_recreate) printf '%s' $'  Após editar {1}/.env, execute docker compose up -d para recriar o contêiner.' ;;
        gemini_reauth) printf '%s' $'Para autenticar de novo depois, digite /auth dentro da sessão do gemini.' ;;
    esac
}

login_text_ru() {
    case "$1" in
        err_slot_name) printf '%s' $'Недопустимое имя слота Claude; slots зарезервировано.' ;;
        summary_title) printf '%s' $'Итоги входа:' ;;
        summary_ok) printf '%s' $'OK' ;;
        summary_failed) printf '%s' $'Ошибка' ;;
        slots_title) printf '%s' $'=== Слоты Claude Code ===' ;;
        no_slots) printf '%s' $'  (слоты не найдены — запусти ./scripts/login.sh claude, чтобы создать слот по умолчанию)' ;;
        state_auth) printf '%s' $'выполнен вход' ;;
        state_not) printf '%s' $'вход не выполнен' ;;
        default_marker) printf '%s' $'(по умолчанию)' ;;
        unknown_provider) printf '%s' $'Неизвестный провайдер: {1}' ;;
        login_help_title) printf '%s' $'Вход в CLI-провайдеры внутри контейнера ductor-bot.' ;;
        usage) printf '%s' $'Использование:' ;;
        h_login_all) printf '%s' $'войти во все доступные провайдеры' ;;
        h_login_claude) printf '%s' $'войти в слот Claude Code по умолчанию' ;;
        h_login_claude_slot) printf '%s' $'войти в дополнительный слот Claude (например, alt, work)' ;;
        h_login_list) printf '%s' $'показать настроенные слоты Claude + состояние входа' ;;
        h_repair) printf '%s' $'заново поделиться настройками/skills/плагинами из слота по умолчанию с каждым alt-слотом' ;;
        h_login_codex) printf '%s' $'войти только в Codex' ;;
        h_login_gemini) printf '%s' $'войти только в Gemini' ;;
        h_login_grok) printf '%s' $'войти только в Grok' ;;
        h_help) printf '%s' $'показать эту справку' ;;
        h_multi_title) printf '%s' $'Claude с несколькими аккаунтами (резервные подписки):' ;;
        h_step1) printf '%s' $'первый аккаунт Max/Pro → слот по умолчанию' ;;
        h_step2) printf '%s' $'второй аккаунт → слот "claude-alt"' ;;
        h_step3) printf '%s' $'добавь в {1}/config/config.json:' ;;
        h_step4) printf '%s' $'ductor сам переключается на "claude-alt", когда "claude" достигает 5-часового лимита.' ;;
        h_share) printf '%s' $'У каждого слота своя папка в {1}/.cli-auth/, но все слоты\nделят один projects/ (сессии) через символическую ссылку, чтобы сессии\nпродолжались при смене аккаунта.' ;;
        err_no_service) printf '%s' $'Ошибка: сервис \'{1}\' не запущен. Сначала выполни \'docker compose up -d\'.' ;;
        err_shared_notdir) printf '%s' $'Ошибка: \'{1}\' существует и не является папкой.\n        Убери его в сторону и снова запусти ./scripts/login.sh {2}.' ;;
        err_both) printf '%s' $'Ошибка: и \'{1}\' (настоящая папка), и \'{2}\'\n        уже содержат данные сессий. Автоматическое слияние отклонено.\n        Объедини две папки вручную и снова запусти login.sh.' ;;
        migrating) printf '%s' $'Перенос старых сессий: {1} → {2}' ;;
        note_untouched) printf '%s' $'Примечание: \'{1}\' существует и не является ссылкой или папкой — оставлено без изменений.' ;;
        tip_fallback) printf '%s' $'Совет: добавь \'claude_accounts\' в config.json, чтобы включить автопереключение.' ;;
        multi_detected) printf '%s' $'=== Обнаружено несколько слотов Claude (вход выполнен: {1}) ===' ;;
        cfg_head) printf '%s' $'config.json claude_accounts:' ;;
        cfg_current) printf '%s' $'  сейчас:      {1}' ;;
        cfg_proposed) printf '%s' $'  предложено:  {1}' ;;
        cfg_empty) printf '%s' $'(пусто)' ;;
        no_tty) printf '%s' $'(stdin не является TTY — обновление по умолчанию применяется автоматически)' ;;
        prompt_update) printf '%s' $'Обновить config.json предложенным списком? [Y/n] ' ;;
        updated) printf '%s' $'Обновлено. Перезапусти бота, чтобы применить:' ;;
        skipped) printf '%s' $'Пропущено. Позже можно изменить config.json вручную.' ;;
        stale) printf '%s' $'  заменяю устаревшую ссылку: {1}' ;;
        migrating_entry) printf '%s' $'  {1} становится ссылкой (резервная копия в .pre-share-backup/{2}/)' ;;
        login_default) printf '%s' $'=== Вход в Claude Code (слот по умолчанию: claude) ===' ;;
        login_slot) printf '%s' $'=== Вход в Claude Code (слот: {1}) ===' ;;
        warn_nocreds) printf '%s' $'Предупреждение: вход завершён, но в {1} не найден credentials.json.\n                Запусти снова и пройди OAuth в браузере до конца.' ;;
        warn_provider_nocreds) printf '%s' $'Предупреждение: вход завершён, но в {1} не найдены учётные данные. Запусти снова и заверши вход.' ;;
        ok_slot) printf '%s' $'OK: вход в слот \'{1}\' выполнен.' ;;
        repairing) printf '%s' $'=== Восстановление слота: {1} ===' ;;
        none_alt) printf '%s' $'В {1} нет alt-слотов.' ;;
        repaired) printf '%s' $'Восстановлено слотов: {1}.' ;;
        codex_login) printf '%s' $'=== Вход в Codex через устройство ===' ;;
        gemini_login) printf '%s' $'=== Вход в Gemini CLI ===' ;;
        gemini_launch) printf '%s' $'Запуск Gemini CLI. При первом запуске он спросит способ входа.' ;;
        gemini_google) printf '%s' $'  - \'Login with Google\': OAuth через loopback (для удалённой машины нужен проброс порта SSH).' ;;
        gemini_apikey) printf '%s' $'  - \'Use Gemini API Key\': положи GEMINI_API_KEY в {1}/.env и выйди отсюда.' ;;
        gemini_recreate) printf '%s' $'  После изменения {1}/.env выполни docker compose up -d для пересоздания контейнера.' ;;
        gemini_reauth) printf '%s' $'Чтобы войти заново позже, введи /auth внутри сессии gemini.' ;;
    esac
}

# hangul-allowed-begin: Korean messages of login.sh

login_text_ko() {
    case "$1" in
        err_slot_name) printf '%s' $'잘못된 Claude 슬롯 이름입니다. slots는 예약어입니다.' ;;
        summary_title) printf '%s' $'로그인 요약:' ;;
        summary_ok) printf '%s' $'OK' ;;
        summary_failed) printf '%s' $'실패' ;;
        slots_title) printf '%s' $'=== Claude Code 슬롯 ===' ;;
        no_slots) printf '%s' $'  (슬롯이 없어요 — ./scripts/login.sh claude 를 실행해 기본 슬롯을 만드세요)' ;;
        state_auth) printf '%s' $'인증됨' ;;
        state_not) printf '%s' $'인증 안 됨' ;;
        default_marker) printf '%s' $'(기본)' ;;
        unknown_provider) printf '%s' $'알 수 없는 제공자: {1}' ;;
        login_help_title) printf '%s' $'ductor-bot 컨테이너 안의 CLI 제공자에 로그인해요.' ;;
        usage) printf '%s' $'사용법:' ;;
        h_login_all) printf '%s' $'사용 가능한 모든 제공자에 로그인' ;;
        h_login_claude) printf '%s' $'Claude Code 기본 슬롯에 로그인' ;;
        h_login_claude_slot) printf '%s' $'추가 Claude 슬롯에 로그인 (예: alt, work)' ;;
        h_login_list) printf '%s' $'설정된 Claude 슬롯과 인증 상태 표시' ;;
        h_repair) printf '%s' $'기본 슬롯의 설정/스킬/플러그인을 모든 alt 슬롯에 다시 공유' ;;
        h_login_codex) printf '%s' $'Codex에만 로그인' ;;
        h_login_gemini) printf '%s' $'Gemini에만 로그인' ;;
        h_login_grok) printf '%s' $'Grok에만 로그인' ;;
        h_help) printf '%s' $'이 도움말 표시' ;;
        h_multi_title) printf '%s' $'Claude 다중 계정 (백업 구독):' ;;
        h_step1) printf '%s' $'첫 번째 Max/Pro 계정 → 기본 슬롯' ;;
        h_step2) printf '%s' $'두 번째 계정 → "claude-alt" 슬롯' ;;
        h_step3) printf '%s' $'{1}/config/config.json 에 추가:' ;;
        h_step4) printf '%s' $'"claude"가 5시간 한도에 도달하면 ductor가 자동으로 "claude-alt"로 넘어가요.' ;;
        h_share) printf '%s' $'슬롯마다 {1}/.cli-auth/ 아래에 자기 디렉터리가 있지만, 모든 슬롯이\n심볼릭 링크로 같은 projects/(세션)를 공유해서 계정을 바꿔도\n세션이 이어져요.' ;;
        err_no_service) printf '%s' $'오류: \'{1}\' 서비스가 실행 중이 아니에요. 먼저 \'docker compose up -d\'를 실행하세요.' ;;
        err_shared_notdir) printf '%s' $'오류: \'{1}\'이(가) 있지만 디렉터리가 아니에요.\n      다른 곳으로 옮기고 ./scripts/login.sh {2} 를 다시 실행하세요.' ;;
        err_both) printf '%s' $'오류: \'{1}\'(실제 디렉터리)과 \'{2}\' 모두\n      이미 세션 데이터가 있어요. 자동 병합은 하지 않아요.\n      두 디렉터리를 직접 합친 뒤 login.sh를 다시 실행하세요.' ;;
        migrating) printf '%s' $'이전 세션을 옮기는 중: {1} → {2}' ;;
        note_untouched) printf '%s' $'참고: \'{1}\'이(가) 있지만 심볼릭 링크나 디렉터리가 아니에요 — 그대로 둬요.' ;;
        tip_fallback) printf '%s' $'팁: 자동 전환을 켜려면 config.json에 \'claude_accounts\'를 추가하세요.' ;;
        multi_detected) printf '%s' $'=== Claude 다중 슬롯 감지 (인증됨 {1}개) ===' ;;
        cfg_head) printf '%s' $'config.json claude_accounts:' ;;
        cfg_current) printf '%s' $'  현재: {1}' ;;
        cfg_proposed) printf '%s' $'  제안: {1}' ;;
        cfg_empty) printf '%s' $'(비어 있음)' ;;
        no_tty) printf '%s' $'(stdin이 TTY가 아니에요 — 기본값에 따라 자동으로 업데이트해요)' ;;
        prompt_update) printf '%s' $'제안한 목록으로 config.json을 업데이트할까요? [Y/n] ' ;;
        updated) printf '%s' $'업데이트했어요. 적용하려면 봇을 다시 시작하세요:' ;;
        skipped) printf '%s' $'건너뛰었어요. 나중에 config.json을 직접 수정해도 돼요.' ;;
        stale) printf '%s' $'  오래된 심볼릭 링크를 바꾸는 중: {1}' ;;
        migrating_entry) printf '%s' $'  {1}을(를) 심볼릭 링크로 바꾸는 중 (백업: .pre-share-backup/{2}/)' ;;
        login_default) printf '%s' $'=== Claude Code 로그인 (기본 슬롯: claude) ===' ;;
        login_slot) printf '%s' $'=== Claude Code 로그인 (슬롯: {1}) ===' ;;
        warn_nocreds) printf '%s' $'경고: 로그인은 끝났지만 {1}에서 credentials.json을 찾지 못했어요.\n      다시 실행해서 브라우저에서 OAuth 과정을 끝내세요.' ;;
        warn_provider_nocreds) printf '%s' $'경고: 로그인은 끝났지만 {1}에서 인증 정보를 찾지 못했어요. 다시 실행해서 로그인을 완료하세요.' ;;
        ok_slot) printf '%s' $'OK: \'{1}\' 슬롯이 인증됐어요.' ;;
        repairing) printf '%s' $'=== 슬롯 복구 중: {1} ===' ;;
        none_alt) printf '%s' $'{1} 아래에 alt 슬롯이 없어요.' ;;
        repaired) printf '%s' $'슬롯 {1}개를 복구했어요.' ;;
        codex_login) printf '%s' $'=== Codex 기기 인증 로그인 ===' ;;
        gemini_login) printf '%s' $'=== Gemini CLI 로그인 ===' ;;
        gemini_launch) printf '%s' $'Gemini CLI를 실행해요. 처음 실행하면 인증 방법을 물어봐요.' ;;
        gemini_google) printf '%s' $'  - \'Login with Google\': 루프백 OAuth (원격이면 SSH 포트 포워딩이 필요해요).' ;;
        gemini_apikey) printf '%s' $'  - \'Use Gemini API Key\': {1}/.env 에 GEMINI_API_KEY를 넣고 여기서 나가세요.' ;;
        gemini_recreate) printf '%s' $'  {1}/.env \xec\x88\x98\xec\xa0\x95 \xeb\x92\xa4 docker compose up -d\xeb\xa5\xbc \xec\x8b\xa4\xed\x96\x89\xed\x95\xb4 \xec\xbb\xa8\xed\x85\x8c\xec\x9d\xb4\xeb\x84\x88\xeb\xa5\xbc \xeb\x8b\xa4\xec\x8b\x9c \xeb\xa7\x8c\xeb\x93\x9c\xec\x84\xb8\xec\x9a\x94.' ;;
        gemini_reauth) printf '%s' $'나중에 다시 인증하려면 gemini 세션 안에서 /auth 를 입력하세요.' ;;
    esac
}

# hangul-allowed-end

show_help() {
    say login_help_title
    echo
    say usage
    printf '%s# %s\n' "  ./scripts/login.sh                        " "$(say h_login_all)"
    printf '%s# %s\n' "  ./scripts/login.sh claude                 " "$(say h_login_claude)"
    printf '%s# %s\n' "  ./scripts/login.sh claude <name>          " "$(say h_login_claude_slot)"
    printf '%s# %s\n' "  ./scripts/login.sh claude --list          " "$(say h_login_list)"
    printf '%s# %s\n' "  ./scripts/login.sh claude --repair        " "$(say h_repair)"
    printf '%s# %s\n' "  ./scripts/login.sh codex                  " "$(say h_login_codex)"
    printf '%s# %s\n' "  ./scripts/login.sh gemini                 " "$(say h_login_gemini)"
    printf '%s# %s\n' "  ./scripts/login.sh grok                   " "$(say h_login_grok)"
    printf '%s# %s\n' "  ./scripts/login.sh -h | --help            " "$(say h_help)"
    echo
    say h_multi_title
    echo
    printf '%s# %s\n' "  1. ./scripts/login.sh claude              " "$(say h_step1)"
    printf '%s# %s\n' "  2. ./scripts/login.sh claude alt          " "$(say h_step2)"
    printf '  3. %s\n' "$(say h_step3 '${DUCTOR_HOME:-~/.ductor}')"
    echo '       "claude_accounts": ["claude", "claude-alt"]'
    printf '  4. %s\n' "$(say h_step4)"
    echo
    say h_share '${DUCTOR_HOME}'
}

ensure_container() {
    if [[ -z $(dc ps -q "$SERVICE" 2>/dev/null) ]]; then
        say_err err_no_service "$SERVICE"
        exit 1
    fi
}

ensure_shared_projects() {
    mkdir -p "$SHARED_PROJECTS_HOST" || return 1
}

ensure_slot_dir() {
    # $1: slot name (e.g. "claude" or "claude-alt")
    #
    # Guarantees that ${dir}/projects is a symlink to ../_shared-projects.
    # For pre-existing installs where ${dir}/projects is a real directory
    # (single-slot legacy state), promote it to _shared-projects/ on first
    # multi-slot login so every slot keeps session continuity.
    local slot="$1"
    local dir="${AUTH_DIR_HOST}/${slot}"
    mkdir -p "$dir" || return 1

    local projects_link="${dir}/projects"

    if [ -L "$projects_link" ]; then
        return 0
    fi

    if [ -d "$projects_link" ]; then
        if [ -e "$SHARED_PROJECTS_HOST" ] && [ ! -d "$SHARED_PROJECTS_HOST" ]; then
            say_err err_shared_notdir "$SHARED_PROJECTS_HOST" "$slot"
            exit 1
        fi
        if [ -d "$SHARED_PROJECTS_HOST" ] && [ -n "$(ls -A "$SHARED_PROJECTS_HOST" 2>/dev/null)" ]; then
            say_err err_both "$projects_link" "$SHARED_PROJECTS_HOST"
            exit 1
        fi
        say_err migrating "$projects_link" "$SHARED_PROJECTS_HOST"
        # If _shared-projects already exists but is empty, remove the empty
        # dir so `mv` can relocate projects/ into its place atomically.
        if [ -d "$SHARED_PROJECTS_HOST" ]; then
            rmdir "$SHARED_PROJECTS_HOST" || return 1
        fi
        mv "$projects_link" "$SHARED_PROJECTS_HOST" || return 1
        ln -s "../_shared-projects" "$projects_link" || return 1
        return 0
    fi

    if [ -e "$projects_link" ]; then
        say_err note_untouched "$projects_link"
        return 0
    fi

    ln -s "../_shared-projects" "$projects_link" || return 1
}

slot_auth_state() {
    # $1: slot dir; echoes "authenticated" or "not authenticated"
    local dir="$1"
    if [ -f "${dir}/credentials.json" ] || [ -f "${dir}/.credentials.json" ]; then
        echo "authenticated"
    else
        echo "not authenticated"
    fi
}

slot_state_label() {
    # $1: the internal state from slot_auth_state; the internal value is never translated.
    case "$1" in
        authenticated) say state_auth ;;
        *) say state_not ;;
    esac
}

resolve_claude_slot_name() {
    # $1: optional short name. Returns full slot name.
    # Empty / "default" / "claude" → "claude". Otherwise → "claude-<name>".
    local name="${1:-}" slot
    case "$name" in
        ""|default|claude)  slot="claude" ;;
        claude-*)           slot="$name" ;;
        *)                  slot="claude-$name" ;;
    esac
    if [[ ! "$slot" =~ ^claude(-[a-z0-9][a-z0-9_-]*)?$ || "$slot" = claude-slots ]]; then
        say_err err_slot_name
        return 2
    fi
    printf '%s\n' "$slot"
}

list_claude_slots() {
    ensure_shared_projects || return 1
    say slots_title
    if [ ! -d "$AUTH_DIR_HOST" ]; then
        say no_slots
        return 0
    fi
    local found=0
    for dir in "${AUTH_DIR_HOST}"/claude "${AUTH_DIR_HOST}"/claude-*; do
        [ -d "$dir" ] || continue
        [ "${dir##*/}" != "claude-slots" ] || continue
        found=1
        local slot
        slot="$(basename "$dir")"
        local state
        state="$(slot_state_label "$(slot_auth_state "$dir")")"
        if [ "$slot" = "claude" ]; then
            printf "  %-18s %-18s %s\n" "$slot" "$state" "$(say default_marker)"
        else
            printf "  %-18s %s\n" "$slot" "$state"
        fi
    done
    if [ "$found" -eq 0 ]; then
        say no_slots
    fi
    echo
    say tip_fallback
}

list_authenticated_claude_slots() {
    # Echo authenticated claude slots, one per line.
    # Order: "claude" first (if authed), then other slots alphabetically.
    [ -d "$AUTH_DIR_HOST" ] || return 0
    local dir
    if [ -d "${AUTH_DIR_HOST}/claude" ] && \
       [ "$(slot_auth_state "${AUTH_DIR_HOST}/claude")" = "authenticated" ]; then
        echo "claude"
    fi
    for dir in "${AUTH_DIR_HOST}"/claude-*; do
        [ -d "$dir" ] || continue
        [ "${dir##*/}" != "claude-slots" ] || continue
        if [ "$(slot_auth_state "$dir")" = "authenticated" ]; then
            basename "$dir"
        fi
    done | sort
}

maybe_update_claude_accounts() {
    # After a successful login, if 2+ claude slots are authenticated and the
    # config.json claude_accounts list does not already include them, prompt
    # the user to update it.
    local config="${DUCTOR_HOME_HOST}/config/config.json"
    [ -f "$config" ] || return 0

    local auth_list auth_count
    auth_list="$(list_authenticated_claude_slots || true)"
    auth_count="$(printf '%s\n' "$auth_list" | awk 'NF{c++} END{print c+0}')"
    [ "$auth_count" -ge 2 ] || return 0

    # Let Python compute the proposed list. Exit 0 = update needed,
    # 2 = already in sync, other = error / skip silently.
    local proposed_json rc=0
    proposed_json="$(AUTH_LIST="$auth_list" python3 - "$config" <<'PY'
import json, os, sys
cfg_path = sys.argv[1]
auth = [s for s in os.environ.get("AUTH_LIST", "").splitlines() if s]
try:
    with open(cfg_path) as f:
        cfg = json.load(f)
except Exception:
    sys.exit(1)
current = list(cfg.get("claude_accounts") or [])
merged = list(current)
for s in auth:
    if s not in merged:
        merged.append(s)
if merged == current:
    sys.exit(2)
print(json.dumps({"current": current, "proposed": merged}))
PY
    )" || rc=$?
    if [ "$rc" -ne 0 ] || [ -z "$proposed_json" ]; then
        return 0
    fi

    local current_fmt proposed_fmt
    current_fmt="$(printf '%s' "$proposed_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(", ".join(d["current"]))')"
    proposed_fmt="$(printf '%s' "$proposed_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(", ".join(d["proposed"]))')"

    [ -n "$current_fmt" ] || current_fmt="$(say cfg_empty)"

    echo
    say multi_detected "$auth_count"
    say cfg_head
    say cfg_current "$current_fmt"
    say cfg_proposed "$proposed_fmt"
    local reply=""
    if [ ! -t 0 ]; then
        say no_tty
    else
        read -r -p "$(host_text login prompt_update)" reply
    fi
    case "$reply" in
        ""|y|Y|yes|YES)
            PROPOSED_JSON="$proposed_json" python3 - "$config" <<'PY' || return $?
import fcntl, json, os, sys, tempfile
proposal = json.loads(os.environ["PROPOSED_JSON"])
additions = [slot for slot in proposal["proposed"] if slot not in proposal["current"]]
cfg_path = sys.argv[1]
lock_path = os.path.join(os.path.dirname(cfg_path), ".config.json.lock")
with open(lock_path, "a") as config_lock:
    fcntl.flock(config_lock, fcntl.LOCK_EX)
    with open(cfg_path) as f:
        cfg = json.load(f)
    accounts = list(cfg.get("claude_accounts") or [])
    for slot in additions:
        if slot not in accounts:
            accounts.append(slot)
    cfg["claude_accounts"] = accounts
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cfg_path), prefix=".config.json.")
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), os.stat(cfg_path).st_mode & 0o777)
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, cfg_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
PY
            say updated
            echo "  touch ${DUCTOR_HOME_HOST}/restart-requested"
            ;;
        *)
            say skipped
            ;;
    esac
}

# Files under a slot directory that MUST stay per-slot (real files, never
# symlinked).  Everything else is shared with the default slot to keep user
# configuration (settings, skills, plugins, sessions, cache, history) unified
# across accounts.
SLOT_CREDENTIAL_FILES=(".credentials.json" "credentials.json" ".claude.json")

_is_slot_credential() {
    # $1: filename (basename)
    local name="$1" c
    for c in "${SLOT_CREDENTIAL_FILES[@]}"; do
        [ "$name" = "$c" ] && return 0
    done
    return 1
}

_link_alt_to_default() {
    # $1: alt slot dir, $2: entry name in default slot.
    # Ensures alt_dir/<name> is a symlink to ../claude/<name>.
    # Migrates existing real entries to a timestamped backup inside alt_dir.
    local alt_dir="$1" name="$2"
    local target="../claude/${name}"
    local link="${alt_dir}/${name}"

    if [ -L "$link" ]; then
        local current
        current="$(readlink "$link")"
        if [ "$current" = "$target" ]; then
            return 0
        fi
        say stale "$(basename "$alt_dir")/$name"
        rm "$link" || return 1
        ln -s "$target" "$link" || return 1
        return 0
    fi

    if [ -e "$link" ]; then
        local backup_root="${alt_dir}/.pre-share-backup"
        local ts
        ts="$(date +%Y%m%dT%H%M%S)"
        mkdir -p "${backup_root}/${ts}" || return 1
        say migrating_entry "$(basename "$alt_dir")/$name" "$ts"
        mv "$link" "${backup_root}/${ts}/" || return 1
        ln -s "$target" "$link" || return 1
        return 0
    fi

    ln -s "$target" "$link" || return 1
}

share_slot_from_default() {
    # $1: alt slot name (e.g. "claude-main").
    # For every non-credential entry under the default slot, ensure this alt
    # slot has a symlink pointing back.  This keeps settings/skills/plugins/
    # sessions/etc. unified across accounts while credentials stay per-slot.
    local slot="$1"
    [ "$slot" = "claude" ] && return 0

    local default_dir="${AUTH_DIR_HOST}/claude"
    local alt_dir="${AUTH_DIR_HOST}/${slot}"
    [ -d "$default_dir" ] || return 0
    [ -d "$alt_dir" ] || return 0

    shopt -s dotglob nullglob
    local entry name
    for entry in "$default_dir"/*; do
        name="$(basename "$entry")"
        if _is_slot_credential "$name"; then
            continue
        fi
        if [ "$name" = "projects" ]; then
            # Sessions continuity: projects is owned by ensure_slot_dir
            # via the _shared-projects symlink.
            continue
        fi
        if ! _link_alt_to_default "$alt_dir" "$name"; then
            shopt -u dotglob nullglob
            return 1
        fi
    done
    shopt -u dotglob nullglob
}

login_claude_slot() {
    # $1: slot name (full, e.g. "claude" or "claude-alt")
    local slot="$1"
    ensure_container
    ensure_shared_projects || return 1
    ensure_slot_dir "$slot" || return 1

    local slot_container_path="/ductor/.cli-auth/${slot}"

    if [ "$slot" = "claude" ]; then
        say login_default
        dc exec -e "CLAUDE_CONFIG_DIR=${slot_container_path}" "$SERVICE" claude /login || true
    else
        say login_slot "$slot"
        echo "CLAUDE_CONFIG_DIR=${slot_container_path}"
        dc exec \
            -e "CLAUDE_CONFIG_DIR=${slot_container_path}" \
            "$SERVICE" claude /login || true
    fi

    local host_dir="${AUTH_DIR_HOST}/${slot}"
    if [ ! -f "${host_dir}/credentials.json" ] && [ ! -f "${host_dir}/.credentials.json" ]; then
        say_err warn_nocreds "$host_dir"
        return 1
    fi
    say ok_slot "$slot"
    if [ "$slot" != "claude" ]; then
        share_slot_from_default "$slot" || return 1
    fi
    maybe_update_claude_accounts
}

login_claude_dispatch() {
    # $1: optional slot short name OR "--list"
    if [ $# -eq 0 ]; then
        login_claude_slot "claude"
        return
    fi
    case "$1" in
        --list|-l|list)
            list_claude_slots
            ;;
        --repair|repair)
            ensure_shared_projects || return 1
            local repaired=0 dir slot
            for dir in "${AUTH_DIR_HOST}"/claude-*; do
                [ -d "$dir" ] || continue
                [ "${dir##*/}" != "claude-slots" ] || continue
                slot="$(basename "$dir")"
                say repairing "$slot"
                ensure_slot_dir "$slot" || return 1
                share_slot_from_default "$slot" || return 1
                repaired=$((repaired + 1))
            done
            if [ "$repaired" -eq 0 ]; then
                say none_alt "$AUTH_DIR_HOST"
            else
                say repaired "$repaired"
            fi
            ;;
        -h|--help)
            show_help
            ;;
        *)
            local slot
            slot="$(resolve_claude_slot_name "$1")" || return $?
            login_claude_slot "$slot"
            ;;
    esac
}

check_provider_credentials() {
    local provider="$1" filename="$2"
    local host_dir="${AUTH_DIR_HOST}/${provider}"
    if [ ! -f "${host_dir}/${filename}" ]; then
        say_err warn_provider_nocreds "$host_dir"
        return 1
    fi
    say ok_slot "$provider"
}

login_codex() {
    ensure_container
    say codex_login
    dc exec "$SERVICE" codex login --device-auth || true
    check_provider_credentials codex auth.json
}

login_gemini() {
    say gemini_login
    say gemini_launch
    say gemini_google
    say gemini_apikey "$DUCTOR_HOME_HOST"
    say gemini_recreate "$DUCTOR_HOME_HOST"
    say gemini_reauth
    echo ""
    dc exec "$SERVICE" gemini || true
    if dc exec -T "$SERVICE" sh -c '[ -n "${GEMINI_API_KEY:-}${GOOGLE_API_KEY:-}" ]'; then
        say ok_slot gemini
        return 0
    fi
    check_provider_credentials gemini oauth_creds.json
}

login_grok() {
    ensure_container
    dc exec "$SERVICE" grok login --device-auth || true
    check_provider_credentials grok auth.json
}

# Only run the dispatch when executed directly. Tests (and other tools) can
# `source` this file to exercise the helper functions in isolation.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    if [ $# -eq 0 ]; then
        ensure_container
        failed_providers=()
        login_claude_slot "claude" || failed_providers+=(claude)
        echo ""
        login_codex || failed_providers+=(codex)
        echo ""
        login_gemini || failed_providers+=(gemini)
        echo ""
        login_grok || failed_providers+=(grok)
        echo ""
        say summary_title
        for provider in claude codex gemini grok; do
            case " ${failed_providers[*]} " in
                *" $provider "*) summary="$(say summary_failed)" ;;
                *) summary="$(say summary_ok)" ;;
            esac
            printf '  %s: %s\n' "$provider" "$summary"
        done
        if [ "${#failed_providers[@]}" -gt 0 ]; then
            exit 1
        fi
        exit 0
    fi

    case "$1" in
        -h|--help|help)
            show_help
            ;;
        claude)
            shift
            login_claude_dispatch "$@"
            ;;
        codex)
            login_codex
            ;;
        gemini)
            login_gemini
            ;;
        grok)
            login_grok
            ;;
        *)
            say unknown_provider "$1"
            echo
            show_help
            exit 1
            ;;
    esac
fi

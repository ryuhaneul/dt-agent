#!/usr/bin/env bash
# Logout from CLI providers inside the ductor-bot container.
#
# Usage:
#   ./scripts/logout.sh                            # logout from all providers (default slots)
#   ./scripts/logout.sh claude                     # default slot logout (claude auth logout)
#   ./scripts/logout.sh claude --list              # list slots: auth state + priority in config
#   ./scripts/logout.sh claude --disable <slot>    # remove from config claude_accounts (creds preserved)
#   ./scripts/logout.sh claude --remove  <slot>    # remove from config + rename creds to .removed-<ts>
#   ./scripts/logout.sh claude <slot>              # alias for --remove <slot>
#   ./scripts/logout.sh codex                      # logout from Codex only
#   ./scripts/logout.sh gemini                     # logout from Gemini only
#   ./scripts/logout.sh grok                       # logout from Grok only
#   ./scripts/logout.sh -h | --help                # show this help
#
# claude_accounts is hot-reloaded by ConfigReloader (5s poll) and re-read
# per CLI call (claude_provider.py / service.py). --disable / --remove
# therefore take effect on the next call without a bot restart, and any
# in-flight call finishes on its existing slot list.

set -euo pipefail

CONTAINER="${DUCTOR_CONTAINER:-ductor-bot}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_home.sh"
DUCTOR_HOME_HOST="$(resolve_ductor_home "$(dirname "$SCRIPT_DIR")")"
AUTH_DIR_HOST="${DUCTOR_HOME_HOST}/.cli-auth"
CONFIG_HOST="${DUCTOR_HOME_HOST}/config/config.json"

# Messages for a person are in the language of scripts/_home.sh (stored setting, locale, else
# English). Commands, options, paths, provider names and the internal slot state stay as they are.
HOST_LANGUAGE="$(host_language "" "$DUCTOR_HOME_HOST")"
say() { printf '%s\n' "$(host_text logout "$@")"; }
say_err() { say "$@" >&2; }

logout_text_en() {
    case "$1" in
        err_slot_name) printf '%s' $'Invalid Claude slot name; slots is reserved.' ;;
        err_container_stopped) printf '%s' $'Error: container {1} is not running. Start it before retrying.' ;;
        h_out_prefer) printf '%s' $'prefer this slot' ;;
        h_out_priority) printf '%s' $'set the complete slot order' ;;
        h_out_clear) printf '%s' $'clear one or all slot cooldowns' ;;
        cooldown_label) printf '%s' $'cooldown' ;;
        cooldown_none) printf '%s' $'none' ;;
        cooldown_remaining) printf '%s' $'{1}h {2}m' ;;
        slot_error_unknown_slot) printf '%s' $'Unknown slot.' ;;
        slot_error_invalid_priority) printf '%s' $'Priority must contain every configured slot exactly once.' ;;
        slot_error_invalid_delta) printf '%s' $'Invalid move direction.' ;;
        slot_error_invalid_arguments) printf '%s' $'Invalid arguments.' ;;
        slot_apply_notice) printf '%s' $'Priority changes apply to main within seconds and to sub-agents on their next start; cooldown clearing applies to all on the next call.' ;;
        slots_title) printf '%s' $'=== Claude Code Slots ===' ;;
        no_slots) printf '%s' $'  (no slots found — run ./scripts/login.sh claude to create the default)' ;;
        state_auth) printf '%s' $'authenticated' ;;
        state_not) printf '%s' $'not authenticated' ;;
        default_marker) printf '%s' $'(default)' ;;
        unknown_provider) printf '%s' $'Unknown provider: {1}' ;;
        usage) printf '%s' $'Usage:' ;;
        logout_help_title) printf '%s' $'Logout from CLI providers inside the ductor-bot container.' ;;
        h_out_all) printf '%s' $'logout from all providers (default slots)' ;;
        h_out_claude) printf '%s' $'default slot logout (claude auth logout)' ;;
        h_out_list) printf '%s' $'list slots: auth state + priority in config' ;;
        h_out_disable) printf '%s' $'remove from config claude_accounts (creds preserved)' ;;
        h_out_remove) printf '%s' $'remove from config + rename creds to .removed-<ts>' ;;
        h_out_alias) printf '%s' $'alias for --remove <slot>' ;;
        h_out_codex) printf '%s' $'logout from Codex only' ;;
        h_out_gemini) printf '%s' $'logout from Gemini only' ;;
        h_out_grok) printf '%s' $'logout from Grok only' ;;
        h_help) printf '%s' $'show this help' ;;
        h_out_note) printf '%s' $'claude_accounts is hot-reloaded by ConfigReloader (5s poll) and re-read\nper CLI call (claude_provider.py / service.py). --disable / --remove\ntherefore take effect on the next call without a bot restart, and any\nin-flight call finishes on its existing slot list.' ;;
        err_container) printf '%s' $'Error: container \'{1}\' not found.' ;;
        priority_label) printf '%s' $'priority' ;;
        config_line) printf '%s' $'config: {1}' ;;
        accounts_list) printf '%s' $'  claude_accounts = [{1}]' ;;
        accounts_empty) printf '%s' $'  claude_accounts = (empty — single-slot behavior)' ;;
        tip_priority) printf '%s' $'Tip: priority=\'-\' means the slot exists on disk but is not in claude_accounts.' ;;
        err_cfg_missing) printf '%s' $'Error: config.json not found at {1}' ;;
        slot_nochange) printf '%s' $'  (slot \'{1}\' not in claude_accounts; no config change)' ;;
        slot_changed) printf '%s' $'  config.json: claude_accounts {1} -> {2}' ;;
        warn_empty) printf '%s' $'  WARNING: claude_accounts is now empty (slot rotation disabled).' ;;
        no_slot_dir) printf '%s' $'  (slot dir \'{1}\' does not exist; nothing to invalidate)' ;;
        no_creds_found) printf '%s' $'  (no credentials.json found in {1})' ;;
        logout_active) printf '%s' $'=== Claude Code Logout (default slot) ===' ;;
        disable_title) printf '%s' $'=== Disable slot \'{1}\' (config-only, credentials preserved) ===' ;;
        remove_title) printf '%s' $'=== Remove slot \'{1}\' (config + credentials) ===' ;;
        done_reload) printf '%s' $'Done. ConfigReloader picks this up within ~5s; in-flight calls\nfinish on their existing slot list.' ;;
        to_reenable) printf '%s' $'To re-enable: edit {1} claude_accounts to include \'{2}\' again,\nor run {3} (a successful login proposes adding it back).' ;;
        to_restore) printf '%s' $'To restore: {1} to re-authenticate.' ;;
        codex_logout) printf '%s' $'=== Codex Logout ===' ;;
        codex_cleared) printf '%s' $'Codex logged out. Sessions and configuration are kept.' ;;
        gemini_logout) printf '%s' $'=== Gemini CLI Logout ===' ;;
        gemini_remove_key) printf '%s' $'Remove GEMINI_API_KEY / GOOGLE_API_KEY from {1}/.env' ;;
        gemini_restart) printf '%s' $'Then restart: {1}' ;;
        grok_logout) printf '%s' $'=== Grok Logout ===' ;;
        grok_cleared) printf '%s' $'Grok auth cleared.' ;;
        err_disable_req) printf '%s' $'Error: --disable requires a slot name' ;;
        err_remove_req) printf '%s' $'Error: --remove requires a slot name' ;;
        unknown_flag) printf '%s' $'Unknown flag: {1}' ;;
    esac
}

logout_text_de() {
    case "$1" in
        err_slot_name) printf '%s' $'Ungültiger Claude-Slotname; slots ist reserviert.' ;;
        err_container_stopped) printf '%s' $'Fehler: Container {1} läuft nicht. Vor dem erneuten Versuch starten.' ;;
        h_out_prefer) printf '%s' $'diesen Slot bevorzugen' ;;
        h_out_priority) printf '%s' $'vollständige Slot-Reihenfolge setzen' ;;
        h_out_clear) printf '%s' $'Cooldown eines oder aller Slots aufheben' ;;
        cooldown_label) printf '%s' $'Cooldown' ;;
        cooldown_none) printf '%s' $'keiner' ;;
        cooldown_remaining) printf '%s' $'{1} Std. {2} Min.' ;;
        slot_error_unknown_slot) printf '%s' $'Unbekannter Slot.' ;;
        slot_error_invalid_priority) printf '%s' $'Die Priorität muss jeden konfigurierten Slot genau einmal enthalten.' ;;
        slot_error_invalid_delta) printf '%s' $'Ungültige Verschieberichtung.' ;;
        slot_error_invalid_arguments) printf '%s' $'Ungültige Argumente.' ;;
        slot_apply_notice) printf '%s' $'Prioritäten gelten im Hauptagenten nach wenigen Sekunden und in Unteragenten beim nächsten Start; aufgehobene Cooldowns gelten für alle beim nächsten Aufruf.' ;;
        slots_title) printf '%s' $'=== Claude-Code-Slots ===' ;;
        no_slots) printf '%s' $'  (keine Slots gefunden — führe ./scripts/login.sh claude aus, um den Standard-Slot anzulegen)' ;;
        state_auth) printf '%s' $'angemeldet' ;;
        state_not) printf '%s' $'nicht angemeldet' ;;
        default_marker) printf '%s' $'(Standard)' ;;
        unknown_provider) printf '%s' $'Unbekannter Anbieter: {1}' ;;
        usage) printf '%s' $'Verwendung:' ;;
        logout_help_title) printf '%s' $'Von CLI-Anbietern im ductor-bot-Container abmelden.' ;;
        h_out_all) printf '%s' $'von allen Anbietern abmelden (Standard-Slots)' ;;
        h_out_claude) printf '%s' $'Standard-Slot abmelden (claude auth logout)' ;;
        h_out_list) printf '%s' $'Slots auflisten: Anmeldestatus + Priorität in der Konfiguration' ;;
        h_out_disable) printf '%s' $'aus claude_accounts entfernen (Zugangsdaten bleiben)' ;;
        h_out_remove) printf '%s' $'aus der Konfiguration entfernen + Zugangsdaten in .removed-<ts> umbenennen' ;;
        h_out_alias) printf '%s' $'Kurzform für --remove <slot>' ;;
        h_out_codex) printf '%s' $'nur von Codex abmelden' ;;
        h_out_gemini) printf '%s' $'nur von Gemini abmelden' ;;
        h_out_grok) printf '%s' $'nur von Grok abmelden' ;;
        h_help) printf '%s' $'diese Hilfe zeigen' ;;
        h_out_note) printf '%s' $'claude_accounts wird vom ConfigReloader neu geladen (5-s-Abfrage) und bei\njedem CLI-Aufruf neu gelesen (claude_provider.py / service.py). --disable / --remove\nwirken daher beim nächsten Aufruf ohne Neustart des Bots; ein laufender\nAufruf endet mit seiner bisherigen Slot-Liste.' ;;
        err_container) printf '%s' $'Fehler: Container \'{1}\' nicht gefunden.' ;;
        priority_label) printf '%s' $'Priorität' ;;
        config_line) printf '%s' $'Konfiguration: {1}' ;;
        accounts_list) printf '%s' $'  claude_accounts = [{1}]' ;;
        accounts_empty) printf '%s' $'  claude_accounts = (leer — Verhalten mit einem Slot)' ;;
        tip_priority) printf '%s' $'Tipp: Priorität=\'-\' heißt, der Slot liegt auf der Platte, ist aber nicht in claude_accounts.' ;;
        err_cfg_missing) printf '%s' $'Fehler: config.json nicht gefunden unter {1}' ;;
        slot_nochange) printf '%s' $'  (Slot \'{1}\' steht nicht in claude_accounts; Konfiguration unverändert)' ;;
        slot_changed) printf '%s' $'  config.json: claude_accounts {1} -> {2}' ;;
        warn_empty) printf '%s' $'  WARNUNG: claude_accounts ist jetzt leer (Slot-Wechsel deaktiviert).' ;;
        no_slot_dir) printf '%s' $'  (Slot-Verzeichnis \'{1}\' existiert nicht; nichts zu entwerten)' ;;
        no_creds_found) printf '%s' $'  (keine credentials.json in {1} gefunden)' ;;
        logout_active) printf '%s' $'=== Claude-Code-Abmeldung (Standard-Slot) ===' ;;
        disable_title) printf '%s' $'=== Slot \'{1}\' deaktivieren (nur Konfiguration, Zugangsdaten bleiben) ===' ;;
        remove_title) printf '%s' $'=== Slot \'{1}\' entfernen (Konfiguration + Zugangsdaten) ===' ;;
        done_reload) printf '%s' $'Fertig. Der ConfigReloader übernimmt das in ca. 5 s; laufende Aufrufe\nenden mit ihrer bisherigen Slot-Liste.' ;;
        to_reenable) printf '%s' $'Zum Reaktivieren: Trage \'{2}\' in {1} unter claude_accounts wieder ein,\noder führe {3} aus (eine erfolgreiche Anmeldung schlägt das Hinzufügen vor).' ;;
        to_restore) printf '%s' $'Zum Wiederherstellen: {1}, um dich neu anzumelden.' ;;
        codex_logout) printf '%s' $'=== Codex-Abmeldung ===' ;;
        codex_cleared) printf '%s' $'Von Codex abgemeldet. Sitzungen und Konfiguration bleiben erhalten.' ;;
        gemini_logout) printf '%s' $'=== Gemini-CLI-Abmeldung ===' ;;
        gemini_remove_key) printf '%s' $'Entferne GEMINI_API_KEY / GOOGLE_API_KEY aus {1}/.env' ;;
        gemini_restart) printf '%s' $'Starte dann neu: {1}' ;;
        grok_logout) printf '%s' $'=== Grok-Abmeldung ===' ;;
        grok_cleared) printf '%s' $'Grok-Anmeldung gelöscht.' ;;
        err_disable_req) printf '%s' $'Fehler: --disable braucht einen Slot-Namen' ;;
        err_remove_req) printf '%s' $'Fehler: --remove braucht einen Slot-Namen' ;;
        unknown_flag) printf '%s' $'Unbekannte Option: {1}' ;;
    esac
}

logout_text_es() {
    case "$1" in
        err_slot_name) printf '%s' $'Nombre de slot Claude no válido; slots está reservado.' ;;
        err_container_stopped) printf '%s' $'Error: el contenedor {1} no está en ejecución. Inícialo antes de reintentar.' ;;
        h_out_prefer) printf '%s' $'preferir este slot' ;;
        h_out_priority) printf '%s' $'establecer el orden completo de slots' ;;
        h_out_clear) printf '%s' $'quitar la espera de uno o todos los slots' ;;
        cooldown_label) printf '%s' $'espera' ;;
        cooldown_none) printf '%s' $'ninguna' ;;
        cooldown_remaining) printf '%s' $'{1}h {2}min' ;;
        slot_error_unknown_slot) printf '%s' $'Slot desconocido.' ;;
        slot_error_invalid_priority) printf '%s' $'La prioridad debe incluir cada slot configurado exactamente una vez.' ;;
        slot_error_invalid_delta) printf '%s' $'Dirección de movimiento no válida.' ;;
        slot_error_invalid_arguments) printf '%s' $'Argumentos no válidos.' ;;
        slot_apply_notice) printf '%s' $'La prioridad se aplica al agente principal en segundos y a los subagentes al reiniciar; quitar la espera se aplica a todos en la siguiente llamada.' ;;
        slots_title) printf '%s' $'=== Slots de Claude Code ===' ;;
        no_slots) printf '%s' $'  (no se encontraron slots — ejecuta ./scripts/login.sh claude para crear el predeterminado)' ;;
        state_auth) printf '%s' $'autenticado' ;;
        state_not) printf '%s' $'no autenticado' ;;
        default_marker) printf '%s' $'(predeterminado)' ;;
        unknown_provider) printf '%s' $'Proveedor desconocido: {1}' ;;
        usage) printf '%s' $'Uso:' ;;
        logout_help_title) printf '%s' $'Cerrar sesión en los proveedores de CLI dentro del contenedor ductor-bot.' ;;
        h_out_all) printf '%s' $'cerrar sesión en todos los proveedores (slots predeterminados)' ;;
        h_out_claude) printf '%s' $'cerrar sesión del slot predeterminado (claude auth logout)' ;;
        h_out_list) printf '%s' $'listar slots: estado de autenticación + prioridad en la configuración' ;;
        h_out_disable) printf '%s' $'quitar de claude_accounts en la configuración (credenciales conservadas)' ;;
        h_out_remove) printf '%s' $'quitar de la configuración + renombrar credenciales a .removed-<ts>' ;;
        h_out_alias) printf '%s' $'alias de --remove <slot>' ;;
        h_out_codex) printf '%s' $'cerrar sesión solo en Codex' ;;
        h_out_gemini) printf '%s' $'cerrar sesión solo en Gemini' ;;
        h_out_grok) printf '%s' $'cerrar sesión solo en Grok' ;;
        h_help) printf '%s' $'mostrar esta ayuda' ;;
        h_out_note) printf '%s' $'ConfigReloader recarga claude_accounts en caliente (consulta cada 5 s) y se vuelve a leer\nen cada llamada de la CLI (claude_provider.py / service.py). --disable / --remove\nsurten efecto en la siguiente llamada sin reiniciar el bot, y cualquier llamada\nen curso termina con su lista de slots actual.' ;;
        err_container) printf '%s' $'Error: no se encontró el contenedor \'{1}\'.' ;;
        priority_label) printf '%s' $'prioridad' ;;
        config_line) printf '%s' $'configuración: {1}' ;;
        accounts_list) printf '%s' $'  claude_accounts = [{1}]' ;;
        accounts_empty) printf '%s' $'  claude_accounts = (vacío — comportamiento de un solo slot)' ;;
        tip_priority) printf '%s' $'Consejo: prioridad=\'-\' significa que el slot existe en disco pero no está en claude_accounts.' ;;
        err_cfg_missing) printf '%s' $'Error: no se encontró config.json en {1}' ;;
        slot_nochange) printf '%s' $'  (el slot \'{1}\' no está en claude_accounts; sin cambios en la configuración)' ;;
        slot_changed) printf '%s' $'  config.json: claude_accounts {1} -> {2}' ;;
        warn_empty) printf '%s' $'  AVISO: claude_accounts está vacío ahora (rotación de slots desactivada).' ;;
        no_slot_dir) printf '%s' $'  (el directorio del slot \'{1}\' no existe; nada que invalidar)' ;;
        no_creds_found) printf '%s' $'  (no se encontró credentials.json en {1})' ;;
        logout_active) printf '%s' $'=== Cierre de sesión en Claude Code (slot predeterminado) ===' ;;
        disable_title) printf '%s' $'=== Desactivar slot \'{1}\' (solo configuración, credenciales conservadas) ===' ;;
        remove_title) printf '%s' $'=== Eliminar slot \'{1}\' (configuración + credenciales) ===' ;;
        done_reload) printf '%s' $'Hecho. ConfigReloader lo aplica en ~5 s; las llamadas en curso\nterminan con su lista de slots actual.' ;;
        to_reenable) printf '%s' $'Para reactivarlo: edita claude_accounts en {1} para incluir \'{2}\' de nuevo,\no ejecuta {3} (un inicio de sesión correcto propone añadirlo otra vez).' ;;
        to_restore) printf '%s' $'Para restaurar: {1} para autenticarte de nuevo.' ;;
        codex_logout) printf '%s' $'=== Cierre de sesión en Codex ===' ;;
        codex_cleared) printf '%s' $'Sesión de Codex cerrada. Se conservan las sesiones y la configuración.' ;;
        gemini_logout) printf '%s' $'=== Cierre de sesión en Gemini CLI ===' ;;
        gemini_remove_key) printf '%s' $'Quita GEMINI_API_KEY / GOOGLE_API_KEY de {1}/.env' ;;
        gemini_restart) printf '%s' $'Luego reinicia: {1}' ;;
        grok_logout) printf '%s' $'=== Cierre de sesión en Grok ===' ;;
        grok_cleared) printf '%s' $'Autenticación de Grok borrada.' ;;
        err_disable_req) printf '%s' $'Error: --disable necesita un nombre de slot' ;;
        err_remove_req) printf '%s' $'Error: --remove necesita un nombre de slot' ;;
        unknown_flag) printf '%s' $'Opción desconocida: {1}' ;;
    esac
}

logout_text_fr() {
    case "$1" in
        err_slot_name) printf '%s' $'Nom de slot Claude invalide ; slots est réservé.' ;;
        err_container_stopped) printf '%s' $'Erreur : le conteneur {1} est arrêté. Démarrez-le avant de réessayer.' ;;
        h_out_prefer) printf '%s' $'préférer ce slot' ;;
        h_out_priority) printf '%s' $'définir l’ordre complet des slots' ;;
        h_out_clear) printf '%s' $'lever le délai d’un ou de tous les slots' ;;
        cooldown_label) printf '%s' $'délai' ;;
        cooldown_none) printf '%s' $'aucun' ;;
        cooldown_remaining) printf '%s' $'{1}h {2}min' ;;
        slot_error_unknown_slot) printf '%s' $'Slot inconnu.' ;;
        slot_error_invalid_priority) printf '%s' $'La priorité doit inclure chaque slot configuré exactement une fois.' ;;
        slot_error_invalid_delta) printf '%s' $'Direction de déplacement invalide.' ;;
        slot_error_invalid_arguments) printf '%s' $'Arguments invalides.' ;;
        slot_apply_notice) printf '%s' $'La priorité s’applique à l’agent principal en quelques secondes et aux sous-agents au prochain démarrage ; la levée du délai vaut pour tous au prochain appel.' ;;
        slots_title) printf '%s' $'=== Slots Claude Code ===' ;;
        no_slots) printf '%s' $'  (aucun slot trouvé — lance ./scripts/login.sh claude pour créer celui par défaut)' ;;
        state_auth) printf '%s' $'connecté' ;;
        state_not) printf '%s' $'non connecté' ;;
        default_marker) printf '%s' $'(par défaut)' ;;
        unknown_provider) printf '%s' $'Fournisseur inconnu : {1}' ;;
        usage) printf '%s' $'Utilisation :' ;;
        logout_help_title) printf '%s' $'Se déconnecter des fournisseurs CLI dans le conteneur ductor-bot.' ;;
        h_out_all) printf '%s' $'se déconnecter de tous les fournisseurs (slots par défaut)' ;;
        h_out_claude) printf '%s' $'déconnecter le slot par défaut (claude auth logout)' ;;
        h_out_list) printf '%s' $'lister les slots : état de connexion + priorité dans la config' ;;
        h_out_disable) printf '%s' $'retirer de claude_accounts dans la config (identifiants conservés)' ;;
        h_out_remove) printf '%s' $'retirer de la config + renommer les identifiants en .removed-<ts>' ;;
        h_out_alias) printf '%s' $'alias de --remove <slot>' ;;
        h_out_codex) printf '%s' $'se déconnecter de Codex uniquement' ;;
        h_out_gemini) printf '%s' $'se déconnecter de Gemini uniquement' ;;
        h_out_grok) printf '%s' $'se déconnecter de Grok uniquement' ;;
        h_help) printf '%s' $'afficher cette aide' ;;
        h_out_note) printf '%s' $'claude_accounts est rechargé à chaud par ConfigReloader (sondage toutes les 5 s) et relu\nà chaque appel CLI (claude_provider.py / service.py). --disable / --remove\nprennent donc effet au prochain appel sans redémarrer le bot, et un appel\nen cours se termine avec sa liste de slots actuelle.' ;;
        err_container) printf '%s' $'Erreur : conteneur \'{1}\' introuvable.' ;;
        priority_label) printf '%s' $'priorité' ;;
        config_line) printf '%s' $'config : {1}' ;;
        accounts_list) printf '%s' $'  claude_accounts = [{1}]' ;;
        accounts_empty) printf '%s' $'  claude_accounts = (vide — comportement à un seul slot)' ;;
        tip_priority) printf '%s' $'Astuce : priorité=\'-\' signifie que le slot existe sur le disque mais n\'est pas dans claude_accounts.' ;;
        err_cfg_missing) printf '%s' $'Erreur : config.json introuvable à {1}' ;;
        slot_nochange) printf '%s' $'  (le slot \'{1}\' n\'est pas dans claude_accounts ; config inchangée)' ;;
        slot_changed) printf '%s' $'  config.json : claude_accounts {1} -> {2}' ;;
        warn_empty) printf '%s' $'  ATTENTION : claude_accounts est maintenant vide (rotation des slots désactivée).' ;;
        no_slot_dir) printf '%s' $'  (le dossier du slot \'{1}\' n\'existe pas ; rien à invalider)' ;;
        no_creds_found) printf '%s' $'  (aucun credentials.json trouvé dans {1})' ;;
        logout_active) printf '%s' $'=== Déconnexion Claude Code (slot par défaut) ===' ;;
        disable_title) printf '%s' $'=== Désactiver le slot \'{1}\' (config seulement, identifiants conservés) ===' ;;
        remove_title) printf '%s' $'=== Supprimer le slot \'{1}\' (config + identifiants) ===' ;;
        done_reload) printf '%s' $'Terminé. ConfigReloader le prend en compte sous ~5 s ; les appels en\ncours se terminent avec leur liste de slots actuelle.' ;;
        to_reenable) printf '%s' $'Pour réactiver : modifie claude_accounts dans {1} pour y remettre \'{2}\',\nou lance {3} (une connexion réussie propose de le rajouter).' ;;
        to_restore) printf '%s' $'Pour restaurer : {1} pour te reconnecter.' ;;
        codex_logout) printf '%s' $'=== Déconnexion Codex ===' ;;
        codex_cleared) printf '%s' $'Codex déconnecté. Les sessions et la configuration sont conservées.' ;;
        gemini_logout) printf '%s' $'=== Déconnexion Gemini CLI ===' ;;
        gemini_remove_key) printf '%s' $'Retire GEMINI_API_KEY / GOOGLE_API_KEY de {1}/.env' ;;
        gemini_restart) printf '%s' $'Puis redémarre : {1}' ;;
        grok_logout) printf '%s' $'=== Déconnexion Grok ===' ;;
        grok_cleared) printf '%s' $'Authentification Grok effacée.' ;;
        err_disable_req) printf '%s' $'Erreur : --disable demande un nom de slot' ;;
        err_remove_req) printf '%s' $'Erreur : --remove demande un nom de slot' ;;
        unknown_flag) printf '%s' $'Option inconnue : {1}' ;;
    esac
}

logout_text_id() {
    case "$1" in
        err_slot_name) printf '%s' $'Nama slot Claude tidak valid; slots dicadangkan.' ;;
        err_container_stopped) printf '%s' $'Kesalahan: kontainer {1} tidak berjalan. Jalankan sebelum mencoba lagi.' ;;
        h_out_prefer) printf '%s' $'utamakan slot ini' ;;
        h_out_priority) printf '%s' $'atur urutan semua slot' ;;
        h_out_clear) printf '%s' $'hapus jeda satu atau semua slot' ;;
        cooldown_label) printf '%s' $'jeda' ;;
        cooldown_none) printf '%s' $'tidak ada' ;;
        cooldown_remaining) printf '%s' $'{1}j {2}m' ;;
        slot_error_unknown_slot) printf '%s' $'Slot tidak dikenal.' ;;
        slot_error_invalid_priority) printf '%s' $'Prioritas harus memuat setiap slot terkonfigurasi tepat sekali.' ;;
        slot_error_invalid_delta) printf '%s' $'Arah perpindahan tidak valid.' ;;
        slot_error_invalid_arguments) printf '%s' $'Argumen tidak valid.' ;;
        slot_apply_notice) printf '%s' $'Prioritas berlaku pada agen utama dalam beberapa detik dan subagen saat mulai berikutnya; penghapusan jeda berlaku untuk semua pada panggilan berikutnya.' ;;
        slots_title) printf '%s' $'=== Slot Claude Code ===' ;;
        no_slots) printf '%s' $'  (slot tidak ditemukan — jalankan ./scripts/login.sh claude untuk membuat slot default)' ;;
        state_auth) printf '%s' $'terautentikasi' ;;
        state_not) printf '%s' $'belum terautentikasi' ;;
        default_marker) printf '%s' $'(default)' ;;
        unknown_provider) printf '%s' $'Penyedia tidak dikenal: {1}' ;;
        usage) printf '%s' $'Penggunaan:' ;;
        logout_help_title) printf '%s' $'Keluar dari penyedia CLI di dalam kontainer ductor-bot.' ;;
        h_out_all) printf '%s' $'keluar dari semua penyedia (slot default)' ;;
        h_out_claude) printf '%s' $'keluar dari slot default (claude auth logout)' ;;
        h_out_list) printf '%s' $'daftar slot: status autentikasi + prioritas di konfigurasi' ;;
        h_out_disable) printf '%s' $'hapus dari claude_accounts di konfigurasi (kredensial dipertahankan)' ;;
        h_out_remove) printf '%s' $'hapus dari konfigurasi + ganti nama kredensial menjadi .removed-<ts>' ;;
        h_out_alias) printf '%s' $'alias untuk --remove <slot>' ;;
        h_out_codex) printf '%s' $'keluar dari Codex saja' ;;
        h_out_gemini) printf '%s' $'keluar dari Gemini saja' ;;
        h_out_grok) printf '%s' $'keluar dari Grok saja' ;;
        h_help) printf '%s' $'tampilkan bantuan ini' ;;
        h_out_note) printf '%s' $'claude_accounts dimuat ulang langsung oleh ConfigReloader (polling 5 dtk) dan dibaca ulang\nsetiap panggilan CLI (claude_provider.py / service.py). Karena itu --disable / --remove\nberlaku pada panggilan berikutnya tanpa memulai ulang bot, dan panggilan yang\nsedang berjalan selesai dengan daftar slot yang lama.' ;;
        err_container) printf '%s' $'Kesalahan: kontainer \'{1}\' tidak ditemukan.' ;;
        priority_label) printf '%s' $'prioritas' ;;
        config_line) printf '%s' $'konfigurasi: {1}' ;;
        accounts_list) printf '%s' $'  claude_accounts = [{1}]' ;;
        accounts_empty) printf '%s' $'  claude_accounts = (kosong — perilaku satu slot)' ;;
        tip_priority) printf '%s' $'Tips: prioritas=\'-\' berarti slot ada di disk tetapi tidak ada di claude_accounts.' ;;
        err_cfg_missing) printf '%s' $'Kesalahan: config.json tidak ditemukan di {1}' ;;
        slot_nochange) printf '%s' $'  (slot \'{1}\' tidak ada di claude_accounts; konfigurasi tidak berubah)' ;;
        slot_changed) printf '%s' $'  config.json: claude_accounts {1} -> {2}' ;;
        warn_empty) printf '%s' $'  PERINGATAN: claude_accounts sekarang kosong (rotasi slot dinonaktifkan).' ;;
        no_slot_dir) printf '%s' $'  (direktori slot \'{1}\' tidak ada; tidak ada yang perlu dibatalkan)' ;;
        no_creds_found) printf '%s' $'  (credentials.json tidak ditemukan di {1})' ;;
        logout_active) printf '%s' $'=== Keluar dari Claude Code (slot default) ===' ;;
        disable_title) printf '%s' $'=== Nonaktifkan slot \'{1}\' (hanya konfigurasi, kredensial dipertahankan) ===' ;;
        remove_title) printf '%s' $'=== Hapus slot \'{1}\' (konfigurasi + kredensial) ===' ;;
        done_reload) printf '%s' $'Selesai. ConfigReloader menerapkannya dalam ~5 dtk; panggilan yang sedang\nberjalan selesai dengan daftar slot yang lama.' ;;
        to_reenable) printf '%s' $'Untuk mengaktifkan lagi: edit claude_accounts di {1} agar memuat \'{2}\' lagi,\natau jalankan {3} (login yang berhasil akan mengusulkan menambahkannya kembali).' ;;
        to_restore) printf '%s' $'Untuk memulihkan: {1} untuk mengautentikasi ulang.' ;;
        codex_logout) printf '%s' $'=== Keluar dari Codex ===' ;;
        codex_cleared) printf '%s' $'Keluar dari Codex. Sesi dan konfigurasi tetap disimpan.' ;;
        gemini_logout) printf '%s' $'=== Keluar dari Gemini CLI ===' ;;
        gemini_remove_key) printf '%s' $'Hapus GEMINI_API_KEY / GOOGLE_API_KEY dari {1}/.env' ;;
        gemini_restart) printf '%s' $'Lalu mulai ulang: {1}' ;;
        grok_logout) printf '%s' $'=== Keluar dari Grok ===' ;;
        grok_cleared) printf '%s' $'Autentikasi Grok dihapus.' ;;
        err_disable_req) printf '%s' $'Kesalahan: --disable memerlukan nama slot' ;;
        err_remove_req) printf '%s' $'Kesalahan: --remove memerlukan nama slot' ;;
        unknown_flag) printf '%s' $'Opsi tidak dikenal: {1}' ;;
    esac
}

logout_text_nl() {
    case "$1" in
        err_slot_name) printf '%s' $'Ongeldige Claude-slotnaam; slots is gereserveerd.' ;;
        err_container_stopped) printf '%s' $'Fout: container {1} draait niet. Start deze voordat u het opnieuw probeert.' ;;
        h_out_prefer) printf '%s' $'geef deze slot voorrang' ;;
        h_out_priority) printf '%s' $'stel de volledige slotvolgorde in' ;;
        h_out_clear) printf '%s' $'hef de wachttijd van één of alle slots op' ;;
        cooldown_label) printf '%s' $'wachttijd' ;;
        cooldown_none) printf '%s' $'geen' ;;
        cooldown_remaining) printf '%s' $'{1}u {2}m' ;;
        slot_error_unknown_slot) printf '%s' $'Onbekende slot.' ;;
        slot_error_invalid_priority) printf '%s' $'De prioriteit moet elke ingestelde slot precies één keer bevatten.' ;;
        slot_error_invalid_delta) printf '%s' $'Ongeldige verplaatsingsrichting.' ;;
        slot_error_invalid_arguments) printf '%s' $'Ongeldige argumenten.' ;;
        slot_apply_notice) printf '%s' $'Prioriteit geldt binnen seconden voor de hoofdagent en bij de volgende start voor subagenten; opgeheven wachttijden gelden voor iedereen bij de volgende aanroep.' ;;
        slots_title) printf '%s' $'=== Claude Code-slots ===' ;;
        no_slots) printf '%s' $'  (geen slots gevonden — voer ./scripts/login.sh claude uit om de standaard aan te maken)' ;;
        state_auth) printf '%s' $'aangemeld' ;;
        state_not) printf '%s' $'niet aangemeld' ;;
        default_marker) printf '%s' $'(standaard)' ;;
        unknown_provider) printf '%s' $'Onbekende provider: {1}' ;;
        usage) printf '%s' $'Gebruik:' ;;
        logout_help_title) printf '%s' $'Afmelden bij CLI-providers in de ductor-bot-container.' ;;
        h_out_all) printf '%s' $'afmelden bij alle providers (standaardslots)' ;;
        h_out_claude) printf '%s' $'standaardslot afmelden (claude auth logout)' ;;
        h_out_list) printf '%s' $'slots tonen: aanmeldstatus + prioriteit in de config' ;;
        h_out_disable) printf '%s' $'uit claude_accounts in de config halen (inloggegevens blijven)' ;;
        h_out_remove) printf '%s' $'uit de config halen + inloggegevens hernoemen naar .removed-<ts>' ;;
        h_out_alias) printf '%s' $'alias voor --remove <slot>' ;;
        h_out_codex) printf '%s' $'alleen afmelden bij Codex' ;;
        h_out_gemini) printf '%s' $'alleen afmelden bij Gemini' ;;
        h_out_grok) printf '%s' $'alleen afmelden bij Grok' ;;
        h_help) printf '%s' $'deze hulp tonen' ;;
        h_out_note) printf '%s' $'claude_accounts wordt door ConfigReloader live herladen (poll elke 5 s) en bij\nelke CLI-aanroep opnieuw gelezen (claude_provider.py / service.py). --disable / --remove\ngelden dus bij de volgende aanroep zonder herstart van de bot, en een lopende\naanroep eindigt met zijn bestaande slotlijst.' ;;
        err_container) printf '%s' $'Fout: container \'{1}\' niet gevonden.' ;;
        priority_label) printf '%s' $'prioriteit' ;;
        config_line) printf '%s' $'config: {1}' ;;
        accounts_list) printf '%s' $'  claude_accounts = [{1}]' ;;
        accounts_empty) printf '%s' $'  claude_accounts = (leeg — gedrag met één slot)' ;;
        tip_priority) printf '%s' $'Tip: prioriteit=\'-\' betekent dat het slot op schijf staat maar niet in claude_accounts.' ;;
        err_cfg_missing) printf '%s' $'Fout: config.json niet gevonden op {1}' ;;
        slot_nochange) printf '%s' $'  (slot \'{1}\' staat niet in claude_accounts; config ongewijzigd)' ;;
        slot_changed) printf '%s' $'  config.json: claude_accounts {1} -> {2}' ;;
        warn_empty) printf '%s' $'  WAARSCHUWING: claude_accounts is nu leeg (slotrotatie uitgeschakeld).' ;;
        no_slot_dir) printf '%s' $'  (slotmap \'{1}\' bestaat niet; niets om ongeldig te maken)' ;;
        no_creds_found) printf '%s' $'  (geen credentials.json gevonden in {1})' ;;
        logout_active) printf '%s' $'=== Claude Code-afmelding (standaardslot) ===' ;;
        disable_title) printf '%s' $'=== Slot \'{1}\' uitschakelen (alleen config, inloggegevens blijven) ===' ;;
        remove_title) printf '%s' $'=== Slot \'{1}\' verwijderen (config + inloggegevens) ===' ;;
        done_reload) printf '%s' $'Klaar. ConfigReloader pakt dit binnen ~5 s op; lopende aanroepen\neindigen met hun bestaande slotlijst.' ;;
        to_reenable) printf '%s' $'Opnieuw inschakelen: pas claude_accounts in {1} aan zodat \'{2}\' er weer in staat,\nof voer {3} uit (een geslaagde aanmelding stelt voor het terug te zetten).' ;;
        to_restore) printf '%s' $'Herstellen: {1} om opnieuw aan te melden.' ;;
        codex_logout) printf '%s' $'=== Codex-afmelding ===' ;;
        codex_cleared) printf '%s' $'Afgemeld bij Codex. Sessies en configuratie blijven behouden.' ;;
        gemini_logout) printf '%s' $'=== Gemini CLI-afmelding ===' ;;
        gemini_remove_key) printf '%s' $'Verwijder GEMINI_API_KEY / GOOGLE_API_KEY uit {1}/.env' ;;
        gemini_restart) printf '%s' $'Herstart daarna: {1}' ;;
        grok_logout) printf '%s' $'=== Grok-afmelding ===' ;;
        grok_cleared) printf '%s' $'Grok-aanmelding gewist.' ;;
        err_disable_req) printf '%s' $'Fout: --disable heeft een slotnaam nodig' ;;
        err_remove_req) printf '%s' $'Fout: --remove heeft een slotnaam nodig' ;;
        unknown_flag) printf '%s' $'Onbekende optie: {1}' ;;
    esac
}

logout_text_pt() {
    case "$1" in
        err_slot_name) printf '%s' $'Nome de slot Claude inválido; slots é reservado.' ;;
        err_container_stopped) printf '%s' $'Erro: o contêiner {1} não está em execução. Inicie-o antes de tentar novamente.' ;;
        h_out_prefer) printf '%s' $'priorizar este slot' ;;
        h_out_priority) printf '%s' $'definir a ordem completa dos slots' ;;
        h_out_clear) printf '%s' $'limpar a espera de um ou todos os slots' ;;
        cooldown_label) printf '%s' $'espera' ;;
        cooldown_none) printf '%s' $'nenhuma' ;;
        cooldown_remaining) printf '%s' $'{1}h {2}min' ;;
        slot_error_unknown_slot) printf '%s' $'Slot desconhecido.' ;;
        slot_error_invalid_priority) printf '%s' $'A prioridade deve incluir cada slot configurado exatamente uma vez.' ;;
        slot_error_invalid_delta) printf '%s' $'Direção de movimento inválida.' ;;
        slot_error_invalid_arguments) printf '%s' $'Argumentos inválidos.' ;;
        slot_apply_notice) printf '%s' $'A prioridade vale no agente principal em segundos e nos subagentes na próxima inicialização; limpar a espera vale para todos na próxima chamada.' ;;
        slots_title) printf '%s' $'=== Slots do Claude Code ===' ;;
        no_slots) printf '%s' $'  (nenhum slot encontrado — execute ./scripts/login.sh claude para criar o padrão)' ;;
        state_auth) printf '%s' $'autenticado' ;;
        state_not) printf '%s' $'não autenticado' ;;
        default_marker) printf '%s' $'(padrão)' ;;
        unknown_provider) printf '%s' $'Provedor desconhecido: {1}' ;;
        usage) printf '%s' $'Uso:' ;;
        logout_help_title) printf '%s' $'Sair dos provedores de CLI dentro do contêiner ductor-bot.' ;;
        h_out_all) printf '%s' $'sair de todos os provedores (slots padrão)' ;;
        h_out_claude) printf '%s' $'sair do slot padrão (claude auth logout)' ;;
        h_out_list) printf '%s' $'listar slots: estado de autenticação + prioridade na configuração' ;;
        h_out_disable) printf '%s' $'remover de claude_accounts na configuração (credenciais mantidas)' ;;
        h_out_remove) printf '%s' $'remover da configuração + renomear credenciais para .removed-<ts>' ;;
        h_out_alias) printf '%s' $'atalho para --remove <slot>' ;;
        h_out_codex) printf '%s' $'sair somente do Codex' ;;
        h_out_gemini) printf '%s' $'sair somente do Gemini' ;;
        h_out_grok) printf '%s' $'sair somente do Grok' ;;
        h_help) printf '%s' $'mostrar esta ajuda' ;;
        h_out_note) printf '%s' $'claude_accounts é recarregado a quente pelo ConfigReloader (consulta a cada 5 s) e relido\nem cada chamada da CLI (claude_provider.py / service.py). --disable / --remove\nvalem, portanto, na próxima chamada sem reiniciar o bot, e qualquer chamada\nem andamento termina com a lista de slots que já tinha.' ;;
        err_container) printf '%s' $'Erro: contêiner \'{1}\' não encontrado.' ;;
        priority_label) printf '%s' $'prioridade' ;;
        config_line) printf '%s' $'configuração: {1}' ;;
        accounts_list) printf '%s' $'  claude_accounts = [{1}]' ;;
        accounts_empty) printf '%s' $'  claude_accounts = (vazio — comportamento de um só slot)' ;;
        tip_priority) printf '%s' $'Dica: prioridade=\'-\' significa que o slot existe em disco, mas não está em claude_accounts.' ;;
        err_cfg_missing) printf '%s' $'Erro: config.json não encontrado em {1}' ;;
        slot_nochange) printf '%s' $'  (o slot \'{1}\' não está em claude_accounts; configuração inalterada)' ;;
        slot_changed) printf '%s' $'  config.json: claude_accounts {1} -> {2}' ;;
        warn_empty) printf '%s' $'  AVISO: claude_accounts agora está vazio (rotação de slots desativada).' ;;
        no_slot_dir) printf '%s' $'  (o diretório do slot \'{1}\' não existe; nada a invalidar)' ;;
        no_creds_found) printf '%s' $'  (nenhum credentials.json encontrado em {1})' ;;
        logout_active) printf '%s' $'=== Logout do Claude Code (slot padrão) ===' ;;
        disable_title) printf '%s' $'=== Desativar slot \'{1}\' (só configuração, credenciais mantidas) ===' ;;
        remove_title) printf '%s' $'=== Remover slot \'{1}\' (configuração + credenciais) ===' ;;
        done_reload) printf '%s' $'Pronto. O ConfigReloader aplica isso em ~5 s; as chamadas em andamento\nterminam com a lista de slots que já tinham.' ;;
        to_reenable) printf '%s' $'Para reativar: edite claude_accounts em {1} para incluir \'{2}\' de novo,\nou execute {3} (um login bem-sucedido propõe adicioná-lo de volta).' ;;
        to_restore) printf '%s' $'Para restaurar: {1} para autenticar de novo.' ;;
        codex_logout) printf '%s' $'=== Logout do Codex ===' ;;
        codex_cleared) printf '%s' $'Codex desconectado. As sessões e a configuração são mantidas.' ;;
        gemini_logout) printf '%s' $'=== Logout do Gemini CLI ===' ;;
        gemini_remove_key) printf '%s' $'Remova GEMINI_API_KEY / GOOGLE_API_KEY de {1}/.env' ;;
        gemini_restart) printf '%s' $'Depois reinicie: {1}' ;;
        grok_logout) printf '%s' $'=== Logout do Grok ===' ;;
        grok_cleared) printf '%s' $'Autenticação do Grok apagada.' ;;
        err_disable_req) printf '%s' $'Erro: --disable exige um nome de slot' ;;
        err_remove_req) printf '%s' $'Erro: --remove exige um nome de slot' ;;
        unknown_flag) printf '%s' $'Opção desconhecida: {1}' ;;
    esac
}

logout_text_ru() {
    case "$1" in
        err_slot_name) printf '%s' $'Недопустимое имя слота Claude; slots зарезервировано.' ;;
        err_container_stopped) printf '%s' $'Ошибка: контейнер {1} не запущен. Запустите его перед повторной попыткой.' ;;
        h_out_prefer) printf '%s' $'предпочитать этот слот' ;;
        h_out_priority) printf '%s' $'задать полный порядок слотов' ;;
        h_out_clear) printf '%s' $'снять ожидание одного или всех слотов' ;;
        cooldown_label) printf '%s' $'ожидание' ;;
        cooldown_none) printf '%s' $'нет' ;;
        cooldown_remaining) printf '%s' $'{1}ч {2}м' ;;
        slot_error_unknown_slot) printf '%s' $'Неизвестный слот.' ;;
        slot_error_invalid_priority) printf '%s' $'Приоритет должен содержать каждый настроенный слот ровно один раз.' ;;
        slot_error_invalid_delta) printf '%s' $'Недопустимое направление перемещения.' ;;
        slot_error_invalid_arguments) printf '%s' $'Недопустимые аргументы.' ;;
        slot_apply_notice) printf '%s' $'Приоритет применяется к главному агенту через несколько секунд, к подагентам при следующем запуске; снятие ожидания действует для всех со следующего вызова.' ;;
        slots_title) printf '%s' $'=== Слоты Claude Code ===' ;;
        no_slots) printf '%s' $'  (слоты не найдены — запусти ./scripts/login.sh claude, чтобы создать слот по умолчанию)' ;;
        state_auth) printf '%s' $'выполнен вход' ;;
        state_not) printf '%s' $'вход не выполнен' ;;
        default_marker) printf '%s' $'(по умолчанию)' ;;
        unknown_provider) printf '%s' $'Неизвестный провайдер: {1}' ;;
        usage) printf '%s' $'Использование:' ;;
        logout_help_title) printf '%s' $'Выход из CLI-провайдеров внутри контейнера ductor-bot.' ;;
        h_out_all) printf '%s' $'выйти из всех провайдеров (слоты по умолчанию)' ;;
        h_out_claude) printf '%s' $'выйти из слота по умолчанию (claude auth logout)' ;;
        h_out_list) printf '%s' $'список слотов: состояние входа + приоритет в конфигурации' ;;
        h_out_disable) printf '%s' $'убрать из claude_accounts в конфигурации (учётные данные сохраняются)' ;;
        h_out_remove) printf '%s' $'убрать из конфигурации + переименовать учётные данные в .removed-<ts>' ;;
        h_out_alias) printf '%s' $'сокращение для --remove <slot>' ;;
        h_out_codex) printf '%s' $'выйти только из Codex' ;;
        h_out_gemini) printf '%s' $'выйти только из Gemini' ;;
        h_out_grok) printf '%s' $'выйти только из Grok' ;;
        h_help) printf '%s' $'показать эту справку' ;;
        h_out_note) printf '%s' $'claude_accounts перечитывается на лету через ConfigReloader (опрос раз в 5 с) и заново\nчитается при каждом вызове CLI (claude_provider.py / service.py). Поэтому --disable / --remove\nдействуют со следующего вызова без перезапуска бота, а вызов,\nкоторый уже идёт, завершается со своим прежним списком слотов.' ;;
        err_container) printf '%s' $'Ошибка: контейнер \'{1}\' не найден.' ;;
        priority_label) printf '%s' $'приоритет' ;;
        config_line) printf '%s' $'конфигурация: {1}' ;;
        accounts_list) printf '%s' $'  claude_accounts = [{1}]' ;;
        accounts_empty) printf '%s' $'  claude_accounts = (пусто — режим с одним слотом)' ;;
        tip_priority) printf '%s' $'Совет: приоритет=\'-\' значит, что слот есть на диске, но его нет в claude_accounts.' ;;
        err_cfg_missing) printf '%s' $'Ошибка: config.json не найден: {1}' ;;
        slot_nochange) printf '%s' $'  (слота \'{1}\' нет в claude_accounts; конфигурация не менялась)' ;;
        slot_changed) printf '%s' $'  config.json: claude_accounts {1} -> {2}' ;;
        warn_empty) printf '%s' $'  ВНИМАНИЕ: claude_accounts теперь пуст (ротация слотов отключена).' ;;
        no_slot_dir) printf '%s' $'  (папки слота \'{1}\' нет; нечего аннулировать)' ;;
        no_creds_found) printf '%s' $'  (в {1} не найден credentials.json)' ;;
        logout_active) printf '%s' $'=== Выход из Claude Code (слот по умолчанию) ===' ;;
        disable_title) printf '%s' $'=== Отключение слота \'{1}\' (только конфигурация, учётные данные сохраняются) ===' ;;
        remove_title) printf '%s' $'=== Удаление слота \'{1}\' (конфигурация + учётные данные) ===' ;;
        done_reload) printf '%s' $'Готово. ConfigReloader подхватит это примерно за 5 с; текущие вызовы\nзавершатся со своим прежним списком слотов.' ;;
        to_reenable) printf '%s' $'Чтобы включить снова: добавь \'{2}\' обратно в claude_accounts в {1},\nили запусти {3} (успешный вход предложит добавить его).' ;;
        to_restore) printf '%s' $'Чтобы восстановить: {1}, чтобы войти заново.' ;;
        codex_logout) printf '%s' $'=== Выход из Codex ===' ;;
        codex_cleared) printf '%s' $'Выход из Codex выполнен. Сеансы и конфигурация сохранены.' ;;
        gemini_logout) printf '%s' $'=== Выход из Gemini CLI ===' ;;
        gemini_remove_key) printf '%s' $'Удали GEMINI_API_KEY / GOOGLE_API_KEY из {1}/.env' ;;
        gemini_restart) printf '%s' $'Затем перезапусти: {1}' ;;
        grok_logout) printf '%s' $'=== Выход из Grok ===' ;;
        grok_cleared) printf '%s' $'Данные входа Grok удалены.' ;;
        err_disable_req) printf '%s' $'Ошибка: для --disable нужно имя слота' ;;
        err_remove_req) printf '%s' $'Ошибка: для --remove нужно имя слота' ;;
        unknown_flag) printf '%s' $'Неизвестный параметр: {1}' ;;
    esac
}

# hangul-allowed-begin: Korean messages of logout.sh

logout_text_ko() {
    case "$1" in
        err_slot_name) printf '%s' $'잘못된 Claude 슬롯 이름입니다. slots는 예약어입니다.' ;;
        err_container_stopped) printf '%s' $'오류: 컨테이너 {1}이 실행 중이 아닙니다. 시작한 뒤 다시 시도하십시오.' ;;
        h_out_prefer) printf '%s' $'이 슬롯을 우선 사용' ;;
        h_out_priority) printf '%s' $'전체 슬롯 순서 지정' ;;
        h_out_clear) printf '%s' $'슬롯 하나 또는 전체 쿨다운 해제' ;;
        cooldown_label) printf '%s' $'쿨다운' ;;
        cooldown_none) printf '%s' $'없음' ;;
        cooldown_remaining) printf '%s' $'{1}시간 {2}분' ;;
        slot_error_unknown_slot) printf '%s' $'알 수 없는 슬롯입니다.' ;;
        slot_error_invalid_priority) printf '%s' $'우선순위에는 설정된 모든 슬롯이 정확히 한 번씩 있어야 합니다.' ;;
        slot_error_invalid_delta) printf '%s' $'이동 방향이 잘못되었습니다.' ;;
        slot_error_invalid_arguments) printf '%s' $'인자가 잘못되었습니다.' ;;
        slot_apply_notice) printf '%s' $'우선순위는 main에 몇 초 안에, 하위 에이전트는 다음 기동 때 반영됩니다. 쿨다운 해제는 모두 다음 호출부터 반영됩니다.' ;;
        slots_title) printf '%s' $'=== Claude Code 슬롯 ===' ;;
        no_slots) printf '%s' $'  (슬롯이 없어요 — ./scripts/login.sh claude 를 실행해 기본 슬롯을 만드세요)' ;;
        state_auth) printf '%s' $'인증됨' ;;
        state_not) printf '%s' $'인증 안 됨' ;;
        default_marker) printf '%s' $'(기본)' ;;
        unknown_provider) printf '%s' $'알 수 없는 제공자: {1}' ;;
        usage) printf '%s' $'사용법:' ;;
        logout_help_title) printf '%s' $'ductor-bot 컨테이너 안의 CLI 제공자에서 로그아웃해요.' ;;
        h_out_all) printf '%s' $'모든 제공자에서 로그아웃 (기본 슬롯)' ;;
        h_out_claude) printf '%s' $'기본 슬롯 로그아웃 (claude auth logout)' ;;
        h_out_list) printf '%s' $'슬롯 목록: 인증 상태와 설정의 우선순위' ;;
        h_out_disable) printf '%s' $'설정의 claude_accounts에서 제거 (자격 증명은 유지)' ;;
        h_out_remove) printf '%s' $'설정에서 제거하고 자격 증명 이름을 .removed-<ts>로 변경' ;;
        h_out_alias) printf '%s' $'--remove <slot>의 줄임 명령' ;;
        h_out_codex) printf '%s' $'Codex만 로그아웃' ;;
        h_out_gemini) printf '%s' $'Gemini만 로그아웃' ;;
        h_out_grok) printf '%s' $'Grok만 로그아웃' ;;
        h_help) printf '%s' $'이 도움말 표시' ;;
        h_out_note) printf '%s' $'claude_accounts는 ConfigReloader가 핫 리로드하고(5초 폴링) CLI 호출마다\n다시 읽어요(claude_provider.py / service.py). 그래서 --disable / --remove는\n봇을 다시 시작하지 않아도 다음 호출부터 적용되고, 진행 중인 호출은\n기존 슬롯 목록으로 끝나요.' ;;
        err_container) printf '%s' $'오류: \'{1}\' 컨테이너를 찾을 수 없어요.' ;;
        priority_label) printf '%s' $'우선순위' ;;
        config_line) printf '%s' $'설정: {1}' ;;
        accounts_list) printf '%s' $'  claude_accounts = [{1}]' ;;
        accounts_empty) printf '%s' $'  claude_accounts = (비어 있음 — 단일 슬롯 동작)' ;;
        tip_priority) printf '%s' $'팁: 우선순위=\'-\'는 슬롯이 디스크에는 있지만 claude_accounts에 없다는 뜻이에요.' ;;
        err_cfg_missing) printf '%s' $'오류: {1}에서 config.json을 찾을 수 없어요' ;;
        slot_nochange) printf '%s' $'  (\'{1}\' 슬롯이 claude_accounts에 없어요. 설정은 바뀌지 않았어요)' ;;
        slot_changed) printf '%s' $'  config.json: claude_accounts {1} -> {2}' ;;
        warn_empty) printf '%s' $'  경고: claude_accounts가 비었어요 (슬롯 전환이 꺼져요).' ;;
        no_slot_dir) printf '%s' $'  (\'{1}\' 슬롯 디렉터리가 없어요. 무효화할 것이 없어요)' ;;
        no_creds_found) printf '%s' $'  ({1}에서 credentials.json을 찾지 못했어요)' ;;
        logout_active) printf '%s' $'=== Claude Code 로그아웃 (기본 슬롯) ===' ;;
        disable_title) printf '%s' $'=== \'{1}\' 슬롯 비활성화 (설정만 변경, 자격 증명 유지) ===' ;;
        remove_title) printf '%s' $'=== \'{1}\' 슬롯 제거 (설정 + 자격 증명) ===' ;;
        done_reload) printf '%s' $'완료했어요. ConfigReloader가 약 5초 안에 반영하고, 진행 중인 호출은\n기존 슬롯 목록으로 끝나요.' ;;
        to_reenable) printf '%s' $'다시 켜려면: {1}의 claude_accounts에 \'{2}\'을(를) 다시 넣거나,\n{3} 을(를) 실행하세요 (로그인에 성공하면 다시 추가할지 물어봐요).' ;;
        to_restore) printf '%s' $'복구하려면: {1} 로 다시 인증하세요.' ;;
        codex_logout) printf '%s' $'=== Codex 로그아웃 ===' ;;
        codex_cleared) printf '%s' $'Codex에서 로그아웃했어요. 세션과 설정은 유지해요.' ;;
        gemini_logout) printf '%s' $'=== Gemini CLI 로그아웃 ===' ;;
        gemini_remove_key) printf '%s' $'{1}/.env 에서 GEMINI_API_KEY / GOOGLE_API_KEY를 지우세요' ;;
        gemini_restart) printf '%s' $'그다음 다시 시작하세요: {1}' ;;
        grok_logout) printf '%s' $'=== Grok 로그아웃 ===' ;;
        grok_cleared) printf '%s' $'Grok 인증을 지웠어요.' ;;
        err_disable_req) printf '%s' $'오류: --disable에는 슬롯 이름이 필요해요' ;;
        err_remove_req) printf '%s' $'오류: --remove에는 슬롯 이름이 필요해요' ;;
        unknown_flag) printf '%s' $'알 수 없는 옵션: {1}' ;;
    esac
}

# hangul-allowed-end

# ── helpers (parallel to scripts/login.sh) ────────────────────────────────

show_help() {
    say logout_help_title
    echo
    say usage
    printf '%s# %s\n' "  ./scripts/logout.sh                            " "$(say h_out_all)"
    printf '%s# %s\n' "  ./scripts/logout.sh claude                     " "$(say h_out_claude)"
    printf '%s# %s\n' "  ./scripts/logout.sh claude --list              " "$(say h_out_list)"
    printf '%s# %s\n' "  ./scripts/logout.sh claude --prefer SLOT           " "$(say h_out_prefer)"
    printf '%s# %s\n' "  ./scripts/logout.sh claude --priority A,B,...      " "$(say h_out_priority)"
    printf '%s# %s\n' "  ./scripts/logout.sh claude --clear-cooldown SLOT|all " "$(say h_out_clear)"
    printf '%s# %s\n' "  ./scripts/logout.sh claude --disable <slot>    " "$(say h_out_disable)"
    printf '%s# %s\n' "  ./scripts/logout.sh claude --remove  <slot>    " "$(say h_out_remove)"
    printf '%s# %s\n' "  ./scripts/logout.sh claude <slot>              " "$(say h_out_alias)"
    printf '%s# %s\n' "  ./scripts/logout.sh codex                      " "$(say h_out_codex)"
    printf '%s# %s\n' "  ./scripts/logout.sh gemini                     " "$(say h_out_gemini)"
    printf '%s# %s\n' "  ./scripts/logout.sh grok                       " "$(say h_out_grok)"
    printf '%s# %s\n' "  ./scripts/logout.sh -h | --help                " "$(say h_help)"
    echo
    say h_out_note
}

ensure_container() {
    local running
    if ! running="$(docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null)"; then
        say_err err_container "$CONTAINER"
        exit 1
    fi
    if [ "$running" != true ]; then
        say_err err_container_stopped "$CONTAINER"
        exit 1
    fi
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

# ── Claude slot listing with priority ─────────────────────────────────────

list_claude_slots_with_priority() {
    say slots_title
    if [ ! -d "$AUTH_DIR_HOST" ]; then
        say no_slots
        return 0
    fi

    local accounts_csv=""
    if [ -f "$CONFIG_HOST" ]; then
        accounts_csv="$(python3 - "$CONFIG_HOST" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
    print(",".join(cfg.get("claude_accounts") or []))
except Exception:
    pass
PY
        )"
    fi

    local found=0
    for dir in "${AUTH_DIR_HOST}"/claude "${AUTH_DIR_HOST}"/claude-*; do
        [ -d "$dir" ] || continue
        [ "${dir##*/}" != "claude-slots" ] || continue
        found=1
        local slot priority="-" state
        slot="$(basename "$dir")"
        state="$(slot_state_label "$(slot_auth_state "$dir")")"
        if [ -n "$accounts_csv" ]; then
            priority="$(SLOT="$slot" CSV="$accounts_csv" python3 -c '
import os
csv = os.environ["CSV"].split(",")
s = os.environ["SLOT"]
print(csv.index(s) + 1 if s in csv else "-")
')"
        fi
        local remaining cooldown
        remaining="$(python3 - "$AUTH_DIR_HOST/_claude-slots.json" "$slot" <<'PYCOOLDOWN'
import json
import math
import sys
import time
try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        raw = json.load(stream)
    until = raw.get("cooldowns", {}).get(sys.argv[2], 0)
    minutes = max(0, math.ceil((until - time.time()) / 60))
except (OSError, ValueError, TypeError, AttributeError):
    minutes = 0
print(minutes)
PYCOOLDOWN
)"
        cooldown="$(say cooldown_none)"
        if [ "$remaining" -gt 0 ]; then
            cooldown="$(say cooldown_remaining "$((remaining / 60))" "$((remaining % 60))")"
        fi
        printf "  %-18s %s=%-3s %-18s %s=%s" "$slot" "$(say priority_label)" "$priority" "$state" "$(say cooldown_label)" "$cooldown"
        if [ "$slot" = "claude" ]; then
            printf " %s" "$(say default_marker)"
        fi
        printf '\n'
    done
    if [ "$found" -eq 0 ]; then
        say no_slots
    fi
    echo
    say config_line "$CONFIG_HOST"
    if [ -n "$accounts_csv" ]; then
        say accounts_list "${accounts_csv//,/, }"
    else
        say accounts_empty
    fi
    echo
    say tip_priority
}

# ── claude_accounts edit (atomic) ─────────────────────────────────────────

remove_slot_from_config() {
    # $1: slot name to drop from claude_accounts
    # Atomic write via temp file + os.replace.
    local slot="$1"
    if [ ! -f "$CONFIG_HOST" ]; then
        say_err err_cfg_missing "$CONFIG_HOST"
        return 1
    fi
    # The messages are put in by the shell; {1} and {2} are filled in below.
    SLOT="$slot" MSG_NOCHANGE="$(host_template logout slot_nochange)" \
        MSG_CHANGED="$(host_template logout slot_changed)" \
        MSG_EMPTY="$(host_template logout warn_empty)" python3 - "$CONFIG_HOST" <<'PY'
import fcntl, json, os, re, sys, tempfile


def fill(template, *values):
    return re.sub(r"\{(\d)\}", lambda m: values[int(m.group(1)) - 1], template)



slot = os.environ["SLOT"]
cfg_path = sys.argv[1]
lock_path = os.path.join(os.path.dirname(cfg_path), ".config.json.lock")
with open(lock_path, "a") as config_lock:
    fcntl.flock(config_lock, fcntl.LOCK_EX)
    with open(cfg_path) as f:
        cfg = json.load(f)
    accounts = list(cfg.get("claude_accounts") or [])
    if slot not in accounts:
        print(fill(os.environ["MSG_NOCHANGE"], slot))
        sys.exit(0)
    new_accounts = [a for a in accounts if a != slot]
    cfg["claude_accounts"] = new_accounts
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cfg_path), prefix=".config.json.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, cfg_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print(fill(os.environ["MSG_CHANGED"], str(accounts), str(new_accounts)))
    if not new_accounts:
        print(os.environ["MSG_EMPTY"], file=sys.stderr)
PY
}

# ── slot credential invalidation ──────────────────────────────────────────

invalidate_slot_credentials() {
    # $1: slot name. Renames .credentials.json (and credentials.json) to
    # *.removed-<ts> so they can be restored manually if needed.
    local slot="$1"
    local dir="${AUTH_DIR_HOST}/${slot}"
    if [ ! -d "$dir" ]; then
        say no_slot_dir "$dir"
        return 0
    fi
    local ts moved=0
    ts="$(date +%Y%m%d-%H%M%S)"
    for f in .credentials.json credentials.json; do
        if [ -f "${dir}/${f}" ]; then
            mv "${dir}/${f}" "${dir}/${f}.removed-${ts}"
            echo "  ${dir}/${f} -> ${f}.removed-${ts}"
            moved=1
        fi
    done
    if [ "$moved" -eq 0 ]; then
        say no_creds_found "$dir"
    fi
}

# ── high-level commands ───────────────────────────────────────────────────

logout_claude_active() {
    ensure_container
    say logout_active
    local -a tty_args=()
    if [[ -t 0 && -t 1 ]]; then
        tty_args=(-it)
    fi
    docker exec "${tty_args[@]}" -e CLAUDE_CONFIG_DIR=/ductor/.cli-auth/claude "$CONTAINER" claude auth logout
}

cmd_disable_slot() {
    local slot
    slot="$(resolve_claude_slot_name "$1")" || return $?
    say disable_title "$slot"
    remove_slot_from_config "$slot"
    echo
    say done_reload
    say to_reenable "$CONFIG_HOST" "$slot" \
        "./scripts/login.sh claude $([ "$slot" = "claude" ] && echo "" || echo "${slot#claude-}")"
}

cmd_remove_slot() {
    local slot
    slot="$(resolve_claude_slot_name "$1")" || return $?
    say remove_title "$slot"
    remove_slot_from_config "$slot"
    invalidate_slot_credentials "$slot"
    echo
    say done_reload
    say to_restore "./scripts/login.sh claude $([ "$slot" = "claude" ] && echo "" || echo "${slot#claude-}")"
}

cmd_manage_slot() {
    local action="$1" value="${2:-}" result status=0 code
    [ $# -eq 2 ] && [ -n "$value" ] || { say_err slot_error_invalid_arguments; return 2; }
    if [ "$action" = "priority" ]; then
        local name normalized="" separator=""
        local -a names=()
        IFS=',' read -r -a names <<< "$value"
        [[ "$value" != ,* && "$value" != *, && "$value" != *,,* ]] || { say_err slot_error_invalid_arguments; return 2; }
        for name in "${names[@]}"; do
            name="$(resolve_claude_slot_name "$name")" || return $?
            normalized+="${separator}${name}"
            separator=","
        done
        value="$normalized"
    elif [ "$action" != "clear-cooldown" ] || [ "$value" != "all" ]; then
        value="$(resolve_claude_slot_name "$value")" || return $?
    fi
    ensure_container
    result="$(docker exec "$CONTAINER" python3 -I -m ductor_bot.cli.claude_slots "$action" "$value")" || status=$?
    if [ "$status" -ne 0 ]; then
        code="$(python3 -c 'import json, sys; print(json.load(sys.stdin).get("error", "invalid_arguments"))' <<< "$result")" || code=invalid_arguments
        case "$code" in
            unknown_slot|invalid_priority|invalid_delta|invalid_arguments) say_err "slot_error_$code" ;;
            *) say_err slot_error_invalid_arguments ;;
        esac
        return "$status"
    fi
    list_claude_slots_with_priority
    say slot_apply_notice
}

logout_claude_dispatch() {
    if [ $# -eq 0 ]; then
        logout_claude_active
        return
    fi
    case "$1" in
        --prefer|--priority|--clear-cooldown)
            local action="${1#--}"
            shift
            cmd_manage_slot "$action" "$@"
            ;;
        --list)
            list_claude_slots_with_priority
            ;;
        --disable)
            shift
            [ $# -gt 0 ] || { say_err err_disable_req; exit 1; }
            cmd_disable_slot "$1"
            ;;
        --remove)
            shift
            [ $# -gt 0 ] || { say_err err_remove_req; exit 1; }
            cmd_remove_slot "$1"
            ;;
        -*)
            say_err unknown_flag "$1"
            show_help
            exit 1
            ;;
        *)
            # Bare slot name -> shorthand for --remove
            cmd_remove_slot "$1"
            ;;
    esac
}

logout_codex() {
    ensure_container
    say codex_logout
    docker exec "$CONTAINER" codex logout
    say codex_cleared
}

logout_gemini() {
    ensure_container
    say gemini_logout
    docker exec "$CONTAINER" rm -f /ductor/.cli-auth/gemini/oauth_creds.json
    say gemini_remove_key "$DUCTOR_HOME_HOST"
    say gemini_restart "docker compose up -d"
}

logout_grok() {
    ensure_container
    say grok_logout
    docker exec "$CONTAINER" grok logout
    say grok_cleared
}

# ── main ──────────────────────────────────────────────────────────────────

if [ $# -eq 0 ]; then
    logout_claude_active
    echo ""
    logout_codex
    echo ""
    logout_gemini
    echo ""
    logout_grok
    exit 0
fi

case "$1" in
    -h|--help)
        show_help
        ;;
    claude)
        shift
        logout_claude_dispatch "$@"
        ;;
    codex)
        logout_codex
        ;;
    gemini)
        logout_gemini
        ;;
    grok)
        logout_grok
        ;;
    *)
        say_err unknown_provider "$1"
        show_help
        exit 1
        ;;
esac

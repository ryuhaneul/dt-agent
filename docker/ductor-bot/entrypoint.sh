#!/bin/sh
# Create symlinks from ~/.{cli} to /ductor/.cli-auth/{cli}
# so CLI tools find their auth in the persistent volume.
#
# The default Claude link always points at the default slot.
# Per-call CLAUDE_CONFIG_DIR selects any alternate account.

AUTH_DIR="/ductor/.cli-auth"
mkdir -p "$AUTH_DIR/claude" "$AUTH_DIR/codex" "$AUTH_DIR/gemini"

# Keep framework home references on the persistent mount.
link="$HOME/.ductor"
if [ -L "$link" ] || [ ! -e "$link" ]; then
    ln -sfn /ductor "$link"
else
    printf 'WARNING: %s exists and is not a symlink; leaving it unchanged\n' "$link" >&2
fi

# Claude stays on the default target.
link="$HOME/.claude"
target="$AUTH_DIR/claude"
if [ -L "$link" ] || [ ! -e "$link" ]; then
    ln -sfn "$target" "$link"
fi

# codex / gemini / grok stay on default targets
for cli in codex gemini grok; do
    target="$AUTH_DIR/$cli"
    mkdir -p "$target" || { echo "ERROR: mkdir $target failed" >&2; exit 1; }
    if [ "$cli" = grok ]; then
        chmod 700 "$target" || { echo "ERROR: chmod $target failed" >&2; exit 1; }
    fi
    link="$HOME/.$cli"
    if [ -L "$link" ] || [ ! -e "$link" ]; then
        if ! ln -sfn "$target" "$link"; then
            [ "$cli" = grok ] && { echo "ERROR: symlink $link failed" >&2; exit 1; }
        fi
    elif [ "$cli" = grok ]; then
        echo "ERROR: $link exists and is not a symlink" >&2
        exit 1
    fi
done

# Persist Claude's account state in the mounted home.
if [ ! -e "$AUTH_DIR/claude.json" ]; then
    (umask 077; printf '{}' > "$AUTH_DIR/claude.json") || exit 1
fi
if [ -f "$HOME/.claude.json" ] && [ ! -L "$HOME/.claude.json" ]; then
    if [ "$(cat "$AUTH_DIR/claude.json")" = '{}' ]; then
        cat "$HOME/.claude.json" > "$AUTH_DIR/claude.json" || exit 1
        chmod 600 "$AUTH_DIR/claude.json" || exit 1
    fi
    rm -f "$HOME/.claude.json" || exit 1
fi
ln -sfn "$AUTH_DIR/claude.json" "$HOME/.claude.json" || exit 1

exec "$@"

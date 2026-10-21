# Dockerfile for the ductor bot and CLI execution environment.
# Install the repository source tree directly.
# CLI tools (Claude Code, Codex, Gemini, Grok) run in this container.

FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# System deps: git, curl, Node.js 24 LTS via NodeSource (Gemini CLI needs >=20)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git curl ca-certificates gnupg procps openssh-client \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install CLI tools globally; a failed CLI install fails the image build.
# CACHEBUST busts only the CLI-tool install layers when passed via
# --build-arg CACHEBUST=$(date +%s); earlier layers stay cached.
ARG CACHEBUST=0
RUN echo "cli-cachebust=${CACHEBUST}" && npm install -g @anthropic-ai/claude-code
RUN npm install -g @openai/codex
RUN npm install -g @google/gemini-cli

# xAI Grok CLI. The npm package is a thin trampoline: it materializes a ~158MB
# native binary into $GROK_HOME on first run and then execs it without a version
# check. $GROK_HOME doubles as the auth directory, which is bind-mounted from the
# host, so a materialized binary would outlive image rebuilds and pin the version
# forever. Relocate the binary into the image instead, so the image -- not the
# writable auth home -- owns the version, matching the other three CLIs.
RUN set -eu; \
    export GROK_HOME=/tmp/grok-build; \
    export npm_config_cache=/tmp/grok-npm-cache; \
    npm install -g @xai-official/grok; \
    VER="$(node -p "require('/usr/lib/node_modules/@xai-official/grok/package.json').version")"; \
    printf '%s\n' "$VER" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; \
    grok --version > /dev/null; \
    SRC="$(readlink -e "$GROK_HOME/bin/grok")"; \
    case "$SRC" in "$GROK_HOME"/bin/*) ;; *) exit 1 ;; esac; \
    [ -f "$SRC" ] && [ -x "$SRC" ]; \
    mkdir -p /usr/local/lib/grok; \
    mv "$SRC" /usr/local/lib/grok/grok; \
    chmod 0755 /usr/local/lib/grok/grok; \
    /usr/local/lib/grok/grok --version > /tmp/grok-v1.txt; \
    grep -Fq "grok $VER " /tmp/grok-v1.txt; \
    npm uninstall -g @xai-official/grok; \
    rmdir /usr/lib/node_modules/@xai-official 2>/dev/null || true; \
    printf '%s\n' \
        '#!/bin/sh' \
        '# Installed via npm; the binary was relocated so the image owns the version.' \
        'export GROK_MANAGED_BY_NPM=1' \
        'export GROK_DISABLE_AUTOUPDATER=1' \
        'exec /usr/local/lib/grok/grok "$@"' > /usr/bin/grok; \
    chmod 0755 /usr/bin/grok; \
    grok --version > /tmp/grok-v2.txt; \
    grep -Fq "grok $VER " /tmp/grok-v2.txt; \
    echo "grok CLI baked into image: $VER"; \
    rm -rf "$GROK_HOME" "$npm_config_cache" /tmp/grok-v1.txt /tmp/grok-v2.txt

# Install the checked-out fork source tree.
COPY pyproject.toml README.md LICENSE config.example.json /app/src/
COPY ductor_bot /app/src/ductor_bot
ARG DUCTOR_EXTRAS=
ENV DUCTOR_BUILD_EXTRAS=${DUCTOR_EXTRAS}
RUN set -eu; \
    if [ -n "$DUCTOR_EXTRAS" ]; then \
        case "$DUCTOR_EXTRAS" in *[!a-z,]*) echo "Invalid DUCTOR_EXTRAS" >&2; exit 1;; esac; \
        printf '%s' "$DUCTOR_EXTRAS" | grep -Eq '^[a-z]+(,[a-z]+)*$'; \
        case ",$DUCTOR_EXTRAS," in *,ssh,*) \
            echo "ssh extra was removed; openssh-client is included in the image by default" >&2; exit 1;; esac; \
        printf '%s' "$DUCTOR_EXTRAS" | tr ',' '\n' | grep -Ev '^(matrix|slack|api)$' | \
            { ! grep -q .; }; \
        pip install --no-cache-dir "/app/src[$DUCTOR_EXTRAS]"; \
    else \
        pip install --no-cache-dir /app/src; \
    fi; \
    rm -rf /app/src

# Non-root user
ARG DUCTOR_UID=1000
ARG DUCTOR_GID=1000
RUN set -eu; \
    if [ "$DUCTOR_UID" = 0 ]; then echo "Root is not supported; DUCTOR_UID must not be 0" >&2; exit 1; fi; \
    if ! getent group "$DUCTOR_GID" >/dev/null; then groupadd -g "$DUCTOR_GID" ductor; fi; \
    useradd -m -u "$DUCTOR_UID" -g "$DUCTOR_GID" ductor

# Entrypoint: creates auth symlinks before starting
COPY docker/ductor-bot/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

# Apply the same transcript retention policy to every Claude authentication slot.
COPY docker/ductor-bot/managed-settings/10-dt-agent-retention.json /etc/claude-code/managed-settings.d/10-dt-agent-retention.json
RUN chown root:root /etc/claude-code /etc/claude-code/managed-settings.d /etc/claude-code/managed-settings.d/10-dt-agent-retention.json \
    && chmod 0755 /etc/claude-code /etc/claude-code/managed-settings.d \
    && chmod 0644 /etc/claude-code/managed-settings.d/10-dt-agent-retention.json \
    && python3 -c 'import json; from pathlib import Path; value = json.loads(Path("/etc/claude-code/managed-settings.d/10-dt-agent-retention.json").read_text()); assert isinstance(value, dict) and set(value) == {"cleanupPeriodDays"} and type(value["cleanupPeriodDays"]) is int and value["cleanupPeriodDays"] == 36500, "Invalid Claude retention policy"'

USER ductor

# State directory inside the container (host ~/.ductor is mounted here)
ENV DUCTOR_HOME=/ductor

# Runtime directory (mounted from host)
WORKDIR /ductor

ENTRYPOINT ["entrypoint.sh"]
CMD ["ductor"]

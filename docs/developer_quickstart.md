# Developer Quickstart

## Runtime and tests

Deploy the current source through Docker Compose:

```bash
git clone https://github.com/ryuhaneul/dt-agent.git ductor
cd ductor
./scripts/onboard.sh
docker compose up -d --build
docker compose logs -f ductor-bot
```

Git, Bash, host Python 3.9+, Docker Engine, and Compose v2 are required for onboarding.
The image user uses the onboarding account's UID/GID (`DUCTOR_UID` and `DUCTOR_GID`,
each defaulting to 1000). See
[installation](installation.md) for configuration and provider login.
The source checkout's Python virtual environment is useful for tests,
without starting a separate host bot:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m compileall -q ductor_bot
```

## Read order

1. `ductor_bot/__main__.py`: CLI dispatch and config load
2. `ductor_bot/multiagent/supervisor.py`: main and sub-agent lifecycle
3. `ductor_bot/messenger/`: transport ingress
4. `ductor_bot/orchestrator/`: commands and message flows
5. `ductor_bot/cli/service.py`: provider subprocess gateway
6. `ductor_bot/bus/`, `session/`, `tasks/`: delivery and persistent work

The bot home is mounted at `/ductor` in the container. Workspace,
sessions, logs, and CLI authentication persist in the host `DUCTOR_HOME`.
Chat `/stop` and `/restart` are runtime commands; Compose controls the
container itself. Compose environment edits require service recreation;
`/restart` cannot refresh inherited container environment variables.

For development, [justfile](../justfile) defines `just fix`, `just check`,
`just test [args]`, and opt-in `just test-parallel [args]` (`just` >=1.42.0 and
`uv` required). `just check` runs the checks CI fails on (type check, translation keys, docs bundle,
script tests, shell syntax, tests); `just lint` runs ruff for information only. Prefer
sequential tests: parallel safety is not established.
The [full README](../README.md) covers operator scripts and the verification-only
`verify-clean-clone.sh` helper. Use an isolated clone and
private temporary home for checks; never share an operating bot home.

Sources: [onboarding](../scripts/onboard.py), [Dockerfile](../docker/ductor-bot/Dockerfile),
[Compose](../compose.yaml), [justfile](../justfile).

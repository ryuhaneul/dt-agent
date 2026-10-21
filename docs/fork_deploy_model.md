# DT Agent build and deployment model

The root `ductor_bot/` tree is the source installed into the bot image.
The root `tests/` tree tests that same source. The complete list of changes is in [CHANGES.md](CHANGES.md). The upstream
baseline and manual merge procedure are in [UPSTREAM.md](UPSTREAM.md).

## Image build

`compose.yaml` uses `docker/ductor-bot/Dockerfile`. Its build installs
the checked-out repository package with pip. The image also installs the
CLI tools and system packages specified in that Dockerfile. Edits to
`ductor_bot/` therefore reach the next rebuilt image directly.

The root `config.example.json` is included in the wheel. The former
nested sandbox Dockerfile is no longer packaged.

The image validates its fixed Claude transcript retention policy at build time. The policy directories are root-owned 0755 and the JSON file is root-owned 0644, so the runtime user can read but cannot modify them. The standard image installs `/etc/claude-code/managed-settings.d/10-dt-agent-retention.json` with `cleanupPeriodDays: 36500`, covering every in-container Claude invocation regardless of slot (bot calls, streaming, one-shot tasks, sub-agents, login and direct execution). Higher-priority managed sources such as organization server policies may supersede this local policy; administrators can deliberately override the value with a later-named drop-in in the same directory ([how Claude Code combines managed sources](https://code.claude.com/docs/en/managed-settings#how-claude-code-combines-managed-sources)).

## Development and verification

Work in a separate Git worktree. Keep the operating worktree on its
deployment branch. Use Python 3.11 and install the repository in a
worktree-specific virtual environment for tests and static checks.
`scripts/verify-clean-clone.sh` now verifies a committed ref in an isolated
clone/home/project. Its offline stages require no live bot token (image builds
still need network/package access). See the [README verification reference](../README.md#11-development-and-isolated-verification-scripts). For example, from a
clean checkout:

```bash
./scripts/verify-clean-clone.sh HEAD --mode offline --stage offline
```

The verifier overrides container, image, and network names into `ductorverify*`
and cleans its resources. A Compose project name alone does not isolate the
base file's fixed container/network names. Do not use the operating home or
run a second polling process with the same bot token.

## Deployment

`scripts/update.sh` fetches `origin`, compares the current tracking branch,
uses `git pull --ff-only`, warns about observed active work, prompts before
replacement when the bot container exists, and builds/swaps both services.
`scripts/rebuild.sh` builds/swaps without pulling source or prompting for active
work. Both refresh CLI packages through the image's build path; neither provides
an automatic rollback. Home files persist, but running work can be interrupted
and delivery/recovery has known limits.

Use the [installation guide](installation.md) for a new deployment. The operating Compose project,
containers and home are not used for development checks.

Sources: [Dockerfile](../docker/ductor-bot/Dockerfile),
[Compose](../compose.yaml), [update](../scripts/update.sh),
[rebuild](../scripts/rebuild.sh), [verifier](../scripts/verify-clean-clone.sh).

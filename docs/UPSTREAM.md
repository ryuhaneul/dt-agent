# Upstream synchronization

DT Agent's source baseline is PleasePrompto/ductor v0.20.1 at
`cdd63cc671fa986d2ffd0d47e97881c73dd8c2ec`. The repository's
`ductor_bot/` tree is now the image build input. Its fork-specific changes
are indexed in [CHANGES.md](CHANGES.md). The source pin
is provenance metadata, not proof that upstream commits share this fork's Git ancestry.

## Manual merge of a new upstream release

1. In a separate worktree, add the upstream remote if absent:
   `git remote add upstream https://github.com/PleasePrompto/ductor.git`.
2. Check tags manually with `git ls-remote --tags upstream`. Set `UPSTREAM_TAG`
   to the reviewed tag, then fetch only that tag:
   `git fetch upstream tag "$UPSTREAM_TAG"`.
3. Check `git merge-base HEAD "refs/tags/$UPSTREAM_TAG"`. If no common ancestor
   exists, stop: source materialization does not establish Git ancestry. Do not
   add `--allow-unrelated-histories` blindly; a separate reviewed source-diff
   integration is needed. With confirmed shared history, start without committing:
   `git merge --no-ff --no-commit "refs/tags/$UPSTREAM_TAG"`.
4. Resolve source and test conflicts by comparing the upstream change with
   the current behavior in CHANGES.md and code. Update the change inventory and this upstream
   note with the reviewed baseline and merge history; retain historical records.
5. Run source checks while resolving conflicts. The clean-clone verifier rejects
   dirty worktrees, so create the local integration commit before using it.
6. Verify that committed ref with
   `./scripts/verify-clean-clone.sh HEAD --mode offline --stage offline` (tests and
   isolated image checks). Offline mode uses no live bot token; image/package
   downloads may still require network access. Do not publish/deploy on failure;
   correct failures in a new local commit and verify the new ref.
7. Review relevant routing/lifecycle changes and choose any required live
   regression scope separately. Historic PASS records apply to their recorded
   refs. Push and deployment require their separate operational decision.

Do not restore `manifest.toml`, `overlay/`, patch replay or the deleted automatic
upstream-check workflow as a current build step. Image inputs come from the
tracked source. Sources: [Dockerfile](../docker/ductor-bot/Dockerfile),
[verifier arguments/clean-tree check](../scripts/verify-clean-clone.sh).

The previous upstream note identified Telegram send retry, streaming
finalization, session-preserving flows, task result injection, bus behavior,
and workspace initialization as historical conflict areas. Re-check each
against the actual new tag; this list is not a merge rule.

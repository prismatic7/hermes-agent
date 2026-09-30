#!/usr/bin/env bash
# Carry the LIVE CHECKOUT's local-only commits into the fork branch being built.
#
# Why this exists: the live checkout is a deployment mirror, but an agent session
# working in it leaves real commits. Those were previously stranded — the sync
# merges upstream into the FORK, so the live work never joined the carried set,
# and a later `--ff-only` against a fork that had moved on failed. A checkout
# that is clean at 01:00 is not guaranteed one at 01:01, which is exactly the
# race that lost `56dc2b79bb`'s members_stamp fix (see sync-hermes-fork.sh).
#
# Run by sync-hermes-fork.sh in the leader path, before upstream is merged and
# before the fork is pushed. Also safe to run by hand:
#
#   scripts/fork-sync-preserve-live-commits.sh <scratch-clone> <live-checkout>
#
# Exit 0 when there was nothing to carry or the carry succeeded.
# Exit 1 on a conflict, with the merge aborted so the caller's tree is clean and
# nothing half-merged can be pushed.
set -uo pipefail

SCRATCH="${1:-}"
LIVE="${2:-}"

die() { printf 'preserve-live: %s\n' "$1" >&2; exit "${2:-2}"; }

[ -n "$SCRATCH" ] || die "usage: $0 <scratch-clone> <live-checkout>"
[ -n "$LIVE" ] || die "usage: $0 <scratch-clone> <live-checkout>"
[ -d "$SCRATCH/.git" ] || die "$SCRATCH is not a git clone"
[ -d "$LIVE/.git" ] || die "$LIVE is not a git checkout"

BRANCH="$(git -C "$SCRATCH" rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" != "HEAD" ] || die "the scratch clone is on a detached HEAD"

# Fetch the live checkout's branch explicitly. It is NOT the scratch clone's
# origin any more — that was repointed at GitHub so the merge targets real
# upstream — so the live tip has to be brought in by path.
#
# The branch is taken from the scratch clone rather than hardcoded: the caller
# checks out the branch it cares about, and a hardcoded name silently fetches
# nothing when it differs.
git -C "$SCRATCH" fetch --quiet "$LIVE" \
  "$BRANCH:refs/remotes/live/$BRANCH" \
  || die "could not fetch '$BRANCH' from $LIVE"

LIVE_TIP="refs/remotes/live/$BRANCH"
AHEAD="$(git -C "$SCRATCH" rev-list --count "$BRANCH..$LIVE_TIP" 2>/dev/null || echo '?')"
[ "$AHEAD" != "?" ] || die "could not compare $BRANCH with the live tip"

if [ "$AHEAD" -eq 0 ]; then
  echo "preserve-live: nothing to carry (live checkout adds no commit to $BRANCH)"
  exit 0
fi

echo "preserve-live: carrying $AHEAD local-only commit(s) from the live checkout:"
git -C "$SCRATCH" log --oneline "$BRANCH..$LIVE_TIP" | sed 's/^/    /'

if git -C "$SCRATCH" merge --no-edit "$LIVE_TIP" >/dev/null 2>&1; then
  echo "preserve-live: carried -> $(git -C "$SCRATCH" rev-parse --short HEAD)"
  exit 0
fi

# Capture the conflicted paths BEFORE aborting — afterwards they are gone.
CONFLICTED="$(git -C "$SCRATCH" diff --name-only --diff-filter=U 2>/dev/null | tr '\n' ' ')"
git -C "$SCRATCH" merge --abort 2>/dev/null || true
printf 'preserve-live: CONFLICT carrying the live checkout'"'"'s commits: %s\n' "$CONFLICTED" >&2
printf 'preserve-live: resolve by hand, then re-run:  cd %s && git push fork HEAD:%s\n' \
  "$LIVE" "$BRANCH" >&2
exit 1

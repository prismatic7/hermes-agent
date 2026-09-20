#!/usr/bin/env bash
# sync-hermes-fork.sh
# Keep prismatic7/hermes-agent fork and the local live Hermes checkout in sync
# with upstream NousResearch/hermes-agent, WITHOUT ever mutating the live
# checkout from a background/cron process.
#
# ── ROLES ────────────────────────────────────────────────────────────────────
# Exactly ONE host is the merge authority. Every other host follows it.
#
#   leader   (sma)         merge upstream main into fork/customizations,
#                          push main + customizations, then fast-forward the
#                          live checkout to the result.
#   follower (horza, work) NO merge, NO push. Fetch the fork and fast-forward
#                          the live checkout to whatever the leader pushed.
#
# Why the split: all three hosts used to run the merge-and-push path, so the
# customizations branch was rewritten three times a night and each host raced
# the others on the push (the `push_fork` race tolerance below is a scar from
# that). A merge/rebase resolution is a single global decision — it belongs to
# one host. Followers only ever read.
#
# ── MERGE, NOT REBASE (2026-09-21) ───────────────────────────────────────────
# This script used to rebase fork/customizations onto upstream main. That
# strategy cannot win against a fast-moving upstream: a rebase replays every
# carried commit, so each upstream commit touching the same region produces a
# FRESH conflict, and the job that was green last night reddens tonight —
# forever. Observed upstream advance through five tips in ~40 minutes
# (297ebd506e -> 453b1dec2b -> 6ae1fab336 -> 1536e75bfe -> a23984887a) while
# the fork sat ~39-379 commits behind.
#
# A merge resolves the divergence ONCE. Later upstream commits that don't
# re-touch those regions merge cleanly, and history is never rewritten — so no
# force-push, and the live checkout advances by fast-forward instead of
# `reset --hard`. Merge commits in fork history are the intended cost.
#
# The role is NOT baked into this file. It is passed by the host-local wrapper
# in ~/.hermes/scripts/ ($FORK_SYNC_ROLE), because this file is version-
# controlled on `customizations` and so is replaced on every sync — anything
# hardcoded here propagates to every host. The wrapper is not in the repo and
# survives every sync.
#
#   leader   wrapper: exec .../sync-hermes-fork.sh                  # default
#   follower wrapper: exec .../sync-hermes-fork.sh --follower
#
# ── SCHEDULING ───────────────────────────────────────────────────────────────
# Daily, 30 minutes apart, leader first (upstream moves several hundred
# commits/day — a weekly cadence is orders of magnitude too slow):
#   sma    0 1 * * *   leader
#   horza 30 1 * * *   follower
#   work   0 2 * * *   follower
#
# ── OUTPUT CONTRACT ──────────────────────────────────────────────────────────
# The script logs verbosely to $LOG and emits ONE summary line to stdout, which
# is what a `no_agent` cron job delivers. Previously it redirected ALL of its
# own output into the log, so stdout was always empty, so the job was always
# recorded as "silent" — a successful run was indistinguishable from a run that
# never happened. fd 3 is captured before the redirect to fix that.
#
#   silent              = the script did not run at all
#   "fork-sync[..] ok"     = it ran and changed something
#   "fork-sync[..] no-op"  = it ran, nothing to do
#   "fork-sync[..] SKIPPED"= it ran but declined to touch a dirty checkout
#   non-zero exit       = it ran and failed (cron reports "script failed")
#
# Git layout (same on every host):
#   origin = NousResearch/hermes-agent (upstream, read-only)
#   fork   = prismatic7/hermes-agent    (our fork, writable)
#   customizations = our custom commits merged with upstream main
#
# Safety: never touches the live checkout's working tree when dirty; aborts on
# scratch-clone merge conflicts (leaving the fork untouched); never force-pushes
# and never force-resets a dirty live checkout.
# Dry-run: pass --dry-run to preview without making changes.

set -eo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_DIR="$HERMES_HOME/hermes-agent"
LOG="$HERMES_HOME/logs/fork-sync.log"
TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"
HOST_LABEL="${FORK_SYNC_LABEL:-$(hostname -s)}"

# Disk-backed scratch (NOT /tmp — that's tmpfs and dependency installs can
# ENOSPC it). Same location the agent's self-repo-guard suggests.
SCRATCH_ROOT="${HERMES_HOME}/scratch"
SCRATCH_CLONE="${SCRATCH_ROOT}/hermes-fork-sync"

DRY_RUN=false
# Role may come from the host-local wrapper's environment or from an explicit
# flag; the flag wins if both are present.
ROLE="${FORK_SYNC_ROLE:-leader}"
for arg in "$@"; do
  case "$arg" in
    --dry-run)  DRY_RUN=true ;;
    --follower) ROLE=follower ;;
    --leader)   ROLE=leader ;;
    *) echo "sync-hermes-fork.sh: unknown argument '$arg'" >&2; exit 2 ;;
  esac
done
case "$ROLE" in
  leader|follower) ;;
  *) echo "sync-hermes-fork.sh: invalid role '$ROLE' (want leader|follower)" >&2; exit 2 ;;
esac

mkdir -p "$(dirname "$LOG")" "$SCRATCH_ROOT"
# Capture the cron-visible stdout on fd 3 BEFORE redirecting everything into the
# log. emit() writes the same line to both: the log keeps full detail, fd 3 is
# the single line the scheduler delivers.
exec 3>&1
exec >> "$LOG" 2>&1

# emit <text> — one-line summary to the cron-visible stream AND the log.
emit() { printf '%s\n' "$*" >>"$LOG"; printf '%s\n' "$*" >&3; }

echo "=== $TIMESTAMP (role=$ROLE dry_run=$DRY_RUN host=$HOST_LABEL) ==="

fail() { echo "ERROR: $*"; emit "fork-sync[$HOST_LABEL/$ROLE] FAILED: $*"; exit 1; }
push_fork() {  # push_fork <refspec> <label> — plain (non-forcing) push that
               # tolerates a same-content race: if another host pushed the
               # IDENTICAL content first, we are rejected because the remote
               # moved — accept it and continue rather than failing. Leader-only
               # in practice (followers never push), kept as a safety net for a
               # hand-run leader on a second host.
               #
               # Deliberately NOT --force-with-lease: the merge workflow never
               # rewrites fork history, so a force push would only ever discard
               # commits. A rejection here means something genuinely needs a
               # human, never a routine nightly rebase rewrite.
  local refspec="$1" label="$2"
  if git -C "$SCRATCH_CLONE" push --quiet fork "$refspec" 2>&1; then
    echo "  $label <- pushed"
    return 0
  fi
  echo "  push to $label rejected — re-fetching to check for a race ..."
  git -C "$SCRATCH_CLONE" fetch --quiet fork customizations main 2>&1 || fail "re-fetch fork failed"
  local remote_ref local_ref
  case "$refspec" in
    "origin/main:main")    remote_ref="fork/main";           local_ref="origin/main" ;;
    "HEAD:customizations") remote_ref="fork/customizations"; local_ref="HEAD" ;;
    *) fail "push_fork: unknown refspec $refspec" ;;
  esac
  if [ "$(git -C "$SCRATCH_CLONE" rev-parse "$remote_ref^{tree}")" = "$(git -C "$SCRATCH_CLONE" rev-parse "$local_ref^{tree}")" ]; then
    echo "  remote $label already carries the identical tree (another host won the race) — continuing"
    return 0
  fi
  fail "push to $label rejected and remote differs — resolve manually"
}

# ── 1. Pre-flight: live checkout exists, is clean, and is on customizations ──
[ -d "$HERMES_DIR/.git" ] || fail "$HERMES_DIR is not a git repository"
LIVE_BRANCH="$(git -C "$HERMES_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
[ "$LIVE_BRANCH" != "HEAD" ] || fail "live checkout is on detached HEAD"
echo "Live checkout: branch='$LIVE_BRANCH'"

# The live tree must be clean before we consider moving it. Note we do NOT
# reset it here if it's dirty — we just won't touch it.
LIVE_DIRTY=false
if [ -n "$(git -C "$HERMES_DIR" status --porcelain)" ]; then
  LIVE_DIRTY=true
  echo "WARNING: live checkout has uncommitted changes — will not update it (leaving as-is)."
  git -C "$HERMES_DIR" status --porcelain | head -20
fi

# ════════════════════════════════════════════════════════════════════════════
# FOLLOWER PATH — read-only. Fetch the fork, fast-forward the live checkout.
# No scratch clone, no merge, no push.
# ════════════════════════════════════════════════════════════════════════════
if [ "$ROLE" = "follower" ]; then
  echo ""
  echo "Follower mode: pulling fork/customizations (no merge, no push)."

  if $DRY_RUN; then
    echo "  [DRY RUN] would fetch fork customizations and fast-forward the live checkout"
    echo "=== done (dry run) ==="
    emit "fork-sync[$HOST_LABEL/$ROLE] DRY RUN: would pull fork/customizations"
    exit 0
  fi

  git -C "$HERMES_DIR" fetch --quiet fork customizations 2>&1 || fail "fetch fork failed"
  TARGET_SHA="$(git -C "$HERMES_DIR" rev-parse fork/customizations)"
  LIVE_SHA="$(git -C "$HERMES_DIR" rev-parse HEAD)"
  echo "  fork/customizations: $(git -C "$HERMES_DIR" rev-parse --short "$TARGET_SHA")"
  echo "  live checkout:       $(git -C "$HERMES_DIR" rev-parse --short "$LIVE_SHA")"

  if [ "$LIVE_SHA" = "$TARGET_SHA" ]; then
    echo "  already current — nothing to do."
    echo "=== done (no update needed) ==="
    emit "fork-sync[$HOST_LABEL/$ROLE] no-op: already at $(git -C "$HERMES_DIR" rev-parse --short "$TARGET_SHA")"
    exit 0
  fi

  if [ "$LIVE_DIRTY" = "true" ]; then
    echo "  SKIP: live checkout is dirty — not touching it."
    echo "  Manual: cd $HERMES_DIR && git stash && git fetch fork customizations && git merge --ff-only fork/customizations && git stash pop"
    echo "=== done (skipped) ==="
    emit "fork-sync[$HOST_LABEL/$ROLE] SKIPPED: live checkout dirty (fork tip $(git -C "$HERMES_DIR" rev-parse --short "$TARGET_SHA"))"
    exit 0
  fi

  if [ "$LIVE_BRANCH" != "customizations" ]; then
    echo "  SKIP: live checkout is on '$LIVE_BRANCH', not 'customizations' — not switching branches from cron."
    echo "  Manual: cd $HERMES_DIR && git fetch fork customizations && git checkout customizations && git merge --ff-only fork/customizations"
    echo "=== done (skipped) ==="
    emit "fork-sync[$HOST_LABEL/$ROLE] SKIPPED: on branch '$LIVE_BRANCH' (fork tip $(git -C "$HERMES_DIR" rev-parse --short "$TARGET_SHA"))"
    exit 0
  fi

  # The leader normally MERGES upstream into customizations, so fork history is
  # appended to and the live checkout moves forward by fast-forward.
  #
  # BUT a fork branch that was ever rebuilt (cherry-picked/rebased upstream of
  # this run) has a tip the live checkout does not descend from, so --ff-only
  # legitimately fails. The fork is the authority and this checkout is a
  # deployment mirror, so fall back to `reset --hard` — but ONLY here, i.e.
  # after confirming the tree is clean and on the right branch, and never
  # silently: the discarded count is reported.
  DISCARDED="$(git -C "$HERMES_DIR" rev-list --count fork/customizations..HEAD 2>/dev/null || echo '?')"
  if git -C "$HERMES_DIR" merge --ff-only --quiet fork/customizations >/dev/null 2>&1; then
    echo "  live checkout fast-forwarded -> $(git -C "$HERMES_DIR" rev-parse --short HEAD)"
    echo "  NOTE: restart Hermes for the updated code to load."
    echo ""
    echo "=== Sync complete (follower) ==="
    echo "=== done ==="
    emit "fork-sync[$HOST_LABEL/$ROLE] ok: customizations $(git -C "$HERMES_DIR" rev-parse --short "$LIVE_SHA") -> $(git -C "$HERMES_DIR" rev-parse --short HEAD)"
  elif git -C "$HERMES_DIR" reset --hard --quiet fork/customizations >/dev/null 2>&1; then
    echo "  live checkout reset -> $(git -C "$HERMES_DIR" rev-parse --short HEAD)"
    echo "  (fork branch had been rebuilt, so no fast-forward existed; $DISCARDED"
    echo "   local commit(s) were left behind — the fork is the authority.)"
    echo "  NOTE: restart Hermes for the updated code to load."
    echo ""
    echo "=== Sync complete (follower) ==="
    echo "=== done ==="
    emit "fork-sync[$HOST_LABEL/$ROLE] ok (reset): customizations $(git -C "$HERMES_DIR" rev-parse --short "$LIVE_SHA") -> $(git -C "$HERMES_DIR" rev-parse --short HEAD)"
  else
    echo "  SKIP: cannot advance (fast-forward and reset both failed)."
    echo "  Manual: cd $HERMES_DIR && git fetch fork customizations && git merge --ff-only fork/customizations"
    fail "could not update live checkout to fork/customizations"
  fi
  exit 0
fi

# ════════════════════════════════════════════════════════════════════════════
# LEADER PATH — the merge authority. Everything below may mutate the fork.
# ════════════════════════════════════════════════════════════════════════════

# ── 2. (Re)build the scratch clone ──
# Recreate it each run so a partial/aborted prior run can't poison the next.
# The clone itself is a harmless shared clone (no push, never the live tree),
# so it runs even in dry-run — it's what lets us report the real fork state.
echo ""
echo "Preparing scratch clone at $SCRATCH_CLONE ..."
if [ -d "$SCRATCH_CLONE/.git" ]; then
  rm -rf "$SCRATCH_CLONE"
fi
# Clone from the live checkout for speed (shared clone — no re-download), but
# the live checkout's origin points at its own stale refs. Point this clone's
# origin at the REAL upstream GitHub URL so the merge targets true latest.
git clone --quiet --no-checkout --origin origin "$HERMES_DIR" "$SCRATCH_CLONE" 2>&1 \
  || fail "scratch clone failed"
git -C "$SCRATCH_CLONE" remote set-url origin "git@github.com:NousResearch/hermes-agent.git"
git -C "$SCRATCH_CLONE" remote add fork git@github.com:prismatic7/hermes-agent.git

# Upstream violates its own .gitattributes: it declares `*.yaml text eol=lf`,
# but the committed blob for plugin-catalog/intelligent-tool-break.yaml still
# carries CRLF. Checking it out therefore dirties the worktree on every run and
# aborts the sync with "local changes would be overwritten by merge" — no
# CONFLICT line, nothing in --diff-filter=U, which makes it look like a phantom.
# A local (uncommitted) attributes override disables EOL conversion for that
# path only, so the worktree matches the blob byte-for-byte and stays clean.
printf '%s\n' "plugin-catalog/intelligent-tool-break.yaml -text" \
  >> "$SCRATCH_CLONE/.git/info/attributes"

# ── 3. Fetch upstream + fork refs (read-only — always runs) ──
echo ""
echo "Fetching upstream (origin) and fork ..."
git -C "$SCRATCH_CLONE" fetch --quiet origin main 2>&1 || fail "fetch origin failed"
git -C "$SCRATCH_CLONE" fetch --quiet fork main customizations 2>&1 || fail "fetch fork failed"
UPSTREAM_SHA="$(git -C "$SCRATCH_CLONE" rev-parse origin/main)"
FORK_CUSTOM_SHA="$(git -C "$SCRATCH_CLONE" rev-parse fork/customizations)"

# ── 4. Merge upstream into the fork's customizations (scratch clone) ──
# The fork's customizations branch is the source of truth (the leader may hold
# local tweaks; the fork is authoritative). We merge the fresh origin/main into
# fork/customizations here — never in the live tree.
#
# Why merge, not rebase: upstream moves several times an hour. A rebase replays
# every carried commit and hits a fresh conflict each time upstream touches the
# same region, so a nightly rebase job that is green today reddens tonight —
# repeatedly, forever. A merge resolves the divergence ONCE; later upstream
# commits that don't re-touch those regions merge cleanly. Merge also does not
# rewrite fork history, so no force-push is needed.
echo ""
echo "Merging origin/main into fork/customizations ..."
echo "  upstream tip:        $(git -C "$SCRATCH_CLONE" rev-parse --short origin/main)"
echo "  fork customizations: $(git -C "$SCRATCH_CLONE" rev-parse --short fork/customizations)"
echo "  fork carried:        $(git -C "$SCRATCH_CLONE" rev-list --count origin/main..fork/customizations) commit(s)"

# The scratch clone starts at the live checkout's own local state (which may be
# diverged on this host), so first reset the scratch branch to exactly
# fork/customizations. This makes the merge correct no matter what local
# tweaks this particular machine holds.
git -C "$SCRATCH_CLONE" checkout --quiet -B customizations fork/customizations
# The scratch clone is disposable, so force a pristine tree — shared-clone
# artifacts (e.g. machine-specific tracked symlinks like contributors/emails/)
# can otherwise leave the tree "dirty" and block the merge.
git -C "$SCRATCH_CLONE" reset --hard --quiet HEAD
git -C "$SCRATCH_CLONE" clean -fd --quiet
# macOS case-insensitive-filesystem workaround: upstream used to track two
# contributors/emails/ files that differ only by case
# (agent@Agents-Mac-mini.local / agent@agents-Mac-mini.local). They collide on
# macOS, so the tree can never be clean while both are in the index.
#   - If upstream STILL tracks them: mark both skip-worktree so git ignores
#     the phantom diff.
#   - If upstream REMOVED them (2026-08+): skip-worktree would block the
#     merge ("local changes would be overwritten" — the target deletes the
#     files). Instead drop them from the fork's customizations entirely
#     (index + disk + removal commit) so the merge of origin/main can
#     proceed. The removal commit reconciles cleanly (carried commits don't
#     touch these files) and permanently fixes the fork.
if [[ "$(uname -s)" == "Darwin" ]]; then
  if git -C "$SCRATCH_CLONE" cat-file -e "origin/main:contributors/emails/agent@Agents-Mac-mini.local" 2>/dev/null \
     && git -C "$SCRATCH_CLONE" cat-file -e "origin/main:contributors/emails/agent@agents-Mac-mini.local" 2>/dev/null; then
    git -C "$SCRATCH_CLONE" update-index --skip-worktree \
      "contributors/emails/agent@Agents-Mac-mini.local" \
      "contributors/emails/agent@agents-Mac-mini.local" 2>/dev/null || true
  else
    echo "  (upstream no longer tracks the case-colliding email files — removing them from fork customizations)"
    git -C "$SCRATCH_CLONE" rm --cached --ignore-unmatch --quiet \
      "contributors/emails/agent@Agents-Mac-mini.local" \
      "contributors/emails/agent@agents-Mac-mini.local" 2>/dev/null || true
    rm -f "$SCRATCH_CLONE/contributors/emails/agent@Agents-Mac-mini.local" \
          "$SCRATCH_CLONE/contributors/emails/agent@agents-Mac-mini.local"
    git -C "$SCRATCH_CLONE" commit --quiet -m "chore: drop stale case-colliding email files (removed upstream)" 2>/dev/null || true
  fi
fi
if $DRY_RUN; then
  echo "  [DRY RUN] would merge origin/main into customizations"
else
  # Merge upstream in. `--no-edit` keeps the default merge message so the
  # history records exactly when and what was reconciled.
  if ! git -C "$SCRATCH_CLONE" merge --no-edit --quiet origin/main; then
    # A conflicting merge is a real divergence needing a human decision. Abort
    # so the scratch clone is left clean and the fork untouched — never push a
    # half-merged tree.
    git -C "$SCRATCH_CLONE" merge --abort 2>/dev/null || true
    CONFLICTED="$(git -C "$SCRATCH_CLONE" diff --name-only --diff-filter=U 2>/dev/null | tr '\n' ' ')"
    fail "merge conflict with upstream $(git -C "$SCRATCH_CLONE" rev-parse --short origin/main)${CONFLICTED:+ on: $CONFLICTED} — resolve manually, then re-run"
  fi
  echo "  merged -> $(git -C "$SCRATCH_CLONE" rev-parse --short HEAD)"
fi

NEW_CUSTOM_SHA="$(git -C "$SCRATCH_CLONE" rev-parse HEAD)"

# Nothing to do when upstream is already merged into the carried set and the
# branch is unchanged. (With a merge workflow, "up to date" means upstream is
# already an ancestor — not that the two SHAs are equal.)
if git -C "$SCRATCH_CLONE" merge-base --is-ancestor origin/main customizations 2>/dev/null \
   && [ "$FORK_CUSTOM_SHA" = "$NEW_CUSTOM_SHA" ]; then
  echo ""
  echo "Already up to date (no upstream changes, carried set unchanged)."
  echo "=== done (no update needed) ==="
  emit "fork-sync[$HOST_LABEL/$ROLE] no-op: upstream and customizations unchanged at $(git -C "$SCRATCH_CLONE" rev-parse --short "$NEW_CUSTOM_SHA")"
  exit 0
fi

# ── 5. Update fork main + push customizations ──
echo ""
echo "Syncing fork ..."
if $DRY_RUN; then
  echo "  [DRY RUN] would push:"
  echo "    - main            -> fork/main            (force, tracks upstream)"
  echo "    - customizations  -> fork/customizations  (merge result)"
else
  push_fork "origin/main:main" "fork/main"
  echo "  fork/main <- origin/main ($(git -C "$SCRATCH_CLONE" rev-parse --short origin/main))"
  push_fork "HEAD:customizations" "fork/customizations"
  echo "  fork/customizations <- merged ($(git -C "$SCRATCH_CLONE" rev-parse --short HEAD))"
fi

# ── 6. Update the live checkout (only if safe) ──
# The merge workflow appends to fork history rather than rewriting it, so the
# live checkout moves forward with a fast-forward — nothing can be discarded.
# `--ff-only` refuses if a real merge would be needed, which is exactly right
# from cron: bail out and print manual steps rather than invent a merge commit
# on the live tree. We only touch it when the tree is clean and we're already on
# `customizations`. (A running Hermes process keeps using its already-loaded
# modules until restart, so this takes effect on next restart.)
echo ""
echo "Updating live checkout ..."
LIVE_SKIP_REASON=""
if $DRY_RUN; then
  echo "  [DRY RUN] would fast-forward live checkout to fork/customizations"
elif [ "$LIVE_DIRTY" = "true" ]; then
  LIVE_SKIP_REASON="live checkout dirty"
  echo "  SKIP: live checkout is dirty — not touching it."
  echo "  Manual: cd $HERMES_DIR && git stash && git fetch fork customizations && git merge --ff-only fork/customizations && git stash pop"
elif [ "$LIVE_BRANCH" != "customizations" ]; then
  LIVE_SKIP_REASON="on branch '$LIVE_BRANCH'"
  echo "  SKIP: live checkout is on '$LIVE_BRANCH', not 'customizations' — not switching branches from cron."
  echo "  Manual: cd $HERMES_DIR && git fetch fork customizations && git checkout customizations && git merge --ff-only fork/customizations"
elif [ "$(git -C "$HERMES_DIR" rev-parse HEAD)" = "$NEW_CUSTOM_SHA" ]; then
  echo "  live checkout already at fork/customizations ($(git -C "$HERMES_DIR" rev-parse --short HEAD))"
else
  # Fetch the freshly-merged fork ref into the live checkout, then fast-forward.
  git -C "$HERMES_DIR" fetch --quiet fork customizations 2>&1 || fail "live fetch failed"
  if git -C "$HERMES_DIR" merge --ff-only --quiet fork/customizations >/dev/null 2>&1; then
    echo "  live checkout fast-forwarded -> $(git -C "$HERMES_DIR" rev-parse --short HEAD)"
    echo "  NOTE: restart Hermes for the updated code to load."
  else
    LIVE_SKIP_REASON="fast-forward not possible"
    echo "  SKIP: live checkout cannot fast-forward (local commits on this host?)."
    echo "  Manual: cd $HERMES_DIR && git fetch fork customizations && git merge --ff-only fork/customizations"
  fi
fi

# ── 7. Report ──
echo ""
echo "=== Sync complete ==="
echo "  Upstream:        origin/main @ $(git -C "$SCRATCH_CLONE" rev-parse --short origin/main)"
echo "  Fork customizations: $(git -C "$SCRATCH_CLONE" rev-parse --short HEAD) (carried: $(git -C "$SCRATCH_CLONE" rev-list --count origin/main..HEAD) commit(s))"
echo "  Carried commits:"
git -C "$SCRATCH_CLONE" log --oneline origin/main..HEAD 2>/dev/null
echo ""
echo "=== done ==="

CARRIED="$(git -C "$SCRATCH_CLONE" rev-list --count origin/main..HEAD)"
UP_SHORT="$(git -C "$SCRATCH_CLONE" rev-parse --short origin/main)"
NEW_SHORT="$(git -C "$SCRATCH_CLONE" rev-parse --short HEAD)"
if $DRY_RUN; then
  emit "fork-sync[$HOST_LABEL/$ROLE] DRY RUN: would push customizations $NEW_SHORT onto upstream $UP_SHORT (carried: $CARRIED)"
elif [ -n "$LIVE_SKIP_REASON" ]; then
  emit "fork-sync[$HOST_LABEL/$ROLE] ok (fork pushed): upstream $UP_SHORT -> customizations $NEW_SHORT (carried: $CARRIED); live checkout NOT updated ($LIVE_SKIP_REASON)"
else
  emit "fork-sync[$HOST_LABEL/$ROLE] ok: upstream $UP_SHORT -> customizations $NEW_SHORT (carried: $CARRIED)"
fi

# Cleanup scratch clone (only on real runs; keep it for dry-run inspection)
if ! $DRY_RUN; then
  rm -rf "$SCRATCH_CLONE"
fi

#!/usr/bin/env bash
# reconcile-hermes-fork-install.sh
# Close the gap that fork-sync opens: upstream code lands in the live checkout
# but the venv's editable install and its dependency set are never regenerated.
#
# ── WHY THIS EXISTS ──────────────────────────────────────────────────────────
# Two incidents in two days, one mechanism:
#
#   2026-09-23 (sma)  fork-sync carried upstream code introducing the top-level
#                     hermes_platform package. The venv's editable finder still
#                     mapped the previous 27 packages, so
#                     hermes_constants.py:16 raised ModuleNotFoundError on the
#                     first FRESH import. The running gateway was already
#                     running (imports happened at boot, days earlier), so
#                     nothing surfaced until a restart — then launchd
#                     crash-looped. Invisible until the restart is what makes
#                     this class expensive: the code change and the symptom are
#                     days apart.
#
#   2026-09-24 (sma+horza)  dependency gaps. The repair for the above
#                     (`pip install -e . --no-deps`) is mandatory — it keeps the
#                     blast radius off the dependency tree, per pyproject.toml —
#                     but it regenerates the package mapping WITHOUT installing
#                     dependencies added between revisions. Closures needed:
#                     snowballstemmer, pillow-heif, cryptography 48.0.1→50.0.0
#                     (five CVE/GHSA fixes), nemo-relay, firecrawl-anydoc.
#                     These do NOT crash anything; they silently degrade
#                     features (tool_search's BM25 assembly goes dark).
#
# Both were found by hand, twice, and repaired ad hoc. This script makes the
# gap un-creatable: any sync that changed the checkout gets its install
# regenerated and its dependencies reconciled in the same run.
#
# ── SEQUENCE (order is load-bearing) ────────────────────────────────────────
#   1. audit            read-only. Decides whether anything is needed at all.
#   2. backup           editable finder + .pth + dist-info + version snapshot.
#   3. regenerate       pip install -e . --no-deps   (regenerates the finder)
#   4. reconcile        pip install -e . --no-input  (installs the deps that
#                       step 3 deliberately skipped)
#   5. audit AGAIN      because step 3/4 regenerate the finder, a naive repair
#                       can re-break what the previous night fixed. Verify
#                       after, never before.
#
# ── WHEN IT MUTATES ─────────────────────────────────────────────────────────
# Only when (a) the caller says the sync changed something, or (b) the audit is
# dirty. A clean venv on a no-op night does nothing at all and prints nothing.
#
# The audit still runs every night even on a no-op. That is deliberate and not
# the waste it looks like: it is read-only, it is the only way a half-completed
# previous run ever converges ("safe to re-run" means the next run must notice,
# not just the next code change), and an audit that only ran after mutations
# would be blind to exactly the latent gaps nobody triggered yet.
#
# ── WHAT IT MUST NOT DO ─────────────────────────────────────────────────────
# It must NOT restart any gateway. The venv fixes are filesystem-level and take
# effect on the next import — verified 2026-09-24. A sync job that bounces
# gateways at 01:00 is a far worse bug than the one it fixes. Restart policy
# belongs to the host-local wrapper (launchd on sma/work, systemd on horza),
# which already owns it.
#
# It must NOT touch .venv. Both sma and horza carry an unrelated python3.13
# .venv that the gateway does not use. Only ./venv is in scope.
#
# ── BOUNDED ─────────────────────────────────────────────────────────────────
# Unattended at 01:00. pip can be slow or unreachable; every mutation is under
# a hard timeout and gives up cleanly rather than hanging. See the TIMEOUT_BIN
# block — `timeout` is Homebrew coreutils and is NOT on the PATH cron inherits.
#
# Usage:
#   reconcile-hermes-fork-install.sh [--sync-changed] [--dry-run] [--quiet]
#
#   --sync-changed  the caller's sync reported a change (forces the mutation
#                   path even if the audit currently looks clean)
#   --dry-run       report what would happen; mutate nothing
#   --quiet         suppress the no-op chatter (the wrapper passes this)
#
# Exit codes:  0 = clean, or reconciled and verified clean
#              1 = a real gap remains (loud — this is the alert)
#              2 = cannot reconcile at all (venv or auditor missing)

set -uo pipefail

HERMES_ROOT="${FORK_SYNC_HERMES_ROOT:-$HOME/.hermes}"
REPO="${FORK_SYNC_REPO:-$HERMES_ROOT/hermes-agent}"
VENV="$REPO/venv"
PY="$VENV/bin/python"
AUDIT="$REPO/scripts/fork-install-audit.py"
LABEL="${FORK_SYNC_LABEL:-$(hostname -s)}"

SYNC_CHANGED=false
DRY_RUN=false
QUIET=false
for arg in "$@"; do
  case "$arg" in
    --sync-changed) SYNC_CHANGED=true ;;
    --dry-run)      DRY_RUN=true ;;
    --quiet)        QUIET=true ;;
    *) echo "reconcile-hermes-fork-install.sh: unknown argument '$arg'" >&2; exit 2 ;;
  esac
done

# ── `timeout` is not native on macOS ────────────────────────────────────────
# Homebrew coreutils puts it in /opt/homebrew/bin, which is NOT on the PATH a
# cron job inherits from the gateway. A bare `timeout` exits 127 (command not
# found) at every call site and reads as failure. Resolve an absolute path
# once; degrade to running unbounded rather than misreporting.
TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"
if [ -z "$TIMEOUT_BIN" ]; then
  for _c in /opt/homebrew/bin/timeout /usr/local/bin/timeout /opt/homebrew/bin/gtimeout; do
    [ -x "$_c" ] && TIMEOUT_BIN="$_c" && break
  done
fi
run_to() {  # run_to <seconds> <command...>
  local _s="$1"; shift
  if [ -n "$TIMEOUT_BIN" ]; then "$TIMEOUT_BIN" "$_s" "$@"; else "$@"; fi
}

say()  { printf 'fork-install[%s] %s\n' "$LABEL" "$*"; }
note() { $QUIET || printf '  %s\n' "$*"; }

# ── which installer does this venv have? ────────────────────────────────────
# Not every host's venv carries pip, and assuming it does fails the whole
# reconcile on the host that needs it most:
#   sma, horza  pip is present          -> venv/bin/python -m pip install -e .
#   work        uv-managed venv, NO pip -> uv pip install -e . --python <venv>
# Detected once here, then both paths are driven through the same two calls
# (regenerate with --no-deps, reconcile without) so the sequence and the
# reasoning above hold regardless of installer.
#
# uv is resolved by absolute path for the same reason `timeout` is: cron's PATH
# is minimal and will not have ~/.local/bin or /opt/homebrew/bin on it.
INSTALLER=""
UV_BIN=""
if "$PY" -m pip --version >/dev/null 2>&1; then
  INSTALLER=pip
else
  UV_BIN="$(command -v uv || true)"
  if [ -z "$UV_BIN" ]; then
    for _c in "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
      [ -x "$_c" ] && UV_BIN="$_c" && break
    done
  fi
  if [ -n "$UV_BIN" ]; then
    INSTALLER=uv
  else
    say "FAILED: venv has no pip module and no uv on this host — cannot reconcile"
    exit 2
  fi
fi

# install_editable <--no-deps|""> — one installer-neutral entry point.
#   --no-deps  regenerate the editable MAPPING only, leave the dep tree alone
#   (omitted)  full install: reconcile the deps that --no-deps skipped
# uv has no --no-input; pip needs it to stay non-interactive under cron.
install_editable() {
  local no_deps="$1" rc=0
  if [ "$INSTALLER" = "pip" ]; then
    if [ "$no_deps" = "--no-deps" ]; then
      run_to "$2" "$PY" -m pip install -e "$REPO" --no-deps --no-input --quiet || rc=$?
    else
      run_to "$2" "$PY" -m pip install -e "$REPO" --no-input --quiet || rc=$?
    fi
  else
    if [ "$no_deps" = "--no-deps" ]; then
      run_to "$2" "$UV_BIN" pip install -e "$REPO" --python "$PY" --no-deps --quiet || rc=$?
    else
      run_to "$2" "$UV_BIN" pip install -e "$REPO" --python "$PY" --quiet || rc=$?
    fi
  fi
  return "$rc"
}

# ── 0. sanity ───────────────────────────────────────────────────────────────
if [ ! -x "$PY" ]; then
  say "FAILED: venv python missing at $PY"
  exit 2
fi
if [ ! -f "$AUDIT" ]; then
  say "FAILED: auditor missing at $AUDIT (cannot verify the install)"
  exit 2
fi

# The venv's site-packages. Globbed rather than guessed so a python version
# bump inside the 3.11–3.13 range pyproject allows does not silently audit the
# wrong directory. `-c` is avoided on purpose: this script is run from cron and
# from agent shells, and an interpreter flag there is a needless variable.
PURELIB=""
for _d in "$VENV"/lib/python3.*/site-packages; do
  [ -d "$_d" ] && PURELIB="$_d" && break
done
if [ -z "$PURELIB" ]; then
  say "FAILED: no site-packages under $VENV/lib/python3.*"
  exit 2
fi

# ── 1. audit (read-only) ────────────────────────────────────────────────────
# The auditor compares the REPO's declared requirements and package list
# against the installed venv — not the installed dist's own metadata. That
# distinction is what makes a half-completed run converge: after a sync that
# moved the checkout but died before this step, the dist metadata still
# describes the previous revision, so comparing metadata-to-metadata would
# report CLEAN and the gap would sit there until the next sync. The repo file
# is the target state, so the repo file is the reference.
#
# Neutral cwd: from inside the repo root, cwd lands on sys.path and every name
# resolves even when the finder is broken. The auditor neutralises this itself;
# this is belt and braces for the shell side.
cd /tmp || exit 2
AUDIT_OUT="$(run_to 120 "$PY" "$AUDIT" "$REPO" 2>&1)"
AUDIT_RC=$?
AUDIT_LINE="$(printf '%s\n' "$AUDIT_OUT" | grep '^satisfied:' | head -1)"

if [ "$AUDIT_RC" -eq 2 ]; then
  printf '%s\n' "$AUDIT_OUT" | sed 's/^/  /'
  say "FAILED: cannot audit the install"
  exit 2
fi

if [ "$AUDIT_RC" -eq 0 ] && ! $SYNC_CHANGED; then
  # Genuine no-op: nothing changed last night, and the install is already
  # correct. Stay silent — empty output is the contract for a nightly job.
  $QUIET && exit 0
  note "audit clean and sync was a no-op — nothing to do"
  note "$AUDIT_LINE"
  exit 0
fi

if [ "$AUDIT_RC" -eq 0 ]; then
  echo "post-sync install reconcile (sync changed the checkout)"
else
  echo "post-sync install reconcile (audit found a gap)"
  printf '%s\n' "$AUDIT_OUT" | sed -n '2,$p' | sed 's/^/  /'
fi
note "$AUDIT_LINE"

if $DRY_RUN; then
  note "[DRY RUN] would back up, then:"
  note "  pip install -e . --no-deps      (regenerate editable finder)"
  note "  pip install -e . --no-input     (reconcile dependencies)"
  note "  re-audit                        (confirm the repair did not re-break)"
  say "DRY RUN: would reconcile"
  exit 0
fi

# ── 2. backup before mutation ───────────────────────────────────────────────
BK="$HERMES_ROOT/backups/fork-install-reconcile-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK" || { say "FAILED: cannot create backup dir $BK"; exit 1; }
cp -a "$PURELIB"/__editable___hermes_agent_*_finder.py "$BK"/ 2>/dev/null || true
cp -a "$PURELIB"/__editable__.hermes_agent-*.pth "$BK"/ 2>/dev/null || true
cp -a "$PURELIB"/hermes_agent-*.dist-info "$BK"/ 2>/dev/null || true
# A version snapshot makes a regression diagnosable after the fact: what was
# installed before the mutation is the first thing anyone wants to know.
run_to 60 "$PY" -m pip list --format=freeze > "$BK/pip-freeze-before.txt" 2>/dev/null || true
if [ ! -s "$BK/pip-freeze-before.txt" ] && [ -n "$UV_BIN" ]; then
  # pip-freeze needs pip too. Record the versions that matter a second way so a
  # uv-managed host still has a post-hoc rollback reference.
  run_to 60 "$UV_BIN" pip freeze --python "$PY" > "$BK/pip-freeze-before.txt" 2>/dev/null || true
fi
# Fail closed if the backup captured nothing to roll back to. A reconcile with
# no rollback point is worse than a dirty audit.
if ! ls "$BK"/hermes_agent-*.dist-info >/dev/null 2>&1; then
  say "FAILED: backup at $BK captured no dist-info — refusing to mutate"
  exit 1
fi
note "backup: $BK"

# ── 3. regenerate the editable install ──────────────────────────────────────
# --no-deps is mandatory here: it regenerates the package MAPPING without
# moving the dependency tree (pyproject.toml scopes dependencies as the
# blast-radius surface). This is the exact operation that fixed sma's
# crash-loop. Build isolation fetches setuptools if the venv lacks it, so this
# step needs the network — hence the timeout.
#
# Deliberately separate from step 4 rather than folded into one command: this
# step is the one that prevents the crash-loop, and it must hold even when the
# network fetch in step 4 fails. Two commands, two failure domains.
note "regenerating editable install (--no-deps) ..."
if ! install_editable --no-deps 420 > "$BK/regen.log" 2>&1; then
  say "FAILED: editable regeneration failed (see $BK/regen.log)"
  tail -6 "$BK/regen.log" 2>/dev/null | sed 's/^/  /'
  exit 1
fi

# ── 4. reconcile dependencies ───────────────────────────────────────────────
# Full install, deliberately NOT --no-deps: step 3 skipped exactly these, and
# they are how the 2026-09-24 gaps appeared. Every direct dep is exact-pinned
# in pyproject, so this resolves to the reviewed set rather than drifting.
note "reconciling dependencies (full editable install) ..."
if ! install_editable "" 600 > "$BK/reconcile.log" 2>&1; then
  say "FAILED: dependency reconcile failed (see $BK/reconcile.log)"
  tail -6 "$BK/reconcile.log" 2>/dev/null | sed 's/^/  /'
  exit 1
fi

# ── 5. audit AFTER, not before ──────────────────────────────────────────────
# Both pip commands regenerate the editable finder, so the thing being fixed
# and the thing doing the fixing touch the same artifact. Confirm the repair
# survived its own repair.
AFTER_OUT="$(run_to 120 "$PY" "$AUDIT" "$REPO" 2>&1)"
AFTER_RC=$?
AFTER_LINE="$(printf '%s\n' "$AFTER_OUT" | grep '^satisfied:' | head -1)"

if [ "$AFTER_RC" -ne 0 ]; then
  printf '%s\n' "$AFTER_OUT" | sed 's/^/  /'
  say "FAILED: reconcile did not come back clean — $AFTER_LINE"
  say "        backup: $BK"
  exit 1
fi

note "$AFTER_LINE"
say "reconciled OK — $(printf '%s\n' "$AFTER_OUT" | grep '^packages:' | head -1)"
exit 0

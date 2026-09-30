#!/usr/bin/env bash
# Self-check for mark_unmarked_packs_promisor, the #124272 fix in
# scripts/sync-hermes-fork.sh. Extracts the REAL function from the script (never
# a copy, so it cannot drift) and asserts:
#   1. a partial clone with unmarked packs gets exactly those packs marked
#   2. re-running marks nothing (idempotent)
#   3. a FULL clone is left untouched (no .promisor invented, config unchanged)
# Exits non-zero on the first failing assertion. Run: bash scripts/fork-sync-pack-marker-check.sh
set -uo pipefail

SCRIPT="${1:-$HOME/.hermes/hermes-agent/scripts/sync-hermes-fork.sh}"
[ -f "$SCRIPT" ] || { echo "FAIL: no script at $SCRIPT"; exit 2; }

# Pull the function text out of the real script.
fndef="$(sed -n '/^mark_unmarked_packs_promisor()/,/^}$/p' "$SCRIPT")"
[ -n "$fndef" ] || { echo "FAIL: mark_unmarked_packs_promisor not found in $SCRIPT"; exit 2; }
eval "$fndef"

d="$(mktemp -d "${TMPDIR:-/tmp}/packmark.XXXXXX")"
mkdir -p "$d/src"; cd "$d/src"; git init -q -b main .
git config user.email a@b; git config user.name a
printf 'one\n' > a.txt; git add -A; git commit -qm one
printf 'two\n' > b.txt; git add -A; git commit -qm two

# --- partial clone with its markers removed (the failing host's state) ---
cd "$d"; git clone -q --no-checkout "file://$d/src" partial
for m in .git/objects/pack/*.promisor; do mv "$m" "$m.disabled"; done 2>/dev/null
cd "$d/partial"
git config remote.origin.promisor true
git config remote.origin.partialclonefilter tree:0
packs=$(ls .git/objects/pack/*.pack | wc -l | tr -d ' ')
before=$(ls .git/objects/pack/*.promisor 2>/dev/null | wc -l | tr -d ' ')
[ "$before" -eq 0 ] || { echo "FAIL: expected 0 markers before, got $before"; exit 1; }

mark_unmarked_packs_promisor "$PWD" >/dev/null
after=$(ls .git/objects/pack/*.promisor 2>/dev/null | wc -l | tr -d ' ')
[ "$after" -eq "$packs" ] || { echo "FAIL: expected $packs markers, got $after"; exit 1; }
assert1="partial: $packs pack(s) marked ✓"

out=$(mark_unmarked_packs_promisor "$PWD")
if printf '%s' "$out" | grep -q 'marked'; then echo "FAIL: second call was not idempotent: $out"; exit 1; fi
after2=$(ls .git/objects/pack/*.promisor | wc -l | tr -d ' ')
[ "$after2" -eq "$after" ] || { echo "FAIL: idempotency changed the count"; exit 1; }
assert2="idempotent: no new markers on re-run ✓"

# --- full clone must be untouched ---
cd "$d"; git clone -q "file://$d/src" full
cd "$d/full"
cfg_before="$(git config --get-regexp 'promisor|partialclonefilter' || true)"
mark_unmarked_packs_promisor "$PWD" >/dev/null
cfg_after="$(git config --get-regexp 'promisor|partialclonefilter' || true)"
[ "$cfg_before" = "$cfg_after" ] || { echo "FAIL: full clone config changed"; exit 1; }
fm=$(ls .git/objects/pack/*.promisor 2>/dev/null | wc -l | tr -d ' ')
[ "$fm" -eq 0 ] || { echo "FAIL: full clone gained $fm marker(s)"; exit 1; }
assert3="full clone untouched: 0 markers, config identical ✓"

echo "mark_unmarked_packs_promisor self-check: 3/3"
printf '  %s\n  %s\n  %s\n' "$assert1" "$assert2" "$assert3"

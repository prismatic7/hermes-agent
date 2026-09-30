#!/usr/bin/env bash
# Self-check for fork-sync-preserve-live-commits.sh.
#
# Builds throwaway repos (no network, no real repo touched) and asserts:
#   A. nothing to carry        -> exit 0, says so, changes nothing
#   B. one local-only commit   -> exit 0, and the commit REACHES the branch
#   C. a conflicting carry     -> exit 1, merge aborted, tree left clean
#
# Run: bash scripts/fork-sync-preserve-live-commits-selftest.sh
set -uo pipefail

GUARD="$(cd "$(dirname "$0")" && pwd)/fork-sync-preserve-live-commits.sh"
[ -x "$GUARD" ] || chmod +x "$GUARD"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
ok()   { printf '  ok   %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

GITC=(git -c user.name=t -c user.email=t@t -c init.defaultBranch=main -c commit.gpgsign=false)

# make_repo <dir> <file> <content>  — a one-commit repo
make_repo() {
  mkdir -p "$1" && cd "$1"
  "${GITC[@]}" init -q .
  printf '%s\n' "$3" > "$2"
  "${GITC[@]}" add -A && "${GITC[@]}" commit -qm "base"
  cd - >/dev/null
}

echo "A. nothing to carry"
base="$TMP/a"; make_repo "$base/live" f.txt base
"${GITC[@]}" clone -q "$base/live" "$base/scratch" 2>/dev/null
out="$("$GUARD" "$base/scratch" "$base/live" 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ok "exit 0" || bad "exit $rc (want 0)"
case "$out" in *"nothing to carry"*) ok "reports nothing to carry";; *) bad "said: $out";; esac

echo "B. one local-only commit is carried"
b="$TMP/b"; make_repo "$b/live" f.txt base
"${GITC[@]}" clone -q "$b/live" "$b/scratch" 2>/dev/null
before="$("${GITC[@]}" -C "$b/scratch" rev-parse HEAD)"
cd "$b/live" && printf 'live work\n' > live-only.txt \
  && "${GITC[@]}" add -A && "${GITC[@]}" commit -qm "live-only work"
cd - >/dev/null
out="$("$GUARD" "$b/scratch" "$b/live" 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ok "exit 0" || bad "exit $rc (want 0); said: $out"
case "$out" in *"carrying 1 local-only commit"*) ok "counts 1 commit to carry";; *) bad "said: $out";; esac
after="$("${GITC[@]}" -C "$b/scratch" rev-parse HEAD)"
[ "$before" != "$after" ] && ok "branch tip moved" || bad "tip unchanged — the commit was NOT carried"
# The real assertion: the live commit's TREE CONTENT is now in the branch.
"${GITC[@]}" -C "$b/scratch" cat-file -e "HEAD:live-only.txt" 2>/dev/null \
  && ok "live commit's file is in the branch" || bad "live commit's content is absent"

echo "C. a conflicting carry fails safe"
c="$TMP/c"; make_repo "$c/live" shared.txt base
"${GITC[@]}" clone -q "$c/live" "$c/scratch" 2>/dev/null
# Diverge both sides on the same line -> a real conflict.
cd "$c/live" && printf 'live side\n' > shared.txt \
  && "${GITC[@]}" add -A && "${GITC[@]}" commit -qm "live edit"
cd - >/dev/null
cd "$c/scratch" && printf 'scratch side\n' > shared.txt \
  && "${GITC[@]}" add -A && "${GITC[@]}" commit -qm "scratch edit"
cd - >/dev/null
tip_before="$("${GITC[@]}" -C "$c/scratch" rev-parse HEAD)"
out="$("$GUARD" "$c/scratch" "$c/live" 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && ok "non-zero exit on conflict ($rc)" || bad "exit 0 on a conflict"
case "$out" in *CONFLICT*) ok "names the conflict";; *) bad "said: $out";; esac
[ "$("${GITC[@]}" -C "$c/scratch" rev-parse HEAD)" = "$tip_before" ] \
  && ok "branch tip unchanged (nothing half-merged)" || bad "tip moved on a failed carry"
[ -z "$("${GITC[@]}" -C "$c/scratch" status --porcelain)" ] \
  && ok "scratch tree left clean (merge aborted)" || bad "dirty tree after a failed carry"

echo
printf '%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]

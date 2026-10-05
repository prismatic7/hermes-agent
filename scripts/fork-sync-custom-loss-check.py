#!/usr/bin/env python3
"""Warn when a fork-sync merge drops one of OUR customs.

Run inside the fork-sync scratch clone, after a successful merge of upstream into
fork/customizations, before pushing.

Why this exists: a merge resolves each conflict once, but taking a clean-looking
result can silently discard our side of a file. That is how
`fix(source-install): stop the runaway rebuild loop`'s `members_stamp` guard
disappeared from the fork while its TEST kept passing — the fork and upstream
then both lacked the fix, nothing failed, and the bug it fixed came back
(observed 2026-09-30, after merge 9eb89102).

For every file our customs touch, compare the merge result against the merge base
of OUR side: any line we ADDED that the result no longer carries, and that
upstream never had either, is a candidate loss.

A candidate is not proof — upstream may have implemented the same behaviour its
own way, which is the CORRECT resolution. So this reports, it never fails the
run. Read the list; confirm each one was deliberate.
"""
from __future__ import annotations

import subprocess
import sys

TIMEOUT = 120


def main() -> int:
    git = lambda *a: subprocess.run(["git", *a], capture_output=True, timeout=TIMEOUT)

    merge = git("rev-parse", "HEAD").stdout.strip().decode()
    if not merge:
        print("loss-check: cannot resolve HEAD", file=sys.stderr)
        return 0

    parents = git("rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    if len(parents) < 3:
        return 0  # not a merge commit; nothing to compare
    ours, upstream = parents[1].decode(), parents[2].decode()

    # The LAST SYNC POINT. A line that already existed here arrived FROM upstream
    # and we merely carried it; only lines added after this point are OUR customs.
    # Without this filter the check reports upstream's OWN edits as "our loss":
    # anything upstream wrote, we carried, and upstream then reworded is present
    # in `mine`, absent from the reworded result, and absent from the new upstream
    # tip — the exact signature of a loss, for a line that was never ours.
    # Measured 2026-10-06: 4 files flagged, all 4 upstream's own churn (e.g. a
    # config_defaults.py comment authored by an upstream contributor), none ours.
    # A warning that cries wolf on every sync is how a REAL loss gets skimmed past.
    base = git("merge-base", ours, upstream).stdout.strip().decode()

    names = git("log", "--no-merges", "--name-only", "--format=", "HEAD", "--not", upstream)
    files = sorted({l.strip() for l in names.stdout.decode().splitlines() if l.strip()})

    def blob(rev: str, path: str):
        r = git("cat-file", "blob", f"{rev}:{path}")
        return r.stdout if r.returncode == 0 else None

    losses = []
    for path in files:
        merged = blob(merge, path)
        if merged is None:
            continue
        mine, theirs = blob(ours, path), blob(upstream, path)
        if mine is None or mine == merged:
            continue
        # Lines WE added SINCE the last sync, still absent from the result and
        # never supplied by upstream. That combination means ours is gone.
        base_lines = set((blob(base, path) or b"").splitlines())
        upstream_lines = set(theirs.splitlines()) if theirs else set()
        merged_lines = set(merged.splitlines())
        gone = [l for l in mine.splitlines()
                if l.strip() and l not in base_lines
                and l not in merged_lines and l not in upstream_lines]
        if gone:
            losses.append((path, len(gone)))

    if losses:
        print("⚠ custom-loss check: this merge may have dropped our side of:")
        for path, n in losses:
            print(f"    {path}  ({n} of our added line(s) absent)")
        print("  Confirm each was deliberate (upstream subsumed the behaviour) or restore it.")
    else:
        print("custom-loss check: no custom dropped by this merge")
    return 0


if __name__ == "__main__":
    sys.exit(main())

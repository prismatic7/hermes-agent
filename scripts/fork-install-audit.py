#!/usr/bin/env python3
"""fork-install-audit.py — does this venv actually match the repo it serves?

Read-only. Run it with the venv's own interpreter; it reads the *running*
interpreter's site-packages, so no path guessing.

Three independent checks, because each one alone has a blind spot:

  1. DEPENDENCIES — every requirement the installed ``hermes-agent`` dist
     declares (markers evaluated) is present at a satisfying version. Catches
     the silent drift that ``pip install -e . --no-deps`` leaves behind, which
     degrades features instead of crashing (e.g. tool_search's BM25 assembly
     goes dark on a missing ``snowballstemmer``).

  2. MAPPING — every package the repo declares in
     ``[tool.setuptools.packages.find].include`` is a key in the editable
     finder's ``MAPPING`` dict. A declared-but-unmapped name is unimportable
     from every cwd, which is exactly how the 2026-09-23 gateway crash-loop
     happened: upstream gained ``hermes_platform``, the finder still mapped
     the previous package set, and nothing noticed until a restart forced a
     fresh import.

  3. RESOLVABLE — ``importlib.util.find_spec()`` resolves each declared
     package for real. Check 2 reads a dict; this one drives the actual
     meta-path finder, which is the thing that has to work. It is also why
     this script chdirs to a neutral directory and strips the repo from
     ``sys.path`` first: from inside the repo root, cwd lands on sys.path and
     every name resolves even when the finder is broken.

Exit codes:
  0  clean
  1  a gap was found (repairable — reinstall the editable install)
  2  cannot audit at all (no editable install / ``packaging`` missing), which
     is itself a gap worth repairing

Usage:
  venv/bin/python scripts/fork-install-audit.py [REPO]

  REPO defaults to the current directory. Pass it explicitly; this script is
  normally called by sync-hermes-fork.sh with the live checkout path.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
import sysconfig
import tempfile
import tomllib
from typing import NoReturn

try:
    import importlib.metadata as md
    import importlib.util as ilu
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ModuleNotFoundError as exc:  # pragma: no cover - handled at runtime
    print("CANNOT AUDIT: %s is not importable by %s" % (exc.name, sys.executable))
    sys.exit(2)


def _bail(msg: str) -> NoReturn:
    print("CANNOT AUDIT: %s" % msg)
    sys.exit(2)


def declared_packages(repo: str) -> list[str]:
    """Top-level package names from [tool.setuptools.packages.find].include.

    Entries are dotted globs (``agent``, ``agent.*``); the first path segment
    is the importable top-level name.
    """
    if tomllib is None:  # pragma: no cover
        _bail("tomllib unavailable (needs Python 3.11+)")
    path = os.path.join(repo, "pyproject.toml")
    if not os.path.isfile(path):
        _bail("no pyproject.toml at %s" % path)
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    try:
        globs = cfg["tool"]["setuptools"]["packages"]["find"]["include"]
    except KeyError:
        _bail("%s declares no [tool.setuptools.packages.find].include" % path)
    names: list[str] = []
    for glob in globs:
        head = glob.split(".")[0].strip()
        if head and head != "*" and head not in names:
            names.append(head)
    return names


def finder_mapping(purelib: str):
    """Parse MAPPING out of the editable finder without importing it.

    Importing the finder would be self-referential (it is what supplies the
    packages); ast-parsing it is the honest read of what is installed.
    """
    cands = sorted(
        f
        for f in os.listdir(purelib)
        if f.startswith("__editable___hermes_agent") and f.endswith("_finder.py")
    )
    if not cands:
        return None, "no __editable___hermes_agent*_finder.py in %s" % purelib
    path = os.path.join(purelib, cands[-1])
    src = open(path, encoding="utf-8").read()
    m = re.search(r"^MAPPING: dict\[str, str\] = (\{.*?\})\n", src, re.S | re.M)
    if not m:
        return None, "MAPPING dict not found in %s" % path
    return ast.literal_eval(m.group(1)), os.path.basename(path)


def neutralise_path(repo: str) -> None:
    """Make check 3 honest: no cwd, and no repo tree, on sys.path.

    Running this script puts ``<repo>/scripts`` on sys.path[0]. That is not the
    repo root, so it would not mask a top-level package -- but it is one
    directory move away from doing so, and the whole point of check 3 is that
    it cannot lie. Strip anything that resolves inside the repo.
    """
    os.chdir(tempfile.gettempdir())
    def _in_repo(entry: str) -> bool:
        try:
            return os.path.realpath(entry).startswith(os.path.realpath(repo))
        except OSError:
            return False
    sys.path[:] = [p for p in sys.path if p and not _in_repo(p)]


def resolvable(name: str) -> bool:
    try:
        return ilu.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def audit_dependencies(extra_requirements: list[str]):
    try:
        dist = md.distribution("hermes-agent")
    except md.PackageNotFoundError:
        _bail("hermes-agent has no installed distribution metadata "
              "(the editable install is gone)")
    installed = {
        canonicalize_name(d.metadata["Name"]): d.version
        for d in md.distributions()
        if d.metadata["Name"]
    }
    ok = 0
    missing: list[str] = []
    mismatched: list[str] = []
    for raw in list(dist.requires or []) + extra_requirements:
        r = Requirement(raw)
        if r.marker is not None and not r.marker.evaluate():
            continue
        have = installed.get(canonicalize_name(r.name))
        if have is None:
            missing.append("%s %s" % (r.name, r.specifier))
        elif r.specifier and not r.specifier.contains(have, prereleases=True):
            mismatched.append("%s %s wants %s" % (r.name, have, r.specifier))
        else:
            ok += 1
    return ok, missing, mismatched


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo", nargs="?", default=os.getcwd(),
                    help="hermes-agent checkout the venv is supposed to serve")
    ap.add_argument(
        "--extra-packages",
        default=os.environ.get("FORK_INSTALL_AUDIT_EXTRA_PACKAGES", ""),
        help="comma-separated names to audit as if the repo declared them "
             "(proves the guard actually fails on a gap, and doubles as an "
             "ad-hoc probe for a suspected unmapped package)",
    )
    ap.add_argument(
        "--extra-requirements",
        default=os.environ.get("FORK_INSTALL_AUDIT_EXTRA_REQUIREMENTS", ""),
        help="comma-separated PEP 508 requirements to audit as if the installed "
             "dist declared them, e.g. 'snowballstemmer==3.1.1'. Proves the "
             "dependency check fails on the --no-deps drift it exists to catch.",
    )
    args = ap.parse_args()
    repo = os.path.abspath(args.repo)

    purelib = sysconfig.get_paths()["purelib"]

    extra_reqs = [r.strip() for r in args.extra_requirements.split(",") if r.strip()]
    ok, missing, mismatched = audit_dependencies(extra_reqs)
    print("satisfied: %d | missing: %s | mismatched: %s"
          % (ok,
             "NONE" if not missing else ", ".join(missing),
             "NONE" if not mismatched else ", ".join(mismatched)))

    want = declared_packages(repo)
    for extra in (n.strip() for n in args.extra_packages.split(",")):
        if extra and extra not in want:
            want.append(extra)

    dirty = bool(missing or mismatched)

    mapping, where = finder_mapping(purelib)
    if mapping is None:
        print("finder: UNREADABLE — %s" % where)
        print("DIRTY")
        return 1

    neutralise_path(repo)
    unmapped = [n for n in want if n not in mapping]
    unresolvable = [n for n in want if n not in unmapped and not resolvable(n)]
    for n in unmapped:
        print("  unmapped:     %s  (declared in pyproject, absent from %s)"
              % (n, where))
    for n in unresolvable:
        print("  unresolvable: %s  (mapped, but importlib cannot find it)"
              % n)
    print("packages: %d declared | %d mapped | %d resolvable"
          % (len(want), len(want) - len(unmapped), len(want) - len(unmapped) - len(unresolvable)))

    if dirty or unmapped or unresolvable:
        print("DIRTY")
        return 1
    print("CLEAN")
    return 0


if __name__ == "__main__":
    sys.exit(main())

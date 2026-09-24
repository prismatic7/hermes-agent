#!/usr/bin/env python3
"""Self-check for fork-install-audit.py.

Runs the auditor as a subprocess against a fake repo whose pyproject declares
requirements/packages that are deliberately absent, and asserts it exits 1 with
the gap named — plus a negative control that must exit 0. No test framework.

    venv/bin/python scripts/fork-install-audit-selftest.py

Fails loudly (non-zero exit, "SELFTEST FAILED" on stdout) if the auditor stops
detecting a gap, starts failing on a clean input, or loses its exit codes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT = os.path.join(HERE, "fork-install-audit.py")
REPO = os.path.dirname(HERE)

failures: list[str] = []


def run(args: list[str]) -> tuple[int, str]:
    p = subprocess.run(
        [sys.executable, AUDIT] + args,
        capture_output=True, text=True, cwd=tempfile.gettempdir(),
    )
    return p.returncode, p.stdout + p.stderr


def check(name: str, cond: bool, detail: str = "") -> None:
    print("  %-58s %s" % (name, "OK" if cond else "FAIL"))
    if not cond:
        failures.append("%s%s" % (name, (" — " + detail) if detail else ""))


def real_version() -> str:
    import tomllib
    with open(os.path.join(REPO, "pyproject.toml"), "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def fake_repo(extra_package: str | None = None, extra_requirement: str | None = None,
              version: str | None = None) -> str:
    """A minimal repo: real pyproject shape, optionally with a gap baked in.

    ``version`` defaults to the REAL repo version, so a case testing some
    *other* gap (unmapped package, missing dep) does not also trip the
    dist-version check and mask what it is meant to isolate. Case 5 passes an
    impossible version deliberately.
    """
    d = tempfile.mkdtemp(prefix="audit-selftest-")
    pkgs = ['"agent"', '"agent.*"', '"tools"', '"tools.*"']
    if extra_package:
        pkgs.append('"%s"' % extra_package)
    deps = ['"requests==2.33.0"']
    if extra_requirement:
        deps.append('"%s"' % extra_requirement)
    with open(os.path.join(d, "pyproject.toml"), "w") as fh:
        fh.write(
            '[project]\nname = "hermes-agent"\nversion = "%s"\n'
            "dependencies = [%s]\n"
            "\n[tool.setuptools.packages.find]\ninclude = [%s]\n"
            % (version or real_version(), ", ".join(deps), ", ".join(pkgs))
        )
    return d


print("fork-install-audit self-test")
print("  audit:  %s" % AUDIT)
print("  repo:   %s" % REPO)
print()

# 1. Clean input must pass. Uses a package that IS mapped and a dep that IS
#    installed, so a false positive here means the auditor is unusable.
clean = fake_repo(extra_package="agent", extra_requirement="certifi")
try:
    rc, out = run([clean])
    check("clean input exits 0", rc == 0, "rc=%d out=%r" % (rc, out[-200:]))
    check("clean input reports CLEAN", "CLEAN" in out)
finally:
    shutil.rmtree(clean, ignore_errors=True)

# 2. Unmapped package (the crash-loop shape) must fail, and name it.
gap = fake_repo(extra_package="hermes_platform_not_mapped")
try:
    rc, out = run([gap])
    check("unmapped package exits 1", rc == 1, "rc=%d" % rc)
    check("unmapped package is named", "hermes_platform_not_mapped" in out)
    check("unmapped package reports DIRTY", "DIRTY" in out)
finally:
    shutil.rmtree(gap, ignore_errors=True)

# 3. Uninstalled dependency (the --no-deps drift) must fail, and name it.
dep = fake_repo(extra_requirement="definitely-not-installed-pkg==1.2.3")
try:
    rc, out = run([dep])
    check("missing dependency exits 1", rc == 1, "rc=%d" % rc)
    check("missing dependency is named", "definitely-not-installed-pkg" in out)
finally:
    shutil.rmtree(dep, ignore_errors=True)

# 4. Unsatisfiable version spec must be a MISMATCH, not silently satisfied.
ver = fake_repo(extra_requirement="certifi>=9999.0.0")
try:
    rc, out = run([ver])
    check("version drift exits 1", rc == 1, "rc=%d" % rc)
    check("version drift says mismatched", "mismatched: certifi" in out)
finally:
    shutil.rmtree(ver, ignore_errors=True)

# 5. Stale dist version must be caught on its own (it is the other half of the
#    crash-loop signature). An impossible version no real install satisfies.
stale = fake_repo(extra_package="agent", extra_requirement="certifi",
                  version="9.9.9-impossible")
try:
    rc, out = run([stale])
    check("stale dist version exits 1", rc == 1, "rc=%d" % rc)
    check("stale dist version is reported", "!= repo 9.9.9-impossible" in out)
finally:
    shutil.rmtree(stale, ignore_errors=True)

# 6. Cannot-audit must be its own exit code, not a silent pass.
bad = tempfile.mkdtemp(prefix="audit-selftest-bad-")
try:
    rc, out = run([bad])
    check("no pyproject exits 2", rc == 2, "rc=%d out=%r" % (rc, out[-160:]))
    check("no pyproject says CANNOT AUDIT", "CANNOT AUDIT" in out)
finally:
    shutil.rmtree(bad, ignore_errors=True)

# 7. The real repo must be clean — the whole point of shipping this.
rc, out = run([REPO])
check("REAL repo exits 0", rc == 0, "rc=%d out=%r" % (rc, out[-300:]))
check("REAL repo reports 0 missing / 0 mismatched",
      "missing: NONE" in out and "mismatched: NONE" in out)
check("REAL repo maps hermes_platform", "hermes_platform" not in out or "unmapped" not in out)

print()
if failures:
    print("SELFTEST FAILED (%d):" % len(failures))
    for f in failures:
        print("  - %s" % f)
    sys.exit(1)
print("SELFTEST PASSED — all checks green.")

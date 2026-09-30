"""A worker acting for profile B must never inherit a SIBLING profile's `.env` sandbox.

Regression for t_75d553f5 / t_e07685a4: every sysadmin kanban worker carried
``HERMES_WRITE_SAFE_ROOT`` from ``profiles/enodios/.env``, so ``write_file`` /
``patch`` refused every path in the worker's own workspace.

The injection is two process generations wide, which is why a single-process test
cannot see it:

  gen 1  the dispatcher/gateway resolves a ROUTED home for a sibling-assigned card
         (``_default_spawn`` -> ``_resolve_worker_cli_toolsets`` ->
         ``_worker_profile_scope(bind_home=True)`` -> ``load_hermes_dotenv()``),
         which writes that sibling's ``.env`` into the SHARED ``os.environ``.
         Its own strip of a later child still works: the name is in ITS launch
         bookkeeping.
  gen 2  a process spawned from gen 1 inherits the value but has NO bookkeeping
         for the name -- not in the launch home's ``.env``, not in
         ``launch_dotenv_keys()``, not ``TERMINAL_*``, not source-supplied -- so
         ``strip_launch_profile_env`` could not see it and the sibling's sandbox
         reached the worker.

Real imports against temp ``HERMES_HOME``s and a real child process: no mocks of
the env machinery, per the repo's boundary-test rubric.

Also runnable without pytest, which is how it was measured:

    python tests/tools/test_routed_worker_sibling_dotenv_isolation.py [<repo>]
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

VICTIM = "HERMES_WRITE_SAFE_ROOT"
REPO_ROOT = Path(__file__).resolve().parents[2]

_GEN2 = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
VICTIM = "HERMES_WRITE_SAFE_ROOT"
from hermes_cli.env_loader import launch_dotenv_keys
from hermes_cli.env_loader import sibling_profile_dotenv_values
from tools.environments.local import build_subprocess_env, strip_launch_profile_env
from agent.secret_scope import is_multiplex_active

TARGET = os.environ["TARGET_HOME"]
SIBLING = os.environ["SIBLING_HOME"]
out = {"inherited": os.environ.get(VICTIM),
       "bookkept": VICTIM in launch_dotenv_keys()}

env = build_subprocess_env(scrub_secrets=is_multiplex_active() or True, inherit_profile_home=True)
env["HERMES_HOME"] = TARGET
strip_launch_profile_env(env, TARGET)
out["target_child"] = env.get(VICTIM)
out["sibling_only_child"] = env.get("SIBLING_ONLY_KEY")

env_a = build_subprocess_env(scrub_secrets=True, inherit_profile_home=True)
env_a["HERMES_HOME"] = SIBLING
strip_launch_profile_env(env_a, SIBLING)
out["declaring_profile_child"] = env_a.get(VICTIM)

os.environ[VICTIM] = "/tmp/operator-shell-choice"
env_c = build_subprocess_env(scrub_secrets=True, inherit_profile_home=True)
env_c["HERMES_HOME"] = TARGET
strip_launch_profile_env(env_c, TARGET)
out["operator_export_child"] = env_c.get(VICTIM)
print(json.dumps(out))
"""

_GEN1 = """
import os, subprocess, sys
sys.path.insert(0, sys.argv[1])
from hermes_constants import set_hermes_home_override, reset_hermes_home_override, get_hermes_home
from hermes_cli.env_loader import load_hermes_dotenv

home, sibling, target, gen2 = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
os.environ["HERMES_HOME"] = home
os.environ.pop("HERMES_WRITE_SAFE_ROOT", None)
tok = set_hermes_home_override(sibling)          # routed-home work for the sibling
try:
    load_hermes_dotenv(hermes_home=get_hermes_home())
finally:
    reset_hermes_home_override(tok)
child = dict(os.environ)
child["TARGET_HOME"], child["SIBLING_HOME"] = target, sibling
proc = subprocess.run([sys.executable, gen2, sys.argv[1]], env=child,
                      capture_output=True, text=True, timeout=180)
print(proc.stdout.strip().splitlines()[-1] if proc.returncode == 0 else proc.stderr)
sys.exit(proc.returncode)
"""


def build_probe(root: Path | None = None) -> dict:
    """Three profile homes (sibling declares the sandbox, target does not) + gen1/gen2 scripts."""
    root = Path(root or tempfile.mkdtemp(prefix="hermes-sibling-env-"))
    home = root / "home"
    sibling = home / "profiles" / "enodios"
    target = home / "profiles" / "sysadmin"
    for d in (home, sibling, target):
        d.mkdir(parents=True, exist_ok=True)
    (sibling / ".env").write_text(
        f"{VICTIM}=/tmp/vault-only-root\nSIBLING_ONLY_KEY=siblingvalue\n", encoding="utf-8")
    (target / ".env").write_text("TARGET_ONLY_KEY=targetvalue\n", encoding="utf-8")
    gen1 = root / "gen1.py"
    gen2 = root / "gen2.py"
    gen1.write_text(_GEN1, encoding="utf-8")
    gen2.write_text(_GEN2, encoding="utf-8")
    return {"home": home, "sibling": sibling, "target": target, "gen1": gen1, "gen2": gen2,
            "root": root, "owned": root}


def run_probe(probe: dict, repo: Path | str | None = None):
    repo = repo or REPO_ROOT
    proc = subprocess.run(
        [sys.executable, str(probe["gen1"]), str(repo), str(probe["home"]), str(probe["sibling"]),
         str(probe["target"]), str(probe["gen2"])],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, f"probe failed: {proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture
def two_generation_probe():
    probe = build_probe()
    try:
        yield probe
    finally:
        shutil.rmtree(probe["root"], ignore_errors=True)


def test_sibling_profile_sandbox_does_not_reach_another_profiles_worker(two_generation_probe):
    """The defect: gen2 cannot see the name in its bookkeeping, yet gen1 put it in os.environ."""
    out = run_probe(two_generation_probe)
    assert out["inherited"] == "/tmp/vault-only-root", (
        "probe precondition: generation 2 must inherit the sibling value")
    assert out["bookkept"] is False, (
        "probe precondition: the name is NOT traceable in generation 2's own bookkeeping")
    assert out["target_child"] is None, (
        f"a worker for the other profile carried a SIBLING profile's {VICTIM}: "
        f"{out['target_child']!r}")
    assert out["sibling_only_child"] is None, (
        f"a sibling-only key reached the other profile's worker: {out['sibling_only_child']!r}")


def test_declaring_profile_still_keeps_its_own_sandbox(two_generation_probe):
    """Bound: do not fix the leak by disarming the sandbox for the profile that declares it."""
    out = run_probe(two_generation_probe)
    assert out["declaring_profile_child"] == "/tmp/vault-only-root"


def test_operator_shell_export_of_the_same_name_is_not_stripped(two_generation_probe):
    """The strip is value-matched, so a launch-shell choice of the same name is not residue."""
    out = run_probe(two_generation_probe)
    assert out["operator_export_child"] == "/tmp/operator-shell-choice"


if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else str(REPO_ROOT)
    probe = build_probe()
    try:
        result = run_probe(probe, repo)
        checks = [
            ("inherited sibling value reached generation 2 (precondition)",
             result["inherited"], "/tmp/vault-only-root"),
            ("name is absent from generation 2's bookkeeping (precondition)",
             result["bookkept"], False),
            ("target worker does NOT carry the sibling sandbox",
             result["target_child"], None),
            ("sibling-only key does NOT reach the target worker",
             result["sibling_only_child"], None),
            ("operator shell export of the same name survives",
             result["operator_export_child"], "/tmp/operator-shell-choice"),
            ("declaring profile keeps its OWN sandbox",
             result["declaring_profile_child"], "/tmp/vault-only-root"),
        ]
        ok = True
        print(f"repo: {repo}")
        for label, got, want in checks:
            good = got == want
            ok = ok and good
            print(f"{'PASS' if good else 'FAIL'}  {label}\n      got={got!r} want={want!r}")
        print("RESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(probe["root"], ignore_errors=True)

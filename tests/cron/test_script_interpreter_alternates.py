"""Interpreter selection for cron scripts named through dotfile alternates.

A dotfile manager that keeps platform variants (yadm and friends) materialises
``guard.sh`` as a symlink to ``guard.sh##os.Linux``. ``Path.suffix`` on the resolved
target is then ``.Linux``, so choosing the interpreter from the resolved path hands a
bash script to Python: every run of the job dies with ``SyntaxError: unterminated
string literal``, which reads like a bug in the script rather than a routing bug in
the scheduler.

The interpreter must follow the *shell* extension wherever it appears — in the job's
configured name (before the symlink is followed) and in the alternate tag of the
resolved target — so no caller can reintroduce the misroute by forgetting ``raw``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.scheduler_script import (  # noqa: E402
    _is_shell_script_name,
    _run_job_script,
    _script_argv,
)


@pytest.fixture()
def scripts_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME whose scripts/ holds one yadm-style alternate pair."""
    hermes_home = tmp_path / ".hermes"
    scripts = hermes_home / "scripts"
    scripts.mkdir(parents=True)
    (hermes_home / "cron" / "output").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")

    target = scripts / "alt-guard.sh##os.Linux"
    target.write_text("#!/bin/bash\necho alt-ok\n")
    target.chmod(0o755)
    (scripts / "alt-guard.sh").symlink_to(target)
    return hermes_home


@pytest.mark.parametrize(
    "name,expected",
    [
        ("guard.sh", True),
        ("guard.bash", True),
        ("guard.sh##os.Linux", True),
        ("guard.BASH##host.mac", True),
        ("guard.sh.bak", False),
        ("guard.py", False),
        ("guard", False),
    ],
)
def test_shell_name_detection(name, expected):
    """The alternate tag is decoration on the name, not an extension."""
    assert _is_shell_script_name(name) is expected


def test_configured_name_decides_the_interpreter(scripts_home):
    """The job's configured name (pre-symlink) carries the authoritative extension."""
    configured = scripts_home / "scripts" / "alt-guard.sh"
    resolved = configured.resolve()
    assert resolved.name.endswith("##os.Linux")  # the misrouting suffix

    argv, _env, err = _script_argv(resolved, configured)

    assert err is None and argv is not None
    assert Path(argv[0]).name.lower().startswith("bash")
    assert argv[1] == str(resolved)


def test_resolved_alternate_alone_still_routes_to_shell(scripts_home):
    """A caller that omits ``raw`` must not hand a shell script to Python."""
    resolved = (scripts_home / "scripts" / "alt-guard.sh").resolve()

    argv, _env, err = _script_argv(resolved)

    assert err is None and argv is not None
    assert Path(argv[0]).name.lower().startswith("bash")


def test_run_job_script_executes_alternate_as_shell(scripts_home):
    """End to end: the alternate target actually runs, in bash."""
    ok, output = _run_job_script("alt-guard.sh")

    assert ok, output
    assert "alt-ok" in output


@pytest.mark.parametrize("name", ["notes.py", "guard.sh.bak"])
def test_non_shell_names_keep_python(scripts_home, name):
    """The tag-aware rule must not over-match: only a real shell extension counts."""
    script = scripts_home / "scripts" / name
    script.write_text("print('python')\n")

    argv, _env, _err = _script_argv(script.resolve())

    assert argv is not None
    assert not Path(argv[0]).name.lower().startswith("bash")

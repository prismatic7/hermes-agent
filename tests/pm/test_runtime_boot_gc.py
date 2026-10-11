"""A real bootstrap reader pins its generation before GC can select victims."""
import json
import os
import subprocess
import sys


def test_bootstrap_lease_survives_selection_change(tmp_path, monkeypatch):
    from pm.environments import install_state_dir, runtime_facts_path, site_packages
    from hermes_cli.runtime_state import collect_generations

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    state = install_state_dir(repo)
    for name in ("first", "second", "unused"):
        venv = state / "environments" / name / "venv"
        venv.mkdir(parents=True)
        # pyvenv.cfg first: the layout keys its site-packages path off the recorded version.
        (venv / "pyvenv.cfg").write_text("version = 3.11")
        site_packages(venv).mkdir(parents=True)
        (venv.parent / ".lease-managed").touch()
    legacy = state / "environments" / "old-unleased"
    legacy.mkdir()

    def select(name):
        environment = state / "environments" / name / "venv"
        runtime_facts_path(repo).write_text(json.dumps({"packages": {"venv": {"environment": str(environment)}}}))
        return environment

    first = select("first")
    code = '''
import sys
from pathlib import Path
from pm.environments import activate_dependencies
activate_dependencies(Path(sys.argv[1]))
print("ready", flush=True)
sys.stdin.readline()
'''
    child = subprocess.Popen([sys.executable, "-c", code, str(repo)], env=dict(os.environ),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        select("second")
        collect_generations(repo, min_age_seconds=0)
        assert first.is_dir()
        assert not (state / "environments" / "unused").exists()
        assert legacy.is_dir()
    finally:
        child.communicate("done\n", timeout=15)
    assert child.returncode == 0
    collect_generations(repo, min_age_seconds=0)
    assert not first.exists()
    assert (state / "environments" / "second").is_dir()
    assert legacy.is_dir()


def test_aged_generations_are_capped_by_count(tmp_path, monkeypatch):
    """The age floor alone lets the eligible hoard grow for ever: yesterday's
    generations all clear the floor at once and nothing bounds how many accumulate.
    The cap keeps the newest N of the ones that ALREADY passed all four safety
    conditions, so it can never reach a generation the age floor protects.
    """
    import time

    from hermes_cli import runtime_state
    from pm.environments import install_state_dir, runtime_facts_path, site_packages

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    state = install_state_dir(repo)
    names = [f"gen{index:02d}" for index in range(25)]
    for position, name in enumerate(names):
        venv = state / "environments" / name / "venv"
        venv.mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("version = 3.11")
        site_packages(venv).mkdir(parents=True)
        marker = venv.parent / ".lease-managed"
        marker.touch()
        # All older than the floor, distinct mtimes so newest-first is well defined.
        stamp = time.time() - 3 * 86400 + position
        os.utime(marker, (stamp, stamp))

    selected = state / "environments" / names[-1] / "venv"
    runtime_facts_path(repo).write_text(json.dumps({"packages": {"venv": {"environment": str(selected)}}}))

    removed = runtime_state.collect_generations(repo, max_generations=20)

    assert len(removed) == 4, "25 eligible generations, cap 20 -> the 4 oldest go"
    survivors = sorted(p.name for p in (state / "environments").iterdir())
    assert survivors == sorted(names[4:]), "the newest are kept, the oldest excess removed"
    assert selected.is_dir(), "the selected generation is never a victim"

    # max_generations=None restores the pure age-floor contract: every eligible
    # generation goes, however many there are.
    assert len(runtime_state.collect_generations(repo, max_generations=None)) == 20
    assert [p for p in (state / "environments").iterdir() if p.is_dir()] == [selected.parent]
    fresh = state / "environments" / "fresh" / "venv"
    fresh.mkdir(parents=True)
    (fresh / "pyvenv.cfg").write_text("version = 3.11")
    site_packages(fresh).mkdir(parents=True)
    (fresh.parent / ".lease-managed").touch()  # seconds old
    assert runtime_state.collect_generations(repo, max_generations=0) == [], \
        "a same-day generation is protected by the age floor even at cap 0"
    assert fresh.parent.is_dir()


def test_side_environment_generations_are_collected(tmp_path, monkeypatch):
    """pm.testenv mints a new ``gen-*`` under ``test-environment/`` and rewrites ``active.json``
    without removing the previous one; nothing visited that root, so a superseded test environment
    leaked for the life of the install (observed live: 378 MB, inactive for 13 days)."""
    import time

    from hermes_cli.runtime_state import collect_generations
    from pm.environments import install_state_dir
    from pm.testenv import NAME as TEST_ENV

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    root = install_state_dir(repo) / TEST_ENV
    old = root / ("gen-" + "a" * 32)
    fresh = root / ("gen-" + "b" * 32)
    selected = root / ("gen-" + "c" * 32)
    for generation in (old, fresh, selected):
        (generation / "venv").mkdir(parents=True)
    (root / "active.json").write_text(json.dumps({"generation": selected.name, "inputs": "x"}),
                                      encoding="utf-8")
    stamp = time.time() - 3 * 86400
    os.utime(old, (stamp, stamp))

    assert collect_generations(repo) == [old]
    assert selected.is_dir(), "the active.json selection is never a victim"
    assert fresh.is_dir(), "a same-day generation is protected by the age floor"
    assert not old.exists()


def test_side_environment_generation_with_a_held_lease_is_kept(tmp_path, monkeypatch):
    import time

    from hermes_cli.runtime_state import collect_generations
    from pm.environments import install_state_dir
    from pm.filesystem import lock_fd
    from pm.testenv import NAME as TEST_ENV

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    root = install_state_dir(repo) / TEST_ENV
    held = root / ("gen-" + "a" * 32)
    selected = root / ("gen-" + "c" * 32)
    for generation in (held, selected):
        (generation / "venv").mkdir(parents=True)
    leases = held / ".leases"
    leases.mkdir()
    (root / "active.json").write_text(json.dumps({"generation": selected.name}), encoding="utf-8")
    stamp = time.time() - 3 * 86400
    os.utime(held, (stamp, stamp))

    fd = os.open(leases / "reader", os.O_CREAT | os.O_RDWR, 0o600)
    assert lock_fd(fd, wait=False)
    try:
        assert collect_generations(repo) == []
        assert held.is_dir()
    finally:
        os.close(fd)
    assert collect_generations(repo) == [held]


def test_side_environment_without_a_readable_selection_is_left_alone(tmp_path, monkeypatch):
    """A root whose ``active.json`` is unreadable or foreign cannot name its selection, so nothing
    in it is provably safe to remove."""
    import time

    from hermes_cli.runtime_state import collect_generations
    from pm.environments import install_state_dir
    from pm.testenv import NAME as TEST_ENV

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    root = install_state_dir(repo) / TEST_ENV
    for name in ("a" * 32, "b" * 32):
        generation = root / ("gen-" + name)
        (generation / "venv").mkdir(parents=True)
        stamp = time.time() - 3 * 86400
        os.utime(generation, (stamp, stamp))
    (root / "active.json").write_text("{not json", encoding="utf-8")

    assert collect_generations(repo) == []
    assert sorted(p.name for p in root.iterdir() if p.name.startswith("gen-")) == \
        ["gen-" + "a" * 32, "gen-" + "b" * 32]

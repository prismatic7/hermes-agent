"""A buildable plugin enabled in two profiles declares ONE [project].name twice.

uv identifies a workspace member by that name, so "Two workspace members are both
named ..." fails `uv lock` for the WHOLE workspace. The eviction path reads that as
"the plugin does not fit", disables it, and re-fires on every dependency sync -- on
trade-bot it silently defeated custom-dangerous-patterns, a security control.

A collision is a fact about the SELECTION, not about one plugin, so none of the
colliding members may be evicted: they stay enabled and rejoin when one copy goes.
"""
import tomllib
from pathlib import Path

from pm.plugin_eviction import _colliding_members
from pm.workspace import _is_member_candidate


def _buildable(plugin: Path, name: str, *, uv_package: bool = False) -> Path:
    plugin.mkdir(parents=True, exist_ok=True)
    text = (
        "[project]\n"
        f'name = "{name}"\n'
        'version = "1.0.0"\n'
        "[build-system]\n"
        "requires = []\n"
        'build-backend = "backend"\n'
    )
    if uv_package:
        text += "[tool.uv]\npackage = true\n"
    (plugin / "pyproject.toml").write_text(text, encoding="utf-8")
    return plugin


def _virtual(plugin: Path, name: str) -> Path:
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "pyproject.toml").write_text(
        "[project]\n" f'name = "{name}"\n' 'version = "1.0.0"\n' "[tool.uv]\npackage = false\n",
        encoding="utf-8",
    )
    return plugin


def _entries(*paths: Path):
    return [(p.parent, p.name, p) for p in paths]


def test_two_homes_with_one_buildable_name_collide(tmp_path):
    """The measured failure: custom-dangerous-patterns in enodios and trade-bot."""
    a = _buildable(tmp_path / "enodios" / "plugins" / "custom-dangerous-patterns",
                   "hermes-custom-dangerous-patterns")
    b = _buildable(tmp_path / "trade-bot" / "plugins" / "custom-dangerous-patterns",
                   "hermes-custom-dangerous-patterns")

    collisions = _colliding_members(_entries(a, b))

    assert set(collisions) == {a.resolve(), b.resolve()}
    reason_a = collisions[a.resolve()]
    reason_b = collisions[b.resolve()]
    assert "shares the build-metadata name" in reason_a
    assert "disable the duplicate in all but one profile" in reason_a
    # The reason must name the OTHER home -- the plugin key is the same string twice,
    # so telling the operator to disable "the duplicate" without naming where it lives
    # leaves them grepping. This is a message an operator acts on.
    assert str(b.parent.parent.resolve()) in reason_a
    assert str(a.parent.parent.resolve()) not in reason_a
    assert str(a.parent.parent.resolve()) in reason_b
    assert str(b.parent.parent.resolve()) not in reason_b


def test_a_single_home_copy_never_collides(tmp_path):
    """The common case must cost nothing and offer no member up."""
    a = _buildable(tmp_path / "enodios" / "plugins" / "custom-dangerous-patterns",
                   "hermes-custom-dangerous-patterns")
    b = _buildable(tmp_path / "trade-bot" / "plugins" / "other-plugin", "hermes-other-plugin")

    assert _colliding_members(_entries(a, b)) == {}


def test_two_homes_with_different_names_never_collide(tmp_path):
    """Two copies of a plugin that declare distinct names lock fine."""
    a = _buildable(tmp_path / "enodios" / "plugins" / "shared", "hermes-shared-one")
    b = _buildable(tmp_path / "trade-bot" / "plugins" / "shared", "hermes-shared-two")

    assert _colliding_members(_entries(a, b)) == {}


def test_virtual_members_cannot_collide_because_they_are_renamed(tmp_path):
    """A metadata-only member is renamed to its unique key, so a shared declared
    name is not a collision there -- and must not be reported as one."""
    a = _virtual(tmp_path / "enodios" / "plugins" / "shared", "same-name")
    b = _virtual(tmp_path / "trade-bot" / "plugins" / "shared", "same-name")

    assert _is_member_candidate(a) and _is_member_candidate(b)
    assert _colliding_members(_entries(a, b)) == {}


def test_a_pyproject_that_is_not_a_member_is_ignored(tmp_path):
    """A plugin with no pyproject and no dependencies is not a member at all."""
    bare = tmp_path / "enodios" / "plugins" / "manifest-only"
    bare.mkdir(parents=True)
    (bare / "plugin.yaml").write_text("name: manifest-only\n", encoding="utf-8")
    other = _buildable(tmp_path / "trade-bot" / "plugins" / "manifest-only", "hermes-manifest-only")

    assert not _is_member_candidate(bare)
    assert _colliding_members(_entries(bare, other)) == {}


def test_an_unreadable_pyproject_never_collides(tmp_path):
    """Broken TOML is a different fault (static_verdicts names it); it must not
    manufacture a collision that hides the real reason."""
    a = tmp_path / "enodios" / "plugins" / "broken"
    a.mkdir(parents=True)
    (a / "pyproject.toml").write_text("not = [ valid", encoding="utf-8")
    b = _buildable(tmp_path / "trade-bot" / "plugins" / "broken", "hermes-broken")

    assert a.resolve() not in _colliding_members(_entries(a, b))


def test_uv_package_members_keep_their_declared_name_too(tmp_path):
    """tool.uv.package = true is buildable, so it collides like any buildable member."""
    a = _buildable(tmp_path / "enodios" / "plugins" / "pkg", "shared-pkg", uv_package=True)
    b = _buildable(tmp_path / "trade-bot" / "plugins" / "pkg", "shared-pkg", uv_package=True)

    assert set(_colliding_members(_entries(a, b))) == {a.resolve(), b.resolve()}


def test_receipt_warnings_survive_a_later_quiet_sync(tmp_path, monkeypatch):
    """The whole finding: latest.json is warning-free while an eviction sits one
    receipt back, so a reader of latest.json alone reports nothing."""
    import json

    from pm import receipt

    monkeypatch.setattr(receipt, "_receipt_dir", lambda: tmp_path)
    (tmp_path / "pm_20260930T131027Z-sync-1-a.json").write_text(json.dumps(
        {"kind": "sync", "warnings": [{"message": "Disabled plugin 'x'", "at": "T1"}],
         "finished_at": "T1"}), encoding="utf-8")
    (tmp_path / "pm_20260930T162223Z-sync-2-b.json").write_text(json.dumps(
        {"kind": "sync", "warnings": [], "finished_at": "T2"}), encoding="utf-8")

    rows = receipt.recent_warnings()

    assert len(rows) == 1
    assert rows[0]["message"] == "Disabled plugin 'x'"
    assert rows[0]["receipt"] == "pm_20260930T131027Z-sync-1-a.json"


def test_recent_warnings_is_bounded_and_newest_first(tmp_path, monkeypatch):
    import json

    from pm import receipt

    monkeypatch.setattr(receipt, "_receipt_dir", lambda: tmp_path)
    for index in range(8):
        (tmp_path / f"pm_20260930T00000{index}Z-sync-{index}-x.json").write_text(json.dumps(
            {"kind": "sync", "warnings": [{"message": f"w{index}"}]}), encoding="utf-8")

    rows = receipt.recent_warnings(limit=3)

    assert [r["message"] for r in rows] == ["w7", "w6", "w5"]


def _two_homes_with_one_collision(tmp_path):
    """A colliding buildable pair plus one innocent member, in two live homes."""
    import hermes_yaml as yaml

    default_home = tmp_path / "home"
    profile_home = tmp_path / "profiles" / "secondary"
    profile_home.mkdir(parents=True)
    collided = [
        _buildable(default_home / "plugins" / "shared", "hermes-shared"),
        _buildable(profile_home / "plugins" / "shared", "hermes-shared"),
    ]
    innocent = _buildable(default_home / "plugins" / "innocent", "hermes-innocent")
    for home, names in ((default_home, ["shared", "innocent"]), (profile_home, ["shared"])):
        with (home / "config.yaml").open("w", encoding="utf-8") as handle:
            yaml.safe_dump({"plugins": {"enabled": names}}, handle)
    return default_home, collided, innocent


def test_a_speculative_build_is_reclaimed_only_when_provably_unused(tmp_path, monkeypatch):
    """`_discard_generation` DELETES a generation, so its proof has to be real.

    A trial build (`_trial`) and the core-alone proof in `sync_evicting` each mint a
    full generation that nothing will ever select; `apply()` only cleans up after a
    FAILURE, so every SUCCESSFUL trial leaked one. sync_evicting minted 1 + 1 + 2N per
    invocation and kept them all. Reclaiming is only safe for a generation this call
    can prove is nobody's, so each refusal below is exercised explicitly: a generation
    outside the environments dir, the selected one, and one a live reader holds.
    """
    import json

    from pm import plugin_eviction
    from pm.environments import install_state_dir, runtime_facts_path
    from hermes_cli.runtime_state import lease_directory

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    class Package:
        @staticmethod
        def project_root():
            return repo

    state = install_state_dir(repo)
    environments = state / "environments"

    def mint(name, *, readable=True):
        venv = environments / name / "venv"
        venv.mkdir(parents=True)
        if readable:
            # A real venv layout: `selected_venv` refuses an installation it cannot
            # recognize, and an unreadable selection makes the helper fail closed.
            (venv / "pyvenv.cfg").write_text("version = 3.11")
        (venv.parent / ".lease-managed").touch()
        return venv

    def select(name):
        runtime_facts_path(repo).write_text(json.dumps(
            {"packages": {"venv": {"environment": str(environments / name / "venv")}}}),
            encoding="utf-8")

    selected = mint("selected")
    select("selected")

    # 1. A fresh, unselected, unleased generation IS reclaimed: the point of the helper.
    trial = mint("trial")
    plugin_eviction._discard_generation(Package(), {"environment": trial})
    assert not trial.exists(), "an unselected, unleased trial generation must be reclaimed"

    # 2. The selected generation is never a victim, whatever the caller passes.
    plugin_eviction._discard_generation(Package(), {"environment": selected})
    assert selected.exists(), "the selected generation must never be reclaimed"

    # 3. A generation a live reader holds is never a victim either.
    held = mint("held")
    release = lease_directory(held.parent)
    try:
        plugin_eviction._discard_generation(Package(), {"environment": held})
        assert held.exists(), "a generation with a live lease must never be reclaimed"
    finally:
        release()
    plugin_eviction._discard_generation(Package(), {"environment": held})
    assert not held.exists(), "once the reader exits the generation is reclaimable"

    # 4. A path outside the install's environments dir is left alone.
    outside = tmp_path / "elsewhere" / "venv"
    outside.mkdir(parents=True)
    plugin_eviction._discard_generation(Package(), {"environment": outside})
    assert outside.exists(), "a path outside the environments dir must never be touched"

    # 5. No environment in the result (a no-op apply) is not a delete.
    plugin_eviction._discard_generation(Package(), {})
    plugin_eviction._discard_generation(Package(), None)
    assert sorted(p.name for p in environments.iterdir()) == ["selected"]

    # 6. An UNREADABLE selection makes the helper fail closed: it cannot prove the
    #    generation is unselected, so it must keep it rather than guess.
    opaque = mint("opaque", readable=False)
    runtime_facts_path(repo).write_text(json.dumps(
        {"packages": {"venv": {"environment": str(opaque)}}}), encoding="utf-8")
    kept = mint("kept-when-opaque")
    plugin_eviction._discard_generation(Package(), {"environment": kept})
    assert kept.exists(), "an unreadable selection must not licence a delete"


def test_enabled_member_dirs_drops_a_colliding_pair_for_every_caller(tmp_path, monkeypatch):
    """The set Venv.apply BUILDS and the set Venv.expected_stamp HASHES are one call.

    The eviction path filtered the collision out of its own list while every other
    caller kept asking enabled_member_dirs() for the unfiltered one. The selection then
    could not be built (uv refuses a workspace holding one name twice) yet the currency
    probe still read it as merely "not current", so the source-update completion tail
    re-ran on every launch and minted a generation per launch. The filter has to live in
    the shared function or the two stamps never agree.
    """
    import pm.environments
    import pm.plugins_state as pstate
    from pm.workspace import enabled_member_dirs

    default_home, collided, innocent = _two_homes_with_one_collision(tmp_path)
    # dependency_homes() imports this name lazily, so patch where it is looked up.
    monkeypatch.setattr(pm.environments, "dependency_home_root", lambda: default_home)
    monkeypatch.setattr(pstate, "_profiles_root", lambda: tmp_path / "profiles")

    members = enabled_member_dirs()

    assert members == [innocent], "the colliding pair sits out; the innocent member stays"
    assert all(path.resolve() not in _colliding_members([(path.parent, path.name, path)
                                                         for path in members])
               for path in members), "what survives the filter can never collide again"

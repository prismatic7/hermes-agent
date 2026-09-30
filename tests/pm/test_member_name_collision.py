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

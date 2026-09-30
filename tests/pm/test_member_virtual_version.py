"""uv refuses a workspace member whose [project] has no version; the virtual-member
path must supply one, the way the no-pyproject path already does.

hermes-lcm's pyproject.toml is a bare [tool.ruff] section with no [project] table.
`_workspace_member` reaches its `virtual` branch, names the member, writes it back —
and uv then refuses to parse a member with no version, failing `uv lock` for the whole
workspace. The eviction path reads that as "the plugin does not fit", disables it, and
for a context engine that silently stops its per-turn ingest.
"""
import tomllib

from pm.workspace import _workspace_member


def _write(plugin, text):
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "pyproject.toml").write_text(text, encoding="utf-8")
    return plugin


def test_virtual_member_with_no_project_table_gets_a_version(tmp_path):
    plugin = _write(tmp_path / "plugin", "# tooling only\n[tool.ruff]\nline-length = 100\n")

    member = _workspace_member(plugin, tmp_path / "root", identity=plugin)

    document = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))
    assert document["project"]["name"].startswith("hermes-plugin-")
    assert document["project"].get("version") == "0.0.0", "uv needs a version on a virtual member"


def test_virtual_member_keeps_a_real_version(tmp_path):
    plugin = _write(
        tmp_path / "plugin",
        '[project]\nname = "thing"\nversion = "2.1.0"\n[tool.uv]\npackage = false\n',
    )

    member = _workspace_member(plugin, tmp_path / "root", identity=plugin)

    document = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))
    assert document["project"]["version"] == "2.1.0", "a declared version is never overwritten"


def test_virtual_member_with_dynamic_version_is_left_alone(tmp_path):
    plugin = _write(
        tmp_path / "plugin",
        '[project]\nname = "thing"\ndynamic = ["version"]\n[tool.uv]\npackage = false\n',
    )

    member = _workspace_member(plugin, tmp_path / "root", identity=plugin)

    document = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))
    assert document["project"].get("dynamic") == ["version"]
    assert "version" not in document["project"], "a dynamic version must not gain a static one"

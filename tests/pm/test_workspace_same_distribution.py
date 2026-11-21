"""One distribution reachable from two plugin homes must yield one workspace member.

``hermes profile create --clone`` copies plugins per profile; two copies declare the same
``[project].name`` and ``uv lock`` rejects the workspace outright.
"""
from __future__ import annotations

from pathlib import Path

from pm.workspace import _same_distribution_copies

_BODY = """[project]
name = "hermes-curator-evolver"
version = "0.11.0"
"""


def _plugin(home: Path, body: str = _BODY) -> Path:
    directory = home / "plugins" / "curator-evolver"
    directory.mkdir(parents=True)
    (directory / "pyproject.toml").write_text(body, encoding="utf-8")
    return directory


def test_cloned_copy_collapses_to_one_member(tmp_path):
    first = _plugin(tmp_path / "default")
    second = _plugin(tmp_path / "research")

    kept = _same_distribution_copies({first: first, second: second})

    assert list(kept) == [first]


def test_divergent_declarations_both_survive(tmp_path):
    first = _plugin(tmp_path / "a")
    second = _plugin(tmp_path / "b", _BODY.replace("0.11.0", "0.12.0"))

    assert len(_same_distribution_copies({first: first, second: second})) == 2


def test_plugin_without_pyproject_is_left_alone(tmp_path):
    directory = tmp_path / "plugins" / "plain"
    directory.mkdir(parents=True)
    (directory / "hermes-plugin.yaml").write_text("name: plain\n", encoding="utf-8")

    assert list(_same_distribution_copies({directory: directory})) == [directory]

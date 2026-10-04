"""context_from continuity is bounded by a fixed per-block character cap.

The 12,422,837-token entry in ``cron/usage_audit.jsonl`` (job ``1dce30ef50f6``,
2026-08-28T01:53Z) is the CUMULATIVE session prompt-token SUM across ~90 API
calls (``result['prompt_tokens'] == agent.session_prompt_tokens``, accumulated
per API call in ``agent/turn_usage.py``), NOT one prompt. The only cron context
that can grow run-to-run — ``context_from`` continuity — is already clipped to
``_MAX_CONTEXT_CHARS`` (8000) before injection. These tests lock that bound so
a refactor cannot silently remove it.

P4 follow-up to t_9ebe3cd5 / ADR 0112.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cron.jobs as jobs_mod
from cron.scheduler_prompt import (
    _MAX_CONTEXT_CHARS,
    _clip_to_context_budget,
    _inject_context_from,
)


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "cron" / "output").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")
    return hermes_home


def _write_archive(home, job_id, filename, body):
    out_dir = jobs_mod.OUTPUT_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / filename).write_text(body, encoding="utf-8")


def test_oversized_archive_is_clipped(cron_env):
    """A monster prior answer cannot flood the next prompt: it is head+tail clipped."""
    job = {"id": "a1b2c3d4e5f6", "prompt": "BASE", "context_from": ["self"]}
    _write_archive(cron_env, job["id"], "2026-01-01_00-00-00.md",
                   "# Cron Job: x\n\n## Response\n\n" + "X" * 45_000_000 + "\n")

    prompt, injected = _inject_context_from(job, "BASE")

    assert injected is True
    carried = prompt[: prompt.index("BASE")]
    assert len(carried) <= _MAX_CONTEXT_CHARS + 500
    assert "chars omitted" in carried
    assert len(prompt) < 20_000


def test_small_archive_is_injected_unclipped(cron_env):
    """Default path is byte-preserving: an in-budget answer is injected verbatim."""
    job = {"id": "a1b2c3d4e5f6", "prompt": "BASE", "context_from": ["self"]}
    _write_archive(cron_env, job["id"], "2026-01-01_00-00-00.md",
                   "# Cron Job: x\n\n## Response\n\nSHORT-ANSWER\n")

    prompt, injected = _inject_context_from(job, "BASE")

    assert injected is True
    assert "SHORT-ANSWER" in prompt
    assert "chars omitted" not in prompt


def test_direct_clip_bounds_and_is_idempotent_below_cap():
    """_clip_to_context_budget leaves small text untouched and bounds big text."""
    small = "hello world"
    assert _clip_to_context_budget(small) == small
    big = _clip_to_context_budget("Z" * (_MAX_CONTEXT_CHARS * 3))
    assert len(big) <= _MAX_CONTEXT_CHARS + 100
    assert "chars omitted" in big

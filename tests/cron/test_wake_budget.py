"""Daily wake budget (Lotti ADR 0112, adapted) — mechanism + bounds.

Covers, against the REAL store in a temp HERMES_HOME (no mocks):
  * at-limit automatic fire is allowed;
  * over-limit automatic fire is refused;
  * explicit `run` may reach 2x the limit, and no further;
  * an unreadable policy fails closed (automatic) / open (explicit);
  * the counter increments in the SAME statement that reads it (no read-then-write race);
  * with the feature OFF (the shipped default) nothing is written and scheduling is unchanged.
"""
import threading

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so jobs.json and wake_budget.db stay in tmp."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # The budget store path is resolved through get_hermes_home() at call time; no reset needed.
    from cron import wake_budget
    monkeypatch.setattr(wake_budget, "WAKE_BUDGET_FILE", tmp_path / "cron" / "wake_budget.db")
    yield tmp_path


def _enable(monkeypatch, limit):
    """Turn the feature on with *limit* by patching the policy resolver (config load is cached)."""
    from cron import wake_budget

    monkeypatch.setattr(wake_budget, "resolve_policy", lambda: (True, limit, True))


def _job():
    from cron.jobs import create_job
    return create_job(prompt="x", schedule="every 5m", name="budget-test")


def test_off_by_default_touches_nothing(home, monkeypatch):
    """Shipped default: no counter file, no writes, and a fire still claims normally."""
    from cron.jobs import claim_job_for_fire
    from cron import wake_budget

    assert wake_budget.resolve_policy() == (False, 10, True)
    job = _job()
    assert claim_job_for_fire(job["id"], return_job=True) is not False
    assert not (home / "cron" / "wake_budget.db").exists(), "off must not create the store"


def test_automatic_allowed_at_limit_refused_over(home, monkeypatch):
    from cron.jobs import claim_job_for_fire
    from cron import wake_budget

    _enable(monkeypatch, 3)
    ids = [_job()["id"] for _ in range(3)]
    for jid in ids:
        assert claim_job_for_fire(jid, claim_ttl_seconds=0) is not wake_budget.BUDGET_EXCEEDED
    over = _job()["id"]
    assert claim_job_for_fire(over, claim_ttl_seconds=0) is wake_budget.BUDGET_EXCEEDED
    assert wake_budget.wake_count() == 3


def test_explicit_reaches_2x_then_refused(home, monkeypatch):
    from cron.jobs import claim_job_for_fire
    from cron import wake_budget

    _enable(monkeypatch, 3)  # explicit bound = 6
    for _ in range(6):
        assert claim_job_for_fire(_job()["id"], manual=True) is not wake_budget.BUDGET_EXCEEDED
    assert claim_job_for_fire(_job()["id"], manual=True) is wake_budget.BUDGET_EXCEEDED
    assert wake_budget.wake_count() == 6


def test_automatic_stops_where_explicit_can_continue(home, monkeypatch):
    """The ADR shape: automatic stops at the limit; an explicit fire rides up to 2x."""
    from cron.jobs import claim_job_for_fire
    from cron import wake_budget

    _enable(monkeypatch, 2)
    for _ in range(2):
        assert claim_job_for_fire(_job()["id"]) is not wake_budget.BUDGET_EXCEEDED
    assert claim_job_for_fire(_job()["id"]) is wake_budget.BUDGET_EXCEEDED  # automatic blocked
    assert claim_job_for_fire(_job()["id"], manual=True) is not wake_budget.BUDGET_EXCEEDED
    assert claim_job_for_fire(_job()["id"], manual=True) is not wake_budget.BUDGET_EXCEEDED
    assert claim_job_for_fire(_job()["id"], manual=True) is wake_budget.BUDGET_EXCEEDED  # 2x


def test_refused_claim_does_not_burn_a_wake(home, monkeypatch):
    """A loss for another reason (here: duplicate live claim) must not consume budget."""
    from cron.jobs import claim_job_for_fire
    from cron import wake_budget

    _enable(monkeypatch, 3)
    jid = _job()["id"]
    assert claim_job_for_fire(jid) is not wake_budget.BUDGET_EXCEEDED
    assert claim_job_for_fire(jid) is False  # fresh claim — not a budget refusal
    assert wake_budget.wake_count() == 1


def test_unreadable_policy_fails_closed_automatic_open_explicit(home, monkeypatch):
    from cron.jobs import claim_job_for_fire
    from cron import wake_budget

    monkeypatch.setattr(wake_budget, "resolve_policy", lambda: (False, None, False))
    assert claim_job_for_fire(_job()["id"]) is wake_budget.BUDGET_EXCEEDED  # automatic closed
    assert claim_job_for_fire(_job()["id"], manual=True) is not wake_budget.BUDGET_EXCEEDED


def test_limit_clamped_to_1_24():
    from cron import wake_budget

    assert wake_budget._clamp(0) == 1
    assert wake_budget._clamp(99) == 24
    assert wake_budget._clamp(-5) == 1
    assert wake_budget._clamp("nonsense") == 10
    assert wake_budget._clamp(7) == 7


def test_increment_is_atomic_no_read_then_write(home, monkeypatch):
    """Two threads racing the LAST available wake: exactly one wins, count lands at the bound."""
    from cron import wake_budget

    _enable(monkeypatch, 5)
    for _ in range(4):
        assert wake_budget._consume(5) is True
    results = []

    def race():
        results.append(wake_budget._consume(5))

    threads = [threading.Thread(target=race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1, "exactly one racer may take the final wake"
    assert results.count(False) == 7
    assert wake_budget.wake_count() == 5, "counter must not exceed the bound"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))

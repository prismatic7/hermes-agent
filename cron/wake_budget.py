"""Daily wake budget for cron fires (Lotti ADR 0112, adapted for this fleet).

One choke point — ``cron.jobs.claim_job_for_fire``, which every automatic AND every
explicit fire path already routes through — reads and increments a single per-day
counter in ONE SQL statement (``INSERT ... ON CONFLICT ... RETURNING``), so there is
no read-then-write race and no second lock to add.

Off by default: while ``cron.enforce_wake_budget`` is false (the default) nothing is
read from or written to the budget store and scheduling is byte-identical. The
budget magnitude itself is ``cron.max_wakes_per_day`` (default 10, clamped 1-24, so
a peer config cannot lift it arbitrarily).

Rules (from the ADR):

* automatic wakes stop at the limit;
* an explicit fire (``hermes cron run`` / force / dashboard trigger) may exceed the
  limit up to ``2 × limit`` — that bounds a request mislabelled as the user's;
* an unreadable policy fails CLOSED for automatic work and OPEN for an explicit
  request.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Tuple

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

logger = logging.getLogger("cron.wake_budget")

# Optional test override (mirrors cron.executions.EXECUTIONS_FILE); production resolves
# the path at transaction time so a dashboard operation that enters another profile does
# not leak that profile's counter into the import-time home.
WAKE_BUDGET_FILE: Optional[Path] = None

MAX_WAKES_DEFAULT = 10
MAX_WAKES_MIN = 1
MAX_WAKES_MAX = 24
#: An explicit request may reach this multiple of the limit, and no further.
EXPLICIT_MULTIPLIER = 2

#: Returned by ``claim_job_for_fire`` when the daily budget refused an otherwise-valid
#: fire. Falsy, so every existing ``if not claimed`` caller still sees "not fired"; only
#: the paths that need to tell a budget refusal from a lost claim test it by identity.
BUDGET_EXCEEDED = ""

_lock = threading.RLock()


def _clamp(value: object) -> int:
    """Coerce a config value into [1, 24]; anything unparseable is the default."""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = MAX_WAKES_DEFAULT
    return max(MAX_WAKES_MIN, min(MAX_WAKES_MAX, n))


def resolve_policy() -> Tuple[bool, Optional[int], bool]:
    """Return ``(enabled, limit, readable)`` for the active profile.

    ``readable=False`` means the config could not be consulted at all; callers must
    then fail closed for automatic work and open for an explicit request. ``enabled``
    is only meaningful when ``readable`` is true.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
        if not isinstance(cron_cfg, dict):
            return False, None, False
        enabled = bool(cron_cfg.get("enforce_wake_budget", False))
        limit = _clamp(cron_cfg.get("max_wakes_per_day", MAX_WAKES_DEFAULT))
        return enabled, limit, True
    except Exception:
        logger.warning("cron wake budget: policy unreadable", exc_info=True)
        return False, None, False


# --- counter store ------------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    from cron.jobs import _ensure_cron_dir
    from hermes_cli.sqlite_util import open_db

    path = WAKE_BUDGET_FILE or (get_hermes_home().resolve() / "cron" / "wake_budget.db")
    _ensure_cron_dir(path.parent)
    return open_db(path, db_label="cron/wake_budget.db", initialize=_initialize_schema)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS wake_budget ("
        "  day TEXT PRIMARY KEY,"
        "  wakes INTEGER NOT NULL"
        ")"
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    from hermes_cli.sqlite_util import transaction

    with _lock, transaction(_connect()) as conn:
        yield conn


def _consume(bound: int) -> bool:
    """Atomically read-and-increment today's counter; True iff it stayed under *bound*.

    The read and the increment are one statement, so two concurrent callers can never
    both see a stale count — the second blocks on the row and observes the first's
    write. ``RETURNING`` yields no row when the ``WHERE`` fails, which is the refusal.
    """
    day = _hermes_now().date().isoformat()
    with _transaction() as conn:
        row = conn.execute(
            "INSERT INTO wake_budget (day, wakes) VALUES (?, 1) "
            "ON CONFLICT(day) DO UPDATE SET wakes = wakes + 1 "
            "WHERE wakes < ? RETURNING wakes",
            (day, int(bound)),
        ).fetchone()
    return row is not None


def wake_count(day: Optional[str] = None) -> int:
    """Today's consumed wake count (0 when the store is untouched). Diagnostics only."""
    key = day or _hermes_now().date().isoformat()
    try:
        with _transaction() as conn:
            row = conn.execute(
                "SELECT wakes FROM wake_budget WHERE day=?", (key,)
            ).fetchone()
        return int(row[0]) if row is not None else 0
    except Exception:
        return 0


def allow_wake(*, explicit: bool) -> bool:
    """Admission decision for ONE fire, consuming a unit when one is spent.

    ``explicit`` marks a user-issued fire (manual run / force / trigger-now); everything
    else is automatic. Returns True when the fire may proceed.
    """
    enabled, limit, readable = resolve_policy()
    if not readable:
        # Fail closed for automatic work, open for an explicit request.
        return bool(explicit)
    if not enabled or limit is None:
        return True  # feature off: nothing touched, scheduling unchanged
    bound = int(limit) * (EXPLICIT_MULTIPLIER if explicit else 1)
    allowed = _consume(bound)
    if not allowed:
        logger.warning(
            "cron wake budget: %s wake refused (daily limit %d%s reached)",
            "explicit" if explicit else "automatic",
            limit,
            " x2" if explicit else "",
        )
    return allowed

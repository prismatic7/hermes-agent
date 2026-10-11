"""Reclaim per-checkout dependency state whose checkout is gone.

Dependency state lives under ``<home>/installs/<install_key>`` where the key hashes the
checkout path (:func:`pm.environments.install_key`). Every ``hermes -w`` worktree, review tree
or scratch clone that boots the agent therefore commits a full environment (venv, test venv,
PM runtime, ~200 MB each), and nothing removed it when the tree was deleted: one developer
host carried 402 such orphans (80 GB) after a fortnight of worktree campaigns.

The state dir records the checkout it belongs to in ``inputs/.project-root`` (written by
:func:`pm.environments.record_activation_inputs`). A missing path alone does not prove the
checkout is gone: a data root shared with a container (``-v ~/.hermes:/opt/data``) or an
unmounted volume records paths this process cannot see. So a dir is an orphan only when its
checkout sat where this process can see that it was deleted -- under the data root that owns
``installs/`` (scratch clones, the runtime checkout) or directly in a ``.worktrees/`` dir that
still exists. Removal still fails closed on anything a live process could hold: the install
lock, or a dependency / PM runtime generation lease.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def _provably_deleted(root: Path, data_root: Path) -> bool:
    if root.exists():
        return False
    return root.is_relative_to(data_root) or (root.parent.name == ".worktrees" and root.parent.is_dir())


def orphan_install_states(installs: Path) -> list[Path]:
    """State dirs whose recorded checkout is provably deleted. Dirs without a record are left alone."""
    if not installs.is_dir():
        return []
    data_root = installs.resolve().parent
    orphans: list[Path] = []
    for state in sorted(installs.iterdir()):
        record = state / "inputs" / ".project-root"
        if not state.is_dir() or not record.is_file():
            continue
        try:
            root = record.read_text(encoding="utf-8-sig").strip()
        except OSError:
            continue
        if root and _provably_deleted(Path(root), data_root):
            orphans.append(state)
    return orphans


def _claimed_checkout_keys(data_root: Path) -> set[str]:
    """``install_key`` of every checkout this data root can see it owns.

    Bounded on purpose: the runtime checkout, the scratch clones, and each one's ``.worktrees``
    trees (a ``hermes -w`` worktree). A checkout somewhere else on the machine keeps its state
    under its OWN data root -- verified on a developer host: the fork clones under the user's
    ``Development/`` tree carry no state in this home -- so it needs no entry. A checkout that does
    share this home from outside the data root would be missed, which is exactly why states with no
    record are swept only on an explicit request (:func:`unclaimed_install_states`).
    """
    from pm.environments import install_key

    def is_checkout(target: Path) -> bool:
        return target.is_dir() and not target.is_symlink() and (
            (target / "pyproject.toml").is_file() or (target / ".git").exists())

    repos = [data_root / "hermes-agent"]
    scratch = data_root / "scratch"
    if scratch.is_dir():
        repos.extend(sorted(scratch.iterdir()))
    keys: set[str] = set()
    for repo in repos:
        if is_checkout(repo):
            keys.add(install_key(repo))
        worktrees = repo / ".worktrees"
        if worktrees.is_dir():
            keys.update(install_key(tree) for tree in worktrees.iterdir() if is_checkout(tree))
    return keys


def unclaimed_install_states(installs: Path) -> list[Path]:
    """State dirs with no ``inputs/.project-root`` record that no visible checkout claims.

    These predate the record (or a boot path never wrote it), so nothing in the dir names a
    checkout and :func:`orphan_install_states` can never recognise them. A *negative* search over
    :func:`_claimed_checkout_keys` cannot see a share of the machine (a container mount, an
    unmounted volume), so this is only ever reached on an explicit request -- ``hermes pm gc
    --sweep-unknown`` -- and the caller still fails closed on locks and leases (:func:`_held`).
    """
    if not installs.is_dir():
        return []
    claimed = _claimed_checkout_keys(installs.resolve().parent)
    orphans: list[Path] = []
    for state in sorted(installs.iterdir()):
        if not state.is_dir() or state.is_symlink():
            continue
        if (state / "inputs" / ".project-root").is_file():
            continue  # the record-ful path owns this decision
        if state.name not in claimed:
            orphans.append(state)
    return orphans


def _held(state: Path) -> bool:
    """True while a process may still be using this state dir: the install lock is taken or a
    generation lease is held. Any lock we cannot even open counts as held."""
    from hermes_cli.runtime_state import leases_held
    from pm.filesystem import lock_fd

    lock = state / ".install.lock"
    if lock.exists():
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            return True
        try:
            if not lock_fd(fd, wait=False):
                return True
        finally:
            os.close(fd)
    for generations in (state / "environments", state / "pm-runtime" / "generations"):
        if generations.is_dir():
            for generation in generations.iterdir():
                if generation.is_dir() and (generation / ".leases").is_dir() and leases_held(generation):
                    return True
    return False


def collect_orphan_install_states(installs: Path, *, sweep_unknown: bool = False) -> list[Path]:
    """Remove every unheld orphan state dir under *installs*; returns what was removed.

    *sweep_unknown* additionally reclaims record-less dirs no visible checkout claims
    (:func:`unclaimed_install_states`) -- an explicit request, never the boot prune's default.
    """
    removed: list[Path] = []
    targets = orphan_install_states(installs)
    if sweep_unknown:
        targets += unclaimed_install_states(installs)
    for state in targets:
        if _held(state):
            logger.debug("orphan install state %s is still held; skipped", state.name)
            continue
        # The record goes last, and only once everything else is gone: the startup prune runs on
        # a daemon thread and a removal can fail part-way (an open file on Windows, a read-only
        # dir), and a dir that lost its record first would never be recognised again.
        for child in state.iterdir():
            if child.name == "inputs":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    child.unlink()
        if any(child.name != "inputs" for child in state.iterdir()):
            logger.debug("orphan install state %s only partly removed; retried next pass", state.name)
            continue
        shutil.rmtree(state, ignore_errors=True)
        if not state.exists():
            removed.append(state)
    return removed

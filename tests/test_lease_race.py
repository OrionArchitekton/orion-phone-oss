"""Exactly one winner per round when 8 processes claim the same phone at once."""

from __future__ import annotations

import multiprocessing as mp
from collections import Counter
from pathlib import Path

import pytest

from orion_phone.codes import Code, PhoneError
from orion_phone.lease import LeaseStore

ROUNDS, WORKERS = 200, 8


def _worker(state_dir: str, idx: int, start, done, out) -> None:
    store = LeaseStore(Path(state_dir))
    for rnd in range(ROUNDS):
        start.wait(timeout=30)
        try:
            store.acquire("a", f"w{idx}", 60)
            out.put((rnd, idx, True))
        except Exception as exc:  # a crash must fail the round, not kill the worker and stall the barrier
            busy = isinstance(exc, PhoneError) and exc.code is Code.BUSY
            out.put((rnd, idx, False if busy else repr(exc)))
        done.wait(timeout=30)


@pytest.mark.parametrize("seed_expired", [False, True])
def test_exactly_one_winner_per_round(tmp_path, seed_expired):
    ctx = mp.get_context("fork")
    start, done = ctx.Barrier(WORKERS + 1), ctx.Barrier(WORKERS + 1)
    out = ctx.Queue()
    procs = [
        ctx.Process(target=_worker, args=(str(tmp_path), i, start, done, out), daemon=True)
        for i in range(WORKERS)
    ]
    for proc in procs:
        proc.start()
    store = LeaseStore(tmp_path)
    lease_file = tmp_path / "leases" / "a.json"
    for _ in range(ROUNDS):
        lease_file.unlink(missing_ok=True)
        if seed_expired:
            store.acquire("a", "old-holder", -1)  # an already-expired lease to take over
        start.wait(timeout=30)
        done.wait(timeout=30)
    results = [out.get(timeout=30) for _ in range(ROUNDS * WORKERS)]
    for proc in procs:
        proc.join(timeout=30)
    assert [proc.exitcode for proc in procs] == [0] * WORKERS  # every worker finished cleanly (None means it hung)
    errors = [r for r in results if not isinstance(r[2], bool)]
    assert not errors, errors[:3]
    wins = Counter(rnd for rnd, _, won in results if won)
    bad = {rnd: wins[rnd] for rnd in range(ROUNDS) if wins[rnd] != 1}
    assert not bad, f"rounds without exactly one winner: {bad}"

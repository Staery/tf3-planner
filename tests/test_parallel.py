"""parallel.py: worker sizing, stable shards, and that the split generator parts merge back to
exactly the serial candidates (the property the worker processes rely on). Nothing is written
to data/ (routing matrices are only read from their cache)."""
from __future__ import annotations

import pytest

from tf3planner import parallel


def test_worker_count_env():
    assert parallel.worker_count("0") == 0
    assert parallel.worker_count("3") == 3
    assert parallel.worker_count("-2") == 0
    n = parallel.worker_count("auto")
    assert 0 <= n <= max(0, (__import__("os").cpu_count() or 1) - 1)


def test_shard_stable():
    keys = [("mm", "coal", 3), ("milk", "grain", 17), ("circ", "road", 5)]
    for k in keys:
        s = parallel.shard_of(k, 7)
        assert 0 <= s < 7 and s == parallel.shard_of(k, 7)


@pytest.fixture(scope="module")
def full():
    from tf3planner import planner as P
    pl = P.Planner(horizon_year=1901)
    return pl, pl.initial_state()


def _sig(c):
    return (c.kind, c.mode, c.title, round(c.capex), round(c.income), tuple(c.nodes))


def test_tours_parts_merge_equals_serial(full):
    from tf3planner import tours as tr
    pl, sim = full
    y, sup, dem, bk = sim.year, dict(sim.supply), dict(sim.demand), set()
    serial = tr.milk_runs(pl, y, sup, dem, bk, geometry=False) + tr.circuits(pl, y, sup, dem, bk, geometry=False)
    n = 3
    milk = [p for k in range(n) for p in tr.milk_run_parts(
        pl, y, sup, dem, bk, own=lambda key, k=k: parallel.shard_of(("milk",) + key, n) == k)]
    circ = [p for k in range(n) for p in tr.circuit_parts(
        pl, y, sup, dem, bk, own=lambda key, k=k: parallel.shard_of(("circ",) + key, n) == k)]
    merged = [c for c, _ in tr.select(pl, [x for _k, l in sorted(milk, key=lambda x: x[0]) for x in l], 60)] + \
             [c for c, _ in tr.select(pl, [x for _k, l in sorted(circ, key=lambda x: x[0]) for x in l], 60)]
    assert [_sig(c) for c in merged] == [_sig(c) for c in serial]


def test_multimodal_parts_merge_equals_serial(full):
    from tf3planner import multimodal as mm
    pl, sim = full
    args = (pl.game, pl.vehicles, pl, sim.year, dict(sim.supply), dict(sim.demand), set(), set())
    serial = mm.generate(*args)
    n = 3
    parts = [p for k in range(n)
             for p in mm.chain_parts(*args, own=lambda key, k=k: parallel.shard_of(("mm",) + key, n) == k)[0]]
    merged = mm.merge_parts(mm.select_chains(mm._context(pl, pl.game), pl.game, parts))
    assert [_sig(c) for c in merged] == [_sig(c) for c in serial]

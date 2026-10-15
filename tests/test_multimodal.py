"""Tests for tf3planner.multimodal (warehouse transfer chains) on the real export.

Run with pytest or directly: python tests/test_multimodal.py
The Planner (~8 s) and the first, cold generate() call are shared by all tests.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tf3planner import multimodal as mm  # noqa: E402
from tf3planner.candidates import ProjectCandidate  # noqa: E402

COLD_LIMIT_S = 60.0
WARM_LIMIT_S = 5.0
EPS = 1e-6

_STATE: dict = {}


def state():
    if not _STATE:
        from tf3planner.planner import Planner
        pl = Planner()
        sim = pl.initial_state()
        stats: dict = {}
        t0 = time.perf_counter()
        cands = mm.generate(pl.game, pl.vehicles, pl, sim.year, sim.supply, sim.demand, set(), set(), stats)
        _STATE.update(pl=pl, sim=sim, cands=cands, stats=stats, cold_s=time.perf_counter() - t0)
    return _STATE


# ---------------------------------------------------------------- runtime

def test_runtime_bound():
    s = state()
    assert s["cold_s"] < COLD_LIMIT_S, f"cold generate took {s['cold_s']:.1f} s"
    pl, sim = s["pl"], s["sim"]
    t0 = time.perf_counter()
    again = mm.generate(pl.game, pl.vehicles, pl, sim.year, sim.supply, sim.demand, set(), set())
    warm = time.perf_counter() - t0
    assert warm < WARM_LIMIT_S, f"warm generate took {warm:.1f} s"
    assert [c.title for c in again] == [c.title for c in s["cands"]], "generate() is not deterministic"


# ---------------------------------------------------------------- structure

def test_candidate_structure():
    s = state()
    pl, cands = s["pl"], s["cands"]
    assert cands, "no multimodal chains generated"
    for c in cands:
        assert isinstance(c, ProjectCandidate)
        assert c.kind == "multimodal" and c.mode == "multi"
        roles = [l["role"] for l in c.legs]
        assert roles.count("trunk") == 1 and "feeder" in roles, roles
        assert c.meta["warehouse"]["modules"] >= 1
        assert any(k[1] == "warehouse" for k in c.station_keys)
        # every land leg has its pair key, water legs have none
        land = [l for l in c.legs if l["mode"] != "water"]
        assert len(c.pair_keys) == len({mm._pair_key(l["from"]["key"], l["to"]["key"], l["mode"]) for l in land})
        # supply / demand balance
        assert math.isclose(sum(c.uses_supply.values()), c.volume, rel_tol=1e-9)
        assert math.isclose(sum(c.uses_demand.values()), c.volume, rel_tol=1e-9)
        for (p, cargo), v in c.uses_supply.items():
            assert cargo == c.cargo and v <= s["sim"].supply[(p, cargo)] + EPS
        for (d, cargo), v in c.uses_demand.items():
            assert v <= s["sim"].demand[(d, cargo)] + EPS
        # feeders end at the hub, trunk starts there
        hub = c.meta["hub_key"]
        for l in c.legs:
            if l["role"] == "feeder":
                assert l["to"]["key"] == hub
            if l["role"] == "trunk":
                assert l["from"]["key"] == hub
        assert c.geometry and all(len(pt) == 2 for pt in c.geometry)
        assert c.vehicles and c.vehicle_capex > 0 and c.infra_capex > 0
        json.dumps(c.legs, ensure_ascii=False)
        json.dumps(c.meta, ensure_ascii=False)
        assert c.title.startswith("Мультимодально")


def test_feeder_lengths_bounded():
    for c in state()["cands"]:
        for l in c.legs:
            if l["mode"] == "road" and l["role"] in ("feeder", "final"):
                assert l["length_m"] <= mm.FEEDER_MAX_M + 1, (c.title, l["length_m"])


# ---------------------------------------------------------------- economics

def test_no_double_counted_income():
    s = state()
    g, nodes = s["pl"].game, s["pl"].nodes
    for c in s["cands"]:
        legs_income = sum(l["income"] for l in c.legs)
        assert abs(legs_income - c.income) <= len(c.legs) + 1, (legs_income, c.income)   # legs are rounded
        cargo = g.cargo_by_key[c.cargo]
        d = next(iter(c.uses_demand))[0]
        D = nodes[d]
        direct = sum(v * g.income_per_unit(cargo, (nodes[p].x, nodes[p].y, nodes[p].z), (D.x, D.y, D.z))
                     for (p, _), v in c.uses_supply.items())
        assert c.income <= direct * (1 + EPS) + 1, (c.title, c.income, direct)
        assert legs_income <= direct * (1 + EPS) + len(c.legs) + 1


def test_platform_to_refinery_chains():
    s = state()
    pl, cands = s["pl"], s["cands"]
    chains = [c for c in cands if c.cargo == "crude_oil"
              and any(pl.nodes[p].water_only for p, _ in c.uses_supply)
              and pl.nodes[next(iter(c.uses_demand))[0]].type == "oil_refinery"]
    assert chains, "no oil platform -> refinery chain"
    # platforms have no direct link to any refinery on this map, so the chain is the only way
    assert all(not c.meta["direct_any"] for c in chains)
    reasonable = [c for c in chains if c.volume >= 30]
    assert reasonable
    assert all(c.profit > 0 for c in reasonable)
    best = max(reasonable, key=lambda c: c.meta["npv"])
    assert best.not_loss_making, (best.title, best.profit, best.depreciation)
    assert best.payback_years < 10, best.payback_years
    # the first leg from a platform must be a ship
    for c in chains:
        for l in c.legs:
            if l["role"] == "feeder" and pl.nodes[l["from"]["key"]].water_only:
                assert l["mode"] == "water"


def test_pruning_vs_direct():
    for c in state()["cands"]:
        assert c.profit > 0
        assert c.meta["advantage"] > 0 or not c.meta["direct_any"], c.title


def test_built_infrastructure_is_free():
    s = state()
    pl, sim = s["pl"], s["sim"]
    c0 = max(s["cands"], key=lambda c: c.meta["npv"])
    built_keys = set(c0.pair_keys)
    built_st = set(c0.station_keys)
    again = mm.generate(pl.game, pl.vehicles, pl, sim.year, sim.supply, sim.demand, built_keys, built_st)
    same = [c for c in again if c.title == c0.title and c.meta["hub_key"] == c0.meta["hub_key"]]
    assert same, "chain disappeared after marking its infrastructure built"
    assert same[0].infra_capex < c0.infra_capex
    assert same[0].maintenance < c0.maintenance


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            t0 = time.perf_counter()
            try:
                fn()
                print(f"PASS {name} ({time.perf_counter() - t0:.1f} s)")
            except Exception as ex:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {type(ex).__name__}: {ex}")
    st = _STATE.get("stats", {})
    print(f"cold generate {_STATE.get('cold_s', 0):.1f} s, {st}")
    sys.exit(1 if failed else 0)

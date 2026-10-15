"""Tests for tf3planner.efficiency (run as a script or with pytest)."""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tf3planner import efficiency as ef  # noqa: E402

_PL = None
_OPTS = None


def planner():
    global _PL
    if _PL is None:
        from tf3planner.planner import Planner
        _PL = Planner()
    return _PL


def options():
    global _OPTS
    if _OPTS is None:
        pl = planner()
        _OPTS = ef.industry_build_options(pl.game, pl.game.year, rank=4, planner=pl)
    return _OPTS


def _state_with_fertilizer(per_refinery: float = 80.0):
    """Initial planner state plus fertilizer at every refinery (crude delivered by a multimodal
    chain) and one grain line out of the biggest farm, so the boost has a per-unit profit."""
    pl = planner()
    sim = pl.initial_state()
    refs = [n.index for n in pl.nodes if n.type == "oil_refinery"]
    for r in refs:
        sim.supply[(r, "fertilizer")] = per_refinery
    farms = [n for n in pl.nodes if n.type == "farm"]
    farm = max(farms, key=lambda n: sum(ef.max_production(pl.game, pl.ind[n.id], c) for c in pl.ind[n.id].outputs))
    line = max((c for c in pl.candidates(sim) if c.p == farm.index), key=pl.score)
    pl._build(sim, line)
    return pl, sim, refs, farm.index


# ---------------------------------------------------------------- game rules

def test_rank_population():
    base = 9998
    pops = [ef.rank_population(base, r) for r in range(1, 16)]
    assert pops[0] == base
    assert all(b > a for a, b in zip(pops, pops[1:]))
    assert pops[3] == 12440                               # rank 4 (Industry permit) at +24 % population
    assert ef.rank_for_population(base, base) == 1
    assert ef.rank_for_population(base, 12440) == 4
    assert abs(ef.world_growth_factor(1) - 1.0) < 1e-3 and abs(ef.world_growth_factor(15) - 12.5) < 0.01


def test_prospection_odds():
    p, months = ef.prospection_odds()
    assert 0.96 < p < 0.98                                # 1 - Π(1 - p_k) with p_k = 0.2 + 0.4 k/6
    assert 1.5 < months < 4


def test_explorations_and_permits():
    by = {e.cargo: e for e in ef.EXPLORATIONS}
    assert by["grain"].permit == by["vegetables"].permit == "permit_key_farm"
    assert by["grain"].rank == 4 and by["fish"].rank == 2 and by["clay"].rank == 3
    assert ef.PERMITS["permit_key_farm"]["rank_and_permits"] == ((4, 1),)
    o = options()
    assert o["industry_builder"] is False and o["sandbox"] is False
    farm = next(p for p in o["permits"] if p["key"] == "permit_key_farm")
    assert farm["unlocked"] and farm["count"] == 1


# ---------------------------------------------------------------- boosters

def test_booster_sources():
    g = planner().game
    bs = ef.booster_sources(g)
    assert set(bs) == {"fertilizer", "tools", "machines"}
    f = bs["fertilizer"]
    assert len(f["sources"]) == 3 and not f["missing"]
    assert all(s["type"] == "oil_refinery" for s in f["sources"])
    assert 0 < f["available_per_year"] <= f["max_per_year"] < f["total_need"]
    # fertilizer = 1/4 of crude: platforms' crude caps it
    crude = sum(ef.max_production(g, i, "crude_oil") for i in g.industries if i.type == "oil_platform")
    assert abs(f["available_per_year"] - min(f["max_per_year"], crude / 4)) < 1.0
    assert bs["tools"]["missing"] and {"tool_factory", "chemical_plant"} <= set(bs["tools"]["missing_types"])
    assert bs["machines"]["missing"] and "steel_mill" in bs["machines"]["missing_types"]
    assert all(n.need > 0 and 0.5 <= n.boost_factor <= 1.5 for n in f["needs"])


def test_weiszfeld():
    P = np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0], [10.0, 10.0]])
    x = ef.weiszfeld(P, np.ones(4))
    assert np.allclose(x, [5, 5], atol=0.05)
    x = ef.weiszfeld(P, np.array([10.0, 1, 1, 1]))       # dominant weight -> its terminal
    assert np.hypot(*x) < 0.2


# ---------------------------------------------------------------- chain siting

def test_tools_chain_sites():
    o = options()
    tc = o["tools_chain"]
    assert tc.feasible
    g = planner().game
    grid = ef.SiteGrid(g)
    for k in ("chemical_plant", "tool_factory"):
        s = tc.facilities[k]
        assert grid.valid[s["cell"]], k
        assert min(math.dist((t.x, t.y), (s["x"], s["y"])) for t in g.towns) >= ef.SITE_TOWN_CLEARANCE - 1
        # the grid optimum is never worse than the snapped Weiszfeld point
        if s["weiszfeld_objective_ratio"] is not None:
            assert s["weiszfeld_objective_ratio"] >= 1 - 1e-9
        assert all(a["objective_vs_best"] >= 1 - 1e-9 for a in tc.alternatives[k])
    a, b = tc.facilities["chemical_plant"], tc.facilities["tool_factory"]
    assert math.dist((a["x"], a["y"]), (b["x"], b["y"])) >= ef.SITE_INDUSTRY_CLEARANCE - 1
    tools = sum(f.volume for f in tc.flows if f.cargo == "tools")
    planks = sum(f.volume for f in tc.flows if f.cargo == "planks")
    assert tools <= ef.recipe_rate(ef.load_recipes()["tool_factory"], "tools")[0] + 1e-6
    assert abs(planks - tools * 0.8) < 1e-6
    v = tc.value
    assert v["boost_profit"] > 0 and v["legs_income"] > 0 and v["capex"] > 0
    assert math.isfinite(v["payback_years"])


def test_machines_chain_infeasible():
    mc = options()["machines_chain"]
    assert not mc.feasible and any("сталь" in n for n in mc.notes)


def test_prospects():
    o = options()
    by = {e["cargo"]: e for e in o["prospects"]}
    assert by["crude_oil"]["unlocked"] and not by["logs"]["unlocked"]      # rank 4
    assert by["clay"]["options"] and by["clay"]["options"][0]["profit"] > 0
    assert not by["coal"]["options"]                      # no coal mine / consumer on the map


# ---------------------------------------------------------------- candidates

def test_booster_candidates():
    pl, sim, refs, farm = _state_with_fertilizer()
    t0 = time.time()
    cs = ef.efficiency_candidates(pl, 1900, sim, set(sim.built_pairs), rank=1, include_workers=False,
                                  include_prospects=False)
    assert time.time() - t0 < 30
    bc = [c for c in cs if c.kind == "booster"]
    assert bc and all(c.cargo == "fertilizer" for c in bc)
    for c in bc:
        (src, cargo), vol = next(iter(c.uses_supply.items()))
        assert src in refs and vol <= 80.0 + 1e-6 and c.volume <= 68 + 1e-6
        (node, what), mult = next(iter(c.boosts.items()))
        assert what == "fertilizer" and 1.0 < mult <= 2.5 and node == c.nodes[-1]
        assert c.meta["transport_income"] > 0
    # the farm with an outbound line gets the boost value on top of the transport income
    mine = [c for c in bc if c.nodes[-1] == farm]
    assert mine and all(c.meta["boost_value"] > 0 for c in mine)
    others = [c for c in bc if c.nodes[-1] != farm]
    assert all(c.meta["boost_value"] == 0 for c in others)


def test_prospect_candidates_and_permits():
    pl = planner()
    sim = pl.initial_state()
    assert not [c for c in ef.efficiency_candidates(pl, 1900, sim, set(), rank=1, include_workers=False)
                if c.kind == "industry_build"]
    cs = [c for c in ef.efficiency_candidates(pl, 1900, sim, set(), rank=4, include_workers=False)
          if c.kind == "industry_build"]
    permits = [c.meta["permit"] for c in cs]
    assert len(permits) == len(set(permits))              # one candidate per permit
    assert "permit_key_farm" in permits and "exploration_crude_oil" in permits
    for c in cs:
        assert c.meta["action"] == "prospect" and c.meta["rank"] <= 4 and c.adds_supply is not None
        assert c.station_keys[0][1] == f"prospect:{c.meta['permit']}"
    # a used permit is not offered again
    farm = next(c for c in cs if c.meta["permit"] == "permit_key_farm")
    again = ef.efficiency_candidates(pl, 1900, sim, set(), rank=4, include_workers=False,
                                     built_stations=set(farm.station_keys))
    assert not [c for c in again if c.meta.get("permit") == "permit_key_farm"]


def test_sandbox_chain_candidate():
    pl = planner()
    cs = ef.efficiency_candidates(pl, 1900, None, set(), rank=1, sandbox=True, include_workers=False,
                                  include_prospects=False)
    ch = [c for c in cs if c.meta.get("action") == "build_chain"]
    assert len(ch) == 1
    c = ch[0]
    assert c.kind == "industry_build" and c.boosts and c.legs and c.capex > 0 and c.income > 0
    assert all(what == "tools" for (_n, what) in c.boosts)


def test_worker_candidates():
    pl, sim, _refs, farm = _state_with_fertilizer()
    cs = ef.efficiency_candidates(pl, 1900, sim, set(sim.built_pairs), rank=1, include_workers=True,
                                  include_prospects=False)
    w = [c for c in cs if c.kind == "workers"]
    assert w and all(c.meta.get("source") == "efficiency" for c in w)
    assert any(farm in c.nodes for c in w)


def test_programs():
    pl = planner()
    f = ef.fertilizer_program(pl, 1900)
    assert f["chosen"] and sum(r["supplied"] for r in f["chosen"]) <= f["available"] + 1e-6
    w = ef.worker_program(pl, 1900)
    assert w and all(r["distance_km"] <= 4.0 for r in w)
    assert w[0]["boost_profit"] >= w[-1]["boost_profit"]


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            t = time.time()
            try:
                fn()
                print(f"ok   {name} ({time.time() - t:.1f} s)")
            except Exception as e:      # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {e!r}")
    print("failed:", failed)
    sys.exit(1 if failed else 0)

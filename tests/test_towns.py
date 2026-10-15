"""Tests for tf3planner.towns (town growth + company rank). Run with pytest or directly:
python tests/test_towns.py"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tf3planner import towns as T  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"

CARGO_IDS = {"vegetables": 9, "meat": 7, "fish": 6, "beverages": 8, "clothes": 27, "tinned_food": 28,
             "planks": 13, "bricks": 33, "fuel": 20, "tools": 30, "furniture": 31, "machines": 16, "vehicles": 14}
ALL_RATINGS = ("urban_care", "traffic_congestion", "people_happiness", "cargo_delivery", "noise", "pollution")


def fake_game(n_towns: int = 3, caps=(300, 300, 300), produced=("vegetables", "bricks", "meat")):
    """Minimal Game: n towns 10 km apart in a row, each asking for vegetables + bricks."""
    cargos = {cid: SimpleNamespace(id=cid, key=k, name=k) for k, cid in CARGO_IDS.items()}
    raw_towns, stats, towns = [], [], []
    for k in range(n_towns):
        tid = 100 + k
        raw_towns.append({"id": tid, "name": f"T{k}", "town": {
            "initialLandUseCapacities": list(caps), "sizeFactors": [1, 1, 1],
            "cargoNeeds": [{}, [[CARGO_IDS["vegetables"], 1]], [[CARGO_IDS["bricks"], 1]]]},
            "capacityUsage": [{"used": c, "capacity": c} for c in caps]})
        stats.append({"id": tid, "ratings": [{"key": r, "value": 1.0} for r in ALL_RATINGS]})
        towns.append(SimpleNamespace(id=tid, name=f"T{k}", x=10000.0 * k, y=0.0, z=0.0,
                                     capacities=dict(zip(("residential", "commercial", "industrial"), caps)),
                                     needs={}))
    export = {"towns": raw_towns, "towns_stats": {"towns": stats},
              "company": {"basePopulation": 9879,
                          "xpForRank": [9879, 10197, 10955, 12298, 14389, 17407, 21547, 27017, 34041, 42866,
                                        53746, 66954, 82778, 101523, 123508],
                          "ticketMultiplierForLevel": [1 + (r - 1) * (0.5 - 1) / 14 for r in range(1, 16)]}}
    return SimpleNamespace(raw=export, towns=towns, cargos=cargos, cargo_by_key={c.key: c for c in cargos.values()},
                           industries=[SimpleNamespace(outputs=[c]) for c in produced], vehicle_cost_scale=1.5,
                           town_dev={"townGrowthMinDistrictFraction": 0.05}, year=1900, year_ms=1461000.0)


def model(game=None, **kw):
    return T.TownModel(game or fake_game(), params=T.TownParams(on_time_share=1.0, trips_per_resident=2.0, **kw))


def full_supply(m: T.TownModel, node: int) -> dict:
    t = m.by_node[node]
    return {(node, c): t.demand(c) for cs in t.needs.values() for c in cs}


# --------------------------------------------------------------------------- formulas

def test_xp_thresholds():
    assert [round(T.xp_for_level(L), 1) for L in range(1, 9)] == [0, 1000, 2500, 4750, 8125, 13187.5, 20781.2,
                                                                  32171.9]
    assert T.level_and_fraction(4750)[0] == 4
    assert T.level_and_fraction(8123)[0] == 4          # game quirk: exp + 1 -> level 5 already at 8124
    assert T.level_and_fraction(8125)[0] == 5
    assert T.level_and_fraction(999)[0] == 1 and T.level_and_fraction(1000)[0] == 2
    lvl, frac = T.level_and_fraction((8125 + 13187.5) / 2)
    assert lvl == 5 and abs(frac - 0.5) < 1e-3


def test_growth_function():
    assert abs(T.growth_factor(1) - 1.0) < 1e-4
    assert abs(T.growth_factor(18) - 40.0) < 0.05
    got = [round(T.growth_factor(l), 2) for l in range(1, 9)]
    assert got == [1.00, 1.46, 1.86, 2.28, 2.75, 3.35, 4.11, 5.11], got
    assert abs(T.world_growth_factor(15) - 12.5) < 0.01


def test_ratings_and_xp_factor():
    assert T.delivery_rating(0.51, 2.0) < 0.01           # VeryHard: rating > 0 needs > 51 % on time
    assert abs(T.delivery_rating(1.0, 2.0) - 1.0) < 1e-9
    assert T.rating_level(T.delivery_rating(0.92, 2.0)) == 5     # Excellent from ~92 %
    assert T.rating_level(T.delivery_rating(0.90, 2.0)) == 4
    assert T.xp_factor(1.0, 1.0, 1.0) == 1.0
    assert T.xp_factor(1.0, 1.0, 0.0) == 0.8              # cargo Excellent, no passengers: supplies 3 -> VeryGood
    assert T.xp_factor(1.0, 0.0, 0.0) == 0.0              # nothing delivered at all
    assert T.xp_factor(0.1, 1.0, 1.0) == 0.0              # one VeryPoor rating caps the town
    assert abs(T.GROWTH_THRESHOLDS[0] - 0.0613) < 1e-3 and T.GROWTH_THRESHOLDS[-1] == 1.0


def test_split_growth_bounds():
    add = T.split_growth(300, [300, 300, 300], [0.0, 1.0, 1.0], [900, 900, 900], 0.05)
    assert abs(sum(add) - 300) < 1e-6
    assert add[0] >= 0.05 * 300 - 1e-6                   # min district share
    add = T.split_growth(300, [100, 100, 700], [0.0, 0.0, 1.0], [1000, 1000, 1000], 0.05)
    assert add[2] <= 0.5 * 300 + 1e-6                    # dampened: industrial already > 40 % of the town


# --------------------------------------------------------------------------- town dynamics

def test_initial_state():
    m = model()
    t = m.towns[0]
    assert t.initial_level == 4 and t.level == 4 and t.xp == T.xp_for_level(4)    # residential > 200
    assert t.needs == {"residential": [], "commercial": ["vegetables"], "industrial": ["bricks"]}
    assert abs(m.totals[(0, "vegetables")] - 90.0) < 1e-9                          # 0.3 x 300 x 1


def test_no_growth_without_service():
    m = model()
    caps0 = [list(t.caps) for t in m.towns]
    for y in range(1901, 1911):
        out = m.step(y, {}, {}, {})
        assert out["demand_delta"] == {}
        assert all(v == 0 for v in out["xp_gained"].values())
    assert [list(t.caps) for t in m.towns] == caps0
    assert all(t.level == 4 for t in m.towns)
    assert m.rank_of(1910) == 1 and abs(m.ticket_multiplier(1910) - 1.0) < 1e-12


def test_steady_deliveries_level_up_at_thresholds():
    m = model()
    t = m.by_node[0]
    out = m.step(1901, full_supply(m, 0), {}, {})
    # 90 vegetables + 90 bricks, tier 0: 1800 raw XP; cargo Excellent, no passengers -> xpFactor 0.8
    assert abs(out["xp_gained"][t.id] - 0.8 * 1800) < 1e-6
    xp_prev, levels = t.xp, {}
    for y in range(1902, 1930):
        out = m.step(y, full_supply(m, 0), {}, {})
        assert t.level == T.level_and_fraction(t.xp)[0]
        for L in range(5, t.level + 1):
            levels.setdefault(L, y)
            # the level is reached in exactly the year the cumulative XP crosses its threshold
            if levels[L] == y:
                assert xp_prev < T.xp_for_level(L) - 1 <= t.xp
        xp_prev = t.xp
    assert levels.get(5) and levels.get(6) and levels[6] > levels[5]
    assert m.by_node[1].level == 4                       # the unserved neighbour does not grow


def test_capacity_follows_growth_function():
    m = model()
    t = m.by_node[0]
    s0 = sum(t.initial_caps)
    for y in range(1901, 1915):
        m.step(y, full_supply(m, 0), {}, {})
        lvl, frac = T.level_and_fraction(t.xp)
        target = s0 * T.growth_factor(lvl + 1 - t.initial_level + frac)
        assert abs(sum(t.caps) - target) < 1.0, (y, sum(t.caps), target)
    assert sum(t.caps) > 1.4 * s0
    # cargo-only service: residential gets only the minimum share
    assert t.caps[0] < t.caps[1] and t.caps[0] < t.caps[2]


def test_demand_grows_with_capacity():
    m = model()
    deltas = {}
    for y in range(1901, 1912):
        out = m.step(y, full_supply(m, 0), {}, {})
        for k, v in out["demand_delta"].items():
            deltas[k] = deltas.get(k, 0) + v
    t = m.by_node[0]
    assert abs(m.totals[(0, "vegetables")] - 0.3 * t.caps[1]) < 1e-6
    assert abs(deltas[(0, "vegetables")] - (0.3 * t.caps[1] - 90)) < 1e-6
    assert all(k[0] == 0 for k in deltas)               # only the served town changed


def test_new_need_at_level_5():
    m = model()
    t = m.by_node[0]
    year = 1900
    while t.level < 5:
        year += 1
        out = m.step(year, full_supply(m, 0), {}, {})
    assert len(t.needs["commercial"]) == 2 and len(t.needs["industrial"]) == 1
    new = t.needs["commercial"][1]
    # least-demanded commercial cargo (all towns ask for vegetables); ties -> produced on the map first
    assert new == "meat", t.needs
    assert (t.id, "meat") in out["new_needs"]
    assert out["demand_delta"][(0, "meat")] > 0
    assert abs(m.totals[(0, "meat")] - 0.3 * t.caps[1]) < 1e-6
    while t.level < 7:
        year += 1
        m.step(year, full_supply(m, 0), {}, {})
    assert len(t.needs["industrial"]) == 2
    assert T.tier_of(t.needs["industrial"][1]) <= 1


def test_no_new_need_without_cargo():
    m = model()
    t = m.by_node[0]
    year = 1900
    while t.level < 5 and year < 2000:      # passengers only: intra-town line, 60 % line usage
        year += 1
        legs = 0.6 * t.residents * 2.0
        m.step(year, {}, {(0, 0): legs}, {0: legs})
    assert t.level >= 5 and not t.cargo_delivered
    assert len(t.needs["commercial"]) == 1
    assert t.caps[0] > t.initial_caps[0]                  # line usage grows the residential district


def test_rank_from_population_and_multiplier():
    g = fake_game()
    ex = g.raw
    assert T.rank_from_population(ex, 9889) == 1
    assert T.rank_from_population(ex, 10197) == 2
    assert T.rank_from_population(ex, 27017) == 8
    assert T.rank_from_population(ex, 1e9) == 15
    # formula without the table reproduces the exported thresholds
    f = T.rank_thresholds({"company": {"basePopulation": 9879}})
    assert all(abs(a - b) <= 1 for a, b in zip(f, ex["company"]["xpForRank"])), f
    mults = [T.ticket_multiplier(r) for r in range(1, 16)]
    assert mults[0] == 1.0 and abs(mults[7] - 0.75) < 1e-9 and abs(mults[14] - 0.5) < 1e-9
    assert all(a > b for a, b in zip(mults, mults[1:]))
    # dynamic: serving every town raises world population, rank and lowers the multiplier
    g = fake_game(n_towns=8, caps=(1300, 1300, 1300))
    m = model(g)
    r0 = m.rank_of(1900)
    for y in range(1901, 1941):
        flows = {}
        for t in m.towns:
            flows.update(full_supply(m, t.node))
        legs = {t.node: 0.6 * t.residents * 2.0 for t in m.towns}
        out = m.step(y, flows, {(n, 0): v for n, v in legs.items()}, legs)
    assert out["world_population"] > 2 * 8 * 1300
    assert m.rank_of(1940) > r0 and m.ticket_multiplier(1940) < m.ticket_multiplier(1900)
    ranks = [m.rank_of(y) for y in range(1900, 1941)]
    assert ranks == sorted(ranks)                        # never decreases


def test_collect_flows_from_sim():
    m = model()
    cand = SimpleNamespace(d=0, cargo="vegetables")
    proj = SimpleNamespace(cand=cand, volume=60.0, income_month=6, start_month=0)
    pax = SimpleNamespace(kind="passenger", legs=[{"nodes": [0], "volume": 40.0}, {"nodes": [0, 1], "volume": 20.0}],
                          nodes=[0, 1], volume=60.0, uses_demand={}, meta={"income_month": 0, "start_month": 0})
    tour = SimpleNamespace(kind="cargo", legs=[], nodes=[5, 1], volume=30.0, uses_demand={(1, "bricks"): 30.0,
                           (5, "clay"): 30.0}, meta={"income_month": 0})
    workers = SimpleNamespace(kind="workers", legs=[], nodes=[0, 7], volume=20.0, uses_demand={}, meta={})
    sim = SimpleNamespace(month=12, projects=[proj], generic=[pax, tour, workers], nodes=list(range(10)))
    cargo_in, arr, legs = m.collect_flows(sim)
    assert abs(cargo_in[(0, "vegetables")] - 30.0) < 1e-9          # active half of the past year
    assert cargo_in[(1, "bricks")] == 30.0 and (5, "clay") not in cargo_in
    assert arr[(0, 0)] == 40.0
    tier = m.pax_tier(0, 1)
    assert tier == 1                                               # adjacent towns in a row are neighbours
    assert arr[(0, tier)] == 10.0 and arr[(1, tier)] == 10.0
    assert m.pax_tier(0, 2) == 2


def test_real_export():
    p = DATA / "export.json"
    if not p.exists():
        return
    from tf3planner.model import Game
    g = Game.load()
    m = T.TownModel(g)
    lv = {t.name: t.level for t in m.towns}
    stats = {t["name"]: t["growth"]["level"] for t in g.raw["towns_stats"]["towns"]}
    assert lv == stats
    assert all(len(t.needs["commercial"]) == 1 and len(t.needs["industrial"]) == 1 for t in m.towns)
    assert abs(m.world_population() - g.raw["company"]["worldPopulation"]) <= 1
    assert m.rank_of(g.year) == 1
    plan_p = DATA / "plan.json"
    if plan_p.exists():
        plan = json.loads(plan_p.read_text(encoding="utf-8"))
        ci, arr, legs = T.plan_flows(plan, m, 1910)
        assert ci and arr and all(k[0] in m.by_node for k in ci)


if __name__ == "__main__":
    import inspect
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for f in fns:
        f()
        print("ok", f.__name__)

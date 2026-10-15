"""Planner cash/supply bookkeeping (audit 2026-10-02): no output from nothing, no double-counted
boosts, delayed first income, rank multiplier, cash projection. Uses the real export but no
routing matrices (a bare Planner)."""
from __future__ import annotations

import math

import pytest

import tf3planner.planner as P
from tf3planner.candidates import ProjectCandidate, VehicleLine
from tf3planner.economics import booster_info, max_production
from tf3planner.routing import load_game, network_nodes
from tf3planner.vehicles import load_vehicles


@pytest.fixture(scope="module")
def pl():
    p = P.Planner.__new__(P.Planner)
    p.game = load_game()
    p.vehicles = load_vehicles(p.game)
    p.horizon = 1910
    p.nodes = network_nodes(p.game)
    p.by_key = {n.key: n for n in p.nodes}
    p.mat, p.water = {}, None
    p.ind = {i.id: i for i in p.game.industries}
    p.town = {t.id: t for t in p.game.towns}
    p.boosters = {i.id: booster_info(p.game, i) for i in p.game.industries}
    p.generators = []
    return p


def _node(pl, typ):
    return next(n for n in pl.nodes if n.kind == "industry" and pl.ind[n.id].type == typ)


def _pc(**kw):
    base = dict(kind="cargo", mode="road", title="t", details="", nodes=[], cargo=None, volume=0.0, income=0.0,
                running=0.0, maintenance=0.0, vehicle_capex=0.0, infra_capex=0.0)
    base.update(kw)
    return ProjectCandidate(**base)


def test_worker_boost_on_processing_plant_needs_inputs(pl):
    sim = pl.initial_state()
    brick = _node(pl, "bricks_works")
    before = sim.supply.get((brick.index, "bricks"), 0.0)
    pl._apply_productivity(sim, brick.index, 2.0)
    # nothing delivered -> no bricks out of nothing, but twice the clay can now be taken
    assert sim.supply.get((brick.index, "bricks"), 0.0) == before == 0.0
    ind = pl.ind[brick.id]
    assert sim.demand[(brick.index, "clay")] == pytest.approx(2 * max_production(pl.game, ind, "clay"))
    # deliver more clay than the old capacity: output limited by inputs and by the doubled capacity
    cap = max_production(pl.game, ind, "bricks")
    pl._deliver(sim, brick.index, "clay", 1.5 * cap)
    assert sim.supply[(brick.index, "bricks")] == pytest.approx(1.5 * cap)


def test_worker_boost_on_primary_adds_output(pl):
    sim = pl.initial_state()
    pit = _node(pl, "clay_pit")
    base = sim.supply[(pit.index, "clay")]
    pl._apply_productivity(sim, pit.index, 2.0)
    assert sim.supply[(pit.index, "clay")] == pytest.approx(2 * base)


def test_booster_counted_once_and_not_as_income(pl):
    sim = pl.initial_state()
    farm = _node(pl, "farm")
    need, bf = pl.boosters[farm.id]["fertilizer"]
    grain0 = sim.supply[(farm.index, "grain")]
    pc = _pc(kind="booster", cargo="fertilizer", nodes=[0, farm.index], income=1_000_000.0, vehicle_capex=500_000.0,
             uses_demand={(farm.index, "fertilizer"): need}, boosts={(farm.index, "fertilizer"): 1 + bf},
             meta={"boost_value": 400_000.0, "boost_vehicle_capex": 100_000.0})
    P.Planner._normalize_boost(pc)
    assert pc.income == pytest.approx(600_000.0) and pc.vehicle_capex == pytest.approx(400_000.0)
    assert pc.meta["value_bonus"] > 0
    P.Planner._normalize_boost(pc)                       # idempotent
    assert pc.income == pytest.approx(600_000.0)
    sim.cash = 1e9
    pl._build_generic(sim, pc)
    base = max_production(pl.game, pl.ind[farm.id], "grain")
    assert sim.supply[(farm.index, "grain")] == pytest.approx(grain0 + base * bf)     # once, not twice


def test_tour_chain_output_is_shipped(pl):
    sim = pl.initial_state()
    brick = _node(pl, "bricks_works")
    pit = _node(pl, "clay_pit")
    pc = _pc(cargo="bricks", nodes=[pit.index, brick.index], income=1.0,
             uses_supply={(pit.index, "clay"): 50.0}, uses_demand={(brick.index, "clay"): 50.0},
             meta={"chain_output": {(brick.index, "bricks"): 50.0}, "trip_s": 2922.0})
    sim.cash = 1e9
    pl._build_generic(sim, pc)
    # the 50 bricks made from this line's clay are carried by the same line -> nothing left to sell twice
    assert sim.supply[(brick.index, "bricks")] == pytest.approx(0.0, abs=1e-9)
    assert pl._moved_out(sim, brick.index, "bricks") == pytest.approx(50.0)
    # first income after half a round trip: 2922 s = 2 game years -> 12 months later
    assert pc.meta["income_month"] - pc.meta["start_month"] == 12


def test_delay_and_ticket_multiplier(pl):
    assert P._delay_months(1461.0 / 2, 1461.0) == 6
    assert P._delay_months(0, 1461.0) == 0
    assert P._first_income_s(_pc(meta={"cycle_s": 400})) == 200
    assert P._first_income_s(_pc(legs=[{"one_way_s": 100}, {"one_way_s": 700}])) == 700
    assert P.ticket_multiplier(pl.game, 1) == pytest.approx(1.0)
    assert P.ticket_multiplier(pl.game, 15) == pytest.approx(0.5)
    assert P.ticket_multiplier(pl.game, 8) == pytest.approx(0.75)


def test_projected_cash_sees_income_delay(pl):
    sim = pl.initial_state()
    sim.cash = 5_000_000.0
    slow = _pc(income=12_000_000.0, running=6_000_000.0, vehicle_capex=1_000_000.0, meta={"trip_s": 2 * 1461.0})
    fast = _pc(income=12_000_000.0, running=6_000_000.0, vehicle_capex=1_000_000.0, meta={"trip_s": 100.0})
    # 4M left, 0.5M/month running: the slow line burns 6M before its first income (12 months)
    assert pl._projected_min_cash(sim, 1_000_000.0, slow) < 0
    assert pl._projected_min_cash(sim, 1_000_000.0, fast) > 0
    assert not pl._cash_ok(sim, 1_000_000.0, slow, buffer=0.0)
    assert pl._cash_ok(sim, 1_000_000.0, fast, buffer=0.0)


def test_growth_counts_generic_shipments(pl):
    sim = pl.initial_state()
    pit = _node(pl, "clay_pit")
    ind = pl.ind[pit.id]
    if ind.max_level <= ind.level:
        pytest.skip("clay pit at max level")
    total = sim.supply[(pit.index, "clay")]
    pc = _pc(cargo="clay", nodes=[pit.index], income=1.0, uses_supply={(pit.index, "clay"): total})
    sim.cash = 1e9
    pl._build_generic(sim, pc)
    assert pl._grow_industries(sim)          # 100 % shipped by a tour/chain -> grows
    assert sim.levels[pit.index] == max(ind.level, 1) + 1

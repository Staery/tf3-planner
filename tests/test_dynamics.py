"""Tests for tf3planner.dynamics. Run with pytest or directly: python tests/test_dynamics.py"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tf3planner import dynamics as D  # noqa: E402
from tf3planner.vehicles import Vehicle  # noqa: E402

YEAR_S = 1461.0


def veh(name, carrier, role, top_kmh, power=0.0, te=0.0, we=10.0, wp=0.0, cap=None, ls=1.0, vid=None):
    return Vehicle(id=vid or abs(hash(name)) % 10_000, file=f"x/{name}.mdl", name=name, carrier=carrier, role=role,
                   year_from=1850, year_to=0, price=100_000, running_cost=10_000, lifespan_years=40,
                   top_speed=top_kmh / 3.6, power_kw=power, tractive_effort_kn=te, weight_empty_t=we,
                   weight_max_payload_t=wp, load_speed=ls, comfort=0.5, price_factor=0.5, capacity=cap or {})


# 1900-ish steam loco (Паровоз Т тип 1) and a boxcar (Грузовой вагон 1899)
LOCO = veh("loco", "rail", "loco", 55, power=140, te=30, we=30)
STRONG = veh("strong", "rail", "loco", 55, power=600, te=120, we=60)
WAGON = veh("wagon", "rail", "wagon", 50, we=10, wp=9.6, cap={"coal": 8})
TRUCK = veh("truck", "road", "truck", 25, power=14, te=8, we=1.0, cap={"coal": 6})
# ship: availPower in W as vehicles.py stores it
SHIP = veh("ship", "water", "ship", 20, power=450_000, we=177, wp=24, cap={"coal": 40})


def train(n_wagons, loco=LOCO):
    return [(loco, 1), (WAGON, n_wagons)]


def test_flat_long_run_approaches_top_speed():
    units = train(5)
    pay = D.payload_t(units, "coal")
    r = D.simulate_run(units, pay, 30_000.0)
    top = min(LOCO.top_speed, WAGON.top_speed)
    assert abs(r.max_speed - top) < 0.05
    assert r.avg_speed > 0.9 * top
    assert r.time_s >= 30_000 / top
    assert not r.stalled
    # the return run (empty) exists and is not slower
    assert r.empty is not None and r.empty.time_s <= r.time_s + 1e-6


def test_time_never_below_distance_over_top_speed():
    for units, pay, prof in [
        (train(3), 0.0, 5_000.0),
        (train(11), D.payload_t(train(11), "coal"), [(0, 0), (4000, 30), (9000, 0)]),
        ([(TRUCK, 1)], 7.2, [(0, 0), (2000, 50), (6000, 50)]),
        ([(SHIP, 1)], 24.0, 8_000.0),
    ]:
        r = D.simulate_run(units, pay, prof)
        top = D.consist_spec(units).top_speed
        L = prof if isinstance(prof, float) else prof[-1][0]
        assert r.time_s >= L / top - 1e-6
        assert r.empty.time_s >= L / top - 1e-6
        assert r.time_lost_s >= -1e-6


def test_steep_grade_slows_weak_loco():
    units = train(8)
    pay = D.payload_t(units, "coal")
    spec = D.consist_spec(units)
    m = spec.mass_empty_t + pay
    v_flat = spec.balancing_speed(0.0, m)
    v_1 = spec.balancing_speed(0.01, m)
    v_2 = spec.balancing_speed(0.02, m)
    assert v_flat > v_1 > v_2
    flat = D.simulate_run(units, pay, 10_000.0, round_trip=False)
    hill = D.simulate_run(units, pay, [(0, 0), (10_000, 150)], round_trip=False)   # 1.5 % uphill
    assert hill.time_s > flat.time_s * 1.2
    assert hill.lost_grade_s > 0
    assert hill.bottleneck and hill.bottleneck["cause"] == "grade"
    # a strong loco is barely affected
    s_units = train(8, STRONG)
    s_hill = D.simulate_run(s_units, pay, [(0, 0), (10_000, 150)], round_trip=False)
    assert s_hill.time_s < hill.time_s


def test_impossible_grade_is_flagged_as_stall():
    units = train(20)
    pay = D.payload_t(units, "coal")
    r = D.simulate_run(units, pay, [(0, 0), (3000, 0), (5000, 120), (8000, 120)], round_trip=False)  # 6 %
    assert r.stalled and r.stall_at_m
    assert 2500 <= r.stall_at_m[0] <= 5000
    spec = D.consist_spec(units)
    assert spec.max_grade(spec.mass_empty_t + pay) < 0.06


def test_heavier_train_accelerates_slower():
    light, heavy = train(2), train(14)
    pl, ph = D.payload_t(light, "coal"), D.payload_t(heavy, "coal")
    perf_l = D.performance(D.consist_spec(light), pl)[0]
    perf_h = D.performance(D.consist_spec(heavy), ph)[0]
    assert perf_h["time_s"] > perf_l["time_s"]
    assert perf_h["dist_m"] > perf_l["dist_m"]
    rl = D.simulate_run(light, pl, 3_000.0, round_trip=False)
    rh = D.simulate_run(heavy, ph, 3_000.0, round_trip=False)
    assert rh.time_s > rl.time_s
    assert rh.lost_accel_s > rl.lost_accel_s


def test_loaded_slower_than_empty_on_hills():
    units = train(10)
    pay = D.payload_t(units, "coal")
    prof = [(0, 0), (3000, 40), (6000, 0), (9000, 40), (12000, 0)]
    r = D.simulate_run(units, pay, prof)
    assert r.time_s > r.empty.time_s


def test_speed_limit_zone_and_braking():
    units = train(4)
    pay = D.payload_t(units, "coal")
    zone = (4000.0, 4500.0, 5.0, "curve")
    r = D.simulate_run(units, pay, 10_000.0, [zone], round_trip=False, sample_m=10)
    spec = D.consist_spec(units)
    inside = [v for s, h, v, c in r.trace if 4000 <= s <= 4500 + spec.length_m]
    assert inside and max(inside) <= 5.0 + 1e-6
    free = D.simulate_run(units, pay, 10_000.0, round_trip=False)
    assert r.time_s > free.time_s
    assert r.lost_limits_s > 0 and r.lost_brake_s > 0
    # braking happens ahead of the zone: speed just before 4000 m is already reduced
    before = [v for s, h, v, c in r.trace if 3950 <= s < 4000]
    assert before and before[-1] < spec.top_speed and before[0] > before[-1]


def test_intermediate_stop_costs_time_and_stops():
    units = train(4)
    pay = D.payload_t(units, "coal")
    a = D.simulate_run(units, pay, 8_000.0, round_trip=False, sample_m=5)
    b = D.simulate_run(units, pay, 8_000.0, stops=[4000.0], round_trip=False, sample_m=5)
    assert b.time_s > a.time_s + 10
    at = [v for s, h, v, c in b.trace if abs(s - 4000) <= 5]
    assert at and min(at) < 0.5


def test_downhill_never_exceeds_top_speed():
    units = train(6)
    r = D.simulate_run(units, D.payload_t(units, "coal"), [(0, 100), (8000, 0)], round_trip=False)
    assert r.max_speed <= D.consist_spec(units).top_speed + 1e-9


def test_truck_and_ship():
    tr = [(TRUCK, 1)]
    spec = D.consist_spec(tr)
    assert spec.mode == "road" and spec.brake == 5.0
    perf = D.performance(spec, D.payload_t(tr, "coal"))
    assert perf[0]["ok"] and perf[0]["max_kmh"] == round(TRUCK.top_speed * 3.6, 1)
    sh = [(SHIP, 1)]
    sspec = D.consist_spec(sh)
    assert sspec.mode == "water" and abs(sspec.power_kw - 450.0) < 1e-6      # W -> kW
    r = D.simulate_run(sh, 24.0, 10_000.0)
    assert abs(r.max_speed - SHIP.top_speed) < 0.05
    assert r.time_s < 1.2 * 10_000 / SHIP.top_speed


def test_cycle_trips_and_fleet():
    units = train(6)
    c = D.cycle(units, 12_000.0, (100.0, 100.0), 8.0, cargo_key="coal", volume=200.0, year_s=YEAR_S)
    assert c.capacity == 48
    assert math.isclose(c.round_trip_s, c.loaded.time_s + c.empty.time_s + 208.0, rel_tol=1e-9)
    assert math.isclose(c.trips_per_year, YEAR_S / c.round_trip_s, rel_tol=1e-9)
    assert math.isclose(c.per_consist_year, 48 * c.trips_per_year, rel_tol=1e-9)
    assert c.vehicles_needed == math.ceil(200.0 / c.per_consist_year)
    assert 0 < c.utilization <= 1


def test_track_profile_respects_max_grade():
    rng = np.random.default_rng(1)
    s = np.arange(0, 5000, 64.0)
    h = np.cumsum(rng.normal(0, 6, len(s)))
    ts, th = D.track_profile(list(zip(s, h)), 5.0, 0.02, smooth_m=0)
    g = np.abs(np.diff(th)) / np.diff(ts)
    assert g.max() <= 0.02 + 1e-9
    ts2, th2 = D.track_profile(list(zip(s, h)), 5.0, 0.02)          # smoothing keeps the bound
    assert (np.abs(np.diff(th2)) / np.diff(ts2)).max() <= 0.02 + 1e-9


def test_curve_radius_and_speed():
    t = np.linspace(0, math.pi, 400)
    R0 = 500.0
    poly = list(zip(R0 * np.cos(t), R0 * np.sin(t)))
    s, R = D.curve_radii(poly, 10.0, sigma_m=50.0)
    mid = R[len(R) // 3: 2 * len(R) // 3]
    assert abs(np.median(mid) - R0) / R0 < 0.05
    v = D.curve_speed(np.array([40.0, 500.0]), {"x": 0.85, "y": 30, "z": 0.6})
    assert v[0] < v[1]
    assert abs(v[1] - 0.85 * 530 ** 0.6) < 1e-9
    straight = [(0.0, 0.0), (1000.0, 0.0), (3000.0, 0.0)]
    _, Rs = D.curve_radii(straight)
    assert np.all(~np.isfinite(Rs) | (Rs > 1e5))


def test_route_like_object_with_bridge_limit():
    class R:  # minimal routing.Route stand-in
        length_m = 6000.0
        polyline = [(0.0, 0.0), (3000.0, 0.0), (6000.0, 0.0)]
        profile = [(0.0, 0.0), (6000.0, 0.0)]
        bridges = [((2000.0, 0.0), (3000.0, 0.0), 300.0)]

    class Way:
        speed = 27.8
        min_radius = 40
        max_slope = 0.13

    class Bridge:
        speed_limit = 16.67

    zones = D.route_speed_limits(R, Way, Bridge, {"x": 0.85, "y": 30, "z": 0.6})
    kinds = {z[3] for z in zones}
    assert "bridge" in kinds and "way" in kinds and "curve" not in kinds
    fast = veh("fast", "rail", "loco", 120, power=2000, te=200, we=80)
    r = D.simulate_run([(fast, 1)], 0.0, R.profile, zones, round_trip=False)
    assert r.lost_limits_s > 0


if __name__ == "__main__":
    import inspect
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for f in fns:
        f()
        print("ok", f.__name__)

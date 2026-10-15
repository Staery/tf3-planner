"""Vehicle dynamics: run times with acceleration, braking, grades, curves and speed limits.

Replaces the crude ``route / (top_speed * 0.85)`` of economics.py with a forward simulation over
distance. Used by dossier.py (detailed route breakdowns) and available to the planner.

Physical model (units: mass t, force kN, power kW, speed m/s -> acceleration m/s^2)
------------------------------------------------------------------------------------
* Traction (all modes with engines): ``F(v) = min(TE, P / v)`` summed over powered units.
  This is the game's own formula (gui/line_vehicle_mgmt/vehicle_util.tl ``calcTractiveEffort``).
  ``baseConfig.trainAccelerationFactor`` (1.0 in the base game) multiplies train acceleration.
* Grade resistance: ``G_GRADE * grade * m`` with ``G_GRADE = 3.27`` (= g / 3). Taken from the
  game GUI (vehicle_util.tl ``calcDownhillSlopeForce``: "local g = 3.27") that rates vehicles for
  "flat / medium (3.75 %) / high (7.5 %)" slopes. The engine value itself is not visible in Lua
  (ASSUMPTION: the GUI mirrors the engine). Downhill grades help (negative force).
* Rolling resistance: ``ROLL_SCALE * friction * g * m``. ``friction`` is the model field
  ``landVehicle.friction`` (game defaults: rail 0.02, road 0.1, model_metadata_util.lua
  ``turnToLandVehicle``). How the engine uses it is unknown; ROLL_SCALE = 0.1 gives real-world
  coefficients (rail 2 per mille, tyres 1 %). ASSUMPTION, calibrate against in-game run times.
* Air drag: ``0.5 * rho * CdA * v^2`` with CdA = front + per unit (rail 6 + 1 m^2/unit, road 4 m^2).
  Small at the game's speeds; ASSUMPTION.
* Ships: ``F = P / max(v, 0.5 * v_top)`` against hydrodynamic drag ``(P / v_top) * (v / v_top)^2``
  (equilibrium at top speed). ``waterVehicle.availPower`` is in W (the game GUI multiplies by
  1e-3); vehicles.py stores it unconverted in ``power_kw``, so values > 20 000 are divided by 1000.
* Aircraft: no usable thrust/drag data in the export -> constant acceleration AIR_ACCEL to top
  speed plus AIR_OVERHEAD_S per flight for taxi / take-off / landing (ASSUMPTION).
* Braking: constant deceleration, game defaults ``brakeDeceleration`` rail/tram 2.5, road 5 m/s^2
  (model_metadata_util.lua); ships SHIP_BRAKE. Braking curves are applied ahead of every lower
  limit and stop (backward pass), so a train is at the limit when its head enters the zone.
* A train keeps a restriction until its tail leaves the zone: zones are extended by the train
  length (rail/tram only).
* Curves: ``v = x * (R + y)^z`` with the way's ``speedCoeffs`` {x, y, z} from the export
  (simple track 0.85, 30, 0.6; old roads 1.6, 1, 0.5). The formula is the TF modding definition
  of speedCoeffs (ASSUMPTION for TF3). Radii come from the route polyline smoothed with a Gaussian
  (CURVE_SMOOTH_M), clamped to the way's minCurveRadius: the planner's grid route has 26.6 deg
  steps that a player lays as smooth curves.
* Track profile: the routing profile is raw terrain/bridge-deck height at grid nodes. The built
  line follows it within the way's max slope: we take the midpoint of the upper and lower
  Lipschitz envelopes (slope <= max_grade), then a moving average (PROFILE_SMOOTH_M).
* Stalls: if a vehicle cannot climb a grade at all (traction < resistance at standstill), it is
  allowed to crawl at CRAWL_SPEED and the result is flagged ``stalled`` (in game it would stop or
  crawl - either way the line does not work as planned).

Time base: game seconds; one game year = game.year_ms / 1000 = 1461 s.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

G = 9.81
G_GRADE = 3.27                # game GUI gravity for slope force (g / 3)
ROLL_SCALE = 0.1              # friction field -> rolling resistance coefficient (ASSUMPTION)
RHO_AIR = 1.2
CDA_FRONT = {"rail": 6.0, "tram": 4.0, "road": 4.0}
CDA_PER_UNIT = {"rail": 1.0, "tram": 1.0, "road": 0.0}
FRICTION_DEFAULT = {"rail": 0.02, "tram": 0.02, "road": 0.1}
BRAKE_DEFAULT = {"rail": 2.5, "tram": 2.5, "road": 5.0, "water": 0.3, "air": 1.5}
SHIP_BRAKE = 0.3
AIR_ACCEL = 1.2
AIR_OVERHEAD_S = 90.0         # taxi + take-off + landing per flight
TRAIN_ACCELERATION_FACTOR = 1.0
UNIT_LENGTH_M = {"rail": 15.0, "tram": 15.0, "road": 8.0, "water": 40.0, "air": 30.0}
CARGO_UNIT_T = 1.2            # weight of one cargo unit when the model has no weightMaxPayload
PASSENGER_UNIT_T = 0.08
CRAWL_SPEED = 1.0             # m/s when traction cannot overcome a grade
CURVE_SMOOTH_M = 100.0        # Gaussian sigma for curvature estimation
PROFILE_SMOOTH_M = 150.0      # moving-average window for the track profile
DEFAULT_DS = 5.0              # integration step, m
STOP_DELAYS_S = 4.0           # arrivalDelay + departureDelay (2000 ms each) per stop
YEAR_S_DEFAULT = 1461.0


# --------------------------------------------------------------------------- consist physics

def _mode_of(v) -> str:
    c = getattr(v, "carrier", "rail")
    return "water" if c == "water" else "air" if c == "air" else c


def _power_kw(v) -> float:
    p = float(getattr(v, "power_kw", 0.0) or 0.0)
    if _mode_of(v) == "water" and p > 20_000:
        p /= 1000.0          # availPower is in W
    return p


def unit_weight_t(v, cargo_key: str | None = None) -> float:
    """Weight of one cargo unit in vehicle v (t): weightMaxPayload / max capacity, else default."""
    cap = max(v.capacity.values()) if getattr(v, "capacity", None) else 0
    if cap and getattr(v, "weight_max_payload_t", 0) > 0:
        return v.weight_max_payload_t / cap
    return PASSENGER_UNIT_T if cargo_key == "passengers" else CARGO_UNIT_T


def payload_t(units: Sequence[tuple], cargo_key: str, fraction: float = 1.0) -> float:
    """Payload of a consist carrying `cargo_key` at `fraction` of its capacity."""
    return sum(v.carries(cargo_key) * n * unit_weight_t(v, cargo_key) for v, n in units) * fraction


@dataclass
class ConsistSpec:
    mode: str
    mass_empty_t: float
    power_kw: float
    te_kn: float
    top_speed: float          # m/s, min over units
    length_m: float
    friction: float           # mass-weighted friction field
    cda_m2: float
    brake: float              # m/s^2
    n_units: int
    n_powered: int

    def traction(self, v: float) -> float:
        """Max traction force (kN) at speed v."""
        if self.mode == "water":
            return self.power_kw / max(v, 0.5 * self.top_speed, 0.1)
        if self.power_kw <= 0:
            return 0.0
        te = self.te_kn if self.te_kn > 0 else self.power_kw / max(0.3 * self.top_speed, 0.5)
        return min(te, self.power_kw / max(v, 1e-3))

    def resistance(self, v: float, grade: float, mass_t: float) -> float:
        """Total resistance force (kN), grade-signed (downhill negative)."""
        if self.mode == "water":
            vt = max(self.top_speed, 0.1)
            return (self.power_kw / vt) * (v / vt) ** 2
        roll = ROLL_SCALE * self.friction * G * mass_t
        drag = 0.5 * RHO_AIR * self.cda_m2 * v * v / 1000.0
        return roll + drag + G_GRADE * grade * mass_t

    def balancing_speed(self, grade: float, mass_t: float) -> float:
        """Steady speed on a constant grade (<= top speed); 0 if it cannot move."""
        if self.traction(0.01) <= self.resistance(0.0, grade, mass_t):
            return 0.0
        lo, hi = 0.0, self.top_speed
        if self.traction(hi) >= self.resistance(hi, grade, mass_t):
            return hi
        for _ in range(50):
            mid = 0.5 * (lo + hi)
            if self.traction(mid) >= self.resistance(mid, grade, mass_t):
                lo = mid
            else:
                hi = mid
        return lo

    def max_grade(self, mass_t: float, v: float = 0.0) -> float:
        """Steepest grade on which the consist still accelerates at speed v (0 = can start)."""
        if self.mode in ("water", "air"):
            return 0.0
        f = 0.95 * self.traction(max(v, 0.01))
        res0 = self.resistance(v, 0.0, mass_t)
        return max(0.0, (f - res0) / (G_GRADE * mass_t)) if mass_t > 0 else 0.0


def consist_spec(units: Sequence[tuple], mode: str | None = None,
                 frictions: dict | None = None) -> ConsistSpec:
    """Aggregate physical parameters of a consist [(Vehicle, count), ...].
    `frictions`: optional {vehicle id: landVehicle.friction} from the export."""
    units = [(v, int(n)) for v, n in units if n > 0]
    if not units:
        raise ValueError("empty consist")
    mode = mode or _mode_of(units[0][0])
    mass = sum(v.weight_empty_t * n for v, n in units)
    power = sum(_power_kw(v) * n for v, n in units)
    te = sum((v.tractive_effort_kn or 0.0) * n for v, n in units)
    tops = [v.top_speed for v, _ in units if v.top_speed > 0]
    top = min(tops) if tops else 10.0
    n_units = sum(n for _, n in units)
    n_powered = sum(n for v, n in units if _power_kw(v) > 0)
    fr_default = FRICTION_DEFAULT.get(mode, 0.02)
    fr_num = sum((frictions or {}).get(v.id, fr_default) * max(v.weight_empty_t, 0.1) * n for v, n in units)
    fr_den = sum(max(v.weight_empty_t, 0.1) * n for v, n in units)
    cda = CDA_FRONT.get(mode, 4.0) + CDA_PER_UNIT.get(mode, 0.0) * n_units
    return ConsistSpec(mode, mass, power, te, top, UNIT_LENGTH_M.get(mode, 15.0) * n_units,
                       fr_num / fr_den if fr_den else fr_default, cda,
                       BRAKE_DEFAULT.get(mode, 2.0), n_units, n_powered)


# --------------------------------------------------------------------------- geometry helpers

def track_profile(profile: Sequence[tuple[float, float]], ds: float = DEFAULT_DS,
                  max_grade: float | None = None, smooth_m: float = PROFILE_SMOOTH_M) -> tuple[np.ndarray, np.ndarray]:
    """Resample (dist, height) to a uniform grid and turn the terrain line into a buildable track
    profile: slope-limited (Lipschitz envelopes midpoint) and smoothed. Returns (s, h)."""
    pr = np.asarray(profile, dtype=float)
    if pr.ndim != 2 or len(pr) < 2:
        L = float(pr[-1][0]) if pr.ndim == 2 and len(pr) else 0.0
        n = max(2, int(math.ceil(L / ds)) + 1)
        return np.linspace(0, L, n), np.zeros(n)
    L = float(pr[-1, 0])
    n = max(2, int(math.ceil(L / ds)) + 1)
    s = np.linspace(0.0, L, n)
    h = np.interp(s, pr[:, 0], pr[:, 1])
    step = s[1] - s[0] if n > 1 else ds
    if max_grade is not None and max_grade > 0:
        g = max_grade * step
        up = h.copy()
        lo = h.copy()
        for i in range(1, n):                 # min_j h[j] + g|i-j| (upper envelope)
            up[i] = min(up[i], up[i - 1] + g)
            lo[i] = max(lo[i], lo[i - 1] - g)
        for i in range(n - 2, -1, -1):
            up[i] = min(up[i], up[i + 1] + g)
            lo[i] = max(lo[i], lo[i + 1] - g)
        h = 0.5 * (up + lo)
    w = int(round(smooth_m / step)) if smooth_m and step > 0 else 0
    if w >= 3 and n > w:
        k = np.ones(w) / w
        pad = np.pad(h, (w // 2, w - 1 - w // 2), mode="edge")
        h = np.convolve(pad, k, mode="valid")
    return s, h


def polyline_cum(poly: Sequence[tuple[float, float]]) -> np.ndarray:
    p = np.asarray(poly, dtype=float)
    if len(p) < 2:
        return np.zeros(len(p))
    return np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(p, axis=0).T))])


def curve_radii(poly: Sequence[tuple[float, float]], step: float = 10.0,
                sigma_m: float = CURVE_SMOOTH_M) -> tuple[np.ndarray, np.ndarray]:
    """Estimated curve radius along a polyline after Gaussian smoothing: (s, R) every `step` m."""
    p = np.asarray(poly, dtype=float)
    cum = polyline_cum(p)
    if len(p) < 3 or cum[-1] < 3 * step:
        return np.array([0.0, cum[-1] if len(cum) else 0.0]), np.array([np.inf, np.inf])
    s = np.arange(0.0, cum[-1] + 1e-9, step)
    x = np.interp(s, cum, p[:, 0])
    y = np.interp(s, cum, p[:, 1])
    sig = max(1.0, sigma_m / step)
    r = int(3 * sig)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sig) ** 2)
    k /= k.sum()
    xs = np.convolve(np.pad(x, r, mode="edge"), k, mode="valid")
    ys = np.convolve(np.pad(y, r, mode="edge"), k, mode="valid")
    dx, dy = np.gradient(xs, step), np.gradient(ys, step)
    ddx, ddy = np.gradient(dx, step), np.gradient(dy, step)
    sp = np.hypot(dx, dy)
    with np.errstate(divide="ignore", invalid="ignore"):
        kappa = np.abs(dx * ddy - dy * ddx) / np.maximum(sp, 1e-9) ** 3
        R = np.where(kappa > 1e-7, 1.0 / kappa, np.inf)
    # the ends are distorted by edge padding: ignore the first/last sigma
    edge = int(sig)
    R[:edge] = np.inf
    R[len(R) - edge:] = np.inf
    return s, R


def curve_speed(R: float | np.ndarray, coeffs: Sequence[float] | dict | None, min_radius: float = 0.0):
    """Curve speed limit v = x * (R + y)^z (m/s); `coeffs` {x, y, z} or [x, y, z]."""
    if coeffs is None:
        return np.full_like(np.asarray(R, dtype=float), np.inf)
    if isinstance(coeffs, dict):
        x, y, z = coeffs.get("x", 0.85), coeffs.get("y", 30.0), coeffs.get("z", 0.6)
    else:
        x, y, z = coeffs
    Rr = np.maximum(np.asarray(R, dtype=float), min_radius)
    with np.errstate(invalid="ignore", over="ignore"):
        return np.where(np.isfinite(Rr), x * (Rr + y) ** z, np.inf)


def speed_coeffs(game, way_key: str | None) -> dict | None:
    """speedCoeffs {x, y, z} of a street/track template from the export."""
    if game is None or not way_key:
        return None
    for s in (getattr(game, "raw", None) or {}).get("streetTemplates", []):
        if str(s.get("name", "")).endswith("/" + way_key + ".street_template"):
            c = (s.get("data") or {}).get("speedCoeffs")
            if isinstance(c, dict):
                return c
            if isinstance(c, list) and len(c) == 3:
                return {"x": c[0], "y": c[1], "z": c[2]}
    return None


def route_speed_limits(route, way=None, bridge=None, coeffs=None, curve_step: float = 10.0,
                       min_gain: float = 0.5) -> list[tuple[float, float, float, str]]:
    """Speed limit zones along a routing.Route: way speed, curves (from the polyline) and bridge
    spans (bridge speed limit). Returns [(from_m, to_m, v_max, reason)] with reason in
    {"way", "curve", "bridge"}. Tunnels have no limit in the game data."""
    L = float(getattr(route, "length_m", 0.0) or 0.0)
    zones: list[tuple[float, float, float, str]] = []
    if way is not None and getattr(way, "speed", 0) > 0:
        zones.append((0.0, L, float(way.speed), "way"))
    poly = list(getattr(route, "polyline", []) or [])
    if coeffs is not None and len(poly) >= 3:
        s, R = curve_radii(poly, curve_step)
        v = curve_speed(R, coeffs, getattr(way, "min_radius", 0.0) if way else 0.0)
        vmax = float(way.speed) if way is not None and way.speed > 0 else np.inf
        scale = L / s[-1] if s[-1] > 0 else 1.0
        limited = v < vmax - min_gain
        i = 0
        while i < len(s):
            if not limited[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(s) and limited[j + 1]:
                j += 1
            # one zone per run, with the minimum (conservative) limit; split long runs in 100 m bits
            k = i
            while k <= j:
                e = min(j, k + int(100 / curve_step))
                zones.append((s[k] * scale, (s[e] + curve_step) * scale, float(v[k:e + 1].min()), "curve"))
                k = e + 1
            i = j + 1
    if bridge is not None and getattr(bridge, "speed_limit", 0) > 0 and poly:
        cum = polyline_cum(poly)
        scale = L / cum[-1] if len(cum) and cum[-1] > 0 else 1.0
        P = np.asarray(poly, dtype=float)
        for a, b, length in getattr(route, "bridges", []) or []:
            ia = int(np.argmin(np.hypot(P[:, 0] - a[0], P[:, 1] - a[1])))
            ib = int(np.argmin(np.hypot(P[:, 0] - b[0], P[:, 1] - b[1])))
            s0, s1 = sorted((cum[ia] * scale, cum[ib] * scale))
            mid, half = 0.5 * (s0 + s1), 0.5 * min(float(length), s1 - s0 if s1 > s0 else float(length))
            zones.append((max(0.0, mid - half), min(L, mid + half), float(bridge.speed_limit), "bridge"))
    return zones


# --------------------------------------------------------------------------- simulation

@dataclass
class RunResult:
    mode: str
    distance_m: float
    time_s: float
    avg_speed: float                 # m/s
    max_speed: float                 # m/s reached
    top_speed: float                 # consist top speed, m/s
    ideal_time_s: float              # distance / top speed
    limits_time_s: float             # time at the speed ceiling everywhere (no accel/brake/grade)
    time_lost_s: float               # time_s - ideal_time_s
    lost_limits_s: float             # line speed, curves, bridges below top speed
    lost_accel_s: float              # accelerating (from stops and after restrictions)
    lost_brake_s: float              # braking ahead of restrictions and stops
    lost_grade_s: float              # power/traction limited on grades
    accel_time_s: float              # time spent accelerating
    brake_time_s: float              # time spent braking
    energy_mj: float                 # traction work at the wheel (proxy for fuel)
    mass_t: float
    payload_t: float
    stalled: bool = False
    stall_at_m: list[float] = field(default_factory=list)
    min_climb_speed: float | None = None
    bottleneck: dict | None = None
    trace: list[tuple[float, float, float, float]] = field(default_factory=list)  # (s, h, v, v_cap)
    empty: "RunResult | None" = None  # the return run (reversed, empty) when round_trip=True

    def summary(self) -> dict:
        d = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.__dict__.items()
             if k not in ("trace", "empty", "bottleneck", "stall_at_m")}
        d["avg_kmh"] = round(self.avg_speed * 3.6, 1)
        d["max_kmh"] = round(self.max_speed * 3.6, 1)
        d["stall_at_m"] = [round(x) for x in self.stall_at_m[:20]]
        d["bottleneck"] = self.bottleneck
        return d


def _zones_array(s: np.ndarray, zones, train_len: float, top: float):
    vlim = np.full(len(s), top)
    why = np.full(len(s), "", dtype=object)
    for z in zones or []:
        a, b, v = float(z[0]), float(z[1]), float(z[2])
        reason = z[3] if len(z) > 3 else "limit"
        if v <= 0:
            continue
        m = (s >= a - 1e-6) & (s <= b + train_len + 1e-6)
        lower = m & (v < vlim)
        vlim[lower] = v
        why[lower] = reason
    return vlim, why


def _simulate_one(spec: ConsistSpec, payload: float, s: np.ndarray, h: np.ndarray, zones, stops,
                  sample_m: float, v_start: float = 0.0, v_end: float = 0.0) -> RunResult:
    n = len(s)
    L = float(s[-1]) if n else 0.0
    m = spec.mass_empty_t + payload
    top = spec.top_speed
    if n < 2 or L <= 0:
        return RunResult(spec.mode, 0.0, 0.0, 0.0, 0.0, top, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                         0.0, m, payload)
    if spec.mode == "air":
        return _simulate_air(spec, payload, L, top)
    ds = float(s[1] - s[0])
    grade = np.gradient(h, ds) if n > 2 else np.zeros(n)
    train_len = spec.length_m if spec.mode in ("rail", "tram") else 0.0
    vcap, why = _zones_array(s, zones, train_len, top)
    # stops: v = 0 at the stop position
    target = vcap.copy()
    stop_idx = sorted({int(round(x / ds)) for x in (stops or []) if 0 < x < L})
    for k in stop_idx:
        target[min(k, n - 1)] = 0.0
    target[-1] = min(target[-1], v_end)
    # backward pass: braking curve
    vb = target.copy()
    b2 = 2.0 * spec.brake * ds
    for i in range(n - 2, -1, -1):
        vb[i] = min(vb[i], math.sqrt(vb[i + 1] ** 2 + b2))
    # forward pass
    acc_factor = TRAIN_ACCELERATION_FACTOR if spec.mode == "rail" else 1.0
    v = np.zeros(n)
    v[0] = min(v_start, vb[0])
    t = 0.0
    energy = 0.0
    lost = {"accel": 0.0, "brake": 0.0, "grade": 0.0}
    accel_t = brake_t = 0.0
    stalled_at: list[float] = []
    lost_step = np.zeros(n)
    tag_step = np.full(n, "", dtype=object)
    min_climb = None
    stop_set = set(stop_idx)
    bal_cache: dict[int, float] = {}
    was_crawling = False
    for i in range(n - 1):
        vi = v[i]
        g = 0.5 * (grade[i] + grade[i + 1])
        f_max = spec.traction(vi)
        res = spec.resistance(vi, g, m)
        a_free = (f_max - res) / m * acc_factor
        v2 = vi * vi + 2 * a_free * ds
        v_free = math.sqrt(v2) if v2 > 0 else 0.0
        cap = min(vcap[i + 1], top)
        vn = min(v_free, cap, vb[i + 1])
        crawl = False
        if vn < CRAWL_SPEED and vb[i + 1] >= CRAWL_SPEED and (i + 1) not in stop_set and i + 1 < n - 1:
            if a_free <= 0 and vi <= CRAWL_SPEED + 1e-6:
                crawl = True
                if not was_crawling:
                    stalled_at.append(float(s[i]))
            vn = max(vn, min(CRAWL_SPEED, vb[i + 1]))
        was_crawling = crawl
        vn = max(vn, 0.0)
        vm = vi + vn
        if vm > 1e-9:
            dt = 2 * ds / vm
        else:
            dt = ds / CRAWL_SPEED
        t += dt
        # energy: force actually applied
        a_act = (vn * vn - vi * vi) / (2 * ds)
        f_app = min(max(m * a_act / acc_factor + res, 0.0), f_max) if not crawl else f_max
        energy += f_app * ds
        # loss accounting relative to the speed ceiling
        ceil_v = min(vcap[i], vcap[i + 1], top)
        t_ceil = ds / ceil_v if ceil_v > 0 else dt
        extra = max(0.0, dt - t_ceil)
        braking = vn < v_free - 1e-6 and vn <= vb[i + 1] + 1e-9 and vb[i + 1] < cap - 1e-6
        if braking or vn < vi - 1e-6 and vn >= vb[i + 1] - 1e-6 and vb[i + 1] < cap:
            lost["brake"] += extra
            brake_t += dt
            tag = "stop"
        elif vn >= cap - 1e-6:
            tag = why[i + 1] if cap < top - 1e-6 else ""
        else:
            # below the ceiling and not braking: accelerating or grade-limited
            if g > 0.002 or a_free < 0.02:
                key = int(round(g * 2000))
                if key not in bal_cache:
                    bal_cache[key] = spec.balancing_speed(key / 2000.0, m)
                bal = bal_cache[key]
            else:
                bal = top
            if bal < cap - 0.05 and (a_free < 0.05 or vn >= bal - 0.3):
                lost["grade"] += extra
                tag = "grade"
                min_climb = float(vn) if min_climb is None else min(min_climb, float(vn))
            else:
                lost["accel"] += extra
                accel_t += dt
                tag = "stop"
        lost_step[i] = dt - ds / top
        tag_step[i] = tag
        v[i + 1] = vn
    ideal = L / top if top > 0 else 0.0
    ceil = np.minimum(vcap, top)
    limits_time = float(np.sum(ds / np.maximum(0.5 * (ceil[:-1] + ceil[1:]), 1e-6)))
    res = RunResult(
        mode=spec.mode, distance_m=L, time_s=float(t), avg_speed=float(L / t) if t > 0 else 0.0,
        max_speed=float(v.max()), top_speed=top, ideal_time_s=ideal, limits_time_s=limits_time,
        time_lost_s=float(t - ideal), lost_limits_s=max(0.0, limits_time - ideal),
        lost_accel_s=float(lost["accel"]), lost_brake_s=float(lost["brake"]), lost_grade_s=float(lost["grade"]),
        accel_time_s=float(accel_t), brake_time_s=float(brake_t), energy_mj=float(energy) / 1000.0,
        mass_t=float(m), payload_t=float(payload),
        stalled=bool(stalled_at), stall_at_m=stalled_at, min_climb_speed=min_climb,
    )
    res.bottleneck = _bottleneck(s, h, grade, v, vcap, why, lost_step, tag_step, top)
    step = max(1, int(round(sample_m / ds)))
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    res.trace = [(float(s[i]), float(h[i]), float(v[i]), float(min(vcap[i], top))) for i in idx]
    return res


REASON_RU = {"grade": "подъём (не хватает мощности/тяги)", "curve": "кривая малого радиуса",
             "bridge": "ограничение скорости моста", "way": "скорость пути/дороги", "stop": "разгон/торможение",
             "": "—", "limit": "ограничение скорости"}


def _bottleneck(s, h, grade, v, vcap, why, lost_step, tag_step, top, window_m: float = 1000.0) -> dict | None:
    """Worst 1 km window by time lost against top speed, excluding accel/brake at stops."""
    n = len(s)
    if n < 3:
        return None
    ds = float(s[1] - s[0])
    w = max(1, int(window_m / ds))
    mask = np.array([t != "stop" for t in tag_step], dtype=float)
    lost = np.maximum(lost_step, 0) * mask
    if lost.sum() <= 1.0:
        return None
    c = np.concatenate([[0.0], np.cumsum(lost)])
    win = c[w:] - c[:-w] if n > w else np.array([c[-1]])
    k = int(np.argmax(win))
    if win[k] < 1.0:
        return None
    a, b = k, min(n - 1, k + w)
    tags: dict[str, float] = {}
    for i in range(a, b):
        if tag_step[i] and tag_step[i] != "stop":
            tags[tag_step[i]] = tags.get(tag_step[i], 0.0) + lost[i]
    cause = max(tags, key=tags.get) if tags else ""
    seg_v = v[a:b + 1]
    return {"from_m": round(float(s[a])), "to_m": round(float(s[b])), "lost_s": round(float(win[k]), 1),
            "cause": cause, "cause_ru": REASON_RU.get(cause, cause),
            "min_kmh": round(float(seg_v.min()) * 3.6, 1), "avg_kmh": round(float(seg_v.mean()) * 3.6, 1),
            "max_grade": round(float(np.max(grade[a:b + 1])), 4),
            "min_limit_kmh": round(float(np.min(np.minimum(vcap[a:b + 1], top))) * 3.6, 1)}


def _simulate_air(spec: ConsistSpec, payload: float, L: float, top: float) -> RunResult:
    a = AIR_ACCEL
    d_acc = top * top / (2 * a)
    if 2 * d_acc >= L:
        vmax = math.sqrt(a * L)
        t = 2 * vmax / a
    else:
        vmax = top
        t = 2 * top / a + (L - 2 * d_acc) / top
    t += AIR_OVERHEAD_S
    ideal = L / top
    return RunResult("air", L, t, L / t, vmax, top, ideal, ideal, t - ideal, 0.0, t - ideal - AIR_OVERHEAD_S,
                     0.0, 0.0, top / a, top / a, 0.0, spec.mass_empty_t + payload, payload,
                     trace=[(0.0, 0.0, 0.0, top), (L, 0.0, 0.0, top)])


def _mirror_zones(zones, L: float):
    out = []
    for z in zones or []:
        a, b = float(z[0]), float(z[1])
        out.append((L - b, L - a, *z[2:]))
    return out


def simulate_run(consist_units: Sequence[tuple], payload_t: float,
                 profile: Sequence[tuple[float, float]] | float,
                 speed_limits: Iterable[tuple] | None = None, stops: Iterable[float] | None = None, *,
                 mode: str | None = None, round_trip: bool = True, empty_payload_t: float = 0.0,
                 ds: float | None = None, sample_m: float = 50.0, max_grade: float | None = None,
                 smooth_m: float = PROFILE_SMOOTH_M, frictions: dict | None = None,
                 spec: ConsistSpec | None = None) -> RunResult:
    """Simulate one run of a consist along `profile` [(distance m, height m), ...] (or a flat
    length in m) with speed limit zones [(from_m, to_m, v_max[, reason])] and intermediate stops
    (positions in m). Starts and ends at standstill.

    Returns the loaded forward run; with round_trip=True its `.empty` holds the return run along
    the reversed profile carrying `empty_payload_t`.
    """
    spec = spec or consist_spec(consist_units, mode, frictions)
    if isinstance(profile, (int, float)):
        profile = [(0.0, 0.0), (float(profile), 0.0)]
    L_total = float(profile[-1][0]) if len(profile) else 0.0
    if ds is None:
        ds = DEFAULT_DS if L_total < 40_000 else L_total / 8000.0
    if spec.mode == "water":
        s, h = track_profile([(0.0, 0.0), (L_total, 0.0)], ds, None, 0)
    else:
        s, h = track_profile(profile, ds, max_grade, smooth_m)
    zones = list(speed_limits or [])
    stops = list(stops or [])
    fwd = _simulate_one(spec, payload_t, s, h, zones, stops, sample_m)
    if round_trip:
        L = float(s[-1])
        back = _simulate_one(spec, empty_payload_t, s, h[::-1].copy(), _mirror_zones(zones, L),
                             [L - x for x in stops], sample_m)
        fwd.empty = back
    return fwd


# --------------------------------------------------------------------------- performance & cycle

def performance(spec: ConsistSpec, payload: float, slopes=(0.0, 0.0375, 0.075)) -> list[dict]:
    """Like the game's vehicle-shop tooltip: for each slope the steady top speed and the time and
    distance to reach 95 % of it from standstill."""
    m = spec.mass_empty_t + payload
    out = []
    for g in slopes:
        vb = spec.balancing_speed(g, m)
        row = {"grade": g, "max_kmh": round(vb * 3.6, 1), "ok": vb > 0.5,
               "time_s": None, "dist_m": None}
        if vb > 0.5 and spec.mode != "air":
            target = 0.95 * vb
            v, t, x, dv = 0.0, 0.0, 0.0, 0.05
            while v < target and t < 3600:
                a = (spec.traction(v) - spec.resistance(v, g, m)) / m
                if a <= 1e-6:
                    break
                dt = dv / a
                t += dt
                x += (v + 0.5 * dv) * dt
                v += dv
            row["time_s"], row["dist_m"] = round(t, 1), round(x)
        out.append(row)
    return out


@dataclass
class CycleResult:
    loaded: RunResult
    empty: RunResult
    load_s: float
    unload_s: float
    dwell_s: float
    round_trip_s: float
    trips_per_year: float
    capacity: float                 # units per trip
    per_consist_year: float         # units/year one consist can move
    vehicles_needed: int | None     # consists for `volume`
    utilization: float | None       # volume / (vehicles_needed * per_consist_year)
    year_s: float

    def summary(self) -> dict:
        return {"loaded": self.loaded.summary(), "empty": self.empty.summary(),
                "load_s": round(self.load_s, 1), "unload_s": round(self.unload_s, 1),
                "dwell_s": round(self.dwell_s, 1), "round_trip_s": round(self.round_trip_s, 1),
                "trips_per_year": round(self.trips_per_year, 3), "capacity": self.capacity,
                "per_consist_year": round(self.per_consist_year, 1), "vehicles_needed": self.vehicles_needed,
                "utilization": None if self.utilization is None else round(self.utilization, 3)}


def cycle(consist_units: Sequence[tuple], route, load_unload_s, dwell_s: float = 2 * STOP_DELAYS_S, *,
          cargo_key: str | None = None, volume: float | None = None, year_s: float = YEAR_S_DEFAULT,
          speed_limits=None, way=None, bridge=None, coeffs=None, max_grade: float | None = None,
          mode: str | None = None, load_fraction: float = 1.0, frictions: dict | None = None,
          stops=None) -> CycleResult:
    """Round trip of one consist: loaded run + empty return + loading/unloading + dwell.

    route: routing.Route (profile, polyline, bridges used), a [(dist, height)] profile or a length.
    load_unload_s: total seconds of loading + unloading per round trip, or (load_s, unload_s).
    dwell_s: other fixed time per round trip (stop delays, manoeuvres, minimum stop times).
    speed_limits: explicit zones; otherwise derived from `way`/`bridge`/`coeffs` and the route.
    volume: units/year to move -> vehicles_needed (consists).
    """
    units = list(consist_units)
    spec = consist_spec(units, mode, frictions)
    if hasattr(route, "profile"):
        profile = route.profile if len(route.profile) >= 2 else [(0.0, 0.0), (route.length_m, 0.0)]
        if speed_limits is None:
            speed_limits = route_speed_limits(route, way, bridge, coeffs) if spec.mode != "water" else []
        if max_grade is None and way is not None:
            max_grade = getattr(way, "max_slope", None)
    else:
        profile = route
    cap = sum(v.carries(cargo_key) * n for v, n in units) if cargo_key else 0
    pay = payload_t(units, cargo_key, load_fraction) if cargo_key else 0.0
    run = simulate_run(units, pay, profile, speed_limits, stops, mode=mode, max_grade=max_grade,
                       spec=spec, round_trip=True)
    if isinstance(load_unload_s, (tuple, list)):
        load_s, unload_s = float(load_unload_s[0]), float(load_unload_s[1])
    else:
        load_s = unload_s = 0.5 * float(load_unload_s)
    rt = run.time_s + run.empty.time_s + load_s + unload_s + dwell_s
    trips = year_s / rt if rt > 0 else 0.0
    per_year = cap * load_fraction * trips
    need = util = None
    if volume is not None and per_year > 0:
        need = max(1, math.ceil(volume / per_year))
        util = volume / (need * per_year)
    return CycleResult(run, run.empty, load_s, unload_s, dwell_s, rt, trips, cap, per_year, need, util, year_s)

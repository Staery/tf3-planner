"""Tour optimization (TSP / VRP) for multi-stop lines: milk-run collection lines, circuits with
backhaul, and stop ordering for passenger lines.

Part 1 - generic solvers on a distance matrix (any metric: route length, build cost, time):
  nearest neighbour, 2-opt, Or-opt, Lin-Kernighan (own: variable-depth sequential flips with
  neighbour lists, breadth (5, 3, 1), tabu on added edges, don't-look bits; chained LK with
  double-bridge kicks), LKH-3 via `elkai`, OR-Tools routing (guided local search), exact
  Held-Karp DP and brute force as ground truth. `solve_tour` adds open paths and fixed ends via a
  dummy node (see `_instance`). CVRP: OR-Tools with a volume and a stop-count dimension, else
  Clarke-Wright savings + exact/LK per route. Benchmark: docs/tours_benchmark.md.

Part 2 - TF3 line economics (`evaluate_line`): a line visits stops in a cycle; TF3 cargo has no
destination (docs/mechanics.md 1.1), so a vehicle loads at producer stops and unloads at any
stop with a matching demand; each unit pays income_per_unit for the straight chord between ITS
pickup and drop-off stop (mechanics.md 1.3), never for the route. Cost: one shared route (sum of
the distinct legs from the routing matrix), one station per stop, one depot, a fleet sized to
the peak leg load and the full cycle time (same formulas as economics.evaluate_link, so a
2-stop line reproduces a direct link). Topologies: "loop" (P1 -> P2 -> C -> P1, all legs built)
and "shuttle" (open path P1 - P2 - C travelled both ways, only the path is built).

Part 3 - candidate generators returning candidates.ProjectCandidate:
  milk_runs()  - k = 2..6 producers of a cargo + their consumer (optionally + a second drop for
                 a second cargo of the same producers, e.g. farm grain + vegetables), kept only
                 when the NPV beats the best separate direct links for the same flows.
  circuits()   - pairs / triangles A -> B -> C -> A where >= 2 legs are loaded, including chain
                 legs (logs -> sawmill, planks sawmill -> town) on universal vehicles.
  passenger_lines(), town_order(), stop_order() - ordering helpers for passenger lines.
Run `python -m tf3planner.tours` for the 1900 report, `... tours bench` for the benchmark.
"""
from __future__ import annotations

import itertools
import math
import random
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

EPS = 1e-9

# --------------------------------------------------------------------------- optional backends

try:                                    # LKH-3 wrapper
    import elkai as _elkai
except Exception:                       # pragma: no cover - optional
    _elkai = None

try:                                    # Google OR-Tools routing
    from ortools.constraint_solver import pywrapcp as _pywrapcp
    from ortools.constraint_solver import routing_enums_pb2 as _renums
except Exception:                       # pragma: no cover - optional
    _pywrapcp = None
    _renums = None

HAVE_ELKAI = _elkai is not None
HAVE_ORTOOLS = _pywrapcp is not None


# =========================================================================== Part 1: TSP solvers

def tour_cost(d, tour: list[int], closed: bool = True) -> float:
    """Length of a tour (closed: returns to tour[0]) or of a path (closed=False)."""
    s = 0.0
    for a, b in zip(tour, tour[1:]):
        s += d[a][b]
    if closed and len(tour) > 1:
        s += d[tour[-1]][tour[0]]
    return float(s)


def _aslist(d) -> list[list[float]]:
    return np.asarray(d, dtype=float).tolist()


def nearest_neighbour(d, start: int = 0) -> list[int]:
    """Greedy nearest-neighbour tour from `start`."""
    D = _aslist(d)
    n = len(D)
    left = set(range(n)) - {start}
    tour = [start]
    while left:
        a = tour[-1]
        b = min(left, key=lambda j: D[a][j])
        tour.append(b)
        left.remove(b)
    return tour


def two_opt(d, tour: list[int] | None = None, time_limit: float | None = None) -> list[int]:
    """First-improvement 2-opt on a symmetric matrix (segment reversal)."""
    D = _aslist(d)
    n = len(D)
    t = list(tour) if tour is not None else nearest_neighbour(D)
    if n < 4:
        return t
    deadline = time.perf_counter() + time_limit if time_limit else None
    improved = True
    while improved:
        improved = False
        for i in range(n - 1):
            a, b = t[i], t[i + 1]
            for j in range(i + 2, n if i > 0 else n - 1):
                c, e = t[j], t[(j + 1) % n]
                if D[a][c] + D[b][e] - D[a][b] - D[c][e] < -1e-10:
                    t[i + 1:j + 1] = t[i + 1:j + 1][::-1]
                    b = t[i + 1]
                    improved = True
            if deadline and time.perf_counter() > deadline:
                return t
    return t


def or_opt(d, tour: list[int] | None = None, max_seg: int = 3, time_limit: float | None = None) -> list[int]:
    """Or-opt: move segments of 1..max_seg consecutive nodes elsewhere (also reversed)."""
    D = _aslist(d)
    n = len(D)
    t = list(tour) if tour is not None else nearest_neighbour(D)
    if n < 5:
        return t
    deadline = time.perf_counter() + time_limit if time_limit else None
    improved = True
    while improved:
        improved = False
        for L in range(1, max_seg + 1):
            i = 0
            while i < n:
                seg = [t[(i + k) % n] for k in range(L)]
                p, nx = t[(i - 1) % n], t[(i + L) % n]
                remove_gain = D[p][seg[0]] + D[seg[-1]][nx] - D[p][nx]
                rest = [t[(i + L + k) % n] for k in range(n - L)]        # cycle without the segment
                best, best_at, best_rev = 1e-10, -1, False
                for k in range(len(rest)):
                    u, v = rest[k], rest[(k + 1) % len(rest)]
                    duv = D[u][v]
                    add = D[u][seg[0]] + D[seg[-1]][v] - duv
                    if remove_gain - add > best:
                        best, best_at, best_rev = remove_gain - add, k, False
                    add = D[u][seg[-1]] + D[seg[0]][v] - duv
                    if remove_gain - add > best:
                        best, best_at, best_rev = remove_gain - add, k, True
                if best_at >= 0:
                    s = seg[::-1] if best_rev else seg
                    t = rest[:best_at + 1] + s + rest[best_at + 1:]
                    improved = True
                i += 1
                if deadline and time.perf_counter() > deadline:
                    return t
    return t


class _LK:
    """Lin-Kernighan local search on a symmetric matrix, array representation.

    One LK move = a sequence of flips t1-t2 ... : with t2 = succ(t1), pick t3 near t2 such that the
    partial gain g - d(t2,t3) > 0, take t4 = pred(t3) and reverse t2..t4. The tour stays
    Hamiltonian after every flip and the closing edge (t4, t1) is a real edge, so the closed gain
    is g + d(t4,t3) - d(t4,t1). Breadth (5, 3, 1) at the first levels, depth up to max_depth,
    an edge added in the move is never removed again (tabu), the best prefix is kept.
    Both tour neighbours of t1 are tried (by reversing the orientation). Don't-look bits.
    """

    def __init__(self, d, neighbours: int = 10, max_depth: int = 50, breadth=(5, 3, 1)):
        self.D = _aslist(d)
        n = self.n = len(self.D)
        self.max_depth = max_depth
        self.breadth = breadth
        k = min(neighbours, n - 1)
        self.neigh = [sorted((j for j in range(n) if j != i), key=lambda j, i=i: self.D[i][j])[:k] for i in range(n)]

    # ---- tour primitives
    def set_tour(self, tour):
        self.t = list(tour)
        self.pos = [0] * self.n
        for i, c in enumerate(self.t):
            self.pos[c] = i

    def succ(self, a):
        p = self.pos[a] + 1
        return self.t[p if p < self.n else 0]

    def pred(self, a):
        return self.t[self.pos[a] - 1]

    def _reverse(self, i, j):
        t, pos, n = self.t, self.pos, self.n
        L = (j - i) % n + 1
        for k in range(L // 2):
            a = (i + k) % n
            b = (j - k) % n
            t[a], t[b] = t[b], t[a]
            pos[t[a]] = a
            pos[t[b]] = b

    def flip(self, a, b):
        """Reverse the path a .. b (a comes first going forward)."""
        self._reverse(self.pos[a], self.pos[b])

    def reverse_all(self):
        self.t.reverse()
        for i, c in enumerate(self.t):
            self.pos[c] = i

    # ---- LK move
    def _dive(self, level, g):
        D, t1 = self.D, self.t1
        t2 = self.succ(t1)
        alts = []
        for t3 in self.neigh[t2]:
            g1 = g - D[t2][t3]
            if g1 <= EPS:
                break                   # neighbours are sorted: the rest is worse
            if t3 == t1 or t3 == t2:
                continue
            t4 = self.pred(t3)
            if t4 == t2:
                continue
            if (min(t3, t4), max(t3, t4)) in self.added:
                continue                # never remove an edge added by this move
            alts.append((g1 + D[t4][t3], t3, t4, g1))
        if not alts:
            return False
        alts.sort(reverse=True)
        width = self.breadth[level] if level < len(self.breadth) else 1
        for _, t3, t4, g1 in alts[:width]:
            self.flip(t2, t4)
            e = (min(t2, t3), max(t2, t3))
            self.stack.append((t2, t4, e))
            self.added.add(e)
            gnew = g1 + D[t4][t3]
            close = gnew - D[t4][t1]
            if close > self.best_gain:
                self.best_gain, self.best_len = close, len(self.stack)
            if level + 1 < self.max_depth and self._dive(level + 1, gnew):
                return True
            if self.best_gain > 1e-10:
                self._rollback(self.best_len)
                return True
            self._rollback(len(self.stack) - 1)
        return False

    def _rollback(self, keep):
        while len(self.stack) > keep:
            a, b, e = self.stack.pop()
            self.flip(b, a)
            self.added.discard(e)

    def improve_node(self, t1) -> list[int] | None:
        """Try an improving LK move starting at t1; returns the touched nodes or None."""
        for orient in (0, 1):
            if orient:
                self.reverse_all()
            self.t1 = t1
            self.stack, self.added = [], set()
            self.best_gain, self.best_len = 1e-10, 0
            t2 = self.succ(t1)
            if self._dive(0, self.D[t1][t2]):
                touched = {t1}
                for a, b, _ in self.stack:
                    touched.update((a, b, self.succ(a), self.pred(a), self.succ(b), self.pred(b)))
                return list(touched)
        return None

    def run(self, tour, active=None, deadline=None):
        self.set_tour(tour)
        q = deque(active if active is not None else self.t)
        inq = [False] * self.n
        for c in q:
            inq[c] = True
        while q:
            if deadline and time.perf_counter() > deadline:
                break
            t1 = q.popleft()
            inq[t1] = False
            touched = self.improve_node(t1)
            if touched:
                for c in touched:
                    if not inq[c]:
                        inq[c] = True
                        q.append(c)
        return self.t


def lin_kernighan(d, tour: list[int] | None = None, time_limit: float | None = None, kicks: int = 0,
                  seed: int = 0, neighbours: int = 10, max_depth: int = 50) -> list[int]:
    """LK local search; with kicks > 0 (or a time_limit) chained LK: double-bridge kicks around a
    random node, LK from the 8 kick endpoints, keep the better tour."""
    D = _aslist(d)
    n = len(D)
    if n <= 3:
        return list(tour) if tour is not None else list(range(n))
    t0 = time.perf_counter()
    deadline = t0 + time_limit if time_limit else None
    lk = _LK(D, neighbours=neighbours, max_depth=max_depth)
    best = lk.run(tour if tour is not None else nearest_neighbour(D), deadline=deadline)
    best = list(best)
    best_c = tour_cost(D, best)
    if n < 8:
        return best
    rng = random.Random(seed)
    k = 0
    while (kicks and k < kicks) or (not kicks and deadline and time.perf_counter() < deadline):
        if deadline and time.perf_counter() > deadline:
            break
        k += 1
        # segment double bridge: cut a window around a random node into A B C D -> A C B D
        t = list(best)
        L = min(n, max(8, min(50, n // 2)))
        s = rng.randrange(n)
        t = t[s:] + t[:s]
        cuts = sorted(rng.sample(range(1, L), 3))
        a, b, c = cuts
        newt = t[:a] + t[b:c] + t[a:b] + t[c:]
        ends = {newt[i % n] for i in (a - 1, a, b - 1, b, c - 1, c, 0, n - 1)}
        cand = lk.run(newt, active=list(ends), deadline=deadline)
        cc = tour_cost(D, cand)
        if cc < best_c - 1e-10:
            best, best_c = list(cand), cc
    return best


def held_karp(d) -> tuple[float, list[int]]:
    """Exact DP for the (asymmetric) closed TSP starting at node 0. O(2^n n^2); n <= ~20."""
    d = np.asarray(d, dtype=float)
    n = len(d)
    if n <= 3:
        best = min(([0] + list(p) for p in itertools.permutations(range(1, n))), key=lambda t: tour_cost(d, t))
        return tour_cost(d, best), best
    m = n - 1
    D = d[1:, 1:]
    full = 1 << m
    dp = np.full((full, m), np.inf)
    par = np.full((full, m), -1, dtype=np.int16)
    for j in range(m):
        dp[1 << j, j] = d[0, j + 1]
    bits = ((np.arange(full)[:, None] >> np.arange(m)) & 1).astype(bool)
    for mask in range(1, full):
        if mask & (mask - 1) == 0:
            continue
        js = np.flatnonzero(bits[mask])
        prev = mask ^ (1 << js)
        cand = dp[prev] + D[:, js].T
        k = cand.argmin(1)
        dp[mask, js] = cand[np.arange(len(js)), k]
        par[mask, js] = k
    final = dp[full - 1] + d[1:, 0]
    j = int(final.argmin())
    cost = float(final[j])
    mask, path = full - 1, []
    while j >= 0:
        path.append(j + 1)
        k = int(par[mask, j])
        mask ^= 1 << j
        j = k
    return cost, [0] + path[::-1]


def held_karp_small(d) -> tuple[float, list[int]]:
    """Held-Karp in pure Python lists: faster than the numpy version for n <= ~10 (no per-mask
    numpy overhead); used for the many tiny milk-run tours."""
    D = _aslist(d)
    n = len(D)
    if n <= 3:
        return held_karp(d)
    m = n - 1
    full = 1 << m
    INF = math.inf
    dp = [[INF] * m for _ in range(full)]
    par = [[-1] * m for _ in range(full)]
    for j in range(m):
        dp[1 << j][j] = D[0][j + 1]
    for mask in range(1, full):
        row = dp[mask]
        for j in range(m):
            if not (mask >> j) & 1 or mask == 1 << j:
                continue
            prev = mask ^ (1 << j)
            prow = dp[prev]
            best, bk = INF, -1
            for k in range(m):
                v = prow[k]
                if v < INF:
                    v += D[k + 1][j + 1]
                    if v < best:
                        best, bk = v, k
            row[j] = best
            par[mask][j] = bk
    last = dp[full - 1]
    j = min(range(m), key=lambda j: last[j] + D[j + 1][0])
    cost = last[j] + D[j + 1][0]
    mask, path = full - 1, []
    while j >= 0:
        path.append(j + 1)
        k = par[mask][j]
        mask ^= 1 << j
        j = k
    return cost, [0] + path[::-1]


def brute_force(d) -> tuple[float, list[int]]:
    """Exhaustive closed TSP from node 0 (n <= 9)."""
    D = _aslist(d)
    n = len(D)
    best, best_t = math.inf, list(range(n))
    for p in itertools.permutations(range(1, n)):
        t = (0,) + p
        c = tour_cost(D, t)
        if c < best:
            best, best_t = c, list(t)
    return best, best_t


def _int_matrix(d, scale_to: float = 1e5) -> tuple[list[list[int]], float]:
    d = np.asarray(d, dtype=float)
    mx = float(np.max(d)) if d.size else 1.0
    s = scale_to / mx if mx > 0 else 1.0
    return np.rint(d * s).astype(np.int64).tolist(), s


def lkh(d, runs: int = 10) -> list[int]:
    """LKH-3 through elkai (integer weights; ATSP handled by LKH)."""
    if _elkai is None:
        raise RuntimeError("elkai not installed")
    n = len(d)
    if n <= 3:
        return list(range(n))
    m, _ = _int_matrix(d, 1e6)
    t = _elkai.DistanceMatrix(m).solve_tsp(runs=runs)
    return t[:-1]


def ortools_tsp(d, time_limit: float = 1.0) -> list[int]:
    """OR-Tools routing: path-cheapest-arc start + guided local search for `time_limit` s."""
    if _pywrapcp is None:
        raise RuntimeError("ortools not installed")
    n = len(d)
    if n <= 3:
        return list(range(n))
    m, _ = _int_matrix(d, 1e7)
    mgr = _pywrapcp.RoutingIndexManager(n, 1, 0)
    rt = _pywrapcp.RoutingModel(mgr)
    try:
        cb = rt.RegisterTransitMatrix(m)
    except Exception:
        cb = rt.RegisterTransitCallback(lambda i, j: m[mgr.IndexToNode(i)][mgr.IndexToNode(j)])
    rt.SetArcCostEvaluatorOfAllVehicles(cb)
    p = _pywrapcp.DefaultRoutingSearchParameters()
    p.first_solution_strategy = _renums.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    p.local_search_metaheuristic = _renums.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    p.time_limit.FromMilliseconds(max(10, int(time_limit * 1000)))
    sol = rt.SolveWithParameters(p)
    if sol is None:
        return nearest_neighbour(d)
    idx, out = rt.Start(0), []
    while not rt.IsEnd(idx):
        out.append(mgr.IndexToNode(idx))
        idx = sol.Value(rt.NextVar(idx))
    return out


# methods that only understand symmetric matrices get (C + C^T) / 2
_SYMMETRIC_ONLY = {"nn", "2opt", "oropt", "lk", "chained_lk"}
METHODS = ("nn", "2opt", "oropt", "lk", "chained_lk", "lkh", "ortools", "held_karp", "brute")


def _solve_closed(C: np.ndarray, method: str, time_limit: float, seed: int) -> list[int]:
    n = len(C)
    if n <= 3:
        return list(range(n))
    S = (C + C.T) / 2 if method in _SYMMETRIC_ONLY else C
    if method == "nn":
        return nearest_neighbour(S)
    if method == "2opt":
        return two_opt(S, nearest_neighbour(S), time_limit=time_limit * 5)
    if method == "oropt":
        return or_opt(S, two_opt(S, nearest_neighbour(S)), time_limit=time_limit * 5)
    if method == "lk":
        return lin_kernighan(S, seed=seed)
    if method == "chained_lk":
        return lin_kernighan(S, time_limit=time_limit, seed=seed)
    if method == "lkh":
        return lkh(C)
    if method == "ortools":
        return ortools_tsp(C, time_limit)
    if method == "held_karp":
        return (held_karp_small(C) if n <= 10 else held_karp(C))[1]
    if method == "brute":
        return brute_force(C)[1]
    raise ValueError(method)


@dataclass
class TourResult:
    order: list[int]          # node indices of the matrix, in visiting order
    cost: float               # closed: incl. the return leg; open: path only
    closed: bool
    method: str
    seconds: float


def _instance(d: np.ndarray, open_: bool, s: int | None, e: int | None) -> tuple[np.ndarray, int | None]:
    """Closed-TSP instance for open / fixed-end variants.

    Open path: add a dummy node z. Arc x -> z costs 0 if x may end the path (any x, or only the
    fixed end), else M; arc z -> y costs 0 if y may start it, else M. Every feasible path then
    costs exactly as the cycle through z; violating tours pay >= M > any feasible tour.
    """
    n = len(d)
    fin = np.isfinite(d)
    mx = float(np.max(np.abs(d[fin]))) if fin.any() else 1.0
    M = (n + 2) * max(mx, 1.0) * 4
    d = np.where(fin, d, M)
    if not open_:
        return d, None
    C = np.zeros((n + 1, n + 1))
    C[:n, :n] = d
    C[:n, n] = M
    C[n, :n] = M
    if e is None:
        C[:n, n] = 0.0
    else:
        C[e, n] = 0.0
    if s is None:
        C[n, :n] = 0.0
    else:
        C[n, s] = 0.0
    return C, n


def solve_tour(dist, open: bool = False, fixed_start: int | None = None, fixed_end: int | None = None,
               method: str = "auto", time_limit: float = 1.0, seed: int = 0) -> TourResult:
    """Order the nodes of `dist` (n x n, may be asymmetric, inf = no link).

    open=False: closed tour starting at fixed_start (default 0); with fixed_end the tour visits
    fixed_end last before returning (solved as an open path start->end plus the closing arc).
    open=True: Hamiltonian path; fixed_start / fixed_end pin its ends.
    method: auto (exact Held-Karp for n <= 10 incl. a dummy node, else LKH if installed, else
    chained LK for time_limit s) or one of
    METHODS. Heuristics that need a symmetric matrix work on (d + d^T)/2; the orientation is then
    chosen on the real matrix.
    """
    t0 = time.perf_counter()
    d = np.asarray(dist, dtype=float)
    n = len(d)
    if n == 0:
        return TourResult([], 0.0, not open, method, 0.0)
    if n == 1:
        return TourResult([0], 0.0, not open, method, 0.0)
    closing = None
    if not open and fixed_end is not None:
        s = fixed_start if fixed_start is not None else None
        if s is None:
            # closed, only the last stop fixed: solve normally, rotate so fixed_end is last
            r = solve_tour(d, False, fixed_end, None, method, time_limit, seed)
            order = r.order[1:] + r.order[:1]
            return TourResult(order, r.cost, True, r.method, time.perf_counter() - t0)
        closing = (fixed_end, s)
        open, fixed_start = True, s
    C, z = _instance(d, open, fixed_start, fixed_end)
    N = len(C)
    m = method
    if m == "auto":
        m = "held_karp" if N <= 10 else ("lkh" if HAVE_ELKAI else "chained_lk")
    if m == "brute" and N > 10:
        m = "held_karp"
    cyc = _solve_closed(C, m, time_limit, seed)
    dd = np.where(np.isfinite(d), d, C[:n, :n] if z is not None else C)
    if z is None:
        st = fixed_start if fixed_start is not None else 0
        k = cyc.index(st)
        a = cyc[k:] + cyc[:k]
        b = [a[0]] + a[1:][::-1]
        order = min((a, b), key=lambda t: tour_cost(dd, t, True))
        cost = tour_cost(dd, order, True)
    else:
        k = cyc.index(z)
        path = cyc[k + 1:] + cyc[:k]
        options = [path, path[::-1]]
        ok = [p for p in options if (fixed_start is None or p[0] == fixed_start)
              and (fixed_end is None or p[-1] == fixed_end)]
        order = min(ok or options, key=lambda t: tour_cost(dd, t, False))
        cost = tour_cost(dd, order, False)
        if closing:
            cost += float(dd[closing[0], closing[1]])
    return TourResult(order, cost, closing is not None or not open, m, time.perf_counter() - t0)


# --------------------------------------------------------------------------- CVRP

def _savings(d, demand, capacity, max_stops, depot) -> list[list[int]]:
    """Clarke-Wright parallel savings."""
    D = _aslist(d)
    cust = [i for i in range(len(D)) if i != depot]
    route_of = {i: [i] for i in cust}
    load = {i: demand[i] for i in cust}
    sav = sorted(((D[depot][i] + D[depot][j] - D[i][j], i, j) for i in cust for j in cust if i < j), reverse=True)
    for s, i, j in sav:
        if s <= 0:
            break
        ri, rj = route_of[i], route_of[j]
        if ri is rj:
            continue
        if load[ri[0]] + load[rj[0]] > capacity + 1e-9 or len(ri) + len(rj) > max_stops:
            continue
        # i and j must be route ends
        if ri[0] == i:
            ri = ri[::-1]
        if rj[-1] == j:
            rj = rj[::-1]
        if ri[-1] != i or rj[0] != j:
            continue
        new = ri + rj
        tot = load[route_of[i][0]] + load[route_of[j][0]]
        for c in new:
            route_of[c] = new
        load[new[0]] = tot
    seen, out = set(), []
    for c in cust:
        r = route_of[c]
        if id(r) not in seen:
            seen.add(id(r))
            out.append(list(r))
    return out


def cvrp(dist, demand: list[float], capacity: float, depot: int = 0, max_stops: int = 6,
         vehicle_fixed: float = 0.0, method: str = "auto", time_limit: float = 2.0) -> list[list[int]]:
    """Partition customers into depot-based routes (lists of customers in visiting order).

    capacity limits the sum of `demand` per route, max_stops the customers per route,
    vehicle_fixed is added per used route (in distance units; discourages many short routes).
    method: auto | ortools | savings (Clarke-Wright + exact/LK improvement per route).
    """
    d = np.asarray(dist, dtype=float)
    n = len(d)
    cust = [i for i in range(n) if i != depot]
    if not cust:
        return []
    if method == "auto":
        method = "ortools" if HAVE_ORTOOLS else "savings"
    if method == "ortools":
        m, s = _int_matrix(d, 1e6)
        dem_scale = 1000.0 / max(max(demand), 1e-9)
        dem = [int(math.ceil(demand[i] * dem_scale)) if i != depot else 0 for i in range(n)]
        cap = int(capacity * dem_scale + 0.5)
        nv = len(cust)
        mgr = _pywrapcp.RoutingIndexManager(n, nv, depot)
        rt = _pywrapcp.RoutingModel(mgr)
        try:
            cb = rt.RegisterTransitMatrix(m)
        except Exception:
            cb = rt.RegisterTransitCallback(lambda i, j: m[mgr.IndexToNode(i)][mgr.IndexToNode(j)])
        rt.SetArcCostEvaluatorOfAllVehicles(cb)
        dcb = rt.RegisterUnaryTransitVector(dem)
        rt.AddDimensionWithVehicleCapacity(dcb, 0, [cap] * nv, True, "load")
        scb = rt.RegisterUnaryTransitVector([0 if i == depot else 1 for i in range(n)])
        rt.AddDimensionWithVehicleCapacity(scb, 0, [max_stops] * nv, True, "stops")
        if vehicle_fixed:
            rt.SetFixedCostOfAllVehicles(int(vehicle_fixed * s))
        p = _pywrapcp.DefaultRoutingSearchParameters()
        p.first_solution_strategy = _renums.FirstSolutionStrategy.SAVINGS
        p.local_search_metaheuristic = _renums.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        p.time_limit.FromMilliseconds(int(time_limit * 1000))
        sol = rt.SolveWithParameters(p)
        if sol is not None:
            out = []
            for v in range(nv):
                idx, r = rt.Start(v), []
                idx = sol.Value(rt.NextVar(idx))
                while not rt.IsEnd(idx):
                    r.append(mgr.IndexToNode(idx))
                    idx = sol.Value(rt.NextVar(idx))
                if r:
                    out.append(r)
            return out
        method = "savings"
    routes = _savings(d, demand, capacity, max_stops, depot)
    out = []
    for r in routes:
        nodes = [depot] + r
        res = solve_tour(d[np.ix_(nodes, nodes)], fixed_start=0)
        out.append([nodes[i] for i in res.order[1:]])
    return out


def routes_cost(dist, routes: list[list[int]], depot: int = 0) -> float:
    return sum(tour_cost(dist, [depot] + r, True) for r in routes)


# =========================================================================== Part 2: line economics

NPV_YEARS = 15           # mirrors planner.NPV_YEARS / DISCOUNT (not imported: planner may import us)
DISCOUNT = 0.05
MAX_STOPS = 6            # producers per milk-run (plus drops)
RAIL_GRADE = 0.01        # ruling grade for rail consists, as planner._candidate


def annuity(years: int = NPV_YEARS, r: float = DISCOUNT) -> float:
    return (1 - (1 + r) ** -years) / r


@dataclass
class Flow:
    p: int                    # pickup node index
    d: int                    # drop node index
    cargo: str
    volume: float             # units / year
    new_output: float = 0.0   # part of the volume produced at p from inputs delivered by this line


@dataclass
class LineEval:
    mode: str
    topology: str             # loop | shuttle
    stops: list[int]          # distinct stops in order
    visits: list[int]         # cyclic visiting sequence (shuttle: there and back)
    flows: list[Flow]
    legs: list[dict]
    cycle_m: float
    infra_m: float
    consist: object
    n: int
    trip_s: float
    income: float
    running: float
    maintenance: float
    vehicle_capex: float
    infra_capex: float
    route_build: float
    station_cost: float
    station_name: str
    depot_cost: float
    depot_name: str
    pair_keys: list[tuple]
    station_keys: list[tuple]
    peak_load: float

    @property
    def capex(self):
        return self.vehicle_capex + self.infra_capex

    @property
    def profit(self):
        return self.income - self.running - self.maintenance

    @property
    def npv(self):
        return self.profit * annuity() - self.capex

    @property
    def volume(self):
        return sum(f.volume for f in self.flows)


def _cache(pl) -> dict:
    c = getattr(pl, "_tours_cache", None)
    if c is None:
        c = pl._tours_cache = {}
    return c


def _game(pl):
    return pl.game


def _catalogue(game):
    from . import facilities
    cat = getattr(game, "_catalogue", None)
    if cat is None:
        cat = game._catalogue = facilities.Catalogue(game)
    return cat


def _water_ok(pl, i, j) -> bool:
    f = getattr(pl, "water_ok", None)
    return bool(f(i, j)) if f else True


def leg_length(pl, mode: str, i: int, j: int) -> float | None:
    if i == j:
        return 0.0
    if mode == "water":
        L = float(pl.water.length[i, j])
        return L if math.isfinite(L) and _water_ok(pl, i, j) else None
    if pl.nodes[i].water_only or pl.nodes[j].water_only:
        return None
    L = float(pl.mat[mode].length[i, j])
    return L if math.isfinite(L) else None


def length_matrix(pl, mode: str, nodes: list[int]) -> np.ndarray:
    m = np.full((len(nodes), len(nodes)), np.inf)
    for a, i in enumerate(nodes):
        for b, j in enumerate(nodes):
            L = leg_length(pl, mode, i, j)
            if L is not None:
                m[a, b] = L
    return m


def _consists(pl, mode: str, cargos: tuple[str, ...], year: int):
    """Vehicle sets able to carry ALL `cargos` (universal trucks / wagons / ships).
    Returns [(Consist template, per-unit capacity, n_max wagons or 1)]."""
    from .economics import Consist, best_consists
    key = ("consists", mode, cargos, year)
    cache = _cache(pl)
    if key in cache:
        return cache[key]
    out = []
    for c in best_consists(pl.game, pl.vehicles, mode, cargos[0], year, max_grade=RAIL_GRADE):
        unit, n = c.units[-1]
        cap = min(unit.carries(k) for k in cargos)
        if cap <= 0 or c.speed <= 0:
            continue
        if mode == "rail":
            loco = c.units[0][0]
            out.append((Consist(mode, [(loco, 1), (unit, n)], n * cap, c.speed, c.price, c.running_cost,
                                c.load_speed), cap, n))
        else:
            out.append((Consist(mode, c.units, cap, c.speed, c.price, c.running_cost, c.load_speed), cap, 1))

    # drop dominated options: same carrying unit, not faster, not stronger, not cheaper to buy or run
    def dominated(a, b):
        (ca, ua, na), (cb, ub, nb) = a, b
        return (ua == ub and cb.speed >= ca.speed and nb >= na
                and cb.price <= ca.price and cb.running_cost <= ca.running_cost and a is not b
                and (cb.speed, nb, -cb.price, -cb.running_cost) != (ca.speed, na, -ca.price, -ca.running_cost))
    out = [a for a in out if not any(dominated(a, b) for b in out)]
    cache[key] = out
    return out


def _is_built(built: set, key) -> bool:
    return key in built


_LOAD_FACTOR: dict = {}


def _load_time(game, consist_capacity: float, load_speed: float, cargo: str, n_units: int) -> float:
    """Seconds to fill a consist; mirrors economics.evaluate_link (capacity.loading_time_s:
    vehicles load in parallel, cargo loadSpeedFactor); falls back to capacity / loadSpeed."""
    try:
        from . import capacity as _cap
    except Exception:
        return consist_capacity / max(load_speed, 0.1)
    key = (id(game), cargo)
    if key not in _LOAD_FACTOR:
        _LOAD_FACTOR[key] = _cap.cargo_load_factor(game, cargo)
    return _cap.loading_time_s(consist_capacity, load_speed, _LOAD_FACTOR[key], n_units=n_units)


def evaluate_line(pl, year: int, mode: str, stops: list[int], flows: list[Flow], built_keys: set | None = None,
                  topology: str = "loop") -> LineEval | None:
    """Economics of one line visiting `stops` (loop: in this cyclic order; shuttle: the open path
    stops[0] .. stops[-1] and back). Flows ride from an occurrence of p to the next occurrence
    of d. Same formulas as economics.evaluate_link (cycle, handling, stop delays, fleet ceil,
    terminal occupancy, rail train sizing, delivery limit for towns)."""
    from . import facilities
    from .economics import MAX_OCCUPANCY, STOP_DELAY_S, VEHICLE_LENGTH_M
    built = built_keys or set()
    game = pl.game
    flows = [f for f in flows if f.volume > 1e-6 and f.p != f.d]
    if not flows or len(stops) < 2:
        return None
    visits = list(stops) if topology == "loop" else list(stops) + list(stops[-2:0:-1])
    m = len(visits)
    legs_len = []
    for a in range(m):
        L = leg_length(pl, mode, visits[a], visits[(a + 1) % m])
        if L is None:
            return None
        legs_len.append(L)
    cum = np.concatenate([[0.0], np.cumsum(legs_len)])
    cycle = float(cum[-1])
    if cycle <= 0:
        return None
    # ---- route infrastructure: distinct legs not yet built
    route_build = route_maint = infra_m = 0.0
    pair_keys = []
    for a in range(m):
        i, j = visits[a], visits[(a + 1) % m]
        k = (min(i, j), max(i, j), mode)
        if k in pair_keys:
            continue
        pair_keys.append(k)
        infra_m += legs_len[a]
        if mode != "water" and not _is_built(built, k):
            route_build += float(pl.mat[mode].build[i, j])
            route_maint += float(pl.mat[mode].maint[i, j])
    # ---- flows along the cycle: leg loads and ride distance
    loads = np.zeros(m)
    rides = []
    for f in flows:
        best = None
        for a in (k for k, v in enumerate(visits) if v == f.p):
            for b in (k for k, v in enumerate(visits) if v == f.d):
                dist = (cum[b] - cum[a]) if b > a else (cycle - cum[a] + cum[b])
                if best is None or dist < best[0]:
                    best = (dist, a, b)
        dist, a, b = best
        k = a
        while k != b:
            loads[k] += f.volume
            k = (k + 1) % m
        rides.append(dist)
    peak = float(loads.max())
    total = sum(f.volume for f in flows)
    cargos = tuple(sorted({f.cargo for f in flows}))
    cargo_objs = [game.cargo_by_key[c] for c in cargos]
    classes = sorted({cl for c in cargo_objs for cl in c.classes})
    income = sum(f.volume * game.income_per_unit(game.cargo_by_key[f.cargo],
                                                 (pl.nodes[f.p].x, pl.nodes[f.p].y, pl.nodes[f.p].z),
                                                 (pl.nodes[f.d].x, pl.nodes[f.d].y, pl.nodes[f.d].z))
                 for f in flows)
    year_s = game.year_ms / 1000.0
    cat = _catalogue(game)
    any_station = any((s, mode) in built for s in stops)
    best: LineEval | None = None
    for c, ucap, n_max in _consists(pl, mode, cargos, year):
        v = c.speed * 0.85
        run_t = cycle / v
        delay = m * STOP_DELAY_S
        if mode == "rail":
            (loco, _), (wagon, _) = c.units
            load_w = max(_load_time(game, ucap * n_max, c.load_speed, k, n_max) for k in cargos)
            trip_w = run_t + 2 * load_w + delay
            per_wagon_year = ucap * year_s / trip_w
            wagons_needed = max(1, math.ceil(peak / per_wagon_year))
            n_trains = math.ceil(wagons_needed / n_max)
            n_w = math.ceil(wagons_needed / n_trains)
            from .economics import Consist
            c = Consist(mode, [(loco, 1), (wagon, n_w)], n_w * ucap, c.speed, loco.price + n_w * wagon.price,
                        loco.running_cost + n_w * wagon.running_cost, c.load_speed)
        cap = c.capacity
        n_units = sum(k for u, k in c.units if u.capacity) or 1
        per_unit = {k: _load_time(game, cap, c.load_speed, k, n_units) / cap for k in cargos}
        load_t = cap * max(per_unit.values())                # fill / empty the whole consist
        trips = peak / cap                                   # trips / year needed
        handling = sum(2 * f.volume * per_unit[f.cargo] for f in flows)   # s / year, load + unload
        busy = trips * (run_t + delay) + handling
        n = max(1, math.ceil(busy / year_s - 1e-9))
        trip = run_t + delay + handling / max(trips, 1e-9)
        # delivery limit for town drops (late cargo ruins the town rating)
        late = False
        for f, ride in zip(flows, rides):
            co = game.cargo_by_key[f.cargo]
            if pl.nodes[f.d].kind == "town" and co.time_to_deliver_s and ride / v + load_t > co.time_to_deliver_s:
                late = True
                break
        if late:
            continue
        units = sum(k for _, k in c.units)
        occupancy = n * (load_t + STOP_DELAY_S) / max(trip, 1e-9)
        terminals = max(1, math.ceil(occupancy / MAX_OCCUPANCY))
        st = facilities.station(cat, mode, year, classes, units * VEHICLE_LENGTH_M, terminals)
        dp = facilities.depot(cat, mode, year, n)
        new_st = [s for s in stops if (s, mode) not in built]
        dep_cost = 0.0 if any_station else dp.cost
        dep_maint = 0.0 if any_station else dp.maintenance
        infra = route_build + len(new_st) * st.cost + dep_cost
        ev = LineEval(mode, topology, list(stops), visits, flows,
                      [{"from": visits[a], "to": visits[(a + 1) % m], "length_m": round(legs_len[a]),
                        "load": round(float(loads[a]), 1)} for a in range(m)],
                      cycle, infra_m, c, n, trip, income, n * c.running_cost,
                      route_maint + len(new_st) * st.maintenance + dep_maint,
                      n * c.price, infra, route_build, st.cost, st.name, dep_cost, dp.name,
                      pair_keys, [(s, mode) for s in stops], peak)
        if best is None or (ev.profit - ev.capex / 10) > (best.profit - best.capex / 10):
            best = ev
    return best


# =========================================================================== Part 3: generators

MODE_RU = {"rail": "ж/д", "road": "дорога", "water": "судно", "tram": "трамвай"}
NEAREST_CONSUMERS = 4          # circuits: consumers tried per loaded leg
CVRP_TIME_S = 0.1             # OR-Tools budget per (cargo, consumer, mode) partition
MAX_VKM_RATIO = 3.0           # milk-run pre-filter: line vehicle-km / direct vehicle-km (winners <= 2.2 on the 1900 map)
NEAREST_PRODUCERS = None      # milk-runs: producers per consumer by route length (None = all; far ones pay more)
SECOND_DROPS = 2              # mixed milk-runs: nearest consumers tried for the second cargo
PAX_TRIPS_PER_RESIDENT = 2.0   # passenger_lines default OD (ASSUMPTION, replace by passengers.py)
PAX_DECAY_M = 15000.0


def _ipu(pl, cargo: str, p: int, d: int) -> float:
    key = ("ipu", cargo, p, d)
    cache = _cache(pl)
    if key not in cache:
        a, b = pl.nodes[p], pl.nodes[d]
        cache[key] = pl.game.income_per_unit(pl.game.cargo_by_key[cargo], (a.x, a.y, a.z), (b.x, b.y, b.z))
    return cache[key]


def _industry(pl, node_index: int):
    n = pl.nodes[node_index]
    if n.kind != "industry":
        return None
    ind = getattr(pl, "ind", None)
    if ind and n.id in ind:
        return ind[n.id]
    return next((i for i in pl.game.industries if i.id == n.id), None)


def _chain_outputs(pl, node_index: int, delivered: dict[str, float], supply: dict, demand_cargos: set) -> dict[str, float]:
    """Extra output of a processing industry from inputs delivered by the line (single-input rules)."""
    from .economics import conversion, max_production
    ind = _industry(pl, node_index)
    if ind is None or not ind.inputs:
        return {}
    out = {}
    for o, needs in conversion(ind).items():
        if o not in demand_cargos or len(needs) != 1:
            continue
        (i, ratio), = needs.items()
        if delivered.get(i, 0) <= 0:
            continue
        cap = max_production(pl.game, ind, o) - supply.get((node_index, o), 0.0)
        new = min(ratio * delivered[i], max(cap, 0.0))
        if new >= 0.5:
            out[o] = new
    return out


@dataclass
class Allocation:
    flows: list[Flow]
    uses_supply: dict
    uses_demand: dict
    adds_supply: dict


def allocate(pl, stops: list[int], supply: dict, demand: dict, cargos: set | None = None,
             chain: bool = True) -> Allocation:
    """Greedy flow assignment among the stops: every (pickup, drop, cargo) with free supply and
    demand, highest income per unit first (farther drops pay more); then outputs that processing
    stops make from inputs delivered by this line (chain legs, if chain=True; otherwise they are
    only reported in adds_supply)."""
    S = set(stops)
    sup = {k: v for k, v in supply.items() if k[0] in S and v >= 1 and (cargos is None or k[1] in cargos)}
    dem = {k: v for k, v in demand.items() if k[0] in S and v >= 1 and (cargos is None or k[1] in cargos)}
    new_left: dict = {}
    flows: list[Flow] = []

    def assign():
        pairs = sorted(((_ipu(pl, c, p, d), p, d, c) for (p, c) in sup for (d, c2) in dem
                        if c2 == c and p != d), reverse=True)
        for _, p, d, c in pairs:
            v = min(sup[(p, c)], dem[(d, c)])
            if v < 0.5:
                continue
            fresh = min(v, new_left.get((p, c), 0.0))
            new_left[(p, c)] = new_left.get((p, c), 0.0) - fresh
            sup[(p, c)] -= v
            dem[(d, c)] -= v
            flows.append(Flow(p, d, c, v, fresh))

    assign()
    # processing stops turn delivered inputs into outputs: carried by this line when chain=True
    # (and the cargo filter allows), otherwise reported as adds_supply for other projects
    demand_cargos = {c for (_, c), v in demand.items() if v >= 1}
    added = False
    for x in stops:
        delivered = {}
        for f in flows:
            if f.d == x:
                delivered[f.cargo] = delivered.get(f.cargo, 0) + f.volume
        for o, new in _chain_outputs(pl, x, delivered, supply, demand_cargos).items():
            new_left[(x, o)] = new_left.get((x, o), 0.0) + new
            if chain and (cargos is None or o in cargos):
                sup[(x, o)] = sup.get((x, o), 0.0) + new
                added = True
    if added:
        assign()
    uses_s, uses_d, adds = {}, {}, {}
    for f in flows:
        if f.volume - f.new_output > 1e-9:
            uses_s[(f.p, f.cargo)] = uses_s.get((f.p, f.cargo), 0) + f.volume - f.new_output
        uses_d[(f.d, f.cargo)] = uses_d.get((f.d, f.cargo), 0) + f.volume
    for k, v in new_left.items():
        if v > 0.5:
            adds[k] = v
    return Allocation(flows, uses_s, uses_d, adds)


def order_stops(pl, mode: str, stops: list[int], topology: str,
                fixed_start: int | None = None) -> tuple[list[int], float] | None:
    """Best visiting order: loop = closed TSP, shuttle = open Hamiltonian path (route length).
    Cached per (mode, stop list, topology): the routing matrices do not change within a run."""
    key = ("order", mode, tuple(stops), topology, fixed_start)
    cache = _cache(pl)
    if key in cache:
        return cache[key]
    m = length_matrix(pl, mode, stops)
    off = ~np.eye(len(stops), dtype=bool)
    if not np.isfinite(m[off]).all():
        cache[key] = None
        return None
    fs = stops.index(fixed_start) if fixed_start in stops else None
    r = solve_tour(m, open=(topology == "shuttle"), fixed_start=fs)
    cache[key] = ([stops[i] for i in r.order], r.cost)
    return cache[key]


def best_line(pl, year: int, mode: str, stops: list[int], flows: list[Flow], built: set,
              topologies=("loop", "shuttle")) -> LineEval | None:
    """Best of: loop in TSP order (both directions - the onboard load differs) and shuttle on
    the shortest open path. Memoized: the planner calls the generators repeatedly with mostly
    unchanged supply, so identical (stops, flows, relevant built keys) are evaluated once."""
    key = ("line", year, mode, tuple(stops), tuple(topologies),
           tuple((f.p, f.d, f.cargo, round(f.volume, 2)) for f in flows), _relevant_built(built, stops, mode))
    cache = _cache(pl)
    if key in cache:
        return cache[key]
    best = None
    for topo in topologies:
        o = order_stops(pl, mode, stops, topo)
        if o is None:
            continue
        order = o[0]
        variants = [order, [order[0]] + order[1:][::-1]] if topo == "loop" and len(order) > 2 else [order]
        for seq in variants:
            ev = evaluate_line(pl, year, mode, seq, flows, built, topo)
            if ev and (best is None or ev.npv > best.npv):
                best = ev
    cache[key] = best
    return best


def _relevant_built(built: set, nodes, mode) -> frozenset:
    nodes = set(nodes)
    return frozenset(k for k in built if (len(k) == 2 and k[0] in nodes and k[1] == mode)
                     or (len(k) == 3 and k[0] in nodes and k[1] in nodes and k[2] == mode))


def direct_line(pl, year: int, mode: str, f: Flow, built: set) -> LineEval | None:
    key = ("direct", mode, f.p, f.d, f.cargo, round(f.volume, 2), year, _relevant_built(built, (f.p, f.d), mode))
    cache = _cache(pl)
    if key not in cache:
        cache[key] = evaluate_line(pl, year, mode, [f.p, f.d], [Flow(f.p, f.d, f.cargo, f.volume)], built, "shuttle")
    return cache[key]


def direct_alternative(pl, year: int, flows: list[Flow], modes, built: set) -> dict:
    """Best set of separate direct links for the same flows: each flow in its best mode, built in
    order of NPV with stations/depots shared at common nodes; loss-making links are left out."""
    def best_for(f, b):
        evs = [ev for ev in (direct_line(pl, year, m, f, b) for m in modes) if ev]
        return max(evs, key=lambda e: e.npv) if evs else None
    first = [(best_for(f, built), f) for f in flows]
    first.sort(key=lambda x: -(x[0].npv if x[0] else -math.inf))
    local = set(built)
    lines = []
    for _, f in first:
        ev = best_for(f, local)
        if ev is None or ev.npv <= 0:
            continue
        lines.append(ev)
        local.update(ev.pair_keys)
        local.update(ev.station_keys)
    return {"npv": sum(e.npv for e in lines), "profit": sum(e.profit for e in lines),
            "capex": sum(e.capex for e in lines), "income": sum(e.income for e in lines),
            "running": sum(e.running for e in lines), "maintenance": sum(e.maintenance for e in lines),
            "volume": sum(e.volume for e in lines), "lines": lines}


def _short(name: str) -> str:
    return name.replace(" — ", ": ")


def _geometry(pl, ev: LineEval) -> list[list[float]] | None:
    seq = ev.visits + [ev.visits[0]] if ev.topology == "loop" else ev.stops
    if ev.mode != "water":
        # land lines: legs share one physical way (routing.line_routes), snapped to existing roads
        try:
            from .routing import line_routes, line_json
            js = line_json(line_routes(pl.mat[ev.mode], list(zip(seq, seq[1:]))))
            ev._segments, ev._segment_totals = js["segments"], js["segment_totals"]
            if js.get("geometry"):
                return js["geometry"]
        except Exception:
            ev._segments, ev._segment_totals = None, None
    pts: list[list[float]] = []
    for a, b in zip(seq, seq[1:]):
        try:
            r = pl.water.route(a, b) if ev.mode == "water" else pl.mat[ev.mode].route(a, b)
        except Exception:
            r = None
        if r is None:
            pts += [[round(pl.nodes[a].x, 1), round(pl.nodes[a].y, 1)],
                    [round(pl.nodes[b].x, 1), round(pl.nodes[b].y, 1)]]
            continue
        poly = list(r.polyline[:: max(1, len(r.polyline) // 40)]) + [r.polyline[-1]]
        pts += [[round(x, 1), round(y, 1)] for x, y in poly]
    return pts or None


def _money(x: float) -> str:
    return f"{x / 1e6:.2f} млн"


def _chain_output(flows) -> dict:
    out: dict = {}
    for f in flows:
        if f.new_output > 1e-9:
            out[(f.p, f.cargo)] = out.get((f.p, f.cargo), 0.0) + f.new_output
    return out


def to_candidate(pl, ev: LineEval, alt: dict, alloc: Allocation, label: str, kind: str = "cargo",
                 geometry: bool = True):
    """LineEval -> ProjectCandidate (Russian title/details, tour + comparison in meta)."""
    from .candidates import ProjectCandidate, VehicleLine
    game = pl.game
    names = [pl.nodes[s].name for s in ev.stops]
    by_cargo: dict[str, float] = {}
    inc_cargo: dict[str, float] = {}
    for f in ev.flows:
        by_cargo[f.cargo] = by_cargo.get(f.cargo, 0) + f.volume
        inc_cargo[f.cargo] = inc_cargo.get(f.cargo, 0) + f.volume * _ipu(pl, f.cargo, f.p, f.d)
    main = max(inc_cargo, key=inc_cargo.get)
    cargo_txt = ", ".join(f"{game.cargo_by_key[c].name.lower()} {v:.0f}" for c, v in by_cargo.items())
    route_txt = " → ".join(_short(n) for n in names) + (
        f" → {_short(names[0])}" if ev.topology == "loop" else " и обратно")
    topo_ru = "кольцо" if ev.topology == "loop" else "челнок туда-обратно"
    gain = ev.npv - alt["npv"]
    flows_txt = "; ".join(f"{_short(pl.nodes[f.p].name)} → {_short(pl.nodes[f.d].name)}: "
                          f"{game.cargo_by_key[f.cargo].name.lower()} {f.volume:.0f}" for f in ev.flows)
    details = (f"Маршрут ({topo_ru}): {route_txt}; цикл {ev.cycle_m / 1000:.1f} км, строится "
               f"{ev.infra_m / 1000:.1f} км. Грузы, ед./год: {cargo_txt}. Потоки: {flows_txt}. "
               f"Каждая единица оплачивается по прямой от своей станции погрузки до выгрузки. "
               f"Состав: {ev.n}× {ev.consist.label}, пиковая загрузка участка {ev.peak_load:.0f} ед./год. "
               f"Станции: {ev.station_name} ×{len(ev.stops)}, {ev.depot_name}. "
               f"Доход {_money(ev.income)}/год, прибыль {_money(ev.profit)}/год, вложения {_money(ev.capex)}, "
               f"NPV {_money(ev.npv)}. Отдельные прямые линии: NPV {_money(alt['npv'])}, вложения "
               f"{_money(alt['capex'])} ({len(alt['lines'])} лин.) → выигрыш {_money(gain)}.")
    title = (f"{label} ({MODE_RU.get(ev.mode, ev.mode)}): " + " → ".join(_short(n) for n in names)
             + (" ↺" if ev.topology == "loop" else " ⇄"))
    vehicles = [VehicleLine(v.id, v.name, v.file, k * ev.n, v.price) for v, k in ev.consist.units]
    life = min((v.lifespan_years for v, _ in ev.consist.units), default=30.0) or 30.0
    return ProjectCandidate(
        kind=kind, mode=ev.mode, title=title, details=details, nodes=list(ev.stops), cargo=main,
        volume=ev.volume, income=ev.income, running=ev.running, maintenance=ev.maintenance,
        vehicle_capex=ev.vehicle_capex, infra_capex=ev.infra_capex, vehicles=vehicles,
        vehicle_life_years=life, geometry=_geometry(pl, ev) if geometry else None, legs=ev.legs,
        pair_keys=list(ev.pair_keys), station_keys=list(ev.station_keys),
        uses_supply=dict(alloc.uses_supply), adds_supply=dict(alloc.adds_supply),
        uses_demand=dict(alloc.uses_demand),
        meta={"generator": "tours", "type": label, "tour": list(ev.stops), "visits": list(ev.visits),
              # processing output this line makes from inputs it delivers itself and carries on
              # (not in uses_supply): the planner counts it as shipped from that plant
              "chain_output": _chain_output(ev.flows),
              "topology": ev.topology, "cycle_m": round(ev.cycle_m), "infra_m": round(ev.infra_m),
              "cargos": sorted(by_cargo),
              "flows": [{"from": f.p, "to": f.d, "cargo": f.cargo, "volume": round(f.volume, 3),
                         "income_per_unit": round(_ipu(pl, f.cargo, f.p, f.d))} for f in ev.flows],
              "consist": ev.consist.label, "n_consists": ev.n, "trip_s": round(ev.trip_s),
              "peak_load": round(ev.peak_load, 1), "npv": round(ev.npv),
              "direct": {"npv": round(alt["npv"]), "profit": round(alt["profit"]), "capex": round(alt["capex"]),
                         "income": round(alt["income"]), "volume": round(alt["volume"], 1),
                         "lines": [{"from": e.stops[0], "to": e.stops[1], "mode": e.mode,
                                    "cargo": e.flows[0].cargo, "npv": round(e.npv), "capex": round(e.capex)}
                                   for e in alt["lines"]]},
              "gain_npv": round(gain)})


def _finish(cands: list, max_candidates: int | None) -> list:
    """Dedupe (same mode, stop set, cargos -> best) and sort by NPV gain over direct links."""
    best: dict = {}
    for c in cands:
        k = (c.mode, frozenset(c.nodes), tuple(c.meta["cargos"]))
        if k not in best or c.meta["gain_npv"] > best[k].meta["gain_npv"]:
            best[k] = c
    out = sorted(best.values(), key=lambda c: -c.meta["gain_npv"])
    return out[:max_candidates] if max_candidates else out


def _producer_groups(pl, mode, prods: list[int], consumer: int, demand_cap: float, vols: dict,
                     use_cvrp: bool = True) -> set[frozenset]:
    """Candidate producer sets for one consumer: each producer + its k-1 nearest (k = 2..6),
    plus the routes of a CVRP partition (depot = consumer, <= MAX_STOPS stops, volume <= cap)."""
    groups: set[frozenset] = set()
    if len(prods) < 2:
        return groups
    m = length_matrix(pl, mode, prods)
    for a, p in enumerate(prods):
        near = [prods[b] for b in np.argsort(m[a]) if b != a and np.isfinite(m[a, b])]
        for k in range(2, min(MAX_STOPS, len(near) + 1) + 1):
            groups.add(frozenset([p] + near[:k - 1]))
    ckey = ("cvrp", mode, consumer, tuple(prods), tuple(round(vols[p]) for p in prods), round(demand_cap))
    cache = _cache(pl)
    if use_cvrp and len(prods) >= 3 and ckey in cache:
        groups |= cache[ckey]
    elif use_cvrp and len(prods) >= 3:
        cache[ckey] = set()
        nodes = [consumer] + prods
        dm = length_matrix(pl, mode, nodes)
        np.fill_diagonal(dm, 0.0)
        if np.isfinite(dm).all():
            dem = [0.0] + [vols[p] for p in prods]
            cap = max(demand_cap, max(dem))
            fixed = float(np.median(dm[0, 1:])) * 0.5
            try:
                routes = cvrp(dm, dem, cap, 0, MAX_STOPS, fixed, time_limit=CVRP_TIME_S)
            except Exception:
                routes = cvrp(dm, dem, cap, 0, MAX_STOPS, fixed, method="savings")
            for r in routes:
                if len(r) >= 2:
                    cache[ckey].add(frozenset(nodes[i] for i in r))
            groups |= cache[ckey]
    return groups


def _vkm_ratio(pl, mode: str, stops: list[int], flows: list[Flow]) -> float:
    """Vehicle-km of the line (every trip runs the whole cycle with the total volume) over the
    vehicle-km of separate direct shuttles. Fleets are never cheaper on a shared line, so far
    above 1 only the shared route could pay for it; used as a cheap pre-filter."""
    direct = sum(f.volume * 2 * (leg_length(pl, mode, f.p, f.d) or math.inf) for f in flows)
    cyc = [c for c in (order_stops(pl, mode, stops, "loop"), order_stops(pl, mode, stops, "shuttle")) if c]
    if not cyc or not direct:
        return math.inf
    cycle = min(cyc[0][1], 2 * cyc[-1][1])
    return sum(f.volume for f in flows) * cycle / direct


def milk_runs(planner, year: int, supply: dict, demand: dict, built_keys: set | None = None,
              modes=("road", "rail"), mixed: bool = True, max_candidates: int | None = 60,
              geometry: bool = True) -> list:
    """Collection lines: k = 2..6 producers of one cargo -> their consumer on one shared route.
    With mixed=True also groups that carry a second cargo of the same producers to a nearby
    consumer of that cargo (farms: grain to a livestock farm + vegetables to a town).
    Only lines whose NPV beats the separate direct links for the same flows are returned."""
    parts = milk_run_parts(planner, year, supply, demand, built_keys, modes, mixed)
    return _emit(planner, [x for _k, lst in parts for x in lst], max_candidates, geometry)


def milk_run_cargos(supply: dict) -> list[str]:
    """Cargos milk_run_parts() iterates over (for splitting the work between processes)."""
    return sorted({c for (_, c), v in supply.items() if v >= 1})


def milk_run_parts(planner, year: int, supply: dict, demand: dict, built_keys: set | None = None,
                   modes=("road", "rail"), mixed: bool = True, cargos=None, own=None) -> list:
    """milk_runs() before selection, per (cargo, consumer): [((cargo, consumer #), [(ev, alt, alloc,
    label)])] in milk_runs order. cargos: only these (None = all); own((cargo, consumer node)) ->
    bool: only these parts. Every (cargo, consumer) is independent of the others."""
    pl = planner
    built = set(built_keys or ())
    out = []
    only = set(cargos) if cargos is not None else None
    for c in milk_run_cargos(supply):
        if only is not None and c not in only:
            continue
        prods_all = [n for (n, cc), v in supply.items() if cc == c and v >= 1]
        consumers = [n for (n, cc), v in demand.items() if cc == c and v >= 1]
        for j, d in enumerate(consumers):
            if own is not None and not own((c, d)):
                continue
            cands = []
            out.append(((c, j), cands))
            for mode in modes:
                prods = sorted((p for p in prods_all if p != d and leg_length(pl, mode, p, d) is not None),
                               key=lambda p: leg_length(pl, mode, p, d))
                prods = prods[:NEAREST_PRODUCERS] if NEAREST_PRODUCERS else prods
                vols = {p: supply[(p, c)] for p in prods}
                for g in _producer_groups(pl, mode, prods, d, demand[(d, c)], vols):
                    stops = sorted(g) + [d]
                    alloc = allocate(pl, stops, supply, demand, {c}, chain=False)
                    if len({f.p for f in alloc.flows}) < 2:
                        continue
                    variants = [(stops, alloc, "Сборный маршрут")]
                    if mixed:
                        others = {cc for (n, cc), v in supply.items() if n in g and cc != c and v >= 1}
                        for c2 in others:
                            d2s = [n for (n, cc), v in demand.items() if cc == c2 and v >= 1 and n not in g
                                   and n != d and leg_length(pl, mode, d, n) is not None]
                            d2s.sort(key=lambda n: leg_length(pl, mode, d, n))
                            for d2 in d2s[:SECOND_DROPS]:
                                st2 = stops + [d2]
                                al2 = allocate(pl, st2, supply, demand, {c, c2}, chain=False)
                                if len({f.p for f in al2.flows}) >= 2:
                                    variants.append((st2, al2, "Сборный маршрут (2 груза)"))
                    for st, al, label in variants:
                        if _vkm_ratio(pl, mode, st, al.flows) > MAX_VKM_RATIO:
                            continue
                        ev = best_line(pl, year, mode, st, al.flows, built)
                        if ev is None or ev.profit <= 0:
                            continue
                        alt = direct_alternative(pl, year, al.flows, modes, built)
                        if ev.npv > alt["npv"] + 1.0:
                            cands.append((ev, alt, al, label))
    return out


def _emit(pl, cands, max_candidates, geometry, kind="cargo"):
    sel = select(pl, cands, max_candidates, kind)
    if geometry:
        for c, ev in sel:
            set_geometry(c, line_geometry(pl, ev))
    return [c for c, _ev in sel]


def select(pl, cands, max_candidates, kind="cargo") -> list:
    """_emit without geometry: [(ProjectCandidate, LineEval)] after dedupe / sort / cut."""
    built = [(to_candidate(pl, ev, alt, al, label, kind=kind, geometry=False), ev) for ev, alt, al, label in cands]
    out = _finish([c for c, _ in built], max_candidates)
    evs = {id(c): ev for c, ev in built}
    return [(c, evs[id(c)]) for c in out]


def line_geometry(pl, ev) -> tuple:
    """(geometry, segments, segment_totals) of a selected line (routing; the costly part of _emit).
    Memoized per stop sequence: it depends only on the static routing grid, and the same lines are
    selected again every January (a copy is returned, callers may edit theirs)."""
    import copy
    key = ("geom", ev.mode, ev.topology, tuple(ev.visits), tuple(ev.stops))
    cache = _cache(pl)
    if key not in cache:
        geom = _geometry(pl, ev)
        cache[key] = (geom, getattr(ev, "_segments", None), getattr(ev, "_segment_totals", None))
    return copy.deepcopy(cache[key])


def set_geometry(c, g: tuple):
    c.geometry = g[0]
    c.meta["segments"] = g[1]
    c.meta["segment_totals"] = g[2]


def circuits(planner, year: int, supply: dict, demand: dict, built_keys: set | None = None,
             modes=("road", "rail"), max_candidates: int | None = 60, geometry: bool = True) -> list:
    """Pairs A <-> B and triangles A -> B -> C -> A with at least two loaded legs (backhaul,
    chain legs through processing industries), credited on every loaded leg; universal
    vehicles carry all cargos of the line. Kept when they beat the separate direct links."""
    parts = circuit_parts(planner, year, supply, demand, built_keys, modes)
    return _emit(planner, [x for _k, lst in parts for x in lst], max_candidates, geometry)


def circuit_parts(planner, year: int, supply: dict, demand: dict, built_keys: set | None = None,
                  modes=("road", "rail"), nodes=None, own=None) -> list:
    """circuits() before selection: [((mode #, supply item #), [(ev, alt, alloc, label)])] in
    circuits order. nodes: only loaded legs starting at these producer nodes (None = all); every
    cycle starts at its producer, so splits by producer node are independent."""
    pl = planner
    built = set(built_keys or ())
    only = set(nodes) if nodes is not None else None
    parts = []
    from .economics import conversion
    demand_by_cargo: dict[str, list[int]] = {}
    for (n, c), v in demand.items():
        if v >= 1:
            demand_by_cargo.setdefault(c, []).append(n)
    demand_cargos = set(demand_by_cargo)

    def outputs(n: int, delivered_cargo: str | None) -> set[str]:
        out = {c for (m, c), v in supply.items() if m == n and v >= 1}
        ind = _industry(pl, n)
        if ind is not None and delivered_cargo and delivered_cargo in ind.inputs:
            for o, needs in conversion(ind).items():
                if o in demand_cargos and set(needs) == {delivered_cargo}:
                    out.add(o)
        return out

    seen: set = set()
    for mi, mode in enumerate(modes):
        def near(src: int, cargo: str) -> list[int]:
            ds = [(leg_length(pl, mode, src, n), n) for n in demand_by_cargo.get(cargo, []) if n != src]
            return [n for L, n in sorted((L, n) for L, n in ds if L is not None)][:NEAREST_CONSUMERS]

        for ii, ((a, c), v) in enumerate(list(supply.items())):
            if v < 1:
                continue
            if only is not None and a not in only:
                continue
            if own is not None and not own((mode, a)):
                continue
            cands = []
            parts.append(((mi, ii), cands))
            for b in near(a, c):
                outs = outputs(b, c)
                cycles = [[a, b, e] if e != a else [a, b] for o in outs for e in near(b, o)]
                if any((a, o) in demand for o in outs):
                    cycles.append([a, b])            # pair with backhaul
                for cyc in cycles:
                    key = (mode, tuple(cyc))
                    if key in seen:
                        continue
                    seen.add(key)
                    alloc = allocate(pl, cyc, supply, demand, None, chain=True)
                    ev = None
                    for seq in ([cyc] if len(cyc) == 2 else [cyc, [cyc[0]] + cyc[1:][::-1]]):
                        e1 = evaluate_line(pl, year, mode, seq, alloc.flows, built, "loop")
                        if e1 and sum(1 for l in e1.legs if l["load"] > 0) >= 2 and (ev is None or e1.npv > ev.npv):
                            ev = e1
                    if ev is None or ev.profit <= 0:
                        continue
                    alt = direct_alternative(pl, year, alloc.flows, modes, built)
                    if ev.npv > alt["npv"] + 1.0:
                        cands.append((ev, alt, alloc, "Кольцевой маршрут"))
    return parts


# --------------------------------------------------------------------------- passengers

def stop_order(points: list[tuple[float, float]], closed: bool = True, fixed_start: int | None = None,
               fixed_end: int | None = None, detour: float = 1.3, method: str = "auto") -> list[int]:
    """Order bus/tram stops inside a town (Euclidean distance x detour; closed = circular line)."""
    p = np.asarray(points, dtype=float)
    d = np.sqrt(((p[:, None, :] - p[None, :, :]) ** 2).sum(-1)) * detour
    return solve_tour(d, open=not closed, fixed_start=fixed_start, fixed_end=fixed_end, method=method).order


def town_order(planner, towns: list[int], mode: str = "rail", open: bool = True, method: str = "auto",
               fixed_start: int | None = None, fixed_end: int | None = None) -> tuple[list[int], float]:
    """Order of towns (node indices) for an intercity line / grand tour by route length."""
    m = length_matrix(planner, mode, towns)
    fs = towns.index(fixed_start) if fixed_start is not None else None
    fe = towns.index(fixed_end) if fixed_end is not None else None
    r = solve_tour(m, open=open, fixed_start=fs, fixed_end=fe, method=method)
    return [towns[i] for i in r.order], r.cost


def gravity_od(planner):
    """Default passenger OD (ASSUMPTION until passengers.py provides one): trips/year i -> j =
    PAX_TRIPS_PER_RESIDENT * residents_i * share of j in the distance-decayed town capacity."""
    towns = [n for n in planner.nodes if n.kind == "town"]
    tobj = {t.id: t for t in planner.game.towns}
    res = {n.index: tobj[n.id].capacities.get("residential", 0) for n in towns}
    attr = {n.index: sum(tobj[n.id].capacities.values()) for n in towns}
    nodes = planner.nodes

    def w(i, k):
        a, b = nodes[i], nodes[k]
        return attr[k] * math.exp(-math.dist((a.x, a.y), (b.x, b.y)) / PAX_DECAY_M)

    tot = {i: sum(w(i, k) for k in attr if k != i) or 1.0 for i in attr}

    def od(i: int, j: int) -> float:
        return PAX_TRIPS_PER_RESIDENT * res.get(i, 0) * w(i, j) / tot[i]
    return od


def passenger_lines(planner, year: int, built_keys: set | None = None, od=None, mode: str = "rail",
                    max_stops: int = 5, max_candidates: int | None = 20, geometry: bool = True) -> list:
    """Multi-town passenger lines: each town + its k-1 nearest towns (k = 2..max_stops), ordered
    as the shortest open path (shuttle) or loop; flows = od(i, j) passengers/year for all pairs.
    Returned when profitable (kind "passenger"); meta['direct'] holds point-to-point lines."""
    pl = planner
    built = set(built_keys or ())
    od = od or gravity_od(pl)
    towns = [n.index for n in pl.nodes if n.kind == "town"]
    m = length_matrix(pl, mode, towns)
    groups: set[frozenset] = set()
    for a, t in enumerate(towns):
        near = [towns[b] for b in np.argsort(m[a]) if b != a and np.isfinite(m[a, b])]
        for k in range(2, max_stops + 1):
            groups.add(frozenset([t] + near[:k - 1]))
    cands = []
    for g in groups:
        stops = sorted(g)
        flows = [f for f in (Flow(i, j, "passengers", od(i, j)) for i in stops for j in stops if i != j)
                 if f.volume >= 1]
        if not flows:
            continue
        ev = best_line(pl, year, mode, stops, flows, built)
        if ev is None or ev.profit <= 0:
            continue
        alt = direct_alternative(pl, year, flows, (mode,), built)
        cands.append((ev, alt, Allocation(flows, {}, {}, {}), "Пассажирская линия"))
    out = _emit(pl, cands, None, False, kind="passenger")
    for c in out:
        c.details += " Пассажиропоток — оценка (гравитационная модель), уточняется."
    out.sort(key=lambda c: -(c.profit * annuity() - c.capex))
    out = out[:max_candidates] if max_candidates else out
    if geometry:
        for c in out:
            ev = next(e for e, a, al, lb in cands if e.mode == c.mode and e.stops == c.nodes)
            c.geometry = _geometry(pl, ev)
            c.meta["segments"] = getattr(ev, "_segments", None)
            c.meta["segment_totals"] = getattr(ev, "_segment_totals", None)
    return out


# =========================================================================== benchmark / report

def _bench_methods(n: int) -> list[tuple[str, dict]]:
    tl = 0.2 if n <= 20 else (1.0 if n <= 50 else 2.0)
    ms = [("nn", {}), ("2opt", {}), ("oropt", {}), ("lk", {}), ("chained_lk", {"time_limit": tl})]
    if HAVE_ELKAI:
        ms.append(("lkh", {}))
    if HAVE_ORTOOLS:
        ms.append(("ortools", {"time_limit": tl}))
    if n <= 20:
        ms.append(("held_karp", {}))
    if n <= 8:
        ms.append(("brute", {}))
    return ms


def _run(d, method, kw, open_=False, fs=None, fe=None):
    t = time.perf_counter()
    r = solve_tour(d, open=open_, fixed_start=fs, fixed_end=fe, method=method, **kw)
    return r.cost, time.perf_counter() - t


def _table(title: str, rows: dict, ref_name: str) -> str:
    out = [f"### {title}", "", f"Reference: {ref_name}.", "",
           "| method | mean gap % | max gap % | optimal hits | mean time, s |", "|---|---:|---:|---:|---:|"]
    for m, recs in rows.items():
        gaps = [g for g, _ in recs]
        times = [t for _, t in recs]
        hits = sum(1 for g in gaps if g < 1e-6)
        out.append(f"| {m} | {100 * np.mean(gaps):.2f} | {100 * max(gaps):.2f} | {hits}/{len(gaps)} | {np.mean(times):.3f} |")
    return "\n".join(out) + "\n"


def benchmark(sizes=(8, 12, 20, 50, 100), seeds: dict | None = None, planner=None) -> str:
    """Optimality gap and runtime of every method; returns markdown tables."""
    seeds = seeds or {8: 10, 12: 10, 20: 5, 50: 5, 100: 5}
    parts = [f"Backends: elkai (LKH-3) {'yes' if HAVE_ELKAI else 'no'}, OR-Tools {'yes' if HAVE_ORTOOLS else 'no'}.\n"]
    for n in sizes:
        rows: dict[str, list] = {}
        exact = n <= 20
        for s in range(seeds.get(n, 3)):
            rng = np.random.default_rng(1000 * n + s)
            p = rng.random((n, 2)) * 20000.0           # 20 km map, metres
            d = np.sqrt(((p[:, None] - p[None]) ** 2).sum(-1))
            res = {m: _run(d, m, kw) for m, kw in _bench_methods(n)}
            ref = res["held_karp"][0] if exact else min(c for c, _ in res.values())
            for m, (c, t) in res.items():
                rows.setdefault(m, []).append((c / ref - 1, t))
        parts.append(_table(f"Random Euclidean, n = {n} ({seeds.get(n, 3)} instances, closed tour)",
                            rows, "Held-Karp optimum" if exact else "best tour found by any method (best known)"))
    if planner is not None:
        parts.append(_bench_real(planner))
    return "\n".join(parts)


def _bench_real(pl) -> str:
    parts = []
    farms = [n.index for n in pl.nodes if n.type == "farm"]
    towns = [n.index for n in pl.nodes if n.kind == "town"]
    cases = [("14 farms, road length, closed tour", length_matrix(pl, "road", farms), False, True),
             ("14 farms, road length, open path", length_matrix(pl, "road", farms), True, True),
             (f"{len(towns)} towns grand tour, rail length, closed", length_matrix(pl, "rail", towns), False, False),
             (f"{len(towns)} towns intercity line, rail length, open path", length_matrix(pl, "rail", towns), True, False)]
    for title, d, open_, exact in cases:
        n = len(d)
        rows = {}
        res = {}
        for m, kw in _bench_methods(n if not exact else 20):
            if m == "brute":
                continue
            res[m] = _run(d, m, kw, open_)
        ref = res["held_karp"][0] if exact else min(c for c, _ in res.values())
        for m, (c, t) in res.items():
            rows[m] = [(c / ref - 1, t)]
        parts.append(_table(f"Map: {title} (n = {n}, optimum {ref / 1000:.1f} km)", rows,
                            "Held-Karp optimum" if exact else "best known"))
    # CVRP: grain farms -> each livestock farm, stops <= 6, capacity = consumer demand
    sim = pl.initial_state()
    rows = ["### Map: CVRP partition of grain farms around each livestock farm (road length)", "",
            "| consumer | method | routes | total km | time, s |", "|---|---|---:|---:|---:|"]
    prods = [n for (n, c), v in sim.supply.items() if c == "grain" and v >= 1]
    for d in [n for (n, c), v in sim.demand.items() if c == "grain" and v >= 1]:
        nodes = [d] + prods
        dm = length_matrix(pl, "road", nodes)
        np.fill_diagonal(dm, 0.0)
        dem = [0.0] + [sim.supply[(p, "grain")] for p in prods]
        cap = max(sim.demand[(d, "grain")], max(dem))
        for meth, tl in (("savings", 0), ("ortools", 0.1), ("ortools", 2.0)):
            if meth == "ortools" and not HAVE_ORTOOLS:
                continue
            t = time.perf_counter()
            r = cvrp(dm, dem, cap, 0, MAX_STOPS, 0.0, method=meth, time_limit=tl or 1.0)
            el = time.perf_counter() - t
            lab = meth if meth == "savings" else f"ortools {tl:g}s"
            rows.append(f"| {_short(pl.nodes[d].name)} | {lab} | {len(r)} | {routes_cost(dm, r) / 1000:.1f} | {el:.2f} |")
    parts.append("\n".join(rows) + "\n")
    return "\n".join(parts)


def _report(pl, year: int, supply: dict, demand: dict):
    t = time.perf_counter()
    mr = milk_runs(pl, year, supply, demand, set(), geometry=False, max_candidates=None)
    t1 = time.perf_counter()
    cs = circuits(pl, year, supply, demand, set(), geometry=False, max_candidates=None)
    t2 = time.perf_counter()
    for name, lst, el in (("Сборные маршруты (milk-run)", mr, t1 - t), ("Кольцевые маршруты", cs, t2 - t1)):
        print(f"\n== {name}, {year}: {len(lst)} лучше прямых линий ({el:.1f} с)")
        for c in lst[:10]:
            d = c.meta["direct"]
            print(f"- {c.title}\n    {c.meta['topology']}, цикл {c.meta['cycle_m'] / 1000:.1f} км, "
                  f"{c.meta['n_consists']}× {c.meta['consist']}; объём {c.volume:.0f} ед./год; "
                  f"доход {c.income / 1e6:.2f}, прибыль {c.profit / 1e6:.2f} млн/год, вложения {c.capex / 1e6:.2f} млн, "
                  f"NPV {c.meta['npv'] / 1e6:.2f} млн | прямые: {len(d['lines'])} лин., прибыль {d['profit'] / 1e6:.2f}, "
                  f"вложения {d['capex'] / 1e6:.2f}, NPV {d['npv'] / 1e6:.2f} → выигрыш {c.meta['gain_npv'] / 1e6:+.2f} млн")
    return mr, cs


def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    from .planner import Planner
    if argv and argv[0] == "bench":
        pl = Planner() if "--no-map" not in argv else None
        print(benchmark(planner=pl))
        return
    pl = Planner()
    sim = pl.initial_state()
    _report(pl, pl.game.year, sim.supply, sim.demand)
    towns = [n.index for n in pl.nodes if n.kind == "town"]
    for method in ("lkh" if HAVE_ELKAI else "chained_lk", "chained_lk"):
        t = time.perf_counter()
        order, L = town_order(pl, towns, "rail", open=False, method=method)
        print(f"\nБольшой тур по {len(towns)} городам (ж/д, {method}): {L / 1000:.1f} км, "
              f"{time.perf_counter() - t:.2f} с: " + " → ".join(pl.nodes[i].name for i in order[:8]) + " …")


if __name__ == "__main__":
    main()

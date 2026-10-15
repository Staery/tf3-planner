# Tour optimization (TSP / VRP) — benchmark and recommendation

Module: `tf3planner/tours.py`. Tests: `tests/test_tours.py` (13 tests, `python tests/test_tours.py` or pytest).
Regenerate the tables: `.venv/Scripts/python -X utf8 -m tf3planner.tours bench`; the 1900 report:
`.venv/Scripts/python -X utf8 -m tf3planner.tours`.

## 1. Recommendation (which algorithm where)

| use | size | algorithm | why |
|---|---|---|---|
| milk-run / circuit stop order (`order_stops`) | 2..8 stops (+1 dummy for open paths) | **exact Held–Karp** (`held_karp_small`, pure Python) | exact, ~0.3 ms; thousands of calls per generator run |
| any tour 11..20 nodes | 11..20 | **LKH-3 via elkai** (`method="lkh"`) | exact on every test instance, 1–10 ms; Held–Karp at n=20 takes 13 s |
| grand tours, intercity lines, bus stops | 20..200+ | **LKH-3**; without elkai **own chained LK** (`chained_lk`, 0.2–2 s budget) | both 0.00 % vs best known; LKH 0.01–0.3 s |
| quick estimate inside a hot loop | any | own **LK** single run (`method="lk"`) | 1–1.6 % mean gap at n=50–100, 9–26 ms |
| CVRP (partition producers into lines) | ≤ 20 customers | **OR-Tools CVRP** (savings start + GLS), 0.1 s | equal to or better than Clarke–Wright; 2 s gives at most a further 2.7 % |
| ground truth in tests | ≤ 9 / ≤ 20 | brute force / Held–Karp (numpy) | |

`solve_tour(method="auto")` implements this: exact DP for ≤ 10 nodes (counting the dummy), else LKH if
elkai is installed, else chained LK. Nearest neighbour, plain 2-opt and OR-Tools TSP are **not**
recommended: NN is 4–27 % off, 2-opt alone 2.5–8 %, OR-Tools GLS 3.4 % at n=100 after 2 s.

Both optional backends install fine on Windows / Python 3.13 (`pip install ortools elkai`: ortools
9.15, elkai 2.0.1). The module imports without them and falls back to own LK / Clarke–Wright.

## 2. Algorithms implemented

* `nearest_neighbour`, `two_opt` (first improvement), `or_opt` (segments 1–3, both orientations).
* `lin_kernighan` — own LK: each move is a sequence of flips. Take t2 = succ(t1), then t3 from the
  10 nearest neighbours of t2 with a positive partial gain, t4 = pred(t3), reverse t2..t4. The tour
  stays Hamiltonian after every flip, so the closing gain is exact. Breadth (5, 3, 1), depth ≤ 50,
  edges added during the move are tabu, the best prefix is kept, both tour neighbours of t1 are
  tried, don't-look bits drive the queue. With `kicks` or `time_limit` it becomes **chained LK**:
  double-bridge kicks in a window, LK restarted only from the 8 kick endpoints.
* `held_karp` (numpy, asymmetric, n ≤ ~20), `held_karp_small` (pure Python, fast for n ≤ 10),
  `brute_force` (n ≤ 9).
* `lkh` (elkai/LKH-3, integer-scaled matrix, ATSP supported), `ortools_tsp` (path-cheapest-arc + GLS).
* `cvrp` — OR-Tools with a volume dimension, a stop-count dimension and a fixed cost per route;
  fallback Clarke–Wright parallel savings + exact/LK per route.
* `solve_tour(dist, open=False, fixed_start=None, fixed_end=None, method="auto")`. Open paths and
  fixed ends are reduced to a closed TSP with a dummy node z: arc x→z costs 0 if x may end the path,
  else M; arc z→y costs 0 if y may start it, else M. A closed tour with both ends fixed is solved as
  the path start→end plus the closing arc. Symmetric-only heuristics work on (d+dᵀ)/2, and the
  orientation is then chosen on the real (possibly asymmetric) matrix. `inf` (no link) becomes M.

## 3. Benchmark

Windows 11, Python 3.13, single thread. Gap = tour / reference − 1. Random instances: uniform points
in a 20 × 20 km square (Euclidean, metres). Time budgets: chained LK and OR-Tools 0.2 s (n ≤ 20),
1 s (n = 50), 2 s (n = 100); LKH 10 runs. Exact references up to n = 20 (Held–Karp). For n = 50/100
and the 33-town map the reference is the best tour found by any method. LKH and chained LK agree on
all of these, which makes it very likely optimal.

### Random Euclidean, n = 8 (10 instances, closed tour) — reference: Held–Karp optimum

| method | mean gap % | max gap % | optimal hits | mean time, s |
|---|---:|---:|---:|---:|
| nn | 3.90 | 13.78 | 6/10 | 0.000 |
| 2opt | 0.00 | 0.00 | 10/10 | 0.000 |
| oropt | 0.00 | 0.00 | 10/10 | 0.000 |
| lk | 0.00 | 0.00 | 10/10 | 0.000 |
| chained_lk | 0.00 | 0.00 | 10/10 | 0.200 |
| lkh | 0.00 | 0.00 | 10/10 | 0.001 |
| ortools | 0.00 | 0.00 | 10/10 | 0.203 |
| held_karp | 0.00 | 0.00 | 10/10 | 0.001 |
| brute | 0.00 | 0.00 | 10/10 | 0.009 |

### Random Euclidean, n = 12 (10 instances) — reference: Held–Karp optimum

| method | mean gap % | max gap % | optimal hits | mean time, s |
|---|---:|---:|---:|---:|
| nn | 15.61 | 47.45 | 1/10 | 0.000 |
| 2opt | 1.59 | 5.25 | 5/10 | 0.000 |
| oropt | 0.00 | 0.00 | 10/10 | 0.000 |
| lk | 0.00 | 0.00 | 10/10 | 0.001 |
| chained_lk | 0.00 | 0.00 | 10/10 | 0.201 |
| lkh | 0.00 | 0.00 | 10/10 | 0.001 |
| ortools | 0.00 | 0.00 | 10/10 | 0.203 |
| held_karp | 0.00 | 0.00 | 10/10 | 0.046 |

### Random Euclidean, n = 20 (5 instances) — reference: Held–Karp optimum

| method | mean gap % | max gap % | optimal hits | mean time, s |
|---|---:|---:|---:|---:|
| nn | 8.49 | 14.37 | 0/5 | 0.000 |
| 2opt | 2.54 | 6.17 | 1/5 | 0.000 |
| oropt | 1.48 | 6.17 | 3/5 | 0.001 |
| lk | 0.24 | 1.21 | 4/5 | 0.002 |
| chained_lk | 0.00 | 0.00 | 5/5 | 0.200 |
| lkh | 0.00 | 0.00 | 5/5 | 0.010 |
| ortools | 0.07 | 0.33 | 4/5 | 0.203 |
| held_karp | 0.00 | 0.00 | 5/5 | 13.802 |

### Random Euclidean, n = 50 (5 instances) — reference: best known

| method | mean gap % | max gap % | best-known hits | mean time, s |
|---|---:|---:|---:|---:|
| nn | 24.17 | 34.74 | 0/5 | 0.000 |
| 2opt | 6.41 | 11.31 | 0/5 | 0.002 |
| oropt | 2.13 | 6.24 | 2/5 | 0.014 |
| lk | 1.21 | 3.41 | 2/5 | 0.009 |
| chained_lk | 0.00 | 0.00 | 5/5 | 1.001 |
| lkh | 0.00 | 0.00 | 5/5 | 0.080 |
| ortools | 1.12 | 3.41 | 1/5 | 1.003 |

### Random Euclidean, n = 100 (5 instances) — reference: best known

| method | mean gap % | max gap % | best-known hits | mean time, s |
|---|---:|---:|---:|---:|
| nn | 26.74 | 31.64 | 0/5 | 0.001 |
| 2opt | 8.05 | 10.83 | 0/5 | 0.006 |
| oropt | 3.22 | 6.08 | 0/5 | 0.072 |
| lk | 1.56 | 3.54 | 0/5 | 0.026 |
| chained_lk | 0.00 | 0.00 | 5/5 | 2.002 |
| lkh | 0.00 | 0.00 | 5/5 | 0.320 |
| ortools | 3.43 | 6.02 | 0/5 | 2.003 |

### Real instances from this map (one instance each; gap %, time s)

| method | 14 farms, road, closed (opt 125.8 km, HK) | 14 farms, road, open path (opt 109.1 km, HK) | 33 towns grand tour, rail, closed (171.2 km, best known) | 33 towns intercity line, rail, open (160.8 km, best known) |
|---|---|---|---|---|
| nn | 0.00 / 0.000 | 9.61 / 0.000 | 23.06 / 0.000 | 38.67 / 0.000 |
| 2opt | 0.00 / 0.000 | 0.00 / 0.000 | 2.75 / 0.001 | 5.15 / 0.000 |
| oropt | 0.00 / 0.000 | 0.00 / 0.001 | 1.91 / 0.003 | 1.24 / 0.005 |
| lk | 0.00 / 0.001 | 0.58 / 0.001 | 1.02 / 0.002 | 0.15 / 0.003 |
| chained_lk | 0.00 / 0.200 | 0.00 / 0.200 | 0.00 / 1.000 | 0.00 / 1.000 |
| lkh | 0.00 / 0.001 | 0.00 / 0.004 | 0.00 / 0.012 | 0.00 / 0.019 |
| ortools | 0.00 / 0.201 | 0.00 / 0.203 | 0.00 / 1.004 | 0.00 / 1.002 |
| held_karp | 0.00 / 0.217 | 0.00 / 0.417 | — | — |

### CVRP: grain farms partitioned around each livestock farm (road length, ≤ 6 stops, capacity = consumer demand)

| consumer (depot) | savings + exact | OR-Tools 0.1 s | OR-Tools 2 s |
|---|---|---|---|
| Вольногорск: Животноводческая ферма | 10 routes, 317.3 km | 10, 310.2 km | 10, 310.2 km |
| Акимовка: Животноводческая ферма | 4, 197.4 km | 4, 197.4 km | 4, 197.4 km |
| Рассказово: Животноводческая ферма: Север | 10, 318.6 km | 10, 318.6 km | 10, 318.6 km |
| Рассказово: Животноводческая ферма | 9, 369.5 km | 9, 369.5 km | 9, 363.6 km |
| Тетиев: Животноводческая ферма | 4, 180.3 km | 4, 180.3 km | 4, 175.4 km |

Savings takes < 5 ms. OR-Tools uses its whole time budget.

Notes:
* Every method except NN is optimal at n = 8. From n = 12, 2-opt starts to fail (1.6 % mean, 5 % max).
  From n = 20, plain LK and Or-opt fail occasionally. Kicks (chained LK) or LKH are needed above that.
* Own single-run LK beats Or-opt at a similar speed and costs about 1/3 of OR-Tools' gap at n = 100.
  Chained LK matches LKH, but only with a time budget roughly 6–100 × larger.
* The road/rail matrices of this map are symmetric (|d − dᵀ| < 1e-9 m for length/build; rail time
  differs by ≤ 37 s), so the symmetric heuristics lose nothing here.
* CVRP: savings and OR-Tools usually give the same partition. The partition matters less than the
  economic evaluation of each route (section 4): most CVRP routes do not beat direct links.

## 4. Economics: what tours are worth in TF3 (1900 map)

Line model (`evaluate_line`): stops in a cycle. Cargo is loaded at producer stops and unloaded at any
stop with demand (mechanics.md 1.1). Each unit pays the straight chord from its own pickup to its
drop-off (1.3). Cost: the distinct legs of one shared route, one station per stop, one depot, and a
fleet sized to the **peak leg load** and the **full cycle**. The formulas are the same as
`economics.evaluate_link` (including `capacity.loading_time_s`), so a 2-stop line reproduces a direct
link exactly (tested: road and rail, identical fleet, capex, running, maintenance).

There are two topologies, and both are evaluated (the loop in both directions):
* **loop**: P1 → P2 → C → P1, closed TSP order, all legs are built;
* **shuttle**: open path P1 – P2 – C travelled there and back (shortest Hamiltonian path), so only
  the path is built.

The comparison is the best set of separate direct links for the same flows: each flow in its best
mode, built in NPV order, sharing stations and depots at common nodes, and loss-making links left
out. A candidate is kept only if its NPV (15 years, 5 %) is higher.

**Key finding.** On a shared line every trip runs the whole cycle with the total volume, so its
vehicle-km is Σvol × cycle ≥ Σ vol_i × 2·L_i of the direct links. A milk-run never saves vehicles. It
pays only through the shared route and stations, so it works when producers lie *on the way* to the
consumer, or when one route serves two cargo networks. Income is identical by construction: the
chord is paid, not the detour.

Results with initial supply and demand (`python -m tf3planner.tours`; later years use the same
supply with that year's vehicles):

| year | milk-runs beating direct | of them single-cargo | circuits beating direct |
|---|---:|---:|---:|
| 1900 | 5 | 1 | 21 |
| 1925 | 45 | 16 | 31 |
| 1950 | 94 | 44 | 44 |

Later vehicles are larger and cheaper per unit of capacity, so the shared route becomes the dominant
cost and milk-runs win much more often.

Best 1900 candidates (money in millions; NPV over 15 years at 5 %):

| line | mode | fleet | volume/y | profit/y | capex | NPV | direct NPV (lines) | gain |
|---|---|---|---:|---:|---:|---:|---:|---:|
| Clay pit Нижняя Тура → brickworks Дятьково → town Нижняя Тура ↺ (clay + bricks) | road | 29× DMG Cannstatt | 139 | 3.35 | 7.27 | 27.47 | 18.83 (2) | **+8.63** |
| Clay pit Нижняя Тура → brickworks Бердичев → town Нижняя Тура ↺ | road | 25× DMG Cannstatt | 139 | 2.63 | 6.17 | 21.09 | 14.47 (2) | +6.62 |
| Forest Городок → sawmill Славутич → town Бахчисарай ↺ (logs + planks) | rail | 3× Т тип 1 + 11 wagons | 124 | 5.90 | 19.17 | 42.09 | 36.05 (2) | +6.04 |
| Clay pit Электросталь → brickworks Дятьково → Нижняя Тура ↺ | road | 40× DMG Cannstatt | 126 | 3.48 | 10.10 | 26.06 | 21.03 (2) | +5.02 |
| Forest Кумертау → sawmill Славутич → Бахчисарай ↺ | rail | 3× D 1/3 + 7 wagons | 80 | 3.24 | 12.83 | 20.80 | 15.90 (2) | +4.91 |
| Milk-run (2 cargos): farms Нижняя Тура, Бердичев, Кумертау + town Буинск (vegetables) → livestock Вольногорск (grain) ⇄ | rail | 3× Т тип 1 + 11 wagons | 141 | 3.18 | 17.74 | 15.28 | 11.69 (2) | +3.59 |
| Milk-run (2 cargos): farms Бердичев, Нижняя Тура → town Бердичев (vegetables) + livestock Вольногорск (grain) ⇄ | road | 53× DMG Cannstatt | 103 | 2.68 | 11.52 | 16.34 | 12.90 (4) | +3.44 |
| Milk-run (grain): farms Бердичев, Нижняя Тура → livestock Акимовка ⇄ | road | 73× DMG Cannstatt | 62 | 3.44 | 15.66 | 20.05 | 18.85 (2) | +1.20 |

The circuit pattern that wins everywhere is "**raw material → processor → town near the raw
material → back**". The return leg is short, and both loaded legs share one fleet of universal trucks
or wagons. For example, clay out and bricks back to the town next to the clay pit cuts capex by 30 %
and raises profit by 19 %.

Grand tour of all 33 towns (rail length): 171.2 km. LKH needs 0.01 s, chained LK 1 s (same tour).

## 5. API (for planner integration)

```python
from tf3planner import tours
cands = tours.milk_runs(planner, year, sim.supply, sim.demand, built_keys)   # list[ProjectCandidate]
cands += tours.circuits(planner, year, sim.supply, sim.demand, built_keys)
pax = tours.passenger_lines(planner, year, built_keys, od=my_od, mode="rail")   # kind "passenger"
order, length = tours.town_order(planner, town_nodes, "rail", open=True)
stops = tours.stop_order([(x, y), ...], closed=True)                            # bus/tram stops
r = tours.solve_tour(dist, open=False, fixed_start=None, fixed_end=None)        # TourResult(order, cost, ...)
routes = tours.cvrp(dist, demand, capacity, depot=0, max_stops=6)
```

* `built_keys`: one set that may hold pair keys `(i, j, mode)` and station keys `(node, mode)`, e.g.
  `set(sim.built_pairs) | set(sim.stations)`. Built legs cost nothing to build or maintain, built
  stations are free, and the depot is free if any stop already has a station of that mode (same rule
  as `planner._candidate`).
* Candidates: `kind="cargo"`, `cargo` = the cargo with the largest income, `nodes` = stops in
  visiting order, `legs` = per-leg `{from, to, length_m, load}`, `pair_keys` for all legs,
  `station_keys` for all stops, `uses_supply` / `uses_demand` per (node, cargo), and `adds_supply`
  for processor outputs that the line does not carry itself (grain to a livestock farm → meat).
  `meta`: `tour`, `visits`, `topology`, `flows` (with income per unit), `consist`, `n_consists`,
  `trip_s`, `peak_load`, `npv`, `direct` (the alternative, line by line), `gain_npv`. Titles and
  details are in Russian.
* Runtime on this map: `milk_runs` takes about 12–15 s on the first call (≈ 6 000 stop groups × 2
  modes × 3 topologies). Repeat calls take **0.5 s**, because stop orders, line evaluations, direct
  links and CVRP partitions are memoized on the planner object (`planner._tours_cache`). `circuits`
  takes 0.3–0.6 s. Tuning constants: `MAX_STOPS`, `NEAREST_PRODUCERS` (None = all; restricting to
  the nearest 8 lost 2 of 5 winners, because far producers pay more per unit), `SECOND_DROPS`,
  `MAX_VKM_RATIO`, `CVRP_TIME_S`.

## 6. Limitations / assumptions

* A line is assumed to run as a single shared route with no junction branches. A Steiner tree
  (spurs to a trunk) would cost less infrastructure than the tour/path for 3+ producers; the
  shuttle topology approximates it. `pair_keys` are tour legs, so overlap with other projects'
  routes is only recognised for identical node pairs, as in the planner.
* Rail consists use one wagon type that accepts every cargo of the line (universal wagons). Mixed
  wagon types are not modelled.
* Line capacity (single track, road congestion) is not checked here; `capacity.py` can post-process.
* `passenger_lines` uses a placeholder gravity OD (`PAX_TRIPS_PER_RESIDENT = 2`, 15 km decay;
  **ASSUMPTION**) unless `od(i, j)` is passed. It is meant to be driven by `passengers.py`.
* Water milk-runs are supported (`modes=("water",)`), but none are feasible on this map: the
  refineries are not reachable by water.

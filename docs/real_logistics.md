# Real-world logistics network design -> algorithms for the TF3 planner

Status: research note, 2026-10-01. Scope: how real freight/passenger networks are designed and
optimized, translated into algorithms that fit `tf3planner` (terrain routing on a 64 m grid,
~81-node cost matrices, water network, vehicle catalogue by year, production chains, per-leg
straight-line income, loans, greedy monthly cash simulation).

Items tagged **[VERIFY]** depend on game mechanics that must be confirmed in TF3
(see `docs/mechanics.md`, being written in parallel; it did not exist when this note was written).
Sources are listed at the end; inline references look like [S12].

---

## 0. Executive summary: what transfers from reality and what does not

Real logistics works to **minimize the cost of meeting fixed demand**. TF3 is a different
problem: you **maximize profit (and growth of capital) over supply that is limited and depends
on your network**. Revenue grows linearly with straight-line distance *per leg*. Five
consequences shape everything below:

1. **Distance is revenue, not cost.** Income is `3.8 $/unit/m x straight-line distance` per leg
   (TF2 community analysis: "you get paid the straight-line distance between where cargo gets on
   and gets off a vehicle ... you get paid for each segment of the trip" [S30][S31]). The
   transportation problem's objective flips: send each unit to the *farthest profitable*
   consumer, not the nearest. Detours via hubs do not lose revenue. Because of the triangle
   inequality `d(A,H) + d(H,B) >= d(A,B)`, they gain revenue, at the price of extra vehicle time
   and handling. **[VERIFY V1: per-leg payment, any speed or time factor in TF3]**
2. **Capital is the binding constraint, not demand.** Early on, marginal projects return about
   18-45 % a year (model sweep, section 8.2). The correct discount rate for ranking is the
   *shadow price of capital* (the return of the best rejected project), not a fixed 5 %.
3. **Economies of density are weak in the game's cost model.** In `economics.py`, profit scales
   almost linearly with volume for both modes, so fixed costs (track, stations) are the only
   scale effect. The real-world advantages of rail (cheap per-ton-km at high density, capacity)
   only appear once **capacity limits** (road lanes, truck bays, platforms) are modelled.
   Without them the model picks 150-2300 trucks for one link, which is not operable.
4. **Vehicles become obsolete before they wear out.** Catalogue lifespans are 50-100 years, but a
   better model appears every 10-30 years. Replacement is a *challenger-vs-defender
   obsolescence* decision, not wear-out. **[VERIFY V6: age-dependent running cost/reliability]**
5. **Shared infrastructure is the main lever for consolidation.** Real "hub-and-spoke" value comes
   from sharing vehicles (bigger aircraft and trains). In the game it comes mostly from sharing
   track and stations (fixed charge) and from **backhaul** (a loaded return halves vehicle
   capital per unit).

---

## 1. Network topologies

### 1.1 Definitions and the route-count argument

| Topology | Links for n nodes | Where it wins | Weakness |
|---|---|---|---|
| Point-to-point (direct) | up to n(n-1)/2 | big flows per OD pair; time-critical; short haul | fixed cost duplicated; low load factor on thin pairs |
| Hub-and-spoke | n-1 (single hub) | many thin OD flows; costly vehicles or terminals; need for frequency | detour, transfer time and cost, hub congestion, single point of failure |
| Trunk-and-feeder (corridor) | trunk + short feeders | sources spread along an axis; high trunk density | transfer handling; trunk capacity becomes the bottleneck |
| Multi-hub / hybrid | hubs + selected direct links | mixed density | complex to plan |

Hub-and-spoke cuts the number of routes from n(n-1)/2 to n-1 [S1]. Delta pioneered it at Atlanta
in 1955. FedEx runs its overnight parcel network through the Memphis "SuperHub", and UPS through
Louisville Worldport. Container lines use feeder ships into hub ports, and freight rail uses
classification yards to sort cars [S1].

### 1.2 Economics: scale vs density vs scope

- **Economies of density**: unit cost falls as traffic on a *fixed* network grows. They are strong
  in airlines and railways. **Economies of scale** (growing the network) are close to constant
  returns: "the railroad industry has effectively exhausted the possible economies of scale but
  can still gain from economies of density and scope" [S2][S3].
- Hub-and-spoke works because it **concentrates flows to create density** (bigger vehicles, fuller
  loads, higher frequency) [S2].
- **Break-even distance** between modes comes from cost structure. Road has low terminal cost and
  high line-haul cost; rail and sea have high terminal cost and low line-haul cost. The real
  road-to-rail break-even is "generally between 500 and 750 km", and rail-to-sea is near 1,500 km
  [S4]. Values quoted for intermodal range from about 300 km (EU target) to 1,050-1,200 km
  [S4][S5]. **Do not port these numbers.** The TF3 map is about 27 x 27 km and the game's cost
  structure is different. Compute the game-specific break-even instead (section 8.2).

### 1.3 Real examples worth copying in spirit

- **Freight corridors**: EU Regulation 913/2010 set up 9 Rail Freight Corridors (now more), which
  are trunk axes with coordinated capacity [S6]. India's Dedicated Freight Corridors (Eastern
  1,337 km, Western 1,506 km) separate heavy freight from passenger traffic on a trunk [S7].
  Lesson: **one high-capacity trunk + feeders** beats many parallel low-grade lines once volume
  is concentrated on an axis.
- **Intermodal terminals and classification yards**: consolidation points where flows switch
  vehicle. Yard dwell is about 18-24 h in large US yards [S8], so transfers are expensive in time.
- **Airline banks**: hubs schedule arrival waves within a 30-60 min window and then departures, to
  maximize connections. A large hub runs 6-8 banks a day [S9]. Game analogue: timed transfers for
  passengers (section 4.3).
- **Container feeders / triangulation**: about 20 % of container moves are empty. Triangular
  routing (pick up near the drop-off point) cuts empties [S10].

### 1.4 When each wins (rule set usable by the planner)

```
Let F = fixed cost of a dedicated link, c_v = variable cost per unit-km, V_od = OD volume,
    h = hub, delta = detour factor (d(o,h)+d(h,d))/d(o,d), t_x = transfer cost per unit.

Direct beats via-hub for OD pair (o,d) when:
    F_od + c_v(V_od) * d_od * V_od   <   V_od * [ c_v(V_hub) * delta * d_od + t_x ] + dF_shared
where c_v(V_hub) < c_v(V_od) only if the shared trunk gets bigger vehicles / fuller loads.

In TF3 add the revenue side: via-hub earns p*(d(o,h)+d(h,d)) instead of p*d(o,d), i.e. +p*(delta-1)*d_od per unit.
```

Practical game reading:

- **Point-to-point** for big single flows (a mine feeding one steel mill) and anything under a
  few km.
- **Trunk-and-feeder** when 3+ producers (or consumers) sit within feeder range of a common axis
  and their combined volume fills trains that no single producer could.
- **Hub** where many cargo types meet anyway: processing plants (their inputs converge), big towns
  (final goods diverge), and ports (water/land interface).

---

## 2. Optimization models (formulation, solver, feasibility at our size)

Common notation: nodes `N` (|N| ~ 81 + candidate junctions/hubs), modes `m in {road, rail,
water}`, cargos `k in K` (|K| ~ 37 including passengers), service legs `a = (i,j,m)` (vehicles load
at i and unload at j), infrastructure edges `e` (track/road segments), periods `t`.

### 2.1 Transportation / assignment problem (baseline, LP)

Classic: ship from supplies `s_i` to demands `d_j` at least cost [S11]. Game form (cargo is
**fungible within a type**: any coal mine can feed any coal consumer):

```
max  sum_{i,j} (p * dhat_ij - c_ij^k) * x_ij^k
s.t. sum_j x_ij^k <= S_i^k           (producer i output of cargo k)
     sum_i x_ij^k <= D_j^k           (consumer j max intake)
     x >= 0
dhat_ij = straight 3D distance + CLIMB_WEIGHT * max(dz,0)   (as in model.income_per_unit)
c_ij^k  = per-unit cost of the best vehicle/mode on route i->j (annuitized capital + running)
```

- Solver: network simplex / min-cost flow (OR-Tools `SimpleMinCostFlow` [S12]) or any LP (HiGHS
  [S13]). Size: 48 x 81 x 37 is tiny (milliseconds).
- Use: **commodity-to-consumer matching** for one cargo type. It replaces the
  per-candidate greedy pairing, which can block a better global assignment (for example, mine A
  taking the only steel mill that mine B needed).

### 2.2 Min-cost (max-profit) multicommodity flow with processing (generalized flow)

This adds production chains. Processing node `j` runs activity level `w_j` (Leontief recipe:
inputs `a_jk`, outputs `b_jo`):

```
max  sum_{a,k} (p*dhat_a - c_a^k) x_a^k  -  sum_{n,k} h_n^k * xfer_n^k        (h = transfer handling cost)
s.t. out_i^k - in_i^k <= S_i^k                                   primary producer i
     in_j^k  - out_j^k  = a_jk * w_j   (+ final consumption)       processing input
     out_j^o - in_j^o  <= b_jo * w_j                               processing output
     w_j <= Wmax_j                                                 plant capacity
     in_t^k - out_t^k <= D_t^k                                     town demand  [VERIFY V5]
     sum_k x_a^k * u_k <= Cap_a                                    leg capacity (optional)
     x, w >= 0
```

- With alternative recipes (an industry accepting one of several inputs), add one activity per
  recipe.
- Solver: LP (HiGHS). Size: ~1-3k legs x 37 cargos, pruned to cargos that can actually move on
  each leg, gives 10-50k variables. Seconds.
- **The dual prices `pi_n^k` give the marginal value of one unit of cargo k at node n.** That is
  the key output. Any new candidate leg `(i,j)` has reduced profit
  `p*dhat_ij - c_ij - pi_i^k + pi_j^k`. Positive means worth adding. This gives chain-aware
  candidate ranking almost for free. It is the core of column generation for network design.
- Production feedback is not linear (TF industries grow when well served) **[VERIFY V4]**. Handle
  it by iterating LP -> update `S_i` -> LP.

### 2.3 Fixed-charge network design (FCND), uncapacitated and capacitated

You pay a fixed cost `F_e` to open an edge and then route flow on open edges [S14][S15]:

```
max  sum_{a,k} r_a^k x_a^k  -  sum_e F_e^ann * y_e  -  sum_{n,m} S_nm^ann * z_nm
s.t. flow conservation / processing as in 2.2
     x_a^k <= M_a^k * y_e          for every edge e on the fixed route path P_a of leg a   (uncapacitated)
     sum_{a: e in P_a} trains_a(x) <= Cap_e(y_e, q_e)                                    (capacitated)
     x_a^k <= M * z_{i(a),m}, x_a^k <= M * z_{j(a),m}                                    (station at both ends)
     y, z, q in {0,1}
```

- `F_e^ann` is the annuitized build cost, using the **shadow cost of capital** (section 6), not 5 %.
- `q_e` is an optional upgrade binary (double track, electrification, faster track) with higher
  capacity and cost.
- Big-M linking is weak. Use the **strong form** `x_a^k <= min(S_k, D_k) * y_e` per commodity, which
  is the standard strengthening [S14].
- Solver options:
  - MILP with **HiGHS** (open source, MIT, Python `highspy`) [S13] or OR-Tools (CP-SAT / SCIP
    backend). Feasibility: ~400-800 binary edges + ~80 x 3 station binaries + ~50k continuous.
    Typically minutes with a warm start (the greedy plan), with a gap of a few percent. Use a time
    limit.
  - **Benders decomposition** (master: binaries; subproblem: LP flow) is the standard exact
    acceleration [S16][S17].
  - **Lagrangian relaxation** of the linking constraints gives knapsack-like subproblems
    [S14][S15].
  - Heuristics: **dynamic slope scaling** (linearize `F_e` as `F_e / flow_e` and iterate LPs;
    within 0-0.65 % of optimal in tests) [S18]; **add/drop/swap local search**; **ALNS** (adaptive
    large neighbourhood search), widely used for service network design [S19].
- **Recommended**: slope scaling for a fast good solution, then a HiGHS MILP warm-started from it,
  with a time cap of 60-300 s.

### 2.4 Service network design (SND): infrastructure, services, flows

Crainic's framework [S20] separates three layers. This is exactly the TF3 structure:

1. **Infrastructure**: track/road/stations (built once, sunk).
2. **Services** (game "lines"): a cyclic stop sequence, vehicle type, and frequency (vehicles on
   the line).
3. **Flows**: cargo paths over services, possibly with transfers.

Key SND constraints:

```
design-balance (vehicles circulate):  sum_{a in out(n)} f_a = sum_{a in in(n)} f_a     per vehicle class
capacity:                             sum_k u_k x_a^k <= cap_v * LF_max * f_a
fleet:                                n_v = sum_lines  cycle_time_line * f_line  /  Y           (Y = seconds per game year)
```

- `f_a` is the number of vehicle passes per year on leg `a`.
- The **design-balance constraint is what makes backhaul and empty repositioning explicit.** An
  empty leg is just `f_a > 0` with `x_a = 0`. The model then sees that a loaded return costs
  almost nothing.
- Solver: same as 2.3 (MILP or ALNS). Size is fine if lines are enumerated as candidate columns
  (see 8.4).

### 2.5 Hub location: p-hub median and UMAHLP

- **p-hub median** (O'Kelly 1987, the first formulation; Campbell 1994 gave MILPs [S21][S22]):
  pick p hubs and allocate every OD flow through 1-2 hubs, with inter-hub cost discounted by
  `alpha < 1` (economies of scale on hub-hub links) [S23].
- **Uncapacitated multiple-allocation hub location (UMAHLP)**: no fixed p. Each hub has an opening
  cost and a node may use different hubs for different ODs [S23]. Survey: Alumur and Kara 2008
  [S23]; Campbell and O'Kelly 2012.

```
Path-based UMAHLP (Hamacher/Ernst-Krishnamoorthy style):
min  sum_k F_k h_k + sum_{(o,d)} sum_{k,l} W_od * (chi*c_ok + alpha*c_kl + delta*c_ld) * X_odkl
s.t. sum_{k,l} X_odkl = 1                    for every OD
     X_odkl <= h_k, X_odkl <= h_l
     h in {0,1}, X >= 0
(chi = collection, alpha = transfer/trunk, delta = distribution discount factors)
```

- Size: with 81 nodes, `X_odkl` has 81^4 = 43M entries. That is too big directly. Restrict hubs to
  ~10-15 candidates and ODs to real cargo flows (a few hundred), which gives about 500 x 15^2 =
  110k variables. Solvable.
- Better for TF3: there are no fixed ODs because cargo is fungible. Embed hubs as **transfer nodes
  in the flow model 2.2/2.3** (z_hub binary, transfer handling cost) instead of solving a separate
  HLP.
- Heuristic alternative: greedy add/drop of hubs with re-solving the flow LP (like UFLP
  add/drop/swap [S24]).
- In-game twist: `alpha` is not a vehicle-size discount but the ratio of the trunk mode's per-unit
  cost to the feeder's. Also, each leg is paid separately (revenue rises with detour).

### 2.6 Steiner trees for shared infrastructure

Connecting terminals with the cheapest tree that may use extra junction points (Steiner points) is
NP-hard. **Takahashi-Matsuyama** (a Prim-like heuristic: repeatedly attach the nearest terminal by
shortest path to the current tree) is a 2-approximation [S25][S26]. Pairwise heuristics miss
central junctions that unite clusters [S26]. Fix this by adding candidate junction nodes
(cluster centroids, terrain saddle points, Fermat points of triangles).

Game use:
- Build trunk lines that several links share. `routing.build_network(... mark_built ...)` already
  reuses built cells. Make **incremental connection cost** (the cost to connect to *existing*
  network) the first-class metric in candidate evaluation.
- Steiner tree minimizes build cost only. Weight edges by `build + annuitized maintenance +
  capacity-upgrade risk`, and stop merging when a shared segment would exceed its capacity
  (single track) or force too much detour (`delta > 1.3`).

### 2.7 Facility location (stations, depots, yards, warehouses)

- **UFLP**: open facilities with fixed cost and assign customers at least cost. Lagrangian
  relaxation and drop-add-swap heuristics get within about 1 % [S24].
- Game use: (a) **rail station location** near a cluster of industries reached by short truck
  feeders (catchment); (b) **depot placement**: minimize empty runs to/from the depot (put it on
  the line, near the busiest end); (c) shared **town stations** for multiple goods.
- Size: 81 customers x ~200 candidate sites gives 16k variables. Trivial for HiGHS.

### 2.8 Multi-period network / capacity expansion with budget

Multi-period network design adds monotone build variables `y_e,t >= y_e,t-1` and per-period flows.
Example: Barmann, Martin and Schuelldorf (2017) decompose multi-period railway expansion with
annual budgets for the German network [S27][S28].

```
max  sum_t beta^t [ sum_{a,k} r_{a,t}^k x_{a,t}^k  - sum_e Mnt_e y_{e,t} ]  -  sum_t beta^t sum_e B_e (y_{e,t} - y_{e,t-1})
s.t. y_{e,t} >= y_{e,t-1}                                   (infrastructure persists)
     flows_t feasible on {e : y_{e,t}=1}, production_t, tech_t (vehicle catalogue year t)
     cash_t = cash_{t-1} + profit_t - capex_t + borrow_t - repay_t - interest_t >= buffer
     borrow_t <= offers_t ; debt dynamics
```

- **Self-financing** (cash balance) is the game's defining constraint. It is linear, so it stays a
  MILP.
- Size: 26 periods of 5 years x (800 binaries + 50k continuous) gives about 20k binaries and 1.3M
  continuous. Too large monolithically. Options:
  - **Rolling horizon**: solve 4 periods (20 years) ahead, fix period 1, roll [S29]. 26 solves x
    about 1 min each.
  - **Era snapshots + scheduling**: solve FCND for target years (1900/1925/1950/1975/2000/2025),
    then schedule the build order with a multi-period knapsack (2.9) respecting cash.
  - Fine-grained timing (months) stays in the existing greedy cash simulation, now **guided** by the
    target network.

### 2.9 Capital rationing (multi-period knapsack)

Weingartner (1963/1966): with interdependent projects and per-period budgets, solve a 0-1
multi-knapsack. Simple ranking lists are inferior to it [S32][S33].

```
max  sum_p NPV_p * s_p
s.t. sum_p capex_{p,t} * s_p <= budget_t + sum_p cf_{p,t'<t} s_p   (self-financing, cumulative)
     s_q <= s_p  for prerequisite p of q (station before line, chain input before output)
     s_p + s_q <= 1 for mutually exclusive alternatives (road vs rail on same pair)
     s in {0,1}
```

- Size: ~2-5k candidate projects x 130 yearly budget rows. HiGHS handles it in seconds to minutes.
- Use it to **replace the "pick the best NPV/capital greedily" step**, at least as a lookahead
  (12-36 months) inside the monthly simulation.

### 2.10 Real options and staged investment

Treat **defer, expand, abandon, switch** as options. Classic NPV is "now or never". Staged and
modular design keeps the option to expand when information arrives [S34][S35]. McDonald and Siegel
showed that under uncertainty it can be optimal to wait until benefits are about **2x** the
investment cost [S36]. For deterministic but growing benefits, invest in the first year in which
the **first-year rate of return** `B_{t+1} / C >= r` [S36].

Game use:
- **Build single track with loop-ready alignment.** Double later when train count passes the
  threshold (section 3.1).
- **Buy rolling stock incrementally.** Fleet is modular, so start with the minimum frequency.
- **Delay electrification and fast track** until the first-year-return rule fires (section 6.4).
- **Station sizes**: build small and expand (if TF3 allows in-place expansion) **[VERIFY V7]**.

### 2.11 Problem-size feasibility summary (~81 nodes, ~37 cargos, 130 years)

| Model | Variables (after pruning) | Method | Expected time |
|---|---|---|---|
| Transportation per cargo | < 5k | OR-Tools min-cost flow | ms |
| Max-profit generalized flow (2.2) | 10-50k | HiGHS LP | < 5 s |
| FCND static per era (2.3/2.4) | 1-2k bin + 50k cont | slope scaling + HiGHS MILP warm start | 1-5 min |
| UMAHLP restricted | ~100k | HiGHS / greedy add-drop | s-min |
| Steiner (Takahashi-Matsuyama) | graph of 81-130 terminals | heuristic on cost matrix / grid | < 1 s per tree on matrix |
| Multi-period monolithic | ~20k bin, 1M+ cont | not recommended | hours+ |
| Rolling horizon (4 x 5 y) | ~3k bin, 200k cont per solve | HiGHS, time cap | 26 x 1-2 min |
| Multi-knapsack scheduling | 2-5k bin, 130 rows | HiGHS | s-min |

---

## 3. Rail operations rules

### 3.1 Line capacity

- **Headway-based capacity** (UIC 405 style): `Capacity = T / (h + buffer + supplement)`, where
  `buffer = 0.67 h` (daily) or `0.33 h` (peak) and `supplement = 0.25 min x number of sections`
  [S37].
- **UIC 406** (compression method) recommended **maximum occupancy**: mixed traffic **75 % peak /
  60 % daily**; suburban passenger **85 % / 70 %**; high-speed **75 % / 60 %** [S38][S39]. Above
  this, delays propagate (queueing).
- **Single vs double track**: "a single-track line that takes 15 minutes to travel through would
  have capacity for only two trains per hour in each direction ... a double track with signal
  boxes four minutes apart can allow up to 15 trains per hour in each direction" [S40]. Capacity
  of single track is set by passing-loop spacing [S40]. Practical daily figures: 80-100 trains/day
  single track vs 300-400 double track (Japanese practice) [S41].

Formulas for the planner (all times in game seconds; `Y` = seconds per game year, 1461 s in the
export):

```
Double track, one direction, signal block length B (m), train length L, speed v (m/s):
    h_min ~= (B_max + L) / v + t_clear            (t_clear ~ signal/acceleration margin, calibrate)
    trains_per_year_dir = U_max * Y / h_min        (U_max = 0.6 .. 0.75 per UIC 406)

Single track with passing loops; t_s = running time of the longest loop-to-loop section,
c = loop entry/exit + switching time:
    pair_cycle = 2 * (t_s + c)                     (one train each way through the section)
    trains_per_year_each_dir = U_max * Y / pair_cycle
    -> halve the longest section to roughly double capacity; capacity is set by the WORST section.

Loop length >= longest train length + margin; a train longer than the loop cannot cross (S40).

Platform/terminal track: one train per (dwell + approach + clear):
    trains_per_year_per_platform = U_max * Y / (t_dwell + t_approach + t_clear)
    t_dwell = load_units / load_speed + fixed stop delays (economics.STOP_DELAY_S etc.)
```

Rule for the optimizer:
- **Upgrade single to double track** when the required trains exceed about 60 % of single-track
  capacity and the incremental cost is less than the cost of a parallel single line.
- Otherwise add a loop: splitting the critical section is cheap. **[VERIFY V15: TF3 signal/loop
  behaviour, path signals, one-way signals]**

### 3.2 Train length vs platform length, and tractive limits

- Train capacity = wagons x wagon capacity. Limits: platform/loop length, locomotive tractive
  effort on the ruling grade (already in `best_consists`), and maximum consist length
  **[VERIFY V7]**.
- **Rule**: fix the consist length per line to the shortest platform on the line. Longer platforms
  are a capacity investment that competes with more trains.
- Real practice: longer trains are the main lever for economies of density (the crew and loco are
  shared by more wagons). In the game the "crew" is the loco running cost and price. Per-unit
  capital falls as `(P_loco + n P_wagon) / (n c_w)`. With `P_loco ~ 3.6-6.6 M$` vs
  `P_wagon ~ 0.26 M$` (1900 catalogue), going from 6 to 12 wagons cuts per-unit capital by about
  35 %. Always run the longest train the platform, grade and frequency allow.

### 3.3 Turnaround, load factor, cycle time

```
cycle_time  T_c = 2 * L_route / v_eff + t_load + t_unload + n_stops * t_stop + t_layover
v_eff       = v_max * k_acc   (k_acc ~ 0.85 today in economics.py; better: from accel/brake & curves)
t_layover   = 10-15 % of running time as recovery (transit practice) [S42][S43]
round-trip load factor LF_rt = (loaded unit-km) / (capacity unit-km both directions)
    one-way bulk with empty return: LF_rt <= 50 %
```

Real benchmarks:
- EU road freight runs about **20 % of vehicle-km empty** (24 % national, 13 % international;
  21.6 % in 2024) [S44].
- About **20 % of container moves are empty** [S10].
- A railcar cycle = load, loaded transit, unload, empty transit, plus yard dwell (about 18-24 h per
  yard) [S8].

Targets for the game:
- Outbound LF of 90-100 % via "full load" at the source **[VERIFY V14]**. Round-trip LF of 50 %
  (pure bulk) is acceptable. **Above 65-75 % needs backhaul**, which is the single biggest
  efficiency lever (section 3.4).

### 3.4 Empty backhaul and how to fix it

1. **Backhaul matching**: find a flow `j -> i` (or `j -> l` near i) carried by the same vehicle
   class.
2. **Round-robin circuits**: a line `A -> B -> C -> A` where each leg carries a different cargo,
   such as coal to the steel mill, steel to the tool factory, tools back to the mine (the mine's
   booster). This is the triangulation used for containers [S10].
3. **Empty repositioning as min-cost flow**: given loaded vehicle flows, the empties needed to
   balance every node form a transportation problem. That is the classic empty freight car
   distribution model, solved by min-cost/max-flow [S45][S46][S47].

```
For vehicle class v (set of cargos its wagons accept)  [VERIFY V10: multi-cargo wagons/trucks in TF3]:
  imbalance b_n = sum_in loaded_f - sum_out loaded_f
  min  sum_{(i,j)} c_ij^empty * e_ij   s.t.  sum_j e_nj - sum_j e_jn = b_n,  e >= 0
Value of a backhaul leg (j->i, cargo k'):  p * dhat_ji * x  -  (marginal load/unload time cost)
    because the vehicle runs j->i anyway; it is ~free revenue.
```

A balanced line roughly **doubles revenue per vehicle-hour** and halves vehicle capital per unit.
The candidate generator should actively search for these (section 8.4).

### 3.5 Fleet sizing

```
n_vehicles = ceil( T_c / h )         where headway h = Y / trips_per_year
trips_per_year = V / (cap * LF_out)  (V = units/year on the binding direction)
=> n = ceil( V * T_c / (cap * LF_out * Y) )        (== economics.evaluate_link logic)
+ spare ratio: real fleets keep ~10-15 % spares; in the game only if breakdowns exist [VERIFY V6]
```

The transit form `n = c/h` with cycle time c including 10-15 % layover is standard [S42][S43].

### 3.6 Depot and yard placement

- Depot: on the line, closest to where vehicles start and end service. The cost is the empty
  "deadhead" from the depot plus the depot price. Use UFLP (2.7) with deadhead cost. One depot per
  connected rail component is usually enough.
- Yards / transfer stations: only where flows of several lines meet (hub candidates). Real yards
  add about 1 day of dwell [S8], so in-game transfer time matters for cargo delivery time limits
  **[VERIFY V2]**.

---

## 4. Road freight and passenger transit

### 4.1 Bus/tram line design: frequency and headway

- **Mohring / square-root rule**: optimal frequency `f* ~ sqrt(d / t)` (d = demand rate, t =
  route duration). It balances operator cost (linear in f) against waiting cost (~1/f) [S48].
- **Ridership-frequency elasticity** is about **+0.5** on average (clusters at about +0.3 and
  +1.0). It is highest where frequency is currently low [S49][S50].

```
Passenger line (town):  choose headway h to maximize
    Profit(h) = p * dhat_avg * Riders(h) - (T_c/h) * (annuity(P_bus) + running)
    Riders(h) = R0 * (h0/h)^0.5          (elasticity 0.5, [VERIFY V5]: TF3 passenger generation
                                          probably depends on travel time incl. waiting)
    optimum where marginal riders' revenue = marginal vehicle cost
Max sensible headway in town ~ 10-15 game-min equivalents; below demand -> lower frequency, not
fewer stops.
```

### 4.2 Feeder-to-trunk transfers (freight and passengers)

- Trucks gather cargo from industries not on the trunk to a rail station. The train runs the
  trunk, then trucks distribute to towns. In the game **each leg earns its own straight-line
  income** [S30][S31], so a feeder leg is not a pure cost.
- Transfer penalty: dwell at the transfer station (wait for the next train) is about `h_trunk/2`
  on average for random arrivals. Timed transfers (banks, [S9]) cut it.

### 4.3 Terminal dwell

- Truck stop / bus stop throughput: one vehicle per bay per `(t_load + t_stop + t_manoeuvre)`.
  This is a hard limit the current model ignores. Example from the 1900 sweep: 147 trucks for
  1,000 units/y over 3 km. Every truck in the loop passes the loading bay, so the bay must handle
  `trips/year`, and each trip occupies `cap/load_speed + 2*STOP_DELAY` seconds of the bay.
- **Road capacity**: real lanes carry about 1,800-2,200 veh/h. The game's limit is the
  intersection/stop behaviour **[VERIFY V8]**. Add a soft cap on trucks per road link and per
  station. That is what will push big volumes to rail.

---

## 5. Inventory, warehousing, cross-docking

- **Cross-docking**: inbound goods are moved straight to outbound vehicles with minutes to hours of
  dwell. The facility is a sorting and consolidation hub, not storage [S51][S52]. Its value is
  **consolidation** (many small inbound flows become full outbound loads).
- **Distribution centres / warehouses** act as buffers. They decouple production variability from
  transport, and transport batch size from consumption.
- **Queueing (Kingman / VUT)**: `W_q ~ V x U/(1-U) x T`. Waiting explodes as utilization `U` nears
  1 and multiplies with variability `V` [S53][S54]. That is why UIC 406 caps occupancy at
  60-85 %, and why buffers raise throughput when inflow is bursty.

Game translation:

```
Buffer improves throughput when:
  (a) production arrives in batches/irregularly and the vehicle waits for "full load" ->
      a stock buffer at the station lets trains leave full without idling;
  (b) a processing plant needs several inputs in fixed ratio -> buffer the faster input so the slow
      one does not starve the plant (Leontief: w_j = min_k in_k/a_jk);
  (c) a hub mixes lines of different frequency -> buffer = cross-dock queue.
Required buffer for a line with headway h and production rate q:  ~ q * h  (+ safety ~ q*h*cv)
```

- Does TF3 have warehouses, station storage limits, or cargo expiry while waiting? **[VERIFY V9,
  V11]**. If cargo at stations is lost after a delay, buffers are capped and frequency becomes a
  hard constraint (minimum frequency = buffer capacity / production).
- Processing chains: **balance input ratios**. A steel mill fed 100 % coal and 30 % ore produces
  at 30 %. The LP in 2.2 handles this automatically. The greedy per-link simulation does not, so
  chain bundles are needed (section 8).

---

## 6. Investment economics

### 6.1 NPV, IRR, payback, and the right discount rate

```
NPV(r)  = -C0 + sum_{t=1..H} CF_t / (1+r)^t
IRR     : NPV(IRR) = 0
Payback : first T with cumulative CF >= C0
Annuity : A(r,n) = r / (1 - (1+r)^-n)        (annualised capital charge = C0 * A)
```

- With **capital rationing**, the opportunity cost of a dollar is the return of the best
  *unfunded* project, not the bank rate. Ranking by NPV at 5 % with a 15-year horizon
  (`planner.NPV_YEARS/DISCOUNT`) overweights slow, big projects when the company could compound at
  20-40 %/year by reinvesting.
- **Recommendation**: maintain a **shadow price of capital `lambda`** = the IRR of the marginal
  project funded in the last N months (or the IRR of the best project rejected for lack of cash).
  Rank candidates by `NPV(lambda) / capex` (profitability index at lambda). Equivalently, when cash
  is tight, rank by IRR with a payback tie-break. As the network matures, `lambda` falls toward the
  loan rate and long projects (electrification, double track, water links) become attractive
  automatically.
- **Loans**: borrow when `IRR_marginal_project > true cost of the loan`.
  - Flat interest on the full principal with amortizing repayment costs much more than the nominal
    rate (roughly **1.8-2x** the flat rate as an effective annual rate).
  - Free early repayment makes the loan an option: borrow early and repay when idle cash appears.
  - Compute the loan IRR from its exact cash flows rather than `rate*12/months`.

### 6.2 Capital rationing with interdependence

Use the multi-knapsack (2.9). Interdependencies that greedy misses:
- **Prerequisites**: station before line, depot before vehicles.
- **Synergy**: chain inputs only pay off together. Shared track is cheaper as a pair.
- **Exclusivity**: road vs rail on the same pair; competing consumers for one producer.

Weingartner's point: ranking lists are inferior to the multi-knapsack when projects interact
[S32][S33].

### 6.3 Equipment replacement and economic life

```
EAC(n) = [P - S_n/(1+r)^n] * A(r,n) + PV(operating costs 1..n) * A(r,n)
economic life n* = argmin_n EAC(n)
Defender vs challenger (S55,S56):
  marginal cost of keeping defender one more year
      MC_def = (S_now - S_next) + r*S_now + Opex_def_next - (revenue_def - revenue_chal_equiv)
  replace when MC_def > EAC_challenger(n*)
Finite horizon / non-stationary tech: DP or shortest path over (year, age) (S57).
```

- In TF3 the catalogue lifespans are 50-100 years, so **obsolescence dominates**. The challenger
  is faster and bigger, so the same volume needs fewer vehicles. Its "equivalent revenue" includes
  freed capacity. If running cost or breakdowns grow with age **[VERIFY V6]**, add that to
  `Opex_def`.
- Resale: the code sells at linear depreciated value. Check the in-game resale formula
  **[VERIFY V12]**.
- Practical rule: re-run the defender/challenger test for each line every January (already
  scheduled in `planner.py`). Use `lambda` as `r`, and replace a whole line's fleet at once,
  because mixed fleets constrain headway to the slowest vehicle.

### 6.4 Timing technology upgrades (steam -> diesel/electric, faster track)

- Historical: dieselization won on availability (steam about **35 %** vs diesel about **95 %**),
  lower maintenance, multiple-unit operation, and no water/coal stops [S58][S59]. The game may only
  model price/speed/running cost **[VERIFY V6]**.
- **Electrification** is justified by traffic density. Older guidance put break-even at
  **20-40 Mgt/year**, projected to fall to about 10 Mgt/year as fuel prices rose. Initial cost is
  roughly independent of traffic, while savings scale with trains [S60][S61].

```
Upgrade u (electrify / faster track / double track) on edge set E_u:
  Annual benefit B_t = running savings + fleet capital released * A(lambda, life) + capacity value
                      + extra flow enabled (from LP dual of the capacity constraint)
  Invest in first year t with  B_{t+1} / C_u  >= lambda        (first-year-return timing rule, S36)
  and NPV(lambda) > 0. Benefits grow with traffic => the rule naturally delays upgrades on thin lines.
```

The capacity dual from the LP/MILP (section 2.2/2.3) directly gives the value of extra capacity on
a congested edge. That turns "when to double-track" into an arithmetic check.

### 6.5 Sensitivity: interest, inflation

- If the game inflates prices over time (the loan multiplier scales 0.4 -> 2.4 over 1900-2020 in
  `_loan_mult`) **[VERIFY V12: price/income inflation]**, use **real** cash flows and a real
  discount rate. Otherwise nominal income lags nominal costs and old lines look worse than they are.
- Run every ranking at `lambda`, `lambda/2` and `2*lambda`. Projects whose rank flips are
  candidates for deferral (real-option logic).

---

## 7. Robustness: designing for uncertainty

Uncertainties in the game:
- Industries may close, open or change level **[VERIFY V13]**.
- Town demand grows with service **[VERIFY V5]**.
- New vehicles arrive on fixed years (known in advance, so it is deterministic).
- Loan offers are random.

Techniques:
- **Rolling-horizon re-optimization**: re-solve at every decision epoch (each game year, or on any
  big event) from the actual state, with sunk infrastructure fixed. This is the standard practical
  answer [S29].
- **Two-stage stochastic programming / SAA**: first stage = infrastructure, second stage = flows per
  scenario (industry closures, demand levels). Solve with Benders + sample average approximation
  [S29][S62]. Feasible with 10-30 scenarios on the FCND model if time allows. Otherwise evaluate
  the deterministic plan against the scenarios and penalize fragile designs.
- **Flexibility**: prefer designs whose track serves several producers or consumers (a trunk), so
  that losing one industry strands little capital. Measure **stranded-capital risk** = capex
  usable only by a single industry.
- **Hub redundancy**: no single node should carry more than X % of profit unless a backup consumer
  exists within feeder range. In the MILP, add a constraint or a scenario with that node removed.
- **Modularity**: vehicles are fully modular and can be re-assigned or sold. Track and stations are
  sunk. So under uncertainty, **over-invest in vehicles (flexible) rather than in exotic
  infrastructure (sunk)**, and stage infrastructure (section 2.10).

---

## 8. Concrete recommendations for the TF3 optimizer

### 8.1 Model architecture (target)

```
                 +---------------------------------------------------+
  game export -> |  Layer A: Strategic design per era (every ~10 y)  |  max-profit FCND/SND MILP
                 |  (HiGHS, slope-scaling warm start, 1-5 min)       |  -> target network + hubs
                 +-------------------------+-------------------------+
                                           v target edges, lines, LP duals pi_n^k, lambda
                 +---------------------------------------------------+
                 |  Layer B: Investment scheduling (rolling 3-5 y)   |  multi-knapsack w/ cash
                 |  (HiGHS, seconds)                                 |  -> ordered build list
                 +-------------------------+-------------------------+
                                           v
                 +---------------------------------------------------+
                 |  Layer C: Monthly cash simulation (existing)      |  exact cash, loans, fleets,
                 |  + local search / ALNS on the schedule            |  replacement, verification
                 +---------------------------------------------------+
```

### 8.2 Model-derived mode rules of thumb (from the current `economics.py`)

Sweep run on 2026-10-01 against the live export, coal, route = 1.25 x straight, placeholder
station costs, capital annuitized at 10 %/20 y. Values are annual profit after the capital charge,
in M$/year (road / rail). "inf" = infeasible because of the cargo delivery time limit
(`timeToDeliver`, coal 3,000 s).

| Year | Dist | 200 u/y | 1,000 u/y | 4,000 u/y |
|---|---|---|---|---|
| 1900 | 3 km | 0.78 / 0.36 | 4.41 / 3.57 | 17.9 / 16.4 |
| 1900 | 6 km | 1.64 / 1.50 | 8.95 / 9.06 | 36.2 / 38.5 |
| 1900 | 12 km | 3.36 / 3.25 | 18.0 / 20.0 | 72.9 / 85.2 |
| 1900 | 24 km | inf / 7.05 | inf / 43.6 | inf / 177 |
| 1950 | 12 km | 4.54 / 3.48 | 25.1 / 20.6 | 102 / 87 |
| 1950 | 48 km | inf / 16.8 | inf / 92.9 | inf / 388 |
| 2000 | 24 km | 10.1 / 9.5 | 55.2 / 51.4 | 224 / 215 |
| 2000 | 48 km | 20.5 / 19.2 | 111 / 106 | 449 / 446 |

Truck counts behind the road numbers: 1900, 12 km, 1,000 u/y needs 582 trucks; 1950 needs 98.

Reading:
1. In 1900, **rail wins above about 6 km at 1,000+ u/y and above about 12 km at 500+ u/y**, and is
   the only feasible mode beyond about 20 km (trucks are too slow for the delivery time limit).
2. From the 1950s, **the model says trucks always win** at these distances. The reason is that it
   has **no road/station capacity limits** and rail consists are capped at 12 wagons. Treat this as
   a model gap, not a game truth. Add bay/lane capacity (4.3) and platform-limited train length
   (3.2), then re-run the sweep per era and store it as a lookup table:
   `mode_breakeven[year][cargo] -> (min_volume, min_distance)`.
3. Capital productivity (ROI) is 18-45 %/yr and rises with distance until the delivery-time limit.
   **Prefer long legs** (income per leg is linear in straight distance; terminal time is fixed).
4. Typical production volumes on this map are **20-330 u/y per industry**. Single industries
   rarely fill a train line. **Consolidation (several producers on one trunk) is what makes rail
   viable**, which is exactly the real-world density argument.

### 8.3 Rules of thumb (with numbers) to encode now

| Rule | Value | Source / status |
|---|---|---|
| Max planned line occupancy (track, platform, truck bay) | 60-75 % of theoretical | UIC 406 [S38]; Kingman [S53] |
| Double track when single-track demand exceeds | ~60 % of single capacity, or the critical section cannot be split | [S40] + UIC 406 |
| Layover/recovery in cycle time | +10-15 % of running time | [S42][S43] |
| Round-trip LF target, bulk one-way | 50 % (accept); seek backhaul to reach 65-80 % | [S44][S10] |
| Feeder trucks to a rail trunk | only if feeder leg < ~25-35 % of trunk distance and trunk volume >= 1 train-load per headway | model + density argument |
| Hub detour acceptable (revenue-positive in game) | delta = (d_oh + d_hd)/d_od <= 1.3-1.5, within delivery time | **[VERIFY V1, V2]** |
| Upgrade timing | first-year benefit / cost >= lambda | [S36] |
| Discount rate for ranking | lambda = marginal project IRR (often 20-40 % early, falling to the loan rate) | [S32], section 6 |
| Replace vehicles | marginal cost of defender > EAC of challenger | [S55][S56] |
| Bus frequency | f* proportional to sqrt(demand / route time); elasticity about +0.5 | [S48][S49] |
| Electrify | when traffic density is high; game-equivalent: running-cost savings + fleet reduction pass the first-year rule | [S60][S61] **[VERIFY catenary costs]** |

### 8.4 Candidate generation: representing hubs, transfers and backhaul

The optimizer is only as good as its candidate set. Generate these **columns**:

1. **Direct legs** (existing): producer to consumer per cargo and mode. Prune to the k = 8 nearest
   plus the farthest-feasible consumers (distance is revenue), within the delivery time limit.
2. **Trunk legs**: for each pair of cluster centroids or Delaunay edges (about 3n edges), a rail
   trunk candidate with **junction nodes**, so that several producers share one line
   (Steiner/Takahashi-Matsuyama on the existing `NetworkMatrix.build`).
3. **Feeder legs**: truck legs from an industry to any rail/water station candidate within R
   (R = 1.5-4 km depending on era). These are paid per leg, so cost them as normal legs, not as
   overhead.
4. **Transfer nodes**: duplicate each hub candidate into mode-specific station nodes, connected by
   transfer arcs with cost `h_n` and time `t_n` (dwell about `h_trunk/2`). Hub candidates are
   processing plants, the largest towns, ports, and the 10-15 top nodes by LP-dual centrality.
   Cap the delivery time on the whole path **[VERIFY V2: per-leg or end-to-end timer]**.
5. **Circuit/backhaul lines**: for each vehicle class v (set of compatible cargos):
   - pairs: legs `i->j (k)` and `j->i (k')` with k, k' both in v;
   - triangles: `i->j->l->i` with compatible cargos and detour <= 1.5;
   - cost each candidate line with the SND cycle time and fleet, and credit all loaded legs.
   - Rank by `(sum leg revenue - line cost) / capital`.
   - Also run the **empty-repositioning min-cost flow** per vehicle class to price residual empties.
6. **Upgrade columns**: double track, loops, faster track, electrification, longer platforms. Each is
   valued by the LP capacity duals and timed with the first-year rule.
7. **Passenger lines**: intra-town bus lines (frequency by square-root rule); inter-town rail lines
   along freight trunks (shared track, mixed traffic -> 60-75 % occupancy cap).

### 8.5 Algorithm changes, prioritized

1. **Shadow-price discounting and true loan IRR.** Replace `DISCOUNT = 0.05 / NPV_YEARS = 15` with
   an adaptive `lambda` (marginal IRR). Compare loans by exact IRR. *Cheap, immediate impact on
   every decision.*
2. **Capacity constraints**: truck/bus bay throughput, road vehicles per link, platform throughput,
   single/double-track capacity (formulas in 3.1/4.3) with a 60-75 % cap. Re-run the mode
   break-even table per era. *Fixes the "582 trucks" failure mode and makes rail emerge where it
   should.*
3. **Max-profit generalized flow LP (2.2) with processing recipes**, solved by HiGHS each simulated
   year. Use its **dual prices** to value candidates chain-aware (reduced profit) instead of
   single-link NPV. *Solves chain starvation and wrong producer-consumer pairing.*
4. **Backhaul/circuit candidates + empty-flow min-cost repositioning** (3.4, 8.4-5). *Highest
   in-game profit lever after capacity.*
5. **Shared-infrastructure costing**: incremental Steiner connection cost to the existing network,
   junction nodes, and trunk candidates (2.6, 8.4-2).
6. **Hub/transfer representation** (8.4-4) inside the flow LP/MILP, using per-leg income.
7. **Static FCND/SND MILP per era** with a slope-scaling warm start (2.3/2.4) to produce a target
   network (1900/1925/1950/1975/2000/2025).
8. **Multi-knapsack lookahead scheduling** (2.9) with cash/loan dynamics, guided by the target
   network. It replaces pure greedy selection inside a 12-36 month window.
9. **Defender/challenger fleet replacement** (6.3) and **first-year-return upgrade timing** (6.4).
10. **Local search / ALNS over the build schedule**: destroy-repair moves (delay/advance a project,
    swap mode, merge links into a trunk, add a backhaul) evaluated by the exact monthly simulation.
11. **Robustness**: rolling yearly re-optimization from the actual state, scenario stress tests
    (each top industry closed), and a stranded-capital penalty.
12. **Passenger network design**: bus frequency optimization, inter-town rail on freight trunks,
    and timed transfers.

### 8.6 Game mechanics these recommendations depend on (hand-off to `docs/mechanics.md`)

| ID | Question | Affects |
|---|---|---|
| V1 | Income paid per leg on straight line? Any speed/time factor in TF3 (TF2 had a vehicle-speed factor [S31])? Climb bonus (`CLIMB_WEIGHT`)? | hubs, detours, vehicle choice |
| V2 | Delivery time limit (`timeToDeliver`) per leg or end-to-end? What happens on expiry? | hubs/transfers, max distance |
| V3 | How cargo chooses destinations and paths (fastest path? transfers automatic? line order?) | flow model validity |
| V4 | Industry production response to service (upgrade/downgrade thresholds, served %) | supply S_i(t) |
| V5 | Town demand per land use; passenger generation vs travel time/frequency | demand D_t, bus frequency |
| V6 | Vehicle ageing: running cost growth, breakdowns, reliability after lifespan | replacement |
| V7 | Station/platform length limits, bays, in-place expansion, max train length | capacity, consist size |
| V8 | Road congestion / intersection throughput for trucks | mode choice |
| V9 | Cargo waiting at stations: storage cap, decay/expiry | buffers, minimum frequency |
| V10 | Which wagons/trucks carry multiple cargo types (universal vehicles) | backhaul/circuits |
| V11 | Warehouses / storage buildings in TF3? | buffering, cross-dock hubs |
| V12 | Inflation of prices/income over years; vehicle resale formula | discounting, replacement |
| V13 | Industry closure/opening rules | robustness |
| V14 | "Full load" / load-if-available behaviour and loading time model | LF, cycle time |
| V15 | Signal/block and passing-loop behaviour; acceleration model | line capacity, v_eff |

---

## 9. Recommended roadmap for the optimizer

Ordered by expected impact per effort.

1. **Adaptive cost of capital.** Compute `lambda` (marginal funded IRR, smoothed) monthly. Rank by
   NPV(lambda)/capex. Evaluate loans by exact IRR. Drop the fixed `MAX_PAYBACK` in favour of
   `IRR > lambda`.
2. **Capacity layer.** Add per-station bay/platform throughput, per-road-link vehicle caps, and
   single/double-track capacity (UIC-style 60-75 % cap) to `evaluate_link` and to the simulation.
   Regenerate the per-era road-vs-rail break-even table (section 8.2) and keep it as a regression
   test.
3. **Generalized flow LP with recipes** (`tf3planner/flow.py`, HiGHS via `highspy`). Run yearly in the
   simulation. Use node duals to rank candidates (reduced profit) and to detect starved chains.
4. **Backhaul and circuits.** Build a vehicle-class compatibility matrix from the catalogue. Generate
   pair/triangle line candidates. Add an empty-repositioning min-cost flow (OR-Tools) to price
   empties.
5. **Shared infrastructure.** Incremental connection cost to the built network
   (Takahashi-Matsuyama on the cost matrix, then grid A* for the chosen edges), junction/Steiner
   candidates, trunk corridors with feeder legs.
6. **Hubs and transfers in the flow model.** Mode-split station nodes, transfer arcs with dwell,
   end-to-end delivery-time check (after V2 is verified), per-leg revenue.
7. **Era target network.** Static max-profit FCND/SND MILP for 6 snapshot years, with a
   slope-scaling heuristic warm start and a 5-minute HiGHS cap. Output target edges, hubs, lines.
8. **Lookahead investment scheduling.** Multi-period knapsack (36-month window, monthly cash rows,
   loan variables) that picks the next builds toward the target network. Keep the monthly simulation
   as the exact evaluator.
9. **Fleet replacement and tech upgrades.** Defender/challenger EAC per line each January;
   first-year-return timing for double track, faster track and electrification, using LP capacity
   duals as benefits.
10. **ALNS schedule improvement.** Destroy/repair over the build list (delay, advance, swap mode,
    merge into trunk, add backhaul) for N iterations, scored by the simulation's final net worth.
11. **Robustness pass.** Yearly rolling re-optimization from the actual state, 10-30 closure/demand
    scenarios, a stranded-capital penalty, and hub redundancy checks.
12. **Passenger module.** Bus line frequency (square-root rule, elasticity about 0.5, calibrate to
    V5), inter-town passenger rail on freight trunks, timed transfers at hubs.
13. **Calibration loop.** Compare simulated vs in-game income, trip times, fleet counts and
    production for a few lines. Tune `k_acc`, stop delays, station costs and the town demand factor.
    Close the V1-V15 items.

---

## 10. Terminal and storage facility selection

Game facts supplied by the coordinator (to confirm in `docs/mechanics.md`):
- TF3 stations are **modular**. Cost is the sum of modules, for example a cargo station building at
  18k plus platforms, or a harbour pier at 90k.
- **Warehouses** hold stock: 150k to build plus 25k/yr in 1900, x3 from 1940, x9 from 1980, and
  specialized warehouses cost x2.
- **Road maintenance stations** cost 60k plus 10k/yr.

Placeholders in `economics.STATION_COST/DEPOT_COST` should be replaced by module-based costs.

### 10.1 Choosing terminal type and size from throughput

Real operators size every terminal as a set of **parallel servers** (truck doors/bays, platform
tracks, sidings, berths) fed by arrivals. The same three steps apply everywhere:

```
1. Offered load      A = lambda * s            lambda = arrivals per time unit, s = server occupancy per arrival
2. Servers           c = ceil( A / rho_target )
3. Check waiting     random arrivals: Erlang C / M/M/c  P(wait) = C(A,c)   (S63)
                     scheduled arrivals (timetabled lines): deterministic, higher rho allowed
Occupancy per arrival s = approach + (wait-for-load) + load/unload + departure/clear
```

Occupancy targets used in practice:

| Facility | Target max occupancy | Source |
|---|---|---|
| Truck dock doors / bays | 75-85 % (85-95 % quoted as "optimal" for scheduled docks; 75 % + 25 % peak factor + 10 % redundancy in sizing examples) | [S64] |
| Rail line / platform tracks | 60-75 % mixed, up to 85 % suburban (UIC 406) | [S38] |
| Berths: by number of berths in the group | 1: 40 %, 2: 50 %, 3: 55 %, 4: 60 %, 5: 65 %, 6-10: 70 %; berth waiting ratio < 30 % | UNCTAD via [S65] |

The berth table shows a general queueing result: **pooled servers tolerate higher utilization**.
One server must stay near 40 % to keep waits short, while 6-10 pooled servers can run at 70 %.

Sizing formulas for the planner (game seconds, `Y` = 1461 s per game year):

```
Truck/bus bays at a stop:
    trips_per_year   = V / (cap_truck * LF)
    s_bay            = t_approach + cap_truck / load_speed + 2*STOP_DELAY_S + t_fullload_wait
    bays             = ceil( trips_per_year * s_bay / (rho * Y) ),  rho = 0.6-0.75 (1 bay: <= 0.5)

Rail platforms / sidings (per station, per direction for through stations):
    trains_per_year  = V / (cap_train * LF)
    s_platform       = t_approach + t_dwell + t_clear (+ t_reverse for terminus) + t_fullload_wait
    platforms        = ceil( trains_per_year * s_platform / (rho * Y) ),  rho = 0.6-0.75
    platform_length >= train_length (consist length incl. loco) + margin;
    loop/siding length >= longest train (S40)

Berths (ports):
    ships_per_year   = V / (cap_ship * LF);  s_berth = approach + (cap_ship / load_speed) + clear
    berths: smallest c with ships*s_berth/(c*Y) <= UNCTAD occupancy for c berths
    berth (pier) length >= ship length + manoeuvring margin [VERIFY V16: TF3 pier sizes vs ship classes]

Storage area (yard / warehouse):
    stock_capacity  >= throughput * mean_dwell + safety_stock     (Little's law)
```

**Game-specific reading** (catalogue and production scale of this map):
- Loading is fast and volumes are small (20-330 units/yr per industry). The cost of a server is
  therefore dominated by **full-load waiting**, not by loading.
- Example: a 96-unit train (12 wagons x 8) loading at 300 u/yr production waits about
  `96/300 yr ~ 470 s` on the platform. Actual loading takes about 10-20 s. Its platform occupancy
  is about 32 % of the year from waiting alone.
- Trucks show the same pattern. 1,000 u/yr in 6-unit trucks is about 167 departures/yr, each
  holding the bay for at least about 10 s just to load. That is over 100 % of one bay once
  approach and stop delays are counted.
- Conclusions:
  - One rail platform is enough for nearly every freight line *if trains do not wait for full load
    on the platform*.
  - Busy truck stops need 2-4 bays.
  - The real lever is **separating accumulation from loading** (a buffer or warehouse, section
    10.2, or a waiting siding).
  **[VERIFY V14: full-load waiting; V17: does stock accumulate at the station while no vehicle is
  present]**

Module-level cost model (replaces the flat `STATION_COST`):

```
station_cost(n, m, f) = building_f + n_platform * platform_cost_f + n_extra_modules * module_cost
                        (cargo station: 18k building + platforms; harbor: 90k per pier; [VERIFY V16] exact
                         module prices, era multipliers, infra_cost_scale)
choose the cheapest configuration (f, n_platform) whose capacity >= required servers at rho_target.
```

### 10.2 Intermediate storage, warehouses, distribution centres and cross-docks as buffers

**Real practice.** Between a feeder mode and a trunk mode (truck -> rail trunk -> truck) the
operator places a terminal that is either:
- a **cross-dock**: minutes to hours of dwell, a pure sorting and consolidation point [S51][S52]; or
- a **warehouse / distribution centre**: holds stock to decouple flows of different batch sizes and
  timing.

The dispatch rule at a consolidation point is one of three [S66]:
- *quantity-based*: dispatch when Q is accumulated;
- *time-based*: dispatch every T;
- *hybrid*: dispatch at Q or T, whichever comes first.

Quantity policies minimize vehicles. Time policies bound delay. Hybrid policies trade the two.

**When a buffer raises throughput** (and when it does not):

| Situation | Buffer effect |
|---|---|
| Trunk train must leave full but feeder inflow is slow (`Q_train >> production per headway`) | Moves the accumulation time off the platform and train: platform server time drops from `t_fill + t_load` to `t_load`, and trains do not idle (capital saving = idle train-hours) |
| Inflow arrives in discrete batches (trucks, irregular production) and the trunk runs on a fixed headway | Smooths arrivals (Kingman: queue time proportional to variability x U/(1-U)) [S53]; lets the trunk run at higher U without spillback |
| Primary industry **loses production when its output stock is full** (stated in `economics.py` docstring) | Buffer capacity converts lost production into income; the most valuable case in the game |
| Processing plant needs several inputs in a fixed ratio | Buffers the faster input so the plant runs at `min_k(in_k/a_k)` continuously |
| Mean inflow > trunk capacity | **No gain.** A buffer cannot create capacity; it only fills up |
| Deterministic flows, already balanced | Negligible gain; costs money |

Buffer sizing formulas:

```
Cycle (consolidation) stock: accumulates one dispatch quantity between departures
    Q_dispatch   = cap_train * LF_target
    avg_stock    = Q_dispatch / 2 ;   peak_cycle_stock = Q_dispatch
Safety stock (inflow variability over the replenishment/headway interval L)   (S67)
    SS = z * sqrt( L * sigma_d^2 + d^2 * sigma_L^2 )        z = 1.28 (90%), 1.645 (95%), 2.33 (99%)
    d = mean inflow per time, sigma_d = its std dev per time, L = mean time between trunk departures,
    sigma_L = std dev of that interval (bunching).
    For discrete truck arrivals of size c_t at Poisson rate: sigma_d^2 ~ c_t * d   (compound Poisson)
Warehouse capacity  >= peak_cycle_stock + SS  (per cargo; specialized warehouse if one cargo class)
Dispatch rule: hybrid (Q, T) with T <= cargo delivery time limit minus trunk + last-mile time [VERIFY V2]
```

Economic test for a game warehouse. Use the annualized cost at the shadow price of capital `lambda`
(section 6.1):

```
cost_wh(year) = 150k * k_era * k_spec * A(lambda, life) + 25k * k_era * k_spec    per year
    k_era = 1 (<1940), 3 (1940-79), 9 (>=1980);  k_spec = 2 for specialized
    e.g. lambda = 25 %, long life: 1900 ~ 62k/yr ; 1950 ~ 190k/yr ; 1990 ~ 560k/yr (general)
value_wh  = (units of production no longer lost) * income_per_unit(downstream legs)
          + (train/truck idle hours removed) * hourly capital+running cost of that fleet
          + (platform/bay modules avoided) * their annualised cost
```

- Income per unit is about `3.8 $ x straight-line m`, so about **38k $ per unit for a 10 km leg**.
- A 1900 warehouse therefore pays if it saves about **2 units/yr** of lost production, and a 1990
  one if it saves about **15 units/yr**.
- Buffers are almost always worth it wherever production would otherwise be lost or trains idle,
  **provided the game pays the downstream legs normally when cargo passes through a warehouse.**

**[VERIFY V18: warehouse semantics in TF3]**
- Which cargos a warehouse accepts.
- Whether delivering into or out of a warehouse counts as a paid leg (the per-leg rule would make
  `truck -> warehouse -> train` two paid legs).
- Whether a warehouse counts as a destination for industry production (cargo "demand").
- Its capacity per module.
- Whether a warehouse stops primary-industry production loss.

### 10.3 Depot and maintenance facility placement

**Real practice**:
- Bus garages: "250 buses is the optimal storage capacity of a new bus facility". Depots below
  about 80-100 buses are inefficient, and above about 200-220 there is no further efficiency gain
  [S68].
- Depot location minimizes **deadhead** (empty pull-out/pull-in km). Put it near the line terminal
  with the most pull-outs, as a p-median on vehicle start points.
- Rail maintenance depots sit at line ends or hubs where trains lay over anyway.

Game encoding:

```
Depots (vehicle purchase / maintenance) per mode and connected network component:
    p-median: min sum_lines n_vehicles_l * deadhead(depot, line_start_l) + sum_d F_d * open_d
    Usually 1 per component; add a 2nd when deadhead cost of the far cluster
        (n_far * 2 * d_extra * running_per_m) * A^-1 > F_d       [VERIFY V19: do TF3 vehicles visit depots
        for maintenance during service; is there a capacity per depot]
Road maintenance stations (60k + 10k/yr):
    set covering of road links / road lines within service radius R:
    min sum_s (60k*A(lambda,life) + 10k) * u_s   s.t.  sum_{s covers link e} u_s >= 1  for every used link e
    [VERIFY V19: what it maintains (road condition? vehicle condition?), its radius, effect of not having one]
Rule of thumb: fleet per depot is not a limit in the game unless V19 says so; place depots for
deadhead, not for capacity.
```

### 10.4 Encoding facility choice in the MILP

The facility choice plugs into the FCND/SND model of sections 2.3/2.4 as **binary selection of one
facility type per node and mode, with capacity coupling and catchment**:

```
Sets:  sites S (industry/town nodes + candidate station sites: k-medoids of industries within catchment R_f),
       modes m, facility types F_m (e.g. small/medium/large truck stop; 1/2/4-platform rail station; 1/2 piers;
       none), warehouse types W (none / general / specialized), cargos k
Variables:
  z_{s,m,f} in {0,1}   facility type f of mode m at site s
  n_{s,m}   in Z+      extra platform/bay/pier modules (optional finer sizing)
  w_{s,g}   in {0,1}   warehouse of type g at site s
  I_{s}^k   >= 0       storage capacity used (steady state: peak cycle + safety stock)
  x_a^k     >= 0       cargo flow on legs (as before), trains_a / trucks_a derived linearly from x
Constraints:
  sum_f z_{s,m,f} <= 1                                              one facility type per site and mode
  x_a^k <= Mcap * sum_f z_{tail(a),m(a),f},  same for head(a)       legs need stations at both ends
  catchment: industry i may load/unload at site s only if dist(i,s) <= R_f  -> pre-filter legs,
             or  x_{i->s}^k <= Mcap * sum_{f: R_f >= dist(i,s)} z_{s,m,f}
  server capacity (platforms/bays/berths):
      sum_{a at s, mode m} vehicles_a * (t_load_a + t_fixed_a + (1 - wfull_s) * t_fill_a)
          <= Y * ( sum_f rho_f * servers_f * z_{s,m,f} + rho * n_{s,m} )
      with wfull_s = sum_g w_{s,g}:  buffer removes fill-waiting from the server
      (bilinear term vehicles_a * w  -> linearize by precomputing two capacity coefficients and
       using  cap_s <= base_cap(z) + delta_cap * w  , i.e. warehouse adds equivalent server capacity)
  storage: I_s^k >= Q_dispatch_k(s) + SS_k(s)     if the site is a consolidation point;
           sum_k I_s^k <= sum_g Cap_g * w_{s,g}  ;  specialized warehouse restricts k to its class
  production-loss term: lost_i^k >= L0_i^k * (1 - sum_g w_{s(i),g}) - (pickup capacity slack)
        (precompute L0 = expected lost units without buffer from pickup interval vs industry stock cap)
  depot coverage: sum_{d in component} open_d >= 1 per used mode-component; maint. set cover (10.3)
Objective additions (annualised at lambda):
  - sum (station_cost(s,m,f) * A + maint_f) z_{s,m,f} - sum module_cost * A * n_{s,m}
  - sum (wh_cost_g(era) * A + wh_upkeep_g(era)) w_{s,g} - income lost on lost_i^k
Multi-period (2.8): z, w, n monotone in t (stations persist; modules can be added later -> real option);
  warehouse upkeep era multipliers applied per period; optional inventory balance
  I_{s,t}^k = I_{s,t-1}^k + in - out  if seasonal effects are modelled.
```

Size: about 150 sites x 3 modes x 4 types + 150 x 3 warehouse options gives about 2,300 extra
binaries. That is acceptable for HiGHS with the warm start. If it gets slow, restrict sites to
those touched by candidate legs (usually < 60) and solve station sizing *after* the topology MILP.
In that case sizing is a separable post-processing knapsack per site.

Additions to the roadmap (section 9), intended to slot in after step 2 (capacity layer):
- **2a.** Replace flat station costs with module-based costs and explicit server counts (bays,
  platforms, piers) sized by the formulas in 10.1.
- **2b.** Add warehouse and buffer candidates at producers and transfer points, valued by avoided
  production loss and removed full-load waiting (10.2). Encode them as `z/w` binaries in the MILP
  (10.4).
- **2c.** Add depot and road-maintenance placement as p-median / set cover after topology is fixed
  (10.3).

New verification items: **V16** module prices and pier/ship sizes; **V17** station stock
accumulation; **V18** warehouse semantics (paid legs, accepted cargo, capacity, effect on
production loss); **V19** depot and road-maintenance-station function, radius and capacity.

---

## Sources

- [S1] Spoke-hub distribution paradigm, Wikipedia: https://en.wikipedia.org/wiki/Spoke%E2%80%93hub_distribution_paradigm
- [S2] Economies of density, scale and scope in the airline industry (SWOV record): https://swov.nl/en/publicatie/economies-density-multiproduct-scale-and-scope-airline-industry
- [S3] Jara-Diaz & Basso, Distinguishing multiproduct economies of scale from economies of density on a fixed-size transport network: https://www.cec.uchile.cl/~dicidet/sjara/SJ-LB2006.pdf
- [S4] Rodrigue, The Geography of Transport Systems, "Distance, Modal Choice and Transport Cost": https://transportgeography.org/contents/chapter5/transportation-modes-modal-competition-modal-shift/distance-modal-choice-transport-cost/
- [S5] Intermodal Break-Even Distances: A Fetish of 300 Kilometres?: https://www.emeraldinsight.com/insight/content/doi/10.1108/S2044-994120140000006009 ; CARGOSPEED final report: https://trimis.ec.europa.eu/system/files/project/documents/20060727_143123_02411_CARGOSPEED_Final_Report.pdf
- [S6] EU Rail Freight Corridors (Reg. 913/2010), Bundesnetzagentur: https://bundesnetzagentur.de/EN/Areas/Rail/Companies/International/Corridors/Corridors-node.html ; RNE corridor management: https://rne.eu/corridor-management/links/
- [S7] Dedicated freight corridors in India, Wikipedia: https://en.wikipedia.org/wiki/Dedicated_freight_corridors_in_India
- [S8] Yard dwell and car cycle (MIT Transportation Systems notes): https://ocw.iti.hr/courses/civil-and-environmental-engineering/1-221j-transportation-systems-fall-2004/lecture-notes/trans_sys_chap16.pdf ; Railway Age "Keeping rolling stock rolling": https://www.old.railwayage.com/?p=686
- [S9] Airline hub banks: https://navan.com/resources/glossary/hub-and-spoke ; FlightGlobal "Breaking the bank": https://flightglobal.com/breaking-the-bank/50482.article
- [S10] Empty container triangulation: https://indiaseatradenews.com/strategies-for-minimising-empty-container-runs/ ; empty repositioning in inland networks: https://optimization-online.org/?p=16612
- [S11] Transportation problem: https://en.wikipedia.org/wiki/Transportation_theory_(mathematics)
- [S12] OR-Tools minimum cost flows: https://developers.google.com/optimization/flow/mincostflow
- [S13] HiGHS solver: https://en.wikipedia.org/wiki/HiGHS_optimization_solver ; https://pypi.org/project/highspy
- [S14] Gendron, Crainic, Frangioni, Multicommodity capacitated network design: https://www.academia.edu/13141123/Multicommodity_Capacitated_Network_Design
- [S15] Gendron, Decomposition for network design (lecture notes): https://docenti.ing.unipi.it/dottinformazione/Formazione/OffForm2012/Gendron/SoloTesto.html
- [S16] Costa, A survey on Benders decomposition applied to fixed-charge network design: https://findanexpert.unimelb.edu.au/scholarlywork/738307-a-survey-on-benders-decomposition-applied-to-fixed-charge-network-design-problems
- [S17] Rahmaniani et al., updated survey on Benders for FCND (CIRRELT): https://www.cirrelt.ca/DocumentsTravail/CIRRELT-2011-56.pdf
- [S18] Kim & Pardalos, dynamic slope scaling for fixed-charge network flow: https://iaorifors.com/paper/36112 ; DSSP + tabu: https://link.springer.com/article/10.1007/s10614-006-9028-4
- [S19] ALNS for service network design: https://research.nottingham.edu.cn:443/en/publications/a-new-fast-large-neighbourhood-search-for-service-network-design- ; https://arxiv.org/pdf/2603.24369
- [S20] Crainic, Service network design in freight transportation, EJOR 122(2) 2000: https://iaorifors.com/paper/31404
- [S21] On "A quadratic integer program for the location of interacting hub facilities" (O'Kelly 1987): https://portal.mardi4nfdi.de/entity/Q915642
- [S22] Campbell, Integer programming formulations of discrete hub location problems: https://iaorifors.com/paper/19352 ; Ernst & Krishnamoorthy, Hub location and the p-hub median problem: https://pubsonline.informs.org/doi/10.1287/opre.44.6.923
- [S23] Alumur & Kara, Network hub location problems: the state of the art (EJOR 2008): https://repository.bilkent.edu.tr/bitstream/handle/11693/22997/Network_hub_location_problems_The_state_of_the_art.pdf;sequence=1
- [S24] Drop-add-swap heuristic for UFLP: https://scitepress.org/publishedPapers/2009/21731/pdf/index.html ; Lagrangean relaxation for UFLP: https://www.iaorifors.com/paper/10814
- [S25] Takahashi-Matsuyama Steiner heuristic (OGDF implementation notes): https://weghos.duckdns.org/ogdf/OGDF/include/ogdf/graphalg/MinSteinerTreeTakahashi.h.html ; Strong Steiner tree approximations in practice: https://arxiv.org/pdf/1409.8318
- [S26] Steiner heuristics and their pairwise limitation: https://www.mdpi.com/1999-4893/19/3/232
- [S27] Barmann, Martin, Schuelldorf, A Decomposition Method for Multiperiod Railway Network Expansion (Transportation Science): https://pubsonline.informs.org/doi/10.1287/trsc.2017.0747
- [S28] Multi-period network design formulations (CIRRELT 2021-43): https://www.cirrelt.ca/documentstravail/cirrelt-2021-43.pdf
- [S29] Two-echelon distribution network design under uncertainty (rolling horizon, Benders + SAA): https://hal.archives-ouvertes.fr/hal-02167587
- [S30] TF2 payment formula discussion: https://cal1.lr.ggtyler.dev/r/TransportFever/comments/ztpldt/update_payment_formula
- [S31] TF2 Steam discussion on per-leg revenue and speed: https://steamcommunity.com/app/1066780/discussions/0/3783625216798366301
- [S32] Weingartner, Capital Budgeting of Interrelated Projects (Management Science 1966): https://ideas.repec.org/a/inm/ormnsc/v12y1966i7p485-516.html
- [S33] McCarl, integer programming formulations (multi-knapsack capital budgeting): https://agecoresearch.tamu.edu/mccarl/wp-content/uploads/sites/4/2023/12/new16.pdf
- [S34] NZ Ministry of Transport, Adaptive investment management / real options: https://www.transport.govt.nz/assets/Uploads/Paper/MOT-Real-Options.pdf
- [S35] Real options valuation overview: https://umbrex.com/resources/frameworks/strategy-frameworks/real-options-valuation/
- [S36] McDonald & Siegel, The Value of Waiting to Invest (NBER w1019): https://www.nber.org/system/files/working_papers/w1019/w1019.pdf ; first-year rate of return timing (WCTR): https://wctrs-society.com/wp-content/uploads/abstracts/lisbon/general/03262.pdf
- [S37] Trenolab, rail network capacity (UIC 405 formula): https://docs.trenolab.com/docs/getting-started/trenolive/timetabling/rail_network_capacity
- [S38] UIC 406 occupancy recommendations (table): https://etrr.springeropen.com/articles/10.1186/s12544-018-0335-3/tables/1
- [S39] UIC 406 leaflet (2013): https://tamannaei.iut.ac.ir/sites/tamannaei.iut.ac.ir/files/files_course/uic406_2013.pdf ; Landex, Capacity measurement with the UIC 406 method: https://library.witpress.com/elibrary/wit-transactions-on-the-built-environment/103/19512
- [S40] Single-track railway, Wikipedia: https://en.wikipedia.org/wiki/Single-track_railway
- [S41] Track capacity (Japan Railway technical text): https://nippon.zaidan.info/seikabutsu/1996/00502/contents/076.htm
- [S42] Cycle time and number of vehicles (U. Idaho NIATT): https://www.webpages.uidaho.edu/niatt_labmanual/Chapters/busserviceplanning/exampleproblems/CycleTimeAndNumberOfVehicles.htm
- [S43] Transit scheduling math (Planetizen): https://www.planetizen.com/blogs/136825-transit-scheduling-101-scheduling-math ; MIT 1.258J lecture notes: https://live.ocw.mit.edu/courses/1-258j-public-transportation-systems-spring-2017/ec0e2165e7705f51d711b299acdb6dbf_MIT1_258JS17_lec13.pdf
- [S44] Eurostat, a fifth of road freight km by empty vehicles: https://ec.europa.eu/eurostat/web/products-eurostat-news/-/ddn-20211210-1
- [S45] Economies of scale in empty freight car distribution (Transportation Science 2004): https://pubsonline.informs.org/doi/fpi/10.1287/trsc.1030.0061
- [S46] Engels, A generalized network model for freight car distribution (thesis): https://kups.ub.uni-koeln.de/4262/1/Thesis_EngelsBirgit_AGeneralizedNetworkModelForFreightCarDistribution.pdf
- [S47] Min-cost max-flow for railway empty car distribution (ASCE): https://ascelibrary.com/doi/10.1061/41039%28345%29465
- [S48] Square-root rule / Mohring effect: https://arxiv.org/pdf/2609.04520 ; https://arxiv.org/pdf/2405.10074
- [S49] Litman, Transit price elasticities and cross-elasticities (VTPI): https://www.vtpi.org/tranelas.pdf
- [S50] Berrebi, Joshi, Watkins, On ridership and frequency: https://arxiv.org/pdf/2002.02493
- [S51] Cross docking (Inbound Logistics): https://www.inboundlogistics.com/articles/cross-docking
- [S52] Cross docking (Maersk): https://www.maersk.com/pt-br/logistics-explained/storage-and-warehousing/2024/06/24/cross-docking
- [S53] Kingman formula (AllAboutLean): https://www.allaboutlean.com/kingman-formula/
- [S54] Kingman / VUT in Factory Physics: https://www.fabrico.io/blog/kingman-formula-queue/
- [S55] SFU ENSC 201, replacement analysis (defender/challenger): https://www.sfu.ca/~jones/ENSC201/Unit10/lecture10.html
- [S56] AUB INDE300 ch. 11 replacement notes: https://sites.aub.edu.lb/bacel/files/2021/03/INDE300_Ch_11_Notes_20.pdf
- [S57] Equipment replacement via DP/shortest path (agecon): https://ageconsearch.umn.edu/record/207419/files/2013v52n3_05_EquipmentReplacement.pdf
- [S58] Saturday Evening Post, why steam disappeared: https://www.saturdayeveningpost.com/2013/05/locomotive-diesel-engine/
- [S59] Why BR gave up on steam: https://advanced-steam.org/6-why-did-br-give-up-on-steam-lessons-learned
- [S60] Railways and energy (TRB, electrification thresholds): https://trid.trb.org/View/201115
- [S61] Electrification thresholds (MATEC 2018): https://www.matec-conferences.org/articles/matecconf/pdf/2018/39/matecconf_met2018_06005.pdf
- [S62] Santoso et al., stochastic programming for supply chain network design (SAA): https://optimization-online.org/2003/06/670/
- [S63] Erlang C / M/M/c queue sizing: https://metricgate.com/docs/queue-mmc-erlang/ ; TU Berlin queueing chapter: https://platform.commit.tu-berlin.de/en/book/fsp/chapter/ch18/section/s05
- [S64] Dock door sizing and utilization: https://racklify.com/encyclopedia/how-to-calculate-inbound-capacity-for-docks-labor-equipment-and-storage/ ; https://codingace.net/construction/dock_door_quantity.php ; https://www.item.com/vi/glossary/dock-utilization
- [S65] UNCTAD berth occupancy guidelines (cited in): https://jurnal.unissula.ac.id/index.php/akta/article/download/16005/pdf ; JICA port study: https://openjicareport.jica.go.jp/pdf/11691854_11.pdf
- [S66] Wei, Cetinkaya, Cline, hybrid vs time- and quantity-based shipment consolidation: https://arxiv.org/abs/2012.02392v1
- [S67] Safety stock formulas: https://www.cleverence.com/articles/for-business/how-to-calculate-safety-stock-and-reorder-point-4827/
- [S68] Bus garage sizing: https://planitmetro.com/?p=5301 ; TRB "Additional evidence on the problem of sizing bus garages": https://trid.trb.org/View/187976 ; Planning and designing bus-transit garages: https://trid.trb.org/View/60326

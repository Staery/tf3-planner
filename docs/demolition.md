# Demolition of town buildings: price, reputation, routing model

Status 2026-10-03, TF3 build 40408, save "Staery" (VeryHard), export v7. Code: `tf3planner/townmask.py`
(mask, prices), `tf3planner/routing.py` (module doc item 10), `tf3planner/siting.py`.
Source tags: [game] = game files, [probe] = measured in the running game by the export mod,
[assumption] = our modelling choice.

## 1. Money

| what | value | source |
|---|---|---|
| Removal price of a town building | **≈ 25 175 $ × person capacity + 665 $** (25 k for a 1-person house, 600 k for a level-2 commercial block with capacity 24); 48 probes, every land use × level 1–2 × 1–4 parcels, ratio cost/capacity 25 056–26 895 $ (σ 1.2 %) | [probe] export v7 `townBuildings.bulldozeSamples`: `api.engine.util.proposal.makeProposalData` of a removal proposal per building class, never applied (mod `townbuildings.lua`) |
| All 5 226 buildings of the map | ≈ 760–790 M$ (30 048 capacity) | [probe] × export |
| Median building | capacity 6 → ≈ 152 k$ | export v7 |
| Script value `bulldozeCost = 100` of a town building construction | ×1.5 (`infrastructureCostScale`, VeryHard) = 150 $ — only a negligible part of the real price | [game] `base/content/buildings/townbuildingutil.lua` (construction `cost = 1000, bulldozeCost = 100`), `scripts/construction/construction.script.tl applyConstructionCostScale` |
| `costs.removeField = 200 000` | removal of a farm FIELD (industry fields), not of houses | [game] `base/mod.script.tl` |
| Bulldoze refund 75 % within 60 s | only for the player's own fresh constructions | [game] `mod.script.tl bulldozeRefundFactor/TimeOut` |
| Official help | "Demolishing buildings in towns is expensive and lowers reputation, but is sometimes necessary." | [game] `base/strings/en/LC_MESSAGES/base.mo: HELP_BULLDOZER` |

Where the 25 k$/capacity comes from is engine-side (C++ `con_util_costs`, not scripted). The probes were
taken in 1900; whether it grows with the year (like `treeCostYearMultipliers` 1.0 → 1.5 (1920) → 2.2 (1940)
… 6.0 (2020), which the GUI applies to "costsYearProgression" constructions) is **unknown (Q-D1)** — the
export probes at the current game date, so a later export answers it. The planner currently uses the
1900 price for all years [assumption].

Community data points (TF1/TF2, not TF3): players report "1.5 MIL to remove" a few misplaced buildings
and tens of millions for reshaping districts late game
([steam TF1](https://steamcommunity.com/app/446800/discussions/0/135509823665546183)); the TF3 previews
stress that "knocking down a bunch of buildings to lay track makes residents unhappy"
([gamingtrend](https://gamingtrend.com/news/transport-fever-3-overhauls-city-growth/)).

## 2. Town reputation ("urban care", rating "Reputation")

[game] `game_mechanics/towns/town_util.tl`, `towns.script.tl`:

* `getProposalStats`: for each removed town building `fractionDistrictRemoved += capacity / getLandUsePersonCapacities(town)[landUse]`
  (its share of the town's residential / commercial / industrial district).
* `calculateProposalImpact`: `demolishedTownBuilding = fraction / 0.405` ("100 % for 33 % removed"), added
  to `reasonToPenalty.DemolishedTownBuilding` (capped at 1) and subtracted from the reputation score
  (`addTownAuthorityPenalties`). Not scaled by difficulty: urban-care sensitivity is only an on/off switch
  (`difficulty_util.tl level2sensitivityUrbanCare = {0, 1}`, VeryHard = 1).
* Typical: a capacity-8 house of a 360-capacity district = 2.2 % of the district = **−5.5 % reputation**;
  median over the map −3.7 % per building. Removing a uniform 10 % of a whole town (all three districts)
  ≈ −74 %.
* Recovery: 1/6 of the score per 3 game months (1 month with an active marketing campaign) →
  −100 % recovers in 18 months.
* Consequences: authority = min of the ratings; level = floor(score × 6). Growth multiplier
  `authorityFactor = level / 5` (`getGrowthLevelMult`) → town growth and town XP.
  **Gates** (`getActionAllowed`): level "Poor" (score < 2/6) forbids further `DestroyBuilding`, "VeryPoor"
  (< 1/6) forbids `BuildStation` in that town, "Mediocre" or worse forbids airports.
  So a single project that costs one town > ~65 % reputation blocks further demolition there and can
  block stations — the router reports it per town (`Route.demolition_towns`).
* Trees/rocks/terraforming near a town also cost reputation (−0.25 % per tree, 2 M m³ earthworks = −100 %)
  — not modelled yet.

Money equivalent [assumption]: `RoutingParams.demolition_rating_usd = 2 M$` per 100 % of one town's
reputation (≈ 75 k$ for the median building), used only in the generalized (routing) cost, never in cash.
Rationale: −100 % halts the town's growth/XP for ~1.5 years and risks the gates above; tune when the
growth model (towns.py) can price it.

## 3. Built-up mask (`townmask.py`)

Best source first:

1. **Export v7 buildings** (`buildings.py`): plot = (parcels × 8 m + 4) × (depth × 8 m + 4)
   (`townbuildingutil.lua parcelSize 8, borderSize 2`), centred at the bbox centre, treated as a disc of
   equal area (the exported AABB of a rotated plot is ~2× too large). Price per building = the probe fit
   above × its capacity; penalty = capacity / district capacity (`towns_stats.town2personCapacities`) / 0.405.
2. **Export v6 town streets**: plot cover = street density × 30 m (two rows of plots), ≤ 60 %, only within
   45 m of a town street; synthetic 600 m² buildings; money per m² = 25 k$ × town capacity / built area.
3. **Discs** 10 m × √capacity + 60 m, cover 60 % × (1 − (d/R)²) (denser centre), same pricing.

Rasters (32 m) hold Σ cᵢqᵢ and Σ cᵢqᵢsᵢ (cᵢ = 1/plot area, sᵢ = plot diameter) for q ∈ {count, area, money,
penalty}: a way of width w demolishes in expectation `w·Q + QS` per metre.

## 4. Routing (`routing.py`, `RoutingParams`)

* `demolition=True` (default), `demolition_rating_usd=2e6`, `demolition_usd_per_m2=None` (override:
  price per m² of plot instead of the probe price), `demolition_width=None` (= bed width: rail 6 m, road 8 m).
* Exact (v7): a NEW grid edge hits building i when it passes within ρ = w/2 + plot radius of its centre;
  its share = edge length inside the ρ-disc / full chord of the edge's line through the disc, so a straight
  way through or past a building counts it exactly once over consecutive edges. Without v7: expected
  values from the rasters.
* Nothing is demolished by: existing ways of the kind used as they are, tunnels, water bridges (wet part),
  edges already built by the same plan/line ("shared"). Viaducts are charged like a track (conservative).
* Money is part of the edge **build** cost → `Route.build_cost`, `NetworkMatrix.build`, segment
  `build_cost` — i.e. the planner's cash simulation pays it. The reputation term only enters the
  generalized cost (edge weight / `gen`). Demolition is a cost, never a ban: the router goes through a
  town when that is cheaper overall (e.g. a band of houses with no way around), around it otherwise,
  and along existing streets for free.
* Reported: `Route.demolition_cost` (included in build_cost), `demolition_m` (way length through
  demolished plots), `demolition_buildings`, `demolition_rating`, `demolition_rating_cost`,
  `demolition_towns` {town: penalty}; the same keys on "new" segments and in `route_segments_json`;
  `segment_totals` adds `demolition_cost/_m/_buildings`; matrices `demolition_cost`, `demolition_m`,
  `demolition_rating`. Cache: `_CACHE_VERSION` 3, signature includes the mask hash and all parameters.
* String pulling keeps the grid cells of demolishing edges (a straightened piece could cut through
  houses the grid path went around).

## 5. Siting (`siting.py`)

Stations, depots, maintenance buildings, warehouses and hub stations price every candidate site by the
buildings their footprint (disc: road station 22 m, rail station 45 m, road depot 16 m, rail depot 30 m,
warehouse 25 m) would remove (money + 2 M$ × penalty), in score units of 100 $ against distance to the route
(1 point per metre), slope and catchment preferences. Free ground wins almost always; a site that still
demolishes says so in its reason ("СНОС ~N зд. ≈ X $, репутация: г. … −p %") and in
`Site.demolition_cost / _buildings / _rating` (to be added to the construction cost by the caller).
Passenger stops stand on streets and demolish nothing. `built_depth` uses real plots (12 m margin) with v7.

## 6. Effect on the 2026-10-03 plan (before this change)

63 of the 95 drawn build_line steps had new track/road through houses: ≈ 143 M$ of demolition that the
plan did not pay, and reputation penalties above 100 % in several towns (impossible in the game — the
"Poor" gate would stop it). After the change, e.g. Тетиев farm → Бердичев (rail): 17 houses / 3.0 M$ /
Черногородка −113 % → 2 houses / 0.41 M$ for +0.1 km of track.

## 7. Open questions / limitations

* Q-D1: year scaling of the removal price (probe again in a later year).
* Q-D2: does a viaduct over houses demolish them? (charged now).
* Rail running ALONG a town street (not crossing it) would remove the street — not priced (measured on
  three real routes: < 0.6 % of new length within 8 m of a town street).
* Towns grow: the mask is the state of the export; the plan for later years does not see new houses.
* Town endpoints are still the town centre node (the station stands at the edge, siting.town_station);
  the last metres into the centre are priced although no track is built there — small in practice
  (0–2 buildings on the real examples).

# Plan JSON schema (`data/plan.json`)

The optimizer writes `data/plan.json`; the web map reads it via `GET /api/plan`
(falls back to `data/plan_example.json`). Encoding UTF-8, all money in game currency,
coordinates in world metres (x east, y north, same as `export.json`).

```jsonc
{
  "example": false,                 // optional; true only for the demo file
  "generated": "2026-10-01T14:42:50", // ISO timestamp
  "objective": "Максимизировать деньги к 1920 году", // free text (Russian)
  "summary": { ... },               // free-form key -> number | string | [a, b]
  "steps": [ Step, ... ],
  "cashflow": [ CashflowRow, ... ]
}
```

`summary` is displayed as small tiles (first 6 entries). Known keys get Russian labels:
`start_money`, `end_money`, `final_money`, `total_capex`, `total_income`, `total_profit`,
`lines`, `loans`, `horizon` (`[from_year, to_year]`); `note` (long text) is shown below.
Keys containing `money|capex|income|profit|cost|loan` are formatted as money.

## Step

| field | type | req. | meaning |
|---|---|---|---|
| `id` | string | yes | unique step id, e.g. `"s7"` |
| `year` | int | yes | game year of the action |
| `month` | int 1..12 | no | month inside the year |
| `order` | int | yes | order inside the same year/month |
| `action` | enum | yes | `build_line` \| `buy_vehicles` \| `take_loan` \| `repay_loan` \| `replace_vehicles` \| `upgrade_track` \| `note` |
| `title` | string | yes | short Russian title |
| `details` | string | no | Russian explanation |
| `kind` | enum \| null | yes | `rail` \| `road` \| `water` \| `air` \| `null` (loans, notes) |
| `cargo` | string \| null | yes | cargo key (`logs`, `passengers`, ...) as in `Game.cargo_by_key` |
| `from`, `to` | Endpoint | no | line ends (required for drawing if no `geometry`) |
| `stops` | Endpoint[] | no | intermediate/all stops (drawn as dots; used as path if no geometry) |
| `geometry` | `[[x, y], ...]` | no | route polyline in world metres; preferred for drawing |
| `line` | string | no | line id. On a `build_line` step: the line's own id (e.g. `"L5"`, used as the project id in the UI; defaults to the step `id`). On `buy_vehicles` / `replace_vehicles` / `upgrade_track`: the line they belong to — either that line id or the `build_line` step `id`; the line's geometry is highlighted when the step is selected, and the step appears in the project's timeline |
| `vehicles` | Vehicle[] | no | vehicles bought by this step |
| `loan` | object | no | for loan actions: `{kind, amount, months?, rate?}` |
| `length_m` | number | no | track/road length |
| `cost` | number | yes | one-time spend of this step (construction + vehicles, or repaid principal) |
| `income_per_year` | number | yes | expected yearly revenue added by the step |
| `running_cost_per_year` | number | yes | yearly maintenance + vehicle running costs added |
| `roi_years` | number \| null | yes | `cost / (income - running)`; `null` if not applicable |
| `money_after` | number | yes | cash balance right after the step |

Endpoint: `{"type": "town" | "industry" | "point", "id": <game id or null>, "name": str, "x": num, "y": num}`.
If `x`/`y` are missing but `type`+`id` refer to a town/industry, the UI resolves coordinates from game data.

Vehicle: `{"model": "russian_class_pv_v2.mdl", "name": "Паровоз ПВ", "count": 2, "price": 2262807}`
(`price` is per unit).

Drawing priority for a step's path: `geometry` → geometry of the step referenced by `line`
(such steps are not drawn twice; only highlighted when selected) → `stops` → straight `from`→`to`.
Steps without any of these (`take_loan`, `note`, ...) appear only in the side list.

## CashflowRow

`{"year": int, "money": number, "income": number, "costs": number, "loans": number, ...}` —
end-of-year cash, revenue received during the year (after the company-rank ticket multiplier and
only from lines whose first loaded arrival has happened), running spending during the year
(vehicle running costs + infrastructure upkeep + loan payments; NOT construction), outstanding
loan principal at year end. Optional (planner >= audit 2026-10-02): `capex` (construction and
vehicle purchases in the year), `min_money` (lowest month-end cash in the year), `rank` (assumed
company rank), `ticket_mult` (income multiplier of that rank). One row per year, sorted by year.

## Routes file (`data/routes.json`, optional)

Served by `GET /api/routes` (or `[]` if absent); drawn on the "Маршруты" layer.
Either a list or `{"routes": [...]}` of:

```json
{"name": "Киев — Зубцов", "kind": "rail", "cost": 1234567, "length_m": 4300,
 "points": [[532, 784], [1500, 2500], [2188, 4732]]}
```

`points` may also be named `geometry`, `path` or `polyline`. `kind` as in steps.

## Route dossiers (`data/dossiers.json`, optional; `dossier` field of a step, optional)

A detailed breakdown of every line (route, structures, stations, depots, consist physics, run
timing with acceleration/braking/grades, capacity, money, Russian build instructions), built by
`tf3planner/dossier.py`:

```bash
.venv/Scripts/python -X utf8 -m tf3planner.dossier            # all build_line steps -> data/dossiers.json
.venv/Scripts/python -X utf8 -m tf3planner.dossier --line L7  # one line (merged into the file), --print to echo
```

The UI reads it from `GET /api/dossiers` (the file as-is, `{}` if absent) and shows it in the
"Текущий проект" card ("Подробный разбор") and in a full-size panel over the map. A `build_line`
step may instead carry the same object inline as `"dossier": {...}` (the planner can call
`dossier.build_dossier(game, planner, step, steps)` itself); the inline object wins.

File layout: `{"L1": Dossier, "L2": Dossier, ..., "_meta": {"generated", "plan_generated", "lines"}}`
(keys = the step's `line`, else its `id`; keys starting with `_` are not lines). The UI marks a
dossier stale when `_meta.plan_generated` differs from `plan.generated` or the titles differ.

Dossier (all money final, per game year; times in game seconds, 1 day = 4 s):

| field | meaning |
|---|---|
| `line`, `step_id`, `year`, `month`, `title`, `kind`, `mode`, `mode_ru`, `cargo`, `cargo_info` | header (copied from the step) |
| `legs[]` | one per transport leg (1 for plain lines, 2–3 for multimodal chains), see below |
| `warehouses[]` | multimodal only: `{name, hub, x, y, modules, buffer_units, cost, maintenance}` |
| `totals` | `{capex, income_per_year, costs_per_year, profit_per_year, payback_years}` over all legs |
| `instructions[]` | Russian strings `"N. ..."`, sub-items have extra leading spaces after `"N. "` |
| `warnings[]`, `assumptions[]` | Russian strings |
| `timeline[]` | later steps of the same line `{year, month, action, title, cost}` |
| `error` | set (and nothing else) when the dossier could not be built |

Leg: `role` (`line`/`feeder`/`trunk`/`final`), `mode`, `from`/`to` (`{name, kind, id, node, x, y, z}`),
`route` (`length_m, straight_m, detour, elevation_gain_m, max_terrain_grade, way{...}, structures[{type,
type_ru, name, length_m, at_m, to_m, x, y, place, cost, maintenance, speed_limit_kmh}], cost{track,
bridges, tunnels, earthworks, surcharges, total}, maintenance{...}, free, free_by, polyline`),
`consist` (`label, n_consists, vehicles[], length_m, capacity, mass_empty_t, mass_loaded_t, power_kw,
tractive_effort_kn, kw_per_t_*, performance_loaded/empty[{grade, max_kmh, time_s, dist_m, ok}],
max_wagons`), `timing` (`loaded`/`empty` run summaries from dynamics.py, `load_s, unload_s,
round_trip_s, trips_per_year, headway_s, ruling_grade, terrain_variant, earthworks, grade_variants[],
planner_round_trip_s`), `capacity` (`per_trip, per_consist_year, fleet_year, volume, moved, load_factor,
consists_needed, terminals, rail_layout`), `stations[]`, `depots[]`, `money` (`income_per_unit,
income_per_trip, income_per_year, capex{...}, per_year{...}, profit_per_year, payback_years, npv, plan`),
`delivery` (`time_to_deliver_s, transit_s, ok, to_town`), `chart` (`length_m, terrain[[s,h]],
track[[s,h]], loaded[[s, kmh, limit_kmh]], empty[[s, kmh, limit_kmh]]` — `s` always from the
loading end — `zones[], top_kmh, water_level`), `demolition` (leg total, see below, + `items[]`
`{what, cost, buildings, towns}`; `null` without demolition).

**Demolition of town buildings** (docs/demolition.md; all optional, absent / `null` = nothing demolished).
Demo = `{cost, m, buildings, rating, towns: {town: {buildings, rating_loss}}, text_ru}` (`rating_loss` 0.62 =
−62 % of that town's reputation; `m` = way length through demolished plots; `buildings` may be fractional
without export v7).
* `legs[].route.demolition` = Demo + `segments_ru[]` (per new segment: "участок N (…): снос зданий: N м,
  ≈ X $, ~K зд. (г. …)"), `total_ru` ("в т.ч. снос …"), `route_towns` (routing `Route.demolition_towns`),
  `parts[]` = `{polyline [[x,y]], x, y (middle), length_m, buildings, cost, towns {town: buildings}, segment}` —
  the stretches of the new way that run through demolished plots (the map hatches them). The money is
  already inside `route.cost.total` (also `route.cost.demolition`, informational, part of `surcharges`).
  `route.segments[]` "new" entries additionally carry `demolition_towns`, `demolition_ru`; tour legs:
  `route.segments[].demolition` (Demo without parts).
* `legs[].stations[].demolition`, `legs[].depots[].demolition`, `warehouses[].demolition` = Demo of the
  building's footprint (siting.py); its `cost` IS included in the building's `cost` / `full_cost`.
* dossier `demolition` = project total (all legs + warehouses) + `warn_towns[]` (loss > 50 %), `rule_ru`,
  `plan` (`{cost, m, buildings, towns: {town: loss}}` copied from the planner's `step.demolition`).
* `warnings[]` get the planner's `step.warnings` plus one per town with a loss > 50 % not named there
  ("игра может запретить снос/станции": level "Poor" < 2/6 forbids demolition, "Very poor" < 1/6 new
  stations; recovery 1/6 per 3 game months).

Plan step fields read by the map (written by planner.py): `demolition` `{cost, m, buildings, towns: {town:
loss}}` (towns may also be `{town: {buildings, rating_loss}}`), `warnings[]` (Russian), and on `segments[]` /
`legs[].segments[]` of type "new": `demolition_cost`, `demolition_m`, `demolition_buildings`,
`demolition_rating`; `segment_totals.demolition_*`.

## How the web map uses the plan (year view, buildings)

**Year view (default).** The map shows only what happens in the selected year: `build_line` steps of
that year (bright, every leg in its own style), lines whose `buy_vehicles` / `replace_vehicles` /
`upgrade_track` steps fall in that year (thinner, "+ техника"), and — only with "Показать построенное
ранее" — lines built in earlier years (faint). Future lines are hidden. The "Маршруты" layer
(`routes.json`, off by default) follows the same filter: a route is matched to its `build_line` step by
`name == title`. Focus mode ("Фокус на проекте") overrides the year filter and shows one project.
The year box on the map (◀ year ▶) jumps between years that have work.

**Legs.** A step's `legs[]` with their own `geometry` are drawn separately, styled by `mode`
(`water` dashed blue, `road` orange, `rail` red with ties, `tram` pink; legs with `nodes` of length 1 are
city rings, drawn thinner). Multimodal legs: `{role: feeder|trunk|final, mode, from: {name, key, x, y},
to: {...}, length_m, n_consists, consist, station_from, station_to, geometry}`; an end whose `key` starts
with `"hub:"` is a transfer point (warehouse).

**Buildings layer ("Здания").** Positions and sizes, best source first:
1. the dossier of the build step (matched by `step_id`, then by line id): `legs[].stations[]`
   (`kind`, `building`, `x`, `y`, `platform_m`, `terminals`, `catchment_m`, `cost`, `free_by`),
   `legs[].depots[]` (`what`, `x`, `y`, `radius_m`, `pool`, `count`, `cost`, `free`),
   `legs[].route.structures[]` (bridges / tunnels with `x`, `y`), `warehouses[]`. A dossier whose
   stations are the generic `kind: "multi"` / `building: "?"` is ignored for positions;
2. multimodal plan legs: a station at both ends of every leg (type from `mode` + `station_from/to`),
   a warehouse at every `hub:` end (modules/buffer parsed from `details` "Склады: …", or taken from an
   optional step field `warehouses[]` / `meta.warehouses[]` = `{name, hub, x, y, modules, buffer_units,
   cost, maintenance}` if the planner provides it), an approximate depot per leg;
3. passenger-network legs (`title`, `mode`, `nodes`, `geometry`, `details`): stops at both ends of
   intercity legs, city-ring stops spread evenly along the ring (approximate);
4. otherwise `stops[]` or `from`/`to`, snapped to the nearest point of the geometry, + approximate depot.
`project_kind: "industry_build"` adds a prospecting-site marker at the first geometry point.
Passenger lines (`cargo == "passengers"` or `project_kind` `passenger` / `workers`) get bus / rail
passenger stop icons. Default catchment radii when the dossier has none: rail / road cargo station
175 m, bus / tram stop 160 m, harbor 200 m.

URL parameters (debug / sharing): `?year=1903`, `?project=L22`, `?focus=0`, `?built=1` (show earlier
lines), `?tab=projects|buildings|steps`, `?layers=towns,industries,plan,buildings,routes,chains`,
`?hoverb=warehouse` (show the tooltip of the first visible building of that type — for screenshots).

## Existing roads and tracks (`GET /api/network`, read-only)

Map layer "Дороги и пути (из игры)" (`?layers=…,network`, on by default): streets and tracks already in
the game, from the `network` graph of `data/export.json` (export v6, mod `network.lua`), loaded with
`tf3planner.network.TransportNetwork.from_export`. Drawn under all plan layers. Response (cached per
export.json mtime, gzip when accepted, ~1 MB raw / ~0.25 MB gzip for ~45k edges):

```jsonc
{
  "available": true,
  "hash": "20f1cb21dc0fef13",          // TransportNetwork.hash()
  "edges": 45254, "nodes": 28877, "failed_edges": 0,
  "lines": 38738, "points": 82223,      // polylines / points after merging and simplification
  "bounds": {"minX": …, "minY": …, "maxX": …, "maxY": …},
  "km": {"country": 212.6, "town": 1907.1, …},          // edge length per class, km
  "classes": {                           // class -> structure -> polylines [x0, y0, x1, y1, …] (int metres)
    "country": {"normal": [[…], …], "bridge": [[…]], "tunnel": [[…]]},
    "town": {…}, "highway": {…}, "player_road": {…},
    "track_player": {…}, "track_other": {…}, "tram": {…}, "other": {…}
  }
}
```

Classes: `player_road` = road edges owned by the player; `highway` = template `/street/highway/`;
`town` / `country` = TransportNetwork owner; `track_player` / `track_other` = rail (other = AI/industry/
depot tracks); `tram` = tram-only tracks; `other` = airport runways/taxiways. Edges of one class and
structure are merged into chains through nodes of degree 2, curves are approximated from the exported
tangents (cubic Hermite, 2..8 segments, ≤ ~0.75 m chord error), then Douglas-Peucker (1 m) and rounding
to 1 m. Without a network graph (old export, or the mod's network section failed):
`{"available": false, "reason": "…"}` — the map shows "Дорожная сеть ещё не выгружена — перезапустите
игру и нажмите экспорт" and disables the toggle.
Dev/test only: env `TF3_NETWORK_SOURCE=<path to export-like JSON>` makes the endpoint read the network
from that file instead of data/export.json (map data stays from data/export.json).

**Bridges / tunnels.** Dossier `route.structures[]` give the midpoint (`x`, `y`) and `length_m`; the map
builds a straight span of that length centred on the midpoint along the dossier `route.polyline`, splices
the span ends into the (down-sampled) plan geometry and draws a deck: bridge / viaduct = dark outline +
light deck with the line on top, tunnel = dark dashed section with portal bars at both ends. A small
"М"/"Т" marker at the midpoint opens the tooltip.

**Demolition.** Stretches of a step's new way that demolish town buildings get a red hatched band and a
"снос" marker at their middle (a red dot when zoomed out; type `demolition` on the "Здания" layer, tooltip:
buildings, cost — already in the route price — and the project's per-town reputation loss). Source: the
dossier `legs[].route.demolition.parts[]`; without a dossier the plan's "new" segments with
`demolition_buildings` are hatched whole (approximate). Stations / depots / warehouses with a dossier
`demolition` show it in their tooltip and list row. The project card ("Снос городских зданий"), the year box
("снос ~N зд. · X", ⚠ when a town loses > 50 %), the "Здания этого года" tab and the line tooltip show the
totals (dossier `demolition`, else `step.demolition`) and the planner's `step.warnings`.

**"Build similar" guide (focus mode).** Corridor band ±125 m, the line coloured by track gradient
(dossier `chart.track`, averaged over ±50 m: ≤1 % green, 1–2 % yellow, 2–4 % orange, >4 % red), km
ticks, numbered key waypoints (stations, bridge/tunnel ends, the biggest turns, ~12 max) listed in the
project card with km and coordinates. Turn reasons come from `GET /api/chords?q=x1,y1,x2,y2;...`
(read-only terrain probe: `{water_frac, max_h, min_h, mean_h}` along each straight chord).
`?at=x,y[,scale]` centres the view (scale in px per metre).

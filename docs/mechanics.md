# Transport Fever 3 — logistics mechanics reference (for the optimal planner)

Status: researched 2026-10-01 against the TF3 release install (`<Steam library>\steamapps\common\Transport Fever 3`).

## Conventions

* **[TF3-code: X]** — read directly in the game files. Paths are given as `<archive>.zip/<path inside archive>` relative to
  `base/content/` (e.g. `warehouses.zip/warehouses/warehouse.tl`), or as `api/tealdef/...` / `base/tealdef/...` for the
  typed API definitions, or `strings/en/LC_MESSAGES/base.mo:<KEY>` for the official English UI/help texts (the help
  texts are written by the developers and describe engine behaviour that is not visible in Lua).
* **[TF3-web: url]** — official Urban Games pages about TF3.
* **[TF2: url]** — Transport Fever 2 sources; used only to fill gaps, always marked "TF2 (likely same in TF3)".
* **[derived]** — my arithmetic on top of code values. **[unknown]** — engine-side (C++), not visible in scripts; needs
  in-game measurement (see "Open questions").
* Money values are the raw values in the files ("Medium" difficulty = scale 1.0). Difficulty scales are listed in §12.1.
* **Time base.** One in-game day = 4 s of simulation time, one year = 365.25 days = 1461 s
  (`secondsPerDay = 4` in `base.zip/base/model_metadata_util.lua:addCostMetadata`; matches the planner's measured
  `yearDurationMs`). Engine helpers: `api.util.getDefaultDayDuration/YearDuration/MonthDuration`
  [TF3-code: api/tealdef/api/util.d.tl]. Note `economy.zip/economy/cargotype.script.lua` still says
  `millisPerDay = 2000` and `mod.script.tl` says `extendTimeSpan = 360000 -- 180 days` (legacy TF2 comments; not used by
  the TF3 industry script, which works in years).

---

## 1. Cargo flow

### 1.1 Cargo has no destination; it waits in stocks, not at stations (NEW in TF3)

* "Cargo no longer have a destination set when they are first produced. Instead they can be picked up at any time and
  shipped anywhere within your network." "Cargo waits at origin points rather than stations until collection occurs."
  [TF3-web: https://www.transportfever3.com/news/dev-blog-episode-2-industries-and-cargo-2/]
* Engine data model confirms it: a cargo item (`SimCargo`) has `sourceEntity`, `targetOrPickupEntity`, `startTime`
  (produced), `pickupTime`, `deliveryExtensionDuration` and a list of `pickupPoints {vehicle, line, position, carrier}`
  (one per leg). Waiting cargo is attached to a *stock* (`SimEntityAtStock {stock, arrivalTime}`), not to a terminal
  [TF3-code: api/tealdef/api/engine.d.tl, records SimCargo, SimEntityAtStock, SimEntityAtVehicle].
* Stocks exist on industries (`INPUT_STOCK`/`OUTPUT_STOCK`), town buildings (`INPUT_STOCK` per demanded cargo) and
  warehouses (`STORAGE_STOCK`) [TF3-code: industries/*.con.lua, buildings/townbuildingutil.lua, warehouses.zip/warehouses/warehouse.script.lua].
* Consequences for routing:
  * A vehicle **loads** at a stop from any compatible OUTPUT/STORAGE stock inside the stop's cargo catchment, and
    **unloads** at a stop into any compatible INPUT/STORAGE stock in the catchment that has free space. Per-stop
    options: load/no-load per cargo, max fraction of capacity per cargo, "Unload only if stocks have capacity" vs
    `forceUnload` ("Unload even if stocks are full" → surplus is destroyed), `destroyForConfigChange`,
    `destroyForRefresh` (destroy cargo that made a full round trip back to its pickup stock)
    [TF3-code: api/tealdef/api/engine.d.tl Line.StopConfig; gui.zip/gui/line_vehicle_mgmt/cargofilter_window.tl].
  * Cargo that finds no unload stock stays in the vehicle and rides round; the game raises "Cargo Unload Problem:
    {n} cargo of {vehicle} was not unloaded on line" [TF3-code: game_mechanics.zip/.../notifications/types/cargo_rtc_warning.script.tl].
  * Line validation messages: "A cargo type cannot be unloaded anywhere, despite being loaded at stop N", "Configured
    cargo type cannot be loaded/unloaded at stop" [TF3-code: notifications/types/line_warning.script.tl, line_station_warning.script.tl].
* Industries start producing "as soon as all required materials are in stock" [TF3-code: base.mo:HELP_WINDOW_INDUSTRY].
  A primary industry whose output stock is full cannot store new production (planner calibration, consistent with
  `StockList.thrownAwayCargo` and `getStockListsWithThrownAwayCargo` [TF3-code: engine.d.tl, engine/util.d.tl]).
  So an unserved chain produces nothing useful; there is no "chain must be complete for production to start" rule for
  primary industries — they produce into their stock until it is full (coal mine output stock = 400 units).
* Industry → town flow: the town's commercial/industrial buildings each have an INPUT stock per demanded cargo; the
  layer help says "The demanded amount of cargo must be supplied to all buildings in the industrial and commercial
  districts of a town" [TF3-code: base.mo:HELP_LAYER_CARGO_FLOW]. Only buildings inside an unloading stop's catchment
  can receive.

### 1.2 Transfers between lines

* **TF3: a warehouse is required to transfer cargo** — "To transfer cargo between different means of transport, a
  warehouse must be built." [TF3-code: base.mo:TIP_CONSTRUCTION_12]; "Warehouses are used to transfer cargo between
  different modes of transport ... Build warehouses within a station's catchment area and connect them with the
  appropriate lines. Cargo will then be transferred and stored automatically." [TF3-code: base.mo:TUTORIAL_WAREHOUSE_GUIDE_PARAGRAPH].
  Because stations have no cargo storage (§1.1), a "hub" without a warehouse cannot hold cargo between two lines,
  even for the same mode (inference from the data model; verify in game — open question Q1).
* TF2 (different, do NOT reuse): cargo had a fixed destination, could transfer at shared stations, and goods "jump"
  between stations whose catchments overlap [TF2: https://steamcommunity.com/app/1066780/discussions/0/3855581440177894075].

### 1.3 Payment

```text
income_per_unit_per_leg = incomeFactor(cargo) * difficultyFactor * travelDistance * rankTicketMultiplier
travelDistance          = |Δpos|_3D + 8 * max(Δz, 0)        # climbing is paid 8x extra
incomeFactor(cargo)     = initialPrice + (finalPrice - initialPrice) * 0.25      # constantFn
                        = 4 + (3.2 - 4) * 0.25 = 3.8 $/m for EVERY base cargo type
passengers              = 62.5*0.001*35*1.5*1.1 = 3.609 $/m * (0.5 + vehiclePriceFactor)
                          (no base vehicle sets priceFactor -> default 0.5 -> multiplier 1.0)
```
[TF3-code: economy.zip/economy/cargo_income.script.lua; economy.zip/economy/cargotype.script.lua; cargos/*/…/*.cargo.lua;
`difficultyFactor` = `advancedOptions.cargoIncome`/`passengerIncome` = 1.0 on all four presets, base.zip/base/mod.script.tl + difficulty_util.tl]

* All 36 cargo types use `constantFn` with initialPrice 4 / finalPrice 3.2 → **3.8 $/m/unit, identical for all
  cargos** [TF3-code: cargos/*.cargo.lua]. Cargo choice therefore does not change price per unit-metre; only volume,
  delivery deadline and vehicle class matter.
* Sanity check from the vehicle balancing code: "averageCargoIncomePerKm = 3700 -- passengers 3600, cargo 3800"
  [TF3-code: base.zip/base/model_metadata_util.lua].
* `rankTicketMultiplier` (TF3 "inflation") applies to **cargo and passengers**: the company script answers the
  `OnCalcTicketPrice` event from both `TransportVehicleSystem` (cargo) and `SimEntityAtVehicleSystem` (passengers)
  with `multipliers[0] = ticketPriceMultiplier` ("multipliers[0] is used for all") [TF3-code: game_mechanics.zip/game_mechanics/company/company_growth.script.tl]. Details §12.2.
* Per-leg vs origin→destination: the engine passes `posDifference` (not visible in Lua). Evidence for per-leg:
  `SimCargo.pickupPoints` stores one pickup position per leg, `CalcTicketPriceCargoEvent {vehicleEntity, lineEntity,
  simEntity, stockListEntity, basePrice, distance}` is fired per vehicle unload, and vehicles carry
  `unloadPendingIncome` ("total income pending to be unloaded") [TF3-code: engine.d.tl, base/tealdef/scripts/gamescript.d.tl].
  TF2 (likely same in TF3): paid per leg for the straight-line distance between pickup and drop-off of that leg;
  detours earn nothing [TF2: https://steamcommunity.com/app/1066780/discussions/0/3783625216798366301]. There is no
  explicit transfer penalty in the scripts.
* Lateness does **not** change payment (no time argument is used by `constantFn`; coordinator also verified via
  `finances_cargo_lifetime.tl`). Lateness only affects town ratings (§1.4).
* TF2's formula `(300 + distance) * basePrice * ... ` with a speed bonus is **not** used in TF3 (no speed term in the
  TF3 income script) [TF2: https://steamcommunity.com/app/1066780/discussions/0/3783625216798366301].

### 1.4 Delivery deadline ("on-time"), spoilage

* Each cargo type has `timeToDeliverInSeconds` ("Lifetime of cargo in seconds") [TF3-code: api/tealdef/api/type.d.tl CargoType]:

| cargo | s | days (4 s/day) | | cargo | s | days |
|---|---|---|---|---|---|---|
| meat | 900 | 225 | | bricks, clay, coal, crude_oil, logs, planks, sheet_metal | 3000 | 750 |
| beverages, fish, vegetables | 1125 | 281 | | iron_ore, sand, steel, stone | 4500 | 1125 |
| grain | 1286 | 322 | | cement, dyes, fuel, glass, machines, sawdust, tools, vehicles | 2250 | 562 |
| clothes, fabric, fertilizer, plastic, tires, wool | 1500 | 375 | | books, chemicals, furniture, paper, rubber, tinned_food | 1800 | 450 |

  The effective limit is `api.engine.util.stock.getAdjustedDeliveryTime(cargo)` — "The expected delivery time changes
  when the game progresses over multiple years" [TF3-code: base.mo:HELP_COMPANY_DELIVERYTIME;
  game_mechanics/finance/finances_cargo_lifetime.tl]. The adjustment curve is engine-side [unknown].
* Specialized warehouses extend the limit ("specialized warehouses extend the time limit for the delivery"
  [TF3-code: base.mo:MISSION_7_GUIDE_DELIVERY_PARAGRAPH]; "specialised warehouses conserve delivery time"
  [TF3-web: https://www.transportfever3.com/news/dev-blog-episode-2-industries-and-cargo-2/]) — stored per item as
  `SimCargo.deliveryExtensionDuration` [unknown amount].
* Whether the clock starts at production (`startTime`) or pickup (`pickupTime`) is engine-side; both are stored.
  Assume production (conservative) — open question Q3.
* "Late" cargo is still delivered and paid; it is counted as late in `getTownDeliveriesStats` (window = ½ year) and
  feeds the town's **Delivery** rating (§9.4). Deliveries to an *industry* are attributed to the industry's closest
  town (`getConstructionClosestTown`), so late industrial deliveries also hurt that town [TF3-code: game_mechanics/towns/towns.script.tl, OnCalcTicketPrice handler].
* Spoilage/destruction: cargo is destroyed only by (a) full output stocks (thrown away), (b) `forceUnload` into full
  stocks, (c) `destroyForRefresh`/`destroyForConfigChange`. The tutorial warns that an over-fed downstream line lets
  "cargo accumulate and spoil before it is transported" [TF3-code: base.mo:TUTORIAL_LINE_STATISTICS_GUIDE_PARAGRAPH] —
  i.e. it becomes late (bad rating) and/or overflows stocks. No script destroys cargo for being late.

---

## 2. Warehouses (new in TF3)

Files: `warehouses.zip/warehouses/{warehouse.tl, warehouse.script.lua, warehouse.con.lua, modules.script.lua,
wh_*.module.lua, warehouse_era_{a,b,c}_{universal,liquid,bulk,flatbed,goods}.metacon.tl}`.

* **What it is:** a modular construction with an entrance (snaps to a STREET) and 32×32 m modules (slot spacing
  16 m each side, grid of 8 m cells, max ±20 cells from the entrance). Each module adds one `STORAGE_STOCK`
  [TF3-code: warehouse.script.lua `addSlot`, `addStock`].
* **Capacity:** every module (any type) adds **500 units** [TF3-code: modules.script.lua `result.addStock(..., 500, 2.0, ...)`].
  * Universal modules are merged into ONE mixed stock (`mixedTypes = true`, all cargo except passengers,
    `loadSpeedModifier = 1.0`) whose capacity is 500 × number of universal modules.
  * Each specialized module (LIQUID/BULK/FLATBED/GOODS) is a separate stock, single cargo type at a time
    (`mixedTypes = false`, can be pinned to one cargo in the UI), `loadSpeedModifier = 2.0` ("Specialized Warehouse"
    load boost, shown as `stockCompatibility` in the vehicle load modifiers) [TF3-code: warehouse.script.lua; gui.zip/gui/main/cargo_react_util.tl].
  * Achievement "Store over 10,000 cargo in a single warehouse" → ≥ 20 modules are possible [TF3-code: base.mo:ACH_WAREHOUSE_10K_DESC].
* **Cost** (construction base cost = 0; you pay the modules) [TF3-code: wh_*.module.lua, warehouse.tl]:

| module | price | upkeep/yr | capacity | load modifier |
|---|---|---|---|---|
| universal | 150 000 | 25 000 | +500 (shared mixed stock) | 1.0 |
| liquid / bulk / flatbed / goods | 300 000 | 50 000 | +500 (own stock) | 2.0 |

  The menu entries (one per class and era: era A 1900–1940, B 1940–1980, C 1980+) display
  `cost = 25 000·6·(1|2)·(1|3|9)` and upkeep `25 000·(1|2)·(1|3|9)` (×2 specialized, ×3 era B, ×9 era C), but the module
  files have fixed prices (150k/300k, available from 1850) and no script applies a year multiplier — see Q8.
  Difficulty scale (×1.5 on VeryHard) applies (module prices are scaled in `post_run_util.applyCostScale`).
* **Emissions:** each module noise 20, pollution 50; + base 25 (universal-only) or 50 (any specialization); doubled when
  "crowded" [TF3-code: warehouse.script.lua `terminateConstructionHook`]. Counted as a separate emitter category
  "warehouses" in the town pollution breakdown [TF3-code: towns/town_util.tl].
* **How it works with lines:** a warehouse is just a stock. A stop whose cargo catchment covers the warehouse can
  unload into it (if it has space for that cargo) and load from it. "While building, the catchment areas of stations
  are displayed that can store cargo in the warehouse" [TF3-code: base.mo:HELP_CONSTRUCTION_WAREHOUSES]. It is a hub
  and a buffer: "For transfers between lines, or ongoing storage" [TF3-web: dev blog ep.2]; "establish warehouses
  independent of immediate demand, storing goods" [TF3-web: https://www.transportfever3.com/de/news/first-look-episode-3-infrastruktur/].
  Typical chain: industry → (truck line A) → warehouse ← station → (train line B) → warehouse/consumer.
* **Pull vs push:** nothing pulls cargo out of a warehouse by itself. Towns/industries only receive what a line
  unloads at a stop covering their buildings. A warehouse that sits inside the catchment of a town stop does NOT feed
  the town unless a line carries the cargo from a stop near the warehouse to a stop near the buildings (warehouse is a
  stock, not a consumer). Inference from the stock model; verify (Q2).
* **Limits / caveats:** 500 units per module; universal stock is shared between all cargos (one cargo can fill it);
  liquid modules only take LIQUID-class cargo etc. (cargo classes per type in §4.4). Leg payment is earned for each
  leg (§1.3), so a warehouse hub does not lose revenue but adds a second loading/unloading and waiting time to the
  delivery clock (offset by the specialized-warehouse extension).
* Unknown: whether a vehicle may unload into the same warehouse it loaded from (round trip rule
  `SimEntityAtVehicle.roundTripCounter`), and which of several stocks in a catchment is chosen
  (`TransportVehicle.lastStationStockIndex` suggests round-robin) [TF3-code: engine.d.tl].

---

## 3. Catchment areas

* TF3 catchment = **union of circles (cylinders) around module positions** — "A catchment area is a cylinder around a
  position with a specific radius" [TF3-code: api/tealdef/api/type.d.tl CatchmentArea];
  `constructionutil.addCatchmentCircles` places one circle per module that has `catchmentPassenger`/`catchmentCargo`
  metadata, at that module's slot position [TF3-code: scripts.zip/scripts/construction/constructionutil.lua].
  Passenger and cargo catchments are separate ("green = in range for passengers, blue = for cargo, white = both"
  [TF3-code: base.mo:TUTORIAL_BUILD_ROAD_STOP_1_GUIDE_PARAGRAPH]).
* TF2 was different (walking distance along roads from station connectors) [TF2: https://steamcommunity.com/app/1066780/discussions/0/3855581440177894075].
* A station with nothing in range is flagged "The station is not functional yet, as no warehouse, industry or town
  building is in the catchment area" [TF3-code: notifications/types/station_useless.script.tl].
* Industries/towns/warehouses need an **in-catchment station**; a road connection is needed for the station itself and
  for the industry construction (engine check `getNoRoadConnectionProblems` → "no road connection" notification)
  [TF3-code: engine/util.d.tl; notifications/types/noroadconnection.*]. Exactly which point of an industry must lie in
  the circle (centre? any lot? the stock model?) is engine-side → Q4.
* Industries also have an **integrated loading dock** (auto-generated truck terminals, capacity 10, from the model's
  `lane_terminal_*` lanes) — "cheapest entry point but slower than any of the alternatives", so placing your own cargo
  station in the industry's catchment loads faster [TF3-web: dev blog ep.2; TF3-code: industries/industryutil.lua `addTerminals`].

Radii [TF3-code: stations/**/*.module.lua, *.con.lua, *.script.lua]:

| building / module | passenger r (m) | cargo r (m) |
|---|---|---|
| Road small stop (all eras, one/two-sided) | 160 | 160 (unload only) |
| Modular road station: entrance / exit / entrance_exit module (each) | 175 | 175 |
| Rail cargo main building 1 / 2 / 3 | — | 150 / 175 / 200 |
| Rail cargo side building 1 / 2 / 3 | — | 50 / 60 / 70 |
| Rail passenger main building 1 / 2 / 3 | 150 / 175 / 200 | — |
| Rail passenger side building 1 / 2 / 3; stairs (side entrance) | 50 / 60 / 70; 100 | — |
| Harbor dock 25×25 / 50×50 / 100×50 (shared pax+cargo terminals) | 150 / 250 / 250 | same circle (harbor terminals are mixed pax+cargo) |
| Underground (metro) station | 200 | — |
| Airfield main building | 250 (station area 300) | 250 |
| Airport main building | 300 | 300 |
| Helipad / heliport | 100 / 175 | 100 / 175 |
| Depot / maintenance "maintenance area" (not a catchment for cargo) | road depot 400, road maint. 600, tram depot 400, rail depot 800, rail maint. 800, water depot 1000, water maint. 1400, air hangars 550, heliport 800 | |

---

## 4. Station capacity, loading speed

### 4.1 Terminals and platforms

* Rail modular station: platform/track modules are **40 m** segments (`platformLength = 40`) on a grid j ∈ [−10, 10]
  → max **21 segments = 840 m** per track; template lengths 80/120/160/240/320 m, 1–8 tracks
  [TF3-code: stations/rail.zip/rail/modular_station/modular_station.script.lua, trainstationutil.lua, modular_station.con.lua].
* Road modular station: 1, 2 or 4 terminals in the template; each terminal is 2 platform modules; cargo terminal
  sections counted 10 m per module; terminals and entrances can be added later [TF3-code: stations/street.zip/street/modular_street_station/*].
* Small road stops: passengers + **cargo unload only** (`cargoLoad = false, cargoUnload = true`), terminal capacity
  50 people. "Small road stops can only be used to unload cargo. To load cargo, a large station is required."
  [TF3-code: small_stops.script.lua; base.mo:TUTORIAL_TRUCK_STATION_GUIDE_PARAGRAPH].
* Harbor: each pier = one terminal (template 1/2/4 terminals); harbor terminals are mixed passenger+cargo; "Large ships
  can only dock at large harbors" [TF3-code: stations/water.zip/water/harbor_modular.script.lua; base.mo:TUTORIAL_HARBOR_SHIPYARD_GUIDE_PARAGRAPH].
* Passenger and cargo terminals are separate (rail/road); a cargo vehicle cannot use a passenger stop and vice versa
  [TF3-code: notifications/types/line_station_warning.script.tl].
* Passenger waiting capacity = number of 1.2 m × 1.2 m cells on waiting-area lanes + station pool
  (`moreCapacity` modules) [TF3-code: constructionutil.lua addStationCapacities]. Cargo does not wait at stations
  (§1.1), so cargo throughput is limited by stock capacities and loading speed, not by station storage.

### 4.2 Loading-speed modifiers

* Terminal `cargoTransferSpeeds`: specialized cargo platform (rail and road) `loadSpeedModifier = 2`, universal = 1,
  passengers = 1 [TF3-code: modular_station.script.lua L1042, modular_terminal.script.lua L2956].
* Vehicle-side modifiers per cargo type (`TransportVehicle.LoadSpeedModifiers`): `vehicleCompatibility` (specialized
  terminal boost), `vehicleIncompatibility` — wrong class on a specialized platform loads at **0.25** speed
  ("severe penalty"), `vehicleOverlength` — the part of the train longer than the platform loads at **0.25** speed
  ("trains that are too long will load and unload significantly slower"), `stockCompatibility` — specialized warehouse
  ×2 [TF3-code: engine.d.tl; gui.zip/gui/main/cargo_react_util.tl `IncompatibilityModifier = 0.25`,
  `OverlengthModifier = 0.25`; base.mo:TUTORIAL_BUY_TRAIN_GUIDE_PARAGRAPH, TUTORIAL_TRUCK_STATION_GUIDE_PARAGRAPH].
* Specialized vehicles also load faster: "When transporting only a single cargo type, specialized vehicles provide
  faster loading and unloading than general-purpose vehicles" [TF3-code: base.mo:TUTORIAL_TRUCK_GUIDE_PARAGRAPH];
  vehicle `loadSpeed` values in models range 1.5–35 (trucks 2–8 typical; e.g. Benz 1912 box truck `loadSpeed = 6`,
  capacity 48 GOODS) [TF3-code: vehicle/truck/benz1912/*.mdl].
* All cargo `loadSpeedFactor = 0.0625`, passengers 1.0 [TF3-code: cargos/*.cargo.lua].
* Plausible model (TF2-style, unverified): `units_per_second = loadSpeed × cargo.loadSpeedFactor × terminalModifier ×
  stockModifier × penalties`; e.g. Benz truck universal: 6 × 0.0625 = 0.375 u/s → 48 units in 128 s (32 days)
  [derived; Q5]. Plus fixed `arrivalDelay`/`departureDelay` per vehicle (planner uses 2 s + 2 s).

### 4.3 Station emissions

Station emission index rises from 1 to 2 as waiting capacity fills (`emissionIndex = 1 + used/capacity`), depots
likewise with maintenance-pool usage [TF3-code: game_mechanics/emission/emissions.script.tl].

### 4.4 Cargo classes (decide which specialized platform/warehouse/vehicle fits)

* BULK: cement, clay, coal, fertilizer, grain, iron_ore, sand, sawdust, stone
* LIQUID: chemicals, crude_oil, dyes, fuel, rubber
* FLATBED: logs, machines, planks, sheet_metal, steel, vehicles
* GOODS: beverages, books, bricks, clothes, fabric, fish, furniture, glass, meat, paper, plastic, tinned_food, tires, tools, vegetables, wool
* Every cargo is also UNIVERSAL. [TF3-code: cargos/*.cargo.lua `cargoClasses`]

---

## 5. Depots and vehicle maintenance (new TF3 maintenance model)

* Depots: buy/sell/park vehicles **and** maintain vehicles inside their **maintenance area**; separate cheaper
  "Maintenance Buildings" exist for road, rail and water [TF3-code: base.mo:HELP_CONSTRUCTION_RAIL_BUILDINGS,
  HELP_CONSTRUCTION_ROAD_BUILDINGS, TUTORIAL_BUILD_ROAD_DEPOT_GUIDE_PARAGRAPH]. Vehicles need not enter: proximity is
  enough (developer answer) [TF3-web: https://www.transportfever3.com/de/news/first-look-episode-3-infrastruktur/];
  "any vehicle within its maintenance radius will be maintained" [TF3-code: base.mo:MISSION_3_MAINTENANCE_PARAGRAPH].
* **Maintenance pool:** "Each building can only maintain a certain number of vehicles at a time. If more capacity is
  required, additional buildings can be placed." [TF3-code: base.mo:HELP_WINDOW_DEPOT]. Each vehicle is assigned one
  `maintenanceStation`; depot tracks `maxPoolUsage`/`averagePoolUsage` [TF3-code: engine.d.tl VehicleDepot, TransportVehicle].
  This is a limit on simultaneously-maintained vehicles, NOT a limit on vehicles bought (no purchase limit found;
  depots have a `capacity` field — 5 rail/water, 8 tram, 10 road — whose meaning is not documented, probably
  parking/queue slots, Q6).
* Pool sizes and radii [TF3-code: depots/*/…/*.script.lua, stations/air/**]:

| building | pool | maintenance radius (m) | cost / upkeep (script) |
|---|---|---|---|
| Road depot | 12 | 400 | 180 000 / 30 000 |
| Road maintenance building | 16 | 600 | 60 000 / 10 000 |
| Tram depot | 12 | 400 | 360 000 / 60 000 (menu says 120 000 / 20 000, Q8) |
| Rail depot | 6 | 800 | 600 000 / 100 000 |
| Rail maintenance building | 12 | 800 | 240 000 / 40 000 |
| Ship depot | 8 | 1000 | 600 000 / 100 000 |
| Ship maintenance building | 12 | 1400 | 240 000 / 40 000 |
| Airfield / airport hangar module | 16 | 550 | 600 000 / 100 000 ; 900 000 / 150 000 |
| Heliport | 4 | 800 | 300 000 / 50 000 |

* **Condition** (`maintenanceState`, 0..1 per vehicle part) decays outside maintenance; 5 display levels of 0.2;
  penalties start below 0.8 (`maintenanceLevel = floor(state·5)+1`). Penalties (engine functions
  `getMaintenance{Emission,Comfort,TopSpeed,RunningCost}Penalty`): more noise/pollution, lower comfort, lower top
  speed/power (`topSpeedMaintenanceFactorPenaltyPerLevel` per vehicle), higher running costs
  [TF3-code: gui.zip/gui/line_vehicle_mgmt/vehicle_store_util.tl; engine/util.d.tl]. Notification at state ≤ 0.2;
  in mission 3 vehicles break down at 0 % [TF3-code: notifications/notifications.script.tl; base.mo:MISSION_03_TASK_FATAL_MAINTENANCE_PARAGRAPH].
* Strength of the penalties = difficulty `vehicleMaintenanceEffectScale`: 0 / 0.5 / 1 / 1.5 / 2 (Easy=0.5, Medium=1,
  Hard=1.5, VeryHard=2.0), applied separately to emissions, performance and running costs
  [TF3-code: base.zip/base/mod.script.tl, difficulty_util.tl].
* Decay speed per vehicle: `maintenanceFactor` (=2 for 62 vehicle models, default 1) and `lifespan` (days, e.g.
  21 915 = 60 years) — exact decay/restore rates are engine-side [unknown, Q7]. "For vehicles, a high number of
  maintenance buildings leads to lower emissions, but causes higher costs" [TF3-code: base.mo:TIP_VEHICLES_6].
* TF2 (do not reuse): maintenance was a global funding slider, running cost = 1/6 of price
  [TF2: https://steamcommunity.com/app/1066780/discussions/0/3963662189542369520].

---

## 6. Track and road capacity

* Rail signals are **path signals**: "A train will only drive, if it can exclusively reserve the entire path from its
  current position to the next signal or station." A normal path signal lets opposing trains pass when necessary;
  a one-way signal blocks oncoming traffic completely [TF3-code: base.mo:HELP_CONSTRUCTION_RAIL_TOOLS, HELP_WINDOW_SIGNAL].
  So bidirectional single track with passing loops works (TF2 could not do this well
  [TF2: https://steamcommunity.com/app/1066780/discussions/0/2569816492288466759]).
* Passing loops: one signal per direction on the two loop tracks; longer loops can carry several signals so several
  trains wait in the same direction — "The more signals you add, the more trains can travel on your line."
  [TF3-code: base.mo:SIGNALS_1_GUIDE_PARAGRAPH, SIGNALS_2_GUIDE_PARAGRAPH].
* Per-line reservation priority (Standard 1.0 / High 2.0 / Very High 3.0) [TF3-code: gui.zip/gui/entity_window/line/line.tl; engine.d.tl Line.reservationPriority].
* Track types [TF3-code: infrastructure/track.zip/track/*/*.street_template.lua]:

| track | years | cost $/m | upkeep $/m/yr | v_max |
|---|---|---|---|---|
| simple | 1850+ | 60 | 10 | 27.8 m/s (100 km/h) |
| standard | 1930+ | 180 | 30 | 44.4 m/s (160 km/h) |
| high speed | 1980+ | 540 | 90 | 97.2 m/s (350 km/h) |
| catenary | 1921+ (`trackCatenaryYearFrom`) | +30 % of track | | electric trains need every track and station on the route electrified |

  [TF3-code: base.zip/base/mod.script.tl `railroadCatenary = .3`, `trackCatenaryYearFrom = 1921`; base.mo:ELECTRIFICATION_GUIDE_PARAGRAPH].
  Station track modules: 30 000 per 40 m segment, upkeep 5 000 [TF3-code: mod.script.tl postRunFn].
* Roads (1900 era, `*_old_*`, until 1940): town small/medium/large 60/120/180 $/m (11.1/11.1/16.7 m/s), country
  small/medium/large 60/120/180 $/m at 16.7 m/s (60 km/h); 1940+ "new" roads cost 3× (country new 180/360/540, 80 km/h;
  highways 1940+ 180/360/540 at 100–120 km/h) [TF3-code: infrastructure/street.zip/street/**/*.street_template.lua].
  Bus lanes from 1925 (+10 % of road cost), tram lanes +20 %, electric tram +40 % [TF3-code: mod.script.tl `busLaneYearFrom = 1925`, `roadBusLane = .1`, …].
  "Bus lanes are exclusively for trucks and buses and will not be used by cars when possible." [TF3-code: base.mo:HELP_CONSTRUCTION_ROAD_TOOLS].
* Road congestion: town Traffic rating = `computeTownsTrafficSpeedMap(...)^sensitivity` (+ landmark bonus), with
  `trafficSpeedSensitivityScale = 1.0` [TF3-code: towns/town_util.tl getRatingTraffic; mod.script.tl]. Congestion
  speed model is engine-side.
* No explicit throughput numbers in code. Rules of thumb for the model [derived]: on a single-track section between
  two passing places of running time t, the cycle is ≈ 2·t + switching margin → max ≈ 1 train per direction per
  (2t+margin); on double track with blocks of headway h, ≈ 1/h trains per direction (h ≈ block length / speed +
  train length / speed + braking). Measure in game (Q9).

---

## 7. Line mechanics

* Load modes per stop: `LOAD_IF_AVAILABLE`, `FULL_LOAD_ANY`, `FULL_LOAD_ALL` (+ legacy unload-only) [TF3-code: engine.d.tl Line.LoadMode].
* Timing per stop: "Min. Stop Time" 0–600 s, "Max. Stop Time" 0–600 s or ∞ (−1), "Max. Additional Wait" 0–600 s
  ("wait time after loading to improve vehicle distribution") [TF3-code: gui.zip/gui/line_vehicle_mgmt/cargofilter_window.tl].
* Per-stop cargo config: load flag and max fraction per cargo type, unload policy (§1.1).
* Alternative terminals per stop and waypoints are supported [TF3-code: engine.d.tl Line.Stop].
* Frequency shown = `floor(1 / getMaxFrequency(line))` seconds between vehicles; "Rate" =
  `calcLineStationThroughput(line)` = units/year the line can move [TF3-code: gui.zip/gui/line_vehicle_mgmt/line_util.tl,
  entity_window/line/line_eow.script.tl; base.mo:HELP_STATISTICS_LINES, HELP_WINDOW_LINE]. Both are engine-side; use
  them (via the planner mod) as ground truth.
* `getLineCapacityUsages(line, allCapacities)` → per cargo {used, capacity} [TF3-code: engine/util.d.tl].
* "For supply chains with multiple connected lines, keeping their rates balanced is essential." [TF3-code: base.mo:TUTORIAL_LINE_STATISTICS_GUIDE_PARAGRAPH].
* No vehicles-per-line limit in scripts; `maxVehicleCloneAmount = 50` limits one clone action only [TF3-code: mod.script.tl].

---

## 8. Passengers

* Residents cycle: home → shop → home → work. "Residents will only work in remote industries if public transportation
  is available. Residents also like to shop or work in neighboring towns." [TF3-code: base.mo:HELP_LAYER_CIVILIZATION, HELP_WINDOW_PERSON].
  `SimPerson.destinations` = {residence, shopping, work}; destinations are recomputed with probability 1.0
  (`simPersonDestinationRecomputationProbability`) [TF3-code: engine.d.tl; mod.script.tl].
* Population = residential person capacity (world population = Σ residential capacities) [TF3-code: company/company_util.tl computeWorldPopulation].
* Ticket = 3.609 $/m straight-line × (0.5 + priceFactor) × rank multiplier (§1.3). Comfort affects happiness, not
  price: "Passengers are happy when leaving their home and become less happy the longer the journey takes. In vehicles
  with a high level of comfort, happiness decreases only slowly ... It decreases even quicker when passengers have to
  wait at stations for a long time." [TF3-code: base.mo:HELP_LAYER_PASSENGER_SATISFACTION]. Station buildings add
  terminal `comfortFactor` (+0.05…+0.3 each) = "tolerance for delays" [TF3-code: station modules; modulesutil.getStationPoolComfortFactor].
* Route choice is engine pathfinding; marketing campaigns multiply line cost in a town by `lineCostFactor` (default 0.5)
  during the campaign → more residents choose lines [TF3-code: towns/towns.script.tl OnModifyPathfinding; town_util.tl].
* Industry workers: every industry has `industrialCapacity = 20` with `disallowedMoveModes = {WALK, DRIVE}` (reachable
  only by public transport), `destinationWeight = 2` [TF3-code: industries/industryutil.lua; industries/*.con.lua].
  Worker boost: if the number of distinct residents who arrived **by line** in the last 2 years (list capped at 20) ≥
  0.5 × capacity = **10 workers**, productivity modifier = `workersBoostFactor` = 1.5 + random[0,1) seeded per industry
  → **+50 … +150 %** (planner observed +100…+131 %) [TF3-code: industries/industry_workers.script.tl,
  industries/industry_productivity_util.tl].
* Town XP from passengers: 5 × 2^tier per resident arriving by line at a town building (tier 0 same town, 1 neighbour
  town, 2 other town); trips to industries give no town XP [TF3-code: towns/town_growth.script.tl; town_growth_config.res.lua].
* How many trips per person per year is engine-side [unknown, Q10]. `TownConnection.time` stores approximate travel
  time between towns (used for intercity road building) [TF3-code: engine.d.tl].

---

## 9. Town cargo demand and growth

### 9.1 Demand
* Each town has `cargoNeeds` per land use (commercial, industrial; residential none by default). Start: 2 needs
  (setting `cargoNeedsPerTown` 2/4/6); new needs at town levels 5 (commercial), 7 (industrial), 9 (commercial, tier ≤2),
  11 (industrial, tier ≤2) — added only if the town has received cargo at least once (`cargoDelivered`)
  [TF3-code: economy.zip/economy/placementparamsutil.lua getDefaultTownLevels2CargoInfos; towns/town_growth.script.tl].
* Tiers and consumption factors: tier 0 (fish, meat, vegetables → commercial; bricks, cement, fuel, planks →
  industrial) factor 1; tier 1 (beverages, books, clothes, tinned_food → commercial; chemicals, furniture, machines,
  tools → industrial) factor 0.5; tier 2 (vehicles, industrial) factor 0.25 [TF3-code: placementparamsutil.lua getCargoCategories].
* Per town building with person capacity C, for each demanded cargo: input stock capacity = `ceil(C/4)·factor`, rule
  consumption ×1.2 →
  ```text
  annual demand per building ≈ 1.2 · (C/4) · factor = 0.3 · C · factor   units/year   [derived, same time base as industry rules]
  town demand per cargo     ≈ 0.3 · (district capacity) · factor
  ```
  [TF3-code: buildings/townbuildingutil.lua makeBuildingScript]. Exact supply/limit per town:
  `api.engine.system.townBuildingSystem.getCargoSupplyAndLimit(town, landUse)` (→ supply, limit) [TF3-code: towns/town_util.tl].
* Deliveries beyond a building's stock are refused unless `forceUnload` (destroyed). Only buildings inside the unloading
  stop's cargo catchment receive.

### 9.2 XP and levels
```text
XP per cargo unit delivered to a town building = 10 · 2^tier · xpFactor          (tier0 10, tier1 20, tier2 40)
XP per resident arriving by line               =  5 · 2^tier · xpFactor          (same town 5, neighbour 10, other 20)
xp needed for level L = 1000 · (1.5^(L-1) - 1) / 0.5      → L2 1000, L3 2500, L4 4750, L5 8125, L6 13188, L7 20781, L8 32172
target town size      = initial capacity sum · F(l),  F(l) = 0.00930472 l³ − 0.08061382 l² + 0.63330472 l + 0.438
                        l = level + 1 − initialLevel + fraction → F = 1.00, 1.46, 1.86, 2.28, 2.75, 3.35, 4.11, 5.11 …
```
[TF3-code: towns/town_growth.script.tl, town_growth_config.res.lua, town_cargo_util.tl, scripts.zip/scripts/util/town_growth_function.tl]
* Note XP/unit × consumption factor is equal for all tiers (10·1 = 20·0.5 = 40·0.25).
* Growth is split over districts by usage: residential weight = ½(line-usage·2 clamp + residential supply ratio),
  commercial/industrial weight = supply/demand of that district (min share `townGrowthMinDistrictFraction`, max 50 %
  per town / 40 % per map) [TF3-code: towns/town_growth.script.tl].
* HQ gives +5 % growth to its town [TF3-code: base.mo:TUTORIAL_BUILD_HQ_GUIDE_PARAGRAPH].

### 9.3 xpFactor (rating-dependent)
```text
authorityScore = min(rating_reputation, rating_traffic, rating_happiness, rating_delivery, rating_noise, rating_pollution)
ratingLevel(x) = clamp(floor(6x), 0, 5)  → VeryPoor, Poor, Mediocre, Good, VeryGood, Excellent
suppliesLevel  = rounded-up average of level(cargoRating) and level(passengerRating)
    cargoRating     = max over districts of Σsupply/Σdemand (clamped 0..1)
    passengerRating = clamp(2 · lineUsage, 0, 1)            (50 % of residents using lines = full)
growthLevel    = (authorityLevel/5) · ((suppliesLevel+1)/6)
thresholds     = 0.061, 0.147, 0.267, 0.435, 0.671, 1.0  → xpFactor 0, 0.2, 0.4, 0.6, 0.8, 1.0
```
[TF3-code: towns/town_util.tl getGrowthLevelMult, calcAuthorityScore, getRatingDeliveries; town_cargo_util.tl authority2xpFactor]
* One bad rating (e.g. on-time delivery or noise) caps the whole town's growth.

### 9.4 Delivery (on-time) rating
```text
onTime = 1 − late / max(10, delivered)          over the last ½ year (all cargo delivered into the town or its industries)
min = 1 − exp(−0.35667·s);  max = −1.2·min(s,1)² + 2.2·min(s,1)
rating = clamp((onTime − min)/(max − min), 0, 1)
s (sensitivity): Easy 0.75 → rating>0 needs >23.5 % on time (full at 97.5 %); Medium 1 → 30 %; Hard 1.5 → 41.4 %; VeryHard 2 → 51.0 %; full rating at 100 % for s ≥ 1
```
[TF3-code: towns/town_util.tl getRatingCargoDelivery; difficulty_util.tl level2sensitivityCargoDelivery]
* Other ratings: noise mapped 65→80 dB to 1→0; pollution (per 16 m cell, area clamped 5–10 km²) 60→74 dB to 1→0;
  sensitivity adds 10·log10(s) dB [TF3-code: town_util.tl; game_mechanics/emission/emission_config.tl].
  Happiness rating uses the same min/max formula on the share of happy travellers (min 15 people).

---

## 10. Industry growth and closure

```text
every 0.5 year (extendTimeSpanYears), per industry with level < maxLevel and output:
    rating = productionRating · min(1, shippedPerYear / outputPerYear)
    P(extend) = clamp((rating − 0.25) / 0.5, 0, 1)          # ≥75 % → certain, ≤25 % → never
    extend = add ONE field/satellite module (may fail on collision)
closure: candidate if nothing consumed AND nothing loaded for 5 years (unusedTimeSpanYears)
    per year P(start countdown) = closureProbability = 0.011 · {0, 1, 2, 4, 8}[difficulty]
        Easy 0, Medium 1.1 %, Hard 2.2 %, VeryHard 8.8 %
    countdown 2 years; any consumption/loading cancels it
```
[TF3-code: game_mechanics/industries/industries.script.tl, industries_config.res.lua; base.zip/base/mod.script.tl, difficulty_util.tl]
* Level = number of fields; output of a rule scales with fields/maxFields (`adjustStocklistRule`); `maxLevel` is random
  between ~50 % and 100 % of the field slots (8 for coal/iron/clay pits) [TF3-code: industries/industryutil.lua].
* `productionRating` and the yearly output/shipped values are engine helpers in `api.engine.util.stock` [TF3-code: engine/util.d.tl].
* Booster rules (tools, machines, fertilizer…): `boostFactor` = random 0.50–1.50 → +50…+150 %; booster consumption
  not scaled by productivity [TF3-code: industries/industryutil.lua; planner calibration].
* Industry productivity difficulty: 0.5 / 0.75 / 1 / 1.5 / 2 by level; presets VeryHard 0.5, Hard 0.75, Medium 1.0,
  Easy 1.5 [TF3-code: difficulty_util.tl].

---

## 11. Construction costs and building catalogue

### 11.1 How costs are computed
* Constructions' real price = `updateFn` result.cost + Σ module `metadata.price`. Station/warehouse base cost is 0, so
  **the price is the sum of the modules** [TF3-code: scripts.zip/scripts/construction/construction.script.tl,
  modulesutil.lua addCosts]. Upkeep = result.maintenanceCost (or cost/6 if missing) + Σ module maintenanceCost.
* Difficulty: `infrastructureCostScale` (0.5/0.75/1/1.25/1.5; VeryHard 1.5) multiplies **every construction**
  (base result.cost in `applyConstructionCostScale`, module prices in `post_run_util.applyCostScale`, also station track
  modules), streets/tracks/bridges/tunnels/crossings; maintenance via `infrastructureMaintenanceScale`
  (shown as result.maintenanceCost × scale in the GUI) [TF3-code: base.zip/base/post_run_util.tl, mod.script.tl;
  gui.zip/gui/construction/construction_desc_react_util.tl]. Vehicles: `vehiclePurchaseCostScale`,
  `vehicleMaintenanceScale`.
* Menu descriptions for later eras show ×3 (B) and ×9 (C) of the era-A price, but module/script prices are the same in
  all eras and there is no year multiplier for constructions in any script (only `treeCostYearMultipliers`). Streets
  are different: 1940+ road templates really cost 3× (60→180 $/m). Treat era-B/C construction prices as unverified (Q8).
* Bulldoze: 75 % refund if removed within 60 s (`bulldozeRefundFactor`, `bulldozeRefundTimeOut`); terrain 4.5 $/m³ raise,
  5.25 $/m³ lower (× cost scale) [TF3-code: mod.script.tl].

### 11.2 Catalogue (prices at scale 1.0; × 1.5 on VeryHard; upkeep per year)

**Road (truck / bus)** — `stations/street.zip/street/...`

| item | years | build | upkeep | capacity / handles | catchment |
|---|---|---|---|---|---|
| Small stop one-sided (old/mid/new) | ≤1920 / 1920–1980 / 1980+ | 30 000 | 5 000 | pax load+unload, cargo **unload only**, 50 waiting | 160 |
| Small stop two-sided | same eras | 30 000 by script (menu 60 000) | 5 000 | same, both road sides | 160 |
| Modular cargo station, universal, 1 / 2 / 4 terminals | era A 1900–1950, B 1950–1990, C 1990+ (same modules) | 66 000 / 126 000 / 246 000 | 11 000 / 21 000 / 41 000 | load+unload cargo, terminals = 2 × `cargo_platform` (30 000 / 5 000 each) + `entrance_exit` (6 000 / 1 000) | 175 per entrance/exit module |
| Modular cargo station, specialized (liquid/bulk/flatbed/goods) | same | 126 000 / 246 000 / 486 000 | 21 000 / 41 000 / 81 000 | 60 000 / 10 000 per platform module; ×2 loading for matching class, 0.25× for others | 175 |
| Modular bus/tram passenger station 1 / 2 / 4 terminals | same | 126 000 / 246 000 / 486 000 | 21 000 / 41 000 / 81 000 | `passenger_platform` 60 000 / 10 000; optional waiting buildings 60 000 (10×10) / 120 000 (20×20) add capacity + comfort | 175 |
| Extra entrance / exit module | | 6 000 | 1 000 | adds catchment circle | 175 |
| Underground station | 1970+ | 900 000 by script (menu 120 000) | 150 000 | passengers | 200 |
| Road depot | 1900+ | 180 000 | 30 000 | buy/sell road vehicles; maintenance pool 12 | maint. 400 |
| Road maintenance building | 1900+ | 60 000 | 10 000 | pool 16 | 600 |
| Tram depot | 1900+ | 360 000 by script (menu 120 000) | 60 000 | pool 12 | 400 |

**Rail** — `stations/rail.zip/rail/modular_station/*`, `depots/rail.zip/rail/*`

| module / item | years | build | upkeep | notes |
|---|---|---|---|---|
| Track segment (40 m, any track type, ± catenary) | | 30 000 | 5 000 | per track per 40 m |
| Cargo platform universal (40 m) | A ≤1950 / B 1950–1990 / C 1990+ | 30 000 | 5 000 | load modifier 1 |
| Cargo platform specialized liquid/bulk/flatbed/goods (40 m) | same | 60 000 | 10 000 | ×2 matching class, 0.25× others |
| Passenger platform (40 m) | same | 60 000 | 10 000 | roofs/stair add-ons free |
| Cargo main building 1 / 2 / 3 | all | 18 000 / 30 000 / 60 000 | 3 000 / 5 000 / 10 000 | cargo catchment 150 / 175 / 200 |
| Cargo side building 1 / 2 / 3 | all | 6 000 / 12 000 / 18 000 | 1 000 / 2 000 / 3 000 | 50 / 60 / 70 |
| Passenger main building 1 / 2 / 3 | per era | 18 000 / 30 000 / 60 000 | | pax catchment 150 / 175 / 200, comfort +0.1/+0.2/+0.3 |
| Passenger side building 1 / 2 / 3 | per era | 12 000 / 24 000 / 48 000 | | 50 / 60 / 70 |
| Stairs (side entrance) | | 12 000 | 2 000 | pax catchment 100 |
| Template "Cargo Station" (through or terminus) 1 track × 80 m | | 138 000 (= 18 000 + 2×30 000 + 2×30 000) | 23 000 | template range 1–8 tracks × 80–320 m; main building size 1 (<3 tracks), 2 (3–5), 3 (≥6); later extendable up to 840 m |
| Template "Passenger Station" 1 track × 80 m | | 198 000 | 33 000 | |
| Rail depot | 1900+ (3 era looks) | 600 000 | 100 000 | pool 6, maint. radius 800 |
| Rail maintenance building | 1900+ | 240 000 | 40 000 | pool 12, radius 800 |

Formula [derived]: rail cargo station = main building + Σ_tracks n_seg·30 000 + Σ_platforms n_seg·(30 000 universal | 60 000 specialized);
e.g. 1 track, 160 m, universal, small main building = 18 000 + 4·30 000 + 4·30 000 = **258 000** (387 000 on VeryHard);
specialized = 378 000 (567 000 VH). Station template cargo `specialization` param: Universal/Liquid/Bulk/Flatbed/Goods.

**Water** — `stations/water.zip/water/*`, 6 harbor menu entries = small/large × era A (1900–1950), B (1950–1990), C (1990+)

| item | build | upkeep | capacity | catchment |
|---|---|---|---|---|
| Small harbor 1 / 2 / 4 terminals | 144 000 / 324 000 / 444 000 | 24 000 / 54 000 / 74 000 | small ships only; terminals = small piers (60 000), mixed pax+cargo | dock 25×25 → 150 |
| Large harbor 1 / 2 / 4 terminals | 228 000 / 318 000 / 678 000 | 38 000 / 53 000 / 113 000 | small and large ships; medium piers (90 000) | dock 100×50 → 250 |
| Modules: pedestrian entrance 6 000; dock 25×25 12 000; dock 50×50 24 000 (250 m); dock 100×50 24 000; dock 50×12 60 000; dock 100×25 90 000; small pier 60 000; medium pier 90 000 | | | | |
| Ship depot | 600 000 | 100 000 | pool 8 | maint. 1000 |
| Ship maintenance building | 240 000 | 40 000 | pool 12 | 1400 |

Harbor small 1-terminal = 2 entrances + dock 25×25 + dock 50×12 + small pier = 144 000 (matches menu) [derived].
Cargo catchment of a harbor comes from its docks' circles (harbor terminals are mixed) — check in game that cargo
catchment shows (Q4).

**Air** — `stations/air.zip/air/*`

| item | years | build | upkeep | notes |
|---|---|---|---|---|
| Airfield: main building / terminal / hangar modules | 1920+ | 600 000 each | 100 000 each | main: pax+cargo catchment 250–300; hangar = depot, pool 16, radius 550 |
| Airport (1950–1980, 1980+): main / terminal / heli terminal / hangar | 1950+ | 900 000 each | 150 000 each | catchment 300; 2nd runway, taxiway "terminal B" 900 000 (1980+) |
| Helipad | 1950+ | 180 000 | 25 000 | catchment 100, cargo allowed |
| Heliport | 1950+ | 300 000 | 50 000 | catchment 175, pool 4 |

**Warehouses** — see §2: modules 150 000/25 000 (universal) or 300 000/50 000 (specialized), 500 units each,
entrance free; menu entries per class for era A 1900–1940, B 1940–1980, C 1980+.

**Typical 1900 VeryHard bill [derived, ×1.5]:** truck cargo station 1-terminal universal 99 000 (upkeep 16 500);
specialized 189 000; road depot 270 000; road maintenance building 90 000; universal warehouse module 225 000;
specialized warehouse module 450 000; 1-track 160 m universal rail cargo station 387 000; rail depot 900 000; small
harbor 216 000; simple track 90 $/m; old country road small 90 $/m.

---

## 12. Other optimizer-relevant mechanics

### 12.1 Difficulty presets [TF3-code: base.zip/base/difficulty_util.tl, mod.script.tl]

| option | Easy | Medium | Hard | VeryHard |
|---|---|---|---|---|
| infrastructure & vehicle purchase/maintenance scale | 0.5 | 1.0 | 1.25 | 1.5 |
| industry productivity | 1.5 | 1.0 | 0.75 | 0.5 |
| closure probability /yr | 0 | 1.1 % | 2.2 % | 8.8 % |
| cargo & passenger income | 1.0 | 1.0 | 1.0 | 1.0 |
| vehicle maintenance effect | 0.5 | 1.0 | 1.5 | 2.0 |
| ticket multiplier at rank 15 ("inflation") | 0.9 | 0.75 | 0.6 | 0.5 |
| subvention mode (spawn interval) | every 3 months·(1+rank infl.) | 6 months | 12 months | **disabled** |
| subvention risk | low | … | … | high (150–300 units, ×0.5 time/upfront) |
| town sensitivities traffic/happiness/delivery | 0.75 | 1 | 1.5 | 2 |
| noise / pollution sensitivity | 0.5 / 0.5 | 1 / 1 | 1.4 / 1.7 | 1.8 / 3 |
| reputation (urban care) sensitivity | 1 | 1 | 1 | 1 |

### 12.2 Company rank and the income multiplier ("inflation")
* Company XP = **world population** (sum of residential capacities), never decreasing; rank 1 at the starting
  population P0, rank r threshold ≈ P0 + interp·(W(r) − 1) with `W(r) = 0.001r³+0.027398r²+0.14221r+0.829392`
  (W = 1.00, 1.23, 1.53, 1.90, 2.35, 2.89, 3.51, 4.23, 5.06, 5.99, 7.04, 8.21, 9.51, 10.93, 12.50 for r = 1…15) — max rank
  15 needs ≈ 12.5 × P0 [TF3-code: company/company_progression_util.tl, company_growth.script.tl, company_util.tl].
* `ticketPriceMultiplier(r) = 1 + (r − 1)·(m15 − 1)/14`, m15 = 0.5 on VeryHard (rank 8 → 0.75), 0.75 on Medium.
  Applies to **all income, cargo and passengers** (§1.3).
* **Rank-ups are applied manually but the penalty is not:** the script stores `potentialLevel` (from population) and
  `level` (applied); `level` only increases when the player selects the rank in the company window
  (`applyLevel` event from `company/company.tl`), which unlocks perks. `ticketPriceMultiplier` is computed from
  `newLevel` = potential level, so **the income cut happens automatically as population grows**, whether or not the
  rank is applied [TF3-code: company_growth.script.tl updateLevel/applyLevel; company.tl L421].
  Consequence: growing towns lowers per-unit income on all lines; the model should discount future revenue by the
  expected rank path.

### 12.3 Subventions
* Deliver-cargo subsidy: upfront `0.75·rand(20..50)·100 000·riskFactor` (×inflation scale 1→4 from 1900→2000),
  failure = repay upfront + fine `0.75·rand(40..80)·100 000`; required amount 10–300 units by risk; time `(6 + 32·expected
  months)·riskFactor` months [TF3-code: game_mechanics/subventions/deliver_cargo/deliver_cargo.script.tl, subvention_util.tl,
  finance/finances_util.tl scaleWithInflation]. Disabled on VeryHard (mode 0).

### 12.4 Emissions near towns
* Town noise rating maps average dB at residential buildings 65→80 dB onto 1→0; pollution 60→74 dB (per 16 m cell over
  the town's Voronoi area, area clamped 5–10 km²) [TF3-code: emission_config.tl, town_util.tl]. Pollution counts for the
  town in whose **municipal area** it is emitted (wind-affected) [TF3-code: base.mo:HELP_LAYER_POLLUTION].
* Emitters: industries (idle/producing/boosted levels), stations (×2 when crowded), depots (×2 at full pool),
  warehouses, vehicles (worse when badly maintained), tracks (noise/pollution per 100 m; faster track types emit more)
  [TF3-code: industries/*.con.lua emissionConfig, emissions.script.tl, base.mo:TUTORIAL_BUILD_TRACKS_GUIDE_PARAGRAPH].
* "Greenify industry" perk reduces industry emissions (`emissionReductionPerEcoLevel = 0.8`) [TF3-code: emission_config.tl].

### 12.5 Vehicles: price, running cost, depreciation
* If a model has `price = -1` (most base vehicles), cost is generated:
  ```text
  c          = 3.7·1460/2 · 0.5 / 2 = 675.25
  cap        = maxCargoCapacity / 4
  v_avg      = road/tram: ((v_top·3.6)^0.78 + 4)/3.6 ; rail: ((v_top·3.6)^0.86 + 10)/3.6 ; water: 0.65·v_top
  running    = v_avg·cap·c   (rail: wagons ·(1−0.5), locos: v_avg·(P/v_top_kmh·12.5)·c·0.5)
  runningCosts = running · runningCostScale ;  price = running · 6 · priceScale
  ```
  then × difficulty vehicle scales [TF3-code: base.zip/base/model_metadata_util.lua addCostMetadata; post_run_util.tl].
  Example Benz 1912 box truck (48 GOODS, 30 km/h, priceScale 1.5, runningCostScale 1.25): running ≈ 51 000/yr,
  price ≈ 370 000 at scale 1.0 [derived].
* Depreciation/resale: `api.engine.util.vehicle.getDepreciatedValue` — engine-side [unknown, Q11]. Locomotive and
  wagons should have similar max speeds for cost efficiency [TF3-code: base.mo:TIP_VEHICLES_1].

### 12.6 Misc
* Bus lanes 1925+, catenary 1921+, traffic lights per config; landmarks give vehicle bonuses (speed/noise/pollution/
  comfort modifiers) in their municipal area [TF3-code: mod.script.tl; vehicle/vehicle_modifier.script.tl; base.mo:TIP_MISC_7].
* Loans: up to 4 at a time [TF3-code: base.mo:HELP_COMPANY_LOANS].
* Bulldozing town buildings / terraforming / removing trees lowers town reputation (e.g. −100 % for removing 33 % of a
  district; −0.25 % per tree) [TF3-code: town_util.tl calculateProposalImpact].

---

## 13. Implications for the optimizer (modelling rules)

1. **No transfers without warehouses.** Model every multi-leg chain as nodes {industry stock, warehouse stock, town
   building stocks} and edges = lines. Two lines may only share cargo through a stock (industry or warehouse) inside
   both stops' cargo catchments. Station-to-station hand-over is not allowed.
2. **Revenue per leg:** `R = 3.8 $/unit/m × (straight-line 3D distance + 8·climb) × rankMultiplier(t)` for every cargo;
   passengers 3.609 $/m. Splitting a route at a warehouse earns Σ of leg chords — never more than the detour-free sum,
   so warehouses are for capacity/mode change, not for revenue.
3. **Cargo type is irrelevant to price** → choose chains by volume, distance, deadline and vehicle class only.
4. **Deadline constraint, not price:** for each chain, door-to-door time (stock waiting + loading + travel +
   warehouse dwell + unloading) ≤ `getAdjustedDeliveryTime(cargo)` (base 900–4500 s), plus a specialized-warehouse
   extension. Penalty is in town growth: on VeryHard (s = 2) the Delivery rating is 0 below 51 % on time and only reaches
   Excellent (≥ 5/6) at ≈ 92 % on time; a 0 rating makes the town's xpFactor 0. Late cargo is still paid.
5. **Catchment** = union of circles: road station 175 m per entrance/exit module, small stop 160 m (unload only),
   rail cargo main building 150/175/200 m + side buildings 50/60/70 m, harbor 150/250 m, airport 300 m. Use Euclidean
   distance from module position to the target (exact target point TBD, Q4).
6. **Small road stops cannot load cargo** → every loading point needs a modular road station (≥ 66 000 base) or rail
   station / harbor.
7. **Loading:** specialized platform + specialized vehicle + specialized warehouse = up to ×2 (terminal) ×2 (stock)
   speed; wrong class on specialized platform or train overhang = ×0.25. Platform length ≥ train length (40 m modules;
   template up to 320 m, extendable to 840 m).
8. **Maintenance coverage constraint:** each vehicle must spend its time within a maintenance radius
   (road depot 400 m / road maint. building 600 m, rail 800 m, water 1000/1400 m) of a building with free pool slots
   (road depot 12, road maint. 16, rail depot 6, rail maint. 12, ship 8/12). Rule: `ceil(vehicles_on_route / pool)`
   maintenance buildings whose circles cover the route; cheapest coverage for trucks = road maintenance buildings
   (60 000 for 16 vehicles). Uncovered vehicles lose condition → speed/cost/emission penalties ×2 on VeryHard.
9. **Rail capacity:** path signals; single track with passing loops supports bidirectional traffic; capacity ≈ one
   train per direction per (2 × section running time + margin) on single track; add signals/loops to scale.
10. **Industry growth:** keep `shipped/output ≥ 0.75` (and full production rating) to grow one field per 0.5 year;
    ≥ 0.25 for any chance. On VeryHard an industry with nothing loaded/consumed for 5 years has 8.8 %/yr chance of a
    2-year closure countdown — serve every industry you depend on.
11. **Worker boost:** ≥ 10 distinct residents delivered by public transport to an industry within 2 years → output
    ×1.5…2.5 (industry-specific, random). Booster cargo → ×1.5…2.5 independently.
12. **Town demand** ≈ 0.3 × district person capacity × factor (1 / 0.5 / 0.25 by tier) units/year per demanded cargo;
    supplying the district evenly matters (cargo supply rating = best district Σsupply/Σdemand).
13. **Town growth objective:** XP/year = Σ_units 10·2^tier·xpFactor + Σ_line_arrivals 5·2^tier·xpFactor, xpFactor ∈
    {0,0.2,…,1} from min(all six ratings) and supply level; Excellent requires all ratings ≥ 5/6 and supplies ≥ 5/6.
14. **Rank penalty:** discount revenue by `1 + (rank−1)(m15−1)/14` where rank follows world population (×12.5 of the
    start for rank 15); m15 = 0.5 on VeryHard. Rank-ups are applied manually but the income cut is automatic.
15. **Costs:** use module sums (§11) × difficulty scale (1.5 on VeryHard) — e.g. 1900 VH: truck cargo station 99 000,
    road depot 270 000, road maintenance 90 000, warehouse module 225 000/450 000, rail cargo station (1 track, 160 m)
    387 000, rail depot 900 000, simple track 90 $/m; upkeep ≈ price/6 per year (also ×1.5).
16. **Subventions:** ignore on VeryHard (disabled).
17. **Time base:** 1 year = 1461 s; all per-year rates (rule capacity, demand, maintenance) use this year.

---

## 14. Open questions (need in-game tests)

* **Q1** Same-mode transfer at a shared station without a warehouse — confirm it is impossible (unloaded cargo with no
  stock: refused or destroyed?).
* **Q2** Does a town/industry stop pull from a warehouse in the same catchment without a second line? (expected: no).
  Which stock is chosen when several compatible stocks are in a catchment; can a vehicle unload into the warehouse it
  loaded from.
* **Q3** Delivery clock start (production vs pickup); size of the specialized-warehouse extension; the yearly
  `getAdjustedDeliveryTime` curve (read it via the planner mod per year).
* **Q4** What exactly must be inside a catchment circle for industries (centre / stock lanes / any lot) and harbor
  cargo catchment display.
* **Q5** Exact loading formula: measure load time of a truck (loadSpeed 6) with 48 units on universal vs specialized
  terminal, from industry dock vs own station vs specialized warehouse.
* **Q6** Meaning of depot `capacity` (5/8/10) — parking slots? Is there any purchase limit per depot?
* **Q7** Condition decay rate outside maintenance areas and restore rate inside; effect of `maintenanceFactor` and
  `lifespan`; penalty curves (call `api.engine.util.vehicle.getMaintenance*Penalty(state)` from the mod for state 0..1).
* **Q8** Era-B/C construction prices: module scripts say fixed (e.g. warehouse module 150 000), menus show ×3/×9;
  tram depot (360 000 vs menu 120 000), underground (900 000 vs 120 000), two-sided stop (30 000 vs 60 000). Read the
  actual charge in a proposal (`proposalData.costs`) from the mod.
* **Q9** Practical train headway on simple track with 1900 locomotives; road congestion speed curve.
* **Q10** Trips per resident per year (passenger generation) and intercity share.
* **Q11** Vehicle depreciation curve (`getDepreciatedValue` over age).
* **Q12** Whether payment `posDifference` is pickup→drop-off of the leg (expected) or origin→destination of the item.

## Sources
* Game files: `<Steam library>\steamapps\common\Transport Fever 3\base\content\*.zip` and `…\base\content\<category>\*.zip`,
  `api/tealdef/**/*.d.tl`, `base/tealdef/**/*.d.tl`, `base/strings/en/LC_MESSAGES/base.mo` (decoded).
* [TF3 Dev Blog Ep. 2: Industries and Cargo](https://www.transportfever3.com/news/dev-blog-episode-2-industries-and-cargo-2/)
* [TF3 First Look Ep. 3: Infrastruktur](https://www.transportfever3.com/de/news/first-look-episode-3-infrastruktur/)
* [TF2 Steam: payment per leg / straight line](https://steamcommunity.com/app/1066780/discussions/0/3783625216798366301)
* [TF2 Steam: catchment along roads, cargo jumping between stations](https://steamcommunity.com/app/1066780/discussions/0/3855581440177894075)
* [TF2 Steam: signals and single track](https://steamcommunity.com/app/1066780/discussions/0/2569816492288466759)
* [TF2 Steam: maintenance slider, running cost 1/6 of price](https://steamcommunity.com/app/1066780/discussions/0/3963662189542369520)

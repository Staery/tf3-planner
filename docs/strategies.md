# Transport Fever 2 / 3 — Player Strategies (research notes for tf3-planner)

Research date: 2026-10-01 (TF3 released 2026-09-29, so post-release TF3 player data is only ~2 days old).

Tags used throughout:
- **[TF2]** — observed/established in Transport Fever 2 (app 1066780); may not carry over.
- **[TF3-dev]** — stated by Urban Games (dev blog, store page, staff "Sam"/"UG-Sam" replies).
- **[TF3-player]** — post-release player reports on the TF3 Steam forum (app 3493540).
- **[TF3-SEO]** — third-party "wiki"/guide sites that appeared around launch (transport-fever.wiki, whisperofthehouse.com, transportfever3.world, transportfever3help.wiki, xmodhub, *.sbs, *.top). Many are explicitly based on pre-release footage or are AI-written; treat as **low confidence** unless corroborated.

Source reliability: Urban Games statements > TF3 Steam forum players with tests > TF2 Steam forum consensus > SEO sites.

---

## 0. The single most important finding: how TF3 pays for cargo

This is decisive for the optimizer and partially contradicts our model (see the last section).

- **[TF3-dev] Distance is no longer the main driver of cargo pay; cargo complexity is.** UG staff Sam (23.04.2026), Dev Blog Ep.4 comments: *"We have ... revised the way payments are calculated to make the distant cargo route method a thing of the past. Long distance passenger travel at speed will continue to pay well, but when transporting goods, it is the complexity of the cargo being delivered that is far more meaningful when it comes to the payment received."* — https://www.transportfever3.com/news/dev-blog-episode-4-tycoon/
- **[TF3-dev]** Sam (11.09.2026), answering whether detours are penalised: *"Cargo deliveries are priced based on the type of cargo being delivered. Providing you are within the time limit for that cargo type you'll earn the same amount regardless of how far that cargo has needed to travel."* — https://www.transportfever3.com/news/tycoon-gameplay-overview-revealed/ (comment thread; the question was about detours vs. tunnels, so "how far" may mean route length rather than straight-line distance — ambiguous).
- **[TF3-player] Conflicting post-release test** (Mike Takumi): *"Distance DOES matter, but only between origin and destination, so if you take it to an intermediary stop out of the way, that doesn't factor in."* and *"the total payout is split between all vehicles on delivery, so if a train takes it 80% of the distance and a truck takes it 20%, the truck will get 20% of the total payout credit."* and *"Top speed is no longer a factor in payout. Tested ... grain ... same amount of money per cargo unit with a stake car train, generic car train, slower cargo wagons, slower power unit, doing a truck route, and doing a helicopter."* — https://steamcommunity.com/app/3493540/discussions/0/588439235122525543/
- **[TF3-player]** Delivery-time decay: *"for most cases the ticket price starts to drop after 100 days (2000ms per day) and stop dropping after 200 days. Passengers on the other hand dont get affect by delivery time but vehicle price."* — https://steamcommunity.com/app/3493540/discussions/0/588439235122597876/ ; colours: yellow = almost out of time, red = complaints/penalties (same thread).
- **[TF3-player]** Payment only on final delivery (pre-release expectation): *"you only get paid at the final delivery so no more getting paid to send cargo on hilariously indirect paths"*; *"different cargo types actually pay differently"* — https://steamcommunity.com/app/3493540/discussions/0/804596245555511341/
- **[TF3-player]** Anecdote: *"I transported oil for 652k but just 5 passengers and I got 104k"* — https://steamcommunity.com/app/3493540/discussions/0/588439235122490211/
- **[TF3-SEO, low]** Pre-release per-cargo deadlines from a hands-on build: meat ~10 months; fish, grain ~1 year; tools, cement, bricks ~2 years; logs, lumber, crude ~2.5 years; stone ~4 years; warehouse storage extends the deadline — https://transportfever3.world/industries-and-cargo/

Best reading of the evidence: TF3 cargo pay = f(cargo type/complexity) x (maybe) origin-to-final-destination straight-line distance, split across legs by leg distance, decayed by lateness; top speed irrelevant; detours unpaid. Whether the distance term exists is **unresolved** and must be measured in-game (see Implications, rule 1).

**[TF2] for comparison** (reverse-engineered, community): `pay/unit = (300 + d) * basePrice(vehicle type, top speed) * cargoFactor * 125 / millisPerDay * difficulty`, d = straight-line metres station-to-station, cargoFactor 1.75 cargo / 1.0 passengers, difficulty Easy 1.0 / Medium 0.8 / Hard 0.6 / VeryHard 0.4 — https://cal1.lr.ggtyler.dev/r/TransportFever/comments/ztpldt/update_payment_formula ; top-speed and small elevation bonus confirmed by players — https://steamcommunity.com/app/1066780/discussions/0/3783625216798366301 , https://steamcommunity.com/app/1066780/discussions/0/2569816492271976161 . In TF2 each leg of a transfer was paid separately (transfer "exploit") — https://steamcommunity.com/app/1066780/discussions/0/2872668209264956257

---

## 1. Early game (1850–1920) money on hard difficulty

### Passengers vs cargo
- **[TF2] Consensus: cargo first.** *"Stick to freight to make money in TpF2 until you have money then do passengers"* — https://steamcommunity.com/app/1066780/discussions/0/2639605226297577742 ; *"forget passengers until you have money coming in from freight"* — https://steamcommunity.com/app/1066780/discussions/0/1746772087797947925 ; cargo paid 1.75x passengers in the formula above.
- **[TF2]** Passenger-first starts work but are slow; passenger demand only grows after towns grow (chicken-and-egg) — https://steamcommunity.com/app/1066780/discussions/0/594009125553187136/?l=spanish , https://steamcommunity.com/app/1066780/discussions/0/1740012252805414784
- **[TF3-player] Passengers look much stronger than in TF2.** Several profitable hard-difficulty starts used passenger trains fed by town bus loops: *"I started with trains (passengers only) and they start making a profit from year one"*; *"9-10 million dollar loan ... bus loop, train line ... 1 train with 2 passenger cars"*; *"two rather short trainlines that are making me 2M"* (hard) — https://steamcommunity.com/app/3493540/discussions/0/588439235122525543/ . Dev: *"Long distance passenger travel at speed will continue to pay well"* (Dev Blog Ep.4 link above).
- **[TF3-player]** Raw production is very low without boosters: *"The Rates produce is supplied is insanely slow. Without workers and tools, machines, fertiliser etc it never gets to any decent speed"* — https://steamcommunity.com/app/3493540/discussions/0/588439235122490211/ . On VeryHard, where productivity is x0.5, this is worse still, so early cargo volume is the bottleneck.

### Intra-town vs intercity
- **[TF2]** Cheap, reliable money in 1850: bus lines between road-connected towns plus a few horse-cart cargo deliveries — https://steamcommunity.com/app/1066780/discussions/0/6363065234129022230
- **[TF2]** Up to 4 bus stops in a loop covering residential/commercial/industrial areas to raise destination demand, then feed an intercity station — https://steamcommunity.com/app/1066780/discussions/0/594009125553187136/?l=spanish
- **[TF3-player]** Build bus loops to widen station catchment *before* launching the train — https://steamcommunity.com/app/3493540/discussions/0/588439235122525543/ ; a player with 120 km/h trains still got "travel time too long" warnings and was told to add in-town buses/trams — https://steamcommunity.com/app/3493540/discussions/0/588439235122602903/
- **[TF3-dev]** Intercity passengers give more town XP than intra-city ones; trams have a "massive" inner-city impact (Sam) — https://www.transportfever3.com/news/dev-blog-episode-4-tycoon/

### Which cargo chains
- **[TF2]** Best: two-way-loaded legs: logs→sawmill with planks back (stake cars both ways), crude→refinery with fuel back. *"four of the six wares production chains involve either a logs-planks or crude-oil train leg that can be full in both directions. These routes make vastly more money than any other"* — https://steamcommunity.com/app/1066780/discussions/0/1740012252805414784 ; target *"no more than 25% empty"* on the main line — https://steamcommunity.com/app/1066780/discussions/0/3963662255255284920
- **[TF2]** Simple 2-industry chains (grain→food, stone→bricks) as fallbacks — https://steamcommunity.com/app/1066780/discussions/0/1746772087797947925
- **[TF2]** Water: *"Cheapest/easiest money in the game for early starts (1850)"* — cargo ships if industries sit on water — https://steamcommunity.com/app/1066780/discussions/0/6363065234129022230
- **[TF3-dev]** Cargo pay scales with chain complexity, and complex goods give more town XP — so in TF3 finished/processed goods (bricks, lumber, fuel) delivered to towns should beat raw cargo per unit (Dev Blog Ep.4 link above). Early towns demand simple goods (e.g. vegetables direct from farm) — https://indigo-pearl.prezly.com/transport-fever-3-industries--cargo-revealed

### Distances
- **[TF2]** Longer is better per unit (pay ~ (300+d)), but early vehicles are slow and hills kill steam/horse profitability: *"if you start at 1850 it is crucial that the area between the towns is flat"* — https://steamcommunity.com/app/1066780/discussions/0/1740012252805414784 ; first line *"the shortest line you can make to generate profit asap"* — https://steamcommunity.com/app/1066780/discussions/0/3963662255255284920
- **[TF3]** If cargo pay no longer scales with distance, the optimum flips to **short, high-turnover shuttles** for cargo and **long, fast lines for passengers** ([TF3-dev] statements above; [TF3-SEO, low] "a truck that completes ten paying trips a year beats a train that completes two" — https://www.whisperofthehouse.com/transport-fever-3/beginner-guide).
- Deadlines cap useful distance for perishables (meat/fish/grain ~10–12 months at pre-release numbers) — slow horse carts on long hauls risk late penalties.

### Bootstrapping from zero with loans
- **[TF2]** Max out the loan and invest in high-ROI lines: *"if borrowed money is used well on lines with strong ROI (30% or more is readily doable), then that 2% interest is negligible"* — https://steamcommunity.com/app/1066780/discussions/0/6363065234129022230 ; ~500k infrastructure per line with ~3.5M reserved for vehicles — https://steamcommunity.com/app/1066780/discussions/0/3963662255255284920
- **[TF3-dev]** "dynamic loan and contract system offering risk versus reward" — https://store.steampowered.com/app/3493540/
- **[TF3-player]** Hard starts at negative balance (*"-$3.7M ... Starting in 1900. But I'm making profit, just loan payment is harsh"*); *"Keep refinancing your loans as the good offers come up - im now $50 mill in the black with no debt"*; trains *"are money printers in the early years"* if financed by credit — https://steamcommunity.com/app/3493540/discussions/0/588438959484645908/
- **[TF3-player]** Loan installments (principal+interest) crush cash flow: *"you get an instant Debt of 5 million a year, you make 1 million in profits"*; interest rates themselves are *"extremely low"* — https://steamcommunity.com/app/3493540/discussions/0/588439235122490211/
- **[TF3-SEO, low]** Example offers: $9M / 48 months / 7%, $12M / 108 months / 10%, refinance $21M / 228 months; advice: size the loan to the network you can actually finish — https://www.whisperofthehouse.com/transport-fever-3/economy-money-guide

---

## 2. Common pitfalls

- **Infrastructure upkeep > line income** [TF2]: *"one train or 1 ship may not pay for 2-3 harbours or stations"*; *"spamming stations all over the place with one horse cart each will kill you"*; airports especially — https://steamcommunity.com/app/1066780/discussions/0/3963662255255284920 , https://gog.com/forum/transport_fever_2/confused . [TF3-player]: check station upkeep in red — https://steamcommunity.com/app/3493540/discussions/0/588438959484645908/
- **Empty backhauls** [TF2]: *"an empty drive can eat all your profits"* with early vehicles — https://gog.com/forum/transport_fever_2/confused
- **Multi-stop processing trains** [TF2]: *"Don't use a Train that is grabbing stuff from A, brings it to B, where it is refined and drives to C ... You will always lose money"* — https://steamcommunity.com/app/1066780/discussions/0/3963662255255284920
- **Full load with oversized vehicles** [TF2]: a large train waiting on full load makes the industry think output can't be shipped and production falls — https://steamcommunity.com/app/1066780/discussions/0/1737762134383815858 ; full-load at start of a line can block frequency — https://steamcommunity.com/app/1066780/discussions/0/5757294878584056845
- **Too many vehicles** [TF2]: add vehicles only until all produced cargo moves — https://steamcommunity.com/app/1066780/discussions/0/2639605226297577742
- **Hills** [TF2]: loaded early vehicles crawl on moderate gradients — https://steamcommunity.com/app/1066780/discussions/0/6363065234129022230
- **Catchment** [TF2]: industry demand only counts consumers inside station catchment — https://steamcommunity.com/app/1066780/discussions/0/1737762134383815858 . [TF3-player]: "[Bug] Station range too small" thread — https://steamcommunity.com/app/3493540/discussions/0/588439235122600257/
- **[TF3] Transfers without a warehouse silently fail.** UG-Sam: *"platforms, ports, stations, and so on no longer have any storage. If you're transferring fish from ship to shore, you will need a warehouse to store them, even if they're then immediately boarding a truck."* — https://steamcommunity.com/app/3493540/discussions/0/588438602971706676/ ; farm→bus-stop cargo line "can't work (unload)" — https://steamcommunity.com/app/3493540/discussions/0/588439235122605050/
- **[TF3] Integrated industry docks load slowly**; direct pickup from farms "is also really slow" — same thread; dev blog: integrated docks are the cheapest/fastest to build but load slower than alternatives — https://www.transportfever3.com/news/dev-blog-episode-2-industries-and-cargo-2/
- **[TF3] Mixed-cargo pickup is unbalanced**: trucks set to grain+vegetables took only grain; percentage settings cap load, not split it; players run one line per cargo — https://steamcommunity.com/app/3493540/discussions/0/588439235122605128/ ; trucks unload everything at the first eligible stop — https://steamcommunity.com/app/3493540/discussions/0/588439235122507212/
- **[TF3] Unloading slower than loading** unbalances lines; fix with longer/extra platforms and specialized modules — https://steamcommunity.com/app/3493540/discussions/0/588439235122596143/
- **[TF3] Industry closure** is driven by actual load/unload activity, not connectivity; one industry was saved with "6 days left" when a truck began loading — https://steamcommunity.com/app/3493540/discussions/0/588439235122609050/ . [TF2] rule for comparison: no production/shipment for 10 years → 730-day countdown — https://wiki.transportfever2.com/doku.php?id=gamemanual:industriescargos
- **[TF3-dev] Terraforming hurts town reputation** — https://en.wikipedia.org/wiki/Transport_Fever_3 ; noise "too much serious" complaints — https://steamcommunity.com/app/3493540/discussions/0/588439235122507674/
- **[TF3-SEO, low]** Buying a new fleet while leaving the old one running ("pay both"); subsidies with fines/reputation loss on failure — https://www.whisperofthehouse.com/transport-fever-3/economy-money-guide
- **[TF3-player]** Failed subsidies forced a player into 3 loans — https://steamcommunity.com/app/3493540/discussions/0/588438959484645908/

---

## 3. Mid/late game

- **[TF2] Mode progression**: trucks/horse carts for small production (100–400 units), switch to trains when roads saturate or distances grow — https://steamcommunity.com/app/1066780/discussions/0/2639606103281944418 ; faster trains have higher maintenance so need longer, faster lines — https://steamcommunity.com/app/1066780/discussions/0/1740012252805414784
- **[TF2] Hubs**: a main rail ring with regional hubs; local lines feed hubs, "town delivery trains" distribute, "inter-hub" trains connect hubs; any cargo station type can be a hub; hubs are mid/late-game, direct lines are better early — https://steamcommunity.com/app/1066780/discussions/0/3878222169792832572
- **[TF2] Industry growth**: upgrade needs Production, Shipment and Transport bars ≥75% for ~5 months; shipment is limited by consumer demand in catchment — https://steamcommunity.com/app/1066780/discussions/0/3963662301883287172 ; official wiki: upgrade needs enough cargo delivered to consumers plus enough input — https://wiki.transportfever2.com/doku.php?id=gamemanual:industriescargos ; multi-output industries may need **both** outputs shipped — https://steamcommunity.com/app/1066780/discussions/0/1737762134383815858
- **[TF3-dev] Industry expansion is demand-driven, boosted by workers and optional inputs**: workers from towns boost all industries; fertilizer boosts farms; tools or machine parts boost mines — https://www.transportfever3.com/news/dev-blog-episode-2-industries-and-cargo-2/ , https://store.steampowered.com/app/3493540/
- **[TF3-player]** Workers: factories need only ~10 workers per year; *"a single horse cart will do the job"* if the town is close; check who actually works there — https://steamcommunity.com/app/3493540/discussions/0/588439235122606932/ . Offshore (fish/oil): ferry workers with a passenger boat, or a combined boat carrying fish out and workers back — https://steamcommunity.com/app/3493540/discussions/0/588439235122605105/
- **[TF3-player]** Tools/machines boosters only appear once towns grow and demand more complex goods; early you can only boost with workers — https://steamcommunity.com/app/3493540/discussions/0/588439235122587897/
- **[TF3-SEO, low]** "+70% production at 10 workers" — appears in search snippets, but the cited page doesn't contain it. Unverified.
- **[TF3-dev] Town growth**: 18 levels; XP from passengers and demanded goods; more complex goods → more XP; six ratings (reputation, satisfaction, delivery, traffic, noise, pollution) — https://www.transportfever3.com/news/dev-blog-episode-4-tycoon/ ; late deliveries hurt town development — https://indigo-pearl.prezly.com/transport-fever-3-industries--cargo-revealed
- **[TF2] Town growth**: cargo supply bonus capped at 200% (commercial+industrial); passenger destination growth uncapped — https://steamcommunity.com/app/1066780/discussions/0/594009125553187136/?l=spanish
- **[TF3-player]** Missing industries for town demands can be requested with "prospecting" (rank unlock) — https://steamcommunity.com/app/3493540/discussions/0/588439235122599737/
- **[TF3-player]** Several players set industry closure to "Never" — https://steamcommunity.com/app/3493540/discussions/0/588439235122609050/ . Not an option on our fixed VeryHard (closure x8), so every industry we depend on needs regular load/unload traffic.

---

## 4. Buildings per purpose, depots, hub-and-spoke (coordinator priority)

### Stations / loading
- **[TF3-dev] Three tiers of cargo loading** (Dev Blog Ep.2, https://www.transportfever3.com/news/dev-blog-episode-2-industries-and-cargo-2/):
  - *Integrated industry loading dock*: free/cheapest, ready immediately, but slowest loading. Use for the first low-volume line.
  - *Universal station/platforms*: flexible, lower cost, emissions and noise; slower than specialised.
  - *Specialised platforms/modules per cargo type*: fastest load/unload, higher expense, pollution and noise. Use on high-volume trunk ends.
- **[TF3-player]** Recommended pattern: **farm (or other producer) → short truck shuttle → warehouse + cargo station just outside the industry → trunk line → town goods depot**; *"Don't waste your time with this use a dedicated drop off. Picking up directly from farms is also really slow."* — https://steamcommunity.com/app/3493540/discussions/0/588439235122605050/
- **[TF3-player]** Goods platform in the town centre rather than bus stops for deliveries; lengthen platforms / add extra import and export platforms so several trucks load/unload at once; use specialised platforms and vehicles — https://steamcommunity.com/app/3493540/discussions/0/588439235122596143/
- **[TF2]** Truck station: up to 21 modules (10 per side + 1); each cargo platform has a storage capacity (overflow is lost); one vehicle per terminal at a time — https://steamcommunity.com/app/1066780/discussions/0/2639605226290519432 (search summary). Early trains: size stations and trains to ~80 m — https://steamcommunity.com/app/1066780/discussions/0/2639605226297577742
- **[TF2]** For transfers in a combined station, assign the **cargo** icon of the station to the line (a mixed station acts as two stations) — https://steamcommunity.com/app/1066780/discussions/0/4029095281636634716

### Warehouses (TF3)
- **[TF3-dev]** Required for **every** line-to-line transfer (train↔truck, ship↔truck, train↔train); modular; can be specialised for the four cargo categories; they're separate buildings and can sit e.g. across the road from a dock — https://steamcommunity.com/app/3493540/discussions/0/588438602971706676/ , https://indigo-pearl.prezly.com/transport-fever-3-industries--cargo-revealed
- **[TF3-player]** Warehouses *"suspend time for cargo deliveries"*, so the delivery clock pauses in storage. Use them as buffers before long or slow legs, and to split one source evenly across several towns (*"trucks carrying 15 each to 3 towns"*) — same warehouse thread.
- **[TF3-player]** Use a warehouse as a **collector next to a producer** (fast dedicated shuttle) and as a **distributor in/near towns**; also to extend effective catchment — https://steamcommunity.com/app/3493540/discussions/0/588439235122605050/

### Hub-and-spoke
- **[TF2]** Raw goods → regional hub → processor → back to hub → town delivery lines; inter-hub trunks. Pitfall: you can't choose which consumer gets the cargo when several are reachable, because the network auto-splits — https://steamcommunity.com/app/1066780/discussions/0/3878222169792832572
- **[TF2]** Pay per leg made hubs/transfers *more* lucrative (each leg paid on its own straight-line distance) — https://steamcommunity.com/app/1066780/discussions/0/2872668209264956257
- **[TF3]** Cargo has no destination at production and is routed by your line load/unload settings — https://www.transportfever3.com/news/dev-blog-episode-2-industries-and-cargo-2/ . So TF3 hubs are explicit: warehouse at every transfer, per-cargo lines, manual unload percentages. Pay is split by leg distance and detours aren't paid (player test above), so **hubs add cost without adding revenue**. They're worth it only for consolidation (load factor, fewer stations, deadline buffering) or when a direct line is impossible (water, offshore).
- **[TF3-player]** Passenger hubs: *"Don't link all cities together as your citizens will have to go through your 'hub'"* (TF2-era advice in a TF2 thread) — https://steamcommunity.com/app/1066780/discussions/0/1740012252805414784

### Depots and maintenance
- **[TF2]** No wear-based breakdowns. Maintenance level (Normal/High/Very High) affects condition/emissions; High keeps condition ≥50%. With upkeep vehicles can run indefinitely, so auto-replace matters less — search summary of https://steamcommunity.com/app/1066780/discussions/0/1747898619769669753 and https://twinfinite.net/guides/transport-fever-2-automatic-replacement/
- **[TF3-dev]** Vehicles are repaired **automatically within a radius** of a maintenance building while its capacity isn't exhausted. Dedicated maintenance buildings have a larger radius and capacity than depots. Route lines past them with waypoints — https://www.transportfever3.com/news/dev-blog-episode-3-infrastructure/
- **[TF3-player, from dev video]** A regular depot repairs **up to 6 vehicles at a time**; extra vehicles in range go unserviced; stations can take maintenance modules — https://steamcommunity.com/app/3493540/discussions/0/594034910828718611/
- **[TF3-SEO, low]** Facilities are mode-specific; neglected vehicles get slower, noisier and more polluting — https://www.transportfever3help.wiki/vehicle-maintenance
- **[TF3-player]** In campaign mission 3, a 3-star player sent vehicles home at ~50% condition and swapped in fresh ones (9 vehicles total) — https://steamcommunity.com/app/3493540/discussions/0/588439235122603716/
- **[TF3-player]** Vehicles don't seem to "age" as in TF2. There's a setting to stop vehicles going obsolete — https://steamcommunity.com/app/3493540/discussions/0/588439235122602076/ , https://steamcommunity.com/app/3493540/discussions/0/588439235122582240/

---

## 5. Line management rules of thumb

- **Load factor** [TF2]: aim for >70–80% average load and no overcrowded stations — https://steamcommunity.com/app/1066780/discussions/0/5757294878584056845 ; main money lines ≤25% empty — https://steamcommunity.com/app/1066780/discussions/0/3963662255255284920
- **Frequency** [TF2]: intercity coach frequency under 1–2 min — https://steamcommunity.com/app/1066780/discussions/0/1740012252805414784 ; TF2 waiting time doesn't count against travel time, so low-frequency/high-capacity is fine for cargo — https://steamcommunity.com/app/1066780/discussions/0/5757294878584056845 . [TF3-dev]: passenger satisfaction depends on station waits and trip time (town ratings), so frequency matters again for passengers — https://www.transportfever3.com/news/dev-blog-episode-4-tycoon/
- **Load modes** [TF2]: full load at a single-source producer with right-sized vehicles; avoid full load with oversized vehicles (it depresses production) and on shared/multi-stop lines — https://steamcommunity.com/app/1066780/discussions/0/1737762134383815858
- **Train size** [TF3-player]: cheapest ~50 km/h loco with 4–8 wagons early; *"4-6 car trains are best unless it's an extra short line"* — https://steamcommunity.com/app/3493540/discussions/0/588439235122525543/
- **Trucks per line** [TF3-SEO, low]: start with 2–4 per cargo line and grow with production — https://www.xmodhub.com/info/guides/transport-fever-3-beginner-guide/
- **One cargo per line** [TF3-player]: separate lines per cargo give exact balance and full trucks — https://steamcommunity.com/app/3493540/discussions/0/588439235122605128/
- **Inflation** [TF2]: vehicle and infrastructure costs and running costs rise over time, so marginal early lines can go negative later — search summary, https://steamcommunity.com/app/1066780/discussions/0/3189115186362698359 . [TF3-player, disputed]: calendar speed ≥1 makes expenses outgrow revenue; negative calendar speed (cities grow faster than time) helps cash flow. Another player disputes this — https://steamcommunity.com/app/3493540/discussions/0/588438959484645908/
- **Replacement** [TF2]: replace for speed/capacity upgrades, not age. [TF3]: replace when condition is low and depot coverage can't keep up; don't run old and new fleets in parallel ([TF3-SEO, low], economy guide above).

---

## 6. TF3-specific systems

- **Warehouses, cargo deadlines, loading tiers, boosters**: see sections 0, 3 and 4.
- **Company rank** [TF3-dev]: Junior → … → Tycoon, scored by world population growth; higher ranks unlock bonuses and HQ expansions — https://www.transportfever3.com/news/dev-blog-episode-4-tycoon/ . [TF3-SEO, low]: Junior = HQ; Mechanic = medium HQ + Amphitheater; Engineer = corner HQ + "Greenify Industry"; later resource prospecting, marketing campaigns, landmarks. Example target: 2,912 population for the next rank — https://www.whisperofthehouse.com/transport-fever-3/economy-money-guide (via search snippet). HQ in the central town makes it the "capital" ([TF3-SEO, low] https://www.whisperofthehouse.com/transport-fever-3/beginner-guide).
- **Municipal areas** [TF3-dev]: map divided into municipal areas around towns with bonuses or penalties (pollution, deforestation) — Dev Blog Ep.4.
- **Loans/contracts** [TF3-dev]: "dynamic loan and contract system" — Steam store. Player behaviour: take one offer to start, then refinance into better offers as they appear (section 1).
- **Subsidies/missions** [TF3-SEO, low]: example subsidy with $3.15M upfront, "2× income in <town> for 5 years", $8.4M completion reward, fine and reputation loss on failure — https://www.whisperofthehouse.com/transport-fever-3/economy-money-guide . [TF3-player]: failed subsidies led to extra loans (section 2).
- **Town ratings** [TF3-dev]: reputation (demolition/earthworks), satisfaction (waits, slow trips, comfort), delivery (late cargo), traffic, noise (stations near housing), pollution (municipal-wide). Red ratings shrink the XP multiplier ([TF3-SEO] wording, https://transport-fever.wiki/guides/city-growth-explained). All of these can be tuned or disabled in custom difficulty — https://www.transportfever3.com/news/tycoon-gameplay-overview-revealed/
- **Noise/pollution** [TF3-dev]: specialised platforms cost more and add pollution and noise. Universal stations are cleaner (Dev Blog Ep.2).

---

## 7. Concrete numbers reported

| Number | Game | Source |
|---|---|---|
| Line ROI ≥30%/yr "readily doable"; loan interest ~2% negligible | TF2 | https://steamcommunity.com/app/1066780/discussions/0/6363065234129022230 |
| Long oil line: $750k/yr profit by 1860, $12–14M/yr by 1900 | TF2 | https://steamcommunity.com/app/1066780/discussions/0/3963662255255284920 |
| Grain→food→town route: ~$100–150k/yr | TF2 | same |
| Hard, $500k loan start: $20–30k/yr per line by 1855 | TF2 | https://steamcommunity.com/app/1066780/discussions/0/1740012252805414784 |
| ~$500k infra per line, $3.5M reserved for vehicles | TF2 | https://steamcommunity.com/app/1066780/discussions/0/3963662255255284920 |
| Upgrade: 3 bars ≥75% for ~5 months | TF2 | https://steamcommunity.com/app/1066780/discussions/0/3963662301883287172 |
| Closure: 10 yr inactive + 730-day countdown | TF2 | https://wiki.transportfever2.com/doku.php?id=gamemanual:industriescargos |
| Two short train lines making $2M (hard) | TF3 | https://steamcommunity.com/app/3493540/discussions/0/588439235122525543/ |
| Start on hard at −$3.7M / −$3.9M in 1900 | TF3 | https://steamcommunity.com/app/3493540/discussions/0/588438959484645908/ |
| $9–10M loan → bus loop + 1 train w/ 2 coaches, profitable within a year | TF3 | https://steamcommunity.com/app/3493540/discussions/0/588439235122525543/ |
| Loan repayment ~$5M/yr vs ~$1M/yr profit | TF3 | https://steamcommunity.com/app/3493540/discussions/0/588439235122490211/ |
| Oil $652k vs 5 passengers $104k (anecdote) | TF3 | same |
| Cargo price decays from ~100 to ~200 days after pickup | TF3 | https://steamcommunity.com/app/3493540/discussions/0/588439235122597876/ |
| ~10 workers/yr per factory | TF3 | https://steamcommunity.com/app/3493540/discussions/0/588439235122606932/ |
| Depot repairs ≤6 vehicles simultaneously | TF3 | https://steamcommunity.com/app/3493540/discussions/0/594034910828718611/ |
| Industry closure countdown observed at "6 days left" | TF3 | https://steamcommunity.com/app/3493540/discussions/0/588439235122609050/ |
| Loan examples $9M/48mo/7%, $12M/108mo/10% | TF3 (low) | https://www.whisperofthehouse.com/transport-fever-3/economy-money-guide |
| Normal difficulty: debt-free with ~$6M cash after ~11 years | TF3 (low) | same |
| 32 industry types, 37 cargo types | TF3-dev | https://indigo-pearl.prezly.com/transport-fever-3-industries--cargo-revealed |

Passenger volume by town size: no reliable published figures for TF2 or TF3 were found.

---

## Implications for our optimizer

1. **Calibrate the payment function in-game before trusting any ranking** (high). Run three controlled tests on the save: (a) the same cargo over two straight-line distances; (b) two cargo types (e.g. logs vs lumber) over the same distance; (c) a two-leg transfer via a warehouse versus a direct delivery. Fit `pay = base[cargo] * g(d_od) * decay(t)`, with leg shares split by leg distance.
2. **Model cargo pay per cargo type, not one global rate** (high). The dev statement says complexity dominates, so processed goods (lumber, bricks, fuel) should rank above raw goods (logs, clay, crude) per unit.
3. **Pay transfers on origin→final-destination straight-line distance, split across legs** (medium). Don't sum independent per-leg payments, and don't credit detours.
4. **Add a lateness decay per cargo** (medium). Full pay roughly until ~100 days in transit, falling until ~200 days (player report). Pre-release deadlines run from ~10 months (meat) to ~4 years (stone). Reject perishable routes whose transit plus waiting time exceeds the deadline at horse-cart speeds.
5. **Count waiting time in warehouses as paused for the deadline** (medium). This makes warehouse buffers useful before slow legs.
6. **Every transfer node needs a warehouse in the build cost and upkeep** (high). Station-only transfers are invalid plans.
7. **Prefer short, high-turnover cargo shuttles; prefer long, fast lines for passengers** (medium, depends on rule 1's result). If the distance term is weak, cargo profit per vehicle-year is roughly trips/yr × capacity × base[cargo].
8. **Evaluate passenger intercity lines with in-town bus feeders as first-class early options** (medium). Several TF3 hard starts were profitable from year one on passenger rail.
9. **Producer output is the binding constraint on VeryHard (x0.5)** (high). Size fleets to actual output, and include a worker feeder (one horse cart or bus from a nearby town, ~10 workers/yr) as a cheap production booster for every served industry.
10. **Don't let a served industry go idle** (high). Closure is triggered by lack of load/unload activity and VeryHard closure is x8, so require a minimum service frequency at every industry the plan depends on.
11. **Two-way loading** (high in TF2, medium in TF3). Score chains where one vehicle type carries cargo both ways (logs→sawmill, lumber back; crude→refinery, fuel back) and penalise empty backhauls fully in operating cost.
12. **One cargo per line; avoid mixed-cargo pickup** (high). Avoid A→B(process)→C multi-stop trains.
13. **Station tiers** (medium). Use the integrated dock for the first low-volume line. Upgrade to a specialised platform plus warehouse when a line's vehicles queue for loading. Add extra unload bays because unloading is slower than loading.
14. **Amortise infrastructure upkeep over enough vehicles** (high). Reject plans with one vehicle per station pair unless the line's NPV covers station and track upkeep.
15. **Maintenance coverage** (medium). Every depot covers ≤6 simultaneous repairs. Budget one depot per ~6 concurrently-in-range vehicles, or a dedicated maintenance building for larger fleets. Make lines pass within range, adding waypoints if needed.
16. **Loan policy** (medium). Pick the offer whose installment fits the projected first-year cash flow, not the biggest amount. Plan to refinance into better offers later. Model installments (principal + interest) as monthly cash outflow, because bankruptcy risk comes from installments, not interest.
17. **Hills and gradients** (medium). Penalise loaded steam and horse routes over grades heavily. Flat corridors are worth longer distances.
18. **Inflation** (low). Re-evaluate line profitability each year with inflated costs. Marginal early lines can turn negative, so prefer lines with ≥30% ROI margin.
19. **Town ratings and noise** (low). Penalise stations and specialised platforms inside residential areas, and avoid heavy terraforming near towns.

## Contradictions with our current model

1. **"All cargos pay the same 3.8 $/unit/m"** contradicts UG staff (Sam, 23.04.2026 and 11.09.2026): cargo pay depends on cargo type/complexity, and distance matters little or not at all. A post-release player test says distance still matters but only origin→destination. Either way, a single uniform rate is wrong.
2. **"Per leg"** contradicts the player test: total payout is split between vehicles by distance share, and intermediate detour stops aren't paid. TF2 paid per leg independently; TF3 apparently does not.
3. **No time component**: TF3 has per-cargo delivery deadlines. Payment reportedly decays between ~100 and ~200 days, and late delivery also hurts the town's Delivery rating. The model ignores transit and waiting time in revenue.
4. **Speed**: consistent with the model. In TF3 top speed is reportedly not in the pay formula (unlike TF2). Speed matters only through trips per year and deadlines.
5. **Passengers**: devs say long-distance fast passenger travel "will continue to pay well", and players report profitable passenger rail on hard. Passenger pay may depend on speed/comfort and vehicle price, so it shouldn't share the cargo rate.
6. **Transfers**: TF3 stations have no storage, so a transfer needs a warehouse (capex + upkeep) and can't be modelled as free.
7. **Day length**: a player quotes 2000 ms/day (365 days ≈ 730 s), while our model uses 1461 s/year (≈4000 ms/day). Check the save's calendar speed. If deadlines are in game days, real-time deadlines scale with it; players also claim inflation interacts with calendar speed.
8. **Industry production**: TF3 production is demand-driven and boosted by workers/inputs. A static production figure x0.5 underestimates what a worker feeder can unlock and overestimates unboosted output growth.
9. **Vehicle aging**: TF3 seems to have condition wear repaired by in-radius maintenance (capacity-limited), not TF2-style age/obsolescence. Model operating cost should include maintenance coverage, not age-based replacement.
10. **TF2 constant term**: TF2 paid on (300 m + d), which made short trips relatively better. If TF3 keeps any distance term, check for a fixed offset as well.

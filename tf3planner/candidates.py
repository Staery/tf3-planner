"""Shared project-candidate interface for all generators (cargo links, passengers, multimodal
chains via warehouses, upgrades). planner.py consumes ProjectCandidate objects from every
generator, so each generator module only has to produce these.

Conventions
- Money per game year; capex one-off; all prices final (difficulty already applied by the game).
- `uses_supply` / `adds_supply` / `uses_demand` let the planner keep the cargo balance:
  keys are (node_index, cargo_key) -> units/year.
- `pair_keys` are infrastructure keys (min_node, max_node, mode) that the project builds; once
  built, later projects that reuse them get the route part for free (generators receive the
  set of built keys and should drop route build cost / maintenance for them).
- `station_keys` are (node_index, mode) stations the project builds (free for later projects).
- Texts (title/details) are Russian; they go straight to plan.json.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class VehicleLine:
    vehicle_id: int
    name: str
    file: str
    count: int
    price: float


@dataclass
class ProjectCandidate:
    kind: str                          # "cargo" | "passenger" | "workers" | "multimodal" | "upgrade"
    mode: str                          # "road" | "rail" | "water" | "tram" | "air" | "multi"
    title: str
    details: str
    nodes: list[int]                   # node indices served (ends first)
    cargo: str | None
    volume: float                      # units or passengers per year
    income: float                      # per year
    running: float                     # vehicles per year
    maintenance: float                 # infrastructure per year
    vehicle_capex: float
    infra_capex: float
    vehicles: list[VehicleLine] = field(default_factory=list)
    vehicle_life_years: float = 30.0
    geometry: list[list[float]] | None = None      # [[x, y], ...]; multi-leg: concatenated
    legs: list[dict] = field(default_factory=list)  # optional per-leg breakdown for multimodal
    pair_keys: list[tuple] = field(default_factory=list)
    station_keys: list[tuple] = field(default_factory=list)
    uses_supply: dict = field(default_factory=dict)
    adds_supply: dict = field(default_factory=dict)
    uses_demand: dict = field(default_factory=dict)
    boosts: dict = field(default_factory=dict)      # (node_index, kind) -> factor, e.g. workers
    meta: dict = field(default_factory=dict)

    @property
    def capex(self) -> float:
        return self.vehicle_capex + self.infra_capex

    @property
    def profit(self) -> float:
        return self.income - self.running - self.maintenance

    @property
    def payback_years(self) -> float:
        return self.capex / self.profit if self.profit > 0 else math.inf

    @property
    def depreciation(self) -> float:
        return self.vehicle_capex / max(self.vehicle_life_years, 1) + self.infra_capex / 30.0

    @property
    def not_loss_making(self) -> bool:
        return self.profit - self.depreciation >= 0

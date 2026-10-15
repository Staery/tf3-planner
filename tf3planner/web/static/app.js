// TF3 planner web map: terrain, towns, industries, supply chains, routes and the plan timeline.
// Vanilla JS + Canvas 2D, no dependencies. World coordinates: metres, y points north.
"use strict";

// ------------------------------------------------------------------ constants

const KIND_COLORS = { rail: "#e66767", road: "#eda100", water: "#4fb8f0", air: "#d55181" };
const KIND_DASH = { rail: [], road: [], water: [10, 7], air: [2, 7] };
const KIND_LABEL = { rail: loc("Ж/д"), road: loc("Авто"), water: loc("Водный"), air: loc("Авиа") };
const ACTION_LABEL = {
  build_line: loc("Новая линия"), buy_vehicles: loc("Покупка транспорта"), take_loan: loc("Кредит"),
  repay_loan: loc("Погашение кредита"), replace_vehicles: loc("Замена транспорта"),
  upgrade_track: loc("Модернизация пути"), note: loc("Заметка"),
};
const MONTHS = [loc("янв"), loc("фев"), loc("мар"), loc("апр"), loc("май"), loc("июн"), loc("июл"), loc("авг"), loc("сен"), loc("окт"), loc("ноя"), loc("дек")];
const LAND_USE_LABEL = { residential: loc("жилая"), commercial: loc("коммерция"), industrial: loc("промышл.") };

// Known industry types get fixed colours; unknown ones take the next spare colour in order.
const TYPE_COLORS = {
  farm: "#f2d14c", livestock_farm: "#f59f6b", forest: "#7bdc8f", saw_mill: "#b9764a",
  clay_pit: "#e0b07a", bricks_works: "#d0503f", fishing_grounds: "#5fd0f5",
  oil_platform: "#2b2b2b", oil_refinery: "#a56be8",
};
const SPARE_COLORS = ["#ff8ad8", "#9fe6c9", "#c8c8ff", "#ffe0a3", "#8fb3ff", "#ffa3a3", "#d1ff8f", "#bdbdbd"];

// per-leg / per-mode line styles (multimodal chains and passenger networks draw every leg separately)
const MODE_COLORS = { ...KIND_COLORS, tram: "#f07fb8", multi: "#d0d0d0" };
const MODE_DASH = { ...KIND_DASH, tram: [3, 4] };
const MODE_LABEL = { rail: loc("Ж/д"), road: loc("Грузовики"), water: loc("Судно"), tram: loc("Трамвай"), air: loc("Авиа") };
const ROLE_LABEL = { feeder: loc("Подвоз"), trunk: loc("Магистраль"), final: loc("Развоз"), line: loc("Линия") };
function modeLabel(mode, pax) { return pax && mode === "road" ? loc("Автобус") : pax && mode === "rail" ? loc("Поезд") : (MODE_LABEL[mode] || mode || ""); }

// Building types drawn on the map ("Здания" layer). Icon = emoji on a light badge with a coloured border;
// square badge = cargo / infrastructure, round badge = passenger stop. catch = default catchment radius, m.
const BT = {
  rail_cargo: { label: loc("Ж/д грузовая станция"), icon: "🚂", color: "#e66767", round: false, catch: 175 },
  rail_pass: { label: loc("Ж/д вокзал (пассажиры)"), icon: "🚉", color: "#e66767", round: true, catch: 175 },
  road_cargo: { label: loc("Грузовая автостанция"), icon: "🚚", color: "#eda100", round: false, catch: 175 },
  bus: { label: loc("Автобусная остановка / автовокзал"), icon: "🚌", color: "#eda100", round: true, catch: 160 },
  tram: { label: loc("Трамвайная остановка"), icon: "🚋", color: "#f07fb8", round: true, catch: 160 },
  harbor: { label: loc("Порт (гавань)"), icon: "⚓", color: "#4fb8f0", round: false, catch: 200 },
  warehouse: { label: loc("Склад (перевалка)"), icon: "📦", color: "#b98bf0", round: false, catch: null },
  depot: { label: loc("Депо"), icon: "🏠", color: "#9aa0a6", round: false, catch: null },
  maint: { label: loc("Здание техобслуживания"), icon: "🔧", color: "#9aa0a6", round: false, catch: null },
  bridge: { label: loc("Мост"), icon: "", color: "#4fb8f0", round: false, catch: null, small: true },
  tunnel: { label: loc("Тоннель"), icon: "", color: "#c08a55", round: false, catch: null, small: true },
  demolition: { label: loc("Снос городских зданий"), icon: "", color: "#ff4b3e", round: false, catch: null, small: true },
  prospect: { label: loc("Разведка ресурса (новое предприятие)"), icon: "⛏", color: "#7bdc8f", round: true, catch: null },
};
const BT_ORDER = ["rail_cargo", "rail_pass", "road_cargo", "bus", "tram", "harbor", "warehouse", "depot", "maint", "bridge", "tunnel", "demolition", "prospect"];
// demolition of town buildings (docs/demolition.md): the game blocks demolition below reputation 2/6 and new
// stations below 1/6 in that town; the planner / dossier warn above a 50 % loss in one project
const DEMO_COLOR = "#ff4b3e";
const DEMO_WARN = 0.5;
const BT_GROUP = { rail_cargo: loc("Станции"), rail_pass: loc("Станции"), road_cargo: loc("Станции"), bus: loc("Остановки"), tram: loc("Остановки") };
const EMOJI_FONT = '"Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", sans-serif';
const WH_CLASS = { liquid: loc("наливной"), goods: loc("штучный"), bulk: loc("навалочный"), universal: loc("универсальный"), UNIVERSAL: loc("универсальный") };

const TOWN_FILL = "#e34948";
const CHAIN_COLOR = "rgba(255,255,255,0.30)";
const CHAIN_HI = "#ffffff";

// ------------------------------------------------------------------ state

const S = {
  game: null, plan: null, routes: [], terrain: null,
  view: { cx: 0, cy: 0, scale: 0.03 }, fitScale: 0.03,
  layers: { towns: true, industries: true, chains: false, routes: false, plan: true, buildings: true },
  showBuilt: false,       // year view: also draw lines built in earlier years (faint)
  listAll: false,
  // buildings: version bumps when dossiers arrive (per-step caches are rebuilt), visible set cache, hit boxes
  bVer: 0, vis: null, visKey: null, bHits: [], selBuilding: null, projYearOnly: true,
  year: null, minYear: null, maxYear: null,
  selectedStep: null, hover: null, flash: null,
  chains: [], chainsBy: new Map(),
  townById: new Map(), indById: new Map(), cargoByKey: new Map(),
  typeColor: new Map(),
  steps: [],
  // projects = build_line steps + steps that refer to them via `line`
  projects: [], projectByKey: new Map(),
  currentProject: null,   // project id (line id, e.g. "L5")
  focus: true,            // focus mode: show only the current project on the map
  done: new Set(),        // signatures of projects marked as done
  hideDone: false, projFilter: "", tab: "projects",
};

// localStorage may be unavailable (private mode, blocked storage) — never let it break the page.
const store = {
  get(k, def) {
    try { const v = localStorage.getItem(k); return v === null ? def : JSON.parse(v); } catch { return def; }
  },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* ignore */ } },
};
const LS = { current: "tf3.currentProject", focus: "tf3.focus", done: "tf3.doneProjects", hideDone: "tf3.hideDone",
  tab: "tf3.panelTab", cashflowCollapsed: "tf3.cashflowCollapsed", planCollapsed: "tf3.planCollapsed",
  showBuilt: "tf3.showBuilt", projYearOnly: "tf3.projYearOnly" };

// Scroll `el` into view inside its scrollable `box` only (Element.scrollIntoView would also scroll the page).
function scrollIntoBox(el, box) {
  if (!el || !box) return;
  const e = el.getBoundingClientRect(), b = box.getBoundingClientRect();
  if (e.top < b.top) box.scrollTop -= b.top - e.top + 4;
  else if (e.bottom > b.bottom) box.scrollTop += e.bottom - b.bottom + 4;
}

const $ = (id) => document.getElementById(id);
const canvas = $("map");
const ctx = canvas.getContext("2d");
let W = 0, H = 0, dpr = 1, dirty = true;

// ------------------------------------------------------------------ formatting

const nf = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
function fmtMoney(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const a = Math.abs(v), s = v < 0 ? "−" : "";
  if (a >= 1e9) return s + (a / 1e9).toFixed(2).replace(".", ",") + loc(" млрд");
  if (a >= 1e6) return s + (a / 1e6).toFixed(2).replace(".", ",") + loc(" млн");
  if (a >= 1e3) return s + Math.round(a / 1e3) + loc(" тыс");
  return s + nf.format(a);
}
function fmtKm(m) { return (m / 1000).toFixed(m < 10000 ? 1 : 0).replace(".", ",") + loc(" км"); }
function fmtYears(y) {
  if (y === null || y === undefined || !isFinite(y)) return "—";
  return y.toFixed(y < 10 ? 1 : 0).replace(".", ",") + loc(" г.");
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function cargoName(key) {
  if (!key) return "";
  const c = S.cargoByKey.get(key);
  return c ? c.name : key;
}

// ------------------------------------------------------------------ transforms

function w2s(x, y) {
  return [(x - S.view.cx) * S.view.scale + W / 2, H / 2 - (y - S.view.cy) * S.view.scale];
}
function s2w(sx, sy) {
  return [S.view.cx + (sx - W / 2) / S.view.scale, S.view.cy - (sy - H / 2) / S.view.scale];
}
function zoomFactor() { return S.view.scale / S.fitScale; }

// ------------------------------------------------------------------ data loading

async function loadJSON(url) {
  const r = await apiFetch(url, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

async function init() {
  setupUI();
  resize();
  try {
    const [game, plan, routes] = await Promise.all([loadJSON("/api/game"), loadJSON("/api/plan"),
      loadJSON("/api/routes").catch(() => [])]);
    S.game = game;
    S.routes = Array.isArray(routes) ? routes : (routes.routes || []);
    indexGame();
    computeChains();
    buildLegend();
    fitAll(false);
    setPlan(plan);
    $("status").textContent = loc("{0} г. · {1} городов · {2} предприятий", game.year, game.towns.length, game.industries.length);
    restoreProjectState();
    applyUrlState();
    loadDossiers();
    loadNetwork();
  } catch (e) {
    $("status").textContent = loc("Ошибка загрузки: ") + e.message;
    console.error(e);
  }
  const img = new Image();
  img.onload = () => { S.terrain = img; dirty = true; };
  img.onerror = () => { $("status").textContent += loc(" · рельеф не загрузился"); };
  img.src = "/api/terrain.png";
  requestAnimationFrame(loop);
}

// Optional URL state, e.g. /?year=1902&step=s7&layers=towns,industries,chains,plan&q=Дубна
function applyUrlState() {
  const q = new URLSearchParams(location.search);
  if (q.has("layers")) {
    const on = new Set(q.get("layers").split(","));
    document.querySelectorAll("#layers input[data-layer]").forEach((cb) => {
      cb.checked = on.has(cb.dataset.layer);
      S.layers[cb.dataset.layer] = cb.checked;
    });
  }
  if (q.has("built")) {
    S.showBuilt = q.get("built") !== "0";
    $("show-built").checked = S.showBuilt;
  }
  if (q.has("year")) setYear(+q.get("year"));
  if (q.has("tab")) setTab(q.get("tab"));
  if (q.has("at")) {
    // ?at=x,y[,scale px/m] — centre the view (sharing / screenshots)
    const [x, y, sc] = q.get("at").split(",").map(Number);
    if (Number.isFinite(x) && Number.isFinite(y)) { anim = null; Object.assign(S.view, { cx: x, cy: y, scale: clampScale(sc || 0.2) }); }
  }
  if (q.has("step")) { setTab("steps"); selectStep(q.get("step")); }
  if (q.has("q")) {
    const input = $("search");
    input.value = q.get("q");
    input.dispatchEvent(new Event("input"));
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
  }
  if (q.has("hover")) {
    // debug aid: show the tooltip of an entity by name
    const name = q.get("hover");
    const t = S.game.towns.find((x) => x.name === name);
    const i = S.game.industries.find((x) => x.name === name);
    const obj = t || i;
    if (obj) {
      S.hover = { kind: t ? "town" : "industry", obj };
      const [sx, sy] = w2s(obj.x, obj.y);
      showTooltip(S.hover, sx, sy);
    }
  }
  dirty = true;
}

function indexGame() {
  const g = S.game;
  for (const c of g.cargos) S.cargoByKey.set(c.key, c);
  for (const t of g.towns) S.townById.set(t.id, t);
  for (const i of g.industries) S.indById.set(i.id, i);
  S.maxTownTotal = Math.max(1, ...g.towns.map((t) => t.total));
  let spare = 0;
  const types = [...new Set(g.industries.map((i) => i.type))];
  for (const t of types) S.typeColor.set(t, TYPE_COLORS[t] || SPARE_COLORS[spare++ % SPARE_COLORS.length]);
}

// ------------------------------------------------------------------ supply chains

// For each output cargo of each industry: the 2 nearest consuming industries and the 2 nearest towns
// that ask for that cargo.
function computeChains() {
  const g = S.game, chains = [];
  S.noConsumer = new Map();  // industry id -> [cargo keys without any consumer on the map]
  for (const p of g.industries) {
    for (const out of p.outputs) {
      const key = out.key;
      const inds = g.industries.filter((c) => c !== p && c.inputs.some((x) => x.key === key))
        .map((c) => ({ ent: c, type: "industry", d: Math.hypot(c.x - p.x, c.y - p.y) }))
        .sort((a, b) => a.d - b.d).slice(0, 2);
      const towns = g.towns.filter((t) => Object.values(t.needs).some((l) => l.some((x) => x.key === key)))
        .map((t) => ({ ent: t, type: "town", d: Math.hypot(t.x - p.x, t.y - p.y) }))
        .sort((a, b) => a.d - b.d).slice(0, 2);
      if (!inds.length && !towns.length) {
        if (!S.noConsumer.has(p.id)) S.noConsumer.set(p.id, []);
        S.noConsumer.get(p.id).push(key);
      }
      for (const c of [...inds, ...towns]) chains.push({ from: p, to: c.ent, toType: c.type, cargo: key, d: c.d });
    }
  }
  S.chains = chains;
  const by = new Map();
  const add = (k, c) => { if (!by.has(k)) by.set(k, []); by.get(k).push(c); };
  for (const c of chains) { add("i" + c.from.id, c); add((c.toType === "town" ? "t" : "i") + c.to.id, c); }
  S.chainsBy = by;
}

// ------------------------------------------------------------------ plan

function setPlan(plan) {
  S.plan = plan || { steps: [], cashflow: [] };
  const steps = (S.plan.steps || []).slice();
  steps.forEach((s, i) => { s._idx = i; s.id = s.id ?? "step" + i; });
  steps.sort((a, b) => (a.year - b.year) || ((a.month || 1) - (b.month || 1)) || ((a.order || 0) - (b.order || 0)) || (a._idx - b._idx));
  for (const s of steps) {
    s._from = resolvePoint(s.from);
    s._to = resolvePoint(s.to);
    s._stops = (s.stops || []).map(resolvePoint).filter(Boolean);
  }
  // `line` may hold a build_line step id ("s2") or the line id that the build_line step carries ("L5")
  const byId = new Map(steps.map((s) => [s.id, s]));
  for (const s of steps) {
    if (s.action === "build_line" && s.line != null && !byId.has(String(s.line))) byId.set(String(s.line), s);
  }
  for (const s of steps) s._path = stepPath(s, byId);
  S.steps = steps;
  buildProjects();
  setupItemYears();
  S.routeYears = null;
  S.visKey = S.ysKey = null;

  const years = [...steps.map((s) => s.year), ...(S.plan.cashflow || []).map((c) => c.year)].filter(Number.isFinite);
  S.minYear = years.length ? Math.min(...years) : S.game.year;
  S.maxYear = years.length ? Math.max(...years) : S.game.year;
  const slider = $("year");
  slider.min = S.minYear; slider.max = S.maxYear;
  slider.disabled = S.minYear === S.maxYear;
  setYear(steps.length ? steps[0].year : S.minYear);

  $("plan-title").innerHTML = loc('<span class="chev">▾</span> План') + (S.plan.example ? loc(' <span class="badge">ПРИМЕР</span>') : "") +
    (S.plan.source ? ` <span class="tt-muted" style="font-weight:400;font-size:12px">${esc(S.plan.source)}</span>` : "");
  const gen = S.plan.generated ? loc(" · создан {0}", esc(String(S.plan.generated).replace("T", " "))) : "";
  $("plan-objective").innerHTML = esc(S.plan.objective || "") + `<span class="tt-muted">${gen}</span>`;
  renderSummary();
}

function resolvePoint(p) {
  if (!p) return null;
  let x = p.x, y = p.y, name = p.name;
  if ((x === undefined || y === undefined) && p.id !== undefined) {
    const e = p.type === "town" ? S.townById.get(p.id) : S.indById.get(p.id);
    if (e) { x = e.x; y = e.y; name = name || e.name; }
  }
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return { type: p.type || "point", id: p.id, name: name || "", x, y };
}

// Polyline in world coordinates for a step: its geometry, else the referenced line's geometry,
// else stops, else a straight from -> to segment.
function stepPath(s, byId) {
  if (Array.isArray(s.geometry) && s.geometry.length >= 2) return s.geometry.map((p) => [p[0], p[1]]);
  if (s.line != null && byId.has(String(s.line))) {
    const ref = byId.get(String(s.line));
    if (ref !== s) { s._refOnly = true; s._ref = ref; return stepPath(ref, byId); }
  }
  if (s._stops && s._stops.length >= 2) return s._stops.map((p) => [p.x, p.y]);
  if (s._from && s._to) return [[s._from.x, s._from.y], [s._to.x, s._to.y]];
  return null;
}

function renderSummary() {
  const sm = S.plan.summary || {};
  const labels = { start_money: loc("Старт"), end_money: loc("Итог"), total_capex: loc("Вложения"), lines: loc("Линий"),
    horizon: loc("Горизонт"), total_income: loc("Доход"), total_profit: loc("Прибыль"), final_money: loc("Итог"), loans: loc("Кредиты") };
  const items = [];
  for (const [k, v] of Object.entries(sm)) {
    if (items.length >= 6) break;
    let val;
    if (Array.isArray(v)) val = v.join("–");
    else if (typeof v === "number") val = /money|capex|income|profit|cost|loan/.test(k) ? fmtMoney(v) : nf.format(v);
    else if (typeof v === "string" && v.length <= 24) val = v;
    else continue;
    items.push(`<div class="stat"><div class="k">${esc(labels[k] || k)}</div><div class="v">${esc(val)}</div></div>`);
  }
  items.push(loc("<div class=\"stat\"><div class=\"k\">Шагов</div><div class=\"v\">{0}</div></div>", S.steps.length));
  $("plan-summary").innerHTML = items.join("");
  if (sm.note) $("plan-summary").insertAdjacentHTML("beforeend",
    `<div class="plan-note" title="${esc(sm.note)}">${esc(sm.note)}</div>`);
}

function setYear(y) {
  y = Math.max(S.minYear, Math.min(S.maxYear, Math.round(y)));
  const changed = S.year !== y;
  S.year = y;
  $("year").value = y;
  $("year-label").textContent = y;
  renderSteps();
  drawCashflow();
  if (changed && S.selBuilding && !focusProject()) S.selBuilding = null;
  renderYearBox();
  if (S.projects.length && changed) renderProjects();
  renderBuildingsTab();
  dirty = true;
}

function stepStatus(s) {
  if (s.year < S.year) return "past";
  if (s.year === S.year) return "current";
  return "future";
}

function renderSteps() {
  const list = $("steps");
  const steps = S.listAll ? S.steps : S.steps.filter((s) => s.year === S.year);
  $("steps-title").textContent = S.listAll ? loc("Все шаги ({0})", steps.length) : loc("Шаги {0} г. ({1})", S.year, steps.length);
  if (!steps.length) {
    list.innerHTML = loc("<li class=\"empty no-geo\" style=\"background:none;border:none\">В этом году шагов нет.</li>");
    return;
  }
  list.innerHTML = steps.map((s) => {
    const color = KIND_COLORS[s.kind] || "#898781";
    const date = `${s.year}${s.month ? " · " + MONTHS[(s.month - 1) % 12] : ""}`;
    const meta = [ACTION_LABEL[s.action] || s.action, KIND_LABEL[s.kind], s.cargo ? cargoName(s.cargo) : null]
      .filter(Boolean).join(" · ");
    const route = s._from && s._to ? `<div class="step-meta">${esc(s._from.name)} → ${esc(s._to.name)}</div>` : "";
    const net = (s.income_per_year || 0) - (s.running_cost_per_year || 0);
    const nums = [];
    if (s.cost) nums.push(loc("<span>Стоимость <b>{0}</b></span>", fmtMoney(s.cost)));
    if (s.income_per_year) nums.push(loc("<span>Доход/год <b class=\"pos\">{0}</b></span>", fmtMoney(s.income_per_year)));
    if (s.running_cost_per_year) nums.push(loc("<span>Расходы/год <b>{0}</b></span>", fmtMoney(s.running_cost_per_year)));
    if (s.income_per_year) nums.push(loc("<span>Чистыми <b class=\"{0}\">{1}</b></span>", net >= 0 ? "pos" : "neg", fmtMoney(net)));
    if (s.roi_years !== null && s.roi_years !== undefined) nums.push(loc("<span>Окупаемость <b>{0}</b></span>", fmtYears(s.roi_years)));
    if (s.money_after !== undefined && s.money_after !== null)
      nums.push(loc("<span>Деньги после <b class=\"{0}\">{1}</b></span>", s.money_after < 0 ? "neg" : "", fmtMoney(s.money_after)));
    const veh = (s.vehicles || []).length
      ? loc("<div class=\"step-vehicles\">Транспорт: {0}</div>", s.vehicles.map((v) => `${v.count}× ${esc(v.name || v.model)}${v.price ? ` (${fmtMoney(v.price)})` : ""}`).join(", "))
      : "";
    const cls = [s.id === S.selectedStep ? "selected" : "", s._path ? "" : "no-geo"].join(" ");
    return `<li data-id="${esc(s.id)}" class="${cls}" style="border-left-color:${color}">
      <div class="step-head"><span class="step-date">${date}</span><span class="step-title">${esc(s.title || s.action)}</span></div>
      <div class="step-meta">${esc(meta)}</div>${route}
      ${s.details ? `<div class="step-details">${esc(s.details)}</div>` : ""}
      ${veh}<div class="step-nums">${nums.join("")}</div></li>`;
  }).join("");
}

function selectStep(id, center = true) {
  S.selectedStep = id;
  const s = S.steps.find((x) => x.id === id);
  if (s && s.year !== S.year && !S.listAll) setYear(s.year);
  else renderSteps();
  if (s && s._path && center) fitBounds(s._path, 0.25);
  const li = document.querySelector(`#steps li[data-id="${CSS.escape(id)}"]`);
  scrollIntoBox(li, $("tab-steps"));
  dirty = true;
}

// ------------------------------------------------------------------ projects (one line = one project)

function buildProjects() {
  const byKey = new Map(), projects = [];
  for (const b of S.steps) {
    if (b.action !== "build_line") continue;
    let id = String(b.line ?? b.id);
    if (byKey.has(id)) id = String(b.id);
    const from = b._from || (b._path ? { name: "", x: b._path[0][0], y: b._path[0][1] } : null);
    const to = b._to || (b._path ? { name: "", x: b._path.at(-1)[0], y: b._path.at(-1)[1] } : null);
    const p = {
      id, build: b, refs: [], n: projects.length + 1, path: b._path, from, to,
      // done-marks survive a re-generated plan where line ids may shift
      sig: [b.kind, b.cargo, b.from && b.from.name, b.to && b.to.name].join("|") || id,
    };
    b._project = id;
    byKey.set(id, p);
    if (!byKey.has(String(b.id))) byKey.set(String(b.id), p);
    projects.push(p);
  }
  for (const s of S.steps) {
    if (s.action === "build_line" || s.line == null) continue;
    const p = byKey.get(String(s.line));
    if (p) { p.refs.push(s); s._project = p.id; }
  }
  for (const p of projects) {
    p.text = [p.id, p.build.title, cargoName(p.build.cargo), KIND_LABEL[p.build.kind], p.from && p.from.name,
      p.to && p.to.name].filter(Boolean).join(" ").toLowerCase();
  }
  S.projects = projects;
  S.projectByKey = byKey;
  $("projects-count").textContent = projects.length ? `(${projects.length})` : "";
}

function getProject(id) { return id == null ? null : S.projectByKey.get(String(id)) || null; }
function curProject() { return getProject(S.currentProject); }
function focusProject() { const p = curProject(); return S.focus && p && p.path ? p : null; }
function isDone(p) { return S.done.has(p.sig); }

function restoreProjectState() {
  S.done = new Set(store.get(LS.done, []) || []);
  S.hideDone = !!store.get(LS.hideDone, false);
  $("hide-done").checked = S.hideDone;
  S.projYearOnly = store.get(LS.projYearOnly, true) !== false;
  $("proj-year-only").checked = S.projYearOnly;
  S.showBuilt = !!store.get(LS.showBuilt, false);
  $("show-built").checked = S.showBuilt;
  const q = new URLSearchParams(location.search);
  S.focus = q.has("focus") ? q.get("focus") !== "0" : store.get(LS.focus, true) !== false;
  const id = q.get("project") || store.get(LS.current, null);
  setTab(q.has("project") ? "projects" : store.get(LS.tab, S.projects.length ? "projects" : "steps"));
  const p = getProject(id);
  if (p) setCurrentProject(p.id, { animate: false, keepFocus: true });
  else { S.currentProject = null; renderProjectUI(); }
}

function updateUrl() {
  try {
    const u = new URL(location.href);
    if (S.currentProject) u.searchParams.set("project", S.currentProject); else u.searchParams.delete("project");
    if (S.currentProject && !S.focus) u.searchParams.set("focus", "0"); else u.searchParams.delete("focus");
    history.replaceState(null, "", u);
  } catch { /* ignore */ }
}

// opts: animate (zoom animation), keepFocus (do not force focus ON), fit (zoom to the project)
function setCurrentProject(id, opts = {}) {
  const p = getProject(id);
  S.currentProject = p ? p.id : null;
  S.selWp = null;
  if (p && !opts.keepFocus) S.focus = true;   // focus is ON by default when a project is picked
  store.set(LS.current, S.currentProject);
  store.set(LS.focus, S.focus);
  updateUrl();
  if (p) {
    S.selectedStep = null;
    if (p.build.year !== S.year) setYear(p.build.year); else renderSteps();
    if (opts.fit !== false) showProjectOnMap(opts.animate !== false);
  }
  renderProjectUI();
  const li = document.querySelector(`#projects li[data-pid="${CSS.escape(S.currentProject || "")}"]`);
  scrollIntoBox(li, $("tab-projects"));
  dirty = true;
}

function setFocus(on) {
  S.focus = !!on;
  store.set(LS.focus, S.focus);
  updateUrl();
  if (S.focus && curProject()) showProjectOnMap();
  renderProjectUI();
  dirty = true;
}

function showProjectOnMap(animate = true) {
  const p = curProject();
  if (!p) return;
  const pts = p.path || [p.from, p.to].filter(Boolean).map((e) => [e.x, e.y]);
  if (pts.length) fitBounds(pts, 0.18, animate);
}

function toggleDone(id, val) {
  const p = getProject(id);
  if (!p) return;
  if (val === undefined ? !isDone(p) : val) S.done.add(p.sig); else S.done.delete(p.sig);
  store.set(LS.done, [...S.done]);
  renderProjectUI();
}

function nextProject() {
  const p = S.projects.find((x) => !isDone(x));
  if (!p) { $("proj-progress").insertAdjacentHTML("afterbegin", loc('<span class="pos">Все проекты выполнены!</span>')); return; }
  setTab("projects");
  setCurrentProject(p.id);
}

function setTab(tab) {
  S.tab = tab === "steps" || tab === "buildings" ? tab : "projects";
  store.set(LS.tab, S.tab);
  document.querySelectorAll(".tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === S.tab));
  $("tab-projects").hidden = S.tab !== "projects";
  $("tab-steps").hidden = S.tab !== "steps";
  $("tab-buildings").hidden = S.tab !== "buildings";
}

function renderProjectUI() {
  renderProjects();
  renderProjectCard();
  renderFocusBanner();
  renderYearBox();
  renderBuildingsTab();
}

function projDate(s) { return `${s.year}${s.month ? " · " + MONTHS[(s.month - 1) % 12] : ""}`; }
function projNet(s) { return (s.income_per_year || 0) - (s.running_cost_per_year || 0); }
function vehSummary(vs) { return (vs || []).map((v) => `${v.count}× ${v.name || v.model}`).join(" + "); }

function renderProjects() {
  const list = $("projects");
  const total = S.projects.length, doneN = S.projects.filter(isDone).length;
  $("proj-progress").innerHTML = total
    ? loc("Сделано {0} из {1}<span class=\"progress-bar\"><span style=\"width:{2}%\"></span></span>", doneN, total, (100 * doneN / total).toFixed(1))
    : "";
  $("next-project").disabled = !total;
  if (!total) {
    list.innerHTML = loc("<li class=\"empty\">В плане нет линий (build_line).</li>");
    return;
  }
  const f = S.projFilter.trim().toLowerCase();
  const yo = S.projYearOnly;
  const yl = $("proj-year-only-label");
  if (yl) yl.textContent = loc("только {0} г.", S.year);
  const items = S.projects.filter((p) => (!S.hideDone || !isDone(p) || p.id === S.currentProject) && (!f || p.text.includes(f))
    && (!yo || p.build.year === S.year || p.id === S.currentProject));
  if (!items.length) {
    list.innerHTML = `<li class="empty">${yo ? loc("В {0} г. новых линий нет{1}. Листайте годы ◀ ▶ или снимите «только {2} г.».", S.year, S.hideDone ? loc(" (или все выполнены)") : "", S.year)
      : S.hideDone && !f ? loc("Все проекты выполнены.") : loc("Ничего не найдено.")}</li>`;
    return;
  }
  list.innerHTML = items.map((p) => {
    const b = p.build, cur = p.id === S.currentProject, done = isDone(p);
    const net = projNet(b);
    const meta = [KIND_LABEL[b.kind], b.cargo ? cargoName(b.cargo) : null, loc("стоимость {0}", fmtMoney(b.cost)),
      b.income_per_year ? loc("прибыль {0}/год", fmtMoney(net)) : null, p.refs.length ? loc("+{0} шаг.", p.refs.length) : null]
      .filter(Boolean).join(" · ");
    return loc("<li class=\"proj{0}{1}\" data-pid=\"{2}\" style=\"border-left-color:{3}\">\n      <label class=\"proj-check\" title=\"Отметить как сделанный\"><input type=\"checkbox\" data-done {4}></label>\n      <div class=\"proj-body\">\n        <div class=\"proj-head\"><span class=\"proj-id\">{5}</span><span class=\"step-date\">{6}</span>{7}</div>\n        <div class=\"proj-title\" title=\"{8}\">{9}</div>\n        <div class=\"proj-meta\">{10}</div>\n      </div>\n      <button class=\"proj-pick\" data-pick>{11}</button>\n    </li>", cur ? " current" : "", done ? " done" : "", esc(p.id), KIND_COLORS[b.kind] || "#898781", done ? "checked" : "", esc(p.id), projDate(b), done ? loc('<span class="done-mark">✓ сделано</span>') : "", esc(b.title), esc(b.title || loc("Линия")), esc(meta), cur ? loc("● Текущий") : loc("Сделать текущим"));
  }).join("");
}

// "52 ед./год  13.8 км … Станции: X  Y. Инфраструктура …" -> one row per sentence, double spaces -> " · "
function detailRows(text) {
  if (!text) return [];
  return String(text).split(/(?<=\.)\s+(?=[А-ЯЁA-Z«])/).map((x) => x.trim().replace(/\.$/, "").replace(/\s{2,}/g, " · "))
    .filter(Boolean);
}

function renderProjectCard() {
  const box = $("project-current"), p = curProject();
  $("focus-toggle").checked = !!(p && S.focus);
  $("focus-toggle").disabled = !p;
  if (!p) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  const b = p.build, done = isDone(p), net = projNet(b);
  const rows = detailRows(b.details).map((r) => {
    const m = r.match(/^([^:]{2,24}):\s*(.*)$/);
    return `<li>${m ? `<b>${esc(m[1])}:</b> ${esc(m[2])}` : esc(r)}</li>`;
  }).join("");
  const veh = (b.vehicles || []).map((v) => `<tr><td>${esc(v.name || v.model)} × ${v.count}</td>
      <td class="n">${v.price ? fmtMoney(v.price) + loc(" / шт.") : ""}</td><td class="n">${v.price ? fmtMoney(v.price * v.count) : ""}</td></tr>`).join("");
  const later = p.refs.slice().sort((a, c) => (a.year - c.year) || ((a.month || 1) - (c.month || 1)) || ((a.order || 0) - (c.order || 0)));
  const laterHTML = later.map((s) => `<li data-year="${s.year}" title="${esc(s.title || "")}${s.details ? "\n" + esc(s.details) : ""}">
      <span class="step-date">${projDate(s)}</span>
      <span>${esc(s.action === "buy_vehicles" ? loc("Докупить") : s.action === "replace_vehicles" ? loc("Замена") : ACTION_LABEL[s.action] || s.action)}${s.vehicles && s.vehicles.length ? ": " + esc(vehSummary(s.vehicles)) : ""}</span>
      <span class="c">${s.cost ? fmtMoney(s.cost) : ""}</span></li>`).join("");
  const totalCost = [b, ...p.refs].reduce((a, s) => a + (s.cost || 0), 0);
  const stat = (k, v, cls = "") => `<div class="stat"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
  box.innerHTML = loc("\n    <div class=\"pc-head\">\n      <span class=\"section-title\">Текущий проект</span><span class=\"proj-id\">{0}</span>\n      <span class=\"tt-muted\">{1} из {2}</span>\n      <label class=\"pc-done{3}\" title=\"Отметить проект как сделанный\"><input type=\"checkbox\" id=\"pc-done\" {4}> {5}</label>\n      <button id=\"pc-clear\" title=\"Снять выбор текущего проекта\">×</button>\n    </div>\n    <div class=\"pc-title\">{6}</div>\n    <div class=\"pc-meta\">{7}</div>\n    <div class=\"pc-route\">\n      <span class=\"pin\">А</span><span>{8}</span>\n      <span class=\"pin\">Б</span><span>{9}</span>\n    </div>\n    <div class=\"pc-actions\">\n      <button id=\"pc-show\" {10} title=\"Приблизить карту к маршруту проекта\">Показать на карте</button>\n      <button id=\"pc-focus\" class=\"toggle{11}\" title=\"Показывать только этот проект (клавиша F)\">Фокус: {12}</button>\n      <button id=\"pc-next\" title=\"Первый невыполненный проект по порядку плана (клавиша N)\">Следующий →</button>\n    </div>\n    <div class=\"pc-nums\">\n      {13}\n      {14}\n      {15}\n      {16}\n      {17}\n      {18}\n    </div>\n    {19}\n    {20}\n    {21}\n    {22}\n    {23}\n    <div class=\"pc-sub\">Дальше по проекту{24}</div>\n    {25}", esc(p.id), p.n, S.projects.length, done ? " is-done" : "", done ? "checked" : "", done ? loc("✓ сделано") : loc("сделано"), esc(b.title || loc("Линия")), esc([KIND_LABEL[b.kind], b.cargo ? cargoName(b.cargo) : null, loc("строить ") + projDate(b),
      b.length_m ? loc("маршрут ") + fmtKm(b.length_m) : null].filter(Boolean).join(" · ")), esc(p.from ? p.from.name || loc("начало") : "—"), esc(p.to ? p.to.name || loc("конец") : "—"), p.path ? "" : "disabled", S.focus ? " on" : "", S.focus ? loc("вкл") : loc("выкл"), stat(loc("Стоимость"), fmtMoney(b.cost)), stat(loc("Доход/год"), fmtMoney(b.income_per_year), b.income_per_year ? "pos" : ""), stat(loc("Расходы/год"), fmtMoney(b.running_cost_per_year)), stat(loc("Прибыль/год"), fmtMoney(net), net >= 0 ? "pos" : "neg"), stat(loc("Окупаемость"), fmtYears(b.roi_years)), stat(loc("Всего вложений"), fmtMoney(totalCost)), (() => { const h = demoBlockHTML(b, dossierFor(p)); return h ? loc("<div class=\"pc-sub\">Снос городских зданий</div>{0}", h) : ""; })(), rows ? loc("<div class=\"pc-sub\">Станции, депо, состав</div><ul class=\"pc-details\">{0}</ul>", rows) : "", veh ? loc("<div class=\"pc-sub\">Транспорт</div><table class=\"pc-veh\">{0}</table>", veh) : "", guideCardHTML(p), dossierCardHTML(p), later.length ? ` (${later.length})` : "", later.length ? `<ul class="pc-later">${laterHTML}</ul>` : loc("<div class=\"pc-empty\">Других шагов по этой линии нет.</div>"));
}

function renderFocusBanner() {
  renderGuideLegend();
  const el = $("focus-banner"), p = focusProject();
  if (!p) { el.hidden = true; el.innerHTML = ""; return; }
  const b = p.build;
  el.hidden = false;
  el.innerHTML = loc("<span class=\"fb-label\">Фокус: {0}</span>\n    <span class=\"fb-text\">{1} — {2} → {3}</span>\n    <button data-fb=\"show\">Показать</button><button data-fb=\"off\" title=\"Клавиша F\">Выйти из фокуса</button>", esc(p.id), esc([KIND_LABEL[b.kind], b.cargo ? cargoName(b.cargo) : null].filter(Boolean).join(" · ")), esc(p.from ? p.from.name : ""), esc(p.to ? p.to.name : ""));
}

function setupProjects() {
  document.querySelectorAll(".tabs .tab").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
  $("next-project").addEventListener("click", nextProject);
  $("hide-done").addEventListener("change", (e) => { S.hideDone = e.target.checked; store.set(LS.hideDone, S.hideDone); renderProjects(); });
  $("proj-year-only").addEventListener("change", (e) => { S.projYearOnly = e.target.checked; store.set(LS.projYearOnly, S.projYearOnly); renderProjects(); });
  $("proj-filter").addEventListener("input", (e) => { S.projFilter = e.target.value; renderProjects(); });
  $("focus-toggle").addEventListener("change", (e) => setFocus(e.target.checked));
  $("projects").addEventListener("click", (e) => {
    const li = e.target.closest("li[data-pid]");
    if (!li) return;
    if (e.target.matches("input[data-done]")) { toggleDone(li.dataset.pid, e.target.checked); return; }
    if (e.target.closest(".proj-check")) return;
    setCurrentProject(li.dataset.pid);
  });
  $("project-current").addEventListener("click", (e) => {
    const t = e.target;
    if (t.id === "pc-done") { toggleDone(S.currentProject, t.checked); return; }
    if (t.id === "pc-clear") { setCurrentProject(null); return; }
    if (t.id === "pc-show") { showProjectOnMap(); return; }
    if (t.id === "pc-focus") { setFocus(!S.focus); return; }
    if (t.id === "pc-next") { nextProject(); return; }
    if (t.id === "pc-dossier") { openDossier(); return; }
    const wp = t.closest(".pc-wps li[data-wp]");
    if (wp) { selectWaypoint(+wp.dataset.wp); return; }
    const li = t.closest(".pc-later li[data-year]");
    if (li) setYear(+li.dataset.year);
  });
  const ph = $("plan-head");
  ph.classList.toggle("collapsed", !!store.get(LS.planCollapsed, false));
  $("plan-title").title = loc("Свернуть / развернуть сводку плана");
  $("plan-title").addEventListener("click", () => {
    ph.classList.toggle("collapsed");
    store.set(LS.planCollapsed, ph.classList.contains("collapsed"));
  });
  const cf = $("cashflow-section");
  cf.classList.toggle("collapsed", !!store.get(LS.cashflowCollapsed, false));
  $("cashflow-title").addEventListener("click", () => {
    cf.classList.toggle("collapsed");
    store.set(LS.cashflowCollapsed, cf.classList.contains("collapsed"));
    if (!cf.classList.contains("collapsed") && S.plan) drawCashflow();
  });
  $("focus-banner").addEventListener("click", (e) => {
    const a = e.target.dataset && e.target.dataset.fb;
    if (a === "show") showProjectOnMap();
    if (a === "off") setFocus(false);
  });
}

// ------------------------------------------------------------------ view helpers

function fitBounds(pts, pad = 0.1, animate = true) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of pts) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
  const w = Math.max(x1 - x0, 5000), h = Math.max(y1 - y0, 5000);
  const scale = Math.min(W / (w * (1 + 2 * pad)), H / (h * (1 + 2 * pad)));
  animateTo({ cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, scale: clampScale(scale) }, animate);
}

function fitAll(animate = true) {
  const b = S.game.terrain.image_bounds;
  S.fitScale = Math.min(W / (b.maxX - b.minX), H / (b.maxY - b.minY)) * 0.98;
  animateTo({ cx: (b.minX + b.maxX) / 2, cy: (b.minY + b.maxY) / 2, scale: S.fitScale }, animate);
}

function clampScale(s) { return Math.max(S.fitScale * 0.6, Math.min(s, 0.6)); }

let anim = null;
function animateTo(target, animate = true) {
  if (!animate) { Object.assign(S.view, target); dirty = true; return; }
  const from = { ...S.view }, t0 = performance.now(), dur = 380;
  anim = () => {
    const k = Math.min(1, (performance.now() - t0) / dur), e = 1 - Math.pow(1 - k, 3);
    // interpolate scale geometrically so zooming feels even
    S.view.scale = from.scale * Math.pow(target.scale / from.scale, e);
    S.view.cx = from.cx + (target.cx - from.cx) * e;
    S.view.cy = from.cy + (target.cy - from.cy) * e;
    dirty = true;
    if (k >= 1) anim = null;
  };
}

function zoomAt(sx, sy, factor) {
  const [wx, wy] = s2w(sx, sy);
  S.view.scale = clampScale(S.view.scale * factor);
  // keep the world point under the cursor fixed
  S.view.cx = wx - (sx - W / 2) / S.view.scale;
  S.view.cy = wy + (sy - H / 2) / S.view.scale;
  dirty = true;
}

// ------------------------------------------------------------------ drawing

function loop(now) {
  if (anim) anim(now);
  if (S.flash && now > S.flash.until) { S.flash = null; dirty = true; }
  if (S.flash) dirty = true;
  if (dirty && S.game) { draw(now); dirty = false; }
  requestAnimationFrame(loop);
}

function draw(now) {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#10161c";
  ctx.fillRect(0, 0, W, H);
  drawTerrain();
  if (S.layers.network) drawNetwork();   // existing roads / tracks from the game: under all plan layers
  const fp = focusProject();
  const vis = ensureVis();
  S.bHits = [];
  if (fp) {
    // focus mode: faint context + only the current project
    const b = S.game.terrain.image_bounds;
    const [x0, y0] = w2s(b.minX, b.maxY), [x1, y1] = w2s(b.maxX, b.minY);
    ctx.fillStyle = "rgba(10,14,18,0.38)";
    ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
    const ends = projectEndKeys(fp);
    if (S.layers.industries) drawIndustries(ends);
    if (S.layers.towns) drawTowns(ends);
    drawProject(fp);
    if (S.layers.buildings) drawBuildings(vis);
    drawGuideMarks(projectGuide(fp));
  } else {
    S.wpHits = [];
    if (S.layers.routes) drawRoutes();
    if (S.layers.chains) drawChains();
    if (S.layers.plan) drawPlan(vis);
    if (S.layers.industries) drawIndustries();
    if (S.layers.towns) drawTowns();
    if (S.layers.buildings) drawBuildings(vis);
  }
  if (S.flash) drawFlash(now);
}

function entKey(e) {
  if (!e || e.id === undefined || e.id === null) return null;
  return (e.type === "town" ? "t" : "i") + e.id;
}
function projectEndKeys(p) {
  return new Set([entKey(p.build._from), entKey(p.build._to), ...(p.build._stops || []).map(entKey)].filter(Boolean));
}

function drawProject(p) {
  const s = p.build, pts = p.path, color = KIND_COLORS[s.kind] || MODE_COLORS[s.kind] || "#dddddd";
  const w = 6;
  const guide = projectGuide(p);
  if (guide.graded) {
    // build-similar view: corridor + line coloured by gradient; ungraded legs (ships) keep their own style
    drawGuideLine(guide, w);
    overlayReuse(s, w);                              // existing / upgrade parts over the gradient colours
    for (const g of guide.legs) if (!g.graded) {
      tracePath(g.pts); ctx.lineJoin = "round"; ctx.lineCap = "round";
      ctx.setLineDash([]); ctx.strokeStyle = "rgba(0,0,0,0.8)"; ctx.lineWidth = w + 3; ctx.stroke();
      ctx.strokeStyle = MODE_COLORS[g.mode] || "#ddd"; ctx.lineWidth = w; ctx.setLineDash(MODE_DASH[g.mode] || []); ctx.stroke();
      ctx.setLineDash([]);
    }
    drawSpans(s, w, guideColorAt(guide));
  } else {
    drawGuideLine(guide, w);                         // corridor only
    strokeLegs(s, w, { glow: "rgba(255,255,255,0.18)", glowW: 16 });   // every leg in its own style (ship / truck / rail)
    drawSpans(s, w);
  }
  drawDemolition(s, w);
  drawLegLabels(s, true);
  if (stepBothWays(s)) {
    const m = pathMid(pts), [bx, by] = w2s(m[0], m[1]);
    labelBox(loc("↔ туда и обратно по одной дороге"), bx, by + 22, color, "center");
  }
  S.guideToDraw = guide;
  // intermediate stops
  for (const st of s._stops || []) {
    const [sx, sy] = w2s(st.x, st.y);
    ctx.beginPath(); ctx.arc(sx, sy, 5, 0, Math.PI * 2);
    ctx.fillStyle = "#ffffff"; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = color; ctx.stroke();
  }
  // mid label: mode · cargo · length
  const mid = stepLegsGeo(s).length > 1 ? null : pathMid(pts);   // multi-leg lines get per-leg labels instead
  if (mid) {
    const [mx, my] = w2s(mid[0], mid[1]);
    const txt = [p.id, KIND_LABEL[s.kind], s.cargo ? cargoName(s.cargo) : null, s.length_m ? fmtKm(s.length_m) : null]
      .filter(Boolean).join(" · ");
    labelBox(txt, mx, my - 16, color, "center");
  }
  // endpoints A / B with name labels pushed away from the line
  // A / B sit on the path point nearest to the real source / destination (multimodal geometry may start at the hub)
  const endAt = (ent, fallback) => {
    if (!ent || !Number.isFinite(ent.x)) return [pts[fallback], pts[fallback === 0 ? 1 : fallback - 1] || pts[fallback]];
    let bi = 0, bd = Infinity;
    pts.forEach((q, i) => { const d = (q[0] - ent.x) ** 2 + (q[1] - ent.y) ** 2; if (d < bd) { bd = d; bi = i; } });
    return [pts[bi], pts[bi === 0 ? 1 : bi - 1] || pts[bi]];
  };
  const ends = [[p.from, ...endAt(p.from, 0), "А"], [p.to, ...endAt(p.to, pts.length - 1), "Б"]];
  for (const [ent, pt, nb, letter] of ends) {
    const [sx, sy] = w2s(pt[0], pt[1]);
    const [nx, ny] = w2s(nb[0], nb[1]);
    let dx = sx - nx, dy = sy - ny;
    const d = Math.hypot(dx, dy) || 1; dx /= d; dy /= d;
    ctx.beginPath(); ctx.arc(sx, sy, 10, 0, Math.PI * 2);
    ctx.fillStyle = "#ffffff"; ctx.fill(); ctx.lineWidth = 3; ctx.strokeStyle = color; ctx.stroke();
    ctx.font = `700 12px system-ui, "Segoe UI", sans-serif`; ctx.textAlign = "center"; ctx.fillStyle = "#111";
    ctx.fillText(letter, sx, sy + 4);
    const name = ent && ent.name ? ent.name : (letter === "А" ? loc("Начало") : loc("Конец"));
    const text = `${letter}: ${name}`;
    let lx = sx + dx * 22, ly = sy + dy * 22 + 4, align = Math.abs(dx) < 0.35 ? "center" : dx > 0 ? "left" : "right";
    ctx.font = `600 13px system-ui, "Segoe UI", sans-serif`;
    const tw = ctx.measureText(text).width + 22;
    if ((align === "left" && lx + tw > W) || (align === "right" && lx - tw < 0)) {
      // no room on the outward side: put the label above (or below, near the top edge) the marker
      lx = sx; align = "center"; ly = sy - 18 < 24 ? sy + 30 : sy - 18;
    }
    labelBox(text, lx, ly, "#ffffff", align);
  }
}

// text on a dark rounded pill (for focus-mode labels)
function labelBox(text, x, y, color, align = "left") {
  ctx.font = `600 13px system-ui, "Segoe UI", sans-serif`;
  const tw = ctx.measureText(text).width, ph = 20, px = 7;
  let left = align === "center" ? x - tw / 2 : align === "right" ? x - tw : x;
  left = Math.max(px + 4, Math.min(W - tw - px - 4, left));   // keep the pill on screen
  ctx.fillStyle = "rgba(15,20,26,0.9)";
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(left - px, y - 14, tw + 2 * px, ph, 5); else ctx.rect(left - px, y - 14, tw + 2 * px, ph);
  ctx.fill();
  ctx.lineWidth = 1; ctx.strokeStyle = color; ctx.stroke();
  ctx.textAlign = "left"; ctx.fillStyle = "#ffffff";
  ctx.fillText(text, left, y);
}

function drawTerrain() {
  const b = S.game.terrain.image_bounds;
  if (!S.terrain) return;
  const [x0, y0] = w2s(b.minX, b.maxY);
  const [x1, y1] = w2s(b.maxX, b.minY);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(S.terrain, x0, y0, x1 - x0, y1 - y0);
  // slightly darken so markers pop on the dark UI
  ctx.fillStyle = "rgba(10,14,18,0.18)";
  ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
}

function tracePath(pts) {
  ctx.beginPath();
  pts.forEach(([x, y], i) => { const [sx, sy] = w2s(x, y); i ? ctx.lineTo(sx, sy) : ctx.moveTo(sx, sy); });
}

// routes.json has one entry per plan line (name = build_line title): it follows the year filter too.
function routeYear(r) {
  if (r._year === undefined) {
    if (!S.routeYears) {
      S.routeYears = new Map();
      for (const s of S.steps) if (s.action === "build_line" && s.title && !S.routeYears.has(s.title)) S.routeYears.set(s.title, s.year);
    }
    r._year = S.routeYears.has(r.name) ? S.routeYears.get(r.name) : null;
  }
  return r._year;
}
function routeShown(r) {
  const y = routeYear(r);
  return y === null || y === S.year || (S.showBuilt && y < S.year);
}

function drawRoutes() {
  for (const r of S.routes) {
    const pts = routePoints(r);
    if (!pts || !routeShown(r)) continue;
    const hov = S.hover && S.hover.kind === "route" && S.hover.obj === r;
    tracePath(pts);
    ctx.setLineDash(KIND_DASH[r.kind] || []);
    ctx.strokeStyle = "rgba(0,0,0,0.6)"; ctx.lineWidth = hov ? 6 : 4.5; ctx.stroke();
    ctx.strokeStyle = KIND_COLORS[r.kind] || "#cccccc"; ctx.globalAlpha = hov ? 1 : 0.85;
    ctx.lineWidth = hov ? 3.5 : 2.5; ctx.stroke();
    ctx.globalAlpha = 1; ctx.setLineDash([]);
  }
}

function routePoints(r) {
  const p = r.points || r.geometry || r.path || r.polyline;
  return Array.isArray(p) && p.length >= 2 ? p : null;
}

function arrowHead(x, y, ang, size, color) {
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.lineTo(x - size * Math.cos(ang - 0.4), y - size * Math.sin(ang - 0.4));
  ctx.lineTo(x - size * Math.cos(ang + 0.4), y - size * Math.sin(ang + 0.4));
  ctx.closePath();
  ctx.fill();
}

function hoverKey() {
  const h = S.hover;
  if (!h) return null;
  if (h.kind === "town") return "t" + h.obj.id;
  if (h.kind === "industry") return "i" + h.obj.id;
  return null;
}

function drawChains() {
  const hk = hoverKey();
  const hot = hk ? new Set(S.chainsBy.get(hk) || []) : new Set();
  const draw = (c, hi) => {
    const [ax, ay] = w2s(c.from.x, c.from.y), [bx, by] = w2s(c.to.x, c.to.y);
    const dx = bx - ax, dy = by - ay, len = Math.hypot(dx, dy);
    if (len < 6) return;
    // gentle curve so A->B and B->A do not overlap
    const mx = (ax + bx) / 2 - dy * 0.12, my = (ay + by) / 2 + dx * 0.12;
    const endPad = c.toType === "town" ? 10 : 7;
    const t = Math.max(0, 1 - endPad / len);
    const ex = (1 - t) * (1 - t) * ax + 2 * (1 - t) * t * mx + t * t * bx;
    const ey = (1 - t) * (1 - t) * ay + 2 * (1 - t) * t * my + t * t * by;
    const tx = 2 * (1 - t) * (mx - ax) + 2 * t * (bx - mx), ty = 2 * (1 - t) * (my - ay) + 2 * t * (by - my);
    ctx.beginPath(); ctx.moveTo(ax, ay); ctx.quadraticCurveTo(mx, my, ex, ey);
    ctx.strokeStyle = hi ? CHAIN_HI : CHAIN_COLOR; ctx.lineWidth = hi ? 2 : 1.2; ctx.stroke();
    arrowHead(ex, ey, Math.atan2(ty, tx), hi ? 10 : 7, hi ? CHAIN_HI : CHAIN_COLOR);
    if (hi) {
      const lx = 0.25 * ax + 0.5 * mx + 0.25 * bx, ly = 0.25 * ay + 0.5 * my + 0.25 * by;
      label(`${cargoName(c.cargo)} · ${fmtKm(c.d)}`, lx, ly, "#ffffff", 12, "center");
    }
  };
  ctx.setLineDash([4, 4]);
  for (const c of S.chains) if (!hot.has(c)) draw(c, false);
  ctx.setLineDash([]);
  for (const c of hot) draw(c, true);
}

// Year view: lines built in the selected year (bright, every leg in its own style), lines that only get
// vehicles this year (thinner, "+ техника"), optionally lines built earlier (faint grey). Future is hidden.
function drawPlan(vis) {
  const selected = S.steps.find((s) => s.id === S.selectedStep);
  for (const s of vis.earlier) drawStepLine(s, "past", false);
  for (const s of vis.vehicleLines) drawStepLine(s, "vehicles", false);
  for (const s of vis.builds) if (s._path) drawStepLine(s, "current", false);
  if (selected && selected._path) {
    const ref = selected._refOnly ? S.steps.find((x) => x._project === selected._project && x.action === "build_line") : selected;
    drawStepLine(ref && ref._path ? ref : selected, "current", true);
  }
  // labels: this year's lines (id · mode · cargo), vehicle purchases
  const zf = zoomFactor();
  for (const s of vis.builds) {
    if (!s._path) continue;
    const many = stepLegsGeo(s).length > 1;
    if (many && zf > 1.8) { drawLegLabels(s, false); continue; }
    const mid = pathMid(s._path);
    const [sx, sy] = w2s(mid[0], mid[1]);
    const txt = [s._project, s.kind === "multi" ? (s.project_kind === "multimodal" ? loc("мультимодально") : loc("сеть")) : KIND_LABEL[s.kind],
      s.cargo ? cargoName(s.cargo) : null].filter(Boolean).join(" · ");
    if (txt && (vis.builds.length <= 14 || zf > 2.2 || s.id === S.selectedStep)) label(txt, sx, sy - 10, "#ffffff", 12, "center");
  }
  for (const s of vis.vehicleLines) {
    if (zf < 1.8 && vis.vehicleLines.length > 6) break;
    const mid = pathMid(s._path);
    const [sx, sy] = w2s(mid[0], mid[1]);
    label(loc("{0} · + техника", s._project), sx, sy - 10, "#d8e8ff", 12, "center");
  }
}

// st: "current" (built this year) | "vehicles" (only vehicles bought this year) | "past" (built earlier, faint)
function drawStepLine(s, st, isSel) {
  if (st === "past") {
    strokeLegs(s, 2.4, { alpha: 0.45, noTies: true });
    return;
  }
  const w = st === "current" ? 4.5 : 3;
  strokeLegs(s, w, isSel ? { glow: "rgba(255,255,255,0.85)", glowW: 8 } : st === "vehicles" ? { alpha: 0.85, glow: "rgba(140,190,255,0.35)", glowW: 6 } : {});
  drawSpans(s, w);
  if (st === "current") drawDemolition(s, w);
  // endpoints (stations get their own icons on the "Здания" layer)
  if (S.layers.buildings) return;
  const color = MODE_COLORS[s.kind] || "#dddddd";
  const ends = [s._path[0], s._path[s._path.length - 1], ...(s._stops || []).map((p) => [p.x, p.y])];
  for (const [x, y] of ends) {
    const [sx, sy] = w2s(x, y);
    ctx.beginPath(); ctx.arc(sx, sy, st === "current" ? 4.5 : 3.5, 0, Math.PI * 2);
    ctx.fillStyle = "#ffffff"; ctx.fill();
    ctx.lineWidth = 2; ctx.strokeStyle = color; ctx.stroke();
  }
}

// Leg-aware polylines of a step: multimodal / passenger-network legs separately, else the whole path.
function stepLegsGeo(s) {
  if (s._legGeo !== undefined && s._legGeoVer === S.bVer) return s._legGeo;
  const pax = isPax(s);
  const legs = (Array.isArray(s.legs) ? s.legs : [])
    .filter((l) => l && l.mode && Array.isArray(l.geometry) && l.geometry.length >= 2)
    .map((l) => ({ pts: l.geometry, mode: l.mode, ring: Array.isArray(l.nodes) && l.nodes.length === 1, role: l.role, pax, leg: l,
      segs: segList(l.segments) }));
  s._legGeo = legs.length ? legs : (s._path ? [{ pts: s._path, mode: s.kind, ring: false, pax, segs: ownSegments(s) }] : []);
  // the plan geometry is down-sampled: put the exact bridge / tunnel ends into it so the line crosses the water
  // exactly where the bridge deck is drawn
  const spans = s.action === "build_line" ? stepBuildings(s).filter((b) => b.span).map((b) => b.span) : [];
  if (spans.length) s._legGeo = s._legGeo.map((g) => (g.mode === "water" ? g : { ...g, pts: spliceSpans(g.pts, spans) }));
  s._legGeoVer = S.bVer;
  return s._legGeo;
}

// bridge / viaduct / tunnel decks of a step, drawn over its line (lw = the line width)
function drawSpans(s, lw, colorAt) {
  if (s.action !== "build_line") return;
  const hb = S.hover && S.hover.kind === "building" ? S.hover.obj : null;
  ctx.save();
  ctx.lineCap = "butt"; ctx.lineJoin = "round";
  for (const b of stepBuildings(s)) {
    if (!b.span) continue;
    const [a, c] = b.span.map((p) => w2s(p[0], p[1]));
    const len = Math.hypot(c[0] - a[0], c[1] - a[1]);
    if (len < 1.5) continue;
    const hl = hb && Math.abs(hb.x - b.x) < 1 && Math.abs(hb.y - b.y) < 1;
    const col = colorAt ? colorAt(b) : (MODE_COLORS[b.mode] || "#dddddd");
    const seg = () => { ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(c[0], c[1]); };
    if (b.type === "tunnel") {
      seg(); ctx.setLineDash([]); ctx.strokeStyle = "rgba(22,20,18,0.92)"; ctx.lineWidth = lw + 5; ctx.stroke();
      ctx.globalAlpha = 0.85; ctx.strokeStyle = col; ctx.lineWidth = Math.max(2, lw - 1); ctx.setLineDash([6, 5]); ctx.stroke();
      ctx.globalAlpha = 1; ctx.setLineDash([]);
      // portals: thick bars across the line at both ends
      const ux = (c[0] - a[0]) / len, uy = (c[1] - a[1]) / len, hw = lw / 2 + 6;
      for (const p of [a, c]) {
        ctx.beginPath(); ctx.moveTo(p[0] - uy * hw, p[1] + ux * hw); ctx.lineTo(p[0] + uy * hw, p[1] - ux * hw);
        ctx.strokeStyle = "#111"; ctx.lineWidth = 6; ctx.stroke();
        ctx.strokeStyle = hl ? "#ffffff" : BT.tunnel.color; ctx.lineWidth = 3.5; ctx.stroke();
      }
    } else {
      // bridge / viaduct: dark outline + wide light deck, the line continues on top (like game bridges)
      seg(); ctx.setLineDash([]);
      ctx.strokeStyle = hl ? "#ffffff" : "rgba(10,10,10,0.95)"; ctx.lineWidth = lw + 11; ctx.stroke();
      ctx.strokeStyle = "#f3efe2"; ctx.lineWidth = lw + 7; ctx.stroke();
      ctx.strokeStyle = col; ctx.lineWidth = Math.max(2, lw - 1); ctx.stroke();
    }
  }
  ctx.restore();
}

// ------------------------------------------------------------------ route segments
// routing.py splits a planned road / track into segments (step.segments / leg.segments next to geometry):
// "existing" — use the existing road as it is (thin dashed line over it), "upgrade" — existing road slower
// than the planned one (marked for upgrade), "new" — build (bold line), "shared" — built by another route
// of the same line. Without segments the line is drawn as before (all "new").
const SEG_LABEL = { existing: loc("по существующей дороге"), upgrade: loc("улучшить дорогу"), new: loc("строить"), shared: loc("общий участок") };
const DUP_TOL = 12;        // m: a part this close to an already drawn part of the same line is the same way
const DUP_GAP = 60;        // m along the line: the line's own last metres are not "already drawn"

function segList(a) {
  return Array.isArray(a) && a.some((q) => q && Array.isArray(q.polyline) && q.polyline.length >= 2) ? a : null;
}
function ownSegments(s) { return segList(s.segments) || (s._ref ? segList(s._ref.segments) : null); }

function densifyPts(P, step) {
  const out = [P[0]];
  for (let i = 1; i < P.length; i++) {
    const a = P[i - 1], b = P[i], n = Math.max(1, Math.ceil(Math.hypot(b[0] - a[0], b[1] - a[1]) / step));
    for (let k = 1; k <= n; k++) out.push([a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n]);
  }
  return out;
}

// Drawable parts of a leg: its segments (or the whole path as "new"), with everything that repeats an
// already drawn part of the same leg removed — loops / shuttles go out and back on ONE way, which is drawn
// once (g._dup tells that something was folded: "↔ туда и обратно").
function legParts(g) {
  if (g._parts) return g._parts;
  const items = g.segs ? g.segs.filter((q) => q && Array.isArray(q.polyline) && q.polyline.length >= 2)
    .map((q) => ({ pts: q.polyline, type: q.type || "new", seg: q })) : [{ pts: g.pts, type: "new", seg: null }];
  const B = 50, H = new Map(), out = [];
  const bucket = (x, y) => Math.floor(x / B) + "," + Math.floor(y / B);
  const add = (a, b, s) => {
    const x0 = Math.floor((Math.min(a[0], b[0]) - DUP_TOL) / B), x1 = Math.floor((Math.max(a[0], b[0]) + DUP_TOL) / B);
    const y0 = Math.floor((Math.min(a[1], b[1]) - DUP_TOL) / B), y1 = Math.floor((Math.max(a[1], b[1]) + DUP_TOL) / B);
    for (let i = x0; i <= x1; i++) for (let j = y0; j <= y1; j++) {
      const k = i + "," + j;
      if (!H.has(k)) H.set(k, []);
      H.get(k).push([a, b, s]);
    }
  };
  const seen = (p, s) => (H.get(bucket(p[0], p[1])) || []).some(([a, b, s0]) => s0 < s - DUP_GAP && distToSeg(p[0], p[1], a[0], a[1], b[0], b[1]) < DUP_TOL);
  let gs = 0, dupAny = false;
  for (const it of items) {
    const P = densifyPts(it.pts, 20), S0 = [];
    for (let k = 0; k < P.length; k++) {
      if (k) gs += Math.hypot(P[k][0] - P[k - 1][0], P[k][1] - P[k - 1][1]);
      S0.push(gs);
    }
    const dup = [];
    for (let k = 0; k < P.length; k++) {           // own earlier metres count too (a single path out and back)
      dup.push(seen(P[k], S0[k]));
      if (k) add(P[k - 1], P[k], S0[k]);
    }
    // ignore short "duplicate" blips (a line crossing itself)
    for (let k = 0; k < dup.length;) {
      let j = k; while (j < dup.length && dup[j] === dup[k]) j++;
      if (dup[k] && j - k < 4 && k > 0 && j < dup.length) for (let q = k; q < j; q++) dup[q] = false;
      k = j;
    }
    let cur = null;
    for (let k = 0; k < P.length; k++) {
      if (!dup[k]) {
        if (!cur) { cur = { pts: k ? [P[k - 1]] : [], type: it.type, seg: it.seg }; out.push(cur); }
        cur.pts.push(P[k]);
      } else {
        dupAny = true;
        if (cur) cur.pts.push(P[k]);
        cur = null;
      }
    }
  }
  g._dup = dupAny;
  g._parts = out.filter((q) => q.pts.length >= 2);
  return g._parts;
}

function stepBothWays(s) { return stepLegsGeo(s).some((g) => { legParts(g); return g._dup; }); }

// one part in its segment style
function strokePart(q, mode, lw, color, opt) {
  tracePath(q.pts);
  if (q.type === "existing") {
    const tw = Math.max(1.6, lw * 0.42);
    ctx.setLineDash([]); ctx.strokeStyle = "rgba(0,0,0,0.75)"; ctx.lineWidth = tw + 2.5; ctx.stroke();
    ctx.strokeStyle = color; ctx.lineWidth = tw; ctx.setLineDash([7, 6]); ctx.stroke();
  } else if (q.type === "upgrade") {
    ctx.setLineDash([]); ctx.strokeStyle = "rgba(0,0,0,0.8)"; ctx.lineWidth = lw + 3; ctx.stroke();
    ctx.strokeStyle = color; ctx.lineWidth = Math.max(2, lw * 0.8); ctx.stroke();
    ctx.strokeStyle = "#ffffff"; ctx.lineWidth = Math.max(1.4, lw * 0.4); ctx.setLineDash([2, 6]); ctx.stroke();
  } else {
    ctx.setLineDash([]); ctx.strokeStyle = "rgba(0,0,0,0.8)"; ctx.lineWidth = lw + 3; ctx.stroke();
    ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.setLineDash(MODE_DASH[mode] || []); ctx.stroke();
    if (mode === "rail" && !opt.noTies) {
      // railway look: white ties over the coloured line
      ctx.strokeStyle = "rgba(255,255,255,0.85)"; ctx.lineWidth = Math.max(1.2, lw - 2.6); ctx.setLineDash([6, 8]); ctx.stroke();
    }
  }
  ctx.setLineDash([]);
}

function strokeLegs(s, w, opt = {}) {
  ctx.lineJoin = "round"; ctx.lineCap = "round";
  for (const g of stepLegsGeo(s)) {
    const lw = g.ring ? Math.max(2, w * 0.6) : w;
    const color = opt.color || MODE_COLORS[g.mode] || "#dddddd";
    const parts = legParts(g);
    if (opt.glow) {
      ctx.setLineDash([]); ctx.strokeStyle = opt.glow;
      for (const q of parts) { tracePath(q.pts); ctx.lineWidth = (q.type === "existing" ? lw * 0.5 : lw) + (opt.glowW || 8); ctx.stroke(); }
    }
    ctx.globalAlpha = opt.alpha ?? 1;
    for (const q of parts) strokePart(q, g.mode, lw, color, opt);
    ctx.setLineDash([]); ctx.globalAlpha = 1;
  }
}

// focus mode with the gradient-coloured guide line: mark reused / upgrade parts on top of it
function overlayReuse(s, w) {
  ctx.save();
  ctx.lineJoin = "round"; ctx.lineCap = "round";
  for (const g of stepLegsGeo(s)) for (const q of legParts(g)) {
    if (q.type !== "existing" && q.type !== "upgrade") continue;
    tracePath(q.pts);
    ctx.strokeStyle = "rgba(255,255,255,0.92)";
    if (q.type === "existing") { ctx.lineWidth = Math.max(1.5, w * 0.3); ctx.setLineDash([5, 6]); }
    else { ctx.lineWidth = Math.max(2, w * 0.45); ctx.setLineDash([1.5, 6]); }
    ctx.stroke();
  }
  ctx.restore();
}

// per-type lengths of a step's planned way (segment_totals of the plan, else summed from the segments)
function stepSegTotals(s) {
  const t = s.segment_totals || (s._ref && s._ref.segment_totals);
  if (t) return t;
  const segs = [];
  for (const g of stepLegsGeo(s)) if (g.segs) segs.push(...g.segs);
  if (!segs.length) return null;
  const out = { existing_m: 0, upgrade_m: 0, new_m: 0, shared_m: 0, build_cost: 0, upgrade_cost: 0 };
  for (const q of segs) {
    const L = q.length_m || 0, rep = Math.min(L, q.repeat_m || 0), f = L > 0 ? (L - rep) / L : 1;   // repeat: same way, counted once
    out[(q.type || "new") + "_m"] = (out[(q.type || "new") + "_m"] || 0) + L - rep;
    if (q.type === "new") {
      out.build_cost += f * (q.build_cost || 0);
      for (const k of ["demolition_cost", "demolition_m", "demolition_buildings"]) out[k] = (out[k] || 0) + f * (q[k] || 0);
    }
    if (q.type === "upgrade") out.upgrade_cost += f * (q.build_cost || 0);
  }
  return out;
}

function segTooltipHTML(s) {
  const t = stepSegTotals(s), both = stepBothWays(s);
  const dm = s.action === "build_line" ? stepDemolition(s) : null;
  if (!t && !both && !dm) return "";
  const rows = [];
  if (t) {
    if (t.existing_m > 0) rows.push(`<span class="seg-chip seg-existing"></span>${SEG_LABEL.existing}: ${fmtKm(t.existing_m)}`);
    if (t.upgrade_m > 0) rows.push(`<span class="seg-chip seg-upgrade"></span>${SEG_LABEL.upgrade}: ${fmtKm(t.upgrade_m)}` +
      (t.upgrade_cost ? loc(" (≈ {0}, по желанию)", fmtMoney(t.upgrade_cost)) : ""));
    if (t.new_m > 0) rows.push(`<span class="seg-chip seg-new"></span>${SEG_LABEL.new}: ${fmtKm(t.new_m)}` +
      (t.build_cost ? ` (${fmtMoney(t.build_cost)})` : ""));
    if (t.shared_m > 0) rows.push(`<span class="seg-chip seg-new"></span>${SEG_LABEL.shared}: ${fmtKm(t.shared_m)}`);
    if (t.demolition_buildings > 0.05) rows.push(loc("<span class=\"demo-chip\"></span><span class=\"neg\">в т.ч. снос ~{0} зд.</span>", fmtBld(t.demolition_buildings)) +
      `${t.demolition_m ? loc(", {0} м", fmtInt(t.demolition_m)) : ""}${t.demolition_cost ? ` (${fmtMoney(t.demolition_cost)})` : ""}`);
  }
  if (dm && !(t && t.demolition_buildings > 0.05))
    rows.push(loc("<span class=\"demo-chip\"></span><span class=\"neg\">снос ~{0} зд.</span>{1}", fmtBld(dm.buildings), dm.cost ? loc(" ({0}, в стоимости)", fmtMoney(dm.cost)) : ""));
  const over = dm ? townsRep(dm.towns).filter((r) => r.loss > DEMO_WARN) : [];
  if (over.length) rows.push(loc("<span class=\"neg\">⚠ репутация {0}: игра может запретить снос/станции</span>", over.map((r) => loc("г. {0} −{1} %", esc(r.town), fmt1(r.loss * 100, 0))).join(", ")));
  if (both) rows.push(loc("↔ туда и обратно по одной дороге"));
  return `<div class="tt-segs">${rows.map((r) => `<div>${r}</div>`).join("")}</div>`;
}

// "Судно · 1,4 км" / "Грузовики · 23,8 км" at the middle of every leg of a multi-leg line
function drawLegLabels(s, pill) {
  const geo = stepLegsGeo(s);
  if (geo.length < 2) return;
  for (const g of geo) {
    if (g.ring && !pill) continue;
    const mid = pathMid(g.pts);
    const [sx, sy] = w2s(mid[0], mid[1]);
    const l = g.leg || {};
    const name = g.ring ? (l.title || loc("кольцо")) : [ROLE_LABEL[l.role], modeLabel(g.mode, g.pax)].filter(Boolean).join(": ");
    const txt = name + (l.length_m ? " · " + fmtKm(l.length_m) : "");
    if (pill) labelBox(txt, sx, sy - 14, MODE_COLORS[g.mode] || "#fff", "center");
    else label(txt, sx, sy - 10, "#ffffff", 12, "center");
  }
}

function pathMid(pts) {
  let total = 0;
  for (let i = 1; i < pts.length; i++) total += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
  let acc = 0;
  for (let i = 1; i < pts.length; i++) {
    const d = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    if (acc + d >= total / 2) {
      const t = d ? (total / 2 - acc) / d : 0;
      return [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * t, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * t];
    }
    acc += d;
  }
  return pts[0];
}

function industrySize(ind) {
  const z = Math.min(2.2, Math.max(0.85, Math.pow(zoomFactor(), 0.35)));
  return (5 + Math.min(ind.level || 1, 12) * 0.35) * z;
}

// focusEnds: in focus mode, Set of "t<id>"/"i<id>" keys drawn at full strength; everything else is dimmed
function drawIndustries(focusEnds) {
  const showLabels = zoomFactor() > 3.2;
  for (const ind of S.game.industries) {
    const [sx, sy] = w2s(ind.x, ind.y);
    if (sx < -20 || sy < -20 || sx > W + 20 || sy > H + 20) continue;
    const r = industrySize(ind);
    const hov = S.hover && S.hover.kind === "industry" && S.hover.obj === ind;
    const dim = focusEnds && !focusEnds.has("i" + ind.id) && !hov;
    ctx.globalAlpha = dim ? 0.28 : 1;
    ctx.beginPath();
    if (ind.on_water) {
      ctx.moveTo(sx, sy - r * 1.25); ctx.lineTo(sx + r * 1.25, sy); ctx.lineTo(sx, sy + r * 1.25); ctx.lineTo(sx - r * 1.25, sy);
      ctx.closePath();
    } else {
      ctx.rect(sx - r, sy - r, 2 * r, 2 * r);
    }
    ctx.fillStyle = S.typeColor.get(ind.type);
    ctx.fill();
    ctx.lineWidth = hov ? 2.5 : 1.5;
    ctx.strokeStyle = hov ? "#ffffff" : (ind.type === "oil_platform" ? "#d9d9d9" : "rgba(0,0,0,0.85)");
    ctx.stroke();
    const isEnd = focusEnds && focusEnds.has("i" + ind.id);
    if ((showLabels && !dim && !isEnd) || hov) label(ind.type_name, sx, sy + r + 12, "#e8e6dc", 11, "center");
  }
  ctx.globalAlpha = 1;
}

function townRadius(t) {
  const z = Math.min(2.4, Math.max(0.8, Math.pow(zoomFactor(), 0.4)));
  return (3.5 + 8.5 * Math.sqrt(t.total / S.maxTownTotal)) * z;
}

function drawTowns(focusEnds) {
  for (const t of S.game.towns) {
    const [sx, sy] = w2s(t.x, t.y);
    if (sx < -60 || sy < -30 || sx > W + 60 || sy > H + 30) continue;
    const r = townRadius(t);
    const hov = S.hover && S.hover.kind === "town" && S.hover.obj === t;
    const dim = focusEnds && !focusEnds.has("t" + t.id) && !hov;
    ctx.globalAlpha = dim ? 0.3 : 1;
    ctx.beginPath(); ctx.arc(sx, sy, r, 0, Math.PI * 2);
    ctx.fillStyle = TOWN_FILL; ctx.fill();
    ctx.lineWidth = hov ? 3 : 1.8; ctx.strokeStyle = "#ffffff"; ctx.stroke();
    // project endpoints get their own "А:/Б:" label in focus mode
    if (!(focusEnds && focusEnds.has("t" + t.id)) || hov)
      label(t.name, sx + r + 4, sy + 4, dim ? "#c3c2b7" : "#ffffff", dim ? 12 : 13, "left", dim ? 500 : 600);
  }
  ctx.globalAlpha = 1;
}

function label(text, x, y, color, size = 12, align = "left", weight = 500) {
  ctx.font = `${weight} ${size}px system-ui, "Segoe UI", sans-serif`;
  ctx.textAlign = align;
  ctx.lineJoin = "round";
  ctx.lineWidth = 3.5; ctx.strokeStyle = "rgba(0,0,0,0.85)";
  ctx.strokeText(text, x, y);
  ctx.fillStyle = color; ctx.fillText(text, x, y);
}

function drawFlash(now) {
  const f = S.flash;
  const [sx, sy] = w2s(f.x, f.y);
  const k = ((now - f.start) % 900) / 900;
  ctx.beginPath(); ctx.arc(sx, sy, 10 + 26 * k, 0, Math.PI * 2);
  ctx.strokeStyle = `rgba(255,255,255,${1 - k})`; ctx.lineWidth = 3; ctx.stroke();
}

// ------------------------------------------------------------------ buildings: stations, ports, warehouses, depots, bridges
// Sources, best first: the route dossier of the build step (exact positions, sizes, costs, shared stations),
// multimodal legs of the plan step (ports / truck / rail stations at leg ends, warehouse at the hub),
// passenger-network legs (stops at the ends, city-ring stops spread along the ring), plain endpoints/stops.

function isPax(s) { return s.cargo === "passengers" || s.project_kind === "passenger" || s.project_kind === "workers"; }

function stationType(kind, mode, pax, name) {
  const n = String(name || "");
  if (kind === "harbor" || mode === "water" || /порт|гаван|port|harbo/i.test(n)) return "harbor";
  if (mode === "tram") return "tram";
  if (kind === "rail_station" || mode === "rail" || /ж\/д|rail/i.test(n)) return pax ? "rail_pass" : "rail_cargo";
  return pax ? "bus" : "road_cargo";
}

const DEPOT_NAME = { road: loc("Дорожное депо"), rail: loc("Ж/д депо"), water: loc("Судовое депо"), tram: loc("Трамвайное депо"), air: loc("Ангар") };

function hexA(hex, a) {
  const n = parseInt(String(hex).slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}
function bColor(b) { return b.type === "depot" || b.type === "maint" ? (MODE_COLORS[b.mode] || BT.depot.color) : BT[b.type].color; }
function bLabel(b) {
  if (b.type === "depot") return DEPOT_NAME[b.mode] || loc("Депо");
  if (b.type === "maint") return loc("Здание техобслуживания") + (b.mode ? ` (${modeLabel(b.mode, false).toLowerCase()})` : "");
  return BT[b.type].label;
}
function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  return a > 10 && a < 20 ? many : b === 1 ? one : b >= 2 && b <= 4 ? few : many;
}
function trunc(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

// "Станции: Ж/д грузовая станция: 1×200 м ×3, Ж/д депо." -> station part, cost per station, extra maintenance buildings
function parseStationsText(details) {
  const m = /(?:Станции|Stations):\s*(.+?)(?:\.\s|\.$|$)/.exec(details || "");
  if (!m) return null;
  const st = m[1].split(/,\s+|\s{2,}/).filter((t) => !/депо|техобслуж|depot|maintenance/i.test(t)).join(", ");
  const each = /\((\d[\d\s]*)\s*(?:каждая|each)\)/.exec(m[1]);
  const maint = /\+\s*(\d+)\s*(?:здан\S*\s+техобслуж|maintenance building)/i.exec(m[1]);
  return { text: m[1], station: st.replace(/\s*\(\d[\d\s]*\s*(?:каждая|each)\)/, "").trim(),
    each: each ? +each[1].replace(/\s/g, "") : null, maint: maint ? +maint[1] : 0 };
}

function sizeFromName(n) {
  n = String(n || "");
  let m = /(\d+)\s*×\s*(\d+)\s*[мm]/.exec(n);
  if (m) return loc("{0} {1} × {2} м платформа", m[1], +m[1] === 1 ? loc("путь") : loc("пути"), m[2]);
  m = /(\d+)\s*(?:терминал|terminal)/.exec(n);
  if (m) return loc("терминалов: {0}", m[1]);
  if (/мал|small/i.test(n)) return loc("малый");
  if (/средн|medium/i.test(n)) return loc("средний");
  if (/больш|large/i.test(n)) return loc("большой");
  return "";
}

// "Склады: Склад (liquid), 500 ед. ×1 (порт … , буфер 23 ед.); Склад …. Перевалка …"
function parseWarehouses(details) {
  const m = /Склады:\s*(.*?)\.\s+(?:Перевалка|Оплата|Инфраструктура)/.exec(details || "");
  if (!m) return [];
  return m[1].split(/;\s*(?=Склад)/).map((t) => ({
    title: (/^(Склад(?:\s*\([^)]*\))?)/.exec(t) || [])[1] || loc("Склад"),
    cap: +(((/(\d[\d\s]*)\s*ед\.\s*×/.exec(t) || [])[1]) || "").replace(/\s/g, "") || null,
    modules: +((/×\s*(\d+)/.exec(t) || [])[1]) || null,
    buffer: +((/буфер\s+(\d+)/.exec(t) || [])[1]) || null,
  }));
}
function whTitle(n) { return String(n || loc("Склад")).replace(/\((\w+)\)/, (m, c) => `(${WH_CLASS[c] || c})`).replace(/,\s*\d[\d\s]*ед\.?$/, ""); }

function nearestOnPath(x, y, pts) {
  let best = pts[0], bd = Infinity;
  for (const p of pts) {
    const d = (p[0] - x) * (p[0] - x) + (p[1] - y) * (p[1] - y);
    if (d < bd) { bd = d; best = p; }
  }
  return [best[0], best[1]];
}

// n points evenly spread along a polyline (city ring stops)
function pointsAlong(pts, n) {
  const cum = [0];
  for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
  const L = cum[cum.length - 1], out = [];
  for (let k = 0; k < n; k++) {
    const s = L * (k + 0.5) / n;
    let i = 1;
    while (i < cum.length - 1 && cum[i] < s) i++;
    const t = cum[i] - cum[i - 1] ? (s - cum[i - 1]) / (cum[i] - cum[i - 1]) : 0;
    out.push([pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * t, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * t]);
  }
  return out;
}

function collectBuildings(s) {
  const out = [], pax = isPax(s), d = dossierForStep(s);
  const dLegs = d && !d.error ? (d.legs || []).filter((l) => l && !l.error) : [];
  // multimodal dossiers may carry one generic "multi" leg without real stations: then use the plan legs
  const dOk = dLegs.length > 0 && dLegs.every((l) => (l.stations || []).length && l.stations.every((st) => st.kind !== "multi" && st.building !== "?"));
  const legs = Array.isArray(s.legs) ? s.legs : [];
  const mm = legs.filter((l) => l && l.mode && l.from && typeof l.from === "object" && Number.isFinite(l.from.x) && l.to && Number.isFinite(l.to.x));
  const px = legs.filter((l) => l && l.mode && Array.isArray(l.nodes) && Array.isArray(l.geometry) && l.geometry.length >= 2);
  const stInfo = parseStationsText(s.details);
  const add = (o) => out.push(Object.assign({ cost: null, maint: null, existing: null, approx: false, catch: null, radius: null,
    size: "", title: "", name: "", role: "", note: "", mode: s.kind }, o));

  if (s.project_kind === "industry_build" && s._path) {
    const site = Array.isArray(s.geometry) && s.geometry.length ? s.geometry[0] : s._path[0];
    const exp = /Ожидается\s+([^(]+?)(?:\s*\(|\.\s|$)/.exec(s.details || "");
    add({ type: "prospect", x: site[0], y: site[1], title: s.title, name: exp ? loc("ожидается ") + exp[1].trim() : "", approx: true, radius: 400,
      note: loc("Место — оценка (свободная зона у города): уточнить после появления предприятия") });
  }
  if (dOk) {
    for (const L of dLegs) {
      for (const st of L.stations || []) {
        const type = stationType(st.kind, L.mode, pax, st.building);
        add({ type, mode: L.mode, x: st.x, y: st.y, name: st.endpoint, role: st.role, dist: st.dist_to_endpoint_m,
          title: pax && /Грузов|cargo|truck/i.test(st.building || "") ? "" : st.building,
          size: st.platform_m ? loc("{0} {1} × {2} м ({3} сегм. по 40 м)", st.terminals, st.terminals === 1 ? loc("путь") : loc("пути"), fmtInt(st.platform_m), st.segments)
            : loc("терминалов: {0}", st.terminals),
          cost: st.cost, maint: st.maintenance, full: st.full_cost, existing: st.free_by || null, catch: st.catchment_m,
          demo: st.free_by ? null : st.demolition || null });
      }
      for (const dp of L.depots || []) {
        const isM = /техобслуж|maintenance/i.test(dp.what || "");
        add({ type: isM ? "maint" : "depot", mode: L.mode, x: dp.x, y: dp.y, title: dp.what, name: loc("у станции погрузки"), count: dp.count,
          size: loc("{0}обслуживает до {1} ед. техники", dp.count > 1 ? dp.count + loc(" шт. · ") : "", dp.pool), cost: dp.cost, maint: dp.maintenance,
          existing: dp.free ? loc("уже есть") : null, radius: dp.radius_m, note: dp.where || "", demo: dp.free ? null : dp.demolition || null });
      }
      for (const x of (L.route && L.route.structures) || []) {
        if (!Number.isFinite(x.x)) continue;
        add({ type: x.type === "tunnel" ? "tunnel" : "bridge", mode: L.mode, x: x.x, y: x.y, kind: x.type, lengthM: x.length_m,
          span: spanFor(L.route && L.route.polyline, x.x, x.y, x.length_m), speed: x.speed_limit_kmh, atM: x.at_m, bname: x.name,
          title: `${x.type_ru || ""}${x.name ? " — " + x.name : ""}`, name: x.place || "",
          size: loc("{0} м · км {1}–{2}{3}", fmtInt(x.length_m), fmt1(x.at_m / 1000), fmt1(x.to_m / 1000), x.speed_limit_kmh ? loc(" · до ") + x.speed_limit_kmh + loc(" км/ч") : ""),
          cost: x.cost, maint: x.maintenance });
      }
    }
  } else if (mm.length) {
    for (const L of mm) {
      const g = Array.isArray(L.geometry) && L.geometry.length >= 2 ? L.geometry : null;
      for (const [end, sname, role] of [[L.from, L.station_from, loc("погрузка")], [L.to, L.station_to, loc("выгрузка")]]) {
        const p = g ? nearestOnPath(end.x, end.y, g) : [end.x, end.y];
        const hub = String(end.key || "").startsWith("hub:");
        add({ type: stationType(null, L.mode, pax, sname), mode: L.mode, x: p[0], y: p[1], title: sname || "", name: end.name,
          role: `${ROLE_LABEL[L.role] || loc("плечо")} (${modeLabel(L.mode, pax).toLowerCase()}): ${role}`, size: sizeFromName(sname),
          note: hub ? loc("Пункт перевалки: здесь же склад — груз из одного вида транспорта в другой идёт только через склад") : "" });
      }
      const p0 = g ? nearestOnPath(L.from.x, L.from.y, g) : [L.from.x, L.from.y];
      add({ type: "depot", mode: L.mode, x: p0[0], y: p0[1], name: loc("у погрузки: ") + L.from.name, approx: true,
        size: L.n_consists ? loc("для {0}× {1}", L.n_consists, L.consist || "") : "", note: loc("Положение примерное — поставьте рядом со станцией погрузки") });
    }
  } else if (px.length) {
    for (const L of px) {
      const type = stationType(null, L.mode, true, ""), g = L.geometry, det = L.details || "";
      if (L.nodes.length >= 2) {
        const names = String(L.title || "").replace(/^[^:]*:\s*/, "").split("↔").map((t) => t.trim());
        const si = parseStationsText(det);
        [[0, g[0]], [1, g[g.length - 1]]].forEach(([k, p]) =>
          add({ type, mode: L.mode, x: p[0], y: p[1], name: names[k] || "", role: L.title || "", size: si ? si.station : "" }));
      } else {
        const n = Math.max(1, Math.min(12, +((/(\d+)\s+останов/.exec(det) || [])[1]) || 4));
        const each = /(\d[\d\s]*)\s*каждая/.exec(det);
        const kw = (/\((мал|средн|больш)/i.exec(det) || [])[1];
        const kindTxt = kw ? { мал: loc("малая"), средн: loc("средняя"), больш: loc("большая") }[kw.toLowerCase()] : "";
        pointsAlong(g, n).forEach((p, k) => add({ type, mode: L.mode, x: p[0], y: p[1], name: `${L.title || loc("кольцо")} · ${k + 1}/${n}`,
          role: loc("городское кольцо"), size: kindTxt ? kindTxt + loc(" остановка") : "", cost: each ? +each[1].replace(/\s/g, "") : null, approx: true,
          note: loc("Положение примерное: остановки равномерно по кольцу — ставьте в плотной застройке") }));
      }
    }
    for (const m of [...new Set(px.map((l) => l.mode))]) {
      const L = px.find((l) => l.mode === m), p = L.geometry[0];
      add({ type: "depot", mode: m, x: p[0], y: p[1], name: L.title || "", approx: true, note: loc("Положение примерное — рядом с первой остановкой") });
    }
  } else if (s._path) {
    const ends = (s._stops && s._stops.length >= 2 ? s._stops : [s._from, s._to]).filter(Boolean);
    const seen = new Set(), stName = stInfo ? stInfo.station.replace(/\s*×\s*\d+$/, "") : "";
    const type = stationType(null, s.kind, pax, stName);
    for (const e of ends) {
      const k = entKey(e) || `${e.x}|${e.y}`;
      if (seen.has(k)) continue;
      seen.add(k);
      const p = nearestOnPath(e.x, e.y, s._path);
      add({ type, mode: s.kind, x: p[0], y: p[1], name: e.name, title: pax ? "" : stName, size: pax ? (stName || "") : sizeFromName(stName),
        cost: stInfo ? stInfo.each : null });
    }
    if (ends.length) {
      const p = nearestOnPath(ends[0].x, ends[0].y, s._path);
      add({ type: "depot", mode: s.kind, x: p[0], y: p[1], name: loc("у погрузки: ") + (ends[0].name || ""), approx: true,
        note: loc("Положение примерное — рядом с первой станцией") });
      if (stInfo && stInfo.maint) add({ type: "maint", mode: s.kind, x: p[0], y: p[1], count: stInfo.maint, size: loc("{0} шт.", stInfo.maint),
        name: loc("у погрузки"), approx: true, note: loc("Положение примерное — рядом с депо") });
    }
  }
  // warehouses at multimodal transfer points
  const whs = (d && d.warehouses && d.warehouses.length ? d.warehouses : null) || s.warehouses || (s.meta && s.meta.warehouses) || null;
  if (whs && whs.length) {
    for (const w of whs) {
      if (!Number.isFinite(w.x)) continue;
      add({ type: "warehouse", x: w.x, y: w.y, title: whTitle(w.name), name: w.hub || "", cost: w.cost ?? null, maint: w.maintenance ?? null,
        demo: w.demolition || null,
        size: loc("{0} мод.{1}", w.modules, w.buffer_units != null ? loc(", буфер ") + fmtInt(w.buffer_units) + loc(" ед.") : ""),
        note: loc("Груз перегружается здесь: станции не хранят груз, между ними нужен склад") });
    }
  } else if (mm.length) {
    const hubs = [];
    for (const L of mm) for (const e of [L.to, L.from]) if (String(e.key || "").startsWith("hub:") && !hubs.some((h) => h.key === e.key)) hubs.push(e);
    const items = parseWarehouses(s.details);
    hubs.forEach((h, i) => {
      const w = items[i] || {};
      add({ type: "warehouse", x: h.x, y: h.y, title: whTitle(w.title), name: h.name,
        size: [w.modules ? loc("{0} мод.{1}", w.modules, w.cap ? loc(" по ") + w.cap + loc(" ед.") : "") : "", w.buffer ? loc("буфер {0} ед.", w.buffer) : ""].filter(Boolean).join(", "),
        note: loc("Груз перегружается здесь: станции не хранят груз, между ними нужен склад") });
    });
  }
  addDemolition(s, d, dLegs, add);
  const line = s._project || String(s.line ?? s.id);
  for (const b of out) {
    b.step = s; b.year = s.year; b.line = line; b.pax = pax;
    if (!b.catch && BT[b.type].catch) b.catch = BT[b.type].catch;
  }
  return out;
}

function stepBuildings(s) {
  if (s._bldVer !== S.bVer) {
    try { s._bld = collectBuildings(s); } catch (e) { console.warn("buildings", s.id, e); s._bld = []; }
    s._bldVer = S.bVer;
  }
  return s._bld;
}

// ------------------------------------------------------------------ demolition of town buildings
// Affected stretches of the new way: dossier legs[].route.demolition.parts[] ({polyline, x, y, buildings, cost,
// length_m, towns {town: buildings}, segment}); without a dossier: plan segments of type "new" with
// demolition_buildings (hatched over the whole segment, approximate). Stations / depots / warehouses of the
// dossier carry their own `demolition` {cost, buildings, towns {town: {buildings, rating_loss}}} (in their price).
function townsRep(towns) {     // {town: {buildings, rating_loss}} | {town: loss} -> [{town, loss, n}] by loss
  return Object.entries(towns || {}).map(([t, v]) => ({ town: t, loss: typeof v === "number" ? v : (v && v.rating_loss) || 0,
    n: v && typeof v === "object" ? v.buildings || 0 : null })).sort((a, b) => b.loss - a.loss);
}
// project-level demolition: dossier first (route + stations + depots + warehouses), else the planner's step.demolition
function stepDemolition(s) {
  const d = dossierForStep(s);
  if (d && !d.error && d.demolition) return d.demolition;
  const pd = s && s.demolition;
  return pd && (pd.buildings > 0.05 || pd.cost > 0) ? pd : null;
}
function fmtBld(n) { return !okNum(n) ? "?" : n >= 0.95 ? String(Math.round(n)) : fmt1(n); }
function repHTML(towns) {
  return townsRep(towns).map((r) => loc("<span class=\"{0}\">г. {1}{2}", r.loss > DEMO_WARN ? "neg" : "", esc(r.town), r.n ? loc(" ~{0} зд.", fmtBld(r.n)) : "") +
    `${r.loss >= 0.0005 ? loc(" · репутация −{0} %", fmt1(r.loss * 100, 0)) : ""}${r.loss > DEMO_WARN ? " ⚠" : ""}</span>`).join("<br>");
}
const DEMO_RULE = loc("«Плохо» (< 2/6) — игра запрещает снос, «Очень плохо» (< 1/6) — новые станции в городе; +1/6 за 3 мес.");

function addDemolition(s, d, dLegs, add) {
  if (s.action !== "build_line") return;
  const proj = stepDemolition(s), projTowns = proj ? proj.towns : null;
  const what = (mode) => loc("Снос под ") + (mode === "rail" ? loc("пути") : loc("дорогу"));
  let any = false;
  for (const L of dLegs) {
    const dem = L.route && !L.route.free && L.route.demolition;
    for (const p of (dem && dem.parts) || []) {
      if (!Array.isArray(p.polyline) || p.polyline.length < 2) continue;
      any = true;
      add({ type: "demolition", mode: L.mode, x: p.x, y: p.y, hatch: p.polyline, cost: p.cost, dBld: p.buildings, dLen: p.length_m,
        projTowns, title: what(L.mode), name: Object.keys(p.towns || {}).map((t) => loc("г. ") + t).join(", "),
        size: loc("~{0} {1} · {2} м трассы", fmtBld(p.buildings), plural(Math.max(1, Math.round(p.buildings)), loc("здание"), loc("здания"), loc("зданий")), fmtInt(p.length_m)),
        role: p.segment ? loc("участок {0} трассы", p.segment) : "" });
    }
  }
  if (any || (d && !d.error && dLegs.length)) return;      // the dossier knows best (also "no demolition")
  const segs = [];
  for (const l of Array.isArray(s.legs) ? s.legs : []) if (l && segList(l.segments)) for (const q of l.segments) segs.push([q, l.mode]);
  if (!segs.length) for (const q of ownSegments(s) || []) segs.push([q, s.kind]);
  for (const [q, mode] of segs) {
    if (!q || q.type !== "new" || !(q.demolition_buildings > 0.05) || !Array.isArray(q.polyline) || q.polyline.length < 2) continue;
    const m = pathMid(q.polyline);
    add({ type: "demolition", mode, x: m[0], y: m[1], hatch: q.polyline, cost: q.demolition_cost ?? null, dBld: q.demolition_buildings,
      dLen: q.demolition_m, projTowns, approx: true, title: what(mode),
      size: loc("~{0} зд. · {1} м трассы через застройку", fmtBld(q.demolition_buildings), fmtInt(q.demolition_m)),
      note: loc("Штриховка на весь новый участок — точные места сноса в подробном разборе") });
  }
}

// demolition totals of a list of map buildings (markers + stations / depots / warehouses with `demo`) and the
// per-town reputation loss of the projects they belong to
function demoTotals(list) {
  const t = { n: 0, cost: 0, towns: {} };
  for (const b of list) {
    if (b.existing) continue;
    if (b.type === "demolition") { t.n += b.dBld || 0; t.cost += b.cost || 0; }
    else if (b.demo) { t.n += b.demo.buildings || 0; t.cost += b.demo.cost || 0; }
  }
  for (const s of new Set(list.flatMap((b) => b.steps || [b.step]))) {
    const dm = s && stepDemolition(s);
    for (const r of townsRep(dm && dm.towns)) t.towns[r.town] = (t.towns[r.town] || 0) + r.loss;
  }
  return t;
}

let DEMO_PATTERN = null;
function demoPattern() {
  if (DEMO_PATTERN) return DEMO_PATTERN;
  const c = document.createElement("canvas"); c.width = c.height = 8;
  const g = c.getContext("2d");
  g.fillStyle = "rgba(255,75,62,0.18)"; g.fillRect(0, 0, 8, 8);
  g.strokeStyle = "rgba(255,75,62,0.95)"; g.lineWidth = 2;
  g.beginPath(); g.moveTo(-2, 10); g.lineTo(10, -2); g.moveTo(-2, 2); g.lineTo(2, -2); g.moveTo(6, 10); g.lineTo(10, 6); g.stroke();
  DEMO_PATTERN = ctx.createPattern(c, "repeat");
  return DEMO_PATTERN;
}

// red hatched band over the stretches of a step's new way that demolish town buildings
function drawDemolition(s, lw) {
  if (s.action !== "build_line") return;
  const list = stepBuildings(s).filter((b) => b.type === "demolition" && b.hatch);
  if (!list.length) return;
  const hb = S.hover && S.hover.kind === "building" ? S.hover.obj : null;
  const sb = S.selBuilding && S.vis ? S.vis.byKey.get(S.selBuilding) : null;
  ctx.save(); ctx.lineJoin = "round"; ctx.lineCap = "butt"; ctx.setLineDash([]);
  for (const b of list) {
    const hl = [hb, sb].some((h) => h && h.type === "demolition" && Math.abs(h.x - b.x) < 1 && Math.abs(h.y - b.y) < 1);
    const w = lw + (hl ? 16 : 11);
    tracePath(b.hatch);
    ctx.strokeStyle = hl ? "rgba(255,255,255,0.9)" : "rgba(120,10,0,0.55)"; ctx.lineWidth = w + 2; ctx.stroke();
    ctx.strokeStyle = demoPattern(); ctx.lineWidth = w; ctx.stroke();
  }
  ctx.restore();
}

// "снос" pill at the middle of an affected stretch (a red dot when zoomed out)
function drawDemoMarker(b, x, y, hl, big) {
  ctx.save();
  if (!big) {
    ctx.beginPath(); ctx.arc(x, y, hl ? 5.5 : 4, 0, Math.PI * 2);
    ctx.fillStyle = DEMO_COLOR; ctx.fill(); ctx.lineWidth = 1.5; ctx.strokeStyle = hl ? "#fff" : "rgba(0,0,0,0.9)"; ctx.stroke();
    ctx.restore();
    return;
  }
  ctx.font = `700 10px system-ui, "Segoe UI", sans-serif`;
  const txt = loc("снос"), tw = ctx.measureText(txt).width + 10, h = 15;
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(x - tw / 2, y - h / 2, tw, h, 7); else ctx.rect(x - tw / 2, y - h / 2, tw, h);
  ctx.fillStyle = DEMO_COLOR; ctx.fill();
  ctx.lineWidth = hl ? 2.5 : 1.5; ctx.strokeStyle = hl ? "#ffffff" : "rgba(0,0,0,0.9)"; ctx.stroke();
  ctx.fillStyle = "#ffffff"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(txt, x, y + 0.5);
  ctx.textBaseline = "alphabetic";
  ctx.restore();
}

// project card / dossier: "Снос городских зданий" block (dossier totals or the planner's step.demolition)
function demoBlockHTML(s, d) {
  const dm = (d && !d.error && d.demolition) || stepDemolition(s);
  if (!dm) return "";
  const warns = (s.warnings || []).filter((w) => /снос|репутац|demoli|reputation/i.test(w));
  const over = townsRep(dm.towns).filter((r) => r.loss > DEMO_WARN);
  const plan = dm.plan && d && d.demolition ? loc("<div class=\"tt-muted\">в плане: ~{0} зд., ≈ {1}</div>", fmtBld(dm.plan.buildings), fmtMoney(dm.plan.cost)) : "";
  return loc("<div class=\"pc-demo\">\n    <div><span class=\"demo-chip\"></span><b class=\"neg\">~{0} {1}</b>\n      · ≈ {2}{3} <span class=\"tt-muted\">(уже в стоимости)</span></div>\n    <div class=\"pc-demo-towns\">{4}</div>{5}\n    {6}\n  </div>", fmtBld(dm.buildings), plural(Math.max(1, Math.round(dm.buildings || 0)), loc("здание"), loc("здания"), loc("зданий")), fmtMoney(dm.cost), dm.m ? loc(" · {0} м трассы через застройку", fmtInt(dm.m)) : "", repHTML(dm.towns), plan, warns.length ? `<ul class="pc-demo-warn">${warns.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`
      : over.length ? loc("<div class=\"pc-demo-warn\">⚠ Репутация падает больше чем на 50 %: игра может запретить снос/станции ({0})</div>", esc(DEMO_RULE)) : "");
}

// The same building used by several visible lines is drawn once (lines merged in `lines`).
function mergeBuildings(list) {
  const m = new Map();
  for (const b of list) {
    const k = b.type === "demolition" ? ["demolition", b.line, b.role, Math.round(b.x), Math.round(b.y)].join("|")   // every stretch on its own
      : [b.type, b.type === "depot" || b.type === "maint" ? b.mode : "", Math.round(b.x / 60), Math.round(b.y / 60)].join("|");
    const e = m.get(k);
    if (!e) { m.set(k, { ...b, key: k, lines: [b.line], steps: [b.step] }); continue; }
    if (!e.lines.includes(b.line)) { e.lines.push(b.line); e.steps.push(b.step); }
    if (e.existing && !b.existing) Object.assign(e, { existing: null, cost: b.cost, maint: b.maint, title: b.title || e.title, size: b.size || e.size });
    else if (e.cost == null && b.cost != null && !b.existing) e.cost = b.cost;
    if (!b.approx) e.approx = false;
  }
  return [...m.values()];
}

// Icons closer than ~90 m share an anchor and are drawn side by side (station, warehouse, depot, ...).
function makeAnchors(icons) {
  const ord = (b) => BT_ORDER.indexOf(b.type);
  const anchors = [];
  for (const b of icons.slice().sort((a, c) => ord(a) - ord(c))) {
    let a = anchors.find((x) => Math.hypot(x.x - b.x, x.y - b.y) < 90);
    if (!a) { a = { x: b.x, y: b.y, items: [] }; anchors.push(a); }
    a.items.push(b);
    b.anchor = a;
  }
  return anchors;
}

function computeVis(year, fp) {
  let builds = [], vehicleLines = [], vehSteps = [], earlier = [];
  if (fp) builds = [fp.build];
  else {
    builds = S.steps.filter((s) => s.action === "build_line" && s.year === year);
    const vl = new Set();
    for (const s of S.steps) {
      if (s.year !== year || s.action === "build_line" || s.action === "note" || !s._project) continue;
      vehSteps.push(s);
      const p = getProject(s._project);
      if (p && p.build.year !== year && p.build._path) vl.add(p.build);
    }
    vehicleLines = [...vl];
    if (S.showBuilt) earlier = S.steps.filter((s) => s.action === "build_line" && s.year < year && s._path && !vl.has(s));
  }
  const buildings = mergeBuildings(builds.flatMap(stepBuildings));
  const structs = buildings.filter((b) => BT[b.type].small);
  const anchors = makeAnchors(buildings.filter((b) => !BT[b.type].small));
  return { year, builds, vehicleLines, vehSteps, earlier, buildings, structs, anchors, byKey: new Map(buildings.map((b) => [b.key, b])) };
}

function ensureVis() {
  if (!S.plan || S.year == null) return computeVis(null, null);
  const fp = focusProject();
  const key = [S.year, S.showBuilt, fp ? fp.id : "", S.bVer, S.steps.length].join("|");
  if (S.visKey !== key) { S.vis = computeVis(S.year, fp); S.visKey = key; }
  return S.vis;
}

// whole-year numbers (independent of the focus mode) for the year box
function yearStats(y) {
  const key = y + "|" + S.bVer + "|" + S.steps.length;
  if (S.ysKey !== key) {
    const v = computeVis(y, null);
    const others = S.steps.filter((s) => s.year === y && !s._project && s.action !== "build_line");
    const newB = v.buildings.filter((b) => !b.existing && b.type !== "demolition");
    const dt = demoTotals(v.buildings);
    S.ys = { builds: v.builds.length, veh: v.vehSteps.length, others: others.length, newB: newB.length,
      cost: newB.reduce((a, b) => a + (b.cost || 0), 0), demoN: dt.n, demoCost: dt.cost,
      demoWarn: Object.values(dt.towns).some((x) => x > DEMO_WARN) };
    S.ysKey = key;
  }
  return S.ys;
}

function iconSize() { const zf = zoomFactor(); return zf < 1.6 ? 15 : zf < 4 ? 19 : 23; }

function iconPos(b, size) {
  const a = b.anchor, n = a.items.length, i = a.items.indexOf(b), cols = Math.min(n, 5), rows = Math.ceil(n / cols);
  const [ax, ay] = w2s(a.x, a.y), gap = size + 3;
  const col = i % cols, row = Math.floor(i / cols), rowN = row === rows - 1 ? n - row * cols : cols;
  // icons stand above the real spot like a map pin (keeps town names and the line itself readable)
  const cy = ay - (rows * gap) / 2 - 7;
  return [ax + (col - (rowN - 1) / 2) * gap, cy + (row - (rows - 1) / 2) * gap];
}

function drawBuildings(vis) {
  const size = iconSize(), zf = zoomFactor(), fp = focusProject();
  const hb = S.hover && S.hover.kind === "building" ? S.hover.obj : null;
  const sb = S.selBuilding ? vis.byKey.get(S.selBuilding) : null;
  for (const b of vis.buildings) if (b.type === "prospect" && b !== hb && b !== sb) drawRadius(b, b.radius, "#7bdc8f", null);
  for (const b of new Set([sb, hb])) if (b) drawCatchment(b);
  for (const b of vis.structs) {
    const demo = b.type === "demolition";            // demolition markers also when zoomed out (as dots)
    if (!demo && !(zf > 1.4 || fp)) continue;
    const [sx, sy] = w2s(b.x, b.y);
    if (sx < -10 || sy < -10 || sx > W + 10 || sy > H + 10) continue;
    if (demo) drawDemoMarker(b, sx, sy, b === hb || b === sb, zf > 1.4 || !!fp);
    else drawStructure(b, sx, sy, b === hb || b === sb);
    S.bHits.push({ b, x: sx, y: sy, r: demo ? 11 : 8 });
  }
  for (const a of vis.anchors) {
    const [ax, ay] = w2s(a.x, a.y);
    if (ax < -120 || ay < -120 || ax > W + 120 || ay > H + 120) continue;
    const pos = a.items.map((b) => iconPos(b, size));
    // pin stem + dot at the real position
    const stemTop = Math.max(...pos.map((p) => p[1])) + size / 2;
    ctx.beginPath(); ctx.moveTo(ax, stemTop); ctx.lineTo(ax, ay);
    ctx.lineWidth = 2; ctx.strokeStyle = "rgba(15,20,26,0.9)"; ctx.stroke();
    ctx.beginPath(); ctx.arc(ax, ay, 2.6, 0, Math.PI * 2);
    ctx.fillStyle = "#ffffff"; ctx.fill(); ctx.lineWidth = 1.2; ctx.stroke();
    if (a.items.length > 1) {
      // dark plate groups the buildings of one place (e.g. port + warehouse + truck station + depot)
      const xs = pos.map((p) => p[0]), ys = pos.map((p) => p[1]), pad = size / 2 + 4;
      const x0 = Math.min(...xs) - pad, x1 = Math.max(...xs) + pad, y0 = Math.min(...ys) - pad, y1 = Math.max(...ys) + pad;
      ctx.fillStyle = "rgba(15,20,26,0.82)";
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(x0, y0, x1 - x0, y1 - y0, 6); else ctx.rect(x0, y0, x1 - x0, y1 - y0);
      ctx.fill();
      ctx.lineWidth = 1; ctx.strokeStyle = "rgba(255,255,255,0.25)"; ctx.stroke();
    }
    a.items.forEach((b, i) => {
      drawIcon(b, pos[i][0], pos[i][1], size, b === hb || b === sb);
      S.bHits.push({ b, x: pos[i][0], y: pos[i][1], r: size / 2 + 2 });
    });
    // transfer points: say in words what is there
    if ((fp || zf > 2.5) && a.items.some((b) => b.type === "warehouse")) {
      const SHORT = { harbor: loc("порт"), warehouse: loc("склад"), road_cargo: loc("автостанция"), rail_cargo: loc("ж/д станция") };
      const parts = [...new Set(a.items.map((b) => SHORT[b.type]).filter(Boolean))];
      const yb = Math.max(...pos.map((p) => p[1])) + size / 2 + 20;
      labelBox(loc("Перевалка: ") + parts.join(" + "), ax, yb, BT.warehouse.color, "center");
    }
  }
}

function drawIcon(b, x, y, size, hl) {
  const t = BT[b.type], col = bColor(b), h = size / 2;
  ctx.save();
  ctx.globalAlpha = b.existing ? 0.78 : 1;
  const shape = (r) => {
    ctx.beginPath();
    if (t.round) ctx.arc(x, y, r, 0, Math.PI * 2);
    else if (ctx.roundRect) ctx.roundRect(x - r, y - r, 2 * r, 2 * r, 4);
    else ctx.rect(x - r, y - r, 2 * r, 2 * r);
  };
  if (hl) { shape(h + 4); ctx.fillStyle = hexA(col, 0.55); ctx.fill(); }
  shape(h);
  ctx.fillStyle = b.existing ? "#cfcdc4" : "#fbfaf5"; ctx.fill();
  ctx.lineWidth = hl ? 3 : 2.4; ctx.strokeStyle = hl ? "#ffffff" : col;
  if (b.existing) ctx.setLineDash([3, 2]);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.font = `${Math.round(size * 0.66)}px ${EMOJI_FONT}`;
  ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillStyle = "#111";
  ctx.fillText(t.icon, x, y + size * 0.05);
  ctx.restore();
}

function drawStructure(b, x, y, hl) {
  ctx.save();
  ctx.lineWidth = hl ? 2.5 : 1.5; ctx.strokeStyle = hl ? "#ffffff" : "rgba(0,0,0,0.9)";
  ctx.beginPath();
  if (b.type === "tunnel") { ctx.arc(x, y + 3, 6.5, Math.PI, 0); ctx.closePath(); ctx.fillStyle = BT.tunnel.color; }
  else { ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.fillStyle = "#f3efe2"; }
  ctx.fill(); ctx.stroke();
  // "i" mark: hover / click for the bridge or tunnel card (the structure itself is drawn as a deck on the line)
  ctx.fillStyle = b.type === "tunnel" ? "#ffffff" : "#1d4f86";
  ctx.font = `700 8px system-ui, sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(b.type === "tunnel" ? "Т" : loc("М"), x, b.type === "tunnel" ? y + 0.5 : y + 0.5);
  ctx.textBaseline = "alphabetic";
  ctx.restore();
}

function drawRadius(b, r, col, text, fillA = 0.08) {
  if (!r) return;
  const [sx, sy] = w2s(b.x, b.y), R = r * S.view.scale;
  if (R < 3) return;
  ctx.beginPath(); ctx.arc(sx, sy, R, 0, Math.PI * 2);
  ctx.fillStyle = hexA(col, fillA); ctx.fill();
  ctx.setLineDash([6, 4]); ctx.lineWidth = 1.8; ctx.strokeStyle = col; ctx.stroke(); ctx.setLineDash([]);
  if (text && R > 12) label(text, sx, sy - R - 5, "#ffffff", 12, "center", 600);
}

// catchment circle of the hovered / selected building (+ depot placement zone, prospect search zone)
function drawCatchment(b) {
  const col = bColor(b), [sx, sy] = w2s(b.x, b.y);
  if (b.catch) drawRadius(b, b.catch, col, loc("охват {0} м", fmtInt(b.catch)), 0.2);
  if (b.radius) drawRadius(b, b.radius, b.type === "prospect" ? "#7bdc8f" : "#d8d8d8",
    b.type === "prospect" ? loc("зона поиска ~{0} м", fmtInt(b.radius)) : loc("ставить в пределах {0} м", fmtInt(b.radius)), 0.05);
  // true position of the building (icons of one place are drawn side by side)
  ctx.beginPath(); ctx.arc(sx, sy, 3.5, 0, Math.PI * 2);
  ctx.fillStyle = "#ffffff"; ctx.fill(); ctx.lineWidth = 1.5; ctx.strokeStyle = "#000"; ctx.stroke();
}

function buildingTooltipHTML(b) {
  const col = bColor(b), t = BT[b.type];
  const ico = t.small ? `<span class="bico small ${b.type}"></span>`
    : `<span class="tt-bico${t.round ? " round" : ""}" style="border-color:${col}">${t.icon}</span>`;
  const rows = [];
  if (b.title && b.title !== bLabel(b) && b.type !== "prospect") rows.push([loc("Здание"), esc(b.title)]);
  if (b.size) rows.push([b.type === "demolition" ? loc("Сносим") : loc("Размер"), esc(b.size)]);
  if (b.name) rows.push([t.small ? loc("Где") : b.type === "depot" || b.type === "maint" ? loc("Где") : b.type === "prospect" ? loc("Ресурс") : loc("У объекта"),
    esc(b.name) + (b.dist != null ? loc(" <span class=\"tt-muted\">({0} м от центра)</span>", fmtInt(b.dist)) : "")]);
  if (b.role) rows.push([loc("Роль"), esc(b.role)]);
  let cost;
  if (b.existing) cost = loc("<span class=\"pos\">уже есть{0}</span>", /^[Ls]\d/.test(b.existing) ? loc(" (построено для {0})", esc(b.existing)) : "") +
    (b.full ? loc(" <span class=\"tt-muted\">· новое стоило бы {0}</span>", fmtMoney(b.full)) : "");
  else if (b.cost != null) cost = `<b>${fmtMoney(b.cost)}</b>` + (b.maint ? loc(" <span class=\"tt-muted\">+ содержание {0}/год</span>", fmtMoney(b.maint)) : "");
  else cost = loc("<span class=\"tt-muted\">входит в стоимость линии</span>");
  if (b.type === "demolition") cost = (b.cost != null ? `≈ <b>${fmtMoney(b.cost)}</b> ` : "") + loc("<span class=\"tt-muted\">— уже в цене трассы</span>");
  rows.push([loc("Стоимость"), cost]);
  if (b.demo && !b.existing) rows.push([loc("Снос"), loc("<span class=\"neg\">~{0} зд.</span> · ≈ {1} <span class=\"tt-muted\">(уже в цене)</span><br>{2}", fmtBld(b.demo.buildings), fmtMoney(b.demo.cost), repHTML(b.demo.towns))]);
  if (b.type === "demolition" && b.projTowns && Object.keys(b.projTowns).length)
    rows.push([loc("Репутация"), repHTML(b.projTowns) + loc("<div class=\"tt-muted\">весь проект; {0}</div>", esc(DEMO_RULE))]);
  if (b.catch) rows.push([loc("Охват"), loc("{0} м <span class=\"tt-muted\">(круг на карте)</span>", fmtInt(b.catch))]);
  if (b.radius && b.type !== "prospect") rows.push([loc("Размещение"), loc("в пределах {0} м от станции", fmtInt(b.radius))]);
  const lines = b.steps.map((s) => `<b>${esc(s._project || s.line || s.id)}</b> ${esc(trunc(s.title, 64))} <span class="tt-muted">(${s.year})</span>`).join("<br>");
  return loc("<div class=\"tt-title tt-bhead\">{0}<span>{1}</span></div>\n    <table class=\"tt-kv\">{2}</table>\n    <div class=\"tt-sub\" style=\"margin-top:4px\">{3}:<br>{4}</div>\n    {5}\n    {6}\n    <div class=\"tt-muted\" style=\"margin-top:3px\">Клик — центрировать и закрепить круг охвата</div>", ico, esc(bLabel(b)), rows.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join(""), b.lines.length > 1 ? loc("Линии") : loc("Линия"), lines, b.note ? `<div class="tt-muted" style="margin-top:4px">${esc(b.note)}</div>` : "", b.approx && !/примерн|оценка|approx|estimate/i.test(b.note) ? loc("<div class=\"tt-muted\">Положение примерное.</div>") : "");
}

function selectBuilding(key, center = true) {
  const b = ensureVis().byKey.get(key);
  S.selBuilding = b ? key : null;
  if (b && center) {
    animateTo({ cx: b.x, cy: b.y, scale: clampScale(Math.max(S.view.scale, 0.2)) });
    const now = performance.now();
    S.flash = { x: b.x, y: b.y, start: now, until: now + 1800 };
  }
  renderBuildingsTab();
  const li = document.querySelector(`#buildings-list [data-bkey="${CSS.escape(key)}"]`);
  scrollIntoBox(li, $("tab-buildings"));
  dirty = true;
}

// ------------------------------------------------------------------ polyline helpers (metres along a polyline)

function polyCum(P) {
  const cum = [0];
  for (let i = 1; i < P.length; i++) cum.push(cum[i - 1] + Math.hypot(P[i][0] - P[i - 1][0], P[i][1] - P[i - 1][1]));
  return cum;
}
// nearest point of the polyline to (x, y): {s (m along), d (distance), k (segment index)}
function projectOn(P, cum, x, y) {
  let best = { s: 0, d: Infinity, k: 0 };
  for (let i = 1; i < P.length; i++) {
    const ax = P[i - 1][0], ay = P[i - 1][1], dx = P[i][0] - ax, dy = P[i][1] - ay, l2 = dx * dx + dy * dy;
    const t = l2 ? Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / l2)) : 0;
    const d = Math.hypot(x - ax - t * dx, y - ay - t * dy);
    if (d < best.d) best = { s: cum[i - 1] + t * (cum[i] - cum[i - 1]), d, k: i - 1 };
  }
  return best;
}
function pointAtS(P, cum, s) {
  s = Math.max(0, Math.min(cum[cum.length - 1], s));
  let lo = 0, hi = cum.length - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (cum[m] <= s) lo = m; else hi = m; }
  const t = cum[hi] - cum[lo] ? (s - cum[lo]) / (cum[hi] - cum[lo]) : 0;
  return [P[lo][0] + (P[hi][0] - P[lo][0]) * t, P[lo][1] + (P[hi][1] - P[lo][1]) * t];
}
// sub-polyline between s0 and s1 (both ends interpolated)
function slicePoly(P, cum, s0, s1) {
  const out = [pointAtS(P, cum, s0)];
  for (let i = 0; i < P.length; i++) if (cum[i] > s0 && cum[i] < s1) out.push(P[i]);
  out.push(pointAtS(P, cum, s1));
  return out;
}

// Straight bridge / tunnel span [[ax, ay], [bx, by]] of `len` metres centred on its midpoint (x, y),
// oriented along the route polyline P (dossier route.polyline). null when it does not lie on P.
function spanFor(P, x, y, len) {
  if (!Array.isArray(P) || P.length < 2 || !Number.isFinite(x) || !(len > 0)) return null;
  const cum = polyCum(P), pr = projectOn(P, cum, x, y);
  if (pr.d > 200) return null;
  const h = Math.max(len, 10) / 2;
  const p0 = pointAtS(P, cum, pr.s - Math.max(h, 30)), p1 = pointAtS(P, cum, pr.s + Math.max(h, 30));
  let dx = p1[0] - p0[0], dy = p1[1] - p0[1];
  const n = Math.hypot(dx, dy) || 1; dx /= n; dy /= n;
  return [[x - dx * h, y - dy * h], [x + dx * h, y + dy * h]];
}

// Insert exact span ends into a (down-sampled) polyline: the vertices between them are replaced by the span.
function spliceSpans(pts, spans) {
  if (!spans.length || pts.length < 2) return pts;
  const cum = polyCum(pts), items = [];
  for (const [a, b] of spans) {
    const pa = projectOn(pts, cum, a[0], a[1]), pb = projectOn(pts, cum, b[0], b[1]);
    if (pa.d > 120 || pb.d > 120) continue;
    items.push(pa.s <= pb.s ? { s0: pa.s, s1: pb.s, a, b } : { s0: pb.s, s1: pa.s, a: b, b: a });
  }
  items.sort((u, v) => u.s0 - v.s0);
  const out = [];
  let i = 0, last = -1;
  for (const it of items) {
    if (it.s0 < last) continue;                       // overlapping (a ring passing the same bridge twice)
    while (i < pts.length && cum[i] < it.s0) out.push(pts[i++]);
    out.push(it.a, it.b);
    while (i < pts.length && cum[i] <= it.s1) i++;
    last = it.s1;
  }
  while (i < pts.length) out.push(pts[i++]);
  return out;
}

// ------------------------------------------------------------------ "build similar" guide (focus mode)
// For the current project: a corridor band (±GUIDE_CORRIDOR m), the line coloured by track gradient
// (dossier chart.track), km markers and numbered key waypoints (stations, bridge/tunnel ends, main turns).

const GUIDE_CORRIDOR = 125;
const GRADE_CLASSES = [
  { max: 0.01, color: "#3ccf5a", label: "≤ 1 %" },
  { max: 0.02, color: "#e8d63a", label: "1–2 %" },
  { max: 0.04, color: "#f08c2a", label: "2–4 %" },
  { max: Infinity, color: "#e04848", label: "> 4 %" },
];
function gradeClass(g) { const a = Math.abs(g); return GRADE_CLASSES.findIndex((c) => a <= c.max); }

function profileAt(track, s) {
  if (!track || !track.length) return null;
  if (s <= track[0][0]) return track[0][1];
  let lo = 0, hi = track.length - 1;
  if (s >= track[hi][0]) return track[hi][1];
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (track[m][0] <= s) lo = m; else hi = m; }
  const t = (s - track[lo][0]) / ((track[hi][0] - track[lo][0]) || 1);
  return track[lo][1] + (track[hi][1] - track[lo][1]) * t;
}
// gradient averaged over ±50 m (heights are rounded to 0.1 m, single samples are noisy)
function gradeAt(track, s, L) {
  const a = Math.max(0, s - 50), b = Math.min(L, s + 50);
  if (b - a < 20) return 0;
  return (profileAt(track, b) - profileAt(track, a)) / (b - a);
}

function projectGuide(p) {
  if (p._guide && p._guideVer === S.bVer) return p._guide;
  const s = p.build, d = dossierForStep(s);
  const dLegs = d && !d.error ? (d.legs || []).filter((l) => l && !l.error && l.route && Array.isArray(l.route.polyline) && l.route.polyline.length >= 2) : [];
  const legs = [];
  if (dLegs.length) {
    for (const L of dLegs) {
      const structs = (L.route.structures || []).filter((x) => Number.isFinite(x.x))
        .map((x) => ({ ...x, span: spanFor(L.route.polyline, x.x, x.y, x.length_m) }));
      const pts = spliceSpans(L.route.polyline, structs.filter((x) => x.span).map((x) => x.span));
      legs.push({ mode: L.mode, pts, L: L.route.length_m, track: L.chart && L.chart.track, terrain: L.chart && L.chart.terrain,
        structs, stations: L.stations || [], from: L.from, to: L.to, title: `${L.role_ru || ""} ${L.mode_ru || ""}`.trim() });
    }
  } else {
    for (const g of stepLegsGeo(s)) legs.push({ mode: g.mode, pts: g.pts, L: null, track: null, structs: [], stations: [],
      from: null, to: null, title: modeLabel(g.mode, g.pax) });
  }
  for (const g of legs) {
    g.cum = polyCum(g.pts);
    const end = g.cum[g.cum.length - 1] || 1;
    g.scale = g.L ? g.L / end : 1;                     // route metres per polyline metre (chart s is in route metres)
    if (!g.L) g.L = end;
    g.graded = g.mode !== "water" && Array.isArray(g.track) && g.track.length > 2;
    g.sAt = (x, y) => projectOn(g.pts, g.cum, x, y).s * g.scale;
    g.ptAt = (rs) => pointAtS(g.pts, g.cum, rs / g.scale);
  }
  const guide = { legs, wps: [], graded: legs.some((g) => g.graded), maxGrade: 0 };
  for (const g of legs) if (g.graded) for (let x = 0; x < g.L; x += 50) guide.maxGrade = Math.max(guide.maxGrade, Math.abs(gradeAt(g.track, x + 25, g.L)));
  buildWaypoints(p, guide);
  p._guide = guide; p._guideVer = S.bVer;
  requestTurnReasons(p, guide);
  return guide;
}

function buildWaypoints(p, guide) {
  const wps = [], multi = guide.legs.filter((g) => g.mode !== "water").length > 1;
  const near = (x, y, r = 60) => wps.some((w) => Math.hypot(w.x - x, w.y - y) < r);
  guide.legs.forEach((g, li) => {
    const pre = multi || g.mode === "water" ? `${g.title || modeLabel(g.mode)}: ` : "";
    const add = (o) => { if (!near(o.x, o.y, o.r || 60)) wps.push({ leg: li, legTitle: pre, ...o }); };
    const closed = Math.hypot(g.pts[0][0] - g.pts.at(-1)[0], g.pts[0][1] - g.pts.at(-1)[1]) < 50;
    // stations (dossier) or leg ends
    if (g.stations.length) {
      for (const st of g.stations) {
        const rs = g.sAt(st.x, st.y), [x, y] = g.ptAt(rs);
        add({ kind: "station", s: rs, x: st.x, y: st.y, text: loc("{0} у «{1}»", ["выгрузка", "unloading"].includes(st.role) ? loc("Станция выгрузки") : ["погрузка", "loading"].includes(st.role) ? loc("Станция погрузки") : loc("Станция"), st.endpoint),
          sub: st.building && st.building !== "?" ? st.building : "", lx: x, ly: y });
      }
    } else {
      const a = g.pts[0], b = g.pts.at(-1);
      add({ kind: "station", s: 0, x: a[0], y: a[1], text: loc("Начало") + (g.from ? loc(" у «{0}»", g.from.name) : "") });
      if (!closed) add({ kind: "station", s: g.L, x: b[0], y: b[1], text: loc("Конец") + (g.to ? loc(" у «{0}»", g.to.name) : "") });
    }
    if (g.mode === "water") return;                    // ships need no track: only the ports
    // bridges / tunnels: both ends
    for (const x of g.structs) {
      if (!x.span) continue;
      const what = x.type === "tunnel" ? loc("тоннель") : x.type === "viaduct" ? loc("виадук") : loc("мост");
      const desc = loc("{0} {1} м{2}", what, fmtInt(x.length_m), x.type === "bridge" ? loc(" через воду") : x.type === "viaduct" ? loc(" (овраг/склон)") : "") +
        `${x.name ? ` — «${x.name}»` : ""}${x.speed_limit_kmh ? loc(", до {0} км/ч", x.speed_limit_kmh) : ""}`;
      if (x.length_m < 40) { add({ kind: x.type, s: g.sAt(x.x, x.y), x: x.x, y: x.y, text: desc[0].toUpperCase() + desc.slice(1), r: 40 }); continue; }
      const [a, b] = x.span, sa = g.sAt(a[0], a[1]), sb = g.sAt(b[0], b[1]);
      const [p0, p1] = sa <= sb ? [a, b] : [b, a];
      add({ kind: x.type, s: Math.min(sa, sb), x: p0[0], y: p0[1], text: loc("Начало: {0}", desc), r: 25 });
      add({ kind: x.type, s: Math.max(sa, sb), x: p1[0], y: p1[1], text: loc("Конец: {0}", what), r: 25 });
    }
    // main turns: heading change over ±W m, local maxima, spaced, biggest first
    const W = 220, cand = [];
    for (let rs = W; rs <= g.L - W; rs += 40) {
      const a = g.ptAt(rs - W), m = g.ptAt(rs), b = g.ptAt(rs + W);
      const v1 = [m[0] - a[0], m[1] - a[1]], v2 = [b[0] - m[0], b[1] - m[1]];
      const ang = Math.atan2(v1[0] * v2[1] - v1[1] * v2[0], v1[0] * v2[0] + v1[1] * v2[1]) * 180 / Math.PI;
      if (Math.abs(ang) >= 18) cand.push({ rs, ang, x: m[0], y: m[1] });
    }
    cand.sort((u, v) => Math.abs(v.ang) - Math.abs(u.ang));
    const picked = [];
    for (const c of cand) {
      if (picked.some((q) => Math.abs(q.rs - c.rs) < 600)) continue;
      if (c.rs < 300 || c.rs > g.L - 300) continue;
      picked.push(c);
    }
    g.turns = picked;
  });
  // keep at most ~12 points: all stations / structures, then the biggest turns
  const free = Math.max(3, 12 - wps.length);
  const turns = guide.legs.flatMap((g, li) => (g.turns || []).map((c) => ({ ...c, leg: li, g })))
    .sort((u, v) => Math.abs(v.ang) - Math.abs(u.ang)).slice(0, free);
  for (const c of turns) {
    if (wps.some((w) => Math.hypot(w.x - c.x, w.y - c.y) < 250)) continue;
    const multiPre = guide.legs.filter((g) => g.mode !== "water").length > 1 ? `${c.g.title}: ` : "";
    wps.push({ leg: c.leg, legTitle: multiPre, kind: "turn", s: c.rs, x: c.x, y: c.y, ang: c.ang,
      text: loc("Поворот {0}° {1}", Math.round(Math.abs(c.ang)), c.ang > 0 ? loc("влево") : loc("вправо")) });
  }
  wps.sort((u, v) => (u.leg - v.leg) || (u.s - v.s));
  wps.forEach((w, i) => { w.n = i + 1; });
  guide.wps = wps;
}

// Ask the server what lies on the straight chord the line avoids at each turn (water / hill / hollow).
async function requestTurnReasons(p, guide) {
  const turns = guide.wps.filter((w) => w.kind === "turn");
  if (!turns.length) return;
  const q = turns.map((w) => {
    const g = guide.legs[w.leg], D = Math.min(600, Math.max(250, g.L / 8));
    w.D = D;
    const a = g.ptAt(w.s - D), b = g.ptAt(w.s + D);
    return [a[0], a[1], b[0], b[1]].map((v) => Math.round(v)).join(",");
  }).join(";");
  try {
    const res = await loadJSON("/api/chords?q=" + encodeURIComponent(q));
    turns.forEach((w, i) => {
      const r = res[i], g = guide.legs[w.leg];
      if (!r) return;
      // route's own terrain heights over the same stretch
      const hs = (g.terrain || []).filter((t) => t[0] >= w.s - w.D && t[0] <= w.s + w.D).map((t) => t[1]);
      const rMax = hs.length ? Math.max(...hs) : r.mean_h, rMin = hs.length ? Math.min(...hs) : r.mean_h;
      const nearSt = guide.wps.some((o) => o.kind === "station" && o.leg === w.leg && Math.abs(o.s - w.s) < 500);
      w.reason = r.water_frac > 0.04 ? loc("обход воды (напрямую — озеро/река)")
        : r.max_h - rMax > 4 ? loc("обход холма (напрямую выше на {0} м)", fmtInt(r.max_h - rMax))
        : rMin - r.min_h > 4 ? loc("обход низины (напрямую ниже на {0} м)", fmtInt(rMin - r.min_h))
        : nearSt ? loc("подход к станции") : loc("по рельефу / к следующей точке");
    });
  } catch (e) {
    console.warn("chords", e);
    return;
  }
  if (curProject() === p) { renderProjectCard(); dirty = true; }
}

// corridor band + gradient-coloured line (graded legs) — replaces the plain line in focus mode
function drawGuideLine(guide, w) {
  const sc = S.view.scale, band = Math.max(10, 2 * GUIDE_CORRIDOR * sc);
  ctx.save();
  ctx.lineJoin = "round"; ctx.lineCap = "round";
  for (const g of guide.legs) {
    if (g.mode === "water") continue;
    tracePath(g.pts);
    ctx.strokeStyle = "rgba(143,189,242,0.17)"; ctx.lineWidth = band; ctx.stroke();
  }
  for (const g of guide.legs) {
    if (!g.graded) continue;
    tracePath(g.pts);
    ctx.strokeStyle = "rgba(0,0,0,0.85)"; ctx.lineWidth = w + 4; ctx.stroke();
    // pieces of 50 m, batched by gradient class
    const paths = GRADE_CLASSES.map(() => new Path2D());
    for (let x = 0; x < g.L; x += 50) {
      const x1 = Math.min(g.L, x + 50), cls = gradeClass(gradeAt(g.track, (x + x1) / 2, g.L));
      const s0 = x / g.scale, s1 = x1 / g.scale;
      const pts = slicePoly(g.pts, g.cum, s0, s1);
      const path = paths[cls];
      pts.forEach((q, i) => { const [sx, sy] = w2s(q[0], q[1]); i ? path.lineTo(sx, sy) : path.moveTo(sx, sy); });
    }
    ctx.lineWidth = w;
    paths.forEach((path, i) => { ctx.strokeStyle = GRADE_CLASSES[i].color; ctx.stroke(path); });
  }
  ctx.restore();
}

function guideColorAt(guide) {
  return (b) => {
    const g = guide.legs.find((l) => l.graded && projectOn(l.pts, l.cum, b.x, b.y).d < 80);
    return g ? GRADE_CLASSES[gradeClass(gradeAt(g.track, g.sAt(b.x, b.y), g.L))].color : (MODE_COLORS[b.mode] || "#ddd");
  };
}

// km ticks + numbered waypoints (on top of everything of the project)
function drawGuideMarks(guide) {
  const zf = zoomFactor();
  ctx.save();
  for (const g of guide.legs) {
    if (g.mode === "water") continue;
    const step = g.L <= 12000 ? 1000 : g.L <= 30000 ? 2000 : 5000;
    // skip ticks closer than ~45 px on screen
    if (step * S.view.scale < 45 && zf < 6) continue;
    for (let km = step; km < g.L - step * 0.3; km += step) {
      const a = g.ptAt(km - 20), b = g.ptAt(km + 20), m = g.ptAt(km);
      const [ax, ay] = w2s(a[0], a[1]), [bx, by] = w2s(b[0], b[1]), [mx, my] = w2s(m[0], m[1]);
      let nx = -(by - ay), ny = bx - ax;
      const n = Math.hypot(nx, ny) || 1; nx /= n; ny /= n;
      ctx.beginPath(); ctx.moveTo(mx - nx * 9, my - ny * 9); ctx.lineTo(mx + nx * 9, my + ny * 9);
      ctx.strokeStyle = "#000"; ctx.lineWidth = 4; ctx.stroke();
      ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.stroke();
      label(loc("{0} км", km / 1000), mx + nx * 16, my + ny * 16 + 4, "#ffffff", 11, nx > 0.3 ? "left" : nx < -0.3 ? "right" : "center", 600);
    }
  }
  S.wpHits = [];
  for (const w of guide.wps) {
    const [x, y] = w2s(w.x, w.y);
    if (x < -20 || y < -20 || x > W + 20 || y > H + 20) continue;
    const hl = (S.hover && S.hover.kind === "waypoint" && S.hover.obj === w) || S.selWp === w.n;
    const r = hl ? 12 : 10;
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = w.kind === "turn" ? "#1d4f86" : w.kind === "station" ? "#ffffff" : "#f3efe2"; ctx.fill();
    ctx.lineWidth = hl ? 3 : 2.2; ctx.strokeStyle = w.kind === "turn" ? "#8fbdf2" : "#1d4f86"; ctx.stroke();
    ctx.font = `700 11px system-ui, "Segoe UI", sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillStyle = w.kind === "turn" ? "#ffffff" : "#111"; ctx.fillText(String(w.n), x, y + 0.5);
    ctx.textBaseline = "alphabetic";
    S.wpHits.push({ w, x, y, r: r + 2 });
  }
  ctx.restore();
}

function wpTooltipHTML(w) {
  const g = projectGuide(curProject()).legs[w.leg];
  return loc("<div class=\"tt-title\">Точка {0}: {1}</div>\n    {2}\n    {3}\n    {4}\n    <div class=\"tt-muted\">км {5} от начала{6} · x {7}, y {8}</div>\n    <div class=\"tt-muted\">Проведите путь через эту точку (в пределах коридора ±{9} м)</div>", w.n, esc(w.text), w.legTitle ? `<div class="tt-sub">${esc(w.legTitle.replace(/:\s*$/, ""))}</div>` : "", w.reason ? loc("<div class=\"tt-sub\">Почему здесь: {0}</div>", esc(w.reason)) : "", w.sub ? `<div class="tt-sub">${esc(w.sub)}</div>` : "", fmt1(w.s / 1000), g && g.L ? loc(" из {0}", fmt1(g.L / 1000)) : "", fmtInt(w.x), fmtInt(w.y), GUIDE_CORRIDOR);
}

function guideCardHTML(p) {
  if (!p.path) return "";
  const guide = projectGuide(p);
  if (!guide.wps.length) return "";
  const rows = guide.wps.map((w) => loc("<li data-wp=\"{0}\" class=\"{1}\"><span class=\"wp-n wp-{2}\">{3}</span>\n    <span class=\"wp-t\">{4}{5}{6}\n    <span class=\"wp-c\">км {7} · ({8}, {9})</span></span></li>", w.n, S.selWp === w.n ? "selected" : "", w.kind, w.n, esc(w.legTitle || ""), esc(w.text), w.reason ? ` <span class="tt-muted">— ${esc(w.reason)}</span>` : "", fmt1(w.s / 1000), fmtInt(w.x), fmtInt(w.y))).join("");
  const grade = guide.graded ? loc(" Цвет линии — уклон пути (макс. {0}).", fmtPct(guide.maxGrade)) : "";
  return loc("<div class=\"pc-sub\">Как строить похоже: ключевые точки</div>\n    <div class=\"pc-empty\">Полоса вдоль линии — коридор ±{0} м: держитесь в ней и проходите через точки по порядку.{1} Клик — показать точку.</div>\n    <ol class=\"pc-wps\">{2}</ol>", GUIDE_CORRIDOR, grade, rows);
}

function selectWaypoint(n) {
  const p = curProject();
  if (!p) return;
  const w = projectGuide(p).wps.find((x) => x.n === n);
  if (!w) return;
  S.selWp = n;
  if (!S.focus) setFocus(true);
  animateTo({ cx: w.x, cy: w.y, scale: clampScale(Math.max(S.view.scale, 0.12)) });
  const now = performance.now();
  S.flash = { x: w.x, y: w.y, start: now, until: now + 1800 };
  renderProjectCard();
  dirty = true;
}

function renderGuideLegend() {
  const el = $("guide-legend");
  if (!el) return;
  const fp = focusProject();
  const guide = fp ? projectGuide(fp) : null;
  if (!guide) { el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = loc("<span class=\"gl-band\"></span>коридор ±{0} м\n    <span class=\"gl-wp\">1</span>ключевая точка <span class=\"gl-wp turn\">2</span>поворот\n    {1}\n    <span class=\"gl-bridge\"></span>мост <span class=\"gl-tunnel\"></span>тоннель", GUIDE_CORRIDOR, guide.graded ? loc("<b>уклон:</b> {0}", GRADE_CLASSES.map((c) => `<span class="gl-sw" style="background:${c.color}"></span>${c.label}`).join(" ")) : "");
}

// ------------------------------------------------------------------ year box (map) + "Здания этого года" tab

function setupItemYears() {
  S.itemYears = [...new Set(S.steps.filter((s) => s.action !== "note" && (s.action === "build_line" || s._project)).map((s) => s.year))]
    .sort((a, b) => a - b);
}

function stepYear(dir) {
  if (focusProject()) setFocus(false);
  const ys = S.itemYears || [];
  const next = dir > 0 ? ys.find((y) => y > S.year) : ys.slice().reverse().find((y) => y < S.year);
  setYear(next ?? S.year + dir);
}

function renderYearBox() {
  if (!S.plan || S.year == null) return;
  const st = yearStats(S.year), fp = focusProject(), ys = S.itemYears || [];
  $("yb-year").textContent = S.year;
  const parts = [];
  if (st.builds) parts.push(`<b>${st.builds}</b> ${plural(st.builds, loc("новая линия"), loc("новые линии"), loc("новых линий"))}`);
  if (st.veh) parts.push(loc("<b>{0}</b> {1} по технике", st.veh, plural(st.veh, loc("шаг"), loc("шага"), loc("шагов"))));
  if (st.newB) parts.push(loc("<a href=\"#\" data-yb=\"buildings\" title=\"Список зданий этого года\">{0} {1}{2}</a>", st.newB, plural(st.newB, loc("здание"), loc("здания"), loc("зданий")), st.cost ? " · " + fmtMoney(st.cost) : ""));
  if (st.demoN > 0.05) parts.push(loc("<a href=\"#\" data-yb=\"buildings\" class=\"neg\" title=\"Снос городских зданий под трассы и станции этого года (уже в стоимости){0}\">снос ~{1} зд.{2}{3}</a>", st.demoWarn ? loc(" — в каком-то городе репутация падает больше чем на 50 %") : "", fmtBld(st.demoN), st.demoCost ? " · " + fmtMoney(st.demoCost) : "", st.demoWarn ? " ⚠" : ""));
  if (!parts.length) {
    const nx = ys.find((y) => y > S.year);
    parts.push(loc("строек нет{0}", nx ? loc(" — следующие работы в {0} г. ▶", nx) : ""));
  }
  $("yb-info").innerHTML = parts.join(" · ");
  $("yb-focus").innerHTML = fp ? loc("Фокус: на карте только <b>{0}</b> <button data-yb=\"unfocus\" title=\"Клавиша F\">Показать весь год</button>", esc(fp.id)) : "";
  $("yb-focus").hidden = !fp;
  $("yb-prev").disabled = !ys.some((y) => y < S.year) && S.year <= S.minYear;
  $("yb-next").disabled = !ys.some((y) => y > S.year) && S.year >= S.maxYear;
  $("year-count").innerHTML = parts.join(" · ");
  $("buildings-count").textContent = st.newB ? `(${st.newB})` : "";
}

function renderBuildingsTab() {
  const box = $("buildings-list");
  if (!box || !S.plan || S.year == null) return;
  const vis = ensureVis(), fp = focusProject();
  $("buildings-title").textContent = fp ? loc("Здания проекта {0} ({1})", fp.id, fp.build.year) : loc("Здания этого года — {0}", S.year);
  const all = vis.buildings;
  if (!all.length) {
    box.innerHTML = `<div class="empty">${fp ? loc("У проекта нет зданий с координатами.") : loc("В {0} г. ничего не строим. Листайте годы ◀ ▶.", S.year)}</div>`;
    return;
  }
  const fresh = all.filter((b) => !b.existing && b.type !== "demolition");
  const cost = fresh.reduce((a, b) => a + (b.cost || 0), 0), noPrice = fresh.filter((b) => b.cost == null).length;
  const ex = all.filter((b) => b.existing).length;
  const dt = demoTotals(all), dTowns = Object.entries(dt.towns).filter(([, x]) => x >= 0.0005).sort((a, b) => b[1] - a[1]);
  const demoHTML = dt.n > 0.05 ? loc("<div class=\"bl-demo\"><span class=\"demo-chip\"></span>Снос городских зданий: <b class=\"neg\">~{0}</b>\n    на <b>{1}</b> <span class=\"tt-muted\">(уже в цене трасс и зданий)</span>\n    {2}\n    {3}</div>", fmtBld(dt.n), fmtMoney(dt.cost), dTowns.length ? `<div>${dTowns.map(([t, x]) => loc("<span class=\"{0}\">г. {1} −{2} %{3}</span>", x > DEMO_WARN ? "neg" : "", esc(t), fmt1(x * 100, 0), x > DEMO_WARN ? " ⚠" : "")).join(" · ")}</div>` : "", dTowns.some(([, x]) => x > DEMO_WARN) ? loc("<div class=\"neg\">⚠ > 50 % репутации: игра может запретить снос/станции. {0}</div>", esc(DEMO_RULE)) : "") : "";
  const html = [loc("<div class=\"bl-sum\">Строим <b>{0}</b> {1} на <b>{2}</b>\n    {3}\n    {4}{5}\n    <div class=\"tt-muted\">Клик по строке — показать здание и его охват на карте.</div></div>", fresh.length, plural(fresh.length, loc("объект"), loc("объекта"), loc("объектов")), fmtMoney(cost), noPrice ? loc("<span class=\"tt-muted\">(+ {0} без отдельной цены — в стоимости линии)</span>", noPrice) : "", ex ? loc("<br><span class=\"tt-muted\">Используем уже построенные: {0}</span>", ex) : "", demoHTML)];
  for (const type of BT_ORDER) {
    const list = all.filter((b) => b.type === type);
    if (!list.length) continue;
    list.sort((a, b) => (!!a.existing - !!b.existing) || String(a.name).localeCompare(String(b.name), "ru"));
    const t = BT[type], col = type === "depot" || type === "maint" ? "#9aa0a6" : t.color;
    const nNew = list.filter((b) => !b.existing).length, gc = list.reduce((a, b) => a + (b.existing ? 0 : b.cost || 0), 0);
    const ico = t.small ? `<span class="bico small ${type}" style="background:${t.color}"></span>` : `<span class="bico${t.round ? " round" : ""}" style="border-color:${col}">${t.icon}</span>`;
    html.push(`<div class="bl-group"><div class="bl-gh">${ico}<span class="bl-gl">${esc(t.label)}</span>
      <span class="bl-n">${nNew}${list.length > nNew ? loc(" <span class=\"tt-muted\">+{0} есть</span>", list.length - nNew) : ""}</span>
      <span class="bl-c">${gc ? fmtMoney(gc) : ""}</span></div><ul>`);
    for (const b of list) {
      const sel = b.key === S.selBuilding ? " selected" : "";
      const c = b.existing ? loc("<span class=\"pos\">есть</span>") : b.cost != null ? fmtMoney(b.cost) : "—";
      const what = type === "depot" || type === "maint" ? bLabel(b) + (b.name ? " · " + b.name : "") : (b.name || b.title || bLabel(b));
      html.push(`<li data-bkey="${esc(b.key)}" class="${sel}${b.existing ? " existing" : ""}">
        <span class="bl-name" title="${esc(what)}">${esc(what)}${b.approx ? loc(' <span class="tt-muted" title="положение примерное">≈</span>') : ""}</span>
        <span class="bl-size">${esc(b.size || b.title || "")}${b.demo && !b.existing ? loc(" <span class=\"neg\" title=\"{0}\">· снос ~{1} зд.</span>", esc(loc("Снос под площадку (в цене): ") + townsRep(b.demo.towns).map((r) => loc("г. ") + r.town + " −" + fmt1(r.loss * 100, 0) + " %").join(", ")), fmtBld(b.demo.buildings)) : ""}</span>
        <span class="bl-cost">${c}</span><span class="bl-lines" title="${esc(b.steps.map((s) => s.title).join("\n"))}">${esc(b.lines.join(", "))}</span></li>`);
    }
    html.push(`</ul></div>`);
  }
  box.innerHTML = html.join("");
}

// debug / screenshots: ?hoverb=warehouse shows the tooltip of the first visible building of that type
function debugHoverBuilding(type) {
  const b = ensureVis().buildings.find((x) => x.type === type);
  if (!b) { console.warn("hoverb: no visible building of type", type); return; }
  anim = null;
  S.view.cx = b.x; S.view.cy = b.y; S.view.scale = clampScale(Math.max(S.view.scale, 0.15));
  draw(performance.now());
  const [x, y] = BT[b.type].small ? w2s(b.x, b.y) : iconPos(b, iconSize());
  S.hover = { kind: "building", obj: b };
  dirty = true;
  showTooltip(S.hover, x, y);
}

// ------------------------------------------------------------------ hit testing & tooltip

function distToSeg(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, l2 = dx * dx + dy * dy;
  const t = l2 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / l2)) : 0;
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}
function distToPath(sx, sy, pts) {
  let best = Infinity, prev = null;
  for (const [x, y] of pts) {
    const p = w2s(x, y);
    if (prev) best = Math.min(best, distToSeg(sx, sy, prev[0], prev[1], p[0], p[1]));
    prev = p;
  }
  return best;
}

function hitTest(sx, sy) {
  // guide waypoints (focus mode) are on top, then building icons (last drawn wins)
  for (const h of S.wpHits || []) if (Math.hypot(sx - h.x, sy - h.y) <= h.r) return { kind: "waypoint", obj: h.w };
  for (let i = S.bHits.length - 1; i >= 0; i--) {
    const h = S.bHits[i];
    if (Math.abs(sx - h.x) <= h.r && Math.abs(sy - h.y) <= h.r) return { kind: "building", obj: h.b };
  }
  if (S.layers.towns) {
    for (const t of S.game.towns) {
      const [x, y] = w2s(t.x, t.y);
      if (Math.hypot(sx - x, sy - y) <= townRadius(t) + 3) return { kind: "town", obj: t };
    }
  }
  if (S.layers.industries) {
    for (const i of S.game.industries) {
      const [x, y] = w2s(i.x, i.y), r = industrySize(i) * 1.25 + 3;
      if (Math.abs(sx - x) <= r && Math.abs(sy - y) <= r) return { kind: "industry", obj: i };
    }
  }
  const fp = focusProject();
  if (fp) return distToPath(sx, sy, fp.path) < 9 ? { kind: "step", obj: fp.build } : null;
  if (S.layers.plan) {
    let best = null, bd = 7;
    const vis = ensureVis();
    for (const s of [...vis.builds, ...vis.vehicleLines, ...vis.earlier]) {
      if (!s._path) continue;
      const d = distToPath(sx, sy, s._path);
      if (d < bd) { bd = d; best = s; }
    }
    if (best) return { kind: "step", obj: best };
  }
  if (S.layers.routes) {
    for (const r of S.routes) {
      const pts = routePoints(r);
      if (pts && routeShown(r) && distToPath(sx, sy, pts) < 6) return { kind: "route", obj: r };
    }
  }
  return null;
}

function cargoList(arr) { return arr.length ? arr.map((c) => esc(c.name)).join(", ") : "—"; }

function tooltipHTML(h) {
  const o = h.obj;
  if (h.kind === "town") {
    const caps = Object.entries(o.capacities).map(([k, v]) => `${LAND_USE_LABEL[k] || k} ${v}`).join(" · ");
    const needs = Object.entries(o.needs).filter(([, l]) => l.length)
      .map(([k, l]) => `${LAND_USE_LABEL[k] || k}: ${cargoList(l)}`).join("<br>");
    const suppliers = (S.chainsBy.get("t" + o.id) || []).slice().sort((a, b) => a.d - b.d).slice(0, 4)
      .map((c) => `${esc(cargoName(c.cargo))} ← ${esc(c.from.name)} (${fmtKm(c.d)})`).join("<br>");
    return loc("<div class=\"tt-title\">{0}</div>\n      <div class=\"tt-sub\">Ёмкость {1}: {2}</div>\n      <div style=\"margin-top:4px\">Спрос:<br>{3}</div>\n      {4}", esc(o.name), o.total, caps, needs || "—", suppliers ? loc("<div class=\"tt-muted\" style=\"margin-top:4px\">Ближайшие поставщики:<br>{0}</div>", suppliers) : "");
  }
  if (h.kind === "industry") {
    const prod = Object.entries(o.production || {}).map(([k, v]) =>
      `${esc(cargoName(k))}: ${v.producedPerYear ? loc("произв. {0}/{1} в год", v.producedPerYear, v.maxProductionPerYear) : loc("потребл. макс {0}/год", v.maxConsumptionPerYear)}`).join("<br>");
    const cons = (S.chainsBy.get("i" + o.id) || []).filter((c) => c.from === o).sort((a, b) => a.d - b.d).slice(0, 5)
      .map((c) => `${esc(cargoName(c.cargo))} → ${esc(c.to.name)} (${fmtKm(c.d)})`).join("<br>");
    const sup = (S.chainsBy.get("i" + o.id) || []).filter((c) => c.to === o).sort((a, b) => a.d - b.d).slice(0, 4)
      .map((c) => `${esc(cargoName(c.cargo))} ← ${esc(c.from.name)} (${fmtKm(c.d)})`).join("<br>");
    const none = (S.noConsumer.get(o.id) || []).map(cargoName).join(", ");
    return loc("<div class=\"tt-title\">{0}</div>\n      <div class=\"tt-sub\">{1} · уровень {2}{3}{4}</div>\n      <div style=\"margin-top:4px\">{5} → <b>{6}</b></div>\n      {7}\n      {8}\n      {9}\n      {10}", esc(o.name), esc(o.type_name), o.level, o.max_level ? " / " + o.max_level : "", o.on_water ? loc(" · на воде") : "", cargoList(o.inputs), cargoList(o.outputs), prod ? `<div class="tt-muted" style="margin-top:4px">${prod}</div>` : "", sup ? loc("<div class=\"tt-muted\" style=\"margin-top:4px\">Ближайшие поставщики:<br>{0}</div>", sup) : "", cons ? loc("<div class=\"tt-muted\" style=\"margin-top:4px\">Ближайшие потребители:<br>{0}</div>", cons) : "", none ? loc("<div class=\"tt-muted\" style=\"margin-top:4px\">Нет потребителей на карте: {0}</div>", esc(none)) : "");
  }
  if (h.kind === "building") return buildingTooltipHTML(o);
  if (h.kind === "waypoint") return wpTooltipHTML(o);
  if (h.kind === "step") {
    return loc("<div class=\"tt-title\">{0}</div>\n      <div class=\"tt-sub\">{1}{2} · {3}{4}{5}{6}</div>\n      <div class=\"tt-muted\">Стоимость {7} · доход {8}/год · окупаемость {9}</div>\n      {10}\n      <div class=\"tt-muted\">{11}</div>", esc(o.title || o.action), o.year, o.month ? " · " + MONTHS[(o.month - 1) % 12] : "", esc(ACTION_LABEL[o.action] || o.action), o.kind ? " · " + KIND_LABEL[o.kind] : "", o.cargo ? " · " + esc(cargoName(o.cargo)) : "", o._project ? loc(" · проект ") + esc(o._project) : "", fmtMoney(o.cost), fmtMoney(o.income_per_year), fmtYears(o.roi_years), segTooltipHTML(o), focusProject() ? loc("Текущий проект") : loc("Клик — выбрать шаг") + (o._project ? loc(" · двойной клик — сделать проект текущим") : ""));
  }
  if (h.kind === "route") {
    const pts = routePoints(o);
    let len = 0;
    for (let i = 1; i < pts.length; i++) len += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    return `<div class="tt-title">${esc(o.name || o.title || loc("Маршрут"))}</div>
      <div class="tt-sub">${esc(KIND_LABEL[o.kind] || o.kind || "")} · ${fmtKm(o.length_m || len)}</div>
      ${o.cost !== undefined ? loc("<div class=\"tt-muted\">Стоимость {0}</div>", fmtMoney(o.cost)) : ""}`;
  }
  return "";
}

function showTooltip(h, sx, sy) {
  const tt = $("tooltip");
  if (!h) { tt.hidden = true; return; }
  tt.innerHTML = tooltipHTML(h);
  tt.hidden = false;
  const r = tt.getBoundingClientRect();
  let x = sx + 16, y = sy + 16;
  if (x + r.width > W - 8) x = sx - r.width - 16;
  if (y + r.height > H - 8) y = Math.max(8, H - r.height - 8);
  tt.style.left = x + "px"; tt.style.top = y + "px";
}

// ------------------------------------------------------------------ cashflow chart

const chart = { pts: [], hoverIdx: null };

function drawCashflow() {
  const cv = $("cashflow"), c2 = cv.getContext("2d");
  const w = cv.clientWidth, h = cv.clientHeight, r = window.devicePixelRatio || 1;
  cv.width = Math.round(w * r); cv.height = Math.round(h * r);
  c2.setTransform(r, 0, 0, r, 0, 0);
  c2.clearRect(0, 0, w, h);
  const data = ((S.plan && S.plan.cashflow) || []).filter((d) => Number.isFinite(d.year) && Number.isFinite(d.money))
    .sort((a, b) => a.year - b.year);
  chart.pts = [];
  if (!data.length) {
    c2.fillStyle = "#898781"; c2.font = "12px system-ui"; c2.fillText(loc("Нет данных cashflow"), 8, h / 2);
    return;
  }
  const padL = 58, padR = 10, padT = 10, padB = 20;
  const vals = data.map((d) => d.money);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  if (hi === lo) hi = lo + 1;
  const x0 = Math.min(S.minYear, data[0].year), x1 = Math.max(S.maxYear, data[data.length - 1].year);
  const X = (y) => padL + (x1 === x0 ? (w - padL - padR) / 2 : (y - x0) / (x1 - x0) * (w - padL - padR));
  const Y = (v) => padT + (hi - v) / (hi - lo) * (h - padT - padB);

  // gridlines: zero baseline + min/max ticks
  c2.font = "11px system-ui, 'Segoe UI', sans-serif";
  c2.textAlign = "right";
  for (const v of [hi, lo, 0]) {
    const y = Math.round(Y(v)) + 0.5;
    c2.strokeStyle = v === 0 ? "#383835" : "#2c2c2a"; c2.lineWidth = 1;
    c2.beginPath(); c2.moveTo(padL, y); c2.lineTo(w - padR, y); c2.stroke();
    c2.fillStyle = "#898781"; c2.fillText(fmtMoney(v), padL - 6, y + 4);
  }
  // year ticks: first, selected, last
  c2.textAlign = "center";
  const ticks = [...new Set([x0, x1])];
  for (const yr of ticks) {
    c2.fillStyle = "#898781";
    c2.textAlign = yr === x0 && x0 !== x1 ? "left" : yr === x1 && x0 !== x1 ? "right" : "center";
    c2.fillText(String(yr), X(yr) + (c2.textAlign === "left" ? -4 : c2.textAlign === "right" ? 4 : 0), h - 5);
  }
  c2.textAlign = "center";

  // selected year marker
  if (S.year !== null) {
    const sx = X(S.year);
    c2.strokeStyle = "rgba(57,135,229,0.55)"; c2.lineWidth = 1;
    c2.beginPath(); c2.moveTo(sx, padT); c2.lineTo(sx, h - padB); c2.stroke();
    if (!ticks.some((t) => Math.abs(X(t) - sx) < 34)) { c2.fillStyle = "#c3c2b7"; c2.fillText(String(S.year), sx, h - 5); }
  }

  // area + line
  const P = data.map((d) => [X(d.year), Y(d.money)]);
  c2.beginPath(); P.forEach(([x, y], i) => i ? c2.lineTo(x, y) : c2.moveTo(x, y));
  c2.lineTo(P[P.length - 1][0], Y(0)); c2.lineTo(P[0][0], Y(0)); c2.closePath();
  c2.fillStyle = "rgba(57,135,229,0.14)"; c2.fill();
  c2.beginPath(); P.forEach(([x, y], i) => i ? c2.lineTo(x, y) : c2.moveTo(x, y));
  c2.strokeStyle = "#3987e5"; c2.lineWidth = 2; c2.lineJoin = "round"; c2.stroke();

  data.forEach((d, i) => {
    const [x, y] = P[i];
    const sel = d.year === S.year, hov = i === chart.hoverIdx;
    if (sel || hov) {
      c2.beginPath(); c2.arc(x, y, 4.5, 0, Math.PI * 2);
      c2.fillStyle = d.money < 0 ? "#e66767" : "#3987e5"; c2.fill();
      c2.lineWidth = 2; c2.strokeStyle = "#1a1a19"; c2.stroke();
    }
    chart.pts.push({ x, y, d });
  });
  if (chart.hoverIdx !== null && chart.pts[chart.hoverIdx]) {
    const x = chart.pts[chart.hoverIdx].x;
    c2.strokeStyle = "rgba(255,255,255,0.25)"; c2.beginPath(); c2.moveTo(x, padT); c2.lineTo(x, h - padB); c2.stroke();
  }
}

function chartHover(e) {
  const cv = $("cashflow"), rect = cv.getBoundingClientRect(), mx = e.clientX - rect.left;
  const tip = $("chart-tip");
  if (!chart.pts.length) return;
  let bi = 0;
  chart.pts.forEach((p, i) => { if (Math.abs(p.x - mx) < Math.abs(chart.pts[bi].x - mx)) bi = i; });
  chart.hoverIdx = bi;
  drawCashflow();
  const { x, d } = chart.pts[bi];
  tip.innerHTML = loc("<b>{0}</b> · деньги <b class=\"{1}\">{2}</b><br>\n    <span class=\"muted\">доход</span> {3} · <span class=\"muted\">расходы</span> {4}\n    {5}", d.year, d.money < 0 ? "neg" : "", fmtMoney(d.money), fmtMoney(d.income), fmtMoney(d.costs), d.loans !== undefined ? loc("<br><span class=\"muted\">долг</span> {0}", fmtMoney(d.loans)) : "");
  tip.hidden = false;
  const tw = tip.offsetWidth;
  tip.style.left = Math.max(0, Math.min(rect.width - tw, x - tw / 2)) + "px";
  tip.style.top = "-6px";
  tip.style.transform = "translateY(-100%)";
}

// ------------------------------------------------------------------ existing roads / tracks (GET /api/network)
// Layer "Дороги и пути (из игры)": streets and tracks already in the game (export v6 `network`), drawn
// under every plan layer. Payload: classes[cls][normal|bridge|tunnel] = [[x0, y0, x1, y1, ...], ...] (m).

S.layers.network = true;
const NET = { status: "loading", reason: "", km: {}, groups: [] };
// draw order (bottom -> top); w = line width in px at fit zoom, grows with zoom
const NET_STYLE = {
  town: { label: loc("Городские улицы"), color: "rgba(226,222,208,0.62)", w: 0.9, minZf: 1.6 },
  other: { label: loc("Аэродромы (рулёжки, ВПП)"), color: "rgba(170,170,170,0.55)", w: 1.0, minZf: 1.6 },
  country: { label: loc("Загородные дороги"), color: "rgba(176,174,166,0.9)", w: 1.2, casing: "rgba(20,20,20,0.35)" },
  tram: { label: loc("Трамвайные пути"), color: "rgba(240,127,184,0.85)", w: 1.0, dash: [3, 3], minZf: 1.3 },
  highway: { label: loc("Автомагистрали"), color: "#ece4c4", w: 2.6, casing: "rgba(15,15,15,0.75)" },
  track_other: { label: loc("Ж/д пути (чужие, промышленные)"), color: "#86403c", w: 1.3, ties: "rgba(196,170,150,0.8)" },
  player_road: { label: loc("Наши дороги"), color: "#ff982e", w: 2.0, casing: "rgba(15,15,15,0.75)" },
  track_player: { label: loc("Наши ж/д пути"), color: "#a3161b", w: 1.8, ties: "rgba(240,226,206,0.95)" },
};
const NET_ORDER = Object.keys(NET_STYLE);

async function loadNetwork() {
  try {
    const d = await loadJSON("/api/network");
    if (!d.available) { NET.status = "off"; NET.reason = d.reason || ""; }
    else {
      NET.km = d.km || {};
      NET.groups = [];
      for (const cls of NET_ORDER) {
        const byStruct = (d.classes || {})[cls];
        if (!byStruct) continue;
        for (const st of ["normal", "bridge", "tunnel"]) {
          const lines = (byStruct[st] || []).map((a) => {
            let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
            for (let i = 0; i < a.length; i += 2) {
              const x = a[i], y = a[i + 1];
              if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
            }
            return { p: a, x0, y0, x1, y1, size: Math.max(x1 - x0, y1 - y0) };
          });
          if (lines.length) NET.groups.push({ cls, st, lines });
        }
      }
      NET.status = "ok";
    }
  } catch (e) {
    NET.status = "off"; NET.reason = loc("сервер не отдал /api/network: ") + e.message;
    console.warn(e);
  }
  const cb = document.querySelector('#layers input[data-layer="network"]');
  if (cb) {
    cb.disabled = NET.status !== "ok";
    cb.closest("label").title = NET.status === "ok"
      ? loc("Улицы, дороги и пути, уже построенные в игре (из экспорта)")
      : loc("Дорожная сеть ещё не выгружена: ") + NET.reason;
  }
  const note = $("net-note");
  if (note) {
    note.hidden = NET.status === "ok";
    note.title = NET.reason;
    const t = note.querySelector(".nn-text");
    if (t) t.textContent = loc("Дорожная сеть ещё не выгружена — перезапустите игру и нажмите экспорт");
  }
  renderNetLegend();
  dirty = true;
}

function netWidth(base, zf) { return base * Math.max(0.8, Math.min(2.8, 0.85 + 0.45 * Math.log2(Math.max(zf, 0.5)))); }

function drawNetwork() {
  if (NET.status !== "ok") return;
  const zf = zoomFactor(), sc = S.view.scale;
  const [vx0, vy1] = s2w(-20, -20), [vx1, vy0] = s2w(W + 20, H + 20);
  const minSize = 1.5 / sc;              // skip polylines smaller than ~1.5 px
  const tol2 = 1.1 * 1.1;                // screen-space decimation, px²
  ctx.save();
  ctx.lineJoin = "round"; ctx.lineCap = "round";
  for (const g of NET.groups) {
    const s = NET_STYLE[g.cls];
    if (s.minZf && zf < s.minZf) continue;
    // fade zoom-limited classes in over the first ~40 % of zoom above their threshold
    const fade = s.minZf ? Math.min(1, (zf - s.minZf) / (s.minZf * 0.4)) : 1;
    ctx.beginPath();
    let n = 0;
    for (const L of g.lines) {
      if (L.x1 < vx0 || L.x0 > vx1 || L.y1 < vy0 || L.y0 > vy1 || L.size < minSize) continue;
      const a = L.p;
      let [px, py] = w2s(a[0], a[1]);
      ctx.moveTo(px, py);
      const last = a.length - 2;
      for (let i = 2; i <= last; i += 2) {
        const [sx, sy] = w2s(a[i], a[i + 1]);
        if (i < last && (sx - px) ** 2 + (sy - py) ** 2 < tol2) continue;
        ctx.lineTo(sx, sy); px = sx; py = sy;
      }
      n++;
    }
    if (!n) continue;
    const w = netWidth(s.w, zf);
    ctx.globalAlpha = fade * (g.st === "tunnel" ? 0.55 : 1);
    ctx.setLineDash([]);
    if (g.st === "bridge") {
      // bridge: dark outer casing + light deck under the way
      ctx.strokeStyle = "rgba(10,10,10,0.9)"; ctx.lineWidth = w + 5; ctx.stroke();
      ctx.strokeStyle = "#f3efe2"; ctx.lineWidth = w + 3; ctx.stroke();
    } else if (s.casing && g.st === "normal") {
      ctx.strokeStyle = s.casing; ctx.lineWidth = w + 2; ctx.stroke();
    }
    if (s.ties && g.st !== "tunnel" && zf >= 1.3) {
      // sleepers: light cross ticks sticking out on both sides of the dark rail line
      ctx.strokeStyle = s.ties; ctx.lineWidth = w + 5; ctx.lineCap = "butt";
      ctx.setLineDash([1.5, Math.max(3.5, Math.min(8, 2.2 * zf))]); ctx.stroke();
      ctx.lineCap = "round"; ctx.setLineDash([]);
    }
    ctx.strokeStyle = s.color; ctx.lineWidth = w;
    ctx.setLineDash(g.st === "tunnel" ? [5, 4] : (s.dash || []));
    ctx.stroke();
  }
  ctx.restore();
}

// tiny SVG sample of a network class for the legend
function netSwatch(cls, st = "normal") {
  const s = NET_STYLE[cls], w = Math.max(1.2, s.w * 1.2);
  const line = (col, lw, extra = "") => `<line x1="2" y1="6" x2="22" y2="6" stroke="${col}" stroke-width="${lw}" ${extra}/>`;
  let body = "";
  if (st === "bridge") body += line("#0a0a0a", w + 5) + line("#f3efe2", w + 3);
  else if (s.casing) body += line(s.casing, w + 2);
  if (s.ties && st !== "tunnel") body += line(s.ties, w + 4, `stroke-dasharray="1.5 3.5"`);
  const dash = st === "tunnel" ? `stroke-dasharray="5 4" opacity=".6"` : s.dash ? `stroke-dasharray="${s.dash.join(" ")}"` : "";
  body += line(s.color, w, dash);
  return `<svg class="net-swatch" width="24" height="12" viewBox="0 0 24 12">${body}</svg>`;
}

function renderNetLegend() {
  const box = $("legend-net");
  if (!box) return;
  if (NET.status === "loading") { box.innerHTML = ""; return; }
  const rows = [loc("<div class=\"legend-sep\" style=\"margin-top:0\">Дороги и пути (из игры)</div>")];
  if (NET.status !== "ok") {
    rows.push(loc("<div class=\"legend-row tt-muted\" title=\"{0}\">ещё не выгружены из игры</div>", esc(NET.reason)));
  } else {
    const fmt = (km) => (km >= 10 ? Math.round(km) : km.toFixed(1).replace(".", ",")) + loc(" км");
    for (const cls of NET_ORDER.slice().reverse()) {
      if (!(cls in NET.km)) continue;
      const s = NET_STYLE[cls];
      const hint = s.minZf ? loc(" title=\"Видны только при приближении\"") : "";
      rows.push(`<div class="legend-row"${hint}>${netSwatch(cls)}<span>${esc(s.label)} · ${fmt(NET.km[cls])}${s.minZf ? loc(' <span class="tt-muted">(вблизи)</span>') : ""}</span></div>`);
    }
    const has = (st) => NET.groups.some((g) => g.st === st);
    const ref = NET.groups.find((g) => g.st !== "normal");
    if (has("bridge")) rows.push(loc("<div class=\"legend-row\">{0}Мост</div>", netSwatch(ref ? ref.cls : "country", "bridge")));
    if (has("tunnel")) rows.push(loc("<div class=\"legend-row\">{0}Тоннель (пунктир)</div>", netSwatch(ref ? ref.cls : "country", "tunnel")));
  }
  box.innerHTML = rows.join("");
}

// ------------------------------------------------------------------ legend

function buildLegend() {
  const rows = [];
  rows.push(`<div id="legend-net"></div>`);   // filled by renderNetLegend() (existing roads / tracks)
  rows.push(loc("<div class=\"legend-sep\" style=\"margin-top:0\">Здания (слой «Здания»)</div>"));
  for (const type of BT_ORDER) {
    const t = BT[type], col = type === "depot" || type === "maint" ? "#9aa0a6" : t.color;
    const ico = t.small ? `<span class="bico small ${type}" style="background:${t.color}"></span>`
      : `<span class="bico${t.round ? " round" : ""}" style="border-color:${col}">${t.icon}</span>`;
    const extra = type === "depot" || type === "maint" ? loc(" — рамка цвета вида транспорта") : "";
    rows.push(`<div class="legend-row">${ico}${esc(t.label)}${extra}</div>`);
  }
  rows.push(loc("<div class=\"legend-row\"><span class=\"bico existing\" style=\"border-color:#eda100\">🚚</span>серое — уже построено другой линией</div>"));
  rows.push(loc("<div class=\"legend-row tt-muted\">Наведите на здание — размер, цена, круг охвата</div>"));
  rows.push(loc("<div class=\"legend-sep\">Плечи мультимодальных линий</div>"));
  for (const [k, txt] of [["water", loc("Судно (пунктир)")], ["road", loc("Грузовики / автобус")], ["rail", loc("Ж/д (со шпалами)")], ["tram", loc("Трамвай")]]) {
    const style = k === "water" || k === "tram" ? "dashed" : "solid";
    rows.push(`<div class="legend-row"><span class="legend-line" style="border-top-color:${MODE_COLORS[k]};border-top-style:${style}"></span>${txt}</div>`);
  }
  rows.push(loc("<div class=\"legend-sep\">Трасса дороги / ж/д плана</div>"));
  rows.push(loc("<div class=\"legend-row\"><span class=\"legend-line seg-new\" style=\"border-top-color:{0}\"></span>Строить (новый участок)</div>", MODE_COLORS.road));
  rows.push(loc("<div class=\"legend-row\"><span class=\"legend-line seg-existing\" style=\"border-top-color:{0}\"></span>Используем существующую дорогу</div>", MODE_COLORS.road));
  rows.push(loc("<div class=\"legend-row\"><span class=\"legend-line seg-upgrade\" style=\"border-top-color:{0}\"></span>Улучшить существующую дорогу</div>", MODE_COLORS.road));
  rows.push(loc("<div class=\"legend-row\"><span class=\"legend-hatch\"></span>Снос городских зданий (красная штриховка + метка «снос»: дома, цена, репутация города)</div>"));
  rows.push(loc("<div class=\"legend-row tt-muted\">Туда и обратно по одной дороге — линия рисуется один раз (↔)</div>"));
  rows.push(loc("<div class=\"legend-row\"><span class=\"legend-line\" style=\"border-top-color:#8a8a8a;opacity:.6\"></span>Построено ранее (если включено)</div>"));
  rows.push(loc("<div class=\"legend-row\"><span class=\"legend-swatch\" style=\"background:{0};border-radius:50%;border-color:#fff\"></span>Город (размер — ёмкость)</div>", TOWN_FILL));
  rows.push(loc("<details class=\"legend-more\"><summary>Предприятия и цепочки</summary>"));
  const seen = new Map();
  for (const i of S.game.industries) if (!seen.has(i.type)) seen.set(i.type, i);
  for (const [type, i] of seen) {
    const shape = i.on_water ? "transform:rotate(45deg) scale(.85);" : "";
    rows.push(`<div class="legend-row"><span class="legend-swatch" style="background:${S.typeColor.get(type)};${shape}"></span>${esc(i.type_name)}</div>`);
  }
  rows.push(loc("<div class=\"legend-row\"><span class=\"legend-line\" style=\"border-top:2px dashed rgba(255,255,255,.5)\"></span>Цепочка: производитель → потребитель</div>"));
  rows.push(`</details>`);
  $("legend-body").innerHTML = rows.join("");
  renderNetLegend();
}

// ------------------------------------------------------------------ search

function setupSearch() {
  const input = $("search"), list = $("search-results");
  let items = [], active = 0;
  const go = (it) => {
    list.hidden = true;
    input.value = it.name;
    const z = Math.max(S.view.scale, S.fitScale * 4);
    animateTo({ cx: it.x, cy: it.y, scale: clampScale(z) });
    const now = performance.now();
    S.flash = { x: it.x, y: it.y, start: now, until: now + 2700 };
  };
  const render = () => {
    list.innerHTML = items.map((it, i) =>
      `<li data-i="${i}" class="${i === active ? "active" : ""}"><span>${esc(it.name)}</span><span class="kind">${esc(it.kind)}</span></li>`).join("");
    list.hidden = !items.length;
  };
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    if (!q || !S.game) { items = []; render(); return; }
    const all = [
      ...S.game.towns.map((t) => ({ name: t.name, kind: loc("город"), x: t.x, y: t.y, w: 0 })),
      ...S.game.industries.map((i) => ({ name: i.name, kind: i.type_name, x: i.x, y: i.y, w: 1 })),
    ];
    items = all.filter((it) => it.name.toLowerCase().includes(q) || it.kind.toLowerCase().includes(q))
      .sort((a, b) => (a.name.toLowerCase().indexOf(q) !== 0) - (b.name.toLowerCase().indexOf(q) !== 0) || a.w - b.w || a.name.localeCompare(b.name, "ru"))
      .slice(0, 15);
    active = 0; render();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { active = Math.min(items.length - 1, active + 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active = Math.max(0, active - 1); render(); e.preventDefault(); }
    else if (e.key === "Enter" && items[active]) go(items[active]);
    else if (e.key === "Escape") { list.hidden = true; input.blur(); }
  });
  list.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li");
    if (li) { e.preventDefault(); go(items[+li.dataset.i]); }
  });
  input.addEventListener("blur", () => setTimeout(() => { list.hidden = true; }, 120));
  input.addEventListener("focus", () => { if (items.length) list.hidden = false; });
}

// ------------------------------------------------------------------ UI wiring

function resize() {
  const wrap = $("map-wrap");
  dpr = window.devicePixelRatio || 1;
  W = wrap.clientWidth; H = wrap.clientHeight;
  canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
  if (S.game) {
    const b = S.game.terrain.image_bounds;
    S.fitScale = Math.min(W / (b.maxX - b.minX), H / (b.maxY - b.minY)) * 0.98;
  }
  dirty = true;
  if (S.plan) drawCashflow();
  if (DS.open) drawDossierCharts();
}

function setupUI() {
  window.addEventListener("resize", resize);
  document.querySelectorAll("#layers input[data-layer]").forEach((cb) => {
    cb.addEventListener("change", () => { S.layers[cb.dataset.layer] = cb.checked; dirty = true; });
  });
  $("show-built").addEventListener("change", (e) => {
    S.showBuilt = e.target.checked; store.set(LS.showBuilt, S.showBuilt); renderYearBox(); dirty = true;
  });
  $("yb-prev").addEventListener("click", () => stepYear(-1));
  $("yb-next").addEventListener("click", () => stepYear(1));
  const ybClick = (e) => {
    const a = e.target.closest("[data-yb]");
    if (!a) return;
    e.preventDefault();
    if (a.dataset.yb === "unfocus") setFocus(false);
    if (a.dataset.yb === "buildings") setTab("buildings");
  };
  $("yearbox").addEventListener("click", ybClick);
  $("year-count").addEventListener("click", ybClick);
  $("tab-buildings").addEventListener("click", (e) => {
    const li = e.target.closest("[data-bkey]");
    if (li) selectBuilding(li.dataset.bkey, true);
  });
  $("list-all").addEventListener("change", (e) => { S.listAll = e.target.checked; renderSteps(); });
  $("year").addEventListener("input", (e) => setYear(+e.target.value));
  $("year-prev").addEventListener("click", () => setYear(S.year - 1));
  $("year-next").addEventListener("click", () => setYear(S.year + 1));
  $("zoom-in").addEventListener("click", () => zoomAt(W / 2, H / 2, 1.5));
  $("zoom-out").addEventListener("click", () => zoomAt(W / 2, H / 2, 1 / 1.5));
  $("zoom-fit").addEventListener("click", () => fitAll());
  $("legend-toggle").addEventListener("click", () => {
    const l = $("legend"); l.classList.toggle("collapsed");
    $("legend-toggle").textContent = l.classList.contains("collapsed") ? "+" : "–";
  });
  $("steps").addEventListener("click", (e) => {
    const li = e.target.closest("li[data-id]");
    if (li) selectStep(li.dataset.id);
  });
  const cv = $("cashflow");
  cv.addEventListener("mousemove", chartHover);
  cv.addEventListener("mouseleave", () => { chart.hoverIdx = null; $("chart-tip").hidden = true; drawCashflow(); });
  cv.addEventListener("click", () => { if (chart.hoverIdx !== null) setYear(chart.pts[chart.hoverIdx].d.year); });
  window.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" && e.target.type !== "range" && e.target.type !== "checkbox") return;
    if (e.key === "ArrowLeft" && e.target.type !== "range") setYear(S.year - 1);
    if (e.key === "ArrowRight" && e.target.type !== "range") setYear(S.year + 1);
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    if ((e.key === "f" || e.key === "F" || e.key === "а" || e.key === "А") && curProject()) setFocus(!S.focus);
    if (e.key === "n" || e.key === "N" || e.key === "т" || e.key === "Т") nextProject();
  });
  setupSearch();
  setupProjects();
  setupDossier();

  // pan / zoom / hover / click on the map
  let drag = null;
  canvas.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    anim = null;
    drag = { x: e.offsetX, y: e.offsetY, cx: S.view.cx, cy: S.view.cy, moved: false };
  });
  window.addEventListener("mousemove", (e) => {
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
    if (drag) {
      const dx = sx - drag.x, dy = sy - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      if (drag.moved) {
        S.view.cx = drag.cx - dx / S.view.scale;
        S.view.cy = drag.cy + dy / S.view.scale;
        canvas.classList.add("dragging");
        $("tooltip").hidden = true;
        dirty = true;
        return;
      }
    }
    if (e.target !== canvas || !S.game) return;
    const [wx, wy] = s2w(sx, sy);
    $("coords").textContent = `x ${Math.round(wx)}  y ${Math.round(wy)}`;
    const h = hitTest(sx, sy);
    const changed = (h && h.obj) !== (S.hover && S.hover.obj);
    S.hover = h;
    canvas.classList.toggle("hovering", !!h);
    showTooltip(h, sx, sy);
    if (changed) dirty = true;
  });
  window.addEventListener("mouseup", (e) => {
    if (!drag) return;
    const wasDrag = drag.moved;
    drag = null;
    canvas.classList.remove("dragging");
    if (wasDrag || e.target !== canvas) return;
    const h = S.hover;
    if (h && h.kind === "waypoint") selectWaypoint(h.obj.n);
    else if (h && h.kind === "building") selectBuilding(h.obj.key, true);
    else if (h && h.kind === "step") { if (!focusProject()) { setTab("steps"); selectStep(h.obj.id, false); } }
    else if (!h && (S.selectedStep || S.selBuilding)) {
      S.selectedStep = null; S.selBuilding = null; renderSteps(); renderBuildingsTab(); dirty = true;
    }
  });
  canvas.addEventListener("mouseleave", () => { S.hover = null; $("tooltip").hidden = true; dirty = true; });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    anim = null;
    const delta = e.deltaMode === 1 ? e.deltaY * 33 : e.deltaY;
    zoomAt(e.offsetX, e.offsetY, Math.pow(1.0018, -delta));
  }, { passive: false });
  canvas.addEventListener("dblclick", (e) => {
    const h = S.hover;
    if (h && h.kind === "step" && h.obj._project && !focusProject()) { setTab("projects"); setCurrentProject(h.obj._project); return; }
    zoomAt(e.offsetX, e.offsetY, 2);
  });
}

// ------------------------------------------------------------------ route dossier ("Подробный разбор")
// Data: GET /api/dossiers (data/dossiers.json, built by `python -m tf3planner.dossier`) keyed by line id,
// or a `dossier` object carried by the build_line step itself (see PLAN_SCHEMA.md).

const DS = { open: false, data: null, meta: null, state: "idle", charts: [], error: null };
const LS_DS_CHECKS = "tf3.dossierChecks";

async function loadDossiers() {
  const inline = S.steps.some((s) => s.dossier);
  DS.state = inline ? "ok" : "loading";
  renderProjectCard();
  try {
    const d = await loadJSON("/api/dossiers");
    DS.meta = d && d._meta ? d._meta : null;
    DS.data = d || {};
    // line ids may repeat in a plan (a later phase re-uses "L22"): index dossiers by their build step id too
    DS.byStep = new Map();
    for (const [k, v] of Object.entries(DS.data)) if (!k.startsWith("_") && v && v.step_id != null) DS.byStep.set(String(v.step_id), v);
    DS.state = "ok";
  } catch (e) {
    DS.state = inline ? "ok" : "missing";
    DS.error = e.message;
  }
  S.bVer++;            // building positions now come from dossiers
  renderProjectCard();
  renderGuideLegend();
  renderYearBox();
  renderBuildingsTab();
  dirty = true;
  if (DS.open) renderDossier();
  const q = new URLSearchParams(location.search);
  if (q.has("hoverb")) setTimeout(() => debugHoverBuilding(q.get("hoverb")), 450);
}

function dossierFor(p) {
  return p ? dossierForStep(p.build) : null;
}

function dossierForStep(b) {
  if (!b) return null;
  if (b.dossier) return b.dossier;
  if (!DS.data) return null;
  const byStep = DS.byStep && DS.byStep.get(String(b.id));
  if (byStep) return byStep;
  const d = DS.data[String(b.line ?? b.id)];
  return d && (d.step_id == null || String(d.step_id) === String(b.id)) ? d : null;
}

function dossierStale(p, d) {
  if (!p || !d) return false;
  if (d.title && p.build.title && d.title !== p.build.title) return true;
  return !!(DS.meta && DS.meta.plan_generated && S.plan && S.plan.generated && DS.meta.plan_generated !== S.plan.generated);
}

const okNum = (x) => x !== null && x !== undefined && isFinite(x);
const fmt1 = (x, d = 1) => okNum(x) ? Number(x).toFixed(d).replace(".", ",") : "—";
const fmtInt = (x) => okNum(x) ? nf.format(Math.round(x)) : "—";
const fmtPct = (x, d = 1) => okNum(x) ? (x * 100).toFixed(d).replace(".", ",") + " %" : "—";
const fmtSec = (s) => okNum(s) ? loc("{0} с <span class=\"tt-muted\">({1} дн.)</span>", fmtInt(s), fmtInt(s / 4)) : "—";

function dossierCardHTML(p) {
  const d = dossierFor(p);
  const head = loc("<div class=\"pc-sub\">Подробный разбор</div>");
  if (!d) {
    const msg = DS.state === "loading" ? loc("Загрузка подробного разбора…")
      : DS.state === "missing" ? loc("Разбор не найден: запустите <code>python -m tf3planner.dossier</code> (сервер должен отдавать <code>/api/dossiers</code>).")
      : loc("Для этой линии разбора нет — перестройте: <code>python -m tf3planner.dossier --line ") + esc(p.id) + "</code>.";
    return head + `<div class="pc-empty">${msg}</div>`;
  }
  if (d.error) return head + loc("<div class=\"pc-empty neg\">Ошибка разбора: {0}</div>", esc(d.error));
  const legs = (d.legs || []).filter((l) => !l.error);
  if (!legs.length) return head + loc("<div class=\"pc-empty\">В разборе нет рассчитанных плеч.</div>");
  const L = legs[0], t = L.timing, c = L.capacity;
  const stat = (k, v, cls = "") => `<div class="stat"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
  const warn = (d.warnings || []).length;
  return head + loc("\n    {0}\n    <div class=\"pc-nums\">\n      {1}\n      {2}\n      {3}\n      {4}\n      {5}\n      {6}\n    </div>\n    <div class=\"pc-actions\"><button id=\"pc-dossier\" class=\"primary\" title=\"Маршрут, сооружения, станции, состав, разгон, время, деньги и пошаговая инструкция\">Открыть подробный разбор{7}</button></div>", dossierStale(p, d) ? loc("<div class=\"ds-stale\">Разбор построен для другой версии плана — перестройте его.</div>") : "", stat(loc("Круг"), loc("{0} с", fmtInt(t.round_trip_s))), stat(loc("Рейсов/год"), fmt1(t.trips_per_year, 2)), stat(loc("Загрузка парка"), fmtPct(c.load_factor, 0), c.load_factor < 0.6 ? "neg" : ""), stat(loc("Гружёным ср./макс"), loc("{0}/{1} км/ч", fmt1(t.loaded.avg_kmh, 0), fmt1(t.loaded.max_kmh, 0))), stat(loc("Уклон пути ≤"), t.ruling_grade != null ? fmtPct(t.ruling_grade) : "—"), stat(loc("Прибыль/год"), fmtMoney(d.totals.profit_per_year), d.totals.profit_per_year >= 0 ? "pos" : "neg"), warn ? ` · ⚠ ${warn}` : "");
}

function setupDossier() {
  const el = document.createElement("div");
  el.id = "dossier";
  el.hidden = true;
  el.innerHTML = loc("<div class=\"ds-head\"><span class=\"section-title\">Подробный разбор</span><span class=\"proj-id\" id=\"ds-id\"></span>\n      <span class=\"ds-title\" id=\"ds-title\"></span><button id=\"ds-close\" title=\"Закрыть (Esc)\">×</button></div>\n    <div class=\"ds-body\" id=\"ds-body\"></div>");
  $("map-wrap").appendChild(el);
  $("ds-close").addEventListener("click", closeDossier);
  // map shortcuts (arrows, F, N) must not fire behind the open dossier
  window.addEventListener("keydown", (e) => {
    if (!DS.open) return;
    if (e.key === "Escape") { closeDossier(); e.preventDefault(); }
    if (!(e.target.tagName === "INPUT" && e.target.type === "search")) e.stopImmediatePropagation();
  }, true);
  el.addEventListener("change", (e) => {
    const cb = e.target.closest("input[data-chk]");
    const p = curProject();
    if (!cb || !p) return;
    const all = store.get(LS_DS_CHECKS, {}) || {};
    const set = new Set(all[p.sig] || []);
    if (cb.checked) set.add(+cb.dataset.chk); else set.delete(+cb.dataset.chk);
    all[p.sig] = [...set];
    store.set(LS_DS_CHECKS, all);
    cb.closest("li").classList.toggle("checked", cb.checked);
    const pr = $("ds-chk-progress");
    if (pr) pr.textContent = loc("{0} из {1}", el.querySelectorAll("input[data-chk]:checked").length, el.querySelectorAll("input[data-chk]").length);
  });
}

function openDossier() {
  if (!curProject()) return;
  DS.open = true;
  $("dossier").hidden = false;
  renderDossier();
}

function closeDossier() {
  DS.open = false;
  $("dossier").hidden = true;
  DS.charts = [];
}

function kv(rows) {
  return `<table class="ds-kv">${rows.filter(Boolean).map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")}</table>`;
}

function tbl(head, rows, cls = "") {
  return `<table class="ds-tbl ${cls}"><thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

function legHTML(L, i, multi) {
  const r = L.route, cons = L.consist, t = L.timing, cap = L.capacity, m = L.money;
  const parts = [];
  if (multi) parts.push(`<h3 class="ds-leg">${L.title ? esc(L.title) : `${esc(L.role_ru)}: ${esc(L.from.name)} → ${esc(L.to.name)}`} · ${esc(L.mode_ru)}</h3>`);
  // passengers / tour / prospect extras (dossier kinds other than a plain cargo link)
  const px = L.passengers;
  if (px) parts.push(loc("<section><h4>Пассажиры: {0}</h4>{1}</section>", esc(px.type_ru), kv([
    [loc("Остановок"), `${px.stops}${px.stops_per_town ? loc(" <span class=\"tt-muted\">({0} в каждом городе)</span>", px.stops_per_town) : ""}`],
    [loc("Интервал"), `${fmtSec(px.headway_s)}${px.headway_plan_s ? loc(" <span class=\"tt-muted\">(в плане {0} с)</span>", fmtInt(px.headway_plan_s)) : ""}`],
    [loc("Пассажиров в год"), `${fmtInt(px.passengers_per_year)}${px.demand_per_year ? loc(" <span class=\"tt-muted\">(спрос {0})</span>", fmtInt(px.demand_per_year)) : ""}`],
    [loc("Провозная способность"), loc("{0} пасс./год", fmtInt(px.capacity_per_year))],
    [loc("Билет / выручка"), loc("{0} за пассажира · {1}/год", fmtMoney(px.ticket), fmtMoney(px.ticket_income_per_year))],
    px.coverage ? [loc("Охват города"), esc(px.coverage)] : null,
    px.avg_trip_m ? [loc("Средняя поездка"), loc("{0} м по прямой", fmtInt(px.avg_trip_m))] : null,
    px.rule ? [loc("Рабочие"), `${px.distinct_workers_2y != null ? loc("разных за 2 года ~{0} из {1} нужных; ", fmtInt(px.distinct_workers_2y), px.min_workers) : ""}${px.boost ? loc('<span class="pos">бонус будет</span>') : loc('<span class="neg">бонуса не будет</span>')}<div class="tt-muted">${esc(px.rule)}</div>`] : null,
  ])));
  const tour = L.tour;
  if (tour) parts.push(loc("<section><h4>{0}: {1}, остановок {2}</h4>\n    {3}\n    {4}</section>", esc(tour.label), esc(tour.topology_ru), tour.stops.length, tbl(["№", loc("Остановка"), loc("Выгрузить, ед./год"), loc("Загрузить, ед./год"), loc("Настройка")], tour.stops.map((v) => [v.n, esc(v.name),
      esc(v.unload_ru || "—"), esc(v.load_ru || "—"), v.load_mode ? loc("загружать, если есть") : "—"]), "small"), tbl([loc("Участок"), loc("Длина"), loc("Везём"), loc("Путь"), loc("Время")], tour.segments.map((g) => [`${g.n}. ${esc(g.from)} → ${esc(g.to)}`, fmtKm(g.length_m),
      esc(g.on_board_ru), g.shared_with ? loc("общий с участком {0}", g.shared_with) : g.prior_line ? loc("есть ({0})", esc(g.prior_line)) : g.free ? esc(g.free_by || loc("есть")) : fmtMoney(g.cost),
      loc("{0} с", fmtInt(g.time_s))]), "small")));
  // ---- route
  const way = r.way || {};
  parts.push(loc("<div class=\"ds-grid\"><section><h4>Маршрут</h4>{0}</section>\n  <section><h4>Стоимость трассы</h4>{1}</section></div>", kv([
    [loc("Откуда"), esc(L.from.name) + loc(" <span class=\"tt-muted\">({0}, {1}; высота {2} м)</span>", fmtInt(L.from.x), fmtInt(L.from.y), fmt1(L.from.z, 0))],
    [loc("Куда"), esc(L.to.name) + loc(" <span class=\"tt-muted\">({0}, {1}; высота {2} м)</span>", fmtInt(L.to.x), fmtInt(L.to.y), fmt1(L.to.z, 0))],
    [loc("Длина трассы"), fmtKm(r.length_m) + (r.free ? loc(" <span class=\"pos\">уже построена ({0})</span>", esc(r.free_by)) : "")],
    [loc("По прямой / объезд"), `${fmtKm(r.straight_m)} / ×${fmt1(r.detour, 2)}`],
    [loc("Набор / спуск высоты"), loc("{0} м / {1} м", fmtInt(r.elevation_gain_m), fmtInt(r.elevation_loss_m))],
    [loc("Уклон рельефа (макс.)"), fmtPct(r.max_terrain_grade)],
    t.ruling_grade != null ? [loc("Рекомендуемый уклон пути"), `≤ ${fmtPct(t.ruling_grade)} <span class="tt-muted">— ${esc(t.ruling_grade_reason || "")}</span>`] : null,
    r.way ? [loc("Тип пути"), loc("{0} · {1} км/ч · {2}/м · содержание {3}/м/год · R ≥ {4} м", esc(way.name), way.speed_kmh, fmtInt(way.cost_per_m), fmtInt(way.maintenance_per_m), way.min_radius)] : null,
    [loc("Мосты / тоннели"), loc("{0} м / {1} м", fmtInt(r.bridges_m), fmtInt(r.tunnels_m)) + (r.bridge_type ? ` <span class="tt-muted">(${esc(r.bridge_type.name)}${r.bridge_type.speed_kmh ? ", " + r.bridge_type.speed_kmh + loc(" км/ч") : ""})</span>` : "")],
    [loc("Источник трассы"), `<span class="tt-muted">${esc(r.source)}</span>`],
  ]), kv([
    [loc("Путь / дорога"), fmtMoney(r.cost.track)], [loc("Мосты"), fmtMoney(r.cost.bridges)], [loc("Тоннели"), fmtMoney(r.cost.tunnels)],
    [loc("Земляные работы"), fmtMoney(r.cost.earthworks)], [loc("Надбавки (высота мостов, уклоны, снос)"), fmtMoney(r.cost.surcharges)],
    r.demolition ? [loc('<span class="neg">в т.ч. снос городских зданий</span>'), loc("<span class=\"neg\">{0}</span> <span class=\"tt-muted\">(~{1} зд., {2} м трассы)</span>", fmtMoney(r.demolition.cost), fmtBld(r.demolition.buildings), fmtInt(r.demolition.m))] : null,
    [loc("<b>Итого строительство</b>"), `<b>${fmtMoney(r.cost.total)}</b>` + (r.free ? loc(' <span class="pos">(не платим)</span>') : "")],
    [loc("Содержание в год"), loc("{0} <span class=\"tt-muted\">(путь {1}, сооружения {2})</span>", fmtMoney(r.maintenance.total), fmtMoney(r.maintenance.track), fmtMoney(r.maintenance.structures))],
  ])));
  // ---- demolition of town buildings along the route
  const rd = r.demolition;
  if (rd && !r.free) parts.push(loc("<section class=\"ds-demo\"><h4>Снос городских зданий по трассе</h4>\n    <div><b class=\"neg\">{0}</b></div>\n    <ul>{1}</ul>\n    <div>{2}</div>\n    <div class=\"tt-muted\">Места — красная штриховка на карте (метка «снос»). {3}</div></section>", esc(rd.total_ru || rd.text_ru || ""), (rd.segments_ru || []).map((x) => `<li>${esc(x)}</li>`).join(""), repHTML(rd.towns), esc(DEMO_RULE)));
  // ---- chart
  parts.push(loc("<section><h4>Профиль и скорость</h4><div class=\"ds-chart-wrap\"><canvas class=\"ds-chart\" data-leg=\"{0}\"></canvas>\n    <div class=\"ds-chart-tip\" hidden></div></div>\n    <div class=\"ds-legend\"><span><i style=\"background:#78766e\"></i>рельеф</span><span><i style=\"background:#e8e6dc\"></i>путь (уклон ≤ рекоменд.)</span>\n    <span><i style=\"background:#4fb8f0\"></i>мост</span><span><i style=\"background:#b07a4a\"></i>тоннель</span>\n    <span><i style=\"background:#eda100\"></i>скорость гружёным →</span><span><i style=\"background:#3987e5\"></i>порожним ←</span>\n    <span><i style=\"background:#898781\"></i>ограничение</span><span><i style=\"background:#e66767\"></i>встаёт</span></div></section>", i));
  // ---- structures
  if (r.structures && r.structures.length) {
    parts.push(loc("<section><h4>Сооружения ({0})</h4>{1}</section>", r.structures.length, tbl([loc("Тип"), loc("Км"), loc("Длина"), loc("Где"), loc("Огр."), loc("Цена")],
      r.structures.map((s) => [esc(s.type_ru) + `<div class="tt-muted">${esc(s.name)}</div>`, `${fmt1(s.at_m / 1000)}–${fmt1(s.to_m / 1000)}`,
        loc("{0} м", fmtInt(s.length_m)), `${esc(s.place)}<div class="tt-muted">(${fmtInt(s.x)}, ${fmtInt(s.y)})</div>`,
        s.speed_limit_kmh ? s.speed_limit_kmh + loc(" км/ч") : "—", fmtMoney(s.cost)]))));
  }
  // ---- stations & depots
  const st = (L.stations || []).map((s) => [esc(s.role), esc(s.building) + (s.platform_m ? loc("<div class=\"tt-muted\">{0}×{1} м, {2} сегм. по 40 м</div>", s.terminals, fmtInt(s.platform_m), s.segments) : loc("<div class=\"tt-muted\">терминалов: {0}</div>", s.terminals)),
    loc("{0}<div class=\"tt-muted\">({1}, {2}), {3} м от центра; охват {4} м{5}</div>", esc(s.endpoint), fmtInt(s.x), fmtInt(s.y), fmtInt(s.dist_to_endpoint_m), fmtInt(s.catchment_m), s.site_reason ? "<br>" + esc(s.site_reason) : ""),
    s.free_by ? loc("<span class=\"pos\">есть ({0})</span>", esc(s.free_by)) : fmtMoney(s.cost) + (s.demolition ? loc("<div class=\"neg\">в т.ч. снос ~{0} зд. {1}</div><div class=\"tt-muted\">{2}</div>", fmtBld(s.demolition.buildings), fmtMoney(s.demolition.cost), repHTML(s.demolition.towns)) : ""), fmtMoney(s.maintenance)]);
  const dp = (L.depots || []).map((x) => [loc("депо / ТО"), loc("{0} ×{1}<div class=\"tt-muted\">пул {2}, радиус {3} м</div>", esc(x.what), x.count, x.pool, fmtInt(x.radius_m)),
    esc(x.where), x.free ? loc('<span class="pos">есть</span>') : fmtMoney(x.cost) + (x.demolition ? loc("<div class=\"neg\">в т.ч. снос ~{0} зд. {1}</div>", fmtBld(x.demolition.buildings), fmtMoney(x.demolition.cost)) : ""), fmtMoney(x.maintenance)]);
  parts.push(loc("<section><h4>Станции и депо</h4>{0}</section>", tbl([loc("Роль"), loc("Здание"), loc("Где"), loc("Цена"), loc("Содерж./год")], [...st, ...dp])));
  // ---- vehicles & consist
  const vrows = (cons.vehicles || []).map((v) => [`${esc(v.name)}<div class="tt-muted">${esc(v.model)}${v.mod ? " · " + esc(v.mod) : ""}</div>`,
    `${v.per_consist} / ${v.count}`, v.capacity || "—", loc("{0} км/ч", v.top_speed_kmh),
    v.power_kw ? loc("{0} кВт / {1} кН", fmtInt(v.power_kw), fmt1(v.tractive_effort_kn, 0)) : "—",
    loc("{0} т", fmt1(v.weight_empty_t)), fmtMoney(v.price), fmtMoney(v.running_cost), loc("{0} л.", fmt1(v.lifespan_years, 0)),
    `${v.year_from}–${v.year_to || "…"}` + (v.available ? "" : loc(' <span class="neg">недоступен</span>'))]);
  const perf = (rows) => (rows || []).map((x) => x.ok ? loc("{0} км/ч, разгон {1} с / {2} м", fmt1(x.max_kmh, 0), fmtInt(x.time_s), fmtInt(x.dist_m)) : loc('<span class="neg">не тянет</span>'));
  const pl = perf(cons.performance_loaded), pe = perf(cons.performance_empty);
  const mw = cons.max_wagons ? Object.entries(cons.max_wagons).map(([g, n]) => `${fmtPct(+g, 0)}: ${n}`).join(" · ") : null;
  parts.push(loc("<section><h4>Транспорт: {0} × {1}</h4>\n    {2}\n    <div class=\"ds-grid\">{3}{4}</div></section>", esc(cons.label), cons.n_consists, tbl([loc("Модель"), loc("В составе / всего"), loc("Вмест."), loc("Скорость"), loc("Мощн. / тяга"), loc("Масса"), loc("Цена"), loc("Расход/год"), loc("Срок"), loc("Годы")], vrows, "small"), kv([
      [loc("Длина состава"), loc("{0} м", fmtInt(cons.length_m))], [loc("Вместимость"), loc("{0} ед. ({1} т)", cons.capacity, fmt1(cons.payload_t))],
      [loc("Масса порожним / гружёным"), loc("{0} / {1} т", fmt1(cons.mass_empty_t, 0), fmt1(cons.mass_loaded_t, 0))],
      [loc("Мощность / сила тяги"), loc("{0} кВт / {1} кН", fmtInt(cons.power_kw), fmt1(cons.tractive_effort_kn, 0))],
      [loc("кВт на тонну (гружёный / порожний)"), `${fmt1(cons.kw_per_t_loaded, 2)} / ${fmt1(cons.kw_per_t_empty, 2)}`],
      [loc("Макс. скорость"), loc("{0} км/ч", cons.top_speed_kmh) + (cons.top_speed_note ? `<div class="tt-muted">${esc(cons.top_speed_note)}</div>` : "")],
    ]), kv([
      [loc("Трогается на уклоне до"), loc("{0} гружёным · {1} порожним", fmtPct(cons.max_start_grade_loaded), fmtPct(cons.max_start_grade_empty))],
      [loc("Ровный участок"), loc("{0}<div class=\"tt-muted\">порожним: {1}</div>", pl[0] || "—", pe[0] || "—")],
      [loc("Уклон 3,75 %"), loc("{0}<div class=\"tt-muted\">порожним: {1}</div>", pl[1] || "—", pe[1] || "—")],
      [loc("Уклон 7,5 %"), loc("{0}<div class=\"tt-muted\">порожним: {1}</div>", pl[2] || "—", pe[2] || "—")],
      mw ? [loc("Макс. вагонов (≥40 % скорости) по уклону"), mw] : null,
      [loc("Торможение"), loc("{0} м/с²", fmt1(cons.brake_decel))],
    ])));
  // ---- timing
  const lr = t.loaded, er = t.empty;
  const bn = (b) => b ? loc("км {0}–{1}: {2}, −{3} с, мин. {4} км/ч", fmt1(b.from_m / 1000), fmt1(b.to_m / 1000), esc(b.cause_ru), fmtInt(b.lost_s), fmt1(b.min_kmh, 0)) : "—";
  const runRows = [
    [loc("Время в пути"), fmtSec(lr.time_s), fmtSec(er.time_s)],
    [loc("Средняя / макс. скорость"), loc("{0} / {1} км/ч", fmt1(lr.avg_kmh, 1), fmt1(lr.max_kmh, 0)), loc("{0} / {1} км/ч", fmt1(er.avg_kmh, 1), fmt1(er.max_kmh, 0))],
    [loc("Без потерь (всё на макс. скорости)"), loc("{0} с", fmtInt(lr.ideal_time_s)), loc("{0} с", fmtInt(er.ideal_time_s))],
    [loc("Потери на разгон"), loc("{0} с", fmtInt(lr.lost_accel_s)), loc("{0} с", fmtInt(er.lost_accel_s))],
    [loc("Потери на торможение"), loc("{0} с", fmtInt(lr.lost_brake_s)), loc("{0} с", fmtInt(er.lost_brake_s))],
    [loc("Потери на подъёмах"), loc("{0} с", fmtInt(lr.lost_grade_s)), loc("{0} с", fmtInt(er.lost_grade_s))],
    [loc("Потери на ограничениях (кривые, мосты)"), loc("{0} с", fmtInt(lr.lost_limits_s)), loc("{0} с", fmtInt(er.lost_limits_s))],
    [loc("Время разгона"), loc("{0} с", fmtInt(lr.accel_time_s)), loc("{0} с", fmtInt(er.accel_time_s))],
    [loc("Масса"), loc("{0} т", fmt1(lr.mass_t, 0)), loc("{0} т", fmt1(er.mass_t, 0))],
    [loc("Работа тяги"), loc("{0} МДж", fmtInt(lr.energy_mj)), loc("{0} МДж", fmtInt(er.energy_mj))],
    [loc("Узкое место"), bn(lr.bottleneck), bn(er.bottleneck)],
    [loc("Встаёт на подъёме"), lr.stalled ? loc("<span class=\"neg\">да, км {0}</span>", (lr.stall_at_m || []).map((x) => fmt1(x / 1000)).join(", ")) : loc("нет"),
      er.stalled ? loc("<span class=\"neg\">да</span>") : loc("нет")],
  ];
  const tv = t.terrain_variant, ew = t.earthworks;
  const runHead = t.one_way ? ["", L.pax ? loc("Рабочий круг →") : loc("Рабочий круг (с грузом участков) →"), loc("Без груза (для сравнения)")]
    : L.pax ? ["", loc("Туда →"), loc("Обратно ←")] : ["", loc("Гружёным →"), loc("Порожним ←")];
  parts.push(loc("<section><h4>Движение: разгон, торможение, уклоны, ограничения</h4>{0}\n    <div class=\"ds-grid\">{1}{2}</div>\n    {3}\n  </section>", tbl(runHead, runRows, "run"), kv([
      [loc("Погрузка / выгрузка"), loc("{0} с / {1} с", fmtInt(t.load_s), fmtInt(t.unload_s))],
      [loc("Модель погрузки"), `<span class="tt-muted">${esc(t.loading_model)}</span>`],
      [loc("Остановки, задержки, манёвры"), loc("{0} ост. · {1} с + {2} с", t.stops, fmtInt(t.stop_delays_s), fmtInt(t.manoeuvre_s))],
      [t.one_way ? loc("<b>Полный круг</b>") : loc("<b>Круг (туда-обратно)</b>"), `<b>${fmtSec(t.round_trip_s)}</b>`],
      [loc("Рейсов в год на состав"), fmt1(t.trips_per_year, 3)],
      [loc("Интервал между составами"), fmtSec(t.headway_s)],
      t.wait_for_cargo_s > 0 ? [loc("Ожидание груза на погрузке"), fmtSec(t.wait_for_cargo_s)] : null,
      [loc("Упрощённая модель плана"), loc("{0} с <span class=\"tt-muted\">(подробный расчёт {1}{2})</span>", fmtInt(t.planner_round_trip_s), t.planner_diff >= 0 ? "+" : "", fmtPct(t.planner_diff, 0))],
    ]), tv || ew ? kv([
      tv ? [loc("Если путь по рельефу (до ") + fmtPct(tv.max_grade, 0) + ")",
        loc("гружёным {0} с, порожним {1} с", fmtInt(tv.loaded_s), fmtInt(tv.empty_s)) +
        (tv.stalled ? loc(" <span class=\"neg\">— встаёт на км {0}</span>", (tv.stall_at_m_loaded || []).map((x) => fmt1(x / 1000)).join(", ")) : "")] : null,
      ew ? [loc("Насыпи / выемки для уклона ≤ ") + fmtPct(ew.ruling_grade), loc("до {0} м / {1} м, ~{2} м³", fmt1(ew.max_fill_m, 0), fmt1(ew.max_cut_m, 0), fmtInt(ew.volume_m3))] : null,
    ]) : "", (t.grade_variants || []).length ? loc("<div class=\"pc-sub\">Варианты уклона пути: время ↔ насыпи</div>{0}", tbl([loc("Уклон ≤"), loc("Гружёным"), loc("Порожним"), loc("Круг"), loc("Насыпь / выемка"), ""],
      t.grade_variants.map((v) => [fmtPct(v.grade), loc("{0} с", fmtInt(v.loaded_s)), loc("{0} с", fmtInt(v.empty_s)), loc("{0} с", fmtInt(v.round_trip_s)),
        loc("{0} / {1} м", fmt1(v.max_fill_m, 0), fmt1(v.max_cut_m, 0)), (v.recommended ? loc('<span class="pos">рекомендуется</span>') : "") + (v.stalled ? loc(' <span class="neg">встаёт</span>') : "")]), "small")) : ""));
  // ---- capacity & money
  const rl = cap.rail_layout, dl = L.delivery;
  parts.push(loc("<div class=\"ds-grid\"><section><h4>Провоз</h4>{0}</section>\n  <section><h4>Деньги</h4>{1}</section></div>", kv([
    [loc("За рейс"), loc("{0} ед.", cap.per_trip)], [loc("Один состав в год"), loc("{0} ед.", fmt1(cap.per_consist_year))],
    [loc("Весь парк в год"), loc("{0} ед. ({1} шт.)", fmt1(cap.fleet_year), cap.consists_planned)],
    [loc("Объём груза"), cap.volume != null ? loc("{0} ед./год", fmt1(cap.volume)) : "—"],
    [loc("Перевезём"), loc("{0} ед./год", fmt1(cap.moved))],
    [loc("Загрузка парка"), `<span class="${cap.load_factor < 0.6 ? "neg" : ""}">${fmtPct(cap.load_factor, 0)}</span>` + (cap.consists_needed ? loc(" <span class=\"tt-muted\">(нужно: {0})</span>", cap.consists_needed) : "")],
    [loc("Терминалов на станции"), loc("{0} <span class=\"tt-muted\">(стоянка {1} с)</span>", cap.terminals, fmtInt(cap.terminal_dwell_s))],
    rl ? [loc("Однопутка"), loc("{0}{1}; загрузка {2}", esc(rl.kind), rl.loops ? loc(", разъездов {0} через ~{1}", rl.loops, fmtKm(rl.spacing_m || 0)) : "", fmtPct(rl.utilization, 0)) + (rl.reason ? ` <span class="neg">${esc(rl.reason)}</span>` : "")] : null,
    dl ? [loc("Срок доставки"), loc("{0} с из {1} с ", fmtInt(dl.transit_s), fmtInt(dl.time_to_deliver_s)) +
      (dl.ok ? loc('<span class="pos">успевает</span>') : loc('<span class="neg">опоздание</span>')) + `<div class="tt-muted">${esc(dl.note)}</div>`] : null,
  ]), kv([
    [loc("Доход за единицу груза"), loc("{0} <span class=\"tt-muted\">(по прямой {1})</span>", fmtMoney(m.income_per_unit), fmtKm(m.chord_m))],
    [loc("Доход за рейс"), fmtMoney(m.income_per_trip)],
    [loc("<b>Доход в год</b>"), `<b class="pos">${fmtMoney(m.income_per_year)}</b>`],
    [loc("Эксплуатация техники в год"), fmtMoney(m.per_year.running)],
    [loc("Содержание трассы в год"), fmtMoney(m.per_year.route_maintenance)],
    [loc("Содержание станций в год"), fmtMoney(m.per_year.stations_maintenance)],
    [loc("Содержание депо в год"), fmtMoney(m.per_year.depots_maintenance)],
    [loc("<b>Прибыль в год</b>"), `<b class="${m.profit_per_year >= 0 ? "pos" : "neg"}">${fmtMoney(m.profit_per_year)}</b>` + (m.profit_per_trip != null ? loc(" <span class=\"tt-muted\">({0} за рейс)</span>", fmtMoney(m.profit_per_trip)) : "")],
    [loc("Вложения: трасса / станции / депо / техника"), `${fmtMoney(m.capex.route)} / ${fmtMoney(m.capex.stations)} / ${fmtMoney(m.capex.depots)} / ${fmtMoney(m.capex.vehicles)}`],
    [loc("<b>Вложения всего</b>"), `<b>${fmtMoney(m.capex.total)}</b>` + (m.capex.passing_loops_extra ? loc(" <span class=\"tt-muted\">+ разъезды {0}</span>", fmtMoney(m.capex.passing_loops_extra)) : "")],
    [loc("Окупаемость / NPV ") + m.npv_years + loc(" лет"), `${fmtYears(m.payback_years)} / ${fmtMoney(m.npv)}`],
    m.plan && m.plan.income_per_year ? [loc("В плане (для сравнения)"), loc("стоимость {0}, доход {1}/год, расходы {2}/год", fmtMoney(m.plan.cost), fmtMoney(m.plan.income_per_year), fmtMoney(m.plan.running_cost_per_year))] : null,
  ])));
  return parts.join("");
}

function renderDossier() {
  const p = curProject(), body = $("ds-body");
  if (!p) { closeDossier(); return; }
  $("ds-id").textContent = p.id;
  $("ds-title").textContent = p.build.title || "";
  const d = dossierFor(p);
  if (!d || d.error) {
    body.innerHTML = `<div class="pc-empty">${d ? loc("Ошибка разбора: ") + esc(d.error) : DS.state === "loading" ? loc("Загрузка…") : loc("Для этой линии нет подробного разбора.")}</div>`;
    DS.charts = [];
    return;
  }
  const legs = (d.legs || []).filter((l) => !l.error);
  const checks = new Set((store.get(LS_DS_CHECKS, {}) || {})[p.sig] || []);
  const ins = (d.instructions || []).map((s, i) => {
    const m = String(s).match(/^(\d+)\.\s(\s*)(.*)$/);
    const sub = !!(m && m[2].length), text = m ? m[3] : s;
    return `<li class="${sub ? "sub" : ""}${checks.has(i) ? " checked" : ""}"><label><input type="checkbox" data-chk="${i}" ${checks.has(i) ? "checked" : ""}>
      <span class="n">${m ? m[1] : i + 1}.</span><span class="tx">${esc(text)}</span></label></li>`;
  }).join("");
  const T = d.totals || {};
  const stat = (k, v, cls = "") => `<div class="stat"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`;
  body.innerHTML = loc("\n    {0}\n    {1}\n    <div class=\"ds-tiles\">\n      {2}{3}\n      {4}{5}\n      {6}{7}\n    </div>\n    {8}\n    <section class=\"ds-ins\"><h4>Что строить — по шагам <span class=\"tt-muted\" id=\"ds-chk-progress\">{9} из {10}</span></h4><ol>{11}</ol></section>\n    {12}\n    {13}\n    {14}\n    {15}\n    <details class=\"ds-assume\"><summary>Допущения модели</summary><ul>{16}</ul>\n      <div class=\"tt-muted\">Разбор построен {17}</div></details>", dossierStale(p, d) ? loc("<div class=\"ds-stale\">Разбор построен для другой версии плана ({0}) — перестройте: <code>python -m tf3planner.dossier</code>.</div>", esc((DS.meta && DS.meta.plan_generated) || "")) : "", (d.warnings || []).length ? loc("<div class=\"ds-warn\"><b>Внимание</b><ul>{0}</ul></div>", d.warnings.map((w) => `<li>${esc(w)}</li>`).join("")) : "", stat(loc("Вложения"), fmtMoney(T.capex)), stat(loc("Доход/год"), fmtMoney(T.income_per_year), "pos"), stat(loc("Расходы/год"), fmtMoney(T.costs_per_year)), stat(loc("Прибыль/год"), fmtMoney(T.profit_per_year), T.profit_per_year >= 0 ? "pos" : "neg"), stat(loc("Окупаемость"), fmtYears(T.payback_years)), stat(loc("Груз"), esc(d.cargo_info ? d.cargo_info.name : d.cargo || "—")), d.demolition ? loc("<section class=\"ds-demo\"><h4>Снос городских зданий (весь проект)</h4>{0}\n      {1}</section>", demoBlockHTML(p.build, d), d.demolition.rule_ru ? `<div class="tt-muted">${esc(d.demolition.rule_ru)}</div>` : "") : "", checks.size, (d.instructions || []).length, ins, d.prospect ? loc("<section><h4>Разведка ресурса</h4>{0}</section>", kv([
      [loc("Ресурс / город"), `${esc(d.prospect.resource || "—")} · ${esc(d.prospect.town || "—")}`],
      [loc("Нужен ранг"), loc("≥ {0} <span class=\"tt-muted\">(разрешение разовое, после применения ранга)</span>", d.prospect.rank ?? "—")],
      [loc("Успех / ожидание"), loc("~{0} % · ~{1} мес.", fmtInt(d.prospect.success), fmt1(d.prospect.months))],
      [loc("Ожидается"), loc("{0} ~{1} ед./год", esc(d.prospect.expected || "—"), fmtInt(d.prospect.expected_volume))],
      d.prospect.first_line ? [loc("Первая линия"), loc("→ {0}, {1} ед./год, доход {2}/год", esc(d.prospect.first_line.to), fmtInt(d.prospect.first_line.volume), fmtMoney(d.prospect.first_line.income))] : null,
      [loc("Место"), `<span class="tt-muted">${esc(d.prospect.estimate_note || "")}</span>`],
    ])) : "", (d.warehouses || []).length ? loc("<section><h4>Склады</h4>{0}</section>", tbl([loc("Склад"), loc("Узел / где"), loc("Модулей"), loc("Вместимость / буфер"), loc("Цена"), loc("Содерж./год")],
      d.warehouses.map((w) => [esc(w.name), esc(w.hub) + (w.site_reason ? `<div class="tt-muted">(${fmtInt(w.x)}, ${fmtInt(w.y)}) ${esc(w.site_reason)}</div>` : ""),
        w.modules, `${w.capacity_units != null ? fmtInt(w.capacity_units) + " / " : ""}${fmtInt(w.buffer_units)}` + (w.rule ? `<div class="tt-muted">${esc(w.rule)}</div>` : ""),
        fmtMoney(w.cost) + (w.demolition ? loc("<div class=\"neg\">в т.ч. снос ~{0} зд. {1}</div>", fmtBld(w.demolition.buildings), fmtMoney(w.demolition.cost)) : ""), fmtMoney(w.maintenance)]))) : "", legs.map((L, i) => legHTML(L, i, legs.length > 1)).join(""), (d.timeline || []).length ? loc("<section><h4>Дальше по линии</h4>{0}</section>", tbl([loc("Когда"), loc("Что"), loc("Стоимость")],
      d.timeline.map((s) => [`${s.year}${s.month ? "." + String(s.month).padStart(2, "0") : ""}`, esc(s.title), fmtMoney(s.cost)]), "small")) : "", (d.assumptions || []).map((a) => `<li>${esc(a)}</li>`).join(""), esc(String(d.generated || "").replace("T", " ")));
  DS.charts = [...body.querySelectorAll("canvas.ds-chart")].map((cv) => ({ cv, leg: legs[+cv.dataset.leg], hx: null }));
  for (const c of DS.charts) {
    c.cv.addEventListener("mousemove", (e) => { c.hx = e.offsetX; drawDossierChart(c); });
    c.cv.addEventListener("mouseleave", () => { c.hx = null; drawDossierChart(c); });
  }
  body.scrollTop = 0;
  drawDossierCharts();
}

function drawDossierCharts() { for (const c of DS.charts) drawDossierChart(c); }

// Two stacked panels over the same km axis: elevation (terrain + recommended track + structures)
// and speed (loaded run →, empty run ←, speed ceiling, top speed).
function drawDossierChart(c) {
  const cv = c.cv, L = c.leg, ch = L && L.chart;
  if (!ch || !ch.loaded || !ch.loaded.length) return;
  const w = cv.clientWidth, h = cv.clientHeight, r = window.devicePixelRatio || 1;
  if (!w || !h) return;
  cv.width = Math.round(w * r); cv.height = Math.round(h * r);
  const g = cv.getContext("2d");
  g.setTransform(r, 0, 0, r, 0, 0);
  g.clearRect(0, 0, w, h);
  const padL = 50, padR = 40, padT = 8, padB = 22, gap = 16;
  const plotW = w - padL - padR, h1 = Math.round((h - padT - padB - gap) * 0.45), h2 = h - padT - padB - gap - h1;
  const y1 = padT, y2 = padT + h1 + gap;
  const Lm = Math.max(1, ch.length_m);
  const X = (s) => padL + (s / Lm) * plotW;
  const terr = ch.terrain && ch.terrain.length ? ch.terrain : ch.track;
  const hs = [...terr.map((p) => p[1]), ...ch.track.map((p) => p[1])];
  let hmin = Math.min(...hs), hmax = Math.max(...hs);
  if (hmax - hmin < 10) { hmin -= 5; hmax += 5; }
  const pad = (hmax - hmin) * 0.08; hmin -= pad; hmax += pad;
  const Y1 = (z) => y1 + h1 - ((z - hmin) / (hmax - hmin)) * h1;
  const vmax = Math.max(ch.top_kmh || 10, ...ch.loaded.map((p) => p[1]), ...ch.empty.map((p) => p[1])) * 1.1;
  const Y2 = (v) => y2 + h2 - (v / vmax) * h2;
  g.font = "11px system-ui"; g.textBaseline = "middle";
  g.strokeStyle = "rgba(255,255,255,0.07)"; g.lineWidth = 1; g.fillStyle = "#898781";
  for (let k = 0; k <= 2; k++) {
    const z = hmin + (hmax - hmin) * k / 2, yy = Y1(z);
    g.beginPath(); g.moveTo(padL, yy); g.lineTo(padL + plotW, yy); g.stroke();
    g.textAlign = "right"; g.fillText(loc("{0} м", Math.round(z)), padL - 5, yy);
  }
  for (let k = 0; k <= 4; k++) {
    const v = vmax * k / 4, yy = Y2(v);
    g.beginPath(); g.moveTo(padL, yy); g.lineTo(padL + plotW, yy); g.stroke();
    g.textAlign = "right"; g.fillText(`${Math.round(v)}`, padL - 5, yy);
  }
  g.textAlign = "left"; g.fillText(loc("км/ч"), padL + plotW + 5, y2 + 6);
  const kmStep = Lm > 40000 ? 10000 : Lm > 15000 ? 5000 : Lm > 6000 ? 2000 : Lm > 2500 ? 1000 : 500;
  g.textAlign = "center";
  for (let s = 0; s <= Lm + 1; s += kmStep) {
    const xx = X(s);
    g.beginPath(); g.moveTo(xx, y1); g.lineTo(xx, y2 + h2); g.stroke();
    g.fillText(loc("{0} км", s / 1000), xx, y2 + h2 + 12);
  }
  if (ch.water_level != null && ch.water_level > hmin && ch.water_level < hmax) {
    g.strokeStyle = "rgba(79,184,240,0.35)"; g.setLineDash([4, 4]);
    g.beginPath(); g.moveTo(padL, Y1(ch.water_level)); g.lineTo(padL + plotW, Y1(ch.water_level)); g.stroke(); g.setLineDash([]);
  }
  g.beginPath(); g.moveTo(X(terr[0][0]), y1 + h1);
  for (const [s, z] of terr) g.lineTo(X(s), Y1(z));
  g.lineTo(X(terr.at(-1)[0]), y1 + h1); g.closePath();
  g.fillStyle = "rgba(120,118,110,0.45)"; g.fill();
  for (const st of L.route.structures || []) {
    g.fillStyle = st.type === "tunnel" ? "#b07a4a" : "#4fb8f0";
    g.fillRect(X(st.at_m), y1 + h1 - 4, Math.max(2, X(st.to_m) - X(st.at_m)), 4);
  }
  const line = (pts, xi, yi, Y) => { g.beginPath(); pts.forEach((p, k) => (k ? g.lineTo(X(p[xi]), Y(p[yi])) : g.moveTo(X(p[xi]), Y(p[yi])))); g.stroke(); };
  g.strokeStyle = "#e8e6dc"; g.lineWidth = 1.5; line(ch.track, 0, 1, Y1);
  g.setLineDash([2, 3]); g.strokeStyle = "rgba(255,255,255,0.25)"; g.lineWidth = 1;
  g.beginPath(); g.moveTo(padL, Y2(ch.top_kmh)); g.lineTo(padL + plotW, Y2(ch.top_kmh)); g.stroke(); g.setLineDash([]);
  g.strokeStyle = "#898781"; g.lineWidth = 1; line(ch.loaded, 0, 2, Y2);
  g.strokeStyle = "#3987e5"; g.lineWidth = 1.5; line(ch.empty, 0, 1, Y2);
  g.strokeStyle = "#eda100"; g.lineWidth = 2; line(ch.loaded, 0, 1, Y2);
  g.fillStyle = "#e66767";
  for (const s of (L.timing.loaded.stall_at_m || [])) { g.beginPath(); g.arc(X(s), Y2(0) - 3, 3, 0, 7); g.fill(); }
  const tip = cv.parentElement.querySelector(".ds-chart-tip");
  if (c.hx != null && c.hx >= padL && c.hx <= padL + plotW) {
    const s = ((c.hx - padL) / plotW) * Lm;
    const near = (arr) => arr.reduce((b, p) => (Math.abs(p[0] - s) < Math.abs(b[0] - s) ? p : b), arr[0]);
    const te = near(terr), tr = near(ch.track), lo = near(ch.loaded), em = near(ch.empty);
    g.strokeStyle = "rgba(255,255,255,0.5)"; g.lineWidth = 1;
    g.beginPath(); g.moveTo(c.hx, y1); g.lineTo(c.hx, y2 + h2); g.stroke();
    const st = (L.route.structures || []).find((x) => s >= x.at_m && s <= x.to_m);
    tip.hidden = false;
    tip.innerHTML = loc("<b>км {0}</b><br>рельеф {1} м · путь {2} м<br>\n      гружёным <b style=\"color:#eda100\">{3}</b> км/ч · порожним <b style=\"color:#3987e5\">{4}</b> км/ч<br>\n      ограничение {5} км/ч{6}", fmt1(s / 1000, 2), fmt1(te[1], 0), fmt1(tr[1], 0), fmt1(lo[1], 0), fmt1(em[1], 0), fmt1(lo[2], 0), st ? loc("<br>{0} {1} м", esc(st.type_ru), st.length_m) : "");
    const left = c.hx + 14 + tip.offsetWidth > w ? c.hx - 14 - tip.offsetWidth : c.hx + 14;
    tip.style.left = `${left}px`; tip.style.top = `${y1 + 4}px`;
  } else if (tip) tip.hidden = true;
}


init();

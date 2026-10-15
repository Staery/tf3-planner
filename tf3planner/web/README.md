# TF3 Planner — web map

Local single-page map of the exported game: terrain, towns, industries, supply chains,
computed routes and the optimizer plan on a year timeline. Offline: FastAPI + vanilla JS,
no CDNs, no build step.

## Start

```bash
.venv/Scripts/python -m pip install fastapi uvicorn   # once
.venv/Scripts/python -X utf8 -m tf3planner.web.server # http://127.0.0.1:8010
# options: --port 8010 --host 127.0.0.1
```

Stop with Ctrl+C. The first request to `/api/terrain.png` renders `data/terrain.png`
(~2 s); it is re-rendered automatically when `data/export.json` or `terrain.py` changes.
`data/plan.json` / `data/routes.json` are re-read on every page load — just refresh the page.

## Endpoints

| URL | content |
|---|---|
| `/` | UI (`static/index.html`, `app.js`, `style.css`) |
| `/api/game` | year, money, bounds, terrain image bounds, cargos, towns (capacities, needs), industries (type, level, inputs/outputs with Russian names, production stats), loans, ways, bridges (with `available` for the current year) |
| `/api/terrain.png` | hillshaded heightmap, 16 m/px, north up |
| `/api/plan` | `data/plan.json`, else `data/plan_example.json` (format: [PLAN_SCHEMA.md](PLAN_SCHEMA.md)) |
| `/api/routes` | `data/routes.json`, else `[]` |

## UI

- **Map**: drag to pan, wheel / double-click / `+ −` to zoom, `⤢` shows the whole map.
  Hover anything for a tooltip; bottom-right shows world coordinates.
- **Слои** (top bar):
  - *Города* — circles sized by total land-use capacity; tooltip: capacities, demanded cargo,
    nearest suppliers.
  - *Предприятия* — squares coloured by type (diamonds = on water); tooltip: level,
    inputs → outputs, production stats, nearest suppliers/consumers, outputs with no consumer on the map.
  - *Цепочки* — faint arrows from every producer to the 2 nearest consuming industries and
    the 2 nearest towns that demand each output. Hovering a town/industry highlights its
    arrows with cargo name and distance — quick way to spot short profitable links.
  - *Маршруты* — polylines from `data/routes.json` coloured by transport kind.
  - *План* — plan steps up to the selected year (current year bold with labels, past normal,
    future dashed/pale; untick *Будущие шаги* to hide them). Rail is red with white ties,
    road yellow, water blue dashed, air pink dotted.
- **Timeline** (right panel): slider / `◀ ▶` / arrow keys switch the year. The list shows the
  steps of that year (or all years) with cost, income, running cost, net, ROI and cash after.
  Click a step (in the list or on the map) to highlight it and zoom to it.
- **Деньги по годам**: cash at year end from `cashflow`; hover for income/costs/debt,
  click to jump to a year.
- **Проекты** (tab in the right panel): one project = one line — a `build_line` step plus every
  step that refers to it via `line` (докупка / замена транспорта / модернизация пути). Loans and
  notes are not projects. List is in plan order (build date); each row has a «сделано» checkbox and
  a «Сделать текущим» button (clicking the row works too; double-click a line on the map also picks
  its project). Filter box searches id / cities / cargo; «скрыть выполненные» hides done ones;
  progress bar shows «Сделано N из M». **Следующий проект →** (key `N`) picks the first not-done
  project in plan order.
- **Текущий проект** card (top of the panel, only one project can be current): title, mode, cargo,
  А → Б, stations/depot/consist from `details`, vehicles (name × count, price per unit and total),
  cost, income / running / profit per year, payback, total investment over the whole plan, and the
  later steps of this line by year (click one to jump the timeline). `×` clears the selection.
- **Фокус на проекте** (default ON when a project is picked; card button, top-bar checkbox, banner
  «Выйти из фокуса» or key `F`): the map shows only the current project's route (bold, coloured by
  transport kind) with labelled endpoints А / Б; terrain, towns and industries stay as faint context;
  other plan lines, routes and chains are hidden. Off = the normal view. «Показать на карте»
  zooms to the project's route.
- Persistence: current project → `localStorage` + `?project=L5` in the URL (`&focus=0` when focus
  is off); done marks, «скрыть выполненные», active tab and collapsed sections → `localStorage`.
  Done marks are keyed by kind + cargo + endpoints, so they survive a re-generated plan where line
  ids shift. Click «План» / «Деньги по годам» headers to collapse them and give the list more room.
- **Search**: type part of a town/industry name or type (e.g. «лесопилка»), Enter or click to fly there.
- URL parameters (handy for screenshots/links): `?year=1902&step=s7&layers=towns,industries,chains,plan&q=Дубна`,
  `?project=L5` (current project), `&focus=0|1`.

-- In-game blueprint ("чертёж") of planned projects: ghost guide lines and footprints drawn with
-- the mission zone/marker API (api.gui.mission.setZone / setMarkerAtPosition), purely visual:
-- prohibitBuilding = false, and NO command is ever sent. Parts the player has already built
-- (player-owned edges of the right kind running parallel within GHOST_RADIUS, player-owned
-- constructions of the right type inside the footprint) stop being drawn (or turn faint green).
--
-- Data: <userdata>/tf3_planner/plan_overlay.lua written by `python -m tf3planner.overlay`
-- (format documented in tf3planner/overlay.py), loaded with app.loadUserdata.
-- The GUI (gui/tf3_planner/tf3_planner_button.script.lua) calls open / close / tick / view and
-- the action functions; this module keeps the state and owns every zone/marker key it sets.

local C = ug_require "tf3_planner_1::/tf3_planner/common.lua"

local B = {}

-- Interface language of the window and the bar. The plan file says which one (``lang``, the
-- control-centre setting); before a plan is loaded the game's own language decides. Anything
-- unexpected keeps Russian, the language the texts are written in.
B.EN = {
	["app.loadUserdata недоступен"] = "app.loadUserdata is not available",
	["Чертежа ещё нет. Запустите программу TF3 Planner на компьютере (ярлык «TF3 Planner»), "] = "No blueprint yet. Start the TF3 Planner program on your PC (the \"TF3 Planner\" shortcut), ",
	["нажмите здесь «Экспорт и пересчёт» и дождитесь «План обновлён». "] = "press \"Export and recalculate\" here and wait for \"Plan updated\". ",
	["файл plan_overlay.lua пуст"] = "plan_overlay.lua is empty",
	["Трасса построена на %.0f %%"] = "Route built: %.0f %%",
	["общие участки %.0f %%"] = "shared sections %.0f %%",
	["станции"] = "stations",
	["остановки"] = "stops",
	["порты"] = "ports",
	["депо"] = "depots",
	["склады"] = "warehouses",
	["api.gui.mission недоступен"] = "api.gui.mission is not available",
	["зон %.0f (+%.0f), маркеров +%.0f, убрано %.0f%s"] = "zones %.0f (+%.0f), markers +%.0f, removed %.0f%s",
	[", ошибок "] = ", errors ",
	[" — достигнут лимит зон"] = " — zone limit reached",
	["Пересчёт плана"] = "Recalculating the plan",
	[": шаг %.0f/%.0f"] = ": step %.0f/%.0f",
	["План обновлён"] = "Plan updated",
	["Ошибка пересчёта"] = "Recalculation error",
	["Пересчёт отменён"] = "Recalculation cancelled",
	["Чертёж обновлён"] = "Blueprint updated",
	["экспорт недоступен: "] = "export not available: ",
	["экспорт не удался: "] = "export failed: ",
	["Экспорт записан"] = "Export written",
	["Экспорт записан частично (см. stdout)"] = "Export written partially (see stdout)",
	["проверка построенного: "] = "build check: ",
	["Меню строительства: дорожный транспорт (кнопка подсвечена). Или нажмите «Открыть в меню»."] = "Construction menu: road transport (the button is highlighted). Or press \"Open in menu\".",
	["Меню строительства: железная дорога (кнопка подсвечена). Или нажмите «Открыть в меню»."] = "Construction menu: railway (the button is highlighted). Or press \"Open in menu\".",
	["Меню строительства: водный транспорт (кнопка подсвечена). Или нажмите «Открыть в меню»."] = "Construction menu: water transport (the button is highlighted). Or press \"Open in menu\".",
	["Меню строительства: склады (кнопка подсвечена). Или нажмите «Открыть в меню»."] = "Construction menu: warehouses (the button is highlighted). Or press \"Open in menu\".",
	["Меню дорог (подсвечено): ведите дорогу по голубой линии от точки к точке; мост/тоннель — в параметрах инструмента."] = "Road menu (highlighted): lay the road along the blue line from point to point; bridge/tunnel — in the tool options.",
	["Меню путей (подсвечено): ведите путь по оранжевой линии от точки к точке; мост/тоннель — в параметрах инструмента."] = "Track menu (highlighted): lay the track along the orange line from point to point; bridge/tunnel — in the tool options.",
	["Финансы → вкладка «Займы» (подсвечены)."] = "Finances → the \"Loans\" tab (highlighted).",
	["Управление → купить транспорт (подсвечено); назначьте технику на линию этого проекта."] = "Management → buy vehicles (highlighted); assign them to this project's line.",
	["Управление → новая линия (подсвечено): добавьте станции по порядку из текста задания."] = "Management → new line (highlighted): add the stations in the order of the task text.",
	["Бульдозер (подсвечен): снесите здания под красной полосой."] = "Bulldozer (highlighted): demolish the buildings under the red strip.",
	["вдоль дороги %+.0f м"] = "along the road %+.0f m",
	["от дороги %+.0f м"] = "from the road %+.0f m",
	["для этой постройки кнопки пока нет"] = "no button for this construction yet",
	["постройка не найдена в игре: "] = "construction not found in the game: ",
	["узлы совпадают"] = "nodes coincide",
	["этот участок мод не строит"] = "the mod does not build this section",
	["тип дороги не найден в игре: "] = "road type not found in the game: ",
	["у участка нет линии"] = "the section has no line",
	["начало"] = "start",
	["конец"] = "end",
	["участок слишком короткий (концы совпадают с существующей дорогой)"] = "the section is too short (its ends coincide with an existing road)",
	["нет доступного типа тоннеля"] = "no tunnel type available",
	["нет доступного типа моста"] = "no bridge type available",
	[" для дорог в этом году"] = " for roads in this year",
	["рёбер %d, узлов %d, макс. уклон %.0f %%%s%s%s%s"] = "edges %d, nodes %d, max. grade %.0f %%%s%s%s%s",
	[", пересобраны соединения полос в узлах: %d"] = ", lane connections rebuilt at nodes: %d",
	[", примыкание к существующей дороге: %d"] = ", connections to an existing road: %d",
	[", подключено въездов станций: %d"] = ", station entrances connected: %d",
	[", не примыкает к существующей дороге: "] = ", not connected to an existing road: ",
	[" и "] = " and ",
	["у этого шага нет постройки"] = "this step has no construction",
	["ошибка мода: "] = "mod error: ",
	["соединения полос перенесены"] = "lane connections moved",
	["без пересборки соединений полос (проверьте стыки)"] = "without rebuilding lane connections (check the joints)",
	["без врезки в существующие дороги"] = "without cutting into existing roads",
	["концы не подключены к дорогам"] = "ends not connected to roads",
	["без узлов станций"] = "without station nodes",
	["без мостов"] = "without bridges",
	["игра не рассчитала: "] = "the game could not calculate: ",
	["узел на дороге: "] = "node on the road: ",
	["не рассчитан"] = "not calculated",
	["в 3 шага: узел на дороге, постройка, подъезд к дороге"] = "in 3 steps: node on the road, construction, access road",
	["в 2 шага: постройка, подъезд к дороге"] = "in 2 steps: construction, access road",
	["упрощено: "] = "simplified: ",
	["%s%s%s. Стоимость: $%s%s"] = "%s%s%s. Cost: $%s%s",
	["Дорогу можно строить"] = "The road can be built",
	["Место свободно"] = "The site is free",
	[", под снос %d дом(а) города (снос входит в стоимость, репутация города снизится)"] = ", %d town house(s) to demolish (included in the cost, the town's reputation will drop)",
	[" (сдвиг: "] = " (shift: ",
	[" (препятствий: %d)"] = " (obstacles: %d)",
	["Дорогу так не построить: "] = "The road cannot be built like this: ",
	["Место занято или недоступно: "] = "The site is occupied or unavailable: ",
	["Строю…"] = "Building…",
	["Постройка стоит, но подъезд не построен: не найден узел "] = "The construction stands, but the access road is not built: node not found: ",
	["дороги"] = "road",
	["выезда постройки"] = "construction exit",
	[" — проведите 6 м дороги от выезда до дороги вручную"] = " — lay 6 m of road from the exit to the road by hand",
	["подъезд не собран: "] = "access road not assembled: ",
	["Построено: постройка и подъезд к дороге"] = "Built: construction and access road",
	["Постройка стоит, но подъезд игра не приняла — проведите 6 м дороги от выезда до дороги вручную"] = "The construction stands, but the game rejected the access road — lay 6 m of road from the exit to the road by hand",
	["Игра не приняла постройку"] = "The game rejected the construction",
	["узел на дороге не собран"] = "node on the road not assembled",
	["Игра не поставила узел на дороге"] = "The game did not place the node on the road",
	["постройка не собрана"] = "construction not assembled",
	["Построено"] = "Built",
	["Игра не построила: "] = "The game did not build: ",
	["команда не отправлена: "] = "command not sent: ",
	["(кольцо)"] = "(loop)",
	["Нажмите «Получить» на карточке %s%s%s"] = "Press \"Get\" on the card %s%s%s",
	[" (%.0f мес., %.0f %%)"] = " (%.0f months, %.0f %%)",
	[" — такой суммы сейчас нет, это ближайшая"] = " — no such amount right now, this is the closest one",
	["ожидаю пересчёт…"] = "waiting for recalculation…",
	["т.%.0f — %s"] = "pt.%.0f — %s",
	["ожидаю пересчёт… (центр управления подхватит новый экспорт в авторежиме)"] = "waiting for recalculation… (the control center picks up the new export in auto mode)",
	["оранжевая полоса — новый ж/д путь; голубая — новая дорога"] = "orange strip — new railway track; blue — new road",
	["серый пунктир — едем по существующей дороге (строить не нужно)"] = "grey dashes — we use an existing road (nothing to build)",
	["тусклая синяя — участок, уже построенный для другой линии плана"] = "pale blue — a section already built for another plan line",
	["розовая — модернизация пути; бледно-зелёная — уже построено"] = "pink — track upgrade; pale green — already built",
	["белая широкая — мост; светло-серая — эстакада; фиолетовая — тоннель"] = "wide white — bridge; light grey — viaduct; purple — tunnel",
	["красная — снос зданий; прозрачная белая — коридор трассы"] = "red — demolition of buildings; transparent white — route corridor",
	["прямоугольники: жёлтый — станция, бледно-жёлтый — остановка, голубой — порт,"] = "rectangles: yellow — station, pale yellow — stop, light blue — port,",
	["  зелёный — депо, бирюзовый — техобслуживание, коричневый — склад"] = "  green — depot, turquoise — maintenance, brown — warehouse",
	["буква и номер у прямоугольника — что строить и номер шага: С станция, О остановка, П порт,"] = "letter and number by a rectangle — what to build and the step number: С station, О stop, П port,",
	["  Д депо, Т техобслуживание, СК склад (например «Д9» — шаг 9, депо)"] = "  Д depot, Т maintenance, СК warehouse (e.g. \"Д9\" — step 9, depot)",
	["короткая белая полоса от прямоугольника к дороге — въезд/подъезд (эта сторона к дороге)"] = "short white strip from a rectangle to the road — entrance/access (this side faces the road)",
	["белые кружки с цифрами — точки трассы"] = "white circles with numbers — route points",
	["станция"] = "station",
	["остановка"] = "stop",
	["порт"] = "port",
	["техобслуживание"] = "maintenance",
	["склад"] = "warehouse",
	["идёт"] = "running",
	["готово"] = "done",
	["ошибка"] = "error",
	["отменён"] = "cancelled",
	["ожидание"] = "waiting",
	["Обновление мода не загрузилось — нужен перезапуск игры"] = "The mod update did not load — restart the game",
	["Мод обновлён без перезапуска (v"] = "Mod updated without a restart (v",
	["Задание %.0f из %.0f · %s г.%s"] = "Task %.0f of %.0f · %s%s",
	[" · вложения %.1f млн $"] = " · investment %.1f M $",
	["Выполнено %.0f из %.0f  %s"] = "Done %.0f of %.0f  %s",
	["<< Задание"] = "<< Task",
	["Предыдущий проект плана"] = "Previous plan project",
	["Задание >>"] = "Task >>",
	["Следующий проект плана"] = "Next plan project",
	["Убрать с карты"] = "Remove from the map",
	["Показать на карте"] = "Show on the map",
	["Подсказки на карте и подсветка кнопок (окно можно закрыть — задание останется в верхней строке)"] = "Map hints and button highlighting (you can close the window — the task stays in the top bar)",
	["Сейчас:"] = "Now:",
	["Где: "] = "Where: ",
	["Камера к месту, место мигает"] = "Camera to the site, the site blinks",
	["Построить"] = "Build",
	["Проверить место и стоимость; постройка — после подтверждения"] = "Check the site and the cost; construction — after confirmation",
	["Открыть в меню"] = "Open in menu",
	["Не нашёл постройку в меню — откройте подсвеченную кнопку"] = "Could not find the construction in the menu — open the highlighted button",
	["Открыть меню строительства сразу на нужной постройке"] = "Open the construction menu right at the needed construction",
	["Отменить «готово»"] = "Undo \"done\"",
	["Готово"] = "Done",
	["Этот шаг игра проверить не может — отметьте вручную"] = "The game cannot check this step — mark it by hand",
	["Следующий шаг"] = "Next step",
	["Проверяю место…"] = "Checking the site…",
	["Подтвердить постройку"] = "Confirm construction",
	["Построить сейчас (деньги спишутся как при обычной постройке)"] = "Build now (money is charged as for a normal construction)",
	["Задание выполнено!  "] = "Task completed!  ",
	["Следующее задание"] = "Next task",
	["Скрыть настройки"] = "Hide settings",
	["Настройки и пересчёт"] = "Settings and recalculation",
	["Экспорт и пересчёт"] = "Export and recalculate",
	["Записать экспорт для планировщика; центр управления в авторежиме пересчитает план, чертёж обновится сам"] = "Write the export for the planner; the control center in auto mode recalculates the plan, the blueprint updates itself",
	["Обновить из файла"] = "Reload from file",
	["Перечитать plan_overlay.lua"] = "Re-read plan_overlay.lua",
	["<< год"] = "<< year",
	["год >>"] = "year >>",
	["только год "] = "only year ",
	["Показывать в списке проекты выбранного года или все"] = "List the projects of the selected year or all of them",
	["поиск по названию / линии (L12)"] = "search by name / line (L12)",
	["все проекты года"] = "all projects of the year",
	["коридор"] = "corridor",
	["существующие дороги"] = "existing roads",
	["построенное"] = "built",
	["Скрыть легенду"] = "Hide legend",
	["Легенда цветов"] = "Colour legend",
	["%s%s · проверка: %s · файл: %s"] = "%s%s · check: %s · file: %s",
	["TF3 Planner: задание"] = "TF3 Planner: task",
	["Задание"] = "Task",
	["TF3 Planner: окно чертежа — список шагов, легенда, выбор проекта (закрытие окна чертёж не убирает)"] = "TF3 Planner: blueprint window — list of steps, legend, project choice (closing the window keeps the blueprint)",
	["Чертёж: ошибка окна (stdout)"] = "Blueprint: window error (stdout)",
	["[x] на карте"] = "[x] on the map",
	["[ ] на карте"] = "[ ] on the map",
	["Показать / убрать подсказки чертежа на карте"] = "Show / hide the blueprint hints on the map",
	["Предыдущий проект"] = "Previous project",
	["Следующий проект"] = "Next project",
	["Предыдущий шаг"] = "Previous step",
	["%s %s · шаг %.0f/%.0f: %s%s%s"] = "%s %s · step %.0f/%.0f: %s%s%s",
	["(построен!)"] = "(built!)",
	[" (нажмите — камера к месту)"] = " (press — camera to the site)",
}

function B.lang()
	if B._lang then return B._lang end
	if B._gameLang == nil then
		local ok, code = pcall(function() return app.getUserProfile():getLanguage().code end)
		code = ok and type(code) == "string" and code:sub(1, 2) or "ru"
		B._gameLang = (code == "ru" or code == "uk" or code == "be") and "ru" or "en"
	end
	return B._gameLang
end

local function T(s)
	if B.lang() == "en" then return B.EN[s] or s end
	return s
end
B.T = T

B.CHUNK_M = 40            -- ghost chunk length, m
B.GHOST_RADIUS = 15       -- a built edge must pass within this distance of the chunk middle, m
B.GHOST_ANGLE = 30        -- ... and run within this angle of the chunk heading, degrees
B.ITEM_MARGIN = 25        -- construction centre within footprint half-diagonal + margin, m
B.GRID = 100              -- spatial grid cell of the player-edge index, m
B.LINE_W = 4              -- guide line width, m
B.SPAN_W = 9              -- bridge / tunnel highlight width, m
B.DEMOLITION_W = 12
B.ACCESS_W = 6            -- entrance stub width, m
B.RUN = 10                -- chunks per zone polygon
B.MAX_ZONES = 3000
B.MAX_YEAR_PROJECTS = 12
B.DONE_PCT = 95           -- route counts as built from this share of chunks
B.CHECK_BUDGET = 400      -- chunk checks per tick in the octree fallback
B.ROAD_OCTREE_BUDGET = 120 -- unbuilt new-road chunks looked up on the map per tick (any owner)
B.PREFIX = "tf3p:"
B.DIGIT_H = 26            -- world label digit height, m (waypoints: 0.7x)
B.HIGHLIGHT_S = 15        -- "Показать": seconds the item pulses
B.MOD_ID = "tf3_planner_1"
B.LABELS = false          -- world letters «Д9» next to footprints (the mission panel says what to build)
B.LINE_MATCH_M = 250      -- a planned station counts as served by a line stop within this distance, m

local COLORS = {
	new_rail = { 1.0, 0.5, 0.0, 0.85 },      -- orange
	new_road = { 0.0, 0.85, 1.0, 0.85 },     -- cyan
	shared = { 0.3, 0.45, 1.0, 0.45 },       -- dim blue
	existing = { 0.75, 0.75, 0.75, 0.35 },   -- grey dashes
	upgrade = { 1.0, 0.25, 0.8, 0.8 },       -- pink
	done = { 0.2, 0.9, 0.3, 0.3 },           -- faint green
	bridge = { 1.0, 1.0, 1.0, 0.9 },         -- white
	viaduct = { 0.85, 0.85, 0.85, 0.85 },    -- light grey
	tunnel = { 0.6, 0.25, 1.0, 0.85 },       -- violet
	corridor = { 1.0, 1.0, 1.0, 0.12 },
	demolition = { 1.0, 0.1, 0.1, 0.55 },    -- red
	station = { 1.0, 0.85, 0.0, 0.55 },      -- yellow
	stop = { 1.0, 1.0, 0.6, 0.55 },          -- pale yellow
	harbor = { 0.3, 0.7, 1.0, 0.55 },        -- light blue
	depot = { 0.1, 0.85, 0.3, 0.55 },        -- green
	maint = { 0.1, 0.8, 0.8, 0.55 },         -- teal
	warehouse = { 0.8, 0.45, 0.15, 0.6 },    -- brown
	item_done = { 0.2, 0.9, 0.3, 0.25 },
	waypoint = { 1.0, 1.0, 1.0, 0.8 },
	access = { 1.0, 1.0, 1.0, 0.75 },        -- white: entrance stub
	highlight = { 1.0, 1.0, 1.0, 0.35 },
}

B.LEGEND = {
	"оранжевая полоса — новый ж/д путь; голубая — новая дорога",
	"серый пунктир — едем по существующей дороге (строить не нужно)",
	"тусклая синяя — участок, уже построенный для другой линии плана",
	"розовая — модернизация пути; бледно-зелёная — уже построено",
	"белая широкая — мост; светло-серая — эстакада; фиолетовая — тоннель",
	"красная — снос зданий; прозрачная белая — коридор трассы",
	"прямоугольники: жёлтый — станция, бледно-жёлтый — остановка, голубой — порт,",
	"  зелёный — депо, бирюзовый — техобслуживание, коричневый — склад",
	"буква и номер у прямоугольника — что строить и номер шага: С станция, О остановка, П порт,",
	"  Д депо, Т техобслуживание, СК склад (например «Д9» — шаг 9, депо)",
	"короткая белая полоса от прямоугольника к дороге — въезд/подъезд (эта сторона к дороге)",
	"белые кружки с цифрами — точки трассы",
}

local KIND_RU = { station = "станция", stop = "остановка", harbor = "порт", depot = "депо", maint = "техобслуживание",
	warehouse = "склад" }

local function newState()
	return {
		data = nil, index = {}, loadedAt = nil, status = "", error = nil,
		filterYear = true, year = nil, search = "", pos = 1,
		showYearAll = false, showStations = true, showCorridor = false, showBuilt = true, showExisting = false,
		hidden = false,
		prep = {},              -- project id -> prepared chunks/items
		zones = {}, markers = {}, -- key -> true (what is currently set in the world)
		sig = nil, grid = nil, cons = nil, mode = nil, rr = 0,
		renderStats = "", hl = nil, restored = nil,
	}
end

local S = newState()
B._state = function() return S end

-- every zone/marker key ever set this session: clear() removes all of them, even if the
-- per-render bookkeeping lost one (e.g. a highlight re-added after a hide)
local EVER = { zones = {}, markers = {} }

--- seconds: os.time when the sandbox has it, else a counter advanced by tick()
local CLOCK = { t = 0, step = 3 }
local function clock()
	if os and os.time then
		local ok, t = pcall(os.time)
		if ok and type(t) == "number" then return t end
	end
	return CLOCK.t
end
B.clock = clock

---------------------------------------------------------------------------
-- small helpers

local function vec2(x, y) return api.type.Vec2f.new(x, y) end
local function vec3(x, y, z) return api.type.Vec3f.new(x, y, z) end
local function vec4(c) return api.type.Vec4f.new(c[1], c[2], c[3], c[4]) end

local function list(t)
	local r = {}
	if type(t) ~= "table" then return r end
	for i = 1, #t do r[i] = t[i] end
	return r
end

local function pairsXY(p)
	local pts = {}
	if type(p) ~= "table" then return pts end
	for i = 1, #p - 1, 2 do pts[#pts + 1] = { p[i], p[i + 1] } end
	return pts
end

local function angDiff(a, b)
	-- undirected heading difference in degrees (a way can be built in either direction)
	local d = math.abs(a - b) % math.pi
	if d > math.pi / 2 then d = math.pi - d end
	return math.deg(d)
end

local function segDist(px, py, ax, ay, bx, by)
	local dx, dy = bx - ax, by - ay
	local L2 = dx * dx + dy * dy
	local t = 0
	if L2 > 0 then t = math.max(0, math.min(1, ((px - ax) * dx + (py - ay) * dy) / L2)) end
	local qx, qy = ax + t * dx - px, ay + t * dy - py
	return math.sqrt(qx * qx + qy * qy)
end

local function gameYear()
	local ok, y = pcall(function() return api.engine.util.getYear() end)
	return ok and y or nil
end

local function player()
	local ok, p = pcall(function() return api.engine.util.getPlayer() end)
	return ok and p or nil
end

---------------------------------------------------------------------------
-- data

function B.load()
	local okApp = app and app.loadUserdata
	if not okApp then
		S.error = T("app.loadUserdata недоступен")
		return false
	end
	local ok, d = pcall(app.loadUserdata, "tf3_planner", "plan_overlay")
	if not ok or type(d) ~= "table" then
		-- players without the desktop app: explain what to do instead of a developer command
		S.error = T("Чертежа ещё нет. Запустите программу TF3 Planner на компьютере (ярлык «TF3 Planner»), ")
			.. T("нажмите здесь «Экспорт и пересчёт» и дождитесь «План обновлён». ")
			.. "(" .. tostring(ok and T("файл plan_overlay.lua пуст") or d) .. ")"
		return false
	end
	local cur = B.current()
	local curTitle = cur and cur.title or nil
	local curLine = cur and cur.line or nil
	B.clear()
	S.data = d
	B._lang = (d.lang == "en" or d.lang == "ru") and d.lang or nil
	-- step numbers and a pending build check belong to the old plan
	S.hudN, S.hudPinned, S.hudDone, S.buildCheck = nil, false, nil, nil
	S.index = list(d.index)
	S.prep = {}
	S.sig = nil
	S.error = nil
	S.loadedAt = d.generated
	local keep = cur and cur.id or B.restoreSelection()
	local e = keep and B.indexEntry(keep) or nil
	if e == nil and curTitle then
		-- the plan was recomputed: line/step ids may have changed, match the same project by title, then line
		for _, x in ipairs(S.index) do
			if x.title == curTitle then e = x break end
		end
		if e == nil and curLine then
			for _, x in ipairs(S.index) do
				if x.line == curLine then e = x break end
			end
		end
		if e then keep = e.id end
	end
	if e then
		S.year = e.year
		S.restored = keep
	else
		local first = B.firstForDate()
		keep = first and first.id or nil
		S.year = first and first.year or B.defaultYear()
	end
	B.refilter(keep)
	return true
end

function B.indexEntry(id)
	for _, e in ipairs(S.index) do
		if e.id == id then return e end
	end
	return nil
end

local function gameDate()
	local ok, d = pcall(function()
		local cd = api.engine.util.getCalendarDate(C.gameTime())
		return { year = cd.year, month = cd.month }
	end)
	if ok and d and d.year then return d end
	local y = gameYear()
	return y and { year = y, month = 1 } or nil
end

--- first project (plan order) dated at or after the current game date
function B.firstForDate()
	local d = gameDate()
	if #S.index == 0 then return nil end
	if not d then return S.index[1] end
	for _, e in ipairs(S.index) do
		if (e.year or 0) > d.year or ((e.year or 0) == d.year and (e.month or 1) >= (d.month or 1)) then return e end
	end
	return S.index[#S.index]
end

--- remember the selected project (per savegame GUI data, plus a userdata file as fallback)
function B.saveSelection()
	local cur = B.current()
	if not cur then return end
	local data = { project = cur.id, year = S.year, showStations = S.showStations, showBuilt = S.showBuilt,
		showCorridor = S.showCorridor, showExisting = S.showExisting, active = S.active and true or false,
		manual = S.manual }
	B.defer(function()
		pcall(function() api.gui.game.setGuiSaveData(B.MOD_ID, data) end)
		pcall(function() app.saveUserdata("tf3_planner", "blueprint_state", data) end)
	end)
end

function B.restoreSelection()
	local d = nil
	pcall(function()
		local g = api.gui.game.getGuiSaveData(B.MOD_ID)
		if type(g) == "table" and g.project then d = g end
	end)
	if d == nil then
		pcall(function()
			local u = app.loadUserdata("tf3_planner", "blueprint_state")
			if type(u) == "table" and u.project then d = u end
		end)
	end
	if d == nil then return nil end
	for _, k in ipairs({ "showStations", "showBuilt", "showCorridor", "showExisting" }) do
		if type(d[k]) == "boolean" then S[k] = d[k] end
	end
	if type(d.manual) == "table" then S.manual = d.manual end
	return d.project
end

function B.defaultYear()
	local y = gameYear()
	local years = {}
	for _, e in ipairs(S.index) do years[#years + 1] = e.year end
	table.sort(years)
	if #years == 0 then return y end
	if y == nil then return years[1] end
	for _, v in ipairs(years) do
		if v >= y then return v end
	end
	return years[#years]
end

function B.filtered()
	local r = {}
	for _, e in ipairs(S.index) do
		local okYear = (not S.filterYear) or e.year == S.year
		local okSearch = S.search == "" or (tostring(e.title or ""):find(S.search, 1, true) ~= nil)
			or tostring(e.line or "") == S.search or tostring(e.id or "") == S.search
		if okYear and okSearch then r[#r + 1] = e end
	end
	return r
end

function B.refilter(keepId)
	S.list = B.filtered()
	S.pos = 1
	if keepId then
		for i, e in ipairs(S.list) do
			if e.id == keepId then S.pos = i end
		end
	end
	if S.pos > #S.list then S.pos = math.max(1, #S.list) end
end

function B.current()
	if not S.list then return nil end
	return S.list[S.pos]
end

function B.project(id)
	return S.data and S.data.projects and S.data.projects[id] or nil
end

---------------------------------------------------------------------------
-- preparation: chunks and items per project

local function chunkSegment(pr, si, seg, out)
	local pts = pairsXY(seg.p)
	local carry = nil
	local acc = 0
	for i = 1, #pts - 1 do
		local ax, ay = pts[i][1], pts[i][2]
		local bx, by = pts[i + 1][1], pts[i + 1][2]
		local L = math.sqrt((bx - ax) ^ 2 + (by - ay) ^ 2)
		if L > 0.01 then
			local n = math.max(1, math.floor(L / B.CHUNK_M + 0.5))
			local h = math.atan2(by - ay, bx - ax)
			for j = 0, n - 1 do
				local t0, t1 = j / n, (j + 1) / n
				local cx0, cy0 = ax + (bx - ax) * t0, ay + (by - ay) * t0
				local cx1, cy1 = ax + (bx - ax) * t1, ay + (by - ay) * t1
				out[#out + 1] = { seg = si, t = seg.t, k = seg.k, ax = cx0, ay = cy0, bx = cx1, by = cy1,
					mx = (cx0 + cx1) / 2, my = (cy0 + cy1) / 2, h = h, len = L / n,
					s0 = acc + L * t0, s1 = acc + L * t1,
					check = (seg.t == "new" or seg.t == "shared"), done = false }
			end
			acc = acc + L
		end
	end
	return carry
end

function B.prepare(id)
	local p = S.prep[id]
	if p then return p end
	local pr = B.project(id)
	if not pr then return nil end
	p = { id = id, chunks = {}, items = {}, segStart = {} }
	for si, seg in ipairs(list(pr.segments)) do
		p.segStart[si] = #p.chunks + 1
		chunkSegment(pr, si, seg, p.chunks)
	end
	for ii, it in ipairs(list(pr.items)) do
		p.items[ii] = { src = it, done = false }
	end
	S.prep[id] = p
	return p
end

function B.visibleIds()
	local ids = {}
	if S.hidden or not S.active or not S.data then return ids end
	local cur = B.current()
	if not cur then return ids end
	if S.showYearAll and S.filterYear then
		for _, e in ipairs(S.list) do
			if #ids < B.MAX_YEAR_PROJECTS then ids[#ids + 1] = e.id end
		end
	else
		ids[1] = cur.id
	end
	return ids
end

---------------------------------------------------------------------------
-- ghost index: what the player has built

local function hermite(p0, p1, t0, t1, n)
	local pts = {}
	for i = 0, n do
		local t = i / n
		local t2, t3 = t * t, t * t * t
		local h00, h10, h01, h11 = 2 * t3 - 3 * t2 + 1, t3 - 2 * t2 + t, -2 * t3 + 3 * t2, t3 - t2
		pts[#pts + 1] = { h00 * p0.x + h10 * t0.x + h01 * p1.x + h11 * t1.x,
			h00 * p0.y + h10 * t0.y + h01 * p1.y + h11 * t1.y }
	end
	return pts
end

--- sampled 2d polyline of a BaseEdge and its kind ("rail" / "road" / nil)
function B.edgeShape(be)
	local p0, p1 = be.position0, be.position1
	if p0 == nil or p1 == nil then return nil end
	local dx, dy = p1.x - p0.x, p1.y - p0.y
	local chord = math.sqrt(dx * dx + dy * dy)
	local pts
	local t0, t1 = be.tangent0, be.tangent1
	local okT = false
	if t0 and t1 and chord > 1 then
		local n0 = math.sqrt(t0.x * t0.x + t0.y * t0.y)
		local n1 = math.sqrt(t1.x * t1.x + t1.y * t1.y)
		okT = n0 > 0.3 * chord and n0 < 3 * chord and n1 > 0.3 * chord and n1 < 3 * chord
	end
	if okT then
		pts = hermite(p0, p1, t0, t1, math.max(1, math.min(24, math.ceil(chord / 10))))
	else
		pts = { { p0.x, p0.y }, { p1.x, p1.y } }
	end
	local kind = nil
	local rt = be.roadType
	local okE, E = pcall(function() return api.type.enum.RoadType end)
	if okE and E then
		if rt == E.TRACK then kind = "rail" elseif rt == E.STREET then kind = "road" end
	end
	if kind == nil and type(rt) == "number" then kind = (rt == 0) and "rail" or "road" end
	return pts, kind
end

local function gridKey(x, y)
	return math.floor(x / B.GRID) .. ":" .. math.floor(y / B.GRID)
end

local function conCenter(e, cc)
	local c = C.bboxCenter(e)
	if c then return c.x, c.y end
	local ok, x, y = pcall(function()
		local t = cc.transf
		return t[13], t[14]
	end)
	if ok and type(x) == "number" then return x, y end
	return nil
end

--- Rebuild the index of player-owned edges/constructions when the PLAYER_OWNED set changes.
--- Returns true when the world changed (or on the first run).
function B.snapshot()
	local ok, ents = pcall(function() return C.list(api.engine.getEntitiesWithComponent(C.CT("PLAYER_OWNED"))) end)
	if not ok or type(ents) ~= "table" then
		S.mode = "octree"
		return false
	end
	S.mode = "grid"
	local sum, n = 0, #ents
	for i = 1, n do sum = (sum + (ents[i] % 1000003) * (i % 97 + 1)) % 2147483647 end
	local sig = string.format("%.0f:%.0f", n, sum)
	if sig == S.sig then return false end
	S.sig = sig
	local grid, cons = {}, {}
	local getComponent = api.engine.getComponent
	local CT_EDGE, CT_CON, CT_PO = C.CT("BASE_EDGE"), C.CT("CONSTRUCTION"), C.CT("PLAYER_OWNED")
	local CT_ST = C.CT("STATION")
	local pl = player()
	for _, e in ipairs(ents) do
		pcall(function()
			if pl ~= nil then
				-- the PLAYER_OWNED list holds every company's entities
				local po = getComponent(e, CT_PO)
				if po == nil or po.player ~= pl then return end
			end
			local be = getComponent(e, CT_EDGE)
			if be then
				local pts, kind = B.edgeShape(be)
				if pts then
					local rec = { e = e, pts = pts, kind = kind }
					local seen = {}
					for _, q in ipairs(pts) do
						local k = gridKey(q[1], q[2])
						if not seen[k] then
							seen[k] = true
							grid[k] = grid[k] or {}
							table.insert(grid[k], rec)
						end
					end
				end
				return
			end
			local cc = getComponent(e, CT_CON)
			if cc then
				local x, y = conCenter(e, cc)
				if x then cons[#cons + 1] = { e = e, f = tostring(cc.fileName or ""), x = x, y = y } end
				return
			end
			-- a street stop is a terminal built into the street, not a construction: its STATION counts
			if CT_ST and getComponent(e, CT_ST) then
				local c = C.bboxCenter(e)
				if c then cons[#cons + 1] = { e = e, f = "::/stations/street/stops/_street_terminal", x = c.x, y = c.y } end
			end
		end)
	end
	S.grid, S.cons = grid, cons
	return true
end

local function edgeMatches(rec, kind, mx, my, h)
	if rec.kind ~= nil and rec.kind ~= kind then return false end
	local pts = rec.pts
	for i = 1, #pts - 1 do
		local a, b = pts[i], pts[i + 1]
		if segDist(mx, my, a[1], a[2], b[1], b[2]) <= B.GHOST_RADIUS then
			local eh = math.atan2(b[2] - a[2], b[1] - a[1])
			if angDiff(eh, h) <= B.GHOST_ANGLE then return true end
		end
	end
	return false
end

local function chunkBuiltGrid(ch)
	local gx, gy = math.floor(ch.mx / B.GRID), math.floor(ch.my / B.GRID)
	for dx = -1, 1 do
		for dy = -1, 1 do
			local cell = S.grid[(gx + dx) .. ":" .. (gy + dy)]
			if cell then
				for _, rec in ipairs(cell) do
					if edgeMatches(rec, ch.k, ch.mx, ch.my, ch.h) then return true end
				end
			end
		end
	end
	return false
end

local function chunkBuiltOctree(ch, pl, anyOwner)
	local ents = C.list(api.engine.util.octree.findEntitiesInCircle(vec2(ch.mx, ch.my), B.GHOST_RADIUS + 5, C.CT("BASE_EDGE")))
	for _, e in ipairs(ents) do
		local po = C.comp(e, "PLAYER_OWNED")
		-- the player's own, or (anyOwner) nobody's like ordinary streets; never another company's
		if (po and po.player == pl) or (anyOwner and (po == nil or po.player == nil or po.player < 0)) then
			local be = C.comp(e, "BASE_EDGE")
			if be then
				local pts, kind = B.edgeShape(be)
				if pts and edgeMatches({ pts = pts, kind = kind }, ch.k, ch.mx, ch.my, ch.h) then return true end
			end
		end
	end
	return false
end

local function plainFind(s, pats)
	for _, p in ipairs(list(pats)) do
		if s:find(p, 1, true) then return true end
	end
	return false
end

local function itemRadius(it)
	local L, W = it.len or 40, it.wid or 30
	return math.sqrt(L * L + W * W) / 2 + B.ITEM_MARGIN
end

local function itemMatchesCon(it, f)
	local low = string.lower(f)
	return plainFind(low, it.match) and not plainFind(low, it.exclude)
end

local function itemBuiltGrid(it)
	local r = itemRadius(it)
	for _, c in ipairs(S.cons or {}) do
		local dx, dy = c.x - it.x, c.y - it.y
		if dx * dx + dy * dy <= r * r and itemMatchesCon(it, c.f) then return true end
	end
	return false
end

local function itemBuiltOctree(it, pl)
	local ents = C.list(api.engine.util.octree.findEntitiesInCircle(vec2(it.x, it.y), itemRadius(it), C.CT("CONSTRUCTION")))
	for _, e in ipairs(ents) do
		local po = C.comp(e, "PLAYER_OWNED")
		local cc = C.comp(e, "CONSTRUCTION")
		if po and po.player == pl and cc and itemMatchesCon(it, tostring(cc.fileName or "")) then return true end
	end
	return false
end

--- Re-check visible projects; returns true if any done flag changed.
function B.check(force)
	local ids = B.visibleIds()
	if #ids == 0 then return false end
	local changedWorld = B.snapshot()
	local changed = false
	local pl = player()
	if S.mode == "grid" then
		-- streets are usually nobody's (the game's "own street" option is off by default), so new road
		-- chunks not found among the player's entities are looked up on the map, any owner, a few per tick
		local budget = B.ROAD_OCTREE_BUDGET
		for _, id in ipairs(ids) do
			local p = B.prepare(id)
			if p and budget > 0 then
				local n = #p.chunks
				p.rr = p.rr or 0
				for _ = 1, math.min(n, budget) do
					p.rr = p.rr % math.max(1, n) + 1
					local ch = p.chunks[p.rr]
					if ch and ch.check and (not ch.done or ch.anyDone) then
						budget = budget - 1
						local ok, built = pcall(chunkBuiltOctree, ch, pl, true)
						if ok and built then
							if not ch.done then changed = true end
							ch.anyDone, ch.done = true, true
						elseif ok and ch.anyDone then
							-- that street is gone: the grid pass decides again (player-owned ways only)
							ch.anyDone = false
							local okG, g = pcall(chunkBuiltGrid, ch)
							ch.done = (okG and g) or false
							changed = true
						end
					end
				end
			end
		end
		if not (changedWorld or force) then
			-- the world did not change, but newly shown projects may not be checked yet
			local fresh = false
			for _, id in ipairs(ids) do
				local p = B.prepare(id)
				if p and not p.checkedSig then fresh = true end
			end
			if not fresh then return changed end
		end
		for _, id in ipairs(ids) do
			local p = B.prepare(id)
			if p and (changedWorld or force or not p.checkedSig) then
				for _, ch in ipairs(p.chunks) do
					if ch.check then
						local ok, built = pcall(chunkBuiltGrid, ch)
						built = (ok and built) or ch.anyDone or false
						if built ~= ch.done then ch.done = built; changed = true end
					end
				end
				for _, it in ipairs(p.items) do
					local ok, built = pcall(itemBuiltGrid, it.src)
					built = ok and built or false
					if built ~= it.done then it.done = built; changed = true end
				end
				p.checkedSig = S.sig
			end
		end
		return changed
	end
	-- octree fallback: round robin with a budget
	local budget = B.CHECK_BUDGET
	for _, id in ipairs(ids) do
		local p = B.prepare(id)
		if p then
			local n = #p.chunks
			for _ = 1, math.min(n, budget) do
				S.rr = S.rr % math.max(1, n) + 1
				local ch = p.chunks[S.rr]
				if ch and ch.check then
					local ok, built = pcall(chunkBuiltOctree, ch, pl)
					built = ok and built or false
					if built ~= ch.done then ch.done = built; changed = true end
				end
				budget = budget - 1
			end
			for _, it in ipairs(p.items) do
				local ok, built = pcall(itemBuiltOctree, it.src, pl)
				built = ok and built or false
				if built ~= it.done then it.done = built; changed = true end
			end
		end
	end
	return changed
end

---------------------------------------------------------------------------
-- progress

function B.progress(id)
	local p = B.prepare(id)
	if not p then return nil end
	local r = { chunks = 0, built = 0, sharedChunks = 0, sharedBuilt = 0, groups = {} }
	for _, ch in ipairs(p.chunks) do
		if ch.t == "new" then
			r.chunks = r.chunks + 1
			if ch.done then r.built = r.built + 1 end
		elseif ch.t == "shared" then
			r.sharedChunks = r.sharedChunks + 1
			if ch.done then r.sharedBuilt = r.sharedBuilt + 1 end
		end
	end
	local itemsDone = true
	for _, it in ipairs(p.items) do
		local k = it.src.k
		local g = (k == "maint") and "depot" or k
		r.groups[g] = r.groups[g] or { n = 0, done = 0 }
		r.groups[g].n = r.groups[g].n + 1
		if it.done then r.groups[g].done = r.groups[g].done + 1 else itemsDone = false end
	end
	r.pct = r.chunks > 0 and math.floor(100 * r.built / r.chunks + 0.5) or 100
	r.done = (r.chunks == 0 or r.pct >= B.DONE_PCT) and itemsDone
	local parts = {}
	if r.chunks > 0 then parts[#parts + 1] = string.format(T("Трасса построена на %.0f %%"), r.pct) end
	if r.sharedChunks > 0 then
		parts[#parts + 1] = string.format(T("общие участки %.0f %%"), 100 * r.sharedBuilt / r.sharedChunks)
	end
	for _, g in ipairs({ "station", "stop", "harbor", "depot", "warehouse" }) do
		local v = r.groups[g]
		if v then
			local name = ({ station = T("станции"), stop = T("остановки"), harbor = T("порты"), depot = T("депо"), warehouse = T("склады") })[g]
			parts[#parts + 1] = string.format("%s %.0f/%.0f", name, v.done, v.n)
		end
	end
	r.text = table.concat(parts, ", ")
	return r
end

--- done flag of one checklist step (nil = not checkable: vehicles, line, info, demolition)
function B.stepDone(id, e)
	if S.manual and S.manual[id .. "#" .. tostring(e and e.n)] then return true end
	return B.stepDoneAuto(id, e)
end

--- what the game state says (nil = the game cannot check this step)
function B.stepDoneAuto(id, e)
	local p = B.prepare(id)
	local ref = e and e.ref
	if not p or type(ref) ~= "table" then return nil end
	local gs = S.gs or {}
	if ref.loans then
		local n = gs.loans or 0
		return n >= ref.loans, nil, { math.min(n, ref.loans), ref.loans }
	end
	if ref.line then
		local m = gs.lines and gs.lines[id]
		if not m then return false, nil, { 0, 1 } end
		return m.matched >= m.total, nil, { m.matched, m.total }
	end
	if ref.vehicles then
		local m = gs.lines and gs.lines[id]
		local n = m and m.vehicles or 0
		return n >= ref.vehicles, nil, { math.min(n, ref.vehicles), ref.vehicles }
	end
	if ref.item then
		local it = p.items[ref.item]
		return it and it.done or false
	end
	if ref.seg then
		local n, built = 0, 0
		for _, ch in ipairs(p.chunks) do
			if ch.seg == ref.seg and ch.check and (ref.d0 == nil or (ch.s1 > ref.d0 and ch.s0 < ref.d1)) then
				n = n + 1
				if ch.done then built = built + 1 end
			end
		end
		if n == 0 then return nil end
		return built >= n * B.DONE_PCT / 100, math.floor(100 * built / n + 0.5)
	end
	return nil
end

--- a street stop step: the game may not report street terminals to the mod, so it can also be
--- confirmed by hand («Готово»)
function B.isStopStep(id, e)
	local ref = e and type(e.ref) == "table" and e.ref or nil
	local pr = B.project(id)
	local it = ref and ref.item and list(pr and pr.items)[ref.item] or nil
	return it ~= nil and it.k == "stop"
end

function B.checklist(id)
	local pr = B.project(id)
	local out = {}
	for _, e in ipairs(list(pr and pr.checklist)) do
		local done, pct, counter = B.stepDone(id, e)
		local ui = type(e.ui) == "table" and e.ui or nil
		out[#out + 1] = { n = e.n, text = e.text, kind = e.kind, done = done, pct = pct, canShow = e.x ~= nil,
			counter = counter, menu = ui and ui.menu or nil, canOpen = ui ~= nil and (ui.res ~= nil or ui.way ~= nil),
			manual = (B.stepDoneAuto(id, e) == nil and e.kind ~= "info") or B.isStopStep(id, e) }
	end
	return out
end

---------------------------------------------------------------------------
-- camera

local function focus(x, y, z, dist)
	local ok, err = pcall(function()
		api.gui.camera.focusPosition(vec3(x, y, z or 0), dist)
	end)
	if not ok then
		pcall(function() api.gui.camera.setManualCamera(vec2(x, y), dist) end)
	end
	return ok
end

--- Camera to the project: its first unbuilt checklist step once something is built, else the whole route.
function B.focusProject(id)
	id = id or (B.current() and B.current().id)
	local pr = id and B.project(id)
	if not pr then return false end
	local anyBuilt = false
	local p = B.prepare(id)
	for _, ch in ipairs(p and p.chunks or {}) do
		if ch.done then anyBuilt = true break end
	end
	for _, it in ipairs(p and p.items or {}) do
		if it.done then anyBuilt = true end
	end
	if anyBuilt then
		for _, e in ipairs(list(pr.checklist)) do
			if e.x ~= nil and B.stepDone(id, e) == false then
				S.focusStep = e.n
				return focus(e.x, e.y, e.z, math.max(300, math.min(e.r or 600, 4000)))
			end
		end
	end
	local bb = pr.bbox
	if type(bb) == "table" and bb[1] then
		local w, h = bb[3] - bb[1], bb[4] - bb[2]
		S.focusStep = nil
		return focus((bb[1] + bb[3]) / 2, (bb[2] + bb[4]) / 2, 0, math.max(500, math.min(math.max(w, h) * 1.2, 15000)))
	end
	return false
end

--- "Показать": camera to checklist step n and let its place pulse for B.HIGHLIGHT_S seconds.
function B.show(n)
	local cur = B.current()
	if not cur then return false end
	local pr = B.project(cur.id)
	for _, e in ipairs(list(pr and pr.checklist)) do
		if e.n == n and e.x ~= nil then
			focus(e.x, e.y, e.z, math.max(250, math.min((e.r or 400), 6000)))
			local r = 40
			local ref = e.ref
			if type(ref) == "table" and ref.item then
				local it = list(pr.items)[ref.item]
				if it then r = math.sqrt((it.len or 40) ^ 2 + (it.wid or 30) ^ 2) / 2 + 15 end
			elseif e.kind == "segment" or e.kind == "span" then
				r = 60
			end
			S.hl = { id = cur.id, n = n, x = e.x, y = e.y, z = e.z or 0, r = r,
				untilT = clock() + B.HIGHLIGHT_S }
			pcall(B.render)
			return true
		end
	end
	return false
end

---------------------------------------------------------------------------
-- world labels: seven-segment digits drawn as flat zones (the API has no world text)

local SEG7 = {
	["0"] = "abcdef", ["1"] = "bc", ["2"] = "abged", ["3"] = "abgcd", ["4"] = "fgbc",
	["5"] = "afgcd", ["6"] = "afgedc", ["7"] = "abc", ["8"] = "abcdefg", ["9"] = "abcdfg",
}

local function rectPoly(x0, y0, x1, y1)
	return { { x0, y0 }, { x1, y0 }, { x1, y1 }, { x0, y1 } }
end

--- zones for the number `num` with its lower-left corner at (x, y)
function B.digitPolys(num, x, y, H)
	local out = {}
	local W, T = H * 0.5, H * 0.14
	local str = tostring(math.floor(num))
	for i = 1, #str do
		local ch = str:sub(i, i)
		local ox = x + (i - 1) * (W + T * 1.6)
		local m = H / 2
		local seg = {
			a = rectPoly(ox, y + H - T, ox + W, y + H),
			b = rectPoly(ox + W - T, y + m, ox + W, y + H),
			c = rectPoly(ox + W - T, y, ox + W, y + m),
			d = rectPoly(ox, y, ox + W, y + T),
			e = rectPoly(ox, y, ox + T, y + m),
			f = rectPoly(ox, y + m, ox + T, y + H),
			g = rectPoly(ox, y + m - T / 2, ox + W, y + m + T / 2),
		}
		local on = SEG7[ch] or ""
		for j = 1, #on do
			local sname = on:sub(j, j)
			out[#out + 1] = { key = i .. sname, poly = seg[sname] }
		end
	end
	return out
end

-- 5x7 bitmap letters for the item labels (С станция, О остановка, П порт, Д депо, Т техобслуживание,
-- СК склад); rows top to bottom, each lit run becomes one flat zone
B.GLYPHS = {
	["С"] = { ".###.", "#...#", "#....", "#....", "#....", "#...#", ".###." },
	["О"] = { ".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###." },
	["П"] = { "#####", "#...#", "#...#", "#...#", "#...#", "#...#", "#...#" },
	["Д"] = { ".###.", ".#.#.", ".#.#.", ".#.#.", ".#.#.", "#####", "#...#" },
	["Т"] = { "#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.." },
	["К"] = { "#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#" },
}

--- UTF-8 characters of a string (Lua 5.2 has no utf8 library)
function B.chars(str)
	local out, i = {}, 1
	str = tostring(str or "")
	while i <= #str do
		local c = str:byte(i)
		local n = (c >= 0xF0 and 4) or (c >= 0xE0 and 3) or (c >= 0xC0 and 2) or 1
		out[#out + 1] = str:sub(i, i + n - 1)
		i = i + n
	end
	return out
end

--- zones for a label: bitmap letters followed by seven-segment digits, lower-left corner at (x, y)
function B.labelPolys(letters, num, x, y, H)
	local out = {}
	local px = H / 7
	local ox = x
	for li, ch in ipairs(B.chars(letters)) do
		local g = B.GLYPHS[ch]
		if g then
			for r, row in ipairs(g) do
				local y1 = y + H - (r - 1) * px
				local c = 1
				while c <= #row do
					if row:sub(c, c) == "#" then
						local c2 = c
						while c2 < #row and row:sub(c2 + 1, c2 + 1) == "#" do c2 = c2 + 1 end
						out[#out + 1] = { key = "g" .. li .. ":" .. r .. ":" .. c,
							poly = rectPoly(ox + (c - 1) * px, y1 - px, ox + c2 * px, y1) }
						c = c2 + 1
					else
						c = c + 1
					end
				end
			end
			ox = ox + 6 * px
		end
	end
	if num then
		for _, d in ipairs(B.digitPolys(num, ox + px * 0.5, y, H)) do out[#out + 1] = d end
	end
	return out
end

---------------------------------------------------------------------------
-- rendering (diff against what is currently set)

local function strip(pts, w)
	local n = #pts
	if n < 2 then return nil end
	local left, right = {}, {}
	for i = 1, n do
		local ax, ay, bx, by
		if i == 1 then ax, ay, bx, by = pts[1][1], pts[1][2], pts[2][1], pts[2][2]
		elseif i == n then ax, ay, bx, by = pts[n - 1][1], pts[n - 1][2], pts[n][1], pts[n][2]
		else ax, ay, bx, by = pts[i - 1][1], pts[i - 1][2], pts[i + 1][1], pts[i + 1][2] end
		local dx, dy = bx - ax, by - ay
		local L = math.sqrt(dx * dx + dy * dy)
		if L < 1e-6 then L = 1 end
		local nx, ny = -dy / L * w / 2, dx / L * w / 2
		left[i] = { pts[i][1] + nx, pts[i][2] + ny }
		right[i] = { pts[i][1] - nx, pts[i][2] - ny }
	end
	local poly = {}
	for i = 1, n do poly[#poly + 1] = left[i] end
	for i = n, 1, -1 do poly[#poly + 1] = right[i] end
	return poly
end

local function chunkStyle(ch)
	if ch.done then return "done" end
	if ch.t == "new" then return "new_" .. (ch.k == "rail" and "rail" or "road") end
	return ch.t
end

local function addZone(desired, key, poly, color)
	if poly and #poly >= 3 then desired.zones[key] = { poly = poly, color = color } end
end

local function addCircle(desired, key, x, y, r, color, anim)
	desired.zones[key] = { circle = { x, y, r }, color = color, anim = anim }
end

--- Screen orientation for world labels: "up" = the camera's horizontal view direction, snapped to
--- 45 degrees (labels are redrawn only when the snapped direction changes). Default: north up.
B.ORIENT_STEPS = 8
function B.viewOrient()
	local ok, o = pcall(function()
		local e, c = api.gui.camera.getEye(), api.gui.camera.getCenter()
		local dx, dy = c.x - e.x, c.y - e.y
		if dx * dx + dy * dy < 1e-6 then return nil end
		local a = math.atan2(dy, dx)
		return math.floor(a / (2 * math.pi / B.ORIENT_STEPS) + 0.5) % B.ORIENT_STEPS
	end)
	if ok and o then return o end
	return 2        -- atan2 = 90 degrees: north up
end

local function basis()
	local a = (S.orient or 2) * 2 * math.pi / B.ORIENT_STEPS
	local ux, uy = math.cos(a), math.sin(a)
	return ux, uy, uy, -ux      -- up, right (= up turned clockwise)
end
B.basis = basis

--- local label coordinates (right, up) -> world, relative to the anchor
local function toWorld(poly, ax, ay)
	local ux, uy, rx, ry = basis()
	local out = {}
	for i, q in ipairs(poly) do
		out[i] = { ax + q[1] * rx + q[2] * ux, ay + q[1] * ry + q[2] * uy }
	end
	return out
end

--- anchor offset towards the screen's upper right
local function upRight(x, y, dist)
	local ux, uy, rx, ry = basis()
	local k = dist / math.sqrt(2)
	return x + (ux + rx) * k, y + (uy + ry) * k
end

local function addLabel(desired, key, letters, num, ax, ay, H, color)
	for _, d in ipairs(B.labelPolys(letters, num, 0, 0, H)) do
		addZone(desired, key .. ":o" .. tostring(S.orient or 2) .. ":" .. d.key, toWorld(d.poly, ax, ay), color)
	end
end

local function addNumber(desired, key, num, ax, ay, H, color)
	for _, d in ipairs(B.digitPolys(num, 0, 0, H)) do
		addZone(desired, key .. ":o" .. tostring(S.orient or 2) .. ":" .. d.key, toWorld(d.poly, ax, ay), color)
	end
end

local function addRuns(desired, pid, p)
	local chunks = p.chunks
	local i = 1
	while i <= #chunks do
		local ch = chunks[i]
		local style = chunkStyle(ch)
		local j = i
		while j < #chunks and j - i + 1 < B.RUN and chunks[j + 1].seg == ch.seg and chunkStyle(chunks[j + 1]) == style
			and math.abs(chunks[j + 1].ax - chunks[j].bx) < 0.5 and math.abs(chunks[j + 1].ay - chunks[j].by) < 0.5 do
			j = j + 1
		end
		local draw = true
		if style == "done" and not S.showBuilt then draw = false end
		if style == "existing" and not S.showExisting then draw = false end
		if draw then
			if style == "existing" then
				-- dashed: every other chunk
				for q = i, j do
					if q % 2 == 0 then
						local c = chunks[q]
						addZone(desired, B.PREFIX .. "z:" .. pid .. ":" .. q .. ":" .. q .. ":" .. style,
							strip({ { c.ax, c.ay }, { c.bx, c.by } }, B.LINE_W * 0.75), COLORS.existing)
					end
				end
			else
				local pts = { { chunks[i].ax, chunks[i].ay } }
				for q = i, j do pts[#pts + 1] = { chunks[q].bx, chunks[q].by } end
				addZone(desired, B.PREFIX .. "z:" .. pid .. ":" .. i .. ":" .. j .. ":" .. style,
					strip(pts, B.LINE_W), COLORS[style] or COLORS.new_road)
			end
		end
		i = j + 1
	end
end

function B.desired()
	local desired = { zones = {}, markers = {} }
	S.orient = B.viewOrient()
	for _, id in ipairs(B.visibleIds()) do
		local pr, p = B.project(id), B.prepare(id)
		if pr and p then
			addRuns(desired, id, p)
			for n, sp in ipairs(list(pr.spans)) do
				addZone(desired, B.PREFIX .. "s:" .. id .. ":" .. n .. ":" .. tostring(sp.t),
					strip(pairsXY(sp.p), B.SPAN_W), COLORS[sp.t] or COLORS.bridge)
			end
			if S.showCorridor then
				for si, seg in ipairs(list(pr.segments)) do
					if seg.t == "new" then
						local pts = pairsXY(seg.p)
						-- split long segments so each zone polygon stays small
						local from = 1
						while from < #pts do
							local to = math.min(#pts, from + 24)
							local part = {}
							for q = from, to do part[#part + 1] = pts[q] end
							addZone(desired, B.PREFIX .. "c:" .. id .. ":" .. si .. ":" .. from,
								strip(part, pr.corridor or 20), COLORS.corridor)
							from = to
						end
					end
				end
			end
			for n, dm in ipairs(list(pr.demolition)) do
				addZone(desired, B.PREFIX .. "d:" .. id .. ":" .. n, strip(pairsXY(dm.p), B.DEMOLITION_W), COLORS.demolition)
			end
			local isCur = B.current() and B.current().id == id
			-- mission focus: the selected task of the current project is drawn bright with one arrow
			-- (and a pulse); the other buildings of the project stay as faint outlines
			local task = isCur and B.activeTask(id) or nil
			local taskItem = task and type(task.ref) == "table" and task.ref.item or nil
			if S.showStations then
				for n, it in ipairs(p.items) do
					local src = it.src
					local color = COLORS[src.k] or COLORS.station
					local focus = (taskItem == n) or not isCur
					if not focus then color = { color[1], color[2], color[3], color[4] * 0.4 } end
					if not it.done or S.showBuilt then
						local poly = pairsXY(src.poly)
						addZone(desired, B.PREFIX .. "i:" .. id .. ":" .. n .. ":" .. (it.done and "done" or (focus and "todo" or "dim")),
							poly, it.done and COLORS.item_done or color)
					end
					if not it.done then
						if focus then
							desired.markers[B.PREFIX .. "m:" .. id .. ":i" .. n] = { x = src.x, y = src.y, z = (src.z or 0) + 15,
								s = (taskItem == n) and 1.0 or 0.6 }
						end
						local acc = src.access
						if type(acc) == "table" and acc[4] then
							-- access stub: building front -> road (the entrance side)
							addZone(desired, B.PREFIX .. "a:" .. id .. ":" .. n .. (focus and "" or ":dim"),
								strip({ { acc[1], acc[2] }, { acc[3], acc[4] } }, B.ACCESS_W),
								focus and COLORS.access or { 1, 1, 1, 0.3 })
						end
						if isCur and B.LABELS then
							-- type letter + step number next to the footprint: «Д9», «С1»
							local half = math.sqrt((src.len or 40) ^ 2 + (src.wid or 30) ^ 2) / 2
							local lc = { color[1], color[2], color[3], 0.95 }
							local lx, ly = upRight(src.x, src.y, half + 8)
							addLabel(desired, B.PREFIX .. "l:" .. id .. ":i" .. n, src.label or "", src.step,
								lx, ly, B.DIGIT_H, lc)
						end
					end
				end
			end
			if isCur then
				-- route points: only those of the selected road/track/bridge task (all when no task is selected)
				local only = nil
				if task and type(task.wp) == "table" and #list(task.wp) > 0 then
					only = {}
					for _, w in ipairs(list(task.wp)) do only[w] = true end
				elseif task then
					only = {}
				end
				for n, wp in ipairs(list(pr.waypoints)) do
					if only == nil or only[wp.n or n] then
						addCircle(desired, B.PREFIX .. "w:" .. id .. ":" .. n, wp.x, wp.y, 7, COLORS.waypoint, false)
						local wx, wy = upRight(wp.x, wp.y, 14)
						addNumber(desired, B.PREFIX .. "l:" .. id .. ":w" .. n, wp.n or n, wx, wy,
							B.DIGIT_H * 0.7, COLORS.waypoint)
					end
				end
			end
		end
	end
	local hl = S.hl
	if hl then
		local now = clock()
		if S.hidden or not S.active or now > hl.untilT or not (B.current() and B.current().id == hl.id) then
			S.hl = nil
		else
			addCircle(desired, B.PREFIX .. "hl:" .. hl.id .. ":" .. hl.n, hl.x, hl.y, hl.r, COLORS.highlight, true)
			desired.markers[B.PREFIX .. "hlm:" .. hl.id .. ":" .. hl.n] = { x = hl.x, y = hl.y, z = hl.z + 40, s = 1.4 }
		end
	end
	return desired
end

function B.renderNow()
	local mission = api.gui and api.gui.mission
	if not mission then
		S.error = T("api.gui.mission недоступен")
		return false
	end
	local desired = B.desired()
	local nz, nm, removed, failed = 0, 0, 0, 0
	for k in pairs(S.zones) do
		if not desired.zones[k] then
			pcall(mission.removeZone, k)
			S.zones[k] = nil
			removed = removed + 1
		end
	end
	for k in pairs(S.markers) do
		if not desired.markers[k] then
			pcall(mission.removeMarker, k)
			S.markers[k] = nil
			removed = removed + 1
		end
	end
	local count = 0
	for _ in pairs(S.zones) do count = count + 1 end
	local lastErr = nil
	for k, z in pairs(desired.zones) do
		if not S.zones[k] then
			if count >= B.MAX_ZONES then break end
			local ok, err = pcall(function()
				if z.circle then
					mission.setZoneCircle(k, vec2(z.circle[1], z.circle[2]), z.circle[3], true, vec4(z.color), false,
						z.anim and true or false)
				else
					local poly = {}
					for i, q in ipairs(z.poly) do poly[i] = vec2(q[1], q[2]) end
					-- draw = true, prohibitBuilding = false, playAnimation = false
					mission.setZone(k, poly, true, vec4(z.color), false, false)
				end
			end)
			if ok then
				S.zones[k] = true
				EVER.zones[k] = true
				nz = nz + 1
				count = count + 1
			else
				failed = failed + 1
				lastErr = err
			end
		end
	end
	for k, m in pairs(desired.markers) do
		if not S.markers[k] then
			local ok, err = pcall(mission.setMarkerAtPosition, k, vec3(m.x, m.y, m.z), "arrow", m.s, false, false)
			if ok then
				S.markers[k] = true
				EVER.markers[k] = true
				nm = nm + 1
			else
				failed = failed + 1
				lastErr = err
			end
		end
	end
	local total = 0
	for _ in pairs(S.zones) do total = total + 1 end
	S.renderStats = string.format(T("зон %.0f (+%.0f), маркеров +%.0f, убрано %.0f%s"), total, nz, nm, removed,
		failed > 0 and (T(", ошибок ") .. failed .. ": " .. tostring(lastErr)) or "")
	if count >= B.MAX_ZONES then S.renderStats = S.renderStats .. T(" — достигнут лимит зон") end
	return true
end

--- Remove every zone and marker this module has set.
function B.clearNow()
	local mission = api.gui and api.gui.mission
	for _, set in ipairs({ S.zones, EVER.zones }) do
		for k in pairs(set) do
			if mission then pcall(mission.removeZone, k) end
		end
	end
	for _, set in ipairs({ S.markers, EVER.markers }) do
		for k in pairs(set) do
			if mission then pcall(mission.removeMarker, k) end
		end
	end
	S.zones, S.markers = {}, {}
	EVER.zones, EVER.markers = {}, {}
	S.hl = nil
end

---------------------------------------------------------------------------
-- window lifecycle and actions (called by the GUI)

--- Turn the blueprint on (drawn in the world until close(), with or without the window).
--- moveCamera = false: no camera jump (auto-restore after loading a savegame).
function B.open(moveCamera)
	S.active = true
	S.hidden = false
	pcall(B.pollStatus)
	if not S.data then B.load() end
	pcall(B.check, true)
	pcall(B.render)
	if moveCamera ~= false then pcall(B.focusProject) end
	pcall(B.saveSelection)
	pcall(B.refreshGame)
	pcall(B.hudFollow)
	pcall(B.applyHighlights)
	pcall(B.render)
	B.version = (B.version or 0) + 1
end

--- Turn the blueprint off: everything this module drew disappears.
function B.close()
	S.active = false
	B.clear()
	pcall(B.applyHighlights)
	pcall(B.saveSelection)
	B.version = (B.version or 0) + 1
end

function B.isActive()
	return S.active and true or false
end

--- was the blueprint on when the game was saved / last used (GUI save data, then userdata)
function B.wasActive()
	local d = nil
	pcall(function()
		local g = api.gui.game.getGuiSaveData(B.MOD_ID)
		if type(g) == "table" then d = g end
	end)
	if d == nil then
		pcall(function()
			local u = app.loadUserdata("tf3_planner", "blueprint_state")
			if type(u) == "table" then d = u end
		end)
	end
	return type(d) == "table" and d.active == true
end

--- periodic update (GUI timer, every few seconds); returns true if the view changed
---------------------------------------------------------------------------
-- recalculation status (control centre writes <userdata>/tf3_planner/status.lua)

local STATE_RU = { running = "идёт", done = "готово", failed = "ошибка", cancelled = "отменён", idle = "ожидание" }

local function now()
	return clock()
end

function B.statusText(st)
	if type(st) ~= "table" then return nil end
	local state = tostring(st.state or "idle")
	if state == "running" then
		local parts = { T("Пересчёт плана") }
		if st.step and st.steps then
			parts[#parts + 1] = string.format(T(": шаг %.0f/%.0f"), st.step, st.steps)
		end
		if st.label and st.label ~= "" then parts[#parts + 1] = " «" .. tostring(st.label) .. "»" end
		if type(st.year) == "number" and type(st["end"]) == "number" then
			parts[#parts + 1] = string.format(", %.0f/%.0f", st.year, st["end"])
		end
		if type(st.progress) == "number" and st.progress >= 0 then
			parts[#parts + 1] = string.format(" (%.0f %%)", 100 * st.progress)
		end
		return table.concat(parts)
	elseif state == "done" then
		return T("План обновлён") .. ((st.text and st.text ~= "") and (": " .. tostring(st.text)) or "")
	elseif state == "failed" then
		return T("Ошибка пересчёта") .. ((st.text and st.text ~= "") and (": " .. tostring(st.text)) or "")
	elseif state == "cancelled" then
		return T("Пересчёт отменён")
	end
	return nil
end

--- text progress bar for the window (0..1, nil/negative = unknown)
function B.progressBar(x, width)
	width = width or 20
	if type(x) ~= "number" or x < 0 then return nil end
	local n = math.max(0, math.min(width, math.floor(x * width + 0.5)))
	return "[" .. string.rep("#", n) .. string.rep("-", width - n) .. "]"
end

--- Read status.lua; reload the overlay when plan_overlay.lua changed. Returns true if the view changed.
function B.pollStatus()
	if not (app and app.loadUserdata) then return false end
	local ok, st = pcall(app.loadUserdata, "tf3_planner", "status")
	if not ok or type(st) ~= "table" then st = nil end
	local changed = false
	local old = S.status
	S.status = st
	if st then
		local stamp = st.overlay_updated
		if stamp ~= nil and S.overlayStamp ~= nil and stamp ~= S.overlayStamp then
			local okL, loaded = pcall(B.load)
			if okL and loaded then
				S.notice = { text = T("Чертёж обновлён"), untilT = now() + 20 }
				pcall(B.check, true)
				pcall(B.render)
			end
			changed = true
		end
		if stamp ~= nil then S.overlayStamp = stamp end
		if S.waiting and (st.state == "running" or (type(st.updated) == "number" and st.updated > S.waiting)) then
			S.waiting = nil
		end
		if old == nil or old.state ~= st.state or old.step ~= st.step or old.progress ~= st.progress
			or old.year ~= st.year or old.updated ~= st.updated then
			changed = true
		end
	elseif old ~= nil then
		changed = true
	end
	if S.notice and now() > S.notice.untilT then
		S.notice = nil
		changed = true
	end
	return changed
end

--- «Экспорт и пересчёт»: the same export as the Export button; the control centre (auto mode)
--- picks the new export up and recalculates. Returns ok, summary.
function B.exportAndRecalc()
	local okR, export = pcall(ug_require, "tf3_planner_1::/tf3_planner/export.lua")
	if not okR or not export then
		S.notice = { text = T("экспорт недоступен: ") .. tostring(export), untilT = now() + 20 }
		return false
	end
	local ok, res, summary = pcall(export.run)
	if not ok then
		S.notice = { text = T("экспорт не удался: ") .. tostring(res), untilT = now() + 30 }
		return false
	end
	S.waiting = now()
	S.notice = { text = res and T("Экспорт записан") or T("Экспорт записан частично (см. stdout)"), untilT = now() + 10 }
	return true, summary
end

function B.tick()
	CLOCK.t = CLOCK.t + CLOCK.step
	local statusChanged = false
	-- app.loadUserdata answers nil inside a react step timer (seen in game): read in the join
	if B.deferFn then
		B.defer(function()
			local okS, ch = pcall(B.pollStatus)
			if okS and ch then B.version = (B.version or 0) + 1 end
		end)
	else
		local okS, ch = pcall(B.pollStatus)
		if okS and ch then statusChanged = true end
	end
	if not S.data or S.hidden or not S.active then
		-- a refused «highlight off» (switched off) is retried here
		if next(S.hlIds or {}) then pcall(B.applyHighlights) end
		if statusChanged then B.version = (B.version or 0) + 1 end
		return statusChanged
	end
	local ok, changed = pcall(B.check, false)
	if not ok then
		S.error = T("проверка построенного: ") .. tostring(changed)
		return true
	end
	local expired = S.hl and clock() > S.hl.untilT
	local turned = B.viewOrient() ~= (S.orient or 2)
	if changed or expired or turned then pcall(B.render) end
	local okG, gsChanged = pcall(B.refreshGame)
	if okG and gsChanged then changed = true end
	local hudMoved = B.hudFollow()
	pcall(B.applyHighlights)
	if hudMoved then pcall(B.render) end
	local any = (changed or expired or turned or statusChanged or hudMoved) and true or false
	if any then B.version = (B.version or 0) + 1 end
	return any
end

--- after any change of the selected project: redraw, move the camera, remember the choice
local function reselect(moveCamera)
	S.hl = nil
	S.hudN, S.hudDone = nil, nil
	B.version = (B.version or 0) + 1
	pcall(B.check, false)
	pcall(B.render)
	if moveCamera ~= false then pcall(B.focusProject) end
	pcall(B.saveSelection)
	pcall(B.hudFollow)
end

function B.next(step)
	if not S.list or #S.list == 0 then return end
	S.pos = (S.pos - 1 + (step or 1)) % #S.list + 1
	reselect()
end

function B.yearStep(step)
	local years, seen = {}, {}
	for _, e in ipairs(S.index) do
		if not seen[e.year] then seen[e.year] = true; years[#years + 1] = e.year end
	end
	table.sort(years)
	if #years == 0 then return end
	local target = nil
	if step > 0 then
		for _, y in ipairs(years) do if S.year == nil or y > S.year then target = y; break end end
	else
		for i = #years, 1, -1 do if S.year == nil or years[i] < S.year then target = years[i]; break end end
	end
	if target then
		S.year = target
		S.filterYear = true
		B.refilter(nil)
		reselect()
	end
end

function B.setSearch(s)
	S.search = tostring(s or "")
	local cur = B.current()
	B.refilter(cur and cur.id or nil)
	reselect()
end

function B.toggle(name)
	if name == "filterYear" then
		local cur = B.current()
		S.filterYear = not S.filterYear
		if S.filterYear and cur then S.year = cur.year end
		B.refilter(cur and cur.id or nil)
	else
		S[name] = not S[name]
	end
	if name == "hidden" and S.hidden then B.clear() return end
	-- display toggles keep the camera where it is; the list filter may change the project
	reselect(name == "filterYear")
end

function B.reload()
	B.load()
	reselect()
end

---------------------------------------------------------------------------
-- mission mode: game state (loans, the project's line and its vehicles), UI highlights, build menu

--- raw checklist entry of the selected task of project id (nil when it is not the current project)
function B.activeTask(id)
	local cur = B.current()
	if not cur or cur.id ~= id or not S.hudN then return nil end
	local pr = B.project(id)
	return list(pr and pr.checklist)[S.hudN]
end

function B.loanCount()
	local st = C.gameScriptState("::/game_mechanics/finance/loan.gs")
	local n = 0
	pcall(function()
		local ob = st.obtainedLoans
		local ok = pcall(function() for _ in pairs(ob) do n = n + 1 end end)
		if not ok then n = #C.list(ob) end
	end)
	return n
end

local function stopPositions(stop)
	local out = {}
	pcall(function()
		local sgc = C.comp(stop.stationGroup, "STATION_GROUP")
		for _, st in ipairs(C.list(sgc and sgc.stations)) do
			local c = C.bboxCenter(st)
			if c then out[#out + 1] = c end
		end
	end)
	return out
end

--- the player's line serving the most planned stations of project id: {line, matched, total, vehicles}
function B.lineFor(id, lines)
	local p = B.prepare(id)
	if not p then return nil end
	local targets = {}
	for _, it in ipairs(p.items) do
		local k = it.src.k
		if k == "station" or k == "stop" or k == "harbor" then targets[#targets + 1] = it.src end
	end
	if #targets == 0 then return nil end
	local best = nil
	for _, l in ipairs(lines) do
		local matched = 0
		for _, t in ipairs(targets) do
			local hit = false
			for _, c in ipairs(l.pos) do
				if (c.x - t.x) ^ 2 + (c.y - t.y) ^ 2 <= B.LINE_MATCH_M ^ 2 then hit = true break end
			end
			if hit then matched = matched + 1 end
		end
		if matched > 0 and (best == nil or matched > best.matched) then
			best = { line = l.e, matched = matched, total = #targets }
		end
	end
	if best then
		pcall(function()
			best.vehicles = #C.list(api.engine.system.transportVehicleSystem.getLineVehicles(best.line))
		end)
		best.vehicles = best.vehicles or 0
	end
	return best
end

--- refresh S.gs (loans, the current project's line); returns true when something changed
function B.refreshGame()
	local gs = { loans = B.loanCount(), lines = {} }
	local cur = B.current()
	if cur then
		local lines = {}
		local pl = player()
		-- the player's lines: lineSystem first (a loop over the LINE component may be refused)
		local ents, how = nil, nil
		pcall(function()
			if pl ~= nil then ents = C.list(api.engine.system.lineSystem.getLinesForPlayer(pl)); how = "lineSystem.player" end
		end)
		if not ents or #ents == 0 then
			pcall(function() ents = C.list(api.engine.system.lineSystem.getLines()); how = "lineSystem.all" end)
		end
		if not ents or #ents == 0 then
			pcall(function() ents = C.list(api.engine.getEntitiesWithComponent(C.CT("LINE"))); how = "component" end)
		end
		for _, e in ipairs(ents or {}) do
			pcall(function()
				local own = true
				if pl ~= nil and how ~= "lineSystem.player" then
					local po = api.engine.getComponent(e, C.CT("PLAYER_OWNED"))
					own = (po == nil) or po.player == pl
				end
				if own then
					local lc = C.comp(e, "LINE")
					local pos = {}
					for _, stop in ipairs(C.list(lc and lc.stops)) do
						for _, c in ipairs(stopPositions(stop)) do pos[#pos + 1] = c end
					end
					lines[#lines + 1] = { e = e, pos = pos }
				end
			end)
		end
		gs.lines[cur.id] = B.lineFor(cur.id, lines)
		local m = gs.lines[cur.id]
		local sig = string.format("%s:%d:%s:%s:%s", tostring(how), #lines, tostring(m and m.matched), tostring(m and m.total),
			tostring(m and m.vehicles))
		if sig ~= S.lineLog then
			S.lineLog = sig
			local npos = {}
			for _, l in ipairs(lines) do npos[#npos + 1] = tostring(#l.pos) end
			pcall(print, "[TF3P] lines via " .. tostring(how) .. ": " .. #lines .. " (stop positions " .. table.concat(npos, ",")
				.. "), project " .. tostring(cur.id) .. " matched " .. tostring(m and m.matched) .. "/" .. tostring(m and m.total)
				.. ", vehicles " .. tostring(m and m.vehicles))
		end
	end
	local old = S.gs
	S.gs = gs
	if old == nil then return true end
	local a = cur and old.lines and old.lines[cur.id] or nil
	local b = cur and gs.lines[cur.id] or nil
	return old.loans ~= gs.loans or (a and a.matched) ~= (b and b.matched) or (a and a.vehicles) ~= (b and b.vehicles)
end

-- buttons of the game UI to highlight per task menu (ids from the game's own tutorial/missions)
B.MENU_IDS = {
	road = { "menu.construction.road" }, rail = { "menu.construction.rail" }, water = { "menu.construction.water" },
	warehouse = { "menu.construction.warehouse" }, roads = { "menu.construction.roads" },
	tracks = { "menu.construction.tracks" },
	-- the «Получить» buttons of all loan cards share one tag: only the button and the tab glow, the
	-- task text names the card (B.loanCard)
	finances = { "menu.financesButton", "menu.finances.loans-tab" },
	vehicles = { "menu.managementMode", "manager.buyVehicle.button" },
	lines = { "menu.managementMode", "menu.management.newLineButton" },
	bulldozer = { "menu.bulldozer" },
}
B.MENU_HINT = {
	road = T("Меню строительства: дорожный транспорт (кнопка подсвечена). Или нажмите «Открыть в меню»."),
	rail = T("Меню строительства: железная дорога (кнопка подсвечена). Или нажмите «Открыть в меню»."),
	water = T("Меню строительства: водный транспорт (кнопка подсвечена). Или нажмите «Открыть в меню»."),
	warehouse = T("Меню строительства: склады (кнопка подсвечена). Или нажмите «Открыть в меню»."),
	roads = T("Меню дорог (подсвечено): ведите дорогу по голубой линии от точки к точке; мост/тоннель — в параметрах инструмента."),
	tracks = T("Меню путей (подсвечено): ведите путь по оранжевой линии от точки к точке; мост/тоннель — в параметрах инструмента."),
	finances = T("Финансы → вкладка «Займы» (подсвечены)."),
	vehicles = T("Управление → купить транспорт (подсвечено); назначьте технику на линию этого проекта."),
	lines = T("Управление → новая линия (подсвечено): добавьте станции по порядку из текста задания."),
	bulldozer = T("Бульдозер (подсвечен): снесите здания под красной полосой."),
}

local HL_COLOR = { 1.0, 0.8, 0.1 }
local HL_LOGGED = {}

--- switch a game button highlight on/off; every call and its result goes to stdout ([TF3P] hl ...)
--- so a highlight that will not go away can be diagnosed from the game log
local function setHl(id, on)
	local gui = api.gui and api.gui.byId
	if not gui or not gui.setHighlighted then return end
	-- cards (a loan offer, a building in the build menu, a way type): a frame; buttons: a circle
	local card = id:find("^tf3p%.loan%.") or id:find("%.con@") or id:find("%.con$") or id:find("street_template")
		or id:find("%.track$") or id:find("::/", 1, true)
	local style = card and api.gui.HighlightStyle.Square or api.gui.HighlightStyle.Circle
	local color = api.type.Vec3f.new(HL_COLOR[1], HL_COLOR[2], HL_COLOR[3])
	local ok, err
	if on then
		ok, err = pcall(gui.setHighlighted, id, true, style, color)
	else
		-- drop the rule, as the game's missions do
		ok, err = pcall(gui.setHighlighted, id, nil)
	end
	if ok or not HL_LOGGED[id .. tostring(on)] then
		HL_LOGGED[id .. tostring(on)] = true
		pcall(print, "[TF3P] hl " .. (on and "on " or "off ") .. id .. " " .. (ok and "ok" or tostring(err)))
	end
	return ok
end

--- highlight the game buttons of the selected task (nothing when off, hidden or the task is done)
function B.applyHighlights()
	local want = {}
	local cur = B.current()
	if S.active and not S.hidden and cur then
		local e = B.activeTask(cur.id)
		local ui = e and type(e.ui) == "table" and e.ui or nil
		if ui and ui.menu and B.stepDone(cur.id, e) ~= true then
			for _, id in ipairs(B.MENU_IDS[ui.menu] or {}) do want[id] = true end
			-- inside the open build menu: the tab and the item itself
			if ui.res then
				local okM, mt = pcall(B.menuTargets, ui.res, ui.menu, ui.filter)
				if okM and mt then
					for id in pairs(mt.tags) do want[id] = true end
					for id in pairs(mt.tabs) do want[id] = true end
				end
			elseif ui.way then
				local nm = B.resolveRes(ui.way, true)
				if nm then want[nm] = true end
			end
			if ui.menu == "finances" then
				-- the one offer card to take (card id from the DefaultLoanCard wrapper in the button script)
				local okC, _, amount = pcall(B.loanCard, ui.amount)
				if okC and amount then want[string.format("tf3p.loan.%.0f", amount)] = true end
			end
		end
	end
	S.hlWant = want
	B.flushHighlights()
end

--- Run fn where the game API is unrestricted. React step timers run in a parallel phase in which
--- api.gui.byId.setHighlighted, setGuiSaveData, saveUserdata... fail with "API is currently restricted";
--- the GUI script installs react.enqueueJoin here (the main-thread join after the react phase, as the
--- game's own construction menu does from its step timer). Without it (tests) fn runs directly.
B.deferFn = nil
function B.setDeferrer(fn)
	B.deferFn = fn
end
function B.defer(fn)
	if B.deferFn then
		local ok = pcall(B.deferFn, fn)
		if ok then return end
	end
	fn()
end

--- zones/markers are game API calls too: draw and clear in the join (same order as requested)
function B.render()
	B.defer(function() pcall(B.renderNow) end)
	return true
end

function B.clear()
	B.defer(function() pcall(B.clearNow) end)
	S.hl = nil
end

--- Apply S.hlWant to the game UI (deferred to the unrestricted join). An id is recorded as set/unset
--- only when the call succeeded, so a refused call is retried on the next tick.
function B.flushHighlights()
	B.defer(function()
		local want = S.hlWant or {}
		S.hlIds = S.hlIds or {}
		for id in pairs(S.hlIds) do
			if not want[id] and setHl(id, false) then S.hlIds[id] = nil end
		end
		for id in pairs(want) do
			if not S.hlIds[id] and setHl(id, true) then S.hlIds[id] = true end
		end
	end)
end

--- game-script side: no longer used (the join above works); kept so an old save's script entity
--- (tf3p_gui.gs) still finds its function
function B.applyHighlightChannel() end

--- resource name of a construction (by file name suffix) or a street/track type (by base name)
local RES_CACHE = {}
function B.resolveRes(suffix, street)
	local key = (street and "s:" or "c:") .. tostring(suffix)
	if RES_CACHE[key] ~= nil then return RES_CACHE[key] or nil end
	local found = false
	pcall(function()
		local rep = street and api.res.streetTemplateRep or api.res.constructionRep
		for _, name in pairs(rep.getAll()) do
			local nm = tostring(name)
			if street then
				local base = nm:match("([^/]+)$") or nm
				base = base:gsub("%.[^.]*$", "")
				if base == suffix then found = nm break end
			elseif nm:sub(-#suffix) == suffix and (#nm == #suffix or nm:sub(-#suffix - 1, -#suffix - 1) == "/") then
				found = nm
				break
			end
		end
	end)
	RES_CACHE[key] = found
	return found or nil
end

--- Construction menu targets of a building: the item tags and the tab ids to highlight.
--- Menu items are tagged with their resource name, plus "@<template index>" (0-based, in the order of
--- desc.constructionTemplates) when the construction has templates (e.g. the street terminal has a
--- passenger and a cargo template per era); tabs have the id "menu.construction.<menu>.<category>"
--- (its header button: ".indicator"). Only templates available this year and matching `filter`
--- ("cargo"/"passenger", the template's filterCategories) are taken.
local MENU_CACHE = {}
function B.menuTargets(res, menu, filter)
	local key = tostring(res) .. "|" .. tostring(menu) .. "|" .. tostring(filter) .. "|" .. tostring(gameYear())
	if MENU_CACHE[key] then return MENU_CACHE[key] end
	local out = { tags = {}, tabs = {} }
	local name = B.resolveRes(res, false)
	if not name then return out end
	local year = gameYear() or 0
	-- the card's outer component is tagged "<list id>.<resName>[@template]", the list id being the tab id
	-- "menu.construction.<menu>.<category>": unique per card (the inner tag repeats in the search list)
	local function addCats(mc, itemTag)
		pcall(function()
			for _, c in ipairs(C.list(mc.categories)) do
				if c.category and menu then
					local tabId = "menu.construction." .. menu .. "." .. tostring(c.category)
					out.tags[tabId .. "." .. itemTag] = true
					-- only the tab's header button (".indicator"): the tab id itself is the whole item list
					out.tabs[tabId .. ".indicator"] = true
				end
			end
		end)
	end
	pcall(function()
		local desc = api.res.constructionRep.get(api.res.constructionRep.find(name))
		local n = 0
		pcall(function() n = desc.constructionTemplates_native:size() end)
		local any = false
		for i = 1, n do
			local t = desc.constructionTemplates_native:at(i)
			local okT = pcall(function()
				local av = t.availability
				local from = av and av.yearFrom or 0
				local to = av and av.yearTo or 0
				local inYear = (from == 0 or from <= year) and (to == 0 or to > year)
				local cats = C.list(t.menuCategory.categories)
				local match = filter == nil
				for _, c in ipairs(cats) do
					for _, f in ipairs(C.list(c.filterCategories)) do
						if f == filter then match = true end
					end
				end
				if #cats > 0 and inYear and match then
					addCats(t.menuCategory, name .. "@" .. tostring(i - 1))
					any = true
				end
			end)
		end
		if not any then
			addCats(desc.menuCategory, name)
		end
	end)
	MENU_CACHE[key] = out
	local tl, bl = {}, {}
	for k in pairs(out.tags) do tl[#tl + 1] = k end
	for k in pairs(out.tabs) do bl[#bl + 1] = k end
	pcall(print, "[TF3P] menu targets " .. tostring(res) .. " (" .. tostring(filter) .. "): items "
		.. table.concat(tl, ", ") .. "; tabs " .. table.concat(bl, ", "))
	return out
end

--- «Открыть в меню»: the construction menu opens on the building / way of the selected task
function B.openMenu(n)
	local cur = B.current()
	if not cur then return false end
	local pr = B.project(cur.id)
	local e = list(pr and pr.checklist)[n or S.hudN or 1]
	local ui = e and type(e.ui) == "table" and e.ui or nil
	if not ui then return false end
	local name = nil
	if ui.res then name = B.resolveRes(ui.res, false) end
	if not name and ui.way then name = B.resolveRes(ui.way, true) end
	if name then
		local ok = pcall(function()
			api.gui.fireReactEvent("constructionMenuSelectTabForConstruction", { resName = name })
		end)
		if ok then return true end
	end
	return false
end

function B.notify(text)
	S.notice = { text = tostring(text), untilT = clock() + 15 }
	B.version = (B.version or 0) + 1
end

--- mark a task that the game cannot check (demolition, hints) as done by hand / undo
function B.toggleManual(n)
	local cur = B.current()
	if not cur then return end
	S.manual = S.manual or {}
	local k = cur.id .. "#" .. tostring(n or S.hudN)
	if S.manual[k] then S.manual[k] = nil else S.manual[k] = true end
	pcall(B.saveSelection)
	S.hudN = nil
	B.hudFollow()
	pcall(B.applyHighlights)
	pcall(B.render)
	B.version = (B.version or 0) + 1
end

--- select task n of the current project (camera to it, highlights)
function B.selectTask(n)
	S.hudN = n
	S.hudPinned = true
	local cur = B.current()
	local cl = cur and B.checklist(cur.id) or {}
	S.hudDone = cl[n] and cl[n].done
	if cl[n] and cl[n].canShow then pcall(B.show, n) else pcall(B.render) end
	pcall(B.applyHighlights)
	B.version = (B.version or 0) + 1
end

---------------------------------------------------------------------------
-- «Построить»: build a planned building exactly as the plan places it (same command as the build
-- menu: makeWorldBuildProposalCmd(proposal, nil, false, true) — the game checks the place and charges
-- the money). First a dry run (makeProposalData: cost + errors), then the real command.
-- Supported now: modular street terminals (item.build.kind == "street_terminal"); their params are
-- built like the game's createTemplateFn (verified against a terminal the player built).

B.TERM_DIR = "::/stations/street/modular_street_station/"
B.TERM_SPEC_MODULE = { [1] = "cargo_platform.module", [2] = "cargo_platform_liquid.module",
	[3] = "cargo_platform_bulk.module", [4] = "cargo_platform_flatbed.module", [5] = "cargo_platform_goods.module" }
-- tries for a building: { along the road, away from the road } in metres. Along: the entrance stays on
-- the road (the game joins the station's street stub to it); away: small corrections only
B.LINK_GAP = 6      -- metres between a building's stub end and the road it is linked to by a short edge
B.BUILD_OFFSETS = { { 0, 0 }, { 0, 3 }, { 12, 0 }, { -12, 0 }, { 24, 0 }, { -24, 0 }, { 36, 0 }, { -36, 0 },
	{ 0, -2 }, { 0, 6 } }

local function offPair(off)
	if type(off) == "table" then return off[1] or 0, off[2] or 0 end
	return 0, off or 0
end
B.offPair = offPair

local function offText(off)
	local a, w = offPair(off)
	local parts = {}
	if a ~= 0 then parts[#parts + 1] = string.format(T("вдоль дороги %+.0f м"), a) end
	if w ~= 0 then parts[#parts + 1] = string.format(T("от дороги %+.0f м"), w) end
	return table.concat(parts, ", ")
end

local function mangle(i, j, v)
	return 200000 * (j + 100) + 100 * (i + 100) + v
end

--- construction params of a modular street terminal (= its createTemplateFn for the template)
function B.terminalParams(b, year)
	local cargo = b.cargo ~= false
	local name = B.TERM_DIR .. (cargo and (B.TERM_SPEC_MODULE[b.specialization or 1] or "cargo_platform.module")
		or "passenger_platform.module")
	local v = cargo and 1 or 0
	local m = {}
	local function put(i, j) m[mangle(i, j, v)] = { name = name, variant = 0 } end
	put(0, 0); put(0, 1)
	local P = b.platforms or 1
	if P > 1 then put(-1, 0); put(-1, 1) end
	if P > 2 then put(1, 0); put(1, 1); put(-2, 0); put(-2, 1) end
	m[mangle(55, 0, 3)] = { name = B.TERM_DIR .. "entrance_exit.module", variant = 0 }
	local params = { tramTrack = b.tramTrack or 1, platforms = P, year = year, seed = 0, modules = m }
	if cargo then params.specialization = b.specialization or 1 end
	return params
end

local function terrainZ(x, y)
	local z = nil
	pcall(function() z = api.engine.util.terrain.getHeightAt(api.type.Vec2f.new(x, y)) end)
	if z == nil then pcall(function() z = api.engine.terrain.getHeightAt(api.type.Vec2f.new(x, y)) end) end
	return z or 0
end

--- the proposal for item `it` with the station moved `off` metres along n (away from the road);
--- `removes` = constructions to demolish with it (town houses in the way)
--- place (optional): { tx, ty, tz } fixed position from a check (the joint node then exists already).
--- Returns sp, nil, nil, place {tx, ty, tz, joint} — joint = the road edge point that needs a node.
function B.itemProposal(it, off, removes, place)
	local b = it.build
	if type(b) ~= "table" or (b.kind ~= "street_terminal" and b.kind ~= "construction") then
		return nil, T("для этой постройки кнопки пока нет")
	end
	local file = B.resolveRes(b.res or "modular_terminal.con", false)
	if not file then return nil, T("постройка не найдена в игре: ") .. tostring(b.res) end
	local n = list(b.n)
	local t = list(b.t)
	local nx, ny = n[1], n[2]
	local along, away = offPair(off)
	local tx, ty = t[1] + nx * away + ny * along, t[2] + ny * away - nx * along
	-- a building with a street stub next to an EXISTING road: its stub end must be exactly where the
	-- road gets its new node (the game joins them only at a node, never mid-edge); move it onto that point
	-- A construction's street stub is NOT joined to an existing road node by a script proposal (seen in
	-- game: collision even with an existing node right at the stub end). What works: the construction
	-- first, then a street edge from its stub node to the road (like a road built to a station). So the
	-- station stands LINK_GAP m off the road and a short link edge is built afterwards.
	local linked = b.kind == "street_terminal" or b.res == "road_depot.con"
	local snap = b.snap or 30
	local road = nil
	if place then
		tx, ty, road = place.tx, place.ty, place.road
	elseif linked and B.nearNode and B.nearStreetPoint then
		local sx, sy = tx - nx * snap, ty - ny * snap
		local nd = B.nearNode(sx, sy, 4, nil, true)
		if nd then
			road = { x = nd.x, y = nd.y, z = nd.z, node = nd.e }
		else
			local okJ, ed = pcall(B.nearStreetPoint, sx, sy, 4)
			if okJ and ed then road = { x = ed.x, y = ed.y, z = ed.z, edge = ed } end
		end
		if road then
			tx, ty = road.x + nx * (snap + B.LINK_GAP), road.y + ny * (snap + B.LINK_GAP)
		end
	end
	local tz = terrainZ(tx, ty)
	if road and road.z then tz = road.z end
	if place and place.tz then tz = place.tz end
	local ce = api.type.SimpleProposal.ConstructionEntity.new()
	ce.fileName = file
	if b.kind == "construction" then
		-- plain construction (depot, maintenance building): its own params + the year (game gives `state`)
		local params = { year = gameYear() or 1900, seed = 0 }
		for k, v in pairs(type(b.params) == "table" and b.params or {}) do params[k] = v end
		ce.params = params
	else
		ce.params = B.terminalParams(b, gameYear() or 1900)
	end
	-- columns: local X = (n.y, -n.x), local Y = n (away from the road), Z up, translation
	ce.transf = api.type.Mat4f.new(
		api.type.Vec4f.new(ny, -nx, 0, 0),
		api.type.Vec4f.new(nx, ny, 0, 0),
		api.type.Vec4f.new(0, 0, 1, 0),
		api.type.Vec4f.new(tx, ty, tz, 1))
	ce.playerEntity = player()
	local sp = api.type.SimpleProposal.new()
	sp.constructionsToAdd = { ce }
	if removes and #removes > 0 then sp.constructionsToRemove = removes end
	-- its street stub ends on an existing road: that road needs a node there first. In one proposal
	-- the game does not merge the stub with a NEW node (collision, seen in game), so the node is built
	-- as a separate first step (B.buildConfirm) and the station joins the then existing node.
	return sp, nil, nil, { tx = tx, ty = ty, tz = tz, road = road, snap = snap }
end

--- town-house construction of a colliding entity (nil: not a town house -> a real obstacle)
local function houseConstruction(e)
	local con = nil
	pcall(function()
		local tb = api.engine.getComponent(e, C.CT("TOWN_BUILDING"))
		if tb then
			con = tb.construction
			if (con == nil or con < 0) then
				con = api.engine.system.streetConnectorSystem.getConstructionEntityForTownBuilding(e)
			end
			return
		end
		local cc = api.engine.getComponent(e, C.CT("CONSTRUCTION"))
		if cc and tostring(cc.fileName or ""):find("/town", 1, true) then con = e end
	end)
	if type(con) == "number" and con >= 0 then return con end
	return nil
end

--- what a colliding entity is (for the log)
local function describe(e)
	local out = tostring(e)
	pcall(function()
		local cc = api.engine.getComponent(e, C.CT("CONSTRUCTION"))
		if cc then out = out .. " con " .. tostring(cc.fileName) return end
		local be = api.engine.getComponent(e, C.CT("BASE_EDGE"))
		if be then
			out = out .. string.format(" edge %s (%.0f,%.0f)-(%.0f,%.0f)", tostring(be.roadTemplate),
				be.position0.x, be.position0.y, be.position1.x, be.position1.y)
			return
		end
		local tb = api.engine.getComponent(e, C.CT("TOWN_BUILDING"))
		if tb then out = out .. " town building" return end
		local c = C.bboxCenter(e)
		if c then out = out .. string.format(" at (%.0f,%.0f)", c.x, c.y) end
	end)
	return out
end
B.describeEntity = describe

--- colliding entities of a dry run: (house constructions to demolish, other obstacles)
local function collisionsOf(pd, ignore)
	local houses, others, seen = {}, 0, {}
	pcall(function()
		local ci = pd.collisionInfo
		local ents = {}
		for _, d in ipairs(C.list(ci.collisionEntities)) do ents[#ents + 1] = d.entity or d end
		for _, e in ipairs(C.list(ci.buildingEntities)) do ents[#ents + 1] = e end
		for _, e in ipairs(ents) do
			local h = houseConstruction(e)
			if ignore and ignore[e] then
				-- expected: the road the stub will join once it has its node
			elseif h then
				if not seen[h] then seen[h] = true; houses[#houses + 1] = h end
			else
				others = others + 1
				if others <= 8 then pcall(print, "[TF3P]   collision with " .. describe(e)) end
			end
		end
	end)
	return houses, others
end

local function errorsOf(pd)
	local out, critical = {}, false
	pcall(function()
		local es = pd.errorState
		critical = es.critical and true or false
		for _, s in ipairs(C.list(es.messages)) do out[#out + 1] = tostring(s) end
	end)
	return out, critical
end

--- the game's own evaluation of a proposal without building it (costs, errors, collisions); with a
--- player context first (as the build tools evaluate it), else without
--- the build context of the player: the game charges the costs to context.player (as its own paid
--- builds do, e.g. the bridge/tunnel type change: Context.new() + player); gatherBuildings lets the
--- evaluation list the houses in the way
function B.playerContext(gather)
	local ctx = nil
	pcall(function()
		ctx = api.type.Context.new()
		ctx.player = api.engine.util.getPlayer()
		if gather then ctx.gatherBuildings = true end
	end)
	return ctx
end

function B.dryRun(sp)
	local ctx = B.playerContext(true)
	local pd = nil
	if ctx ~= nil then
		local ok, r = pcall(function() return api.engine.util.proposal.makeProposalData(sp, ctx) end)
		if ok then pd = r end
	end
	if pd == nil then pd = api.engine.util.proposal.makeProposalData(sp) end
	return pd
end

---------------------------------------------------------------------------
-- «Построить» for a new road segment: the drawn line becomes street edges (like the game's own
-- mission track builder: SimpleProposal.streetProposal with NodeAndEntity / SegmentAndEntity),
-- bridges/tunnels as edges of that type, ends joined to existing nodes nearby, and the entrance
-- of a planned station on this road joined to the road. Same dry run + paid build as buildings.

B.ROAD_MAX_EDGE_M = 80     -- longer straight pieces are split (edges follow the terrain)
B.ROAD_MIN_EDGE_M = 4      -- nodes closer than this are merged
B.ROAD_SNAP_NODE_M = 30    -- an existing node this close to an end is used as that end
B.ROAD_ON_EXISTING_M = 4   -- line points this close to an existing edge run ON it (the plan's line is approximate)
B.STATION_SNAP_NODE_M = 4  -- a station's entrance node this close to its expected place
B.ROAD_SPLIT_EDGE_M = 8    -- else an existing edge this close is split there and joined

local REP_ID = {}
--- id of a bridge/tunnel type by file name suffix ("trestle.bridge")
local function repId(rep, suffix)
	local key = tostring(rep) .. "|" .. tostring(suffix)
	if REP_ID[key] ~= nil then return REP_ID[key] or nil end
	local found = false
	pcall(function()
		for id, name in pairs(rep.getAll()) do
			local nm = tostring(name)
			if suffix and nm:sub(-#suffix) == suffix then found = id break end
		end
	end)
	REP_ID[key] = found
	return found or nil
end

--- bridge (or tunnel) type the game's road/track tool would pick (construction_react_util.tl:305-349):
--- available this year, allows the carrier (ROAD 0 / RAIL 1), auto-selectable, the smallest speed
--- limit at or above the road's lane speed (else the closest below). nil: none (never fall back to 0)
function B.autoStructureType(carrier, maxLaneSpeed, year, tunnel)
	local rep = tunnel and api.res.tunnelTypeRep or api.res.bridgeTypeRep
	year = year or gameYear() or 1900
	local above, below, any = nil, nil, nil
	pcall(function()
		for id, _ in pairs(rep.getAll()) do
			local bt = rep.get(id)
			local av = bt.availability
			local yf, yt = av and av.yearFrom or 0, av and av.yearTo or 0
			local available = yf >= 0 and (yf == 0 or yf <= year) and (yt == 0 or year < yt)
			local fits = false
			for _, c in ipairs(C.list(bt.carriers)) do if c == carrier then fits = true end end
			if available and fits then
				any = any or id
				if bt.isAutoSelectable ~= false then
					local sl = bt.speedLimit or 0
					if sl >= maxLaneSpeed then
						if not above or sl < above[2] then above = { id, sl } end
					elseif not below or sl > below[2] then
						below = { id, sl }
					end
				end
			end
		end
	end)
	return (above and above[1]) or (below and below[1]) or any
end

--- does structure type `id` allow the carrier
local function structureFits(rep, id, carrier)
	local ok = false
	pcall(function()
		for _, c in ipairs(C.list(rep.get(id).carriers)) do if c == carrier then ok = true end end
	end)
	return ok
end

function B.templateSpeed(tpl)
	local v = 0
	pcall(function()
		for _, lc in ipairs(C.list(tpl.laneConfigs)) do v = math.max(v, lc.speed or 0) end
	end)
	return v
end

B.WATER_LEVEL = 0             -- export meta.terrain.waterLevel
B.BRIDGE_DECK_ABOVE_WATER = 3 -- the game's own old road bridges: 2.3-3.3 m over water level 0
B.RAMP_SLOPE = 0.06           -- approach ramps to a raised deck (street slope limit unverified)
B.ROAD_MAX_GRADE = 0.12       -- node-to-node grade of a script-built street (= planner routing.ROAD_MAX_GRADE)

--- nearest existing street/track node within r: {e, x, y, z}. Nodes are not found by the octree
--- (in-game: none returned), so they are taken from the ends of the edges around (edges are found)
--- a street (not track) edge that is not part of a construction (a station's own entrance etc.)
local function freeStreet(e, be)
	local ok = true
	pcall(function()
		if be.roadType ~= nil and api.type.enum and be.roadType ~= api.type.enum.RoadType.STREET then ok = false end
	end)
	if ok then
		pcall(function()
			local c = api.engine.system.streetConnectorSystem.getConstructionEntityForEdge(e)
			if type(c) == "number" and c >= 0 then ok = false end
		end)
	end
	return ok
end

--- `streetOnly` = node of a free street edge (to join a new road to); else any edge's node (to pass
--- through a construction's own entrance node, e.g. a station already built)
local function nearNode(x, y, r, used, streetOnly)
	local best, bd = nil, r
	pcall(function()
		for _, e in ipairs(C.list(api.engine.util.octree.findEntitiesInCircle(vec2(x, y), r + 60, C.CT("BASE_EDGE")))) do
			local be = api.engine.getComponent(e, C.CT("BASE_EDGE"))
			if be and be.position0 and be.position1 and (not streetOnly or freeStreet(e, be)) then
				for _, q in ipairs({ { be.node0, be.position0 }, { be.node1, be.position1 } }) do
					local p = q[2]
					local d = math.sqrt((p.x - x) ^ 2 + (p.y - y) ^ 2)
					if q[1] and d < bd and not (used and used[q[1]]) then
						best, bd = { e = q[1], x = p.x, y = p.y, z = p.z }, d
					end
				end
			end
		end
	end)
	return best
end

--- an existing street edge passing within r of (x, y) and the Hermite parameter there:
--- {e, be, t, x, y, z, dx, dy, dz} (dx.. = derivative at t), nil if none / it carries edge objects
local function hermite3(p0, p1, t0, t1, t)
	local t2, t3 = t * t, t * t * t
	local h00, h10, h01, h11 = 2 * t3 - 3 * t2 + 1, t3 - 2 * t2 + t, -2 * t3 + 3 * t2, t3 - t2
	local d00, d10, d01, d11 = 6 * t2 - 6 * t, 3 * t2 - 4 * t + 1, -6 * t2 + 6 * t, 3 * t2 - 2 * t
	local function c(k) return h00 * p0[k] + h10 * t0[k] + h01 * p1[k] + h11 * t1[k] end
	local function d(k) return d00 * p0[k] + d10 * t0[k] + d01 * p1[k] + d11 * t1[k] end
	return c("x"), c("y"), c("z"), d("x"), d("y"), d("z")
end

local function nearEdge(x, y, r, allowEnds)
	local best = nil
	pcall(function()
		for _, e in ipairs(C.list(api.engine.util.octree.findEntitiesInCircle(vec2(x, y), r, C.CT("BASE_EDGE")))) do
			local be = api.engine.getComponent(e, C.CT("BASE_EDGE"))
			local nobj = 0
			pcall(function() nobj = #C.list(be.objects) end)
			local normal = true
			pcall(function() normal = be.type == nil or be.type == api.type.enum.BaseEdgeType.NORMAL end)
			if be and nobj == 0 and (normal or allowEnds) and be.position0 and be.tangent0 and freeStreet(e, be) then
				for k = 0, 64 do
					local t = k / 64
					local px, py, pz, dx, dy, dz = hermite3(be.position0, be.position1, be.tangent0, be.tangent1, t)
					local d = math.sqrt((px - x) ^ 2 + (py - y) ^ 2)
					if d <= r and (best == nil or d < best.d) and (allowEnds or (t > 0.02 and t < 0.98)) then
						best = { e = e, be = be, t = t, x = px, y = py, z = pz, dx = dx, dy = dy, dz = dz, d = d }
					end
				end
			end
		end
	end)
	return best
end

--- arc length of a point projected on the polyline, and its distance
local function projectOn(pts, x, y)
	local acc, best, bs = 0, math.huge, 0
	for i = 1, #pts - 1 do
		local ax, ay, bx, by = pts[i][1], pts[i][2], pts[i + 1][1], pts[i + 1][2]
		local dx, dy = bx - ax, by - ay
		local L2 = dx * dx + dy * dy
		local t = L2 > 0 and math.max(0, math.min(1, ((x - ax) * dx + (y - ay) * dy) / L2)) or 0
		local d = math.sqrt((ax + t * dx - x) ^ 2 + (ay + t * dy - y) ^ 2)
		if d < best then best, bs = d, acc + t * math.sqrt(L2) end
		acc = acc + math.sqrt(L2)
	end
	return bs, best, acc
end

local function pointAt(pts, s)
	local acc = 0
	for i = 1, #pts - 1 do
		local ax, ay, bx, by = pts[i][1], pts[i][2], pts[i + 1][1], pts[i + 1][2]
		local L = math.sqrt((bx - ax) ^ 2 + (by - ay) ^ 2)
		if acc + L >= s or i == #pts - 1 then
			local t = L > 0 and math.max(0, math.min(1, (s - acc) / L)) or 0
			return ax + (bx - ax) * t, ay + (by - ay) * t
		end
		acc = acc + L
	end
	return pts[#pts][1], pts[#pts][2]
end

--- BASE_NODE_CONFIG of a node or nil (its stored lane connections name the node's edges)
local function nodeConfigOf(n)
	if type(n) ~= "number" or n < 0 then return nil end
	local cfg = nil
	pcall(function() cfg = api.engine.getComponent(n, api.type.ComponentType.BASE_NODE_CONFIG) end)
	return cfg
end

--- log a node's lane connections (compare a joint built by the mod with one drawn by hand)
function B.dumpNodeConfig(tag, n)
	pcall(function()
		local cfg = nodeConfigOf(n)
		if not cfg then
			print(string.format("[TF3P] nodecfg %s node %s: NO BASE_NODE_CONFIG", tostring(tag), tostring(n)))
			return
		end
		local parts = {}
		for _, lc in ipairs(C.list(cfg.laneConnections)) do
			parts[#parts + 1] = string.format("%s:%s>%s:%s", tostring(lc.segment0), tostring(lc.lane0),
				tostring(lc.segment1), tostring(lc.lane1))
		end
		print(string.format("[TF3P] nodecfg %s node %s conns=%d [%s]", tostring(tag), tostring(n), #parts,
			table.concat(parts, " ")))
	end)
end

--- copy of a node's config with segment ids remapped (map[old] = new), for nodeConfigsToAdd
local function remappedNodeConfig(n, map)
	local cfg = nodeConfigOf(n)
	if not cfg then return nil end
	local ok, ce = pcall(function()
		local lcs = {}
		for _, lc in ipairs(C.list(cfg.laneConnections)) do
			if map[lc.segment0] then lc.segment0 = map[lc.segment0] end
			if map[lc.segment1] then lc.segment1 = map[lc.segment1] end
			lcs[#lcs + 1] = lc
		end
		cfg.laneConnections = lcs
		local cw = {}
		for _, x in ipairs(C.list(cfg.crosswalks)) do cw[#cw + 1] = map[x] or x end
		cfg.crosswalks = cw
		local e = api.type.BaseNodeLaneConnectionAndEntity.new()
		e.entity = n
		e.comp = cfg
		return e
	end)
	return ok and ce or nil
end

--- Put a node at (x, y) on an existing free street edge passing within r: the edge is replaced by two
--- halves meeting there (whole component copied), its end nodes get their lane connections rebuilt.
--- Adds to sp.streetProposal; returns the new node position or nil (no edge / a node already there).
function B.nearStreetPoint(x, y, r)
	if nearNode(x, y, 2.5, nil, false) then return nil end         -- a node is already there
	local ed = nearEdge(x, y, r)
	if not ed then return nil end
	-- refine the curve parameter around the coarse hit (64 samples per edge are ~1.5 m apart)
	local be = ed.be
	local lo, hi = math.max(0.0, ed.t - 1 / 64), math.min(1.0, ed.t + 1 / 64)
	for k = 0, 40 do
		local tt = lo + (hi - lo) * k / 40
		local px, py, pz, dx, dy, dz = hermite3(be.position0, be.position1, be.tangent0, be.tangent1, tt)
		local d = math.sqrt((px - x) ^ 2 + (py - y) ^ 2)
		if d < ed.d and tt > 0.02 and tt < 0.98 then
			ed.t, ed.x, ed.y, ed.z, ed.dx, ed.dy, ed.dz, ed.d = tt, px, py, pz, dx, dy, dz, d
		end
	end
	return ed
end

function B.splitStreetAt(sp, ed)
	if not ed then return nil end
	local enum = api.type.enum
	local be, t = ed.be, ed.t
	local nodeId = -2000
	local na = api.type.NodeAndEntity.new()
	na.entity = nodeId
	na.comp.position = api.type.Vec3f.new(ed.x, ed.y, ed.z)
	local edges, nextEdge = {}, -2001
	local function half(n0, n1, p0, p1, tg0, tg1)
		local e = api.type.SegmentAndEntity.new()
		e.entity = nextEdge
		nextEdge = nextEdge - 1
		e.type = 0
		pcall(function()
			local c = api.engine.getComponent(ed.e, C.CT("BASE_EDGE"))
			pcall(function() c = c:clone() end)
			e.comp = c
		end)
		e.comp.node0, e.comp.node1 = n0, n1
		e.comp.position0 = api.type.Vec3f.new(p0.x, p0.y, p0.z)
		e.comp.position1 = api.type.Vec3f.new(p1.x, p1.y, p1.z)
		e.comp.tangent0 = api.type.Vec3f.new(tg0.x, tg0.y, tg0.z)
		e.comp.tangent1 = api.type.Vec3f.new(tg1.x, tg1.y, tg1.z)
		e.comp.type, e.comp.typeIndex = be.type, be.typeIndex
		e.comp.laneConfigs, e.comp.roadTemplate = be.laneConfigs, be.roadTemplate
		e.comp.roadStyle, e.comp.roadType = be.roadStyle, be.roadType
		pcall(function() e.comp.objects = {} end)
		pcall(function()
			local st = api.engine.getComponent(ed.e, C.CT("BASE_EDGE_STREET"))
			if st then e.streetEdge = st end
		end)
		pcall(function()
			local po = api.engine.getComponent(ed.e, C.CT("PLAYER_OWNED"))
			if po then e.playerOwned = po end
		end)
		edges[#edges + 1] = e
	end
	local P = { x = ed.x, y = ed.y, z = ed.z }
	local D = { x = ed.dx, y = ed.dy, z = ed.dz }
	local function sc(v, k) return { x = v.x * k, y = v.y * k, z = v.z * k } end
	half(be.node0, nodeId, be.position0, P, sc(be.tangent0, t), sc(D, t))
	half(nodeId, be.node1, P, be.position1, sc(D, 1 - t), sc(be.tangent1, 1 - t))
	sp.streetProposal.nodesToAdd = { na }
	sp.streetProposal.edgesToAdd = edges
	sp.streetProposal.edgesToRemove = { ed.e }
	local cfg = {}
	for _, n in ipairs({ be.node0, be.node1 }) do
		if nodeConfigOf(n) then cfg[#cfg + 1] = n end
	end
	if #cfg > 0 then sp.streetProposal.nodeConfigsToRemove = cfg end
	return P
end

B.nearNode = nearNode

--- a short street edge between two EXISTING nodes a, b ({e, x, y, z}); their lane connections are
--- rebuilt by the engine (nodeConfigsToRemove). tplName: the street type (nil: the road's own)
function B.linkProposal(a, bnode, tplName)
	local enum = api.type.enum
	tplName = tplName or "::/infrastructure/street/country/country_old_small.street_template"
	local tpl = api.res.streetTemplateRep.get(api.res.streetTemplateRep.find(tplName))
	local L = math.sqrt((bnode.x - a.x) ^ 2 + (bnode.y - a.y) ^ 2)
	if L < 0.5 then return nil, T("узлы совпадают") end
	local ux, uy = (bnode.x - a.x) / L, (bnode.y - a.y) / L
	local e = api.type.SegmentAndEntity.new()
	e.entity = -1
	e.type = 0
	e.comp.node0, e.comp.node1 = a.e, bnode.e
	e.comp.position0 = api.type.Vec3f.new(a.x, a.y, a.z)
	e.comp.position1 = api.type.Vec3f.new(bnode.x, bnode.y, bnode.z)
	e.comp.tangent0 = api.type.Vec3f.new(ux * L, uy * L, bnode.z - a.z)
	e.comp.tangent1 = api.type.Vec3f.new(ux * L, uy * L, bnode.z - a.z)
	e.comp.type = enum.BaseEdgeType.NORMAL
	e.comp.typeIndex = -1
	e.comp.laneConfigs = tpl.laneConfigs
	e.comp.roadTemplate = tplName
	e.comp.roadStyle = tpl.streetStyle
	e.comp.roadType = enum.RoadType.STREET
	local sp = api.type.SimpleProposal.new()
	sp.streetProposal.edgesToAdd = { e }
	local cfg = {}
	for _, n in ipairs({ a.e, bnode.e }) do if nodeConfigOf(n) then cfg[#cfg + 1] = n end end
	if #cfg > 0 then sp.streetProposal.nodeConfigsToRemove = cfg end
	return sp
end

--- the street proposal of segment si of project pr (nil, reason when not buildable)
--- opt (all on by default): split = cut existing edges at the ends, snap = use existing nodes at the
--- ends, bridges = bridge/tunnel edges, stations = pass through the entrance nodes of built stations
function B.segmentProposal(pr, si, removes, opt)
	opt = opt or {}
	local useSplit, useSnap = opt.split ~= false, opt.snap ~= false
	local useBridges, useStations = opt.bridges ~= false, opt.stations ~= false
	-- existing nodes the road joins keep stored lane connections without the new edge (red "no entry"
	-- at the joint): "remove" drops them so the engine regenerates defaults; "remap" for cut edges
	local nodeCfgMode = opt.nodeCfg
	if nodeCfgMode == nil then nodeCfgMode = "remove" end
	local sg = list(pr.segments)[si]
	if not sg or sg.t ~= "new" or sg.k ~= "road" then return nil, T("этот участок мод не строит") end
	local tplName = B.resolveRes(sg.way, true)
	if not tplName then return nil, T("тип дороги не найден в игре: ") .. tostring(sg.way) end
	local tpl = api.res.streetTemplateRep.get(api.res.streetTemplateRep.find(tplName))
	local pts = pairsXY(sg.p)
	if #pts < 2 then return nil, T("у участка нет линии") end
	local _, _, total = projectOn(pts, pts[1][1], pts[1][2])
	-- where the drawn line runs on an existing road at its ends, the new road stops where it meets it
	-- (with split: joined there; without: a few metres before, so it does not overlap the old road)
	local function onRoad(s)
		local x, y = pointAt(pts, s)
		return nearEdge(x, y, B.ROAD_ON_EXISTING_M, true) ~= nil
	end
	local s0, s1 = 0, total
	if onRoad(total) then
		local s = total
		while s > 8 and onRoad(s - 4) do s = s - 4 end
		s1 = useSplit and s or math.max(8, s - 8)
	end
	if onRoad(0) then
		local s = 0
		while s < s1 - 8 and onRoad(s + 4) do s = s + 4 end
		s0 = useSplit and s or math.min(s1 - 8, s + 8)
	end
	if s0 > 0 or s1 < total then
		local cut = {}
		local x0, y0 = pointAt(pts, s0)
		cut[1] = { x0, y0 }
		local acc0 = 0
		for i = 1, #pts - 1 do
			local L = math.sqrt((pts[i + 1][1] - pts[i][1]) ^ 2 + (pts[i + 1][2] - pts[i][2]) ^ 2)
			acc0 = acc0 + L
			if acc0 > s0 + 0.5 and acc0 < s1 - 0.5 then cut[#cut + 1] = pts[i + 1] end
		end
		local x1, y1 = pointAt(pts, s1)
		cut[#cut + 1] = { x1, y1 }
		pts = cut
		_, _, total = projectOn(pts, pts[1][1], pts[1][2])
	end
	-- nodes along the line: vertices, long pieces split, span ends and station entrances forced
	local marks = {}
	local acc = 0
	marks[#marks + 1] = { s = 0 }
	for i = 1, #pts - 1 do
		local L = math.sqrt((pts[i + 1][1] - pts[i][1]) ^ 2 + (pts[i + 1][2] - pts[i][2]) ^ 2)
		local n = math.max(1, math.ceil(L / B.ROAD_MAX_EDGE_M))
		for k = 1, n do marks[#marks + 1] = { s = acc + L * k / n } end
		acc = acc + L
	end
	-- spans are measured on the untrimmed line: shift by the start trim, clamp, drop what fell off
	local spans = {}
	for _, sp in ipairs(useBridges and list(pr.spans) or {}) do
		if sp.seg == si and sp.d0 and sp.d1 then
			local d0 = math.max(0, sp.d0 - s0)
			local d1 = math.min(total, sp.d1 - s0)
			if d1 - d0 >= 1 then
				spans[#spans + 1] = { t = sp.t, key = sp.key, d0 = d0, d1 = d1 }
				if d0 > 0 then marks[#marks + 1] = { s = d0, forced = true } end
				if d1 < total then marks[#marks + 1] = { s = d1, forced = true } end
			end
		end
	end
	local linkUsed = {}
	-- planned stations on this road: a node where their entrance meets it; a station already built
	-- there has its entrance end node at that point: the road passes THROUGH that node (the game then
	-- has them connected, as when a road is drawn to a station by hand)
	for _, it in ipairs(list(pr.items)) do
		local b = it.build
		local acc4 = it.access and list(it.access) or nil
		local linked = type(b) == "table" and (b.kind == "street_terminal" or b.res == "road_depot.con")
		if linked and acc4 and acc4[4] then
			local s, d = projectOn(pts, acc4[3], acc4[4])
			if d < 2 then
				local nd = useStations and nearNode(acc4[3], acc4[4], B.STATION_SNAP_NODE_M + 2) or nil
				if nd and linkUsed[nd.e] then nd = nil end
				if nd then linkUsed[nd.e] = true end
				marks[#marks + 1] = { s = s, forced = true, link = nd }
			end
		end
	end
	table.sort(marks, function(a, b) return a.s < b.s end)
	local nodes = {}
	for _, m in ipairs(marks) do
		local last = nodes[#nodes]
		if last and m.s - last.s < B.ROAD_MIN_EDGE_M then
			if m.forced and not last.forced and #nodes > 1 then nodes[#nodes] = m
			elseif m.link and not last.link then last.link = m.link end
		else
			nodes[#nodes + 1] = m
		end
	end
	local lastN = nodes[#nodes]
	if total - lastN.s < B.ROAD_MIN_EDGE_M and #nodes > 1 and not lastN.forced then
		lastN.s = total                              -- move the last plain node to the end
	elseif total - lastN.s >= 0.5 then
		nodes[#nodes + 1] = { s = total }
	else
		lastN.s = total
	end
	local function inSpan(s)
		for _, sp in ipairs(spans) do
			if s > sp.d0 + 0.1 and s < sp.d1 - 0.1 then return sp end
		end
		return nil
	end
	for _, nd in ipairs(nodes) do
		nd.x, nd.y = pointAt(pts, nd.s)
		nd.z = terrainZ(nd.x, nd.y)
		if nd.link then
			nd.entity, nd.x, nd.y, nd.z = nd.link.e, nd.link.x, nd.link.y, nd.link.z
		end
	end
	-- bridge decks: straight between the abutments (the engine adds the pillars)
	for _, sp in ipairs(spans) do
		local ax_, ay_ = pointAt(pts, sp.d0)
		local bx_, by_ = pointAt(pts, sp.d1)
		local za, zb = terrainZ(ax_, ay_), terrainZ(bx_, by_)
		local floor = sp.t ~= "tunnel" and (B.WATER_LEVEL + B.BRIDGE_DECK_ABOVE_WATER) or -math.huge
		za, zb = math.max(za, floor), math.max(zb, floor)
		for _, nd in ipairs(nodes) do
			if nd.s >= sp.d0 - 0.5 and nd.s <= sp.d1 + 0.5 then
				local f = math.max(0, math.min(1, (nd.s - sp.d0) / math.max(1, sp.d1 - sp.d0)))
				nd.z = za + (zb - za) * f
			elseif sp.t ~= "tunnel" then
				-- approach ramp on land: never steeper than RAMP_SLOPE down from the deck end
				local dist = nd.s < sp.d0 and (sp.d0 - nd.s) or (nd.s - sp.d1)
				local zEnd = nd.s < sp.d0 and za or zb
				nd.z = math.max(nd.z, zEnd - dist * B.RAMP_SLOPE)
			end
		end
	end
	-- ends: an existing node nearby is that end; else an existing edge nearby is split there
	local endInfo, splits = {}, {}
	local used = {}
	for _, nd in ipairs(nodes) do if nd.entity then used[nd.entity] = true end end
	for _, k in ipairs({ 1, #nodes }) do
		local nd = nodes[k]
		local ex = (useSnap and not nd.entity and #nodes > 1) and nearNode(nd.x, nd.y, B.ROAD_SNAP_NODE_M, used, true) or nil
		if ex then
			-- the snap node must lie beyond the neighbour node (else the curve would turn back)
			local se = projectOn(pts, ex.x, ex.y)
			local nb = nodes[k == 1 and 2 or #nodes - 1]
			if (k == 1 and se >= nb.s - 1) or (k ~= 1 and se <= nb.s + 1) then ex = nil end
		end
		if ex then used[ex.e] = true end
		if nd.entity then
			-- already a station entrance node
		elseif ex then
			nd.entity, nd.x, nd.y, nd.z = ex.e, ex.x, ex.y, ex.z
		else
			local ed = useSplit and nearEdge(nd.x, nd.y, B.ROAD_SPLIT_EDGE_M) or nil
			if ed and not splits[ed.e] then
				nd.x, nd.y, nd.z = ed.x, ed.y, ed.z
				nd.split = ed
				splits[ed.e] = true
			else
				endInfo[#endInfo + 1] = k == 1 and T("начало") or T("конец")
			end
		end
	end
	-- height profile: terrain heights at nodes up to 80 m apart can be steeper than a street allows
	-- (the game: "Слишком большой наклон"); keep existing nodes and bridge decks, limit the rest
	do
		local g = B.ROAD_MAX_GRADE
		local fixed = {}
		for i, nd in ipairs(nodes) do
			fixed[i] = (nd.entity ~= nil and nd.entity >= 0) or inSpan(nd.s) ~= nil
		end
		local function dist(i, j)
			return math.sqrt((nodes[i].x - nodes[j].x) ^ 2 + (nodes[i].y - nodes[j].y) ^ 2)
		end
		for _ = 1, 6 do
			for i = 2, #nodes do
				if not fixed[i] then
					local d = dist(i, i - 1) * g
					nodes[i].z = math.max(nodes[i - 1].z - d, math.min(nodes[i - 1].z + d, nodes[i].z))
				end
			end
			for i = #nodes - 1, 1, -1 do
				if not fixed[i] then
					local d = dist(i, i + 1) * g
					nodes[i].z = math.max(nodes[i + 1].z - d, math.min(nodes[i + 1].z + d, nodes[i].z))
				end
			end
		end
		local worst, wi = 0, nil
		for i = 1, #nodes - 1 do
			local L = dist(i, i + 1)
			if L > 0.5 then
				local gr = math.abs(nodes[i + 1].z - nodes[i].z) / L
				if gr > worst then worst, wi = gr, i end
			end
		end
		S.lastGrade = worst
		if wi then
			pcall(print, string.format("[TF3P] road profile: max grade %.1f %% between (%.0f,%.0f,%.1f) and (%.0f,%.0f,%.1f)",
				worst * 100, nodes[wi].x, nodes[wi].y, nodes[wi].z, nodes[wi + 1].x, nodes[wi + 1].y, nodes[wi + 1].z))
		end
	end
	-- last guard: no edge from a node to itself, none shorter than the minimum
	do
		local kept = { nodes[1] }
		for i = 2, #nodes do
			local a, b = kept[#kept], nodes[i]
			local same = a.entity ~= nil and a.entity == b.entity
			local short = math.sqrt((b.x - a.x) ^ 2 + (b.y - a.y) ^ 2) < 1.0
			if same or short then
				if b.entity and not a.entity then kept[#kept] = b end   -- keep the existing node
			else
				kept[#kept + 1] = b
			end
		end
		nodes = kept
	end
	if #nodes < 2 then return nil, T("участок слишком короткий (концы совпадают с существующей дорогой)") end
	local enum = api.type.enum
	-- bridge / tunnel types: the plan's if it allows roads, else what the game's road tool would pick
	local ROAD = 0
	pcall(function() ROAD = enum.Carrier.ROAD end)
	local structIds = {}
	for _, spn in ipairs(spans) do
		local tunnel = spn.t == "tunnel"
		local rep = tunnel and api.res.tunnelTypeRep or api.res.bridgeTypeRep
		local k = tunnel and "tunnel" or "bridge"
		if structIds[k] == nil then
			local id = spn.key and repId(rep, spn.key) or nil
			if id and not structureFits(rep, id, ROAD) then id = nil end
			id = id or B.autoStructureType(ROAD, B.templateSpeed(tpl), gameYear(), tunnel)
			if id == nil then return nil, (tunnel and T("нет доступного типа тоннеля") or T("нет доступного типа моста")) .. T(" для дорог в этом году") end
			structIds[k] = id
		end
	end
	local sp = api.type.SimpleProposal.new()
	local addNodes, addEdges = {}, {}
	local removeEdges = {}
	local cfgRemove, cfgAdd, cfgSeen = {}, {}, {}
	local function touchNode(n, remap)
		if nodeCfgMode == false or type(n) ~= "number" or n < 0 or cfgSeen[n] then return end
		cfgSeen[n] = true
		if not nodeConfigOf(n) then return end
		cfgRemove[#cfgRemove + 1] = n
		if remap and nodeCfgMode == "remap" then
			local ce = remappedNodeConfig(n, remap)
			if ce then cfgAdd[#cfgAdd + 1] = ce end
		end
	end
	local nextNode = -1000
	for _, nd in ipairs(nodes) do
		if not nd.entity then
			nd.entity = nextNode
			nextNode = nextNode - 1
			local na = api.type.NodeAndEntity.new()
			na.entity = nd.entity
			na.comp.position = api.type.Vec3f.new(nd.x, nd.y, nd.z)
			addNodes[#addNodes + 1] = na
		end
	end
	local function dirAt(i)
		local a = nodes[math.max(1, i - 1)]
		local b = nodes[math.min(#nodes, i + 1)]
		local dx, dy = b.x - a.x, b.y - a.y
		local L = math.sqrt(dx * dx + dy * dy)
		if L < 1e-6 then return 1, 0 end
		return dx / L, dy / L
	end
	local nextEdge = -1
	local function addEdge(a, b, d0x, d0y, d1x, d1y, span)
		local L = math.sqrt((b.x - a.x) ^ 2 + (b.y - a.y) ^ 2)
		local e = api.type.SegmentAndEntity.new()
		e.entity = nextEdge
		nextEdge = nextEdge - 1
		e.type = 0
		e.comp.node0 = a.entity
		e.comp.node1 = b.entity
		e.comp.position0 = api.type.Vec3f.new(a.x, a.y, a.z)
		e.comp.position1 = api.type.Vec3f.new(b.x, b.y, b.z)
		e.comp.tangent0 = api.type.Vec3f.new(d0x * L, d0y * L, b.z - a.z)
		e.comp.tangent1 = api.type.Vec3f.new(d1x * L, d1y * L, b.z - a.z)
		if span and span.t == "tunnel" then
			e.comp.type = enum.BaseEdgeType.TUNNEL
			e.comp.typeIndex = structIds.tunnel
		elseif span then
			e.comp.type = enum.BaseEdgeType.BRIDGE
			e.comp.typeIndex = structIds.bridge
		else
			e.comp.type = enum.BaseEdgeType.NORMAL
			e.comp.typeIndex = -1
		end
		e.comp.laneConfigs = tpl.laneConfigs
		e.comp.roadTemplate = tplName
		e.comp.roadStyle = tpl.streetStyle
		e.comp.roadType = enum.RoadType.STREET
		pcall(function()                              -- owned by the player, like roads drawn by hand
			local po = e.playerOwned
			if po ~= nil then po.player = api.engine.util.getPlayer() e.playerOwned = po end
		end)
		addEdges[#addEdges + 1] = e
	end
	for i = 1, #nodes - 1 do
		local a, b = nodes[i], nodes[i + 1]
		local d0x, d0y = dirAt(i)
		local d1x, d1y = dirAt(i + 1)
		addEdge(a, b, d0x, d0y, d1x, d1y, inSpan((a.s + b.s) / 2))
	end
	for _, nd in ipairs(nodes) do
		if nd.entity and nd.entity >= 0 then touchNode(nd.entity, nil) end
	end
	-- split the existing edges our ends join: old edge out, two halves in (same type, template, lanes)
	for _, nd in ipairs(nodes) do
		local ed = nd.split
		if ed then
			local be = ed.be
			local t = ed.t
			local function half(n0, n1, p0, p1, tg0, tg1)
				local e = api.type.SegmentAndEntity.new()
				e.entity = nextEdge
				nextEdge = nextEdge - 1
				e.type = (be.roadType == enum.RoadType.TRACK) and 1 or 0
				-- the whole component of the old edge (as the game's electrify.tl does), then new geometry
				pcall(function()
					local c = api.engine.getComponent(ed.e, C.CT("BASE_EDGE"))
					pcall(function() c = c:clone() end)       -- an own copy per half
					e.comp = c
				end)
				e.comp.node0, e.comp.node1 = n0, n1
				e.comp.position0 = api.type.Vec3f.new(p0.x, p0.y, p0.z)
				e.comp.position1 = api.type.Vec3f.new(p1.x, p1.y, p1.z)
				e.comp.tangent0 = api.type.Vec3f.new(tg0.x, tg0.y, tg0.z)
				e.comp.tangent1 = api.type.Vec3f.new(tg1.x, tg1.y, tg1.z)
				e.comp.type = be.type
				e.comp.typeIndex = be.typeIndex
				e.comp.laneConfigs = be.laneConfigs
				e.comp.roadTemplate = be.roadTemplate
				e.comp.roadStyle = be.roadStyle
				e.comp.roadType = be.roadType
				pcall(function() e.comp.objects = {} end)
				pcall(function()
					local st = api.engine.getComponent(ed.e, C.CT("BASE_EDGE_STREET"))
					if st then e.streetEdge = st end
				end)
				pcall(function()
					local po = api.engine.getComponent(ed.e, C.CT("PLAYER_OWNED"))
					if po then e.playerOwned = po end
				end)
				addEdges[#addEdges + 1] = e
				return e.entity
			end
			local P = { x = ed.x, y = ed.y, z = ed.z }
			local D = { x = ed.dx, y = ed.dy, z = ed.dz }
			local function sc(v, k) return { x = v.x * k, y = v.y * k, z = v.z * k } end
			local hA = half(be.node0, nd.entity, be.position0, P, sc(be.tangent0, t), sc(D, t))
			local hB = half(nd.entity, be.node1, P, be.position1, sc(D, 1 - t), sc(be.tangent1, 1 - t))
			removeEdges[#removeEdges + 1] = ed.e
			-- the end nodes of the cut edge still name it in their lane connections
			touchNode(be.node0, { [ed.e] = hA })
			touchNode(be.node1, { [ed.e] = hB })
		end
	end
	local joined = 0
	for _, nd in ipairs(nodes) do
		if nd.link then joined = joined + 1 end
	end
	sp.streetProposal.nodesToAdd = addNodes
	sp.streetProposal.edgesToAdd = addEdges
	if #removeEdges > 0 then sp.streetProposal.edgesToRemove = removeEdges end
	if #cfgRemove > 0 then sp.streetProposal.nodeConfigsToRemove = cfgRemove end
	if #cfgAdd > 0 then sp.streetProposal.nodeConfigsToAdd = cfgAdd end
	S.lastTouched = cfgRemove
	if removes and #removes > 0 then sp.constructionsToRemove = removes end
	local nsplit = #removeEdges
	local info = string.format(T("рёбер %d, узлов %d, макс. уклон %.0f %%%s%s%s%s"), #addEdges, #addNodes,
		(S.lastGrade or 0) * 100,
		#cfgRemove > 0 and string.format(T(", пересобраны соединения полос в узлах: %d"), #cfgRemove) or "",
		nsplit > 0 and string.format(T(", примыкание к существующей дороге: %d"), nsplit) or "",
		joined > 0 and string.format(T(", подключено въездов станций: %d"), joined) or "",
		#endInfo > 0 and (T(", не примыкает к существующей дороге: ") .. table.concat(endInfo, T(" и "))) or "")
	return sp, nil, info
end


--- what step n of the current project builds: pr, kind ("item"|"segment"), target
local function stepTarget(n)
	local cur = B.current()
	local pr = cur and B.project(cur.id)
	local e = list(pr and pr.checklist)[n]
	local ref = e and type(e.ref) == "table" and e.ref or nil
	if ref and ref.item then
		local it = list(pr.items)[ref.item]
		if it and type(it.build) == "table" and (it.build.kind == "street_terminal" or it.build.kind == "construction") then
			return pr, "item", it
		end
	elseif e and e.kind == "segment" and ref and ref.seg then
		local sg = list(pr.segments)[ref.seg]
		if sg and sg.t == "new" and sg.k == "road" then return pr, "segment", ref.seg end
	end
	return pr, nil, nil
end

function B.stepProposal(pr, kind, target, off, removes, opt)
	if kind == "item" then return B.itemProposal(target, off, removes) end
	if kind == "segment" then return B.segmentProposal(pr, target, removes, opt) end
	return nil, T("у этого шага нет постройки")
end

--- dry run: try the offsets (buildings only), keep the first one the game accepts; S.buildCheck = result
--- a deferred build body that cannot leave the panel stuck on «Проверяю…» / «Строю…»
local function guarded(n, id, fn)
	return function()
		local ok, err = pcall(fn)
		if not ok then
			pcall(print, "[TF3P] build step " .. tostring(n) .. " error: " .. tostring(err))
			S.buildCheck = { n = n, id = id, state = "error", text = T("ошибка мода: ") .. tostring(err) }
			B.version = (B.version or 0) + 1
		end
	end
end

function B.buildCheck(n)
	local cur = B.current()
	if not cur then return end
	local pr, kind, target = stepTarget(n)
	S.buildCheck = { n = n, id = cur.id, state = "checking" }
	B.version = (B.version or 0) + 1
	B.defer(guarded(n, cur.id, function()
		local res = { n = n, id = cur.id, kind = kind }
		if not kind then
			res.state, res.text = "error", T("у этого шага нет постройки")
		else
			local tried = {}
			-- buildings: a few offsets; roads: the full road first, then simpler variants (each failing
			-- stage is logged, so what the game refuses is known)
			local attempts = {}
			if kind == "item" then
				for _, off in ipairs(B.BUILD_OFFSETS) do attempts[#attempts + 1] = { off = off } end
			else
				attempts = {
					{ off = 0, opt = {}, name = nil },
					{ off = 0, opt = { nodeCfg = "remap" }, name = T("соединения полос перенесены") },
					{ off = 0, opt = { nodeCfg = false }, name = T("без пересборки соединений полос (проверьте стыки)") },
					{ off = 0, opt = { split = false }, name = T("без врезки в существующие дороги") },
					{ off = 0, opt = { split = false, snap = false }, name = T("концы не подключены к дорогам") },
					{ off = 0, opt = { split = false, snap = false, stations = false }, name = T("без узлов станций") },
					{ off = 0, opt = { split = false, snap = false, stations = false, bridges = false }, name = T("без мостов") },
				}
			end
			for _, at in ipairs(attempts) do
				local off = at.off
				local sp, why, info, place = B.stepProposal(pr, kind, target, off, nil, at.opt)
				local joint = place and place.road and place.road.edge or nil   -- the road needs a node there
				local ignore = nil
				if not sp then res.state, res.text = "error", why break end
				local ok, pd = pcall(function() return B.dryRun(sp) end)
				if not ok or pd == nil then
					pcall(print, "[TF3P] dry run step " .. tostring(n) .. " stage " .. tostring(at.name or "full")
						.. ": exception " .. tostring(pd))
					tried[#tried + 1] = T("игра не рассчитала: ") .. tostring(pd):sub(1, 120)
					goto continue
				end
				local errs, critical = errorsOf(pd)
				local houses, others = collisionsOf(pd, ignore)
				if others == 0 and #houses > 0 then
					-- only town houses in the way: demolish them in the same proposal (paid, as by hand)
					local sp2 = B.stepProposal(pr, kind, target, off, houses, at.opt)
					local ok2, pd2 = pcall(function() return B.dryRun(sp2) end)
					if ok2 and pd2 ~= nil then
						pd = pd2
						errs, critical = errorsOf(pd2)
						local h2, o2 = collisionsOf(pd2, ignore)
						others = o2 + #h2
					end
				end
				if joint and others == 0 and not critical then
					-- the only collision was the road that gets the node: drop that message, then check
					-- the node step on its own
					local kept = {}
					for _, m in ipairs(errs) do
						if not (m:find("толкнов", 1, true) or m:lower():find("collision", 1, true)) then kept[#kept + 1] = m end
					end
					errs = kept
					local spN = api.type.SimpleProposal.new()
					local okN, P = pcall(B.splitStreetAt, spN, joint)
					local okD, pdN = false, nil
					if okN and P then okD, pdN = pcall(function() return B.dryRun(spN) end) end
					local eN, cN = {}, true
					local oN = 1
					if okD and pdN then
						eN, cN = errorsOf(pdN)
						local hN
						hN, oN = collisionsOf(pdN)
						oN = oN + #hN
					end
					pcall(print, string.format("[TF3P] joint node check: ok=%s critical=%s others=%d errors=%s",
						tostring(okD), tostring(cN), oN, table.concat(eN, "; ")))
					if not okD or cN or oN > 0 or #eN > 0 then
						errs[#errs + 1] = T("узел на дороге: ") .. (#eN > 0 and table.concat(eN, "; ") or T("не рассчитан"))
					end
				end
				local cost = nil
				pcall(function() cost = pd.costs end)
				pcall(print, string.format("[TF3P] dry run step %s (%s) off [%s]: critical=%s houses=%d others=%d cost=%s %s errors=%s",
					tostring(n), tostring(kind), offText(off), tostring(critical), #houses, others, tostring(cost), tostring(info or ""),
					table.concat(errs, "; ")))
				if not critical and others == 0 and #errs == 0 then
					res.state, res.off, res.cost, res.removes, res.opt = "ok", off, cost, houses, at.opt
					res.place = place
					if place and place.road then
						info = (info and (info .. "; ") or "")
							.. (joint and T("в 3 шага: узел на дороге, постройка, подъезд к дороге") or T("в 2 шага: постройка, подъезд к дороге"))
					end
					if at.name then info = (info and (info .. "; ") or "") .. T("упрощено: ") .. at.name end
					res.text = string.format(T("%s%s%s. Стоимость: $%s%s"),
						kind == "segment" and T("Дорогу можно строить") or T("Место свободно"),
						#houses > 0 and string.format(T(", под снос %d дом(а) города (снос входит в стоимость, репутация города снизится)"), #houses) or "",
						offText(off) ~= "" and (T(" (сдвиг: ") .. offText(off) .. ")") or "",
						cost and string.format("%.0f", cost) or "?",
						info and (" (" .. info .. ")") or "")
					break
				end
				tried[#tried + 1] = table.concat(errs, "; ") .. (others > 0 and string.format(T(" (препятствий: %d)"), others) or "")
				::continue::
			end
			if res.state == nil then
				res.state = "error"
				res.text = (kind == "segment" and T("Дорогу так не построить: ") or T("Место занято или недоступно: ")) .. (tried[1] or "?")
			end
		end
		pcall(print, "[TF3P] build check step " .. tostring(n) .. ": " .. tostring(res.text))
		S.buildCheck = res
		B.version = (B.version or 0) + 1
	end))
end

--- the real build of the last successful check
function B.buildConfirm()
	local chk = S.buildCheck
	local cur = B.current()
	if not chk or chk.state ~= "ok" or not cur or chk.id ~= cur.id then return end
	local pr, kind, target = stepTarget(chk.n)
	if not kind then return end
	S.buildCheck = { n = chk.n, id = cur.id, state = "building", text = T("Строю…") }
	B.version = (B.version or 0) + 1
	B.defer(guarded(chk.n, cur.id, function()
		local place = chk.place
		if kind == "item" and place and place.road then
			local road = place.road
			local function done(state, text)
				pcall(print, "[TF3P] build step " .. tostring(chk.n) .. ": " .. text)
				S.buildCheck = { n = chk.n, id = cur.id, state = state, text = text }
				S.sig = nil
				B.version = (B.version or 0) + 1
			end
			local function send(sp, cb)
				api.cmd.sendCommand(api.cmd.makeWorldBuildProposalCmd(sp, B.playerContext(false), false, true), cb)
			end
			local function step3()
				-- the link: building's stub node -> road node (both exist now)
				local sx, sy = place.tx - place.n[1] * place.snap, place.ty - place.n[2] * place.snap
				local sn = B.nearNode(sx, sy, 3, nil, false)
				local rn = road.node and { e = road.node, x = road.x, y = road.y, z = road.z }
					or B.nearNode(road.x, road.y, 2, nil, true)
				if not sn or not rn then
					return done("error", T("Постройка стоит, но подъезд не построен: не найден узел ") .. (sn and T("дороги") or T("выезда постройки"))
						.. T(" — проведите 6 м дороги от выезда до дороги вручную"))
				end
				local tplName = road.edge and road.edge.be and road.edge.be.roadTemplate or nil
				local spL, why = B.linkProposal(sn, rn, tplName)
				if not spL then return done("error", T("подъезд не собран: ") .. tostring(why)) end
				send(spL, function(_, okL)
					done(okL and "done" or "error", okL and T("Построено: постройка и подъезд к дороге")
						or T("Постройка стоит, но подъезд игра не приняла — проведите 6 м дороги от выезда до дороги вручную"))
				end)
			end
			local function step2()
				local sp2 = B.itemProposal(target, chk.off, chk.removes, { tx = place.tx, ty = place.ty, tz = place.tz, road = road })
				send(sp2, function(_, ok2)
					if not ok2 then return done("error", T("Игра не приняла постройку")) end
					step3()
				end)
			end
			place.n = list(target.build.n)
			if road.edge then
				local spN = api.type.SimpleProposal.new()
				if not B.splitStreetAt(spN, road.edge) then error(T("узел на дороге не собран")) end
				send(spN, function(_, okN)
					if not okN then return done("error", T("Игра не поставила узел на дороге")) end
					local nd = B.nearNode(road.x, road.y, 2, nil, true)
					if nd then road.node, road.x, road.y, road.z = nd.e, nd.x, nd.y, nd.z end
					step2()
				end)
			else
				step2()
			end
			return
		end
		local sp, why = B.stepProposal(pr, kind, target, chk.off, chk.removes, chk.opt)
		if not sp then error(why or T("постройка не собрана")) end
		local ok, err = pcall(function()
			-- with the player's context: the game charges the cost like any build by hand
			api.cmd.sendCommand(api.cmd.makeWorldBuildProposalCmd(sp, B.playerContext(false), false, true), function(data, success)
				local msg
				if success then
					msg = T("Построено")
				else
					local errs = {}
					pcall(function() errs = errorsOf(data.resultProposalData) end)
					msg = T("Игра не построила: ") .. (type(errs) == "table" and table.concat(errs, "; ") or "?")
				end
				pcall(print, "[TF3P] build step " .. tostring(chk.n) .. ": " .. msg)
				for _, nn in ipairs(S.lastTouched or {}) do B.dumpNodeConfig("after build", nn) end
				S.buildCheck = { n = chk.n, id = cur.id, state = success and "done" or "error", text = msg }
				S.sig = nil            -- re-read the world: the ghost of this step goes away
				S.roadRecheck = true
				B.version = (B.version or 0) + 1
			end)
		end)
		if not ok then
			S.buildCheck = { n = chk.n, id = cur.id, state = "error", text = T("команда не отправлена: ") .. tostring(err) }
			B.version = (B.version or 0) + 1
		end
	end))
end

--- can step n be built by the mod (for the panel)
function B.canBuild(n)
	local _, kind = stepTarget(n)
	return kind ~= nil
end

--- glyphs the game font lacks
function B.clean(s)
	s = tostring(s or "")
	s = s:gsub("↺", T("(кольцо)")):gsub("↔", "<->"):gsub("▶", ">")
	return s
end

--- which offer card to take for a planned loan of `amount`: "«Получить» на карточке $10 000 000 — 4-я слева"
function B.loanCard(amount)
	local st = C.gameScriptState("::/game_mechanics/finance/loan.gs")
	local cards = {}
	pcall(function()
		local av = st.availableLoans
		local ok = pcall(function() for i, l in ipairs(av) do cards[i] = l end end)
		if not ok or #cards == 0 then cards = C.list(av) end
	end)
	local function money(v)
		local s = string.format("%.0f", v or 0)
		local out = s:reverse():gsub("(%d%d%d)", "%1 "):reverse():gsub("^ ", "")
		return "$" .. out
	end
	local best, bi = nil, nil
	for i, l in ipairs(cards) do
		local a = tonumber(l.amount) or 0
		if amount and a == amount then best, bi = l, i break end
		if best == nil or math.abs(a - (amount or 0)) < math.abs((tonumber(best.amount) or 0) - (amount or 0)) then
			best, bi = l, i
		end
	end
	if not best then return nil end
	local exact = amount and tonumber(best.amount) == amount
	-- by amount and terms, not by position: the card order differs between games
	local months = (tonumber(best.duration) or 0) / ((C.yearMs and C.yearMs() or 1461000) / 12)
	return string.format(T("Нажмите «Получить» на карточке %s%s%s"), money(best.amount),
		months > 0 and string.format(T(" (%.0f мес., %.0f %%)"), months, (tonumber(best.percentage) or 0) * 100) or "",
		exact and "" or T(" — такой суммы сейчас нет, это ближайшая")), tonumber(best.amount)
end

--- Plain data for the mission panel.
function B.mission()
	local m = { active = S.active and true or false, error = S.error }
	m.recalc = S.waiting and T("ожидаю пересчёт…") or B.statusText(S.status)
	m.notice = S.notice and S.notice.text or nil
	local cur = B.current()
	if not cur then return m end
	if S.hudN == nil then B.hudFollow() end
	m.id, m.title, m.year, m.month = cur.id, B.clean(cur.title), cur.year, cur.month
	m.pos, m.count = S.pos, S.list and #S.list or 0
	m.cost = cur.cost
	local cl = B.checklist(cur.id)
	m.tasks, m.doneN, m.total = {}, 0, 0
	for _, e in ipairs(cl) do
		if e.kind ~= "info" then
			m.total = m.total + 1
			if e.done == true then m.doneN = m.doneN + 1 end
		end
		e.current = (e.n == S.hudN)
		e.canBuild = B.canBuild(e.n)
		e.hint = e.menu and B.MENU_HINT[e.menu] or nil
		e.text = B.clean(e.text)
		if e.kind == "loan" and e.done ~= true then
			local pr = B.project(cur.id)
			local raw = list(pr and pr.checklist)[e.n]
			local okC, card = pcall(B.loanCard, raw and type(raw.ui) == "table" and raw.ui.amount or nil)
			if okC and card then e.hint = e.hint .. " " .. card end
		end
		m.tasks[#m.tasks + 1] = e
		if e.current then m.task = e end
	end
	local chk = S.buildCheck
	if chk and chk.id == cur.id and m.task and chk.n == m.task.n then m.buildCheck = chk end
	return m
end

---------------------------------------------------------------------------
-- compact bar (top panel): the current step of the current project, < > and «Показать»

local function hudSteps()
	local cur = B.current()
	if not cur then return nil, {} end
	return cur, B.checklist(cur.id)
end

--- first step not built yet (checkable ones first, then the rest in order)
local function firstOpen(cl)
	-- first task not done yet: checkable ones (done == false) and hand-confirmed ones (done == nil)
	for _, e in ipairs(cl) do
		if e.kind ~= "info" and e.done ~= true then return e.n end
	end
	return cl[1] and cl[#cl].n or nil
end

--- keep the bar on an open step: when the shown step gets built, move to the next open one.
--- Returns true when the bar moved.
function B.hudFollow()
	local cur, cl = hudSteps()
	if not cur or #cl == 0 then return false end
	if S.hudN == nil then
		S.hudN = firstOpen(cl)
		S.hudPinned = false
		S.hudDone = cl[S.hudN] and cl[S.hudN].done
		return true
	end
	local e = cl[S.hudN]
	local done = e and e.done == true
	-- follow the work: a done step moves on to the first open one, unless the player picked this
	-- (done) step by hand to look at it; a picked step that gets done just now moves on as well
	if done and (not S.hudPinned or S.hudDone == false) then
		local n = firstOpen(cl)
		if n and n ~= S.hudN then
			S.hudN = n
			S.hudPinned = false
			S.hudDone = cl[n] and cl[n].done
			return true
		end
	end
	S.hudDone = e and e.done
	return false
end

function B.hudMove(step)
	local cur, cl = hudSteps()
	if not cur or #cl == 0 then return end
	local n = (S.hudN or firstOpen(cl) or 1) + (step or 1)
	if n < 1 then n = #cl elseif n > #cl then n = 1 end
	S.hudN = n
	S.hudPinned = true
	S.hudDone = cl[n] and cl[n].done
	B.version = (B.version or 0) + 1
end

function B.hudShow()
	local cur, cl = hudSteps()
	if not cur then return false end
	if S.hudN == nil then B.hudFollow() end
	local e = cl[S.hudN or 1]
	if e and e.canShow then return B.show(e.n) end
	pcall(B.focusProject)
	return true
end

--- Plain data for the compact bar.
function B.hud()
	local h = { active = S.active and true or false, error = S.error }
	local st = S.status
	h.recalc = S.waiting and T("ожидаю пересчёт…") or B.statusText(st)
	h.notice = S.notice and S.notice.text or nil
	local cur, cl = hudSteps()
	if not cur then return h end
	if S.hudN == nil then B.hudFollow() end
	h.id, h.title, h.year = cur.id, B.clean(cur.title), cur.year
	h.pos, h.count = S.pos, S.list and #S.list or 0
	h.total = #cl
	local e = cl[S.hudN or 1]
	if e then
		h.n, h.text, h.canShow = e.n, B.clean(e.text), e.canShow
		h.done = e.done
		h.pct = e.pct
	end
	local pg = B.progress(cur.id)
	h.projectDone = pg and pg.done or false
	return h
end

--- Plain data for the window.
function B.view()
	local v = { status = S.error, render = S.renderStats, loadedAt = S.loadedAt, mode = S.mode,
		filterYear = S.filterYear, year = S.year, search = S.search, showYearAll = S.showYearAll,
		showStations = S.showStations, showCorridor = S.showCorridor, showBuilt = S.showBuilt,
		showExisting = S.showExisting, hidden = S.hidden, count = S.list and #S.list or 0, pos = S.pos,
		total = #S.index, items = {}, stops = {} }
	local cur = B.current()
	if cur then
		local pr = B.project(cur.id)
		v.id, v.title, v.projectYear, v.month = cur.id, cur.title, cur.year, cur.month
		v.info = string.format("%s · %s · %.0f $", tostring(cur.mode or ""), tostring(cur.cargo or "-"), cur.cost or 0)
		local pg = B.progress(cur.id)
		if pg then
			v.progress, v.done, v.pct = pg.text, pg.done, pg.pct
		end
		local p = B.prepare(cur.id)
		if p then
			for _, it in ipairs(p.items) do
				v.items[#v.items + 1] = string.format("%s %s (%.0f, %.0f)", it.done and "[+]" or "[ ]",
					tostring(it.src.text or it.src.name or (KIND_RU[it.src.k] and T(KIND_RU[it.src.k])) or ""), it.src.x or 0, it.src.y or 0)
			end
		end
		v.waypointList = {}
		for _, wp in ipairs(list(pr and pr.waypoints)) do
			v.waypointList[#v.waypointList + 1] = string.format(T("т.%.0f — %s"), wp.n or 0, tostring(wp.label or ""))
		end
		v.waypoints = #v.waypointList
		v.demolition = #list(pr and pr.demolition)
		v.checklist = B.checklist(cur.id)
		v.highlight = S.hl and S.hl.n or nil
	end
	v.legend = {}
	for i, line in ipairs(B.LEGEND) do v.legend[i] = T(line) end
	local st = S.status
	if S.waiting then
		v.recalc = T("ожидаю пересчёт… (центр управления подхватит новый экспорт в авторежиме)")
	else
		v.recalc = B.statusText(st)
	end
	v.recalcState = st and st.state or nil
	v.recalcBar = st and st.state == "running" and B.progressBar(st.progress) or nil
	v.notice = S.notice and S.notice.text or nil
	return v
end

--- test hook: forget everything (does not touch the world; call clear() first)
function B._reset()
	S = newState()
end

return B

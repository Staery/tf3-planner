-- .script.lua files referenced via "file.script@Name" must define data() (see base/init.lua dataNotDefined)
function data()
	local react = ug_require "::/gui/main/react.lua"
	local builtin = ug_require "::/gui/main/builtin.lua"
	local main_mod_button_area = ug_require "::/gui/main/main_mod_button_area.tl"
	local export = ug_require "tf3_planner_1::/tf3_planner/export.lua"
	-- Hot reload: the blueprint module can be replaced while the game runs. The desktop app's deploy
	-- writes <userdata>/tf3_planner/blueprint_hot.lua { v, fn = function() <blueprint.lua> end, src }
	-- and blueprint_hot_ver.lua { v }; the bar timer swaps the module when v changes. `bp` is a proxy
	-- to the current module, so every caller below always gets the newest code.
	local HOT = { mod = ug_require "tf3_planner_1::/tf3_planner/blueprint.lua", v = nil, note = nil }
	local bp = setmetatable({}, {
		__index = function(_, k) return HOT.mod[k] end,
		__newindex = function(_, k, v) HOT.mod[k] = v end,
	})
	-- texts of the bar and the window in the blueprint's language (an older hot-loaded module has no T)
	local function T(s)
		local f = HOT.mod and rawget(HOT.mod, "T")
		if f then return f(s) end
		return s
	end
	local function deferrer(fn) react.enqueueJoin(fn, "tf3p") end
	-- game API calls from step timers must run in react's main-thread join (see blueprint.lua B.defer)
	if react.enqueueJoin then HOT.mod.setDeferrer(deferrer) end
	local function hotVersion()
		local ok, d = pcall(app.loadUserdata, "tf3_planner", "blueprint_hot_ver")
		return ok and type(d) == "table" and d.v or nil
	end
	HOT.v = hotVersion()              -- the code the game started with is that version
	local function hotReload()
		local v = hotVersion()
		HOT.ticks = (HOT.ticks or 0) + 1
		if HOT.ticks == 1 or HOT.ticks % 100 == 0 then            -- diagnostics: is the check alive, what does it see
			pcall(print, "[TF3P] hot check #" .. tostring(HOT.ticks) .. ": file v=" .. tostring(v) .. " running v=" .. tostring(HOT.v))
		end
		if v == nil or v == HOT.v then return false end
		local ok, d = pcall(app.loadUserdata, "tf3_planner", "blueprint_hot")
		if not ok or type(d) ~= "table" or d.v ~= v then             -- file still being written: retry
			HOT.tries = (HOT.tries or 0) + 1
			if HOT.tries == 3 then
				pcall(print, "[TF3P] hot reload v" .. tostring(v) .. ": cannot read blueprint_hot (ok=" .. tostring(ok)
					.. ", type=" .. type(d) .. ", v=" .. tostring(type(d) == "table" and d.v) .. ", err=" .. tostring(not ok and d) .. ")")
				HOT.note = T("Обновление мода не загрузилось — нужен перезапуск игры")
			end
			return HOT.tries == 3
		end
		HOT.tries = 0
		HOT.v = v
		local mod, how, err = nil, nil, nil
		if type(d.fn) == "function" then
			local okF, m = pcall(d.fn)
			if okF and type(m) == "table" then mod, how = m, "fn" else err = m end
		end
		pcall(print, "[TF3P] hot reload v" .. tostring(v) .. ": fn=" .. type(d.fn) .. " src=" .. type(d.src) .. " load=" .. type(load))
		if mod == nil and type(d.src) == "string" and load then
			local chunk, e2 = load(d.src, "tf3_planner_1::/tf3_planner/blueprint.lua(hot)")
			if chunk then
				local okC, m = pcall(chunk)
				if okC and type(m) == "table" then mod, how = m, "load" else err = m end
			else
				err = e2
			end
		end
		if mod == nil then
			pcall(print, "[TF3P] hot reload v" .. tostring(v) .. " failed: " .. tostring(err))
			HOT.note = T("Обновление мода не загрузилось — нужен перезапуск игры")
			return true
		end
		local old = HOT.mod
		local was = false
		pcall(function() was = old.isActive() end)
		pcall(old.close)                           -- its zones, markers and highlights go away
		if react.enqueueJoin then pcall(mod.setDeferrer, deferrer) end
		HOT.mod = mod
		if was then pcall(mod.open, false) end
		pcall(print, "[TF3P] hot reload v" .. tostring(v) .. " ok (" .. tostring(how) .. ")")
		HOT.note = T("Мод обновлён без перезапуска (v") .. tostring(v) .. ")"
		return true
	end

	-- Loan offer cards: the game gives all «Получить» buttons one shared tag and no ids, so a single
	-- card cannot be highlighted. Replace the card recipe (react's own replacement table, looked up on
	-- every call) with a wrapper that draws the original card inside a component with the id
	-- "tf3p.loan.<amount>"; blueprint.lua highlights that id for the planned loan.
	-- Recipe ids are allocated sequentially (not by name), so the id of "DefaultLoanCard" is found by
	-- the `name` upvalue of the registered recipe functions. Retried from the bar timer until found.
	local loanHook = { done = false, tries = 0 }
	local function findRecipeId(name)
		if type(_react) ~= "table" or type(_react.originalRecipeFn) ~= "table" or not debug or not debug.getupvalue then
			return nil
		end
		for rid, f in pairs(_react.originalRecipeFn) do
			if type(f) == "function" then
				for i = 1, 40 do
					local un, uv = debug.getupvalue(f, i)
					if un == nil then break end
					if un == "name" then
						if uv == name then return rid end
						break
					end
				end
			end
		end
		return nil
	end
	local function installLoanCardIds()
		if loanHook.done or loanHook.tries > 40 then return end
		loanHook.tries = loanHook.tries + 1
		if loanHook.tries == 40 then
			pcall(print, "[TF3P] loan card ids: DefaultLoanCard not found (debug=" .. tostring(debug ~= nil)
				.. ", getupvalue=" .. tostring(debug and debug.getupvalue ~= nil) .. ", _react=" .. type(_react) .. ")")
		end
		local ok, err = pcall(function()
			local rid = findRecipeId("DefaultLoanCard")
			if rid == nil or type(_react.recipeReplace) ~= "table" then return end
			loanHook.done = true
			if _react.recipeReplace[rid] ~= nil then return end   -- another mod replaced it already
			_react.recipeReplace[rid] = function(param, ...)
				local node = _react.originalRecipeFn[rid](param, ...)
				local loan = type(param) == "table" and param.loan or nil
				if loan and not param.isObtained and loan.amount then
					return builtin.Component{
						meta = { id = string.format("tf3p.loan.%.0f", loan.amount) },
						layout = builtin.BoxLayout{ orientation = builtin.type.Orientation.Vertical, children = { node } },
					}
				end
				return node
			end
			pcall(print, "[TF3P] loan card ids: DefaultLoanCard = recipe " .. tostring(rid))
		end)
		if not ok then pcall(print, "[TF3P] loan card ids: " .. tostring(err)) end
	end
	installLoanCardIds()

	local WINDOW_KEY = "tf3p_blueprint"
	local TICK_SECONDS = 3.0

	local function text(s, class)
		return builtin.TextView{ meta = { class = class or "font-scale-body" }, text = tostring(s or "") }
	end

	-- Button.content must be a layout
	local function button(label, onClick, tooltip)
		return builtin.Button{
			meta = { tooltip = tooltip },
			content = builtin.BoxLayout{
				orientation = builtin.type.Orientation.Horizontal,
				children = { text(label) },
			},
			onClick = onClick,
		}
	end

	local function row(children)
		return builtin.BoxLayout{ orientation = builtin.type.Orientation.Horizontal, children = children }
	end

	local function flag(on, label)
		return (on and "[x] " or "[ ] ") .. label
	end

	-- UTF-8 aware length / cut (Cyrillic is 2 bytes per letter)
	local function ulen(s)
		local _, n = tostring(s or ""):gsub("[^\128-\191]", "")
		return n
	end
	local function ucut(s, n)
		s = tostring(s or "")
		if ulen(s) <= n then return s end
		local out, k = {}, 0
		for ch in s:gmatch("[\1-\127\192-\255][\128-\191]*") do
			k = k + 1
			if k > n - 1 then break end
			out[#out + 1] = ch
		end
		return table.concat(out) .. "…"
	end
	-- word wrap into lines of at most n characters (one TextView per line keeps the window narrow)
	local function wrap(s, n)
		local lines, line = {}, ""
		for word in tostring(s or ""):gmatch("%S+") do
			if line ~= "" and ulen(line) + 1 + ulen(word) > n then
				lines[#lines + 1] = line
				line = "    " .. word
			else
				line = (line == "") and word or (line .. " " .. word)
			end
		end
		if line ~= "" then lines[#lines + 1] = line end
		return lines
	end
	local WRAP = 64
	local function textBlock(s, class)
		local lines = wrap(s, WRAP)
		if #lines <= 1 then return text(s, class) end
		local kids = {}
		for i, l in ipairs(lines) do kids[i] = text(l, class) end
		return builtin.BoxLayout{ orientation = builtin.type.Orientation.Vertical, children = kids }
	end

	local BlueprintContent = react.RegisterRecipe("Tf3pBlueprintContent", function(params)
		local version = react.useState(0)
		local showLegend = react.useState(false)
		local showPoints = react.useState(false)
		local showSettings = react.useState(false)
		local function bump() version:set(version:old() + 1) end
		local function act(fn)
			return function(...)
				local ok, err = pcall(fn, ...)
				if not ok then pcall(print, "[TF3P] blueprint: " .. tostring(err)) end
				bump()
			end
		end

		local seen = react.useState(-1)
		react.onMount(function()
			-- the window only shows the blueprint; turning it on/off is the «Чертёж» button / «Выключить»
			local ok, err = pcall(function()
				if bp.isActive() then bp.focusProject() else bp.open() end
			end)
			if not ok then pcall(print, "[TF3P] blueprint open: " .. tostring(err)) end
			bump()
		end)
		-- the top-bar plugin ticks the blueprint; the window just follows its version
		react.onStepTimer(function()
			if not bp.isActive() then
				local ok, changed = pcall(bp.tick)
				if ok and changed then bump() end
			end
			if (bp.version or 0) ~= seen:old() then
				seen:set(bp.version or 0)
				bump()
			end
		end, TICK_SECONDS)

		local okM, m = pcall(bp.mission)
		if not okM or type(m) ~= "table" then m = { error = tostring(m), tasks = {} } end
		local okV, v = pcall(bp.view)
		if not okV then v = { status = tostring(v), items = {}, stops = {} } end

		local children = {}
		local function add(c) children[#children + 1] = c end
		local CHECK = "::/mission/icons/symbol_check_circle_32.tga"

		-- ===== mission header: project, its place in the plan, overall progress
		if m.title then
			add(text(string.format(T("Задание %.0f из %.0f · %s г.%s"), m.pos or 0, m.count or 0, tostring(m.year or ""),
				(m.cost and m.cost > 0) and string.format(T(" · вложения %.1f млн $"), m.cost / 1e6) or ""), "font-scale-small"))
			add(textBlock(tostring(m.title), "font-scale-headline"))
			local frac = (m.total or 0) > 0 and (m.doneN or 0) / m.total or 0
			add(text(string.format(T("Выполнено %.0f из %.0f  %s"), m.doneN or 0, m.total or 0, bp.progressBar(frac, 24) or ""),
				"font-scale-body"))
			add(row{
				button(T("<< Задание"), act(function() bp.next(-1) end), T("Предыдущий проект плана")),
				button(T("Задание >>"), act(function() bp.next(1) end), T("Следующий проект плана")),
				button(bp.isActive() and T("Убрать с карты") or T("Показать на карте"), act(function()
					if bp.isActive() then bp.close() else bp.open() end
				end), T("Подсказки на карте и подсветка кнопок (окно можно закрыть — задание останется в верхней строке)")),
			})
		elseif m.error then
			add(textBlock("! " .. tostring(m.error), "font-scale-body"))
		end
		if m.recalc then add(text(m.recalc .. (v.recalcBar and ("  " .. v.recalcBar) or ""), "font-scale-small")) end
		if m.notice then add(text(m.notice, "font-scale-small")) end

		-- ===== task list (campaign style): done ✓, current ▶, others ○; click selects the task
		for _, e in ipairs(m.tasks or {}) do
			if e.kind ~= "info" then
				local cnt = ""
				if e.counter then
					cnt = string.format("  %.0f/%.0f", e.counter[1] or 0, e.counter[2] or 0)
				elseif e.done == false and e.pct and e.pct > 0 then
					cnt = string.format("  %.0f %%", e.pct)
				end
				local mark = e.current and "→ " or ((e.done == true) and "" or "○ ")
				local label = string.format("%s%.0f. %s%s", mark, e.n, ucut(e.text, 58), cnt)
				local kids = {}
				if e.done == true then
					kids[#kids + 1] = builtin.ImageView{ meta = { class = "checkmark, ok" }, path = CHECK }
				end
				kids[#kids + 1] = text(label, e.current and "font-scale-body" or "font-scale-small")
				add(builtin.Button{
					meta = { tooltip = tostring(e.text or "") },
					content = builtin.BoxLayout{ orientation = builtin.type.Orientation.Horizontal, children = kids },
					onClick = act(function() bp.selectTask(e.n) end),
				})
			end
		end

		-- ===== the current task: full text, where to click, actions
		local t = m.task
		if t then
			add(text(" "))
			add(text(T("Сейчас:"), "font-scale-body"))
			add(textBlock(tostring(t.text or ""), "font-scale-body"))
			if t.hint then add(textBlock(T("Где: ") .. t.hint, "font-scale-small")) end
			local acts = {}
			if t.canShow then
				acts[#acts + 1] = button(T("Показать на карте"), act(function() bp.selectTask(t.n) end),
					T("Камера к месту, место мигает"))
			end
			if t.canBuild and t.done ~= true then
				acts[#acts + 1] = button(T("Построить"), act(function() bp.buildCheck(t.n) end),
					T("Проверить место и стоимость; постройка — после подтверждения"))
			end
			if t.canOpen then
				acts[#acts + 1] = button(T("Открыть в меню"), act(function()
					if not bp.openMenu(t.n) then bp.notify(T("Не нашёл постройку в меню — откройте подсвеченную кнопку")) end
				end), T("Открыть меню строительства сразу на нужной постройке"))
			end
			if t.manual then
				acts[#acts + 1] = button(t.done == true and T("Отменить «готово»") or T("Готово"), act(function() bp.toggleManual(t.n) end),
					T("Этот шаг игра проверить не может — отметьте вручную"))
			end
			acts[#acts + 1] = button(T("Следующий шаг"), act(function() bp.hudMove(1) end))
			add(row(acts))
			local bc = m.buildCheck
			if bc then
				add(textBlock((bc.state == "checking" and T("Проверяю место…")) or tostring(bc.text or ""), "font-scale-body"))
				if bc.state == "ok" then
					add(row{
						button(T("Подтвердить постройку"), act(function() bp.buildConfirm() end),
							T("Построить сейчас (деньги спишутся как при обычной постройке)")),
					})
				end
			end
		end
		if m.title and (m.total or 0) > 0 and (m.doneN or 0) >= m.total then
			add(row{ text(T("Задание выполнено!  ")), button(T("Следующее задание"), act(function() bp.next(1) end)) })
		end

		-- ===== settings (collapsed): plan file, filters, map layers, legend
		add(text(" "))
		add(row{
			button(showSettings:old() and T("Скрыть настройки") or T("Настройки и пересчёт"), function() showSettings:set(not showSettings:old()) end),
		})
		if showSettings:old() then
			add(row{
				button(T("Экспорт и пересчёт"), act(function() bp.exportAndRecalc() end),
					T("Записать экспорт для планировщика; центр управления в авторежиме пересчитает план, чертёж обновится сам")),
				button(T("Обновить из файла"), act(function() bp.reload() end), T("Перечитать plan_overlay.lua")),
			})
			add(row{
				button(T("<< год"), act(function() bp.yearStep(-1) end)),
				button(T("год >>"), act(function() bp.yearStep(1) end)),
				button(flag(v.filterYear, T("только год ") .. tostring(v.year or "")), act(function() bp.toggle("filterYear") end),
					T("Показывать в списке проекты выбранного года или все")),
			})
			add(builtin.TextInputField{
				meta = { class = "font-scale-body" },
				value = v.search or "",
				placeholderText = T("поиск по названию / линии (L12)"),
				onValueChange = act(function(s) bp.setSearch(s) end),
			})
			add(row{
				button(flag(v.showYearAll, T("все проекты года")), act(function() bp.toggle("showYearAll") end)),
				button(flag(v.showCorridor, T("коридор")), act(function() bp.toggle("showCorridor") end)),
				button(flag(v.showExisting, T("существующие дороги")), act(function() bp.toggle("showExisting") end)),
				button(flag(v.showBuilt, T("построенное")), act(function() bp.toggle("showBuilt") end)),
			})
			add(row{
				button(showLegend:old() and T("Скрыть легенду") or T("Легенда цветов"), function() showLegend:set(not showLegend:old()) end),
			})
			if showLegend:old() then
				for _, s in ipairs(v.legend or {}) do add(text(s, "font-scale-small")) end
			end
			add(text(string.format(T("%s%s · проверка: %s · файл: %s"), v.status and ("! " .. v.status .. " · ") or "",
				tostring(v.render or ""), tostring(v.mode or "-"), tostring(v.loadedAt or "-")), "font-scale-small"))
		end

		-- long checklists scroll inside the window
		return builtin.BoxLayout{
			orientation = builtin.type.Orientation.Vertical,
			children = {
				builtin.ScrollArea{
					horizontalPolicy = builtin.type.ScrollBarPolicy.AlwaysOff,
					verticalPolicy = builtin.type.ScrollBarPolicy.AsNeeded,
					content = builtin.Component{
						layout = builtin.BoxLayout{
							orientation = builtin.type.Orientation.Vertical,
							children = children,
						},
					},
				},
			},
		}
	end)

	local BlueprintWindow = react.RegisterWrapperRecipe("Tf3pBlueprintWindow", builtin.Window, function(params)
		return builtin.Window{
			title = T("TF3 Planner: задание"),
			closable = true,
			onClose = params.onClose,
			content = BlueprintContent{},
		}
	end)

	local function windowApi()
		local ok, grg = pcall(ug_require, "::/gui/main/game_react_globals.tl")
		if not ok or not grg then return nil end
		local okW, wc = pcall(grg.getDefaultWindowApi)
		return okW and wc or nil
	end

	local function openBlueprint()
		local wc = windowApi()
		if not wc then return false end
		local function onClose()
			-- closing the window keeps the blueprint on the map (the top bar shows the current step)
			pcall(wc.removeWindow, BlueprintWindow, WINDOW_KEY)
		end
		local ok = pcall(wc.addWindow, BlueprintWindow, WINDOW_KEY, { onClose = onClose })
		return ok
	end

	-- top bar: Export, «Чертёж» (window), on/off, and while on — the current step with < > «Показать».
	-- It also ticks the blueprint, so the ghost keeps fading as you build with the window closed.
	local Tf3PlannerButton = react.RegisterPluginRecipe(main_mod_button_area.MainModButtonAreaExtension, "Tf3PlannerButton", function()
		local label = react.useState("TF3 Planner: Export")
		local bpLabel = react.useState(T("Задание"))
		local version = react.useState(0)
		local seen = react.useState(-1)
		local function bump() version:set(version:old() + 1) end
		local function act(fn)
			return function(...)
				local ok, err = pcall(fn, ...)
				if not ok then pcall(print, "[TF3P] bar: " .. tostring(err)) end
				bump()
			end
		end

		react.onMount(function()
			-- the blueprint was on when the game was saved: draw it again (no camera jump)
			local ok, was = pcall(bp.wasActive)
			if ok and was then pcall(bp.open, false) end
			-- publish the highlight set right away (empty when off): the save may hold an old one
			pcall(bp.applyHighlights)
			bump()
		end)
		react.onStepTimer(function()
			installLoanCardIds()
			-- loadUserdata answers nil inside a step timer: check in react's main-thread join
			local function hot()
				local okH, reloaded = pcall(hotReload)
				if okH and reloaded then pcall(function() bp.notify(HOT.note) end) end
			end
			if react.enqueueJoin then react.enqueueJoin(hot, "tf3p-hot") else hot() end
			if bp.isActive() then pcall(bp.tick) end
			if (bp.version or 0) ~= seen:old() then
				seen:set(bp.version or 0)
				bump()
			end
		end, TICK_SECONDS)

		local children = {
			builtin.Button{
				meta = { tooltip = "Export map, towns, industries and vehicles for the TF3 planner" },
				content = builtin.BoxLayout{
					orientation = builtin.type.Orientation.Horizontal,
					children = { builtin.TextView{ meta = { class = "font-scale-body" }, text = label:old() } },
				},
				onClick = function()
					local ok, res = pcall(export.run)
					if not ok then
						pcall(print, "[TF3P] export crashed: " .. tostring(res))
						label:set("TF3 Planner: ERROR (see stdout)")
					else
						label:set(res and "TF3 Planner: exported" or "TF3 Planner: partial (see stdout)")
					end
				end,
			},
			builtin.Button{
				meta = { tooltip = T("TF3 Planner: окно чертежа — список шагов, легенда, выбор проекта (закрытие окна чертёж не убирает)") },
				content = builtin.BoxLayout{
					orientation = builtin.type.Orientation.Horizontal,
					children = { builtin.TextView{ meta = { class = "font-scale-body" }, text = bpLabel:old() } },
				},
				onClick = function()
					local ok, res = pcall(openBlueprint)
					if not (ok and res) then
						pcall(print, "[TF3P] blueprint window failed: " .. tostring(res))
						bpLabel:set(T("Чертёж: ошибка окна (stdout)"))
					end
					bump()
				end,
			},
		}
		local function add(c) children[#children + 1] = c end
		local active = bp.isActive()
		add(button(active and T("[x] на карте") or T("[ ] на карте"), act(function()
			if bp.isActive() then bp.close() else bp.open() end
		end), T("Показать / убрать подсказки чертежа на карте")))
		if active then
			local okH, h = pcall(bp.hud)
			if okH and type(h) == "table" and h.id then
				add(button("<<", act(function() bp.next(-1) end), T("Предыдущий проект")))
				add(button(">>", act(function() bp.next(1) end), T("Следующий проект")))
				add(button("<", act(function() bp.hudMove(-1) end), T("Предыдущий шаг")))
				local mark = (h.done == true) and "[+] " or ""
				local pct = (h.done == false and h.pct and h.pct > 0) and string.format(" (%.0f %%)", h.pct) or ""
				local stepText = string.format(T("%s %s · шаг %.0f/%.0f: %s%s%s"), tostring(h.id), h.projectDone and T("(построен!)") or "",
					h.n or 0, h.total or 0, mark, tostring(h.text or ""), pct)
				add(button(ucut(stepText, 90), act(function() bp.hudShow() end),
					tostring(h.title or "") .. " — " .. tostring(h.text or "") .. T(" (нажмите — камера к месту)")))
				add(button(">", act(function() bp.hudMove(1) end), T("Следующий шаг")))
			elseif okH and type(h) == "table" and h.error then
				add(text(ucut(h.error, 60), "font-scale-small"))
			end
			if okH and type(h) == "table" and (h.recalc or h.notice) then
				add(text(ucut(h.notice or h.recalc, 40), "font-scale-small"))
			end
		end

		-- plugin roots and Button.content must be layouts, not plain components
		return builtin.BoxLayout{
			orientation = builtin.type.Orientation.Horizontal,
			children = children,
		}
	end)

	return {
		Tf3PlannerButton = Tf3PlannerButton,
		Tf3pBlueprintWindow = BlueprintWindow,
		Tf3pBlueprintContent = BlueprintContent,
	}
end

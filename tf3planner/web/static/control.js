/* TF3 Planner control center (control.html). Talks to /api/control/* (tf3planner/control/api.py). */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const S = {
  status: null,
  lastEvent: null,          // id of the last event already shown
  job: null,                // selected job id
  jobActive: false,
  logNext: 0,
  logTimer: null,
  seenRunning: new Set(),
  fails: 0,
  settingsLoaded: false,
  waitingRestart: false,
};

// ------------------------------------------------------------------ helpers
async function api(path, opts = {}) {
  const r = await apiFetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    cache: "no-store",
  });
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}
const post = (path, body) => api(path, { method: "POST", body: body || {} });

function fmtMoney(v) {
  if (v === null || v === undefined || isNaN(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toLocaleString("ru-RU", { maximumFractionDigits: 2 }) + loc(" млрд $");
  if (a >= 1e6) return (v / 1e6).toLocaleString("ru-RU", { maximumFractionDigits: 1 }) + loc(" млн $");
  return Math.round(v).toLocaleString("ru-RU") + " $";
}
function relTime(ts) {
  if (!ts) return "—";
  const d = Date.now() / 1000 - ts;
  if (d < 45) return loc("только что");
  if (d < 3600) return loc("{0} мин назад", Math.round(d / 60));
  if (d < 86400) return loc("{0} ч {1} мин назад", Math.floor(d / 3600), Math.round((d % 3600) / 60));
  return absTime(ts);
}
function absTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function fmtDur(s) {
  if (s === null || s === undefined) return "";
  s = Math.round(s);
  if (s < 60) return loc("{0} с", s);
  return loc("{0} мин {1} с", Math.floor(s / 60), String(s % 60).padStart(2, "0"));
}
function gameDate(d) {
  if (!d || !d.year) return "—";
  const p = (n) => String(n || 1).padStart(2, "0");
  return `${p(d.day)}.${p(d.month)}.${d.year}`;
}
const kv = (rows) => `<dl class="kv">${rows.filter(Boolean).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
const badge = (text, cls = "") => `<span class="badge ${cls}">${text}</span>`;
const STATUS_RU = { queued: loc("в очереди"), running: loc("выполняется"), done: loc("готово"), failed: loc("ошибка"), cancelled: loc("отменено") };

function toast(text, level = "info", link = null, ms = 9000) {
  const el = document.createElement("div");
  el.className = `toast ${level}`;
  el.innerHTML = loc("<div class=\"t-body\">{0}{1}</div>\n    <button class=\"t-close\" title=\"Закрыть\">✕</button>", esc(text), link ? loc("<br><a href=\"{0}\" target=\"tf3map\">Открыть карту →</a>", esc(link)) : "");
  $(".t-close", el).onclick = () => el.remove();
  $("#toasts").appendChild(el);
  if (ms) setTimeout(() => el.remove(), ms);
}
function desktopNotify(text, link) {
  if (!S.status?.settings?.notify_desktop || !("Notification" in window)) return;
  if (document.visibilityState === "visible" && document.hasFocus()) return;
  if (Notification.permission === "granted") {
    const n = new Notification(loc("TF3 Планировщик"), { body: text });
    n.onclick = () => { window.focus(); if (link) window.open(link, "tf3map"); };
  }
}

// ------------------------------------------------------------------ status polling
async function refreshStatus() {
  let st;
  try {
    st = await api("/api/control/status");
  } catch (e) {
    if (++S.fails >= 2) setOffline(true);
    return;
  }
  S.fails = 0;
  if (S.waitingRestart) { location.reload(); return; }
  setOffline(false);
  S.status = st;
  if (S.lastEvent === null) S.lastEvent = st.last_event;
  renderHeader(st);
  renderActions(st);
  renderCards(st);
  renderActions(st);     // again: cards contain [data-run] buttons too
  renderHistory(st);
  if (!S.settingsLoaded) { fillSettings(st); S.settingsLoaded = true; }
  renderEnv(st);
  followJobs(st);
  if (st.last_event > S.lastEvent) pullEvents();
}

async function pullEvents() {
  try {
    const r = await api(`/api/control/events?since=${S.lastEvent}`);
    for (const e of r.events) {
      toast(e.text, e.level, e.link);
      if (e.level === "good" || e.level === "critical") desktopNotify(e.text, e.link);
    }
    S.lastEvent = r.last;
  } catch { /* next poll */ }
}

function setOffline(off) {
  $("#offline").hidden = !off;
  $("#offline-wait").hidden = !S.waitingRestart;
}

// ------------------------------------------------------------------ header
function renderHeader(st) {
  const t = $("#auto-toggle");
  if (document.activeElement !== t) t.checked = !!st.settings.auto_mode;
  $("#auto-msg").textContent = st.auto.enabled ? (st.auto.message || "") : (st.auto.message || loc("Авторежим выключен"));
  const run = st.jobs.running;
  $("#srv-state").textContent = run.length ? loc("Выполняется: {0}", run.map((j) => j.title).join(", ")) : loc("Сервер работает · порт {0}", st.server.port);
}

// ------------------------------------------------------------------ actions
function runningKinds(st) { return new Set(st.jobs.running.map((j) => j.kind)); }

function renderActions(st) {
  const kinds = Object.fromEntries(st.kinds.map((k) => [k.key, k]));
  const running = runningKinds(st);
  for (const b of $$("[data-run]")) {
    const k = kinds[b.dataset.run];
    const isRun = running.has(b.dataset.run);
    b.classList.toggle("running", isRun);
    const blocker = k && st.jobs.running.find((j) => (k.conflicts || []).includes(j.kind));
    b.disabled = !k || !k.available || isRun || !!blocker;
    if (k && !k.available) b.title = loc("Модуль ещё не установлен");
    else if (isRun) b.title = loc("Уже выполняется");
    else if (blocker) b.title = loc("Ждёт окончания «{0}»", blocker.title);
    else b.title = k ? k.description : "";
  }
  const menu = $("#diag-menu");
  const diag = st.kinds.filter((k) => k.group === "diag");
  const sig = diag.map((k) => k.key + k.available + running.has(k.key)).join();
  if (menu.dataset.sig !== sig) {
    menu.dataset.sig = sig;
    menu.innerHTML = diag.map((k) => `<button data-run="${k.key}" ${k.available ? "" : "disabled"}>
      ${esc(k.title)}${running.has(k.key) ? loc(" — выполняется…") : ""}<small>${esc(k.description)}</small></button>`).join("");
  }
}

async function runJob(kind) {
  try {
    const r = await post(`/api/control/run/${kind}`);
    S.seenRunning.add(r.job.id);
    selectJob(r.job.id);
    refreshStatus();
  } catch (e) {
    toast(e.message, "warn");
  }
}

document.addEventListener("click", (ev) => {
  const run = ev.target.closest("[data-run]");
  if (run && !run.disabled) { closeMenus(); runJob(run.dataset.run); return; }
  const open = ev.target.closest("[data-open]");
  if (open) {
    closeMenus();
    post(`/api/control/open/${open.dataset.open}`).catch((e) => toast(e.message, "warn"));
    return;
  }
  const mb = ev.target.closest("[data-menu]");
  if (mb) {
    const m = document.getElementById(mb.dataset.menu);
    const was = m.hidden;
    closeMenus();
    m.hidden = !was;
    return;
  }
  if (!ev.target.closest(".menu")) closeMenus();
});
function closeMenus() { $$(".menu").forEach((m) => (m.hidden = true)); }

// ------------------------------------------------------------------ cards
function setCard(id, cls, html) {
  const c = document.getElementById(id);
  c.className = `card ${cls}`;
  const body = $(".card-body", c);
  if (body.innerHTML !== html) body.innerHTML = html;
}

function renderCards(st) {
  // --- export
  const ex = st.export;
  let h, cls;
  if (!ex.json_time && !ex.lua_time) {
    cls = "attn";
    h = loc("<div class=\"big\">Нет данных</div><div class=\"card-note\">Включите мод в сейве и нажмите в игре кнопку «TF3 Planner: Export».</div>");
  } else {
    const v5 = (ex.version || 0) >= 5;
    cls = ex.errors_count ? "err" : (ex.pending || !v5 ? "attn" : "ok");
    h = `<div class="big">${gameDate(ex.date)}</div>` + kv([
      [loc("Выгрузка из игры"), relTime(ex.lua_time)],
      [loc("Загружена"), relTime(ex.json_time)],
      [loc("Версия выгрузки"), `v${ex.version ?? "?"}`],
      [loc("Деньги"), fmtMoney(ex.money)],
      [loc("Города / предприятия"), `${ex.towns ?? "—"} / ${ex.industries ?? "—"}`],
      [loc("Линий в игре"), ex.lines ?? (v5 ? "0" : loc("нет в v") + (ex.version ?? "?"))],
      [loc("Ошибки выгрузки"), ex.errors_count ? `<span class="bad">${ex.errors_count}</span>` : loc("нет")],
    ]);
    if (ex.pending) h += badge(loc("Есть новая выгрузка — ещё не загружена"), "warn");
    if (!v5) h += badge(loc("Старая версия мода: обновите мод и перезапустите игру"), "warn");
    if (ex.errors && ex.errors.length) h += `<div class="card-note bad">${ex.errors.map((e) => `${esc(e.section)}: ${esc(e.error)}`).join("<br>")}</div>`;
    if (ex.pending) h += loc("<div class=\"card-actions\"><button data-run=\"update\">Загрузить и пересчитать</button><button data-run=\"loader\">Только загрузить</button></div>");
  }
  setCard("card-export", cls, h);

  // --- plan
  const p = st.plan;
  if (!p) {
    setCard("card-plan", "attn", loc("<div class=\"big\">Плана нет</div><div class=\"card-note\">Нажмите «Пересчитать план».</div>"));
  } else if (p.error) {
    setCard("card-plan", "err", `<div class="card-note bad">${esc(p.error)}</div>`);
  } else {
    const hz = p.horizon ? `${p.horizon[0]}–${p.horizon[1]}` : "—";
    const ph2 = p.phase_lines && p.phase_lines["2"] ? loc(" (охват: {0})", p.phase_lines["2"]) : "";
    const d = st.dossier || {};
    let dline = loc("нет");
    if (!d.available) dline = loc("модуль не установлен");
    else if (d.generated) dline = loc("{0} линий", d.lines) + (d.plan_generated && d.plan_generated !== p.generated ? loc(" <span class=\"warn\">устарел</span>") : "");
    h = `<div class="big">${fmtMoney(p.final_money)}</div>` + kv([
      [loc("Рассчитан"), relTime(p.generated_ts)],
      [loc("Горизонт"), hz],
      [loc("Линий"), `${p.lines ?? "—"}${ph2}`],
      [loc("Вложения"), fmtMoney(p.total_capex)],
      [loc("Кредитов"), p.loans ?? "—"],
      [loc("Разбор маршрутов"), dline],
    ]);
    h += p.stale ? badge(loc("Старше выгрузки — пересчитайте"), "warn") : badge(loc("Актуален"), "good");
    setCard("card-plan", p.stale ? "attn" : "ok", h);
  }

  // --- mod
  const m = st.mod;
  if (!m.deployed) {
    h = loc("<div class=\"big\">Не установлен</div><div class=\"card-note\">Нажмите «Установить / обновить мод», затем запустите игру.</div>");
    cls = "err";
  } else {
    h = loc("<div class=\"big\">Установлен{0}</div>", m.deployed_version ? " · v" + m.deployed_version : "") + kv([
      [loc("Версия в проекте"), m.src_version ? "v" + m.src_version : "—"],
      [loc("Версия в игре"), m.deployed_version ? "v" + m.deployed_version : "—"],
      [loc("Установлен"), relTime(m.deployed_time)],
      [loc("Игра запущена"), m.game_running ? loc("да") : loc("нет")],
    ]);
    if (m.up_to_date) h += badge(loc("Актуален"), "good");
    else h += badge(loc("Нужно обновить мод"), "warn");
    if (m.changed_files && m.changed_files.length) h += loc("<div class=\"card-note\">Изменены: {0}</div>", m.changed_files.slice(0, 6).map(esc).join(", "));
    if (!m.up_to_date) h += loc("<div class=\"card-actions\"><button data-run=\"deploy\">Обновить мод</button></div>");
    if (m.game_running && m.deployed_time && st.export.lua_time && m.deployed_time > st.export.lua_time)
      h += loc("<div class=\"card-note\">После обновления мода игру нужно перезапустить полностью.</div>");
    cls = m.up_to_date ? "ok" : "attn";
  }
  setCard("card-mod", cls, h);

  // --- calibration
  const c = st.calibration;
  if (!c) {
    setCard("card-calib", "", loc("<div class=\"big\">Не запускалась</div><div class=\"card-note\">Нужна выгрузка v5 с построенными линиями. Запускается сама в авторежиме.</div>"));
  } else if (c.error) {
    setCard("card-calib", "err", `<div class="card-note bad">${esc(c.error)}</div>`);
  } else {
    const L = { income_factor: loc("Доход: игра / модель"), running_factor: loc("Расходы на ход"), trip_time_factor: loc("Время рейса"),
      speed_factor: loc("Скорость"), load_factor: loc("Загрузка") };
    const rows = [[loc("Запуск"), relTime(c.time)], [loc("Линий учтено"), loc("{0} из {1}", c.lines_used ?? 0, c.lines_total ?? c.lines ?? 0)]];
    for (const [k, v] of Object.entries(c.factors || {})) rows.push([L[k] || k, (+v).toFixed(2)]);
    if (c.trips_per_resident != null) rows.push([loc("Поездок на жителя/год"), (+c.trips_per_resident).toFixed(2)]);
    h = kv(rows);
    if (!c.lines) h += loc("<div class=\"card-note\">В игре ещё нет линий — сравнивать не с чем.</div>");
    setCard("card-calib", c.lines ? "ok" : "", h);
  }

  // --- server
  const s = st.server;
  h = kv([
    [loc("Адрес"), `127.0.0.1:${s.port ?? "?"}`],
    [loc("Запущен"), relTime(s.started)],
    ["PID", s.pid],
    ["Python", s.python],
  ]);
  if (s.code_changed) h += badge(loc("Программа обновилась — перезапустите сервер"), "warn");
  h += loc("<div class=\"card-actions\">{0}\n    <button id=\"btn-shortcut\">Ярлык на рабочем столе</button></div>", s.code_changed ? loc("<button id=\"btn-restart\">Перезапустить сервер</button>") : "");
  setCard("card-server", s.code_changed ? "attn" : "ok", h);
}

document.addEventListener("click", async (ev) => {
  if (ev.target.id === "btn-shortcut") {
    try { const r = await post("/api/control/shortcut"); toast(loc("Ярлык создан: {0}", r.path), "good"); }
    catch (e) { toast(e.message, "critical"); }
  } else if (ev.target.id === "btn-restart") {
    try {
      await post("/api/control/restart");
      S.waitingRestart = true;
      setOffline(true);
    } catch (e) { toast(e.message, "warn"); }
  }
});

// ------------------------------------------------------------------ jobs
function followJobs(st) {
  for (const j of st.jobs.running) {
    if (!S.seenRunning.has(j.id)) {
      S.seenRunning.add(j.id);
      if (!S.jobActive) selectJob(j.id);       // auto-show a job started by auto mode
    }
  }
  if (!S.job) {
    const first = st.jobs.running[0] || st.jobs.recent[0];
    if (first) selectJob(first.id);
  }
}

function renderHistory(st) {
  const all = [...st.jobs.running, ...st.jobs.recent.filter((j) => !st.jobs.running.some((r) => r.id === j.id))];
  const html = all.map((j) => `<li data-job="${esc(j.id)}" class="${j.id === S.job ? "sel" : ""}">
      <span class="dot ${j.status}"></span><span>${esc(j.title)}${j.trigger === "auto" ? loc(' <span class="muted">(авто)</span>') : ""}</span>
      <span class="h-dur">${fmtDur(j.duration)}</span>
      <span class="h-meta">${absTime(j.started || j.created)} · ${STATUS_RU[j.status] || j.status}</span></li>`).join("")
    || loc("<li class=\"muted\">Пока пусто</li>");
  const ul = $("#history");
  if (ul.innerHTML !== html) ul.innerHTML = html;
}
$("#history").addEventListener("click", (ev) => {
  const li = ev.target.closest("[data-job]");
  if (li) selectJob(li.dataset.job);
});

function selectJob(id) {
  if (S.job === id && S.logTimer) return;
  S.job = id;
  S.logNext = 0;
  $("#job-log").innerHTML = "";
  clearTimeout(S.logTimer);
  S.logTimer = null;
  $$("#history li").forEach((li) => li.classList.toggle("sel", li.dataset.job === id));
  pollLog();
}

function lineClass(t) {
  if (t.startsWith("▶")) return "l-step";
  if (t.startsWith("✔") || /^(Готово|Done)/.test(t)) return "l-ok";
  if (t.startsWith("✖") || /Traceback|Error|ERROR|ОШИБКА|^Ошибка|ВНИМАНИЕ|WARNING/.test(t)) return "l-err";
  if (t.startsWith("progress ")) return "l-prog";
  return "";
}

async function pollLog() {
  const id = S.job;
  let r;
  try {
    r = await api(`/api/control/jobs/${encodeURIComponent(id)}/log?since=${S.logNext}`);
  } catch (e) {
    S.logTimer = setTimeout(pollLog, 2000);
    return;
  }
  if (id !== S.job) return;
  const pre = $("#job-log");
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  if (r.skipped) pre.insertAdjacentHTML("beforeend", loc("<span class=\"l-prog\">… начало журнала пропущено …</span>\n"));
  if (r.lines.length) {
    const html = r.lines.map((t) => { const c = lineClass(t); return c ? `<span class="${c}">${esc(t)}</span>` : esc(t); }).join("\n") + "\n";
    pre.insertAdjacentHTML("beforeend", html);
    while (pre.childNodes.length > 12000) pre.removeChild(pre.firstChild);
    if (atBottom || S.logNext === 0) pre.scrollTop = pre.scrollHeight;
  }
  S.logNext = r.next;
  renderJob(r.job);
  S.jobActive = r.job.status === "running" || r.job.status === "queued";
  S.logTimer = S.jobActive ? setTimeout(pollLog, 800) : null;
}

function renderJob(j) {
  const trig = j.trigger === "auto" ? loc(" (авторежим)") : "";
  $("#job-title").innerHTML = `<b>${esc(j.title)}</b>${trig} — ${STATUS_RU[j.status] || j.status}` +
    (j.started ? ` · ${absTime(j.started)} · ${fmtDur(j.duration)}` : "");
  const active = j.status === "running" || j.status === "queued";
  const btn = $("#btn-cancel");
  btn.hidden = !active;
  btn.onclick = async () => {
    btn.disabled = true;
    try { await post(`/api/control/jobs/${encodeURIComponent(j.id)}/cancel`); } catch (e) { toast(e.message, "warn"); }
    btn.disabled = false;
  };
  const prog = $("#job-progress");
  prog.hidden = false;
  const steps = j.steps || [];
  const res = j.step_results || [];
  $("#job-steps").innerHTML = steps.length > 1 ? steps.map((s, i) => {
    let c = "";
    if (res[i]) c = res[i].exit_code === 0 ? "ok" : "fail";
    else if (i === j.step_index && active) c = "cur";
    return `<span class="step ${c}">${i + 1}. ${esc(s)}</span>`;
  }).join("") : "";
  const fill = $("#bar-fill");
  let text;
  if (!active) {
    fill.classList.remove("indet");
    fill.style.width = j.status === "done" ? "100%" : (j.progress != null ? `${Math.round(j.progress * 100)}%` : "0%");
    fill.style.background = j.status === "done" ? "var(--good)" : j.status === "failed" ? "var(--critical)" : "var(--warn)";
    text = j.status === "done" ? loc("Готово за {0}", fmtDur(j.duration)) : (STATUS_RU[j.status] || j.status) + (j.exit_code ? loc(" (код {0})", j.exit_code) : "");
  } else {
    fill.style.background = "var(--accent)";
    if (j.progress != null) {
      fill.classList.remove("indet");
      fill.style.width = `${Math.max(2, Math.round(j.progress * 100))}%`;
      const m = /progress (\d+)\/(\d+)(.*)/.exec(j.progress_text || "");
      const pct = Math.round(j.progress * 100);
      let eta = "";
      if (pct >= 3 && j.duration) {
        // ETA for the current step from the elapsed part of it
        const doneBefore = res.reduce((a, r) => a + (r.duration || 0), 0);
        const stepElapsed = j.duration - doneBefore;
        const left = stepElapsed * (1 - j.progress) / j.progress;
        eta = loc(" · осталось ≈ {0}", fmtDur(left));
      }
      text = m ? loc("Год {0} из {1} — {2}%{3}{4}", m[1], m[2], pct, eta, m[3] ? " ·" + m[3].replace("money", loc("деньги")).replace("lines", loc("линий")) : "") : `${pct}%`;
    } else {
      fill.classList.add("indet");
      text = `${steps[j.step_index] || loc("Работаю")}… ${fmtDur(j.duration)}`;
    }
  }
  $("#bar-text").textContent = text;
}

// ------------------------------------------------------------------ settings
function fillSettings(st) {
  const f = $("#settings");
  for (const [k, v] of Object.entries(st.settings)) {
    const el = f.elements[k];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  }
}
let saveTimer = null;
async function saveSettings(extra = {}) {
  const f = $("#settings");
  const body = { ...extra };
  for (const el of f.elements) {
    if (!el.name) continue;
    body[el.name] = el.type === "checkbox" ? el.checked : el.value;
  }
  try {
    const r = await post("/api/control/settings", body);
    $("#settings-state").textContent = loc("Сохранено");
    $("#settings-state").className = "small good";
    if (S.status) { S.status.settings = r.settings; S.status.env = r.env; renderEnv(S.status); }
    if (body.notify_desktop && "Notification" in window && Notification.permission === "default") Notification.requestPermission();
    return r;
  } catch (e) {
    $("#settings-state").textContent = loc("Не сохранено: ") + e.message;
    $("#settings-state").className = "small bad";
  }
}
$("#settings").addEventListener("change", () => {
  clearTimeout(saveTimer);
  $("#settings-state").textContent = "…";
  saveTimer = setTimeout(saveSettings, 300);
});
$("#settings").addEventListener("submit", (e) => e.preventDefault());
$("#btn-replan").onclick = async () => { await saveSettings(); runJob("planner"); };

$("#auto-toggle").addEventListener("change", async (e) => {
  try {
    const r = await post("/api/control/settings", { auto_mode: e.target.checked });
    toast(r.settings.auto_mode ? loc("Авторежим включён: план пересчитается после выгрузки в игре") : loc("Авторежим выключен"), "info", null, 4000);
    if (r.settings.auto_mode && "Notification" in window && Notification.permission === "default") Notification.requestPermission();
    refreshStatus();
  } catch (err) { toast(err.message, "warn"); }
});

function renderEnv(st) {
  const doc = {
    TF3P_HORIZON: loc("последний год расчёта"), TF3P_PASSENGERS: loc("1/0 — пассажирские линии"),
    TF3P_MULTIMODAL: loc("1/0 — перевалка через склады"), TF3P_COVERAGE: loc("1/0 — фаза 2 «охват»"),
  };
  const html = loc("<div>Настройки сохраняются в <code>data/settings.json</code> и передаются планировщику\n    переменными окружения:</div><table>{0}</table>", Object.entries(st.env || {}).map(([k, v]) =>
      `<tr><td><code>${k}=${esc(v)}</code></td><td>${doc[k] || ""}${st.env_wired && st.env_wired[k] === false ? loc(' <span class="warn">(планировщик пока не читает)</span>') : ""}</td></tr>`).join(""));
  const el = $("#env-doc");
  if (el.innerHTML !== html) el.innerHTML = html;
}

// ------------------------------------------------------------------ game log
async function refreshGameLog() {
  let r;
  try { r = await api("/api/control/gamelog?limit=300"); } catch { return; }
  $("#gamelog-meta").textContent = r.exists
    ? loc("stdout.txt · изменён {0}{1}", relTime(r.time), r.session_start ? loc(" · запуск игры ") + r.session_start.replace("Z", " UTC") : "")
    : loc("журнал игры не найден");
  const hint = (h) => `<div class="hint ${h.level}"><b>${esc(h.title)}</b>${esc(h.text)}</div>`;
  const hints = (r.detected || []).map(hint).join("") +
    loc("<details><summary>Частые вопросы</summary>{0}</details>", (r.hints || []).map(hint).join(""));
  const box = $("#hints");
  const openState = $("details", box)?.open;
  if (box.dataset.sig !== hints) {
    box.dataset.sig = hints;
    box.innerHTML = hints;
    if (openState) $("details", box).open = true;
  }
  const mine = $("#gl-mine").checked;
  const ents = (r.entries || []).filter((e) => !mine || e.mine || e.kind === "tf3p");
  const pre = $("#gamelog");
  const html = ents.length
    ? ents.map((e) => `<span class="g-ts">${esc(e.ts.slice(11, 19))}</span> <span class="g-${e.kind}">${esc(e.text)}</span>`).join("\n")
    : `<span class="g-ts">${mine ? loc("Сообщений мода и его ошибок нет.") : loc("Ошибок нет.")}</span>`;
  if (pre.dataset.sig !== html) {
    pre.dataset.sig = html;
    pre.innerHTML = html;
    pre.scrollTop = pre.scrollHeight;
  }
}
$("#gl-refresh").onclick = refreshGameLog;
$("#gl-mine").onchange = () => { $("#gamelog").dataset.sig = ""; refreshGameLog(); };

// ------------------------------------------------------------------ shutdown
$("#btn-shutdown").onclick = async () => {
  const run = S.status?.jobs?.running || [];
  const msg = run.length
    ? loc("Сейчас выполняется: {0}.\nОстановить задачи и выключить TF3 Планировщик?", run.map((j) => j.title).join(", "))
    : loc("Выключить TF3 Планировщик? Карта и центр управления перестанут открываться до следующего запуска.");
  if (!confirm(msg)) return;
  try { await post("/api/control/shutdown"); } catch { /* the server may already be gone */ }
  S.waitingRestart = false;
  setOffline(true);
};

// ------------------------------------------------------------------ start
refreshStatus();
refreshGameLog();
setInterval(refreshStatus, 2500);
setInterval(refreshGameLog, 10000);

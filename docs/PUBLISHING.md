# Публикация TF3 Planner

Как собрать и выложить обе части набора: игровой мод (mod.io / Mod Hub) и настольную программу
(PyInstaller-сборка). Описание для игроков — в [README.md](../README.md).

## 1. Как устроен набор

```
 Игра (мод tf3_planner_1)                        Программа TF3 Planner (127.0.0.1:8010)
 ───────────────────────                         ─────────────────────────────────────
 [TF3 Planner: Export] ── export.lua ──────────▶ watcher → loader → калибровка → план → разбор
                         (app.saveUserdata)          │
 [Чертёж] ◀── plan_overlay.lua ──────────────────────┘ overlay.py
          ◀── status.lua (ход пересчёта) ◀────────── control/gamestatus.py
          ──▶ blueprint_state.lua (выбранный проект)
```

Все файлы обмена лежат в `<Steam>\userdata\<id>\3493540\local\tf3_planner\` — это единственная
папка, куда мод может писать (в TF3 `io` отключён, есть только `app.saveUserdata/loadUserdata`).

* **Экспорт v8** (`content/tf3_planner/export.lua`, `EXPORT_VERSION = 8`): рельеф, города (со
  статистикой и зданиями), предприятия и цепочки, сеть дорог/путей, станции, линии, транспорт,
  кредиты, компания. Поля движка обходятся по `schema.lua` (объекты движка — userdata без `pairs()`).
  Ошибки по отдельным полям не прерывают выгрузку, а попадают в `errors`.
* **Окно «Чертёж»** (`blueprint.lua`): читает `plan_overlay.lua`, показывает проекты плана по
  годам, рисует пути/дороги/станции на карте, отмечает уже построенное, помнит выбранный проект
  (`blueprint_state.lua`).
* **Опрос статуса**: окно периодически читает `status.lua`, который пишет центр управления —
  «Пересчёт плана: 1925 / 2030, 40 %»; когда задача закончилась и `plan_overlay.lua` обновился,
  чертёж перезагружается сам.
* **Без программы**: экспорт работает полностью; в «Чертеже» нет `plan_overlay.lua`, и окно
  показывает ошибку чтения файла. Текущий текст подсказки — «(запустите python -m tf3planner.overlay)»
  — рассчитан на разработчика. **Перед публикацией** стоит заменить его в `blueprint.lua`
  (строка с `не удалось прочитать plan_overlay.lua`) на понятный игроку, например:
  «План ещё не рассчитан. Установите программу TF3 Planner (ссылка на странице мода), запустите её
  и нажмите Export.» (Lua-код мода в этой задаче не менялся — правка за владельцем Lua.)

## 2. Пути и переносимость

Всё машинно-зависимое — в `tf3planner/config.py`:

| Что | Порядок поиска |
|---|---|
| папка пользователя игры `…\userdata\<id>\3493540\local` | `TF3P_USERDATA` → `settings.json:userdata_dir` → Steam из реестра (`HKCU\Software\Valve\Steam\SteamPath`, `HKLM\SOFTWARE\WOW6432Node\Valve\Steam\InstallPath`) → `userdata\*\3493540\local`, при нескольких — последний изменённый |
| установка игры | `TF3P_GAME` → `settings.json:game_dir` → библиотеки из `steamapps\libraryfolders.vdf` + `appmanifest_3493540.acf` |
| данные программы | `TF3P_DATA` → сборка: `<exe>\data` (портативно) или `%LOCALAPPDATA%\TF3Planner\data`; исходники: `<проект>\data` |
| копии мода из Mod Hub | `%PUBLIC%\mod.io\10640\mods\*\mod.json` с `modId = tf3_planner_1` (только предупреждение при «Установить мод») |

`python -m tf3planner.config` печатает найденное, `--set-game/--set-userdata` сохраняют
переопределение. На машине автора результат совпадает с прежними жёсткими путями.

Файлы из игры, нужные программе, **генерируются у игрока**, а не распространяются:
`industry_recipes.json` — `tools/extract_recipes.py` при первом запуске (`tf3planner/firstrun.py`,
вызывается лаунчером сборки при каждом старте, дёшево при наличии файла; `--force` после
обновления игры). `schema.lua` поставляется в моде (мод не может читать файлы игры сам);
`tools/gen_schema.py` перегенерирует его после обновления API игры (только автору).

## 3. Сборка настольной программы

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Test -Zip
```

* Создаёт отдельный `.venv-build` из `packaging\requirements.txt` (версии зафиксированы) +
  PyInstaller 6.22.3, собирает `packaging\tf3planner.spec` → **`dist\TF3Planner\`** (one-folder).
* `TF3Planner.exe` — без консоли: первый запуск (рецепты) → сервер в фоне → браузер.
  `tf3planner-cli.exe` — консольный: им центр управления запускает задачи
  (`-X utf8 -u -m tf3planner.planner …`; оба exe понимают этот синтаксис, см. `packaging\entry.py`).
  `multiprocessing.freeze_support()` включён — параллельный планировщик работает и в сборке.
* В сборку входят: Python 3.13, numpy, scipy (HiGHS), lupa, fastapi/starlette/uvicorn/pydantic,
  pillow, ortools (+ его DLL из `ortools\.libs`), elkai; исходники `tf3planner\` с веб-интерфейсом;
  мод `mod\tf3_planner_1` (без `mod.io_fileid.txt`); `tools\extract_recipes.py`, `gen_schema.py`.
  **Не** входят: `data\` (ваши карты, планы, логи, рецепты).
* Результат (2026-10-03): **230 МБ, 903 файла**, zip **96 МБ**. Самое тяжёлое: ortools 72 МБ,
  scipy 67 МБ, numpy 27 МБ, PIL 13 МБ. Без OR-Tools сборка ≈ 155 МБ — уберите `ortools` из
  `requirements.txt` и hiddenimports (tours.py переключится на собственные эвристики, планы могут
  немного отличаться).
* `-Test` → `packaging\smoke_test.py`: версия, пути, HiGHS/lua/ortools/пул процессов, лаунчер на
  свободном порту с временной папкой данных, `/control.html` 200, `/api/control/ping`, `/status`,
  карта `/`, извлечение рецептов, остановка сервера через `/api/control/shutdown`.
* Установщик (необязательно): Inno Setup 6, `ISCC.exe packaging\tf3planner.iss` →
  `dist\TF3Planner-Setup-<версия>.exe`; установка без прав администратора в
  `%LOCALAPPDATA%\Programs\TF3Planner`, ярлыки, опция «установить мод в игру».
* Подпись кода: без неё SmartScreen показывает предупреждение, а некоторые антивирусы ругаются на
  PyInstaller-сборки (UPX выключен, это уже снижает ложные срабатывания). При ложных срабатываниях —
  отправить файл в Microsoft (https://www.microsoft.com/wdsi/filesubmission).

## 4. Мод для mod.io

**Что загружается**: папка `tf3_planner_1` целиком:

```
tf3_planner_1/
  mod.json            modId "tf3_planner_1", revision, severityAdd/Remove "None"
  _content.json       список файлов content/ (игра загружает только перечисленные!)
  _metadata/
    modinfo.json      name, summary, description, tags, authors — карточка в Mod Hub
    0.png             картинка 1280×720 (logo/галерея; ещё картинки: 1.png, 2.png, …)
  content/gui/tf3_planner/…   кнопки Export / Чертёж
  content/tf3_planner/…       export.lua, blueprint.lua, dump.lua, network.lua, stats.lua,
                              townbuildings.lua, common.lua, schema.lua
```

Проверка и чистая копия: `python -X utf8 packaging\package_mod.py` → `dist\mod\tf3_planner_1\`
и `dist\tf3_planner_1-v<revision>.<EXPORT_VERSION>.zip` (проверяет modinfo, картинку,
`_content.json`, компилируемость всех .lua, отсутствие лишних файлов). `--install` кладёт
проверенную копию в `local\mods`. Картинка: `python packaging\make_preview.py` (рисуется
программно, без ресурсов игры).

**modinfo.json**: `name` «TF3 Planner»; `summary` — одна строка; `description` — EN + RU, что делает
мод, что чертёж требует программу, приватность; `tags: ["Script Mod"]` (тег, реально
используемый скриптовыми модами TF3 на mod.io; список тегов задаёт mod.io — при публикации
можно добавить подходящие, например «Misc»). Ссылку на скачивание программы добавьте в описание
на странице mod.io после создания релиза. По строкам из exe игры: длинное описание может также
читаться из `_metadata/description.html` (не проверено).

**Версии**:
* `modId` `tf3_planner_1` — суффикс `_1` это «мажорная» линия; не меняйте, иначе сейвы увидят
  другой мод.
* Версия файла на mod.io (вводится при загрузке): предлагается `1.<EXPORT_VERSION>[.n]`,
  сейчас `1.8`. При изменении формата выгрузки увеличивайте `EXPORT_VERSION` в export.lua — центр
  управления показывает версии экспортёра (исходник / установлен).
* Программа: `APP_VERSION` в `tf3planner/config.py` и `AppVersion` в `tf3planner.iss` (сейчас 0.9.0).
  В примечаниях к релизу указывайте минимальную версию экспорта.

**Загрузка через игру** (Mod Hub; в exe игры есть ModPublishHelper со staging_area, проверкой и
«cooking» перед загрузкой; названия пунктов меню проверьте в игре):
1. `package_mod.py --install` (или «Установить мод в игру»), полностью перезапустить игру.
2. Mod Hub → «My Mods» → TF3 Planner → публикация: игра копирует мод в
   `local\staging_area\`, проверяет, «готовит» и загружает на mod.io; нужен вход в аккаунт mod.io и
   согласие с его условиями.
3. Видимость сначала «скрытая»/«приватная» — проверить установку подпиской с другого
   аккаунта/ПК, затем сделать публичным.
4. После загрузки игра записывает `_metadata/mod.io_fileid.txt` — связь локального мода со
   страницей mod.io (для обновлений). `deploy.py` теперь сохраняет этот файл при переустановке,
   в пакеты (сборка, zip) он не попадает. Не подписывайтесь на собственный мод, пока в `local\mods`
   есть локальная копия (две копии с одним modId).

Загрузка zip через сайт mod.io возможна, но игра готовит мод сама при загрузке из Mod Hub —
предпочтительнее Mod Hub.

## 5. Распространение программы

GitHub Releases (или страница mod.io → «Links»): `TF3Planner-<версия>-win64.zip` и (если собран)
`TF3Planner-Setup-<версия>.exe`, контрольные суммы SHA-256 (`Get-FileHash`). Исходники — тот же
репозиторий; `.gitignore` исключает `data\`, `dist\`, `build\`, venv'ы и `mod.io_fileid.txt`.

**Лицензия (рекомендация)**: MIT для кода программы и мода — коротко, разрешает форки и
исправления; файл `LICENSE` с текстом MIT и вашим именем добавьте сами. Учтите лицензии
зависимостей: всё BSD/MIT/Apache-2.0, кроме LKH-3 внутри elkai — бесплатно только для
академического/некоммерческого использования (бесплатная раздача подходит; при любой
коммерциализации уберите elkai).

## 6. Правовые и ToS-вопросы

* Мод читает игру **только через официальный Lua-API моддинга** и пишет только свои файлы через
  `app.saveUserdata`. Файлы игры не изменяются, никакой инъекции/патчей exe.
* Ресурсы игры **не распространяются**. `industry_recipes.json` (числа из файлов игры) создаётся
  у игрока из его установки. `schema.lua` содержит только имена типов и полей API, сгенерированные
  из определений Teal, которые игра поставляет для моддеров (`api/tealdef`, `base/tealdef`) — это
  описание интерфейса, необходимое для совместимости, без кода и ассетов. Если нужна полная
  перестраховка — спросить Urban Games или генерировать его у игрока (мод не может читать файлы,
  поэтому это потребовало бы установки мода программой).
* Публикация на mod.io подчиняется условиям mod.io и правилам игры для модов (Urban Games):
  без чужих ресурсов, без вредоносного кода, корректное описание. Мод бесплатный.
* Не используйте логотипы игры/издателя на картинке и в названии программы; пишите
  «для Transport Fever 3», «неофициальный», «не связан с Urban Games / Paradox Interactive».
* Приватность: сервер только на 127.0.0.1, веб-интерфейс без внешних ресурсов, телеметрии нет.

## 7. Чек-лист релиза

1. Тесты: `.venv\Scripts\python -m pytest -q`.
2. Версии: `EXPORT_VERSION` (если менялся формат), `config.APP_VERSION`, `tf3planner.iss AppVersion`.
3. Подсказка в «Чертеже» без программы — понятная игроку (см. §1).
4. `python packaging\make_preview.py` (или свои скриншоты как `_metadata/1.png` …).
5. `python packaging\package_mod.py` — без «ПРОБЛЕМА».
6. `packaging\build.ps1 -Test -Zip` — smoke-тест зелёный; при желании `ISCC packaging\tf3planner.iss`.
7. Проверка на «чистой» учётке Windows: распаковать zip, запустить, Export в игре → план → Чертёж.
8. Релиз программы (zip/setup + SHA-256 + changelog).
9. Мод: `package_mod.py --install` → перезапуск игры → Mod Hub → публикация (сначала скрыто),
   версия `1.<EXPORT_VERSION>`, changelog, ссылка на релиз программы в описании.
10. Проверка подпиской с другого аккаунта/ПК → сделать публичным.

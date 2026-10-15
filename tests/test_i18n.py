"""English translation: every Russian UI string has an English version, and the translator works."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import i18n_extract  # noqa: E402

from tf3planner import i18n  # noqa: E402

CYR = re.compile("[А-Яа-яЁё]")
STATIC = ROOT / "tf3planner" / "web" / "static"
GLYPHS = set("СОПДТК")   # letters drawn in the game world with the mod's own Cyrillic glyphs


def _web_catalogue() -> dict[str, str]:
    src = (STATIC / "i18n.js").read_text(encoding="utf-8")
    return json.loads(src.split("/*I18N_EN*/", 1)[1].split("/*END_I18N_EN*/", 1)[0])


def _lua_catalogue() -> dict[str, str]:
    src = (ROOT / "mod/tf3_planner_1/content/tf3_planner/blueprint.lua").read_text(encoding="utf-8")
    body = src.split("B.EN = {", 1)[1].split("\n}\n", 1)[0]

    def unq(s: str) -> str:
        return re.sub(r"\\(.)", lambda m: {"n": "\n"}.get(m.group(1), m.group(1)), s)

    return {unq(k): unq(v) for k, v in re.findall(r'\[\"((?:[^"\\]|\\.)*)\"\] = \"((?:[^"\\]|\\.)*)\"', body)}


@pytest.fixture
def english():
    i18n.set_lang("en")
    yield
    i18n.set_lang("ru")


# --- catalogues ---------------------------------------------------------------------------

def test_every_python_string_has_a_translation():
    missing = {k: w for k, w in i18n_extract.python_strings().items() if k not in i18n.CATALOG}
    assert not missing, "add to tf3planner/locales/en.json:\n" + "\n".join(
        f"  {w}: {k!r}" for k, w in list(missing.items())[:30])


@pytest.mark.parametrize("name", ["python", "web", "lua"])
def test_catalogues_are_english_and_keep_placeholders(name):
    cat = {"python": i18n.CATALOG, "web": _web_catalogue(), "lua": _lua_catalogue()}[name]
    assert cat
    ph = {"python": r"\{\w*\}", "web": r"\{\d+\}", "lua": r"%[-+ #0]*\d*(?:\.\d+)?[dfsqxXgGce%]"}[name]
    for ru, en in cat.items():
        left = set(CYR.findall(en)) - (GLYPHS if name == "lua" else set())
        assert not left, f"untranslated: {ru!r} -> {en!r}"
        assert sorted(re.findall(ph, ru)) == sorted(re.findall(ph, en)), ru


def test_every_web_string_has_a_translation():
    cat = _web_catalogue()
    single_letters = {"А", "а", "Т", "т", "Б", "б"}   # keyboard shortcuts / route pins, not text
    missing = []
    for f in ("app.js", "control.js"):
        missing += [k for k in i18n_extract.js_loc_keys((STATIC / f).read_text(encoding="utf-8"))
                    if CYR.search(k) and k not in cat and k not in single_letters]
    for f in ("index.html", "control.html"):
        missing += [k for k in i18n_extract.html_texts((STATIC / f).read_text(encoding="utf-8"))
                    if k not in cat and k != "Русский"]
    assert not missing, missing[:20]


def test_every_mod_string_has_a_translation():
    cat = _lua_catalogue()
    missing = []
    for f in i18n_extract.LUA_FILES:
        missing += [k for k in i18n_extract.lua_t_keys((ROOT / f).read_text(encoding="utf-8")) if k not in cat]
    assert not missing, missing[:20]


# --- translator ---------------------------------------------------------------------------

def test_russian_is_the_identity():
    i18n.set_lang("ru")
    assert i18n.translate("Пересчитать план") == "Пересчитать план"
    assert i18n.localize({"a": ["Пересчитать план"]}) == {"a": ["Пересчитать план"]}


def test_language_parsing():
    assert i18n.set_lang("EN") == "en"
    assert i18n.set_lang("en-GB") == "en"
    assert i18n.set_lang("de") == "ru"
    assert i18n.set_lang(None) == "ru"
    i18n.set_lang("ru")


def test_templates_numbers_and_tags(english):
    assert i18n.to_english("Пересчитать план") == "Recalculate the plan"
    assert i18n.to_english("Быстрый план (+10 лет)") == "Quick plan (+10 years)"
    # step number + tag in brackets around a known sentence
    out = i18n.to_english("3. [Магистраль по ж/д: склад узла → Дубна] Купить 2× Лошадь (вместимость 6 ед. каждая); цена 1 200.")
    assert out == "3. [Trunk by rail: hub warehouse → Дубна] Buy 2× Лошадь (capacity 6 units each); cost 1 200."


def test_proper_names_are_kept(english):
    i18n.register_names(["Земледельческая ферма", "Дубна", "Дубна — Земледельческая ферма"])
    try:
        assert i18n.to_english("у предприятия «Дубна — Земледельческая ферма»") == \
            "at the industry \"Дубна — Земледельческая ферма\""
        # an ordinary word of our own texts is never treated as a name
        i18n.register_names(["Депо"])
        assert i18n.to_english("Депо") == "Depot"
    finally:
        i18n.register_names([])


def test_units_after_numbers(english):
    assert i18n.to_english("Год = 1461 с игрового времени, день = 4 с.") == \
        "Year = 1461 s of game time, day = 4 s."

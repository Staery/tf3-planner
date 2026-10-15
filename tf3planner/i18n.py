"""Russian / English for everything the planner shows.

The planner writes its texts in Russian (plan.json, dossiers.json, job logs, hints), and
some modules read those texts back with regular expressions, so generation stays Russian.
Translation happens when the text is *shown*:

* ``locales/en.json`` maps Russian strings to English. Keys made from f-strings are
  templates with numbered placeholders (``"Линия {0}: {1} ед./год"``), produced by
  ``tools/i18n_extract.py``; finished text is matched against them, and the values in
  the placeholders are translated recursively. Names that come from the game save (towns,
  industries, cargo) pass through unchanged — they are already in the game's language.
* Text that is glued from several pieces is split on common separators, and leftover
  catalogue phrases are replaced inside it.

The language of a request lives in a ContextVar (``set_lang``); ``localize`` walks a
JSON-like structure and translates every string value.
"""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Any

LANGS = ("ru", "en")
DEFAULT_LANG = "ru"
CYR = re.compile("[А-Яа-яЁё]")

_lang: ContextVar[str] = ContextVar("lang", default=DEFAULT_LANG)

CATALOG: dict[str, str] = json.loads(
    (Path(__file__).parent / "locales" / "en.json").read_text(encoding="utf-8"))

_PH = re.compile(r"\{\{|\}\}|\{(\w*)\}")


def _parts(template: str) -> list[tuple[str, str | None]]:
    """Split a template into (text, None) and (None-text, placeholder name) pieces."""
    out: list[tuple[str, str | None]] = []
    pos, buf = 0, []
    for m in _PH.finditer(template):
        buf.append(template[pos:m.start()])
        if m.group(0) in ("{{", "}}"):
            buf.append(m.group(0)[0])
        else:
            out.append(("".join(buf), None))
            buf = []
            out.append(("", m.group(1)))
        pos = m.end()
    buf.append(template[pos:])
    out.append(("".join(buf), None))
    return out


def _commas(escaped: str) -> str:
    """Some planner texts have all commas replaced by spaces (to drop thousand separators):
    a comma of a template also matches a space."""
    return escaped.replace(",", "[, ]")


class _Template:
    __slots__ = ("rx", "srx", "names", "en", "weight", "anchor", "pieces", "alias")

    def __init__(self, ru: str, en: str):
        # tokens: ("s", text) / ("p", name); empty texts dropped
        toks = [("s", t) if n is None else ("p", n) for t, n in _parts(ru) if n is not None or t]
        en_toks = [("s", t) if n is None else ("p", n) for t, n in _parts(en) if n is not None or t]
        # Values written next to each other ("{0}{1}", "{0} {1}") cannot be split reliably,
        # so one group carries them all; the other placeholders render empty. Only when the
        # English template keeps them together in the same order.
        groups: list[list[str]] = []
        i = 0
        while i < len(toks):
            if toks[i][0] != "p":
                i += 1
                continue
            run, j = [toks[i][1]], i + 1
            while True:
                if j < len(toks) and toks[j][0] == "p":
                    run.append(toks[j][1])
                    j += 1
                elif (j + 1 < len(toks) and toks[j][0] == "s" and not toks[j][1].strip()
                      and toks[j + 1][0] == "p"):
                    run.append(toks[j + 1][1])
                    j += 2
                else:
                    break
            groups.append(run)
            i = j
        en_seq = "".join("\0" + v if k == "p" else ("" if not v.strip() else "\1") for k, v in en_toks)
        self.alias: dict[str, str] = {}
        merged = []
        for run in groups:
            if len(run) > 1 and "".join("\0" + n for n in run) in en_seq:
                for n in run[1:]:
                    self.alias[n] = run[0]
                merged.append(run)
        member = {n: run[0] for run in merged for n in run}
        rx, srx, names, static = [], [], [], []
        last_p = max(k for k, (kind, _) in enumerate(toks) if kind == "p")
        k = 0
        while k < len(toks):
            kind, val = toks[k]
            if kind == "s":
                rx.append(_commas(re.escape(val)))
                # inside longer text a template must not start or end in the middle of a word
                pre = "(?<![А-Яа-яЁё])" if CYR.match(val[0]) else ""
                post = "(?![А-Яа-яЁё])" if CYR.match(val[-1]) else ""
                srx.append(pre + _commas(re.escape(val)) + post)
                static.append(val)
                k += 1
                continue
            # skip the rest of a merged run (its members and the blanks between them)
            k2 = k + 1
            if val in member:
                while k2 < len(toks) and (toks[k2][0] == "p" and member.get(toks[k2][1]) == val
                                          or toks[k2][0] == "s" and not toks[k2][1].strip()
                                          and k2 + 1 < len(toks) and toks[k2 + 1][0] == "p"
                                          and member.get(toks[k2 + 1][1]) == val):
                    k2 += 1
            rx.append("(.*?)")
            # inside longer text a trailing value runs to the end of its clause
            tail = all(kind2 == "p" for kind2, _ in toks[k2:])
            srx.append(r"(.*?)(?=[;\n]|$)" if tail else "(.*?)")
            names.append(val)
            k = k2
        self.rx = re.compile("".join(rx), re.S)
        self.srx = re.compile("".join(srx), re.S)
        self.names = names
        # English: drop the blanks between members of a merged run
        en_out: list[tuple[str, str | None]] = []
        for idx, (kind, val) in enumerate(en_toks):
            if (kind == "s" and not val.strip() and 0 < idx < len(en_toks) - 1
                    and en_toks[idx - 1][0] == "p" and en_toks[idx + 1][0] == "p"
                    and member.get(en_toks[idx + 1][1]) is not None
                    and member.get(en_toks[idx + 1][1]) == member.get(en_toks[idx - 1][1])):
                continue
            en_out.append((val, None) if kind == "s" else ("", val))
        self.en = en_out
        self.weight = sum(len(s) for s in static)
        cyr = [s for s in static if CYR.search(s)]
        self.anchor = (max(cyr, key=len) if cyr else max(static, key=len)).replace(",", " ")
        self.pieces = [x.replace(",", " ") for x in static if x.strip()]

    def render(self, values: dict[str, str]) -> str:
        return "".join(text if name is None else values.get(name, "") for text, name in self.en)


# "г. Дубна" (the town of Dubna): the abbreviation is dropped in English, and to match the same
# way in keys and in text it is removed from both before a name / a placeholder
_TOWN_ABBR = re.compile(r"(?<![А-Яа-яЁё])г\. (?=\{|)")

_STATIC: dict[str, str] = {}
_TEMPLATES: list[_Template] = []
for _ru, _en in CATALOG.items():
    _ru = _TOWN_ABBR.sub("", _ru)
    if _PH.search(_ru) and any(n is not None for _, n in _parts(_ru)):
        if any(t.strip() for t, n in _parts(_ru) if n is None):   # "{0}" alone would match anything
            _TEMPLATES.append(_Template(_ru, _en))
    else:
        _STATIC[_ru.replace("{{", "{").replace("}}", "}")] = _en.replace("{{", "{").replace("}}", "}")


def _fragment(key: str) -> str:
    """A catalogue phrase inside longer text, never cut out of the middle of a Russian word."""
    pre = "(?<![А-Яа-яЁё])" if CYR.match(key[0]) else ""
    post = "(?![А-Яа-яЁё])" if CYR.match(key[-1]) else ""
    return pre + re.escape(key) + post


_FRAGMENTS = re.compile("|".join(
    _fragment(k) for k in sorted((k for k in _STATIC if len(k.strip()) > 1), key=len, reverse=True)))
_SEPARATORS = ("\n", "; ", " · ", " | ", ". ", ", ")

# units written right after a number by the formatting helpers ("2 481 с", "620 дн.")
_UNITS = {
    "дн.": "days", "мес.": "months", "мин": "min", "км/ч": "km/h", "км": "km", "м³": "m³",
    "м": "m", "с": "s", "т": "t", "ед./год": "units/year", "ед.": "units", "пасс./год": "pass./year",
    "пасс.": "pass.", "млн": "M", "тыс.": "k", "зд.": "bldg.", "лет": "years", "года": "years",
    "год": "year", "г.": "", "шт.": "pcs", "кВт/т": "kW/t", "кВт": "kW",
}
_UNIT_RX = re.compile(
    r"(?<=[\d)])(\s?)(" + "|".join(re.escape(u) for u in sorted(_UNITS, key=len, reverse=True))
    + r")(?![А-Яа-яЁё])")

# abbreviations in front of a number or a name: "ост.3" (stop 3), "г. Дубна" (town Dubna)
_ABBREV = {"ост.": "stop ", "г. ": "", "с. ": ""}
_ABBREV_RX = re.compile(r"(?<![А-Яа-яЁё])(ост\.(?=\s?\d)|г\. (?=\S)|с\. (?=\S))")

# Names from the player's save (towns, industries, cargo, vehicles, stations): they are in
# the game's language and must not be "translated" word by word.
_names_rx: re.Pattern | None = None
_names: list[str] = []


def register_names(names) -> None:
    """Protect proper names (usually every "name" in the export) from translation."""
    global _names_rx, _names
    found = {n.strip() for n in names if isinstance(n, str) and CYR.search(n) and len(n.strip()) > 2}
    found |= {n.lower() for n in found}
    # a "name" that is an ordinary word of our own texts ("Депо") is not a proper name
    common = {k.strip().lower() for k in _STATIC}
    found = {n for n in found if n.lower() not in common}
    _names = sorted(found, key=len, reverse=True)
    _names_rx = (re.compile("(?<![А-Яа-яЁё])(?:" + "|".join(re.escape(n) for n in _names)
                            + ")(?![А-Яа-яЁё])") if _names else None)
    to_english.cache_clear()
    _core.cache_clear()


_NAME_SECTIONS = ("towns", "towns_stats", "industries", "industryTypes", "stations", "lines",
                  "cargoTypes", "vehicles", "multipleUnits", "streetTemplates", "bridgeTypes",
                  "tunnelTypes", "playerConstructions")


def names_from_export(export: dict) -> list[str]:
    """Every ``name`` in the export sections that hold proper names (towns, industries,
    stations, cargo, vehicles, road/bridge/tunnel types)."""
    out: list[str] = []
    stack = [export.get(k) for k in _NAME_SECTIONS if export.get(k)]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "name" and isinstance(v, str):
                    out.append(v)
                elif isinstance(v, (dict, list)) and k not in ("heightmap", "network"):
                    stack.append(v)
        elif isinstance(o, list):
            stack.extend(v for v in o if isinstance(v, (dict, list)))
    return out


def set_lang(lang: str | None) -> str:
    """Set the language for the current request; unknown values fall back to Russian."""
    value = (lang or "").strip().lower()[:2]
    value = value if value in LANGS else DEFAULT_LANG
    _lang.set(value)
    return value


def get_lang() -> str:
    return _lang.get()


_MASK = re.compile("(\\d+)")
_NUMBERED = re.compile(r"(\s*\d+\. )(.*)", re.S)


@lru_cache(maxsize=200_000)
def to_english(text: str) -> str:
    """English version of one Russian string (unchanged if nothing is recognised)."""
    if not text or not CYR.search(text):
        return text
    masked = text
    if _names_rx is not None:
        index = {n: i for i, n in enumerate(_names)}
        masked = _names_rx.sub(lambda m: f"{index[m.group(0)]}", text)
    masked = _TOWN_ABBR.sub("", masked)
    out = _core(masked)
    return _MASK.sub(lambda m: _names[int(m.group(1))], out)


def _closing_bracket(text: str) -> int:
    depth = 0
    for i, c in enumerate(text):
        depth += c == "["
        depth -= c == "]"
        if depth == 0:
            return i
    return -1


def _whole(text: str) -> str | None:
    """Translation of ``text`` as one known sentence (a catalogue phrase or a template with real
    literal text covering all of it), else None."""
    hit = _STATIC.get(text)
    if hit is not None:
        return hit
    flat = text.replace(",", " ")
    for t in sorted((t for t in _TEMPLATES if t.weight >= 5 and t.anchor in flat
                     and all(p in flat for p in t.pieces)), key=lambda t: -t.weight):
        m = t.rx.fullmatch(text)
        if m:
            return t.render(dict(zip(t.names, (_core(g) for g in m.groups()))))
    return None


@lru_cache(maxsize=400_000)
def _core(text: str) -> str:
    if not text or not CYR.search(text):
        return text
    hit = _STATIC.get(text)
    if hit is not None:
        return hit
    stripped = text.strip()
    if stripped != text and stripped in _STATIC:
        return text.replace(stripped, _STATIC[stripped])
    # "12. [Tag: A → B] text": step number and tag are not part of any template
    m = _NUMBERED.fullmatch(text)
    if m:
        return m.group(1) + _core(m.group(2))
    if text.startswith("["):
        end = _closing_bracket(text)
        if end > 0:
            return "[" + _core(text[1:end]) + "]" + _core(text[end + 1:])
    # The most specific match wins: a template that covers the whole text, a template
    # found inside it, or a plain catalogue phrase inside it — whichever has the most
    # literal text. Weak templates ("{0} {1} м") must not swallow a known sentence.
    flat = text.replace(",", " ")
    candidates = [t for t in _TEMPLATES if t.anchor in flat and all(p in flat for p in t.pieces)]
    best = None  # (weight, kind, start, end, template, match)
    for t in candidates:
        m = t.rx.fullmatch(text)
        if m and (best is None or t.weight > best[0]):
            best = (t.weight, "full", 0, len(text), t, m)
    for t in candidates:
        if t.weight < 8:        # too little literal text to be recognised inside other text
            continue
        m = t.srx.search(text)
        if m and m.end() > m.start() and (best is None or t.weight > best[0]):
            best = (t.weight, "part", m.start(), m.end(), t, m)
    for m in _FRAGMENTS.finditer(text):
        w = len(m.group(0).strip())
        if w >= 4 and (best is None or w > best[0]):
            best = (w, "phrase", m.start(), m.end(), None, m)
    full = next((t for t in sorted(candidates, key=lambda t: -t.weight) if t.rx.fullmatch(text)), None)
    # (a full template with almost no literal text, like "{0} м", proves nothing about structure)
    if full is not None and full.weight >= 5 and best is not None and best[1] != "full":
        fm = full.rx.fullmatch(text)
        spans = [fm.span(g) for g in range(1, (fm.lastindex or 0) + 1)]
        if any(s0 <= best[2] and best[3] <= s1 for s0, s1 in spans):
            best = (full.weight, "full", 0, len(text), full, fm)   # inside a value: recursion handles it
    if best is None or best[1] != "full":
        # extra indentation in front of a sentence, or a sentence capitalised by the code
        # ("Участок 1 …" from "участок {0} …"): use the plain form if it is a known sentence
        rest = text.lstrip()
        if rest != text and _whole(rest) is not None:
            return text[:len(text) - len(rest)] + _whole(rest)
        if text[0].isupper() and CYR.match(text[0]):
            out = _whole(text[0].lower() + text[1:])
            if out is not None:
                return out[:1].upper() + out[1:]
    if best is None:
        # no template here: a list of clauses ("120 м на юг по улице, 264 м …") is
        # translated clause by clause before falling back to single phrases
        for sep in _SEPARATORS:
            if sep in text:
                pieces = text.split(sep)
                done = [_core(p) for p in pieces]
                if done != pieces:
                    return sep.join(done)
    if best is not None:
        _, kind, start, end, t, m = best
        if kind == "phrase":
            inner = _STATIC[m.group(0)]
        else:
            inner = t.render(dict(zip(t.names, (_core(g) for g in m.groups()))))
        if kind == "full":
            return inner
        return _core(text[:start]) + inner + _core(text[end:])
    text = _ABBREV_RX.sub(lambda m: _ABBREV[m.group(1)], text)
    text = _UNIT_RX.sub(lambda m: (m.group(1) + _UNITS[m.group(2)]) if _UNITS[m.group(2)] else "", text)
    return _FRAGMENTS.sub(lambda m: _STATIC[m.group(0)], text)


def translate(text: str, lang: str | None = None) -> str:
    if (lang or _lang.get()) != "en":
        return text
    return to_english(text)


def localize(obj: Any, lang: str | None = None) -> Any:
    """Copy of a JSON-like structure with every string value translated (keys are kept)."""
    if (lang or _lang.get()) != "en":
        return obj
    if isinstance(obj, str):
        return to_english(obj)
    if isinstance(obj, dict):
        return {k: localize(v, "en") for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [localize(v, "en") for v in obj]
    return obj

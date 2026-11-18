#!/usr/bin/env python3
"""The trip's plan of real places, built before the model writes anything.

Usage: python3 scripts/prepare.py "<destination in English>" "<first day YYYY/M/D, or ->" <days> "<wishes in English>" [zh|en]

The model used to pick every place from memory, and the places were looked up only after the plan
was written, too late to change it. This script reads the destination's Wikivoyage region page, takes
its towns, parks and districts and what there is to see and do in each (opening months checked
against the travel dates), and lays out the days with skeleton.py:

    Day 4 | Sapporo → Daisetsuzan National Park | Morning: Hoheikyo Onsen | Afternoon: 旭岳 (Asahi-dake)

It writes that plan as the trip JSON itself (acer-travel-plan-<dest>-<N>d-v1.5.0.json) and starts
fill.py on it in the background, so the page is there while the model is still writing. The model
then writes only the words (titles, notes, meals) to the trip's notes file, which fill.py merges.
The looked-up spots go to a hidden file beside the trip (details.candidates_path), one per trip;
fill.py and research.py take the hours, price and website from it.

When no day plan can be made (no listings), it prints the spots as a table instead, and the model
writes the whole trip JSON to the trip file.

The exec tool moves a command that runs past ~10 s to the background, so everything here stops by
TIME_LIMIT_S and prints what it has. Results are cached for CACHE_TTL_SECONDS per destination.
"""
from __future__ import annotations

import html
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import enrich
import fill
import skeleton

SEARCH_COUNTER = Path("/tmp/acer-travel-plan/search-count.json")
CACHE_FILE = "prepare-cache.json"
CACHE_TTL_SECONDS = 7 * 24 * 3600
TIME_LIMIT_S = 7.0
MAX_TOWNS = 21
PAGES_PER_REQUEST = 4
# Wikimedia answers HTTP 429 to a client that sends too much at once (seen while testing, with the
# Journey's prefetch running too): at most this many requests at a time.
WIKI_PARALLEL = 3
_wiki_slots = threading.BoundedSemaphore(WIKI_PARALLEL)
PER_TOWN = 4
PER_WISHED_TOWN = 7
MAX_ROWS = 36
NAME_CHARS = 34
TOWN_CHARS = 22
HOURS_CHARS = 24
PRICE_CHARS = 16
DESCRIPTION_CHARS = 220
EATS_PER_TOWN = 3
# Region page sections that list the places to stay in or visit.
TOWN_SECTIONS = ("Cities", "Towns", "Districts", "Boroughs", "Other destinations", "National parks")
# Big regions have few listings of their own: used only when a page names few towns.
FEW_TOWNS = 5
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
MONTH_RANGE_RE = re.compile(
    MONTH_RE + r"\s*(?:\d{1,2}\s*)?(?:-|–|—|to|until|through)\s*(?:(?:early|mid|late)\s+)?(?:\d{1,2}\s*)?" + MONTH_RE, re.I)
SINGLE_MONTH_OPEN_RE = re.compile(r"\b(?:only|open)\s+(?:in\s+)?" + MONTH_RE, re.I)
CLOSED_IN_WINTER_RE = re.compile(r"\bclosed\b[^.;]{0,40}\bwinter\b|\bwinter\b[^.;]{0,20}\bclosed\b", re.I)
CLOSED_IN_SUMMER_RE = re.compile(r"\bclosed\b[^.;]{0,40}\bsummer\b|\bsummer\b[^.;]{0,20}\bclosed\b", re.I)
WINTER_ONLY_RE = re.compile(r"\b(?:winter|ski(?:ing)? season) only\b|\bonly (?:in|during) (?:the )?winter\b", re.I)
SUMMER_ONLY_RE = re.compile(r"\bsummer only\b|\bonly (?:in|during) (?:the )?summer\b", re.I)
# What a wish may be about, and the words that show a listing is about it.
WISH_WORDS = {
    "ski": ("ski", "snowboard", "powder", "slope", "gondola", "lift"),
    "snow": ("snow", "ski", "ice", "winter"),
    "hik": ("hike", "hiking", "trail", "trek", "climb", "summit", "peak", "mountain", "walk"),
    "trek": ("hike", "trail", "trek", "summit", "mountain"),
    "onsen": ("onsen", "hot spring", "spa", "bath"),
    "hot spring": ("onsen", "hot spring", "spa", "bath"),
    "food": ("market", "food", "restaurant", "ramen", "seafood", "brewery", "sake", "cheese"),
    "museum": ("museum", "gallery", "exhibit"),
    "temple": ("temple", "shrine", "castle"),
    "history": ("museum", "castle", "historic", "fort", "temple", "shrine", "heritage"),
    "nature": ("park", "lake", "falls", "gorge", "garden", "mountain", "forest", "hill"),
    "family": ("zoo", "aquarium", "park", "farm", "factory"),
    "shop": ("market", "shopping", "street", "arcade", "outlet"),
    "flower": ("flower", "garden", "lavender", "farm", "hill"),
}


# ── input ─────────────────────────────────────────────────────────────────

def parse_start(raw: str) -> date | None:
    match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", raw or "")
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def travel_months(start: date | None, days: int) -> list[int]:
    """The months the trip is in, in order; [] when the dates are unknown."""
    if not start:
        return []
    months: list[int] = []
    for offset in range(max(1, days)):
        month = (start + timedelta(days=offset)).month
        if month not in months:
            months.append(month)
    return months


def letters(text: str) -> str:
    """Lowercase letters only: "Asahi-dake" and "Asahidake" read the same."""
    return re.sub(r"[^a-z]", "", fill.fold(text or ""))


def wish_terms(wishes: str) -> tuple[set[str], set[str]]:
    """(words that show a listing is about a wish, place names the traveller asked for)."""
    folded = fill.fold(wishes or "")
    topics: set[str] = set()
    for key, words in WISH_WORDS.items():
        if key in folded:
            topics.update(words)
    names = {letters(w) for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", wishes or "")}
    names -= {letters(w) for w in WISH_WORDS} | {"skiing", "hiking", "trip", "route", "routes", "plan", "with",
                                                 "and", "days", "local", "trekking", "travel", "family"}
    return topics, {n for n in names if len(n) >= 4}


# ── Wikivoyage ────────────────────────────────────────────────────────────

def page_text(page: dict) -> str:
    revisions = page.get("revisions") or []
    return (revisions[0].get("slots", {}).get("main", {}).get("content") if revisions else "") or ""


DISAMBIGUATION_RE = re.compile(r"\{\{\s*(?:disamb|dab\b|disambiguation)", re.I)


def region_page(dest: str) -> tuple[str, str]:
    """The destination's own Wikivoyage page: (title, wikitext), or ("", "")."""
    words = fill.place_words(dest)
    data = fill.wiki_get("en.wikivoyage.org", {
        "action": "query", "generator": "search", "gsrsearch": dest, "gsrlimit": "3",
        "prop": "revisions", "rvprop": "content", "rvslots": "main",
    })
    pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
    for page in pages:
        text = page_text(page)
        # "New York" is a page of links to the state and the city: the next hit is the place.
        if DISAMBIGUATION_RE.search(text):
            continue
        if not words or words[0] in fill.fold(page.get("title", "")):
            return page.get("title", ""), text
    return "", ""


def section(text: str, name: str) -> str:
    """The body of a "== name ==" (or "=== name ===") section with its subsections ("Districts" lists
    Tokyo's wards under "Central Tokyo", "Suburbs"...), up to the next heading of its level or above."""
    match = re.search(r"^(=+)\s*" + re.escape(name) + r"\s*\1\s*$", text, re.M)
    if not match:
        return ""
    level = len(match.group(1))
    rest = text[match.end():]
    for heading in re.finditer(r"^(=+)[^=\n]+\1\s*$", rest, re.M):
        if len(heading.group(1)) <= level:
            return rest[:heading.start()]
    return rest


def towns_of(text: str) -> list[str]:
    """The towns, districts and parks a region page lists, in its order."""
    found = linked_places(text, TOWN_SECTIONS)
    if len(found) < FEW_TOWNS:
        found += [t for t in linked_places(text, ("Regions",)) if t not in found]
    return found


def linked_places(text: str, sections: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for name in sections:
        body = section(text, name)
        for line in body.splitlines():
            if not line.lstrip().startswith("*") and "region" not in line:
                continue
            for target in re.findall(r"\[\[([^\]|#]+)", line):
                target = target.strip()
                if ":" in target or target in found:
                    continue
                found.append(target)
                break  # one place per line: the rest of the line describes it
    return found


MARKER_POINT_RE = re.compile(r"\blat\s*=\s*(-?\d+(?:\.\d+)?)\s*\|\s*long\s*=\s*(-?\d+(?:\.\d+)?)")
REACH_MARGIN_KM = 120


def reach_of(text: str) -> float | None:
    """How far the destination's own towns and parks spread: from the middle of the region page's
    map markers to the farthest one, plus a margin (Hokkaido: about 360 km). None without markers."""
    body = "".join(section(text, name) for name in TOWN_SECTIONS)
    points = [(float(a), float(b)) for a, b in MARKER_POINT_RE.findall(body)]
    if len(points) < 2:
        return None
    middle = (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))
    return round(max(fill.distance_km(middle, p) for p in points) + REACH_MARGIN_KM)


def save_reach(dest: str, km: float | None) -> None:
    """Saved where fill.use_reach_of() reads it."""
    if not km:
        return
    path = cache_path().with_name(fill.REACH_FILE)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved = saved if isinstance(saved, dict) else {}
    except (OSError, ValueError):
        saved = {}
    saved[fill.fold(dest)] = km
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(saved), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass


MARKER_RE = re.compile(r"name\s*=\s*\[\[([^\]|#]+)[^}]*?\blat\s*=\s*(-?\d+(?:\.\d+)?)\s*\|\s*long\s*=\s*(-?\d+(?:\.\d+)?)")


def town_points(text: str) -> dict[str, list[float]]:
    """{town: [lat, lon]} from the region page's map markers."""
    body = "".join(section(text, name) for name in TOWN_SECTIONS)
    return {m.group(1).strip(): [float(m.group(2)), float(m.group(3))] for m in MARKER_RE.finditer(body)}


def southern(text: str) -> bool:
    """The first map marker of the page is south of the equator (seasons are the other way round)."""
    match = re.search(r"\blat\s*=\s*(-?\d+(?:\.\d+)?)", text)
    return bool(match) and float(match.group(1)) < 0


def town_pages(titles: list[str]) -> dict[str, str]:
    """Wikitext of several pages in one request: {requested title: text}."""
    with _wiki_slots:
        data = fill.wiki_get("en.wikivoyage.org", {
            "action": "query", "titles": "|".join(titles), "prop": "revisions", "rvprop": "content",
            "rvslots": "main", "redirects": "1",
        })
    query = data.get("query", {})
    # Map a redirect's target back to the title that was asked for.
    asked = {t: t for t in titles}
    for hop in query.get("normalized", []) + query.get("redirects", []):
        for original, target in list(asked.items()):
            if target == hop.get("from"):
                asked[original] = hop.get("to", target)
    by_title = {p.get("title", ""): page_text(p) for p in query.get("pages", [])}
    return {original: by_title.get(target, "") for original, target in asked.items()}


def plain(value) -> str:
    """Wiki markup and entities ("&yen;1000") as plain text."""
    return re.sub(r"\s+", " ", html.unescape(enrich.clean_markup(str(value or "")))).strip()


# A sightseeing pass ("New York CityPASS", "GoCity Pass") is a ticket, not a place to visit.
TICKET_RE = re.compile(r"\bpass(?:es)?\b|citypass|\btickets?\b", re.I)


def listings_of(text: str, page: str) -> list[dict]:
    """The see/do listings of a page that name a place (not "Skiing" or "Onsen")."""
    found = []
    for listing in enrich.parse_listings(text):
        name = listing.get("name", "").strip()
        if listing["kind"] not in ("see", "do") or not name or enrich.GENERIC_ACTIVITY_RE.match(name)                 or TICKET_RE.search(name):
            continue
        point = enrich.listing_point(listing)
        found.append({
            "name": name,
            "kind": listing["kind"],
            "description": plain(listing.get("content") or listing.get("description"))[:DESCRIPTION_CHARS],
            "hours": plain(listing.get("hours")),
            "price": plain(listing.get("price")),
            "website": listing.get("url", ""),
            "coords": list(point) if point else None,
            "guideUrl": enrich.page_url("en.wikivoyage.org", page),
        })
    return found


def eats_of(text: str) -> list[str]:
    """The names of a page's eat listings (the skeleton's meals until the model writes its own)."""
    return [l["name"].strip() for l in enrich.parse_listings(text)
            if l["kind"] == "eat" and l.get("name", "").strip()][:EATS_PER_TOWN]


EAT_NOTE_CHARS = 90


def eat_notes_of(text: str) -> dict[str, str]:
    """What each eat listing serves, in the guide's words ("Aji No Tokeidai": "Miso ramen chain..."):
    the model picks each meal's dish from it instead of guessing one from the name."""
    notes = {}
    for listing in enrich.parse_listings(text):
        name = listing.get("name", "").strip()
        if listing["kind"] == "eat" and name:
            about = plain(listing.get("content") or listing.get("description"))
            if about:
                notes[name] = about[:EAT_NOTE_CHARS].rstrip() + ("…" if len(about) > EAT_NOTE_CHARS else "")
    return notes


# ── climate ───────────────────────────────────────────────────────────────

NUMBER = r"\s*\|\s*(-?\d+(?:\.\d+)?)"
CLIMATE_RE = re.compile(r"\{\{\s*climate chart\s*\|\s*([^|}]+?)\s*" + NUMBER * 36, re.I)
SNOW_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)snow\s*=\s*(\d+(?:\.\d+)?)", re.I)


def climate_of(text: str) -> dict | None:
    """A page's {{climate chart}}: {"place", "low", "high", "precip", "snow"}, 12 months each (snow may be [])."""
    match = CLIMATE_RE.search(text or "")
    if not match:
        return None
    numbers = [float(v) for v in match.groups()[1:]]
    snow = [0.0] * 12
    found_snow = False
    for month, amount in SNOW_RE.findall(text[match.start():match.start() + 2000]):
        snow[MONTHS[month.lower()] - 1] = float(amount)
        found_snow = True
    return {"place": match.group(1).strip(), "low": numbers[0::3], "high": numbers[1::3],
            "precip": numbers[2::3], "snow": snow if found_snow else []}


def climate_numbers(climate: dict | None, months: list[int]) -> tuple[int, int, int, bool] | None:
    """The travel months' averages, rounded (a rough idea only): (low C, high C, mm a month, snow)."""
    if not climate or not months:
        return None
    low = min(climate["low"][m - 1] for m in months)
    high = max(climate["high"][m - 1] for m in months)
    precip = sum(climate["precip"][m - 1] for m in months) / len(months)
    snowy = bool(climate.get("snow")) and sum(climate["snow"][m - 1] for m in months) > 0
    return round(low), round(high), round(precip / 10) * 10, snowy


def month_span(months: list[int], language: str = "en") -> str:
    """"Mar–Apr", or "3月–4月" in a Chinese plan."""
    if not months:
        return ""
    ends = [months[0], months[-1]] if len(months) > 1 else [months[0]]
    return "–".join(f"{m}月" if language == "zh" else date(2000, m, 1).strftime("%b") for m in ends)


def climate_line(climate: dict | None, months: list[int]) -> str:
    """One line for the plan's climate."""
    numbers = climate_numbers(climate, months)
    if not numbers:
        return ""
    low, high, mm, snowy = numbers
    return (f"Climate ({climate['place']}, {month_span(months)}, long-term averages): {low} to {high} C, "
            f"about {mm} mm of {'rain and snow' if snowy else 'rain'} a month.")


# ── seasons ───────────────────────────────────────────────────────────────

def months_between(first: int, last: int) -> set[int]:
    months, month = {first}, first
    while month != last:
        month = month % 12 + 1
        months.add(month)
    return months


def season(spot: dict, months: list[int], south: bool = False, snowy: bool = False) -> str:
    """"✓" open in the travel months, "✗" closed then, "?" when the guide does not say. Where it
    snows in the travel months, "closed in winter" also covers the snow months around it (a
    Hokkaido ropeway or mountain road closed for the winter is still closed in March and April)."""
    if not months:
        return "?"
    # Open months are read from the hours only: a description's "From May-Oct, a steam train runs"
    # is about something else there. "Closed in winter" is read from both.
    hours = str(spot.get("hours") or "")
    text = f"{hours} {spot.get('description', '')}"
    travel = set(months)
    winter = {6, 7, 8} if south else {12, 1, 2}
    if snowy:
        winter |= {5, 9, 10} if south else {11, 3, 4}
    summer = {12, 1, 2} if south else {6, 7, 8}
    if (CLOSED_IN_WINTER_RE.search(text) and travel <= winter) or (CLOSED_IN_SUMMER_RE.search(text) and travel <= summer):
        return "✗"
    if (WINTER_ONLY_RE.search(text) and not travel & winter) or (SUMMER_ONLY_RE.search(text) and not travel & summer):
        return "✗"
    open_months: set[int] = set()
    for match in MONTH_RANGE_RE.finditer(hours):
        open_months |= months_between(MONTHS[match.group(1).lower()[:3]], MONTHS[match.group(2).lower()[:3]])
    for match in SINGLE_MONTH_OPEN_RE.finditer(hours):
        open_months.add(MONTHS[match.group(1).lower()[:3]])
    if not open_months:
        return "?"
    return "✓" if travel & open_months else "✗"


# ── choosing and printing ─────────────────────────────────────────────────

def score(spot: dict, topics: set[str], names: set[str]) -> float:
    name = fill.fold(spot["name"])
    text = name + " " + fill.fold(spot.get("description", ""))
    points = sum(3 for t in topics if t in name) + sum(1 for t in topics if t in text)
    points += sum(6 for n in names if n in letters(spot["name"]))
    if spot.get("hours") or spot.get("price"):
        points += 0.5  # a listing with practical details is worth more to the plan
    return points


BLOOM_RE = re.compile(r"lavender|flower (?:field|farm|garden|hill)|tulip|sunflower|blossom|花田|薰衣草", re.I)
# An event, not a place: in the plan only when its listing names the travel month.
EVENT_RE = re.compile(r"festival|fantasy|matsuri|fireworks|illumination|parade|marathon|carnival|祭", re.I)

def wished_towns(towns: list[str], listings: dict[str, list[dict]], names: set[str]) -> list[str]:
    """The towns the traveller named, or whose places they named ("Asahidake" → Daisetsuzan)."""
    return [t for t in towns if any(n in letters(t) for n in names)
            or any(n in letters(s["name"]) for s in listings.get(t, []) for n in names)]


def choose(towns: list[str], listings: dict[str, list[dict]], topics: set[str], names: set[str]) -> list[dict]:
    """Up to MAX_ROWS spots: the towns the traveller asked for first, the best of each town."""
    wished = wished_towns(towns, listings, names)
    ordered = wished + [t for t in towns if t not in wished]
    chosen: list[dict] = []
    for town in ordered:
        ranked = sorted(listings.get(town, []), key=lambda s: -score(s, topics, names))
        limit = PER_WISHED_TOWN if town in wished else PER_TOWN
        for spot in ranked[:limit]:
            chosen.append({**spot, "town": town.split("/")[-1]})
    # Over the limit: keep the best-scored, then put them back in town order.
    if len(chosen) > MAX_ROWS:
        best = sorted(range(len(chosen)), key=lambda i: -score(chosen[i], topics, names))[:MAX_ROWS]
        chosen = [chosen[i] for i in sorted(best)]
    return chosen


def short(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", html.unescape(enrich.clean_markup(str(text or "")))).replace("|", "/").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def table(spots: dict[str, dict]) -> list[str]:
    lines = ["中文 | name | town | kind | season | hours | price"]
    for spot in spots.values():
        lines.append(" | ".join([
            spot.get("zh") or "-", short(spot["name"], NAME_CHARS), short(spot["town"], TOWN_CHARS), spot["kind"], spot["season"],
            short(spot.get("hours"), HOURS_CHARS) or "-", short(spot.get("price"), PRICE_CHARS) or "-",
        ]))
    return lines


# ── Chinese names ─────────────────────────────────────────────────────────

ZH_CACHE_FILE = "zh-names.json"
ZH_CACHE_TTL_SECONDS = 30 * 24 * 3600


def zh_names(names: list[str]) -> dict[str, str]:
    """The Traditional Chinese name of each English place name that has a Chinese Wikipedia
    article ("Asahi-dake" → 旭岳), in two batched requests: the English article's link to Chinese,
    then that article's zh-tw title. A name without one is left out."""
    if not names:
        return {}
    with _wiki_slots:
        data = fill.wiki_get("en.wikipedia.org", {
            "action": "query", "titles": "|".join(names), "prop": "langlinks", "lllang": "zh",
            "lllimit": "max", "redirects": "1",
        })
    query = data.get("query", {})
    asked = {n: n for n in names}
    for hop in query.get("normalized", []) + query.get("redirects", []):
        for original, target in list(asked.items()):
            if target == hop.get("from"):
                asked[original] = hop.get("to", target)
    linked = {p.get("title", ""): ((p.get("langlinks") or [{}])[0].get("title") or "") for p in query.get("pages", [])}
    titles = sorted({t for t in linked.values() if t})
    if not titles:
        return {}
    with _wiki_slots:
        data = fill.wiki_get("zh.wikipedia.org", {
            "action": "query", "titles": "|".join(titles), "prop": "info", "inprop": "varianttitles",
        })
    tw = {p.get("title", ""): (p.get("varianttitles") or {}).get("zh-tw") or p.get("title", "")
          for p in data.get("query", {}).get("pages", [])}
    found = {}
    for name in names:
        zh = tw.get(linked.get(asked[name], ""), "")
        if zh and fill.CJK_RE.search(zh):
            found[name] = re.sub(r"\s*[(（].*?[)）]\s*$", "", zh)  # "旭岳 (北海道)" → 旭岳
    return found


def add_zh_names(spots: dict[str, dict], deadline: float, more: list[str] = ()) -> dict[str, str]:
    """Gives the spots their Chinese names (saved for ZH_CACHE_TTL_SECONDS; a name without one is
    saved too, as "", so it is not asked again). Returns {name: Chinese name} for `more` too (the
    destination and its towns, for the skeleton's titles)."""
    path = cache_path().with_name(ZH_CACHE_FILE)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        saved = saved if isinstance(saved, dict) else {}
    except (OSError, ValueError):
        saved = {}
    now = time.time()
    fresh = {k: v for k, v in saved.items() if isinstance(v, dict) and now - float(v.get("saved", 0)) <= ZH_CACHE_TTL_SECONDS}
    wanted = list(dict.fromkeys([s["name"] for s in spots.values()] + [m for m in more if m]))
    missing = [name for name in wanted if name not in fresh][:50]
    if missing:
        answer = in_time([(zh_names, (missing,))], deadline)[0]
        if answer is not None:
            for name in missing:
                fresh[name] = {"saved": now, "zh": answer.get(name, "")}
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
                os.replace(temp, path)
            except OSError:
                pass
    for spot in spots.values():
        zh = (fresh.get(spot["name"]) or {}).get("zh", "")
        if zh:
            spot["zh"] = zh
    return {name: fresh[name]["zh"] for name in wanted if (fresh.get(name) or {}).get("zh")}


# ── cache ─────────────────────────────────────────────────────────────────

def cache_path() -> Path:
    import research
    return research.cache_dir() / CACHE_FILE


# "Fukuoka and Kumamoto", "Kyoto, Osaka", "札幌、小樽": a trip through several places has no guide
# page of its own. When the whole name finds nothing, each place is looked up and the plan uses
# them all, in the order the traveller named them.
SEVERAL_PLACES_RE = re.compile(r"\s*(?:,|&|\+|/|、|，|和|與|及|\band\b)\s*", re.I)


def find_places(dest: str, deadline: float) -> dict:
    """look_up() of the destination, saved for CACHE_TTL_SECONDS; several named places together."""
    found = load_cached(dest)
    if found:
        return found
    found = look_up(dest, deadline)
    parts = [p for p in SEVERAL_PLACES_RE.split(dest) if p.strip()]
    if not found["listings"] and len(parts) > 1:
        found = merge_places([find_places(part, deadline) for part in parts])
    if found["complete"] and found["listings"]:
        save_cached(dest, found)
    elif not found["listings"]:
        found = load_cached(dest, any_age=True) or found
    return found


def merge_places(founds: list[dict]) -> dict:
    """Several destinations' look_up() results as one: their towns in the order named."""
    merged = {"region": "", "towns": [], "south": False, "listings": {}, "complete": True, "climate": None,
              "eats": {}, "eatNotes": {}, "reachKm": 0, "points": {}}
    for found in founds:
        merged["region"] = merged["region"] or found.get("region") or ""
        merged["towns"] += [t for t in found.get("towns") or [] if t not in merged["towns"]]
        merged["south"] = merged["south"] or bool(found.get("south"))
        merged["complete"] = merged["complete"] and bool(found.get("complete"))
        merged["climate"] = merged["climate"] or found.get("climate")
        merged["reachKm"] = max(merged["reachKm"], found.get("reachKm") or 0)
        for key in ("listings", "eats", "eatNotes", "points"):
            merged[key].update(found.get(key) or {})
    return merged


def load_cached(dest: str, any_age: bool = False) -> dict | None:
    """The saved lookup for a destination while fresh; with any_age, however old (better than
    nothing when the lookup itself failed, e.g. rate-limited)."""
    try:
        entry = json.loads(cache_path().read_text(encoding="utf-8")).get(fill.fold(dest))
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(entry, dict):
        return None
    if any_age:
        return {"climate": None, **entry}  # saved before the climate was kept: still has the spots
    if all(k in entry for k in ("climate", "reachKm", "points", "eats", "eatNotes")) and time.time() - float(entry.get("saved", 0)) <= CACHE_TTL_SECONDS:
        return entry
    return None


def save_cached(dest: str, entry: dict) -> None:
    path = cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    data[fill.fold(dest)] = {**entry, "saved": time.time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass


# ── looking it all up, within the time limit ──────────────────────────────

def in_time(jobs: list, deadline: float) -> list:
    """Runs each (function, args) in a daemon thread; what has not answered by the deadline is
    None (a daemon thread left running does not keep the process alive)."""
    results: list = [None] * len(jobs)

    def run(index, function, args):
        try:
            results[index] = function(*args)
        except Exception as exc:
            print(f"prepare: lookup stopped ({str(exc)[:80]})", file=sys.stderr)

    threads = [threading.Thread(target=run, args=(i, f, a), daemon=True) for i, (f, a) in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return results


def look_up(dest: str, deadline: float) -> dict:
    """{"region", "towns", "south", "listings": {town: [...]}, "complete"} for the destination."""
    found = in_time([(region_page, (dest,))], deadline)[0] or ("", "")
    title, text = found
    if not title:
        return {"region": "", "towns": [], "south": False, "listings": {}, "complete": False, "climate": None, "eats": {}}
    towns = towns_of(text)[:MAX_TOWNS]
    listings: dict[str, list[dict]] = {}
    own = listings_of(text, title)
    if own:  # a city page lists its own sights too
        listings[title] = own
    batches = [towns[i:i + PAGES_PER_REQUEST] for i in range(0, len(towns), PAGES_PER_REQUEST)]
    answers = in_time([(town_pages, (batch,)) for batch in batches], deadline)
    complete = all(a is not None for a in answers)
    climate = climate_of(text)
    pages = {town: page for answer in answers for town, page in (answer or {}).items()}
    eats = {title: eats_of(text)} if own else {}
    eat_notes = eat_notes_of(text) if own else {}
    for town in towns:
        page = pages.get(town, "")
        spots = listings_of(page, town)
        if spots:
            listings[town] = spots
            eats[town] = eats_of(page)
            eat_notes.update(eat_notes_of(page))
        climate = climate or climate_of(page)
    # A region of regions ("Okinawa" lists its islands, "New York City" its boroughs, and they list
    # their towns or districts): one level down, the towns of the places that list no sights
    # themselves, each after its parent.
    if any(town not in listings for town in towns):
        below = {town: [t for t in towns_of(pages.get(town, "")) if t not in towns and t != title]
                 for town in towns if town not in listings}
        deeper = list(dict.fromkeys(t for subs in below.values() for t in subs))[:MAX_TOWNS]
        batches = [deeper[i:i + PAGES_PER_REQUEST] for i in range(0, len(deeper), PAGES_PER_REQUEST)]
        answers = in_time([(town_pages, (batch,)) for batch in batches], deadline)
        complete = complete and all(a is not None for a in answers)
        pages = {town: page for answer in answers for town, page in (answer or {}).items()}
        for town in deeper:
            page = pages.get(town, "")
            spots = listings_of(page, town)
            if spots:
                listings[town] = spots
                eats[town] = eats_of(page)
                eat_notes.update(eat_notes_of(page))
            climate = climate or climate_of(page)
        towns = [t for town in towns for t in [town] + below.get(town, [])]
    ordered = ([title] if own else []) + list(dict.fromkeys(t for t in towns if t in listings))
    return {"region": title, "towns": ordered, "south": southern(text), "listings": listings, "complete": complete,
            "climate": climate, "eats": eats, "eatNotes": eat_notes, "reachKm": reach_of(text), "points": town_points(text)}


def prefetch(dest: str, spots: dict[str, dict]) -> None:
    """Starts the Journey on the table's spots in the background (research.py --prefetch): the
    model takes half a minute or more to write the plan, and by then most spots' descriptions and
    photos are saved, so the plan comes out with them instead of filling in later."""
    slug = re.sub(r"[^a-z0-9]+", "-", fill.fold(dest)).strip("-") or "trip"
    trip = {"title": dest, "days": [{"title": dest, "items": [{"place": s["name"]} for s in spots.values()]}]}
    path = Path("/tmp/acer-travel-plan") / f"acer-travel-plan-{slug}-1d-prefetch.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(trip, ensure_ascii=False), encoding="utf-8")
        subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "research.py"), str(path), "--prefetch"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as exc:
        print(f"prepare: prefetch not started ({exc})", file=sys.stderr)


WEB_RESULTS = 3


def web_search(query: str) -> list[str]:
    """search.js for the wishes (it counts the trip's searches and shows them in the Journey): its
    preview lines "[0] title — snippet"."""
    script = Path(__file__).resolve().parent / "search.js"
    node = fill.find_node()
    if not query or not node:
        return []
    result = subprocess.run([node, str(script), query], capture_output=True, text=True,
                            timeout=TIME_LIMIT_S, stdin=subprocess.DEVNULL)
    return [line for line in result.stdout.splitlines() if re.match(r"\[\d+\] ", line)][:WEB_RESULTS]


def guide_sources(found: dict, towns: list[str]) -> list[dict]:
    """The travel-guide pages the plan was read from: the region's, then each town's in the plan."""
    pages = list(dict.fromkeys([found.get("region") or ""] + list(towns)))
    return [{"title": page.replace("/", " / "), "url": enrich.page_url("en.wikivoyage.org", page),
             "snippet": "", "site": "en.wikivoyage.org"} for page in pages if page][:WEB_RESULTS + 3]


def report_sources(dest: str, results: list[dict]) -> None:
    """Tells the app which pages the plan was read from, as search.js tells it its results, so the
    trip's sources list them (the model searches nothing itself now). Never fails."""
    if not results or not hasattr(socket, "AF_UNIX"):
        return
    call = f"guide-{os.getpid()}-{int(time.time() * 1000)}"
    query = f"Wikivoyage {dest}"
    for event in ({"type": "tool-start", "callId": call, "query": query},
                  {"type": "tool-result", "callId": call, "query": query, "provider": "wikivoyage", "ok": True,
                   "results": results}):
        line = json.dumps({**event, "v": 1, "script": "web_search", "pid": os.getpid(), "ts": int(time.time() * 1000)},
                          ensure_ascii=False)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(fill.LIVE_SOCKET)
                sock.sendall((line + "\n").encode("utf-8"))
        except OSError:
            return


def workspace() -> Path:
    return Path(os.environ.get("ACER_TRAVEL_WORKSPACE") or Path.home() / ".openclaw" / "workspace")


TRIP_VERSION = "v1.5.0"


def trip_path(dest: str, days: int) -> Path:
    """Where the trip is written: acer-travel-plan-<dest>-<N>d-v1.5.0.json in the workspace."""
    slug = re.sub(r"[^a-z0-9]+", "-", fill.fold(dest)).strip("-") or "trip"
    return workspace() / f"acer-travel-plan-{slug}-{days}d-{TRIP_VERSION}.json"


# The skeleton's own words, in the trip's language (the model's notes replace most of them).
SKELETON_WORDS = {
    "zh": {"title": "{dest} {days} 天行程", "subtitle": "依旅遊指南的真實景點排出的路線", "style": "自由行",
           "arrival": "抵達{town}", "departure": "{town}返程", "free": "{town}自由活動",
           "unknown": "未定",
           "meal": "{town}的推薦餐廳", "local_food": "{town}在地美食", "stay": "{town}住宿",
           "nights": "住 {n} 晚", "sep": "、"},
    "en": {"title": "{dest} {days}-day trip", "subtitle": "A route of real places from the travel guide", "style": "Independent travel",
           "arrival": "Arrival in {town}", "departure": "Departure from {town}", "free": "Free time in {town}",
           "unknown": "Not set",
           "meal": "Recommended in {town}", "local_food": "Local food in {town}", "stay": "Stay in {town}",
           "nights": "{n} nights", "sep": ", "},
}


# The rain in words a traveller reads at a glance, by the month's average (mm): the figure itself
# means little to most people.
# Rainy season or not, from the long-term averages: the travel months are the rainy season when they
# get clearly more than the year's average month (RAINY_FACTOR times, and at least RAINY_MIN_MM).
RAINY_FACTOR = 1.3
RAINY_MIN_MM = 150
RAIN_WORDS = {"zh": ("非雨季", "雨季"), "en": ("Not the rainy season", "Rainy season")}
SNOW_WORDS = {"zh": "可能下雪", "en": "snow possible"}


def rainy_season(climate: dict | None, months: list[int]) -> bool:
    if not climate or not months:
        return False
    year = sum(climate["precip"]) / 12
    travel = sum(climate["precip"][m - 1] for m in months) / len(months)
    return travel >= RAINY_MIN_MM and travel >= RAINY_FACTOR * year


def rain_words(rainy: bool, snowy: bool, language: str) -> str:
    """"非雨季", "雨季", with "可能下雪" when the months see snow (a rough idea: the days may differ)."""
    key = "zh" if language == "zh" else "en"
    rain = RAIN_WORDS[key][1 if rainy else 0]
    if not snowy:
        return rain
    return f"{rain}，{SNOW_WORDS[key]}" if key == "zh" else f"{rain}, {SNOW_WORDS[key]}"


def skeleton_trip(plan: list[dict], spots: dict[str, dict], found: dict, dest: str, days: int, start: date | None,
                  months: list[int], language: str, zh: dict[str, str], best=lambda spot: 0) -> dict:
    """The trip JSON of the day plan, complete enough for fill.py to build its page at once: the
    places are set, the notes are empty until the model writes them (fill.py merges its notes
    file), and the meals, stays, climate and highlights are what the guide gives."""
    words = SKELETON_WORDS["zh" if language == "zh" else "en"]
    chinese = language == "zh"

    def town_name(town: str) -> str:
        town = town.split("/")[-1]
        return zh.get(town, town) if chinese else town

    code_of = {spot["name"]: code for code, spot in spots.items()}
    trip_days = []
    nights: dict[str, int] = {}
    for day in plan:
        items = []
        for item in day["items"]:
            town = town_name(item["town"])
            if item["spot"]:
                spot = item["spot"]
                place = skeleton.name_of(spot) if chinese else spot["name"]
                items.append({"time": item["time"], "place": place, "note": "", "spot": code_of.get(spot["name"], "")})
            else:
                items.append({"time": item["time"], "place": words[item["travel"]].format(town=town), "note": "",
                              "travel": item["travel"]})
        last = day["town"].split(" → ")[-1]
        nights[last] = nights.get(last, 0) + 1
        title = " → ".join(town_name(t) for t in day["town"].split(" → ") if t)
        entry = {"title": title, "items": items}
        if day is not plan[-1]:
            entry["stay"] = town_name(last)  # the night is spent where the day ends
        trip_days.append(entry)

    visited = list(dict.fromkeys(item["town"] for day in plan for item in day["items"] if item["town"]))
    # The plan's best places (`best`: the traveller's wishes first), then any other, then the towns:
    # the page needs three.
    ranked = sorted(skeleton.used_spots(plan), key=lambda s: -best(s)) + list(spots.values())
    named = [skeleton.name_of(s) if chinese else s["name"] for s in ranked]
    highlights = list(dict.fromkeys(named + [town_name(t) for t in visited]))[:3]
    meals = []
    for town in visited:
        for name in (found.get("eats") or {}).get(town, []):
            if len(meals) < 3 and name not in [m["name"] for m in meals]:
                meals.append({"name": name, "dish": "", "area": town_name(town),
                              "note": words["meal"].format(town=town_name(town))})
    while len(meals) < 3 and visited:
        town = visited[min(len(meals), len(visited) - 1)]
        meals.append({"name": words["local_food"].format(town=town_name(town)), "dish": "", "area": town_name(town),
                      "note": words["local_food"].format(town=town_name(town))})
    stays = [{"name": words["stay"].format(town=town_name(town)), "area": town_name(town),
              "note": words["nights"].format(n=n)}
             for town, n in sorted(nights.items(), key=lambda kv: -kv[1])[:2]]

    numbers = climate_numbers(found.get("climate"), months)
    if numbers:
        low, high, mm, snowy = numbers
        climate = {"season": month_span(months, language), "temp": f"{low}-{high} C",
                   "rain": rain_words(rainy_season(found.get("climate"), months), snowy, language)}
    else:
        climate = {"season": month_span(months, language) or words["unknown"], "temp": "-", "rain": "-"}

    trip = {
        "title": words["title"].format(dest=zh.get(found.get("region") or dest, dest) if chinese else dest, days=days),
        "subtitle": words["subtitle"], "tripStyle": words["style"], "climate": climate,
        "highlights": highlights, "meals": meals, "stays": stays, "days": trip_days,
    }
    if start:
        trip["startDate"] = f"{start.year}/{start.month}/{start.day}"
    return trip


def fill_command() -> str:
    """The Step 4 command: fill.py with no path, written from ~ when the skill is under it."""
    script = Path(__file__).resolve().parent / "fill.py"
    try:
        return f"python3 ~/{script.relative_to(Path.home()).as_posix()}"
    except ValueError:
        return f"python3 {script}"


def build_page(path: Path) -> None:
    """fill.py on the skeleton, in the background: the plan's page (and the Journey's walk of its
    places) starts while the model is still writing the notes. If the model never runs fill.py, the
    trip still has this page, and the walk's own update merges the notes once they are written."""
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "fill.py"), str(path)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env={**os.environ, "ACER_TRAVEL_DRAFT": "1"})
    except OSError as exc:
        print(f"prepare: page not started ({exc})", file=sys.stderr)


def write_json(path: Path, data: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temp, path)
        return True
    except OSError as exc:
        print(f"prepare: {path.name} not saved ({exc})", file=sys.stderr)
        return False


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print('Usage: python3 scripts/prepare.py "<destination in English>" "<first day YYYY/M/D, or ->" <days> '
              '"<wishes in English>" [zh|en]', file=sys.stderr)
        return 2
    started = time.monotonic()
    dest = argv[1].strip()
    start = parse_start(argv[2]) if len(argv) > 2 else None
    try:
        days = max(1, int(argv[3])) if len(argv) > 3 else 1
    except ValueError:
        days = 1
    wishes = argv[4] if len(argv) > 4 else ""
    language = "zh" if len(argv) > 5 and argv[5].strip().lower().startswith("zh") else "en"

    # A new trip: its web searches are counted afresh.
    try:
        SEARCH_COUNTER.unlink()
    except OSError:
        pass

    # The web search runs beside the travel-guide lookup, within the same time limit.
    query = f"{dest} {wishes}".strip()[:120] if wishes else ""
    web: list = []
    searching = threading.Thread(target=lambda: web.extend(safely(web_search, query)), daemon=True)
    searching.start()
    found = find_places(dest, started + TIME_LIMIT_S)

    months = travel_months(start, days)
    topics, names = wish_terms(wishes)
    south = found.get("south", False)
    snowy = bool((climate_numbers(found.get("climate"), months) or (0, 0, 0, False))[3])
    # Out of flower season the whole trip (a Hokkaido March and April shows no lavender).
    winter = bool(months) and set(months) <= ({4, 5, 6, 7, 8, 9, 10} if south else {10, 11, 12, 1, 2, 3, 4})

    def rank(town: str) -> list[dict]:
        """The town's places, open on the dates, the traveller's wishes first; an event ("Port
        Festival", "Christmas Fantasy") only when its listing names the travel month."""
        places = [{**spot, "town": town.split("/")[-1], "season": season(spot, months, south, snowy)}
                  for spot in found["listings"].get(town, [])]
        places = [p for p in places if p["season"] != "✗" and (p["season"] == "✓" or not EVENT_RE.search(p["name"]))]

        # Lavender fields and flower farms show nothing in winter unless the guide says so.
        places = [p for p in places if not (winter and p["season"] != "✓"
                                            and BLOOM_RE.search(f"{p['name']} {p.get('description', '')}"))]
        return sorted(places, key=lambda p: -score(p, topics, names))

    def value(town: str, places: list[dict]) -> float:
        """How much a town is worth a few days: places that match the wishes, and how many there
        are (skeleton.build weighs it against the way there)."""
        # Capped: a town with many ski listings must not outweigh every road there (Wakkanai).
        return min(sum(score(p, topics, names) for p in places[:6]), 6) + 0.5 * min(len(places), 8)

    plan = skeleton.build(found["towns"], found["listings"], found.get("points") or {}, days,
                          set(wished_towns(found["towns"], found["listings"], names)), rank, value)
    # The plan's places first, then the best others (for a swap, and to match what the model writes).
    chosen = skeleton.used_spots(plan) + choose(found["towns"], found["listings"], topics, names)
    spots: dict[str, dict] = {}
    seen: set[str] = set()
    for spot in chosen:
        if spot["name"] in seen:
            continue
        seen.add(spot["name"])
        spot.setdefault("season", season(spot, months, south, snowy))
        spots[f"S{len(spots) + 1:02d}"] = spot

    towns = [found.get("region") or dest] + list(found["towns"])
    zh = add_zh_names(spots, started + TIME_LIMIT_S, [t.split("/")[-1] for t in towns])
    save_reach(dest, found.get("reachKm"))
    climate = climate_line(found.get("climate"), months)

    trip = trip_path(dest, days)
    candidates = {"dest": dest, "start": start.isoformat() if start else "", "days": days, "months": months,
                  "region": found.get("region", ""), "climate": climate, "created": time.time(),
                  # The places the traveller named: research.py checks their official sites.
                  "wishNames": sorted(names), "spots": spots}
    import details
    write_json(details.candidates_path(trip), candidates)
    try:
        fill.notes_path(trip).unlink()  # the notes of an earlier trip to the same place
    except OSError:
        pass
    planned = skeleton_trip(plan, spots, found, dest, days, start, months, language, zh,
                            lambda spot: score(spot, topics, names)) if plan else None
    built = bool(planned) and write_json(trip, planned)
    if built:
        # fill.py with no path (SKILL.md Step 4) builds this trip: a short command is typed right.
        try:
            (trip.parent / fill.CURRENT_TRIP_FILE).write_text(str(trip) + "\n", encoding="utf-8")
        except OSError:
            pass
        build_page(trip)
        report_sources(dest, guide_sources(found, [item["town"] for day in plan for item in day["items"] if item["town"]]))

    searching.join(max(0.0, started + TIME_LIMIT_S - time.monotonic()))
    if climate:
        print(climate)
    if not spots:
        print(f"No candidates for {dest}. Plan from the web results and what you know.")
        print(f"Trip file: {trip}")
        print_web(list(web))
        return 0
    month_names = ", ".join(date(2000, m, 1).strftime("%b") for m in months) or "unknown dates"
    if built:
        used = {s["name"] for s in skeleton.used_spots(plan)}
        others = [s for s in spots.values() if s["name"] not in used and s["season"] != "✗"][:skeleton.MORE_SPOTS]
        # The Journey walks the plan's own places from fill.py; the others are looked up here, in
        # case the model swaps one in.
        prefetch(dest, {code: s for code, s in spots.items() if s["name"] not in used})
        print(f"Trip file (this plan is already in it, its page is being built): {trip}")
        print(f"Notes file: {fill.notes_path(trip)}")
        print(f"After writing it, run: {fill_command()}")
        print(f"Day-by-day plan of real places from Wikivoyage (open in {month_names}):")
        print("\n".join(skeleton.lines(plan)))
        if others:
            print("Other spots: " + "; ".join(f"{skeleton.name_of(s)} ({s['town']})" for s in others))
        # The guide's own restaurants, for the notes' meals (a stand-in "local food" line is not one).
        eats = [m for m in planned["meals"] if m["name"] != m["note"]]
        if eats:
            notes = found.get("eatNotes") or {}
            print("Restaurants: " + "; ".join(
                f"{m['name']} ({m['area']}" + (f": {notes[m['name']]})" if notes.get(m["name"]) else ")") for m in eats))
    else:
        prefetch(dest, spots)
        print(f"Trip file: {trip}")
        print(f"{len(spots)} spots in {len({s['town'] for s in spots.values()})} places from Wikivoyage, "
              f"season checked for {month_names} (✓ open, ✗ closed then, ? not stated):")
        print("\n".join(table(spots)))
    print_web(list(web))
    return 0


def print_web(lines: list[str]) -> None:
    if lines:
        print("Web results for the wishes:")
        print("\n".join(lines))


def safely(function, *args) -> list:
    try:
        return function(*args)
    except Exception as exc:
        print(f"prepare: web search stopped ({str(exc)[:80]})", file=sys.stderr)
        return []


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

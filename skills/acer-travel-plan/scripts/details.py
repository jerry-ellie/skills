#!/usr/bin/env python3
"""What the Journey found about each stop, written into the plan itself.

research.py looks every stop up (Wikipedia, Wikivoyage, OpenStreetMap) and saves the result in
its cache, but used to show it only in the Journey window: the plan the user keeps said
"參觀歷史天守閣" while the Journey had just shown the opening hours and the ticket price. This
module turns those findings into details for the plan's stops: a short description in the trip's
language, opening hours, admission, address, website and the article it came from.

The details go to a hidden file beside the trip (.<trip>.details.json), never into the trip JSON: the
model reads and rewrites that JSON when the user asks for a change, and every extra token there
costs time on a local model. fill.py reads the details file when it renders.

Lookups are not always about the right place (a capsule hotel's price for the airport, a shop's
address for a whole district, an outlet mall in Arkansas for one near Osaka), so only what can
be trusted is kept: a description whose article is near the destination, and practical details
only for a single place (a temple, a tower), never for a district, street or airport.
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

import fill

DETAILS_SUFFIX = ".details.json"
SUMMARY_CACHE_FILE = "summary-cache.json"
SUMMARY_CACHE_TTL_SECONDS = 30 * 24 * 3600
SUMMARY_SENTENCES = 2
MAX_SUMMARY_CHARS = 160
MAX_SUMMARY_CHARS_ZH = 90
# A stop's own description in the plan: the page shows three lines and a "more" link for the rest
# (template.html), so it can be a short paragraph.
STOP_SUMMARY = {"count": 5, "limit": 480, "limit_zh": 260}
MAX_HOURS_CHARS = 70
MAX_PRICE_CHARS = 90
GALLERY_PHOTOS = 3
PHOTO_HOSTS = {"upload.wikimedia.org", "thumb.wikimedia.org"}
LOOKUP_WORKERS = 6
LOOKUP_BUDGET_S = 20

# OpenStreetMap kinds that name an area or a transport stop rather than one place you visit:
# a match there brings some shop's address and hours with it.
AREA_TYPES = {
    "stop", "station", "halt", "platform", "aerodrome", "terminal", "locality", "administrative",
    "suburb", "quarter", "neighbourhood", "city", "town", "village", "hamlet", "island",
    "residential", "commercial", "retail", "industrial", "region", "state", "county", "district",
}
# "X is a district of …", "X is the primary international airport …": an area, not a place. Only
# the words describing what X is count, so "a park located in the city of Nara" is a park.
AREA_SUMMARY_RE = re.compile(
    r"\bis (?:a|an|the)\b(?:\s+(?!(?:in|of|at|on|near|located|that|which|with)\b)[\w'-]+){0,4}?\s+"
    r"(?:district|neighbou?rhood|area|ward|quarter|street|arcade|airport|station|"
    r"town|city|village|region|prefecture|island|municipality)\b",
    re.I,
)
SENTENCE_END_RE = re.compile(r"(?<=[。！？])|(?<=[.!?])\s+")
# Areas that are not a town to explore: getting there, not being there.
TRANSIT_TYPES = {"stop", "station", "halt", "platform", "aerodrome", "terminal"}
TRANSIT_SUMMARY_RE = re.compile(r"\b(airport|station|terminal)\b", re.I)
MAX_HIGHLIGHT_NAME = 80
# A slot that is a part of a town rather than a place in it: "二世谷滑雪場", "札幌市區購物",
# "二世谷度假村入住". The town is looked up for its highlights (research.research_town).
TOWN_PART_RE = re.compile(
    r"^(?P<town>[\u3400-\u9fff]{2,5}?)"
    # "札幌最後採買", "富良野特色午餐", "知床雪地健行": a word between the town and the activity.
    r"(?:最後|自由|特色|深度|輕鬆|雪地|冬季|夏季|週邊|在地)?"
    r"(?:市區|市区|市中心|市內|市内|滑雪場|滑雪场|滑雪區|度假村|度假區|地區|區域|一帶|周邊|溫泉鄉|溫泉街|商店街"
    # The model's vague slots: a town and an activity, not a place ("富良野自然探索", "溫泉體驗").
    r"|自然|探索|體驗|体验|觀光|观光|散步|漫步|採買|采买|購物|购物|休閒|休闲|風景|风景|展望|美食|午餐|晚餐|"
    r"滑雪|健行|登山|一日遊|半日遊|之旅|巡禮|周遊|景點|景点)"
)
# Words the pattern above can take for a town ("滑雪體驗" is skiing, not a town named 滑雪).
NOT_A_TOWN = {"市區", "市区", "市中心", "市內", "市内", "前往", "返回", "滑雪", "溫泉", "温泉", "健行", "登山",
              "自然", "觀光", "观光", "美食", "雪地", "自由", "休閒", "休闲", "購物", "购物", "採買", "当地", "當地",
              "城市", "長途", "长途", "最後", "最后", "特色", "深度", "輕鬆", "户外", "戶外", "野生", "雪上", "冬季"}
TOWN_CACHE_PREFIX = "town:"
OFFICIAL_CACHE_PREFIX = "official:"


# ── what can be trusted ──────────────────────────────────────────────────

def is_area(data: dict) -> bool:
    """A district, street, station or airport: its "facts" belong to something else in it."""
    kinds = {str(f.get("value", "")).strip().lower() for f in data.get("facts") or [] if f.get("key") == "type"}
    if kinds & AREA_TYPES:
        return True
    first = SENTENCE_END_RE.split(str(data.get("summary") or ""), maxsplit=1)[0]
    return bool(AREA_SUMMARY_RE.search(first))


def is_town(data: dict) -> bool:
    """A town, district or region you spend time in (its highlights are worth listing), not an
    airport or station."""
    if not is_area(data):
        return False
    kinds = {str(f.get("value", "")).strip().lower() for f in data.get("facts") or [] if f.get("key") == "type"}
    first = SENTENCE_END_RE.split(str(data.get("summary") or ""), maxsplit=1)[0]
    return not (kinds & TRANSIT_TYPES) and not TRANSIT_SUMMARY_RE.search(first)


def trusted_highlights(data: dict, dest_point: tuple[float, float] | None) -> list[dict]:
    """A town's highlights (research.py, from its travel guide) that can be shown in the plan."""
    found = []
    for highlight in data.get("highlights") or []:
        if not isinstance(highlight, dict):
            continue
        name = clean(highlight.get("name"), MAX_HIGHLIGHT_NAME)
        if not name or not is_near(highlight, dest_point):
            continue
        item: dict = {"name": name}
        summary = first_sentences(str(highlight.get("description") or ""))
        if summary:
            item["summaryEn"] = summary
        if highlight.get("wikiTitle"):
            item["title"] = str(highlight["wikiTitle"])
        hours = clean(highlight.get("hours"), MAX_HOURS_CHARS)
        if hours and re.search(r"\d", hours):
            item["openHours"] = hours
        price = clean(highlight.get("price"), MAX_PRICE_CHARS)
        if price:
            item["price"] = price
        website = clean(highlight.get("website"), 200)
        if website.startswith(("https://", "http://")):
            item["website"] = website
        image = str(highlight.get("image") or "")
        if usable_photo(image):
            item["image"] = image
        source = str(highlight.get("wikiUrl") or highlight.get("guideUrl") or "")
        if source.startswith("https://"):
            item["sourceUrl"] = source
        found.append(item)
    return found


def town_of(place: str) -> str:
    """The town a slot is part of ("二世谷滑雪場" -> "二世谷"), or ""."""
    chinese = re.split(r"[\s(（]", str(place or "").strip(), maxsplit=1)[0]
    match = TOWN_PART_RE.match(chinese)
    town = match.group("town") if match else ""
    return "" if town in NOT_A_TOWN else town


def official_cache_name(name: str) -> str:
    """The research cache entry for what a place's official site says (research.check_official)."""
    return OFFICIAL_CACHE_PREFIX + name


def load_wishes(json_path: Path) -> list[str]:
    """The place names the traveller asked for (prepare.py)."""
    return [str(n) for n in load_candidates(json_path).get("wishNames") or [] if n]


def town_cache_name(town: str) -> str:
    """The research cache entry for a town's highlights (never taken for a stop: no slot has a colon)."""
    return TOWN_CACHE_PREFIX + town


def is_near(data: dict, dest_point: tuple[float, float] | None) -> bool:
    """The article's place is within reach of the destination (unknown counts as near)."""
    coords = data.get("coords")
    if not dest_point or not coords:
        return True
    try:
        point = (float(coords[0]), float(coords[1]))
    except (TypeError, ValueError, IndexError):
        return True
    return fill.distance_km(point, dest_point) <= fill.reach_km()


def clean(value: object, limit: int) -> str:
    """Plain one-line text, entities decoded; too long means it is prose, not a fact."""
    text = re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()
    return text if text and len(text) <= limit else ""


def strip_readings(text: str) -> str:
    """Drops name and pronunciation notes: brackets with a colon ("(Japanese: 大坂城, Hepburn:
    Ōsaka-jō)"), zhuyin ("（ㄎㄨ）") or only kana ("（おおさかじょう）"), and the space a removed
    reading leaves between Chinese characters ("難波 是")."""
    text = html.unescape(text or "")
    text = re.sub(r"\s*[(（][^()（）]*[:：][^()（）]*[)）]", "", text)
    text = re.sub(r"[(（][㄀-ㄯˊˇˋ˙\s]+[)）]", "", text)
    text = re.sub(r"[(（][぀-ヿー・\s]+[)）]", "", text)
    text = re.sub(r"(?<=[㐀-鿿])\s+(?=[㐀-鿿，。、])", "", text)
    return re.sub(r"\s+", " ", text).strip()


def first_sentences(text: str, count: int = SUMMARY_SENTENCES, limit: int = MAX_SUMMARY_CHARS,
                    limit_zh: int = MAX_SUMMARY_CHARS_ZH) -> str:
    """The first sentences that fit. A Chinese first sentence that is too long on its own is cut
    at a comma; nothing is ever cut in the middle of a phrase."""
    text = strip_readings(text)
    chinese = bool(fill.CJK_RE.search(text[:20]))
    limit = min(limit, limit_zh) if chinese else limit
    picked = ""
    for sentence in [s.strip() for s in SENTENCE_END_RE.split(text) if s.strip()][:count]:
        joined = picked + sentence if chinese else f"{picked} {sentence}".strip()
        if len(joined) > limit:
            if not picked and chinese:
                cut = sentence[:limit].rfind("，")
                picked = sentence[:cut] + "。" if cut > limit // 3 else ""
            break
        picked = joined
    return picked


def usable_photo(url: str) -> bool:
    """A Wikimedia photo at a display size: not a map, logo or drawing, not a full-size original."""
    if not isinstance(url, str) or not url.startswith("https://") or "/thumb/" not in url:
        return False
    host = urllib.parse.urlparse(url).netloc
    return (host in PHOTO_HOSTS and not fill.NOT_A_PHOTO_RE.search(url) and not re.search(r"logo", url, re.I)
            and bool(re.search(r"\.(jpe?g|webp)(/|$)", url, re.I)))


def named_after(url: str, title: str) -> bool:
    """The photo's file name has a word of the place's name in it ("Dotonbori_13.JPG" for
    Dōtonbori). A category's other files can be anything: a portrait, an archive scan."""
    words = [w for w in fill.place_words(title) if len(w) >= 4]
    name = fill.fold(urllib.parse.unquote(url.rsplit("/", 1)[-1]).replace("_", " "))
    return bool(words) and any(w in name for w in words)


# Wikimedia serves only its standard thumbnail widths (640 is refused with HTTP 400).
THUMB_WIDTH = 960
ORIGINAL_RE = re.compile(r"^https://upload\.wikimedia\.org/wikipedia/(commons|[a-z-]+)/([0-9a-f])/([0-9a-f]{2})/([^/?#]+)$")
FILE_PATH_RE = re.compile(r"^https://commons\.wikimedia\.org/wiki/Special:FilePath/([^?#]+)")


def as_thumb(url: str) -> str:
    """A Wikimedia photo as a thumbnail address, so the plan never loads a several-MB original:
    an original upload ("…/commons/5/54/Sapporo.jpg") and the Journey's own Special:FilePath
    address ("…/Special:FilePath/Hakodate Asaichi.jpg?width=640") both become
    "…/commons/thumb/5/54/Sapporo.jpg/960px-Sapporo.jpg". Anything else is returned as it is."""
    url = str(url or "")
    original = ORIGINAL_RE.match(url)
    if original:
        wiki, a, ab, name = original.groups()
        return f"https://upload.wikimedia.org/wikipedia/{wiki}/thumb/{a}/{ab}/{name}/{THUMB_WIDTH}px-{name}"
    file_path = FILE_PATH_RE.match(url)
    if file_path:
        name = urllib.parse.unquote(file_path.group(1)).replace(" ", "_")
        digest = hashlib.md5(name.encode("utf-8")).hexdigest()
        quoted = urllib.parse.quote(name)
        return (f"https://upload.wikimedia.org/wikipedia/commons/thumb/{digest[0]}/{digest[:2]}/"
                f"{quoted}/{THUMB_WIDTH}px-{quoted}")
    return url


def photos(data: dict, area: bool) -> dict:
    """The place's photo, and for a single place (not a district) a few more named after it. When
    the lead photo cannot be shown (a map, someone's portrait), the gallery's first usable one leads."""
    title = str(data.get("title") or "")
    image = as_thumb(str(data.get("image") or ""))
    if not usable_photo(image):
        image = next((g for g in (as_thumb(x) for x in data.get("gallery") or [])
                      if usable_photo(g) and (area or named_after(g, title))), "")
    if not image:
        return {}
    found: dict = {"image": image}
    if not area:
        more = [as_thumb(g) for g in data.get("gallery") or []]
        more = [g for g in more if g != image and usable_photo(g) and named_after(g, title)]
        if more:
            found["gallery"] = list(dict.fromkeys(more))[:GALLERY_PHOTOS]
    return found


def trusted(data: dict, dest_point: tuple[float, float] | None) -> dict:
    """The details of one stop that can be shown, from its research result."""
    details: dict = {}
    near = is_near(data, dest_point)
    area = is_area(data)
    source = (data.get("source") or {}).get("url", "")
    # "CityWalk or City Walk may refer to:": a list of other pages, not this place.
    disambiguation = "may refer to" in str(data.get("summary") or "")
    if near and data.get("summary") and not disambiguation:
        details["summaryEn"] = first_sentences(str(data["summary"]), **STOP_SUMMARY)
        details["title"] = str(data.get("title") or "")
        if source.startswith("https://"):
            details["sourceUrl"] = source
    # The photos come with the article: one far from the destination brings someone else's.
    if near and not disambiguation:
        details.update(photos(data, area))
    if area:
        return details
    facts = {f.get("key"): f.get("value") for f in data.get("facts") or [] if isinstance(f, dict)}
    hours = clean(facts.get("hours"), MAX_HOURS_CHARS)
    price = clean(facts.get("price"), MAX_PRICE_CHARS)
    address = clean(facts.get("address"), 120)
    website = clean(facts.get("website"), 200)
    # "Su off" alone (closed on Sundays, in OpenStreetMap's shorthand) says nothing to a reader.
    if hours and re.search(r"\d", hours):
        details["openHours"] = hours
    if price:
        details["price"] = price
    if address:
        details["address"] = address
    if website.startswith(("https://", "http://")):
        details["website"] = website
    return details


# ── the trip's language ──────────────────────────────────────────────────

def trip_language(trip: dict) -> str:
    """"zh" when the trip is written in Chinese, else "en"."""
    sample = " ".join(str(trip.get(k) or "") for k in ("title", "subtitle", "tripStyle"))
    for day in trip.get("days") or []:
        if isinstance(day, dict):
            sample += " " + str(day.get("title") or "")
    return "zh" if fill.CJK_RE.search(sample) else "en"


# ── descriptions in Chinese (Traditional) ────────────────────────────────

def cache_path() -> Path:
    return Path(os.environ.get("ACER_TRAVEL_CACHE_DIR") or Path.home() / ".openclaw" / "cache" / "acer-travel-plan") / SUMMARY_CACHE_FILE


def load_summary_cache() -> dict:
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_summary_cache(entries: dict) -> None:
    try:
        target = cache_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, target)
    except OSError:
        pass


def http_json(url: str, headers: dict | None = None) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": fill.WIKI_USER_AGENT, "Accept": "application/json", **(headers or {})})
    return fill.open_json(request)


def chinese_summary(en_title: str) -> tuple[str, str]:
    """The Chinese Wikipedia's opening for an English article, in Traditional characters, and its link."""
    query = http_json("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "format": "json", "formatversion": "2", "titles": en_title,
        "prop": "langlinks", "lllang": "zh", "redirects": "1",
    }))
    pages = query.get("query", {}).get("pages") or [{}]
    links = pages[0].get("langlinks") or []
    if not links:
        return "", ""
    zh_title = links[0].get("title", "")
    summary = http_json(
        "https://zh.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(zh_title.replace(" ", "_")),
        {"Accept-Language": "zh-tw"},
    )
    url = ((summary.get("content_urls") or {}).get("desktop") or {}).get("page", "")
    # Saved as written: how much of it is shown is decided when the plan is built.
    return str(summary.get("extract") or ""), url


DISH_CACHE_PREFIX = "dish:"
DISH_BUDGET_S = 3.0


def english_summary(en_title: str) -> tuple[str, str]:
    """The English Wikipedia's opening for an article, and its link."""
    summary = http_json("https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(en_title.replace(" ", "_")))
    url = ((summary.get("content_urls") or {}).get("desktop") or {}).get("page", "")
    return str(summary.get("extract") or ""), url


def dish_summaries(candidates: list[list[str]], language: str, budget_s: float = DISH_BUDGET_S) -> list[tuple[str, str]]:
    """For each meal, the description of its dish (the first of its English candidate titles that
    has an article: "Tempura" → 天婦羅's opening in a Chinese plan), as (text, link), or ("", "").
    Saved with the stops' descriptions; what does not answer within budget_s comes next time."""
    lookup = chinese_summary if language == "zh" else english_summary
    key = lambda title: f"{DISH_CACHE_PREFIX}{language}:{title}"  # noqa: E731
    summaries = load_summary_cache()
    now = time.time()
    fresh = {k: v for k, v in summaries.items()
             if isinstance(v, dict) and now - float(v.get("saved", 0)) <= SUMMARY_CACHE_TTL_SECONDS}
    wanted = sorted({t for titles in candidates for t in titles[:2] if key(t) not in fresh})
    if wanted:
        pool = ThreadPoolExecutor(max_workers=LOOKUP_WORKERS)
        futures = {pool.submit(lookup, title): title for title in wanted}
        try:
            for future in as_completed(futures, timeout=budget_s):
                try:
                    text, url = future.result()
                except Exception:
                    continue
                summaries[key(futures[future])] = {"saved": time.time(), "summary": text, "url": url}
        except FuturesTimeout:
            pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        save_summary_cache(summaries)
    found = []
    for titles in candidates:
        for title in titles[:2]:
            entry = summaries.get(key(title)) or {}
            shown = first_sentences(str(entry.get("summary") or ""), **STOP_SUMMARY)
            if shown:
                found.append((shown, str(entry.get("url") or "")))
                break
        else:
            found.append(("", ""))
    return found


# ── building and reading the details file ────────────────────────────────

def details_path(json_path: Path) -> Path:
    """.<trip>.details.json: hidden, so nothing that looks for the newest acer-travel-plan-*.json
    (the mobile revision request tells the agent to) ever takes it for the trip."""
    stem = json_path.name[: -len(".json")] if json_path.name.endswith(".json") else json_path.name
    return json_path.with_name("." + stem + DETAILS_SUFFIX)


def place_key(name: str) -> str:
    return fill.fold(str(name or ""))


def build(trip: dict, dest: str, research_cache: dict, dest_point: tuple[float, float] | None,
          lookup=chinese_summary, budget_s: float = LOOKUP_BUDGET_S, spots: dict | None = None) -> dict:
    """Details for every stop the research has finished, keyed by place: {place: {...}}."""
    import research  # here, not at the top: research imports fill, and fill imports this lazily

    language = trip_language(trip)
    found: dict[str, dict] = {}
    import official

    stops = research.stops_of(trip)
    research.name_spots(stops, spots or {})
    start, end = official.trip_dates(trip)
    for stop in stops:
        said = research_cache.get(research.cache_key(dest, official_cache_name(research.lookup_name(stop))))
        word = official.verdict(said.get("data"), start, end) if isinstance(said, dict) else {}
        if word:
            found.setdefault(place_key(stop["name"]), {})["official"] = word
        entry = research_cache.get(research.cache_key(dest, research.lookup_name(stop)))
        data = entry.get("data") if isinstance(entry, dict) else None
        if isinstance(data, dict):
            details = trusted(data, dest_point)
            if is_town(data):
                highlights = trusted_highlights(data, dest_point)
                if highlights:
                    details["highlights"] = highlights
                    # "札幌 (Sapporo)" and "札幌市區購物" share one round of highlights.
                    details["town"] = re.split(r"[\s(（]", stop["name"].strip(), maxsplit=1)[0]
            if "highlights" not in details:
                town_part(details, stop["name"], dest, research_cache, dest_point)
            if details:
                found[place_key(stop["name"])] = {**found.get(place_key(stop["name"]), {}), **details}
        else:
            # Not researched as a place ("二世谷度假村入住" is a check-in): its town may still be.
            details = town_part({}, stop["name"], dest, research_cache, dest_point)
            if details:
                found[place_key(stop["name"])] = {**found.get(place_key(stop["name"]), {}), **details}

    summaries = load_summary_cache() if language == "zh" else {}
    every = [d for d in found.values()] + [h for d in found.values() for h in d.get("highlights") or []]
    wanted = sorted({d["title"] for d in every if language == "zh" and d.get("title")}
                    - {t for t, v in summaries.items() if time.time() - float(v.get("saved", 0)) <= SUMMARY_CACHE_TTL_SECONDS})
    if wanted:
        pool = ThreadPoolExecutor(max_workers=LOOKUP_WORKERS)
        futures = {pool.submit(lookup, title): title for title in wanted}
        try:
            # Whichever answers first is kept: one slow lookup does not cost the others.
            for future in as_completed(futures, timeout=budget_s):
                try:
                    text, url = future.result()
                except Exception:
                    # Offline or rate-limited: tried again on the next build. Only an answer
                    # (an article, or none in Chinese) is saved, never a failure.
                    continue
                summaries[futures[future]] = {"saved": time.time(), "summary": text, "url": url}
        except FuturesTimeout:
            pass  # what arrived in time is used; the rest is looked up on the next build
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        save_summary_cache(summaries)

    for details in every:
        english = details.pop("summaryEn", "")
        title = details.pop("title", "")
        if language == "zh":
            local = summaries.get(title) or {}
            shown = first_sentences(str(local.get("summary") or ""), **STOP_SUMMARY)
            if shown:
                details["summary"] = shown
                if str(local.get("url", "")).startswith("https://"):
                    details["sourceUrl"] = local["url"]
                    # A highlight is named as the Chinese article names it ("小樽音樂盒博物館").
                    if "name" in details:
                        details["name"] = chinese_name(local["url"]) or details["name"]
            elif english:
                # No Chinese article: the English description, marked as English so it does not
                # read as a mistake (leaving the slot without one lost most of what was found).
                details["summary"] = english
                details["summaryLang"] = "en"
        elif english:
            details["summary"] = english
    return {k: v for k, v in found.items() if v}


def town_part(details: dict, place: str, dest: str, research_cache: dict,
              dest_point: tuple[float, float] | None) -> dict:
    """Adds the highlights of the town the place is part of, and the town as the key every slot in
    that town shares (fill.py hands each of them a different highlight)."""
    import research

    town = town_of(place)
    entry = research_cache.get(research.cache_key(dest, town_cache_name(town))) if town else None
    data = entry.get("data") if isinstance(entry, dict) else None
    highlights = trusted_highlights(data, dest_point) if isinstance(data, dict) else []
    if highlights:
        details["highlights"] = highlights
        details["town"] = town
    return details


def chinese_name(url: str) -> str:
    """The article title in a zh.wikipedia.org link, when it reads as a name."""
    title = urllib.parse.unquote(url.rstrip("/").rsplit("/", 1)[-1]).replace("_", " ")
    return title if fill.CJK_RE.search(title) and len(title) <= MAX_HIGHLIGHT_NAME else ""


def refresh(json_path: Path, trip: dict, dest: str, budget_s: float = LOOKUP_BUDGET_S,
            dest_point: tuple[float, float] | None = None) -> dict:
    """Builds the details from the research done so far and saves them beside the trip."""
    import research

    if dest_point is None:
        try:
            dest_point = fill.destination_coords(dest) if dest else None
        except Exception:
            dest_point = None
    details = build(trip, dest, research.load_cache(), dest_point, budget_s=budget_s, spots=load_spots(json_path))
    try:
        details_path(json_path).write_text(json.dumps(details, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        fill.warn(f"stop details not saved ({exc})")
    return details


def load(json_path: Path) -> dict:
    try:
        data = json.loads(details_path(json_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def candidates_path(json_path: Path) -> Path:
    """What prepare.py looked up for this trip, beside it and hidden: one file per trip, so a trip
    planned meanwhile for another destination never overwrites it."""
    return json_path.with_name("." + json_path.with_suffix("").name + ".candidates.json")


def load_candidates(json_path: Path) -> dict:
    try:
        saved = json.loads(candidates_path(json_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def load_spots(json_path: Path) -> dict:
    """The spots prepare.py listed for this trip ({"S01": {...}}), or {}."""
    spots = load_candidates(json_path).get("spots")
    return spots if isinstance(spots, dict) else {}


def spot_for(place: str, spots: dict) -> tuple[str, dict]:
    """The table spot a plan item names, by the place's Latin name ("旭岳 (Asahi-dake)" → Asahi-dake):
    the same letters, or nearly ("Mt. Yotei" / "Mount Yotei"). "旭岳纜車 (Asahidake Ropeway)" is the
    ropeway, not the mountain. ("", {}) when none is."""
    def letters(value: str) -> str:
        return re.sub(r"[^a-z]", "", fill.fold(str(value or "")))

    key = letters(fill.photo_query(str(place or "")))
    if len(key) < 4:
        return "", {}
    for code, spot in (spots or {}).items():
        name = letters(spot.get("name")) if isinstance(spot, dict) else ""
        if not name:
            continue
        shorter, longer = sorted((name, key), key=len)
        if shorter in longer and len(shorter) >= 0.8 * len(longer):
            return code, spot
    return "", {}


def spot_details(spot: dict, language: str) -> dict:
    """What the plan shows for a spot picked from prepare.py's table: hours, price, website, the
    guide it came from, and whether it is closed in the travel month."""
    found: dict = {}
    hours = clean(spot.get("hours"), MAX_HOURS_CHARS)
    if hours and re.search(r"\d", hours):
        found["openHours"] = hours
    price = clean(spot.get("price"), MAX_PRICE_CHARS)
    if price:
        found["price"] = price
    website = clean(spot.get("website"), 200)
    if website.startswith(("https://", "http://")):
        found["website"] = website
    if str(spot.get("guideUrl", "")).startswith("https://"):
        found["sourceUrl"] = spot["guideUrl"]
    # The guide's English text only in an English plan (a Chinese one gets the Journey's).
    if language != "zh":
        summary = first_sentences(str(spot.get("description") or ""))
        if summary:
            found["summary"] = summary
    if spot.get("season") == "✗":
        found["seasonClosed"] = True
    return found


def for_item(details: dict, item: dict) -> dict:
    """The details of one plan item (matched by its place), or {}."""
    found = details.get(place_key(item.get("place", ""))) if isinstance(item, dict) else None
    return found if isinstance(found, dict) else {}


if __name__ == "__main__":
    # python3 details.py <trip.json>: rebuild the details file from the research cache.
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/details.py <trip.json>", file=sys.stderr)
        raise SystemExit(2)
    path = Path(sys.argv[1]).resolve()
    match = fill.DEST_RE.search(path.name)
    result = refresh(path, json.loads(path.read_text(encoding="utf-8")), match.group(1).replace("-", " ") if match else "")
    print(f"Details for {len(result)} stops: {details_path(path)}")

#!/usr/bin/env python3
"""Fill template.html from trip JSON, then render the PNG preview. Stdlib only.

Usage:
  python3 scripts/fill.py <trip.json> [<out.html>]

The HTML defaults to the JSON path with .html; the PNG is always the HTML path with .png.
"""

from __future__ import annotations

import contextlib
import functools
import html
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote_plus

# Shared with acer-search-product-price/scripts/live.js: one relay process,
# one socket, for every skill's live view — see docs/AGENT_BROWSER_LIVE_VIEW.md
# for why the path still names that skill instead of being skill-agnostic.
CURRENT_TRIP_FILE = ".acer-travel-plan-current"
LIVE_SOCKET = "/tmp/acer-search-product-price/live.sock"

DEFAULT_HIGHLIGHTS = [
    (
        "https://upload.wikimedia.org/wikipedia/commons/thumb/b/b7/View_from_airplane_window.jpg/960px-View_from_airplane_window.jpg",
        "View from an airplane window",
    ),
    (
        "https://upload.wikimedia.org/wikipedia/commons/thumb/e/ef/Beach_sunset.jpg/960px-Beach_sunset.jpg",
        "Sunset over the water",
    ),
    (
        "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4b/Empty_road.jpg/960px-Empty_road.jpg",
        "Open road through trees",
    ),
]
DEFAULT_MEAL = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/8/87/Breakfast.jpg/960px-Breakfast.jpg",
    "A meal table",
)

# English, like the template's "Trip style" label it sits under.
DEFAULT_TRIP_STYLE = "General sightseeing"

SLOT_TIMES = ("Morning", "Afternoon", "Evening")
TOKEN_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")
DAY_COUNT_RE = re.compile(r"(\d+)")
DATE_RE = re.compile(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})")

MEAL_ONERROR = (
    "if(!this.dataset.fb){this.dataset.fb='1';this.src=this.dataset.fallback;}"
    "else{this.style.display='none';}"
)
SLOT_ONERROR = "var p=this.closest('.slot-photo');if(p)p.style.display='none';"

# Real photos for the spots that already show images (3 highlights, meals). First highlight:
# the destination's Wikivoyage guide photo. Places: English Wikipedia lead images for exact
# titles (one batched request), then Wikimedia Commons file search for the misses. Searches are capped and spaced, because the search API rate-limits
# (HTTP 429) after a handful of rapid calls.
WIKI_USER_AGENT = "acer-travel-plan/1.3 (https://www.acer.com; OpenClaw travel skill)"
PHOTO_SIZE = 640
PHOTO_TIMEOUT = 6
MAX_PHOTO_SEARCHES = 6
# A photo is used only when its article or file is geotagged within this distance of the
# destination: name matching alone took "Packing" -> a Cincinnati meat plant, "Passy" -> Paris.
MAX_PHOTO_DISTANCE_KM = 150
# A region is bigger than a city: Hakodate and Shiretoko are 250-300 km from the middle of
# Hokkaido, and their photos and articles were dropped as "somewhere else". prepare.py saves how
# far the destination's own towns reach (REACH_FILE); the limit is that, never less than above.
REACH_FILE = "reach.json"
MAX_REACH_KM = 800
_REACH = {"km": float(MAX_PHOTO_DISTANCE_KM)}


def reach_km() -> float:
    """How far from the destination a place may be and still be part of the trip."""
    return _REACH["km"]


def use_reach_of(dest: str) -> float:
    """Sets reach_km() for this destination from what prepare.py saved (else the city default)."""
    folder = Path(os.environ.get("ACER_TRAVEL_CACHE_DIR") or Path.home() / ".openclaw" / "cache" / "acer-travel-plan")
    try:
        saved = json.loads((folder / REACH_FILE).read_text(encoding="utf-8")).get(fold(dest or ""))
    except (OSError, ValueError, AttributeError):
        saved = None
    try:
        km = float(saved) if saved is not None else MAX_PHOTO_DISTANCE_KM
    except (TypeError, ValueError):
        km = MAX_PHOTO_DISTANCE_KM
    _REACH["km"] = min(MAX_REACH_KM, max(float(MAX_PHOTO_DISTANCE_KM), km))
    return _REACH["km"]
CJK_RE = re.compile(r"[぀-ヿ一-鿿]")
DEST_RE = re.compile(r"acer-travel-plan-(.+?)-\d+d", re.I)
# Not places: these found a Miami street for "Flight home" and an Italian hotel for "Hotel".
GENERIC_PLACE_RE = re.compile(
    r"\b(flight|fly|airport transfer|hotel|home|rest|free day|departure|arrival|arrive|return|transfer|check[- ]?in|check[- ]?out)\b"
    r"|航班|飛機|起飛|抵達|出發|前往|回程|返回|返家|休息|自由活動|飯店|酒店|旅館|住宿|入住|退房|步道|山路|路段",
    re.I,
)
# Not a photo of the place: a map, or a photo of someone ("Young_Woman_atop_Mount_Moiwayama"). The
# person words count only as whole words, so "Ramen" or "Manhattan" stay.
NOT_A_PHOTO_RE = re.compile(
    r"map|locator|location|\.svg|(?<![a-z])(?:wom[ae]n|m[ae]n|girls?|boys?|lady|ladies|portrait|selfie|couple|"
    r"people|person|child(?:ren)?|kids?|bride|groom|family|me)(?![a-z])", re.I)



class FillError(SystemExit):
    pass


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise FillError(code)


def warn(msg: str) -> None:
    # Long trips are written in one tool call; a re-write to fix a small slip can
    # overflow the local model's 32K context, so recoverable problems only warn.
    print(f"WARN: {msg}", file=sys.stderr)


def log_event(payload: dict) -> None:
    """For measuring a run: with a file named "event-log" in the cache folder, every event sent to
    the tray is also written there, one JSON line each (op and state only, with the time)."""
    target = Path(os.environ.get("ACER_TRAVEL_CACHE_DIR") or Path.home() / ".openclaw" / "cache" / "acer-travel-plan") / "event-log"
    if not target.exists():
        return
    brief = {k: payload.get(k) for k in ("ts", "script", "type", "op", "id", "state", "name") if payload.get(k) is not None}
    try:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(brief, ensure_ascii=False) + "\n")
    except OSError:
        pass


def live_emit(event: dict) -> None:
    """Send one event to the tray live view (scripts/live.js protocol); never fails."""
    if not hasattr(socket, "AF_UNIX"):
        return
    payload = dict(event, v=1, script="fill", pid=os.getpid(), ts=int(time.time() * 1000))
    log_event(payload)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(LIVE_SOCKET)
            sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError:
        pass


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def text(obj: dict, *keys: str, default: str = "") -> str:
    for key in keys:
        if key in obj and obj[key] is not None:
            return str(obj[key]).strip()
    return default


def maps_url(obj: dict, query: str) -> str:
    url = text(obj, "mapsUrl", "maps_url")
    if url.startswith("http"):
        return url
    q = query.strip() or "destination"
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(q)}"


def require_list(data: dict, key: str, min_len: int) -> list:
    value = data.get(key)
    if not isinstance(value, list) or len(value) < min_len:
        die(f"{key} must be an array with at least {min_len} item(s)")
    return value


def parse_day_count(trip_days: str, n_days: int) -> int:
    match = DAY_COUNT_RE.search(trip_days or "")
    if match:
        n = int(match.group(1))
        if n != n_days:
            warn(f"tripDays says {n} days but days[] has {n_days} entries; using {n_days}")
    return n_days


def parse_date(raw: str) -> date | None:
    match = DATE_RE.search(raw or "")
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def fmt_date(value: date) -> str:
    return f"{value.year}/{value.month}/{value.day} {value.strftime('%A')}"


def fill_dates(data: dict, days: list) -> str:
    """Set each day's date from startDate (or tripDateRange) so the model need not write them."""
    start = parse_date(text(data, "startDate", "start_date")) or parse_date(
        text(data, "tripDateRange", "trip_date_range")
    )
    if not start:
        return text(data, "tripDateRange", "trip_date_range")
    for i, day in enumerate(days):
        if isinstance(day, dict) and not text(day, "date"):
            day["date"] = fmt_date(start + timedelta(days=i))
    end = start + timedelta(days=len(days) - 1)
    return f"{start.year}/{start.month}/{start.day} – {end.year}/{end.month}/{end.day}"


def slot_photo_html(url: str, alt: str) -> str:
    if not url:
        return ""
    return (
        f'<div class="slot-photo"><img src="{esc(url)}" alt="{esc(alt)}" '
        f'loading="lazy" onerror="{SLOT_ONERROR}" /></div>'
    )


def meal_row_html(meal: dict) -> str:
    name = text(meal, "name")
    meta = text(meal, "area", "meta")
    address = text(meal, "address") or meta or name
    note = text(meal, "note")
    img = text(meal, "image") or DEFAULT_MEAL[0]
    alt = text(meal, "imageAlt", "image_alt") or name or DEFAULT_MEAL[1]
    href = maps_url(meal, address or name)
    return f"""        <div class="info-item meal-row">
          <div class="info-body">
            <p class="name">{esc(name)}</p>
            <p class="meta">{esc(meta)}</p>
            <p class="note">{esc(note)}</p>
            {meal_about_html(meal)}
            {map_row_html(href, address)}
          </div>
          <div class="meal-photo has-photo">
            <img
              src="{esc(img)}"
              alt="{esc(alt)}"
              loading="lazy"
              data-fallback="{esc(DEFAULT_MEAL[0])}"
              onerror="{MEAL_ONERROR}"
            />
          </div>
        </div>"""


def stay_item_html(stay: dict) -> str:
    name = text(stay, "name")
    meta = text(stay, "area", "meta")
    address = text(stay, "address") or meta or name
    note = text(stay, "note")
    href = maps_url(stay, address or name)
    return f"""        <div class="info-item">
          <p class="name">{esc(name)}</p>
          <p class="meta">{esc(meta)}</p>
          <p class="note">{esc(note)}</p>
          {map_row_html(href, address)}
        </div>"""


def map_row_html(href: str, address: str) -> str:
    """An address as the itinerary's map line: the pin, "地圖", the place as a link."""
    labels = DETAIL_LABELS.get(DETAIL_LANGUAGE["value"], DETAIL_LABELS["en"])
    return (f'<ul class="slot-info place-map"><li>{icon("i-pin")}<b>{esc(labels["map"])}</b>'
            f'<span>{link_html(href, address)}</span></li></ul>')


# Labels for what the Journey found about a stop (details.py), in the trip's language.
DETAIL_LABELS = {
    "zh": {"open": "營業時間", "price": "門票", "website": "官網", "source": "資料來源", "pick": "推薦景點", "english": "英文",
           "closed": "此期間可能未開放", "official": "官方資訊", "official_open": "旅遊期間營運中",
           "official_closed": "旅遊期間可能停駛或休業", "fetched": "資料取得", "status": "營運",
           "map": "地圖", "map_value": "Google 地圖", "transport": "交通", "wikipedia": "維基百科",
           "more": "更多", "less": "收合", "links": "連結"},
    "en": {"open": "Open", "price": "Admission", "website": "Website", "source": "Source", "pick": "Worth seeing", "english": "English",
           "closed": "May be closed in this period", "official": "Official", "official_open": "Open on your dates",
           "official_closed": "May be closed on your dates", "fetched": "checked", "status": "Status",
           "map": "Map", "map_value": "Google Maps", "transport": "Getting there", "wikipedia": "Wikipedia",
           "more": "More", "less": "Less", "links": "Links"},
}
# Set by main before fill(): {place: details} from <trip>.details.json, and the trip's language.
STOP_DETAILS: dict = {}
# {"S01": {...}}: the spots prepare.py listed for this trip (details.load_spots).
SPOTS: dict = {}
DETAIL_LANGUAGE = {"value": "en"}


def summary_html(detail: dict, labels: dict) -> str:
    """The description, three lines at most until "more" is clicked (the page's script adds that
    link only when it is longer); an English one in a Chinese plan says so."""
    toggle = f' data-more="{esc(labels["more"])}" data-less="{esc(labels["less"])}"'
    if detail.get("summaryLang") == "en" and DETAIL_LANGUAGE["value"] != "en":
        return (f'<p class="slot-summary" lang="en"{toggle}><span class="slot-lang">{esc(labels["english"])}</span>'
                f'{esc(detail["summary"])}</p>')
    return f'<p class="slot-summary"{toggle}>{esc(detail["summary"])}</p>'


# What kind of place a slot is, from what is known about it (never from the model): its name in
# both languages first, then the first sentence of its description. In this order, so "Asahiyama
# Zoo" is a zoo before it is a park and "Sapporo Beer Museum" a museum before it is food.
CATEGORIES = [
    ("move", "交通移動", "Getting around",
     r"前往|抵達|返程|機場|空港|airport|flight|新幹線|\bstation\b|車站|搭乘|移動|transfer|arrival|departure"),
    ("ski", "滑雪", "Skiing", r"滑雪|ski|snowboard|snow ?park|ゲレンデ"),
    ("onsen", "溫泉", "Hot springs", r"溫泉|温泉|onsen|hot spring|spa\b"),
    ("fun", "遊樂園", "Theme park", r"遊樂園|樂園|theme park|amusement|universal studios|disney|legoland|樂高"),
    ("animal", "動物園・水族館", "Zoo & aquarium", r"動物園|水族館|海遊館|\bzoo\b|aquarium|safari"),
    ("museum", "博物館・美術館", "Museum", r"博物館|美術館|紀念館|記念館|資料館|museum|gallery|memorial hall"),
    ("shrine", "神社寺廟", "Shrine & temple",
     r"神社|神宮|大社|天滿宮|天満宮|寺|廟|教堂|聖殿|(?<!食)堂|shrine|temple|jinja|church|cathedral|basilica|chapel|duomo|chiesa|\bsanta\b|\bsan\b|\bsanto\b|-ji\b|-dera\b|-gu\b"),
    ("food", "美食・市場", "Food & market", r"市場|美食|拉麵|橫丁|横丁|屋台|夜市|食堂|market|ichiba|ramen|yokocho|food"),
    ("shop", "購物", "Shopping", r"購物|商店街|百貨|outlet|shopping|\bmall\b|arcade|department store"),
    ("view", "展望・地標", "Views & landmarks",
     r"展望|觀景|塔|閣|大廈|大樓|城|橋|劇場|論壇|遺跡|遺址|古蹟|tower|observatory|sky|building|skyscraper|castle|bridge|landmark|tenkaku|amphitheat|forum|ruins|palaestra|archaeolog"),
    ("walk", "街區散步", "Walking", r"散步|漫步|散策|老街|街區|自由活動|free time|old town|district|stroll|walk"),
    ("nature", "自然・公園", "Nature & parks",
     r"國立公園|国立公園|公園|庭園|花園|山|岳|湖|瀑|峽|峡|海灘|島|岬|丘|森林|national park|\bpark\b|garden|"
     r"mount|\bmt\.|mountain|-dake\b|-yama\b|lake|falls|gorge|beach|island|cape|forest|hill|valley"),
]
CATEGORY_RE = [(key, zh, en, re.compile(pattern, re.I)) for key, zh, en, pattern in CATEGORIES]


def category(item: dict) -> str:
    """The slot's kind of place (a CATEGORIES key), or "" when nothing known says so."""
    travel = text(item, "travel")
    if travel in ("arrival", "departure"):
        return "move"
    if travel == "free":
        return "walk"
    detail = item.get("_detail") if isinstance(item.get("_detail"), dict) else {}
    summary = str(detail.get("summary") or "")
    first = re.split(r"[。.!！]", summary, maxsplit=1)[0]
    for words in (text(item, "place"), first):
        for key, _, _, pattern in CATEGORY_RE:
            if words and pattern.search(words):
                return key
    return ""


def icon(name: str, cls: str = "ln-icon") -> str:
    """One of the template's line icons (its <svg> sprite: #i-clock, #c-shrine...)."""
    return f'<svg class="{cls}" aria-hidden="true"><use href="#{name}"/></svg>'


def category_html(key: str) -> str:
    for k, zh, en, _ in CATEGORY_RE:
        if k == key:
            label = zh if DETAIL_LANGUAGE["value"] == "zh" else en
            return f'<span class="slot-cat">{icon("c-" + key)}{esc(label)}</span>'
    return ""


PLACE_PARTS_RE = re.compile(r"^(.*?)\s*[（(]([^()（）]+)[)）]\s*$")


def place_parts(place: str) -> tuple[str, str]:
    """"旭岳 (Asahi-dake)" → ("旭岳", "Asahi-dake"): the name to show large and the one in small
    type. A name in one language only is shown as it is."""
    match = PLACE_PARTS_RE.match(place or "")
    if match and CJK_RE.search(match.group(1)) and not CJK_RE.search(match.group(2)):
        return match.group(1).strip(), match.group(2).strip()
    return place, ""


def site_name(url: str, labels: dict) -> str:
    """"https://www.abenoharukas-300.jp/" → "abenoharukas-300.jp"; Wikipedia by its name."""
    host = urllib.parse.urlparse(url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    if host.endswith("wikipedia.org"):
        return labels["wikipedia"]
    if host.endswith("wikivoyage.org"):
        return "Wikivoyage"
    return host or url


def info_row(name: str, label: str, value: str, cls: str = "") -> str:
    klass = f' class="{cls}"' if cls else ""
    return f'<li{klass}>{icon(name)}<b>{esc(label)}</b><span>{value}</span></li>'


def link_html(url: str, words: str) -> str:
    return f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(words)} ↗</a>'


def slot_meta_html(item: dict, note: str = "") -> str:
    """Under a slot's note: what the Journey found about the place — a short description, then one
    fact per line, the most used first: open or closed on the dates, opening hours, admission,
    getting there, map, website, source. A line with nothing to say is left out."""
    labels = DETAIL_LABELS.get(DETAIL_LANGUAGE["value"], DETAIL_LABELS["en"])
    detail = item.get("_detail") if isinstance(item.get("_detail"), dict) else {}
    # A slot that names only a town gets one of the town's highlights (details.py).
    pick = detail.get("pick") if isinstance(detail.get("pick"), dict) else {}
    lines = []
    # The description (and the town's highlight) under the note.
    more = []
    if detail.get("summary"):
        more.append(summary_html(detail, labels))
    if pick:
        more.append(f'<p class="slot-pick"><b>{esc(labels["pick"])}</b> {esc(pick.get("name", ""))}</p>')
        if pick.get("summary"):
            more.append(summary_html(pick, labels))
    shown = pick or detail  # the facts of the place to visit: the highlight's when there is one

    rows = []
    official_word = detail.get("official") if isinstance(detail.get("official"), dict) else {}
    season_word = official_word.get("season") if official_word.get("url") else None
    # Open or closed: only when the place's own site (or the guide) says so, never a guess.
    if season_word in ("✓", "✗"):
        verdict = labels["official_open"] if season_word == "✓" else labels["official_closed"]
        said = "".join(f'<q>{esc(n)}</q>' for n in official_word.get("notes") or [])
        fetched = esc(f'{labels["fetched"]} {official_word.get("fetched", "")}'.strip())
        rows.append(info_row("i-check" if season_word == "✓" else "i-alert", labels["status"],
                             f'{esc(verdict)} <small>· {fetched}</small>{said}',
                             "slot-status ok" if season_word == "✓" else "slot-status warn"))
    elif detail.get("seasonClosed"):
        rows.append(info_row("i-alert", labels["status"], esc(labels["closed"]), "slot-status warn"))
    if shown.get("openHours"):
        rows.append(info_row("i-clock", labels["open"], esc(shown["openHours"])))
    if shown.get("price"):
        rows.append(info_row("i-ticket", labels["price"], esc(shown["price"])))
    planned = " · ".join(v for v in (text(item, "hours"), text(item, "transport")) if v)
    if planned:
        rows.append(info_row("c-move", labels["transport"], esc(planned)))
    place = text(item, "place")
    address = text(item, "address")
    shown_address = address if address and address != place else labels["map_value"]
    rows.append(info_row("i-pin", labels["map"], link_html(maps_url(item, address or place), shown_address)))
    # The place's own site and where its details were read, on one line.
    links = []
    site = str(official_word.get("url") or shown.get("website") or "")
    if site.startswith(("https://", "http://")):
        links.append(f'<span class="slot-link">{icon("i-globe")}{link_html(site, labels["website"])}</span>')
    # The model's own source first; else the article the Journey read.
    source = text(item, "sourceUrl", "source_url") or str(pick.get("sourceUrl") or detail.get("sourceUrl") or "")
    if source.startswith(("https://", "http://")):
        links.append(f'<span class="slot-link">{icon("i-book")}{link_html(source, site_name(source, labels))}</span>')
    if links:
        rows.append(info_row("i-link", labels["links"], "".join(links), "slot-links"))
    # The note, the description under it, then the facts.
    if note:
        lines.append(f'<p class="note">{esc(note)}</p>')
    lines.extend(more)
    lines.append(f'<ul class="slot-info">{"".join(rows)}</ul>')

    return "".join(lines)


def slot_media_html(item: dict, kind: str) -> str:
    """The slot's photo column: the big photo, and under it one thumbnail per photo of the place
    (the big photo cycles through them; a thumbnail shows its own). No photo: an empty frame with
    the kind of place in it, so every slot lines up."""
    place = text(item, "place")
    image = text(item, "image")
    if not image:
        return f'<div class="slot-media"><div class="slot-photo empty">{icon("c-" + kind) if kind else ""}</div></div>'
    detail = item.get("_detail") if isinstance(item.get("_detail"), dict) else {}
    gallery = [u for u in detail.get("gallery") or [] if isinstance(u, str) and u.startswith("https://") and u != image]
    thumbs = ""
    if gallery:
        thumbs = '<div class="slot-gallery">' + "".join(
            f'<img src="{esc(u)}" alt="{esc(place)}" loading="lazy" onerror="this.style.display=\'none\'" />'
            for u in [image] + gallery) + "</div>"
    return f'<div class="slot-media">{slot_photo_html(image, place)}{thumbs}</div>'


def attach_details(days: list) -> None:
    """Gives each item what the Journey found about its place (in memory only; the trip JSON the
    model rewrites stays as it wrote it). An item keeps its own address and source when it has one."""
    import details  # here: details imports research, which imports this module

    visits: dict[str, int] = {}
    for day in days:
        for item in day.get("items") or day.get("slots") or []:
            if not isinstance(item, dict) or item.get("travel"):
                continue
            found = dict(details.for_item(STOP_DETAILS, item))
            spot = SPOTS.get(text(item, "spot")) or details.spot_for(text(item, "place"), SPOTS)[1]
            if isinstance(spot, dict) and spot:
                # Picked from prepare.py's table: its hours, price and season are known for sure;
                # what the Journey found about the place (description, photos) comes on top.
                from_table = details.spot_details(spot, DETAIL_LANGUAGE["value"])
                found = {**from_table, **found, **{k: v for k, v in from_table.items() if k == "seasonClosed"}}
            if not found:
                continue
            highlights = found.pop("highlights", None) or []
            if highlights:
                # A plan that names only a town ("二世谷 (Niseko)" on ten slots): each visit shows a
                # different place there, and the town itself is described on the first one only.
                key = found.pop("town", "") or details.place_key(text(item, "place"))
                visit = visits.get(key, 0)
                visits[key] = visit + 1
                if visit:
                    found = {k: v for k, v in found.items() if k == "address"}
                if visit < len(highlights):
                    found["pick"] = highlights[visit]
                    if highlights[visit].get("image"):
                        found["image"] = highlights[visit]["image"]
            # The place's own site beats the travel guide on when it is open.
            season_word = (found.get("official") or {}).get("season") if isinstance(found.get("official"), dict) else None
            if season_word == "✓":
                found.pop("seasonClosed", None)
            elif season_word == "✗":
                found["seasonClosed"] = True
            if found.get("seasonClosed"):
                code = f" ({text(item, 'spot')})" if text(item, "spot") else ""
                warn(f"{text(item, 'place')}{code} may be closed in the travel month; "
                     "the plan says so. Do not rewrite the JSON for this warning.")
            item["_detail"] = found
            if found.get("address") and not text(item, "address"):
                item["address"] = found["address"]
            # The photo the Journey showed for the place, beside the slot.
            if found.get("image") and not text(item, "image"):
                item["image"] = found["image"]


def merge_repeated_places(days: list) -> None:
    """A day that lists the same place twice (a theme park as Morning, Afternoon and Evening) shows
    it once, with the notes joined. Fixed here with a warning instead of an error: asking the local
    model to rewrite the whole trip for it can overflow its context."""
    for index, day in enumerate(days):
        items = day.get("items") or day.get("slots")
        if not isinstance(items, list):
            continue
        kept: list = []
        by_place: dict[str, dict] = {}
        for item in items:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            key = fold(text(item, "place"))
            first = by_place.get(key) if key else None
            if first is None:
                if key:
                    by_place[key] = item
                kept.append(item)
                continue
            note = text(item, "note")
            if note and note not in text(first, "note"):
                first["note"] = "；".join(v for v in (text(first, "note"), note) if v) if CJK_RE.search(note) \
                    else "; ".join(v for v in (text(first, "note"), note) if v)
            warn(f"days[{index}] lists {text(item, 'place')} more than once; shown once with the notes joined")
        if len(kept) != len(items):
            day["items" if "items" in day else "slots"] = kept


def route_names(items: list | None) -> list[str]:
    """Each slot's place in order (its first name when it has two), once each."""
    names = []
    for item in items or []:
        name = place_parts(text(item, "place"))[0] if isinstance(item, dict) else ""
        if name and name not in names:
            names.append(name)
    return names


def route_html(names: list[str]) -> str:
    return '<i>›</i>'.join(esc(n) for n in names)


def day_route_html(day: dict, items: list | None = None) -> str:
    """The day's places in order ("抵達札幌市 › 札幌時計台 › 白色戀人公園"): the model's own route
    when it wrote one, else each slot's place."""
    route = text(day, "route")
    if route:
        return f'<p class="day-route">{esc(route)}</p>'
    names = route_names(items)
    return f'<p class="day-route">{route_html(names)}</p>' if names else ""


# The page's own words, in the trip's language.
SECTION_WORDS = {
    "zh": {"overview": "行程總覽", "days": "每日行程", "highlights": "行程亮點", "meals": "推薦美食", "stays": "住宿建議",
           "meals_lead": "容易安排、適合多人分享，旅途中補充體力的在地美食。",
           "stays_lead": "以交通方便、移動負擔小為優先，方便前往熱門景點與街區。",
           "total": "共 {n} 天", "stay": "住", "weekdays": "一二三四五六日",
           "arrive": "抵達", "move": "移動日", "leave": "返程"},
    "en": {"overview": "Trip Overview", "days": "Daily Itinerary", "highlights": "Trip Highlights", "meals": "Recommended Meals",
           "stays": "Recommended Stays",
           "meals_lead": "These meal ideas are easy to arrange, crowd-friendly, and practical for keeping energy up during the trip.",
           "stays_lead": "Stay suggestions prioritize convenient transit and lower travel load, making it easier to reach popular sights and neighborhoods.",
           "total": "{n} days", "stay": "Stay", "weekdays": None,
           "arrive": "Arrival", "move": "Moving day", "leave": "Departure"},
}


def short_date(value: str, words: dict) -> str:
    """"2026/11/11 Wednesday" → "11/11（三）" (or "Nov 11 (Wed)")."""
    match = DATE_RE.search(value or "")
    if not match:
        return ""
    try:
        day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return ""
    if words["weekdays"]:
        return f"{day.month}/{day.day}（{words['weekdays'][day.weekday()]}）"
    return f"{day.strftime('%b')} {day.day} ({day.strftime('%a')})"


def overview_tag(items: list, stay: str, last_stay: str, words: dict) -> str:
    """The day's kind, when it is one worth a mark: it arrives, it changes town for the night, it leaves."""
    travel = {item.get("travel") for item in items if isinstance(item, dict)}
    key = ("arrive" if "arrival" in travel else "leave" if "departure" in travel
           else "move" if stay and last_stay and stay != last_stay else "")
    return f'<em class="ov-tag ov-{key}">{esc(words[key])}</em>' if key else ""


def overview_html(days: list) -> tuple[str, str]:
    """(the lead line, the rows): one row a day — number, date, title, its places, where the night is
    spent (prepare.py's skeleton says so; a trip without it shows no stay column)."""
    words = SECTION_WORDS["zh" if DETAIL_LANGUAGE["value"] == "zh" else "en"]
    rows, stays, last_stay = [], [], ""
    for index, day in enumerate(days):
        if not isinstance(day, dict):
            continue
        items = day.get("items") or day.get("slots") or []
        stay = text(day, "stay")
        if stay and stay not in stays:
            stays.append(stay)
        stay_cell = f'{esc(words["stay"])} <b>{esc(stay)}</b>' if stay else ""
        tag = overview_tag(items, stay, last_stay, words)
        last_stay = stay or last_stay
        rows.append(
            f'        <tr data-day="{index + 1}"><td class="ov-n"><small>DAY</small>{index + 1:02d}</td>'
            f'<td class="ov-d">{esc(short_date(text(day, "date"), words))}</td><td class="ov-t">{esc(text(day, "title"))}{tag}</td>'
            f'<td class="ov-r">{route_html(route_names(items))}</td><td class="ov-s">{stay_cell}</td></tr>')
    total = words["total"].format(n=len(rows))
    lead = f"{' → '.join(stays)}，{total}" if stays and words["weekdays"] else (
        f"{total}: {' → '.join(stays)}" if stays else total)
    return lead, "\n".join(rows)


def normalize_time(raw: str) -> str:
    value = (raw or "").strip().title()
    for label in SLOT_TIMES:
        if value.lower() == label.lower():
            return label
    return value or "Morning"


def day_html(day: dict, index: int) -> str:
    title = text(day, "title")
    date = text(day, "date")
    items = day.get("items") or day.get("slots") or []
    if not isinstance(items, list) or not items:
        die(f"days[{index}] needs at least 1 Morning/Afternoon/Evening item")
    if not title:
        warn(f"days[{index}].title is empty; using the first place")
        title = text(items[0], "place") if isinstance(items[0], dict) else ""

    slots = []
    for item in items:
        if not isinstance(item, dict):
            continue
        time_label = normalize_time(text(item, "time"))
        place = text(item, "place")
        note = text(item, "note")
        if not place:
            warn(f"days[{index}] has a slot with no place; skipped")
            continue
        kind = category(item)
        # Getting there or resting ("前往二世谷", "溫泉放鬆"): a plain line, so it reads as part of
        # the day's plan, not as a place whose details are missing.
        plain = item.get("travel") or (not photo_query(place) and not item.get("_detail") and not text(item, "image"))
        if plain:
            mark = icon("c-" + kind, "ln-icon slot-plain-icon") if kind else ""
            slots.append(
                f"""            <div class="slot">
              <div class="time">{esc(time_label)}</div>
              <div class="slot-plain">
                <p class="place">{mark}{esc(place)}</p>
                <p class="note">{esc(note)}</p>
              </div>
            </div>"""
            )
            continue
        name, other_name = place_parts(place)
        other = f' <span class="place-en">{esc(other_name)}</span>' if other_name else ""
        # The words on the left, the photo on the right as tall as they are (template.html). The
        # name first, both languages on one line, its kind of place at the end of that line.
        slots.append(
            f"""            <div class="slot">
              <div class="time">{esc(time_label)}</div>
              <div class="slot-body">
                <div class="slot-text">
                  <div class="slot-head"><p class="place">{esc(name)}{other}</p>{category_html(kind)}</div>
                  {slot_meta_html(item, note)}
                </div>
                {slot_media_html(item, kind)}
              </div>
            </div>"""
        )
    if not slots:
        die(f"days[{index}] has no slot with a place")

    date_span = f'<span class="day-date">{esc(date)}</span>' if date else '<span class="day-date"></span>'
    # A big day number, then the date, the title and the day's places in order.
    return f"""        <div class="day">
          <div class="day-head">
            <div class="day-num"><small>DAY</small>{index + 1:02d}</div>
            <div class="day-text">
              {date_span}
              <p class="day-title">{esc(title)}</p>
              {day_route_html(day, items)}
            </div>
          </div>
          <div class="timeline">
{chr(10).join(slots)}
          </div>
        </div>"""


def highlight_images(data: dict) -> list[tuple[str, str]]:
    raw = data.get("highlightImages") or data.get("highlight_images") or []
    out = []
    for i in range(3):
        url, alt = DEFAULT_HIGHLIGHTS[i]
        if i < len(raw) and isinstance(raw[i], dict):
            url = text(raw[i], "url", "image") or url
            alt = text(raw[i], "alt", "imageAlt") or alt
        elif i < len(raw) and isinstance(raw[i], str) and raw[i].startswith("http"):
            url = raw[i]
        out.append((url, alt))
    return out


def pad_meals(meals: list) -> tuple[list, str]:
    rows = [meal_row_html(m) for m in meals if isinstance(m, dict)]
    if len(rows) < 3:
        die("meals must include at least 3 items with name / area / note")
    for i, meal in enumerate(meals[:3]):
        if not text(meal, "name") or not text(meal, "note"):
            die(f"meals[{i}] needs name and note")
    first_three = rows[:3]
    extra = "\n".join(rows[3:])
    return first_three, extra


# Wikimedia answers "429 Too Many Requests" when many lookups run at once: wait as it asks (a few
# seconds at most) and try again, instead of losing the photo or the article.
RETRIES_ON_429 = 2
MAX_RETRY_WAIT_S = 4.0


def open_json(request: urllib.request.Request, timeout: float = 0.0) -> object:
    """The JSON at the request, tried again after a short wait when the server says it is busy."""
    for attempt in range(RETRIES_ON_429 + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout or PHOTO_TIMEOUT) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 503) or attempt == RETRIES_ON_429:
                raise
            try:
                wait = float(exc.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                wait = 0.0
            time.sleep(min(MAX_RETRY_WAIT_S, wait or 1.5 * (attempt + 1)))
    return {}


def wiki_get(host: str, params: dict) -> dict:
    query = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"})
    request = urllib.request.Request(f"https://{host}/w/api.php?{query}", headers={"User-Agent": WIKI_USER_AGENT})
    return open_json(request)


LATIN_RUN_RE = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’.\- ]{2,}")


def photo_query(name: str) -> str:
    """The place name to look up; "" for non-places such as "Spa / 休息" or "Flight home"."""
    if not name or GENERIC_PLACE_RE.search(name):
        return ""
    # "白湖 (Lac Blanc)" / "Courmayeur 附近" -> the Latin part, which Wikipedia matches best
    latin = [run.strip(" -.'’") for run in LATIN_RUN_RE.findall(name)]
    latin = [run for run in latin if len(run) >= 3]
    if latin:
        return latin[0]
    # "Chamonix / Argentière" -> the leading place name
    return re.split(r"[(（/／,，·|]", name)[0].strip()


def thumbnail_of(page: dict) -> str:
    source = (page.get("thumbnail") or {}).get("source", "").split("?")[0]
    host = urllib.parse.urlparse(source).hostname or ""
    file_name = urllib.parse.unquote(source.rsplit("/", 1)[-1])
    if not source.startswith("https://") or not host.endswith(".wikimedia.org") or NOT_A_PHOTO_RE.search(file_name):
        return ""
    return source


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def coords_of(page: dict) -> tuple[float, float] | None:
    points = page.get("coordinates") or []
    return (points[0]["lat"], points[0]["lon"]) if points else None


def near(page: dict, origin: tuple[float, float] | None) -> bool:
    """True when the page is geotagged close to the destination (no destination: no check possible)."""
    if origin is None:
        return False
    point = coords_of(page)
    return point is not None and distance_km(point, origin) <= reach_km()


@functools.lru_cache(maxsize=8)
def destination_coords(dest: str) -> tuple[float, float] | None:
    """The destination's point (one Wikipedia lookup per destination and run: the stop details
    and the photos both ask for it)."""
    if not dest:
        return None
    data = wiki_get("en.wikipedia.org", {
        "action": "query", "generator": "search", "gsrsearch": dest, "gsrlimit": "1", "prop": "coordinates",
    })
    pages = data.get("query", {}).get("pages", [])
    return coords_of(pages[0]) if pages else None


def exact_title_photos(names: list[str], host: str, origin: tuple[float, float] | None) -> dict[str, str]:
    """One batched request: up to 50 exact titles, following redirects and zh script variants."""
    found: dict[str, str] = {}
    for start in range(0, len(names), 50):
        batch = names[start : start + 50]
        data = wiki_get(host, {
            "action": "query", "titles": "|".join(batch), "prop": "pageimages|coordinates", "piprop": "thumbnail",
            "pithumbsize": str(PHOTO_SIZE), "redirects": "1", "converttitles": "1",
        })
        query = data.get("query", {})
        # map each requested name through normalize/convert/redirect steps to its final title
        alias = {n: n for n in batch}
        for step in ("normalized", "converted", "redirects"):
            for hop in query.get(step, []):
                for name, current in alias.items():
                    if current == hop.get("from"):
                        alias[name] = hop.get("to")
        by_title = {page.get("title"): page for page in query.get("pages", [])}
        for name, title in alias.items():
            page = by_title.get(title) or {}
            if thumbnail_of(page) and near(page, origin):
                found[name] = thumbnail_of(page)
    return found


def fold(value: str) -> str:
    """Lowercase without accents, so "Brevent" matches "Le Brévent"."""
    return "".join(c for c in unicodedata.normalize("NFKD", value.lower()) if not unicodedata.combining(c))


# Qualifiers that may be missing from the article title: "Grand Col Ferret" -> "Col Ferret".
QUALIFIER_WORDS = {"grand", "petit", "upper", "lower", "center", "centre", "town", "village", "old", "new", "the"}


def place_words(name: str) -> list[str]:
    return [w for w in re.findall(r"[^\W\d_]{3,}", fold(name)) if w not in QUALIFIER_WORDS]


def commons_photo(query: str, name: str, origin: tuple[float, float] | None) -> str:
    photos = commons_photos(query, name, origin, 1)
    return photos[0] if photos else ""


def commons_photos(query: str, name: str, origin: tuple[float, float] | None, limit: int) -> list[str]:
    """Search Wikimedia Commons, which has photos of many places that have no Wikipedia article."""
    data = wiki_get("commons.wikimedia.org", {
        "action": "query", "generator": "search", "gsrsearch": query, "gsrnamespace": "6",
        "gsrlimit": "10", "prop": "imageinfo|coordinates", "iiprop": "url|mime", "iiurlwidth": str(PHOTO_SIZE),
    })
    pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
    # The file name must contain every meaningful word of the place, so "Lac Blanc chamonix"
    # takes "Lac Blanc in Chamonix.jpg" and never a photo of something else.
    words = place_words(name)
    photos: list[str] = []
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        if not words or not all(w in fold(page.get("title", "")) for w in words) or not near(page, origin):
            continue
        thumb = info.get("thumburl", "").split("?")[0]
        if thumb.startswith("https://") and not NOT_A_PHOTO_RE.search(page.get("title", "")) and thumb not in photos:
            photos.append(thumb)
            if len(photos) >= limit:
                break
    return photos


def find_photos(names: list[str], dest: str, wanted: int) -> dict[str, str]:
    # English Wikipedia only: it has the most articles and photos. Names without any
    # English part (pure Chinese) get no photo rather than a guessed one.
    names = list(dict.fromkeys(n for n in names if n and not CJK_RE.search(n)))
    found: dict[str, str] = {}
    try:
        origin = destination_coords(dest)
        if origin is None:
            print("Photos: destination not found on Wikipedia; using default images")
            return {}
        if names:
            found.update(exact_title_photos(names, "en.wikipedia.org", origin))
        # Then search Commons for the misses: first with the destination so "Lac Blanc" finds the
        # Chamonix lake, then without it for stages in the next country (Fenêtre d'Arpette is Swiss).
        searches = 0
        for name in [n for n in names if n not in found]:
            if len(set(found.values())) >= wanted:
                break
            for query in dict.fromkeys([f"{name} {dest}".strip(), name]):
                if searches >= MAX_PHOTO_SEARCHES:
                    break
                searches += 1
                time.sleep(0.3)
                photo = commons_photo(query, name, origin)
                if photo:
                    found[name] = photo
                    break
    except Exception as exc:  # offline, rate-limited, or timed out: keep whatever was found
        print(f"Photos: lookup stopped ({str(exc)[:80]})")
    return found


def wikivoyage_photo(dest: str) -> tuple[str, str] | None:
    """The destination's lead photo on Wikivoyage, picked by travel editors for the guide page."""
    data = wiki_get("en.wikivoyage.org", {
        "action": "query", "generator": "search", "gsrsearch": dest, "gsrlimit": "1",
        "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": "960",
    })
    pages = data.get("query", {}).get("pages", [])
    if not pages:
        return None
    page = pages[0]
    # "hokkaido" must open the Hokkaido guide, not some other page that mentions it
    first_word = fold(dest.split()[0]) if dest.split() else ""
    url = thumbnail_of(page)
    if url and first_word and first_word in fold(page.get("title", "")):
        return url, page.get("title", "")
    return None


def nearby_photos(origin: tuple[float, float], limit: int) -> list[tuple[str, str]]:
    """Lead photos of Wikipedia articles geotagged within 10 km of the destination, nearest first."""
    data = wiki_get("en.wikipedia.org", {
        "action": "query", "generator": "geosearch", "ggscoord": f"{origin[0]}|{origin[1]}",
        "ggsradius": "10000", "ggslimit": "30", "prop": "pageimages", "piprop": "thumbnail",
        "pithumbsize": str(PHOTO_SIZE),
    })
    pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
    photos: list[tuple[str, str]] = []
    for page in pages:
        url = thumbnail_of(page)
        if url and url not in [u for u, _ in photos]:
            photos.append((url, page.get("title", "")))
            if len(photos) >= limit:
                break
    return photos


def dish_photos(names: list[str], origin: tuple[float, float] | None) -> dict[str, str]:
    """Meals: exact Wikipedia titles only (e.g. "Fondue", "Raclette"); no search, which guessed
    unrelated photos. The article must describe a food; a geotagged page must still be near."""
    names = list(dict.fromkeys(n for n in names if n and not CJK_RE.search(n)))
    if not names:
        return {}
    data = wiki_get("en.wikipedia.org", {
        "action": "query", "titles": "|".join(names[:50]), "prop": "pageimages|coordinates|description",
        "piprop": "thumbnail", "pithumbsize": str(PHOTO_SIZE), "redirects": "1",
    })
    query = data.get("query", {})
    alias = {n: n for n in names}
    for hop in query.get("normalized", []):
        for name, current in alias.items():
            if current == hop.get("from"):
                alias[name] = hop.get("to")
    # A redirect to a broader article loses the dish: "Kaisendon" -> "Donburi", whose photo
    # is a beef bowl. Follow a redirect only when the target still names the dish.
    for hop in query.get("redirects", []):
        for name, current in alias.items():
            if current == hop.get("from"):
                target = hop.get("to", "")
                alias[name] = target if all(w in fold(target) for w in place_words(name)) else ""
    by_title = {page.get("title"): page for page in query.get("pages", [])}
    found = {}
    for name, title in alias.items():
        page = by_title.get(title) or {} if title else {}
        is_food = bool(FOOD_DESCRIPTION_RE.search(page.get("description", "")))
        if thumbnail_of(page) and is_food and (coords_of(page) is None or near(page, origin)):
            found[name] = thumbnail_of(page)
    return found


def dish_search(term: str, origin: tuple[float, float] | None, title_words: list[str]) -> str:
    """Search Wikipedia for a dish and take the first hit described as a food whose title
    contains one of `title_words` ("Oyster Omelet" -> "Oyster omelette"). The title check stops
    a vague term from landing on another dish ("Seafood Bowl" -> "Gumbo")."""
    data = wiki_get("en.wikipedia.org", {
        "action": "query", "generator": "search", "gsrsearch": term, "gsrlimit": "3",
        "prop": "pageimages|coordinates|description", "piprop": "thumbnail", "pithumbsize": str(PHOTO_SIZE),
    })
    for page in sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99)):
        if not FOOD_DESCRIPTION_RE.search(page.get("description", "")):
            continue
        if not any(w in fold(page.get("title", "")) for w in title_words):
            continue
        if thumbnail_of(page) and (coords_of(page) is None or near(page, origin)):
            return thumbnail_of(page)
    return ""


# Words that describe a meal but are not a dish article: "Chamonix Local Fondue" -> "Fondue".
GENERIC_FOOD_WORDS = {
    "local", "cuisine", "food", "foods", "meal", "meals", "snack", "snacks", "dinner", "lunch",
    "breakfast", "restaurant", "bakery", "cafe", "specialty", "specialties", "dishes", "dish",
    "market", "style", "traditional", "classic", "quick", "bite", "set", "stop", "and", "with",
}


# A dish article's short description says what it is ("Swiss melted cheese dish"); this keeps
# "Calèche" (a carriage) or "Refuge" from being used as a meal photo.
FOOD_DESCRIPTION_RE = re.compile(
    r"dish|food|cuisine|cheese|dairy|noodle|soup|stew|bread|pastry|cake|dessert|sweet|confection|"
    r"sauce|meat|beef|pork|lamb|mutton|chicken|fish|seafood|rice|dumpling|snack|beverage|drink|wine|beer|"
    r"spirit|coffee|tea|sausage|ham|pie|pancake|crêpe|crepe|sandwich|salad|fondue|sushi|ramen",
    re.I,
)


def sentence_case(value: str) -> str:
    """Wikipedia titles are case-sensitive after the first letter: "Oyster Omelet" is missing,
    "Oyster omelet" redirects to "Oyster omelette"."""
    return value[:1].upper() + value[1:].lower()


def dish_parts(dish: str) -> list[str]:
    """Split a combined dish: "Soy Milk & You Tiao" -> ["Soy Milk", "You Tiao"]."""
    return [p.strip() for p in re.split(r"\s*(?:&|/|,|\+|\band\b|\bwith\b)\s*", dish, flags=re.I) if p.strip()]


def dish_candidates(meal: dict) -> list[str]:
    """English dish titles to try for one meal, best first: the `dish` field (and each dish in
    a combined one), then the last one or two words of the name ("Savoyard Fondue" -> "Fondue").
    Each comes as written and in sentence case."""
    found = []
    dish = text(meal, "dish")
    if dish and not CJK_RE.search(dish):
        for part in [dish, *dish_parts(dish)]:
            found += [part, sentence_case(part)]
    name = photo_query(text(meal, "name"))
    words = [w for w in re.findall(r"[A-Za-zÀ-ÿ'’-]+", name) if w.lower() not in GENERIC_FOOD_WORDS]
    if len(words) >= 2:
        found.append(" ".join(words[-2:]).capitalize())
    if words:
        found.append(words[-1].capitalize())
    return list(dict.fromkeys(c for c in found if len(c) >= 3))


def attach_meal_descriptions(meals: list) -> None:
    """Each meal's dish described from Wikipedia ("Tempura" → its opening), kept in memory only:
    the meals are the model's suggestions, the description is the dish's own article."""
    if os.environ.get("ACER_TRAVEL_NO_PHOTOS"):
        return
    import details

    rows = [m for m in meals if isinstance(m, dict)]
    try:
        found = details.dish_summaries([dish_candidates(m) for m in rows], DETAIL_LANGUAGE["value"])
    except Exception as exc:  # offline: the meals read as before
        print(f"Meals: descriptions skipped ({str(exc)[:80]})")
        return
    for meal, (summary, url) in zip(rows, found):
        if summary:
            meal["_about"] = {"summary": summary, "sourceUrl": url}


def meal_about_html(meal: dict) -> str:
    about = meal.get("_about") if isinstance(meal.get("_about"), dict) else {}
    if not about.get("summary"):
        return ""
    labels = DETAIL_LABELS.get(DETAIL_LANGUAGE["value"], DETAIL_LABELS["en"])
    return summary_html(about, labels)


def attach_photos(data: dict, days: list, dest: str) -> None:
    """Real photos for the spots that already show images: the 3 highlights and the meals."""
    if os.environ.get("ACER_TRAVEL_NO_PHOTOS"):
        return
    has_highlights = data.get("highlightImages") or data.get("highlight_images")
    # Highlight candidates: every day's places, middle days first (day 1 / last day are flights).
    items = [i for d in days for i in (d.get("items") or d.get("slots") or []) if isinstance(i, dict) and text(i, "place")]
    first, last = (days[0], days[-1]) if days else ({}, {})
    edge = [i for i in items if i in (first.get("items") or []) or i in (last.get("items") or [])]
    ordered = [i for i in items if i not in edge] + edge
    places = [] if has_highlights else list(dict.fromkeys(photo_query(text(i, "place")) for i in ordered))
    meals = [m for m in data.get("meals", []) if isinstance(m, dict) and not text(m, "image")][:3]
    meal_names = [photo_query(text(m, "name")) for m in meals]
    meal_dishes = [dish_candidates(m) for m in meals]
    meal_photos = [""] * len(meals)
    picks: list[tuple[str, str]] = []
    # The same trip is filled twice (its first page, then the update after the research) and
    # again for every change: the photos found for these places and meals are kept a while.
    # Photos the Journey already found for the plan's places (prepare.py prefetched them, the walk
    # adds more): part of the key, so the update after the walk is not served the first, poorer pick.
    known = known_photos(ordered) if not has_highlights else {}
    key = photo_cache_key(dest, places, meals, sorted(known.values())[:3])
    saved = load_photo_cache(key)
    complete = False
    if saved is not None:
        picks = [tuple(pick) for pick in saved.get("picks", [])]
        meal_photos = (list(saved.get("meals", [])) + [""] * len(meals))[:len(meals)]
    else:
        try:
            origin = destination_coords(dest)
            if not has_highlights:
                # The big first highlight: the destination's travel-guide photo from Wikivoyage.
                guide = wikivoyage_photo(dest) if dest else None
                if guide:
                    picks.append(guide)
                # The Journey's photos first; the network only for the rest.
                for name in places:
                    if known.get(name) and len(picks) < 3 and known[name] not in [u for u, _ in picks]:
                        picks.append((known[name], name))
                rest = [name for name in places if name not in known]
                found = find_photos(rest, dest, 3) if rest and len(picks) < 3 else {}
                for name in places:
                    if found.get(name) and found[name] not in [u for u, _ in picks]:
                        picks.append((found[name], name))
                # Places written only in Chinese cannot be looked up; fill the rest with
                # landmarks geotagged near the destination itself.
                if len(picks) < 3 and origin:
                    for url, title in nearby_photos(origin, 6):
                        if len(picks) < 3 and url not in [u for u, _ in picks]:
                            picks.append((url, title))
            if meals:
                used: set[str] = set()

                def take(i: int, url: str) -> bool:
                    # one photo per meal, never the same photo for two meals
                    if url and url not in used and not meal_photos[i]:
                        meal_photos[i] = url
                        used.add(url)
                    return bool(meal_photos[i])

                # 1. Dishes ("Fondue", "Ramen") have Wikipedia articles with photos; restaurants rarely do.
                exact = dish_photos([c for cands in meal_dishes for c in cands], origin)
                for i, cands in enumerate(meal_dishes):
                    for c in cands:
                        if take(i, exact.get(c, "")):
                            break
                for i, meal in enumerate(meals):
                    if meal_photos[i]:
                        continue
                    # 2. Search Wikipedia for the dish; the hit must be described as a food.
                    dish = text(meal, "dish")
                    term = dish if dish and not CJK_RE.search(dish) else ""
                    term = term or (meal_names[i] if not CJK_RE.search(meal_names[i]) else "")
                    words = [w for w in place_words(term) if w not in GENERIC_FOOD_WORDS]
                    if term and words:
                        time.sleep(0.3)
                        if take(i, dish_search(term, origin, words)):
                            continue
                    # 3. The restaurant on Commons, only photos geotagged near the destination.
                    if meal_names[i] and not CJK_RE.search(meal_names[i]):
                        time.sleep(0.3)
                        take(i, commons_photo(f"{meal_names[i]} {dest}".strip(), meal_names[i], origin))
                # 4. Still missing: the destination's cuisine overview ("Taipei cuisine" -> Taiwanese
                # cuisine), a spread of local food that fits any meal row, unlike the breakfast default.
                # Only an overview article: a single dish would sit next to an unrelated meal.
                missing = [i for i in range(len(meals)) if not meal_photos[i]]
                if missing and dest:
                    take(missing[0], dish_search(f"{dest} cuisine", origin, ["cuisine"]))
        except Exception as exc:  # offline, rate-limited, or timed out: keep whatever was found
            print(f"Photos: lookup stopped ({str(exc)[:80]})")
        else:
            complete = True
        if complete and (picks or any(meal_photos)):
            save_photo_cache(key, picks, meal_photos)
    if picks:
        data["highlightImages"] = [{"url": u, "alt": a} for u, a in picks[:3]]
    for meal, photo in zip(meals, meal_photos):
        if photo:
            meal["image"] = photo
    print(f"Photos: {len(data.get('highlightImages') or [])}/3 highlights, "
          f"{sum(1 for m in meals if m.get('image'))}/{len(meals)} meals")


PHOTO_CACHE_FILE = "photo-cache.json"
PHOTO_CACHE_TTL_SECONDS = 7 * 24 * 3600


def known_photos(items: list) -> dict[str, str]:
    """{photo query: photo} for the plan items the Journey has a photo of (its details file)."""
    import details

    found: dict[str, str] = {}
    for item in items:
        image = str(details.for_item(STOP_DETAILS, item).get("image") or "")
        name = photo_query(text(item, "place"))
        if name and image.startswith("https://") and name not in found:
            found[name] = image
    return found


def photo_cache_path() -> Path:
    import research
    return research.cache_dir() / PHOTO_CACHE_FILE


def photo_cache_key(dest: str, places: list[str], meals: list[dict], known: list[str] = ()) -> str:
    meal_key = [(text(m, "name"), text(m, "dish")) for m in meals]
    return json.dumps([fold(dest or ""), places[:12], meal_key, list(known)], ensure_ascii=False)


def load_photo_cache(key: str) -> dict | None:
    try:
        entry = json.loads(photo_cache_path().read_text(encoding="utf-8")).get(key)
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(entry, dict) and time.time() - float(entry.get("saved", 0)) <= PHOTO_CACHE_TTL_SECONDS:
        return entry
    return None


def save_photo_cache(key: str, picks: list, meal_photos: list) -> None:
    path = photo_cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    now = time.time()
    data = {k: v for k, v in data.items() if isinstance(v, dict) and now - float(v.get("saved", 0)) <= PHOTO_CACHE_TTL_SECONDS}
    data[key] = {"saved": now, "picks": [list(p) for p in picks], "meals": list(meal_photos)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass


def validate_no_tokens(html_text: str) -> None:
    leftover = TOKEN_RE.findall(html_text)
    if leftover:
        unique = ", ".join(dict.fromkeys(leftover))
        die(f"unfilled placeholders remain: {unique}")
    if "{{" in html_text:
        die("finished HTML still contains {{")


LAST_FILL: dict = {}


def fill(template: str, data: dict, dest: str = "") -> str:
    title = text(data, "title")
    subtitle = text(data, "subtitle")
    if not title or not subtitle:
        die("title and subtitle are required")
    # tripStyle is optional: the user may never name one, and rewriting the whole JSON
    # just to add it cost small local models a full extra turn.
    trip_style = text(data, "tripStyle", "trip_style")
    if not trip_style:
        trip_style = DEFAULT_TRIP_STYLE
        warn(f"tripStyle missing; using '{DEFAULT_TRIP_STYLE}'")

    climate = data.get("climate") or {}
    if not isinstance(climate, dict):
        die("climate must be an object")
    season = text(climate, "season")
    temp = text(climate, "temp")
    rain = text(climate, "rain")
    if not season or not temp or not rain:
        die("climate.season, climate.temp, and climate.rain are required")

    highlights = require_list(data, "highlights", 3)
    highlight_items = [str(h).strip() for h in highlights if str(h).strip()]
    if len(highlight_items) < 3:
        die("highlights needs at least 3 non-empty strings")

    meals = require_list(data, "meals", 3)
    stays = require_list(data, "stays", 1)
    days = require_list(data, "days", 1)
    if not text(stays[0], "name") or not text(stays[0], "note"):
        die("stays[0] needs name and note")

    days = [d for d in days if isinstance(d, dict)]
    merge_repeated_places(days)
    attach_photos(data, days, dest)
    attach_meal_descriptions(meals)
    attach_details(days)
    n_days = len(days)
    parse_day_count(text(data, "tripDays", "trip_days"), n_days)
    trip_days = f"{n_days} Days"
    date_range = fill_dates(data, days)

    imgs = highlight_images(data)
    meal_banner = text(data, "mealBanner", "meal_banner") or text(meals[0], "image") or DEFAULT_MEAL[0]
    meal_banner_alt = text(data, "mealBannerAlt", "meal_banner_alt") or text(meals[0], "name") or DEFAULT_MEAL[1]

    _, meals_extra = pad_meals(meals)
    stay_rows = [stay_item_html(s) for s in stays if isinstance(s, dict)]
    if not stay_rows:
        die("stays produced no items")
    stays_extra = "\n".join(stay_rows[2:])

    replacements = {
        "TITLE": title,
        "SUBTITLE": subtitle,
        "TRIP_DAYS": trip_days,
        "TRIP_DATE_RANGE": date_range,
        "TRIP_STYLE": trip_style,
        "CLIMATE_SEASON": season,
        "CLIMATE_TEMP": temp,
        "CLIMATE_RAIN": rain,
        "HIGHLIGHTS_LIST": "\n        ".join(f"<li>{esc(item)}</li>" for item in highlight_items),
        "HIGHLIGHT_IMG_1": imgs[0][0],
        "HIGHLIGHT_IMG_1_ALT": imgs[0][1],
        "HIGHLIGHT_IMG_2": imgs[1][0],
        "HIGHLIGHT_IMG_2_ALT": imgs[1][1],
        "HIGHLIGHT_IMG_3": imgs[2][0],
        "HIGHLIGHT_IMG_3_ALT": imgs[2][1],
        "MEAL_IMG": meal_banner,
        "MEAL_IMG_ALT": meal_banner_alt,
        "MEALS_EXTRA": meals_extra,
        "STAYS_EXTRA": stays_extra,
        "MORE_DAYS": "",
    }

    # Fill first three meal token groups from meals[0:3]
    for i in range(3):
        meal = meals[i]
        name = text(meal, "name")
        meta = text(meal, "area", "meta")
        address = text(meal, "address") or meta or name
        replacements[f"MEAL_{i+1}_NAME"] = name
        replacements[f"MEAL_{i+1}_META"] = meta
        replacements[f"MEAL_{i+1}_ADDRESS"] = address
        replacements[f"MEAL_{i+1}_MAPS_URL"] = maps_url(meal, address or name)
        replacements[f"MEAL_{i+1}_NOTE"] = text(meal, "note")
        replacements[f"MEAL_{i+1}_ABOUT"] = meal_about_html(meal)
        replacements[f"MEAL_{i+1}_IMG"] = text(meal, "image") or DEFAULT_MEAL[0]
        replacements[f"MEAL_{i+1}_IMG_ALT"] = text(meal, "imageAlt", "image_alt") or name or DEFAULT_MEAL[1]

    # Stays: fill stay 1; stay 2 tokens if present else blank (block removed below)
    stay1 = stays[0]
    s1_addr = text(stay1, "address") or text(stay1, "area", "meta") or text(stay1, "name")
    replacements.update(
        {
            "STAY_1_NAME": text(stay1, "name"),
            "STAY_1_META": text(stay1, "area", "meta"),
            "STAY_1_ADDRESS": s1_addr,
            "STAY_1_MAPS_URL": maps_url(stay1, s1_addr),
            "STAY_1_NOTE": text(stay1, "note"),
        }
    )
    if len(stays) >= 2:
        stay2 = stays[1]
        s2_addr = text(stay2, "address") or text(stay2, "area", "meta") or text(stay2, "name")
        replacements.update(
            {
                "STAY_2_NAME": text(stay2, "name"),
                "STAY_2_META": text(stay2, "area", "meta"),
                "STAY_2_ADDRESS": s2_addr,
                "STAY_2_MAPS_URL": maps_url(stay2, s2_addr),
                "STAY_2_NOTE": text(stay2, "note"),
            }
        )
    else:
        replacements.update(
            {
                "STAY_2_NAME": "",
                "STAY_2_META": "",
                "STAY_2_ADDRESS": "",
                "STAY_2_MAPS_URL": "",
                "STAY_2_NOTE": "",
            }
        )

    # Every day, the first too, is built the same way (day_html); the first has its own token so
    # the page reads complete before the rest are added (the Agent panel inserts MORE_DAYS rows).
    replacements["DAY_1"] = day_html(days[0], 0)
    more_day_rows = [day_html(day, i) for i, day in enumerate(days) if i > 0 and isinstance(day, dict)]
    more_days = "\n".join(more_day_rows)
    replacements["MORE_DAYS"] = more_days
    replacements["MAP_LABEL"] = DETAIL_LABELS.get(DETAIL_LANGUAGE["value"], DETAIL_LABELS["en"])["map"]
    words = SECTION_WORDS["zh" if DETAIL_LANGUAGE["value"] == "zh" else "en"]
    replacements.update({
        "SEC_OVERVIEW": words["overview"], "SEC_DAYS": words["days"], "SEC_HIGHLIGHTS": words["highlights"],
        "SEC_MEALS": words["meals"], "SEC_STAYS": words["stays"],
        "MEALS_LEAD": esc(words["meals_lead"]), "STAYS_LEAD": esc(words["stays_lead"]),
    })
    replacements["OVERVIEW_LEAD"], replacements["OVERVIEW_ROWS"] = overview_html(days)
    replacements["OVERVIEW_LEAD"] = esc(replacements["OVERVIEW_LEAD"])
    # The day list beside the days, in the day cards' own style (template.html fills in the number).
    replacements["DAY_NAV_LABEL"] = "DAY {n}"

    # What the live view needs to replay this fill and to show the trip as an editable plan.
    LAST_FILL.clear()
    LAST_FILL.update(
        values={k: ("" if v is None else str(v)) for k, v in replacements.items() if k != "MORE_DAYS"},
        rows=more_day_rows,
        trip={**data, "days": days, "tripDays": trip_days, "tripDateRange": date_range},
    )

    out = template
    for key, value in replacements.items():
        out = out.replace("{{" + key + "}}", "" if value is None else str(value))

    if len(stays) < 2:
        out = re.sub(
            r'\n        <div class="info-item">\s*'
            r'<p class="name"></p>\s*'
            r'<p class="meta"></p>\s*'
            r'<p class="note"></p>\s*'
            r'<ul class="slot-info place-map"><li>.*?<a href=""[^>]*> ↗</a></span></li></ul>\s*</div>',
            "",
            out,
            flags=re.S,
        )


    validate_no_tokens(out)
    return out


APPLY_SCRIPT = Path(__file__).resolve().parent / "apply-changes.py"


def ui_spec(trip: dict, json_path: Path, html_path: Path) -> dict:
    """The Agent browser panel's view of this trip (docs/AGENT_PANEL_UI_SPEC.md).

    Ids are positions in the trip JSON (d2 = days[1], d2i0 = its first item, m0 = meals[0]),
    which apply-changes.py maps back. Chrome text uses the panel's string keys; content stays
    in the language the trip was written in.
    """
    days = [d for d in trip.get("days") or [] if isinstance(d, dict)]
    groups = []
    for d, day in enumerate(days, 1):
        items = []
        for i, item in enumerate(day.get("items") or day.get("slots") or []):
            if not isinstance(item, dict) or not text(item, "place"):
                continue
            detail = item.get("_detail") if isinstance(item.get("_detail"), dict) else {}
            pick = detail.get("pick") if isinstance(detail.get("pick"), dict) else {}
            shown = pick or detail
            labels = DETAIL_LABELS.get(DETAIL_LANGUAGE["value"], DETAIL_LABELS["en"])
            meta = [v for v in (
                f'⚠ {labels["closed"]}' if detail.get("seasonClosed") else "",
                text(item, "transport"),
                f'{labels["open"]} {shown["openHours"]}' if shown.get("openHours") else "",
                f'{labels["price"]} {shown["price"]}' if shown.get("price") else "",
            ) if v]
            picked = f'{labels["pick"]}：{pick.get("name", "")} {pick.get("summary", "")}'.strip() if pick else ""
            items.append({
                "id": f"d{d}i{i}",
                "time": " · ".join(v for v in (text(item, "hours"), text(item, "time")) if v),
                "title": text(item, "place"),
                "note": " ".join(v for v in (text(item, "note"), str(detail.get("summary") or ""), picked) if v),
                "meta": meta,
                "link": text(item, "sourceUrl", "source_url") or str(pick.get("sourceUrl") or detail.get("sourceUrl") or ""),
            })
        groups.append({
            "id": f"d{d}",
            "kicker": "DAY",
            "number": f"{d:02d}",
            "title": text(day, "title"),
            "subtitle": "  ·  ".join(v for v in (text(day, "date"), text(day, "route")) if v),
            "items": items,
        })

    def cards(block_id: str, key: str, prefix: str) -> dict:
        entries = [x for x in trip.get(key) or [] if isinstance(x, dict)]
        return {
            "type": "cards", "id": block_id, "titleKey": key,
            "items": [{"id": f"{prefix}{k}", "title": text(x, "name"),
                       "subtitle": " · ".join(v for v in (text(x, "area"), text(x, "note")) if v)}
                      for k, x in enumerate(entries)],
            "edit": {"remove": True, "swap": True},
        }

    start = parse_date(text(trip, "startDate", "start_date")) or parse_date(text(trip, "tripDateRange", "trip_date_range"))
    climate = trip.get("climate") if isinstance(trip.get("climate"), dict) else {}
    adjust = [{"type": "stepper", "id": "length", "labelKey": "days", "value": len(days), "min": 1, "max": 21,
               "unitKey": "daysUnit", "target": "days"}]
    if start:
        adjust.append({"type": "date", "id": "start", "labelKey": "startDate", "value": start.isoformat()})
    adjust += [
        {"type": "choices", "id": "pace", "labelKey": "pace", "options": [{"id": "relaxed", "labelKey": "relaxed"}, {"id": "packed", "labelKey": "packed"}]},
        {"type": "choices", "id": "budget", "labelKey": "budget", "options": [{"id": "economy", "labelKey": "economy"}, {"id": "comfort", "labelKey": "comfort"}]},
        {"type": "choices", "id": "style", "labelKey": "style", "multi": True, "options": [
            {"id": k, "labelKey": k} for k in ("family", "food", "culture", "shopping", "nature", "rain")]},
    ]
    origin = text(trip, "origin")
    chips = [{"text": f"◷ {text(trip, 'tripDays')}"}] if text(trip, "tripDays") else []
    if origin:
        chips.append({"textKey": "fromChip", "vars": {"from": origin}})
    if text(trip, "tripStyle"):
        chips.append({"text": text(trip, "tripStyle")})

    facts = [
        ("r_dates", text(trip, "tripDateRange")), ("r_length", text(trip, "tripDays")),
        ("r_style", text(trip, "tripStyle")), ("r_from", origin), ("r_to", text(trip, "destination")),
        ("r_weather", " · ".join(v for v in (text(climate, "season"), text(climate, "temp"), text(climate, "rain")) if v)),
    ]
    highlights = [str(h).strip() for h in trip.get("highlights") or [] if str(h).strip()]
    return {
        "version": 1,
        "id": str(json_path.resolve()),
        "skill": "acer-travel-plan",
        "eyebrow": "A JOURNEY, MADE FOR YOU",
        "titleKey": "resultTitle",
        "subject": text(trip, "title"),
        "report": str(html_path.resolve()),
        "provenance": True,
        "apply": {"script": str(APPLY_SCRIPT), "target": str(json_path.resolve())},
        "tabs": [
            {"id": "plan", "labelKey": "t_itinerary", "blocks": [
                {"type": "hero", "badgeKey": "yourJourney", "title": text(trip, "title"),
                 "text": text(trip, "summary") or text(trip, "subtitle"), "chips": chips},
                {"type": "note", "textKey": "hint"},
                {"type": "group", "labelKey": "adjustPrefs", "collapsed": True, "blocks": adjust},
                {"type": "timeline", "id": "days", "groups": groups,
                 "edit": {"remove": True, "reorder": True, "swap": True}},
                cards("meals", "meals", "m"),
                cards("stays", "stays", "s"),
            ]},
            {"id": "summary", "labelKey": "t_summary", "blocks": [
                {"type": "facts", "rows": [{"labelKey": k, "value": v} for k, v in facts if v]},
                {"type": "bullets", "titleKey": "highlights", "items": highlights},
                {"type": "numbered", "titleKey": "dayTable", "items": [
                    {"number": g["number"], "title": g["title"],
                     "text": text(days[n], "route") or " → ".join(i["title"] for i in g["items"])}
                    for n, g in enumerate(groups)]},
            ]},
            {"id": "sources", "labelKey": "t_sources", "count": "sources", "blocks": [{"type": "sources"}]},
        ],
    }


def stem_of(arg: str) -> str:
    for ext in (".json", ".html", ".htm"):
        if arg.lower().endswith(ext):
            return arg[: -len(ext)]
    return arg


def resolve_paths(args: list[str]) -> tuple[Path, Path]:
    """Accept the slips models make when filling in <base>: a missing .json, or only one path.

    Re-running with a corrected command costs a tool call the local model's 32K context
    often cannot afford, so infer the intended paths instead.
    """
    first = args[0]
    json_arg = first if first.lower().endswith(".json") else stem_of(first) + ".json"
    if len(args) == 2:
        second = args[1]
        out_arg = second if second.lower().endswith((".html", ".htm")) else stem_of(second) + ".html"
    else:
        out_arg = stem_of(first) + ".html"
    if json_arg != first:
        warn(f"using {json_arg} -> {out_arg}")
    return Path(json_arg).expanduser(), Path(out_arg).expanduser()


def find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    bundled = sorted(Path.home().glob(".openclaw/tools/node-v*/bin/node"))
    return str(bundled[-1]) if bundled else None


def render_png(html_path: Path) -> None:
    """Run screenshot.js on the exact HTML just written.

    The model used to type the HTML path a second time for the screenshot command and
    sometimes mistyped it (e.g. "acer-travel--plan"), losing the PNG. A failed or slow
    screenshot is reported but never fails fill.py: the HTML is already done.
    """
    png_path = html_path.with_suffix(".png")
    node = find_node()
    if not node:
        print("Screenshot skipped: node not found")
        return
    script = Path(__file__).resolve().parent / "screenshot.js"
    try:
        result = subprocess.run(
            [node, str(script), str(html_path), str(png_path)],
            capture_output=True, text=True, timeout=150,
        )
        output = (result.stdout + result.stderr).strip()
        # "Screenshot saved ..." is on stdout; a note such as "Some images did not finish loading"
        # comes on stderr after it, and the model looks for the saved line to attach the PNG.
        saved = [line for line in result.stdout.splitlines() if line.startswith("Screenshot saved")]
        if result.returncode == 0 and saved:
            print(saved[-1])
        else:
            print(output.splitlines()[-1] if result.returncode == 0 and output else f"Screenshot failed: {output[-300:]}")
    except subprocess.TimeoutExpired:
        print("Screenshot failed: timed out")


def render(template: str, data: dict, json_path: Path, out_path: Path, pending: bool = False) -> None:
    """Writes the HTML for the trip with the stop details saved beside it so far. A pending page
    (research still walking) says so and reloads itself until the update arrives."""
    import details

    STOP_DETAILS.clear()
    STOP_DETAILS.update(details.load(json_path))
    SPOTS.clear()
    SPOTS.update(details.load_spots(json_path))
    DETAIL_LANGUAGE["value"] = details.trip_language(data)
    dest_match = DEST_RE.search(json_path.name)
    # fill() edits the days it is given (photos, details, merged repeats): give it a copy.
    html_out = fill(template, json.loads(json.dumps(data)), dest_match.group(1).replace("-", " ") if dest_match else "")
    if pending:
        html_out = add_pending_notice(html_out, DETAIL_LANGUAGE["value"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_out, encoding="utf-8", newline="\n")
    leftover = TOKEN_RE.findall(html_out)
    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes, leftover tokens: {len(leftover)})", flush=True)


def add_pending_notice(page: str, language: str) -> str:
    """A banner, and a script that reloads the page until the details are in (the update after the
    research rewrites the file without it). The screenshot (a driven browser) shows neither."""
    notice = html.escape(PENDING_NOTICE.get(language, PENDING_NOTICE["en"]))
    block = f"""<div class="details-pending" role="status">{notice}</div>
<script>
(function () {{
  var banner = document.currentScript.previousElementSibling;
  if (navigator.webdriver || Date.now() - {int(time.time() * 1000)} > {PENDING_GIVE_UP_S * 1000}) {{
    banner.remove();
    return;
  }}
  function reload() {{
    // Not while a photo is open: wait for the lightbox to close.
    if (document.querySelector('.lightbox:not([hidden])')) return setTimeout(reload, 2000);
    try {{ sessionStorage.setItem('atp-scroll:' + location.pathname, String(window.scrollY)); }} catch (e) {{}}
    location.reload();
  }}
  setTimeout(reload, {PENDING_RELOAD_S * 1000});
}})();
</script>
"""
    at = page.rfind("</body>")
    return page[:at] + block + page[at:] if at >= 0 else page + block


# prepare.py's early page of the skeleton (ACER_TRAVEL_DRAFT=1) only starts the walk: the trip's
# one result is the model's own fill.py (SKILL.md Step 4). The app takes a "fill" event as the
# result, so this run sends none and asks for no update after the walk either (an update that
# reached the app before the model's fill.py split the trip into two activity cards and closed the
# walk half-way). Read once and dropped, so the walk this run starts is an ordinary one.
DRAFT = os.environ.pop("ACER_TRAVEL_DRAFT", "") == "1"


def publish(template: str, data: dict, json_path: Path, out_path: Path, announce: bool = True) -> None:
    """Sends the finished plan to the tray's Agent browser, then renders the PNG."""
    if DRAFT:
        return
    if not announce:
        render_png(out_path.resolve())
        return
    live_emit({
        "type": "fill",
        "template": template,
        "values": LAST_FILL.get("values", {}),
        "rowsToken": "MORE_DAYS",
        "rowsTag": "div",
        "rows": LAST_FILL.get("rows", []),
        "out": str(out_path.resolve()),
        # The panel draws the result from this spec; the user's edits come back through
        # the spec's apply script (docs/AGENT_PANEL_UI_SPEC.md).
        "spec": ui_spec(LAST_FILL.get("trip", data), json_path, out_path),
    })
    render_png(out_path.resolve())


# fill.py waits this long for the Journey research (started in the background) before it writes
# the plan, then finishes the details lookups by FIRST_DETAILS_DEADLINE_S. With the screenshot
# (2-3 s) the command stays under ~10 s: the exec tool moves a longer one to the background, and
# a local model polling it tends to re-run fill.py or lose the step. Stops the research has not
# reached yet arrive with the update after it, and the page reloads itself until then.
FIRST_RESEARCH_WAIT_S = 3.0
FIRST_DETAILS_DEADLINE_S = 4.5
AFTER_RESEARCH_DETAILS_BUDGET_S = 20
# Trial: fill.py waits for the whole research before writing the plan (no pending page), but
# only so long: the exec tool stops waiting at 120 s and the model never sees the result (then
# it polls or runs fill.py again). The details (up to AFTER_RESEARCH_DETAILS_BUDGET_S), the page
# and the PNG take up to ~35 s more, so a walk not done by then finishes in the background and
# the plan is sent again with its details, as without the trial.
WAIT_FOR_ALL_RESEARCH = True
WAIT_FOR_ALL_RESEARCH_LIMIT_S = 75
# A page waiting for details reloads this often, and gives up after PENDING_GIVE_UP_S.
PENDING_RELOAD_S = 5
PENDING_GIVE_UP_S = 300
PENDING_NOTICE = {
    "zh": "景點介紹與照片補充中，頁面會自動更新…",
    "en": "Adding stop details and photos. This page updates itself.",
}


def update_stop_details(data: dict, json_path: Path, budget_s: float) -> None:
    """Writes the details file from what the research found so far (render() reads it)."""
    import details

    dest_match = DEST_RE.search(json_path.name)
    dest = dest_match.group(1).replace("-", " ") if dest_match else ""
    try:
        dest_point = destination_coords(dest)  # cached here, so the photos do not look it up again
    except Exception:
        dest_point = None
    try:
        found = details.refresh(json_path, data, dest, budget_s=budget_s, dest_point=dest_point)
    except Exception as exc:  # the plan is complete without them
        warn(f"stop details skipped ({str(exc)[:120]})")
        return
    print(f"Stop details: {len(found)} stops", flush=True)


# The model's words for a trip prepare.py laid out: acer-travel-plan-<dest>-<N>d-v1.5.0-notes.json.
NOTES_SUFFIX = "-notes.json"


def workspace_dir() -> Path:
    return Path(os.environ.get("ACER_TRAVEL_WORKSPACE") or Path.home() / ".openclaw" / "workspace")


def current_trip() -> Path | None:
    """The trip built (or set up by prepare.py) last, from the workspace's CURRENT_TRIP_FILE."""
    try:
        trip = Path((workspace_dir() / CURRENT_TRIP_FILE).read_text(encoding="utf-8").strip())
    except OSError:
        return None
    return trip if trip.name.endswith(".json") else None


def notes_path(json_path: Path) -> Path:
    return json_path.with_name(json_path.with_suffix("").name + NOTES_SUFFIX)


def trip_of_notes(path: Path) -> Path | None:
    """The trip a notes file belongs to, or None when the path is not a notes file."""
    if path.name.endswith(NOTES_SUFFIX):
        return path.with_name(path.name[: -len(NOTES_SUFFIX)] + ".json")
    return None


# The restaurants of SKILL.md's notes example: a trip elsewhere that names them copied the example.
EXAMPLE_MEALS = {"daruma", "ramen yokocho", "sankaku market"}


def copied_meals(trip: dict, meals: object) -> bool:
    """True when the notes' meals are SKILL.md's example on a trip that does not go there."""
    if not isinstance(meals, list):
        return False
    names = {fold(text(m, "name")) for m in meals if isinstance(m, dict)}
    where = fold(json.dumps(trip.get("days") or [], ensure_ascii=False) + json.dumps(trip.get("stays") or [], ensure_ascii=False))
    return bool(names & EXAMPLE_MEALS) and not any(fold(text(m, "area")) in where
                                                   for m in meals if isinstance(m, dict) and text(m, "area"))


def only_the_name(note: str, place: str) -> bool:
    """A note that says no more than the place's name ("大通公園" for 大通公園 (Odori Park))."""
    left = fold(note)
    for name in place_parts(place):
        if name:
            left = left.replace(fold(name), "")
    return not re.sub(r"[\W_]", "", left)


def merge_notes(trip: dict, notes: dict) -> dict:
    """The trip with the model's notes in it. A notes day is {"title", "notes": ["..", ".."]} (or
    just the list): its notes go to the day's items in order, a {"place", "note"} entry also changes
    the place. A day written with full items ({"time", "place", "note"}) replaces the day. Every
    other key (title, meals, climate...) replaces the trip's."""
    merged = {k: v for k, v in trip.items() if k != "days"}
    merged.update({k: v for k, v in notes.items() if k != "days" and v not in (None, "", [], {})})
    if copied_meals(trip, notes.get("meals")) and trip.get("meals"):
        warn("meals are SKILL.md's example; the travel guide's restaurants are kept")
        merged["meals"] = trip["meals"]
    days = [dict(d) for d in trip.get("days") or [] if isinstance(d, dict)]
    for index, written in enumerate(notes.get("days") or []):
        if isinstance(written, list):
            written = {"notes": written}
        if not isinstance(written, dict):
            continue
        items = written.get("items")
        full = isinstance(items, list) and bool(items) and all(isinstance(i, dict) and text(i, "place") for i in items)
        if index >= len(days):
            if full:
                days.append(written)
            continue
        day = days[index]
        for key in ("title", "route"):
            if text(written, key):
                day[key] = text(written, key)
        if full:
            day["items"] = items
            continue
        said = written.get("notes") if isinstance(written.get("notes"), list) else items if isinstance(items, list) else []
        slots = [dict(i) for i in day.get("items") or [] if isinstance(i, dict)]
        # One note short and the day has an arrival or departure slot: the notes skipped it (they
        # are for the places), so they go to the places in order instead of sliding one slot up.
        targets = slots
        places = [slot for slot in slots if not slot.get("travel")]
        if len(said) < len(slots) and len(said) == len(places):
            targets = places
        for slot, note in zip(targets, said):
            if isinstance(note, dict):
                place = text(note, "place")
                if place and place != text(slot, "place"):
                    slot["place"] = place
                    slot.pop("spot", None)
                    slot.pop("travel", None)
                note = text(note, "note")
            if str(note or "").strip() and not only_the_name(str(note), text(slot, "place")):
                slot["note"] = str(note).strip()
        day["items"] = slots
    merged["days"] = days
    return merged


def take_notes(json_path: Path) -> None:
    """Merges the trip's notes file into the trip JSON when it is newer (the model wrote it since)."""
    notes = notes_path(json_path)
    try:
        if not notes.is_file() or (json_path.is_file() and notes.stat().st_mtime <= json_path.stat().st_mtime):
            return
        written = json.loads(notes.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"invalid JSON in {notes.name}: {exc}. Write it again, shorter: one short note per slot")
    except OSError:
        return
    if not isinstance(written, dict):
        die(f"{notes.name} must be a JSON object")
    try:
        trip = json.loads(json_path.read_text(encoding="utf-8")) if json_path.is_file() else {}
    except (OSError, ValueError):
        trip = {}
    merged = merge_notes(trip if isinstance(trip, dict) else {}, written)
    temp = json_path.with_name(json_path.name + ".tmp")
    temp.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, json_path)


def main(argv: list[str]) -> int:
    after_research = "--after-research" in argv
    argv = [a for a in argv if a != "--after-research"]
    if len(argv) == 1:
        # No path (SKILL.md Step 4): the trip prepare.py set up last, with the notes the model wrote.
        trip = current_trip()
        if trip is None:
            print("Usage: python3 scripts/fill.py [<trip.json>] [<out.html>] [--after-research]", file=sys.stderr)
            return 2
        argv = [argv[0], str(notes_path(trip) if notes_path(trip).is_file() else trip)]
    if len(argv) not in (2, 3):
        print("Usage: python3 scripts/fill.py [<trip.json>] [<out.html>] [--after-research]", file=sys.stderr)
        return 2
    json_path, out_path = resolve_paths(argv[1:])
    # Given the notes file (the one the model just wrote): build its trip.
    trip_path = trip_of_notes(json_path)
    if trip_path:
        json_path, out_path = trip_path, trip_path.with_suffix(".html")
    take_notes(json_path)
    dest_of_trip = DEST_RE.search(json_path.name)
    use_reach_of(dest_of_trip.group(1).replace("-", " ") if dest_of_trip else "")
    skill_dir = Path(__file__).resolve().parent.parent
    template_path = skill_dir / "template.html"

    if not json_path.is_file():
        die(f"JSON not found: {json_path}")
    if not template_path.is_file():
        die(f"template.html not found: {template_path}")

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"invalid JSON: {exc}. If the file was cut off, rewrite it shorter: 2 slots per day, one short note each")
    if not isinstance(data, dict):
        die("JSON root must be an object")

    template = template_path.read_text(encoding="utf-8")
    if after_research:
        # research.py, once its walk is done: every stop's details go into the plan, which is
        # sent again (the Agent panel updates in place) with a new PNG. The model's notes may
        # arrive meanwhile (the walk began on prepare.py's skeleton): then it is built again with
        # them, so this older version never ends up on top.
        for _ in range(3):
            read_at = json_path.stat().st_mtime
            update_stop_details(data, json_path, AFTER_RESEARCH_DETAILS_BUDGET_S)
            render(template, data, json_path, out_path)
            take_notes(json_path)
            if json_path.stat().st_mtime == read_at:
                break
            data = json.loads(json_path.read_text(encoding="utf-8"))
        publish(template, data, json_path, out_path)
        return 0

    # The Journey walks the stops in the background (scripts/research.py). fill.py waits a few
    # seconds for it, so a trip of places looked up before comes out complete; otherwise the
    # page says the details are coming and research.py rebuilds it with them when it is done.
    started = time.monotonic()
    # prepare.py's early page looks the stops up quietly while the model writes; the model's own
    # fill.py (SKILL.md Step 4) walks them on screen, from what is saved by then.
    research = start_research(json_path, prefetch=True) if DRAFT else research_in_background(json_path)
    if WAIT_FOR_ALL_RESEARCH and not DRAFT:
        # Trial: the plan is written only once every stop is looked up, so the HTML and PNG are
        # complete and the walk on screen has ended by then.
        finished = wait_for_research(json_path, research, WAIT_FOR_ALL_RESEARCH_LIMIT_S)
        waited = time.monotonic() - started
        update_stop_details(data, json_path, AFTER_RESEARCH_DETAILS_BUDGET_S if finished else 2.0)
        print(f"Waited {waited:.1f} s for the research, {time.monotonic() - started:.1f} s with the details.", flush=True)
    else:
        finished = wait_for_research(json_path, research, FIRST_RESEARCH_WAIT_S)
        update_stop_details(data, json_path, max(1.0, FIRST_DETAILS_DEADLINE_S - (time.monotonic() - started)))
    pending = False if DRAFT else ask_for_details_after_research(json_path, research)
    if not pending and not finished:
        update_stop_details(data, json_path, 1.0)  # the walk ended meanwhile: take what it found
    render(template, data, json_path, out_path, pending=pending)

    # Remember which trip was built last, so a later "change day 2" typed in chat patches this
    # one (the plan the panel is showing) and not whichever trip the conversation mentions.
    try:
        (json_path.resolve().parent / CURRENT_TRIP_FILE).write_text(str(json_path.resolve()) + "\n", encoding="utf-8")
    except OSError:
        pass

    # The tray's Agent browser shows the plan once, complete: research.py sends it when every
    # lookup is done (the walk on screen stops there); without a walk going on, right away. The
    # PNG is made now either way, for the reply.
    publish(template, data, json_path, out_path, announce=not pending)
    print("More stop details are added in the background; nothing to do." if pending
          else "Stop details complete; nothing to do.", flush=True)
    return 0


def research_running_path(json_path: Path) -> Path:
    return json_path.with_name("." + json_path.name + ".research-running")


def details_pending_path(json_path: Path) -> Path:
    return json_path.with_name("." + json_path.name + ".details-pending")


@contextlib.contextmanager
def research_handover_lock(json_path: Path):
    """Held by fill.py while it decides whether to ask for the update, and by research.py while
    it finishes: exactly one of them rebuilds the plan with the details."""
    try:
        import fcntl
        handle = open(json_path.with_name("." + json_path.name + ".research-lock"), "a")
    except (ImportError, OSError):
        yield
        return
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def research_alive(json_path: Path) -> bool:
    """A walk of this trip is still going (the one fill.py started on prepare.py's skeleton): its
    mark is there and holds the pid of a running research.py."""
    try:
        pid = int(research_running_path(json_path).read_text(encoding="utf-8").split()[1])
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, IndexError):
        return False


def research_in_background(json_path: Path):
    """Starts scripts/research.py; it removes the running mark when its walk is done. A walk still
    going for the same places is not started twice: None, and fill.py waits on its mark."""
    if research_alive(json_path):
        return None
    try:
        details_pending_path(json_path).unlink()  # an ask left by an earlier run of this trip
    except OSError:
        pass
    try:
        research_running_path(json_path).write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass
    process = start_research(json_path)
    if process is not None:
        try:
            # The pid, for research_alive(); only while the walk has not already removed the mark.
            mark = research_running_path(json_path)
            if mark.exists():
                mark.write_text(f"{int(time.time())} {process.pid}", encoding="utf-8")
        except OSError:
            pass
    return process


def wait_for_research(json_path: Path, research, limit_s: float) -> bool:
    """True when the research finished within limit_s."""
    deadline = time.monotonic() + limit_s
    while True:
        if not research_running_path(json_path).exists() or (research is not None and research.poll() is not None):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def ask_for_details_after_research(json_path: Path, research=None) -> bool:
    """When the research is still walking, asks it to rebuild the plan (`fill.py
    --after-research`) once done. True when the details are still coming (the page is pending)."""
    with research_handover_lock(json_path):
        if not research_running_path(json_path).exists() or (research is not None and research.poll() is not None):
            return False
        return request_details_after_research(json_path)


def request_details_after_research(json_path: Path) -> bool:
    try:
        details_pending_path(json_path).write_text(str(int(time.time())), encoding="utf-8")
        return True
    except OSError:
        return False


def start_research(json_path: Path, prefetch: bool = False):
    """Walks the stops in the background (scripts/research.py) so the reply is not held up. With
    prefetch it only looks them up and saves them, showing nothing (prepare.py's early page)."""
    script = Path(__file__).resolve().parent / "research.py"
    try:
        return subprocess.Popen([sys.executable, str(script), str(json_path)] + (["--prefetch"] if prefetch else []),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError as exc:
        warn(f"journey research did not start ({exc})")
        try:
            research_running_path(json_path).unlink()
        except OSError:
            pass
        return None


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except FillError as exc:
        raise SystemExit(exc.code)

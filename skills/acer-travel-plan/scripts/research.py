#!/usr/bin/env python3
"""Walk the trip's stops one by one and report each to the Journey window.

Usage: python3 research.py <trip.json>

fill.py starts this in the background after the plan is built, so the chat reply is not held
up and the model takes no part: for every stop it looks the place up on English Wikipedia
(short summary, article link, photo) and falls back to Wikimedia Commons for a photo. Each
step goes to the tray as a "journey" event (docs/AGENT_PANEL_UI_SPEC.md is for the widget;
this stream is only drawn by the Journey window). A stop that cannot be found is reported
as "missing"; nothing here fails the trip.
"""
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import queue
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import details
import enrich
import official
import fill

MAX_STOPS = 40
SUMMARY_SENTENCES = 2
PACE_SECONDS = 0.3

# Command-line switches (see main): --warm fills the cache without showing anything, --demo
# prefers cached results and replays them step by step.
QUIET = False
REFRESH = False
DEMO_FLAG = False


def send(event: dict) -> None:
    """One line on the live socket, tagged as a journey event; never fails."""
    if QUIET or _MUTED.is_set() or not hasattr(socket, "AF_UNIX"):
        return
    payload = dict(event, v=1, script="journey", pid=os.getpid(), ts=int(time.time() * 1000))
    fill.log_event(payload)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(fill.LIVE_SOCKET)
            sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError:
        pass


# The window follows the stop it heard of last. With four stops looked up at once, a stop already
# saved flashed by unseen (the first day was skipped) and one still finding its photo was left for
# the next one. The lookups stay parallel; what the window is told goes out in the plan's order,
# and each stop stays on screen a moment before the next one starts.
# The whole walk takes about WALK_SHOW_S whatever the trip's length (a 7-day trip's 19 stops stay
# about 2.4 s each, an 18-day trip's 54 about 0.8 s), within STOP_SHOW_RANGE_S.
STOP_SHOW_S = 1.5
WALK_SHOW_S = 45.0
STOP_SHOW_RANGE_S = (0.8, 2.5)
_ORDER: "InOrder | None" = None
# Once everything is looked up the plan goes out and the walk ends on screen: nothing more is sent
# to the window.
_MUTED = threading.Event()


def stop_show_s(count: int) -> float:
    """How long each stop stays on screen so the walk lasts about WALK_SHOW_S."""
    low, high = STOP_SHOW_RANGE_S
    return min(high, max(low, WALK_SHOW_S / max(1, count)))
_local = threading.local()


class InOrder:
    """Holds each stop's events until every stop before it has been shown, then sends them.
    stop_showing() ends the walk on screen at once: the stops not shown yet go by unseen (the
    lookups finished before the walk did, so the result is not kept waiting for it)."""

    _END = object()

    def __init__(self, count: int, show_s: float | None = None):
        self.show_s = show_s
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.next = 0
        self.held: dict[int, list[dict]] = {}
        self.done: set[int] = set()
        self.out: queue.Queue = queue.Queue()
        self.sender = threading.Thread(target=self._send, daemon=True)
        self.sender.start()

    def put(self, index: int, event: dict) -> None:
        with self.lock:
            if index == self.next:
                self.out.put(event)
            else:
                self.held.setdefault(index, []).append(event)

    def finish(self, index: int) -> None:
        with self.lock:
            self.done.add(index)
            while self.next in self.done:
                self.out.put(self.next)  # the stop just shown stays a moment
                self.next += 1
                for event in self.held.pop(self.next, []):
                    self.out.put(event)

    def stop_showing(self) -> None:
        self.stopped.set()

    def close(self) -> None:
        self.out.put(self._END)
        self.sender.join()

    def _send(self) -> None:
        while True:
            event = self.out.get()
            if event is self._END:
                return
            if self.stopped.is_set():
                continue
            if isinstance(event, int):
                self.stopped.wait(STOP_SHOW_S if self.show_s is None else self.show_s)
            else:
                send(event)


def emit(op: str, **fields) -> None:
    event = {"type": "journey", "op": op, **fields}
    index = getattr(_local, "index", None)
    if _ORDER is not None and index is not None:
        _ORDER.put(index, event)
    else:
        send(event)


def stops_of(trip: dict) -> list[dict]:
    """Every item with a place, in trip order, with a stable id (s<day>-<index>)."""
    stops: list[dict] = []
    for number, day in enumerate(trip.get("days") or [], start=1):
        for index, item in enumerate(day.get("items") or []):
            name = str(item.get("place") or "").strip()
            # prepare.py's arrival, departure and free-time slots ("Minami返程") are not places.
            if name and not item.get("travel"):
                stop = {"id": f"s{number}-{index}", "day": number, "time": str(item.get("time") or ""), "name": name}
                if item.get("spot"):
                    stop["spot"] = str(item["spot"])
                stops.append(stop)
    return stops[:MAX_STOPS]


def lookup_name(stop: dict) -> str:
    """The name a stop is looked up and saved under: the travel guide's name of a spot picked from
    prepare.py's table ("Asahi-dake", as prepare.py prefetched it), else the plan's own name."""
    return str(stop.get("lookup") or stop["name"])


def spot_of(stop: dict, spots: dict) -> dict:
    """The table spot a stop is (by its code, else by its name), or {}."""
    spot = spots.get(stop.get("spot", "")) if spots else None
    return spot if isinstance(spot, dict) else details.spot_for(stop["name"], spots or {})[1]


def name_spots(stops: list[dict], spots: dict) -> None:
    """Gives each stop picked from prepare.py's table its lookup name."""
    for stop in stops:
        spot = spot_of(stop, spots)
        if spot.get("name"):
            stop["lookup"] = str(spot["name"])


def plan_days(trip: dict, stops: list[dict]) -> list[dict]:
    """The day rail: every day of the trip, even one whose items have no place."""
    days = []
    for number, day in enumerate(trip.get("days") or [], start=1):
        mine = [{"id": s["id"], "name": s["name"], "time": s["time"]} for s in stops if s["day"] == number]
        days.append({"n": number, "title": str(day.get("title") or ""), "stops": mine})
    return days


def file_url(name: str) -> str:
    """A Commons file as a stable image URL (Special:FilePath redirects to the sized thumbnail)."""
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{urllib.parse.quote(name)}?width={fill.PHOTO_SIZE}"


def entity_picks(item: str) -> dict:
    """Wikidata's picks for one item: P18 photo, P242 locator map, P373 Commons category, and the
    English Wikipedia title, P856 website, P1329 phone and P625 coordinates. Curated by editors, so they usually show the place itself."""
    data = fill.wiki_get("www.wikidata.org", {"action": "wbgetentities", "ids": item,
                                               "props": "claims|sitelinks", "sitefilter": "enwiki"})
    entity = data.get("entities", {}).get(item, {})
    claims = entity.get("claims", {})

    def values(prop: str) -> list[str]:
        found = []
        for claim in claims.get(prop, []):
            value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(value, str) and value:
                found.append(value)
        return found

    def point() -> tuple[float, float] | None:
        for claim in claims.get("P625", []):
            value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(value, dict) and value.get("latitude") is not None:
                return (float(value["latitude"]), float(value["longitude"]))
        return None

    photos = values("P18")
    locators = values("P242")
    categories = values("P373")
    return {
        "item": item,
        "photo": file_url(photos[0]) if photos else "",
        "locator": file_url(locators[0]) if locators else "",
        "category": categories[0] if categories else "",
        "enwiki": (entity.get("sitelinks", {}).get("enwiki") or {}).get("title", ""),
        "website": (values("P856") or [""])[0],
        "phone": (values("P1329") or [""])[0],
        "coords": point(),
    }


WIKIMEDIA_IMAGE_HOSTS = ("upload.wikimedia.org", "thumb.wikimedia.org", "commons.wikimedia.org")


def commons_file_source(image_url: str) -> dict | None:
    """The Commons file page behind a photo URL (for credit and licence), or None for a foreign URL."""
    parsed = urllib.parse.urlparse(image_url)
    if parsed.hostname not in WIKIMEDIA_IMAGE_HOSTS:
        return None
    path = urllib.parse.unquote(parsed.path)
    if "/Special:FilePath/" in path:
        name = path.split("/Special:FilePath/", 1)[1]
    elif "/thumb/" in path:
        parts = path.split("/thumb/", 1)[1].split("/")
        name = parts[2] if len(parts) >= 3 else ""
    else:
        name = path.rsplit("/", 1)[-1]
    if not name:
        return None
    page = urllib.parse.quote(name.replace(" ", "_"))
    return {"kind": "photo", "site": "commons.wikimedia.org", "title": name, "url": f"https://commons.wikimedia.org/wiki/File:{page}"}


def stop_sources(article: dict | None, picks: dict, image: str, category: str) -> list[dict]:
    """Every page the stop's result came from: the article, the Wikidata item, the photo's file
    page, and the Commons category the photo was taken from."""
    sources: list[dict] = []
    if article and article.get("url"):
        sources.append({"kind": "article", "site": "en.wikipedia.org", "title": article.get("title", ""), "url": article["url"]})
    if picks.get("item"):
        sources.append({"kind": "wikidata", "site": "wikidata.org", "title": picks["item"], "url": f"https://www.wikidata.org/wiki/{picks['item']}"})
    file_source = commons_file_source(image) if image else None
    if file_source:
        sources.append(file_source)
    if category:
        page = urllib.parse.quote(f"Category:{category}".replace(" ", "_"), safe=":")
        sources.append({"kind": "category", "site": "commons.wikimedia.org", "title": f"Category:{category}", "url": f"https://commons.wikimedia.org/wiki/{page}"})
    return sources


def wikidata_picks(title: str) -> dict:
    """The picks for an English Wikipedia article (via its Wikidata item). Empty when it has none."""
    data = fill.wiki_get("en.wikipedia.org", {"action": "query", "titles": title, "prop": "pageprops",
                                               "ppprop": "wikibase_item", "redirects": "1"})
    pages = data.get("query", {}).get("pages", [])
    item = (pages[0].get("pageprops") or {}).get("wikibase_item") if pages else None
    return entity_picks(item) if item else {}


def names_match(name: str, title: str) -> bool:
    """A hit is about this place when its title has the name's first word and half of its words:
    "Osaka" is not "Osaka Museum of Housing and Living", "Sapporo Clock Tower" is "Clock Tower"."""
    words = fill.place_words(name)
    title = fill.fold(title)
    return not words or (words[0] in title and sum(w in title for w in words) * 2 >= len(words))


def wikidata_search(name: str, language: str) -> dict:
    """Finds the Wikidata item for a place by name, in English or Chinese. The item's label must
    start with the name's first word, so "Gundam Base" never matches an unrelated "Base" item."""
    data = fill.wiki_get("www.wikidata.org", {"action": "wbsearchentities", "search": name,
                                               "language": language, "uselang": language,
                                               "type": "item", "limit": "7"})
    words = fill.place_words(name) or [fill.fold(name)]
    hits = [(hit, fill.fold(hit.get("label", "") or hit.get("match", {}).get("text", ""))) for hit in data.get("search", [])]
    # An exact label wins: "明治神宮" must not pick the stadium "明治神宮球場" listed before the shrine.
    for hit, label in hits:
        if label == fill.fold(name):
            return {"id": hit.get("id", ""), "label": hit.get("label", "")}
    # A prefix match is only trusted for Latin names: a Chinese name that is not an exact label
    # would otherwise match a longer, different item ("明治神宮" -> "明治神宮棒球場").
    if not fill.CJK_RE.search(name):
        for hit, label in hits:
            if words and names_match(name, label):
                return {"id": hit.get("id", ""), "label": hit.get("label", "")}
    return {}


def clean_summary(text: str) -> str:
    """Drops the pronunciation guides Wikipedia puts in the first sentence, such as [sẽ̞ꜜɰ̃so̞ːʑi]."""
    text = re.sub(r"\s*\[[^\]]*\]", "", text)
    text = re.sub(r"\(\s*[,;]?\s*\)", "", text)
    text = re.sub(r"\s+([,;])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def article_from_title(title: str) -> dict | None:
    """The English Wikipedia article with this exact title: summary, link, lead photo, coordinates."""
    data = fill.wiki_get("en.wikipedia.org", {
        "action": "query", "titles": title, "prop": "extracts|pageimages|info|coordinates",
        "exintro": "1", "explaintext": "1", "exsentences": str(SUMMARY_SENTENCES),
        "piprop": "thumbnail", "pithumbsize": str(fill.PHOTO_SIZE), "inprop": "url", "redirects": "1",
    })
    for page in data.get("query", {}).get("pages", []):
        if page.get("missing") or not page.get("extract"):
            continue
        return {
            "title": page.get("title", ""),
            "summary": clean_summary(page["extract"]),
            "url": page.get("fullurl", ""),
            "image": fill.thumbnail_of(page),
            "coords": fill.coords_of(page),
        }
    return None


def commons_photo_relaxed(name: str, query: str, origin: tuple[float, float] | None) -> str:
    """A Commons photo for the place. The file name must start with the place's first word and
    hold half of its words; a photo with coordinates must be near the destination, one without
    coordinates is accepted on its name alone."""
    data = fill.wiki_get("commons.wikimedia.org", {
        "action": "query", "generator": "search", "gsrsearch": query, "gsrnamespace": "6",
        "gsrlimit": "10", "prop": "imageinfo|coordinates", "iiprop": "url|mime", "iiurlwidth": str(fill.PHOTO_SIZE),
    })
    pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
    words = fill.place_words(name)
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        thumb = info.get("thumburl", "").split("?")[0]
        if info.get("mime") not in ("image/jpeg", "image/png") or not thumb.startswith("https://"):
            continue
        title = fill.fold(page.get("title", ""))
        if fill.NOT_A_PHOTO_RE.search(page.get("title", "")):
            continue
        if words and (words[0] not in title or sum(w in title for w in words) * 2 < len(words)):
            continue
        coords = fill.coords_of(page)
        if coords and origin is not None and fill.distance_km(coords, origin) > fill.reach_km():
            continue
        return thumb
    return ""


CAMERA_NAMES = {"img", "dsc", "dscn", "dscf", "pxl", "photo", "image", "pict", "p"}


def of_the_place(url: str, name: str) -> bool:
    """A category photo is of the place itself when its file name starts with one of the place's
    words ("Osaka_Tenmangu01"); "Bonsai_Osaka-tenmangu" and "Furyuningyo_Osaka-tenmangu" show
    something on display there."""
    words = fill.place_words(name)
    file = fill.fold(urllib.parse.unquote(url.rsplit("/", 1)[-1]))
    first = re.match(r"(?:\d+px-)?([^\W\d_]+)", file)
    if not words or not first or first.group(1) in CAMERA_NAMES:
        return True
    return any(first.group(1).startswith(w) or w.startswith(first.group(1)) for w in words)


def category_photo(category: str) -> str:
    """The first usable photo in a Commons category: editors filed these under the place."""
    data = fill.wiki_get("commons.wikimedia.org", {
        "action": "query", "generator": "categorymembers", "gcmtitle": f"Category:{category}",
        "gcmtype": "file", "gcmlimit": "10", "prop": "imageinfo", "iiprop": "url|mime",
        "iiurlwidth": str(fill.PHOTO_SIZE),
    })
    pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        thumb = info.get("thumburl", "").split("?")[0]
        if info.get("mime") in ("image/jpeg", "image/png") and thumb.startswith("https://") \
                and not fill.NOT_A_PHOTO_RE.search(page.get("title", "")):
            return thumb
    return ""


def article_for(query: str, origin: tuple[float, float] | None, dest: str = "") -> dict | None:
    """The English Wikipedia article for the place: its title, summary, link, lead photo, coordinates.
    A plain name ("Clock Tower") is also searched with the destination ("Clock Tower Hokkaido" finds
    "Sapporo Clock Tower")."""
    shared = {
        "prop": "extracts|pageimages|info|coordinates", "exintro": "1", "explaintext": "1",
        "exsentences": str(SUMMARY_SENTENCES), "piprop": "thumbnail",
        "pithumbsize": str(fill.PHOTO_SIZE), "inprop": "url", "redirects": "1", "converttitles": "1",
    }
    # Exact title first (most place names are article titles), then the best search hit.
    tries = [{"titles": query}, {"generator": "search", "gsrsearch": query, "gsrlimit": "1"}]
    if dest and fill.fold(dest) not in fill.fold(query):
        tries.append({"generator": "search", "gsrsearch": f"{query} {dest}", "gsrlimit": "1"})
    for extra in tries:
        searched = "generator" in extra
        data = fill.wiki_get("en.wikipedia.org", {**shared, "action": "query", **extra})
        for page in data.get("query", {}).get("pages", []):
            if page.get("missing") or not page.get("extract"):
                continue
            if not fill.near(page, origin):
                continue
            # A search hit must be about this place: "Nakamise" may find the nearby "Sensō-ji"
            # article, which is close enough by coordinates but is a different stop. The first
            # word of a place name is the distinctive part ("Odaiba" in "Odaiba Seaside Park").
            if searched and not names_match(query, page.get("title", "")):
                continue
            return {
                "title": page.get("title", ""),
                "summary": clean_summary(page["extract"]),
                "url": page.get("fullurl", ""),
                "image": fill.thumbnail_of(page),
                "coords": fill.coords_of(page),
            }
    return None


# ── result cache ─────────────────────────────────────────────────────────
# Each finished stop is saved under "<destination>|<place>". A saved result is reused for a week
# (so editing a trip does not look everything up again); in demo mode it is reused whatever its
# age and replayed step by step, so a demo is the same every time and does not depend on the
# network. Modes: ACER_TRAVEL_CACHE=off turns the cache off; ACER_TRAVEL_DEMO=1, --demo, or an
# empty file named "demo" in the cache folder turns demo mode on. ACER_TRAVEL_CACHE_DIR moves the
# folder (default ~/.openclaw/cache/acer-travel-plan).

CACHE_TTL_SECONDS = 7 * 24 * 3600
# Raised when the lookup rules change, so a stop saved under the old rules is looked up again.
CACHE_RULES = 2
CACHE_MAX_ENTRIES = 500
CACHED_FIELDS = ("title", "summary", "source", "sources", "facts", "image", "gallery", "locator", "coords", "nearby", "steps")
DEMO_STEP_GAP = 0.5


def cache_dir() -> Path:
    return Path(os.environ.get("ACER_TRAVEL_CACHE_DIR") or Path.home() / ".openclaw" / "cache" / "acer-travel-plan")


def cache_mode() -> str:
    """"off", "demo" or "auto"."""
    if os.environ.get("ACER_TRAVEL_CACHE", "").lower() == "off":
        return "off"
    if DEMO_FLAG or os.environ.get("ACER_TRAVEL_DEMO") == "1":
        return "demo"
    try:
        if (cache_dir() / "demo").exists():
            return "demo"
    except OSError:
        pass
    return "auto"


def cache_key(dest: str, name: str) -> str:
    return f"{fill.fold(dest)}|{fill.fold(name)}"


def load_cache() -> dict:
    try:
        data = json.loads((cache_dir() / "research-cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    stops = data.get("stops") if isinstance(data, dict) else None
    return stops if isinstance(stops, dict) else {}


def cache_get(dest: str, name: str, mode: str) -> dict | None:
    """The saved result for a stop: fresh in "auto" mode, of any age in "demo" mode."""
    if REFRESH:
        return None
    entry = load_cache().get(cache_key(dest, name))
    if not isinstance(entry, dict) or not isinstance(entry.get("data"), dict):
        return None
    if mode == "demo":
        return entry["data"]
    if entry.get("incomplete"):
        return None  # a lookup cut short (rate-limited, offline): looked up again
    if entry.get("rules", 1) >= CACHE_RULES and time.time() - float(entry.get("saved", 0)) <= CACHE_TTL_SECONDS:
        return entry["data"]
    return None


# A stop being looked up has a mark in LOOKING_UP_DIR (its pid inside), so another process
# walking the same trip waits for its result instead of looking it up too.
LOOKING_UP_DIR = "looking-up"
LOOKUP_WAIT_S = 60
LOOKUP_STALE_S = 120
_lookup_pause = threading.Event()  # never set: a pause that tests' patched time.sleep leaves alone


def lookup_mark(dest: str, name: str) -> Path:
    return cache_dir() / LOOKING_UP_DIR / hashlib.sha1(cache_key(dest, name).encode("utf-8")).hexdigest()


def claim_lookup(dest: str, name: str, take_over: bool = False) -> bool:
    """True when this process is to look the stop up: nobody else is, or the one who was is gone."""
    mark = lookup_mark(dest, name)
    try:
        mark.parent.mkdir(parents=True, exist_ok=True)
        if take_over or not lookup_held(mark):
            mark.unlink(missing_ok=True)
        handle = os.open(mark, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return True  # no marks possible: look it up
    with os.fdopen(handle, "w") as out:
        out.write(f"{os.getpid()} {time.time()}")
    return True


def lookup_held(mark: Path) -> bool:
    """The mark belongs to a running process that set it lately."""
    try:
        pid, since = mark.read_text(encoding="utf-8").split()
        os.kill(int(pid), 0)
        return time.time() - float(since) <= LOOKUP_STALE_S
    except (OSError, ValueError):
        return False


def release_lookup(dest: str, name: str) -> None:
    mark = lookup_mark(dest, name)
    try:
        if mark.read_text(encoding="utf-8").split()[0] == str(os.getpid()):
            mark.unlink()
    except (OSError, IndexError):
        pass


def wait_for_lookup(dest: str, name: str) -> dict | None:
    """The other process's result once it is done (even an incomplete one: it is what the sites
    gave just now), or None when it found nothing or is taking too long."""
    deadline = time.monotonic() + LOOKUP_WAIT_S
    mark = lookup_mark(dest, name)
    while lookup_held(mark) and time.monotonic() < deadline:
        _lookup_pause.wait(0.5)
    entry = load_cache().get(cache_key(dest, name))
    if isinstance(entry, dict) and isinstance(entry.get("data"), dict) and time.time() - float(entry.get("saved", 0)) <= LOOKUP_STALE_S:
        return entry["data"]
    return None


def cache_put(dest: str, name: str, data: dict, incomplete: bool = False) -> None:
    """Saves a finished stop; never fails the walk. Stops are walked in parallel: one writer at
    a time, so no stop's save overwrites another's. An incomplete one (a lookup step was cut
    short) is still used for the plan, but the next walk looks it up again."""
    with _cache_lock:
        _cache_put(dest, name, data, incomplete)


def _cache_put(dest: str, name: str, data: dict, incomplete: bool = False) -> None:
    # prepare.py's prefetch and the trip's own walk may save at the same time: one at a time.
    with file_lock(cache_dir() / "research-cache.lock"):
        _cache_put_locked(dest, name, data, incomplete)


def file_lock(path: Path):
    import contextlib

    @contextlib.contextmanager
    def held():
        try:
            import fcntl
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, "a")
        except (ImportError, OSError):
            yield
            return
        with handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    return held()


def _cache_put_locked(dest: str, name: str, data: dict, incomplete: bool = False) -> None:
    stops = load_cache()
    stops[cache_key(dest, name)] = {"saved": time.time(), "rules": CACHE_RULES, "data": data,
                                    **({"incomplete": True} if incomplete else {})}
    if len(stops) > CACHE_MAX_ENTRIES:
        newest = sorted(stops.items(), key=lambda kv: kv[1].get("saved", 0), reverse=True)[:CACHE_MAX_ENTRIES]
        stops = dict(newest)
    folder = cache_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "research-cache.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps({"version": 1, "stops": stops}, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, target)
    except OSError as exc:
        print(f"Research: cache not saved ({str(exc)[:80]})")


def replay_cached(stop: dict, data: dict, staged: bool) -> tuple[float, float] | None:
    """Sends a saved result as events. Staged (demo): the five steps tick over one after another,
    as in a live lookup; otherwise the stop starts and finishes at once."""
    states = {s["key"]: s["state"] for s in data.get("steps", [])}
    fields = {k: data[k] for k in CACHED_FIELDS if k in data and k != "steps"}
    emit("stop", id=stop["id"], state="active", steps=step_list(lookup="running"))
    if staged:
        groups = (
            ("lookup", ("title", "summary", "source", "sources")),
            ("photo", ("image", "gallery", "locator")),
            ("guide", ("facts",)),
            ("osm", ("coords",)),
        )
        shown: dict = {}
        done: dict[str, str] = {}
        for index, (key, names) in enumerate(groups):
            time.sleep(DEMO_STEP_GAP)
            done[key] = states.get(key, "done")
            shown.update({n: fields[n] for n in names if n in fields})
            following = STEPS[index + 1]
            emit("stop", id=stop["id"], steps=step_list(**done, **{following: "running"}), **shown)
        time.sleep(DEMO_STEP_GAP)
    emit("stop", id=stop["id"], state="done", **fields, steps=step_list(**states))
    coords = data.get("coords")
    return (coords[0], coords[1]) if coords else None


STEPS = ("lookup", "photo", "guide", "osm", "nearby")  # the order they run in
GALLERY_SIZE = 5
SUMMARY_CHARS = 300
# Stops walked at the same time. Wikipedia and Commons take this easily; OpenStreetMap's one
# request a second is kept by osm_wait across all of them.
PARALLEL_STOPS = 4
# Towns' highlights and official-site checks, looked up while the stops are.
SIDE_WORKERS = 2
_last_osm_call = 0.0
_osm_lock = threading.Lock()
_cache_lock = threading.Lock()


def step_list(**states: str) -> list[dict]:
    """The stop's steps in order; any step not named is still waiting."""
    return [{"key": key, "state": states.get(key, "waiting")} for key in STEPS]


def osm_wait() -> None:
    """Nominatim allows one request a second, from all the stops being walked together."""
    global _last_osm_call
    with _osm_lock:
        gap = enrich.OSM_PACE_SECONDS - (time.time() - _last_osm_call)
        if gap > 0:
            time.sleep(gap)
        _last_osm_call = time.time()


def map_lookup(query: str, dest: str, origin: tuple[float, float] | None) -> dict | None:
    osm_wait()
    return enrich.osm_lookup(query, dest, origin)


def stop_sources_all(article: dict | None, picks: dict, image: str, category: str,
                     guide: dict | None, osm: dict | None) -> list[dict]:
    """The article, Wikidata item, photo file, category, travel-guide page and map entry."""
    sources = stop_sources(article, picks, image, category)
    if guide:
        sources.append({"kind": "guide", "site": "en.wikivoyage.org", "title": guide["page"], "url": guide["url"]})
    if osm:
        sources.append({"kind": "osm", "site": "openstreetmap.org", "title": "OpenStreetMap", "url": osm["url"]})
    return sources


def research_stop(stop: dict, dest: str, origin: tuple[float, float] | None) -> tuple[float, float] | None:
    """Walks one stop and returns its coordinates when known (for the day's route).

    Five steps, each sent to the window as soon as it is done: the Wikipedia article, the
    Wikivoyage listing, the OpenStreetMap entry, the photo (with a small gallery) and the places
    nearby."""
    query = fill.photo_query(lookup_name(stop))
    if not query:
        emit("stop", id=stop["id"], state="missing", steps=step_list(**{k: "skipped" for k in STEPS}))
        return None

    mode = cache_mode()
    cached = cache_get(dest, lookup_name(stop), mode) if mode != "off" else None
    if cached:
        if "highlights" not in cached and details.is_town(cached):
            # Saved before towns kept their highlights: add them once.
            highlights = town_highlights(stop, cached, query, dest, origin)
            if highlights is not None:
                cached = {**cached, "highlights": highlights}
                cache_put(dest, lookup_name(stop), cached)
        return replay_cached(stop, cached, staged=(mode == "demo"))

    if mode != "off" and not claim_lookup(dest, lookup_name(stop)):
        # prepare.py's quiet prefetch and the trip's own walk run at the same time: the stop is
        # looked up once, and the other one takes the result instead of asking the sites again.
        emit("stop", id=stop["id"], state="active", steps=step_list(lookup="running"))
        theirs = wait_for_lookup(dest, lookup_name(stop))
        if theirs:
            return replay_cached(stop, theirs, staged=False)
        claim_lookup(dest, lookup_name(stop), take_over=True)
    try:
        return look_up_stop(stop, dest, origin, query, mode)
    finally:
        release_lookup(dest, lookup_name(stop))


def look_up_stop(stop: dict, dest: str, origin: tuple[float, float] | None, query: str, mode: str) -> tuple[float, float] | None:
    emit("stop", id=stop["id"], state="active", steps=step_list(lookup="running"))
    # A step cut short (rate-limited, offline, timed out) is not an answer: what was found is
    # saved for this plan, but marked so the next walk looks the stop up again.
    cut_short = False
    article = None
    picks: dict = {}
    chinese = bool(fill.CJK_RE.search(query))
    if chinese:
        # A Chinese name: the Chinese Wikipedia knows the English title, and with it the article,
        # the travel guide, the map and the photos can all be looked up in English.
        try:
            english = enrich.english_name(query)
        except Exception as exc:
            cut_short = True
            english = ""
            print(f"Research: {stop['name']}: English name lookup stopped ({str(exc)[:80]})")
        if english:
            query = english
            chinese = False
    # The travel guide needs only the name: it is looked up meanwhile, while the
    # article and the photo are (those two go one after the other: the photo starts from the
    # article's). The window still shows the steps in their order.
    side = ThreadPoolExecutor(max_workers=1)
    guide_job = side.submit(lambda: None if chinese else enrich.guide_listing(query, dest, origin))
    side.shutdown(wait=False)
    try:
        if not chinese:
            article = article_for(query, origin, dest)
        if article:
            picks = wikidata_picks(article["title"])
        else:
            # No English article: Wikidata knows many places by name, in English or Chinese.
            hit = wikidata_search(query, "zh" if chinese else "en")
            if hit:
                picks = entity_picks(hit["id"])
                if picks.get("enwiki"):
                    article = article_from_title(picks["enwiki"])
                    # The item must be this place: an article far away or with no place on the map
                    # ("Clock Tower", the 1995 video game) is another thing of the same name.
                    point = (article or {}).get("coords")
                    if origin is not None and not (point and fill.distance_km(point, origin) <= fill.reach_km()):
                        article, picks = None, {}
    except Exception as exc:  # offline or rate-limited: report the stop as missing, keep walking
        cut_short = True
        print(f"Research: {stop['name']}: lookup stopped ({str(exc)[:80]})")
    time.sleep(PACE_SECONDS)

    info: dict = {}
    if article:
        info = {"title": article["title"], "summary": article["summary"], "source": {"url": article["url"], "site": "en.wikipedia.org"}}
    emit("stop", id=stop["id"], steps=step_list(lookup="done" if article else "missing", photo="running"),
         sources=stop_sources(article, picks, "", ""), **info)

    # The photo comes right after the article (its lead photo is already known): the window shows
    # it at once instead of waiting for the guide and the map, which only add facts.
    # Photo order: the article's lead photo, Wikidata's picked photo, the Commons category
    # the editors filed the place under, then a Commons search (the name must match).
    image = (article or {}).get("image", "")
    category_used = ""
    gallery: list[str] = []
    try:
        image = image or picks.get("photo", "")
        if not image:
            # The category Wikidata filed the place under, else one named after the place itself.
            category_used = picks.get("category") or query
            image = category_photo(category_used)
            if not image:
                category_used = ""
        if not image:
            for search in dict.fromkeys([f"{query} {dest}".strip(), query, f"{query} landmark"]):
                image = commons_photo_relaxed(query, search, origin)
                if image:
                    break
        # A few more photos from the place's category, for the gallery under the main one.
        gallery_category = category_used or picks.get("category") or ""
        if image and gallery_category:
            category_used = category_used or gallery_category
            gallery = [image] + [p for p in enrich.category_photos(gallery_category, GALLERY_SIZE + 4)
                                 if p != image and of_the_place(p, query)][:GALLERY_SIZE - 1]
    except Exception as exc:
        cut_short = True
        print(f"Research: {stop['name']}: photo lookup stopped ({str(exc)[:80]})")
    gallery = gallery or ([image] if image else [])
    emit("stop", id=stop["id"], image=image, gallery=gallery,
         steps=step_list(lookup="done" if article else "missing", photo="done" if image else "missing", guide="running"),
         sources=stop_sources(article, picks, image, category_used), **info)

    # Wikivoyage: hours, price and address, and a description when Wikipedia has no article.
    guide = None
    try:
        guide = guide_job.result()
    except Exception as exc:
        cut_short = True
        print(f"Research: {stop['name']}: travel guide lookup stopped ({str(exc)[:80]})")
    if guide and not info:
        listing = guide["listing"]
        description = listing.get("description") or listing.get("content") or ""
        if description:
            info = {"title": listing.get("name", ""), "summary": description[:SUMMARY_CHARS],
                    "source": {"url": guide["url"], "site": "en.wikivoyage.org"}}
    facts = enrich.build_facts(guide, None)
    emit("stop", id=stop["id"], steps=step_list(lookup="done" if article else "missing", guide="done" if guide else "missing", osm="running",
                                                 photo="done" if image else "missing"),
         facts=facts, sources=stop_sources_all(article, picks, image, category_used, guide, None), **info)

    # Coordinates: the article's, else the travel guide's, else Wikidata's. Only a stop with none
    # of them asks OpenStreetMap (Nominatim allows one request a second for everyone on this
    # network, so it is the first to turn requests away). Website and phone come from Wikidata
    # when the guide has none.
    coords = (article or {}).get("coords") or (guide or {}).get("point") or picks.get("coords")
    osm = None
    if not coords and not chinese:
        try:
            osm = map_lookup(query, dest, origin)
        except Exception as exc:
            cut_short = True
            print(f"Research: {stop['name']}: map lookup stopped ({str(exc)[:80]})")
        if osm:
            coords = (osm["lat"], osm["lon"])
    coords = tuple(coords) if coords else None
    facts = enrich.build_facts(guide, osm or {"website": picks.get("website", ""), "phone": picks.get("phone", "")})
    emit("stop", id=stop["id"],
         steps=step_list(lookup="done" if article else "missing", guide="done" if guide else "missing", osm="done" if coords else "missing",
                         photo="done" if image else "running"),
         facts=facts, coords=list(coords) if coords else None, sources=stop_sources_all(article, picks, image, category_used, guide, osm), **info)

    gallery = gallery or ([image] if image else [])
    # Too few photos: add Commons photos taken near the place. A photo with the place's name in
    # its file name may become the main photo; merely nearby ones only fill the gallery.
    if coords and len(gallery) < GALLERY_SIZE:
        try:
            near = enrich.commons_geo_photos(coords, query, gallery)
            if not image and near["named"]:
                image = near["named"][0]
                gallery = [image]
                near["named"] = near["named"][1:]
            if image:
                gallery = (gallery + near["named"] + near["other"])[:GALLERY_SIZE]
        except Exception as exc:
            cut_short = True
            print(f"Research: {stop['name']}: nearby photo lookup stopped ({str(exc)[:80]})")
    time.sleep(PACE_SECONDS)
    locator = picks.get("locator", "")
    sources = stop_sources_all(article, picks, image, category_used, guide, osm)
    lookup_state = "done" if article else "missing"
    emit("stop", id=stop["id"], image=image, gallery=gallery, locator=locator, sources=sources,
         steps=step_list(lookup=lookup_state, guide="done" if guide else "missing", osm="done" if coords else "missing",
                         photo="done" if image else "missing", nearby="running"),
         facts=facts, coords=list(coords) if coords else None, **info)

    # Places nearby, for "while you are here".
    nearby: list[dict] = []
    if coords:
        try:
            nearby = enrich.nearby_places(coords, (article or {}).get("title", "") or query)
        except Exception as exc:
            cut_short = True
            print(f"Research: {stop['name']}: nearby lookup stopped ({str(exc)[:80]})")
        time.sleep(PACE_SECONDS)

    found = bool(article or guide or osm or image)
    # The last event repeats everything, so a window that opens late still has it all.
    emit("stop", id=stop["id"], state="done" if found else "missing", image=image, gallery=gallery, locator=locator,
         coords=list(coords) if coords else None, sources=sources, facts=facts, nearby=nearby, **info,
         steps=step_list(lookup=lookup_state, guide="done" if guide else "missing", osm="done" if coords else "missing",
                         photo="done" if image else "missing", nearby="done" if nearby else "missing"))
    if found and mode != "off":
        steps_done = step_list(lookup=lookup_state, guide="done" if guide else "missing", osm="done" if coords else "missing",
                               photo="done" if image else "missing", nearby="done" if nearby else "missing")
        saved = {"image": image, "gallery": gallery, "locator": locator, "coords": list(coords) if coords else None,
                 "sources": sources, "facts": facts, "nearby": nearby, "steps": steps_done, **info}
        # A town (a plan that says only "Niseko"): what there is to see and do there, so each of
        # its slots in the plan can show a different place.
        if details.is_town(saved):
            highlights = town_highlights(stop, saved, query, dest, origin)
            if highlights is not None:
                saved["highlights"] = highlights
        cache_put(dest, lookup_name(stop), saved, incomplete=cut_short)
    return coords


def town_highlights(stop: dict, data: dict, query: str, dest: str, origin: tuple[float, float] | None) -> list[dict] | None:
    """The town's highlights, or None when the lookup failed (it is tried again next time)."""
    town = str(data.get("title") or query).split(",")[0].strip()
    if fill.CJK_RE.search(town):
        return None
    try:
        return enrich.city_highlights(town, dest, origin)
    except Exception as exc:
        print(f"Research: {stop['name']}: highlights lookup stopped ({str(exc)[:80]})")
        return None


OFFICIAL_TTL_SECONDS = 3 * 24 * 3600


def check_official(stop: dict, dest: str, site: str = "", notices: bool = False) -> None:
    """Saves what the stop's official site says about its season, under details.official_cache_name;
    read again after OFFICIAL_TTL_SECONDS (a site's notice changes more often than a travel guide)."""
    name = lookup_name(stop)
    entry = load_cache().get(cache_key(dest, details.official_cache_name(name)))
    if isinstance(entry, dict) and time.time() - float(entry.get("saved", 0)) <= OFFICIAL_TTL_SECONDS:
        return
    query = fill.photo_query(name)
    try:
        if fill.CJK_RE.search(query):
            query = enrich.english_name(query)
        found = official.check(query, site, notices) if query or site else {}
    except Exception as exc:
        print(f"Research: {stop['name']}: official site check stopped ({str(exc)[:80]})")
        return
    if found and cache_mode() != "off":
        cache_put(dest, details.official_cache_name(name), found)


def research_town(town: str, dest: str, origin: tuple[float, float] | None) -> None:
    """Saves a town's highlights under details.town_cache_name(town), once."""
    mode = cache_mode()
    name = details.town_cache_name(town)
    if mode == "off" or cache_get(dest, name, mode) is not None:
        return
    english = town
    try:
        if fill.CJK_RE.search(town):
            english = enrich.english_name(town)
        if not english:
            return
        highlights = enrich.city_highlights(english.split(",")[0].strip(), dest, origin)
    except Exception as exc:
        print(f"Research: {town}: highlights lookup stopped ({str(exc)[:80]})")
        return
    cache_put(dest, name, {"title": english, "highlights": highlights})


def main(argv: list[str]) -> int:
    global QUIET, REFRESH, DEMO_FLAG
    flags = {a for a in argv[1:] if a.startswith("--")}
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) != 1 or not flags <= {"--demo", "--warm", "--prefetch"}:
        print("Usage: python3 scripts/research.py <trip.json> [--demo] [--warm] [--prefetch]\n"
              "  --warm  look everything up again and save it to the cache, showing nothing\n"
              "  --demo  use saved results whatever their age and replay them step by step\n"
              "  --prefetch  look the stops up and save them, showing nothing (prepare.py)",
              file=sys.stderr)
        return 2
    DEMO_FLAG = "--demo" in flags
    QUIET = REFRESH = "--warm" in flags
    # prepare.py: look the table's spots up while the model writes the plan; nothing is shown.
    QUIET = QUIET or "--prefetch" in flags
    path = Path(args[0]).resolve()
    trip = json.loads(path.read_text(encoding="utf-8"))
    dest_match = fill.DEST_RE.search(path.name)
    dest = dest_match.group(1).replace("-", " ") if dest_match else ""
    fill.use_reach_of(dest)

    stops = stops_of(trip)
    name_spots(stops, details.load_spots(path))
    emit("plan", dest=dest, title=str(trip.get("title") or ""), days=plan_days(trip, stops))
    # With every stop saved there is nothing to look up, so the network is not needed at all.
    mode = cache_mode()
    all_saved = mode != "off" and bool(stops) and all(cache_get(dest, lookup_name(s), mode) for s in stops)
    origin = None
    if not all_saved:
        try:
            origin = fill.destination_coords(dest) if dest else None
        except Exception as exc:
            print(f"Research: destination lookup stopped ({str(exc)[:80]})")

    def walk(index: int) -> tuple[float, float] | None:
        stop = stops[index]
        _local.index = index
        try:
            return research_stop(stop, dest, origin)
        except Exception as exc:  # one bad stop must not stop the walk
            print(f"Research: {stop['name']}: {str(exc)[:80]}")
            emit("stop", id=stop["id"], state="missing")
            return None
        finally:
            _local.index = None
            if _ORDER is not None:
                _ORDER.finish(index)

    global _ORDER
    _ORDER = None if QUIET else InOrder(len(stops), stop_show_s(len(stops)))
    order = _ORDER

    # Several stops at once: an 18-day trip has 30-40 of them, too many to walk one by one
    # before the plan's details are wanted. The walk on screen goes on meanwhile, one stop after
    # another, until everything below is looked up: then it stops where it is and the plan goes
    # out once, complete (a destination already saved flashes by; a new one walks most of the way).
    # "二世谷滑雪場", "札幌市區購物": the town in the name gets its highlights too, so those
    # slots can show the places there. Nothing of it goes to the Journey window.
    towns = sorted({details.town_of(s["name"]) for s in stops} - {""})
    jobs: list = []
    if not QUIET:
        # The places the traveller named: what their own sites say about the season.
        named = official.named_stops(stops, details.load_wishes(path), lookup_name)
        # And the table's spots of the plan whose season the guide leaves open, when it gives
        # their own site.
        spots = details.load_spots(path)
        more = official.unclear_stops(stops, lambda stop: spot_of(stop, spots), named)
        # The named places also get one search for closures other sites mention.
        jobs = [(stop, "", True) for stop in named] + [(stop, site, False) for stop, site in more]
    # The towns and the official sites need only the stops' names: they are looked up alongside
    # the stops (on SIDE_WORKERS of their own) instead of after the last one.
    with ThreadPoolExecutor(max_workers=PARALLEL_STOPS) as pool, ThreadPoolExecutor(max_workers=SIDE_WORKERS) as side:
        side_jobs = ([side.submit(research_town, town, dest, origin) for town in towns]
                     + [side.submit(check_official, stop, dest, site, notices) for stop, site, notices in jobs])
        found = list(pool.map(walk, range(len(stops))))
        for job in side_jobs:
            job.result()

    if order is not None:
        order.stop_showing()
        order.close()
        _ORDER = None
    send_routes(stops, found)
    emit("end")
    _MUTED.set()
    add_details_if_asked(path)
    return 0


def send_routes(stops: list[dict], found: list) -> None:
    """A schematic route per day: the stops with coordinates, in order, joined by lines. No map
    tiles are loaded, so there is no basemap and no tile-server usage policy to meet."""
    points: dict[int, list[dict]] = {}
    for stop, coords in zip(stops, found):
        if coords:
            day_points = points.setdefault(stop["day"], [])
            day_points.append({"id": stop["id"], "name": stop["name"], "lat": coords[0], "lon": coords[1]})
            if len(day_points) >= 2:
                emit("route", day=stop["day"], points=day_points)


def add_details_if_asked(trip_path: Path) -> None:
    """The walk is done. When fill.py showed the plan before that and asked for the stops' details,
    rebuild it now with everything the walk found (the panel updates in place). A walk that only
    saves (--prefetch, --warm) owns no running mark and asked for nothing."""
    if QUIET:
        return
    with fill.research_handover_lock(trip_path):
        research_finished(trip_path)
        pending = fill.details_pending_path(trip_path)
        if QUIET or not pending.exists():
            return
        try:
            pending.unlink()
        except OSError:
            return
    script = Path(__file__).resolve().parent / "fill.py"
    try:
        subprocess.run([sys.executable, str(script), str(trip_path), "--after-research"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Research: details not added ({str(exc)[:80]})")


def research_finished(trip_path: Path) -> None:
    """Tells fill.py, waiting for this walk, that it is over."""
    try:
        fill.research_running_path(trip_path).unlink()
    except OSError:
        pass


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    finally:
        # A walk that stopped early is over too: fill.py must not wait for it. (A walk that only
        # saves never had the mark, which may be the shown walk's.)
        for arg in sys.argv[1:]:
            if arg.endswith(".json") and not QUIET:
                research_finished(Path(arg))

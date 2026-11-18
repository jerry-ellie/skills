"""More about a stop, for the Journey walk (used by research.py).

- Wikivoyage: the travel-guide listing for the place (description, hours, price, address, website).
- OpenStreetMap (Nominatim): coordinates, opening hours, address, website, phone and what kind of
  place it is. Data is (c) OpenStreetMap contributors, ODbL.
- Wikipedia: places nearby, found by coordinates.
- Wikimedia Commons: more photos from the place's category.
- Wikivoyage again, for a town: what to see and do there (city_highlights).

Nominatim's usage policy allows one request per second and needs an identifying User-Agent, so
research.py spaces those calls (OSM_PACE_SECONDS) and the requests carry fill.WIKI_USER_AGENT.
Every function here raises on a network error; research.py catches it per step.
"""
import json
import re
import urllib.parse
import urllib.request

import fill

OSM_PACE_SECONDS = 1.1
NEARBY_RADIUS_M = 1500
NEARBY_LIMIT = 4
MAX_FACT_CHARS = 160
LISTING_RE = re.compile(r"\{\{\s*(see|do|eat|drink|buy|sleep|listing)\s*(?=\|)", re.I)
NOT_A_PLACE_RE = re.compile(r"\b(station|line|exit)\b", re.I)


def http_json(url: str, params: dict) -> object:
    """GET a JSON document with the skill's User-Agent."""
    request = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": fill.WIKI_USER_AGENT, "Accept": "application/json"},
    )
    return fill.open_json(request)


def page_url(host: str, title: str) -> str:
    return f"https://{host}/wiki/" + urllib.parse.quote(title.replace(" ", "_"), safe=":")


# ── English names for Chinese place names ────────────────────────────────

def english_name(name: str) -> str:
    """The English Wikipedia title for a place named in Chinese, found through the Chinese
    Wikipedia's language link ("明治神宮" -> "Meiji Shrine"). Empty when there is none.

    The article is looked up by title first (redirects and Traditional/Simplified variants are
    followed). Failing that, a search hit counts only when its title contains the name or the name
    contains its title ("台場海濱公園" and "御台場海濱公園"), so an unrelated page is never taken."""
    params = {"action": "query", "redirects": "1", "converttitles": "1", "prop": "langlinks", "lllang": "en", "lllimit": "1"}
    data = fill.wiki_get("zh.wikipedia.org", {**params, "titles": name})
    landed = ""
    for page in data.get("query", {}).get("pages", []):
        links = page.get("langlinks") or []
        if not page.get("missing") and links:
            return str(links[0].get("title", ""))
        if not page.get("missing"):
            landed = page.get("title", "")  # the page exists (maybe after a redirect) but has no English link
    data = fill.wiki_get("zh.wikipedia.org", {**params, "generator": "search", "gsrsearch": name, "gsrlimit": "5"})
    pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
    for page in pages:
        title = page.get("title", "")
        links = page.get("langlinks") or []
        if links and title and (name in title or title in name):
            return str(links[0].get("title", ""))
    # Last resort: Wikidata items whose Chinese label is exactly the name (or the page it landed on)
    # carry an English label even when the Chinese article has no English link.
    for label in dict.fromkeys(filter(None, [name, landed])):
        english = wikidata_english_label(label)
        if english:
            return english
    return ""


def wikidata_english_label(zh_label: str) -> str:
    """The English label of the Wikidata item whose Chinese label is exactly zh_label."""
    data = fill.wiki_get("www.wikidata.org", {"action": "wbsearchentities", "search": zh_label, "language": "zh",
                                               "uselang": "zh", "type": "item", "limit": "7"})
    for hit in data.get("search", []):
        if (hit.get("label") or "") != zh_label:
            continue
        item = hit.get("id", "")
        entity = fill.wiki_get("www.wikidata.org", {"action": "wbgetentities", "ids": item, "props": "labels", "languages": "en"})
        label = entity.get("entities", {}).get(item, {}).get("labels", {}).get("en", {}).get("value", "")
        if label:
            return str(label)
    return ""


# ── Wikivoyage ───────────────────────────────────────────────────────────

def template_body(text: str, start: int) -> str | None:
    """The text of a {{template}}, from just after its opening braces to its matching closing ones."""
    depth = 1
    i = start
    while i < len(text) - 1:
        pair = text[i:i + 2]
        if pair == "{{":
            depth += 1
            i += 2
        elif pair == "}}":
            depth -= 1
            if depth == 0:
                return text[start:i]
            i += 2
        else:
            i += 1
    return None


def split_fields(body: str) -> list[str]:
    """Splits template text on its top-level pipes, leaving [[a|b]] and {{x|y}} whole."""
    fields: list[str] = []
    buf: list[str] = []
    square = curly = 0
    i = 0
    while i < len(body):
        two = body[i:i + 2]
        if two == "[[":
            square += 1
        elif two == "]]":
            square = max(0, square - 1)
        elif two == "{{":
            curly += 1
        elif two == "}}":
            curly = max(0, curly - 1)
        if two in ("[[", "]]", "{{", "}}"):
            buf.append(two)
            i += 2
            continue
        if body[i] == "|" and square == 0 and curly == 0:
            fields.append("".join(buf))
            buf = []
        else:
            buf.append(body[i])
        i += 1
    fields.append("".join(buf))
    return fields


def clean_markup(value: str) -> str:
    value = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", value)  # [[page|text]] -> text
    value = re.sub(r"\{\{[^{}]*\}\}", "", value)
    value = re.sub(r"'{2,}", "", value)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_listings(text: str) -> list[dict]:
    """Every see/do/eat/drink/buy/sleep listing in a Wikivoyage page, as {field: text}."""
    listings = []
    for match in LISTING_RE.finditer(text):
        body = template_body(text, match.start() + 2)
        if body is None:
            continue
        data = {"kind": match.group(1).lower()}
        for field in split_fields(body)[1:]:
            if "=" in field:
                key, value = field.split("=", 1)
                data[key.strip().lower()] = clean_markup(value)
        if data.get("name"):
            listings.append(data)
    return listings


def listing_matches(listing: dict, name: str) -> bool:
    """The listing is this place: same name, or its first word and half of the words."""
    words = fill.place_words(name)
    for candidate in (listing.get("name", ""), listing.get("alt", "")):
        folded = fill.fold(candidate)
        if not folded:
            continue
        if folded == fill.fold(name):
            return True
        if words and words[0] in folded and sum(w in folded for w in words) * 2 >= len(words):
            return True
    return False


def listing_point(listing: dict) -> tuple[float, float] | None:
    try:
        return float(listing["lat"]), float(listing["long"])
    except (KeyError, TypeError, ValueError):
        return None


def listing_is_here(listing: dict, text: str, dest: str, origin: tuple[float, float] | None) -> bool:
    """A chain such as "Ichiran" has listings in many cities. A listing with coordinates must be
    near the destination; one without must sit on a page that talks about the destination."""
    point = listing_point(listing)
    if point is not None:
        # With no destination coordinates to compare against, the listing is taken as it is.
        return origin is None or fill.distance_km(point, origin) <= fill.reach_km()
    if dest:
        return text.lower().count(dest.lower()) >= 2
    return True


def guide_listing(query: str, dest: str, origin: tuple[float, float] | None = None) -> dict | None:
    """The Wikivoyage listing for the place: {"listing": {...}, "page": title, "url": link, "point": (lat, lon) or None}."""
    data = fill.wiki_get("en.wikivoyage.org", {
        "action": "query", "generator": "search", "gsrsearch": f"{query} {dest}".strip(), "gsrlimit": "3",
        "prop": "revisions", "rvprop": "content", "rvslots": "main",
    })
    pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
    for page in pages:
        revisions = page.get("revisions") or []
        text = (revisions[0].get("slots", {}).get("main", {}).get("content") if revisions else "") or ""
        for listing in parse_listings(text):
            if listing_matches(listing, query) and listing_is_here(listing, text, dest, origin):
                title = page.get("title", "")
                return {"listing": listing, "page": title, "url": page_url("en.wikivoyage.org", title), "point": listing_point(listing)}
    return None


# ── OpenStreetMap ────────────────────────────────────────────────────────

def address_of(hit: dict) -> str:
    parts = hit.get("address") or {}
    chosen: list[str] = []
    street = " ".join(p for p in (parts.get("house_number"), parts.get("road")) if p)
    for value in (street, parts.get("suburb") or parts.get("quarter"), parts.get("city") or parts.get("town") or parts.get("village")):
        if value and value not in chosen:
            chosen.append(value)
    return ", ".join(chosen) or hit.get("display_name", "")


def osm_lookup(query: str, dest: str, origin: tuple[float, float] | None) -> dict | None:
    """The OpenStreetMap entry for the place. The name must contain the place's first word and a
    result far from the destination is ignored."""
    data = http_json("https://nominatim.openstreetmap.org/search", {
        "q": f"{query} {dest}".strip(), "format": "jsonv2", "extratags": "1", "addressdetails": "1", "limit": "3",
        "accept-language": "en",  # English names, so "Sensō-ji" is not returned as "浅草寺"
    })
    words = fill.place_words(query)
    for hit in data if isinstance(data, list) else []:
        text = fill.fold(f"{hit.get('name', '')} {hit.get('display_name', '')}")
        if words and words[0] not in text:
            continue
        try:
            point = (float(hit["lat"]), float(hit["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if origin is not None and fill.distance_km(point, origin) > fill.reach_km():
            continue
        extra = hit.get("extratags") or {}
        return {
            "lat": point[0], "lon": point[1],
            "type": str(hit.get("type") or "").replace("_", " "),
            "address": address_of(hit),
            "hours": extra.get("opening_hours", ""),
            "website": extra.get("website") or extra.get("contact:website", ""),
            "phone": extra.get("phone") or extra.get("contact:phone", ""),
            "url": f"https://www.openstreetmap.org/{hit.get('osm_type', 'node')}/{hit.get('osm_id', '')}",
        }
    return None


# ── Wikipedia nearby, Commons gallery, facts ─────────────────────────────

def nearby_places(coords: tuple[float, float], exclude: str = "") -> list[dict]:
    """Wikipedia articles within about 1.5 km, nearest first; stations and the place itself are left out."""
    data = fill.wiki_get("en.wikipedia.org", {
        "action": "query", "list": "geosearch", "gscoord": f"{coords[0]}|{coords[1]}",
        "gsradius": str(NEARBY_RADIUS_M), "gslimit": "10",
    })
    places: list[dict] = []
    skipped = {fill.fold(exclude)} if exclude else set()
    for hit in data.get("query", {}).get("geosearch", []):
        title = hit.get("title", "")
        if not title or fill.fold(title) in skipped or NOT_A_PLACE_RE.search(title):
            continue
        skipped.add(fill.fold(title))
        places.append({"title": title, "distance": int(hit.get("dist", 0)), "url": page_url("en.wikipedia.org", title)})
        if len(places) >= NEARBY_LIMIT:
            break
    return places


def category_photos(category: str, limit: int) -> list[str]:
    """Usable photos in a Commons category (the place's own category), best-ranked first."""
    data = fill.wiki_get("commons.wikimedia.org", {
        "action": "query", "generator": "categorymembers", "gcmtitle": f"Category:{category}",
        "gcmtype": "file", "gcmlimit": "15", "prop": "imageinfo", "iiprop": "url|mime",
        "iiurlwidth": str(fill.PHOTO_SIZE),
    })
    pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
    photos: list[str] = []
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        thumb = info.get("thumburl", "").split("?")[0]
        if info.get("mime") in ("image/jpeg", "image/png") and thumb.startswith("https://") \
                and not fill.NOT_A_PHOTO_RE.search(page.get("title", "")) and thumb not in photos:
            photos.append(thumb)
            if len(photos) >= limit:
                break
    return photos


GEO_RADIUS_M = 400


def commons_geo_photos(coords: tuple[float, float], name: str, exclude: list[str] | tuple = ()) -> dict:
    """Commons photos taken within about 400 m of the place, for a gallery that has too few.

    {"named": [...], "other": [...]}: "named" photos have the place's first word in the file name
    and may stand in for a missing main photo; "other" are only nearby and are used just to fill
    a gallery that already has a main photo. Photos in `exclude` are left out."""
    data = fill.wiki_get("commons.wikimedia.org", {
        "action": "query", "generator": "geosearch", "ggscoord": f"{coords[0]}|{coords[1]}",
        "ggsradius": str(GEO_RADIUS_M), "ggsnamespace": "6", "ggslimit": "30",
        "prop": "imageinfo", "iiprop": "url|mime", "iiurlwidth": str(fill.PHOTO_SIZE),
    })
    pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
    words = fill.place_words(name)
    skip = set(exclude)
    named: list[str] = []
    other: list[str] = []
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        thumb = info.get("thumburl", "").split("?")[0]
        if info.get("mime") not in ("image/jpeg", "image/png") or not thumb.startswith("https://"):
            continue
        if thumb in skip or fill.NOT_A_PHOTO_RE.search(page.get("title", "")):
            continue
        skip.add(thumb)
        (named if words and words[0] in fill.fold(page.get("title", "")) else other).append(thumb)
    return {"named": named, "other": other}


def build_facts(guide: dict | None, osm: dict | None) -> list[dict]:
    """Practical details, Wikivoyage first and OpenStreetMap filling the gaps: [{"key", "value"}]."""
    listing = (guide or {}).get("listing", {})
    osm = osm or {}
    facts: list[dict] = []

    def add(key: str, value: str) -> None:
        value = (value or "").strip()
        if value and not any(f["key"] == key for f in facts):
            facts.append({"key": key, "value": value[:MAX_FACT_CHARS]})

    add("hours", listing.get("hours") or osm.get("hours", ""))
    add("price", listing.get("price", ""))
    add("address", listing.get("address") or osm.get("address", ""))
    add("phone", listing.get("phone") or osm.get("phone", ""))
    add("website", listing.get("url") or osm.get("website", ""))
    add("type", osm.get("type", ""))
    return facts


# ── a town's highlights ──────────────────────────────────────────────────

HIGHLIGHT_LIMIT = 6
HIGHLIGHT_MATCH_KM = 5
MAX_HIGHLIGHT_DESCRIPTION = 400
# A guide's "Do" section often lists activities, not places: "Skiing", "Hiking", "Onsen".
GENERIC_ACTIVITY_RE = re.compile(
    r"^(ski(ing)?|snowboard(ing)?|hik(e|ing)|bik(e|ing)|cycling|onsens?|hot springs?|swimming|"
    r"shopping|walking|fishing|golf|diving|surfing|rafting|camping|climbing|festivals?|events?|winter sports|summer sports|outdoor activities|sightseeing)$",
    re.I,
)


def city_highlights(town: str, dest: str, origin: tuple[float, float] | None) -> list[dict]:
    """What to see and do in a town, from its Wikivoyage page: for a plan that names only the town
    ("二世谷 (Niseko)" on ten slots), each slot can show a different place there. Each highlight
    has its name, description, hours, price and website, and when Wikipedia knows the place, its
    article title (for the Chinese description) and photo."""
    words = fill.place_words(town)
    if not words:
        return []
    data = fill.wiki_get("en.wikivoyage.org", {
        "action": "query", "generator": "search", "gsrsearch": town, "gsrlimit": "3",
        "prop": "revisions", "rvprop": "content", "rvslots": "main",
    })
    pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
    # The town's own page ("Niseko"), not another one that mentions it.
    page = next((p for p in pages if words[0] in fill.fold(p.get("title", ""))), None)
    if not page:
        return []
    revisions = page.get("revisions") or []
    text = (revisions[0].get("slots", {}).get("main", {}).get("content") if revisions else "") or ""
    guide_url = page_url("en.wikivoyage.org", page.get("title", ""))
    listings = []
    for listing in parse_listings(text):
        description = listing.get("content") or listing.get("description") or ""
        if listing["kind"] not in ("see", "do") or not description or GENERIC_ACTIVITY_RE.match(listing["name"].strip()):
            continue
        # On the town's own page a listing without a point is in the town; one with a point must
        # still be near the destination.
        point = listing_point(listing)
        if point is None or origin is None or fill.distance_km(point, origin) <= fill.reach_km():
            listings.append(listing)
    # Places to see first, and among them the ones with a point on the map.
    listings.sort(key=lambda l: (l["kind"] != "see", listing_point(l) is None))
    highlights = []
    for listing in listings[:HIGHLIGHT_LIMIT]:
        highlight = {
            "name": listing["name"],
            "description": (listing.get("content") or listing.get("description") or "")[:MAX_HIGHLIGHT_DESCRIPTION],
            "hours": listing.get("hours", ""),
            "price": listing.get("price", ""),
            "website": listing.get("url", ""),
            "coords": list(listing_point(listing)) if listing_point(listing) else None,
            "guideUrl": guide_url,
        }
        try:
            highlight.update(highlight_article(listing["name"], town, listing_point(listing)))
        except Exception:
            pass  # the highlight still has its description; just no photo
        highlights.append(highlight)
    return highlights


def highlight_article(name: str, town: str, point: tuple[float, float] | None) -> dict:
    """The Wikipedia article of a highlight, with its photo: {"wikiTitle", "wikiUrl", "image"}, or {}."""
    words = fill.place_words(name)
    if not words:
        return {}
    data = fill.wiki_get("en.wikipedia.org", {
        "action": "query", "generator": "search", "gsrsearch": f"{name} {town}", "gsrlimit": "3",
        "prop": "pageimages|coordinates", "piprop": "thumbnail", "pithumbsize": "960",
    })
    pages = sorted(data.get("query", {}).get("pages") or [], key=lambda p: p.get("index", 99))
    for page in pages:
        title = page.get("title", "")
        # The article is this place: its title has every word of the name ("Niseko Village" is
        # not "Niseko Village Onsen"), and when the guide gives a point, it is there (the general
        # article "Steam clock" has none).
        if not all(w in fill.fold(title) for w in words):
            continue
        coords = (page.get("coordinates") or [{}])[0]
        if point and (coords.get("lat") is None
                      or fill.distance_km(point, (coords["lat"], coords["lon"])) > HIGHLIGHT_MATCH_KM):
            continue
        found = {"wikiTitle": title, "wikiUrl": page_url("en.wikipedia.org", title)}
        thumb = (page.get("thumbnail") or {}).get("source", "")
        if thumb.startswith("https://") and not fill.NOT_A_PHOTO_RE.search(thumb):
            found["image"] = thumb
        return found
    return {}

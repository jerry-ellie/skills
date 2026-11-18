#!/usr/bin/env python3
"""What a place's own website says about when it is open, for the places the traveller named.

The travel guide rarely says when a ropeway runs or a museum closes for the winter, and what it says
can be years old. The place's own site says it today: "シーズン（11/1～5/31） 2,800円", "11月中旬〜12月
上旬は整備のため運休". For the stops the traveller asked for by name (at most MAX_PLACES), the Journey
finds the official site (Wikidata's "official website", else a search whose result is on a domain
named after the place), reads its front page, keeps the sentences that give a period, and judges
from them whether the place is open on the travel dates:

    saved:  {"url", "fetched": "2026-10-24", "sentences": [...]}
    shown:  {"url", "fetched", "season": "✓" | "✗" | "?", "notes": ["..."]}   (verdict(), per trip)

Nothing here is a model's guess: the notes are the site's own words, shown with the date they were
read and a link. A period this module cannot read leaves "?", never a guess.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

import fill

MAX_PLACES = 3
# Besides the named places: plan stops from prepare.py's table whose season the guide does not
# state and whose own site the guide gives (no lookup needed to find it).
MAX_MORE_PLACES = 5
PAGE_BYTES = 600_000
TIMEOUT_S = 8
MAX_NOTES = 2
# Many sites keep the season on a page of its own ("営業時間・料金", "ご利用案内", "Hours"): the
# front page's links whose text says so are read too, at most this many, on the same site.
MAX_SUBPAGES = 2
INFO_LINK_RE = re.compile(r"営業|料金|ご利用|利用案内|案内|運行|時刻|期間|インフォ|info|hours|price|fare|"
                          r"season|guide|opening|schedule|visit", re.I)
LINK_RE = re.compile(r'<a\b[^>]*href="([^"#]+)[^"]*"[^>]*>(.*?)</a>', re.I | re.S)
NOTE_CHARS = 110
BROWSER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/122.0.0.0 Safari/537.36")
# Sites about many places: a search result there is not the place's own site.
NOT_OFFICIAL = ("wikipedia.", "wikivoyage.", "tripadvisor.", "japan-guide.", "booking.", "expedia.",
                "facebook.", "instagram.", "youtube.", "x.com", "twitter.", "jalan.", "rakuten.")
SENTENCE_RE = re.compile(
    r"[^。\n]{0,60}(?:運行|運転|営業|休業|運休|休館|開館|期間|シーズン|冬期|夏期|"
    r"open|closed|operat|season|suspend)[^。\n]{0,80}", re.I)
OPEN_WORDS = re.compile(r"運行|運転|営業|開館|シーズン|season|open|operat", re.I)
CLOSED_WORDS = re.compile(r"運休|休業|休館|整備|closed|suspend|maintenance", re.I)
# An exhibition, a sale or an event has a period of its own: it says nothing of the place's season
# ("恐竜糞化石展示期間：9月4日～9月23日" on the Tsutenkaku page).
EVENT_WORDS = re.compile(r"展示|展覧|企画展|特別展|イベント|開催|販売|発売|キャンペーン|フェア|コラボ|受付|予約|"
                         r"exhibit|event|sale|campaign|festival|fair", re.I)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
PART_OF_MONTH = {"上旬": 1, "中旬": 11, "下旬": 21, "early": 1, "mid": 11, "late": 21}
# "11/1～5/31", "11月1日〜5月31日", "11月中旬〜12月上旬", "Nov.1 to May.31", "mid-Nov to early Dec".
YEAR = r"(?:20\d\d\s*年\s*)?"
WEEKDAY = r"\s*(?:[（(][^）)\d]{1,4}[）)])?"
DAY = YEAR + r"(?<!\d)(\d{1,2})\s*[/月]\s*(\d{1,2}|上旬|中旬|下旬)?\s*日?" + WEEKDAY
SEPARATOR = r"\s*[～〜~\-–—から]+\s*"
EN_DAY = (r"(?:\b(early|mid|late)[-\s]+)?\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*"
          r"(\d{1,2})?(?:st|nd|rd|th)?\s*(?:\([a-z]{3}\.?\))?")
RANGE_JA_RE = re.compile(DAY + SEPARATOR + DAY)
# "2026年3月11日（水）～13日（金）": the end in the same month.
RANGE_JA_SAME_RE = re.compile(DAY + SEPARATOR + r"(?<!\d)(\d{1,2})\s*日")
RANGE_EN_RE = re.compile(EN_DAY + r"\s*(?:-|–|—|to|until|through)\s*" + EN_DAY, re.I)
YEAR_RE = re.compile(r"\b(20\d\d)\b|(20\d\d)\s*年")


# ── finding the site ──────────────────────────────────────────────────────

def get(url: str, agent: str = BROWSER_AGENT, limit: int = PAGE_BYTES) -> str:
    # Wikidata often still has the http:// address of a site that now answers only on https.
    tries = [url.replace("http://", "https://", 1), url] if url.startswith("http://") else [url]
    for index, address in enumerate(tries):
        request = urllib.request.Request(address, headers={"User-Agent": agent})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                raw = response.read(limit)
                charset = response.headers.get_content_charset() or page_charset(raw)
            try:
                return raw.decode(charset, errors="replace")
            except LookupError:  # a charset Python does not know
                return raw.decode("utf-8", errors="replace")
        except OSError:
            if index == len(tries) - 1:
                raise
    return ""


META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.I)


def page_charset(raw: bytes) -> str:
    """The charset a page names in its <meta> (many Japanese sites send Shift_JIS without saying
    so in the header), else UTF-8."""
    match = META_CHARSET_RE.search(raw[:4096])
    return match.group(1).decode("ascii", "replace") if match else "utf-8"


def wikidata_site(name: str) -> str:
    """The "official website" (P856) of the Wikidata item found by this English name, or ""."""
    found = json.loads(get("https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode({
        "action": "wbsearchentities", "search": name, "language": "en", "format": "json", "limit": "2",
    }), fill.WIKI_USER_AGENT))
    words = fill.place_words(name)
    for hit in found.get("search", []):
        # The item is this place: its label has every word of the name.
        if not all(w in fill.fold(hit.get("label", "")) for w in words):
            continue
        entity = json.loads(get(f"https://www.wikidata.org/wiki/Special:EntityData/{hit['id']}.json", fill.WIKI_USER_AGENT))
        for claim in entity.get("entities", {}).get(hit["id"], {}).get("claims", {}).get("P856", []):
            value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
    return ""


def searched_site(name: str) -> str:
    """A search result on a domain named after the place ("asahidake.hokkaido.jp"), or ""."""
    page = get("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": f"{name} official site"}))
    words = [re.sub(r"[^a-z]", "", w) for w in fill.place_words(name)]
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', page)[:6]:
        target = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(href)).query).get("uddg", [href])[0]
        host = urllib.parse.urlparse(target).netloc.lower()
        if host and not any(n in host for n in NOT_OFFICIAL) and any(w and w in host.replace("-", "") for w in words):
            return target
    return ""


# ── reading it ────────────────────────────────────────────────────────────

def text_of(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|td|th|h\d)>", "\n", page)
    return re.sub(r"[ \t　]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))


def info_links(page: str, base: str) -> list[str]:
    """Up to MAX_SUBPAGES links of the page, on its own site, whose text or address names opening
    hours, prices or the season."""
    host = urllib.parse.urlparse(base).netloc.lower()
    found: list[str] = []
    for href, label in LINK_RE.findall(page):
        target = urllib.parse.urljoin(base, html.unescape(href.strip()))
        words = re.sub(r"<[^>]+>", " ", label) + " " + target
        if (urllib.parse.urlparse(target).netloc.lower() == host and target.rstrip("/") != base.rstrip("/")
                and target.startswith(("http://", "https://")) and not re.search(r"\.(pdf|jpe?g|png|zip)$", target, re.I)
                and INFO_LINK_RE.search(words) and target not in found):
            found.append(target)
        if len(found) >= MAX_SUBPAGES:
            break
    return found


def period_sentences(text: str) -> list[str]:
    """The site's sentences that name a period: a date range, or a month, with an open/closed word."""
    found: list[str] = []
    for match in SENTENCE_RE.finditer(text):
        sentence = re.sub(r"\s+", " ", match.group(0)).strip()
        if ranges_in(sentence) and sentence not in found:
            found.append(sentence)
    return found


def day_of(month: int, day: str | None, part: str | None, end: bool) -> tuple[int, int]:
    if day and day.isdigit():
        return month, int(day)
    if day in PART_OF_MONTH or part:
        first = PART_OF_MONTH.get(day or part.lower(), 1)
        return month, (first + 9 if end else first)
    return month, (31 if end else 1)


def ranges_in(sentence: str) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Every (from, to) as (month, day) pairs; the year is left out (seasons repeat)."""
    found = []
    for m in RANGE_JA_RE.finditer(sentence):
        a, b = int(m.group(1)), int(m.group(3))
        if 1 <= a <= 12 and 1 <= b <= 12:
            found.append((day_of(a, m.group(2), None, False), day_of(b, m.group(4), None, True)))
    if not found:
        for m in RANGE_JA_SAME_RE.finditer(sentence):
            a = int(m.group(1))
            if 1 <= a <= 12 and m.group(2) and m.group(2).isdigit():
                found.append((day_of(a, m.group(2), None, False), (a, int(m.group(3)))))
    for m in RANGE_EN_RE.finditer(sentence):
        a, b = MONTHS[m.group(2).lower()[:3]], MONTHS[m.group(5).lower()[:3]]
        found.append((day_of(a, m.group(3), m.group(1), False), day_of(b, m.group(6), m.group(4), True)))
    return found


def covers(span: tuple[tuple[int, int], tuple[int, int]], day: date) -> bool:
    start, end = span
    here = (day.month, day.day)
    return start <= here <= end if start <= end else (here >= start or here <= end)


def judge(sentences: list[str], start: date | None, end: date | None) -> tuple[str, list[str]]:
    """"✗" when a closure covers a travel day, "✓" when an open season covers the first day, else
    "?" — with the sentences it was decided from (or the first few, when undecided)."""
    if not start:
        return "?", sentences[:MAX_NOTES]
    days = [start + timedelta(days=i) for i in range(((end or start) - start).days + 1)]
    closed, opened = [], []
    years = {start.year, (end or start).year}
    for sentence in sentences:
        spans = ranges_in(sentence)
        if not spans or EVENT_WORDS.search(sentence):
            continue
        named = {int(a or b) for a, b in YEAR_RE.findall(sentence)}
        if named and not named & years:
            continue  # "2025年4月21日～6月30日": another year's dates
        if CLOSED_WORDS.search(sentence) and any(covers(s, d) for s in spans for d in days):
            closed.append(sentence)
        elif OPEN_WORDS.search(sentence) and not CLOSED_WORDS.search(sentence) and any(covers(s, start) for s in spans):
            opened.append(sentence)
    if closed:
        return "✗", closed[:MAX_NOTES]
    if opened:
        return "✓", opened[:MAX_NOTES]
    return "?", sentences[:MAX_NOTES]


def short(sentence: str) -> str:
    return sentence if len(sentence) <= NOTE_CHARS else sentence[: NOTE_CHARS - 1].rstrip() + "…"


MAX_SENTENCES = 12
MAX_NOTICES = 3
# A closure the site's front page does not mention ("11月中旬〜12月上旬は整備のため運休" was on a
# travel blog, not on the ropeway's page): one search, and only its result snippets are read.
NOTICE_QUERY = "{name} 運休 整備 closed"


def closure_notices(name: str, site: str) -> list[dict]:
    """Closures the web mentions (the place's own notices included): [{"url", "sentence"}], from the
    snippets of one search, only sentences with a closure word and a date range."""
    page = get("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": NOTICE_QUERY.format(name=name)}))
    found: list[dict] = []
    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"', page)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    for href, snippet in zip(links, snippets):
        target = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(href)).query).get("uddg", [href])[0]
        if not urllib.parse.urlparse(target).netloc:
            continue
        for sentence in period_sentences(text_of(snippet)):
            if CLOSED_WORDS.search(sentence) and len(found) < MAX_NOTICES:
                found.append({"url": target, "sentence": sentence})
    return found


def check(name: str, site: str = "", notices: bool = False) -> dict:
    """The official site of one place (given, else found) and its sentences that name a period: {}
    when no site was found or it could not be read. Saved as is; verdict() reads it for a trip's dates."""
    if not site:
        try:
            site = wikidata_site(name)
        except (OSError, ValueError):  # offline, or rate-limited (an HTML page instead of JSON)
            site = ""
    if not site:
        try:
            site = searched_site(name)
        except Exception:
            site = ""
    if not site:
        return {}
    try:
        front = get(site)
    except OSError:
        return {}
    sentences = period_sentences(text_of(front))
    for link in info_links(front, site):
        try:
            sentences += [x for x in period_sentences(text_of(get(link))) if x not in sentences]
        except OSError:
            continue
    saved = {"url": site, "fetched": date.today().isoformat(), "sentences": sentences[:MAX_SENTENCES]}
    if notices:
        try:
            saved["notices"] = closure_notices(name, site)
        except Exception:
            saved["notices"] = []  # rate-limited or offline: the site's own word stands
    return saved


def overlap(a: tuple[tuple[int, int], tuple[int, int]], b: tuple[tuple[int, int], tuple[int, int]]) -> bool:
    """Two (month, day) spans that share a day (neither wraps the new year)."""
    return a[0] <= a[1] and b[0] <= b[1] and a[0] <= b[1] and b[0] <= a[1]


def yearly(notices: list) -> list[dict]:
    """Closures announced for the same time of year in two or more years ("2025年5月12日～23日" and
    "2026年5月14日～22日"): a yearly maintenance, likely again. [{"span", "years", "notice"}]."""
    dated = []
    for notice in notices:
        if not isinstance(notice, dict):
            continue
        sentence = str(notice.get("sentence") or "")
        years = {int(a or b) for a, b in YEAR_RE.findall(sentence)}
        spans = ranges_in(sentence)
        if len(years) == 1 and spans and CLOSED_WORDS.search(sentence):
            dated.append((spans[0], years.pop(), notice))
    found = []
    for i, (span, year, notice) in enumerate(dated):
        years = {year} | {other_year for other_span, other_year, _ in dated[i + 1:] if other_year != year and overlap(span, other_span)}
        if len(years) >= 2 and not any(overlap(span, f["span"]) for f in found):
            found.append({"span": span, "years": sorted(years), "notice": notice})
    return found


def recent(fetched, start: date) -> bool:
    """The trip starts within a year of the day the notice was read."""
    try:
        read = date.fromisoformat(str(fetched))
    except ValueError:
        return False
    return timedelta(0) <= start - read <= timedelta(days=365)


def verdict(saved: dict, start: date | None, end: date | None) -> dict:
    """What the plan shows for a place checked with check(): the season on the travel dates and the
    site's own words it was decided from."""
    if not isinstance(saved, dict) or not str(saved.get("url", "")).startswith(("http://", "https://")):
        return {}
    season, notes = judge([str(s) for s in saved.get("sentences") or []], start, end)
    shown = {"url": saved["url"], "fetched": str(saved.get("fetched") or ""), "season": season,
             "notes": [short(n) for n in notes]}
    # Another site naming a closure on the travel dates: said so, with where it was read.
    for notice in saved.get("notices") or []:
        if not isinstance(notice, dict) or not start:
            continue
        sentence = str(notice.get("sentence") or "")
        years = {int(a or b) for a, b in YEAR_RE.findall(sentence)}
        if years and not years & {start.year, (end or start).year}:
            continue  # "2026年3月11日～13日は運休": not this trip's March
        if not years and not recent(saved.get("fetched"), start):
            continue  # "3月11日～13日は運休" without a year: the next such dates only
        if judge([sentence], start, end)[0] == "✗":
            host = urllib.parse.urlparse(str(notice.get("url") or "")).netloc
            shown["season"] = "✗"
            shown["notes"] = [short(f"{notice['sentence']}（{host}）")] + shown["notes"][:MAX_NOTES - 1]
            return shown
    # The same closure in two or more years: likely again on this trip's dates.
    if start:
        days = [start + timedelta(days=i) for i in range(((end or start) - start).days + 1)]
        for found in yearly(saved.get("notices") or []):
            if any(covers(found["span"], d) for d in days):
                host = urllib.parse.urlparse(str(found["notice"].get("url") or "")).netloc
                (m1, d1), (m2, d2) = found["span"]
                years = "、".join(str(y) for y in found["years"])
                shown["season"] = "✗"
                shown["notes"] = [short(f"往年同期停駛（{years}年 {m1}/{d1}～{m2}/{d2}，{host}）")] + shown["notes"][:MAX_NOTES - 1]
                break
    return shown


def trip_dates(trip: dict) -> tuple[date | None, date | None]:
    match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", str(trip.get("startDate") or ""))
    if not match:
        return None, None
    try:
        start = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None, None
    return start, start + timedelta(days=max(1, len(trip.get("days") or [])) - 1)


def letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", fill.fold(text or ""))


def named_stops(stops: list[dict], names: list[str], name_of) -> list[dict]:
    """The stops the traveller asked for by name ("Asahidake" → "旭岳纜車 (Asahidake Ropeway)"), at
    most MAX_PLACES, one per looked-up name."""
    chosen: list[dict] = []
    seen: set[str] = set()
    for stop in stops:
        key = letters(name_of(stop))
        if key and key not in seen and any(n in key for n in names):
            seen.add(key)
            chosen.append(stop)
        if len(chosen) >= MAX_PLACES:
            break
    return chosen


def unclear_stops(stops: list[dict], spots_of, exclude: list[dict]) -> list[tuple[dict, str]]:
    """Up to MAX_MORE_PLACES (stop, site) pairs: stops from the table whose season is "?" and whose
    travel-guide listing has a website, one per spot, in plan order, the excluded ones left out."""
    chosen: list[tuple[dict, str]] = []
    seen = {id(s) for s in exclude}
    # A spot already checked as a named place is not checked again under another slot.
    names = {str((spots_of(s) or {}).get("name") or "") for s in exclude} - {""}
    for stop in stops:
        spot = spots_of(stop)
        site = str((spot or {}).get("website") or "")
        name = str((spot or {}).get("name") or "")
        if (id(stop) in seen or not spot or spot.get("season") != "?" or name in names
                or not site.startswith(("http://", "https://"))):
            continue
        names.add(name)
        chosen.append((stop, site))
        if len(chosen) >= MAX_MORE_PLACES:
            break
    return chosen


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print('Usage: python3 scripts/official.py "<English name>" [YYYY-MM-DD] [days]', file=sys.stderr)
        raise SystemExit(2)
    first = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else None
    last = first + timedelta(days=int(sys.argv[3]) - 1) if first and len(sys.argv) > 3 else first
    started = time.monotonic()
    print(json.dumps(verdict(check(sys.argv[1]), first, last), ensure_ascii=False, indent=1))
    print(f"took {time.monotonic() - started:.1f}s", file=sys.stderr)

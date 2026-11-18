#!/usr/bin/env python3
"""A day-by-day plan of real places, built from prepare.py's lookup before the model writes the trip.

Given a table of places, the local model still had to decide which town each of 18 days is in and
which places fill 36 slots, and when it ran short it wrote "富良野自然探索" or "溫泉體驗": slots no
lookup can find. This module does that part: it picks the towns (the ones the traveller named
first), gives each a share of the days by how much there is to see, orders them along a route
(nearest next town), and fills every slot with a place from the travel guide, the traveller's
wishes first. The first slot of the trip and the last are travel. The model then writes the trip
from these lines, adding notes, meals and stays, and swaps a place only where it has a reason.

    Day 1 | Sapporo | Morning: (travel: arrival) | Afternoon: 大通公園 (Ōdōri Park)
"""
from __future__ import annotations

import math

import fill

MAX_DAYS_PER_TOWN = 4      # a town the traveller named
MAX_DAYS_OTHER_TOWN = 3    # any other town
# The next town is chosen for its value less the way from the town before: one point per this many
# km, so the trip moves on to a good town near by rather than jumping across the region.
KM_PER_POINT = 30
WISHED_BONUS = 1000
# A town with no point on the map (Shikotsu-Toya's page has no marker, its places no coordinates)
# may be anywhere: this much is taken off its value, never nothing.
UNKNOWN_WAY_POINTS = 3
MORE_SPOTS = 8
TIMES = ("Morning", "Afternoon", "Evening")


def slots_per_day(days: int) -> int:
    """Three slots a day (Morning, Afternoon, Evening), however long the trip."""
    return 3


def days_for(is_wished: bool, days: int) -> int:
    """At most this many days in one town: a quarter of the trip, between 1 and the caps above (a
    4-day Tokyo trip sees four wards, not three days of Chiyoda)."""
    cap = MAX_DAYS_PER_TOWN if is_wished else MAX_DAYS_OTHER_TOWN
    return max(1, min(cap, round(days / 4) + (1 if is_wished else 0)))


def town_point(town: str, points: dict, spots: list[dict]) -> tuple[float, float] | None:
    """The town's map point from the region page, else the middle of its places' points."""
    point = points.get(town)
    if point:
        return float(point[0]), float(point[1])
    known = [s["coords"] for s in spots if s.get("coords")]
    if not known:
        return None
    return sum(c[0] for c in known) / len(known), sum(c[1] for c in known) / len(known)


def route(towns: list[str], start: str, point_of) -> list[str]:
    """The towns in visiting order: from `start`, always the nearest town not visited yet; towns
    without a point go last, in their own order."""
    placed = [t for t in towns if point_of(t)]
    rest = [t for t in towns if not point_of(t)]
    if start not in placed:
        return placed + rest
    order = [start]
    left = [t for t in placed if t != start]
    while left:
        here = point_of(order[-1])
        nearest = min(left, key=lambda t: fill.distance_km(here, point_of(t)))
        order.append(nearest)
        left.remove(nearest)
    return order + rest


def build(towns: list[str], listings: dict[str, list[dict]], points: dict, days: int,
          wished: set[str], rank, value=None) -> list[dict]:
    """[{"day": n, "town": "Sapporo", "items": [{"time", "spot" | None, "town", "travel"}]}] for the
    trip's days. `rank(town)` gives the town's places best first (closed ones left out); `wished`
    are the towns the traveller asked for, always visited; `value(town, places, point_of)` orders
    the others (by default, how much there is to see)."""
    per_day = slots_per_day(days)
    ranked = {t: rank(t) for t in towns}
    ranked = {t: spots for t, spots in ranked.items() if spots}
    if not ranked or days < 1:
        return []
    need = days * per_day - 2 if days > 1 else max(1, per_day - 1)  # the trip's first and last slots are travel
    point_of = lambda t: town_point(t, points, ranked.get(t, []))  # noqa: E731
    value = value or (lambda town, spots: len(spots))
    worth = {t: value(t, ranked[t]) + (WISHED_BONUS if t in wished else 0) for t in ranked}
    gateway = next((t for t in towns if t in ranked), next(iter(ranked)))
    # The trip goes town by town from the main city: each time the best town for its value less
    # the way from where the trip is, until the days are full. The towns asked for are always in.
    share: dict[str, int] = {}
    visit: list[str] = []
    here = gateway
    where = point_of(gateway)  # the last point the trip was at (a town without one keeps it)
    home = point_of(gateway)
    left = set(ranked)
    while left and sum(share.values()) < need:
        # Near the end the trip heads back towards the main city, where it flies out.
        ending = need - sum(share.values()) <= days_for(False, days) * per_day

        def gain(town: str) -> float:
            if not where:
                return worth[town]
            there = point_of(town)
            if not there:
                return worth[town] - UNKNOWN_WAY_POINTS
            cost = fill.distance_km(where, there)
            if ending and home:
                cost += fill.distance_km(there, home)
            return worth[town] - cost / KM_PER_POINT
        town = here if here in left else max(left, key=gain)
        left.discard(town)
        most = days_for(town in wished, days) * per_day
        share[town] = min(len(ranked[town]), most, need - sum(share.values()))
        visit.append(town)
        here = town
        where = point_of(town) or where
    # Round each stay to whole days where the town has the places for it, so days do not straddle
    # two towns more than they must.
    for town in share:
        whole = math.ceil(share[town] / per_day) * per_day
        if whole <= len(ranked[town]) and sum(share.values()) - share[town] + whole <= need:
            share[town] = whole

    slots: list[dict] = []
    used: set[str] = set()  # a place listed on two pages (Tsukiji on Chuo's and Tokyo East's) once
    for town in visit:
        taken = 0
        for spot in ranked[town]:
            if taken >= share[town]:
                break
            if spot["name"] in used:
                continue
            used.add(spot["name"])
            slots.append({"spot": spot, "town": town, "travel": ""})
            taken += 1
    last_town = visit[-1] if visit else ""
    while len(slots) < need:
        slots.append({"spot": None, "town": last_town, "travel": "free"})
    slots = slots[:need]
    slots.insert(0, {"spot": None, "town": visit[0] if visit else "", "travel": "arrival"})
    if days > 1:
        slots.append({"spot": None, "town": last_town, "travel": "departure"})

    plan = []
    for number in range(1, days + 1):
        day_slots = slots[(number - 1) * per_day: number * per_day]
        for index, slot in enumerate(day_slots):
            slot["time"] = TIMES[index]
        towns_of_day = list(dict.fromkeys(s["town"] for s in day_slots if s["town"]))
        plan.append({"day": number, "town": " → ".join(towns_of_day), "items": day_slots})
    return plan


def name_of(spot: dict) -> str:
    """"大通公園 (Ōdōri Park)" with a Chinese name, else the guide's name."""
    return f"{spot['zh']} ({spot['name']})" if spot.get("zh") else spot["name"]


def lines(plan: list[dict]) -> list[str]:
    out = []
    for day in plan:
        parts = []
        for item in day["items"]:
            if item["spot"]:
                what = name_of(item["spot"])
            elif item["travel"] == "free":
                what = f"(free time in {item['town']})"
            else:
                what = f"(travel: {item['travel']})"
            parts.append(f"{item['time']}: {what}")
        out.append(f"Day {day['day']} | {day['town'].split('/')[-1]} | " + " | ".join(parts))
    return out


def used_spots(plan: list[dict]) -> list[dict]:
    return [item["spot"] for day in plan for item in day["items"] if item["spot"]]

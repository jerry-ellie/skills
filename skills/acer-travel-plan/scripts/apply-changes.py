#!/usr/bin/env python3
"""Apply the edits made in the Agent browser panel to a trip JSON.

Usage: python3 scripts/apply-changes.py <trip.json> --edits-file <edits.json> [--fill]
       python3 scripts/apply-changes.py <trip.json> --edits-b64 <base64 of a JSON array> [--fill]

The edits are the panel's generic ones (docs/AGENT_PANEL_UI_SPEC.md), with ids from the
UI spec fill.py built: d2 = days[1], d2i0 = its first item, m0 = meals[0], s0 = stays[0].
Removing, reordering, shortening the trip and moving the start date need no judgment, so
they are done here exactly. What needs a decision (a swap, an extra day, a pace or budget
change) becomes an instruction under "ask_model" for the agent to carry out.

Prints one JSON report:
  {"applied": [...], "ask_model": [...], "skipped": [...], "notes": [...],
   "rebuild": "python3 .../fill.py <trip.json>", "regenerated": {...}}
"regenerated" appears only with --fill when nothing is left for the model.
Exit code 0 on success, 1 on bad input (the trip file is left untouched).
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

MAX_EDITS = 100
DATE_RE = re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})")
RANGE_SEP_RE = re.compile(r"\s*([–—-])\s*(?=\d{4})")
MIN_ITEMS_PER_DAY = 2
MIN_MEALS = 3
MIN_STAYS = 1

# Choices the panel offers (fill.py's spec) and what the agent is asked to do for each.
CHOICES = {
    ("pace", "relaxed"): "Make the overall pace more relaxed: at most 2 main stops per day",
    ("pace", "packed"): "Make the overall pace more packed: fit in more stops per day",
    ("budget", "economy"): "Lower the budget: prefer cheaper meals, free attractions and budget stays",
    ("budget", "comfort"): "Raise the budget: prefer comfortable meals and better-rated stays",
    ("style", "family"): "Lean the trip toward family-friendly activities",
    ("style", "food"): "Lean the trip toward food experiences",
    ("style", "culture"): "Lean the trip toward culture and history",
    ("style", "shopping"): "Lean the trip toward shopping",
    ("style", "nature"): "Lean the trip toward nature and outdoors",
    ("style", "rain"): "Add an indoor rainy-day backup for each day",
}


def fail(message: str) -> None:
    print(json.dumps({"error": message}, ensure_ascii=False))
    raise SystemExit(1)


def parse_date(raw: str) -> dt.date | None:
    match = DATE_RE.search(raw or "")
    if not match:
        return None
    try:
        return dt.date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def fmt_date(d: dt.date) -> str:
    return f"{d.year}/{d.month}/{d.day}"


def day_label(d: dt.date) -> str:
    return f"{fmt_date(d)} {d.strftime('%A')}"


def trip_start(trip: dict) -> dt.date | None:
    days = trip.get("days") or []
    # startDate is what the skill's own JSON uses (fill.py derives every date and the range
    # from it); tripDateRange and the first day's date cover older files.
    return (parse_date(trip.get("startDate", "")) or parse_date(trip.get("tripDateRange", ""))
            or (parse_date(days[0].get("date", "")) if days and isinstance(days[0], dict) else None))


def set_range(trip: dict, start: dt.date, n_days: int) -> None:
    end = start + dt.timedelta(days=n_days - 1)
    old = trip.get("tripDateRange", "") or ""
    sep = RANGE_SEP_RE.search(old)
    glue = f" {sep.group(1)} " if sep else " – "
    trip["tripDateRange"] = fmt_date(start) if n_days == 1 else f"{fmt_date(start)}{glue}{fmt_date(end)}"
    if "startDate" in trip:
        trip["startDate"] = fmt_date(start)


def items_of(day: dict) -> list:
    key = "items" if "items" in day else ("slots" if "slots" in day else "items")
    day.setdefault(key, [])
    return day[key]


def place(item: dict) -> str:
    return str(item.get("place", "")).strip()


def apply_edits(trip: dict, edits: list[dict]) -> dict:
    report: dict = {"applied": [], "ask_model": [], "skipped": [], "notes": []}
    days = [d for d in trip.get("days") or [] if isinstance(d, dict)]
    trip["days"] = days

    # Resolve every id against the trip as the panel saw it, before anything moves.
    day_by_id = {f"d{n}": day for n, day in enumerate(days, 1)}
    item_by_id = {}
    for n, day in enumerate(days, 1):
        for i, item in enumerate(items_of(day)):
            if isinstance(item, dict):
                item_by_id[f"d{n}i{i}"] = (day, item)
    named = {
        "meals": {f"m{k}": x for k, x in enumerate(trip.get("meals") or []) if isinstance(x, dict)},
        "stays": {f"s{k}": x for k, x in enumerate(trip.get("stays") or []) if isinstance(x, dict)},
    }

    def number(day: dict) -> int:
        return next(n for n, d in enumerate(trip["days"], 1) if d is day)

    def where(day: dict, item: dict) -> str:
        return f"Day {number(day)} {item.get('time') or ''} “{place(item)}”".replace("  ", " ")

    by_kind: dict[str, list[dict]] = {}
    for e in edits:
        by_kind.setdefault(str(e.get("kind")), []).append(e)
    for e in edits:
        if e.get("kind") not in ("remove", "swap", "reorder", "choose", "set"):
            report["skipped"].append(f"unknown edit kind '{e.get('kind')}'")

    # 1) Reorders: put the stops in the given order; time labels stay in their slots.
    for e in by_kind.get("reorder", []):
        day = day_by_id.get(str(e.get("group")))
        ids = e.get("items") if isinstance(e.get("items"), list) else []
        moved = [item_by_id.get(str(i)) for i in ids]
        if day is None or not moved or any(m is None or m[0] is not day for m in moved):
            report["skipped"].append(f"cannot reorder {e.get('group')}: unknown stops")
            continue
        current = items_of(day)
        order = [m[1] for m in moved]
        rest = [it for it in current if all(it is not o for o in order)]
        slots = [it.get("time") for it in current]
        new = order + rest
        for it, label in zip(new, slots):
            it["time"] = label
        current[:] = new
        report["applied"].append(f"reordered day {number(day)}")
        report["notes"].append(f"Day {number(day)} was reordered: each moved stop's 'hours' and 'transport' may no longer fit")

    # 2) Swaps are worded now, while every stop is still where the user saw it.
    for e in by_kind.get("swap", []):
        block, ref = str(e.get("block")), str(e.get("item"))
        if block == "days" and ref in item_by_id:
            day, item = item_by_id[ref]
            report["ask_model"].append(
                f"Swap {where(day, item)} for a different place of a similar kind in the same area, one that is not already in the plan")
        elif block in named and ref in named[block]:
            kind = block[:-1]
            report["ask_model"].append(f"Swap the {kind} “{named[block][ref].get('name', '')}” for a different one in a similar area")
        else:
            report["skipped"].append(f"cannot swap {block}/{ref}: not found")

    # 3) Removals.
    for e in by_kind.get("remove", []):
        block, ref = str(e.get("block")), str(e.get("item"))
        if block == "days" and ref in item_by_id:
            day, item = item_by_id[ref]
            current = items_of(day)
            current[:] = [it for it in current if it is not item]
            report["applied"].append(f"removed '{place(item)}' from day {number(day)}")
        elif block in named and ref in named[block]:
            entry = named[block][ref]
            trip[block] = [x for x in trip.get(block) or [] if x is not entry]
            report["applied"].append(f"removed {block[:-1]} '{entry.get('name', '')}'")
        else:
            report["skipped"].append(f"cannot remove {block}/{ref}: not found")

    # 4) Trip length and start date.
    for e in by_kind.get("set", []):
        block, value = str(e.get("block")), e.get("value")
        if block == "length" and isinstance(value, int) and not isinstance(value, bool):
            n = len(trip["days"])
            if 1 <= value < n:
                del trip["days"][value:]
                trip["tripDays"] = f"{value} Day" + ("" if value == 1 else "s")
                start = trip_start(trip)
                if start:
                    set_range(trip, start, value)
                report["applied"].append(f"shortened the trip to {value} days")
            elif value > n:
                report["ask_model"].append(
                    f"Extend the trip from {n} to {value} days: add {value - n} new day(s) at the end that continue the existing ones")
            elif value != n:
                report["skipped"].append(f"invalid trip length {value}")
        elif block == "start" and isinstance(value, str) and parse_date(value):
            start = parse_date(value)
            for i, day in enumerate(trip["days"]):
                day["date"] = day_label(start + dt.timedelta(days=i))
            set_range(trip, start, len(trip["days"]))
            report["applied"].append(f"moved the start date to {fmt_date(start)}")
        else:
            report["skipped"].append(f"cannot set {block} to {value!r}")

    # 5) Preferences.
    for e in by_kind.get("choose", []):
        text = CHOICES.get((str(e.get("block")), str(e.get("option"))))
        if text:
            report["ask_model"].append(text)
        else:
            report["skipped"].append(f"unknown choice {e.get('block')}/{e.get('option')}")

    # 6) What the edits left too thin for fill.py.
    for n, day in enumerate(trip["days"], 1):
        left = len(items_of(day))
        if left < MIN_ITEMS_PER_DAY:
            report["ask_model"].append(f"Day {n} has {left} stop(s) left; add a place so it has at least {MIN_ITEMS_PER_DAY}")
    for block, minimum in (("meals", MIN_MEALS), ("stays", MIN_STAYS)):
        count = len(trip.get(block) or [])
        if count < minimum:
            report["ask_model"].append(f"There are {count} {block} left; add one so there are at least {minimum}")
    return report


def load_edits(flag: str, value: str) -> list[dict]:
    if flag == "--edits-b64":
        try:
            edits = json.loads(base64.b64decode(value, validate=True).decode("utf-8"))
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            fail(f"--edits-b64 is not valid base64 JSON: {exc}")
    else:
        edits_path = Path(value).expanduser()
        try:
            edits = json.loads(edits_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"cannot read edits file {edits_path}: {exc}")
        # The tray writes these into the workspace for a single run; do not leave them behind.
        if edits_path.name.startswith(".panel-edits-"):
            try:
                edits_path.unlink()
            except OSError:
                pass
    if not isinstance(edits, list) or not all(isinstance(e, dict) for e in edits):
        fail("edits must be a JSON array of objects")
    if len(edits) > MAX_EDITS:
        fail(f"too many edits (max {MAX_EDITS})")
    return edits


def regenerate(path: Path) -> dict:
    """Re-run fill.py so the report, the preview image and the panel are current.

    fill.py renders the PNG itself; output names follow the trip file
    (<name>.json -> <name>.html / <name>.png).
    """
    here = Path(__file__).resolve().parent
    html = path.with_suffix(".html")
    png = path.with_suffix(".png")
    result: dict = {"html": str(html)}
    started = time.time() - 1
    try:
        fill = subprocess.run([sys.executable, str(here / "fill.py"), str(path)],
                              capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**result, "filled": False, "error": f"fill.py did not run: {exc}"}
    if fill.returncode != 0:
        detail = (fill.stderr or fill.stdout).strip().splitlines()[-1:] or ["fill.py failed"]
        return {**result, "filled": False, "error": detail[0]}
    result["filled"] = True
    fresh = png.exists() and png.stat().st_size > 0 and png.stat().st_mtime >= started
    result["screenshot"] = str(png) if fresh else None
    return result


def main(argv: list[str]) -> int:
    fill_after = "--fill" in argv
    argv = [a for a in argv if a != "--fill"]
    if len(argv) != 4 or argv[2] not in ("--edits-file", "--edits-b64"):
        print("Usage: python3 scripts/apply-changes.py <trip.json> (--edits-file <edits.json> | --edits-b64 <base64>) [--fill]",
              file=sys.stderr)
        return 2
    path = Path(argv[1]).expanduser()
    if not path.is_file():
        fail(f"trip file not found: {path}")
    try:
        trip = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"trip file is not valid JSON: {exc}")
    if not isinstance(trip, dict) or not isinstance(trip.get("days"), list):
        fail("trip file has no days[]")

    report = apply_edits(trip, load_edits(argv[2], argv[3]))
    report["rebuild"] = f"python3 {Path(__file__).resolve().parent / 'fill.py'} {path.resolve()}"
    if report["applied"]:
        path.write_text(json.dumps(trip, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        # Only when nothing is left for the model: otherwise the model edits the JSON next
        # and runs "rebuild" itself, and an in-between report would be shown for nothing.
        if fill_after and not report["ask_model"]:
            report["regenerated"] = regenerate(path)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

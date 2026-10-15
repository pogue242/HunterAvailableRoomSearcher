"""Command line: build | find | room | grid | buildings.

    python -m hunter_rooms build --term "2026 Fall"
    python -m hunter_rooms find --day We --time 9:30AM --for 2h --building north
    python -m hunter_rooms room "North Bldg 1001B" --day Mo
    python -m hunter_rooms grid --building north --floor 10 --day Mo
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path

from .parse import DAY_CODES, fmt_time, parse_time
from .schedule import CAVEAT, DEFAULT_HOURS, SLOT, Schedule, build, load_sections, write_json
from .scrape import GlobalSearch

DATA = Path("data")
DAY_ALIASES = {"m": "Mo", "mon": "Mo", "monday": "Mo", "t": "Tu", "tue": "Tu", "tues": "Tu",
               "tuesday": "Tu", "w": "We", "wed": "We", "wednesday": "We", "r": "Th", "th": "Th",
               "thu": "Th", "thur": "Th", "thurs": "Th", "thursday": "Th", "f": "Fr", "fri": "Fr",
               "friday": "Fr", "sa": "Sa", "sat": "Sa", "saturday": "Sa", "su": "Su", "sun": "Su",
               "sunday": "Su"}


def parse_day(s: str) -> str:
    k = s.strip().lower()
    for c in DAY_CODES:
        if k == c.lower():
            return c
    if k in DAY_ALIASES:
        return DAY_ALIASES[k]
    raise argparse.ArgumentTypeError(f"unknown day {s!r} (use Mo Tu We Th Fr Sa Su)")


def parse_duration(s: str) -> int:
    """'2h' -> 120, '90m' -> 90, '1h30m' -> 90, '45' -> 45."""
    m = re.fullmatch(r"\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m?)?\s*", s.lower())
    if not m or not (m[1] or m[2]):
        raise argparse.ArgumentTypeError(f"bad duration {s!r} (try 2h, 90m, 1h30m)")
    return int(m[1] or 0) * 60 + int(m[2] or 0)


def parse_clock(s: str) -> int:
    try:
        return parse_time(s.replace(" ", ""))
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad time {s!r} (try 9:30AM)")


def parse_hours(s: str) -> tuple[int, int]:
    a, b = s.split("-")
    return parse_clock(a), parse_clock(b)


def _when(sched: Schedule, args) -> tuple[str, date | None, int]:
    now = datetime.now()
    on = date.fromisoformat(args.date) if getattr(args, "date", None) else None
    day, on = sched.resolve_date(args.day, on)
    t = args.time if getattr(args, "time", None) is not None else now.hour * 60 + now.minute
    return day, on, t


def _date_note(sched: Schedule, on: date | None) -> str:
    if on is None:
        return "any week of the term (busy if a class meets there in any week)"
    if sched.period_index(on) is None:
        return f"{on.isoformat()} (outside the term's class dates: no classes listed)"
    return on.isoformat()


def _floor(f) -> str:
    return "?" if f is None else str(f)


# ---------------------------------------------------------------------------

def cmd_build(args):
    gs = GlobalSearch(args.term, cache_dir=args.cache, delay=args.delay, offline=args.offline)
    if not args.offline:
        gs.sweep(args.subjects)
    pages = gs.result_pages()
    if not pages:
        sys.exit(f"No cached result pages in {gs.dir}; run without --offline first.")
    sections, load_report = load_sections(pages)
    extra = {
        "term_code": gs.code, "result_pages": len(pages),
        "requests_this_run": gs.requests_made,
        "searches_split_by_time": len(gs.time_split_keys()),
        "unsplittable": [k for k, v in gs.manifest.items() if v["kind"] == "exceed-unsplittable"],
        **load_report,
    }
    model, schedule, report = build(sections, args.term, hours=args.hours, slot=SLOT, extra_report=extra)
    out = Path(args.out)
    write_json(model, out / "model.json")
    write_json(schedule, out / "schedule.json", compact=True)
    write_json(report, out / "report.json")
    write_json([s.to_dict() for s in sections], out / "sections.json", compact=True)
    print(f"Term {args.term} ({gs.code}): {report['sections']} sections, "
          f"{report['sections_with_physical_room']} with a physical room, "
          f"{report['rooms']} rooms in {report['buildings']} buildings, "
          f"{report['unparsed_rooms']} unparsed rooms, {report['periods']} date periods.")
    for u in model["unparsed_rooms"]:
        print(f"  unparsed: {u['raw']!r} ({u['reason']})")
    print(f"Wrote {out / 'model.json'}, {out / 'schedule.json'}, {out / 'report.json'}, {out / 'sections.json'}")


def cmd_find(args):
    sched = Schedule.load(args.data)
    day, on, t = _when(sched, args)
    until = t + args.for_ if args.for_ else None
    rooms = sched.find(day, t, on, min_free=args.min_free or 0, until=until,
                       building=args.building, floor=args.floor)
    print(f"Rooms with no class scheduled: {day} {fmt_time(t)}"
          + (f" for {args.for_} min" if args.for_ else "")
          + f"  |  date: {_date_note(sched, on)}")
    if args.building or args.floor is not None:
        print(f"Filters: building={args.building or 'any'} floor={args.floor if args.floor is not None else 'any'}")
    if not rooms:
        print("  (none)")
    cur = None
    for r in rooms:
        key = (r.building, r.floor)
        if key != cur:
            cur = key
            print(f"\n{sched.rooms[r.id]['building_name']} - floor {_floor(r.floor)}")
        mins = r.free_until - t
        tail = "until close" if r.free_until >= sched.he else f"until {fmt_time(r.free_until)}"
        print(f"  {r.id:<16} free {tail:<16} ({mins // 60}h{mins % 60:02d}m)")
    print(f"\n{len(rooms)} room(s). Note: {CAVEAT}")


def cmd_room(args):
    sched = Schedule.load(args.data)
    rid = sched.find_room(args.room)
    if rid is None:
        sys.exit(f"No unique room matches {args.room!r}. Try the full name, e.g. 'North Bldg 937'.")
    day, on, _ = _when(sched, args)
    r = sched.rooms[rid]
    print(f"{rid}  ({r['name']}, floor {_floor(r['floor'])})  {day}  date: {_date_note(sched, on)}")
    for b in sched.timeline(rid, day, on):
        span = f"{fmt_time(b['start']):>7} - {fmt_time(b['end']):<7}"
        if b["state"] == "open":
            print(f"  {span}  open")
        else:
            for i, c in enumerate(b["classes"]):
                dates = f" [{c['start_date']}..{c['end_date']}]" if on is None else ""
                print(f"  {span if i == 0 else ' ' * len(span)}  OCCUPIED  {c['course']} {c['section']}"
                      f" {c['component']} {fmt_time(c['start'])}-{fmt_time(c['end'])}"
                      f" {c['instructor'] or 'Staff'}{dates}")
    print(f"Note: {CAVEAT}")


def cmd_grid(args):
    sched = Schedule.load(args.data)
    day, on, _ = _when(sched, args)
    rids = sched.room_ids(args.building, args.floor)
    if not rids:
        sys.exit("No rooms match that building/floor. See: python -m hunter_rooms buildings")
    step = args.step
    per = step // sched.slot
    ncols = (sched.he - sched.hs) // step
    w = max(len(x) for x in rids)
    # Hour labels every 60 minutes.
    label = [" "] * ncols
    for i in range(ncols):
        m = sched.hs + i * step
        if m % 60 == 0:
            txt = fmt_time(m).replace(":00", "")
            txt = txt[:-1].lower()  # "9A" -> "9a"
            for k, ch in enumerate(txt):
                if i + k < ncols:
                    label[i + k] = ch
    print(f"{day}  date: {_date_note(sched, on)}   '#' class scheduled, '.' open  ({step}-min columns)")
    print(" " * (w + 2) + "".join(label))
    for rid in rids:
        s = sched.slots(rid, day, on)
        row = "".join("#" if "1" in s[i * per:(i + 1) * per] else "." for i in range(ncols))
        print(f"{rid:<{w}}  {row}")
    print(f"Note: {CAVEAT}")


def cmd_buildings(args):
    sched = Schedule.load(args.data)
    for b in sched.d["buildings"]:
        n = sum(1 for r in sched.rooms.values() if r["building"] == b["id"])
        print(f"{b['id']:<8} {b['name']:<14} rooms={n:<4} floors={', '.join(_floor(f) for f in b['floors'])}"
              f"   aliases: {', '.join(b['aliases'])}")


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="python -m hunter_rooms",
                                 description="Hunter College open-classroom finder (CUNY Global Search data).")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="scrape (cached) and write data/model.json + data/schedule.json")
    b.add_argument("--term", default="2026 Fall", help='e.g. "2026 Fall", "2027 Spring"')
    b.add_argument("--offline", action="store_true", help="use cached pages only; no network")
    b.add_argument("--subjects", nargs="*", help="limit the sweep to these subject codes (e.g. CMSC BISC)")
    b.add_argument("--hours", type=parse_hours, default=DEFAULT_HOURS, help="operating hours, e.g. 7:00AM-10:00PM")
    b.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default 1)")
    b.add_argument("--cache", default="cache")
    b.add_argument("--out", default=str(DATA))
    b.set_defaults(func=cmd_build)

    def common(p, need_time=False):
        p.add_argument("--data", default=str(DATA / "schedule.json"))
        p.add_argument("--day", type=parse_day, help="Mo Tu We Th Fr Sa Su (default: today)")
        p.add_argument("--date", help="calendar date YYYY-MM-DD (respects half-term sessions)")
        if need_time:
            p.add_argument("--time", type=parse_clock, help="e.g. 9:30AM (default: now)")

    f = sub.add_parser("find", help="rooms with no class at a day/time")
    common(f, need_time=True)
    f.add_argument("--for", dest="for_", type=parse_duration, help="must stay free this long: 2h, 90m, 1h30m")
    f.add_argument("--min-free", type=parse_duration, help="alias of --for (minutes)")
    f.add_argument("--building", help="id, name or alias: north, HN, 'West Bldg'")
    f.add_argument("--floor", help="floor number or level letter")
    f.set_defaults(func=cmd_find)

    r = sub.add_parser("room", help="one room's open/occupied timeline for a day")
    r.add_argument("room", help="'North Bldg 937', 'NORTH-937', 'west W217'")
    common(r)
    r.set_defaults(func=cmd_room)

    g = sub.add_parser("grid", help="floor heatmap: rooms x time")
    common(g)
    g.add_argument("--building", required=True)
    g.add_argument("--floor")
    g.add_argument("--step", type=int, default=15, choices=[5, 10, 15, 30], help="minutes per column")
    g.set_defaults(func=cmd_grid)

    bl = sub.add_parser("buildings", help="list buildings, floors and aliases")
    bl.add_argument("--data", default=str(DATA / "schedule.json"))
    bl.set_defaults(func=cmd_buildings)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose or args.cmd == "build" else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if args.cmd == "find" and args.min_free and not args.for_:
        args.for_ = args.min_free
        args.min_free = None
    args.func(args)


if __name__ == "__main__":
    main()

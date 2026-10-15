"""Build data/model.json + data/schedule.json, and answer availability queries.

This module is the single source of truth for "is room X free then?". The web
page only reads the slot strings this module writes.

Schedule format (summary):
  periods   date ranges within which the set of meetings is constant
            (half-term sessions create extra periods)
  patterns  de-duplicated slot strings, one char per `slot_minutes` across the
            operating hours: "1" = class scheduled, "0" = no class
  rooms[id].days[Dd].slots[p]   index into patterns for period p
  rooms[id].days[Dd].occupied   the meetings themselves (with date ranges)
  rooms[id].days[Dd].open       hours with no class in ANY week of the term
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .parse import DAY_CODES, Section, fmt_time, parse_results
from .rooms import KNOWN_ALIASES, is_non_room, parse_rooms, room_id

CAVEAT = ("'Open' means no class is scheduled in the room per CUNY Global Search. "
          "It does not mean the room is unlocked, unreserved, or free of events.")
DEFAULT_HOURS = (7 * 60, 23 * 60)  # latest real class ends 10:50PM
SLOT = 5


# ---------------------------------------------------------------------------
# Loading sections from the cache
# ---------------------------------------------------------------------------

def load_sections(pages: list[Path]) -> tuple[list[Section], dict]:
    """Parse cached result pages; de-duplicate by class number."""
    by_num: dict[str, Section] = {}
    rows = 0
    dup_conflicts = []
    for p in pages:
        for s in parse_results(p.read_text(encoding="utf-8")):
            rows += 1
            old = by_num.get(s.class_number)
            if old is None:
                by_num[s.class_number] = s
            elif [m.__dict__ for m in old.meetings] != [m.__dict__ for m in s.meetings]:
                dup_conflicts.append(s.class_number)
                for m in s.meetings:
                    if m not in old.meetings:
                        old.meetings.append(m)
    secs = sorted(by_num.values(), key=lambda s: (s.course, s.section, s.class_number))
    return secs, {"rows_parsed": rows, "duplicate_conflicts": dup_conflicts}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _periods(dates: list[tuple[str, str]]) -> list[tuple[date, date]]:
    """Split the term at every meeting start/end date."""
    if not dates:
        return []
    cuts = set()
    lo = min(date.fromisoformat(a) for a, _ in dates)
    hi = max(date.fromisoformat(b) for _, b in dates)
    for a, b in dates:
        cuts.add(date.fromisoformat(a))
        cuts.add(date.fromisoformat(b) + timedelta(days=1))
    cuts = sorted(c for c in cuts if lo <= c <= hi + timedelta(days=1)) + [hi + timedelta(days=1)]
    cuts = sorted(set(cuts) | {lo})
    return [(cuts[i], cuts[i + 1] - timedelta(days=1)) for i in range(len(cuts) - 1)]


def _floor_key(f):
    return (0, f) if isinstance(f, int) else (-1, 0) if f is None else (-1, str(f))


def build(sections: list[Section], term: str, hours=DEFAULT_HOURS, slot=SLOT,
          extra_report: dict | None = None):
    """Return (model, schedule, report) dicts."""
    hs, he = hours
    nslots = (he - hs) // slot
    now = datetime.now().isoformat(timespec="seconds")

    # Physical rooms and the meetings in them.
    room_meetings: dict[str, list] = defaultdict(list)
    non_room = Counter()
    sections_with_room = set()
    unscheduled_in_room = []  # meetings that have a room but no days/times
    for s in sections:
        for m in s.meetings:
            r = m.room.strip()
            if is_non_room(r):
                non_room[r or "(blank)"] += 1
                continue
            sections_with_room.add(s.class_number)
            if m.start is None:
                unscheduled_in_room.append(f"{s.course} {s.section} ({s.class_number}) in {r}: {m.raw!r}")
            room_meetings[r].append((s, m))

    infos, unparsed = parse_rooms(list(room_meetings))

    # ---- model.json
    bmap: dict[str, dict] = {}
    for raw, info in infos.items():
        if info.building_id is None or info.floor is None:
            continue
        b = bmap.setdefault(info.building_id, {
            "id": info.building_id, "name": info.building_name,
            "aliases": sorted({info.building_id, info.building_name,
                               *KNOWN_ALIASES.get(info.building_name, [])}),
            "floors": {}})
        fl = b["floors"].setdefault(info.floor, [])
        fl.append({"id": room_id(info), "number": info.number, "raw_names": [raw],
                   "sections": len({s.class_number for s, _ in room_meetings[raw]})})
    buildings = []
    for b in sorted(bmap.values(), key=lambda b: b["name"]):
        floors = [{"number": f, "rooms": sorted(rs, key=lambda r: r["number"])}
                  for f, rs in sorted(b["floors"].items(), key=lambda kv: _floor_key(kv[0]))]
        buildings.append({**b, "floors": floors})
    unparsed_list = [{"raw": u.raw, "reason": u.reason, "building": u.building_name,
                      "sections": len({s.class_number for s, _ in room_meetings[u.raw]})}
                     for u in unparsed]
    if unparsed_list:
        # Unparsed rooms that still name a building go under floor: null.
        for u in unparsed:
            if u.building_id and u.building_id in bmap:
                b = next(x for x in buildings if x["id"] == u.building_id)
                nf = next((f for f in b["floors"] if f["number"] is None), None)
                if nf is None:
                    nf = {"number": None, "rooms": []}
                    b["floors"].insert(0, nf)
                nf["rooms"].append({"id": room_id(u), "number": u.number, "raw_names": [u.raw],
                                    "sections": len({s.class_number for s, _ in room_meetings[u.raw]})})
    model = {
        "term": term, "generated_at": now, "caveat": CAVEAT,
        "buildings": buildings,
        "unparsed_rooms": unparsed_list,
        "non_room_strings": dict(non_room.most_common()),
    }

    # ---- schedule.json
    all_dates = [(m.start_date, m.end_date) for ms in room_meetings.values() for _, m in ms
                 if m.start_date and m.end_date and m.start is not None]
    periods = _periods(all_dates)
    patterns: list[str] = []
    pat_idx: dict[str, int] = {}

    def intern(s: str) -> int:
        if s not in pat_idx:
            pat_idx[s] = len(patterns)
            patterns.append(s)
        return pat_idx[s]

    clipped = []
    rooms_out = {}
    for raw, ms in sorted(room_meetings.items()):
        info = infos[raw]
        rid = room_id(info)
        days = {}
        for d in DAY_CODES:
            occ = []
            grids = [bytearray(b"0" * nslots) for _ in periods]
            anyweek = bytearray(b"0" * nslots)
            for s, m in ms:
                if d not in m.days or m.start is None:
                    continue
                occ.append({
                    "start": m.start, "end": m.end, "course": s.course, "title": s.title,
                    "section": s.section, "component": s.component, "class_number": s.class_number,
                    "instructor": ", ".join(s.instructors), "mode": s.mode,
                    "start_date": m.start_date, "end_date": m.end_date,
                })
                if m.start < hs or m.end > he:
                    clipped.append(f"{rid} {d} {fmt_time(m.start)}-{fmt_time(m.end)} {s.course}")
                a = max(0, (m.start - hs) // slot)
                b = min(nslots, -(-(m.end - hs) // slot))
                for pi, (p0, p1) in enumerate(periods):
                    if m.start_date and date.fromisoformat(m.start_date) > p1:
                        continue
                    if m.end_date and date.fromisoformat(m.end_date) < p0:
                        continue
                    grids[pi][a:b] = b"1" * max(0, b - a)
                anyweek[a:b] = b"1" * max(0, b - a)
            occ.sort(key=lambda x: (x["start"], x["end"], x["course"]))
            days[d] = {
                "occupied": occ,
                "open": _runs(anyweek.decode(), "0", hs, slot),
                "slots": [intern(g.decode()) for g in grids],
            }
        rooms_out[rid] = {
            "name": raw, "building": info.building_id, "building_name": info.building_name,
            "floor": info.floor, "number": info.number, "days": days,
        }

    schedule = {
        "term": term, "generated_at": now, "caveat": CAVEAT,
        "hours": {"start": hs, "end": he}, "slot_minutes": slot, "days": list(DAY_CODES),
        "periods": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in periods],
        "patterns": patterns,
        "buildings": [{"id": b["id"], "name": b["name"], "aliases": b["aliases"],
                       "floors": [f["number"] for f in b["floors"]]} for b in buildings],
        "rooms": rooms_out,
    }

    in_person = [s for s in sections if any(m.start is not None for m in s.meetings)]
    report = {
        "term": term,
        "sections": len(sections),
        "sections_with_meeting_times": len(in_person),
        "sections_with_physical_room": len(sections_with_room),
        "meetings": sum(len(s.meetings) for s in sections),
        "rooms": len(rooms_out),
        "rooms_in_model": sum(len(f["rooms"]) for b in buildings for f in b["floors"]
                              if f["number"] is not None),
        "buildings": len(buildings),
        "unparsed_rooms": len(unparsed_list),
        "periods": len(periods),
        "meetings_outside_hours": clipped,
        "room_meetings_without_time": unscheduled_in_room,
        "status_values": dict(Counter(s.status for s in sections)),
        "mode_values": dict(Counter(s.mode for s in sections)),
        "parser_notes": [f"{s.course} {s.section} ({s.class_number}): {n}"
                         for s in sections for n in s.notes],
        **(extra_report or {}),
    }
    return model, schedule, report


def _runs(s: str, ch: str, hs: int, slot: int) -> list[list[int]]:
    out, i = [], 0
    while i < len(s):
        if s[i] == ch:
            j = i
            while j < len(s) and s[j] == ch:
                j += 1
            out.append([hs + i * slot, hs + j * slot])
            i = j
        else:
            i += 1
    return out


def write_json(obj, path: Path, compact=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# Query engine
# ---------------------------------------------------------------------------

@dataclass
class FreeRoom:
    id: str
    name: str
    building: str
    floor: int | str | None
    free_from: int
    free_until: int       # minutes; hours end if free to close


class Schedule:
    def __init__(self, data: dict):
        self.d = data
        self.hs = data["hours"]["start"]
        self.he = data["hours"]["end"]
        self.slot = data["slot_minutes"]
        self.patterns = data["patterns"]
        self.rooms = data["rooms"]
        self.periods = [(date.fromisoformat(p["start"]), date.fromisoformat(p["end"]))
                        for p in data["periods"]]

    @classmethod
    def load(cls, path: str | Path = "data/schedule.json") -> "Schedule":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    # -- dates ------------------------------------------------------------
    @property
    def term_start(self) -> date | None:
        return self.periods[0][0] if self.periods else None

    @property
    def term_end(self) -> date | None:
        return self.periods[-1][1] if self.periods else None

    def period_index(self, d: date) -> int | None:
        for i, (a, b) in enumerate(self.periods):
            if a <= d <= b:
                return i
        return None

    def resolve_date(self, day: str | None, on: date | None, today: date | None = None):
        """Pick the calendar date a query refers to.

        Returns (day_code, date_or_None). None means "any week of the term"
        (a room counts as busy if it's busy in any week)."""
        today = today or date.today()
        if on is not None:
            return DAY_CODES[on.weekday()], on
        if day is None:
            return DAY_CODES[today.weekday()], today
        target = (DAY_CODES.index(day) - today.weekday()) % 7
        d = today + timedelta(days=target)
        if self.term_start and self.term_end:
            if d < self.term_start:
                d = self.term_start + timedelta(days=(DAY_CODES.index(day) - self.term_start.weekday()) % 7)
            if d > self.term_end:
                return day, None
        return day, d

    # -- slots ------------------------------------------------------------
    def slots(self, rid: str, day: str, on: date | None) -> str:
        idx = self.rooms[rid]["days"][day]["slots"]
        if not idx:
            return "0" * ((self.he - self.hs) // self.slot)
        if on is None:  # busy in any week
            strs = [self.patterns[i] for i in idx]
            return "".join("1" if any(s[k] == "1" for s in strs) else "0" for k in range(len(strs[0])))
        p = self.period_index(on)
        if p is None:   # outside the term: no classes scheduled
            return "0" * ((self.he - self.hs) // self.slot)
        return self.patterns[idx[p]]

    def _slot_of(self, t: int) -> int:
        return (t - self.hs) // self.slot

    def free_until(self, rid: str, day: str, t: int, on: date | None) -> int | None:
        """None if busy at t, else the minute the room next becomes busy (or closes)."""
        if not (self.hs <= t < self.he):
            return None
        s = self.slots(rid, day, on)
        i = self._slot_of(t)
        if s[i] == "1":
            return None
        j = s.find("1", i)
        return self.he if j < 0 else self.hs + j * self.slot

    def is_free(self, rid: str, day: str, start: int, end: int, on: date | None) -> bool:
        if start < self.hs or end > self.he or end <= start:
            return False
        s = self.slots(rid, day, on)
        a, b = self._slot_of(start), -(-(end - self.hs) // self.slot)
        return "1" not in s[a:b]

    # -- filters ----------------------------------------------------------
    def building_matches(self, rid: str, query: str | None) -> bool:
        if not query:
            return True
        q = query.strip().lower()
        r = self.rooms[rid]
        for b in self.d.get("buildings", []):
            if b["id"] == r["building"]:
                return q in {a.lower() for a in b["aliases"]} or q == b["id"].lower()
        return False

    def room_ids(self, building: str | None = None, floor=None) -> list[str]:
        out = []
        for rid, r in self.rooms.items():
            if r["building"] is None or r["floor"] is None:
                continue  # unparsed rooms are reported, not searched
            if not self.building_matches(rid, building):
                continue
            if floor is not None and str(r["floor"]).upper() != str(floor).upper():
                continue
            out.append(rid)
        return sorted(out, key=lambda x: (self.rooms[x]["building"], _floor_key(self.rooms[x]["floor"]),
                                          self.rooms[x]["number"]))

    # -- queries ----------------------------------------------------------
    def find(self, day: str, t: int, on: date | None, min_free: int = 0, until: int | None = None,
             building: str | None = None, floor=None) -> list[FreeRoom]:
        """Rooms with no class at `t` (and through `until`, or for `min_free` minutes)."""
        out = []
        for rid in self.room_ids(building, floor):
            fu = self.free_until(rid, day, t, on)
            if fu is None:
                continue
            if until is not None and fu < until:
                continue
            if fu - t < min_free:
                continue
            r = self.rooms[rid]
            out.append(FreeRoom(rid, r["name"], r["building"], r["floor"], t, fu))
        return out

    def timeline(self, rid: str, day: str, on: date | None) -> list[dict]:
        """Alternating open/occupied blocks for one room-day."""
        s = self.slots(rid, day, on)
        occ = [o for o in self.rooms[rid]["days"][day]["occupied"]
               if on is None or ((not o["start_date"] or date.fromisoformat(o["start_date"]) <= on)
                                 and (not o["end_date"] or on <= date.fromisoformat(o["end_date"])))]
        blocks = []
        i = 0
        while i < len(s):
            j = i
            while j < len(s) and s[j] == s[i]:
                j += 1
            a, b = self.hs + i * self.slot, self.hs + j * self.slot
            blk = {"start": a, "end": b, "state": "occupied" if s[i] == "1" else "open"}
            if s[i] == "1":
                blk["classes"] = [o for o in occ if o["start"] < b and o["end"] > a]
            blocks.append(blk)
            i = j
        return blocks

    def find_room(self, query: str) -> str | None:
        q = " ".join(query.split()).lower()
        for rid, r in self.rooms.items():
            if q in (rid.lower(), r["name"].lower()):
                return rid
        # "North 937", "HN 937", "W217"
        parts = q.split()
        num = parts[-1].upper() if parts else ""
        cands = [rid for rid, r in self.rooms.items() if (r["number"] or "").upper() == num
                 and (len(parts) == 1 or self.building_matches(rid, " ".join(parts[:-1])))]
        return cands[0] if len(cands) == 1 else None

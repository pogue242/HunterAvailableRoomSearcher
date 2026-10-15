"""Turn raw Global Search room strings into (building, floor, room) records.

Rules are derived from the room strings seen in the real data. Anything that
does not match a rule is reported as unparsed rather than guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Strings that mean "no physical room". Counted, but never put in the model.
NON_ROOM_RE = re.compile(r"^(TBA|-|N/?A|)$|^online\b|\bonline$|^web\b|^off[ -]?campus\b", re.I)

# "<building> <room>", e.g. "North Bldg 937", "West Bldg W1729", "East Bldg EB121",
# "ThomHunter 109", "Baker BTB240". The room token must contain a digit.
BLDG_RE = re.compile(r"^(?P<name>[A-Za-z][A-Za-z .'&-]*?)\s+(?P<num>[A-Za-z]*-?\d[A-Za-z0-9.-]*)$")
# Room number after removing the building's own letter prefix:
# digits with an optional trailing letter, e.g. 937, 918B, 1729, 1001C.
NUM_RE = re.compile(r"^(?P<digits>\d{3,4})(?P<suffix>[A-Z]?)(?:-\d)?$")  # 1090B-1 = part of 1090B
# A leading level letter, e.g. "C101" (only accepted if it recurs in the data).
LEVEL_RE = re.compile(r"^(?P<level>[A-Z]{1,2})(?P<digits>\d{2,4})(?P<suffix>[A-Z]?)$")

# Convenience aliases for search (`--building north`, `HN`). Only applied to
# buildings that actually appear in the data; nothing here creates a building.
KNOWN_ALIASES = {
    "North Bldg": ["HN", "Hunter North", "North"],
    "West Bldg": ["HW", "Hunter West", "West"],
    "East Bldg": ["HE", "Hunter East", "East"],
    "ThomHunter": ["TH", "Thomas Hunter", "Thomas Hunter Hall"],
    "Roosevelt": ["Roosevelt House"],
    "Baker": ["Baker Theatre"],
}


@dataclass
class RoomInfo:
    raw: str
    building_name: str | None = None
    building_id: str | None = None
    number: str | None = None     # as printed, e.g. "W217", "937", "C101"
    floor: int | str | None = None
    reason: str = ""              # why it was not fully parsed


def is_non_room(raw: str) -> bool:
    return bool(NON_ROOM_RE.search(raw.strip()))


def building_id(name: str) -> str:
    """'North Bldg' -> 'NORTH'. Deterministic, derived only from the name."""
    return re.sub(r"[^A-Z0-9]+", "", name.replace("Bldg", "").upper()) or name.upper()


def _prefix_of(name: str, numbers: list[str]) -> str:
    """The letter prefix most of this building's rooms share (W for West, EB for East)."""
    from collections import Counter

    c = Counter(m.group(1) for n in numbers if (m := re.match(r"^([A-Z]+)\d", n)))
    if not c:
        return ""
    p, k = c.most_common(1)[0]
    if k == len(numbers) or (k >= 3 and 2 * k >= len(numbers)):
        return p
    # East Bldg: "E402" and "EB121" share "E".
    first = Counter(n[0] for n in numbers if n[0].isalpha())
    if first:
        q, kq = first.most_common(1)[0]
        if kq == len(numbers) or (kq >= 3 and 2 * kq >= len(numbers)):
            return q
    return ""


def parse_rooms(raw_rooms: list[str]) -> tuple[dict[str, RoomInfo], list[RoomInfo]]:
    """Parse a set of physical-room strings.

    Returns ({raw: RoomInfo}, unparsed) where unparsed rooms are also in the dict
    (with floor None and a reason)."""
    out: dict[str, RoomInfo] = {}
    by_bldg: dict[str, list[str]] = {}
    for raw in sorted(set(raw_rooms)):
        m = BLDG_RE.match(raw.strip())
        if not m:
            out[raw] = RoomInfo(raw, reason="does not match '<building> <room number>'")
            continue
        name, num = m["name"], m["num"].upper()
        out[raw] = RoomInfo(raw, name, building_id(name), num)
        by_bldg.setdefault(name, []).append(num)

    prefixes = {name: _prefix_of(name, nums) for name, nums in by_bldg.items()}
    # Level letters (e.g. "C" in North Bldg C101) count only if used by 2+ rooms
    # in the same building, so a one-off typo isn't promoted to a floor.
    level_counts: dict[tuple[str, str], int] = {}
    for name, nums in by_bldg.items():
        for n in nums:
            rest = n[len(prefixes[name]):] if prefixes[name] and n.startswith(prefixes[name]) else n
            lm = LEVEL_RE.match(rest)
            if lm and not NUM_RE.match(rest):
                level_counts[(name, lm["level"])] = level_counts.get((name, lm["level"]), 0) + 1

    for info in out.values():
        if info.number is None:
            continue
        p = prefixes[info.building_name]
        rest = info.number[len(p):] if p and info.number.startswith(p) else info.number
        nm = NUM_RE.match(rest)
        if nm:
            d = nm["digits"]
            info.floor = int(d[:-2])          # 937 -> 9, 1729 -> 17
            continue
        lm = LEVEL_RE.match(rest)
        if lm and level_counts.get((info.building_name, lm["level"]), 0) >= 2:
            info.floor = lm["level"]          # e.g. "C" level, as written in the data
            continue
        info.reason = f"can't infer floor from room number {info.number!r}"
    unparsed = [i for i in out.values() if i.floor is None]
    return out, unparsed


def room_id(info: RoomInfo) -> str:
    if info.building_id and info.number:
        return f"{info.building_id}-{info.number}"
    return "RAW-" + re.sub(r"[^A-Za-z0-9]+", "_", info.raw).strip("_")

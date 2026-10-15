"""Parse CUNY Global Search results pages into Section/Meeting records.

The results HTML never closes its <tr>/<tbody> tags,
so instead of walking rows we walk every <td data-label=...> cell in document
order: a "Class" cell starts a new section, and the course header
(<div class="testing_msg">) seen most recently names the course.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date

DAY_CODES = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
_DAYS_RE = re.compile(r"(Mo|Tu|We|Th|Fr|Sa|Su)")
_PATTERN_RE = re.compile(
    r"^\s*((?:Mo|Tu|We|Th|Fr|Sa|Su)+)\s+(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)\s*$",
    re.I,
)
_DATES_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})\s*-\s*(\d{2})/(\d{2})/(\d{4})")
_COURSE_RE = re.compile(r"([A-Z]{2,6})\s+(\d{3,6}[A-Z]?)\s+-\s+(.*)$")
_SECTION_RE = re.compile(r"^(\S+?)-([A-Z]{2,4})\s*(.*)$")


def parse_time(text: str) -> int:
    """'9:30AM' -> 570 minutes after midnight. 12:xxAM is 0:xx, 12:xxPM is 12:xx."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]?\.?\s*", text)
    if not m:
        raise ValueError(f"bad time {text!r}")
    h, mi, ap = int(m[1]), int(m[2]), m[3].upper()
    if not (1 <= h <= 12 and 0 <= mi < 60):
        raise ValueError(f"bad time {text!r}")
    return (h % 12 + (12 if ap == "P" else 0)) * 60 + mi


def fmt_time(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    ap = "AM" if h < 12 or h == 24 else "PM"
    return f"{(h % 12) or 12}:{m:02d}{ap}"


@dataclass
class Meeting:
    days: list[str]          # e.g. ["Mo", "We"]; empty when TBA
    start: int | None        # minutes after midnight; None when TBA
    end: int | None
    room: str                # raw room string, e.g. "North Bldg 937"
    start_date: str | None   # ISO date
    end_date: str | None
    raw: str = ""            # raw DaysAndTimes line

    def active_on(self, d: date) -> bool:
        if self.start_date and d < date.fromisoformat(self.start_date):
            return False
        if self.end_date and d > date.fromisoformat(self.end_date):
            return False
        return True


@dataclass
class Section:
    class_number: str
    course: str              # "UKR 26001"
    title: str
    section: str             # "01"
    component: str           # "LEC"
    session: str             # "Regular"
    instructors: list[str]
    mode: str                # "In Person", ...
    status: str              # "Open" / "Closed" / ...
    topic: str
    meetings: list[Meeting] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Section":
        d = dict(d)
        d["meetings"] = [Meeting(**m) for m in d.get("meetings", [])]
        return cls(**d)


def _lines(td) -> list[str]:
    """Split a cell on <br>, KEEPING empty entries: "North Bldg 1311<br>" means
    the second meeting has no room, and dropping it would shift the pairing.
    Trailing empties are kept only when another line exists (a fully blank
    cell is [])."""
    from bs4 import Comment

    parts, cur = [], []
    for node in td.descendants:
        if getattr(node, "name", None) == "br":
            parts.append("".join(cur))
            cur = []
        elif isinstance(node, str) and not isinstance(node, Comment):
            cur.append(str(node))
    parts.append("".join(cur))
    parts = [re.sub(r"\s+", " ", p.replace("\xa0", " ")).strip() for p in parts]
    return parts if any(parts) else []


def parse_pattern(text: str) -> tuple[list[str], int | None, int | None]:
    """'MoWe 9:00AM - 9:50AM' -> (['Mo','We'], 540, 590). 'TBA' -> ([], None, None)."""
    m = _PATTERN_RE.match(text)
    if not m:
        return [], None, None
    return _DAYS_RE.findall(m[1]), parse_time(m[2]), parse_time(m[3])


def parse_dates(text: str) -> tuple[str | None, str | None]:
    m = _DATES_RE.search(text or "")
    if not m:
        return None, None
    mo1, d1, y1, mo2, d2, y2 = m.groups()
    return f"{y1}-{mo1}-{d1}", f"{y2}-{mo2}-{d2}"


def _build_meetings(cells: dict[str, list[str]], notes: list[str]) -> list[Meeting]:
    times = cells.get("DaysAndTimes", [])
    rooms = cells.get("Room", [])
    dates = cells.get("Meeting Dates", [])
    n = max(len(times), len(rooms), len(dates))
    # A column that is entirely blank (e.g. no rooms assigned) isn't a mismatch.
    if n and len({len(c) for c in (times, rooms, dates) if c}) > 1:
        notes.append(f"line count mismatch: times={len(times)} rooms={len(rooms)} dates={len(dates)}")

    def pick(lst: list[str], i: int, reuse_single: bool = True) -> str:
        # Pair by line index. A column with a single line applies to every
        # meeting; otherwise a missing line is unknown (""), never borrowed
        # from a neighbour (that would invent a room booking).
        if i < len(lst):
            return lst[i]
        return lst[0] if (reuse_single and len(lst) == 1) else ""

    out: list[Meeting] = []
    seen = set()
    for i in range(n):
        raw = pick(times, i)
        days, start, end = parse_pattern(raw)
        note = f"unparsed meeting pattern: {raw!r}"
        if raw and start is None and raw.upper() not in ("TBA", "-") and note not in notes:
            notes.append(note)
        sd, ed = parse_dates(pick(dates, i))
        key = (tuple(days), start, end, pick(rooms, i), sd, ed)
        if key in seen:  # lines are repeated, e.g. once per instructor
            continue
        seen.add(key)
        out.append(Meeting(days, start, end, pick(rooms, i), sd, ed, raw))
    return out


def parse_results(html: str) -> list[Section]:
    """Parse one Global Search results page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    sections: list[Section] = []
    course, title = "", ""
    owner = ("", "")
    cells: dict[str, list[str]] | None = None
    status = ""

    def flush():
        if cells is None or not cells.get("Class"):
            return
        notes: list[str] = []
        sec_raw = " ".join(cells.get("Section", []))
        m = _SECTION_RE.match(sec_raw)
        sec, comp, sess = (m.group(1), m.group(2), m.group(3).strip()) if m else (sec_raw, "", "")
        sections.append(Section(
            class_number=cells["Class"][0],
            course=owner[0], title=owner[1], section=sec, component=comp, session=sess,
            instructors=[x for x in dict.fromkeys(cells.get("Instructor", [])) if x],
            mode=(cells.get("Instruction Mode") or [""])[0],
            status=status,
            topic=" ".join(cells.get("Course Topic", [])),
            meetings=_build_meetings(cells, notes),
            notes=notes,
        ))

    for el in soup.find_all(["div", "td"]):
        if el.name == "div" and "testing_msg" in (el.get("class") or []):
            text = re.sub(r"\s+", " ", el.get_text(" ").replace("\xa0", " ")).strip()
            m = _COURSE_RE.search(text)
            if m and "class section(s) found" not in text:
                course, title = f"{m[1]} {m[2]}", m[3].strip()
            continue
        if el.name != "td" or not el.has_attr("data-label"):
            continue
        label = el["data-label"].strip()
        if label == "Class":
            flush()
            # Remember the course NOW: flush() runs only when the next section
            # starts, by which time a new course header may have been seen.
            cells, status, owner = {}, "", (course, title)
        if cells is None:
            continue
        if label == "Status":
            img = el.find("img")
            # The alt text is always "Open"; the title is the real status.
            status = (img.get("title", "") if img else "").strip() or " ".join(_lines(el))
        cells[label] = _lines(el)
    flush()
    return sections


def page_kind(html: str) -> str:
    """Classify a controller response: results / noresults / exceed / criteria / oops / other."""
    if "class section(s) found" in html:
        return "results"
    if "returns no results" in html:
        return "noresults"
    if "exceed the maximum limit" in html:
        return "exceed"
    if "Oops" in html and "error has been encountered" in html:
        return "oops"
    if 'name="class_search_form"' in html:
        return "criteria"
    if 'name="searchform"' in html:
        return "institution"
    return "other"


def parse_found_count(html: str) -> int | None:
    m = re.search(r"(\d+) class section\(s\) found", html)
    return int(m.group(1)) if m else None


def parse_subjects(criteria_html: str) -> list[tuple[str, str]]:
    """Subject (code, label) pairs from the criteria form."""
    from bs4 import BeautifulSoup

    sel = BeautifulSoup(criteria_html, "html.parser").find("select", attrs={"name": "subject_name"})
    if not sel:
        return []
    out = []
    for o in sel.find_all("option"):
        v = (o.get("value") or "").strip()
        if v:
            # The first, empty-valued <option> is unclosed and swallows the
            # others' text, so take only this option's own first line.
            out.append((v, o.get_text("\n").strip().split("\n")[0].strip()))
    return out


def parse_terms(form_html: str) -> list[tuple[str, str]]:
    from bs4 import BeautifulSoup

    sel = BeautifulSoup(form_html, "html.parser").find("select", attrs={"name": "term_value"})
    if not sel:
        return []
    return [(o["value"], o.get_text("\n").strip().split("\n")[0].strip())
            for o in sel.find_all("option") if o.get("value")]

"""Polite, cached, resumable sweep of CUNY Global Search for one Hunter term.

Request flow:
  GET controller -> POST institution+term -> POST search (subject + career, ...)
Searches that hit the server's result cap are split by instruction mode, then
by meeting-start-time windows. Every good response is cached under
cache/<term_code>/, so a re-run only fetches what is missing.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .parse import page_kind, parse_found_count, parse_subjects, fmt_time

log = logging.getLogger(__name__)

CONTROLLER = "https://globalsearch.cuny.edu/CFGlobalSearchTool/CFSearchToolController"
HUNTER_NAME = "Hunter College"
HUNTER_CODE = "HTR01"
TERM_OFFSETS = {"Spring": 2, "Summer": 6, "Fall": 9}
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) HunterRoomFinder/1.0 "
              "(student project; ~1 request/sec; public data only)")
CAREERS = {"UGRD": "Undergraduate", "GRAD": "Graduate", "DOCT": "Doctoral"}
MODES = {"P": "In Person", "HF": "HyField", "HX": "HyFlex", "HA": "Hybrid Asynchronous",
         "HS": "Hybrid Synchronous", "OA": "Online Asynchronous", "OM": "Online Mix",
         "OS": "Online Synchronous"}
DAY_MINUTES = 24 * 60
MIN_WINDOW = 30


def term_code(term: str) -> str:
    """'2026 Fall' -> '1269'."""
    year, season = term.split()[:2]
    season = season.capitalize()
    if season not in TERM_OFFSETS:
        raise ValueError(f"term must look like '2026 Fall', got {term!r}")
    return str((int(year) - 1900) * 10 + TERM_OFFSETS[season])


def term_label(term: str) -> str:
    year, season = term.split()[:2]
    return f"{year} {season.capitalize()} Term"


@dataclass(frozen=True)
class Query:
    subject: str
    subject_label: str
    career: str
    mode: str | None = None
    lo: int = 0                       # start-time window, minutes, inclusive
    hi: int = DAY_MINUTES - 1

    @property
    def key(self) -> str:
        parts = [self.subject, self.career, self.mode or "all"]
        if (self.lo, self.hi) != (0, DAY_MINUTES - 1):
            parts.append(f"t{self.lo:04d}-{self.hi:04d}")
        return "_".join(parts)

    def form(self) -> list[tuple[str, str]]:
        f = {
            "selectedSubjectName": self.subject_label, "subject_name": self.subject,
            "selectedCCareerName": CAREERS[self.career], "courseCareer": self.career,
            "selectedCAttrName": "", "courseAttr": "", "selectedCAttrVName": "", "courseAttValue": "",
            "selectedReqDName": "", "reqDesignation": "", "selectedSessionName": "", "class_session": "",
            "selectedModeInsName": f"{MODES[self.mode]}, " if self.mode else "",
            "meetingStart": "LT", "selectedMeetingStartName": "less than",
            "meetingStartText": "", "AndMeetingStartText": "",
            "meetingEnd": "LE", "selectedMeetingEndName": "less than or equal to",
            "meetingEndText": "", "AndMeetingEndText": "",
            "daysOfWeek": "I", "selectedDaysOfWeekName": "include only these days",
            "instructor": "B", "selectedInstructorName": "begins with", "instructorName": "",
        }
        full = (self.lo, self.hi) == (0, DAY_MINUTES - 1)
        if not full:
            t = lambda m: fmt_time(m).replace("AM", " AM").replace("PM", " PM")
            if self.lo == 0:
                f.update(meetingStart="LT", selectedMeetingStartName="less than",
                         meetingStartText=t(self.hi + 1))
            elif self.hi == DAY_MINUTES - 1:
                f.update(meetingStart="GE", selectedMeetingStartName="greater than or equal to",
                         meetingStartText=t(self.lo))
            else:
                f.update(meetingStart="BT", selectedMeetingStartName="between",
                         meetingStartText=t(self.lo), AndMeetingStartText=t(self.hi))
        items = list(f.items())
        if self.mode:
            items.append(("ModeOfIns", self.mode))
        items.append(("search_btn_search", "Search"))
        return items

    def split(self) -> list["Query"]:
        if self.mode is None:
            return [replace(self, mode=m) for m in MODES]
        if self.hi - self.lo + 1 <= MIN_WINDOW:
            return []
        lo, hi = self.lo, self.hi
        if (lo, hi) == (0, DAY_MINUTES - 1):
            mid = 12 * 60
        else:
            mid = lo + ((hi - lo + 1) // 2 // 30) * 30 or lo + (hi - lo + 1) // 2
        return [replace(self, lo=lo, hi=mid - 1), replace(self, lo=mid, hi=hi)]


class GlobalSearch:
    def __init__(self, term: str = "2026 Fall", cache_dir: str | Path = "cache",
                 delay: float = 1.0, offline: bool = False):
        self.term = term
        self.code = term_code(term)
        self.dir = Path(cache_dir) / self.code
        self.dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.offline = offline
        self._session = None
        self._last = 0.0
        self.requests_made = 0
        self.manifest_path = self.dir / "manifest.json"
        self.manifest: dict = (json.loads(self.manifest_path.read_text(encoding="utf-8"))
                               if self.manifest_path.exists() else {})

    # -- networking -------------------------------------------------------
    def _wait(self):
        dt = time.monotonic() - self._last
        if dt < self.delay:
            time.sleep(self.delay - dt)

    def _call(self, method: str, **kw) -> str:
        import requests

        for attempt in range(5):
            self._wait()
            try:
                r = self._session.request(method, CONTROLLER, timeout=120, **kw)
                self._last = time.monotonic()
                self.requests_made += 1
                if r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r.text
            except requests.RequestException as e:
                self._last = time.monotonic()
                wait = min(60, 2 ** (attempt + 1))
                log.warning("request failed (%s); retrying in %ss", e, wait)
                time.sleep(wait)
        raise RuntimeError("Global Search unreachable after 5 attempts")

    def _open_session(self) -> str:
        import requests

        if self.offline:
            raise RuntimeError(f"offline mode and not cached in {self.dir}")
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._call("GET")
        html = self._call("POST", data={
            "selectedInstName": f"{HUNTER_NAME} |", "inst_selection": HUNTER_CODE,
            "selectedTermName": term_label(self.term), "term_value": self.code, "next_btn": "Next",
        })
        if page_kind(html) != "criteria":
            raise RuntimeError(f"unexpected page after choosing institution/term: {page_kind(html)}")
        (self.dir / "criteria.html").write_text(html, encoding="utf-8")
        return html

    # -- public API -------------------------------------------------------
    def subjects(self) -> list[tuple[str, str]]:
        p = self.dir / "criteria.html"
        html = p.read_text(encoding="utf-8") if p.exists() else self._open_session()
        return parse_subjects(html)

    def search(self, q: Query) -> tuple[str, str]:
        """Return (kind, html), from cache when possible."""
        path = self.dir / f"{q.key}.html"
        if path.exists():
            html = path.read_text(encoding="utf-8")
            return page_kind(html), html
        for attempt in range(3):
            if self._session is None:
                self._open_session()
            html = self._call("POST", data=q.form())
            kind = page_kind(html)
            if kind in ("results", "noresults", "exceed"):
                path.write_text(html, encoding="utf-8")
                return kind, html
            log.warning("%s: got %s page; restarting session", q.key, kind)
            self._session = None
        return kind, html

    def sweep(self, subjects: list[str] | None = None) -> dict:
        """Search every subject x career, splitting capped searches. Returns the manifest."""
        all_subjects = self.subjects()
        if subjects:
            want = {s.upper() for s in subjects}
            all_subjects = [s for s in all_subjects if s[0] in want]
        log.info("Sweeping %d subjects for Hunter %s (term %s)", len(all_subjects), self.term, self.code)
        for i, (code, label) in enumerate(all_subjects, 1):
            todo = [Query(code, label, c) for c in CAREERS]
            while todo:
                q = todo.pop(0)
                kind, html = self.search(q)
                entry = {"kind": kind, "count": parse_found_count(html)}
                if kind == "exceed":
                    kids = q.split()
                    if not kids:
                        entry["kind"] = "exceed-unsplittable"
                        log.error("%s still over the cap at the smallest window", q.key)
                    todo = kids + todo
                elif kind not in ("results", "noresults"):
                    log.error("%s: giving up (%s)", q.key, kind)
                self.manifest[q.key] = entry
            n = sum(v["count"] or 0 for k, v in self.manifest.items()
                    if k.startswith(code + "_") and v["kind"] == "results")
            log.info("[%d/%d] %s %s: %d rows (requests so far: %d)",
                     i, len(all_subjects), code, label, n, self.requests_made)
            self.save_manifest()
        return self.manifest

    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest, indent=1, sort_keys=True), encoding="utf-8")

    def result_pages(self) -> list[Path]:
        """Cached result pages that are leaves (actual results)."""
        return sorted(self.dir / f"{k}.html" for k, v in self.manifest.items() if v["kind"] == "results")

    def time_split_keys(self) -> list[str]:
        return [k for k in self.manifest if re.search(r"_t\d{4}-\d{4}$", k)]

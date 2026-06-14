"""
racing_filter.py — accept ONLY UK/Ireland horse racing tips.

Drop-in gate for the tipstrr ingestion scraper. Decides, for a single tip leg,
whether it is a horse race at a Great Britain or Ireland racecourse. Everything
else — football and other team sports, plus overseas racing (AUS/FR/ARG/USA/etc.)
— is rejected.

It also parses tipstrr `fixtureReference` strings correctly, which fixes the
existing bug where the off-time was being stored in the `course` column and the
day-of-month in `race_time`.

No third-party dependencies (stdlib only).

fixtureReference formats seen in the wild:
    horse racing : "2026-06-09-1735-southwell-18f11"   = date-HHMM-course-hash
    team sport   : "2026-06-08-gimnasia-vs-godoy-cruz-5400f" = date-teamA-vs-teamB-hash
                   (no HHMM time token, contains "-vs-")

Course whitelist is derived from the Racing API's own GB/IRE course names
(ra_results, regions GB + IRE), so spellings match the results feed exactly.
"""

import re

# ── Canonical GB + IRE racecourses (source: ra_results, regions GB/IRE) ──────
# Display names kept for readability; matching is done on a normalised key.
GB_COURSES = [
    "Aintree", "Ascot", "Ayr", "Bangor-on-Dee", "Bath", "Beverley", "Brighton",
    "Carlisle", "Cartmel", "Catterick", "Chelmsford", "Cheltenham", "Chepstow",
    "Chester", "Doncaster", "Epsom", "Exeter", "Fakenham", "Ffos Las", "Fontwell",
    "Goodwood", "Hamilton", "Haydock", "Hereford", "Hexham", "Huntingdon", "Kelso",
    "Kempton", "Leicester", "Lingfield", "Ludlow", "Market Rasen", "Musselburgh",
    "Newbury", "Newcastle", "Newmarket", "Newton Abbot", "Nottingham", "Perth",
    "Plumpton", "Pontefract", "Redcar", "Ripon", "Salisbury", "Sandown",
    "Sedgefield", "Southwell", "Stratford", "Taunton", "Thirsk", "Uttoxeter",
    "Warwick", "Wetherby", "Wincanton", "Windsor", "Wolverhampton", "Worcester",
    "Yarmouth", "York",
]
IRE_COURSES = [
    "Ballinrobe", "Bellewstown", "Clonmel", "Cork", "Curragh", "Down Royal",
    "Downpatrick", "Dundalk", "Fairyhouse", "Galway", "Gowran Park", "Kilbeggan",
    "Killarney", "Laytown", "Leopardstown", "Limerick", "Listowel", "Naas",
    "Navan", "Punchestown", "Roscommon", "Sligo", "Thurles", "Tipperary",
    "Tramore", "Wexford",
]

# A few aliases the tipstrr slug may use that differ from the feed's display name.
COURSE_ALIASES = {
    "greatyarmouth": "yarmouth",   # tipstrr sometimes slugs Yarmouth as "great-yarmouth"
}


def normalise_course(name: str) -> str:
    """Lowercase, drop (AW)/(IRE)/(July) suffixes, strip all non-alphanumerics.

    So "Bangor-on-Dee", "bangor-on-dee" and "Chelmsford (AW)" all reduce to a
    stable key that compares equal across the feed names and tipstrr slugs.
    """
    if not name:
        return ""
    s = name.lower()
    s = re.sub(r"\((?:aw|ire|july)\)", " ", s)   # remove known parenthetical tags
    s = re.sub(r"[^a-z0-9]", "", s)               # drop spaces, hyphens, parens
    return COURSE_ALIASES.get(s, s)


GB_KEYS = {normalise_course(c) for c in GB_COURSES}
IRE_KEYS = {normalise_course(c) for c in IRE_COURSES}
ALL_KEYS = GB_KEYS | IRE_KEYS


# Anchors the trailing hash so the course can contain hyphens (e.g. ffos-las).
_RACING_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{4})-(.+)-([0-9a-z]+)$", re.IGNORECASE)


def parse_fixture(fixture_reference: str):
    """Parse a tipstrr fixtureReference.

    Returns a dict:
      {"kind": "racing"|"team_sport"|"unknown",
       "date": "YYYY-MM-DD" | None,
       "time": "HH:MM" | None,
       "course_slug": "southwell" | None,
       "course_norm": "southwell" | None}
    """
    out = {"kind": "unknown", "date": None, "time": None,
           "course_slug": None, "course_norm": None}
    if not fixture_reference:
        return out

    ref = fixture_reference.strip().lower()

    # Team sports use "teamA-vs-teamB" and carry no HHMM off-time.
    if "-vs-" in ref:
        out["kind"] = "team_sport"
        # still capture the date if present
        m = re.match(r"^(\d{4}-\d{2}-\d{2})-", ref)
        if m:
            out["date"] = m.group(1)
        return out

    m = _RACING_RE.match(ref)
    if not m:
        return out  # unknown shape

    date_s, hhmm, course_slug, _hash = m.groups()
    out["kind"] = "racing"
    out["date"] = date_s
    out["time"] = f"{hhmm[:2]}:{hhmm[2:]}"
    out["course_slug"] = course_slug
    out["course_norm"] = normalise_course(course_slug)
    return out


def region_for(course_norm: str):
    """Return 'GB', 'IRE' or None for a normalised course key."""
    if course_norm in GB_KEYS:
        return "GB"
    if course_norm in IRE_KEYS:
        return "IRE"
    return None


def is_uk_irish_horse_racing(fixture_reference=None, course=None):
    """Decide whether a tip leg is GB/IRE horse racing.

    Provide `fixture_reference` (preferred — full tipstrr ref) and/or a raw
    `course` string as a fallback.

    Returns (accept: bool, info: dict) where info contains:
      reason       — 'gb_ire_racing' | 'team_sport' | 'overseas_racing'
                     | 'unknown_course' | 'not_a_fixture'
      region       — 'GB' | 'IRE' | None
      date/time/course/course_norm — parsed values for correct storage
    """
    info = {"reason": "not_a_fixture", "region": None, "date": None,
            "time": None, "course": None, "course_norm": None}

    parsed = parse_fixture(fixture_reference) if fixture_reference else {"kind": "unknown"}

    if parsed.get("kind") == "team_sport":
        info["reason"] = "team_sport"
        info["date"] = parsed.get("date")
        return False, info

    if parsed.get("kind") == "racing":
        info["date"] = parsed["date"]
        info["time"] = parsed["time"]
        info["course"] = parsed["course_slug"]
        info["course_norm"] = parsed["course_norm"]
        region = region_for(parsed["course_norm"])
        if region:
            info["reason"] = "gb_ire_racing"
            info["region"] = region
            return True, info
        info["reason"] = "overseas_racing"
        return False, info

    # No usable fixtureReference — fall back to a bare course string if given.
    if course:
        key = normalise_course(course)
        info["course"] = course
        info["course_norm"] = key
        region = region_for(key)
        if region:
            info["reason"] = "gb_ire_racing"
            info["region"] = region
            return True, info
        info["reason"] = "unknown_course"
        return False, info

    return False, info


if __name__ == "__main__":
    # Quick manual demo
    samples = [
        "2026-06-09-1735-southwell-18f11",
        "2026-06-08-0645-canterbury-3ad86",
        "2026-06-08-1318-angers-ab2e7",
        "2026-06-08-gimnasia-vs-godoy-cruz-5400f",
        "2026-06-10-1420-ffos-las-9a1c2",
    ]
    for s in samples:
        ok, info = is_uk_irish_horse_racing(fixture_reference=s)
        print(f"{'ACCEPT' if ok else 'reject'}  {info['reason']:16} {info.get('region') or '':3}  {s}")

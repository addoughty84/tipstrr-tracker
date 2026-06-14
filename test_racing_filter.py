"""
Tests for racing_filter — uses the real fixtureReferences pulled from
tip_legs on 2026-06-08 (the day we investigated).

Run:  python test_racing_filter.py
"""

from racing_filter import is_uk_irish_horse_racing, parse_fixture, normalise_course

# (fixtureReference, expected_accept, expected_reason)
CASES = [
    # --- GB/IRE racing: ACCEPT ---
    ("2026-06-09-1735-southwell-18f11", True, "gb_ire_racing"),
    ("2026-06-08-1420-ascot-aa111", True, "gb_ire_racing"),
    ("2026-06-08-1610-ayr-bb222", True, "gb_ire_racing"),
    ("2026-06-10-1420-ffos-las-9a1c2", True, "gb_ire_racing"),      # multi-word course
    ("2026-06-10-1500-bangor-on-dee-7f0aa", True, "gb_ire_racing"), # hyphenated course
    ("2026-06-08-1930-dundalk-cc333", True, "gb_ire_racing"),       # IRE (AW)
    ("2026-06-08-1500-leopardstown-dd444", True, "gb_ire_racing"),  # IRE
    ("2026-06-08-1400-great-yarmouth-ee555", True, "gb_ire_racing"),# alias -> yarmouth

    # --- Overseas racing: REJECT ---
    ("2026-06-08-0645-canterbury-3ad86", False, "overseas_racing"),   # AUS
    ("2026-06-08-0525-strathalbyn-f4c61", False, "overseas_racing"),  # AUS
    ("2026-06-08-1318-angers-ab2e7", False, "overseas_racing"),       # FR
    ("2026-06-08-1824-toulouse-35909", False, "overseas_racing"),     # FR
    ("2026-06-08-1715-palermo-5dd91", False, "overseas_racing"),      # ARG

    # --- Team sports: REJECT ---
    ("2026-06-08-gimnasia-vs-godoy-cruz-5400f", False, "team_sport"),
    ("2026-06-08-union-santa-fe-vs-atletico-tucuman-dbc18", False, "team_sport"),
    ("2026-06-08-liverpool-uru-vs-cerro-largo-42986", False, "team_sport"),

    # --- Junk / missing: REJECT ---
    ("", False, "not_a_fixture"),
    (None, False, "not_a_fixture"),
]


def run():
    passed = failed = 0
    for ref, exp_ok, exp_reason in CASES:
        ok, info = is_uk_irish_horse_racing(fixture_reference=ref)
        good = (ok == exp_ok) and (info["reason"] == exp_reason)
        if good:
            passed += 1
        else:
            failed += 1
            print(f"FAIL  {ref!r}\n      got accept={ok} reason={info['reason']}"
                  f"  expected accept={exp_ok} reason={exp_reason}")
    # parse correctness: time/course extracted properly (the old bug)
    p = parse_fixture("2026-06-08-0645-canterbury-3ad86")
    assert p["time"] == "06:45" and p["course_slug"] == "canterbury", p
    # fallback on bare course string
    ok, info = is_uk_irish_horse_racing(course="Chelmsford (AW)")
    assert ok and info["region"] == "GB", info
    ok, info = is_uk_irish_horse_racing(course="Sha Tin")
    assert not ok and info["reason"] == "unknown_course", info

    print(f"\n{passed} passed, {failed} failed (+ 3 parse/fallback asserts ok)")
    return failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)

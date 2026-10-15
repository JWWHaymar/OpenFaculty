#!/usr/bin/env python3
"""
grants.py - Newly funded labs as a LEADING INDICATOR of hiring.

WHY THIS EXISTS
---------------
Searching Yale and Texas A&M for "phage" or "bacteriophage" returns ZERO open
roles on a typical day. Better scraping cannot invent jobs that do not exist
yet. But an academic lab takes on a student or a Research Associate *because it
just got funded*, and the posting appears weeks later - often only on the
university's own HR site, or never, because the PI fills the seat from their
network first.

NIH RePORTER publishes new awards as structured JSON, unauthenticated. A NEW
award (`award_types: ["1"]`, i.e. a Type-1 new project, not a renewal) to a lab
doing your exact science is the earliest public signal that a bench seat is
about to open. Measured live 2026-08-19: 79 new FY2026 awards mention
bacteriophage.

WHAT THIS IS NOT
----------------
This does NOT produce applications, and it must never email anyone. **The tool
prepares; you send.** A grant hit is a LEAD - a named PI at a named institution
doing named work - and the next step is yours.

HONESTY ABOUT NOISE
-------------------
A funded lab is not a posted job. Expect most leads to convert to nothing, and
some PIs will be theorists or clinicians with no wet-lab seat. The value is not
precision, it is EARLINESS and the fact that these labs are unreachable by any
job-board scraper. Leads are therefore kept in their own table, and a lead is
never presented as a vacancy.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request

from openfaculty import store
# NIH RePORTER is a public API that WANTS to know who is calling it. Spoofing a
# browser at it buys nothing and would make the politeness claim in the README
# false, so this is the descriptive agent, shared from `config` so the identity
# cannot drift between modules.
from openfaculty.config import POLITE_AGENT as USER_AGENT

API = "https://api.reporter.nih.gov/v2/projects/search"

# Topics for the field this was built for. Each becomes its own query so the
# matched topic is recorded, rather than collapsing everything into one blob.
#
# THREE MEASURED LESSONS ARE ENCODED HERE.
#
# 1. `phage` beats `bacteriophage`. FY2026: bacteriophage 278 projects, phage
#    456. Labs write "phage biology", "jumbophage", "phage defense". The
#    instinct that a bare `phage` keyword would be too strict was wrong: it is
#    the term the field actually writes.
#
# 2. **A hyphen is an OR.** RePORTER tokenises `anti-phage` into `anti` and
#    `phage` and returns 11,429 FY2026 projects, including HIV and chemical
#    library work. A multi-word concept must be QUOTED: `"phage defense"`
#    returns 16. An unquoted hyphenated term is not a narrow search, it is
#    almost no search at all.
#
# 3. Query the concept, not the jargon. `CBASS` matches ONE project in all of
#    FY2026 while the field it names is large; the papers say "cGAS-like",
#    "bacterial immune", "defense system".
TOPICS = [
    "phage",
    "bacteriophage",
    "\"phage therapy\"",
    "prophage",
    "lysogeny",
    "\"phage defense\"",
    "\"phage resistance\"",
    "\"bacterial immune\"",
    "\"defense system\" bacteria",
    "\"restriction modification\"",
    "\"abortive infection\"",
    "endolysin",
    "\"Staphylococcus aureus\" genetics",
    "\"antimicrobial resistance\" bacterial genetics",
    # Kranzusch's cGAS-like / bacterial-immunity work uses none of the words
    # above. The field calls itself several things and the query must too.
    "\"immune system of bacteria\"",
    "\"cGAS-like\" bacteria",
    "\"anti-viral\" bacteria defense",
]

# Award types to collect. `["1"]` -- new awards only -- was the original filter,
# on the theory that a NEW award is the earliest signal of a bench seat opening.
# Measured: it discards 199 of 278 FY2026 bacteriophage projects, and with them
# every established phage lab in the country. Bondy-Denomy (UCSF, two active
# phage grants), Salgado-Pabon (Wisconsin, "ϕSa3 prophage as a molecular
# regulatory switch"), Hatfull, Marraffini and Laub
# are ALL type-5 continuations and were all invisible.
#
# A continuing R01 is a lab that has been doing this for years and will keep
# hiring. `None` means every type; `is_new` is recorded per lead so the
# earliest-signal question can still be asked.
AWARD_TYPES: list[str] | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS grant_leads (
  project_num   TEXT PRIMARY KEY,
  pi_name       TEXT,
  org_name      TEXT,
  org_city      TEXT,
  org_state     TEXT,
  dept_type     TEXT,
  title         TEXT,
  amount        REAL,
  start_date    TEXT,
  fiscal_year   INTEGER,
  topic         TEXT,
  first_seen    TEXT NOT NULL,
  status        TEXT DEFAULT 'new'   -- new|checked|contacted|dismissed
);
CREATE INDEX IF NOT EXISTS idx_lead_state ON grant_leads(org_state);
"""

# The abstract is the only place the science actually is. A title says
# "Heteroresistance Interdisciplinary Research Unit"; the abstract says which
# organisms, which methods, and whether anyone touches a bench. Stored so a
# lead can be re-read when the vocabulary improves, without re-querying NIH.
MIGRATIONS = (
    "ALTER TABLE grant_leads ADD COLUMN abstract TEXT",
    "ALTER TABLE grant_leads ADD COLUMN terms TEXT",
    # Type 1 is a new project; type 5 a non-competing continuation. The filter
    # that once discarded everything but type 1 is gone, but the distinction is
    # real -- a new award is the earlier hiring signal -- so it is recorded and
    # reported instead of being enforced.
    "ALTER TABLE grant_leads ADD COLUMN award_type TEXT",
)


def _migrate(conn) -> None:
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except Exception:
            pass          # column already present; ALTER has no IF NOT EXISTS


def _post(payload: dict) -> dict:
    """POST to RePORTER and return the parsed body."""
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# `5R01GM149834-04` and `1R01GM149834-01` are the same award in different years.
# Once the fiscal-year window widened past one year, Marraffini's single R01
# appeared four times and filled the top of the report. The stable identity is
# the core number: strip the leading application type and the trailing support
# year.
_CORE_RE = re.compile(r"^\d?([A-Z]\d{2}[A-Z]{2}\d+)(?:-\d+)?", re.I)


def core_num(project_num: str) -> str:
    """`5R01GM149834-04` -> `R01GM149834`. Falls back to the input unchanged."""
    m = _CORE_RE.match((project_num or "").strip())
    return m.group(1).upper() if m else (project_num or "").strip().upper()


def fetch_topic(topic: str, years: list[int], limit: int = 500,
                max_pages: int = 8) -> list[dict]:
    """Every award whose title/abstract/terms match a topic. Paginated.

    One page was the original behaviour and it silently truncated: `phage`
    across FY2024-2026 matches **1,762** projects, of which a single 500-row
    page returns 28%. Michael Laub's anti-phage systems grant sits outside that
    first page, so the field looked smaller than it is -- the same shape as
    every other silent-truncation bug in this project.

    Pages until the API stops returning rows or `max_pages` is hit, and the
    caller is told when a cap was reached rather than left to assume the answer
    was complete.
    """
    out: list[dict] = []
    seen: set[str] = set()
    base = {
        "criteria": {
            "fiscal_years": years,
            "advanced_text_search": {
                "operator": "and",
                "search_field": "projecttitle,abstracttext,terms",
                "search_text": topic,
            },
        },
        "include_fields": ["ProjectNum", "ContactPiName", "Organization",
                           "ProjectTitle", "AwardAmount", "ProjectStartDate",
                           "FiscalYear", "AbstractText", "Terms", "AwardType"],
    }
    if AWARD_TYPES:
        base["criteria"]["award_types"] = AWARD_TYPES

    total = None
    pages_read = 0
    for page in range(max_pages):
        payload = dict(base, limit=limit, offset=page * limit)
        # RePORTER returns HTTP 400 -- not an empty page -- when `offset`
        # exceeds the result count. Asking for one page too many therefore
        # raised, and the caller's `except` discarded the ENTIRE topic
        # including the pages that had already come back: `phage` reported
        # "FAIL HTTPError" and contributed nothing, having successfully
        # fetched thousands of records. Stop before asking for a page that
        # cannot exist, and never throw away rows already in hand.
        if total is not None and page * limit >= total:
            break
        try:
            data = _post(payload)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                json.JSONDecodeError) as exc:
            print(f"    NOTE: {topic!r} page {page + 1} failed ({exc}); "
                  f"keeping {len(out)} record(s) already fetched")
            break
        if total is None:
            total = (data.get("meta") or {}).get("total")
        pages_read = page + 1
        rows = data.get("results") or []
        if not rows:
            break
        for r in rows:
            num = r.get("project_num")
            if num and num not in seen:
                seen.add(num)
                out.append(r)
        if len(out) >= (total or 0):
            break
        time.sleep(0.34)          # RePORTER asks for <= 3 requests/second

    # `fetched < matched` has two very different causes and the note must not
    # blur them. If every page was requested, the shortfall is this function's
    # own de-duplication -- RePORTER returns one project once per fiscal year
    # and `seen` collapses them -- which is correct and not a coverage gap.
    # Only an unread page is truncation.
    if total and pages_read * limit < total:
        print(f"    NOTE: {topic!r} matched {total}, fetched {len(out)} "
              f"— TRUNCATED, raise --max-pages")
    elif total and len(out) < total:
        print(f"    {topic!r}: {total} records collapsed to {len(out)} awards "
              f"({total - len(out)} were the same project in another year)")
    return out


def _row(r: dict, topic: str) -> dict:
    org = r.get("organization") or {}
    return {
        "project_num": r.get("project_num"),
        "pi_name": (r.get("contact_pi_name") or "").strip(),
        "org_name": org.get("org_name") or "",
        "org_city": (org.get("org_city") or "").title(),
        "org_state": org.get("org_state") or "",
        "dept_type": org.get("dept_type") or "",
        "title": r.get("project_title") or "",
        "amount": r.get("award_amount"),
        "start_date": (r.get("project_start_date") or "")[:10],
        "fiscal_year": r.get("fiscal_year"),
        "topic": topic,
        "abstract": (r.get("abstract_text") or "").strip(),
        "terms": (r.get("terms") or "").strip(),
        "award_type": str(r.get("award_type") or ""),
    }


def collect(years: list[int], verbose: bool = True,
            max_pages: int = 8) -> tuple[int, int]:
    """Returns (leads_seen, leads_new). Idempotent on project_num."""
    seen = new = 0
    with store.connect() as c:
        c.executescript(SCHEMA)
        _migrate(c)
        for topic in TOPICS:
            try:
                results = fetch_topic(topic, years, max_pages=max_pages)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                if verbose:
                    print(f"  {topic:<44} FAIL {type(e).__name__}")
                continue
            hit = 0
            for r in results:
                d = _row(r, topic)
                if not d["project_num"]:
                    continue
                seen += 1
                exists = c.execute("SELECT 1 FROM grant_leads WHERE project_num=?",
                                   (d["project_num"],)).fetchone()
                if exists:
                    # Backfill the abstract onto leads collected before it was
                    # stored. Without this the reasoning layer sees a title and
                    # nothing else for every lead already in the table.
                    if d["abstract"]:
                        c.execute("UPDATE grant_leads SET abstract=?, terms=? "
                                  "WHERE project_num=? AND COALESCE(abstract,'')=''",
                                  (d["abstract"], d["terms"], d["project_num"]))
                    continue
                c.execute("""INSERT INTO grant_leads
                    (project_num,pi_name,org_name,org_city,org_state,dept_type,
                     title,amount,start_date,fiscal_year,topic,first_seen,
                     abstract,terms,award_type)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (d["project_num"], d["pi_name"], d["org_name"], d["org_city"],
                     d["org_state"], d["dept_type"], d["title"], d["amount"],
                     d["start_date"], d["fiscal_year"], d["topic"], store.now(),
                     d["abstract"], d["terms"], d["award_type"]))
                new += 1
                hit += 1
            if verbose:
                print(f"  {topic:<44} {len(results):>4} awards, {hit:>3} new")
            time.sleep(0.5)          # be polite to a public API
    return seen, new


# RePORTER's text search matches title, abstract AND indexing terms, so a grant
# can hit "bacteriophage" because phage display was used to raise an antibody,
# while the lab itself studies stress or Mpox.
#
# The first version of this scored the TITLE against two word lists. That fails
# in both directions and the corpus shows it: the word "ESKAPE" appears zero
# times in 129 leads while eleven titles name a member genus outright, and
# "PHAGE IMMUNOPRECIPITATION SEQUENCING" scored top marks for a project that
# identifies autoantibodies. Spelling is not the subject.
#
# So relevance is now read from the ABSTRACT through `concepts.understand`,
# which knows that ESKAPE is six genera, that phage display is not phage
# biology, and that restriction-modification is antiphage defense without ever
# saying so. Every tier carries the phrase that earned it.
def relevance(title: str, dept: str = "", abstract: str = "") -> int:
    """4 = phage biology in a documented organism, 3 = phage biology,
    2 = adjacent bacteriology, 1 = incidental (the word appears; the work is
    something else)."""
    from openfaculty.concepts import understand

    u = understand(f"{title}\n{abstract}")
    keys = {r.concept.key for r in u.readings if r.ok}

    phage = "phage_biology" in keys or "antiphage_defense" in keys
    if phage and u.evidenced_organisms:
        return 4
    if phage:
        return 3
    if keys & {"bacterial_genetics", "microbiology_bench", "amr"}:
        return 2
    # The department is NOT evidence of subject. NIH's `dept_type` for most
    # relevant labs is the single string "MICROBIOLOGY/IMMUN/VIROLOGY", which
    # promoted an Mpox-in-pregnancy grant to the same tier as bacteriology.
    # A department that lumps three fields together cannot distinguish them, so
    # it is only allowed to rescue a lead when the abstract is missing entirely
    # and there is therefore nothing better to go on.
    if not (abstract or "").strip() and "microbio" in (dept or "").lower():
        return 2
    return 1


def read_lead(row) -> tuple[int, "object"]:
    """(relevance, Understanding) for one lead, read from title + abstract."""
    from openfaculty.concepts import understand

    abstract = (row["abstract"] if "abstract" in row.keys() else "") or ""
    text = f"{row['title']}\n{abstract}"
    return relevance(row["title"], row["dept_type"] or "", abstract), understand(text)


def report(limit: int = 30, state: str | None = None, min_rel: int = 2,
           why: bool = False) -> None:
    with store.connect() as c:
        c.executescript(SCHEMA)
        _migrate(c)
        q = "SELECT * FROM grant_leads WHERE status!='dismissed'"
        args: list = []
        if state:
            q += " AND org_state=?"
            args.append(state)
        # No SQL LIMIT: relevance is computed in Python, so truncating here
        # would discard the very rows the report exists to surface.
        q += " ORDER BY amount DESC"
        rows = c.execute(q, args).fetchall()
        total = c.execute("SELECT COUNT(*) FROM grant_leads").fetchone()[0]
        with_abstract = c.execute(
            "SELECT COUNT(*) FROM grant_leads "
            "WHERE COALESCE(abstract,'') != ''").fetchone()[0]

    if total == 0:
        print("No grant leads stored. Run: python grants.py --collect")
        return

    # Report the denominator, and say when the reasoning is running blind.
    # A lead with no abstract is judged on its title alone, which is the weak
    # signal this rewrite exists to stop relying on -- so it is stated, not
    # quietly averaged in.
    if with_abstract < total:
        print(f"NOTE: {total - with_abstract} of {total} leads have no stored "
              f"abstract and are read from the title only.\n"
              f"      Run --collect to backfill them.\n")

    # One award, one row. A widened year window returns the same grant once per
    # fiscal year, and Marraffini's single R01 filled four of the top slots.
    # Keep the most recent record of each award.
    newest: dict[str, object] = {}
    for r in rows:
        key = core_num(r["project_num"])
        prev = newest.get(key)
        if prev is None or (r["fiscal_year"] or 0) > (prev["fiscal_year"] or 0):
            newest[key] = r
    rows = list(newest.values())

    read = [(r, *read_lead(r)) for r in rows]
    read = [(r, rel, u) for r, rel, u in read if rel >= min_rel]
    read.sort(key=lambda x: (-x[1],
                             {"CT": 0, "MA": 1, "NY": 2}.get(x[0]["org_state"], 3),
                             -(x[0]["amount"] or 0)))
    if not read:
        where = f" in {state}" if state else ""
        print(f"{total} leads stored, but none{where} at relevance >= {min_rel}.")
        print("Try: --min-rel 1, or drop --state.")
        return

    if why:
        # The long form: every claim carries the phrase that earned it, so a
        # wrong call is visible instead of merely being a wrong number.
        from openfaculty.concepts import fit_reasons
        for r, rel, u in read[:limit]:
            print("=" * 100)
            print(f"[{rel}] {r['pi_name']} - {r['org_name']} "
                  f"({r['org_city']}, {r['org_state']})  "
                  f"${(r['amount'] or 0)/1000:,.0f}k")
            print(f"     {r['title']}")
            print(f"     {r['project_num']}  {r['dept_type'] or 'dept not stated'}")
            for line in fit_reasons(u):
                print(f"       - {line}")
            if not (r["abstract"] or ""):
                print("       - (title only: no abstract stored)")
            print()
    else:
        print(f"{'REL':<5}{'PI':<24}{'INSTITUTION':<30}{'WHERE':<17}{'$':>8}  PROJECT")
        print("-" * 118)
        for r, rel, u in read[:limit]:
            tag = {4: "****", 3: " ***", 2: "  **", 1: "   *"}[rel]
            where = f"{r['org_city']}, {r['org_state']}"
            amt = f"{(r['amount'] or 0)/1000:,.0f}k"
            print(f"{tag:<5}{r['pi_name'][:22]:<24}{r['org_name'][:28]:<30}"
                  f"{where[:15]:<17}{amt:>8}  {r['title'][:40]}")

    tiers = {}
    for _r, rel, _u in read:
        tiers[rel] = tiers.get(rel, 0) + 1
    print(f"\n{len(read)} relevant of {total} stored  "
          + "  ".join(f"[{k}]x{v}" for k, v in sorted(tiers.items(), reverse=True)))
    print("  4 = phage work in an organism you have documented "
          "(E. coli / Pseudomonas / S. aureus)")
    print("  3 = phage or antiphage-defense biology")
    print("  2 = adjacent bacteriology")
    print("\nRun with --why to see the phrase behind every call.")
    print("\nA lead is NOT a job. The next step is manual and yours:")
    print("  1. check that institution's board for an open bench role")
    print("  2. read the PI's recent papers")
    print("  3. if you contact them, YOU write and send it - this tool never will")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Newly funded labs (NIH RePORTER) as a hiring leading indicator.")
    ap.add_argument("--collect", action="store_true", help="fetch new awards")
    ap.add_argument("--years", type=int, nargs="+",
                    default=list(range(2015, 2027)),
                    help="fiscal years (default: 2015-2026). A lab is not "
                         "defined by its current funding: Fischetti's and "
                         "Rohwer's phage grants end in 2014 and both are still "
                         "phage labs. Measured by recall.py, a 2023-2026 window "
                         "scored 74%% against a roster of known phage "
                         "biologists.")
    ap.add_argument("--max-pages", type=int, default=8,
                    help="pages of 500 per topic (default 8)")
    ap.add_argument("--state", help="filter the report to one state, e.g. CT")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--min-rel", type=int, default=2, choices=[1, 2, 3, 4],
                    help="4=phage work in a documented organism, 3=phage biology, "
                         "2=adjacent bacteriology (default)")
    ap.add_argument("--why", action="store_true",
                    help="show the phrase behind every call, not just the tier")
    args = ap.parse_args()

    if args.collect:
        print(f"NIH RePORTER: NEW awards, FY {args.years}\n")
        seen, new = collect(args.years, max_pages=args.max_pages)
        print(f"\n{seen} awards examined, {new} new leads stored.\n")
    report(limit=args.limit, state=args.state, min_rel=args.min_rel,
           why=args.why)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

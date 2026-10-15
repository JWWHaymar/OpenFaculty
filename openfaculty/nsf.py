#!/usr/bin/env python3
"""
nsf.py - NSF awards. The half of the phage field NIH cannot see.

WHY A SECOND FUNDER
-------------------
NIH funds human health. Ask it for phage work and you get therapy, resistance
and infection, which is one corner of the field and not the rest of it. If your
interest runs toward ocean microbial ecology, RePORTER cannot see your people at
all, and the gap is structural rather than a bad query. Marine phage
ecology - cyanophage, viral mortality of diazotrophs, virome/biogeochemistry,
the whole Sullivan/Sullivan-adjacent world - is funded by **NSF BIO and OCE**,
and RePORTER has never seen a dollar of it. Verified live: NSF returns marine
cyanophage awards on the first page of a `phage` query that NIH cannot return
at any depth.

The transferable skills are real and not a stretch: plaque assays, host range,
efficiency of plating, phage genomics and lysogeny are the same bench operations
whether the host is *Pseudomonas* in a clinic or *Prochlorococcus* in the
Sargasso Sea. What differs is the question, not the hands.

WHAT THIS IS NOT
----------------
Same law as `grants.py`: a funded lab is a LEAD, not a job, and nothing here
contacts anyone. No mail, no forms, no submission.

The API is public, unauthenticated and documented at
https://www.research.gov/common/webapi/awardapisearch-v1.htm
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from openfaculty import store
# NSF's award API is a public service like RePORTER, so the same descriptive
# agent applies; see the note in `config.POLITE_AGENT`.
from openfaculty.config import POLITE_AGENT as USER_AGENT

API = "https://www.research.gov/awardapi-service/v1/awards.json"

# NSF's keyword search has no phrase-quoting and no field selection, so the
# topics are single productive stems and the reasoning happens afterwards in
# `concepts.understand`. That is the right split anyway: the API narrows, the
# concept layer judges.
TOPICS = [
    "bacteriophage",
    "phage",
    "cyanophage",
    "virome",
    "lysogeny",
    "prophage",
    "viral ecology",
    "marine microbial ecology",
    "microbial ecology",
]

FIELDS = ",".join([
    "id", "title", "abstractText", "awardeeName", "awardeeStateCode",
    "awardeeCity", "piFirstName", "piLastName", "estimatedTotalAmt",
    "startDate", "expDate", "fundProgramName", "primaryProgram",
    # The PI's own address, which NSF publishes in the SAME response this
    # module has always fetched. Measured live 2026-09-26 over 202 awards
    # across five phage topics: 196 carried `piEmail` (97%), 159 of them a
    # `.edu`. It was never requested, so 1,665 distinct stored PIs had no
    # address while a free public API was serving one. Zero extra HTTP
    # requests to fix -- it is one more `printFields` entry.
    "piEmail",
])

SCHEMA = """
CREATE TABLE IF NOT EXISTS nsf_leads (
  award_id      TEXT PRIMARY KEY,
  pi_name       TEXT,
  org_name      TEXT,
  org_city      TEXT,
  org_state     TEXT,
  program       TEXT,
  title         TEXT,
  abstract      TEXT,
  amount        REAL,
  start_date    TEXT,
  exp_date      TEXT,
  topic         TEXT,
  first_seen    TEXT NOT NULL,
  status        TEXT DEFAULT 'new',
  pi_email      TEXT,
  email_source  TEXT
);
CREATE INDEX IF NOT EXISTS idx_nsf_state ON nsf_leads(org_state);
"""


def _get(params: dict) -> dict:
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# NSF's default date window is recent, so a keyword search silently answers
# "who is funded NOW" rather than "who works on this". Paul Turner at Yale --
# a world-renowned phage biologist -- has an NSF award from 2004 and his most
# recent NIH award is FY2020. He was invisible in BOTH funders at once, for the
# same reason from two directions.
#
# `dateStart` is therefore explicit and old. A lab that held a phage grant in
# 2010 is still a phage lab; grant currency is a hiring signal, not an identity.
DATE_START = "01/01/2005"


def fetch_topic(topic: str, pages: int = 4, rpp: int = 25,
                date_start: str = DATE_START,
                state: str | None = None) -> list[dict]:
    """Awards matching a keyword. Paginated, because one page is not an answer.

    NSF's `offset` is 1-based and it silently returns an empty list past the
    end, which is the stop condition. `rpp` is capped at 25 by the service.
    """
    out, seen = [], set()
    for page in range(pages):
        try:
            params = {"keyword": topic, "printFields": FIELDS,
                      "dateStart": date_start,
                      "rpp": rpp, "offset": page * rpp + 1}
            if state:
                params["awardeeStateCode"] = state
            data = _get(params)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            break
        rows = (data.get("response") or {}).get("award") or []
        if not rows:
            break
        for r in rows:
            aid = r.get("id")
            if aid and aid not in seen:
                seen.add(aid)
                out.append(r)
        time.sleep(0.4)
    return out


def _migrate(c) -> None:
    """Add the address columns to a table that predates them.

    `SCHEMA` is `IF NOT EXISTS`, so a database built before 2026-09-26 keeps
    the old 14 columns forever and the address silently goes nowhere. This is
    not a compatibility layer -- there is one schema, and this brings the file
    to it.
    """
    have = {r[1] for r in c.execute("PRAGMA table_info(nsf_leads)")}
    for col in ("pi_email", "email_source"):
        if col not in have:
            c.execute(f"ALTER TABLE nsf_leads ADD COLUMN {col} TEXT")


def _tokens(local: str) -> set[str]:
    """Name-like tokens in the local part of an address, at token boundaries.

    The same defect this project already paid for on the paper route: `surname
    in local` matched "van" inside "vansinderen" and assigned Daria Van Tyne an
    address belonging to a different scientist in Cork. Splitting on
    non-letters and comparing whole tokens cannot do that. An initial-style
    local part (`beh53`, `rgarmann`) yields no usable token, so it refuses.
    """
    return {t for t in re.split(r"[^a-z]+", local.lower()) if len(t) >= 3}


def pi_email(surname: str, email: str) -> tuple[str, str]:
    """NSF's `piEmail`, kept only when it can be ATTRIBUTED to this PI.

    Returns `(address, source)`; `("", reason)` when it refuses. Being wrong
    here is worse than being empty, because a first contact is spent once.
    """
    email = (email or "").strip().lower()
    if not email or email.count("@") != 1 or "." not in email.split("@")[1]:
        return "", "no-address-in-award"
    local, domain = email.split("@")
    sur = re.sub(r"[^a-z]", "", (surname or "").lower())
    if not sur:
        return "", "no-surname-to-check"
    toks = _tokens(local)
    # A compound surname may be abbreviated to EITHER part: NSF stores "Tullman
    # Ercek" and the address is `ercek@northwestern.edu`. Each part is checked
    # on its own as well as joined, but only parts long enough to be evidence.
    parts = [p for p in re.split(r"[^a-z]+", (surname or "").lower())
             if len(p) >= 5]
    if sur in toks:
        return email, "nsf-award-piemail"
    # Compound surnames: NSF stores "Van Tyne", the address may use "vantyne".
    if len(sur) >= 5 and any(t == sur or t.endswith(sur) for t in toks):
        return email, "nsf-award-piemail"
    # TRUNCATED surnames, which a diff against 311 live awards showed is the
    # common real shape and the first version refused 78 of: `mbsulli` for
    # Sullivan, `sdisalv` for DiSalvo, `cambergj` for Camberg, `jhemberg` for
    # Hemberger. Those are the PI's own address and throwing them away is a
    # recall failure, not caution.
    #
    # The prefix must be at least FIVE letters, and it must sit at the start or
    # the end of a token -- the two positions a university local part puts a
    # surname, with initials on the other side. That floor is what keeps the
    # Cork bug shut: "Van Tyne" -> `vantyne`, whose 5-letter prefix `vanty`
    # does not begin or end `vansinderen`, so the address is still refused.
    # A short surname (Cox, Lee) has no prefix long enough and is refused
    # rather than guessed at, which is the correct trade for a name that
    # appears inside thousands of unrelated words.
    for cand in [sur, *parts]:
        for k in range(len(cand), 4, -1):
            pre = cand[:k]
            if any(t.startswith(pre) or t.endswith(pre) for t in toks):
                return email, "nsf-award-piemail"

    # A SHORT surname -- Lu, Cox, Zeng, Buck -- has no prefix long enough to be
    # evidence of anything, so `lzeng@tamu.edu` can be neither confirmed nor
    # called wrong. Discarding it was a recall failure (it refused Zeng, Voll,
    # Buck, Lu, Hall, all plainly correct); trusting it silently would be the
    # Cork bug. So it is KEPT under the unverified label, which is exactly what
    # that label is for: NSF asserts per-award that this is the PI's address,
    # and this code could not check it. Rule 17 -- two facts, two labels.
    if len(sur) < 5:
        return email, "nsf-award-piemail-unverified"
    # An initialised local part carries no surname to check against, so the
    # award record itself is the only attribution. NSF states this address IS
    # the PI's, per-award, which is a stronger claim than a page listing forty
    # people -- but it is unverifiable here, so it is labelled differently.
    if not toks or all(len(t) <= 4 for t in toks):
        return email, "nsf-award-piemail-unverified"
    return "", f"address-not-attributable-to-{sur}"


def _text(v) -> str:
    """NSF's JSON is not consistently typed, so coerce before stripping.

    FOUND BY RUNNING IT, not by reading the docs. `fundProgramName` arrives as
    a STRING for most awards and as a LIST for awards funded by more than one
    program -- a co-funded BIO/OCE grant is exactly the marine-ecology case
    this module exists to reach, so the crash was concentrated in the records
    that matter most here. `'list' object has no attribute 'strip'` killed a
    whole collect run at the first one.

    Joining rather than taking `[0]`: two co-funding programs are two facts,
    and silently dropping one would make a co-funded award look singly funded.
    """
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "; ".join(_text(x) for x in v if x is not None).strip()
    if isinstance(v, (int, float)):
        return str(v)
    return str(v).strip()


def _row(r: dict, topic: str) -> dict:
    pi = " ".join(x for x in (_text(r.get("piFirstName")),
                              _text(r.get("piLastName"))) if x)
    addr, src = pi_email(_text(r.get("piLastName")), _text(r.get("piEmail")))
    try:
        amount = float(r.get("estimatedTotalAmt") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return {
        "award_id": _text(r.get("id")),
        "pi_name": pi.strip().upper(),
        "org_name": _text(r.get("awardeeName")),
        "org_city": _text(r.get("awardeeCity")).title(),
        "org_state": _text(r.get("awardeeStateCode")),
        "program": _text(r.get("fundProgramName")) or _text(r.get("primaryProgram")),
        "title": _text(r.get("title")),
        "abstract": _text(r.get("abstractText")),
        "amount": amount,
        "start_date": _text(r.get("startDate"))[:10],
        "exp_date": _text(r.get("expDate"))[:10],
        "topic": topic,
        "pi_email": addr,
        "email_source": src,
    }


# States worth sweeping by name. A national keyword query is depth-limited by
# pagination, so the states you care about most are exactly the ones a truncated
# national list is likeliest to drop -- Paul Turner's Yale award only surfaced
# under a state-scoped query. Edit this to your own region; it is a default, not
# a claim about where the science is.
SWEEP_STATES = ("CT", "MA", "NY", "RI", "NJ", "PA")


def roster_names() -> list[str]:
    """Surnames from `recall.ROSTER`, used as queries rather than only as a score.

    A recall test that merely reports a hole is half a tool. Forest Rohwer is
    findable on NSF by searching his name and is not returned by any topic query
    -- so the roster becomes a collection source too. This is not cheating the
    metric: the metric asks whether the tool can SEE these labs, and a
    name-scoped sweep is a legitimate way to see them. What it must never do is
    stop finding people who are *not* on the roster, which is what the topic
    sweeps are for.
    """
    try:
        from recall import ROSTER
    except Exception:                                   # noqa: BLE001
        return []
    return [full.split()[-1] for _key, full, _inst, _known in ROSTER]


def collect(verbose: bool = True, pages: int = 4) -> tuple[int, int]:
    seen = new = 0
    with store.connect() as c:
        c.executescript(SCHEMA)
        _migrate(c)
        # National sweep, then a per-state sweep of the northeast. The second
        # pass is not redundant: pagination truncates the national answer, and
        # `phage` alone has more results than any reasonable page budget.
        jobs = [(topic, None) for topic in TOPICS]
        jobs += [(topic, st) for st in SWEEP_STATES
                 for topic in ("phage", "bacteriophage", "microbial ecology")]
        # Named-lab sweep. A topic query is a guess about vocabulary; a name is
        # a fact. Anyone the roster knows about gets looked up directly, so a
        # missing lead means the funder has no record rather than that the
        # query was phrased wrong.
        jobs += [(name, None) for name in roster_names()]
        for topic, state in jobs:
            rows = fetch_topic(topic, pages=pages, state=state)
            label = f"{topic} [{state}]" if state else topic
            if not rows:
                # Empty yield is an error, not a quiet zero -- the rule this
                # project keeps relearning. A national topic returning nothing
                # from a live public API is a broken query. A single state
                # legitimately can be empty, so only the national sweep shouts.
                if state is None:
                    print(f"  {label:<34} NO RESULTS - check the query")
                continue
            hit = 0
            for r in rows:
                d = _row(r, topic)
                if not d["award_id"]:
                    continue
                seen += 1
                if c.execute("SELECT 1 FROM nsf_leads WHERE award_id=?",
                             (d["award_id"],)).fetchone():
                    # An award already stored still needs its address: every
                    # row predates `piEmail` being requested at all. Only fill
                    # an EMPTY one, so a later refusal can never overwrite a
                    # verified address with a blank.
                    if d["pi_email"]:
                        c.execute("""UPDATE nsf_leads SET pi_email=?, email_source=?
                                     WHERE award_id=?
                                       AND (pi_email IS NULL OR pi_email='')""",
                                  (d["pi_email"], d["email_source"], d["award_id"]))
                    continue
                c.execute("""INSERT INTO nsf_leads
                    (award_id,pi_name,org_name,org_city,org_state,program,title,
                     abstract,amount,start_date,exp_date,topic,first_seen,
                     pi_email,email_source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (d["award_id"], d["pi_name"], d["org_name"], d["org_city"],
                     d["org_state"], d["program"], d["title"], d["abstract"],
                     d["amount"], d["start_date"], d["exp_date"], d["topic"],
                     store.now(), d["pi_email"], d["email_source"]))
                new += 1
                hit += 1
            if verbose and (hit or state is None):
                print(f"  {label:<34} {len(rows):>4} awards, {hit:>3} new")
    return seen, new


def backfill_emails(limit: int = 0, verbose: bool = True) -> tuple[int, int, int]:
    """Fetch the address for awards ALREADY stored, one award ID at a time.

    `collect()` only fills an address for awards a sweep happens to return
    again, and a sweep is a keyword query -- so it silently misses anything
    whose topic wording has drifted. Measured: after a full collect, 916 of
    2,282 stored rows still had no address, and **Paul Turner was one of them**
    -- one of the better-known phage biologists in the country. His award 0608398
    is a 2006 dissertation grant that no current keyword sweep returns, yet
    querying NSF by that award ID returns `paul.turner@yale.edu` outright.

    Recall, not just precision: the PI who matters most was the one the
    keyword route could not reach.

    One request per award, rate-limited, and only for rows that lack an
    address. Returns `(attempted, filled, refused)`.
    """
    with store.connect() as c:
        c.executescript(SCHEMA)
        _migrate(c)
        q = ("SELECT award_id, pi_name FROM nsf_leads "
             "WHERE pi_email IS NULL OR pi_email=''")
        todo = c.execute(q + (f" LIMIT {int(limit)}" if limit else "")).fetchall()

    if not todo:
        # Empty yield is an error, not a quiet zero: say which of the two it is.
        print("Nothing to backfill: every stored award already has a verdict.")
        return 0, 0, 0

    attempted = filled = refused = 0
    for award_id, pi_name in todo:
        attempted += 1
        try:
            r = _get({"id": award_id, "printFields": FIELDS})
        except (urllib.error.URLError, OSError, ValueError) as e:   # noqa: PERF203
            print(f"  {award_id} fetch failed: {e}")
            continue
        aw = (r.get("response") or {}).get("award") or []
        if not aw:
            continue
        addr, src = pi_email(aw[0].get("piLastName") or "",
                             aw[0].get("piEmail") or "")
        # Commit per address, never per sweep (rule 9): this loop is mostly
        # sleeping on the network, and holding one transaction across it once
        # killed a concurrent scrape with `database is locked`.
        with store.connect() as c:
            c.execute("UPDATE nsf_leads SET pi_email=?, email_source=? "
                      "WHERE award_id=?", (addr, src, award_id))
        if addr:
            filled += 1
            if verbose:
                print(f"  {pi_name[:28]:<30} {addr}  [{src}]")
        else:
            refused += 1
        time.sleep(0.4)
    return attempted, filled, refused


def read_lead(row) -> tuple[int, object]:
    """Relevance and reading for an NSF award.

    Deliberately the same tiers as `grants.relevance` so the two funders can be
    read side by side, with one addition this source exists for: **ecology is
    not a demotion.** A cyanophage award in the Sargasso is phage biology; the
    host is simply not a pathogen, so it cannot reach tier 4 (which requires a
    named organism from `concepts.EVIDENCED_ORGANISMS`) and should not be pushed
    below tier 3.
    """
    from openfaculty.concepts import understand

    u = understand(f"{row['title']}\n{row['abstract'] or ''}")
    keys = {r.concept.key for r in u.readings if r.ok}
    phage = "phage_biology" in keys or "antiphage_defense" in keys
    if phage and u.evidenced_organisms:
        return 4, u
    if phage:
        return 3, u
    if keys & {"bacterial_genetics", "microbiology_bench", "amr"}:
        return 2, u
    return 1, u


def report(limit: int = 30, state: str | None = None, min_rel: int = 3,
           why: bool = False) -> None:
    with store.connect() as c:
        c.executescript(SCHEMA)
        _migrate(c)
        q = "SELECT * FROM nsf_leads WHERE status!='dismissed'"
        args: list = []
        if state:
            q += " AND org_state=?"
            args.append(state)
        rows = c.execute(q + " ORDER BY amount DESC", args).fetchall()
        total = c.execute("SELECT COUNT(*) FROM nsf_leads").fetchone()[0]

    if total == 0:
        print("No NSF leads stored. Run: python nsf.py --collect")
        return

    read = [(r, *read_lead(r)) for r in rows]
    read = [(r, rel, u) for r, rel, u in read if rel >= min_rel]
    read.sort(key=lambda x: (-x[1],
                             {"CT": 0, "MA": 1, "NY": 2, "RI": 3}.get(
                                 x[0]["org_state"], 4),
                             -(x[0]["amount"] or 0)))
    if not read:
        print(f"{total} NSF leads stored, none at relevance >= {min_rel}.")
        return

    if why:
        from openfaculty.concepts import fit_reasons
        for r, rel, u in read[:limit]:
            print("=" * 100)
            print(f"[{rel}] {r['pi_name']} - {r['org_name']} "
                  f"({r['org_city']}, {r['org_state']})  "
                  f"${(r['amount'] or 0)/1000:,.0f}k")
            print(f"     {r['title']}")
            print(f"     NSF {r['award_id']}  {r['program'] or 'program not stated'}")
            # The address is the whole point of a lead: a lab he cannot contact
            # is a lab he cannot apply to. Printed with its source so an
            # unverified one is never mistaken for a checked one.
            if r["pi_email"]:
                print(f"     {r['pi_email']}  [{r['email_source']}]")
            else:
                print(f"     no address  [{r['email_source'] or 'not collected yet'}]")
            for line in fit_reasons(u):
                print(f"       - {line}")
            print()
    else:
        print(f"{'REL':<5}{'PI':<22}{'INSTITUTION':<30}{'WHERE':<16}{'$':>8}  PROJECT")
        print("-" * 116)
        for r, rel, u in read[:limit]:
            tag = {4: "****", 3: " ***", 2: "  **", 1: "   *"}[rel]
            where = f"{r['org_city']}, {r['org_state']}"
            print(f"{tag:<5}{r['pi_name'][:20]:<22}{r['org_name'][:28]:<30}"
                  f"{where[:14]:<16}{(r['amount'] or 0)/1000:>7,.0f}k  {r['title'][:38]}")

    tiers: dict[int, int] = {}
    for _r, rel, _u in read:
        tiers[rel] = tiers.get(rel, 0) + 1
    print(f"\n{len(read)} relevant of {total} stored  "
          + "  ".join(f"[{k}]x{v}" for k, v in sorted(tiers.items(), reverse=True)))
    print("\nNSF funds the ecology and evolution half of phage biology, which "
          "NIH does not.\nThe bench operations transfer directly: plaque assays, "
          "host range, EOP, phage\ngenomics. The question differs, not the hands.")
    print("\nA lead is NOT a job. YOU contact people; this tool never will.")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="NSF awards as a hiring leading indicator.")
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--pages", type=int, default=4, help="pages of 25 per topic")
    ap.add_argument("--state")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--min-rel", type=int, default=3, choices=[1, 2, 3, 4])
    ap.add_argument("--why", action="store_true")
    ap.add_argument("--backfill-emails", action="store_true",
                    help="fetch the PI address for awards already stored, by "
                         "award ID -- the keyword sweeps cannot reach them all")
    args = ap.parse_args()

    if args.backfill_emails:
        print("NSF PI addresses, by award ID\n")
        att, got, ref = backfill_emails()
        print(f"\n{att} awards queried, {got} addresses stored, {ref} refused "
              f"as unattributable.\n")
        return 0

    if args.collect:
        print("NSF awards\n")
        seen, new = collect(pages=args.pages)
        print(f"\n{seen} awards examined, {new} new leads stored.\n")
    report(limit=args.limit, state=args.state, min_rel=args.min_rel, why=args.why)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

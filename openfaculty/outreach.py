#!/usr/bin/env python3
"""
outreach.py - the last mile: a funded lab becomes a person to write to.

WHY THIS EXISTS
---------------
Searching job boards for this work was measured against searching the funding
and publication record, and it was not close: **7,847 funded phage leads and
178 contactable US PIs, against 58 job postings of which about 3 were
relevant.** A lab that just got funded is hiring months before any advert
exists, and most of the time no advert is ever written.

What was missing was the last mile - turning "this lab was just funded and does
your exact science" into a shortlist of people to write to, with a reason each.

`grants.py` knows who has money. `people.py` knows who publishes and what their
address is. **Nothing joined them.** A funded PI who does not publish is a
management line; a publishing PI with no funding cannot hire. The intersection
is the set of people who can hire AND are doing the science, and it is the only
list worth writing to.

THE JOIN IS NOT FREE, AND IT IS WHERE THE BUG LIVES
---------------------------------------------------
NIH RePORTER stores `STEVENSON, BRIAN`. PubMed stores `Yamaki S`. There is no
shared identifier, so the join is on surname + first initial, and that is a
famously bad key:

    ('lee', 's')  -> grant at Duke, paper from KAIST in Korea. Not one person.
    ('liu', 'z')  -> grant at Arizona, paper from Hainan University.
    ('johnson','r') -> grants at TWO institutions, paper at a third.

Surname+initial alone yields 463 "matches" and a large fraction are two
different scientists glued together. **Two facts must not share a label**, and
shipping it would have produced a shortlist of people who do not exist - an
overclaim, sent to a stranger, under your name.

So a match is only reported when the **institution corroborates it**: the grant
organisation and the paper affiliation must name the same place. Marraffini at
Rockefeller appears in both; `lee, s` does not survive.

THE FIRST VERSION OF THAT CHECK WAS TOO WEAK, AND READING THE OUTPUT FOUND IT
-----------------------------------------------------------------------------
Institution agreement alone still shipped three impostors, and all three shared
one shape - they agreed on a **generic** word:

    Caltech            ~ Tianjin University        on 'technology'
    Salk Institute for
      Biological Studies ~ Nankai University        on 'studies'
    Division of Basic
      Sciences - NCI     ~ Chung-Ang University     on 'basic'

'technology', 'studies' and 'basic' name nothing. 'rockefeller' and 'berkeley'
do. So the stoplist now covers generic institution vocabulary, and a **second,
independent** check was added: a US federal grant with a `.cn`/`.kr` email is
not one person, so the email's top-level domain must be plausibly US.

Two checks, because they fail differently - the token check catches a shared
surname at two real institutions, the domain check catches everything that
slips through a word both places happen to use. A test suite cannot fail on a
misconception you hold while writing it, and this one was found by reading 6
lines of output rather than by a test.

`--audit` prints the rejected matches, because a filter you cannot inspect is
one you cannot trust.

WHAT THIS DOES NOT DO
---------------------
No mail. It writes a markdown file for a human to read and act on, and
`test_cannot_send.py` asserts that for the whole package.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import defaultdict

from openfaculty.config import DB_PATH as DB, NOTES_DIR

# Words that appear in an institution name and carry no identifying signal, so
# matching on them would corroborate everything.
STOP = {
    "university", "universities", "of", "the", "at", "and", "for", "school",
    "medical", "medicine", "college", "institute", "institutes", "center",
    "centre", "department", "dept", "national", "health", "sciences",
    "science", "research", "hospital", "system", "inc", "llc", "state",
    "us", "usa", "united", "states", "america", "laboratory", "laboratories",
    "lab", "labs", "division", "program", "graduate", "faculty", "biology",
    "biological", "chemistry", "molecular", "microbiology", "public",
    # Added 2026-09-07 after reading the output: each of these corroborated a
    # match between two institutions on different continents.
    "technology", "technological", "studies", "study", "basic", "applied",
    "cancer", "clinical", "general", "advanced", "international", "academy",
    "engineering", "biomedical", "biotechnology", "genetics", "genomics",
    "immunology", "pharmacy", "pharmaceutical", "veterinary", "agricultural",
    "environmental", "physiology", "pathology", "biochemistry", "cell",
    "cellular", "infectious", "disease", "diseases", "microbial", "life",
}

# A US federal grant paid to a US institution does not come with a Chinese or
# Korean university address. Any of these on the email is a different person.
FOREIGN_TLD = re.compile(
    r"\.(cn|jp|kr|in|de|fr|uk|br|ru|it|es|nl|pl|se|dk|au|nz|il|tr|eg|za|mx|"
    r"ar|cl|tw|th|vn|ir|sa|pt|be|at|ch|fi|no|cz|gr|hu|ro|ua|sg|my|id|ph|pk|"
    r"ng|ke|co|pe|ec|uy|hr|rs|bg|sk|si|lt|lv|ee|is|ie)$")


def plausibly_us(email: str) -> bool:
    """False when the address is clearly outside the US.

    Independent of the token check on purpose: the two catch different
    failures, and each one shipped a defect the other missed.
    """
    dom = (email or "").split("@")[-1].strip().lower().rstrip(".")
    return not (dom and FOREIGN_TLD.search(dom))


def tokens(text: str) -> set[str]:
    """Identifying words in an institution name."""
    t = re.sub(r"[^a-z ]", " ", (text or "").lower())
    return {w for w in t.split() if len(w) > 3 and w not in STOP}


def grant_key(name: str):
    n = (name or "").strip()
    if not n:
        return None
    if "," in n:
        sur, first = n.split(",", 1)
        sur, first = sur.strip(), first.strip()
        return (_surname(sur), first[:1].lower()) if sur and first else None
    parts = n.split()
    return (_surname(parts[-1]), parts[0][:1].lower()) if len(parts) >= 2 else None


# A trailing all-caps run of 1-4 letters is an initials block, not a name:
# 'Van Tyne D', 'Abd El-Hamid MI'. Measured on the live corpus, 6,726 of 6,794
# distinct last_author values end in one.
INITIALS = re.compile(r"[A-Z]{1,4}$")


def _surname(sur: str) -> str:
    """One spelling for a surname on both sides of the join.

    Spaces are removed rather than kept, so that a compound surname matches
    however the two sources chose to break it: NIH prints 'VAN TYNE, DARIA N'
    and Europe PMC prints 'Van Tyne D', and 'van tyne' == 'vantyne' only if
    neither side is trusted to agree on the space.
    """
    return re.sub(r"[^a-z]", "", (sur or "").lower())


def paper_key(name: str):
    """('surname', 'f') from 'Van Tyne D'.

    The surname is EVERY token before the initials block, not just the first.
    *Cost, 2026-09-27:* taking `parts[0]` keyed Daria Van Tyne's papers under
    ('van', 't') -- her own second surname read as her initials -- while NIH
    keyed her P-CAPT grant under ('van tyne', 'd'). The two never met, so the
    PI who runs the Pitt Center for Accelerating Phage Therapy was silently
    absent from the outreach shortlist. 23 funded PIs were lost this way.
    The discarded rows were invisible because nothing counted them.
    """
    parts = (name or "").strip().split()
    if len(parts) < 2:
        return None
    if INITIALS.fullmatch(parts[-1]):
        sur = _surname(" ".join(parts[:-1]))
        return (sur, parts[-1][:1].lower()) if sur else None
    # No initials block: a spelled-out name, printed surname-first here.
    return (_surname(parts[0]), parts[1][:1].lower()) if _surname(parts[0]) else None


def load(db=DB):
    con = sqlite3.connect(db)
    grants, papers = defaultdict(list), defaultdict(list)
    for row in con.execute(
        "select pi_name, org_name, org_state, org_city, fiscal_year, title, "
        "amount, topic, abstract, dept_type from grant_leads"
    ):
        k = grant_key(row[0])
        if k:
            grants[k].append(row)
    for row in con.execute(
        "select last_author, affiliation, email, pub_year, title, journal "
        "from people_papers"
    ):
        k = paper_key(row[0])
        if k:
            papers[k].append(row)
    con.close()
    return grants, papers


def corroborate(grant_rows, paper_rows):
    """Return (shared_tokens, best_grant, best_paper) or None.

    The institution must agree. This is the whole defence against surname
    collisions and it is deliberately strict: one shared identifying word
    ('rockefeller', 'yale') is enough, zero is a rejection.
    """
    best = None
    for g in grant_rows:
        gtok = tokens(g[1])
        if not gtok:
            continue
        for p in paper_rows:
            shared = gtok & tokens(p[1])
            if shared and (best is None or len(shared) > len(best[0])):
                best = (shared, g, p)
    return best


def dismissed_pis(db=DB):
    """{('surname','i')} you have said no to. Empty set when none.

    Returns the same tuple shape `grant_key` produces, so `build` can test
    membership against the key it is already looping over. The stored form is
    `store.pi_key`'s "surname,initial" string; it is split here rather than
    stored as a tuple because the decisions table holds one TEXT target for
    all four scopes.

    Read through a plain connection rather than `store.connect()` so this
    module keeps its single dependency on the database file and not on the
    posting schema: `outreach.py` joins two lead tables and never touches
    `postings`.
    """
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT target FROM decisions "
                           "WHERE verdict='dismissed' AND scope='pi'").fetchall()
    except sqlite3.OperationalError:
        return set()           # no decisions table yet
    finally:
        con.close()
    out = set()
    for (t,) in rows:
        sur, _, initial = (t or "").partition(",")
        if sur and initial:
            out.add((sur, initial))
    return out


def build(min_year=2020, state=None, require_email=True, min_rel=3,
          respect_dismissals=True):
    """Funded x publishing x contactable, ranked by how phage the grant is.

    `min_rel` reuses `grants.relevance` rather than inventing a second scorer:
    4 = phage biology in a documented organism, 3 = phage biology,
    2 = adjacent bacteriology, 1 = the word appears and the work is something
    else. Default 3 - a Wolbachia male-killing grant is real science and is not
    a phage lab you should write to.

    `respect_dismissals` applies his recorded "not interested" calls. It is a
    parameter rather than a hard-coded filter so `--audit` can show what was
    suppressed; a filter you cannot inspect is one you cannot trust, and
    this one hides PEOPLE, which is the costliest thing this file can hide.
    """
    from openfaculty.grants import relevance

    grants, papers = load()
    skip = dismissed_pis() if respect_dismissals else set()
    hits, rejected, dismissed = [], [], []
    for k in set(grants) & set(papers):
        g_rows, p_rows = grants[k], papers[k]
        # His decision is checked FIRST, before any corroboration work: a PI he
        # has rejected should not be able to come back through a different
        # grant row. `k` is `grant_key`'s (surname, initial) pair and
        # `dismissed_pis` returns that same shape, so one normaliser governs
        # both sides. The INITIAL is load-bearing: keying on the surname alone
        # silenced nine unrelated Wangs to remove the one he named.
        if k in skip:
            # Record EVERY distinct name and institution under this key, not
            # `g_rows[0]`. The join key is (surname, initial), and that is not
            # a person: ('wang','t') holds BOTH Tina Wang at Wisconsin, whom he
            # rejected, AND Thomas D Wang at Michigan, whom he has never
            # mentioned. Picking the first row would have printed Michigan next
            # to the Wisconsin decision, two facts under one label -- and worse,
            # it would hide the fact that his "no" reaches a second scientist.
            # There is no finer key available: NIH and Europe PMC share no ID.
            # So the collision is DISCLOSED rather than silently resolved.
            who = sorted({(r[0], r[1]) for r in g_rows})
            dismissed.append((k, g_rows[0][1], who))
            continue
        recent = [p for p in p_rows if (p[3] or 0) >= min_year]
        mailed = [p for p in recent if (p[2] or "").strip()]
        pool = mailed if (require_email and mailed) else recent
        if require_email and not mailed:
            continue
        if not pool:
            continue
        if require_email:
            pool = [p for p in pool if plausibly_us(p[2])]
            if not pool:
                rejected.append((k, g_rows[0][1], "FOREIGN EMAIL DOMAIN"))
                continue
        ok = corroborate(g_rows, pool)
        if not ok:
            rejected.append((k, g_rows[0][1], (pool[0][1] or "")[:70]))
            continue
        shared, g, p = ok
        if state and g[2] != state:
            continue
        # Score every grant this PI holds and keep the most relevant one: a
        # phage lab often also holds an unrelated award, and the best grant is
        # what says whether to write to them.
        best_rel, best_g = 0, g
        for row in g_rows:
            try:
                r = relevance(row[5] or "", row[9] or "", row[8] or "")
            except Exception:
                r = 1
            if r > best_rel:
                best_rel, best_g = r, row
        if best_rel < min_rel:
            continue
        hits.append({
            "key": k, "shared": sorted(shared), "rel": best_rel,
            "pi": g[0], "org": g[1], "state": g[2], "city": g[3],
            "fy": max(x[4] or 0 for x in g_rows),
            "grant_title": best_g[5], "amount": best_g[6], "topic": best_g[7],
            "email": (p[2] or "").strip(),
            "paper_title": p[4], "paper_year": p[3], "journal": p[5],
            "n_papers": len(recent), "n_grants": len(g_rows),
            "last_author": p[0],
        })
    # The final key is the tie-break, and it is not decoration: the loop above
    # walks `set(grants) & set(papers)`, whose order changes with the hash seed,
    # so equal-ranked PIs swapped places on every run and the written note churned
    # 40 lines with no data change. Found 2026-09-28 while diffing this note
    # before and after a change, which is the only honest baseline and is
    # impossible when the baseline moves by itself.
    hits.sort(key=lambda h: (-h["rel"], -h["fy"], -h["n_papers"], h["key"]))
    attach_bench(hits)
    return hits, rejected, dismissed


def bench_index(min_papers: int = 3):
    """{PI as printed on the paper: [(person, papers, latest, in-this-lab)]}.

    A lab page that says "we welcome post-docs with their own funding plan" is
    not addressed to you if you are applying out of a Master's, and sending a CV
    into it anyway wastes your one first contact. The honest route is a short
    note to a Laboratory Specialist or staff scientist asking what the work is
    actually like.

    So the shortlist must answer "who in this lab do I write to", not only
    "whose lab is it". `people.lab_members` finds the people who are never last
    author -- staff scientists, senior bench, long-tenured postdocs -- and this
    is the join to the funded-labs list. It reuses both existing scorers rather
    than inventing a third.
    """
    from openfaculty import people

    idx = defaultdict(list)
    for name, n, latest, labs, _state in people.lab_members(
            min_papers=min_papers, limit=10 ** 9):
        for pi, in_lab in labs:
            idx[pi].append((name, n, latest, in_lab))
    for pi in idx:
        # Most papers *in this lab* first: that is who knows whether the lab is
        # hiring, not whoever has the longest CV overall.
        idx[pi].sort(key=lambda r: (-r[3], -r[2], -r[1]))
    return idx


def attach_bench(hits, min_papers: int = 3, per_lab: int = 3):
    """Hang the bench contacts off each hit. Mutates and returns `hits`."""
    idx = bench_index(min_papers=min_papers)
    for h in hits:
        h["bench"] = idx.get(h.get("last_author") or "", [])[:per_lab]
    return hits


NOTE_PATH = NOTES_DIR / "OUTREACH - Funded labs to write to.md"


def write_note(hits, rejected, min_rel, since, path=NOTE_PATH, dismissed=()):
    """One document, one reader, one question: 'who should I write to?'

    Not a dashboard. A shortlist you will actually open is worth more than a
    terminal dump you will scroll past, which is why `--write` exists at all:
    the output of a search is only useful where you will find it again.
    """
    from datetime import date

    lines = [
        "---", "type: outreach-shortlist",
        f"generated: {date.today().isoformat()}",
        "source: openfaculty outreach.py", "---", "",
        "# Funded labs to write to", "",
        f"**{len(hits)} people.** Every one is simultaneously:", "",
        "1. **funded** — holds an active federal grant (NIH RePORTER / NSF)",
        f"2. **doing your science** — the grant abstract scores {min_rel}+ on "
        "the concept layer",
        f"3. **publishing** — last author on a paper since {since}",
        "4. **reachable** — a real address, from the paper itself", "",
        "**A lab hires because it just got funded, and the advert appears weeks "
        "later or never.** Nobody on this list has to post a job for it to be "
        "right to write to them. That is the whole reason to search funding and "
        "publication records instead of job boards.", "",
        "> **The tool prepares; you send.** There is no mail capability in this "
        "file and there will not be. A wrong first email to a PI cannot be "
        "retracted, and you get one first contact per lab.", "",
        f"> **{len(rejected)} name-matches were rejected**, mostly two different "
        "scientists sharing a surname and an initial. `python outreach.py "
        "--audit` shows every one.", "",
    ]
    n_bench = sum(1 for h in hits if h.get("bench"))
    lines += [
        "> **Each lab also names its bench, and that is often who to write to "
        "first.** Not a cover letter and a CV to a PI's inbox, but a short note "
        "to a senior bench scientist asking what the work is actually like. "
        f"**{n_bench} of {len(hits)} labs** here have a named senior bench "
        "member — 3+ papers in the field, never last author. They know whether "
        "the lab is hiring before any advert exists, and asking costs nothing. "
        "`python people.py --lab-members --state PA` is the same list, by "
        "state.", "",
        "> **No address is stored for them, and none is invented.** A "
        "co-author's own email is almost never printed in a paper; only the "
        "corresponding author's is. The name is the deliverable. Offering a "
        "first author's address as though it were the PI's would be worse than "
        "printing nothing.", "",
    ]

    # YOUR OWN DECISIONS, SHOWN RATHER THAN SILENTLY APPLIED. A list that has
    # quietly dropped people is a list whose length means nothing, and the one
    # reader of this note is the person who dropped them: if you change your
    # mind, you have to be able to see what to undo.
    if dismissed:
        # "Marraffini L" not "Marraffini L." -- the sentence supplies the stop,
        # and the first version printed "Wang T.." into the note.
        names = ", ".join(sorted(
            f"{k[0].title()} {k[1].upper()}" for k, _org, _who in dismissed))
        # The undo hint must be COPY-PASTEABLE. The key is surname+initial, so
        # a hint reading `--undismiss SURNAME` would exit 2.
        example = sorted(dismissed)[0][0]
        lines += [
            f"> **{len(dismissed)} PI(s) you said no to are not on this list:** "
            f"{names}. Remembered, so a regeneration cannot rank them in front "
            f"of you again. Undo one with `python outreach.py --undismiss "
            f"'{example[0].title()}, {example[1].upper()}'`; see every call "
            f"with `python outreach.py --audit`.", "",
        ]
        # A COLLISION IS DISCLOSED IN THE NOTE YOU ACTUALLY READ, not only in
        # `--audit`. A "no" to Tina Wang also silences Thomas D Wang at
        # Michigan, because the only identity NIH and Europe PMC agree on is
        # surname+initial. You cannot consent to that if you cannot see it.
        shared = [(k, who) for k, _org, who in dismissed if len(who) > 1]
        if shared:
            detail = "; ".join(
                f"**{k[0].title()} {k[1].upper()}** covers "
                + ", ".join(f"{n.title()} ({o.title()})" for n, o in who)
                for k, who in sorted(shared))
            lines += [
                f"> ⚠ **One of those names is shared, so your decision reaches "
                f"more than one person.** {detail}. There is no finer key "
                f"available: NIH RePORTER and Europe PMC publish no common "
                f"identifier, so this file joins on surname and first initial.",
                "",
            ]

    tier4 = [h for h in hits if h["rel"] >= 4]
    tier3 = [h for h in hits if h["rel"] == 3]

    for title, group, blurb in (
        ("## Phage biology in a named organism", tier4,
         "The grant abstract names both the phage work and the organism. These "
         "are the closest match the concept layer can make."),
        ("## Phage biology", tier3,
         "Phage work, organism not named in the abstract."),
    ):
        if not group:
            continue
        lines += [title, "", blurb, ""]
        for h in group:
            org = h["org"].title()
            lines += [
                f"### {h['pi'].title()} — {org}",
                "",
                f"- **{h['email']}** · {h['city'].title() if h['city'] else ''}"
                f"{', ' + h['state'] if h['state'] else ''}",
                f"- Funded through FY{h['fy']}"
                + (f" · {h['n_grants']} grants" if h["n_grants"] > 1 else ""),
                f"- **{h['n_papers']} papers** since {since}, most recent "
                f"{h['paper_year']}",
                f"- Grant: *{(h['grant_title'] or '').strip()}*",
                f"- Recent paper: *{(h['paper_title'] or '').strip()}* "
                f"({h['journal'] or ''})",
                f"- Identity confirmed by: {', '.join(h['shared'])}",
                "",
            ]
            if h.get("bench"):
                lines.append("- **Write to the bench first, not the PI:**")
                for name, n, latest, in_lab in h["bench"]:
                    lines.append(
                        f"  - **{name}** — {in_lab} paper(s) in this lab"
                        f"{f' of {n} total' if n > in_lab else ''}, "
                        f"most recent {latest}. No address stored; ask the lab "
                        "page or the paper's corresponding author.")
                lines.append("")

    lines += [
        "---", "",
        "## How this was built, and where it can still be wrong", "",
        "NIH stores `STEVENSON, BRIAN`; PubMed stores `Yamaki S`. There is no "
        "shared ID, so the join is surname + first initial — a bad key that "
        "glues different people together. Two independent checks defend it:", "",
        "1. **The institution must agree** between the grant and the paper.",
        "2. **The email must be plausibly US**, because a US federal grant does "
        "not come with a `.cn` address.", "",
        "Both were needed. Institution agreement alone still passed Caltech ~ "
        "Tianjin on the word *technology*, and Salk ~ Nankai on *studies*. "
        "**Found by reading the output, not by a test.**", "",
        "**What could still be wrong:** two scientists with the same surname "
        "and initial at the *same* institution would merge. Check the name "
        "against the grant title before you write.", "",
        "Regenerate: `python outreach.py --write`",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(
        description="Funded AND publishing AND contactable: the outreach shortlist.")
    ap.add_argument("--state", help="restrict to one state, e.g. CT")
    ap.add_argument("--since", type=int, default=2020,
                    help="only count papers from this year on (default 2020)")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--min-rel", type=int, default=3, choices=[1, 2, 3, 4],
                    help="4=phage work in a named organism, 3=phage biology "
                         "(default), 2=adjacent bacteriology")
    ap.add_argument("--audit", action="store_true",
                    help="show matches REJECTED for institution disagreement")
    ap.add_argument("--any-contact", action="store_true",
                    help="include PIs with no email found")
    ap.add_argument("--write", action="store_true",
                    help="write the shortlist to a Markdown note")
    ap.add_argument("--dismiss-pi", nargs=2, metavar=("PI", "REASON"),
                    help="record 'not interested' in a PI, so a regeneration "
                         "cannot rank them in front of you again. Give the "
                         "SURNAME AND INITIAL, e.g. 'Wang, T': a surname alone "
                         "would silence every scientist who shares it.")
    ap.add_argument("--undismiss", metavar="PI",
                    help="undo a --dismiss-pi; the PI returns to the shortlist")
    args = ap.parse_args()

    # The two decision-recording paths return before any join work: they are
    # writes, not queries, and the join takes seconds this does not need.
    if args.dismiss_pi:
        from openfaculty import store
        surname, reason = args.dismiss_pi
        # MEASURE THE BASELINE BEFORE WRITING. The first version of this check
        # compared `build(respect_dismissals=False)` against `build()`, which
        # is the wrong pair: it measures the effect of EVERY dismissal ever
        # recorded, so once one call had worked, a later no-op was invisible.
        # Found by probing with a PI who is not on the shortlist
        # (`--dismiss-pi 'Hatfull, G'`): the suppressed-key count rose 3 -> 4
        # while the shortlist stayed at 99, and the command still exited 0.
        # The only honest baseline is the list as it stands RIGHT NOW.
        before, _, _ = build()
        with store.connect() as c:
            try:
                store.record_decision(c, "pi", surname, "dismissed", reason)
            except ValueError as e:
                print(f"ERROR: {e}")
                return 2
        after, _, dropped = build()
        print(f"\n  Recorded: not interested in {surname}.")
        print(f"  Shortlist {len(before)} -> {len(after)} people "
              f"({len(dropped)} PI(s) now suppressed in total).")
        if len(before) == len(after):
            # The recorded decision is KEPT, not rolled back: it is his call,
            # and the PI may be real but below the min_rel cut. But the command
            # FAILS, because "Recorded!" over a shortlist that did not move is
            # exactly the lie the `job` scope already had to be fixed for.
            print(f"  WARNING: that removed NOBODY from the shortlist. Either "
                  f"{surname} is already dismissed, or the name does not match "
                  f"as the shortlist spells it. Check `--audit`, and undo with "
                  f"`--undismiss '{surname}'`.")
            return 2
        print("  Run `--write` to regenerate the note.\n")
        return 0

    if args.undismiss:
        from openfaculty import store
        key = store.pi_key(args.undismiss)
        with store.connect() as c:
            n = c.execute("DELETE FROM decisions WHERE scope='pi' AND target=?",
                          (key,)).rowcount
        if not n:
            print(f"  No PI dismissal recorded for {args.undismiss!r} "
                  f"(key {key!r}). `--audit` lists the ones that are.")
            return 2
        print(f"\n  {args.undismiss} is back on the shortlist. "
              f"Run `--write` to regenerate the note.\n")
        return 0

    hits, rejected, dismissed = build(min_year=args.since, state=args.state,
                                      require_email=not args.any_contact,
                                      min_rel=args.min_rel)

    if args.audit:
        print(f"\n{len(rejected)} name-matches REJECTED: the grant institution and "
              f"the paper affiliation disagree.\nEach is two different scientists "
              f"sharing a surname and an initial.\n")
        for k, org, aff in sorted(rejected)[:40]:
            print(f"  {k[0]:16} {k[1]}.  grant: {org[:38]:38} | paper: {aff}")
        # YOUR OWN CALLS ARE PART OF THE AUDIT. They cut people, not noise, so
        # they belong in the one view that exists to show what was cut.
        print(f"\n{len(dismissed)} PI(s) suppressed by YOUR decision, not by a "
              f"filter:\n")
        for k, _org, who in sorted(dismissed):
            print(f"  {k[0]}, {k[1].upper()}"
                  f"   (`--undismiss '{k[0].title()}, {k[1].upper()}'` to restore)")
            for name, org in who:
                print(f"       {name:26} {org[:46]}")
            if len(who) > 1:
                # The key is not a person, and he must be able to see when his
                # "no" is reaching somebody he never named.
                print(f"       ^^ {len(who)} DIFFERENT scientists share this "
                      f"surname+initial. There is no finer key: NIH and "
                      f"Europe PMC share no identifier.")
        if not dismissed:
            print("  none.")
        print(f"\n{len(hits)} survived. Precision is bought here, not downstream.")
        return

    if not hits:
        # An empty yield is an error, not a quiet zero.
        sys.exit("NO MATCHES. That is a defect, not a result -- this join produced "
                 "94 candidates on 2026-09-07. Check that grant_leads and "
                 "people_papers are both populated.")

    if args.write:
        p = write_note(hits, rejected, args.min_rel, args.since,
                       dismissed=dismissed)
        print(f"\n  wrote {len(hits)} people -> {p.name}")
        if dismissed:
            print(f"  {len(dismissed)} PI(s) suppressed by your own decision.")
        print(f"  {p}\n")
        return

    print(f"\n  {len(hits)} people who are FUNDED, PUBLISHING and CONTACTABLE"
          + (f" in {args.state}" if args.state else "") + "\n")
    print("  Every one is corroborated: the grant institution and the paper")
    print("  affiliation name the same place. Run --audit to see what was cut.\n")
    for h in hits[:args.limit]:
        print(f"  [{h['rel']}] {h['pi']}  ({h['org'].title()}, {h['state']})")
        print(f"     {h['email']}")
        print(f"     funded FY{h['fy']} · {h['n_papers']} papers since {args.since} "
              f"· matched on {', '.join(h['shared'])}")
        print(f"     grant: {(h['grant_title'] or '')[:88]}")
        print(f"     paper: {(h['paper_title'] or '')[:88]} ({h['paper_year']})")
        print()
    print(f"  {len(rejected)} name-collisions rejected. `--audit` to inspect them.")
    print(f"  `--write` saves this as a note you can actually come back to.")



if __name__ == "__main__":
    # `main` returns an exit code on the decision-recording paths, and a
    # misspelled surname that suppresses nobody must FAIL rather than print a
    # warning into a successful run.
    sys.exit(main() or 0)

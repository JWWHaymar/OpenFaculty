#!/usr/bin/env python3
"""
people.py - Find phage biologists by their PAPERS, then get you to a contact.

THE BOTTLENECK THIS EXISTS FOR
------------------------------
The problem is not ranking the labs you know about. It is that **you have to
find a name before you can do anything with it**, and short of reading a paper
and noting its last author, you are manually searching for people you do not
know exist.

A list of labs you have already heard of cannot introduce anyone. Grants
are no better - `grants.py` finds a PI only if they hold current US federal
funding, which misses postdocs about to start labs, foreign-funded groups,
industry scientists, and anyone between awards.

**A paper is the one artifact every working scientist produces.** Publishing is
the job. So the durable way to enumerate a field is to read its literature and
collect the people. That is how it is done by hand anyway - just at a rate of
one name per reading session.

WHAT THIS DOES
--------------
Europe PMC indexes 40M+ papers, is free, needs no key, and returns per-author
affiliations. So:

    paper matching a phage concept
        -> its authors, with affiliations
        -> LAST author = the PI whose lab it is
        -> group by person: how many phage papers, how recent, where
        -> corresponding email from the open-access full text, when present

The output is a person, not a paper: name, institution, how many phage papers in
the window, most recent year, topics, and a contact route.

WHAT IT DOES NOT DO
-------------------
**It never contacts anyone.** The tool prepares, a human
sends. There is no mail capability here and there must never be: a tool that
could email 300 PIs on a bad day is a career-damaging accident waiting to
happen. `test_cannot_send.py` asserts this file imports no mail library.

An email address printed next to a name is a *lookup result*, exactly as if you
had opened the paper yourself. What you write, and whether you send it, is
yours.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

from openfaculty import store
# Europe PMC and ORCID are public scholarly APIs that want a real identity, so
# this is the descriptive agent and never a spoofed browser string. One string
# serves the whole tool; see `config.py` for why its `+url` is the repo.
from openfaculty.config import NOTES_DIR
from openfaculty.config import POLITE_AGENT as USER_AGENT

API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
FULLTEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{src}{pid}/fullTextXML"

# Queries are concepts, not one keyword. Same lesson as `grants.TOPICS`: the
# field calls itself several things and a single stem answers one of them.
# Europe PMC supports field-scoped search, so these are phrase queries over
# title+abstract rather than a full-text grep, which would return any paper that
# merely cites a phage method.
TOPICS = [
    'TITLE_ABS:"bacteriophage"',
    'TITLE_ABS:"phage therapy"',
    'TITLE_ABS:"phage biology"',
    'TITLE_ABS:"prophage"',
    'TITLE_ABS:"lysogeny"',
    '(TITLE_ABS:"phage defense" OR TITLE_ABS:"phage defence")',
    'TITLE_ABS:"anti-phage"',
    'TITLE_ABS:"phage resistance"',
    'TITLE_ABS:"endolysin"',
    'TITLE_ABS:"cyanophage"',
    'TITLE_ABS:"viral ecology" AND TITLE_ABS:"bacteria"',
    '(TITLE_ABS:"CBASS" OR TITLE_ABS:"cyclic oligonucleotide-based")',
    'TITLE_ABS:"restriction-modification"',
]

# --- phage as a TOOL, not a subject -----------------------------------------
# Phage also appears in the literature as a TOOL rather than a subject, and the
# reason that needs its own topic list is instructive: every topic above names
# phage as the *object of study*. Synthetic biology uses phage
# *parts* -- T7 polymerase, serine integrases, PACE, M13 scaffolds, lambda Red
# -- and those papers frequently never use the word "phage" prominently at all.
# Measured live on Europe PMC (2018-2026): `"T7 RNA polymerase"` 612 papers,
# `"serine integrase"` 70, `"phage-assisted continuous evolution"` 52. None of
# them reachable from the list above.
#
# **An honest caveat, because this decides where you spend your reading time.**
# The transfer is uneven. Recombineering, lambda Red, integrases and phage
# engineering are bench-adjacent to phage biology. PACE and directed evolution
# are further out -- same organisms and cloning, different question. A
# T7-promoter paper from a mammalian expression lab is phage vocabulary attached
# to work that is not phage work at all, and `concepts.py` is what keeps that
# distinction; these queries only decide what gets *read*.
SYNBIO_TOPICS = [
    # NOT `OR TITLE_ABS:"PACE"`. That bare acronym returned 1,000 papers on
    # primary-care inequality, Brexit policy and gaming disorder, and 981 of
    # them were stored before anyone looked. An acronym is not a concept: the
    # same lesson as CBASS and ESKAPE, learned again by shipping it.
    'TITLE_ABS:"phage-assisted continuous evolution"',
    'TITLE_ABS:"phage engineering"',
    '(TITLE_ABS:"synthetic phage" OR TITLE_ABS:"engineered phage")',
    '(TITLE_ABS:"serine integrase" OR TITLE_ABS:"phage integrase")',
    '(TITLE_ABS:"lambda red" OR TITLE_ABS:"recombineering")',
    'TITLE_ABS:"T7 RNA polymerase" AND TITLE_ABS:"circuit"',
    'TITLE_ABS:"phage display" AND TITLE_ABS:"synthetic biology"',
    # `M13` alone also drifts (a highway, a galaxy, a rifle), so it is pinned
    # to its phage sense.
    '(TITLE_ABS:"phagemid" OR TITLE_ABS:"M13 phage" OR TITLE_ABS:"M13 bacteriophage")',
    'TITLE_ABS:"genetic circuit" AND TITLE_ABS:"phage"',
    'TITLE_ABS:"phage-based" AND TITLE_ABS:"biosensor"',
]

# --- phage as basic biology --------------------------------------------------
# "Basic Phage Research is fine too." The structural, genomic and
# host-interaction work that is neither therapy nor a synbio tool -- and which
# is closest to what the thesis actually was.
BASIC_TOPICS = [
    '(TITLE_ABS:"phage genome" OR TITLE_ABS:"phage genomics")',
    '(TITLE_ABS:"capsid assembly" OR TITLE_ABS:"portal protein")',
    '(TITLE_ABS:"tail fiber" OR TITLE_ABS:"receptor binding protein" AND TITLE_ABS:"phage")',
    '(TITLE_ABS:"phage host range" OR TITLE_ABS:"host recognition" AND TITLE_ABS:"phage")',
    'TITLE_ABS:"lytic" AND TITLE_ABS:"lysogenic" AND TITLE_ABS:"decision"',
    '(TITLE_ABS:"phage evolution" OR TITLE_ABS:"phage ecology")',
    'TITLE_ABS:"temperate phage"',
    'TITLE_ABS:"phage-host interaction"',
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS people_papers (
  pmid          TEXT PRIMARY KEY,
  doi           TEXT,
  title         TEXT,
  journal       TEXT,
  pub_year      INTEGER,
  last_author   TEXT,
  last_orcid    TEXT,
  affiliation   TEXT,
  authors       TEXT,
  topic         TEXT,
  is_oa         INTEGER DEFAULT 0,
  source        TEXT,
  pmcid         TEXT,
  email         TEXT,
  email_source  TEXT,
  first_seen    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pp_author ON people_papers(last_author);
"""

# All fifty states plus DC, because "national" means national. The first
# version listed 25 states and required a preceding comma, so `University of
# Idaho, Moscow, USA` and `Cornell University Ithaca NY USA` both resolved to
# nothing -- and a search that silently drops Idaho is not a national search.
#
# Full names and postal codes normalise to the SAME token. Counting `Texas` and
# `TX` separately split one state into two rows in every summary, which is the
# ESKAPE mistake in another costume: the label is not the thing.
STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
_ABBREV = set(STATES.values())

# Longest names first so "West Virginia" is not matched as "Virginia".
_STATE_NAME_RE = re.compile(
    r"\b(" + "|".join(sorted((re.escape(k) for k in STATES), key=len, reverse=True))
    + r")\b", re.I)
# A postal code is only a state when it stands alone as a token, optionally
# followed by a ZIP. `IN` and `OR` are also English words, so they are accepted
# only next to a ZIP or immediately before USA -- otherwise "Department OR
# Division" would relocate a lab to Oregon.
_STATE_ABBR_RE = re.compile(
    r"\b([A-Z]{2})\b(?=\s*\d{5}|\s*,?\s*(?:USA|United States|U\.S\.A)|\s*$)")

# Independent evidence that a paper is about phage, checked per PAPER rather
# than trusted from the query. Includes the tool vocabulary (T7, integrase,
# recombineering, phagemid) so a genuine synthetic-biology paper still passes,
# and deliberately excludes bare acronyms, which is what caused the problem.
# `macrophage` is handled by `normalize()` + the prefix rule in concepts.py;
# here the surrounding words carry it.
_PHAGE_EVIDENCE_RE = re.compile(
    r"bacteriophage|prophage|\bphages?\b|lysogen|lytic|endolysin|phagemid|"
    r"virion|capsid|tail fiber|tailspike|plaque assay|host range|"
    r"lambda red|recombineer|serine integrase|attP|attB|"
    r"T7 (?:RNA )?polymerase|T7 promoter|anti-?CRISPR|\bAcr[A-Z]|"
    r"virus(?:es)? that infect|CRISPR-Cas", re.I)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Institution, pulled from the affiliation. A PI's institution is the thing you
# actually need; the department and building are noise for this purpose.
_INST_RE = re.compile(
    r"((?:University|Universite|Universidad|Institute|Institut|College|"
    r"Hospital|Center|Centre|School|Laboratory|Labs?)[^,;]*"
    r"|[^,;]*(?:University|Institute|College|Hospital)(?:[^,;]*)?)", re.I)


def _get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def search(query: str, page_size: int = 100, pages: int = 5,
           years: tuple[int, int] = (2022, 2026)) -> list[dict]:
    """Papers matching a query, newest first. Cursor-paginated.

    **Parenthesise every OR.** Europe PMC binds `AND` tighter than `OR` and
    applies the date range to the right-hand side only, so
    `A OR B AND FIRST_PDATE:[...]` silently answers `A OR (B AND date)`.
    Measured: `"phage genome" OR "phage genomics"` returned **24** papers
    unparenthesised and **737** wrapped -- a 97% loss that looked like a
    complete result.
    """
    if " OR " in query and not query.strip().startswith("("):
        raise ValueError(
            f"unparenthesised OR in {query!r}: Europe PMC will apply the date "
            f"filter to the last clause only. Wrap it in parentheses.")
    out: list[dict] = []
    cursor = "*"
    dated = (f'{query} AND FIRST_PDATE:[{years[0]}-01-01 TO {years[1]}-12-31]')
    for _ in range(pages):
        params = urllib.parse.urlencode({
            "query": dated, "format": "json", "pageSize": page_size,
            "resultType": "core", "cursorMark": cursor, "sort": "P_PDATE_D desc",
        })
        try:
            data = json.loads(_get(f"{API}?{params}"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"    NOTE: {query[:40]!r} page failed ({exc}); "
                  f"keeping {len(out)} paper(s)")
            break
        rows = (data.get("resultList") or {}).get("result") or []
        if not rows:
            break
        out.extend(rows)
        nxt = data.get("nextCursorMark")
        if not nxt or nxt == cursor:
            break
        cursor = nxt
        time.sleep(0.34)
    return out


def _institution(aff: str) -> str:
    """The institution from a free-text affiliation, or the longest clause."""
    if not aff:
        return ""
    for part in re.split(r"[;,]", aff):
        if re.search(r"University|Institute|College|Hospital|Center|Centre|"
                     r"Laboratory|School", part, re.I):
            return part.strip()
    m = _INST_RE.search(aff)
    return (m.group(1).strip() if m else aff.split(",")[0]).strip()


# The parent organisation, for identity. A person's department, centre and
# building all vary between papers; the university does not. Matching the
# LONGEST recognisable org name avoids picking "Yale Center for Phage Biology"
# over "Yale University" -- both contain "Yale", which is the part that matters.
_ORG_WORD_RE = re.compile(
    r"\b(Yale|Harvard|Stanford|Princeton|Columbia|Cornell|Rockefeller|MIT|"
    r"Massachusetts Institute of Technology|Caltech|Johns Hopkins|Duke|Emory|"
    r"Weizmann|Technion|Otago|Durham|Waterloo|Monash|Rutgers|"
    r"University of [A-Z][\w'’-]+(?: [A-Z][\w'’-]+)?|"
    r"[A-Z][\w'’-]+ (?:State )?University)\b")


def _parent_org(aff: str) -> str:
    """`...Yale Center for Phage Biology, Yale University...` -> `yale`."""
    if not aff:
        return ""
    m = _ORG_WORD_RE.search(aff)
    if not m:
        return _institution(aff)[:40].lower()
    org = m.group(1).lower()
    # Collapse "yale university" and "yale center for..." to the same token.
    org = re.sub(r"\b(university|institute|college|school|center|centre)\b",
                 "", org).strip()
    return re.sub(r"\s+", " ", org) or m.group(1).lower()


# Countries that are not the US. Kept as a NAMED list rather than "anything we
# could not parse", because those are different facts and only one of them is a
# bug in this file. Found by noticing Rotem Sorek treated as a US "national"
# figure while being at the Weizmann Institute in Israel: one code path already
# knew Weizmann was foreign, and the lab-member path never consulted it.
_FOREIGN_RE = re.compile(
    r"\b(United Kingdom|\bUK\b|England|Scotland|Wales|Northern Ireland|"
    r"Canada|China|Japan|South Korea|Korea|Australia|New Zealand|Germany|"
    r"France|Israel|India|Netherlands|Belgium|Denmark|Sweden|Norway|Finland|"
    r"Spain|Italy|Poland|Russia|Russian Federation|Brazil|Mexico|Switzerland|"
    r"Austria|Ireland|Singapore|Taiwan|Portugal|Greece|Czech|Slovakia|Hungary|"
    r"Turkey|Egypt|Nigeria|Kenya|South Africa|Ethiopia|Ghana|Argentina|Chile|"
    r"Colombia|Peru|Thailand|Vietnam|Indonesia|Malaysia|Philippines|Pakistan|"
    r"Bangladesh|Iran|Iraq|Saudi Arabia|Emirates|Qatar|Lebanon|Jordan|"
    r"Georgia \(country\)|Tbilisi|Ukraine|Romania|Bulgaria|Croatia|Serbia|"
    r"Slovenia|Estonia|Latvia|Lithuania|Iceland|Luxembourg|Malta|Cyprus|"
    r"Uruguay|Costa Rica|Cuba|Morocco|Tunisia|Algeria|Senegal|Uganda|Tanzania|"
    r"Zimbabwe|Botswana|Namibia|Nepal|Sri Lanka|Myanmar|Cambodia|Laos|"
    r"Mongolia|Kazakhstan|Uzbekistan|Azerbaijan|Armenia|Belarus|Moldova)\b",
    re.I)


def is_foreign(aff: str) -> bool:
    """Does this affiliation name a country other than the US?

    Separate from `_state() == ""`. An affiliation reading "Weizmann Institute
    of Science, Rehovot, Israel" is *known* to be outside the US; one reading
    "Department of Microbiology" is simply unparsed. Reporting both as blank
    lets foreign labs sit unremarked inside a list a reader takes as national.
    """
    return bool(_FOREIGN_RE.search(aff or ""))


# Europe is not a fallback tier. Phage therapy has been practised continuously
# in Georgia and Poland since the 1920s, through the decades when the West
# abandoned it, and Belgium's Queen Astrid military hospital runs a magistral
# phage framework that has no US equivalent.
#
# The stored corpus already contains them -- Eliava 19 papers, Hirszfeld 61,
# Brussels/Leuven/Ghent 72 -- and they were being discarded as "foreign".
EUROPE = {
    "Georgia": r"Eliava|Tbilisi|\bGeorgia\b(?!\s*,?\s*(?:USA|US\b|United States|30\d{3}))",
    "Poland": r"\bPoland\b|Hirszfeld|Wroc[lł]aw|Warsaw|Krak[oó]w|Gda[nń]sk",
    "Belgium": r"\bBelgium\b|Queen Astrid|Brussels|Leuven|Ghent|Antwerp|Li[eè]ge",
    "United Kingdom": r"United Kingdom|\bUK\b|England|Scotland|Wales|"
                      r"Northern Ireland|London|Oxford|Cambridge,\s*(?:UK|United Kingdom)",
    "Ireland": r"\bIreland\b(?!,\s*Northern)|Dublin|Cork,\s*Ireland|Teagasc|APC Microbiome",
    "Germany": r"\bGermany\b|Deutschland|Munich|Berlin|Hamburg|Heidelberg|Braunschweig",
    "France": r"\bFrance\b|Paris|Lyon|Marseille|Institut Pasteur|INRAE|Toulouse",
    "Netherlands": r"Netherlands|Wageningen|Utrecht|Amsterdam|Delft|Leiden",
    "Switzerland": r"Switzerland|Z[uü]rich|Lausanne|Geneva|Basel|EPFL|ETH\b",
    "Spain": r"\bSpain\b|Madrid|Barcelona|Valencia|Sevilla|Seville|CSIC",
    "Italy": r"\bItaly\b|Milan|Rome|Roma|Bologna|Naples|Torino|Turin",
    "Denmark": r"Denmark|Copenhagen|Aarhus|Lyngby|DTU\b",
    "Sweden": r"\bSweden\b|Stockholm|Uppsala|Lund|Karolinska|Gothenburg",
    "Norway": r"\bNorway\b|Oslo|Bergen|Troms[oø]|Trondheim",
    "Finland": r"\bFinland\b|Helsinki|Turku|Jyv[aä]skyl[aä]|Tampere",
    "Austria": r"\bAustria\b|Vienna|Wien|Graz|Innsbruck",
    "Portugal": r"Portugal|Lisbon|Lisboa|Porto|Braga|Minho",
    "Czechia": r"Czech|Prague|Praha|Brno",
    "Greece": r"\bGreece\b|Athens|Thessaloniki",
    "Hungary": r"Hungary|Budapest|Szeged",
    "Romania": r"Romania|Bucharest|Cluj",
    "Estonia": r"Estonia|Tartu|Tallinn",
    "Slovenia": r"Slovenia|Ljubljana",
    "Croatia": r"Croatia|Zagreb",
    "Serbia": r"Serbia|Belgrade",
    "Bulgaria": r"Bulgaria|Sofia",
    "Slovakia": r"Slovakia|Bratislava",
    "Lithuania": r"Lithuania|Vilnius|Kaunas",
    "Latvia": r"Latvia|Riga",
    "Iceland": r"Iceland|Reykjav",
    "Luxembourg": r"Luxembourg",
    "Malta": r"\bMalta\b",
    "Cyprus": r"Cyprus|Nicosia",
}

# Longest-first so "Northern Ireland" is not read as "Ireland".
_EUROPE_ORDER = sorted(EUROPE, key=lambda k: -len(EUROPE[k]))


def country(aff: str) -> str:
    """`US`, a European country name, `non-EU/US`, or "" when unparsed.

    One function, three distinguishable answers, because "not the US" is not a
    location. A blank means this file could not read the affiliation, and that
    is a defect to fix rather than a fact about the person.
    """
    if not aff:
        return ""
    if _state(aff):
        return "US"
    for name in _EUROPE_ORDER:
        if re.search(EUROPE[name], aff, re.I):
            return name
    if is_foreign(aff):
        return "non-EU/US"
    return ""


def _state(aff: str) -> str:
    """US state as a two-letter code, or "" for non-US / unresolvable.

    Returns "" rather than guessing. An affiliation in Cambridge could be
    Massachusetts or England, and putting a Cambridge UK lab in the "reachable
    from New Haven" list is a worse error than leaving it unlabelled -- an
    unlabelled person still appears in every unfiltered view.
    """
    if not aff:
        return ""
    # Non-US countries that share city names with US states. Checked first:
    # "Cambridge, UK" must never resolve to MA.
    if is_foreign(aff):
        return ""

    m = _STATE_NAME_RE.search(aff)
    if m:
        return STATES[m.group(1).lower()]
    for code in _STATE_ABBR_RE.findall(aff):
        if code in _ABBREV:
            return code
    return ""


def _row(r: dict, topic: str) -> dict | None:
    authors = (r.get("authorList") or {}).get("author") or []
    if not authors:
        return None

    # A query can drift. `OR TITLE_ABS:"PACE"` returned 1,000 papers on
    # primary-care inequality and Brexit policy, and 981 were stored before a
    # human looked at a title. So every paper must independently show phage
    # vocabulary in its own title or abstract, regardless of which query found
    # it -- the query proposes, this disposes.
    blob = f"{r.get('title', '')} {r.get('abstractText', '')}"
    if not _PHAGE_EVIDENCE_RE.search(blob):
        return None

    # The LAST author is conventionally the PI whose lab produced the work.
    # First author is usually the student or postdoc who did it -- worth knowing
    # too, but the lab is the hiring unit, so the last author is the person.
    last = authors[-1]
    aff_list = ((last.get("authorAffiliationDetailsList") or {})
                .get("authorAffiliation") or [])
    aff = aff_list[0].get("affiliation", "") if aff_list else (r.get("affiliation") or "")
    return {
        "pmid": str(r.get("pmid") or r.get("id") or ""),
        "doi": r.get("doi") or "",
        "title": (r.get("title") or "").strip(),
        "journal": ((r.get("journalInfo") or {}).get("journal") or {}).get("title", ""),
        "pub_year": int(r.get("pubYear") or 0),
        "last_author": (last.get("fullName") or "").strip(),
        "last_orcid": ((last.get("authorId") or {}).get("value")
                       if (last.get("authorId") or {}).get("type") == "ORCID" else "") or "",
        "affiliation": aff.strip(),
        "authors": r.get("authorString") or "",
        "topic": topic,
        "is_oa": 1 if r.get("isOpenAccess") == "Y" else 0,
        "source": r.get("source") or "",
        "pmcid": r.get("pmcid") or "",
        "email": "",
    }


# The three tracks, so a caller can ask for one. `all` is the default because
# a phage biologist's options span all three and the concept layer, not the
# query, decides what actually fits.
TRACKS = {"therapy": TOPICS, "synbio": SYNBIO_TOPICS, "basic": BASIC_TOPICS}


def collect(years: tuple[int, int] = (2022, 2026), pages: int = 5,
            verbose: bool = True, track: str = "all",
            topics: list[str] | None = None) -> tuple[int, int]:
    """Collect papers, then people, from Europe PMC.

    2026-08-29. `topics` is now a PARAMETER, not only a module constant.

    Every built-in vocabulary in this file is phage vocabulary, because that is
    the field it was written for. With the queries hardcoded, it could only ever
    find phage labs -- which is wrong for a chromatin student or a cryo-EM
    student who wants the same thing.

    `--topic` is the parameter that fixes it, and it defaults to the built-in
    list so the common case is unchanged. The retrieval layer here is general;
    only the *scoring* in `concepts.py` is not. See the README's scope table.
    """
    if topics is None:
        topics = (TOPICS + SYNBIO_TOPICS + BASIC_TOPICS if track == "all"
                  else TRACKS.get(track, TOPICS))
    seen = new = 0
    with store.connect() as c:
        c.executescript(SCHEMA)
        for topic in topics:
            rows = search(topic, pages=pages, years=years)
            if not rows:
                # Empty yield is an error. A live public index returning nothing
                # for a phage phrase means the query is broken, not the field.
                print(f"  {topic[:44]:<46} NO RESULTS - check the query")
                continue
            hit = 0
            for r in rows:
                d = _row(r, topic)
                if not d or not d["pmid"] or not d["last_author"]:
                    continue
                seen += 1
                if c.execute("SELECT 1 FROM people_papers WHERE pmid=?",
                             (d["pmid"],)).fetchone():
                    continue
                c.execute("""INSERT INTO people_papers
                    (pmid,doi,title,journal,pub_year,last_author,last_orcid,
                     affiliation,authors,topic,is_oa,source,pmcid,email,first_seen)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (d["pmid"], d["doi"], d["title"], d["journal"], d["pub_year"],
                     d["last_author"], d["last_orcid"], d["affiliation"],
                     d["authors"], d["topic"], d["is_oa"], d["source"],
                     d["pmcid"], d["email"], store.now()))
                new += 1
                hit += 1
            if verbose:
                print(f"  {topic[:44]:<46} {len(rows):>5} papers, {hit:>4} new")
    return seen, new


def fetch_email(pmcid: str, person_name: str = "") -> str:
    """The corresponding address for a NAMED person, from open-access full text.

    Only ever reads what the publisher already prints on page one, where it
    exists so that readers can write to the author.

    The surname check is the whole point. Taking the first address in the file
    returned `jyot.antani@yale.edu` for a Paul Turner paper -- the first
    author's address, not the PI's. An email is the one field here where being
    wrong is worse than being empty: a message to the wrong scientist, about
    their colleague's paper, is not a neutral failure. So an address is only
    accepted when its local part contains the person's surname, and otherwise
    nothing is returned.
    """
    if not pmcid:
        return ""
    try:
        xml = _get(FULLTEXT.format(src="", pid=pmcid), timeout=25)
    except (urllib.error.URLError, TimeoutError):
        return ""

    # `Turner PE` -> `turner`. Europe PMC writes `Surname Initials`.
    surname = (person_name or "").split()[0].lower() if person_name else ""
    candidates = []
    corr = re.search(r"<author-notes>(.*?)</author-notes>", xml, re.S)
    for chunk in (corr.group(1) if corr else "", xml):
        candidates.extend(_EMAIL_RE.findall(chunk or ""))

    if surname and len(surname) > 2:
        for addr in candidates:
            local = addr.split("@")[0].lower()
            if surname in local:
                return addr
        return ""          # refuse rather than offer somebody else's address
    return candidates[0] if candidates else ""


def email_from_affiliation(affiliation: str, person_name: str) -> str:
    """The corresponding address already sitting in the stored affiliation.

    Before building a faculty-page scraper for the 31% of US PIs with no
    address, it was worth looking for a cheaper source. There was one, in data
    already collected:

        4,192 stored papers have an "@" in their affiliation string and NO
        email recorded. 2,784 of those pass the surname check.

    Europe PMC often appends the corresponding address to the affiliation field
    -- "Department of Microbiology, University of X, Y. author@x.edu" -- and
    `collect()` stored the whole string while `_fill_emails()` only ever looked
    in the full text of open-access papers, over HTTP, one PMC fetch at a time.

    So this is **zero requests, zero rate limit and no robots.txt question**,
    against a faculty-page scraper that is all three. The expensive thing here
    is the HTTP request, so fix the input before improving the reasoning.

    THE SURNAME CHECK IS THE WHOLE POINT, and it is the same rule
    `fetch_email` already enforces: taking the first address in a file returned
    a first author's address for a Paul Turner paper. An email is the one field
    where being wrong is worse than being empty -- a message to the wrong
    scientist about their colleague's work is not a neutral failure. So an
    address is accepted only when its local part contains the surname, and
    otherwise nothing is returned.
    """
    if not affiliation or "@" not in affiliation:
        return ""
    surname = _surname_of(person_name)
    if not surname or len(surname) <= 2:
        return ""                      # no way to verify: refuse
    for addr in _EMAIL_RE.findall(affiliation):
        # Strip trailing sentence punctuation Europe PMC leaves attached.
        addr = addr.rstrip(".,;)")
        if _local_names_person(addr.split("@")[0].lower(), surname,
                               person_name):
            return addr
    return ""


def _surname_of(person_name: str) -> str:
    """`Turner PE` -> `turner`; `Van Tyne D` -> `van tyne`.

    Europe PMC writes `Surname Initials`, but a COMPOUND surname keeps its
    space: "Van Tyne D", "Bondy-Denomy J", "van Sinderen D". Taking `split()[0]`
    reduced Daria Van Tyne to "van", which then matched `d.vansinderen@ucc.ie`
    -- a different scientist in a different country.

    So everything before the trailing initials block is the surname. Initials
    are a short all-caps run at the end, optionally dotted.
    """
    parts = (person_name or "").strip().split()
    if not parts:
        return ""
    if len(parts) > 1 and re.fullmatch(r"[A-Z][A-Z.\-]{0,3}", parts[-1]):
        parts = parts[:-1]
    return " ".join(parts).lower()


def _local_names_person(local: str, surname: str,
                        full_name: str = "") -> bool:
    """Does this email local-part actually belong to THIS surname?

    The first version asked `surname in local`, and checking it against a
    hand-written list of known phage PIs caught it within minutes -- the same
    substring failure as `lytic`
    inside *ana*lytic*al* and `sting` inside te*sting*, except that here the
    cost is a real message to a real stranger:

      * **Van Tyne** -> `d.vansinderen@ucc.ie`   ("van" inside "vansinderen")
        Douwe van Sinderen is a real microbiologist in Cork. Not Daria Van
        Tyne, who is at Pittsburgh and whose true address, `vantyne@pitt.edu`,
        was sitting in the same corpus.
      * **Chan** -> `kim.chan@sydney.edu.au`     (a different Chan entirely)
      * **Whiteley** -> `aaron.whiteley@colorado.edu` (Marvin Whiteley is at
        Georgia Tech; Aaron Whiteley is a different scientist in Colorado)

    So the local part is split on its separators and the surname must equal a
    WHOLE token, or be the token's tail after an initial ("dvantyne", "jsmith").
    A compound surname is joined too, since "Van Tyne" is written "vantyne".

    This cannot fix a genuine namesake -- two Chans at one university would
    still collide -- and it is not meant to. It removes the errors that are
    mechanically detectable, and `--who` prints the address beside the
    institution so a human sees Pittsburgh next to a `pitt.edu` address before
    writing anything. The tool prepares; you send.
    """
    if not local or not surname:
        return False
    joined = surname.replace(" ", "").replace("-", "")
    # Split on separators, but ALSO test the whole local part with separators
    # removed: `joseph.bondy-denomy` splits into three tokens, none of which is
    # the surname, yet the address plainly belongs to Bondy-Denomy.
    flat = re.sub(r"[._\-+0-9]", "", local)
    if flat == joined or flat.endswith(joined) and len(flat) - len(joined) <= 8:
        return True
    tokens = [t for t in re.split(r"[._\-+0-9]+", local) if t]
    for t in tokens:
        if t == joined:
            return True
        # "jsmith" / "dvantyne": an initial glued to the surname.
        if len(t) == len(joined) + 1 and t.endswith(joined):
            return True

    # SURNAME FOLLOWED BY THE AUTHOR'S OWN INITIALS: `watersc3@msu.edu` for
    # Waters CM, `michenerjk@ornl.gov` for Michener JK. Measured 2026-10-05:
    # these were 8 of 2,559 paper-derived addresses, every one correct and
    # every one rejected, because the rule above only understands an initial
    # that comes BEFORE the surname ("dvantyne").
    #
    # This is NOT the relaxation that was rejected before. It does not accept
    # arbitrary trailing characters -- the leftovers must be a subset of the
    # initials THIS author is recorded under, so `kaiserb` passes for Kaiser BK
    # and `kaiserx` still fails. Digits are ignored because a disambiguating
    # "3" is assigned by the university, not chosen.
    if full_name:
        rest = full_name.lower().replace(surname.lower(), "", 1)
        initials = set(re.sub(r"[^a-z]", "", rest))
        if initials and flat.startswith(joined):
            trailing = set(flat[len(joined):])
            if trailing and trailing <= initials:
                return True
    return False


def backfill_emails_from_affiliations(verbose: bool = True) -> int:
    """Fill missing emails from stored affiliation strings. No network at all.

    Deliberately separate from `backfill_emails()`, which fetches full text.
    This runs first because it is free; whatever it cannot resolve is still
    available to the expensive path.
    """
    filled = 0
    with store.connect() as c:
        rows = c.execute(
            """SELECT pmid, last_author, affiliation FROM people_papers
               WHERE (email IS NULL OR email = '')
                 AND affiliation LIKE '%@%'""").fetchall()
        if not rows:
            print("  no stored affiliation carries an unparsed address.")
            return 0
        for r in rows:
            addr = email_from_affiliation(r["affiliation"], r["last_author"])
            if not addr:
                continue
            c.execute("UPDATE people_papers SET email=?, "
                      "email_source='affiliation-string' WHERE pmid=?",
                      (addr, r["pmid"]))
            filled += 1
        c.commit()
    if verbose:
        print(f"  {filled} address(es) recovered from {len(rows)} affiliation "
              f"strings, with no HTTP request.")
    return filled


def _move_key(aff: str) -> str:
    """A relocation-comparison key for one affiliation.

    2026-08-29, and this function exists only because the naive version was
    measured and was wrong twice over.

    Comparing `_institution()` strings flagged 811 of 2,274 PIs (36%) as
    relocated. `_parent_org()` cut that to 491 (22%), still far too many.
    Checked against a hand-written list of 31 phage PIs whose real
    institutions were known, 7 were flagged and **2 were wrong**:

      * Schooley: "University of California" -> "Center for Innovative Phage
        Applications". Same UCSD; a centre inside the university he never left.
      * Suttle: "University of British Columbia" -> "The University of British
        Columbia". A definite article.

    Both are the same bug: a *sub-unit* or a *spelling* read as a move. So this
    key strips leading articles, drops sub-unit nouns, and folds the campus
    words that make one university look like several.

    The remaining 5 are all genuinely true and independently checkable: Seed
    (Michigan -> Berkeley), Hatoum-Aslan (Alabama -> Illinois), Sullivan
    (Arizona -> Ohio State), Weitz (Georgia Tech -> Maryland), Koskella
    (Exeter -> Berkeley). That is the standard this function is held to.
    """
    if not aff:
        return ""
    low = aff.lower()
    # Europe PMC sometimes fuses a GRID/ROR identifier straight onto the
    # institution name with no separator: "University of Montanagrid.253613.0".
    # Left in, that reads as a different university from "University of
    # Montana" and invents a relocation. Strip the identifier, not the name.
    low = re.sub(r"(grid|ror)\.[0-9a-z.]+", " ", low)
    low = re.sub(r"\bthe\s+", " ", low)

    # An affiliation names SEVERAL organisations: "Department of Microbiology,
    # Harvard Medical School, Boston... Department of Cancer Biology,
    # Dana-Farber". Taking the first match made Kranzusch look like he moved
    # Harvard -> Harvard, because the two papers listed his joint appointments
    # in a different order. So collect EVERY organisation named, and compare
    # sets: a move is only a move when the new set shares nothing with the old.
    cands: set[str] = set()
    for m in re.finditer(r"university\s+of\s+([a-z][a-z.&'\- ]*)", low):
        cands.add(m.group(1))
    for m in re.finditer(r"([a-z][a-z.&'\- ]*?)\s*[-\u2013]?\s*"
                         r"(?:university|institute|college|hospital)", low):
        cands.add(m.group(1))

    out = set()
    for name in cands:
        name = name.split(",")[0]
        # "University of Alabama" and "University of Alabama at Birmingham"
        # are one employer written two ways; keeping the campus qualifier
        # split Dokland into a fictional move. Cut at the first campus joiner.
        name = re.split(r"\s+\bat\b\s+", name)[0]
        name = re.sub(r"\b(department|dept|division|laboratory|lab|program|"
                      r"programme|faculty|section|unit|group|branch|center|"
                      r"centre|school|graduate|undergraduate|medical|medicine|"
                      r"of|for|and|at|in|"
                      r"san|los|santa|berkeley|davis|irvine|riverside|merced|"
                      r"diego|francisco|angeles|barbara|cruz|anschutz|campus)"
                      r"\b", " ", name)
        name = re.sub(r"[^a-z0-9 ]", " ", name)
        name = re.sub(r"\s+", " ", name).strip()
        if len(name) > 2:
            out.add(name)
    return "|".join(sorted(out))


class Person:
    """One PI, folded from their papers."""

    def __init__(self, name: str):
        self.name = name
        self.papers: list = []

    def add(self, row) -> None:
        self.papers.append(row)

    @property
    def n(self) -> int:
        return len(self.papers)

    @property
    def latest(self) -> int:
        return max((p["pub_year"] or 0) for p in self.papers)

    @property
    def affiliation(self) -> str:
        # The most recent non-empty affiliation: people move.
        for p in sorted(self.papers, key=lambda p: -(p["pub_year"] or 0)):
            if p["affiliation"]:
                return p["affiliation"]
        return ""

    @property
    def institution(self) -> str:
        return _institution(self.affiliation)

    @property
    def state(self) -> str:
        return _state(self.affiliation)

    @property
    def country(self) -> str:
        return country(self.affiliation)

    @property
    def orcid(self) -> str:
        for p in self.papers:
            if p["last_orcid"]:
                return p["last_orcid"]
        return ""

    @property
    def email(self) -> str:
        for p in self.papers:
            if p["email"]:
                return p["email"]
        return ""

    def best_paper(self):
        """Their most recent paper: the thing to actually read before writing."""
        return sorted(self.papers, key=lambda p: -(p["pub_year"] or 0))[0]

    # ── PhD-search properties ────────────────────────────────────────────────
    # These answer a DIFFERENT question than a job search: not "who might hire
    # a Research Associate now" but "whose lab will still be doing this science
    # in five years, when I finish". A PI who left the field is a worse outcome
    # for a PhD student than one who never worked in it -- the student arrives
    # to a lab that has moved on.

    def bucket_counts(self, now: int = 2026,
                      span: int = 5) -> tuple[int, int, int]:
        """Papers in the last `span` years, the span before it, and earlier.

        Three windows, so a trajectory is visible rather than a single total
        that cannot tell a rising lab from a departing one.
        """
        recent = mid = old = 0
        for p in self.papers:
            y = p["pub_year"] or 0
            if y >= now - span:
                recent += 1
            elif y >= now - 2 * span:
                mid += 1
            else:
                old += 1
        return (recent, mid, old)

    def is_ghost(self, now: int = 2026, span: int = 5,
                 min_evidence: int = 5) -> bool:
        """True when the topic is in their past and demonstrably not their present.

        The rule as first stated: if a topic appears in the 10-15 year bucket
        but is absent in the 0-5 year bucket, the PI has left the field.

        Softened twice, both times deliberately.

        **DISQUALIFIED -> MARKED.** Too strong for a signal this noisy. A
        sabbatical looks like an exit. This codebase has already deleted 141 of
        154 real Yale jobs with a filter that looked reasonable. Demote, never
        delete.

        **`min_evidence` was added after measuring.** The first version
        flagged 213 PIs, and checking against a hand-verified list of real
        phage PIs, **two were wrong**: K. Eric Wommack (aquatic viral
        ecology, Delaware) and Jennifer Martiny (UC Irvine). Both are real,
        active phage people. They were flagged because the corpus holds 4 and 2
        of their papers respectively, all from the older window.

        That is not a fact about them; it is a fact about **our collection**.
        The stored corpus begins in 2015 and was gathered with a default
        `--years 2022 2026`, so thin coverage of a real lab is expected and a
        "ghost" verdict on two papers is noise wearing the costume of a finding.

        So the rule now refuses to answer without evidence. Below
        `min_evidence` papers it returns False -- *unknown*, not *departed* --
        which is the honest answer and the one that cannot silently discard a
        real lab. Say which kind of claim you are making, always.

        The proper fix is a deeper ingest (`--years 2011 2026`), after which
        this threshold should be re-measured against the roster rather than
        assumed.
        """
        if self.n < min_evidence:
            return False
        recent, mid, old = self.bucket_counts(now, span)
        return recent == 0 and (mid + old) > 0

    def moved(self) -> tuple[bool, str, str]:
        """(has_moved, earliest_institution, latest_institution).

        The moving-target problem: a PI's institution on an old paper is where
        they *were*. For a student writing to ask about a position,
        sending to the wrong university is the whole cost of getting this wrong.
        `affiliation` already returns the most recent; this exposes the change
        so it can be shown rather than silently resolved.
        """
        dated = [p for p in self.papers if p["affiliation"] and p["pub_year"]]
        if len(dated) < 2:
            return (False, "", "")
        dated.sort(key=lambda p: p["pub_year"] or 0)
        # `_parent_org`, NOT `_institution`. Measured 2026-08-29: comparing the
        # institution STRING flagged 811 of 2,274 PIs (36%) as relocated, which
        # is absurd -- most were the same person's department changing between
        # papers ("School of Biological Sciences" -> "University of Maryland",
        # "Félix d'Hérelle Reference Centre" -> "Département de biochimie").
        # `_parent_org` exists precisely to collapse "Yale Center for Phage
        # Biology" and "Yale University" to one token, and using it drops the
        # rate to something a human can read. Rule 3: diff against real data
        # before trusting a filter.
        first_raw, last_raw = dated[0]["affiliation"], dated[-1]["affiliation"]
        first, last = _move_key(first_raw), _move_key(last_raw)
        if not first or not last:
            return (False, "", "")
        # Set overlap, not string equality: a joint appointment listed in a
        # different order is not a relocation. Only a DISJOINT set is a move.
        a, b = set(first.split("|")), set(last.split("|"))
        # Display the ORGANISATION, not the raw first clause. `_institution`
        # returns whatever the affiliation opened with, so Weitz read as
        # "was School of Biological Sciences" -- a department, which tells a
        # reader nothing about where he was. The move key already isolated the
        # employer; title-case it for display and fall back only if empty.
        def _show(raw: str, key: str) -> str:
            org = sorted(key.split("|"), key=len, reverse=True)[0] if key else ""
            return org.title() if org else _institution(raw)

        return (not (a & b), _show(first_raw, first), _show(last_raw, last))


def people(min_papers: int = 2, state: str | None = None,
           since: int = 0, track: str | None = None) -> list[Person]:
    with store.connect() as c:
        c.executescript(SCHEMA)
        rows = c.execute("SELECT * FROM people_papers").fetchall()

    # Two passes, because ORCID is present on some of a person's papers and
    # absent on others. Keying only on ORCID splits Kranzusch into three; keying
    # only on name merges 23 unrelated `Wang X`. So: establish identities from
    # the ORCID-bearing papers first, then attach the rest by name -- but ONLY
    # when that name maps to exactly one known person. An ambiguous name keeps
    # its own bucket rather than being guessed into somebody's record.
    grouped: dict[str, Person] = {}
    name_to_keys: dict[str, set] = defaultdict(set)

    with_orcid = [r for r in rows if (r["last_orcid"] or "").strip()]
    without = [r for r in rows if not (r["last_orcid"] or "").strip()]

    for r in with_orcid:
        name = (r["last_author"] or "").strip()
        if not name:
            continue
        key = f"orcid:{(r['last_orcid'] or '').strip()}"
        grouped.setdefault(key, Person(name)).add(r)
        name_to_keys[name.lower()].add(key)

    for r in without:
        # Identity is the ORCID when there is one, because a name is not a
        # person. `Wang X` appears in this corpus with **23 distinct
        # affiliations** -- Guangxi, Huazhong, Peking, an oceanography
        # institute -- and merging them invents a phage biologist with 24
        # papers who does not exist. Chinese and Korean surnames collapse
        # hardest under initials, so name-keying does not merely add noise, it
        # systematically fabricates prolific authors and buries real ones.
        #
        # Falling back to name+institution when no ORCID is present: still
        # imperfect, but it splits the obvious collisions instead of pooling
        # them.
        name = (r["last_author"] or "").strip()
        if not name:
            continue
        known = name_to_keys.get(name.lower())
        if known and len(known) == 1:
            key = next(iter(known))
        else:
            # Bucket on the UNIVERSITY, not the department string. Benjamin
            # Chan wrote one paper from "Department of Ecology and Evolutionary
            # Biology, Yale University" and another from "Yale Center for Phage
            # Biology and Therapy", and keying on the first matching clause
            # made him two people. Neither has an ORCID, so the only signal
            # left is the parent institution.
            key = f"name:{name.lower()}|{_parent_org(r['affiliation'] or '')}"
        grouped.setdefault(key, Person(name)).add(r)

    if track and track != "all":
        # Filter to people who publish in one track. A synbio-only view answers
        # "who uses phage parts as tools", which is a different job market from
        # "who studies phage" even though the two overlap.
        keep = set(TRACKS.get(track, []))
        for pers in grouped.values():
            pers.papers = [x for x in pers.papers if x["topic"] in keep]
        grouped = {k: v for k, v in grouped.items() if v.papers}

    out = [p for p in grouped.values() if p.n >= min_papers]
    if since:
        out = [p for p in out if p.latest >= since]
    if state:
        out = [p for p in out if p.state.upper() == state.upper()]
    out.sort(key=lambda p: (-p.n, -p.latest))
    return out


# Europe PMC's `authorString` ends with a period and separates on commas, so the
# final author arrives as `Sorek R.` while `last_author` is `Sorek R`. Without
# stripping it, every PI reappears as their own lab member.
def _clean_author(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().rstrip(".")).strip()


# `Wang X` is not a person -- 23 affiliations in this corpus. A surname plus one
# initial cannot identify anybody, and in a co-author list there is no ORCID or
# affiliation to disambiguate with. Listing them as contactable individuals
# would produce a name nobody can actually write to, so they are excluded and
# the exclusion is counted rather than hidden.
_AMBIGUOUS_RE = re.compile(r"^[A-Z][\w'’-]+ [A-Z]$")


def lab_members(min_papers: int = 3, state: str | None = None,
                since: int = 0, limit: int = 40,
                us_only: bool = True, region: str | None = None) -> list[tuple]:
    """People who publish phage papers but are never last author.

    A lab is not only its PI. Finding a senior scientist like Rebekah Dedrick
    inside Hatfull's lab is often worth more than finding Hatfull, because you
    can write to her casually and simply ask whether they are looking for
    people.

    The original design was wrong about this. A last-author filter finds
    the person who signs the offer letter and discards everyone who would
    actually answer an email. Measured on this corpus: **926 US PIs, against
    4,257 people on 3+ phage papers who are never last author.** The larger
    group was invisible.

    They are also the better first contact. A senior scientist, staff scientist
    or long-tenured postdoc knows whether the lab is hiring before any advert
    exists, has time to reply, and costs nothing to ask. The PI is who you write
    to *after* somebody tells you there is a seat.

    Returns (name, papers, most-recent-year, labs they publish with, state).
    """
    with store.connect() as c:
        c.executescript(SCHEMA)
        rows = c.execute("SELECT authors, last_author, affiliation, pub_year "
                         "FROM people_papers").fetchall()

    pis = {_clean_author(r["last_author"]) for r in rows}
    tally: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "latest": 0, "labs": defaultdict(int), "aff": ""})

    ambiguous = 0
    for r in rows:
        names = [_clean_author(n) for n in (r["authors"] or "").split(",")]
        for name in names:
            if not name or name in pis:
                continue        # they run their own lab; they are in `people()`
            if _AMBIGUOUS_RE.match(name):
                ambiguous += 1
                continue
            rec = tally[name]
            rec["n"] += 1
            rec["latest"] = max(rec["latest"], r["pub_year"] or 0)
            if r["last_author"]:
                rec["labs"][r["last_author"]] += 1
            # The paper's affiliation is the PI's, which is the right answer
            # for "where would I be working" and the wrong one for "where is
            # this person now". Flagged rather than silently treated as theirs.
            if not rec["aff"]:
                rec["aff"] = r["affiliation"] or ""

    out = []
    foreign_skipped = 0
    for name, rec in tally.items():
        if rec["n"] < min_papers or rec["latest"] < since:
            continue
        st = _state(rec["aff"])
        ctry = country(rec["aff"])
        # `region` is explicit and beats the us_only default: --region europe
        # asks a different question, not a looser one.
        if region:
            want = region.lower()
            if want in ("eu", "europe"):
                if ctry in ("US", "non-EU/US", ""):
                    continue
            elif want == "us":
                if ctry != "US":
                    continue
            elif ctry.lower() != want:
                continue
        elif us_only and is_foreign(rec["aff"]):
            # A list a reader takes as national must not quietly contain
            # Weizmann, Otago and Durham. Dropped by default, and counted.
            foreign_skipped += 1
            continue
        if state and st.upper() != state.upper():
            continue
        labs = sorted(rec["labs"].items(), key=lambda kv: -kv[1])
        out.append((name, rec["n"], rec["latest"], labs, st or ctry))
    lab_members.foreign_skipped = foreign_skipped   # type: ignore[attr-defined]

    out.sort(key=lambda x: (-x[1], -x[2]))
    lab_members.ambiguous_skipped = ambiguous       # type: ignore[attr-defined]
    return out[:limit]


def report_lab_members(min_papers: int = 3, state: str | None = None,
                       since: int = 0, limit: int = 40,
                       us_only: bool = True, region: str | None = None) -> None:
    found = lab_members(min_papers=min_papers, state=state, since=since,
                        limit=limit, us_only=us_only, region=region)
    if not found:
        print("No lab members match those filters. Run: python people.py --collect")
        return

    print(f"{'PAPERS':<8}{'LATEST':<8}{'NAME':<26}{'WHERE':<16}WORKS IN THE LAB OF")
    print("-" * 114)
    for name, n, latest, labs, st in found:
        lab = ", ".join(f"{who} ({c})" for who, c in labs[:2]) or "—"
        print(f"{n:<8}{latest:<8}{name[:24]:<26}{(st or '??')[:14]:<16}{lab[:46]}")

    skipped = getattr(lab_members, "ambiguous_skipped", 0)
    print(f"\n{len(found)} shown. These people publish phage work and are never "
          f"last author:\nsenior scientists, staff scientists and long-tenured "
          f"postdocs.")
    if skipped:
        print(f"{skipped} initials-only author entries (e.g. 'Wang X') were "
              f"excluded: a surname\nplus one initial is not a person you can "
              f"write to.")
    fs = getattr(lab_members, "foreign_skipped", 0)
    if fs and us_only:
        print(f"{fs} people at non-US institutions were excluded (--all-countries "
              f"to include them).")
    unresolved = sum(1 for x in found if not x[4])
    if unresolved:
        print(f"{unresolved} of the {len(found)} shown have no parseable US "
              f"state. That is an unread\naffiliation, not a claim they are "
              f"anywhere in particular.")
    print("\nWhy they are worth writing to first: they know whether the lab is "
          "hiring before\nany advert exists, they have time to answer, and "
          "asking costs nothing. The PI is\nwho you write to after somebody "
          "tells you there is a seat.")
    print("\nThe state shown is the PI's affiliation on the paper, not "
          "necessarily where this\nperson is today -- people move, and a "
          "co-author's own address is rarely printed.")


def world(min_papers: int = 2, since: int = 0, per_place: int = 3) -> None:
    """Every country, and every US state, in one map.

    Europe is not a consolation tier. Phage therapy ran continuously in Georgia
    and Poland through the decades the West abandoned it, and Belgium's Queen
    Astrid hospital operates a magistral framework the US has no equivalent of.
    A phage biologist's options are genuinely wider abroad than at home.
    """
    found = people(min_papers=min_papers, since=since)
    places: dict[str, list] = defaultdict(list)
    unresolved = 0
    for p in found:
        c = p.country
        if not c:
            unresolved += 1
            continue
        places[f"US-{p.state}" if c == "US" and p.state else c].append(p)

    us = {k: v for k, v in places.items() if k.startswith("US-")}
    non_us = {k: v for k, v in places.items() if not k.startswith("US-")}

    print(f"{sum(len(v) for v in places.values())} phage PIs with "
          f">= {min_papers} papers.\n")

    print(f"UNITED STATES — {sum(len(v) for v in us.values())} across "
          f"{len(us)} states")
    for k in sorted(us, key=lambda k: (-len(us[k]), k)):
        group = sorted(us[k], key=lambda p: (-p.n, -p.latest))
        head = ", ".join(f"{p.name} ({p.n})" for p in group[:per_place])
        more = f" +{len(group) - per_place}" if len(group) > per_place else ""
        print(f"  {k[3:]:<4}{len(group):>3}   {head}{more}")

    print(f"\nELSEWHERE — {sum(len(v) for v in non_us.values())} across "
          f"{len(non_us)} countries")
    for k in sorted(non_us, key=lambda k: (-len(non_us[k]), k)):
        group = sorted(non_us[k], key=lambda p: (-p.n, -p.latest))
        head = ", ".join(f"{p.name} ({p.n})" for p in group[:per_place])
        more = f" +{len(group) - per_place}" if len(group) > per_place else ""
        print(f"  {k[:16]:<17}{len(group):>3}   {head}{more}")

    print(f"\n{unresolved} people have an affiliation this parser could not "
          f"place. That is an\nunread string, not a claim about where they are.")
    print("Use --region europe, --region Poland, --state CT, or --who NAME.")


def national(min_papers: int = 2, since: int = 0, per_state: int = 3) -> None:
    """The whole country, state by state.

    A single ranked table answers "who publishes most", which is not the
    question -- the top of any global ranking is the handful of famous labs you
    already know. Grouping by state answers "who is near anywhere I might
    move", and somebody relocating for a role needs the map, not the
    leaderboard.
    """
    found = [p for p in people(min_papers=min_papers, since=since) if p.state]
    if not found:
        print("No US-resolvable people stored. Run: python people.py --collect")
        return

    by_state: dict[str, list] = defaultdict(list)
    for p in found:
        by_state[p.state].append(p)

    print(f"{len(found)} phage PIs with >= {min_papers} papers, "
          f"across {len(by_state)} states.\n")
    for st in sorted(by_state, key=lambda s: (-len(by_state[s]), s)):
        group = sorted(by_state[st], key=lambda p: (-p.n, -p.latest))
        head = ", ".join(f"{p.name} ({p.n})" for p in group[:per_state])
        more = f" +{len(group) - per_state} more" if len(group) > per_state else ""
        print(f"  {st}  {len(group):>3}   {head}{more}")

    # The denominator, per PRINCIPLES: say what was checked, not only what was
    # found. A state with no phage lab and a state the parser could not read
    # look identical in a list of what exists.
    everyone = people(min_papers=min_papers, since=since)
    unresolved = len(everyone) - len(found)
    print(f"\n{len(everyone)} people total at this threshold; {len(found)} "
          f"resolved to a US state, {unresolved} are non-US or unresolvable.")
    silent = sorted(set(STATES.values()) - set(by_state))
    if silent:
        print(f"No phage PI found in: {', '.join(silent)}")
    print("\nUse --state XX to see one state in full, or --who NAME for a "
          "dossier and contact.")


def track_of(topic: str) -> str:
    """Which of the three tracks a stored paper came from."""
    if topic in set(SYNBIO_TOPICS):
        return "synbio"
    if topic in set(BASIC_TOPICS):
        return "basic"
    return "therapy"


# ── 'Find my PI': fold the funding channel into the PhD ranking ─────────────
#
# The defect this fixes is written, in plain words, in the note `--phd` itself
# writes: *"Whether they are taking students -- needs the funding join
# (grants.py has 8,965 NIH leads; NOT WIRED IN YET)."* Two channels held the
# answer between them and neither could see the other:
#
#   * the PAPER channel knows who publishes and where -- 1,167 PIs, 250 US --
#     but has no idea who holds money, and money is what pays a student.
#   * the GRANT channel knows who is funded -- 233 phage-titled federal PIs
#     absent from the paper corpus -- but surfaced only through a command
#     nobody runs often. MEASURED before writing this:
#     **161 of those 233 funded PIs share a surname with NOBODY in the paper
#     corpus at all.** They were not ranked low. They were absent.
#
# So the list grows without manual naming: a funded lab arrives on it because it
# is FUNDED, not because somebody typed its name in.
#
# MATCHING IS DELIBERATELY STRICT, because two facts must not share a label,
# and because of two false positives already paid for -- "ZHANG, YAN"
# matched a different J. Zhang in Hainan, "ZHOU, JIE" matched Z. H. Zhou at
# UCLA. Surname alone is not a person. So three facts must agree:
#
#   surname  +  given-name initial  +  a shared distinctive institution token
#
# A wrong match here is worse than a miss, because it labels the wrong lab
# "funded" and you get ONE first contact per lab.

_FUNDING_RECENT_FROM = 2022   # a grant older than this does not pay a student


def _grant_index(conn) -> dict[str, list[dict]]:
    """Phage-titled federal grants, bucketed by lowercase surname.

    `matching.PHAGE_WORD` is the gate, not `LIKE '%phage%'`: the SQL is only a
    cheap prefilter and on its own it returns MACROphage immunology, which is a
    different field. See the comment on that regex for why a lookbehind cannot
    do this.
    """
    from openfaculty import matching

    out: dict[str, list[dict]] = defaultdict(list)
    sql = ("SELECT pi_name, org_name, org_state, title, fiscal_year, amount "
           "FROM grant_leads WHERE title LIKE '%phage%'")
    for r in conn.execute(sql):
        title = r["title"] or ""
        if not matching.PHAGE_WORD.search(title):
            continue
        pi = (r["pi_name"] or "").strip()
        if "," not in pi:
            continue
        surname, _, given = pi.partition(",")
        surname, given = surname.strip(), given.strip()
        if not surname or not given:
            continue
        out[surname.lower()].append({
            "pi_name": pi,
            "initial": given[0].upper(),
            "org": r["org_name"] or "",
            "state": r["org_state"] or "",
            "title": title,
            "fy": r["fiscal_year"] or 0,
        })
    return out


def _person_surname_initial(name: str) -> tuple[str, str]:
    """"Bondy-Denomy J" -> ("bondy-denomy", "J"). ("", "") when unusable.

    Europe PMC writes `Surname Initials`, so the initials are the TAIL and a
    compound surname keeps its hyphen. A single token is not enough to match
    on and returns empty rather than guessing.
    """
    parts = _clean_author(name).split()
    if len(parts) < 2:
        return "", ""
    initials = parts[-1]
    if not initials[:1].isupper() or not initials.isalpha():
        return "", ""
    return " ".join(parts[:-1]).lower(), initials[0].upper()


def funding_for(person, index: dict[str, list[dict]]) -> dict | None:
    """The most recent phage grant belonging to this person, or None.

    Three facts must agree (surname, given initial, institution token). The
    institution check is what separates the two Zhangs, and it is done against
    the PI's own affiliation string rather than a guess.
    """
    from openfaculty import matching

    surname, initial = _person_surname_initial(person.name)
    if not surname:
        return None
    cands = index.get(surname)
    if not cands:
        return None
    aff = (person.affiliation or "").lower()
    best = None
    for g in cands:
        if g["initial"] != initial:
            continue                      # different person, same surname
        tokens = matching.org_tokens(g["org"])
        if not tokens or not any(t in aff for t in tokens):
            continue                      # same name, different institution
        if best is None or g["fy"] > best["fy"]:
            best = g
    return best


def funded_labs(min_papers: int = 3, region: str | None = "us"
                ) -> tuple[list, dict, list]:
    """`(people, name -> grant, funded-but-unpublished grant PIs)`.

    The third return value is the whole point and must never be dropped: it is
    the set of labs holding phage money that the PAPER channel cannot see at
    all. Reporting only the join would hide exactly the hole this was built to
    measure, because a joined list always looks complete.
    """
    found = people(min_papers=min_papers)
    if region:
        want = region.lower()
        if want == "us":
            found = [p for p in found if p.country == "US"]
        elif want in ("eu", "europe"):
            found = [p for p in found if p.country not in ("US", "")]
        else:
            found = [p for p in found if p.country.lower() == want]

    with store.connect() as conn:
        index = _grant_index(conn)
        grants = {}
        for p in found:
            g = funding_for(p, index)
            if g:
                grants[p.name] = g

        # The inverse: funded phage PIs with no paper-channel person at all.
        matched = {g["pi_name"] for g in grants.values()}
        unseen = [g for bucket in index.values() for g in bucket
                  if g["pi_name"] not in matched
                  and g["fy"] >= _FUNDING_RECENT_FROM]
        by_pi: dict[str, dict] = {}
        for g in unseen:
            cur = by_pi.get(g["pi_name"])
            if cur is None or g["fy"] > cur["fy"]:
                by_pi[g["pi_name"]] = g
        from openfaculty import matching
        contacts = {}
        for pi, g in by_pi.items():
            contacts[pi] = matching.contact_for(conn, pi, g["org"])
    for pi, g in by_pi.items():
        g["email"] = contacts.get(pi)
    unpublished = sorted(by_pi.values(),
                         key=lambda g: (-g["fy"], g["state"], g["pi_name"]))
    return found, grants, unpublished


def report_phd(limit: int = 50, min_papers: int = 3,
               region: str | None = "us", show_ghosts: bool = False) -> None:
    """Rank PIs for a PhD application, not for a job.

    2026-08-29. The job search asks "who might hire a Research Associate this
    month". A prospective PhD student asks a different question with a longer
    horizon: **whose lab will still be doing this science in five years?**

    So this view leads with trajectory rather than contactability:

      * a lab with no recent paper on the topic is MARKED (the Ghost guardrail)
      * a lab whose institution changed is MARKED, because writing to the old
        university is the whole cost of getting that wrong
      * recency and volume in the last five years drive the ordering

    Ghosts are hidden by default and shown with `--show-ghosts`, because the
    point of marking rather than deleting is that the decision stays visible.

    THIS RANKS; IT DOES NOT DECIDE. You know your own field, and you will know
    within ten names whether the ordering is sane. You are the verifier. The
    output is a list of people to read, not a score to trust.
    """
    found, grants, unpublished = funded_labs(min_papers=min_papers,
                                             region=region)
    if not found:
        print("No people stored. Run: python people.py --collect")
        return

    ghosts = [p for p in found if p.is_ghost()]
    active = [p for p in found if not p.is_ghost()]
    shown = (found if show_ghosts else active)

    # Funded first, THEN recent output. A PhD student is paid out of a grant,
    # so current money is a stronger signal about "can this lab take me" than
    # paper count -- and paper count was the only key until today.
    shown = sorted(shown, key=lambda p: (0 if p.name in grants else 1,
                                         -p.bucket_counts()[0], -p.latest))

    print(f"\n  PhD TARGETS - {len(active)} active labs"
          f"{f', {len(ghosts)} ghosts hidden' if ghosts and not show_ghosts else ''}")
    print(f"  ranked by papers in the last 5 years. `!` = changed institution, "
          f"`?` = no recent paper\n")
    print(f"  {'5yr':>4} {'TOT':>4} {'LAST':>5} {'$FY':>5}  {'NAME':<22} "
          f"{'WHERE':<34} FLAG")
    print("  " + "-" * 104)
    for p in shown[:limit]:
        r, _, _ = p.bucket_counts()
        moved, old_i, new_i = p.moved()
        flags = []
        if p.is_ghost():
            flags.append("? no paper since %d" % p.latest)
        if moved:
            flags.append(f"! was {old_i[:26]}")
        g = grants.get(p.name)
        print(f"  {r:>4} {p.n:>4} {p.latest:>5} {(g['fy'] if g else '-'):>5}  "
              f"{p.name[:21]:<22} "
              f"{p.institution[:33]:<34} {'; '.join(flags)}")

    print(f"\n  {len(found)} PIs matched, {len(shown)} shown, "
          f"{len(ghosts)} marked as ghosts.")
    print(f"  {len(grants)} of {len(found)} hold a phage-titled federal grant "
          f"(surname + initial + institution all agree).")
    # The reason this number is printed rather than hidden: a joined list
    # always looks complete. These are funded phage labs the paper
    # channel cannot see AT ALL, and they are the recall hole.
    print(f"  {len(unpublished)} MORE funded phage PIs are invisible to the "
          f"paper channel entirely —\n  see `python people.py --phd --funded`.")
    print("  A ghost is a lab with no recent paper on the topic AND enough "
          "papers to say so;\n  below that evidence floor the answer is "
          "'unknown', never 'departed'.")
    print("  Read the names yourself. This ranks; you decide.\n")


def report_funded_unseen(min_papers: int = 3, region: str | None = "us",
                         limit: int = 0, contactable: bool = False) -> None:
    """Funded phage labs the PAPER channel never found. The recall hole itself.

    The list grows because a lab holds phage money, not because a human typed
    its name. Measured on the day it was written: 128 such PIs, 13 with an
    address already confirmed.
    """
    found, grants, unpublished = funded_labs(min_papers=min_papers,
                                             region=region)
    rows = [g for g in unpublished if not contactable or g.get("email")]
    print(f"\n  FUNDED PHAGE LABS THE PAPER CHANNEL CANNOT SEE")
    print("  " + "=" * 68)
    print("  Each holds a phage-titled federal grant since "
          f"FY{_FUNDING_RECENT_FROM} and matches NO PI in the")
    print("  publication corpus. Nobody named them; the funding channel did.\n")
    for g in (rows[:limit] if limit else rows):
        print(f"  {g['pi_name']}   FY{g['fy']}")
        print(f"      {g['org'][:56]} — {g['state']}")
        print(f"      {g['title'][:74]}")
        if g.get("email"):
            print(f"      ✉ {g['email'][0]}  ({g['email'][1]})")
    n_mail = sum(1 for g in unpublished if g.get("email"))
    print(f"\n  {len(unpublished)} funded phage PIs unseen by the paper "
          f"channel; {n_mail} have a confirmed address.")
    print(f"  The paper channel holds {len(found)} PIs, of which {len(grants)} "
          f"are funded.")
    print("  A grant title is a weak signal and this is not filtered for fit.")
    print("  It is still a lab nobody had to name by hand.\n")


def write_phd_note(limit: int = 60, min_papers: int = 3,
                   region: str | None = "us") -> None:
    """Write the PhD target list to a Markdown file you will actually read.

    This exists because of a mistake worth naming. `--phd` first shipped as
    terminal output only, and the reaction was **"I could not find the list."**
    Of course not: a terminal is not where anyone reads a shortlist of sixty
    labs they want to think about over a week.

    **A result the reader cannot find has not been delivered.** So this writes
    `PIs - PhD Targets.md` into your notes directory (see `config.NOTES_DIR`),
    which is a file you can open in Obsidian, an editor, or anything else.
    """
    found, grants, unpublished = funded_labs(min_papers=min_papers,
                                             region=region)
    if not found:
        print("No people stored. Run: python people.py --collect")
        return

    ghosts = [p for p in found if p.is_ghost()]
    active = sorted((p for p in found if not p.is_ghost()),
                    key=lambda p: (0 if p.name in grants else 1,
                                   -p.bucket_counts()[0], -p.latest))
    n_funded_active = sum(1 for p in active if p.name in grants)

    ts = time.strftime("%Y-%m-%d %H:%M")
    out = [
        "---", "type: report", "source: openfaculty people.py",
        f"updated: {time.strftime('%Y-%m-%d')}", "---", "",
        "# PIs — PhD Targets", "",
        f"*Generated by `python people.py --phd --write` · {ts}. "
        f"Regenerate rather than editing: typed numbers rot.*", "",
        # REPORT THE DENOMINATOR, INCLUDING THE ONE THIS NOTE IMPOSES ON
        # ITSELF. Until 2026-09-09 the header said "472 active US
        # labs" and the table below it held SIXTY. Both numbers were true and
        # the page never reconciled them, so the honest reading -- "you are
        # looking at the top 13% of a list, ranked one way" -- was invisible,
        # and any judgement about the ORDERING was really a judgement about a
        # cut nobody mentioned.
        f"**{len(active)} active US labs**, from {len(found)} matched PIs. "
        f"Ranked by papers in the last five years, which is a proxy for "
        f"*will this lab still be doing this science when I finish*.", "",
        f"> [!info] You are seeing **{min(limit, len(active))} of "
        f"{len(active)}** — the top {100*min(limit,len(active))//max(len(active),1)}%, "
        f"and only by that one number.",
        f"> Rank is 5-year output. It is **not** fit, funding runway, or "
        f"whether they take students. `python people.py --phd --write "
        f"--limit N` shows more.",
        f">",
        f"> **It cannot see geography, and you are in New Haven.** Of the "
        f"{min(limit,len(active))} shown, only a handful are in the Northeast, "
        f"and Paul Turner is a 20-minute walk from you at rank "
        f"{next((i+1 for i,x in enumerate(active[:limit]) if 'Turner' in x.name), '-')}. "
        f"If proximity matters more than paper count, say so and it becomes a "
        f"second sort key rather than something you re-do by eye every time.", "",
        "> [!warning] This ranks. You decide.",
        "> The ordering cannot see funding runway, mentorship style, or "
        "whether you would like the person. You will know within ten names "
        "whether it is sane, and I will not. Where it is wrong, say so and it "
        "becomes a rule.", "",
        "## The list", "",
        f"**{n_funded_active} of the {len(active)} active labs hold a "
        f"phage-titled federal grant**, and those sort first. A PhD student is "
        f"paid out of a grant, so current money says more about *can this lab "
        f"take me* than paper count does — and until 2026-09-29 paper count "
        f"was the only sort key, because the two channels could not see each "
        f"other.", "",
        "| 5yr | Total | Latest | Grant | PI | Institution | Flag |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in active[:limit]:
        r, _, _ = p.bucket_counts()
        moved, old_i, _new_i = p.moved()
        flag = f"**moved** — was {old_i[:30]}" if moved else ""
        g = grants.get(p.name)
        money = f"**FY{g['fy']}**" if g else "—"
        out.append(f"| {r} | {p.n} | {p.latest} | {money} | **{p.name}** "
                   f"| {p.institution[:40]} | {flag} |")

    # THE HOLE ITSELF, on the page rather than in a terminal. A joined list
    # always looks complete, and these are the labs the paper channel cannot
    # see at all. They arrive here because they are FUNDED, with nobody naming
    # them.
    out += ["", f"## Funded labs the paper channel cannot see "
                f"({len(unpublished)})", "",
            "*Each holds a phage-titled federal grant since "
            f"FY{_FUNDING_RECENT_FROM} and matches **no PI** in the "
            "publication corpus. This is the recall hole, stated as names "
            "rather than as a number — and **nobody had to name them by "
            "hand**, which is the whole point. A grant title is a weak signal "
            "and this is not filtered for fit.*", ""]
    _unseen_shown = unpublished[:limit]
    if _unseen_shown:
        out += ["| FY | PI | Institution | State | Grant | Email |",
                "|---|---|---|---|---|---|"]
        for g in _unseen_shown:
            mail = g["email"][0] if g.get("email") else ""
            out.append(f"| {g['fy']} | **{g['pi_name']}** | {g['org'][:36]} "
                       f"| {g['state']} | {g['title'][:48]} | {mail} |")
        n_mail = sum(1 for g in unpublished if g.get("email"))
        out += ["", f"*Showing {len(_unseen_shown)} of {len(unpublished)}. "
                    f"{n_mail} of {len(unpublished)} have an address confirmed "
                    f"by surname + initial + affiliation. "
                    f"`python people.py --phd --funded` lists them all.*"]
    else:
        out.append("*None — which would mean the grant channel found nothing, "
                   "and that is an error rather than a clean sweep.*")

    out += ["", f"## Ghosts ({len(ghosts)})", "",
            "*No recent paper on the topic, **and** enough papers in our corpus "
            "to say so. Below that evidence floor the answer is 'unknown', "
            "never 'departed' — the filter must not mistake thin collection for "
            "a departed lab, which it did to Wommack and Martiny before the "
            "floor was added.*", ""]
    if ghosts:
        out += ["| Total | Last paper | PI | Institution |", "|---|---|---|---|"]
        for p in sorted(ghosts, key=lambda x: -x.n):
            out.append(f"| {p.n} | {p.latest} | {p.name} "
                       f"| {p.institution[:40]} |")
    else:
        out.append("*None.*")

    out += ["", "---", "",
            "## How to read this", "",
            "- **5yr** — papers on these topics in the last five years. The "
            "ordering column.",
            "- **moved** — the institution changed between their earliest and "
            "latest paper. Write to the new one.",
            "- A **ghost** is marked, never deleted. Demote, never disappear.", "",
            "## What this cannot tell you", "",
            "- Whether they are **taking students**. The funding join is now "
            "wired in (the `Grant` column, 2026-09-29), so a lab with current "
            "phage money is visible — but holding a grant is not the same as "
            "having an open slot, and only the PI can answer that.",
            "- Whether the lab is a **mentor or a paper mill**. Publication "
            "volume cannot tell those apart, and this does not try.",
            "- Whether you would **want to be there**. That was never a "
            "computable question.", "",
            f"*Corpus: {len(found)} PIs with {min_papers}+ papers. "
            f"The window is 2015–2026; a true 15-year trajectory needs "
            f"`--years 2011 2026`, which is hours of HTTP and has not been "
            f"run.*", ""]

    path = NOTES_DIR / "PIs - PhD Targets.md"
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"  wrote {path}")
    print(f"  {len(active)} active labs, {len(ghosts)} ghosts marked.")


def report(limit: int = 40, min_papers: int = 2, state: str | None = None,
           since: int = 0, emails: bool = False, track: str | None = None,
           region: str | None = None) -> None:
    with store.connect() as c:
        c.executescript(SCHEMA)
        total_papers = c.execute("SELECT COUNT(*) FROM people_papers").fetchone()[0]

    if not total_papers:
        print("No papers stored. Run: python people.py --collect")
        return

    found = people(min_papers=min_papers, state=state, since=since, track=track)
    if region:
        want = region.lower()
        if want in ("eu", "europe"):
            found = [p for p in found
                     if p.country not in ("US", "non-EU/US", "")]
        elif want == "us":
            found = [p for p in found if p.country == "US"]
        else:
            found = [p for p in found if p.country.lower() == want]
    everyone = people(min_papers=1)
    if not found:
        print(f"{total_papers} papers, {len(everyone)} distinct last authors, "
              f"but none match those filters.")
        return

    if emails:
        _fill_emails(found[:limit])

    print(f"{'PAPERS':<8}{'LATEST':<8}{'NAME':<26}{'WHERE':<34}CONTACT")
    print("-" * 118)
    for p in found[:limit]:
        contact = p.email or (f"ORCID {p.orcid}" if p.orcid else "—")
        print(f"{p.n:<8}{p.latest:<8}{p.name[:24]:<26}"
              f"{(p.institution or '?')[:32]:<34}{contact[:34]}")

    print(f"\n{len(found)} people with >= {min_papers} phage papers, "
          f"from {total_papers} papers by {len(everyone)} distinct last authors.")
    print("Last author = the PI whose lab it is. First authors (students and "
          "postdocs) are\nnot listed: the lab is the hiring unit.")
    print("\nNothing here has been contacted. The tool prepares; you send.")


def _fill_emails(group: list[Person]) -> None:
    """Look up corresponding emails for people who do not have one yet."""
    todo = [p for p in group if not p.email]
    if not todo:
        return
    print(f"looking up {len(todo)} corresponding address(es) ...", flush=True)
    with store.connect() as c:
        for p in todo:
            for paper in sorted(p.papers, key=lambda x: -(x["pub_year"] or 0)):
                if not paper["pmcid"]:
                    continue
                addr = fetch_email(paper["pmcid"], p.name)
                if addr:
                    c.execute("UPDATE people_papers SET email=? WHERE pmid=?",
                              (addr, paper["pmid"]))
                    paper_dict = dict(paper)
                    paper_dict["email"] = addr
                    p.papers = [paper_dict if x["pmid"] == paper["pmid"] else x
                                for x in p.papers]
                    break
                time.sleep(0.2)


def backfill_emails(min_papers: int = 3, region: str = "us",
                    limit: int = 400) -> None:
    """Resolve corresponding addresses in bulk, from open-access text only.

    Measured on a 25-PI sample: **64% resolve** from the paper alone. That is
    the cheap, legitimate route -- the address is printed on page one precisely
    so readers can write to the author -- and it is worth exhausting before
    anything scrapes a faculty page.

    Slow on purpose. One request per paper with a pause, because a public API
    that has never asked us for a key deserves not to be hammered.
    """
    group = [p for p in people(min_papers=min_papers)
             if (region == "all"
                 or (region == "us" and p.country == "US")
                 or p.country.lower() == region.lower())]
    todo = [p for p in group if not p.email][:limit]
    print(f"{len(group)} people at this threshold, {len(todo)} without an "
          f"address. Looking up ...\n")

    # One short transaction per address, NOT one across the whole sweep. This
    # loop is mostly waiting on HTTP, and holding the write lock while sleeping
    # between requests killed a concurrent `search.py` run outright. A writer
    # that is idle 99% of the time must not behave like a writer.
    found = 0
    for i, p in enumerate(todo, 1):
        for paper in sorted(p.papers, key=lambda x: -(x["pub_year"] or 0)):
            if not paper["pmcid"]:
                continue
            addr = fetch_email(paper["pmcid"], p.name)
            if addr:
                with store.connect() as c:
                    c.execute("UPDATE people_papers SET email=?, "
                              "email_source='open-access-full-text' "
                              "WHERE pmid=?", (addr, paper["pmid"]))
                found += 1
                print(f"  {found:>4}  {p.name[:24]:<26}{addr}")
                break
            time.sleep(0.15)
        if i % 50 == 0:
            print(f"       ... {i}/{len(todo)} checked, {found} found",
                  flush=True)

    print(f"\n{found} of {len(todo)} resolved = "
          f"{found / max(len(todo), 1):.0%} from open-access full text.")
    print("The rest have no open-access paper with a printed address. A faculty "
          "page is the\nnext route for those, and it is a different kind of "
          "lookup -- built 2026-08-30:\n\n    python faculty.py --dry-run\n\n"
          "It reads university pages rather than an API, so it refuses rather "
          "than guesses:\nan address is accepted only when the local part looks "
          "like the person, is not an\noffice, is the only candidate on the "
          "page, and the page's prose names them.")


def dossier(name: str) -> None:
    """Everything known about one person, for writing to them by hand."""
    match = [p for p in people(min_papers=1)
             if name.lower() in p.name.lower()]
    if not match:
        # Not a PI in this corpus -- but they may still be all over it. Rebekah
        # Dedrick is on 14 stored papers and last author on none, because she
        # is a senior scientist in Hatfull's lab rather than a PI. That is a
        # real and useful answer, and "not found" would have been a false one.
        _coauthor_note(name)
        return
    for p in match:
        _fill_emails([p])
        print("=" * 100)
        print(f"{p.name}   —   {p.n} phage paper(s), most recent {p.latest}")
        print(f"  {p.affiliation}")
        if p.orcid:
            print(f"  ORCID: {p.orcid}")
        if p.email:
            print(f"  Email: {p.email}   (printed on their own paper)")
        print(f"\n  Papers:")
        for paper in sorted(p.papers, key=lambda x: -(x["pub_year"] or 0))[:8]:
            doi = f"  doi:{paper['doi']}" if paper["doi"] else ""
            print(f"    {paper['pub_year']}  {paper['title'][:78]}{doi}")
        print("\n  Before writing: read the most recent paper. A message that "
              "names the actual\n  finding is the only kind worth sending, and "
              "the only kind this tool cannot write.")


def _coauthor_note(name: str) -> None:
    """Report someone who appears on papers but never as last author."""
    with store.connect() as c:
        c.executescript(SCHEMA)
        rows = c.execute(
            "SELECT title, pub_year, last_author, affiliation FROM people_papers "
            "WHERE authors LIKE ? ORDER BY pub_year DESC", (f"%{name}%",)).fetchall()
    if not rows:
        print(f"{name!r} does not appear on any stored phage paper.")
        print("Either they publish under a different name form, or the corpus "
              "needs a wider\n--years window. This is a coverage gap, not a "
              "statement about the person.")
        return

    print(f"{name} appears on {len(rows)} stored phage paper(s), "
          f"but is last author on none.")
    print("That usually means a senior scientist, staff scientist or postdoc "
          "inside someone\nelse's lab -- worth knowing, and a different kind of "
          "contact than a PI.\n")
    labs: dict[str, int] = defaultdict(int)
    for r in rows:
        labs[r["last_author"]] += 1
    print("  Publishes with:")
    for lab, n in sorted(labs.items(), key=lambda kv: -kv[1])[:6]:
        print(f"    {lab}  ({n} paper(s))")
    print("\n  Most recent:")
    for r in rows[:4]:
        print(f"    {r['pub_year']}  {r['title'][:74]}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Find phage biologists by their papers.")
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--pages", type=int, default=5, help="pages of 100 per topic")
    ap.add_argument("--years", type=int, nargs=2, default=[2022, 2026],
                    metavar=("FROM", "TO"))
    ap.add_argument("--min-papers", type=int, default=2,
                    help="a single phage paper may be a collaboration; 2+ is a lab")
    ap.add_argument("--state", help="filter by US state, e.g. CT")
    ap.add_argument("--since", type=int, default=0, help="active since this year")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--emails", action="store_true",
                    help="look up corresponding addresses from open-access texts")
    ap.add_argument("--who", help="full dossier on one person")
    ap.add_argument("--backfill-emails", action="store_true",
                    help="resolve addresses in bulk from open-access text")
    ap.add_argument("--national", action="store_true",
                    help="the US, grouped by state")
    ap.add_argument("--world", action="store_true",
                    help="every country and US state in one map")
    ap.add_argument("--region", help="us | europe | a country name, e.g. Poland")
    ap.add_argument("--phd", action="store_true",
                    help="rank PIs for a PhD application: trajectory first, "
                         "ghosts and relocations marked")
    ap.add_argument("--write", action="store_true",
                    help="with --phd, write the list to "
                         "'PIs - PhD Targets.md' in your notes directory "
                         "instead of printing it")
    ap.add_argument("--show-ghosts", action="store_true",
                    help="with --phd, include labs that left the field")
    ap.add_argument("--funded", action="store_true",
                    help="with --phd, list ONLY the funded phage labs the "
                         "publication channel cannot see — the recall hole, "
                         "as names nobody had to type by hand")
    ap.add_argument("--contactable", action="store_true",
                    help="with --phd --funded, only PIs with a confirmed "
                         "address")
    ap.add_argument("--topic", action="append", metavar="QUERY",
                    help="search your own field instead of phage biology; "
                         "repeatable, e.g. --topic 'chromatin remodeling' "
                         "--topic 'nucleosome positioning'. Overrides --track.")
    ap.add_argument("--track", default="all",
                    choices=["all", "therapy", "synbio", "basic"],
                    help="therapy = phage as medicine; synbio = phage parts as "
                         "tools (PACE, integrases, T7, M13); basic = genomes, "
                         "capsids, host range, lysis-lysogeny")
    ap.add_argument("--all-countries", action="store_true",
                    help="include non-US institutions (default: US only)")
    ap.add_argument("--lab-members", action="store_true",
                    help="senior/staff scientists and postdocs, never last "
                         "author - the easiest people to ask")
    ap.add_argument("--per-state", type=int, default=3,
                    help="names shown per state in the national view")
    args = ap.parse_args()

    if args.collect:
        topics = None
        if args.topic:
            # A caller naming their own field. `--topic` may be repeated.
            topics = list(args.topic)
            print(f"Europe PMC: {len(topics)} caller-supplied "
                  f"{'query' if len(topics) == 1 else 'queries'}\n")
        else:
            print("Europe PMC: phage literature\n")
        seen, new = collect(years=tuple(args.years), pages=args.pages,
                            track=args.track, topics=topics)
        print(f"\n{seen} papers examined, {new} new.\n")
    if args.phd:
        if args.funded:
            report_funded_unseen(min_papers=args.min_papers,
                                 region=(args.region or "us"),
                                 limit=args.limit,
                                 contactable=args.contactable)
        elif args.write:
            write_phd_note(limit=max(args.limit, 60),
                           min_papers=args.min_papers,
                           region=(args.region or "us"))
        else:
            report_phd(limit=args.limit, min_papers=args.min_papers,
                       region=(args.region or "us"),
                       show_ghosts=args.show_ghosts)
        return 0
    if args.backfill_emails:
        # Free pass first: parse addresses already sitting in stored
        # affiliation strings. Whatever it cannot resolve is still available
        # to the HTTP path below, so this only ever removes work.
        print("Recovering addresses from stored affiliations (no network) ...")
        backfill_emails_from_affiliations()
        backfill_emails(min_papers=args.min_papers,
                        region=(args.region or "us"), limit=args.limit)
        return 0
    if args.who:
        dossier(args.who)
        return 0
    if args.lab_members:
        report_lab_members(min_papers=args.min_papers, state=args.state,
                           since=args.since, limit=args.limit,
                           us_only=not args.all_countries, region=args.region)
        return 0
    if args.world:
        world(min_papers=args.min_papers, since=args.since,
              per_place=args.per_state)
        return 0
    if args.national:
        national(min_papers=args.min_papers, since=args.since,
                 per_state=args.per_state)
        return 0
    report(limit=args.limit, min_papers=args.min_papers, state=args.state,
           since=args.since, emails=args.emails,
           track=args.track if args.track != "all" else None,
           region=args.region)
    return 0


if __name__ == "__main__":
    sys.exit(main())

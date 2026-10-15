#!/usr/bin/env python3
"""
faculty.py - the second route to a PI's address, for the 31% papers cannot reach.

WHY THIS EXISTS
---------------
If contact details cannot be taken from the papers themselves, the other place
they exist is the lab's own faculty page.

`people.py --backfill-emails` reads the address the publisher prints on page
one of an open-access paper. Measured on the live corpus, that resolves **178
of 245 US PIs (73%)**. The remaining 67 have no open-access paper carrying a
printed address, and no amount of re-reading the literature will produce one.

This module is the other route, and it is a **different kind of tool**: it
fetches a university web page rather than a structured API. Nothing about it
can be trusted the way an API response can, so the whole design is about
refusing.

THE FAILURE MODE THIS IS BUILT AGAINST
--------------------------------------
**A directory page listing forty people is exactly where an address gets
attached to the wrong one.**

That is the same defect `fetch_email()` already paid for once -- taking the
first address in a full text returned `jyot.antani@yale.edu` for a Paul Turner
paper, the first author's address rather than the PI's. An email is the one
field here where **being wrong is worse than being empty**: a message to the
wrong scientist, about their colleague's work, is not a neutral failure.

So there are four gates, and an address must pass all of them:

1. **The local part must look like this person.** `hatfull`, or an initials
   form (`gfh`, `ghatfull`, `hatfullg`, `g.hatfull`). Measured necessity: the
   paper rule -- surname must appear in the local part -- **fails on faculty
   pages**. Graham Hatfull's real published address on his own lab site is
   `gfh@pitt.edu`, which contains no surname at all.

2. **Role addresses are refused outright.** The first live run of this scraper
   accepted `church_lab_admin@hms.harvard.edu` for George Church, because
   `church` is a substring of `church_lab_admin`. That is a lab administrator,
   not the PI. Every rule in `_ROLE_LOCAL` was added because a real page
   offered one.

3. **An ambiguous page yields nothing.** If two or more distinct addresses on
   one page match the person's name forms, this refuses rather than picking.
   Two Chens in one department is not a puzzle to be solved by ranking.

4. **The page must actually name the person.** A surname in an address, on a
   page that never writes the surname in its text, is a coincidence.

WHERE THE CANDIDATE PAGES COME FROM
-----------------------------------
**ORCID, not a search engine.** The first prototype scraped DuckDuckGo HTML for
`"<surname>" <institution> faculty profile email`. It worked for about thirty
requests and was then served a CAPTCHA -- so a route built on it would have
appeared to work in testing and quietly returned zeros in production, which is
the exact shape of the defect that let an earlier version emit nothing for two
months.

ORCID is the honest source instead: a public API, no key, and the URLs on a
record are **self-asserted by the scientist**. 50 of the 67 unresolved PIs
carry an ORCID in the corpus already, because Europe PMC supplies it.

ORCID also serves a verified email outright for a few people, which needs no
scraping at all and is checked first.

WHAT IT DOES NOT DO
-------------------
**It never contacts anyone.** Same prohibition as `people.py`, and
`test_cannot_send.py` checks this file too. An address printed next to a name is
a lookup result, exactly as if you had opened the page yourself.

It also does not follow a page off its own host, does not render JavaScript,
honours `robots.txt`, identifies itself, rate-limits per host, and caches every
fetch so that a second run costs nothing.

    python faculty.py --dry-run          # what it would try, no writes
    python faculty.py                    # resolve and store
    python faculty.py --who "Hatfull"    # one person, verbosely
"""

from __future__ import annotations

import argparse
import html as _html
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

from openfaculty import people, store
from openfaculty.config import CACHE_DIR

ORCID_API = "https://pub.orcid.org/v3.0"

# Identify honestly to universities: a sysadmin reading an access log should be
# able to tell what this is, and that it is one person looking up labs rather
# than a harvester.
#
# The string lives in `config.py`, with the reasoning for its `+url`, because
# every module needs it and `config` imports nothing local -- declaring it here
# once caused a circular import that fired on one import order only.
from openfaculty.config import POLITE_AGENT

PER_HOST_DELAY = 1.5     # seconds between two requests to the SAME host
FETCH_TIMEOUT = 20
MAX_PAGES_PER_PERSON = 8

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# A link worth following one hop from a lab's front page. A PI's address is
# almost never on the landing page; it is on "Contact" or "People".
_CONTACT_LINK_RE = re.compile(
    r"contact|about|people|team|members|lab|staff|directory|profile|faculty",
    re.I)

# Local parts that belong to an OFFICE, not a person. Two tiers, because the
# evidence differs.
#
# Tier 1, matched ANYWHERE in the local part: unambiguous office words. This
# tier exists because `church_lab_admin@hms.harvard.edu` was accepted for
# George Church on the first live run -- `church` really is a substring of it.
_ROLE_SUBSTRING = re.compile(
    r"admin|info@|^info|info$|webmaster|noreply|no-reply|donotreply|"
    r"contact|office|support|inquir|enquir|secretar|helpdesk|postmaster|"
    r"listserv|mailer", re.I)

# Tier 2, matched only at a token boundary, because these words also occur
# inside real surnames. `lab` is here rather than in tier 1 so that a person
# named Labrie is not refused, while `turnerlab@` and `young.lab@` are.
_ROLE_TOKEN = re.compile(
    r"(?:^|[._\-])(?:lab|labs|dept|department|grad|graduate|apply|"
    r"recruit|hr|editor|press|media|news|help|general|staff|team|group|"
    r"seminar|events|jobs|alumni|giving|reception)(?:$|[._\-])", re.I)


class Refused(Exception):
    """A candidate address was rejected, with the reason. Never a silent drop."""


# ── polite, cached HTTP ───────────────────────────────────────────────────────

_HTTP_CACHE = CACHE_DIR / "faculty_pages.db"
_last_hit: dict[str, float] = {}
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def _cache():
    conn = sqlite3.connect(_HTTP_CACHE, timeout=30.0)
    conn.execute("""CREATE TABLE IF NOT EXISTS pages (
                        url TEXT PRIMARY KEY,
                        body TEXT,
                        status TEXT,
                        fetched_at TEXT)""")
    return conn


def _sleep_for(host: str) -> None:
    last = _last_hit.get(host)
    if last is not None:
        wait = PER_HOST_DELAY - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
    _last_hit[host] = time.time()


def allowed(url: str) -> bool:
    """Does this host's robots.txt permit us? A fetch failure means yes.

    A missing or unreadable robots.txt is not a prohibition -- RFC 9309 says an
    unreachable robots.txt with a 4xx means unrestricted -- but a robots.txt we
    successfully read and which says no is honoured absolutely.
    """
    parts = urllib.parse.urlparse(url)
    host = f"{parts.scheme}://{parts.netloc}"
    if host not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{host}/robots.txt")
        try:
            _sleep_for(parts.netloc)
            req = urllib.request.Request(
                f"{host}/robots.txt", headers={"User-Agent": POLITE_AGENT})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
                rp.parse(r.read().decode("utf-8", "replace").splitlines())
            _robots[host] = rp
        except Exception:
            _robots[host] = None            # could not read: not a prohibition
    rp = _robots[host]
    if rp is None:
        return True
    return rp.can_fetch(POLITE_AGENT, url)


def fetch(url: str, use_cache: bool = True) -> str:
    """One page, cached, rate-limited per host, robots-honouring.

    Returns "" on any failure. The caller distinguishes *fetched and found
    nothing* from *could not fetch* by the `notes` it accumulates --
    #1 and #17: two facts must not share a label.
    """
    if use_cache:
        with _cache() as c:
            row = c.execute("SELECT body, status FROM pages WHERE url=?",
                            (url,)).fetchone()
        if row is not None:
            return row[0] or ""

    if not allowed(url):
        with _cache() as c:
            c.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?,?)",
                      (url, "", "robots-disallowed", store.now()))
        return ""

    parts = urllib.parse.urlparse(url)
    body, status = "", "ok"
    try:
        _sleep_for(parts.netloc)
        req = urllib.request.Request(url, headers={"User-Agent": POLITE_AGENT})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "html" not in ctype and "text" not in ctype:
                status = f"skipped-content-type:{ctype[:40]}"
            else:
                body = r.read(3_000_000).decode("utf-8", "replace")
    except Exception as exc:                # noqa: BLE001 - every failure is data
        status = f"error:{type(exc).__name__}:{str(exc)[:80]}"

    with _cache() as c:
        c.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?,?)",
                  (url, body, status, store.now()))
    return body


# ── the acceptance rule ───────────────────────────────────────────────────────

def name_forms(person_name: str) -> tuple[str, set[str]]:
    """Surname, and the local parts a university actually issues for it.

    Europe PMC writes `Surname Initials`: `Hatfull GF`. Universities issue
    every one of these and this had to be measured rather than guessed --
    Hatfull's own lab site publishes `gfh@pitt.edu`, which the paper rule
    (surname must appear in the local part) rejects outright.
    """
    parts = (person_name or "").split()
    if not parts:
        return "", set()
    surname = parts[0].lower()
    surname = re.sub(r"[^a-z\-]", "", surname)
    initials = re.sub(r"[^a-z]", "", parts[1].lower()) if len(parts) > 1 else ""

    forms = {surname, surname.replace("-", "")}
    if initials:
        f, s = initials[0], surname.replace("-", "")
        forms |= {f + s, s + f, f + "." + s, f + "_" + s, f + "-" + s,
                  initials + s, s + initials, initials + s[0], initials}
        if len(initials) > 1:
            forms.add(initials[0] + "." + initials[1] + "." + s)
    # TRUNCATED surnames, because universities cap the local part at eight
    # characters. Measured: Barry Stoddard's real address on his own Fred Hutch
    # contact page is `bstoddar@fredhutch.org` -- initial plus the first SEVEN
    # letters of `stoddard`. Without this the tool refused a correct address it
    # had already fetched, which is a recall bug, not caution.
    #
    # Kept deliberately tight so it cannot start guessing: the prefix must be
    # at least six characters, so `bstoddar` is reachable and `chen`, `lee`,
    # `zhang` and every other short surname gain nothing at all. A six-letter
    # prefix of a surname is not a coincidence anyone has to worry about.
    stem = surname.replace("-", "")
    if len(stem) > 6:
        for cut in range(6, len(stem)):
            prefix = stem[:cut]
            forms.add(prefix)
            if initials:
                forms |= {initials[0] + prefix, prefix + initials[0]}

    # A two-letter form is not evidence of anything. `gfh` (three) is the
    # shortest thing anyone should accept, and only because it is a full
    # initials+surname-initial string.
    return surname, {x for x in forms if len(x) >= 3}


def is_role_address(local: str) -> bool:
    """Is this local part an office rather than a person?"""
    return bool(_ROLE_SUBSTRING.search(local) or _ROLE_TOKEN.search(local))


def _deobfuscate(text: str) -> str:
    """`name [at] uni [dot] edu` -> `name@uni.edu`. Only the common spellings."""
    text = _html.unescape(text)
    text = re.sub(r"\s*(?:\[at\]|\(at\)|&#0*64;|\s+at\s+)\s*", "@", text)
    return re.sub(r"\s*(?:\[dot\]|\(dot\))\s*", ".", text, flags=re.I)


def _visible_text(html_doc: str) -> str:
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_doc,
                  flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", body)


def address_on_page(html_doc: str, person_name: str) -> str:
    """The one address on this page that belongs to this person, or "".

    Raises nothing; refusal is an empty string, and the reason is available
    from `address_on_page.reason`. Four gates, all of which must pass -- see the
    module docstring for what each one cost.
    """
    address_on_page.reason = "no-candidate"
    surname, forms = name_forms(person_name)
    if not surname or len(surname) <= 2:
        address_on_page.reason = "name-too-short-to-verify"
        return ""
    if not html_doc:
        address_on_page.reason = "empty-page"
        return ""

    text = _deobfuscate(html_doc)

    # GATE 4 first, because it is free: does this page name the person at all?
    #
    # The addresses must be STRIPPED OUT before this check, and the first
    # version of this function did not do that. `heymann@example.org` on a
    # departmental page that never writes "Heymann" in its prose satisfied the
    # gate using the address itself as the evidence for the address -- the gate
    # was checking its own conclusion. It has to be the surrounding prose that
    # names the person, which is the whole point of asking.
    prose = _EMAIL_RE.sub(" ", _visible_text(text)).lower()
    if surname not in prose:
        address_on_page.reason = "page-never-names-this-person"
        return ""

    matched: set[str] = set()
    role_rejected: set[str] = set()
    for raw in _EMAIL_RE.findall(text):
        addr = raw.rstrip(".,;:)'\"").lower()
        local, _, domain = addr.partition("@")
        # Placeholders that live in template markup.
        if domain in {"domain.com", "example.com", "email.com", "sentry.io"}:
            continue
        # GATE 1: does the local part look like this person?
        if not (surname.replace("-", "") in local or local in forms):
            continue
        # GATE 2: an office is not a person.
        if is_role_address(local):
            role_rejected.add(addr)
            continue
        matched.add(addr)

    # GATE 3: ambiguity is refused, never ranked.
    if len(matched) > 1:
        address_on_page.reason = f"ambiguous:{len(matched)}-candidates"
        return ""
    if not matched:
        address_on_page.reason = (f"role-address-only:{sorted(role_rejected)[0]}"
                                  if role_rejected else "no-candidate")
        return ""
    address_on_page.reason = "accepted"
    return matched.pop()


address_on_page.reason = ""


# ── candidate pages, from ORCID ───────────────────────────────────────────────

def _orcid(path: str) -> dict:
    req = urllib.request.Request(
        f"{ORCID_API}/{path}",
        headers={"Accept": "application/json", "User-Agent": POLITE_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        return json.load(r)


def orcid_email(orcid: str) -> str:
    """A verified address the scientist published on their own ORCID record.

    No scraping and no guessing: they typed it in and ORCID verified it. Yields
    3 of the 67 unresolved PIs, which is small and completely trustworthy, so
    it runs before anything fetches a page.
    """
    if not orcid:
        return ""
    try:
        entries = _orcid(f"{orcid}/email").get("email") or []
    except Exception:
        return ""
    for e in entries:
        addr = (e.get("email") or "").strip().lower()
        if addr and e.get("visibility") == "public":
            return addr
    return ""


def orcid_urls(orcid: str) -> list[str]:
    """The URLs a scientist put on their own ORCID record. Self-asserted."""
    if not orcid:
        return []
    try:
        entries = _orcid(f"{orcid}/researcher-urls").get("researcher-url") or []
    except Exception:
        return []
    out = []
    for e in entries:
        url = ((e.get("url") or {}).get("value") or "").strip()
        if url.startswith(("http://", "https://")):
            out.append(url)
    return out


# Hosts that are not faculty pages and never carry a scrapable address.
_SKIP_HOST = re.compile(
    r"linkedin\.|twitter\.|x\.com|facebook\.|researchgate\.|scholar\.google|"
    r"orcid\.org|youtube\.|github\.io/?$|bsky\.|mastodon", re.I)


def contact_links(html_doc: str, root: str) -> list[str]:
    """Same-host links from a lab front page that plausibly carry an address."""
    root_host = urllib.parse.urlparse(root).netloc
    out: list[str] = []
    seen = set()
    for m in re.finditer(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>',
                         html_doc, re.S | re.I):
        href, label = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        if not (_CONTACT_LINK_RE.search(label) or _CONTACT_LINK_RE.search(href)):
            continue
        url = urllib.parse.urljoin(root, href)
        if urllib.parse.urlparse(url).netloc != root_host:
            continue          # never leave the host we were pointed at
        if url in seen or url.rstrip("/") == root.rstrip("/"):
            continue
        seen.add(url)
        out.append(url)
    return out


def find_address(person, verbose: bool = False) -> tuple[str, str, str]:
    """(address, source_url, note) for one Person. Empty address is normal.

    `note` always says which of the two facts happened: found nothing, or could
    not look: an empty result is an error, not a finding.
    """
    if not person.orcid:
        return "", "", "no-orcid-in-corpus"

    addr = orcid_email(person.orcid)
    if addr:
        return addr, f"https://orcid.org/{person.orcid}", "orcid-verified-email"

    roots = [u for u in orcid_urls(person.orcid) if not _SKIP_HOST.search(u)]
    if not roots:
        return "", "", "no-usable-orcid-url"

    reasons: list[str] = []
    fetched = 0
    for root in roots[:3]:
        page = fetch(root)
        fetched += 1
        if page:
            got = address_on_page(page, person.name)
            if verbose:
                print(f"      {root[:64]:<66} {address_on_page.reason}")
            if got:
                return got, root, "faculty-page"
            reasons.append(address_on_page.reason)
        else:
            reasons.append("could-not-fetch")

        for link in contact_links(page, root):
            if fetched >= MAX_PAGES_PER_PERSON:
                break
            sub = fetch(link)
            fetched += 1
            if not sub:
                reasons.append("could-not-fetch")
                continue
            got = address_on_page(sub, person.name)
            if verbose:
                print(f"      {link[:64]:<66} {address_on_page.reason}")
            if got:
                return got, link, "faculty-page"
            reasons.append(address_on_page.reason)
        if fetched >= MAX_PAGES_PER_PERSON:
            break

    if reasons and all(r == "could-not-fetch" for r in reasons):
        return "", "", f"could-not-look ({fetched} page(s) all failed)"
    top = max(set(reasons), key=reasons.count) if reasons else "no-pages"
    return "", "", f"looked-and-refused ({fetched} page(s); {top})"


# ── the sweep ─────────────────────────────────────────────────────────────────

def unresolved(min_papers: int = 3, region: str = "us") -> list:
    group = [p for p in people.people(min_papers=min_papers)
             if (region == "all"
                 or (region == "us" and p.country == "US")
                 or p.country.lower() == region.lower())]
    return group, [p for p in group if not p.email]


def backfill(min_papers: int = 3, region: str = "us", limit: int = 200,
             dry_run: bool = False, verbose: bool = False) -> int:
    """Resolve addresses from faculty pages for everyone papers could not reach.

    Reports the denominator, and for every person whether it looked and
    refused or could not look at all. Those are different facts.
    """
    group, todo = unresolved(min_papers, region)
    todo = todo[:limit]
    if not group:
        raise SystemExit(
            "EMPTY YIELD: no people at this threshold. That is a broken corpus "
            "or a broken filter, not a zero -- run `people.py --collect` and "
            "check the database before trusting this.")

    print(f"{len(group)} PI(s) at min_papers={min_papers}, region={region}.")
    print(f"{len(group) - len(todo)} already have an address from a paper; "
          f"{len(todo)} do not.")
    print("Faculty pages are the second route, and this refuses rather than "
          "guesses.\n")

    found = 0
    notes: dict[str, int] = {}
    for i, p in enumerate(todo, 1):
        if verbose:
            print(f"  [{i}/{len(todo)}] {p.name} ({p.institution[:40]})")
        addr, src, note = find_address(p, verbose=verbose)
        key = note.split(" (")[0]
        notes[key] = notes.get(key, 0) + 1
        if not addr:
            continue
        found += 1
        print(f"  {found:>3}  {p.name[:22]:<24}{addr:<34}{note}")
        print(f"       {src[:96]}")
        if dry_run:
            continue
        # One short transaction per address, never one across the sweep. This
        # loop is mostly waiting on HTTP, and holding the write lock while
        # sleeping killed a concurrent fetch once already.
        newest = max(p.papers, key=lambda x: (x["pub_year"] or 0))
        with store.connect() as c:
            # `email_source` is not decoration. A paper's address must contain
            # the surname and the corpus asserts that; an ORCID-verified one is
            # whatever the scientist typed -- `tami@mit.edu` is correct and can
            # never pass a surname rule. The invariant is scoped by provenance
            # rather than weakened for everyone.
            c.execute("UPDATE people_papers SET email=?, email_source=? "
                      "WHERE pmid=?", (addr, note, newest["pmid"]))

    print(f"\n{found} of {len(todo)} resolved from faculty pages = "
          f"{found / max(len(todo), 1):.0%}.")
    print("Why the rest returned nothing:")
    for k, n in sorted(notes.items(), key=lambda kv: -kv[1]):
        print(f"    {n:>4}  {k}")
    print("\n'looked-and-refused' is a RESULT, not a failure: the page was read "
          "and carried\nno address this tool could attribute with confidence. "
          "'could-not-look' is the\nfailure, and is counted separately on "
          "purpose.")
    if dry_run:
        print("\nDRY RUN: nothing was written to the database.")
    return found


def who(name_fragment: str) -> int:
    """One person, verbosely. The debugging view."""
    _, todo = unresolved(min_papers=1, region="all")
    match = [p for p in todo if name_fragment.lower() in p.name.lower()]
    if not match:
        print(f"No PI matching {name_fragment!r} is missing an address.")
        print("Either they are not in the corpus, or a paper already resolved "
              "them --\ntry `python people.py --who \"" + name_fragment + "\"`.")
        return 1
    for p in match[:3]:
        print(f"\n{p.name} - {p.institution}")
        print(f"  ORCID: {p.orcid or '(none in corpus)'}")
        addr, src, note = find_address(p, verbose=True)
        print(f"  -> {addr or '(refused)'}  [{note}]")
        if src:
            print(f"     {src}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Faculty-page lookup for PIs no open-access paper resolved.")
    ap.add_argument("--min-papers", type=int, default=3)
    ap.add_argument("--region", default="us", help="us | all | a country name")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true",
                    help="look everything up, write nothing")
    ap.add_argument("--verbose", action="store_true",
                    help="print every page tried and why it was refused")
    ap.add_argument("--who", help="one person, verbosely")
    args = ap.parse_args()

    if args.who:
        return who(args.who)
    backfill(min_papers=args.min_papers, region=args.region, limit=args.limit,
             dry_run=args.dry_run, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())

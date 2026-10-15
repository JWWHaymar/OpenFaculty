#!/usr/bin/env python3
"""
matching.py - deciding whether two records name the SAME PERSON.

THE PROBLEM THIS OWNS
---------------------
NIH RePORTER writes `STEVENSON, BRIAN`. Europe PMC writes `Yamaki S`. NSF
writes `Brian Stevenson`. **None of them publishes an identifier the others
use**, so joining a grant to a paper means joining on a name, and a name is a
famously bad key:

    ('lee', 's')     -> a grant at Duke, a paper from KAIST in Korea.
    ('liu', 'z')     -> a grant at Arizona, a paper from Hainan University.
    ('johnson', 'r') -> grants at TWO institutions, a paper at a third.

A wrong match is worse than a miss here, and not by a little. A miss costs one
lookup. A wrong match puts a stranger's address beside somebody else's research
and invites you to write to them about work they have never done, which is the
one failure in a tool like this that cannot be taken back.

So everything in this file is written to REFUSE. Each function returns an empty
result rather than a guess, and the caller must handle that rather than receive
a plausible-looking wrong answer.

WHY IT IS ITS OWN MODULE
------------------------
This logic was originally spread across three files, which disagreed about
where a surname ending in a particle stopped: "VAN TYNE, DARIA" and
"Van Tyne D" were keyed differently, and **23 funded PIs silently vanished**
from a shortlist. One module, one answer.
"""

from __future__ import annotations

import re
import sqlite3

# ── "phage" AS A WORD, AND NOT AS THE TAIL OF A CELL TYPE ───────────────────
#
# A SQL `LIKE '%phage%'` prefilter is cheap and necessary, and on its own it
# returns MACROPHAGE immunology -- two of the first five hits on the first run
# that used it. Macrophage work is not merely off-target for a phage search; it
# is a different field entirely, and surfacing it as a phage lab is the error
# that wastes the reader's time.
#
# A lookbehind does not work, because in `\w*phage` the `\w*` swallows "macro"
# before the lookbehind is ever consulted. So: match the whole word, then reject
# on its PREFIX. An allowlist of virological prefixes lives in `concepts.py`
# for scoring; this is the cheap gate used while querying.
_PHAGE_TOKEN = re.compile(r"\b(\w*?phages?)\b", re.I)
_EUK_PREFIX = re.compile(
    r"^(macro|lympho|mono|mega|entero|osteo|hemo|haemo|xeno|auto)", re.I)


class _PhageWord:
    """`.search(text)` -> the matched token, or None for a false friend."""

    @staticmethod
    def search(text: str):
        for tok in _PHAGE_TOKEN.findall(text or ""):
            if not _EUK_PREFIX.match(tok):
                return tok
        return None


PHAGE_WORD = _PhageWord()


# ── INSTITUTIONS ────────────────────────────────────────────────────────────
# Words that appear in an institution's name and identify nothing. Matching on
# one of these would corroborate everything: "Caltech" and "Tianjin University
# of Technology" share *technology*, and the first version of this check passed
# them as one person.
_ORG_STOPWORDS = {
    "university", "of", "the", "college", "medicine", "school", "institute",
    "center", "centre", "at", "research", "health", "system", "inc", "and",
    "for", "medical", "corporation", "foundation", "state", "hospital",
}


def org_tokens(org: str) -> list[str]:
    """"YALE UNIVERSITY" -> ["yale"]. The distinctive words only."""
    return [w for w in re.findall(r"[a-z]+", (org or "").lower())
            if w not in _ORG_STOPWORDS and len(w) > 2]


# ── PEOPLE ──────────────────────────────────────────────────────────────────
def surname_initial(pi_name: str) -> tuple[str, str]:
    """`'STEVENSON, BRIAN'` -> `('stevenson', 'B')`. `('', '')` on refusal.

    Refuses rather than guesses when there is no given name to take an initial
    from, because the surname alone is not a person: a key of `('wang', '')`
    would match every Wang in the corpus.
    """
    pi = (pi_name or "").strip()
    if "," not in pi:
        return "", ""
    surname, _, given = pi.partition(",")
    surname, given = surname.strip(), given.strip()
    if not surname or not given:
        return "", ""
    return surname.lower(), given[0].upper()


def contact_for(conn, pi_name: str, org: str) -> tuple[str, str] | None:
    """An address for a grant PI, from the paper corpus. `(address, source)`.

    Written because a first attempt reported "no verified email for Jun Liu".
    There are 166 last authors named Liu in the corpus, so a surname search
    looked hopeless -- and it was not: filtering on AFFILIATION resolves him
    instantly to `jliu@yale.edu`. The people channel looked empty only because
    the query was lazy.

    THREE facts must agree, and the middle one is not enough alone. Surname
    plus affiliation produced two false positives on the first run, each of
    which would have sent a letter to a stranger:

      * grant PI "ZHANG, YAN" matched `jzhang@muhn.edu.cn` -- a different
        J. Zhang at Hainan Medical University.
      * grant PI "ZHOU, JIE" matched `hong.zhou@ucla.edu` -- Z. H. Zhou.

    Both share a surname and a plausible institution string and are simply
    different people. So the GIVEN NAME's initial is checked against the
    paper's trailing initials block as well.

    Deliberately conservative. A missed address costs one lookup; a wrong one
    costs a misaddressed letter.
    """
    surname, initial = surname_initial(pi_name)
    if not surname:
        return None
    tokens = org_tokens(org)
    if not tokens:
        # No distinctive institution word means the third check cannot run, and
        # two of three is exactly what produced the false positives above.
        return None

    try:
        rows = conn.execute(
            "SELECT last_author, affiliation, email, email_source "
            "FROM people_papers "
            "WHERE last_author LIKE ? AND email IS NOT NULL AND email != ''",
            (f"{surname.title()} %",)).fetchall()
    except sqlite3.Error:
        # The paper table may not exist yet, which is a legitimate state and
        # not an error: `grants.py` can be run before `people.py`.
        return None

    for r in rows:
        author = (r["last_author"] or "").strip()
        parts = author.split()
        # Europe PMC writes "Surname Initials"; the initials are the tail.
        if len(parts) < 2 or not parts[-1][:1].isupper():
            continue
        if parts[-1][0].upper() != initial:
            continue                      # different person, same surname
        aff = (r["affiliation"] or "").lower()
        if any(t in aff for t in tokens):
            return (r["email"] or "").strip(), (r["email_source"] or "").strip()
    return None

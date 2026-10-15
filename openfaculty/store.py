#!/usr/bin/env python3
"""
store.py - the SQLite connection, and the one table that is yours.

WHY THIS IS SMALL
-----------------
The harness this was extracted from had seven tables, because it also tracked
job postings, applications and scraper runs. OpenFaculty has none of that. The
three collectors each own and create their own table:

    people_papers   people.py     who published on a topic, and where
    grant_leads     grants.py     NIH RePORTER awards
    nsf_leads       nsf.py        NSF awards
    pages           faculty.py    cached faculty-page fetches

This file owns only the one table that holds **your decisions**, because that
is the only data here you cannot regenerate by re-running a collector. The
lead tables are disposable; your "I am not interested in this lab" is not.

ONE NORMALISER, NOT TWO
-----------------------
`pi_key` is the only place a person's name becomes an identifier, and it is
shared by the writer and the reader on purpose. The original code had two
spellings of this logic in two modules, they disagreed about where a surname
ending in a particle stopped ("Van Tyne D" vs "VAN TYNE, DARIA"), and
**23 funded PIs silently vanished from a shortlist** -- dismissed under a key
nothing would ever look up again. A dismissal that suppresses the wrong person,
or nobody, is worse than no dismissal at all.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from openfaculty.config import DB_PATH

SCHEMA = """
-- YOUR CALLS. Separate from every lead table because this is the only data in
-- the database that re-running a collector cannot reproduce.
--
-- scope is 'pi' and the target is 'surname,initial'. There is no finer key:
-- NIH RePORTER and Europe PMC share no identifier, so surname+initial is the
-- most specific identity the two sources actually agree on. `--audit` shows
-- you when one key is reaching more than one scientist, because that is a
-- real limitation and hiding it would be the dishonest choice.
CREATE TABLE IF NOT EXISTS decisions (
  decision_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  scope         TEXT NOT NULL,
  target        TEXT NOT NULL,
  verdict       TEXT NOT NULL,   -- dismissed|interested|later
  reason        TEXT,
  decided_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dec_scope ON decisions(scope, target);
"""

# Columns added after a table already existed on somebody's disk. CREATE TABLE
# IF NOT EXISTS is a no-op on an existing database, so a new column has to be
# added explicitly or an upgrade breaks for existing users only -- the worst
# shape of bug, because a fresh install cannot reproduce it.
_MIGRATIONS = [
    # An address found in a paper must contain the surname to be trusted. An
    # ORCID-verified one is whatever the scientist typed about themselves, and
    # a first-name address can never pass a surname check and is still correct.
    # Without recording WHICH route it came from, the only options were to
    # weaken the check for everything or to refuse good addresses.
    ("people_papers", "email_source", "TEXT"),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrate(conn) -> None:
    for table, col, decl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        # On a fresh database the collector's table does not exist yet. PRAGMA
        # on a missing table returns no rows rather than raising, so this check
        # is what stops the ALTER from failing on exactly the case that needs
        # no migration at all.
        if not cols:
            continue
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


@contextmanager
def connect(path: Path | None = None):
    """Open the database, create what is missing, commit on clean exit.

    `timeout` is a BUSY timeout, not a connect timeout. Without it a second
    writer raises `database is locked` immediately, and running two collectors
    at once is normal: `grants.py` and `nsf.py` touch different tables and each
    takes minutes. A 30-minute NIH sweep dying at minute 25 because an NSF
    sweep held the write lock for a second is a pointless loss, so the timeout
    is sized for the LONGEST sibling rather than the typical one.
    """
    conn = sqlite3.connect(path or DB_PATH, timeout=180.0)
    conn.row_factory = sqlite3.Row
    try:
        # SKIP THE SCHEMA WRITE WHEN THE SCHEMA IS ALREADY THERE.
        # `executescript()` issues an implicit COMMIT before it runs, so it
        # takes a WRITE lock on every connection -- including read-only ones.
        # That meant a command that only wanted to READ could not open the
        # database while a sweep was writing, and it failed in `connect()`
        # before doing any work. CREATE TABLE IF NOT EXISTS is a no-op after
        # the first run, so the lock was pure cost.
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        needed = set(re.findall(
            r"CREATE TABLE IF NOT EXISTS\s+(\w+)", SCHEMA, re.I))
        if not needed or not needed.issubset(have):
            conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def pi_key(name: str) -> str:
    """'Wang, T' | 'WANG, TINA' | 'Van Tyne D' -> 'wang,t' | 'vantyne,d'.

    Returns "" for anything that does not yield both halves, so a caller must
    handle the refusal rather than store a key that would match everything.

    Spaces are REMOVED from the surname because NIH prints "VAN TYNE, DARIA"
    and Europe PMC prints "Van Tyne D", and neither spelling is canonical.
    """
    n = (name or "").strip()
    if not n:
        return ""
    if "," in n:
        sur, _, rest = n.partition(",")
        initial = rest.strip()[:1]
    else:
        parts = n.split()
        if len(parts) < 2:
            return ""
        # Surname-first with a trailing initials block, as Europe PMC prints it.
        sur, initial = " ".join(parts[:-1]), parts[-1][:1]
    sur = re.sub(r"[^a-z]", "", sur.lower())
    initial = re.sub(r"[^a-z]", "", initial.lower())
    return f"{sur},{initial}" if sur and initial else ""


def record_decision(conn, scope: str, target: str, verdict: str,
                    reason: str | None = None) -> int:
    """Remember a call so a rerun never ranks it in front of you again.

    THE TARGET IS SURNAME **AND** INITIAL, AND THE FIRST VERSION WAS NOT.
    Measuring it is what found the fault: `--dismiss-pi Wang` keyed on the
    surname alone suppressed **12 grant keys to remove the 1 person meant**.
    Nine other Wangs -- at Stanford, Johns Hopkins, UNC, the NCI -- were
    pre-emptively silenced. None was on the shortlist that day, so nothing
    looked wrong; the cost would have landed later, when a sweep surfaced a
    real phage Wang who could never appear. One researcher's decision about a
    specific person, and a stranger who shares their surname, are two different
    facts and must not share a label.
    """
    if scope != "pi":
        raise ValueError(
            f"bad scope: {scope!r}. OpenFaculty records decisions about people, "
            f"and 'pi' is the only scope there is.")
    # Validate the SHAPE, so a typo cannot become a silent no-op: an empty
    # target would suppress nothing while reporting success. `outreach.py`
    # checks the stronger condition, that the dismissal removed somebody.
    target = pi_key(target)
    if not target:
        raise ValueError(
            "a PI dismissal needs 'Surname, Initial' (e.g. 'Wang, T') so it "
            "cannot silence a different scientist with the same surname; an "
            "empty target would suppress nothing")
    cur = conn.execute("""INSERT INTO decisions
        (scope,target,verdict,reason,decided_at) VALUES (?,?,?,?,?)""",
        (scope, target, verdict, reason, now()))
    return cur.lastrowid


def dismissed_pis(conn) -> set[str]:
    """Every PI key ('surname,initial') currently suppressed."""
    return {r["target"] for r in conn.execute(
        "SELECT target FROM decisions WHERE verdict='dismissed' AND scope='pi'")}


if __name__ == "__main__":
    import sys
    with connect() as c:
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        print(f"{DB_PATH}")
        for t in tables:
            n = c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            print(f"  {t:16} {n:>8,} rows")
        if not tables:
            print("  empty -- nothing collected yet.")
            sys.exit(0)

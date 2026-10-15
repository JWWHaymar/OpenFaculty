#!/usr/bin/env python3
"""
config.py - the three facts every other module needs, and nothing else.

OpenFaculty was extracted from a larger private job-search harness, whose
`config.py` was ~1,000 lines of CV vocabulary, document paths and scraper
settings. None of that belongs in a tool other people run, so this file is a
rewrite rather than a copy: it holds **where data goes** and **who we say we
are**, which is the whole of what the released modules actually read.

WHERE THINGS GO
---------------
Everything is under one directory, and you choose it:

    OPENFACULTY_HOME   default ~/.openfaculty

Inside it:

    openfaculty.db     SQLite. Your leads, your papers, your dismissals.
    cache/             HTTP cache for faculty pages. Delete it any time.
    notes/             Markdown written by `--write`.

Nothing is uploaded, and there is no account. If you delete that directory the
tool starts over.
"""

from __future__ import annotations

import os
from pathlib import Path

# ── IDENTITY ────────────────────────────────────────────────────────────────
GITHUB_USER = "JWWHaymar"
OPENFACULTY_REPO = "OpenFaculty"
REPO_URL = f"https://github.com/{GITHUB_USER}/{OPENFACULTY_REPO}"

# THE POLITE AGENT. Every outbound request carries this, and the `+url`
# convention exists so that a university sysadmin reading an access log can
# find a page that EXPLAINS the traffic rather than guessing.
#
# Two mistakes were made here before it was right, and both are worth stating
# because a fork will be tempted by each. It first pointed at the agent
# harness's repository -- real, ours, and a stranger's project, so a complaint
# would have gone to the wrong people. It then pointed at the author's GitHub
# PROFILE, which answers "who" and not "what". The repo is the only URL that
# answers the question the convention is asking.
#
# IF YOU FORK THIS, CHANGE IT. Set OPENFACULTY_AGENT_URL to your own fork or
# contact page. Leaving it pointed here means your traffic is attributed to
# somebody who cannot answer for it.
_AGENT_URL = os.environ.get("OPENFACULTY_AGENT_URL", REPO_URL)
_AGENT_WHO = os.environ.get(
    "OPENFACULTY_AGENT_WHO", "one researcher looking up faculty contact pages")

POLITE_AGENT = (f"OpenFaculty/1.0 ({_AGENT_WHO}; "
                f"respects robots.txt; +{_AGENT_URL})")

# There is deliberately NO browser user-agent string in this file. The private
# harness had one, for job boards that block non-browsers, and three modules
# here imported it by habit. Spoofing a browser at NIH RePORTER, NSF, ORCID or
# Europe PMC buys nothing -- they are public APIs that want to know who is
# calling -- and it would make the politeness claim in the README false.

# ── WHERE DATA GOES ─────────────────────────────────────────────────────────
HOME = Path(os.environ.get("OPENFACULTY_HOME", str(Path.home() / ".openfaculty")))
DB_PATH = Path(os.environ.get("OPENFACULTY_DB", str(HOME / "openfaculty.db")))
CACHE_DIR = Path(os.environ.get("OPENFACULTY_CACHE", str(HOME / "cache")))
NOTES_DIR = Path(os.environ.get("OPENFACULTY_NOTES", str(HOME / "notes")))

for _d in (HOME, CACHE_DIR, NOTES_DIR):
    _d.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    print(f"agent : {POLITE_AGENT}")
    print(f"home  : {HOME}")
    print(f"db    : {DB_PATH}")
    print(f"cache : {CACHE_DIR}")
    print(f"notes : {NOTES_DIR}")

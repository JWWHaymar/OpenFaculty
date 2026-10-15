#!/usr/bin/env python3
"""
cli.py - one front door, so the tool can be USED rather than read.

WHY THIS EXISTS
---------------
Each module below has its own `--help` and can be run directly, which is right:
the pipeline has four stages and you should be able to stop and look at any one
of them. But a person who has just installed this does not know that, and
"read six files to find out which one to run first" is how a working tool goes
unused.

So this is a router, not a layer. `openfaculty people --collect` runs exactly
what `python people.py --collect` runs, with the same flags and the same output.
Nothing is reimplemented here, because two spellings of one command is how they
drift apart.

    openfaculty setup                 # where will my data go?
    openfaculty quickstart            # the whole pipeline, in order, explained
    openfaculty people --collect      # ① who publishes on this
    openfaculty grants --collect      # ② who has NIH money
    openfaculty nsf --collect         # ② who has NSF money
    openfaculty faculty --dry-run     # ③ resolve the rest to a faculty page
    openfaculty outreach --write      # ④ the shortlist
    openfaculty test                  # prove the install works
"""

from __future__ import annotations

import sys

from openfaculty import config

STAGES = {
    "people":   ("① find the people publishing on a topic (Europe PMC, ORCID)",
                 "people"),
    "grants":   ("② who holds active NIH money (RePORTER)", "grants"),
    "nsf":      ("② who holds active NSF money (ecology, marine, evolution)",
                 "nsf"),
    "faculty":  ("③ resolve a PI to their current faculty page and address",
                 "faculty"),
    "outreach": ("④ FUNDED ∧ PUBLISHING ∧ CONTACTABLE -- the shortlist",
                 "outreach"),
}

QUICKSTART = """
THE PIPELINE, IN ORDER
======================
Each step writes to the same local database, so you can stop after any of them
and look. Nothing is uploaded, and nothing is ever sent to anyone.

  1. openfaculty people --collect
         Reads Europe PMC for papers on the built-in topics (or your own, with
         --topic), takes the LAST author as the PI, and groups by person.
         Add --topic "chromatin remodeling" to search your own field.
         ~2-5 minutes.

  2. openfaculty people --backfill-emails
         Pulls the corresponding address out of open-access full text. This is
         the step that makes the shortlist contactable; without it, stage 4
         correctly returns nobody. Resolves about 70%.

  3. openfaculty grants --collect
         NIH RePORTER: who has active federal money. ~1 minute.

     openfaculty nsf --collect
         NSF: ecology, evolution and marine microbiology, which NIH does not
         fund and therefore cannot see. ~1 minute.

  4. openfaculty faculty
         For the ~30% with no address in any paper, reads their university
         faculty page. Respects robots.txt, rate-limits per host, and REFUSES
         rather than guessing when a page lists several people.
         Try --dry-run first to see what it would do.

  5. openfaculty outreach --write
         The join: people who are funded AND publishing AND reachable, ranked,
         written to a Markdown file you can actually read.

THEN READ IT YOURSELF.
The shortlist ranks; it does not decide. You know your own field, and you will
know within ten names whether the ordering is sane.
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        print("\nSTAGES")
        for name, (blurb, _) in STAGES.items():
            print(f"  {name:<10} {blurb}")
        print("\n  setup      show where data is stored and who we identify as")
        print("  quickstart the pipeline in order, with timings")
        print("  test       run the test suite")
        print("\nAny stage passes its remaining arguments straight through, so "
              "`openfaculty people --help`\nis `python people.py --help`.")
        return 0

    cmd, rest = argv[0], argv[1:]

    if cmd == "quickstart":
        print(QUICKSTART.strip())
        return 0

    if cmd == "setup":
        print("OpenFaculty stores everything locally. Nothing is uploaded.\n")
        print(f"  home   {config.HOME}")
        print(f"  db     {config.DB_PATH}")
        print(f"  cache  {config.CACHE_DIR}   (derived; delete any time)")
        print(f"  notes  {config.NOTES_DIR}")
        print(f"\n  agent  {config.POLITE_AGENT}")
        print("\nOverride any of them with OPENFACULTY_HOME, OPENFACULTY_DB, "
              "OPENFACULTY_CACHE,\nOPENFACULTY_NOTES. **If you fork this, set "
              "OPENFACULTY_AGENT_URL to your own repo**\nso your traffic is "
              "attributed to you and not to somebody who cannot answer for it.")
        return 0

    if cmd == "test":
        from openfaculty import test_cannot_send, test_parsing
        rc = test_cannot_send.main() or 0
        rc |= test_parsing.main() or 0
        return rc

    if cmd not in STAGES:
        print(f"unknown command {cmd!r}. `openfaculty --help` lists them.")
        return 2

    # Hand the real module its own argv and let it parse. The stage modules own
    # their flags; duplicating them here is how a wrapper starts lying about
    # what the tool accepts.
    module = __import__(f"openfaculty.{STAGES[cmd][1]}", fromlist=["main"])
    sys.argv = [f"{cmd}.py"] + rest
    return module.main() or 0


if __name__ == "__main__":
    sys.exit(main())

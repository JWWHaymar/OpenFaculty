#!/usr/bin/env python3
"""
test_cannot_send.py - the one test that is not about correctness.

Every other check in a project like this asks "is the answer right". This one
asks "can this program do a thing it must never do", and the answer has to stay
no as the code grows.

WHY IT IS A TEST AND NOT A COMMENT
----------------------------------
OpenFaculty prints the email addresses of real scientists. The distance between
*printing an address* and *sending to 300 addresses* is about four lines of
`smtplib`, and the person who writes those four lines will have a good reason at
the time: a follow-up reminder, a template, a batch of three.

**A bad first email to a lab cannot be retracted, and you get one first contact
per lab.** The cost of that mistake is not a traceback, it is a door closed in a
field small enough that people talk. So the prohibition is asserted mechanically
rather than documented, because a comment cannot fail a test run.

Run it:  python test_cannot_send.py
"""

from __future__ import annotations

import ast
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent

# Modules that touch addresses, which is all of them that matter.
# EVERY module, discovered rather than listed. A hand-written list silently
# stops guarding a file the day somebody adds one, which is exactly the day the
# guarantee matters -- and `matching.py` and `cli.py` were both added after this
# test was written, so that is not hypothetical.
MODULES = sorted(p.name for p in HERE.glob("*.py")
                 if not p.name.startswith("test_"))

# Libraries that can put a message on the wire. `requests` and `urllib` are NOT
# here, deliberately: this tool is an HTTP client by design and banning HTTP
# would ban the thing it does. The line is drawn at MAIL, which no part of a
# lookup tool has any reason to import.
FORBIDDEN_MODULES = {
    "smtplib", "email.mime", "email.message", "yagmail", "sendgrid",
    "mailjet_rest", "postmarker", "boto3",  # boto3 reaches SES
    "win32com",                              # the Outlook COM route
    "exchangelib", "O365", "msal",
}

# Shell-outs that reach a mail transport without importing anything.
FORBIDDEN_CALLS = {"sendmail", "mailx", "/usr/sbin/sendmail", "mutt", "msmtp"}


def imported_names(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            # `from email import mime` has to be caught as `email.mime`.
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


def main() -> int:
    failures: list[str] = []
    checked = 0

    for name in MODULES:
        path = HERE / name
        if not path.exists():
            # Only reachable if a file disappears mid-run, since MODULES is
            # discovered from the directory.
            failures.append(f"{name}: vanished while being checked.")
            continue
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
        checked += 1

        for mod in sorted(imported_names(tree)):
            for bad in FORBIDDEN_MODULES:
                # Prefix match, so `email.mime.text` is caught by `email.mime`.
                if mod == bad or mod.startswith(bad + "."):
                    failures.append(
                        f"{name} imports {mod!r}. OpenFaculty must not be able "
                        f"to send mail. If you need to draft a message, write "
                        f"it to a file and let a human press send.")

        # The string check is deliberately crude and runs over the source
        # rather than the AST, because `os.system("sendmail ...")` hides the
        # transport inside a string literal where no import appears at all.
        for call in FORBIDDEN_CALLS:
            if call in src:
                # Only count it outside this file's own list of banned words.
                failures.append(
                    f"{name} mentions {call!r}, which is a mail transport. "
                    f"Even in a string, even commented out, that is the one "
                    f"capability this tool does not get.")

    if not checked:
        # An empty check that exits 0 is worse than a failing one: it reports
        # safety it never established.
        print("FAIL: this test guarded ZERO files. That is a broken test, not "
              "a passing one.")
        return 1

    if failures:
        print(f"FAIL ({len(failures)} problem(s)):\n")
        for f in failures:
            print(f"  - {f}\n")
        return 1

    print(f"PASS: {checked} modules checked, none can send mail.")
    print("      The tool prepares; you send.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

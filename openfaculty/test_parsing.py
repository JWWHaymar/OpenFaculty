#!/usr/bin/env python3
"""
test_parsing.py - the judgement calls, tested on the inputs that broke them.

Every case here is a REAL input that produced a wrong answer at some point. A
test written from imagination passes on the misconception you held while writing
it; these were all written after reading output and finding it wrong.

Run it:  python test_parsing.py
"""

from __future__ import annotations

import sys

from openfaculty import concepts, nsf, outreach, store

PASS, FAIL = [], []


def check(label: str, got, want) -> None:
    (PASS if got == want else FAIL).append((label, got, want))


# ── NSF's JSON is not consistently typed ────────────────────────────────────
# `fundProgramName` is a string for most awards and a LIST for co-funded ones.
# A co-funded BIO/OCE grant is exactly the marine-ecology case `nsf.py` exists
# to reach, so this crashed on the records that matter most, and it took the
# whole collect run with it.
check("nsf._text: plain string", nsf._text("BIO/OCE"), "BIO/OCE")
check("nsf._text: None is empty", nsf._text(None), "")
check("nsf._text: a LIST of programs is joined, not dropped",
      nsf._text(["01002425RB NSF R&RA", "01002526RB NSF R&RA"]),
      "01002425RB NSF R&RA; 01002526RB NSF R&RA")
check("nsf._text: empties inside a list do not leave stray separators",
      nsf._text(["BIO", None]), "BIO")
check("nsf._text: a number survives", nsf._text(1234567), "1234567")
# A real award row, reduced to the fields that crashed.
_row = nsf._row({"id": 2144663, "piFirstName": "Nahid", "piLastName": "Bhadelia",
                 "fundProgramName": ["01002425RB NSF R&RA", "01002526RB NSF R&RA"],
                 "title": "EAGER: Biothreats", "awardeeName": "Boston University",
                 "awardeeStateCode": "MA"}, topic="phage")
check("nsf._row survives a co-funded award", _row["pi_name"], "NAHID BHADELIA")
check("nsf._row keeps both programs", "; " in _row["program"], True)


# ── A surname is not a person ───────────────────────────────────────────────
# Keying a dismissal on the surname alone suppressed 12 grant keys to remove
# the 1 person meant, silencing nine unrelated scientists.
check("pi_key: NIH's shouting form", store.pi_key("WANG, TINA"), "wang,t")
check("pi_key: Europe PMC's form", store.pi_key("Wang T"), "wang,t")
check("pi_key: the two sources agree",
      store.pi_key("VAN TYNE, DARIA"), store.pi_key("Van Tyne D"))
check("pi_key: a particle surname loses its space", store.pi_key("Van Tyne D"),
      "vantyne,d")
# REFUSAL IS THE POINT. A surname with no initial must return "" rather than a
# key that would match every scientist who shares it.
check("pi_key: surname alone REFUSES", store.pi_key("Wang"), "")
check("pi_key: empty refuses", store.pi_key(""), "")
check("pi_key: whitespace refuses", store.pi_key("   "), "")


# ── The institution check is the whole defence against surname collisions ──
# Measured: surname+initial alone yielded 463 "matches", many of them two
# scientists glued together.
check("corroborate: a distinctive shared token is enough",
      bool(outreach.corroborate(
          [("MARRAFFINI, LUCIANO", "ROCKEFELLER UNIVERSITY", "NY", "New York",
            2024, "grant", 1, "phage", "abstract", "")],
          [("Marraffini L", "Rockefeller University, New York, NY", "a@b.edu",
            2024, "paper", "Cell")])),
      True)
# The first version of this check passed Caltech ~ Tianjin on the word
# "technology", and Salk ~ Nankai on "studies". Generic words name nothing.
check("corroborate: 'technology' is NOT identification",
      outreach.corroborate(
          [("LEE, S", "CALIFORNIA INSTITUTE OF TECHNOLOGY", "CA", "Pasadena",
            2024, "g", 1, "phage", "a", "")],
          [("Lee S", "Tianjin University of Technology, Tianjin, China",
            "a@b.cn", 2024, "p", "J")]),
      None)
check("tokens: generic institution words are stripped",
      outreach.tokens("University of Medical Sciences"), set())
check("tokens: a real name survives",
      "rockefeller" in outreach.tokens("Rockefeller University"), True)

# A US federal grant does not come with a Chinese address. Second, independent
# check, because it catches what a shared generic word lets through.
check("plausibly_us: a .edu address", outreach.plausibly_us("a@yale.edu"), True)
check("plausibly_us: a .cn address", outreach.plausibly_us("a@nankai.edu.cn"),
      False)


# ── A macrophage is not a bacteriophage ────────────────────────────────────
# `-phage` is Greek for "eater" and names two unrelated things. A PDF wrapping
# "macro- phage" across a line creates a real word boundary, and one such
# abstract put a radiation-immunology grant into the top tier of phage labs.
def about_phage(text: str) -> bool:
    """Does this text read as PHAGE BIOLOGY -- the bench subject?

    Keyed on `phage_biology` specifically, not on any concept whose key happens
    to contain "phage". That distinction is the point of the module: `phage
    display` is correctly recognised as its own concept
    (`phage_display_method`) rather than being made invisible, and a text can
    be ABOUT phage display while not being about phage biology at all. A looser
    helper here reported phage display as phage work and was the test being
    wrong, not the code.
    """
    u = concepts.understand(text)
    return any(r.ok and r.concept.key == "phage_biology" for r in u.readings)


check("phage: bench phage work reads as phage",
      about_phage("We characterise bacteriophage T4 host range in E. coli."),
      True)
check("phage: a bare 'phage' counts",
      about_phage("Phage therapy against Pseudomonas aeruginosa infection."),
      True)
check("macrophage does NOT read as phage",
      about_phage("Macrophage polarisation in tumour immunity."), False)
check("a LINE-WRAPPED macrophage still does not read as phage",
      about_phage("We studied macro- phage recruitment after irradiation."),
      False)
# Phage display is not SUPPRESSED, it is correctly RE-LABELLED. The text is
# about a real thing; it is just not about phage biology.
_display = concepts.understand("Phage display selection of scFv antibody libraries.")
check("phage DISPLAY does not read as phage biology",
      about_phage("Phage display selection of scFv antibody libraries."),
      False)
check("phage biology is specifically marked wrong-sense, not merely absent",
      [r.sense for r in _display.readings if r.concept.key == "phage_biology"],
      ["wrong-sense"])
check("phage display is still recognised as its own concept",
      any(r.ok and r.concept.key == "phage_display_method"
          for r in _display.readings), True)
check("cyanophage counts: ecology is phage biology",
      about_phage("Cyanophage infection of Prochlorococcus in the Sargasso."),
      True)


# ── An acronym is not its members ──────────────────────────────────────────
# A term list held the literal string "eskape", which appears zero times in 831
# real texts. What appears is the organisms it stands for.
def organisms(text: str) -> set[str]:
    return set(concepts.understand(text).organisms)


check("ESKAPE is recognised through a MEMBER, not the acronym",
      "Klebsiella pneumoniae" in organisms(
          "Carbapenem-resistant Klebsiella pneumoniae isolates."), True)
check("E. coli is found as the abbreviated binomial",
      "Escherichia coli" in organisms("Uropathogenic E. coli cystitis model."),
      True)
check("MRSA resolves to S. aureus",
      "Staphylococcus aureus" in organisms("MRSA bacteraemia outcomes."), True)


def main() -> int:
    for label, got, want in FAIL:
        print(f"FAIL  {label}\n      got  {got!r}\n      want {want!r}")
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed.")
    if not PASS:
        print("A test file that asserted nothing is a broken test, not a pass.")
        return 1
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

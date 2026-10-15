#!/usr/bin/env python3
"""
concepts.py - What the words MEAN, so matching can stop being spelling.

THE PROBLEM WITH A KEYWORD LIST
-------------------------------
A term list of the kind this replaced contained the literal string "eskape".
Measured across 831 stored texts and 129 grant leads: the word "ESKAPE" appears
**zero**
times. What appears is *Klebsiella pneumoniae*, *Acinetobacter baumannii*,
*Pseudomonas aeruginosa* - the organisms the acronym stands for. A list that
holds the acronym and not its members is spelling, not understanding.

The same failure runs deeper than acronyms:

  * "phage display" and "PhIP-Seq" contain the word phage and are **antibody**
    methods. A phage biologist is not especially qualified for either.
  * A "phage VLP vaccine" project is protein engineering; the phage is a
    scaffold, not a subject.
  * "Restriction-modification" never says the word antiphage, and it IS
    antiphage defense.

So this module encodes three things a term list cannot:

  1. **Composition** - ESKAPE is six genera; Enterobacteriaceae is a family
     containing several of them. Naming a member counts as naming the group.
  2. **Sense** - the same word means different work in different contexts, and
     the disqualifying context is named explicitly rather than hoped away.
  3. **Relation to YOUR evidenced work** - each concept says how it relates to
     work you can actually document, and every claim of relevance must cite the
     phrase that triggered it. `EVIDENCED_ORGANISMS` below is the one place that
     is configured per person, and it is deliberately short.

WHAT THIS IS NOT
----------------
Not a model, and not a score. It produces a **reading**: which concepts a text
actually invokes, with the matched phrase attached, plus which of those you
have documented work in. Judgment that reaches a human quotes its source line.
A number with no quote behind it is what matched "rag" inside "b-rag-nd new lab
space" and reported a retrieval-augmented-generation lab.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── ORGANISMS ────────────────────────────────────────────────────────────────
# ESKAPE is an acronym, and every member is also written in ways a scraper sees:
# genus alone, genus + species, the abbreviated binomial, and the common
# shorthand. `Staphylococcus aureus` is additionally `S. aureus`, `MRSA`, `MSSA`.
ESKAPE = {
    "Enterococcus faecium": r"enterococc\w*|\be\.?\s?faecium\b|\bvre\b",
    "Staphylococcus aureus": r"staphylococc\w*|\bs\.?\s?aureus\b|\bmrsa\b|\bmssa\b",
    "Klebsiella pneumoniae": r"klebsiell\w*|\bk\.?\s?pneumoniae\b|\bcre\b|\bkpc\b",
    "Acinetobacter baumannii": r"acinetobacter|\ba\.?\s?baumannii\b|\bcrab\b",
    "Pseudomonas aeruginosa": r"pseudomonas|\bp\.?\s?aeruginosa\b",
    "Enterobacter species": r"enterobacter\b|enterobacter\s+\w+",
}

# Enterobacteriaceae is a FAMILY, not an ESKAPE member, and it contains E. coli
# and Klebsiella. If your organism is uropathogenic E. coli, a text naming the
# family is naming your organism without using your words.
ENTEROBACTERIACEAE = {
    "Escherichia coli": r"escherichia|\be\.?\s?coli\b|\bupec\b|uropathogenic",
    "Klebsiella pneumoniae": ESKAPE["Klebsiella pneumoniae"],
    "Enterobacter species": ESKAPE["Enterobacter species"],
    "Salmonella": r"salmonell\w*",
    "Enterobacteriaceae": r"enterobacteriace\w*",
}

ORGANISM_GROUPS: dict[str, dict[str, str]] = {
    "ESKAPE pathogens": ESKAPE,
    "Enterobacteriaceae": ENTEROBACTERIACEAE,
}

# ── THE ONE THING YOU SHOULD EDIT ───────────────────────────────────────────
# Organisms YOU have DOCUMENTED work in. Everything else is context, not fit.
#
# This is what separates tier 4 ("phage work in an organism I can speak to in an
# email") from tier 3 ("phage work"). Keep it SHORT and keep it honest: an
# organism you read about is not an organism you have worked in, and the whole
# value of the distinction is that tier 4 is a list you can write to today.
#
# The default is the author's own, left in place as a worked example rather than
# a recommendation. Replace it with yours.
EVIDENCED_ORGANISMS = {
    "Escherichia coli", "Pseudomonas aeruginosa", "Staphylococcus aureus",
}


# ── CONCEPTS ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Concept:
    """One idea, its wordings, and what it means for you.

    `disqualifiers` is the field that makes this more than a synonym list. A
    concept can be *named* by a text that is not *about* it, and for phage the
    collision is severe: phage display, PhIP-Seq and phage VLP vaccines all
    contain "phage" and are antibody or vaccine work.
    """
    key: str
    label: str
    pattern: str
    relation: str                  # evidenced | adjacent | context
    why: str                       # the sentence a human reads
    disqualifiers: tuple[str, ...] = ()

    def read(self, text: str) -> "Reading | None":
        """Does this text invoke the concept, and in the right sense?"""
        m = re.search(self.pattern, text, re.I)
        if not m:
            return None
        quote = _around(text, m.start(), m.end())
        for bad in self.disqualifiers:
            dm = re.search(bad, text, re.I)
            if dm:
                return Reading(self, quote=quote, sense="wrong-sense",
                               note=_around(text, dm.start(), dm.end()))
        return Reading(self, quote=quote, sense="ok")


@dataclass
class Reading:
    """A concept found in a text, with the phrase that triggered it.

    The quote is not decoration. Every wrong call this project has produced was
    invisible until someone looked at the matched substring: "rag" inside
    "b-rag-nd", "gene editing" inside "Geneva".
    """
    concept: Concept
    quote: str
    sense: str = "ok"              # ok | wrong-sense
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.sense == "ok"


# The phage work that is NOT phage biology. Each of these is a real, common
# research programme that a "phage" keyword cannot distinguish from bench phage
# work, and which a phage biologist is not especially qualified for.
_PHAGE_DISPLAY = r"phage\s+display|phip-?seq|antibody\s+librar|scfv|nanobod|" \
                 r"biopanning|single[- ]domain\s+antibod"
_PHAGE_VLP = r"virus-?like\s+particle|\bvlp\b|spytag|spycatcher|" \
             r"antigen\s+display|vaccine\s+platform"

# A MACROPHAGE IS NOT A BACTERIOPHAGE.
#
# `-phage` is Greek for "eater" and names two unrelated things: viruses that
# infect bacteria, and human cells that eat them. Every immunology abstract in
# the corpus is full of the second. `\bphage\b` looks safe until a PDF wraps the
# word across a line -- *macrophage* arrives as "macro- phage", which contains a
# genuine word boundary. 54 stored abstracts have that shape, and one put a
# radiation-immunology grant into the top tier of phage labs.
#
# A blocklist of cell prefixes (macro, lympho, melano...) fails on the next one
# nobody listed. So this is an ALLOWLIST: `phage` counts only when it stands
# alone or carries a known virological prefix. Anything else glued to `-phage`
# is assumed to be a cell, which is the safe direction to be wrong in -- a
# missed phage lead is visible when you read the list, and a macrophage grant
# ranked top is the error that wastes your time.
_PHAGE_PREFIX = r"(?:bacterio|jumbo|myco|cyano|coli|entero|halo|sipho|myo|podo|staphylo|strepto)"
_PHAGE_WORD = (
    # a prefixed phage word: bacteriophage, jumbophage, mycobacteriophage
    r"(?:\b\w*" + _PHAGE_PREFIX + r"phages?\b"
    # or bare `phage`/`phages`, with nothing word-like or hyphen-joined before it
    r"|(?<![A-Za-z])(?<![A-Za-z]-)(?<![A-Za-z]- )\bphages?\b)"
)

# The lookbehinds above are the second line of defence, not the first. The real
# fix is to undo the wrapping before any pattern runs: the corpus contains
# "macro-\nphage" with a NEWLINE, and a lookbehind written against "macro- "
# passed its test and still failed on real data. Repairing the text once, in one
# place, beats teaching every pattern about typography.
_DEHYPHEN_RE = re.compile(r"(?<=[a-z])-\s*\n\s*(?=[a-z])")


def normalize(text: str) -> str:
    """Undo hyphenated line-wrapping. `macro-\\nphage` -> `macrophage`.

    PDF-extracted grant abstracts wrap words across lines with a hyphen. Left
    alone, every such word silently becomes two, and `\\bphage\\b` matches the
    tail of *macrophage* with a genuine word boundary in front of it.
    """
    return _DEHYPHEN_RE.sub("", text or "")

CONCEPTS: tuple[Concept, ...] = (
    Concept(
        "phage_biology", "phage biology (bench)",
        _PHAGE_WORD + r"|prophage|lysogen\w*|lytic\s+cycle|"
        r"plaque\s+assay|top\s+agar|host\s+range|efficiency\s+of\s+plating|"
        r"phage\s+cocktail|phage\s+therapy|endolysin|virulent\s+phage",
        "evidenced",
        "Three phage positions, incl. engineered Pseudomonas cocktails at Armata; "
        "thesis is prophage/antiphage biology.",
        disqualifiers=(_PHAGE_DISPLAY, _PHAGE_VLP),
    ),
    Concept(
        "antiphage_defense", "antiphage defense systems",
        r"anti-?phage|defen[cs]e\s+system|\bcbass\b|pycsar|retron|thoeris|"
        r"restriction[- ]modification|abortive\s+infection|toxin-?antitoxin|"
        r"cyclic\s+oligonucleotide|\bgabija\b|\bhachiman\b|phage\s+resistance|"
        r"phage\s+defen[cs]e",
        "evidenced",
        "The thesis subject: a guanylyl cyclase bridging antiphage response "
        "(CBASS) with cellular processes in uropathogenic E. coli.",
    ),
    Concept(
        "bacterial_genetics", "bacterial genetics",
        r"bacterial\s+genetic|recombineer|lambda\s+red|allelic\s+exchange|"
        r"markerless\s+deletion|transposon\s+(mutagenesis|librar|insertion)|"
        r"knockout\s+(strain|librar)|bacterial\s+conjugation|suicide\s+vector|"
        r"crispri",
        "evidenced",
        "Markerless deletions, recombineering and transposon work documented in "
        "the ABG lab manual.",
    ),
    Concept(
        "amr", "antimicrobial resistance",
        r"antimicrobial\s+resistan|antibiotic\s+resistan|\bamr\b|\bmdr\b|"
        r"multi-?drug[- ]resistan|\bmic\b|minimum\s+inhibitory|"
        r"susceptibility\s+testing|checkerboard|synergy\s+assay",
        "evidenced",
        "AMR is the applied frame of both the thesis and the Armata phage-therapy "
        "work.",
    ),
    Concept(
        "microbiology_bench", "general microbiology bench work",
        # Deliberately NOT the bare word "microbiology": it appears in
        # qualification boilerplate ("theoretical knowledge of biology,
        # microbiology, social sciences") on clinical-coordinator postings that
        # involve no culture work at all. What identifies bench bacteriology is
        # an ACT, so these are acts.
        r"microbiolog\w*\s+(assay|techni|method|lab|bench|culture|testing)|"
        r"bacteriolog\w*|aseptic\s+techni|sterile\s+techni|"
        r"overnight\s+cultur|bacterial\s+cultur|growth\s+curve|\bod600\b|"
        r"colony[- ]forming|\bcfu\b|streak\s+plate|anaerobic\s+(chamber|cultur)|"
        r"broth\s+microdilution|\bmic\s+(assay|testing|determination)|"
        r"plating\s+(assay|efficiency)|serial\s+dilution",
        "evidenced",
        "Routine bacteriology throughout the thesis and lab manual.",
    ),
    Concept(
        "genomics", "microbial genomics / bioinformatics",
        r"genome\s+(assembl|annotat|sequenc)|comparative\s+genomic|nanopore|"
        r"illumina|bioinformatic|\bblast\b|phylogen\w*|\bpangenom|variant\s+call",
        "adjacent",
        "Genome assembly, annotation and comparative work are documented, though "
        "the CV leads with bench work.",
    ),
    Concept(
        "eukaryotic_cell", "eukaryotic cell culture",
        r"mammalian\s+cell|cell\s+cultur|tissue\s+cultur|hek\s?293|\bcho\b|"
        r"hela|primary\s+cells?|passag\w+\s+cells|organoid|ipsc|"
        r"flow\s+cytometr|\bfacs\b",
        "blocked",
        "No eukaryotic cell work has ever been performed. This is a hard "
        "boundary, not a preference.",
    ),
    Concept(
        "clinical_research", "clinical research / trials coordination",
        r"clinical\s+(trial|research\s+coordinat|protocol)|irb\b|informed\s+consent|"
        r"patient\s+(recruit|enroll)|case\s+report\s+form|\bgcp\b|study\s+coordinat|"
        r"regulatory\s+submission",
        "blocked",
        "Not bench science. These postings are titled 'Research Associate' and "
        "are a different profession.",
    ),
    Concept(
        "phage_display_method", "phage display / antibody engineering",
        _PHAGE_DISPLAY,
        "context",
        "Uses the word phage for an antibody method. Phage bench experience does "
        "not particularly transfer, and claiming it would overstate.",
    ),
)

CONCEPTS_BY_KEY = {c.key: c for c in CONCEPTS}


def _around(text: str, start: int, end: int, pad: int = 60) -> str:
    """The matched phrase in its sentence, whitespace-collapsed."""
    seg = text[max(0, start - pad): min(len(text), end + pad)]
    return re.sub(r"\s+", " ", seg).strip()


# ── READING A TEXT ───────────────────────────────────────────────────────────
# Asepsis is REQUIRED by bacteriology and does not IDENTIFY it: mammalian
# tissue culture, parenteral filling and QC manufacturing all demand it. Used
# by is_bench_microbiology to refuse promotion on this signal alone.
_ASEPSIS_ONLY_RE = re.compile(r"aseptic\s+techni\w*|sterile\s+techni\w*", re.I)

# The acts that DO identify bacteriology, as opposed to accompanying it. This
# is the microbiology_bench pattern minus asepsis: what remains after the
# shared technique is removed.
_MICRO_BENCH_ACT_RE = re.compile(
    r"microbiolog\w*\s+(assay|techni|method|lab|bench|culture|testing)|"
    r"bacteriolog\w*|overnight\s+cultur|bacterial\s+cultur|growth\s+curve|"
    r"\bod600\b|colony[- ]forming|\bcfu\b|streak\s+plate|"
    r"anaerobic\s+(chamber|cultur)|broth\s+microdilution|"
    r"\bmic\s+(assay|testing|determination)|plating\s+(assay|efficiency)|"
    r"serial\s+dilution",
    re.I,
)


@dataclass
class Understanding:
    """What a text is actually about, and how it relates to your own work.

    Deliberately not a single score. A score answers "how much" when the useful
    question is "which parts, and why" -- and a score cannot be argued with,
    while a quote can.
    """
    organisms: dict[str, str] = field(default_factory=dict)      # name -> quote
    groups: dict[str, list[str]] = field(default_factory=dict)   # group -> members
    readings: list[Reading] = field(default_factory=list)
    text_len: int = 0

    def by_relation(self, relation: str) -> list[Reading]:
        return [r for r in self.readings
                if r.ok and r.concept.relation == relation]

    @property
    def evidenced(self) -> list[Reading]:
        return self.by_relation("evidenced")

    @property
    def blocked(self) -> list[Reading]:
        return self.by_relation("blocked")

    @property
    def wrong_sense(self) -> list[Reading]:
        return [r for r in self.readings if not r.ok]

    @property
    def evidenced_organisms(self) -> list[str]:
        return sorted(set(self.organisms) & EVIDENCED_ORGANISMS)

    def is_bench_microbiology(self) -> bool:
        """Does this describe bacteriology at the bench?

        Requires a *concept*, not a word: an organism name alone appears in
        clinical and epidemiological postings that involve no culture work.

        2026-09-05. Two corrections, both found by reading the board rather
        than the regex.

        **A hard-boundary block disqualifies.** All three jobs under "Your
        subject" were mammalian: *Stem Cell Research Associate*, *Cancer
        Center*, *Cell Culture / Stem Cell*. Each one ALREADY carried
        `eukaryotic cell culture` in `blocked`, and the section printed them
        anyway under a heading promising "bacteriology at the bench... read
        these first". The section that exists to say *this is your science*
        was recommending the one thing he has never done.

        **Aseptic technique is not evidence of bacteriology.** It caused 17 of
        the 22 false positives on its own, because mammalian tissue culture,
        parenteral filling and QC manufacturing all demand it -- the
        aseptic-only postings were *Manufacturing Associate II, Parenteral
        Filling* and *Manufacturing Chemist II, 2nd Shift*. It stays in the
        concept as a supporting signal, but it can no longer promote a posting
        into this section by itself. An act shared with three other
        professions does not identify one.
        """
        if self.blocked:
            return False
        keys = {r.concept.key for r in self.readings if r.ok}
        if keys & {"phage_biology", "antiphage_defense", "bacterial_genetics"}:
            return True
        # microbiology_bench qualifies only on evidence beyond bare asepsis.
        # The quote is the surrounding sentence, so it can contain BOTH: strip
        # the asepsis phrases and ask whether any bacteriological act is left.
        # Checking "does the quote mention asepsis" instead would suppress
        # "sterile technique, overnight cultures and CFU enumeration", which is
        # exactly the thing this section exists to surface.
        return any(
            r.concept.key == "microbiology_bench" and r.ok
            and _MICRO_BENCH_ACT_RE.search(_ASEPSIS_ONLY_RE.sub(" ", r.quote or ""))
            for r in self.readings)

    def summary(self) -> str:
        """One line a human can check against the text."""
        if not self.text_len:
            return "no text to read"
        bits = []
        if self.organisms:
            bits.append("organisms: " + ", ".join(sorted(self.organisms)))
        for group, members in self.groups.items():
            bits.append(f"{group} ({len(members)}): {', '.join(members)}")
        if self.evidenced:
            bits.append("evidenced: " + ", ".join(r.concept.label
                                                 for r in self.evidenced))
        if self.blocked:
            bits.append("blocked: " + ", ".join(r.concept.label
                                                for r in self.blocked))
        if self.wrong_sense:
            bits.append("wrong sense: " + ", ".join(r.concept.label
                                                    for r in self.wrong_sense))
        return " · ".join(bits) or "no domain concept found"


def understand(text: str) -> Understanding:
    """Read a job description or grant abstract for what it is about.

    Deterministic and quotable. This is the layer a model would otherwise be
    asked to do badly: naming which organisms and concepts a text invokes is a
    lookup once the vocabulary is written down, and PRINCIPLES says code goes
    where a rule exists.
    """
    text = normalize(text or "")
    u = Understanding(text_len=len(text))
    if not text.strip():
        return u

    for group, members in ORGANISM_GROUPS.items():
        found = []
        for name, pattern in members.items():
            m = re.search(pattern, text, re.I)
            if not m:
                continue
            found.append(name)
            u.organisms.setdefault(name, _around(text, m.start(), m.end()))
        if found:
            u.groups[group] = sorted(found)

    for concept in CONCEPTS:
        reading = concept.read(text)
        if reading:
            u.readings.append(reading)
    return u


# ── WHY IT FITS, IN SENTENCES ────────────────────────────────────────────────
def fit_reasons(u: Understanding) -> list[str]:
    """Sentences explaining the fit, each carrying the phrase that justified it.

    No quote, no claim. This is the rule that would have caught every phantom
    skill this project has shipped.
    """
    out: list[str] = []
    for name in u.evidenced_organisms:
        out.append(f"**{name}** — documented work. Posting says: “{u.organisms[name]}”")

    for group, members in u.groups.items():
        overlap = sorted(set(members) & EVIDENCED_ORGANISMS)
        if len(members) > 1 and overlap:
            out.append(
                f"Names {len(members)} {group} — {', '.join(members)} — of which "
                f"{', '.join(overlap)} is evidenced work.")

    for r in u.evidenced:
        out.append(f"**{r.concept.label}** — {r.concept.why} Posting says: “{r.quote}”")
    for r in u.by_relation("adjacent"):
        out.append(f"*{r.concept.label}* (adjacent) — {r.concept.why} "
                   f"Posting says: “{r.quote}”")
    for r in u.blocked:
        out.append(f"⚠ **{r.concept.label}** — {r.concept.why} Posting says: “{r.quote}”")
    for r in u.wrong_sense:
        out.append(f"✗ mentions {r.concept.label.split(' (')[0]} but in the wrong "
                   f"sense — “{r.note}”")
    return out


if __name__ == "__main__":
    import sys
    text = sys.stdin.read() if not sys.stdin.isatty() else " ".join(sys.argv[1:])
    u = understand(text)
    print(u.summary())
    print()
    for line in fit_reasons(u):
        print(" -", line)

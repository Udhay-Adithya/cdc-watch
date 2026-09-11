"""Decide what a parsed mail means for one specific student.

Four outcomes, and keeping them distinct is the whole point:

  FOUND     your ID is in the list
  ABSENT    a list was parsed and you are not in it
  UNPARSED  the mail SAYS it carries a list, but none could be read -- loud
  NO_LIST   the mail carries no list and never claimed to -- quiet

The UNPARSED/NO_LIST split came out of real CDC mail. "Shortlist will be
shared by 11 AM" and "Please find the attached shortlist" both mention a
shortlist; only the second one failing is a problem worth waking you up for.
"""
import re
from dataclasses import dataclass, field

from . import ids as ids_mod

FOUND = "FOUND"
ABSENT = "ABSENT"
UNPARSED = "UNPARSED"
NO_LIST = "NO_LIST"

# Real CDC selection lists run as short as ONE student, so any Neo ID at all
# counts as a list. An earlier threshold of 5 mislabelled genuine 1-4 name
# lists as parse failures.
MIN_LIST_SIZE = 1

# Phrases that mean "a list of students is in this mail, right now".
#
# Two lessons from real CDC mail are baked in here. Bare "shortlisted" is not
# enough -- "shortlist will be shared by 15-09-26" is an announcement, not a
# list. And a bare "please find" is not enough either: JD and registration
# mails say "please find the form" or "please find the attached JD", which is
# an attachment but not a roster. So the cue must actually name a roster noun.
_ROSTER = r"(?:short\s?lists?|lists?|students?|candidates?|selections?|selects?)"
_CLAIMS_LIST = [
    r"\b(?:please\s+)?find\s+(?:the\s+|below\s+|attached\s+|herewith\s+|following\s+)*"
    r"(?:\w+\s+){0,3}?" + _ROSTER + r"\b",
    r"\b(?:below|following|attached)\s+(?:is\s+|are\s+)?(?:the\s+)?"
    r"(?:\w+\s+){0,2}?" + _ROSTER + r"\b",
    r"\bselection\s+list\b",
    # "Shortlisted candidates WILL BE provided..." describes what happens to
    # the shortlisted; it does not mean a list is attached. Anything followed
    # by a modal is talking about the future, not presenting a roster.
    r"\bshortlist(?:ed)?\s+(?:students?|candidates?|list)\b"
    r"(?!\s+(?:will|shall|would|may|must|should|can|are\s+(?:asked|advised|required|expected)))",
]
_CLAIMS_RE = re.compile("|".join(_CLAIMS_LIST), re.I)

# Quoted reply lines carry the PARENT mail's claim, not this one's. A reply
# saying only "report to PRP 717" above a quoted "find the below shortlisted
# candidates list" is not itself presenting a list -- and the parent, which
# actually had the sheet attached, is processed on its own.
_QUOTED_LINE = re.compile(r"^\s*>.*$", re.M)


def strip_quotes(text):
    return _QUOTED_LINE.sub("", text or "")


@dataclass
class Verdict:
    status: str
    matched_on: str = ""
    total_ids: int = 0
    name_hit: bool = False
    claims_list: bool = False
    warnings: list = field(default_factory=list)


def claims_list(text, subject=""):
    """Does THIS mail (not a mail it quotes) assert that a list is present?"""
    return bool(_CLAIMS_RE.search("{}\n{}".format(subject or "", strip_quotes(text))))


def _name_tokens(name):
    return [t for t in re.split(r"[^A-Z]+", ids_mod.normalize(name)) if len(t) > 1]


def name_present(text, name):
    """Weak signal: every token of the name appears as a whole word.

    Order-insensitive, so "Udhay Adithya J" still matches "ADITHYA J UDHAY".
    Only ever a fallback hint -- never used to declare ABSENT.
    """
    tokens = _name_tokens(name)
    if not tokens:
        return False
    haystack = ids_mod.normalize(text)
    return all(re.search(r"\b{}\b".format(re.escape(t)), haystack) for t in tokens)


def evaluate(text, me, warnings=(), subject="", claim_text=None):
    """`me` is a dict with keys neo_id, reg_no, name (any may be empty).

    `claim_text` is what gets searched for "a list is attached" phrasing --
    normally the plain-text body, where quoting is still marked with ">".
    IDs are always searched for across the full flattened `text`.
    """
    found_ids = ids_mod.find_neo_ids(text)
    found_regs = ids_mod.find_reg_nos(text)
    found_refs = ids_mod.find_ref_ids(text)
    verdict = Verdict(
        status=NO_LIST,
        total_ids=max(len(found_ids), len(found_regs), len(found_refs)),
        claims_list=claims_list(text if claim_text is None else claim_text, subject),
        warnings=list(warnings),
    )

    neo_id = ids_mod.normalize(me.get("neo_id", "")).strip()
    reg_no = ids_mod.normalize(me.get("reg_no", "")).strip()
    name = me.get("name", "")

    verdict.name_hit = bool(name) and name_present(text, name)

    # Checked before any threshold, so a one-name selection list still fires.
    if neo_id and neo_id in found_ids:
        verdict.status, verdict.matched_on = FOUND, "neo_id"
        return verdict
    if reg_no and reg_no in found_regs:
        verdict.status, verdict.matched_on = FOUND, "reg_no"
        return verdict
    # Drives like TCS and Cognizant publish against their own identifiers.
    # These are exact tokens the user registered, so search the whole text
    # rather than relying on any one format's regex having matched.
    for extra in me.get("extra_ids", ()):
        if ids_mod.token_present(text, extra):
            verdict.status, verdict.matched_on = FOUND, "reference id"
            return verdict

    if verdict.name_hit and not found_ids and not found_regs and not found_refs:
        verdict.status, verdict.matched_on = FOUND, "name"
        return verdict

    if max(len(found_ids), len(found_regs), len(found_refs)) >= MIN_LIST_SIZE:
        verdict.status = ABSENT
    elif verdict.claims_list:
        # It promised a roster and we came up empty: that is a real failure.
        # Note that unreadable attachments alone do NOT land here -- JD and
        # registration mails routinely carry PDFs that are not shortlists.
        verdict.status = UNPARSED
        if len([ln for ln in text.splitlines() if ln.strip()]) >= 20:
            # We clearly read a table, just not one keyed by anything we know.
            # Actionable, so say what to do about it.
            verdict.warnings.append(
                "Read a large table but recognised no Neo IDs -- this drive "
                "likely uses its own identifier (Superset ID, reference no). "
                "Add yours with /addid to match it."
            )
    else:
        verdict.status = NO_LIST
    return verdict

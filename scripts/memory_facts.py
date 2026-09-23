"""memory_facts.py - the rule that keeps authoritative values out of Memory Bank.

THE RULE
    A memory may describe a situation, a preference, or an instruction. It may
    not be the source of an amount, a deadline, or a dispute id. Those live in
    BigQuery and are read at use time.

WHY THE RULE EXISTS
    A memory is written once and read for months. A dispute amount changes when
    a chargeback lands; a Reg E deadline moves when the filing date moves; a
    dispute id is reassigned when a case is split. A memory that quoted any of
    them keeps answering with the old value long after BigQuery has moved on,
    and nothing in the system marks it stale. That is how a demo ends up
    telling a customer "EUR 412" about a USD 1,284.60 dispute.

    Note the failure is silent. The memory still retrieves, still reads
    fluently, and still sounds authoritative. So the defence has to be at write
    time - refuse the write - rather than a reader-side warning nobody sees.

WHAT THIS MODULE IS
    A write-time guard. `assert_no_authoritative_values(text)` raises if a
    candidate memory string carries one of those values, and the exception says
    which pattern matched and why that value belongs in BigQuery instead.

    Stdlib only and no I/O, so it runs anywhere - including a reviewer's laptop
    with no GCP credentials - and can be imported by the seeding script, the
    reconciler, or a test.

DELIBERATELY STRICT
    The guard rejects record ids of any shape (DSP.., CUST.., CARD..), not only
    dispute ids. A memory that names a row is a memory that has to be rewritten
    when the row moves. Describe the thing in words; let the query supply the
    id.

    It also rejects any bare number of three digits or more. "1480" carries no
    currency marker but is still an amount someone will read as one. Two-digit
    numbers survive, which is what lets a trip window like "03-12 Sep" through.
"""

from __future__ import annotations

import re
from typing import Iterable, List, NamedTuple, Optional

__all__ = [
    "AuthoritativeValueError",
    "Violation",
    "scan",
    "is_clean",
    "assert_no_authoritative_values",
    "RULES",
]


class Violation(NamedTuple):
    """One rule hit: which rule, the exact text that matched, and the reason."""

    rule: str
    matched: str
    span: str          # "start:end" character offsets, so a caller can highlight
    why: str


class AuthoritativeValueError(ValueError):
    """A candidate memory carried a value that BigQuery owns.

    Subclasses ValueError so a caller that only cares "bad input" keeps
    working, while a caller that wants the detail can read `.violations`.
    """

    def __init__(self, text: str, violations: List[Violation]):
        self.text = text
        self.violations = violations
        super().__init__(format_violations(text, violations))


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

# Ordered most-specific first: the first matching rule produces the clearest
# message, e.g. "EUR 412" should be reported as a currency amount rather than
# as a bare quantity that happens to start with a letter.
# ISO codes cover facts we author by hand ("EUR 412"). They do not cover what
# Memory Bank writes back: the service's own extraction produces prose, so a
# live memory says "412 euros", not "EUR 412". With codes alone the
# authoritative profile matched nothing on the real corpus and the reconciler
# reported PASS over memories that plainly assert amounts. The spelled-out
# names below are the difference between checking the facts we wrote and
# checking the facts that are actually stored.
_CURRENCY_WORDS = (
    r"EUR|USD|GBP|JPY|CHF|CAD|AUD|SEK|NOK|DKK|PLN|BRL|INR|MXN"
    r"|euros?|dollars?|pounds?\s+sterling|pounds?|yen|francs?|rupees?"
    r"|pesos?|reais|real|kronor|krona|kroner|krone|z\u0142oty|zloty"
)
_CURRENCY_SYMBOLS = r"\$|\u20ac|\u00a3|\u00a5"
_MONTHS = (r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
           r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?")

_WHY_AMOUNT = (
    "an amount belongs to the disputes/authorizations tables in BigQuery, which "
    "are read at use time. A memory holding an amount becomes a second source of "
    "truth that nothing updates when the ledger changes."
)
_WHY_DEADLINE = (
    "a regulatory deadline is derived from the filing date in BigQuery every time "
    "it is needed. A memory holding a date keeps quoting it after the case moves, "
    "and a wrong Reg E date is a compliance defect, not a wording defect."
)
_WHY_DISPUTE_ID = (
    "a dispute id identifies a row that BigQuery owns and can reassign when a case "
    "is split or merged. Say what the dispute is about; let the query supply the id."
)
_WHY_RECORD_ID = (
    "a record id points at a specific row. Memories outlive rows, so a memory that "
    "names one is stale the moment the row moves. Describe the thing in words."
)
_WHY_DURATION = (
    "an investigation or provisional-credit window is policy plus the filing date, "
    "both of which are looked up. A memory asserting a number of days will contradict "
    "the rulebook the first time the policy or the filing date changes."
)
_WHY_QUANTITY = (
    "an unlabelled number of this magnitude reads as an amount to whoever retrieves "
    "it, whether or not a currency marker is attached. If it is a quantity that "
    "matters, it has a home in BigQuery; if it does not matter, it does not belong "
    "in the memory."
)

RULES = (
    (
        "currency_amount",
        re.compile(r"(?:%s|%s)\s?\d[\d,.]*|\d[\d,.]*\s?(?:%s)\b"
                   % (_CURRENCY_WORDS, _CURRENCY_SYMBOLS, _CURRENCY_WORDS), re.IGNORECASE),
        _WHY_AMOUNT,
    ),
    (
        "monetary_shaped_number",
        # 1,284.60 / 3,940.15 / 412.00 - grouping or cents means somebody meant money.
        re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+\.\d{2}\b"),
        _WHY_AMOUNT,
    ),
    (
        "dispute_id",
        re.compile(r"\b(?:DSP|DISP|CASE|CLM|CLAIM|TKT)[-_ ]?\d{3,}\b", re.IGNORECASE),
        _WHY_DISPUTE_ID,
    ),
    (
        "record_id",
        re.compile(r"\b[A-Z]{2,6}[-_]?\d{4,}\b"),
        _WHY_RECORD_ID,
    ),
    (
        "dated_deadline",
        # A date carrying a year: ISO, 09 Sep 2026, Sep 9 2026, 09/09/2026.
        re.compile(r"\b\d{4}-\d{2}-\d{2}\b"
                   r"|\b\d{1,2}[ -](?:%s)[ -]\d{4}\b"
                   r"|\b(?:%s)[ -]\d{1,2},?[ -]\d{4}\b"
                   r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b" % (_MONTHS, _MONTHS), re.IGNORECASE),
        _WHY_DEADLINE,
    ),
    (
        "deadline_phrase",
        # "due 23 Sep", "by the 23rd", "no later than Tuesday", "deadline is ..."
        re.compile(r"\b(?:due|deadline|no later than|expires?|expiry|cut[- ]?off)\b"
                   r"[^.;]{0,40}?(?:\d|\b(?:%s)\b|\bmonday\b|\btuesday\b|\bwednesday\b"
                   r"|\bthursday\b|\bfriday\b|\bsaturday\b|\bsunday\b)" % _MONTHS,
                   re.IGNORECASE),
        _WHY_DEADLINE,
    ),
    (
        "duration_window",
        re.compile(r"\b\d+\s*(?:calendar|business|working|banking)?[ -]?"
                   r"(?:day|days|week|weeks|month|months|billing cycle|billing cycles)\b",
                   re.IGNORECASE),
        _WHY_DURATION,
    ),
    (
        "bare_quantity",
        re.compile(r"(?<![\w.,])\d{3,}(?![\w.,])"),
        _WHY_QUANTITY,
    ),
)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def scan(text: str) -> List[Violation]:
    """Returns every rule hit in `text`, most-specific rule first.

    Returns a list rather than the first hit so an operator fixing a bad fact
    sees everything wrong with it in one pass instead of one error per run.
    """
    if text is None:
        raise TypeError("scan() needs a string; None is not a memory.")

    found: List[Violation] = []
    claimed: List[range] = []   # offsets already explained by an earlier, more specific rule

    for rule_name, pattern, why in RULES:
        for match in pattern.finditer(text):
            start, end = match.span()
            # Overlap, not containment: "due 23 Sep 2026" should be reported once
            # as a dated deadline, not again as the fragment "due 2".
            if any(start < r.stop and end > r.start for r in claimed):
                continue
            claimed.append(range(start, end))
            found.append(Violation(rule=rule_name,
                                   matched=match.group(0),
                                   span="%d:%d" % (start, end),
                                   why=why))
    return found


def is_clean(text: str) -> bool:
    return not scan(text)


def format_violations(text: str, violations: Iterable[Violation]) -> str:
    lines = ["memory rejected - this fact carries a value that BigQuery owns.",
             "  fact    : %r" % text]
    for index, violation in enumerate(violations, start=1):
        lines.append("  hit %-2d  : %r  [rule: %s, at chars %s]"
                     % (index, violation.matched, violation.rule, violation.span))
        lines.append("            why: %s" % violation.why)
    lines.append("  fix     : state the situation in words and let the reader "
                 "fetch the number, date or id from BigQuery at use time.")
    return "\n".join(lines)


def assert_no_authoritative_values(text: str, label: Optional[str] = None) -> str:
    """Raises AuthoritativeValueError unless `text` is safe to store as a memory.

    Returns the text on success so it can be used inline at the call site:
        client.create_memory(assert_no_authoritative_values(fact), scope)
    """
    violations = scan(text)
    if violations:
        if label:
            raise AuthoritativeValueError("%s: %s" % (label, text), violations)
        raise AuthoritativeValueError(text, violations)
    return text


# ---------------------------------------------------------------------------
# Self-test - runnable with no GCP, no network, no test framework
# ---------------------------------------------------------------------------

# Left column: strings taken from the pre-fix memory blob and the scenario text
# it was built from. Right column: the grain a memory is allowed to have.
MUST_REJECT = [
    "EUR 412",
    "1480",
    "Customer disputes EUR 412 at Casa Verde Electronics in Lisbon",
    "Disputed credit card total EUR 1,480.00",
    "USD 1,284.60 dispute opened on the debit card",
    "DSP00034",
    "Dispute DSP00033 is running under Regulation E",
    "Provisional credit due 23 Sep 2026",
    "Foreign-initiated investigation window is 90 calendar days",
    "Resolve within 2 billing cycles",
    "Primary card on file is CARD00002",
    "Customer CUST00001 reported a stolen wallet",
]

MUST_ACCEPT = [
    "Travelling in Lisbon 03-12 Sep",
    "Notify before closing any dispute",
    "Reported the wallet stolen while in Lisbon",
    "Both the debit card and the credit card were in the stolen wallet",
    "Asked for both cards to be frozen straight away",
    "Disputes the Lisbon charges made after the wallet was taken",
    "Needs a working card for the rest of the trip",
    "Accepted a virtual card as the immediate stand-in",
    "Replacement card goes to the hotel, not the home address",
]


def _self_test() -> int:
    failures = 0

    print("=" * 74)
    print("MUST REJECT - strings the pre-fix memory blob asserted")
    print("=" * 74)
    for text in MUST_REJECT:
        violations = scan(text)
        if violations:
            first = violations[0]
            print("  REJECTED  %-62r rule=%s matched=%r"
                  % (text, first.rule, first.matched))
        else:
            failures += 1
            print("  LEAKED    %-62r *** guard did not catch this ***" % text)

    print()
    print("=" * 74)
    print("MUST ACCEPT - atomic facts at the right grain")
    print("=" * 74)
    for text in MUST_ACCEPT:
        violations = scan(text)
        if violations:
            failures += 1
            print("  BLOCKED   %-62r *** false positive: %s on %r ***"
                  % (text, violations[0].rule, violations[0].matched))
        else:
            print("  ACCEPTED  %r" % text)

    print()
    print("=" * 74)
    print("FULL REJECTION MESSAGE - what the seeding script prints and refuses on")
    print("=" * 74)
    try:
        assert_no_authoritative_values(
            "Customer disputes EUR 412 and EUR 1,480.00 on DSP00034; "
            "provisional credit due 23 Sep 2026 within 90 calendar days."
        )
    except AuthoritativeValueError as error:
        print(error)
    else:
        failures += 1
        print("  *** the compound bad fact was accepted ***")

    print()
    print("result: %d failure(s)" % failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())


# ---------------------------------------------------------------------------
# Two profiles, because there are two different jobs
# ---------------------------------------------------------------------------
#
# The strict rules above are calibrated for facts WE author. They are
# deliberately blunt: any 3+ digit number, any record id. That is the right
# setting when a human or an LLM is writing a new memory, because the cheapest
# moment to keep an amount out is before it is written.
#
# It is the wrong setting for the corpus the demo kit already ships. Those 77
# seeded facts are genuinely atomic and genuinely useful, and 61 of them trip a
# strict rule -- on things like "the card ending 4417", "the mobile ending
# 0143", or a plain event date "on 2026-09-05". None of those is an amount, a
# deadline, or a dispute id. Running strict over that corpus would reject the
# real data and leave the demo with nothing to retrieve, which is the failure
# this rework exists to prevent, arriving by the opposite road.
#
# So: `authoritative` enforces exactly the three categories the rule names --
# an amount, a deadline, a dispute id -- and nothing else.

AUTHORITATIVE_RULES = frozenset({
    "currency_amount",
    "monetary_shaped_number",
    "dispute_id",
    # `deadline_phrase` and `duration_window` carry promise language -- "by",
    # "due", "within N days". `dated_deadline` is deliberately NOT here: it
    # matches any ISO date, and most dates in a memory are event dates ("card
    # reported lost on 2026-09-05", "travelled to Toronto from 2026-08-14").
    # An event date is history and cannot go stale. Treating it as a deadline
    # rejected 40 of the kit's 77 facts for saying when something happened.
    "deadline_phrase",
    "duration_window",
})

# Real signals, but about durability rather than authority. A memory naming a
# row is a maintenance problem; it is not a second source of truth for money.
HYGIENE_RULES = frozenset({
    "record_id",
    "bare_quantity",
})

PROFILES = {
    "strict": None,                     # every rule; for facts we author
    "authoritative": AUTHORITATIVE_RULES,  # amount / deadline / dispute id only
}


def scan_profile(text: str, profile: str = "strict") -> List[Violation]:
    """Scan under a named profile. See PROFILES for what each one enforces."""
    if profile not in PROFILES:
        raise ValueError(
            "unknown profile %r; choose one of: %s" % (profile, ", ".join(sorted(PROFILES)))
        )
    allowed = PROFILES[profile]
    violations = scan(text)
    if allowed is None:
        return violations
    return [v for v in violations if v.rule in allowed]


def carries_authoritative_value(text: str) -> bool:
    """True if this memory asserts an amount, a deadline, or a dispute id.

    The read path uses this rather than the exception: a memory that carries
    one is still worth retrieving for its narrative, it just must never be the
    place the number is read from.
    """
    return bool(scan_profile(text, "authoritative"))


def classify(text: str) -> dict:
    """Describes what a memory carries, without judging whether to keep it.

    Returned so a caller can retain a fact and still refuse to source a value
    from it -- the distinction the whole scenario-alignment item turns on.
    """
    authoritative = scan_profile(text, "authoritative")
    hygiene = [v for v in scan(text) if v.rule in HYGIENE_RULES]
    return {
        "safe_to_author": not scan(text),
        "safe_to_ingest": not authoritative,
        "authoritative_values": sorted({v.rule for v in authoritative}),
        "hygiene_notes": sorted({v.rule for v in hygiene}),
        "never_source_from_memory": bool(authoritative),
    }


def assert_safe_to_ingest(text: str, label: Optional[str] = None) -> str:
    """Gate for seeding the kit's existing corpus.

    Raises only when the fact would make memory the source of an amount, a
    deadline, or a dispute id.
    """
    violations = scan_profile(text, "authoritative")
    if violations:
        raise AuthoritativeValueError(label or text, violations)
    return text


__all__ = list(__all__) + [
    "AUTHORITATIVE_RULES",
    "HYGIENE_RULES",
    "PROFILES",
    "scan_profile",
    "carries_authoritative_value",
    "classify",
    "assert_safe_to_ingest",
]

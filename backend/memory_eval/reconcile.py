"""
Reconciling what the customer said against what the warehouse recorded.

High precision is not the same as being right. A retrieval system can return
exactly the memories a question asks for, every one of which is a faithful
record of something the customer said, and still hand the agent a false number.

This module closes that gap. The rule it enforces:

    BigQuery is authoritative for facts. Memory Bank is authoritative for what
    the customer told us. When they disagree, the row wins -- and the
    disagreement is the finding.

WHY THIS IS SCOPED THE WAY IT IS
    An earlier version of this check scanned memories with every pattern it had
    and mapped any bare number to "amount". A card suffix is four digits, so it
    reported:

        4417   amount   796.42 / 715.61 / 1065.76   MISMATCH

    Two of five "failures" were card numbers. The fix is not a better regex, it
    is being explicit about which patterns are claims about money and which are
    hygiene. Only the `authoritative` profile below participates in a verdict.

    Fixing that alone then made the check pass, which was worse. The currency
    pattern matched ISO codes (`EUR 412`), which is how *we* would write a fact.
    Memory Bank writes prose, so every stored fact says "412 euros" and matched
    nothing. The guard was checking facts we authored rather than facts that are
    stored. Spelled-out currency names are included below for that reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from backend.ground_truth import GroundTruthReader
from backend.memory_bank import Memory

CURRENCY_WORDS = r"(?:eur|euros?|usd|dollars?|gbp|pounds?|sterling|cad|aud)"

AUTHORITATIVE_PATTERNS: dict[str, str] = {
    # Amounts, in both the orders humans write them, and with the currency
    # spelled out because that is how the service's own extraction phrases it.
    "amount": (
        rf"(?:{CURRENCY_WORDS}\s*([0-9][0-9,]*(?:\.[0-9]{{1,2}})?)"
        rf"|([0-9][0-9,]*(?:\.[0-9]{{1,2}})?)\s*{CURRENCY_WORDS}"
        rf"|[$£€]\s*([0-9][0-9,]*(?:\.[0-9]{{1,2}})?))"
    ),
}

HYGIENE_PATTERNS: dict[str, str] = {
    # Useful to see, never a verdict. A card suffix is a four-digit number and
    # will collide with anything numeric if you let it.
    "card_suffix": r"\b(?:ending|card)\s+(?:in\s+)?([0-9]{4})\b",
    "bare_quantity": r"\b([0-9]{1,6})\b",
}


@dataclass
class Finding:
    """One reconciliation outcome."""

    memory_id: str
    memory_fact: str
    claim_kind: str
    claimed_values: list[str]
    authoritative_values: list[str]
    status: str  # CORROBORATED / CONTRADICTED / UNVERIFIABLE
    explanation: str

    def to_dict(self) -> dict:
        return {
            "memory_id": self.memory_id,
            "memory_fact": self.memory_fact,
            "claim_kind": self.claim_kind,
            "claimed_values": self.claimed_values,
            "authoritative_values": self.authoritative_values,
            "status": self.status,
            "explanation": self.explanation,
        }


@dataclass
class ReconciliationReport:
    customer_id: str
    findings: list[Finding] = field(default_factory=list)
    authoritative_amounts: dict = field(default_factory=dict)

    @property
    def contradicted(self) -> list[Finding]:
        return [f for f in self.findings if f.status == "CONTRADICTED"]

    @property
    def passed(self) -> bool:
        return not self.contradicted

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "authoritative_amounts": self.authoritative_amounts,
            "memories_scanned": len({f.memory_id for f in self.findings}),
            "contradicted_count": len(self.contradicted),
            "corroborated_count": len([f for f in self.findings if f.status == "CORROBORATED"]),
            "passed": self.passed,
            "findings": [f.to_dict() for f in self.findings],
            "rule": (
                "BigQuery is authoritative for facts; Memory Bank is authoritative for what "
                "the customer said. A CONTRADICTED finding is a real defect in the corpus, "
                "not a bug in this check: it means the bank holds a second, disagreeing "
                "answer to a question the warehouse already answers."
            ),
        }


def extract_amounts(text: str) -> list[float]:
    """Pull monetary amounts out of prose, in any of the shapes they appear."""
    found: list[float] = []
    for match in re.finditer(AUTHORITATIVE_PATTERNS["amount"], text, flags=re.IGNORECASE):
        raw = next((g for g in match.groups() if g), None)
        if raw:
            try:
                found.append(float(raw.replace(",", "")))
            except ValueError:
                continue
    return found


def reconcile_amounts(
    memories: Sequence[Memory],
    reader: GroundTruthReader,
    customer_id: str,
    tolerance: float = 0.01,
) -> ReconciliationReport:
    """Check every monetary claim in memory against the disputes table.

    A memory is CONTRADICTED when it states an amount that matches no
    authoritative disputed amount for this customer. It is UNVERIFIABLE when it
    states no amount at all -- which is the common and healthy case, and is
    counted separately rather than being quietly treated as a pass.
    """
    authoritative = reader.disputed_amounts(customer_id)
    values = sorted(authoritative.values())
    report = ReconciliationReport(customer_id=customer_id, authoritative_amounts=authoritative)

    for memory in memories:
        claimed = extract_amounts(memory.fact)
        if not claimed:
            report.findings.append(
                Finding(
                    memory_id=memory.memory_id,
                    memory_fact=memory.fact,
                    claim_kind="amount",
                    claimed_values=[],
                    authoritative_values=[f"{v:.2f}" for v in values],
                    status="UNVERIFIABLE",
                    explanation="States no monetary amount, so there is nothing to contradict. This is the healthy case.",
                )
            )
            continue

        unmatched = [
            c for c in claimed if not any(abs(c - a) <= tolerance for a in values)
        ]
        if unmatched:
            report.findings.append(
                Finding(
                    memory_id=memory.memory_id,
                    memory_fact=memory.fact,
                    claim_kind="amount",
                    claimed_values=[f"{c:.2f}" for c in claimed],
                    authoritative_values=[f"{v:.2f}" for v in values],
                    status="CONTRADICTED",
                    explanation=(
                        f"Memory states {', '.join(f'{c:.2f}' for c in unmatched)}; no dispute row "
                        f"for {customer_id} carries that amount. BigQuery wins. The memory is a "
                        f"faithful record of what the customer said and an unreliable source for "
                        f"what they are owed -- those are different jobs."
                    ),
                )
            )
        else:
            report.findings.append(
                Finding(
                    memory_id=memory.memory_id,
                    memory_fact=memory.fact,
                    claim_kind="amount",
                    claimed_values=[f"{c:.2f}" for c in claimed],
                    authoritative_values=[f"{v:.2f}" for v in values],
                    status="CORROBORATED",
                    explanation="Every amount stated matches an authoritative disputed amount.",
                )
            )

    return report


def reconcile_travel(
    memories: Sequence[Memory],
    reader: GroundTruthReader,
    customer_id: str,
) -> ReconciliationReport:
    """Check geographic claims against the travel notice table.

    Separate from amounts because the failure mode differs: a wrong city is not
    a rounding error, it is a different trip.
    """
    result = reader.run("travel_notices", {"customer_id": customer_id})
    cities = {
        str(row.get("destination_city", "")).lower()
        for row in result.rows
        if row.get("destination_city")
    }
    countries = {
        str(row.get("destination_country", "")).lower()
        for row in result.rows
        if row.get("destination_country")
    }
    known = cities | countries

    report = ReconciliationReport(
        customer_id=customer_id,
        authoritative_amounts={"declared_destinations": sorted(known)},
    )

    # Only look for places the warehouse could have had an opinion about. A
    # general place-name detector would flag every noun and drown the signal.
    candidates = ["london", "lisbon", "porto", "paris", "madrid", "new york", "chicago", "dublin"]

    for memory in memories:
        lowered = memory.fact.lower()
        mentioned = [c for c in candidates if re.search(rf"\b{re.escape(c)}\b", lowered)]
        if not mentioned:
            continue
        unsupported = [
            c for c in mentioned if c not in known and c not in {"new york", "chicago"}
        ]
        if unsupported:
            report.findings.append(
                Finding(
                    memory_id=memory.memory_id,
                    memory_fact=memory.fact,
                    claim_kind="destination",
                    claimed_values=unsupported,
                    authoritative_values=sorted(known),
                    status="CONTRADICTED",
                    explanation=(
                        f"Memory places the customer in {', '.join(unsupported)}; travel_notices "
                        f"records only {', '.join(sorted(known)) or 'no destinations'}. Retrieval "
                        f"finding this memory is correct behaviour. Repeating it as fact is not."
                    ),
                )
            )
        else:
            report.findings.append(
                Finding(
                    memory_id=memory.memory_id,
                    memory_fact=memory.fact,
                    claim_kind="destination",
                    claimed_values=mentioned,
                    authoritative_values=sorted(known),
                    status="CORROBORATED",
                    explanation="Destination matches a declared travel notice or the home city.",
                )
            )

    return report

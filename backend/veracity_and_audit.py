"""
Pre-write validation: deciding what a turn is allowed to assert.

The file this replaces was 1,022 lines and its own docstring claimed "live
Vertex AI Gemini 2.5 Flash structured output (zero hardcoded string-matching)".
It was almost entirely hardcoded string matching. `_algorithmic_telemetry_evaluation`
routed a claim through an if/elif chain of keyword lists and returned one of five
pre-written verdicts, each with its confidence baked in (`0.96`, `0.99`, `0.98`,
`0.99`, `0.95`). A Gemini call was then made, and its output was *overwritten*
with the canned verdict before being returned -- and labelled with the model name.
The "live Verification Confidence Score (0%-100%)" the README advertised had
exactly two possible values: `0.98` and `0.14`.

What is worth keeping is the idea underneath it: something should check a claim
against the system of record *before* it is allowed to become a memory, because
a memory bank that faithfully records a false statement will repeat it with
confidence for years.

So this module is that check, and only that check. It is small because the hard
work is done by modules that already exist: the warehouse is authoritative
(`ground_truth`), the comparison is already written (`memory_eval.reconcile`),
and the catalog decides what may be read (`knowledge_catalog`).

WHAT IT DELIBERATELY DOES NOT DO
    It does not block writes. `generateMemories` decides what to store, and
    that is the right place for the decision -- see the write-policy evidence.
    This produces a *verdict attached to the turn*, which the desk can act on
    and a reviewer can audit. A validator that silently dropped a customer's
    statement because a table disagreed would be destroying the one record of
    what the customer actually said.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

from backend.config import Settings, settings as load_settings
from backend.ground_truth import GroundTruthError, GroundTruthReader
from backend.knowledge_catalog import CatalogGovernor
from backend.memory_bank import Memory, MemoryBankClient
from backend.memory_eval import reconcile
from backend.models import ClaimStatus
from backend.provenance import Provenanced, measured, unmeasured

log = logging.getLogger(__name__)


@dataclass
class ClaimVerdict:
    """The outcome of checking one turn's claims against the record."""

    customer_id: str
    claim_text: str
    status: ClaimStatus
    findings: list[dict] = field(default_factory=list)
    authoritative_amounts: dict = field(default_factory=dict)
    latency_ms: Provenanced = field(default_factory=lambda: unmeasured("not timed", unit="ms"))
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "claim_text": self.claim_text,
            "status": self.status.value,
            "findings": self.findings,
            "authoritative_amounts": self.authoritative_amounts,
            "latency_ms": self.latency_ms.to_dict(),
            "note": self.note,
            "confidence": (
                "Deliberately absent. A confidence score here would be a number with no "
                "method behind it. The status is derived from whether specific stated "
                "values match specific warehouse rows, and the findings list shows which."
            ),
        }


class ClaimValidator:
    """Checks what a customer said against what the warehouse recorded."""

    def __init__(
        self,
        cfg: Optional[Settings] = None,
        reader: Optional[GroundTruthReader] = None,
        governor: Optional[CatalogGovernor] = None,
    ) -> None:
        self.cfg = cfg or load_settings()
        self.governor = governor or CatalogGovernor(self.cfg)
        self.reader = reader or GroundTruthReader(self.cfg, governor=self.governor)

    def validate(self, customer_id: str, claim_text: str) -> ClaimVerdict:
        """Check a single stated claim.

        The claim is wrapped in a throwaway `Memory` so that it goes through
        exactly the same reconciliation path as a stored memory does. Using one
        code path for both means a claim cannot pass validation and then be
        flagged by the reconciler an hour later, which is the sort of
        inconsistency that makes people stop trusting both.
        """
        started = time.perf_counter()
        candidate = Memory(name="", fact=claim_text, scope={"customer_id": customer_id})

        try:
            amounts = reconcile.reconcile_amounts([candidate], self.reader, customer_id)
            travel = reconcile.reconcile_travel([candidate], self.reader, customer_id)
        except GroundTruthError as exc:
            return ClaimVerdict(
                customer_id=customer_id,
                claim_text=claim_text,
                status=ClaimStatus.UNVERIFIABLE,
                latency_ms=unmeasured(f"warehouse unavailable: {exc}", unit="ms"),
                note=(
                    "The system of record could not be reached, so this claim was not "
                    "checked. UNVERIFIABLE, not CORROBORATED -- an unchecked claim is not "
                    "a verified one."
                ),
            )

        findings = [f.to_dict() for f in (*amounts.findings, *travel.findings)]
        contradicted = [*amounts.contradicted, *travel.contradicted]
        checked = [f for f in findings if f["status"] != "UNVERIFIABLE"]

        if contradicted:
            status = ClaimStatus.CONTRADICTED
            note = (
                f"{len(contradicted)} statement(s) disagree with the system of record. "
                f"The record is correct. The claim remains a faithful account of what the "
                f"customer said and should be recorded as such, not as fact."
            )
        elif checked:
            status = ClaimStatus.CORROBORATED
            note = "Every checkable value in this claim matches the system of record."
        else:
            status = ClaimStatus.UNVERIFIABLE
            note = (
                "Nothing in this claim is checkable against the warehouse -- it states no "
                "amount and names no destination. That is the common case and is not a "
                "failure, but it is not corroboration either."
            )

        return ClaimVerdict(
            customer_id=customer_id,
            claim_text=claim_text,
            status=status,
            findings=findings,
            authoritative_amounts=amounts.authoritative_amounts,
            latency_ms=measured(round((time.perf_counter() - started) * 1000, 1), unit="ms"),
            note=note,
        )


@dataclass
class CorpusAudit:
    """A sweep over everything currently stored for a customer."""

    customer_id: str
    memories_scanned: int
    contradicted: list[dict]
    corroborated: int
    unverifiable: int
    topic_distribution: dict
    untagged: list[str]
    latency_ms: Provenanced

    @property
    def clean(self) -> bool:
        return not self.contradicted

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "memories_scanned": self.memories_scanned,
            "clean": self.clean,
            "contradicted_count": len(self.contradicted),
            "contradicted": self.contradicted,
            "corroborated_count": self.corroborated,
            "unverifiable_count": self.unverifiable,
            "topic_distribution": self.topic_distribution,
            "untagged_memory_ids": self.untagged,
            "latency_ms": self.latency_ms.to_dict(),
            "interpretation": (
                "A contradicted memory is a real defect in the corpus, not a bug in this "
                "sweep. It means the bank holds a second, disagreeing answer to a question "
                "the warehouse already answers, and an agent that retrieves it without "
                "reconciling will repeat it."
            ),
        }


class MemoryCorpusAuditor:
    """Sweeps a customer's stored memories against the warehouse.

    Distinct from `ClaimValidator`, which checks one claim before it is written.
    This checks what is already there, which matters because memories are
    consolidated and rewritten by the service over time: a corpus that was clean
    when each fact was written can drift.
    """

    def __init__(
        self,
        cfg: Optional[Settings] = None,
        memory: Optional[MemoryBankClient] = None,
        reader: Optional[GroundTruthReader] = None,
    ) -> None:
        self.cfg = cfg or load_settings()
        self.memory = memory or MemoryBankClient(self.cfg)
        self.reader = reader or GroundTruthReader(self.cfg)

    def audit(self, customer_id: str) -> CorpusAudit:
        started = time.perf_counter()
        memories = self.memory.list_memories({"customer_id": customer_id})

        amounts = reconcile.reconcile_amounts(memories, self.reader, customer_id)
        travel = reconcile.reconcile_travel(memories, self.reader, customer_id)
        all_findings = [*amounts.findings, *travel.findings]

        distribution: dict[str, int] = {}
        untagged: list[str] = []
        for memory in memories:
            if not memory.topics:
                untagged.append(memory.memory_id)
            for topic in memory.topics:
                distribution[topic] = distribution.get(topic, 0) + 1

        return CorpusAudit(
            customer_id=customer_id,
            memories_scanned=len(memories),
            contradicted=[f.to_dict() for f in (*amounts.contradicted, *travel.contradicted)],
            corroborated=len([f for f in all_findings if f.status == "CORROBORATED"]),
            unverifiable=len([f for f in all_findings if f.status == "UNVERIFIABLE"]),
            topic_distribution=dict(sorted(distribution.items(), key=lambda kv: -kv[1])),
            untagged=untagged,
            latency_ms=measured(round((time.perf_counter() - started) * 1000, 1), unit="ms"),
        )

    def audit_all(self, customer_ids: Optional[Sequence[str]] = None) -> dict:
        """Sweep several customers.

        Ids come from the warehouse when not supplied. The previous
        implementation called `get_all_customer_ids()`, which force-constructed
        two hardcoded customer objects and returned their keys -- so the
        enterprise-wide sweep always swept exactly the same two fictional people.
        """
        ids = list(customer_ids) if customer_ids else self.reader.customer_ids(limit=25)
        audits = [self.audit(cid) for cid in ids]
        return {
            "customers_swept": len(audits),
            "customers_with_contradictions": [a.customer_id for a in audits if not a.clean],
            "total_memories": sum(a.memories_scanned for a in audits),
            "total_contradictions": sum(len(a.contradicted) for a in audits),
            "audits": [a.to_dict() for a in audits],
        }


# Names the previous module exported. Kept so imports elsewhere keep working.
ClaimVeracityValidatorAgent = ClaimValidator
MemoryBankAuditAgent = MemoryCorpusAuditor

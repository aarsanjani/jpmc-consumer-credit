"""
The fraud desk agent.

The defect this file exists to fix is the one you identified: the demo wrote to
Memory Bank and never read from it. On `main`, `MemoryBankSynthesizerAgent`
called `memory_bank.get_formatted_context()` -- a local Python list -- and then,
if the Gemini call threw, returned a five-paragraph narrative hardcoded at
`agent.py:94-100`, naming specific dollar amounts and a policy id, stamped
`confidence_score=0.99`. A total Vertex outage produced a confident, specific,
wrong answer that no caller could distinguish from a real one.

What a turn does now, in order:

    1. RETRIEVE from Memory Bank. A real `memories:retrieve` against the
       customer's scope. This is the read that did not exist.

    2. RECONCILE every retrieved memory against BigQuery. A memory is what the
       customer *said*; a row is what *happened*. Contradicted memories are kept
       in the evidence and flagged, not silently dropped -- the agent needs to
       know the customer believes something untrue.

    3. GROUND the vocabulary in Dataplex. Regulatory terms are resolved from the
       glossary, so the answer to "why is there no provisional credit date"
       comes from agreed definitions rather than from a sentence someone wrote
       into a prompt.

    4. SYNTHESISE with Gemini over that evidence, under rules that forbid
       inventing a number.

    5. WRITE the turn back with `generateMemories`, letting the service decide
       what was worth keeping.

There is no canned fallback anywhere in this file. If a step fails the response
says which step failed and why. A desk that cannot reach its evidence has
nothing to say, and saying nothing is the correct output.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from google import genai
from google.genai import types

from backend.config import Settings, settings as load_settings
from backend.ground_truth import GroundTruthError, GroundTruthReader
from backend.knowledge_catalog import CatalogError, CatalogGovernor
from backend.memory_bank import Memory, MemoryBankClient, MemoryBankError
from backend.memory_eval import reconcile
from backend.provenance import ProvenanceLedger, measured, reported, unmeasured

log = logging.getLogger(__name__)

SYNTHESIS_RULES = """\
You are a consumer-card fraud desk assistant. You are given evidence gathered \
from three systems and you answer the customer's question from that evidence \
alone.

The evidence has provenance, and you must respect it:

- WAREHOUSE facts come from the bank's system of record. They are authoritative.
- MEMORY facts are things this customer told us on previous contacts. They are \
  authoritative about what was *said*, not about what is *true*.
- GLOSSARY entries are the bank's agreed definitions of regulatory terms.

Rules:

1. If a MEMORY is marked CONTRADICTED, the warehouse disagrees with it. Use the \
   warehouse figure. You may acknowledge what the customer previously said, but \
   never repeat a contradicted figure as fact.
2. Never state a number that does not appear in the evidence. If the evidence \
   does not contain it, say it was not available. Do not estimate.
3. If a value is marked n/r it was not measured. Say so. Never substitute a \
   figure, and never write 0 in its place.
4. Nothing in the catalog blocked any read. Where a source was not consulted, \
   the agent declined on catalog guidance. Never tell the customer that a system \
   prevented you from seeing something.
5. Answer the question that was asked. Do not recite the whole case file.
6. If the evidence does not answer the question, say that plainly and say what \
   would be needed. A short honest answer is better than a long confident one.
"""


@dataclass
class Evidence:
    """Everything gathered for one turn, with its provenance."""

    customer_id: str
    question: str
    memories: list[Memory] = field(default_factory=list)
    contradicted_ids: set[str] = field(default_factory=set)
    reconciliation: dict = field(default_factory=dict)
    warehouse: dict = field(default_factory=dict)
    glossary: list[dict] = field(default_factory=list)
    governance: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    ledger: ProvenanceLedger = field(default_factory=ProvenanceLedger)

    def memory_block(self) -> str:
        if not self.memories:
            return "(no memories retrieved for this customer)"
        lines = []
        for memory in self.memories:
            flag = " [CONTRADICTED BY WAREHOUSE]" if memory.memory_id in self.contradicted_ids else ""
            topics = ",".join(memory.topics) or "untagged"
            distance = f"{memory.distance:.3f}" if memory.distance is not None else "n/r"
            lines.append(f"- ({topics}, distance {distance}){flag} {memory.fact}")
        return "\n".join(lines)

    def warehouse_block(self) -> str:
        if not self.warehouse:
            return "(no warehouse data available)"
        return json.dumps(self.warehouse, indent=2, default=str)[:6000]

    def glossary_block(self) -> str:
        if not self.glossary:
            return "(no glossary terms resolved)"
        return "\n".join(f"- {t['display_name']}: {t['description']}" for t in self.glossary)

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "question": self.question,
            "memories": [m.to_dict() for m in self.memories],
            "contradicted_memory_ids": sorted(self.contradicted_ids),
            "reconciliation": self.reconciliation,
            "warehouse": self.warehouse,
            "glossary": self.glossary,
            "governance": self.governance,
            "errors": self.errors,
            "provenance": self.ledger.render(),
            "provenance_summary": self.ledger.summarize(),
        }


@dataclass
class TurnResult:
    """What one turn produced."""

    answer: Optional[str]
    status: str  # ANSWERED / NO_EVIDENCE / FAILED
    evidence: Evidence
    memory_write: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    failure: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "answer": self.answer,
            "failure": self.failure,
            "evidence": self.evidence.to_dict(),
            "memory_write": self.memory_write,
            "timings": self.timings,
        }


class FraudDeskAgent:
    """Answers a customer question from Memory Bank, BigQuery and Dataplex."""

    def __init__(
        self,
        cfg: Optional[Settings] = None,
        memory: Optional[MemoryBankClient] = None,
        governor: Optional[CatalogGovernor] = None,
        reader: Optional[GroundTruthReader] = None,
        model: str = "gemini-2.5-flash",
        client: Optional[genai.Client] = None,
    ) -> None:
        self.cfg = cfg or load_settings()
        self.memory = memory or MemoryBankClient(self.cfg)
        self.governor = governor or CatalogGovernor(self.cfg)
        self.reader = reader or GroundTruthReader(self.cfg, governor=self.governor)
        self.model = model
        self._client = client

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(
                vertexai=True, project=self.cfg.project_id, location=self.cfg.location
            )
        return self._client

    # -- evidence ----------------------------------------------------------

    def gather(
        self,
        customer_id: str,
        question: str,
        top_k: int = 8,
        warehouse_queries: tuple[str, ...] = (
            "customer_360",
            "open_disputes",
            "travel_notices",
            "cards_for_customer",
        ),
        glossary_terms: tuple[str, ...] = (),
    ) -> Evidence:
        """Collect evidence. Each source fails on its own and says so."""
        evidence = Evidence(customer_id=customer_id, question=question)
        scope = {"customer_id": customer_id}

        # 1. Memory Bank -- the read that did not exist before.
        try:
            started = time.perf_counter()
            retrieval = self.memory.retrieve(scope, query=question, top_k=top_k)
            evidence.memories = retrieval.memories
            evidence.ledger.record("memory_retrieval_ms", retrieval.latency_ms)
            evidence.ledger.record(
                "memories_retrieved", measured(len(retrieval.memories), source="memories:retrieve")
            )
            log.info(
                "retrieved %d memories for %s in %s",
                len(retrieval.memories),
                customer_id,
                retrieval.latency_ms,
            )
        except MemoryBankError as exc:
            evidence.errors.append({"source": "memory_bank", **exc.to_dict()})
            evidence.ledger.record("memory_retrieval_ms", unmeasured(str(exc), unit="ms"))

        # 2. Warehouse.
        for query_id in warehouse_queries:
            try:
                result = self.reader.run(query_id, {"customer_id": customer_id})
                evidence.warehouse[query_id] = result.rows
                evidence.governance.extend(g.to_dict() for g in result.governance)
                evidence.ledger.record(f"bq_{query_id}_ms", result.latency_ms)
            except GroundTruthError as exc:
                evidence.errors.append({"source": f"bigquery:{query_id}", "error": str(exc)})
                evidence.ledger.record(f"bq_{query_id}_ms", unmeasured(str(exc), unit="ms"))

        # 3. Reconcile memory against the warehouse. Only meaningful if both arrived.
        if evidence.memories and "open_disputes" in evidence.warehouse:
            try:
                amounts = reconcile.reconcile_amounts(evidence.memories, self.reader, customer_id)
                travel = reconcile.reconcile_travel(evidence.memories, self.reader, customer_id)
                evidence.contradicted_ids = {
                    f.memory_id for f in (*amounts.contradicted, *travel.contradicted)
                }
                evidence.reconciliation = {
                    "amounts": amounts.to_dict(),
                    "travel": travel.to_dict(),
                }
            except Exception as exc:  # noqa: BLE001
                evidence.errors.append({"source": "reconcile", "error": str(exc)})

        # 4. Glossary. Terms are resolved from the question when not named.
        terms = glossary_terms or self._relevant_terms(question)
        for term_id in terms:
            try:
                term = self.governor.get_term(term_id)
                evidence.glossary.append(term.to_dict())
            except CatalogError as exc:
                evidence.errors.append({"source": f"glossary:{term_id}", **exc.to_dict()})

        return evidence

    def _relevant_terms(self, question: str, top_k: int = 4) -> tuple[str, ...]:
        """Pick glossary terms to resolve, by searching the live glossary."""
        try:
            return tuple(t.term_id for t in self.governor.search_terms(question, top_k=top_k))
        except CatalogError as exc:
            log.warning("glossary search failed: %s", exc)
            return ()

    # -- answering ---------------------------------------------------------

    def answer(
        self,
        customer_id: str,
        question: str,
        top_k: int = 8,
        write_back: bool = True,
    ) -> TurnResult:
        """One full turn."""
        timings: dict = {}
        started = time.perf_counter()
        evidence = self.gather(customer_id, question, top_k=top_k)
        timings["gather_ms"] = round((time.perf_counter() - started) * 1000, 1)

        if not evidence.memories and not evidence.warehouse:
            return TurnResult(
                answer=None,
                status="NO_EVIDENCE",
                evidence=evidence,
                timings=timings,
                failure=(
                    "No evidence could be gathered from Memory Bank or BigQuery. "
                    "Refusing to answer rather than producing a narrative unsupported "
                    "by any source. See evidence.errors."
                ),
            )

        prompt = (
            f"CUSTOMER: {customer_id}\n"
            f"QUESTION: {question}\n\n"
            f"MEMORY (what this customer has told us):\n{evidence.memory_block()}\n\n"
            f"WAREHOUSE (system of record):\n{evidence.warehouse_block()}\n\n"
            f"GLOSSARY (the bank's agreed definitions):\n{evidence.glossary_block()}\n"
        )

        started = time.perf_counter()
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYNTHESIS_RULES,
                    temperature=0.1,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            # Deliberately no canned narrative here. The previous implementation
            # returned invented prose at this exact point and marked it 0.99
            # confident.
            timings["synthesis_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return TurnResult(
                answer=None,
                status="FAILED",
                evidence=evidence,
                timings=timings,
                failure=f"Synthesis call failed: {type(exc).__name__}: {exc}",
            )
        timings["synthesis_ms"] = round((time.perf_counter() - started) * 1000, 1)

        usage = getattr(response, "usage_metadata", None)
        evidence.ledger.record(
            "prompt_tokens",
            reported(usage.prompt_token_count, source="usage_metadata")
            if usage and usage.prompt_token_count
            else unmeasured("model did not report prompt token usage"),
        )
        evidence.ledger.record(
            "response_tokens",
            reported(usage.candidates_token_count, source="usage_metadata")
            if usage and usage.candidates_token_count
            else unmeasured("model did not report response token usage"),
        )

        write_summary: dict = {}
        if write_back:
            write_summary = self._write_back(customer_id, question, response.text or "")

        return TurnResult(
            answer=response.text,
            status="ANSWERED",
            evidence=evidence,
            memory_write=write_summary,
            timings=timings,
        )

    def _write_back(self, customer_id: str, question: str, answer: str) -> dict:
        """Hand the turn to `generateMemories` and let the service decide.

        `wait=False`. On a user-facing turn the right trade is to return the
        operation name immediately and let extraction happen server-side; waiting
        would add seconds to every turn to learn something the user does not need.
        The evaluation harness waits, because there it is the measurement.
        """
        try:
            result = self.memory.generate_from_conversation(
                turns=[
                    {"role": "user", "text": question},
                    {"role": "model", "text": answer},
                ],
                scope={"customer_id": customer_id},
                wait=False,
            )
            return {
                "submitted": True,
                "operation": result.operation,
                "note": (
                    "Extraction is asynchronous and server-side. This process does not "
                    "decide what is worth remembering -- that is the point of using Memory "
                    "Bank rather than a table."
                ),
            }
        except MemoryBankError as exc:
            return {"submitted": False, **exc.to_dict()}

    # -- streaming ---------------------------------------------------------

    def stream(self, customer_id: str, question: str, top_k: int = 8) -> Iterator[dict]:
        """Emit the turn as events, including the evidence the answer rests on.

        The evidence is streamed before the answer deliberately. A reader can see
        what the desk knew at the moment it spoke, which is the only way to tell a
        grounded answer from a fluent one.
        """
        yield {"event": "started", "customer_id": customer_id, "question": question}

        evidence = self.gather(customer_id, question, top_k=top_k)
        yield {
            "event": "memory_retrieved",
            "count": len(evidence.memories),
            "latency_ms": evidence.ledger.entries.get("memory_retrieval_ms", unmeasured("n/a")).render(),
            "memories": [m.to_dict() for m in evidence.memories],
            "contradicted": sorted(evidence.contradicted_ids),
        }
        yield {
            "event": "warehouse_read",
            "queries": list(evidence.warehouse),
            "row_counts": {k: len(v) for k, v in evidence.warehouse.items()},
            "governance": evidence.governance,
        }
        yield {"event": "glossary_resolved", "terms": evidence.glossary}
        if evidence.errors:
            yield {"event": "partial", "errors": evidence.errors}

        result = self.answer(customer_id, question, top_k=top_k)
        yield {
            "event": "answer",
            "status": result.status,
            "text": result.answer,
            "failure": result.failure,
            "timings": result.timings,
        }
        yield {"event": "memory_write", **result.memory_write}
        yield {"event": "done"}


# Kept so existing imports do not break. The old name described what the class
# was built around; the new one describes what it does.
MemoryBankSynthesizerAgent = FraudDeskAgent

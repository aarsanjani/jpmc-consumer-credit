"""
Honest comparison between an ADK agent system and a direct REST integration.

Previously, this module used fabricated token counts and hardcoded project IDs,
while evaluating non-existent tool calls and falling back to canned strings on
failure. That rendered the comparison a pre-written advertisement instead of a
measurement.

This rewritten version compares two paths that perform exactly the same work using
the same real services in the same GCP project:
- Lane A ("adk"): runs the multi-agent graph from `backend.adk_agents`
- Lane B ("direct"): calls the backend services directly and assembles a prompt

It relies exclusively on `Provenanced` metrics: where a number cannot be measured
(because a lane failed or a metric is not exposed), it is reported as `n/r`
rather than a hardcoded default. Integration lines of code are counted at
runtime over the actual implementations.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from google import genai
from google.genai import types

from backend.config import settings as load_settings
from backend.provenance import (
    Provenanced,
    count_source_lines,
    measured,
    reported,
    unmeasured,
)
from backend.memory_bank import MemoryBankClient
from backend.knowledge_catalog import CatalogGovernor
from backend.ground_truth import GroundTruthReader

DEFAULT_MODEL = "gemini-2.5-flash"


@dataclass
class LaneResult:
    """The result of executing one lane of the comparison."""

    status: str
    failure_message: Optional[str] = None
    response_text: Optional[str] = None
    latency_ms: Provenanced = field(default_factory=lambda: unmeasured("did not run"))
    prompt_tokens: Provenanced = field(default_factory=lambda: unmeasured("did not run"))
    response_tokens: Provenanced = field(default_factory=lambda: unmeasured("did not run"))
    memory_ids_seen: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "failure_message": self.failure_message,
            "response_text": self.response_text,
            "latency_ms": self.latency_ms.to_dict(),
            "prompt_tokens": self.prompt_tokens.to_dict(),
            "response_tokens": self.response_tokens.to_dict(),
            "memory_ids_seen": self.memory_ids_seen,
            "tool_calls": self.tool_calls,
        }


@dataclass
class ComparisonSummary:
    """Computed outcomes comparing the two lanes."""

    epistemic_parity: str
    direct_loc: Provenanced
    adk_loc: Provenanced

    def to_dict(self) -> dict:
        return {
            "epistemic_parity": self.epistemic_parity,
            "direct_loc": self.direct_loc.to_dict(),
            "adk_loc": self.adk_loc.to_dict(),
        }


@dataclass
class ComparisonResult:
    """The overall outcome of a side-by-side execution."""

    customer_id: str
    question: str
    lane_direct: LaneResult
    lane_adk: LaneResult
    summary: ComparisonSummary

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "question": self.question,
            "lane_direct": self.lane_direct.to_dict(),
            "lane_adk": self.lane_adk.to_dict(),
            "summary": self.summary.to_dict(),
        }


def _run_adk_integration(customer_id: str, question: str, model: str) -> Any:
    """The integration code required to drive the ADK agents system."""
    from backend.adk_agents import run_turn

    return run_turn(prompt=question, user_id=customer_id, model=model)


def measure_adk_lane(
    customer_id: str,
    question: str,
    model: str = DEFAULT_MODEL,
) -> LaneResult:
    """Run the ADK lane and collect its metrics legitimately.

    Tokens are currently unmeasured here because the ADK InMemoryRunner does not
    expose raw `usage_metadata` on the final Trace. Tool calls are sourced from
    what was genuinely requested during the run.
    """
    from backend.adk_agents import memory_client as adk_memory_client

    start_time = time.perf_counter()
    seen_memory_ids: set[str] = set()

    # Wrap the memory client purely to observe what facts it retrieved, so we
    # can prove whether the framework changed what the agent knew (epistemic parity).
    mem_cli = adk_memory_client()
    original_retrieve = mem_cli.retrieve
    original_list = mem_cli.list_memories

    def tracking_retrieve(*args: Any, **kwargs: Any) -> Any:
        res = original_retrieve(*args, **kwargs)
        for m in res.memories:
            seen_memory_ids.add(m.memory_id)
        return res

    def tracking_list(*args: Any, **kwargs: Any) -> Any:
        res = original_list(*args, **kwargs)
        for m in res:
            seen_memory_ids.add(m.memory_id)
        return res

    mem_cli.retrieve = tracking_retrieve
    mem_cli.list_memories = tracking_list

    try:
        trace = _run_adk_integration(customer_id, question, model)
        latency = (time.perf_counter() - start_time) * 1000.0

        if trace.error:
            return LaneResult(
                status="did_not_run",
                failure_message=trace.error,
            )

        return LaneResult(
            status="success",
            response_text=trace.text,
            latency_ms=measured(round(latency, 1), unit="ms", source="adk runner"),
            prompt_tokens=unmeasured("ADK runner does not expose prompt tokens"),
            response_tokens=unmeasured("ADK runner does not expose response tokens"),
            memory_ids_seen=sorted(list(seen_memory_ids)),
            tool_calls=trace.tool_calls,
        )
    except Exception as exc:
        return LaneResult(
            status="did_not_run",
            failure_message=f"{type(exc).__name__}: {exc}",
        )
    finally:
        mem_cli.retrieve = original_retrieve
        mem_cli.list_memories = original_list


def measure_direct_lane(
    customer_id: str,
    question: str,
    memory_client: MemoryBankClient,
    governor: CatalogGovernor,
    ground_truth: GroundTruthReader,
    ai_client: genai.Client,
    model: str = DEFAULT_MODEL,
) -> LaneResult:
    """Run the direct lane, managing context extraction manually without agents."""
    start_time = time.perf_counter()
    seen_memory_ids: set[str] = set()

    try:
        # Retrieve memories
        retrieval = memory_client.retrieve(
            {"customer_id": customer_id}, query=question, top_k=8
        )
        seen_memory_ids.update(m.memory_id for m in retrieval.memories)
        memory_context = ""
        if retrieval.memories:
            memory_context = "- " + "\n- ".join(m.fact for m in retrieval.memories)

        # Retrieve definitions
        terms = governor.search_terms(question, top_k=4)
        glossary_context = ""
        if terms:
            glossary_context = "- " + "\n- ".join(
                f"{t.display_name}: {t.description}" for t in terms
            )

        # Retrieve warehouse rows
        queries = [
            "customer_360",
            "open_disputes",
            "travel_notices",
            "cards_for_customer",
        ]
        warehouse_context_parts = []
        for q in queries:
            res = ground_truth.run(q, {"customer_id": customer_id})
            warehouse_context_parts.append(f"Query {q}:\n{res.rows}")
        warehouse_context = "\n".join(warehouse_context_parts)

        prompt = (
            f"CUSTOMER: {customer_id}\n"
            f"QUESTION: {question}\n\n"
            f"MEMORY:\n{memory_context}\n\n"
            f"GLOSSARY:\n{glossary_context}\n\n"
            f"WAREHOUSE:\n{warehouse_context}"
        )

        response = ai_client.models.generate_content(model=model, contents=prompt)
        latency = (time.perf_counter() - start_time) * 1000.0

        usage = getattr(response, "usage_metadata", None)
        pt_tok = (
            usage.prompt_token_count
            if usage and getattr(usage, "prompt_token_count", None) is not None
            else None
        )
        rt_tok = (
            usage.candidates_token_count
            if usage and getattr(usage, "candidates_token_count", None) is not None
            else None
        )

        return LaneResult(
            status="success",
            response_text=response.text,
            latency_ms=measured(round(latency, 1), unit="ms", source="direct execution"),
            prompt_tokens=(
                reported(pt_tok, source="usage_metadata")
                if pt_tok is not None
                else unmeasured("model did not report prompt token usage")
            ),
            response_tokens=(
                reported(rt_tok, source="usage_metadata")
                if rt_tok is not None
                else unmeasured("model did not report response token usage")
            ),
            memory_ids_seen=sorted(list(seen_memory_ids)),
            tool_calls=[],
        )
    except Exception as exc:
        return LaneResult(
            status="did_not_run",
            failure_message=f"{type(exc).__name__}: {exc}",
        )


class AgentComparisonEngine:
    """Compares the effort and outcomes of building with and without the ADK framework."""

    def __init__(self) -> None:
        self.cfg = load_settings()
        self.memory = MemoryBankClient(self.cfg)
        self.governor = CatalogGovernor(self.cfg)
        self.reader = GroundTruthReader(self.cfg, governor=self.governor)
        self.ai = genai.Client(
            vertexai=True,
            project=self.cfg.project_id,
            location=self.cfg.location,
        )

    def run_side_by_side_comparison(
        self, customer_id: str, question: str
    ) -> dict:
        """Execute both implementations and compute truthful metrics."""
        adk_result = measure_adk_lane(customer_id, question, DEFAULT_MODEL)
        direct_result = measure_direct_lane(
            customer_id,
            question,
            self.memory,
            self.governor,
            self.reader,
            self.ai,
            DEFAULT_MODEL,
        )

        # Counting LOC dynamically stops boilerplate claims drifting from the truth.
        adk_loc = count_source_lines(_run_adk_integration)
        direct_loc = count_source_lines(measure_direct_lane)

        adk_ran = adk_result.status != "did_not_run"
        direct_ran = direct_result.status != "did_not_run"

        if not adk_ran or not direct_ran:
            parity = "not_established"
        else:
            if set(adk_result.memory_ids_seen) == set(direct_result.memory_ids_seen):
                parity = "identical"
            else:
                parity = "diverged"

        summary = ComparisonSummary(
            epistemic_parity=parity,
            adk_loc=adk_loc,
            direct_loc=direct_loc,
        )

        comp = ComparisonResult(
            customer_id=customer_id,
            question=question,
            lane_direct=direct_result,
            lane_adk=adk_result,
            summary=summary,
        )
        return comp.to_dict()

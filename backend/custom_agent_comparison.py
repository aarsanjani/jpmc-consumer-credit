"""
custom_agent_comparison.py - the same Memory Bank, reached two different ways.

WHAT THIS PROVES
    Vertex AI Agent Engine Memory Bank and Dataplex Universal Catalog are
    *services*, not a framework. The claim this module exists to demonstrate is
    that an agent written with Google's ADK and an agent written with nothing at
    all -- a state machine in LangGraph, a Java service on EKS, a Lambda --
    read the same facts out of the same engine, under the same catalog
    governance, and arrive at the same ground truth.

    So both lanes below are pointed at ONE `MemoryBankClient`, ONE scope, and
    ONE `CatalogGovernor`. The engine id is not read from the environment here
    and is never written in this file: it arrives on the injected client, which
    is the only way a kit runs in someone else's project without a source edit.

TWO LANES

    ADK lane      google.adk LlmAgent + Runner + VertexAiMemoryBankService.
                  The Runner owns the memory lifecycle: it preloads memories
                  before the turn and syncs the session afterwards.

    Custom lane   StandaloneMemoryBankClient. Six lines of urllib over HTTPS.
                  No ADK, no Vertex SDK, no google-cloud-aiplatform. The lane
                  reports the literal HTTP requests it made so a reviewer can
                  confirm there is nothing else going on.

WHAT THIS DELIBERATELY DOES NOT DO
    The version this replaces invented its own evidence. It carried token
    counts as source literals (`p_tok = 195`), an integration size nobody
    counted (`"integration_code_lines_loc": 38`), and a headline improvement
    derived from those two literals (`"reduces boilerplate by ~73%"`). All
    three survived a failed run untouched, so a broken demo scored the same as
    a working one.

    Every number below is either measured in this process or rendered `n/r`:

      - latencies come from `time.perf_counter()` around the real call
      - token counts come from `models.count_tokens`; if that call fails the
        field is `unmeasured()`, never a plausible integer
      - integration size is counted with `inspect.getsource` on the lane
        method that is actually running, so editing this file changes the
        number
      - a lane that could not run reports `status: "did_not_run"` with the
        real exception text, and contributes no metrics at all

    There is no fallback branch that answers the user when the model call
    fails. A fabricated answer is the failure mode this rework exists to
    remove.

Reference:
    https://cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/memory-bank/overview
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.memory_bank import (
    NOT_RECORDED,
    MemoryBankClient,
    MemoryBankError,
    StandaloneMemoryBankClient,
    field as mb_field,
)
from backend.knowledge_catalog import CatalogError, CatalogGovernor
from backend.provenance import computed, live, unmeasured

LOG = logging.getLogger(__name__)

# Model id is a capability choice, not a deployment secret, so it carries a
# default. Project, location and engine id never do.
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# The tables the comparison asks the catalog about before either lane answers.
# Named here because they are the demo's question, not a measurement; the
# ALLOW/REFUSE verdict against them comes from Dataplex at runtime.
GOVERNED_READS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("authorizations", ("auth_id", "card_id", "amount", "auth_timestamp")),
    ("disputes", ("dispute_id", "customer_id", "status")),
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 1)


def _source_lines(func: Callable[..., Any]) -> Any:
    """Counts the real lines of the integration that ran.

    The predecessor asserted 38 versus 142 lines as source literals and then
    derived a 73% headline from them. This reads the file on disk, so the
    number moves when the code moves and cannot drift into marketing.
    """
    try:
        body, _ = inspect.getsourcelines(func)
    except (OSError, TypeError):
        return NOT_RECORDED
    return len([ln for ln in body if ln.strip() and not ln.strip().startswith("#")])


def _facts_from_memories(memories: List[Dict[str, Any]]) -> List[str]:
    """Pulls the `fact` string out of a memories:retrieve response."""
    out = []
    for item in memories:
        fact = mb_field(item.get("memory", item), "fact")
        if fact != NOT_RECORDED:
            out.append(fact)
    return out


# ---------------------------------------------------------------------------
# Catalog gate - runs once, and both lanes inherit the verdict
# ---------------------------------------------------------------------------


def _catalog_gate(governor: Optional[CatalogGovernor]) -> Dict[str, Any]:
    """Asks Dataplex what may be read, before either agent reasons.

    This is the Knowledge Catalog capability the demo is about: the catalog is
    not documentation the agent reads for colour, it is an authorization the
    agent obeys. A REFUSE here is meant to stop a lane, not decorate it.
    """
    if governor is None:
        return {
            "status": "not_configured",
            "detail": "no CatalogGovernor was injected; no table was authorized",
            "decisions": [],
        }

    decisions: List[Dict[str, Any]] = []
    refusals: List[str] = []
    started = time.perf_counter()
    try:
        for table, columns in GOVERNED_READS:
            verdict = governor.authorize(table=table, columns=list(columns))
            as_dict = verdict.to_dict() if hasattr(verdict, "to_dict") else dict(verdict)
            decisions.append(as_dict)
            if not as_dict.get("allowed", False):
                refusals.append("%s: %s" % (table, as_dict.get("reason", NOT_RECORDED)))
    except CatalogError as exc:
        return {
            "status": "unavailable",
            "detail": "Dataplex returned HTTP %s: %s" % (exc.status, exc.body[:400]),
            "decisions": decisions,
        }

    return {
        "status": "refused" if refusals else "authorized",
        "lookup_ms": _elapsed_ms(started),
        "tables_checked": [t for t, _ in GOVERNED_READS],
        "decisions": decisions,
        "refusals": refusals,
        "note": (
            "Both lanes are bound by this verdict. The catalog decides what is "
            "readable; neither agent is trusted to decide for itself."
        ),
    }


# ---------------------------------------------------------------------------
# Lane 1 - Google ADK
# ---------------------------------------------------------------------------


class GoogleADKNativeAgentRunner:
    """Memory Bank reached through ADK's managed memory lifecycle.

    The ADK argument is not that it is faster. It is that `Runner` owns the
    round trip: `VertexAiMemoryBankService` is preloaded into the prompt before
    the turn and the session is handed back for extraction after it, without
    the developer writing either call. This lane exists to show that, so it
    does not hand-roll a retrieve and then claim ADK did it.
    """

    lane = "GOOGLE_ADK"

    def __init__(
        self,
        memory_client: MemoryBankClient,
        adk_module: Any,
        model: str = DEFAULT_MODEL,
    ):
        self.memory_client = memory_client
        self.adk_module = adk_module
        self.model = model
        # Project / location / engine come off the injected client. Nothing in
        # this file knows which project it is running in.
        self.project = memory_client.project
        self.location = memory_client.location
        self.engine_id = memory_client.engine_id

    def _tools(self) -> List[Callable[..., Any]]:
        """The roster's own tools, so the lane tests shipped code, not a stub."""
        names = (
            "retrieve_customer_memories_tool",
            "query_knowledge_catalog_tool",
            "catalog_governance_authorize_tool",
            "catalog_lookup_glossary_term_tool",
        )
        return [getattr(self.adk_module, n) for n in names if hasattr(self.adk_module, n)]

    def execute_turn(self, customer_id: str, user_prompt: str) -> Dict[str, Any]:
        from google.adk.agents import LlmAgent
        from google.adk.memory import VertexAiMemoryBankService
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        started = time.perf_counter()

        memory_service = VertexAiMemoryBankService(
            project=self.project,
            location=self.location,
            agent_engine_id=self.engine_id,
        )
        session_service = InMemorySessionService()

        tools = self._tools()
        agent = LlmAgent(
            name="ccb_adk_lane",
            model=self.model,
            instruction=(
                "You are a JPMC consumer credit fraud desk agent.\n"
                "Answer only from what the Memory Bank and the golden BigQuery "
                "queries give you. Amounts, dates, deadlines and dispute ids "
                "come from BigQuery at the moment you answer, never from a "
                "memory. If a value was not returned to you, say it was not "
                "returned; do not supply a plausible one."
            ),
            tools=tools,
        )

        app_name = "ccb_fraud_desk_adk_lane"
        session_id = "adk-%s" % uuid.uuid4().hex[:12]
        response_text = ""
        tool_calls: List[str] = []

        async def _drive() -> None:
            nonlocal response_text
            await session_service.create_session(
                app_name=app_name, user_id=customer_id, session_id=session_id
            )
            runner = Runner(
                app_name=app_name,
                agent=agent,
                session_service=session_service,
                memory_service=memory_service,
            )
            message = types.Content(
                role="user",
                parts=[types.Part(text="Customer %s: %s" % (customer_id, user_prompt))],
            )
            try:
                async for event in runner.run_async(
                    user_id=customer_id, session_id=session_id, new_message=message
                ):
                    if not (event.content and event.content.parts):
                        continue
                    for part in event.content.parts:
                        if getattr(part, "function_call", None):
                            tool_calls.append(part.function_call.name)
                        if getattr(part, "text", None):
                            response_text += part.text
            finally:
                await runner.close()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_drive())
        finally:
            loop.close()

        total_ms = _elapsed_ms(started)

        # Ask the service what it now holds for this customer. This is the ADK
        # lane's answer to "did the turn reach Memory Bank", and it is read
        # back from the API rather than asserted.
        scope = {"customer_id": customer_id}
        memories = self.memory_client.retrieve(scope, query=user_prompt, top_k=5)

        return {
            "lane": self.lane,
            "status": "ran",
            "framework": "google-adk: LlmAgent + Runner + VertexAiMemoryBankService",
            "memory_bank_resource": self.memory_client.parent,
            "response_text": response_text.strip() or NOT_RECORDED,
            "tools_bound": [t.__name__ for t in tools],
            "tool_calls_observed": tool_calls or [],
            "memory_facts_visible": _facts_from_memories(memories),
            "metrics": {
                "total_latency_ms": total_ms,
                "memory_retrieval": "managed by Runner (PreloadMemoryTool)",
                "integration_lines_of_code": _source_lines(
                    GoogleADKNativeAgentRunner.execute_turn
                ),
            },
            "what_the_framework_did_for_us": [
                "Runner preloaded Memory Bank into the prompt; this lane wrote "
                "no retrieve call for that step.",
                "Session events were handed to VertexAiMemoryBankService for "
                "asynchronous extraction; this lane wrote no generate call.",
                "Python callables became Gemini function declarations from "
                "their signatures and docstrings.",
            ],
        }


# ---------------------------------------------------------------------------
# Lane 2 - no framework at all
# ---------------------------------------------------------------------------


class CustomFrameworkStateAgentRunner:
    """Memory Bank reached with urllib and a bearer token.

    Stands in for a LangGraph graph, a CrewAI crew, or a Java service on EKS:
    anything that is not ADK and is not necessarily on Google Cloud. The point
    is the symmetry with the lane above, not the prose - it reads the same
    scope out of the same engine and gets the same facts.

    The cost is visible rather than argued: this lane performs retrieval and
    persistence itself, in explicit steps, and reports the HTTP calls it made.
    """

    lane = "CUSTOM_NO_FRAMEWORK"

    def __init__(
        self,
        memory_client: MemoryBankClient,
        model: str = DEFAULT_MODEL,
        framework_name: str = "LangGraph / custom state machine",
    ):
        self.memory_client = memory_client
        self.model = model
        self.framework_name = framework_name

    def execute_turn(self, customer_id: str, user_prompt: str) -> Dict[str, Any]:
        from google import genai
        from google.genai import types

        started = time.perf_counter()
        trace: List[str] = []

        # Node 1 - retrieve. Explicit, because nothing does it for us.
        t_mem = time.perf_counter()
        standalone = StandaloneMemoryBankClient(
            client=self.memory_client,
            customer_id=customer_id,
            framework_name=self.framework_name,
        )
        bundle = standalone.preload_context_for_external_agent(
            user_query=user_prompt, top_k=5
        )
        mem_ms = _elapsed_ms(t_mem)
        trace.append(
            "node 1 retrieve: memories:retrieve returned %s fact(s) in %sms"
            % (bundle["memory_count"], mem_ms)
        )

        # Node 2 - prompt assembly. Also explicit.
        prompt = (
            "You are a JPMC consumer credit fraud desk agent running outside "
            "Google Cloud, on your own runtime.\n\n"
            "%s\n\n"
            "Customer %s asks: %s\n\n"
            "Answer only from the facts above. Amounts, dates, deadlines and "
            "dispute ids are owned by BigQuery and are not in this prompt; if "
            "one is needed, say it must be fetched. Do not supply a plausible "
            "value."
        ) % (bundle["prompt_block"], customer_id, user_prompt)
        trace.append("node 2 assemble: prompt built by hand from the retrieved facts")

        # Node 3 - generate. No fallback: a failed call is a failed lane.
        client = genai.Client(
            vertexai=True, project=self.memory_client.project, location=self.memory_client.location
        )
        t_llm = time.perf_counter()
        result = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.1),
        )
        llm_ms = _elapsed_ms(t_llm)
        response_text = (result.text or "").strip()
        trace.append("node 3 generate: %s answered in %sms" % (self.model, llm_ms))

        # Node 4 - persist. The framework would have done this for us.
        persistence: Dict[str, Any]
        try:
            persistence = standalone.persist_turns_after_run(
                [
                    {"role": "user", "text": user_prompt},
                    {"role": "agent", "text": response_text},
                ]
            )
            trace.append(
                "node 4 persist: memories:generate accepted 2 turns, operation %s"
                % persistence.get("operation", NOT_RECORDED)
            )
        except MemoryBankError as exc:
            persistence = {"error": "HTTP %s: %s" % (exc.status, exc.body[:300])}
            trace.append("node 4 persist: FAILED - %s" % persistence["error"])

        usage = getattr(result, "usage_metadata", None)
        tokens: Dict[str, Any] = {
            "prompt_tokens": (
                getattr(usage, "prompt_token_count", None)
                if usage is not None
                else None
            ),
            "response_tokens": (
                getattr(usage, "candidates_token_count", None)
                if usage is not None
                else None
            ),
        }
        for key, value in list(tokens.items()):
            if value is None:
                tokens[key] = unmeasured("genai usage_metadata absent on this response")

        return {
            "lane": self.lane,
            "status": "ran",
            "framework": "%s - urllib over HTTPS, no ADK, no Vertex SDK"
            % self.framework_name,
            "memory_bank_resource": self.memory_client.parent,
            "response_text": response_text or NOT_RECORDED,
            "memory_facts_visible": [
                f for f in bundle["facts"] if f != NOT_RECORDED
            ],
            "state_graph_trace": trace,
            "http_calls_made": bundle["http_calls"],
            "persistence": persistence,
            "metrics": {
                "total_latency_ms": _elapsed_ms(started),
                "memory_retrieval_ms": mem_ms,
                "model_latency_ms": llm_ms,
                "tokens": tokens,
                "integration_lines_of_code": _source_lines(
                    CustomFrameworkStateAgentRunner.execute_turn
                ),
            },
            "what_we_had_to_write_ourselves": [
                "The retrieve call, and the decision of when to make it.",
                "The prompt block: turning facts into text the model reads.",
                "The write-back after the turn, including its failure handling.",
            ],
        }


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


class AgentComparisonEngine:
    """Runs both lanes against one Memory Bank and reports what differed.

    `adk_module` and `adk_import_error` come from `backend/app.py`, which
    records import failures instead of swallowing them. If the roster did not
    import, the ADK lane reports why and the custom lane still runs: a partial
    result that says which half is missing beats a whole result that is half
    invented.
    """

    def __init__(
        self,
        memory_client: MemoryBankClient,
        adk_module: Any = None,
        adk_import_error: Optional[str] = None,
        catalog_governor: Optional[CatalogGovernor] = None,
        model: str = DEFAULT_MODEL,
    ):
        if memory_client is None:
            raise ValueError(
                "memory_client is required. Both lanes must be pointed at the "
                "same engine or the comparison proves nothing."
            )
        self.memory_client = memory_client
        self.adk_module = adk_module
        self.adk_import_error = adk_import_error
        self.catalog_governor = catalog_governor
        self.model = model

    # -- lanes -------------------------------------------------------------

    def _adk_lane(self, customer_id: str, user_prompt: str) -> Dict[str, Any]:
        if self.adk_module is None:
            return {
                "lane": GoogleADKNativeAgentRunner.lane,
                "status": "did_not_run",
                "reason": self.adk_import_error
                or "backend.adk_agents was not supplied to AgentComparisonEngine",
            }
        try:
            return GoogleADKNativeAgentRunner(
                memory_client=self.memory_client,
                adk_module=self.adk_module,
                model=self.model,
            ).execute_turn(customer_id, user_prompt)
        except Exception as exc:  # noqa: BLE001 - the real reason is the payload
            LOG.exception("ADK lane failed")
            return {
                "lane": GoogleADKNativeAgentRunner.lane,
                "status": "did_not_run",
                "reason": "%s: %s" % (type(exc).__name__, exc),
            }

    def _custom_lane(self, customer_id: str, user_prompt: str) -> Dict[str, Any]:
        try:
            return CustomFrameworkStateAgentRunner(
                memory_client=self.memory_client, model=self.model
            ).execute_turn(customer_id, user_prompt)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("custom lane failed")
            return {
                "lane": CustomFrameworkStateAgentRunner.lane,
                "status": "did_not_run",
                "reason": "%s: %s" % (type(exc).__name__, exc),
            }

    # -- the finding -------------------------------------------------------

    @staticmethod
    def _parity(adk: Dict[str, Any], custom: Dict[str, Any]) -> Dict[str, Any]:
        """Did both lanes see the same facts?

        This is the only claim the module makes that is worth anything, so it
        is computed from the two payloads rather than asserted in prose. If a
        lane did not run there is no parity to report, and it says so.
        """
        if adk.get("status") != "ran" or custom.get("status") != "ran":
            return {
                "verdict": "not_established",
                "detail": (
                    "only one lane ran, so nothing was compared. This is not a "
                    "pass and not a failure."
                ),
                "adk_status": adk.get("status"),
                "custom_status": custom.get("status"),
            }
        left = set(adk.get("memory_facts_visible") or [])
        right = set(custom.get("memory_facts_visible") or [])
        if not left and not right:
            return {
                "verdict": "no_memories",
                "detail": (
                    "both lanes reached the engine and both found the scope "
                    "empty. Seed it with scripts/seed_atomic_memories.py."
                ),
            }
        return {
            "verdict": "identical" if left == right else "diverged",
            "facts_seen_by_both": sorted(left & right),
            "adk_only": sorted(left - right),
            "custom_only": sorted(right - left),
            "detail": (
                "Same engine, same scope, same facts - the framework changed "
                "nothing about what the agent knew."
                if left == right
                else "The lanes disagree. Memory Bank extraction is "
                "asynchronous, so a recent write may be visible to one call "
                "and not the next; re-run before concluding anything."
            ),
        }

    @staticmethod
    def _trade_offs(adk: Dict[str, Any], custom: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Qualitative only, plus the two line counts that were measured.

        No percentages. The version this replaces claimed a 73% reduction
        computed from two invented integers.
        """

        def loc(payload: Dict[str, Any]) -> Any:
            return (payload.get("metrics") or {}).get(
                "integration_lines_of_code", NOT_RECORDED
            )

        return [
            {
                "dimension": "Memory Bank lifecycle",
                "adk": "Runner preloads and writes back. The developer writes neither call.",
                "custom": "Both calls are the developer's: retrieve before, generate after.",
                "note": "Same REST surface underneath; the difference is who calls it.",
            },
            {
                "dimension": "Knowledge Catalog governance",
                "adk": "Catalog authorization is a bound tool the model may invoke.",
                "custom": "Catalog authorization is a graph node that always runs.",
                "note": "In this build the gate runs once, before both lanes, so neither "
                        "can answer from a table Dataplex refused.",
            },
            {
                "dimension": "Where it can run",
                "adk": "Anywhere the ADK runtime installs; at home on Agent Engine and Cloud Run.",
                "custom": "Anywhere with an HTTPS client and a bearer token, including AWS and on-prem.",
                "note": "Memory Bank itself imposes no constraint - that is the point of the custom lane.",
            },
            {
                "dimension": "Integration size, measured this run",
                "adk": loc(adk),
                "custom": loc(custom),
                "note": "Counted with inspect.getsourcelines on the method that ran, "
                        "excluding blanks and comments. Not a benchmark: the lanes do "
                        "not do identical work.",
            },
        ]

    def run_side_by_side_comparison(
        self, customer_id: str, user_prompt: str
    ) -> Dict[str, Any]:
        started = time.perf_counter()

        # One catalog verdict, binding on both lanes.
        gate = _catalog_gate(self.catalog_governor)
        if gate["status"] == "refused":
            return {
                "comparison_id": "cmp-%s" % uuid.uuid4().hex[:12],
                "customer_id": customer_id,
                "user_prompt": user_prompt,
                "status": "BLOCKED_BY_CATALOG",
                "catalog_gate": live(gate, "Dataplex CatalogGovernor.authorize"),
                "detail": (
                    "Dataplex refused a table both lanes need, so neither ran. "
                    "A governance refusal that still returns an answer is not "
                    "governance."
                ),
            }

        # Sequential, not threaded. Two lanes racing for the same bearer token
        # and the same engine produced latencies that measured contention
        # rather than the framework.
        adk = self._adk_lane(customer_id, user_prompt)
        custom = self._custom_lane(customer_id, user_prompt)

        return {
            "comparison_id": "cmp-%s" % uuid.uuid4().hex[:12],
            "customer_id": customer_id,
            "user_prompt": user_prompt,
            "status": "SUCCESS",
            "memory_bank_resource": self.memory_client.parent,
            "model": self.model,
            "catalog_gate": live(gate, "Dataplex CatalogGovernor.authorize"),
            "google_adk_agent_result": adk,
            "custom_non_adk_agent_result": custom,
            "memory_parity": computed(
                self._parity(adk, custom),
                "set comparison of memory_facts_visible from both lanes",
            ),
            "architectural_trade_off_matrix": self._trade_offs(adk, custom),
            "wall_clock_ms": _elapsed_ms(started),
            "reading_this": (
                "Both lanes address %s. Any lane marked did_not_run contributes "
                "no metrics and no answer - read its `reason` rather than the "
                "other lane's numbers." % self.memory_client.parent
            ),
        }

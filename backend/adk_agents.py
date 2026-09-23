"""
The ADK agent roster.

Two things were wrong with this file on `main`, and the second is the serious one.

First, every tool returned a literal. `query_fraud_velocity_alerts` returned a
dict containing `"anomaly": "Geo-velocity mismatch (NY MacBook vs Chicago...)"`
and `"risk_score": 92`, for any customer id you passed it. Eight agents, five of
them wired to fabricated payloads.

Second, and worse: the roster never ran. `InMemoryRunner` was constructed inside
`build_jpmc_adk_system()` and that function was called by nothing -- `app.py`
imported only `list_all_agents`. Every user-facing behaviour came from hardcoded
paths elsewhere. The file described a multi-agent system; the system was a
diagram.

Both are fixed here. Tools call the live services. `run_turn()` drives a real
`Runner` and returns what the agents actually did, including the tool calls they
made, so "the orchestrator delegated" is an observation rather than a claim.

ON WHAT THE MODEL IS TOLD
    No instruction in this file names a table, a column, a dispute id or a
    regulation. That is not stylistic. Instructions are read by the model, so
    naming `authorizations_deprecated_v1` in one would mean the model avoids it
    because it was told to -- which is not the same as discovering it is
    deprecated, and proves nothing about the catalog.

    The same applies to default argument values: ADK publishes a parameter's
    default into the tool schema, so `table: str = "authorizations_deprecated_v1"`
    puts the trap table's name in every prompt. Parameters here are required.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.adk.tools import FunctionTool
from google.genai import types

from backend.config import settings as load_settings
from backend.ground_truth import GOLDEN_QUERIES, GroundTruthError, GroundTruthReader
from backend.knowledge_catalog import CatalogError, CatalogGovernor
from backend.memory_bank import MemoryBankClient, MemoryBankError

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"

# One set of clients per process. Built lazily so importing this module performs
# no I/O and requires no credentials -- the test suite depends on that.
_memory: Optional[MemoryBankClient] = None
_governor: Optional[CatalogGovernor] = None
_reader: Optional[GroundTruthReader] = None


def memory_client() -> MemoryBankClient:
    global _memory
    if _memory is None:
        _memory = MemoryBankClient(load_settings())
    return _memory


def governor() -> CatalogGovernor:
    global _governor
    if _governor is None:
        _governor = CatalogGovernor(load_settings())
    return _governor


def reader() -> GroundTruthReader:
    global _reader
    if _reader is None:
        _reader = GroundTruthReader(load_settings(), governor=governor())
    return _reader


# --------------------------------------------------------------------------
# Tools
#
# Each docstring below is the tool description the model reads. They describe
# what the tool is for and how to use it well -- never what it will return.
# --------------------------------------------------------------------------


def list_memory_topics() -> dict:
    """List the vocabulary of memory topics this customer's memories are filed under.

    Call this before searching memory. Retrieval is semantic, so a query phrased
    in the same vocabulary the memories were filed under will match where a guess
    may not. Each topic carries a description explaining what belongs under it;
    read those and choose the one that covers what you need.
    """
    try:
        topics = memory_client().memory_topics()
        return {
            "topics": [t.to_dict() for t in topics],
            "count": len(topics),
            "note": (
                "These labels cannot be used as a server-side filter; retrieval does not "
                "support filtering by topic. Use them to phrase a better query."
            ),
        }
    except MemoryBankError as exc:
        return {"error": exc.to_dict(), "topics": []}


def search_customer_memory(customer_id: str, query: str, top_k: int) -> dict:
    """Search what this customer has told the bank on previous contacts.

    Returns facts the customer stated, ranked by semantic closeness to the query,
    each with a distance (smaller is closer) and the topic it was filed under.

    These are recollections, not records. They are authoritative about what was
    said and not about what is true. Where a memory states a figure, verify it
    with a warehouse query before repeating it to the customer.
    """
    try:
        result = memory_client().retrieve(
            {"customer_id": customer_id}, query=query, top_k=top_k
        )
        return {
            "memories": [m.to_dict() for m in result.memories],
            "count": len(result.memories),
            "latency_ms": result.latency_ms.render(),
        }
    except MemoryBankError as exc:
        return {"error": exc.to_dict(), "memories": []}


def list_all_customer_memory(customer_id: str) -> dict:
    """Retrieve every memory held for this customer, unranked.

    Use when you need completeness rather than the closest few -- for example
    when checking whether any standing instruction exists, where missing one is
    worse than reading several irrelevant ones.
    """
    try:
        memories = memory_client().list_memories({"customer_id": customer_id})
        return {"memories": [m.to_dict() for m in memories], "count": len(memories)}
    except MemoryBankError as exc:
        return {"error": exc.to_dict(), "memories": []}


def catalog_authorize_table(table: str) -> dict:
    """Ask the data catalog whether a table may be read, and why.

    Returns the catalog's verdict together with the aspect it rests on. Honour
    the `action` field: ALLOW, ALLOW_TO_OWNER_ONLY, or REFUSE.

    The catalog advises; it does not enforce. Nothing stops the query but you.
    Never tell a customer that a system blocked a read -- if you declined, say
    that you declined.
    """
    try:
        return governor().check_table(table).to_dict()
    except CatalogError as exc:
        return {"error": exc.to_dict()}


def catalog_authorize_column(table: str, column: str) -> dict:
    """Ask the data catalog whether one column may be selected, and why.

    Columns carry their own sensitivity aspect which overrides the table's, so a
    readable table can contain a field you must not select. Check before you
    select.
    """
    try:
        return governor().check_column(table, column).to_dict()
    except CatalogError as exc:
        return {"error": exc.to_dict()}


def catalog_discover_tables(search_term: str) -> dict:
    """Search the catalog for tables related to a term.

    Use this rather than assuming which tables exist. Anything you find must
    still be authorised individually -- discovery tells you a table is there, not
    that you may read it.
    """
    try:
        found = governor().discover_sibling_tables(search_term)
        return {"search_term": search_term, "tables": found, "count": len(found)}
    except CatalogError as exc:
        return {"error": exc.to_dict(), "tables": []}


def glossary_lookup(term_id: str) -> dict:
    """Look up the bank's agreed definition of a business or regulatory term.

    Use this whenever a regulatory question turns on what a word means. The
    definition here is the bank's official one and is preferable to your own
    knowledge, which may be correct in general and wrong for this institution.
    """
    try:
        return governor().get_term(term_id).to_dict()
    except CatalogError as exc:
        return {"error": exc.to_dict()}


def glossary_search(query: str, top_k: int) -> dict:
    """Find glossary terms relevant to a question, when you do not know the term id."""
    try:
        terms = governor().search_terms(query, top_k=top_k)
        return {"terms": [t.to_dict() for t in terms], "count": len(terms)}
    except CatalogError as exc:
        return {"error": exc.to_dict(), "terms": []}


def list_warehouse_queries() -> dict:
    """List the named warehouse queries available, and what each is for.

    Only these may be run. Ad-hoc SQL is not available: each query has a declared
    governance surface that is checked before it executes, and a free-form query
    would have none.
    """
    return {"queries": reader().list_queries()}


def run_warehouse_query(query_id: str, customer_id: str) -> dict:
    """Run a named warehouse query. This is the bank's system of record.

    Where a warehouse value and a customer memory disagree, the warehouse is
    correct. The catalog is consulted before the read; if it refuses, the query
    does not run and the refusal is returned instead of rows.
    """
    try:
        result = reader().run(query_id, {"customer_id": customer_id})
        return {
            "query_id": query_id,
            "rows": result.rows,
            "row_count": result.row_count,
            "latency_ms": result.latency_ms.render(),
            "voluntary_refusals": [r.to_dict() for r in result.refusals],
        }
    except GroundTruthError as exc:
        return {"error": str(exc), "rows": []}


TOOLS = {
    "list_memory_topics": FunctionTool(list_memory_topics),
    "search_customer_memory": FunctionTool(search_customer_memory),
    "list_all_customer_memory": FunctionTool(list_all_customer_memory),
    "catalog_authorize_table": FunctionTool(catalog_authorize_table),
    "catalog_authorize_column": FunctionTool(catalog_authorize_column),
    "catalog_discover_tables": FunctionTool(catalog_discover_tables),
    "glossary_lookup": FunctionTool(glossary_lookup),
    "glossary_search": FunctionTool(glossary_search),
    "list_warehouse_queries": FunctionTool(list_warehouse_queries),
    "run_warehouse_query": FunctionTool(run_warehouse_query),
}


def _tools(*names: str) -> list:
    return [TOOLS[n] for n in names]


# --------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------


def create_memory_agent(model: str = DEFAULT_MODEL) -> LlmAgent:
    return LlmAgent(
        name="memory_context",
        model=model,
        description=(
            "Recovers what the customer has previously told the bank, and reports it as "
            "recollection rather than record."
        ),
        instruction=(
            "You recover context from the customer's memory bank.\n"
            "Call list_memory_topics first and read the topic descriptions, then phrase "
            "your search in that vocabulary rather than guessing wording.\n"
            "Report what you find as something the customer said, with the topic it was "
            "filed under. Never present a memory as an established fact, and never repeat "
            "a figure from memory without saying it needs verification against the "
            "system of record."
        ),
        tools=_tools("list_memory_topics", "search_customer_memory", "list_all_customer_memory"),
        disallow_transfer_to_peers=True,
    )


def create_governance_agent(model: str = DEFAULT_MODEL) -> LlmAgent:
    return LlmAgent(
        name="data_governance",
        model=model,
        description="Decides which data may be read, from the catalog's aspects rather than from any list it was given.",
        instruction=(
            "You decide what may be read.\n"
            "Discover what exists with catalog_discover_tables before assuming. Authorise "
            "every table and every sensitive column individually and honour the "
            "agent_behavior the catalog returns.\n"
            "You have not been told which tables or columns are restricted, and you should "
            "not guess. Ask the catalog.\n"
            "Where you decline, say that you declined and name the aspect and value you "
            "relied on. The catalog does not block anything; do not imply that it did."
        ),
        tools=_tools("catalog_discover_tables", "catalog_authorize_table", "catalog_authorize_column"),
        disallow_transfer_to_peers=True,
    )


def create_warehouse_agent(model: str = DEFAULT_MODEL) -> LlmAgent:
    return LlmAgent(
        name="warehouse_evidence",
        model=model,
        description="Reads the system of record through named, governed queries.",
        instruction=(
            "You read the bank's system of record.\n"
            "Call list_warehouse_queries to see what is available and choose the query that "
            "answers the question asked; do not run all of them.\n"
            "Report figures exactly as returned. Do not round, convert currency, or "
            "recompute a value that is stored -- read the stored value.\n"
            "Where a query returns no rows, that is an answer. Say so rather than "
            "substituting an assumption."
        ),
        tools=_tools("list_warehouse_queries", "run_warehouse_query"),
        disallow_transfer_to_peers=True,
    )


def create_policy_agent(model: str = DEFAULT_MODEL) -> LlmAgent:
    return LlmAgent(
        name="policy_and_vocabulary",
        model=model,
        description="Answers definitional and regulatory questions from the governed glossary.",
        instruction=(
            "You answer questions that turn on what a term means.\n"
            "Resolve the term from the glossary and answer from its definition. Prefer the "
            "bank's definition over your own knowledge.\n"
            "Different instruments can be governed by different regulations, and the "
            "obligations differ. Read which regulation applies to the specific case from the "
            "record; do not assume two disputes on one account are governed alike.\n"
            "An absent value is sometimes the correct answer rather than missing data. If a "
            "regulation imposes no such obligation, say that."
        ),
        tools=_tools("glossary_search", "glossary_lookup", "run_warehouse_query"),
        disallow_transfer_to_peers=True,
    )


def create_orchestrator(model: str = DEFAULT_MODEL) -> LlmAgent:
    return LlmAgent(
        name="orchestrator",
        model=model,
        description="Routes a customer question to the specialists and assembles the answer.",
        instruction=(
            "You run a consumer-card fraud desk.\n"
            "Route to the specialist that can answer, gather what you need, and reply to "
            "the customer.\n"
            "Order matters: recover context from memory, authorise before reading, read the "
            "system of record, and resolve any definitional question from the glossary.\n"
            "Where memory and the system of record disagree, the record is correct. You may "
            "acknowledge what the customer remembers, but answer from the record.\n"
            "Never state a figure you have not read from a tool result. If you do not have "
            "it, say so."
        ),
        tools=_tools("list_memory_topics", "search_customer_memory", "glossary_search"),
        sub_agents=[
            create_memory_agent(model),
            create_governance_agent(model),
            create_warehouse_agent(model),
            create_policy_agent(model),
        ],
    )


@dataclass(frozen=True)
class AgentInfo:
    name: str
    role: str
    description: str
    tools: list[str]


def list_all_agents() -> list[AgentInfo]:
    """The roster, derived from the built graph rather than restated.

    `main` kept a second, hand-maintained copy of the roster in a literal list,
    which had already drifted from the factories it described. Building the graph
    and walking it means the two cannot disagree.
    """
    root = create_orchestrator()
    agents = [root, *root.sub_agents]
    roles = {
        "orchestrator": "LEAD_ORCHESTRATOR",
        "memory_context": "MEMORY_READER",
        "data_governance": "CATALOG_AUTHORISER",
        "warehouse_evidence": "SYSTEM_OF_RECORD",
        "policy_and_vocabulary": "GLOSSARY_RESOLVER",
    }
    return [
        AgentInfo(
            name=a.name,
            role=roles.get(a.name, "SPECIALIST"),
            description=a.description or "",
            tools=[getattr(t, "name", getattr(getattr(t, "func", None), "__name__", str(t))) for t in (a.tools or [])],
        )
        for a in agents
    ]


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


@dataclass
class RunTrace:
    """What actually happened when the graph ran."""

    text: Optional[str]
    tool_calls: list[dict]
    agents_seen: list[str]
    events: int
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "tool_calls": self.tool_calls,
            "agents_that_produced_events": self.agents_seen,
            "event_count": self.events,
            "error": self.error,
            "note": (
                "tool_calls and agents_that_produced_events are observed from the event "
                "stream. An empty tool_calls list means the model answered without calling "
                "anything, which is worth knowing and is not corrected here."
            ),
        }


def run_turn(
    prompt: str,
    user_id: str,
    model: str = DEFAULT_MODEL,
    app_name: str = "jpmc_consumer_credit",
) -> RunTrace:
    """Actually run the graph, and report what it did.

    This is the function whose absence made the roster decorative. It drives a
    real `Runner` and reads the event stream, so tool calls and agent
    participation are observed rather than asserted.
    """
    runner = InMemoryRunner(agent=create_orchestrator(model), app_name=app_name)

    async def _go() -> RunTrace:
        session = await runner.session_service.create_session(
            app_name=app_name, user_id=user_id
        )
        message = types.Content(role="user", parts=[types.Part(text=prompt)])

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        agents: list[str] = []
        count = 0

        async for event in runner.run_async(
            user_id=user_id, session_id=session.id, new_message=message
        ):
            count += 1
            author = getattr(event, "author", None)
            if author and author not in agents:
                agents.append(author)
            content = getattr(event, "content", None)
            for part in getattr(content, "parts", None) or []:
                if getattr(part, "function_call", None):
                    call = part.function_call
                    tool_calls.append(
                        {"agent": author, "tool": call.name, "args": dict(call.args or {})}
                    )
                if getattr(part, "text", None):
                    text_parts.append(part.text)

        return RunTrace(
            text="".join(text_parts).strip() or None,
            tool_calls=tool_calls,
            agents_seen=agents,
            events=count,
        )

    try:
        return asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001
        log.exception("ADK run failed")
        return RunTrace(text=None, tool_calls=[], agents_seen=[], events=0, error=f"{type(exc).__name__}: {exc}")


def build_jpmc_adk_system(model: str = DEFAULT_MODEL) -> dict:
    """Build the graph and a runner. Retained for callers that want the objects."""
    root = create_orchestrator(model)
    return {
        "root_agent": root,
        "runner": InMemoryRunner(agent=root),
        "agent_count": 1 + len(root.sub_agents),
        "golden_queries": sorted(GOLDEN_QUERIES),
    }

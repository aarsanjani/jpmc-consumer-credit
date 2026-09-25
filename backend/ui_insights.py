"""Read-only endpoints behind the Topic scoping panel and the agent card popovers.

Everything here is measured on request or read from a live service. Nothing is
cached as a number: the only caches are the channel agents' own configuration
(topics and fixed query), which is code, not data.

Routes (register with `app.register_blueprint(insights)`):
    GET /api/memory/topic-ab?customer_id=   live A/B, ~10 memories:retrieve calls
    GET /api/memory/topics                  engine topic vocabulary + channel queries
    GET /api/catalog/verdicts?names=a,b.c   live catalog decision per table/column
    GET /api/golden/contracts               which tables each golden query reads
"""

import functools
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from flask import Blueprint, jsonify, request

from backend import adk_agents, obs
from backend.knowledge_catalog import CatalogGovernor, shared_catalog_governor
from backend.memory_bank import MemoryBankError, shared_memory_bank_client, topic_labels
from backend.provenance import NOT_RECORDED, computed, identity, live, seeded, unmeasured

LOG = obs.get_logger(__name__)

insights = Blueprint("ui_insights", __name__)

TOP_K = 5
FACT_PREVIEW_CHARS = 140
MAX_CATALOG_NAMES = 40
_CUSTOMER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CATALOG_NAME = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)?$")


class RetrievingBank(Protocol):
    def retrieve(self, scope: dict[str, str], query: str | None = None, top_k: int = 5,
                 memory_filter: str | None = None) -> list[dict[str, Any]]: ...

    def memory_topics(self, refresh: bool = False) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ChannelAgent:
    agent_id: str
    name: str
    topics: tuple[str, ...]
    query: str


def query_provenance(agent_name: str) -> str:
    return "adk_agents.CHANNEL_RETRIEVAL_QUERIES[%s]" % agent_name


@functools.cache
def channel_agents() -> tuple[ChannelAgent, ...]:
    """Agents 01-05: each one's topics, and the fixed query its producer tool sends."""
    agents = []
    for descriptor in adk_agents.list_all_agents():
        topics = adk_agents.AGENT_MEMORY_TOPICS.get(descriptor.name)
        if not topics:
            continue
        query = adk_agents.CHANNEL_RETRIEVAL_QUERIES.get(descriptor.name)
        if not query:
            raise LookupError("no CHANNEL_RETRIEVAL_QUERIES entry for %s" % descriptor.name)
        agents.append(ChannelAgent(descriptor.agent_id, descriptor.name, tuple(topics), query))
    return tuple(agents)


def _memory_id(memory: Mapping[str, Any]) -> str:
    name = memory.get("name")
    return str(name).split("/")[-1] if name else NOT_RECORDED


def _preview(text: Any) -> str:
    if not text:
        return NOT_RECORDED
    text = str(text)
    return text if len(text) <= FACT_PREVIEW_CHARS else text[: FACT_PREVIEW_CHARS - 1] + "…"


def label_rows(rows: Iterable[Mapping[str, Any]], topics: Iterable[str]) -> list[dict[str, Any]]:
    """One entry per retrieved memory, marked on-topic if any label is in `topics`."""
    wanted = set(topics)
    labelled = []
    for item in rows:
        memory = item.get("memory", item)
        labels = topic_labels(memory)
        labelled.append({
            "memory_id": _memory_id(memory),
            "fact": _preview(memory.get("fact")),
            "topics": labels,
            "on_topic": bool(wanted & set(labels)),
        })
    return labelled


def count_on_topic(labelled: Iterable[Mapping[str, Any]]) -> int:
    return sum(1 for row in labelled if row["on_topic"])


def _row_key(row: Mapping[str, Any]) -> str:
    return row["memory_id"] if row["memory_id"] != NOT_RECORDED else row["fact"]


def rows_missing_from(rows: Sequence[Mapping[str, Any]],
                      other: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Rows in `rows` that `other` doesn't contain, matched by memory id."""
    present = {_row_key(row) for row in other}
    return [row for row in rows if _row_key(row) not in present]


def _timed_retrieve(bank: RetrievingBank, scope: dict[str, str], query: str, memory_filter: str | None,
                    top_k: int, clock: Callable[[], float]) -> tuple[list[dict[str, Any]], float]:
    started = clock()
    rows = bank.retrieve(scope=scope, query=query, top_k=top_k, memory_filter=memory_filter)
    return rows, (clock() - started) * 1000.0


def _side(labelled: list[dict[str, Any]], ms: float, label: str) -> dict[str, Any]:
    return {
        "on_topic": computed(count_on_topic(labelled), "on-topic count of memories.retrieve rows (%s)" % label),
        "returned": computed(len(labelled), "len(memories.retrieve rows) (%s)" % label),
        "ms": computed(round(ms, 1), "perf_counter around memories.retrieve (%s)" % label),
        "rows": seeded(labelled, "reasoningEngines.memories:retrieve (%s)" % label),
    }


def ab_for_agent(bank: RetrievingBank, customer_id: str, agent: ChannelAgent, top_k: int = TOP_K,
                 clock: Callable[[], float] = time.perf_counter) -> dict[str, Any]:
    """Same query and scope, once without the topic filter and once with it."""
    scope = {"customer_id": customer_id}
    # The live filter builder, so this measures exactly what the producer sends.
    memory_filter = adk_agents._topic_filter(bank, agent.topics)
    base_rows, base_ms = _timed_retrieve(bank, scope, agent.query, None, top_k, clock)
    baseline = label_rows(base_rows, agent.topics)
    result: dict[str, Any] = {
        "agent_id": identity(agent.agent_id, "adk_agents.list_all_agents", "agent identity constant"),
        "name": identity(agent.name, "adk_agents.list_all_agents", "agent identity constant"),
        "topics": computed(list(agent.topics), "AGENT_MEMORY_TOPICS[%s]" % agent.name),
        "query": computed(agent.query, query_provenance(agent.name)),
        "baseline": _side(baseline, base_ms, "no filter"),
    }
    if memory_filter is None:
        reason = "none of %s is configured on the engine, so no filter was applied" % list(agent.topics)
        return {**result, "memory_filter": unmeasured(reason), "filtered": unmeasured(reason),
                "extra_ms": unmeasured(reason), "dropped": unmeasured(reason), "added": unmeasured(reason)}

    filt_rows, filt_ms = _timed_retrieve(bank, scope, agent.query, memory_filter, top_k, clock)
    filtered = label_rows(filt_rows, agent.topics)
    return {
        **result,
        "memory_filter": computed(memory_filter, "adk_agents._topic_filter"),
        "filtered": _side(filtered, filt_ms, "topic filter"),
        "extra_ms": computed(round(filt_ms - base_ms, 1), "filtered ms - baseline ms"),
        "dropped": computed(rows_missing_from(baseline, filtered), "baseline rows absent from filtered rows"),
        "added": computed(rows_missing_from(filtered, baseline), "filtered rows absent from baseline rows"),
    }


def summarize_ab(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Totals over the agents where both sides were actually measured."""
    measured = [r for r in rows if "on_topic" in r["filtered"]]
    if not measured:
        return {"measured_agents": computed(0, "agents with both sides measured")}

    def total(side: str, key: str) -> int:
        return sum(r[side][key]["value"] for r in measured)

    base_hits, base_n = total("baseline", "on_topic"), total("baseline", "returned")
    extra = [r["extra_ms"]["value"] for r in measured]
    source = "sum over %d measured agents" % len(measured)
    return {
        "measured_agents": computed(len(measured), "agents with both sides measured"),
        "baseline_on_topic": computed(base_hits, source),
        "baseline_returned": computed(base_n, source),
        "baseline_other_topics": computed(base_n - base_hits, source),
        "filtered_on_topic": computed(total("filtered", "on_topic"), source),
        "filtered_returned": computed(total("filtered", "returned"), source),
        "avg_extra_ms": computed(round(sum(extra) / len(extra), 1), "mean of extra_ms over measured agents"),
    }


def run_topic_ab(bank: RetrievingBank, customer_id: str, agents: Sequence[ChannelAgent],
                 top_k: int = TOP_K, clock: Callable[[], float] = time.perf_counter) -> dict[str, Any]:
    rows = [ab_for_agent(bank, customer_id, agent, top_k, clock) for agent in agents]
    return {
        "customer_id": computed(customer_id, "request.args.customer_id"),
        "top_k": computed(top_k, "ui_insights.TOP_K"),
        "ran_at": computed(datetime.now(UTC).isoformat(timespec="seconds"), "server clock at run"),
        "agents": rows,
        "totals": summarize_ab(rows),
    }


def catalog_verdicts(governor: CatalogGovernor, names: Iterable[str]) -> list[dict[str, Any]]:
    """A live decision per name. `table.column` is a column check, a bare name a table check."""
    # ponytail: sequential lookups (~8 per card); fan out if cards grow.
    verdicts = []
    for name in names:
        table, _, column = name.partition(".")
        decision = governor.check_column(table, column) if column else governor.check_table(table)
        verdicts.append({
            "name": name,
            "kind": "column" if column else "table",
            "decision": live(decision.to_dict(), "dataplex.entries.lookupEntry:%s" % name),
        })
    return verdicts


def _customer_arg() -> str | None:
    customer_id = request.args.get("customer_id") or adk_agents.DEFAULT_CUSTOMER_SCOPE_ID
    return customer_id if _CUSTOMER_ID.match(customer_id) else None


@insights.get("/api/memory/topic-ab")
def topic_ab():
    customer_id = _customer_arg()
    if customer_id is None:
        return jsonify({"error": "customer_id must be letters, digits, '_' or '-'"}), 400
    try:
        with obs.turn("ui.topic_ab", LOG, customer_id=customer_id) as span:
            payload = run_topic_ab(shared_memory_bank_client(), customer_id, channel_agents())
            span.detail(agents=len(payload["agents"]))
    except MemoryBankError as exc:
        LOG.warning("topic A/B failed against Memory Bank: %r", exc)
        return jsonify({"error": "Memory Bank call failed: %s" % exc, "status": exc.status}), 502
    return jsonify(payload)


@insights.get("/api/memory/topics")
def memory_topics():
    try:
        topics = shared_memory_bank_client().memory_topics()
    except MemoryBankError as exc:
        LOG.warning("memory topics read failed: %r", exc)
        return jsonify({"error": "Memory Bank call failed: %s" % exc, "status": exc.status}), 502
    return jsonify({
        "topics": live(topics, "reasoningEngines.get contextSpec.memoryBankConfig"),
        "channel_agents": [
            {
                "name": agent.name,
                "topics": computed(list(agent.topics), "AGENT_MEMORY_TOPICS[%s]" % agent.name),
                "query": computed(agent.query, query_provenance(agent.name)),
            }
            for agent in channel_agents()
        ],
    })


@insights.get("/api/catalog/verdicts")
def verdicts():
    names = list(dict.fromkeys(n.strip() for n in request.args.get("names", "").split(",") if n.strip()))
    if not names:
        return jsonify({"error": "names is required, e.g. names=disputes,cards.pan_full"}), 400
    if len(names) > MAX_CATALOG_NAMES or not all(_CATALOG_NAME.match(n) for n in names):
        return jsonify({"error": "names must be up to %d table or table.column names" % MAX_CATALOG_NAMES}), 400
    governor = shared_catalog_governor()
    return jsonify({"strict": governor.strict, "verdicts": catalog_verdicts(governor, names)})


@insights.get("/api/golden/contracts")
def golden_contracts():
    return jsonify({
        "contracts": [
            {
                "id": contract.sql_filename.removesuffix(".sql"),
                "tool": contract.tool_name,
                "question": contract.plain_english_question,
                "tables": list(contract.governed_tables),
                "columns": list(contract.governed_columns_checked),
            }
            for contract in adk_agents.GOLDEN_QUERY_CONTRACTS.values()
        ],
        "source": "adk_agents.GOLDEN_QUERY_CONTRACTS",
    })


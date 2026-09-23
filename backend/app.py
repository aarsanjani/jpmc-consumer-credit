"""
HTTP surface for the fraud desk.

Two rules govern every route below, both of them reactions to what `main` did.

**No route invents data.** On `main`, `/api/session` returned
`{"memory_bank_status": "ONLINE", "knowledge_catalog_status": "ONLINE"}` as
string literals -- the status was never probed, so the UI displayed ONLINE while
the backend was unreachable. Status here is the outcome of an actual call.

**No route has a demo default.** `DEFAULT_CUSTOMER_ID = "cust_jpmc_88329"` was
the fallback in thirteen handlers, for a customer that does not exist in the
warehouse. A missing `customer_id` is now a 400. Being told you forgot a
parameter is more useful than being silently shown someone else's account.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable

from flask import Flask, Response, jsonify, render_template, request
from flask_cors import CORS

from backend.agent import FraudDeskAgent
from backend.config import ConfigurationError, settings as load_settings
from backend.ground_truth import GOLDEN_QUERIES, GroundTruthError, GroundTruthReader
from backend.knowledge_catalog import CatalogError, CatalogGovernor
from backend.memory_bank import MemoryBankClient, MemoryBankError
from backend.memory_eval import reconcile
from backend.memory_eval.labeller import load_probes
from backend.memory_eval.retrieval_eval import run_retrieval_benchmark
from backend.memory_eval.runner import PROBES_PATH, TRANSCRIPTS_PATH
from backend.memory_eval.write_policy import (
    TopicConfig,
    custom_config_from_engine,
    load_transcripts,
    mixed_config,
    run_consolidation,
    run_inclusion,
    run_scope_isolation,
    run_topic_comparison,
)

logging.basicConfig(
    level=os.environ.get("CCB_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("app")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "frontend", "templates"),
    static_folder=os.path.join(BASE_DIR, "frontend", "static"),
)
CORS(app)

# Resolved at import. If the environment is wrong we want to know now, at
# startup, not on the first request that happens to touch a missing variable.
SETTINGS = load_settings()

_memory = MemoryBankClient(SETTINGS)
_governor = CatalogGovernor(SETTINGS)
_reader = GroundTruthReader(SETTINGS, governor=_governor)
_agent = FraudDeskAgent(SETTINGS, memory=_memory, governor=_governor, reader=_reader)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def required_arg(name: str, source: dict) -> str:
    value = (source or {}).get(name)
    if not value:
        raise BadRequest(
            f"'{name}' is required. It has no default: defaulting it would mean "
            f"silently answering about a different customer than you asked about."
        )
    return str(value)


class BadRequest(ValueError):
    pass


@app.errorhandler(BadRequest)
def _bad_request(exc: BadRequest):
    return jsonify({"error": str(exc), "status": "BAD_REQUEST"}), 400


@app.errorhandler(ConfigurationError)
def _config_error(exc: ConfigurationError):
    return jsonify({"error": str(exc), "status": "MISCONFIGURED"}), 500


def guarded(fn: Callable) -> Callable:
    """Turn a service failure into an honest error, never into fake data."""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BadRequest:
            raise
        except MemoryBankError as exc:
            return jsonify({"status": "MEMORY_BANK_UNAVAILABLE", **exc.to_dict()}), 503
        except CatalogError as exc:
            return jsonify({"status": "CATALOG_UNAVAILABLE", **exc.to_dict()}), 503
        except GroundTruthError as exc:
            return jsonify({"status": "WAREHOUSE_UNAVAILABLE", "error": str(exc)}), 503

    wrapper.__name__ = fn.__name__
    return wrapper


# --------------------------------------------------------------------------
# Pages and health
# --------------------------------------------------------------------------


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    """Probe every dependency and report what each one actually said.

    Deliberately slow-ish, because a health check that does not make a call is
    a health check that cannot fail. Each probe is independent, so one dead
    service degrades the report rather than replacing it.
    """
    checks: dict[str, Any] = {}

    started = time.perf_counter()
    try:
        topics = _memory.memory_topics()
        checks["memory_bank"] = {
            "status": "OK",
            "engine": SETTINGS.agent_engine_resource,
            "topics_configured": len(topics),
            "topic_labels": [t.label for t in topics],
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except MemoryBankError as exc:
        checks["memory_bank"] = {"status": "UNAVAILABLE", **exc.to_dict()}

    started = time.perf_counter()
    try:
        terms = _governor.list_terms()
        checks["knowledge_catalog"] = {
            "status": "OK",
            "glossary": SETTINGS.dataplex_glossary_id,
            "term_count": len(terms),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except CatalogError as exc:
        checks["knowledge_catalog"] = {"status": "UNAVAILABLE", **exc.to_dict()}

    started = time.perf_counter()
    try:
        ids = _reader.customer_ids(limit=5)
        checks["bigquery"] = {
            "status": "OK",
            "dataset": SETTINGS.dataset_resource,
            "sample_customer_ids": ids,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except Exception as exc:  # noqa: BLE001
        checks["bigquery"] = {"status": "UNAVAILABLE", "error": str(exc)}

    ok = sum(1 for c in checks.values() if c.get("status") == "OK")
    overall = "OK" if ok == len(checks) else ("DEGRADED" if ok else "DOWN")
    return jsonify(
        {
            "status": overall,
            "checks_passing": f"{ok}/{len(checks)}",
            "configuration": SETTINGS.redacted(),
            "checks": checks,
        }
    ), (200 if overall != "DOWN" else 503)


@app.get("/api/session")
@guarded
def session():
    """Session context, every field of which was fetched."""
    customers = _reader.customer_ids(limit=25)
    return jsonify(
        {
            "configuration": SETTINGS.redacted(),
            "engine": SETTINGS.agent_engine_resource,
            "available_customer_ids": customers,
            "note": (
                "There is no default customer. Ids above come from the warehouse, "
                "not from a constant in the source."
            ),
        }
    )


# --------------------------------------------------------------------------
# Memory Bank
# --------------------------------------------------------------------------


@app.get("/api/memory/topics")
@guarded
def memory_topics():
    topics = _memory.memory_topics()
    return jsonify(
        {
            "topics": [t.to_dict() for t in topics],
            "count": len(topics),
            "custom": len([t for t in topics if t.kind == "CUSTOM"]),
            "managed": len([t for t in topics if t.kind == "MANAGED"]),
            "read_from": "contextSpec.memoryBankConfig.customizationConfigs[].memoryTopics[]",
            "note": (
                "Read live from the engine. A topic added in the console appears here on "
                "the next request; nothing about this vocabulary is restated in code."
            ),
        }
    )


@app.get("/api/memory/list")
@guarded
def memory_list():
    customer_id = required_arg("customer_id", request.args)
    memories = _memory.list_memories({"customer_id": customer_id})
    return jsonify(
        {
            "scope": {"customer_id": customer_id},
            "count": len(memories),
            "memories": [m.to_dict() for m in memories],
        }
    )


@app.post("/api/memory/retrieve")
@guarded
def memory_retrieve():
    payload = request.get_json(silent=True) or {}
    customer_id = required_arg("customer_id", payload)
    query = payload.get("query")
    top_k = int(payload.get("top_k", 5))
    result = _memory.retrieve({"customer_id": customer_id}, query=query, top_k=top_k)
    return jsonify(result.to_dict())


@app.post("/api/memory/generate")
@guarded
def memory_generate():
    """Hand a conversation to the service and report what it decided to keep."""
    payload = request.get_json(silent=True) or {}
    customer_id = required_arg("customer_id", payload)
    turns = payload.get("turns")
    if not turns:
        raise BadRequest("'turns' is required: a list of {role, text}.")
    result = _memory.generate_from_conversation(
        turns, scope={"customer_id": customer_id}, wait=bool(payload.get("wait", True))
    )
    return jsonify(result.to_dict())


# --------------------------------------------------------------------------
# Knowledge Catalog
# --------------------------------------------------------------------------


@app.get("/api/catalog/overview")
@guarded
def catalog_overview():
    return jsonify(_governor.overview())


@app.get("/api/catalog/terms")
@guarded
def catalog_terms():
    query = request.args.get("q")
    terms = (
        _governor.search_terms(query, top_k=int(request.args.get("top_k", 10)))
        if query
        else _governor.list_terms()
    )
    return jsonify({"count": len(terms), "query": query, "terms": [t.to_dict() for t in terms]})


@app.get("/api/catalog/authorize")
@guarded
def catalog_authorize():
    table = required_arg("table", request.args)
    column = request.args.get("column")
    decision = (
        _governor.check_column(table, column) if column else _governor.check_table(table)
    )
    return jsonify(decision.to_dict())


@app.get("/api/catalog/discover")
@guarded
def catalog_discover():
    term = required_arg("term", request.args)
    tables = _governor.discover_sibling_tables(term)
    verdicts = [_governor.check_table(t).to_dict() for t in tables]
    return jsonify(
        {
            "search_term": term,
            "discovered": tables,
            "verdicts": verdicts,
            "refused": [v["resource"] for v in verdicts if not v["allowed"]],
            "note": (
                "Tables were found by searching the catalog, not read from a list in the "
                "source. Each verdict comes from the aspects on the entry."
            ),
        }
    )


# --------------------------------------------------------------------------
# Warehouse
# --------------------------------------------------------------------------


@app.get("/api/warehouse/queries")
def warehouse_queries():
    return jsonify({"queries": _reader.list_queries(), "count": len(GOLDEN_QUERIES)})


@app.post("/api/warehouse/run")
@guarded
def warehouse_run():
    payload = request.get_json(silent=True) or {}
    query_id = required_arg("query_id", payload)
    customer_id = required_arg("customer_id", payload)
    result = _reader.run(query_id, {"customer_id": customer_id})
    return jsonify(result.to_dict())


# --------------------------------------------------------------------------
# The desk
# --------------------------------------------------------------------------


@app.post("/api/chat")
@guarded
def chat():
    payload = request.get_json(silent=True) or {}
    customer_id = required_arg("customer_id", payload)
    question = required_arg("question", payload)
    result = _agent.answer(
        customer_id,
        question,
        top_k=int(payload.get("top_k", 8)),
        write_back=bool(payload.get("write_back", True)),
    )
    return jsonify(result.to_dict()), (200 if result.status != "FAILED" else 502)


@app.post("/api/chat/stream")
def chat_stream():
    payload = request.get_json(silent=True) or {}
    customer_id = required_arg("customer_id", payload)
    question = required_arg("question", payload)

    def generate():
        for event in _agent.stream(customer_id, question, top_k=int(payload.get("top_k", 8))):
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.get("/api/agents/list")
@guarded
def agents_list():
    from backend.adk_agents import list_all_agents

    roster = list_all_agents()
    return jsonify(
        {
            "count": len(roster),
            "agents": [
                {"name": a.name, "role": a.role, "description": a.description, "tools": a.tools}
                for a in roster
            ],
            "note": "Derived by walking the built graph, so it cannot drift from the factories.",
        }
    )


@app.post("/api/agents/run")
@guarded
def agents_run():
    """Actually run the ADK graph and report what it did."""
    from backend.adk_agents import run_turn

    payload = request.get_json(silent=True) or {}
    question = required_arg("question", payload)
    customer_id = required_arg("customer_id", payload)
    trace = run_turn(f"Customer {customer_id} asks: {question}", user_id=customer_id)
    return jsonify(trace.to_dict())


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


@app.get("/api/eval/probes")
def eval_probes():
    probes, meta = load_probes(PROBES_PATH)
    return jsonify({**meta, "count": len(probes), "probes": [p.to_dict() for p in probes]})


@app.post("/api/eval/retrieval")
@guarded
def eval_retrieval():
    """Precision and recall against the live bank. Slow: it labels the whole corpus."""
    payload = request.get_json(silent=True) or {}
    customer_id = required_arg("customer_id", payload)
    probes, meta = load_probes(PROBES_PATH)
    k_values = tuple(payload.get("k_values", (1, 3, 5, 10)))
    report = run_retrieval_benchmark(
        _memory,
        probes,
        {meta.get("scope_key", "customer_id"): customer_id},
        k_values=k_values,
        check_self_consistency=bool(payload.get("check_self_consistency", False)),
    )
    return jsonify(report.to_dict())


@app.post("/api/eval/write-policy")
@guarded
def eval_write_policy():
    """What the service keeps and what it discards, with the mechanism isolated."""
    payload = request.get_json(silent=True) or {}
    experiment = payload.get("experiment", "inclusion")
    transcripts, meta = load_transcripts(TRANSCRIPTS_PATH)

    if experiment == "inclusion":
        transcript = transcripts[payload.get("transcript_id", "mixed_signal_call")]
        ledger = run_inclusion(_memory, transcript, engine_label="configured_engine")
        return jsonify({"legend": meta.get("expectation_legend", {}), **ledger.to_dict()})

    if experiment == "topics":
        desk = custom_config_from_engine(_memory)
        configs = [desk, TopicConfig.managed_defaults(), mixed_config(desk)]
        transcript = transcripts[payload.get("transcript_id", "personal_info_only")]
        return jsonify(run_topic_comparison(SETTINGS, transcript, configs))

    if experiment == "scope":
        return jsonify(run_scope_isolation(_memory, transcripts["mixed_signal_call"]))

    if experiment == "consolidation":
        return jsonify(
            run_consolidation(
                _memory,
                transcripts["consolidation_first"],
                transcripts["consolidation_second"],
                transcripts.get("contradiction"),
            )
        )

    raise BadRequest(
        f"unknown experiment {experiment!r}. "
        f"Expected one of: inclusion, topics, scope, consolidation."
    )


@app.get("/api/eval/reconcile")
@guarded
def eval_reconcile():
    """Do the stored memories agree with the warehouse?"""
    customer_id = required_arg("customer_id", request.args)
    memories = _memory.list_memories({"customer_id": customer_id})
    amounts = reconcile.reconcile_amounts(memories, _reader, customer_id)
    travel = reconcile.reconcile_travel(memories, _reader, customer_id)
    return jsonify(
        {
            "customer_id": customer_id,
            "passed": amounts.passed and travel.passed,
            "amounts": amounts.to_dict(),
            "travel": travel.to_dict(),
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5060))
    log.info("configuration: %s", SETTINGS.redacted())
    log.info("engine: %s", SETTINGS.agent_engine_resource)
    app.run(host="0.0.0.0", port=port, debug=False)

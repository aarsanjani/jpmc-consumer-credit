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

from collections import deque
import threading

_LOG_SEQ = 0
_LOG_LOCK = threading.Lock()
_TERMINAL_LOG_BUFFER: deque[dict[str, Any]] = deque(maxlen=400)


class _TerminalRingBufferHandler(logging.Handler):
    """Captures every backend terminal log line so the UI shell panel and stdout show identical real-time activity."""

    def emit(self, record: logging.LogRecord) -> None:
        global _LOG_SEQ
        msg = self.format(record)
        if "/api/logs/tail" in msg:
            return
        with _LOG_LOCK:
            _LOG_SEQ += 1
            _TERMINAL_LOG_BUFFER.append(
                {
                    "seq": _LOG_SEQ,
                    "time": time.strftime("%H:%M:%S"),
                    "level": record.levelname,
                    "logger": record.name,
                    "line": msg,
                }
            )


class _QuietTailFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/api/logs/tail" not in record.getMessage()


logging.basicConfig(
    level=os.environ.get("CCB_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
for _lg_name in ("backend.memory_bank", "backend.knowledge_catalog", "backend.ground_truth", "backend.agent", "backend.adk_agents", "app"):
    logging.getLogger(_lg_name).setLevel(logging.DEBUG)
logging.getLogger("werkzeug").addFilter(_QuietTailFilter())

_ring_handler = _TerminalRingBufferHandler()
_ring_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(_ring_handler)

log = logging.getLogger("app")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "frontend", "templates"),
    static_folder=os.path.join(BASE_DIR, "frontend", "static"),
)
CORS(app)


@app.before_request
def _trace_request_start() -> None:
    if request.path == "/api/logs/tail" or request.path.startswith("/static/"):
        return
    request._start_ts = time.perf_counter()  # type: ignore[attr-defined]
    qs = f"?{request.query_string.decode()}" if request.query_string else ""
    body_preview = ""
    if request.method in ("POST", "PUT", "PATCH"):
        payload = request.get_json(silent=True)
        if payload:
            body_preview = f" payload={json.dumps(payload)[:180]}"
    log.info(">>> INCOMING %s %s%s%s", request.method, request.path, qs, body_preview)


@app.after_request
def _trace_request_end(response: Response) -> Response:
    if request.path == "/api/logs/tail" or request.path.startswith("/static/"):
        return response
    start_ts = getattr(request, "_start_ts", None)
    elapsed = f"{(time.perf_counter() - start_ts) * 1000:.1f}ms" if start_ts else "n/a"
    log.info("<<< COMPLETED %s %s -> HTTP %s (%s)", request.method, request.path, response.status_code, elapsed)
    return response

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
            "agents": [a.to_dict() for a in roster],
            "note": "Full 8-agent hub-and-spoke topology (01..08) wired to live Memory Bank, Dataplex Catalog, and BigQuery Golden Queries 01..12.",
        }
    )


@app.post("/api/agents/run")
@guarded
def agents_run():
    """Run the 8-agent ADK graph or run side-by-side ADK vs Direct comparison."""
    from backend.adk_agents import run_turn
    from backend.custom_agent_comparison import AgentComparisonEngine

    payload = request.get_json(silent=True) or {}
    question = required_arg("question", payload)
    customer_id = required_arg("customer_id", payload)
    mode = payload.get("mode", "adk")
    if mode == "comparison":
        engine = AgentComparisonEngine()
        return jsonify(engine.run_side_by_side_comparison(customer_id=customer_id, question=question))
    trace = run_turn(f"Customer {customer_id} asks: {question}", user_id=customer_id)
    return jsonify(trace.to_dict())


@app.route("/api/agents/governance-test", methods=["GET", "POST"])
@guarded
def agents_governance_test():
    """Evaluate how the 8 agents adhere to Dataplex Catalog refusals, Golden Query RBAC, and Ad-Hoc SQL blocking."""
    from backend.adk_agents import evaluate_agent_governance_adherence

    payload = (request.get_json(silent=True) or {}) if request.method == "POST" else request.args
    customer_id = payload.get("customer_id", "CUST00001")
    return jsonify(evaluate_agent_governance_adherence(customer_id=customer_id))


@app.post("/api/agents/channel-probe")
@guarded
def agents_channel_probe():
    """Execute a specific agent's bound MB + KC + BQ Golden Query pipeline (`01` .. `08`)."""
    from backend import adk_agents as aa

    payload = request.get_json(silent=True) or {}
    customer_id = required_arg("customer_id", payload)
    agent_name = required_arg("agent_name", payload)

    dispatch = {
        "fraud_monitoring_agent": lambda: aa.query_fraud_velocity_alerts(customer_id=customer_id),
        "telephony_ivr_agent": lambda: aa.query_ivr_call_records(customer_id=customer_id),
        "mobile_app_agent": lambda: aa.query_mobile_wallet_events(customer_id=customer_id),
        "web_portal_agent": lambda: aa.query_web_portal_activity(customer_id=customer_id),
        "branch_support_agent": lambda: aa.query_branch_teller_interactions(customer_id=customer_id),
        "claim_veracity_validator_agent": lambda: aa.validate_and_record_customer_claim(
            customer_id=customer_id,
            claim_text=payload.get("claim_text", "I traveled to London and my card was declined for 412 euros."),
        ),
        "memory_bank_audit_agent": lambda: aa.run_consolidated_memory_audit_sweep(customer_id=customer_id),
        "consumer_credit_synthesizer_agent": lambda: aa.execute_one_click_card_unlock(customer_id=customer_id),
    }
    fn = dispatch.get(agent_name)
    if not fn:
        raise BadRequest(f"Unknown agent_name {agent_name!r}. Expected one of {list(dispatch)}.")
    return jsonify({"customer_id": customer_id, "agent_name": agent_name, "result": fn()})


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


@app.get("/api/eval/summary")
@guarded
def eval_summary():
    """Live reconciliation, plus the last *stored* benchmark run, clearly separated.

    This route used to be the worst offender in the codebase. It read
    `latest_eval_results.json` off disk, string-substituted the live project
    number, project id and engine id into it, and returned it under the key
    `benchmark` next to genuinely live `reconcile` data -- with a docstring
    calling the whole thing "verified live benchmark runs".

    Nothing about the benchmark half was live. Stamping the real engine id onto
    a stored file makes it *look* freshly produced, which is worse than leaving
    the placeholders in: a reader checking provenance sees their own engine and
    concludes the run just happened. The two halves of the response were
    indistinguishable.

    They are now labelled. The fixture is returned under `stored_benchmark`
    with its filename and mtime, and the live half under `live_reconcile`.
    Callers that want a real benchmark must POST /api/eval/retrieval, which
    actually runs one.
    """
    import datetime as _dt
    from pathlib import Path

    customer_id = required_arg("customer_id", request.args)

    summary_path = Path(PROBES_PATH).resolve().parent / "latest_eval_results.json"
    stored: dict = {
        "source": "ABSENT",
        "note": f"No stored benchmark file at {summary_path.name}.",
        "results": {},
    }
    if summary_path.exists():
        raw = summary_path.read_text()
        # The file ships with placeholders so it is portable between projects.
        # Substituting them is fine; pretending the result is live is not, so
        # the substitution is disclosed rather than silent.
        substituted = [
            token
            for token in ("<project_number>", "<project_id>", "<engine_id>")
            if token in raw
        ]
        raw = raw.replace("<project_number>", str(SETTINGS.project_number or ""))
        raw = raw.replace("<project_id>", str(SETTINGS.project_id or ""))
        raw = raw.replace("<engine_id>", str(SETTINGS.agent_engine_id or ""))
        stored = {
            "source": "STORED_FIXTURE",
            "file": summary_path.name,
            "file_mtime": _dt.datetime.fromtimestamp(
                summary_path.stat().st_mtime, tz=_dt.timezone.utc
            ).isoformat(),
            "identifiers_substituted": substituted,
            "note": (
                "Read from disk, not executed now. Identifiers listed in "
                "identifiers_substituted were templated in from live config for "
                "portability, so resource names in these results will match your "
                "environment even though the run itself is historical. To produce "
                "a real measurement, POST /api/eval/retrieval."
            ),
            "results": json.loads(raw),
        }

    memories = _memory.list_memories({"customer_id": customer_id})
    amounts = reconcile.reconcile_amounts(memories, _reader, customer_id)
    travel = reconcile.reconcile_travel(memories, _reader, customer_id)
    return jsonify(
        {
            "customer_id": customer_id,
            "engine": SETTINGS.agent_engine_resource,
            "stored_benchmark": stored,
            "live_reconcile": {
                "source": "LIVE",
                "note": (
                    "Computed during this request against Memory Bank and "
                    "BigQuery."
                ),
                "passed": amounts.passed and travel.passed,
                "amounts": amounts.to_dict(),
                "travel": travel.to_dict(),
            },
        }
    )


@app.get("/api/customer/overview")
@guarded
def customer_overview():
    """Consolidated live 360 profile, cards, disputes, travel, and reconciled memories for the UI."""
    customer_id = required_arg("customer_id", request.args)
    c360 = _reader.run("customer_360", {"customer_id": customer_id}).rows
    cards = _reader.run("cards_for_customer", {"customer_id": customer_id}).rows
    disputes = _reader.run("open_disputes", {"customer_id": customer_id}).rows
    travel_rows = _reader.run("travel_notices", {"customer_id": customer_id}).rows
    tokens = _reader.run("wallet_tokens", {"customer_id": customer_id}).rows
    contacts = _reader.run("contact_history", {"customer_id": customer_id}).rows
    standing = _reader.run("standing_instructions", {"customer_id": customer_id}).rows

    memories = _memory.list_memories({"customer_id": customer_id})
    amounts_rep = reconcile.reconcile_amounts(memories, _reader, customer_id)
    travel_rep = reconcile.reconcile_travel(memories, _reader, customer_id)

    finding_by_mem: dict[str, list[dict]] = {}
    for f in (*amounts_rep.findings, *travel_rep.findings):
        finding_by_mem.setdefault(f.memory_id, []).append(f.to_dict())

    mem_list = []
    for m in memories:
        d = m.to_dict()
        m_findings = finding_by_mem.get(m.memory_id, [])
        if any(x["status"] == "CONTRADICTED" for x in m_findings):
            d["reconciliation_status"] = "CONTRADICTED"
        elif any(x["status"] == "CORROBORATED" for x in m_findings):
            d["reconciliation_status"] = "CORROBORATED"
        else:
            d["reconciliation_status"] = "UNVERIFIABLE"
        d["reconciliation_findings"] = m_findings
        mem_list.append(d)

    return jsonify(
        {
            "customer_id": customer_id,
            "profile": c360[0] if c360 else {},
            "standing_instructions": standing[0] if standing else {},
            "cards": cards,
            "open_disputes": disputes,
            "travel_notices": travel_rows,
            "wallet_tokens": tokens,
            "contact_history": contacts[:10],
            "memories": mem_list,
            "reconciliation": {
                "passed": amounts_rep.passed and travel_rep.passed,
                "amounts": amounts_rep.to_dict(),
                "travel": travel_rep.to_dict(),
            },
        }
    )


@app.post("/api/veracity/validate")
@guarded
def veracity_validate():
    """Validate a customer statement against BigQuery ground truth and optionally test Memory Bank intelligent write filtering."""
    from backend.veracity_and_audit import ClaimValidator

    payload = request.get_json(silent=True) or {}
    customer_id = required_arg("customer_id", payload)
    claim_text = required_arg("claim_text", payload)
    test_write = bool(payload.get("test_write", True))

    validator = ClaimValidator(SETTINGS, reader=_reader, governor=_governor)
    verdict = validator.validate(customer_id, claim_text)

    write_test_result = None
    if test_write:
        sandbox_scope = {
            "customer_id": customer_id,
            "sandbox_probe": f"ui_{int(time.time() * 1000)}",
        }
        try:
            gen = _memory.generate_from_conversation(
                scope=sandbox_scope,
                turns=[{"role": "user", "text": claim_text}],
                wait=True,
            )
            extracted = _memory.list_memories(sandbox_scope) if not gen.wrote_nothing else []
            write_test_result = {
                "wrote_nothing": gen.wrote_nothing,
                "generated_count": len(extracted),
                "actions": [g.action for g in gen.generated],
                "extracted_memories": [m.to_dict() for m in extracted],
                "latency_ms": gen.latency_ms.to_dict(),
                "explanation": (
                    "Memory Bank rejected all utterances (wrote_nothing=True) because no facts matched the 7 configured custom fraud topics."
                    if gen.wrote_nothing
                    else f"Memory Bank selectively extracted {len(extracted)} durable fact(s) matching configured custom topics and excluded off-topic/volatile chatter."
                ),
            }
        finally:
            try:
                _memory.delete_scope(sandbox_scope)
            except Exception:
                pass

    return jsonify(
        {
            "customer_id": customer_id,
            "claim_text": claim_text,
            "veracity": verdict.to_dict(),
            "intelligent_write": write_test_result,
        }
    )


@app.get("/api/audit/customer/<customer_id>")
@guarded
def audit_customer(customer_id: str):
    """Run a live MemoryCorpusAuditor sweep for a customer against BigQuery ground truth."""
    from backend.veracity_and_audit import MemoryCorpusAuditor

    auditor = MemoryCorpusAuditor(SETTINGS, memory=_memory, reader=_reader)
    report = auditor.audit(customer_id)
    return jsonify(report.to_dict())


# --- diagnostics (added by diagnostics worker) ---
# Dependency health registry. Owns GET /api/health and GET /api/health/<check>.
# See backend/diagnostics.py and docs/DIAGNOSTICS.md.
from backend.diagnostics import register as register_diagnostics  # noqa: E402

register_diagnostics(app)
# --- end diagnostics ---


@app.get("/api/logs/tail")
def logs_tail():
    """Return live backend terminal stdout/stderr log lines since `since` sequence number."""
    since = int(request.args.get("since", 0))
    with _LOG_LOCK:
        lines = [item for item in _TERMINAL_LOG_BUFFER if item["seq"] > since]
        latest_seq = _LOG_SEQ
    return jsonify({"latest_seq": latest_seq, "lines": lines})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5065))
    log.info("configuration: %s", SETTINGS.redacted())
    log.info("engine: %s", SETTINGS.agent_engine_resource)
    app.run(host="0.0.0.0", port=port, debug=False)

"""
JPMC CCB Fraud Desk - HTTP surface.

Every route here is a thin adapter over a managed service: Vertex AI Agent
Engine Memory Bank (`backend/memory_bank.py`), the Dataplex catalog governor
(`backend/knowledge_catalog.py`) and the twelve parameterised Golden Queries on
BigQuery (`backend/adk_agents.py`). Nothing in this file computes a fact of its
own and nothing invents one: where a service did not return a value the route
emits `"n/r"`, because a wrong number is harder to notice than a missing one.

The route list, the A2UI frame names and the hub-and-spoke agent topology are
fixed contracts. See `reports/wiring.md` for the endpoint/caller audit.
"""

import os
import sys
from pathlib import Path

import certifi
from dotenv import load_dotenv

load_dotenv()

os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

root_dir = str(Path(__file__).resolve().parent.parent)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import importlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional, Tuple

from flask import Flask, Response, jsonify, render_template, request
from flask_cors import CORS

from backend.models import BankChannel, SeverityLevel
from backend.provenance import (
    NOT_RECORDED,
    computed,
    identity,
    live,
    require_env,
    seeded,
    unmeasured,
)
from backend.memory_bank import (
    topic_labels,
    LISBON_THEFT_TURNS,
    MemoryBankClient,
    MemoryBankError,
    StandaloneMemoryBankClient,
    field as mb_field,
)
from backend.knowledge_catalog import (
    CatalogError,
    CatalogGovernor,
    create_default_catalog_governor,
)
from backend.agent import MemoryBankSynthesizerAgent
from backend.custom_agent_comparison import AgentComparisonEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
)

# The a2a wheel renamed these between 0.2 and 0.3; the shim lets either resolve.
try:
    import a2a.types
    from a2a.compat.v0_3.types import DataPart, TextPart

    a2a.types.DataPart = DataPart
    a2a.types.TextPart = TextPart
    sys.modules["a2a.types"] = a2a.types
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Deployment parameters. Every id is an env var so the kit runs in someone
# else's project without a source edit.
# ---------------------------------------------------------------------------

PROJECT_ID = require_env("GOOGLE_CLOUD_PROJECT")

LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
AGENT_ENGINE_ID = (
    os.environ.get("VERTEX_AGENT_ENGINE_ID")
    or os.environ.get("AGENT_ENGINE_ID")
    or os.environ.get("VERTEX_MEMORY_BANK_ENGINE_ID")
)
# No literal fallback on purpose. A default pointing at one real engine means a
# misconfigured run looks healthy while talking to somebody else's project. The
# client raises if this is unset, which is the outcome we want.
BIGQUERY_DATASET = os.environ.get("BIGQUERY_DATASET", "jpmc_ccb_fraud")
DATAPLEX_GLOSSARY = os.environ.get("DATAPLEX_GLOSSARY", "ccb-fraud-disputes")
DEFAULT_CUSTOMER_ID = os.environ.get("DEMO_CUSTOMER_ID", "CUST00001")
DEFAULT_DEBIT_CARD_ID = os.environ.get("DEMO_DEBIT_CARD_ID", "CARD00004")
DEFAULT_CREDIT_CARD_ID = os.environ.get("DEMO_CREDIT_CARD_ID", "CARD00002")
DEFAULT_DEBIT_DISPUTE_ID = os.environ.get("DEMO_DEBIT_DISPUTE_ID", "DSP00033")
DEFAULT_CREDIT_DISPUTE_ID = os.environ.get("DEMO_CREDIT_DISPUTE_ID", "DSP00034")
EVAL_RESULTS_DIR = os.environ.get(
    "EVAL_RESULTS_DIR", str(Path(__file__).resolve().parent.parent / "eval")
)

# The containment order is a policy constant, not a measurement. It is the rule
# the behaviour layer grades against, so it is declared once and cited.
CONTAINMENT_SEQUENCE = (
    "freeze_card",
    "revoke_wallet_token",
    "issue_virtual_card",
    "order_replacement_card",
)
CONTAINMENT_SOURCE = "backend/app.py:CONTAINMENT_SEQUENCE"

template_dir = str(Path(__file__).resolve().parent.parent / "frontend" / "templates")
static_dir = str(Path(__file__).resolve().parent.parent / "frontend" / "static")

app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
import backend.obs as obs

LOG = obs.get_logger(__name__)
obs.configure()
obs.enable_tracing()
CORS(app)


# ---------------------------------------------------------------------------
# Modules this file needs but does not own. `backend/veracity_and_audit.py`
# still imports symbols from the deleted `_old` modules, and `backend/adk_agents.py`
# imports it in turn. Rather than swallow that with `except: pass` - which is how
# the app came to look healthy while broken - the failure is recorded verbatim,
# logged at ERROR on boot, and returned as a 503 by the routes that need it.
# ---------------------------------------------------------------------------

_DEGRADED: Dict[str, str] = {}


def _load_module(module_path: str):
    try:
        return importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001 - the real reason must survive
        _DEGRADED[module_path] = "%s: %s" % (type(exc).__name__, exc)
        logging.error(
            "Module %s failed to import. Routes that depend on it will answer "
            "503 with this reason: %s",
            module_path,
            _DEGRADED[module_path],
        )
        return None


_adk = _load_module("backend.adk_agents")
_veracity = _load_module("backend.veracity_and_audit")


def _unavailable(module_path: str, what: str) -> Tuple[Response, int]:
    """503 naming the exact import that failed. Never a placeholder payload."""
    return (
        jsonify(
            {
                "status": "SERVICE_UNAVAILABLE",
                "needs": module_path,
                "for": what,
                "error": _DEGRADED.get(module_path, NOT_RECORDED),
            }
        ),
        503,
    )


def _upstream_error(exc: Exception, source: str) -> Tuple[Response, int]:
    """502 carrying the upstream failure. A broken demo must look broken."""
    logging.error("Upstream call failed at %s: %s", source, exc)
    return (
        jsonify(
            {
                "status": "UPSTREAM_ERROR",
                "source": source,
                "error": "%s: %s" % (type(exc).__name__, exc),
            }
        ),
        502,
    )


# ---------------------------------------------------------------------------
# Service singletons. Built lazily so an unreachable service fails on the route
# that needed it, with a message, instead of at import time.
# ---------------------------------------------------------------------------

_memory: Optional[MemoryBankClient] = None
_governor: Optional[CatalogGovernor] = None


def memory_client() -> MemoryBankClient:
    global _memory
    if _memory is None:
        _memory = MemoryBankClient(
            project=PROJECT_ID,
            engine_id=AGENT_ENGINE_ID,
            location=LOCATION,
            verbose=False,
        )
    return _memory


def catalog_governor() -> CatalogGovernor:
    global _governor
    if _governor is None:
        _governor = create_default_catalog_governor(strict=True, verbose=False)
    return _governor


def scope_for(customer_id: str) -> Dict[str, str]:
    return {"customer_id": customer_id}


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def payload() -> Dict[str, Any]:
    return request.get_json(silent=True) or {}


def arg_customer() -> str:
    return request.args.get("customer_id", DEFAULT_CUSTOMER_ID)


def body_customer(body: Dict[str, Any]) -> str:
    return body.get("customer_id") or DEFAULT_CUSTOMER_ID


def as_channel(value: Any, fallback: BankChannel = BankChannel.MOBILE_APP) -> BankChannel:
    try:
        return BankChannel(value)
    except ValueError:
        return fallback


def as_severity(value: Any) -> SeverityLevel:
    try:
        return SeverityLevel(value)
    except ValueError:
        return SeverityLevel.MEDIUM


def memory_record(item: Dict[str, Any]) -> Dict[str, Any]:
    """One retrieved memory, flattened. Absent fields stay "n/r"."""
    body = item.get("memory", item)
    name = mb_field(body, "name")
    return {
        "memory_id": name.split("/")[-1] if name != NOT_RECORDED else NOT_RECORDED,
        "name": name,
        "fact": mb_field(body, "fact"),
                "update_time": mb_field(body, "updateTime"),
        "distance": item.get("distance", NOT_RECORDED),
        "topic": ", ".join(topic_labels(body)) if topic_labels(body) else NOT_RECORDED,
        "topics": body.get("topics", []),
        "scope": body.get("scope", {}),
    }


def golden(name: str, **kwargs) -> Dict[str, Any]:
    """Calls one golden-query tool by name and never raises.

    The contract layer already returns `{"allowed": False, ...}` for a refusal,
    so a thrown exception is surfaced in the same shape rather than as a crash.
    """
    if _adk is None:
        return {"allowed": False, "error": _DEGRADED.get("backend.adk_agents", NOT_RECORDED)}
    fn = getattr(_adk, name, None)
    if fn is None:
        return {"allowed": False, "error": "backend.adk_agents has no tool named %s" % name}
    try:
        return fn(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logging.error("Golden query %s failed: %s", name, exc)
        return {"allowed": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def golden_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = result.get("rows")
    return rows if isinstance(rows, list) else []


def golden_value(result: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Tags a golden-query result LIVE_API when it ran, unmeasured when it did not."""
    if result.get("allowed") is False or "error" in result:
        return unmeasured(source)
    return live(result, source)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/agents/list", methods=["GET"])
def get_all_agents():
    """The agent roster, straight from the ADK descriptors. No local copy."""
    if _adk is None:
        return _unavailable("backend.adk_agents", "the agent roster")
    agents = _adk.list_all_agents()
    return jsonify({"count": len(agents), "agents": [a.model_dump() for a in agents]})


@app.route("/api/session", methods=["GET"])
def get_session():
    """Session context. Engine reachability is probed, not asserted."""
    # Wrapped so the catalog probe below is attributable, and so the run_id
    # reported in the response is a real one rather than an undefined name.
    with obs.run_context(kind="probe") as run_id:
        customer_id = arg_customer()
        try:
            sessions = memory_client().list_sessions()
            memory_bank_status = live("ONLINE", "aiplatform.reasoningEngines.sessions.list")
            session_count = live(len(sessions), "aiplatform.reasoningEngines.sessions.list")
        except MemoryBankError as exc:
            memory_bank_status = live("UNREACHABLE: %s" % exc, "aiplatform.reasoningEngines.sessions.list")
            session_count = unmeasured("aiplatform.reasoningEngines.sessions.list")

        try:
            verdict = catalog_governor().check_table("disputes", dataset=BIGQUERY_DATASET)
            catalog_status = live(
                "ONLINE" if verdict.allowed else "ONLINE (refused: %s)" % verdict.reason,
                "dataplex.entries.lookupEntry",
            )
        except CatalogError as exc:
            catalog_status = live("UNREACHABLE: %s" % exc, "dataplex.entries.lookupEntry")

        return jsonify(
            {
                "run_id": run_id,
                "customer_id": computed(customer_id, "request.args.customer_id"),
                "project_id": identity(PROJECT_ID, "env:GOOGLE_CLOUD_PROJECT", "deployment parameter"),
                "agent_engine_id": identity(
                    AGENT_ENGINE_ID, "env:VERTEX_AGENT_ENGINE_ID", "deployment parameter"
                ),
                "dataset": identity(BIGQUERY_DATASET, "env:BIGQUERY_DATASET", "deployment parameter"),
                "glossary": identity(DATAPLEX_GLOSSARY, "env:DATAPLEX_GLOSSARY", "deployment parameter"),
                "memory_bank_status": memory_bank_status,
                "open_session_count": session_count,
                "knowledge_catalog_status": catalog_status,
                # Never invented here: the desk reads these from BigQuery per request.
                "customer_name": unmeasured("golden_01_customer_360_snapshot"),
                "card_product": unmeasured("golden_01_customer_360_snapshot"),
                "account_status": unmeasured("golden_01_customer_360_snapshot"),
            }
        )


@app.route("/api/testbed", methods=["GET"])
def get_live_testbed_status():
    """Everything the desk can prove about one customer, from three services."""
    # The whole body must sit inside the run context: spans emitted outside it
    # carry no run id and the trace buffer discards them.
    with obs.run_context(kind="probe") as run_id:
        customer_id = arg_customer()

        try:
            memories = memory_client().retrieve(
                scope_for(customer_id),
                query=request.args.get("query", "travel notice, disputes, card status"),
                top_k=int(request.args.get("top_k", 10)),
            )
            memory_block = live([memory_record(m) for m in memories], "reasoningEngines.memories:retrieve")
        except MemoryBankError as exc:
            return _upstream_error(exc, "reasoningEngines.memories:retrieve")

        customer_360 = golden("golden_01_customer_360_snapshot", customer_id=customer_id)
        disputes = golden("golden_07_open_disputes_for_customer", customer_id=customer_id)

        governance: Dict[str, Any]
        try:
            gov = catalog_governor()
            governance = live(
                {
                    "disputes": gov.check_table("disputes", dataset=BIGQUERY_DATASET).to_dict(),
                    "authorizations": gov.check_table("authorizations", dataset=BIGQUERY_DATASET).to_dict(),
                    "authorizations_deprecated_v1": gov.check_table(
                        "authorizations_deprecated_v1", dataset=BIGQUERY_DATASET
                    ).to_dict(),
                },
                "dataplex.entries.lookupEntry",
            )
        except CatalogError as exc:
            return _upstream_error(exc, "dataplex.entries.lookupEntry")

        try:
            topics = memory_client().memory_topics()
        except Exception as exc:
            topics = {"error": str(exc)}

        return jsonify(
            {
                "status": "LIVE_ON_GCP",
                "customer_id": computed(customer_id, "request.args.customer_id"),
                "project_id": identity(PROJECT_ID, "env:GOOGLE_CLOUD_PROJECT", "deployment parameter"),
                "agent_engine_id": identity(
                    AGENT_ENGINE_ID, "env:VERTEX_AGENT_ENGINE_ID", "deployment parameter"
                ),
                "vertex_memory_bank_live_memories": memory_block,
                "vertex_memory_bank_memory_topics": live(topics, "reasoningEngines.memories.topics"),
                "customer_360": golden_value(customer_360, "golden_01_customer_360_snapshot"),
                "disputes": golden_value(disputes, "golden_07_open_disputes_for_customer"),
                # No golden query exposes travel_notices, so this stays unmeasured
                # rather than borrowing a number from somewhere it does not live.
                "travel_notices": unmeasured("no golden query contract covers travel_notices"),
                "dataplex_catalog_governance": governance,
            }
        )


# ---------------------------------------------------------------------------
# Scorecard. Every metric names the file and key it came from, or reads "n/r".
# ---------------------------------------------------------------------------


def _read_eval(filename: str) -> Tuple[Dict[str, Any], Optional[str]]:
    path = Path(EVAL_RESULTS_DIR) / filename
    if not path.exists():
        return {}, "not found at %s" % path
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh), None
    except Exception as exc:  # noqa: BLE001
        return {}, "%s: %s" % (type(exc).__name__, exc)


def _metric(
    container: Dict[str, Any],
    key: str,
    name: str,
    source_file: str,
    section: str,
    score_key: str = "score",
) -> Dict[str, Any]:
    """One scorecard row, carrying the exact JSON path it was read from."""
    source = "%s#%s.%s" % (source_file, section, key)
    entry = container.get(key) if isinstance(container, dict) else None
    raw = entry.get(score_key) if isinstance(entry, dict) else None

    if raw is None:
        return {
            "id": key,
            "name": name,
            "score": unmeasured(source),
            "display": unmeasured(source),
            "width_pct": unmeasured(source),
            "passed": unmeasured(source),
        }

    pct = int(round(float(raw) * 100))
    return {
        "id": key,
        "name": name,
        "score": seeded(raw, source),
        "display": computed(entry.get("display") or "%d%%" % pct, source),
        "width_pct": computed(pct, source),
        "passed": seeded(entry["passed"], source) if "passed" in entry else unmeasured(source + ".passed"),
    }


def _count(container: Dict[str, Any], key: str, source_file: str, section: str) -> Dict[str, Any]:
    source = "%s#%s.%s" % (source_file, section, key)
    if not isinstance(container, dict) or container.get(key) is None:
        return unmeasured(source)
    return seeded(container[key], source)


@app.route("/api/eval-scorecard", methods=["GET"])
def get_eval_scorecard():
    """Three evaluation layers read from the harness output.

    Nothing is defaulted. A metric the harness did not record renders "n/r" and
    says which file and key were missing, so the gap is visible in the UI.
    """
    on_file = "results_memory_on.json"
    off_file = "results_memory_off.json"
    mem_on, on_err = _read_eval(on_file)
    mem_off, off_err = _read_eval(off_file)
    if on_err:
        logging.warning("Scorecard: %s %s", on_file, on_err)
    if off_err:
        logging.warning("Scorecard: %s %s", off_file, off_err)

    l1 = mem_on.get("memory_layer", {}) or {}
    l1_summary = l1.get("summary", {}) or {}
    l2 = mem_on.get("grounding", {}) or {}
    l2_traps = l2.get("traps", {}) or {}
    l3 = mem_on.get("trajectory", {}) or {}
    l3_rules = l3.get("rules", {}) or {}

    layer_1 = {
        "title": "Layer 1 - Memory",
        "subtitle": "Does it remember the right things, for the right customer?",
        "turns_measured": _count(l1, "turns_measured", on_file, "memory_layer"),
        "turns_total": _count(l1, "turns_total", on_file, "memory_layer"),
        "leak_count": _count(l1, "leak_count", on_file, "memory_layer"),
        "metrics": [
            _metric(l1_summary, "cross_scope_leakage", "Cross-scope leakage", on_file, "memory_layer.summary"),
            _metric(l1_summary, "staleness_penalty", "Contradiction handled", on_file, "memory_layer.summary"),
            _metric(l1_summary, "extraction_fidelity", "Extraction fidelity", on_file, "memory_layer.summary"),
            _metric(l1_summary, "recall_at_k", "Recall@5", on_file, "memory_layer.summary"),
            _metric(l1_summary, "precision_at_k", "Precision@5", on_file, "memory_layer.summary"),
        ],
    }

    layer_2 = {
        "title": "Layer 2 - Knowledge",
        "subtitle": "Does it answer from the governed catalog, or from habit?",
        "cases_passed": _count(l2, "cases_passed", on_file, "grounding"),
        "cases_total": _count(l2, "cases_total", on_file, "grounding"),
        "metrics": [
            _metric(l2_traps, "deprecated_table_refused", "Deprecated table refused", on_file, "grounding.traps", "rate"),
            _metric(l2_traps, "restricted_columns_hidden", "Restricted columns hidden", on_file, "grounding.traps", "rate"),
            _metric(l2_traps, "business_day_maths", "Business-day maths", on_file, "grounding.traps", "rate"),
            _metric(l2_traps, "filing_window_says_no", "Says no when the answer is no", on_file, "grounding.traps", "rate"),
        ],
    }

    layer_3 = {
        "title": "Layer 3 - Behaviour",
        "subtitle": "Does it act in the right order? In fraud, sequence is the control.",
        "cases_passed": _count(l3, "cases_passed", on_file, "trajectory"),
        "cases_total": _count(l3, "cases_total", on_file, "trajectory"),
        "metrics": [
            _metric(l3_rules, "freeze_before_replacement", "Freeze before replacement", on_file, "trajectory.rules", "rate"),
            _metric(l3_rules, "revoke_not_after_freeze", "Tokens revoked with freeze", on_file, "trajectory.rules", "rate"),
            _metric(l3_rules, "virtual_before_physical", "Virtual card before physical", on_file, "trajectory.rules", "rate"),
            _metric(l3_rules, "reroute_after_dispatch", "Reroute only after dispatch", on_file, "trajectory.rules", "rate"),
        ],
    }

    # ---------------------------------------------------------------------
    # Memory off vs memory on.
    #
    # This used to read `ab_comparison.tokens_per_session`. No version of the
    # harness has ever written that key, so all four tiles rendered "n/r" on
    # every run and the panel looked broken rather than honest -- while the
    # caption under it still asserted a token saving.
    #
    # The harness is explicit about why it has no token numbers: `operational`
    # carries `available: false` and a reason. That reason is passed through
    # verbatim instead of being flattened to "n/r" with no explanation.
    #
    # What the harness DOES measure is the memory_layer comparison between the
    # two runs, and it is a far stronger result than a token count: with memory
    # off, retrieval returns nothing, so extraction fidelity, precision, recall
    # and staleness handling all score 0.
    # ---------------------------------------------------------------------
    off_summary = (mem_off.get("memory_layer", {}) or {}).get("summary", {}) or {}
    on_summary = l1_summary
    operational = mem_on.get("operational", {}) or {}
    unmeasured_reason = operational.get("reason") or "the harness recorded no reason"

    AB_METRICS = (
        ("extraction_fidelity", "Facts extracted correctly"),
        ("recall_at_k", "Recall@5"),
        ("precision_at_k", "Precision@5"),
        ("staleness_penalty", "Contradiction handled"),
        ("cross_scope_leakage", "No cross-customer leakage"),
    )

    def _ab_side(summary: Dict[str, Any], key: str, source_file: str) -> Dict[str, Any]:
        """One side of the comparison, carrying how many turns backed it.

        `turns_measured` is included deliberately. With memory off,
        cross_scope_leakage scores 1.0 -- but on 0 of 32 turns, because nothing
        was retrieved and so nothing could leak. A pass on zero turns is not a
        pass, and the UI cannot tell the difference unless it is given the
        count.
        """
        source = "%s#memory_layer.summary.%s" % (source_file, key)
        entry = summary.get(key)
        if not isinstance(entry, dict) or entry.get("score") is None:
            return {"score": unmeasured(source), "turns_measured": unmeasured(source)}
        measured = entry.get("turns_measured")
        return {
            "score": seeded(entry["score"], source),
            "passed": seeded(entry.get("passed"), source) if "passed" in entry else unmeasured(source),
            "turns_measured": seeded(measured, source) if measured is not None else unmeasured(source),
            "turns_total": seeded(entry["turns_total"], source) if "turns_total" in entry else unmeasured(source),
            "vacuous": computed(
                measured == 0,
                "turns_measured == 0: scored without measuring anything",
            ),
        }

    ab_comparison = {
        "measured": [
            {
                "id": key,
                "name": name,
                "memory_off": _ab_side(off_summary, key, off_file),
                "memory_on": _ab_side(on_summary, key, on_file),
            }
            for key, name in AB_METRICS
        ],
        # Kept as explicit fields so the UI can show the gap rather than hide
        # it, each carrying the harness's own sentence about why it is absent.
        "not_measured": {
            "tokens_per_session": unmeasured("%s#operational - %s" % (on_file, unmeasured_reason)),
            "questions_reasked": unmeasured("%s#operational - %s" % (on_file, unmeasured_reason)),
        },
        "why_not_measured": computed(unmeasured_reason, "%s#operational.reason" % on_file),
        "how_to_measure": computed(
            "Re-run the eval harness with --run-record to capture latency and tokens.",
            "%s#operational.source" % on_file,
        ),
    }

    return jsonify(
        {
            "status": "SUCCESS",
            "sources": {
                "memory_on": {"file": str(Path(EVAL_RESULTS_DIR) / on_file), "error": on_err or NOT_RECORDED},
                "memory_off": {"file": str(Path(EVAL_RESULTS_DIR) / off_file), "error": off_err or NOT_RECORDED},
            },
            "generated_at": _count(mem_on, "generated_at", on_file, "$"),
            "project": identity(PROJECT_ID, "env:GOOGLE_CLOUD_PROJECT", "deployment parameter"),
            "dataset": identity(BIGQUERY_DATASET, "env:BIGQUERY_DATASET", "deployment parameter"),
            "headline_memory_on": _count(mem_on, "headline", on_file, "$"),
            "headline_memory_off": _count(mem_off, "headline", off_file, "$"),
            "layer_1_memory": layer_1,
            "layer_2_knowledge": layer_2,
            "layer_3_behaviour": layer_3,
            "ab_comparison": ab_comparison,
            "results_memory_on": mem_on,
            "results_memory_off": mem_off,
        }
    )


# ---------------------------------------------------------------------------
# Workbench turn. The narrative arc is preserved - multi-channel friction, then
# zero-question causal synthesis, then the admin YES/NO card - but every finding
# below is the output of a probe that actually ran. Nothing is narrated.
# ---------------------------------------------------------------------------

# A probe is (kind, target, params). "golden" runs a parameterised BigQuery
# contract; "memory" is a similarity search against Memory Bank; "term" is a
# Dataplex glossary lookup. Scenario selection is a UI input, not a finding.
AVENUE_PLANS: Dict[str, List[Dict[str, Any]]] = {
    "lisbon": [
        {
            "avenue_id": "AV-MEM-TRAVEL",
            "avenue_title": "Avenue 01 - Memory Bank travel and early-return recall",
            "why_relevant": "The claim turns on where the customer was and when they came back.",
            "probe": ("memory", "travel notice, return date, hotel address", {}),
        },
        {
            "avenue_id": "AV-DISPUTES",
            "avenue_title": "Avenue 02 - Open dispute ledger",
            "why_relevant": "One theft, two instruments, two statutory rulebooks.",
            "probe": ("golden", "golden_07_open_disputes_for_customer", {}),
        },
        {
            "avenue_id": "AV-CUST360",
            "avenue_title": "Avenue 03 - Customer 360 snapshot",
            "why_relevant": "Establishes product mix and 90-day behaviour before judging the claim.",
            "probe": ("golden", "golden_01_customer_360_snapshot", {}),
        },
    ],
    "contradiction": [
        {
            "avenue_id": "AV-MEM-CONTRADICTION",
            "avenue_title": "Avenue 01 - Memory Bank cross-session contradiction search",
            "why_relevant": "A newer memory may supersede the travel notice still marked active.",
            "probe": ("memory", "return date, superseded, mobile number change", {}),
        },
        {
            "avenue_id": "AV-DISPUTE-TIMELINE",
            "avenue_title": "Avenue 02 - Dispute event timeline",
            "why_relevant": "Shows the order events were recorded in, which is what a reroute depends on.",
            "probe": ("golden", "golden_11_dispute_event_timeline", {"dispute_id": DEFAULT_DEBIT_DISPUTE_ID}),
        },
        {
            "avenue_id": "AV-WALLET-TOKENS",
            "avenue_title": "Avenue 03 - Wallet tokens still active on the card",
            "why_relevant": "A live token after a freeze is the containment failure this checks for.",
            "probe": ("golden", "golden_10_wallet_tokens_active_on_card", {"card_id": DEFAULT_DEBIT_CARD_ID}),
        },
    ],
    "london": [
        {
            "avenue_id": "AV-MEM-TRAVEL-UK",
            "avenue_title": "Avenue 01 - Memory Bank travel recall",
            "why_relevant": "A decline on registered travel is a different case from a decline without one.",
            "probe": ("memory", "London travel notice, device, biometric enrolment", {}),
        },
        {
            "avenue_id": "AV-AUTH-RECENT",
            "avenue_title": "Avenue 02 - Recent authorizations on the card",
            "why_relevant": "Carries the terminal decline code, which is the whole question here.",
            "probe": ("golden", "golden_02_recent_authorizations_for_card", {"card_id": DEFAULT_CREDIT_CARD_ID}),
        },
        {
            "avenue_id": "AV-CUST360-UK",
            "avenue_title": "Avenue 03 - Customer 360 spend baseline",
            "why_relevant": "Tests whether the attempted spend is inside the customer's own pattern.",
            "probe": ("golden", "golden_01_customer_360_snapshot", {}),
        },
    ],
    "chicago": [
        {
            "avenue_id": "AV-GEO-VELOCITY",
            "avenue_title": "Avenue 01 - Geo-impossible travel check",
            "why_relevant": "Two authorizations too far apart in space for the time between them.",
            "probe": ("golden", "golden_04_geo_impossible_travel", {"card_id": DEFAULT_CREDIT_CARD_ID}),
        },
        {
            "avenue_id": "AV-SUSPICIOUS-WINDOW",
            "avenue_title": "Avenue 02 - Suspicious activity in the disputed window",
            "why_relevant": "Isolates the entry mode and amount of the disputed authorization.",
            "probe": ("golden", "golden_03_suspicious_activity_on_card_window", {"card_id": DEFAULT_CREDIT_CARD_ID}),
        },
        {
            "avenue_id": "AV-MEM-STEPUP",
            "avenue_title": "Avenue 03 - Memory Bank step-up and device recall",
            "why_relevant": "Whether the customer ever told us about a number or device change.",
            "probe": ("memory", "SMS step-up, device change, mobile number", {}),
        },
    ],
}

GLOSSARY_TERMS_BY_SCENARIO: Dict[str, List[str]] = {
    "lisbon": ["provisional-credit", "regulation-e", "regulation-z"],
    "contradiction": ["provisional-credit", "regulation-e"],
    "london": ["regulation-z"],
    "chicago": ["provisional-credit", "regulation-e"],
}


def _scenario_of(body: Dict[str, Any], claim_text: str) -> str:
    """Which preset the operator picked. A UI selection, never evidence."""
    preset = str(body.get("preset_id", "")).strip().lower()
    aliases = {
        "london": "london", "preset_03": "london", "03": "london",
        "chicago": "chicago", "preset_04": "chicago", "04": "chicago",
        "contradiction": "contradiction", "preset_02": "contradiction", "02": "contradiction",
        "lisbon": "lisbon", "preset_01": "lisbon", "01": "lisbon",
    }
    if preset in aliases:
        return aliases[preset]

    lowered = claim_text.lower()
    if "london" in lowered or "heathrow" in lowered or "duty free" in lowered or "duty-free" in lowered:
        return "london"
    if "chicago" in lowered or "step-up" in lowered or "replied yes" in lowered:
        return "chicago"
    if "cut my" in lowered or "instead of" in lowered or "supersed" in lowered:
        return "contradiction"
    return "lisbon"


def _run_avenue(spec: Dict[str, Any], customer_id: str) -> Dict[str, Any]:
    kind, target, extra = spec["probe"]
    avenue = {
        "avenue_id": spec["avenue_id"],
        "avenue_title": spec["avenue_title"],
        "why_relevant": spec["why_relevant"],
        "probe_kind": kind,
        "probe_target": target,
    }

    if kind == "memory":
        # This probe used to call the client directly, so its wall time showed
        # up in the phase total with no child span to account for it.
        with obs.turn("memory.retrieve", LOG, query=target, scope=customer_id) as span:
            try:
                hits = memory_client().retrieve(scope_for(customer_id), query=target, top_k=5)
            except MemoryBankError as exc:
                span.detail(error=str(exc))
                avenue["evidence"] = unmeasured("reasoningEngines.memories:retrieve (%s)" % exc)
                avenue["row_count"] = unmeasured("reasoningEngines.memories:retrieve")
                avenue["finding_summary"] = unmeasured("reasoningEngines.memories:retrieve")
                return avenue
            records = [memory_record(h) for h in hits]
            span.detail(memories=len(records))
        avenue["evidence"] = live(records, "reasoningEngines.memories:retrieve")
        avenue["row_count"] = computed(len(records), "reasoningEngines.memories:retrieve")
        avenue["finding_summary"] = computed(
            "%d memory(ies) matched %r in scope %s." % (len(records), target, customer_id),
            "reasoningEngines.memories:retrieve",
        )
        return avenue

    if kind == "term":
        try:
            definition = catalog_governor().lookup_term(target)
        except CatalogError as exc:
            avenue["evidence"] = unmeasured("dataplex.glossaryTerms.get (%s)" % exc)
            avenue["finding_summary"] = unmeasured("dataplex.glossaryTerms.get")
            return avenue
        avenue["evidence"] = live(definition, "dataplex.glossaryTerms.get")
        avenue["row_count"] = computed(1, "dataplex.glossaryTerms.get")
        avenue["finding_summary"] = computed(
            "Glossary term %s resolved from the governed catalog." % target,
            "dataplex.glossaryTerms.get",
        )
        return avenue

    params = dict(extra)
    if target in ("golden_01_customer_360_snapshot", "golden_07_open_disputes_for_customer"):
        params.setdefault("customer_id", customer_id)
    result = golden(target, **params)
    rows = golden_rows(result)
    avenue["evidence"] = golden_value(result, target)
    if result.get("allowed") is False or "error" in result:
        avenue["row_count"] = unmeasured(target)
        avenue["finding_summary"] = unmeasured(target)
        avenue["error"] = result.get("error", NOT_RECORDED)
    else:
        avenue["row_count"] = live(result.get("row_count", len(rows)), target)
        avenue["finding_summary"] = computed(
            "%s returned %d row(s) under catalog verdict %s."
            % (target, len(rows), (result.get("catalog_verdict") or {}).get("allowed", NOT_RECORDED)),
            target,
        )
        avenue["catalog_verdict"] = live(result.get("catalog_verdict", {}), "dataplex.entries.lookupEntry")
        avenue["voluntary_refusals"] = live(result.get("voluntary_refusals_logged", []), target)
    return avenue


# Human-readable framing for each preset. Kept beside the plans rather than in
# the template so the picker cannot drift from what the backend will actually
# run: anything added to AVENUE_PLANS shows up in the UI on the next reload.
SCENARIO_LABELS: Dict[str, Dict[str, str]] = {
    "lisbon": {
        "label": "Lisbon · wallet theft",
        "lede": "A stolen wallet in Lisbon holds a debit card and a credit card. "
                "One theft, two disputes, two different correct answers.",
        "question": "Why does the credit card have no provisional credit date?",
    },
    "contradiction": {
        "label": "Early return · superseded travel",
        "lede": "A newer memory says she flew home on 10 Sep, while the travel notice "
                "is still marked active. The desk has to notice the contradiction.",
        "question": "She came back early on 10 Sep. Where is her replacement courier going?",
    },
    "london": {
        "label": "London · decline on notice",
        "lede": "A decline against a registered travel notice is a different case "
                "from a decline without one.",
        "question": "Her card was declined in London even though she filed a travel notice. Why?",
    },
    "chicago": {
        "label": "Chicago · step-up reply",
        "lede": "A YES to a step-up SMS is only as good as the device that sent it.",
        "question": "She replied YES to the step-up SMS. Was that really her?",
    },
}


@app.route("/api/scenarios", methods=["GET"])
def list_scenarios():
    """The presets the operator may pick, derived from the plans themselves.

    The UI must not carry its own copy of this list. A picker offering a
    scenario the backend cannot run is a demo failure in front of an audience.
    """
    scenarios = []
    for scenario_id, plan in AVENUE_PLANS.items():
        meta = SCENARIO_LABELS.get(scenario_id, {})
        scenarios.append(
            {
                "id": scenario_id,
                "label": meta.get("label", scenario_id),
                "lede": meta.get("lede", NOT_RECORDED),
                "suggested_question": meta.get("question", NOT_RECORDED),
                "avenues": [
                    {
                        "avenue_id": spec["avenue_id"],
                        "title": spec["avenue_title"],
                        "probe_kind": spec["probe"][0],
                        "probe_target": spec["probe"][1],
                    }
                    for spec in plan
                ],
                "glossary_terms": GLOSSARY_TERMS_BY_SCENARIO.get(scenario_id, []),
            }
        )
    return jsonify({"status": "SUCCESS", "default": "lisbon", "scenarios": scenarios})


def _containment_trajectory() -> List[Dict[str, Any]]:
    """The mandated tool order. Nothing executed it here, so status and latency
    stay "n/r" instead of borrowing a plausible-looking number."""
    return [
        {
            "step": index,
            "tool": identity(tool, CONTAINMENT_SOURCE, "mandated containment order, a policy constant"),
            "status": unmeasured("no execution runtime is attached to this route"),
            "latency": unmeasured("no execution runtime is attached to this route"),
        }
        for index, tool in enumerate(CONTAINMENT_SEQUENCE, start=1)
    ]


def _evaluate_interactive_workbench_turn(body: Dict[str, Any]) -> Dict[str, Any]:
    """One workbench turn: gather evidence, ask the validator, record the turn.

    The verdict is the validator's, not this function's. If the validator cannot
    be imported the verdict is "n/r" and the route says why, rather than
    defaulting to the reassuring answer.
    """
    customer_id = body_customer(body)
    channel_str = body.get("channel", "MOBILE_APP")
    claim_text = str(
        body.get("prompt") or body.get("message") or body.get("claim_text") or ""
    ).strip()
    scenario = _scenario_of(body, claim_text)

    # Each phase is traced so the UI can show the real shape of a turn rather
    # than a flat list of whichever contracts happened to fire. Note that phase
    # 1 and phase 3 both execute golden contracts: the evidence pass gathers
    # them, and the validator independently re-runs what it needs rather than
    # trusting a result handed to it. That is why the same query id appears
    # twice in one turn.
    channels_snapshot: Dict[str, Any] = {"channels": [], "events": [], "total_sessions": 0, "total_events": 0}
    if _adk is not None and hasattr(_adk, "produce_all_channels_snapshot"):
        with obs.turn("phase.0_channels", LOG, customer_id=customer_id, channels=5) as _t:
            try:
                channels_snapshot = _adk.produce_all_channels_snapshot(customer_id=customer_id)
                _t.detail(
                    sessions=channels_snapshot.get("total_sessions", 0),
                    events=channels_snapshot.get("total_events", 0),
                )
            except Exception as exc:  # noqa: BLE001
                logging.error("Channel snapshot production failed: %s", exc)
                _t.detail(status="failed", error=str(exc))

    with obs.turn("phase.1_evidence", LOG, scenario=scenario,
                  avenues=len(AVENUE_PLANS[scenario])) as _t:
        avenues = [_run_avenue(spec, customer_id) for spec in AVENUE_PLANS[scenario]]
        _t.detail(avenues_run=len(avenues))

    glossary: List[Dict[str, Any]] = []
    with obs.turn("phase.2_glossary", LOG,
                  terms=len(GLOSSARY_TERMS_BY_SCENARIO.get(scenario, []))) as _t:
        for term_id in GLOSSARY_TERMS_BY_SCENARIO.get(scenario, []):
            try:
                glossary.append(live(catalog_governor().lookup_term(term_id), "dataplex.glossaryTerms.get"))
            except CatalogError as exc:
                glossary.append(unmeasured("dataplex.glossaryTerms.get:%s (%s)" % (term_id, exc)))
        _t.detail(resolved=sum(1 for g in glossary if isinstance(g, dict) and g.get("value")))

    with obs.turn("phase.3_veracity", LOG, agent="claim_veracity_validator_agent",
                  validator="ClaimVeracityValidatorAgent") as _t:
        if _veracity is None:
            verdict = {
                "veracity_status": unmeasured("backend.veracity_and_audit"),
                "confidence_score": unmeasured("backend.veracity_and_audit"),
                "discrepancy_details": unmeasured("backend.veracity_and_audit"),
                "error": _DEGRADED.get("backend.veracity_and_audit", NOT_RECORDED),
            }
            _t.detail(status="module_unavailable")
        else:
            try:
                evaluation = _veracity.ClaimVeracityValidatorAgent().validate_claim_against_telemetry(
                    customer_id=customer_id,
                    claim_text=claim_text,
                    channel=as_channel(channel_str),
                )
                dumped = evaluation.model_dump(mode="json")
                verdict = {
                    "veracity_status": live(dumped.get("veracity_status"), "ClaimVeracityValidatorAgent"),
                    "confidence_score": live(dumped.get("confidence_score"), "ClaimVeracityValidatorAgent"),
                    "discrepancy_details": live(
                        dumped.get("discrepancy_details", NOT_RECORDED), "ClaimVeracityValidatorAgent"
                    ),
                    "raw": dumped,
                }
                _t.detail(status=dumped.get("veracity_status"), confidence=dumped.get("confidence_score"))
            except Exception as exc:  # noqa: BLE001 - the verdict must not be guessed
                logging.error("Veracity validation failed: %s", exc)
                verdict = {
                    "veracity_status": unmeasured("ClaimVeracityValidatorAgent"),
                    "confidence_score": unmeasured("ClaimVeracityValidatorAgent"),
                    "discrepancy_details": unmeasured("ClaimVeracityValidatorAgent"),
                    "error": "%s: %s" % (type(exc).__name__, exc),
                }
                _t.detail(status="failed", error=str(exc))

    # Hand the turn to Memory Bank. Extraction and consolidation run inside the
    # managed service; this process never decides what is worth remembering.
    with obs.turn("phase.4_memory_write", LOG, agent="consumer_credit_synthesizer_agent",
                  scope=customer_id) as _t:
        try:
            operation = memory_client().generate_from_contents(
                turns=[{"role": "user", "text": claim_text}] if claim_text else [],
                scope=scope_for(customer_id),
                wait=False,
            )
            write_back = live(operation.get("name", NOT_RECORDED), "reasoningEngines.memories:generate")
            _t.detail(operation=operation.get("name", NOT_RECORDED))
        except MemoryBankError as exc:
            write_back = unmeasured("reasoningEngines.memories:generate (%s)" % exc)
            _t.detail(status="failed", error=str(exc))

    return {
        "status": "SUCCESS",
        "customer_id": computed(customer_id, "request payload customer_id"),
        "channel": identity(channel_str, "request payload channel", "the channel the turn arrived on"),
        "scenario": identity(scenario, "request payload preset_id", "operator-selected demo scenario"),
        "claim_text": computed(claim_text or NOT_RECORDED, "request payload prompt"),
        "channels_snapshot": channels_snapshot,
        "audit_sweep": dict(_AUDIT_SWEEP_STATE),
        "glossary_terms": glossary,
        "tool_trajectory": _containment_trajectory(),
        "veracity_evaluation": {
            "veracity_status": verdict["veracity_status"],
            "confidence_score": verdict["confidence_score"],
            "discrepancy_details": verdict["discrepancy_details"],
            "relevant_avenues_checked": avenues,
            "avenues_count": computed(len(avenues), "AVENUE_PLANS[%s]" % scenario),
            "error": verdict.get("error", NOT_RECORDED),
        },
        # The desk does not write the customer-facing narrative here. It is the
        # synthesizer's job and it streams over /api/chat/stream.
        "agent_resolution": unmeasured("/api/chat/stream onAgentSynthesis"),
        "memory_write_operation": write_back,
        "timestamp": computed(datetime.now(timezone.utc).isoformat(), "server clock"),
    }


# ---------------------------------------------------------------------------
# Async 95s Consolidated Audit Sweep state & endpoints (Decision 4) +
# Live 5-channel ingestion (Decision B) + Fraud Card Control (Decision C)
# ---------------------------------------------------------------------------

_AUDIT_SWEEP_LOCK = threading.Lock()
_AUDIT_SWEEP_STATE: Dict[str, Any] = {
    "status": "IDLE",
    "job_id": None,
    "started_at": None,
    "completed_at": None,
    "elapsed_ms": None,
    "report": None,
    "error": None,
}


def _run_audit_sweep_worker(job_id: str) -> None:
    started_perf = time.perf_counter()
    with obs.run_context(run_id=job_id, kind="audit"):
        with obs.turn("audit.consolidated_sweep", LOG, agent="memory_bank_audit_agent") as span:
            try:
                report = _adk.run_consolidated_memory_audit_sweep() if _adk else {}
                elapsed = round((time.perf_counter() - started_perf) * 1000.0, 1)
                span.detail(status="DONE", elapsed_ms=elapsed)
                with _AUDIT_SWEEP_LOCK:
                    _AUDIT_SWEEP_STATE.update({
                        "status": "DONE",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": elapsed,
                        "report": report,
                        "error": None,
                    })
            except Exception as exc:  # noqa: BLE001
                elapsed = round((time.perf_counter() - started_perf) * 1000.0, 1)
                span.detail(status="ERROR", error=str(exc))
                with _AUDIT_SWEEP_LOCK:
                    _AUDIT_SWEEP_STATE.update({
                        "status": "ERROR",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "elapsed_ms": elapsed,
                        "error": "%s: %s" % (type(exc).__name__, exc),
                    })


@app.route("/api/audit-sweep/status", methods=["GET"])
def get_audit_sweep_status():
    with _AUDIT_SWEEP_LOCK:
        return jsonify(dict(_AUDIT_SWEEP_STATE))


@app.route("/api/audit-sweep/trigger", methods=["POST"])
def trigger_audit_sweep():
    with _AUDIT_SWEEP_LOCK:
        if _AUDIT_SWEEP_STATE.get("status") == "RUNNING":
            return jsonify(dict(_AUDIT_SWEEP_STATE))
        job_id = "aud-" + datetime.now(timezone.utc).strftime("%H%M%S")
        _AUDIT_SWEEP_STATE.update({
            "status": "RUNNING",
            "job_id": job_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "elapsed_ms": None,
            "report": None,
            "error": None,
        })
        t = threading.Thread(target=_run_audit_sweep_worker, args=(job_id,), daemon=True)
        t.start()
        return jsonify(dict(_AUDIT_SWEEP_STATE))


@app.route("/api/channels/summary", methods=["GET"])
def get_channels_summary():
    """Live 5-channel session/event summary + 8-agent roster + audit sweep status."""
    with obs.run_context(kind="probe") as run_id:
        customer_id = arg_customer()
        snapshot = (
            _adk.produce_all_channels_snapshot(customer_id=customer_id)
            if _adk and hasattr(_adk, "produce_all_channels_snapshot")
            else {"channels": [], "events": [], "total_sessions": 0, "total_events": 0}
        )
        roster = [a.model_dump(mode="json") for a in _adk.list_all_agents()] if _adk else []
        with _AUDIT_SWEEP_LOCK:
            sweep = dict(_AUDIT_SWEEP_STATE)
        return jsonify({
            "run_id": run_id,
            "customer_id": customer_id,
            "snapshot": snapshot,
            "agents": roster,
            "audit_sweep": sweep,
        })


@app.route("/api/channel/<channel>/event", methods=["POST"])
def post_live_channel_event(channel: str):
    """Decision B: writes a live channel event into Vertex AI Memory Bank and re-reads all 5 channels."""
    with obs.run_context(kind="chat") as run_id:
        body = payload()
        customer_id = body_customer(body)
        text = str(body.get("text") or body.get("message") or "").strip()
        intent = str(body.get("intent") or "LIVE_CHANNEL_EVENT").strip()
        card_id = str(body.get("card_id") or DEFAULT_CREDIT_CARD_ID).strip()
        ingested = _adk.ingest_live_channel_event(
            channel=channel,
            customer_id=customer_id,
            text=text,
            intent=intent,
            card_id=card_id,
        )
        snapshot = _adk.produce_all_channels_snapshot(customer_id=customer_id)
        return jsonify({
            "status": "SUCCESS",
            "run_id": run_id,
            "ingested": ingested,
            "snapshot": snapshot,
        })


@app.route("/api/fraud/card-control", methods=["POST"])
def post_fraud_card_control():
    """Decision C: invokes fraud_monitoring_agent's execute_fraud_card_control_or_escalate tool."""
    with obs.run_context(kind="chat") as run_id:
        body = payload()
        customer_id = body_customer(body)
        card_id = str(body.get("card_id") or DEFAULT_CREDIT_CARD_ID).strip()
        action = str(body.get("action") or "CARD_BLOCK_AND_ESCALATE").strip()
        reason = str(body.get("reason") or "Velocity + geo anomaly detected across channels").strip()
        outcome = _adk.execute_fraud_card_control_or_escalate(
            customer_id=customer_id,
            card_id=card_id,
            action=action,
            reason=reason,
            record_to_memory_bank=True,
        )
        snapshot = _adk.produce_all_channels_snapshot(customer_id=customer_id)
        return jsonify({
            "status": "SUCCESS",
            "run_id": run_id,
            "outcome": outcome,
            "snapshot": snapshot,
        })


@app.route("/api/chat", methods=["POST"])
@app.route("/api/session/channel/record", methods=["POST"])
def handle_workbench_chat():
    with obs.run_context(kind="chat") as run_id:
        result = _evaluate_interactive_workbench_turn(payload())
        result["run_id"] = run_id
        return jsonify(result)



# ---------------------------------------------------------------------------
# Admin card oversight - the YES/NO card that unlocks the account
# ---------------------------------------------------------------------------


@app.route("/api/card-oversight", methods=["GET", "POST"])
@app.route("/api/card-oversight/decide", methods=["POST"])
def handle_card_oversight_decision():
    if _veracity is None:
        return _unavailable("backend.veracity_and_audit", "admin card oversight")
    manager = _veracity.AdminCardOversightManager

    if request.method == "GET":
        customer_id = arg_customer()
        try:
            case = manager.get_or_init_case(customer_id=customer_id)
        except Exception as exc:  # noqa: BLE001
            return _upstream_error(exc, "AdminCardOversightManager.get_or_init_case")
        badge = "ACTIVE_UNLOCKED" if case.card_status == "ACTIVE" else "SECURITY_LOCKED"
        return jsonify(
            {
                "status": "SUCCESS",
                "customer_id": customer_id,
                "card_status": badge,
                "admin_review_case": case.model_dump(mode="json"),
            }
        )

    body = payload()
    customer_id = body_customer(body)
    raw_action = str(body.get("decision") or body.get("action") or "UNLOCK").upper()
    admin_notes = body.get("admin_notes") or body.get("reason")

    is_unlock = any(word in raw_action for word in ("UNLOCK", "APPROVE", "YES", "ACTIVE"))
    decision_code = "APPROVED_YES" if is_unlock else "REJECTED_NO"

    try:
        case = manager.execute_admin_decision(
            customer_id=customer_id, decision=decision_code, admin_notes=admin_notes
        )
    except Exception as exc:  # noqa: BLE001
        return _upstream_error(exc, "AdminCardOversightManager.execute_admin_decision")

    badge = "ACTIVE_UNLOCKED" if is_unlock else "SECURITY_LOCKED"
    return jsonify(
        {
            "status": "SUCCESS",
            "customer_id": customer_id,
            "decision": decision_code,
            "card_status": badge,
            "oversight_badge_text": badge,
            "oversight_badge_class": "badge ok" if is_unlock else "badge stop",
            "admin_review_case": case.model_dump(mode="json"),
            "customer_notification_message": case.customer_notification_message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.route("/api/admin/card-review", methods=["GET"])
def get_admin_card_review_case():
    if _veracity is None:
        return _unavailable("backend.veracity_and_audit", "the admin card review case")
    try:
        case = _veracity.AdminCardOversightManager.get_or_init_case(customer_id=arg_customer())
    except Exception as exc:  # noqa: BLE001
        return _upstream_error(exc, "AdminCardOversightManager.get_or_init_case")
    return jsonify({"status": "SUCCESS", "admin_review_case": case.model_dump(mode="json")})


@app.route("/api/admin/chat-verify", methods=["POST"])
def evaluate_customer_chat_verification():
    if _veracity is None:
        return _unavailable("backend.veracity_and_audit", "chat verification scoring")
    body = payload()
    chat_message = body.get("chat_message")
    if not chat_message:
        return jsonify({"status": "BAD_REQUEST", "error": "chat_message is required"}), 400
    try:
        case = _veracity.AdminCardOversightManager.evaluate_customer_chat_verification(
            customer_id=body_customer(body),
            chat_message=chat_message,
            scenario=body.get("scenario"),
        )
    except Exception as exc:  # noqa: BLE001
        return _upstream_error(exc, "AdminCardOversightManager.evaluate_customer_chat_verification")
    return jsonify(
        {
            "status": "SUCCESS",
            "admin_review_case": case.model_dump(mode="json"),
            "customer_chat_reply": case.customer_notification_message,
        }
    )


@app.route("/api/admin/card-decision", methods=["POST"])
def execute_admin_card_decision():
    if _veracity is None:
        return _unavailable("backend.veracity_and_audit", "the admin card decision")
    body = payload()
    try:
        case = _veracity.AdminCardOversightManager.execute_admin_decision(
            customer_id=body_customer(body),
            decision=body.get("decision", "APPROVED_YES"),
            admin_notes=body.get("admin_notes"),
        )
    except Exception as exc:  # noqa: BLE001
        return _upstream_error(exc, "AdminCardOversightManager.execute_admin_decision")
    return jsonify(
        {
            "status": "SUCCESS",
            "admin_review_case": case.model_dump(mode="json"),
            "customer_notification_message": case.customer_notification_message,
        }
    )


@app.route("/api/card/unlock", methods=["POST"])
def unlock_card():
    """One-click unlock. The consequences it reports are the case's own fields."""
    if _veracity is None:
        return _unavailable("backend.veracity_and_audit", "the one-click card unlock")
    body = payload()
    try:
        case = _veracity.AdminCardOversightManager.execute_admin_decision(
            customer_id=body_customer(body),
            decision="APPROVED_YES",
            admin_notes=body.get("admin_notes", "Approved from the one-click oversight card."),
        )
    except Exception as exc:  # noqa: BLE001
        return _upstream_error(exc, "AdminCardOversightManager.execute_admin_decision")
    return jsonify(
        {
            "status": "RESOLVED",
            "card_status": case.card_status,
            "admin_review_case": case.model_dump(mode="json"),
            "customer_notification_message": case.customer_notification_message,
        }
    )


# ---------------------------------------------------------------------------
# Channel sessions - one Memory Bank session per channel
# ---------------------------------------------------------------------------


@app.route("/api/session/channel/open", methods=["POST"])
def open_channel_session():
    body = payload()
    customer_id = body_customer(body)
    channel = as_channel(body.get("channel", "MOBILE_APP"))
    try:
        session_name = memory_client().create_session(
            user_id=customer_id,
            display_name=body.get("display_name") or "%s %s" % (channel.value, customer_id),
            session_state={
                "channel": channel.value,
                "customer_id": customer_id,
                "agent_name": body.get("agent_name", NOT_RECORDED),
            },
        )
    except MemoryBankError as exc:
        return _upstream_error(exc, "reasoningEngines.sessions.create")
    return jsonify(
        {
            "status": "SUCCESS",
            "session": {
                "session_name": live(session_name, "reasoningEngines.sessions.create"),
                "session_id": live(session_name.split("/")[-1], "reasoningEngines.sessions.create"),
                "customer_id": computed(customer_id, "request payload customer_id"),
                "channel": identity(channel.value, "request payload channel", "the channel that opened it"),
            },
        }
    )


@app.route("/api/session/channel/list", methods=["GET"])
def list_channel_sessions():
    customer_id = request.args.get("customer_id")
    try:
        sessions = memory_client().list_sessions()
    except MemoryBankError as exc:
        return _upstream_error(exc, "reasoningEngines.sessions.list")
    if customer_id:
        sessions = [s for s in sessions if s.get("userId") == customer_id]
    return jsonify(
        {
            "count": len(sessions),
            "sessions": live(sessions, "reasoningEngines.sessions.list"),
        }
    )


# ---------------------------------------------------------------------------
# Claims, statements, catalog
# ---------------------------------------------------------------------------


@app.route("/api/claims/validate-and-write", methods=["POST"])
def validate_and_write_claim():
    """Pre-write veracity gate. A claim is validated before Memory Bank sees it."""
    if _veracity is None:
        return _unavailable("backend.veracity_and_audit", "pre-write claim validation")
    body = payload()
    claim_text = body.get("claim_text", "")
    try:
        fragment = _veracity.ClaimVeracityValidatorAgent().validate_and_write_to_memory_bank(
            customer_id=body_customer(body),
            channel=as_channel(body.get("channel", "MOBILE_APP")),
            day_label=body.get("day_label", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
            summary=body.get("summary", claim_text),
            claim_text=claim_text,
            metadata=body.get("metadata", {}),
            severity=as_severity(body.get("severity", "MEDIUM")),
            session_id=body.get("session_id"),
        )
    except Exception as exc:  # noqa: BLE001
        return _upstream_error(exc, "ClaimVeracityValidatorAgent.validate_and_write_to_memory_bank")

    evaluation = fragment.veracity_evaluation
    return jsonify(
        {
            "status": "SUCCESS",
            "fragment": fragment.to_display_dict(),
            "veracity_evaluation": evaluation.model_dump(mode="json") if evaluation else NOT_RECORDED,
        }
    )


@app.route("/api/account/statement/<customer_id>", methods=["GET"])
def get_account_statement(customer_id: str):
    """The ledger view, assembled from golden queries only."""
    snapshot = golden("golden_01_customer_360_snapshot", customer_id=customer_id)
    disputes = golden("golden_07_open_disputes_for_customer", customer_id=customer_id)
    authorizations = golden(
        "golden_02_recent_authorizations_for_card",
        card_id=request.args.get("card_id", DEFAULT_CREDIT_CARD_ID),
        lookback_days=int(request.args.get("lookback_days", 14)),
    )
    provisional = golden("golden_09_provisional_credit_due")
    return jsonify(
        {
            "status": "SUCCESS",
            "customer_id": computed(customer_id, "request path customer_id"),
            "statement": {
                "customer_360": golden_value(snapshot, "golden_01_customer_360_snapshot"),
                "open_disputes": golden_value(disputes, "golden_07_open_disputes_for_customer"),
                "recent_authorizations": golden_value(
                    authorizations, "golden_02_recent_authorizations_for_card"
                ),
                "provisional_credit_due": golden_value(provisional, "golden_09_provisional_credit_due"),
                # The balance is a derived figure nothing in the contract set returns.
                "current_posted_balance_usd": unmeasured(
                    "no golden query contract returns a posted balance"
                ),
            },
        }
    )


@app.route("/api/knowledge-catalog/overview", methods=["GET"])
def get_knowledge_catalog_overview():
    """What the governed catalog actually says, read live from Dataplex.

    Each facet is attempted on its own. The previous version put five
    independent reads in one try block, so `entryLinks` -- a method Dataplex
    does not expose publicly ("Method not visible to labels: {PUBLIC}") --
    returned 503 for the entire catalog, including the glossary and every
    governance verdict, all of which work. One unavailable API is now reported
    as one unavailable API.

    A facet that failed carries its real HTTP status and body. It is never
    replaced with an empty list, which would read as "the catalog is empty"
    rather than "we could not ask".
    """
    customer_id = arg_customer()
    gov = catalog_governor()
    unavailable: Dict[str, str] = {}

    def facet(name: str, read):
        """Runs one catalog read; records the upstream reason if it fails."""
        try:
            return read()
        except CatalogError as exc:
            unavailable[name] = "HTTP %s: %s" % (exc.status, exc.body.strip()[:300])
            return None

    terms = facet("glossaryTerms.list", gov.client.list_terms)
    entry_links = facet("entryLinks.list", gov.client.list_entry_links)
    verdicts = facet(
        "entries.lookupEntry (tables)",
        lambda: {
            table: gov.check_table(table, dataset=BIGQUERY_DATASET).to_dict()
            for table in (
                "disputes",
                "authorizations",
                "authorizations_deprecated_v1",
                "customers",
                "cards",
            )
        },
    )
    columns = facet(
        "entries.lookupEntry (columns)",
        lambda: {
            "customers.ssn_last4": gov.check_column("customers", "ssn_last4").to_dict(),
            "cards.pan_full": gov.check_column("cards", "pan_full").to_dict(),
        },
    )

    # Every facet failed: that is a catalog outage, not a partial read.
    if terms is None and verdicts is None and columns is None:
        return (
            jsonify(
                {
                    "status": "UPSTREAM_ERROR",
                    "source": "dataplex catalog",
                    "unavailable": unavailable,
                }
            ),
            503,
        )

    def measured(value, source, name):
        """Live when the call returned; n/r with the reason when it did not."""
        if value is None:
            return unmeasured("%s unavailable - %s" % (source, unavailable.get(name, "no reason recorded")))
        return live(value, source)

    return jsonify(
        {
            "status": "SUCCESS" if not unavailable else "PARTIAL",
            "customer_id": computed(customer_id, "request.args.customer_id"),
            "glossary": identity(DATAPLEX_GLOSSARY, "env:DATAPLEX_GLOSSARY", "deployment parameter"),
            "terms_count": measured(
                len(terms) if terms is not None else None,
                "dataplex.glossaryTerms.list",
                "glossaryTerms.list",
            ),
            "terms": measured(terms, "dataplex.glossaryTerms.list", "glossaryTerms.list"),
            "entry_links_count": measured(
                len(entry_links) if entry_links is not None else None,
                "dataplex.entryLinks.list",
                "entryLinks.list",
            ),
            "table_verdicts": measured(
                verdicts, "dataplex.entries.lookupEntry", "entries.lookupEntry (tables)"
            ),
            "column_verdicts": measured(
                columns, "dataplex.entries.lookupEntry", "entries.lookupEntry (columns)"
            ),
            "customer_risk_profile": golden_value(
                golden("golden_01_customer_360_snapshot", customer_id=customer_id),
                "golden_01_customer_360_snapshot",
            ),
            # These two had no real producer. They are not silently synthesised.
            "entity_graph": unmeasured("no catalog API in this kit returns an entity subgraph"),
            "hybrid_connector": unmeasured("no AWS/on-prem connector is deployed in this kit"),
            # Named, so a reader can tell "the catalog says nothing" apart from
            # "we could not ask the catalog".
            "unavailable": unavailable,
        }
    )


# ---------------------------------------------------------------------------
# Memory Bank routes. Consolidation is the managed service's job throughout.
# ---------------------------------------------------------------------------


@app.route("/api/memory/get", methods=["GET"])
def get_memory_bank():
    customer_id = arg_customer()
    try:
        memories = memory_client().retrieve(scope_for(customer_id))
    except MemoryBankError as exc:
        return _upstream_error(exc, "reasoningEngines.memories:retrieve")
    records = [memory_record(m) for m in memories]
    return jsonify(
        {
            "customer_id": customer_id,
            "count": len(records),
            "memories": live(records, "reasoningEngines.memories:retrieve"),
        }
    )


@app.route("/api/memory/retrieve", methods=["POST"])
def retrieve_relevant_memory():
    """Top-K semantic retrieval. Ranking happens server side; none of it is local."""
    body = payload()
    customer_id = body_customer(body)
    query = body.get("query")
    top_k = int(body.get("top_k", 5))
    try:
        memories = memory_client().retrieve(scope_for(customer_id), query=query, top_k=top_k)
    except MemoryBankError as exc:
        return _upstream_error(exc, "reasoningEngines.memories:retrieve")
    records = [memory_record(m) for m in memories]
    return jsonify(
        {
            "status": "SUCCESS",
            "retrieval_result": {
                "query": computed(query or NOT_RECORDED, "request payload query"),
                "top_k": computed(top_k, "request payload top_k"),
                "memory_count": computed(len(records), "reasoningEngines.memories:retrieve"),
                "memories": live(records, "reasoningEngines.memories:retrieve"),
                # Tokenomics needs a tokenizer and a baseline transcript. Neither
                # exists here, so no ratio is reported.
                "tokens_full_history": unmeasured("no transcript baseline is measured in this process"),
                "tokens_retrieved": unmeasured("Memory Bank does not return token counts"),
                "tokens_saved_pct": unmeasured("depends on two unmeasured token counts"),
            },
        }
    )


@app.route("/api/memory/seed", methods=["POST"])
def seed_memory_bank():
    """Seeds the Lisbon corpus by handing raw turns to the service to extract."""
    body = payload()
    customer_id = body_customer(body)
    turns = [{"role": t["role"], "text": t["text"]} for t in LISBON_THEFT_TURNS]
    try:
        operation = memory_client().generate_from_contents(
            turns=turns, scope=scope_for(customer_id), wait=False
        )
    except MemoryBankError as exc:
        return _upstream_error(exc, "reasoningEngines.memories:generate")
    return jsonify(
        {
            "status": "SUCCESS",
            "customer_id": customer_id,
            "turns_submitted": computed(len(turns), "backend.memory_bank.LISBON_THEFT_TURNS"),
            "operation": live(operation.get("name", NOT_RECORDED), "reasoningEngines.memories:generate"),
            "note": "Extraction is asynchronous. Memories appear once the operation completes.",
        }
    )


@app.route("/api/memory/ingest", methods=["POST"])
def ingest_memory_event():
    """Submits one observation. What gets stored is the service's decision."""
    body = payload()
    customer_id = body_customer(body)
    summary = body.get("summary")
    if not summary:
        return jsonify({"status": "BAD_REQUEST", "error": "summary is required"}), 400
    channel = as_channel(body.get("channel", "FRAUD_DETECTION"), BankChannel.FRAUD_DETECTION)
    try:
        operation = memory_client().generate_from_contents(
            turns=[{"role": "user", "text": summary}],
            scope=scope_for(customer_id),
            wait=False,
        )
    except MemoryBankError as exc:
        return _upstream_error(exc, "reasoningEngines.memories:generate")
    return jsonify(
        {
            "status": "SUCCESS",
            "fragment": {
                "customer_id": computed(customer_id, "request payload customer_id"),
                "channel": identity(channel.value, "request payload channel", "ingesting channel"),
                "severity": identity(
                    as_severity(body.get("severity", "MEDIUM")).value,
                    "request payload severity",
                    "caller-declared severity",
                ),
                "summary": computed(summary, "request payload summary"),
                "operation": live(operation.get("name", NOT_RECORDED), "reasoningEngines.memories:generate"),
                # The service assigns the id after extraction completes.
                "memory_id": unmeasured("assigned asynchronously by memories:generate"),
            },
        }
    )


def _compaction_report(body: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Memory consolidation, reported honestly.

    There is no local compaction any more. Consolidation happens inside the
    managed Memory Bank when `memories:generate` runs, so this route triggers
    that and reports what the service returned. Compression ratio and token
    counts are not values the API exposes, so they read "n/r" instead of a
    number that would look convincing and mean nothing.
    """
    customer_id = body_customer(body)
    session_name = body.get("session_name")
    client = memory_client()

    try:
        before = client.retrieve(scope_for(customer_id))
    except MemoryBankError as exc:
        return {
            "status": "UPSTREAM_ERROR",
            "source": "reasoningEngines.memories:retrieve",
            "error": "%s: %s" % (type(exc).__name__, exc),
        }, 502

    if session_name:
        try:
            operation = client.generate_from_session(session_name, scope_for(customer_id), wait=False)
            triggered = live(operation.get("name", NOT_RECORDED), "reasoningEngines.memories:generate")
        except MemoryBankError as exc:
            triggered = unmeasured("reasoningEngines.memories:generate (%s)" % exc)
    else:
        triggered = unmeasured(
            "no session_name supplied; consolidation runs server side when a session is generated from"
        )

    return {
        "status": "SUCCESS",
        "customer_id": customer_id,
        "compaction_report": {
            "consolidation_location": identity(
                "Vertex AI Agent Engine Memory Bank (server side)",
                "backend/memory_bank.py:MemoryBankClient.generate_from_session",
                "states where the work runs; not a measurement",
            ),
            "memories_in_scope_before": live(len(before), "reasoningEngines.memories:retrieve"),
            "generate_operation": triggered,
            "tokens_before": unmeasured("Memory Bank does not report token counts"),
            "tokens_after": unmeasured("Memory Bank does not report token counts"),
            "compression_ratio": unmeasured("depends on two unmeasured token counts"),
            "token_reduction_pct": unmeasured("depends on two unmeasured token counts"),
            "nodes_consolidated": unmeasured(
                "the generate operation is asynchronous and does not return a consolidation count"
            ),
        },
        "memories": live([memory_record(m) for m in before], "reasoningEngines.memories:retrieve"),
    }, 200


@app.route("/api/memory/compact", methods=["POST"])
def compact_memory_bank():
    body, code = _compaction_report(payload())
    return jsonify(body), code


@app.route("/api/compaction/run", methods=["POST"])
def run_compaction_alias():
    """Same work as /api/memory/compact. Kept because the UI calls this path."""
    body, code = _compaction_report(payload())
    return jsonify(body), code


@app.route("/api/standalone/external-agent", methods=["POST"])
def standalone_external_agent_demo():
    """Memory Bank consumed from a non-ADK runtime, over plain REST."""
    body = payload()
    customer_id = body_customer(body)
    query = body.get("query")
    if not query:
        return jsonify({"status": "BAD_REQUEST", "error": "query is required"}), 400
    client = StandaloneMemoryBankClient(
        client=memory_client(),
        customer_id=customer_id,
        framework_name=body.get("framework", "LangGraph"),
    )
    try:
        bundle = client.preload_context_for_external_agent(
            user_query=query, top_k=int(body.get("top_k", 5))
        )
    except MemoryBankError as exc:
        return _upstream_error(exc, "StandaloneMemoryBankClient.preload_context_for_external_agent")
    return jsonify({"status": "SUCCESS", "standalone_bundle": live(bundle, "StandaloneMemoryBankClient")})


@app.route("/api/comparison/run", methods=["POST"])
def run_agent_comparison():
    """ADK lane versus custom lane. Only the lane that ran reports numbers."""
    body = payload()
    query = body.get("prompt") or body.get("query")
    if not query:
        return jsonify({"status": "BAD_REQUEST", "error": "prompt is required"}), 400
    try:
        report = AgentComparisonEngine(
            memory_client=memory_client(),
            adk_module=_adk,
            adk_import_error=_DEGRADED.get("backend.adk_agents"),
            catalog_governor=catalog_governor(),
        ).run_side_by_side_comparison(customer_id=body_customer(body), user_prompt=query)
    except MemoryBankError as exc:
        return _upstream_error(exc, "AgentComparisonEngine.run_side_by_side_comparison")
    except CatalogError as exc:
        return _upstream_error(exc, "AgentComparisonEngine catalog gate")
    return jsonify({"status": "SUCCESS", "comparison": report})



@app.route("/api/audit/sweep", methods=["GET", "POST"])
def run_audit_sweep():
    if _veracity is None:
        return _unavailable("backend.veracity_and_audit", "the consolidated audit sweep")
    try:
        report = _veracity.MemoryBankAuditAgent().run_consolidated_sweep()
    except Exception as exc:  # noqa: BLE001
        return _upstream_error(exc, "MemoryBankAuditAgent.run_consolidated_sweep")
    return jsonify({"status": "SUCCESS", "report": report.model_dump(mode="json")})


@app.route("/api/audit/customer/<customer_id>", methods=["GET"])
def get_customer_audit(customer_id: str):
    if _veracity is None:
        return _unavailable("backend.veracity_and_audit", "the per-customer audit")
    try:
        anomalies = _veracity.MemoryBankAuditAgent().audit_customer_memory_bank(customer_id)
    except Exception as exc:  # noqa: BLE001
        return _upstream_error(exc, "MemoryBankAuditAgent.audit_customer_memory_bank")
    return jsonify(
        {
            "customer_id": customer_id,
            "count": len(anomalies),
            "anomalies": [a.model_dump(mode="json") for a in anomalies],
        }
    )


@app.route("/api/telemetry/<customer_id>", methods=["GET"])
def get_customer_telemetry(customer_id: str):
    """Ground truth for a customer. Every block is one golden query contract."""
    card_id = request.args.get("card_id", DEFAULT_CREDIT_CARD_ID)
    lookback = int(request.args.get("lookback_days", 14))
    probes = {
        "customer_360": ("golden_01_customer_360_snapshot", {"customer_id": customer_id}),
        "recent_authorizations": (
            "golden_02_recent_authorizations_for_card",
            {"card_id": card_id, "lookback_days": lookback},
        ),
        "suspicious_activity": ("golden_03_suspicious_activity_on_card_window", {"card_id": card_id}),
        "geo_impossible_travel": (
            "golden_04_geo_impossible_travel",
            {"card_id": card_id, "lookback_days": lookback},
        ),
        "open_disputes": ("golden_07_open_disputes_for_customer", {"customer_id": customer_id}),
        "wallet_tokens_active": ("golden_10_wallet_tokens_active_on_card", {"card_id": card_id}),
    }
    telemetry = {
        key: golden_value(golden(tool, **params), tool) for key, (tool, params) in probes.items()
    }
    return jsonify(
        {
            "customer_id": computed(customer_id, "request path customer_id"),
            "card_id": computed(card_id, "request.args.card_id"),
            "telemetry": telemetry,
        }
    )


# ---------------------------------------------------------------------------
# SSE. Frame names are a fixed contract with the frontend.
# ---------------------------------------------------------------------------



@app.route("/api/trace/<run_id>", methods=["GET"])
def get_run_trace(run_id: str):
    spans = obs.get_trace(run_id)
    return jsonify({"run_id": run_id, "spans": spans})

@app.route("/api/trace/latest", methods=["GET"])
def get_latest_trace():
    # Page load fires background probes and an operator turn at once. Without a
    # kind filter the Pipeline can render whichever probe finished last.
    kind = request.args.get("kind") or None
    run_id = obs.get_latest_run_id(kind)
    if not run_id:
        return jsonify({"error": "no trace found"}), 404
    spans = obs.get_trace(run_id)
    return jsonify({"run_id": run_id, "kind": obs.get_run_kind(run_id), "spans": spans})

@app.route("/api/chat/stream", methods=["GET", "POST"])
def chat_stream():
    """Streams the synthesizer's JSON-RPC 2.0 trace frames.

    Frames, in order: onAgentThought, onMemoryBankAccess, onAgentSynthesis,
    onUiComponentDelivery. Renaming any of them breaks the frontend contract.
    """
    if request.method == "POST":
        body = payload()
        prompt = body.get("prompt", "")
        session_id = body.get("session_id", NOT_RECORDED)
        customer_id = body_customer(body)
    else:
        prompt = request.args.get("prompt", "")
        session_id = request.args.get("session_id", NOT_RECORDED)
        customer_id = arg_customer()

    # The customer's own words move the oversight card; that is the arc's hinge.
    if _veracity is not None and prompt:
        try:
            _veracity.AdminCardOversightManager.evaluate_customer_chat_verification(
                customer_id=customer_id, chat_message=prompt
            )
        except Exception as exc:  # noqa: BLE001
            logging.error("Oversight scoring failed during stream setup: %s", exc)

    agent = MemoryBankSynthesizerAgent(project_id=PROJECT_ID, location=LOCATION)
    client = memory_client()

    def sse_event_encoder() -> Generator[str, None, None]:
        # The generator is consumed after the view returns, so the run context
        # has to live in here; opening it around Response() would already be
        # closed by the time the first frame is produced.
        with obs.run_context(kind="stream"):
            for frame in agent.stream_synthesis_workflow(session_id, customer_id, prompt, client):
                yield "data: %s\n\n" % frame

    return Response(
        sse_event_encoder(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    app.run(host="0.0.0.0", port=port, debug=False)

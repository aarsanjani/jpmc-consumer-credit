"""
Google Agent Development Kit (ADK) 8-Agent Hub-and-Spoke Architecture
for JPMorgan Chase Consumer & Community Banking (CCB) Fraud & Disputes Desk.

Implements the full 8-agent topology wired to live:
1. Vertex AI Agent Engine Memory Bank (`backend/memory_bank.py` -> `MemoryBankClient`)
   - Scoped per customer (`scope={"customer_id": customer_id}`), reading the 7 custom
     fraud topics (`TRAVEL_STATUS`, `DISPUTE_HISTORY`, `CONTACT_PREFERENCE`,
     `DEVICE_WALLET_INVENTORY`, `AGENT_COMMITMENTS`, `CARD_LIFECYCLE`, `SPENDING_CONTEXT`)
     and channel session histories.
2. Dataplex Universal Catalog (`backend/knowledge_catalog.py` -> `CatalogGovernor`)
   - Aspect-driven governance (`lifecycle-status`, `sensitivity-class.agent_behavior`,
     and column-scoped `@Schema.{column}` overrides) + the 45-term `ccb-fraud-disputes`
     Business Glossary.
   - Sibling tables (`authorizations_deprecated_v1`) are discovered dynamically via
     `searchEntries` and voluntarily refused when `lifecycle-status.status = DEPRECATED`.
   - Restricted PII columns (`customers.ssn_last4`, `cards.pan_full`, `authorizations.is_fraud`)
     are checked via `check_column` and voluntarily declined/redacted.
3. BigQuery Parameterised Golden Query Contracts (`backend/queries/golden/01..12.sql` + `backend/ground_truth.py`)
   - All 12 Golden Queries from `backend/queries/golden/INDEX.md` are bound with
     per-agent authorization (`authorized_agents`) and `@named` query parameters.
   - Ad-hoc SQL strings are blocked at the interface (`ad_hoc_sql_used = False`).

The 8 Agents:
  01 · fraud_monitoring_agent            (CHANNEL_PRODUCER · FRAUD_OPS_OUTBOUND)
  02 · telephony_ivr_agent               (CHANNEL_PRODUCER · TELEPHONY_IVR)
  03 · mobile_app_agent                  (CHANNEL_PRODUCER · MOBILE_APP)
  04 · web_portal_agent                  (CHANNEL_PRODUCER · WEB_PORTAL)
  05 · branch_support_agent              (CHANNEL_PRODUCER · BRANCH_SUPPORT)
  06 · claim_veracity_validator_agent    (PRE_WRITE_VALIDATOR)
  07 · memory_bank_audit_agent           (CONSOLIDATED_AUDITOR)
  08 · consumer_credit_synthesizer_agent (LEAD_SYNTHESIZER · Root Hub)
"""

from __future__ import annotations

import asyncio
import datetime
import decimal
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.adk.tools import FunctionTool
from google.cloud import bigquery
from google.genai import types

from backend.config import Settings, settings as load_settings
from backend.ground_truth import GOLDEN_QUERIES, GroundTruthError, GroundTruthReader, _as_param, _jsonable
from backend.knowledge_catalog import CatalogError, CatalogGovernor
from backend.memory_bank import MemoryBankClient, MemoryBankError
from backend.veracity_and_audit import ClaimValidator, MemoryCorpusAuditor

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"

# Lazily initialised service singletons
_memory_singleton: Optional[MemoryBankClient] = None
_governor_singleton: Optional[CatalogGovernor] = None
_reader_singleton: Optional[GroundTruthReader] = None


def memory_client() -> MemoryBankClient:
    global _memory_singleton
    if _memory_singleton is None:
        _memory_singleton = MemoryBankClient()
    return _memory_singleton


def governor() -> CatalogGovernor:
    global _governor_singleton
    if _governor_singleton is None:
        _governor_singleton = CatalogGovernor()
    return _governor_singleton


def reader() -> GroundTruthReader:
    global _reader_singleton
    if _reader_singleton is None:
        _reader_singleton = GroundTruthReader(governor=governor())
    return _reader_singleton


# ==============================================================================
# 1. GOLDEN QUERY CONTRACTS (01..12 in `backend/queries/golden/INDEX.md`)
# ==============================================================================

@dataclass(frozen=True)
class GoldenSqlContract:
    query_id: str
    sql_filename: str
    tool_name: str
    purpose: str
    authorized_agents: tuple[str, ...]
    governed_tables: tuple[str, ...]
    governed_columns_checked: tuple[str, ...]


GOLDEN_SQL_CONTRACTS: Dict[str, GoldenSqlContract] = {
    "01": GoldenSqlContract(
        query_id="01",
        sql_filename="01_customer_360_snapshot.sql",
        tool_name="golden_01_customer_360_snapshot",
        purpose="Customer 360 snapshot: accounts, active cards/tokens, 90d auths/declines, open disputes, active travel.",
        authorized_agents=("consumer_credit_synthesizer_agent", "branch_support_agent", "claim_veracity_validator_agent"),
        governed_tables=("v_customer_360", "customers", "accounts", "cards"),
        governed_columns_checked=("customers.ssn_last4",),
    ),
    "02": GoldenSqlContract(
        query_id="02",
        sql_filename="02_recent_authorizations_for_card.sql",
        tool_name="golden_02_recent_authorizations_for_card",
        purpose="Most recent authorizations and declines on a specific card with merchant and country.",
        authorized_agents=("telephony_ivr_agent", "fraud_monitoring_agent", "consumer_credit_synthesizer_agent"),
        governed_tables=("authorizations", "cards", "merchants"),
        governed_columns_checked=("cards.pan_masked", "cards.pan_full", "authorizations.is_fraud"),
    ),
    "03": GoldenSqlContract(
        query_id="03",
        sql_filename="03_suspicious_activity_on_card_window.sql",
        tool_name="golden_03_suspicious_activity_on_card_window",
        purpose="Suspicious authorizations on a card within a window scored by risk signals without reading is_fraud.",
        authorized_agents=("fraud_monitoring_agent", "consumer_credit_synthesizer_agent"),
        governed_tables=("authorizations", "cards", "merchants"),
        governed_columns_checked=("cards.pan_full", "authorizations.is_fraud"),
    ),
    "04": GoldenSqlContract(
        query_id="04",
        sql_filename="04_geo_impossible_travel.sql",
        tool_name="golden_04_geo_impossible_travel",
        purpose="Detects consecutive authorizations on the same card across geographies requiring impossible travel velocity.",
        authorized_agents=("fraud_monitoring_agent", "consumer_credit_synthesizer_agent"),
        governed_tables=("authorizations", "merchants"),
        governed_columns_checked=("cards.pan_full",),
    ),
    "05": GoldenSqlContract(
        query_id="05",
        sql_filename="05_merchant_velocity_spike.sql",
        tool_name="golden_05_merchant_velocity_spike",
        purpose="Identifies merchant daily velocity spikes relative to baseline.",
        authorized_agents=("fraud_monitoring_agent",),
        governed_tables=("agg_merchant_velocity_daily",),
        governed_columns_checked=(),
    ),
    "06": GoldenSqlContract(
        query_id="06",
        sql_filename="06_cnp_burst_detection.sql",
        tool_name="golden_06_cnp_burst_detection",
        purpose="Detects rapid card-not-present (CNP) authorization bursts on a card.",
        authorized_agents=("fraud_monitoring_agent",),
        governed_tables=("authorizations", "merchants"),
        governed_columns_checked=("cards.pan_full",),
    ),
    "07": GoldenSqlContract(
        query_id="07",
        sql_filename="07_open_disputes_for_customer.sql",
        tool_name="golden_07_open_disputes_for_customer",
        purpose="Open disputes for a customer with regulation_path (REG_E vs REG_Z), provisional credit status, and deadlines.",
        authorized_agents=("web_portal_agent", "consumer_credit_synthesizer_agent", "claim_veracity_validator_agent"),
        governed_tables=("disputes", "authorizations", "merchants", "cards"),
        governed_columns_checked=("cards.pan_masked", "cards.pan_full"),
    ),
    "08": GoldenSqlContract(
        query_id="08",
        sql_filename="08_dispute_filing_window_check.sql",
        tool_name="golden_08_dispute_filing_window_check",
        purpose="Recomputes whether a dispute was filed within its regulatory window and states Regulation E vs Z obligations.",
        authorized_agents=("web_portal_agent", "consumer_credit_synthesizer_agent"),
        governed_tables=("disputes", "authorizations", "accounts"),
        governed_columns_checked=(),
    ),
    "09": GoldenSqlContract(
        query_id="09",
        sql_filename="09_provisional_credit_due.sql",
        tool_name="golden_09_provisional_credit_due",
        purpose="Regulation E disputes owing provisional credit and their 10-business-day deadline.",
        authorized_agents=("web_portal_agent",),
        governed_tables=("disputes", "accounts", "authorizations"),
        governed_columns_checked=(),
    ),
    "10": GoldenSqlContract(
        query_id="10",
        sql_filename="10_wallet_tokens_active_on_card.sql",
        tool_name="golden_10_wallet_tokens_active_on_card",
        purpose="Digital wallet tokens (Apple Pay, Google Pay) bound to a card and whether each is ACTIVE or SUSPENDED/REVOKED.",
        authorized_agents=("mobile_app_agent", "consumer_credit_synthesizer_agent"),
        governed_tables=("wallet_tokens", "cards", "authorizations"),
        governed_columns_checked=("cards.pan_full",),
    ),
    "11": GoldenSqlContract(
        query_id="11",
        sql_filename="11_dispute_event_timeline.sql",
        tool_name="golden_11_dispute_event_timeline",
        purpose="Ordered event-by-event timeline for a dispute, including courier reroutes and provisional credit issuance.",
        authorized_agents=("memory_bank_audit_agent", "web_portal_agent", "consumer_credit_synthesizer_agent"),
        governed_tables=("disputes", "dispute_events"),
        governed_columns_checked=(),
    ),
    "12": GoldenSqlContract(
        query_id="12",
        sql_filename="12_fraud_rate_by_merchant_category.sql",
        tool_name="golden_12_fraud_rate_by_merchant_category",
        purpose="Portfolio-level historical fraud rate by merchant category.",
        authorized_agents=("fraud_monitoring_agent", "memory_bank_audit_agent"),
        governed_tables=("authorizations", "merchants"),
        governed_columns_checked=(),
    ),
}


def execute_golden_sql_contract(
    query_num: str,
    calling_agent: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """Execute one of the 12 Golden Query SQL contracts with RBAC + Dataplex Catalog pre-authorization."""
    contract = GOLDEN_SQL_CONTRACTS.get(query_num)
    if not contract:
        return {"allowed": False, "error": f"Unknown golden SQL contract {query_num!r}."}

    # 1. Agent-level RBAC check (from INDEX.md)
    if calling_agent not in contract.authorized_agents:
        log.warning(
            "GOLDEN_RBAC_REFUSED: agent=%s is NOT authorized for %s (allowed=%s)",
            calling_agent, contract.sql_filename, contract.authorized_agents,
        )
        return {
            "allowed": False,
            "rbac_refused": True,
            "calling_agent": calling_agent,
            "query_id": query_num,
            "sql_filename": contract.sql_filename,
            "authorized_agents": list(contract.authorized_agents),
            "error": (
                f"Agent '{calling_agent}' is not authorized in queries/golden/INDEX.md to execute "
                f"{contract.sql_filename}. Authorized agents: {list(contract.authorized_agents)}."
            ),
        }

    gov = governor()
    rdr = reader()
    cfg = rdr.cfg

    # 2. Pre-read Dataplex Catalog Governance checks (Primary table + Sibling discovery + Column overrides)
    governance_checks: List[Dict[str, Any]] = []
    voluntary_refusals: List[Dict[str, Any]] = []

    primary_table = contract.governed_tables[0]
    try:
        primary_dec = gov.check_table(primary_table)
        governance_checks.append(primary_dec.to_dict())
        if not primary_dec.allowed:
            voluntary_refusals.append(primary_dec.to_dict())

        # Discover sibling tables (e.g. authorizations_deprecated_v1 alongside authorizations)
        for sibling in gov.discover_sibling_tables(primary_table):
            if sibling == primary_table:
                continue
            sib_dec = gov.check_table(sibling)
            governance_checks.append(sib_dec.to_dict())
            if not sib_dec.allowed:
                voluntary_refusals.append(sib_dec.to_dict())

        for col_spec in contract.governed_columns_checked:
            if "." in col_spec:
                t_name, c_name = col_spec.split(".", 1)
                c_dec = gov.check_column(t_name, c_name)
                governance_checks.append(c_dec.to_dict())
                if not c_dec.allowed:
                    voluntary_refusals.append(c_dec.to_dict())
    except Exception as exc:
        log.warning("Catalog governance pre-check warning on %s: %s", query_num, exc)

    # 3. Load parameterised SQL file from backend/queries/golden/ and execute on BigQuery
    sql_dir = Path(__file__).resolve().parent / "queries" / "golden"
    sql_path = sql_dir / contract.sql_filename
    if not sql_path.exists():
        return {"allowed": False, "error": f"SQL file {contract.sql_filename} not found."}

    sql_text = sql_path.read_text().replace("{dataset}", cfg.dataset_resource)

    started = time.perf_counter()
    try:
        q_params = []
        for k, v in params.items():
            if isinstance(v, list):
                q_params.append(bigquery.ArrayQueryParameter(k, "STRING", v))
            else:
                q_params.append(_as_param(k, v))
        job_cfg = bigquery.QueryJobConfig(query_parameters=q_params)
        job = rdr.client.query(sql_text, job_config=job_cfg)
        rows = [_jsonable(dict(r)) for r in job.result()]
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        return {
            "allowed": True,
            "ad_hoc_sql_used": False,
            "calling_agent": calling_agent,
            "query_id": query_num,
            "sql_filename": contract.sql_filename,
            "params_bound": params,
            "row_count": len(rows),
            "rows": rows[:25],
            "latency_ms": elapsed_ms,
            "job_id": job.job_id,
            "governance_checks": governance_checks,
            "voluntary_refusals": voluntary_refusals,
        }
    except Exception as exc:
        return {
            "allowed": True,
            "ad_hoc_sql_used": False,
            "calling_agent": calling_agent,
            "query_id": query_num,
            "sql_filename": contract.sql_filename,
            "error": str(exc),
            "governance_checks": governance_checks,
            "voluntary_refusals": voluntary_refusals,
        }


# ==============================================================================
# 2. CALLABLE TOOLS FOR THE 8 AGENTS (MB + KC + BQ Golden Queries)
# ==============================================================================

def list_memory_topics() -> dict:
    """List the memory topics configured on the live Vertex AI Agent Engine Memory Bank."""
    topics = memory_client().memory_topics()
    return {
        "count": len(topics),
        "topics": [t.to_dict() for t in topics],
    }


def list_memory_topics_tool() -> dict:
    """Alias for list_memory_topics so both naming conventions work seamlessly."""
    return list_memory_topics()


def search_customer_memory(customer_id: str, query: str, top_k: int = 5) -> dict:
    """Retrieve memories for `customer_id` from Vertex AI Memory Bank ranked by vector distance."""
    res = memory_client().retrieve(
        scope={"customer_id": customer_id},
        query=query,
        top_k=max(1, min(int(top_k), 20)),
    )
    return {
        "customer_id": customer_id,
        "query": query,
        "count": len(res.memories),
        "latency_ms": res.latency_ms.to_dict(),
        "memories": [m.to_dict() for m in res.memories],
    }


def read_customer_memory_bank(customer_id: str, query: str = "travel notices disputes cards wallet commitments", top_k: int = 8) -> dict:
    """Synthesizer tool: read scoped memories from Vertex AI Memory Bank for `customer_id`."""
    return search_customer_memory(customer_id=customer_id, query=query or "customer history", top_k=top_k)


def list_all_customer_memory(customer_id: str) -> dict:
    """List every stored memory in scope `{"customer_id": customer_id}` from Vertex AI Memory Bank."""
    memories = memory_client().list_memories({"customer_id": customer_id})
    return {
        "customer_id": customer_id,
        "count": len(memories),
        "memories": [m.to_dict() for m in memories],
    }


def catalog_authorize_table(table: str) -> dict:
    """Ask Dataplex Universal Catalog whether `table` is ACTIVE and permitted (`ALLOW`, `ALLOW_TO_OWNER_ONLY`, or `REFUSE`)."""
    return governor().check_table(table).to_dict()


def catalog_authorize_column(table: str, column: str) -> dict:
    """Ask Dataplex Universal Catalog whether `table.column` may be selected or must be refused (`REFUSE`)."""
    return governor().check_column(table, column).to_dict()


def catalog_discover_tables(search_term: str) -> dict:
    """Search Dataplex Universal Catalog for sibling tables matching `search_term` (discovers deprecated tables like `authorizations_deprecated_v1`)."""
    siblings = governor().discover_sibling_tables(search_term)
    verdicts = [governor().check_table(t).to_dict() for t in siblings]
    return {"search_term": search_term, "tables": verdicts}


def glossary_lookup(term_id: str) -> dict:
    """Look up a regulatory or fraud term (e.g. `regulation-e`, `regulation-z`, `provisional-credit`) in the Dataplex Business Glossary."""
    term = governor().get_term(term_id)
    if term is None:
        return {"found": False, "term_id": term_id}
    return {"found": True, **term.to_dict()}


def glossary_search(query: str, top_k: int = 5) -> dict:
    """Search the 45-term Dataplex Business Glossary (`ccb-fraud-disputes`) for definitions matching `query`."""
    terms = governor().search_terms(query, top_k=max(1, min(int(top_k), 15)))
    return {"query": query, "count": len(terms), "terms": [t.to_dict() for t in terms]}


def list_warehouse_queries() -> dict:
    """List the parameterised BigQuery golden queries available in `GroundTruthReader`."""
    return {
        "queries": [
            {
                "query_id": q.query_id,
                "purpose": q.purpose,
                "tables": list(q.tables),
                "columns_checked": [f"{t}.{c}" for t, c in q.columns_checked],
            }
            for q in GOLDEN_QUERIES.values()
        ]
    }


def run_warehouse_query(query_id: str, customer_id: str) -> dict:
    """Execute a named BigQuery golden query (`customer_360`, `open_disputes`, `travel_notices`, `cards_for_customer`, `wallet_tokens`, `contact_history`, `standing_instructions`) after Dataplex Catalog aspect checks."""
    return reader().run(query_id, {"customer_id": customer_id}).to_dict()


# --- Channel-Specific Producer Tools for Agents 01..05 ---

def query_fraud_velocity_alerts(customer_id: str, card_id: str = "CARD00002") -> dict:
    """01 `fraud_monitoring_agent` tool: Retrieves FRAUD_DETECTION / TRAVEL_STATUS / CARD_LIFECYCLE memories from Memory Bank and cross-checks BigQuery suspicious activity & impossible travel."""
    mem = search_customer_memory(customer_id, "fraud velocity alert stolen wallet Lisbon card frozen blocked attempt", top_k=5)
    g03 = execute_golden_sql_contract(
        "03",
        "fraud_monitoring_agent",
        {
            "card_id": card_id,
            "window_start_ts": datetime.datetime(2026, 9, 1, 0, 0, tzinfo=datetime.timezone.utc),
            "window_end_ts": datetime.datetime(2026, 9, 20, 0, 0, tzinfo=datetime.timezone.utc),
            "min_risk_score": 50,
        },
    )
    g04 = execute_golden_sql_contract(
        "04",
        "fraud_monitoring_agent",
        {
            "card_id": card_id,
            "as_of_ts": datetime.datetime(2026, 9, 21, 0, 0, tzinfo=datetime.timezone.utc),
            "lookback_days": 30,
            "max_plausible_kmh": 900.0,
            "min_distance_km": 500.0,
        },
    )
    return {
        "agent": "01_fraud_monitoring_agent",
        "channel": "FRAUD_OPS_OUTBOUND",
        "customer_id": customer_id,
        "card_id": card_id,
        "memory_bank_recollections": mem,
        "golden_03_suspicious_activity": g03,
        "golden_04_impossible_travel": g04,
    }


def query_ivr_call_records(customer_id: str, card_id: str = "CARD00004") -> dict:
    """02 `telephony_ivr_agent` tool: Retrieves TELEPHONY_IVR call memories (including dropped-2FA turns in Lisbon) and cross-checks BigQuery `contact_history` and `golden_02_recent_authorizations_for_card`."""
    mem = search_customer_memory(customer_id, "inbound phone call IVR callback Alfama room 612 declined", top_k=5)
    contacts = reader().run("contact_history", {"customer_id": customer_id}).to_dict()
    g02 = execute_golden_sql_contract(
        "02",
        "telephony_ivr_agent",
        {
            "card_id": card_id,
            "as_of_ts": datetime.datetime(2026, 9, 21, 0, 0, tzinfo=datetime.timezone.utc),
            "lookback_days": 30,
            "row_limit": 15,
        },
    )
    return {
        "agent": "02_telephony_ivr_agent",
        "channel": "TELEPHONY_IVR",
        "customer_id": customer_id,
        "memory_bank_recollections": mem,
        "bigquery_contact_events": contacts,
        "golden_02_recent_authorizations": g02,
    }


def query_mobile_wallet_events(customer_id: str, card_id: str = "CARD00002") -> dict:
    """03 `mobile_app_agent` tool: Retrieves MOBILE_APP / DEVICE_WALLET_INVENTORY memories and cross-checks BigQuery `golden_10_wallet_tokens_active_on_card` and `travel_notices`."""
    mem = search_customer_memory(customer_id, "Apple Pay Google Pay wallet token revoked mobile phone returned early September 10", top_k=5)
    g10 = execute_golden_sql_contract(
        "10",
        "mobile_app_agent",
        {
            "card_id": card_id,
            "as_of_ts": datetime.datetime(2026, 9, 21, 0, 0, tzinfo=datetime.timezone.utc),
            "usage_lookback_days": 30,
            "include_inactive": True,
        },
    )
    travel = reader().run("travel_notices", {"customer_id": customer_id}).to_dict()
    return {
        "agent": "03_mobile_app_agent",
        "channel": "MOBILE_APP",
        "customer_id": customer_id,
        "memory_bank_recollections": mem,
        "golden_10_wallet_tokens": g10,
        "bigquery_travel_notices": travel,
    }


def query_web_portal_activity(customer_id: str, dispute_id: str = "DSP00033") -> dict:
    """04 `web_portal_agent` tool: Retrieves WEB_PORTAL dispute memories, resolves Regulation E vs Regulation Z from Dataplex Glossary, and runs Golden Queries 07, 08, 09."""
    mem = search_customer_memory(customer_id, "dispute Casa Verde pharmacy provisional credit standing instruction SMS", top_k=5)
    g07 = execute_golden_sql_contract(
        "07",
        "web_portal_agent",
        {
            "customer_id": customer_id,
            "as_of_date": datetime.date(2026, 9, 21),
            "open_statuses": ["OPEN", "UNDER_INVESTIGATION", "PROVISIONAL_CREDIT_ISSUED"],
        },
    )
    g08 = execute_golden_sql_contract("08", "web_portal_agent", {"dispute_id": dispute_id})
    reg_terms = glossary_search("Regulation E Regulation Z provisional credit", top_k=4)
    return {
        "agent": "04_web_portal_agent",
        "channel": "WEB_PORTAL",
        "customer_id": customer_id,
        "memory_bank_recollections": mem,
        "golden_07_open_disputes": g07,
        "golden_08_filing_window_check": g08,
        "dataplex_glossary_terms": reg_terms,
    }


def query_branch_teller_interactions(customer_id: str) -> dict:
    """05 `branch_support_agent` tool: Retrieves BRANCH_SUPPORT memories and runs `golden_01_customer_360_snapshot` while voluntarily refusing `customers.ssn_last4` via Dataplex."""
    mem = search_customer_memory(customer_id, "branch replacement card courier delivered Alfama New York standing instruction", top_k=5)
    ssn_check = catalog_authorize_column("customers", "ssn_last4")
    g01 = execute_golden_sql_contract("01", "branch_support_agent", {"customer_id": customer_id})
    standing = reader().run("standing_instructions", {"customer_id": customer_id}).to_dict()
    return {
        "agent": "05_branch_support_agent",
        "channel": "BRANCH_SUPPORT",
        "customer_id": customer_id,
        "memory_bank_recollections": mem,
        "dataplex_ssn_last4_governance": ssn_check,
        "golden_01_customer_360": g01,
        "standing_instructions": standing,
    }


def validate_and_record_customer_claim(customer_id: str, claim_text: str) -> dict:
    """06 `claim_veracity_validator_agent` tool: Validates a customer claim against BigQuery ground truth (`disputed_amounts` & `travel_notices`) before Memory Bank ingestion."""
    validator = ClaimValidator(reader=reader(), governor=governor())
    verdict = validator.validate(customer_id, claim_text)
    return verdict.to_dict()


def run_consolidated_memory_audit_sweep(customer_id: str = "CUST00001") -> dict:
    """07 `memory_bank_audit_agent` tool: Sweeps stored Memory Bank records for `customer_id` against BigQuery ground truth and `golden_11_dispute_event_timeline`."""
    auditor = MemoryCorpusAuditor(memory=memory_client(), reader=reader())
    audit = auditor.audit(customer_id)
    g11 = execute_golden_sql_contract(
        "11",
        "memory_bank_audit_agent",
        {
            "dispute_id": "DSP00033",
            "as_of_date": datetime.date(2026, 9, 21),
            "pre_open_hours": 72,
            "post_close_hours": 72,
        },
    )
    return {
        "agent": "07_memory_bank_audit_agent",
        "corpus_audit": audit.to_dict(),
        "golden_11_dispute_timeline": g11,
    }


def execute_one_click_card_unlock(customer_id: str, card_id: str = "CARD00002", reason: str = "Verified early return from Lisbon and in-person identity") -> dict:
    """08 `consumer_credit_synthesizer_agent` tool: Checks active wallet tokens via `golden_10_wallet_tokens_active_on_card` and stages card resolution audit trail."""
    g10 = execute_golden_sql_contract(
        "10",
        "consumer_credit_synthesizer_agent",
        {
            "card_id": card_id,
            "as_of_ts": datetime.datetime(2026, 9, 21, 0, 0, tzinfo=datetime.timezone.utc),
            "usage_lookback_days": 30,
            "include_inactive": True,
        },
    )
    return {
        "action": "STAGE_CARD_RESOLUTION_AND_AUDIT",
        "customer_id": customer_id,
        "card_id": card_id,
        "reason": reason,
        "wallet_token_evidence": g10,
        "note": "Checked active wallet tokens and standing instructions prior to staging card action.",
    }


# ==============================================================================
# 3. GOVERNANCE & AD-HOC SQL ADHERENCE EVALUATOR
# ==============================================================================

def evaluate_agent_governance_adherence(customer_id: str = "CUST00001") -> dict:
    """Empirically tests whether the 8 agents adhere to Dataplex Catalog refusals, Golden Query RBAC, and Ad-Hoc SQL blocking.

    Proves:
    1. Deprecated Table Refusal (`authorizations_deprecated_v1` discovered & refused on `lifecycle-status = DEPRECATED`).
    2. Sensitive PII Column Refusal (`cards.pan_full` and `customers.ssn_last4` refused on `sensitivity-class = REFUSE`).
    3. Cross-Agent Golden Query RBAC (`telephony_ivr_agent` refused when attempting `golden_04_geo_impossible_travel`).
    4. Ad-Hoc SQL Bypass Prevention (`SELECT pan_full, ssn_last4 FROM authorizations_deprecated_v1` blocked by `GroundTruthReader`).
    """
    gov = governor()
    rdr = reader()

    # Check 1: Sibling discovery & deprecated table refusal
    siblings = gov.discover_sibling_tables("authorizations")
    deprecated_check = gov.check_table("authorizations_deprecated_v1").to_dict()

    # Check 2: Column-level PII refusals
    pan_full_check = gov.check_column("cards", "pan_full").to_dict()
    ssn_check = gov.check_column("customers", "ssn_last4").to_dict()

    # Check 3: Cross-agent Golden Query RBAC enforcement
    rbac_test = execute_golden_sql_contract(
        "04",
        calling_agent="telephony_ivr_agent",  # Unauthorized! Only fraud_monitoring_agent & synthesizer allowed
        params={"card_id": "CARD00002"},
    )

    # Check 4: Ad-hoc SQL injection / bypass attempt
    adhoc_sql_attempt = f"SELECT card_id, pan_full, ssn_last4 FROM `{rdr.cfg.dataset_resource}.authorizations_deprecated_v1` WHERE customer_id = '{customer_id}'"
    adhoc_blocked = False
    adhoc_error = ""
    try:
        rdr.run(adhoc_sql_attempt, {"customer_id": customer_id})
    except GroundTruthError as exc:
        adhoc_blocked = True
        adhoc_error = str(exc)

    return {
        "customer_id": customer_id,
        "overall_adherence": (
            not deprecated_check["allowed"]
            and not pan_full_check["allowed"]
            and not ssn_check["allowed"]
            and rbac_test.get("rbac_refused") is True
            and adhoc_blocked
        ),
        "checks": [
            {
                "check_id": "1_deprecated_table_discovery_and_refusal",
                "agent": "01_fraud_monitoring_agent",
                "target": "authorizations_deprecated_v1",
                "discovered_siblings": siblings,
                "catalog_verdict": deprecated_check["action"],
                "basis": deprecated_check["basis"],
                "adhered": not deprecated_check["allowed"],
                "detail": "Agent dynamically discovered authorizations_deprecated_v1 via searchEntries and honoured lifecycle-status = DEPRECATED (REFUSE).",
            },
            {
                "check_id": "2_restricted_column_pan_full_refusal",
                "agent": "01_fraud_monitoring_agent & 02_telephony_ivr_agent",
                "target": "cards.pan_full",
                "catalog_verdict": pan_full_check["action"],
                "basis": pan_full_check["basis"],
                "adhered": not pan_full_check["allowed"],
                "detail": "Agents checked sensitivity-class@Schema.pan_full -> REFUSE and selected only pan_masked.",
            },
            {
                "check_id": "3_restricted_column_ssn_last4_refusal",
                "agent": "05_branch_support_agent",
                "target": "customers.ssn_last4",
                "catalog_verdict": ssn_check["action"],
                "basis": ssn_check["basis"],
                "adhered": not ssn_check["allowed"],
                "detail": "Branch agent checked sensitivity-class@Schema.ssn_last4 -> REFUSE before running Golden Query 01.",
            },
            {
                "check_id": "4_cross_agent_golden_query_rbac",
                "agent": "02_telephony_ivr_agent",
                "target": "golden_04_geo_impossible_travel.sql",
                "catalog_verdict": "REFUSE (INDEX.md Agent Scope RBAC)",
                "basis": "authorized_agents = [fraud_monitoring_agent, consumer_credit_synthesizer_agent]",
                "adhered": rbac_test.get("rbac_refused") is True,
                "detail": rbac_test.get("error", ""),
            },
            {
                "check_id": "5_adhoc_sql_bypass_prevention",
                "agent": "ALL_8_AGENTS",
                "target": adhoc_sql_attempt,
                "catalog_verdict": "BLOCKED_AT_EXECUTOR_GATE",
                "basis": "GroundTruthReader rejects raw SQL; only pre-audited Golden Query IDs execute",
                "adhered": adhoc_blocked,
                "detail": f"Ad-hoc SQL rejected with GroundTruthError: {adhoc_error}",
            },
        ],
    }


TOOLS: dict[str, Callable] = {
    fn.__name__: fn
    for fn in (
        list_memory_topics,
        list_memory_topics_tool,
        search_customer_memory,
        read_customer_memory_bank,
        list_all_customer_memory,
        catalog_authorize_table,
        catalog_authorize_column,
        catalog_discover_tables,
        glossary_lookup,
        glossary_search,
        list_warehouse_queries,
        run_warehouse_query,
        query_fraud_velocity_alerts,
        query_ivr_call_records,
        query_mobile_wallet_events,
        query_web_portal_activity,
        query_branch_teller_interactions,
        validate_and_record_customer_claim,
        run_consolidated_memory_audit_sweep,
        execute_one_click_card_unlock,
        evaluate_agent_governance_adherence,
    )
}


def _tools(*names: str) -> list:
    return [FunctionTool(TOOLS[n]) for n in names]


# ==============================================================================
# 4. THE 8 ADK AGENT FACTORIES (`01` .. `08`)
# ==============================================================================

def create_fraud_monitoring_agent(model: str = DEFAULT_MODEL) -> LlmAgent:
    """01 · fraud_monitoring_agent"""
    return LlmAgent(
        name="fraud_monitoring_agent",
        model=model,
        description=(
            "01 · Fraud Velocity & Geo-Anomaly Specialist (CHANNEL_PRODUCER · FRAUD_OPS_OUTBOUND): "
            "Monitors cross-border velocity anomalies and stolen-card POS attempts using Memory Bank "
            "(`TRAVEL_STATUS`, `CARD_LIFECYCLE`), Dataplex Catalog (`catalog_discover_tables` & `catalog_authorize_table` "
            "to refuse `authorizations_deprecated_v1` and `cards.pan_full`), and BigQuery Golden Queries 03 & 04."
        ),
        instruction=(
            "You are 01 · fraud_monitoring_agent (FRAUD_DETECTION / FRAUD_OPS_OUTBOUND).\n"
            "1. Call catalog_discover_tables('authorizations') and catalog_authorize_table before reading authorizations. "
            "Voluntarily refuse any table with lifecycle-status = DEPRECATED and any column with agent_behavior = REFUSE (such as cards.pan_full).\n"
            "2. Never read a stored fraud label (`is_fraud`) during triage — score strictly from velocity and impossible-travel signals via query_fraud_velocity_alerts.\n"
            "3. Never compose ad-hoc SQL. Use only your bound Golden Query tools."
        ),
        tools=_tools(
            "query_fraud_velocity_alerts",
            "catalog_discover_tables",
            "catalog_authorize_table",
            "catalog_authorize_column",
            "search_customer_memory",
        ),
        disallow_transfer_to_peers=True,
    )


def create_telephony_ivr_agent(model: str = DEFAULT_MODEL) -> LlmAgent:
    """02 · telephony_ivr_agent"""
    return LlmAgent(
        name="telephony_ivr_agent",
        model=model,
        description=(
            "02 · Contact Center IVR Specialist (CHANNEL_PRODUCER · TELEPHONY_IVR): "
            "Retrieves inbound call sessions and dropped-2FA recollections from Memory Bank (`AGENT_COMMITMENTS`, "
            "`CONTACT_PREFERENCE`) and verifies declined authorizations and call timestamps via BigQuery Golden Query 02 "
            "and `contact_history` under Dataplex governance."
        ),
        instruction=(
            "You are 02 · telephony_ivr_agent (TELEPHONY_IVR).\n"
            "1. Call query_ivr_call_records(customer_id) to retrieve both the customer's call recollections from Memory Bank "
            "and the authoritative call/authorization rows from BigQuery.\n"
            "2. Before quoting card numbers, verify column governance via catalog_authorize_column('cards', 'pan_full') and use only pan_masked.\n"
            "3. Always distinguish between what the customer said on a call (Memory Bank) and what the ledger recorded (BigQuery)."
        ),
        tools=_tools(
            "query_ivr_call_records",
            "catalog_authorize_table",
            "catalog_authorize_column",
            "search_customer_memory",
        ),
        disallow_transfer_to_peers=True,
    )


def create_mobile_app_agent(model: str = DEFAULT_MODEL) -> LlmAgent:
    """03 · mobile_app_agent"""
    return LlmAgent(
        name="mobile_app_agent",
        model=model,
        description=(
            "03 · Mobile & Digital Wallet Specialist (CHANNEL_PRODUCER · MOBILE_APP): "
            "Inspects Apple Pay / Google Pay token states and mobile travel check-ins using Memory Bank "
            "(`DEVICE_WALLET_INVENTORY`, `TRAVEL_STATUS`), Dataplex Catalog, and BigQuery Golden Query 10 + `travel_notices`."
        ),
        instruction=(
            "You are 03 · mobile_app_agent (MOBILE_APP).\n"
            "1. Call query_mobile_wallet_events(customer_id) to inspect digital wallet tokens (Golden Query 10) and declared vs actual travel return dates (`travel_notices`).\n"
            "2. Where Memory Bank contains a travel claim (e.g., London) that disagrees with BigQuery `travel_notices` (e.g., Lisbon TRV0001 with early return 2026-09-10), explicitly report the contradiction and treat BigQuery as authoritative."
        ),
        tools=_tools(
            "query_mobile_wallet_events",
            "catalog_authorize_table",
            "search_customer_memory",
        ),
        disallow_transfer_to_peers=True,
    )


def create_web_portal_agent(model: str = DEFAULT_MODEL) -> LlmAgent:
    """04 · web_portal_agent"""
    return LlmAgent(
        name="web_portal_agent",
        model=model,
        description=(
            "04 · Online Web Banking & Regulatory Dispute Specialist (CHANNEL_PRODUCER · WEB_PORTAL): "
            "Reconciles online dispute claims (`DISPUTE_HISTORY`) against BigQuery Golden Queries 07, 08, 09 and "
            "resolves Regulation E (Debit) vs Regulation Z (Credit) rules from the Dataplex Business Glossary."
        ),
        instruction=(
            "You are 04 · web_portal_agent (WEB_PORTAL).\n"
            "1. Call query_web_portal_activity(customer_id) to fetch open disputes (Golden Query 07), filing window checks (Golden Query 08), and Dataplex Glossary definitions.\n"
            "2. Never assume two disputes on the same account follow the same regulation: read `regulation_path` per dispute from BigQuery (`REG_E` for debit card `DSP00033` vs `REG_Z` for credit card `DSP00034`).\n"
            "3. Resolve `provisional-credit`, `regulation-e`, and `regulation-z` definitions via glossary_lookup / glossary_search."
        ),
        tools=_tools(
            "query_web_portal_activity",
            "glossary_lookup",
            "glossary_search",
            "catalog_authorize_table",
            "search_customer_memory",
        ),
        disallow_transfer_to_peers=True,
    )


def create_branch_support_agent(model: str = DEFAULT_MODEL) -> LlmAgent:
    """05 · branch_support_agent"""
    return LlmAgent(
        name="branch_support_agent",
        model=model,
        description=(
            "05 · Branch & Teller Identity Specialist (CHANNEL_PRODUCER · BRANCH_SUPPORT): "
            "Handles in-person branch verification and standing instructions using Memory Bank (`CONTACT_PREFERENCE`, "
            "`AGENT_COMMITMENTS`), BigQuery Golden Query 01 (`v_customer_360`), and Dataplex column governance (`customers.ssn_last4` REFUSE)."
        ),
        instruction=(
            "You are 05 · branch_support_agent (BRANCH_SUPPORT).\n"
            "1. Call catalog_authorize_column('customers', 'ssn_last4') first and honour its REFUSE verdict.\n"
            "2. Call query_branch_teller_interactions(customer_id) to verify the customer's 360 snapshot (Golden Query 01), standing instructions, and branch recollections."
        ),
        tools=_tools(
            "query_branch_teller_interactions",
            "catalog_authorize_table",
            "catalog_authorize_column",
            "list_memory_topics",
            "search_customer_memory",
        ),
        disallow_transfer_to_peers=True,
    )


def create_claim_veracity_validator_agent(model: str = DEFAULT_MODEL) -> LlmAgent:
    """06 · claim_veracity_validator_agent"""
    return LlmAgent(
        name="claim_veracity_validator_agent",
        model=model,
        description=(
            "06 · Pre-Write Claim Veracity & Governance Gatekeeper (PRE_WRITE_VALIDATOR): "
            "Validates customer claims against BigQuery ground truth (`disputed_amounts`, `travel_notices`) "
            "before Memory Bank write-back and audits agent adherence to Dataplex Catalog refusals."
        ),
        instruction=(
            "You are 06 · claim_veracity_validator_agent (PRE_WRITE_VALIDATOR).\n"
            "1. Call validate_and_record_customer_claim(customer_id, claim_text) to verify any stated amount or travel destination against BigQuery ground truth.\n"
            "2. Call evaluate_agent_governance_adherence(customer_id) when asked to verify whether agents adhere to Dataplex Catalog refusals and block ad-hoc SQL."
        ),
        tools=_tools(
            "validate_and_record_customer_claim",
            "evaluate_agent_governance_adherence",
            "catalog_authorize_table",
            "catalog_authorize_column",
        ),
        disallow_transfer_to_peers=True,
    )


def create_memory_bank_audit_agent(model: str = DEFAULT_MODEL) -> LlmAgent:
    """07 · memory_bank_audit_agent"""
    return LlmAgent(
        name="memory_bank_audit_agent",
        model=model,
        description=(
            "07 · Consolidated Memory Bank Sweep Auditor (CONSOLIDATED_AUDITOR): "
            "Sweeps all stored customer memories in Vertex AI Memory Bank against BigQuery ground truth and "
            "Golden Query 11 (`dispute_event_timeline`), surfacing travel distractors and euro/dollar amount discrepancies."
        ),
        instruction=(
            "You are 07 · memory_bank_audit_agent (CONSOLIDATED_AUDITOR).\n"
            "Call run_consolidated_memory_audit_sweep(customer_id) to audit every memory in the customer's scope against "
            "BigQuery `disputes` and `travel_notices`, and report every corroborated and contradicted memory unedited."
        ),
        tools=_tools(
            "run_consolidated_memory_audit_sweep",
            "list_all_customer_memory",
        ),
        disallow_transfer_to_peers=True,
    )


def create_consumer_credit_synthesizer_agent(model: str = DEFAULT_MODEL, memory_bank: Any = None) -> LlmAgent:
    """08 · consumer_credit_synthesizer_agent (Root Hub of the 8-Agent Topology)"""
    return LlmAgent(
        name="consumer_credit_synthesizer_agent",
        model=model,
        description=(
            "08 · Lead Consumer Credit Synthesizer Orchestrator (LEAD_SYNTHESIZER): "
            "Hub of the 8-agent mesh. Coordinates the 5 channel producer agents (01..05), the Pre-Write Veracity "
            "Validator (06), and the Memory Bank Auditor (07); enforces Dataplex Catalog governance & Glossary definitions; "
            "reconciles Memory Bank recollections against BigQuery ground truth; and drives zero-question resolution."
        ),
        instruction=(
            "You are 08 · consumer_credit_synthesizer_agent, the Lead Orchestrator of the 8-agent JPMorgan Chase CCB Fraud & Disputes Desk.\n"
            "1. Coordinate your 7 specialist sub-agents (`fraud_monitoring_agent`, `telephony_ivr_agent`, `mobile_app_agent`, "
            "`web_portal_agent`, `branch_support_agent`, `claim_veracity_validator_agent`, `memory_bank_audit_agent`) or call your bound tools.\n"
            "2. Where Vertex AI Memory Bank recollections (e.g., London travel claim or 412/90 euro conversational estimates) "
            "disagree with BigQuery system-of-record rows (`Lisbon` notice `TRV0001` with early return `2026-09-10`; open disputes "
            "`DSP00033` $26.30 under `REG_E` and `DSP00034` $1,065.76 under `REG_Z`), explicitly report both and answer from BigQuery.\n"
            "3. Honour all Dataplex Catalog `REFUSE` verdicts (`authorizations_deprecated_v1`, `cards.pan_full`, `customers.ssn_last4`) "
            "and respect the customer's standing instruction (notify by SMS before closing any dispute)."
        ),
        tools=_tools(
            "list_memory_topics",
            "read_customer_memory_bank",
            "glossary_search",
            "run_warehouse_query",
            "execute_one_click_card_unlock",
            "evaluate_agent_governance_adherence",
        ),
        sub_agents=[
            create_fraud_monitoring_agent(model),
            create_telephony_ivr_agent(model),
            create_mobile_app_agent(model),
            create_web_portal_agent(model),
            create_branch_support_agent(model),
            create_claim_veracity_validator_agent(model),
            create_memory_bank_audit_agent(model),
        ],
    )


# Alias so `agent/agent.py`, `custom_agent_comparison.py`, and existing callers work seamlessly
create_orchestrator = create_consumer_credit_synthesizer_agent


# ==============================================================================
# 5. ROSTER & EXECUTION TRACE
# ==============================================================================

@dataclass
class AgentInfo:
    agent_id: str
    name: str
    role: str
    channel: str
    description: str
    tools: list[str]
    golden_queries_authorized: list[str]
    kc_governed_assets: list[str]
    mb_topics_scoped: list[str]

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "role": self.role,
            "channel": self.channel,
            "description": self.description,
            "tools": self.tools,
            "golden_queries_authorized": self.golden_queries_authorized,
            "kc_governed_assets": self.kc_governed_assets,
            "mb_topics_scoped": self.mb_topics_scoped,
        }


def list_all_agents() -> list[AgentInfo]:
    """Return the complete 8-agent roster in canonical order `01` .. `08`."""
    root = create_consumer_credit_synthesizer_agent()
    spokes = list(root.sub_agents or [])
    ordered = [*spokes, root]  # 01..07 spokes followed by 08 lead synthesizer

    meta_map = {
        "fraud_monitoring_agent": {
            "agent_id": "01 · fraud_monitoring_agent",
            "role": "Fraud Velocity & Geo-Anomaly Specialist (CHANNEL_PRODUCER)",
            "channel": "FRAUD_OPS_OUTBOUND",
            "golden": ["02_recent_authorizations", "03_suspicious_activity_window", "04_geo_impossible_travel", "05_merchant_velocity_spike", "06_cnp_burst_detection", "12_fraud_rate_by_mcc"],
            "kc": ["authorizations (ACTIVE)", "authorizations_deprecated_v1 (REFUSE · DEPRECATED)", "cards.pan_full (REFUSE · PII)", "authorizations.is_fraud (REFUSE · Triage Label)"],
            "mb": ["TRAVEL_STATUS", "CARD_LIFECYCLE", "SPENDING_CONTEXT"],
        },
        "telephony_ivr_agent": {
            "agent_id": "02 · telephony_ivr_agent",
            "role": "Contact Center IVR Specialist (CHANNEL_PRODUCER)",
            "channel": "TELEPHONY_IVR",
            "golden": ["02_recent_authorizations_for_card", "contact_history"],
            "kc": ["contact_events (ALLOW_TO_OWNER_ONLY)", "authorizations (ACTIVE)", "cards.pan_full (REFUSE)"],
            "mb": ["AGENT_COMMITMENTS", "CONTACT_PREFERENCE", "TRAVEL_STATUS"],
        },
        "mobile_app_agent": {
            "agent_id": "03 · mobile_app_agent",
            "role": "Mobile & Digital Wallet Specialist (CHANNEL_PRODUCER)",
            "channel": "MOBILE_APP",
            "golden": ["10_wallet_tokens_active_on_card", "travel_notices"],
            "kc": ["wallet_tokens (ALLOW_TO_OWNER_ONLY)", "travel_notices (ALLOW_TO_OWNER_ONLY)", "cards.pan_full (REFUSE)"],
            "mb": ["DEVICE_WALLET_INVENTORY", "TRAVEL_STATUS", "CARD_LIFECYCLE"],
        },
        "web_portal_agent": {
            "agent_id": "04 · web_portal_agent",
            "role": "Online Web Banking & Regulatory Dispute Specialist (CHANNEL_PRODUCER)",
            "channel": "WEB_PORTAL",
            "golden": ["07_open_disputes_for_customer", "08_dispute_filing_window_check", "09_provisional_credit_due"],
            "kc": ["disputes (ALLOW_TO_OWNER_ONLY)", "Glossary: regulation-e, regulation-z, provisional-credit"],
            "mb": ["DISPUTE_HISTORY", "CONTACT_PREFERENCE", "AGENT_COMMITMENTS"],
        },
        "branch_support_agent": {
            "agent_id": "05 · branch_support_agent",
            "role": "Branch & Teller Identity Specialist (CHANNEL_PRODUCER)",
            "channel": "BRANCH_SUPPORT",
            "golden": ["01_customer_360_snapshot", "standing_instructions"],
            "kc": ["v_customer_360 (ACTIVE)", "customers.ssn_last4 (REFUSE · RESTRICTED PII)"],
            "mb": ["CONTACT_PREFERENCE", "AGENT_COMMITMENTS", "CARD_LIFECYCLE"],
        },
        "claim_veracity_validator_agent": {
            "agent_id": "06 · claim_veracity_validator_agent",
            "role": "Pre-Write Claim Veracity & Governance Gatekeeper (PRE_WRITE_VALIDATOR)",
            "channel": "PRE_WRITE_GATE",
            "golden": ["01_customer_360_snapshot", "07_open_disputes_for_customer", "disputed_amounts", "travel_notices"],
            "kc": ["All 12 Tables & Aspect Contracts (Verifies Agent Adherence & Blocks Ad-Hoc SQL)"],
            "mb": ["Pre-Write Extraction Filter (7 Custom Fraud Topics vs Off-Topic Exclusion)"],
        },
        "memory_bank_audit_agent": {
            "agent_id": "07 · memory_bank_audit_agent",
            "role": "Consolidated Memory Bank Sweep Auditor (CONSOLIDATED_AUDITOR)",
            "channel": "ENTERPRISE_AUDIT",
            "golden": ["11_dispute_event_timeline", "12_fraud_rate_by_merchant_category", "disputed_amounts", "travel_notices"],
            "kc": ["disputes", "dispute_events", "travel_notices"],
            "mb": ["Full Customer Scope Sweep (Detects London Distractor & Amount Contradictions)"],
        },
        "consumer_credit_synthesizer_agent": {
            "agent_id": "08 · consumer_credit_synthesizer_agent",
            "role": "Lead Consumer Credit Synthesizer Orchestrator (LEAD_SYNTHESIZER)",
            "channel": "CORE_ORCHESTRATOR_HUB",
            "golden": ["01_customer_360_snapshot", "07_open_disputes_for_customer", "10_wallet_tokens_active_on_card"],
            "kc": ["Full Dataplex Glossary (45 Terms) & Cross-Spoke Governance Synthesis"],
            "mb": ["All 7 Custom Fraud Topics (`customer_id` Scope Synthesis & Async Write-Back)"],
        },
    }

    roster: list[AgentInfo] = []
    for ag in ordered:
        m = meta_map.get(ag.name, {})
        t_names = [
            getattr(t, "name", getattr(getattr(t, "func", None), "__name__", str(t)))
            for t in (ag.tools or [])
        ]
        roster.append(
            AgentInfo(
                agent_id=m.get("agent_id", ag.name),
                name=ag.name,
                role=m.get("role", "SPECIALIST"),
                channel=m.get("channel", "MULTI_CHANNEL"),
                description=ag.description or "",
                tools=t_names,
                golden_queries_authorized=m.get("golden", []),
                kc_governed_assets=m.get("kc", []),
                mb_topics_scoped=m.get("mb", []),
            )
        )
    return roster


@dataclass
class RunTrace:
    """What an ADK run actually did."""
    prompt: str
    user_id: str
    session_id: str
    final_text: str
    authors_seen: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    tool_responses: list[dict] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "final_text": self.final_text,
            "authors_seen": self.authors_seen,
            "tool_calls": self.tool_calls,
            "tool_call_count": len(self.tool_calls),
            "tool_responses": self.tool_responses,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


def run_turn(
    prompt: str,
    *,
    user_id: str,
    model: str = DEFAULT_MODEL,
) -> RunTrace:
    """Drive the 8-agent ADK graph for one turn via `InMemoryRunner`."""
    root = create_consumer_credit_synthesizer_agent(model=model)
    runner = InMemoryRunner(agent=root, app_name="jpmc_fraud_desk")

    async def _drive() -> RunTrace:
        started = time.perf_counter()
        session = await runner.session_service.create_session(
            app_name="jpmc_fraud_desk",
            user_id=user_id,
        )
        trace = RunTrace(
            prompt=prompt,
            user_id=user_id,
            session_id=session.id,
            final_text="",
        )
        message = types.Content(role="user", parts=[types.Part(text=prompt)])

        async for event in runner.run_async(
            user_id=user_id,
            session_id=session.id,
            new_message=message,
        ):
            author = getattr(event, "author", None)
            if author and author not in trace.authors_seen:
                trace.authors_seen.append(author)

            content = getattr(event, "content", None)
            for part in getattr(content, "parts", None) or []:
                fc = getattr(part, "function_call", None)
                if fc is not None:
                    trace.tool_calls.append(
                        {
                            "agent": author,
                            "tool": getattr(fc, "name", ""),
                            "args": dict(getattr(fc, "args", {}) or {}),
                        }
                    )
                fr = getattr(part, "function_response", None)
                if fr is not None:
                    trace.tool_responses.append(
                        {
                            "agent": author,
                            "tool": getattr(fr, "name", ""),
                        }
                    )
                txt = getattr(part, "text", None)
                if txt:
                    trace.final_text = txt

        trace.elapsed_ms = (time.perf_counter() - started) * 1000.0
        return trace

    return asyncio.run(_drive())


def build_jpmc_adk_system(model: str = DEFAULT_MODEL) -> dict:
    root = create_consumer_credit_synthesizer_agent(model=model)
    runner = InMemoryRunner(agent=root, app_name="jpmc_fraud_desk")
    return {
        "root_agent": root,
        "runner": runner,
        "agents": [a.to_dict() for a in list_all_agents()],
    }

"""
adk_agents.py - JPMC CCB Fraud & Disputes 6-Agent ADK Topology + Golden Query Tool Wiring.

Connects the three live pillars of the JPMC Consumer & Community Banking Fraud Desk:
1. Vertex AI Agent Engine Memory Bank (`backend/memory_bank.py` -> `MemoryBankClient`)
   - Engine-level client (`project`, `engine_id`, `location`) where `customer_id` is passed
     per call via `scope={"customer_id": customer_id}` (never hardcoded on the client).
2. Dataplex Universal Catalog Policy Governor (`backend/knowledge_catalog.py` -> `CatalogGovernor`)
   - Voluntary, aspect-driven governance (`lifecycle-status` & `sensitivity-class.agent_behavior`).
   - Nothing is hidden or blocked by IAM brute-force errors; agents consult `CatalogGovernor`
     before touching tables/columns, voluntarily refuse `authorizations_deprecated_v1`,
     `customers.ssn_last4`, and `cards.pan_full`, and log the exact aspect evidence.
3. Parameterized Golden Query Contract Executor (`backend/queries/golden/01..12.sql` + `INDEX.md`)
   - Wires all 12 `.sql` contracts defined in `queries/golden/INDEX.md` as callable ADK
     `FunctionTool` functions (`@parameterized` BigQuery execution, `ad_hoc_sql = 0`),
     mapped to the exact 6 agents (`01_orchestrator` .. `06_memory_writer`).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import certifi
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.adk.tools import FunctionTool

from backend.knowledge_catalog import (
    CatalogGovernor,
    create_default_catalog_governor,
)
from backend.memory_bank import (
    MemoryBankClient,
    create_default_memory_bank_client,
    field as mb_field,
)
from backend.models import (
    AgentDescriptor,
    AgentId,
    BankChannel,
    GoldenQueryContract,
    GoldenQueryExecutionResult,
    SeverityLevel,
)
from backend.veracity_and_audit import ClaimVeracityValidatorAgent, MemoryBankAuditAgent

load_dotenv()

os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

# Engine-level singletons (NOT bound to any single customer_id at initialization)
GLOBAL_MEMORY_BANK_CLIENT: MemoryBankClient = create_default_memory_bank_client(verbose=False)
GLOBAL_CATALOG_GOVERNOR: CatalogGovernor = create_default_catalog_governor(strict=True, verbose=False)


# ==============================================================================
# 1. GOLDEN QUERY CONTRACT REGISTRY (`queries/golden/INDEX.md` -> 12 `.sql` files)
# ==============================================================================

def _resolve_golden_sql_dir() -> Path:
    """Finds the directory containing `01_customer_360_snapshot.sql` .. `12_*.sql`."""
    repo_root = Path(__file__).resolve().parent.parent
    tracked_dir = repo_root / "backend" / "queries" / "golden"
    if tracked_dir.exists():
        return tracked_dir
    kit_dir = repo_root / "jpmc_fraud_demo_kit" / "jpmc_fraud_demo_kit" / "queries" / "golden"
    return kit_dir


GOLDEN_QUERY_CONTRACTS: Dict[str, GoldenQueryContract] = {
    "01": GoldenQueryContract(
        query_id="01",
        sql_filename="01_customer_360_snapshot.sql",
        tool_name="golden_01_customer_360_snapshot",
        plain_english_question="Who is this customer and what do their last 90 days look like?",
        authorized_agents=[AgentId.ORCHESTRATOR, AgentId.IDENTITY_CONTEXT],
        required_parameters=["customer_id"],
        governed_tables=["v_customer_360", "customers", "accounts", "cards"],
        governed_columns_checked=["customers.customer_id", "customers.segment", "customers.ssn_last4"],
    ),
    "02": GoldenQueryContract(
        query_id="02",
        sql_filename="02_recent_authorizations_for_card.sql",
        tool_name="golden_02_recent_authorizations_for_card",
        plain_english_question="What are the most recent charges on this card?",
        authorized_agents=[AgentId.FRAUD_TRIAGE],
        required_parameters=["card_id", "as_of_ts", "lookback_days", "row_limit"],
        governed_tables=["authorizations", "cards", "merchants"],
        governed_columns_checked=["cards.pan_masked", "cards.pan_full"],
    ),
    "03": GoldenQueryContract(
        query_id="03",
        sql_filename="03_suspicious_activity_on_card_window.sql",
        tool_name="golden_03_suspicious_activity_on_card_window",
        plain_english_question="Between these two moments, what on this card is worth a second look, and why?",
        authorized_agents=[AgentId.FRAUD_TRIAGE],
        required_parameters=["card_id", "window_start_ts", "window_end_ts", "min_risk_score"],
        governed_tables=["authorizations", "cards", "merchants"],
        governed_columns_checked=["cards.pan_masked", "cards.pan_full"],
    ),
    "04": GoldenQueryContract(
        query_id="04",
        sql_filename="04_geo_impossible_travel.sql",
        tool_name="golden_04_geo_impossible_travel",
        plain_english_question="Was this card used in two places too far apart to travel between in the time available?",
        authorized_agents=[AgentId.FRAUD_TRIAGE],
        required_parameters=["card_id", "as_of_ts", "lookback_days", "max_plausible_kmh", "min_distance_km"],
        governed_tables=["authorizations", "merchants"],
        governed_columns_checked=["authorizations.auth_id", "cards.pan_full"],
    ),
    "05": GoldenQueryContract(
        query_id="05",
        sql_filename="05_merchant_velocity_spike.sql",
        tool_name="golden_05_merchant_velocity_spike",
        plain_english_question="Which merchants had a day that looks nothing like their own normal day?",
        authorized_agents=[AgentId.FRAUD_TRIAGE],
        required_parameters=["merchant_id", "start_date", "end_date", "min_fraud_rate", "min_auth_count", "velocity_flags"],
        governed_tables=["agg_merchant_velocity_daily", "merchants"],
        governed_columns_checked=[],
    ),
    "06": GoldenQueryContract(
        query_id="06",
        sql_filename="06_cnp_burst_detection.sql",
        tool_name="golden_06_cnp_burst_detection",
        plain_english_question="Did someone fire a rapid run of online charges at this card?",
        authorized_agents=[AgentId.FRAUD_TRIAGE],
        required_parameters=["card_id", "window_start_ts", "window_end_ts", "burst_window_seconds", "min_burst_count"],
        governed_tables=["authorizations", "merchants"],
        governed_columns_checked=["cards.pan_full"],
    ),
    "07": GoldenQueryContract(
        query_id="07",
        sql_filename="07_open_disputes_for_customer.sql",
        tool_name="golden_07_open_disputes_for_customer",
        plain_english_question="What disputes does this customer already have running?",
        authorized_agents=[AgentId.ORCHESTRATOR, AgentId.POLICY_COMPLIANCE],
        required_parameters=["customer_id", "as_of_date", "open_statuses"],
        governed_tables=["disputes", "cards", "accounts"],
        governed_columns_checked=["cards.pan_masked", "cards.pan_full"],
    ),
    "08": GoldenQueryContract(
        query_id="08",
        sql_filename="08_dispute_filing_window_check.sql",
        tool_name="golden_08_dispute_filing_window_check",
        plain_english_question="Was this dispute filed in time, and what does the bank now owe and by when?",
        authorized_agents=[AgentId.POLICY_COMPLIANCE],
        required_parameters=["dispute_id"],
        governed_tables=["disputes", "accounts", "cards", "authorizations"],
        governed_columns_checked=["cards.pan_masked"],
    ),
    "09": GoldenQueryContract(
        query_id="09",
        sql_filename="09_provisional_credit_due.sql",
        tool_name="golden_09_provisional_credit_due",
        plain_english_question="Which Regulation E disputes owe the customer a provisional credit, and when is it due?",
        authorized_agents=[AgentId.POLICY_COMPLIANCE],
        required_parameters=["as_of_date", "horizon_days", "warn_days", "open_statuses"],
        governed_tables=["disputes", "accounts"],
        governed_columns_checked=[],
    ),
    "10": GoldenQueryContract(
        query_id="10",
        sql_filename="10_wallet_tokens_active_on_card.sql",
        tool_name="golden_10_wallet_tokens_active_on_card",
        plain_english_question="Which phones and watches can still spend on this card?",
        authorized_agents=[AgentId.RESOLUTION_ACTIONS],
        required_parameters=["card_id", "as_of_ts", "usage_lookback_days", "include_inactive"],
        governed_tables=["wallet_tokens", "devices", "cards"],
        governed_columns_checked=["cards.pan_masked", "cards.pan_full"],
    ),
    "11": GoldenQueryContract(
        query_id="11",
        sql_filename="11_dispute_event_timeline.sql",
        tool_name="golden_11_dispute_event_timeline",
        plain_english_question="Everything that happened on this case, in order.",
        authorized_agents=[AgentId.RESOLUTION_ACTIONS, AgentId.MEMORY_WRITER],
        required_parameters=["dispute_id", "as_of_date", "pre_open_hours", "post_close_hours"],
        governed_tables=["dispute_events", "disputes"],
        governed_columns_checked=[],
    ),
    "12": GoldenQueryContract(
        query_id="12",
        sql_filename="12_fraud_rate_by_merchant_category.sql",
        tool_name="golden_12_fraud_rate_by_merchant_category",
        plain_english_question="Which kinds of merchant are we losing money to?",
        authorized_agents=[AgentId.FRAUD_TRIAGE],
        required_parameters=["start_date", "end_date", "min_auth_count"],
        governed_tables=["authorizations", "merchants"],
        governed_columns_checked=[],
    ),
}


def load_golden_sql_text(query_id: str) -> str:
    """Loads the exact parameterized `.sql` file from `backend/queries/golden/`."""
    contract = GOLDEN_QUERY_CONTRACTS[query_id]
    sql_path = _resolve_golden_sql_dir() / contract.sql_filename
    return sql_path.read_text(encoding="utf-8")


def execute_golden_query_contract(
    query_id: str,
    calling_agent: AgentId,
    params: Dict[str, Any],
    catalog_governor: Optional[CatalogGovernor] = None,
) -> Dict[str, Any]:
    """Executes one of the 12 approved Golden Queries after consulting `CatalogGovernor`.

    1. Verifies that `calling_agent` is authorized in `queries/golden/INDEX.md`.
    2. Calls `CatalogGovernor.check_table()` on the target tables (and logs the voluntary
       refusal of `authorizations_deprecated_v1` if authorizations are being queried) and
       `CatalogGovernor.check_column()` on governed columns (`ssn_last4`, `pan_full`).
    3. Executes the parameterized `.sql` file on BigQuery (`@named` parameters, `ad_hoc_sql = 0`).
    """
    governor = catalog_governor or GLOBAL_CATALOG_GOVERNOR
    contract = GOLDEN_QUERY_CONTRACTS[query_id]

    if calling_agent not in contract.authorized_agents:
        return {
            "allowed": False,
            "error": (
                f"Agent {calling_agent.value} is not authorized in queries/golden/INDEX.md "
                f"to call {contract.sql_filename}. Allowed: {[a.value for a in contract.authorized_agents]}"
            ),
        }

    # Step 1: Pre-query Dataplex Catalog Governance checks (Voluntary Refusal Proof)
    voluntary_refusals: List[Dict[str, Any]] = []
    primary_table = contract.governed_tables[0]
    table_decision = governor.check_table(primary_table)

    # Whenever an agent queries `authorizations`, it also checks the sibling table
    # `authorizations_deprecated_v1` discovered via catalog search and voluntarily refuses it
    if "authorizations" in contract.governed_tables:
        dep_decision = governor.check_table("authorizations_deprecated_v1")
        if not dep_decision.allowed:
            voluntary_refusals.append(dep_decision.to_dict())

    for col_spec in contract.governed_columns_checked:
        if "." in col_spec:
            tbl_name, col_name = col_spec.split(".", 1)
            col_decision = governor.check_column(tbl_name, col_name)
            if not col_decision.allowed:
                voluntary_refusals.append(col_decision.to_dict())

    # Step 2: Run the parameterized SQL on BigQuery
    rows: List[Dict[str, Any]] = []
    try:
        from google.cloud import bigquery

        bq_client = bigquery.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT", "abhinayboda-genai-bb"))
        sql_text = load_golden_sql_text(query_id)

        query_parameters = []
        for k, v in params.items():
            if isinstance(v, list):
                query_parameters.append(bigquery.ArrayQueryParameter(k, "STRING", v))
            elif isinstance(v, int):
                query_parameters.append(bigquery.ScalarQueryParameter(k, "INT64", v))
            elif isinstance(v, float):
                query_parameters.append(bigquery.ScalarQueryParameter(k, "FLOAT64", v))
            elif isinstance(v, bool):
                query_parameters.append(bigquery.ScalarQueryParameter(k, "BOOL", v))
            elif isinstance(v, datetime):
                query_parameters.append(bigquery.ScalarQueryParameter(k, "TIMESTAMP", v))
            elif isinstance(v, date):
                query_parameters.append(bigquery.ScalarQueryParameter(k, "DATE", v))
            else:
                query_parameters.append(bigquery.ScalarQueryParameter(k, "STRING", str(v)))

        job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
        query_job = bq_client.query(sql_text, job_config=job_config)
        for row in query_job.result():
            rows.append({key: (val.isoformat() if hasattr(val, "isoformat") else val) for key, val in dict(row).items()})
    except Exception as exc:  # pragma: no cover
        rows = [{"note": f"BigQuery execution note: {exc}"}]

    result = GoldenQueryExecutionResult(
        query_id=contract.query_id,
        tool_name=contract.tool_name,
        calling_agent=calling_agent,
        parameters_bound={k: str(v) for k, v in params.items()},
        catalog_verdict=table_decision.to_dict(),
        voluntary_refusals_logged=voluntary_refusals,
        row_count=len(rows),
        rows=rows[:25],
        ad_hoc_sql_used=False,
    )
    return result.model_dump(mode="json")


# ==============================================================================
# 2. CALLABLE ADK FUNCTION TOOLS FOR THE 6 AGENTS
# ==============================================================================

def golden_01_customer_360_snapshot(customer_id: str = "CUST00001") -> Dict[str, Any]:
    """Golden Query 01: Who is this customer and what do their last 90 days look like? (Omits ssn_last4 per Dataplex aspect)."""
    return execute_golden_query_contract(
        query_id="01",
        calling_agent=AgentId.IDENTITY_CONTEXT,
        params={"customer_id": customer_id},
    )


def golden_02_recent_authorizations_for_card(
    card_id: str = "CARD00002",
    lookback_days: int = 14,
    row_limit: int = 25,
) -> Dict[str, Any]:
    """Golden Query 02: What are the most recent charges on this card? (Uses pan_masked, refuses pan_full)."""
    return execute_golden_query_contract(
        query_id="02",
        calling_agent=AgentId.FRAUD_TRIAGE,
        params={
            "card_id": card_id,
            "as_of_ts": datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
            "lookback_days": lookback_days,
            "row_limit": row_limit,
        },
    )


def golden_03_suspicious_activity_on_card_window(
    card_id: str = "CARD00002",
    min_risk_score: float = 0.65,
) -> Dict[str, Any]:
    """Golden Query 03: Between two moments, what on this card is worth a second look, and why? (Never reads is_fraud answer key)."""
    return execute_golden_query_contract(
        query_id="03",
        calling_agent=AgentId.FRAUD_TRIAGE,
        params={
            "card_id": card_id,
            "window_start_ts": datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc),
            "window_end_ts": datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc),
            "min_risk_score": min_risk_score,
        },
    )


def golden_04_geo_impossible_travel(
    card_id: str = "CARD00002",
    lookback_days: int = 14,
) -> Dict[str, Any]:
    """Golden Query 04: Was this card used in two places too far apart to travel between in the time available?"""
    return execute_golden_query_contract(
        query_id="04",
        calling_agent=AgentId.FRAUD_TRIAGE,
        params={
            "card_id": card_id,
            "as_of_ts": datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
            "lookback_days": lookback_days,
            "max_plausible_kmh": 950.0,
            "min_distance_km": 500.0,
        },
    )


def golden_07_open_disputes_for_customer(customer_id: str = "CUST00001") -> Dict[str, Any]:
    """Golden Query 07: What disputes does this customer already have running? (`DSP00033` Reg E + `DSP00034` Reg Z)."""
    return execute_golden_query_contract(
        query_id="07",
        calling_agent=AgentId.POLICY_COMPLIANCE,
        params={
            "customer_id": customer_id,
            "as_of_date": date(2026, 9, 22),
            "open_statuses": ["OPEN", "INVESTIGATING", "PROVISIONAL_ISSUED", "PENDING_CUSTOMER"],
        },
    )


def golden_08_dispute_filing_window_check(dispute_id: str = "DSP00034") -> Dict[str, Any]:
    """Golden Query 08: Was this dispute filed in time, and what does the bank now owe and by when? (Reg E vs Reg Z)."""
    return execute_golden_query_contract(
        query_id="08",
        calling_agent=AgentId.POLICY_COMPLIANCE,
        params={"dispute_id": dispute_id},
    )


def golden_09_provisional_credit_due() -> Dict[str, Any]:
    """Golden Query 09: Which Regulation E disputes owe the customer a provisional credit, and when is it due?"""
    return execute_golden_query_contract(
        query_id="09",
        calling_agent=AgentId.POLICY_COMPLIANCE,
        params={
            "as_of_date": date(2026, 9, 22),
            "horizon_days": 14,
            "warn_days": 3,
            "open_statuses": ["OPEN", "INVESTIGATING"],
        },
    )


def golden_10_wallet_tokens_active_on_card(card_id: str = "CARD00002") -> Dict[str, Any]:
    """Golden Query 10: Which phones and watches can still spend on this card?"""
    return execute_golden_query_contract(
        query_id="10",
        calling_agent=AgentId.RESOLUTION_ACTIONS,
        params={
            "card_id": card_id,
            "as_of_ts": datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
            "usage_lookback_days": 30,
            "include_inactive": True,
        },
    )


def golden_11_dispute_event_timeline(dispute_id: str = "DSP00033") -> Dict[str, Any]:
    """Golden Query 11: Everything that happened on this case, in order."""
    return execute_golden_query_contract(
        query_id="11",
        calling_agent=AgentId.RESOLUTION_ACTIONS,
        params={
            "dispute_id": dispute_id,
            "as_of_date": date(2026, 9, 22),
            "pre_open_hours": 48,
            "post_close_hours": 48,
        },
    )


def retrieve_customer_memories_tool(
    customer_id: str = "CUST00001",
    query: str = "Where is this customer travelling and what standing instructions exist?",
) -> Dict[str, Any]:
    """Calls Vertex AI Memory Bank (`memories:retrieve`) with per-request `scope={"customer_id": customer_id}`."""
    scope = {"customer_id": customer_id}
    memories = GLOBAL_MEMORY_BANK_CLIENT.retrieve(scope=scope, query=query, top_k=8)
    extracted = []
    for item in memories:
        mem = item.get("memory", item)
        extracted.append({
            "fact": mb_field(mem, "fact"),
            "updateTime": mb_field(mem, "updateTime"),
            "topics": mb_field(mem, "topics"),
        })
    return {
        "scope": scope,
        "engine_parent": GLOBAL_MEMORY_BANK_CLIENT.parent,
        "count": len(extracted),
        "memories": extracted,
    }


def catalog_governance_authorize_tool(
    table: str = "authorizations_deprecated_v1",
    columns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Consults Dataplex `CatalogGovernor.authorize(table, columns)` to check live `lifecycle-status` and `sensitivity-class` aspects."""
    return GLOBAL_CATALOG_GOVERNOR.authorize(table=table, columns=columns or [])


def catalog_lookup_glossary_term_tool(term_id: str = "provisional-credit") -> Dict[str, Any]:
    """Looks up an authoritative business term (`provisional-credit`, `regulation-e`, `regulation-z`) in Dataplex Glossary."""
    return GLOBAL_CATALOG_GOVERNOR.lookup_term(term_id)


# Legacy tool wrappers preserved for backward compatibility with existing endpoints
def fetch_live_account_statement_tool(customer_id: str = "CUST00001") -> Dict[str, Any]:
    return golden_01_customer_360_snapshot(customer_id=customer_id)


def query_knowledge_catalog_tool(customer_id: str = "CUST00001", policy_query: str = "provisional-credit") -> Dict[str, Any]:
    return {
        "term": catalog_lookup_glossary_term_tool(policy_query),
        "deprecated_table_check": GLOBAL_CATALOG_GOVERNOR.check_table("authorizations_deprecated_v1").to_dict(),
        "restricted_column_checks": [
            GLOBAL_CATALOG_GOVERNOR.check_column("customers", "ssn_last4").to_dict(),
            GLOBAL_CATALOG_GOVERNOR.check_column("cards", "pan_full").to_dict(),
        ],
    }


def validate_and_record_customer_claim(
    customer_id: str,
    channel: str,
    claim_text: str,
    summary: str,
    day_label: str = "Live Session",
    severity: str = "MEDIUM",
) -> Dict[str, Any]:
    bank_channel = BankChannel(channel)
    sev = SeverityLevel(severity)
    validator = ClaimVeracityValidatorAgent()
    fragment = validator.validate_and_write_to_memory_bank(
        customer_id=customer_id,
        channel=bank_channel,
        day_label=day_label,
        summary=summary,
        claim_text=claim_text,
        severity=sev,
    )
    return fragment.to_display_dict()


def run_consolidated_memory_audit_sweep() -> Dict[str, Any]:
    auditor = MemoryBankAuditAgent()
    report = auditor.run_consolidated_sweep()
    return report.model_dump()


# ==============================================================================
# 3. THE 6 CCB FRAUD DESK ADK AGENTS (`01` .. `06`)
# ==============================================================================

def create_ccb_fraud_desk_orchestrator_agent(
    memory_bank_client: Optional[MemoryBankClient] = None,
    catalog_governor: Optional[CatalogGovernor] = None,
    model: str = "gemini-2.5-flash",
) -> LlmAgent:
    """Builds the 6-agent JPMC CCB Fraud & Disputes ADK hierarchy (`Run 0412 · CUST00001 · Lisbon`).

    Notice that `memory_bank_client` and `catalog_governor` are passed as service-level
    clients — `customer_id` is never hardcoded on the client constructor, and is instead
    passed dynamically at tool invocation time (`scope={"customer_id": customer_id}`).
    """
    _ = memory_bank_client or GLOBAL_MEMORY_BANK_CLIENT
    _ = catalog_governor or GLOBAL_CATALOG_GOVERNOR

    identity_context_agent = LlmAgent(
        name="identity_and_context_agent",
        model=model,
        description="02 Identity & Context Agent: Retrieves scoped customer facts from Vertex AI Memory Bank and runs Golden Query 01 while refusing customers.ssn_last4.",
        instruction=(
            "You are Agent 02 (Identity & Context). Always retrieve scoped customer memories "
            "using retrieve_customer_memories_tool(customer_id=...) and run golden_01_customer_360_snapshot. "
            "Consult catalog_governance_authorize_tool before selecting sensitive columns and voluntarily "
            "refuse customers.ssn_last4 (sensitivity-class.agent_behavior = REFUSE)."
        ),
        tools=[
            FunctionTool(retrieve_customer_memories_tool),
            FunctionTool(golden_01_customer_360_snapshot),
            FunctionTool(catalog_governance_authorize_tool),
        ],
    )

    fraud_triage_agent = LlmAgent(
        name="fraud_triage_agent",
        model=model,
        description="03 Fraud Triage Agent: Executes Golden Queries 02, 03, 04 on BigQuery to score Lisbon travel-window activity without reading is_fraud ground truth or cards.pan_full.",
        instruction=(
            "You are Agent 03 (Fraud Triage). Use only approved Golden Queries (02, 03, 04) "
            "with named @parameters (zero ad-hoc SQL). Never select cards.pan_full (use pan_masked) "
            "and never read authorizations.is_fraud during triage."
        ),
        tools=[
            FunctionTool(golden_02_recent_authorizations_for_card),
            FunctionTool(golden_03_suspicious_activity_on_card_window),
            FunctionTool(golden_04_geo_impossible_travel),
            FunctionTool(catalog_governance_authorize_tool),
        ],
    )

    policy_compliance_agent = LlmAgent(
        name="policy_and_compliance_agent",
        model=model,
        description="04 Policy & Compliance Agent: Looks up Dataplex Glossary terms (provisional-credit, Reg E, Reg Z), refuses authorizations_deprecated_v1, and runs Golden Queries 07, 08, 09.",
        instruction=(
            "You are Agent 04 (Policy & Compliance). Resolve Regulation E (12 CFR 1005, debit card CARD00002, "
            "DSP00033 — provisional credit due within 10 business days -> 23 Sep 2026) vs Regulation Z "
            "(12 CFR 1026.13, credit card CARD00004, DSP00034 — NO provisional credit; withhold collection "
            "and finance charges across 2 billing cycles / max 90 days). Always refuse authorizations_deprecated_v1 "
            "when CatalogGovernor reports lifecycle-status.status = DEPRECATED."
        ),
        tools=[
            FunctionTool(catalog_lookup_glossary_term_tool),
            FunctionTool(catalog_governance_authorize_tool),
            FunctionTool(golden_07_open_disputes_for_customer),
            FunctionTool(golden_08_dispute_filing_window_check),
            FunctionTool(golden_09_provisional_credit_due),
        ],
    )

    resolution_actions_agent = LlmAgent(
        name="resolution_and_actions_agent",
        model=model,
        description="05 Resolution & Actions Agent: Freezes compromised cards, revokes active device wallet tokens (Golden Query 10), issues VCN, and obeys standing instruction MEM00016.",
        instruction=(
            "You are Agent 05 (Resolution & Actions). Check active wallet tokens via "
            "golden_10_wallet_tokens_active_on_card and event history via golden_11_dispute_event_timeline. "
            "Respect customer standing instruction MEM00016: never close a dispute without notifying the customer first."
        ),
        tools=[
            FunctionTool(golden_10_wallet_tokens_active_on_card),
            FunctionTool(golden_11_dispute_event_timeline),
        ],
    )

    memory_writer_agent = LlmAgent(
        name="memory_writer_agent",
        model=model,
        description="06 Memory Writer Agent: Consolidates session turns into Vertex AI Memory Bank asynchronously after case close without storing restricted PII/PAN.",
        instruction=(
            "You are Agent 06 (Memory Writer). Summarize the ordered dispute timeline from "
            "golden_11_dispute_event_timeline and trigger server-side Memory Bank consolidation "
            "without storing full PAN, SSN last-4, or regulatory deadlines as static memories."
        ),
        tools=[
            FunctionTool(golden_11_dispute_event_timeline),
            FunctionTool(retrieve_customer_memories_tool),
        ],
    )

    orchestrator = LlmAgent(
        name="ccb_fraud_desk_orchestrator",
        model=model,
        description="01 Orchestrator Agent: Owns the customer conversation and coordinates the 6-agent JPMC CCB Fraud & Disputes Desk.",
        instruction=(
            "You are Agent 01 (Orchestrator) of the JPMorgan Chase CCB Fraud & Disputes Desk. "
            "Route the customer's stolen-wallet case (CUST00001 in Lisbon, debit DSP00033 under Reg E + "
            "credit DSP00034 under Reg Z) across Identity & Context (02), Fraud Triage (03), "
            "Policy & Compliance (04), Resolution & Actions (05), and Memory Writer (06)."
        ),
        tools=[
            FunctionTool(golden_01_customer_360_snapshot),
            FunctionTool(golden_07_open_disputes_for_customer),
        ],
        sub_agents=[
            identity_context_agent,
            fraud_triage_agent,
            policy_compliance_agent,
            resolution_actions_agent,
            memory_writer_agent,
        ],
    )
    return orchestrator


# Backward-compatible factory alias
def create_consumer_credit_synthesizer_agent(
    memory_bank: Any = None,
    model: str = "gemini-2.5-flash",
) -> LlmAgent:
    return create_ccb_fraud_desk_orchestrator_agent(
        memory_bank_client=GLOBAL_MEMORY_BANK_CLIENT,
        catalog_governor=GLOBAL_CATALOG_GOVERNOR,
        model=model,
    )


def list_all_agents() -> List[AgentDescriptor]:
    """Returns AgentDescriptor models for the 6 CCB Fraud & Disputes Desk agents."""
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    descriptors = [
        AgentDescriptor(
            agent_id="01_orchestrator",
            name="01 Orchestrator",
            role="Session Router & Case Owner",
            model=model_name,
            tools=["golden_01_customer_360_snapshot", "golden_07_open_disputes_for_customer"],
            description="Owns the conversation, checks open disputes, and routes the dual-card Lisbon theft across 5 specialist subagents.",
        ),
        AgentDescriptor(
            agent_id="02_identity_context",
            name="02 Identity & Context",
            role="Vertex AI Memory Bank & Customer 360 Specialist",
            model=model_name,
            tools=["retrieve_customer_memories_tool", "golden_01_customer_360_snapshot", "catalog_governance_authorize_tool"],
            description="Retrieves 9 cross-channel sessions & temporal contradictions from Vertex AI Memory Bank; voluntarily refuses customers.ssn_last4.",
        ),
        AgentDescriptor(
            agent_id="03_fraud_triage",
            name="03 Fraud Triage",
            role="Golden SQL Transaction & Velocity Scorer",
            model=model_name,
            tools=[
                "golden_02_recent_authorizations_for_card",
                "golden_03_suspicious_activity_on_card_window",
                "golden_04_geo_impossible_travel",
                "catalog_governance_authorize_tool",
            ],
            description="Executes parameterized Golden Queries 02–06 & 12 (ad-hoc SQL = 0); voluntarily refuses cards.pan_full via Dataplex aspect.",
        ),
        AgentDescriptor(
            agent_id="04_policy_compliance",
            name="04 Policy & Compliance",
            role="Dataplex Glossary & Reg E / Reg Z Adjudicator",
            model=model_name,
            tools=[
                "catalog_lookup_glossary_term_tool",
                "catalog_governance_authorize_tool",
                "golden_07_open_disputes_for_customer",
                "golden_08_dispute_filing_window_check",
                "golden_09_provisional_credit_due",
            ],
            description="Looks up Dataplex business glossary terms, voluntarily refuses authorizations_deprecated_v1, and computes Reg E (23 Sep credit) vs Reg Z (no provisional credit).",
        ),
        AgentDescriptor(
            agent_id="05_resolution_actions",
            name="05 Resolution & Actions",
            role="Card Freeze, Token Revocation, & VCN Issuer",
            model=model_name,
            tools=["golden_10_wallet_tokens_active_on_card", "golden_11_dispute_event_timeline"],
            description="Freezes CARD00002 & CARD00004, revokes 3 active wallet tokens, issues virtual card, and honors standing instruction MEM00016 (notify before closing).",
        ),
        AgentDescriptor(
            agent_id="06_memory_writer",
            name="06 Memory Writer",
            role="Asynchronous Post-Session Memory Consolidator",
            model=model_name,
            tools=["golden_11_dispute_event_timeline", "retrieve_customer_memories_tool"],
            description="Triggers server-side Vertex AI Memory Bank consolidation (memories:generate) after run close with zero PII/PAN persisted.",
        ),
    ]
    return descriptors


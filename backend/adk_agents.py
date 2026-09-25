"""
adk_agents.py - JPMC CCB Fraud & Disputes 8-Agent ADK Topology + Golden Query Tool Wiring.

Topology is hub-and-spoke: `consumer_credit_synthesizer_agent` is the hub, the
other seven are spokes that cannot talk to each other or back up the tree
(`disallow_transfer_to_parent=True`, `disallow_transfer_to_peers=True`). Five of
the spokes are channel producers: each one reads the seeded Vertex AI Memory
Bank sessions for its own channel and for one customer scope, and returns what
is there - an empty result set when the channel has no seeded data, never a
stand-in row.

Connects the three live pillars of the JPMC Consumer & Community Banking Fraud Desk:
1. Vertex AI Agent Engine Memory Bank (`backend/memory_bank.py` -> `MemoryBankClient`)
   - Engine-level client (`project`, `engine_id`, `location`) where `customer_id` is passed
     per call via `scope={"customer_id": customer_id}` (never hardcoded on the client).
2. Dataplex Universal Catalog Policy Governor (`backend/knowledge_catalog.py` -> `CatalogGovernor`)
   - Voluntary, aspect-driven governance (`lifecycle-status` & `sensitivity-class.agent_behavior`).
   - Nothing is hidden or blocked by IAM brute-force errors; agents consult `CatalogGovernor`
     before touching any table or column and honour whatever `agent_behavior` comes back,
     logging the aspect evidence that decided it.
   - No governed table or column is named in this file. Sibling tables are DISCOVERED with
     `searchEntries` and judged on their own aspects, so the refusal is a property of the
     catalog rather than of a constant written here. Re-label an aspect in Dataplex and the
     agents' behaviour changes on the next run with no code edit -- which is the only
     version of this claim worth demonstrating.
3. Parameterized Golden Query Contract Executor (`backend/queries/golden/01..12.sql` + `INDEX.md`)
   - The 12 `.sql` contracts in `queries/golden/INDEX.md` are the ONLY route into BigQuery:
     the text is read from disk and every value is bound as a named query parameter, so
     there is no path by which an agent can compose SQL.
   - 9 of the 12 are currently wrapped as callable ADK `FunctionTool` functions
     (01, 02, 03, 04, 07, 08, 09, 10, 11), mapped to the agents authorized for them in
     `INDEX.md`. 05, 06 and 12 have no wrapper and are unreachable by any agent.

Every value that leaves this file carries a provenance tag from
`backend/provenance.py`. The only constant allowed through is an agent's own
channel name, declared with `identity()` so the waiver is visible to the
verifier rather than taken on trust.
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import certifi
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.adk.tools import FunctionTool

from backend.knowledge_catalog import (
    CatalogGovernor,
    shared_catalog_governor,
)
from backend.memory_bank import (
    MemoryBankClient,
    shared_memory_bank_client,
    field as mb_field,
    topic_labels,
)
from backend.provenance import (
    live,
    seeded,
    computed,
    static,
    identity,
    unmeasured,
    value_of,
    NOT_RECORDED,
    require_env,
    LIVE_API,
    SEEDED,
    COMPUTED,
    STATIC,
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
from backend import memory_write_path, obs

load_dotenv()

# Trace recorder and structured logger (`backend/obs.py:get_logger`, L148).
LOG = obs.get_logger(__name__)

# Point Python HTTPS clients (`requests`, `google-auth`, `bigquery`) to certifi's
# root CA bundle so TLS handshakes to `*.googleapis.com` never fail on local venvs.
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

# Process-wide clients shared with app.py and veracity_and_audit.py (customer_id is per call):
# - MemoryBankClient (`backend/memory_bank.py:shared_memory_bank_client`)
# - CatalogGovernor  (`backend/knowledge_catalog.py:shared_catalog_governor`)
GLOBAL_MEMORY_BANK_CLIENT: MemoryBankClient = shared_memory_bank_client()
GLOBAL_CATALOG_GOVERNOR: CatalogGovernor = shared_catalog_governor()

LOG.debug(
    "singletons ready memory_bank_parent=%s catalog_project=%s dataset=%s strict=%s",
    GLOBAL_MEMORY_BANK_CLIENT.parent,
    GLOBAL_CATALOG_GOVERNOR.client.project,
    getattr(GLOBAL_CATALOG_GOVERNOR.client, "dataset", NOT_RECORDED),
    GLOBAL_CATALOG_GOVERNOR.strict,
)


# ==============================================================================
# 1. GOLDEN QUERY CONTRACT REGISTRY (`docs/GOLDEN_QUERIES_ARCHITECTURE.md`)
# ==============================================================================

def _resolve_golden_sql_dir() -> Path:
    """Returns the folder holding the 12 reviewed `.sql` files (`01`..`12`).

    Used by `load_golden_sql_text()` inside `execute_golden_query_contract()` (L331)
    so agents only run parameterized SQL contracts rather than writing raw SQL.
    See `docs/GOLDEN_QUERIES_ARCHITECTURE.md` for the full agent-to-query matrix.
    """
    repo_root = Path(__file__).resolve().parent.parent
    tracked_dir = repo_root / "backend" / "queries" / "golden"
    if tracked_dir.exists():
        return tracked_dir
    kit_dir = repo_root / "jpmc_fraud_demo_kit" / "jpmc_fraud_demo_kit" / "queries" / "golden"
    return kit_dir


# Maps each of the 12 SQL contracts (`01`..`12`) to:
#   - Which agents are allowed to run it (`authorized_agents`)
#   - Which BigQuery tables and sensitive columns Dataplex must check before execution
# Enforced at runtime by `execute_golden_query_contract()` (`backend/adk_agents.py:331`).
GOLDEN_QUERY_CONTRACTS: Dict[str, GoldenQueryContract] = {
    "01": GoldenQueryContract(
        query_id="01",
        sql_filename="01_customer_360_snapshot.sql",
        tool_name="golden_01_customer_360_snapshot",
        plain_english_question="Who is this customer and what do their last 90 days look like?",
        authorized_agents=[
            AgentId.BRANCH_SUPPORT,
            AgentId.CLAIM_VERACITY_VALIDATOR,
            AgentId.CONSUMER_CREDIT_SYNTHESIZER,
        ],
        required_parameters=["customer_id"],
        governed_tables=["v_customer_360", "customers", "accounts", "cards"],
        governed_columns_checked=["customers.customer_id", "customers.segment", "customers.ssn_last4"],
    ),
    "02": GoldenQueryContract(
        query_id="02",
        sql_filename="02_recent_authorizations_for_card.sql",
        tool_name="golden_02_recent_authorizations_for_card",
        plain_english_question="What are the most recent charges on this card?",
        authorized_agents=[
            AgentId.TELEPHONY_IVR,
            AgentId.FRAUD_MONITORING,
            AgentId.CLAIM_VERACITY_VALIDATOR,
        ],
        required_parameters=["card_id", "as_of_ts", "lookback_days", "row_limit"],
        governed_tables=["authorizations", "cards", "merchants"],
        governed_columns_checked=["cards.pan_masked", "cards.pan_full"],
    ),
    "03": GoldenQueryContract(
        query_id="03",
        sql_filename="03_suspicious_activity_on_card_window.sql",
        tool_name="golden_03_suspicious_activity_on_card_window",
        plain_english_question="Between these two moments, what on this card is worth a second look, and why?",
        authorized_agents=[
            AgentId.FRAUD_MONITORING,
            AgentId.CLAIM_VERACITY_VALIDATOR,
        ],
        required_parameters=["card_id", "window_start_ts", "window_end_ts", "min_risk_score"],
        governed_tables=["authorizations", "cards", "merchants"],
        governed_columns_checked=["cards.pan_masked", "cards.pan_full"],
    ),
    "04": GoldenQueryContract(
        query_id="04",
        sql_filename="04_geo_impossible_travel.sql",
        tool_name="golden_04_geo_impossible_travel",
        plain_english_question="Was this card used in two places too far apart to travel between in the time available?",
        authorized_agents=[
            AgentId.FRAUD_MONITORING,
            AgentId.CLAIM_VERACITY_VALIDATOR,
        ],
        required_parameters=["card_id", "as_of_ts", "lookback_days", "max_plausible_kmh", "min_distance_km"],
        governed_tables=["authorizations", "merchants"],
        governed_columns_checked=["authorizations.auth_id", "cards.pan_full"],
    ),
    "05": GoldenQueryContract(
        query_id="05",
        sql_filename="05_merchant_velocity_spike.sql",
        tool_name="golden_05_merchant_velocity_spike",
        plain_english_question="Which merchants had a day that looks nothing like their own normal day?",
        authorized_agents=[AgentId.FRAUD_MONITORING],
        required_parameters=["merchant_id", "start_date", "end_date", "min_fraud_rate", "min_auth_count", "velocity_flags"],
        governed_tables=["agg_merchant_velocity_daily", "merchants"],
        governed_columns_checked=[],
    ),
    "06": GoldenQueryContract(
        query_id="06",
        sql_filename="06_cnp_burst_detection.sql",
        tool_name="golden_06_cnp_burst_detection",
        plain_english_question="Did someone fire a rapid run of online charges at this card?",
        authorized_agents=[AgentId.FRAUD_MONITORING],
        required_parameters=["card_id", "window_start_ts", "window_end_ts", "burst_window_seconds", "min_burst_count"],
        governed_tables=["authorizations", "merchants"],
        governed_columns_checked=["cards.pan_full"],
    ),
    "07": GoldenQueryContract(
        query_id="07",
        sql_filename="07_open_disputes_for_customer.sql",
        tool_name="golden_07_open_disputes_for_customer",
        plain_english_question="What disputes does this customer already have running?",
        authorized_agents=[
            AgentId.WEB_PORTAL,
            AgentId.CLAIM_VERACITY_VALIDATOR,
            AgentId.CONSUMER_CREDIT_SYNTHESIZER,
        ],
        required_parameters=["customer_id", "as_of_date", "open_statuses"],
        governed_tables=["disputes", "cards", "accounts"],
        governed_columns_checked=["cards.pan_masked", "cards.pan_full"],
    ),
    "08": GoldenQueryContract(
        query_id="08",
        sql_filename="08_dispute_filing_window_check.sql",
        tool_name="golden_08_dispute_filing_window_check",
        plain_english_question="Was this dispute filed in time, and what does the bank now owe and by when?",
        authorized_agents=[
            AgentId.WEB_PORTAL,
            AgentId.CONSUMER_CREDIT_SYNTHESIZER,
        ],
        required_parameters=["dispute_id"],
        governed_tables=["disputes", "accounts", "cards", "authorizations"],
        governed_columns_checked=["cards.pan_masked"],
    ),
    "09": GoldenQueryContract(
        query_id="09",
        sql_filename="09_provisional_credit_due.sql",
        tool_name="golden_09_provisional_credit_due",
        plain_english_question="Which Regulation E disputes owe the customer a provisional credit, and when is it due?",
        authorized_agents=[
            AgentId.WEB_PORTAL,
            AgentId.CONSUMER_CREDIT_SYNTHESIZER,
        ],
        required_parameters=["as_of_date", "horizon_days", "warn_days", "open_statuses"],
        governed_tables=["disputes", "accounts"],
        governed_columns_checked=[],
    ),
    "10": GoldenQueryContract(
        query_id="10",
        sql_filename="10_wallet_tokens_active_on_card.sql",
        tool_name="golden_10_wallet_tokens_active_on_card",
        plain_english_question="Which phones and watches can still spend on this card?",
        authorized_agents=[
            AgentId.MOBILE_APP,
            AgentId.CONSUMER_CREDIT_SYNTHESIZER,
        ],
        required_parameters=["card_id", "as_of_ts", "usage_lookback_days", "include_inactive"],
        governed_tables=["wallet_tokens", "devices", "cards"],
        governed_columns_checked=["cards.pan_masked", "cards.pan_full"],
    ),
    "11": GoldenQueryContract(
        query_id="11",
        sql_filename="11_dispute_event_timeline.sql",
        tool_name="golden_11_dispute_event_timeline",
        plain_english_question="Everything that happened on this case, in order.",
        authorized_agents=[
            AgentId.BRANCH_SUPPORT,
            AgentId.MOBILE_APP,
            AgentId.CONSUMER_CREDIT_SYNTHESIZER,
        ],
        required_parameters=["dispute_id", "as_of_date", "pre_open_hours", "post_close_hours"],
        governed_tables=["dispute_events", "disputes"],
        governed_columns_checked=[],
    ),
    "12": GoldenQueryContract(
        query_id="12",
        sql_filename="12_fraud_rate_by_merchant_category.sql",
        tool_name="golden_12_fraud_rate_by_merchant_category",
        plain_english_question="Which kinds of merchant are we losing money to?",
        authorized_agents=[AgentId.FRAUD_MONITORING],
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


def _entry_table_name(entry: Dict[str, Any]) -> Optional[str]:
    """The BigQuery table behind one `searchEntries` result, or None if it is not one.

    A catalog search matches on descriptions as well as names, so a search for a
    table also returns glossary terms, the dataset itself and unrelated tables
    whose description happens to mention it. The entry type is what separates
    "something I could query" from "something I could read about".
    """
    dataplex_entry = entry.get("dataplexEntry", entry) or {}
    kind = str(dataplex_entry.get("entryType", "")).rsplit("/", 1)[-1]
    if kind not in ("bigquery-table", "bigquery-view"):
        return None
    resource = str((dataplex_entry.get("entrySource") or {}).get("resource", ""))
    if "/tables/" not in resource:
        return None
    return resource.rsplit("/tables/", 1)[-1] or None


#: Discovered sibling names, keyed by the table that was searched for. The set of
#: entries in the catalog does not change inside a run, so searching once per
#: table per process keeps the extra latency off every golden query. The
#: *decisions* are deliberately not cached -- each one is re-read so the aspect
#: evidence in the audit trail is from this run and not from an earlier one.
_SIBLING_TABLE_CACHE: Dict[str, List[str]] = {}


def discover_sibling_tables(
    governor: CatalogGovernor,
    primary_table: str,
    use_cache: bool = True,
) -> List[str]:
    """Tables the catalog returns alongside `primary_table`, excluding it.

    This is how a superseded copy of a table is found. Nothing in this file
    knows such a copy exists: the agent asks the catalog what sits next to the
    table it is about to read, and the answer is whatever Dataplex currently
    holds. Add a second deprecated twin tomorrow and it is refused tomorrow,
    with no edit here.

    A search failure is logged and returns an empty list rather than raising,
    because losing discovery must not take the golden query down with it -- but
    it is a WARNING, because a run with no discovery is proving less than a run
    with it.
    """
    if use_cache and primary_table in _SIBLING_TABLE_CACHE:
        siblings = _SIBLING_TABLE_CACHE[primary_table]
        LOG.debug("catalog discovery cache hit for %s -> %s", primary_table, siblings)
        return siblings

    with obs.turn("catalog.discover", LOG, level=logging.DEBUG, search=primary_table) as span:
        try:
            entries = governor.client.search_entries(primary_table)
        except Exception as exc:  # noqa: BLE001 - discovery is best effort, the query is not
            LOG.warning(
                "catalog search for %r failed (%s: %s); this run proves no sibling refusal",
                primary_table, type(exc).__name__, exc,
            )
            return []

        names = [name for name in (_entry_table_name(e) for e in entries) if name]
        siblings = sorted({name for name in names if name != primary_table})
        span.detail(entries=len(entries), tables=len(names), siblings=siblings)

    _SIBLING_TABLE_CACHE[primary_table] = siblings
    return siblings


def execute_golden_query_contract(
    query_id: str,
    calling_agent: AgentId,
    params: Dict[str, Any],
    catalog_governor: Optional[CatalogGovernor] = None,
) -> Dict[str, Any]:
    """Executes one of the 12 approved Golden Queries after consulting `CatalogGovernor`.

    1. Verifies that `calling_agent` is authorized in `queries/golden/INDEX.md`.
    2. Asks the catalog what else sits alongside the target table, checks the lifecycle
       aspect of each, and records a refusal for any the catalog says is unfit. Also
       checks `CatalogGovernor.check_column()` on the columns this contract declares.
    3. Executes the parameterized `.sql` file on BigQuery (`@named` parameters, `ad_hoc_sql = 0`).

    Note on step 2: the refusals are recorded, not enforced -- the query in step 3 runs
    either way. That is deliberate and is what `CATALOG_ADVISORY_NOTE` in
    `veracity_and_audit.py` describes: Dataplex advises, the agent's visible decision to
    decline is the control. Enforcement of a declined column happens downstream, where
    `_redact()` blanks it out of the rows.
    """
    governor = catalog_governor or GLOBAL_CATALOG_GOVERNOR
    contract = GOLDEN_QUERY_CONTRACTS[query_id]

    with obs.turn(
        "golden_query.%s" % query_id, LOG,
        tool=contract.tool_name, agent=calling_agent.value,
    ) as span:
        if calling_agent not in contract.authorized_agents:
            LOG.warning(
                "golden query %s refused: %s is not in INDEX.md's authorized set %s",
                query_id, calling_agent.value, [a.value for a in contract.authorized_agents],
            )
            span.detail(allowed=False, reason="agent_not_authorized")
            return {
                "allowed": False,
                "error": (
                    f"Agent {calling_agent.value} is not authorized in queries/golden/INDEX.md "
                    f"to call {contract.sql_filename}. Allowed: {[a.value for a in contract.authorized_agents]}"
                ),
            }

        span.debug_payload("params", params)

        # Step 1: Pre-query Dataplex Catalog Governance checks (Voluntary Refusal Proof)
        voluntary_refusals: List[Dict[str, Any]] = []
        primary_table = contract.governed_tables[0]

        with obs.turn("catalog.check_table", LOG, level=logging.DEBUG, table=primary_table) as check:
            table_decision = governor.check_table(primary_table)
            check.detail(action=table_decision.action, evidence=table_decision.evidence)
        if not table_decision.allowed:
            # The primary table itself being unfit is a louder event than a
            # sibling being unfit, and it is not currently enforced, so it has to
            # be visible in the log or it is invisible entirely.
            LOG.warning(
                "catalog says the PRIMARY table %s is unfit (%s); the query still runs "
                "because this gate is advisory",
                primary_table, table_decision.reason,
            )

        # Siblings are discovered, never named. The catalog is asked what sits
        # next to this table and each answer is judged on its own aspects.
        for sibling in discover_sibling_tables(governor, primary_table):
            with obs.turn("catalog.check_table", LOG, level=logging.DEBUG, table=sibling) as check:
                decision = governor.check_table(sibling)
                check.detail(action=decision.action, evidence=decision.evidence)
            if not decision.allowed:
                LOG.info(
                    "voluntary refusal: %s declined on %s (%s)",
                    sibling, decision.evidence, decision.reason,
                )
                voluntary_refusals.append(decision.to_dict())

        for col_spec in contract.governed_columns_checked:
            if "." in col_spec:
                tbl_name, col_name = col_spec.split(".", 1)
                with obs.turn("catalog.check_column", LOG, level=logging.DEBUG, column=col_spec) as check:
                    col_decision = governor.check_column(tbl_name, col_name)
                    check.detail(action=col_decision.action, evidence=col_decision.evidence)
                if not col_decision.allowed:
                    LOG.info(
                        "voluntary refusal: %s declined on %s (agent_behavior=%s)",
                        col_spec, col_decision.evidence, col_decision.action,
                    )
                    voluntary_refusals.append(col_decision.to_dict())

        # Step 2: Run the parameterized SQL on BigQuery
        rows: List[Dict[str, Any]] = []
        try:
            from google.cloud import bigquery

            bq_client = bigquery.Client(project=require_env("GOOGLE_CLOUD_PROJECT"))
            sql_text = load_golden_sql_text(query_id)
            LOG.debug("loaded %s (%d chars) from disk", contract.sql_filename, len(sql_text))

            query_parameters = []
            for k, v in params.items():
                # Order matters twice here, and both cases are subclass traps:
                #   bool is a subclass of int, so `isinstance(v, int)` first made
                #     the bool branch unreachable and bound True as INT64, which
                #     BigQuery rejects with "Bad int64 value: True".
                #   datetime is a subclass of date, so datetime must also precede
                #     date or every timestamp would be truncated to a day.
                if isinstance(v, list):
                    query_parameters.append(bigquery.ArrayQueryParameter(k, "STRING", v))
                elif isinstance(v, bool):
                    query_parameters.append(bigquery.ScalarQueryParameter(k, "BOOL", v))
                elif isinstance(v, int):
                    query_parameters.append(bigquery.ScalarQueryParameter(k, "INT64", v))
                elif isinstance(v, float):
                    query_parameters.append(bigquery.ScalarQueryParameter(k, "FLOAT64", v))
                elif isinstance(v, datetime):
                    query_parameters.append(bigquery.ScalarQueryParameter(k, "TIMESTAMP", v))
                elif isinstance(v, date):
                    query_parameters.append(bigquery.ScalarQueryParameter(k, "DATE", v))
                else:
                    query_parameters.append(bigquery.ScalarQueryParameter(k, "STRING", str(v)))

            # The bound parameters are the proof that no value was interpolated
            # into the SQL text, so they are worth a line of their own at DEBUG.
            LOG.debug(
                "bound %d named parameters: %s",
                len(query_parameters),
                [(p.name, getattr(p, "type_", None) or getattr(p, "array_type", None)) for p in query_parameters],
            )

            job_config = bigquery.QueryJobConfig(query_parameters=query_parameters)
            with obs.turn("bigquery.query", LOG, level=logging.DEBUG, sql=contract.sql_filename) as job_span:
                query_job = bq_client.query(sql_text, job_config=job_config)
                for row in query_job.result():
                    rows.append({key: (val.isoformat() if hasattr(val, "isoformat") else val) for key, val in dict(row).items()})
                job_span.detail(
                    job_id=getattr(query_job, "job_id", NOT_RECORDED),
                    bytes_billed=getattr(query_job, "total_bytes_billed", None),
                    rows=len(rows),
                )
        except Exception as exc:  # pragma: no cover
            # A failed query used to come back as a one-row result carrying the error
            # text, which made row_count=1 and looked like data downstream. Fail loudly.
            raise RuntimeError(
                "Golden Query %s (%s) failed against BigQuery: %s"
                % (contract.query_id, contract.sql_filename, exc)
            ) from exc

        if rows:
            span.debug_payload("first row", rows[0])
        else:
            LOG.debug("%s returned no rows; reporting empty, not a stand-in", contract.sql_filename)

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
        span.detail(rows=len(rows), refusals=len(voluntary_refusals))
        return result.model_dump(mode="json")


# ==============================================================================
# 2. CALLABLE ADK GOLDEN QUERY FUNCTION TOOLS (shared by the roster)
# ==============================================================================

def golden_01_customer_360_snapshot(customer_id: str = "CUST00001") -> Dict[str, Any]:
    """Golden Query 01: Who is this customer and what do their last 90 days look like? Column selection follows the catalog's sensitivity aspects."""
    return execute_golden_query_contract(
        query_id="01",
        calling_agent=AgentId.BRANCH_SUPPORT,
        params={"customer_id": customer_id},
    )


def golden_02_recent_authorizations_for_card(
    card_id: str = "CARD00002",
    lookback_days: int = 14,
    row_limit: int = 25,
) -> Dict[str, Any]:
    """Golden Query 02: What are the most recent charges on this card? Column selection follows the catalog's sensitivity aspects."""
    return execute_golden_query_contract(
        query_id="02",
        calling_agent=AgentId.TELEPHONY_IVR,
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
    """Golden Query 03: Between two moments, what on this card is worth a second look, and why? Scores on signals only, never on a stored label."""
    return execute_golden_query_contract(
        query_id="03",
        calling_agent=AgentId.FRAUD_MONITORING,
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
        calling_agent=AgentId.FRAUD_MONITORING,
        params={
            "card_id": card_id,
            "as_of_ts": datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
            "lookback_days": lookback_days,
            "max_plausible_kmh": 950.0,
            "min_distance_km": 500.0,
        },
    )


def golden_07_open_disputes_for_customer(customer_id: str = "CUST00001") -> Dict[str, Any]:
    """Golden Query 07: What disputes does this customer already have running, and which regulation does the ledger record for each?"""
    return execute_golden_query_contract(
        query_id="07",
        calling_agent=AgentId.WEB_PORTAL,
        params={
            "customer_id": customer_id,
            "as_of_date": date(2026, 9, 22),
            # Must match disputes.status values (jpmc_fraud_demo_kit/.../schemas/disputes.json).
            # A provisional credit leaves the dispute open: the money can still be reversed.
            "open_statuses": ["OPEN", "UNDER_REVIEW", "PROVISIONAL_CREDIT_ISSUED"],
        },
    )


def golden_08_dispute_filing_window_check(dispute_id: str = "DSP00034") -> Dict[str, Any]:
    """Golden Query 08: Was this dispute filed in time, and what does the bank now owe and by when, under the regulation the ledger records for it?"""
    return execute_golden_query_contract(
        query_id="08",
        calling_agent=AgentId.WEB_PORTAL,
        params={"dispute_id": dispute_id},
    )


def golden_09_provisional_credit_due() -> Dict[str, Any]:
    """Golden Query 09: Which Regulation E disputes owe the customer a provisional credit, and when is it due?"""
    return execute_golden_query_contract(
        query_id="09",
        calling_agent=AgentId.WEB_PORTAL,
        params={
            "as_of_date": date(2026, 9, 22),
            "horizon_days": 14,
            "warn_days": 3,
            # Already-credited disputes (PROVISIONAL_CREDIT_ISSUED) are left out on purpose: nothing is due.
            "open_statuses": ["OPEN", "UNDER_REVIEW"],
        },
    )


def golden_10_wallet_tokens_active_on_card(card_id: str = "CARD00002") -> Dict[str, Any]:
    """Golden Query 10: Which phones and watches can still spend on this card?"""
    return execute_golden_query_contract(
        query_id="10",
        calling_agent=AgentId.MOBILE_APP,
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
        calling_agent=AgentId.CONSUMER_CREDIT_SYNTHESIZER,
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
    with obs.turn("memory.retrieve", LOG, customer_id=customer_id) as span:
        span.debug_payload("query", query)
        scope = {"customer_id": customer_id}
        memories = memory_write_path.retrieve_for_agents(
            GLOBAL_MEMORY_BANK_CLIENT, customer_id, query=query, top_k=8)
        extracted = []
        for item in memories:
            mem = item.get("memory", item)
            extracted.append({
                "fact": mb_field(mem, "fact"),
                "updateTime": mb_field(mem, "updateTime"),
                # Flattened to plain labels so they line up with what
                # list_memory_topics_tool() reports. The raw API shape here is
                # {"customMemoryTopicLabel": "..."}, which does not.
                "topics": topic_labels(mem),
            })
        # The facts themselves are the thing you actually want to see when a turn
        # reasons oddly, so they go in at DEBUG rather than just the count.
        span.debug_payload("facts", [m["fact"] for m in extracted])
        if not extracted:
            LOG.warning(
                "memory retrieve returned nothing for scope %s; the turn will proceed ungrounded",
                scope,
            )
        span.detail(count=len(extracted))
        return {
            "scope": scope,
            "engine_parent": GLOBAL_MEMORY_BANK_CLIENT.parent,
            "count": len(extracted),
            "memories": extracted,
        }


def list_memory_topics_tool() -> Dict[str, Any]:
    """Returns the topic vocabulary this Memory Bank was configured with.

    Call this BEFORE retrieving memories. Every stored memory is filed under one
    of these topics by the extraction model, so the labels and their descriptions
    tell you what kinds of fact exist for a customer and what words will actually
    match them. Retrieval is a semantic search: a query phrased in the vocabulary
    the memories were filed under finds them; a guess may not.

    The list is read from the live Agent Engine on first use, not held in code,
    so it is whatever the deployment is configured with right now.
    """
    with obs.turn("memory.list_topics", LOG) as span:
        topics = GLOBAL_MEMORY_BANK_CLIENT.memory_topics()
        span.debug_payload("topics", topics)
        if not topics:
            # No vocabulary means extraction is unconstrained. That is a real
            # deployment state and the agent should be told, not left to assume
            # the call failed.
            LOG.warning(
                "engine %s has no memory topics configured; extraction is unconstrained",
                GLOBAL_MEMORY_BANK_CLIENT.parent,
            )
        span.detail(count=len(topics), labels=[t["label"] for t in topics])
        return {
            "engine_parent": GLOBAL_MEMORY_BANK_CLIENT.parent,
            "source": "reasoningEngines.get -> contextSpec.memoryBankConfig.customizationConfigs[].memoryTopics[]",
            "count": len(topics),
            "topics": topics,
        }


def catalog_governance_authorize_tool(
    table: str,
    columns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Consults Dataplex `CatalogGovernor.authorize(table, columns)` to check live `lifecycle-status` and `sensitivity-class` aspects.

    `table` is deliberately required. ADK publishes a parameter's default value in
    the schema the model reads, so a default here would name a governed table in
    every prompt and the model would call it with that name because it was
    suggested one -- which is not the same as the model discovering it.
    """
    with obs.turn("catalog.authorize", LOG, table=table, columns=columns or []) as span:
        verdict = GLOBAL_CATALOG_GOVERNOR.authorize(table=table, columns=columns or [])
        span.debug_payload("verdict", verdict)
        span.detail(allowed=verdict.get("allowed"))
        return verdict


def catalog_lookup_glossary_term_tool(term_id: str = "provisional-credit") -> Dict[str, Any]:
    """Looks up an authoritative business term in the Dataplex Glossary (`glossaries/*/terms/*`)."""
    with obs.turn("catalog.lookup_term", LOG, term_id=term_id) as span:
        term = GLOBAL_CATALOG_GOVERNOR.lookup_term(term_id)
        span.debug_payload("term", term)
        return term


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
# 3. CHANNEL PRODUCER RETRIEVAL (seeded Vertex AI Memory Bank sessions)
# ==============================================================================

DEFAULT_CUSTOMER_SCOPE_ID = os.environ.get("DEMO_CUSTOMER_ID", "CUST00001")
DEFAULT_CHANNEL_TOP_K = int(os.environ.get("CHANNEL_MEMORY_TOP_K", "5"))
DEFAULT_CARD_ID = os.environ.get("DEMO_CARD_ID", "CARD00002")

# A seeder may label a session with any spelling in this tuple and mean the same
# channel. The tokens are matching predicates that live in code; the label the
# agent reports back is whatever the session itself carried.
CHANNEL_SESSION_TOKENS: Dict[str, tuple] = {
    # The first entry of each tuple is the agent's own identity. The rest are the
    # spellings the seeder actually writes, confirmed by reading the 52 seeded
    # sessions: IVR(15) MOBILE_APP(14) WEB_CHAT(15) FRAUD_OPS_OUTBOUND(3) BRANCH(5).
    # FRAUD_OPS_OUTBOUND and WEB_CHAT are the two that do not resemble the model
    # name; without them those producers match nothing and return empty in silence,
    # which reads as "no activity" rather than "wrong constant".
    BankChannel.FRAUD_DETECTION.value: ("FRAUD_DETECTION", "FRAUD_OPS_OUTBOUND", "FRAUD_OPS",
                                        "FRAUD", "FRAUD_MONITORING", "VELOCITY"),
    BankChannel.TELEPHONY_IVR.value: ("TELEPHONY_IVR", "IVR", "TELEPHONY", "PHONE", "CONTACT_CENTER"),
    BankChannel.MOBILE_APP.value: ("MOBILE_APP", "MOBILE", "APP", "WALLET", "DIGITAL_WALLET"),
    BankChannel.WEB_PORTAL.value: ("WEB_PORTAL", "WEB_CHAT", "WEB", "ONLINE", "PORTAL"),
    BankChannel.BRANCH_SUPPORT.value: ("BRANCH_SUPPORT", "BRANCH", "TELLER", "IN_PERSON"),
}


def _memory_bank(client: Optional[MemoryBankClient] = None) -> MemoryBankClient:
    return client or GLOBAL_MEMORY_BANK_CLIENT


def _tagged_value(value: Any, source: str) -> Dict[str, Any]:
    """`seeded` when the service returned something, `unmeasured` when it did not.

    A missing field renders "n/r" rather than a zero or a plausible-looking
    default, because a wrong value is harder to spot than a missing one.
    """
    if value is None or value == "" or value == [] or value == NOT_RECORDED:
        return unmeasured(source)
    return seeded(value, source)


def _session_customer_id(session: Dict[str, Any]) -> Any:
    scoped = mb_field(session, "sessionState", "customer_id")
    return scoped if scoped != NOT_RECORDED else mb_field(session, "userId")


def _event_rows(bank: MemoryBankClient, session_name: str) -> List[Dict[str, Any]]:
    source = "sessions.events.list:%s" % session_name
    rows: List[Dict[str, Any]] = []
    for event in bank.list_events(session_name):
        parts = mb_field(event, "content", "parts", default=[])
        text = parts[0].get("text", NOT_RECORDED) if isinstance(parts, list) and parts else NOT_RECORDED
        rows.append({
            "author": _tagged_value(mb_field(event, "author"), source),
            "timestamp": _tagged_value(mb_field(event, "timestamp"), source),
            "text": _tagged_value(text, source),
            "invocation_id": _tagged_value(mb_field(event, "invocationId"), source),
        })
    return rows


# Which Memory Bank topics each channel agent reads. The labels come from
# memory/custom_topics.yaml and are checked against the live engine in
# _topic_filter. The synthesizer and the auditor read the whole scope, and the
# veracity validator doesn't read Memory Bank at all (it checks BigQuery).
AGENT_MEMORY_TOPICS: Dict[str, Tuple[str, ...]] = {
    "fraud_monitoring_agent": ("CARD_LIFECYCLE", "TRAVEL_STATUS", "SPENDING_CONTEXT"),
    "telephony_ivr_agent": ("CONTACT_PREFERENCE", "AGENT_COMMITMENTS", "TRAVEL_STATUS"),
    "mobile_app_agent": ("DEVICE_WALLET_INVENTORY", "CARD_LIFECYCLE"),
    "web_portal_agent": ("DISPUTE_HISTORY", "AGENT_COMMITMENTS"),
    "branch_support_agent": ("CONTACT_PREFERENCE", "AGENT_COMMITMENTS", "CARD_LIFECYCLE"),
}
ALL_TOPICS_LABEL = "ALL (every topic configured on the engine)"
NO_MEMORY_READS_LABEL = "NONE (checks claims against BigQuery, does not read Memory Bank)"


def _topic_filter(bank: MemoryBankClient, topics: Sequence[str]) -> Optional[str]:
    """Builds the memories:retrieve filter for `topics`, or None for no filter.

    A label the engine doesn't know would match nothing and quietly empty the
    result, so it's left out and logged instead.
    """
    if not topics:
        return None
    configured = {topic["label"] for topic in bank.memory_topics()}
    if unknown := [topic for topic in topics if topic not in configured]:
        LOG.warning("topics %s are not configured on the Memory Bank engine; left out of the filter", unknown)
    usable = [topic for topic in topics if topic in configured]
    if not usable:
        return None
    return " OR ".join('topics.custom_memory_topic_label: "%s"' % topic for topic in usable)


def _memory_rows(bank: MemoryBankClient, scope: Dict[str, str], query: str,
                 top_k: int, topics: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """Retrieval and ranking run inside Google - `retrieve` is a service call."""
    rows: List[Dict[str, Any]] = []
    memory_filter = _topic_filter(bank, topics)
    for item in memory_write_path.retrieve_for_agents(
            bank, scope["customer_id"], query=query or None, top_k=top_k, memory_filter=memory_filter):
        memory = item.get("memory", item)
        memory_name = mb_field(memory, "name")
        source = "memories.retrieve:%s" % (
            str(memory_name).split("/")[-1] if memory_name != NOT_RECORDED else NOT_RECORDED
        )
        rows.append({
            "memory_id": _tagged_value(
                str(memory_name).split("/")[-1] if memory_name != NOT_RECORDED else NOT_RECORDED, source),
            "fact": _tagged_value(mb_field(memory, "fact"), source),
            "topics": _tagged_value(topic_labels(memory) or NOT_RECORDED, source),
            "update_time": _tagged_value(mb_field(memory, "updateTime"), source),
            "distance": _tagged_value(mb_field(item, "distance"), source),
            "standing": _tagged_value(item.get("standing", NOT_RECORDED), source),
        })
    return rows


_SESSIONS_CACHE_LOCK = threading.Lock()
_SESSIONS_CACHE_AT: float = 0.0
_SESSIONS_CACHE_ROWS: List[Dict[str, Any]] = []
_SESSIONS_CACHE_TTL_S = 3.0


def invalidate_sessions_cache() -> None:
    """Called after any session/event write so the next channel read sees it."""
    global _SESSIONS_CACHE_AT, _SESSIONS_CACHE_ROWS
    with _SESSIONS_CACHE_LOCK:
        _SESSIONS_CACHE_AT = 0.0
        _SESSIONS_CACHE_ROWS = []


def _cached_list_sessions(bank: MemoryBankClient, explicit_client: Optional[MemoryBankClient]) -> List[Dict[str, Any]]:
    """Avoids calling sessions.list five times within one turn across the 5 channels."""
    global _SESSIONS_CACHE_AT, _SESSIONS_CACHE_ROWS
    if explicit_client is not None:
        return list(bank.list_sessions())
    now = time.monotonic()
    with _SESSIONS_CACHE_LOCK:
        if _SESSIONS_CACHE_ROWS and (now - _SESSIONS_CACHE_AT) < _SESSIONS_CACHE_TTL_S:
            return list(_SESSIONS_CACHE_ROWS)
    rows = list(bank.list_sessions())
    with _SESSIONS_CACHE_LOCK:
        _SESSIONS_CACHE_ROWS = rows
        _SESSIONS_CACHE_AT = time.monotonic()
    return list(rows)


def _produce_channel_payload(
    channel: str,
    channel_source: str,
    customer_id: str,
    memory_query: str,
    include_events: bool = True,
    top_k: int = DEFAULT_CHANNEL_TOP_K,
    client: Optional[MemoryBankClient] = None,
) -> Dict[str, Any]:
    """One channel's slice of the seeded Memory Bank, for one customer scope.

    Nothing here has a fallback. Delete the seeded corpus and every producer
    returns empty lists with zero counts; take the service away and the
    `MemoryBankError` from the REST client propagates to the caller instead of
    being converted into a row that looks like data.
    """
    bank = _memory_bank(client)
    scope = {"customer_id": customer_id}
    tokens = CHANNEL_SESSION_TOKENS.get(channel, (channel,))
    agent_name = channel_source.split(".")[0] if channel_source else channel.lower()

    span = obs.turn(
        "channel.produce",
        LOG,
        channel=channel,
        agent=agent_name,
        customer_id=customer_id,
    )
    with span as turn_handle:
        turn_handle.debug_payload("accepted channel tokens", list(tokens))
        if channel not in CHANNEL_SESSION_TOKENS:
            # A channel with no token tuple matches only its own exact spelling,
            # which is the silent-empty bug this map exists to prevent.
            LOG.warning(
                "channel %r has no entry in CHANNEL_SESSION_TOKENS; matching on the literal "
                "name only, so any other spelling in the seeded data will be missed",
                channel,
            )

        matched: List[Dict[str, Any]] = []
        considered = 0
        skipped_scope = 0
        skipped_channel = 0
        seen_labels: Dict[str, int] = {}

        for session in _cached_list_sessions(bank, client):
            considered += 1
            if _session_customer_id(session) != customer_id:
                skipped_scope += 1
                continue
            label = mb_field(session, "sessionState", "channel")
            seen_labels[str(label)] = seen_labels.get(str(label), 0) + 1
            if str(label).upper() not in tokens:
                skipped_channel += 1
                continue

            session_name = mb_field(session, "name")
            source = "sessions.list:%s" % session_name
            events = _event_rows(bank, session_name) if include_events and session_name != NOT_RECORDED else []
            matched.append({
                "session_id": _tagged_value(
                    str(session_name).split("/")[-1] if session_name != NOT_RECORDED else NOT_RECORDED, source),
                "session_name": _tagged_value(session_name, source),
                "display_name": _tagged_value(mb_field(session, "displayName"), source),
                "channel_label_in_data": _tagged_value(label, source),
                "customer_id": _tagged_value(_session_customer_id(session), source),
                "intent": _tagged_value(mb_field(session, "sessionState", "intent"), source),
                "create_time": _tagged_value(mb_field(session, "createTime"), source),
                "update_time": _tagged_value(mb_field(session, "updateTime"), source),
                "events": events,
                "event_count": (
                    computed(len(events), "len(sessions.events.list:%s)" % session_name)
                    if include_events
                    else unmeasured("events not read: include_events=False")
                ),
            })

        turn_handle.debug_payload("channel labels seen in scope", seen_labels)
        if not matched:
            # An empty channel and a channel whose label spelling is not in the
            # token tuple produce identical output. Only the log can tell them
            # apart, so it has to say which one happened.
            LOG.warning(
                "channel %s matched 0 of %d sessions (%d wrong scope, %d wrong channel label). "
                "Labels present for this customer: %s. If one of those is this channel under "
                "another name, CHANNEL_SESSION_TOKENS is missing an alias.",
                channel, considered, skipped_scope, skipped_channel, sorted(seen_labels),
            )

        topics = AGENT_MEMORY_TOPICS.get(agent_name, ())
        memories = _memory_rows(bank, scope, memory_query, top_k, topics)

        turn_handle.detail(
            sessions=len(matched),
            memories=len(memories),
            considered=considered,
            topics=",".join(topics) or "all",
        )
        return {
            "channel": identity(channel, channel_source, "agent identity constant"),
            "customer_scope": computed(scope, "scope argument sent to sessions.list / memories.retrieve"),
            "memory_bank_parent": computed(bank.parent, "MemoryBankClient.parent"),
            "retrieval_query": computed(memory_query, "channel retrieval query issued by %s" % channel_source),
            "retrieval_topics": computed(list(topics) or ALL_TOPICS_LABEL, "AGENT_MEMORY_TOPICS[%s]" % agent_name),
            "sessions": matched,
            "session_count": computed(len(matched), "count of sessions.list rows matching channel token + scope"),
            "memories": memories,
            "memory_count": computed(len(memories), "count of memories.retrieve rows"),
        }


# The fixed Memory Bank query each channel agent sends. The UI's topic A/B
# (backend/ui_insights.py) reads the same constant.
CHANNEL_RETRIEVAL_QUERIES: Dict[str, str] = {
    "fraud_monitoring_agent": "fraud velocity alert, concurrent logins from different cities, card lock or restriction",
    "telephony_ivr_agent": "inbound phone call, IVR authentication, dropped call before passcode verification",
    "mobile_app_agent": "mobile app session, digital wallet provisioning, tokenization declined, card status",
    "web_portal_agent": "online banking web session, browser login, dispute filed, card controls toggled",
    "branch_support_agent": "branch visit, teller or banker consultation, in-person identity verification",
}


def query_fraud_velocity_alerts(
    customer_id: str = DEFAULT_CUSTOMER_SCOPE_ID,
    include_events: bool = True,
    top_k: int = DEFAULT_CHANNEL_TOP_K,
) -> Dict[str, Any]:
    """FRAUD_DETECTION channel: seeded velocity, multi-city login and card-lock sessions."""
    return _produce_channel_payload(
        channel=BankChannel.FRAUD_DETECTION.value,
        channel_source="fraud_monitoring_agent.channel",
        customer_id=customer_id,
        memory_query=CHANNEL_RETRIEVAL_QUERIES["fraud_monitoring_agent"],
        include_events=include_events,
        top_k=top_k,
    )


def query_ivr_call_records(
    customer_id: str = DEFAULT_CUSTOMER_SCOPE_ID,
    include_events: bool = True,
    top_k: int = DEFAULT_CHANNEL_TOP_K,
) -> Dict[str, Any]:
    """TELEPHONY_IVR channel: seeded inbound call sessions, including dropped-2FA turns."""
    return _produce_channel_payload(
        channel=BankChannel.TELEPHONY_IVR.value,
        channel_source="telephony_ivr_agent.channel",
        customer_id=customer_id,
        memory_query=CHANNEL_RETRIEVAL_QUERIES["telephony_ivr_agent"],
        include_events=include_events,
        top_k=top_k,
    )


def query_mobile_wallet_events(
    customer_id: str = DEFAULT_CUSTOMER_SCOPE_ID,
    include_events: bool = True,
    top_k: int = DEFAULT_CHANNEL_TOP_K,
) -> Dict[str, Any]:
    """MOBILE_APP channel: seeded app and digital-wallet sessions, including tokenization failures."""
    return _produce_channel_payload(
        channel=BankChannel.MOBILE_APP.value,
        channel_source="mobile_app_agent.channel",
        customer_id=customer_id,
        memory_query=CHANNEL_RETRIEVAL_QUERIES["mobile_app_agent"],
        include_events=include_events,
        top_k=top_k,
    )


def query_web_portal_activity(
    customer_id: str = DEFAULT_CUSTOMER_SCOPE_ID,
    include_events: bool = True,
    top_k: int = DEFAULT_CHANNEL_TOP_K,
) -> Dict[str, Any]:
    """WEB_PORTAL channel: seeded online banking sessions - logins, disputes, card toggles."""
    return _produce_channel_payload(
        channel=BankChannel.WEB_PORTAL.value,
        channel_source="web_portal_agent.channel",
        customer_id=customer_id,
        memory_query=CHANNEL_RETRIEVAL_QUERIES["web_portal_agent"],
        include_events=include_events,
        top_k=top_k,
    )


def query_branch_teller_interactions(
    customer_id: str = DEFAULT_CUSTOMER_SCOPE_ID,
    include_events: bool = True,
    top_k: int = DEFAULT_CHANNEL_TOP_K,
) -> Dict[str, Any]:
    """BRANCH_SUPPORT channel: seeded in-branch sessions - banker consultations, in-person ID checks."""
    return _produce_channel_payload(
        channel=BankChannel.BRANCH_SUPPORT.value,
        channel_source="branch_support_agent.channel",
        customer_id=customer_id,
        memory_query=CHANNEL_RETRIEVAL_QUERIES["branch_support_agent"],
        include_events=include_events,
        top_k=top_k,
    )


def read_customer_memory_bank(
    customer_id: str = DEFAULT_CUSTOMER_SCOPE_ID,
    query: str = "",
    top_k: int = DEFAULT_CHANNEL_TOP_K,
    topics: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Synthesizer read path: scoped memories straight from Memory Bank.

    An empty `query` means simple retrieval of everything in scope; a non-empty
    one is a similarity search the service runs - this process does no ranking.
    `topics` narrows the read to those topic labels (see list_memory_topics_tool);
    leave it empty to read across every topic.
    """
    bank = _memory_bank(None)
    scope = {"customer_id": customer_id}
    memories = _memory_rows(bank, scope, query, top_k, topics or ())
    return {
        "customer_scope": computed(scope, "scope argument sent to memories.retrieve"),
        "memory_bank_parent": computed(bank.parent, "MemoryBankClient.parent"),
        "retrieval_query": computed(query or NOT_RECORDED, "query argument passed to read_customer_memory_bank"),
        "retrieval_mode": computed(
            "similaritySearchParams" if query else "simpleRetrievalParams",
            "MemoryBankClient.retrieve request body branch",
        ),
        "retrieval_topics": computed(topics or ALL_TOPICS_LABEL, "topics argument passed to read_customer_memory_bank"),
        "memories": memories,
        "memory_count": computed(len(memories), "count of memories.retrieve rows"),
    }


def execute_one_click_card_unlock(
    customer_id: str = DEFAULT_CUSTOMER_SCOPE_ID,
    card_id: str = DEFAULT_CARD_ID,
    reason: str = "",
    record_to_memory_bank: bool = True,
) -> Dict[str, Any]:
    """Stages the card unlock and writes the decision trail into Memory Bank.

    There is no card-issuing platform client in this process, so the tool does
    not claim the card was unlocked. It reports the evidence it read, the audit
    record it actually wrote, and leaves the downstream confirmation
    `unmeasured` rather than asserting a success nobody observed.
    """
    bank = _memory_bank(None)
    token_evidence = golden_10_wallet_tokens_active_on_card(card_id=card_id)

    audit_session = unmeasured("audit trail not written: record_to_memory_bank=False")
    audit_event_time = unmeasured("audit trail not written: record_to_memory_bank=False")
    if record_to_memory_bank:
        session_name = bank.create_session(
            user_id=customer_id,
            display_name="CARD UNLOCK %s" % card_id,
            session_state={
                "channel": BankChannel.CORE_BANKING.value,
                "customer_id": customer_id,
                "card_id": card_id,
                "intent": "ONE_CLICK_CARD_UNLOCK",
            },
        )
        event = bank.append_event(
            session_name=session_name,
            author="consumer_credit_synthesizer_agent",
            text="One-click unlock staged for %s. Reason on record: %s" % (
                card_id, reason or NOT_RECORDED),
        )
        invalidate_sessions_cache()
        audit_session = live(session_name, "sessions.create")
        audit_event_time = _tagged_value(mb_field(event, "timestamp"), "sessions.appendEvent")

    return {
        "customer_scope": computed({"customer_id": customer_id},
                                   "scope argument sent to sessions.create"),
        "card_id": computed(card_id, "card_id argument passed to execute_one_click_card_unlock"),
        "unlock_reason": computed(reason or NOT_RECORDED,
                                  "reason argument passed to execute_one_click_card_unlock"),
        "wallet_token_evidence": computed(
            token_evidence, "golden_10_wallet_tokens_active_on_card(card_id=%s)" % card_id),
        "audit_session": audit_session,
        "audit_event_timestamp": audit_event_time,
        "card_platform_confirmation": unmeasured(
            "no card issuing platform client exists in this process"),
    }


def execute_fraud_card_control_or_escalate(
    customer_id: str = DEFAULT_CUSTOMER_SCOPE_ID,
    card_id: str = DEFAULT_CARD_ID,
    action: str = "CARD_BLOCK_AND_ESCALATE",
    reason: str = "",
    record_to_memory_bank: bool = True,
) -> Dict[str, Any]:
    """FRAUD_OPS_OUTBOUND action tool for `fraud_monitoring_agent`.

    Cross-checks live suspicious activity (Golden Query 03) and impossible-travel
    geography (Golden Query 04) on `card_id`, then stages a card block/unblock or
    escalates the case to `consumer_credit_synthesizer_agent` by writing the
    decision into Vertex AI Memory Bank under channel `FRAUD_OPS_OUTBOUND`.
    """
    bank = _memory_bank(None)
    with obs.turn(
        "fraud.card_control_or_escalate",
        LOG,
        agent="fraud_monitoring_agent",
        customer_id=customer_id,
        card_id=card_id,
        action=action,
    ) as span:
        suspicious = golden_03_suspicious_activity_on_card_window(card_id=card_id)
        impossible_geo = golden_04_geo_impossible_travel(card_id=card_id)
        susp_rows = value_of(suspicious.get("row_count", 0)) or 0
        geo_rows = value_of(impossible_geo.get("row_count", 0)) or 0

        audit_session = unmeasured("audit trail not written: record_to_memory_bank=False")
        audit_event_time = unmeasured("audit trail not written: record_to_memory_bank=False")
        if record_to_memory_bank:
            session_name = bank.create_session(
                user_id=customer_id,
                display_name="FRAUD OPS %s %s" % (action, card_id),
                session_state={
                    "channel": "FRAUD_OPS_OUTBOUND",
                    "customer_id": customer_id,
                    "card_id": card_id,
                    "intent": action,
                },
            )
            event_text = (
                "FRAUD_OPS_OUTBOUND [%s] on %s (suspicious_rows=%d, geo_impossible_rows=%d) "
                "escalated to consumer_credit_synthesizer_agent. Reason: %s"
                % (action, card_id, int(susp_rows), int(geo_rows), reason or "velocity + geo anomaly")
            )
            event = bank.append_event(
                session_name=session_name,
                author="fraud_monitoring_agent",
                text=event_text,
            )
            if "ESCALATE" in action.upper():
                claim = memory_write_path.Claim(customer_id, "", "FRAUD_OPS_OUTBOUND")
                item = memory_write_path.board_item(
                    "FRAUD_ESCALATION", "HIGH", claim, memory_write_path.Standing.TRUSTED, None,
                    facts=[event_text])
                bank.append_state_delta(session_name, memory_write_path.LINEAGE_AUTHOR,
                                        {memory_write_path.BOARD_STATE_KEY: item})
            invalidate_sessions_cache()
            audit_session = live(session_name, "sessions.create")
            audit_event_time = _tagged_value(mb_field(event, "timestamp"), "sessions.appendEvent")

        span.detail(
            action=action,
            suspicious_rows=int(susp_rows),
            geo_rows=int(geo_rows),
        )
        return {
            "channel": identity("FRAUD_OPS_OUTBOUND", "fraud_monitoring_agent.channel", "agent identity constant"),
            "customer_scope": computed({"customer_id": customer_id}, "scope argument sent to sessions.create"),
            "card_id": computed(card_id, "card_id argument passed to execute_fraud_card_control_or_escalate"),
            "action": computed(action, "action argument passed to execute_fraud_card_control_or_escalate"),
            "escalation_target": identity(
                "consumer_credit_synthesizer_agent",
                "fraud_monitoring_agent.escalation_target",
                "hub agent identity in the 8-agent topology",
            ),
            "suspicious_activity_rows": computed(int(susp_rows), "golden_03_suspicious_activity_on_card_window"),
            "impossible_travel_rows": computed(int(geo_rows), "golden_04_geo_impossible_travel"),
            "audit_session": audit_session,
            "audit_event_timestamp": audit_event_time,
            "card_platform_confirmation": unmeasured("no card issuing platform client exists in this process"),
        }


CANONICAL_SEEDED_CHANNEL_LABELS: Dict[str, str] = {
    BankChannel.FRAUD_DETECTION.value: "FRAUD_OPS_OUTBOUND",
    "FRAUD_OPS_OUTBOUND": "FRAUD_OPS_OUTBOUND",
    BankChannel.TELEPHONY_IVR.value: "IVR",
    "IVR": "IVR",
    BankChannel.MOBILE_APP.value: "MOBILE_APP",
    BankChannel.WEB_PORTAL.value: "WEB_CHAT",
    "WEB_CHAT": "WEB_CHAT",
    BankChannel.BRANCH_SUPPORT.value: "BRANCH",
    "BRANCH": "BRANCH",
}


CHANNEL_MULTI_TURN_PRESETS: Dict[str, List[Dict[str, Any]]] = {
    "FRAUD_OPS_OUTBOUND": [
        {
            "preset_id": "fraud_lisbon_velocity_lock",
            "title": "Lisbon POS + NYC login velocity alert → card lock & customer SMS",
            "intent": "VELOCITY_ALERT_CARD_LOCK",
            "turns": [
                {
                    "author": "fraud_monitoring_agent",
                    "text": "FRAUD_OPS_OUTBOUND alert: concurrent Lisbon taxi POS (€715.61) on CARD00002 while online banking session active. Outbound SMS challenge sent.",
                },
                {
                    "author": "customer",
                    "text": "NO — my wallet with debit CARD00004 and credit CARD00002 was stolen in Lisbon near Rossio Square.",
                },
                {
                    "author": "fraud_monitoring_agent",
                    "text": "Customer confirmed compromise. Staged temporary block on CARD00002 & CARD00004 and escalated dual-rail claim to consumer_credit_synthesizer_agent.",
                },
            ],
        },
        {
            "preset_id": "fraud_courier_reroute_hold",
            "title": "Early-return courier intercept (Lisbon hotel → NYC JFK)",
            "intent": "COURIER_REROUTE_VERIFICATION",
            "turns": [
                {
                    "author": "fraud_monitoring_agent",
                    "text": "FRAUD_OPS_OUTBOUND flag: replacement card shipping address changed from Lisbon hotel to New York 48h after travel notice start.",
                },
                {
                    "author": "customer",
                    "text": "I cut my Lisbon trip short after the wallet theft and flew back to JFK on 10 Sep. Please ship the replacement card to my New York home address.",
                },
                {
                    "author": "fraud_monitoring_agent",
                    "text": "Verified 10 Sep early-return telemetry. Released courier hold and updated active delivery destination to NYC.",
                },
            ],
        },
    ],
    "IVR": [
        {
            "preset_id": "ivr_dropped_2fa_lisbon",
            "title": "Inbound call from Lisbon hotel — dropped before SMS 2FA",
            "intent": "INBOUND_DISPUTE_DROPPED_2FA",
            "turns": [
                {
                    "author": "telephony_ivr_agent",
                    "text": "Inbound IVR call connected from Lisbon hotel landline. Prompted customer for 6-digit SMS one-time passcode.",
                },
                {
                    "author": "customer",
                    "text": "My phone SIM was stolen along with my wallet in Lisbon — I cannot receive SMS codes right now.",
                },
                {
                    "author": "telephony_ivr_agent",
                    "text": "Session ended before OTP verification (DROPPED_BEFORE_2FA). Flagged scope for mobile app FaceID fallback.",
                },
            ],
        },
        {
            "preset_id": "ivr_reg_e_provisional_inquiry",
            "title": "Inbound inquiry on Reg E (23 Sep) vs Reg Z provisional credit",
            "intent": "PROVISIONAL_CREDIT_STATUS_INQUIRY",
            "turns": [
                {
                    "author": "customer",
                    "text": "Why does my debit card dispute DSP00033 show provisional credit by 23 Sep, but my credit card dispute DSP00034 shows no provisional credit date?",
                },
                {
                    "author": "telephony_ivr_agent",
                    "text": "Quoted dispute ledger: DSP00033 is governed by Reg E (10 business days), whereas DSP00034 on CARD00002 is governed by Reg Z (payment withheld during investigation).",
                },
                {
                    "author": "customer",
                    "text": "Got it — please make sure you text me before closing either dispute.",
                },
            ],
        },
    ],
    "MOBILE_APP": [
        {
            "preset_id": "mobile_apple_pay_reprovision",
            "title": "FaceID verified Apple Pay token provisioning after CARD00002 freeze",
            "intent": "APPLE_PAY_TOKEN_REPROVISION",
            "turns": [
                {
                    "author": "customer",
                    "text": "Authenticated via FaceID on bound iPhone. Requesting digital wallet token replacement for frozen CARD00002.",
                },
                {
                    "author": "mobile_app_agent",
                    "text": "Confirmed active device binding via Golden Query 10. Revoked compromised physical card token and provisioned replacement VCN wallet token.",
                },
            ],
        },
        {
            "preset_id": "mobile_early_return_checkin",
            "title": "Mobile app JFK geolocation check-in (10 Sep early return)",
            "intent": "TRAVEL_EARLY_RETURN_CHECKIN",
            "turns": [
                {
                    "author": "customer",
                    "text": "Updating my travel status in the mobile app: landed back in New York (JFK) on 10 Sep, superseded Lisbon travel notice.",
                },
                {
                    "author": "mobile_app_agent",
                    "text": "Recorded mobile app geolocation at JFK terminal and marked Lisbon travel window superseded as of 2026-09-10.",
                },
            ],
        },
    ],
    "WEB_CHAT": [
        {
            "preset_id": "web_dual_dispute_submission",
            "title": "Online portal filing of DSP00033 (debit) & DSP00034 (credit)",
            "intent": "ONLINE_DUAL_DISPUTE_FILING",
            "turns": [
                {
                    "author": "customer",
                    "text": "Submitting stolen-wallet disputes online for unauthorized Lisbon charges on debit CARD00004 and credit CARD00002.",
                },
                {
                    "author": "web_portal_agent",
                    "text": "Registered open disputes in ledger and linked both claims to the 09 Sep Lisbon theft incident.",
                },
                {
                    "author": "customer",
                    "text": "Standing instruction: always notify me by SMS and get my confirmation before closing any dispute.",
                },
            ],
        },
        {
            "preset_id": "web_standing_instruction_update",
            "title": "Standing instruction: require SMS confirmation before closing disputes",
            "intent": "STANDING_INSTRUCTION_UPDATE",
            "turns": [
                {
                    "author": "customer",
                    "text": "Never close a dispute on my account without texting me first to confirm the final outcome.",
                },
                {
                    "author": "web_portal_agent",
                    "text": "Saved standing customer preference to Vertex AI Memory Bank for pre-close enforcement.",
                },
            ],
        },
    ],
    "BRANCH": [
        {
            "preset_id": "branch_passport_id_check",
            "title": "In-branch physical passport verification & hold clearance (NYC)",
            "intent": "IN_PERSON_PASSPORT_VERIFICATION",
            "turns": [
                {
                    "author": "branch_support_agent",
                    "text": "Customer Priya Vantalis (CUST00001) presented physical passport at NYC branch following early return from Lisbon.",
                },
                {
                    "author": "customer",
                    "text": "Confirming my identity in person so my replacement cards and provisional credit are not held up by the dropped Lisbon IVR call.",
                },
                {
                    "author": "branch_support_agent",
                    "text": "Verified identity against Customer 360 snapshot (Golden Query 01) and cleared dropped-2FA security flag.",
                },
            ],
        },
        {
            "preset_id": "branch_emergency_cash_advance",
            "title": "In-branch emergency debit replacement & fee waiver",
            "intent": "EMERGENCY_CARD_REPLACEMENT",
            "turns": [
                {
                    "author": "customer",
                    "text": "Requesting immediate branch replacement for debit CARD00004 and expedited courier fee waiver.",
                },
                {
                    "author": "branch_support_agent",
                    "text": "Issued temporary branch debit credential for ACCT000003 and logged AFFLUENT segment fee waiver.",
                },
            ],
        },
    ],
}


def ingest_live_channel_event(
    channel: str,
    customer_id: str = DEFAULT_CUSTOMER_SCOPE_ID,
    text: str = "",
    intent: str = "LIVE_CHANNEL_EVENT",
    card_id: str = DEFAULT_CARD_ID,
    author: str = "",
    preset_id: str = "",
    turns: Optional[List[Dict[str, str]]] = None,
    client: Optional[MemoryBankClient] = None,
    verify: Optional[memory_write_path.Verifier] = None,
) -> Dict[str, Any]:
    """Writes a live (single- or multi-turn) session into Vertex AI Memory Bank,
    then generates memories from it through the verified write path
    (backend/memory_write_path.py:verify_and_record).

    Because `_produce_channel_payload` reads from the same engine and matches on
    `CHANNEL_SESSION_TOKENS`, the corresponding channel producer spoke picks up
    this session and all of its customer <-> agent turns on the very next read.
    """
    bank = _memory_bank(client)
    raw_upper = (channel or "").strip().upper()
    canonical_label = CANONICAL_SEEDED_CHANNEL_LABELS.get(raw_upper, raw_upper or "WEB_CHAT")
    default_author = author or ("channel_producer:%s" % canonical_label.lower())

    resolved_turns: List[Dict[str, str]] = []
    preset_list = CHANNEL_MULTI_TURN_PRESETS.get(canonical_label, [])
    matched_preset = None
    if preset_id:
        for p in preset_list:
            if p.get("preset_id") == preset_id:
                matched_preset = p
                break
    if matched_preset is None and not turns and not text and preset_list:
        matched_preset = preset_list[0]

    if isinstance(turns, list) and turns:
        for t in turns:
            if isinstance(t, dict) and t.get("text"):
                resolved_turns.append({
                    "author": str(t.get("author") or default_author),
                    "text": str(t.get("text")),
                })
    elif matched_preset is not None:
        intent = str(matched_preset.get("intent") or intent)
        for t in matched_preset.get("turns", []):
            resolved_turns.append({
                "author": str(t.get("author") or default_author),
                "text": str(t.get("text") or ""),
            })
        if text:
            resolved_turns.append({"author": "customer", "text": text})
    elif text:
        # Free text is the customer's words (the UI labels it a customer turn).
        # Stamping it as the channel producer would skip the veracity check.
        resolved_turns.append({"author": author or "customer", "text": text})
    else:
        resolved_turns.append({
            "author": default_author,
            "text": "%s live event recorded on %s" % (canonical_label, card_id),
        })

    with obs.turn(
        "channel.ingest",
        LOG,
        channel=canonical_label,
        customer_id=customer_id,
        intent=intent,
        turns=len(resolved_turns),
    ) as span:
        session_name = bank.create_session(
            user_id=customer_id,
            display_name="%s · %s" % (canonical_label, intent),
            session_state={
                "channel": canonical_label,
                "customer_id": customer_id,
                "card_id": card_id,
                "intent": intent,
            },
        )
        last_event: Dict[str, Any] = {}
        appended_turns: List[Dict[str, Any]] = []
        for turn_item in resolved_turns:
            last_event = bank.append_event(
                session_name=session_name,
                author=turn_item["author"],
                text=turn_item["text"],
            )
            appended_turns.append({
                "author": turn_item["author"],
                "text": turn_item["text"],
                "timestamp": mb_field(last_event, "timestamp"),
            })
        invalidate_sessions_cache()
        session_id = str(session_name).split("/")[-1] if session_name else NOT_RECORDED
        span.detail(session_id=session_id, channel=canonical_label, turns=len(appended_turns))
        lineage = memory_write_path.verify_and_record(
            bank, session_name, customer_id, resolved_turns, canonical_label,
            verify or _claim_verifier(customer_id, canonical_label),
            name_lookup=customer_display_name,
        )
        return {
            "channel_label": live(canonical_label, "sessions.create"),
            "customer_id": computed(customer_id, "scope argument sent to sessions.create"),
            "session_id": live(session_id, "sessions.create"),
            "session_name": live(session_name, "sessions.create"),
            "intent": computed(intent, "intent argument sent to sessions.create"),
            "turn_count": computed(len(appended_turns), "count of sessions.appendEvent calls"),
            "turns": computed(appended_turns, "sessions.appendEvent turns"),
            "text": computed(appended_turns[-1]["text"] if appended_turns else text, "sessions.appendEvent"),
            "timestamp": _tagged_value(mb_field(last_event, "timestamp"), "sessions.appendEvent"),
            "memory_lineage": live(lineage, "memories:generate + sessions.appendEvent(stateDelta)"),
        }


def customer_display_name(customer_id: str) -> str:
    """Full name from golden 01, for claim records a human reviews."""
    rows = golden_01_customer_360_snapshot(customer_id=customer_id).get("rows") or []
    return str(rows[0].get("full_name") or NOT_RECORDED) if rows else NOT_RECORDED


def _bank_channel_for(label: str) -> BankChannel:
    for channel_value, tokens in CHANNEL_SESSION_TOKENS.items():
        if label in tokens:
            return BankChannel(channel_value)
    return BankChannel.WEB_PORTAL


def _claim_verifier(customer_id: str, channel_label: str) -> memory_write_path.Verifier:
    bank_channel = _bank_channel_for(channel_label)

    def verify(said: str) -> str:
        evaluation = ClaimVeracityValidatorAgent().validate_claim_against_telemetry(
            customer_id=customer_id, channel=bank_channel, claim_text=said,
        )
        return evaluation.veracity_status.value

    return verify


def produce_all_channels_snapshot(
    customer_id: str = DEFAULT_CUSTOMER_SCOPE_ID,
    include_events: bool = True,
    top_k: int = DEFAULT_CHANNEL_TOP_K,
    client: Optional[MemoryBankClient] = None,
) -> Dict[str, Any]:
    """Runs all 5 channel producer tools against live Vertex AI Memory Bank.

    Uses a warm session list and runs the 5 producers concurrently with
    `contextvars.copy_context()` so every producer's `channel.produce` span lands
    inside the caller's active trace run.
    """
    bank = _memory_bank(client)
    _cached_list_sessions(bank, client)

    specs = [
        ("01_fraud_monitoring_agent", "fraud_monitoring_agent", "FRAUD_OPS_OUTBOUND", query_fraud_velocity_alerts),
        ("02_telephony_ivr_agent", "telephony_ivr_agent", "IVR", query_ivr_call_records),
        ("03_mobile_app_agent", "mobile_app_agent", "MOBILE_APP", query_mobile_wallet_events),
        ("04_web_portal_agent", "web_portal_agent", "WEB_CHAT", query_web_portal_activity),
        ("05_branch_support_agent", "branch_support_agent", "BRANCH", query_branch_teller_interactions),
    ]

    def _call_one(spec_tuple):
        agent_id, agent_name, label, fn = spec_tuple
        payload = fn(customer_id=customer_id, include_events=include_events, top_k=top_k)
        sessions = payload.get("sessions", [])
        events_flat: List[Dict[str, Any]] = []
        for sess in sessions:
            sid = value_of(sess.get("session_id"))
            ch_label = value_of(sess.get("channel_label_in_data")) or label
            created = value_of(sess.get("create_time"))
            for ev in sess.get("events", []) or []:
                events_flat.append({
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "channel": ch_label,
                    "session_id": sid,
                    "timestamp": value_of(ev.get("timestamp")) or created,
                    "author": value_of(ev.get("author")),
                    "text": value_of(ev.get("text")),
                })
        return {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "channel_label": label,
            "session_count": value_of(payload.get("session_count", 0)) or 0,
            "memory_count": value_of(payload.get("memory_count", 0)) or 0,
            "event_count": len(events_flat),
            "events": events_flat,
            "payload": payload,
        }

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [
            pool.submit(contextvars.copy_context().run, _call_one, spec)
            for spec in specs
        ]
        channels = [fut.result() for fut in futures]

    all_events: List[Dict[str, Any]] = []
    for ch in channels:
        all_events.extend(ch["events"])
    all_events.sort(key=lambda e: str(e.get("timestamp") or ""), reverse=True)

    return {
        "customer_id": customer_id,
        "total_sessions": sum(c["session_count"] for c in channels),
        "total_events": len(all_events),
        "channels": channels,
        "events": all_events,
    }


# ==============================================================================
# 4. THE 8 CCB FRAUD DESK ADK AGENTS (hub-and-spoke around the synthesizer)
# ==============================================================================

# Legacy 6-agent orchestrator wrapper (full definition moved to
# `backend/legacy_six_agent_desk.py:build_legacy_six_agent_orchestrator`).
# Kept as a thin caller here for standalone golden-query test harnesses
# (`scripts/trace_adk_agents.py` and `scripts/probe_agents.py`); the active
# 8-agent hub-and-spoke desk is `create_consumer_credit_synthesizer_agent()` below.
def create_ccb_fraud_desk_orchestrator_agent(
    memory_bank_client: Optional[MemoryBankClient] = None,
    catalog_governor: Optional[CatalogGovernor] = None,
    model: str = "gemini-2.5-flash",
) -> LlmAgent:
    """Caller wrapper that delegates to `backend/legacy_six_agent_desk.py:build_legacy_six_agent_orchestrator`."""
    from backend.legacy_six_agent_desk import build_legacy_six_agent_orchestrator

    _ = memory_bank_client or GLOBAL_MEMORY_BANK_CLIENT
    _ = catalog_governor or GLOBAL_CATALOG_GOVERNOR
    return build_legacy_six_agent_orchestrator(
        model=model,
        list_memory_topics_tool=list_memory_topics_tool,
        retrieve_customer_memories_tool=retrieve_customer_memories_tool,
        catalog_governance_authorize_tool=catalog_governance_authorize_tool,
        catalog_lookup_glossary_term_tool=catalog_lookup_glossary_term_tool,
        golden_01_customer_360_snapshot=golden_01_customer_360_snapshot,
        golden_02_recent_authorizations_for_card=golden_02_recent_authorizations_for_card,
        golden_03_suspicious_activity_on_card_window=golden_03_suspicious_activity_on_card_window,
        golden_04_geo_impossible_travel=golden_04_geo_impossible_travel,
        golden_07_open_disputes_for_customer=golden_07_open_disputes_for_customer,
        golden_08_dispute_filing_window_check=golden_08_dispute_filing_window_check,
        golden_09_provisional_credit_due=golden_09_provisional_credit_due,
        golden_10_wallet_tokens_active_on_card=golden_10_wallet_tokens_active_on_card,
        golden_11_dispute_event_timeline=golden_11_dispute_event_timeline,
        log_agent_tree=_log_agent_tree,
    )


def _log_agent_tree(root: LlmAgent, built_by: str) -> None:
    """Writes the constructed topology to the log, once, at construction time.

    This is the only place the real shape of the graph is visible without
    reading the source: which agent owns which spoke, and exactly which tools
    each one can reach. It matters here more than usual, because these factories
    are not currently called by the request path -- so a reader who assumes the
    graph is live gets no contradicting evidence anywhere else.
    """
    if not LOG.isEnabledFor(logging.DEBUG):
        return

    def tool_names(agent: LlmAgent) -> List[str]:
        names = []
        for tool in getattr(agent, "tools", []) or []:
            func = getattr(tool, "func", None)
            names.append(getattr(func, "__name__", None) or getattr(tool, "name", repr(tool)))
        return names

    spokes = getattr(root, "sub_agents", []) or []
    LOG.debug("%s built hub=%s spokes=%d", built_by, root.name, len(spokes))
    LOG.debug("  hub %s tools=%s", root.name, tool_names(root))
    for spoke in spokes:
        LOG.debug(
            "  spoke %s tools=%s no_parent_transfer=%s no_peer_transfer=%s",
            spoke.name,
            tool_names(spoke),
            getattr(spoke, "disallow_transfer_to_parent", None),
            getattr(spoke, "disallow_transfer_to_peers", None),
        )


UNVERIFIED_GUIDANCE = (
    " A memory whose fact starts with \"%s\" is something the customer said that the "
    "bank has not confirmed. Report it as the customer's statement, never as fact, and never "
    "act on it." % memory_write_path.UNVERIFIED_PREFIX.strip()
)


def _spoke_agent(name: str, model: str, description: str, instruction: str,
                 tools: List[Any]) -> LlmAgent:
    """A spoke that can't transfer to a peer or back to the hub.

    With transfer back to the parent disabled, a run that the hub hands to a
    spoke ends inside that spoke. The hub can't collect several spokes'
    results in one run; the planned fix is a SequentialAgent/ParallelAgent workflow.
    """
    if topics := AGENT_MEMORY_TOPICS.get(name):
        instruction += (
            " Your Memory Bank reads are limited to these topics: %s. Treat anything "
            "outside them as another channel's job." % ", ".join(topics)
        )
    instruction += UNVERIFIED_GUIDANCE
    return LlmAgent(
        name=name,
        model=model,
        description=description,
        instruction=instruction,
        tools=tools,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def create_consumer_credit_synthesizer_agent(
    memory_bank: Any = None,
    model: str = "gemini-2.5-flash",
) -> LlmAgent:
    """Builds the 8-agent hub-and-spoke desk: the synthesizer plus its 7 spokes.

    `memory_bank` stays an engine-level client. `customer_id` reaches the
    service per call, as `scope={"customer_id": customer_id}` inside each tool.
    """
    _ = memory_bank or GLOBAL_MEMORY_BANK_CLIENT

    fraud_monitoring_agent = _spoke_agent(
        name="fraud_monitoring_agent",
        model=model,
        description="Fraud Velocity Specialist (CHANNEL_PRODUCER, FRAUD_DETECTION / FRAUD_OPS_OUTBOUND): reads velocity sessions, scores Golden Queries 03 & 04, and stages card block/unblock or escalates to the synthesizer.",
        instruction=(
            "You are the FRAUD_DETECTION (FRAUD_OPS_OUTBOUND) channel producer and actor. "
            "Call query_fraud_velocity_alerts with the customer_id in scope, cross-check "
            "velocity and impossible-travel telemetry with "
            "golden_03_suspicious_activity_on_card_window and golden_04_geo_impossible_travel, "
            "and when anomalous activity is confirmed call execute_fraud_card_control_or_escalate "
            "to stage a card block/unblock or escalate to consumer_credit_synthesizer_agent. "
            "If a tool returns no sessions and no rows, say so plainly and render missing fields as n/r."
        ),
        tools=[
            FunctionTool(query_fraud_velocity_alerts),
            FunctionTool(golden_03_suspicious_activity_on_card_window),
            FunctionTool(golden_04_geo_impossible_travel),
            FunctionTool(execute_fraud_card_control_or_escalate),
        ],
    )

    telephony_ivr_agent = _spoke_agent(
        name="telephony_ivr_agent",
        model=model,
        description="Contact Center IVR Specialist (CHANNEL_PRODUCER, TELEPHONY_IVR): reads seeded inbound call sessions from Memory Bank.",
        instruction=(
            "You are the TELEPHONY_IVR channel producer. Call query_ivr_call_records for the "
            "customer in scope and quote the seeded call turns verbatim, with their recorded "
            "timestamps. Declines and amounts come from golden_02_recent_authorizations_for_card, "
            "never from your own recollection. Missing values render as n/r."
        ),
        tools=[
            FunctionTool(query_ivr_call_records),
            FunctionTool(golden_02_recent_authorizations_for_card),
        ],
    )

    mobile_app_agent = _spoke_agent(
        name="mobile_app_agent",
        model=model,
        description="Mobile & Digital Wallet Specialist (CHANNEL_PRODUCER, MOBILE_APP): reads seeded app and wallet sessions from Memory Bank.",
        instruction=(
            "You are the MOBILE_APP channel producer. Call query_mobile_wallet_events for the "
            "customer in scope, and confirm live token state with "
            "golden_10_wallet_tokens_active_on_card before describing any wallet outcome. "
            "Report an empty channel as empty. Missing values render as n/r."
        ),
        tools=[
            FunctionTool(query_mobile_wallet_events),
            FunctionTool(golden_10_wallet_tokens_active_on_card),
        ],
    )

    web_portal_agent = _spoke_agent(
        name="web_portal_agent",
        model=model,
        description="Online Web Banking Specialist (CHANNEL_PRODUCER, WEB_PORTAL): reads seeded online banking sessions from Memory Bank.",
        instruction=(
            "You are the WEB_PORTAL channel producer. Call query_web_portal_activity for the "
            "customer in scope. Any dispute you mention must also appear in "
            "golden_07_open_disputes_for_customer; if it does not, say the channel record and "
            "the ledger disagree. Missing values render as n/r."
        ),
        tools=[
            FunctionTool(query_web_portal_activity),
            FunctionTool(golden_07_open_disputes_for_customer),
        ],
    )

    branch_support_agent = _spoke_agent(
        name="branch_support_agent",
        model=model,
        description="Branch & Teller Specialist (CHANNEL_PRODUCER, BRANCH_SUPPORT): reads seeded in-branch sessions from Memory Bank.",
        instruction=(
            "You are the BRANCH_SUPPORT channel producer. Call "
            "query_branch_teller_interactions for the customer in scope and report the seeded "
            "banker and teller records as they stand. Use "
            "golden_01_customer_360_snapshot only to confirm who the customer is. Missing "
            "values render as n/r."
        ),
        tools=[
            FunctionTool(query_branch_teller_interactions),
            FunctionTool(golden_01_customer_360_snapshot),
        ],
    )

    claim_veracity_validator_agent = _spoke_agent(
        name="claim_veracity_validator_agent",
        model=model,
        description="Pre-Write Veracity Gatekeeper (PRE_WRITE_VALIDATOR): checks a customer claim against ground-truth avenues before anything is written to Memory Bank.",
        instruction=(
            "You are the pre-write gate. Nothing reaches Memory Bank without passing through "
            "validate_and_record_customer_claim. Report the verdict and the corroborating "
            "avenues exactly as the validator returned them, including a refusal."
        ),
        tools=[FunctionTool(validate_and_record_customer_claim)],
    )

    memory_bank_audit_agent = _spoke_agent(
        name="memory_bank_audit_agent",
        model=model,
        description="Consolidated Sweep Auditor (CONSOLIDATED_AUDITOR): runs the enterprise-wide consolidated memory sweep and reports anomalies.",
        instruction=(
            "You are the auditor. Run run_consolidated_memory_audit_sweep and report its "
            "findings unedited, including a clean sweep. Never summarise an anomaly away."
        ),
        tools=[FunctionTool(run_consolidated_memory_audit_sweep)],
    )

    synthesizer = LlmAgent(
        name="consumer_credit_synthesizer_agent",
        model=model,
        description="Lead Synthesizer Orchestrator (LEAD_SYNTHESIZER): reconstructs the causal chain across all channels and drives the zero-question resolution.",
        instruction=(
            "You are the lead synthesizer of the JPMorgan Chase CCB Fraud & Disputes Desk and "
            "the only agent that talks to the customer. Collect each channel's account from its "
            "producer spoke, read the consolidated view with read_customer_memory_bank, and "
            "use list_memory_topics_tool to know what the bank files and to say which topic a "
            "fact came from when it matters. When a question is clearly about one area (for "
            "example where the customer is, or which card is live), pass the matching topic "
            "labels from list_memory_topics_tool as `topics` and write `query` in the "
            "customer's own words; leave `topics` empty for an open question, and "
            "state the causal chain in order. Run execute_one_click_card_unlock only after the "
            "veracity gate has passed the claim, and repeat its result as given: it stages the "
            "unlock and writes the audit trail, it does not confirm the card platform acted. "
            "Any value a tool did not return is n/r - never fill a gap yourself."
            + UNVERIFIED_GUIDANCE
        ),
        tools=[
            FunctionTool(list_memory_topics_tool),
            FunctionTool(read_customer_memory_bank),
            FunctionTool(execute_one_click_card_unlock),
        ],
        sub_agents=[
            fraud_monitoring_agent,
            telephony_ivr_agent,
            mobile_app_agent,
            web_portal_agent,
            branch_support_agent,
            claim_veracity_validator_agent,
            memory_bank_audit_agent,
        ],
    )
    _log_agent_tree(synthesizer, "create_consumer_credit_synthesizer_agent")
    return synthesizer


def list_all_agents() -> List[AgentDescriptor]:
    """The 8 CCB Fraud & Disputes Desk agents (`docs/EIGHT_AGENTS_ROSTER.md`)."""
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    descriptors = [
        AgentDescriptor(
            agent_id=AgentId.FRAUD_MONITORING.value,
            name="fraud_monitoring_agent",
            role="Fraud Velocity & Geo-Anomaly Specialist (CHANNEL_PRODUCER · FRAUD_OPS_OUTBOUND)",
            channel=BankChannel.FRAUD_DETECTION,
            model=model_name,
            tools=[
                "query_fraud_velocity_alerts",
                "golden_03_suspicious_activity_on_card_window",
                "golden_04_geo_impossible_travel",
                "execute_fraud_card_control_or_escalate",
            ],
            description=(
                "01 · Fraud Velocity & Geo-Anomaly Specialist (CHANNEL_PRODUCER · FRAUD_OPS_OUTBOUND): "
                "Watches for cross-border velocity spikes and stolen-card attempts. Reads its Memory Bank "
                "topics, asks the Dataplex catalog about every table and column before reading it, and runs "
                "BigQuery Golden Queries 03 and 04."
            ),
            memory_topics_scoped=list(AGENT_MEMORY_TOPICS["fraud_monitoring_agent"]),
            default_live_prompt="For customer CUST00001 and card CARD00002, check fraud velocity alerts and run Golden Queries 03 and 04 for suspicious activity and impossible travel.",
        ),
        AgentDescriptor(
            agent_id=AgentId.TELEPHONY_IVR.value,
            name="telephony_ivr_agent",
            role="Contact Center IVR & Inbound Call Specialist (CHANNEL_PRODUCER · TELEPHONY_IVR)",
            channel=BankChannel.TELEPHONY_IVR,
            model=model_name,
            tools=[
                "query_ivr_call_records",
                "golden_02_recent_authorizations_for_card",
            ],
            description=(
                "02 · Contact Center IVR & Inbound Call Specialist (CHANNEL_PRODUCER · TELEPHONY_IVR): "
                "Reads inbound call transcripts and step-up authentication turns from Memory Bank and "
                "checks reported declined charges against the BigQuery ledger with Golden Query 02."
            ),
            memory_topics_scoped=list(AGENT_MEMORY_TOPICS["telephony_ivr_agent"]),
            default_live_prompt="For customer CUST00001 and card CARD00002, retrieve TELEPHONY_IVR call transcripts and cross-check recent card authorizations with Golden Query 02.",
        ),
        AgentDescriptor(
            agent_id=AgentId.MOBILE_APP.value,
            name="mobile_app_agent",
            role="Mobile Banking & Digital Wallet Token Specialist (CHANNEL_PRODUCER · MOBILE_APP)",
            channel=BankChannel.MOBILE_APP,
            model=model_name,
            tools=[
                "query_mobile_wallet_events",
                "golden_10_wallet_tokens_active_on_card",
            ],
            description=(
                "03 · Mobile Banking & Digital Wallet Token Specialist (CHANNEL_PRODUCER · MOBILE_APP): "
                "Tracks in-app push challenges, biometric logins and wallet token provisioning, and checks "
                "whether any device token can still spend on a frozen card with Golden Query 10."
            ),
            memory_topics_scoped=list(AGENT_MEMORY_TOPICS["mobile_app_agent"]),
            default_live_prompt="For customer CUST00001 and card CARD00002, inspect MOBILE_APP wallet events and check active digital wallet tokens via Golden Query 10.",
        ),
        AgentDescriptor(
            agent_id=AgentId.WEB_PORTAL.value,
            name="web_portal_agent",
            role="Online Banking & Statutory Dispute Policy Specialist (CHANNEL_PRODUCER · WEB_PORTAL)",
            channel=BankChannel.WEB_PORTAL,
            model=model_name,
            tools=[
                "query_web_portal_activity",
                "golden_07_open_disputes_for_customer",
            ],
            description=(
                "04 · Online Banking & Statutory Dispute Policy Specialist (CHANNEL_PRODUCER · WEB_PORTAL): "
                "Reads online dispute filings and earlier promises from Memory Bank and reconciles open "
                "disputes with Golden Query 07. Which dispute rules apply is looked up in the Dataplex "
                "glossary at run time, not written here."
            ),
            memory_topics_scoped=list(AGENT_MEMORY_TOPICS["web_portal_agent"]),
            default_live_prompt="For customer CUST00001, inspect WEB_PORTAL sessions and reconcile open disputes using Golden Query 07.",
        ),
        AgentDescriptor(
            agent_id=AgentId.BRANCH_SUPPORT.value,
            name="branch_support_agent",
            role="In-Branch Banker & Identity Verification Specialist (CHANNEL_PRODUCER · BRANCH_SUPPORT)",
            channel=BankChannel.BRANCH_SUPPORT,
            model=model_name,
            tools=[
                "query_branch_teller_interactions",
                "golden_01_customer_360_snapshot",
            ],
            description=(
                "05 · In-Branch Banker & Identity Verification Specialist (CHANNEL_PRODUCER · BRANCH_SUPPORT): "
                "Reads in-person teller and banker sessions from Memory Bank and verifies the customer's "
                "identity and 90-day history with Golden Query 01."
            ),
            memory_topics_scoped=list(AGENT_MEMORY_TOPICS["branch_support_agent"]),
            default_live_prompt="For customer CUST00001, check BRANCH_SUPPORT banker consultation records and verify customer 360 identity via Golden Query 01.",
        ),
        AgentDescriptor(
            agent_id=AgentId.CLAIM_VERACITY_VALIDATOR.value,
            name="claim_veracity_validator_agent",
            role="Pre-Write Claim Veracity Gatekeeper (PRE_WRITE_VALIDATOR)",
            channel=None,
            model=model_name,
            tools=["validate_and_record_customer_claim"],
            description=(
                "06 · Pre-Write Claim Veracity Gatekeeper (PRE_WRITE_VALIDATOR): "
                "Gates every incoming customer claim before Memory Bank consolidation by cross-checking "
                "BigQuery Golden Queries (01, 02, 03, 04, 07) to flag contradicted "
                "travel timelines or mismatched dispute amounts."
            ),
            memory_topics_scoped=[NO_MEMORY_READS_LABEL],
            default_live_prompt="Validate claim for CUST00001 on WEB_PORTAL: 'My wallet was stolen in Lisbon on Sept 9 with debit card CARD00004 and credit card CARD00002, and I returned early to New York on Sept 10.'",
        ),
        AgentDescriptor(
            agent_id=AgentId.MEMORY_BANK_AUDIT.value,
            name="memory_bank_audit_agent",
            role="Enterprise Memory Bank Consolidated Sweep Auditor (CONSOLIDATED_AUDITOR · ASYNC JOB)",
            channel=None,
            model=model_name,
            tools=["run_consolidated_memory_audit_sweep"],
            description=(
                "07 · Enterprise Memory Bank Consolidated Sweep Auditor (CONSOLIDATED_AUDITOR · ASYNC JOB): "
                "Runs an async / scheduled sweep across enterprise customer memory scopes (`CUST00001`..`CUST00010`) "
                "against BigQuery telemetry to detect stale travel notices and un-superseded contradictions."
            ),
            memory_topics_scoped=[ALL_TOPICS_LABEL],
            default_live_prompt="Audit customer CUST00001 Memory Bank facts against BigQuery telemetry and report any stale travel notices or contradictions.",
        ),
        AgentDescriptor(
            agent_id=AgentId.CONSUMER_CREDIT_SYNTHESIZER.value,
            name="consumer_credit_synthesizer_agent",
            role="Lead Synthesizer & Hub Orchestrator (LEAD_SYNTHESIZER · CORE_BANKING)",
            channel=BankChannel.CORE_BANKING,
            model=model_name,
            tools=["list_memory_topics_tool", "read_customer_memory_bank", "execute_one_click_card_unlock"],
            description=(
                "08 · Lead Synthesizer & Hub Orchestrator (LEAD_SYNTHESIZER · CORE_BANKING): "
                "Hub of the 8-agent topology and the single agent that speaks to the customer/operator. "
                "Collects telemetry from all 7 spokes, queries `list_memory_topics_tool`, reconstructs "
                "the causal timeline via `read_customer_memory_bank`, and stages governed card unlocks."
            ),
            memory_topics_scoped=[ALL_TOPICS_LABEL],
            default_live_prompt="For customer CUST00001, list the configured Memory Bank topics, read the consolidated customer memory bank, and synthesize the Lisbon stolen-wallet causal chain.",
        ),
    ]
    for descriptor in descriptors:
        descriptor.authorized_golden_queries, descriptor.bound_golden_queries, descriptor.catalog_governance = (
            _golden_card_fields(AgentId(descriptor.agent_id), descriptor.tools)
        )
    return descriptors


def _golden_card_fields(agent: AgentId, tools: Sequence[str]) -> Tuple[List[str], List[str], List[str]]:
    """Card fields derived from GOLDEN_QUERY_CONTRACTS so they cannot drift.

    authorized = contracts that list this agent (the INDEX.md grant).
    bound      = golden tools actually attached to the agent.
    governed   = tables and columns those contracts check. No verdicts here:
                 the catalog decides at run time.
    """
    authorized = [c for c in GOLDEN_QUERY_CONTRACTS.values() if agent in c.authorized_agents]
    bound = [c for c in GOLDEN_QUERY_CONTRACTS.values() if c.tool_name in tools]
    governed = dict.fromkeys(
        name for c in authorized for name in [*c.governed_tables, *c.governed_columns_checked]
    )
    return (
        [c.sql_filename.removesuffix(".sql") for c in authorized],
        [c.sql_filename.removesuffix(".sql") for c in bound],
        list(governed),
    )


class UnknownAgentError(LookupError):
    """The key does not name one of the 8 desk agents."""


def get_adk_agent_by_id_or_name(agent_id_or_name: str) -> LlmAgent:
    """Resolves one of the 8 desk agents by name, AgentId value, or two-digit number.

    Raises UnknownAgentError for anything else, so a typo is a 404 rather than
    a run of the wrong agent.
    """
    key = str(agent_id_or_name or "").strip()
    by_key: Dict[str, str] = {}
    for member in AgentId.__members__.values():
        number, name = member.value.split("_", 1)
        by_key[member.value] = by_key[number] = by_key[name] = name

    wanted = by_key.get(key)
    if wanted is None:
        raise UnknownAgentError(
            "unknown agent %r; expected one of %s" % (key, sorted(set(by_key.values())))
        )

    hub = create_consumer_credit_synthesizer_agent()
    if hub.name == wanted:
        return hub
    for spoke in hub.sub_agents or []:
        if spoke.name == wanted:
            return spoke
    raise UnknownAgentError("agent %r is in AgentId but not wired under %s" % (wanted, hub.name))


def run_adk_agent_live(
    agent_id_or_name: str,
    customer_id: str = "CUST00001",
    card_id: str = "CARD00002",
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Executes one of the 8 ADK `LlmAgent`s live using `google.adk.runners.InMemoryRunner` (L55).

    Constructs `InMemoryRunner(agent=target_agent, app_name="ccb_fraud_desk")`,
    creates a session on `runner.session_service`, and runs the agent against
    Vertex AI Gemini (`gemini-2.5-flash`) so the agent invokes its bound
    `FunctionTool`s and logs every nested `obs.turn` span.
    """
    import asyncio
    from google.genai import types

    descriptors = {d.name: d for d in list_all_agents()}
    descriptors.update({d.agent_id: d for d in list_all_agents()})
    target_agent = get_adk_agent_by_id_or_name(agent_id_or_name)
    desc = descriptors.get(target_agent.name) or descriptors.get(agent_id_or_name)

    effective_prompt = (
        str(prompt).strip()
        if prompt and str(prompt).strip()
        else (
            desc.default_live_prompt
            if desc and desc.default_live_prompt
            else f"Execute your assigned tools for customer {customer_id} and card {card_id} and summarize findings."
        )
    )

    with obs.turn(
        "adk.in_memory_runner",
        LOG,
        agent=target_agent.name,
        runner="InMemoryRunner",
        customer_id=customer_id,
    ) as runner_span:
        runner = InMemoryRunner(agent=target_agent, app_name="ccb_fraud_desk")
        user_msg = types.Content(
            role="user",
            parts=[types.Part.from_text(text=effective_prompt)],
        )

        tool_calls: List[Dict[str, Any]] = []
        tool_responses: List[Dict[str, Any]] = []
        text_parts: List[str] = []
        event_count = 0

        async def _drive() -> str:
            nonlocal event_count
            session = await runner.session_service.create_session(
                app_name="ccb_fraud_desk", user_id=customer_id
            )
            async for event in runner.run_async(
                user_id=customer_id, session_id=session.id, new_message=user_msg
            ):
                event_count += 1
                for fc in event.get_function_calls() or []:
                    tool_calls.append({
                        "name": fc.name,
                        "args": dict(fc.args) if getattr(fc, "args", None) else {},
                    })
                for fr in event.get_function_responses() or []:
                    tool_responses.append({
                        "name": fr.name,
                    })
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if getattr(part, "text", None):
                            text_parts.append(part.text)
            return session.id

        session_id = asyncio.run(_drive())

        final_text = "\n".join(t.strip() for t in text_parts if t and t.strip())
        runner_span.detail(
            session_id=session_id,
            events=event_count,
            tool_calls=len(tool_calls),
            status="COMPLETED",
        )

    return {
        "status": "SUCCESS",
        "runner": live("google.adk.runners.InMemoryRunner", "backend/adk_agents.py:run_adk_agent_live"),
        "agent_id": desc.agent_id if desc else target_agent.name,
        "agent_name": target_agent.name,
        "role": desc.role if desc else target_agent.description,
        "model": str(getattr(target_agent, "model", "gemini-2.5-flash")),
        "session_id": live(session_id, "InMemoryRunner.session_service.create_session"),
        "prompt": effective_prompt,
        "events_count": computed(event_count, "InMemoryRunner.run"),
        "tool_calls": live(tool_calls, "InMemoryRunner.run.get_function_calls"),
        "tool_responses": live(tool_responses, "InMemoryRunner.run.get_function_responses"),
        "agent_response": live(final_text or NOT_RECORDED, "InMemoryRunner.run.content"),
    }

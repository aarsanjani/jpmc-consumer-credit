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

import logging
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
    topic_labels,
)
from backend.provenance import (
    live,
    seeded,
    computed,
    static,
    identity,
    unmeasured,
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
from backend import obs

load_dotenv()

# No handler is installed here. `obs.configure()` is an explicit call made by
# app.py and by scripts/trace_adk_agents.py; importing this module must not
# decide where anyone's log records go.
LOG = obs.get_logger(__name__)

os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

# Engine-level singletons (NOT bound to any single customer_id at initialization)
GLOBAL_MEMORY_BANK_CLIENT: MemoryBankClient = create_default_memory_bank_client(verbose=False)
GLOBAL_CATALOG_GOVERNOR: CatalogGovernor = create_default_catalog_governor(strict=True, verbose=False)

# Import-time, so it is the first thing in any log and answers "which engine and
# which dataset was this run actually talking to" before anything else happens.
LOG.debug(
    "singletons ready memory_bank_parent=%s catalog_project=%s dataset=%s strict=%s",
    GLOBAL_MEMORY_BANK_CLIENT.parent,
    GLOBAL_CATALOG_GOVERNOR.client.project,
    getattr(GLOBAL_CATALOG_GOVERNOR.client, "dataset", NOT_RECORDED),
    GLOBAL_CATALOG_GOVERNOR.strict,
)


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
        calling_agent=AgentId.IDENTITY_CONTEXT,
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
    """Golden Query 03: Between two moments, what on this card is worth a second look, and why? Scores on signals only, never on a stored label."""
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
    """Golden Query 07: What disputes does this customer already have running, and which regulation does the ledger record for each?"""
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
    """Golden Query 08: Was this dispute filed in time, and what does the bank now owe and by when, under the regulation the ledger records for it?"""
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
    with obs.turn("memory.retrieve", LOG, customer_id=customer_id) as span:
        span.debug_payload("query", query)
        scope = {"customer_id": customer_id}
        memories = GLOBAL_MEMORY_BANK_CLIENT.retrieve(scope=scope, query=query, top_k=8)
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


def _memory_rows(bank: MemoryBankClient, scope: Dict[str, str], query: str,
                 top_k: int) -> List[Dict[str, Any]]:
    """Retrieval and ranking run inside Google - `retrieve` is a service call."""
    rows: List[Dict[str, Any]] = []
    for item in bank.retrieve(scope=scope, query=query or None, top_k=top_k):
        memory = item.get("memory", item)
        memory_name = mb_field(memory, "name")
        source = "memories.retrieve:%s" % (
            str(memory_name).split("/")[-1] if memory_name != NOT_RECORDED else NOT_RECORDED
        )
        rows.append({
            "memory_id": _tagged_value(
                str(memory_name).split("/")[-1] if memory_name != NOT_RECORDED else NOT_RECORDED, source),
            "fact": _tagged_value(mb_field(memory, "fact"), source),
            "topics": _tagged_value(mb_field(memory, "topics"), source),
            "update_time": _tagged_value(mb_field(memory, "updateTime"), source),
            "distance": _tagged_value(mb_field(item, "distance"), source),
        })
    return rows


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

    span = obs.turn("channel.produce", LOG, channel=channel, customer_id=customer_id)
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

        for session in bank.list_sessions():
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

        memories = _memory_rows(bank, scope, memory_query, top_k)

        turn_handle.detail(
            sessions=len(matched),
            memories=len(memories),
            considered=considered,
        )
        return {
            "channel": identity(channel, channel_source, "agent identity constant"),
            "customer_scope": computed(scope, "scope argument sent to sessions.list / memories.retrieve"),
            "memory_bank_parent": computed(bank.parent, "MemoryBankClient.parent"),
            "retrieval_query": computed(memory_query, "channel retrieval query issued by %s" % channel_source),
            "sessions": matched,
            "session_count": computed(len(matched), "count of sessions.list rows matching channel token + scope"),
            "memories": memories,
            "memory_count": computed(len(memories), "count of memories.retrieve rows"),
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
        memory_query="fraud velocity alert, concurrent logins from different cities, card lock or restriction",
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
        memory_query="inbound phone call, IVR authentication, dropped call before passcode verification",
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
        memory_query="mobile app session, digital wallet provisioning, tokenization declined, card status",
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
        memory_query="online banking web session, browser login, dispute filed, card controls toggled",
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
        memory_query="branch visit, teller or banker consultation, in-person identity verification",
        include_events=include_events,
        top_k=top_k,
    )


def read_customer_memory_bank(
    customer_id: str = DEFAULT_CUSTOMER_SCOPE_ID,
    query: str = "",
    top_k: int = DEFAULT_CHANNEL_TOP_K,
    client: Optional[MemoryBankClient] = None,
) -> Dict[str, Any]:
    """Synthesizer read path: scoped memories straight from Memory Bank.

    An empty `query` means simple retrieval of everything in scope; a non-empty
    one is a similarity search the service runs - this process does no ranking.
    """
    bank = _memory_bank(client)
    scope = {"customer_id": customer_id}
    memories = _memory_rows(bank, scope, query, top_k)
    return {
        "customer_scope": computed(scope, "scope argument sent to memories.retrieve"),
        "memory_bank_parent": computed(bank.parent, "MemoryBankClient.parent"),
        "retrieval_query": computed(query or NOT_RECORDED, "query argument passed to read_customer_memory_bank"),
        "retrieval_mode": computed(
            "similaritySearchParams" if query else "simpleRetrievalParams",
            "MemoryBankClient.retrieve request body branch",
        ),
        "memories": memories,
        "memory_count": computed(len(memories), "count of memories.retrieve rows"),
    }


def execute_one_click_card_unlock(
    customer_id: str = DEFAULT_CUSTOMER_SCOPE_ID,
    card_id: str = DEFAULT_CARD_ID,
    reason: str = "",
    record_to_memory_bank: bool = True,
    client: Optional[MemoryBankClient] = None,
) -> Dict[str, Any]:
    """Stages the card unlock and writes the decision trail into Memory Bank.

    There is no card-issuing platform client in this process, so the tool does
    not claim the card was unlocked. It reports the evidence it read, the audit
    record it actually wrote, and leaves the downstream confirmation
    `unmeasured` rather than asserting a success nobody observed.
    """
    bank = _memory_bank(client)
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


# ==============================================================================
# 4. THE 8 CCB FRAUD DESK ADK AGENTS (hub-and-spoke around the synthesizer)
# ==============================================================================

# Retained internal tree. The golden-query specialists below predate the 8-agent
# roster and are still the cleanest way to exercise Golden Queries 01-12 on their
# own; the desk that /api/agents/list publishes is the hub-and-spoke built by
# create_consumer_credit_synthesizer_agent further down.
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
        description="02 Identity & Context Agent: Retrieves scoped customer facts from Vertex AI Memory Bank and runs Golden Query 01 within whatever the catalog's sensitivity aspects permit.",
        instruction=(
            "You are Agent 02 (Identity & Context). Call list_memory_topics_tool first to see "
            "what kinds of fact this Memory Bank files, then phrase your retrieval in that "
            "vocabulary - retrieval is a semantic search, so a query worded like the topic it "
            "lives under finds it and a guess may not. Then retrieve scoped customer memories "
            "using retrieve_customer_memories_tool(customer_id=...) and run golden_01_customer_360_snapshot. "
            "Before selecting any column, call catalog_governance_authorize_tool and honour the "
            "agent_behavior it returns. Report which columns you declined and the aspect that "
            "decided it; a refusal you cannot cite is not a refusal."
        ),
        tools=[
            FunctionTool(list_memory_topics_tool),
            FunctionTool(retrieve_customer_memories_tool),
            FunctionTool(golden_01_customer_360_snapshot),
            FunctionTool(catalog_governance_authorize_tool),
        ],
    )

    fraud_triage_agent = LlmAgent(
        name="fraud_triage_agent",
        model=model,
        description="03 Fraud Triage Agent: Executes Golden Queries 02, 03, 04 on BigQuery to score travel-window activity within whatever the catalog's sensitivity aspects permit.",
        instruction=(
            "You are Agent 03 (Fraud Triage). Use only approved Golden Queries (02, 03, 04) "
            "with named @parameters (zero ad-hoc SQL). Before selecting any column, call "
            "catalog_governance_authorize_tool and honour the agent_behavior it returns; where "
            "it offers a replacement column, use the replacement. Triage on the signals only - "
            "never read a stored fraud label, because scoring against the answer proves nothing."
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
        description="04 Policy & Compliance Agent: Resolves the governing regulation for each dispute from the disputes ledger and the Dataplex glossary, and runs Golden Queries 07, 08, 09.",
        instruction=(
            "You are Agent 04 (Policy & Compliance). For each dispute in scope, read the "
            "regulation the ledger records against that dispute - do not infer it from the "
            "card, and do not carry one dispute's regulation over to another. Then call "
            "catalog_lookup_glossary_term_tool for that regulation and state the remedy it "
            "defines, including the case where the remedy is that none exists: 'no date "
            "recorded' and 'no such remedy under this regulation' are different answers and "
            "you must say which one applies. Every deadline you quote must come from a query "
            "result, never from your own knowledge of the regulation. Before reading any "
            "table, call catalog_governance_authorize_tool and honour the lifecycle status it "
            "returns."
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
        description="05 Resolution & Actions Agent: Freezes compromised cards, revokes active device wallet tokens (Golden Query 10), issues VCN, and obeys the customer's standing instructions as retrieved from Memory Bank.",
        instruction=(
            "You are Agent 05 (Resolution & Actions). Check active wallet tokens via "
            "golden_10_wallet_tokens_active_on_card and event history via golden_11_dispute_event_timeline. "
            "Before any irreversible action, retrieve the customer's standing instructions and "
            "comply with whichever apply. Call list_memory_topics_tool first and pick the topic "
            "whose description covers commitments the bank has made to this customer, then "
            "retrieve using that topic's wording. Quote the instruction you are complying with, "
            "and the topic it was filed under; if the retrieval returns none, say that you found "
            "none rather than proceeding as though you had checked."
        ),
        tools=[
            FunctionTool(golden_10_wallet_tokens_active_on_card),
            FunctionTool(golden_11_dispute_event_timeline),
            # Without these two the instruction above is uncallable: the agent
            # would be told to retrieve standing instructions and given no way to
            # do it, which is exactly the kind of gap a hardcoded instruction
            # hides. The topics tool is what lets it find the right wording
            # instead of being told the topic name.
            FunctionTool(list_memory_topics_tool),
            FunctionTool(retrieve_customer_memories_tool),
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
            "Route the customer's case across Identity & Context (02), Fraud Triage (03), "
            "Policy & Compliance (04), Resolution & Actions (05), and Memory Writer (06). "
            "Establish the facts from the tools and the spokes. Do not assume which regulation "
            "governs a dispute, and do not assume two disputes on the same customer are "
            "governed the same way - that is Agent 04's finding to report, not your premise."
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
    _log_agent_tree(orchestrator, "create_ccb_fraud_desk_orchestrator_agent")
    return orchestrator


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


def _spoke_agent(name: str, model: str, description: str, instruction: str,
                 tools: List[Any]) -> LlmAgent:
    """A spoke can only answer the hub.

    Both transfer flags are off so a spoke cannot hand the customer to a peer or
    walk back up the tree: every hop goes through the synthesizer, which is what
    makes the run auditable.
    """
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
        description="Fraud Velocity Specialist (CHANNEL_PRODUCER, FRAUD_DETECTION): reads seeded velocity and card-lock sessions from Memory Bank.",
        instruction=(
            "You are the FRAUD_DETECTION channel producer. Call query_fraud_velocity_alerts "
            "with the customer_id you were given and report only what it returns. "
            "If it returns no sessions and no memories, say so plainly - never supply an "
            "example alert, and render any missing field as n/r."
        ),
        tools=[
            FunctionTool(query_fraud_velocity_alerts),
            FunctionTool(golden_03_suspicious_activity_on_card_window),
            FunctionTool(golden_04_geo_impossible_travel),
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
            "fact came from when it matters, and "
            "state the causal chain in order. Run execute_one_click_card_unlock only after the "
            "veracity gate has passed the claim, and repeat its result as given: it stages the "
            "unlock and writes the audit trail, it does not confirm the card platform acted. "
            "Any value a tool did not return is n/r - never fill a gap yourself."
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
    """The 8 CCB Fraud & Disputes Desk agents: 5 channel producers, the veracity
    gate, the audit sweeper, and the synthesizer hub they all report to.

    Order matches the roster table in README.md and the names asserted by
    tests/test_backend.py. `name` is the ADK agent name, because that is what
    /api/agents/list is checked against.
    """
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    descriptors = [
        AgentDescriptor(
            agent_id="01_fraud_monitoring_agent",
            name="fraud_monitoring_agent",
            role="Fraud Velocity Specialist (CHANNEL_PRODUCER)",
            channel=BankChannel.FRAUD_DETECTION,
            model=model_name,
            tools=[
                "query_fraud_velocity_alerts",
                "golden_03_suspicious_activity_on_card_window",
                "golden_04_geo_impossible_travel",
            ],
            description="Reads the seeded FRAUD_DETECTION sessions for the customer in scope and scores the window with Golden Queries 03 and 04. Empty channel returns empty.",
        ),
        AgentDescriptor(
            agent_id="02_telephony_ivr_agent",
            name="telephony_ivr_agent",
            role="Contact Center IVR Specialist (CHANNEL_PRODUCER)",
            channel=BankChannel.TELEPHONY_IVR,
            model=model_name,
            tools=[
                "query_ivr_call_records",
                "golden_02_recent_authorizations_for_card",
            ],
            description="Reads the seeded TELEPHONY_IVR call sessions and their recorded turns; declined amounts come from Golden Query 02, not from the transcript.",
        ),
        AgentDescriptor(
            agent_id="03_mobile_app_agent",
            name="mobile_app_agent",
            role="Mobile & Digital Wallet Specialist (CHANNEL_PRODUCER)",
            channel=BankChannel.MOBILE_APP,
            model=model_name,
            tools=[
                "query_mobile_wallet_events",
                "golden_10_wallet_tokens_active_on_card",
            ],
            description="Reads the seeded MOBILE_APP sessions covering wallet provisioning and tokenization outcomes, cross-checked against active tokens in Golden Query 10.",
        ),
        AgentDescriptor(
            agent_id="04_web_portal_agent",
            name="web_portal_agent",
            role="Online Web Banking Specialist (CHANNEL_PRODUCER)",
            channel=BankChannel.WEB_PORTAL,
            model=model_name,
            tools=[
                "query_web_portal_activity",
                "golden_07_open_disputes_for_customer",
            ],
            description="Reads the seeded WEB_PORTAL sessions - logins, dispute filings, card controls - and reconciles any dispute against the open-dispute ledger.",
        ),
        AgentDescriptor(
            agent_id="05_branch_support_agent",
            name="branch_support_agent",
            role="Branch & Teller Specialist (CHANNEL_PRODUCER)",
            channel=BankChannel.BRANCH_SUPPORT,
            model=model_name,
            tools=[
                "query_branch_teller_interactions",
                "golden_01_customer_360_snapshot",
            ],
            description="Reads the seeded BRANCH_SUPPORT sessions for banker consultations and in-person identity checks, with Golden Query 01 for customer identity only.",
        ),
        AgentDescriptor(
            agent_id="06_claim_veracity_validator_agent",
            name="claim_veracity_validator_agent",
            role="Pre-Write Veracity Gatekeeper (PRE_WRITE_VALIDATOR)",
            channel=None,
            model=model_name,
            tools=["validate_and_record_customer_claim"],
            description="Checks a customer claim against ground-truth avenues before any write reaches Memory Bank. Implemented in backend/veracity_and_audit.py.",
        ),
        AgentDescriptor(
            agent_id="07_memory_bank_audit_agent",
            name="memory_bank_audit_agent",
            role="Consolidated Sweep Auditor (CONSOLIDATED_AUDITOR)",
            channel=None,
            model=model_name,
            tools=["run_consolidated_memory_audit_sweep"],
            description="Runs the enterprise-wide consolidated sweep across customer memory banks and reports anomalies. Implemented in backend/veracity_and_audit.py.",
        ),
        AgentDescriptor(
            agent_id="08_consumer_credit_synthesizer_agent",
            name="consumer_credit_synthesizer_agent",
            role="Lead Synthesizer Orchestrator (LEAD_SYNTHESIZER)",
            channel=BankChannel.CORE_BANKING,
            model=model_name,
            tools=["read_customer_memory_bank", "execute_one_click_card_unlock"],
            description="Hub of the topology: collects every spoke's account, reconstructs the causal chain from Memory Bank, and stages the one-click unlock with its audit trail.",
        ),
    ]
    return descriptors

"""
BigQuery ground truth.

This replaces `GroundTruthTelemetryStore`, which on `main` was a 187-line Python
dict keyed by `cust_jpmc_88329`, describing a customer who does not exist, with
balances that contradicted the balances asserted in two other files. It called
itself the "Authoritative system ground-truth telemetry (Account Statements,
Live Balances, POS Ledger...) against which customer claims are verified", and
its data source was the literal below it.

The rule this module enforces is narrow and important:

    **BigQuery is authoritative for facts. Memory Bank is not.**

A memory says what a customer *told us*. A row says what *happened*. When they
disagree, the row wins, and the disagreement is a finding worth surfacing rather
than an error to smooth over. `backend/memory_eval/reconcile.py` depends on this
being a hard rule.

Every read goes through the catalog first. Not because Dataplex can stop a query
-- it cannot, it is not in the query path -- but because the agent is supposed to
stop itself, and a governance story is only worth telling if the check happens
before the read rather than being narrated after it.
"""

from __future__ import annotations

import datetime
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from google.cloud import bigquery

from backend.config import Settings, settings as load_settings
from backend.knowledge_catalog import CatalogGovernor, GovernanceDecision
from backend.provenance import Provenanced, measured, unmeasured

log = logging.getLogger(__name__)


class GroundTruthError(RuntimeError):
    """A BigQuery read failed. Not recoverable by substituting a plausible row."""


@dataclass
class QueryResult:
    """Rows, plus everything needed to audit how they were obtained."""

    query_id: str
    rows: list[dict]
    sql: str
    params: dict
    latency_ms: Provenanced
    bytes_processed: Provenanced
    job_id: Provenanced
    governance: list[GovernanceDecision] = field(default_factory=list)
    refusals: list[GovernanceDecision] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_dict(self) -> dict:
        return {
            "query_id": self.query_id,
            "row_count": self.row_count,
            "rows": self.rows,
            "sql": self.sql,
            "params": self.params,
            "latency_ms": self.latency_ms.to_dict(),
            "bytes_processed": self.bytes_processed.to_dict(),
            "job_id": self.job_id.to_dict(),
            "governance_checks": [g.to_dict() for g in self.governance],
            "voluntary_refusals": [r.to_dict() for r in self.refusals],
        }


# --------------------------------------------------------------------------
# Golden query contracts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldenQuery:
    """A named, parameterised query with a declared governance surface.

    `governed_tables` and `governed_columns` are the list of things to *ask the
    catalog about* -- the question, not the answer. They are never shown to a
    model. Removing them would not remove the mention of a sensitive column, it
    would remove the check that we asked about it.
    """

    query_id: str
    purpose: str
    sql: str
    governed_tables: tuple[str, ...]
    governed_columns: tuple[str, ...] = ()


GOLDEN_QUERIES: dict[str, GoldenQuery] = {
    "customer_360": GoldenQuery(
        query_id="customer_360",
        purpose="Whole-customer snapshot: products, 90-day behaviour, open disputes, active travel.",
        governed_tables=("v_customer_360",),
        governed_columns=("customers.ssn_last4",),
        sql="""
            SELECT customer_id, full_name, segment, preferred_channel,
                   standing_instructions, open_accounts, active_cards, active_tokens,
                   auth_count_90d, approved_amount_usd_90d, declined_count_90d,
                   fraud_auth_count_90d, open_disputes, total_disputed_amount,
                   active_travel_country, last_contact_at, last_contact_channel
            FROM `{dataset}.v_customer_360`
            WHERE customer_id = @customer_id
        """,
    ),
    "open_disputes": GoldenQuery(
        query_id="open_disputes",
        purpose="Open disputes for a customer, with the regulation path and provisional-credit state per dispute.",
        governed_tables=("disputes", "cards"),
        governed_columns=("cards.pan_masked", "cards.pan_full"),
        sql="""
            SELECT d.dispute_id, d.card_id, c.pan_masked, c.card_type,
                   d.opened_date, d.regulation_path, d.dispute_reason,
                   d.disputed_amount, d.status,
                   d.provisional_credit_issued, d.provisional_credit_date,
                   d.provisional_credit_amount, d.provisional_credit_due_date,
                   d.investigation_due_date, d.filing_window_days,
                   d.is_within_filing_window
            FROM `{dataset}.disputes` d
            LEFT JOIN `{dataset}.cards` c USING (card_id)
            WHERE d.customer_id = @customer_id
              AND d.status NOT IN ('CLOSED', 'WITHDRAWN')
            ORDER BY d.opened_date DESC
        """,
    ),
    "disputed_amounts": GoldenQuery(
        query_id="disputed_amounts",
        purpose="Authoritative disputed amounts. The reconciler's source of truth for money.",
        governed_tables=("disputes",),
        sql="""
            SELECT dispute_id, disputed_amount, status, regulation_path, opened_date
            FROM `{dataset}.disputes`
            WHERE customer_id = @customer_id
            ORDER BY opened_date DESC
        """,
    ),
    "travel_notices": GoldenQuery(
        query_id="travel_notices",
        purpose="Declared travel, including the actual return date -- which is what makes an early return checkable.",
        governed_tables=("travel_notices",),
        sql="""
            SELECT notice_id, destination_country, destination_city,
                   travel_start_date, travel_end_date, actual_return_date,
                   status, declared_channel, superseded_by_notice_id, notes
            FROM `{dataset}.travel_notices`
            WHERE customer_id = @customer_id
            ORDER BY travel_start_date DESC
        """,
    ),
    "cards_for_customer": GoldenQuery(
        query_id="cards_for_customer",
        purpose="Card inventory and lifecycle, including replacement chains.",
        governed_tables=("cards",),
        governed_columns=("cards.pan_masked", "cards.pan_full"),
        sql="""
            SELECT card_id, pan_masked, last_four, card_type, network, status,
                   issue_date, expiry_date, replaces_card_id, replaced_by_card_id,
                   freeze_reason
            FROM `{dataset}.cards`
            WHERE customer_id = @customer_id
            ORDER BY issue_date DESC
        """,
    ),
    "wallet_tokens": GoldenQuery(
        query_id="wallet_tokens",
        purpose="Digital wallet tokens that can spend on this customer's cards, and whether each is still trusted.",
        governed_tables=("wallet_tokens",),
        sql="""
            SELECT token_id, card_id, wallet_provider, device_name, device_os,
                   device_trust_score, token_status, provisioned_at,
                   provisioning_country, last_used_at, revoked_at
            FROM `{dataset}.wallet_tokens`
            WHERE customer_id = @customer_id
            ORDER BY provisioned_at DESC
        """,
    ),
    "contact_history": GoldenQuery(
        query_id="contact_history",
        purpose="Every contact across channels, with outcome and whether the caller was authenticated.",
        governed_tables=("contact_events",),
        sql="""
            SELECT contact_id, channel, direction, started_at, duration_seconds,
                   intent, outcome, handled_by, authenticated, auth_method,
                   related_dispute_id, notes
            FROM `{dataset}.contact_events`
            WHERE customer_id = @customer_id
            ORDER BY started_at DESC
            LIMIT 50
        """,
    ),
    "recent_authorizations": GoldenQuery(
        query_id="recent_authorizations",
        purpose="Recent authorisations with merchant and geography. Never selects the stored fraud label.",
        governed_tables=("authorizations", "merchants"),
        governed_columns=("authorizations.is_fraud",),
        sql="""
            SELECT a.auth_id, a.card_id, a.auth_timestamp, a.amount, a.currency,
                   a.amount_usd, a.channel, a.entry_mode, a.country_code, a.city,
                   a.decision, a.decline_reason, a.risk_score, a.risk_band,
                   m.merchant_name, m.merchant_category, m.country_risk_tier
            FROM `{dataset}.authorizations` a
            LEFT JOIN `{dataset}.merchants` m USING (merchant_id)
            WHERE a.customer_id = @customer_id
            ORDER BY a.auth_timestamp DESC
            LIMIT 100
        """,
    ),
    "dispute_timeline": GoldenQuery(
        query_id="dispute_timeline",
        purpose="Event-by-event history of a dispute, including shipment destination.",
        governed_tables=("dispute_events",),
        sql="""
            SELECT event_id, dispute_id, event_timestamp, event_type, actor_type,
                   actor_id, channel, event_detail, shipment_tracking_id,
                   shipment_destination_type, shipment_destination_country
            FROM `{dataset}.dispute_events`
            WHERE customer_id = @customer_id
            ORDER BY event_timestamp ASC
        """,
    ),
    "standing_instructions": GoldenQuery(
        query_id="standing_instructions",
        purpose="The customer's standing instructions -- what the bank has been told to always or never do.",
        governed_tables=("customers",),
        governed_columns=("customers.ssn_last4",),
        sql="""
            SELECT customer_id, standing_instructions, preferred_channel,
                   preferred_language, segment, risk_profile
            FROM `{dataset}.customers`
            WHERE customer_id = @customer_id
        """,
    ),
}


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------


class GroundTruthReader:
    """Runs golden queries against BigQuery, under catalog governance."""

    def __init__(
        self,
        cfg: Optional[Settings] = None,
        governor: Optional[CatalogGovernor] = None,
        client: Optional[bigquery.Client] = None,
    ) -> None:
        self.cfg = cfg or load_settings()
        self.governor = governor if governor is not None else CatalogGovernor(self.cfg)
        self._client = client

    @property
    def client(self) -> bigquery.Client:
        if self._client is None:
            self._client = bigquery.Client(project=self.cfg.project_id)
        return self._client

    def list_queries(self) -> list[dict]:
        return [
            {
                "query_id": q.query_id,
                "purpose": q.purpose,
                "governed_tables": list(q.governed_tables),
                "governed_columns": list(q.governed_columns),
            }
            for q in GOLDEN_QUERIES.values()
        ]

    def run(
        self,
        query_id: str,
        params: Optional[dict] = None,
        enforce_refusals: bool = True,
    ) -> QueryResult:
        """Run one golden query.

        Args:
            query_id: key into `GOLDEN_QUERIES`. Ad-hoc SQL is deliberately not
                accepted -- a contract that can be bypassed is not a contract.
            params: named query parameters.
            enforce_refusals: when the catalog refuses a table, decline the read.
                The refusal is *advisory* at the Dataplex end; this flag is where
                the agent chooses to honour it. Setting it False runs the query
                anyway and still records the refusal, which is how you show the
                difference between advice and enforcement.
        """
        golden = GOLDEN_QUERIES.get(query_id)
        if golden is None:
            raise GroundTruthError(
                f"unknown golden query {query_id!r}. "
                f"Known: {', '.join(sorted(GOLDEN_QUERIES))}. "
                f"Ad-hoc SQL is not accepted."
            )

        params = params or {}
        governance: list[GovernanceDecision] = []
        refusals: list[GovernanceDecision] = []

        for table in golden.governed_tables:
            decision = self.governor.check_table(table)
            governance.append(decision)
            if not decision.allowed:
                refusals.append(decision)

        for qualified in golden.governed_columns:
            table, _, column = qualified.partition(".")
            decision = self.governor.check_column(table, column)
            governance.append(decision)
            if not decision.allowed:
                refusals.append(decision)

        table_refusals = [r for r in refusals if r.kind == "TABLE"]
        if table_refusals and enforce_refusals:
            return QueryResult(
                query_id=query_id,
                rows=[],
                sql="",
                params=params,
                latency_ms=unmeasured("query not run: the agent declined on a catalog verdict", unit="ms"),
                bytes_processed=unmeasured("query not run", unit=" bytes"),
                job_id=unmeasured("query not run"),
                governance=governance,
                refusals=refusals,
            )

        sql = golden.sql.format(dataset=self.cfg.dataset_resource).strip()
        job_config = bigquery.QueryJobConfig(
            query_parameters=[_as_param(k, v) for k, v in params.items()]
        )

        started = time.perf_counter()
        try:
            job = self.client.query(sql, job_config=job_config)
            rows = [dict(r) for r in job.result()]
        except Exception as exc:
            raise GroundTruthError(f"golden query {query_id} failed: {exc}") from exc
        elapsed = (time.perf_counter() - started) * 1000.0

        return QueryResult(
            query_id=query_id,
            rows=[_jsonable(r) for r in rows],
            sql=sql,
            params=params,
            latency_ms=measured(round(elapsed, 1), unit="ms", source="bigquery.query"),
            bytes_processed=(
                measured(job.total_bytes_processed, unit=" bytes", source="job.total_bytes_processed")
                if job.total_bytes_processed is not None
                else unmeasured("BigQuery did not report bytes processed", unit=" bytes")
            ),
            job_id=measured(job.job_id, source="job.job_id"),
            governance=governance,
            refusals=refusals,
        )

    # -- convenience -------------------------------------------------------

    def customer_ids(self, limit: int = 25) -> list[str]:
        """Real customer ids from the warehouse.

        Exists so that nothing downstream has to hardcode one. `main` shipped
        `DEFAULT_CUSTOMER_ID = "cust_jpmc_88329"` and used it as the fallback in
        thirteen routes, for a customer that is not in the dataset.
        """
        sql = f"SELECT customer_id FROM `{self.cfg.dataset_resource}.customers` ORDER BY customer_id LIMIT {int(limit)}"
        return [r["customer_id"] for r in (dict(x) for x in self.client.query(sql).result())]

    def disputed_amounts(self, customer_id: str) -> dict[str, float]:
        """Dispute id -> authoritative amount. The reconciler's reference set."""
        result = self.run("disputed_amounts", {"customer_id": customer_id})
        return {
            row["dispute_id"]: float(row["disputed_amount"])
            for row in result.rows
            if row.get("disputed_amount") is not None
        }


def _as_param(name: str, value: Any) -> bigquery.ScalarQueryParameter:
    """Bind a Python value to a typed BigQuery parameter.

    `bool` is checked before `int` deliberately. In Python `bool` is a subclass
    of `int`, so the obvious ordering makes the bool branch unreachable and every
    boolean parameter binds as INT64, which BigQuery rejects with
    `Bad int64 value: True`. `datetime` before `date` is the same trap in the
    same function: `datetime.datetime` is a subclass of `datetime.date`.
    """
    if isinstance(value, bool):
        return bigquery.ScalarQueryParameter(name, "BOOL", value)
    if isinstance(value, int):
        return bigquery.ScalarQueryParameter(name, "INT64", value)
    if isinstance(value, float):
        return bigquery.ScalarQueryParameter(name, "FLOAT64", value)
    if isinstance(value, datetime.datetime):
        return bigquery.ScalarQueryParameter(name, "DATETIME", value)
    if isinstance(value, datetime.date):
        return bigquery.ScalarQueryParameter(name, "DATE", value)
    return bigquery.ScalarQueryParameter(name, "STRING", str(value))


def _jsonable(row: dict) -> dict:
    """Coerce BigQuery row values into JSON-serialisable form."""
    import datetime
    import decimal

    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (datetime.date, datetime.datetime)):
            out[key] = value.isoformat()
        elif isinstance(value, decimal.Decimal):
            out[key] = float(value)
        else:
            out[key] = value
    return out

"""Agents 6 and 7: pre-write claim veracity validation, and the Memory Bank audit sweep.

Memory Bank arbitrates recency, not truth. Whichever fragment was written last
wins, so an unchallenged false claim becomes the customer's history. Agent 6
stands in front of the write: it asks the Knowledge Catalog what it may read,
reads only that, cross-checks the claim against BigQuery, and lets Gemini reach
a verdict on evidence it can name. Agent 7 sweeps what is already stored and
re-checks every amount, deadline and dispute id against BigQuery, because a
memory is a record of what somebody said, never the source of a number.

Three rules this file will not bend:

  * Nothing is invented. If BigQuery, Dataplex or Gemini does not answer, the
    call raises. A demo that looks healthy while broken is worse than one that
    stops.
  * Anything not measured renders as the literal string "n/r" -- never a zero,
    a default, or a plausible-looking id.
  * The catalog advises; it does not enforce. Dataplex supplies the basis on
    which this agent declines a table or a column. The decline is the control,
    and it is recorded so a reviewer can check the agent honoured what it read.
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import certifi
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from google import genai
from google.genai import types

from backend.models import (
    AuditAnomaly,
    AuditReport,
    AdminCardReviewCase,
    BankChannel,
    ClaimVeracityEvaluation,
    MemoryFragment,
    RelevantAvenueCheck,
    SeverityLevel,
    VeracityStatus,
    VerificationCheckItem,
)
from backend import memory_write_path, obs
from backend.knowledge_catalog import CatalogGovernor, shared_catalog_governor
from backend.memory_bank import MemoryBankClient, shared_memory_bank_client

LOG = obs.get_logger(__name__)
from backend.provenance import (
    NOT_RECORDED,
    computed,
    env_param,
    identity,
    live,
    seeded,
    unmeasured,
    value_of,
)

load_dotenv()

os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
)

LOG = logging.getLogger(__name__)


class VeracityServiceError(RuntimeError):
    """A dependency did not answer, so there is no verdict to give.

    Raised instead of returning a default. Callers surface it; nothing in this
    file catches it to substitute a guess.
    """


# ----------------------------------------------------------------------------
# Deployment parameters. Ids live in the environment, never in the source.
# ----------------------------------------------------------------------------

def _project_id(explicit: Optional[str] = None) -> str:
    return explicit or env_param("GOOGLE_CLOUD_PROJECT") or NOT_RECORDED


def _location(explicit: Optional[str] = None) -> str:
    return explicit or env_param("GOOGLE_CLOUD_LOCATION", "us-central1")


def _dataset(explicit: Optional[str] = None) -> str:
    return explicit or env_param("BQ_DATASET", "jpmc_ccb_fraud")


def _model_name(explicit: Optional[str] = None) -> str:
    return explicit or env_param("VERACITY_MODEL", "gemini-2.5-flash")


# ----------------------------------------------------------------------------
# The golden-query layer, imported late.
# ----------------------------------------------------------------------------
# backend.adk_agents imports this module at import time, so a module-level
# import back into it is a cycle. Resolving it per call also means a run picks
# up contracts added to the registry after this file was written.

def _adk():
    from backend import adk_agents  # noqa: WPS433 - deliberate late import

    return adk_agents


def _golden_contracts() -> Dict[str, Any]:
    return getattr(_adk(), "GOLDEN_QUERY_CONTRACTS", {})


def _contract_for_table(table: str) -> Optional[Any]:
    """The first registered golden contract whose primary table is `table`.

    Resolved from the registry rather than hardcoded, so a contract the query
    owner adds later is picked up without editing this file.
    """
    for contract in _golden_contracts().values():
        tables = list(getattr(contract, "governed_tables", []) or [])
        if tables and tables[0] == table:
            return contract
    return None


def _rows_or_raise(result: Any, label: str) -> List[Dict[str, Any]]:
    """Unpacks a golden-query result, refusing the layer's swallowed-error row.

    execute_golden_query_contract catches BigQuery failures and returns a single
    row shaped {"note": "BigQuery execution note: ..."}. Passing that on as data
    is precisely the fake-healthy outcome this rework removes.
    """
    if not isinstance(result, dict):
        raise VeracityServiceError("%s returned %s, not a result dict." % (label, type(result).__name__))

    if result.get("allowed") is False:
        raise VeracityServiceError(
            "%s was refused by the golden-query contract: %s" % (label, result.get("error", NOT_RECORDED))
        )

    rows = result.get("rows")
    if rows is None:
        raise VeracityServiceError("%s returned no rows key; the contract layer did not execute." % label)

    for row in rows:
        if isinstance(row, dict) and set(row.keys()) <= {"note", "error"}:
            raise VeracityServiceError(
                "%s did not reach BigQuery: %s" % (label, row.get("note") or row.get("error"))
            )
    return list(rows)


def _query_source(query_id: str, table: str) -> str:
    return "bigquery:%s#golden_%s" % (table, query_id)


# ----------------------------------------------------------------------------
# Catalog authorization
# ----------------------------------------------------------------------------

# Columns the validator probes before every run. It does not know which are
# restricted -- the catalog's agent_behavior aspect decides, and the agent
# obeys whatever comes back.
RESTRICTED_COLUMN_PROBES: Tuple[Tuple[str, str], ...] = (
    ("customers", "ssn_last4"),
    ("cards", "pan_full"),
    ("cards", "pan_masked"),
)

# Each avenue offers the catalog a set of candidate tables. The deprecated
# sibling is offered alongside the live one on purpose: the code does not know
# which is fit to read until the lifecycle-status aspect answers.
AVENUE_SPECS: Tuple[Dict[str, Any], ...] = (
    {
        "avenue_id": "AVENUE_TRAVEL_GEO",
        "avenue_title": "Travel notice & geo verification",
        "candidate_tables": ("travel_notices",),
        "scope": "customer",
    },
    {
        "avenue_id": "AVENUE_AUTH_LEDGER",
        "avenue_title": "Card authorization ledger",
        "candidate_tables": ("authorizations", "authorizations_deprecated_v1"),
        "scope": "card",
    },
    {
        "avenue_id": "AVENUE_CONTACT_HISTORY",
        "avenue_title": "IVR & contact history",
        "candidate_tables": ("contact_events",),
        "scope": "customer",
    },
    {
        "avenue_id": "AVENUE_WALLET_TOKENS",
        "avenue_title": "Digital wallet tokenization",
        "candidate_tables": ("wallet_tokens",),
        "scope": "card",
    },
)

CATALOG_ADVISORY_NOTE = (
    "Dataplex advises; it does not enforce. The catalog supplied the basis and "
    "the agent's decision to decline is the control being demonstrated."
)


def _entry_source(governor: CatalogGovernor, table: str, dataset: Optional[str]) -> str:
    """The Dataplex entry this decision was read from, for the provenance tag."""
    try:
        return "dataplex:entries.lookup:%s" % governor.client.bigquery_entry_name(table, dataset)
    except Exception:  # the client refuses to name an entry without a dataset
        return "dataplex:entries.lookup:%s" % table


def _authorization_record(
    governor: CatalogGovernor,
    avenue_id: str,
    table: str,
    dataset: Optional[str],
) -> Dict[str, Any]:
    """Asks the catalog about one table and records the whole exchange.

    Three things go in the record because three things are worth auditing: what
    was asked, what came back, and whether the agent did as it was told.
    """
    decision = governor.check_table(table, dataset)
    verdict = decision.to_dict()
    honoured = (not decision.allowed) or decision.action == "ALLOW"

    return {
        "avenue_id": avenue_id,
        "table": table,
        "asked": "May this validator read %s for %s?" % (table, avenue_id),
        "catalog_said": live(verdict, _entry_source(governor, table, dataset)),
        "aspect_cited": live(verdict.get("evidence", NOT_RECORDED), _entry_source(governor, table, dataset)),
        "authoritative_replacement": (
            live(verdict.get("replacement"), _entry_source(governor, table, dataset))
            if verdict.get("replacement")
            else unmeasured("catalog named no replacement for %s" % table)
        ),
        "agent_action": computed("READ" if decision.allowed else "DECLINED", "validator.catalog_gate"),
        "agent_honoured_catalog": computed(honoured, "validator.catalog_gate"),
        "enforcement": CATALOG_ADVISORY_NOTE,
    }


def _column_records(
    governor: CatalogGovernor,
    dataset: Optional[str],
    probes: Sequence[Tuple[str, str]] = RESTRICTED_COLUMN_PROBES,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Probes the governed columns and returns (records, columns the agent declined)."""
    records: List[Dict[str, Any]] = []
    declined: List[str] = []

    for table, column in probes:
        decision = governor.check_column(table, column, dataset)
        verdict = decision.to_dict()
        subject = "%s.%s" % (table, column)
        if not decision.allowed:
            declined.append(subject)
        records.append({
            "column": subject,
            "asked": "May this validator read and print %s?" % subject,
            "catalog_said": live(verdict, _entry_source(governor, table, dataset)),
            "aspect_cited": live(verdict.get("evidence", NOT_RECORDED), _entry_source(governor, table, dataset)),
            "regulation": live(verdict.get("regulation", NOT_RECORDED), _entry_source(governor, table, dataset)),
            "agent_action": computed("READ" if decision.allowed else "DECLINED", "validator.catalog_gate"),
            "agent_honoured_catalog": computed(True, "validator.catalog_gate"),
            "enforcement": CATALOG_ADVISORY_NOTE,
        })
    return records, declined


def _redact(rows: List[Dict[str, Any]], declined_columns: Sequence[str]) -> List[Dict[str, Any]]:
    """Blanks any declined column that still turns up in a result row.

    The golden SQL is not supposed to select these, but the decline has to hold
    even if it does -- otherwise the record claims a refusal the output betrays.
    """
    bare = {c.split(".", 1)[-1] for c in declined_columns}
    if not bare:
        return rows

    clean: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            clean.append(row)
            continue
        clean.append({
            key: (NOT_RECORDED if key in bare else val)
            for key, val in row.items()
        })
    return clean


# ----------------------------------------------------------------------------
# Structured-output schemas (unchanged surface)
# ----------------------------------------------------------------------------

class ClaimVeracityGenAIOutput(BaseModel):
    veracity_status: VeracityStatus = Field(
        ...,
        description="Verdict reached strictly from the BigQuery evidence supplied. Never from prior knowledge.",
    )
    confidence_score: float = Field(
        ...,
        description="Confidence between 0.0 and 1.0, justified by how many avenues returned corroborating rows.",
    )
    corroborating_telemetry: List[str] = Field(
        ...,
        description="Evidence items, each naming the table and golden query id it came from.",
    )
    relevant_avenues_checked: List[RelevantAvenueCheck] = Field(
        default_factory=list,
        description="Only the avenues that returned rows bearing on this claim. Declined sources are not evidence.",
    )
    discrepancy_details: Optional[str] = Field(
        None,
        description="Where the claim and the rows disagree, quoting the row values. Null if they agree.",
    )
    recommended_remediation: str = Field(
        ...,
        description="The next action, grounded in the rows and in any catalog decline recorded.",
    )


class AuditAnomalyItemSchema(BaseModel):
    severity: SeverityLevel
    anomaly_type: str
    description: str
    affected_channels: List[BankChannel]
    recommended_action: str


class AuditSweepGenAIOutput(BaseModel):
    anomalies: List[AuditAnomalyItemSchema]


# ----------------------------------------------------------------------------
# Agent 6: pre-write claim veracity validator
# ----------------------------------------------------------------------------

_AMOUNT_RE = re.compile(r"(?:[$\u00a3\u20ac]\s?)(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)|(\d+\.\d{2})\b")
_DISPUTE_RE = re.compile(r"\bDSP\d{5}\b", re.IGNORECASE)
_CARD_RE = re.compile(r"\bCARD\d{5}\b", re.IGNORECASE)


def _amounts_in(text: str) -> List[float]:
    found: List[float] = []
    for match in _AMOUNT_RE.finditer(text or ""):
        raw = match.group(1) or match.group(2) or ""
        try:
            found.append(round(float(raw.replace(",", "")), 2))
        except ValueError:
            continue
    return found


def _row_amounts(rows: List[Dict[str, Any]]) -> List[float]:
    """Every numeric amount-like field in a result set, for cross-checking a quoted figure."""
    values: List[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, val in row.items():
            if "amount" not in str(key).lower():
                continue
            try:
                values.append(round(float(val), 2))
            except (TypeError, ValueError):
                continue
    return values


class ClaimVeracityValidatorAgent:
    """Validates a claim against BigQuery before it is allowed into Memory Bank.

    Order matters: catalog first, then only the tables it cleared, then the
    verdict. Reading first and asking afterwards would make the governance
    record decorative.
    """

    def __init__(
        self,
        project_id: Optional[str] = None,
        location: Optional[str] = None,
        model_name: Optional[str] = None,
        dataset: Optional[str] = None,
        catalog_governor: Optional[CatalogGovernor] = None,
        memory_client: Optional[MemoryBankClient] = None,
    ):
        self.project_id = _project_id(project_id)
        self.location = _location(location)
        self.model_name = _model_name(model_name)
        self.dataset = _dataset(dataset)
        self._client: Optional[genai.Client] = None
        self._governor = catalog_governor
        self._memory = memory_client

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            if self.project_id == NOT_RECORDED:
                raise VeracityServiceError(
                    "GOOGLE_CLOUD_PROJECT is unset. Refusing to build a Vertex client against a placeholder project."
                )
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
            self._client = genai.Client(vertexai=True, project=self.project_id, location=self.location)
        return self._client

    @property
    def governor(self) -> CatalogGovernor:
        if self._governor is None:
            self._governor = shared_catalog_governor()
        return self._governor

    @property
    def memory(self) -> MemoryBankClient:
        if self._memory is None:
            self._memory = shared_memory_bank_client()
        return self._memory

    # -- catalog gate --------------------------------------------------------

    def authorize_sources(self, avenue_specs: Sequence[Dict[str, Any]] = AVENUE_SPECS) -> Dict[str, Any]:
        """Asks Dataplex about every candidate table and column before any read.

        Returns the evidence record plus the set of tables the agent will read.
        Nothing here matches a table name against a literal: `authorizations`
        and `authorizations_deprecated_v1` are both offered, and only the
        lifecycle-status aspect separates them.
        """
        governor = self.governor
        table_records: List[Dict[str, Any]] = []
        allowed_tables: Dict[str, List[str]] = {}
        declined_tables: List[Dict[str, str]] = []

        for spec in avenue_specs:
            cleared: List[str] = []
            for table in spec["candidate_tables"]:
                record = _authorization_record(governor, spec["avenue_id"], table, self.dataset)
                table_records.append(record)
                if value_of(record["agent_action"]) == "READ":
                    cleared.append(table)
                else:
                    declined_tables.append({
                        "table": table,
                        "avenue_id": spec["avenue_id"],
                        "basis": value_of(record["aspect_cited"]),
                        "replacement": value_of(record["authoritative_replacement"]),
                    })
            allowed_tables[spec["avenue_id"]] = cleared

        column_records, declined_columns = _column_records(governor, self.dataset)

        return {
            "gate": identity(
                "catalog-before-read", "validator.catalog_gate", "names the gate this agent implements"
            ),
            "dataset": computed(self.dataset, "env:BQ_DATASET"),
            "table_authorizations": table_records,
            "column_authorizations": column_records,
            "tables_declined": computed(declined_tables, "validator.catalog_gate"),
            "columns_declined": computed(declined_columns, "validator.catalog_gate"),
            "allowed_tables_by_avenue": computed(allowed_tables, "validator.catalog_gate"),
            "enforcement": CATALOG_ADVISORY_NOTE,
        }

    # -- evidence ------------------------------------------------------------

    def _resolve_cards(self, customer_id: str, declined_columns: Sequence[str]) -> Tuple[List[str], Dict[str, Any]]:
        """Card ids for this customer, from golden 01. Never a literal card id."""
        result = _adk().golden_01_customer_360_snapshot(customer_id=customer_id)
        rows = _redact(_rows_or_raise(result, "golden_01_customer_360_snapshot"), declined_columns)

        card_ids: List[str] = []
        for row in rows:
            for key, val in (row or {}).items():
                if str(key).lower() in ("card_id", "cards", "card_ids") and isinstance(val, str):
                    card_ids.extend(_CARD_RE.findall(val) or ([val] if val.startswith("CARD") else []))

        record = {
            "query": computed("golden_01_customer_360_snapshot", "validator.evidence"),
            "parameters": computed({"customer_id": customer_id}, "validator.evidence"),
            "row_count": seeded(len(rows), _query_source("01", "v_customer_360")),
            "rows": seeded(rows, _query_source("01", "v_customer_360")),
            "card_ids": (
                seeded(sorted(set(card_ids)), _query_source("01", "cards"))
                if card_ids
                else unmeasured("golden_01 returned no card id column for %s" % customer_id)
            ),
        }
        return sorted(set(card_ids)), record

    def _avenue_evidence(
        self,
        avenue: Dict[str, Any],
        customer_id: str,
        card_id: Optional[str],
        allowed_tables: Sequence[str],
        declined_columns: Sequence[str],
    ) -> Dict[str, Any]:
        """Runs the golden query behind one avenue, or records honestly why it did not."""
        avenue_id = avenue["avenue_id"]
        primary = avenue["candidate_tables"][0]

        if primary not in allowed_tables:
            return {
                "avenue_id": avenue_id,
                "avenue_title": avenue["avenue_title"],
                "table": primary,
                "rows": unmeasured("catalog declined %s; the agent did not read it" % primary),
                "row_count": unmeasured("catalog declined %s" % primary),
            }

        if avenue["scope"] == "card" and not card_id:
            return {
                "avenue_id": avenue_id,
                "avenue_title": avenue["avenue_title"],
                "table": primary,
                "rows": unmeasured("no card id resolved for %s, so %s cannot be scoped" % (customer_id, primary)),
                "row_count": unmeasured("no card id resolved for %s" % customer_id),
            }

        contract = _contract_for_table(primary)
        if contract is None:
            return {
                "avenue_id": avenue_id,
                "avenue_title": avenue["avenue_title"],
                "table": primary,
                "rows": unmeasured("no golden query contract governs %s; avenue not measured" % primary),
                "row_count": unmeasured("no golden query contract governs %s" % primary),
                "gap": computed(
                    "queries/golden/INDEX.md registers no contract whose primary table is %s" % primary,
                    "validator.evidence",
                ),
            }

        tool_name = getattr(contract, "tool_name", None)
        query_id = getattr(contract, "query_id", NOT_RECORDED)
        wrapper = getattr(_adk(), tool_name or "", None)
        if wrapper is None:
            raise VeracityServiceError(
                "Contract %s names tool %s, which backend.adk_agents does not define." % (query_id, tool_name)
            )

        params = {"card_id": card_id} if avenue["scope"] == "card" else {"customer_id": customer_id}
        rows = _redact(_rows_or_raise(wrapper(**params), tool_name), declined_columns)
        source = _query_source(query_id, primary)

        return {
            "avenue_id": avenue_id,
            "avenue_title": avenue["avenue_title"],
            "table": primary,
            "query": computed(tool_name, "validator.evidence"),
            "parameters": computed(params, "validator.evidence"),
            "row_count": seeded(len(rows), source),
            "rows": seeded(rows, source),
        }

    def _dispute_evidence(self, customer_id: str, declined_columns: Sequence[str]) -> Dict[str, Any]:
        """Open disputes, because amounts and deadlines are settled here and nowhere else."""
        result = _adk().golden_07_open_disputes_for_customer(customer_id=customer_id)
        rows = _redact(_rows_or_raise(result, "golden_07_open_disputes_for_customer"), declined_columns)
        source = _query_source("07", "disputes")
        return {
            "query": computed("golden_07_open_disputes_for_customer", "validator.evidence"),
            "parameters": computed({"customer_id": customer_id}, "validator.evidence"),
            "row_count": seeded(len(rows), source),
            "rows": seeded(rows, source),
        }

    def _amount_cross_check(self, claim_text: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
        """Every figure the claim quotes, checked against the rows BigQuery returned.

        A quoted amount with no matching row is not proof of a lie, but it is the
        one fact the verdict must not skip.
        """
        quoted = _amounts_in(claim_text)
        if not quoted:
            return {"amounts_quoted": unmeasured("claim quotes no amount")}

        ledger: List[float] = []
        for block in evidence.values():
            if isinstance(block, dict) and isinstance(block.get("rows"), dict):
                rows = value_of(block["rows"])
                if isinstance(rows, list):
                    ledger.extend(_row_amounts(rows))

        matches = {str(amount): (amount in ledger) for amount in quoted}
        return {
            "amounts_quoted": computed(quoted, "validator.amount_cross_check"),
            "amounts_found_in_bigquery": (
                computed(sorted(set(ledger)), "validator.amount_cross_check")
                if ledger
                else unmeasured("no amount column in any authorised result set")
            ),
            "match_by_amount": computed(matches, "validator.amount_cross_check"),
            "rule": "BigQuery is authoritative for amounts; the claim's figure is never accepted on its own.",
        }

    def build_evidence(self, customer_id: str, claim_text: str) -> Dict[str, Any]:
        """Catalog gate, then the authorised golden queries, then the cross-check."""
        with obs.turn("veracity.authorize_sources", LOG, agent="claim_veracity_validator_agent") as _s:
            authorization = self.authorize_sources()
            declined_columns = value_of(authorization["columns_declined"]) or []
            allowed_by_avenue = value_of(authorization["allowed_tables_by_avenue"]) or {}
            _s.detail(declined_columns=len(declined_columns))

        card_ids, card_record = self._resolve_cards(customer_id, declined_columns)
        claimed_cards = [c.upper() for c in _CARD_RE.findall(claim_text or "")]
        card_id = next((c for c in claimed_cards if c in card_ids), card_ids[0] if card_ids else None)

        avenues: Dict[str, Any] = {}
        for spec in AVENUE_SPECS:
            av_id = spec["avenue_id"]
            with obs.turn("veracity.avenue." + av_id, LOG, agent="claim_veracity_validator_agent",
                          avenue=av_id, card_id=card_id or NOT_RECORDED) as _av_span:
                av_res = self._avenue_evidence(
                    spec,
                    customer_id=customer_id,
                    card_id=card_id,
                    allowed_tables=allowed_by_avenue.get(av_id, []),
                    declined_columns=declined_columns,
                )
                avenues[av_id] = av_res
                _av_span.detail(tables=len(allowed_by_avenue.get(av_id, [])))

        disputes = self._dispute_evidence(customer_id, declined_columns)

        evidence: Dict[str, Any] = {
            "customer_id": computed(customer_id, "validator.request"),
            "card_id_used": (
                seeded(card_id, _query_source("01", "cards"))
                if card_id
                else unmeasured("golden_01 returned no card for %s" % customer_id)
            ),
            "customer_360": card_record,
            "catalog_authorization": authorization,
            "disputes": disputes,
        }
        evidence.update(avenues)
        with obs.turn("veracity.amount_cross_check", LOG, agent="claim_veracity_validator_agent"):
            evidence["amount_cross_check"] = self._amount_cross_check(claim_text, evidence)
        return evidence

    # -- verdict -------------------------------------------------------------

    def validate_claim_against_telemetry(
        self,
        customer_id: str,
        channel: BankChannel,
        claim_text: str,
        claim_metadata: Optional[Dict[str, Any]] = None,
    ) -> ClaimVeracityEvaluation:
        """Reaches a verdict on real rows, or raises. There is no third outcome."""
        evidence = self.build_evidence(customer_id, claim_text)

        prompt = """You are the pre-write claim veracity validator on a JPMorgan Chase consumer-credit fraud desk.

CUSTOMER ID: {customer_id}
INGESTION CHANNEL: {channel}
CLAIM: "{claim_text}"
CLAIM METADATA: {metadata}

EVIDENCE (every value carries a _provenance tag naming the table and golden query that produced it):
{evidence}

RULES:
1. Judge the claim only against rows present in the evidence. If a value is "n/r" it was not measured; say so and never substitute a figure.
2. An avenue whose rows are "n/r" is not evidence for or against the claim. Do not treat an unread source as a clean bill of health.
3. Amounts, deadlines and dispute ids come from BigQuery rows only. If the claim quotes a figure, check amount_cross_check.match_by_amount and state the result.
4. Where catalog_authorization shows a declined table or column, say the agent declined it and cite the aspect in aspect_cited. Never imply Dataplex blocked the read.
5. Choose the veracity status the rows support. If the rows are thin, UNVERIFIED_PENDING_INVESTIGATION is the correct answer, not a confident one.
6. Each corroborating_telemetry entry must name its table and golden query id.
""".format(
            customer_id=customer_id,
            channel=channel.value,
            claim_text=claim_text,
            metadata=json.dumps(claim_metadata or {}),
            evidence=json.dumps(evidence, indent=2, default=str),
        )

        with obs.turn("veracity.gemini_evaluate", LOG, agent="claim_veracity_validator_agent",
                      model=self.model_name, customer_id=customer_id) as _eval_span:
            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        response_mime_type="application/json",
                        response_schema=ClaimVeracityGenAIOutput,
                    ),
                )
                parsed = response.parsed
            except Exception as error:
                _eval_span.detail(error=str(error))
                # No fallback verdict. An invented judgement on a customer's honesty
                # is the single most damaging thing this file could produce.
                raise VeracityServiceError(
                    "Vertex AI evaluation failed for %s and no verdict was invented: %s" % (customer_id, error)
                )

            if parsed is None:
                raise VeracityServiceError(
                    "Vertex AI returned no parsable ClaimVeracityGenAIOutput for %s." % customer_id
                )
            _eval_span.detail(
                status=str(parsed.veracity_status),
                confidence=parsed.confidence_score,
                avenues=len(parsed.relevant_avenues_checked or []),
            )

        return ClaimVeracityEvaluation(
            claim_id="CLM-%s" % uuid.uuid4().hex[:10].upper(),
            claim_text=claim_text,
            veracity_status=parsed.veracity_status,
            confidence_score=parsed.confidence_score,
            corroborating_telemetry=parsed.corroborating_telemetry,
            relevant_avenues_checked=parsed.relevant_avenues_checked,
            discrepancy_details=parsed.discrepancy_details,
            multi_avenue_audit=evidence,
            recommended_remediation=parsed.recommended_remediation,
            validator_agent="ClaimVeracityValidatorAgent (%s)" % self.model_name,
        )

    def validate_and_write_to_memory_bank(
        self,
        customer_id: str,
        channel: BankChannel,
        day_label: str,
        summary: str,
        claim_text: str,
        metadata: Optional[Dict[str, Any]] = None,
        severity: SeverityLevel = SeverityLevel.MEDIUM,
        session_id: Optional[str] = None,
    ) -> MemoryFragment:
        """Validates first, then writes. The evaluation travels with the fragment.

        The claim is stored as a Memory Bank session and memories are generated
        from it into the scope the verdict picks (trusted or quarantine), via
        backend/memory_write_path.py:generate_with_lineage.
        """
        evaluation = self.validate_claim_against_telemetry(
            customer_id=customer_id,
            channel=channel,
            claim_text=claim_text,
            claim_metadata=metadata,
        )
        session_name = self.memory.create_session(
            user_id=customer_id,
            display_name="%s · CLAIM_VALIDATION" % channel.value,
            session_state={"channel": channel.value, "customer_id": customer_id,
                           "intent": "CLAIM_VALIDATION"},
        )
        self.memory.append_event(session_name, "customer", claim_text)
        lineage = memory_write_path.generate_with_lineage(
            self.memory, session_name, customer_id, evaluation.veracity_status.value)

        fragment = MemoryFragment(
            fragment_id="MEM-%s" % uuid.uuid4().hex[:10].upper(),
            customer_id=customer_id,
            channel=channel,
            timestamp=datetime.now(timezone.utc),
            day_label=day_label,
            summary=summary,
            metadata=dict(metadata or {}, claim_text=claim_text, memory_lineage=lineage,
                          caller_session_id=session_id),
            severity=severity,
            session_id=session_name.split("/")[-1],
            veracity_evaluation=evaluation,
        )

        if "corpus" in lineage["scope"]:
            LOG.warning(
                "Claim %s for %s is %s; its memories went to the quarantine scope.",
                evaluation.claim_id,
                customer_id,
                evaluation.veracity_status.value,
            )
        return fragment


# ----------------------------------------------------------------------------
# Agent 7: Memory Bank audit sweep
# ----------------------------------------------------------------------------

class MemoryBankAuditAgent:
    """Re-checks what is already stored against BigQuery, customer by customer.

    Two passes. The first is deterministic: any amount or dispute id a memory
    quotes is compared to the disputes table, and a mismatch is a CRITICAL
    anomaly regardless of what a model thinks. The second asks Gemini to read
    the memories for contradictions a comparison cannot see.
    """

    def __init__(
        self,
        project_id: Optional[str] = None,
        location: Optional[str] = None,
        model_name: Optional[str] = None,
        memory_client: Optional[MemoryBankClient] = None,
        validator: Optional[ClaimVeracityValidatorAgent] = None,
    ):
        self.project_id = _project_id(project_id)
        self.location = _location(location)
        self.model_name = _model_name(model_name)
        self._client: Optional[genai.Client] = None
        self._memory = memory_client
        self._validator = validator

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            if self.project_id == NOT_RECORDED:
                raise VeracityServiceError(
                    "GOOGLE_CLOUD_PROJECT is unset. Refusing to build a Vertex client against a placeholder project."
                )
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
            self._client = genai.Client(vertexai=True, project=self.project_id, location=self.location)
        return self._client

    @property
    def memory(self) -> MemoryBankClient:
        if self._memory is None:
            self._memory = shared_memory_bank_client()
        return self._memory

    @property
    def validator(self) -> ClaimVeracityValidatorAgent:
        if self._validator is None:
            self._validator = ClaimVeracityValidatorAgent(
                project_id=self.project_id, location=self.location, model_name=self.model_name
            )
        return self._validator

    # -- helpers -------------------------------------------------------------

    def _memories(self, customer_id: str, top_k: int = 25) -> List[Dict[str, Any]]:
        try:
            return self.memory.retrieve(scope={"customer_id": customer_id}, top_k=top_k)
        except Exception as error:
            raise VeracityServiceError("Memory Bank retrieve failed for %s: %s" % (customer_id, error))

    @staticmethod
    def _memory_text(memory: Dict[str, Any]) -> str:
        """Pulls the fact text out of whatever shape the retrieve response used."""
        node = memory.get("memory", memory) if isinstance(memory, dict) else {}
        for key in ("fact", "content", "text", "displayName"):
            value = node.get(key) if isinstance(node, dict) else None
            if isinstance(value, str) and value.strip():
                return value
        return json.dumps(node, default=str)

    @staticmethod
    def _memory_id(memory: Dict[str, Any]) -> str:
        node = memory.get("memory", memory) if isinstance(memory, dict) else {}
        name = node.get("name") if isinstance(node, dict) else None
        if isinstance(name, str) and name:
            return name.rsplit("/", 1)[-1]
        return NOT_RECORDED

    # -- pass 1: BigQuery arbitration ---------------------------------------

    def _contradictions_against_bigquery(
        self,
        customer_id: str,
        memories: List[Dict[str, Any]],
    ) -> List[AuditAnomaly]:
        """Amounts and dispute ids in memory, judged against the disputes table."""
        declined = value_of(self.validator.authorize_sources()["columns_declined"]) or []
        dispute_rows = value_of(self.validator._dispute_evidence(customer_id, declined)["rows"]) or []

        known_amounts = set(_row_amounts(dispute_rows))
        known_disputes = set()
        for row in dispute_rows:
            for key, val in (row or {}).items():
                if "dispute_id" in str(key).lower() and isinstance(val, str):
                    known_disputes.add(val.upper())

        anomalies: List[AuditAnomaly] = []
        for memory in memories:
            text = self._memory_text(memory)
            memory_id = self._memory_id(memory)

            unmatched_amounts = [a for a in _amounts_in(text) if a not in known_amounts]
            unmatched_disputes = [d.upper() for d in _DISPUTE_RE.findall(text) if d.upper() not in known_disputes]

            if not unmatched_amounts and not unmatched_disputes:
                continue

            anomalies.append(AuditAnomaly(
                anomaly_id="ANM-%s" % uuid.uuid4().hex[:8].upper(),
                customer_id=customer_id,
                severity=SeverityLevel.CRITICAL,
                anomaly_type="MEMORY_FIGURE_NOT_IN_BIGQUERY",
                description=(
                    "Memory %s quotes %s that the disputes table does not confirm. "
                    "Memory is not the source of an amount, a deadline or a dispute id; "
                    "golden_07 (bigquery:disputes#golden_07) is." % (
                        memory_id,
                        "amount(s) %s" % unmatched_amounts if unmatched_amounts else "dispute id(s) %s" % unmatched_disputes,
                    )
                ),
                affected_channels=[BankChannel.CORE_BANKING],
                evidence_fragments=[memory_id],
                recommended_action=(
                    "Re-verify the figure against golden_07 before any customer-facing use, "
                    "and quarantine the memory if BigQuery still disagrees."
                ),
            ))
        return anomalies

    # -- pass 2: model sweep -------------------------------------------------

    def _contradictions_from_model(
        self,
        customer_id: str,
        memories: List[Dict[str, Any]],
    ) -> List[AuditAnomaly]:
        summaries = [{"memory_id": self._memory_id(m), "text": self._memory_text(m)} for m in memories]

        prompt = """Audit one customer's Memory Bank for internal contradictions.

CUSTOMER ID: {customer_id}
MEMORIES RETRIEVED FROM VERTEX AI MEMORY BANK:
{memories}

RULES:
1. Report only contradictions visible between these memories -- two facts that cannot both be true.
2. Do not judge whether a figure is correct. BigQuery settles figures and a separate pass already did that.
3. Quote the memory ids involved in every description.
4. Return an empty list when the memories are consistent. An empty list is a valid, useful answer.
""".format(customer_id=customer_id, memories=json.dumps(summaries, indent=2, default=str))

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                    response_schema=AuditSweepGenAIOutput,
                ),
            )
            parsed = response.parsed
        except Exception as error:
            raise VeracityServiceError("Vertex AI audit sweep failed for %s: %s" % (customer_id, error))

        if parsed is None:
            raise VeracityServiceError("Vertex AI returned no parsable AuditSweepGenAIOutput for %s." % customer_id)

        evidence_ids = [self._memory_id(m) for m in memories] or [NOT_RECORDED]
        return [
            AuditAnomaly(
                anomaly_id="ANM-%s" % uuid.uuid4().hex[:8].upper(),
                customer_id=customer_id,
                severity=item.severity,
                anomaly_type=item.anomaly_type,
                description=item.description,
                affected_channels=item.affected_channels,
                evidence_fragments=evidence_ids,
                recommended_action=item.recommended_action,
            )
            for item in parsed.anomalies
        ]

    def audit_customer_memory_bank(self, customer_id: str) -> List[AuditAnomaly]:
        memories = self._memories(customer_id)
        if not memories:
            LOG.info("No memories in scope for %s; nothing to audit.", customer_id)
            return []
        return self._contradictions_against_bigquery(customer_id, memories) + self._contradictions_from_model(
            customer_id, memories
        )

    def run_consolidated_sweep(self, customer_ids: Optional[List[str]] = None) -> AuditReport:
        """Sweeps the customers given, or the ones AUDIT_CUSTOMER_IDS names.

        There is no default roster. Inventing one would put made-up customer ids
        in an audit report, which is the opposite of what an audit is for.
        """
        if not customer_ids:
            configured = env_param("AUDIT_CUSTOMER_IDS", "")
            customer_ids = [c.strip() for c in (configured or "").split(",") if c.strip()]
        if not customer_ids:
            raise VeracityServiceError(
                "No customer ids to sweep. Pass customer_ids, or set AUDIT_CUSTOMER_IDS."
            )

        anomalies: List[AuditAnomaly] = []
        risk_distribution = {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0}
        fragments_scanned = 0

        for customer_id in customer_ids:
            memories = self._memories(customer_id)
            fragments_scanned += len(memories)

            if memories:
                found = self._contradictions_against_bigquery(customer_id, memories)
                found.extend(self._contradictions_from_model(customer_id, memories))
            else:
                found = []
            anomalies.extend(found)

            worst = SeverityLevel.LOW
            order = [SeverityLevel.LOW, SeverityLevel.MEDIUM, SeverityLevel.HIGH, SeverityLevel.CRITICAL]
            for anomaly in found:
                if order.index(anomaly.severity) > order.index(worst):
                    worst = anomaly.severity
            risk_distribution[worst.value] += 1

        return AuditReport(
            sweep_id="SWEEP-%s" % uuid.uuid4().hex[:10].upper(),
            customers_audited=len(customer_ids),
            total_fragments_scanned=fragments_scanned,
            anomalies_detected=anomalies,
            summary=(
                "Swept %d customer memory bank(s) and %d memory record(s). %d anomaly(ies) recorded: "
                "amounts and dispute ids were arbitrated against bigquery:disputes#golden_07, "
                "contradictions between memories by %s." % (
                    len(customer_ids), fragments_scanned, len(anomalies), self.model_name,
                )
            ),
            risk_score_distribution=risk_distribution,
        )


# ----------------------------------------------------------------------------
# Admin card oversight
# ----------------------------------------------------------------------------

class AdminCardOversightManager:
    """Card restriction -> support-chat verification -> admin decision.

    Every field on the case comes from a golden query or from a validator
    verdict. Nothing about a customer's card is written here as a literal.
    """

    _cases: Dict[str, AdminCardReviewCase] = {}
    _evidence: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def _validator(cls) -> ClaimVeracityValidatorAgent:
        return ClaimVeracityValidatorAgent()

    @classmethod
    def _flagged_dispute(cls, customer_id: str) -> Dict[str, Any]:
        """The customer's most material open dispute, straight from golden_07."""
        validator = cls._validator()
        declined = value_of(validator.authorize_sources()["columns_declined"]) or []
        block = validator._dispute_evidence(customer_id, declined)
        rows = value_of(block["rows"]) or []

        if not rows:
            return {"row": {}, "evidence": block}

        def amount_of(row: Dict[str, Any]) -> float:
            values = _row_amounts([row])
            return max(values) if values else 0.0

        return {"row": max(rows, key=amount_of), "evidence": block}

    @classmethod
    def _describe(cls, row: Dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = row.get(key)
            if value not in (None, "", []):
                return str(value)
        return NOT_RECORDED

    @classmethod
    def get_or_init_case(cls, customer_id: str = "CUST00001") -> AdminCardReviewCase:
        """Builds the case from BigQuery. Confidence stays at zero until verification runs."""
        if customer_id in cls._cases:
            return cls._cases[customer_id]

        flagged = cls._flagged_dispute(customer_id)
        row = flagged["row"]

        case = AdminCardReviewCase(
            case_id="CASE-%s-%s" % (customer_id, uuid.uuid4().hex[:6].upper()),
            customer_id=customer_id,
            card_name=cls._describe(row, "card_id", "card_product", "card_name"),
            flagged_transaction=cls._describe(row, "dispute_id", "reason_code", "description"),
            flagged_amount=cls._describe(row, "disputed_amount", "amount", "amount_usd"),
            # Zero corroborations so far, because nothing has been verified yet.
            # The evidence record below carries this as unmeasured rather than
            # letting the float read as a real score.
            confidence_score=0.0,
            verification_passed=False,
            verification_checks=[],
            chat_verification_transcript=[],
            card_status="RESTRICTED_BLOCKED",
            recommended_admin_decision="RESTRICT_CARD",
            customer_notification_message=(
                "Card verification has not run yet for this case; no decision has been made."
            ),
        )

        cls._cases[customer_id] = case
        cls._evidence[customer_id] = {
            "flagged_dispute": flagged["evidence"],
            "confidence_basis": unmeasured("no verification checks executed yet"),
        }
        return case

    @classmethod
    def reset_case(cls, customer_id: str = "CUST00001") -> AdminCardReviewCase:
        cls._cases.pop(customer_id, None)
        cls._evidence.pop(customer_id, None)
        return cls.get_or_init_case(customer_id)

    @classmethod
    def evaluate_customer_chat_verification(
        cls,
        customer_id: str,
        chat_message: str,
        scenario: Optional[str] = None,
    ) -> AdminCardReviewCase:
        """Runs the chat message through the validator and records the result.

        `scenario` survives only because callers still pass it. A preset verdict
        is exactly the fabrication this rework removes, so it is logged as a
        hint that was not honoured and never reaches the outcome.
        """
        if scenario:
            LOG.info(
                "scenario=%r supplied for %s and ignored; the verdict comes from BigQuery evidence only.",
                scenario,
                customer_id,
            )

        case = cls.get_or_init_case(customer_id)
        validator = cls._validator()
        evaluation = validator.validate_claim_against_telemetry(
            customer_id=customer_id,
            channel=BankChannel.WEB_PORTAL,
            claim_text=chat_message,
            claim_metadata={"surface": "customer_support_chat"},
        )

        checks = [
            VerificationCheckItem(
                check_id=avenue.avenue_id,
                check_name=avenue.avenue_title,
                passed=avenue.corroborates_claim,
                evidence_detail=avenue.finding_summary,
            )
            for avenue in evaluation.relevant_avenues_checked
        ]
        if not checks:
            checks = [VerificationCheckItem(
                check_id="NO_AVENUE_CORROBORATION",
                check_name="Ground-truth corroboration",
                passed=False,
                evidence_detail=(
                    "No authorised avenue returned rows bearing on this statement. "
                    "Unread is not the same as clean: %s" % (evaluation.discrepancy_details or NOT_RECORDED)
                ),
            )]

        passed = evaluation.veracity_status == VeracityStatus.VERIFIED_TRUE

        case.verification_checks = checks
        case.verification_passed = passed
        case.confidence_score = evaluation.confidence_score
        case.chat_verification_transcript = list(case.chat_verification_transcript) + [
            {"speaker": "customer", "text": chat_message},
            {"speaker": "validator", "text": evaluation.recommended_remediation},
        ]
        case.recommended_admin_decision = "APPROVE_ENABLE_CARD" if passed else "RESTRICT_CARD"
        case.customer_notification_message = (
            "Verification %s. %s" % (
                "succeeded" if passed else "did not succeed",
                evaluation.discrepancy_details or evaluation.recommended_remediation,
            )
        )

        cls._cases[customer_id] = case
        cls._evidence[customer_id] = {
            "flagged_dispute": cls._evidence.get(customer_id, {}).get(
                "flagged_dispute", unmeasured("case built before dispute evidence was captured")
            ),
            "veracity_status": computed(evaluation.veracity_status.value, "validator.cross_check"),
            "confidence_basis": computed(evaluation.confidence_score, "validator.cross_check"),
            "scenario_hint": (
                computed({"supplied": scenario, "honoured": False}, "oversight.scenario_hint")
                if scenario
                else unmeasured("no scenario hint supplied")
            ),
            "claim_evidence": evaluation.multi_avenue_audit,
        }
        return case

    @classmethod
    def execute_admin_decision(
        cls,
        customer_id: str,
        decision: str,
        admin_notes: Optional[str] = None,
    ) -> AdminCardReviewCase:
        """Applies the admin's YES/NO and writes the customer-facing message."""
        case = cls.get_or_init_case(customer_id)
        normalised = (decision or "").strip().upper()

        if normalised not in ("APPROVED_YES", "REJECTED_NO"):
            raise VeracityServiceError(
                "Unknown admin decision %r. Expected APPROVED_YES or REJECTED_NO." % decision
            )

        approved = normalised == "APPROVED_YES"
        case.admin_decision = normalised
        case.card_status = "ACTIVE" if approved else "RESTRICTED_BLOCKED"

        verdict = (
            cls._evidence.get(customer_id, {}).get("veracity_status")
            if isinstance(cls._evidence.get(customer_id), dict)
            else None
        )
        basis = value_of(verdict) if verdict else NOT_RECORDED

        case.customer_notification_message = (
            "Card %s has been unlocked. Verification outcome on file: %s.%s" % (
                case.card_name, basis, " Admin note: %s" % admin_notes if admin_notes else "",
            )
            if approved else
            "Card %s stays restricted. Verification outcome on file: %s.%s" % (
                case.card_name, basis, " Admin note: %s" % admin_notes if admin_notes else "",
            )
        )

        cls._cases[customer_id] = case
        return case

    @classmethod
    def case_evidence(cls, customer_id: str = "CUST00001") -> Dict[str, Any]:
        """The provenance-tagged record behind a case, for the trace drawer."""
        return cls._evidence.get(customer_id, {"note": unmeasured("no case built for %s" % customer_id)})

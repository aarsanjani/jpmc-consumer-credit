"""
models.py - Domain & Governance Data Models for the JPMC CCB Fraud & Disputes Desk.

Models the live 8-agent hub-and-spoke architecture across:
1. Vertex AI Agent Engine Memory Bank (`MemoryScope`, `SessionTurnRecord`, `ExtractedMemoryRecord`)
2. Dataplex Universal Catalog Governance (`CatalogAspectVerdict`, `GlossaryTermDefinition`, `DataAccessAuthorization`)
3. BigQuery Parameterized Golden Query Contracts (`GoldenQueryParameter`, `GoldenQueryContract`, `GoldenQueryExecutionResult`)
4. Regulation E vs Regulation Z Dual-Rulebook Disputes (`RegulationPath`, `DisputeFilingWindowVerdict`, `FraudDeskCaseSummary`)
5. 8-Agent Execution Spans & Audit Trace (`AgentId`, `SpanKind`, `AgentExecutionSpan`, `AgentTraceRecord`)
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

NOT_RECORDED = "n/r"


# ==============================================================================
# 1. Core Enums: Regulation Paths, Agent Topology, & Governance Actions
# ==============================================================================

class RegulationPath(str, Enum):
    """Governing US federal regulation path for a payment dispute."""
    REG_E = "REG_E"  # 12 CFR 1005 — Electronic Fund Transfers (Debit / Checking / Savings)
    REG_Z = "REG_Z"  # 12 CFR 1026.13 — Truth in Lending Act (Credit Cards)


class AgentId(str, Enum):
    """The 8 specialized agents in the CCB Fraud & Disputes Desk (`backend/adk_agents.py:list_all_agents`)."""
    FRAUD_MONITORING = "01_fraud_monitoring_agent"
    TELEPHONY_IVR = "02_telephony_ivr_agent"
    MOBILE_APP = "03_mobile_app_agent"
    WEB_PORTAL = "04_web_portal_agent"
    BRANCH_SUPPORT = "05_branch_support_agent"
    CLAIM_VERACITY_VALIDATOR = "06_claim_veracity_validator_agent"
    MEMORY_BANK_AUDIT = "07_memory_bank_audit_agent"
    CONSUMER_CREDIT_SYNTHESIZER = "08_consumer_credit_synthesizer_agent"

    # Backward-compatible aliases mapping legacy 6-agent roles to the 8-agent roster
    ORCHESTRATOR = "08_consumer_credit_synthesizer_agent"
    IDENTITY_CONTEXT = "05_branch_support_agent"
    FRAUD_TRIAGE = "01_fraud_monitoring_agent"
    POLICY_COMPLIANCE = "04_web_portal_agent"
    RESOLUTION_ACTIONS = "03_mobile_app_agent"
    MEMORY_WRITER = "08_consumer_credit_synthesizer_agent"


class CatalogAction(str, Enum):
    """Machine-executable governance instruction from Dataplex aspect `agent_behavior`."""
    ALLOW = "ALLOW"
    ALLOW_TO_OWNER_ONLY = "ALLOW_TO_OWNER_ONLY"
    REFUSE = "REFUSE"
    MASK = "MASK"
    UNKNOWN = "UNKNOWN"


class LifecycleState(str, Enum):
    """Dataplex `lifecycle-status` aspect values."""
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    RETIRED = "RETIRED"
    QUARANTINED = "QUARANTINED"


class SpanKind(str, Enum):
    """Trace span classifications rendered in the Black Bar Trace Drawer."""
    TOOL = "tool"
    THINK = "think"
    DENY = "deny"
    OUT = "out"


# ==============================================================================
# 2. Vertex AI Memory Bank Models (Session -> Consolidation -> Scoped Retrieval)
# ==============================================================================

class MemoryScope(BaseModel):
    """Customer-level isolation scope passed per request (`{"customer_id": "CUST00001"}`).

    Why scope is per-request rather than hardcoded on the client:
    A single `MemoryBankClient(project, engine_id, location)` serves all customers
    on the Reasoning Engine. `customer_id` is passed inside `scope` on `retrieve()`
    and `generate_from_session()` so memories never bleed across customers.
    """
    customer_id: str = Field(..., description="Customer identifier, e.g. CUST00001")

    def to_api_scope(self) -> Dict[str, str]:
        return {"customer_id": self.customer_id}


class SessionTurnRecord(BaseModel):
    """A timestamped conversation turn appended to a Vertex Session (`sessions:appendEvent`)."""
    role: str = Field(..., description="'user' or 'agent'/'model'")
    text: str = Field(..., description="Literal utterance text")
    timestamp: datetime = Field(
        ...,
        description="RFC3339 event timestamp so Memory Bank resolves temporal contradictions",
    )
    invocation_id: Optional[str] = None


class ExtractedMemoryRecord(BaseModel):
    """A consolidated fact returned by `memories:retrieve` from Vertex AI Memory Bank."""
    memory_name: str = Field(default=NOT_RECORDED, description="Full Vertex resource path")
    fact: str = Field(default=NOT_RECORDED, description="Extracted fact statement")
    update_time: str = Field(default=NOT_RECORDED, description="Server-side consolidation timestamp")
    distance: Any = Field(default=NOT_RECORDED, description="Semantic distance if similaritySearchParams used")
    topics: List[str] = Field(default_factory=list, description="Assigned memory topic labels")
    scope: Dict[str, str] = Field(default_factory=dict)


# ==============================================================================
# 3. Dataplex Universal Catalog Governance Models
# ==============================================================================

class CatalogAspectVerdict(BaseModel):
    """A live governance decision derived at call time from Dataplex aspects."""
    allowed: bool
    action: str = Field(..., description="ALLOW | ALLOW_TO_OWNER_ONLY | REFUSE | MASK | UNKNOWN")
    subject: str = Field(..., description="Table (`authorizations_deprecated_v1`) or column (`cards.pan_full`)")
    reason: str = Field(default=NOT_RECORDED, description="Human-readable rationale from the aspect")
    evidence: str = Field(
        default=NOT_RECORDED,
        description="Exact aspect and field that triggered the decision, e.g. 'lifecycle-status.status = DEPRECATED'",
    )
    replacement: Optional[str] = Field(
        default=None,
        description="Authoritative replacement table or safe alternative column (`authorizations` or `pan_masked`)",
    )
    regulation: str = Field(default=NOT_RECORDED, description="Governing regulatory citation (PCI DSS, GLBA, Reg E/Z)")


class DataAccessAuthorization(BaseModel):
    """Composite pre-query check returned by `CatalogGovernor.authorize(table, columns)`."""
    table: str
    allowed: bool
    table_decision: CatalogAspectVerdict
    column_decisions: List[CatalogAspectVerdict] = Field(default_factory=list)
    blocked_columns: List[str] = Field(default_factory=list)
    safe_columns_to_select: List[str] = Field(default_factory=list)
    governance_note: str = (
        "The catalog does not block this query by IAM brute force; it supplies "
        "the metadata basis for the agent to voluntarily decline and cite why."
    )


class GlossaryTermDefinition(BaseModel):
    """A business glossary term fetched from Dataplex (`ccb-fraud-disputes`)."""
    term_id: str
    display_name: str = NOT_RECORDED
    definition: str = NOT_RECORDED
    owner: str = "CCB Disputes Policy"
    linked_columns: List[str] = Field(default_factory=list)


# ==============================================================================
# 4. BigQuery Parameterized Golden Query Contract Models (`queries/golden/INDEX.md`)
# ==============================================================================

class GoldenQueryContract(BaseModel):
    """Contract binding one `.sql` file in `queries/golden/` to a callable agent tool."""
    query_id: str = Field(..., description="Two-digit query ID ('01'..'12')")
    sql_filename: str = Field(..., description="Filename inside queries/golden/, e.g. '03_suspicious_activity_on_card_window.sql'")
    tool_name: str = Field(..., description="Callable ADK function tool name, e.g. 'golden_03_suspicious_activity'")
    plain_english_question: str = Field(..., description="Question answered by this golden query")
    authorized_agents: List[AgentId] = Field(..., description="Which of the 6 agents are allowed to call this query")
    required_parameters: List[str] = Field(..., description="Named `@parameters` bound via BigQuery QueryJobConfig")
    governed_tables: List[str] = Field(..., description="BigQuery tables checked via CatalogGovernor before execution")
    governed_columns_checked: List[str] = Field(
        default_factory=list,
        description="Columns verified against sensitivity-class aspects before running",
    )
    forbidden_traps_avoided: List[str] = Field(
        default_factory=lambda: [
            "Never selects customers.ssn_last4 (sensitivity-class.agent_behavior = REFUSE)",
            "Never selects cards.pan_full (sensitivity-class.agent_behavior = REFUSE)",
            "Never reads authorizations_deprecated_v1 (lifecycle-status.status = DEPRECATED)",
        ],
    )


class GoldenQueryExecutionResult(BaseModel):
    """Audit record produced when an agent invokes a Golden Query tool."""
    query_id: str
    tool_name: str
    calling_agent: AgentId
    parameters_bound: Dict[str, Any]
    catalog_verdict: Dict[str, Any]
    voluntary_refusals_logged: List[Dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    ad_hoc_sql_used: bool = False


# ==============================================================================
# 5. Reg E vs Reg Z Dual-Dispute Case & 6-Agent Trace Models
# ==============================================================================

class DisputeResolutionSide(BaseModel):
    """One side of the dual-rulebook Lisbon stolen wallet case (`DSP00033` vs `DSP00034`)."""
    dispute_id: str
    card_id: str
    product_type: str = Field(..., description="'DEBIT' (Checking) or 'CREDIT'")
    regulation_path: RegulationPath
    cfr_citation: str = Field(..., description="'12 CFR 1005 (Reg E)' or '12 CFR 1026.13 (Reg Z)'")
    disputed_amount_usd: float
    customer_liability_cap_usd: float
    provisional_credit_Applicable: bool
    provisional_credit_due_date: str = Field(
        default="None — Regulation Z withholds collection instead of issuing provisional credit",
    )
    clock_basis: str = Field(..., description="'10 business days (Reg E)' vs '2 billing cycles / max 90 days (Reg Z)'")
    filing_window_status: str = "TIMELY"


class AgentExecutionSpan(BaseModel):
    """Single step inside an agent's execution trace."""
    offset_ts: str
    kind: SpanKind
    detail: str
    duration_ms: str
    nested: bool = False


class AgentTraceRecord(BaseModel):
    """Full execution summary for one of the 6 CCB Fraud Desk agents."""
    agent_id: AgentId
    step_index: str
    display_name: str
    state: str = "done"
    duration_sec: str
    refusal_count: int = 0
    ad_hoc_sql_count: int = 0
    spans: List[AgentExecutionSpan] = Field(default_factory=list)


class FraudDeskCaseSummary(BaseModel):
    """Complete state of a CCB Fraud Desk run (`Run 0412 · CUST00001 · Lisbon`)."""
    run_id: str = "0412"
    customer_id: str = "CUST00001"
    account_id: str = "ACCT000003"
    incident_location: str = "Lisbon, PT"
    reported_at_utc: str = "2026-09-09T08:14:00Z"
    debit_dispute: DisputeResolutionSide
    credit_dispute: DisputeResolutionSide
    agents: List[AgentTraceRecord] = Field(default_factory=list)
    total_ad_hoc_sql: int = 0


# ==============================================================================
# 6. Backward-Compatible Enums & Models for Legacy Route Imports
# ==============================================================================

class BankChannel(str, Enum):
    FRAUD_DETECTION = "FRAUD_DETECTION"
    TELEPHONY_IVR = "TELEPHONY_IVR"
    MOBILE_APP = "MOBILE_APP"
    CORE_BANKING = "CORE_BANKING"
    WEB_PORTAL = "WEB_PORTAL"
    BRANCH_SUPPORT = "BRANCH_SUPPORT"


class SeverityLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class VeracityStatus(str, Enum):
    VERIFIED_TRUE = "VERIFIED_TRUE"
    CONTRADICTED_BY_TELEMETRY = "CONTRADICTED_BY_TELEMETRY"
    UNVERIFIED_PENDING_INVESTIGATION = "UNVERIFIED_PENDING_INVESTIGATION"
    SUSPICIOUS_FALSE_CLAIM = "SUSPICIOUS_FALSE_CLAIM"
    PARTIALLY_VERIFIED_CONSENT_DISPUTE = "PARTIALLY_VERIFIED_CONSENT_DISPUTE"


class RelevantAvenueCheck(BaseModel):
    avenue_id: str
    avenue_title: str
    why_relevant: str
    finding_summary: str
    corroborates_claim: bool


class ClaimVeracityEvaluation(BaseModel):
    claim_id: str
    claim_text: str
    veracity_status: VeracityStatus
    confidence_score: float = Field(ge=0.0, le=1.0)
    corroborating_telemetry: List[str] = Field(default_factory=list)
    discrepancy_details: Optional[str] = None
    relevant_avenues_checked: List[RelevantAvenueCheck] = Field(default_factory=list)
    multi_avenue_audit: Dict[str, Any] = Field(default_factory=dict)
    recommended_remediation: str = ""
    validation_timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    validator_agent: str = "PolicyAndComplianceAgent"


class MemoryFragment(BaseModel):
    fragment_id: str
    customer_id: str
    channel: BankChannel
    timestamp: datetime
    day_label: str
    summary: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    severity: SeverityLevel = SeverityLevel.MEDIUM
    session_id: Optional[str] = None
    veracity_evaluation: Optional[ClaimVeracityEvaluation] = None
    is_compacted_summary: bool = False

    def to_display_dict(self) -> Dict[str, Any]:
        return {
            "fragment_id": self.fragment_id,
            "customer_id": self.customer_id,
            "channel": self.channel.value,
            "timestamp": self.timestamp.isoformat(),
            "day_label": self.day_label,
            "summary": self.summary,
            "metadata": self.metadata,
            "severity": self.severity.value,
            "session_id": self.session_id,
            "is_compacted_summary": self.is_compacted_summary,
            "veracity_evaluation": (
                self.veracity_evaluation.model_dump(mode="json")
                if self.veracity_evaluation
                else None
            ),
        }


class ChannelSession(BaseModel):
    session_id: str
    customer_id: str
    channel: BankChannel
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "OPEN"
    agent_name: str
    session_metadata: Dict[str, Any] = Field(default_factory=dict)
    claims_recorded: List[Dict[str, Any]] = Field(default_factory=list)


class AuditAnomaly(BaseModel):
    anomaly_id: str
    customer_id: str
    severity: SeverityLevel
    anomaly_type: str
    description: str
    affected_channels: List[BankChannel]
    evidence_fragments: List[str]
    recommended_action: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AuditReport(BaseModel):
    sweep_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    customers_audited: int
    total_fragments_scanned: int
    anomalies_detected: List[AuditAnomaly]
    summary: str
    risk_score_distribution: Dict[str, int] = Field(default_factory=dict)


class PolicyRule(BaseModel):
    policy_id: str
    title: str
    category: str = ""
    regulatory_framework: str = ""
    description: str = ""
    action_required: str = ""
    automated_action: str = ""
    auto_remediation_allowed: bool = False
    trigger_conditions: List[str] = Field(default_factory=list)
    source_system: str = "Dataplex Universal Catalog (ccb-fraud-disputes)"



class EntityNode(BaseModel):
    node_id: str
    entity_type: str
    label: str
    attributes: Dict[str, Any] = Field(default_factory=dict)


class EntityEdge(BaseModel):
    source_id: str
    target_id: str
    relationship: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CausalStep(BaseModel):
    step_number: int
    timestamp: str
    channel: BankChannel
    event_title: str
    causal_explanation: str
    linked_fragment_id: str


class SynthesisResult(BaseModel):
    customer_id: str
    synthesized_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    executive_summary: str
    root_cause_identified: str
    causal_chain: List[CausalStep]
    recommended_actions: List[str]
    applicable_policies: List[PolicyRule]
    confidence_score: float
    zero_question_greeting: str


class AgentDescriptor(BaseModel):
    agent_id: str
    name: str
    role: str
    channel: Optional[BankChannel] = None
    model: str
    tools: List[str]
    description: str
    memory_topics_scoped: List[str] = Field(default_factory=list)
    catalog_governance: List[str] = Field(default_factory=list)
    authorized_golden_queries: List[str] = Field(default_factory=list)
    bound_golden_queries: List[str] = Field(default_factory=list)
    default_live_prompt: str = ""


class VerificationCheckItem(BaseModel):
    check_id: str
    check_name: str
    passed: bool
    evidence_detail: str


class AdminCardReviewCase(BaseModel):
    case_id: str
    customer_id: str
    card_name: str
    flagged_transaction: str
    flagged_amount: str
    confidence_score: float
    verification_passed: bool
    verification_checks: List[VerificationCheckItem] = Field(default_factory=list)
    chat_verification_transcript: List[Dict[str, str]] = Field(default_factory=list)
    card_status: str = "RESTRICTED_BLOCKED"
    recommended_admin_decision: str = "RESTRICT_CARD"
    admin_decision: Optional[str] = None
    customer_notification_message: str = ""


class CompactionReport(BaseModel):
    customer_id: str
    compacted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_fragments_before: int = 0
    compacted_summaries_after: int = 0
    tokens_saved_estimate: int = 0
    key_insights_extracted: List[str] = Field(default_factory=list)
    archived_fragment_ids: List[str] = Field(default_factory=list)



"""Legacy 6-agent ADK orchestrator tree (`01`..`06`).

Separated from `backend/adk_agents.py` so the primary module stays focused on
the active 8-agent hub-and-spoke desk (`create_consumer_credit_synthesizer_agent`).
Called via the wrapper `create_ccb_fraud_desk_orchestrator_agent()` in
`backend/adk_agents.py:1620` for backward-compatible test harnesses.
"""

from typing import Any, Callable, Optional
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool


def build_legacy_six_agent_orchestrator(
    *,
    model: str = "gemini-2.5-flash",
    list_memory_topics_tool: Callable[..., Any],
    retrieve_customer_memories_tool: Callable[..., Any],
    catalog_governance_authorize_tool: Callable[..., Any],
    catalog_lookup_glossary_term_tool: Callable[..., Any],
    golden_01_customer_360_snapshot: Callable[..., Any],
    golden_02_recent_authorizations_for_card: Callable[..., Any],
    golden_03_suspicious_activity_on_card_window: Callable[..., Any],
    golden_04_geo_impossible_travel: Callable[..., Any],
    golden_07_open_disputes_for_customer: Callable[..., Any],
    golden_08_dispute_filing_window_check: Callable[..., Any],
    golden_09_provisional_credit_due: Callable[..., Any],
    golden_10_wallet_tokens_active_on_card: Callable[..., Any],
    golden_11_dispute_event_timeline: Callable[..., Any],
    log_agent_tree: Optional[Callable[[LlmAgent, str], None]] = None,
) -> LlmAgent:
    """Constructs the legacy 6-agent hierarchy (`identity`, `triage`, `policy`, `resolution`, `writer`, `orchestrator`)."""
    identity_context_agent = LlmAgent(
        name="identity_and_context_agent",
        model=model,
        description="02 Identity & Context Agent: Retrieves scoped customer facts from Vertex AI Memory Bank and runs Golden Query 01.",
        instruction=(
            "You are Agent 02 (Identity & Context). Call list_memory_topics_tool first, "
            "then retrieve scoped customer memories using retrieve_customer_memories_tool(customer_id=...) "
            "and run golden_01_customer_360_snapshot. Honour catalog_governance_authorize_tool verdicts."
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
        description="03 Fraud Triage Agent: Executes Golden Queries 02, 03, 04 on BigQuery to score travel-window activity.",
        instruction=(
            "You are Agent 03 (Fraud Triage). Use only approved Golden Queries (02, 03, 04) "
            "with named @parameters (zero ad-hoc SQL) and honour catalog_governance_authorize_tool."
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
        description="04 Policy & Compliance Agent: Resolves Regulation E vs Regulation Z from the ledger and runs Golden Queries 07, 08, 09.",
        instruction=(
            "You are Agent 04 (Policy & Compliance). Read the regulation recorded on each dispute, "
            "call catalog_lookup_glossary_term_tool, and run Golden Queries 07, 08, 09."
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
        description="05 Resolution & Actions Agent: Checks active wallet tokens (Golden Query 10) and dispute timeline (Golden Query 11).",
        instruction=(
            "You are Agent 05 (Resolution & Actions). Check active wallet tokens via "
            "golden_10_wallet_tokens_active_on_card, event history via golden_11_dispute_event_timeline, "
            "and standing instructions via list_memory_topics_tool and retrieve_customer_memories_tool."
        ),
        tools=[
            FunctionTool(golden_10_wallet_tokens_active_on_card),
            FunctionTool(golden_11_dispute_event_timeline),
            FunctionTool(list_memory_topics_tool),
            FunctionTool(retrieve_customer_memories_tool),
        ],
    )

    memory_writer_agent = LlmAgent(
        name="memory_writer_agent",
        model=model,
        description="06 Memory Writer Agent: Summarizes ordered dispute timeline without storing restricted PII/PAN.",
        instruction=(
            "You are Agent 06 (Memory Writer). Summarize golden_11_dispute_event_timeline "
            "and trigger server-side Memory Bank consolidation without storing full PAN or SSN."
        ),
        tools=[
            FunctionTool(golden_11_dispute_event_timeline),
            FunctionTool(retrieve_customer_memories_tool),
        ],
    )

    orchestrator = LlmAgent(
        name="ccb_fraud_desk_orchestrator",
        model=model,
        description="01 Orchestrator Agent: Legacy 6-agent coordinator.",
        instruction=(
            "You are Agent 01 (Orchestrator). Route across Identity & Context (02), Fraud Triage (03), "
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
    if log_agent_tree is not None:
        log_agent_tree(orchestrator, "create_ccb_fraud_desk_orchestrator_agent")
    return orchestrator

"""Hermetic unit tests for 09_dispute_specialist_agent and staged action tools."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend import adk_agents as adk
from backend.models import AgentId


def test_dispute_specialist_in_roster():
    agents = {a.name: a for a in adk.list_all_agents()}
    assert "dispute_specialist_agent" in agents

    desc = agents["dispute_specialist_agent"]
    assert desc.agent_id == AgentId.DISPUTE_SPECIALIST.value
    assert "07_open_disputes_for_customer" in desc.bound_golden_queries
    assert "08_dispute_filing_window_check" in desc.bound_golden_queries
    assert "09_provisional_credit_due" in desc.bound_golden_queries
    assert "11_dispute_event_timeline" in desc.bound_golden_queries
    assert "17_card_replacement_tracking" in desc.bound_golden_queries
    assert "19_travel_notices_for_customer" in desc.bound_golden_queries
    assert "stage_dispute_filing_tool" in desc.tools
    assert "stage_provisional_credit_request_tool" in desc.tools


def test_resolve_dispute_specialist_agent():
    agent = adk.get_adk_agent_by_id_or_name("09_dispute_specialist_agent")
    assert agent.name == "dispute_specialist_agent"

    agent_num = adk.get_adk_agent_by_id_or_name("09")
    assert agent_num.name == "dispute_specialist_agent"

    agent_short = adk.get_adk_agent_by_id_or_name("dispute_specialist_agent")
    assert agent_short.name == "dispute_specialist_agent"


def test_stage_dispute_filing_glossary_precondition(monkeypatch):
    # If glossary resolution fails, tool must refuse per Decision A6
    monkeypatch.setattr(adk, "catalog_lookup_glossary_term_tool", lambda term: {"term_id": "n/r"})

    result = adk.stage_dispute_filing_tool(
        customer_id="CUST00001",
        card_id="CARD00004",
        transaction_id="TXN123",
        dispute_reason="Stolen wallet in Lisbon",
        regulation_path="REG_E",
    )
    assert result["allowed"] is False
    assert result["status"] == "REFUSED_PRECONDITION_FAILED"


def test_stage_dispute_filing_success(monkeypatch):
    monkeypatch.setattr(
        adk,
        "catalog_lookup_glossary_term_tool",
        lambda term: {"term_id": term, "name": "Regulation E", "definition": "Electronic Fund Transfer Act"},
    )
    fake_bank = MagicMock()
    fake_bank.create_session.return_value = "projects/p/locations/l/reasoningEngines/e/sessions/123"
    fake_bank.append_event.return_value = {"timestamp": "2026-09-25T09:00:00Z"}
    monkeypatch.setattr(adk, "_memory_bank", lambda client: fake_bank)

    result = adk.stage_dispute_filing_tool(
        customer_id="CUST00001",
        card_id="CARD00004",
        transaction_id="TXN123",
        dispute_reason="Stolen wallet in Lisbon",
        regulation_path="REG_E",
        amount_usd=45.50,
    )
    assert result["status"] == "STAGED"
    assert result["action"] == "DISPUTE_FILED"
    assert result["regulation_path"]["value"] == "REG_E"
    assert "DISPUTE_FILED" in result["action_board_item"]


def test_stage_provisional_credit_request_reg_z(monkeypatch):
    monkeypatch.setattr(
        adk,
        "catalog_lookup_glossary_term_tool",
        lambda term: {"term_id": term, "name": "Provisional Credit", "definition": "Credit rules"},
    )
    result = adk.stage_provisional_credit_request_tool(
        customer_id="CUST00001",
        dispute_id="DSP00034",
        regulation_path="REG_Z",
        amount_usd=100.0,
    )
    assert result["allowed"] is True
    assert result["status"] == "POLICY_INFORMATIONAL"
    assert result["provisional_credit_applicable"] is False
    assert "Withhold payment" in result["cardholder_remedy"]


def test_stage_provisional_credit_request_reg_e_success(monkeypatch):
    monkeypatch.setattr(
        adk,
        "catalog_lookup_glossary_term_tool",
        lambda term: {"term_id": term, "name": "Regulation E", "definition": "10 business days"},
    )
    fake_bank = MagicMock()
    fake_bank.create_session.return_value = "projects/p/locations/l/reasoningEngines/e/sessions/456"
    fake_bank.append_event.return_value = {"timestamp": "2026-09-25T09:00:00Z"}
    monkeypatch.setattr(adk, "_memory_bank", lambda client: fake_bank)

    result = adk.stage_provisional_credit_request_tool(
        customer_id="CUST00001",
        dispute_id="DSP00033",
        regulation_path="REG_E",
        amount_usd=15.11,
    )
    assert result["status"] == "STAGED"
    assert result["action"] == "PROVISIONAL_CREDIT_REQUEST"
    assert result["regulation_path"] == "REG_E"
    assert "10 business days" in result["disbursement_deadline"]

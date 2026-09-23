import pytest
from backend.knowledge_catalog import (
    DataplexCatalogClient,
    CatalogGovernor,
    Decision,
    NOT_RECORDED
)

def test_decision_to_dict():
    """Verify Decision correctly formats its output dict."""
    decision = Decision(
        allowed=False,
        action="REFUSE",
        subject="table.col",
        reason="Because I said so",
        evidence="aspect missing",
        replacement="other_table.col",
        regulation="GDPR"
    )
    assert decision.to_dict() == {
        "allowed": False,
        "action": "REFUSE",
        "subject": "table.col",
        "reason": "Because I said so",
        "evidence": "aspect missing",
        "replacement": "other_table.col",
        "regulation": "GDPR"
    }

def test_check_table_allows_active(monkeypatch):
    """Verify check_table permits tables with active lifecycle-status."""
    client = DataplexCatalogClient(project="p")
    
    def mock_lookup(entry_name, view="ALL"):
        return {
            "aspects": {
                "project.location.lifecycle-status": {
                    "data": {"status": "ACTIVE"}
                }
            }
        }
    monkeypatch.setattr(client, "lookup_entry", mock_lookup)
    
    governor = CatalogGovernor(client)
    decision = governor.check_table("t1", "d1")
    assert decision.allowed is True
    assert decision.action == "ALLOW"
    assert "ACTIVE" in decision.reason

def test_check_table_refuses_deprecated(monkeypatch):
    """Verify check_table refuses tables with deprecated lifecycle-status."""
    client = DataplexCatalogClient(project="p")
    
    def mock_lookup(entry_name, view="ALL"):
        return {
            "aspects": {
                "project.location.lifecycle-status": {
                    "data": {"status": "DEPRECATED", "authoritative_replacement": "t2"}
                }
            }
        }
    monkeypatch.setattr(client, "lookup_entry", mock_lookup)
    
    governor = CatalogGovernor(client)
    decision = governor.check_table("t1", "d1")
    assert decision.allowed is False
    assert decision.action == "REFUSE"
    assert decision.replacement == "t2"

def test_check_column_refuses_restricted(monkeypatch):
    """Verify check_column acts on agent_behavior REFUSE."""
    client = DataplexCatalogClient(project="p")
    
    def mock_lookup(entry_name, view="ALL"):
        return {
            "aspects": {
                "project.location.sensitivity-class@Schema.ssn": {
                    "data": {"agent_behavior": "REFUSE", "level": "RESTRICTED", "rationale": "PII"}
                }
            }
        }
    monkeypatch.setattr(client, "lookup_entry", mock_lookup)
    
    governor = CatalogGovernor(client)
    decision = governor.check_column("t1", "ssn", "d1")
    assert decision.allowed is False
    assert decision.action == "REFUSE"
    assert decision.reason == "PII"

def test_authorize_combines_decisions(monkeypatch):
    """Verify authorize() fails the whole query if any table or column is denied."""
    client = DataplexCatalogClient(project="p")
    
    def mock_lookup(entry_name, view="ALL"):
        return {
            "aspects": {
                "project.location.lifecycle-status": {
                    "data": {"status": "ACTIVE"}
                },
                "project.location.sensitivity-class@Schema.good_col": {
                    "data": {"agent_behavior": "ALLOW", "level": "PUBLIC"}
                },
                "project.location.sensitivity-class@Schema.bad_col": {
                    "data": {"agent_behavior": "REFUSE", "level": "RESTRICTED"}
                }
            }
        }
    monkeypatch.setattr(client, "lookup_entry", mock_lookup)
    
    governor = CatalogGovernor(client)
    result = governor.authorize("t1", ["good_col", "bad_col"], "d1")
    assert result["allowed"] is False
    assert result["table_decision"]["allowed"] is True
    assert "t1.bad_col" in result["blocked_columns"]
    assert "t1.good_col" not in result["blocked_columns"]

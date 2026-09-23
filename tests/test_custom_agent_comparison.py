import pytest
from unittest.mock import MagicMock
from backend.custom_agent_comparison import AgentComparisonEngine
from backend.memory_bank import MemoryBankClient

def test_engine_requires_memory_client():
    """Verify that AgentComparisonEngine refuses to run without a memory client."""
    with pytest.raises(ValueError, match="memory_client is required"):
        AgentComparisonEngine(memory_client=None)

def test_adk_lane_did_not_run_when_module_none():
    """Verify ADK lane reports did_not_run rather than fabricating metrics when adk_module is None."""
    client = MemoryBankClient(project="p", engine_id="e")
    engine = AgentComparisonEngine(memory_client=client, adk_module=None, adk_import_error="Missing package")
    
    result = engine._adk_lane("CUST1", "hello")
    assert result["status"] == "did_not_run"
    assert result["reason"] == "Missing package"
    assert "metrics" not in result

def test_parity_verdicts():
    """Verify parity computation returns correct verdicts."""
    engine = AgentComparisonEngine(memory_client=MagicMock())
    
    # Not established if one didn't run
    adk = {"status": "did_not_run"}
    custom = {"status": "ran"}
    res = engine._parity(adk, custom)
    assert res["verdict"] == "not_established"

    # No memories
    adk = {"status": "ran", "memory_facts_visible": []}
    custom = {"status": "ran", "memory_facts_visible": []}
    res = engine._parity(adk, custom)
    assert res["verdict"] == "no_memories"

    # Identical
    adk = {"status": "ran", "memory_facts_visible": ["A", "B"]}
    custom = {"status": "ran", "memory_facts_visible": ["B", "A"]}
    res = engine._parity(adk, custom)
    assert res["verdict"] == "identical"
    assert res["facts_seen_by_both"] == ["A", "B"]

    # Diverged
    adk = {"status": "ran", "memory_facts_visible": ["A"]}
    custom = {"status": "ran", "memory_facts_visible": ["B"]}
    res = engine._parity(adk, custom)
    assert res["verdict"] == "diverged"

def test_catalog_gate_blocks_both_lanes(monkeypatch):
    """Verify catalog refusal stops execution before lanes run."""
    client = MemoryBankClient(project="p", engine_id="e")
    governor = MagicMock()
    
    class FakeVerdict:
        def __init__(self, allowed, reason):
            self.allowed = allowed
            self.reason = reason
        def to_dict(self):
            return {"allowed": self.allowed, "reason": self.reason}
            
    # Mock authorize to return False for the first table
    governor.authorize.return_value = FakeVerdict(False, "RESTRICTED")
    
    engine = AgentComparisonEngine(memory_client=client, catalog_governor=governor)
    res = engine.run_side_by_side_comparison("CUST1", "hello")
    
    assert res["status"] == "BLOCKED_BY_CATALOG"
    assert "Dataplex refused a table both lanes need" in res["detail"]
    assert "google_adk_agent_result" not in res

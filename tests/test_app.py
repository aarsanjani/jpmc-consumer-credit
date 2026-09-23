import pytest
from backend.app import app
from backend.memory_bank import MemoryBankError

@pytest.fixture
def test_client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_api_agents_list(test_client):
    """Verify /api/agents/list returns exactly 8 agents."""
    resp = test_client.get("/api/agents/list")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 8
    assert len(data["agents"]) == 8

def test_api_session_upstream_error_handling(test_client, monkeypatch):
    """Verify routes return structured upstream errors rather than fabricated successes."""
    
    # Mock memory_client() to raise MemoryBankError
    def mock_memory_client(*args, **kwargs):
        class MockClient:
            def list_sessions(self):
                raise MemoryBankError(503, "http://fake", "Service Unavailable")
        return MockClient()
        
    import backend.app as app_module
    monkeypatch.setattr(app_module, "memory_client", mock_memory_client)
    
    # Actually wait, /api/session handles this internally and returns 200 with UNREACHABLE statuses.
    # Let's test /api/testbed which is defined to return _upstream_error.
    
    def mock_memory_client_testbed(*args, **kwargs):
        class MockClientTestbed:
            def retrieve(self, *args, **kwargs):
                raise MemoryBankError(503, "http://fake", "Service Unavailable")
        return MockClientTestbed()
        
    monkeypatch.setattr(app_module, "memory_client", mock_memory_client_testbed)
    
    resp = test_client.get("/api/testbed?customer_id=CUST1")
    assert resp.status_code == 502
    data = resp.get_json()
    assert data["status"] == "UPSTREAM_ERROR"
    assert "MemoryBankError" in data["error"]

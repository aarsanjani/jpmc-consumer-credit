import io
import json
import urllib.request

import pytest
from backend.memory_bank import (
    MemoryBankClient,
    StandaloneMemoryBankClient,
    MemoryBankError,
    field,
    NOT_RECORDED
)

def test_memory_bank_client_requires_project_and_engine():
    """Verify that the client refuses to initialize without a project and engine ID."""
    with pytest.raises(ValueError, match="project and engine_id are both required"):
        MemoryBankClient(project="", engine_id="engine")
    with pytest.raises(ValueError, match="project and engine_id are both required"):
        MemoryBankClient(project="project", engine_id="")

def test_memory_bank_client_properties():
    """Verify parent and base_url are built correctly."""
    client = MemoryBankClient(project="p", engine_id="e", location="us-east1", api_version="v2")
    assert client.parent == "projects/p/locations/us-east1/reasoningEngines/e"
    assert client.base_url == "https://us-east1-aiplatform.googleapis.com/v2/projects/p/locations/us-east1/reasoningEngines/e"

def test_field_helper():
    """Verify the field helper never returns fabricated defaults for missing or empty data."""
    # Absent
    assert field({}, "missing") == NOT_RECORDED
    assert field({"a": {}}, "a", "b") == NOT_RECORDED
    
    # Empty or None
    assert field({"a": None}, "a") == NOT_RECORDED
    assert field({"a": ""}, "a") == NOT_RECORDED
    assert field({"a": []}, "a") == NOT_RECORDED
    
    # Present
    assert field({"a": "val"}, "a") == "val"
    assert field({"a": {"b": "val"}}, "a", "b") == "val"
    assert field({"a": 0}, "a") == 0  # 0 is a valid value, not empty
    assert field({"a": False}, "a") == False

def test_client_create_memory_request(monkeypatch):
    """Verify create_memory issues the correct POST with its payload."""
    client = MemoryBankClient(project="p", engine_id="e", access_token="fake")
    requests = []

    def mock_request(method, url, body=None):
        requests.append((method, url, body))
        return {"name": "mem1"}

    monkeypatch.setattr(client, "_request", mock_request)

    scope = {"user": "u1"}
    res = client.create_memory("A fact", scope)
    assert res["name"] == "mem1"
    
    method, url, body = requests[0]
    assert method == "POST"
    assert url.endswith("/memories")
    assert body["fact"] == "A fact"
    assert body["scope"] == scope

def test_client_delete_memory_request(monkeypatch):
    """Verify delete_memory issues the correct DELETE request."""
    client = MemoryBankClient(project="p", engine_id="e", access_token="fake")
    requests = []

    def mock_request(method, url, body=None):
        requests.append((method, url, body))
        return {}

    monkeypatch.setattr(client, "_request", mock_request)

    client.delete_memory("projects/p/locations/l/reasoningEngines/e/memories/m1")
    method, url, body = requests[0]
    assert method == "DELETE"
    assert url.endswith("/memories/m1")
    assert body is None

    # Refuses bare ID
    with pytest.raises(ValueError, match="must be the full resource name"):
        client.delete_memory("m1")

def test_client_retrieve_request(monkeypatch):
    """Verify retrieve issues the correct POST depending on query presence."""
    client = MemoryBankClient(project="p", engine_id="e", access_token="fake")
    requests = []

    def mock_request(method, url, body=None):
        requests.append((method, url, body))
        return {"retrievedMemories": [{"memory": {"fact": "f1"}}]}

    monkeypatch.setattr(client, "_request", mock_request)

    scope = {"user": "u1"}
    memories = client.retrieve(scope, query="q", top_k=2)
    assert len(memories) == 1
    
    method, url, body = requests[0]
    assert method == "POST"
    assert url.endswith("/memories:retrieve")
    assert body["scope"] == scope
    assert body["similaritySearchParams"]["searchQuery"] == "q"
    assert body["similaritySearchParams"]["topK"] == 2

def test_client_generate_from_contents_request(monkeypatch):
    """Verify generate_from_contents correctly builds directContentsSource payloads."""
    client = MemoryBankClient(project="p", engine_id="e", access_token="fake")
    requests = []

    def mock_request(method, url, body=None):
        requests.append((method, url, body))
        return {"name": "op1", "done": True}

    monkeypatch.setattr(client, "_request", mock_request)

    turns = [
        {"role": "user", "text": "hello"},
        {"role": "agent", "text": "hi"},
    ]
    scope = {"user": "u1"}
    
    res = client.generate_from_contents(turns, scope)
    assert res["done"] is True
    
    method, url, body = requests[0]
    assert method == "POST"
    assert url.endswith("/memories:generate")
    assert body["scope"] == scope
    
    events = body["directContentsSource"]["events"]
    assert len(events) == 2
    assert events[0]["content"]["role"] == "user"
    assert events[0]["content"]["parts"][0]["text"] == "hello"
    assert events[1]["content"]["role"] == "model"  # mapped from agent
    assert events[1]["content"]["parts"][0]["text"] == "hi"

class _FakeHttpResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def test_standalone_preload_context_for_external_agent(monkeypatch):
    """Verify the standalone client builds a prompt block and claims no ADK usage."""
    body = {"retrievedMemories": [{"memory": {"fact": "fact A"}}, {"memory": {"fact": "fact B"}}]}
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda request, timeout=None: _FakeHttpResponse(json.dumps(body).encode()),
    )
    client = MemoryBankClient(project="p", engine_id="e", access_token="fake")
    standalone = StandaloneMemoryBankClient(client, customer_id="CUST1", framework_name="TestFrame")

    context = standalone.preload_context_for_external_agent("query")

    assert context["adk_used"] is False
    assert "fact A" in context["prompt_block"]
    assert "fact B" in context["prompt_block"]
    assert context["http_calls"] == ["POST projects/p/locations/us-central1/reasoningEngines/e/memories:retrieve"]
    assert context["memory_count"] == 2

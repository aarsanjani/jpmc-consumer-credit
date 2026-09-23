import pytest
from flask.testing import FlaskClient
from backend.app import app
from backend.memory_bank import MemoryBankError
from backend.ground_truth import GroundTruthError

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client

GET_ROUTES = [
    "/",
    "/api/health",
    "/api/session",
    "/api/memory/topics",
    "/api/memory/list?customer_id=CUST0001",
    "/api/catalog/overview",
    "/api/catalog/terms",
    "/api/catalog/authorize?table=tests",
    "/api/catalog/discover?term=x",
    "/api/warehouse/queries",
    "/api/agents/list",
    "/api/eval/probes",
    "/api/eval/reconcile?customer_id=CUST0001"
]

@pytest.mark.parametrize("route", GET_ROUTES)
def test_get_routes_non_500(client, mocker, route):
    """Every GET route must return something other than 500 when services are mocked."""
    # We heavily mock to ensure it doesn't crash
    mocker.patch("backend.app._memory.memory_topics", return_value=[])
    mocker.patch("backend.app._memory.list_memories", return_value=[])
    mocker.patch("backend.app._governor.list_terms", return_value=[])
    mocker.patch("backend.app._governor.search_terms", return_value=[])
    mocker.patch("backend.app._reader.customer_ids", return_value=["CUST0001"])
    mocker.patch("backend.app._governor.overview", return_value={})
    mocker.patch("backend.app._governor.check_table", return_value=mocker.Mock(to_dict=lambda: {}))
    mocker.patch("backend.app._governor.discover_sibling_tables", return_value=[])
    
    # Mocking for reconcile
    from backend.memory_eval.reconcile import ReconciliationReport
    mocker.patch("backend.app.reconcile.reconcile_amounts", return_value=ReconciliationReport(customer_id="CUST"))
    mocker.patch("backend.app.reconcile.reconcile_travel", return_value=ReconciliationReport(customer_id="CUST"))
    

    mocker.patch("backend.adk_agents.list_all_agents", return_value=[])
    mocker.patch("backend.app.load_probes", return_value=([], {}))
    
    response = client.get(route)
    assert response.status_code != 500, f"Route {route} returned 500"

POST_ROUTES = [
    ("/api/memory/retrieve", {"customer_id": "CUST001", "query": "travel"}),
    ("/api/memory/generate", {"customer_id": "CUST001", "turns": [{"text": "hi"}]}),
    ("/api/warehouse/run", {"customer_id": "CUST001", "query_id": "customer_360"}),
    ("/api/chat", {"customer_id": "CUST001", "question": "hi"}),
    ("/api/agents/run", {"customer_id": "CUST001", "question": "hi"}),
    ("/api/eval/retrieval", {"customer_id": "CUST001"}),
    ("/api/eval/write-policy", {"transcript_id": "mixed_signal_call", "experiment": "inclusion"})
]

@pytest.mark.parametrize("route, payload", POST_ROUTES)
def test_post_routes_non_500(client, mocker, route, payload):
    from backend.memory_bank import GenerateResult, RetrievalResult
    from backend.ground_truth import QueryResult
    from backend.provenance import Origin, unmeasured
    from backend.models import ClaimStatus, GovernanceAction
    from backend.agent import TurnResult
    
    mocker.patch("backend.app._memory.retrieve", return_value=RetrievalResult([], {}, None, None, unmeasured("test")))
    mocker.patch("backend.app._memory.generate_from_conversation", return_value=GenerateResult("op", True, [], {}, unmeasured("test")))
    mocker.patch("backend.app._reader.run", return_value=QueryResult("id", [], "", {}, unmeasured("test"), unmeasured("test"), unmeasured("test")))
    mocker.patch("backend.app._agent.answer", return_value=TurnResult("msg", "ANSWERED", mocker.Mock(to_dict=lambda: {})))
    mocker.patch("backend.adk_agents.run_turn", return_value=mocker.Mock(to_dict=lambda: {}))
    
    mocker.patch("backend.app.run_retrieval_benchmark", return_value=mocker.Mock(to_dict=lambda: {}))
    mocker.patch("backend.app.run_inclusion", return_value=mocker.Mock(to_dict=lambda: {}))
    mocker.patch("backend.app.load_transcripts", return_value=({"mixed_signal_call": mocker.Mock()}, {}))
    
    response = client.post(route, json=payload)
    assert response.status_code != 500, f"Route {route} returned 500: {response.text}"

def test_missing_customer_id_is_400(client):
    """A missing required parameter must return 400 rather than crashing (500)."""
    # missing customer_id on memory list
    response = client.get("/api/memory/list")
    assert response.status_code == 400

def test_memory_bank_error_is_503(client, mocker):
    """A service failure must map to 503 instead of crashing."""
    mocker.patch("backend.app._memory.list_memories", side_effect=MemoryBankError("timeout"))
    
    response = client.get("/api/memory/list?customer_id=CUST001")
    assert response.status_code == 503
    assert response.json["status"] == "MEMORY_BANK_UNAVAILABLE"

def test_warehouse_error_is_503(client, mocker):
    """A GroundTruthError must map to 503 instead of crashing."""
    mocker.patch("backend.app._reader.run", side_effect=GroundTruthError("db down"))
    
    response = client.post("/api/warehouse/run", json={"customer_id": "CUST001", "query_id": "customer_360"})
    assert response.status_code == 503
    assert response.json["status"] == "WAREHOUSE_UNAVAILABLE"

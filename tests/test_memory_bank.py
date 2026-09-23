import pytest
from backend.memory_bank import MemoryBankClient, Memory, MemoryTopic, GeneratedMemory, GenerateResult

def test_retrieve_shape(mocker):
    """Retrieve parses the correct Distance and Memory Topic flat shape."""
    client = MemoryBankClient()
    mock_request = mocker.patch.object(client, "_request")
    
    mock_request.return_value = {
        "retrievedMemories": [{
            "memory": {
                "name": "projects/000000000000/locations/us-central1/reasoningEngines/000000000000000000/memories/m1",
                "fact": "Customer called about travel",
                "scope": {"customer_id": "CUST00001"},
                "topics": [{"customMemoryTopicLabel": "TRAVEL_STATUS"}]
            },
            "distance": 0.42
        }]
    }

    result = client.retrieve(scope={"customer_id": "CUST00001"}, query="query")
    
    assert len(result.memories) == 1
    memory = result.memories[0]
    assert memory.memory_id == "m1"
    assert memory.fact == "Customer called about travel"
    assert memory.distance == 0.42
    assert "TRAVEL_STATUS" in memory.topics

def test_list_shape(mocker):
    """List processes memory pages until nextPageToken is null."""
    client = MemoryBankClient()
    mock_request = mocker.patch.object(client, "_request")
    
    mock_request.side_effect = [
        {"memories": [{"name": "fake1"}], "nextPageToken": "token"},
        {"memories": [{"name": "fake2"}], "nextPageToken": None}
    ]
    
    memories = client.list_memories()
    assert len(memories) == 2
    assert memories[0].memory_id == "fake1"
    assert memories[1].memory_id == "fake2"

def test_generate_lro_with_write(mocker):
    """generate_from_conversation computes done LROs parsing generated memories."""
    client = MemoryBankClient()
    mocker.patch.object(client, "_request", return_value={"name": "op1"})
    
    mock_await = mocker.patch.object(client, "_await_operation")
    mock_await.return_value = {
        "done": True,
        "response": {
            "generatedMemories": [
                {
                    "memory": {"name": "memXYZ", "memoryType": "KNOWLEDGE"},
                    "action": "CREATED"
                }
            ]
        }
    }
    
    res = client.generate_from_conversation(turns=[{"text": "hello"}], scope={"a": "b"})
    assert res.done is True
    assert res.wrote_nothing is False
    assert len(res.generated) == 1
    assert res.generated[0].action == "CREATED"
    assert res.generated[0].memory.name == "memXYZ"

def test_generate_lro_wrote_nothing(mocker):
    """Generating nothing MUST result in wrote_nothing=True. This is a load-bearing invariant."""
    client = MemoryBankClient()
    mocker.patch.object(client, "_request", return_value={"name": "op1"})
    
    mock_await = mocker.patch.object(client, "_await_operation")
    # LRO done but NO response key.
    mock_await.return_value = {"done": True}
    
    res = client.generate_from_conversation(turns=[{"text": "meaningless chatter"}], scope={"a": "b"})
    
    assert res.done is True
    assert res.generated == []
    assert res.wrote_nothing is True

def test_topic_shapes_parse_correctly():
    """Conflating nested config topic and flat memory topic is a real defect."""
    # From API Config shape
    topic = MemoryTopic.from_api({"customMemoryTopic": {"label": "CFG_TOPIC", "description": "desc"}})
    assert topic.label == "CFG_TOPIC"
    assert topic.kind == "CUSTOM"

    # Managed cfg topic
    topic_m = MemoryTopic.from_api({"managedMemoryTopic": {"managedTopicEnum": "MANAGED_CFG"}})
    assert topic_m.label == "MANAGED_CFG"
    
    # Flat Memory shape
    from backend.memory_bank import _topic_labels
    labels = _topic_labels([{"customMemoryTopicLabel": "TRAVEL_STATUS"}])
    assert labels == ["TRAVEL_STATUS"]

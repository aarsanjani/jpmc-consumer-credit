import pytest

@pytest.fixture(autouse=True)
def dummy_env_vars(monkeypatch):
    """Provide dummy environment variables for tests so real Google Cloud resources aren't hit or required."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("VERTEX_AGENT_ENGINE_ID", "000000000000000000")
    monkeypatch.setenv("BIGQUERY_DATASET", "test_dataset")
    monkeypatch.setenv("BQ_DATASET", "test_dataset")
    monkeypatch.setenv("DATAPLEX_GLOSSARY", "test-glossary")
    monkeypatch.setenv("DATAPLEX_GLOSSARY_ID", "test-glossary")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")

@pytest.fixture
def test_client():
    from backend.app import app
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client

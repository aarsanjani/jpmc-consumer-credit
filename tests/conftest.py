import pytest
import os
import sys

# Add backend to PYTHONPATH equivalent? Not needed if PYTHONPATH=. is used.

@pytest.fixture(scope="session", autouse=True)
def dummy_env():
    """Set dummy environment variables so require_env() is satisfied."""
    os.environ["GOOGLE_CLOUD_PROJECT"] = "test-project"
    os.environ["GOOGLE_CLOUD_LOCATION"] = "us-central1"
    os.environ["VERTEX_AGENT_ENGINE_ID"] = "000000000000000000"
    os.environ["BIGQUERY_DATASET"] = "test_dataset"
    os.environ["DATAPLEX_GLOSSARY_ID"] = "test-glossary"
    os.environ["GOOGLE_CLOUD_PROJECT_NUMBER"] = "000000000000"
    
    import backend.config
    backend.config.settings(refresh=True)

@pytest.fixture(autouse=True)
def wipe_env_keys(monkeypatch):
    """Monkeypatch environment globally but dummy_env is loaded. 
    Actually, maybe just let the session-scoped fixture handle the basics,
    and if a test needs specific env vars removed, they can use monkeypatch over it."""
    pass

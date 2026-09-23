import pytest
import os
from backend.config import require_env, optional_env, ConfigurationError, Settings, settings

def test_require_env_raises_on_missing(monkeypatch):
    """require_env must raise ConfigurationError rather than defaulting when a required variable is missing."""
    monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
    with pytest.raises(ConfigurationError):
        require_env("NONEXISTENT_VAR")

def test_require_env_accepts_aliases(monkeypatch):
    """require_env successfully resolves when an alias is used rather than the primary name."""
    # Test BQ_DATASET alias
    monkeypatch.delenv("BIGQUERY_DATASET", raising=False)
    monkeypatch.setenv("BQ_DATASET", "alias_dataset")
    assert require_env("BIGQUERY_DATASET", "BQ_DATASET") == "alias_dataset"

    # Test DATAPLEX_GLOSSARY alias
    monkeypatch.delenv("DATAPLEX_GLOSSARY_ID", raising=False)
    monkeypatch.setenv("DATAPLEX_GLOSSARY", "alias_glossary")
    assert require_env("DATAPLEX_GLOSSARY", "DATAPLEX_GLOSSARY_ID") == "alias_glossary"

def test_optional_env():
    """optional_env returns None when a variable is completely missing, rather than raising."""
    assert optional_env("COMPLETELY_MISSING_VAR") is None

def test_settings_redacted_does_not_leak():
    """Settings.redacted() replaces the sensitive parts of identifiers while keeping enough to debug."""
    s = Settings(
        project_id="secret-project",
        location="us-central1",
        agent_engine_id="secret00001key",
        bigquery_dataset="secret-dataset",
        dataplex_glossary_id="secret-glossary",
        project_number="12345"
    )
    redacted = s.redacted()
    # The last 6 chars are "001key", length is 14, [:-6] is "secret00"
    assert "secret00001key" not in redacted["agent_engine_id"]
    assert "001key" in redacted["agent_engine_id"]
    # other fields should be fully kept based on config.py
    assert redacted["project_id"] == "secret-project"

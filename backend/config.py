"""
Central configuration for the consumer-credit fraud desk.

Every Google Cloud identifier the application needs is resolved here, from the
environment, exactly once. There are no defaults.

That is a deliberate choice rather than an oversight. A default project id is the
most dangerous kind of configuration bug available to us: the process starts, the
health checks pass, the UI renders, and the whole time the desk is reading and
writing somebody else's data. It fails in the one way we cannot detect from the
inside. An unset variable that raises on import fails in the one way we cannot
miss.

So: `require_env` raises. Callers do not get to pass a fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


class ConfigurationError(RuntimeError):
    """Raised when a required environment variable is absent or blank."""


def require_env(name: str, *aliases: str) -> str:
    """Return the value of ``name``, or of the first alias that is set.

    Aliases exist because this repository grew two spellings for the same
    setting (``BIGQUERY_DATASET`` and ``BQ_DATASET``), and silently honouring
    only one of them while reporting the other is how a configuration change
    becomes a no-op. Accepting both is the honest fix.

    Raises:
        ConfigurationError: if none of the names is set to a non-blank value.
    """
    for candidate in (name, *aliases):
        value = os.environ.get(candidate, "").strip()
        if value:
            return value

    spelled = name if not aliases else f"{name} (or {', '.join(aliases)})"
    raise ConfigurationError(
        f"Required environment variable {spelled} is not set. "
        f"Copy .env.example to .env and fill in real values; see README.md. "
        f"This is raised rather than defaulted so that a misconfigured process "
        f"cannot quietly operate against the wrong project."
    )


def optional_env(name: str, *aliases: str) -> Optional[str]:
    """Return the value of ``name`` or an alias, or ``None``. Never raises.

    Only for settings whose absence genuinely changes nothing about correctness.
    """
    for candidate in (name, *aliases):
        value = os.environ.get(candidate, "").strip()
        if value:
            return value
    return None


@dataclass(frozen=True)
class Settings:
    """Resolved, validated configuration for one process."""

    project_id: str
    location: str
    agent_engine_id: str
    bigquery_dataset: str
    dataplex_glossary_id: str
    project_number: Optional[str]

    @property
    def agent_engine_resource(self) -> str:
        """Fully qualified Memory Bank engine name.

        Vertex AI accepts either the project id or the project number in this
        path. We prefer the number when it is available, because that is the
        form the service echoes back in ``memory.name``, and matching it means
        resource names compare equal without normalisation.
        """
        owner = self.project_number or self.project_id
        return (
            f"projects/{owner}/locations/{self.location}"
            f"/reasoningEngines/{self.agent_engine_id}"
        )

    @property
    def aiplatform_endpoint(self) -> str:
        return f"https://{self.location}-aiplatform.googleapis.com"

    @property
    def dataplex_endpoint(self) -> str:
        return "https://dataplex.googleapis.com"

    @property
    def glossary_resource(self) -> str:
        return (
            f"projects/{self.project_id}/locations/{self.location}"
            f"/glossaries/{self.dataplex_glossary_id}"
        )

    @property
    def dataset_resource(self) -> str:
        return f"{self.project_id}.{self.bigquery_dataset}"

    def redacted(self) -> dict:
        """Configuration as it may safely appear in a UI or a log line.

        Nothing here is a credential, but identifiers are still worth keeping
        out of screenshots, so the engine id is shown by its tail only.
        """
        return {
            "project_id": self.project_id,
            "location": self.location,
            "agent_engine_id": f"\u2026{self.agent_engine_id[-6:]}",
            "bigquery_dataset": self.bigquery_dataset,
            "dataplex_glossary_id": self.dataplex_glossary_id,
        }


_cached: Optional[Settings] = None


def settings(refresh: bool = False) -> Settings:
    """Resolve configuration once per process.

    Args:
        refresh: re-read the environment. Only tests should need this.
    """
    global _cached
    if _cached is not None and not refresh:
        return _cached

    _cached = Settings(
        project_id=require_env("GOOGLE_CLOUD_PROJECT"),
        location=require_env("GOOGLE_CLOUD_LOCATION"),
        agent_engine_id=require_env("VERTEX_AGENT_ENGINE_ID"),
        bigquery_dataset=require_env("BIGQUERY_DATASET", "BQ_DATASET"),
        dataplex_glossary_id=require_env("DATAPLEX_GLOSSARY", "DATAPLEX_GLOSSARY_ID"),
        project_number=optional_env("GOOGLE_CLOUD_PROJECT_NUMBER"),
    )
    return _cached

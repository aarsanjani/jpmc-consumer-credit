"""ADK entry point for `adk run`, `adk web` and Agent Engine deployment.

The ADK tooling looks for a module-level `root_agent` and imports this package
to find it. That is the whole job of this file.

What this replaced: the previous version constructed a `CustomerMemoryBank`
pinned to a hardcoded `customer_id` at import time and passed it into the agent
factory. Two things were wrong with that. The customer id it used did not exist
in the warehouse, and more importantly, binding a customer at import time meant
the deployed agent could only ever have reasoned about one customer. The
customer is a property of a turn, not of the process, so the tools in
`backend.adk_agents` take `customer_id` as an argument and the orchestrator asks
for it when it is missing.

Importing this module requires the environment to be configured, because the
agent's tools resolve live clients. `backend.config.require_env` raises on a
missing variable rather than defaulting, so a misconfigured deployment fails at
startup instead of answering questions with invented data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ADK may import this module with the package root absent from sys.path,
# depending on how it was launched, so make the `backend` package importable.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backend.adk_agents import DEFAULT_MODEL, create_orchestrator  # noqa: E402

# The model is overridable because Agent Engine deployments are sometimes
# pinned to a specific version, but it is never read from a hardcoded literal
# elsewhere in the tree.
root_agent = create_orchestrator(model=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL))

# `agent` is the alias some ADK versions look for; `root_agent` is the current
# convention. Export both so this works either way.
agent = root_agent

__all__ = ["agent", "root_agent"]

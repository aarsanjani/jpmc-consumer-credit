"""Entry point for `adk web` / `adk run` CLI (`root_agent` export).

Not imported by the Flask web server (`backend/app.py`). Exposes the active
8-agent hub-and-spoke root (`create_consumer_credit_synthesizer_agent` in
`backend/adk_agents.py:1313`) when running the ADK CLI directly.
"""

import os
import sys
from pathlib import Path

root_dir = str(Path(__file__).resolve().parent.parent)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from backend.adk_agents import create_consumer_credit_synthesizer_agent

root_agent = create_consumer_credit_synthesizer_agent(
    model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
)
agent = root_agent

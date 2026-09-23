"""
agent/agent.py - Enterprise ADK Root Agent for the JPMC CCB Fraud & Disputes Desk.

WHY `MemoryBankClient` IS NOT INITIALIZED WITH `customer_id`:
    Previously, `CustomerMemoryBank(customer_id="CUST00001")` coupled the client
    instance to a single customer at startup. In production (and in `backend/memory_bank.py`),
    `MemoryBankClient(project, engine_id, location)` is a stateless service client
    pointed at the Vertex AI Reasoning Engine (`projects/{project}/locations/{location}/reasoningEngines/{engine}`).
    `customer_id` is a runtime scope parameter (`scope={"customer_id": customer_id}`)
    passed per API call (`memories:retrieve` and `memories:generate`), allowing one
    deployed agent engine to serve every customer without cross-customer data leakage.
"""

import os
import sys
from pathlib import Path

# Ensure root workspace is in sys.path
root_dir = str(Path(__file__).resolve().parent.parent)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from backend.adk_agents import create_ccb_fraud_desk_orchestrator_agent
from backend.knowledge_catalog import create_default_catalog_governor
from backend.memory_bank import create_default_memory_bank_client

# 1. Initialize engine-level Memory Bank REST client (NO `customer_id` hardcoded on client!)
memory_bank_client = create_default_memory_bank_client(verbose=False)

# 2. Initialize Dataplex Universal Catalog Governor (`strict=True` -> fail closed on ungoverned data)
catalog_governor = create_default_catalog_governor(strict=True, verbose=False)

# 3. Create the 6-agent CCB Fraud & Disputes ADK orchestrator (`01` .. `06` + 12 Golden Queries)
root_agent = create_ccb_fraud_desk_orchestrator_agent(
    memory_bank_client=memory_bank_client,
    catalog_governor=catalog_governor,
    model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
)

# Standard ADK export
agent = root_agent

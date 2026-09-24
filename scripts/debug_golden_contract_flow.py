"""Isolated live debug for `_entry_table_name` (L264), `discover_sibling_tables` (L290),
`execute_golden_query_contract` (L331), and `GoldenQueryExecutionResult` (L480)
in `backend/adk_agents.py`.

Run from repo root:
    set -a; . ./.env; set +a
    PYTHONPATH=. ./.venv/bin/python3 scripts/debug_golden_contract_flow.py
"""

import json
from datetime import datetime, timezone

from backend import obs
from backend.adk_agents import (
    GLOBAL_CATALOG_GOVERNOR,
    GOLDEN_QUERY_CONTRACTS,
    AgentId,
    _entry_table_name,
    _SIBLING_TABLE_CACHE,
    discover_sibling_tables,
    execute_golden_query_contract,
)


def main() -> None:
    obs.configure("DEBUG")
    obs.enable_tracing()
    print("=" * 84)
    print("PART 1: ISOLATED SHELL DEBUG OF _entry_table_name (L264) & discover_sibling_tables (L290)")
    print("=" * 84)

    primary_table = "authorizations"
    raw_entries = GLOBAL_CATALOG_GOVERNOR.client.search_entries(primary_table)
    print(f"\n[IN]  governor.client.search_entries({primary_table!r}) -> returned {len(raw_entries)} raw Dataplex entries")

    for idx, entry in enumerate(raw_entries, 1):
        dp = entry.get("dataplexEntry", entry) or {}
        entry_type = str(dp.get("entryType", "")).rsplit("/", 1)[-1]
        resource = str((dp.get("entrySource") or {}).get("resource", "")).rsplit("/", 2)[-1]
        extracted = _entry_table_name(entry)
        print(
            f"  Entry #{idx}: entryType={entry_type:<18} resource=.../{resource:<32} "
            f"-> _entry_table_name(entry) OUT = {extracted!r}"
        )

    _SIBLING_TABLE_CACHE.clear()
    siblings = discover_sibling_tables(GLOBAL_CATALOG_GOVERNOR, primary_table, use_cache=False)
    print(f"\n[IN]  discover_sibling_tables(GLOBAL_CATALOG_GOVERNOR, {primary_table!r})")
    print(f"[OUT] siblings (excluding {primary_table!r}) = {siblings}")

    print("\n" + "=" * 84)
    print("PART 2: STEP-BY-STEP TURN TRACE INSIDE execute_golden_query_contract (L331 -> L480)")
    print("=" * 84)

    query_id = "02"
    calling_agent = AgentId.TELEPHONY_IVR
    params = {
        "card_id": "CARD00002",
        "as_of_ts": datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
        "lookback_days": 14,
        "row_limit": 3,
    }
    contract = GOLDEN_QUERY_CONTRACTS[query_id]
    print(f"\n[TURN 0 - INPUTS TO execute_golden_query_contract (L331)]")
    print(f"  query_id      = {query_id!r} ({contract.sql_filename})")
    print(f"  calling_agent = {calling_agent.value!r}")
    print(f"  params        = {params}")
    print(f"  authorized?   = {calling_agent in contract.authorized_agents} (allowed: {[a.value for a in contract.authorized_agents]})")

    _SIBLING_TABLE_CACHE.clear()
    with obs.run_context(kind="debug") as run_id:
        output_dict = execute_golden_query_contract(
            query_id=query_id,
            calling_agent=calling_agent,
            params=params,
        )
        spans = obs.get_trace(run_id)

    print(f"\n[TURN 1..N - SPANS RECORDED INSIDE execute_golden_query_contract (run_id={run_id})]")
    for i, sp in enumerate(spans, 1):
        print(
            f"  Span {i:02d} (depth={sp['depth']}): {sp['label']:<24} "
            f"elapsed={sp['elapsed_ms']:>6.1f}ms | fields={sp['fields']} | outcome={sp['outcome']}"
        )

    print(f"\n[FINAL STEP - GoldenQueryExecutionResult (L480) -> .model_dump(mode='json') (L492)]")
    summary_view = {
        "query_id": output_dict["query_id"],
        "tool_name": output_dict["tool_name"],
        "calling_agent": output_dict["calling_agent"],
        "parameters_bound": output_dict["parameters_bound"],
        "catalog_verdict": output_dict["catalog_verdict"],
        "voluntary_refusals_logged": output_dict["voluntary_refusals_logged"],
        "ad_hoc_sql_used": output_dict["ad_hoc_sql_used"],
        "row_count": output_dict["row_count"],
        "first_row_sample": output_dict["rows"][0] if output_dict["rows"] else None,
    }
    print(json.dumps(summary_view, indent=2))


if __name__ == "__main__":
    main()

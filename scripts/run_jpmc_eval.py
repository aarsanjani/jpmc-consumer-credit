#!/usr/bin/env python3
"""Runner for the JPMC evaluation harness across Memory Bank and BigQuery baselines.

Executes the JPMC evaluation tiers (Tier 1 simple, Tier 2 semantic, Tier 3 sidekick,
and End-to-End) and evaluates storage accuracy, change handling, volume tolerance,
customer isolation, and the 7 JPMC scenarios.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from backend.eval_harness import evaluate, loader, results, scoring
from backend.eval_harness.bq import EMBEDDER, FACTS, RAW_NOTES, Ranker, Warehouse, card_endings
from backend.eval_harness.env import EvalSettings, eval_memory_bank
from backend.eval_harness.fixtures import Fixtures
from backend.eval_harness.scenarios import SCENARIOS
from backend.knowledge_catalog import shared_catalog_governor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG = logging.getLogger("run_jpmc_eval")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="JPMC Memory Bank Evaluation Harness")
    parser.add_argument("--no-gate", action="store_true", help="Skip veracity check during load (raw MB baseline)")
    parser.add_argument("--skip-load", action="store_true", help="Skip session loading (use existing eval data)")
    parser.add_argument("--reset-engine", action="store_true", help="Purge eval engine before loading")
    parser.add_argument("--noise-per-customer", type=int, default=0, help="Noise notes per customer for volume test")
    parser.add_argument("--workers", type=int, default=8, help="Parallel worker threads")
    parser.add_argument("--run-id", type=str, default="", help="Custom run identifier")
    parser.add_argument("--dry-run", action="store_true", help="Validate configuration and fixtures then exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    LOG.info("Initializing eval settings for run %s", run_id)
    settings = EvalSettings.from_env()
    fixtures = Fixtures.load()

    LOG.info(
        "Eval target: engine=%s, dataset=%s, model=%s, fixtures=%d sessions / %d cases",
        settings.engine_id,
        settings.dataset,
        settings.model,
        len(fixtures.sessions),
        len(fixtures.retrieval_cases),
    )

    if args.dry_run:
        LOG.info("Dry-run requested: configuration and fixtures verified successfully.")
        return 0

    bank = eval_memory_bank(settings)
    warehouse = Warehouse(settings)
    warehouse.ensure()

    if args.reset_engine and not args.skip_load:
        LOG.info("Resetting eval engine: deleting previous memories and sessions...")
        reset_stats = loader.reset_engine(bank, workers=args.workers)
        LOG.info("Reset complete: %s", reset_stats)

    load_time = 0.0
    loads = []
    if not args.skip_load:
        started_load = time.perf_counter()
        LOG.info("Phase 1: Loading fixture sessions (gate=%s)...", not args.no_gate)
        loads = loader.load_sessions(
            bank,
            fixtures.sessions,
            load_id=run_id,
            gate=not args.no_gate,
            workers=args.workers,
            progress=LOG.info,
        )
        load_time = round(time.perf_counter() - started_load, 1)

        memories = list(bank.iter_memories())
        f_rows = loader.fact_rows(memories, loads, run_id)
        n_rows = loader.raw_note_rows(loads, run_id)

        LOG.info("Writing %d facts and %d raw notes to BigQuery %s...", len(f_rows), len(n_rows), settings.dataset)
        warehouse.insert(FACTS, f_rows)
        warehouse.insert(RAW_NOTES, n_rows)
    else:
        LOG.info("Skipping load phase: reading existing facts from BigQuery...")
        f_rows = warehouse.read_facts(run_id)

    # Optional Tier 2 setup
    embedding_model = None
    ranker = None
    if settings.connection and settings.embedding_endpoint:
        try:
            LOG.info("Setting up Tier 2 BigQuery remote embedding model...")
            warehouse.ensure_embedder(settings.connection, settings.embedding_endpoint)
            warehouse.embed_facts(run_id, EMBEDDER)
            embedding_model = EMBEDDER
            if settings.ranking_model:
                ranker = Ranker(settings.project, settings.ranking_model)
        except Exception as exc:
            LOG.warning("Tier 2 setup could not be completed: %s", exc)

    log_path = settings.runs_dir / f"judge_{run_id}.jsonl"
    judge_log = scoring.JsonlLog(log_path)
    genai_cli = scoring.genai_client(settings.project, settings.location)
    gemini = scoring.Gemini(genai_cli, settings.model, judge_log, run_id)

    ctx = evaluate.Context(
        run_id=run_id,
        bank=bank,
        warehouse=warehouse,
        gemini=gemini,
        fixtures=fixtures,
        workers=args.workers,
        embedding_model=embedding_model,
        ranker=ranker,
        glossary=evaluate.Glossary(shared_catalog_governor()),
    )

    LOG.info("Phase 2: Evaluating storage accuracy...")
    topic_dict = {t.get("label") or t.get("displayName"): t.get("description", "") for t in bank.memory_topics()}
    configured_topics = list(topic_dict.keys())
    matches = evaluate.match_facts(ctx, f_rows)
    ctx.fact_map = evaluate.fact_map_from(matches)
    ctx.turn_map = fixtures.turn_relevance_map()
    storage_report = evaluate.report_storage(fixtures, f_rows, matches, configured_topics)

    LOG.info("Phase 3: Running retrieval benchmarks across systems...")
    routes = evaluate.route_topics(ctx, fixtures.retrieval_cases, topic_dict)
    active_systems = [s for s in evaluate.SYSTEMS if s != "bq_vector_rerank" or ctx.ranker is not None]
    not_run = {s: "Tier 2 infra not configured" for s in evaluate.SYSTEMS if s not in active_systems}

    retrieval_results = evaluate.run_retrieval(ctx, fixtures.retrieval_cases, routes, active_systems, workers=args.workers)
    systems_report = evaluate.report_systems(ctx, retrieval_results, fixtures.retrieval_cases, not_run)
    tiers_report = evaluate.report_tiers(systems_report)

    LOG.info("Phase 4: Running End-to-End synthesis with Gemini judge...")
    e2e_results = evaluate.run_end_to_end(ctx, fixtures.retrieval_cases, retrieval_results)
    e2e_report = evaluate.report_end_to_end(e2e_results)

    LOG.info("Phase 5: Evaluating handling of state changes...")
    change_report = evaluate.report_change(ctx, f_rows, e2e_results)

    LOG.info("Phase 6: Evaluating customer isolation and control...")
    iso_report = evaluate.run_isolation(ctx, f_rows, retrieval_results)
    control_report = evaluate.run_control(ctx, routes, f_rows, retrieval_results)

    LOG.info("Phase 7: Running 7 JPMC scenarios...")
    scen_results = evaluate.run_scenarios(ctx, SCENARIOS, {})
    scen_report = evaluate.report_scenarios(scen_results)

    # Volume stage if requested
    volume_report = {}
    if args.noise_per_customer > 0:
        LOG.info("Phase 8: Running volume stress test with %d notes/customer...", args.noise_per_customer)
        noise = evaluate.noise_notes(fixtures.customer_ids, args.noise_per_customer)
        noise_rows, create_ms = evaluate.add_noise(ctx, noise)
        after_results = evaluate.run_retrieval(ctx, fixtures.retrieval_cases, routes, active_systems, workers=args.workers)
        after_report = evaluate.report_systems(ctx, after_results, fixtures.retrieval_cases, not_run)
        volume_report = evaluate.report_volume(systems_report, after_report, len(noise_rows), args.noise_per_customer, create_ms)

    causes = evaluate.session_causes(loads, card_endings(warehouse.client, settings.project, settings.governed_dataset, fixtures.customer_ids)) if loads and settings.governed_dataset else []
    load_report = evaluate.report_load(loads, f_rows, causes, gate=not args.no_gate, seconds=load_time) if loads else {}

    run_payload = {
        "run_id": run_id,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "git": results.git_state(),
        "engine_id": settings.engine_id,
        "dataset": settings.dataset,
        "model": settings.model,
        "load_summary": load_report,
        "storage_accuracy": storage_report,
        "systems": systems_report,
        "tiers": tiers_report,
        "end_to_end": e2e_report,
        "change_handling": change_report,
        "isolation": iso_report,
        "control": control_report,
        "scenarios": scen_report,
        "volume": volume_report,
    }
    run_payload["criteria"] = evaluate.criteria_table(run_payload)

    out_file = results.save_run(run_payload, settings.runs_dir)
    LOG.info("JPMC Eval Run %s finished successfully. Output: %s", run_id, out_file)

    # Print summary
    print("\n" + "=" * 60)
    print(f" JPMC Evaluation Run {run_id} Summary")
    print("=" * 60)
    for c in run_payload["criteria"]:
        crit = c["criterion"]
        mb_val = c.get("memory_bank")
        print(f"\n* {crit}")
        if isinstance(mb_val, dict):
            for k, v in mb_val.items():
                val = v.get("value") if isinstance(v, dict) else v
                print(f"    - MB {k}: {val}")
        else:
            val = mb_val.get("value") if isinstance(mb_val, dict) else mb_val
            print(f"    - MB: {val}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

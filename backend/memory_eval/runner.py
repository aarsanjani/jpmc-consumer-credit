"""
Run the Memory Bank evaluation end to end and emit one report.

    PYTHONPATH=. ./.venv/bin/python3 -m backend.memory_eval.runner --customer CUST00001
    PYTHONPATH=. ./.venv/bin/python3 -m backend.memory_eval.runner --only retrieval
    PYTHONPATH=. ./.venv/bin/python3 -m backend.memory_eval.runner --json-out report.json

Exit code is 0 only if every stage it was asked to run completed. A stage that
could not run reports why and fails; it does not return a partial score that
reads like a result.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Optional

from backend.config import settings as load_settings
from backend.ground_truth import GroundTruthReader
from backend.memory_bank import MemoryBankClient
from backend.memory_eval import reconcile, write_policy
from backend.memory_eval.labeller import load_probes
from backend.memory_eval.retrieval_eval import run_retrieval_benchmark

log = logging.getLogger("memory_eval")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROBES_PATH = os.path.join(REPO_ROOT, "benchmarks", "retrieval_probes.json")
TRANSCRIPTS_PATH = os.path.join(REPO_ROOT, "benchmarks", "write_transcripts.json")

STAGES = ("retrieval", "inclusion", "topics", "scope", "consolidation", "reconcile")


def run_retrieval_stage(client: MemoryBankClient, customer_id: str, k_values, fast: bool) -> dict:
    probes, meta = load_probes(PROBES_PATH)
    scope = {meta.get("scope_key", "customer_id"): customer_id}
    report = run_retrieval_benchmark(
        client, probes, scope, k_values=k_values, check_self_consistency=not fast
    )
    return report.to_dict()


def run_inclusion_stage(client: MemoryBankClient) -> dict:
    transcripts, meta = load_transcripts_or_fail()
    transcript = transcripts["mixed_signal_call"]
    ledger = write_policy.run_inclusion(
        client, transcript, engine_label="configured_engine(desk_custom_topics)"
    )
    return {"legend": meta.get("expectation_legend", {}), **ledger.to_dict()}


def run_topics_stage(client: MemoryBankClient) -> dict:
    transcripts, _ = load_transcripts_or_fail()
    cfg = client.cfg
    desk = write_policy.custom_config_from_engine(client)
    configs = [
        desk,
        write_policy.TopicConfig.managed_defaults(),
        write_policy.mixed_config(desk),
    ]
    return {
        "mixed_signal_call": write_policy.run_topic_comparison(
            cfg, transcripts["mixed_signal_call"], configs
        ),
        "personal_info_only": write_policy.run_topic_comparison(
            cfg, transcripts["personal_info_only"], configs
        ),
    }


def run_scope_stage(client: MemoryBankClient) -> dict:
    transcripts, _ = load_transcripts_or_fail()
    return write_policy.run_scope_isolation(client, transcripts["mixed_signal_call"])


def run_consolidation_stage(client: MemoryBankClient) -> dict:
    transcripts, _ = load_transcripts_or_fail()
    return write_policy.run_consolidation(
        client,
        transcripts["consolidation_first"],
        transcripts["consolidation_second"],
        transcripts.get("contradiction"),
    )


def run_reconcile_stage(client: MemoryBankClient, customer_id: str) -> dict:
    reader = GroundTruthReader()
    memories = client.list_memories({"customer_id": customer_id})
    amounts = reconcile.reconcile_amounts(memories, reader, customer_id)
    travel = reconcile.reconcile_travel(memories, reader, customer_id)
    return {
        "amounts": amounts.to_dict(),
        "travel": travel.to_dict(),
        "passed": amounts.passed and travel.passed,
    }


def load_transcripts_or_fail():
    return write_policy.load_transcripts(TRANSCRIPTS_PATH)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Memory Bank evaluation harness.")
    parser.add_argument("--customer", default="CUST00001", help="Customer id to scope retrieval to.")
    parser.add_argument(
        "--only",
        action="append",
        choices=STAGES,
        help="Run only these stages. Repeatable. Default: all.",
    )
    parser.add_argument("--k", default="1,3,5,10", help="Comma-separated k values for the sweep.")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip the judge self-consistency double-run. Halves labelling cost and removes the "
        "stability figure, so the report says it was skipped rather than omitting it.",
    )
    parser.add_argument("--json-out", help="Write the full report here.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_settings()
    client = MemoryBankClient(cfg)
    k_values = tuple(int(x) for x in args.k.split(",") if x.strip())
    stages = args.only or list(STAGES)

    report: dict = {
        "configuration": cfg.redacted(),
        "engine": cfg.agent_engine_resource,
        "customer_id": args.customer,
        "stages_requested": stages,
        "results": {},
        "failures": {},
    }

    runners = {
        "retrieval": lambda: run_retrieval_stage(client, args.customer, k_values, args.fast),
        "inclusion": lambda: run_inclusion_stage(client),
        "topics": lambda: run_topics_stage(client),
        "scope": lambda: run_scope_stage(client),
        "consolidation": lambda: run_consolidation_stage(client),
        "reconcile": lambda: run_reconcile_stage(client, args.customer),
    }

    for stage in stages:
        started = time.perf_counter()
        log.info("--- %s ---", stage)
        try:
            report["results"][stage] = runners[stage]()
            log.info("%s ok in %.1fs", stage, time.perf_counter() - started)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s FAILED", stage)
            report["failures"][stage] = {"error": str(exc), "type": type(exc).__name__}

    report["summary"] = summarize(report)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, default=str)
        log.info("wrote %s", args.json_out)

    print(json.dumps(report["summary"], indent=2, default=str))
    return 1 if report["failures"] else 0


def summarize(report: dict) -> dict:
    """The short version, with the caveats attached rather than dropped."""
    results = report.get("results", {})
    out: dict = {"engine": report.get("engine"), "customer_id": report.get("customer_id")}

    retrieval = results.get("retrieval")
    if retrieval:
        out["retrieval"] = {
            "corpus_size": retrieval["corpus_size"],
            "probes": retrieval["probe_count"],
            "headline": retrieval["headline"],
            "label_agreement": retrieval["labelling"]["agreement_between_labellers"].get(
                "raw_agreement_on_positives"
            ),
            "judge_self_consistency": retrieval["labelling"]["judge_self_consistency"].get(
                "raw_agreement_on_positives"
            ),
            "topic_phrasing": retrieval["topic_phrasing_effect"].get("interpretation"),
        }

    inclusion = results.get("inclusion")
    if inclusion:
        out["inclusion"] = inclusion["score"]

    topics = results.get("topics")
    if topics:
        out["topics"] = {
            name: {
                "interpretation": body["interpretation"],
                "written_per_arm": {
                    arm["config"]["name"]: arm["written_count"] for arm in body["arms"]
                },
            }
            for name, body in topics.items()
        }

    if "scope" in results:
        out["scope"] = results["scope"]["verdict"]
    if "consolidation" in results:
        out["consolidation"] = results["consolidation"]["verdict"]
    if "reconcile" in results:
        out["reconcile"] = {
            "passed": results["reconcile"]["passed"],
            "contradicted_amounts": results["reconcile"]["amounts"]["contradicted_count"],
            "contradicted_destinations": results["reconcile"]["travel"]["contradicted_count"],
        }

    if report.get("failures"):
        out["failures"] = report["failures"]
    return out


if __name__ == "__main__":
    sys.exit(main())

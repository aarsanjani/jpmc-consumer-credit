"""Flask blueprint for the JPMC evaluation scorecard and run results.

Exposes the real metrics produced by `scripts/run_jpmc_eval.py` to the UI,
replacing the legacy circular evaluation numbers.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request

from backend.eval_harness import results

LOG = logging.getLogger(__name__)

eval_api = Blueprint("eval_api", __name__)


@eval_api.get("/api/eval/scorecard")
def get_scorecard():
    """Returns the latest JPMC evaluation scorecard, or n/r if no run has executed."""
    run_id = request.args.get("run_id")
    if run_id:
        run_data = results.load_run(run_id)
    else:
        run_data = results.latest_run()

    return jsonify(results.scorecard_view(run_data))


@eval_api.get("/api/eval/runs")
def list_eval_runs():
    """Lists all recorded evaluation runs, newest first."""
    return jsonify({"runs": results.list_runs()})


@eval_api.get("/api/eval/run/<run_id>")
def get_eval_run(run_id: str):
    """Returns the full raw JSON results for a specific run."""
    run_data = results.load_run(run_id)
    if not run_data:
        return jsonify({"error": f"Run '{run_id}' not found"}), 404
    return jsonify(run_data)

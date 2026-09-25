"""Run results serialization, persistence, and scorecard extraction.

Every run writes one JSON file into `runs_dir_from_env()`, containing:
  - run metadata: run_id, timestamp, git SHA, eval engine id, eval dataset
  - provenance-tagged metrics for all stages
  - criteria table and tiers comparison
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.eval_harness.env import runs_dir_from_env
from backend.provenance import computed, identity, live, unmeasured

LOG = logging.getLogger(__name__)


def git_state() -> dict[str, Any]:
    """Captures repo git commit and dirty status for run provenance."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        sha = "unknown"
    try:
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = bool(status)
    except Exception:
        dirty = False
    return {"sha": sha, "dirty": dirty}


def save_run(run_data: Mapping[str, Any], runs_dir: Path | None = None) -> Path:
    """Writes a run dict atomically to a timestamped JSON file."""
    target_dir = runs_dir or runs_dir_from_env()
    target_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(run_data.get("run_id") or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    file_path = target_dir / f"run_{run_id}.json"
    temp_path = target_dir / f".tmp_run_{run_id}.json"
    content = json.dumps(run_data, indent=2, default=str, ensure_ascii=False)
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(file_path)
    LOG.info("Saved eval run to %s", file_path)
    return file_path


def list_runs(runs_dir: Path | None = None) -> list[str]:
    """Lists available run IDs, newest first."""
    target_dir = runs_dir or runs_dir_from_env()
    if not target_dir.is_dir():
        return []
    run_files = sorted(
        target_dir.glob("run_*.json"),
        key=lambda p: p.name,
        reverse=True,
    )
    return [p.stem.removeprefix("run_") for p in run_files]


def load_run(run_id: str, runs_dir: Path | None = None) -> dict[str, Any] | None:
    """Loads a specific run by ID."""
    target_dir = runs_dir or runs_dir_from_env()
    file_path = target_dir / f"run_{run_id}.json"
    if not file_path.is_file():
        return None
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOG.warning("Failed to parse run file %s: %s", file_path, exc)
        return None


def latest_run(runs_dir: Path | None = None) -> dict[str, Any] | None:
    """Loads the latest run from the runs directory, or None if none exist."""
    runs = list_runs(runs_dir)
    if not runs:
        return None
    return load_run(runs[0], runs_dir)


def scorecard_view(run_data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Formats the latest run into the scorecard view payload for the UI.

    If no run exists yet, returns a valid payload with 'n/r' placeholders
    so the UI can render gracefully without crashing.
    """
    if not run_data:
        return {
            "status": "NO_RUN",
            "run_id": unmeasured("no eval harness run recorded yet"),
            "timestamp": unmeasured("no eval harness run recorded yet"),
            "criteria": [
                {
                    "criterion": "Accuracy of what gets stored",
                    "question": "Is what gets stored accurate, dated and tied to the right card?",
                    "memory_bank": unmeasured("harness not run"),
                    "baseline": unmeasured("harness not run"),
                },
                {
                    "criterion": "Value over a simpler approach",
                    "question": "Same cases, notes in an ordinary table, the agent gets the most recent entries.",
                    "memory_bank": unmeasured("harness not run"),
                    "baseline": unmeasured("harness not run"),
                },
                {
                    "criterion": "Handling of change",
                    "question": "After a change (an unlock an hour later), does memory update, keep both, or get confused?",
                    "memory_bank": unmeasured("harness not run"),
                    "baseline": unmeasured("harness not run"),
                },
                {
                    "criterion": "Behaviour at volume",
                    "question": "With hundreds of unrelated notes, do the relevant ones still come back, as fast?",
                    "memory_bank": unmeasured("harness not run"),
                    "baseline": unmeasured("harness not run"),
                },
                {
                    "criterion": "Control",
                    "question": "Can we steer, inspect, correct, delete, and keep customers separate?",
                    "memory_bank": unmeasured("harness not run"),
                    "baseline": unmeasured("harness not run"),
                },
                {
                    "criterion": "7 JPMC scenarios",
                    "question": "Recall, state change, two cards, noise, false refund, fake promise, VIP escalation.",
                    "memory_bank": unmeasured("harness not run"),
                    "baseline": unmeasured("harness not run"),
                },
            ],
            "tiers": [],
            "storage_accuracy": {},
            "end_to_end": {},
            "change_handling": {},
            "scenarios": {},
        }

    return {
        "status": "COMPLETED",
        "run_id": live(run_data.get("run_id"), "eval_harness.run_id"),
        "timestamp": live(run_data.get("timestamp"), "eval_harness.timestamp"),
        "git": run_data.get("git", {}),
        "engine_id": identity(run_data.get("engine_id"), "eval_harness.engine_id", "eval engine"),
        "dataset": identity(run_data.get("dataset"), "eval_harness.dataset", "eval dataset"),
        "criteria": run_data.get("criteria", []),
        "tiers": run_data.get("tiers", []),
        "storage_accuracy": run_data.get("storage_accuracy", {}),
        "end_to_end": run_data.get("end_to_end", {}),
        "change_handling": run_data.get("change_handling", {}),
        "scenarios": run_data.get("scenarios", {}),
        "load_summary": run_data.get("load_summary", {}),
    }

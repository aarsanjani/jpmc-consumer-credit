"""Hermetic unit tests for JPMC evaluation harness and scorecard API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.eval_harness import results, scoring
from backend.provenance import computed, live, unmeasured


def test_retrieval_metrics_binary_relevance():
    expected = frozenset({"MEM00001", "MEM00002"})
    # Both retrieved, order: MEM00001, MEM00003, MEM00002
    ranked = [frozenset({"MEM00001"}), frozenset({"MEM00003"}), frozenset({"MEM00002"}), frozenset({"MEM00004"})]
    metrics = scoring.case_metrics(ranked, expected, ks=[1, 3, 5, 8])

    assert metrics["recall@1"] == 0.5
    assert metrics["recall@3"] == 1.0
    assert metrics["mrr@8"] == 1.0  # first hit is at rank 1
    assert 0.0 < metrics["ndcg@3"] <= 1.0


def test_retrieval_metrics_no_hits():
    expected = frozenset({"MEM00001"})
    ranked = [frozenset({"MEM00002"}), frozenset({"MEM00003"})]
    metrics = scoring.case_metrics(ranked, expected, ks=[1, 3, 8])

    assert metrics["recall@1"] == 0.0
    assert metrics["recall@3"] == 0.0
    assert metrics["mrr@8"] == 0.0
    assert metrics["ndcg@3"] == 0.0


def test_scorecard_view_no_run_returns_nr_placeholders():
    view = results.scorecard_view(None)
    assert view["status"] == "NO_RUN"
    assert view["run_id"]["value"] == "n/r"
    assert len(view["criteria"]) == 6
    for c in view["criteria"]:
        assert "criterion" in c
        assert c["memory_bank"]["value"] == "n/r"


def test_save_and_load_run(tmp_path: Path):
    run_payload = {
        "run_id": "TEST_RUN_001",
        "timestamp": "2026-09-25T07:00:00Z",
        "engine_id": "195250725104648192",
        "dataset": "jpmc_ccb_fraud_eval",
        "criteria": [
            {
                "criterion": "Accuracy of what gets stored",
                "question": "Is what gets stored accurate?",
                "memory_bank": {"coverage": live(0.92, "eval")},
                "baseline": unmeasured("no baseline"),
            }
        ],
        "tiers": [{"tier": "tier1_simple", "recall@5": {"memory_bank": live(0.85, "eval")}}],
    }

    out_file = results.save_run(run_payload, tmp_path)
    assert out_file.is_file()

    loaded = results.load_run("TEST_RUN_001", tmp_path)
    assert loaded is not None
    assert loaded["run_id"] == "TEST_RUN_001"

    runs = results.list_runs(tmp_path)
    assert runs == ["TEST_RUN_001"]

    latest = results.latest_run(tmp_path)
    assert latest is not None
    assert latest["run_id"] == "TEST_RUN_001"

    view = results.scorecard_view(latest)
    assert view["status"] == "COMPLETED"
    assert view["run_id"]["value"] == "TEST_RUN_001"
    assert len(view["criteria"]) == 1


def test_eval_api_scorecard_blueprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from flask import Flask
    from backend.eval_api import eval_api

    app = Flask(__name__)
    app.register_blueprint(eval_api)

    monkeypatch.setattr(results, "runs_dir_from_env", lambda: tmp_path)

    client = app.test_client()

    # When no run exists
    resp = client.get("/api/eval/scorecard")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "NO_RUN"

    # List runs empty
    resp_list = client.get("/api/eval/runs")
    assert resp_list.status_code == 200
    assert resp_list.get_json() == {"runs": []}

    # Save a run and query it
    results.save_run({"run_id": "RUN_123", "criteria": []}, tmp_path)

    resp2 = client.get("/api/eval/scorecard")
    assert resp2.status_code == 200
    assert resp2.get_json()["status"] == "COMPLETED"

    resp3 = client.get("/api/eval/run/RUN_123")
    assert resp3.status_code == 200
    assert resp3.get_json()["run_id"] == "RUN_123"

    resp4 = client.get("/api/eval/run/DOES_NOT_EXIST")
    assert resp4.status_code == 404

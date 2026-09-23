from backend.memory_eval.metrics import (
    ConfusionCounts, precision, recall, f1, aggregate, ndcg, average_precision, ProbeScore
)

def test_precision_recall_f1_undefined():
    """Empty sets must yield None, not zero, to prevent dragging down aggregates."""
    # Retrieved 0 means precision is undefined
    counts_no_retrieved = ConfusionCounts(true_positives=0, false_positives=0, false_negatives=1, true_negatives=1)
    assert precision(counts_no_retrieved) is None
    
    # Relevant 0 means recall is undefined
    counts_no_relevant = ConfusionCounts(true_positives=0, false_positives=1, false_negatives=0, true_negatives=1)
    assert recall(counts_no_relevant) is None

    # f1 is None if either is None
    assert f1(counts_no_retrieved) is None
    assert f1(counts_no_relevant) is None

def test_aggregate_excludes_none():
    """aggregate() excludes undefined values from its means and reports n_undefined."""
    defined_score = ProbeScore(
        probe_id="p1", 
        question="q", 
        k=5,
        counts=ConfusionCounts(1, 0, 0, 0),
        precision_at_k=1.0, 
        recall_at_k=1.0, 
        f1_at_k=1.0,
        reciprocal_rank=1.0,
        ndcg_at_k=1.0,
        average_precision=1.0,
        retrieved_ids=["m1"],
        relevant_ids=["m1"],
        false_positive_ids=[],
        missed_ids=[]
    )
    
    undefined_score = ProbeScore(
        probe_id="p2", 
        question="q", 
        k=5,
        counts=ConfusionCounts(0, 0, 0, 0),
        precision_at_k=None, 
        recall_at_k=None, 
        f1_at_k=None,
        reciprocal_rank=None,
        ndcg_at_k=None,
        average_precision=None,
        retrieved_ids=[],
        relevant_ids=[],
        false_positive_ids=[],
        missed_ids=[]
    )

    agg = aggregate([defined_score, undefined_score])
    
    assert agg["macro_precision_at_k"]["value"] == 1.0  # (1.0 + None) / 1
    assert agg["macro_precision_at_k"]["n_defined"] == 1
    assert agg["macro_precision_at_k"]["n_undefined"] == 1

def test_ndcg_perfect_ranking():
    """ndcg is 1.0 for a perfectly ordered ranking."""
    retrieved = ["a", "b", "c"]
    relevant = ["a", "b", "c"]
    assert ndcg(retrieved, relevant) == 1.0

def test_average_precision_worked_example():
    """average_precision on a known worked example."""
    # 5 retrieved items, relevance: 1, 0, 1, 0, 0
    # Relevant set has 3 items (so one miss).
    retrieved = ["a", "fake", "b", "fake2", "fake3"]
    relevant = ["a", "b", "c"]
    
    # rank 1 is hit (precision = 1/1 = 1.0)
    # rank 3 is hit (precision = 2/3 = 0.666...)
    # sum of precision = 1.666...
    # AP = 1.666... / min(len(relevant), len(retrieved)) = 1.666... / min(3, 5) = 1.666... / 3 = 0.5555...
    assert round(average_precision(retrieved, relevant), 4) == 0.5556

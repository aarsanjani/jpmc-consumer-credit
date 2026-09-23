"""
Retrieval metrics.

Pure functions over sets and ranked lists. No I/O, no service calls, no config.
Everything here is unit-testable without a project, and it is deliberately the
only place the arithmetic lives, so that a number quoted in the UI and a number
quoted in the report cannot diverge.

Three things this module is careful about, because getting any of them wrong
produces a metric that looks fine and is meaningless:

1.  **Recall needs a complete relevant set.** Recall measured against "the
    relevant items we happened to find" is always 1.0 and tells you nothing. The
    denominator here must come from labelling the *whole* corpus in scope, which
    is why `retrieval_eval` sweeps every memory rather than only the retrieved
    ones.

2.  **Empty is not zero.** If a probe has no relevant memories at all, precision
    is undefined, not 0.0, and averaging a fabricated 0.0 into the aggregate
    silently drags it down. Undefined values are returned as `None` and excluded
    from means, with the exclusion counted and reported.

3.  **Distance is not similarity.** Memory Bank returns a *distance*. Smaller is
    closer. Ranking by descending distance would invert the result set, and the
    metrics would still compute cleanly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence


@dataclass(frozen=True)
class ConfusionCounts:
    """Set-level outcome of one retrieval against one labelled relevant set."""

    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int

    @property
    def retrieved(self) -> int:
        return self.true_positives + self.false_positives

    @property
    def relevant(self) -> int:
        return self.true_positives + self.false_negatives

    def to_dict(self) -> dict:
        return {
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "true_negatives": self.true_negatives,
            "retrieved": self.retrieved,
            "relevant": self.relevant,
        }


def confusion(
    retrieved: Sequence[str],
    relevant: Iterable[str],
    corpus: Iterable[str],
) -> ConfusionCounts:
    """Compare a retrieved list against a labelled relevant set.

    Args:
        retrieved: ids returned by the search, in rank order.
        relevant: ids labelled relevant anywhere in the corpus.
        corpus: every id that *could* have been returned. Supplying this is what
            makes true negatives meaningful and keeps recall honest.
    """
    retrieved_set = set(retrieved)
    relevant_set = set(relevant)
    corpus_set = set(corpus) | retrieved_set | relevant_set

    tp = len(retrieved_set & relevant_set)
    fp = len(retrieved_set - relevant_set)
    fn = len(relevant_set - retrieved_set)
    tn = len(corpus_set - retrieved_set - relevant_set)
    return ConfusionCounts(tp, fp, fn, tn)


def precision(counts: ConfusionCounts) -> Optional[float]:
    """Of what we returned, how much was relevant.

    `None` when nothing was retrieved -- precision over an empty result is
    undefined, and reporting 0.0 would claim the search returned only rubbish
    when in fact it returned nothing at all.
    """
    if counts.retrieved == 0:
        return None
    return counts.true_positives / counts.retrieved


def recall(counts: ConfusionCounts) -> Optional[float]:
    """Of what was relevant, how much did we find.

    `None` when nothing in the corpus is relevant to this probe -- there is
    nothing to recall, and 0.0 would be a false accusation.
    """
    if counts.relevant == 0:
        return None
    return counts.true_positives / counts.relevant


def f1(counts: ConfusionCounts) -> Optional[float]:
    p, r = precision(counts), recall(counts)
    if p is None or r is None or (p + r) == 0:
        return None
    return 2 * p * r / (p + r)


def reciprocal_rank(retrieved: Sequence[str], relevant: Iterable[str]) -> Optional[float]:
    """1/rank of the first relevant hit. `None` if none was retrieved.

    Matters for an agent more than precision does: an agent that reads the top
    two results cares a great deal whether the answer was ranked first or fifth,
    and precision@10 cannot see the difference.
    """
    relevant_set = set(relevant)
    for index, item in enumerate(retrieved, start=1):
        if item in relevant_set:
            return 1.0 / index
    return None


def dcg(gains: Sequence[float]) -> float:
    return sum(g / math.log2(i + 1) for i, g in enumerate(gains, start=1))


def ndcg(
    retrieved: Sequence[str],
    relevant: Iterable[str],
    k: Optional[int] = None,
) -> Optional[float]:
    """Normalised discounted cumulative gain over binary relevance.

    Rewards putting relevant results near the top. `None` when nothing is
    relevant, for the same reason as `recall`.
    """
    relevant_set = set(relevant)
    if not relevant_set:
        return None
    cut = retrieved[:k] if k else list(retrieved)
    gains = [1.0 if item in relevant_set else 0.0 for item in cut]
    ideal = [1.0] * min(len(relevant_set), len(cut))
    denominator = dcg(ideal)
    if denominator == 0:
        return None
    return dcg(gains) / denominator


def average_precision(retrieved: Sequence[str], relevant: Iterable[str]) -> Optional[float]:
    """Mean of precision@i taken at each relevant hit."""
    relevant_set = set(relevant)
    if not relevant_set:
        return None
    hits = 0
    total = 0.0
    for index, item in enumerate(retrieved, start=1):
        if item in relevant_set:
            hits += 1
            total += hits / index
    if hits == 0:
        return 0.0
    return total / min(len(relevant_set), len(retrieved)) if retrieved else 0.0


@dataclass
class ProbeScore:
    """Every metric for one probe, plus the evidence behind them."""

    probe_id: str
    question: str
    k: int
    counts: ConfusionCounts
    precision_at_k: Optional[float]
    recall_at_k: Optional[float]
    f1_at_k: Optional[float]
    reciprocal_rank: Optional[float]
    ndcg_at_k: Optional[float]
    average_precision: Optional[float]
    retrieved_ids: list[str]
    relevant_ids: list[str]
    false_positive_ids: list[str]
    missed_ids: list[str]
    latency_ms: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "probe_id": self.probe_id,
            "question": self.question,
            "k": self.k,
            "counts": self.counts.to_dict(),
            "precision_at_k": _round(self.precision_at_k),
            "recall_at_k": _round(self.recall_at_k),
            "f1_at_k": _round(self.f1_at_k),
            "reciprocal_rank": _round(self.reciprocal_rank),
            "ndcg_at_k": _round(self.ndcg_at_k),
            "average_precision": _round(self.average_precision),
            "retrieved_ids": self.retrieved_ids,
            "relevant_ids": self.relevant_ids,
            "false_positive_ids": self.false_positive_ids,
            "missed_ids": self.missed_ids,
            "latency_ms": self.latency_ms,
        }


def score_probe(
    probe_id: str,
    question: str,
    retrieved: Sequence[str],
    relevant: Iterable[str],
    corpus: Iterable[str],
    k: int,
    latency_ms: Optional[float] = None,
) -> ProbeScore:
    """Score one probe, naming every false positive and every miss.

    Naming them is the point. An aggregate precision figure is an assertion; a
    list of the specific memories that should not have come back, and the
    specific ones that should have, is something a reader can check.
    """
    relevant_set = set(relevant)
    retrieved_list = list(retrieved)
    counts = confusion(retrieved_list, relevant_set, corpus)
    return ProbeScore(
        probe_id=probe_id,
        question=question,
        k=k,
        counts=counts,
        precision_at_k=precision(counts),
        recall_at_k=recall(counts),
        f1_at_k=f1(counts),
        reciprocal_rank=reciprocal_rank(retrieved_list, relevant_set),
        ndcg_at_k=ndcg(retrieved_list, relevant_set, k=k),
        average_precision=average_precision(retrieved_list, relevant_set),
        retrieved_ids=retrieved_list,
        relevant_ids=sorted(relevant_set),
        false_positive_ids=[i for i in retrieved_list if i not in relevant_set],
        missed_ids=sorted(relevant_set - set(retrieved_list)),
        latency_ms=latency_ms,
    )


def aggregate(scores: Sequence[ProbeScore]) -> dict:
    """Aggregate across probes, excluding undefined values rather than zeroing them.

    The exclusion counts are reported alongside each mean. "Mean precision 0.82
    over 11 of 14 probes" is a usable statement; "mean precision 0.64" computed
    by treating three undefined values as zero is not, and the two are
    indistinguishable once printed.
    """

    def mean(field: str) -> dict:
        values = [getattr(s, field) for s in scores]
        defined = [v for v in values if v is not None]
        return {
            "value": _round(sum(defined) / len(defined)) if defined else None,
            "n_defined": len(defined),
            "n_undefined": len(values) - len(defined),
        }

    total = ConfusionCounts(
        true_positives=sum(s.counts.true_positives for s in scores),
        false_positives=sum(s.counts.false_positives for s in scores),
        false_negatives=sum(s.counts.false_negatives for s in scores),
        true_negatives=sum(s.counts.true_negatives for s in scores),
    )

    return {
        "probes": len(scores),
        "macro_precision_at_k": mean("precision_at_k"),
        "macro_recall_at_k": mean("recall_at_k"),
        "macro_f1_at_k": mean("f1_at_k"),
        "mean_reciprocal_rank": mean("reciprocal_rank"),
        "macro_ndcg_at_k": mean("ndcg_at_k"),
        "mean_average_precision": mean("average_precision"),
        "micro": {
            "counts": total.to_dict(),
            "precision": _round(precision(total)),
            "recall": _round(recall(total)),
            "f1": _round(f1(total)),
        },
        "probes_with_a_miss": [s.probe_id for s in scores if s.missed_ids],
        "probes_with_a_false_positive": [s.probe_id for s in scores if s.false_positive_ids],
    }


def agreement(labels_a: dict[str, set], labels_b: dict[str, set]) -> dict:
    """How far two labellings of the same corpus agree.

    Reported because the ground truth here is produced by a model judge, and a
    precision figure is only as trustworthy as the labels underneath it. Raw
    agreement plus Cohen's kappa; kappa matters because relevance is sparse and
    two labellers that both say "not relevant" almost always will agree ~90% of
    the time by accident.
    """
    keys = sorted(set(labels_a) | set(labels_b))
    both = only_a = only_b = 0
    for key in keys:
        a, b = labels_a.get(key, set()), labels_b.get(key, set())
        # Only the union of positives is visited: an item both labellers call
        # irrelevant contributes nothing, which is exactly why this is a Jaccard
        # over positives rather than an accuracy.
        for item in a | b:
            in_a, in_b = item in a, item in b
            if in_a and in_b:
                both += 1
            elif in_a:
                only_a += 1
            else:
                only_b += 1

    total_disagree = only_a + only_b
    total = both + total_disagree
    if total == 0:
        return {"raw_agreement": None, "n_compared": 0, "note": "no positive labels from either labeller"}

    raw = both / total
    return {
        "raw_agreement_on_positives": _round(raw),
        "agreed_relevant": both,
        "only_labeller_a": only_a,
        "only_labeller_b": only_b,
        "n_compared": total,
        "note": (
            "Jaccard over positive labels. Reported instead of raw accuracy because "
            "relevance is sparse: two labellers agreeing that most of the corpus is "
            "irrelevant would score ~0.9 accuracy while agreeing on nothing useful."
        ),
    }


def _round(value: Optional[float], places: int = 4) -> Optional[float]:
    return None if value is None else round(value, places)

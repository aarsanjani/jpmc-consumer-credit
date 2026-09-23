"""
Measuring retrieval precision and recall against the live Memory Bank.

The shape of one run:

    1. Read the entire corpus for the scope. This is the universe, and it is
       what makes recall meaningful -- the denominator has to be "everything
       that could have been returned", not "everything we happened to see".

    2. Label the whole universe against every probe, with two independent
       labellers. This is the expensive step and the one the result rests on.

    3. Retrieve for each probe, at several values of k.

    4. Score, naming every false positive and every miss.

WHY SEVERAL VALUES OF K
    A single top_k hides the interesting behaviour. Similarity search returns k
    results whether or not k relevant ones exist, so precision falls as k rises
    for purely structural reasons, and a system quoted at "precision 0.9" at k=3
    may be at 0.35 by k=10 without anything having changed. Sweeping k and
    plotting both curves is the only honest way to present this, and it exposes
    the operating point an agent should actually use.

THE TOPIC-PHRASING ARM
    Memory Bank does not support server-side topic filtering -- `retrieve` with
    `filter='topics.label="TRAVEL_STATUS"'` returns
    `400 INVALID_ARGUMENT: No field filter plugin found for path [topics.label]`.
    Verified, four spellings, all rejected.

    So topics cannot narrow the search. What they can do is tell an agent how
    the memories were *phrased* when they were filed, which should let it write
    a closer query. That is a testable claim, so this module tests it: each
    probe is run twice, once as written and once with its expected topic labels
    appended, and the difference in recall is reported. If it makes no
    difference, that is the finding.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

from backend.memory_bank import Memory, MemoryBankClient
from backend.memory_eval import metrics
from backend.memory_eval.labeller import (
    AnchorRelevanceLabeller,
    LabelSet,
    LlmRelevanceLabeller,
    Probe,
    RelevanceLabeller,
)

log = logging.getLogger(__name__)

DEFAULT_K_SWEEP = (1, 3, 5, 10)


@dataclass
class LabelledCorpus:
    """The scope's full corpus plus one labelling of it."""

    scope: dict
    corpus: list[Memory]
    labels: dict[str, LabelSet]
    labeller_name: str
    elapsed_s: float

    @property
    def corpus_ids(self) -> list[str]:
        return [m.memory_id for m in self.corpus]

    def relevant_for(self, probe_id: str) -> set[str]:
        label_set = self.labels.get(probe_id)
        return label_set.relevant_ids if label_set else set()

    def to_dict(self) -> dict:
        return {
            "labeller": self.labeller_name,
            "scope": self.scope,
            "corpus_size": len(self.corpus),
            "elapsed_s": round(self.elapsed_s, 1),
            "labels": {pid: ls.to_dict() for pid, ls in self.labels.items()},
            "relevant_count_by_probe": {
                pid: len(ls.relevant_ids) for pid, ls in self.labels.items()
            },
        }


def label_corpus(
    probes: Sequence[Probe],
    corpus: Sequence[Memory],
    labeller: RelevanceLabeller,
    scope: dict,
) -> LabelledCorpus:
    started = time.perf_counter()
    labels = {probe.probe_id: labeller.label(probe, corpus) for probe in probes}
    return LabelledCorpus(
        scope=dict(scope),
        corpus=list(corpus),
        labels=labels,
        labeller_name=labeller.name,
        elapsed_s=time.perf_counter() - started,
    )


@dataclass
class RetrievalRun:
    """Everything one retrieval configuration produced."""

    arm: str
    k: int
    scores: list[metrics.ProbeScore]
    aggregate: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "arm": self.arm,
            "k": self.k,
            "aggregate": self.aggregate,
            "probes": [s.to_dict() for s in self.scores],
        }


def evaluate(
    client: MemoryBankClient,
    probes: Sequence[Probe],
    scope: dict,
    labelled: LabelledCorpus,
    k_values: Sequence[int] = DEFAULT_K_SWEEP,
    topic_phrasing: bool = False,
) -> list[RetrievalRun]:
    """Retrieve and score every probe at every k.

    Args:
        topic_phrasing: append the probe's expected topic labels to the query
            text. The topic-vocabulary arm described in the module docstring.
    """
    arm = "topic_phrased" if topic_phrasing else "plain"
    corpus_ids = labelled.corpus_ids
    runs: list[RetrievalRun] = []

    for k in k_values:
        scores: list[metrics.ProbeScore] = []
        for probe in probes:
            query = probe.question
            if topic_phrasing and probe.expected_topics:
                readable = " ".join(t.replace("_", " ").lower() for t in probe.expected_topics)
                query = f"{probe.question} ({readable})"

            result = client.retrieve(scope, query=query, top_k=k)
            scores.append(
                metrics.score_probe(
                    probe_id=probe.probe_id,
                    question=query,
                    retrieved=result.ids(),
                    relevant=labelled.relevant_for(probe.probe_id),
                    corpus=corpus_ids,
                    k=k,
                    latency_ms=result.latency_ms.value,
                )
            )
        runs.append(RetrievalRun(arm=arm, k=k, scores=scores, aggregate=metrics.aggregate(scores)))

    return runs


def self_consistency(
    probes: Sequence[Probe],
    corpus: Sequence[Memory],
    labeller: LlmRelevanceLabeller,
    scope: dict,
) -> dict:
    """Run the model judge twice and report how far it agrees with itself.

    Temperature 0 is widely assumed to mean deterministic. It does not reliably,
    and if the judge is unstable then so is every metric derived from it. Better
    to measure that and print it than to assume it and be quietly wrong.
    """
    first = label_corpus(probes, corpus, labeller, scope)
    second = label_corpus(probes, corpus, labeller, scope)
    agreement = metrics.agreement(
        {pid: ls.relevant_ids for pid, ls in first.labels.items()},
        {pid: ls.relevant_ids for pid, ls in second.labels.items()},
    )
    agreement["probes_that_differed"] = sorted(
        pid
        for pid in first.labels
        if first.labels[pid].relevant_ids != second.labels.get(pid, LabelSet(pid, set())).relevant_ids
    )
    return agreement


@dataclass
class RetrievalReport:
    scope: dict
    corpus_size: int
    probes: list[Probe]
    llm_labels: LabelledCorpus
    anchor_labels: LabelledCorpus
    label_agreement: dict
    judge_self_consistency: dict
    runs: list[RetrievalRun]

    def headline(self) -> dict:
        """The operating point most worth quoting, and why it was chosen.

        k=5 rather than the best-scoring k. Choosing k after seeing the results
        is how a benchmark becomes a sales sheet; k=5 is picked because it is a
        plausible amount of context to hand a model, and the full sweep is
        printed beside it so the choice can be second-guessed.
        """
        target = next((r for r in self.runs if r.arm == "plain" and r.k == 5), None)
        if target is None:
            target = self.runs[-1] if self.runs else None
        if target is None:
            return {}
        return {
            "k": target.k,
            "arm": target.arm,
            "macro_precision": target.aggregate["macro_precision_at_k"],
            "macro_recall": target.aggregate["macro_recall_at_k"],
            "macro_f1": target.aggregate["macro_f1_at_k"],
            "mrr": target.aggregate["mean_reciprocal_rank"],
            "ndcg": target.aggregate["macro_ndcg_at_k"],
            "why_this_k": (
                "k=5 chosen in advance as a plausible context budget, not selected "
                "after seeing which k scored best. The full sweep is reported."
            ),
        }

    def topic_phrasing_effect(self) -> dict:
        """Did phrasing the query in the filed vocabulary help?"""
        out: dict = {}
        for k in sorted({r.k for r in self.runs}):
            plain = next((r for r in self.runs if r.arm == "plain" and r.k == k), None)
            phrased = next((r for r in self.runs if r.arm == "topic_phrased" and r.k == k), None)
            if not plain or not phrased:
                continue
            p_recall = plain.aggregate["macro_recall_at_k"]["value"]
            t_recall = phrased.aggregate["macro_recall_at_k"]["value"]
            delta = None if p_recall is None or t_recall is None else round(t_recall - p_recall, 4)
            out[f"k={k}"] = {
                "plain_recall": p_recall,
                "topic_phrased_recall": t_recall,
                "delta": delta,
            }
        deltas = [v["delta"] for v in out.values() if v["delta"] is not None]
        out["interpretation"] = (
            "No measurable benefit from topic phrasing at any k. Topics govern what is "
            "written, not how well it is found."
            if deltas and all(abs(d) < 1e-9 for d in deltas)
            else (
                f"Mean recall delta {round(sum(deltas) / len(deltas), 4):+} from phrasing the "
                f"query in the topic vocabulary. Note this is a query-wording effect, not "
                f"server-side filtering: Memory Bank rejects topic filters outright."
                if deltas
                else "Not enough defined values to compare."
            )
        )
        return out

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "corpus_size": self.corpus_size,
            "probe_count": len(self.probes),
            "headline": self.headline(),
            "labelling": {
                "llm": self.llm_labels.to_dict(),
                "anchor": self.anchor_labels.to_dict(),
                "agreement_between_labellers": self.label_agreement,
                "judge_self_consistency": self.judge_self_consistency,
                "note": (
                    "Metrics below use the LLM labels. The anchor labeller is a deterministic "
                    "cross-check that under-labels by construction, so where the two disagree "
                    "the true relevant set is usually between them."
                ),
            },
            "topic_phrasing_effect": self.topic_phrasing_effect(),
            "runs": [r.to_dict() for r in self.runs],
        }


def run_retrieval_benchmark(
    client: MemoryBankClient,
    probes: Sequence[Probe],
    scope: dict,
    k_values: Sequence[int] = DEFAULT_K_SWEEP,
    check_self_consistency: bool = True,
) -> RetrievalReport:
    """End to end: read the corpus, label it twice over, retrieve, score."""
    corpus = client.list_memories(scope=scope)
    if not corpus:
        raise ValueError(
            f"scope {scope} holds no memories. Recall over an empty corpus is undefined, "
            f"and scoring it would produce numbers that look like results."
        )
    log.info("corpus for %s: %d memories", scope, len(corpus))

    llm_labeller = LlmRelevanceLabeller()
    anchor_labeller = AnchorRelevanceLabeller()

    llm_labels = label_corpus(probes, corpus, llm_labeller, scope)
    anchor_labels = label_corpus(probes, corpus, anchor_labeller, scope)

    agreement = metrics.agreement(
        {pid: ls.relevant_ids for pid, ls in llm_labels.labels.items()},
        {pid: ls.relevant_ids for pid, ls in anchor_labels.labels.items()},
    )

    consistency = (
        self_consistency(probes, corpus, llm_labeller, scope)
        if check_self_consistency
        else {"skipped": "check_self_consistency=False"}
    )

    runs = evaluate(client, probes, scope, llm_labels, k_values, topic_phrasing=False)
    runs += evaluate(client, probes, scope, llm_labels, k_values, topic_phrasing=True)

    return RetrievalReport(
        scope=dict(scope),
        corpus_size=len(corpus),
        probes=list(probes),
        llm_labels=llm_labels,
        anchor_labels=anchor_labels,
        label_agreement=agreement,
        judge_self_consistency=consistency,
        runs=runs,
    )

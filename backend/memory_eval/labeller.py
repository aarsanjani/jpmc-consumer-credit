"""
Establishing ground truth for retrieval.

A precision figure is only as good as the labels underneath it, so this module
is written to be argued with rather than believed.

The problem: to measure recall you need to know, for each probe, *every* memory
in the corpus that ought to have come back. Not the ones that did come back --
that would make recall 1.0 by construction. So something has to look at all 12
memories for every probe and decide.

Two labellers do that, independently, and the report shows both:

    LlmRelevanceLabeller      Gemini judges every memory against every probe.
                              Understands paraphrase. Is a model, so it is
                              fallible and non-free, and it is judging a system
                              built on the same family of models.

    AnchorRelevanceLabeller   A memory is relevant if it contains one of the
                              probe's anchor strings. Deterministic, free,
                              reproducible by anyone reading the JSON. Blind to
                              paraphrase, so it under-labels.

Neither is authoritative. Where they agree, the label is solid. Where they
diverge, `metrics.agreement` quantifies it and the report says so rather than
quietly preferring whichever produces the nicer headline.

The LLM judge is also run twice at temperature 0 and its self-consistency is
reported, because "temperature 0 is deterministic" is a thing people believe
about these APIs that is not reliably true.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from backend.config import Settings, settings as load_settings
from backend.memory_bank import Memory

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Probe:
    """One benchmark question."""

    probe_id: str
    question: str
    intent: str
    expected_topics: tuple[str, ...]
    anchor_terms: tuple[str, ...]
    ground_truth_anchor: dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: dict) -> "Probe":
        return cls(
            probe_id=payload["probe_id"],
            question=payload["question"],
            intent=payload.get("intent", ""),
            expected_topics=tuple(payload.get("expected_topics", [])),
            anchor_terms=tuple(payload.get("anchor_terms", [])),
            ground_truth_anchor=payload.get("ground_truth_anchor", {}),
        )

    def to_dict(self) -> dict:
        return {
            "probe_id": self.probe_id,
            "question": self.question,
            "intent": self.intent,
            "expected_topics": list(self.expected_topics),
            "anchor_terms": list(self.anchor_terms),
            "ground_truth_anchor": self.ground_truth_anchor,
        }


@dataclass
class LabelSet:
    """Which memories one labeller considers relevant to one probe, and why."""

    probe_id: str
    relevant_ids: set[str]
    reasons: dict[str, str] = field(default_factory=dict)
    labeller: str = ""

    def to_dict(self) -> dict:
        return {
            "probe_id": self.probe_id,
            "labeller": self.labeller,
            "relevant_ids": sorted(self.relevant_ids),
            "reasons": self.reasons,
        }


class RelevanceLabeller(Protocol):
    name: str

    def label(self, probe: Probe, corpus: Sequence[Memory]) -> LabelSet: ...


# --------------------------------------------------------------------------
# Deterministic labeller
# --------------------------------------------------------------------------


class AnchorRelevanceLabeller:
    """A memory is relevant if it mentions one of the probe's anchor terms.

    Crude on purpose. It cannot see that "I got back to New York early" answers
    "when did they return", unless "return" happens to appear. It will therefore
    *under-label*, which means the recall it produces is an upper bound and the
    precision a lower bound -- a bias worth knowing the direction of.

    Its value is that it needs no model, no credentials and no budget, and two
    people running it a month apart get the same answer.
    """

    name = "anchor"

    def label(self, probe: Probe, corpus: Sequence[Memory]) -> LabelSet:
        relevant: set[str] = set()
        reasons: dict[str, str] = {}
        for memory in corpus:
            haystack = memory.fact.lower()
            hits = [
                term
                for term in probe.anchor_terms
                if re.search(rf"\b{re.escape(term.lower())}\b", haystack)
            ]
            if hits:
                relevant.add(memory.memory_id)
                reasons[memory.memory_id] = f"contains anchor term(s): {', '.join(hits)}"
        return LabelSet(probe.probe_id, relevant, reasons, labeller=self.name)


# --------------------------------------------------------------------------
# Model labeller
# --------------------------------------------------------------------------


class _JudgedMemory(BaseModel):
    index: int = Field(..., description="The 1-based index of the memory as numbered in the prompt.")
    relevant: bool = Field(..., description="True if this memory contains information that helps answer the question.")
    reason: str = Field(..., description="One short sentence. Why it does or does not help.")


class _JudgeVerdict(BaseModel):
    judgements: list[_JudgedMemory] = Field(..., description="Exactly one entry per memory, in order.")


JUDGE_INSTRUCTION = """\
You are labelling a retrieval benchmark. You will be given one QUESTION and a \
numbered list of MEMORIES held about a single bank customer.

For EVERY memory, decide whether it contains information that would help a human \
support agent answer the QUESTION. Return exactly one judgement per memory.

Rules:
- Judge helpfulness, not correctness. A memory that answers the question with a \
  fact that later turns out to be wrong is still RELEVANT. Whether the fact is \
  true is a separate check performed elsewhere, and conflating the two makes the \
  retrieval metric un-interpretable.
- Partial help counts. A memory carrying one of several details the question \
  asks for is relevant.
- Topic overlap alone does not count. A memory about travel is not relevant to a \
  question about a card replacement merely because both mention a trip.
- If nothing in the memory bears on the question, say so. Questions with no \
  relevant memory are deliberately included, and labelling something relevant to \
  avoid an empty set would corrupt the benchmark.
"""


class LlmRelevanceLabeller:
    """Gemini as the relevance judge, over the whole corpus, one call per probe.

    One call per probe rather than one per (probe, memory) pair: the judge sees
    all candidates together, which is both far cheaper and a better-posed task,
    because relevance here is partly comparative.
    """

    name = "llm"

    def __init__(
        self,
        cfg: Optional[Settings] = None,
        model: str = "gemini-2.5-flash",
        client: Optional[genai.Client] = None,
    ) -> None:
        self.cfg = cfg or load_settings()
        self.model = model
        self._client = client

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(
                vertexai=True, project=self.cfg.project_id, location=self.cfg.location
            )
        return self._client

    def label(self, probe: Probe, corpus: Sequence[Memory]) -> LabelSet:
        if not corpus:
            return LabelSet(probe.probe_id, set(), {}, labeller=self.name)

        listing = "\n".join(
            f"{i}. {m.fact}" for i, m in enumerate(corpus, start=1)
        )
        prompt = (
            f"QUESTION: {probe.question}\n"
            f"WHY IT IS ASKED: {probe.intent}\n\n"
            f"MEMORIES ({len(corpus)}):\n{listing}\n\n"
            f"Return one judgement for each of the {len(corpus)} memories."
        )

        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=JUDGE_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=_JudgeVerdict,
                temperature=0.0,
            ),
        )

        # No try/except around the parse. A judge that returned something
        # unparseable has not labelled anything, and silently falling back to an
        # empty label set would report recall 1.0 over zero relevant items --
        # a perfect score produced by a failure.
        verdict = _JudgeVerdict.model_validate_json(response.text)

        relevant: set[str] = set()
        reasons: dict[str, str] = {}
        for judged in verdict.judgements:
            if not 1 <= judged.index <= len(corpus):
                log.warning(
                    "judge returned out-of-range index %s for probe %s; ignoring",
                    judged.index,
                    probe.probe_id,
                )
                continue
            memory = corpus[judged.index - 1]
            reasons[memory.memory_id] = judged.reason
            if judged.relevant:
                relevant.add(memory.memory_id)

        missing = len(corpus) - len(reasons)
        if missing:
            log.warning(
                "judge returned %d judgements for %d memories on probe %s; "
                "unjudged memories are treated as NOT relevant, which biases recall upward",
                len(reasons),
                len(corpus),
                probe.probe_id,
            )

        return LabelSet(probe.probe_id, relevant, reasons, labeller=self.name)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def load_probes(path: str) -> tuple[list[Probe], dict]:
    """Read the benchmark file. Returns probes and the surrounding metadata."""
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    probes = [Probe.from_json(p) for p in payload.get("probes", [])]
    if not probes:
        raise ValueError(f"{path} defines no probes")
    meta = {k: v for k, v in payload.items() if k != "probes"}
    return probes, meta

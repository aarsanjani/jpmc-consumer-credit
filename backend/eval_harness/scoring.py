"""Retrieval metrics, and every Gemini call the harness makes, logged as JSONL.

Metrics use binary relevance on expected fixture memory ids. A retrieved item
is relevant when it states an expected fact the list hasn't already covered, so
two copies of one fact don't count twice and nDCG stays within [0, 1]. A case
with no expected memories has no defined Recall/MRR/nDCG; it is left out of
the means (and counted), and the answer judge scores it instead.
"""

import json
import logging
import math
import statistics
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from backend.provenance import NOT_RECORDED

LOG = logging.getLogger(__name__)

KS = (1, 3, 5, 8)
Ranked = Sequence[frozenset[str]]


def covered(ranked: Ranked, expected: frozenset[str], k: int) -> set[str]:
    return set().union(*(ids & expected for ids in ranked[:k])) if ranked else set()


def recall_at_k(ranked: Ranked, expected: frozenset[str], k: int) -> float:
    return len(covered(ranked, expected, k)) / len(expected)


def reciprocal_rank(ranked: Ranked, expected: frozenset[str], k: int) -> float:
    return next((1.0 / rank for rank, ids in enumerate(ranked[:k], 1) if ids & expected), 0.0)


def ndcg_at_k(ranked: Ranked, expected: frozenset[str], k: int) -> float:
    seen: set[str] = set()
    dcg = 0.0
    for rank, ids in enumerate(ranked[:k], 1):
        if new := (ids & expected) - seen:
            dcg += 1.0 / math.log2(rank + 1)
            seen |= new
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(k, len(expected)) + 1))
    return dcg / ideal


def case_metrics(ranked: Ranked, expected: frozenset[str], ks: Iterable[int] = KS) -> dict[str, float]:
    if not expected:
        return {}
    return {name: fn(ranked, expected, k)
            for k in ks
            for name, fn in (("recall@%d" % k, recall_at_k), ("mrr@%d" % k, reciprocal_rank),
                             ("ndcg@%d" % k, ndcg_at_k))}


def mean_metrics(per_case: Iterable[Mapping[str, float]]) -> dict[str, float]:
    scored = [m for m in per_case if m]
    if not scored:
        return {}
    return {name: round(statistics.fmean(m[name] for m in scored), 4) for name in scored[0]}


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


# -- Gemini calls ------------------------------------------------------------

class JsonlLog:
    """Append-only JSONL file shared by worker threads."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, default=str, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as out:
            out.write(line + "\n")


@dataclass(frozen=True)
class Call:
    text: str
    parsed: dict[str, Any] | None
    latency_ms: float
    tokens: dict[str, int | None]
    error: str | None = None


RETRY_CODES = frozenset({429, 500, 503})
RETRY_DELAYS_S = (4.0, 10.0, 30.0, 60.0)


class Gemini:
    """GEMINI_MODEL through google-genai on Vertex, temperature 0, one log line per call."""

    def __init__(self, client: Any, model: str, log: JsonlLog, run_id: str):
        self.client = client
        self.model = model
        self.log = log
        self.run_id = run_id

    def call(self, kind: str, case_id: str, prompt: str, inputs: Mapping[str, Any],
             schema: type[BaseModel] | None = None) -> Call:
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            temperature=0.0,
            **({"response_mime_type": "application/json", "response_schema": schema} if schema else {}))
        started = time.perf_counter()
        call: Call
        for attempt, delay in enumerate((*RETRY_DELAYS_S, None)):
            try:
                response = self.client.models.generate_content(model=self.model, contents=prompt, config=config)
                text = (response.text or "").strip()
                call = Call(text=text, parsed=json.loads(text) if schema and text else None,
                            latency_ms=round((time.perf_counter() - started) * 1000.0, 1),
                            tokens=_tokens(getattr(response, "usage_metadata", None)))
                break
            except errors.APIError as exc:
                if exc.code not in RETRY_CODES or delay is None:
                    call = self._failed(started, exc)
                    break
                LOG.warning("%s %s: HTTP %s, retry %d in %.0fs", kind, case_id, exc.code, attempt + 1, delay)
                time.sleep(delay)
            except Exception as exc:  # noqa: BLE001 - bad JSON or a dropped connection: logged, scored n/r
                call = self._failed(started, exc)
                break
        parsed = call.parsed or {}
        self.log.write({
            "run_id": self.run_id, "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "kind": kind, "case": case_id, "model": self.model, "inputs": dict(inputs),
            "output": call.text, "verdict": parsed.get("verdict", NOT_RECORDED),
            "rationale": parsed.get("rationale", NOT_RECORDED), "parsed": call.parsed,
            "latency_ms": call.latency_ms, "tokens": call.tokens, "error": call.error,
        })
        return call

    @staticmethod
    def _failed(started: float, exc: Exception) -> Call:
        return Call(text="", parsed=None, latency_ms=round((time.perf_counter() - started) * 1000.0, 1),
                    tokens={}, error=repr(exc))


def _tokens(usage: Any) -> dict[str, int | None]:
    if usage is None:
        return {}
    return {name: getattr(usage, attr, None) for name, attr in (
        ("prompt", "prompt_token_count"), ("output", "candidates_token_count"),
        ("thinking", "thoughts_token_count"), ("total", "total_token_count"))}


def genai_client(project: str, location: str) -> Any:
    """Vertex AI client for GEMINI_MODEL calls (google-genai)."""
    from google import genai

    return genai.Client(vertexai=True, project=project, location=location)


# -- judge schemas and prompts ------------------------------------------------

YesNo = Literal["yes", "no", "n/a"]


class FactMatch(BaseModel):
    matched_expected_ids: list[str]
    matched_negative_ids: list[str]
    accurate: YesNo
    dated: YesNo
    right_card: YesNo
    rationale: str


class AnswerJudgment(BaseModel):
    verdict: Literal["CORRECT", "PARTIAL", "INCORRECT", "NO_ANSWER"]
    used_superseded_fact: bool
    unsupported_claim: bool
    rationale: str


class ScenarioJudgment(BaseModel):
    verdict: Literal["PASS", "PARTIAL", "FAIL"]
    rationale: str


class TopicRoute(BaseModel):
    topics: list[str]
    rationale: str


ANSWER_SCORE = {"CORRECT": 1.0, "PARTIAL": 0.5, "INCORRECT": 0.0, "NO_ANSWER": 0.0}
SCENARIO_SCORE = {"PASS": 1.0, "PARTIAL": 0.5, "FAIL": 0.0}

FACT_MATCH_PROMPT = """You are grading what a memory system stored about a bank customer.

STORED MEMORY (written by the memory system; "I" is the customer):
"{fact}"

EXPECTED FACTS for this customer, the answer key. Each has an id:
{expected}

NEVER-STORE ITEMS for this customer: things that must not be kept. Each has an id:
{negatives}

Decide:
1. matched_expected_ids: ids of expected facts this memory states, fully or in substance. A memory may state several. Wording may differ, but the memory must carry the fact's specific content; a shared topic is not a match.
2. matched_negative_ids: ids of never-store items this memory keeps.
3. accurate: "no" if any detail in the memory (date, amount, card, place, name, number) conflicts with a fact it matches; "yes" if it matches and nothing conflicts; "n/a" if it matches nothing.
4. dated: "yes" if the memory keeps the date or time frame the matched fact carries; "no" if it drops it; "n/a" if nothing matched or the fact has no date or time frame.
5. right_card: "yes" if card or account references agree with the matched fact; "no" if they conflict, or are missing where the fact names one; "n/a" if the fact names no card or account.
6. rationale: one or two sentences."""

ANSWER_PROMPT = """You are assisting an agent on a JPMorgan Chase consumer card servicing desk. Today is {today}.
The agent asks about customer {customer_id}:
"{question}"

Everything you have on this customer:
{context}

Answer in at most three sentences, using only the information above. If it does not answer the question, say it is not on record. Anything marked [UNVERIFIED customer statement] was said by the customer and has not been verified by the bank; do not present it as fact."""

ANSWER_JUDGE_PROMPT = """You are grading an answer to a bank agent's question about a customer. Today is {today}.

QUESTION: "{question}"

ANSWER KEY, the facts a correct answer relies on (current as of today):
{expected}

OUTDATED OR TEMPTING FACTS, true once or related, but not the answer:
{distractors}

ANSWER TO GRADE:
"{answer}"

verdict:
- CORRECT: consistent with the answer key and covers its substance.
- PARTIAL: consistent, but misses part of the key or hedges where the key is clear.
- INCORRECT: contradicts the key, relies on an outdated fact, or asserts something the key does not support. When the key is empty, any substantive claim about the customer is INCORRECT.
- NO_ANSWER: says it is not on record while the key has an answer.
used_superseded_fact: true if the answer presents an outdated or tempting fact as current.
unsupported_claim: true if the answer states specifics found in neither list.
rationale: one or two sentences."""

SCENARIO_ANSWER_PROMPT = """You are a JPMorgan Chase card servicing agent in a live conversation with customer {customer_id}. Today is {today}.

What you have on this customer from earlier conversations:
{context}

The customer says: "{message}"

Reply to the customer in at most three sentences, using only the information above. You cannot see account systems in this conversation, so for balances, limits, transactions or refunds say what you will check rather than assume. Anything marked [UNVERIFIED customer statement] was said by the customer and has not been verified by the bank."""

SCENARIO_JUDGE_PROMPT = """You are grading a bank agent's reply in a test scenario.

SCENARIO: {title}

EARLIER SESSIONS:
{transcript}

THE CUSTOMER NOW SAYS: "{message}"

WHAT A GOOD REPLY DOES: {expectation}

REPLY TO GRADE:
"{reply}"

verdict: PASS if the reply does what a good reply does; PARTIAL if it goes part of the way (for example it hedges on something it should know); FAIL if it does the opposite or misses the point (for example it treats an unverified claim as fact, forgets or mixes up the state, or approves what it should block).
rationale: one or two sentences."""

ROUTER_PROMPT = """A bank agent is about to read stored memories about a customer to answer a question. Pick the memory topics worth reading, from this list only:
{topics}

QUESTION: "{question}"

Return one to three topic labels, most relevant first, and a one-sentence rationale."""

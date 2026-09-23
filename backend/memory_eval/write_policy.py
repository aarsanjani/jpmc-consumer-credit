"""
What Memory Bank writes, and what it throws away.

This module exists to answer a question that is usually asserted rather than
measured: *does `generateMemories` actually exercise judgement, or does it just
store whatever it is handed?*

The method is a before/after diff. Take a transcript whose every utterance was
labelled `durable`, `disposable` or `warehouse` by a human **before** the run.
Write it into a scope that did not exist a second ago. Read the scope back. What
is there was kept; what is not was excluded. Nothing is inferred from the API
response alone -- the corpus is the evidence.

FOUR EXPERIMENTS

  1. inclusion    One transcript, one engine. Which utterances survived, under
                  which topic, and which were dropped.

  2. topics       The same transcript against engines that differ *only* in
                  their topic vocabulary. This is the controlled experiment: if
                  the output changes when nothing but the topic list changed,
                  topics are the filter.

  3. scope        Whether a scope is a genuine partition or a ranking hint.

  4. consolidation  Whether restating a fact updates the existing memory or
                  appends a second, and whether a contradiction revises it.

A NOTE ON THE THROWAWAY ENGINES
    Experiment 2 needs engines whose topic configuration differs from the
    desk's. Rather than mutating the configuration of the engine the application
    is pointed at -- which would disturb live data and leave it changed if the
    process died mid-run -- the harness creates its own, uses them, and deletes
    them in a `finally`. Creation takes about a second.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional, Sequence

from backend.config import Settings, settings as load_settings
from backend.memory_bank import GenerateResult, Memory, MemoryBankClient

log = logging.getLogger(__name__)

DURABLE = "durable"
DISPOSABLE = "disposable"
WAREHOUSE = "warehouse"

MANAGED_TOPICS = (
    "USER_PERSONAL_INFO",
    "USER_PREFERENCES",
    "KEY_CONVERSATION_DETAILS",
    "EXPLICIT_INSTRUCTIONS",
)
"""The four topics Memory Bank provides out of the box.

Worth knowing: if `customizationConfigs` is omitted entirely, the service does
not extract nothing and it does not extract everything -- it falls back to
exactly these four. "No topics configured" is therefore not a neutral control,
which is why `TopicConfig.managed_defaults()` states them explicitly rather than
leaving the field empty and hoping.
"""


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Utterance:
    utterance_id: str
    role: str
    text: str
    expectation: str
    why: str = ""

    def as_turn(self) -> dict:
        return {"role": self.role, "text": self.text}


@dataclass(frozen=True)
class Transcript:
    transcript_id: str
    summary: str
    turns: tuple[Utterance, ...]

    def as_turns(self) -> list[dict]:
        return [u.as_turn() for u in self.turns]

    def expected(self, kind: str) -> list[Utterance]:
        return [u for u in self.turns if u.expectation == kind]


def load_transcripts(path: str) -> tuple[dict[str, Transcript], dict]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    transcripts = {
        t["transcript_id"]: Transcript(
            transcript_id=t["transcript_id"],
            summary=t.get("summary", ""),
            turns=tuple(
                Utterance(
                    utterance_id=u["utterance_id"],
                    role=u.get("role", "user"),
                    text=u["text"],
                    expectation=u.get("expectation", DISPOSABLE),
                    why=u.get("why", ""),
                )
                for u in t.get("turns", [])
            ),
        )
        for t in payload.get("transcripts", [])
    }
    meta = {k: v for k, v in payload.items() if k != "transcripts"}
    return transcripts, meta


# --------------------------------------------------------------------------
# Topic configurations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TopicConfig:
    """A topic vocabulary to stand an engine up with."""

    name: str
    rationale: str
    custom: tuple[tuple[str, str], ...] = ()
    managed: tuple[str, ...] = ()

    def as_context_spec(self) -> dict:
        topics: list[dict] = []
        for label, description in self.custom:
            topics.append({"customMemoryTopic": {"label": label, "description": description}})
        for enum_value in self.managed:
            topics.append({"managedMemoryTopic": {"managedTopicEnum": enum_value}})
        return {"memoryBankConfig": {"customizationConfigs": [{"memoryTopics": topics}]}}

    def labels(self) -> list[str]:
        return [label for label, _ in self.custom] + list(self.managed)

    @classmethod
    def managed_defaults(cls) -> "TopicConfig":
        return cls(
            name="managed_defaults",
            rationale=(
                "The four topics Vertex ships. A general-purpose assistant's vocabulary: "
                "who the user is, what they like, what was concluded, what they asked to be remembered."
            ),
            managed=MANAGED_TOPICS,
        )


def custom_config_from_engine(client: MemoryBankClient) -> TopicConfig:
    """Mirror the live engine's vocabulary, read from the engine.

    Read rather than restated. A hardcoded copy of the desk's seven topics would
    be wrong the moment anybody edited one in the console, and the experiment
    would then be comparing against a vocabulary that no longer exists.
    """
    topics = [t for t in client.memory_topics() if t.kind == "CUSTOM"]
    return TopicConfig(
        name="desk_custom",
        rationale=(
            "The fraud desk's own vocabulary, read live from the configured engine. "
            "Narrow and domain-specific by design."
        ),
        custom=tuple((t.label, t.description) for t in topics),
    )


def mixed_config(custom: TopicConfig) -> TopicConfig:
    return TopicConfig(
        name="custom_plus_managed",
        rationale=(
            "The desk's vocabulary plus the four defaults. Tests whether adding general "
            "topics recovers the facts the narrow vocabulary drops, and what it costs."
        ),
        custom=custom.custom,
        managed=MANAGED_TOPICS,
    )


# --------------------------------------------------------------------------
# Throwaway engines
# --------------------------------------------------------------------------


@contextmanager
def temporary_engine(
    cfg: Settings,
    topic_config: TopicConfig,
    keep: bool = False,
) -> Iterator[MemoryBankClient]:
    """Create an engine with a given topic vocabulary; delete it afterwards.

    Deletion is in a `finally`, so an exception mid-experiment still cleans up.
    `keep=True` leaves it behind for inspection, and says so in the log, because
    an engine that silently outlives the run is a resource leak someone else
    pays for.
    """
    admin = MemoryBankClient(cfg)
    body = {
        "displayName": f"jpmc-memeval-{topic_config.name}-{uuid.uuid4().hex[:6]}",
        "description": (
            "Ephemeral engine created by backend/memory_eval/write_policy.py to isolate "
            "topic configuration as the only variable. Safe to delete."
        ),
        "contextSpec": topic_config.as_context_spec(),
    }
    parent = (
        f"projects/{cfg.project_number or cfg.project_id}"
        f"/locations/{cfg.location}/reasoningEngines"
    )
    operation = admin._request("POST", parent, body=body)
    engine_name = operation.get("name", "").split("/operations/")[0]
    if not engine_name:
        raise RuntimeError(f"could not determine engine name from {operation}")

    admin._await_operation(operation["name"], timeout_s=180)
    engine_id = engine_name.rsplit("/", 1)[-1]
    log.info("created ephemeral engine %s (%s)", engine_id, topic_config.name)

    scoped_cfg = Settings(
        project_id=cfg.project_id,
        location=cfg.location,
        agent_engine_id=engine_id,
        bigquery_dataset=cfg.bigquery_dataset,
        dataplex_glossary_id=cfg.dataplex_glossary_id,
        project_number=cfg.project_number,
    )
    client = MemoryBankClient(scoped_cfg)
    try:
        yield client
    finally:
        if keep:
            log.warning("leaving ephemeral engine %s in place (keep=True)", engine_id)
        else:
            try:
                admin._request("DELETE", engine_name, params={"force": "true"})
                log.info("deleted ephemeral engine %s", engine_id)
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "FAILED to delete ephemeral engine %s: %s. Delete it by hand.",
                    engine_name,
                    exc,
                )


# --------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------


_STOPWORDS = {
    "the", "and", "you", "your", "that", "this", "with", "for", "have", "has",
    "was", "will", "not", "are", "about", "from", "there", "their", "they",
    "can", "please", "also", "one", "more", "thing", "very", "much", "just",
    "i", "me", "my", "am", "is", "it", "a", "an", "to", "of", "on", "in", "at",
}


def _content_tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9]+", text.lower())
        if len(t) > 2 and t not in _STOPWORDS
    }


def attribute(
    memory_fact: str,
    utterances: Sequence[Utterance],
    threshold: float = 0.22,
    min_shared: int = 2,
) -> list[str]:
    """Guess which utterances a written memory came from, by weighted token overlap.

    Deliberately approximate, and reported as such. `generateMemories`
    paraphrases and merges, so there is no identifier linking a memory back to a
    turn. Overlap is a reasonable proxy for a short transcript and would not be
    for a long one.

    Two guards, both added after the naive version produced a wrong row. It
    attributed "What time do you close today?" to a memory about travel dates,
    on the strength of the single word *time*, which the paraphrase had
    introduced ("during this time") and the original utterance happened to share:

      * `min_shared` -- one word in common is a coincidence, not an origin.
      * IDF weighting over the transcript -- a token appearing in many
        utterances is weak evidence for any particular one, so it is discounted.

    Used only to *narrate* the ledger. The pass/fail arithmetic in
    `score_inclusion` is computed from expectation counts, which are exact.
    """
    memory_tokens = _content_tokens(memory_fact)
    if not memory_tokens:
        return []

    token_sets = {u.utterance_id: _content_tokens(u.text) for u in utterances}
    document_frequency: dict[str, int] = {}
    for tokens in token_sets.values():
        for token in tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1

    def weight(token: str) -> float:
        return 1.0 / document_frequency.get(token, 1)

    scored: list[tuple[float, str]] = []
    for utterance in utterances:
        tokens = token_sets[utterance.utterance_id]
        if not tokens:
            continue
        shared = tokens & memory_tokens
        if len(shared) < min_shared:
            continue
        denominator = sum(weight(t) for t in tokens)
        if denominator == 0:
            continue
        score = sum(weight(t) for t in shared) / denominator
        if score >= threshold:
            scored.append((score, utterance.utterance_id))

    scored.sort(reverse=True)
    return [uid for _, uid in scored]


# --------------------------------------------------------------------------
# Experiment 1 -- inclusion
# --------------------------------------------------------------------------


@dataclass
class InclusionLedger:
    """Per-utterance verdict for one transcript against one engine."""

    transcript_id: str
    engine_label: str
    scope: dict
    written: list[Memory]
    generate: GenerateResult
    rows: list[dict] = field(default_factory=list)
    score: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "transcript_id": self.transcript_id,
            "engine": self.engine_label,
            "scope": self.scope,
            "written_count": len(self.written),
            "written": [m.to_dict() for m in self.written],
            "ledger": self.rows,
            "score": self.score,
            "generate": self.generate.to_dict(),
        }


def score_inclusion(transcript: Transcript, written: Sequence[Memory]) -> dict:
    """Score writes against the expectations set before the run.

    Two figures, and they answer different questions:

      write_precision  Of what was stored, how much was worth storing. Low means
                       the bank is filling up with noise, and every future
                       retrieval pays for it.

      durable_coverage Of the facts we said were worth keeping, how many
                       survived. Low means the bank is losing information.

    Coverage is computed by matching each durable utterance against the written
    facts by token overlap, so it inherits `attribute`'s approximation. Stated
    here rather than buried: this number is indicative, and the ledger beneath
    it is the evidence.
    """
    durable = transcript.expected(DURABLE)
    disposable = transcript.expected(DISPOSABLE)
    warehouse = transcript.expected(WAREHOUSE)

    covered: set[str] = set()
    for memory in written:
        for uid in attribute(memory.fact, transcript.turns):
            covered.add(uid)

    durable_covered = [u.utterance_id for u in durable if u.utterance_id in covered]
    durable_lost = [u.utterance_id for u in durable if u.utterance_id not in covered]
    disposable_leaked = [u.utterance_id for u in disposable if u.utterance_id in covered]
    warehouse_leaked = [u.utterance_id for u in warehouse if u.utterance_id in covered]

    attributable = [m for m in written if attribute(m.fact, transcript.turns)]
    good = [
        m
        for m in attributable
        if any(
            u.expectation == DURABLE
            for u in transcript.turns
            if u.utterance_id in attribute(m.fact, transcript.turns)
        )
    ]

    return {
        "memories_written": len(written),
        "durable_utterances": len(durable),
        "durable_retained": len(durable_covered),
        "durable_retained_ids": durable_covered,
        "durable_lost_ids": durable_lost,
        "durable_coverage": round(len(durable_covered) / len(durable), 4) if durable else None,
        "disposable_utterances": len(disposable),
        "disposable_leaked_ids": disposable_leaked,
        "warehouse_utterances": len(warehouse),
        "warehouse_leaked_ids": warehouse_leaked,
        "write_precision": round(len(good) / len(written), 4) if written else None,
        "caveat": (
            "Coverage and write_precision rely on token-overlap attribution between a "
            "paraphrased memory and the originating utterance, which is approximate. "
            "The per-utterance ledger is the evidence; these are the summary."
        ),
    }


def run_inclusion(
    client: MemoryBankClient,
    transcript: Transcript,
    engine_label: str,
    scope: Optional[dict] = None,
    cleanup: bool = True,
) -> InclusionLedger:
    """Write one transcript into a fresh scope and record what survived."""
    scope = scope or {"memeval_run": uuid.uuid4().hex[:12]}

    result = client.generate_from_conversation(transcript.as_turns(), scope=scope, wait=True)
    # Read the corpus back rather than trusting the operation response. The
    # response carries memory names but not always the facts, and the corpus is
    # the thing that will actually be retrieved later.
    written = client.list_memories(scope=scope)

    rows: list[dict] = []
    for utterance in transcript.turns:
        matched = [m for m in written if utterance.utterance_id in attribute(m.fact, transcript.turns)]
        rows.append(
            {
                "utterance_id": utterance.utterance_id,
                "role": utterance.role,
                "text": utterance.text,
                "expectation": utterance.expectation,
                "why_expected": utterance.why,
                "outcome": "WRITTEN" if matched else "EXCLUDED",
                "agrees_with_expectation": bool(matched) == (utterance.expectation == DURABLE),
                "written_as": [
                    {"short_id": m.short_id, "fact": m.fact, "topics": list(m.topics)}
                    for m in matched
                ],
            }
        )

    ledger = InclusionLedger(
        transcript_id=transcript.transcript_id,
        engine_label=engine_label,
        scope=scope,
        written=written,
        generate=result,
        rows=rows,
        score=score_inclusion(transcript, written),
    )

    if cleanup:
        try:
            client.delete_scope(scope)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not clean up scope %s: %s", scope, exc)

    return ledger


# --------------------------------------------------------------------------
# Experiment 2 -- topics as the control variable
# --------------------------------------------------------------------------


def run_topic_comparison(
    cfg: Settings,
    transcript: Transcript,
    configs: Sequence[TopicConfig],
) -> dict:
    """Same transcript, engines differing only in topic vocabulary.

    This is the experiment that settles the question. If the transcript, the
    scope, the model and the API call are all identical and the only difference
    is the list of topics, then any difference in what gets stored is caused by
    the topics.
    """
    arms: list[dict] = []
    for config in configs:
        started = time.perf_counter()
        with temporary_engine(cfg, config) as client:
            ledger = run_inclusion(
                client,
                transcript,
                engine_label=config.name,
                cleanup=False,  # engine is about to be deleted wholesale
            )
        arms.append(
            {
                "config": {
                    "name": config.name,
                    "rationale": config.rationale,
                    "topic_labels": config.labels(),
                    "topic_count": len(config.labels()),
                },
                "elapsed_s": round(time.perf_counter() - started, 1),
                "written_count": len(ledger.written),
                "written": [
                    {"fact": m.fact, "topics": list(m.topics)} for m in ledger.written
                ],
                "score": ledger.score,
                "ledger": ledger.rows,
            }
        )

    return {
        "transcript_id": transcript.transcript_id,
        "summary": transcript.summary,
        "controlled_variable": "topic vocabulary",
        "held_constant": [
            "transcript text",
            "scope shape",
            "API call and parameters",
            "extraction model (service default)",
        ],
        "arms": arms,
        "interpretation": _interpret_topic_comparison(arms),
    }


def _interpret_topic_comparison(arms: Sequence[dict]) -> str:
    counts = {a["config"]["name"]: a["written_count"] for a in arms}
    distinct = set(counts.values())
    if len(distinct) == 1:
        return (
            f"All arms stored {distinct.pop()} memories. On this transcript the topic "
            f"vocabulary made no observable difference, which is a real result and not "
            f"a failed experiment -- it means every candidate fact was covered by all "
            f"vocabularies tested."
        )
    detail = ", ".join(f"{name}={count}" for name, count in counts.items())
    return (
        f"Arms diverged ({detail}). The transcript, scope and API call were identical "
        f"across arms, so the topic vocabulary is the cause. Topics are an inclusion "
        f"filter on extraction, not a label applied afterwards."
    )


# --------------------------------------------------------------------------
# Experiment 3 -- scope
# --------------------------------------------------------------------------


def run_scope_isolation(client: MemoryBankClient, transcript: Transcript) -> dict:
    """Is a scope a partition or a hint?

    Writes into scope A, then asks scope B -- with a query chosen to match the
    written content as closely as possible, so that if scope were merely a
    ranking signal the memory would still surface.
    """
    run_id = uuid.uuid4().hex[:8]
    scope_a = {"memeval_scope": f"A-{run_id}"}
    scope_b = {"memeval_scope": f"B-{run_id}"}
    probe = transcript.turns[0].text[:120]

    try:
        client.generate_from_conversation(transcript.as_turns(), scope=scope_a, wait=True)
        in_a = client.list_memories(scope=scope_a)
        in_b = client.list_memories(scope=scope_b)
        search_b = client.retrieve(scope_b, query=probe, top_k=10)
        search_a = client.retrieve(scope_a, query=probe, top_k=10)

        # Superset: the same key plus one more. Tests exact-match vs subset
        # semantics, which decides whether a per-session scope key can ever see
        # the customer-level memories written beside it.
        scope_super = dict(scope_a) | {"session_id": f"S-{run_id}"}
        search_super = client.retrieve(scope_super, query=probe, top_k=10)

        return {
            "written_scope": scope_a,
            "written_count": len(in_a),
            "other_scope": scope_b,
            "other_scope_list_count": len(in_b),
            "other_scope_search_count": len(search_b.memories),
            "own_scope_search_count": len(search_a.memories),
            "superset_scope": scope_super,
            "superset_search_count": len(search_super.memories),
            "probe_query": probe,
            "verdict": _interpret_scope(
                len(in_a), len(search_a.memories), len(search_b.memories), len(search_super.memories)
            ),
        }
    finally:
        for scope in (scope_a, scope_b):
            try:
                client.delete_scope(scope)
            except Exception:  # noqa: BLE001
                pass


def _interpret_scope(written: int, own: int, other: int, superset: int) -> str:
    if written == 0:
        return "Inconclusive: nothing was written, so isolation could not be tested."
    parts = []
    if other == 0:
        parts.append(
            f"A semantically near-identical query against a different scope returned 0 of "
            f"{written}. Scope is a hard partition, not a ranking signal -- retrieval cannot "
            f"leak across it, which is what makes it safe as a tenant boundary."
        )
    else:
        parts.append(
            f"WARNING: {other} memories leaked into a different scope. Scope is not "
            f"isolating; do not rely on it as a tenant boundary."
        )
    if superset == 0 and own > 0:
        parts.append(
            "Adding one extra key to the scope returned 0, so scope matching is exact, not "
            "subset. A per-session scope therefore cannot read customer-level memories: the "
            "scope you write with is the scope you must read with."
        )
    elif superset > 0:
        parts.append(
            f"A superset scope returned {superset}, so matching is not strictly exact."
        )
    return " ".join(parts)


# --------------------------------------------------------------------------
# Experiment 4 -- consolidation
# --------------------------------------------------------------------------


def run_consolidation(
    client: MemoryBankClient,
    first: Transcript,
    second: Transcript,
    contradiction: Optional[Transcript] = None,
) -> dict:
    """Does restating a fact update it, duplicate it, or contradict it?

    The distinction matters more than it sounds. An append-only store that keeps
    both "travelling for about two weeks" and "the 14th to the 29th" will later
    retrieve both, and nothing in the result says which one is current.
    """
    scope = {"memeval_consolidation": uuid.uuid4().hex[:10]}
    stages: list[dict] = []
    try:
        for label, transcript in (("first", first), ("second", second)):
            result = client.generate_from_conversation(transcript.as_turns(), scope=scope, wait=True)
            corpus = client.list_memories(scope=scope)
            stages.append(
                {
                    "stage": label,
                    "transcript_id": transcript.transcript_id,
                    "actions": [g.action for g in result.generated],
                    "corpus_size_after": len(corpus),
                    "corpus": [{"short_id": m.short_id, "fact": m.fact, "topics": list(m.topics)} for m in corpus],
                }
            )

        if contradiction is not None:
            result = client.generate_from_conversation(contradiction.as_turns(), scope=scope, wait=True)
            corpus = client.list_memories(scope=scope)
            stages.append(
                {
                    "stage": "contradiction",
                    "transcript_id": contradiction.transcript_id,
                    "actions": [g.action for g in result.generated],
                    "corpus_size_after": len(corpus),
                    "corpus": [{"short_id": m.short_id, "fact": m.fact, "topics": list(m.topics)} for m in corpus],
                }
            )

        return {"scope": scope, "stages": stages, "verdict": _interpret_consolidation(stages)}
    finally:
        try:
            client.delete_scope(scope)
        except Exception:  # noqa: BLE001
            pass


def _interpret_consolidation(stages: Sequence[dict]) -> str:
    if len(stages) < 2:
        return "Inconclusive: fewer than two stages completed."
    after_first = stages[0]["corpus_size_after"]
    after_second = stages[1]["corpus_size_after"]
    second_actions = stages[1]["actions"]

    if after_second == after_first and "UPDATED" in second_actions:
        verdict = (
            f"Restating the same fact more precisely produced an UPDATE: the corpus stayed at "
            f"{after_first}. The service recognised one underlying fact and revised it rather "
            f"than storing a near-duplicate. This is the behaviour that separates a memory bank "
            f"from an append-only log."
        )
    elif after_second > after_first:
        verdict = (
            f"The corpus grew from {after_first} to {after_second}. The restatement was stored "
            f"separately, so retrieval can now return two versions of one fact with nothing to "
            f"indicate which is current."
        )
    else:
        verdict = f"Corpus went from {after_first} to {after_second}; actions were {second_actions}."

    if len(stages) > 2:
        third = stages[2]
        verdict += (
            f" After an explicit contradiction the corpus held {third['corpus_size_after']} "
            f"memories with actions {third['actions']}."
        )
    return verdict

"""The JPMC evaluation stages. scripts/run_jpmc_eval.py runs them in order.

Every comparison uses the same corpus, scope, k, query, cases, model, prompt
and judge on both sides; only the memory layer differs. Stages return plain
data; the `report_*` functions turn it into provenance-tagged results. Detail
lists are tagged as a whole, so every number sits under a tag.
"""

import logging
import random
import re
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from backend import memory_write_path as mwp
from backend.eval_harness import scoring as sc
from backend.eval_harness.bq import FACTS, RAW_NOTES, Ranker, Warehouse
from backend.eval_harness.env import EvalMemoryBank, parallel_map
from backend.eval_harness.fixtures import CUSTOMER_ROLE, Fixtures
from backend.eval_harness.loader import SessionLoad
from backend.eval_harness.scenarios import Scenario
from backend.memory_bank import NOT_RECORDED, topic_labels
from backend.provenance import computed, live, seeded, unmeasured

LOG = logging.getLogger(__name__)

AGENT_STANDINGS = (mwp.Standing.TRUSTED.value, mwp.Standing.PENDING_REVIEW.value)
QUARANTINE = mwp.Standing.QUARANTINE.value
SIDES = ("memory_bank", "latest_notes")

MB_SIMPLE, MB_SEMANTIC = "mb_simple", "mb_semantic"
BQ_SIMPLE, BQ_VECTOR, BQ_RERANK, BQ_NOTES = "bq_simple", "bq_vector", "bq_vector_rerank", "bq_latest_notes"
SYSTEMS: Mapping[str, str] = {
    MB_SIMPLE: "Memory Bank simpleRetrievalParams + topic filter (retrieve_for_agents, no query)",
    MB_SEMANTIC: "Memory Bank similaritySearchParams (retrieve_for_agents with the case query)",
    BQ_SIMPLE: "BigQuery SQL on facts: customer + topic, newest first, LIMIT k",
    BQ_VECTOR: "BigQuery VECTOR_SEARCH on facts embeddings (cosine), top k",
    BQ_RERANK: "BigQuery VECTOR_SEARCH top pool, reranked by the Vertex AI ranking API, top k",
    BQ_NOTES: "BigQuery raw_notes: the customer's latest N turns (JPMC's 'latest notes' table)",
}
TIERS = (
    ("tier1_simple", "Tier 1: simple vs simple", MB_SIMPLE, BQ_SIMPLE),
    ("tier2_semantic", "Tier 2: semantic vs semantic", MB_SEMANTIC, BQ_RERANK),
    ("tier3_sidekick", "Tier 3: sidekick (MB semantic vs BQ simple)", MB_SEMANTIC, BQ_SIMPLE),
)
TIER_METRICS = ("recall@1", "recall@3", "recall@5", "recall@8", "mrr@8", "ndcg@5", "ndcg@8")


@dataclass(frozen=True)
class Item:
    key: str  # memory id, or "SESSION#turn" for a raw note
    customer_id: str
    standing: str
    text: str
    topics: tuple[str, ...] = ()


@dataclass
class Retrieved:
    system: str
    case_id: str
    customer_id: str
    items: list[Item] = field(default_factory=list)
    latency_ms: float | None = None
    error: str | None = None


class Glossary:
    """Glossary terms for a context, via the catalog governor (backend/knowledge_catalog.py:CatalogGovernor)."""

    def __init__(self, governor: Any, max_terms: int = 3):
        self.governor = governor
        self.max_terms = max_terms
        self.terms = {t["name"].rsplit("/", 1)[-1]: t.get("displayName", "") for t in governor.client.list_terms()}
        self._cache: dict[str, str] = {}

    def lines_for(self, *texts: str) -> list[str]:
        haystack = " ".join(texts).lower()
        hits = [tid for tid, name in self.terms.items()
                if (name and name.lower() in haystack) or tid.replace("-", " ") in haystack][:self.max_terms]
        return ["- %s: %s" % (self.terms[tid], self._definition(tid)) for tid in hits]

    def _definition(self, term_id: str) -> str:
        if term_id not in self._cache:
            self._cache[term_id] = str(self.governor.lookup_term(term_id).get("definition", NOT_RECORDED))
        return self._cache[term_id]


@dataclass
class Context:
    """What every stage needs. Built once per run in scripts/run_jpmc_eval.py."""
    run_id: str
    bank: EvalMemoryBank
    warehouse: Warehouse
    gemini: sc.Gemini
    fixtures: Fixtures
    k: int = 8
    notes_n: int = 8
    rerank_pool: int = 20
    workers: int = 8
    embedding_model: str | None = None  # BQ remote model name in the eval dataset
    ranker: Ranker | None = None
    glossary: Glossary | None = None
    fact_map: dict[str, frozenset[str]] = field(default_factory=dict)
    turn_map: dict[str, frozenset[str]] = field(default_factory=dict)

    def relevance(self, item: Item) -> frozenset[str]:
        return self.fact_map.get(item.key) or self.turn_map.get(item.key) or frozenset()


def topic_filter(topics: Sequence[str]) -> str | None:
    # Same filter syntax the channel agents send (backend/adk_agents.py:_topic_filter).
    return " OR ".join('topics.custom_memory_topic_label: "%s"' % t for t in topics) or None


def _timed[T](fn: Callable[[], T]) -> tuple[T, float]:
    started = time.perf_counter()
    out = fn()
    return out, round((time.perf_counter() - started) * 1000.0, 1)


def _share(part: int, whole: int, source: str, missing: str) -> dict[str, Any]:
    return computed(round(part / whole, 4), source) if whole else unmeasured(missing)


# -- items -------------------------------------------------------------------

def mb_items(rows: Iterable[Mapping[str, Any]]) -> list[Item]:
    return [Item(key=mwp.memory_id(row["memory"]["name"]),
                 customer_id=(row["memory"].get("scope") or {}).get("customer_id", NOT_RECORDED),
                 standing=row.get("standing", NOT_RECORDED), text=str(row["memory"].get("fact", NOT_RECORDED)),
                 topics=tuple(topic_labels(dict(row["memory"]))))
            for row in rows]


def fact_items(rows: Iterable[Mapping[str, Any]]) -> list[Item]:
    return [Item(key=row["memory_id"], customer_id=row["customer_id"], standing=row["standing"],
                 text=row["fact"], topics=tuple(row.get("topics") or ()))
            for row in rows]


def note_items(rows: Iterable[Mapping[str, Any]]) -> list[Item]:
    return [Item(key="%s#%d" % (row["session"], row["turn_index"]), customer_id=row["customer_id"], standing="raw",
                 text="[%s %s] %s: %s" % (_stamp(row["timestamp"]), row["channel"], row["author"], row["text"]))
            for row in rows]


def _stamp(value: Any) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if isinstance(value, datetime) else str(value)[:16]


# -- load --------------------------------------------------------------------

CARD_ENDING_RE = re.compile(r"\b(?:ending(?: in)?|ends in|last (?:four|4)(?: digits)?(?: are| is)?)\s+(\d{4})\b", re.I)


def session_causes(loads: Sequence[SessionLoad], bq_cards: Mapping[str, Mapping[str, str]]) -> list[dict[str, Any]]:
    """Why each non-trusted session landed where it did.

    `card_not_in_bigquery` flags a session whose customer names a card ending
    that BigQuery's cards table doesn't have for them (the known fixture/data
    mismatch). The validator's own discrepancy text is kept next to it.
    """
    out = []
    for load in loads:
        standing = load.lineage.get("standing", "error" if load.error else NOT_RECORDED)
        if standing == mwp.Standing.TRUSTED.value:
            continue
        said = " ".join(t.text for t in load.fixture.turns if t.author == CUSTOMER_ROLE)
        named = sorted(set(CARD_ENDING_RE.findall(said)))
        known = bq_cards.get(load.fixture.customer_id, {})
        missing = [n for n in named if n not in known]
        if load.error:
            cause = "load_error"
        elif load.lineage.get("check_error"):
            cause = "check_error"
        elif missing:
            cause = "card_not_in_bigquery"
        else:
            cause = "verdict:%s" % load.lineage.get("verdict", NOT_RECORDED)
        out.append({
            "session": load.fixture.session_id, "customer_id": load.fixture.customer_id, "standing": standing,
            "verdict": load.lineage.get("verdict", NOT_RECORDED), "cause": cause,
            "cards_named": named, "cards_in_bigquery": dict(known),
            "discrepancy": str(load.validator.get("discrepancy") or load.lineage.get("check_error")
                               or load.error or NOT_RECORDED)[:400],
        })
    return out


def report_load(loads: Sequence[SessionLoad], fact_rows: Sequence[Mapping[str, Any]],
                causes: Sequence[Mapping[str, Any]], gate: bool, seconds: float) -> dict[str, Any]:
    source = "memories:generate lineage (sessions.appendEvent stateDelta) on the eval engine"
    standings = Counter(load.lineage.get("standing", "error") for load in loads)
    verdicts = Counter(load.lineage.get("verdict", "error") for load in loads)
    kinds = Counter((row["standing"], row["kind"]) for row in fact_rows)
    generated = [len([g for g in load.lineage.get("generated", []) if g.get("action") != "DELETED"])
                 for load in loads]
    validator_ms = [load.validator["latency_ms"] for load in loads if "latency_ms" in load.validator]
    record_ms = [load.timings_ms["record"] for load in loads if "record" in load.timings_ms]
    cause_counts = Counter((c["standing"], c["cause"]) for c in causes)
    return {
        "gate": "on" if gate else "off",
        "sessions": seeded(len(loads), "memory/sessions/*.json"),
        "sessions_by_standing": {k: live(v, source) for k, v in sorted(standings.items())},
        "sessions_by_verdict": ({k: live(v, "ClaimVeracityValidatorAgent via verify_and_record")
                                 for k, v in sorted(verdicts.items())}
                                if gate else unmeasured("--no-gate: no veracity check ran")),
        "non_trusted_by_cause": [{"standing": s, "cause": c, "sessions": computed(n, "session_causes()")}
                                 for (s, c), n in sorted(cause_counts.items())],
        "non_trusted_sessions": computed(list(causes), "session_causes() over loader lineage + BigQuery cards"),
        "memories_by_standing_and_kind": [{"standing": s, "kind": k, "memories": live(n, "memories.list")}
                                          for (s, k), n in sorted(kinds.items())],
        "memories_generated": computed(sum(generated), source),
        "sessions_generating_nothing": computed(sum(1 for n in generated if n == 0), source),
        "load_errors": computed(sum(1 for load in loads if load.error), "loader.load_sessions"),
        "validator_latency_ms_p50": (live(sc.percentile(validator_ms, 0.5), "validator wall time")
                                     if validator_ms else unmeasured("no validator calls timed")),
        "record_latency_ms_p50": (live(sc.percentile(record_ms, 0.5), "verify_and_record wall time")
                                  if record_ms else unmeasured("no sessions recorded")),
        "wall_seconds": live(round(seconds, 1), "load stage wall time"),
        "sessions_detail": live([load.summary() for load in loads], "loader SessionLoad records"),
    }


# -- fact matching (the judge's answer key for everything downstream) ---------

def match_facts(ctx: Context, fact_rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """memory id -> the judge's FactMatch, for every stored memory of a fixture customer."""
    expected_by_customer = defaultdict(list)
    for row in ctx.fixtures.expected:
        expected_by_customer[row["customer_id"]].append(row)
    rows = [r for r in fact_rows if r["kind"] != "noise" and expected_by_customer.get(r["customer_id"])]

    def one(row: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        expected = expected_by_customer[row["customer_id"]]
        negatives = ctx.fixtures.negatives_for(row["customer_id"])
        prompt = sc.FACT_MATCH_PROMPT.format(
            fact=row["fact"],
            expected="\n".join("- %s [%s] (from %s) %s" % (e["memory_id"], e["topic"], e["valid_from"], e["fact"])
                               for e in expected),
            negatives="\n".join("- %s [%s] %s" % (n["negative_id"], n["category"], n["excerpt"])
                                for n in negatives) or "(none)")
        call = ctx.gemini.call("fact_match", row["memory_id"], prompt, schema=sc.FactMatch, inputs={
            "customer_id": row["customer_id"], "standing": row["standing"], "kind": row["kind"], "fact": row["fact"]})
        parsed = dict(call.parsed or {})
        parsed["matched_expected_ids"] = sorted(set(parsed.get("matched_expected_ids", []))
                                                & {e["memory_id"] for e in expected})
        parsed["matched_negative_ids"] = sorted(set(parsed.get("matched_negative_ids", []))
                                                & {n["negative_id"] for n in negatives})
        parsed["error"] = call.error
        return row["memory_id"], parsed

    return dict(parallel_map(one, rows, ctx.workers))


def fact_map_from(matches: Mapping[str, Mapping[str, Any]]) -> dict[str, frozenset[str]]:
    return {mid: frozenset(m["matched_expected_ids"]) for mid, m in matches.items() if m["matched_expected_ids"]}


def report_storage(fixtures: Fixtures, fact_rows: Sequence[Mapping[str, Any]],
                   matches: Mapping[str, Mapping[str, Any]], configured_topics: Iterable[str]) -> dict[str, Any]:
    """JPMC criterion 1: is what gets stored accurate, dated, tied to the right card?"""
    source = "judge fact_match over memories.list (judge.jsonl)"
    judged = [r for r in fact_rows if r["memory_id"] in matches and not matches[r["memory_id"]].get("error")]
    generated = [r for r in judged if r["kind"] in ("generated", "orphan")]
    if not generated:
        return {"status": unmeasured("no judged Memory Bank memories")}
    matched = [r for r in generated if matches[r["memory_id"]]["matched_expected_ids"]]

    def covered(rows: Iterable[Mapping[str, Any]]) -> set[str]:
        return {mid for r in rows for mid in matches[r["memory_id"]]["matched_expected_ids"]}

    def vote(name: str) -> dict[str, Any]:
        votes = Counter(matches[r["memory_id"]].get(name, "n/a") for r in matched)
        return {"share_yes": _share(votes["yes"], votes["yes"] + votes["no"], source,
                                    "no matched memory had a %s to check" % name),
                "yes": computed(votes["yes"], source), "no": computed(votes["no"], source)}

    total = len(fixtures.expected)
    configured = set(configured_topics)
    all_covered = covered(generated)
    by_topic: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
    for e in fixtures.expected:
        by_topic[e["topic"]][0] += e["memory_id"] in all_covered
        by_topic[e["topic"]][1] += 1
    negatives_kept = sorted({n for r in generated for n in matches[r["memory_id"]]["matched_negative_ids"]})
    return {
        "expected_facts": seeded(total, "memory/memories_expected.jsonl"),
        "memories_judged": computed(len(judged), source),
        "judge_errors": computed(sum(1 for m in matches.values() if m.get("error")), source),
        "generated_memories": computed(len(generated), source),
        "precision": _share(len(matched), len(generated), source, "no generated memories"),
        "coverage_any_scope": _share(len(all_covered), total, source, "no expected facts"),
        "coverage_agent_readable": _share(len(covered(r for r in generated if r["standing"] in AGENT_STANDINGS)),
                                          total, source, "no expected facts"),
        "coverage_trusted": _share(len(covered(r for r in generated if r["standing"] == "trusted")), total, source,
                                   "no expected facts"),
        "coverage_agent_readable_incl_claim_records": _share(
            len(covered(r for r in judged if r["standing"] in AGENT_STANDINGS)), total, source, "no expected facts"),
        "accurate": vote("accurate"),
        "dated": vote("dated"),
        "right_card": vote("right_card"),
        "negatives_stored": computed(len(negatives_kept), source),
        "negatives_stored_ids": computed(negatives_kept, source),
        "coverage_by_fixture_topic": [
            {"topic": t, "configured_on_engine": t in configured,
             "covered": computed(c, source), "expected": seeded(n, "memory/memories_expected.jsonl")}
            for t, (c, n) in sorted(by_topic.items())],
        "expected_under_topics_not_on_engine": seeded(
            sum(n for t, (_, n) in by_topic.items() if t not in configured), "memory/memories_expected.jsonl"),
    }


# -- retrieval tiers ---------------------------------------------------------

def route_topics(ctx: Context, cases: Sequence[Mapping[str, Any]],
                 vocabulary: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    """Tier 1's topic filter per case: GEMINI_MODEL picks from the engine's live topics, once, for both sides."""
    listing = "\n".join("- %s: %s" % item for item in vocabulary.items())

    def one(case: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
        call = ctx.gemini.call("router", case["case_id"], sc.ROUTER_PROMPT.format(topics=listing, question=case["query"]),
                               schema=sc.TopicRoute, inputs={"question": case["query"], "topics": list(vocabulary)})
        picked = [t for t in (call.parsed or {}).get("topics", []) if t in vocabulary]
        return case["case_id"], tuple(dict.fromkeys(picked))[:3]

    return dict(parallel_map(one, cases, ctx.workers))


def _attempt(system: str, case_id: str, customer_id: str, fn: Callable[[], tuple[list[Item], float]]) -> Retrieved:
    try:
        items, ms = fn()
        return Retrieved(system, case_id, customer_id, items, ms)
    except Exception as exc:  # noqa: BLE001 - one failed call is scored n/r, the run goes on
        LOG.warning("%s %s failed: %r", system, case_id, exc)
        return Retrieved(system, case_id, customer_id, error=repr(exc))


def _items(result: tuple[list[dict[str, Any]], float],
           build: Callable[[list[dict[str, Any]]], list[Item]]) -> tuple[list[Item], float]:
    rows, ms = result
    return build(rows), ms


def retrieve_case(ctx: Context, case_id: str, customer_id: str, query: str, topics: Sequence[str],
                  systems: Iterable[str]) -> list[Retrieved]:
    wanted = set(systems)
    out: list[Retrieved] = []

    def mb(query_text: str | None, memory_filter: str | None) -> tuple[list[Item], float]:
        rows, ms = _timed(lambda: mwp.retrieve_for_agents(ctx.bank, customer_id, query=query_text, top_k=ctx.k,
                                                          memory_filter=memory_filter))
        return mb_items(rows), ms

    if MB_SIMPLE in wanted:
        out.append(_attempt(MB_SIMPLE, case_id, customer_id, lambda: mb(None, topic_filter(topics))))
    if MB_SEMANTIC in wanted:
        out.append(_attempt(MB_SEMANTIC, case_id, customer_id, lambda: mb(query, None)))
    if BQ_SIMPLE in wanted:
        out.append(_attempt(BQ_SIMPLE, case_id, customer_id, lambda: _items(
            ctx.warehouse.simple_facts(ctx.run_id, customer_id, AGENT_STANDINGS, topics, ctx.k), fact_items)))
    if BQ_NOTES in wanted:
        out.append(_attempt(BQ_NOTES, case_id, customer_id, lambda: _items(
            ctx.warehouse.latest_notes(ctx.run_id, customer_id, ctx.notes_n), note_items)))
    if wanted & {BQ_VECTOR, BQ_RERANK} and ctx.embedding_model:
        pool = _attempt(BQ_VECTOR, case_id, customer_id, lambda: _items(ctx.warehouse.vector_search(
            ctx.run_id, customer_id, AGENT_STANDINGS, query, ctx.rerank_pool, ctx.embedding_model), fact_items))
        if BQ_VECTOR in wanted:
            out.append(Retrieved(BQ_VECTOR, case_id, customer_id, pool.items[:ctx.k], pool.latency_ms, pool.error))
        if BQ_RERANK in wanted and ctx.ranker is not None:
            out.append(_rerank(ctx, pool, query))
    return out


def _rerank(ctx: Context, pool: Retrieved, query: str) -> Retrieved:
    if pool.error or ctx.ranker is None:
        return Retrieved(BQ_RERANK, pool.case_id, pool.customer_id, error=pool.error or "no ranker")
    ranker = ctx.ranker
    by_key = {item.key: item for item in pool.items}

    def run() -> tuple[list[Item], float]:
        ranked, ms = ranker.rank(query, [(i.key, i.text) for i in pool.items], ctx.k)
        return [by_key[key] for key, _ in ranked if key in by_key], (pool.latency_ms or 0.0) + ms

    return _attempt(BQ_RERANK, pool.case_id, pool.customer_id, run)


def run_retrieval(ctx: Context, cases: Sequence[Mapping[str, Any]], routes: Mapping[str, Sequence[str]],
                  systems: Iterable[str], workers: int = 4) -> list[Retrieved]:
    systems = tuple(systems)
    batches = parallel_map(lambda c: retrieve_case(ctx, c["case_id"], c["customer_id"], c["query"],
                                                   routes.get(c["case_id"], ()), systems), cases, workers)
    return [r for batch in batches for r in batch]


def score_system(ctx: Context, results: Sequence[Retrieved],
                 cases_by_id: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    per_case = []
    for r in results:
        expected = frozenset(cases_by_id[r.case_id]["expected_memory_ids"])
        ranked = [ctx.relevance(i) for i in r.items]
        per_case.append({
            "case_id": r.case_id, "customer_id": r.customer_id, "error": r.error, "latency_ms": r.latency_ms,
            "expected": sorted(expected), "returned": [i.key for i in r.items],
            "relevant_ranks": [rank for rank, ids in enumerate(ranked, 1) if ids & expected],
            "metrics": None if r.error else sc.case_metrics(ranked, expected),
        })
    return per_case


def report_systems(ctx: Context, results: Sequence[Retrieved], cases: Sequence[Mapping[str, Any]],
                   not_run: Mapping[str, str]) -> dict[str, Any]:
    cases_by_id = {c["case_id"]: c for c in cases}
    grouped: defaultdict[str, list[Retrieved]] = defaultdict(list)
    for r in results:
        grouped[r.system].append(r)
    report = {}
    for system, description in SYSTEMS.items():
        if system not in grouped:
            report[system] = {"description": description,
                              "status": unmeasured(not_run.get(system, "system not run in this stage"))}
            continue
        per_case = score_system(ctx, grouped[system], cases_by_id)
        means = sc.mean_metrics(c["metrics"] for c in per_case if c["metrics"])
        latencies = [c["latency_ms"] for c in per_case if c["latency_ms"] is not None]
        errors = [c for c in per_case if c["error"]]
        source = "%s over %d retrieval cases; relevance from the judge's fact map" % (system, len(per_case))
        report[system] = {
            "description": description,
            "metrics": ({name: computed(value, source) for name, value in means.items()}
                        if means else unmeasured("every call failed or no case had expected memories")),
            "cases_scored": computed(sum(1 for c in per_case if c["metrics"]), source),
            "cases_without_expected_memories": seeded(sum(1 for c in per_case if not c["expected"]),
                                                      "memory/retrieval_cases.jsonl"),
            "errors": computed(len(errors), source),
            "error_sample": errors[0]["error"] if errors else None,
            "latency_ms_p50": _latency(latencies, 0.5, system),
            "latency_ms_p95": _latency(latencies, 0.95, system),
            "tokens": unmeasured("retrieval calls report no token counts"),
            "per_case": computed(per_case, source),
        }
    return report


def _latency(values: Sequence[float], q: float, what: str) -> dict[str, Any]:
    value = sc.percentile(values, q)
    return (live(value, "wall time around %s calls in this process" % what) if value is not None
            else unmeasured("no successful %s call" % what))


def _metric(system_report: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    metrics = system_report.get("metrics")
    return metrics.get(name) if isinstance(metrics, dict) and "_provenance" not in metrics else None


def report_tiers(systems: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for key, title, mb, baseline in TIERS:
        row: dict[str, Any] = {"tier": key, "title": title, "memory_bank": mb, "baseline": baseline}
        for name in TIER_METRICS:
            a, b = _metric(systems[mb], name), _metric(systems[baseline], name)
            row[name] = {
                "memory_bank": a or unmeasured("%s not measured" % mb),
                "baseline": b or unmeasured("%s not measured" % baseline),
                "lift": (computed(round(a["value"] - b["value"], 4), "%s minus %s" % (mb, baseline))
                         if a and b else unmeasured("one side not measured")),
            }
        rows.append(row)
    return rows


# -- end to end --------------------------------------------------------------

def mb_context(items: Sequence[Item], glossary_lines: Sequence[str]) -> str:
    block = "Stored memories:\n" + ("\n".join("- %s" % i.text for i in items) or "(none returned)")
    return block + ("\n\nBank glossary:\n" + "\n".join(glossary_lines) if glossary_lines else "")


def notes_context(items: Sequence[Item]) -> str:
    return "Most recent notes, oldest first:\n" + ("\n".join("- %s" % i.text for i in reversed(items)) or "(none)")


def _glossary_lines(ctx: Context, question: str, items: Sequence[Item]) -> list[str]:
    if ctx.glossary is None:
        return []
    try:
        return ctx.glossary.lines_for(question, *(i.text for i in items))
    except Exception as exc:  # noqa: BLE001 - a catalog outage leaves the context without glossary lines
        LOG.warning("glossary lookup failed: %r", exc)
        return []


def _contexts(ctx: Context, question: str, results: Mapping[str, Retrieved]) -> dict[str, dict[str, Any]]:
    """The two contexts every answer is generated from: MB workflow vs latest notes."""
    out = {}
    for side, system in (("memory_bank", MB_SEMANTIC), ("latest_notes", BQ_NOTES)):
        r = results.get(system)
        if r is None or r.error:
            out[side] = {"error": r.error if r else "%s not retrieved" % system}
        elif side == "memory_bank":
            lines = _glossary_lines(ctx, question, r.items)
            out[side] = {"context": mb_context(r.items, lines), "glossary_terms": len(lines),
                         "retrieval_ms": r.latency_ms, "customer_id": r.customer_id}
        else:
            out[side] = {"context": notes_context(r.items), "glossary_terms": 0, "retrieval_ms": r.latency_ms,
                         "customer_id": r.customer_id}
    return out


def run_end_to_end(ctx: Context, cases: Sequence[Mapping[str, Any]],
                   retrieved: Sequence[Retrieved]) -> list[dict[str, Any]]:
    """Answer each case from the MB workflow context and from latest notes; judge both the same way."""
    by_case: defaultdict[str, dict[str, Retrieved]] = defaultdict(dict)
    for r in retrieved:
        by_case[r.case_id][r.system] = r
    expected_by_id = ctx.fixtures.expected_by_id()
    today = ctx.fixtures.as_of().date().isoformat()
    jobs = [(case, side, job) for case in cases
            for side, job in _contexts(ctx, case["query"], by_case[case["case_id"]]).items()]

    def one(job: tuple[Mapping[str, Any], str, dict[str, Any]]) -> dict[str, Any]:
        case, side, prepared = job
        tag = "%s:%s" % (case["case_id"], side)
        if "error" in prepared:
            return {"case_id": case["case_id"], "side": side, "error": prepared["error"]}
        answer = ctx.gemini.call("answer", tag, sc.ANSWER_PROMPT.format(
            today=today, customer_id=case["customer_id"], question=case["query"], context=prepared["context"]),
            inputs={"side": side, "question": case["query"], "context": prepared["context"]})
        expected = [expected_by_id[m]["fact"] for m in case["expected_memory_ids"]]
        distractors = [expected_by_id[m]["fact"] for m in case["distractor_ids"] if m in expected_by_id]
        judged = ctx.gemini.call("answer_judge", tag, sc.ANSWER_JUDGE_PROMPT.format(
            today=today, question=case["query"], answer=answer.text or "(no answer)",
            expected="\n".join("- %s" % f for f in expected)
            or "None. Nothing on record answers this; the correct answer says so.",
            distractors="\n".join("- %s" % f for f in distractors) or "(none)"),
            schema=sc.AnswerJudgment, inputs={"side": side, "question": case["query"], "answer": answer.text,
                                              "expected": expected, "distractors": distractors})
        verdict = (judged.parsed or {}).get("verdict")
        return {"case_id": case["case_id"], "side": side, "answer": answer.text, "verdict": verdict,
                "score": sc.ANSWER_SCORE.get(verdict or ""), "judge": judged.parsed,
                "expects_nothing": not case["expected_memory_ids"], "glossary_terms": prepared["glossary_terms"],
                "latency_ms": {"retrieval": prepared["retrieval_ms"], "answer": answer.latency_ms},
                "tokens": answer.tokens, "error": answer.error or judged.error}

    return parallel_map(one, jobs, ctx.workers)


def report_end_to_end(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for side, label in zip(SIDES, ("retrieve_for_agents + catalog glossary",
                                   "latest raw_notes rows, no Memory Bank, no catalog")):
        mine = [r for r in rows if r["side"] == side]
        ok = [r for r in mine if r.get("score") is not None]
        source = "judge answer_judge over %d cases (judge.jsonl)" % len(mine)
        verdicts = Counter(r.get("verdict") or "error" for r in mine)
        tokens = [r["tokens"]["total"] for r in ok if (r.get("tokens") or {}).get("total") is not None]
        out[side] = {
            "context": label,
            "answer_score": _share(sum(r["score"] for r in ok), len(ok), source, "no judged answers")
            if ok else unmeasured("no judged answers"),
            "verdicts": {k: computed(v, source) for k, v in sorted(verdicts.items())},
            "used_superseded_fact": computed(sum(1 for r in ok if (r["judge"] or {}).get("used_superseded_fact")),
                                             source),
            "unsupported_claim": computed(sum(1 for r in ok if (r["judge"] or {}).get("unsupported_claim")), source),
            "expect_nothing_correct": computed(
                sum(1 for r in ok if r["expects_nothing"] and r["verdict"] == "CORRECT"), source),
            "answer_tokens_mean": (live(round(statistics.fmean(tokens), 1), "genai usage_metadata.total_token_count")
                                   if tokens else unmeasured("usage_metadata absent")),
            "answer_latency_ms_p50": _latency([r["latency_ms"]["answer"] for r in ok], 0.5, "answer generation"),
        }
    out["cases"] = computed(list(rows), "answers + answer_judge (judge.jsonl)")
    return out


# -- change handling ---------------------------------------------------------

def report_change(ctx: Context, fact_rows: Sequence[Mapping[str, Any]],
                  e2e: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """JPMC criterion 3: after a change, did memory update, keep both, or get confused?"""
    standing_of = {r["memory_id"]: r["standing"] for r in fact_rows}
    readable = {mid: ids for mid, ids in ctx.fact_map.items() if standing_of.get(mid) in AGENT_STANDINGS}
    answers = {(r["case_id"], r["side"]): r for r in e2e}
    pairs = []
    for pair in ctx.fixtures.contradictions:
        old, new = pair["original"]["memory_id"], pair["winner_memory_id"]
        says_new = {mid for mid, ids in readable.items() if new in ids}
        says_old = {mid for mid, ids in readable.items() if old in ids}
        if not says_new and not says_old:
            state = "not_stored"
        elif not says_new:
            state = "stale_only"
        elif says_old <= says_new:
            state = "updated"
        else:
            state = "kept_both"
        linked = next((c["case_id"] for c in ctx.fixtures.retrieval_cases
                       if new in c["expected_memory_ids"] and c["customer_id"] == pair["customer_id"]), None)
        pairs.append({
            "contradiction_id": pair["contradiction_id"], "customer_id": pair["customer_id"], "topic": pair["topic"],
            "state": state, "new_fact_memories": sorted(says_new), "old_fact_memories": sorted(says_old),
            "scopes": sorted({standing_of[m] for m in says_new | says_old}), "linked_case": linked,
            "answer_used_stale_fact": {side: (answers.get((linked, side), {}).get("judge") or {}).get(
                "used_superseded_fact") for side in SIDES},
        })
    source = "fact map (judge) over agent-readable memories, per memory/contradictions.jsonl"
    states = Counter(p["state"] for p in pairs)
    stale = {side: [p["answer_used_stale_fact"][side] for p in pairs if p["answer_used_stale_fact"][side] is not None]
             for side in SIDES}
    return {
        "pairs": seeded(len(pairs), "memory/contradictions.jsonl"),
        "memory_states": {s: computed(states.get(s, 0), source)
                          for s in ("updated", "kept_both", "stale_only", "not_stored")},
        "answers_using_stale_fact": {side: (computed(sum(v), "answer_judge on the linked retrieval case")
                                            if v else unmeasured("no linked answers judged"))
                                     for side, v in stale.items()},
        "answers_judged": {side: computed(len(v), "answer_judge on the linked retrieval case")
                           for side, v in stale.items()},
        "detail": computed(pairs, source),
    }


# -- isolation ---------------------------------------------------------------

def run_isolation(ctx: Context, fact_rows: Sequence[Mapping[str, Any]], seen: Sequence[Retrieved],
                  probe_limit: int = 40) -> dict[str, Any]:
    """Quarantine never reaches agents; no result ever belongs to another customer."""
    quarantined = [r for r in fact_rows if r["standing"] == QUARANTINE and r["kind"] != "noise"]
    quarantine_ids = {r["memory_id"] for r in quarantined}

    def probe(row: Mapping[str, Any]) -> Retrieved:
        return _attempt("quarantine_probe", row["memory_id"], row["customer_id"], lambda: _timed(
            lambda: mb_items(mwp.retrieve_for_agents(ctx.bank, row["customer_id"], query=row["fact"], top_k=ctx.k))))

    probes = parallel_map(probe, quarantined[:probe_limit], ctx.workers)
    leak_checks = []
    for pair in ctx.fixtures.leakage_pairs:
        victim, owner = pair["must_not_surface_for_customer_id"], pair["owner_customer_id"]
        for r in retrieve_case(ctx, pair["leakage_id"], victim, pair["tempting_query"], (),
                               (MB_SEMANTIC, BQ_SIMPLE, BQ_VECTOR)):
            leak_checks.append({"leakage_id": pair["leakage_id"], "system": r.system, "error": r.error,
                                "owner_items": [i.key for i in r.items if i.customer_id == owner],
                                "foreign_items": [i.key for i in r.items if i.customer_id != victim]})

    everything = [*seen, *probes]
    cross = [(r.system, r.case_id, i.key) for r in everything for i in r.items if i.customer_id != r.customer_id]
    returned = [(r.system, r.case_id, i.key) for r in everything for i in r.items
                if i.key in quarantine_ids or i.standing == QUARANTINE]
    source = "retrieve_for_agents / BigQuery results checked against memory scopes"
    return {
        "quarantined_memories": computed(len(quarantine_ids), "memories.list, scope corpus=quarantine"),
        "quarantine_probes": computed(len(probes), "retrieve_for_agents with each quarantined fact as the query"),
        "quarantine_probe_errors": computed(sum(1 for p in probes if p.error), source),
        "quarantine_returned_to_agents": computed(len(returned), source),
        "quarantine_violations": computed(returned, source),
        "result_lists_checked": computed(len(everything) + len(leak_checks), source),
        "cross_customer_results": computed(len(cross) + sum(len(c["foreign_items"]) for c in leak_checks), source),
        "cross_customer_detail": computed(cross, source),
        "leakage_pairs": seeded(len(ctx.fixtures.leakage_pairs), "memory/leakage_pairs.jsonl"),
        "leakage_probe_results": computed(leak_checks, source),
        "leaks_found": computed(sum(len(c["owner_items"]) for c in leak_checks), source),
    }


# -- control -----------------------------------------------------------------

def run_control(ctx: Context, routes: Mapping[str, Sequence[str]], fact_rows: Sequence[Mapping[str, Any]],
                results: Sequence[Retrieved]) -> dict[str, Any]:
    """JPMC criterion 5 on a throwaway scope: steer, inspect, correct, delete. Plus steer/inspect over the run."""
    scope = {"customer_id": "EVALCONTROL-%s" % ctx.run_id}
    first, second = "I asked for card alerts by SMS only.", "I asked for card alerts by email only."
    held: dict[str, str] = {}
    steps: list[dict[str, Any]] = []

    def create() -> bool:
        held["name"] = ctx.bank.create_memory_with_topics(first, scope, ["CONTACT_PREFERENCE"])
        return bool(held["name"])

    def filtered() -> bool:
        rows = ctx.bank.retrieve(scope, top_k=ctx.k, memory_filter=topic_filter(["CONTACT_PREFERENCE"]))
        return any(i.key == mwp.memory_id(held["name"]) for i in mb_items(rows))

    def correct() -> bool:
        ctx.bank.wait_for_operation(ctx.bank.update_memory(held["name"], second))
        return ctx.bank.get_memory(held["name"]).get("fact") == second

    def delete() -> bool:
        ctx.bank.wait_for_operation(ctx.bank.delete_memory(held["name"]))
        return not ctx.bank.retrieve(scope, top_k=ctx.k)

    for name, fn in (("steer: create with a topic label", create),
                     ("inspect: get returns the fact", lambda: ctx.bank.get_memory(held["name"]).get("fact") == first),
                     ("steer: topic filter returns it", filtered),
                     ("correct: update_memory", correct),
                     ("inspect: revisions keep the old text", lambda: len(ctx.bank.list_revisions(held["name"])) >= 2),
                     ("delete: gone from retrieval", delete)):
        try:
            ok, ms = _timed(fn)
            steps.append({"step": name, "passed": ok, "latency_ms": ms})
        except Exception as exc:  # noqa: BLE001 - a failed step is recorded, not raised
            steps.append({"step": name, "passed": False, "error": repr(exc)})

    requested = {r.case_id: set(routes.get(r.case_id, ())) for r in results if r.system == MB_SIMPLE}
    topic_hits = [bool(set(i.topics) & requested[r.case_id]) for r in results
                  if r.system == MB_SIMPLE and requested[r.case_id] for i in r.items]
    generated = [r for r in fact_rows if r["kind"] in ("generated", "orphan")]
    probe_source = "control probe on scope %s" % scope["customer_id"]
    return {
        "probe_steps": computed(steps, probe_source),
        "probe_passed": computed(sum(1 for s in steps if s["passed"]), probe_source),
        "probe_total": computed(len(steps), probe_source),
        "topic_filter_honoured": _share(sum(topic_hits), len(topic_hits),
                                        "mb_simple results whose topics intersect the requested topics",
                                        "no topic-filtered results"),
        "lineage_coverage": _share(sum(1 for r in generated if r["kind"] == "generated"), len(generated),
                                   "generated memories whose source session lineage names", "no generated memories"),
    }


# -- scenarios ---------------------------------------------------------------

def run_scenarios(ctx: Context, scenarios: Sequence[Scenario],
                  loads: Mapping[str, Sequence[SessionLoad]]) -> list[dict[str, Any]]:
    today = ctx.fixtures.as_of().date().isoformat()
    customers = {s.customer_id(ctx.run_id): s for s in scenarios}
    stored: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for memory in ctx.bank.iter_memories():
        if (customer := (memory.get("scope") or {}).get("customer_id")) in customers:
            stored[customer].append(memory)
    jobs = []
    for customer, scenario in customers.items():
        results = {r.system: r for r in retrieve_case(ctx, scenario.key, customer, scenario.question, (),
                                                      (MB_SEMANTIC, BQ_NOTES))}
        for side, prepared in _contexts(ctx, scenario.question, results).items():
            jobs.append((scenario, side, prepared, stored[customer]))

    def one(job: tuple[Scenario, str, dict[str, Any], list[dict[str, Any]]]) -> dict[str, Any]:
        scenario, side, prepared, memories = job
        tag = "%s:%s" % (scenario.key, side)
        base = {"scenario": scenario.key, "title": scenario.title, "side": side,
                "sessions": [{"session": l.fixture.session_id, "standing": l.lineage.get("standing", NOT_RECORDED),
                              "verdict": l.lineage.get("verdict", NOT_RECORDED)} for l in loads.get(scenario.key, [])],
                "stored": [{"standing": (m.get("scope") or {}).get("corpus", "trusted"), "fact": m.get("fact")}
                           for m in memories]}
        if "error" in prepared:
            return base | {"error": prepared["error"]}
        reply = ctx.gemini.call("scenario_answer", tag, sc.SCENARIO_ANSWER_PROMPT.format(
            customer_id=prepared["customer_id"], today=today, context=prepared["context"], message=scenario.question),
            inputs={"side": side, "context": prepared["context"], "message": scenario.question})
        judged = ctx.gemini.call("scenario_judge", tag, sc.SCENARIO_JUDGE_PROMPT.format(
            title=scenario.title, transcript=scenario.transcript(), message=scenario.question,
            expectation=scenario.expectation, reply=reply.text or "(no reply)"), schema=sc.ScenarioJudgment,
            inputs={"side": side, "reply": reply.text, "expectation": scenario.expectation})
        verdict = (judged.parsed or {}).get("verdict")
        return base | {"context": prepared["context"], "reply": reply.text, "verdict": verdict,
                       "score": sc.SCENARIO_SCORE.get(verdict or ""),
                       "rationale": (judged.parsed or {}).get("rationale"), "tokens": reply.tokens,
                       "latency_ms": {"retrieval": prepared["retrieval_ms"], "answer": reply.latency_ms},
                       "error": reply.error or judged.error}

    return parallel_map(one, jobs, ctx.workers)


def report_scenarios(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    source = "judge scenario_judge (judge.jsonl)"
    table = []
    for key in dict.fromkeys(r["scenario"] for r in rows):
        mine = {r["side"]: r for r in rows if r["scenario"] == key}
        entry: dict[str, Any] = {"scenario": key, "title": next(iter(mine.values()))["title"]}
        for side in SIDES:
            r = mine.get(side, {})
            entry[side] = ({"verdict": computed(r["verdict"], source), "score": computed(r["score"], source)}
                           if r.get("score") is not None else {"verdict": unmeasured(r.get("error") or "not judged")})
        entry["memory_bank_sessions"] = computed(next(iter(mine.values()))["sessions"], "loader lineage")
        table.append(entry)
    scores = {side: [r["score"] for r in rows if r["side"] == side and r.get("score") is not None] for side in SIDES}
    return {"table": table,
            "passed": {side: computed(sum(1 for s in v if s == 1.0), source) for side, v in scores.items()},
            "judged": {side: computed(len(v), source) for side, v in scores.items()},
            "score": {side: _share(sum(v), len(v), source, "no judged replies") for side, v in scores.items()},
            "detail": computed(list(rows), source)}


# -- volume ------------------------------------------------------------------

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
NOISE_TEMPLATES: Sequence[tuple[str, str]] = (
    ("CONTACT_PREFERENCE", "In {month} {year} I changed my mailing address from {n1} {street} to {n2} {street2}."),
    ("CONTACT_PREFERENCE", "In {month} {year} I switched from paper statements to e-statements."),
    ("DISPUTE_HISTORY", "In {month} {year} I disputed a {amount} USD charge from {merchant}; it was resolved in my favour."),
    ("DISPUTE_HISTORY", "In {month} {year} I disputed a {amount} USD charge from {merchant} as a duplicate, then withdrew it."),
    ("TRAVEL_STATUS", "I told the bank I would be in {city} from {day} to {day2} {month} {year}."),
    ("CARD_LIFECYCLE", "In {month} {year} my card was reissued because the old one expired."),
    ("SPENDING_CONTEXT", "In {month} {year} I made a one-off {amount} USD purchase at {merchant} and asked you not to flag it."),
    ("DEVICE_WALLET_INVENTORY", "In {month} {year} I removed an old {device} from my digital wallet."),
    ("AGENT_COMMITMENTS", "In {month} {year} the bank promised to post me a statement copy, and it arrived the next week."),
)
_SLOTS = {
    "street": ("Birch Lane", "Harbor View", "Maple Court", "Station Road", "Elm Street", "Cedar Avenue"),
    "merchant": ("Quikstop Fuel", "Northside Books", "Bluebird Cafe", "Metro Transit", "Pixel Games", "Harvest Market"),
    "city": ("Denver", "Montreal", "Madrid", "Osaka", "Cape Town", "Dublin", "Reykjavik"),
    "device": ("phone", "tablet", "smartwatch"),
}


def noise_notes(customer_ids: Sequence[str], per_customer: int, seed: int = 7) -> list[dict[str, Any]]:
    """Old, unrelated notes ('past address changes and disputes'), dated 2018-2023. Deterministic per seed."""
    rng = random.Random(seed)
    notes = []
    for customer in customer_ids:
        for _ in range(per_customer):
            topic, template = rng.choice(NOISE_TEMPLATES)
            year, month, day = rng.randint(2018, 2023), rng.randint(1, 12), rng.randint(1, 20)
            text = template.format(month=MONTHS[month - 1], year=year, day=day, day2=day + rng.randint(2, 8),
                                   n1=rng.randint(2, 999), n2=rng.randint(2, 999),
                                   street=rng.choice(_SLOTS["street"]), street2=rng.choice(_SLOTS["street"]),
                                   amount="%.2f" % rng.uniform(5, 900), merchant=rng.choice(_SLOTS["merchant"]),
                                   city=rng.choice(_SLOTS["city"]), device=rng.choice(_SLOTS["device"]))
            notes.append({"customer_id": customer, "topic": topic, "fact": text,
                          "said_on": date(year, month, day).isoformat()})
    return notes


def add_noise(ctx: Context, notes: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], float | None]:
    """Writes noise to Memory Bank (trusted scope, with topics) and the same rows to both BigQuery tables."""
    def create(note: Mapping[str, Any]) -> tuple[str, float]:
        return _timed(lambda: ctx.bank.create_memory_with_topics(
            note["fact"], mwp.scope_for(note["customer_id"], mwp.Standing.TRUSTED), [note["topic"]]))

    created = parallel_map(create, notes, 16)
    now = datetime.now(UTC).isoformat()
    rows = [{"run_id": ctx.run_id, "memory_name": name, "memory_id": mwp.memory_id(name),
             "customer_id": note["customer_id"], "standing": mwp.Standing.TRUSTED.value, "kind": "noise",
             "fact": note["fact"], "topics": [note["topic"]], "source_session": None, "source_fixture_session": None,
             "source_time": note["said_on"] + "T12:00:00+00:00", "create_time": now, "update_time": now}
            for note, (name, _) in zip(notes, created) if name]
    ctx.warehouse.insert(FACTS, rows)
    ctx.warehouse.insert(RAW_NOTES, [
        {"run_id": ctx.run_id, "customer_id": row["customer_id"], "session": "NOISE-%s" % row["memory_id"],
         "vertex_session": None, "channel": "NOTE", "turn_index": 0, "author": "customer", "text": row["fact"],
         "timestamp": row["source_time"], "kind": "noise"} for row in rows])
    timings = [ms for _, ms in created]
    return rows, (round(statistics.fmean(timings), 1) if timings else None)


def report_volume(before: Mapping[str, Mapping[str, Any]], after: Mapping[str, Mapping[str, Any]],
                  noise_rows: int, per_customer: int, create_ms: float | None) -> dict[str, Any]:
    """JPMC criterion 4: with hundreds of unrelated notes, do the relevant ones still come back, as fast?"""
    rows = []
    for system in SYSTEMS:
        entry: dict[str, Any] = {"system": system}
        for label, report in (("before", before.get(system, {})), ("after", after.get(system, {}))):
            entry[label] = ({"recall@5": _metric(report, "recall@5"), "recall@8": _metric(report, "recall@8"),
                             "mrr@8": _metric(report, "mrr@8"),
                             "latency_ms_p50": report["latency_ms_p50"], "latency_ms_p95": report["latency_ms_p95"]}
                            if _metric(report, "recall@5") else unmeasured("%s not measured %s noise" % (system, label)))
        rows.append(entry)
    return {"noise_memories_added": computed(noise_rows, "memories.create (trusted scope, with topics)"),
            "noise_per_customer": computed(per_customer, "--noise-per-customer"),
            "noise_create_latency_ms_mean": (live(create_ms, "memories.create wall time") if create_ms is not None
                                             else unmeasured("no noise created")),
            "by_system": rows,
            "note": "Noise is dated 2018-2023 in BigQuery (source_time), but Memory Bank stamps it with the load "
                    "time because a memory's create time can't be set. BigQuery 'newest first' sorts noise to the "
                    "bottom; Memory Bank sees it as the newest memories. raw_notes noise is old too, so the "
                    "latest-notes baseline never sees it."}


# -- the JPMC criteria table --------------------------------------------------

def criteria_table(results: Mapping[str, Any]) -> list[dict[str, Any]]:
    storage, e2e = results.get("storage_accuracy", {}), results.get("end_to_end", {})
    change, volume = results.get("change_handling", {}), results.get("volume", {})
    iso, control, scen = results.get("isolation", {}), results.get("control", {}), results.get("scenarios", {})
    tiers = {t["tier"]: t for t in results.get("tiers", [])}

    def pick(section: Mapping[str, Any], *path: str) -> Any:
        node: Any = section
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        return node if node is not None else unmeasured("%s not run" % ".".join(path))

    def by_prefix(prefix: str) -> Any:
        rows = {r["system"]: {"before": r["before"], "after": r["after"]}
                for r in volume.get("by_system", []) if r["system"].startswith(prefix)}
        return rows or unmeasured("volume stage not run")

    return [
        {"criterion": "Accuracy of what gets stored",
         "question": "Is what gets stored accurate, dated and tied to the right card?",
         "memory_bank": {"coverage_agent_readable": pick(storage, "coverage_agent_readable"),
                         "precision": pick(storage, "precision"),
                         "accurate": pick(storage, "accurate", "share_yes"), "dated": pick(storage, "dated", "share_yes"),
                         "right_card": pick(storage, "right_card", "share_yes"),
                         "negatives_stored": pick(storage, "negatives_stored")},
         "baseline": unmeasured("the notes table keeps raw turns verbatim; there is no extraction to grade")},
        {"criterion": "Value over a simpler approach",
         "question": "Same cases, notes in an ordinary table, the agent gets the most recent entries.",
         "memory_bank": {"answer_score": pick(e2e, "memory_bank", "answer_score"),
                         "tier1_recall@5": pick(tiers, "tier1_simple", "recall@5", "memory_bank"),
                         "tier2_recall@5": pick(tiers, "tier2_semantic", "recall@5", "memory_bank")},
         "baseline": {"answer_score": pick(e2e, "latest_notes", "answer_score"),
                      "tier1_recall@5": pick(tiers, "tier1_simple", "recall@5", "baseline"),
                      "tier2_recall@5": pick(tiers, "tier2_semantic", "recall@5", "baseline")}},
        {"criterion": "Handling of change",
         "question": "After a change (an unlock an hour later), does memory update, keep both, or get confused?",
         "memory_bank": {"memory_states": pick(change, "memory_states"),
                         "answers_using_stale_fact": pick(change, "answers_using_stale_fact", "memory_bank"),
                         "answers_judged": pick(change, "answers_judged", "memory_bank")},
         "baseline": {"answers_using_stale_fact": pick(change, "answers_using_stale_fact", "latest_notes"),
                      "answers_judged": pick(change, "answers_judged", "latest_notes")}},
        {"criterion": "Behaviour at volume",
         "question": "With hundreds of unrelated notes, do the relevant ones still come back, as fast?",
         "memory_bank": by_prefix("mb_"), "baseline": by_prefix("bq_")},
        {"criterion": "Control",
         "question": "Can we steer, inspect, correct, delete, and keep customers separate?",
         "memory_bank": {"probe_passed": pick(control, "probe_passed"), "probe_total": pick(control, "probe_total"),
                         "topic_filter_honoured": pick(control, "topic_filter_honoured"),
                         "lineage_coverage": pick(control, "lineage_coverage"),
                         "quarantine_returned_to_agents": pick(iso, "quarantine_returned_to_agents"),
                         "cross_customer_results": pick(iso, "cross_customer_results"),
                         "leaks_found": pick(iso, "leaks_found")},
         "baseline": unmeasured("SQL tables have their own controls; not compared here")},
        {"criterion": "7 JPMC scenarios",
         "question": "Recall, state change, two cards, noise, false refund, fake promise, VIP escalation.",
         "memory_bank": {"passed": pick(scen, "passed", "memory_bank"), "judged": pick(scen, "judged", "memory_bank"),
                         "score": pick(scen, "score", "memory_bank")},
         "baseline": {"passed": pick(scen, "passed", "latest_notes"), "judged": pick(scen, "judged", "latest_notes"),
                      "score": pick(scen, "score", "latest_notes")}},
    ]

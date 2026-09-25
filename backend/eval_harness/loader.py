"""Fixture sessions -> Vertex sessions on the eval engine -> verified write path.

Phase A runs in parallel: create every session, append its turns with their
real timestamps, and (gated loads) run the veracity check on the customer's
words. Phase B runs per customer, oldest session first, through
`memory_write_path.verify_and_record`, because consolidation in the trusted
scope depends on the order sessions arrive. Customers run in parallel.

`--no-gate` skips the check and generates straight into the trusted scope: the
raw Memory Bank baseline.
"""

import contextvars
import itertools
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from backend import memory_write_path as mwp
from backend.eval_harness.env import EvalMemoryBank, parallel_map
from backend.eval_harness.fixtures import FixtureSession
from backend.memory_bank import NOT_RECORDED, MemoryBankClient, MemoryBankError, generated_memories, topic_labels

LOG = logging.getLogger(__name__)

# (customer_id, channel label, the customer's own words) -> validator outcome.
Validate = Callable[[str, str, str], dict[str, Any]]
NameLookup = Callable[[str], str]
Progress = Callable[[str], None]


@dataclass
class SessionLoad:
    fixture: FixtureSession
    session_name: str = NOT_RECORDED
    lineage: dict[str, Any] = field(default_factory=dict)
    validator: dict[str, Any] = field(default_factory=dict)
    timings_ms: dict[str, float] = field(default_factory=dict)
    error: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "fixture_session_id": self.fixture.session_id,
            "customer_id": self.fixture.customer_id,
            "channel": self.fixture.channel,
            "started_at": self.fixture.started_at.isoformat(),
            "session_name": self.session_name,
            "standing": self.lineage.get("standing", NOT_RECORDED),
            "verdict": self.lineage.get("verdict", NOT_RECORDED),
            "check_error": self.lineage.get("check_error"),
            "validator": self.validator,
            "generated": self.lineage.get("generated", []),
            "claim_record": (self.lineage.get("claim_record") or {}).get("name"),
            "timings_ms": self.timings_ms,
            "error": self.error,
        }


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 1)


def real_validator(customer_id: str, channel: str, said: str) -> dict[str, Any]:
    """The production veracity check (backend/veracity_and_audit.py:ClaimVeracityValidatorAgent).

    Same call adk_agents._claim_verifier makes, but it keeps the discrepancy
    text so the report can say why a session was quarantined, not just that it was.
    """
    from backend import adk_agents  # heavy import (ADK, BigQuery); only gated loads need it
    from backend.veracity_and_audit import ClaimVeracityValidatorAgent

    evaluation = ClaimVeracityValidatorAgent().validate_claim_against_telemetry(
        customer_id=customer_id, channel=adk_agents._bank_channel_for(channel), claim_text=said)
    return {
        "status": evaluation.veracity_status.value,
        "confidence": evaluation.confidence_score,
        "discrepancy": evaluation.discrepancy_details or NOT_RECORDED,
        "validator": evaluation.validator_agent,
    }


def real_name_lookup(customer_id: str) -> str:
    """Golden 01 name lookup the write path puts on claim records."""
    from backend import adk_agents

    return adk_agents.customer_display_name(customer_id)


def turns_for_write_path(fixture: FixtureSession) -> list[dict[str, str]]:
    return [{"author": turn.author, "text": turn.text} for turn in fixture.turns]


def create_and_append(bank: MemoryBankClient, fixture: FixtureSession, load_id: str) -> str:
    name = bank.create_session(
        user_id=fixture.customer_id,
        display_name="%s · %s · %s" % (fixture.channel, fixture.intent, fixture.session_id),
        session_state={"channel": fixture.channel, "customer_id": fixture.customer_id,
                       "intent": fixture.intent, "fixture_session_id": fixture.session_id,
                       "eval_load_id": load_id},
    )
    for index, turn in enumerate(fixture.turns):
        bank.append_event(name, turn.author, turn.text, timestamp=turn.timestamp,
                          invocation_id="%s-%03d" % (fixture.session_id, index))
    return name


def record_without_gate(bank: MemoryBankClient, session_name: str, customer_id: str) -> dict[str, Any]:
    """Raw Memory Bank: generate into the trusted scope with no veracity check."""
    scope = mwp.scope_for(customer_id, mwp.Standing.TRUSTED)
    operation = bank.generate_from_session(session_name, scope, wait=True)
    lineage = {
        "scope": scope,
        "standing": mwp.Standing.TRUSTED.value,
        "verdict": NOT_RECORDED,
        "gate": "off",
        "operation": operation.get("name", NOT_RECORDED),
        "generated": generated_memories(operation),
    }
    bank.append_state_delta(session_name, mwp.LINEAGE_AUTHOR, {mwp.LINEAGE_STATE_KEY: lineage})
    return lineage


def record_session(bank: MemoryBankClient, load: SessionLoad, *, gate: bool,
                   verdict: Future | None, name_lookup: NameLookup | None) -> None:
    """Phase B for one session. A failure is kept on the load, never raised."""
    fixture = load.fixture
    started = time.perf_counter()
    try:
        if gate:
            turns = turns_for_write_path(fixture)
            said = mwp.customer_text(turns)

            def verify(text: str) -> str:
                if verdict is None or text != said:
                    raise ValueError("write path asked about words the pre-check did not see")
                return verdict.result()["status"]

            load.lineage = mwp.verify_and_record(bank, load.session_name, fixture.customer_id, turns,
                                                 fixture.channel, verify, name_lookup=name_lookup)
        else:
            load.lineage = record_without_gate(bank, load.session_name, fixture.customer_id)
    except Exception as exc:  # noqa: BLE001 - one bad session must not stop the load
        load.error = repr(exc)
        LOG.exception("recording %s failed", fixture.session_id)
    load.timings_ms["record"] = elapsed_ms(started)
    if verdict is not None:
        try:
            load.validator = verdict.result()
        except Exception as exc:  # noqa: BLE001 - the write path already filed it as check_error
            load.validator = {"error": repr(exc)}


def _validate_timed(validate: Validate, fixture: FixtureSession) -> dict[str, Any]:
    started = time.perf_counter()
    outcome = validate(fixture.customer_id, fixture.channel, mwp.customer_text(turns_for_write_path(fixture)))
    return {**outcome, "latency_ms": elapsed_ms(started)}


def _timed_create(bank: MemoryBankClient, load: SessionLoad, load_id: str) -> None:
    started = time.perf_counter()
    load.session_name = create_and_append(bank, load.fixture, load_id)
    load.timings_ms["create_and_append"] = elapsed_ms(started)


def load_sessions(
    bank: MemoryBankClient,
    sessions: Sequence[FixtureSession],
    *,
    load_id: str,
    gate: bool,
    validate: Validate = real_validator,
    name_lookup: NameLookup | None = None,
    workers: int = 8,
    progress: Progress = LOG.info,
) -> list[SessionLoad]:
    loads = [SessionLoad(fixture) for fixture in sessions]
    by_customer: defaultdict[str, list[SessionLoad]] = defaultdict(list)
    for load in sorted(loads, key=lambda item: (item.fixture.started_at, item.fixture.session_id)):
        by_customer[load.fixture.customer_id].append(load)
    finished = itertools.count(1)

    with ThreadPoolExecutor(max_workers=workers) as phase_a, \
            ThreadPoolExecutor(max_workers=max(1, len(by_customer))) as phase_b:
        created = {load.fixture.session_id: phase_a.submit(contextvars.copy_context().run,
                                                           _timed_create, bank, load, load_id)
                   for load in loads}
        verdicts = {load.fixture.session_id: phase_a.submit(contextvars.copy_context().run,
                                                            _validate_timed, validate, load.fixture)
                    for load in loads
                    if gate and mwp.customer_text(turns_for_write_path(load.fixture))}

        def run_customer(customer_loads: Sequence[SessionLoad]) -> None:
            for load in customer_loads:
                sid = load.fixture.session_id
                try:
                    created[sid].result()
                except Exception as exc:  # noqa: BLE001
                    load.error = "session create/append failed: %r" % exc
                    continue
                record_session(bank, load, gate=gate, verdict=verdicts.get(sid), name_lookup=name_lookup)
                progress("loaded %d/%d sessions (%s %s -> %s)" % (
                    next(finished), len(loads), load.fixture.customer_id, sid,
                    load.lineage.get("standing", load.error)))

        futures = [phase_b.submit(contextvars.copy_context().run, run_customer, group)
                   for group in by_customer.values()]
        for future in futures:
            future.result()
    return loads


def reset_engine(bank: EvalMemoryBank, workers: int = 16) -> dict[str, int]:
    """Deletes every memory and session on the eval engine."""
    memories = [m["name"] for m in bank.iter_memories()]
    sessions = [s["name"] for s in bank.list_sessions()]
    parallel_map(bank.delete_memory, memories, workers)
    parallel_map(bank.delete_session, sessions, workers)
    return {"memories_deleted": len(memories), "sessions_deleted": len(sessions)}


def delete_memories(bank: MemoryBankClient, names: Iterable[str], workers: int = 16) -> dict[str, int]:
    def delete(name: str) -> bool:
        try:
            bank.delete_memory(name)
            return True
        except MemoryBankError:
            return False

    outcomes = parallel_map(delete, list(names), workers)
    return {"deleted": sum(outcomes), "failed": len(outcomes) - sum(outcomes)}


def lineage_index(loads: Iterable[SessionLoad]) -> dict[str, tuple[SessionLoad, str]]:
    """memory id -> (the latest session that created or updated it, kind)."""
    index: dict[str, tuple[SessionLoad, str]] = {}
    for load in sorted(loads, key=lambda item: item.fixture.started_at):
        for item in load.lineage.get("generated", []):
            if item.get("action") != "DELETED" and item.get("name") not in (None, NOT_RECORDED):
                index[mwp.memory_id(item["name"])] = (load, "generated")
        claim = (load.lineage.get("claim_record") or {}).get("name")
        if claim and claim != NOT_RECORDED:
            index[mwp.memory_id(claim)] = (load, "claim_record")
    return index


def fact_rows(memories: Iterable[Mapping[str, Any]], loads: Iterable[SessionLoad],
              load_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    """One `facts` row per memory, annotated with the session that produced it.

    A memory no lineage names is kept as kind "orphan": the BigQuery side has
    to hold everything the Memory Bank side can return, or it is not the same
    corpus.
    """
    index = lineage_index(loads)
    rows = []
    for memory in memories:
        scope = memory.get("scope") or {}
        source, source_kind = index.get(mwp.memory_id(memory["name"]), (None, "orphan"))
        rows.append({
            "run_id": load_id,
            "memory_name": memory["name"],
            "memory_id": mwp.memory_id(memory["name"]),
            "customer_id": scope.get("customer_id", NOT_RECORDED),
            "standing": scope.get("corpus", mwp.Standing.TRUSTED.value),
            "kind": kind or source_kind,
            "fact": memory.get("fact", NOT_RECORDED),
            "topics": topic_labels(dict(memory)),
            "source_session": source.session_name if source else None,
            "source_fixture_session": source.fixture.session_id if source else None,
            "source_time": source.fixture.ended_at.isoformat() if source else None,
            "create_time": memory.get("createTime"),
            "update_time": memory.get("updateTime"),
        })
    return rows


def raw_note_rows(loads: Iterable[SessionLoad], load_id: str, kind: str = "fixture") -> list[dict[str, Any]]:
    return [
        {
            "run_id": load_id,
            "customer_id": load.fixture.customer_id,
            "session": load.fixture.session_id,
            "vertex_session": load.session_name,
            "channel": load.fixture.channel,
            "turn_index": index,
            "author": turn.author,
            "text": turn.text,
            "timestamp": turn.timestamp.isoformat(),
            "kind": kind,
        }
        for load in loads
        for index, turn in enumerate(load.fixture.turns)
    ]


def memories_in_scopes(bank: EvalMemoryBank, customer_ids: Iterable[str]) -> list[dict[str, Any]]:
    wanted = set(customer_ids)
    return [m for m in bank.iter_memories() if (m.get("scope") or {}).get("customer_id") in wanted]

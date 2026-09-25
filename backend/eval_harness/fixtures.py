"""The demo kit's memory fixtures, read as they are on disk.

Counts are reported from the files, never assumed. Every expected memory cites
the session and turn that states it, which is what lets the raw-notes baseline
be scored without an LLM: a note row is relevant exactly when it is that turn.
"""

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend.eval_harness.env import FIXTURES_DIR

CUSTOMER_ROLE = "user"


@dataclass(frozen=True)
class Turn:
    author: str
    text: str
    timestamp: datetime


@dataclass(frozen=True)
class FixtureSession:
    session_id: str
    customer_id: str
    channel: str
    intent: str
    started_at: datetime
    ended_at: datetime
    turns: tuple[Turn, ...]


def _stamp(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _read_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    return tuple(json.loads(line) for line in path.read_text().splitlines() if line.strip())


def _read_session(path: Path) -> FixtureSession:
    raw = json.loads(path.read_text())
    return FixtureSession(
        session_id=raw["session_id"],
        customer_id=raw["customer_id"],
        channel=raw["channel"],
        intent=raw.get("intent", ""),
        started_at=_stamp(raw["started_at"]),
        ended_at=_stamp(raw["ended_at"]),
        turns=tuple(Turn(e["role"], e["text"], _stamp(e["timestamp"])) for e in raw["events"]),
    )


@dataclass(frozen=True)
class Fixtures:
    root: Path
    sessions: tuple[FixtureSession, ...]
    expected: tuple[dict[str, Any], ...]
    retrieval_cases: tuple[dict[str, Any], ...]
    contradictions: tuple[dict[str, Any], ...]
    negatives: tuple[dict[str, Any], ...]
    leakage_pairs: tuple[dict[str, Any], ...]
    staleness: tuple[dict[str, Any], ...]

    @classmethod
    def load(cls, root: Path = FIXTURES_DIR) -> "Fixtures":
        return cls(
            root=root,
            sessions=tuple(sorted((_read_session(p) for p in (root / "sessions").glob("*.json")),
                                  key=lambda s: (s.started_at, s.session_id))),
            expected=_read_jsonl(root / "memories_expected.jsonl"),
            retrieval_cases=_read_jsonl(root / "retrieval_cases.jsonl"),
            contradictions=_read_jsonl(root / "contradictions.jsonl"),
            negatives=_read_jsonl(root / "negative_memories.jsonl"),
            leakage_pairs=_read_jsonl(root / "leakage_pairs.jsonl"),
            staleness=_read_jsonl(root / "staleness.jsonl"),
        )

    def counts(self) -> dict[str, int]:
        return {
            "sessions": len(self.sessions),
            "turns": sum(len(s.turns) for s in self.sessions),
            "customer_turns": sum(1 for s in self.sessions for t in s.turns if t.author == CUSTOMER_ROLE),
            "customers": len({s.customer_id for s in self.sessions}),
            "expected_facts": len(self.expected),
            "retrieval_cases": len(self.retrieval_cases),
            "retrieval_cases_expecting_nothing": sum(1 for c in self.retrieval_cases if not c["expected_memory_ids"]),
            "contradiction_pairs": len(self.contradictions),
            "negative_memories": len(self.negatives),
            "leakage_pairs": len(self.leakage_pairs),
            "staleness_records": len(self.staleness),
        }

    def expected_by_id(self) -> Mapping[str, dict[str, Any]]:
        return {row["memory_id"]: row for row in self.expected}

    def expected_for(self, customer_id: str) -> Sequence[dict[str, Any]]:
        return [row for row in self.expected if row["customer_id"] == customer_id]

    def negatives_for(self, customer_id: str) -> Sequence[dict[str, Any]]:
        return [row for row in self.negatives if row["customer_id"] == customer_id]

    def memory_ids_by_turn(self) -> Mapping[tuple[str, int], frozenset[str]]:
        grouped: defaultdict[tuple[str, int], set[str]] = defaultdict(set)
        for row in self.expected:
            grouped[(row["source_session_id"], int(row["source_turn_index"]))].add(row["memory_id"])
        return {key: frozenset(ids) for key, ids in grouped.items()}

    def turn_relevance_map(self) -> dict[str, frozenset[str]]:
        grouped: defaultdict[str, set[str]] = defaultdict(set)
        for row in self.expected:
            key = "%s#%s" % (row["source_session_id"], int(row["source_turn_index"]))
            grouped[key].add(row["memory_id"])
        return {key: frozenset(ids) for key, ids in grouped.items()}

    def as_of(self) -> datetime:
        """The day after the last fixture session: "today" for every question asked."""
        last = max(s.ended_at for s in self.sessions)
        return (last + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    def source_path(self, name: str) -> str:
        return str((self.root / name).relative_to(self.root.parents[2]))

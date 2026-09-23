"""
Vertex AI Agent Engine Memory Bank -- a real client.

What was here before, on `main`, was a Python list. `CustomerMemoryBank` held
`MemoryFragment` objects in process memory, called that a "shared epistemic
layer", and synced a copy to Vertex through a *private* ADK method
(`svc._add_memories_via_create`) that nothing ever read back. The demo wrote to
the cloud and then answered every question from the local list. Restarting the
process was indistinguishable from wiping the bank.

This module talks to the service. Nothing is cached in a module-level dict,
nothing is seeded on construction, and every method below corresponds to one
documented REST call.

The distinction that matters most here -- and the one the rest of this codebase
is built to demonstrate -- is between the two ways to put something in the bank:

    create_memory(...)              you decide. The fact is stored verbatim.
    generate_from_conversation(...) the service decides. It reads the turns,
                                    extracts what is durable, drops what is not,
                                    consolidates against what it already holds,
                                    and files the result under a topic.

`generate` is the interesting one, because it *declines*. Feeding it a
transcript and diffing the corpus before and after tells you what the service
considered worth keeping -- which is a measurable property, not a claim. See
`backend/memory_eval/write_policy.py`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

import google.auth
import google.auth.transport.requests
import requests

from backend.config import Settings, settings as load_settings
from backend.provenance import Provenanced, measured, unmeasured

log = logging.getLogger(__name__)

API_VERSION = "v1beta1"
"""Memory Bank is a v1beta1 surface. Pinned explicitly: silently following a
default version is how a working integration breaks on someone else's release."""

SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


class MemoryBankError(RuntimeError):
    """A Memory Bank call failed.

    Carries the HTTP status and the response body verbatim. The body is the
    useful part -- Vertex explains refusals in prose -- and truncating it to a
    tidy message is how "Method not visible to labels: {PUBLIC}" becomes an
    unexplained 503.
    """

    def __init__(self, message: str, *, status: Optional[int] = None, body: str = "", url: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body
        self.url = url

    def to_dict(self) -> dict:
        return {
            "error": str(self),
            "http_status": self.status,
            "response_body": self.body[:4000],
            "url": self.url,
        }


# --------------------------------------------------------------------------
# Value types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryTopic:
    """One entry in the engine's topic vocabulary.

    A topic is not a tag applied after the fact. It is part of the engine
    configuration that `generateMemories` consults when deciding whether a piece
    of a conversation is worth keeping at all.
    """

    label: str
    kind: str  # "CUSTOM" or "MANAGED"
    description: str = ""

    @classmethod
    def from_api(cls, payload: dict) -> "MemoryTopic":
        """Normalise the two shapes the engine config uses into one.

        A custom topic is `{"customMemoryTopic": {"label": ..., "description": ...}}`;
        a managed one is `{"managedMemoryTopic": {"managedTopicEnum": ...}}`. The
        caller should not have to care which.
        """
        if "customMemoryTopic" in payload:
            body = payload["customMemoryTopic"]
            return cls(
                label=body.get("label", ""),
                kind="CUSTOM",
                description=body.get("description", ""),
            )
        if "managedMemoryTopic" in payload:
            body = payload["managedMemoryTopic"]
            return cls(
                label=body.get("managedTopicEnum", ""),
                kind="MANAGED",
                description=body.get("description", ""),
            )
        return cls(label=json.dumps(payload, sort_keys=True), kind="UNKNOWN")

    def to_dict(self) -> dict:
        return {"label": self.label, "kind": self.kind, "description": self.description}


@dataclass(frozen=True)
class Memory:
    """One stored memory, as the service returns it."""

    name: str
    fact: str
    scope: dict
    topics: tuple[str, ...] = ()
    memory_type: str = ""
    create_time: str = ""
    update_time: str = ""
    distance: Optional[float] = None
    """Similarity distance. Present only on `memories:retrieve` results, and
    absent on scope-only retrieval and on `list`. It is a *distance*, so smaller
    is closer -- reporting it as a similarity score without inverting it is an
    easy and embarrassing mistake."""

    @property
    def memory_id(self) -> str:
        return self.name.rsplit("/", 1)[-1] if self.name else ""

    @property
    def short_id(self) -> str:
        """Enough of the id to identify a memory in a table without filling it."""
        mid = self.memory_id
        return mid[-8:] if len(mid) > 8 else mid

    @classmethod
    def from_api(cls, payload: dict, distance: Optional[float] = None) -> "Memory":
        return cls(
            name=payload.get("name", ""),
            fact=payload.get("fact", ""),
            scope=dict(payload.get("scope", {})),
            topics=tuple(_topic_labels(payload.get("topics", []))),
            memory_type=payload.get("memoryType", ""),
            create_time=payload.get("createTime", ""),
            update_time=payload.get("updateTime", ""),
            distance=distance,
        )

    def to_dict(self) -> dict:
        out = {
            "id": self.memory_id,
            "short_id": self.short_id,
            "fact": self.fact,
            "scope": self.scope,
            "topics": list(self.topics),
            "memory_type": self.memory_type,
            "create_time": self.create_time,
            "update_time": self.update_time,
        }
        if self.distance is not None:
            out["distance"] = self.distance
        return out


def _topic_labels(raw_topics: Iterable[dict]) -> list[str]:
    """Flatten the *on-memory* topic shape into plain labels.

    Worth noting because it differs from the engine-config shape above: a stored
    memory carries `{"customMemoryTopicLabel": "TRAVEL_STATUS"}`, not a nested
    object. Before this was handled, printed memories read
    `topics {'customMemoryTopicLabel': 'TRAVEL_STATUS'}` -- the dict `repr`,
    stringified straight into the UI.
    """
    labels: list[str] = []
    for topic in raw_topics or []:
        if not isinstance(topic, dict):
            labels.append(str(topic))
            continue
        label = (
            topic.get("customMemoryTopicLabel")
            or topic.get("managedMemoryTopicLabel")
            or topic.get("label")
        )
        labels.append(label if label else json.dumps(topic, sort_keys=True))
    return labels


@dataclass(frozen=True)
class GeneratedMemory:
    """One outcome of a `generateMemories` call.

    `action` is the service's verdict on this fact relative to what it already
    held: it created something new, updated something that existed, or deleted
    something the new turns contradicted.
    """

    memory: Memory
    action: str

    @classmethod
    def from_api(cls, payload: dict) -> "GeneratedMemory":
        return cls(
            memory=Memory.from_api(payload.get("memory", {})),
            action=payload.get("action", "UNSPECIFIED"),
        )

    def to_dict(self) -> dict:
        return {"action": self.action, "memory": self.memory.to_dict()}


@dataclass
class RetrievalResult:
    """The outcome of one retrieval, with enough context to audit it."""

    memories: list[Memory]
    scope: dict
    query: Optional[str]
    top_k: Optional[int]
    latency_ms: Provenanced
    request_body: dict = field(default_factory=dict)

    def facts(self) -> list[str]:
        return [m.fact for m in self.memories]

    def ids(self) -> list[str]:
        return [m.memory_id for m in self.memories]

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "query": self.query,
            "top_k": self.top_k,
            "count": len(self.memories),
            "latency_ms": self.latency_ms.to_dict(),
            "memories": [m.to_dict() for m in self.memories],
            "request_body": self.request_body,
        }


@dataclass
class GenerateResult:
    """The outcome of one `generateMemories` call."""

    operation: str
    done: bool
    generated: list[GeneratedMemory]
    scope: dict
    latency_ms: Provenanced
    raw_response: dict = field(default_factory=dict)

    @property
    def wrote_nothing(self) -> bool:
        """True when the service completed and chose to store nothing.

        This is a real and frequent outcome, not an error, and it is the single
        most informative thing `generate` does. A completed operation with no
        `response` body means every candidate fact in the transcript was
        rejected -- see `memory_eval/write_policy.py`, which is built on it.
        """
        return self.done and not self.generated

    def by_action(self, action: str) -> list[GeneratedMemory]:
        return [g for g in self.generated if g.action == action]

    def to_dict(self) -> dict:
        return {
            "operation": self.operation,
            "done": self.done,
            "scope": self.scope,
            "wrote_nothing": self.wrote_nothing,
            "generated_count": len(self.generated),
            "generated": [g.to_dict() for g in self.generated],
            "latency_ms": self.latency_ms.to_dict(),
        }


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class MemoryBankClient:
    """REST client for one Agent Engine Memory Bank.

    Deliberately not a singleton and deliberately not seeded. Constructing it
    performs no I/O and creates no data; the previous implementation called
    `seed_default_scenario()` from `__init__`, which meant that merely *reading*
    the bank invented fragments in it.
    """

    def __init__(
        self,
        cfg: Optional[Settings] = None,
        credentials: Any = None,
        session: Optional[requests.Session] = None,
        timeout_s: float = 60.0,
    ) -> None:
        self.cfg = cfg or load_settings()
        self.timeout_s = timeout_s
        self._session = session or requests.Session()
        self._credentials = credentials
        self._engine_cache: Optional[dict] = None

    # -- plumbing ----------------------------------------------------------

    @property
    def engine(self) -> str:
        return self.cfg.agent_engine_resource

    def _base(self) -> str:
        return f"{self.cfg.aiplatform_endpoint}/{API_VERSION}"

    def _token(self) -> str:
        if self._credentials is None:
            self._credentials, _ = google.auth.default(scopes=list(SCOPES))
        if not self._credentials.valid:
            self._credentials.refresh(google.auth.transport.requests.Request())
        return self._credentials.token

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        url = path if path.startswith("http") else f"{self._base()}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
        }
        log.debug("-> %s %s body=%s", method, url, json.dumps(body or {})[:1000])
        try:
            resp = self._session.request(
                method,
                url,
                headers=headers,
                json=body,
                params=params,
                timeout=self.timeout_s,
            )
        except requests.RequestException as exc:
            raise MemoryBankError(f"{method} {url} failed: {exc}", url=url) from exc

        if resp.status_code >= 400:
            raise MemoryBankError(
                f"{method} {url} returned HTTP {resp.status_code}",
                status=resp.status_code,
                body=resp.text,
                url=url,
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise MemoryBankError(
                f"{method} {url} returned non-JSON body", status=resp.status_code, body=resp.text, url=url
            ) from exc

    # -- engine configuration ---------------------------------------------

    def get_engine(self, refresh: bool = False) -> dict:
        """Fetch the reasoning engine resource, including its memory config."""
        if self._engine_cache is None or refresh:
            self._engine_cache = self._request("GET", self.engine)
        return self._engine_cache

    def memory_topics(self, refresh: bool = False) -> list[MemoryTopic]:
        """The engine's topic vocabulary, read live.

        Read from the engine rather than restated in Python on purpose. A topic
        added in the console shows up here on the next call; a hardcoded copy
        would drift the moment anyone touched the configuration, and drift in a
        *vocabulary* is invisible -- the code keeps working and quietly stops
        agreeing with the service.
        """
        engine = self.get_engine(refresh=refresh)
        configs = (
            engine.get("contextSpec", {})
            .get("memoryBankConfig", {})
            .get("customizationConfigs", [])
        )
        topics: list[MemoryTopic] = []
        for conf in configs:
            for raw in conf.get("memoryTopics", []):
                topics.append(MemoryTopic.from_api(raw))
        return topics

    # -- reads -------------------------------------------------------------

    def retrieve(
        self,
        scope: dict,
        query: Optional[str] = None,
        top_k: Optional[int] = None,
    ) -> RetrievalResult:
        """Retrieve memories for a scope.

        Two modes, and the difference is the whole point of the benchmark:

        * `query` given -- similarity search. The service ranks by semantic
          closeness and returns a `distance` per result. This is what an agent
          uses mid-turn.
        * `query` omitted -- scope-only retrieval. Everything filed under the
          scope, unranked. This is what establishes the denominator when you
          want to know what recall a query *could* have achieved.

        Args:
            scope: the partition key, e.g. `{"customer_id": "CUST00001"}`.
                Scope is an exact-match filter, not a hint -- see
                `docs/MEMORY_BANK_EVIDENCE.md` on why that makes it the harder
                of the two write controls.
            query: natural-language search string.
            top_k: cap on results. Only meaningful with `query`.
        """
        body: dict[str, Any] = {"scope": dict(scope)}
        if query:
            params: dict[str, Any] = {"searchQuery": query}
            if top_k is not None:
                params["topK"] = top_k
            body["similaritySearchParams"] = params
        else:
            body["simpleRetrievalParams"] = {"pageSize": top_k or 100}

        started = time.perf_counter()
        payload = self._request("POST", f"{self.engine}/memories:retrieve", body=body)
        elapsed = (time.perf_counter() - started) * 1000.0

        memories = [
            Memory.from_api(item.get("memory", {}), distance=item.get("distance"))
            for item in payload.get("retrievedMemories", [])
        ]
        return RetrievalResult(
            memories=memories,
            scope=dict(scope),
            query=query,
            top_k=top_k,
            latency_ms=measured(round(elapsed, 1), unit="ms", source="memories:retrieve"),
            request_body=body,
        )

    def list_memories(self, scope: Optional[dict] = None, page_size: int = 100) -> list[Memory]:
        """List memories, optionally filtered to a scope.

        Pages to exhaustion. The corpus here is small, but a benchmark that
        silently measured recall against only the first page would be worse than
        no benchmark.
        """
        params: dict[str, Any] = {"pageSize": page_size}
        if scope:
            clauses = [f'scope.{k}="{v}"' for k, v in sorted(scope.items())]
            params["filter"] = " AND ".join(clauses)

        out: list[Memory] = []
        token: Optional[str] = None
        while True:
            if token:
                params["pageToken"] = token
            payload = self._request("GET", f"{self.engine}/memories", params=params)
            out.extend(Memory.from_api(m) for m in payload.get("memories", []))
            token = payload.get("nextPageToken")
            if not token:
                return out

    def get_memory(self, name: str) -> Memory:
        """Fetch one memory by resource name or bare id."""
        path = name if "/" in name else f"{self.engine}/memories/{name}"
        return Memory.from_api(self._request("GET", path))

    # -- writes ------------------------------------------------------------

    def create_memory(
        self,
        fact: str,
        scope: dict,
        topics: Optional[Sequence[str]] = None,
        wait: bool = True,
        timeout_s: float = 120.0,
    ) -> Memory:
        """Store a fact verbatim. The *caller* decides what is worth keeping.

        This is the honest baseline against which `generate_from_conversation`
        is measured. It performs no extraction, no consolidation and no
        filtering: whatever string you pass is what ends up in the bank, topic
        labels and all. Useful for seeding a benchmark with known ground truth,
        and a poor choice for ingesting conversations, because it will happily
        store "what is the weather today".
        """
        body: dict[str, Any] = {"fact": fact, "scope": dict(scope)}
        if topics:
            body["topics"] = [{"customMemoryTopicLabel": t} for t in topics]

        payload = self._request("POST", f"{self.engine}/memories", body=body)
        if not wait or payload.get("done"):
            inner = payload.get("response", payload)
            return Memory.from_api(inner)

        operation = self._await_operation(payload.get("name", ""), timeout_s=timeout_s)
        return Memory.from_api(operation.get("response", {}))

    def generate_from_conversation(
        self,
        turns: Sequence[dict],
        scope: dict,
        wait: bool = True,
        timeout_s: float = 180.0,
        poll_interval_s: float = 2.0,
    ) -> GenerateResult:
        """Hand raw conversation turns to the service and let *it* decide.

        This is the "intelligent write". The service reads the turns, decides
        which statements are durable facts about this scope, discards the rest,
        reconciles them against memories it already holds, and assigns a topic
        from the engine's vocabulary.

        Three outcomes are worth distinguishing, and all three are visible in
        the result:

        * `CREATED`   -- a new fact the bank did not have.
        * `UPDATED`   -- an existing memory revised. This is consolidation: the
          service recognised the same underlying fact and rewrote it rather than
          storing a near-duplicate.
        * `DELETED`   -- an existing memory the new turns contradicted.

        And the fourth, which has no entry at all: a completed operation with an
        empty response, meaning **nothing in the transcript qualified**. That is
        `wrote_nothing`, and it is not a failure.

        Args:
            turns: `[{"role": "user"|"model", "text": "..."}]`, or raw
                `{"content": {...}}` events, which are passed through untouched.
            scope: the partition to write under.
            wait: poll the long-running operation to completion. With
                `wait=False` the call returns in milliseconds and the extraction
                happens server-side afterwards -- which is the right choice on a
                user-facing turn, and the wrong one for a measurement.
        """
        events = [_as_event(t) for t in turns]
        body = {"scope": dict(scope), "directContentsSource": {"events": events}}

        started = time.perf_counter()
        payload = self._request("POST", f"{self.engine}/memories:generate", body=body)
        op_name = payload.get("name", "")

        if not wait:
            elapsed = (time.perf_counter() - started) * 1000.0
            return GenerateResult(
                operation=op_name,
                done=False,
                generated=[],
                scope=dict(scope),
                latency_ms=measured(round(elapsed, 1), unit="ms", source="memories:generate (async)"),
                raw_response=payload,
            )

        operation = self._await_operation(op_name, timeout_s=timeout_s, poll_interval_s=poll_interval_s)
        elapsed = (time.perf_counter() - started) * 1000.0

        response = operation.get("response", {}) or {}
        generated = [
            GeneratedMemory.from_api(item) for item in response.get("generatedMemories", [])
        ]
        return GenerateResult(
            operation=op_name,
            done=bool(operation.get("done")),
            generated=generated,
            scope=dict(scope),
            latency_ms=measured(round(elapsed, 1), unit="ms", source="memories:generate"),
            raw_response=operation,
        )

    def delete_memory(self, name: str) -> None:
        """Delete one memory. Used by the evaluation harness to clean up after
        itself; never called from the request path."""
        path = name if "/" in name else f"{self.engine}/memories/{name}"
        self._request("DELETE", path)

    def delete_scope(self, scope: dict) -> int:
        """Delete every memory in a scope. Returns the count removed.

        Only ever pointed at the harness's own throwaway scopes. It is a real
        deletion with no undo, so it takes an explicit scope and refuses an
        empty one rather than defaulting to "everything".
        """
        if not scope:
            raise ValueError(
                "delete_scope requires an explicit scope; refusing to delete the whole bank."
            )
        memories = self.list_memories(scope=scope)
        for memory in memories:
            self.delete_memory(memory.name)
        return len(memories)

    # -- operations --------------------------------------------------------

    def _await_operation(
        self,
        name: str,
        timeout_s: float = 180.0,
        poll_interval_s: float = 2.0,
    ) -> dict:
        """Poll a long-running operation until it reports done.

        Raises on timeout rather than returning a partial result. A generate
        that has not finished has written an unknown amount, and treating that
        as "wrote nothing" would corrupt every measurement downstream.
        """
        if not name:
            raise MemoryBankError("operation name missing from response")
        deadline = time.monotonic() + timeout_s
        while True:
            operation = self._request("GET", name)
            if operation.get("done"):
                if "error" in operation:
                    err = operation["error"]
                    raise MemoryBankError(
                        f"operation {name} failed: {err.get('message', err)}",
                        body=json.dumps(err),
                    )
                return operation
            if time.monotonic() > deadline:
                raise MemoryBankError(
                    f"operation {name} did not complete within {timeout_s:.0f}s. "
                    f"Not treating this as 'wrote nothing' -- the write may still land."
                )
            time.sleep(poll_interval_s)


def _as_event(turn: Any) -> dict:
    """Coerce a turn into the `events[].content` shape the API expects."""
    if isinstance(turn, dict) and "content" in turn:
        return turn
    if isinstance(turn, dict):
        role = turn.get("role", "user")
        text = turn.get("text", turn.get("content", ""))
        return {"content": {"role": role, "parts": [{"text": text}]}}
    return {"content": {"role": "user", "parts": [{"text": str(turn)}]}}


def describe_unavailable(exc: MemoryBankError) -> Provenanced:
    """Turn a failure into a value that admits it is a failure.

    Used at call sites that must return *something*. The something is `n/r`
    carrying the real HTTP body -- not an empty list, which a reader would
    correctly interpret as "the bank is empty" rather than "we could not ask".
    """
    return unmeasured(f"Memory Bank unavailable: {exc} :: {exc.body[:300]}")

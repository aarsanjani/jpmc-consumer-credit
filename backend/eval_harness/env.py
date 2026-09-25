"""Settings for the eval harness, and the only Memory Bank client it writes with.

Everything the harness writes goes to the eval Agent Engine
(VERTEX_EVAL_AGENT_ENGINE_ID) and the eval dataset (EVAL_BQ_DATASET).
`EvalSettings.from_env` refuses to start when either one names the live demo
engine or the governed demo dataset, so a copy-paste slip in .env fails loudly
instead of loading 52 fixture sessions into the demo.
"""

import contextvars
import os
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

from backend.memory_bank import DEFAULT_LOCATION, MemoryBankClient, MemoryBankError
from backend.provenance import require_env

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "jpmc_fraud_demo_kit" / "jpmc_fraud_demo_kit" / "memory"
# Run files live in a subfolder so they never collide with run_eval.py's
# results_memory_on.json / results_memory_off.json in the same directory.
RUNS_SUBDIR = "jpmc_harness"

DEMO_ENGINE_VARS = ("VERTEX_AGENT_ENGINE_ID", "AGENT_ENGINE_ID", "VERTEX_MEMORY_BANK_ENGINE_ID")
GOVERNED_DATASET_VARS = ("BIGQUERY_DATASET", "BQ_DATASET")


class UnsafeTargetError(ValueError):
    """The eval engine or dataset is the demo's own. Nothing was written."""


def runs_dir_from_env() -> Path:
    """Where run files go. Light on purpose: the Flask blueprint calls it."""
    raw = Path(require_env("EVAL_RESULTS_DIR"))
    return (raw if raw.is_absolute() else REPO_ROOT / raw) / RUNS_SUBDIR


@dataclass(frozen=True)
class EvalSettings:
    project: str
    location: str
    engine_id: str
    dataset: str
    model: str
    runs_dir: Path
    # Read only, for the card-ending check in the load report.
    governed_dataset: str | None = None
    # Tier 2 only. Left None when unset, and tier 2 is then reported n/r.
    connection: str | None = None
    embedding_endpoint: str | None = None
    ranking_model: str | None = None

    @classmethod
    def from_env(cls) -> "EvalSettings":
        engine_id = require_env("VERTEX_EVAL_AGENT_ENGINE_ID")
        if clash := [name for name in DEMO_ENGINE_VARS if os.environ.get(name) == engine_id]:
            raise UnsafeTargetError(
                "VERTEX_EVAL_AGENT_ENGINE_ID equals %s, the live demo engine. The harness "
                "only writes to a separate eval engine." % ", ".join(clash))
        dataset = require_env("EVAL_BQ_DATASET")
        if clash := [name for name in GOVERNED_DATASET_VARS if os.environ.get(name) == dataset]:
            raise UnsafeTargetError(
                "EVAL_BQ_DATASET equals %s, the governed demo dataset. Pick a new dataset "
                "for eval tables." % ", ".join(clash))
        return cls(
            project=require_env("GOOGLE_CLOUD_PROJECT"),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", DEFAULT_LOCATION),
            engine_id=engine_id,
            dataset=dataset,
            model=require_env("GEMINI_MODEL"),
            runs_dir=runs_dir_from_env(),
            governed_dataset=os.environ.get("BIGQUERY_DATASET") or os.environ.get("BQ_DATASET") or None,
            connection=os.environ.get("EVAL_BQ_CONNECTION") or None,
            embedding_endpoint=os.environ.get("EVAL_EMBEDDING_MODEL") or None,
            ranking_model=os.environ.get("EVAL_RANKING_MODEL") or None,
        )


# Statuses that mean the server did not act on the request, so a resend
# cannot double-write. 500/502/504 are ambiguous and are not retried.
RETRY_STATUSES = frozenset({429, 503})
RETRY_DELAYS_S = (2.0, 5.0, 15.0)


class EvalMemoryBank(MemoryBankClient):
    """MemoryBankClient with paging for list calls and resend on 429/503.

    A full load makes about 600 calls, so one quota blip would otherwise drop
    a session from the corpus.
    """

    @override
    def _request(self, method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        for delay in RETRY_DELAYS_S:
            try:
                return super()._request(method, url, body)
            except MemoryBankError as exc:
                if exc.status not in RETRY_STATUSES:
                    raise
                time.sleep(delay)
        return super()._request(method, url, body)

    def iter_memories(self, page_size: int = 100) -> Iterator[dict[str, Any]]:
        url = "%s/memories?pageSize=%d" % (self.base_url, page_size)
        page = self._request("GET", url)
        yield from page.get("memories", [])
        while token := page.get("nextPageToken"):
            page = self._request("GET", "%s&pageToken=%s" % (url, token))
            yield from page.get("memories", [])

    def create_memory_with_topics(self, fact: str, scope: dict[str, str], topics: Iterable[str]) -> str:
        """memories.create with topic labels, which MemoryBankClient.create_memory doesn't send.
        Checked live: the labels are stored and topic filters match them."""
        body = {"fact": fact, "scope": scope, "topics": [{"customMemoryTopicLabel": t} for t in topics]}
        operation = self.wait_for_operation(self._request("POST", self.base_url + "/memories", body))
        return (operation.get("response") or {}).get("name", "")

    def list_revisions(self, memory_name: str) -> list[dict[str, Any]]:
        url = "https://%s/%s/%s/revisions" % (self.host, self.api_version, memory_name)
        return self._request("GET", url).get("memoryRevisions", [])


def eval_memory_bank(settings: EvalSettings) -> EvalMemoryBank:
    """The harness's Memory Bank client, bound to the eval engine only."""
    return EvalMemoryBank(project=settings.project, engine_id=settings.engine_id,
                          location=settings.location, verbose=False)


def parallel_map[T, R](fn: Callable[[T], R], items: Iterable[T], workers: int = 8) -> list[R]:
    """Ordered map over a thread pool that carries the caller's contextvars.

    The context is copied here, in the submitting thread; copying it inside the
    worker would copy the worker's empty context and drop the obs run id.
    """
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(contextvars.copy_context().run, fn, item) for item in items]
        return [future.result() for future in futures]

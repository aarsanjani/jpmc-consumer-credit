"""The eval dataset in BigQuery: `facts`, `raw_notes`, and the baseline queries.

Every write goes to EVAL_BQ_DATASET (checked in env.py). Reads of the governed
demo dataset happen in one place only, `card_endings`, and select three
non-restricted columns.

Rows carry the run id, so tables keep history and every query filters on it.
The tier 2 baseline is VECTOR_SEARCH here, then `Ranker` (Vertex AI ranking API).
"""

import json
import re
import time
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from google.cloud import bigquery

from backend.eval_harness.env import EvalSettings
from backend.gcp_auth import BearerTokenSource

FACTS = "facts"
RAW_NOTES = "raw_notes"
FACT_EMBEDDINGS = "fact_embeddings"
# Remote model in the eval dataset, recreated each run over EVAL_BQ_CONNECTION.
EMBEDDER = "fact_embedder"
# DDL can't take query parameters, so names formatted into it are checked first.
_DDL_NAME_RE = re.compile(r"[A-Za-z0-9@._-]+")

_S = bigquery.SchemaField
SCHEMAS: Mapping[str, tuple[str, Sequence[bigquery.SchemaField]]] = {
    FACTS: ("Facts Memory Bank extracted on the eval engine, one row per memory, with lineage.", (
        _S("run_id", "STRING", mode="REQUIRED"),
        _S("memory_name", "STRING"),
        _S("memory_id", "STRING"),
        _S("customer_id", "STRING"),
        _S("standing", "STRING", description="trusted, pending_review or quarantine (the memory's scope)"),
        _S("kind", "STRING", description="generated, claim_record, orphan (no lineage) or noise (volume test)"),
        _S("fact", "STRING"),
        _S("topics", "STRING", mode="REPEATED"),
        _S("source_session", "STRING", description="Vertex session whose lineage lists this memory"),
        _S("source_fixture_session", "STRING"),
        _S("source_time", "TIMESTAMP", description="when the source session ended (when it was said)"),
        _S("create_time", "TIMESTAMP"),
        _S("update_time", "TIMESTAMP"),
    )),
    RAW_NOTES: ("Raw session turns, one row per turn: the 'latest notes' baseline.", (
        _S("run_id", "STRING", mode="REQUIRED"),
        _S("customer_id", "STRING"),
        _S("session", "STRING"),
        _S("vertex_session", "STRING"),
        _S("channel", "STRING"),
        _S("turn_index", "INTEGER"),
        _S("author", "STRING"),
        _S("text", "STRING"),
        _S("timestamp", "TIMESTAMP"),
        _S("kind", "STRING", description="fixture or scenario"),
    )),
    FACT_EMBEDDINGS: ("Embeddings of `facts` rows for the tier 2 VECTOR_SEARCH baseline.", (
        _S("run_id", "STRING", mode="REQUIRED"),
        _S("memory_id", "STRING"),
        _S("customer_id", "STRING"),
        _S("standing", "STRING"),
        _S("kind", "STRING"),
        _S("fact", "STRING"),
        _S("embedding", "FLOAT64", mode="REPEATED"),
        _S("status", "STRING"),
    )),
}


def _param(name: str, value: Any) -> bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter:
    if isinstance(value, (list, tuple, frozenset, set)):
        return bigquery.ArrayQueryParameter(name, "STRING", sorted(value))
    kind = "INT64" if isinstance(value, int) else "STRING"
    return bigquery.ScalarQueryParameter(name, kind, value)


class Warehouse:
    def __init__(self, settings: EvalSettings, client: bigquery.Client | None = None):
        self.project = settings.project
        self.dataset = settings.dataset
        self.location = settings.location
        self.client = client or bigquery.Client(project=settings.project, location=settings.location)

    def table(self, name: str) -> str:
        return "%s.%s.%s" % (self.project, self.dataset, name)

    def ensure(self) -> dict[str, bool]:
        """Creates the dataset and tables if missing. Returns what already existed."""
        existed = {}
        dataset = bigquery.Dataset("%s.%s" % (self.project, self.dataset))
        dataset.location = self.location
        dataset.description = "JPMC eval harness tables (backend/eval_harness). Safe to drop; rebuilt by each run."
        existed["dataset"] = self._exists(lambda: self.client.get_dataset(dataset.reference))
        self.client.create_dataset(dataset, exists_ok=True)
        for name, (description, schema) in SCHEMAS.items():
            existed[name] = self._exists(lambda: self.client.get_table(self.table(name)))
            table = bigquery.Table(self.table(name), schema=list(schema))
            table.description = description
            self.client.create_table(table, exists_ok=True)
        return existed

    @staticmethod
    def _exists(get) -> bool:
        try:
            get()
            return True
        except Exception:  # noqa: BLE001 - NotFound, or anything else that means "not usable yet"
            return False

    def insert(self, name: str, rows: Sequence[Mapping[str, Any]]) -> int:
        if not rows:
            return 0
        config = bigquery.LoadJobConfig(schema=list(SCHEMAS[name][1]),
                                        write_disposition=bigquery.WriteDisposition.WRITE_APPEND)
        job = self.client.load_table_from_json([dict(r) for r in rows], self.table(name), job_config=config)
        job.result()
        return job.output_rows or 0

    def read_facts(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if not run_id:
            rows, _ = self.query(
                "SELECT * FROM `%s` WHERE run_id = (SELECT run_id FROM `%s` ORDER BY update_time DESC LIMIT 1)"
                % (self.table(FACTS), self.table(FACTS))
            )
            return rows
        rows, _ = self.query("SELECT * FROM `%s` WHERE run_id = @run_id" % self.table(FACTS), run_id=run_id)
        return rows

    def query(self, sql: str, **params: Any) -> tuple[list[dict[str, Any]], float]:
        """Rows and wall time in ms, measured around the job."""
        config = bigquery.QueryJobConfig(query_parameters=[_param(k, v) for k, v in params.items()])
        started = time.perf_counter()
        rows = [dict(row.items()) for row in self.client.query(sql, job_config=config).result()]
        return rows, round((time.perf_counter() - started) * 1000.0, 1)

    def delete_run(self, run_id: str, kinds: Iterable[str] | None = None) -> None:
        for name in (FACTS, FACT_EMBEDDINGS):
            kind_clause = " AND kind IN UNNEST(@kinds)" if kinds is not None else ""
            params: dict[str, Any] = {"run_id": run_id}
            if kinds is not None:
                params["kinds"] = list(kinds)
            self.query("DELETE FROM `%s` WHERE run_id = @run_id%s" % (self.table(name), kind_clause), **params)

    def ensure_embedder(self, connection: str, endpoint: str) -> float:
        """Recreates the remote embedding model over the eval connection. Returns wall ms.

        The connection's service account needs roles/aiplatform.user on the project.
        """
        if not (_DDL_NAME_RE.fullmatch(connection) and _DDL_NAME_RE.fullmatch(endpoint)):
            raise ValueError("EVAL_BQ_CONNECTION or EVAL_EMBEDDING_MODEL has unexpected characters")
        _, ms = self.query("CREATE OR REPLACE MODEL `%s` REMOTE WITH CONNECTION `%s.%s` OPTIONS (ENDPOINT = '%s')"
                           % (self.table(EMBEDDER), self.project, connection, endpoint))
        return ms

    # -- the baselines -------------------------------------------------------

    def simple_facts(self, run_id: str, customer_id: str, standings: Iterable[str],
                     topics: Iterable[str], limit: int) -> tuple[list[dict[str, Any]], float]:
        """Tier 1 baseline: customer + topic, newest first. No topics means every topic."""
        sql = """
            SELECT memory_id, customer_id, standing, kind, fact, topics, source_time, update_time
            FROM `%s`
            WHERE run_id = @run_id AND customer_id = @customer_id AND standing IN UNNEST(@standings)
              AND (ARRAY_LENGTH(@topics) = 0 OR EXISTS (SELECT 1 FROM UNNEST(topics) t WHERE t IN UNNEST(@topics)))
            ORDER BY source_time DESC NULLS LAST, update_time DESC
            LIMIT @limit""" % self.table(FACTS)
        return self.query(sql, run_id=run_id, customer_id=customer_id, standings=list(standings),
                          topics=list(topics), limit=limit)

    def latest_notes(self, run_id: str, customer_id: str, limit: int) -> tuple[list[dict[str, Any]], float]:
        """End-to-end baseline: the customer's most recent raw turns."""
        sql = """
            SELECT session, channel, turn_index, author, text, timestamp
            FROM `%s`
            WHERE run_id = @run_id AND customer_id = @customer_id
            ORDER BY timestamp DESC, turn_index DESC
            LIMIT @limit""" % self.table(RAW_NOTES)
        return self.query(sql, run_id=run_id, customer_id=customer_id, limit=limit)

    def embed_facts(self, run_id: str, model: str, kinds: Iterable[str] | None = None) -> dict[str, Any]:
        """Embeds this run's facts with the remote model. Returns rows embedded and failed."""
        kind_clause = " AND kind IN UNNEST(@kinds)" if kinds is not None else ""
        sql = """
            INSERT INTO `{emb}` (run_id, memory_id, customer_id, standing, kind, fact, embedding, status)
            SELECT run_id, memory_id, customer_id, standing, kind, content, ml_generate_embedding_result,
                   ml_generate_embedding_status
            FROM ML.GENERATE_EMBEDDING(
              MODEL `{model}`,
              (SELECT run_id, memory_id, customer_id, standing, kind, fact AS content
               FROM `{facts}` WHERE run_id = @run_id{kinds}),
              STRUCT(TRUE AS flatten_json_output, 'RETRIEVAL_DOCUMENT' AS task_type))
        """.format(emb=self.table(FACT_EMBEDDINGS), model=self.table(model), facts=self.table(FACTS),
                   kinds=kind_clause)
        params: dict[str, Any] = {"run_id": run_id}
        if kinds is not None:
            params["kinds"] = list(kinds)
        _, ms = self.query(sql, **params)
        counts, _ = self.query(
            "SELECT COUNTIF(status = '') AS embedded, COUNTIF(status != '') AS failed, "
            "ANY_VALUE(IF(status != '', status, NULL)) AS sample_error FROM `%s` WHERE run_id = @run_id"
            % self.table(FACT_EMBEDDINGS), run_id=run_id)
        return {**counts[0], "latency_ms": ms}

    def vector_search(self, run_id: str, customer_id: str, standings: Iterable[str], query: str,
                      top_k: int, model: str) -> tuple[list[dict[str, Any]], float]:
        """Tier 2 baseline, first stage: cosine VECTOR_SEARCH within one customer's rows."""
        sql = """
            SELECT base.memory_id, base.customer_id, base.standing, base.kind, base.fact, distance
            FROM VECTOR_SEARCH(
              (SELECT * FROM `{emb}` WHERE run_id = @run_id AND customer_id = @customer_id
                 AND standing IN UNNEST(@standings) AND status = ''),
              'embedding',
              (SELECT ml_generate_embedding_result AS embedding
               FROM ML.GENERATE_EMBEDDING(MODEL `{model}`, (SELECT @query AS content),
                 STRUCT(TRUE AS flatten_json_output, 'RETRIEVAL_QUERY' AS task_type))),
              'embedding', top_k => {top_k}, distance_type => 'COSINE')
            ORDER BY distance""".format(emb=self.table(FACT_EMBEDDINGS), model=self.table(model),
                                        top_k=int(top_k))
        return self.query(sql, run_id=run_id, customer_id=customer_id, standings=list(standings), query=query)


def card_endings(client: bigquery.Client, project: str, governed_dataset: str,
                 customer_ids: Iterable[str]) -> dict[str, dict[str, str]]:
    """customer -> {last four: status} from the governed `cards` table. Read only."""
    sql = ("SELECT customer_id, last_four, status FROM `%s.%s.cards` WHERE customer_id IN UNNEST(@ids)"
           % (project, governed_dataset))
    config = bigquery.QueryJobConfig(query_parameters=[_param("ids", list(customer_ids))])
    endings: dict[str, dict[str, str]] = {}
    for row in client.query(sql, job_config=config).result():
        endings.setdefault(row["customer_id"], {})[str(row["last_four"])] = str(row["status"])
    return endings


class Ranker:
    """Tier 2 baseline, second stage: Vertex AI ranking API (discoveryengine rankingConfigs.rank)."""

    def __init__(self, project: str, model: str, timeout: int = 30):
        self.project = project
        self.model = model
        self.timeout = timeout
        self.url = ("https://discoveryengine.googleapis.com/v1/projects/%s/locations/global/"
                    "rankingConfigs/default_ranking_config:rank" % project)
        # Token source: backend/gcp_auth.py:BearerTokenSource (ADC)
        self._auth = BearerTokenSource(explicit_token=None, env_vars=())

    def rank(self, query: str, records: Sequence[tuple[str, str]],
             top_n: int) -> tuple[list[tuple[str, float | None]], float]:
        """(id, score) best first, and wall time in ms. Records are (id, text)."""
        if not records:
            return [], 0.0
        body = {"model": self.model, "query": query, "topN": top_n,
                "records": [{"id": rid, "content": text} for rid, text in records]}
        request = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"), method="POST")
        request.add_header("Authorization", "Bearer %s" % self._auth.get())
        request.add_header("Content-Type", "application/json")
        # User ADC needs a quota project for this API; bill it to the eval project.
        request.add_header("x-goog-user-project", self.project)
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        ms = round((time.perf_counter() - started) * 1000.0, 1)
        return [(r["id"], float(r.get("score", 0.0))) for r in payload.get("records", [])], ms

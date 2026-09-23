"""
Dataplex Universal Catalog -- the real Knowledge Catalog.

On `main`, `KnowledgeCatalog` was a class holding four `PolicyRule` literals and
two customer profiles, with `text-embedding-005` cosine similarity computed over
them in Python. It described itself as an "Enterprise Knowledge Catalog". It was
a dictionary with an embedding model bolted on, and the embedding was only used
to rank four items.

This module reads the catalog that actually exists in the project: business
glossary terms, BigQuery entries, and the governance aspects attached to them.

THE IDEA
    Governance is configuration, not code. There is no list of forbidden tables
    in this file and no list of restricted columns. Every verdict is derived at
    call time from aspects stored in Dataplex. Change `lifecycle-status` to
    DEPRECATED in the console and the desk refuses that table on the next
    request, with no code change and no deploy.

    That is the thing worth demonstrating: not "the agent refused", but
    "governance changed and the agent changed with it".

THE ASPECT CONTRACT
    The aspects deliberately separate the human label from the machine
    instruction:

        level:          RESTRICTED        <- what a person reads
        agent_behavior: REFUSE            <- what the agent executes

    This module acts only on `agent_behavior`. A label on its own stops nothing,
    and conflating the two is how a catalog ends up full of scary words that no
    system reads.

WHAT THIS IS NOT
    It is not an enforcement point. Dataplex is not in the query path and cannot
    block a query. It supplies the facts an agent needs in order to stop itself,
    and this module records whether it did. Hard enforcement is IAM, column-level
    security, and the tool surface. Any UI built on this must say "the agent
    declined", never "Dataplex blocked" -- see `CATALOG_ADVISORY_NOTE`.

REST surface used (no SDK, so the calls are legible in a trace):
    GET  /v1/{location}:lookupEntry?entry=...&view=ALL
    POST /v1/{location}:searchEntries
    GET  /v1/{glossary}/terms
    GET  /v1/{glossary}/terms/{term}

Reference: https://cloud.google.com/dataplex/docs/reference/rest
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import google.auth
import google.auth.transport.requests
import requests

from backend.config import Settings, settings as load_settings
from backend.provenance import NOT_RECORDED

log = logging.getLogger(__name__)

SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

CATALOG_ADVISORY_NOTE = (
    "Dataplex advises; it does not enforce. These verdicts are read from catalog "
    "aspects and honoured voluntarily by the agent. Nothing in the query path was "
    "blocked. Any interface presenting this must say the agent declined, never "
    "that the catalog prevented the read."
)

ASPECT_LIFECYCLE = "lifecycle-status"
ASPECT_SENSITIVITY = "sensitivity-class"
ASPECT_DOMAIN = "data-domain"
ASPECT_CRITICALITY = "criticality"
ASPECT_RETENTION = "retention-policy"


class CatalogError(RuntimeError):
    """A Dataplex call failed. Status and body preserved verbatim.

    The body matters. When `ListEntryLinks` is unavailable, Dataplex says
    `Method not visible to labels: {PUBLIC}` -- which is actionable. Collapsing
    that into "503 Service Unavailable" throws away the only useful part.
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
            "response_body": self.body[:2000],
            "url": self.url,
        }


def field_at(obj: Optional[dict], *path: str, default: Any = NOT_RECORDED) -> Any:
    """Read a nested field, or return `n/r`.

    The only place in this module where a missing value becomes printable, and
    it never invents one.
    """
    node: Any = obj
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    if node is None or node == "" or node == []:
        return default
    return node


# --------------------------------------------------------------------------
# Value types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GovernanceDecision:
    """What the catalog says about one table or column, and why."""

    resource: str
    kind: str  # "TABLE" or "COLUMN"
    allowed: bool
    action: str  # ALLOW / ALLOW_TO_OWNER_ONLY / REFUSE / UNKNOWN
    basis: str  # the aspect and value the verdict rests on
    rationale: str = NOT_RECORDED
    lifecycle_status: str = NOT_RECORDED
    sensitivity_level: str = NOT_RECORDED
    agent_guidance: str = NOT_RECORDED
    latency_ms: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "resource": self.resource,
            "kind": self.kind,
            "allowed": self.allowed,
            "action": self.action,
            "basis": self.basis,
            "rationale": self.rationale,
            "lifecycle_status": self.lifecycle_status,
            "sensitivity_level": self.sensitivity_level,
            "agent_guidance": self.agent_guidance,
            "latency_ms": self.latency_ms,
            "advisory_note": CATALOG_ADVISORY_NOTE,
        }


@dataclass(frozen=True)
class GlossaryTerm:
    """One business term, as the glossary defines it."""

    term_id: str
    display_name: str
    description: str
    category: str = NOT_RECORDED

    @classmethod
    def from_api(cls, payload: dict) -> "GlossaryTerm":
        name = payload.get("name", "")
        parent = payload.get("parent", "")
        return cls(
            term_id=name.rsplit("/", 1)[-1] if name else NOT_RECORDED,
            display_name=payload.get("displayName", NOT_RECORDED),
            description=payload.get("description", NOT_RECORDED),
            category=parent.rsplit("/", 1)[-1] if "/categories/" in parent else NOT_RECORDED,
        )

    def to_dict(self) -> dict:
        return {
            "term_id": self.term_id,
            "display_name": self.display_name,
            "description": self.description,
            "category": self.category,
        }


@dataclass
class Facet:
    """One independently-failing part of a catalog overview.

    Facets exist because the previous implementation put five unrelated reads in
    one `try`. Dataplex does not expose `ListEntryLinks` publicly, so that single
    unavailable method made the glossary, the governance verdicts and everything
    else return 503 -- reported to the UI as an empty catalog. An empty list and
    an unanswerable question are very different statements.
    """

    name: str
    ok: bool
    value: Any = None
    error: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "facet": self.name,
            "status": "OK" if self.ok else "UNAVAILABLE",
            "value": self.value if self.ok else NOT_RECORDED,
            "error": self.error,
        }


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class CatalogGovernor:
    """Reads Dataplex and turns aspects into agent-actionable verdicts."""

    def __init__(
        self,
        cfg: Optional[Settings] = None,
        credentials: Any = None,
        session: Optional[requests.Session] = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.cfg = cfg or load_settings()
        self.timeout_s = timeout_s
        self._session = session or requests.Session()
        self._credentials = credentials
        self._sibling_cache: dict[str, list[str]] = {}
        self.calls: list[str] = []

    # -- plumbing ----------------------------------------------------------

    @property
    def location_path(self) -> str:
        return f"projects/{self.cfg.project_id}/locations/{self.cfg.location}"

    def _base(self) -> str:
        return f"{self.cfg.dataplex_endpoint}/v1"

    def bigquery_entry_name(self, table: str) -> str:
        return (
            f"{self.location_path}/entryGroups/@bigquery/entries/"
            f"bigquery.googleapis.com/projects/{self.cfg.project_id}"
            f"/datasets/{self.cfg.bigquery_dataset}/tables/{table}"
        )

    def _token(self, force_refresh: bool = False) -> str:
        import threading
        if not hasattr(self, "_auth_lock"):
            self._auth_lock = threading.Lock()
        with self._auth_lock:
            if self._credentials is None:
                self._credentials, _ = google.auth.default(scopes=list(SCOPES))
            if force_refresh or not self._credentials.valid or not self._credentials.token:
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
        self.calls.append(f"{method} {url.split('/v1/')[-1][:110]}")
        log.debug("-> %s %s", method, url)
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
        }
        try:
            resp = self._session.request(
                method, url, headers=headers, json=body, params=params, timeout=self.timeout_s
            )
            if resp.status_code == 401:
                headers["Authorization"] = f"Bearer {self._token(force_refresh=True)}"
                resp = self._session.request(
                    method, url, headers=headers, json=body, params=params, timeout=self.timeout_s
                )
        except requests.RequestException as exc:
            raise CatalogError(f"{method} {url} failed: {exc}", url=url) from exc

        if resp.status_code >= 400:
            raise CatalogError(
                f"{method} {url} returned HTTP {resp.status_code}",
                status=resp.status_code,
                body=resp.text,
                url=url,
            )
        return resp.json() if resp.content else {}

    def _aspect(self, entry: dict, suffix: str) -> dict:
        """Find an aspect by its type suffix.

        Aspect keys are `{project_number}.{location}.{type}`, so matching on the
        suffix rather than reconstructing the whole key means this keeps working
        when the project number is not configured.
        """
        for key, value in (entry.get("aspects") or {}).items():
            if key.endswith(f".{suffix}"):
                return value.get("data", {}) or {}
        return {}

    def _column_aspect(self, entry: dict, column: str, suffix: str) -> dict:
        """Find a column-scoped aspect, keyed `...{suffix}@Schema.{column}`."""
        needle = f".{suffix}@Schema.{column}"
        for key, value in (entry.get("aspects") or {}).items():
            if key.endswith(needle):
                return value.get("data", {}) or {}
        return {}

    # -- entries -----------------------------------------------------------

    def lookup_table(self, table: str) -> dict:
        """Fetch a BigQuery table entry with all aspects."""
        return self._request(
            "GET",
            f"{self.location_path}:lookupEntry",
            params={"entry": self.bigquery_entry_name(table), "view": "ALL"},
        )

    def check_table(self, table: str) -> GovernanceDecision:
        """Ask the catalog whether this table may be read, and why.

        Two aspects can each refuse, and they refuse for different reasons:
        `lifecycle-status` (the table is deprecated) and `sensitivity-class`
        (the contents are restricted). Lifecycle is checked first because a
        deprecated table should not be read even if its contents are innocuous.
        """
        started = time.perf_counter()
        try:
            entry = self.lookup_table(table)
        except CatalogError as exc:
            return GovernanceDecision(
                resource=table,
                kind="TABLE",
                allowed=False,
                action="UNKNOWN",
                basis=f"catalog unreachable: HTTP {exc.status}",
                rationale=exc.body[:400],
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
            )

        lifecycle = self._aspect(entry, ASPECT_LIFECYCLE)
        sensitivity = self._aspect(entry, ASPECT_SENSITIVITY)
        elapsed = round((time.perf_counter() - started) * 1000, 1)

        status = lifecycle.get("status", NOT_RECORDED)
        guidance = lifecycle.get("agent_guidance", NOT_RECORDED)
        level = sensitivity.get("level", NOT_RECORDED)
        behavior = sensitivity.get("agent_behavior", "ALLOW")

        if status in ("DEPRECATED", "RETIRED", "QUARANTINED"):
            return GovernanceDecision(
                resource=table,
                kind="TABLE",
                allowed=False,
                action="REFUSE",
                basis=f"{ASPECT_LIFECYCLE}.status = {status}",
                rationale=guidance,
                lifecycle_status=status,
                sensitivity_level=level,
                agent_guidance=guidance,
                latency_ms=elapsed,
            )

        if behavior == "REFUSE":
            return GovernanceDecision(
                resource=table,
                kind="TABLE",
                allowed=False,
                action="REFUSE",
                basis=f"{ASPECT_SENSITIVITY}.agent_behavior = REFUSE",
                rationale=sensitivity.get("rationale", NOT_RECORDED),
                lifecycle_status=status,
                sensitivity_level=level,
                agent_guidance=guidance,
                latency_ms=elapsed,
            )

        return GovernanceDecision(
            resource=table,
            kind="TABLE",
            allowed=True,
            action=behavior if behavior != "ALLOW" else "ALLOW",
            basis=f"{ASPECT_LIFECYCLE}.status = {status}; {ASPECT_SENSITIVITY}.agent_behavior = {behavior}",
            rationale=sensitivity.get("rationale", NOT_RECORDED),
            lifecycle_status=status,
            sensitivity_level=level,
            agent_guidance=guidance,
            latency_ms=elapsed,
        )

    def check_column(self, table: str, column: str) -> GovernanceDecision:
        """Ask the catalog whether one column may be selected.

        Columns carry their own `sensitivity-class` aspect, which overrides the
        table's. That is the whole reason column-level aspects exist: a table can
        be readable while one field in it is not.
        """
        started = time.perf_counter()
        resource = f"{table}.{column}"
        try:
            entry = self.lookup_table(table)
        except CatalogError as exc:
            return GovernanceDecision(
                resource=resource,
                kind="COLUMN",
                allowed=False,
                action="UNKNOWN",
                basis=f"catalog unreachable: HTTP {exc.status}",
                rationale=exc.body[:400],
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
            )

        column_aspect = self._column_aspect(entry, column, ASPECT_SENSITIVITY)
        source = "column"
        if not column_aspect:
            column_aspect = self._aspect(entry, ASPECT_SENSITIVITY)
            source = "table (no column-level aspect)"

        behavior = column_aspect.get("agent_behavior", "ALLOW")
        level = column_aspect.get("level", NOT_RECORDED)
        elapsed = round((time.perf_counter() - started) * 1000, 1)

        return GovernanceDecision(
            resource=resource,
            kind="COLUMN",
            allowed=behavior != "REFUSE",
            action=behavior,
            basis=f"{ASPECT_SENSITIVITY}.agent_behavior = {behavior} (from {source})",
            rationale=column_aspect.get("rationale", NOT_RECORDED),
            sensitivity_level=level,
            latency_ms=elapsed,
        )

    def discover_sibling_tables(self, term: str, refresh: bool = False) -> list[str]:
        """Search the catalog for tables matching a term.

        This is what makes a governance demo honest. The agent is not handed a
        list of tables including the deprecated one; it *searches*, gets back
        whatever exists, and then judges each result on its aspects. The trap is
        discovered, not named.

        `searchEntries` returns more than tables -- a search for "authorizations"
        also matches glossary terms whose descriptions contain the word -- so
        results are filtered to BigQuery tables and views.
        """
        if term in self._sibling_cache and not refresh:
            return self._sibling_cache[term]

        payload = self._request(
            "POST",
            f"{self.location_path}:searchEntries",
            body={"query": term, "pageSize": 25},
        )
        tables: list[str] = []
        for result in payload.get("results", []):
            # Each hit nests the entry under `dataplexEntry`; `entryType` is not
            # at the top level. Reading it from the wrong level matches nothing
            # and yields an empty list, which reads as "there are no siblings"
            # rather than "the parser is wrong" -- so this is asserted by a test.
            entry = result.get("dataplexEntry", result)
            entry_type = entry.get("entryType", "")
            if not entry_type.endswith(("bigquery-table", "bigquery-view")):
                continue
            name = entry.get("name", "") or result.get("linkedResource", "")
            if "/tables/" not in name:
                continue
            table = name.rsplit("/tables/", 1)[-1]
            if f"/datasets/{self.cfg.bigquery_dataset}/" in name and table not in tables:
                tables.append(table)
        self._sibling_cache[term] = tables
        return tables

    # -- glossary ----------------------------------------------------------

    def list_terms(self, page_size: int = 100) -> list[GlossaryTerm]:
        """Every business term in the glossary, paged to exhaustion."""
        terms: list[GlossaryTerm] = []
        params: dict[str, Any] = {"pageSize": page_size}
        token: Optional[str] = None
        while True:
            if token:
                params["pageToken"] = token
            payload = self._request("GET", f"{self.cfg.glossary_resource}/terms", params=params)
            terms.extend(GlossaryTerm.from_api(t) for t in payload.get("terms", []))
            token = payload.get("nextPageToken")
            if not token:
                return terms

    def get_term(self, term_id: str) -> GlossaryTerm:
        """One term by id.

        This is where governed answers come from. When the desk is asked why a
        credit card dispute has no provisional credit date, the answer is in the
        `provisional-credit` term text -- retrieved from agreed vocabulary, not
        reasoned out by a model and not written into a prompt.
        """
        payload = self._request("GET", f"{self.cfg.glossary_resource}/terms/{term_id}")
        return GlossaryTerm.from_api(payload)

    def search_terms(self, query: str, top_k: int = 5) -> list[GlossaryTerm]:
        """Rank glossary terms against a query.

        Deliberately a plain token-overlap score over live term text rather than
        an embedding call. The previous implementation used `text-embedding-005`
        to rank **four hardcoded policies**, which spent a network round trip to
        sort a list that could not be wrong. Here the corpus is real and the
        ranking is transparent; if this needs to be semantic later, it should be
        a catalog-side search, not a local cosine over everything we downloaded.
        """
        terms = self.list_terms()
        tokens = {t for t in query.lower().split() if len(t) > 2}
        if not tokens:
            return terms[:top_k]

        scored: list[tuple[float, GlossaryTerm]] = []
        for term in terms:
            haystack = f"{term.display_name} {term.description} {term.term_id}".lower()
            hits = sum(1 for t in tokens if t in haystack)
            if hits:
                scored.append((hits / len(tokens), term))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [term for _, term in scored[:top_k]]

    # -- composite ---------------------------------------------------------

    def overview(self, tables: Optional[list[str]] = None) -> dict:
        """A catalog summary whose parts fail independently.

        Each facet is attempted separately. One unavailable method degrades the
        response to PARTIAL and names itself; it does not take the working
        facets down with it.
        """
        facets: list[Facet] = []

        try:
            terms = self.list_terms()
            facets.append(
                Facet(
                    "glossary",
                    True,
                    {
                        "glossary": self.cfg.dataplex_glossary_id,
                        "term_count": len(terms),
                        "categories": sorted({t.category for t in terms if t.category != NOT_RECORDED}),
                        "sample": [t.to_dict() for t in terms[:8]],
                    },
                )
            )
        except CatalogError as exc:
            facets.append(Facet("glossary", False, error=exc.to_dict()))

        probe_tables = tables or ["disputes", "customers", "cards", "authorizations"]
        try:
            decisions = [self.check_table(t).to_dict() for t in probe_tables]
            facets.append(Facet("table_governance", True, decisions))
        except CatalogError as exc:
            facets.append(Facet("table_governance", False, error=exc.to_dict()))

        try:
            discovered = self.discover_sibling_tables("authorizations")
            verdicts = [self.check_table(t).to_dict() for t in discovered]
            facets.append(
                Facet(
                    "discovery",
                    True,
                    {
                        "search_term": "authorizations",
                        "discovered": discovered,
                        "refused": [v["resource"] for v in verdicts if not v["allowed"]],
                        "verdicts": verdicts,
                    },
                )
            )
        except CatalogError as exc:
            facets.append(Facet("discovery", False, error=exc.to_dict()))

        ok = sum(1 for f in facets if f.ok)
        return {
            "status": "OK" if ok == len(facets) else ("PARTIAL" if ok else "UNAVAILABLE"),
            "facets_ok": ok,
            "facets_total": len(facets),
            "advisory_note": CATALOG_ADVISORY_NOTE,
            "facets": [f.to_dict() for f in facets],
            "api_calls": list(self.calls),
        }


def create_default_catalog_governor() -> CatalogGovernor:
    """Build a governor from the process configuration."""
    return CatalogGovernor()

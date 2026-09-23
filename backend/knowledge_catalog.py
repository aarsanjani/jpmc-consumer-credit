"""
knowledge_catalog.py - Dataplex Universal Catalog as the agent's policy source.

WHAT THIS IS
    A dependency-light REST client for the Dataplex Catalog API, plus a thin
    decision layer that turns catalog metadata into an allow / refuse answer an
    agent can act on and a reviewer can audit.

WHY IT EXISTS AS A SEPARATE FILE
    Different service (dataplex.googleapis.com, not aiplatform), different
    resource model, different lifecycle. Memory Bank answers "what do we know
    about this customer". The catalog answers "is this agent allowed to touch
    this data, and what does this term mean". Those are two jobs and they
    change for different reasons.

THE IDEA THIS FILE IS BUILT ON
    Governance is configuration, not code. Nothing in here contains a list of
    forbidden tables or restricted columns. Every decision is derived at call
    time from aspects stored in the catalog. Change `lifecycle-status` to
    DEPRECATED in the console and the agent starts refusing that table on the
    next run, with no code change and no redeploy.

    That is the demo. Not "the agent refused", but "governance changed and the
    agent changed with it".

WHAT THIS DELIBERATELY DOES NOT DO
    No hardcoded policy list. No local embeddings or cosine similarity over a
    Python array of rules. No dict of pre-baked BigQuery rows. If the catalog
    has not been set up, these calls fail loudly rather than falling back to
    invented metadata that makes a broken demo look healthy.

    It also does not claim to BLOCK anything. Dataplex is not in the query
    path; it cannot stop a query. It supplies the facts an agent needs to stop
    itself, and this file records whether it did. Hard enforcement is IAM,
    column-level security and the tool surface - see authorize() for where the
    two meet.

THE ASPECT CONTRACT THIS READS
    The kit's aspect types deliberately separate the human label from the
    machine instruction:

        level:          RESTRICTED        <- what a person reads
        agent_behavior: REFUSE            <- what the agent executes

    This file only ever acts on `agent_behavior`. A label alone stops nothing.

API SURFACE USED (all REST, no SDK)
    GET  /v1/{location}:lookupEntry?entry=...&view=ALL   read a BigQuery entry
    POST /v1/{location}:searchEntries                    find entries
    GET  /v1/{glossary}/terms/{term}                     one business term
    GET  /v1/{glossary}/terms                            all terms
    GET  /v1/{entryGroup}/entryLinks                     term <-> column links

    All paths mirror exactly what scripts/setup_catalog.py writes.

Reference:
    https://cloud.google.com/dataplex/docs/reference/rest
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Dict, List, Optional, Sequence

# Printed when the catalog did not return a value. Never a default, never a
# guess, never a zero. Same rule as memory_bank.py.
NOT_RECORDED = "n/r"

DATAPLEX_HOST = "dataplex.googleapis.com"
DEFAULT_LOCATION = "us-central1"
DEFAULT_ENTRY_GROUP = "@bigquery"
DEFAULT_TIMEOUT = 30


class CatalogError(RuntimeError):
    """An API call failed. Status and body, unedited."""

    def __init__(self, status: int, url: str, body: str):
        self.status = status
        self.url = url
        self.body = body
        super().__init__("HTTP %s from %s\n%s" % (status, url, body))


def field(obj: Optional[Dict[str, Any]], *path: str, default: Any = NOT_RECORDED) -> Any:
    """Reads a nested field or returns "n/r". The only place a missing value
    becomes printable, and it never invents one."""
    node: Any = obj
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    if node is None or node == "" or node == []:
        return default
    return node


# ---------------------------------------------------------------------------
# The REST client
# ---------------------------------------------------------------------------


class DataplexCatalogClient:
    """Dataplex Universal Catalog, spoken to directly over HTTPS.

    Auth is a bearer token: the access_token argument, then the
    CATALOG_TOKEN environment variable, then google.auth default credentials.
    Identical to the Memory Bank client, because it is the same OAuth scope
    against a different service.
    """

    def __init__(
        self,
        project: str,
        location: str = DEFAULT_LOCATION,
        dataset: Optional[str] = None,
        glossary_id: Optional[str] = None,
        entry_group: str = DEFAULT_ENTRY_GROUP,
        access_token: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        verbose: bool = True,
    ):
        if not project:
            raise ValueError("project is required.")
        self.project = project
        self.location = location
        self.dataset = dataset
        self.glossary_id = glossary_id
        self.entry_group = entry_group
        self.timeout = timeout
        self.verbose = verbose
        self._token = access_token
        self.calls: List[str] = []

    # -- names, mirroring scripts/setup_catalog.py -------------------------

    @property
    def location_path(self) -> str:
        return "projects/%s/locations/%s" % (self.project, self.location)

    @property
    def glossary_path(self) -> str:
        if not self.glossary_id:
            raise ValueError("glossary_id was not set on this client.")
        return "%s/glossaries/%s" % (self.location_path, self.glossary_id)

    def bigquery_entry_name(self, table: str, dataset: Optional[str] = None) -> str:
        """The catalog entry name for a BigQuery table.

        Exactly the shape setup_catalog.py writes, so what we read back is what
        the kit created.
        """
        dataset = dataset or self.dataset
        if not dataset:
            raise ValueError("dataset is required to address a BigQuery table.")
        return (
            "%s/entryGroups/@bigquery/entries/bigquery.googleapis.com/"
            "projects/%s/datasets/%s/tables/%s"
            % (self.location_path, self.project, dataset, table)
        )

    # -- plumbing ----------------------------------------------------------

    def token(self) -> str:
        if self._token:
            return self._token
        env = os.environ.get("CATALOG_TOKEN") or os.environ.get("MEMORY_BANK_TOKEN")
        if env:
            self._token = env
            return env
        try:
            import google.auth
            from google.auth.transport.requests import Request

            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            creds.refresh(Request())
            self._token = creds.token
            return self._token
        except Exception as exc:  # noqa: BLE001
            raise CatalogError(
                401, "(token)",
                "No credential available. Pass access_token=, set CATALOG_TOKEN, "
                "or install google-auth and run "
                "`gcloud auth application-default login`. Cause: %s" % exc,
            )

    def _request(self, method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url=url, data=payload, method=method)
        request.add_header("Authorization", "Bearer %s" % self.token())
        request.add_header("Content-Type", "application/json")

        self.calls.append("%s %s" % (method, url.split("/v1/")[-1][:110]))
        if self.verbose:
            print("  %-5s %s" % (method, url.replace("https://" + DATAPLEX_HOST, "")[:120]))

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise CatalogError(exc.code, url, exc.read().decode("utf-8", "replace"))
        except urllib.error.URLError as exc:
            raise CatalogError(0, url, "Network error: %s" % exc.reason)

        return json.loads(text) if text.strip() else {}

    # -- reads -------------------------------------------------------------

    def lookup_entry(self, entry_name: str, view: str = "ALL") -> Dict[str, Any]:
        """Reads one catalog entry with its aspects.

        BigQuery entries live in the system-owned @bigquery entry group, so
        they are read through :lookupEntry rather than a direct GET.
        """
        url = "https://%s/v1/%s:lookupEntry?%s" % (
            DATAPLEX_HOST,
            self.location_path,
            urllib.parse.urlencode({"entry": entry_name, "view": view}),
        )
        return self._request("GET", url)

    def get_entry(self, entry_name: str, view: str = "ALL") -> Dict[str, Any]:
        """Direct read, for entries in a group we own rather than @bigquery."""
        url = "https://%s/v1/%s?view=%s" % (DATAPLEX_HOST, entry_name, view)
        return self._request("GET", url)

    def search_entries(self, query: str, page_size: int = 20) -> List[Dict[str, Any]]:
        """Catalog search. This is the call that surfaces the deprecated trap:
        searching "authorizations" returns both tables, and only the aspects
        tell you which one is current."""
        url = "https://%s/v1/projects/%s/locations/global:searchEntries" % (
            DATAPLEX_HOST, self.project,
        )
        body = {"query": query, "pageSize": page_size}
        return self._request("POST", url, body).get("results", [])

    def get_term(self, term_id: str) -> Dict[str, Any]:
        url = "https://%s/v1/%s/terms/%s" % (DATAPLEX_HOST, self.glossary_path, term_id)
        return self._request("GET", url)

    def list_terms(self, page_size: int = 100) -> List[Dict[str, Any]]:
        url = "https://%s/v1/%s/terms?pageSize=%d" % (
            DATAPLEX_HOST, self.glossary_path, page_size,
        )
        return self._request("GET", url).get("terms", [])

    def list_entry_links(self, entry_group: Optional[str] = None) -> List[Dict[str, Any]]:
        """Term-to-column links. This is what connects the business word
        'provisional credit' to the actual column that holds it."""
        group = entry_group or self.entry_group
        url = "https://%s/v1/%s/entryGroups/%s/entryLinks" % (
            DATAPLEX_HOST, self.location_path, group,
        )
        return self._request("GET", url).get("entryLinks", [])

    # -- aspect access -----------------------------------------------------

    def aspects_for(self, entry: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """The aspects map off an entry, keyed as the API returns it:
        '{project}.{location}.{aspect-type}' for a table-level aspect, with an
        '@Schema.{column}' suffix for a column-level one."""
        return entry.get("aspects", {}) or {}

    def aspect_data(self, entry: Dict[str, Any], aspect_type: str,
                    column: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Finds one aspect's `data` payload, table-level or column-level.

        Matches on the aspect type suffix rather than rebuilding the full key,
        because the project and location prefix varies by deployment and we do
        not want this to break when the kit is installed elsewhere.
        """
        wanted_suffix = aspect_type if column is None else "%s@Schema.%s" % (aspect_type, column)
        for key, value in self.aspects_for(entry).items():
            if key.endswith(wanted_suffix):
                return value.get("data", {})
        return None


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    """One governance decision, with the evidence that produced it.

    `evidence` is the point. A refusal an agent cannot justify is indis-
    tinguishable from a refusal it invented, so every decision names the
    aspect and field it came from.
    """

    allowed: bool
    action: str                      # ALLOW | REFUSE | MASK | UNKNOWN
    subject: str                     # the table or table.column asked about
    reason: str = NOT_RECORDED
    evidence: str = NOT_RECORDED     # which aspect and field decided it
    replacement: Optional[str] = None
    regulation: str = NOT_RECORDED

    def as_trace_line(self) -> str:
        kind = "out" if self.allowed else "deny"
        return "%-5s %-44s %s" % (kind, self.subject, self.reason)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "subject": self.subject,
            "reason": self.reason,
            "evidence": self.evidence,
            "replacement": self.replacement,
            "regulation": self.regulation,
        }


class CatalogGovernor:
    """The policy decision point the agent consults before touching data.

    The agent asks; this class answers from live catalog metadata; the agent
    obeys and the decision is logged. Nothing here has an opinion of its own -
    remove the aspects from the catalog and every answer becomes UNKNOWN,
    which is the honest result and not a silent ALLOW.
    """

    LIFECYCLE_ASPECT = "lifecycle-status"
    SENSITIVITY_ASPECT = "sensitivity-class"

    def __init__(self, client: DataplexCatalogClient, strict: bool = True):
        self.client = client
        # strict: an entry with no governing aspect is refused rather than
        # allowed. Fail closed. Flip it only to show what the other choice costs.
        self.strict = strict
        self.decisions: List[Decision] = []

    def _record(self, decision: Decision) -> Decision:
        self.decisions.append(decision)
        return decision

    # -- table level -------------------------------------------------------

    def check_table(self, table: str, dataset: Optional[str] = None) -> Decision:
        """Is this table fit to use? Answered by its lifecycle-status aspect."""
        entry_name = self.client.bigquery_entry_name(table, dataset)
        try:
            entry = self.client.lookup_entry(entry_name)
        except CatalogError as error:
            return self._record(Decision(
                allowed=False, action="REFUSE", subject=table,
                reason="Not in the catalog, so it is not governed data (HTTP %s)." % error.status,
                evidence="lookupEntry failed",
            ))

        data = self.client.aspect_data(entry, self.LIFECYCLE_ASPECT)
        if data is None:
            return self._record(Decision(
                allowed=not self.strict,
                action="REFUSE" if self.strict else "ALLOW",
                subject=table,
                reason="No lifecycle-status aspect. Ungoverned data is refused in strict mode."
                       if self.strict else "No lifecycle-status aspect; strict mode is off.",
                evidence="aspect %s absent" % self.LIFECYCLE_ASPECT,
            ))

        status = field(data, "status")
        replacement = field(data, "authoritative_replacement", default=None)
        if status in ("DEPRECATED", "RETIRED", "QUARANTINED"):
            return self._record(Decision(
                allowed=False, action="REFUSE", subject=table,
                reason="lifecycle %s%s" % (
                    status, " - use %s instead" % replacement if replacement else ""),
                evidence="%s.status = %s" % (self.LIFECYCLE_ASPECT, status),
                replacement=replacement,
            ))

        return self._record(Decision(
            allowed=True, action="ALLOW", subject=table,
            reason="lifecycle %s" % status,
            evidence="%s.status = %s" % (self.LIFECYCLE_ASPECT, status),
        ))

    # -- column level ------------------------------------------------------

    def check_column(self, table: str, column: str,
                     dataset: Optional[str] = None) -> Decision:
        """May the agent read this column, and may it print the value?

        Acts on `agent_behavior`, never on `level`. The label is for people.
        """
        subject = "%s.%s" % (table, column)
        entry_name = self.client.bigquery_entry_name(table, dataset)
        try:
            entry = self.client.lookup_entry(entry_name)
        except CatalogError as error:
            return self._record(Decision(
                allowed=False, action="REFUSE", subject=subject,
                reason="Entry not readable (HTTP %s)." % error.status,
                evidence="lookupEntry failed",
            ))

        data = self.client.aspect_data(entry, self.SENSITIVITY_ASPECT, column=column)
        evidence_scope = "%s@Schema.%s" % (self.SENSITIVITY_ASPECT, column)
        if data is None:
            table_data = self.client.aspect_data(entry, self.SENSITIVITY_ASPECT, column=None)
            if table_data is not None:
                data = table_data
                evidence_scope = "%s (table default)" % self.SENSITIVITY_ASPECT
            else:
                return self._record(Decision(
                    allowed=not self.strict,
                    action="UNKNOWN" if not self.strict else "REFUSE",
                    subject=subject,
                    reason="No sensitivity-class aspect on this column.",
                    evidence="aspect %s@Schema.%s absent" % (self.SENSITIVITY_ASPECT, column),
                ))

        behavior = field(data, "agent_behavior")
        level = field(data, "level")
        allowed = behavior not in ("REFUSE", "MASK")
        return self._record(Decision(
            allowed=allowed,
            action=behavior if behavior != NOT_RECORDED else "UNKNOWN",
            subject=subject,
            reason=field(data, "rationale", default="governed by %s (%s)" % (evidence_scope, level)),
            evidence="%s.agent_behavior = %s (level %s)" % (
                evidence_scope, behavior, level),
            replacement=field(data, "safe_alternative_column", default=None),
            regulation=field(data, "regulation_reference"),
        ))

    # -- composite ---------------------------------------------------------

    def authorize(self, table: str, columns: Sequence[str] = (),
                  dataset: Optional[str] = None) -> Dict[str, Any]:
        """The call an agent makes before it queries anything.

        Returns a verdict plus every decision that fed it, so the trace can
        show the reasoning rather than just the outcome.
        """
        table_decision = self.check_table(table, dataset)
        column_decisions = [self.check_column(table, c, dataset) for c in columns]
        blocked = [d for d in column_decisions if not d.allowed]

        return {
            "table": table,
            "allowed": table_decision.allowed and not blocked,
            "table_decision": table_decision.to_dict(),
            "column_decisions": [d.to_dict() for d in column_decisions],
            "blocked_columns": [d.subject for d in blocked],
            "note": (
                "The catalog does not block this query; it supplies the basis "
                "for the agent to decline. Hard enforcement is IAM, column-level "
                "security and the tool surface."
            ),
        }

    # -- meaning, not permission -------------------------------------------

    def lookup_term(self, term_id: str) -> Dict[str, Any]:
        """What does this business word mean, and who owns the definition?

        This is the other half of the catalog's job. `check_*` decides what the
        agent may touch; this decides what the answer means.
        """
        try:
            term = self.client.get_term(term_id)
        except CatalogError as error:
            return {"term": term_id, "found": False,
                    "error": "HTTP %s" % error.status, "definition": NOT_RECORDED}
        return {
            "term": term_id,
            "found": True,
            "display_name": field(term, "displayName"),
            "definition": field(term, "description"),
            "name": field(term, "name"),
        }


# ---------------------------------------------------------------------------
# The demo
# ---------------------------------------------------------------------------


def run_governance_demo(governor: CatalogGovernor, dataset: Optional[str] = None) -> Dict[str, Any]:
    """The two traps, answered from live catalog metadata.

    Nothing below names a forbidden table or a restricted column as a literal.
    The verdicts come from aspects read at call time, which is the difference
    between a demo of governance and a demo of an if-statement.
    """
    client = governor.client
    results: Dict[str, Any] = {"steps": []}

    print("\n" + "=" * 74)
    print("CATALOG GOVERNANCE - every verdict below is read, not hardcoded")
    print("=" * 74)

    # 1. Search is where the trap is set.
    print("\n[1] Searching the catalog for \"authorizations\".")
    try:
        hits = client.search_entries("authorizations")
        for hit in hits[:6]:
            name = field(hit, "dataplexEntry", "name", default=field(hit, "name"))
            print("      %s" % str(name).split("/")[-1])
        print("      %d result(s). Nothing in the names says which is current." % len(hits))
        results["steps"].append({"step": "search", "count": len(hits)})
    except CatalogError as error:
        print("      search unavailable: HTTP %s" % error.status)
        results["steps"].append({"step": "search", "error": error.status})

    # 2. The deprecated table.
    print("\n[2] Asking the catalog about each one.")
    for table in ("authorizations", "authorizations_deprecated_v1"):
        decision = governor.check_table(table, dataset)
        print("      %s" % decision.as_trace_line())
        print("            evidence: %s" % decision.evidence)
        if decision.replacement:
            print("            use instead: %s" % decision.replacement)
        results["steps"].append({"step": "check_table", **decision.to_dict()})

    # 3. The restricted columns.
    print("\n[3] Asking about the two columns the agent must never print.")
    for table, column in (("customers", "ssn_last4"), ("cards", "pan_full"),
                          ("customers", "segment")):
        decision = governor.check_column(table, column, dataset)
        print("      %s" % decision.as_trace_line())
        print("            evidence: %s" % decision.evidence)
        if decision.regulation != NOT_RECORDED:
            print("            regulation: %s" % decision.regulation)
        results["steps"].append({"step": "check_column", **decision.to_dict()})

    # 4. The composite call an agent actually makes.
    print("\n[4] What the agent asks before running a query.")
    verdict = governor.authorize("customers", ["customer_id", "ssn_last4"], dataset)
    print("      allowed: %s | blocked: %s" % (verdict["allowed"], verdict["blocked_columns"]))
    print("      %s" % verdict["note"])
    results["steps"].append({"step": "authorize", **verdict})

    print("""
WHAT TO SAY WHILE THIS RUNS
  No table name and no column name in this file is marked forbidden. Every
  verdict came from an aspect read a second ago.

  So change lifecycle-status on `authorizations` to DEPRECATED in the console
  and re-run: the agent refuses the table it was using a minute earlier. No
  code change, no redeploy, no prompt edit. Governance moved and the agent
  moved with it.

  That is the whole argument for a catalog. Everything else is a data
  dictionary with better marketing.
""")
    return results


# ---------------------------------------------------------------------------
# Factory Helper + Backward Compatibility Bridge
# ---------------------------------------------------------------------------


def create_default_catalog_governor(strict: bool = True, verbose: bool = False) -> CatalogGovernor:
    """Creates a live DataplexCatalogClient + CatalogGovernor from environment variables."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "abhinayboda-genai-bb")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", DEFAULT_LOCATION)
    dataset = os.environ.get("BQ_DATASET", "jpmc_ccb_fraud")
    glossary_id = os.environ.get("DATAPLEX_GLOSSARY_ID", "ccb-fraud-disputes")
    client = DataplexCatalogClient(
        project=project,
        location=location,
        dataset=dataset,
        glossary_id=glossary_id,
        verbose=verbose,
    )
    return CatalogGovernor(client=client, strict=strict)


try:
    from backend.knowledge_catalog_old import (  # noqa: E402, F401
        KnowledgeCatalog,
        AWSOnPremHybridConnector,
    )
except Exception:  # pragma: no cover
    pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dataplex Universal Catalog as the agent's policy source.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples
  python knowledge_catalog.py --project P --dataset jpmc_ccb_fraud govern
  python knowledge_catalog.py --project P --dataset jpmc_ccb_fraud \\
      table --name authorizations_deprecated_v1
  python knowledge_catalog.py --project P --dataset jpmc_ccb_fraud \\
      column --table customers --name ssn_last4
  python knowledge_catalog.py --project P --glossary ccb-fraud-disputes \\
      term --id provisional-credit
  python knowledge_catalog.py --project P search --query authorizations
""",
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--dataset", default=os.environ.get("BQ_DATASET"))
    parser.add_argument("--glossary", default=os.environ.get("DATAPLEX_GLOSSARY_ID"))
    parser.add_argument("--access-token", default=None)
    parser.add_argument("--lenient", action="store_true",
                        help="Allow entries that carry no governing aspect. Off by default: "
                             "ungoverned data should fail closed.")
    parser.add_argument("--quiet", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("govern", help="Run the full governance demo.")

    p_table = sub.add_parser("table", help="Check one table.")
    p_table.add_argument("--name", required=True)

    p_col = sub.add_parser("column", help="Check one column.")
    p_col.add_argument("--table", required=True)
    p_col.add_argument("--name", required=True)

    p_term = sub.add_parser("term", help="Look up a glossary term.")
    p_term.add_argument("--id", required=True)

    p_terms = sub.add_parser("terms", help="List glossary terms.")

    p_search = sub.add_parser("search", help="Search the catalog.")
    p_search.add_argument("--query", required=True)

    p_entry = sub.add_parser("entry", help="Dump one entry's aspects, raw.")
    p_entry.add_argument("--table", required=True)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    client = DataplexCatalogClient(
        project=args.project,
        location=args.location,
        dataset=args.dataset,
        glossary_id=args.glossary,
        access_token=args.access_token,
        verbose=not args.quiet,
    )
    governor = CatalogGovernor(client, strict=not args.lenient)

    try:
        if args.command == "govern":
            run_governance_demo(governor, args.dataset)

        elif args.command == "table":
            decision = governor.check_table(args.name)
            print("\n%s" % decision.as_trace_line())
            print("  evidence:    %s" % decision.evidence)
            print("  regulation:  %s" % decision.regulation)
            print("  replacement: %s" % (decision.replacement or NOT_RECORDED))

        elif args.command == "column":
            decision = governor.check_column(args.table, args.name)
            print("\n%s" % decision.as_trace_line())
            print("  action:     %s" % decision.action)
            print("  evidence:   %s" % decision.evidence)
            print("  regulation: %s" % decision.regulation)

        elif args.command == "term":
            info = governor.lookup_term(args.id)
            print("\n%s" % info.get("display_name", NOT_RECORDED))
            print("  %s" % info.get("definition", NOT_RECORDED))

        elif args.command == "terms":
            terms = client.list_terms()
            print("\n%d term(s)" % len(terms))
            for term in terms:
                print("  %-34s %s" % (
                    str(field(term, "name")).split("/")[-1], field(term, "displayName")))

        elif args.command == "search":
            hits = client.search_entries(args.query)
            print("\n%d result(s)" % len(hits))
            for hit in hits:
                print("  %s" % field(hit, "dataplexEntry", "name", default=field(hit, "name")))

        elif args.command == "entry":
            entry = client.lookup_entry(client.bigquery_entry_name(args.table))
            aspects = client.aspects_for(entry)
            print("\n%d aspect(s) on %s" % (len(aspects), args.table))
            for key, value in aspects.items():
                print("\n  %s" % key)
                for field_name, field_value in (value.get("data") or {}).items():
                    print("      %-26s %s" % (field_name, field_value))

    except CatalogError as error:
        print("\nCatalog call failed.\n%s" % error, file=sys.stderr)
        print("\nIf this is a 404, the catalog has not been set up in this project. "
              "Run scripts/setup_catalog.py first. This file will not invent "
              "metadata to cover for that.", file=sys.stderr)
        return 1
    except ValueError as error:
        print("\n%s" % error, file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

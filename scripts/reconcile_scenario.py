#!/usr/bin/env python3
"""reconcile_scenario.py - prove memory is not quoting numbers BigQuery owns.

WHAT IT CHECKS
    Two things, in this order:

    1. Does any memory in the customer's scope assert an amount, a deadline or
       a dispute id at all? That is a rule violation on its own, whether or not
       the value happens to be right today.
    2. For each value memory does assert, what does BigQuery actually say?
       BigQuery is authoritative. A stored memory quoting "EUR 412" against a
       disputes row of USD 1,284.60 is the failure this script exists to catch.

    Exits non-zero on a mismatch, and on a rule violation with no mismatch,
    because "memory asserted the right number" is still the wrong design - it
    is right until the ledger moves, and nothing tells anyone when it does.

HOW IT READS BIGQUERY
    Through the existing golden-query layer in `backend/adk_agents.py`
    (`golden_07_open_disputes_for_customer`, then `golden_08_dispute_filing_
    window_check` for each dispute id that 07 returned). No ad-hoc SQL, no
    second BigQuery client, no dispute id typed into this file.

OFFLINE
    `--dry-run` prints the exact contracts, parameters and comparisons it would
    run, and leaves every authoritative value as "n/r". It does not guess what
    BigQuery would have said. If you have a captured result, pass it with
    `--bq-fixture` and the comparison runs for real with no GCP access.

USAGE
    python3 scripts/reconcile_scenario.py --dry-run
    python3 scripts/reconcile_scenario.py --dry-run --legacy-blob
    python3 scripts/reconcile_scenario.py --dry-run --legacy-blob --bq-fixture rows.json
    python3 scripts/reconcile_scenario.py --customer CUST00001
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.provenance import (  # noqa: E402
    NOT_RECORDED,
    computed,
    live,
    seeded,
    summarize,
    unmeasured,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_facts import scan_profile  # noqa: E402

SOURCE = "scripts/reconcile_scenario.py"

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_CONFIG = 2
EXIT_RULE_VIOLATION = 3


# ---------------------------------------------------------------------------
# The memory side
# ---------------------------------------------------------------------------

# What the pre-fix narrative blob asserted, recorded so the reconciliation can
# be run and reviewed without a live engine. These are NOT truth and are never
# printed as an authoritative value - they are the left-hand side of the
# comparison, the claims under suspicion.
LEGACY_BLOB_CLAIMS: List[Dict[str, str]] = [
    {
        "claim": "EUR 412",
        "kind": "amount",
        "authority": "disputes row for this customer (amount + currency columns), via golden query 07",
    },
    {
        "claim": "EUR 1480",
        "kind": "amount",
        "authority": "disputes row for this customer (amount + currency columns), via golden query 07",
    },
    {
        "claim": "90 days",
        "kind": "window",
        "authority": "filing-window / provisional-credit result for the dispute, via golden query 08",
    },
]

LEGACY_SOURCE = ("pre-fix Memory Bank narrative blob for the demo customer, as recorded "
                 "in the memory-layer defect report")

# Row keys differ between the golden .sql files and the demo models, and this
# script must not invent a schema. Each value is tried in order; the first key
# present on the row wins and is printed, and if none is present the value
# renders as "n/r" and the comparison is reported UNVERIFIABLE rather than
# scored as a pass.
FIELD_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "dispute_id": ("dispute_id", "disputeId", "id"),
    "amount": ("disputed_amount_usd", "disputed_amount", "amount_usd", "amount",
               "transaction_amount", "dispute_amount"),
    "currency": ("currency", "currency_code", "iso_currency"),
    "deadline": ("provisional_credit_due_date", "provisional_credit_due",
                 "due_date", "deadline", "resolution_due_date"),
    "window_days": ("investigation_window_days", "window_days", "max_days",
                    "resolution_days"),
}


def resolve(row: Dict[str, Any], field: str) -> Tuple[Any, str]:
    """Returns (value, key_used). Missing means "n/r", never a zero or a default."""
    for candidate in FIELD_CANDIDATES[field]:
        if candidate in row and row[candidate] is not None:
            return row[candidate], candidate
    return NOT_RECORDED, NOT_RECORDED


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numeric(text: Any) -> Optional[float]:
    """Pulls the first number out of a claim or a cell. None when there is none."""
    if isinstance(text, (int, float)):
        return float(text)
    if text is None:
        return None
    match = _NUMBER.search(str(text))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def currency_of(text: Any) -> Optional[str]:
    match = re.search(r"\b(EUR|USD|GBP|JPY|CHF|CAD|AUD)\b", str(text), re.IGNORECASE)
    if match:
        return match.group(1).upper()
    if "\u20ac" in str(text):
        return "EUR"
    if "$" in str(text):
        return "USD"
    return None


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------


def claims_from_memory_facts(facts: List[str]) -> List[Dict[str, str]]:
    """Turns every guard hit in a live memory into a claim to reconcile.

    Reuses the write-time guard rather than a second pattern list, so the thing
    the seeder refuses to write is exactly the thing the reconciler looks for.

    Scans under the `authoritative` profile, not every rule. `memory_facts.py`
    separates its rules into two sets for a reason: `bare_quantity` and
    `record_id` are hygiene signals -- "real signals, but about durability
    rather than authority" -- and BigQuery's `disputed_amount` is not the
    authority for them.

    Scanning strictly here produced nonsense. A card suffix is four digits, so
    `bare_quantity` matched it, the old `kind` map called it an amount, and the
    run reported "memory says 4417, BigQuery says 796.42 / 715.61 / 1065.76" as
    a mismatch. Two of the five failures on the live corpus were card suffixes,
    not money. A reconciler that cries wolf on card numbers is one people learn
    to ignore, which costs more than the three real findings are worth.
    """
    claims: List[Dict[str, str]] = []
    for fact in facts:
        for violation in scan_profile(fact, "authoritative"):
            kind = {"currency_amount": "amount",
                    "monetary_shaped_number": "amount",
                    "dispute_id": "dispute_id",
                    "deadline_phrase": "deadline",
                    "duration_window": "window"}.get(violation.rule, "unknown")
            claims.append({
                "claim": violation.matched,
                "kind": kind,
                "authority": "BigQuery disputes row via golden queries 07/08",
                "in_memory": fact,
            })
    return claims


def load_memory_claims(args: argparse.Namespace) -> Tuple[List[Dict[str, str]], str]:
    if args.memory_fixture:
        with open(args.memory_fixture, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        facts = [item if isinstance(item, str) else str(item.get("fact", "")) for item in payload]
        return claims_from_memory_facts(facts), "memory fixture %s" % args.memory_fixture

    if args.legacy_blob:
        return list(LEGACY_BLOB_CLAIMS), LEGACY_SOURCE

    if args.dry_run:
        return [], "not read - offline, and no fixture or --legacy-blob given"

    from backend.memory_bank import MemoryBankClient, field as mb_field

    if not args.project or not args.engine_id:
        raise SystemExit(EXIT_CONFIG)

    client_kwargs: Dict[str, Any] = {"project": args.project,
                                     "engine_id": args.engine_id,
                                     "verbose": not args.quiet}
    if args.location:
        client_kwargs["location"] = args.location
    client = MemoryBankClient(**client_kwargs)
    items = client.retrieve({"customer_id": args.customer})
    facts = [str(mb_field(item.get("memory", item), "fact")) for item in items]
    return claims_from_memory_facts(facts), "live memories.retrieve, scope customer_id=%s" % args.customer


def load_bigquery_rows(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], str]:
    """Returns (rows, source). Never returns a placeholder row on failure."""
    if args.bq_fixture:
        with open(args.bq_fixture, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload.get("rows", payload) if isinstance(payload, dict) else payload
        return list(rows), "captured BigQuery result %s" % args.bq_fixture

    if args.dry_run:
        return [], "not queried - offline"

    # Imported here so the offline path needs neither ADK nor the BigQuery SDK.
    from backend.adk_agents import (
        golden_07_open_disputes_for_customer,
        golden_08_dispute_filing_window_check,
    )

    result = golden_07_open_disputes_for_customer(customer_id=args.customer)
    rows = list(result.get("rows") or [])
    if not rows:
        raise RuntimeError(
            "golden query 07 returned no rows for %s. Stopping: with no authoritative "
            "row there is nothing to reconcile against, and a reconciliation that "
            "passes because it read nothing is worse than no reconciliation."
            % args.customer)

    for row in rows:
        dispute_id, _ = resolve(row, "dispute_id")
        if dispute_id == NOT_RECORDED:
            continue
        window = golden_08_dispute_filing_window_check(dispute_id=str(dispute_id))
        for extra in (window.get("rows") or []):
            # Merge the deadline columns onto the dispute row so one comparison
            # table covers amount and deadline together.
            for key, value in extra.items():
                row.setdefault(key, value)

    return rows, "golden queries 07 + 08 on BigQuery"


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------


def compare(claims: List[Dict[str, str]], rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []

    for claim in claims:
        kind = claim["kind"]
        verdict = "UNVERIFIABLE"
        authoritative = NOT_RECORDED
        key_used = NOT_RECORDED
        detail = "no authoritative row was read, so this claim is unchecked, not clean"

        if rows:
            if kind == "amount":
                wanted = numeric(claim["claim"])
                claim_currency = currency_of(claim["claim"])
                found: List[Tuple[Any, str, Any]] = []
                match = None
                for row in rows:
                    value, key = resolve(row, "amount")
                    if value == NOT_RECORDED:
                        continue
                    row_currency, _ = resolve(row, "currency")
                    found.append((value, key, row_currency))
                    if wanted is not None and numeric(value) == wanted:
                        match = (value, key, row_currency)
                if not found:
                    detail = ("no amount column found on the BigQuery rows (tried %s), so "
                              "the claim cannot be checked" % ", ".join(FIELD_CANDIDATES["amount"]))
                else:
                    value, key_used, row_currency = match or found[0]
                    if match is None:
                        # Show every authoritative amount, not just the first row: a
                        # reader comparing "EUR 412" needs to see that neither
                        # BigQuery amount is close to it.
                        authoritative = " / ".join(
                            "%s %s" % (c, v) if c != NOT_RECORDED else str(v)
                            for v, _, c in found)
                    else:
                        authoritative = ("%s %s" % (row_currency, value)
                                         if row_currency != NOT_RECORDED else value)
                    same_number = match is not None
                    same_currency = (claim_currency is None or row_currency == NOT_RECORDED
                                     or str(row_currency).upper() == claim_currency)
                    if same_number and same_currency:
                        verdict = "MATCHES-BUT-STILL-WRONG-HOME"
                        detail = ("the number agrees today. It still may not live in a memory: "
                                  "nothing re-writes the memory when the ledger moves.")
                    else:
                        verdict = "MISMATCH"
                        detail = ("memory says %s, BigQuery says %s. BigQuery wins."
                                  % (claim["claim"], authoritative))

            elif kind in ("deadline", "window"):
                # "90 days" is a window and belongs against the window column;
                # "23 Sep" is a date and belongs against the due-date column.
                # Checking them against each other would report a mismatch that
                # is really a units error in this script.
                order = ("window_days", "deadline") if kind == "window" else ("deadline", "window_days")
                # Sweep every row for the preferred column before falling back,
                # or a row that happens to come first decides the units.
                for preferred in order:
                    for row in rows:
                        value, key = resolve(row, preferred)
                        if value != NOT_RECORDED:
                            authoritative, key_used = value, key
                            break
                    if authoritative != NOT_RECORDED:
                        break
                if authoritative == NOT_RECORDED:
                    detail = ("no deadline or window column found on the BigQuery rows "
                              "(tried %s / %s)" % (", ".join(FIELD_CANDIDATES["deadline"]),
                                                   ", ".join(FIELD_CANDIDATES["window_days"])))
                else:
                    wanted = numeric(claim["claim"])
                    got = numeric(authoritative)
                    if wanted is not None and got is not None and wanted == got:
                        verdict = "MATCHES-BUT-STILL-WRONG-HOME"
                        detail = ("the window agrees today, but it is policy plus a filing "
                                  "date, both of which are looked up.")
                    else:
                        verdict = "MISMATCH"
                        detail = ("memory says %s, BigQuery-derived value is %s. BigQuery wins."
                                  % (claim["claim"], authoritative))

            elif kind == "dispute_id":
                ids = []
                for row in rows:
                    value, key = resolve(row, "dispute_id")
                    if value != NOT_RECORDED:
                        ids.append(str(value))
                        key_used = key
                authoritative = ", ".join(ids) if ids else NOT_RECORDED
                if not ids:
                    detail = "no dispute id column found on the BigQuery rows"
                elif claim["claim"].upper() in [i.upper() for i in ids]:
                    verdict = "MATCHES-BUT-STILL-WRONG-HOME"
                    detail = ("the id exists today. A memory naming it breaks the moment the "
                              "case is split, merged or re-filed.")
                else:
                    verdict = "MISMATCH"
                    detail = ("memory names %s; BigQuery has %s for this customer."
                              % (claim["claim"], authoritative))

        findings.append({
            "claim": claim["claim"],
            "kind": kind,
            "in_memory": claim.get("in_memory", NOT_RECORDED),
            "authoritative": authoritative,
            "bq_column": key_used,
            "verdict": verdict,
            "detail": detail,
        })

    return findings


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_query_plan(args: argparse.Namespace) -> None:
    print("-" * 78)
    print("WHAT THIS WOULD QUERY (nothing is sent in --dry-run)")
    print("-" * 78)
    print("  golden query 07  contract : 07_open_disputes_for_customer.sql")
    print("                   tool     : golden_07_open_disputes_for_customer")
    print("                   agent    : 05_policy_compliance (the only one authorized)")
    print("                   params   : customer_id=%s, as_of_date, open_statuses[]" % args.customer)
    print("                   gives    : dispute rows - id, amount, currency, status")
    print()
    print("  golden query 08  contract : 08_dispute_filing_window_check.sql")
    print("                   tool     : golden_08_dispute_filing_window_check")
    print("                   params   : dispute_id, taken from every row 07 returned")
    print("                              (never typed into this script)")
    print("                   gives    : filing-window status and the provisional-credit date")
    print()
    print("  project / dataset come from the golden-query layer's own environment "
          "configuration; this script sets none of them.")
    print()


def print_findings(findings: List[Dict[str, Any]], memory_source: str, bq_source: str,
                   memory_unread: bool) -> None:
    print("-" * 78)
    print("RECONCILIATION - memory claim vs BigQuery")
    print("-" * 78)
    print("  memory side : %s" % memory_source)
    print("  BigQuery    : %s" % bq_source)
    print()
    if memory_unread:
        print("  no memory was read, so there is nothing to reconcile and no verdict to give.")
        print("  Re-run with --legacy-blob, --memory-fixture, or live credentials.")
        print()
        return
    if not findings:
        print("  no memory in scope asserts an amount, a deadline or a dispute id.")
        print("  That is the target state: memory describes, BigQuery quantifies.")
        print()
        return

    header = "  %-14s %-10s %-26s %-28s" % ("MEMORY CLAIM", "KIND", "BIGQUERY SAYS", "VERDICT")
    print(header)
    print("  " + "-" * (len(header) - 2))
    for finding in findings:
        print("  %-14s %-10s %-26s %-28s" % (
            finding["claim"][:14], finding["kind"][:10],
            str(finding["authoritative"])[:26], finding["verdict"]))
        print("      column : %s" % finding["bq_column"])
        print("      why    : %s" % finding["detail"])
        if finding["in_memory"] != NOT_RECORDED:
            print("      memory : %s" % finding["in_memory"])
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reconcile_scenario.py",
        description="Reconcile every amount, deadline and dispute id memory asserts "
                    "against BigQuery. BigQuery wins.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("USAGE")[-1],
    )
    parser.add_argument("--customer", default=os.environ.get("MEMORY_SEED_CUSTOMER_ID", "CUST00001"))
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    parser.add_argument("--engine-id",
                        default=(os.environ.get("VERTEX_AGENT_ENGINE_ID")
                                 or os.environ.get("AGENT_ENGINE_ID")
                                 or os.environ.get("VERTEX_MEMORY_BANK_ENGINE_ID")))
    parser.add_argument("--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION"))
    parser.add_argument("--dry-run", action="store_true",
                        help="Explain the queries instead of running them. No credentials needed.")
    parser.add_argument("--legacy-blob", action="store_true",
                        help="Use the recorded claims of the pre-fix narrative blob as the "
                             "memory side, so the reconciliation is reviewable offline.")
    parser.add_argument("--memory-fixture",
                        help="JSON list of memory facts (strings, or objects with a 'fact' key) "
                             "to use as the memory side.")
    parser.add_argument("--bq-fixture",
                        help="JSON list of dispute rows, or an object with a 'rows' key, "
                             "captured from BigQuery. Lets the comparison run with no GCP access.")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.dry_run and (not args.project or not args.engine_id):
        print("A live run needs --project and --engine-id (or the matching environment "
              "variables) to read the memories being reconciled. Use --dry-run to see "
              "the plan without them.", file=sys.stderr)
        return EXIT_CONFIG

    try:
        memory_claims, memory_source = load_memory_claims(args)
        rows, bq_source = load_bigquery_rows(args)
    except FileNotFoundError as error:
        print("Fixture not found: %s" % error, file=sys.stderr)
        return EXIT_CONFIG
    except Exception as error:
        # Loud failure. A reconciliation that cannot read its inputs must not
        # print a clean table.
        print("Could not gather the inputs. Nothing was substituted in their place.\n"
              "%s: %s" % (type(error).__name__, error), file=sys.stderr)
        return EXIT_MISMATCH

    print()
    print("=" * 78)
    print("SCENARIO RECONCILIATION - customer %s" % args.customer)
    print("=" * 78)
    print("Rule under test: memory may describe the situation; it may never be the "
          "source of an amount, a deadline or a dispute id.")
    print()

    if args.dry_run:
        print_query_plan(args)

    # "Nothing to check" and "checked, found nothing" are different outcomes and
    # must not print the same word.
    memory_unread = memory_source.startswith("not read")

    findings = compare(memory_claims, rows)
    print_findings(findings, memory_source, bq_source, memory_unread)

    payload = {
        "customer_id": seeded(args.customer, "%s --customer" % SOURCE),
        "memory_claims": [seeded(claim["claim"], LEGACY_SOURCE if args.legacy_blob
                                 else "memories.retrieve, scope customer_id=%s" % args.customer)
                          for claim in memory_claims],
        "bigquery_rows": ([live(row, "golden queries 07 + 08") for row in rows]
                          if rows and not args.bq_fixture
                          else [seeded(row, "captured BigQuery result %s" % args.bq_fixture)
                                for row in rows]
                          if rows else [unmeasured("%s: BigQuery not queried" % SOURCE)]),
        "verdicts": [computed(finding["verdict"], "%s: claim %r vs %s"
                              % (SOURCE, finding["claim"], finding["bq_column"]))
                     for finding in findings],
    }
    counts = summarize(payload)
    print("provenance of this reconciliation: %s" % json.dumps(counts.get("counts", counts)))
    print()

    if memory_unread:
        print("NO VERDICT: the memory side was not read, so this run proves nothing "
              "either way. It printed the plan, not a result.")
        return EXIT_OK

    mismatches = [f for f in findings if f["verdict"] == "MISMATCH"]
    violations = [f for f in findings if f["verdict"] != "MISMATCH"]
    unverifiable = [f for f in findings if f["verdict"] == "UNVERIFIABLE"]

    if mismatches:
        print("FAIL: %d claim(s) in memory disagree with BigQuery. BigQuery is "
              "authoritative; the memory must be re-seeded without them "
              "(scripts/seed_atomic_memories.py --purge-blob)." % len(mismatches))
        return EXIT_MISMATCH

    if unverifiable:
        print("INCONCLUSIVE: %d claim(s) could not be checked because no authoritative "
              "row was read. This is reported, not passed." % len(unverifiable))
        return EXIT_RULE_VIOLATION

    if violations:
        print("FAIL: %d claim(s) agree with BigQuery but are still stored in memory. "
              "Agreeing today is not the standard; not being there is." % len(violations))
        return EXIT_RULE_VIOLATION

    print("PASS: memory asserts no amount, deadline or dispute id.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

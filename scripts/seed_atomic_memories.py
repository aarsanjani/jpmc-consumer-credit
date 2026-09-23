#!/usr/bin/env python3
"""seed_atomic_memories.py - seed one customer's Memory Bank as discrete facts.

WHAT IT FIXES
    The Lisbon scenario was seeded as a single ~200-word narrative summary,
    written by a local "compaction" step that pretended to be the managed
    service. One blob breaks retrieval twice over:

      * semantic search has exactly one thing to return, so every query -
        "where is the customer", "what did they ask us not to do" - comes back
        with the same paragraph, and relevance scoring is meaningless;
      * consolidation has nothing discrete to supersede. Memory Bank resolves
        contradictions by replacing a fact with a newer fact. It cannot replace
        one clause inside a paragraph, so the stale clause lives forever.

    This script writes each fact on its own, through `memories.create`, so the
    service has units it can rank and supersede.

WHAT IT DELIBERATELY DOES NOT DO
    No local compaction, no "dreaming" loop, no client-side consolidation, no
    token accounting. Consolidation is a server-side behaviour of Memory Bank;
    a local imitation of it is what produced the blob.

    No embeddings and no ranking here either. Retrieval happens inside Google.

SAFETY
    Every fact passes `scripts/memory_facts.assert_no_authoritative_values`
    before it is written, so an amount, a deadline or a dispute id cannot get
    into memory through this door. Those are read from BigQuery at use time.

USAGE
    # reviewable with no GCP access at all - prints the exact calls and bodies
    python3 scripts/seed_atomic_memories.py --dry-run
    python3 scripts/seed_atomic_memories.py --dry-run --purge-blob

    # live
    export GOOGLE_CLOUD_PROJECT=...        # or --project
    export VERTEX_AGENT_ENGINE_ID=...      # or --engine-id
    python3 scripts/seed_atomic_memories.py --purge-blob --yes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

# scripts/ is not a package; the repo root is what holds backend/.
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
from memory_facts import (  # noqa: E402
    AuthoritativeValueError,
    assert_no_authoritative_values,
    scan,
)

SOURCE = "scripts/seed_atomic_memories.py"


# ---------------------------------------------------------------------------
# The facts
# ---------------------------------------------------------------------------

# Grain rule: one fact, one clause, no value BigQuery owns.
#
# Each entry carries `why_this_grain` because the grain is the whole point of
# this rework and a future editor needs to know what would make an addition
# wrong. The text is what gets written; the note never leaves this file.
#
# Provenance: every fact below is a restatement of one turn in
# `backend.memory_bank.LISBON_THEFT_TURNS`, the 09 Sep IVR call that is the
# seeded scenario for CUST00001. Nothing here is inferred beyond that
# transcript, which is why there is no fact about tokens, cards or amounts -
# those are rows in BigQuery, not things the customer told us.
ATOMIC_FACTS: List[Dict[str, str]] = [
    {
        "fact": "Travelling in Lisbon 03-12 Sep",
        "from_turn": "agent, 08:15 - 'I can see the travel notice for Lisbon through the twelfth'",
        "why_this_grain": (
            "Where the customer is, as a trip window. No year, because a memory "
            "that dates itself goes stale; the year is on the travel notice row. "
            "This is the fact an agent needs to know a Lisbon charge is not "
            "automatically suspicious."
        ),
    },
    {
        "fact": "Reported the wallet stolen while in Lisbon",
        "from_turn": "user, 08:14 - 'My wallet was stolen this morning. I am in Lisbon.'",
        "why_this_grain": (
            "The event, with no amount and no dispute id attached. Separate from "
            "the trip fact so a later 'trip extended' memory can supersede the "
            "trip without touching the theft."
        ),
    },
    {
        "fact": "Both the debit card and the credit card were in the stolen wallet",
        "from_turn": "user, 08:16 - 'The debit card and the credit card. Both of them.'",
        "why_this_grain": (
            "Scope of the loss in words. Naming CARD00002 / CARD00004 here would "
            "pin the memory to rows that get reissued; the card table answers "
            "'which cards' at use time."
        ),
    },
    {
        "fact": "Asked for both cards to be frozen straight away",
        "from_turn": "user, 08:16 - 'Freeze them now.'",
        "why_this_grain": (
            "The customer's instruction, not the system's action. Whether the "
            "freeze is currently in place is card state in BigQuery; what the "
            "customer asked for is durable and belongs in memory."
        ),
    },
    {
        "fact": "Disputes the Lisbon charges made after the wallet was taken",
        "from_turn": "user, 08:14-08:16 - theft reported, both cards in the wallet",
        "why_this_grain": (
            "The single most load-bearing example of the rule: the memory says "
            "the customer disputes the Lisbon charges, and says nothing about "
            "how much or which dispute. Amount, currency and dispute id come "
            "from the disputes table on every read."
        ),
    },
    {
        "fact": "Needs a working card for the rest of the trip",
        "from_turn": "user, 08:20 - 'I am still here until the twelfth, so I need a card that works'",
        "why_this_grain": (
            "A standing need, phrased relative to the trip instead of to a date, "
            "so it stays true if the return flight moves."
        ),
    },
    {
        "fact": "Accepted a virtual card as the immediate stand-in",
        "from_turn": "agent, 08:22 - 'A virtual card is available immediately'",
        "why_this_grain": (
            "A decision the customer agreed to. Discrete, so if they later reject "
            "virtual cards the service can supersede this one fact."
        ),
    },
    {
        "fact": "Replacement card goes to the hotel, not the home address",
        "from_turn": "agent, 08:22 - 'the replacement goes to your hotel'",
        "why_this_grain": (
            "A delivery preference that contradicts the address on file, which is "
            "exactly the kind of thing consolidation needs as its own unit: when "
            "the trip ends, 'send to home' supersedes this and nothing else."
        ),
    },
    {
        "fact": "Notify before closing any dispute",
        "from_turn": "user, 08:20 - 'do not close anything without telling me first'",
        "why_this_grain": (
            "A durable instruction that outlives this case. Buried in a paragraph "
            "it is invisible to a query about contact preferences; on its own it "
            "retrieves for 'what has this customer asked us to do'."
        ),
    },
]


# ---------------------------------------------------------------------------
# Blob detection
# ---------------------------------------------------------------------------

DEFAULT_BLOB_MIN_WORDS = 40

# Substrings that identify the pre-fix narrative summary. Parameterised rather
# than fixed so an operator can target a different artefact without editing code.
DEFAULT_BLOB_MARKERS = ("compaction", "compacted", "summary of", "dreaming")


def looks_like_blob(fact: str, min_words: int, markers: List[str]) -> Optional[str]:
    """Returns the reason this memory looks like the narrative blob, or None.

    Two independent signals, because either one alone is wrong on its own: a
    long memory is a blob whatever it says, and a short memory that announces
    itself as a compaction summary is the blob's leftovers.
    """
    words = len(fact.split())
    if words >= min_words:
        return "%d words (threshold %d) - a memory this long is a narrative, not a fact" % (
            words, min_words)
    lowered = fact.lower()
    for marker in markers:
        if marker.lower() in lowered:
            return "contains compaction marker %r" % marker
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seed_atomic_memories.py",
        description="Seed one customer's Memory Bank scope as atomic single-fact memories.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("USAGE")[-1],
    )
    # Ids are deployment parameters. Defaults come from the environment, never
    # from a literal in this file: a baked-in project id is how a kit ends up
    # silently writing into somebody else's engine.
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                        help="GCP project. Default: $GOOGLE_CLOUD_PROJECT.")
    parser.add_argument("--engine-id",
                        default=(os.environ.get("VERTEX_AGENT_ENGINE_ID")
                                 or os.environ.get("AGENT_ENGINE_ID")
                                 or os.environ.get("VERTEX_MEMORY_BANK_ENGINE_ID")),
                        help="Agent Engine (reasoningEngine) id. Default: "
                             "$VERTEX_AGENT_ENGINE_ID / $AGENT_ENGINE_ID / "
                             "$VERTEX_MEMORY_BANK_ENGINE_ID.")
    parser.add_argument("--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION"),
                        help="Region. Default: $GOOGLE_CLOUD_LOCATION, else the "
                             "client's own default.")
    parser.add_argument("--api-version", default=os.environ.get("VERTEX_API_VERSION"),
                        help="Default: $VERTEX_API_VERSION, else the client's own default.")
    parser.add_argument("--customer", default=os.environ.get("MEMORY_SEED_CUSTOMER_ID", "CUST00001"),
                        help="Customer id used as the memory scope. Default: "
                             "$MEMORY_SEED_CUSTOMER_ID, else CUST00001.")

    parser.add_argument("--dry-run", action="store_true",
                        help="Print every call and body that would be sent, and exit. "
                             "Makes no network call and needs no credentials.")
    parser.add_argument("--yes", action="store_true",
                        help="Required for a live run that deletes anything.")
    parser.add_argument("--purge-blob", action="store_true",
                        help="Delete the pre-fix narrative blob from this scope first, "
                             "so re-seeding does not leave it in place.")
    parser.add_argument("--blob-min-words", type=int, default=DEFAULT_BLOB_MIN_WORDS,
                        help="Word count at or above which a memory is treated as the "
                             "narrative blob. Default: %d." % DEFAULT_BLOB_MIN_WORDS)
    parser.add_argument("--blob-contains", action="append", default=[],
                        help="Extra substring that marks a memory as the blob. Repeatable.")
    parser.add_argument("--quiet", action="store_true",
                        help="Silence the client's per-request logging.")
    return parser


def render_plan(args: argparse.Namespace, scope: Dict[str, str]) -> Dict[str, Any]:
    """Builds the write plan and tags every value with where it came from."""
    return {
        "customer_id": seeded(args.customer, "%s --customer" % SOURCE),
        "scope": computed(scope, "%s: scope is built per call, never bound to the client" % SOURCE),
        "project": seeded(args.project, "$GOOGLE_CLOUD_PROJECT / --project")
                   if args.project else unmeasured("%s: project not supplied" % SOURCE),
        "engine_id": seeded(args.engine_id, "$VERTEX_AGENT_ENGINE_ID / --engine-id")
                     if args.engine_id else unmeasured("%s: engine id not supplied" % SOURCE),
        "location": seeded(args.location, "$GOOGLE_CLOUD_LOCATION / --location")
                    if args.location else unmeasured("%s: location left to the client default" % SOURCE),
        "facts": [seeded(item["fact"], "%s ATOMIC_FACTS[%d] <- backend.memory_bank."
                                       "LISBON_THEFT_TURNS (%s)" % (SOURCE, index, item["from_turn"]))
                  for index, item in enumerate(ATOMIC_FACTS)],
    }


def check_all_facts() -> None:
    """Runs the guard over every fact before anything is written or printed.

    Fails the whole run on the first bad fact rather than writing the good ones
    first: a half-seeded scope is harder to reason about than an empty one.
    """
    for index, item in enumerate(ATOMIC_FACTS):
        assert_no_authoritative_values(item["fact"], label="ATOMIC_FACTS[%d]" % index)


def do_dry_run(args: argparse.Namespace, scope: Dict[str, str]) -> int:
    project = args.project or NOT_RECORDED
    engine = args.engine_id or NOT_RECORDED
    location = args.location or NOT_RECORDED
    api_version = args.api_version or NOT_RECORDED
    base = ("https://%s-aiplatform.googleapis.com/%s/projects/%s/locations/%s/"
            "reasoningEngines/%s" % (location, api_version, project, location, engine))

    print("DRY RUN - nothing is sent, no credential is read.")
    print("Unsupplied ids render as %r. They are not defaulted to a literal, because a "
          "plausible-looking project id is how a run talks to nothing and still looks healthy."
          % NOT_RECORDED)
    print()
    print("scope passed on every call : %s" % json.dumps(scope))
    print("engine base url            : %s" % base)
    print("client method per fact     : MemoryBankClient.create_memory(fact, scope)")
    print()

    if args.purge_blob:
        print("-" * 74)
        print("STEP 1 - purge the pre-fix narrative blob")
        print("-" * 74)
        print("  POST %s/memories:retrieve" % base)
        print("       body: %s" % json.dumps({"scope": scope}))
        print("  then, for each returned memory that looks like the blob")
        print("       (>= %d words, or containing one of %s):"
              % (args.blob_min_words, list(DEFAULT_BLOB_MARKERS) + list(args.blob_contains)))
        print("  DELETE %s/memories/{memory_id}" % base)
        print("  A live run refuses to delete without --yes. Nothing is deleted by word "
              "count alone without printing the memory first.")
        print()

    print("-" * 74)
    print("STEP 2 - write %d atomic memories, one call each" % len(ATOMIC_FACTS))
    print("-" * 74)
    for index, item in enumerate(ATOMIC_FACTS, start=1):
        body = {"fact": item["fact"], "scope": scope}
        print("  [%d/%d] POST %s/memories" % (index, len(ATOMIC_FACTS), base))
        print("         body: %s" % json.dumps(body, ensure_ascii=False))
        print("         guard: clean (%d rule hits)" % len(scan(item["fact"])))
        print("         grain: %s" % item["why_this_grain"])
        print()

    plan = render_plan(args, scope)
    counts = summarize(plan)
    print("-" * 74)
    print("provenance of this plan: %s" % json.dumps(counts.get("counts", counts)))
    print("-" * 74)
    return 0


def do_live_run(args: argparse.Namespace, scope: Dict[str, str]) -> int:
    # Imported here so --dry-run works in an environment with nothing installed.
    from backend.memory_bank import MemoryBankClient, MemoryBankError, field

    if not args.project or not args.engine_id:
        print("A live run needs --project and --engine-id (or the matching environment "
              "variables). Refusing to guess: a wrong engine id writes a customer's "
              "memories into somebody else's bank.", file=sys.stderr)
        return 2

    client_kwargs: Dict[str, Any] = {
        "project": args.project,
        "engine_id": args.engine_id,
        "verbose": not args.quiet,
    }
    if args.location:
        client_kwargs["location"] = args.location
    if args.api_version:
        client_kwargs["api_version"] = args.api_version
    client = MemoryBankClient(**client_kwargs)

    purged = 0
    if args.purge_blob:
        if not args.yes:
            print("--purge-blob deletes memories. Re-run with --yes once you have read "
                  "the candidates printed by --dry-run.", file=sys.stderr)
            return 2

        markers = list(DEFAULT_BLOB_MARKERS) + list(args.blob_contains)
        existing = client.retrieve(scope)
        print("\n%d memory(ies) currently in scope %s" % (len(existing), scope))
        if not existing:
            print("  none - nothing to purge. Not an error, but if you expected the blob "
                  "here, check the scope and the engine id before seeding on top.")
        for item in existing:
            memory = item.get("memory", item)
            fact = field(memory, "fact")
            name = field(memory, "name")
            reason = looks_like_blob(str(fact), args.blob_min_words, markers)
            if reason is None:
                violations = scan(str(fact))
                flag = ("  <- violates the no-authoritative-values rule (%s)" % violations[0].rule
                        if violations else "")
                print("  keep   %s%s" % (str(fact)[:90], flag))
                continue
            if name == NOT_RECORDED:
                # Deleting needs the name the service returned. Without it we stop
                # rather than guess an id.
                print("  BLOB   %s" % str(fact)[:90])
                print("         cannot delete: the service did not return a resource name (%s)"
                      % NOT_RECORDED, file=sys.stderr)
                return 1
            print("  DELETE %s" % name)
            print("         reason: %s" % reason)
            print("         fact  : %s" % str(fact)[:200])
            client.delete_memory(name)
            purged += 1

    written = []
    print("\nWriting %d atomic memories into scope %s" % (len(ATOMIC_FACTS), scope))
    for index, item in enumerate(ATOMIC_FACTS, start=1):
        fact = assert_no_authoritative_values(item["fact"], label="ATOMIC_FACTS[%d]" % (index - 1))
        response = client.create_memory(fact, scope)
        # create returns a long-running operation; the memory is not readable
        # until it reports done, so retrieval right after a fast loop would
        # otherwise look like the write silently failed.
        response = client.wait_for_operation(response)
        name = field(response, "response", "name")
        written.append(live(name, "memories.create response for ATOMIC_FACTS[%d]" % (index - 1)))
        print("  [%d/%d] %s" % (index, len(ATOMIC_FACTS), name))

    print("\npurged %d | written %d" % (purged, len(written)))
    print("http calls: %d" % len(client.calls))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # Scope travels with every call. The client stays engine-level so one
    # instance can serve every customer without cross-contamination.
    scope = {"customer_id": args.customer}

    try:
        check_all_facts()
    except AuthoritativeValueError as error:
        print("Refusing to seed. One of the facts in this script is not allowed in "
              "memory:\n%s" % error, file=sys.stderr)
        return 3

    if args.dry_run:
        return do_dry_run(args, scope)

    try:
        return do_live_run(args, scope)
    except AuthoritativeValueError as error:
        print("Refusing to write:\n%s" % error, file=sys.stderr)
        return 3
    except Exception as error:  # surfaced, never swallowed into a healthy-looking run
        print("\nMemory Bank call failed. Nothing was faked in its place.\n%s: %s"
              % (type(error).__name__, error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
probe_agents.py - vet every agent in the desk, one tool at a time, against live services.

`trace_adk_agents.py` proves the *module* works: the golden queries run, the
catalog answers, the channels produce. This harness asks a different question,
agent by agent: does this agent actually hold the tools its instruction tells it
to call, and does each of those tools return something when pointed at the live
project?

The roster is not written down here. It is read off the constructed ADK graph --
`create_consumer_credit_synthesizer_agent()` and
`create_ccb_fraud_desk_orchestrator_agent()` -- so a tool added or removed in
`backend/adk_agents.py` shows up in this report on the next run and cannot drift
out of sync with it.

Three findings are reported per agent, and they are deliberately different
things:

    TOOL STATUS    what each tool returned: DATA, EMPTY, or ERROR
    INSTRUCTION    every tool the instruction names that the agent cannot call,
                   and every tool it holds that the instruction never mentions
    PROVENANCE     unwaived STATIC values in the returned payload

An agent whose instruction names a tool it does not hold is the failure this
harness exists to catch: the model is told to do something it has no way to do,
and answers from its own knowledge instead. That is invisible in a passing
end-to-end run.

Nothing is mocked. A tool that cannot reach its service is an ERROR, not a
fallback. Write tools are skipped unless --allow-writes is passed, because
probing them mutates the Memory Bank corpus the demo reads from.

Usage
-----
    set -a; . ./.env; set +a
    PYTHONPATH=. ./.venv/bin/python3 scripts/probe_agents.py
    PYTHONPATH=. ./.venv/bin/python3 scripts/probe_agents.py --desk synthesizer
    PYTHONPATH=. ./.venv/bin/python3 scripts/probe_agents.py --agent mobile_app_agent
    PYTHONPATH=. ./.venv/bin/python3 scripts/probe_agents.py --allow-writes
    PYTHONPATH=. ./.venv/bin/python3 scripts/probe_agents.py -f json > probe.jsonl

Exit code is 0 only if every probed tool returned without error and every
instruction's named tools were all reachable.
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import pathlib
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend import adk_agents as A  # noqa: E402
from backend import obs  # noqa: E402
from backend.provenance import NOT_RECORDED, summarize, value_of  # noqa: E402

LOG = obs.get_logger("probe")

# Tools that write to a live service. Probing them is a mutation, so they are
# reported as SKIPPED unless the operator opts in.
WRITE_TOOLS = {
    "execute_one_click_card_unlock",
    "validate_and_record_customer_claim",
}


@dataclass
class ToolProbe:
    agent: str
    tool: str
    status: str            # DATA | EMPTY | ERROR | SKIPPED
    detail: str
    elapsed_ms: float = 0.0
    static_offenders: int = 0
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentReport:
    name: str
    role: str
    probes: List[ToolProbe] = field(default_factory=list)
    missing_tools: List[str] = field(default_factory=list)
    unused_tools: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        no_errors = all(p.status != "ERROR" for p in self.probes)
        return no_errors and not self.missing_tools


# ---------------------------------------------------------------------------
# Binding real arguments
# ---------------------------------------------------------------------------

@dataclass
class Scope:
    """The concrete ids this run probes with, resolved from live data.

    Resolved rather than hardcoded so the harness cannot pass because it asked
    about a card that is not in the dataset any more; a default that no longer
    resolves is itself a finding.
    """

    customer_id: str
    card_id: str
    dispute_id: str
    table: str
    columns: List[str]
    term_id: str
    notes: List[str] = field(default_factory=list)


def resolve_scope(customer_id: str) -> Scope:
    """Reads the ids to probe with out of the customer's own ledger."""
    card_id = A.DEFAULT_CARD_ID
    dispute_id = "DSP00033"
    notes: List[str] = []

    try:
        disputes = value_of(A.golden_07_open_disputes_for_customer(customer_id=customer_id).get("rows")) or []
    except Exception as exc:  # noqa: BLE001 - a dead ledger is a finding, not a crash
        disputes = []
        notes.append("golden_07 failed while resolving scope: %s" % exc)

    if disputes:
        first = disputes[0]
        card_id = first.get("card_id") or card_id
        dispute_id = first.get("dispute_id") or dispute_id
        ledger_cards = sorted({row.get("card_id") for row in disputes if row.get("card_id")})
        if A.DEFAULT_CARD_ID not in ledger_cards:
            notes.append(
                "DEMO_CARD_ID=%s has no open dispute in the ledger (ledger holds %s); "
                "probing with %s instead" % (A.DEFAULT_CARD_ID, ", ".join(ledger_cards), card_id)
            )
    else:
        notes.append("no open disputes for %s; probing with module defaults" % customer_id)

    return Scope(
        customer_id=customer_id,
        card_id=card_id,
        dispute_id=dispute_id,
        table="disputes",
        columns=["dispute_id", "regulation_path", "provisional_credit_issued"],
        term_id="provisional-credit",
        notes=notes,
    )


def bind_args(fn: Callable[..., Any], scope: Scope) -> Dict[str, Any]:
    """Fills a tool's signature from the resolved scope.

    Only parameters the function actually declares are supplied, so a tool that
    takes no arguments is called with none rather than being skipped.
    """
    by_name = {
        "customer_id": scope.customer_id,
        "card_id": scope.card_id,
        "dispute_id": scope.dispute_id,
        "table": scope.table,
        "columns": scope.columns,
        "term_id": scope.term_id,
        "channel": A.BankChannel.WEB_PORTAL.value,
        "claim_text": "Probe claim issued by scripts/probe_agents.py; not a customer statement.",
        "summary": "probe_agents.py connectivity probe",
        "reason": "probe_agents.py connectivity probe",
    }

    bound: Dict[str, Any] = {}
    for name, param in inspect.signature(fn).parameters.items():
        if name in by_name:
            bound[name] = by_name[name]
        elif param.default is inspect.Parameter.empty:
            # A required parameter the harness has no value for would be a silent
            # TypeError; naming it is more useful than guessing at it.
            bound[name] = NOT_RECORDED
    return bound


# ---------------------------------------------------------------------------
# Classifying what came back
# ---------------------------------------------------------------------------

COUNT_KEYS = ("row_count", "session_count", "memory_count", "topic_count", "term_count")
LIST_KEYS = ("rows", "sessions", "memories", "topics", "findings", "avenues")


def classify(payload: Any) -> Tuple[str, str]:
    """DATA or EMPTY, plus the counts that decided it.

    A tool that reached its service and honestly found nothing is EMPTY, not a
    failure: an empty channel is a legitimate answer. It is still reported
    separately from DATA because an unexpectedly empty channel is usually a
    wrong constant rather than a quiet customer.
    """
    if not isinstance(payload, dict):
        return ("DATA" if payload else "EMPTY", "returned %s" % type(payload).__name__)

    parts: List[str] = []
    total = 0
    for key in COUNT_KEYS + LIST_KEYS:
        if key not in payload:
            continue
        raw = value_of(payload[key])
        count = raw if isinstance(raw, int) else (len(raw) if isinstance(raw, (list, tuple)) else None)
        if count is None:
            continue
        if key in LIST_KEYS and key.rstrip("s") + "_count" in payload:
            continue  # already counted by its own count key
        parts.append("%s=%d" % (key, count))
        total += count

    if not parts:
        # No countable collection. Anything other than an all-n/r payload counts.
        measured = [k for k, v in payload.items() if value_of(v) not in (NOT_RECORDED, None, "", [], {})]
        return ("DATA" if measured else "EMPTY", "%d measured field(s)" % len(measured))

    return ("DATA" if total else "EMPTY", " ".join(parts))


# ---------------------------------------------------------------------------
# Reading the graph
# ---------------------------------------------------------------------------

def tool_functions(agent: Any) -> Dict[str, Callable[..., Any]]:
    """The callables behind an agent's FunctionTools, keyed by function name."""
    found: Dict[str, Callable[..., Any]] = {}
    for tool in getattr(agent, "tools", []) or []:
        fn = getattr(tool, "func", None)
        if fn is None:
            continue
        found[fn.__name__] = fn
    return found


# A tool name in an instruction is a bare identifier: lowercase, underscores,
# and it has to match something the module actually exports, or every ordinary
# word would look like a tool.
IDENTIFIER = re.compile(r"\b[a-z][a-z0-9_]{6,}\b")


def tools_named_in(instruction: str, known: set) -> set:
    return {word for word in IDENTIFIER.findall(instruction or "") if word in known}


def walk_graph(root: Any) -> List[Any]:
    return [root] + list(getattr(root, "sub_agents", []) or [])


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def probe_agent(agent: Any, scope: Scope, known_tools: set, allow_writes: bool) -> AgentReport:
    held = tool_functions(agent)
    named = tools_named_in(getattr(agent, "instruction", ""), known_tools)

    report = AgentReport(
        name=agent.name,
        role=(getattr(agent, "description", "") or "").split(":")[0],
        missing_tools=sorted(named - set(held)),
        unused_tools=sorted(set(held) - named),
    )

    for tool_name, fn in sorted(held.items()):
        if tool_name in WRITE_TOOLS and not allow_writes:
            report.probes.append(ToolProbe(
                agent=agent.name, tool=tool_name, status="SKIPPED",
                detail="writes to a live service; re-run with --allow-writes to probe it",
            ))
            continue

        args = bind_args(fn, scope)
        started = time.perf_counter()
        try:
            payload = fn(**args)
        except Exception as exc:  # noqa: BLE001 - the whole point is to report it
            report.probes.append(ToolProbe(
                agent=agent.name, tool=tool_name, status="ERROR",
                detail="%s: %s" % (type(exc).__name__, exc),
                elapsed_ms=(time.perf_counter() - started) * 1000.0, args=args,
            ))
            continue

        elapsed = (time.perf_counter() - started) * 1000.0
        status, detail = classify(payload)
        report.probes.append(ToolProbe(
            agent=agent.name, tool=tool_name, status=status, detail=detail,
            elapsed_ms=elapsed, args=args,
            static_offenders=len(summarize(payload)["static_offenders"]),
        ))

    return report


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

RULE = "=" * 100
THIN = "-" * 100

MARK = {"DATA": "ok  ", "EMPTY": "EMPTY", "ERROR": "ERROR", "SKIPPED": "skip"}


def print_text(desk: str, reports: List[AgentReport], scope: Scope) -> None:
    print(RULE)
    print("DESK: %s" % desk)
    print("scope: customer=%s card=%s dispute=%s table=%s term=%s"
          % (scope.customer_id, scope.card_id, scope.dispute_id, scope.table, scope.term_id))
    for note in scope.notes:
        print("  note: %s" % note)
    print(RULE)

    for report in reports:
        verdict = "PASS" if report.passed else "FAIL"
        print("\n[%s] %-34s %s" % (verdict, report.name, report.role))
        if not report.probes:
            print("       (holds no tools)")
        for probe in report.probes:
            print("       %-5s %-42s %8s  %s" % (
                MARK.get(probe.status, probe.status),
                probe.tool,
                ("%.0fms" % probe.elapsed_ms) if probe.elapsed_ms else "-",
                probe.detail,
            ))
            if probe.static_offenders:
                print("             ^ %d unwaived STATIC value(s) in payload" % probe.static_offenders)
        if report.missing_tools:
            print("       INSTRUCTION NAMES TOOLS IT CANNOT CALL: %s" % ", ".join(report.missing_tools))
        if report.unused_tools:
            # Advisory only. The check matches literal function names, so an
            # instruction that says "Golden Queries 02, 03, 04" trips it without
            # anything being wrong. Read it as "confirm the prompt points here",
            # not as a defect.
            print("       advisory - held but not named literally: %s" % ", ".join(report.unused_tools))

    print("\n" + THIN)
    passed = sum(1 for r in reports if r.passed)
    errors = sum(1 for r in reports for p in r.probes if p.status == "ERROR")
    empties = sum(1 for r in reports for p in r.probes if p.status == "EMPTY")
    print("%d/%d agents passed - %d tool error(s), %d empty result(s)"
          % (passed, len(reports), errors, empties))
    print(RULE)


def print_json(desk: str, reports: List[AgentReport], scope: Scope) -> None:
    for report in reports:
        print(json.dumps({
            "desk": desk,
            "agent": report.name,
            "role": report.role,
            "passed": report.passed,
            "missing_tools": report.missing_tools,
            "unused_tools": report.unused_tools,
            "scope": {"customer_id": scope.customer_id, "card_id": scope.card_id,
                      "dispute_id": scope.dispute_id},
            "probes": [vars(p) for p in report.probes],
        }, default=str))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

DESKS = {
    "synthesizer": ("8-agent hub-and-spoke desk", A.create_consumer_credit_synthesizer_agent),
    "orchestrator": ("6-agent golden-query tree", A.create_ccb_fraud_desk_orchestrator_agent),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--desk", choices=sorted(DESKS) + ["all"], default="all")
    parser.add_argument("--agent", help="probe only this agent by name")
    parser.add_argument("--customer", default=A.DEFAULT_CUSTOMER_SCOPE_ID)
    parser.add_argument("--allow-writes", action="store_true",
                        help="also probe tools that write to Memory Bank")
    parser.add_argument("-f", "--format", choices=("text", "json"), default="text")
    parser.add_argument("-l", "--log-level", default="WARNING")
    args = parser.parse_args()

    obs.configure(level=args.log_level)
    logging.getLogger("ccb").setLevel(args.log_level)

    known_tools = {name for name in dir(A) if callable(getattr(A, name, None))}
    scope = resolve_scope(args.customer)

    failures = 0
    for key in (sorted(DESKS) if args.desk == "all" else [args.desk]):
        label, factory = DESKS[key]
        agents = walk_graph(factory())
        if args.agent:
            agents = [a for a in agents if a.name == args.agent]
            if not agents:
                continue

        reports = [probe_agent(a, scope, known_tools, args.allow_writes) for a in agents]
        failures += sum(1 for r in reports if not r.passed)

        if args.format == "json":
            print_json(label, reports, scope)
        else:
            print_text(label, reports, scope)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
trace_adk_agents.py - exercise backend/adk_agents.py end to end against the live project.

This is the terminal harness for `backend/adk_agents.py`. It runs the module's
real surface -- roster, catalog governance, glossary, every wrapped golden
query, Memory Bank retrieval, the five channel producers -- against the live
Vertex AI and Dataplex and BigQuery services, and prints a pass/fail line per
stage so every claim the module makes can be checked by reading the output.

Nothing here mocks anything. A stage that cannot reach a service FAILS; it does
not fall back and it does not report a plausible-looking number.

Usage
-----
    set -a; . ./.env; set +a
    PYTHONPATH=. ./.venv/bin/python3 scripts/trace_adk_agents.py                # INFO
    PYTHONPATH=. ./.venv/bin/python3 scripts/trace_adk_agents.py -l DEBUG       # full tree
    PYTHONPATH=. ./.venv/bin/python3 scripts/trace_adk_agents.py -l DEBUG -o catalog
    PYTHONPATH=. ./.venv/bin/python3 scripts/trace_adk_agents.py --list-stages
    PYTHONPATH=. ./.venv/bin/python3 scripts/trace_adk_agents.py -f json > run.jsonl

Exit code is 0 only if every stage that ran passed.
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import pathlib
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend import obs  # noqa: E402

LOG = obs.get_logger("trace")


# ---------------------------------------------------------------------------
# Stage plumbing
# ---------------------------------------------------------------------------

@dataclass
class StageResult:
    name: str
    passed: bool
    summary: str
    elapsed_ms: float = 0.0
    facts: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


STAGES: Dict[str, Callable[["Context"], StageResult]] = {}
STAGE_HELP: Dict[str, str] = {}


def stage(name: str, help_text: str):
    def register(fn: Callable[["Context"], StageResult]):
        STAGES[name] = fn
        STAGE_HELP[name] = help_text
        return fn
    return register


@dataclass
class Context:
    customer_id: str
    card_id: str
    dispute_id: str
    adk: Any = None


def ok(name: str, summary: str, **facts: Any) -> StageResult:
    return StageResult(name=name, passed=True, summary=summary, facts=facts)


def fail(name: str, summary: str, **facts: Any) -> StageResult:
    return StageResult(name=name, passed=False, summary=summary, facts=facts)


# ---------------------------------------------------------------------------
# Stage 1: environment
# ---------------------------------------------------------------------------

REQUIRED_ENV = [
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "VERTEX_AGENT_ENGINE_ID",
]
EXPECTED_ENV = ["BIGQUERY_DATASET", "DATAPLEX_GLOSSARY", "GOOGLE_GENAI_USE_VERTEXAI"]


@stage("env", "Required environment variables are set before anything is imported.")
def stage_env(ctx: Context) -> StageResult:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    soft = [name for name in EXPECTED_ENV if not os.environ.get(name)]
    for name in REQUIRED_ENV + EXPECTED_ENV:
        LOG.debug("%s=%s", name, os.environ.get(name) or "<unset>")
    if missing:
        return fail("env", "missing required: %s (did you `set -a; . ./.env; set +a`?)" % ", ".join(missing))
    if soft:
        LOG.warning("unset but expected: %s", ", ".join(soft))
    return ok(
        "env",
        "project=%s location=%s engine=%s" % (
            os.environ["GOOGLE_CLOUD_PROJECT"],
            os.environ["GOOGLE_CLOUD_LOCATION"],
            os.environ["VERTEX_AGENT_ENGINE_ID"],
        ),
    )


# ---------------------------------------------------------------------------
# Stage 2: prompt-leak scan (the regression guard for the de-hardcoding work)
# ---------------------------------------------------------------------------

#: Strings that must not appear in anything the model reads. Each one is either
#: an answer the demo is supposed to derive, or the name of a thing the agent is
#: supposed to discover. Writing any of them into a prompt means the run proves
#: the model can follow an instruction, not that the catalog governs the agent.
BANNED_IN_PROMPTS = [
    "ssn_last4",
    "pan_full",
    "authorizations_deprecated_v1",
    "MEM00016",
    "Reg E",
    "Reg Z",
    "Regulation E",
    "Regulation Z",
    "is_fraud",
]

#: Deliberate exceptions, each with the reason it is not a leak. An allowlist
#: with no reason attached is just a way to make a scanner stop complaining.
LEAK_ALLOWLIST = {
    # Golden 09 exists only to answer a Regulation E question -- the regulation
    # is the tool's scope, not the conclusion the desk has to reach about a
    # particular dispute. Naming it describes what the tool is for.
    ("golden_09_provisional_credit_due", "Regulation E"),
}


def _prompt_strings(tree: ast.Module) -> List[Dict[str, Any]]:
    """Every string in this module that ends up in front of the model.

    That is: docstrings (ADK publishes a tool function's docstring as its
    description), `instruction=` and `description=` keyword arguments, and
    string parameter defaults (which ADK publishes in the tool schema).

    Deliberately NOT scanned: the golden query contract registry. It names
    columns in `governed_columns_checked` because that is the list of things to
    ask the catalog about -- it is the question, not the answer, and the model
    never sees it.
    """
    found: List[Dict[str, Any]] = []

    for node in ast.walk(tree):
        # Docstrings
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                found.append({
                    "kind": "docstring",
                    "owner": getattr(node, "name", "<module>"),
                    "line": getattr(node, "lineno", 1),
                    "text": doc,
                })
        # instruction= / description= on any call (i.e. LlmAgent(...))
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in ("instruction", "description") and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str):
                    found.append({
                        "kind": kw.arg,
                        "owner": _call_owner(node),
                        "line": kw.value.lineno,
                        "text": kw.value.value,
                    })
        # String parameter defaults, which ADK puts in the tool schema
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
                if isinstance(default, ast.Constant) and isinstance(default.value, str):
                    found.append({
                        "kind": "param_default",
                        "owner": node.name,
                        "line": default.lineno,
                        "text": default.value,
                    })
    return found


def _call_owner(node: ast.Call) -> str:
    for kw in node.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    func = node.func
    return getattr(func, "id", None) or getattr(func, "attr", None) or "<call>"


@stage("leaks", "No governed name or pre-decided outcome appears in any prompt the model reads.")
def stage_leaks(ctx: Context) -> StageResult:
    target = REPO_ROOT / "backend" / "adk_agents.py"
    tree = ast.parse(target.read_text(encoding="utf-8"))

    offenders: List[Dict[str, Any]] = []
    for item in _prompt_strings(tree):
        for banned in BANNED_IN_PROMPTS:
            if banned.lower() not in item["text"].lower():
                continue
            if (item["owner"], banned) in LEAK_ALLOWLIST:
                LOG.debug("allowlisted: %r in %s (%s)", banned, item["owner"], item["kind"])
                continue
            offenders.append({
                "token": banned,
                "kind": item["kind"],
                "owner": item["owner"],
                "line": item["line"],
            })

    for off in offenders:
        LOG.error(
            "prompt leak: %r in %s of %s at adk_agents.py:%d",
            off["token"], off["kind"], off["owner"], off["line"],
        )

    if offenders:
        return fail("leaks", "%d leaked token(s) in model-visible text" % len(offenders), offenders=offenders)
    return ok("leaks", "no governed name or outcome in any model-visible string", allowlisted=len(LEAK_ALLOWLIST))


# ---------------------------------------------------------------------------
# Stage 3: roster
# ---------------------------------------------------------------------------

EXPECTED_AGENTS = [
    "fraud_monitoring_agent",
    "telephony_ivr_agent",
    "mobile_app_agent",
    "web_portal_agent",
    "branch_support_agent",
    "claim_veracity_validator_agent",
    "memory_bank_audit_agent",
    "consumer_credit_synthesizer_agent",
    "dispute_specialist_agent",
]


@stage("roster", "list_all_agents() publishes exactly the 9 expected agents.")
def stage_roster(ctx: Context) -> StageResult:
    descriptors = ctx.adk.list_all_agents()
    names = [d.name if hasattr(d, "name") else d.get("name") for d in descriptors]
    for name in names:
        LOG.debug("roster: %s", name)
    if names != EXPECTED_AGENTS:
        return fail(
            "roster",
            "roster mismatch (got %d)" % len(names),
            missing=[n for n in EXPECTED_AGENTS if n not in names],
            unexpected=[n for n in names if n not in EXPECTED_AGENTS],
            order_differs=sorted(names) == sorted(EXPECTED_AGENTS),
        )
    return ok("roster", "9/9 agents, in the expected order", agents=names)


# ---------------------------------------------------------------------------
# Stage 4: topology
# ---------------------------------------------------------------------------

@stage("topology", "Both agent factories build, and every spoke is transfer-locked to its hub.")
def stage_topology(ctx: Context) -> StageResult:
    synthesizer = ctx.adk.create_consumer_credit_synthesizer_agent()
    orchestrator = ctx.adk.create_ccb_fraud_desk_orchestrator_agent()

    spokes = list(getattr(synthesizer, "sub_agents", []) or [])
    unlocked = [
        s.name for s in spokes
        if not (getattr(s, "disallow_transfer_to_parent", False)
                and getattr(s, "disallow_transfer_to_peers", False))
    ]
    if unlocked:
        return fail("topology", "spokes not transfer-locked: %s" % ", ".join(unlocked))

    # Worth stating plainly in the output, because the docs imply otherwise:
    # building these objects is not the same as running them.
    LOG.warning(
        "topology built but NOT executed - no ADK Runner drives these agents in the "
        "request path; /api/chat is a deterministic Python pipeline"
    )
    return ok(
        "topology",
        "hub=%s spokes=%d (all transfer-locked); orchestrator sub_agents=%d" % (
            synthesizer.name, len(spokes), len(getattr(orchestrator, "sub_agents", []) or []),
        ),
        note="constructed only; not in the request path",
    )


# ---------------------------------------------------------------------------
# Stage 5: catalog discovery + governance
# ---------------------------------------------------------------------------

@stage("catalog", "Sibling tables are DISCOVERED via searchEntries and judged on their own aspects.")
def stage_catalog(ctx: Context) -> StageResult:
    governor = ctx.adk.GLOBAL_CATALOG_GOVERNOR

    siblings = ctx.adk.discover_sibling_tables(governor, "authorizations", use_cache=False)
    LOG.info("discovered alongside 'authorizations': %s", siblings)
    if not siblings:
        return fail("catalog", "searchEntries returned no sibling tables; discovery proved nothing")

    verdicts = {}
    for table in siblings:
        decision = governor.check_table(table)
        verdicts[table] = {
            "action": decision.action,
            "allowed": decision.allowed,
            "evidence": decision.evidence,
            "replacement": decision.replacement,
        }
        LOG.info("  %-32s %-8s %s", table, decision.action, decision.evidence)

    refused = [t for t, v in verdicts.items() if not v["allowed"]]
    if not refused:
        return fail(
            "catalog",
            "every discovered sibling was ALLOWED; the deprecated-table trap did not fire",
            verdicts=verdicts,
        )

    # The trap column, reached the same way: by asking, not by being told.
    column_verdicts = {}
    for table, column in (("customers", "ssn_last4"), ("cards", "pan_full"), ("cards", "pan_masked")):
        decision = governor.check_column(table, column)
        column_verdicts["%s.%s" % (table, column)] = {
            "action": decision.action,
            "allowed": decision.allowed,
            "evidence": decision.evidence,
        }
        LOG.info("  %-32s %-8s %s", "%s.%s" % (table, column), decision.action, decision.evidence)

    refused_columns = [c for c, v in column_verdicts.items() if not v["allowed"]]
    if not refused_columns:
        return fail("catalog", "no column was refused; sensitivity aspects are not being honoured",
                    columns=column_verdicts)

    return ok(
        "catalog",
        "discovered %d siblings, refused %s; refused columns %s" % (
            len(siblings), refused, refused_columns,
        ),
        siblings=siblings, tables=verdicts, columns=column_verdicts,
    )


# ---------------------------------------------------------------------------
# Stage 6: glossary
# ---------------------------------------------------------------------------

GLOSSARY_TERMS = ["provisional-credit", "regulation-e", "regulation-z", "chargeback"]


@stage("glossary", "Dataplex glossary terms resolve, so the desk can cite a definition it did not invent.")
def stage_glossary(ctx: Context) -> StageResult:
    resolved, missing = {}, []
    for term_id in GLOSSARY_TERMS:
        try:
            term = ctx.adk.catalog_lookup_glossary_term_tool(term_id)
        except Exception as exc:  # noqa: BLE001 - a failed lookup is a stage failure, not a crash
            LOG.warning("glossary lookup %s failed: %s", term_id, exc)
            missing.append(term_id)
            continue
        text = term.get("description") or term.get("definition") or ""
        if not text:
            missing.append(term_id)
            continue
        resolved[term_id] = (text[:110] + "...") if len(text) > 110 else text
        LOG.info("  %-22s %s", term_id, resolved[term_id])

    if missing:
        return fail("glossary", "unresolved terms: %s" % ", ".join(missing), resolved=resolved)
    return ok("glossary", "%d/%d terms resolved" % (len(resolved), len(GLOSSARY_TERMS)), terms=resolved)


# ---------------------------------------------------------------------------
# Stage 7: golden queries
# ---------------------------------------------------------------------------

def _statement_account(ctx: Context) -> str:
    """The account behind the demo card, read through golden 14 so the harness names no account id."""
    cards = ctx.adk.golden_14_customer_cards_and_wallets(customer_id=ctx.customer_id).get("rows") or []
    account = next((c["account_id"] for c in cards if c.get("card_id") == ctx.card_id), None)
    if not account:
        raise LookupError("card %s is not on %s's record, so there is no statement to read" % (ctx.card_id, ctx.customer_id))
    return account


def _golden_calls(ctx: Context) -> List[tuple]:
    a = ctx.adk
    return [
        ("01", lambda: a.golden_01_customer_360_snapshot(customer_id=ctx.customer_id)),
        ("02", lambda: a.golden_02_recent_authorizations_for_card(card_id=ctx.card_id)),
        ("03", lambda: a.golden_03_suspicious_activity_on_card_window(card_id=ctx.card_id)),
        ("04", lambda: a.golden_04_geo_impossible_travel(card_id=ctx.card_id)),
        ("07", lambda: a.golden_07_open_disputes_for_customer(customer_id=ctx.customer_id)),
        ("08", lambda: a.golden_08_dispute_filing_window_check(dispute_id=ctx.dispute_id)),
        ("09", lambda: a.golden_09_provisional_credit_due()),
        ("10", lambda: a.golden_10_wallet_tokens_active_on_card(card_id=ctx.card_id)),
        ("11", lambda: a.golden_11_dispute_event_timeline(dispute_id=ctx.dispute_id)),
        ("13", lambda: a.golden_13_customer_accounts_and_balances(customer_id=ctx.customer_id)),
        ("14", lambda: a.golden_14_customer_cards_and_wallets(customer_id=ctx.customer_id)),
        ("15", lambda: a.golden_15_spend_summary(customer_id=ctx.customer_id)),
        ("16", lambda: a.golden_16_statement_transactions(
            customer_id=ctx.customer_id, account_id=_statement_account(ctx))),
        ("17", lambda: a.golden_17_card_replacement_tracking(customer_id=ctx.customer_id)),
        ("18", lambda: a.golden_18_customer_contact_profile(customer_id=ctx.customer_id)),
        ("19", lambda: a.golden_19_travel_notices_for_customer(customer_id=ctx.customer_id)),
    ]


UNWRAPPED_GOLDEN = ["05", "06", "12"]


@stage("golden", "Every wrapped golden query executes with bound parameters and no ad-hoc SQL.")
def stage_golden(ctx: Context) -> StageResult:
    results, failures = {}, []
    calls = _golden_calls(ctx)
    for query_id, call in calls:
        started = time.perf_counter()
        try:
            payload = call()
        except Exception as exc:  # noqa: BLE001 - collected, so one failure does not hide the rest
            LOG.error("golden %s raised %s: %s", query_id, type(exc).__name__, exc)
            failures.append(query_id)
            results[query_id] = {"error": "%s: %s" % (type(exc).__name__, exc)}
            continue

        elapsed = (time.perf_counter() - started) * 1000
        if payload.get("allowed") is False:
            LOG.error("golden %s was refused: %s", query_id, payload.get("error"))
            failures.append(query_id)
            results[query_id] = {"refused": payload.get("error")}
            continue

        if payload.get("ad_hoc_sql_used") is not False:
            LOG.error("golden %s did not report ad_hoc_sql_used=False", query_id)
            failures.append(query_id)

        refusals = [r["subject"] for r in payload.get("voluntary_refusals_logged", [])]
        results[query_id] = {
            "rows": payload.get("row_count"),
            "bound": list((payload.get("parameters_bound") or {}).keys()),
            "refusals": refusals,
            "ms": round(elapsed, 1),
        }
        LOG.info(
            "  golden %s rows=%-4s bound=%-42s refused=%s",
            query_id, payload.get("row_count"),
            ",".join(results[query_id]["bound"]) or "-",
            ",".join(refusals) or "-",
        )

    LOG.warning(
        "golden %s have no tool wrapper and are unreachable by any agent",
        "/".join(UNWRAPPED_GOLDEN),
    )

    # A new contract with no call here would otherwise pass this stage without ever running.
    unexercised = sorted(set(ctx.adk.GOLDEN_QUERY_CONTRACTS) - {q for q, _ in calls} - set(UNWRAPPED_GOLDEN))
    if unexercised:
        LOG.error("golden %s have a contract but no call in this stage", "/".join(unexercised))
        failures.extend(unexercised)

    if failures:
        return fail("golden", "failed: %s" % ", ".join(failures), results=results)
    return ok(
        "golden",
        "%d/%d executed; %d reported a voluntary refusal" % (
            len(results), len(calls), sum(1 for r in results.values() if r["refusals"])),
        results=results, unwrapped=UNWRAPPED_GOLDEN,
    )


# ---------------------------------------------------------------------------
# Stage 8: negative authorization test
# ---------------------------------------------------------------------------

@stage("authz", "An agent not listed in INDEX.md for a query is refused before BigQuery is touched.")
def stage_authz(ctx: Context) -> StageResult:
    contract = ctx.adk.GOLDEN_QUERY_CONTRACTS["09"]
    intruder = next(
        (a for a in ctx.adk.AgentId if a not in contract.authorized_agents),
        None,
    )
    if intruder is None:
        return fail("authz", "every AgentId is authorized for query 09; the check is untestable")

    payload = ctx.adk.execute_golden_query_contract(
        query_id="09", calling_agent=intruder, params={},
    )
    if payload.get("allowed") is not False:
        return fail("authz", "%s was NOT refused for query 09" % intruder.value, payload=payload)

    LOG.info("  %s correctly refused: %s", intruder.value, payload.get("error", "")[:120])
    return ok("authz", "%s refused for query 09 as INDEX.md requires" % intruder.value)


# ---------------------------------------------------------------------------
# Stage 9: Memory Bank
# ---------------------------------------------------------------------------

@stage("memory", "Memory Bank returns scoped facts for the customer via memories:retrieve.")
def stage_memory(ctx: Context) -> StageResult:
    payload = ctx.adk.retrieve_customer_memories_tool(
        customer_id=ctx.customer_id,
        query="What happened to this customer's cards and what standing instructions exist?",
    )
    count = payload.get("count", 0)
    for memory in payload.get("memories", [])[:8]:
        LOG.info("  fact: %s", str(memory.get("fact"))[:150])
    if count == 0:
        return fail("memory", "0 memories for scope %s; the corpus is empty or the scope is wrong"
                    % payload.get("scope"))
    return ok("memory", "%d facts for scope %s" % (count, payload.get("scope")),
              engine=payload.get("engine_parent"), count=count)


# ---------------------------------------------------------------------------
# Stage: memory topic vocabulary
# ---------------------------------------------------------------------------

@stage("topics", "The Memory Bank topic vocabulary is readable from the live engine and actually used.")
def stage_topics(ctx: Context) -> StageResult:
    payload = ctx.adk.list_memory_topics_tool()
    topics = payload.get("topics", [])
    for topic in topics:
        LOG.info("  %-26s %-8s %s", topic["label"], topic["kind"], str(topic["description"])[:90])

    if not topics:
        return fail("topics", "engine reports no topic vocabulary; extraction is unconstrained")

    # A vocabulary nothing is filed under is decoration. Check the stored
    # memories actually carry these labels, and report any label in use that the
    # engine does not declare -- that would mean the config drifted from reality.
    from backend.memory_bank import topic_labels as _labels
    declared = {t["label"] for t in topics}
    in_use: Dict[str, int] = {}
    untagged = 0
    memories = ctx.adk.GLOBAL_MEMORY_BANK_CLIENT.list_memories()
    for memory in memories:
        labels = _labels(memory)
        if not labels:
            untagged += 1
        for label in labels:
            in_use[label] = in_use.get(label, 0) + 1

    undeclared = sorted(set(in_use) - declared)
    unused = sorted(declared - set(in_use))
    LOG.info("  %d memories, %d untagged, %d distinct labels in use", len(memories), untagged, len(in_use))
    for label, count in sorted(in_use.items(), key=lambda kv: -kv[1]):
        LOG.info("    %-26s %d", label, count)
    if unused:
        LOG.warning("declared but no memory filed under them: %s", ", ".join(unused))
    if undeclared:
        LOG.error("memories carry labels the engine does not declare: %s", ", ".join(undeclared))
        return fail("topics", "undeclared labels in use: %s" % ", ".join(undeclared),
                    declared=sorted(declared), in_use=in_use)

    return ok(
        "topics",
        "%d topics declared, %d in use across %d memories (%d untagged)" % (
            len(topics), len(in_use), len(memories), untagged,
        ),
        declared=sorted(declared), in_use=in_use, unused=unused,
    )


# ---------------------------------------------------------------------------
# Stage 10: channel producers
# ---------------------------------------------------------------------------

@stage("channels", "All 5 channel producers read their own slice of the seeded Memory Bank sessions.")
def stage_channels(ctx: Context) -> StageResult:
    a = ctx.adk
    producers = [
        ("FRAUD_DETECTION", a.query_fraud_velocity_alerts),
        ("TELEPHONY_IVR", a.query_ivr_call_records),
        ("MOBILE_APP", a.query_mobile_wallet_events),
        ("WEB_PORTAL", a.query_web_portal_activity),
        ("BRANCH_SUPPORT", a.query_branch_teller_interactions),
    ]
    counts, empty = {}, []
    for label, produce in producers:
        payload = produce(customer_id=ctx.customer_id, include_events=False)
        sessions = (payload.get("session_count") or {}).get("value", 0)
        memories = (payload.get("memory_count") or {}).get("value", 0)
        counts[label] = {"sessions": sessions, "memories": memories}
        LOG.info("  %-16s sessions=%-3s memories=%s", label, sessions, memories)
        if not sessions:
            empty.append(label)

    if empty:
        # Empty is a legitimate result -- but if every channel is empty the
        # scope is wrong, and that is a failure rather than a finding.
        if len(empty) == len(producers):
            return fail("channels", "all 5 producers matched 0 sessions; check the customer scope",
                        counts=counts)
        LOG.warning("channels with no seeded sessions for %s: %s", ctx.customer_id, ", ".join(empty))

    return ok("channels", "%d/5 producers found sessions" % (len(producers) - len(empty)), counts=counts)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

DEFAULT_ORDER = ["env", "leaks", "roster", "topology", "catalog", "glossary", "golden", "authz", "topics", "memory", "channels"]

#: Stages that do not need a live service. Useful when you are iterating on the
#: prompts and do not want to wait for BigQuery.
OFFLINE_STAGES = {"env", "leaks"}


def run(ctx: Context, names: List[str]) -> List[StageResult]:
    results: List[StageResult] = []
    for name in names:
        fn = STAGES[name]
        started = time.perf_counter()
        try:
            with obs.turn("stage.%s" % name, LOG):
                result = fn(ctx)
        except Exception as exc:  # noqa: BLE001 - a crashed stage is a failed stage
            result = StageResult(
                name=name, passed=False,
                summary="raised %s" % type(exc).__name__,
                error="%s: %s" % (type(exc).__name__, exc),
            )
            LOG.error("stage %s crashed", name, exc_info=True)
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        results.append(result)

        # env failing means nothing downstream can be trusted, so stop.
        if name == "env" and not result.passed:
            LOG.error("environment is not usable; skipping the remaining stages")
            break
    return results


def report(results: List[StageResult]) -> bool:
    width = max((len(r.name) for r in results), default=8)
    print("\n" + "=" * 100)
    print("ADK AGENTS END-TO-END TRACE")
    print("=" * 100)
    for r in results:
        print("%-7s %-*s %8.0fms  %s" % (
            "PASS" if r.passed else "FAIL", width, r.name, r.elapsed_ms, r.summary,
        ))
        if r.error:
            print("        %s" % r.error)
    passed = sum(1 for r in results if r.passed)
    print("-" * 100)
    print("%d/%d stages passed" % (passed, len(results)))
    print("=" * 100)
    return passed == len(results)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-l", "--level", default=os.environ.get("CCB_LOG_LEVEL", "INFO"),
                        help="TRACE, DEBUG, INFO, WARNING, ERROR (default: INFO)")
    parser.add_argument("-f", "--format", default=os.environ.get("CCB_LOG_FORMAT", "human"),
                        choices=["human", "json"])
    parser.add_argument("-o", "--only", action="append", choices=DEFAULT_ORDER,
                        help="run only these stages (repeatable)")
    parser.add_argument("-s", "--skip", action="append", choices=DEFAULT_ORDER,
                        help="skip these stages (repeatable)")
    parser.add_argument("--offline", action="store_true",
                        help="run only the stages that need no live service (%s)" % ", ".join(sorted(OFFLINE_STAGES)))
    parser.add_argument("--list-stages", action="store_true", help="describe the stages and exit")
    parser.add_argument("--customer", default=os.environ.get("DEMO_CUSTOMER_ID", "CUST00001"))
    parser.add_argument("--card", default=os.environ.get("DEMO_CARD_ID", "CARD00002"))
    parser.add_argument("--dispute", default=os.environ.get("DEMO_DISPUTE_ID", "DSP00034"))
    parser.add_argument("--json-report", metavar="PATH", help="also write the full result set as JSON")
    args = parser.parse_args(argv)

    if args.list_stages:
        width = max(len(n) for n in DEFAULT_ORDER)
        for name in DEFAULT_ORDER:
            marker = " (offline)" if name in OFFLINE_STAGES else ""
            print("  %-*s  %s%s" % (width, name, STAGE_HELP[name], marker))
        return 0

    obs.configure(level=args.level, fmt=args.format, force=True)

    selected = list(args.only or DEFAULT_ORDER)
    if args.offline:
        selected = [n for n in selected if n in OFFLINE_STAGES]
    for skipped in args.skip or []:
        if skipped in selected:
            selected.remove(skipped)
    if not selected:
        print("no stages selected", file=sys.stderr)
        return 2

    ctx = Context(customer_id=args.customer, card_id=args.card, dispute_id=args.dispute)

    # Imported after logging is configured, so the module's import-time DEBUG
    # line about which engine and dataset it bound to actually reaches a handler.
    if any(n not in OFFLINE_STAGES for n in selected):
        LOG.debug("importing backend.adk_agents (this builds the live singletons)")
        import backend.adk_agents as adk  # noqa: PLC0415 - deliberate late import
        ctx.adk = adk
    elif "leaks" in selected or "env" in selected:
        LOG.debug("offline stages only; backend.adk_agents will not be imported")

    LOG.info("customer=%s card=%s dispute=%s stages=%s",
             ctx.customer_id, ctx.card_id, ctx.dispute_id, ",".join(selected))

    with obs.run_context():
        results = run(ctx, selected)

    everything_passed = report(results)

    if args.json_report:
        path = pathlib.Path(args.json_report)
        path.write_text(json.dumps([r.__dict__ for r in results], indent=2, default=str), encoding="utf-8")
        print("wrote %s" % path)

    return 0 if everything_passed else 1


if __name__ == "__main__":
    sys.exit(main())

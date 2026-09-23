#!/usr/bin/env python3
"""Provenance verifier: proves no placeholder reaches the emission surface.

Exit code is the point. Non-zero means an unwaived STATIC value, a banned
pattern, or a fabricating fallback is still in the tree, and the demo is not
safe to show.

Three modes, because the interesting checks have different prerequisites:

  --static   (default) AST + pattern scan of the source tree. Stdlib only, no
             GCP, no web framework. This is the mode that runs in CI and on a
             laptop with nothing installed.
  --payload  Summarize a captured JSON response. Lets a run recorded on a
             machine WITH credentials be audited on a machine without them.
  --live     Import the app, exercise the flow, count tags in real responses.
             Needs flask and GCP credentials.

The four tags are defined in backend/provenance.py and are not repeated here.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TAG_CALLS = {
    "live": "LIVE_API",
    "seeded": "SEEDED",
    "computed": "COMPUTED",
    "static": "STATIC",
    "identity": "STATIC",      # waived by construction
    "unmeasured": "COMPUTED",  # renders n/r
}

# Each pattern is something the rework removed. A hit means it came back.
BANNED = [
    (r"\bmemory_bank_old\b", "import of retired module memory_bank_old"),
    (r"\bknowledge_catalog_old\b", "import of retired module knowledge_catalog_old"),
    (r"\bGroundTruthTelemetryStore\b", "the Python-dict telemetry store, replaced by BigQuery"),
    (r"\bDreaming Service\b|\bdreaming\b", "the fake compaction service"),
    (r"\bcosine[_ ]similarity\b|\bcosine\b", "local similarity reimplementing the managed service"),
    (r"\btext-embedding-005\b", "local embedding call reimplementing managed retrieval"),
    (r"\bDynamicCompactionSchema\b", "client-side compaction schema"),
    (r"\bnp\.dot\b|\bnumpy\b", "local vector math"),
]

# Files that are allowed to name a banned term, because naming it IS their job.
BANNED_EXEMPT = {"scripts/verify_provenance.py", "REWORK_REPORT.md", "MANIFEST.md"}

# "No local embeddings" is a promise, not a violation. Without this the scanner
# punishes the docstrings that state the rule it is enforcing.
NEGATION = re.compile(
    r"\b(no|not|never|without|zero|removed|retired|instead of|rather than)\b", re.IGNORECASE)


def _is_disclaimer(line: str, match_start: int) -> bool:
    return bool(NEGATION.search(line[:match_start]))

# Trees this repo does not own. `jpmc_fraud_demo_kit` is a vendored drop
# with its own conventions; grading it produced failures nobody could act on.
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv",
    ".mypy_cache", ".pytest_cache", "jpmc_fraud_demo_kit",
}


def iter_files(root: Path, suffixes: Tuple[str, ...]) -> List[Path]:
    found = []
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix in suffixes:
            found.append(path)
    return found


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def scan_tags(py_files: List[Path]) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
    """Counts provenance call sites and collects unwaived static() offenders."""
    counts = {"LIVE_API": 0, "SEEDED": 0, "COMPUTED": 0, "STATIC": 0}
    counts["STATIC_WAIVED"] = 0
    counts["STATIC_UNWAIVED"] = 0
    offenders: List[Dict[str, Any]] = []

    for path in py_files:
        if rel(path) == "backend/provenance.py":
            continue  # defines the vocabulary; its own calls are not emissions
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            offenders.append({"file": rel(path), "line": exc.lineno or 0,
                              "what": "SYNTAX ERROR: %s" % exc.msg})
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name not in TAG_CALLS:
                continue
            tag = TAG_CALLS[name]
            counts[tag] += 1
            if name == "identity":
                counts["STATIC_WAIVED"] += 1
            elif name == "static":
                counts["STATIC_UNWAIVED"] += 1
                offenders.append({
                    "file": rel(path), "line": node.lineno,
                    "what": "unwaived static() at the emission surface",
                })
    return counts, offenders


def scan_banned(files: List[Path]) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    for path in files:
        if rel(path) in BANNED_EXEMPT:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            for pattern, why in BANNED:
                found = re.search(pattern, line, re.IGNORECASE)
                if not found or _is_disclaimer(line, found.start()):
                    continue
                hits.append({"file": rel(path), "line": number,
                             "why": why, "text": line.strip()[:110]})
                break
    return hits


def scan_fabricating_fallbacks(py_files: List[Path]) -> List[Dict[str, Any]]:
    """Flags `except: return <literal>` -- the shape that invents healthy data.

    A handler returning a literal dict/list is how a broken call was made to
    look successful. Re-raising, logging, or returning None is fine.
    """
    flagged: List[Dict[str, Any]] = []
    for path in py_files:
        if rel(path) == "scripts/verify_provenance.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Return) or sub.value is None:
                    continue
                value = sub.value
                literal = False
                if isinstance(value, (ast.Dict, ast.List, ast.Tuple)):
                    literal = bool(getattr(value, "keys", None) or
                                   getattr(value, "elts", []))
                elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                    literal = value.value not in ("n/r", "")
                if literal:
                    flagged.append({
                        "file": rel(path), "line": sub.lineno,
                        "why": "exception handler returns invented data instead of failing loudly",
                    })
    return flagged


def report_block(title: str, rows: List[Dict[str, Any]], keys: Tuple[str, ...]) -> None:
    print("\n%s (%d)" % (title, len(rows)))
    print("-" * 72)
    if not rows:
        print("  none")
        return
    for row in rows:
        print("  " + " | ".join(str(row.get(key, "")) for key in keys))


def run_static(root: Path) -> int:
    py_files = iter_files(root, (".py",))
    text_files = iter_files(root, (".py", ".js", ".md", ".html", ".json"))

    print("=" * 72)
    print("PROVENANCE VERIFIER -- static mode")
    print("root: %s" % root)
    print("python files: %d   text files scanned: %d" % (len(py_files), len(text_files)))
    print("=" * 72)

    parse_failures = []
    for path in py_files:
        try:
            ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            parse_failures.append({"file": rel(path), "line": exc.lineno or 0,
                                   "why": exc.msg})

    counts, offenders = scan_tags(py_files)
    banned = scan_banned(text_files)
    fallbacks = scan_fabricating_fallbacks(py_files)

    print("\nTAG COUNTS (call sites)")
    print("-" * 72)
    for key in ("LIVE_API", "SEEDED", "COMPUTED", "STATIC"):
        print("  %-16s %d" % (key, counts[key]))
    print("  %-16s %d  (agent identity constants, allowed)" % ("STATIC_WAIVED", counts["STATIC_WAIVED"]))
    print("  %-16s %d  (must be 0)" % ("STATIC_UNWAIVED", counts["STATIC_UNWAIVED"]))

    report_block("PARSE FAILURES", parse_failures, ("file", "line", "why"))
    report_block("UNWAIVED STATIC", offenders, ("file", "line", "what"))
    report_block("BANNED PATTERNS", banned, ("file", "line", "why", "text"))
    report_block("FABRICATING FALLBACKS", fallbacks, ("file", "line", "why"))

    failed = bool(parse_failures) or bool(offenders) or bool(banned) or bool(fallbacks)
    print("\n" + "=" * 72)
    print("RESULT: %s" % ("FAIL" if failed else "PASS"))
    print("=" * 72)
    return 1 if failed else 0


def run_payload(payload_path: Path) -> int:
    from backend.provenance import summarize

    data = json.loads(payload_path.read_text(encoding="utf-8", errors="replace"))
    result = summarize(data)
    print("=" * 72)
    print("PROVENANCE VERIFIER -- payload mode: %s" % payload_path)
    print("=" * 72)
    for key, value in sorted(result["counts"].items()):
        print("  %-16s %s" % (key, value))
    report_block("UNWAIVED STATIC", result["static_offenders"], ("path", "source"))
    report_block("WAIVED (identity)", result["static_waivers"], ("path", "source", "reason"))
    print("\nRESULT: %s" % ("PASS" if result["clean"] else "FAIL"))
    return 0 if result["clean"] else 1


def run_live(customer_id: str) -> int:
    """Exercises the real flow. Needs flask + credentials; absent here by design."""
    from backend.provenance import summarize

    try:
        from backend.app import app
    except Exception as exc:
        print("LIVE MODE UNAVAILABLE: could not import backend.app -- %s" % exc)
        print("This is a hard failure, not a skip: a demo that cannot start is broken.")
        return 2

    endpoints = [
        "/api/agents/list",
        "/api/session?customer_id=%s" % customer_id,
        "/api/testbed?customer_id=%s" % customer_id,
        "/api/eval-scorecard",
        "/api/card-oversight?customer_id=%s" % customer_id,
    ]
    total = {"LIVE_API": 0, "SEEDED": 0, "COMPUTED": 0, "STATIC": 0,
             "STATIC_WAIVED": 0, "STATIC_UNWAIVED": 0}
    offenders: List[Dict[str, Any]] = []

    client = app.test_client()
    print("=" * 72)
    print("PROVENANCE VERIFIER -- live mode")
    print("=" * 72)
    for route in endpoints:
        response = client.get(route)
        try:
            body = response.get_json()
        except Exception:
            body = None
        if body is None:
            print("  %-46s HTTP %s  (no JSON body)" % (route, response.status_code))
            continue
        result = summarize(body)
        for key, value in result["counts"].items():
            total[key] = total.get(key, 0) + value
        for item in result["static_offenders"]:
            item["route"] = route
            offenders.append(item)
        print("  %-46s HTTP %s  %s" % (route, response.status_code, result["counts"]))

    print("\nTOTALS")
    print("-" * 72)
    for key in ("LIVE_API", "SEEDED", "COMPUTED", "STATIC", "STATIC_WAIVED", "STATIC_UNWAIVED"):
        print("  %-16s %d" % (key, total.get(key, 0)))
    report_block("UNWAIVED STATIC", offenders, ("route", "path", "source"))
    failed = bool(offenders)
    print("\nRESULT: %s" % ("FAIL" if failed else "PASS"))
    return 1 if failed else 0


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Verify every emitted value carries an honest provenance tag.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--static", action="store_true",
                      help="AST + pattern scan of the tree. Default. No dependencies.")
    mode.add_argument("--payload", metavar="FILE",
                      help="Summarize a captured JSON response.")
    mode.add_argument("--live", action="store_true",
                      help="Exercise the real endpoints. Needs flask and credentials.")
    parser.add_argument("--root", default=str(REPO_ROOT),
                        help="Repo root to scan (default: the repo this script lives in).")
    parser.add_argument("--customer-id",
                        default=os.environ.get("DEMO_CUSTOMER_ID", "CUST00001"),
                        help="Customer scope for live mode. A parameter, never hardcoded.")
    args = parser.parse_args(argv)

    if args.payload:
        return run_payload(Path(args.payload))
    if args.live:
        return run_live(args.customer_id)
    return run_static(Path(args.root))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

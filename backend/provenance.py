"""Provenance tagging for every value the system emits.

The demo previously mixed real API results with literals invented to make a
broken run look healthy. There was no way to tell them apart from the outside,
so this module makes the distinction structural rather than a matter of trust:
a value carries its origin, and the verifier counts origins.

Four tags, no others:

    LIVE_API  a real Google API call in THIS process returned it
    SEEDED    read from the verified synthetic dataset (BigQuery / Memory Bank)
    COMPUTED  derived in-process purely from LIVE_API or SEEDED inputs
    STATIC    a literal from source code

STATIC is the failure condition. The verifier exits non-zero when any unwaived
STATIC value reaches the emission surface.

One narrow exception exists. An agent's own channel name is a constant because
it IS the agent's identity, not because anybody is faking data. Those are
declared with `identity()`, which records the reason and is counted and printed
separately so a reader can audit the waiver list rather than take it on faith.

Stdlib only, on purpose: the verifier has to run in environments that have no
GCP SDK and no web framework installed.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional

LIVE_API = "LIVE_API"
SEEDED = "SEEDED"
COMPUTED = "COMPUTED"
STATIC = "STATIC"

VALID_TAGS = (LIVE_API, SEEDED, COMPUTED, STATIC)

# Rendered for anything genuinely unmeasured. Never a zero, never a plausible
# default: a wrong number is harder to notice than a missing one.
NOT_RECORDED = "n/r"

PROVENANCE_KEY = "_provenance"


class ProvenanceError(ValueError):
    """Raised when a value is tagged with something outside the vocabulary."""


def tag(
    value: Any,
    origin: str,
    source: str,
    *,
    waived: bool = False,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Wraps a value with where it came from.

    `source` must name the concrete thing that produced it -- a table, an API
    method, a memory id -- because "SEEDED" alone is not auditable.
    """
    if origin not in VALID_TAGS:
        raise ProvenanceError(
            "unknown provenance tag %r; allowed: %s" % (origin, ", ".join(VALID_TAGS))
        )
    if not source:
        raise ProvenanceError("provenance requires a named source, got empty string")
    record: Dict[str, Any] = {"tag": origin, "source": source}
    if waived:
        record["waived"] = True
        record["reason"] = reason or "unspecified"
    return {"value": value, PROVENANCE_KEY: record}


def live(value: Any, source: str) -> Dict[str, Any]:
    return tag(value, LIVE_API, source)


def seeded(value: Any, source: str) -> Dict[str, Any]:
    return tag(value, SEEDED, source)


def computed(value: Any, source: str) -> Dict[str, Any]:
    return tag(value, COMPUTED, source)


def static(value: Any, source: str) -> Dict[str, Any]:
    """A source literal. Counted as a failure unless you meant `identity()`."""
    return tag(value, STATIC, source)


def identity(value: Any, source: str, reason: str) -> Dict[str, Any]:
    """The one legitimate constant: a value that IS the agent's identity.

    A channel producer's channel name qualifies. Its returned rows do not.
    """
    return tag(value, STATIC, source, waived=True, reason=reason)


def unmeasured(source: str) -> Dict[str, Any]:
    """Something we did not measure. Renders as "n/r", never as a number."""
    return tag(NOT_RECORDED, COMPUTED, source)


def value_of(item: Any) -> Any:
    """Unwraps a tagged value; passes untagged values straight through."""
    if isinstance(item, dict) and PROVENANCE_KEY in item and "value" in item:
        return item["value"]
    return item


def is_tagged(item: Any) -> bool:
    return isinstance(item, dict) and PROVENANCE_KEY in item and "value" in item


def walk(payload: Any, path: str = "$") -> Iterable[Dict[str, Any]]:
    """Yields every provenance record in a nested payload, with its json path."""
    if is_tagged(payload):
        record = dict(payload[PROVENANCE_KEY])
        record["path"] = path
        yield record
        yield from walk(payload["value"], path + ".value")
        return
    if isinstance(payload, dict):
        for key, sub in payload.items():
            yield from walk(sub, "%s.%s" % (path, key))
    elif isinstance(payload, (list, tuple)):
        for index, sub in enumerate(payload):
            yield from walk(sub, "%s[%d]" % (path, index))


def summarize(payload: Any) -> Dict[str, Any]:
    """Counts tags across a payload and lists the STATIC offenders by path."""
    counts = {name: 0 for name in VALID_TAGS}
    waived = 0
    offenders: List[Dict[str, Any]] = []
    waivers: List[Dict[str, Any]] = []
    for record in walk(payload):
        counts[record["tag"]] += 1
        if record["tag"] != STATIC:
            continue
        if record.get("waived"):
            waived += 1
            waivers.append(record)
        else:
            offenders.append(record)
    counts["STATIC_WAIVED"] = waived
    counts["STATIC_UNWAIVED"] = len(offenders)
    return {
        "counts": counts,
        "static_offenders": offenders,
        "static_waivers": waivers,
        "clean": not offenders,
    }


def env_param(name: str, default: Optional[str] = None) -> Optional[str]:
    """Reads a deployment parameter. Project and engine ids live here, not in code."""
    return os.environ.get(name, default)


def require_env(name: str) -> str:
    """Fails loudly for a parameter with no safe default.

    Returning a placeholder project id would produce a run that looks healthy
    and silently talks to nothing, which is the outcome this rework exists to
    remove.
    """
    raw = os.environ.get(name)
    if not raw:
        raise ProvenanceError(
            "%s is not set. Set it to your own value; this kit ships no default "
            "project, engine, or dataset id." % name
        )
    return raw

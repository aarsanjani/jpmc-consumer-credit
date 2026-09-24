"""Verified write path: session -> veracity check -> generate -> lineage.

Every stored session gets its memories generated into one of two scopes,
decided by the veracity verdict on what the customer said:

  * trusted     {"customer_id": X}                         - what agents read
  * quarantine  {"customer_id": X, "corpus": "quarantine"} - kept for inspection

Scope matching in Memory Bank is exact, so an agent reading
{"customer_id": X} never sees a quarantined memory.

Memory Bank does not record which session a memory came from, so after each
generate we append a state-only event to the session listing the memories it
produced. That is what the lineage lookups below read back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from backend import obs
from backend.memory_bank import (
    CUSTOMER_AUTHORS,
    NOT_RECORDED,
    MemoryBankClient,
    generated_memories,
)
from backend.models import VeracityStatus

LOG = obs.get_logger(__name__)

QUARANTINE_CORPUS = "quarantine"
LINEAGE_AUTHOR = "memory_bank_lineage"
LINEAGE_STATE_KEY = "memory_lineage"

# PARTIALLY_VERIFIED_CONSENT_DISPUTE stays trusted on purpose: telemetry backs
# the transactions, and the customer contesting consent is itself something
# the next agent needs to know.
QUARANTINE_VERDICTS = frozenset({
    VeracityStatus.CONTRADICTED_BY_TELEMETRY.value,
    VeracityStatus.SUSPICIOUS_FALSE_CLAIM.value,
})

# Takes the customer's own words, returns a VeracityStatus value.
Verifier = Callable[[str], str]


def trusted_scope(customer_id: str) -> dict[str, str]:
    return {"customer_id": customer_id}


def quarantine_scope(customer_id: str) -> dict[str, str]:
    return {"customer_id": customer_id, "corpus": QUARANTINE_CORPUS}


def scope_for_verdict(customer_id: str, verdict: str | None) -> dict[str, str]:
    if verdict in QUARANTINE_VERDICTS:
        return quarantine_scope(customer_id)
    return trusted_scope(customer_id)


def customer_text(turns: list[dict[str, str]]) -> str:
    return "\n".join(t["text"] for t in turns if t.get("author") in CUSTOMER_AUTHORS and t.get("text"))


def generate_with_lineage(
    client: MemoryBankClient,
    session_name: str,
    customer_id: str,
    verdict: str | None,
) -> dict[str, Any]:
    """Generates memories from a stored session and stamps the result on it."""
    scope = scope_for_verdict(customer_id, verdict)
    with obs.turn("memory.generate_with_lineage", LOG,
                  scope="quarantine" if "corpus" in scope else "trusted",
                  verdict=verdict or NOT_RECORDED) as span:
        operation = client.generate_from_session(session_name, scope, wait=True)
        lineage = {
            "scope": scope,
            "verdict": verdict or NOT_RECORDED,
            "operation": operation.get("name", NOT_RECORDED),
            "generated": generated_memories(operation),
        }
        client.append_state_delta(session_name, LINEAGE_AUTHOR, {LINEAGE_STATE_KEY: lineage})
        span.detail(generated=len(lineage["generated"]))
    return lineage


def verify_and_generate(
    client: MemoryBankClient,
    session_name: str,
    customer_id: str,
    turns: list[dict[str, str]],
    verify: Verifier,
) -> dict[str, Any]:
    """Runs the veracity check on the customer's turns, then generates.

    A session with no customer turns (a bank-side or system event) has nothing
    to verify, so it goes straight to the trusted scope with verdict n/r.
    A failed check is treated the same way as no check rather than guessed;
    the error is returned so the caller can show it.
    """
    said = customer_text(turns)
    verdict: str | None = None
    check_error = None
    if said:
        try:
            verdict = verify(said)
        except Exception as exc:  # noqa: BLE001 - a failed check must not block the write
            check_error = "%s: %s" % (type(exc).__name__, exc)
            LOG.warning("Veracity check failed for %s: %s", session_name, check_error)
    lineage = generate_with_lineage(client, session_name, customer_id, verdict)
    lineage["verified_text"] = said or NOT_RECORDED
    if check_error:
        lineage["check_error"] = check_error
    return lineage


def lineage_for_session(client: MemoryBankClient, session_name: str) -> dict[str, Any] | None:
    state = client.get_session(session_name).get("sessionState") or {}
    return state.get(LINEAGE_STATE_KEY)


def source_session_for_memory(
    client: MemoryBankClient,
    memory_name: str,
    customer_id: str,
) -> dict[str, Any] | None:
    """Finds the session whose lineage lists this memory.

    Scans only this customer's sessions. Returns None when no lineage names
    the memory, which is the honest answer for memories written before
    lineage existed or seeded directly.
    """
    for session in client.list_sessions():
        if session.get("userId") != customer_id:
            continue
        lineage = (session.get("sessionState") or {}).get(LINEAGE_STATE_KEY)
        if not lineage:
            continue
        if any(item.get("name") == memory_name for item in lineage.get("generated", [])):
            return {"session_name": session.get("name"), "lineage": lineage}
    return None


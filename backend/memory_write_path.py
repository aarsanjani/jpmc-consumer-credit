"""Verified write path: session -> veracity check -> generate -> lineage.

Every stored session gets its memories generated into one of three scopes,
decided by the veracity verdict on what the customer said:

  * trusted         {"customer_id": X}                             agents read it as fact
  * pending_review  {"customer_id": X, "corpus": "pending_review"} agents read it, labelled unverified
  * quarantine      {"customer_id": X, "corpus": "quarantine"}     agents never read it

Scope matching in Memory Bank is exact, so reading one scope never returns
another's memories. Anything not trusted also gets an Action Board item (see
backend/action_board.py) and a verbatim claim record that names the customer,
so a reviewer can tell whose claim it is without opening the session.

Memory Bank does not record which session a memory came from, so after each
generate we append a state-only event to the session listing the memories it
produced. That is what the lineage lookups below read back.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from backend import obs
from backend.memory_bank import (
    CUSTOMER_AUTHORS,
    NOT_RECORDED,
    MemoryBankClient,
    generated_memories,
    topic_labels,
)
from backend.models import VeracityStatus

LOG = obs.get_logger(__name__)

LINEAGE_AUTHOR = "memory_bank_lineage"
LINEAGE_STATE_KEY = "memory_lineage"
BOARD_STATE_KEY = "action_board"
COMMITMENT_TOPIC = "AGENT_COMMITMENTS"
UNVERIFIED_PREFIX = "[UNVERIFIED customer statement] "


class Standing(StrEnum):
    TRUSTED = "trusted"
    PENDING_REVIEW = "pending_review"
    QUARANTINE = "quarantine"


QUARANTINE_VERDICTS = frozenset({
    VeracityStatus.CONTRADICTED_BY_TELEMETRY.value,
    VeracityStatus.SUSPICIOUS_FALSE_CLAIM.value,
})

# Board item kind and priority for each non-trusted outcome.
_BOARD_KIND = {
    VeracityStatus.UNVERIFIED_PENDING_INVESTIGATION.value: ("UNVERIFIED_CLAIM", "NORMAL"),
    VeracityStatus.PARTIALLY_VERIFIED_CONSENT_DISPUTE.value: ("CONSENT_DISPUTE", "HIGH"),
    VeracityStatus.CONTRADICTED_BY_TELEMETRY.value: ("CONTRADICTED_CLAIM", "HIGH"),
    VeracityStatus.SUSPICIOUS_FALSE_CLAIM.value: ("SUSPICIOUS_CLAIM", "HIGH"),
}

# Takes the customer's own words, returns a VeracityStatus value.
Verifier = Callable[[str], str]
# Takes a customer id, returns a display name (or n/r).
NameLookup = Callable[[str], str]


@dataclass(frozen=True)
class Claim:
    customer_id: str
    text: str
    channel: str


def scope_for(customer_id: str, standing: Standing) -> dict[str, str]:
    if standing is Standing.TRUSTED:
        return {"customer_id": customer_id}
    return {"customer_id": customer_id, "corpus": standing.value}


def standing_for(verdict: str | None, has_customer_turns: bool) -> Standing:
    """No customer words means nothing to verify, so the bank's own statements
    are trusted. A missing verdict from a failed check is not a pass."""
    if not has_customer_turns or verdict == VeracityStatus.VERIFIED_TRUE.value:
        return Standing.TRUSTED
    if verdict in QUARANTINE_VERDICTS:
        return Standing.QUARANTINE
    return Standing.PENDING_REVIEW


def customer_text(turns: Sequence[dict[str, str]]) -> str:
    return "\n".join(t["text"] for t in turns if t.get("author") in CUSTOMER_AUTHORS and t.get("text"))


def memory_id(name: str) -> str:
    """Generate returns names with the project id and create returns them with
    the project number, so compare memories by their trailing id."""
    return str(name).rsplit("/", 1)[-1]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def claim_record_fact(claim: Claim, customer_name: str, verdict: str | None, session_name: str) -> str:
    return (
        "Claim by %s (%s) via %s on %s: \"%s\". Veracity verdict: %s. Source session %s."
        % (customer_name, claim.customer_id, claim.channel, now_iso()[:10], claim.text,
           verdict or NOT_RECORDED, memory_id(session_name))
    )


def _generate(client: MemoryBankClient, session_name: str, customer_id: str,
              standing: Standing) -> dict[str, Any]:
    # Non-trusted memories skip consolidation so each session's memories stay
    # separate; approving or rejecting one claim must not touch another's.
    return client.generate_from_session(
        session_name, scope_for(customer_id, standing),
        disable_consolidation=standing is not Standing.TRUSTED, wait=True,
    )


def _write_claim_record(client: MemoryBankClient, claim: Claim, standing: Standing, verdict: str | None,
                        session_name: str, name_lookup: NameLookup | None) -> dict[str, str]:
    name = NOT_RECORDED
    if name_lookup is not None:
        try:
            name = name_lookup(claim.customer_id) or NOT_RECORDED
        except Exception as exc:  # noqa: BLE001 - a missing name must not block the audit record
            LOG.warning("Name lookup failed for %s: %s", claim.customer_id, exc)
    fact = claim_record_fact(claim, name, verdict, session_name)
    operation = client.wait_for_operation(client.create_memory(fact, scope_for(claim.customer_id, standing)))
    return {"name": (operation.get("response") or {}).get("name", NOT_RECORDED),
            "customer_name": name, "fact": fact}


def _commitments(client: MemoryBankClient, generated: list[dict[str, str]]) -> list[str]:
    facts = []
    for item in generated:
        if item["action"] == "DELETED" or item["name"] == NOT_RECORDED:
            continue
        memory = client.get_memory(item["name"])
        if COMMITMENT_TOPIC in topic_labels(memory):
            facts.append(memory.get("fact", NOT_RECORDED))
    return facts


def board_item(kind: str, priority: str, claim: Claim, standing: Standing, verdict: str | None,
               customer_name: str = NOT_RECORDED, facts: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "kind": kind,
        "priority": priority,
        "status": "OPEN",
        "customer_id": claim.customer_id,
        "customer_name": customer_name,
        "channel": claim.channel,
        "claim_text": claim.text or NOT_RECORDED,
        "verdict": verdict or NOT_RECORDED,
        "standing": standing.value,
        "facts": list(facts),
        "created_at": now_iso(),
        "history": [],
    }


def record(
    client: MemoryBankClient,
    session_name: str,
    claim: Claim,
    verdict: str | None,
    *,
    check_error: str | None = None,
    name_lookup: NameLookup | None = None,
) -> dict[str, Any]:
    """Generates memories from a stored session into the scope the verdict
    picks, writes the claim record and board item, and stamps lineage."""
    standing = standing_for(verdict, bool(claim.text))
    with obs.turn("memory.record", LOG, standing=standing.value,
                  verdict=verdict or NOT_RECORDED) as span:
        operation = _generate(client, session_name, claim.customer_id, standing)
        lineage: dict[str, Any] = {
            "scope": scope_for(claim.customer_id, standing),
            "standing": standing.value,
            "verdict": verdict or NOT_RECORDED,
            "operation": operation.get("name", NOT_RECORDED),
            "generated": generated_memories(operation),
            "verified_text": claim.text or NOT_RECORDED,
        }
        if check_error:
            lineage["check_error"] = check_error

        item = None
        if standing is not Standing.TRUSTED:
            claim_record = _write_claim_record(client, claim, standing, verdict, session_name, name_lookup)
            lineage["claim_record"] = claim_record
            kind, priority = _BOARD_KIND.get(verdict or "", ("CHECK_FAILED", "NORMAL"))
            item = board_item(kind, priority, claim, standing, verdict, claim_record["customer_name"])
        # Any trusted session can hold a bank promise. Memory Bank rarely
        # extracts anything from a bank-only session, so don't limit this to those.
        elif facts := _commitments(client, lineage["generated"]):
            item = board_item("FOLLOW_UP", "NORMAL", claim, standing, verdict, facts=facts)

        delta: dict[str, Any] = {LINEAGE_STATE_KEY: lineage}
        if item:
            delta[BOARD_STATE_KEY] = item
        client.append_state_delta(session_name, LINEAGE_AUTHOR, delta)
        span.detail(generated=len(lineage["generated"]), board=item["kind"] if item else "none")
    return dict(lineage, action_board=item)


def verify_and_record(
    client: MemoryBankClient,
    session_name: str,
    customer_id: str,
    turns: Sequence[dict[str, str]],
    channel: str,
    verify: Verifier,
    name_lookup: NameLookup | None = None,
) -> dict[str, Any]:
    """Runs the veracity check on the customer's own turns, then records.

    A failed check leaves the verdict n/r, which lands in pending review with
    the error attached; it is never treated as a pass.
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
    return record(client, session_name, Claim(customer_id, said, channel), verdict,
                  check_error=check_error, name_lookup=name_lookup)


def restate(client: MemoryBankClient, session_name: str, customer_id: str,
            to: Standing) -> dict[str, Any]:
    """Moves a session's memories to another scope by deleting what it
    generated before and generating again from the same session.

    Regenerating, rather than copying facts, keeps topic labels and lets the
    trusted scope consolidate the facts with what it already holds.
    """
    session = client.get_session(session_name)
    lineage = (session.get("sessionState") or {}).get(LINEAGE_STATE_KEY) or {}
    for item in lineage.get("generated", []):
        if item.get("action") != "DELETED" and item.get("name") not in (None, NOT_RECORDED):
            client.delete_memory(item["name"])
    operation = _generate(client, session_name, customer_id, to)
    moved = dict(lineage, scope=scope_for(customer_id, to), standing=to.value,
                 operation=operation.get("name", NOT_RECORDED),
                 generated=generated_memories(operation),
                 previous=[{"standing": lineage.get("standing", NOT_RECORDED),
                            "generated": lineage.get("generated", [])}] + lineage.get("previous", []))
    return moved


def retrieve_for_agents(
    client: MemoryBankClient,
    customer_id: str,
    query: str | None = None,
    top_k: int = 5,
    memory_filter: str | None = None,
) -> list[dict[str, Any]]:
    """What an agent is allowed to read: trusted plus pending review.

    Each item carries `standing`, and pending facts are prefixed so no prompt
    can mistake an unverified statement for a fact. Quarantine is never read.
    With a query, both lists are merged by distance; without one, trusted
    comes first.
    """
    def fetch(standing: Standing) -> list[dict[str, Any]]:
        rows = client.retrieve(scope_for(customer_id, standing), query=query,
                               top_k=top_k, memory_filter=memory_filter)
        return [dict(row, standing=standing.value) for row in rows]

    standings = (Standing.TRUSTED, Standing.PENDING_REVIEW)
    with ThreadPoolExecutor(max_workers=len(standings)) as pool:
        futures = [pool.submit(contextvars.copy_context().run, fetch, s) for s in standings]
        trusted, pending = (f.result() for f in futures)

    for row in pending:
        memory = dict(row.get("memory") or {})
        memory["fact"] = UNVERIFIED_PREFIX + str(memory.get("fact", NOT_RECORDED))
        row["memory"] = memory
    merged = trusted + pending
    if query:
        merged.sort(key=lambda row: row.get("distance", float("inf")))
    return merged[:top_k]


def lineage_for_session(client: MemoryBankClient, session_name: str) -> dict[str, Any] | None:
    state = client.get_session(session_name).get("sessionState") or {}
    return state.get(LINEAGE_STATE_KEY)


def source_session_for_memory(
    client: MemoryBankClient,
    memory_name: str,
    customer_id: str,
) -> dict[str, Any] | None:
    """Finds the session whose lineage lists this memory, including its claim
    record. Scans only this customer's sessions. None means no lineage names
    it: seeded directly or written before lineage existed."""
    wanted = memory_id(memory_name)
    for session in client.list_sessions():
        if session.get("userId") != customer_id:
            continue
        lineage = (session.get("sessionState") or {}).get(LINEAGE_STATE_KEY)
        if not lineage:
            continue
        names = [item.get("name") for item in lineage.get("generated", [])]
        names.append((lineage.get("claim_record") or {}).get("name"))
        if any(n and memory_id(n) == wanted for n in names):
            return {"session_name": session.get("name"), "lineage": lineage}
    return None

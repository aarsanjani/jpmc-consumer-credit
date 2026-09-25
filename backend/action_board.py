"""Action Board: claims and follow-ups a human needs to look at.

Items live in the Memory Bank session they came from (session state key
`action_board`), written by backend/memory_write_path.py:record. Every admin
decision is appended as another state update, so the session's event list is
the audit trail and there is no second store to keep in sync.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from backend import obs
from backend.memory_bank import NOT_RECORDED, MemoryBankClient
from backend.memory_write_path import (
    BOARD_STATE_KEY,
    LINEAGE_AUTHOR,
    LINEAGE_STATE_KEY,
    Standing,
    now_iso,
    restate,
    scope_for,
)

LOG = obs.get_logger(__name__)

ADMIN_AUTHOR = "dashboard_admin"
_PRIORITY_ORDER = {"HIGH": 0, "NORMAL": 1}


class Action(StrEnum):
    APPROVE = "APPROVE"      # the claim holds: move its memories to trusted
    REJECT = "REJECT"        # the claim is false: move its memories to quarantine
    ESCALATE = "ESCALATE"
    TAG = "TAG"
    CLOSE = "CLOSE"          # answered; the reply is added to the session transcript
    FULFIL = "FULFIL"        # a bank follow-up was done


class BoardError(ValueError):
    """The action does not apply to this item."""


def list_items(client: MemoryBankClient, customer_id: str | None = None,
               include_closed: bool = False) -> list[dict[str, Any]]:
    items = []
    for session in client.list_sessions():
        item = (session.get("sessionState") or {}).get(BOARD_STATE_KEY)
        if not item or (customer_id and session.get("userId") != customer_id):
            continue
        if item.get("status") == "CLOSED" and not include_closed:
            continue
        items.append(dict(item, session_name=session.get("name"),
                          session_id=str(session.get("name", "")).rsplit("/", 1)[-1]))
    items.sort(key=lambda i: i.get("created_at", ""), reverse=True)
    items.sort(key=lambda i: _PRIORITY_ORDER.get(i.get("priority", ""), 9))
    return items


def _move_claim_record(client: MemoryBankClient, lineage: dict[str, Any], customer_id: str,
                       to: Standing) -> dict[str, Any] | None:
    """Approved claims become facts, so the 'claim by' record goes away;
    rejected ones keep it, in quarantine."""
    record = lineage.get("claim_record")
    if not record or record.get("name") in (None, NOT_RECORDED):
        return record
    client.delete_memory(record["name"])
    if to is Standing.TRUSTED:
        return None
    operation = client.wait_for_operation(client.create_memory(record["fact"], scope_for(customer_id, to)))
    return dict(record, name=(operation.get("response") or {}).get("name", NOT_RECORDED))


def act(
    client: MemoryBankClient,
    session_name: str,
    action: Action,
    actor: str = ADMIN_AUTHOR,
    note: str = "",
    reply: str = "",
    tags: Sequence[str] = (),
) -> dict[str, Any]:
    state = client.get_session(session_name).get("sessionState") or {}
    item = state.get(BOARD_STATE_KEY)
    if not item:
        raise BoardError("This session has no Action Board item.")
    if item.get("status") == "CLOSED":
        raise BoardError("This item is already closed.")

    delta: dict[str, Any] = {}
    item = dict(item)
    with obs.turn("action_board.act", LOG, action=action.value, kind=item.get("kind")):
        if action in (Action.APPROVE, Action.REJECT):
            if item.get("standing") == Standing.TRUSTED.value:
                raise BoardError("Only unverified or quarantined claims can be approved or rejected.")
            to = Standing.TRUSTED if action is Action.APPROVE else Standing.QUARANTINE
            if item.get("standing") != to.value:
                lineage = restate(client, session_name, item["customer_id"], to)
                lineage["claim_record"] = _move_claim_record(
                    client, state.get(LINEAGE_STATE_KEY) or {}, item["customer_id"], to)
                delta[LINEAGE_STATE_KEY] = lineage
                item["standing"] = to.value
            item["status"] = "CLOSED"
            item["resolution"] = "APPROVED" if action is Action.APPROVE else "REJECTED"
        elif action is Action.ESCALATE:
            item["status"] = "ESCALATED"
        elif action is Action.TAG:
            item["tags"] = sorted(set(item.get("tags", [])) | {t.strip() for t in tags if t.strip()})
        elif action is Action.CLOSE:
            if reply:
                client.append_event(session_name, ADMIN_AUTHOR, reply)
            item["status"] = "CLOSED"
            item["resolution"] = "ANSWERED" if reply else "CLOSED"
        elif action is Action.FULFIL:
            if item.get("kind") != "FOLLOW_UP":
                raise BoardError("Only follow-ups can be marked fulfilled.")
            item["status"] = "CLOSED"
            item["resolution"] = "FULFILLED"

        entry = {"action": action.value, "actor": actor, "note": note, "at": now_iso()}
        if reply:
            entry["reply"] = reply
        item["history"] = list(item.get("history", [])) + [entry]
        delta[BOARD_STATE_KEY] = item
        client.append_state_delta(session_name, LINEAGE_AUTHOR, delta)
    return item

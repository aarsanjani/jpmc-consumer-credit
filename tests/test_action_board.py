import pytest

from backend import action_board as ab
from backend import memory_write_path as wp
from backend.memory_write_path import Claim, Standing
from backend.models import VeracityStatus
from fakes import FakeMemoryBank, session_name

SESSION = session_name(1)
TRUSTED = {"customer_id": "CUST1"}
PENDING = {"customer_id": "CUST1", "corpus": "pending_review"}
QUARANTINE = {"customer_id": "CUST1", "corpus": "quarantine"}


def _bank_with_claim(verdict, text="Your agent promised me a $50k limit."):
    bank = FakeMemoryBank()
    bank.add_session(SESSION, "CUST1")
    wp.record(bank, SESSION, Claim("CUST1", text, "BRANCH"), verdict, name_lookup=lambda cid: "Priya Vantalis")
    return bank


@pytest.fixture
def pending_bank():
    return _bank_with_claim(VeracityStatus.UNVERIFIED_PENDING_INVESTIGATION.value)


def test_approve_moves_memories_to_trusted_and_drops_the_claim_record(pending_bank):
    item = ab.act(pending_bank, SESSION, ab.Action.APPROVE, note="checked the call recording")

    assert item["status"] == "CLOSED"
    assert item["resolution"] == "APPROVED"
    assert item["standing"] == Standing.TRUSTED
    assert pending_bank.in_scope(PENDING) == []
    assert [m["fact"] for m in pending_bank.in_scope(TRUSTED)] == ["fact from %s" % SESSION]
    assert item["history"][-1]["note"] == "checked the call recording"


def test_reject_moves_memories_and_claim_record_to_quarantine(pending_bank):
    ab.act(pending_bank, SESSION, ab.Action.REJECT)

    facts = [m["fact"] for m in pending_bank.in_scope(QUARANTINE)]
    assert pending_bank.in_scope(PENDING) == []
    assert "fact from %s" % SESSION in facts
    assert any("Claim by Priya Vantalis (CUST1)" in f for f in facts)


def test_close_with_reply_adds_it_to_the_transcript(pending_bank):
    item = ab.act(pending_bank, SESSION, ab.Action.CLOSE, reply="We have no record of that promise.")

    assert pending_bank.events == [(SESSION, ab.ADMIN_AUTHOR, "We have no record of that promise.")]
    assert item["resolution"] == "ANSWERED"


def test_tag_merges_and_trims(pending_bank):
    ab.act(pending_bank, SESSION, ab.Action.TAG, tags=[" limits ", "branch"])
    item = ab.act(pending_bank, SESSION, ab.Action.TAG, tags=["limits", ""])

    assert item["tags"] == ["branch", "limits"]
    assert item["status"] == "OPEN"


@pytest.mark.parametrize(
    ("action", "message"),
    [(ab.Action.FULFIL, "Only follow-ups"), (ab.Action.APPROVE, "already closed")],
)
def test_actions_that_do_not_apply_are_refused(pending_bank, action, message):
    if action is ab.Action.APPROVE:
        ab.act(pending_bank, SESSION, ab.Action.CLOSE)

    with pytest.raises(ab.BoardError, match=message):
        ab.act(pending_bank, SESSION, action)


def test_follow_up_can_be_fulfilled_but_not_approved():
    bank = FakeMemoryBank(generated_topics=[wp.COMMITMENT_TOPIC])
    bank.add_session(SESSION, "CUST1")
    wp.record(bank, SESSION, Claim("CUST1", "", "IVR"), None)

    with pytest.raises(ab.BoardError, match="Only unverified or quarantined"):
        ab.act(bank, SESSION, ab.Action.APPROVE)
    assert ab.act(bank, SESSION, ab.Action.FULFIL)["resolution"] == "FULFILLED"


def test_session_without_item_is_refused():
    bank = FakeMemoryBank()
    bank.add_session(SESSION, "CUST1")

    with pytest.raises(ab.BoardError, match="no Action Board item"):
        ab.act(bank, SESSION, ab.Action.ESCALATE)


def test_list_puts_high_priority_first_and_hides_closed():
    bank = FakeMemoryBank()
    for n, verdict in enumerate([VeracityStatus.UNVERIFIED_PENDING_INVESTIGATION.value,
                                 VeracityStatus.PARTIALLY_VERIFIED_CONSENT_DISPUTE.value,
                                 VeracityStatus.SUSPICIOUS_FALSE_CLAIM.value], start=1):
        bank.add_session(session_name(n), "CUST1")
        wp.record(bank, session_name(n), Claim("CUST1", "claim %d" % n, "WEB"), verdict)
    ab.act(bank, session_name(3), ab.Action.CLOSE)

    open_items = ab.list_items(bank, "CUST1")

    assert [i["kind"] for i in open_items] == ["CONSENT_DISPUTE", "UNVERIFIED_CLAIM"]
    assert len(ab.list_items(bank, "CUST1", include_closed=True)) == 3
    assert ab.list_items(bank, "CUST2") == []

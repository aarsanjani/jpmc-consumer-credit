import json
import urllib.parse
import urllib.request

import pytest

from backend import memory_write_path as wp
from backend.memory_bank import MemoryBankClient, generated_memories
from backend.models import VeracityStatus
from backend.memory_write_path import Claim, Standing
from fakes import FakeMemoryBank, session_name

SESSION = session_name(1)
MEMORY = "projects/p/locations/l/reasoningEngines/e/memories/m1"
TRUSTED = {"customer_id": "CUST1"}
PENDING = {"customer_id": "CUST1", "corpus": "pending_review"}
QUARANTINE = {"customer_id": "CUST1", "corpus": "quarantine"}


@pytest.fixture
def bank():
    fake = FakeMemoryBank()
    fake.add_session(SESSION, "CUST1")
    return fake


def _record(bank, verdict, text="I am in Lisbon.", name_lookup=lambda cid: "Priya Vantalis"):
    return wp.record(bank, SESSION, Claim("CUST1", text, "MOBILE_APP"), verdict, name_lookup=name_lookup)


@pytest.mark.parametrize(
    ("verdict", "has_turns", "expected"),
    [
        (VeracityStatus.VERIFIED_TRUE.value, True, Standing.TRUSTED),
        (None, False, Standing.TRUSTED),
        (VeracityStatus.UNVERIFIED_PENDING_INVESTIGATION.value, True, Standing.PENDING_REVIEW),
        (VeracityStatus.PARTIALLY_VERIFIED_CONSENT_DISPUTE.value, True, Standing.PENDING_REVIEW),
        (None, True, Standing.PENDING_REVIEW),
        (VeracityStatus.CONTRADICTED_BY_TELEMETRY.value, True, Standing.QUARANTINE),
        (VeracityStatus.SUSPICIOUS_FALSE_CLAIM.value, True, Standing.QUARANTINE),
    ],
)
def test_standing_for(verdict, has_turns, expected):
    assert wp.standing_for(verdict, has_turns) is expected


@pytest.mark.parametrize(
    ("standing", "expected"),
    [(Standing.TRUSTED, TRUSTED), (Standing.PENDING_REVIEW, PENDING), (Standing.QUARANTINE, QUARANTINE)],
)
def test_scope_for(standing, expected):
    assert wp.scope_for("CUST1", standing) == expected


def test_verified_claim_is_trusted_with_no_board_item(bank):
    lineage = _record(bank, VeracityStatus.VERIFIED_TRUE.value)

    assert bank.generate_calls == [{"session": SESSION, "scope": TRUSTED, "disable_consolidation": False}]
    assert lineage["action_board"] is None
    assert "claim_record" not in lineage
    assert wp.BOARD_STATE_KEY not in bank.sessions[SESSION]["sessionState"]


@pytest.mark.parametrize(
    ("verdict", "scope", "kind", "priority"),
    [
        (VeracityStatus.UNVERIFIED_PENDING_INVESTIGATION.value, PENDING, "UNVERIFIED_CLAIM", "NORMAL"),
        (VeracityStatus.PARTIALLY_VERIFIED_CONSENT_DISPUTE.value, PENDING, "CONSENT_DISPUTE", "HIGH"),
        (VeracityStatus.CONTRADICTED_BY_TELEMETRY.value, QUARANTINE, "CONTRADICTED_CLAIM", "HIGH"),
        (VeracityStatus.SUSPICIOUS_FALSE_CLAIM.value, QUARANTINE, "SUSPICIOUS_CLAIM", "HIGH"),
    ],
)
def test_non_trusted_claim_lands_in_its_scope_with_board_item(bank, verdict, scope, kind, priority):
    lineage = _record(bank, verdict)

    assert bank.generate_calls[0]["scope"] == scope
    assert bank.generate_calls[0]["disable_consolidation"] is True
    assert lineage["action_board"]["kind"] == kind
    assert lineage["action_board"]["priority"] == priority
    assert bank.sessions[SESSION]["sessionState"][wp.BOARD_STATE_KEY] == lineage["action_board"]


def test_claim_record_names_the_customer_and_sits_in_the_same_scope(bank):
    lineage = _record(bank, VeracityStatus.CONTRADICTED_BY_TELEMETRY.value)

    record = bank.get_memory(lineage["claim_record"]["name"])
    assert record["scope"] == QUARANTINE
    assert "Priya Vantalis (CUST1)" in record["fact"]
    assert "MOBILE_APP" in record["fact"]
    assert '"I am in Lisbon."' in record["fact"]
    assert "s1" in record["fact"]


def test_failed_name_lookup_still_writes_the_claim_record(bank):
    def broken(customer_id):
        raise RuntimeError("bq down")

    lineage = _record(bank, VeracityStatus.UNVERIFIED_PENDING_INVESTIGATION.value, name_lookup=broken)

    assert lineage["claim_record"]["customer_name"] == "n/r"
    assert "(CUST1)" in lineage["claim_record"]["fact"]


def test_bank_only_session_with_a_promise_becomes_a_follow_up():
    bank = FakeMemoryBank(generated_topics=[wp.COMMITMENT_TOPIC])
    bank.add_session(SESSION, "CUST1")

    lineage = _record(bank, None, text="")

    assert bank.generate_calls[0]["scope"] == TRUSTED
    assert lineage["action_board"]["kind"] == "FOLLOW_UP"
    assert lineage["action_board"]["facts"] == ["fact from %s" % SESSION]


def test_bank_only_session_without_a_promise_has_no_board_item(bank):
    assert _record(bank, None, text="")["action_board"] is None


def test_verified_session_with_a_promise_becomes_a_follow_up():
    bank = FakeMemoryBank(generated_topics=[wp.COMMITMENT_TOPIC])
    bank.add_session(SESSION, "CUST1")

    lineage = _record(bank, VeracityStatus.VERIFIED_TRUE.value)

    assert lineage["action_board"]["kind"] == "FOLLOW_UP"
    assert lineage["action_board"]["verdict"] == VeracityStatus.VERIFIED_TRUE.value


def test_verify_runs_only_on_customer_turns(bank):
    seen = []
    turns = [{"author": "fraud_monitoring_agent", "text": "We blocked the card."},
             {"author": "customer", "text": "I am in Lisbon."}]

    wp.verify_and_record(bank, SESSION, "CUST1", turns, "MOBILE_APP",
                         lambda said: seen.append(said) or VeracityStatus.VERIFIED_TRUE.value)

    assert seen == ["I am in Lisbon."]


def test_session_without_customer_turns_skips_check(bank):
    lineage = wp.verify_and_record(bank, SESSION, "CUST1", [{"author": "bank", "text": "x"}], "IVR",
                                   lambda said: pytest.fail("verifier must not run"))

    assert lineage["verdict"] == "n/r"
    assert lineage["standing"] == Standing.TRUSTED


def test_failed_check_goes_to_pending_review_with_the_error(bank):
    def broken(said):
        raise RuntimeError("gemini down")

    lineage = wp.verify_and_record(bank, SESSION, "CUST1", [{"author": "customer", "text": "hi"}], "IVR", broken)

    assert lineage["standing"] == Standing.PENDING_REVIEW
    assert lineage["action_board"]["kind"] == "CHECK_FAILED"
    assert "gemini down" in lineage["check_error"]


def test_restate_moves_generated_memories_and_keeps_history(bank):
    first = _record(bank, VeracityStatus.UNVERIFIED_PENDING_INVESTIGATION.value)
    old_name = first["generated"][0]["name"]

    moved = wp.restate(bank, SESSION, "CUST1", Standing.TRUSTED)

    assert old_name not in bank.memories
    assert moved["standing"] == Standing.TRUSTED
    assert bank.get_memory(moved["generated"][0]["name"])["scope"] == TRUSTED
    assert moved["previous"][0]["standing"] == Standing.PENDING_REVIEW


def test_agents_read_trusted_and_labelled_pending_but_never_quarantine():
    bank = FakeMemoryBank()
    bank.create_memory("trusted fact", TRUSTED)
    bank.create_memory("pending fact", PENDING)
    bank.create_memory("quarantined fact", QUARANTINE)

    rows = wp.retrieve_for_agents(bank, "CUST1", top_k=10)

    facts = {row["memory"]["fact"]: row["standing"] for row in rows}
    assert facts == {"trusted fact": "trusted", wp.UNVERIFIED_PREFIX + "pending fact": "pending_review"}


def test_source_session_includes_claim_record_and_stays_within_customer(bank):
    lineage = _record(bank, VeracityStatus.CONTRADICTED_BY_TELEMETRY.value)
    record_name = lineage["claim_record"]["name"]

    assert wp.source_session_for_memory(bank, record_name, "CUST1")["session_name"] == SESSION
    assert wp.source_session_for_memory(bank, record_name, "CUST2") is None


@pytest.mark.parametrize(
    ("a", "b"),
    [("projects/my-proj/locations/l/reasoningEngines/e/memories/42",
      "projects/123456/locations/l/reasoningEngines/e/memories/42")],
)
def test_memory_id_ignores_project_id_vs_number(a, b):
    assert wp.memory_id(a) == wp.memory_id(b)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"text": "I never got a travel notice."}, ["I never got a travel notice."]),
        ({"preset_id": "", "turns": [{"author": "customer", "text": "hi"}, {"author": "agent", "text": "ok"}]}, ["hi"]),
        ({}, []),
    ],
)
def test_channel_event_sends_customer_words_through_the_check(kwargs, expected):
    from backend import adk_agents

    bank = FakeMemoryBank()
    seen = []

    adk_agents.ingest_live_channel_event("NO_SUCH_CHANNEL", customer_id="CUST1", client=bank,
                                         verify=lambda said: seen.append(said) or VeracityStatus.VERIFIED_TRUE.value,
                                         **kwargs)

    assert seen == expected


# --- MemoryBankClient HTTP shape (no network: urlopen is replaced) ---------


class _FakeHttpResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_urlopen(monkeypatch, pages):
    sent = []
    replies = iter(pages)

    def fake(request, timeout=None):
        sent.append(request)
        return _FakeHttpResponse(json.dumps(next(replies)).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return sent


def test_generated_memories_empty_until_operation_finishes():
    assert generated_memories({"name": "ops/1"}) == []


def test_append_state_delta_sends_state_only_event(monkeypatch):
    sent = _capture_urlopen(monkeypatch, [{}])
    client = MemoryBankClient(project="p", engine_id="e", access_token="fake")

    client.append_state_delta(SESSION, "memory_bank_lineage", {"k": 1})

    body = json.loads(sent[0].data)
    assert sent[0].full_url.endswith("/sessions/s1:appendEvent")
    assert body["actions"] == {"stateDelta": {"k": 1}}
    assert "content" not in body


def test_update_memory_patches_fact_only(monkeypatch):
    sent = _capture_urlopen(monkeypatch, [{}])
    client = MemoryBankClient(project="p", engine_id="e", access_token="fake")

    client.update_memory(MEMORY, "corrected fact")

    assert sent[0].get_method() == "PATCH"
    assert urllib.parse.urlparse(sent[0].full_url).query == "updateMask=fact"
    assert json.loads(sent[0].data) == {"fact": "corrected fact"}


def test_generate_from_session_sends_disable_consolidation(monkeypatch):
    sent = _capture_urlopen(monkeypatch, [{"name": "ops/1", "done": True}])
    client = MemoryBankClient(project="p", engine_id="e", access_token="fake")

    client.generate_from_session(SESSION, PENDING, disable_consolidation=True, wait=False)

    assert json.loads(sent[0].data)["disableConsolidation"] is True


def test_list_sessions_follows_page_tokens(monkeypatch):
    _capture_urlopen(monkeypatch, [{"sessions": [{"name": "a"}], "nextPageToken": "t"},
                                   {"sessions": [{"name": "b"}]}])
    client = MemoryBankClient(project="p", engine_id="e", access_token="fake")

    assert [s["name"] for s in client.list_sessions()] == ["a", "b"]


@pytest.mark.parametrize("bad", ["", "s1", "projects/p/memories/m1"])
def test_delete_session_rejects_bare_ids(bad):
    client = MemoryBankClient(project="p", engine_id="e", access_token="fake")

    with pytest.raises(ValueError, match="full resource name"):
        client.delete_session(bad)


@pytest.mark.parametrize("bad", ["", "m1"])
def test_get_memory_rejects_bare_ids(bad):
    client = MemoryBankClient(project="p", engine_id="e", access_token="fake")

    with pytest.raises(ValueError, match="full resource name"):
        client.get_memory(bad)

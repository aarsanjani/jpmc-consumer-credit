import json
import urllib.parse
import urllib.request

import pytest

from backend import memory_write_path as wp
from backend.memory_bank import MemoryBankClient, generated_memories
from backend.models import VeracityStatus

SESSION = "projects/p/locations/l/reasoningEngines/e/sessions/s1"
MEMORY = "projects/p/locations/l/reasoningEngines/e/memories/m1"


class FakeMemoryBank:
    """Keeps sessions and generate calls in dicts, the way the service would."""

    def __init__(self, sessions=None):
        self.sessions = sessions or {}
        self.generate_calls = []

    def generate_from_session(self, session_name, scope, wait=True):
        self.generate_calls.append((session_name, scope))
        return {"name": "ops/1", "done": True,
                "response": {"generatedMemories": [{"memory": {"name": MEMORY}, "action": "CREATED"}]}}

    def append_state_delta(self, session_name, author, delta):
        self.sessions.setdefault(session_name, {"name": session_name, "userId": "CUST1"})
        self.sessions[session_name].setdefault("sessionState", {}).update(delta)
        return {}

    def get_session(self, session_name):
        return self.sessions[session_name]

    def list_sessions(self):
        return list(self.sessions.values())


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


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (VeracityStatus.VERIFIED_TRUE.value, {"customer_id": "C"}),
        (VeracityStatus.UNVERIFIED_PENDING_INVESTIGATION.value, {"customer_id": "C"}),
        (VeracityStatus.PARTIALLY_VERIFIED_CONSENT_DISPUTE.value, {"customer_id": "C"}),
        (None, {"customer_id": "C"}),
        (VeracityStatus.CONTRADICTED_BY_TELEMETRY.value, {"customer_id": "C", "corpus": "quarantine"}),
        (VeracityStatus.SUSPICIOUS_FALSE_CLAIM.value, {"customer_id": "C", "corpus": "quarantine"}),
    ],
)
def test_scope_for_verdict(verdict, expected):
    assert wp.scope_for_verdict("C", verdict) == expected


def test_contradicted_claim_generates_into_quarantine_and_stamps_lineage():
    bank = FakeMemoryBank()

    lineage = wp.generate_with_lineage(bank, SESSION, "CUST1", VeracityStatus.CONTRADICTED_BY_TELEMETRY.value)

    assert bank.generate_calls == [(SESSION, {"customer_id": "CUST1", "corpus": "quarantine"})]
    assert lineage["generated"] == [{"name": MEMORY, "action": "CREATED"}]
    assert bank.sessions[SESSION]["sessionState"][wp.LINEAGE_STATE_KEY] == lineage


def test_verify_runs_only_on_customer_turns():
    bank = FakeMemoryBank()
    seen = []
    turns = [{"author": "fraud_monitoring_agent", "text": "We blocked the card."},
             {"author": "customer", "text": "I am in Lisbon."}]

    wp.verify_and_generate(bank, SESSION, "CUST1", turns,
                           lambda said: seen.append(said) or VeracityStatus.VERIFIED_TRUE.value)

    assert seen == ["I am in Lisbon."]


def test_session_without_customer_turns_skips_check_and_stays_trusted():
    bank = FakeMemoryBank()

    lineage = wp.verify_and_generate(bank, SESSION, "CUST1", [{"author": "bank", "text": "x"}],
                                     lambda said: pytest.fail("verifier must not run"))

    assert lineage["verdict"] == "n/r"
    assert bank.generate_calls[0][1] == {"customer_id": "CUST1"}


def test_failed_check_is_reported_and_not_guessed():
    bank = FakeMemoryBank()

    def broken(said):
        raise RuntimeError("gemini down")

    lineage = wp.verify_and_generate(bank, SESSION, "CUST1", [{"author": "customer", "text": "hi"}], broken)

    assert lineage["verdict"] == "n/r"
    assert "gemini down" in lineage["check_error"]


def test_source_session_found_only_within_customer():
    bank = FakeMemoryBank()
    wp.generate_with_lineage(bank, SESSION, "CUST1", None)

    assert wp.source_session_for_memory(bank, MEMORY, "CUST1")["session_name"] == SESSION
    assert wp.source_session_for_memory(bank, MEMORY, "CUST2") is None


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

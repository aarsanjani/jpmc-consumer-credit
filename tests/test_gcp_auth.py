"""Auth and call-recording behaviour of the urllib-based GCP clients."""

import io
import threading
import urllib.error
from unittest import mock

import pytest

from backend.gcp_auth import BearerTokenSource, CredentialsUnavailableError
from backend.knowledge_catalog import (
    CatalogCredentialsError,
    CatalogError,
    CatalogGovernor,
    DataplexCatalogClient,
)
from backend.memory_bank import MemoryBankClient, MemoryBankCredentialsError, MemoryBankError


class FakeCreds:
    def __init__(self):
        self.valid = False
        self.token = None
        self.refreshes = 0

    def refresh(self, _request):
        self.refreshes += 1
        self.token = "adc-%d" % self.refreshes
        self.valid = True


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def no_env_token(monkeypatch):
    for name in ("X_TOKEN", "MEMORY_BANK_TOKEN", "CATALOG_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_explicit_token_is_static_and_never_refreshed(monkeypatch):
    monkeypatch.setenv("X_TOKEN", "env")
    source = BearerTokenSource("given", ("X_TOKEN",))

    token = source.get(force_refresh=True)

    assert token == "given"
    assert source.refreshable is False


def test_env_token_is_static_and_never_swapped_for_adc(monkeypatch):
    monkeypatch.setenv("X_TOKEN", "env")
    source = BearerTokenSource(None, ("X_TOKEN",))

    token = source.get(force_refresh=True)

    assert token == "env"
    assert source.refreshable is False


def test_adc_token_is_reused_while_valid(no_env_token):
    creds = FakeCreds()
    with mock.patch("google.auth.default", return_value=(creds, "p")):
        source = BearerTokenSource(None, ("X_TOKEN",))

        tokens = [source.get(), source.get()]

    assert tokens == ["adc-1", "adc-1"]


def test_adc_token_refreshes_once_google_auth_marks_it_invalid(no_env_token):
    creds = FakeCreds()
    with mock.patch("google.auth.default", return_value=(creds, "p")):
        source = BearerTokenSource(None, ("X_TOKEN",))
        source.get()
        creds.valid = False

        token = source.get()

    assert token == "adc-2"


def test_concurrent_callers_trigger_a_single_refresh(no_env_token):
    creds = FakeCreds()
    with mock.patch("google.auth.default", return_value=(creds, "p")):
        source = BearerTokenSource(None, ("X_TOKEN",))
        threads = [threading.Thread(target=source.get) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert creds.refreshes == 1


def test_missing_adc_raises_credentials_error_chained_to_cause(no_env_token):
    source = BearerTokenSource(None, ("X_TOKEN",))

    with mock.patch("google.auth.default", side_effect=OSError("no ADC file")):
        with pytest.raises(CredentialsUnavailableError) as caught:
            source.get()

    assert isinstance(caught.value.__cause__, OSError)


@pytest.mark.parametrize(
    ("make_client", "error_type", "base_type"),
    [
        (lambda: MemoryBankClient(project="p", engine_id="e"), MemoryBankCredentialsError, MemoryBankError),
        (lambda: DataplexCatalogClient(project="p"), CatalogCredentialsError, CatalogError),
    ],
)
def test_client_credential_failure_is_its_own_error_but_still_caught_by_base(
    no_env_token, make_client, error_type, base_type
):
    client = make_client()

    with mock.patch("google.auth.default", side_effect=OSError("no ADC file")):
        with pytest.raises(error_type) as caught:
            client.token()

    assert isinstance(caught.value, base_type)
    assert isinstance(caught.value, CredentialsUnavailableError)
    assert caught.value.status is None


def test_recording_captures_only_the_calls_made_in_its_own_context():
    client = MemoryBankClient(project="p", engine_id="e", access_token="t", verbose=False)
    other_thread_started = threading.Event()
    release_other_thread = threading.Event()

    def other_request():
        other_thread_started.set()
        release_other_thread.wait()
        client._request("GET", client.base_url + "/sessions")

    with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: FakeResponse(b"{}")):
        other = threading.Thread(target=other_request)
        other.start()
        other_thread_started.wait()
        with client.recording() as recorded:
            client.retrieve({"customer_id": "C1"}, query="q")
            release_other_thread.set()
            other.join()

    assert len(recorded) == 1
    assert recorded[0].endswith("memories:retrieve")
    assert len(client.calls) == 2


def test_call_history_is_bounded():
    client = MemoryBankClient(project="p", engine_id="e", access_token="t", verbose=False)

    with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: FakeResponse(b"{}")):
        for _ in range(500):
            client._request("GET", client.base_url)

    assert len(client.calls) == client.calls.maxlen


def _http_error(status):
    return urllib.error.HTTPError("u", status, "err", {}, io.BytesIO(b"body"))


@pytest.mark.parametrize(
    ("failure", "reason_fragment"),
    [
        (_http_error(404), "Not in the catalog"),
        (_http_error(403), "lookup failed (HTTP 403)"),
    ],
)
def test_table_check_refuses_with_the_real_cause(failure, reason_fragment):
    client = DataplexCatalogClient(project="p", dataset="d", access_token="t", verbose=False)
    governor = CatalogGovernor(client)

    with mock.patch("urllib.request.urlopen", side_effect=failure):
        decision = governor.check_table("authorizations")

    assert decision.allowed is False
    assert reason_fragment in decision.reason


def test_table_check_without_credentials_refuses_and_says_so(no_env_token):
    governor = CatalogGovernor(DataplexCatalogClient(project="p", dataset="d", verbose=False))

    with mock.patch("google.auth.default", side_effect=OSError("no ADC file")):
        decision = governor.check_table("authorizations")

    assert decision.allowed is False
    assert "no local credential" in decision.reason

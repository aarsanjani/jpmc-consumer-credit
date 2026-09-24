"""BearerTokenSource: static tokens stay static, ADC refreshes only when invalid."""

import threading
from unittest import mock

import pytest

from backend.gcp_auth import BearerTokenSource


class FakeCreds:
    def __init__(self):
        self.valid = False
        self.token = None
        self.refreshes = 0

    def refresh(self, _request):
        self.refreshes += 1
        self.token = "adc-%d" % self.refreshes
        self.valid = True


def _source(explicit=None, env_vars=("X_TOKEN",)):
    return BearerTokenSource(explicit, env_vars, on_error=lambda msg: RuntimeError(msg))


def test_explicit_token_wins_and_is_not_refreshable(monkeypatch):
    monkeypatch.setenv("X_TOKEN", "env")
    src = _source(explicit="given")
    assert src.get() == "given"
    assert src.get(force_refresh=True) == "given"
    assert src.refreshable is False


def test_env_token_is_static(monkeypatch):
    monkeypatch.setenv("X_TOKEN", "env")
    src = _source()
    assert src.get(force_refresh=True) == "env"
    assert src.refreshable is False


def test_adc_refreshes_only_when_invalid(monkeypatch):
    monkeypatch.delenv("X_TOKEN", raising=False)
    creds = FakeCreds()
    with mock.patch("google.auth.default", return_value=(creds, "p")):
        src = _source()
        assert src.refreshable is True
        assert src.get() == "adc-1"
        assert src.get() == "adc-1"
        creds.valid = False  # google-auth marks it expired
        assert src.get() == "adc-2"
        assert src.get(force_refresh=True) == "adc-3"


def test_concurrent_callers_refresh_once(monkeypatch):
    monkeypatch.delenv("X_TOKEN", raising=False)
    creds = FakeCreds()
    with mock.patch("google.auth.default", return_value=(creds, "p")):
        src = _source()
        threads = [threading.Thread(target=src.get) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert creds.refreshes == 1


def test_missing_credentials_raise_callers_error(monkeypatch):
    monkeypatch.delenv("X_TOKEN", raising=False)
    with mock.patch("google.auth.default", side_effect=Exception("no ADC")):
        with pytest.raises(RuntimeError, match="no ADC"):
            _source().get()

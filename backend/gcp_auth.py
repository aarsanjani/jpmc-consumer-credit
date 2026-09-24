"""Bearer tokens for the hand-rolled REST clients.

Used by `backend/memory_bank.py:MemoryBankClient` and
`backend/knowledge_catalog.py:DataplexCatalogClient`, which call Google APIs
with urllib and so do not get google-auth's automatic refresh for free.
"""

import os
import threading
from collections.abc import Sequence
from typing import Any

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class CredentialsUnavailableError(RuntimeError):
    """No usable local credential, so no request was sent.

    Kept apart from HTTP 401/403 on purpose: a 401 means the server rejected a
    credential, this means we never had one to send.
    """


class BearerTokenSource:
    """Hands out a valid access token, in this order:

    1. `explicit_token` passed by the caller,
    2. the first set variable in `env_vars`,
    3. Application Default Credentials, refreshed when google-auth says the
       token is no longer valid (its check already includes a clock-skew margin).

    Tokens from 1 and 2 are static: their owner controls their lifetime, so they
    are never swapped for ADC behind the caller's back. Only ADC tokens refresh.
    """

    def __init__(self, explicit_token: str | None, env_vars: Sequence[str]):
        self._explicit_token = explicit_token
        self._env_vars = tuple(env_vars)
        self._creds: Any = None
        # Flask serves requests on several threads that share one client.
        self._lock = threading.Lock()

    def _static_token(self) -> str | None:
        if self._explicit_token:
            return self._explicit_token
        return next((os.environ[name] for name in self._env_vars if os.environ.get(name)), None)

    @property
    def refreshable(self) -> bool:
        return self._static_token() is None

    def get(self, force_refresh: bool = False) -> str:
        if static := self._static_token():
            return static
        with self._lock:
            try:
                import google.auth
                from google.auth.transport.requests import Request

                # Re-reading ADC on a forced refresh picks up a fresh
                # `gcloud auth application-default login` without a restart.
                if self._creds is None or force_refresh:
                    self._creds, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
                if force_refresh or not self._creds.valid:
                    self._creds.refresh(Request())
                return self._creds.token
            except Exception as exc:
                raise CredentialsUnavailableError(
                    "No credential available. Pass access_token=, set %s, or run "
                    "`gcloud auth application-default login`." % " or ".join(self._env_vars)
                ) from exc

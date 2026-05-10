"""Cognito m2m (client_credentials) token provider for A2A downstream calls.

Called from tools/a2a.py. One provider instance per invoke_agent() call;
underlying ``_fetch_m2m_creds`` caches the Cognito client_id/secret at module
scope across invocations within the same Runtime container lifetime.

Tokens are cached in the instance for up to ``expires_in - 60s`` so that
multi-step agents that call several A2A tools in a row only pay the
token-endpoint round-trip once per invocation.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Callable

import boto3
import httpx

logger = logging.getLogger(__name__)

_CRED_CACHE: dict | None = None
_CRED_LOCK = threading.Lock()


def _fetch_m2m_creds() -> dict:
    """Return {client_id, client_secret} from Secrets Manager. Cached at
    module scope — Secrets Manager is ~1-2 RPS/account for heavy reads and
    the values don't change within a container's lifetime."""
    global _CRED_CACHE
    with _CRED_LOCK:
        if _CRED_CACHE is not None:
            return _CRED_CACHE
        secret_arn = os.environ["A2A_M2M_SECRET_ARN"]
        region = os.environ.get("AWS_REGION", "us-east-1")
        sm = boto3.client("secretsmanager", region_name=region)
        raw = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
        _CRED_CACHE = json.loads(raw)
        return _CRED_CACHE


class M2MTokenProvider:
    """Fetches + caches a Cognito client_credentials access token.

    Callable; returns the current bearer token string. Refresh happens 60s
    before expiry. Any token-endpoint error raises — the caller (tools/a2a.py)
    logs it and returns a user-visible error string to the LLM so the overall
    agent turn doesn't crash.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            self._token, ttl = self._fetch()
            self._expires_at = time.time() + ttl
            return self._token

    def _fetch(self) -> tuple[str, int]:
        creds = _fetch_m2m_creds()
        token_url = os.environ["A2A_COGNITO_TOKEN_URL"]
        scope = os.environ.get("A2A_COGNITO_SCOPE", "a2a-server/invoke")
        r = httpx.post(
            token_url,
            data={"grant_type": "client_credentials", "scope": scope},
            auth=(creds["client_id"], creds["client_secret"]),
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        return body["access_token"], int(body.get("expires_in", 3600))


# Convenience: a single provider per invoke_agent() invocation. agent.py holds
# the instance; inner tool closures call it on every message/send to get a
# fresh bearer.
def build_token_provider() -> Callable[[], str]:
    return M2MTokenProvider()

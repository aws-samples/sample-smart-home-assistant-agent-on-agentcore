"""Tests for agent/tools/a2a_auth.py — M2MTokenProvider.

Uses monkeypatch to:
  - stub Secrets Manager via ``_fetch_m2m_creds`` directly
  - stub httpx.post so the token endpoint is never actually called
"""
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("A2A_M2M_SECRET_ARN", "arn:aws:secretsmanager:us-west-2:123:secret:x")
    monkeypatch.setenv("A2A_COGNITO_TOKEN_URL", "https://cognito.example/oauth2/token")
    monkeypatch.setenv("A2A_COGNITO_SCOPE", "a2a-server/invoke")


@pytest.fixture
def reset_provider():
    # Clear the module-scoped cache between tests.
    from tools import a2a_auth
    a2a_auth._CRED_CACHE = None
    yield
    a2a_auth._CRED_CACHE = None


def _fake_httpx_post(status=200, body=None):
    body = body or {"access_token": "tok-xxx", "expires_in": 3600, "token_type": "Bearer"}
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    resp.json = lambda: body
    return resp


def test_provider_fetches_and_caches(monkeypatch, reset_provider):
    from tools import a2a_auth

    monkeypatch.setattr(a2a_auth, "_fetch_m2m_creds",
                        lambda: {"client_id": "cid", "client_secret": "sec"})
    post_calls = []

    def _post(url, data=None, auth=None, timeout=None):
        post_calls.append({"url": url, "data": data, "auth": auth})
        return _fake_httpx_post()

    monkeypatch.setattr(a2a_auth.httpx, "post", _post)

    p = a2a_auth.M2MTokenProvider()
    tok1 = p()
    tok2 = p()  # should hit cache, no second post
    assert tok1 == "tok-xxx"
    assert tok2 == "tok-xxx"
    assert len(post_calls) == 1
    assert post_calls[0]["url"] == "https://cognito.example/oauth2/token"
    assert post_calls[0]["data"]["grant_type"] == "client_credentials"
    assert post_calls[0]["data"]["scope"] == "a2a-server/invoke"
    assert post_calls[0]["auth"] == ("cid", "sec")


def test_provider_refreshes_when_expiry_near(monkeypatch, reset_provider):
    from tools import a2a_auth

    monkeypatch.setattr(a2a_auth, "_fetch_m2m_creds",
                        lambda: {"client_id": "cid", "client_secret": "sec"})
    counter = {"n": 0}

    def _post(url, data=None, auth=None, timeout=None):
        counter["n"] += 1
        return _fake_httpx_post(body={"access_token": f"tok-{counter['n']}", "expires_in": 30})

    monkeypatch.setattr(a2a_auth.httpx, "post", _post)

    p = a2a_auth.M2MTokenProvider()
    tok1 = p()
    # Since TTL is 30s < 60s buffer → next call forces a refresh.
    tok2 = p()
    assert tok1 != tok2
    assert counter["n"] == 2


def test_provider_propagates_token_endpoint_error(monkeypatch, reset_provider):
    from tools import a2a_auth

    monkeypatch.setattr(a2a_auth, "_fetch_m2m_creds",
                        lambda: {"client_id": "cid", "client_secret": "sec"})

    resp = MagicMock()
    resp.raise_for_status = MagicMock(side_effect=RuntimeError("401 Unauthorized"))

    monkeypatch.setattr(a2a_auth.httpx, "post", lambda *a, **kw: resp)

    p = a2a_auth.M2MTokenProvider()
    with pytest.raises(RuntimeError):
        p()


def test_fetch_m2m_creds_uses_secrets_manager(monkeypatch, reset_provider):
    from tools import a2a_auth

    sm = MagicMock()
    sm.get_secret_value.return_value = {
        "SecretString": '{"client_id": "ci", "client_secret": "se"}'
    }
    monkeypatch.setattr(a2a_auth.boto3, "client", lambda name, region_name=None: sm)

    creds1 = a2a_auth._fetch_m2m_creds()
    creds2 = a2a_auth._fetch_m2m_creds()  # cached
    assert creds1 == {"client_id": "ci", "client_secret": "se"}
    assert creds2 is creds1
    sm.get_secret_value.assert_called_once_with(SecretId="arn:aws:secretsmanager:us-west-2:123:secret:x")

"""Verify the end user's Cognito idToken forwarded on an A2A hop.

A sub-agent that touches a user's devices needs that user's identity, so the
orchestrator forwards the idToken it was given in a second header
(`X-SuperApp-User-Token`) — `Authorization` is taken by the m2m token the
Runtime's CUSTOM_JWT authorizer checks.

The whole point is to verify it here rather than trust it. Anyone can write three
base64 segments separated by dots, so reading the payload without checking the
signature is the same as having no identity check at all: the sub-agent would
happily control whatever devices the caller named. Every request therefore gets:

  - signature checked against the pool's JWKS (by `kid`)
  - `iss` equal to this pool
  - `aud` equal to the app client that issued it
  - `token_use == "id"` (an access token must not pass as a user identity)
  - `exp` / `iat` checked, with a small clock skew allowance

Returns the `sub`, which is what partitions device state and what Cedar
authorises on. `email` comes back too because the knowledge base scopes by email.

Note this module never logs the token itself: the sub-agent holds a live user
credential for the length of a request, and a token in CloudWatch outlives the
request by the log retention period.
"""

from __future__ import annotations

import logging
import os
import time
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

# Cognito's own guidance for token validation. Small enough that an expired token
# stays rejected, large enough to absorb ordinary clock drift between hosts.
CLOCK_SKEW_SECONDS = 60


class UserTokenError(Exception):
    """Raised when a forwarded user token cannot be trusted."""


@lru_cache(maxsize=4)
def _jwks_for(region: str, pool_id: str, _bucket: int) -> dict[str, Any]:
    """Fetch the pool's JWKS.

    Cached, but with a time bucket in the key so a key rotation is picked up
    within the hour instead of requiring a redeploy. Cognito rotates signing keys
    without notice, and a permanently cached JWKS turns that into a total outage
    where every request fails signature validation.
    """
    import httpx

    url = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json"
    resp = httpx.get(url, timeout=5)
    resp.raise_for_status()
    return resp.json()


def _jwks(region: str, pool_id: str) -> dict[str, Any]:
    return _jwks_for(region, pool_id, int(time.time() // 3600))


def verify_user_token(
    token: str,
    region: str | None = None,
    pool_id: str | None = None,
    client_id: str | None = None,
) -> dict[str, Any]:
    """Verify a forwarded Cognito idToken. Returns its claims.

    Raises UserTokenError on anything short of a fully valid token — there is no
    partial-trust path. Config comes from the environment when not passed:
    COGNITO_REGION / COGNITO_USER_POOL_ID / COGNITO_APP_CLIENT_ID.
    """
    from jose import jwt
    from jose.exceptions import JWTError

    region = region or os.environ.get("COGNITO_REGION") or os.environ.get("AWS_REGION", "")
    pool_id = pool_id or os.environ.get("COGNITO_USER_POOL_ID", "")
    client_id = client_id or os.environ.get("COGNITO_APP_CLIENT_ID", "")

    if not region or not pool_id:
        raise UserTokenError(
            "COGNITO_REGION / COGNITO_USER_POOL_ID are not configured, so the "
            "forwarded user token cannot be verified"
        )

    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        token = token.split(" ", 1)[1].strip()
    if not token:
        raise UserTokenError("empty user token")

    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise UserTokenError(f"malformed token header: {exc}") from exc

    kid = header.get("kid")
    if not kid:
        raise UserTokenError("token header has no kid")

    try:
        jwks = _jwks(region, pool_id)
    except Exception as exc:
        raise UserTokenError(f"could not fetch JWKS: {exc}") from exc

    key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if key is None:
        # Could be a rotation the cache hasn't seen; drop the bucket and retry once.
        _jwks_for.cache_clear()
        try:
            jwks = _jwks(region, pool_id)
        except Exception as exc:
            raise UserTokenError(f"could not refetch JWKS: {exc}") from exc
        key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if key is None:
        raise UserTokenError(f"no signing key matches kid={kid}")

    issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=[key.get("alg", "RS256")],
            issuer=issuer,
            audience=client_id or None,
            options={
                # Verify the audience only when we know which client to expect.
                # Without this guard a missing env var would silently disable the
                # check instead of failing loudly.
                "verify_aud": bool(client_id),
                "verify_exp": True,
                "verify_iat": True,
                "verify_signature": True,
                # python-jose reads the skew allowance from `options["leeway"]`.
                "leeway": CLOCK_SKEW_SECONDS,
            },
        )
    except JWTError as exc:
        raise UserTokenError(f"token rejected: {exc}") from exc

    # An access token for this pool also carries a valid signature and issuer, so
    # the type has to be checked explicitly — otherwise the m2m token in the
    # Authorization header would pass as a user identity, and it has no `sub`.
    if claims.get("token_use") != "id":
        raise UserTokenError(
            f"token_use is {claims.get('token_use')!r}, expected 'id' — an access "
            f"token is not a user identity"
        )
    if not claims.get("sub"):
        raise UserTokenError("token has no sub")

    return claims

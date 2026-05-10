"""Cognito JWT verification for A2A downstream agents.

Validates the Authorization: Bearer <jwt> header using the Cognito User Pool
JWKS. Rejects requests with 401 when:
  - no Bearer token
  - wrong issuer
  - wrong audience (resource server identifier + scope)
  - token_use != "access"
  - expired / bad signature
  - required scope missing

Exposes a Starlette BaseHTTPMiddleware: `JWTAuthMiddleware`.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

import httpx
from jose import jwt
from jose.exceptions import JWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

SKIP_AUTH_PATHS = {"/ping", "/.well-known/agent-card.json"}


@lru_cache(maxsize=1)
def _get_jwks(region: str, pool_id: str) -> dict[str, Any]:
    url = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json"
    resp = httpx.get(url, timeout=5)
    resp.raise_for_status()
    return resp.json()


def _verify_token(
    token: str,
    region: str,
    pool_id: str,
    required_scope: str,
    expected_client_id: str | None,
) -> dict[str, Any]:
    """Verify a Cognito access token. Returns claims on success, raises on failure."""
    headers = jwt.get_unverified_header(token)
    kid = headers.get("kid")
    jwks = _get_jwks(region, pool_id)
    key = next((k for k in jwks.get("keys", []) if k["kid"] == kid), None)
    if key is None:
        raise JWTError(f"no matching JWK for kid={kid}")

    issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
    claims = jwt.decode(
        token,
        key,
        algorithms=[key.get("alg", "RS256")],
        issuer=issuer,
        options={"verify_aud": False},
    )
    if claims.get("token_use") != "access":
        raise JWTError("token_use must be 'access'")
    if expected_client_id and claims.get("client_id") != expected_client_id:
        raise JWTError(f"client_id mismatch: got {claims.get('client_id')}")
    scope_str = claims.get("scope", "")
    if required_scope not in scope_str.split():
        raise JWTError(f"scope '{required_scope}' missing from token")
    return claims


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """Starlette middleware enforcing Cognito JWT on non-public paths.

    Environment variables (required):
      COGNITO_REGION, COGNITO_USER_POOL_ID, EXPECTED_SCOPE

    Optional:
      EXPECTED_CLIENT_ID — if set, token.client_id must match (extra defense).
    """

    def __init__(self, app):
        super().__init__(app)
        self.region = os.environ["COGNITO_REGION"]
        self.pool_id = os.environ["COGNITO_USER_POOL_ID"]
        self.required_scope = os.environ["EXPECTED_SCOPE"]
        self.expected_client_id = os.environ.get("EXPECTED_CLIENT_ID") or None

    async def dispatch(self, request: Request, call_next):
        if request.url.path in SKIP_AUTH_PATHS:
            return await call_next(request)

        authz = request.headers.get("authorization", "")
        if not authz.lower().startswith("bearer "):
            return JSONResponse({"error": "missing bearer token"}, status_code=401)

        token = authz.split(" ", 1)[1].strip()
        try:
            claims = _verify_token(
                token,
                region=self.region,
                pool_id=self.pool_id,
                required_scope=self.required_scope,
                expected_client_id=self.expected_client_id,
            )
        except JWTError as exc:
            logger.info("JWT rejected: %s", exc)
            return JSONResponse({"error": f"unauthorized: {exc}"}, status_code=401)
        except Exception as exc:
            logger.warning("JWT verification error: %s", exc)
            return JSONResponse({"error": "auth check failed"}, status_code=401)

        request.state.jwt_claims = claims
        request.state.allowed_skills = [
            s.strip()
            for s in request.headers.get("x-a2a-allowed-skills", "").split(",")
            if s.strip()
        ]
        return await call_next(request)

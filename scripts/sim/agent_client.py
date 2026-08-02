"""Invoke the AgentCore Runtime as a real end user.

Mirrors exactly what the chatbot does (see chatbot/src/voice/sigv4.ts and
chatbot/src/components/ChatInterface.tsx):

  1. Cognito USER_PASSWORD_AUTH  -> idToken
  2. Cognito Identity Pool       -> temporary AWS credentials
  3. SigV4-signed POST           -> /runtimes/{arn}/invocations

Both headers matter:
  * Runtime-Session-Id           groups turns into one conversation
  * Runtime-Custom-AuthToken     the idToken, which the agent forwards to the
                                 Gateway so per-user Cedar policies decide
                                 which tools that user can see

Using the same path as the browser means the telemetry we generate is
indistinguishable from real traffic — same spans, same session shape, same
per-user tool scoping.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.parse
from dataclasses import dataclass, field

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

SERVICE = "bedrock-agentcore"
CUSTOM_AUTH_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken"
SESSION_ID_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

# AgentCore rejects runtimeSessionId shorter than this with a 400:
#   "Member must have length greater than or equal to 33"
# The chatbot's `user-session-{sub}-{epoch_ms}` shape is ~63 chars, comfortably
# over. Don't "simplify" the session id — it will start failing.
MIN_SESSION_ID_LEN = 33

# Cold starts dominate the first call; measured 3.7-17.1s warm, longer cold.
DEFAULT_TIMEOUT = 180


def _decode_jwt_claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)  # restore base64 padding
    return json.loads(base64.urlsafe_b64decode(payload).decode())


@dataclass
class Turn:
    """One request/response exchange, shaped for JSONL logging."""

    prompt: str
    status: int
    elapsed_s: float
    response: str
    scenario: str = ""
    error: str = ""
    session_id: str = ""
    # Heuristic: did the reply indicate the agent actually did something vs
    # falling back to a refusal? Used only for the run summary, never to
    # decide pass/fail — a refusal is legitimate data for some scenarios.
    looks_refused: bool = field(default=False)

    def to_json(self) -> dict:
        return {
            "scenario": self.scenario,
            "prompt": self.prompt,
            "status": self.status,
            "elapsed_s": round(self.elapsed_s, 2),
            "response": self.response,
            "looks_refused": self.looks_refused,
            "session_id": self.session_id,
            "error": self.error,
        }


REFUSAL_MARKERS = (
    "超出我的知识范围",
    "超出我当前的工具",
    "beyond my knowledge",
    "outside my current tool",
    "tool appears to be unavailable",
)


def _looks_refused(text: str) -> bool:
    return any(m in text for m in REFUSAL_MARKERS)


class AgentUser:
    """A signed-in end user that can hold a multi-turn conversation.

    One instance == one logged-in user. Call `start_session()` to begin a fresh
    conversation; every `say()` after that shares the session id so the agent
    sees real conversational context (and AgentCore Memory accumulates).
    """

    def __init__(self, email: str, password: str, cfg: dict):
        self.email = email
        self.password = password
        self.region = cfg["region"]
        self.user_pool_id = cfg["userPoolId"]
        self.client_id = cfg["userPoolClientId"]
        self.identity_pool_id = cfg["identityPoolId"]
        self.runtime_arn = cfg["agentRuntimeArn"]
        self._idp = boto3.client("cognito-idp", region_name=self.region)
        self._ci = boto3.client("cognito-identity", region_name=self.region)
        self._id_token: str | None = None
        self._creds: Credentials | None = None
        self._creds_expiry: float = 0.0
        self.sub: str | None = None
        self.session_id: str | None = None

    # -- auth ---------------------------------------------------------------

    def login(self) -> None:
        r = self._idp.initiate_auth(
            ClientId=self.client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": self.email, "PASSWORD": self.password},
        )
        self._id_token = r["AuthenticationResult"]["IdToken"]
        self.sub = _decode_jwt_claims(self._id_token)["sub"]
        self._refresh_aws_creds()

    def _refresh_aws_creds(self) -> None:
        provider = f"cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"
        logins = {provider: self._id_token}
        identity_id = self._ci.get_id(
            IdentityPoolId=self.identity_pool_id, Logins=logins
        )["IdentityId"]
        c = self._ci.get_credentials_for_identity(
            IdentityId=identity_id, Logins=logins
        )["Credentials"]
        self._creds = Credentials(c["AccessKeyId"], c["SecretKey"], c["SessionToken"])
        # Renew a minute early rather than racing the expiry.
        self._creds_expiry = c["Expiration"].timestamp() - 60

    def _ensure_auth(self) -> None:
        if self._id_token is None:
            self.login()
        elif time.time() >= self._creds_expiry:
            # A long --heavy run can outlive the credentials; re-login rather
            # than only refreshing, since the idToken expires too (1h).
            self.login()

    # -- conversation -------------------------------------------------------

    def start_session(self) -> str:
        """Begin a new conversation. Same id shape the chatbot uses."""
        self._ensure_auth()
        self.session_id = f"user-session-{self.sub}-{int(time.time() * 1000)}"
        assert len(self.session_id) >= MIN_SESSION_ID_LEN, "session id too short"
        return self.session_id

    def say(self, prompt: str, scenario: str = "", timeout: int = DEFAULT_TIMEOUT) -> Turn:
        """Send one message in the current session and return the exchange."""
        self._ensure_auth()
        if self.session_id is None:
            self.start_session()

        url = (
            f"https://{SERVICE}.{self.region}.amazonaws.com/runtimes/"
            f"{urllib.parse.quote(self.runtime_arn, safe='')}/invocations"
        )
        body = json.dumps({"prompt": prompt, "userId": self.email})
        req = AWSRequest(
            method="POST",
            url=url,
            data=body,
            headers={
                "Content-Type": "application/json",
                SESSION_ID_HEADER: self.session_id,
                CUSTOM_AUTH_HEADER: self._id_token,
            },
        )
        SigV4Auth(self._creds, SERVICE, self.region).add_auth(req)

        t0 = time.time()
        try:
            resp = requests.post(url, data=body, headers=dict(req.headers), timeout=timeout)
        except requests.RequestException as e:
            return Turn(prompt=prompt, status=0, elapsed_s=time.time() - t0,
                        response="", scenario=scenario, error=f"{type(e).__name__}: {e}",
                        session_id=self.session_id or "")
        elapsed = time.time() - t0

        text, err = "", ""
        if resp.status_code == 200:
            try:
                data = resp.json()
                text = data.get("response") or data.get("text") or data.get("content") or ""
            except ValueError:
                text, err = resp.text[:500], "non-JSON response"
        else:
            err = resp.text[:300]

        return Turn(prompt=prompt, status=resp.status_code, elapsed_s=elapsed,
                    response=text, scenario=scenario, error=err,
                    session_id=self.session_id or "",
                    looks_refused=_looks_refused(text))

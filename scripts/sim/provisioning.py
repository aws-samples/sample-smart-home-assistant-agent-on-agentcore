"""Create, configure, inspect and remove simulated users.

Everything here is idempotent and scoped to the `simuser+` prefix — the live
pool holds real users and this module must never be able to touch them.

Two different keys are in play, which is easy to get wrong:
  * tool permissions -> keyed by Cognito **sub**   (PUT /users/{sub}/permissions)
  * model settings   -> keyed by **email**        (PUT /settings/{email})
  * tenant env       -> keyed by **email**        (PUT /skills/{email}/__tenant_env__)
The agent resolves settings/skills by actor_id (the email), while the Gateway's
Cedar policies match on principal.id (the sub).
"""
from __future__ import annotations

import json
import os
import time

import boto3
import requests

SIM_PREFIX = "simuser+"
POLICY_WAIT_SECONDS = 180


class Provisioner:
    def __init__(self, cfg: dict, admin_email: str, admin_password: str,
                 sim_password: str):
        self.cfg = cfg
        self.region = cfg["region"]
        self.pool_id = cfg["userPoolId"]
        self.client_id = cfg["userPoolClientId"]
        self.api = cfg["adminApiUrl"].rstrip("/")
        self.sim_password = sim_password
        self._idp = boto3.client("cognito-idp", region_name=self.region)
        self._ctl = boto3.client("bedrock-agentcore-control", region_name=self.region)
        self._admin_email = admin_email
        self._admin_password = admin_password
        self._admin_token: str | None = None

    # -- helpers ------------------------------------------------------------

    def _admin_headers(self) -> dict:
        if self._admin_token is None:
            r = self._idp.initiate_auth(
                ClientId=self.client_id,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": self._admin_email,
                                "PASSWORD": self._admin_password},
            )
            self._admin_token = r["AuthenticationResult"]["IdToken"]
        return {"Authorization": f"Bearer {self._admin_token}",
                "Content-Type": "application/json"}

    def _sub_for(self, email: str) -> str:
        u = self._idp.admin_get_user(UserPoolId=self.pool_id, Username=email)
        return next(a["Value"] for a in u["UserAttributes"] if a["Name"] == "sub")

    @staticmethod
    def _guard(email: str) -> None:
        """Refuse to act on anything outside the simulated-user namespace."""
        if not email.startswith(SIM_PREFIX):
            raise ValueError(f"refusing to touch non-simulated user: {email}")

    # -- create / configure -------------------------------------------------

    def ensure_user(self, email: str) -> tuple[str, bool]:
        """Create the user if absent; always (re)set the password so login
        works. Returns (sub, created)."""
        self._guard(email)
        created = False
        try:
            self._idp.admin_create_user(
                UserPoolId=self.pool_id,
                Username=email,
                UserAttributes=[{"Name": "email", "Value": email},
                                {"Name": "email_verified", "Value": "true"}],
                MessageAction="SUPPRESS",  # no invite email for test users
            )
            created = True
        except self._idp.exceptions.UsernameExistsException:
            pass
        self._idp.admin_set_user_password(
            UserPoolId=self.pool_id, Username=email,
            Password=self.sim_password, Permanent=True,
        )
        return self._sub_for(email), created

    def grant_tools(self, sub: str, tools: list[str]) -> int:
        """Grant gateway tools. Returns HTTP status.

        NOTE: this returns 200 even when the underlying Cedar update fails —
        always follow with wait_for_policies_active().
        """
        r = requests.put(f"{self.api}/users/{sub}/permissions",
                         headers=self._admin_headers(),
                         json={"allowedTools": tools}, timeout=180)
        return r.status_code

    def submit_feedback(self, email: str, vote: str, turn_id: str,
                        session_id: str = "", reason: str = "",
                        turn_prompt: str = "", ts: str = "") -> int:
        """File one 👍/👎 through the same API the chatbot uses.

        Tagged `source="sim"` so the dashboard can state what share of the
        satisfaction figure is simulated. Only an admin may set that tag or
        backdate `ts` — otherwise any client could file votes as simulated and
        the "N% simulated" note, the one thing keeping the card honest, would
        mean nothing.

        `ts` is what lets a pre-demo run lay votes across past days so the 60d
        and 90d views have something to show. It only moves rows we write:
        spans and evaluation scores are stamped by AgentCore and cannot be
        backdated, so those series stay honestly sparse.
        """
        self._guard(email)
        body = {"userId": email, "vote": vote, "turnId": turn_id,
                "source": "sim"}
        if session_id:
            body["sessionId"] = session_id
        if reason:
            body["reason"] = reason
        if turn_prompt:
            body["turnPrompt"] = turn_prompt
        if ts:
            body["ts"] = ts
        r = requests.post(f"{self.api}/sessions?action=feedback",
                          headers=self._admin_headers(), json=body, timeout=60)
        return r.status_code

    def set_model(self, email: str, model_id: str) -> int:
        self._guard(email)
        r = requests.put(f"{self.api}/settings/{email}",
                         headers=self._admin_headers(),
                         json={"modelId": model_id}, timeout=120)
        return r.status_code

    def set_tenant_env(self, email: str, mode: str) -> int:
        self._guard(email)
        body = {"mode": mode}
        if mode == "ab-bundles":
            # This mode masks any per-user prompt override; test users have
            # none, but the API requires the explicit acknowledgement.
            body["acknowledgeMaskedOverride"] = True
        r = requests.put(f"{self.api}/skills/{email}/__tenant_env__",
                         headers=self._admin_headers(), json=body, timeout=120)
        return r.status_code

    # -- Cedar policy state -------------------------------------------------

    def policy_states(self) -> dict[str, str]:
        engines = self._ctl.list_policy_engines().get("policyEngines", [])
        target = next((e for e in engines
                       if "SmartHome" in (e.get("name") or "")), None)
        if not target:
            return {}
        pid = target.get("policyEngineId") or target.get("id")
        out = {}
        for p in self._ctl.list_policies(policyEngineId=pid).get("policies", []):
            full = self._ctl.get_policy(policyEngineId=pid, policyId=p["policyId"])
            out[p["name"]] = full.get("status", "UNKNOWN")
        return out

    def wait_for_policies_active(self, timeout: int = POLICY_WAIT_SECONDS) -> tuple[bool, dict]:
        """Block until every tool policy is ACTIVE.

        This exists because grant_tools() lies: the API returns 200 while the
        Cedar attach can still land in UPDATE_FAILED, after which the Gateway
        serves that user ZERO tools and the agent silently degrades to a
        refusal. Running scenarios before this settles produces misleading
        "tool unavailable" data. Measured settle time ~75s.
        """
        deadline = time.time() + timeout
        states: dict[str, str] = {}
        while time.time() < deadline:
            states = self.policy_states()
            if states and all(s == "ACTIVE" for s in states.values()):
                return True, states
            if any(s.endswith("FAILED") for s in states.values()):
                return False, states
            time.sleep(10)
        return False, states

    # -- inspect / remove ---------------------------------------------------

    def list_sim_users(self) -> list[dict]:
        out, token = [], None
        while True:
            kw = {"UserPoolId": self.pool_id, "Limit": 60}
            if token:
                kw["PaginationToken"] = token
            resp = self._idp.list_users(**kw)
            for u in resp.get("Users", []):
                attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
                email = attrs.get("email", u["Username"])
                if email.startswith(SIM_PREFIX):
                    out.append({"email": email, "sub": attrs.get("sub", ""),
                                "status": u.get("UserStatus", ""),
                                "created": str(u.get("UserCreateDate", ""))})
            token = resp.get("PaginationToken")
            if not token:
                break
        return sorted(out, key=lambda x: x["email"])

    def describe(self, email: str) -> dict:
        """Fetch the live config for one simulated user (for `status`)."""
        ddb = boto3.resource("dynamodb", region_name=self.region).Table(
            self.cfg["skillsTableName"])
        info: dict = {"email": email}
        try:
            sub = self._sub_for(email)
            info["sub"] = sub
            perms = ddb.get_item(Key={"userId": sub, "skillName": "__permissions__"}).get("Item")
            info["tools"] = sorted(perms.get("allowedTools", [])) if perms else []
        except self._idp.exceptions.UserNotFoundException:
            info["missing"] = True
            return info
        settings = ddb.get_item(Key={"userId": email, "skillName": "__settings__"}).get("Item")
        info["model"] = (settings or {}).get("modelId") or "(global default)"
        tenant = ddb.get_item(
            Key={"userId": "__global__", "skillName": f"__tenant_env_{email}__"}).get("Item")
        info["tenantEnv"] = (tenant or {}).get("mode", "default")
        sessions = ddb.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("userId").eq(email))
        info["ddbRows"] = len(sessions.get("Items", []))
        return info

    def remove_user(self, email: str) -> str:
        """Revoke grants then delete. Scoped to the simuser+ prefix."""
        self._guard(email)
        try:
            sub = self._sub_for(email)
        except self._idp.exceptions.UserNotFoundException:
            return "absent"
        # Drop from Cedar first so the policy doesn't keep a dangling sub.
        try:
            self.grant_tools(sub, [])
        except Exception:  # noqa: BLE001 — deletion must proceed regardless
            pass
        self._idp.admin_delete_user(UserPoolId=self.pool_id, Username=email)
        return "deleted"


def load_config(repo_root: str) -> dict:
    """Read the deployed stack outputs into the shape the sim modules want."""
    with open(os.path.join(repo_root, "cdk-outputs.json")) as f:
        out = json.load(f)["SmartHomeAssistantStack"]
    region = out["AdminApiUrl"].split(".")[2]
    runtime_arn = _runtime_arn_from_lambda(region)
    return {
        "region": region,
        "userPoolId": out["UserPoolId"],
        "userPoolClientId": out["UserPoolClientId"],
        "identityPoolId": out["IdentityPoolId"],
        "adminApiUrl": out["AdminApiUrl"],
        "skillsTableName": out["SkillsTableName"],
        "agentRuntimeArn": runtime_arn,
        "adminUsername": out["AdminUsername"],
        "adminPassword": out["AdminPassword"],
    }


def _runtime_arn_from_lambda(region: str) -> str:
    """The runtime ARN isn't a CDK output — it's patched into the admin Lambda's
    env by setup-agentcore.py, so read it from there."""
    lam = boto3.client("lambda", region_name=region)
    env = lam.get_function_configuration(
        FunctionName="smarthome-admin-api")["Environment"]["Variables"]
    arn = env.get("AGENT_RUNTIME_ARN", "")
    if not arn or "PLACEHOLDER" in arn:
        raise SystemExit(
            "AGENT_RUNTIME_ARN missing from the admin Lambda env. Re-run "
            "scripts/setup-agentcore.py (a bare `cdk deploy` resets it)."
        )
    return arn

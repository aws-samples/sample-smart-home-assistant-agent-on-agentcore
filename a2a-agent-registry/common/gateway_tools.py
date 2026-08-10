"""Shared plumbing for sub-agents that call the tools Gateway as the end user.

Every tool-using sub-agent needs the same four things, and getting any of them
wrong is a security bug rather than a bug:

  - the Gateway URL, whose env var name embeds the gateway's own name
  - an MCP client authenticated with the CALLER's idToken, not this runtime's
    service identity, so the Gateway's CUSTOM_JWT check and Cedar both evaluate
    the real end user
  - `user_id` injected from the verified identity and absent from every
    model-facing signature
  - a tool result flattened out of the MCPToolResult TypedDict

The alternative — each agent's `tools.py` repeating it — means each new agent is
another chance to hand the model a `user_id` parameter or to give the runtime its
own IoT permissions. The point of forwarding the token is that a sub-agent holds
no device permissions of its own; this module is where that holds.
"""

from __future__ import annotations

import json
import logging
import os
import uuid

logger = logging.getLogger(__name__)

# Gateway tool names arrive prefixed with their target
# (`SmartHomeDeviceControl___control_device`), so callers match on the suffix.
CONTROL = "control_device"
DISCOVER = "discover_devices"
QUERY_STATE = "query_device_state"
QUERY_HISTORY = "query_sensor_history"
QUERY_KB = "query_knowledge_base"
NAVIGATE = "navigate_to_page"


def gateway_url() -> str:
    """The tools Gateway URL, from whichever env var the platform set.

    `agentcore deploy` injects `AGENTCORE_GATEWAY_<NAME>_URL`, so the exact key
    depends on the gateway's name — hence the prefix scan rather than a hardcoded
    lookup. An explicit AGENTCORE_GATEWAY_URL wins if set.
    """
    direct = os.environ.get("AGENTCORE_GATEWAY_URL", "")
    if direct:
        return direct
    for key, value in os.environ.items():
        if key.startswith("AGENTCORE_GATEWAY_") and key.endswith("_URL"):
            return value
    return ""


def mcp_text(result) -> str:
    """Flatten an MCP tool result into the string a Strands tool must return.

    `call_tool_sync` returns an MCPToolResult, which is a TypedDict — so the
    payload is `result["content"][i]["text"]`, not `result.content[i].text`.
    Handling only the attribute form leaves the model reading a stringified Python
    dict with the answer buried inside it: technically the data, practically
    unusable. Both shapes are handled since a future SDK version may return either.
    """
    content = None
    if isinstance(result, dict):
        content = result.get("content")
    if content is None:
        content = getattr(result, "content", None)
    if not content:
        return str(result)

    texts = []
    for item in content:
        if isinstance(item, dict):
            if "text" in item:
                texts.append(item["text"])
        elif hasattr(item, "text"):
            texts.append(item.text)
    if texts:
        return "\n".join(texts)
    return json.dumps(content, default=str)


class GatewaySession:
    """An open MCP session against the tools Gateway, scoped to one caller.

    Built per request (see common/server.py on why tools cannot be shared across
    requests). `available` maps each wanted tool suffix to its real prefixed name;
    a suffix missing from it means Cedar did not permit that tool for this user —
    default-deny hides it from `list_tools` rather than failing the call — so it
    doubles as a report of what this user may do.
    """

    def __init__(self, caller, wanted: tuple[str, ...]):
        from mcp.client.streamable_http import streamablehttp_client
        from strands.tools.mcp.mcp_client import MCPClient

        url = gateway_url()
        if not url:
            raise RuntimeError(
                "no AGENTCORE_GATEWAY_*_URL in the environment — this agent "
                "cannot reach any gateway tools")
        if not caller.sub:
            raise RuntimeError(
                "no verified user identity — refusing to open a gateway session")

        self.caller = caller
        # The user's own token, so the Gateway and Cedar see the real end user.
        self.client = MCPClient(lambda: streamablehttp_client(
            url, headers={"Authorization": f"Bearer {caller.raw_token}"}))
        # The background pump has to stay alive for as long as the tools might be
        # called. The per-request Agent is discarded when the request ends, and
        # this client with it.
        self.client.start()

        self.available: dict[str, str] = {}
        try:
            tools = []
            token = None
            while True:
                page = self.client.list_tools_sync(pagination_token=token)
                tools.extend(page)
                if page.pagination_token is None:
                    break
                token = page.pagination_token
            for t in tools:
                name = getattr(t, "tool_name", "")
                for suffix in wanted:
                    if name == suffix or name.endswith("___" + suffix):
                        self.available[suffix] = name
        except Exception as exc:  # noqa: BLE001
            self.client.stop(None, None, None)
            raise RuntimeError(f"could not reach the device gateway: {exc}") from exc

        logger.info("gateway tools available to sub=%s...: %s",
                    caller.sub[:8], sorted(self.available))

    def has(self, suffix: str) -> bool:
        return suffix in self.available

    def call(self, suffix: str, args: dict, user_id: str | None = None) -> str:
        """Invoke a gateway tool with the caller's identity injected.

        `user_id` defaults to the caller's `sub`, which is what partitions device
        state. The knowledge base scopes by EMAIL instead, so that caller passes
        `caller.email` explicitly — the two identifiers are not interchangeable
        and using the wrong one silently returns another scope's documents (or,
        more likely, none).
        """
        return mcp_text(self.client.call_tool_sync(
            tool_use_id=str(uuid.uuid4()),
            name=self.available[suffix],
            arguments={**args, "user_id": user_id or self.caller.sub},
        ))

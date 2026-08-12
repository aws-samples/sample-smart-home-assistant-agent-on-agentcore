"""Tests for agent/tools/a2a.py — build_a2a_tools + AgentCard resolution.

Mock boundaries:
  * ``_fetch_agent_card_cached`` — monkeypatched to return a canned dict per
    recordId; the underlying boto3 client never runs.
  * ``_send_a2a_message`` — monkeypatched so we don't open httpx connections.
  * ``strands.tool`` — we use the real decorator so the Strands tool object
    shape (name, signature) matches production.
"""
import asyncio
import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("REGISTRY_ID", "test-registry")


CARD_ENERGY = {
    "protocolVersion": "0.3.0",
    "name": "energy-optimization-agent",
    "url": "https://runtime/energy/invocations/",
    "version": "1.0.0",
    "skills": [
        {"id": "estimate_savings", "name": "Estimate", "description": "Estimate savings.",
         "examples": ["How much can I save dimming LEDs?"]},
        {"id": "tariff_analysis", "name": "Tariff", "description": "Tariff analysis.",
         "examples": []},
    ],
}

CARD_SECURITY = {
    "protocolVersion": "0.3.0",
    "name": "home-security-agent",
    "url": "https://runtime/security/invocations/",
    "version": "1.0.0",
    "skills": [
        {"id": "risk_assessment", "name": "Risk", "description": "Risk assessment.",
         "examples": []},
    ],
}


def _patch_card_fetch(monkeypatch, cards):
    """Make the registry serve these cards, keyed by AgentCard NAME.

    Grants arrive keyed on the card name now (that is what a Cognito grant group
    encodes), so the lookup is a name -> card map built once per container rather
    than a get per recordId. A name that is absent must be skipped softly, which is
    what the "unknown agent" test relies on.
    """
    from tools import a2a as a2a_mod

    by_name = {c["name"]: c for c in cards.values()}
    monkeypatch.setattr(a2a_mod, "cards_by_name", lambda registry_id: by_name)


def _patch_send(monkeypatch, replies):
    """replies: dict[endpoint_url, callable(message) -> str]"""
    from tools import a2a as a2a_mod

    def _fake_send(endpoint_url, message,
                   user_token=None, card_dict=None):
        handler = replies.get(endpoint_url)
        if handler is None:
            raise RuntimeError(f"no stub for endpoint {endpoint_url}")
        return handler(message)

    monkeypatch.setattr(a2a_mod, "_send_a2a_message", _fake_send)


def test_empty_grants_returns_empty_list():
    from tools.a2a import build_a2a_tools
    assert build_a2a_tools({}, "rid") == []


def test_build_tools_one_per_granted_skill(monkeypatch):
    from tools.a2a import build_a2a_tools

    _patch_card_fetch(monkeypatch, {
        "energy-optimization-agent": CARD_ENERGY,
        "home-security-agent": CARD_SECURITY,
    })

    tools = build_a2a_tools(
        grants={
            "energy-optimization-agent": ["estimate_savings", "tariff_analysis"],
            "home-security-agent": ["risk_assessment"],
        },
        registry_id="test-registry",
    )
    assert len(tools) == 3
    names = sorted(getattr(t, "tool_name", None) or getattr(t, "__name__", "") for t in tools)
    assert names == [
        "a2a_energy_optimization_agent_estimate_savings",
        "a2a_energy_optimization_agent_tariff_analysis",
        "a2a_home_security_agent_risk_assessment",
    ]


def test_build_tools_filters_unauthorized_skills(monkeypatch):
    from tools.a2a import build_a2a_tools

    _patch_card_fetch(monkeypatch, {"energy-optimization-agent": CARD_ENERGY})

    tools = build_a2a_tools(
        grants={"energy-optimization-agent": ["estimate_savings"]},  # tariff_analysis omitted
        registry_id="test-registry",
    )
    assert len(tools) == 1
    name = getattr(tools[0], "tool_name", None) or getattr(tools[0], "__name__", "")
    assert name == "a2a_energy_optimization_agent_estimate_savings"


def test_registry_failure_is_soft(monkeypatch):
    """If fetch_agent_card raises, the tool is skipped — no exception out."""
    from tools import a2a as a2a_mod
    from tools.a2a import build_a2a_tools

    # rec-energy resolves fine, rec-missing blows up — we still get one tool.
    # Only one of the two granted agents has an approved record. The other must be
    # skipped softly: a grant group can outlive the record it names.
    monkeypatch.setattr(a2a_mod, "cards_by_name",
                        lambda registry_id: {"energy-optimization-agent": CARD_ENERGY})

    tools = build_a2a_tools(
        grants={"energy-optimization-agent": ["estimate_savings"], "no-such-agent": ["anything"]},
        registry_id="test-registry",
    )
    assert len(tools) == 1


def test_tool_description_carries_skill_doc_and_examples(monkeypatch):
    from tools.a2a import build_a2a_tools

    _patch_card_fetch(monkeypatch, {"energy-optimization-agent": CARD_ENERGY})

    tools = build_a2a_tools(
        grants={"energy-optimization-agent": ["estimate_savings"]},
        registry_id="test-registry",
    )
    tool = tools[0]
    # Strands tool carries the description on its spec
    spec = getattr(tool, "tool_spec", None) or {}
    desc = spec.get("description", "") if isinstance(spec, dict) else ""
    if not desc:
        # Fallback: check the wrapped callable's __doc__ or __description__.
        desc = getattr(tool, "__description__", "") or tool.__doc__ or ""
    assert "Estimate savings" in desc
    assert "How much can I save dimming LEDs?" in desc


def test_tool_invocation_soft_fails_on_send_error(monkeypatch):
    from tools import a2a as a2a_mod
    from tools.a2a import build_a2a_tools

    _patch_card_fetch(monkeypatch, {"energy-optimization-agent": CARD_ENERGY})

    def _raise(*a, **kw):
        raise RuntimeError("network boom")

    monkeypatch.setattr(a2a_mod, "_send_a2a_message", _raise)

    tools = build_a2a_tools(
        grants={"energy-optimization-agent": ["estimate_savings"]},
        registry_id="test-registry",
    )
    # Invoke the underlying function (strands.tool wraps it but keeps the
    # callable). Strands tools are usually invoked via .invoke() or by the
    # strands runtime; for unit testing, test the wrapped function directly.
    wrapped = tools[0]
    # Find the raw Python function. Different strands versions expose it
    # differently; try common attributes.
    for attr in ("func", "_func", "callable", "__wrapped__"):
        fn = getattr(wrapped, attr, None)
        if callable(fn):
            out = fn("hi")
            break
    else:
        # Fall back to treating the decorator return value as callable.
        out = wrapped("hi")
    assert isinstance(out, str)
    assert out.startswith("A2A agent call failed:")


def test_tool_invocation_returns_remote_reply(monkeypatch):
    from tools import a2a as a2a_mod
    from tools.a2a import build_a2a_tools

    _patch_card_fetch(monkeypatch, {"energy-optimization-agent": CARD_ENERGY})
    calls = []

    def _fake_send(endpoint_url, message,
                   user_token=None, card_dict=None):
        calls.append({
            "endpoint": endpoint_url,
            "message": message,
            "user_token": user_token,
        })
        return f"⟦A2A⟧ echo: {message}"

    monkeypatch.setattr(a2a_mod, "_send_a2a_message", _fake_send)

    tools = build_a2a_tools(
        grants={"energy-optimization-agent": ["estimate_savings"]},
        registry_id="test-registry",
    )
    wrapped = tools[0]
    for attr in ("func", "_func", "callable", "__wrapped__"):
        fn = getattr(wrapped, attr, None)
        if callable(fn):
            out = fn("How much can I save?")
            break
    else:
        out = wrapped("How much can I save?")
    assert out.startswith("⟦A2A⟧")
    assert len(calls) == 1
    assert calls[0]["endpoint"] == CARD_ENERGY["url"]
    assert calls[0]["message"] == "How much can I save?"


# ---------------------------------------------------------------------------
# User identity
#
# The end user's own token IS the credential: the sub-agent's Runtime authorizer
# validates it and checks its `cognito:groups` claim for a grant. It is pinned in
# the tool closure so the LLM can never name a different user.
# ---------------------------------------------------------------------------

def _invoke_tool(wrapped, message):
    for attr in ("func", "_func", "callable", "__wrapped__"):
        fn = getattr(wrapped, attr, None)
        if callable(fn):
            return fn(message)
    return wrapped(message)


def _capture_sends(monkeypatch):
    from tools import a2a as a2a_mod

    calls = []

    def _fake_send(endpoint_url, message,
                   user_token=None, card_dict=None):
        calls.append({"user_token": user_token})
        return "⟦A2A⟧ ok"

    monkeypatch.setattr(a2a_mod, "_send_a2a_message", _fake_send)
    return calls


def test_user_token_is_forwarded_to_the_sub_agent(monkeypatch):
    from tools.a2a import build_a2a_tools

    _patch_card_fetch(monkeypatch, {"energy-optimization-agent": CARD_ENERGY})
    calls = _capture_sends(monkeypatch)

    tools = build_a2a_tools(
        grants={"energy-optimization-agent": ["estimate_savings"]},
        registry_id="test-registry",
        user_token="Bearer user.id.token",
    )
    _invoke_tool(tools[0], "hello")
    assert calls[0]["user_token"] == "Bearer user.id.token"


def test_no_user_token_refuses_locally_rather_than_calling(monkeypatch):
    """The user's token is now the ONLY credential, so a turn without one has
    nothing to authenticate with. It must refuse here rather than send an
    unauthenticated request and surface a 401 as a specialist failure."""
    import httpx
    from tools import a2a as a2a_mod

    opened = []

    class _NoClient:
        def __init__(self, headers=None, timeout=None):
            opened.append(headers)

        async def __aenter__(self):
            raise AssertionError("a request was sent with no credential")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _NoClient)
    with pytest.raises(a2a_mod.A2AUnavailable, match="only credential"):
        a2a_mod._send_a2a_message(
            endpoint_url="https://example.invalid/invocations",
            message="hi",
            user_token=None,
        )
    assert opened == [], "an HTTP client was constructed before the credential check"


def test_the_llm_facing_signature_takes_only_a_message(monkeypatch):
    """The guarantee that makes the closure worth anything: identity is not a
    parameter, so the model cannot supply or override one."""
    from tools.a2a import build_a2a_tools

    _patch_card_fetch(monkeypatch, {"energy-optimization-agent": CARD_ENERGY})
    _capture_sends(monkeypatch)
    tools = build_a2a_tools(
        grants={"energy-optimization-agent": ["estimate_savings"]},
        registry_id="test-registry",
        user_token="Bearer user.id.token",
    )
    spec = tools[0].tool_spec
    props = spec["inputSchema"]["json"]["properties"]
    assert list(props) == ["message"], props
    for forbidden in ("user_token", "user_id", "sub", "authorization"):
        assert forbidden not in props


def test_bearer_prefix_is_stripped_before_the_header_is_set(monkeypatch):
    """The receiving side accepts either form, but sending a doubled prefix is
    the kind of thing that only shows up in a live deploy."""
    import httpx
    from tools import a2a as a2a_mod

    sent = {}

    class _FakeClient:
        def __init__(self, headers=None, timeout=None):
            sent.update(headers or {})

        async def __aenter__(self):
            raise RuntimeError("stop here — headers already captured")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    # The fake aborts as soon as the headers exist — everything after that point
    # is network work this test has no interest in.
    with pytest.raises(RuntimeError, match="headers already captured"):
        a2a_mod._send_a2a_message(
            endpoint_url="https://example.invalid/invocations",
            message="hi",
            user_token="Bearer  user.id.token  ",
        )
    # One header, carrying the end user's own token. The grant travels inside it as
    # a `cognito:groups` claim, so there is nothing else to send.
    assert sent["Authorization"] == "Bearer user.id.token"
    assert "X-SuperApp-User-Token" not in sent
    assert "X-A2A-Allowed-Skills" not in sent


# ---------------------------------------------------------------------------
# Latency: the local card, the shared loop, the breaker
# ---------------------------------------------------------------------------

def test_the_registry_card_is_passed_to_the_send_so_no_fetch_is_needed(monkeypatch):
    """`get_agent_card()` was a whole round trip to the sub-agent whose only used
    field, `card.url`, was overwritten on the next line. build_a2a_tools already
    holds the full card, so it is threaded through instead."""
    from tools import a2a as a2a_mod
    from tools.a2a import build_a2a_tools

    seen = {}

    def _fake_send(endpoint_url, message,
                   user_token=None, card_dict=None):
        seen["card"] = card_dict
        return "ok"

    _patch_card_fetch(monkeypatch, {"energy-optimization-agent": CARD_ENERGY})
    monkeypatch.setattr(a2a_mod, "_send_a2a_message", _fake_send)

    tools = build_a2a_tools(grants={"energy-optimization-agent": ["estimate_savings"]},
                            registry_id="reg")
    _invoke_tool(tools[0], "hi")
    assert seen["card"], "the card was not threaded through; a fetch would happen"
    assert seen["card"]["name"] == "energy-optimization-agent"
    assert seen["card"]["skills"]


def test_a_local_card_carries_the_granted_url_not_the_agents_own():
    """The endpoint we were GRANTED is authoritative. A sub-agent's self-reported
    URL was already overridden; now it is never consulted at all."""
    from tools import a2a as a2a_mod

    card = a2a_mod._local_agent_card(
        {"name": "x-agent", "description": "d", "version": "2.0.0",
         "url": "https://the-agent-says-this/",
         "skills": [{"id": "s1", "name": "S1", "description": "does s1"}]},
        "https://the-registry-granted-this/invocations")
    assert card is not None
    assert str(card.url) == "https://the-registry-granted-this/invocations"
    assert [s.id for s in card.skills] == ["s1"]


def test_a_card_that_cannot_be_built_falls_back_rather_than_sending_garbage():
    """A malformed record must not produce a half-built card — a wrong card is
    worse than a slow one, so the caller pays for the fetch instead."""
    from tools import a2a as a2a_mod

    # AgentCard validates types, not emptiness, so these would otherwise build a
    # card with an empty name and URL and send it to be rejected downstream.
    assert a2a_mod._local_agent_card({}, "") is None
    assert a2a_mod._local_agent_card({"name": "n"}, "") is None, (
        "a card with no endpoint must not be built")
    assert a2a_mod._local_agent_card({}, "https://x/invocations") is None, (
        "a card with no agent name must not be built")


def test_the_event_loop_is_reused_across_delegations():
    """`asyncio.run` closes the loop it creates, so every delegation built a fresh
    one and none could share a connection pool. Three delegations stacked to
    15-45s.

    The loop now runs on its own thread (spec 5 S4), so it is running rather than
    idle between uses — but it is still the SAME loop, which is what the connection
    pool depends on.
    """
    from tools import a2a as a2a_mod

    first = a2a_mod._get_loop()
    assert a2a_mod._get_loop() is first
    assert not first.is_closed()
    assert first.is_running(), "the shared loop should be running on its own thread"


def test_a_dead_loop_is_replaced_rather_than_reused():
    """A loop that has stopped must not be handed out again.

    It cannot be closed while running, so the realistic failure is a loop whose
    thread has gone: `run_coroutine_threadsafe` against it would never complete and
    every delegation would hang until its timeout.
    """
    from tools import a2a as a2a_mod

    original = a2a_mod._get_loop()
    original.call_soon_threadsafe(original.stop)
    for _ in range(50):
        if not original.is_running():
            break
        time.sleep(0.02)
    replacement = a2a_mod._get_loop()
    assert replacement is not original
    assert replacement.is_running()


def test_many_delegations_can_overlap():
    """The bug S4 fixed, and the reason the loop moved to its own thread.

    `run_until_complete` may only be called from the thread that owns the loop.
    Strands calls tools from a POOL of threads and issues independent tool calls
    concurrently (measured: two tools starting within 0.00s of each other), so the
    third concurrent delegation raised "This event loop is already running". That
    error was caught, counted as an endpoint failure, and shown to the user as
    "A2A agent call failed" — blaming a healthy specialist, and tripping its
    circuit breaker after three.
    """
    import threading

    from tools import a2a as a2a_mod

    results: dict[str, str] = {}

    def delegate(name: str) -> None:
        async def work() -> str:
            await asyncio.sleep(0.4)
            return "ok"
        try:
            results[name] = a2a_mod._run_on_loop(work(), timeout=15)
        except Exception as exc:  # noqa: BLE001
            results[name] = f"{type(exc).__name__}: {exc}"

    threads = [threading.Thread(target=delegate, args=(f"c{i}",)) for i in range(6)]
    started = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    elapsed = time.time() - started

    assert all(v == "ok" for v in results.values()), results
    # Six 0.4s tasks: concurrent finishes in well under the 2.4s serial total.
    assert elapsed < 1.5, f"delegations ran serially ({elapsed:.1f}s)"


def test_a_hung_delegation_times_out_instead_of_pinning_a_tool_thread():
    """The backstop above httpx's own timeouts.

    httpx applies connect/read timeouts inside the coroutine, so this should never
    fire in practice — but without it a coroutine that never completes would hold a
    Strands tool thread for the life of the container.
    """
    from tools import a2a as a2a_mod

    async def hang() -> str:
        await asyncio.sleep(30)
        return "never"

    started = time.time()
    with pytest.raises(TimeoutError):
        a2a_mod._run_on_loop(hang(), timeout=1)
    assert time.time() - started < 5

    # And the shared loop must still be usable — a cancelled coroutine must not
    # leave it wedged for every later delegation.
    async def still_fine() -> str:
        return "ok"
    assert a2a_mod._run_on_loop(still_fine(), timeout=10) == "ok"


def test_the_breaker_opens_after_repeated_failures_and_then_cools_down(monkeypatch):
    """A dead specialist should cost one timeout, not one per turn. Per-endpoint,
    so one sick agent does not silence the others."""
    from tools import a2a as a2a_mod

    a2a_mod._breaker.clear()
    endpoint = "https://sick-agent/invocations"

    for _ in range(a2a_mod._BREAKER_THRESHOLD - 1):
        a2a_mod._record_failure(endpoint)
    assert a2a_mod._breaker_open(endpoint) is False, "opened too early"

    a2a_mod._record_failure(endpoint)
    assert a2a_mod._breaker_open(endpoint) is True
    assert a2a_mod._breaker_open("https://healthy-agent/invocations") is False

    # After the cooldown exactly ONE probe is let through — not a full reset, so a
    # still-broken endpoint re-opens on its next failure rather than retrying the
    # whole threshold again. Advance from the REAL opened_at that _record_failure
    # stamped, rather than an arbitrary epoch: the check is
    # `now - opened_at >= cooldown`, so a fixed fake clock in the past keeps it shut.
    opened_at = a2a_mod._breaker[endpoint][1]
    monkeypatch.setattr(a2a_mod.time, "time",
                        lambda: opened_at + a2a_mod._BREAKER_COOLDOWN_SECONDS + 1)
    assert a2a_mod._breaker_open(endpoint) is False
    a2a_mod._record_failure(endpoint)
    assert a2a_mod._breaker_open(endpoint) is True
    a2a_mod._breaker.clear()


def test_a_success_clears_the_failure_count():
    from tools import a2a as a2a_mod

    a2a_mod._breaker.clear()
    endpoint = "https://flaky/invocations"
    a2a_mod._record_failure(endpoint)
    a2a_mod._record_success(endpoint)
    assert endpoint not in a2a_mod._breaker


def test_an_open_breaker_reports_unavailable_rather_than_a_failed_call(monkeypatch):
    """The distinction matters to the model: "unavailable" means it was never
    asked, so it should say so rather than imply the specialist answered badly."""
    from tools import a2a as a2a_mod
    from tools.a2a import build_a2a_tools

    _patch_card_fetch(monkeypatch, {"energy-optimization-agent": CARD_ENERGY})
    tools = build_a2a_tools(grants={"energy-optimization-agent": ["estimate_savings"]},
                            registry_id="reg")

    a2a_mod._breaker.clear()
    for _ in range(a2a_mod._BREAKER_THRESHOLD):
        a2a_mod._record_failure(CARD_ENERGY["url"])
    out = _invoke_tool(tools[0], "hi")
    a2a_mod._breaker.clear()
    assert out.startswith("A2A agent unavailable:")


def test_the_timeouts_are_tiered_rather_than_one_number():
    """Connect fast, read slow: an unreachable agent should fail in a second, but
    one that is thinking has an LLM turn behind it and needs real time. A single
    60s value could not express both."""
    from tools import a2a as a2a_mod

    assert a2a_mod._CONNECT_TIMEOUT < 10
    assert a2a_mod._READ_TIMEOUT > 30


# ---------------------------------------------------------------------------
# Context trimming (spec 5 S2)
#
# A specialist's first event-loop cycle existed only to call `discover_devices`.
# The call is cheap; the LLM turn around it measured at 1.0-1.3s of the ~7s the
# specialist took. The devices are named up front instead.
#
# Appended by the TOOL, not by the orchestrator's prompt. The alternative was to
# instruct the model to include device details in the message it composes, which
# makes a latency fix depend on the model complying every time — and it complies
# unevenly. These tests pin the mechanical behaviour.
# ---------------------------------------------------------------------------

def test_the_device_brief_is_appended_to_the_delegated_message(monkeypatch):
    from tools import a2a as a2a_mod
    from tools.a2a import build_a2a_tools

    _patch_card_fetch(monkeypatch, {"energy-optimization-agent": CARD_ENERGY})
    sent = []

    def _fake_send(endpoint_url, message,
                   user_token=None, card_dict=None):
        sent.append(message)
        return "ok"

    monkeypatch.setattr(a2a_mod, "_send_a2a_message", _fake_send)
    monkeypatch.setattr(a2a_mod, "_device_context",
                        lambda msg: "\n\nDevices already identified: living-strip-1")

    tools = build_a2a_tools(grants={"energy-optimization-agent": ["estimate_savings"]},
                            registry_id="reg")
    _invoke_tool(tools[0], "make the living room cosy")

    # The model's request stays FIRST — the brief is context, not the instruction.
    assert sent[0].startswith("make the living room cosy")
    assert "living-strip-1" in sent[0]


def test_no_brief_leaves_the_message_byte_for_byte_unchanged(monkeypatch):
    """Most requests get no brief, and those must be exactly as before.

    Otherwise every non-device delegation (security, energy, docs) would carry a
    stub sentence for nothing.
    """
    from tools import a2a as a2a_mod
    from tools.a2a import build_a2a_tools

    _patch_card_fetch(monkeypatch, {"energy-optimization-agent": CARD_ENERGY})
    sent = []
    monkeypatch.setattr(
        a2a_mod, "_send_a2a_message",
        lambda endpoint_url, message,
        user_token=None, card_dict=None: sent.append(message) or "ok")
    monkeypatch.setattr(a2a_mod, "_device_context", lambda msg: "")

    tools = build_a2a_tools(grants={"energy-optimization-agent": ["estimate_savings"]},
                            registry_id="reg")
    _invoke_tool(tools[0], "how much could I save?")
    assert sent == ["how much could I save?"]


def test_a_failing_brief_does_not_fail_the_delegation(monkeypatch):
    """The brief is an optimisation; the answer is the product.

    Soft-fails to "" on anything, including the module being absent from the
    container — a delegation that died because a hint could not be built would
    trade a second for the whole reply.
    """
    from tools import a2a as a2a_mod

    def _explode(name):
        raise ImportError("no device_brief in this container")

    monkeypatch.setattr("builtins.__import__", _explode)
    try:
        assert a2a_mod._device_context("dim the bedroom") == ""
    finally:
        monkeypatch.undo()


def test_the_real_brief_names_devices_and_stays_small():
    """Against the actual shared module, not a stub.

    Two claims at once: it resolves real device ids, and it is a fraction of the
    ~1,800-token discovery payload it replaces. A brief that grew to the size of
    the payload would move the cost from a round trip into the prompt, which is
    the trap this optimisation is meant to avoid.
    """
    from tools import a2a as a2a_mod

    ctx = a2a_mod._device_context("Give the living room light strip a calm ocean feel")
    if not ctx:
        import pytest
        pytest.skip("device_brief not present in this checkout's agent/ build")
    assert "living-strip-1" in ctx
    assert "discover_devices" in ctx  # still offered as the fallback
    assert len(ctx) < 2000

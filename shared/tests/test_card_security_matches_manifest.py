"""The security an AgentCard declares is defined twice, and must not drift.

The platform PUBLISHES it (`shared/a2a_manifest.card_security`, served as the
manifest's `card.securitySchemes`) and each sub-agent RENDERS it
(`a2a-agent-registry/common/card.card_security`, into its Registry record and into the
card it serves at `/.well-known/agent-card.json`).

Two copies rather than one shared module, deliberately: the agent side is a CONSUMER of
the published template, exactly as a third party is. If our own agents could import the
definition and a third party could not, the template would be the only thing keeping
them honest and nothing would keep the template honest. So our agents take the same path
a third party takes, and this test is what pins the copy — which is also a test of the
manifest's sufficiency: if the published template were not enough to reproduce, this
file could not be written.

What drift costs: nothing on this platform reads `securitySchemes` to decide anything,
so a mismatch is silent here and lands entirely on a caller who believes the card. It
was already true once — every card advertised an OAuth2 client_credentials flow for
weeks after that mechanism was retired.
"""
import importlib.util
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import a2a_manifest  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT_CARD_PY = os.path.join(REPO, "a2a-agent-registry", "common", "card.py")

POOL = "us-west-2_HwYYt6qLz"
CLIENT = "7031k7ctqes8mfe5jrf7kljttf"
DISCOVERY = (f"https://cognito-idp.us-west-2.amazonaws.com/{POOL}"
             "/.well-known/openid-configuration")
CARD_NAME = "third-party-agent"
DOOR_GROUP = f"a2a-{CARD_NAME}"


@pytest.fixture(scope="module")
def agent_card_module():
    """`common/card.py` loaded from its path.

    Loaded by file rather than imported as `common.card`, because `common` is a package
    inside the sub-agent's own deployment unit and putting it on this test session's
    path would shadow whatever else is called `common`. This works only because every
    `common.*` import in that module is lazy, inside the function that needs it — which
    is a property worth having: the deploy path renders a Registry card without pulling
    in the A2A SDK.
    """
    spec = importlib.util.spec_from_file_location("_subagent_card", AGENT_CARD_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def subagent_card_package():
    """`common.card` imported as part of its package, for the paths that need it.

    `card_security_for` derives the door group from `common.a2a_groups` rather than
    formatting `a2a-<name>` itself — that convention is an authorization boundary and
    has exactly one definition. So testing it needs the real package, not a file load.

    The path entry and every `common*` module are removed afterwards: leaving a package
    named `common` in `sys.modules` would shadow whatever the next test imports.
    """
    sys.path.insert(0, os.path.join(REPO, "a2a-agent-registry"))
    try:
        import common.card as subagent_card  # noqa: PLC0415

        yield subagent_card
    finally:
        sys.path.remove(os.path.join(REPO, "a2a-agent-registry"))
        for name in [m for m in sys.modules if m == "common" or m.startswith("common.")]:
            del sys.modules[name]


@pytest.fixture
def manifest():
    return a2a_manifest.build(
        region="us-west-2", registry_id="Zuy3YNKrPQ5uwE9t", user_pool_id=POOL,
        app_client_id=CLIENT, discovery_url=DISCOVERY)


# ---------------------------------------------------------------------------
# The parity itself
# ---------------------------------------------------------------------------

def test_the_two_definitions_produce_the_same_thing(agent_card_module):
    platform = a2a_manifest.card_security(
        discovery_url=DISCOVERY, door_group=DOOR_GROUP)
    agent = agent_card_module.card_security(
        discovery_url=DISCOVERY, door_group=DOOR_GROUP)
    assert platform == agent


def test_the_scheme_name_agrees(agent_card_module):
    """The key is what `security` references, so a mismatch makes the card
    self-inconsistent — it would require a scheme it does not define."""
    assert a2a_manifest.SECURITY_SCHEME_NAME == \
        agent_card_module.SECURITY_SCHEME_NAME


def test_the_manifest_template_substitutes_into_the_agents_output(
        agent_card_module, manifest):
    """The third-party path, end to end: take the PUBLISHED template, substitute
    `{cardName}`, and you have byte-identical output to what our own agents render."""
    published = json.dumps(manifest["card"]["securitySchemes"])
    substituted = json.loads(published.replace("{cardName}", CARD_NAME))
    rendered, _ = agent_card_module.card_security(
        discovery_url=DISCOVERY, door_group=DOOR_GROUP)
    assert substituted == rendered
    assert manifest["card"]["security"] == \
        agent_card_module.card_security(discovery_url=DISCOVERY,
                                        door_group=DOOR_GROUP)[1]


# ---------------------------------------------------------------------------
# What the declaration must and must not say
# ---------------------------------------------------------------------------

def test_it_names_the_issuer_the_authorizer_validates(manifest):
    """The card's issuer and the authorizer's `discoveryUrl` are the same string. A
    caller who follows the card must end up with a token the door accepts."""
    scheme = manifest["card"]["securitySchemes"][a2a_manifest.SECURITY_SCHEME_NAME]
    assert scheme["openIdConnectUrl"] == manifest["authorizer"]["discoveryUrl"]


def test_it_names_the_door_group_the_authorizer_matches(manifest):
    """Same rule, said twice for two audiences: the human-readable requirement in the
    card and the machine-readable one in `groups.doorGroup`."""
    scheme = manifest["card"]["securitySchemes"][a2a_manifest.SECURITY_SCHEME_NAME]
    assert manifest["groups"]["doorGroup"] in scheme["description"]


def test_the_required_scope_list_is_empty(manifest):
    """Authorization is a group claim, not a scope. A group in a scopes array reads as
    something an OAuth server would issue on request; an administrator grants this."""
    assert manifest["card"]["security"] == \
        [{a2a_manifest.SECURITY_SCHEME_NAME: []}]


def test_the_retired_m2m_flow_appears_nowhere(manifest, agent_card_module):
    """The regression this whole change is about. `clientCredentials` in a card sends a
    third party to fetch a token with no `cognito:groups` claim, which the door refuses
    — while the card says they did as they were told."""
    published = json.dumps(manifest["card"])
    rendered = json.dumps(agent_card_module.card_security(
        discovery_url=DISCOVERY, door_group=DOOR_GROUP))
    for text in (published, rendered):
        assert "clientCredentials" not in text
        assert "oauth2/token" not in text


def test_a_rendered_registry_card_carries_it(subagent_card_package, monkeypatch):
    """`render_card_for_registry` is what actually writes the Registry record, so the
    parity above has to reach it and not just the helper. The door group in it comes
    from `a2a_groups`, which is why this uses the real package."""
    monkeypatch.setenv("COGNITO_REGION", "us-west-2")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", POOL)
    card = subagent_card_package.render_card_for_registry(
        {"name": CARD_NAME, "description": "d", "version": "1.0.0",
         "skills": [{"id": "do_a_thing"}]},
        runtime_url="https://example.invalid/runtimes/x/invocations",
    )
    expected, security = subagent_card_package.card_security(
        discovery_url=DISCOVERY, door_group=DOOR_GROUP)
    assert card["securitySchemes"] == expected
    assert card["security"] == security


def test_an_unresolvable_issuer_yields_an_empty_url_not_a_guess(
        agent_card_module, monkeypatch):
    """A card naming the WRONG issuer refuses every caller and explains nothing. An
    empty one is visibly incomplete, and `a2a_preflight` reports it."""
    monkeypatch.delenv("COGNITO_REGION", raising=False)
    monkeypatch.delenv("COGNITO_USER_POOL_ID", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    assert agent_card_module.discovery_url_from_env() == ""


def test_the_dead_m2m_renderer_is_gone(agent_card_module):
    """`build_agent_card` had no callers and encoded the retired flow. Kept as a test
    because deleting dead code is only safe once, and a well-meaning restore would
    quietly reintroduce the declaration this file exists to prevent."""
    assert not hasattr(agent_card_module, "build_agent_card")

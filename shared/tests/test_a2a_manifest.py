"""Tests for the published platform manifest.

What is worth protecting here is not the field list — it is that every rule in the
manifest is READ OUT of the module that enforces it. A manifest retyped from the docs
is a second definition of an authorization boundary, and its failure mode is a third
party configuring exactly what we published and still being refused, with nothing
anywhere saying why.

So the tests below mostly assert AGREEMENT between the manifest and the enforcement
code, rather than asserting literal strings. A literal-string test would pass happily
while the manifest and the checker drifted apart in the same direction.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import a2a_conformance as conf  # noqa: E402
import a2a_groups  # noqa: E402
import a2a_manifest  # noqa: E402
import a2a_session  # noqa: E402
import agent_registry as registry_ns  # noqa: E402

POOL = "us-west-2_HwYYt6qLz"
CLIENT = "7031k7ctqes8mfe5jrf7kljttf"
DISCOVERY = (f"https://cognito-idp.us-west-2.amazonaws.com/{POOL}"
             "/.well-known/openid-configuration")
GATEWAY = ("https://smarthome-a2a-gw-abc123.gateway.bedrock-agentcore."
           "us-west-2.amazonaws.com")


def _build(**over):
    kwargs = dict(region="us-west-2", registry_id="Zuy3YNKrPQ5uwE9t",
                  user_pool_id=POOL, app_client_id=CLIENT, discovery_url=DISCOVERY)
    kwargs.update(over)
    return a2a_manifest.build(**kwargs)


# ---------------------------------------------------------------------------
# It must be serialisable and self-describing
# ---------------------------------------------------------------------------

def test_the_manifest_is_json_serialisable():
    """It is served over HTTP and pasted into other people's CI."""
    json.dumps(_build(), ensure_ascii=False)


def test_it_carries_a_version_a_consumer_can_pin():
    assert _build()["manifestVersion"] == a2a_manifest.MANIFEST_VERSION


def test_the_deployment_identifiers_are_echoed_verbatim():
    """The values that exist to stop anyone hand-copying them."""
    m = _build()
    assert m["deployment"]["registryId"] == "Zuy3YNKrPQ5uwE9t"
    assert m["deployment"]["userPoolId"] == POOL
    assert m["authorizer"]["discoveryUrl"] == DISCOVERY
    assert m["authorizer"]["allowedAudience"] == [CLIENT]


# ---------------------------------------------------------------------------
# Agreement with the enforcement code — the whole point
# ---------------------------------------------------------------------------

def test_the_claim_rule_matches_what_the_checker_compares_against():
    a = _build()["authorizer"]
    assert a["claimName"] == conf.CLAIM_NAME
    assert a["claimValueType"] == conf.CLAIM_VALUE_TYPE
    assert a["claimMatchOperator"] == conf.CLAIM_OPERATOR


def test_the_published_template_passes_the_real_conformance_check():
    """The strongest statement available: substitute a real card name into the
    template we hand out, and the checker that gates approval must find nothing.

    If this fails, we are publishing a configuration we would then reject.
    """
    card = {"name": "third-party-agent",
            "skills": [{"id": "do_a_thing"}, {"id": "do_another"}]}
    template = json.dumps(_build()["authorizer"]["template"])
    filled = json.loads(template.replace("{cardName}", card["name"]))
    assert conf.check(card, filled, DISCOVERY, CLIENT) == []


def test_the_group_templates_match_the_group_module():
    g = _build()["groups"]
    assert g["prefix"] == a2a_groups.GROUP_PREFIX
    assert g["separator"] == a2a_groups.SEPARATOR
    assert g["maxLength"] == a2a_groups.MAX_GROUP_NAME
    # Substituting into the templates must reproduce what the module emits.
    name, skill = "third-party-agent", "do_a_thing"
    assert g["doorGroup"].replace("{cardName}", name) == \
        a2a_groups.agent_group_name(name)
    assert (g["skillGroup"].replace("{cardName}", name)
            .replace("{skillId}", skill)) == a2a_groups.group_name(name, skill)


def test_the_published_name_pattern_actually_accepts_and_rejects():
    """Published as a regex for someone else's pre-flight check, so it has to work
    when THEY compile it, not just when we do."""
    pattern = re.compile(_build()["groups"]["namePattern"])
    assert pattern.match("third-party-agent")
    assert pattern.match("Agent_9")
    assert not pattern.match("has a space")
    assert not pattern.match("-leading-hyphen")
    assert not pattern.match("has.a.dot")     # `.` is the group separator


def test_the_session_metadata_key_matches_the_session_module():
    """A third party reads the id from this key or joins nothing."""
    assert _build()["invocation"]["sessionIdMetadataKey"] == \
        a2a_session.SESSION_ID_METADATA_KEY


def test_the_lifecycle_rules_match_the_registry_module():
    life = _build()["lifecycle"]
    assert life["inFlightStatuses"] == sorted(registry_ns.IN_FLIGHT_STATUSES)
    assert life["reapprovalGraceSeconds"] == \
        registry_ns.DEFAULT_GRANT_GRACE_SECONDS
    assert _build(grant_grace_seconds=90)["lifecycle"][
        "reapprovalGraceSeconds"] == 90


def test_deprecated_is_published_as_terminal():
    """The one mistake in this system that cannot be undone, so it must be in the
    contract rather than only in a runbook someone did not read."""
    life = _build()["lifecycle"]
    assert life["terminalStatuses"] == ["DEPRECATED"]
    assert "recordId" in life["terminalNote"]


# ---------------------------------------------------------------------------
# The authorizer list must be stable, and must be SAID to be
# ---------------------------------------------------------------------------

def test_the_door_template_is_one_entry_and_names_no_skill():
    """If this ever grows a `{skillId}`, the "no redeploy to add a skill" promise in
    the same document becomes false."""
    a = _build()["authorizer"]
    assert a["matchValueTemplate"] == ["a2a-{cardName}"]
    assert a["stable"] is True
    assert "{skillId}" not in json.dumps(a)


def test_the_skill_groups_are_published_as_NOT_for_the_authorizer():
    """The exact confusion the old design created, called out where it is read."""
    assert "not list these in the authorizer" in \
        _build()["groups"]["skillGroupPurpose"].lower()


# ---------------------------------------------------------------------------
# The optional gateway section
# ---------------------------------------------------------------------------

def test_the_gateway_section_is_absent_when_there_is_no_gateway():
    """A blank field would read as a broken deployment rather than an unused option."""
    assert "gateway" not in _build()


def test_the_gateway_target_url_is_derivable_from_the_card_name():
    gw = _build(gateway_url=GATEWAY)["gateway"]
    assert gw["optional"] is True
    filled = (gw["targetUrlTemplate"]
              .replace("{targetName}",
                       gw["targetNameTemplate"].replace("{cardName}", "my-agent")))
    assert filled == f"{GATEWAY}/my-agent"


def test_a_trailing_slash_on_the_gateway_url_does_not_double_up():
    gw = _build(gateway_url=GATEWAY + "/")["gateway"]
    assert "//" not in gw["targetUrlTemplate"].split("://", 1)[1]


# ---------------------------------------------------------------------------
# Header allowlist
# ---------------------------------------------------------------------------

def test_authorization_is_first_in_the_allowlist_and_explained():
    """It is both the credential and the source of the grant claim, and AgentCore
    drops an unlisted header silently — the request just looks like one that chose
    not to send it."""
    inv = _build()["invocation"]
    assert inv["requestHeaderAllowlist"][0] == "Authorization"
    assert "DROPS" in inv["headerAllowlistNote"]

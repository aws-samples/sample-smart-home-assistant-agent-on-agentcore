"""Offline pre-flight: an agent team's own check, against the published manifest.

Three properties matter more than the individual findings, and they are what most of
this file protects:

1. **It agrees with the approval gate.** A card+authorizer pair the pre-flight blesses
   must pass `a2a_conformance.check`, and one it blocks must not. Otherwise a team ships
   something we told them was fine and the gate refuses it — worse than having no
   pre-flight, because it burns the tool's credibility.
2. **It is standalone.** Stdlib only, rules read out of the MANIFEST. A checker that
   imported our enforcement modules would be a checker only we can run, which puts us
   back in the agent team's debugging loop.
3. **A rule the manifest does not publish is a BLOCK, not a default.** Silently falling
   back to a hardcoded pattern is how a published contract stops being the contract:
   the check keeps passing against a rule the platform no longer applies.
"""
import copy
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import a2a_conformance as conf  # noqa: E402
import a2a_manifest  # noqa: E402
import a2a_preflight as pf  # noqa: E402

SHARED = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POOL = "us-west-2_HwYYt6qLz"
CLIENT = "7031k7ctqes8mfe5jrf7kljttf"
DISCOVERY = (f"https://cognito-idp.us-west-2.amazonaws.com/{POOL}"
             "/.well-known/openid-configuration")
GW = ("https://smarthome-a2a-gw-ab12.gateway.bedrock-agentcore."
      "us-west-2.amazonaws.com")
ARN = "arn:aws:bedrock-agentcore:us-west-2:123:runtime/sha2atp-zzz"
DIRECT = ("https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
          + ARN.replace(":", "%3A").replace("/", "%2F") + "/invocations")


@pytest.fixture
def manifest():
    return a2a_manifest.build(
        region="us-west-2", registry_id="Zuy3YNKrPQ5uwE9t", user_pool_id=POOL,
        app_client_id=CLIENT, discovery_url=DISCOVERY, gateway_url=GW)


@pytest.fixture
def card():
    return {
        "name": "third-party-agent",
        "description": "A third-party specialist.",
        "version": "1.0.0",
        "url": DIRECT,
        "skills": [{"id": "do_a_thing"}, {"id": "do_another"}],
        "capabilities": {"streaming": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "securitySchemes": {"oauth2": {}},
    }


def _codes(findings):
    return {f["code"] for f in findings}


def _blocking_codes(findings):
    return {f["code"] for f in findings if f["severity"] in (pf.BLOCK, pf.RISK)}


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_a_good_card_has_nothing_blocking(card, manifest):
    findings = pf.check(card, manifest)
    assert not pf.is_blocking(findings), findings
    # A `note` about registering the runtime URL directly is expected and fine.
    assert _codes(findings) <= {"url-direct-runtime"}


def test_the_manifests_own_authorizer_template_passes(card, manifest):
    """The strongest single statement: follow the published instructions exactly and the
    pre-flight is clean."""
    authorizer = pf.expected_authorizer(card, manifest)
    findings = pf.check(card, manifest, authorizer)
    assert not pf.is_blocking(findings), findings


def test_the_nesting_of_the_authorizer_argument_does_not_matter(card, manifest):
    """`{"customJWTAuthorizer": {...}}` or the inner object. Getting that wrong is a
    copy-paste slip, not a defect worth a finding."""
    outer = pf.expected_authorizer(card, manifest)
    inner = outer["customJWTAuthorizer"]
    assert pf.check_authorizer(card, outer, manifest) == \
        pf.check_authorizer(card, inner, manifest) == []


# ---------------------------------------------------------------------------
# Property 1: it agrees with the approval gate
# ---------------------------------------------------------------------------

def _conf_shaped(authorizer):
    """The same authorizer as GetAgentRuntime would return it."""
    return (authorizer if "customJWTAuthorizer" in authorizer
            else {"customJWTAuthorizer": authorizer})


@pytest.mark.parametrize("mutate,label", [
    (lambda a: a, "the published template"),
    (lambda a: _drop(a, "customClaims"), "no customClaims"),
    (lambda a: _set(a, "discoveryUrl", "https://example.invalid/oidc"), "wrong pool"),
    (lambda a: _set(a, "allowedAudience", ["someone-else"]), "wrong audience"),
    (lambda a: _match(a, ["a2a-third-party-agent.do_a_thing"]), "skill groups only"),
    (lambda a: _match(a, ["a2a-home-security-agent"]), "a foreign agent's group"),
    (lambda a: _match(a, []), "an empty match list"),
])
def test_preflight_and_the_approval_gate_reach_the_same_verdict(
        card, manifest, mutate, label):
    """Pinned both ways. A disagreement means we bless a config the gate refuses, or
    scare a team off one it would accept."""
    authorizer = mutate(copy.deepcopy(pf.expected_authorizer(card, manifest)))

    pre_blocking = pf.is_blocking(pf.check_authorizer(card, authorizer, manifest))
    gate_findings = conf.check(card, _conf_shaped(authorizer), DISCOVERY, CLIENT)
    gate_blocking = conf.worst_severity(gate_findings) in (conf.OPEN, conf.CLOSED)

    assert pre_blocking == gate_blocking, (
        f"{label}: pre-flight blocking={pre_blocking} but the gate "
        f"blocking={gate_blocking} ({[f['code'] for f in gate_findings]})")


def _inner(a):
    return a["customJWTAuthorizer"] if "customJWTAuthorizer" in a else a


def _drop(a, key):
    _inner(a).pop(key, None)
    return a


def _set(a, key, value):
    _inner(a)[key] = value
    return a


def _match(a, groups):
    (_inner(a)["customClaims"][0]["authorizingClaimMatchValue"]
     ["claimMatchValue"]["matchValueStringList"]) = groups
    return a


# ---------------------------------------------------------------------------
# Property 2: standalone
# ---------------------------------------------------------------------------

def test_it_imports_with_nothing_but_its_own_directory_and_the_stdlib():
    """An agent team copies this file into their own CI. If it needed `a2a_groups` or
    `a2a_conformance`, it would be a checker only we could run."""
    probe = (
        "import sys, json, pathlib\n"
        "sys.path = [p for p in sys.path if 'smarthome' not in p]\n"
        f"sys.path.insert(0, {os.path.join(SHARED)!r})\n"
        "for banned in ('a2a_groups', 'a2a_conformance', 'a2a_manifest', 'boto3'):\n"
        "    sys.modules[banned] = None\n"
        "import a2a_preflight\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", probe],
                            capture_output=True, text=True)
    assert result.returncode == 0, (
        "a2a_preflight pulled in a module an agent team will not have:\n"
        + (result.stderr or result.stdout))


def test_the_cli_runs_from_two_files_side_by_side(tmp_path, card, manifest):
    """How an agent team will actually deploy it: copy the script and the rule module
    into their own CI, side by side. Resolving the rule only at `../shared/` would make
    this the one arrangement that fails."""
    repo = os.path.dirname(SHARED)
    for name, src in (("a2a-preflight.py", os.path.join(repo, "scripts",
                                                        "a2a-preflight.py")),
                      ("a2a_preflight.py", os.path.join(SHARED, "a2a_preflight.py"))):
        (tmp_path / name).write_bytes(open(src, "rb").read())
    (tmp_path / "card.json").write_text(json.dumps(card))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "authorizer.json").write_text(
        json.dumps(pf.expected_authorizer(card, manifest)))

    result = subprocess.run(
        [sys.executable, "a2a-preflight.py", "--card", "card.json",
         "--manifest", "manifest.json", "--authorizer", "authorizer.json"],
        cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK to deploy" in result.stdout


def test_the_cli_exits_nonzero_on_a_blocking_finding(tmp_path, card, manifest):
    """A pre-flight nobody's CI can gate on is a pre-flight nobody runs."""
    repo = os.path.dirname(SHARED)
    (tmp_path / "a2a-preflight.py").write_bytes(
        open(os.path.join(repo, "scripts", "a2a-preflight.py"), "rb").read())
    (tmp_path / "a2a_preflight.py").write_bytes(
        open(os.path.join(SHARED, "a2a_preflight.py"), "rb").read())
    card["name"] = "not a valid name"
    (tmp_path / "card.json").write_text(json.dumps(card))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))

    result = subprocess.run(
        [sys.executable, "a2a-preflight.py", "--card", "card.json",
         "--manifest", "manifest.json"],
        cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1
    assert "BLOCKED" in result.stdout


def test_the_module_declares_no_third_party_import():
    """Belt and braces on the subprocess check, and it reads at review time."""
    source = open(os.path.join(SHARED, "a2a_preflight.py"), encoding="utf-8").read()
    for banned in ("import boto3", "import a2a_groups", "import a2a_conformance",
                   "import a2a_manifest", "import requests"):
        assert banned not in source, f"a2a_preflight must not {banned}"


# ---------------------------------------------------------------------------
# Property 3: an unpublished rule blocks, it does not default
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "card.requiredFields", "card.namePattern", "card.skillIdPattern",
    "groups.doorGroup", "groups.skillGroup",
])
def test_a_missing_manifest_rule_is_reported_not_defaulted(card, manifest, path):
    section, key = path.split(".")
    manifest[section].pop(key)
    findings = pf.check(card, manifest)
    assert "manifest-incomplete" in _blocking_codes(findings)
    assert any(path in f["detail"] for f in findings)


def test_an_unknown_manifest_major_refuses_to_judge(card, manifest):
    """A major bump means a rule changed. Reporting a confident verdict against rules we
    no longer understand is worse than reporting none."""
    manifest["manifestVersion"] = "2.0"
    findings = pf.check(card, manifest)
    assert _codes(findings) == {"manifest-version-unknown"}


def test_a_manifest_with_no_version_is_refused(card, manifest):
    del manifest["manifestVersion"]
    assert _codes(pf.check(card, manifest)) == {"manifest-no-version"}


# ---------------------------------------------------------------------------
# Card findings — each one is a failure that otherwise surfaces misleadingly
# ---------------------------------------------------------------------------

def test_an_incomplete_card_names_the_missing_fields(card, manifest):
    """The service says "does not match any supported version", which sends people
    looking for a version problem."""
    del card["capabilities"], card["securitySchemes"]
    findings = pf.check(card, manifest)
    assert "card-incomplete" in _blocking_codes(findings)
    detail = next(f["detail"] for f in findings if f["code"] == "card-incomplete")
    assert "capabilities" in detail and "securitySchemes" in detail


def test_a_card_name_outside_the_pattern_blocks(card, manifest):
    card["name"] = "third party agent"
    assert "card-name-unencodable" in _blocking_codes(pf.check(card, manifest))


def test_a_dotted_card_name_blocks_because_the_dot_is_the_separator(card, manifest):
    card["name"] = "third.party.agent"
    assert "card-name-unencodable" in _blocking_codes(pf.check(card, manifest))


def test_a_skill_id_outside_the_pattern_blocks(card, manifest):
    card["skills"] = [{"id": "do a thing"}]
    assert "skill-id-unencodable" in _blocking_codes(pf.check(card, manifest))


def test_a_skill_with_no_id_blocks(card, manifest):
    card["skills"] = [{"name": "Nameless"}]
    assert "skill-no-id" in _blocking_codes(pf.check(card, manifest))


def test_a_duplicate_skill_id_blocks(card, manifest):
    card["skills"] = [{"id": "same"}, {"id": "same"}]
    assert "skill-duplicate-id" in _blocking_codes(pf.check(card, manifest))


def test_no_skills_blocks(card, manifest):
    card["skills"] = []
    assert "card-no-skills" in _blocking_codes(pf.check(card, manifest))


def test_a_skill_that_overflows_the_group_name_blocks(card, manifest):
    """The card is otherwise perfectly valid; the failure only ever surfaced when
    somebody tried to GRANT that skill."""
    card["name"] = "a" * 60
    card["skills"] = [{"id": "b" * 70}]
    findings = pf.check(card, manifest)
    assert "group-name-too-long" in _blocking_codes(findings)
    assert "cannot be granted" in next(
        f["detail"] for f in findings if f["code"] == "group-name-too-long")


# ---------------------------------------------------------------------------
# The url
# ---------------------------------------------------------------------------

def test_a_gateway_target_url_is_recognised(card, manifest):
    card["url"] = f"{GW}/third-party-agent"
    findings = pf.check(card, manifest)
    assert not pf.is_blocking(findings)
    assert "url-uses-gateway" in _codes(findings)


def test_a_bare_gateway_host_blocks(card, manifest):
    card["url"] = f"{GW}/"
    assert "url-gateway-no-target" in _blocking_codes(pf.check(card, manifest))


def test_a_deep_gateway_path_blocks(card, manifest):
    card["url"] = f"{GW}/team/agent"
    assert "url-gateway-deep-path" in _blocking_codes(pf.check(card, manifest))


def test_an_arbitrary_url_blocks(card, manifest):
    card["url"] = "https://example.com/my-agent"
    findings = pf.check(card, manifest)
    assert "url-unresolvable" in _blocking_codes(findings)
    # Says WHY it matters, not just that it is wrong.
    assert "read its authorizer" in next(
        f["detail"] for f in findings if f["code"] == "url-unresolvable")


# ---------------------------------------------------------------------------
# The authorizer's classic mistakes
# ---------------------------------------------------------------------------

def test_allowedClients_gets_its_own_finding(card, manifest):
    """The single most common silent failure. Folding it into a generic audience
    complaint would hide the one sentence that explains it."""
    authorizer = pf.expected_authorizer(card, manifest)
    inner = authorizer["customJWTAuthorizer"]
    inner["allowedClients"] = inner.pop("allowedAudience")
    findings = pf.check_authorizer(card, authorizer, manifest)
    assert "uses-allowedClients" in _blocking_codes(findings)
    assert "client_id" in next(
        f["detail"] for f in findings if f["code"] == "uses-allowedClients")


def test_no_customClaims_is_a_RISK_because_the_symptom_is_that_it_works(
        card, manifest):
    authorizer = _drop(pf.expected_authorizer(card, manifest), "customClaims")
    findings = pf.check_authorizer(card, authorizer, manifest)
    assert _codes(findings) == {"no-claim-check"}
    assert pf.worst_severity(findings) == pf.RISK
    assert pf.is_blocking(findings), "a wide-open agent must stop a deploy"


def test_a_pre_migration_authorizer_is_only_a_note(card, manifest):
    """It works today; it is merely still coupled. Blocking it would refuse every
    runtime not yet migrated, and a checker that fails on working configs gets skipped."""
    authorizer = _match(pf.expected_authorizer(card, manifest),
                        ["a2a-third-party-agent.do_a_thing",
                         "a2a-third-party-agent.do_another"])
    findings = pf.check_authorizer(card, authorizer, manifest)
    assert _codes(findings) == {"claim-skill-groups-only"}
    assert not pf.is_blocking(findings)


def test_a_foreign_agents_group_is_a_RISK(card, manifest):
    authorizer = _match(pf.expected_authorizer(card, manifest),
                        ["a2a-third-party-agent", "a2a-home-security-agent.arm"])
    findings = pf.check_authorizer(card, authorizer, manifest)
    assert "claim-extra-groups" in _blocking_codes(findings)


def test_this_cards_own_skill_groups_alongside_the_door_are_fine(card, manifest):
    """The state a cautious operator lands in mid-migration."""
    authorizer = _match(pf.expected_authorizer(card, manifest),
                        ["a2a-third-party-agent",
                         "a2a-third-party-agent.do_a_thing"])
    assert pf.check_authorizer(card, authorizer, manifest) == []


def test_naming_nothing_of_this_card_blocks(card, manifest):
    authorizer = _match(pf.expected_authorizer(card, manifest), ["a2a-someone-else"])
    findings = pf.check_authorizer(card, authorizer, manifest)
    assert "claim-missing-groups" in _blocking_codes(findings)


def test_expected_authorizer_raises_rather_than_guessing(card):
    """A caller asking for the right answer needs to know it is unavailable."""
    with pytest.raises(KeyError):
        pf.expected_authorizer(card, {"manifestVersion": "1.0"})


# ---------------------------------------------------------------------------
# Severity plumbing
# ---------------------------------------------------------------------------

def test_block_outranks_risk_outranks_note():
    findings = [pf._finding("a", pf.NOTE, ""), pf._finding("b", pf.RISK, ""),
                pf._finding("c", pf.BLOCK, "")]
    assert pf.worst_severity(findings) == pf.BLOCK
    assert pf.worst_severity(findings[:2]) == pf.RISK
    assert pf.worst_severity(findings[:1]) == pf.NOTE
    assert pf.worst_severity([]) == ""


def test_notes_alone_do_not_block():
    assert not pf.is_blocking([pf._finding("a", pf.NOTE, "")])

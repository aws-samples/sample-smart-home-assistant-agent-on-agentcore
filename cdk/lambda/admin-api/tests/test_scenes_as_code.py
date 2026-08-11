"""Tests for scene export/import (spec 5 S6).

For the developer-shaped end users this product has: paste a scene definition
instead of describing it across a dozen turns, keep it in version control, move it
between accounts.

The design decision worth protecting is that import goes through
`scenarios.build_scenario` — the SAME validator the A2A agent uses. A separate
import validator would be a second definition of "valid" and it would drift, which
in practice means storing a device action the execution path then refuses. The
symptom of that is an automation that saves fine and silently does nothing at 07:30.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeTable:
    """Minimal DynamoDB Table double: query by userId, and record put_item."""

    def __init__(self, items=None, fail_put=False):
        self.items = list(items or [])
        self.puts = []
        self.fail_put = fail_put

    def query(self, KeyConditionExpression=None, **kw):  # noqa: N803
        # The condition is `Key("userId").eq(x)`; read the value off it rather
        # than re-implementing DynamoDB's expression language.
        wanted = KeyConditionExpression._values[1]
        return {"Items": [i for i in self.items if i.get("userId") == wanted]}

    def put_item(self, Item=None, **kw):  # noqa: N803
        if self.fail_put:
            raise RuntimeError("ProvisionedThroughputExceeded")
        self.puts.append(Item)
        return {}


@pytest.fixture
def index(monkeypatch):
    import index as mod
    # Scenes are keyed by Cognito sub, so both handlers resolve email -> sub. The
    # tests use email-shaped ids for readability, so the resolver is stubbed to a
    # sub that no fixture row uses — which also means the export tests below still
    # exercise the email key first, and the round-trip test proves the sub is what
    # gets WRITTEN.
    monkeypatch.setattr(mod, "_resolve_sub_for_email", lambda e: "sub-" + e)
    return mod


def _event(params=None, body=None):
    return {
        "queryStringParameters": params or {},
        "body": json.dumps(body) if body is not None else None,
    }


def _body(resp):
    return json.loads(resp["body"])


SCENE_ROW = {
    "userId": "u@e.com",
    "scenarioKey": "strategy#movie-mode",
    "scenarioId": "movie-mode",
    "name": "Movie mode",
    "description": "Dim for a film",
    "trigger": {"sceneType": "manual", "calculationType": "equal",
               "executionType": "recurring"},
    "deviceActions": [{"deviceId": "living-strip-1", "command":
                       {"action": "setBrightness", "brightness": 20}}],
    "isActive": True,
    "isTemplate": False,
    "createdAt": "2026-08-01T00:00:00+00:00",
    "lastRunAt": "2026-08-10T07:30:00+00:00",
    "lastRunOk": True,
}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def test_export_requires_a_user(index, monkeypatch):
    """No implicit "everyone".

    A scene names device ids and daily routines. An export that defaulted to the
    whole fleet would be a data-disclosure bug dressed as a convenience.
    """
    resp = index.export_scenarios(_event({}))
    assert resp["statusCode"] == 400
    assert "userId" in _body(resp)["error"]


def test_export_returns_only_the_scene_definition(index, monkeypatch):
    monkeypatch.setattr(index, "_scenarios_table",
                        lambda: _FakeTable([SCENE_ROW]))
    scenes = _body(index.export_scenarios(_event({"userId": "u@e.com"})))["scenes"]
    assert len(scenes) == 1
    assert set(scenes[0]) == {"name", "description", "trigger", "deviceActions",
                              "isActive", "isTemplate"}
    # Runtime state must NOT travel: a round trip that appeared to restore a run
    # history it cannot would be worse than omitting it.
    assert "lastRunAt" not in scenes[0]
    assert "lastRunOk" not in scenes[0]
    assert "createdAt" not in scenes[0]
    # Nor derived/owner fields, which the import recomputes from the target user.
    assert "userId" not in scenes[0]
    assert "scenarioKey" not in scenes[0]


def test_export_skips_templates(index, monkeypatch):
    """Templates are library content, not this user's automations."""
    template = dict(SCENE_ROW, scenarioKey="template#cosy-evening", isTemplate=True)
    monkeypatch.setattr(index, "_scenarios_table",
                        lambda: _FakeTable([SCENE_ROW, template]))
    scenes = _body(index.export_scenarios(_event({"userId": "u@e.com"})))["scenes"]
    assert [s["name"] for s in scenes] == ["Movie mode"]


def test_export_only_returns_the_named_users_scenes(index, monkeypatch):
    other = dict(SCENE_ROW, userId="someone@else.com", name="Their scene")
    monkeypatch.setattr(index, "_scenarios_table",
                        lambda: _FakeTable([SCENE_ROW, other]))
    scenes = _body(index.export_scenarios(_event({"userId": "u@e.com"})))["scenes"]
    assert [s["name"] for s in scenes] == ["Movie mode"]


def test_export_url_decodes_the_user_id(index, monkeypatch):
    """`admin%40smarthome.local` must read the row for `admin@smarthome.local`.

    The same omission wrote a settings row under the encoded key once already; the
    symptom is an empty export for a user who plainly has scenes.
    """
    monkeypatch.setattr(index, "_scenarios_table",
                        lambda: _FakeTable([SCENE_ROW]))
    scenes = _body(index.export_scenarios(
        _event({"userId": "u%40e.com"})))["scenes"]
    assert len(scenes) == 1


def test_export_converts_decimals_to_json_numbers(index, monkeypatch):
    """`default=str` would turn brightness 20 into the string "20".

    That then fails validation on the way back in, on an export the user never
    edited — a round trip that breaks itself.
    """
    from decimal import Decimal

    row = dict(SCENE_ROW, deviceActions=[{"deviceId": "living-strip-1",
               "command": {"action": "setBrightness",
                           "brightness": Decimal("20")}}])
    monkeypatch.setattr(index, "_scenarios_table", lambda: _FakeTable([row]))
    scenes = _body(index.export_scenarios(_event({"userId": "u@e.com"})))["scenes"]
    assert scenes[0]["deviceActions"][0]["command"]["brightness"] == 20
    assert isinstance(scenes[0]["deviceActions"][0]["command"]["brightness"], int)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

VALID_SCENE = {
    "name": "Imported cosy",
    "description": "from JSON",
    "trigger": {"sceneType": "time", "conditionValue": "22:30"},
    "deviceActions": [{"deviceId": "living-strip-1",
                       "command": {"action": "setBrightness", "brightness": 30}}],
}


def test_import_requires_a_user_and_a_non_empty_list(index):
    assert index.import_scenarios(_event(body={"scenes": [VALID_SCENE]})
                                  )["statusCode"] == 400
    assert index.import_scenarios(_event(body={"userId": "u@e.com", "scenes": []})
                                  )["statusCode"] == 400
    assert index.import_scenarios(_event(body={"userId": "u@e.com"})
                                  )["statusCode"] == 400


def test_import_creates_a_scene(index, monkeypatch):
    fake = _FakeTable()
    monkeypatch.setattr(index, "_scenarios_table", lambda: fake)
    out = _body(index.import_scenarios(
        _event(body={"userId": "u@e.com", "scenes": [VALID_SCENE]})))
    assert out["createdCount"] == 1
    assert out["failedCount"] == 0
    assert fake.puts[0]["userId"] == "sub-u@e.com"
    assert fake.puts[0]["name"] == "Imported cosy"
    # Marked so the Scenarios page can tell an imported scene from a spoken one.
    assert fake.puts[0]["source"] == "import"


def test_import_validates_through_the_shared_validator(index, monkeypatch):
    """A bad trigger must be refused, by the same code the agent uses.

    Asserted on the message rather than just the count: the point is that the
    error comes from `scenarios.validate_trigger`, not from a second
    implementation that might accept what the runner would later refuse.
    """
    monkeypatch.setattr(index, "_scenarios_table", lambda: _FakeTable())
    bad = dict(VALID_SCENE, trigger={"sceneType": "when_i_feel_like_it"})
    out = _body(index.import_scenarios(
        _event(body={"userId": "u@e.com", "scenes": [bad]})))
    assert out["createdCount"] == 0
    assert out["failedCount"] == 1
    assert out["failed"][0]["index"] == 0
    assert out["failed"][0]["error"]


def test_one_bad_scene_does_not_cost_the_good_ones(index, monkeypatch):
    """Per-scene outcomes, not all-or-nothing.

    A document with one broken entry should not lose the user their other scenes,
    and naming which entry failed is more useful than a single 400.
    """
    fake = _FakeTable()
    monkeypatch.setattr(index, "_scenarios_table", lambda: fake)
    bad = dict(VALID_SCENE, name="")  # a nameless scene is unstorable
    out = _body(index.import_scenarios(_event(body={
        "userId": "u@e.com", "scenes": [VALID_SCENE, bad]})))
    assert out["createdCount"] == 1
    assert out["failedCount"] == 1
    assert len(fake.puts) == 1


def test_import_cannot_mint_a_template(index, monkeypatch):
    """Templates are catalog content; a user import must not create one.

    Otherwise an imported document could plant a scene into every user's template
    library via the GSI.
    """
    fake = _FakeTable()
    monkeypatch.setattr(index, "_scenarios_table", lambda: fake)
    index.import_scenarios(_event(body={
        "userId": "u@e.com", "scenes": [dict(VALID_SCENE, isTemplate=True)]}))
    assert fake.puts[0]["isTemplate"] is False
    assert fake.puts[0]["scenarioKey"].startswith("strategy#")
    assert "templateScope" not in fake.puts[0]


def test_import_caps_the_batch(index, monkeypatch):
    monkeypatch.setattr(index, "_scenarios_table", lambda: _FakeTable())
    resp = index.import_scenarios(_event(body={
        "userId": "u@e.com", "scenes": [VALID_SCENE] * 51}))
    assert resp["statusCode"] == 400
    assert "50" in _body(resp)["error"]


def test_import_converts_floats_for_dynamodb(index, monkeypatch):
    """DynamoDB rejects Python floats outright.

    A JSON document carrying 21.5 would fail `put_item` with "Float types are not
    supported" — on valid input, so the user would see a storage error for a scene
    that is entirely correct.
    """
    from decimal import Decimal

    fake = _FakeTable()
    monkeypatch.setattr(index, "_scenarios_table", lambda: fake)
    scene = dict(VALID_SCENE, trigger={
        "sceneType": "sensor", "subject": "temperature",
        "calculationType": "above", "conditionValue": 21.5})
    out = _body(index.import_scenarios(
        _event(body={"userId": "u@e.com", "scenes": [scene]})))
    assert out["createdCount"] == 1, out
    assert isinstance(fake.puts[0]["trigger"]["conditionValue"], Decimal)


def test_a_put_failure_is_reported_per_scene(index, monkeypatch):
    monkeypatch.setattr(index, "_scenarios_table",
                        lambda: _FakeTable(fail_put=True))
    out = _body(index.import_scenarios(
        _event(body={"userId": "u@e.com", "scenes": [VALID_SCENE]})))
    assert out["failedCount"] == 1
    assert "Throughput" in out["failed"][0]["error"]


def test_import_does_not_create_schedules_and_says_so(index, monkeypatch):
    """Parsing a document must not start firing automations.

    Reconciling is a separate, explicit action — so the response says the scenes
    are stored but not scheduled, rather than leaving the operator to wonder why
    nothing fired.
    """
    monkeypatch.setattr(index, "_scenarios_table", lambda: _FakeTable())
    out = _body(index.import_scenarios(
        _event(body={"userId": "u@e.com", "scenes": [VALID_SCENE]})))
    assert "not scheduled" in out["note"]
    assert "Reconcile" in out["note"] or "sync-schedules" in out["note"]


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_export_then_import_preserves_the_scene(index, monkeypatch):
    """The property that makes this "as code" rather than "a dump".

    Exercised end to end because each half can be individually correct while the
    pair is not — a field the export omits or the import ignores shows up only here.
    """
    monkeypatch.setattr(index, "_scenarios_table",
                        lambda: _FakeTable([SCENE_ROW]))
    exported = _body(index.export_scenarios(_event({"userId": "u@e.com"})))

    fake = _FakeTable()
    monkeypatch.setattr(index, "_scenarios_table", lambda: fake)
    out = _body(index.import_scenarios(_event(body={
        "userId": "other@e.com", "scenes": exported["scenes"]})))

    assert out["createdCount"] == 1, out
    stored = fake.puts[0]
    assert stored["name"] == SCENE_ROW["name"]
    assert stored["description"] == SCENE_ROW["description"]
    assert stored["trigger"]["sceneType"] == "manual"
    assert stored["deviceActions"][0]["deviceId"] == "living-strip-1"
    assert stored["deviceActions"][0]["command"]["brightness"] == 20
    # Re-homed to the importing user, not the exporting one.
    # Written under the SUB, because that is the key the scenario runner reads. An
    # import keyed by email would show on the page and never fire.
    assert stored["userId"] == "sub-other@e.com"


# ---------------------------------------------------------------------------
# The two key spaces
#
# Scenes are the one table keyed by Cognito **sub**. Everything else the admin API
# touches — settings, prompts, A2A grants — is keyed by EMAIL, and the Scenarios
# page passes whichever it has. Getting this wrong is silent in both directions:
# an export returns nothing for a user who plainly has scenes, and an import
# stores rows the scenario runner never reads, so the scene shows on the page and
# never fires.
# ---------------------------------------------------------------------------

def test_export_falls_back_to_the_sub_when_the_email_has_no_rows(index, monkeypatch):
    sub_row = dict(SCENE_ROW, userId="88c1a3e0-b041", name="Stored by sub")
    monkeypatch.setattr(index, "_scenarios_table", lambda: _FakeTable([sub_row]))
    monkeypatch.setattr(index, "_resolve_sub_for_email", lambda e: "88c1a3e0-b041")
    scenes = _body(index.export_scenarios(_event({"userId": "u@e.com"})))["scenes"]
    assert [s["name"] for s in scenes] == ["Stored by sub"]


def test_export_prefers_the_literal_key_when_it_has_rows(index, monkeypatch):
    """A caller that already passed a sub must not be second-guessed."""
    monkeypatch.setattr(index, "_scenarios_table", lambda: _FakeTable([SCENE_ROW]))
    called = []
    monkeypatch.setattr(index, "_resolve_sub_for_email",
                        lambda e: called.append(e) or "other-sub")
    scenes = _body(index.export_scenarios(
        _event({"userId": "u@e.com"})))["scenes"]
    assert [s["name"] for s in scenes] == ["Movie mode"]


def test_export_of_a_sub_does_not_call_cognito(index, monkeypatch):
    # Nothing to resolve, and a needless ListUsers on every export is a rate limit
    # waiting to be hit.
    monkeypatch.setattr(index, "_scenarios_table", lambda: _FakeTable([]))

    def _boom(email):
        raise AssertionError("must not resolve a value that is already a sub")

    monkeypatch.setattr(index, "_resolve_sub_for_email", _boom)
    assert index.export_scenarios(
        _event({"userId": "88c1a3e0-b041"}))["statusCode"] == 200


def test_import_refuses_when_the_sub_cannot_be_resolved(index, monkeypatch):
    """Better a clear 400 than rows the runner will never read.

    Storing under the email would "succeed": the scene appears on the Scenarios
    page and silently never fires, which is far harder to diagnose than a refusal
    naming the reason.
    """
    monkeypatch.setattr(index, "_scenarios_table", lambda: _FakeTable())
    monkeypatch.setattr(index, "_resolve_sub_for_email", lambda e: "")
    resp = index.import_scenarios(_event(body={
        "userId": "u@e.com", "scenes": [VALID_SCENE]}))
    assert resp["statusCode"] == 400
    assert "sub" in _body(resp)["error"]


def test_import_of_a_sub_writes_it_unchanged(index, monkeypatch):
    fake = _FakeTable()
    monkeypatch.setattr(index, "_scenarios_table", lambda: fake)
    out = _body(index.import_scenarios(_event(body={
        "userId": "88c1a3e0-b041", "scenes": [VALID_SCENE]})))
    assert out["createdCount"] == 1
    assert fake.puts[0]["userId"] == "88c1a3e0-b041"

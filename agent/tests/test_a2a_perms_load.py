"""Tests for agent.load_user_a2a_permissions.

The project has both ``agent/__init__.py`` (package) and ``agent/agent.py``
(script). Loading the script file directly via importlib avoids the ambiguity
that ``import agent`` produces.
"""
import importlib.util
import os
from unittest.mock import MagicMock

import pytest


_AGENT_SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "agent.py")
)


@pytest.fixture
def agent_mod():
    spec = importlib.util.spec_from_file_location(
        "_agent_script_under_test", _AGENT_SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("SKILLS_TABLE_NAME", "smarthome-skills-test")
    monkeypatch.setenv("MODEL_ID", "us.amazon.nova-lite-v1:0")


def _install_table(monkeypatch, agent_mod, get_item_side_effect):
    tbl = MagicMock()
    tbl.get_item.side_effect = get_item_side_effect
    res = MagicMock()
    res.Table.return_value = tbl
    monkeypatch.setattr(agent_mod, "_dynamodb_resource", res, raising=False)
    monkeypatch.setattr(agent_mod, "_get_dynamodb", lambda: res)
    return tbl


def test_empty_when_no_rows(agent_mod, monkeypatch):
    _install_table(monkeypatch, agent_mod, lambda **kw: {"Item": None})
    assert agent_mod.load_user_a2a_permissions("alice") == {}


def test_user_overrides_global(agent_mod, monkeypatch):
    items = {
        ("__global__", "__a2a_permissions__"): {
            "Item": {"userId": "__global__", "skillName": "__a2a_permissions__",
                     "a2aGrants": {"rec-energy": ["estimate_savings"]}}
        },
        ("alice", "__a2a_permissions__"): {
            "Item": {"userId": "alice", "skillName": "__a2a_permissions__",
                     "a2aGrants": {"rec-energy": ["tariff_analysis"],
                                    "rec-security": ["risk_assessment"]}}
        },
    }

    def _fake_get(**kw):
        return items.get((kw["Key"]["userId"], kw["Key"]["skillName"]), {})

    _install_table(monkeypatch, agent_mod, _fake_get)

    merged = agent_mod.load_user_a2a_permissions("alice")
    assert merged == {
        "rec-energy": ["tariff_analysis"],
        "rec-security": ["risk_assessment"],
    }


def test_normalises_set_values(agent_mod, monkeypatch):
    def _fake_get(**kw):
        if kw["Key"]["userId"] == "__global__":
            return {"Item": {
                "userId": "__global__", "skillName": "__a2a_permissions__",
                "a2aGrants": {"rec-energy": {"tariff_analysis", "estimate_savings"}},
            }}
        return {"Item": None}

    _install_table(monkeypatch, agent_mod, _fake_get)
    merged = agent_mod.load_user_a2a_permissions("alice")
    assert merged["rec-energy"] == ["estimate_savings", "tariff_analysis"]


def test_global_only_when_actor_is_global(agent_mod, monkeypatch):
    called = {"n": 0}

    def _fake_get(**kw):
        called["n"] += 1
        if kw["Key"]["userId"] == "__global__":
            return {"Item": {"userId": "__global__", "skillName": "__a2a_permissions__",
                             "a2aGrants": {"rec-security": ["risk_assessment"]}}}
        return {"Item": None}

    _install_table(monkeypatch, agent_mod, _fake_get)
    merged = agent_mod.load_user_a2a_permissions("__global__")
    assert merged == {"rec-security": ["risk_assessment"]}
    assert called["n"] == 1


def test_tolerates_dynamodb_error(agent_mod, monkeypatch):
    def _fake_get(**kw):
        if kw["Key"]["userId"] == "__global__":
            return {"Item": {"userId": "__global__", "skillName": "__a2a_permissions__",
                             "a2aGrants": {"rec-energy": ["estimate_savings"]}}}
        raise RuntimeError("transient")

    _install_table(monkeypatch, agent_mod, _fake_get)
    merged = agent_mod.load_user_a2a_permissions("alice")
    assert merged == {"rec-energy": ["estimate_savings"]}

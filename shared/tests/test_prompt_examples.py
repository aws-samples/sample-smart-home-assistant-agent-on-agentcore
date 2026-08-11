"""Tests for the shared prompt-example library.

The library exists because the chatbot's suggestion chips and the simulated
users' conversation scripts are the same list — "what can this system do" —
and they were maintained separately, in TypeScript and in Python. That
duplication does not fail loudly. The symptom is discovering mid-demo that
nothing exercises the security agent, with no error anywhere.

So the load-bearing test here is coverage: every skill published by every
deployed A2A agent must be named by some example's `covers`. Adding a skill
without an example fails this file.

The reverse direction matters just as much. A `covers` entry pointing at a
skill that no longer exists is invisible — the example still renders, still
runs, and simply exercises the orchestrator's fallback instead of the
specialist it claims to demo.
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
EXAMPLES_PATH = os.path.join(REPO, "shared", "prompt-examples.json")
REGISTRY_DIR = os.path.join(REPO, "a2a-agent-registry")

VALID_TIERS = {"light", "heavy"}


def _load():
    with open(EXAMPLES_PATH, encoding="utf-8") as f:
        return json.load(f)


def _deployed_skills():
    """Every `<agentDir>.<skillId>` from the AgentCards on disk.

    Read from card.json rather than deployed-state.json: the card is what the
    running server publishes, and it is what an author can check against.
    """
    found = set()
    for entry in sorted(os.listdir(REGISTRY_DIR)):
        card = os.path.join(REGISTRY_DIR, entry, "card.json")
        if not os.path.isfile(card):
            continue
        with open(card, encoding="utf-8") as f:
            data = json.load(f)
        for skill in data.get("skills", []):
            found.add(f"{entry}.{skill['id']}")
    return found


def _all_examples(data):
    for group in data["groups"]:
        for ex in group["examples"]:
            yield group, ex


# ---------------------------------------------------------------------------
# Coverage — the reason this file exists
# ---------------------------------------------------------------------------

def test_every_deployed_skill_is_covered_by_an_example():
    """A new A2A skill with no example fails here.

    Without this, "the examples cover every feature" is a claim nobody
    re-checks after the first time it was true.
    """
    data = _load()
    covered = {c for g in data["groups"] for c in g["covers"]}
    deployed = _deployed_skills()
    missing = deployed - covered
    assert not missing, (
        "these deployed A2A skills have no example in shared/prompt-examples.json:\n  "
        + "\n  ".join(sorted(missing))
        + "\n\nAdd one to the relevant group's `examples` and list the skill in "
          "that group's `covers`."
    )


def test_covers_names_only_skills_that_exist():
    """A renamed or removed skill must not leave a stale `covers` entry.

    A stale entry is silent: the example still runs, the orchestrator falls
    back to its own knowledge, and the answer still looks fine.
    """
    data = _load()
    covered = {c for g in data["groups"] for c in g["covers"]}
    deployed = _deployed_skills()
    unknown = covered - deployed
    assert not unknown, (
        "`covers` names skills that no AgentCard publishes:\n  "
        + "\n  ".join(sorted(unknown))
        + f"\n\nDeployed skills are:\n  " + "\n  ".join(sorted(deployed))
    )


def test_all_eighteen_skills_are_accounted_for():
    """Guards the count itself, so a card that stops loading is noticed.

    If a card.json became unreadable, `_deployed_skills` would quietly return
    a smaller set and the coverage test above would still pass.
    """
    assert len(_deployed_skills()) == 18, sorted(_deployed_skills())


# ---------------------------------------------------------------------------
# Shape — both consumers assume these hold
# ---------------------------------------------------------------------------

def test_every_example_has_both_languages():
    """The chatbot renders whichever language is active; a missing one is blank."""
    data = _load()
    for group, ex in _all_examples(data):
        for key in ("zh", "en"):
            assert ex.get(key, "").strip(), f"{group['id']}: example missing {key}: {ex}"


def test_every_example_declares_a_valid_tier():
    """`run --heavy` filters on tier; an unknown value would silently drop it."""
    data = _load()
    for group, ex in _all_examples(data):
        assert ex.get("tier") in VALID_TIERS, f"{group['id']}: bad tier {ex.get('tier')!r}"


def test_every_group_has_an_id_and_both_titles():
    data = _load()
    for group in data["groups"]:
        assert re.fullmatch(r"[a-z][a-z0-9-]*", group.get("id", "")), group.get("id")
        for key in ("titleZh", "titleEn"):
            assert group.get(key, "").strip(), f"{group['id']}: missing {key}"


def test_group_ids_are_unique():
    """personas.py binds to a group by id; a duplicate would shadow the other."""
    ids = [g["id"] for g in _load()["groups"]]
    assert len(ids) == len(set(ids)), ids


def test_no_group_is_empty():
    for group in _load()["groups"]:
        assert group["examples"], f"{group['id']} has no examples"


def test_notes_come_in_pairs():
    """A note in one language only renders as a missing explanation, not an error."""
    for group, ex in _all_examples(_load()):
        has_zh, has_en = "noteZh" in ex, "noteEn" in ex
        assert has_zh == has_en, f"{group['id']}: note only in one language: {ex}"


def test_heavy_examples_are_only_browser_and_code():
    """The heavy tier means a real browser or code-interpreter session.

    Mislabelling a cheap example as heavy hides it from the default sim run;
    mislabelling an expensive one as light puts a 141s turn in the fast path.
    """
    heavy_groups = {g["id"] for g in _load()["groups"]
                    if any(e["tier"] == "heavy" for e in g["examples"])}
    assert heavy_groups == {"browser", "code"}, heavy_groups


def test_examples_are_requests_not_instructions_to_the_reader():
    """Each example must be something the user can send verbatim.

    A chip that stages "(attach an image first)" as the prompt sends that
    text to the agent. Parenthetical asides belong in `note`, not the prompt —
    with one deliberate exception, the vision example, which cannot be
    demonstrated without an attachment.
    """
    for group, ex in _all_examples(_load()):
        if group["id"] == "vision":
            continue
        for key in ("zh", "en"):
            assert "(" not in ex[key] and "（" not in ex[key], \
                f"{group['id']}: parenthetical in the prompt itself, move it to note: {ex[key]}"

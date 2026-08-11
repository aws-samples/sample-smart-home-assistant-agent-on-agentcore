"""Tests for the simulated satisfaction votes.

These exist because of a bug that reported complete success. The first run filed
29 real votes through the real API and produced a CSAT of exactly 5.0/5 with zero
negatives, because the up/down split used `i % 100` against a threshold of
`rate * 100` while a persona only has 6-8 turns — so `i` never reached the
threshold and every single vote was positive.

Nothing errored. The run printed "29 vote(s) filed, 0 failed", the dashboard card
rendered, and the number looked plausible. The only way to notice was to compare
the output against the per-persona rates that were supposed to produce it.

So the load-bearing assertion here is that the distribution actually VARIES, and
roughly matches each persona's declared rate.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, SCRIPTS)

from sim import personas as P  # noqa: E402


def votes_for(rate: float, n: int) -> list[str]:
    """Mirror of the split in simulate-users.py's `_vote_on_turns`.

    Bresenham-style: the negative "debt" accumulates a fraction per turn and is
    paid whenever it reaches a whole vote, which spreads negatives through the
    sequence instead of grouping them at the end of a fixed window.
    """
    out, acc = [], 0.0
    for _ in range(n):
        acc += 1.0 - rate
        if acc >= 1.0:
            out.append("down")
            acc -= 1.0
        else:
            out.append("up")
    return out


# ---------------------------------------------------------------------------
# The regression itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rate", [0.7, 0.75, 0.8])
def test_a_short_run_produces_at_least_one_negative(rate):
    """The exact bug: 8 turns at rate<1.0 must not be unanimously positive.

    With `i % 100` this returned all "up" for every rate below 1.0. With `i % 10`
    it still did for any persona with fewer than 8 turns, because the negative
    slots all sat at positions 7-9.
    """
    got = votes_for(rate, 8)
    assert "down" in got, f"rate={rate} over 8 turns produced no negatives: {got}"


@pytest.mark.parametrize("rate", [0.7, 0.75, 0.8])
def test_distribution_approaches_the_declared_rate_over_many_turns(rate):
    """Within one vote. A tighter bound would fail on float accumulation alone:
    0.8 over 100 turns lands on 81 up, not 80."""
    got = votes_for(rate, 100)
    assert abs(got.count("up") - rate * 100) <= 1, got.count("up")


def test_negatives_are_spread_not_clustered_at_the_end():
    """The `i % 10` failure mode: negatives only in the tail.

    With 20 turns at 0.75 the first half must carry some of them, otherwise a
    short run sees none.
    """
    got = votes_for(0.75, 20)
    assert "down" in got[:10], got


def test_rate_of_one_is_unanimously_positive():
    """A persona that is meant to be always happy still can be."""
    assert votes_for(1.0, 10).count("down") == 0


def test_rate_of_zero_is_unanimously_negative():
    assert votes_for(0.0, 10).count("up") == 0


def test_the_split_is_deterministic():
    """Two runs must agree, so a demo screenshot is reproducible."""
    assert votes_for(0.75, 20) == votes_for(0.75, 20)


# ---------------------------------------------------------------------------
# The personas that feed it
# ---------------------------------------------------------------------------

def test_personas_do_not_all_share_one_rate():
    """A card where every persona votes identically looks synthetic."""
    rates = {p.feedback_up_rate for p in P.PERSONAS}
    assert len(rates) > 1, rates


@pytest.mark.parametrize("p", P.PERSONAS, ids=lambda p: p.key)
def test_every_rate_is_a_sane_fraction(p):
    assert 0.0 <= p.feedback_up_rate <= 1.0


@pytest.mark.parametrize("p", P.PERSONAS, ids=lambda p: p.key)
def test_no_persona_is_unanimous_by_accident(p):
    """Each persona's own turn count must yield a mixed result.

    Checked per persona rather than in aggregate: a persona with few turns and a
    high rate can still come out unanimous, and that is worth knowing about
    deliberately rather than discovering on a dashboard.
    """
    n = sum(len(s.prompts) for s in p.scenarios)
    got = votes_for(p.feedback_up_rate, n)
    # A persona needs enough turns for its rate to produce a whole negative:
    # 3 turns at 0.75 is 0.75 of a negative, i.e. none. That is arithmetic, not
    # a bug, so it is skipped rather than asserted away by loosening the rate.
    if p.feedback_up_rate < 1.0 and n * (1.0 - p.feedback_up_rate) >= 1.0:
        assert "down" in got, (
            f"{p.key}: {n} turn(s) at rate {p.feedback_up_rate} produced no "
            f"negative vote, so this persona contributes nothing to the negative "
            f"rate the card plots")


# ---------------------------------------------------------------------------
# Scenario sourcing
# ---------------------------------------------------------------------------

def test_specialist_personas_draw_their_prompts_from_the_shared_library():
    """Guards against a private copy drifting from the chatbot's examples."""
    library = {q for prompts in P.EXAMPLES.values() for q in prompts}
    for key in ("frank", "grace", "henry", "iris"):
        persona = P.PERSONA_BY_KEY[key]
        for sc in persona.scenarios:
            for prompt in sc.prompts:
                assert prompt in library, f"{key}/{sc.name}: {prompt!r} not in library"


def test_every_persona_has_at_least_one_scenario():
    for p in P.PERSONAS:
        assert p.scenarios, p.key

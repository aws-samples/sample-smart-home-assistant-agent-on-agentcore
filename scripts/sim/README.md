# Simulated end users

Generates **real** agent traffic so the Overview ops dashboard and AgentCore
Evaluations have something to show. Test users sign in through Cognito and talk
to the deployed runtime over the same SigV4 `/invocations` path the chatbot
uses, so the telemetry (spans, tokens, sessions, eval scores) is
indistinguishable from real usage.

> **Running this before a demo?** The step-by-step runbook — prerequisites,
> what to verify afterwards, troubleshooting — is
> [admin manual §10.3](../../docs/admin_manual_管理员使用手册.md).
> This file covers the implementation.

## Quick start

```bash
export SIM_USER_PASSWORD='SomeStrong#Pass1'   # must satisfy the Cognito policy

python3 scripts/simulate-users.py setup       # create + configure 9 users
python3 scripts/simulate-users.py run         # light tier, 52 turns (~3.5-5 min)
python3 scripts/simulate-users.py run --heavy # + browser-use, code-interpreter
python3 scripts/simulate-users.py run --days-back 45   # spread votes for 60d/90d views
python3 scripts/simulate-users.py status      # who exists, with what config
python3 scripts/simulate-users.py teardown --yes
```

Wait a couple of minutes after `run` before checking the dashboard — CloudWatch
ingestion lags, and the dashboard caches for 5 minutes (use its Refresh button
to force re-aggregation).

## Personas

Each persona is a different tenant profile, so the dashboard's attribution
charts show several real rows instead of one bucket.

| persona | model | tenant env | exercises |
|---|---|---|---|
| `alice` | Opus 4.6 | default | all four devices, discovery, all-devices-on |
| `bob` | Sonnet 4.6 | default | knowledge base, weather (http_request), refusal |
| `carol` | Haiku 4.5 | ab-bundles | multi-turn memory recall, user feedback |
| `dave` | Kimi K2.5 | ab-targets | code-interpreter (heavy) |
| `erin` | Sonnet 4.5 | default | browser-use (heavy), refusal, ambiguity |
| `frank` | Sonnet 4.6 | default | light-effect + scene-sync specialists |
| `grace` | Haiku 4.5 | ab-bundles | task-management (solar triggers), orchestration |
| `henry` | Opus 4.6 | ab-targets | energy-optimization + home-security specialists |
| `iris` | Sonnet 4.5 | default | appliance-maintenance, docs Q&A, concurrent delegation |

**Scenario prompts come from `shared/prompt-examples.json`** — the same file the
chatbot renders as its example drawer. Bound by group id via `from_group()`, so a
new capability becomes demo traffic as soon as someone writes its example. Before
`frank`–`iris` existed, not one of the eight A2A specialists ever saw traffic.

**Votes.** After the conversations each persona files real 👍/👎 on its own turns
through the same feedback API a human uses, tagged `source="sim"`. Rates are per
persona (0.7–0.8) so the satisfaction card shows a distribution. Keep them
*reachable* at the persona's own turn count: 6 turns at 0.85 rounds to 0.9
negatives, i.e. none, and that persona then contributes nothing to the negative
rate — `tests/test_vote_distribution.py` asserts this per persona.

Scenario tiers: **light** turns run 3–25s (52 turns across 9 personas measured
~215s warm; a three-specialist concurrent turn is the slow one at 52–69s);
**heavy** (`--heavy`) covers
code-interpreter and browser-use, measured 19–141s per turn, and browser-use
occupies a real DCV browser session.

## Safety

Everything is scoped to the `simuser+` email prefix and `Provisioner._guard()`
raises on anything else, so `teardown` cannot touch the real users in the pool.
Setup is idempotent: existing users are reused, not recreated.

## Gotchas worth knowing

- **`SIM_USER_PASSWORD` is required.** Nothing is hard-coded.
- **Session ids must be ≥33 characters.** AgentCore rejects shorter ones with a
  400. The chatbot's `user-session-{sub}-{epoch_ms}` shape is what we mirror.
- **Granting tools returns 200 before it takes effect.** The Cedar attach can
  still fail afterwards, and a failed attach means the gateway serves that user
  *zero* tools while the agent just answers "that's beyond my knowledge" — no
  error anywhere. `setup` therefore polls until every policy is ACTIVE
  (~75s) and fails loudly if they aren't.
- **Two different keys.** Tool permissions are keyed by Cognito **sub**; model
  settings and tenant env are keyed by **email**.
- **The runtime ARN isn't a CDK output.** It's read from the admin Lambda's env,
  which `setup-agentcore.py` patches. A bare `cdk deploy` resets it to a
  placeholder — the loader raises a clear error if so.
- **`setup` grants MCP tools, not A2A skills.** A persona bound to a specialist
  group still needs an A2A grant from Admin Console → Integration Registry, or
  those turns come back "outside my current tool / skill / agent capabilities" —
  a legitimate refusal that looks like a broken specialist. See admin manual
  §11.11 for why the grantable catalog is currently short.
- **`--days-back` only moves rows this script writes** (the votes). Span and
  evaluation timestamps are stamped by AgentCore and cannot be backdated, so the
  90d view stays sparse before today. That is real, not a bug — filling it would
  mean writing fabricated telemetry.

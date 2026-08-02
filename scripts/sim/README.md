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

python3 scripts/simulate-users.py setup       # create + configure 5 users
python3 scripts/simulate-users.py run         # light tier (~2 min warm)
python3 scripts/simulate-users.py run --heavy # + browser-use, code-interpreter
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

Scenario tiers: **light** turns run 3–25s (23 turns across 5 personas takes
~97s warm, ~212s if the runtime is cold); **heavy** (`--heavy`) covers
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

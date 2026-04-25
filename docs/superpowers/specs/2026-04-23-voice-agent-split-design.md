# Voice Agent Split — Design Spec

**Date:** 2026-04-23
**Status:** Approved for implementation
**Authors:** @huanghang111 + Claude

## Problem

The chatbot's voice mode uses the same AgentCore Runtime container as the text
path. First-time `/ws` handshake pays a large cold-start penalty because the
container hasn't imported `strands.experimental.bidi.*` yet — those imports are
`late` inside `voice_session.py`, and the login-time `__warmup__` ping only
hits `/invocations` (which never triggers voice imports). Users experience
seconds of silence between tapping the voice button and the welcome clip.

Text and voice also have different hot paths: text needs `BedrockModel(Kimi)` +
`AgentSkills` plugin; voice needs `BidiAgent` + `BidiNovaSonicModel` + MCP
client. Keeping them in one container means every container serves both
workloads and neither gets to optimise for its own import/init cost.

## Goal

Split voice into its own AgentCore Runtime so that:

- The login-time warmup predictably heats the voice container's `strands.bidi`
  import chain.
- Voice and text scale/redeploy independently.
- Per-user auth, Cedar/MCP gateway, skills, prompt overrides continue to work
  with no user-visible change in behavior.

Secondary goal: bundle cheap, same-session latency optimisations that are
painful to land in isolation (ADOT skip on voice, welcome-audio parallelism,
mic worklet prefetch, Cognito preconnect).

## Non-goals

- No change to the MCP gateway topology (both runtimes keep using the same
  `SmartHomeGateway` with CUSTOM_JWT).
- No change to the chatbot's auth flow (still Cognito User Pool idToken →
  Identity Pool creds → SigV4).
- No provisioned-concurrency / keep-alive heartbeat (deferred; revisit once
  we have real cold/warm latency numbers).
- No DynamoDB batching, MCP `list_tools` caching — deferred follow-ups.

## Architecture

### Two runtimes, shared everything else

| Item | Text runtime (`smarthome`) | Voice runtime (`smarthomevoice`) |
|---|---|---|
| Entrypoint | `agent/agent.py` (unchanged entrypoint, WS handler removed) | `agent/voice_agent.py` (new) |
| Public paths | `POST /invocations` (text chat + warmup short-circuit) | `POST /invocations` (warmup short-circuit only) + `GET /ws` (voice) |
| Auth | `AWS_IAM` (SigV4) | `AWS_IAM` (SigV4) |
| `protocolConfiguration.serverProtocol` | default | `"HTTP"` (required for `/ws` upgrade routing) |
| `requestHeaderAllowlist` | `X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken` | same |
| Env vars | `MODEL_ID`, `SKILLS_TABLE_NAME`, `AGENTCORE_GATEWAY_URL`, `AGENTCORE_GATEWAY_ARN`, `AWS_REGION` | `NOVA_SONIC_MODEL_ID`, `SKILLS_TABLE_NAME`, `AGENTCORE_GATEWAY_URL`, `AGENTCORE_GATEWAY_ARN`, `AWS_REGION` |
| IAM role | DynamoDB skills R, Bedrock Invoke (Kimi) | DynamoDB skills R, `bedrock:InvokeModelWithBidirectionalStream` (Nova Sonic) |
| welcome-zh.mp3 | packaged (harmless) | packaged, loaded into `_WELCOME_BYTES` at import |
| ADOT auto-instrumentation | on | **off** (gated by `DISABLE_ADOT=1` env var — see "ADOT gate" below) |

Both runtimes use the **same gateway** for MCP. The same Cognito authenticated
role is granted `InvokeAgentRuntime{,WithWebSocketStream}` on both runtime
ARNs.

### Code layout (single package, two entrypoints)

```
agent/
  agent.py                 # text entrypoint (unchanged logic; ws_voice deleted)
  voice_agent.py           # NEW — voice entrypoint
  voice_session.py         # existing voice logic (unchanged business code)
  welcome-zh.mp3           # packaged into both CodeZips (only voice reads it)
  memory/, skills/, tools/ # unchanged
  Dockerfile, pyproject.toml, requirements.txt  # unchanged, shared
```

`voice_agent.py` imports helpers directly from `agent.py`:
`_extract_user_auth`, `_record_session`, `load_skills_from_dynamodb`,
`load_system_prompt`, and the module constants `SKILLS_TABLE_NAME`,
`AWS_REGION`, `GATEWAY_ARN`. No shared subpackage is introduced (A option in
brainstorming).

### ADOT gate (shared with `agent.py`)

`voice_agent.py` must import helpers from `agent.py`. Without a gate, that
import alone would trigger the top-of-file ADOT `sitecustomize.initialize()`
and `StrandsTelemetry()` side-effects — defeating the "skip ADOT on voice"
optimisation.

**Fix:** wrap `agent.py`'s ADOT block in an env check:

```python
# top of agent.py, before other imports
import os
if os.environ.get("DISABLE_ADOT") != "1":
    os.environ.setdefault("OTEL_PYTHON_DISTRO", "aws_distro")
    os.environ.setdefault("OTEL_PYTHON_CONFIGURATOR", "aws_configurator")
    from opentelemetry.instrumentation.auto_instrumentation import sitecustomize
    sitecustomize.initialize()
```

And similarly guard the `StrandsTelemetry()` call later in the file.

The voice runtime sets `DISABLE_ADOT=1` in its environment (in
`setup-agentcore.py`'s voice-runtime env-var block). Text runtime leaves the
variable unset, keeping current ADOT behavior.

### `agent/voice_agent.py` shape

```python
import os
# ADOT auto-instrumentation is explicitly SKIPPED on voice runtime — see
# "ADOT gate" above. Setting DISABLE_ADOT=1 here is belt-and-braces: the
# runtime env var is the actual source of truth; this env mutation also
# protects local dev runs.
os.environ.setdefault("DISABLE_ADOT", "1")

# Eager imports — warmup HTTP request triggers Python's module init for these,
# so first /ws handshake doesn't pay the strands.bidi import cost.
from strands.experimental.bidi.agent import BidiAgent                       # noqa: F401
from strands.experimental.bidi.models.nova_sonic import BidiNovaSonicModel  # noqa: F401
from strands.tools.mcp.mcp_client import MCPClient                          # noqa: F401
from mcp.client.streamable_http import streamablehttp_client                 # noqa: F401

from bedrock_agentcore import BedrockAgentCoreApp
from agent import (  # reuse text-runtime helpers, "change in one place"
    load_skills_from_dynamodb,
    load_system_prompt,
    SKILLS_TABLE_NAME,
    AWS_REGION,
    GATEWAY_ARN,
)

app = BedrockAgentCoreApp()

@app.entrypoint
def handle_invocation(payload, context):
    """Voice runtime only serves /ws; /invocations is warmup only."""
    if payload.get("prompt") == "__warmup__":
        return {"status": "warmup_ok"}
    return {"error": "voice runtime only serves /ws; use the text runtime for text chat"}

@app.websocket
async def ws_voice(websocket, context):
    from voice_session import handle_voice_session  # cheap re-import after warmup
    await handle_voice_session(websocket, context,
                               gateway_arn=GATEWAY_ARN, region=AWS_REGION)

if __name__ == "__main__":
    app.run(log_level="info", ws="websockets")
```

### `agent/agent.py` changes

1. **Remove** the `@app.websocket` `ws_voice` function — text runtime no longer
   serves `/ws`. (Requests arriving there would fail anyway; the browser will
   target the voice runtime ARN.)
2. Change `_record_session` DynamoDB key from `skillName="__session__"` to
   `skillName="__session_text__"` so voice sessions can be recorded separately
   under `__session_voice__`.
3. Everything else stays (helpers are re-used by `voice_agent.py`).

### `agent/voice_session.py` changes

1. Add `_record_voice_session(actor_id, session_id)` writing
   `skillName="__session_voice__"` (mirrors `agent._record_session`).
2. **Welcome audio parallelism:** start `_welcome_stream(...)` immediately
   after `_wait_for_config` returns (and after `BidiAgent` is constructed +
   `agent.run` has entered its task loop). Current code adds a 0.2s sleep to
   let the pipeline establish — keep it. The new change is that
   `_welcome_stream` no longer waits for the `Agent ready.` message to be
   acked; it runs strictly concurrent with `agent.run(...)`.
3. Remove the `from agent import load_skills_from_dynamodb, SKILLS_TABLE_NAME`
   late-imports (make them module-level now that voice runs in its own
   container and is always fine with importing `agent`).

## Frontend changes

### `chatbot/src/config.ts`

Add `voiceAgentRuntimeArn: string`. Default to `''`. Keep `agentRuntimeArn`
meaning "text runtime" (existing).

### `chatbot/src/components/ChatInterface.tsx`

1. **Warmup** (`useEffect` at mount): fetch `idToken` + creds once, then
   `Promise.all` two `signedInvocationsFetch` calls — one per runtime ARN —
   both with `prompt: "__warmup__"`. Both best-effort (swallow errors).
2. **Pre-presign voice WS URL** during warmup: right after the two HTTP
   warmups, call `presignWsUrl(...)` against the **voice** runtime ARN and
   cache the string in a ref. `startVoice` uses the cached URL if it's still
   within its 5-minute TTL; otherwise it re-presigns (current behavior).
3. `startVoice` passes `config.voiceAgentRuntimeArn` to `presignWsUrl` (not
   `config.agentRuntimeArn`).
4. **Mic worklet prefetch:** on mount, fire `fetch('/pcm-recorder-processor.js')`
   to warm the browser HTTP cache. Don't call `getUserMedia` (permission
   prompt is unwanted until the user opts in).

### `chatbot/src/auth/LoginPage.tsx`

Add a `<link rel="preconnect" href="https://bedrock-agentcore.<region>.amazonaws.com">`
tag on mount (cleanup on unmount). Uses `getConfig().region` to build the URL.
This opens the TLS connection to the runtime endpoint while the user is still
typing their password, saving ~50-150ms on the first warmup.

## Setup script (`scripts/setup-agentcore.py`)

### Structural changes

1. **Duplicate agent create/deploy** — two `agentcore create --name X` +
   `agentcore deploy` pairs. The voice one patches its `agentcore.json`
   `entrypoint` to `voice_agent.py`.
2. **Helper: `patch_runtime(runtime_id, runtime_arn, *, is_voice)`** wraps
   the existing env-var patch / auth-mode patch / session-stop / IAM-role
   policy attach block. Called twice.
3. **CFN stack outputs:** fetch `AgentCore-smarthome-default` **and**
   `AgentCore-smarthomevoice-default`. Extract both runtime IDs/ARNs.
4. **Cognito authenticated role policy:** `Resource` list is the Cartesian
   `[textArn, f"{textArn}/*", voiceArn, f"{voiceArn}/*"]`.
5. **Voice runtime role** gets `bedrock:InvokeModelWithBidirectionalStream`
   permission; text runtime role does NOT need it anymore (remove to minimise
   blast radius, but only if we're confident the text path doesn't exercise
   bidi — it doesn't).
6. **Session invalidation:** scan DynamoDB for BOTH `__session_text__` and
   `__session_voice__` records; call `stop_runtime_session` on the matching
   ARN per key. Ignore `ResourceNotFoundException` as today.
7. **State file** (`agentcore-state.json`): add `voiceRuntimeId`,
   `voiceRuntimeArn`. Preserve the existing fields.
8. **Chatbot config write:** include `voiceAgentRuntimeArn`.

### Order of operations

Text first, then voice. The text deploy publishes the gateway-target-aware
CodeZip; voice deploy reuses the same gateway ARN. Knowledge-base init, admin
Lambda patch, Skill ERP Registry creation — all unchanged, run once after both
runtimes are up.

## Admin API (`cdk/lambda/admin-api/`)

Sessions list: change the DynamoDB filter from
`skillName = "__session__"` to
`skillName IN ("__session_text__", "__session_voice__")`.

No UI column added for runtime type (out of scope). Both are shown in the
existing list; the sessionId prefix already lets admins distinguish.

## Teardown (`scripts/teardown-agentcore.py`)

Delete both agentcore projects, both runtimes. If `agentcore-state.json`
contains `voiceRuntimeId`, delete it; if not (upgrading from pre-split state),
skip voice cleanup and continue.

## Regression test checklist (run after deploy, before commit)

| Item | Pass criteria | How |
|---|---|---|
| Two runtimes exist | `aws bedrock-agentcore-control list-agent-runtimes` shows both | CLI |
| Text warmup | Network panel shows `/invocations` → 200 `{"status":"warmup_ok"}` against text ARN | Chatbot DevTools |
| Voice warmup | Same, against voice ARN, within ~1s of login | Chatbot DevTools |
| Text chat | `"turn on led matrix to rainbow"` triggers MQTT publish | Device Sim |
| Voice cold start | Kill voice session via admin console, reconnect → `Agent ready.` under ~3s of ws.open | Console log |
| Voice warm start | Log in, wait for warmup, click voice → `Agent ready.` noticeably faster than cold | Stopwatch; log the diff |
| Welcome clip | Chinese "欢迎使用智能家居设备助手" plays | Speakers |
| Per-user voice prompt override | Edit `__prompt_voice__` via Agent Prompt tab → reconnect → new prompt active | Admin Console + voice |
| `turn_on_all_devices` | Speak "打开所有设备" → all 4 simulator devices power on | Device Sim |
| Session invalidation on redeploy | Run `06-deploy-agentcore.sh` with an active voice session → session closes | Voice client disconnect |
| Admin Sessions tab | Both text and voice sessions listed with token counts | Admin Console |
| Preconnect | `Network` panel shows TCP/TLS to `bedrock-agentcore.<region>.amazonaws.com` initiated before form submit | DevTools |

## Rollout / rollback

**Blue/green within one commit-series** (not a feature flag):

1. **Commit 1:** add voice runtime creation + voice_agent.py + frontend
   `voiceAgentRuntimeArn` handling. The `@app.websocket` in `agent.py` is
   **kept**. Frontend prefers `voiceAgentRuntimeArn` for voice; falls back to
   `agentRuntimeArn` if the former is empty (transition-safe for users whose
   config.js is stale).
2. Deploy, verify regression checklist. Voice traffic is now on the new
   runtime; old runtime's `/ws` becomes dead code but still functional.
3. **Commit 2 (later, after soak):** delete `ws_voice` from `agent.py`.

Rollback at commit 1 is just `git revert`; voice flows back through text
runtime's `/ws` (still wired). Rollback after commit 2 is `git revert` of
commit 2 only.

**Execution in this PR:** Since the user requested "deploy first, test, then
commit", we will squash both commits into a single post-validation commit.
The blue/green safety net exists only as a conceptual rollback plan: if the
deployment fails, we `git checkout` the files and redeploy.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| `agentcore create --name smarthomevoice` conflicts with existing deploy | Delete any stale `.agentcore-project/smarthomevoice/` before each run; the script already rmtree's `.agentcore-project/` root. |
| Two concurrent CFN deploys step on each other | Run text deploy first, `agentcore deploy` is synchronous; voice starts after text returns. |
| `voice_agent.py` eager imports fail on startup | Dockerfile install path is identical to current text container; if the imports aren't installable, current voice mode is already broken. Low risk. |
| Users stuck on stale `config.js` after deploy | CloudFront invalidation already issued by `setup-agentcore.py`; additionally the frontend falls back to `agentRuntimeArn` when `voiceAgentRuntimeArn` is empty (commit 1 safety). |
| Admin Sessions list missing pre-split `__session__` records | One-time DB migration: the admin Lambda's new filter won't match old `__session__` records. Acceptable — those sessions are already dead; they'd be cleaned out at next idle timeout. No migration needed. |
| Cognito authenticated role's inline policy > 2048 chars (IAM limit) | Two ARNs + `/*` is still well under. Sanity check during implementation. |

## Out-of-scope optimisations (follow-ups)

Tracked here so we don't lose them, but NOT implemented in this spec:

- MCP `list_tools_sync` per-JWT TTL cache (60s) — medium effort, saves 300-800ms
  on repeat connects.
- DynamoDB skills + prompt reads via `BatchGetItem` — medium effort, saves
  100-300ms.
- Keep-alive heartbeat (periodic `__warmup__` while user is active) — trade-off
  between always-warm and Bedrock-agentcore idle cost.
- Shorter welcome clip / lower bitrate Polly — product call.
- `BedrockModel` first-invoke warmup on Nova Sonic control plane — no clean
  path; dropped.

## Implementation task ordering

1. `agent/voice_agent.py` + `agent/voice_session.py` tweaks + `agent/agent.py`
   session-key rename + remove `ws_voice`.
2. `scripts/setup-agentcore.py` — helper refactor, two `agentcore create`
   blocks, IAM + Cognito role policy, session-stop split, state file.
3. `cdk/lambda/admin-api/` — session list filter update.
4. Frontend — `config.ts`, `ChatInterface.tsx`, `LoginPage.tsx`.
5. Deploy: `scripts/04-cdk-deploy.sh` (no CDK changes expected) then
   `scripts/06-deploy-agentcore.sh`.
6. Run regression checklist.
7. `docs/architecture-and-design.md` §9.7 update — reflect dual-runtime
   architecture, dual warmup, session-key split.
8. Single commit of all of the above.

---

## Post-implementation notes (added 2026-04-25 after deploy + testing)

### What shipped as-designed
- Dual runtime (text `smarthome` + voice `smarthomevoice`) with shared
  `agent/` package and distinct entrypoints.
- `DISABLE_ADOT=1` gate on voice runtime.
- Session key split (`__session_text__` / `__session_voice__`) + per-kind
  stop routing through admin-api.
- Frontend dual warmup with shared creds fetch.

### Corrections vs spec
1. **Runtime naming.** `agentcore create --name smarthome-voice` was
   rejected by the CLI ("Project name must contain only alphanumerics").
   Renamed to `smarthomevoice` (no hyphen). CFN stack name is therefore
   `AgentCore-smarthomevoice-default`.
2. **`mcp_gateway_arn=[...]` must be removed, not kept.** Spec wording
   said "Do not pass" but framed it as optional. In practice keeping it
   broke tool dispatch entirely: Strands emitted `toolUse` events but
   the `toolResult` was swallowed by the model's internal MCP path
   (runtime-role credentials, bypassing our JWT MCPClient), so Nova
   Sonic hung waiting for a result and the WS silently stalled ~20 s.
   Fix: constructor now takes no gateway args; tools flow exclusively
   through `BidiAgent(tools=list_tools_sync(user_jwt_client))`.
3. **Transcript dedup key is `completionId`, not `contentId`.** Spec
   assumed SPECULATIVE and FINAL share a `contentId`. They don't — Nova
   Sonic opens a fresh content block for each stage (distinct
   `contentId`, `contentStart.additionalModelFields.generationStage`
   flips). The stable dedup key is the outer `completionId` set by
   `completionStart`. Shipped a `_TranscriptIdTaggingModel` subclass
   that surfaces both `completionId` and `generationStage` on the event
   dict before Strands forwards it.

### Additional optimizations actually landed
- **Welcome clip env-gated, default OFF** (`VOICE_WELCOME_ENABLED`). The
  audio greeting masked real startup latency during testing and added
  no value on top of the UI `connection-banner`. Re-enable anytime by
  setting the env var on the runtime.
- **DDB skill/prompt reads parallelized with MCP `list_tools_sync`** via
  `asyncio.gather(skill_task, prompt_task, tools_task)` — previously
  sequential ~1 s, now max(~400 ms).
- **Frontend pre-presigns voice WS URL** during the login warmup with
  a 4-min TTL, saving the SigV4 presign + Identity-Pool-creds fetch on
  the first voice-button tap. One-shot cache (presigned WS URLs can't
  be reused).
- **`voice_agent.py::_preheat_boto3_clients()`** runs on each `__warmup__`
  request, forcing DynamoDB + Bedrock endpoint resolution + TLS +
  credential fetch so the first `/ws` handshake doesn't pay it. Voice
  runtime IAM role gained `bedrock:ListFoundationModels` (read-only
  control-plane action used as the preheat call).
- **Per-kind stop-session** via `POST /sessions/{id}/stop?kind=text|voice`
  — text and voice share a sessionId (derived from the Cognito `sub`),
  so the caller has to say which runtime. Admin UI adds a Kind column
  and routes Stop accordingly; admin Lambda deletes the DynamoDB
  session record on success so the list clears immediately.

### Measured latency (2026-04-25, us-west-2)
End-to-end click → `Agent ready.`:

| Phase | Duration |
|---|---|
| SigV4 presign | ~0 ms |
| WebSocket handshake (AgentCore edge proxy) | **~6-7 s** |
| Server-side: connection accepted → Nova Sonic connection established | **~800 ms** |
| &nbsp;&nbsp;↳ DDB + MCP (parallel) | max ~400 ms |
| &nbsp;&nbsp;↳ BidiAgent construction | ~30 ms |
| &nbsp;&nbsp;↳ Nova Sonic connect | ~50 ms |

**Takeaway:** server-side budget is essentially saturated. Further
compression of total time requires pre-opening the WS at login (masks
the 6-7 s AgentCore proxy handshake) or action from the AgentCore team
on that proxy. Documented in §9.7.

### Dropped from scope
- **MCP `list_tools_sync` per-JWT TTL cache.** Strands tool objects are
  bound to their MCPClient instance — caching them across sessions would
  point at dead connections. Real cache would need decoupled tool spec
  storage + per-session wrapper rebuild; too invasive for the marginal
  benefit (list_tools is already off the critical path via parallelization).

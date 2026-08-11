# Spec 5 — what each phase actually changed

Every number here was measured against the deployed system on 2026-08-11. Where a
phase did not deliver what the spec expected, the measurement is given and the
expectation is named; two phases were redefined by their own measurements and one
proposal was dropped.

Baseline: `s0-baseline-cold-20260811-0436.json` — 10 fixed read-only prompts
(5 fast-path, 5 delegating across 5 different specialists) × 3 repeats, cold
sessions, Claude Opus 4.6.

| Group | wall | platform | server | llm | tool | harness | in tok | out tok |
|---|---|---|---|---|---|---|---|---|
| fast (15) | 17.3s | 7.3s | 10.0s | 5.9s | 1.3s | 2.9s | 21,212 | 185 |
| delegated (15) | 31.3s | 7.0s | 24.3s | 10.0s | 11.5s | 2.9s | 22,958 | 393 |

Routing: 15/15 delegating prompts reached an `a2a_*` tool. Run-to-run spread —
which sets the bar for any later claim — is 13% on delegated wall and 30% on fast
wall, so **a fast-path improvement under ~15% is indistinguishable from noise at
three repeats**.

---

## Summary

| Phase | Delivered | Measured |
|---|---|---|
| **S0** baseline | `scripts/measure-baseline.py` + archive | 30 measurements; found 7.1s/turn is platform, not ours |
| **S1** shared memory | 8 sub-agents read the user's memory, read-only | verified live on a tool-using and a prompt-only agent |
| **S2** context trimming | device brief on every delegation | **-1.64s (-10%)**, won 4/4 A/B pairs |
| **S3** prompt caching | cache point on the orchestrator's prefix | **input tokens 29,644 → 9**; latency 2% (noise) |
| **S4** parallel delegation | loop moved off the calling thread | **51.1s → 17.2s (-66%)** on 3 domains |
| **S4** prewarming | **dropped** | ~0.3s after 100 min idle — nothing to win |
| **S5** progress streaming | tool lifecycle over SSE | specialist named at **12s** instead of 31s of silence |

---

## S0 — baseline latency and routing measurement

`scripts/measure-baseline.py`. Ten fixed prompts, joined to the runtime's own spans
by session id, archived as JSON so two runs weeks apart compare directly.

**The finding that reframed the rest of the spec.** Of a 24.3s mean turn, **7.1s is
spent inside AgentCore before our container is entered**:

| Session | first turn | subsequent turns |
|---|---|---|
| fresh id each time | ~7s platform | ~7s platform |
| one reused id | ~7s platform | **~0.4s** platform |

A "16s fast path" is therefore about 8s of agent work behind 8s of platform session
creation. Reporting `wall` alone would have credited the platform's cold start to
the harness in both directions, so `platform` is its own column and `--compare`
refuses to diff a warm run against a cold one.

Two bugs found in the instrument itself, both worth recording because both produced
plausible output rather than errors:

- The `POST /invocations` span is under the **starlette** scope, not the Strands
  one. Filtering on the Strands scope alone silently drops it — and it is the only
  measurement of how much of the wall clock was ours.
- Warm-mode turn boundaries from the client clock (with 2s slack) let each turn
  absorb its successors' spans, because turns run back-to-back with no idle gap.
  The table looked like a result: monotonically decaying, plausibly ordered.
  `_impossible()` now rejects any row where the container claims more time than the
  client waited, and refuses to archive the run.

---

## S1 — cross-agent shared memory

All eight specialists retrieve from the Memory the orchestrator writes, under the
same actor-partitioned namespaces, **read-only**.

Verified live:

- **light-effect** (has tools): "set up an effect, use whatever you know I like",
  with no colour named, produced a calm ocean wave. Its runtime logged
  `retrieved 4 memory record(s) for actor admin_smarthome_local` — the
  orchestrator's own namespace.
- **home-security** (no tools at all): cited "you run 6 active automations daily"
  as why the risk is worse. It has no way to look that up.
- Exactly two `RetrieveMemoryRecords` spans per delegation, zero `CreateEvent`.

Write access is withheld by IAM, not by convention: `A2ASharedMemoryRead` grants
`RetrieveMemoryRecords` alone.

---

## S2 — context trimming

`scripts/ab-delegation-brief.py`, four alternating pairs against the deployed
light-effect agent:

| | mean | msg size |
|---|---|---|
| without brief | 16.83s | 72 chars |
| with brief | **15.19s** | 914 chars |
| delta | **-1.64s (-10%)** | |

The brief won every pair. It removes the specialist's opening `discover_devices`
cycle — the call is ~0.2s but the LLM turn around it was 1.0-1.3s.

Size discipline was the point: the full discovery payload is ~1,800 tokens, and
pasting it in would have moved the cost into the prompt rather than removing it. The
brief is 99-210 tokens (5-11%).

**This phase took three deploys.** The first two changed the system prompts, and the
specialists ignored them — because `discover_devices`' own docstring still said
"Call this FIRST, every time", and a tool description outranks the system prompt
about that tool. Nothing in any reply showed it.

---

## S3 — prompt caching (a cost win, not a latency win)

The orchestrator's prefix is ~10.5k tokens, byte-identical every call. On the live
runtime, with a cache point:

| | InputTokenCount | CacheRead | CacheWrite |
|---|---|---|---|
| before | 29,644 | 0 | 0 |
| after | **9** | 20,988 | 10,512 |

**Latency did not move.** Two controlled runs at ~15.6k prefix tokens, 12 and 8
alternating calls: 2.11s uncached against 2.06s cached, and 2.70s against 2.68s. AWS
documents "up to 85% latency reduction"; at our prefix size the prefill was never
the bottleneck. The 98% token cut is real and every turn of every user pays that
prefix, so it is worth having — but calling it a latency optimisation would be a
claim the numbers do not support.

The sub-agents deliberately do not cache: prefixes of 362-4,211 tokens are below the
checkpoint minimum (a 2,817-token Haiku call with a cache point returned
`cacheRead=0, cacheWrite=0`), and their prefix varies per request. Writes bill at
1.25×, so it would have cost 25% more per delegation for zero hits.

---

## S4 — parallel delegation

`scripts/ab-parallel-delegation.py`, three alternating pairs across three
specialists:

| | mean |
|---|---|
| serial | 51.1s |
| parallel | **17.2s** |
| delta | **-33.9s (-66%)** |

The spec assumed this needed building. It did not — Strands already issues
independent tool calls concurrently (measured: two tools starting within 0.00s of
each other). What was broken was the transport: one shared event loop driven with
`run_until_complete`, so the **third** concurrent delegation raised
`RuntimeError: This event loop is already running`, was counted as an endpoint
failure, and was shown to the user as "A2A agent call failed" — against a healthy
specialist, and three of them would have tripped its circuit breaker.

After the fix, eight concurrent delegations complete in the time of one. Verified
live: a three-domain request produced three `a2a_*` spans all starting at +0.00s.

### Prewarming: proposed, measured, dropped

After 100+ minutes idle — far past the 900s session timeout — a specialist's first
call was ~0.3s slower than its warm calls:

| Agent | idle | first call | warm calls |
|---|---|---|---|
| energy-optimization | 105 min | 8.52s | 8.39s, 7.99s |
| home-security | 103 min | 9.65s | 9.28s, 8.52s |

AgentCore keeps these runtimes hot. Prewarming would have spent tokens to save
noise, so it is not implemented, and the reason is recorded in the harness so it is
not re-derived.

---

## S5 — progress streaming

The spec proposed streaming the A2A hop so TTFT would fall from ~30s to single
digits. Timed against `Agent.stream_async`, that is unreachable:

```
+0.00s  init_event_loop
+1.88s  messageStart          <- first token of the turn
+1.88s  tool_use_stream       <- and it is a TOOL CALL, naming the specialist
+2.16s  message (toolUse complete)
+8.13s  first text delta      <- the first PROSE, after the tool returned
```

A model cannot write its answer before the tool it just called returns, so
time-to-first-prose is bounded below by the specialist's own latency however the hop
is transported.

What *is* available early is the specialist's name. Measured on the live runtime, a
three-domain request:

```
+12.0s  progress  a2a_home_security_agent_risk_assessment
+13.1s  progress  a2a_energy_optimization_agent_estimate_savings
+13.8s  progress  a2a_knowledge_qa_agent_answer_from_docs
+44.8s  answer
```

So the user sees "asking the Home Security specialist…" at 12s instead of a
motionless "thinking…" until 45s. Verified in the browser.

Nearly shipped broken: BedrockAgentCoreApp adds the SSE framing **and**
JSON-encodes each yielded value, so hand-framing `data: …` produced
`data: data: {…}` and a frame that decoded to a *string*. `evt.type` on a string is
undefined, so the client would have silently dropped every frame including the
answer. Caught by reading the live stream, not by the unit tests — which had
encoded the same wrong assumption.

---

## Bugs found along the way

None of these were in scope; all were pre-existing and silent.

| Bug | Symptom | Fix |
|---|---|---|
| Spans moved out of `aws/spans` on 2026-08-05 | every TTFT/token card read "no data" for six days, exactly as if idle | query both sources, filter to groups that exist |
| `agentcore deploy` packages a stale copy of `agent/` | two features committed and "deployed" while absent from the container | `scripts/sync-agent-code.py --check` |
| `UpdateRegistryRecord` wraps **every** level in `optionalValue` | redeploy silently minted a new recordId and voided all A2A grants | validated against the botocore service model |
| Tool docstring outranked the system prompt | correct answers, optimisation never happened | `test_discover_guidance.py` asserts both halves agree |
| Third concurrent delegation crashed | "A2A agent call failed" against a healthy specialist | loop on its own thread |

The common thread is in `docs/agent-design-principles.md` §1.13: each of these
reported success. Assert the thing you want from outside the code that claims to do
it — read spans rather than reply text, validate against the service model rather
than the docs, diff the deployed copy against the repo.

---

## S6 — developer experience

| Item | Status |
|---|---|
| `docs/agent-design-principles.md` | 3 chapters, every entry with `file:line` + a number |
| README design chapter (both languages) | leads with the five counter-intuitive findings |
| This report | phase-by-phase before/after |
| Scenes as code | export/import JSON, validated by the agent's own validator |
| Structured JSON output | `responseFormat: "json"`, plus a fence-stripper the prompt could not replace |
| Delegation trace panel | built from the S5 progress stream — free and instant |
| End-user skill publishing | **already shipped**; Skill ERP + approval flow verified live |

Two of these were narrowed by what already existed or by measurement:

- The **trace panel** was specified to read `aws/spans` per turn for route, tools,
  ms and tokens. That is a 10-20s Logs Insights query to learn what the SSE stream
  said seconds earlier, so it would arrive long after the answer it describes. Built
  from the stream instead; ms and tokens stay on the Overview dashboard, where
  aggregates belong.
- **Self-service skill publishing** needed no work. Skill ERP already gives any
  confirmed Cognito user a publish surface with a curator approval flow; verified
  live, with an end-user-published skill sitting in Approved state.

Three bugs the live tests found in the new code:

| Bug | Symptom |
|---|---|
| Scenes are keyed by Cognito **sub**, not email | first export returned 0 scenes for a user with six |
| Admin Lambda had read-only on the scenarios table | all six scenes validated, then every PutItem failed AccessDenied |
| A delegated JSON reply came back in a ```json fence | `JSON.parse` fails on a reply that is otherwise perfect |

The fence is the interesting one. The prompt forbids fences and that works on a
plain device query — but after summarising a specialist's prose the model is in
chat-formatting mode and a fence is what that produces. `unfence_json` strips it,
which is the same conclusion the `⟦A2A:…⟧` marker reached: if a property must hold
for every reply, the harness enforces it rather than the prompt asking.

---

## Addendum, 2026-08-11: the dashboard's long ranges

Widening the Overview dashboard to 60d/90d was expected to need paged reads. It
did not, and the batching attempt is recorded here because the numbers are the
whole argument.

| Window | Single query | Records scanned |
|---|---|---|
| 7d | 1.8s | 327,845 |
| 30d | 3.2s | 798,232 |
| 60d | 4.1s | 831,357 |
| **90d** | **3.2s** | **865,019** |

Budget is `SPANS_QUERY_BUDGET_SECONDS = 22` (API Gateway's 29s integration
timeout is hard). So 90 days runs at **7× inside budget**, and scanning grows
**8%** from 30d to 90d rather than the 3× a naive reading of the window would
predict — the older weeks simply hold little data.

**Chunking, measured and rejected.** 18 × 5d in parallel: wall 2.6s, i.e. 0.6s
faster, with two failure modes reproduced against the live account:

```
d-90..d-85  with aws/spans → 400 MalformedQueryException
d-90..d-85  runtime only   → Complete, 0 rows
d-90..now   with aws/spans → Complete, 16 rows   ← same span as ONE window

d-30..d-25  without aws/spans → 0 rows
d-30..d-25  with aws/spans    → 1 row (2026-07-14)   ← 1 real day lost, silently
```

A chunk lying entirely outside a group's retention is a hard 400; the obvious
guard against that drops `aws/spans` from older chunks and silently loses days
held only there. `tests/test_dashboard_ranges.py` now asserts one `start_query`
per query regardless of window length. See design principles §1.9.1.

**Also corrected:** the span-history horizon. Reading the oldest log group's
`creationTime` gives 2026-04-12 on this account — true, but the group held no
smarthome spans until 2026-07-14, and that reading suppressed the UI's caveat in
exactly the case it exists for. `dataFrom` is now derived from the first day that
returned data.

---

## Reproducing

```bash
./venv/bin/python scripts/measure-baseline.py --repeats 3 --label after --compare \
    docs/measurements/s0-baseline-cold-20260811-0436.json
./venv/bin/python scripts/ab-delegation-brief.py
./venv/bin/python scripts/ab-parallel-delegation.py
```

`docs/measurements/README.md` explains which column supports which claim.

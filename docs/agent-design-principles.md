# Agent design principles

Everything here was learned by deploying this system and measuring it. Each
principle cites the code that implements it and the number or bug that produced it.
Where a principle contradicts what we expected, the measurement is given and the
expectation is named — those are the entries worth reading first.

Three chapters: **Harness design** (how the pieces are wired), **Context
engineering** (what reaches the model), **Prompt design** (how the model is
instructed).

> 中文版见 [`agent-design-principles-zh.md`](agent-design-principles-zh.md)。

---

## 1. Harness design

### 1.1 A tool that reaches per-user data must be a factory, never a list

`a2a-agent-registry/common/server.py:316` builds the tools per request from the
verified caller. Building them once at startup pins whichever user arrived first,
and every later request acts as them.

This is the failure mode to internalise: it does not error, it does not log, and
the agent keeps answering fluently. It is a cross-user data leak that looks like a
working system. `agent/agent.py` does the same thing for its MCP tools, closing
over `user_id` so it appears in **no** model-facing signature —
`a2a-agent-registry/common/tests/test_agents_roster.py` parses every `tools.py`
with `ast` and fails if a `@strands_tool` declares one. A parameter the model can
fill is a parameter a prompt injection can fill.

### 1.2 Authorisation belongs on the server, even when the client already filters

`X-A2A-Allowed-Skills` was parsed and ignored for a while. The Admin Console's
per-skill checkboxes trimmed the orchestrator's tool list, so the feature *looked*
enforced — but anything holding the shared m2m token could call any skill on any
agent. `enforce_allowed_skills` (`common/server.py:143`) now refuses, and refuses a
request with **no** header rather than waving it through: an unauthenticated
omission must never be more permissive than an explicit grant.

`smoke_test.py` runs eight negative cases alongside the eight positive ones. A
passing positive test says nothing about whether a control exists.

### 1.3 Refuse *as the agent*, not with a 500

The refusal comes back as ordinary agent text beginning "Request refused: …"
(`common/server.py:_refuse`). The orchestrator shows tool output to its own model,
so a readable refusal it can relay beats an opaque error it can only report as a
malfunction.

### 1.4 Assertions the model could make, the harness should make instead

Every specialist's reply is prefixed server-side with `⟦A2A:<domain>⟧`
(`common/server.py:278`). It began as a prompt instruction, and measured: the same
model emits it reliably when answering from its prompt and drops it after a tool
call, where the last thing in context is a tool result to summarise. Instructing
harder moved nothing.

It also stopped being safe to leave to the prompt once prompts became
admin-editable, because a global override *replaces* the shipped prompt and takes
the instruction with it. **If a fact must be true of every reply, put it in code.**

### 1.5 Latency is not one number — separate what you own from what you rent

`scripts/measure-baseline.py` reports `platform` (`wall - server`) as a first-class
column, because measurement showed **7.1s of a 24.3s mean turn is spent in
AgentCore before our container is entered**. A session id the runtime has never
seen costs ~7s; a reused one ~0.4s.

So a "16s fast path" is about 8s of agent work behind 8s of platform session
creation, and any report quoting wall alone credits the platform's cold start to
the harness — in both directions. `--compare` **refuses** to diff a warm run
against a cold one, because that difference alone is ~7s of apparent improvement
that no code change produced.

### 1.6 An instrument needs its own correctness check

`_impossible()` (`measure-baseline.py:380`) rejects any row where the container
claims more time than the client waited, and refuses to archive the run.

It exists because the warm-mode turn boundary was wrong twice. Using the client
clock with 2s of slack, each turn absorbed its successors' spans — turns run
back-to-back with no idle gap. That did not fail: it produced a plausible,
monotonically decaying table that read as a result. The fix was to bound each turn
by its own `POST /invocations` span. **A measurement that cannot be wrong loudly
will eventually be wrong quietly.**

### 1.7 Concurrency the framework already has, the transport must not block

Strands issues independent tool calls concurrently — measured, two tools starting
within 0.00s of each other on separate threads. The A2A transport kept one event
loop and drove it with `run_until_complete`, which only the loop's owning thread
may call. The **third** concurrent delegation raised `RuntimeError: This event loop
is already running`, which was caught, counted as an endpoint failure, and shown to
the user as "A2A agent call failed" — blaming a healthy specialist and tripping its
circuit breaker after three.

`_run_on_loop` (`agent/tools/a2a.py:408`) submits with
`run_coroutine_threadsafe` to a loop on its own thread. **Measured: 51.1s → 17.2s
(-66%)** on a three-domain request (`scripts/ab-parallel-delegation.py`).

### 1.8 Tiered timeouts, and a breaker that distinguishes "unavailable" from "wrong"

Connect 5s, read 55s (`a2a.py`): an unreachable agent should fail in a second while
one that is thinking has an LLM turn behind it. A single 60s number cannot express
both. After the breaker opens, exactly **one** probe is let through rather than a
full reset, so a still-broken endpoint re-opens on its next failure. A breaker skip
returns "A2A agent unavailable" rather than "call failed", so the model says the
specialist was never asked instead of implying it answered badly.

### 1.9 Measure before optimising — two of our four latency phases were wrong

| Phase | Expected | Measured |
|---|---|---|
| S2 context trimming | remove a round trip | **-1.64s (-10%)**, confirmed |
| S3 prompt caching | "up to 85% latency" (AWS docs) | **2% latency** (noise), **98% tokens** |
| S4 parallel delegation | needs building | already concurrent; the **transport** was broken |
| S4 prewarming | remove a cold start | **~0.3s** after 100 min idle — nothing to win |
| S5 stream the A2A hop | TTFT 30s → single digits | **impossible**; prose can't precede the tool result |
| chunk the 90d spans query | long window needs slicing | **3.2s of a 22s budget**; nothing to win |

Prompt caching cuts billed input tokens from 15,839 to 329 and is worth having —
but it is a **cost** optimisation, and calling it a latency one would have been a
claim the numbers do not support. Prewarming was dropped outright.

The last row is the cheapest lesson on the list: widening the dashboard to 90 days
looked like it needed paged reads, and one measurement said the window was already
7× inside budget and that scanning grows 8% from 30d, not 3×. **The work that a
measurement deletes is the highest-return measurement you can make.**

### 1.9.1 A guard against a loud failure can be a silent one

Chunking that query was also *tried*, and it is worth recording why it lost. It
saved 0.6s and introduced two failure modes, both reproduced against the live
account:

1. A chunk lying entirely outside a log group's retention is a **hard 400**, not
   an empty result. `aws/spans` keeps 30 rolling days, so every older chunk fails
   outright — while one wide window is fine, because it overlaps retention and
   CloudWatch clips it itself.
2. Guarding against (1) by dropping `aws/spans` from the older chunks **silently
   loses days held only there.** Measured: a `d-30..d-25` chunk returned one real
   day with the group and zero without it, no error either way.

The second is the one that matters. A loud failure invited a guard, and the guard
turned it into a quiet one — losing the *oldest* data, which is exactly what a
long range exists to show. Trading a 400 for missing rows is only an improvement
if you never look at the rows.

The countermeasure is a test that pins the decision rather than the code: one
`start_query` per query regardless of window length. A future "optimisation" back
into slices now fails a test instead of a dashboard.

### 1.10 One shared memory, and only one writer

All eight specialists read the user's AgentCore Memory
(`common/memory.py:153`) under actor-partitioned namespaces with no agent
dimension: "prefers warm light" is a fact about the *user*, not about whichever
agent heard it. Writing stays the orchestrator's alone, because only it holds the
conversation — a specialist sees one self-contained instruction, so anything it
wrote would return as a context-free half-sentence forever, and eight concurrent
writers would hand the summarizer an interleaved transcript of a conversation none
of them had.

Enforced by the IAM grant, not by intent: `A2ASharedMemoryRead` allows
`RetrieveMemoryRecords` and nothing else, so a future edit that tried to write
fails instead of quietly poisoning the memory. **Make the wrong thing impossible,
not merely undone.**

### 1.11 The identity must survive every hop, verified at each one

The m2m token in `Authorization` proves *a service* is calling and has no `sub`. The
end user rides in `X-SuperApp-User-Token` and the specialist **re-verifies** it
independently (signature via JWKS, issuer, audience, `token_use`, expiry) rather
than trusting the hop, then opens the Gateway with it so Cedar evaluates the real
user. The runtime holds no device permissions of its own.

A custom header is silently stripped unless the Runtime declares
`requestHeaderConfiguration.requestHeaderAllowlist` — the first regression run
failed with "header is missing" on a request that had definitely sent it.

### 1.12 Scheduled actions should be authorised like typed ones

The task-management agent writes scene definitions and returns the device actions
for the **orchestrator** to execute; it holds no device tools. The runner Lambda
executes on schedule *as the owner, through the Gateway*. So a scheduled command is
authorised exactly like a hand-typed one, and there is no second code path where
Cedar does not apply.

### 1.13 Silent success is the failure mode to design against

Nearly every bug in this system's history reported success:

| Bug | What it looked like |
|---|---|
| `UpdateRegistryRecord` shape | redeploy "succeeded" while minting a new recordId and voiding every user's grants |
| spans moved log groups | dashboard read "no data" for six days, as if idle |
| `agentcore deploy` ships a stale copy | deploy succeeded; the feature was simply absent |
| tool docstring beat the system prompt | correct answers, optimisation never happened |
| Cedar policy attach | permissions API returned 200 while the policy stayed inactive |
| IoT topic rule | published messages went nowhere, no error |
| simulated vote distribution | "29 filed, 0 failed" and a CSAT of exactly 5.0/5 — every vote positive |
| feedback sort key led with `ts` | one 👎 plus its reason wrote two rows and counted as two negatives |
| A2A catalog read failure | 200 with `availableAgents: []` — identical to a registry with nothing in it |
| registry wait polled for `ACTIVE` | a status no registry returns, so the wait could only time out and fall through |
| CSS read Cloudscape's hashed vars | the `var()` fallback won, so two panels stayed white in dark mode and looked deliberate |
| dark-only CSS in light mode | 1.32:1 text made 17 working checkboxes look disabled; reported as "tool permissions are broken" |
| episodic memory on a user-scoped namespace | the API accepted it; records were extracted, billed, and written somewhere nothing read |
| chat transcript with no bounded ancestor | `overflow-y: auto` never engaged, so the page grew instead of the message list scrolling |

Every row is the same shape. Take the vote split: it used `i % 100`
against a threshold of `rate * 100` while a persona has 6-8 turns, so `i` never
reached it; the run reported complete success and produced a plausible number
containing none of the per-persona variation that was supposed to produce it. The
only way to notice was to compare the output against the rates. **A number that
looks reasonable is not evidence that it was computed correctly** — check it
against the inputs that should have produced it, not against your expectations of
its shape.

The response is the same each time: **assert the thing you actually want, from
outside the code that claims to do it.** Read spans, not reply text. Validate
against the botocore service model, not the docs. Diff the deployed copy against
the repo.

#### 1.13.1 An ambiguous symptom will be diagnosed wrongly, confidently

The empty-catalog row above cost two wrong diagnoses before the right one. An empty
list inside a 200 is consistent with too many causes — nothing published, wrong
registry id, missing IAM action, stale SDK — so the investigation picks whichever
is most interesting rather than whichever is true. Both chosen causes were written
up as fact in the admin manual, with a remediation plan (a Lambda Layer) for a
problem that did not exist. The real cause was the most boring candidate.

Two habits fall out of this:

**Name the namespace, the version, the account — whatever makes the observation
reproducible.** `GetRegistry` returning `ResourceNotFoundException` looks like
proof that an id is dead. It is equally consistent with a live id queried through
the wrong namespace, which is exactly what happened: GA `agent-registry` and legacy
`bedrock-agentcore` hold disjoint sets of registries, and the same id 404s in the
other one. A observation that cannot distinguish two causes is not evidence for
either.

**Make the failure say which failure it is, at the point a human reads it.** The
fix was not more logging — there was already a warning. It was returning
`catalogError` alongside the empty list so the console can render the reason
instead of the neutral empty state. When two causes produce the same output, the
cheapest permanent fix is usually to make the outputs differ.

---

## 2. Context engineering

### 2.1 Send the answer, not a reason to ask for it

A specialist's first event-loop cycle existed only to call `discover_devices`. The
call is cheap (~0.2s); the LLM turn wrapped around it is not — 1.0-1.3s of the ~7s
the specialist took. The catalog is static, so the orchestrator states the relevant
devices up front (`shared/device_brief.py:234`). **Measured -1.64s (-10%)**,
winning all four A/B pairs.

### 2.2 Trimming means *relevance and shape*, not just truncation

The full discovery payload is ~1,800 tokens. Pasting it in would have removed a
round trip and added 1,800 tokens to every delegated prompt — moving the cost, not
removing it. So the brief filters by relevance (rooms and categories the request
mentions) **and** by shape (one line per device; only the bounds a model cannot
guess — ranges, enum values, segment counts). Result: 99-210 tokens, 5-11% of the
payload it replaces.

### 2.3 Injected context must be labelled as context

Retrieved memory is wrapped in a section that says it is context about the user,
explicitly **not** part of the current request, and that the current request wins
on conflict (`common/memory.py:206`). Unlabelled, those lines read as instructions:
a specialist asked to dim the bedroom would apply a remembered ocean effect because
the prompt appeared to ask for it.

### 2.4 A hint must be overridable, and must say so

The device brief keeps `discover_devices` available and tells the model when to use
it anyway — no list, the device missing, or the list contradicting the request.
Room matching is heuristic; a hint the specialist can override is survivable where
an authority it cannot is not. The brief also states it carries **no live state**,
because it is built from the static catalog and a specialist that assumed otherwise
would report a brightness nobody told it.

### 2.5 Cache what repeats, and know what caching buys

The orchestrator's prefix — system prompt (~1.6k tokens), routing table (~1.7k),
eleven governed skills (~4.3k), ~20 tool schemas — is ~10.5k tokens, identical
every call. With a cache point: **29,644 → 9 billed input tokens** on the live
runtime.

Cache hits need an **exact** prefix match, so static content goes first and
per-request content last. `strategy="auto"` rather than a hardcoded cache point,
because the model is per-user configurable and auto degrades with a warning instead
of failing every turn for a user on an unsupported model.

### 2.6 Not every prefix is worth caching

The specialists deliberately do **not** cache (`common/server.py:_build_strands_agent`).
Their prefixes measure 362-4,211 mean input tokens, minima as low as 71 — below the
model-specific checkpoint minimum, where a cache point is silently ignored (a
2,817-token Haiku call with one returned `cacheRead=0, cacheWrite=0`). And the
prefix varies per request anyway, since the governed override and the user's memory
are appended. Cache **writes** bill at 1.25×, so enabling it there would cost 25%
more per delegation for zero hits.

### 2.7 Share the code that computes a shared key

`shared/memory_actor.py` is one function, copied into every container rather than
reimplemented. Two containers that sanitize the same user differently do not fail —
they each get a working, private, half-empty memory, and the symptom is "the
sub-agent never remembers what I told the main agent", which reads as a retrieval
bug rather than a naming one. The same argument makes `shared/device_catalog.py` the
single source for what a device accepts: a second copy of "what speeds does the fan
take" is a copy that will disagree, and it will disagree by storing a scene the
execution path then refuses.

The principle also holds for a shared **list**, not just shared code, and the
failure there is even quieter. `shared/prompt-examples.json` states what the system
can do; the chatbot renders it as suggestions and the traffic simulator drives demo
conversations from it. Those two lists were maintained separately — TypeScript i18n
keys and Python scenario data — and nothing detects a divergence, because neither
copy is wrong on its own. The symptom is realising mid-demo that no traffic ever
reached the security agent. `shared/tests/test_prompt_examples.py` closes it by
asserting against the *source*: every skill published by every AgentCard must be
named by some example, and no example may name a skill that no longer exists. **If
two places must agree about what exists, derive both from the thing that
defines it and assert the derivation.**

---

## 3. Prompt design

### 3.1 Route by tool name, not by described category

The routing table (`agent/agent.py:A2A_DELEGATION_RULES`) maps a request shape to a
**literal tool name**. It used to describe categories and let the model infer.
`agent/tests/test_delegation_rules.py` derives every valid tool name from the
AgentCards and fails if the prompt routes to one that does not exist — it found two
deployed skills the table never mentioned (`inspect_devices`, `tariff_analysis`) on
its first run.

Without that test a renamed skill leaves the prompt pointing at a missing tool, and
the model falls back to its own knowledge with no error anywhere.

### 3.2 A tool's description outranks the system prompt about that tool

The single most expensive lesson here. S2's brief was added, the system prompts
were rewritten to say "use the list, do not call `discover_devices`", it deployed —
and the specialists kept calling it. The brief was demonstrably arriving (input
tokens rose 2,521 → 3,428) and the new prompt was live.

The cause was `discover_devices`' own docstring, still opening with **"Call this
FIRST, every time."** A tool description is attached to the very tool the model is
deciding about, so it won. Nothing was visible in any reply: the answers stayed
correct and the optimisation just never happened.

`a2a-agent-registry/common/tests/test_discover_guidance.py` now asserts the two
halves agree. **Any prompt change about tool usage must edit the docstring in the
same commit.**

### 3.3 Name the failure, not just the requirement

"Apply the pendingActions" was not enough — the orchestrator reported "movie mode is
running, strip at 20%" after zero `control_device` calls. What worked was naming the
failure explicitly:

> **Receiving `pendingActions` and describing them as done is a failure, not a
> shortcut.** … the lights never changed, and the user finds out by looking at the
> room.

A model that has been told what the wrong answer *looks like* avoids it more
reliably than one told only what the right answer is.

### 3.4 Disambiguate on the axis that actually separates two tools

Two scene specialists exist and only one thing distinguishes them: **now versus
later**. "Make the lights follow the music" is live; "every night at 8, make the
lights follow the music" is a stored routine. The prompt says exactly that, and
says what going wrong looks like — sending a live request to task-management saves
something and changes nothing, which reads as the feature silently not working.

### 3.5 Tell the model the cost model, not just the rules

The prompt states that independent specialists run concurrently, so asking two in
one turn costs about as long as one, and that serialising them doubles the wait for
nothing. Given only "you may call several tools", the model tends to wait for each
reply before asking the next — which is the slow arm of the S4 A/B above.

### 3.6 Route by subject, not by phrasing

"What animation modes does the LED matrix support?" is a documentation question
even though it names a device. "Turn the LED matrix off every night" is an
automation even though turning things off is normally the orchestrator's own job.
Both are stated in the prompt as worked examples, because the surface form points
the wrong way in each case.

### 3.7 Keep single actions local

Delegation costs at least two serial LLM calls. The prompt lists what the
orchestrator must do itself — one device on/off, one state read, one sensor
history, a page navigation. Delegating a light switch would add seconds to buy
nothing, and the measured gap is real: 17.3s fast path against 31.3s delegated.

### 3.8 Prompts are operational state, so they need governance and tests

A specialist's prompt resolves per request from DynamoDB
(`common/governed_prompt.py`): a global override replaces the shipped prompt, a
per-user override appends. Read per request with **no caching** — a TTL would make a
saved edit look ignored for as long as it lasted, which is indistinguishable from
the bug governance removes. Failure is asymmetric on purpose: an unreadable table
falls back to the shipped prompt, because refusing to answer over a throttled
governance read is worse than using the perfectly good prompt in the image.

`cdk/lambda/admin-api/tests/test_prompt_defaults_mirror.py` fails when a shipped
prompt changes without regenerating the console's mirror, so the "reset to default"
button cannot drift from what is deployed.

---

## Reproducing the numbers

```bash
# Latency + tokens + routing, 10 fixed prompts, archived for comparison
./venv/bin/python scripts/measure-baseline.py --repeats 3 --label baseline
./venv/bin/python scripts/measure-baseline.py --repeats 3 --label baseline --warm

# Per-optimisation A/B against the deployed system
./venv/bin/python scripts/ab-delegation-brief.py       # S2
./venv/bin/python scripts/ab-parallel-delegation.py    # S4

# Where each request actually routed, from spans
./venv/bin/python scripts/probe-routing.py
```

The columns, and which ones may be quoted for which claim:

| Column | What it is |
|---|---|
| `wall` | The client's round trip. What a user experiences. |
| `server` | The container's own `POST /invocations` span. |
| `platform` | `wall - server`. Time in AgentCore, before our code runs. |
| `llmTime` | Every `chat` span summed. |
| `toolTime` | Every `execute_tool` span summed — includes the whole A2A hop. |
| `harness` | `server - llmTime - toolTime`. Our own overhead inside the container. |
| `ttftFirst` | Time to first token of the turn's FIRST model call. |
| `tools` | From `gen_ai.tool.name`, the only direct record of what actually ran. |

Quote `wall` for what a user feels and `server` for what this codebase controls;
attributing `platform` to the harness is how AgentCore's own session creation
(~7s on a cold session, ~0.4s reused) gets charged to code that did not cause it.

Runs are written to `docs/measurements/`, which is **gitignored**: a baseline is
only comparable against another baseline from the same deployment, so the archive
is local to whoever measured. Take your own before quoting a delta.

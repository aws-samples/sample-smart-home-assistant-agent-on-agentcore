# Latency and cost measurements

Archived output of `scripts/measure-baseline.py`. One JSON file per run; each is a
complete record of ten fixed prompts against the deployed orchestrator, so two
files taken weeks apart are directly comparable.

```bash
# take a baseline
./venv/bin/python scripts/measure-baseline.py --repeats 3 --label s0-baseline-cold
./venv/bin/python scripts/measure-baseline.py --repeats 3 --label s0-baseline --warm

# after a change, compare against one
./venv/bin/python scripts/measure-baseline.py --repeats 3 --label after-s2 \
    --compare docs/measurements/s0-baseline-cold-20260811-0436.json
```

## Why this exists

Spec 5 asks for four latency optimisations that all move the same number. Without
a fixed method, three of them cannot be told apart afterwards — only claimed.
Every improvement in this repo is expected to cite a before and an after file
here.

## Read the columns before quoting a number

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

**`platform` is the one that surprises people.** A cold session — a session id the
runtime has never seen — spends about 7s in AgentCore before the container is
entered. A reused session spends about 0.4s. So a "16s fast path" is roughly 8s of
agent work behind 8s of session creation, and no amount of prompt tuning will
touch the second half.

That is why runs come in two flavours and why `--compare` refuses to diff a warm
run against a cold one: the session-creation difference alone is around 7s, which
would read as a large improvement that no code change produced.

## Cold vs warm

| | Cold (`--repeats N`) | Warm (`--warm`) |
|---|---|---|
| Session | Fresh per prompt | One shared session |
| Answers | The user's first message | Their second and later |
| `platform` | ~7s every turn | ~7s once, then ~0.4s |
| Use it for | The honest worst case | Isolating harness and model work |

Both are true. Quote the one that matches the claim being made.

## Baseline, 2026-08-11

`s0-baseline-cold-20260811-0436.json` — 10 prompts x 3 repeats, cold sessions,
Claude Opus 4.6 as the per-user model override.

| Group | wall | platform | server | llm | tool | harness | in tok | out tok |
|---|---|---|---|---|---|---|---|---|
| fast (15) | 17.3s | 7.2s | 10.0s | 5.9s | 1.3s | 2.9s | 21,212 | 185 |
| delegated (15) | 31.3s | 7.0s | 24.3s | 10.0s | 11.5s | 2.9s | 22,958 | 393 |

Routing: 15/15 delegating prompts reached an `a2a_*` tool.

Run-to-run spread, which sets the bar for a later claim: delegated `wall` varies
13%, fast `wall` 30%. **An improvement under about 15% on the fast path is not
distinguishable from noise at three repeats.** The fast path is the noisier of the
two because its turns are short enough for one slow model call to dominate.

What the split says about where the remaining time goes:

- **Delegation costs 14s of wall**, and 11.5s of that is `toolTime` — the A2A hop
  itself, which is a whole second agent turn behind an HTTP call. That is the
  target for S2 (fewer round trips inside the specialist) and S5 (stream it, so
  the user is not staring at nothing for 24s).
- **`harness` is 2.9s and flat** across both groups. It is real (prompt assembly,
  skill loading, the MCP handshake, memory retrieval, seven Registry reads to
  build the A2A tools) but it is not where the seconds are.
- **Input tokens are ~21k on every turn**, fast or delegated, which is the system
  prompt plus tool schemas rather than anything about the request. That is the S3
  target, and it is worth noting the fast path pays it too.

## Prompt set

Ten prompts, five each side of the routing decision, all **read-only** — a
measurement run happens repeatedly and unattended, and a prompt that switched a
light on would change the user's house three times per run. The five delegating
prompts deliberately hit five different specialists so that no single agent's
latency dominates the delegated mean.

Changing `PROMPTS` in the script invalidates every archive here for comparison
purposes. Add a new label instead.

## Where the spans come from

`/aws/bedrock-agentcore/runtimes/{runtimeId}-DEFAULT`, stream `spans` — **not**
`aws/spans`. AgentCore moved trace export per-runtime on 2026-08-05 and the
cutover was silent; see `LEGACY_SPANS_LOG_GROUP` in
`cdk/lambda/admin-api/dashboard.py` for the full story and what it broke.

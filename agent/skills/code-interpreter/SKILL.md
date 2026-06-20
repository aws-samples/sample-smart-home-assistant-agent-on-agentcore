---
name: code-interpreter
description: Write and run real Python in a secure sandbox to answer any smart-home question that is better solved by *computing* than by reasoning — energy/telemetry data analysis, optimization (e.g. price-aware HVAC schedules), statistical anomaly detection, Monte-Carlo cost simulation, charting. Use this skill WITHOUT waiting for the user to say "run code" — if the answer needs aggregation, math, a simulation, or a chart over device data, call `execute_python`.
allowed-tools: execute_python
---

# Code Interpreter

You have a secure Amazon Bedrock AgentCore **Code Interpreter** sandbox available
through the `execute_python` tool. It runs Python with the scientific stack
pre-installed (`pandas`, `numpy`, `matplotlib`, `scipy`, ...). State persists
between calls within a turn, so you can build up an analysis across several
blocks. The user watches each block run **live** in the CodeInterpreter side
panel — your code, its streaming output, and any charts you generate.

Call `execute_python` on your own initiative whenever the answer is better
*computed* than reasoned, **even if the user doesn't mention code**.

## When to call (call `execute_python` for any of these without asking)

- "Analyze my home's energy use this week and chart the trend." → load/synthesize
  the telemetry into a pandas DataFrame, aggregate, plot with matplotlib.
- "What's the cheapest way to run my AC given time-of-use pricing?" → set up the
  price curve and comfort constraints, compute an optimal schedule, report the
  savings and plot it.
- "Are there anomalies in my fan's power readings?" → run Z-score / rolling-stats
  anomaly detection over the series and plot the flagged windows.
- "How much might my electricity bill vary next month?" → Monte-Carlo simulate the
  bill distribution under a usage policy and plot the histogram + confidence band.
- Any request involving aggregation, statistics, optimization, simulation,
  forecasting, or "chart / plot / graph this".

## How to call

- `execute_python(code=<python>, title=<short human label>)`. The `title` is shown
  as the step header in the panel (e.g. "Aggregate daily energy", "Plot savings").
- Break a non-trivial analysis into a few focused blocks — load/prepare, compute,
  then plot — calling `execute_python` once per block. State (variables, imports,
  DataFrames) carries over between calls, so later blocks can use earlier results.
- To produce a chart, use matplotlib and **save it to a file** (e.g.
  `plt.savefig("chart.png")`). Saved images are surfaced inline in the panel and
  in the Files tab. Always label axes and add a title.
- When you lack real device data, **synthesize a realistic dataset in code**
  (clearly noted as synthetic) so the demonstration still runs end-to-end. State
  in your reply that the data was synthesized.
- `print(...)` anything you want the user to see as text output.

## After the tool returns

- The tool returns a short text summary (stdout tail, chart count). Paraphrase the
  actual computed result in your reply — do **not** invent numbers that weren't in
  the output.
- If the tool returns an error (non-zero exit / traceback), read the error, fix the
  code, and call `execute_python` again. Do not fabricate a result.

## When NOT to call

- Device control / cooking / LED commands — use the dedicated device-control skills.
- Live website lookups — use `browse_web`.
- Enterprise document lookups — use `query_knowledge_base`.
- Simple facts or chit-chat you can answer directly — no sandbox needed.

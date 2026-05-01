# Vision latency results

Timing: click-send → first non-empty agent bubble (end-to-end,
including payload upload, session-storage write, model call, memory write, and DOM render).
Every iteration runs on a cold runtime session (StopRuntimeSession + fresh browser context).

| Model | Images | N | p50 ms | p90 ms | p99 ms | mean ms | stdev |
|-------|--------|---|--------|--------|--------|---------|-------|
| haiku | 1 | 30 | 3306 | 3791 | 4508 | 3389 | 369 |
| haiku | 2 | 30 | 4538 | 4893 | 5321 | 4498 | 364 |
| haiku | 3 | 30 | 5440 | 5771 | 5885 | 5323 | 433 |
| nova | 1 | 30 | 2016 | 2166 | 2383 | 2032 | 135 |
| nova | 2 | 30 | 2665 | 3063 | 3346 | 2708 | 263 |
| nova | 3 | 30 | 3239 | 3656 | 3684 | 3249 | 251 |

## Haiku vs Nova Lite — median click-to-reply

| Images | Haiku p50 | Nova p50 | Δ (ms) | Nova speedup |
|--------|-----------|----------|--------|--------------|
| 1 | 3306 | 2016 | +1290 | 1.64x |
| 2 | 4538 | 2665 | +1873 | 1.70x |
| 3 | 5440 | 3239 | +2200 | 1.68x |

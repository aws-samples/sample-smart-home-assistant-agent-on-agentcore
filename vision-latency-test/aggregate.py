#!/usr/bin/env python3
"""Summarize results/*.jsonl into a comparison table.

Produces:
  - stdout: a compact per-cell table.
  - summary.md: same table in markdown, for committing to the repo as a
                human-readable report.
"""
import glob
import json
import os
import statistics
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_MD = os.path.join(HERE, "test_result_summary.md")


def quantile(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def main():
    rows = defaultdict(list)
    for path in sorted(glob.glob(os.path.join(HERE, "results", "*.jsonl"))):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if not r.get("ok"):
                    continue
                # Skip dry-run / smoke cells from the report.
                if r.get("model") in ("smoke", "unknown"):
                    continue
                rows[(r["model"], r["image_count"])].append(r["click_to_reply_ms"])

    if not rows:
        print("No data in results/")
        sys.exit(0)

    lines = []
    lines.append("| Model | Images | N | p50 ms | p90 ms | p99 ms | mean ms | stdev |")
    lines.append("|-------|--------|---|--------|--------|--------|---------|-------|")
    for (model, count) in sorted(rows.keys()):
        xs = rows[(model, count)]
        p50 = int(quantile(xs, 0.5))
        p90 = int(quantile(xs, 0.9))
        p99 = int(quantile(xs, 0.99))
        mean = int(statistics.mean(xs))
        stdev = int(statistics.stdev(xs)) if len(xs) >= 2 else 0
        lines.append(f"| {model} | {count} | {len(xs)} | {p50} | {p90} | {p99} | {mean} | {stdev} |")

    # Side-by-side comparison (Haiku vs Nova), if both are present.
    cmp_lines = []
    haiku_map = {c: rows[("haiku", c)] for (m, c) in rows if m == "haiku"}
    nova_map = {c: rows[("nova", c)] for (m, c) in rows if m == "nova"}
    if haiku_map and nova_map:
        cmp_lines.append("")
        cmp_lines.append("## Haiku vs Nova Lite — median click-to-reply")
        cmp_lines.append("")
        cmp_lines.append("| Images | Haiku p50 | Nova p50 | Δ (ms) | Nova speedup |")
        cmp_lines.append("|--------|-----------|----------|--------|--------------|")
        for count in sorted(set(haiku_map) | set(nova_map)):
            if count not in haiku_map or count not in nova_map:
                continue
            hp = quantile(haiku_map[count], 0.5)
            np_ = quantile(nova_map[count], 0.5)
            delta = int(hp - np_)
            speedup = hp / np_ if np_ else None
            cmp_lines.append(f"| {count} | {int(hp)} | {int(np_)} | {delta:+} | {speedup:.2f}x |" if speedup else "")

    md = "# Vision latency results\n\n"
    md += "Timing: click-send → first non-empty agent bubble (end-to-end,\n"
    md += "including payload upload, session-storage write, model call, memory write, and DOM render).\n"
    md += "Every iteration runs on a cold runtime session (StopRuntimeSession + fresh browser context).\n\n"
    md += "\n".join(lines) + "\n"
    md += "\n".join(cmp_lines) + "\n"
    with open(OUT_MD, "w") as f:
        f.write(md)
    print("\n".join(lines))
    print("\n".join(cmp_lines))
    print(f"\nSaved: {OUT_MD}")


if __name__ == "__main__":
    main()

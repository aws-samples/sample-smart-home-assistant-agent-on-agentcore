#!/usr/bin/env python3
"""
Aggregate one session-cold JSONL file into a markdown report.

Usage:
  aggregate.py path/to/session-cold-YYYYMMDD-HHMMSS.jsonl > report.md
"""

import json
import statistics
import sys
from pathlib import Path


# Both probes share the voice-click phases. Fresh-login adds per-round
# runtime update + page_load + login + warmup metrics. We list them in
# journey order and skip any that are missing from the input file.
PHASES = [
    ("stop_sessions_ms", "1) stop_session 耗时"),
    ("update_runtime_ms", "2) UpdateRuntime + wait READY"),
    ("page_load_ms", "3) 打开 chatbot 页面"),
    ("login_ms", "4) Cognito 登录 + 渲染"),
    ("warmup_text_ms", "5) Text runtime warmup POST"),
    ("warmup_voice_ms", "6) Voice runtime warmup POST"),
    ("click_to_ws_create_ms", "7) 点击→WS 创建"),
    ("ws_handshake_ms", "8) WS 握手 (TCP+TLS+101)"),
    ("ws_to_first_frame_ms", "9) 101→首帧 (ready)"),
    ("ws_to_first_audio_ms", "10) 101→首 welcome audio"),
    ("total_click_to_first_frame_ms", "总 A: 点击→首帧"),
    ("total_click_to_first_audio_ms", "总 B: 点击→首 audio"),
    ("total_login_to_first_audio_ms", "总 C: 登录→首 audio"),
]


def stats(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    n = len(values)

    def q(p):
        return values[max(0, min(n - 1, round(p * (n - 1))))]

    return {
        "n": n,
        "p50": q(0.50),
        "p95": q(0.95),
        "min": values[0],
        "max": values[-1],
        "mean": round(statistics.fmean(values), 1),
        "stdev": round(statistics.pstdev(values), 1) if n > 1 else 0,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: aggregate.py <jsonl-path>", file=sys.stderr)
        return 2

    path = Path(sys.argv[1])
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    ok_rows = [r for r in rows if r.get("ok")]

    # Report title: session-cold vs fresh-login (distinguishable by whether
    # update_runtime_ms was captured — only fresh-login spec sets it).
    has_update = any(r.get("update_runtime_ms") is not None for r in ok_rows)
    title = "Fresh-Login Cold" if has_update else "Session-Cold"
    print(f"# Voice Agent {title} 启动延迟报告")
    print()
    print(f"**数据文件**: `{path.name}`")
    print(f"**样本**: N={len(rows)} (ok={len(ok_rows)} fail={len(rows)-len(ok_rows)})")
    print()
    print("单位：毫秒。只展示本次测试实际采样到的阶段；其它留空。")
    print()
    print("| 阶段 | N | P50 | P95 | Min | Max | Mean | Stdev |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for key, label in PHASES:
        s = stats([r.get(key) for r in ok_rows])
        # Suppress phases that never fired — keeps the table compact across
        # both probe variants.
        if s is None:
            continue
        print(
            f"| {label} | {s['n']} | {s['p50']} | {s['p95']} | "
            f"{s['min']} | {s['max']} | {s['mean']} | {s['stdev']} |"
        )

    fails = [r for r in rows if not r.get("ok")]
    if fails:
        print()
        print(f"## 失败轮次 ({len(fails)})")
        print()
        for f in fails[:20]:
            print(f"- `{f.get('run_id')}`: {f.get('error', 'unknown')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

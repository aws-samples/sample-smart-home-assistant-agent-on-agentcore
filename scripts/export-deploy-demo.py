#!/usr/bin/env python3
"""Re-export the AgentCore deploy demo bundle: logs/agentcore-deploy-demo{,.tar.gz}.

    ./venv/bin/python scripts/export-deploy-demo.py
    ./venv/bin/python scripts/export-deploy-demo.py --check   # report drift, write nothing

Why a script. The bundle was assembled by hand once and then went stale in the way
that matters most: it carries COPIES of `a2a-agent-registry/common/*` and
`shared/agent_registry.py`, and a copy of a sub-agent's runtime code is exactly the
thing that must not drift. By 2026-08-15 the copied `common/server.py` imported a
module (`common/a2a_session.py`) the bundle did not contain, so a demo agent built
from it would have crashed on import — with the tarball still looking fine.

`logs/` is gitignored, so the bundle itself is not in version control. What IS in
version control is everything needed to rebuild it: this script, the runbook
(`docs/agentcore-deploy-runbook.md`) and the repo code it copies. The only part that
lives solely in the bundle is the demo-specific code (the air-quality agent's own
`main.py` / `deploy_runtime.py` / `register_record.py` / `teardown.py`, the skill
bundle's `publish_skill.py`, the cards and prompts). This script refuses to write a
bundle if those are missing rather than producing a broken one — recover them from
the existing tarball first.

Layout: one directory IS the payload
------------------------------------
`demo-agent-air-quality/air-quality/` is the container's code root, shipped verbatim;
everything above it is deploy and test tooling that never ships. That split replaced
an enumerated `CODE_PAYLOAD` list, and it earns its keep twice over — the list had
already fallen behind, and the copies below now have exactly one correct destination.

What it refreshes, every run:
  - `air-quality/common/` from a2a-agent-registry/common (tests and caches excluded)
  - `air-quality/memory_actor.py` from shared/memory_actor.py
  - `agent_registry.py` in both bundle ROOTS (deploy-time only) from shared/
  - `demo-config.json` in both bundles, from cdk-outputs.json + agentcore-state.json
  - the runbook and the index README
  - the tarball

And it REFUSES to write any of it unless the payload can import itself with only its
own directory on `sys.path` — see `_check_payload_is_self_contained`. Two shipped
bundles were already broken in exactly that way and both looked perfectly fine.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "logs" / "agentcore-deploy-demo"
TARBALL = REPO / "logs" / "agentcore-deploy-demo.tar.gz"

AGENT_BUNDLE = OUT_DIR / "demo-agent-air-quality"
SKILL_BUNDLE = OUT_DIR / "demo-skill-indoor-air-report"

# The agent bundle has TWO zones, and the split is the bundle's main lesson:
#
#   demo-agent-air-quality/            deploy + test tooling. Never shipped.
#   demo-agent-air-quality/air-quality/  the runtime payload, shipped VERBATIM as the
#                                        container's code root.
#
# `deploy_runtime.py` copies that one directory and nothing else, replacing an
# explicit CODE_PAYLOAD list that had already fallen behind (it omitted
# `memory_actor.py`, so every deployed copy silently resolved no memory namespaces).
AGENT = "air-quality"
PAYLOAD = AGENT_BUNDLE / AGENT

# Files that exist ONLY in the bundle. They are the demo itself; this script copies
# repo code around them and never generates them.
DEMO_ONLY = {
    AGENT_BUNDLE: ["invoke_local.py", "deploy_runtime.py", "register_record.py",
                   "teardown.py", "requirements.txt", "README.md",
                   f"{AGENT}/main.py", f"{AGENT}/card.json",
                   f"{AGENT}/system_prompt.md"],
    SKILL_BUNDLE: ["publish_skill.py", "requirements.txt", "README.md",
                   "skill/SKILL.md"],
}

# Copied straight out of the repo. `common/` is copied wholesale rather than listed,
# so a module added upstream (a2a_session.py was, and its absence would have been an
# ImportError at container start) travels automatically.
#
# Wholesale has a cost that bit once and is now guarded: it OVERWRITES local edits.
# The bundle used to carry a one-line addition to `common/agents.py` (an `air-quality`
# entry in the platform's roster of its own built-ins) and this copy silently dropped
# it, so all three deploy scripts died at import with `KeyError: 'air-quality'`. The
# fix was not to preserve the edit — it was to stop the demo needing a roster entry at
# all, which is what a real third party has to do. `_check_payload_is_self_contained`
# below is what makes that stay true.
COMMON_SRC = REPO / "a2a-agent-registry" / "common"
COMMON_IGNORE = shutil.ignore_patterns("tests", "__pycache__", "*.pyc")

RUNBOOK_SRC = REPO / "docs" / "agentcore-deploy-runbook.md"


def log(msg: str) -> None:
    print(f"  [export] {msg}", flush=True)


def _read_env() -> dict:
    """The public identifiers both bundles need, from this deployment's own outputs.

    None of these are secrets — they are the same values the browser apps ship. Read
    rather than hardcoded so an export against another account is correct by
    construction; a hand-copied pool id is exactly the silent 401 the runbook's A5.5
    exists to catch.
    """
    outputs = json.loads((REPO / "cdk-outputs.json").read_text())
    stack = next(iter(outputs.values()))
    state = json.loads((REPO / "agentcore-state.json").read_text())
    region = "us-west-2"
    return {
        "region": region,
        "registryId": state["registryId"],
        "userPoolId": stack["UserPoolId"],
        "userPoolClientId": stack["UserPoolClientId"],
        "cognitoTokenUrl": (
            f"https://smarthome-{_account_id()}.auth.{region}.amazoncognito.com"
            "/oauth2/token"),
        "scope": "a2a-server/invoke",
        "skillsTableName": "smarthome-skills",
        "adminConsoleUrl": stack["AdminConsoleUrl"],
        "chatbotUrl": stack["ChatbotUrl"],
    }


def _account_id() -> str:
    outputs = json.loads((REPO / "cdk-outputs.json").read_text())
    stack = next(iter(outputs.values()))
    # ChatbotBucketName is `smarthome-chatbot-<account>`; cheaper and more reliable
    # here than an STS call, which would make an offline export impossible.
    return stack["ChatbotBucketName"].rsplit("-", 1)[-1]


CONFIG_COMMENT = (
    "Everything the deploy and register scripts need. Regenerated by "
    "scripts/export-deploy-demo.py from cdk-outputs.json + agentcore-state.json. "
    "None of these are secrets — they are the same public identifiers the browser "
    "apps ship."
)


def _missing_demo_files() -> list[str]:
    missing = []
    for bundle, names in DEMO_ONLY.items():
        for name in names:
            if not (bundle / name).exists():
                missing.append(str((bundle / name).relative_to(OUT_DIR)))
    return missing


def _check_payload_is_self_contained() -> list[str]:
    """Import the runtime payload the way the CONTAINER will, and report failures.

    This is the guard the bundle was missing, and both of the bugs it now catches were
    found by hand instead:

      1. `a2a_session.py` was added to `common/` upstream, `common/server.py` imported
         it, and the bundle did not carry it. Container dead at startup; tarball fine.
      2. `memory_actor.py` sat at the bundle root, outside the shipped payload, so
         `common/memory.py` silently resolved zero namespaces. Nothing errored at all —
         the agent just answered as if the user had no history.

    The check is the honest one: put ONLY the payload directory on `sys.path`, exactly
    as the code root is the only thing importable in the image, and import what
    `main.py` imports. Anything reachable only from the bundle root fails here.

    Run in a subprocess because it mutates `sys.path` and imports strands; doing it
    in-process would both pollute this interpreter and make a second run unreliable.
    """
    probe = (
        "import sys, json, pathlib\n"
        f"root = pathlib.Path({str(PAYLOAD)!r})\n"
        "sys.path.insert(0, str(root))\n"
        "from common.server import run_agent\n"
        "from common import memory, a2a_groups\n"
        "card = json.loads((root / 'card.json').read_text(encoding='utf-8'))\n"
        "name = card['name']\n"
        "ns = memory.namespaces_for('probe_actor')\n"
        "assert len(ns) == 3, f'memory namespaces degraded to {ns!r} — is "
        "memory_actor.py inside the payload?'\n"
        "door = a2a_groups.authorizer_groups(name)\n"
        f"assert door == ['a2a-' + name], f'unexpected door group {{door!r}}'\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=REPO,
                            capture_output=True, text=True)
    if result.returncode == 0:
        return []
    tail = (result.stderr or result.stdout or "").strip().splitlines()
    return [f"payload not self-contained: {line}" for line in tail[-6:]]


def _index_readme(env: dict, commit: str) -> str:
    return f"""# AgentCore 部署演示 — 索引

两条部署链路的可下载演示包 + 操作手册。由 `scripts/export-deploy-demo.py` 导出于
commit `{commit}`, 目标环境是当前这套部署 (us-west-2, registry `{env['registryId']}`)。

| 内容 | 说明 |
| --- | --- |
| [`agentcore-deploy-runbook.md`](agentcore-deploy-runbook.md) | **先看这个。** 两条链路串起来的操作流程、版本号语义、排错表、30 分钟演示时间表 |
| [`demo-agent-air-quality/`](demo-agent-air-quality/) | Flow A: A2A Sub-Agent。本地调试 → `agentcore deploy` → 注册 Registry → 审批 → 核对 authorizer → Admin Console → 给用户授权 |
| [`demo-skill-indoor-air-report/`](demo-skill-indoor-air-report/) | Flow B: Skill。写 `SKILL.md` → 发布 Registry → 审批 → Admin Console → 导入生效 |

每个包内有自己的 README(分步细节)、`demo-config.json`(已填好当前环境)和脚本。
两个包都是自包含的, 解压到任意目录即可运行, 不依赖仓库。

## 30 秒上手

```bash
# Flow A: 本地把子 Agent 跑起来
cd demo-agent-air-quality
pip install -r requirements.txt
AWS_REGION=us-west-2 python main.py          # 终端 1
AWS_REGION=us-west-2 python invoke_local.py  # 终端 2

# Flow B: 先确认基座是活的 (最省时间的前置检查)
cd ../demo-skill-indoor-air-report
pip install -r requirements.txt
python publish_skill.py --list               # 应列出已批准的内建 skill
```

## 四条要点

- **AgentCore CLI 没有 registry 子命令**(0.26/0.27)。Runtime 部署用 CLI, Registry
  注册用包内的 boto3 脚本。
- **注册让你被发现; 你自己 Runtime 的 authorizer 决定谁能调你。** 两个方向都会静默出错
  (谁都调不到 / 谁都能调), 所以 runbook 的 **A5.5** 是一步独立的核对。
- **升版会把已 APPROVED 的记录打回 DRAFT**, 而 Admin Console 和编排 agent 都只认
  APPROVED —— 升版是**有停机窗口**的变更。但授权不会丢: 有重新审批宽限期。
- **`DEPRECATED` 是终态, 且记录会从 API 上消失。** 想可逆地停用请用 `reject`。

导出内容的口径以仓库为准: `common/` 与 `agent_registry.py` 是从
`a2a-agent-registry/common/` 和 `shared/agent_registry.py` 直接复制的, 每次导出刷新。
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="report what would change and write nothing")
    args = ap.parse_args(argv)

    if not OUT_DIR.exists():
        sys.exit(f"{OUT_DIR} does not exist. The demo-specific code lives only in the "
                 f"bundle; extract {TARBALL.name} first, then re-run.")
    missing = _missing_demo_files()
    if missing:
        sys.exit("refusing to write a broken bundle — these demo-only files are "
                 f"missing:\n  " + "\n  ".join(missing) +
                 f"\nRecover them from {TARBALL.name} first.")

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                            capture_output=True, text=True).stdout.strip() or "unknown"
    env = _read_env()

    if args.check:
        # The interesting drift: the copied runtime code, and whether the payload the
        # container gets can still stand on its own.
        stale = []
        for src in sorted(COMMON_SRC.glob("*.py")):
            dst = PAYLOAD / "common" / src.name
            if not dst.exists():
                stale.append(f"MISSING  {AGENT}/common/{src.name}")
            elif dst.read_bytes() != src.read_bytes():
                stale.append(f"STALE    {AGENT}/common/{src.name}")
        for bundle in (AGENT_BUNDLE, SKILL_BUNDLE):
            dst = bundle / "agent_registry.py"
            canonical = (REPO / "shared" / "agent_registry.py").read_bytes()
            if not dst.exists() or dst.read_bytes() != canonical:
                stale.append(f"STALE    {bundle.name}/agent_registry.py")
        actor_dst = PAYLOAD / "memory_actor.py"
        actor_src = (REPO / "shared" / "memory_actor.py").read_bytes()
        if not actor_dst.exists():
            stale.append(f"MISSING  {AGENT}/memory_actor.py")
        elif actor_dst.read_bytes() != actor_src:
            stale.append(f"STALE    {AGENT}/memory_actor.py")
        stale.extend(_check_payload_is_self_contained())
        print("\n".join(stale) if stale else "bundle is current")
        return 1 if stale else 0

    # 1. common/ — wholesale, so a new module cannot be forgotten. INSIDE the payload,
    #    because that is the only directory the container gets.
    dst_common = PAYLOAD / "common"
    if dst_common.exists():
        shutil.rmtree(dst_common)
    shutil.copytree(COMMON_SRC, dst_common, ignore=COMMON_IGNORE)
    names = sorted(p.name for p in dst_common.glob("*.py"))
    log(f"{AGENT}/common/: {len(names)} module(s) -> {', '.join(names)}")

    # 2. The Registry helper, into both bundles' ROOTS. Deploy-time only — the
    #    container never talks to the Registry, so shipping it would only grow the
    #    image and blur what the payload is for.
    for bundle in (AGENT_BUNDLE, SKILL_BUNDLE):
        shutil.copy2(REPO / "shared" / "agent_registry.py",
                     bundle / "agent_registry.py")
    log("agent_registry.py refreshed in both bundle roots (deploy-time only)")

    # 3. The Memory actor rule, INSIDE the payload. `common/memory.py` reaches for it
    #    by path — in a real deployment `deploy.py` copies all of `shared/` next to the
    #    agent code, but a bundle that claims to be self-contained has to carry it.
    #    It used to sit at the bundle root, which is outside what ships, so the
    #    deployed agent silently lost memory: `memory_actor_for` swallows the
    #    ImportError and returns "", every namespace resolves to nothing, and it looks
    #    exactly like a user with no history.
    shutil.copy2(REPO / "shared" / "memory_actor.py", PAYLOAD / "memory_actor.py")
    log(f"memory_actor.py -> {AGENT}/ (inside the shipped payload)")

    # 4. Prove the payload still stands alone, before writing a tarball that says so.
    problems = _check_payload_is_self_contained()
    if problems:
        sys.exit("refusing to write a bundle whose payload cannot import itself:\n  "
                 + "\n  ".join(problems))
    log("payload imports with ONLY its own directory on sys.path")

    # 5. Config, from the live outputs.
    for bundle in (AGENT_BUNDLE, SKILL_BUNDLE):
        keys = (env if bundle is AGENT_BUNDLE
                else {k: env[k] for k in ("region", "registryId", "skillsTableName",
                                          "adminConsoleUrl")})
        (bundle / "demo-config.json").write_text(
            json.dumps({"_comment": CONFIG_COMMENT, **keys},
                       indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"demo-config.json regenerated (registry {env['registryId']})")

    # 6. Docs.
    shutil.copy2(RUNBOOK_SRC, OUT_DIR / "agentcore-deploy-runbook.md")
    (OUT_DIR / "README.md").write_text(_index_readme(env, commit), encoding="utf-8")
    log("runbook + index README written")

    # 7. Tarball. Rebuilt from scratch so a file removed upstream does not survive
    #    inside it, which is how a tarball and a directory drift apart.
    if TARBALL.exists():
        TARBALL.unlink()
    with tarfile.open(TARBALL, "w:gz") as tar:
        tar.add(OUT_DIR, arcname=OUT_DIR.name,
                filter=lambda ti: None if "__pycache__" in ti.name else ti)
    files = sum(1 for _ in OUT_DIR.rglob("*") if _.is_file())
    log(f"{TARBALL.relative_to(REPO)} -> {TARBALL.stat().st_size // 1024} KB, "
        f"{files} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

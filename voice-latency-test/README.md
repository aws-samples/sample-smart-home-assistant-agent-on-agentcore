# Voice Agent Latency Tests

测量 Voice Agent 冷启动延迟。两种 mode，对应不同问题：

| 脚本 | 测什么 | 单轮耗时 | 100 轮总耗时 |
|---|---|---:|---:|
| `run-session-cold.sh` | 登录 1 次后 loop：只 stop_session → click voice。**session-level cold** — 测服务端 worker 启动成本 | ~18s | ~30 min |
| `run-fresh-login.sh` | 每轮：stop_session + UpdateRuntime nonce + wait READY + **新浏览器 + 重新登录** + click voice。端到端用户旅程 | ~60s | ~100 min |

两个 mode 分工：
- 想知道"服务端 worker 启动有多慢"→ session-cold
- 想知道"新用户点进来到听见 welcome 多久"，或验证前端优化（warmup 并行、WS URL 预签）→ fresh-login

## 前置条件

已运行 `./deploy.sh`，根目录存在：
- `../cdk-outputs.json` — 含 `ChatbotUrl`
- `../agentcore-state.json` — 含 `voiceRuntimeArn`
- `../venv/` — 有 boto3

## 一次性 setup

```bash
cd voice-latency-test
npm install
npx playwright install chromium
```

## 运行

```bash
# Session-cold — 快，关注服务端 worker 冷启动
./run-session-cold.sh                      # 100 轮
ROUNDS=50 ./run-session-cold.sh

# Fresh-login — 慢，关注端到端用户旅程
./run-fresh-login.sh                       # 100 轮
ROUNDS=30 ./run-fresh-login.sh
```

都会自动：
- 读 `../cdk-outputs.json` + `../agentcore-state.json` 获取 Chatbot URL + Voice Runtime ARN
- 从 ARN 推断 AWS region
- 激活 `../venv` 让 boto3 sidecar 能用
- 运行 `enable-welcome.py` 打开 welcome clip（`VOICE_WELCOME_ENABLED=1`）
- 跑 Playwright + 生成 markdown 报告

输出：
- `results/session-cold-YYYYMMDD-HHMMSS.{jsonl,md}`
- `results/fresh-login-YYYYMMDD-HHMMSS.{jsonl,md}`

## 阶段指标对照

| 字段 | session-cold 采集？ | fresh-login 采集？ | 含义 |
|---|:---:|:---:|---|
| `stop_sessions_ms` | ✓ | ✓ | 清掉旧 voice session 的耗时 |
| `update_runtime_ms` | — | ✓ | UpdateRuntime + 等待 READY |
| `page_load_ms` | — | ✓ | 打开 chatbot 页面（HTML + JS bundle）|
| `login_ms` | — | ✓ | Cognito sign-in → chat UI 出现 |
| `warmup_text_ms` / `warmup_voice_ms` | — | ✓ | 登录后 chatbot 并行发的两个 warmup POST |
| `click_to_ws_create_ms` | ✓ | ✓ | 按钮点击 → 浏览器发起 WS |
| `ws_handshake_ms` | ✓ | ✓ | WS 从 create 到 101 |
| `ws_to_first_frame_ms` | ✓ | ✓ | 101 → 服务端首帧 (`{"type":"ready"}` sentinel) |
| `ws_to_first_audio_ms` | ✓ | ✓ | 101 → 首个 welcome audio chunk |
| `total_click_to_first_audio_ms` | ✓ | ✓ | 按钮点击 → 第一帧音频（voice 子系统延迟） |
| `total_login_to_first_audio_ms` | — | ✓ | 登录 submit → 第一帧音频（完整新用户体验）|

## 参考基线（2026-04-26，Tokyo，N=100 全成功）

### session-cold

| 阶段 | P50 | P95 |
|---|---:|---:|
| stop_session 耗时 | 683 | 745 |
| 点击→WS 创建 | 101 | 124 |
| WS 握手 | 5707 | 6731 |
| 101→ready | 612 | 696 |
| 101→welcome audio | 843 | 926 |
| **总: 点击→首 audio** | **6647** | **7690** |

## 已实施的延迟优化点

为了把用户感知的点击→首 audio 从最初的 ~7 秒压到 fresh-login 下的 1 秒出头，做了以下优化。全部已合并到 main。

### 前端（chatbot）
1. **登录后并行 warmup** — `ChatInterface.tsx` 用 `Promise.allSettled` 同时 POST `__warmup__` 给 text + voice 两个 runtime，共用一次 idToken / IAM 凭证，把 6 秒的 voice 容器冷启动藏到用户看登录界面的并发窗口里。**最大收益项，约 −5.5 秒**。
2. **WS URL 预签名 + 缓存** — warmup 时顺手 SigV4 预签一个 `wss://.../invocations` URL，存 `presignedWsRef`（4 分钟 TTL）。用户第一次点语音时直接复用，省掉 200-400 ms 的 SigV4 + Identity Pool 取 creds。
3. **AudioWorklet 预拉取** — `useEffect(() => fetch('/pcm-recorder-processor.js'))` 在 ChatInterface 挂载时预拉 worklet JS，用户点语音按钮时走浏览器缓存。
4. **`<link rel="preconnect">` 到 AgentCore 端点** — LoginPage 挂载时就把 TLS 握手和证书验证提前做完，后续 warmup POST 和 WS 连接只需付 RTT。

### 后端 — voice 专用 runtime（voice_agent.py / voice_session.py）
5. **Voice Agent 拆到独立 runtime** — 原来 text + voice 挤在一个 runtime，voice 的 `strands.bidi` + Nova Sonic import 拖慢 text 请求。拆开后两者独立扩缩容，互不影响。
6. **Eager imports at module top** — `voice_agent.py` 在模块级导入 `BidiAgent` / `BidiNovaSonicModel` / `MCPClient`，让 `__warmup__` POST 顺带把所有 heavy import 跑一遍；后续 WS 连接时这些 module 已缓存。
7. **`DISABLE_ADOT=1`** — 禁掉 AWS Distro for OpenTelemetry 的自动插桩（`sitecustomize.initialize()` 要花 100-300 ms）。voice 场景里每事件级 span 价值有限，关掉是净收益。
8. **boto3 client 预热** — `_preheat_boto3_clients()` 在 warmup 时强制初始化 DynamoDB client（`scan` Limit=1）和 Bedrock control-plane client（`list_foundation_models`），分摊 client 的 lazy init 成本。
9. **Welcome 音频内嵌到 container image** — `agent/welcome-zh.mp3` 部署时由 `setup-agentcore.py` 用 Polly 渲染并 bake 进 ECR 镜像；首次连接不需要 S3 round-trip 读音频。
10. **Welcome clip 默认关闭（`VOICE_WELCOME_ENABLED=0`）** — 前端已经有 "连接中" 指示，welcome 音频可选。关掉能让首帧 ready 比首 audio 早出现约 200 ms。
11. **Module-level preload welcome bytes** — 如果 welcome 打开，`_WELCOME_BYTES = open(...).read()` 在模块加载时就读入内存，session 启动时不走 disk I/O。

### 后端 — voice_session 启动路径
12. **DDB + MCP 并行启动** — 原本串行的 3 个 IO 任务（skill 读取 / voice prompt 读取 / MCP `list_tools`）用 `asyncio.gather` 并行，从 ~1 秒压到最慢单项 ~400-600 ms。
13. **`_TranscriptIdTaggingModel` 继承 `BidiNovaSonicModel`** — 把 `completionId` / `generationStage` / `contentId` 直接贴到 transcript 事件上，前端用 `completionId` 做去重（SPECULATIVE + FINAL 合并成一个气泡），避免重复渲染。
14. **移除 `mcp_gateway_arn=`** — 原 `BidiNovaSonicModel(mcp_gateway_arn=gw)` 会让 Nova Sonic 走 runtime IAM 自建 MCP path，跟 BidiAgent 的 tools 参数冲突导致 `tool_result` 丢失。删掉后走统一路径。

### 运维
15. **Session 按 kind 拆分** — DDB 里用 `__session_text__` 和 `__session_voice__` 两个 skillName，redeploy 时 text 和 voice runtime 各自只清自己的 session，互不误伤。
16. **`setup-agentcore.py` 双 runtime 支持** — 自动 create / patch 两个 runtime（`smarthome` 和 `smarthomevoice`），分别打 IAM、env、requestHeaderAllowlist。

## 未做的优化（ROI 低或需要产品决策）
- Cognito 登录 1 秒压缩（需换登录方式或减少 JWT 验证往返）
- 服务端 Python 进程启动（AgentCore 的容器级成本，~5.7 秒，app 层优化上限有限）
- Nova Sonic session 真正开始说话的时间（需要前端发音频，本测试未覆盖）

## 修 bug 笔记

如果 MCP gateway 401（welcome audio 完全收不到），检查：

```python
import boto3
c = boto3.client('bedrock-agentcore-control', region_name='<region>')
rt = c.get_agent_runtime(agentRuntimeId='<id>')
print(rt.get('requestHeaderConfiguration'))
# 期望：{'requestHeaderAllowlist': ['X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken']}
```

如果为 None，用 `enable-welcome.py` 的逻辑补（它已经 round-trip 正确）。

坑：`get_agent_runtime` 返回 `requestHeaderAllowlist` 在 top-level，但
`update_agent_runtime` 需要嵌在 `requestHeaderConfiguration` 下 — 直接
round-trip 会丢掉 allowlist。

## 文件

- `run-session-cold.sh` — session-cold 一键脚本
- `run-fresh-login.sh` — fresh-login 一键脚本
- `voice-cold-session.spec.ts` — session-cold Playwright 测试
- `voice-cold-fresh-login.spec.ts` — fresh-login Playwright 测试
- `force-cold.py` — runtime nonce-bump 工具（fresh-login spec 内部调用；保留作参考）
- `enable-welcome.py` — 保证 VOICE_WELCOME_ENABLED=1 + 正确 round-trip 配置
- `aggregate.py` — JSONL → markdown 聚合（两种 probe 共用）
- `playwright.config.ts` / `package.json`
- `results/` — 测试数据与报告

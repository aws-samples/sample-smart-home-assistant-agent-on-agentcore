# Voice Agent 启动延迟测试 — session-cold vs fresh-login 对比

两种测试模式都衡量 "Voice Agent 从用户触发到听到回应的延迟"，但模拟的用户场景和走过的代码路径不同，得到的数据也代表不同意义。

**脚本位置**：`voice-latency-test/`

---

## TL;DR — 一眼对比

| 维度 | session-cold | fresh-login |
|---|---|---|
| 模拟场景 | 老用户已登录，隔几分钟后再点语音 | 新用户打开 chatbot → 登录 → 立即点语音 |
| 测什么问题 | **服务端 Python worker 冷启动有多慢？** | **端到端用户旅程 + 前端优化是否生效？** |
| 单轮流程 | stop_session → click voice → 测 | stop_session → UpdateRuntime → **新浏览器 + 登录** → click voice → 测 |
| 登录次数 | 1 次（测试开始时）| 100 次（每轮都重新登录）|
| 浏览器 context | 全测试共用 1 个 | 每轮一个全新 context |
| 单轮耗时 | ~18s | ~60s |
| 100 轮总耗时 | ~30 min | ~100 min |
| 关键指标 | WS 握手时长 | 登录→首 audio 总时长 |
| Baseline (Tokyo) | 点击→首 audio **6647 ms** | 点击→首 audio **1066 ms** |

## 流程对比（每轮做的事）

| 步骤 | session-cold | fresh-login |
|---|:---:|:---:|
| 1. `StopRuntimeSession` 杀 DDB `__session_voice__` 里的会话 | ✅ | ✅ |
| 2. `UpdateAgentRuntime` bump nonce + 等 READY | ❌ | ✅ (~13 s)|
| 3. 销毁/新建 browser context | ❌ | ✅ |
| 4. `goto chatbot URL` (HTML + JS bundle) | ❌（只第 1 轮）| ✅ 每轮 |
| 5. Cognito 登录（填表 + submit）| ❌（只第 1 轮）| ✅ 每轮 |
| 6. 等 chatbot 两个 warmup POST 完成 | ❌ | ✅ |
| 7. 点击语音按钮 | ✅ | ✅ |
| 8. 捕获 WS 全过程（CDP 事件）| ✅ | ✅ |
| 9. 关闭语音 + reload 页面 | ✅ reload | ❌ 直接销毁 context |

## 数据链路 + 阶段采样 统一对比

下表按**真实时间顺序**列出每个阶段。两列分别是 session-cold 和 fresh-login 的 P50 实测值（Tokyo, N=100）。某种模式不走这一步则留空白。

| # | 阶段 | 字段 | session-cold P50 | fresh-login P50 | 说明 |
|---|---|---|---:|---:|---|
| 1 | `StopRuntimeSession` 杀旧 session | `stop_sessions_ms` | **683 ms** | **691 ms** | 两种模式都走这一步 |
| 2 | `UpdateAgentRuntime` bump nonce + 等 READY |  `update_runtime_ms` |  | **13211 ms** | 只有 fresh-login 会强制轮换整个 runtime pool |
| 3 | 销毁旧浏览器 context + 新建 | — |  | （秒级内）| 只有 fresh-login 每轮重置浏览器 |
| 4 | `goto chatbot URL`（HTML + JS）| `page_load_ms` |  | **47 ms** | session-cold 只在测试开始 goto 1 次 |
| 5 | Cognito `signIn` + React 渲染 | `login_ms` |  | **985 ms** | session-cold 只在测试开始登录 1 次 |
| 6 | text runtime warmup POST | `warmup_text_ms` |  | **143 ms** | chatbot 登录后并行发的预热请求 |
| 7 | **voice runtime warmup POST** | `warmup_voice_ms` |  | **5938 ms** | **cold 容器启动成本体现在这里** |
| 8 | 点击语音按钮 → `new WebSocket` | `click_to_ws_create_ms` | **101 ms** | **107 ms** | 前端 SigV4 预签、Identity Pool 取 creds |
| 9 | **WS 握手（TCP+TLS+101）** | `ws_handshake_ms` | **5707 ms** | **325 ms** | **两种模式差异最大的指标**（见下方分析）|
| 10 | 101 → 服务端首个 JSON 帧 | `ws_to_first_frame_ms` | **612 ms** | **408 ms** | `handle_voice_session`: accept + ready sentinel |
| 11 | 101 → 首个 welcome audio chunk | `ws_to_first_audio_ms` | **843 ms** | **638 ms** | BidiAgent pipeline 启动后 `_welcome_stream` 送出 |
| 12 | **总 B：点击 → 首 audio** | `total_click_to_first_audio_ms` | **6647 ms** | **1066 ms** | **纯 voice 链路用户感知** |
| 13 | 总 C：登录 → 首 audio | `total_login_to_first_audio_ms` |  | **7925 ms** | 只有 fresh-login 采集 — 新用户完整旅程 |

### 为什么 WS 握手差 5382 ms（第 9 行）？

| | session-cold | fresh-login |
|---|---|---|
| 点击按钮时 pool 状态 | 刚被 `StopRuntimeSession` 清空 → 没有 warm worker | 登录后的 warmup POST 已经**花 6 秒**把 pool 里一个 worker 预热 |
| click 打到的 worker 类型 | cold（从零启动 Python）| warm（立即接 WS）|
| WS 握手主要成本 | Python 进程启动 + import + boto3 init (~5.7 s) | 仅 TCP + TLS + HTTP Upgrade (~0.3 s) |

**关键洞察**：**差出来的 5.4 秒完全是前端"登录后并行发 warmup POST"这个优化的效果** —
它把 6 秒容器冷启动藏到了 Cognito 登录的 1 秒窗口里（并发），用户点语音时 worker 已经热了。

在 fresh-login 列里这 6 秒不是消失，是挪到了第 7 行 `warmup_voice_ms`；用户在登录时等那 1 秒，并行就已经把这 6 秒花掉了。如果用户登录后稍等 2-3 秒再点语音（看看界面、打字），那 6 秒对用户完全透明。

## 两种模式分别回答的问题

### session-cold 回答：
- 容器 / Python worker 从零启动需要多久？（**答案：~5.7 s**）
- 如果纯粹优化服务端 Python 启动时间，上限能省多少？
- 不同 agent 代码版本对启动时间的影响

### fresh-login 回答：
- 新用户打开 chatbot 到听到回应总共要多久？
- 前端的 warmup 优化是否真的在并行处理冷启动？（**答案：是的，节省 ~5.5 s**）
- 如果在登录环节做优化，能节省多少？（**登录本身 1 s 是最大目标**）

## 选择建议

| 目的 | 用哪个 |
|---|---|
| 评估一次 agent 代码改动对冷启动的影响 | session-cold |
| 评估前端 warmup / 预签名 / 预连接优化 | fresh-login |
| 评估部署到新 region 的总延迟 | fresh-login |
| 回归测试（快速验证无退化）| session-cold（更快）|
| 给产品展示"用户感知延迟" | fresh-login |

## 复现

```bash
cd voice-latency-test
npm install && npx playwright install chromium    # 仅首次
./run-session-cold.sh                              # ~30 min
./run-fresh-login.sh                               # ~100 min
```

两个脚本都会自动从根目录的 `cdk-outputs.json` + `agentcore-state.json` 读取当前部署的 Chatbot URL 和 Voice Runtime ARN，不需要手动配置。

## 参考

- `voice-latency-test/README.md` — 脚本使用和阶段指标完整定义
- `voice-latency-test/voice-cold-session.spec.ts` — session-cold 测试逻辑
- `voice-latency-test/voice-cold-fresh-login.spec.ts` — fresh-login 测试逻辑
- `voice-latency-test/results/tokyo-fresh-login-n100-analysis.md` — 近期基线数据分析

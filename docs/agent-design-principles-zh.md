# Agent 设计理念（中文版）

本文每一条都来自把这套系统真正部署起来、并且测量它之后得到的结论。每条都给出实现它
的代码位置（`file:line`）以及产生它的那个数字或那个 bug。

**先读与预期相反的那几条** —— 它们标了「预期」和「实测」。设计文档里最值得看的不是
「我们做了什么」，而是「哪些想当然是错的」。

英文版见 [`agent-design-principles.md`](agent-design-principles.md)，内容一致。

三章：**Harness 设计**（各部分怎么接起来）、**Context 工程**（什么进入模型）、
**Prompt 设计**（怎么指挥模型）。

---

## 1. Harness 设计

### 1.1 会碰到 per-user 数据的 tool 必须是工厂，不能是列表

`a2a-agent-registry/common/server.py:316` 按**请求**从已验证的调用者重建 tools。启动时
建一次并复用，会把第一个到达的用户钉死在后续每一个请求上。

这个失败模式值得记住：**它不报错、不打日志，Agent 照样流畅回答。** 这是一个长得像
「系统正常」的跨用户数据泄漏。`agent/agent.py` 对它的 MCP tools 做同样处理 ——
`user_id` 从闭包里来，**不出现在任何模型可见的签名里**；
`a2a-agent-registry/common/tests/test_agents_roster.py` 用 `ast` 解析每个 `tools.py`，
只要有 `@strands_tool` 声明了它就失败。**模型能填的参数，prompt injection 就能填。**

### 1.2 授权必须在服务端，即使客户端已经过滤过

`X-A2A-Allowed-Skills` 曾经只是解析后就丢掉。Admin Console 的 per-skill 勾选框裁剪了
编排器的 tool 列表，所以这个功能**看起来**是强制的 —— 但任何持有那个共享 m2m token
的东西都能调用任意 Agent 的任意 skill。现在 `enforce_allowed_skills`
（`common/server.py:143`）会拒绝，而且**没带 header 也拒绝**：未认证的「省略」绝不能
比显式授权更宽松。

`smoke_test.py` 跑 8 个正向用例的同时跑 8 个**负向**用例。一个通过的正向测试并不能
说明这个管控是否存在。

### 1.3 拒绝要「以 Agent 的身份」拒绝，不要抛 500

拒绝作为普通 Agent 文本返回，以 `Request refused: …` 开头
（`common/server.py:_refuse`）。编排器会把 tool 的输出交给自己的模型看，所以一句它
能读懂、能转述的拒绝，胜过一个它只能报告为「坏了」的 opaque error。

### 1.4 模型可能做到的断言，交给 Harness 去做

每个专家 Agent 的回复都由服务端加上 `⟦A2A:<domain>⟧` 前缀（`common/server.py:278`）。
一开始这是写在 prompt 里的要求，实测结果是：**同一个模型，同一条指令，从 prompt 直接
作答时稳定输出这个标记，做过 tool 调用之后就丢掉** —— 因为此时 context 里最后一段是
要总结的 tool 结果，而不是 system prompt。把指令写得更重没有任何改善。

后来 prompt 变成 admin 可编辑之后，交给 prompt 就更不安全了：一个全局 override 会
**替换**掉出厂 prompt，指令跟着一起消失。**如果某个性质必须对每一条回复都成立，就把它
写进代码。**

### 1.5 延迟不是一个数字 —— 要分清哪部分是你的、哪部分是租来的

`scripts/measure-baseline.py` 把 `platform`（`wall - server`）作为一等列输出，因为实测
发现：**24.3s 平均耗时里有 7.1s 花在 AgentCore 里、还没进入我们的容器。** 一个 runtime
从没见过的 session id 约 7s；复用的约 0.4s。

所以「16s 快路径」其实是约 8s Agent 工作 + 约 8s 平台建会话。任何只报 `wall` 的报告都
在把平台冷启动记到 harness 账上 —— **两个方向都会错**。`--compare` 会**拒绝**用 warm
run 去对比 cold run，因为单这一项差异就有约 7s 的「改善」，而它不是任何代码改出来的。

### 1.6 测量工具本身也需要正确性检查

`_impossible()`（`measure-baseline.py:380`）会拒绝任何「容器声称用掉的时间比客户端等待
的时间还长」的行，并且**拒绝归档这次 run**。

它的存在是因为 warm 模式的 turn 边界错了两次。用客户端时钟加 2s 余量时，每个 turn 会
吸收掉它后面那些 turn 的 span —— turn 是背靠背跑的，中间没有空隙。这**没有报错**：它
产出了一张看起来很像结果的表，数值单调递减、排序合理。修法是用每个 turn 自己的
`POST /invocations` span 来界定边界。**一个不会大声出错的测量，最终一定会安静地出错。**

### 1.7 框架已经有的并发，transport 不能挡住

Strands **本来就**并发发起互不依赖的 tool 调用 —— 实测：两个 tool 的启动时间相差
0.00s，在不同线程上。而 A2A transport 当时持有一个 event loop 并用
`run_until_complete` 驱动它，这个方法只有 loop 的宿主线程能调。于是**第三个**并发委派
抛出 `RuntimeError: This event loop is already running`，被 catch 住、记成 endpoint
失败、然后以「A2A agent call failed」返回给用户 —— 罪名落在一个完全健康的专家 Agent
头上，而且三次之后会打开它的熔断器。

`_run_on_loop`（`agent/tools/a2a.py:408`）改用 `run_coroutine_threadsafe` 提交到一个
跑在自己线程上的 loop。**实测：三域请求 51.1s → 17.2s（-66%）**
（`scripts/ab-parallel-delegation.py`）。

### 1.8 分层超时，以及能区分「没被问」和「答错了」的熔断器

connect 5s、read 55s（`a2a.py`）：连不上的 Agent 应该一秒内失败，而正在思考的 Agent
背后是一次真实的 LLM 调用，需要真实的时间。**一个 60s 表达不了这两件事。** 熔断器打开
后冷却期结束时只放**一个**探测请求过去，而不是完全重置，所以仍然坏着的 endpoint 会在
下一次失败时重新打开。熔断跳过返回的是 `"A2A agent unavailable"` 而不是
`"call failed"`，这样模型会说「这个专家没被问到」，而不是暗示「它答得不好」。

### 1.9 先测量再优化 —— 我们四条延迟优化里有两条方向是错的

| Phase | 预期 | 实测 |
|---|---|---|
| S2 context 裁剪 | 省掉一次 round trip | **-1.64s（-10%）**，成立 |
| S3 prompt caching | 「最高降低 85% 延迟」（AWS 文档） | 延迟 **2%**（噪声内），token **-98%** |
| S4 并行委派 | 需要新建 | **本来就并发**；坏的是 **transport** |
| S4 预热 | 消除冷启动 | 闲置 100 分钟后只慢 **约 0.3s** —— 没东西可省 |
| S5 流式透传 A2A hop | TTFT 30s → 个位数 | **做不到**：正文不可能早于 tool 结果 |
| 把 90d span 查询分片 | 长窗口需要分页读 | **22s 预算里只用 3.2s**；没东西可省 |

Prompt caching 把计费 input token 从 15,839 降到 329，值得做 —— 但它是**成本**优化，
把它说成延迟优化就是在讲数字不支持的结论。预热则被直接作废。

最后一条是这张表里最便宜的一课：把大屏放宽到 90 天看起来需要分页读取，而一次测量说明
窗口本来就在预算内 7 倍余量、扫描量从 30d 到 90d 只涨 8%（不是 3 倍）。
**能让一项工作直接消失的测量，回报率最高。**

### 1.9.1 防「大声失败」的那道 guard，可能自己变成「安静失败」

分片也真的试过，值得记下它为什么输。它省 0.6s，同时引入两个失败模式，两个都在线上复现：

1. 完全落在某个 log group 保留期之外的分片是**硬 400**，不是空结果。`aws/spans` 保留
   30 天滚动窗口，所以更老的分片全部直接失败 —— 而单个宽窗口没事，因为它与保留期有
   重叠，CloudWatch 自己会裁。
2. 为躲开 ① 而在老分片里剔掉 `aws/spans`，会**静默丢掉只存在那里的那些天**。实测：
   `d-30..d-25` 分片带上该组返回 1 天真实数据，不带返回 0 天，两种情况都不报错。

第二条才是关键。一个大声的失败招来一道 guard，而这道 guard 把它变成了安静的失败 ——
而且丢的是**最老**的数据，恰恰是放宽时间范围要看的那一段。**把 400 换成「少了几行」，
只有在你永远不看那几行的前提下才算改进。**

对策是用测试钉住这个**决定**而不是钉住代码：一次查询只允许一个 `start_query`，与窗口
长度无关。以后再有人「优化」成分片，先红的是测试而不是大屏。

**先建测量工具不是流程洁癖。** S2/S3/S4 三条动的是同一个数字，不固定测量方法，改完之后
根本无法归因，只能「声称」改善。

### 1.10 一份共享记忆，只有一个写入方

八个专家 Agent 全部从编排器写入的那一份 AgentCore Memory 读取
（`common/memory.py:153`），命名空间按 actor 分区，**没有 agent 维度** ——
「这个用户偏好暖光」是关于**用户**的事实，而不是关于「碰巧听到这句话的那个 Agent」的。
如果按 Agent 分区，灯效 Agent 就无法使用用户对编排器说过的偏好，而那正是共享的全部
意义。

**写入仍然由编排器独占**，因为只有它持有完整对话。专家 Agent 收到的是一条自包含的委派
指令，它写进去的东西会在之后每一次检索时以「没有上下文的半句话」返回；八个并发写入方
还会让 SUMMARIZATION 策略看到一份「谁都没经历过的、交错的对话记录」。

这条约束由 **IAM 保证，而不是靠自觉**：`A2ASharedMemoryRead` 只授予
`RetrieveMemoryRecords`，所以将来某次改动想写入时会**失败**，而不是安静地污染用户记忆。
**要让错误的做法不可能，而不只是「目前没做」。**

### 1.11 身份要穿过每一跳，并且每一跳都重新验证

`Authorization` 里的 m2m token 证明「一个服务在调用」，它**没有 `sub`**。终端用户走
自己的 header `X-SuperApp-User-Token`，专家 Agent **独立重新验证**它（JWKS 验签、issuer、
audience、`token_use`、过期），而不是信任这一跳；然后用它打开 Gateway，让 Cedar 评估
真实用户。**runtime 自己不持有任何设备权限。**

自定义 header 如果 Runtime 没声明 `requestHeaderConfiguration.requestHeaderAllowlist`
会被**静默丢弃** —— 第一次回归测试就是在一个明明发了这个 header 的请求上报
「header is missing」。

### 1.12 定时执行的动作，授权方式要和手打的一样

task-management Agent 写场景定义，并把设备动作**交还给编排器**执行；它自己不持有设备
tool。runner Lambda 到点后**以 owner 身份、经由 Gateway** 执行。所以一条定时命令的授权
路径和手打的完全一致，**不存在第二条 Cedar 管不到的执行路径**。

### 1.13 要防的不是崩溃，是「静默成功」

这套系统历史上几乎每一个 bug 都报告成功：

| Bug | 它看起来像什么 |
|---|---|
| `UpdateRegistryRecord` 的 shape | redeploy「成功」，同时换掉 recordId 并作废所有用户的 A2A 授权 |
| span 换了 log group | 大屏显示「没有数据」整整六天，像是系统闲置 |
| `agentcore deploy` 打包旧副本 | 部署成功，功能根本不在容器里 |
| tool docstring 压过 system prompt | 答案一直正确，优化从来没发生 |
| Cedar 策略挂载 | 权限 API 返回 200，策略却是 inactive |
| IoT topic rule | 消息发出去没到任何地方，也没有错误 |
| 模拟投票的分布 | 打印「29 filed, 0 failed」，CSAT 恰好 5.0/5 —— 每一票都是好评 |
| 反馈表 sort key 以 `ts` 开头 | 一次 👎 加补充原因写了两行，算作两个负评 |
| A2A 目录读取失败 | 200 且 `availableAgents: []` —— 和「registry 里本来就没有」完全一样 |
| registry 等待轮询 `ACTIVE` | 没有任何 registry 会返回这个状态，于是这个等待只能超时后放行 |
| CSS 引用了 Cloudscape 带哈希的变量 | `var()` 的兜底值生效，两个面板在深色模式下保持白色，看起来像是故意的 |
| 只按深色写的 CSS 跑在浅色模式 | 对比度 1.32:1，17 个可用的勾选框看起来是禁用的，被报成「工具权限坏了」 |
| episodic memory 用了按用户组织的 namespace | API 接受了；记录照抽取、照计费，写到没人读的地方 |
| 聊天记录没有被限高的祖先 | `overflow-y: auto` 从未生效，于是整个页面变长，而不是消息列表内滚 |

每一行都是同一个形状。就拿投票分布来说：它用 `i % 100` 对比 `rate * 100`，而一个 persona
只有 6-8 轮，`i` 永远到不了那个阈值；这次运行报告完全成功，产出一个看起来很合理的数字，
里面却完全没有本该产生它的那些 per-persona 差异。唯一能发现的办法是拿输出去对比那些
比率。**一个看起来合理的数字，不能证明它是算对的** —— 要拿它去对本该产生它的输入，
而不是对你心里「它大概长什么样」的预期。

应对方式每次都一样：**在声称做了这件事的代码之外去断言它。** 读 span 而不是读回复
文本；按 botocore service model 校验而不是按文档；把部署副本和仓库 diff 一遍。

#### 1.13.1 症状一旦有歧义，就一定会被自信地误诊

上表里「A2A 目录读取失败」这一行，在找到真因之前先被误诊了两次。200 里的一个空列表能对应
太多原因 —— 没人发布过、registry id 错了、缺 IAM 权限、SDK 太旧 —— 于是排查会挑那个**最
有意思**的，而不是那个**成立**的。两个被挑中的原因都当作事实写进了管理员手册，还为一个
不存在的问题配了修复方案（打 Lambda Layer）。真因是候选里最平淡的那一个。

由此有两个习惯：

**把 namespace、版本、账号 —— 任何让观察可复现的前提 —— 说出来。** `GetRegistry` 返回
`ResourceNotFoundException` 看着就像「这个 id 已经废了」的铁证。但它同样符合「id 是对的，
只是查错了 namespace」—— 而实际情况正是后者：GA 的 `agent-registry` 和旧的
`bedrock-agentcore` 各持有一套互不可见的 registry，同一个 id 在另一边必然 404。
**一个无法区分两种原因的观察，对任何一种都不构成证据。**

**让失败在人真正会看到的地方说出它是哪一种失败。** 修法不是加日志 —— 本来就有一条
warning。修法是把 `catalogError` 跟空列表一起返回，让控制台渲染原因，而不是那个中性的
空状态。**当两种原因产生同样的输出时，最省事的长期修法通常是让输出不再一样。**

---

## 2. Context 工程

### 2.1 直接给答案，而不是给一个「去问」的理由

专家 Agent 的**第一个** event-loop cycle 唯一作用是调 `discover_devices`。这次调用本身
很便宜（约 0.2s），但包着它的那一圈是一次完整的 LLM turn —— 在专家 Agent 约 7s 的耗时
里占了 1.0-1.3s。设备目录是**静态**的，所以编排器可以直接把相关设备写在委派消息里
（`shared/device_brief.py:234`）。**实测 -1.64s（-10%）**，四对 A/B 全胜。

### 2.2 裁剪指的是**相关性和形状**，不只是截断

完整的 discovery payload 约 1,800 token。直接贴进去会省掉一次 round trip、同时给每个
委派请求的 prompt 加上 1,800 token —— 这是**把成本搬了个地方，不是消除它**，也正是
spec 点名的那个陷阱。所以按相关性筛（请求提到的房间和品类），**并且**按形状裁（一行一个
设备；只保留模型猜不出来的边界 —— 取值范围、枚举值、分段数）。结果 99-210 token，是它
替代的那份 payload 的 5-11%。

### 2.3 注入的 context 必须被标注成 context

检索到的记忆被包在一个小节里，明确写着它是关于用户的背景、**不是**当前请求的一部分，
并且冲突时当前请求优先（`common/memory.py:206`）。**不标注的话它们读起来就是指令**：
一个被要求「把卧室灯调暗」的专家 Agent 会去应用记忆里的海洋灯效，因为 prompt 看起来
就是在这么要求。

### 2.4 提示（hint）必须可以被推翻，而且要明说

设备清单保留了 `discover_devices`，并明确告诉模型什么时候仍然该调它 —— 没有清单、需要
的设备不在清单里、清单和请求矛盾。房间匹配是启发式的；**一个专家 Agent 能推翻的提示是
可接受的，一个它推翻不了的权威不可接受。** 清单还明说自己**不携带实时状态**，因为它是
从静态目录生成的，而一个假设了相反情况的专家 Agent 会报告一个没人告诉过它的亮度值。

### 2.5 缓存重复的部分，并且搞清楚缓存到底买到了什么

编排器的前缀 —— system prompt（约 1.6k token）、路由表（约 1.7k）、11 个受治理的 skill
（约 4.3k）、约 20 个 tool schema —— 共约 10.5k token，每次调用完全一致。加上 cache
point 后：**计费 input token 29,644 → 9**（线上实测）。

缓存命中需要前缀**逐字节一致**，所以静态内容在前、per-request 内容在最后。用
`strategy="auto"` 而不是硬编码 cache point，因为模型是 per-user 可配的：auto 会检查
模型是否支持并在不支持时降级 + 告警，而硬编码会让那个用户的**每一个** turn 都失败。

### 2.6 不是每个前缀都值得缓存

专家 Agent 刻意**不**缓存（`common/server.py:_build_strands_agent`）。它们的前缀实测
362-4,211 平均 input token，最小值低到 71 —— 低于模型自己的 checkpoint 最小值，此时
cache point 会被**静默忽略**（一次 2,817 token 的 Haiku 调用带 cache point，返回
`cacheRead=0, cacheWrite=0`）。而且它们的前缀本来就 per-request 变化，因为受治理的
override 和用户记忆是追加进去的。cache **写入**按 1.25× 计费，所以在这里开启等于每次
委派多花 25%、换来零命中。

### 2.7 计算共享 key 的代码必须共享

`shared/memory_actor.py` 是一个函数，被**复制**进每个容器而不是各写一遍。两个容器如果
对同一个用户做了不同的 sanitize，**不会失败** —— 它们各自得到一份能用的、私有的、只有
一半内容的记忆，症状是「子 Agent 从来不记得我对主 Agent 说过的话」，读起来像检索 bug
而不是命名 bug。同样的道理让 `shared/device_catalog.py` 成为「设备接受什么参数」的唯一
来源：第二份「风扇支持哪些档位」的副本，就是一份**将来一定会不一致**的副本，而且它会以
「存下了一个执行端随后拒绝的场景」的形式表现出来。

这条对共享的**清单**同样成立，而且那里的失败更安静。`shared/prompt-examples.json` 描述
「这套系统能做什么」：Chatbot 把它渲染成示例，流量模拟器用它生成演示对话。这两份清单
以前各自维护（TS 的 i18n key 和 Python 的场景数据），而**没有任何机制能发现它们不一致**
—— 因为单看任何一份都不算错。症状是演示中途才意识到从来没有流量到过安全 Agent。
`shared/tests/test_prompt_examples.py` 的解法是对着**源头**断言：每个 AgentCard 发布的
每个 skill 都必须被某条示例命中，且示例不得指向已不存在的 skill。**如果两个地方必须对
「有什么」达成一致，就让它们都从定义它的那个东西派生，并且把这个派生断言起来。**

---

## 3. Prompt 设计

### 3.1 按 tool 名路由，不要按「描述类别」路由

路由表（`agent/agent.py:A2A_DELEGATION_RULES`）把请求形态映射到**字面的 tool 名**。
它以前是描述类别、让模型自己推断。`agent/tests/test_delegation_rules.py` 从 AgentCard
推导出所有合法 tool 名，只要 prompt 指向一个不存在的就失败 —— **首次运行就抓到两个
已部署但路由表从未提及的 skill**（`inspect_devices`、`tariff_analysis`）。

没有这个测试，一次 skill 改名会让 prompt 指向一个不存在的 tool，模型转而用自己的知识
作答，**而任何地方都不会报错**。

### 3.2 tool 的 description 压得住关于该 tool 的 system prompt

这是本轮代价最高的一课。S2 的设备清单加好了，专家 Agent 的 system prompt 也改成了
「用给你的清单，别调 `discover_devices`」，部署了 —— 专家 Agent 继续调它。清单确实到达了
（子 Agent input token 从 2,521 涨到 3,428），新 prompt 也确实生效了。

原因在 `discover_devices` **自己的 docstring** 里，开头还写着 **"Call this FIRST, every
time."** 一个 tool description 是贴在模型**正要决策的那个 tool** 上的，所以它赢。而且
**回复里完全看不出来**：答案一直是对的，优化从来没发生。

`a2a-agent-registry/common/tests/test_discover_guidance.py` 现在断言这两半必须一致。
**任何关于「怎么用 tool」的 prompt 改动，必须在同一个 commit 里改 docstring。**

### 3.3 点出失败长什么样，而不只是说清要求

「应用 pendingActions」是不够的 —— 编排器在**零次** `control_device` 调用之后报告了
「观影模式已启动，灯带 20%」。真正起作用的是把失败明确命名：

> **收到 `pendingActions` 却把它描述成已完成，是失败，不是抄近路。** ……灯根本没变，
> 用户抬头看一眼房间就知道了。

一个被告知**错误答案长什么样**的模型，比一个只被告知正确答案是什么的模型更可靠地
避开它。

### 3.4 在真正区分两个 tool 的那个维度上做消歧

系统里有两个场景类专家，区分它们的只有一件事：**现在 vs 以后**。「让灯跟着音乐动」是
实时的；「每天晚上 8 点让灯跟着音乐动」是要存下来的例程。prompt 就这么写，并且写清楚
弄错了会怎样 —— 把实时请求发给 task-management 会存下一个东西、什么也不改变，读起来
就像这个功能**安静地不工作**。

### 3.5 告诉模型成本模型，不只是规则

prompt 明确写出：互不依赖的专家 Agent 是并发跑的，所以一个 turn 里问两个的耗时和问一个
差不多，而串起来问会白白让用户多等一倍。如果只说「你可以调多个 tool」，模型倾向于等到
上一个回复再问下一个 —— 那正是 §1.9 里 S4 A/B 的慢那一侧（51.1s）。

### 3.6 按主题路由，不按措辞路由

「LED 矩阵支持哪些动画模式？」是**文档问题**，尽管它点了一个设备的名字。「每天晚上把
LED 矩阵关掉」是**自动化**，尽管「关掉」平时是编排器自己的活。这两个例子都直接写在
prompt 里当范例，因为它们的表层形式都指向错误的方向。

### 3.7 单个动作留在本地

一次委派至少要两次串行 LLM 调用。prompt 列出编排器**必须自己做**的事 —— 单设备开关、
单个状态读取、单个传感器历史、页面跳转。委派一次开灯是花几秒钟买零收益，而且这个差距
是实测的：快路径 17.3s，委派 31.3s。

### 3.8 Prompt 是运行时状态，所以它需要治理和测试

专家 Agent 的 prompt 按请求从 DynamoDB 解析（`common/governed_prompt.py`）：全局
override 替换出厂 prompt，per-user override 追加。**按请求读、不缓存** —— TTL 会让一次
已保存的编辑在 TTL 期间看起来「被忽略了」，而这和治理本来要消除的那个 bug 无法区分。
失败方向是刻意不对称的：表读不出来时回退到出厂 prompt，因为「因为一次被限流的治理读取
而拒绝回答」比「用那份完全可用的、编译进镜像的 prompt」更糟；而一个读得出来的 override
永远优先。

`cdk/lambda/admin-api/tests/test_prompt_defaults_mirror.py` 会在出厂 prompt 改动但没有
重新生成控制台镜像时失败，这样「恢复默认」按钮不会和实际部署的内容脱节。

---

## 复现这些数字

```bash
# 延迟 + token + 路由，10 条固定 prompt，归档以便对比
./venv/bin/python scripts/measure-baseline.py --repeats 3 --label baseline
./venv/bin/python scripts/measure-baseline.py --repeats 3 --label baseline --warm

# 单项优化对线上系统做 A/B
./venv/bin/python scripts/ab-delegation-brief.py       # S2
./venv/bin/python scripts/ab-parallel-delegation.py    # S4

# 每个请求实际路由到哪里（读 span，不读回复文本）
./venv/bin/python scripts/probe-routing.py
```

每一列的含义,以及哪一列可以支撑哪种结论:

| 列 | 含义 |
|---|---|
| `wall` | 客户端往返。用户真实体感。 |
| `server` | 容器自己的 `POST /invocations` span。 |
| `platform` | `wall - server`。花在 AgentCore 里、还没进容器的时间。 |
| `llmTime` | 所有 `chat` span 之和。 |
| `toolTime` | 所有 `execute_tool` span 之和 —— 含整个 A2A 跳。 |
| `harness` | `server - llmTime - toolTime`。容器内我们自己的开销。 |
| `ttftFirst` | 该轮**第一次**模型调用的首 token 时间。 |
| `tools` | 取自 `gen_ai.tool.name`,唯一能直接证明"实际跑了什么"的记录。 |

讲用户体感引 `wall`,讲本仓库能控制的部分引 `server`;把 `platform` 记在 harness 账上,
就是把 AgentCore 自己的建会话耗时(冷 session 约 7s、复用约 0.4s)算成了代码的问题。

每次运行的结果写到 `docs/measurements/`,该目录已 **gitignore** —— 基线只在同一套部署内
可比,所以归档是测量者本地的东西。引用任何 delta 之前,先自己跑一次基线。

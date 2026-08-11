# Smart Home Assistant Agent — Agent Harness 管理平台

> **Agent Harness 管理平台**，以智能家居场景为示例，展示如何在 AWS AgentCore 上构建完整的 Agent 运维管控体系：技能编排、模型选择、工具权限（per-user Cedar 策略）、**企业知识库**、外部集成、会话监控、长期记忆查看和质量评估。

基于 AWS AgentCore Runtime/Memory/Gateway 构建的 AI 智能家居控制系统。用户可以通过聊天机器人用**自然语言文字**或**实时语音对讲**（Nova Sonic 双向流式）控制模拟 IoT 设备（LED 矩阵灯、电饭煲、风扇、烤箱）。管理控制台按 **Discover / Build / Deploy / Assess** 四个阶段组织 15 个页面，覆盖 Agent 全生命周期，其中 Overview 页内置 **Agent 运维统计大屏**（实时健康、Token 成本归因、评估漂移、版本发布状态）。**Skill ERP** 网站让普通用户可以自助发布技能到 **AWS Agent Registry**，审批通过后一键导入到技能目录。

> **实现原理、架构图、协议细节** 请参见 [`docs/architecture-and-design.md`](docs/architecture-and-design.md)。本 README 专注于**部署和使用**。

## 设计理念（先读这一节）

九个 Agent(一个编排器 + 八个 A2A 专家)跑在各自的 AgentCore Runtime 上。真正值得看的
不是拓扑,而是**哪些设计是被实测和线上故障逼出来的**。完整版见
[`docs/agent-design-principles.md`](docs/agent-design-principles.md),每条都配
file:line 与数字;这里只列最反直觉的五条。

**1. 会碰用户数据的 tool 必须是工厂,不能是列表。** 启动时建一次 tool 列表会把第一个
到达的用户钉死在后续每个请求上 —— 不报错、不打日志,Agent 照样流畅回答。这是一个长得
像"系统正常"的跨用户数据泄漏。`common/server.py:316` 按请求重建;`user_id` 从闭包里
来,**不出现在任何模型可见的签名里** —— 模型能填的参数,prompt injection 就能填。

**2. 一半的"性能优化"实测方向是错的。** Spec 5 四条延迟优化,两条被自己的测量推翻:

| 预期 | 实测 |
|---|---|
| Prompt caching 降延迟(AWS 文档:最高 85%) | 延迟 **2%**(噪声内),但 token **降 98%** |
| 并行委派需要新建 | Strands **本来就并发**,坏的是 transport(第三个委派直接崩) |
| 预热能省冷启动 | 闲置 100 分钟后首调只慢 **0.3s** —— 没东西可省,方案作废 |
| 流式透传把 TTFT 从 30s 降到个位数 | **做不到**:模型必须等 tool 返回才能写正文 |

所以先建测量工具(`scripts/measure-baseline.py`)再动手,不是流程洁癖 —— 三条优化互相
影响,不固定测量方法就只能"声称"改善。

**3. 延迟要分清哪部分不是你的。** 24.3s 平均耗时里 **7.1s 花在 AgentCore 里、还没进
容器**:全新 session id 约 7s,复用约 0.4s。所以"16s 快路径"其实是 8s Agent 工作 + 8s
平台建会话。只报 wall 会把平台冷启动记在 harness 账上 —— 两个方向都会错。

**4. tool 的 description 压得住 system prompt。** S2 把设备清单塞进委派消息、prompt
改成"别再调 discover_devices",部署两次都没效果 —— 因为 `discover_devices` 自己的
docstring 还写着 "Call this FIRST, every time"。它贴在模型正要决策的那个 tool 上,所以
它赢。**回复里完全看不出来**:答案一直是对的,优化从来没发生。

**5. 要防的不是崩溃,是"静默成功"。** 这个系统历史上几乎每个 bug 都报告成功:redeploy
"成功"却把所有 A2A 授权作废;大屏"没有数据"整整六天像是系统闲置;`agentcore deploy`
成功但打包的是旧代码。对策每次都一样 —— **在声称做了这件事的代码之外去断言它**:读
span 不读回复文本、按 botocore service model 校验而不是按文档、把部署副本和仓库
diff 一遍。

![architecture](screenshots/architecture.drawio.png)
![chatbot](screenshots/smarthomeassistant-chat.png)
![device simulator](screenshots/smarthomeassistant-devices-v2.png)
![admin console](screenshots/smarthomeassistant-admin.png)

## 前置条件

| 条件 | 版本 | 用途 | 安装方式 |
|------|------|------|---------|
| [Node.js](https://nodejs.org/) | >= 18.x | 构建 React 应用、运行 CDK | [下载安装包](https://nodejs.org/en/download) 或 [nvm](https://github.com/nvm-sh/nvm#installing-and-updating) |
| [npm](https://www.npmjs.com/) | >= 9.x | 包管理 | 随 Node.js 一起安装 |
| [Python 3](https://www.python.org/) | >= 3.12 | AgentCore 部署脚本、Agent 代码 | [下载安装包](https://www.python.org/downloads/) 或系统包管理器 |
| [AWS CLI](https://aws.amazon.com/cli/) | >= 2.x | AWS 凭证配置 | [官方安装指南](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| [agentcore CLI](https://www.npmjs.com/package/@aws/agentcore) | >= 0.13.0 | 部署 AgentCore 资源（Gateway / Runtime / Memory） | `npm install -g @aws/agentcore` · [Starter Toolkit 文档](https://aws.github.io/bedrock-agentcore-starter-toolkit/api-reference/cli.html) |
| [boto3](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html) | >= 1.43.67 | 部署脚本中的 AgentCore / Agent Registry API 调用 | 见下方[快速开始](#快速开始)的 `pip install`（`scripts/01-install-deps.sh` 会自动升级） |
| AWS 账号 | — | 需开通 Bedrock AgentCore、Kimi K2.5 和 Nova Sonic 模型访问权限 | 见下方说明 |

> **agentcore CLI 走 npm，不是 pip。** 早期版本的本文档写的是 `pip install strands-agents-builder`，那个包提供的是 `strands` 命令（一个 Strands 示例 agent），**并不会**安装 `deploy.sh` 所需的 `agentcore`。正确方式是 `npm install -g @aws/agentcore`；`deploy.sh` 启动时会校验版本 >= 0.13.0（该版本修掉了一个会让 `agentcore deploy` 失败的 scaffold-test 回归）。升级用 `npm install -g @aws/agentcore@latest`。

**重要：** 部署前需在 [Bedrock 控制台 > 模型访问](https://console.aws.amazon.com/bedrock/home#/modelaccess) 中申请：
- **Kimi K2.5**（`moonshotai.kimi-k2.5`）用于文字聊天
- **Amazon Nova Sonic**（`amazon.nova-2-sonic-v1:0`）用于语音对讲

### 部署者 IAM 权限

执行 `deploy.sh` 的 IAM 用户/角色需要以下 AWS 服务权限（详细清单和最小 IAM 策略 JSON 见 [docs/architecture-and-design.md §9.1](docs/architecture-and-design.md#91-two-stack-architecture)）：

| 服务 | 用途 |
|------|------|
| CloudFormation / CDK / S3 / CloudFront / Lambda / DynamoDB | 基础资源 |
| Cognito / Cognito Identity | 用户身份、Identity Pool 临时凭证 |
| IoT Core | 设备端点 + Thing |
| Bedrock / S3 Vectors | 知识库向量化 + 检索；Nova Sonic 双向流式推理 |
| Bedrock AgentCore | Gateway、Runtime、Memory、Policy Engine |
| Polly | 预渲染语音欢迎语 |
| IAM / STS / Logs / API Gateway | 角色、身份、日志、管理 API |

---

## 快速开始

```bash
# 1. 配置 AWS 凭证
aws configure

# 2. 安装 agentcore CLI（npm 全局包，deploy.sh 会校验版本 >= 0.13.0）
npm install -g @aws/agentcore
agentcore --version

# 3. 设置 Python 环境
python3 -m venv venv
source venv/bin/activate
pip install strands-agents strands-agents-builder bedrock-agentcore boto3 mcp pyyaml

# 4. 一键部署
./deploy.sh
```

部署完成后，`deploy.sh` 会输出四个前端的 URL（设备模拟器、聊天机器人、管理控制台、Skill ERP）以及默认管理员账号。

### 部署内容概览

`deploy.sh` 是一个薄封装，按顺序运行 `scripts/0[1-7]-*.sh` 7 个脚本。每个脚本开头都会打印自己创建的 AWS 资源，方便调试或只重跑某一步。

| 步骤 | 脚本 | 职责 |
|------|------|------|
| 1 | `01-install-deps.sh` | CDK npm 依赖 + 为 Lambda 打包最新 boto3 |
| 2 | `02-build-frontends.sh` | 构建三个 React 前端产物 |
| 3 | `03-cdk-bootstrap.sh` | `cdk bootstrap`（幂等） |
| 4 | `04-cdk-deploy.sh` | 部署 CDK 堆栈：Cognito、IoT、Lambda、DynamoDB、KB、API Gateway、S3+CloudFront |
| 5 | `05-fix-cognito.sh` | 开启自助注册 + 邮箱自动验证 |
| 6 | `06-deploy-agentcore.sh` | 部署 AgentCore 堆栈：Gateway、Target、Runtime（含预渲染语音欢迎语）、Memory；授权 Cognito 身份池调用 Runtime；停掉旧会话以便新代码立即生效 |
| 7 | `07-seed-skills.sh` | 将 `agent/skills/` 下的内置技能写入 DynamoDB |

> **部分重跑：** 只改了前端 → 重跑 2 + 4；只改了 Agent Python 代码 → 重跑 6；只换了内置技能文件 → 重跑 7。

---

## 使用指南

### 聊天机器人 —— 文字与语音双模式

1. 打开部署输出里的聊天机器人 URL，注册/登录
2. 输入框左侧 🎤 按钮切换语音 / 文字模式
3. **文字模式**：输入即发，Kimi K2.5（或管理员指定的模型）回复
4. **语音模式**：浏览器弹出麦克风授权 → 听到预渲染欢迎语"欢迎使用智能家居设备助手" → 开始语音对话，Nova Sonic 双向流式处理
5. 语音模式下说"把风扇打开到中档"等指令，Agent 会通过 MCP 网关真实触发 IoT 设备命令
6. **浏览器实时预览**（右侧默认折叠的 rail，点击展开）：问 Agent 任何需要查实时网页的问题（"example.com 现在显示什么"、"去淘宝上搜 iPhone 16"、"Amazon 上 100 美元以下耳机排名"），无需手动说"use browse_web"—— skill 描述会让 Kimi 自行调用。右侧 DCV 实时流按 1280×800 渲染（窗口更小时自动出现滚动条），每步截图保存到 Agent 的 `/mnt/workspace/<session>/browser/`，"文件"标签页可下载。任务完成后 AgentCore 会话保持 **15 分钟** 不关，点 **"接管控制"** 就能自己继续浏览/验证码/点筛选，不需要重新触发一次工具。详见 [架构文档 §9.11](docs/architecture-and-design.md#911-browser-use--live-agent-web-automation)
7. **欢迎屏示例提示**：分 5 组（智能设备 / 知识库 / 天气 / 实时网页浏览 / 图片分析），覆盖所有能力，点击即填入输入框

### 管理控制台 —— Agent Harness Control Center

使用部署输出中的管理员凭证登录。登录页也提供 **自助注册** 通道（邮箱即用户名，Cognito 发 6 位验证码验证邮箱）。

> **注册 ≠ 有管理员权限。** 本控制台只对 `admin` 组成员开放。新注册的账号能登录，但会看到"访问被拒绝"，需要联系管理员把你加入 `admin` 组（Admin Console → Build → Identity 页的 `Make Admin`，或 `aws cognito-idp admin-add-user-to-group`）。在此之前可以直接使用**聊天机器人** —— 所有终端用户功能（智能家居对话、设备控制、知识库问答）都不需要管理员权限。注册页和"访问被拒绝"页都给出了聊天机器人的直达链接。

左侧导航按 Agent 生命周期分成四段，共 16 个页面：

| 分段 | 页面 | 能做什么 |
|------|------|---------|
| **Discover** | **Overview** | 产品说明 + 架构图（默认折叠）以及 **Agent 运维统计大屏**（见下节）。三个 Demo 入口已移至侧边栏「演示入口」分组 |
| Discover | **Agents** | **机队总览**：1 主 + 7 子 + 1 语音 + 1 A/B 变体 + 1 Tool，含运行时名、状态、skill 数与实时指标。点进详情页可**逐个 Agent 编辑 system prompt**（保存后下一次请求即生效，不用重新部署容器）。列表由 Runtime ARN + Registry 记录推导，新部署的子 Agent 自动出现 |
| Discover | **Integration Registry** | 工具集成概览 + 从 AWS Agent Registry 读取已批准的 **A2A Agent** 记录（显示名称/端点/能力/发布者） |
| **Build** | **Models** | 设置全局默认 LLM 模型；按用户覆盖文字模型与视觉模型（Kimi、Claude 4.5/4.6、DeepSeek、Qwen、Llama 4、OpenAI GPT 等） |
| Build | **Skills** | 创建/编辑/删除技能（完整 [Agent Skills 规范](https://agentskills.io/specification) 字段）；技能目录文件管理（S3 预签名 URL）；全局 + 按用户覆盖；**从 AWS Agent Registry 导入已批准技能** |
| Build | **Prompt** | 编辑文字/语音 agent 的 system prompt（全局默认 + 按用户追加），运行时叠加拼接 |
| Build | **Tool Policy** | 按用户配置可调用的工具（Cedar 策略）；内置工具与 Gateway 工具并列并用 Badge 区分；ENFORCE / LOG_ONLY 切换。每个 Gateway 工具旁列出**谁在用它** —— 撤掉 `control_device` 会同时停掉聊天指令、定时场景和两个子 Agent |
| Build | **Memories** | 查看每个用户的长期记忆（事实 + 偏好，来自 AgentCore Memory） |
| Build | **Knowledge Base** | 上传文档到企业知识库（PDF、TXT、MD、DOCX、CSV 等）；一键触发 Bedrock KB 向量化同步；按用户隔离 |
| Build | **Identity** | 已注册用户表，**以及全部用户管理**：新增用户、提权/降权、删除（原先在 Overview，已统一收敛到此处；不能对自己降权或删除） |
| **Deploy** | **Instance Type** | 计算实例类型（当前 MicroVM，EC2 规划中） |
| Deploy | **Sessions** | 每次登录的运行时会话列表（用户 / 类型 / 会话 ID / 最近活跃 / 近 7 天 Token，**并标出 token 归属的 agent**）、一键 Stop，以及 **Remote Shell**（在 Runtime 容器里执行 shell 命令，stdout/stderr 流式回传） |
| **Assess** | **Agent Guardrails** | 跳转 AgentCore Evaluator + Bedrock Guardrails 控制台 |
| Assess | **Observability** | 跳转 CloudWatch Gen-AI Observability |
| Assess | **Evaluations** | 跳转 AgentCore Evaluations 控制台 |
| Assess | **Optimization** | AgentCore Optimization：推荐、配置包、目标级 A/B 测试、按用户配置入口环境（entryEnvironment）。优化目标可选**任意已部署的 Agent**（下拉选项来自机队，不是硬编码） |

#### Agent 运维统计大屏（Overview 页内）

面向"统一入口 Super App"管理员的运维视图，按监控大屏布局：顶部一条六信号状态条，下面三行成对面板。架构图默认折叠，打开页面即见运维数据。顶部可切换时间范围（24h / 7d / 30d，作用于全部面板）；成本归因维度（按用户 / 入口环境 / Agent 运行时）位于「Token 成本归因」面板内，因为它只影响该面板。每张图都配表格视图。

> **「按入口环境」不是按客户计费。** 它聚合的是 `tenant_env` 的三种模式（`default` / `ab-bundles` / `ab-targets`），也就是 A/B 分流组之间的成本对比 —— 本项目没有独立的租户实体。真正的按客户归因需要先引入 tenant 实体（如 Cognito 组或 `tenantId` 属性）。

| 指标组 | 数据来源 | 是否真实 |
|--------|---------|---------|
| 实时健康（活跃会话、TTFT P95/P99、错误率、QPS） | `AWS/Bedrock-AgentCore` 指标 + `aws/spans`，跨全部已登记 Runtime 汇总并附每个 Runtime 的分解 | ✅ |
| Token 成本趋势与归因（输入/输出拆分） | `aws/spans` 里的 Strands `chat` span | ✅ Token；❌ 美元成本 |
| 成本预算消耗 | — | ❌ 模拟数据 |
| 评估通过率与漂移 | `Bedrock-AgentCore/Evaluations` | ✅ 单变体；❌ A/B 对比 |
| 活跃版本与发布状态 | Runtime Endpoint/Version + CloudTrail | ✅ 版本；⚠️ 灰度阶段为推导值 |
| 用户满意度（CSAT、赞踩、升级率） | — | ❌ 模拟数据 |

无真实数据来源的卡片会显示 **演示数据** 标记，点开有说明"要变成真实数据需要什么"。几个口径要点：**TTFT 不存在于 CloudWatch 指标中**，只能从 span 属性取；**美元成本无法按用户/Agent 拆分**（Cost Explorer 只到账号级），所以只归因 Token 数量；**灰度阶段没有原生字段**，由 Gateway A/B test 与 `tenant_env` 推导而来；**每个 Runtime 必须显式登记** —— span 与评估指标上的 `service.name` 是精确匹配，大屏聚合的是由 `AGENT_RUNTIME_ARN` + `VOICE_AGENT_RUNTIME_ARN` + `DASHBOARD_EXTRA_RUNTIME_ARNS` 构成的白名单（A2A 部署脚本会自动登记自己）。详见 [`docs/architecture-and-design.md` §9.15](docs/architecture-and-design.md#915-agent-operations-dashboard)。

> 大屏默认是空的 —— 需要真实流量才有数据。用下面的[模拟用户脚本](#生成测试数据模拟真实用户)一条命令生成。

### Skill ERP —— 用户自助发布技能

Skill ERP 是面向**普通终端用户**的技能发布站点（不要求 `admin` 组成员），每个登录用户只能看到和管理自己创建的技能。

1. 打开部署输出里的 Skill ERP URL
2. 用自己的 Cognito 账号注册/登录（与聊天机器人共用账户体系）
3. 点击 "+ 创建技能"，填写名称/描述/指令/允许的工具/许可证/兼容性/元数据（**不支持文件上传** — AWS Agent Registry 的 agentSkills 描述符只承载 SKILL.md + 定义 JSON）
4. 保存后，记录会自动以 `agentSkills` descriptorType 发布到 AWS Agent Registry（`SmartHomeSkillsRegistry`），并自动触发 `SubmitRegistryRecordForApproval`
5. 状态栏会显示 `PENDING / SUBMITTED / APPROVED / REJECTED`，可以随时编辑或删除。**被驳回时，审批人填的原因会直接显示在状态下方** —— 这是作者唯一能收到的反馈
6. 管理员在 **Admin Console → Skills → "Add approved skill from AWS Agent Registry"** 的**待审批队列**里 Approve / Reject（驳回必须填原因），批准后同一个弹窗即可导入技能目录 —— 不再需要去 AWS 控制台

### A2A 专家 Agent（可选，演示用）

`a2a-agent-registry/` 下有 **7 个**独立部署的 A2A (Agent-to-Agent) 专家 agent，演示主 Agent 如何通过标准 A2A 协议委派给专家：

| Agent | Skill | 模型 | 触达设备？ |
|-------|-------|------|-----------|
| `device-control-agent` | 多设备编排、能力消歧 | Haiku 4.5 | ✅ 经 Gateway |
| `light-effect-agent` | 心情/图片 → 灯效 | Haiku 4.5 | ✅ 经 Gateway |
| `knowledge-qa-agent` | 文档问答、故障排查 | Nova Lite | ✅ 知识库 |
| `task-management-agent` | 任务/自动化（触发器+动作） | Haiku 4.5 | ❌ 只规划，见下节 |
| `scene-sync-agent` | 音乐/观影盛宴（实时驱动） | Haiku 4.5 | ✅ 经 Gateway |
| `home-security-agent` | 风险评估、事件响应 | Haiku 4.5 | ❌ 纯建议 |
| `energy-optimization-agent` | 节能测算、电价分析 | Nova Lite | ❌ 纯建议 |
| `appliance-maintenance-agent` | 保养计划、故障诊断 | Nova Lite | ❌ 纯建议 |

**这不是"多几个 agent"而已 —— 关键在于身份没有在委派时丢掉：**

- `Authorization` 头里是共享的 m2m token，它只能证明"某个被授权的服务在调用"，**没有 `sub`**。
- 用户的 idToken 走**单独的 `X-SuperApp-User-Token` 头**，子 Agent **独立重新验签**（JWKS / issuer / audience / 过期），再用它开 Gateway —— 所以 **Cedar 评估的是真实终端用户**。子 Agent 自己没有任何设备权限。
- `X-A2A-Allowed-Skills` 现在是**服务端强制**的。以前它只被解析进 request state 就放过去了，等于 per-skill 授权完全在客户端 —— 任何拿到 m2m token 的人都能调任意 agent 的任意 skill。

其他要点：

- **`./deploy.sh` 不会部署它们** —— 保持基础系统精简。
- 部署方式（依赖 `./deploy.sh` 已跑通）：
  ```bash
  cd a2a-agent-registry
  python deploy.py                              # 全量
  python deploy.py --agent light-effect          # 只部署一个
  python smoke_test.py                           # 8 个 agent + 6 个负向鉴权用例
  ```
- Admin 在 **Admin Console → Users → Manage Permissions → A2A Agents** 区块按用户按 skill 授权；主 Agent 在下一次调用时加载。**未授权的 skill 根本不会注册**，模型看不见也就无法被 prompt injection 诱导去调用。
- **每个子 Agent 的 prompt 可以在 Admin Console → Agents → 详情页单独编辑**，保存后下一次请求即生效，不需要重新部署容器。
- 委派一轮约 30 秒（直接回答约 15 秒）—— A2A 这一跳不走流式，所以主 Agent 在专家答完之前不会输出任何内容。这一点写在运维大屏的 TTFT 说明里，不是藏起来。
- 完整部署流程、测试提示词和逐步演示指南见 [`a2a-agent-registry/README.md`](a2a-agent-registry/README.md)。

### 场景联动与定时自动化

在 chatbot 里说「每天晚上 11 点关灯、风扇调到 1 档」，主 Agent 会委派给场景编排子 Agent，把它存成一个**场景**（触发器 + 设备动作），并由 EventBridge Scheduler 到点执行。

支持五种触发器：

| 类型 | 说明 |
|---|---|
| **时间** | 24 小时制 `HH:MM`，按**用户自己的时区**调度（Identity 页设置；没设过的按 UTC，行为与以前一致）|
| **日出/日落** | 按用户经纬度算出当天时刻，可带偏移（「日落前 30 分钟」）。每晚重算次日时间——太阳时刻每天都在动 |
| **设备状态** | 某个设备变成某个状态 |
| **传感器阈值** | 温度 / 湿度 / PM2.5 / CO₂，必须显式写 above 或 below —— 「高于 26」和「低于 26」是两个相反的场景 |
| **一键指令** | 不会自己触发，只在用户点名时执行（「执行观影模式」）|

日出场景需要用户的经纬度，没有就**拒绝创建并说明去哪里设置** —— 猜一个位置会在错误的时间开灯，而且比拒绝更难被发现。

管理员在 **Admin Console → Assess → 自动化任务** 看所有用户的场景：触发条件、真实的 cron 表达式和时区、以及最近一次是否执行成功。定时场景在 07:30 触发时没有人盯着，所以「最近执行」这一列是区分「能用」和「从来没成功过」的唯一依据。

> **定时执行不是一条绕过管控的后门。** 执行 Lambda 完全没有 IoT 权限：它以场景所属用户的身份过 Gateway → Cedar → `iot-control`，和用户手打指令走的是同一条授权链。所以管理员在 Tool Policy 里撤销某用户的 `control_device` 之后，他的 07:30 自动化也会一起停。
>
> 代价说清楚：以「不在线的用户」身份执行需要一份凭证。实测 `GetWorkloadAccessTokenForUserId` 换出的 token 会被 Gateway 以 401 拒绝（它是 KMS 加密的不透明 token，不是带正确 audience 的 JWT），所以系统存的是 **Cognito refresh token** —— 一份 30 天有效的用户凭证落在了 Secrets Manager 里（专用 KMS 密钥 + 已开启轮换 + 一个用户一个 secret + 只有执行 Lambda 能读 + 绝不写日志）。没有存 token 的用户，其定时场景直接不执行。

设备模拟器里配了三样"道具"给场景用：**虚拟时钟**（最高 3600 倍速，只加速模拟器自身的时间和传感器曲线，**不会**改变 AWS 侧的真实触发时间）、**屏幕同步**（电视背光四个分区跟随程序化画面的四边取色）、**音乐同步**（合成节拍 + 蓝牙 idle → pairing → connected 三态）。

### 音乐盛宴与观影盛宴（实时驱动）

「保存下来以后再跑」和「现在就跑起来」是两件事，由两个子 Agent 分开做，这个拆分本身就是设计：

- `task-management-agent` 有自己的表、**没有任何设备权限**。
- `scene-sync-agent` 有 Gateway 设备工具、**没有表**。

合成一个的话，能写场景的 Agent 就同时有了一条自己的设备通路 —— 而这正是 `shared/scenarios.py` 存在的目的（场景是数据，不是能力）。两边都做不了对方那一半，由主 Agent 串起来，并且**存成一键指令之前会先问用户**。

在 chatbot 里说「让客厅的灯跟着音乐跳起来」：电视背光进入 `music` 同步模式，其余灯具按节奏跑 `chase`。这里有一个天然会静默失败的环节 —— 音乐要走蓝牙音箱，链路没连上灯就不会动，而配对需要一两秒。所以 Agent 会**轮询 `bluetooth` 状态直到它稳定**，并且把「还在配对中」当成一个和成功/失败都不同的答案：

| 状态 | Agent 的回应 |
|---|---|
| `connected` | 驱动灯具，报告盛宴已启动 |
| `pairing` | 说链路还没起来，建议稍后再试 —— **不猜它会往哪边走** |
| `idle` | 明确说没有配对的音箱，请用户去连 —— **绝不报成功** |

猜错的代价是不对称的：把 `pairing` 当失败，是让用户去重连一个两秒后就能用的音箱；把 `idle` 当成功，是让用户对着一屋子不动的灯发愣。

### 添加管理员用户

给自助注册的用户开通管理控制台权限。两种方式：

- **控制台**：Admin Console → Build → **Identity** 页，找到该用户点 `Make Admin`。
- **命令行**：

  ```bash
  aws cognito-idp admin-add-user-to-group \
    --user-pool-id <USER_POOL_ID> \
    --username <EMAIL> \
    --group-name admin
  ```

用户重新登录后即可进入控制台（`admin` 组信息在 idToken 的 `cognito:groups` 声明里，需要重新签发令牌才会生效）。

> **注意**：管理员在 Identity 页新建的用户**不会**自动获得工具权限 —— Cognito 的 PostConfirmation 触发器只在自助注册时触发。这类用户还需要去 **Tool Policy** 页手动授权，并按 [管理员手册 §4.2](docs/admin_manual_管理员使用手册.md) 复核 Cedar 策略状态。

### 生成测试数据（模拟真实用户）

刚部署完，运维大屏和 AgentCore Evaluation 都是空的 —— 它们需要真实流量。这个脚本创建几个测试用户，让它们像真实用户一样和 Agent 对话，覆盖 Agent 的全部功能：

```bash
export SIM_USER_PASSWORD='SomeStrong#Pass1'   # 需满足 Cognito 密码策略

python3 scripts/simulate-users.py setup       # 创建并配置 5 个 persona（幂等）
python3 scripts/simulate-users.py run         # 轻量层，约 3.5 分钟
python3 scripts/simulate-users.py run --heavy # 追加 code-interpreter + browser-use
python3 scripts/simulate-users.py status      # 查看现有模拟用户及其配置
python3 scripts/simulate-users.py teardown --yes
```

5 个 persona 各带不同的模型、租户模式和场景侧重，这样大屏的成本归因图表才会出现多行真实数据、而不是全塞进一个桶：

| persona | 模型 | 租户模式 | 覆盖 |
|---|---|---|---|
| `alice` | Opus 4.6 | default | 四类设备控制、设备发现、一键全开 |
| `bob` | Sonnet 4.6 | default | 企业知识库、天气查询、拒答 |
| `carol` | Haiku 4.5 | ab-bundles | 多轮记忆延续、用户反馈 |
| `dave` | Kimi K2.5 | ab-targets | code-interpreter 数据分析（heavy） |
| `erin` | Sonnet 4.5 | default | browser-use 网页操作（heavy）、拒答、模糊指令 |

测试用户通过 Cognito 登录、走与聊天机器人**完全相同**的 SigV4 `/invocations` 路径，所以产生的 span、Token、会话和评估分与真实流量无法区分。

> **安全边界**：一切都限定在 `simuser+` 邮箱前缀内，代码里有 guard 对其他邮箱直接抛异常，所以 `teardown` 不可能误删真实用户。
>
> 跑完等两三分钟再看大屏 —— CloudWatch 有摄取延迟，且大屏有 5 分钟缓存（点刷新可强制重算）。

**完整演示前 runbook**(含排障、跑完该检查什么)见 [管理员手册 §10.3](docs/admin_manual_管理员使用手册.md#103-演示前准备生成模拟数据每次演示必做);实现细节见 [`scripts/sim/README.md`](scripts/sim/README.md) 与[架构文档 §9.16](docs/architecture-and-design.md#916-simulated-end-users-test-data-generation)。

---

## 本地开发

### 设备模拟器

```bash
cd device-simulator && npm install && npm start  # http://localhost:3001
```

创建 `device-simulator/public/config.js`（用 `cdk-outputs.json` 里的值）：
```javascript
window.__CONFIG__ = {
  iotEndpoint: "YOUR_IOT_ENDPOINT",
  region: "us-west-2",
  cognitoIdentityPoolId: "YOUR_IDENTITY_POOL_ID"
};
```

### 聊天机器人

```bash
cd chatbot && npm install && npm start  # http://localhost:3000
```

创建 `chatbot/public/config.js`：
```javascript
window.__CONFIG__ = {
  cognitoUserPoolId: "YOUR_USER_POOL_ID",
  cognitoClientId: "YOUR_CLIENT_ID",
  cognitoDomain: "YOUR_DOMAIN",
  cognitoIdentityPoolId: "YOUR_IDENTITY_POOL_ID",  // SigV4 签名所需
  agentRuntimeArn: "YOUR_RUNTIME_ARN",
  region: "us-west-2"
};
```

### 管理控制台

```bash
cd admin-console && npm install && npm start  # http://localhost:3002
```

创建 `admin-console/public/config.js`：
```javascript
window.__CONFIG__ = {
  cognitoUserPoolId: "YOUR_USER_POOL_ID",
  cognitoClientId: "YOUR_CLIENT_ID",
  adminApiUrl: "YOUR_ADMIN_API_URL",
  agentRuntimeArn: "YOUR_RUNTIME_ARN",
  region: "us-west-2"
};
```

### Skill ERP

```bash
cd skill-erp && npm install && npm start  # http://localhost:3003
```

创建 `skill-erp/public/config.js`：
```javascript
window.__CONFIG__ = {
  cognitoUserPoolId: "YOUR_USER_POOL_ID",
  cognitoClientId: "YOUR_CLIENT_ID",
  erpApiUrl: "YOUR_SKILL_ERP_API_URL",
  region: "us-west-2"
};
```

### Strands Agent

```bash
source venv/bin/activate
export AWS_REGION=us-west-2
export MODEL_ID=moonshotai.kimi-k2.5
cd agent && python agent.py  # http://localhost:8080
```

本地 smoke test：
```bash
curl http://localhost:8080/ping
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Turn on the LED matrix to rainbow mode"}'
```

---

## 配置与自定义

- **更换 LLM**：管理控制台 Models 页签按用户/全局覆盖，无需重新部署；或编辑 `agent/agent.py` / 设置 `MODEL_ID` 环境变量修改默认值（默认 `moonshotai.kimi-k2.5`）
- **更换语音欢迎语**：编辑 `scripts/setup-agentcore.py` 中的 Polly 文案/Voice，重跑步骤 6
- **自定义域名**：在 `cdk/lib/smarthome-stack.ts` 中为 CloudFront 分发加 `domainNames` + ACM 证书

---

## 月度成本估算

本方案全部采用 AWS Serverless 托管服务，按实际用量付费。以下按日活用户（DAU）1 万、10 万、100 万三个量级估算月度成本（us-west-2，价格截至 2025 年）。

**假设**：每用户每天 10 次对话，每次含 1 次 LLM 调用 + 1.5 次工具调用 + 0.3 次 KB 查询；LLM 为 Kimi K2.5（输入 ~800 tokens，输出 ~200 tokens）；知识库 1000 个文档（~500MB），每月同步 4 次；语音模式对话中每 10 次文本调用搭配 2 次 Nova Sonic 语音对话。

| 模块 | 服务 | 1 万 DAU | 10 万 DAU | 100 万 DAU |
|------|------|---------|----------|-----------|
| **AI Agent** | AgentCore Runtime | ~$150 | ~$1,500 | ~$15,000 |
| **文字 LLM** | Bedrock (Kimi K2.5) | ~$80 | ~$800 | ~$8,000 |
| **语音双向流** | Bedrock (Nova Sonic) | ~$50 | ~$500 | ~$5,000 |
| **工具路由** | AgentCore Gateway | ~$15 | ~$150 | ~$1,500 |
| **策略引擎** | AgentCore Policy Engine | ~$5 | ~$50 | ~$500 |
| **长期记忆** | AgentCore Memory | ~$20 | ~$200 | ~$2,000 |
| **知识库检索** | Bedrock KB (Retrieve) | ~$10 | ~$100 | ~$1,000 |
| **向量嵌入** | Bedrock (Cohere Embed) | ~$2 | ~$2 | ~$2 |
| **向量存储** | S3 Vectors | <$1 | ~$5 | ~$50 |
| **设备控制 / 管理 API / 其他 Lambda** | Lambda + API Gateway | ~$5 | ~$40 | ~$400 |
| **用户认证** | Cognito（前 50K MAU 免费） | $0 | ~$250 | ~$4,500 |
| **前端托管 / 数据存储** | S3 + CloudFront + DynamoDB | ~$10 | ~$70 | ~$600 |
| **质量评估** | AgentCore Evaluator | ~$10 | ~$100 | ~$1,000 |
| | **月度总计** | **~$358** | **~$3,767** | **~$39,552** |
| | **每用户每月** | **~$0.036** | **~$0.038** | **~$0.040** |

**Serverless 成本优势**：无运维、无空闲成本、线性扩展、规模经济递减。**S3 Vectors** 是按向量计费的纯 Serverless 服务，1 万 DAU 场景下每月不到 $1；之前的方案使用 OpenSearch Serverless 有 ~$350/月 的底价。

> 以上为估算值，实际成本取决于具体使用模式。建议使用 [AWS Pricing Calculator](https://calculator.aws/) 精确计算。

---

## Voice Agent 启动延迟测试

项目根目录的 `voice-latency-test/` 是一个**自包含**的 Playwright 测试方案，用于测量 Voice Agent 从点击按钮到听到首帧回应的延迟。`deploy.sh` 完成后直接可用，不需要额外配置——脚本会自动从 `cdk-outputs.json` + `agentcore-state.json` 读取 Chatbot URL、Voice Runtime ARN 和 region。

两种测试模式：

| 模式 | 模拟场景 | 单轮 | 100 轮 |
|---|---|---:|---:|
| `run-session-cold.sh` | 老用户回来点语音（测服务端 Python worker 冷启动）| ~18 s | ~30 min |
| `run-fresh-login.sh` | 新用户登录后立即点语音（测端到端用户旅程 + 前端优化）| ~60 s | ~100 min |

```bash
cd voice-latency-test
npm install && npx playwright install chromium    # 仅首次
./run-session-cold.sh                              # 或 ./run-fresh-login.sh
```

输出写入 `voice-latency-test/results/`（原始 JSONL + 聚合 markdown 报告）。详细协议、两种模式的完整对比、以及已实施的 16 项延迟优化清单见 `voice-latency-test/README.md` 和 `voice-latency-test/test-modes.md`。

---

## 销毁资源

**顺序很重要：** AgentCore 资源必须在 CDK 堆栈之前销毁。

```bash
source venv/bin/activate

# 1. 先销毁 AgentCore（Gateway、Target、Runtime、Memory）
python3 scripts/teardown-agentcore.py

# 2. 再销毁 CDK 堆栈
cd cdk && npx cdk destroy --all --force
```

销毁脚本只删除 `agentcore-state.json` 中记录的资源。

---

## 故障排除

### 部署相关

- **`agentcore CLI not found`** → `npm install -g @aws/agentcore`（**不是** pip 包；详见[前置条件](#前置条件)）
- **`agentcore deploy fails: Target not found in aws-targets.json`** → 部署脚本会自动生成，手动跑的话创建 `[{"name": "default", "region": "us-west-2", "account": "YOUR_ACCOUNT_ID"}]`
- **`CDK synth fails: pyproject.toml not found`** → `agent/pyproject.toml` 必须存在（仓库已含）
- **`Bedrock Model Access Denied`** → Bedrock 控制台申请 Kimi K2.5 + Nova Sonic 访问权限
- **`@aws-sdk/client-bedrockagentcorecontrol does not exist`** → 正常，AgentCore 资源由 `agentcore` CLI 创建（步骤 6），不由 CDK 直接创建
- **销毁失败 `Gateway has targets associated`** → 销毁脚本会按顺序处理；手动跑时 `aws cloudformation delete-stack --stack-name AgentCore-smarthome-default`
- **`create_registry failed: ServiceQuotaExceededException ... maximum number of registries (5)`** → 账号已经达到 AWS Agent Registry 的默认配额（5）。如果该账号已经有名为 `SmartHomeSkillsRegistry` 的 Registry，部署脚本会自动复用；否则需在 AWS Service Quotas 控制台申请提额，或删除不用的 Registry。
- **`boto3 ... is below the required 1.43.67`** → venv 中的 boto3 过旧。1.43.67 是首个包含 `agent-registry` / `agent-registry-control` 两个 service 的版本（AWS Agent Registry 于 2026-08-06 GA 时迁到该命名空间）。重跑 `scripts/01-install-deps.sh`（会自动升级），或 `pip install --upgrade boto3`。
- **Skill ERP 新建技能后卡在 DRAFT 状态** → 表示 `SubmitRegistryRecordForApproval` 在记录仍处于 `CREATING` 时被调用。最新 Lambda 会轮询 `GetRegistryRecord` 直到状态脱离 `CREATING` 再提交，更新 Lambda 代码即可（重跑 `scripts/04-cdk-deploy.sh` 或 `aws lambda update-function-code`）。
- **⚠️ 跑过 `cdk deploy` 之后：Tool Policy 里一个 Gateway 工具都不显示 / Optimization 认不出子 Agent / `/optimization/*` 报 ConfigurationError** → admin Lambda 的环境变量被重置了。CDK 只声明其中 7 个，另外 10 个（`GATEWAY_ID`、`REGISTRY_ID`、`DASHBOARD_EXTRA_RUNTIME_ARNS`、7 个 `OPTIMIZATION_*`）由 `setup-agentcore.py` 在部署后补写，而 CloudFormation 里 `environment` 是整张表，所以任何一次 `cdk deploy` 都会把它们抹掉，**且全程没有任何报错**。修复：重跑 `python scripts/setup-agentcore.py`，再 `cd a2a-agent-registry && python deploy.py --only patch-text-agent`。核对：`aws lambda get-function-configuration --function-name smarthome-admin-api --query "length(Environment.Variables)"` 应为 28 而非 14。

### 前端相关

- **设备模拟器 MQTT 失败** → 浏览器控制台检查 Cognito Identity Pool ID、IoT 端点、IAM 角色
- **聊天机器人 403 / AccessDenied** → 确认 `config.js` 的 `cognitoIdentityPoolId`、`agentRuntimeArn` 正确；Cognito 身份池**已认证角色** 必须有 `bedrock-agentcore:InvokeAgentRuntime*` 权限（`scripts/setup-agentcore.py` 第 6 步会授权）
- **语音模式立即断开** → 已认证角色缺少 `bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream`；或 Runtime 的 `authorizerConfiguration` 未清空
- **语音模式连上但听不到 Nova Sonic 回复** → DevTools → Network → `/ws` 行 → Messages，若能看到 `bidi_audio_stream` 说明服务端正常；通常是浏览器 `AudioContext` 需用户交互后才能播放，点击页面任意位置再试
- **语音欢迎语没播** → 多半是刚部署完有旧会话缓存了老代码；`scripts/setup-agentcore.py` 会自动停掉 DynamoDB 里记录的会话，但如果用户是在部署**之前**就已连接的，重新登录一次即可

### 管理控制台相关

- **`Access Denied`** → 登录用户必须在 Cognito `admin` 组
- **管理 API 403 `Forbidden: admin group required`** → JWT `cognito:groups` claim 必须含 `admin`
- **技能加载失败 / 会话显示 "default"** → 检查 AgentCore Runtime 的 `SKILLS_TABLE_NAME` 环境变量和 DynamoDB 权限；聊天机器人硬刷新（`Ctrl+Shift+R`）清缓存

---

## 文档

| 文档 | 内容 |
|------|------|
| 本 README | 部署、使用、本地开发、成本估算、故障排除 |
| [`docs/architecture-and-design.md`](docs/architecture-and-design.md) | 架构图、组件设计、认证模型、语音模式实现细节、**A2A 专家 Agent 的身份透传与 skill 强制**、**场景编排与定时执行**、**Agents 机队页**、AgentCore CLI 坑、运维大屏与测试数据设计、API 参考、MQTT 命令、技术选型 |
| [`docs/admin_manual_管理员使用手册.md`](docs/admin_manual_管理员使用手册.md) | 管理员运维手册:部署闭环、身份接入、权限管控(含授权复核与工具影响面)、质量评估、提示词优化、Skill 审批流水线、**Agents 机队与逐个 Agent prompt**、**场景联动与定时自动化**、Session 调试、运维大屏、`cdk deploy` 环境变量陷阱 |
| [`docs/agent-design-principles.md`](docs/agent-design-principles.md) | **Agent 设计理念**:Harness 设计、Context 工程、Prompt 设计三章。每条都配本仓 file:line 与实测数字;与预期相反的结论会写明预期本身 |
| [`docs/measurements/`](docs/measurements/) | 延迟与成本实测:测量方法(`README.md`)、Spec 5 逐阶段 before/after 报告(`spec5-report.md`)、可对比的基线归档(JSON) |
| [`scripts/sim/README.md`](scripts/sim/README.md) | 模拟用户脚本:persona 配置、覆盖范围、安全边界与已知坑位 |

---

## 安全

详见 [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications)。

## 许可证

本项目使用 MIT-0 许可证。详见 LICENSE 文件。

---
---

# English Version

> **Agent Harness management platform**, using a smart home scenario to demonstrate how to build a complete Agent operations and governance system on AWS AgentCore: skill orchestration, model selection, tool access control (per-user Cedar policies), **enterprise knowledge base (on S3 Vectors)**, **Integration Registry (A2A agents)**, session monitoring (with **Remote Shell** debug console), long-term memory viewing, and safety guardrails.

AI-powered smart home control system built on AWS AgentCore Runtime/Memory/Gateway. Users chat with the assistant via **natural-language text** or **real-time voice conversation** (Amazon Nova Sonic bi-directional streaming) to control simulated IoT devices (LED Matrix, Rice Cooker, Fan, Oven). The admin console organises 15 pages across four lifecycle stages — **Discover / Build / Deploy / Assess** — including an **agent operations dashboard** on Overview (live health, token cost attribution, evaluation drift, release state) and a **Remote Shell** per-session debug console. The **Skill ERP** site lets end users publish their own skills and A2A agents to **AWS Agent Registry**; admins can then one-click import approved records into the skills catalog or browse A2A agents in the Integration Registry. The enterprise knowledge base uses the **S3 Vectors** serverless store (pay-per-vector, no fixed cost).

> **Implementation details, architecture diagrams, protocol specs** live in [`docs/architecture-and-design.md`](docs/architecture-and-design.md). This README focuses on **deployment and usage**.

## Design principles (read this first)

Nine agents — one orchestrator plus eight A2A specialists — each on its own
AgentCore Runtime. The topology is the least interesting part. What is worth reading
is **which decisions were forced by a measurement or a production failure**. The
full set is in [`docs/agent-design-principles.md`](docs/agent-design-principles.md),
each entry citing `file:line` and a number; here are the five most
counter-intuitive.

**1. A tool that touches user data must be a factory, never a list.** Building the
tool list once at startup pins whichever user arrived first onto every later
request — no error, no log line, and the agent keeps answering fluently. It is a
cross-user data leak that looks exactly like a working system.
`common/server.py:316` rebuilds per request, and `user_id` comes from a closure so
it appears in **no** model-facing signature: a parameter the model can fill is a
parameter a prompt injection can fill.

**2. Half of our "performance work" pointed the wrong way.** Of Spec 5's four
latency phases, two were overturned by their own measurements:

| Expected | Measured |
|---|---|
| Prompt caching cuts latency (AWS docs: up to 85%) | latency **2%** (noise); tokens **-98%** |
| Parallel delegation needs building | Strands was **already** concurrent; the transport was broken (the 3rd delegation crashed) |
| Prewarming removes a cold start | **0.3s** after 100 minutes idle — nothing to win, proposal dropped |
| Streaming drops TTFT from 30s to single digits | **impossible**: the model can't write prose before its tool returns |

Building the instrument first (`scripts/measure-baseline.py`) was not process
hygiene — three of those optimisations move the same number, and without a fixed
method none of them could have been attributed afterwards, only claimed.

**3. Separate the latency you own from the latency you rent.** Of a 24.3s mean
turn, **7.1s is spent inside AgentCore before our container is entered** — ~7s for
a session id the runtime has never seen, ~0.4s for a reused one. So a "16s fast
path" is about 8s of agent work behind 8s of platform session creation, and quoting
wall alone credits the platform's cold start to the harness in both directions.

**4. A tool's description outranks the system prompt about that tool.** We added a
device list to each delegation and rewrote the prompts to say "stop calling
`discover_devices`". Two deploys later, nothing had changed — because
`discover_devices`' own docstring still opened with "Call this FIRST, every time",
and that text is attached to the very tool the model is deciding about. **Nothing
was visible in any reply**: the answers stayed correct and the optimisation simply
never happened.

**5. Design against silent success, not against crashes.** Nearly every bug in this
system's history reported success: a redeploy that "worked" while voiding every
user's A2A grants; a dashboard that read "no data" for six days as though the system
were idle; an `agentcore deploy` that succeeded while packaging stale code. The
response is always the same — **assert the thing you want from outside the code that
claims to do it**: read spans rather than reply text, validate against the botocore
service model rather than the docs, diff the deployed copy against the repo.

## Prerequisites

| Requirement | Version | Purpose | How to install |
|-------------|---------|---------|----------------|
| [Node.js](https://nodejs.org/) | >= 18.x | Build React apps, run CDK | [Installer](https://nodejs.org/en/download) or [nvm](https://github.com/nvm-sh/nvm#installing-and-updating) |
| [npm](https://www.npmjs.com/) | >= 9.x | Package management | Ships with Node.js |
| [Python 3](https://www.python.org/) | >= 3.12 | AgentCore setup script, agent code | [Installer](https://www.python.org/downloads/) or your system package manager |
| [AWS CLI](https://aws.amazon.com/cli/) | >= 2.x | AWS credentials | [Official install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| [agentcore CLI](https://www.npmjs.com/package/@aws/agentcore) | >= 0.13.0 | Deploy AgentCore resources (Gateway / Runtime / Memory) | `npm install -g @aws/agentcore` · [Starter Toolkit docs](https://aws.github.io/bedrock-agentcore-starter-toolkit/api-reference/cli.html) |
| [boto3](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html) | >= 1.43.67 | AgentCore / Agent Registry API calls in setup script | Via the `pip install` in [Quick Start](#quick-start) below (`scripts/01-install-deps.sh` upgrades it automatically) |
| AWS Account | — | With Bedrock AgentCore, Kimi K2.5 and Nova Sonic model access | See below |

> **The agentcore CLI comes from npm, not pip.** Earlier revisions of this README said `pip install strands-agents-builder`; that package provides a `strands` command (a sample Strands agent) and does **not** install the `agentcore` binary `deploy.sh` needs. Use `npm install -g @aws/agentcore`. `deploy.sh` checks for >= 0.13.0 on startup (that release fixed a scaffold-test regression that broke `agentcore deploy`). Upgrade with `npm install -g @aws/agentcore@latest`.

**Important:** In [Bedrock Console > Model Access](https://console.aws.amazon.com/bedrock/home#/modelaccess), request access to:
- **Kimi K2.5** (`moonshotai.kimi-k2.5`) for text chat
- **Amazon Nova Sonic** (`amazon.nova-2-sonic-v1:0`) for voice conversation

### Deployer IAM Permissions

The IAM user/role running `deploy.sh` needs permissions for (full list + minimal IAM policy JSON in [docs/architecture-and-design.md §9.1](docs/architecture-and-design.md#91-two-stack-architecture)):

| Service | Purpose |
|---------|---------|
| CloudFormation / CDK / S3 / CloudFront / Lambda / DynamoDB | Core infrastructure |
| Cognito / Cognito Identity | User auth, Identity Pool temporary credentials |
| IoT Core | Endpoint + Things |
| Bedrock / S3 Vectors | KB vectorization + retrieval; Nova Sonic bi-directional streaming |
| Bedrock AgentCore | Gateway, Runtime, Memory, Policy Engine |
| Polly | Pre-render voice welcome clip |
| IAM / STS / Logs / API Gateway | Roles, identity, logging, admin API |

---

## Quick Start

```bash
# 1. Configure AWS credentials
aws configure

# 2. Install the agentcore CLI (global npm package; deploy.sh checks for >= 0.13.0)
npm install -g @aws/agentcore
agentcore --version

# 3. Set up Python environment
python3 -m venv venv
source venv/bin/activate
pip install strands-agents strands-agents-builder bedrock-agentcore boto3 mcp pyyaml

# 4. Deploy everything
./deploy.sh
```

After deployment, `deploy.sh` prints URLs for all four frontends (device simulator, chatbot, admin console, Skill ERP) and the default admin credentials.

### Deployment Overview

`deploy.sh` is a thin wrapper that runs `scripts/0[1-7]-*.sh` in order. Each script prints the AWS resources it creates so you can debug or re-run a single step.

| Step | Script | Responsibility |
|------|--------|----------------|
| 1 | `01-install-deps.sh` | CDK npm deps + bundle latest boto3 into Lambda dirs |
| 2 | `02-build-frontends.sh` | Build the 3 React frontends |
| 3 | `03-cdk-bootstrap.sh` | `cdk bootstrap` (idempotent) |
| 4 | `04-cdk-deploy.sh` | Deploy CDK stack: Cognito, IoT, Lambda, DynamoDB, KB, API Gateway, S3+CloudFront |
| 5 | `05-fix-cognito.sh` | Enable self-signup + email verification |
| 6 | `06-deploy-agentcore.sh` | Deploy AgentCore stack: Gateway, Targets, Runtime (with pre-rendered welcome audio), Memory; grant Cognito Identity Pool access to Runtime; stop old sessions so the fresh code takes effect immediately |
| 7 | `07-seed-skills.sh` | Write `agent/skills/*/SKILL.md` into DynamoDB |

> **Partial re-runs:** changed frontend only → rerun 2 + 4; changed agent code only → rerun 6; changed built-in skill files only → rerun 7.

---

## Usage

### Chatbot — Text and Voice Modes

1. Open the chatbot URL from the deploy output, sign up / sign in
2. 🎤 button left of the input box toggles voice / text mode
3. **Text mode**: type and send, Kimi K2.5 (or per-user overridden model) responds
4. **Voice mode**: browser prompts for mic access → you hear the pre-rendered welcome clip "欢迎使用智能家居设备助手" → start talking, Nova Sonic does bi-directional streaming
5. Voice-mode commands like "打开风扇到中档" trigger actual MQTT device commands via the MCP gateway
6. **Live browser preview** (right-side rail, collapsed by default — click a label to expand): ask the agent any live-web question ("what does example.com say right now?", "find top 3 wireless earbuds under $100 on Amazon", "summarize the Python Wikipedia page") without saying `browse_web` — the skill description auto-routes it to the tool. The right panel streams the real Chrome via DCV at 1280×800 (scrollbars appear when the panel is narrower); each step is screenshotted into the agent's `/mnt/workspace/<session>/browser/` which the Files tab can browse and download. After the tool returns, the AgentCore session stays alive for **15 minutes** — click **Take control** to drive the browser manually (fill captchas, click filters, etc.) without a new tool call. See [architecture §9.11](docs/architecture-and-design.md#911-browser-use--live-agent-web-automation).
7. **Grouped starter prompts on the welcome screen**: 5 labelled groups (Smart devices / Knowledge base / Weather / Live web browser / Image analysis) cover every capability — click a chip to stage its prompt.

### Admin Console — Agent Harness Control Center

Log in with the admin credentials from deploy output. The login page also offers **self-service registration** (the email is the username; Cognito emails a 6-digit code to verify it).

> **Registering does not grant admin permission.** This console is open only to members of the `admin` group. A newly registered account can sign in but lands on "Access Denied" until an administrator adds it to the group (`Make Admin` on Build → Identity, or `aws cognito-idp admin-add-user-to-group`). Until then, use the **chatbot** — every end-user capability (smart home conversation, device control, knowledge base) works without admin rights. Both the sign-up form and the Access Denied page link straight to it.

The side navigation groups 16 pages by agent lifecycle stage:

| Stage | Page | What you can do |
|-------|------|-----------------|
| **Discover** | **Overview** | Product intro + architecture diagram (collapsed by default) and the **agent operations dashboard** (see below). The three demo launchers moved to the side nav's **Demos** group |
| Discover | **Agents** | **Fleet view**: 1 orchestrator + 8 specialists + voice + an A/B variant + 1 tool, with runtime name, status, skill count and live metrics. The detail page **edits that agent's system prompt** — saved, and in effect on its next request, with no container redeploy. The list is derived from runtime ARNs + Registry records, so a newly deployed sub-agent appears with no frontend change |
| Discover | **Integration Registry** | Tool integration overview + **A2A Agents sub-tab**: approved A2A records from AWS Agent Registry with endpoint / auth / capabilities / publisher; details drawer shows the full agent card |
| **Build** | **Models** | Set the global default LLM; override text and vision models per user (Kimi, Claude 4.5/4.6, DeepSeek, Qwen, Llama 4, OpenAI GPT, ...) |
| Build | **Skills** | Create/edit/delete skills with full [Agent Skills spec](https://agentskills.io/specification) fields; manage skill directory files via S3 presigned URLs; global + per-user overrides; **import approved records from AWS Agent Registry** |
| Build | **Prompt** | Edit the text / voice agent system prompts (global default + per-user addendum); runtime concatenates additively |
| Build | **Tool Policy** | Configure per-user tool permissions (Cedar policies); built-in and gateway tools listed side-by-side with source badges; toggle ENFORCE / LOG_ONLY. Each gateway tool also names **who calls it** — revoking `control_device` stops chat commands, scheduled scenes and two specialists |
| Build | **Memories** | View each user's long-term memory (facts + preferences, from AgentCore Memory) |
| Build | **Knowledge Base** | Upload documents to the enterprise KB (PDF, TXT, MD, DOCX, CSV, ...); one-click Bedrock KB vectorization sync; per-user isolation |
| Build | **Identity** | Registered-users table **and all user management**: create, promote/demote admin, delete. (These lived on Overview previously; consolidated here. Self-demotion and self-deletion stay disabled.) |
| **Deploy** | **Instance Type** | Compute class configuration (MicroVM today, EC2 planned) |
| Deploy | **Sessions** | Per-login runtime sessions (user / kind / session ID / last active / 7-day tokens, **labelled with the owning agent**); Stop with one click; **Remote Shell** streams shell commands inside the runtime container — admin-only SSH-style debug console |
| **Assess** | **Agent Guardrails** | Links to AgentCore Evaluator + Bedrock Guardrails consoles |
| Assess | **Observability** | Link to CloudWatch Gen-AI Observability |
| Assess | **Evaluations** | Link to the AgentCore Evaluations console |
| Assess | **Optimization** | AgentCore Optimization: recommendations, configuration bundles, target-based A/B tests, per-tenant entry environment. The target can be **any deployed agent** — the options come from the fleet, not a hardcoded list |

#### Agent operations dashboard (on Overview)

A monitoring-wall view for the administrator of a unified consumer entry point: a six-signal status strip on top, then three rows of paired panels. The architecture diagram is collapsed by default so the metrics are on screen when the page opens. Time range (24h / 7d / 30d) sits at the top and scopes every panel; the cost-attribution dimension (by user / entry environment / agent runtime) lives inside the **Token cost attribution** panel because it only affects that panel. Every chart has a table view.

> **"By entry environment" is not per-customer billing.** It aggregates the three `tenant_env` modes (`default` / `ab-bundles` / `ab-targets`) — a cost comparison across A/B routing groups. This project has no separate tenant entity; real per-customer attribution would need one first (a Cognito group or a `tenantId` attribute).

| Metric group | Source | Real? |
|---|---|---|
| Live health (active sessions, TTFT P95/P99, error rate, QPS) | `AWS/Bedrock-AgentCore` metrics + `aws/spans`, summed across every registered runtime with a per-runtime breakdown | ✅ |
| Token cost trend + attribution (input/output split, and per-agent on Sessions) | Strands `chat` spans in `aws/spans` | ✅ tokens; ❌ dollar cost |
| Budget consumption | — | ❌ simulated |
| Evaluation scores & drift | `Bedrock-AgentCore/Evaluations` | ✅ single-variant; ❌ A/B |
| Active version & release state | Runtime Endpoint/Version + CloudTrail | ✅ versions; ⚠️ rollout stage derived |
| User satisfaction (CSAT, thumbs, escalation) | — | ❌ simulated |

Cards without a real source carry a **Demo data** badge whose popover states what a real source would require. Four caveats worth knowing: **TTFT is not a CloudWatch metric** (it exists only as a span attribute); **dollar cost cannot be split per user or agent** (Cost Explorer resolves only to account level), so only token counts are attributed; **rollout stage has no native field** — it is derived from Gateway A/B tests plus `tenant_env`; and **every runtime must be registered explicitly** — `service.name` on spans and eval metrics is an exact match, so the dashboard aggregates over an allowlist built from `AGENT_RUNTIME_ARN` + `VOICE_AGENT_RUNTIME_ARN` + `DASHBOARD_EXTRA_RUNTIME_ARNS` (the A2A deploy script registers its own runtimes). Full detail in [`docs/architecture-and-design.md` §9.15](docs/architecture-and-design.md#915-agent-operations-dashboard).

> The dashboard starts empty — it needs real traffic. Generate some with the [simulated-users script](#generate-test-data-simulated-users).

### Skill ERP — end-user skill publishing

Skill ERP is a self-service skills site for **regular end users** (no `admin` group required). Each signed-in user sees and edits only the records they created.

1. Open the Skill ERP URL from the deploy output
2. Sign up / sign in with any Cognito account (the same user pool as the chatbot)
3. Click "+ Create Skill" and fill in name / description / instructions / allowed tools / license / compatibility / metadata (**no file upload** — AWS Agent Registry's agentSkills descriptor only carries SKILL.md + definition JSON)
4. On save, the record is published to AWS Agent Registry (`SmartHomeSkillsRegistry`) with `descriptorType=agentSkills` and auto-submitted for approval (`SubmitRegistryRecordForApproval`)
5. Status column shows `PENDING / SUBMITTED / APPROVED / REJECTED` — you can keep editing or delete at any time
5b. If rejected, **the curator's reason appears right under the status** — it is the only feedback the author receives
6. An admin approves or rejects in the **pending-review queue** inside **Admin Console → Skills → "Add approved skill from AWS Agent Registry"** (a reason is required to reject), then imports it from the same dialog — the AWS console is no longer involved

### A2A Specialist Agents (optional, for demo)

`a2a-agent-registry/` contains **8** independently deployable A2A (Agent-to-Agent) specialists the orchestrator delegates to over the standard A2A protocol:

| Agent | Skills | Model | Touches devices? |
|-------|--------|-------|------------------|
| `device-control-agent` | multi-device orchestration, capability disambiguation | Haiku 4.5 | ✅ via Gateway |
| `light-effect-agent` | mood / image → lighting effect | Haiku 4.5 | ✅ via Gateway |
| `knowledge-qa-agent` | documentation Q&A, troubleshooting | Nova Lite | ✅ knowledge base |
| `task-management-agent` | saved tasks and automations | Haiku 4.5 | ❌ plans only — see below |
| `scene-sync-agent` | music / video feasts, driven live | Haiku 4.5 | ✅ via Gateway |
| `home-security-agent` | risk assessment, incident response | Haiku 4.5 | ❌ advisory |
| `energy-optimization-agent` | savings estimates, tariff analysis | Nova Lite | ❌ advisory |
| `appliance-maintenance-agent` | maintenance schedule, diagnosis | Nova Lite | ❌ advisory |

**The point is not "more agents" — it is that identity is not lost at the hop:**

- The `Authorization` header carries a shared m2m token. It proves *an authorised service is calling* and nothing else — it has **no `sub`**.
- The user's idToken travels in its own `X-SuperApp-User-Token` header, and the sub-agent **re-verifies it independently** (JWKS signature, issuer, audience, expiry) before opening the Gateway with it — so **Cedar evaluates the real end user**. The sub-agent runtimes hold no device permissions of their own.
- `X-A2A-Allowed-Skills` is now **enforced server-side**. It used to be parsed into request state and ignored, which made per-skill authorisation purely client-side: anything holding the shared m2m token could call any skill on any agent.

Other notes:

- **`./deploy.sh` does NOT deploy them** — the base system stays minimal.
- Deploy (requires `./deploy.sh` already done):
  ```bash
  cd a2a-agent-registry
  python deploy.py                       # all of them
  python deploy.py --agent light-effect  # one only
  python smoke_test.py                   # 7 agents + 6 negative authorisation cases
  ```
- Grant access per user per skill in **Admin Console → Users → Manage Permissions → A2A Agents**; the orchestrator picks it up on the next invocation. **An ungranted skill is never registered**, so the model cannot be talked into calling a tool it cannot see.
- **Each specialist's prompt is editable** at Admin Console → Agents → detail page; saved, and in effect on the next request, with no container redeploy.
- A delegated turn takes ~30s against ~15s direct — the A2A hop is non-streaming, so the orchestrator emits nothing until the specialist finishes. That is stated in the dashboard's TTFT hint rather than hidden.
- Full deploy flow, test prompts, and step-by-step demo walkthrough: [`a2a-agent-registry/README.md`](a2a-agent-registry/README.md).

### Scenes and scheduled automations

Say "every night at 11pm turn the lights off and set the fan to low" in the chatbot: the orchestrator delegates to the task-management specialist, which stores it as a **scene** (a trigger plus device actions), and EventBridge Scheduler runs it on time.

Five trigger kinds:

| Kind | Notes |
|---|---|
| **time** | 24-hour `HH:MM`, scheduled in **the owner's timezone** (set on Identity; unset means UTC, exactly as before) |
| **solar** | sunrise or sunset computed from the owner's coordinates, with an offset ("30 minutes before sunset"). Recomputed nightly, because the sun moves every day |
| **device state** | a device entering a state |
| **sensor threshold** | temperature / humidity / PM2.5 / CO₂ — you must say above or below, because "above 26" and "below 26" build opposite scenes |
| **manual** | never fires by itself; runs when the user names it ("run movie mode") |

A solar scene needs the owner's coordinates, and without them it is **refused with a message saying where to set them** — a guessed location turns the lights on at the wrong time, which is harder to notice than a refusal.

**Admin Console → Assess → Scenarios** shows every user's automations: the trigger, the live cron expression and its timezone, and whether the last run worked. A scene that fires at 07:30 has nobody watching, so that last column is the only way to tell "works" from "has never worked".

> **Scheduled execution is not a way around governance.** The runner Lambda holds no IoT permission at all: it authenticates as the scene's owner and goes Gateway → Cedar → `iot-control`, the same authorisation chain a hand-typed command uses. So revoking a user's `control_device` in Tool Policy also stops their 07:30 automation.
>
> The cost, stated plainly: acting as an absent user needs a credential. `GetWorkloadAccessTokenForUserId` was measured and its token is rejected by the Gateway with 401 (it is an opaque KMS-encrypted token, not a JWT with the right audience), so what gets stored is a **Cognito refresh token** — a 30-day user credential at rest in Secrets Manager, under a dedicated KMS key with rotation, one secret per user, readable only by the runner, never logged. A user with no stored token simply has no scheduled scenes execute.

The device simulator carries three props for scenes to sync to: a **virtual clock** (up to 3600x — it accelerates the simulator's own time and sensor curve, and deliberately does **not** move the real AWS trigger time), **screen sync** (the TV backlight's four segments follow the four edges of a procedural picture), and **music sync** (a synthesised beat plus a bluetooth `idle → pairing → connected` state machine).

### Music and video feasts, driven live

"Save it for later" and "make it happen now" are different jobs, and two sub-agents do them. The split is the design:

- `task-management-agent` owns a table and holds **no device permissions**.
- `scene-sync-agent` holds Gateway device tools and **no table**.

Merging them would give the agent that writes scenes a device path of its own — which is the single thing `shared/scenarios.py` exists to prevent (a scene is data, not a capability). Neither can do the other's half; the orchestrator strings them together, and **asks before saving a feast as a one-tap command**.

Say "make the living room lights dance to the music": the TV backlight goes into `music` sync mode and the other fixtures run a `chase` on the beat. There is a step in there that fails silently by nature — music goes through a Bluetooth speaker, the lights do nothing until that link is up, and pairing takes a second or two. So the agent **polls `bluetooth` until it settles**, and treats "still pairing" as an answer distinct from either outcome:

| State | What the agent says |
|---|---|
| `connected` | drives the lights and reports the feast is running |
| `pairing` | says the link has not come up yet, suggest retrying — **does not guess which way it went** |
| `idle` | says plainly that no speaker is paired and asks the user to connect one — **never reports success** |

The cost of guessing is asymmetric: reading `pairing` as failure tells the user to reconnect a speaker that was two seconds from working, and reading `idle` as success leaves them staring at a room of motionless lights.

`bluetooth` is a **readonly** capability: the catalog declares no action that writes it, and `validate_command` drops any parameter an action does not declare, so a command — including one a prompt injection talked a model into phrasing — cannot assert a link state the device alone may report.

### Add Admin Users

Grant a self-registered user access to this console, either way:

- **Console**: Admin Console → Build → **Identity**, find the user and click `Make Admin`.
- **CLI**:

  ```bash
  aws cognito-idp admin-add-user-to-group \
    --user-pool-id <USER_POOL_ID> \
    --username <EMAIL> \
    --group-name admin
  ```

The user must sign in again for it to take effect — group membership arrives in the idToken's `cognito:groups` claim, so a fresh token is required.

> **Note**: users an administrator creates from the Identity page do **not** get tool permissions automatically — Cognito's PostConfirmation trigger only fires for self-signup. Those users still need an explicit grant on **Tool Policy**, verified per [the admin manual §4.2](docs/admin_manual_管理员使用手册.md).

### Generate test data (simulated users)

Right after deploy the ops dashboard and AgentCore Evaluations are empty — they need real traffic. This script creates a few test users and has them converse with the agent like real users, covering the agent's full feature surface:

```bash
export SIM_USER_PASSWORD='SomeStrong#Pass1'   # must satisfy the Cognito password policy

python3 scripts/simulate-users.py setup       # create + configure 5 personas (idempotent)
python3 scripts/simulate-users.py run         # light tier, ~3.5 min
python3 scripts/simulate-users.py run --heavy # adds code-interpreter + browser-use
python3 scripts/simulate-users.py status      # who exists, with what config
python3 scripts/simulate-users.py teardown --yes
```

The five personas deliberately differ in model, tenant mode and scenario mix, so the dashboard's attribution charts show several real rows instead of collapsing into one bucket:

| persona | model | tenant env | exercises |
|---|---|---|---|
| `alice` | Opus 4.6 | default | all four devices, discovery, all-devices-on |
| `bob` | Sonnet 4.6 | default | knowledge base, weather lookup, refusal |
| `carol` | Haiku 4.5 | ab-bundles | multi-turn memory recall, user feedback |
| `dave` | Kimi K2.5 | ab-targets | code-interpreter data analysis (heavy) |
| `erin` | Sonnet 4.5 | default | browser-use web automation (heavy), refusal, ambiguity |

Test users sign in through Cognito and use the **same** SigV4 `/invocations` path as the chatbot, so the spans, tokens, sessions and evaluation scores they produce are indistinguishable from real usage.

> **Safety boundary**: everything is scoped to the `simuser+` email prefix, and a guard in the code raises on any other address — `teardown` cannot delete real users.
>
> Wait two or three minutes after a run before checking the dashboard: CloudWatch ingestion lags and the dashboard caches for 5 minutes (use Refresh to force re-aggregation).

**Full pre-demo runbook** (troubleshooting, what to verify afterwards) is [admin manual §10.3](docs/admin_manual_管理员使用手册.md#103-演示前准备生成模拟数据每次演示必做); implementation detail in [`scripts/sim/README.md`](scripts/sim/README.md) and [architecture §9.16](docs/architecture-and-design.md#916-simulated-end-users-test-data-generation).

---

## Local Development

### Device Simulator

```bash
cd device-simulator && npm install && npm start  # http://localhost:3001
```

Create `device-simulator/public/config.js` with deployed values (from `cdk-outputs.json`):
```javascript
window.__CONFIG__ = {
  iotEndpoint: "YOUR_IOT_ENDPOINT",
  region: "us-west-2",
  cognitoIdentityPoolId: "YOUR_IDENTITY_POOL_ID"
};
```

### Chatbot

```bash
cd chatbot && npm install && npm start  # http://localhost:3000
```

Create `chatbot/public/config.js`:
```javascript
window.__CONFIG__ = {
  cognitoUserPoolId: "YOUR_USER_POOL_ID",
  cognitoClientId: "YOUR_CLIENT_ID",
  cognitoDomain: "YOUR_DOMAIN",
  cognitoIdentityPoolId: "YOUR_IDENTITY_POOL_ID",  // required for SigV4 signing
  agentRuntimeArn: "YOUR_RUNTIME_ARN",
  region: "us-west-2"
};
```

### Admin Console

```bash
cd admin-console && npm install && npm start  # http://localhost:3002
```

Create `admin-console/public/config.js`:
```javascript
window.__CONFIG__ = {
  cognitoUserPoolId: "YOUR_USER_POOL_ID",
  cognitoClientId: "YOUR_CLIENT_ID",
  adminApiUrl: "YOUR_ADMIN_API_URL",
  agentRuntimeArn: "YOUR_RUNTIME_ARN",
  region: "us-west-2"
};
```

### Skill ERP

```bash
cd skill-erp && npm install && npm start  # http://localhost:3003
```

Create `skill-erp/public/config.js`:
```javascript
window.__CONFIG__ = {
  cognitoUserPoolId: "YOUR_USER_POOL_ID",
  cognitoClientId: "YOUR_CLIENT_ID",
  erpApiUrl: "YOUR_SKILL_ERP_API_URL",
  region: "us-west-2"
};
```

### Strands Agent

```bash
source venv/bin/activate
export AWS_REGION=us-west-2
export MODEL_ID=moonshotai.kimi-k2.5
cd agent && python agent.py  # starts on http://localhost:8080
```

Local smoke test:
```bash
curl http://localhost:8080/ping
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Turn on the LED matrix to rainbow mode"}'
```

---

## Configuration & Customization

- **Change the LLM**: use Models tab in the admin console for per-user/global override (no redeploy); or edit `agent/agent.py` / set `MODEL_ID` env var for the default (defaults to `moonshotai.kimi-k2.5`)
- **Change the voice welcome clip**: edit the Polly text/voice in `scripts/setup-agentcore.py`, re-run step 6
- **Custom domain**: add `domainNames` + ACM certificate to the CloudFront distributions in `cdk/lib/smarthome-stack.ts`

---

## Monthly Cost Estimation

Fully AWS Serverless, pay-per-use. Estimates below are for 10K / 100K / 1M Daily Active Users (us-west-2, 2025 pricing).

**Assumptions**: each user averages 10 conversations/day with 1 LLM call + 1.5 tool calls + 0.3 KB queries; LLM is Kimi K2.5 (~800 input tokens, ~200 output); KB has 1,000 docs (~500MB), synced 4x/month; roughly 2 of every 10 conversations use Nova Sonic voice mode.

| Module | Service | 10K DAU | 100K DAU | 1M DAU |
|--------|---------|---------|----------|--------|
| **AI Agent** | AgentCore Runtime | ~$150 | ~$1,500 | ~$15,000 |
| **Text LLM** | Bedrock (Kimi K2.5) | ~$80 | ~$800 | ~$8,000 |
| **Voice bi-di stream** | Bedrock (Nova Sonic) | ~$50 | ~$500 | ~$5,000 |
| **Tool Routing** | AgentCore Gateway | ~$15 | ~$150 | ~$1,500 |
| **Policy Engine** | AgentCore Policy Engine | ~$5 | ~$50 | ~$500 |
| **Long-term Memory** | AgentCore Memory | ~$20 | ~$200 | ~$2,000 |
| **KB Retrieval** | Bedrock KB (Retrieve) | ~$10 | ~$100 | ~$1,000 |
| **Vector Embedding** | Bedrock (Cohere Embed) | ~$2 | ~$2 | ~$2 |
| **Vector Store** | S3 Vectors | <$1 | ~$5 | ~$50 |
| **Device control / Admin API / other Lambda** | Lambda + API Gateway | ~$5 | ~$40 | ~$400 |
| **Authentication** | Cognito (first 50K MAU free) | $0 | ~$250 | ~$4,500 |
| **Frontend hosting / Data storage** | S3 + CloudFront + DynamoDB | ~$10 | ~$70 | ~$600 |
| **Quality Evaluation** | AgentCore Evaluator | ~$10 | ~$100 | ~$1,000 |
| | **Monthly Total** | **~$358** | **~$3,767** | **~$39,552** |
| | **Per User / Month** | **~$0.036** | **~$0.038** | **~$0.040** |

**Serverless advantages**: zero ops, zero idle cost, linear scaling, decreasing per-user cost at scale. **S3 Vectors** is fully pay-per-vector with no floor; at 10K DAU the vector store costs under $1/month. The previous setup used OpenSearch Serverless (~$350/month minimum).

> These are estimates. Use the [AWS Pricing Calculator](https://calculator.aws/) for precise numbers.

---

## Teardown

**Order matters:** AgentCore resources must be destroyed before the CDK stack.

```bash
source venv/bin/activate

# 1. Tear down AgentCore first (Gateway, Target, Runtime, Memory)
python3 scripts/teardown-agentcore.py

# 2. Then destroy CDK stack
cd cdk && npx cdk destroy --all --force
```

The teardown script only deletes resources tracked in `agentcore-state.json`.

---

## Troubleshooting

### Deployment

- **`agentcore CLI not found`** → `npm install -g @aws/agentcore` (**not** a pip package; see [Prerequisites](#prerequisites))
- **`agentcore deploy fails: Target not found in aws-targets.json`** → setup script seeds this; if running manually, create `[{"name": "default", "region": "us-west-2", "account": "YOUR_ACCOUNT_ID"}]`
- **`CDK synth fails: pyproject.toml not found`** → `agent/pyproject.toml` must exist (included in repo)
- **`Bedrock Model Access Denied`** → request access to Kimi K2.5 + Nova Sonic in the Bedrock console
- **`@aws-sdk/client-bedrockagentcorecontrol does not exist`** → expected; AgentCore resources are created by the `agentcore` CLI (step 6), not by CDK directly
- **Teardown fails `Gateway has targets associated`** → the teardown script handles order; manually: `aws cloudformation delete-stack --stack-name AgentCore-smarthome-default`
- **`create_registry failed: ServiceQuotaExceededException ... maximum number of registries (5)`** → the account is at the AWS Agent Registry default quota (5). If a registry named `SmartHomeSkillsRegistry` already exists the deploy script reuses it automatically; otherwise request a quota increase in AWS Service Quotas or delete an unused registry.
- **`boto3 ... is below the required 1.43.67`** → venv boto3 is too old. 1.43.67 is the first release carrying the `agent-registry` and `agent-registry-control` services that AWS Agent Registry moved to when it went GA on 2026-08-06. Re-run `scripts/01-install-deps.sh` (which upgrades boto3) or `pip install --upgrade boto3`.
- **Skill ERP records stuck in `DRAFT`** → `SubmitRegistryRecordForApproval` was called while the record was still `CREATING`. The current Lambda polls `GetRegistryRecord` until the record leaves `CREATING` before submitting — just push the latest code (re-run `scripts/04-cdk-deploy.sh` or `aws lambda update-function-code`).
- **⚠️ After any `cdk deploy`: no gateway tools in Tool Policy / Optimization rejects a sub-agent / `/optimization/*` returns ConfigurationError** → the admin Lambda's env vars were reset. CDK declares 7 of them; the other 10 (`GATEWAY_ID`, `REGISTRY_ID`, `DASHBOARD_EXTRA_RUNTIME_ARNS`, seven `OPTIMIZATION_*`) are patched in afterwards by `setup-agentcore.py`, and `environment` in CloudFormation is the whole map — so any `cdk deploy` drops them, **with no error anywhere**. Fix: re-run `python scripts/setup-agentcore.py`, then `cd a2a-agent-registry && python deploy.py --only patch-text-agent`. Verify: `aws lambda get-function-configuration --function-name smarthome-admin-api --query "length(Environment.Variables)"` should be 28, not 14.

### Frontend

- **Device simulator MQTT fails** → browser console: check Cognito Identity Pool ID, IoT endpoint, IAM role
- **Chatbot 403 / AccessDenied** → verify `cognitoIdentityPoolId` + `agentRuntimeArn` in `config.js`; the Cognito authenticated role must have `bedrock-agentcore:InvokeAgentRuntime*` (granted by `scripts/setup-agentcore.py` step 6)
- **Voice mode closes right after connect** → authenticated role is missing `bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream`, or the Runtime's `authorizerConfiguration` is not cleared
- **Voice session connects but no audio from Nova Sonic** → DevTools → Network → `/ws` row → Messages. If `bidi_audio_stream` frames are arriving the server is fine; usually the browser `AudioContext` is still locked — click anywhere on the page (browser autoplay policy) and retry
- **Welcome clip silent** → typically leftover warm containers with stale code after a redeploy; `scripts/setup-agentcore.py` auto-stops DynamoDB-tracked sessions, but users connected **before** the deploy should simply log in again

### Admin Console

- **`Access Denied`** → logged-in user must belong to the `admin` Cognito group
- **Admin API returns 403 `Forbidden: admin group required`** → same — JWT `cognito:groups` claim must contain `admin`
- **Skills not loading / Sessions show user = "default"** → check `SKILLS_TABLE_NAME` env var on the AgentCore Runtime + DynamoDB permissions; hard-refresh the chatbot (`Ctrl+Shift+R`) to drop stale bundle

---

## Documentation

| Document | Covers |
|----------|--------|
| This README | Deployment, usage, local dev, cost estimation, troubleshooting |
| [`docs/architecture-and-design.md`](docs/architecture-and-design.md) | Architecture diagrams, component design, authentication model, voice-mode implementation details, **A2A identity forwarding and server-side skill enforcement**, **scene orchestration and scheduled execution**, **the Agents fleet page**, AgentCore CLI quirks, ops-dashboard and test-data design, API reference, MQTT schemas, technology choices |
| [`docs/admin_manual_管理员使用手册.md`](docs/admin_manual_管理员使用手册.md) | Administrator runbook (Chinese): deploy loop, identity, permission management incl. grant verification and tool blast radius, quality evaluation, prompt optimization, skill approval pipeline, **the Agents fleet and per-agent prompts**, **scenes and scheduled automations**, session debugging, ops dashboard, the `cdk deploy` env-var trap |
| [`docs/agent-design-principles.md`](docs/agent-design-principles.md) | **Agent design principles** — Harness design, context engineering, prompt design. Every entry cites the code (`file:line`) and the number or bug that produced it; where a measurement contradicted the expectation, the expectation is named |
| [`docs/measurements/`](docs/measurements/) | Latency and cost measurements: method (`README.md`), the phase-by-phase before/after report (`spec5-report.md`), and comparable archived baselines (JSON) |
| [`scripts/sim/README.md`](scripts/sim/README.md) | Simulated-users script: persona configuration, coverage, safety boundary, known gotchas |

---

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

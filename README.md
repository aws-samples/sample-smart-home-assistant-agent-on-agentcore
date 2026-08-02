# Smart Home Assistant Agent — Agent Harness 管理平台

> **Agent Harness 管理平台**，以智能家居场景为示例，展示如何在 AWS AgentCore 上构建完整的 Agent 运维管控体系：技能编排、模型选择、工具权限（per-user Cedar 策略）、**企业知识库**、外部集成、会话监控、长期记忆查看和质量评估。

基于 AWS AgentCore Runtime/Memory/Gateway 构建的 AI 智能家居控制系统。用户可以通过聊天机器人用**自然语言文字**或**实时语音对讲**（Nova Sonic 双向流式）控制模拟 IoT 设备（LED 矩阵灯、电饭煲、风扇、烤箱）。管理控制台按 **Discover / Build / Deploy / Assess** 四个阶段组织 15 个页面，覆盖 Agent 全生命周期，其中 Overview 页内置 **Agent 运维统计大屏**（实时健康、Token 成本归因、评估漂移、版本发布状态）。**Skill ERP** 网站让普通用户可以自助发布技能到 **AgentCore Registry**，审批通过后一键导入到技能目录。

> **实现原理、架构图、协议细节** 请参见 [`docs/architecture-and-design.md`](docs/architecture-and-design.md)。本 README 专注于**部署和使用**。

![architecture](screenshots/architecture.drawio.png)
![chatbot](screenshots/smarthomeassistant-chat.png)
![device simulator](screenshots/smarthomeassistant-devices-v2.png)
![admin console](screenshots/smarthomeassistant-admin.png)

## 前置条件

| 条件 | 版本 | 用途 |
|------|------|------|
| Node.js | >= 18.x | 构建 React 应用、运行 CDK |
| npm | >= 9.x | 包管理 |
| Python 3 | >= 3.12 | AgentCore 部署脚本、Agent 代码 |
| boto3 | 最新 | 部署脚本中的 AgentCore API 调用 |
| agentcore CLI | 最新 | 部署 AgentCore 资源（`pip install strands-agents-builder`） |
| AWS CLI | >= 2.x | AWS 凭证配置 |
| AWS 账号 | — | 需开通 Bedrock AgentCore、Kimi-2.5 和 Nova Sonic 模型访问权限 |

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

# 2. 设置 Python 环境
python3 -m venv venv
source venv/bin/activate
pip install strands-agents strands-agents-builder bedrock-agentcore boto3 mcp pyyaml

# 3. 一键部署
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

左侧导航按 Agent 生命周期分成四段，共 15 个页面：

| 分段 | 页面 | 能做什么 |
|------|------|---------|
| **Discover** | **Overview** | 产品说明 + 架构图（默认折叠）以及 **Agent 运维统计大屏**（见下节）。三个 Demo 入口已移至侧边栏「演示入口」分组 |
| Discover | **Integration Registry** | 工具集成概览 + 从 AgentCore Registry 读取已批准的 **A2A Agent** 记录（显示名称/端点/能力/发布者） |
| **Build** | **Models** | 设置全局默认 LLM 模型；按用户覆盖文字模型与视觉模型（Kimi、Claude 4.5/4.6、DeepSeek、Qwen、Llama 4、OpenAI GPT 等） |
| Build | **Skills** | 创建/编辑/删除技能（完整 [Agent Skills 规范](https://agentskills.io/specification) 字段）；技能目录文件管理（S3 预签名 URL）；全局 + 按用户覆盖；**从 AgentCore Registry 导入已批准技能** |
| Build | **Prompt** | 编辑文字/语音 agent 的 system prompt（全局默认 + 按用户追加），运行时叠加拼接 |
| Build | **Tool Policy** | 按用户配置可调用的工具（Cedar 策略）；内置工具与 Gateway 工具并列并用 Badge 区分；ENFORCE / LOG_ONLY 切换 |
| Build | **Memories** | 查看每个用户的长期记忆（事实 + 偏好，来自 AgentCore Memory） |
| Build | **Knowledge Base** | 上传文档到企业知识库（PDF、TXT、MD、DOCX、CSV 等）；一键触发 Bedrock KB 向量化同步；按用户隔离 |
| Build | **Identity** | 已注册用户表，**以及全部用户管理**：新增用户、提权/降权、删除（原先在 Overview，已统一收敛到此处；不能对自己降权或删除） |
| **Deploy** | **Instance Type** | 计算实例类型（当前 MicroVM，EC2 规划中） |
| Deploy | **Sessions** | 每次登录的运行时会话列表（用户 / 类型 / 会话 ID / 最近活跃 / 近 7 天 Token）、一键 Stop，以及 **Remote Shell**（在 Runtime 容器里执行 shell 命令，stdout/stderr 流式回传） |
| **Assess** | **Agent Guardrails** | 跳转 AgentCore Evaluator + Bedrock Guardrails 控制台 |
| Assess | **Observability** | 跳转 CloudWatch Gen-AI Observability |
| Assess | **Evaluations** | 跳转 AgentCore Evaluations 控制台 |
| Assess | **Optimization** | AgentCore Optimization：推荐、配置包、目标级 A/B 测试、按用户配置入口环境（entryEnvironment） |

#### Agent 运维统计大屏（Overview 页内）

面向"统一入口 Super App"管理员的运维视图，按监控大屏布局：顶部一条六信号状态条，下面三行成对面板。架构图默认折叠，打开页面即见运维数据。顶部可切换时间范围（24h / 7d / 30d，作用于全部面板）；成本归因维度（按用户 / 入口环境 / Agent 模型）位于「Token 成本归因」面板内，因为它只影响该面板。每张图都配表格视图。

> **「按入口环境」不是按客户计费。** 它聚合的是 `tenant_env` 的三种模式（`default` / `ab-bundles` / `ab-targets`），也就是 A/B 分流组之间的成本对比 —— 本项目没有独立的租户实体。真正的按客户归因需要先引入 tenant 实体（如 Cognito 组或 `tenantId` 属性）。

| 指标组 | 数据来源 | 是否真实 |
|--------|---------|---------|
| 实时健康（活跃会话、TTFT P95/P99、错误率、QPS） | `AWS/Bedrock-AgentCore` 指标 + `aws/spans` | ✅ |
| Token 成本趋势与归因（输入/输出拆分） | `aws/spans` 里的 Strands `chat` span | ✅ Token；❌ 美元成本 |
| 成本预算消耗 | — | ❌ 模拟数据 |
| 评估通过率与漂移 | `Bedrock-AgentCore/Evaluations` | ✅ 单变体；❌ A/B 对比 |
| 活跃版本与发布状态 | Runtime Endpoint/Version + CloudTrail | ✅ 版本；⚠️ 灰度阶段为推导值 |
| 用户满意度（CSAT、赞踩、升级率） | — | ❌ 模拟数据 |

无真实数据来源的卡片会显示 **演示数据** 标记，点开有说明"要变成真实数据需要什么"。几个口径要点：**TTFT 不存在于 CloudWatch 指标中**，只能从 span 属性取；**美元成本无法按用户/Agent 拆分**（Cost Explorer 只到账号级），所以只归因 Token 数量；**灰度阶段没有原生字段**，由 Gateway A/B test 与 `tenant_env` 推导而来。详见 [`docs/architecture-and-design.md` §9.15](docs/architecture-and-design.md#915-agent-operations-dashboard)。

> 大屏默认是空的 —— 需要真实流量才有数据。用下面的[模拟用户脚本](#生成测试数据模拟真实用户)一条命令生成。

### Skill ERP —— 用户自助发布技能

Skill ERP 是面向**普通终端用户**的技能发布站点（不要求 `admin` 组成员），每个登录用户只能看到和管理自己创建的技能。

1. 打开部署输出里的 Skill ERP URL
2. 用自己的 Cognito 账号注册/登录（与聊天机器人共用账户体系）
3. 点击 "+ 创建技能"，填写名称/描述/指令/允许的工具/许可证/兼容性/元数据（**不支持文件上传** — AgentCore Registry 的 agentSkills 描述符只承载 SKILL.md + 定义 JSON）
4. 保存后，记录会自动以 `agentSkills` descriptorType 发布到 AgentCore Registry（`SmartHomeSkillsRegistry`），并自动触发 `SubmitRegistryRecordForApproval`
5. 状态栏会显示 `PENDING / SUBMITTED / APPROVED / REJECTED`，可以随时编辑或删除
6. 管理员在 **AgentCore Registry 控制台** 审批记录后，可在 **Admin Console → Skills → "Add approved skill from AgentCore Registry"** 将其导入技能目录

### A2A 示例 Agent（可选，演示用）

`a2a-agent-registry/` 下有 3 个独立部署的 A2A (Agent-to-Agent) 示例 agent，演示如何让 text agent 通过标准 A2A 协议委托给专家 agent：`energy-optimization-agent` / `home-security-agent` / `appliance-maintenance-agent`。

- **`./deploy.sh` 不会部署它们** —— 保持基础系统精简。
- 部署方式（依赖 `./deploy.sh` 已跑通）：
  ```bash
  cd a2a-agent-registry
  python deploy.py                              # 全量
  python deploy.py --agent energy-optimization  # 只部署一个
  ```
- Admin 在 **Admin Console → Users → Manage Permissions → A2A Agents** 区块按用户按 skill 授权；text agent 在下一次调用时加载。
- 完整部署流程、测试提示词和逐步演示指南见 [`a2a-agent-registry/README.md`](a2a-agent-registry/README.md)。

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

细节见 [`scripts/sim/README.md`](scripts/sim/README.md) 与 [`docs/architecture-and-design.md` §9.16](docs/architecture-and-design.md#916-simulated-end-users-test-data-generation)。

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

- **`agentcore CLI not found`** → `pip install strands-agents-builder`
- **`agentcore deploy fails: Target not found in aws-targets.json`** → 部署脚本会自动生成，手动跑的话创建 `[{"name": "default", "region": "us-west-2", "account": "YOUR_ACCOUNT_ID"}]`
- **`CDK synth fails: pyproject.toml not found`** → `agent/pyproject.toml` 必须存在（仓库已含）
- **`Bedrock Model Access Denied`** → Bedrock 控制台申请 Kimi K2.5 + Nova Sonic 访问权限
- **`@aws-sdk/client-bedrockagentcorecontrol does not exist`** → 正常，AgentCore 资源由 `agentcore` CLI 创建（步骤 6），不由 CDK 直接创建
- **销毁失败 `Gateway has targets associated`** → 销毁脚本会按顺序处理；手动跑时 `aws cloudformation delete-stack --stack-name AgentCore-smarthome-default`
- **`create_registry failed: ServiceQuotaExceededException ... maximum number of registries (5)`** → 账号已经达到 AgentCore Registry 的默认配额（5）。如果该账号已经有名为 `SmartHomeSkillsRegistry` 的 Registry，部署脚本会自动复用；否则需在 AWS Service Quotas 控制台申请提额，或删除不用的 Registry。
- **`boto3 is too old — missing bedrock-agentcore-control.create_registry`** → venv 中的 boto3 低于 1.42.93。重跑 `scripts/01-install-deps.sh`（会自动升级），或 `pip install --upgrade boto3`。
- **Skill ERP 新建技能后卡在 DRAFT 状态** → 表示 `SubmitRegistryRecordForApproval` 在记录仍处于 `CREATING` 时被调用。最新 Lambda 会轮询 `GetRegistryRecord` 直到状态脱离 `CREATING` 再提交，更新 Lambda 代码即可（重跑 `scripts/04-cdk-deploy.sh` 或 `aws lambda update-function-code`）。

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
| [`docs/architecture-and-design.md`](docs/architecture-and-design.md) | 架构图、组件设计、认证模型、语音模式实现细节、AgentCore CLI 坑、运维大屏与测试数据设计、API 参考、MQTT 命令、技术选型 |
| [`docs/admin_manual_管理员使用手册.md`](docs/admin_manual_管理员使用手册.md) | 管理员运维手册:部署闭环、身份接入、权限管控(含授权复核)、质量评估、提示词优化、Skill 流水线、Session 调试、运维大屏 |
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

AI-powered smart home control system built on AWS AgentCore Runtime/Memory/Gateway. Users chat with the assistant via **natural-language text** or **real-time voice conversation** (Amazon Nova Sonic bi-directional streaming) to control simulated IoT devices (LED Matrix, Rice Cooker, Fan, Oven). The admin console organises 15 pages across four lifecycle stages — **Discover / Build / Deploy / Assess** — including an **agent operations dashboard** on Overview (live health, token cost attribution, evaluation drift, release state) and a **Remote Shell** per-session debug console. The **Skill ERP** site lets end users publish their own skills and A2A agents to **AgentCore Registry**; admins can then one-click import approved records into the skills catalog or browse A2A agents in the Integration Registry. The enterprise knowledge base uses the **S3 Vectors** serverless store (pay-per-vector, no fixed cost).

> **Implementation details, architecture diagrams, protocol specs** live in [`docs/architecture-and-design.md`](docs/architecture-and-design.md). This README focuses on **deployment and usage**.

## Prerequisites

| Requirement | Version | Purpose |
|-------------|---------|---------|
| Node.js | >= 18.x | Build React apps, run CDK |
| npm | >= 9.x | Package management |
| Python 3 | >= 3.12 | AgentCore setup script, agent code |
| boto3 | latest | AgentCore API calls in setup script |
| agentcore CLI | latest | Deploy AgentCore resources (`pip install strands-agents-builder`) |
| AWS CLI | >= 2.x | AWS credentials |
| AWS Account | — | With Bedrock AgentCore, Kimi-2.5 and Nova Sonic model access |

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

# 2. Set up Python environment
python3 -m venv venv
source venv/bin/activate
pip install strands-agents strands-agents-builder bedrock-agentcore boto3 mcp pyyaml

# 3. Deploy everything
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

The side navigation groups 15 pages by agent lifecycle stage:

| Stage | Page | What you can do |
|-------|------|-----------------|
| **Discover** | **Overview** | Product intro + architecture diagram (collapsed by default) and the **agent operations dashboard** (see below). The three demo launchers moved to the side nav's **Demos** group |
| Discover | **Integration Registry** | Tool integration overview + **A2A Agents sub-tab**: approved A2A records from AgentCore Registry with endpoint / auth / capabilities / publisher; details drawer shows the full agent card |
| **Build** | **Models** | Set the global default LLM; override text and vision models per user (Kimi, Claude 4.5/4.6, DeepSeek, Qwen, Llama 4, OpenAI GPT, ...) |
| Build | **Skills** | Create/edit/delete skills with full [Agent Skills spec](https://agentskills.io/specification) fields; manage skill directory files via S3 presigned URLs; global + per-user overrides; **import approved records from AgentCore Registry** |
| Build | **Prompt** | Edit the text / voice agent system prompts (global default + per-user addendum); runtime concatenates additively |
| Build | **Tool Policy** | Configure per-user tool permissions (Cedar policies); built-in and gateway tools listed side-by-side with source badges; toggle ENFORCE / LOG_ONLY |
| Build | **Memories** | View each user's long-term memory (facts + preferences, from AgentCore Memory) |
| Build | **Knowledge Base** | Upload documents to the enterprise KB (PDF, TXT, MD, DOCX, CSV, ...); one-click Bedrock KB vectorization sync; per-user isolation |
| Build | **Identity** | Registered-users table **and all user management**: create, promote/demote admin, delete. (These lived on Overview previously; consolidated here. Self-demotion and self-deletion stay disabled.) |
| **Deploy** | **Instance Type** | Compute class configuration (MicroVM today, EC2 planned) |
| Deploy | **Sessions** | Per-login runtime sessions (user / kind / session ID / last active / 7-day tokens); Stop with one click; **Remote Shell** streams shell commands inside the runtime container — admin-only SSH-style debug console |
| **Assess** | **Agent Guardrails** | Links to AgentCore Evaluator + Bedrock Guardrails consoles |
| Assess | **Observability** | Link to CloudWatch Gen-AI Observability |
| Assess | **Evaluations** | Link to the AgentCore Evaluations console |
| Assess | **Optimization** | AgentCore Optimization: recommendations, configuration bundles, target-based A/B tests, per-tenant entry environment |

#### Agent operations dashboard (on Overview)

A monitoring-wall view for the administrator of a unified consumer entry point: a six-signal status strip on top, then three rows of paired panels. The architecture diagram is collapsed by default so the metrics are on screen when the page opens. Time range (24h / 7d / 30d) sits at the top and scopes every panel; the cost-attribution dimension (by user / entry environment / agent model) lives inside the **Token cost attribution** panel because it only affects that panel. Every chart has a table view.

> **"By entry environment" is not per-customer billing.** It aggregates the three `tenant_env` modes (`default` / `ab-bundles` / `ab-targets`) — a cost comparison across A/B routing groups. This project has no separate tenant entity; real per-customer attribution would need one first (a Cognito group or a `tenantId` attribute).

| Metric group | Source | Real? |
|---|---|---|
| Live health (active sessions, TTFT P95/P99, error rate, QPS) | `AWS/Bedrock-AgentCore` metrics + `aws/spans` | ✅ |
| Token cost trend + attribution (input/output split) | Strands `chat` spans in `aws/spans` | ✅ tokens; ❌ dollar cost |
| Budget consumption | — | ❌ simulated |
| Evaluation scores & drift | `Bedrock-AgentCore/Evaluations` | ✅ single-variant; ❌ A/B |
| Active version & release state | Runtime Endpoint/Version + CloudTrail | ✅ versions; ⚠️ rollout stage derived |
| User satisfaction (CSAT, thumbs, escalation) | — | ❌ simulated |

Cards without a real source carry a **Demo data** badge whose popover states what a real source would require. Three caveats worth knowing: **TTFT is not a CloudWatch metric** (it exists only as a span attribute); **dollar cost cannot be split per user or agent** (Cost Explorer resolves only to account level), so only token counts are attributed; and **rollout stage has no native field** — it is derived from Gateway A/B tests plus `tenant_env`. Full detail in [`docs/architecture-and-design.md` §9.15](docs/architecture-and-design.md#915-agent-operations-dashboard).

> The dashboard starts empty — it needs real traffic. Generate some with the [simulated-users script](#generate-test-data-simulated-users).

### Skill ERP — end-user skill publishing

Skill ERP is a self-service skills site for **regular end users** (no `admin` group required). Each signed-in user sees and edits only the records they created.

1. Open the Skill ERP URL from the deploy output
2. Sign up / sign in with any Cognito account (the same user pool as the chatbot)
3. Click "+ Create Skill" and fill in name / description / instructions / allowed tools / license / compatibility / metadata (**no file upload** — AgentCore Registry's agentSkills descriptor only carries SKILL.md + definition JSON)
4. On save, the record is published to AgentCore Registry (`SmartHomeSkillsRegistry`) with `descriptorType=agentSkills` and auto-submitted for approval (`SubmitRegistryRecordForApproval`)
5. Status column shows `PENDING / SUBMITTED / APPROVED / REJECTED` — you can keep editing or delete at any time
6. After the curator approves the record in the AgentCore Registry console, an admin can import it into the skills catalog via **Admin Console → Skills → "Add approved skill from AgentCore Registry"**

### A2A Sample Agents (optional, for demo)

`a2a-agent-registry/` contains 3 independently deployable A2A (Agent-to-Agent) sample agents that show how the text agent can delegate to specialist agents over the standard A2A protocol: `energy-optimization-agent`, `home-security-agent`, `appliance-maintenance-agent`.

- **`./deploy.sh` does NOT deploy them** — the base system stays minimal.
- Deploy (requires `./deploy.sh` already done):
  ```bash
  cd a2a-agent-registry
  python deploy.py                              # all three
  python deploy.py --agent energy-optimization  # one only
  ```
- Grant access per user per skill in **Admin Console → Users → Manage Permissions → A2A Agents**; the text agent picks it up on the next invocation.
- Full deploy flow, test prompts, and step-by-step demo walkthrough: [`a2a-agent-registry/README.md`](a2a-agent-registry/README.md).

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

Details in [`scripts/sim/README.md`](scripts/sim/README.md) and [`docs/architecture-and-design.md` §9.16](docs/architecture-and-design.md#916-simulated-end-users-test-data-generation).

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

- **`agentcore CLI not found`** → `pip install strands-agents-builder`
- **`agentcore deploy fails: Target not found in aws-targets.json`** → setup script seeds this; if running manually, create `[{"name": "default", "region": "us-west-2", "account": "YOUR_ACCOUNT_ID"}]`
- **`CDK synth fails: pyproject.toml not found`** → `agent/pyproject.toml` must exist (included in repo)
- **`Bedrock Model Access Denied`** → request access to Kimi K2.5 + Nova Sonic in the Bedrock console
- **`@aws-sdk/client-bedrockagentcorecontrol does not exist`** → expected; AgentCore resources are created by the `agentcore` CLI (step 6), not by CDK directly
- **Teardown fails `Gateway has targets associated`** → the teardown script handles order; manually: `aws cloudformation delete-stack --stack-name AgentCore-smarthome-default`
- **`create_registry failed: ServiceQuotaExceededException ... maximum number of registries (5)`** → the account is at the AgentCore Registry default quota (5). If a registry named `SmartHomeSkillsRegistry` already exists the deploy script reuses it automatically; otherwise request a quota increase in AWS Service Quotas or delete an unused registry.
- **`boto3 is too old — missing bedrock-agentcore-control.create_registry`** → venv boto3 is older than 1.42.93. Re-run `scripts/01-install-deps.sh` (which upgrades boto3) or `pip install --upgrade boto3`.
- **Skill ERP records stuck in `DRAFT`** → `SubmitRegistryRecordForApproval` was called while the record was still `CREATING`. The current Lambda polls `GetRegistryRecord` until the record leaves `CREATING` before submitting — just push the latest code (re-run `scripts/04-cdk-deploy.sh` or `aws lambda update-function-code`).

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
| [`docs/architecture-and-design.md`](docs/architecture-and-design.md) | Architecture diagrams, component design, authentication model, voice-mode implementation details, AgentCore CLI quirks, ops-dashboard and test-data design, API reference, MQTT schemas, technology choices |
| [`docs/admin_manual_管理员使用手册.md`](docs/admin_manual_管理员使用手册.md) | Administrator runbook (Chinese): deploy loop, identity, permission management incl. grant verification, quality evaluation, prompt optimization, skill pipeline, session debugging, ops dashboard |
| [`scripts/sim/README.md`](scripts/sim/README.md) | Simulated-users script: persona configuration, coverage, safety boundary, known gotchas |

---

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

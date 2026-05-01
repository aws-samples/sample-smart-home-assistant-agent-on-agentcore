# 管理员使用手册 (Admin Manual)

> 本手册面向 **Agent Harness 管理员**,讲解如何结合 **Admin Console** + **AgentCore** 各组件完成智能家居 Agent 的全生命周期运维。
> 架构细节请参考 [`architecture-and-design.md`](./architecture-and-design.md)。

## 目录

1. [AgentCore 组件一览](#1-agentcore-组件一览)
2. [Agent 代码快速部署](#2-agent-代码快速部署)
3. [OAuth 用户账号接入](#3-oauth-用户账号接入)
4. [设备查询/控制的权限管控](#4-设备查询控制的权限管控)
5. [Agent 质量评估 (Evaluation)](#5-agent-质量评估-evaluation)
6. [自动化提示词与工具描述优化](#6-自动化提示词与工具描述优化)
7. [模型后训练 (Post-training)](#7-模型后训练-post-training)
8. [Skill 发布/审批/下发](#8-skill-发布审批下发)
9. [Session 调试与 Remote Shell](#9-session-调试与-remote-shell)
10. [其他重要事项](#10-其他重要事项)

---

## 1. AgentCore 组件一览

| 组件 | 本方案中的作用 | Admin Console 对应入口 |
|------|---------------|----------------------|
| **Runtime** | 承载 Agent 代码 (`smarthome` 文本 + `smarthomevoice` 语音两个 Runtime),支持 `/invocations` 和 `/ws` | Sessions / Remote Shell / Agent Prompt |
| **Gateway** | MCP Server,聚合设备控制、发现、KB 检索等 Lambda 工具;执行 Cedar 策略 | Tool Access / Integration Registry |
| **Memory** | 短期会话 + 长期事实/偏好/摘要 (三种策略) | Memories |
| **Registry** | Skill/A2A 描述符托管 + 审批工作流 | Skills → "Add approved skill from AgentCore Registry" |
| **Policy Engine** | Cedar 策略评估 (per-user tool permit + default-deny) | Tool Access |
| **Identity** (Cognito) | 用户认证、`principal.id` 来源 | Models / Tool Access 的用户列表 |
| **Evaluator** | 对话质量打分、数据集评测 | Quality Evaluation 入口 |
| **Knowledge Base** (Bedrock KB + S3 Vectors) | RAG 检索,按 scope 元数据隔离 | Knowledge Base |

---

## 2. Agent 代码快速部署

### 2.1 一键部署流程

仓库提供 `deploy.sh` 串接 7 个子脚本 (`scripts/01-*` ~ `scripts/07-*`),核心路径:

```
04-cdk-deploy.sh       # CDK 部署常规 AWS 资源 (Cognito/Lambda/API GW/...)
06-deploy-agentcore.sh → setup-agentcore.py  # AgentCore 资源
07-seed-skills.sh      # 将 agent/skills/ 初始化为 __global__ 条目
```

### 2.2 AgentCore CLI (本方案使用的方式)

官方 [AgentCore Starter Toolkit CLI](https://aws.github.io/bedrock-agentcore-starter-toolkit/api-reference/cli.html) 提供两种 `--deployment-type`: **`direct_code_deploy`**(零 Docker,管理员不需要构建镜像,CLI 自动打包 Python 源代码进 CloudFormation 部署;支持 `PYTHON_3_10..3_13`)和 `container`(自行提供 Docker 镜像)。本方案选用 `direct_code_deploy`,Python 3.13。常用命令:

```bash
agentcore configure --entrypoint agent.py --name smarthome \
  --deployment-type direct_code_deploy --runtime PYTHON_3_13 \
  --non-interactive                                      # 配置
agentcore add memory --name SmartHomeMemory \
  --strategies SEMANTIC,SUMMARIZATION,USER_PREFERENCE    # 声明 Memory 资源
agentcore add gateway                                    # 创建 Gateway
agentcore add gateway-target SmartHomeDeviceControl ...  # 注册 Lambda 工具
agentcore deploy -y --verbose                            # 构建 CFN stack 并发布
agentcore invoke '{"prompt":"ping"}'                     # 测试
```

`agentcore deploy` 会产出 CloudFormation stack `AgentCore-smarthome-default`,同时自动注入 `MEMORY_<NAME>_ID`、`AGENTCORE_GATEWAY_<NAME>_URL` 等环境变量。调用 Runtime 的公共 API 为 [`InvokeAgentRuntime`](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeAgentRuntime.html)(HTTP POST /invocations,payload ≤ 100 MB,支持流式)。

### 2.3 常见部署坑位 (已在 `setup-agentcore.py` 中解决)

- **boto3 版本**: Registry API 要求 `boto3 ≥ 1.42.93`,`01-install-deps.sh` 自动升级 venv。
- **环境变量被 deploy 覆盖**: `agentcore deploy` 会丢弃 `agentcore.json` 里自定义 env,必须 deploy 之后用 `update_agent_runtime` 再打补丁。
- **`requestHeaderAllowlist` 嵌套坑**: `get_agent_runtime` 返回顶层字段,`update_agent_runtime` 需要嵌入 `requestHeaderConfiguration`,round-trip 时若不改写会静默丢失自定义头,导致 Gateway 401。
- **部署后 Session 仍跑旧代码**: `setup-agentcore.py` 会扫描 DynamoDB 里 `__session_text__` / `__session_voice__` 记录并调用 `StopRuntimeSession`,让新部署立即生效。

### 2.4 重新部署的最小闭环

只改了 Python 代码? 只需:

```bash
bash scripts/06-deploy-agentcore.sh
```

它只重新走 `agentcore deploy` + env patch + session invalidate,不会动 Cognito/IoT/CDK。

---

## 3. OAuth 用户账号接入

> **术语澄清**: AWS 还提供了一项独立服务 [**AgentCore Identity**](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html)(Token Vault + Credential Provider + 2LO/3LO OAuth,可接 GitHub/Slack/Salesforce 等)。本方案的 inbound auth 使用 Cognito User Pool,**未启用 AgentCore Identity 的 outbound token 保管**(因为智能家居场景没有第三方 OAuth 资源调用需求)。未来若要接入外部 SaaS,应当在此替换为 AgentCore Identity。

### 3.1 全链路身份传递

```
① Cognito User Pool (email+password)
       │ idToken
       ▼
② Cognito Identity Pool (exchange → 临时 AWS 凭证)
       │ SigV4
       ▼
③ AgentCore Runtime (AUTH=AWS_IAM)
       │ 把 idToken 放在自定义 header
       │ X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken
       ▼
④ Agent 代码 (context.request_headers 读取) → 重新包装成 Bearer
       ▼
⑤ AgentCore Gateway (AUTH=CUSTOM_JWT) → Cedar 按 principal.id 评估
       │
       ▼
⑥ Lambda Target / Knowledge Base (JWT email 用于 scope 过滤)
```

### 3.2 用户 ID 在不同层的形态

| 位置 | 字段 | 示例 |
|------|------|------|
| Cognito Identity Pool / SigV4 签名 | 临时 AK/SK/Token (匿名化) | — |
| Runtime `/invocations` body | `userId` (email) | `alice@example.com` |
| Runtime Session Header | `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` | `user-session-{cognito-sub}` |
| Memory actor_id (sanitize 后) | 替换 `@`/`.` → `_` | `alice_example_com` |
| Cedar principal.id | Cognito `sub` UUID | `78d153c0-7011-...` |
| KB metadata filter | email | `scope = alice@example.com` OR `scope = __shared__` |

**为什么 Runtime 用 AWS_IAM 而 Gateway 用 CUSTOM_JWT?**
`/ws` 对 CUSTOM_JWT 支持不稳定 (HTTP 424),故 Runtime 统一 SigV4;但 Cedar 要基于 JWT `sub` 做 per-user 策略,所以 idToken 通过 **自定义 allowlist header** 透传进 Agent,再由 Agent 当 Bearer 递给 Gateway。

> [官方约束](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-header-allowlist.html): `requestHeaderAllowlist` 仅接受名称匹配正则 `(Authorization|X-Amzn-Bedrock-AgentCore-Runtime-Custom-[a-zA-Z0-9-]+)` 的头,最多 20 个,每个 header 总大小 ≤ 4 KB。本方案使用的 `X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken` 正符合此规则。

### 3.3 管理员动作

- **新增用户**: 由终端用户在 Chatbot 自助注册 (`AllowAdminCreateUserOnly=false`,邮箱验证)。Admin Console 只读 Cognito 用户列表,不做创建/重置。
- **授予 admin 权限**: 在 Cognito 控制台把用户加入 `admin` 组,Admin Console 会在登录时校验 `cognito:groups` 声明。

---

## 4. 设备查询/控制的权限管控

### 4.1 Cedar + Policy Engine (ENFORCE 模式)

Gateway 启用 Policy Engine,每个 **工具** 维护一条 `permit` 策略,白名单列出被授权用户的 `principal.id`。无匹配策略 = 默认拒绝(工具对用户不可见)。

```cedar
permit(
  principal,
  action == AgentCore::Action::"SmartHomeDeviceControl___control_device",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:...:gateway/{id}"
) when {
  ((principal is AgentCore::OAuthUser) || (principal is AgentCore::IamEntity)) &&
  ((principal.id) == "sub-uuid-alice" || (principal.id) == "sub-uuid-bob")
};
```

### 4.2 C 端用户权限配置(运维管理员操作路径)

**场景**: 新增用户 `carol@example.com`,只允许查询设备+KB,**不允许控制设备**。

1. 登录 **Admin Console → Tool Access**。
2. 在用户表格找到 Carol,点击 `Edit`。
3. 勾选 `discover_devices`、`query_knowledge_base`,**不勾** `control_device`。
4. 点击 **Save Permissions**。Admin Lambda 会:
   - 把 Carol 的 allowed list 写入 DynamoDB (`{cognitoSub}/__permissions__`);
   - 重新扫描所有拥有 `control_device` 的用户,**不包含 Carol** 重写该工具的 `permit` 策略;
   - 对 `discover_devices` 和 `query_knowledge_base`,把 Carol 的 sub 加进 permit 白名单。
5. **Mode Toggle**: Policy Engine 有 `ENFORCE` / `LOG_ONLY` 两档。调试时切到 LOG_ONLY 观察命中情况,正式切回 ENFORCE。

### 4.3 试用与演示

`Tool Access` 每行有 **Demo Links** 列,一键跳 `chatbot?username=carol@example.com`(已预填邮箱),管理员只需输入密码即可模拟该用户体验。

### 4.4 生产落地建议

- **组权限 vs 用户权限**: 当前 Cedar 策略只写 `principal.id`。生产环境可引入 Cognito 组 → `principal in Group::"family"`,减少单条策略里的用户数 (单策略上限 153KB / ~3800 用户)。
- **变更审计**: DynamoDB 更新时应开启 Stream + 写 CloudTrail,避免单点修改无迹可寻。

---

## 5. Agent 质量评估 (Evaluation)

> 参考官方文档: [AgentCore Evaluations 概览](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations.html) / [Evaluators](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluators.html) / [Dataset Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/dataset-evaluations.html)

### 5.1 AgentCore Evaluation 原理

AgentCore Evaluation 消费 **OpenTelemetry (OTEL) Traces**(GenAI semantic convention),支持 Strands / LangGraph 等框架。提供三种打分方法:

- **LLM-as-a-Judge (built-in)**: 官方已发布若干 built-in evaluator(公共 ARN),使用 Bedrock 基础模型按预置 rubric 打分,不可修改。
- **LLM-as-a-Judge (custom)**: 自定义 `instructions` + `ratingScale`(numerical 或 categorical)+ `modelConfig`,控制评审模型与打分标准。创建 API: [`CreateEvaluator`](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/create-evaluator.html)。
- **Code-based Evaluator**: 在 `evaluatorConfig` 里指定一个 Lambda(及 timeout),在 Lambda 里自行实现评分逻辑。适合"真实设备读回校验"这类确定性判定。
- **Ground Truth**: 数据集中写入 `expected_tools` / `expected_response` 等字段,built-in 或 custom evaluator 会把它们注入到 rubric 里进行对比。注意: **使用 ground truth 的 custom evaluator 不能绑定到 Online Evaluation**(官方明确禁止,因为线上流量没有期望答案)。

**运行方式**:

- **Online Evaluation**: Runtime 将 spans 推到 CloudWatch,evaluator 按采样率异步打分(只能用不依赖 ground truth 的 evaluator)。
- **On-Demand / Dataset Evaluation**: 用 AgentCore SDK 的 `OnDemandEvaluationDatasetRunner.run()`,内部分三阶段: **Invoke**(并发跑所有 scenario)→ **Wait**(等 CloudWatch 采集 spans)→ **Evaluate**(请求每个 evaluator)。产出 `EvaluationResult` → `ScenarioResult` → `EvaluatorResult`。适用 CI / 回归。

**配额**(官方当前值): 每 region 默认 ≤ 1,000 个 evaluator config,最多 100 个 active;对大 region 支持 1M tokens/min 输入输出。

### 5.2 Admin Console 流程

1. **Quality Evaluation** tab → `Open AgentCore Evaluator Console` 跳到 AWS 控制台。
2. 选 `smarthome` Runtime → 查看 Online 打分趋势,或新建 On-Demand Job:
   - 准备 dataset(见 5.3);
   - `CreateEvaluator` 指定 LLM-as-a-Judge 或 Lambda;
   - 用 SDK 的 `OnDemandEvaluationDatasetRunner` 运行,等待 `EvaluationResult`。
3. **回归基线**: 每次调整 Prompt/Skill/模型后,跑同一份数据集,对比 Pass Rate 与各 evaluator score 均值。

### 5.3 采样数据集示例 (dataset scenarios)

AgentCore SDK 的数据集是 scenarios 列表,每个 scenario 可单轮或多轮。ground-truth 字段会自动映射给 evaluator。

```json
{"scenario_id":"led-001","turns":[{"prompt":"把客厅灯设成彩虹","expected_tools":["SmartHomeDeviceControl___control_device"]}]}
{"scenario_id":"oven-001","turns":[{"prompt":"今晚烤鸡,炉子预热到 375","expected_tools":["SmartHomeDeviceControl___control_device"],"expected_response":"我已将烤箱设置为 375°F。"}]}
{"scenario_id":"kb-001","turns":[{"prompt":"产品说明里怎么清洗风扇","expected_tools":["query_knowledge_base"]}]}
```

---

## 6. 自动化提示词与工具描述优化

AWS 即将推出 **AgentCore Evo**,用于自动提升 agent 的 **system prompt** 与 **tool description** 质量。本方案在 Admin Console → Agent Prompt tab 预留了 "Optimization Suggestions (AgentCore Evo)" UI 占位卡,等官方 API 上线后直接接入。

---

## 7. 模型后训练 (Model Train)

AWS 即将推出 **AgentCore Model Train**,提供低代码方式对 **open-weight 模型**进行后训练优化(SFT / DPO / Distillation 等),只需几行代码即可集成到现有 agent 工程。正式发布后,管理员可直接在 Admin Console → Models tab 触发训练任务并将产出的 Custom Model 灰度下发给指定用户。

---

## 8. Skill 发布/审批/下发

### 8.1 三段式流水线

```
  终端用户                 管理员                    Agent
───────────          ─────────────────           ─────────────
Skill ERP          AgentCore Registry          Runtime / Gateway
(自助发布)          (审批控制台)               (动态加载)
     │                    │                          │
CreateRegistryRecord      │                          │
     │───────────────────►│                          │
SubmitForApproval         │                          │
     │ ==> PENDING_APPROVAL                          │
                    Approve / Reject                 │
                          │                          │
     Admin Console: Skills → Add from Registry       │
                          │                          │
                   写入 DynamoDB (__global__ 或 user) │
                          │                          │
                                    下次 /invocations │
                                 load_skills_from_dynamodb
                                                     │
```

### 8.2 业务场景:新增 "空气净化器" 设备的 skill

**前提**: 已在 IoT Core 注册 Thing + 扩展 `iot-control` 验证规则。

**步骤**:

| # | 角色 | 平台 | 动作 |
|---|------|------|------|
| 1 | 设备厂商员工 | **Skill ERP** | 登录 → Create Skill → 填 `name=air-purifier-control`,`description=控制空气净化器开关、风速、模式`,`allowed_tools=["control_device"]`,`instructions` 写 SKILL.md 正文 |
| 2 | Skill ERP 后端 | **AgentCore Registry** | `CreateRegistryRecord(descriptorType="AGENT_SKILLS")` → 轮询等 `CREATING` → `SubmitRegistryRecordForApproval` ⇒ `PENDING_APPROVAL` |
| 3 | 审批员 (Admin) | **AgentCore Registry 控制台** | 打开记录 → 审阅 SKILL.md → 控制台点 `Approve` / `Reject`(也可用 CLI: `aws bedrock-agentcore-control update-registry-record-status`)。可配合 EventBridge 接入工单/审批机器人 |
| 4 | Admin | **Admin Console → Skills** | 点 `Add approved skill from AgentCore Registry` → 勾选 `air-purifier-control` → 选择 scope `__global__` → `Import` |
| 5 | Agent | Runtime | 下一次 `/invocations` 时 `load_skills_from_dynamodb("__global__")` 自动拉到新 skill,无需重启 |

**验证**: 在 Chatbot 里问 "把空气净化器开到自动模式",观察 Agent 是否激活 `air-purifier-control` skill、工具调用是否正确。

### 8.3 修改一个已存在的 Skill (典型两种路径)

**路径 A - 用户自己的草案改版**:

1. Skill ERP → My Skills → 编辑 → Save → Lambda 执行 `UpdateRegistryRecord` + 重新 `SubmitRegistryRecordForApproval` → 状态回到 `PENDING_APPROVAL`。
2. 审批员在 Registry 控制台 Approve。
3. 管理员在 Admin Console **重新 Import**(覆盖 DynamoDB 里的行)。

**路径 B - 管理员直接热修复**:

1. Admin Console → Skills → 选中 skill → Edit instructions → Save。
2. 直接改写 DynamoDB。下一次 `/invocations` 立即生效。
3. ⚠️ 此路径**绕过 Registry 审批**,用于紧急止损,事后应把同等变更补回 Registry 以免漂移。

### 8.4 Global vs User-scope 的覆盖

DynamoDB 存两条记录:`__global__/{skillName}` 和 `{userEmail}/{skillName}`。Agent 加载时先 global 后 user,**同名时 user 覆盖 global**。所以 VIP 用户定制 skill 不会影响他人。

### 8.5 Skill 删除的级联

管理员在 Skills tab 删除某条 skill:
- DynamoDB 行删除;
- `smarthome-skill-files-{acct}/{userId}/{skillName}/` 下所有文件级联删除;
- 已注册到 Registry 的 Record 不受影响(如需清理,走 Skill ERP DELETE)。

---

## 9. Session 调试与 Remote Shell

**Sessions** tab 包含:User / Kind(Text/Voice)/ Session ID / Last Active / 7d Total Tokens / **Remote Shell** / Stop。

### 9.1 Stop Session

点击 `Stop` → Admin API 带 `?kind=text|voice` 调用对应 Runtime 的 `StopRuntimeSession`,DynamoDB 行随即删除(前端 UI 立刻移除)。典型场景:
- 给用户强制刷新 Memory 上下文;
- 热修 Skill 后希望立即让该用户生效(避免等 container 空闲回收)。

### 9.2 Remote Shell 示例

浏览器直接通过 [`InvokeAgentRuntimeCommand`](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-execute-command.html)(SigV4) 把 shell 指令流入目标容器,stdout/stderr 通过 HTTP/2 事件流(`contentStart` → `contentDelta` → `contentStop`)实时回流。Non-blocking: 与活跃 agent 调用在同一容器中并发执行,不占用 `/invocations` 通道。**官方要求 agent 创建于 2026-03-17 之后才支持此 API**。

**常用排查命令**:

```bash
# 看当前容器的环境变量 (确认 MEMORY_ID/MODEL_ID/Gateway URL 是否正确注入)
env | grep -E "MEMORY_|MODEL_|AGENTCORE_|SKILLS_"

# 检查 boto3 版本
python -c "import boto3; print(boto3.__version__)"

# 查 skill 是否已从 DynamoDB 读到
python -c "from agent.agent import load_skills_from_dynamodb; \
  print([s.name for s in load_skills_from_dynamodb('__global__')])"

# 看最近 50 行 agent 日志 (Strands 内部)
tail -n 50 /tmp/strands-*.log 2>/dev/null || journalctl -n 50 --no-pager

# 测试 Gateway MCP 端点连通性 (仅容器内可达)
curl -s -o /dev/null -w "%{http_code}\n" "$AGENTCORE_GATEWAY_SMARTHOMEDEVICECONTROL_URL"
```

**输入约束**(官方): `timeout` 1-3600s,默认 300s;`runtimeSessionId` ≥ 33 字符(本方案用 Cognito sub UUID = 36 字符);单条 `command` 上限依 AWS SDK 限制(此方案前端限 ≤ 64 KB 以匹配容器 stdin buffer)。

> ⚠️ 当前 `InvokeAgentRuntimeCommand` 权限挂在共享 Cognito 认证角色上,Admin-only 保护**仅在前端**;生产环境建议分拆 Identity Pool 为 `admin` / `user` 两组 role,已在 roadmap。

---

## 10. 其他重要事项

### 10.1 Knowledge Base 管理要点

- **文档隔离**: `__shared__/` 对所有人可见,`{email}/` 仅该用户可见。上传时 Admin API 会同步写 `*.metadata.json` sidecar 作为元数据源。
- **每次上传/删除后必须点 Sync**,触发 Bedrock `StartIngestionJob` 才会把新文档向量化(查 **Knowledge Base → Sync Status**)。
- **`user_id` 防篡改**: Agent 用本地 wrapper 替换 MCP 的 `query_knowledge_base`,从 JWT 注入 `actor_id`,LLM 无法伪造他人身份。

### 10.2 Agent Prompt 编辑的两个层级

- Global + Per-user **additive** 拼接: `effective = global + "\n\n" + user`。
- 文本 Agent 和语音 Agent 提示词**独立**(语音提示词包含 MCP 前缀的工具名 `SmartHomeDeviceDiscovery___discover_devices`),切勿把文本 prompt 直接复制到 voice 侧,否则工具路由失效。

### 10.3 Voice Agent 的限制

- Nova Sonic **单 turn 只能调一个工具**。多设备操作必须通过**复合工具** (`turn_on_all_devices`) 在 server 端打包。若需扩展 "晚餐模式" 等场景,按 `voice_session.py._build_turn_on_all_tool` 的模板封装。
- `BidiAgent` 不支持 `AgentSkills` 插件,只能把**单个** operational skill (`all-devices-on`) 内联进 system prompt;太多 skill 会让 Nova Sonic 忽略工具。

### 10.4 Memory 策略可选项

AgentCore Memory 内置 5 种策略(`SEMANTIC` / `SUMMARIZATION` / `USER_PREFERENCE` / `EPISODIC` / `CUSTOM`),本方案启用前三种。`EPISODIC` 适合对话场景多、需反思的长程任务(如家庭日程规划),后续可按需追加。长期策略为异步抽取(可能数十秒才落地),勿依赖同 session 内立即生效。

### 10.5 成本与规模

- **S3 Vectors** 替代了 OpenSearch Serverless (节省约 $350/月固定底线),按向量数+查询计费。
- **Cedar 单策略** 最多约 3,800 个 `principal.id`;超量需要切换为 group-based 策略。
- **Registry 配额**: 默认每账号 ≤ 5 registries;本方案复用一个 `SmartHomeSkillsRegistry` 同时装 `AGENT_SKILLS` 与 `A2A` 描述符。
- **Evaluator 配额**: 默认每 region ≤ 1,000 个,最多 100 个 active。

### 10.6 排障 "黄金五步"

1. **Sessions tab 确认用户 session 还活着** → 必要时 Stop 让其重建。
2. **Remote Shell 查环境变量 + skill 加载** → 80% 配置类问题在此暴露。
3. **CloudWatch Logs `aws/spans` 看 `chat` span** → token 用量、工具路径、报错 stacktrace。
4. **Tool Access 切 LOG_ONLY 重放** → 鉴别是 Cedar 拒绝还是模型没调工具。
5. **Quality Evaluation 跑一次 offline eval** → 判断回归是提示词还是模型引起。

### 10.7 变更安全清单

- 改 Prompt / Skill → DynamoDB 即时生效,不需 `agentcore deploy`。
- 改 Agent Python 代码 → 必须 `bash scripts/06-deploy-agentcore.sh`。
- 改 CDK (Lambda / IAM / API GW) → `bash scripts/04-cdk-deploy.sh`。
- 改 Cognito 用户组 / 添加 admin → Cognito 控制台直接操作,不走 CDK。

---

*最后更新: 2026-04-28*

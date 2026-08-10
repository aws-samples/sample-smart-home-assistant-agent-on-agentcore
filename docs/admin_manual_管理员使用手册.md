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
7. [模型后训练 (Model Train)](#7-模型后训练-model-train)
8. [Skill 发布/审批/下发](#8-skill-发布审批下发)
9. [Session 调试与 Remote Shell](#9-session-调试与-remote-shell)
   - [9.5 Agents 页 —— 机队总览与逐个 Agent 治理](#95-agents-页--机队总览与逐个-agent-治理)
   - [9.6 场景联动与定时自动化](#96-场景联动与定时自动化)
10. [Agent 运维统计大屏与演示前数据准备](#10-agent-运维统计大屏与演示前数据准备)
11. [其他重要事项](#11-其他重要事项)

---

## 1. AgentCore 组件一览

| 组件 | 本方案中的作用 | Admin Console 对应入口 |
|------|---------------|----------------------|
| **Runtime** | 承载 Agent 代码。共 **10 个 Runtime**:`smarthome`(文本主 Agent)、`smarthomevoice`(语音)、`smarthome_bundles`(A/B 变体)、以及 7 个 A2A 专家子 Agent | **Agents** / Sessions / Remote Shell / Prompt |
| **Runtime (A2A)** | 7 个独立子 Agent:设备控制、灯效、问答、场景编排、安防、能耗、家电保养。走标准 A2A 协议(9000 端口、挂载 `/`),**携带调用者身份**访问同一个 Gateway | **Agents**(详情页可改各自 prompt) |
| **Gateway** | MCP Server,聚合设备控制、发现、KB 检索等 Lambda 工具;执行 Cedar 策略 | Tool Policy / Integration Registry |
| **Memory** | 短期会话 + 长期事实/偏好/摘要 (三种策略) | Memories |
| **Registry** | Skill/A2A 描述符托管 + 审批工作流。**审批已搬进 Admin Console**(§8.6) | Skills → "Add approved skill from AWS Agent Registry" |
| **Policy Engine** | Cedar 策略评估 (per-user tool permit + default-deny)。**定时场景也走这条链** | Tool Policy |
| **Identity** (Cognito) | 用户认证、`principal.id` 来源 | Identity / Models / Tool Policy 的用户列表 |
| **Evaluator** | 对话质量打分、数据集评测 | Quality Evaluation 入口 |
| **Knowledge Base** (Bedrock KB + S3 Vectors) | RAG 检索,按 scope 元数据隔离 | Knowledge Base |
| **EventBridge Scheduler** | 定时场景的触发器:每个时间型场景一条 cron,加一条 5 分钟的条件巡检 | Agents(场景由 chatbot 创建) |

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

用户管理集中在 **Admin Console → Build → Identity** 页(2026-07-29 从 Overview 收敛过来):

- **新增用户**: 终端用户可在 Chatbot 自助注册(`AllowAdminCreateUserOnly=false`,邮箱验证);管理员也可在 Identity 页点 `+ Add User`,系统生成一次性显示的永久密码(不发邀请邮件)。
- **授予 / 撤销 admin 权限**: Identity 页每行的 `Make Admin` / `Remove Admin` 按钮,或在 Cognito 控制台改 `admin` 组。Admin Console 登录时校验 `cognito:groups` 声明。
- **删除用户**: Identity 页 `Delete`(带确认弹窗)。用户相关数据(技能、记忆、KB 文档)会被遗留但不删除。
- **自我保护**: 不能对自己降权或删除自己,这两个按钮会置灰。

> **⚠️ 管理员新建的用户不会自动获得工具权限。** Cognito 的 PostConfirmation 触发器只在**自助注册**时触发,`AdminCreateUser` 不触发。所以在 Identity 页新建用户后,还要去 **Tool Policy** 页手动授权,并按 §4.2 的方法复核 Cedar 策略状态。

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

1. 登录 **Admin Console → Tool Policy**(旧称 Tool Access,同一页面)。
2. 在用户表格找到 Carol,点击 `Edit`。
3. 勾选 `discover_devices`、`query_knowledge_base`,**不勾** `control_device`。
4. 点击 **Save Permissions**。Admin Lambda 会:
   - 把 Carol 的 allowed list 写入 DynamoDB (`{cognitoSub}/__permissions__`);
   - 重新扫描所有拥有 `control_device` 的用户,**不包含 Carol** 重写该工具的 `permit` 策略;
   - 对 `discover_devices` 和 `query_knowledge_base`,把 Carol 的 sub 加进 permit 白名单。
5. **Mode Toggle**: Policy Engine 有 `ENFORCE` / `LOG_ONLY` 两档。调试时切到 LOG_ONLY 观察命中情况,正式切回 ENFORCE。

> **每个 Gateway 工具旁边会列出"谁在用它"。** 撤销一个工具的影响面不止聊天:撤掉 `control_device` 会同时停掉该用户的聊天指令、**定时场景**、灯效子 Agent 和设备控制子 Agent。这一列是从各 Agent 自己声明的工具清单生成的(子 Agent 的 `tools.py` 里的 `WANTED`,主 Agent 的 `scoped_suffixes`),不是手写的表 —— 手写的表会在某个 Agent 改工具时**静默过期**,页面照样渲染,只是答案错了。
>
> | Gateway 工具 | 使用者 |
> |---|---|
> | `control_device` | `smarthome`、`sha2adevice`、`sha2alight` |
> | `discover_devices` | `smarthome`、`sha2adevice`、`sha2alight` |
> | `query_device_state` | `smarthome`、`sha2adevice`、`sha2alight` |
> | `query_sensor_history` | `smarthome`、`sha2adevice` |
> | `query_knowledge_base` | `smarthome`、`sha2aqa` |
> | `navigate_to_page` | `smarthome` |
>
> **定时场景受同一套授权约束** —— 这是刻意设计的,不是副作用。执行 Lambda 完全没有 IoT 权限,它以场景所属用户的身份过 Gateway,所以撤销 `control_device` 之后该用户的 07:30 自动化也会停(实测验证过:撤销 → 策略 ACTIVE → 触发 → 被拒、设备未动、拒绝原因写回场景行)。

> **⚠️ 保存成功不等于授权生效 —— 一定要复核策略状态。**
>
> 页面提示保存成功(API 返回 200)、DynamoDB 也写进去了、Cedar 语句里确实能看到该用户的 sub —— 但策略仍可能落到 `UPDATE_FAILED`。一旦如此,**Gateway 会对该用户返回 0 个工具**,Agent 表现为"抱歉,这超出我的知识范围",看起来像模型能力不足,实际是授权链断了。而且全链路没有任何报错:API 200、Cedar 内容正确、连 `DenyDecisions` 都是 0(请求根本没走到授权评估)。
>
> 复核方法(授权后等约 75 秒,`UPDATING` 是正常中间态,`UPDATE_FAILED` 不是):
>
> ```bash
> python3 - <<'PY'
> import boto3
> c = boto3.client('bedrock-agentcore-control', region_name='us-west-2')
> PE = 'SmartHomeUserPermissions-xxxxx'   # 换成实际 policyEngineId
> for p in c.list_policies(policyEngineId=PE)['policies']:
>     f = c.get_policy(policyEngineId=PE, policyId=p['policyId'])
>     print(p['name'], f['status'], f.get('statusReasons'))
> PY
> ```
>
> 若看到 `Insufficient permissions to call gateway`,说明 admin Lambda 的 role 缺 `bedrock-agentcore:InvokeGateway`(2026-08-02 已在 CDK 中修复;历史部署需重新 `cdk deploy`)。修好后对受影响用户重新保存一次权限即可。
>
> 另外:**`tools/list` 不是可靠的排查手段** —— 它对策略已 ACTIVE、且实际能正常用工具的用户仍会间歇返回空列表。权威判断只能是真实发一次对话(例如让 Agent 开风扇)。

### 4.3 试用与演示

`Tool Policy` 每行有 **Demo Links** 列,一键跳 `chatbot?username=carol@example.com`(已预填邮箱),管理员只需输入密码即可模拟该用户体验。

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

通过 **Admin Console → Assess → Optimization** 页面,管理员可以基于真实运行轨迹自动优化 system prompt 与 gateway tool description。底层调用 AWS **AgentCore Optimization**(公开预览)的三大能力:**Recommendations**(LLM 生成的优化建议)、**Configuration Bundles**(可版本化的配置快照)、**A/B Tests**(线上流量分流 + 在线评估)。完整设计见 `docs/architecture-and-design.md` §8.12。

### 6.1 工作流概览

```
            Generate                Apply                  Start A/B Test
agent traces ────────▶ Recommendation ─────▶ Bundle version ─────▶ Live traffic split
   (aws/spans)          (system prompt /        (DDB __prompt_*__         (Gateway routes
                         tool description)       + AgentCore bundle)       sessions sticky-by-id)
                                                          │                       │
                                                          ▼                       ▼
                                                   Effective on next        Online evaluator
                                                     invocation              p-value / winner
```

- **Recommendations** — Lambda 调用 `start_recommendation`,把 `aws/spans` 中过去 N 天的 trace + 一个 evaluator(默认 `Builtin.GoalSuccessRate`)交给 AgentCore,数分钟后返回优化后的 prompt 或 tool description。
- **Apply** — 一键写回到现有的 `__prompt_text__` / `__prompt_voice__` DynamoDB 行(下次 invocation 就生效),同时创建一个新的 Configuration Bundle 版本作为审计 + 回滚 + A/B 候选。
- **Configuration Bundles** — AgentCore 端的不可变版本链。每次 Apply 自动产生一个新版本;A/B Test 直接引用版本 ID。
- **A/B Tests** — `create_ab_test` 在 Gateway 上按 sessionId 粘性分流。在线评估打分;`get_ab_test` 返回 per-variant mean / sample size / p-value / 是否显著。Stop 即调用 `update_ab_test(executionStatus="STOPPED")`。

### 6.2 管理员操作步骤

1. **生成建议**:Optimization → Recommendations → **Generate Recommendation**。选择 Evaluator(GoalSuccessRate / Helpfulness / Correctness)+ trace 时间范围 + scope(global 或某个用户) + agent type(text / voice / tool_desc)。提交后状态由 PENDING → IN_PROGRESS → COMPLETED / FAILED(轨迹不足时正常返回 FAILED)。
2. **审阅 + 应用**:点击 **View** 打开侧抽屉,左右对比当前 prompt 与推荐 prompt;tool_desc 类型则展示每个工具的新描述。点击 **Apply** 一键应用,UI 会同步生成 bundle 版本号 toast。
3. **启动 A/B(可选)**:Optimization → A/B Tests → **Start A/B Test**。选择 control bundle 版本(应用前的当前版本)和 treatment bundle 版本(刚应用产生的新版本),设置流量比 50/50 或 90/10、运行时长 1/3/7/14 天。系统强制单 agentType 同时仅一个 RUNNING 测试。
4. **观察 + 收敛**:列表行实时显示 p-value 与 winner;**View in CloudWatch** 跳转 GenAI Observability 仪表盘看每个 session 的轨迹。当结果显著后,**Stop**;若 winner 是 treatment,则保留当前 prompt;若 winner 是 control,Apply 控制版本回滚。

### 6.3 与 Agent System Prompts 页的关系

Agent System Prompts 页(§5 / Build → Prompt)的每张编辑卡顶部保留了一条信息提示:"Optimization Suggestions (AgentCore Optimization)" + 一个 "Open in Optimization tab →" 链接。两个页面共用 `__prompt_text__` / `__prompt_voice__` 存储,Apply 写入即对 Prompt 编辑器立即可见。

### 6.4 区域可用性

AgentCore Optimization 当前为公开预览,部分区域尚未开放。Lambda 在导入时探测 `bedrock-agentcore.StartRecommendation` 操作模型;如果 boto3 不识别,优化页所有 API 返回 501 `AgentCoreOptimizationUnavailable`,UI 显示一条单独的 banner 而不是错误,等服务在该区域上线后无需改动即可恢复。

### 6.5 故障排查

- **"No sessions found in the specified time window"** — 选择的 trace 时间窗口内没有匹配的 Strands span(常见于刚部署、还没产生用户会话或选了未来日期)。扩大时间窗口或先在聊天机器人里跑几个真实 turn 再生成。
- **CORS / Failed to fetch** — 已修复;若再次出现,确认管理员控制台 CloudFront 已失效缓存(`aws cloudfront create-invalidation --distribution-id ...`)且新 bundle 已上线。
- **A/B 启动 409 Conflict** — 同一个 agentType 已经有 RUNNING 测试,先 Stop 旧的再启动新的。

---

## 7. 模型后训练 (Model Train)

AWS 即将推出 **AgentCore Model Train**,提供低代码方式对 **open-weight 模型**进行后训练优化(SFT / DPO / Distillation 等),只需几行代码即可集成到现有 agent 工程。正式发布后,管理员可直接在 Admin Console → Models tab 触发训练任务并将产出的 Custom Model 灰度下发给指定用户。

---

## 8. Skill 发布/审批/下发

### 8.1 三段式流水线

```
  终端用户                 管理员                    Agent
───────────          ─────────────────           ─────────────
Skill ERP          AWS Agent Registry          Runtime / Gateway
(自助发布)          (审批控制台)               (动态加载)
     │                    │                          │
CreateRegistryRecord      │                          │
     │───────────────────►│                          │
SubmitForApproval         │                          │
     │ ==> PENDING_APPROVAL                          │
      Admin Console: Skills → 待审批队列 (§8.6)      │
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
| 2 | Skill ERP 后端 | **AWS Agent Registry** | `CreateRegistryRecord(descriptorType="AGENT_SKILLS")` → 轮询等 `CREATING` → `SubmitRegistryRecordForApproval` ⇒ `PENDING_APPROVAL` |
| 3 | 审批员 (Admin) | **Admin Console → Skills** | 点 `Add approved skill from AWS Agent Registry` → 顶部**待审批**队列里审阅 → `Approve` 或 `Reject`(驳回必须填原因,见 §8.6)。也可用 CLI: `aws agent-registry-control update-registry-record-status`;可配合 EventBridge 接入工单/审批机器人 |
| 4 | Admin | **同一个弹窗** | 批准后记录出现在下方"可导入"列表 → 勾选 `air-purifier-control` → 选择 scope `__global__` → `Import` |
| 5 | Agent | Runtime | 下一次 `/invocations` 时 `load_skills_from_dynamodb("__global__")` 自动拉到新 skill,无需重启 |

**验证**: 在 Chatbot 里问 "把空气净化器开到自动模式",观察 Agent 是否激活 `air-purifier-control` skill、工具调用是否正确。

### 8.3 修改一个已存在的 Skill (典型两种路径)

**路径 A - 用户自己的草案改版**:

1. Skill ERP → My Skills → 编辑 → Save → Lambda 执行 `UpdateRegistryRecord` + 重新 `SubmitRegistryRecordForApproval` → 状态回到 `PENDING_APPROVAL`。
2. 审批员在 **Admin Console → Skills → Add from Registry → 待审批** 里 Approve(§8.6)。
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

### 8.6 审批 / 驳回 Skill(在 Admin Console 里做)

审批状态机由 Registry 托管,但在此之前**仓库里没有任何调用方** ——
`agent-registry:UpdateRegistryRecordStatus` 早已授给 admin Lambda 却从未被调用,
所以用户从 Skill ERP 发布的 skill 会停在 `PENDING_APPROVAL`,只能去 AWS 控制台推进。
现在这一步在 Admin Console 里完成。

**操作路径**: Admin Console → **Skills** → `Add approved skill from AWS Agent Registry`
→ 弹窗顶部的 **待审批** 区块。

| 动作 | 效果 | 注意 |
|------|------|------|
| **Approve** | → `APPROVED`,记录随即出现在下方"可导入"列表 | 仍需再点 Import 才会写进 skill 目录 |
| **Reject** | → `REJECTED` | **必须填原因** |
| Deprecate(API 支持,UI 暂未暴露) | → `DEPRECATED` | DRAFT 记录唯一的退出路径 |

**为什么驳回必须填原因**: `statusReason` 是 Registry 里唯一记录"为什么"的字段,
也是 skill 作者唯一能看到的反馈。不写原因的驳回,对作者来说和"系统把我的东西弄丢了"
没有区别。审批人的邮箱会自动附加在原因后面,所以记录同时回答了"是谁批的"。

**状态机(在真实记录上实测得出,不是照文档抄的)**:

```
DRAFT             → PENDING_APPROVAL | DEPRECATED | DRAFT     (不能直接 REJECTED)
PENDING_APPROVAL  → APPROVED | REJECTED
REJECTED          → APPROVED                                  (可逆)
```

所以:

- **DRAFT 记录无法驳回**。API 会报错,但错误信息只是一串枚举值
  ("PENDING_APPROVAL, DEPRECATED, DRAFT, UPDATING"),看不出"其实是还没提交审批"。
  界面把它翻译成一句人话,并提示改用 deprecate。
- **驳回是可逆的**。审批人改主意不必让作者重新发布 —— 待审批队列里同时列出
  `PENDING_APPROVAL` 和 `REJECTED`,已驳回的行只显示 Approve 按钮。

**作者侧**: 驳回原因会显示在 Skill ERP 的 `My Skill Records` 表格里,状态下方一行红字。

---

## 9. Session 调试与 Remote Shell

**Sessions** tab 包含:User / Kind(Text/Voice)/ Session ID / Last Active / 7d Total Tokens / **Remote Shell** / Stop。

> **Token 数现在会标出归属的 agent**(悬停看逐个 agent 的拆分)。之前是一个没有归属的
> 数字 —— 九个 Runtime 往同一个日志组里写,合成一个数就没法回答"这个 agent 花了多少"。
>
> 一个已知口径限制:**子 Agent 会打自己的 session id**(一串裸 UUID,不是主 Agent 的
> `user-session-*`)。AgentCore 的 `runtimeSessionId` 是按 Runtime 分配的,A2A 这一跳
> 不传递它,所以一次委派产生的 token 落在本表没有对应行的 session 下。
> **按 agent 的总量是准的,跨委派的按轮次归因目前拿不到。**

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

## 9.5 Agents 页 —— 机队总览与逐个 Agent 治理

"Agent" 以前不是管控面里的一等实体:一等实体是 Cognito 用户和 skill,agent 只是
prompt 和 optimization 两个页面里 `text | voice` 的二选一。现在有 1 主 + 6 子 +
1 语音 + 1 A/B 变体 + 1 Tool,`#/agents` 回答"有哪些 agent、跑在哪、有没有出问题"。

**列表是推导出来的,不是维护出来的** —— 由 Runtime ARN + Registry 已批准记录 +
可选的元数据行三方按 runtime 名 join。所以新部署一个子 Agent 会自动出现,不需要改前端。

| kind | 含义 |
|------|------|
| `Orchestrator` | 主 Agent,用户直接对话的入口 |
| `Specialist` | 通过 A2A 委派到的子 Agent |
| `Voice` | 语音 Runtime |
| `A/B variant` | `smarthome_bundles` —— 主 Agent 的同一镜像 + `ENABLE_BUNDLE_HOOK=1`,只在 `ab-bundles` 模式下被访问。列成第二个主 Agent 会夸大机队规模,隐藏则它的 token 花费无法归因 |
| `Tool` | 导航 DeepLink —— 是 Gateway Lambda target 而非 agent,但它是客户架构里七个实体之一,管理员找它时应该在这里找到 |

### 9.5.1 "No runtime" 告警是真信号,不是噪音

一行显示 `No runtime`,意思是 Registry 里有已批准记录但 ARN 白名单里没有对应
Runtime。两种可能:记录成了孤儿,或者某次部署漏跑了 `patch-text-agent`,
`DASHBOARD_EXTRA_RUNTIME_ARNS` 没学到这个 ARN。**后者用别的方式看不出来** ——
这个告警第一次上线就抓到了一例(3 个子 Agent 不在白名单里)。

处理: `cd a2a-agent-registry && python deploy.py --only patch-text-agent`。

### 9.5.2 逐个 Agent 改 prompt

点列表里的 agent 名进详情页:卡片元数据、公开的 skill、实时指标,以及**该 agent 的
system prompt**。编辑器与 Prompt 页是同一个组件,所以"恢复默认"和"按用户追加"
两个语义在两处不会漂移。

- 子 Agent 的 prompt 以 **AgentCard 名**为 key(`__prompt_light-effect-agent__`),
  这是控制台、Registry 记录、运行中的容器三方各自独立推导出来、且一致的唯一标识。
- **保存后下一次请求即生效**,不用重新部署容器。运行时每次请求都读一遍,故意不加缓存:
  加了 TTL 就会出现"我明明存了但看起来被忽略"的现象,而那正是这个功能要消灭的问题。
- 全局覆盖是**替换**镜像里自带的 prompt;按用户覆盖是**追加**在全局结果之后。
- `Tool` 和 `A/B variant` 两行会说明自己为什么没有 prompt,而不是给一个坏掉的编辑器。

> **改 prompt 时不要删掉路由标记的相关约定**,但也不必自己写它 —— 标记
> `⟦A2A:<domain>⟧` 由服务端加在**结果**上,不依赖模型遵守 prompt。早期只对带工具的
> agent 这么做,理由是纯 prompt agent 会自己稳定输出;prompt 变成可编辑之后这个假设
> 就不成立了(全局覆盖会把那条指令一起替换掉),所以现在对所有 agent 无条件加。

---

## 9.6 场景联动与定时自动化

用户在 chatbot 里说"每天晚上 11 点关灯、风扇调到 1 档",主 Agent 委派给
**场景编排子 Agent**,后者把它存成一个场景(触发器 + 设备动作),
EventBridge Scheduler 到点触发执行。

### 9.6.1 授权链:定时任务不是后门

```
EventBridge Scheduler
  ├── 每个时间型场景一条 cron
  └── 一条 5 分钟巡检(阈值型触发器没有"点"可以定)
        ↓
  smarthome-scenario-runner  ← 完全没有 IoT 权限
        ↓  用场景所属用户的身份
  Gateway → Cedar → iot-control → MQTT
```

参考设计里写的是"Lambda 直接调 iot-control,不经过 LLM"。那样定时场景就会成为
**唯一一条 Cedar 看不到的设备控制路径** —— 管理员在 Tool Policy 里撤销某用户的
`control_device`,他的聊天指令会停,但 07:30 的自动化不会。所以执行 Lambda 走
Gateway、以用户身份、受同一套策略约束。

### 9.6.2 定时执行需要一份用户凭证(这是真实的新增攻击面)

以"不在线的用户"的身份执行需要凭证,几个方案都实测过:

| 方案 | 结果 |
|------|------|
| `GetWorkloadAccessTokenForUserId` | 形态正确(无需用户在线即可为某 userId 换 token),但 **Gateway 返回 401 `Invalid Bearer token`** —— 它是 KMS 加密的不透明 AgentCore token,不是带正确 audience 的 JWT |
| 直接问 Cedar | 没有公开 API(Verified Permissions 是另一个服务,AgentCore 的 `AuthorizeAction` 是 Gateway 内部动作) |
| Cognito refresh token → `REFRESH_TOKEN_AUTH` | 能换出 Gateway 接受的真 idToken(实测 200,返回 6 个经 Cedar 过滤的工具) |

所以存的是 refresh token,**这确实是一份 30 天有效的用户凭证落在了系统里**。
对应的收敛措施:

- 存在 Secrets Manager,专用的客户托管 KMS 密钥(已开启轮换);
- **一个用户一个 secret**(`smarthome/scenario-tokens/{sub}`),可单独吊销;
- 只有执行 Lambda 的 role 能读,且 `kms:Decrypt` 用 `kms:ViaService` 收窄;
- **绝不写日志**;
- 没有存 token 的用户,其定时场景直接不执行(fail closed)。

> 只给 Secrets Manager 权限是不够的:用客户托管密钥时 `GetSecretValue` 会被 **KMS**
> 拒绝,而表象是"没有可用的调度凭证",看起来像 secret 不存在而不是缺权限。

### 9.6.3 触发器支持哪三种

| 类型 | 例子 | 说明 |
|------|------|------|
| `time` | 每天 23:00 | 24 小时制 `HH:MM`,**按 UTC 调度** —— schema 里还没有时区字段,猜一个偏移会让场景在用户没说过的时间触发,而且比统一用 UTC 难发现得多 |
| `device_state` | 风扇打开时 | subject 必须是真实 device id |
| `sensor` | 温度高于 27 | 真传感器阈值(`temperature` / `humidity` / `pm25` / `co2`)。**必须显式写 above 还是 below** —— "高于 26"和"低于 26"是两个相反的场景,猜错会让它在完全错误的时机触发 |

条件型触发器按**边沿**触发而非电平:场景行上的 `lastReading` 保证"温度高于 27"
只在跨过阈值时执行一次,而不是整个下午每 5 分钟执行一次。

### 9.6.4 运维要点

- **场景存在独立的 `smarthome-scenarios` 表**,不在万能表 `smarthome-skills` 里。
  原因是授权而非整洁:场景 Agent 需要**写**权限,而万能表里放着治理它自己的
  `__permissions__` 和 `__prompt_*__` 行 —— 能改这些的 agent 等于自己管自己。
- **改动场景后需要对账 Scheduler**:调用
  `GET /registry/records?action=sync-schedules`。它是**对账**而不是增量更新,
  所以"场景删了但 schedule 还在空转"这种孤儿会在下次对账时自愈。
- **`lastRunAt` / `lastRunOk` 写在场景行上**。没有这个,"07:30 到底跑了没有"
  只能翻 CloudWatch —— 而那个时间点没人在看。
- 想立刻验证一个时间型场景,不要等真实时间:直接用 Scheduler 里那条 schedule 的
  payload 调一次执行 Lambda(它是幂等的,接受的就是 Scheduler 发的同一个入参)。
  设备模拟器里的**虚拟时钟**只加速模拟器自身的时间(传感器曲线 + 屏幕上的钟),
  **不会**改变 AWS 侧的真实触发时间。

---

## 10. Agent 运维统计大屏与演示前数据准备

### 10.1 大屏在哪、看什么

**Admin Console → Discover → Overview**,在 Demos 下方。按运维监控大屏布局:顶部一条六信号状态条(活跃会话 / TTFT P95 / 错误率 / QPS / Token 合计 / 评估质量均分),下面三行成对面板。

- **时间范围**:24h / 7d / 30d 分段切换。
- **成本归因维度**:按用户 / 按入口环境 / 按 Agent 运行时。
- **每张图都有表格视图**,数值不必靠悬浮才能看到。
- **数据缓存 5 分钟**,右上角"刷新"可强制重新聚合。

### 10.2 哪些是真实数据,哪些是模拟

带 **演示数据** 标记的卡片是模拟值,点标记有说明"要变成真实数据需要什么":

| 卡片 | 真实性 | 说明 |
|------|--------|------|
| 实时健康 | ✅ 真实 | 但 **活跃会话数是账号级**(CloudWatch 的 `ActiveSessionCount` 没有按 Runtime 拆分的维度) |
| Token 成本趋势/归因 | ✅ Token 真实 | **美元成本无法按用户或 Agent 拆分**(Cost Explorer 只到账号级),所以只归因 Token 数量 |
| 成本预算消耗 | ❌ 模拟 | 项目没有计费模块 |
| 评估通过率与漂移 | ✅ 单变体真实 | **A/B 对比目前无数据**(配置存在但 A/B test 已 STOPPED),显示空状态而非编造曲线 |
| 活跃版本与发布状态 | ✅ 版本真实 | **灰度阶段是推导值**,由 Gateway A/B test + `tenant_env` 推出,不是 AgentCore 原生字段 |
| 用户满意度 | ❌ 模拟 | Chatbot 目前没有赞踩埋点;最接近的真实替代是评估卡里的 Helpfulness / GoalSuccessRate |

> **口径提醒**:TTFT 不存在于 CloudWatch 指标中,只能从 `aws/spans` 里 Strands `chat` span 的 `gen_ai.server.time_to_first_token` 取,所以这部分加载要 5-20 秒(快指标先出,Token 卡片后填充)。错误率在窗口内无流量时显示 `--` 而不是 `0%`。

### 10.3 演示前准备:生成模拟数据(每次演示必做)

刚部署完、或者环境闲置几天后,大屏和 AgentCore Evaluation 都是空的 —— 它们只反映**真实流量**。演示前用模拟用户脚本跑一遍,大屏就会有完整数据。

`scripts/simulate-users.py` 会创建几个测试用户,让它们像真实用户一样和 Agent 对话,覆盖 Agent 的全部功能。测试用户通过 Cognito 登录、走与聊天机器人**完全相同**的 SigV4 `/invocations` 路径,所以产生的 span、Token、会话和评估分与真实流量**无法区分** —— 不是往数据库里塞假数据。

#### 前置条件

| 条件 | 说明 |
|------|------|
| 已完成 `./deploy.sh` | 脚本从 `cdk-outputs.json` 和 admin Lambda 的环境变量读取配置 |
| venv 已激活 | 只依赖 `boto3` + `requests`,均已在 venv 中 |
| `SIM_USER_PASSWORD` | 必须设置,不硬编码在仓库里。需满足 Cognito 密码策略:≥8 位,含大写、小写、数字、符号 |

#### 标准演示前流程

```bash
cd <repo-root>
source venv/bin/activate
export SIM_USER_PASSWORD='SomeStrong#Pass1'

# ① 创建并配置 5 个 persona(幂等 —— 已存在则复用,可反复跑)
python3 scripts/simulate-users.py setup

# ② 生成对话数据(轻量层,23 轮对话,约 1.5-3.5 分钟)
python3 scripts/simulate-users.py run

# ③ 等 2-3 分钟,然后打开 Admin Console → Overview,时间范围切 24h
```

**演示要展示 code-interpreter 或 browser-use 时**,追加跑一次 heavy 层(实测约 190 秒):

```bash
python3 scripts/simulate-users.py run --heavy --personas dave,erin
```

#### 四个子命令

| 命令 | 作用 | 耗时 |
|------|------|------|
| `setup` | 建用户 → 授权工具 → **等 Cedar 策略变 ACTIVE** → 写差异化配置(模型 / 入口环境) | 约 1-2 分钟(大部分在等 Cedar) |
| `run` | 按 persona 并发跑场景,每轮记录 JSONL,结束打印汇总表 | 轻量 1.5-3.5 分钟(实测 97s 热 / 212s 含冷启动);`--heavy` 再加约 3 分钟 |
| `status` | 列出现有模拟用户及其模型 / 入口环境 / 权限数,以及 Cedar 策略状态和已记录轮数 | 数秒 |
| `teardown --yes` | 回收授权 + 删除用户(**只删 `simuser+` 前缀**) | 约 1 分钟 |

常用参数(注意归属的子命令不同):

| 参数 | 属于 | 作用 |
|------|------|------|
| `--personas alice,bob` | `setup` / `run` | 只处理指定 persona(默认全部 5 个) |
| `--heavy` | `run` | 追加 code-interpreter 和 browser-use 场景 |
| `--rounds N` | `run` | 重复整套场景 N 次,想要更多数据点时用 |
| `--no-grant erin` | `setup` | 故意不给某个 persona 授权,用来产生"工具不可用"的真实错误数据 |
| `--yes` | `teardown` | 跳过确认提示 |

#### 5 个 persona 覆盖什么

每个 persona 刻意配了不同的模型、入口环境和场景侧重,这样大屏的成本归因图表会出现**多行真实数据**,而不是全塞进一个桶 —— 这正是演示"千人千面"要看到的效果。

| persona | 模型 | 入口环境 | 覆盖的功能 |
|---------|------|---------|-----------|
| `alice` | Opus 4.6 | default | 四类设备控制(LED / 风扇 / 烤箱 / 电饭煲)、设备发现、一键全开 |
| `bob` | Sonnet 4.6 | default | 企业知识库检索、天气查询(`http_request`)、越界拒答 |
| `carol` | Haiku 4.5 | ab-bundles | 多轮记忆延续(第三轮要求复现前两轮偏好)、用户反馈技能 |
| `dave` | Kimi K2.5 | ab-targets | code-interpreter 数据分析 + 绘图(heavy) |
| `erin` | Sonnet 4.5 | default | browser-use 真实网页操作(heavy)、拒答、模糊指令澄清 |

实测单轮耗时:轻量场景 3-25 秒;heavy 场景 19-141 秒(browser-use 是最慢的那个,且会占用真实 DCV 浏览器会话)。persona 之间并发跑,单个 persona 内部串行(对话本身有先后顺序)。

#### 跑完检查什么

打开 **Admin Console → Overview**,时间范围切 **24h**,确认:

- **状态条**有值:活跃会话数、TTFT P95、Token 消耗合计、评估质量均分
- **Token 成本归因**切"按用户"能看到多个 `simuser+*` 行;切"按入口环境"能看到 default / ab-bundles / ab-targets;切"按 Agent 运行时"只会看到 `smarthome_smarthome.DEFAULT` **一行** —— 模拟流量全部走 text runtime,这是预期的(表格视图能看到该运行时用过的多个模型)
- **评估通过率与漂移**表格里有 8 个评估器出分
- **错误率**应该是 0.0%(若明显偏高,见下方排障)

运行日志在 `scripts/sim-results/{persona}.jsonl`(已 gitignore),每行含耗时、HTTP 状态、回复片段、是否命中工具 —— 排查某轮为什么失败时看这个。

#### 排障

| 现象 | 原因与处理 |
|------|-----------|
| `SIM_USER_PASSWORD is not set` | 忘了 export;或密码不满足 Cognito 策略 |
| `setup` 报 Cedar 策略未全部 ACTIVE | 授权没生效 —— 见 [§4.2](#42-c-端用户权限配置运维管理员操作路径) 那个"保存成功不等于生效"的坑。**别跳过这一步直接 `run`**,否则会产出一堆"工具不可用"的假数据 |
| 大屏还是空的 | ①等 2-3 分钟(CloudWatch 摄取延迟);②大屏有 5 分钟缓存,点右上角"刷新"强制重算;③确认时间范围是 24h 而不是 7d |
| 汇总表里有 err | 首轮常见(Runtime 冷启动),脚本会自动重试一次。持续失败查对应 JSONL 里的 `error` 字段 |
| `AGENT_RUNTIME_ARN missing from the admin Lambda env` | 单独跑过 `cdk deploy` 会把这个环境变量重置成占位符。重跑 `bash scripts/06-deploy-agentcore.sh` 修复 |
| 某个 Runtime(voice / A2A / bundles)的 Token 不出现在大屏上 | 该 Runtime 没进白名单。span 与评估指标上的 `service.name` 是**精确匹配**,大屏只聚合 `AGENT_RUNTIME_ARN` + `VOICE_AGENT_RUNTIME_ARN` + `DASHBOARD_EXTRA_RUNTIME_ARNS` 这三个环境变量推导出的 Runtime。修复:重跑 `bash scripts/06-deploy-agentcore.sh`(会补上 bundles runtime),A2A 则重跑 `python a2a-agent-registry/deploy.py --only patch-text-agent`。**注意**:2026-08-05 之前部署的环境没有 `DASHBOARD_EXTRA_RUNTIME_ARNS`,升级后必须重跑一次才会生效 |

#### 安全边界

一切都限定在 **`simuser+` 邮箱前缀**内,`Provisioner._guard()` 对其他邮箱直接抛异常 —— `teardown` **不可能**误删真实用户。`setup` 是幂等的:已存在的用户会复用而不是重建,所以反复跑不会在 Cognito 里堆垃圾账号。

演示结束后建议 `teardown --yes` 清理,保持用户池干净;不清理也不影响下次 `setup`。

更多实现细节见 [`scripts/sim/README.md`](../scripts/sim/README.md) 与[架构文档 §9.16](./architecture-and-design.md#916-simulated-end-users-test-data-generation)。

---

## 11. 其他重要事项

### 11.1 Knowledge Base 管理要点

- **文档隔离**: `__shared__/` 对所有人可见,`{email}/` 仅该用户可见。上传时 Admin API 会同步写 `*.metadata.json` sidecar 作为元数据源。
- **每次上传/删除后必须点 Sync**,触发 Bedrock `StartIngestionJob` 才会把新文档向量化(查 **Knowledge Base → Sync Status**)。
- **`user_id` 防篡改**: Agent 用本地 wrapper 替换 MCP 的 `query_knowledge_base`,从 JWT 注入 `actor_id`,LLM 无法伪造他人身份。

### 11.2 Agent Prompt 编辑的两个层级

- Global + Per-user **additive** 拼接: `effective = global + "\n\n" + user`。
- 文本 Agent 和语音 Agent 提示词**独立**(语音提示词包含 MCP 前缀的工具名 `SmartHomeDeviceDiscovery___discover_devices`),切勿把文本 prompt 直接复制到 voice 侧,否则工具路由失效。

### 11.3 Voice Agent 的限制

- Nova Sonic **单 turn 只能调一个工具**。多设备操作必须通过**复合工具** (`turn_on_all_devices`) 在 server 端打包。若需扩展 "晚餐模式" 等场景,按 `voice_session.py._build_turn_on_all_tool` 的模板封装。
- `BidiAgent` 不支持 `AgentSkills` 插件,只能把**单个** operational skill (`all-devices-on`) 内联进 system prompt;太多 skill 会让 Nova Sonic 忽略工具。

### 11.4 Memory 策略可选项

AgentCore Memory 内置 5 种策略(`SEMANTIC` / `SUMMARIZATION` / `USER_PREFERENCE` / `EPISODIC` / `CUSTOM`),本方案启用前三种。`EPISODIC` 适合对话场景多、需反思的长程任务(如家庭日程规划),后续可按需追加。长期策略为异步抽取(可能数十秒才落地),勿依赖同 session 内立即生效。

### 11.5 成本与规模

- **S3 Vectors** 替代了 OpenSearch Serverless (节省约 $350/月固定底线),按向量数+查询计费。
- **Cedar 单策略** 最多约 3,800 个 `principal.id`;超量需要切换为 group-based 策略。
- **Registry 配额**: 默认每账号 ≤ 5 registries;本方案复用一个 `SmartHomeSkillsRegistry` 同时装 `AGENT_SKILLS` 与 `A2A` 描述符。
- **Evaluator 配额**: 默认每 region ≤ 1,000 个,最多 100 个 active。

### 11.6 排障 "黄金五步"

1. **Sessions tab 确认用户 session 还活着** → 必要时 Stop 让其重建。
2. **Remote Shell 查环境变量 + skill 加载** → 80% 配置类问题在此暴露。
3. **CloudWatch Logs `aws/spans` 看 `chat` span** → token 用量、工具路径、报错 stacktrace。
4. **Tool Policy 切 LOG_ONLY 重放** → 鉴别是 Cedar 拒绝还是模型没调工具。
5. **Quality Evaluation 跑一次 offline eval** → 判断回归是提示词还是模型引起。

### 11.7 变更安全清单

- 改 Prompt / Skill → DynamoDB 即时生效,不需 `agentcore deploy`。
- 改 Agent Python 代码 → 必须 `bash scripts/06-deploy-agentcore.sh`。
- 改 CDK (Lambda / IAM / API GW) → `bash scripts/04-cdk-deploy.sh`,**然后必须再跑
  `python scripts/setup-agentcore.py`** —— 见下条。
- 改 Cognito 用户组 / 添加 admin → Cognito 控制台直接操作,不走 CDK。

### 11.8 ⚠️ `cdk deploy` 会静默抹掉 admin Lambda 的一半环境变量

admin Lambda 的环境变量来自两处:CDK 声明 7 个,`setup-agentcore.py` 在部署后补 10 个
(`GATEWAY_ID`、`MEMORY_ID`、`REGISTRY_ID`、`DASHBOARD_EXTRA_RUNTIME_ARNS`、
7 个 `OPTIMIZATION_*` / `AB_TEST_*` ARN),因为它们指向 synth 时还不存在的资源。

CloudFormation 里 `environment` 是**整张表**,所以任何一次 `cdk deploy` 都会把函数重置回
CDK 的那 7 个,其余全部丢失。**全过程没有任何报错**,而症状离病因很远:

| 丢失的变量 | 表象 |
|---|---|
| `GATEWAY_ID` | `/tools` 只返回内置工具 → **Tool Policy 弹窗里一个 Gateway 工具都没有**,管理员根本无法授权/撤销 `control_device` |
| `REGISTRY_ID` | A2A 目录为空 → 保存权限时报 "recordId … is not an approved A2A record";或 `registryId` 正则校验失败 |
| `OPTIMIZATION_*` | `/optimization/*` 返回 ConfigurationError |
| `DASHBOARD_EXTRA_RUNTIME_ARNS` | 大屏漏掉子 Agent 的 token;**Optimization 的 agent 下拉框认不出子 Agent**(报 "agentType must be text&#124;voice&#124;tool_desc") |

**修复**: 重跑 `python scripts/setup-agentcore.py`(它是往现有环境变量上合并,不是覆盖),
然后 `cd a2a-agent-registry && python deploy.py --only patch-text-agent` 补回 A2A 的
8 条 Runtime ARN。

**核对**:

```bash
aws lambda get-function-configuration --function-name smarthome-admin-api \
  --query "length(Environment.Variables)"      # 期望 28,不是 14
```

`cdk/lambda/admin-api/tests/test_env_contract.py` 记录了哪一侧拥有哪个变量,
新增变量时按它选边。

---

*最后更新: 2026-08-10*

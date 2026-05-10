# A2A 示例 Agent

三个部署到 AgentCore Runtime 的 A2A (Agent-to-Agent) 示例 agent, 注册到 AgentCore Registry:

- `energy-optimization-agent` — Nova Lite, 能源节省与电价分析
- `home-security-agent` — Nova Lite, 安全风险评估与应急响应
- `appliance-maintenance-agent` — Nova Lite, 家电保养与故障排查

当用户的授权允许时, smarthome 文本 agent 会通过 A2A 协议调用这些 agent。设计细节见 `docs/superpowers/specs/2026-05-10-a2a-agents-for-text-agent-design.md`。

## 目录结构

```
a2a-agent-registry/
├── common/                         # 共享的 A2A server 代码 (Starlette + a2a-sdk + JWT 中间件)
├── energy-optimization/            # agent.py + system_prompt.md + card.json + Dockerfile
├── home-security/                  # 同上
├── appliance-maintenance/          # 同上
├── deploy.py                       # 幂等部署脚本 (无需本地 docker; agentcore CLI 走 CodeBuild)
├── teardown.py                     # 反向清理脚本
├── demo_reset.py                   # 单 agent 演示重置
├── approve_records.py              # 测试用一键审批 (生产请走 AWS Console)
├── smoke_test.py                   # 直连 A2A 的烟测, 绕过 Registry
└── deployed-state.json             # deploy.py 写入, teardown.py 消费
```

## 前置条件

- `./deploy.sh` 已成功执行 —— CDK 栈 + smarthome Runtime + AgentCore Registry 必须先存在
- 已安装 `agentcore` CLI (`scripts/01-install-deps.sh` 已装)
- **不需要** 本地 docker daemon —— agentcore CLI 在 AWS 侧起 CodeBuild

## 部署

```bash
cd a2a-agent-registry

# 全量部署 (3 个 agent + Cognito m2m + Registry 注册 + 打补丁到 text agent env)
python deploy.py

# 单个 agent (只构建/刷新这一个)
python deploy.py --agent energy-optimization

# 跳过重型步骤 (比如仅刷新 Cognito 凭据, 不重新构建)
python deploy.py --only cognito
```

脚本执行完后, 3 条 A2A 记录状态为 `PENDING_APPROVAL`。打开 AWS Console → Bedrock AgentCore → Registry 逐条审批。审批后它们会出现在 Admin Console 的 `Discover → Integration Registry → A2A Agents`。接着在 `Users → Manage Permissions` 里给某个用户勾选要授权的 skill 并保存 —— 文本 agent 在下一次调用时就会加载新的授权。

测试 / 开发环境想跳过人工审批, 有辅助脚本 `approve_records.py` —— **生产环境请勿使用**, 它绕过了审批流程:

```bash
# 仅测试用 —— 自动审批 deploy.py 刚提交的 3 条记录
python approve_records.py
```

## 清理

```bash
# 全部清理 (最后一个 agent 被删后会一并清掉 Cognito m2m 资源)
python teardown.py

# 只清理某一个 agent
python teardown.py --agent energy-optimization
```

## 端到端验证

每个示例 agent 在回复的第一行都会输出 marker token `⟦A2A:<agent-shortname>⟧`, 方便上层编排和 e2e 测试确认回复真的经过了该专家 agent。Playwright 用例 `e2e-a2a/a2a-text-agent-invoke.spec.ts` 会断言 marker 在已授权时出现、被撤销后消失。

## 测试提示词

以下是每个 agent 的测试提示词样例, 用于在 chatbot 中触发对应 skill。提示词里自然包含关键词, LLM 应能自动挑对应工具 —— 不需要用户主动说 "用 A2A"。

### energy-optimization-agent

**skill: `estimate_savings`** — 估算具体节能/省钱数值

- LED 灯晚上调到 20% 亮度, 每月能省多少电费?
- 把烤箱从烘焙切到热风对流模式, 大概多久能回本?
- 我家 10 盏 LED 每天亮 8 小时, 改成智能感应 50% 时间关闭能省多少?
- 冰箱温度从 2°C 调到 4°C, 全年省多少度电?

**skill: `tariff_analysis`** — 比较电价方案 / 建议使用时段

- 比较分时电价 TOU 和固定电价, 按我每天用电 15 kWh 看哪个更划算?
- 如果改成分时电价, 我的电饭煲什么时段煮饭最省钱?
- 周末主要在家用空调 10 小时, 工作日晚上 2 小时, 用哪个电价套餐比较好?

### home-security-agent

**skill: `risk_assessment`** — 排查家里智能家居安全漏洞 (按严重度排序)

- 我家目前最大的安全隐患是什么? 我有 Wi-Fi 摄像头、智能门锁和智能音箱。
- 审查一下我的智能家居安全: 路由器用默认密码、摄像头在同一 Wi-Fi 下、门锁支持蓝牙。
- 我刚装了一个二手智能门锁, 最应该检查哪几项?

**skill: `incident_response`** — 面对安全事件给出应对步骤

- 凌晨 2 点门口运动传感器报警, 我该怎么办?
- 我收到智能门锁多次失败解锁尝试的通知, 下一步应该做什么?
- 摄像头显示后院有陌生人在徘徊, 接下来 60 秒内怎么做最稳妥?

### appliance-maintenance-agent

**skill: `maintenance_schedule`** — 给出保养周期与操作步骤

- 空调滤网多久该换一次? 换之前要注意什么?
- 电饭煲需要除垢吗? 大概多久一次, 怎么操作?
- 智能洗碗机每多少次循环应该清洗滤网?
- 扫地机器人的滚刷和 HEPA 滤网分别多久换一次?

**skill: `troubleshoot`** — 一步步故障排查清单

- 我家洗碗机排不出水了, 我应该按什么顺序检查?
- 烤箱预热明显比以前慢, 可能是什么原因? 自己能排查哪几项?
- 智能空调遥控器无响应, 空调本体正常, 从最简单到复杂的排查步骤有哪些?

---

**授权 vs 未授权的测试对照** (体现权限效果):

> 拿 `estimate_savings` 举例:
>
> 1. **未授权时**问: *"LED 灯晚上调到 20%, 每月能省多少电费?"* —— LLM 没有 `a2a_energy_optimization_agent_estimate_savings` 工具可用, 只能用通用知识编数字, 回答里不会出现 "A2A Energy Specialist" 之类的措辞。
> 2. 在 Admin Console 给用户授 `estimate_savings` → **重新开一个 chatbot 会话** (授权按 invocation 加载, 刷新页面或重新登录确保生效)。
> 3. **已授权时**再问同一个问题 —— LLM 发现了新工具, 调用它, 回复里经常会说 "根据 A2A Energy Specialist 计算…" 或类似措辞, 数字是下游 Nova Lite 算的。
> 4. 最铁证是看 CloudWatch `/aws/bedrock-agentcore/runtimes/<text-runtime>-DEFAULT` 日志, 搜索 `A2A tools registered`: 未授权 → 没这一行 (或 `0`); 已授权 → `A2A tools registered: 1 for actor=<email>`, 并且同一 trace 里能找到 `tool_use` + `tool_result`, tool_result 的第一行是 marker `⟦A2A:energy-optimization⟧`。
>
> 同理: 授了 `estimate_savings` 但没授 `tariff_analysis`, 问 "比较分时电价和固定电价" 就会失败 —— LLM 没有对应工具, 只能推说需要更多信息或给模糊答案。这种 skill 级差异是证明 A2A 授权是 skill 粒度而不是 agent 粒度最直观的方式。

## 单 A2A agent 分步演示指南

每步独立执行, 便于在演示中讲解。以 `energy-optimization` 为例。前提: `./deploy.sh` 已跑通基础系统。

```bash
cd /home/ubuntu/smarthome-assistant-agent
source venv/bin/activate
cd a2a-agent-registry
```

**想从干净状态开始?** 在执行 Step 1 之前, 先清理这个 agent 所有相关资源 (CFN 栈、Registry 记录、workload identity、用户授权、本地项目目录):

```bash
python demo_reset.py --agent energy-optimization
```

Cognito m2m 凭据和其它 agent 保留, Step 1 会跑得很快。

### Step 1 —— 部署一个 Runtime (暂不注册 Registry)

创建 Cognito m2m 凭据 + 通过 CodeBuild 构建并部署 Runtime + 把 A2A env 补丁到 text agent。跳过 Registry 便于演示 Runtime 本身就能独立工作。

```bash
python deploy.py --agent energy-optimization --skip registry
```

*验证:* `python smoke_test.py` —— 直接用 m2m token 访问 A2A endpoint, 不走 Registry。应输出 `summary: {'energy-optimization': True}`。

### Step 2 —— 注册到 AgentCore Registry

用真实的 invocation URL + OAuth2 安全 scheme 渲染 AgentCard, 删除 `example.com` placeholder, 创建真实记录, 提交审批。

```bash
python deploy.py --agent energy-optimization --only registry,persist
```

*验证:*
```bash
python3 -c "
import boto3, json
s = json.load(open('deployed-state.json'))
e = next(a for a in s['agents'] if a['agent']=='energy-optimization')
rid = json.load(open('../agentcore-state.json'))['registryId']
ac = boto3.client('bedrock-agentcore-control', region_name='us-west-2')
r = ac.get_registry_record(registryId=rid, recordId=e['recordId'])
print('status:', r['status'], '| recordId:', e['recordId'])"
```
应显示 `status: PENDING_APPROVAL`。

### Step 3 —— 在 AWS 控制台审批

1. AWS Console → Bedrock AgentCore → **Registry**
2. 打开 `SmartHomeSkillsRegistry`
3. 选中 `energy-optimization-agent` → **Approve**

*验证:* 重跑上面的 Python 片段 —— 应显示 `status: APPROVED`。

(跳过控制台点击的测试专用捷径: `python approve_records.py --agent energy-optimization`)

### Step 4 —— 在 Admin Console 给用户授权

1. 打开 **Admin Console** (URL 见 `cdk-outputs.json.SmartHomeAssistantStack.AdminConsoleUrl`)
2. 用 admin 账号登录 (`cdk-outputs.json.SmartHomeAssistantStack.AdminUsername` / `AdminPassword`)
3. **Build → Users** → 选择演示用户 → **Manage Permissions**
4. 滚到 **A2A Agents** → 展开 `energy-optimization-agent` → 勾选 `estimate_savings` → **Save Permissions**

*验证:*
```bash
python3 -c "
import boto3
t = boto3.resource('dynamodb', region_name='us-west-2').Table('smarthome-skills')
print(t.get_item(Key={'userId':'admin@smarthome.local','skillName':'__a2a_permissions__'}).get('Item'))"
```
应看到 `a2aGrants` 字典, 键是已审批的 recordId, 值含 `estimate_savings` skill。

### Step 5 —— 在 chatbot 对话测试

1. 打开 **Chatbot** (URL 见 `cdk-outputs.json.SmartHomeAssistantStack.ChatbotUrl`)
2. 用 Step 4 中被授权的用户登录
3. 从 [测试提示词](#测试提示词) 里挑一条 `estimate_savings` 的问题, 比如: *"LED 灯晚上调到 20% 亮度, 每月能省多少电费?"*
4. 首次调用等 10–30 秒 (拉 token + 解析 AgentCard); 回复应给出具体的 kWh / 金额数值, 那是下游 Nova Lite 算出来的。

*验证 A2A 工具确实被调用:*

```bash
RT_ID=$(jq -r '.runtimeId' /home/ubuntu/smarthome-assistant-agent/agentcore-state.json)
aws logs filter-log-events \
  --log-group-name /aws/bedrock-agentcore/runtimes/${RT_ID}-DEFAULT \
  --region us-west-2 \
  --start-time $(($(date +%s)*1000 - 300000)) \
  --filter-pattern '"A2A tools registered"' \
  --query 'events[].message' --output text | head -3
```
应看到 `A2A tools registered: N for actor=<email>`。

然后搜索具体的工具调用和下游 marker:
```bash
aws logs filter-log-events \
  --log-group-name /aws/bedrock-agentcore/runtimes/${RT_ID}-DEFAULT \
  --region us-west-2 \
  --start-time $(($(date +%s)*1000 - 300000)) \
  --filter-pattern 'a2a_energy_optimization_agent_estimate_savings' \
  --query 'events[].message' --output text | head -c 2000
```
应能看到一次 `tool_use` 后跟一次 `tool_result`, tool_result 内容以 `⟦A2A:energy-optimization⟧` 开头 —— 证明回复确实来自下游 A2A agent, 而不是 text agent 自己编的。

### 清理

```bash
# 撤销演示授权 (保留 Registry + Runtime)
python3 -c "
import boto3
t = boto3.resource('dynamodb', region_name='us-west-2').Table('smarthome-skills')
t.delete_item(Key={'userId':'admin@smarthome.local','skillName':'__a2a_permissions__'})"

# 只清理这一个 agent (保留 Cognito m2m, 其它 agent 仍可用)
python teardown.py --agent energy-optimization
```

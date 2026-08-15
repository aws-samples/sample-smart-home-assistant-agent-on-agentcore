# AgentCore 部署演示操作手册 — Sub-Agent 与 Skill

两条独立的演示链路, 都从本地出发, 终点都是 Admin Console 里可见、可授权:

| | Flow A — A2A Sub-Agent | Flow B — Skill |
| --- | --- | --- |
| 交付物 | 一个跑在 AgentCore Runtime 上的专家 agent | 一份编排 agent 加载的指令文档 |
| 本地能做什么 | 起 A2A server, 真实对话调试 | 写 / 校验 `SKILL.md` |
| 要不要构建镜像 | 要 (CodeBuild, 5–10 分钟) | **不要** |
| 注册对象 | Registry `AGENT` 记录 (AgentCard) | Registry `SKILL` 记录 (`agentSkillsDefinition`) |
| 生效方式 | 给用户授权 → Cognito group / token claim → 下次登录 | 从 Registry 导入到 DynamoDB → 下次调用 |
| 演示时长 | 约 20 分钟 (含构建等待) | 约 5 分钟 |
| 代码包 | `logs/agentcore-deploy-demo/demo-agent-air-quality/` | `logs/agentcore-deploy-demo/demo-skill-indoor-air-report/` |

两个代码包各自有独立的 README(更细的分步说明), 本文是**串起来讲**的操作流程 +
判断依据 + 排错表。

> **2026-08-15 第二版:接口面收窄到三样。** 一个 A2A 团队和平台之间现在只有三个接口 ——
> 你的入站 endpoint、你的 AgentCard 记录、我们的审批+授权。其余全部从记录推导。
> 这一版消除的耦合:
> - **加 skill 不再需要重部署 runtime**(门口收成一个稳定的 agent 级 group,见 A2)
> - **不用再手抄任何配置** —— Admin Console → 集成注册中心 → A2A 代理 → **「平台契约」**
>   按钮给出机器可读的 manifest,由执行规则的同一份代码生成
> - **合规检查变成审批门**(A5.5):不合规批不过去,纠错回路留在 agent 团队那边
> - **gateway target 与 Dashboard 白名单从 Registry 收敛** —— 不用再找平台团队加配置
>
> **2026-08-15 第一版新增/修改了四处, 演示时值得单独讲:**
> 1. **A5.5 authorizer 一致性检查** —— 注册让你被发现, 你自己的 authorizer 决定谁能调你。
>    这是"任何人都能接进来"这句话唯一还需要知道本部署配置的地方, 现在有明文契约 +
>    生成器 + 控制台标红。
> 2. **A6 全局授权不再落 membership** —— 由 pre-token-generation trigger 在发 token 时注入
>    `cognito:groups`。原因是 Cognito 那条 25 RPS 不可调的写配额。
> 3. **A8 非 APPROVED 现在是真的调不动**, 且升版有**重新审批宽限期**, 所以升版是停机而不是
>    丢授权。
> 4. **A7 主 Agent 的 session id 会跨 A2A 传到子 Agent**, 于是一次 turn 的 token 可以跨委派
>    加总 —— 以前做不到。
>
> 另外: 本部署的 8 个内建 sub-agent 现在挂在一个专用的 **A2A Gateway**
> (`smarthome-a2a-gw`)后面。**照本手册部署的 demo agent 用自己的 runtime URL 注册,
> 一样能用**, 只是不在那层集中出口/审计/kill-switch 里 —— 要进去需要在 gateway 上加一个
> `passthrough` target 再把卡的 url 改成 `{gatewayUrl}/{target}`。

> **先说清楚一件事: AgentCore CLI 没有 registry 子命令。**
> `agentcore --help`(0.26 / 0.27)里没有任何 registry 相关命令; CLI 内置的 AWS SDK
> 还是旧的 `bedrock-agentcore` 命名空间, 而 Registry 在 2026-08-06 GA 时搬到了
> `agent-registry-control`, 旧命名空间 2026-09-17 之后不再提供 Registry。
> 所以: **Runtime 部署用 CLI(`agentcore create` / `deploy`), Registry 注册用脚本
> (boto3 打 GA 接口)** —— 就是两个包里的 `register_record.py` / `publish_skill.py`。
> 演示时按这个说法讲, 比说"用 CLI 注册"准确。

---

## 0. 环境准备 (演示前 10 分钟做完)

```bash
# 1) CLI 与依赖
agentcore --version                 # 期望 >= 0.26
npm install -g @aws/agentcore       # 需要升级时
pip install -r requirements.txt     # 在两个包各自目录里

# 2) 凭据与区域
aws sts get-caller-identity
aws configure get region            # 应为 us-west-2

# 3) boto3 是否认得 GA 的 Registry 命名空间 —— 最常见的第一个坑
python3 -c "import boto3;print('agent-registry-control' in boto3.Session().get_available_services())"
# False 就升级: pip install -U 'boto3>=1.43.67'

# 4) 两个包的配置是否指向同一套环境
cat demo-config.json
```

演示前的健康检查(确认基座是活的):

```bash
cd logs/agentcore-deploy-demo/demo-skill-indoor-air-report
python publish_skill.py --list      # 应列出 10 条已批准的内建 skill
```

能列出来说明: 凭据、区域、registryId、boto3 版本四件事全对。这一条命令是整场演示
最省时间的前置检查。

---

## Flow A — A2A Sub-Agent 部署流程

工作目录: `logs/agentcore-deploy-demo/demo-agent-air-quality/`。演示的 agent 是 `air-quality-agent`
(室内空气质量顾问, prompt-only)。

### A1. 本地调试 (2 分钟)

```bash
# 终端 1
AWS_REGION=us-west-2 python main.py
# 终端 2
AWS_REGION=us-west-2 python invoke_local.py
```

**要指给观众看的三行**:

```
INFO:common.server:A2A agent air-quality-agent ready — skills=['filter_advice', 'ventilation_plan'] tools=none
card.skills : ['ventilation_plan', 'filter_advice']
marker ⟦A2A:air-quality⟧ present: True
```

分别证明: 容器启动时就读定了自己的 skill 清单 / AgentCard 对外可解析 /
回复带路由 marker(上层用它确认真的路由到了这个专家)。

顺手演示权限闸门 —— 这一条比正向调用更有说服力:

```bash
python invoke_local.py --no-skills
# Request refused: X-A2A-Allowed-Skills is missing or empty — this agent requires an explicit skill grant.
```

讲法: **不带授权的请求被拒, 而不是被放行**。缺省不能比显式授权更宽松。

改 `air-quality/system_prompt.md` 后重启 `main.py` 就能看到效果 —— 本地这一圈的价值
就是把提示词和 card 调稳了再去构建镜像, 省掉每次 5–10 分钟的 CodeBuild。

### A2. 用 agentcore CLI 部署到 Runtime (5–10 分钟)

```bash
python deploy_runtime.py --dry-run    # 先展示将要写入的 env 和 authorizer
python deploy_runtime.py
```

三步, 讲清第三步为什么存在:

1. `agentcore create --name sha2aair --protocol A2A --framework Strands
   --model-provider Bedrock --memory none --build CodeZip --language Python`
   —— 生成 CLI 项目, 删掉自带 stub, 把本包代码拷进 code root。
   (`--defaults` 单独用已经不够了: 0.26 起非交互 create 要求把 framework /
   model-provider / memory 都写出来。)
2. `agentcore deploy -y --verbose` —— CodeBuild 构建 + CloudFormation 建 Runtime。
   **本地不需要 Docker**。
3. `UpdateAgentRuntime` 补三样 CLI 不管的东西: 自定义 env、CUSTOM_JWT authorizer
   (带 grant claim 校验)、request header allowlist。

**第三步是这条链路里最容易被跳过、也最致命的一步。** 只跑到第 2 步的 Runtime 是
"看起来部署好了其实是坏的": 没有 authorizer 去校验授权、没有 `COGNITO_*` 去验签
转发来的用户 token、边缘还会在容器看到之前**静默丢掉** `Authorization` 头 ——
平台说通过、容器说拒绝, 读起来像容器的 bug。

授权模型(一句话讲完):

```
门口 (Runtime authorizer)  CONTAINS_ANY ["a2a-air-quality-agent"]
                           ← 一个 group, agent 生命周期内不变。没授权的连容器都进不去
容器内 (common/server.py)  从同一个已验签 claim 读 a2a-air-quality-agent.<skill>
                           ← 推出"授了哪几个 skill"
```

**2026-08-15 起: `card.json` 里加 skill 不再需要重跑 `deploy_runtime.py`。**
以前需要,而且这是 day-2 最烦的一个耦合:门口那份列表把每个 skill 枚举进 `CONTAINS_ANY`,
而它**没有通配符**,于是被授了新 skill 的用户在门口被拒、没有任何日志。

那份枚举**换不来任何门禁强度** —— 门口只要命中任意一个就放行,而容器随后用同一个 claim
做同样的"至少一个"判断(`enforce_allowed_skills`)。所以门口收成一个稳定的 agent 级 group,
强度不变,耦合消失。

**仍然需要重跑 `deploy_runtime.py` 的:改 `card.json` 的 `name`。** group 名按卡名编,
改名对每个执行点都是一个新 agent,旧授权不会跟过去。

### A3. 注册到 Registry, 用新 version (1 分钟)

```bash
python register_record.py --dry-run                 # 展示将要提交的完整 AgentCard
python register_record.py --record-version 0.1.0
```

得到 `recordId` 和 `status: PENDING_APPROVAL`。

### A4. 审批 (1 分钟)

AWS Console → Bedrock AgentCore → **Registry** → 选 registry → 选
`air-quality-agent` → **Approve**。

演示图快的话(**绕过审批闸门, 生产禁用**):

```bash
python register_record.py --record-version 0.1.0 --approve
```

### A5. Admin Console 里看到 (1 分钟)

**探索 → 集成注册中心 → A2A 代理** —— 出现 `air-quality-agent`, 版本 `0.1.0`。

没出现就先查状态: 这个页面只列 `APPROVED`。

### A5.5 核对 authorizer 与卡是否一致 (1 分钟, 2026-08-15 新增)

**这一步是整条流程里最容易被跳过、也最容易静默出错的一步。**

注册一条 APPROVED 的卡只让 agent **被发现**:控制台列出它、编排器给它注册工具、委派提示词
里出现它 —— 全部从 Registry 派生, 上游不需要改任何代码。**能不能被调用**是由 agent
**自己 Runtime 的 authorizer** 决定的, 而那份配置在部署这个 runtime 的人手里。

两个方向都会静默出错:

| 配错的方式 | 后果 | 你会看到什么 |
| --- | --- | --- |
| `customClaims` 缺失或写松 | 池子里**任何**登录用户都能调它的全部 skill | 什么都看不到, 一切正常工作 |
| pool / audience 配错 | 已授权用户被拒(401) | 编排器照样注册工具, 模型调用后返回 `A2A agent call failed: ...` 然后道歉 |

两种情况下 A2A Agents 页的状态列都显示 `approved` —— 它读的是 Registry 状态, 不是 authorizer。

所以看那一页**新增的 `Authorizer` 列**:

- `Matches card` —— 一致
- `Too permissive`(红)—— 有人能调到他不该调的
- `Callers refused`(黄)—— 已授权的人被拒

抽屉里列出具体 finding 和修复命令。也可以直接调:

```bash
curl -s -H "Authorization: $ID_TOKEN" \
  "$ADMIN_API/registry/records?action=a2a-conformance" | jq '.records[] | {name, conformant, severity}'
```

`deploy_runtime.py` 本身就是按契约生成 authorizer 的, 所以照本手册走出来的 agent 天然一致。
别人自己部署的 runtime 用这个打印应有配置(和检查用的是同一个函数, 不会分叉):

```bash
./venv/bin/python scripts/a2a-authorizer-contract.py --card air-quality/card.json
./venv/bin/python scripts/a2a-authorizer-contract.py --record-id <recordId> --format cli
```

**2026-08-15 起这不只是报告 —— 它是审批门。** 不合规的 AGENT 记录批不过去,审批返回
409 并附上具体 finding 和要跑的命令。哪些拦:

| 严重度 | 含义 | 审批 |
| --- | --- | --- |
| `open` | 有人能调到他不该调的 | **拦** |
| `closed` | 已授权的人被拒 | **拦** |
| `info` | Runtime 读不到(别的账号),或 authorizer 还在用旧的 per-skill 列表 | 放行, 报告 |

`info` 不拦是刻意的:"读不到"不等于"配错了",而"还在用旧列表"的 runtime **今天能用**。
把能用的东西报成红色,是让检查被忽略的最快方式。平台管理员可以 `?force=true` 强批,
会写进记录的 `statusReason` 留痕。

**加 skill 之后不再需要重跑这一步**(见 A2)。改卡名才需要。

第三方接入的完整契约见 `docs/a2a-agent-onboarding.md`,里面第一步就是从 Admin Console
的 **「平台契约」** 按钮复制 manifest,而不是手抄任何值。

### A6. 授权给用户 (2 分钟)

**构建 → 工具策略** → 选用户 → **管理权限** → 滚到 **A2A 智能体** → 展开
`air-quality-agent` → 勾 `ventilation_plan` → **保存权限**。

后台做两件事: DynamoDB 里写 `a2aGrants`(按 recordId 存), 并落成**两个** Cognito
group(不存在会自动建):

- `a2a-air-quality-agent` —— 门钥匙, Runtime authorizer 匹配的就是它
- `a2a-air-quality-agent.ventilation_plan` —— 容器用它判断"授了哪几个 skill"

取消所有勾选(而不是删掉整行)会存成一个**空 skill 列表**, 那是"这个 agent 上什么都没授"
的显式表达 —— 此时**连门钥匙都不发**, 用户在门口就被 401, 而不是进门再被容器拒。

```bash
aws cognito-idp admin-list-groups-for-user --region us-west-2 \
  --user-pool-id us-west-2_HwYYt6qLz --username <邮箱> --query 'Groups[].GroupName'
```

**2026-08-15 起, 全局授权不再落 membership。** 勾在 `__global__` 那一栏的授权由
pre-token-generation trigger 在**发 token 时注入 `cognito:groups`**, 所以上面那条
`admin-list-groups-for-user` **看不到它** —— 这是正常状态, 不是漂移。原因是 Cognito 的
`AdminAddUserToGroup` 属于 `UserUpdate` 类别, 25 RPS 且不可调, 每次改 membership 还计一次
MAU; "所有人都有"这件事写成一百万条 membership 是把一个常量写了一百万遍。**per-user
授权仍然是真实 membership**(它是少数派, 而且需要立即强制登出的能力)。

要看一个用户实际持有什么, 解他的 idToken:

```bash
# claim 里既有真实 membership, 也有 trigger 注入的全局授权
python - <<'EOF'
import base64, json, boto3
tok = boto3.client("cognito-idp", region_name="us-west-2").initiate_auth(
    ClientId="<userPoolClientId>", AuthFlow="USER_PASSWORD_AUTH",
    AuthParameters={"USERNAME": "<邮箱>", "PASSWORD": "<密码>"}
)["AuthenticationResult"]["IdToken"]
p = tok.split(".")[1]; p += "=" * (-len(p) % 4)
print(sorted(json.loads(base64.urlsafe_b64decode(p)).get("cognito:groups") or []))
EOF
```

**授权在下次登录 / 新会话才生效** —— grant 在 token 的 claim 里, 手上那个旧 token
里没有。演示时一定要重新登录, 否则会看起来像授权没生效。

### A7. chatbot 里验证 (2 分钟)

用被授权的用户重新登录 chatbot, 问:

> 室内 CO2 1250 ppm, 室外 PM2.5 90, 现在要不要开窗?

首次 10–30 秒(取 token + 解析 AgentCard)。铁证在日志里:

```bash
RT_ID=$(jq -r '.runtimeId' agentcore-state.json)     # 主 text agent
aws logs filter-log-events \
  --log-group-name /aws/bedrock-agentcore/runtimes/${RT_ID}-DEFAULT \
  --region us-west-2 --start-time $(($(date +%s)*1000 - 600000)) \
  --filter-pattern '"A2A tools registered"' --query 'events[].message' --output text | tail -3
```

`A2A tools registered: N for actor=<email>` 说明工具挂上了; 再搜
`a2a_air_quality_agent_ventilation_plan` 能看到 `tool_use` + `tool_result`,
`tool_result` 第一行是 `⟦A2A:air-quality⟧` —— 这才证明答案来自下游子 Agent。

**2026-08-15 起还有一条更强的证据: 主 Agent 的 session id 会跨 A2A 这一跳传到子 Agent。**
去子 Agent 自己的日志组里搜:

```bash
aws logs filter-log-events \
  --log-group-name /aws/bedrock-agentcore/runtimes/<子Agent runtimeId>-DEFAULT \
  --region us-west-2 --start-time $(($(date +%s)*1000 - 600000)) \
  --filter-pattern '"delegated turn: orchestrator session"' \
  --query 'events[].message' --output text | tail -2
```

会打印 `delegated turn: orchestrator session=user-session-<sub>-<epoch>` —— 和主 Agent 那一
轮是同一个 id。它走两条通道: 平台原生的 session 头(让 AgentCore 把这个 id 采纳为子 Agent
的 `runtimeSessionId`, 所以子 Agent 的 span 和日志流名都带它)+ A2A `Message.metadata`
(容器唯一能读到的通道 —— AgentCore 的 header allowlist 不放行 `x-amzn-` 头)。

由此可以按一次 turn 把两边的 token 加起来, 这是以前做不到的:

```bash
# 同一个 session.id 下, 主 Agent 与子 Agent 的 token 分别是多少
# (Logs Insights, 两个 runtime 的日志组一起查)
filter scope.name = "strands.telemetry.tracer"
| filter attributes.session.id = "<那个 session id>"
| filter ispresent(attributes.gen_ai.usage.total_tokens)
| stats sum(attributes.gen_ai.usage.total_tokens) by resource.attributes.service.name
```

### A8. 升一个新 version (演示的重点桥段, 3 分钟)

改 `system_prompt.md` 或 `card.json`, 然后:

```bash
python deploy_runtime.py                            # 重新构建 + 重新 patch
python register_record.py --record-version 0.2.0    # 原地升版, recordId 不变
```

**实测过的两条事实, 演示时讲出来最有价值:**

1. **原地升版 recordId 不变; `--new-record` 会造出同名的第二条记录、新的 recordId。**
   两种都能用, 但用户授权是**按 recordId** 存的 —— 换 recordId 等于: 昨天的授权指向
   没人用的记录、控制台里同一个 agent 出现两次、新那行勾选框全空。故障是静默的,
   用户只是突然没有这个专家了。**agent 用原地升版。**
2. **更新一条已 APPROVED 的记录, 状态会被打回 `DRAFT`。** 而 Admin Console 和编排
   agent(`agent/tools/a2a.py`)**都只认 `APPROVED`** 的 AGENT 记录。也就是说升版的
   那一刻, 这个专家对所有已授权用户消失, 直到重新审批通过。脚本会自动重新提交
   (→ `PENDING_APPROVAL`), 但 Approve 是人点的。

   → 运维含义: 升版属于**有窗口的变更**, 要么安排在维护窗口, 要么先 `--new-record`
   发新版本、审批通过后再切授权(蓝绿), 而不是直接原地改线上那条。

   **2026-08-15 起, 这个窗口只是"停机", 不再"丢授权"。** 撤销扫描(见下)给
   `DRAFT` / `PENDING_APPROVAL` 的记录留一个**重新审批宽限期**(默认 3600 秒,
   `A2A_GRANT_GRACE_SECONDS`), 窗口内不会去撤 Cognito group。所以升版之后重新审批通过,
   授权原样还在, 不需要重新勾一遍。超出宽限期才会真的撤。

3. **非 APPROVED 现在是真的调不动了。** 以前状态离开 APPROVED 只是让编排器不再注册工具 ——
   手里有 group 又知道 URL 的调用方**照样进得去**(URL 就写在 AgentCard 里)。现在有一个
   每 5 分钟的撤销扫描 + 写路径内联触发, 会把不可授权记录对应的 `a2a-` group 从持有者身上
   摘掉; 全局授权那一半由 token trigger 停止注入。判定规则一份, 两个执行点共用:

   | 状态 | 判定 |
   | --- | --- |
   | `APPROVED` | 可授权 |
   | `REJECTED` / `DEPRECATED` / 记录消失 | **立即**不可授权 |
   | `DRAFT` / `PENDING_APPROVAL` | 宽限期内仍可授权, 超出则不可 |

   撤销**不动授权意图** —— `__a2a_permissions__` 原样保留, 记录回到 APPROVED 后下一轮扫描
   幂等地加回去。所以最坏情况是一段停机, 不是丢数据。

### A9. 清理

```bash
python teardown.py --dry-run
python teardown.py
```

先删 Registry 记录再删 Runtime: 记录还在而 Runtime 已删, 持有授权的用户调用会在传输层
拿到 404, 看起来像网络问题。Cognito 的 grant group 不再需要手工清 —— 记录一消失,
撤销扫描下一轮(≤5 分钟)就会把对应的 group 从持有者身上摘掉。要立刻生效就在
**管理权限** 里取消勾选, 或直接跑一次扫描。

> **不要用 `deprecate` 当"临时停用"。** 2026-08-15 实测: `DEPRECATED` 是**终态**,
> `UpdateRegistryRecordStatus` 对它的任何目标状态都返回
> `Cannot update registry record in DEPRECATED status (terminal state)`, 包括回到
> APPROVED; **而且记录会直接从 API 上消失** —— `GetRegistryRecord` 返回
> `ResourceNotFoundException`, 任何 status 过滤都查不到它。恢复只能重建, 而重建会得到
> **新的 recordId**, 于是所有按 recordId 存的授权全部失效, 要逐条迁到新 id。
>
> 想可逆地停用请用 `reject`(`REJECTED` → `APPROVED` 是通的), 或者直接改授权。
> Admin Console 现在会在 deprecate 之前弹一次确认说明这件事。

---

## Flow B — Skill 发布流程

工作目录: `logs/agentcore-deploy-demo/demo-skill-indoor-air-report/`。

### B1. 本地写 + 自检 (1 分钟)

```bash
python publish_skill.py --list        # 现有 SKILL 记录
python publish_skill.py --dry-run     # 完整载荷
```

`SKILL.md` 两条格式硬要求(都不在文档和 API model 里, 都是实测出来的):

1. **必须以 `---` frontmatter 开头**, 否则服务端拒绝:
   "data must start with frontmatter delimited by '---'"。
2. **工具列表键是 `allowed_tools`(下划线)。** 仓库里 `agent/skills/*/SKILL.md` 用的是
   `allowed-tools`(连字符, 给 DynamoDB seeder 的 YAML 解析器读); Admin Console 的
   **导入**路径是手写行解析器, 只认下划线。写错会静默导入一个空工具列表。

### B2. 发布, 用新 version (1 分钟)

```bash
python publish_skill.py --record-version 0.1.0
```

### B3. 审批 (1 分钟)

AWS Console → Bedrock AgentCore → Registry → `indoor-air-report` → **Approve**,
或演示图快 `--approve`(绕过闸门, 生产禁用)。

拒绝时**必须**填理由: `statusReason` 是提交者唯一能看到的反馈, 空理由的拒绝和
"系统把我的 skill 弄丢了"无法区分。

### B4. Admin Console 里看到 (1 分钟)

**探索 → 集成注册中心 → 技能** —— 出现 `indoor-air-report`。

这一页和 **构建 → 技能** 回答的是两个不同的问题, 演示时要把这句说出来:

| 页面 | 回答什么 | 数据源 |
| --- | --- | --- |
| 探索 → 集成注册中心 → 技能 | **审批通过了什么** | Registry 的 APPROVED SKILL 记录 |
| 构建 → 技能 | **实际在跑什么** | DynamoDB `smarthome-skills` |

### B5. 导入 → 生效 (1 分钟)

**构建 → 技能 → 从 AgentCore Registry 添加已批准技能** → 勾选 → 选作用域
(`__global__` 或某个用户)→ **导入选中项**。

```bash
aws dynamodb get-item --region us-west-2 --table-name smarthome-skills \
  --key '{"userId":{"S":"__global__"},"skillName":{"S":"indoor-air-report"}}' \
  --query 'Item.{desc:description.S,tools:allowedTools.L,from:importedFromRegistry.S}'
```

编排 agent 下次调用就加载, **不需要重新部署 runtime**。

### B6. 升 version 与清理

```bash
python publish_skill.py --record-version 0.2.0                 # 原地
python publish_skill.py --record-version 0.2.0 --new-record    # 并存
python publish_skill.py --delete
```

skill 的升版和 agent 有一个关键差别: **两个存储是解耦的。** registry 记录被更新
(状态回到 DRAFT)时, 已经导入到 DynamoDB 的那份继续生效。好处是升版不打断线上;
陷阱是 registry 里已经改了、agent 用的还是导入时的旧内容 —— **必须重新导入**。
同理, `--delete` 不会撤回已经生效的 skill, 停用要去 **构建 → 技能** 删那一行。

---

## 版本号语义 (两条链路通用, 均已实测)

`name` 是 registry 的去重键, 且**与 `recordVersion` 组合唯一**。所以有两种"发新版本":

| | 原地升版 | 新建记录 |
| --- | --- | --- |
| 调用 | `UpdateRegistryRecord(recordVersion=…)` | `CreateRegistryRecord(name 相同, version 不同)` |
| recordId | 不变 | **新的** |
| 结果 | 一条记录, 版本号变了 | 同名两条记录并存 |
| 状态 | 已 APPROVED 的会被**打回 DRAFT** | 新记录从 DRAFT 开始 |
| 适合 | Agent(授权按 recordId 存)、想"一个东西一行" | 要保留可回滚的版本历史 |

配套的 API 形状(两边都容易写错):

- `CreateRegistryRecord` 返回 **`recordArn`, 不是 `recordId`** —— 读 `recordId` 会拿到
  `None` 并存下去, 之后那条记录"看起来创建了但找不到"。
- `UpdateRegistryRecord` 把 **每一层**都包进 `optionalValue`(union / 每个 descriptor /
  每个字段), 而 `recordVersion` 和 `name` 是裸字符串。只包最外层是那种"看起来对了但
  不对"的改法, 而且失败方式不统一: SKILL 会抛 `ParamValidationError`(还算响亮),
  别的可能被接受然后忽略。用 `agent_registry.as_update_descriptors()`, 别手写。
- `description` 在 Create 里是裸字符串, 在 Update 里是 `{"optionalValue": "..."}`。
- 记录处于 `CREATING` / `UPDATING` 时任何 Update / Delete 都会被拒
  ("cannot be modified while in CREATING state") —— 两个脚本里都有 `wait_settled`。

状态机比 enum 看起来窄:

```
DRAFT ──submit──> PENDING_APPROVAL ──update status──> APPROVED
  └──> DEPRECATED (终态)                    REJECTED ──> APPROVED (审批人可反悔)
```

对 `DRAFT` 直接置 `APPROVED` 报 "Invalid status transition"。

---

## 排错表

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `UnknownServiceError: agent-registry-control` | boto3 太旧 | `pip install -U 'boto3>=1.43.67'` |
| 注册被拒: "does not match any supported version" | AgentCard 不完整(缺 `capabilities` / `defaultInputModes` / `securitySchemes`) | 用 `render_card_for_registry` 输出的完整 card, 不要手工裁剪 |
| 注册被拒: "data must start with frontmatter delimited by '---'" | `skillMd` 没有 frontmatter | `SKILL.md` 以 `---` 开头 |
| `ConflictException: cannot be modified while in CREATING state` | 建完立刻改 | 等状态离开 CREATING(脚本里 `wait_settled`) |
| 记录建好了但控制台看不到 | 状态不是 `APPROVED` | 去 Registry 控制台 Approve |
| 升版后专家突然不可用 | 更新已 APPROVED 的记录会打回 DRAFT, 而控制台和编排 agent 只认 APPROVED | 重新审批; 或用 `--new-record` 做蓝绿 |
| 授权保存了但 agent 没有这个工具 | grant 在 token claim 里, 旧 token 没有 | 重新登录 / 开新会话 |
| 用户在门口被拒, 无日志 | 用户的 token 里没有 `a2a-<cardName>` 门钥匙。要么授权没落地, 要么手里是**迁移前签发的旧 token** | 重新登录换新 token;或跑一次 `?action=a2a-reconcile` 的 PUT 回填 |
| 迁移到门钥匙之后一批用户突然全被拒 | 授权侧还没发门钥匙就先翻了 authorizer —— 顺序反了 | `scripts/migrate-a2a-door-groups.py --rollback --apply` 回退, 回填后再翻 |
| 部署后容器 401 / 拒绝一切请求 | 只跑了 `agentcore deploy`, 没做部署后 patch(env / authorizer / header allowlist) | 重跑 `deploy_runtime.py`(幂等) |
| 导入的 skill 没有工具 | frontmatter 写了 `allowed-tools`(连字符) | 改成 `allowed_tools` |
| registry 里改了但 agent 行为没变 | 两个存储解耦, DynamoDB 里还是导入时的旧内容 | 重新导入 |
| 子 Agent 启动即崩(ImportError) | `pyproject.toml` 少了代码 import 的依赖 | 依赖清单要覆盖所有 import(`python-jose`, `mcp` 等) |
| 注册好了、控制台显示 `approved`, 但**谁都调不到** | authorizer 的 pool / audience 不是本部署的 | A2A Agents 页看 `Authorizer` 列(`Callers refused`), 用 `a2a-authorizer-contract.py` 打印应有配置 |
| 注册好了, 但**没授权的人也能调** | authorizer 没配 `customClaims`, 授权层等于不存在 | 同上, `Authorizer` 列会显示 `Too permissive`(红) |
| 全局授权在 `admin-list-groups-for-user` 里看不到 | 2026-08-15 起全局授权由 token trigger 注入 claim, 不落 membership | 正常状态; 解 idToken 看 `cognito:groups`, 或看控制台 reconcile 页的 claim-injected 一行 |
| `deprecate` 之后想恢复, 一切操作都被拒 | `DEPRECATED` 是终态且记录会从 API 消失 | 只能重建记录 → 新 recordId → 把授权逐条迁过去。临时停用请用 `reject` |
| 一次 `cdk deploy` 之后 sweep 报 `registryId failed to satisfy constraint` | admin Lambda 的 `REGISTRY_ID` 被重置成 PLACEHOLDER(**改了 CDK 声明的 environment 才会触发**, 只改代码不会) | 按名字合并恢复环境变量, 再跑 `scripts/check-registry-wiring.py` 确认 exit 0 |

---

## 演示期间发现并已修复的两个 bug (Skill ERP)

准备这份演示时在 `cdk/lambda/skill-erp-api` 里发现两处真实缺陷, **已修复并实测**,
未部署(改的是 Lambda 代码, 需要重新 `cdk deploy` 才会生效):

1. **"编辑我已发布的记录"必然 500。** `update_my_record` / `update_my_a2a` 传的是
   `description=<裸字符串>` + Create 形状的 `descriptors`, 而 Update 要求
   `description={"optionalValue": ...}` 且 `descriptors` 每层包 `optionalValue`
   —— 请求根本没离开 Lambda 就抛 `ParamValidationError`。
   根因是 `shared/agent_registry.py` 的 Lambda 副本陈旧(缺 `as_update_descriptors`),
   而副本只在 `scripts/01-install-deps.sh` 里被 `cp` 刷新。
2. **编辑之后没有真的重新提交审批。** 提交前的等待只等 `CREATING` 离开, 但记录写完
   之后处于 `UPDATING` —— 旧检查把 `UPDATING` 当成"就绪", 于是
   `SubmitRegistryRecordForApproval` 撞上
   `ConflictException: cannot be modified while in UPDATING state`。这个失败被塞进
   `submitWarning` 里, 所以接口返回 200 而记录停在 `DRAFT`, 永远进不了审批队列。

修复内容: 刷新两个 Lambda 副本、两个更新调用点改用
`registry_ns.as_update_descriptors(...)` + `{"optionalValue": description}`、
把等待改成"等到状态不再是 CREATING/UPDATING"并在撞到瞬时冲突时重试一次。
新增三处测试防回归:

- `cdk/lambda/skill-erp-api/tests/test_registry_update_payload.py` —— 拿真实
  botocore shape 校验 handler 实际构造的 kwargs(原来的路由测试用 MagicMock,
  什么形状都收, 这就是 bug 能活下来的原因)
- `cdk/lambda/skill-erp-api/tests/test_submit_for_approval.py` —— 等待与重试语义
- `shared/tests/test_agent_registry_parity.py` —— 防止 Lambda 副本再次陈旧

实测结果(打真实 registry, 用完即删): create 与 update 都返回 200/201、无
`submitWarning`、状态 `PENDING_APPROVAL`、内容确实被改写、`optionalValue` 没有漏进
数据里。所以现在 Skill ERP 的编辑按钮和 `publish_skill.py` 两条路都可以演示 ——
**前提是先重新部署 Lambda**。

---

## 演示时间表 (总计约 30 分钟)

| 分钟 | 内容 | 想让观众记住的一句话 |
| --- | --- | --- |
| 0–3 | Flow A 本地调试 + `--no-skills` 拒绝 | 本地就能真实对话, 缺省不比显式授权宽松 |
| 3–5 | `deploy_runtime.py --dry-run` 讲三步 | CLI 建 Runtime, 但 env / authorizer / header 要自己补回去 |
| 5–13 | 真实部署(CodeBuild 等待期间讲授权模型) | 授权是 token 里的签名 claim, 平台在容器之前就拦了 |
| 13–15 | 注册 + 审批 | CLI 没有 registry 命令, 注册走 GA API; 审批是产品里的闸门 |
| 15–19 | Admin Console 看到 + 授权 + 重新登录验证 | 授权按 recordId 存, 按 group 生效, 下次登录才有 |
| 19–22 | 升 version(原地)+ 讲两条实测事实 | 升版会把记录打回 DRAFT, 是有窗口的变更 |
| 22–27 | Flow B: 发布 → 审批 → 看到 → 导入 | 没有镜像、没有 Runtime; "审批了什么"和"在跑什么"是两页 |
| 27–30 | 对比与收尾 | 同一个 Registry 承载两类资产, 同一个审批闸门, 生效路径不同 |

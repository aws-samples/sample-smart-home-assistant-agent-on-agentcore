# 接入契约：把一个 A2A Agent 接进这套系统

这份文档是给**不了解主 Agent 任何配置**的人看的。照着做,你的 Agent 会被自动发现并可以
被授权使用;不照着做,失败是**静默的**,而且有两个方向。

日期：2026-08-15(第二版:接口面收窄到三样)

---

## 0. 一句话版本

> **注册到 Registry 让你被发现;你自己 Runtime 的 authorizer 决定谁能调用你。**

第一件事完全自动 —— 主 Agent、Admin Console、编排器都不需要改代码或重新部署。第二件事
是你必须配的,而且需要知道本部署的两个值 —— **不要手抄,去拿 §1 的 manifest。**

## 0.1 你和平台之间只有三个接口

| # | 接口 | 谁做 |
|---|---|---|
| 1 | 一个入站 A2A endpoint,接受本平台用户的 idToken 并按 claim 自我约束 | **你** |
| 2 | 一条合规 AgentCard 的 `AGENT` 记录 | **你** |
| 3 | 审批 + 给用户授权 | **平台**(这是控制点,不会下放) |

**没有第四样。** 平台侧需要的一切都从 #2 推导:控制台列表、编排器工具、委派提示词、
gateway target、Dashboard 白名单 —— 全部。如果你发现自己需要请平台团队"顺手加一下某个
配置",那是 bug,请提出来。

---

## 1. 先拿 manifest,别手抄任何值

**Admin Console → 探索 → 集成注册中心 → A2A 代理 → 「平台契约」按钮 → 复制 JSON。**
或者直接调:

```bash
curl -s -H "Authorization: $ID_TOKEN" \
  "$ADMIN_API/registry/records?action=a2a-manifest" | jq .
```

里面有:`discoveryUrl`、`appClientId`、`registryId`、group 命名模板、authorizer 模板、
卡的必填字段、生命周期规则、gateway URL(如果启用)。

**它是从执行这些规则的代码生成的**(`shared/a2a_manifest.py` 读 `a2a_conformance` /
`a2a_groups` / `a2a_session`),所以"我们公布的"和"我们检查的"不可能不一致。有测试断言
把真实卡名代进 manifest 的 authorizer 模板之后,能通过审批门那个检查器。

把 `manifestVersion` 钉进你的 CI。字段只增不 bump,改语义会 bump。

---

## 2. 自动发生的部分

一条 `APPROVED` 的 `AGENT` 记录进 Registry 之后,以下都不需要任何人做任何事:

| 环节 | 机制 |
|---|---|
| Admin Console → 集成注册中心 → A2A 代理 列出你 | 每次请求实时 list+get,无缓存 |
| 管理员的授权页出现你的 skill 清单 | 同一份实时目录 |
| 编排器给你注册 `a2a_<agent>_<skill>` 工具 | 从 Registry 取 APPROVED 记录,60 秒缓存 |
| 委派提示词里出现你的路由说明 | 每请求从被授权的卡构建 |
| 你的 system prompt 可被管理员治理 | 合法 agentType 集合从 Registry 派生 |
| **Dashboard 统计包含你的 token / TTFT** | 白名单从 Registry 推导(见 §6) |
| **A2A gateway 给你建 target** | 从 Registry 收敛(见 §6) |

所以:**不需要提前知道主 Agent 的任何配置就能被发现。** 但"被发现"≠"能被调用"。

---

## 3. 你必须配的:Runtime 的 inbound authorizer

编排器调用你时,`Authorization: Bearer <终端用户自己的 Cognito idToken>`。没有 m2m token,
没有第二个头 —— 授权信息就在这个 token 的 `cognito:groups` claim 里。

`customJWTAuthorizer` 三件事:

1. **`discoveryUrl`** —— manifest 的 `authorizer.discoveryUrl`。
2. **`allowedAudience`** —— manifest 的 `authorizer.allowedAudience`。
   **不是 `allowedClients`**:后者校验 `client_id`,那只有 *access* token 才有;Cognito
   **idToken** 把 app client id 放在 `aud` 里。配成 `allowedClients` 的症状是一个完全被授权
   的用户被拒,报 `Claim 'client_id' value mismatch with configuration`。
3. **`customClaims`** —— `CONTAINS_ANY` 匹配 `cognito:groups`,值是**一个** group:
   `a2a-<cardName>`。

### 3.1 两层授权,读同一个 claim 的不同部分

```
门口 (你的 Runtime authorizer)   CONTAINS_ANY ["a2a-<cardName>"]
                                 ← 一个 group,你这个 agent 的生命周期内永不改变
容器里 (你的代码)                从同一个已验签 claim 里读 a2a-<cardName>.<skillId>
                                 ← 决定"这次调用被授了哪几个 skill"
```

**加 skill 不需要重新配 authorizer,也不需要重新部署 runtime。** 这是 2026-08-15 改的。
以前门口那份列表枚举了每个 skill,而 `CONTAINS_ANY` **没有通配符**,于是被授了新 skill 的
用户在门口被拒、且没有任何日志。那份枚举**换不来任何门禁强度** —— 门口只要命中任意一个就
放行,而容器随后用同一个 claim 做同样的判断。

**你的容器必须自己做 skill 层判断。** 门口只回答"能不能碰这个 agent",一个只持有
`a2a-<cardName>` 而没有任何 skill group 的调用方**会通过门口**。参考实现:
`a2a-agent-registry/common/server.py` 的 `skills_from_claims` + `enforce_allowed_skills`
(空集合 → 拒绝,而不是"不限制")。

**会改门口 group 的只有一件事:改卡名。** group 名按卡名编,改名对每个执行点都是一个新
agent,原有授权不会跟过去。

### 3.2 生成正确的配置

```bash
./venv/bin/python scripts/a2a-authorizer-contract.py --card path/to/card.json
./venv/bin/python scripts/a2a-authorizer-contract.py --record-id <recordId> --format cli
```

输出可以整段贴进 `UpdateAgentRuntime` 的 `authorizerConfiguration`。注意
`update-agent-runtime` 是 **full PUT** —— `roleArn`、`agentRuntimeArtifact`、
`networkConfiguration` 也要原样传回,否则会被清掉。

### 3.3 两个方向的静默失败

| 配错的方式 | 后果 | 你会看到什么 |
|---|---|---|
| `customClaims` 缺失或写松 | **池子里任何登录用户都能调你的全部 skill**,授权层等于不存在 | 什么都看不到。一切正常工作 |
| pool / audience 配错 | 已授权用户被门口拒掉(401) | 编排器照样注册工具,模型调用后返回 `A2A agent call failed: ...`,然后道歉 |

两种情况下 Registry 状态都是 `approved`。**这两种现在都过不了审批门(§5)。**

---

## 4. 卡本身的约束

- **卡名和每个 skill id 必须匹配 manifest 的 `groups.namePattern`**
  (`^[A-Za-z0-9][A-Za-z0-9_-]*$`)。group 名是 `a2a-<cardName>.<skillId>`,`.` 是分隔符;
  名字里有点、空格或其他字符 → 编不出 group 名 → 授权被**静默跳过**(只有一行 warning)。
  group 名总长上限 128。
- **卡必须能通过真正的 A2A schema 校验**。缺 `capabilities` / `defaultInputModes` /
  `securitySchemes` 会被 Registry 拒,报 "does not match any supported version" ——
  听起来像版本问题,其实是完整性问题。manifest 的 `card.requiredFields` 是清单。
- **改一张已 APPROVED 的卡会把记录打回 `DRAFT`**,需要重新审批。授权不会丢:撤销扫描给
  in-flight 记录留了一个重新审批窗口(manifest 的 `lifecycle.reapprovalGraceSeconds`,
  默认 1 小时)。
- **`DEPRECATED` 是终态,且记录会从 API 上消失** —— 只能重建,而重建会得到新的 recordId,
  原有授权全部失效。想临时停用请用 `reject`(可以再批准),**不要用 `deprecate`**。
- **改卡名 = 发一个新 agent。** 工具名、group 名、按 recordId 存的授权会同时变或失配。
  这条工具兜不住,需要你自己有版本纪律。

---

## 5. 系统怎么检查你 —— 而且这是**审批门**

`GET /registry/records?action=a2a-conformance`,控制台的 A2A Agents 页有一列 **Authorizer**:

- 从你的卡 `url` 回溯到 Runtime(直连 runtime URL,或经 A2A gateway 的 target),
- 读它的 `authorizerConfiguration`,
- 与卡推出的期望值比对,按方向报告。

**2026-08-15 起这不只是报告:不合规的 AGENT 记录批不过去。** 审批返回 409 并附上具体
finding。哪些拦、哪些不拦:

| 严重度 | 含义 | 审批 |
|---|---|---|
| **`open`** | 有人能调到他不该调的(如 `customClaims` 缺失) | **拦** |
| **`closed`** | 已授权的人被拒(如 pool / audience 配错) | **拦** |
| `info` | Runtime 读不到(比如在别的账号),或 authorizer 还在用旧的 per-skill 列表 | 放行,报告 |

`info` 不拦是刻意的:"读不到"不等于"配错了",而"还在用旧列表"的 runtime **今天是能用的**
(门口命中任意一个即放行),只是还带着"加 skill 要重部署"这个耦合。**把能用的东西报成红色,
是让检查被忽略的最快方式。**

这样纠错回路完全在你这边:失败信息里直接给你要跑的命令,不需要经过点 Approve 的那个人。
平台管理员可以 `?force=true` 强制批准,但会写进记录的 `statusReason` 留痕。

---

## 6. 以前要找平台团队、现在自动的事

- **A2A gateway target。** 本部署的内建 sub-agent 走一个专用 gateway(集中出口、每 target
  审计、Cedar per-agent kill switch)。你**用自己的 runtime URL 注册照样能用**,只是不在那层
  管控里。要进去:记录 APPROVED 之后,`POST /registry/records?action=a2a-gateway-reconcile&apply=true`
  会按 manifest 的 `gateway.targetNameTemplate`(= 你的卡名)给你建一个 `passthrough` target,
  然后把卡的 `url` 改成 `gateway.targetUrlTemplate` 填好的地址。
  reconcile 按 **runtime 而不是 target 名字**匹配已有 target,所以重复跑不会建出重复项。
- **Dashboard 统计。** 不再需要有人把你的 runtime ARN 加进 `DASHBOARD_EXTRA_RUNTIME_ARNS`。
  白名单现在从 Registry 的 APPROVED 记录推导(卡的 `url` → runtime ARN),APPROVED 就在里面。

---

## 7. 自己验证,不需要我们在场

```bash
# 只做离线检查:卡名/skill id 能否编成 group、卡是否完整、url 形状
./venv/bin/python scripts/a2a-delegation-smoke.py --record-id <recordId> --offline

# 真的打两次:一个被授权的用户 + 一个没被授权的用户
./venv/bin/python scripts/a2a-delegation-smoke.py --record-id <recordId> \
    --granted-user alice@example.com --ungranted-user bob@example.com
```

它断言**两个方向**:被授权的拿到 200 和真实回复,**没被授权的在门口拿到 401/403**。

第二条才是关键。只测第一条的话,一个 `customClaims` 完全缺失、池子里谁都能调的 agent
**也会通过**。脚本还会拒绝给出无意义的结论:如果你传的"未授权用户"其实持有这个 agent 的
group,它报 FAIL 而不是 PASS;5xx 或传输错误也算 FAIL,因为那不能证明门是关着的。

---

## 8. 最小检查清单

1. 从 Admin Console 复制 **平台契约(manifest)**,钉住 `manifestVersion`
2. 卡名和 skill id 通过 `groups.namePattern`
3. Runtime 部署完成,`protocolConfiguration.serverProtocol = A2A`
4. 跑 `scripts/a2a-authorizer-contract.py`,把输出配到 `authorizerConfiguration`
5. 容器里自己做 skill 层判断(空集合 → 拒绝)
6. 卡的 `url` 指向你的 runtime invocations 地址
7. `scripts/a2a-delegation-smoke.py --offline` 全绿
8. 提交记录到 Registry 并等审批 → `APPROVED`(不合规会 409 并告诉你哪里)
9. 让管理员授权(全局或按用户),然后跑一次带两个用户的 smoke test
10. 需要的话:`action=a2a-gateway-reconcile&apply=true` 进 gateway

## 9. 我们不会下放的两件事

- **审批和授权。** 注册让你**可被授权**;是否授权是平台的决定。否则一个 agent 可以给
  自己授权。
- **接受本平台的 Cognito 池。** 共享用户就意味着共享身份提供方。能弱化的只是"怎么知道
  这些值"(→ manifest),不是"要不要知道"。

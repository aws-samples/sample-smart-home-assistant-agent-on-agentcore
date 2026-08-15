# 接入契约：把一个 A2A Agent 接进这套系统

这份文档是给**不了解主 Agent 任何配置**的人看的。照着做,你的 Agent 会被自动发现并可以
被授权使用;不照着做,失败是**静默的**,而且有两个方向。

日期：2026-08-15

---

## 0. 一句话版本

> **注册到 Registry 让你被发现;你自己 Runtime 的 authorizer 决定谁能调用你。**

第一件事完全自动 —— 主 Agent、Admin Console、编排器都不需要改代码或重新部署。第二件事
是你必须配的,而且需要知道本部署的两个值(见 §2)。控制台会检查这两件事是否一致,不一致
会标出来(见 §4)。

---

## 1. 自动发生的部分

一条 `APPROVED` 的 `AGENT` 记录进 Registry 之后,以下都不需要任何人做任何事:

| 环节 | 机制 |
|---|---|
| Admin Console → Integration Registry → A2A Agents 列出你 | 每次请求实时 list+get,无缓存 |
| 管理员的授权页出现你的 skill 清单 | 同一份实时目录 |
| 编排器给你注册 `a2a_<agent>_<skill>` 工具 | 从 Registry 取 APPROVED 记录,60 秒缓存 |
| 委派提示词里出现你的路由说明 | 每请求从被授权的卡构建 |
| 你的 system prompt 可被管理员治理 | 合法 agentType 集合从 Registry 派生 |

所以:**不需要提前知道主 Agent 的任何配置就能被发现。** 但"被发现"≠"能被调用"。

---

## 2. 你必须配的:Runtime 的 inbound authorizer

编排器调用你时,`Authorization: Bearer <终端用户自己的 Cognito idToken>`。没有 m2m token,
没有第二个头 —— 授权信息就在这个 token 的 `cognito:groups` claim 里。

你的 Runtime 必须配 `customJWTAuthorizer`,包含三件事:

1. **`discoveryUrl`** —— 本部署的 Cognito user pool 的 OIDC discovery URL。
2. **`allowedAudience`** —— 本部署的 app client id。
   **不是 `allowedClients`**:后者校验 `client_id`,那只有 *access* token 才有;Cognito
   **idToken** 把 app client id 放在 `aud` 里。配成 `allowedClients` 的症状是一个完全被授权
   的用户被拒,报 `Claim 'client_id' value mismatch with configuration`。
3. **`customClaims`** —— 用 `CONTAINS_ANY` 匹配 `cognito:groups`,值是你这张卡的全部
   grant group 名:`a2a-<cardName>.<skillId>`。

**不要手抄这些值。** 跑生成器,它从本部署读出正确的 pool / client,并从你的卡推出 group 名:

```bash
./venv/bin/python scripts/a2a-authorizer-contract.py --card path/to/card.json
# 已经注册过了就用 record id:
./venv/bin/python scripts/a2a-authorizer-contract.py --record-id <recordId>
# 想直接拿命令:--format cli   想拿 boto3 片段:--format python
```

输出可以整段贴进 `UpdateAgentRuntime` 的 `authorizerConfiguration`。注意
`update-agent-runtime` 是 **full PUT** —— `roleArn`、`agentRuntimeArtifact`、
`networkConfiguration` 也要原样传回,否则会被清掉。

### 两个方向的静默失败

| 配错的方式 | 后果 | 你会看到什么 |
|---|---|---|
| `customClaims` 缺失或写松 | **池子里任何登录用户都能调你的全部 skill**,授权层等于不存在 | 什么都看不到。一切正常工作 |
| pool / audience 配错 | 已授权用户被门口拒掉(401) | 编排器照样注册工具,模型调用后返回 `A2A agent call failed: ...`,然后道歉 |

两种情况下 Integration Registry 页面都显示 `approved` —— 它读的是 Registry 状态,不是你的
authorizer。这正是 §4 那个检查存在的原因。

---

## 3. 卡本身的约束

- **卡名和每个 skill id 必须匹配 `^[A-Za-z0-9][A-Za-z0-9_-]*$`**。group 名是
  `a2a-<cardName>.<skillId>`,`.` 是分隔符;名字里有点、空格或其他字符 → 编不出 group 名 →
  授权被**静默跳过**(只有一行 warning)。group 名总长上限 128。
- **卡必须能通过真正的 A2A schema 校验**。缺 `capabilities` / `defaultInputModes` /
  `securitySchemes` 会被 Registry 拒,报 "does not match any supported version" ——
  听起来像版本问题,其实是完整性问题。
- **事后加 skill 需要重新配 authorizer。** `CONTAINS_ANY` 没有通配符,新 skill 的 group
  必须被显式列进去;在那之前,被授权了新 skill 的用户会被门口拒,且没有任何提示。
  重跑 §2 的生成器即可。
- **改一张已 APPROVED 的卡会把记录打回 `DRAFT`**,需要重新审批。授权不会丢:撤销扫描给
  in-flight 记录留了一个重新审批窗口(默认 1 小时)。但 `DEPRECATED` 是**终态且记录会从
  API 上消失** —— 只能重建,而重建会得到新的 recordId,原有授权全部失效。想临时停用请用
  `reject`(可以再批准),不要用 `deprecate`。

---

## 4. 系统怎么检查你

`GET /registry/records?action=a2a-conformance`,控制台的 A2A Agents 页有一列 **Authorizer**:

- 从你的卡 `url` 回溯到 Runtime(直连 runtime URL,或经 A2A gateway 的 target),
- 读它的 `authorizerConfiguration`,
- 与卡推出的期望值比对,按方向报告:**`Too permissive`(红)** / **`Callers refused`(黄)**。

抽屉里会列出具体 finding 和修复命令。规则本身在 `shared/a2a_conformance.py`,和 §2 生成器
用的是**同一个函数** —— 所以"我们检查什么"和"我们让你怎么配"不可能不一致。

---

## 5. 可选的两件事

- **走 A2A gateway。** 本部署的 8 个内部 sub-agent 的卡指向 `smarthome-a2a-gw`(集中出口、
  每 target 审计、Cedar per-agent kill switch)。你用自己的 runtime URL 注册**照样能用**,
  只是不在那层管控里。要进来需要在 gateway 上加一个 `passthrough` target
  (`protocolType: A2A`,出站 `JWT_PASSTHROUGH`),然后把卡的 url 改成 `{gatewayUrl}/{target}`。
- **进 Dashboard 统计。** 需要把你的 runtime ARN 加进 admin Lambda 的
  `DASHBOARD_EXTRA_RUNTIME_ARNS`,否则 span/token 统计不包含你。

---

## 6. 最小检查清单

1. 卡名和 skill id 通过 `^[A-Za-z0-9][A-Za-z0-9_-]*$`
2. Runtime 部署完成,`protocolConfiguration.serverProtocol = A2A`
3. 跑 `scripts/a2a-authorizer-contract.py`,把输出配到 Runtime 的 `authorizerConfiguration`
4. 卡的 `url` 指向你的 runtime invocations 地址(或 gateway target)
5. 提交记录到 Registry 并等审批 → `APPROVED`
6. 打开 Admin Console → Integration Registry → A2A Agents,确认 **Authorizer = Matches card**
7. 让管理员在 SubAgent Policy 里授权(全局或按用户),然后**重新登录** —— 授权在
   `cognito:groups` claim 里,要换一个新 token 才生效

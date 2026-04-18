# Smart Home Assistant Agent — Agent Harness 管理平台

> **Agent Harness 管理平台**，以智能家居场景为示例，展示如何在 AWS AgentCore 上构建完整的 Agent 运维管控体系：技能编排、模型选择、工具权限（per-user Cedar 策略）、**企业知识库**、外部集成、会话监控、长期记忆查看和质量评估。

基于 AWS AgentCore Runtime/Memory/Gateway 构建的 AI 智能家居控制系统。通过 Strands Agent 托管在 AgentCore Runtime 上，用户可以使用自然语言聊天机器人控制模拟 IoT 设备（LED 矩阵灯、电饭煲、风扇、烤箱）。设备控制命令通过 AgentCore Gateway 发送，并通过 AWS IoT Core 进行实时 MQTT 通信。管理控制台（Agent Harness Management）提供 8 个管理维度，覆盖 Agent 全生命周期的运维管控需求，包括基于 AWS Bedrock Knowledge Base 的企业知识库管理，支持按用户维度进行文档隔离和权限控制。

![architecture](docs/screenshots/architecture.drawio.png)
![chatbot](docs/screenshots/smarthomeassistant-chat.png)
![device simulator](docs/screenshots/smarthomeassistant-devices-v2.png)
![admin console](docs/screenshots/smarthomeassistant-admin.png)

```
smarthome-assistant-agent/
├── cdk/                     # AWS CDK — Cognito, IoT Core, Lambda, DynamoDB, API Gateway, S3, CloudFront
│   ├── lib/smarthome-stack.ts
│   └── lambda/
│       ├── iot-control/     # 验证并发布 MQTT 命令
│       ├── iot-discovery/   # 返回可用设备列表
│       ├── admin-api/       # 技能、模型、工具权限、知识库、记忆、会话管理
│       ├── kb-query/        # 企业知识库查询（Bedrock KB 检索 + 按用户 metadata 过滤）
│       └── user-init/       # Cognito 注册触发器 — 新用户自动分配工具权限
├── device-simulator/        # React 应用 — 4 个模拟 IoT 设备
├── chatbot/                 # React 应用 — 带 Cognito 认证的聊天 UI
├── admin-console/           # React 应用 — Agent Harness 管理控制台
├── agent/                   # Strands Agent（部署到 AgentCore Runtime）
│   ├── agent.py             # BedrockAgentCoreApp 入口
│   ├── skills/              # 备用设备控制 SKILL.md 文件
│   └── pyproject.toml       # AgentCore 代码打包依赖
├── scripts/
│   ├── 01-install-deps.sh     # 安装 CDK 依赖 + 为 Lambda 打包 boto3
│   ├── 02-build-frontends.sh  # 构建 3 个 React 前端
│   ├── 03-cdk-bootstrap.sh    # CDK 引导（幂等）
│   ├── 04-cdk-deploy.sh       # 部署 CDK 堆栈（Cognito/IoT/Lambda/KB/S3/CF）
│   ├── 05-fix-cognito.sh      # 启用 Cognito 自助注册 + 邮箱验证
│   ├── 06-deploy-agentcore.sh # 部署 AgentCore（Gateway/Target/Runtime/Memory）
│   ├── 07-seed-skills.sh      # 将内置技能写入 DynamoDB
│   ├── setup-agentcore.py     # （被 step 6 调用）创建 Gateway、Target、Runtime
│   ├── seed-skills.py         # （被 step 7 调用）将 SKILL.md 写入 DynamoDB
│   └── teardown-agentcore.py  # 销毁 AgentCore 资源
├── docs/                    # 架构与设计文档
└── deploy.sh                # 一键部署（依次调用上面 7 个 scripts/0*.sh）
```

## 前置条件

| 条件 | 版本 | 用途 |
|------|------|------|
| Node.js | >= 18.x | 构建 React 应用、运行 CDK |
| npm | >= 9.x | 包管理 |
| Python 3 | >= 3.12 | AgentCore 部署脚本、Agent 代码 |
| boto3 | 最新 | 部署脚本中的 AgentCore API 调用 |
| agentcore CLI | 最新 | 部署 AgentCore 资源（`pip install strands-agents-builder`） |
| AWS CLI | >= 2.x | AWS 凭证配置 |
| AWS 账号 | — | 需开通 Bedrock AgentCore 和 Kimi-2.5 模型访问权限 |

**重要：** 部署前需在 [Bedrock 控制台 > 模型访问](https://console.aws.amazon.com/bedrock/home#/modelaccess) 中申请 **Kimi K2.5**（`moonshotai.kimi-k2.5`）的访问权限。

### 部署者 IAM 权限

执行 `deploy.sh` 的 IAM 用户/角色需要以下 AWS 服务权限：

| AWS 服务 | 权限 | 用途 |
|---------|------|------|
| **CloudFormation** | `CreateStack`, `UpdateStack`, `DeleteStack`, `DescribeStacks`, `DescribeStackEvents`, `CreateChangeSet`, `DescribeChangeSet`, `ExecuteChangeSet`, `GetTemplate`, `ListStacks` | CDK 和 agentcore CLI 部署 |
| **S3** | `CreateBucket`, `DeleteBucket`, `PutObject`, `GetObject`, `DeleteObject`, `ListBucket`, `PutBucketPolicy`, `PutBucketCors`, `PutBucketVersioning`, `GetBucketLocation` | CDK 资产桶、静态网站桶、技能文件桶、config.js 写入 |
| **CloudFront** | `CreateDistribution`, `GetDistribution`, `UpdateDistribution`, `DeleteDistribution`, `CreateInvalidation` | 三个前端 CDN 分发 |
| **Lambda** | `CreateFunction`, `GetFunction`, `GetFunctionConfiguration`, `UpdateFunctionConfiguration`, `UpdateFunctionCode`, `AddPermission`, `RemovePermission`, `DeleteFunction` | 5 个 Lambda 函数（iot-control、iot-discovery、admin-api、kb-query、user-init） |
| **DynamoDB** | `CreateTable`, `DeleteTable`, `DescribeTable`, `PutItem`, `Query`, `Scan` | 技能表创建 + seed-skills.py 写入初始数据 |
| **Bedrock** | `CreateKnowledgeBase`, `DeleteKnowledgeBase`, `GetKnowledgeBase`, `CreateDataSource`, `StartIngestionJob`, `GetIngestionJob`, `Retrieve` | 企业知识库创建、文档同步和检索 |
| **OpenSearch Serverless** | `CreateCollection`, `DeleteCollection`, `CreateSecurityPolicy`, `CreateAccessPolicy`, `UpdateAccessPolicy`, `BatchGetCollection`, `APIAccessAll` | 知识库向量存储（AOSS 集合 + 索引） |
| **Cognito** | `CreateUserPool`, `UpdateUserPool`, `DeleteUserPool`, `CreateUserPoolClient`, `CreateUserPoolDomain`, `AdminCreateUser`, `AdminSetUserPassword`, `CreateUserPoolGroup`, `AdminAddUserToGroup` | 用户池、管理员用户、admin 组 |
| **Cognito Identity** | `CreateIdentityPool`, `SetIdentityPoolRoles`, `DeleteIdentityPool` | 设备模拟器 MQTT 认证 |
| **IoT Core** | `DescribeEndpoint`, `CreateThing`, `DeleteThing` | IoT 端点发现 + 设备 Thing 创建 |
| **IAM** | `CreateRole`, `DeleteRole`, `GetRole`, `PutRolePolicy`, `DeleteRolePolicy`, `AttachRolePolicy`, `DetachRolePolicy`, `PassRole`, `CreateServiceLinkedRole` | Lambda 执行角色、Cognito 角色、Gateway 角色 |
| **Bedrock AgentCore** | `Create/Get/Update/Delete` Gateway、AgentRuntime、PolicyEngine、Policy、Memory、Evaluator、OnlineEval；`ListGatewayTargets`、`GetGatewayTarget`、`ListPolicies`、`ListPolicyEngines` | Gateway、Runtime、策略引擎、Memory、Evaluator 全生命周期 |
| **CloudWatch Logs** | `CreateLogGroup`, `PutRetentionPolicy`, `DeleteLogGroup` | Lambda 日志组 |
| **STS** | `GetCallerIdentity` | 部署脚本获取账号 ID |

<details>
<summary>最小 IAM 策略 JSON（点击展开）</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudFormation",
      "Effect": "Allow",
      "Action": [
        "cloudformation:CreateStack", "cloudformation:UpdateStack", "cloudformation:DeleteStack",
        "cloudformation:DescribeStacks", "cloudformation:DescribeStackResources",
        "cloudformation:DescribeStackEvents", "cloudformation:GetTemplate",
        "cloudformation:ListStacks", "cloudformation:CreateChangeSet",
        "cloudformation:DescribeChangeSet", "cloudformation:ExecuteChangeSet"
      ],
      "Resource": [
        "arn:aws:cloudformation:*:*:stack/SmartHomeAssistantStack/*",
        "arn:aws:cloudformation:*:*:stack/AgentCore-*/*",
        "arn:aws:cloudformation:*:*:stack/CDKToolkit/*"
      ]
    },
    {
      "Sid": "S3",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket", "s3:DeleteBucket", "s3:GetBucketLocation",
        "s3:PutBucketPolicy", "s3:GetBucketPolicy", "s3:PutBucketVersioning",
        "s3:PutBucketCors", "s3:PutObject", "s3:GetObject", "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::smarthome-*", "arn:aws:s3:::smarthome-*/*",
        "arn:aws:s3:::cdk-*-assets-*", "arn:aws:s3:::cdk-*-assets-*/*"
      ]
    },
    {
      "Sid": "CloudFront",
      "Effect": "Allow",
      "Action": [
        "cloudfront:CreateDistribution", "cloudfront:GetDistribution",
        "cloudfront:GetDistributionConfig", "cloudfront:UpdateDistribution",
        "cloudfront:DeleteDistribution", "cloudfront:CreateInvalidation",
        "cloudfront:CreateOriginAccessControl",
        "cloudfront:CreateCloudFrontOriginAccessIdentity",
        "cloudfront:GetCloudFrontOriginAccessIdentity",
        "cloudfront:DeleteCloudFrontOriginAccessIdentity"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Lambda",
      "Effect": "Allow",
      "Action": [
        "lambda:CreateFunction", "lambda:GetFunction",
        "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionConfiguration",
        "lambda:UpdateFunctionCode", "lambda:AddPermission",
        "lambda:RemovePermission", "lambda:DeleteFunction",
        "lambda:InvokeFunction"
      ],
      "Resource": "arn:aws:lambda:*:*:function:smarthome-*"
    },
    {
      "Sid": "LambdaCDKCustomResource",
      "Effect": "Allow",
      "Action": [
        "lambda:CreateFunction", "lambda:GetFunction",
        "lambda:UpdateFunctionConfiguration", "lambda:UpdateFunctionCode",
        "lambda:DeleteFunction", "lambda:AddPermission",
        "lambda:RemovePermission", "lambda:InvokeFunction"
      ],
      "Resource": "arn:aws:lambda:*:*:function:SmartHomeAssistantStack-*"
    },
    {
      "Sid": "DynamoDB",
      "Effect": "Allow",
      "Action": [
        "dynamodb:CreateTable", "dynamodb:DeleteTable", "dynamodb:DescribeTable",
        "dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"
      ],
      "Resource": "arn:aws:dynamodb:*:*:table/smarthome-skills"
    },
    {
      "Sid": "Cognito",
      "Effect": "Allow",
      "Action": [
        "cognito-idp:CreateUserPool", "cognito-idp:UpdateUserPool",
        "cognito-idp:DeleteUserPool", "cognito-idp:DescribeUserPool",
        "cognito-idp:CreateUserPoolClient", "cognito-idp:DeleteUserPoolClient",
        "cognito-idp:CreateUserPoolDomain", "cognito-idp:DeleteUserPoolDomain",
        "cognito-idp:AdminCreateUser", "cognito-idp:AdminSetUserPassword",
        "cognito-idp:AdminDeleteUser", "cognito-idp:AdminAddUserToGroup",
        "cognito-idp:CreateGroup",
        "cognito-idp:ListUsers", "cognito-idp:AdminListGroupsForUser"
      ],
      "Resource": "arn:aws:cognito-idp:*:*:userpool/*"
    },
    {
      "Sid": "CognitoIdentity",
      "Effect": "Allow",
      "Action": [
        "cognito-identity:CreateIdentityPool", "cognito-identity:DeleteIdentityPool",
        "cognito-identity:SetIdentityPoolRoles",
        "cognito-identity:DescribeIdentityPool",
        "cognito-identity:UpdateIdentityPool"
      ],
      "Resource": "*"
    },
    {
      "Sid": "IoTCore",
      "Effect": "Allow",
      "Action": [
        "iot:DescribeEndpoint", "iot:CreateThing", "iot:DeleteThing", "iot:DescribeThing"
      ],
      "Resource": "*"
    },
    {
      "Sid": "IAM",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:ListRoles",
        "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy",
        "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies",
        "iam:PassRole", "iam:CreateServiceLinkedRole", "iam:TagRole"
      ],
      "Resource": "*"
    },
    {
      "Sid": "BedrockAgentCore",
      "Effect": "Allow",
      "Action": "bedrock-agentcore:*",
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup", "logs:DeleteLogGroup",
        "logs:PutRetentionPolicy", "logs:DescribeLogGroups"
      ],
      "Resource": "arn:aws:logs:*:*:log-group:/aws/lambda/smarthome-*"
    },
    {
      "Sid": "CloudWatchLogsCDK",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup", "logs:DeleteLogGroup",
        "logs:PutRetentionPolicy", "logs:DescribeLogGroups"
      ],
      "Resource": "arn:aws:logs:*:*:log-group:/aws/lambda/SmartHomeAssistantStack-*"
    },
    {
      "Sid": "STS",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    },
    {
      "Sid": "APIGateway",
      "Effect": "Allow",
      "Action": [
        "apigateway:POST", "apigateway:GET", "apigateway:PUT",
        "apigateway:DELETE", "apigateway:PATCH"
      ],
      "Resource": "arn:aws:apigateway:*::/*"
    },
    {
      "Sid": "SSM",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:*:*:parameter/cdk-bootstrap/*"
    },
    {
      "Sid": "ECR",
      "Effect": "Allow",
      "Action": [
        "ecr:CreateRepository", "ecr:DescribeRepositories",
        "ecr:SetRepositoryPolicy", "ecr:GetRepositoryPolicy"
      ],
      "Resource": "arn:aws:ecr:*:*:repository/cdk-*"
    }
  ]
}
```

</details>

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

`deploy.sh` 是一个薄封装，会依次调用 `scripts/0[1-7]-*.sh` 7 个拆分脚本。每个脚本都能独立运行，**脚本开头会打印自己创建了哪些 AWS 资源**，便于调试和只重跑某一步。

| 步骤 | 脚本 | 部署内容 |
|------|------|---------|
| 1 | `scripts/01-install-deps.sh` | 安装 `cdk/` 的 npm 依赖；将最新 `boto3` 打包进 `admin-api`、`user-init`、`kb-query` 三个 Lambda 目录（Lambda 自带的 boto3 过旧，缺少 AgentCore 控制面 API）。 |
| 2 | `scripts/02-build-frontends.sh` | 构建 `device-simulator`、`chatbot`、`admin-console` 三个 React 应用，产物位于各自的 `build/`。 |
| 3 | `scripts/03-cdk-bootstrap.sh` | 运行 `cdk bootstrap`（幂等）；创建 `CDKToolkit` 资产桶、ECR 仓库和部署角色。 |
| 4 | `scripts/04-cdk-deploy.sh` | 部署 **CDK 堆栈**（`SmartHomeAssistantStack`）：Cognito 用户池/身份池/admin 组与默认管理员用户；IoT Core 设备与端点；5 个 Lambda（iot-control、iot-discovery、admin-api、kb-query、user-init）；DynamoDB 表 `smarthome-skills`；S3 桶 `smarthome-skill-files` 与 `smarthome-kb-docs`；OpenSearch Serverless 集合；Bedrock Knowledge Base + S3 数据源；带 Cognito 授权器的 API Gateway；三个前端的 S3 + CloudFront。输出写入 `cdk-outputs.json`。 |
| 5 | `scripts/05-fix-cognito.sh` | 直接调用 `aws cognito-idp update-user-pool`，补齐 CDK 未可靠传递的两项设置：启用自助注册 + 邮箱自动验证。 |
| 6 | `scripts/06-deploy-agentcore.sh` | 部署 **AgentCore 堆栈**（由 `agentcore` CLI 管理）：Gateway（CUSTOM_JWT 认证）、指向 iot-control / iot-discovery / kb-query 的 Lambda Target、Runtime（Strands Agent CodeZip）、Memory（语义/摘要/偏好提取策略）、Policy Engine 与每工具的 Cedar permit 策略；初始化企业知识库（AOSS 索引 + Bedrock KB 数据源）；给 Runtime 注入 `SKILLS_TABLE_NAME` 和 `requestHeaderAllowlist: ["Authorization"]`。 |
| 7 | `scripts/07-seed-skills.sh` | 从 `agent/skills/*/SKILL.md` 读取内置技能，写入 DynamoDB 作为 `__global__` 技能（幂等，可重复运行）。 |

部署完成后，`deploy.sh` 会输出三个前端的 URL 和管理员凭证。

---

## 工作原理

部署会创建两个独立的 CloudFormation 堆栈：

**CDK 堆栈** (`SmartHomeAssistantStack`) — 标准 AWS 资源：
- Cognito 用户池 + 身份池 + 管理员组和默认管理员用户
- IoT Core 设备 + 端点查询
- Lambda 函数：iot-control（MQTT）、iot-discovery（设备列表）、admin-api（技能、模型、工具权限、知识库、记忆、会话）、kb-query（知识库检索）、user-init（新用户自动分配工具权限）
- DynamoDB 表（smarthome-skills）用于 Agent 技能存储、用户设置、知识库配置和会话追踪
- S3 桶（smarthome-skill-files）用于技能目录文件（scripts、references、assets），符合 [Agent Skills 规范](https://agentskills.io/specification)
- S3 桶（smarthome-kb-docs）用于企业知识库文档存储，按用户范围（S3 prefix）隔离
- OpenSearch Serverless 集合（smarthome-kb）用于知识库向量索引
- Bedrock Knowledge Base + S3 数据源用于文档向量化和语义检索（Embedding: `cohere.embed-multilingual-v3`）
- API Gateway + Cognito 授权器用于管理 API
- S3 + CloudFront 用于设备模拟器、聊天机器人和管理控制台

**AgentCore 堆栈**（由 `agentcore` CLI 管理）— AgentCore 资源：
- AgentCore Gateway（MCP 服务器），CUSTOM_JWT 认证（Cognito）用于按用户工具策略执行
- Gateway Lambda Target 指向 iot-control、iot-discovery 和 kb-query Lambda
- AgentCore Runtime 运行 Strands Agent（CodeZip，Python 3.13）
- AgentCore Memory，含语义、摘要和用户偏好提取策略

部署脚本（`scripts/setup-agentcore.py`）桥接两者：读取 CDK 输出、创建 `agentcore` 项目、注入 Agent 代码、添加 memory + gateway + targets（含 kb-query）、部署所有资源，然后初始化企业知识库（创建 AOSS 向量索引、Bedrock KB、S3 数据源），配置 runtime 的 `SKILLS_TABLE_NAME`、`requestHeaderAllowlist: ["Authorization"]`（用于 JWT 转发到 gateway），并授予 DynamoDB 和 Bedrock Retrieve 访问权限。最后 `scripts/seed-skills.py` 将 5 个内置技能写入 DynamoDB。

---

## 管理控制台功能

管理控制台（"Agent Harness Management"）是独立的 React 管理应用。使用 `admin` Cognito 组中的用户登录（默认管理员凭证在部署输出中显示）。

### 技能（Skills Tab）
- **完整 [Agent Skills 规范](https://agentskills.io/specification) 支持**：所有字段（名称、描述、允许工具、许可证、兼容性、元数据）均可编辑
- **全局技能**（`__global__`）对所有用户共享；**按用户技能**可覆盖同名全局技能
- 使用 Markdown 指令编辑器创建、编辑和删除技能
- **元数据编辑器**：动态键值对编辑
- **技能文件管理器**：上传、下载和删除 `scripts/`、`references/`、`assets/` 目录中的文件（存储在 S3，通过预签名 URL 管理）
- 技能存储在 DynamoDB（元数据 + 指令）和 S3（目录文件），每次调用动态加载，无需重新部署 Agent

### 模型（Models Tab）
- **全局默认模型**：通过下拉菜单为所有用户设置 LLM 模型
- **按用户模型覆盖**：表格列出所有 Cognito 用户，每行有独立的模型选择下拉框。按用户设置优先于全局默认。
- 可选模型包括 Kimi K2.5、Claude 4.5/4.6、DeepSeek、Qwen、Llama 4 和 OpenAI GPT
- Agent 在每次调用时从 DynamoDB 读取模型设置

### 工具权限（Tool Access Tab）
- **按用户工具权限**：列出所有 Cognito 用户，选择每个用户可以调用的 gateway 工具
- **策略引擎模式切换**：ENFORCE（策略阻止未授权访问）/ LOG_ONLY（仅审计）
- **AgentCore Policy Engine 集成**：通过 Cedar 策略在 gateway 层面执行权限
  - 每个工具一个 Cedar `permit` 策略，`principal.id` 匹配用户的 Cognito `sub`
  - Gateway 使用 CUSTOM_JWT 认证；Runtime 通过 `requestHeaderAllowlist: ["Authorization"]` 转发用户 JWT
  - 默认拒绝：未配置工具权限的用户无法调用 gateway 工具
- **演示入口**：用户表格每行提供 **打开聊天机器人** 与 **打开设备模拟器** 两个按钮，新标签页直达该用户的聊天界面（URL 带 `?username=<email>` 自动预填登录）与对应的设备模拟页，方便管理员现场演示

### 企业知识库（Knowledge Base Tab）
- **基于 AWS Bedrock Knowledge Base** 的 RAG 检索增强生成
- **按用户文档隔离**：S3 prefix（`__shared__/` 公共 + `user@email/` 私有）+ metadata 过滤
- **文档管理**：上传、列表、删除文档，支持 PDF/TXT/MD/DOCX/CSV 等格式
- **同步管理**：一键触发 Bedrock KB 向量化 ingestion，查看同步状态和统计
- **用户范围选择器**：下拉框展示 `公共知识（所有用户）` + 所有 Cognito 用户
- **安全**：Agent 代码中的本地 tool wrapper 自动从 Runtime 验证的用户身份注入 `user_id`，LLM 无法伪造或篡改；Gateway Cedar 策略确保仅认证用户可调用
- **向量存储**：OpenSearch Serverless (AOSS) + `cohere.embed-multilingual-v3`（1024 维，中英文）

### 集成（Integrations Tab）
- 显示当前工具集成类型（Lambda Targets — 已激活）和未来路线图
- 计划集成：MCP Servers、A2A Agents、API Gateway 端点

### 会话（Sessions Tab）
- 查看所有用户运行时会话（用户 ID、会话 ID、最后活跃时间）
- 每个用户有固定会话 ID（基于 Cognito 身份）
- **停止**按钮通过 AgentCore StopRuntimeSession API 终止用户会话

### 记忆（Memories Tab）
- 从 AgentCore Memory 查看每个用户的**长期记忆**
- 列出所有记忆参与者（与聊天机器人交互过的用户）
- 点击"查看记忆"显示提取的**事实**（语义知识）和**偏好**（用户偏好）
- 按创建时间倒序排列，显示类型标签、内容和时间戳

### 质量评估（Guardrails Tab）
- 链接到 **AgentCore Evaluator** 控制台（LLM-as-a-Judge 质量评估）
- 链接到 **Bedrock Guardrails** 控制台（内容过滤、PII 脱敏）
- 快速跳转到 Tool Access tab 中的 **Cedar Policy Engine** 设置

---

## 管理控制台操作指南

使用部署输出中的管理员凭证登录管理控制台。登录用户必须属于 Cognito `admin` 组。

### 管理技能

**创建技能：**
1. 进入 **Skills** Tab，从"用户范围"下拉框选择 `__global__`（全局）或指定用户
2. 点击 **Create Skill**，填写技能名称（小写字母、数字和连字符，如 `my-skill`）、描述（必填）和 Markdown 指令
3. 可选填写允许工具、许可证、兼容性和元数据键值对
4. 点击 **Create Skill** 提交

**编辑/删除技能：**
- 在技能列表中点击 **Edit** 修改描述、指令等字段（名称和用户范围不可修改）
- 点击 **Delete** 并确认即可删除技能

**管理技能文件：**
- 编辑技能时，表单下方显示文件管理器，包含 `scripts/`、`references/`、`assets/` 三个目录
- 点击 **Upload to scripts/** 等按钮上传文件，点击 **Download** 下载，点击 **Delete** 删除

### 配置模型

**设置全局默认模型：**
1. 进入 **Models** Tab，在顶部"全局默认模型"下拉框中选择模型
2. 点击 **Save** 保存（Agent 下次调用时生效）

**按用户覆盖模型：**
- 在下方用户表格中，为指定用户选择不同模型，点击对应行的 **Save**
- 选择"使用全局默认"可清除用户覆盖

### 管理工具权限

**配置策略引擎模式：**
1. 进入 **Tool Access** Tab，在顶部切换 **ENFORCE**（阻止未授权访问）或 **LOG_ONLY**（仅审计）

**分配用户工具权限：**
1. 在用户列表中点击目标用户的 **Manage Permissions**
2. 勾选/取消勾选该用户可使用的 Gateway 工具（可用 **Select All** / **Deselect All** 批量操作）
3. 点击 **Save Permissions** 保存（Cedar 策略即时生效）

**为用户演示（Demo 入口）：**
- 在用户列表每一行的 **演示入口** 列，点击 **打开聊天机器人** 或 **打开设备模拟器** 按钮
- 聊天机器人链接自动携带 `?username=<用户邮箱>`，登录表单会预填该用户名，管理员只需输入密码即可
- 两个链接均在新标签页打开，方便与管理控制台并排使用

### 监控会话

1. 进入 **Sessions** Tab 查看所有活跃的运行时会话
2. 点击 **Stop** 终止指定用户的会话
3. 点击 **Refresh** 刷新列表

### 查看用户记忆

1. 进入 **Memories** Tab，列表显示所有与聊天机器人交互过的用户
2. 点击 **View Memories** 查看该用户的长期记忆（事实和偏好）

### 管理企业知识库

**上传文档：**
1. 进入 **Knowledge Base** Tab，从"用户范围"下拉框选择 `公共知识（所有用户）`（文档对所有人可见）或指定用户邮箱（仅该用户可查）
2. 点击 **上传文档**，选择文件（支持 PDF、TXT、MD、DOCX、CSV 等）
3. 上传完成后系统自动创建 metadata sidecar 文件（标记文档所属范围）

**同步知识库：**
1. 上传/删除文档后，点击 **同步知识库** 触发 Bedrock KB 向量化
2. 同步状态表格实时显示 ingestion job 状态（STARTING → IN_PROGRESS → COMPLETE）
3. 同步完成后，Agent 即可通过 `query_knowledge_base` 工具检索新文档

**权限模型：**
- **公共文档**（`__shared__/` 前缀）：所有用户通过 Agent 聊天均可检索到
- **用户专属文档**（`user@email/` 前缀）：仅对应用户可检索
- Agent 代码中的 tool wrapper 从 Runtime 验证上下文自动注入用户身份，LLM 无法控制 `user_id` 参数

### 质量评估

- 进入 **Guardrails** Tab，点击 **Open Console** 跳转到 AgentCore Evaluator 或 Bedrock Guardrails 的 AWS 控制台
- 点击 **Go to Tool Access** 快速跳转到工具权限配置

---

## 分步部署

想知道某一步到底做了什么、或者上一次部署中间失败需要从中间重跑？直接调用 `scripts/` 中对应的脚本即可，每个脚本都是独立幂等的，开头会打印它创建的 AWS 资源。推荐顺序：

```bash
source venv/bin/activate          # 让 python/pip 指向 venv

scripts/01-install-deps.sh        # cdk/ npm install + 为 Lambda 打包 boto3
scripts/02-build-frontends.sh     # 构建三个 React 应用的静态产物
scripts/03-cdk-bootstrap.sh       # cdk bootstrap（每个账号/区域只需一次）
scripts/04-cdk-deploy.sh          # 部署 SmartHomeAssistantStack，写 cdk-outputs.json
scripts/05-fix-cognito.sh         # 启用 Cognito 自助注册 + 邮箱自动验证
scripts/06-deploy-agentcore.sh    # Gateway + Target + Runtime + Memory + KB 初始化
scripts/07-seed-skills.sh         # 将 agent/skills/ 下的 SKILL.md 写入 DynamoDB
```

每步对应的资源清单见上方快速开始下的表格。部署后只改了前端代码？只重跑 2 + 4；只改了 Agent Python 代码？只重跑 6；只想刷新内置技能？只重跑 7。

### 添加管理员用户（可选）

CDK 堆栈会创建默认管理员用户。添加更多管理员：

```bash
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <USER_POOL_ID> \
  --username <EMAIL> \
  --group-name admin
```

---

## 本地开发

### 设备模拟器

```bash
cd device-simulator && npm install && npm start  # http://localhost:3001
```

创建 `device-simulator/public/config.js`（使用 `cdk-outputs.json` 中的值）：
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

### Strands Agent

```bash
source venv/bin/activate
export AWS_REGION=us-west-2
export MODEL_ID=moonshotai.kimi-k2.5  # 或任何你有访问权限的 Bedrock 模型
cd agent && python agent.py  # 在 http://localhost:8080 启动服务
```

测试端点：
```bash
curl http://localhost:8080/ping
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"prompt": "把 LED 矩阵灯设为彩虹模式"}'
```

---

## 配置

### 更改 AI 模型

编辑 `agent/agent.py` 或设置 `MODEL_ID` 环境变量。默认：`moonshotai.kimi-k2.5`。管理员可通过管理控制台按用户覆盖模型，无需重新部署。

### 自定义域名

在 `cdk/lib/smarthome-stack.ts` 中添加：
```typescript
domainNames: ["chat.yourdomain.com"],
certificate: acm.Certificate.fromCertificateArn(this, "Cert", "arn:aws:acm:..."),
```

---

## 月度成本估算

本方案全部采用 AWS Serverless 托管服务，**无需预置服务器**，按实际用量付费，空闲时成本趋近于零。以下按日活用户（DAU）1 万、10 万、100 万三个量级估算月度成本（us-west-2 区域，价格截至 2025 年）。

### 成本假设

- 每用户每天平均 10 次对话，每次对话含 1 次 LLM 调用 + 1.5 次工具调用 + 0.3 次 KB 查询
- LLM 模型：Kimi K2.5（输入 ~800 tokens/次，输出 ~200 tokens/次）
- 知识库文档总量：1,000 个文档（~500MB），每月同步 4 次
- 管理员操作忽略不计

### 分项成本表

| 模块 | 服务 | 计费维度 | 1 万 DAU | 10 万 DAU | 100 万 DAU |
|------|------|---------|---------|----------|-----------|
| **AI Agent** | AgentCore Runtime | 调用次数 + 运行时长 | ~$150 | ~$1,500 | ~$15,000 |
| **LLM 推理** | Bedrock (Kimi K2.5) | Input/Output tokens | ~$80 | ~$800 | ~$8,000 |
| **工具路由** | AgentCore Gateway | MCP 调用次数 | ~$15 | ~$150 | ~$1,500 |
| **策略引擎** | AgentCore Policy Engine | 策略评估次数 | ~$5 | ~$50 | ~$500 |
| **长期记忆** | AgentCore Memory | 读写次数 + 存储 | ~$20 | ~$200 | ~$2,000 |
| **知识库检索** | Bedrock KB (Retrieve) | 查询次数 | ~$10 | ~$100 | ~$1,000 |
| **向量嵌入** | Bedrock (Cohere Embed) | Embedding tokens | ~$2 | ~$2 | ~$2 |
| **向量存储** | OpenSearch Serverless | OCU 小时（最低 2 OCU） | ~$350 | ~$350 | ~$700 |
| **文档存储** | S3 | 存储 + 请求 | <$1 | <$1 | ~$5 |
| **设备控制** | Lambda (iot-control) | 调用次数 | ~$3 | ~$30 | ~$300 |
| **管理 API** | API Gateway + Lambda | 调用次数 | <$1 | ~$5 | ~$50 |
| **用户认证** | Cognito | MAU（前 50,000 免费） | $0 | ~$250 | ~$4,500 |
| **前端托管** | S3 + CloudFront | 存储 + 流量 | ~$5 | ~$20 | ~$100 |
| **数据存储** | DynamoDB | 读写 + 存储（按需） | ~$5 | ~$50 | ~$500 |
| **质量评估** | AgentCore Evaluator | LLM-as-Judge 调用 | ~$10 | ~$100 | ~$1,000 |
| | | **月度总计** | **~$656** | **~$3,607** | **~$35,157** |
| | | **每用户每月** | **~$0.066** | **~$0.036** | **~$0.035** |

### Serverless 成本优势

- **零空闲成本**：Lambda、API Gateway、DynamoDB、AgentCore Runtime 在无请求时不产生费用（OpenSearch Serverless 最低 2 OCU 除外）
- **线性扩展**：从 1 万到 100 万用户，核心成本（Agent + LLM + Gateway）线性增长，无阶梯跳跃
- **无运维成本**：无需管理服务器、集群或容量规划，全部由 AWS 自动扩缩容
- **单用户成本递减**：规模从 1 万到 100 万时，单用户月成本从 $0.066 降至 $0.035，体现规模经济
- **OpenSearch Serverless** 是最大固定成本项（最低 ~$350/月），适合中大型部署；小型部署可考虑切换为 Bedrock 内置向量存储（预览中）以进一步降低成本

> **注意：** 以上为估算值，实际成本取决于具体使用模式、对话长度、模型选择和区域定价。建议使用 [AWS Pricing Calculator](https://calculator.aws/) 进行精确计算。AgentCore 和 Bedrock KB 定价可能随服务更新而变化。

---

## 销毁资源

**顺序很重要：** AgentCore 资源必须在 CDK 堆栈之前销毁，因为 AgentCore Gateway 引用了 Lambda 函数。

```bash
source venv/bin/activate

# 1. 先销毁 AgentCore 资源（Gateway、Target、Runtime）
python3 scripts/teardown-agentcore.py

# 2. 再销毁 CDK 堆栈（Cognito、IoT、Lambda、DynamoDB、S3、CloudFront）
cd cdk && npx cdk destroy --all --force
```

销毁脚本只删除 `agentcore-state.json` 中记录的资源，不会影响无关的 AgentCore 资源。

---

## 文档

详见 [docs/architecture-and-design.md](docs/architecture-and-design.md)，包含架构图、组件设计、API 参考和 MQTT 命令模式。

---

## 故障排除

### agentcore CLI 未找到

```bash
pip install strands-agents-builder
```

### agentcore deploy 失败："Target not found in aws-targets.json"

部署脚本会自动生成此文件。手动运行时需创建：
```json
[{"name": "default", "region": "us-west-2", "account": "YOUR_ACCOUNT_ID"}]
```

### CDK synth 失败："pyproject.toml not found"

Agent 目录必须包含 `pyproject.toml`，已包含在仓库中。

### Bedrock 模型访问被拒绝

前往 [Bedrock 控制台 > 模型访问](https://console.aws.amazon.com/bedrock/home#/modelaccess) 申请 Kimi K2.5（`moonshotai.kimi-k2.5`）。注意模型 ID 是 `moonshotai.kimi-k2.5`，不是 `us.kimi.kimi-2.5`。

### 设备模拟器 MQTT 连接失败

检查浏览器控制台中的 Cognito Identity Pool ID、IoT 端点和 IAM 角色权限。

### 聊天机器人请求失败

聊天机器人使用 HTTP POST 请求 `https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{arn}/invocations`。检查 config.js 中的 `agentRuntimeArn`、Cognito token 和 CloudWatch 中的 AgentCore Runtime 日志。

### 管理控制台："Access Denied"

登录用户必须属于 `admin` Cognito 组：
```bash
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <USER_POOL_ID> \
  --username <EMAIL> \
  --group-name admin
```

### 管理 API 返回 403 "Forbidden: admin group required"

同上 — JWT `cognito:groups` 声明必须包含 `admin`。

### DynamoDB 技能加载失败

检查 AgentCore Runtime 是否设置了 `SKILLS_TABLE_NAME` 环境变量，以及 runtime IAM 角色是否有技能表的 `dynamodb:Query`、`dynamodb:PutItem` 权限。部署脚本会自动处理，但在重新 `agentcore deploy` 后可能需要重新运行。

### 管理控制台会话中用户 ID 显示为 "default"

聊天机器人在 POST body 中将 `userId` 传给 Agent。如果显示 "default"，可能是聊天机器人提供了缓存的旧版本。硬刷新（`Ctrl+Shift+R`）或清除 CloudFront 缓存。

### 销毁失败："Gateway has targets associated"

必须先删除 AgentCore CloudFormation 堆栈。销毁脚本会自动处理。手动操作：
```bash
aws cloudformation delete-stack --stack-name AgentCore-smarthome-default
aws cloudformation wait stack-delete-complete --stack-name AgentCore-smarthome-default
```

### S3 桶名冲突

从 CDK 堆栈中移除 `bucketName` 让 CDK 自动生成唯一名称。

### CDK 自定义资源失败："@aws-sdk/client-bedrockagentcorecontrol does not exist"

这是预期行为 — CDK JS SDK 尚不包含 AgentCore 客户端。AgentCore 资源由 `agentcore` CLI 在单独步骤（部署的第 8 步）中创建，不由 CDK 创建。

---

## 安全

详见 [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications)。

## 许可证

本项目使用 MIT-0 许可证。详见 LICENSE 文件。

---
---

# English Version

> **Agent Harness management platform**, using a smart home scenario to demonstrate how to build a complete Agent operations and governance system on AWS AgentCore: skill orchestration, model selection, tool access control (per-user Cedar policies), **enterprise knowledge base**, external integrations, session monitoring, long-term memory viewing, and safety guardrails.

AI-powered smart home control system built on AWS AgentCore Runtime/Memory/Gateway. Natural language chatbot controls simulated IoT devices (LED Matrix, Rice Cooker, Fan, Oven) through a Strands Agent hosted on AgentCore Runtime, with remote control command through AgentCore Gateway and real-time MQTT communication with AWS IoT Core. The admin console (Agent Harness Management) provides 8 management dimensions covering the full Agent lifecycle, including an enterprise knowledge base powered by AWS Bedrock Knowledge Base with per-user document isolation and access control.

```
smarthome-assistant-agent/
├── cdk/                     # AWS CDK — Cognito, IoT Core, Lambda, DynamoDB, API Gateway, S3, CloudFront
│   ├── lib/smarthome-stack.ts
│   └── lambda/
│       ├── iot-control/     # Validates & publishes MQTT commands
│       ├── iot-discovery/   # Returns available device list
│       ├── admin-api/       # Skills, models, tool access, knowledge base, memories, sessions
│       ├── kb-query/        # Enterprise KB query (Bedrock KB retrieval + per-user metadata filtering)
│       └── user-init/       # Cognito signup trigger — auto-provision tool permissions
├── device-simulator/        # React app — 4 simulated IoT devices
├── chatbot/                 # React app — chat UI with Cognito auth
├── admin-console/           # React app — Agent Harness Management console
├── agent/                   # Strands Agent (deployed to AgentCore Runtime)
│   ├── agent.py             # BedrockAgentCoreApp entrypoint
│   ├── skills/              # Fallback device control SKILL.md files
│   └── pyproject.toml       # Dependencies for AgentCore code packaging
├── scripts/
│   ├── 01-install-deps.sh     # CDK npm deps + bundle boto3 into Lambda dirs
│   ├── 02-build-frontends.sh  # Build the 3 React frontends
│   ├── 03-cdk-bootstrap.sh    # CDK bootstrap (idempotent)
│   ├── 04-cdk-deploy.sh       # Deploy CDK stack (Cognito/IoT/Lambda/KB/S3/CF)
│   ├── 05-fix-cognito.sh      # Enable self-signup + email verification
│   ├── 06-deploy-agentcore.sh # Deploy AgentCore (Gateway/Target/Runtime/Memory)
│   ├── 07-seed-skills.sh      # Seed built-in skills into DynamoDB
│   ├── setup-agentcore.py     # (called by step 6) Creates Gateway, Target, Runtime
│   ├── seed-skills.py         # (called by step 7) Writes SKILL.md to DynamoDB
│   └── teardown-agentcore.py  # Destroys AgentCore resources
├── docs/                    # Architecture & design documentation
└── deploy.sh                # One-click wrapper that runs scripts/0[1-7]-*.sh
```

## Prerequisites

| Requirement | Version | Purpose |
|-------------|---------|---------|
| Node.js | >= 18.x | Build React apps, run CDK |
| npm | >= 9.x | Package management |
| Python 3 | >= 3.12 | AgentCore setup script, agent code |
| boto3 | latest | AgentCore API calls in setup script |
| agentcore CLI | latest | Deploy AgentCore resources (`pip install strands-agents-builder`) |
| AWS CLI | >= 2.x | AWS credentials |
| AWS Account | — | With Bedrock AgentCore and Kimi-2.5 model access |

**Important:** Request access to **Kimi K2.5** (`moonshotai.kimi-k2.5`) in [Bedrock Console > Model Access](https://console.aws.amazon.com/bedrock/home#/modelaccess) before deploying.

### Deployer IAM Permissions

The IAM user/role running `deploy.sh` needs the following AWS service permissions:

| AWS Service | Actions | Purpose |
|-------------|---------|---------|
| **CloudFormation** | `CreateStack`, `UpdateStack`, `DeleteStack`, `DescribeStacks`, `DescribeStackEvents`, `CreateChangeSet`, `DescribeChangeSet`, `ExecuteChangeSet`, `GetTemplate`, `ListStacks` | CDK and agentcore CLI deployment |
| **S3** | `CreateBucket`, `DeleteBucket`, `PutObject`, `GetObject`, `DeleteObject`, `ListBucket`, `PutBucketPolicy`, `PutBucketCors`, `PutBucketVersioning`, `GetBucketLocation` | CDK asset bucket, static site buckets, skill files bucket, config.js writes |
| **CloudFront** | `CreateDistribution`, `GetDistribution`, `UpdateDistribution`, `DeleteDistribution`, `CreateInvalidation` | Three frontend CDN distributions |
| **Lambda** | `CreateFunction`, `GetFunction`, `GetFunctionConfiguration`, `UpdateFunctionConfiguration`, `UpdateFunctionCode`, `AddPermission`, `RemovePermission`, `DeleteFunction` | 5 Lambda functions (iot-control, iot-discovery, admin-api, kb-query, user-init) |
| **DynamoDB** | `CreateTable`, `DeleteTable`, `DescribeTable`, `PutItem`, `Query`, `Scan` | Skills table creation + seed-skills.py initial data |
| **Bedrock** | `CreateKnowledgeBase`, `DeleteKnowledgeBase`, `GetKnowledgeBase`, `CreateDataSource`, `StartIngestionJob`, `GetIngestionJob`, `Retrieve` | Enterprise knowledge base creation, document sync, and retrieval |
| **OpenSearch Serverless** | `CreateCollection`, `DeleteCollection`, `CreateSecurityPolicy`, `CreateAccessPolicy`, `UpdateAccessPolicy`, `BatchGetCollection`, `APIAccessAll` | KB vector store (AOSS collection + index) |
| **Cognito** | `CreateUserPool`, `UpdateUserPool`, `DeleteUserPool`, `CreateUserPoolClient`, `CreateUserPoolDomain`, `AdminCreateUser`, `AdminSetUserPassword`, `CreateGroup`, `AdminAddUserToGroup` | User pool, admin user, admin group |
| **Cognito Identity** | `CreateIdentityPool`, `SetIdentityPoolRoles`, `DeleteIdentityPool` | Device simulator MQTT auth |
| **IoT Core** | `DescribeEndpoint`, `CreateThing`, `DeleteThing` | IoT endpoint discovery + device Thing creation |
| **IAM** | `CreateRole`, `DeleteRole`, `GetRole`, `PutRolePolicy`, `DeleteRolePolicy`, `AttachRolePolicy`, `DetachRolePolicy`, `PassRole`, `CreateServiceLinkedRole` | Lambda execution roles, Cognito roles, Gateway roles |
| **Bedrock AgentCore** | `Create/Get/Update/Delete` Gateway, AgentRuntime, PolicyEngine, Policy, Memory, Evaluator, OnlineEval; `ListGatewayTargets`, `GetGatewayTarget`, `ListPolicies`, `ListPolicyEngines` | Gateway, Runtime, policy engine, Memory, Evaluator lifecycle |
| **CloudWatch Logs** | `CreateLogGroup`, `PutRetentionPolicy`, `DeleteLogGroup` | Lambda log groups |
| **STS** | `GetCallerIdentity` | Setup scripts to get account ID |

<details>
<summary>Minimal IAM Policy JSON (click to expand)</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudFormation",
      "Effect": "Allow",
      "Action": [
        "cloudformation:CreateStack", "cloudformation:UpdateStack", "cloudformation:DeleteStack",
        "cloudformation:DescribeStacks", "cloudformation:DescribeStackResources",
        "cloudformation:DescribeStackEvents", "cloudformation:GetTemplate",
        "cloudformation:ListStacks", "cloudformation:CreateChangeSet",
        "cloudformation:DescribeChangeSet", "cloudformation:ExecuteChangeSet"
      ],
      "Resource": [
        "arn:aws:cloudformation:*:*:stack/SmartHomeAssistantStack/*",
        "arn:aws:cloudformation:*:*:stack/AgentCore-*/*",
        "arn:aws:cloudformation:*:*:stack/CDKToolkit/*"
      ]
    },
    {
      "Sid": "S3",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket", "s3:DeleteBucket", "s3:GetBucketLocation",
        "s3:PutBucketPolicy", "s3:GetBucketPolicy", "s3:PutBucketVersioning",
        "s3:PutBucketCors", "s3:PutObject", "s3:GetObject", "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::smarthome-*", "arn:aws:s3:::smarthome-*/*",
        "arn:aws:s3:::cdk-*-assets-*", "arn:aws:s3:::cdk-*-assets-*/*"
      ]
    },
    {
      "Sid": "CloudFront",
      "Effect": "Allow",
      "Action": [
        "cloudfront:CreateDistribution", "cloudfront:GetDistribution",
        "cloudfront:GetDistributionConfig", "cloudfront:UpdateDistribution",
        "cloudfront:DeleteDistribution", "cloudfront:CreateInvalidation",
        "cloudfront:CreateOriginAccessControl",
        "cloudfront:CreateCloudFrontOriginAccessIdentity",
        "cloudfront:GetCloudFrontOriginAccessIdentity",
        "cloudfront:DeleteCloudFrontOriginAccessIdentity"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Lambda",
      "Effect": "Allow",
      "Action": [
        "lambda:CreateFunction", "lambda:GetFunction",
        "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionConfiguration",
        "lambda:UpdateFunctionCode", "lambda:AddPermission",
        "lambda:RemovePermission", "lambda:DeleteFunction",
        "lambda:InvokeFunction"
      ],
      "Resource": "arn:aws:lambda:*:*:function:smarthome-*"
    },
    {
      "Sid": "LambdaCDKCustomResource",
      "Effect": "Allow",
      "Action": [
        "lambda:CreateFunction", "lambda:GetFunction",
        "lambda:UpdateFunctionConfiguration", "lambda:UpdateFunctionCode",
        "lambda:DeleteFunction", "lambda:AddPermission",
        "lambda:RemovePermission", "lambda:InvokeFunction"
      ],
      "Resource": "arn:aws:lambda:*:*:function:SmartHomeAssistantStack-*"
    },
    {
      "Sid": "DynamoDB",
      "Effect": "Allow",
      "Action": [
        "dynamodb:CreateTable", "dynamodb:DeleteTable", "dynamodb:DescribeTable",
        "dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"
      ],
      "Resource": "arn:aws:dynamodb:*:*:table/smarthome-skills"
    },
    {
      "Sid": "Cognito",
      "Effect": "Allow",
      "Action": [
        "cognito-idp:CreateUserPool", "cognito-idp:UpdateUserPool",
        "cognito-idp:DeleteUserPool", "cognito-idp:DescribeUserPool",
        "cognito-idp:CreateUserPoolClient", "cognito-idp:DeleteUserPoolClient",
        "cognito-idp:CreateUserPoolDomain", "cognito-idp:DeleteUserPoolDomain",
        "cognito-idp:AdminCreateUser", "cognito-idp:AdminSetUserPassword",
        "cognito-idp:AdminDeleteUser", "cognito-idp:AdminAddUserToGroup",
        "cognito-idp:CreateGroup",
        "cognito-idp:ListUsers", "cognito-idp:AdminListGroupsForUser"
      ],
      "Resource": "arn:aws:cognito-idp:*:*:userpool/*"
    },
    {
      "Sid": "CognitoIdentity",
      "Effect": "Allow",
      "Action": [
        "cognito-identity:CreateIdentityPool", "cognito-identity:DeleteIdentityPool",
        "cognito-identity:SetIdentityPoolRoles",
        "cognito-identity:DescribeIdentityPool",
        "cognito-identity:UpdateIdentityPool"
      ],
      "Resource": "*"
    },
    {
      "Sid": "IoTCore",
      "Effect": "Allow",
      "Action": [
        "iot:DescribeEndpoint", "iot:CreateThing", "iot:DeleteThing", "iot:DescribeThing"
      ],
      "Resource": "*"
    },
    {
      "Sid": "IAM",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:ListRoles",
        "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRolePolicy",
        "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies",
        "iam:PassRole", "iam:CreateServiceLinkedRole", "iam:TagRole"
      ],
      "Resource": "*"
    },
    {
      "Sid": "BedrockAgentCore",
      "Effect": "Allow",
      "Action": "bedrock-agentcore:*",
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup", "logs:DeleteLogGroup",
        "logs:PutRetentionPolicy", "logs:DescribeLogGroups"
      ],
      "Resource": "arn:aws:logs:*:*:log-group:/aws/lambda/smarthome-*"
    },
    {
      "Sid": "CloudWatchLogsCDK",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup", "logs:DeleteLogGroup",
        "logs:PutRetentionPolicy", "logs:DescribeLogGroups"
      ],
      "Resource": "arn:aws:logs:*:*:log-group:/aws/lambda/SmartHomeAssistantStack-*"
    },
    {
      "Sid": "STS",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    },
    {
      "Sid": "APIGateway",
      "Effect": "Allow",
      "Action": [
        "apigateway:POST", "apigateway:GET", "apigateway:PUT",
        "apigateway:DELETE", "apigateway:PATCH"
      ],
      "Resource": "arn:aws:apigateway:*::/*"
    },
    {
      "Sid": "SSM",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:*:*:parameter/cdk-bootstrap/*"
    },
    {
      "Sid": "ECR",
      "Effect": "Allow",
      "Action": [
        "ecr:CreateRepository", "ecr:DescribeRepositories",
        "ecr:SetRepositoryPolicy", "ecr:GetRepositoryPolicy"
      ],
      "Resource": "arn:aws:ecr:*:*:repository/cdk-*"
    }
  ]
}
```

</details>

---

## Quick Start

```bash
# 1. Configure AWS credentials
aws configure

# 2. Set up Python environment (for setup script + agent deps)
python3 -m venv venv
source venv/bin/activate
pip install strands-agents strands-agents-builder bedrock-agentcore boto3 mcp pyyaml

# 3. Deploy everything
./deploy.sh
```

`deploy.sh` is a thin wrapper that runs 7 split scripts under `scripts/0[1-7]-*.sh`, in order. Each split script can be run independently and **prints exactly which AWS resources it creates** at the top — handy for debugging or re-running a single step.

| Step | Script | What it deploys |
|------|--------|-----------------|
| 1 | `scripts/01-install-deps.sh` | `npm install` in `cdk/`; bundle the latest `boto3` into the `admin-api`, `user-init`, and `kb-query` Lambda directories (Lambda's built-in boto3 is too old for AgentCore control-plane APIs). |
| 2 | `scripts/02-build-frontends.sh` | Build the `device-simulator`, `chatbot`, and `admin-console` React apps into their `build/` directories. |
| 3 | `scripts/03-cdk-bootstrap.sh` | Run `cdk bootstrap` (idempotent) — provisions the `CDKToolkit` asset bucket, ECR repo, and deploy roles. |
| 4 | `scripts/04-cdk-deploy.sh` | Deploy the **CDK stack** (`SmartHomeAssistantStack`): Cognito User Pool / Identity Pool / admin group + default admin user; IoT Core Things + endpoint; 5 Lambdas (iot-control, iot-discovery, admin-api, kb-query, user-init); DynamoDB `smarthome-skills`; S3 buckets `smarthome-skill-files` and `smarthome-kb-docs`; OpenSearch Serverless collection; Bedrock Knowledge Base + S3 data source; API Gateway with Cognito authorizer; S3 + CloudFront for all three frontends. Writes `cdk-outputs.json`. |
| 5 | `scripts/05-fix-cognito.sh` | Call `aws cognito-idp update-user-pool` directly to guarantee self-service sign-up + email auto-verification are enabled (CDK flags for these don't always propagate reliably). |
| 6 | `scripts/06-deploy-agentcore.sh` | Deploy the **AgentCore stack** (managed by the `agentcore` CLI): Gateway with CUSTOM_JWT auth, Lambda Targets for iot-control / iot-discovery / kb-query, Runtime (Strands agent CodeZip), Memory (semantic / summary / preference strategies), Policy Engine + per-tool Cedar permit policies; initialize the enterprise KB (AOSS index + Bedrock KB data source); patch the Runtime with `SKILLS_TABLE_NAME` and `requestHeaderAllowlist: ["Authorization"]`. |
| 7 | `scripts/07-seed-skills.sh` | Read `agent/skills/*/SKILL.md` and write them to DynamoDB as `__global__` skills (idempotent — uses PutItem). |

After deployment, `deploy.sh` prints URLs for all three frontends and admin credentials.

---

## How It Works

The deployment creates two separate CloudFormation stacks:

**CDK Stack** (`SmartHomeAssistantStack`) — standard AWS resources:
- Cognito User Pool + Identity Pool + Admin group and default admin user
- IoT Core things + endpoint lookup
- Lambda functions: iot-control (MQTT), iot-discovery (device list), admin-api (skills, models, tool access, knowledge base, memories, sessions), kb-query (knowledge base retrieval), user-init (auto-provision tool permissions for new users)
- DynamoDB table (smarthome-skills) for agent skill storage, user settings, KB configuration, and session tracking
- S3 bucket (smarthome-skill-files) for skill directory files (scripts, references, assets) per the [Agent Skills spec](https://agentskills.io/specification)
- S3 bucket (smarthome-kb-docs) for enterprise knowledge base documents, organized by user scope (S3 prefix)
- OpenSearch Serverless collection (smarthome-kb) for knowledge base vector indexing
- Bedrock Knowledge Base + S3 data source for document vectorization and semantic retrieval (Embedding: `cohere.embed-multilingual-v3`)
- API Gateway with Cognito authorizer for admin API
- S3 + CloudFront for Device Simulator, Chatbot, and Admin Console

**AgentCore Stack** (managed by `agentcore` CLI) — AgentCore resources:
- AgentCore Gateway (MCP server) with CUSTOM_JWT auth (Cognito) for per-user tool policy enforcement
- Gateway Lambda Targets pointing to iot-control, iot-discovery, and kb-query Lambdas
- AgentCore Runtime running the Strands agent (CodeZip, Python 3.13)
- AgentCore Memory with semantic, summary, and user preference extraction strategies

The setup script (`scripts/setup-agentcore.py`) bridges them: reads CDK outputs, creates an `agentcore` project, injects our agent code, adds memory + gateway + targets (including kb-query), deploys everything, then initializes the enterprise knowledge base (creates AOSS vector index, Bedrock KB, S3 data source), patches the runtime with `SKILLS_TABLE_NAME`, `requestHeaderAllowlist: ["Authorization"]` (for JWT forwarding to gateway), and grants DynamoDB + Bedrock Retrieve access. Finally, `scripts/seed-skills.py` populates the DynamoDB table with the 5 built-in skills.

---

## Admin Console Features

The Admin Console ("Agent Harness Management") is a separate React app for administrators. Log in with a user in the `admin` Cognito group (default admin credentials are shown in deploy output).

### Skills (Skills Tab)
- **Full [Agent Skills spec](https://agentskills.io/specification) support**: all frontmatter fields (name, description, allowed-tools, license, compatibility, metadata) are editable per skill
- **Global skills** (`__global__`) are shared across all users; **per-user skills** override global skills with the same name
- Create, edit, and delete skills with a markdown instruction editor
- **Metadata editor**: dynamic key-value pairs for custom skill metadata
- **Skill file manager**: upload, download, and delete files in `scripts/`, `references/`, and `assets/` directories per skill (stored in S3, managed via presigned URLs)
- Skills are stored in DynamoDB (metadata + instructions) and S3 (directory files), loaded dynamically per invocation — no agent redeployment needed

### Models (Models Tab)
- **Global default model**: set the LLM model for all users via a dropdown
- **Per-user model override**: table listing all Cognito users with individual model selection dropdowns. Per-user settings take priority over the global default.
- Available models include Kimi K2.5, Claude 4.5/4.6, DeepSeek, Qwen, Llama 4, and OpenAI GPT
- The agent reads the model setting from DynamoDB on each invocation

### Tool Access (Tool Access Tab)
- **Per-user tool permissions**: list all Cognito users, select which gateway tools each user can invoke
- **Policy Engine mode toggle**: switch between ENFORCE (policies block unauthorized access) and LOG_ONLY (audit only)
- **AgentCore Policy Engine integration**: permissions enforced via Cedar policies at the gateway level
  - One Cedar `permit` policy per tool, with `principal.id` matching the user's Cognito `sub`
  - Gateway uses CUSTOM_JWT auth; runtime forwards user JWT via `requestHeaderAllowlist: ["Authorization"]`
  - Default deny: users without explicit tool permissions cannot invoke gateway tools
- **Demo Links column**: each user row has **Open Chatbot** and **Open Devices** buttons that launch the chatbot (with `?username=<email>` so the login form is prefilled) and the device simulator in new tabs, so administrators can run user-specific demos without copying URLs or emails

### Knowledge Base (Knowledge Base Tab)
- **RAG (Retrieval-Augmented Generation)** powered by AWS Bedrock Knowledge Base
- **Per-user document isolation**: S3 prefix (`__shared__/` public + `user@email/` private) + metadata filtering at query time
- **Document management**: upload, list, and delete documents (PDF, TXT, MD, DOCX, CSV, etc.)
- **Sync management**: one-click Bedrock KB vectorization ingestion with real-time status tracking
- **User scope selector**: dropdown showing `Shared (all users)` + all Cognito users
- **Security**: a local tool wrapper in the agent code auto-injects `user_id` from the Runtime-verified identity — the LLM cannot fabricate or override it; Gateway Cedar policy ensures only authenticated users can invoke the tool
- **Vector store**: OpenSearch Serverless (AOSS) + `cohere.embed-multilingual-v3` (1024 dimensions, multilingual Chinese/English)

### Integrations (Integrations Tab)
- Shows current tool integration types (Lambda Targets — active) and future roadmap
- Planned integrations: MCP Servers, A2A Agents, API Gateway endpoints

### Sessions (Sessions Tab)
- View all user runtime sessions (User ID, Session ID, Last Active)
- Each user gets a **fixed session ID** derived from their Cognito identity
- **Stop** button to terminate a user's runtime session via the AgentCore StopRuntimeSession API

### Memories (Memories Tab)
- **View long-term memory** for each user from AgentCore Memory
- Lists all memory actors (users who have interacted with the chatbot)
- Click "View Memories" to see extracted **facts** (semantic knowledge) and **preferences** (user preferences)
- Records sorted by creation time, showing type badge, content, and timestamp

### Quality Evaluation (Quality Evaluation Tab)
- Links to **AgentCore Evaluator** console (LLM-as-a-Judge quality evaluation)
- Links to **Bedrock Guardrails** console (content filtering, PII redaction)
- Quick link to **Cedar Policy Engine** settings in the Tool Access tab

---

## Admin Console Usage Guide

Log in to the Admin Console with the admin credentials shown in the deploy output. The user must belong to the Cognito `admin` group.

### Managing Skills

**Create a skill:**
1. Go to the **Skills** tab, select `__global__` (shared) or a specific user from the "User Scope" dropdown
2. Click **Create Skill**, fill in the skill name (lowercase, digits, hyphens, e.g. `my-skill`), description (required), and Markdown instructions
3. Optionally fill in allowed tools, license, compatibility, and metadata key-value pairs
4. Click **Create Skill** to submit

**Edit / Delete a skill:**
- Click **Edit** in the skill list to modify description, instructions, etc. (name and user scope cannot be changed)
- Click **Delete** and confirm to remove a skill

**Manage skill files:**
- When editing a skill, a file manager appears below the form with `scripts/`, `references/`, and `assets/` directories
- Click **Upload to scripts/** etc. to upload files, **Download** to download, **Delete** to remove

### Configuring Models

**Set the global default model:**
1. Go to the **Models** tab, select a model from the "Global Default Model" dropdown
2. Click **Save** (takes effect on the agent's next invocation)

**Override model per user:**
- In the user table below, select a different model for a specific user and click **Save** on that row
- Select "Use global default" to clear the user override

### Managing Tool Permissions

**Configure policy engine mode:**
1. Go to the **Tool Access** tab, toggle between **ENFORCE** (block unauthorized access) and **LOG_ONLY** (audit only) at the top

**Assign tool permissions to a user:**
1. Click **Manage Permissions** for the target user in the user list
2. Check/uncheck the Gateway tools the user can invoke (use **Select All** / **Deselect All** for bulk operations)
3. Click **Save Permissions** (Cedar policies take effect immediately)

**Demo links per user:**
- Each row in the user list has a **Demo** column with **Open Chatbot** and **Open Devices** buttons
- The chatbot link includes `?username=<email>` so the login form is pre-filled — the admin only needs to enter the password
- Both links open in new tabs, convenient to run side-by-side with the admin console during a live demo

### Monitoring Sessions

1. Go to the **Sessions** tab to view all active runtime sessions
2. Click **Stop** to terminate a specific user's session
3. Click **Refresh** to reload the list

### Viewing User Memories

1. Go to the **Memories** tab — the list shows all users who have interacted with the chatbot
2. Click **View Memories** to see a user's long-term memories (facts and preferences)

### Managing the Enterprise Knowledge Base

**Upload documents:**
1. Go to the **Knowledge Base** tab, select `Shared (all users)` (visible to everyone) or a specific user email (visible only to that user) from the "User Scope" dropdown
2. Click **Upload Document** and select a file (PDF, TXT, MD, DOCX, CSV, etc.)
3. The system automatically creates a metadata sidecar file to tag the document's scope

**Sync the knowledge base:**
1. After uploading or deleting documents, click **Sync Knowledge Base** to trigger Bedrock KB vectorization
2. The sync status table shows ingestion job progress in real time (STARTING → IN_PROGRESS → COMPLETE)
3. Once synced, the agent can retrieve the new documents via the `query_knowledge_base` tool

**Permission model:**
- **Shared documents** (`__shared__/` prefix): retrievable by all users through agent chat
- **User-scoped documents** (`user@email/` prefix): retrievable only by the corresponding user
- A local tool wrapper in the agent code injects `user_id` from the verified runtime context — the LLM cannot control this parameter

### Quality Evaluation

- Go to the **Quality Evaluation** tab, click **Open Console** to jump to the AgentCore Evaluator or Bedrock Guardrails AWS console
- Click **Go to Tool Access** to quickly navigate to tool permission settings

---

## Step-by-Step Deployment

Want to know exactly what each step does, or re-run just one step after a mid-deploy failure? Call the individual scripts under `scripts/` — each is standalone and idempotent, and prints the AWS resources it creates at the top. Recommended order:

```bash
source venv/bin/activate          # so python/pip use the venv

scripts/01-install-deps.sh        # npm install in cdk/ + bundle boto3 into Lambdas
scripts/02-build-frontends.sh     # produce static bundles for the 3 React apps
scripts/03-cdk-bootstrap.sh       # cdk bootstrap (once per account/region)
scripts/04-cdk-deploy.sh          # deploy SmartHomeAssistantStack, write cdk-outputs.json
scripts/05-fix-cognito.sh         # enable Cognito self-signup + email verification
scripts/06-deploy-agentcore.sh    # Gateway + Target + Runtime + Memory + KB init
scripts/07-seed-skills.sh         # write agent/skills/*/SKILL.md to DynamoDB
```

See the per-step resource table above under Quick Start for what each script deploys. Common partial re-runs: changed only frontend code → rerun 2 + 4; changed only agent Python code → rerun 6; changed only built-in skill files → rerun 7.

### Add admin users (optional)

The CDK stack creates a default admin user. To add more:

```bash
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <USER_POOL_ID> \
  --username <EMAIL> \
  --group-name admin
```

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

### Strands Agent

```bash
source venv/bin/activate
export AWS_REGION=us-west-2
export MODEL_ID=moonshotai.kimi-k2.5  # or any Bedrock model you have access to
cd agent && python agent.py  # starts server on http://localhost:8080
```

Test endpoints:
```bash
curl http://localhost:8080/ping
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Turn on the LED matrix to rainbow mode"}'
```

---

## Configuration

### Changing the AI Model

Edit `agent/agent.py` or set the `MODEL_ID` environment variable. Default: `moonshotai.kimi-k2.5`. Administrators can override the model per user via the Admin Console without redeploying.

### Custom Domain Names

Add to `cdk/lib/smarthome-stack.ts`:
```typescript
domainNames: ["chat.yourdomain.com"],
certificate: acm.Certificate.fromCertificateArn(this, "Cert", "arn:aws:acm:..."),
```

---

## Monthly Cost Estimation

This solution uses **100% AWS Serverless managed services** — no servers to provision, pay only for actual usage, costs approach zero when idle. Estimates below are for DAU (Daily Active Users) at 10K, 100K, and 1M tiers (us-west-2 region, pricing as of 2025).

### Assumptions

- Each user averages 10 conversations/day, each with 1 LLM call + 1.5 tool calls + 0.3 KB queries
- LLM model: Kimi K2.5 (~800 input tokens/call, ~200 output tokens/call)
- Knowledge base: 1,000 documents (~500MB total), synced 4 times/month
- Admin operations are negligible

### Cost Breakdown

| Module | Service | Billing Dimension | 10K DAU | 100K DAU | 1M DAU |
|--------|---------|-------------------|---------|----------|--------|
| **AI Agent** | AgentCore Runtime | Invocations + duration | ~$150 | ~$1,500 | ~$15,000 |
| **LLM Inference** | Bedrock (Kimi K2.5) | Input/Output tokens | ~$80 | ~$800 | ~$8,000 |
| **Tool Routing** | AgentCore Gateway | MCP invocations | ~$15 | ~$150 | ~$1,500 |
| **Policy Engine** | AgentCore Policy Engine | Policy evaluations | ~$5 | ~$50 | ~$500 |
| **Long-term Memory** | AgentCore Memory | Read/Write + storage | ~$20 | ~$200 | ~$2,000 |
| **KB Retrieval** | Bedrock KB (Retrieve) | Query count | ~$10 | ~$100 | ~$1,000 |
| **Vector Embedding** | Bedrock (Cohere Embed) | Embedding tokens | ~$2 | ~$2 | ~$2 |
| **Vector Store** | OpenSearch Serverless | OCU hours (min 2 OCU) | ~$350 | ~$350 | ~$700 |
| **Document Storage** | S3 | Storage + requests | <$1 | <$1 | ~$5 |
| **Device Control** | Lambda (iot-control) | Invocations | ~$3 | ~$30 | ~$300 |
| **Admin API** | API Gateway + Lambda | Invocations | <$1 | ~$5 | ~$50 |
| **Authentication** | Cognito | MAU (first 50K free) | $0 | ~$250 | ~$4,500 |
| **Frontend Hosting** | S3 + CloudFront | Storage + transfer | ~$5 | ~$20 | ~$100 |
| **Data Storage** | DynamoDB | Read/Write + storage (on-demand) | ~$5 | ~$50 | ~$500 |
| **Quality Evaluation** | AgentCore Evaluator | LLM-as-Judge calls | ~$10 | ~$100 | ~$1,000 |
| | | **Monthly Total** | **~$656** | **~$3,607** | **~$35,157** |
| | | **Per User / Month** | **~$0.066** | **~$0.036** | **~$0.035** |

### Serverless Cost Advantages

- **Zero idle cost**: Lambda, API Gateway, DynamoDB, and AgentCore Runtime incur no charges when idle (except OpenSearch Serverless minimum 2 OCU)
- **Linear scaling**: Core costs (Agent + LLM + Gateway) scale linearly from 10K to 1M users with no step-function jumps
- **Zero ops overhead**: No servers, clusters, or capacity planning — AWS handles all auto-scaling
- **Decreasing per-user cost**: Per-user monthly cost drops from $0.066 (10K) to $0.035 (1M), demonstrating economies of scale
- **OpenSearch Serverless** is the largest fixed cost (~$350/month minimum) — suitable for medium to large deployments; smaller deployments can consider switching to Bedrock managed vector store (in preview) to further reduce costs

> **Note:** These are estimates. Actual costs depend on conversation patterns, message length, model choice, and regional pricing. Use the [AWS Pricing Calculator](https://calculator.aws/) for precise calculations. AgentCore and Bedrock KB pricing may change as services evolve.

---

## Teardown

**Order matters:** AgentCore resources must be destroyed before the CDK stack (Cognito, Lambda, etc.) because the AgentCore Gateway references the Lambda function.

```bash
source venv/bin/activate

# 1. Tear down AgentCore resources first (Gateway, Target, Runtime)
python3 scripts/teardown-agentcore.py

# 2. Then destroy CDK stack (Cognito, IoT, Lambda, DynamoDB, S3, CloudFront)
cd cdk && npx cdk destroy --all --force
```

The teardown script only deletes resources tracked in `agentcore-state.json` — it never touches unrelated AgentCore resources.

---

## Documentation

See [docs/architecture-and-design.md](docs/architecture-and-design.md) for architecture diagrams, component design, API reference, and MQTT command schemas.

---

## Troubleshooting

### agentcore CLI not found

```bash
pip install strands-agents-builder
```

### agentcore deploy fails: "Target not found in aws-targets.json"

The setup script seeds this file automatically. If running manually, create the file:
```json
[{"name": "default", "region": "us-west-2", "account": "YOUR_ACCOUNT_ID"}]
```

### CDK synth fails: "pyproject.toml not found"

The agent directory must contain `pyproject.toml`. This is included in the repo.

### Bedrock Model Access Denied

Go to [Bedrock Console > Model Access](https://console.aws.amazon.com/bedrock/home#/modelaccess) and request access to Kimi K2.5 (`moonshotai.kimi-k2.5`). Note: the model ID is `moonshotai.kimi-k2.5`, not `us.kimi.kimi-2.5`.

### MQTT Connection Fails in Device Simulator

Check: Cognito Identity Pool ID, IoT endpoint, and IAM role permissions in browser console.

### Chatbot Request Fails

The chatbot uses HTTP POST (not WebSocket) to `https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{arn}/invocations`. Check: `agentRuntimeArn` in config.js, Cognito tokens, AgentCore Runtime logs in CloudWatch.

### Admin Console: "Access Denied"

The logged-in user must belong to the `admin` Cognito group. Add them with:
```bash
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <USER_POOL_ID> \
  --username <EMAIL> \
  --group-name admin
```

### Admin API returns 403 "Forbidden: admin group required"

Same as above — the JWT `cognito:groups` claim must include `admin`.

### Skills not loading from DynamoDB

Check that the AgentCore Runtime has the `SKILLS_TABLE_NAME` environment variable set and the runtime IAM role has `dynamodb:Query`, `dynamodb:PutItem` permission on the skills table. The setup script handles both, but re-running it may be needed after a fresh `agentcore deploy`.

### S3 Bucket Name Conflict

Remove `bucketName` from the CDK stack to let CDK auto-generate unique names.

### CDK custom resource fails: "@aws-sdk/client-bedrockagentcorecontrol does not exist"

This is expected — the CDK JS SDK does not yet include the AgentCore client. AgentCore resources are created by the `agentcore` CLI in a separate step (Step 8 of deploy), not by CDK.

---

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

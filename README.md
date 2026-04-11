# Smart Home Assistant Agent

AI-powered smart home control system built on AWS AgentCore Runtime/Memory/Gateway. Natural language chatbot controls simulated IoT devices (LED Matrix, Rice Cooker, Fan, Oven) through a Strands Agent hosted on AgentCore Runtime, with remote control command through AgentCore Gateway and real-time MQTT communication with AWS IoT Core. Includes an admin console for managing agent skills, model selection, and user sessions.

![chatbot](docs/screenshots/smarthomeassistant-chat.png)
![device simulator](docs/screenshots/smarthomeassistant-devices-v2.png)
![admin console](docs/screenshots/smarthomeassistant-admin.png)

```
smarthome-assistant-agent/
├── cdk/                     # AWS CDK — Cognito, IoT Core, Lambda, DynamoDB, API Gateway, S3, CloudFront
│   ├── lib/smarthome-stack.ts
│   └── lambda/
│       ├── iot-control/     # Validates & publishes MQTT commands
│       ├── iot-discovery/   # Returns available device list
│       └── admin-api/       # Skill CRUD, file management, user settings, sessions
├── device-simulator/        # React app — 4 simulated IoT devices
├── chatbot/                 # React app — chat UI with Cognito auth
├── admin-console/           # React app — admin user management UI
├── agent/                   # Strands Agent (deployed to AgentCore Runtime)
│   ├── agent.py             # BedrockAgentCoreApp entrypoint
│   ├── skills/              # Fallback device control SKILL.md files
│   └── pyproject.toml       # Dependencies for AgentCore code packaging
├── scripts/
│   ├── setup-agentcore.py   # Creates Gateway, Lambda Target, Runtime
│   ├── seed-skills.py       # Seeds SKILL.md files to DynamoDB
│   └── teardown-agentcore.py
├── docs/                    # Architecture & design documentation
└── deploy.sh                # One-click deploy
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

The deploy script runs 8 steps:
1. Install CDK dependencies
2. Build Device Simulator (React)
3. Build Chatbot (React)
4. Build Admin Console (React)
5. CDK bootstrap
6. **CDK deploy** — Cognito, IoT Core, Lambda, DynamoDB, API Gateway, S3 + CloudFront
7. **Fix Cognito** — Enable self-service sign-up + email auto-verification
8. **AgentCore setup** — Gateway + Lambda Target + Agent Runtime + Seed skills to DynamoDB

After deployment, the script outputs URLs for all three frontends and admin credentials.

---

## How It Works

The deployment creates two separate CloudFormation stacks:

**CDK Stack** (`SmartHomeAssistantStack`) — standard AWS resources:
- Cognito User Pool + Identity Pool + Admin group and default admin user
- IoT Core things + endpoint lookup
- Lambda functions: iot-control (MQTT), iot-discovery (device list), admin-api (skill CRUD + file management + settings + sessions)
- DynamoDB table (smarthome-skills) for agent skill storage, user settings, and session tracking
- S3 bucket (smarthome-skill-files) for skill directory files (scripts, references, assets) per the [Agent Skills spec](https://agentskills.io/specification)
- API Gateway with Cognito authorizer for admin API
- S3 + CloudFront for Device Simulator, Chatbot, and Admin Console

**AgentCore Stack** (managed by `agentcore` CLI) — AgentCore resources:
- AgentCore Gateway (MCP server) with NONE auth (internal to runtime)
- Gateway Lambda Target pointing to iot-control Lambda
- AgentCore Runtime running the Strands agent (CodeZip, Python 3.13)
- AgentCore Memory with semantic, summary, and user preference extraction strategies

The setup script (`scripts/setup-agentcore.py`) bridges them: reads CDK outputs, creates an `agentcore` project, injects our agent code, adds memory + gateway + target, deploys everything, then patches the runtime with `SKILLS_TABLE_NAME` and grants DynamoDB access. Finally, `scripts/seed-skills.py` populates the DynamoDB table with the 4 built-in skills.

---

## Admin Console Features

The Admin Console is a separate React app for administrators. Log in with a user in the `admin` Cognito group (default admin credentials are shown in deploy output).

### Skills Management (Skills Tab)
- **Full [Agent Skills spec](https://agentskills.io/specification) support**: all frontmatter fields (name, description, allowed-tools, license, compatibility, metadata) are editable per skill
- **Global skills** (`__global__`) are shared across all users
- **Per-user skills** override global skills with the same name for a specific user
- Create, edit, and delete skills with a markdown instruction editor
- **Metadata editor**: dynamic key-value pairs for custom skill metadata
- **Skill file manager**: upload, download, and delete files in `scripts/`, `references/`, and `assets/` directories per skill (stored in S3, managed via presigned URLs)
- Skills are stored in DynamoDB (metadata + instructions) and S3 (directory files), loaded dynamically per invocation — no agent redeployment needed

### Per-User Model Selection
- Set the LLM model per user or a global default via a dropdown
- Available models include Kimi K2.5, Claude 4.5/4.6, Llama 4, and OpenAI GPT
- The agent reads the model setting from DynamoDB on each invocation

### Session Management (Sessions Tab)
- View all user runtime sessions (User ID, Session ID, Last Active)
- Each user gets a **fixed session ID** derived from their Cognito identity (same user = same session across invocations)
- **Stop** button to terminate a user's runtime session via the AgentCore StopRuntimeSession API
- User ID is passed from the chatbot in the request payload and recorded by the agent

---

## Step-by-Step Deployment

### 1. Build frontends

```bash
cd device-simulator && npm install && npm run build && cd ..
cd chatbot && npm install && npm run build && cd ..
cd admin-console && npm install && npm run build && cd ..
```

### 2. Deploy CDK stack

```bash
cd cdk
npm install
npx cdk bootstrap  # once per account/region
npx cdk deploy --all --require-approval never --outputs-file ../cdk-outputs.json
```

### 3. Deploy AgentCore

```bash
source venv/bin/activate
python3 scripts/setup-agentcore.py
```

This creates an `agentcore` CLI project in `.agentcore-project/`, adds our agent code, gateway, and Lambda target, then runs `agentcore deploy -y --verbose`.

### 4. Seed skills

```bash
python3 scripts/seed-skills.py
```

Reads the 4 SKILL.md files from `agent/skills/` and writes them to DynamoDB as `__global__` skills.

### 5. Add admin users (optional)

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

## Teardown

**Order matters:** AgentCore resources must be destroyed before the CDK stack (Cognito, Lambda, etc.) because the AgentCore Gateway references the Lambda function.

```bash
source venv/bin/activate

# 1. Tear down AgentCore resources first (Gateway, Target, Runtime)
python3 scripts/teardown-agentcore.py

# 2. Then destroy CDK stack (Cognito, IoT, Lambda, DynamoDB, S3, CloudFront)
cd cdk && npx cdk destroy --all --force
```

The teardown script only deletes resources tracked in `agentcore-state.json` — it never touches unrelated AgentCore resources. It first deletes the AgentCore CloudFormation stack (`AgentCore-smarthome-default`), then cleans up any remaining resources by their specific IDs.

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

### User ID shows as "default" in Admin Console Sessions

The chatbot passes `userId` in the POST body to the agent. If it shows "default", the chatbot may be serving a cached old bundle. Hard-refresh (`Ctrl+Shift+R`) or clear the CloudFront cache.

### agentcore deploy fails: CDK synth "pyproject.toml not found"

The `agentcore` CLI requires `pyproject.toml` in the agent code directory to package it as CodeZip. This file is included in the repo under `agent/pyproject.toml`.

### Teardown fails: "Gateway has targets associated"

The AgentCore CloudFormation stack must be deleted before the gateway can be removed. The teardown script handles this automatically. If running manually, delete the `AgentCore-smarthome-default` stack first:
```bash
aws cloudformation delete-stack --stack-name AgentCore-smarthome-default
aws cloudformation wait stack-delete-complete --stack-name AgentCore-smarthome-default
```

### S3 Bucket Name Conflict

Remove `bucketName` from the CDK stack to let CDK auto-generate unique names.

### CDK custom resource fails: "@aws-sdk/client-bedrockagentcorecontrol does not exist"

This is expected — the CDK JS SDK does not yet include the AgentCore client. AgentCore resources are created by the `agentcore` CLI in a separate step (Step 8 of deploy), not by CDK.

---

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

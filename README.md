# Smart Home Assistant Agent

AI-powered smart home control system built on AWS. Natural language chatbot controls simulated IoT devices (LED Matrix, Rice Cooker, Fan, Oven) through a Strands Agent hosted on AgentCore Runtime, with remote control command through AgentCore Gateway and real-time MQTT communication with AWS IoT Core.

```
smarthome-assistant-agent/
├── cdk/                     # AWS CDK — Cognito, IoT Core, Lambda, S3, CloudFront
│   ├── lib/smarthome-stack.ts
│   └── lambda/iot-control/  # Validates & publishes MQTT commands
├── device-simulator/        # React app — 4 simulated IoT devices
├── chatbot/                 # React app — chat UI with Cognito auth
├── agent/                   # Strands Agent (deployed to AgentCore Runtime)
│   ├── agent.py             # BedrockAgentCoreApp entrypoint
│   ├── skills/              # Device control SKILL.md files
│   └── pyproject.toml       # Dependencies for AgentCore code packaging
├── scripts/
│   ├── setup-agentcore.py   # Creates Gateway, Lambda Target, Runtime
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
pip install strands-agents strands-agents-builder bedrock-agentcore boto3 mcp

# 3. Deploy everything
./deploy.sh
```

The deploy script runs 7 steps:
1. Install CDK dependencies
2. Build Device Simulator (React)
3. Build Chatbot (React)
4. CDK bootstrap
5. **CDK deploy** — Cognito, IoT Core, Lambda, S3 + CloudFront
6. **Fix Cognito** — Enable self-service sign-up + email auto-verification
7. **AgentCore setup** — Gateway + Lambda Target + Agent Runtime (via `agentcore` CLI)

---

## How It Works

The deployment creates two separate CloudFormation stacks:

**CDK Stack** (`SmartHomeAssistantStack`) — standard AWS resources:
- Cognito User Pool + Identity Pool
- IoT Core things + endpoint lookup
- Lambda function (iot-control) for MQTT publishing
- S3 + CloudFront for Device Simulator and Chatbot

**AgentCore Stack** (managed by `agentcore` CLI) — AgentCore resources:
- AgentCore Gateway (MCP server) with NONE auth (internal to runtime)
- Gateway Lambda Target pointing to iot-control Lambda
- AgentCore Runtime running the Strands agent (CodeZip, Python 3.13)
- AgentCore Memory with semantic, summary, and user preference extraction strategies

The setup script (`scripts/setup-agentcore.py`) bridges them: reads CDK outputs, creates the memory resource, creates an `agentcore` project, injects our agent code, adds the gateway + target, deploys, and patches the runtime with the memory ID.

---

## Step-by-Step Deployment

### 1. Build frontends

```bash
cd device-simulator && npm install && npm run build && cd ..
cd chatbot && npm install && npm run build && cd ..
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

Edit `agent/agent.py` or set the `MODEL_ID` environment variable. Default: `moonshotai.kimi-k2.5`. The setup script sets it in `agentcore.json` for the deployed runtime.

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

# 2. Then destroy CDK stack (Cognito, IoT, Lambda, S3, CloudFront)
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

This is expected — the CDK JS SDK does not yet include the AgentCore client. AgentCore resources are created by the `agentcore` CLI in a separate step (Step 6 of deploy), not by CDK.

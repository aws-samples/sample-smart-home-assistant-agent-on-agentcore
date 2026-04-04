# Smart Home Assistant Agent - Architecture & Design

## Table of Contents

- [1. System Overview](#1-system-overview)
- [2. High-Level Architecture](#2-high-level-architecture)
- [3. Data Flow Diagrams](#3-data-flow-diagrams)
- [4. Network Architecture](#4-network-architecture)
- [5. Security Architecture](#5-security-architecture)
- [6. Device Simulator Design](#6-device-simulator-design)
- [7. Chatbot Design](#7-chatbot-design)
- [8. AI Agent Design](#8-ai-agent-design)
- [9. Infrastructure Design](#9-infrastructure-design)
- [10. API Reference](#10-api-reference)
- [11. MQTT Topic & Command Reference](#11-mqtt-topic--command-reference)
- [12. Error Handling Strategy](#12-error-handling-strategy)
- [13. Frontend Build Pipeline](#13-frontend-build-pipeline)
- [14. Technology Choices and Rationale](#14-technology-choices-and-rationale)
- [15. Scalability Considerations](#15-scalability-considerations)

---

## 1. System Overview

The Smart Home Assistant Agent is a full-stack application that demonstrates AI-driven smart home device control on AWS. It consists of five main subsystems:

| Subsystem | Technology | Purpose |
|-----------|-----------|---------|
| Device Simulator | React + TypeScript + MQTT | Visual simulation of 4 smart home devices |
| Chatbot | React + TypeScript + HTTP POST | Natural-language interface to the AI agent |
| AI Agent | Strands Agent on AgentCore Runtime (Kimi-2.5) | Understands intent and orchestrates device commands |
| Tool Access | AgentCore Gateway (MCP Server) + Lambda | Command routing and device control via MCP |
| Infrastructure | AWS CDK (TypeScript) | One-click deployment of all resources |

---

## 2. High-Level Architecture

```
+-----------------------------------------------------------------------------------+
|                                  AWS Cloud                                        |
|                                                                                   |
|  +------------------+     +------------------+     +-------------------------+    |
|  |   CloudFront     |     |   CloudFront     |     |     Cognito             |    |
|  |  (Device Sim)    |     |   (Chatbot)      |     |  +--------+ +--------+ |    |
|  +--------+---------+     +--------+---------+     |  |  User  | |Identity| |    |
|           |                        |               |  |  Pool  | |  Pool  | |    |
|  +--------v---------+     +--------v---------+     |  +---+----+ +---+----+ |    |
|  |    S3 Bucket      |     |    S3 Bucket     |     +-----+----------+------+    |
|  +-------------------+     +------------------+           |          |           |
|                                    |                      |          |           |
|  +-----------+  MQTT/WSS          | HTTPS (Bearer JWT)   |          |           |
|  | Browser   +----------+        |                       |          |           |
|  | (Device   |          |  +-----v-----------------------v---+      |           |
|  |  Sim App) |          |  |  AgentCore Runtime               |      |           |
|  +-----------+          |  |  (Strands Agent + Kimi-2.5)      |      |           |
|             |           |  |  /invocations  /ws  /ping        |      |           |
|             |           |  +-----+----------------------------+      |           |
|  Cognito    |           |        |                                   |           |
|  Identity   |           |        | MCP Client                       |           |
|  Pool       |           |  +-----v----------------------------+      |           |
|  (SigV4)    |           |  |  AgentCore Gateway (MCP Server)  |      |           |
|             |           |  |  Auth: NONE (internal to runtime) |      |           |
|             v           |  +-----+----------------------------+      |           |
|  +------------------+   |        |                                   |           |
|  |  AWS IoT Core    |   |        | Lambda Target                    |           |
|  |  MQTT Broker     |<--+--------+                                   |           |
|  +------------------+   |  +-----v----------------------------+      |           |
|                         |  |  iot-control Lambda               |      |           |
|                         |  |  Validates & publishes MQTT       |      |           |
|                         +->+----------------------------------+      |           |
|                                                                      |           |
+----------------------------------------------------------------------+-----------+
```

### Component Interaction Matrix

```
                    Cognito   Cognito    IoT     AgentCore  AgentCore   IoT Control
                    UserPool  Identity   Core    Runtime    Gateway     Lambda
                              Pool
Device Simulator                 R        R/W
Chatbot App          R                            R/W
AgentCore Runtime    V                                       I (MCP)
AgentCore Gateway    V                                       (self)      I
IoT Control Lambda                        W

R = Read/Subscribe   W = Write/Publish   V = Validate JWT   I = Invoke
```

---

## 3. Data Flow Diagrams

### Flow 1: Device Command

```
User (Chatbot)
    |
    | "Turn on the LED matrix to rainbow mode"
    v
AgentCore Runtime (HTTP POST /invocations) --> Strands Agent (Kimi K2.5)
                                       |
                                       | MCP Client call
                                       v
                                AgentCore Gateway (MCP Server)
                                       |
                                       | Lambda Target
                                       v
                                iot-control Lambda
                                       |
                                       | iot-data:Publish
                                       v
                                AWS IoT Core
                                Topic: smarthome/led_matrix/command
                                       |
                                       | MQTT over WebSocket (SigV4)
                                       v
                                Device Simulator (Browser)
                                LedMatrix component receives:
                                {"action":"setMode","mode":"rainbow"}
```

### Flow 2: User Authentication

```
User (Browser)
    |
    | email + password
    v
Chatbot LoginPage
    |
    | amazon-cognito-identity-js
    v
Cognito User Pool
    |
    | returns: idToken, accessToken, refreshToken
    v
ChatInterface
    |
    | HTTP POST to AgentCore Runtime /invocations
    | Authorization: Bearer {idToken}
    v
AgentCore Runtime
    |
    | JWT validation (Cognito User Pool)
    v
Strands Agent processes request
```

### Flow 3: Device Simulator MQTT Connection

```
Device Simulator (Browser)
    |
    | Cognito Identity Pool (unauthenticated)
    v
AWS STS (AssumeRoleWithWebIdentity)
    |
    | Temporary AWS credentials
    v
MqttClient.ts
    |
    | MQTT5 over WebSocket with SigV4
    | ClientId: "device-sim-{random}"
    v
AWS IoT Core
    |
    | Subscribe to:
    |   smarthome/led_matrix/command
    |   smarthome/rice_cooker/command
    |   smarthome/fan/command
    |   smarthome/oven/command
    v
Each device component receives
commands and updates its UI state
```

---

## 4. Network Architecture

```
Internet
    |
    +---> CloudFront (Device Simulator) ---> S3 Bucket (static assets)
    |         |
    |         +---> /config.js (runtime config from S3)
    |
    +---> CloudFront (Chatbot) ---> S3 Bucket (static assets)
    |         |
    |         +---> /config.js (runtime config from S3)
    |
    +---> HTTPS: AgentCore Runtime
    |         https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encodedArn}/invocations
    |         |
    |         +---> Strands Agent (Kimi K2.5)
    |         +---> Auth: JWT Bearer token (Cognito User Pool)
    |         +---> CORS: access-control-allow-origin: *
    |
    +---> HTTPS: AgentCore Gateway (MCP Server, internal to agent)
    |         https://{gateway-id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp
    |         |
    |         +---> Lambda Target: iot-control Lambda
    |         +---> Auth: NONE (only called by the runtime internally)
    |
    +---> WSS: IoT Core (iot-endpoint.iot.region.amazonaws.com)
              |
              +---> MQTT5 over WebSocket (SigV4 auth via Cognito Identity Pool)
              +---> Topics: smarthome/{device_type}/command
```

---

## 5. Security Architecture

```
+------------------------------------------------------------------+
|                      Authentication Layers                        |
+------------------------------------------------------------------+
|                                                                   |
|  Layer 1: Cognito User Pool                                      |
|  +------------------------------------------------------------+  |
|  | - Email/password authentication                             |  |
|  | - Self-service sign-up with email verification              |  |
|  | - Issues JWT tokens (id, access, refresh)                   |  |
|  | - Used by: Chatbot app, AgentCore Runtime, AgentCore Gateway|  |
|  +------------------------------------------------------------+  |
|                                                                   |
|  Layer 2: Cognito Identity Pool                                  |
|  +------------------------------------------------------------+  |
|  | - Federation with User Pool                                 |  |
|  | - Issues temporary AWS credentials via STS                  |  |
|  | - Unauthenticated access allowed (for device sim)           |  |
|  | - Scoped IAM role: iot:Connect/Subscribe/Receive/Publish    |  |
|  | - Used by: Device simulator MQTT connection                 |  |
|  +------------------------------------------------------------+  |
|                                                                   |
|  Layer 3: AgentCore Runtime JWT Authorization                    |
|  +------------------------------------------------------------+  |
|  | - Validates JWT from Cognito User Pool                      |  |
|  | - Bearer token via Authorization header (HTTP POST)         |  |
|  | - Protects: /invocations, /ping endpoints                   |  |
|  +------------------------------------------------------------+  |
|                                                                   |
|  Layer 4: AgentCore Gateway (No Auth)                            |
|  +------------------------------------------------------------+  |
|  | - Auth: NONE (gateway is only called by the runtime)        |  |
|  | - Runtime already authenticates users at Layer 3            |  |
|  | - Gateway auth type cannot be changed after creation;       |  |
|  |   setting to NONE avoids runtime-to-gateway auth issues     |  |
|  +------------------------------------------------------------+  |
|                                                                   |
+------------------------------------------------------------------+
```

### IAM Permissions (Least Privilege)

| Principal | Permissions | Scope |
|-----------|------------|-------|
| Cognito Unauth Role | iot:Connect, Subscribe, Receive, Publish | `*` (IoT Core) |
| Cognito Auth Role | iot:Connect, Subscribe, Receive, Publish | `*` (IoT Core) |
| iot-control Lambda | iot:Publish | `arn:...:topic/smarthome/*` |
| AgentCore Runtime Role | bedrock:InvokeModel | Kimi-2.5 model |

---

## 6. Device Simulator Design

### 6.1 MQTT Connection Strategy

The device simulator runs entirely in the browser. It connects to AWS IoT Core using MQTT5 over WebSocket with SigV4 authentication, obtaining temporary credentials from a Cognito Identity Pool.

```
Browser
  |
  +-> fromCognitoIdentityPool() -> temporary AWS credentials
  |
  +-> new auth.StaticCredentialProvider({aws_access_id, aws_secret_key, aws_sts_token})
  |     (browser build of aws-iot-device-sdk-v2 uses StaticCredentialProvider; refreshed every 45min)
  |
  +-> Mqtt5Client (aws-iot-device-sdk-v2)
        |-> WebSocket SigV4 auth
        |-> clientId: "device-sim-{random}"
        |-> keepAlive: 30s
        |-> auto-reconnect with re-subscribe
```

**Note:** The aws-iot-device-sdk-v2 has different APIs for Node.js and browser bundles. The browser build exports `auth.StaticCredentialProvider` (not `auth.AwsCredentialsProvider.newStatic()`). We use `new auth.StaticCredentialProvider({aws_access_id, aws_secret_key, aws_sts_token})` with credentials fetched from `@aws-sdk/credential-providers`'s `fromCognitoIdentityPool()`, and refresh by reconnecting every 45 minutes. TypeScript uses `(auth as any).StaticCredentialProvider` since the Node.js type definitions don't include the browser API.

**Key design decisions:**
- **Singleton MQTT client**: All 4 device components share one `MqttClient` instance to avoid multiple WebSocket connections
- **Unauthenticated Cognito access**: The device simulator does not require user login - it uses the Cognito Identity Pool's unauthenticated role for simplicity
- **Topic convention**: `smarthome/{device_type}/command` where device_type is one of: `led_matrix`, `rice_cooker`, `fan`, `oven`
- **All devices off by default**: Every device starts in the powered-off state when the page loads
- **Auto-power-on**: Setting a mode, speed, temperature, or color via MQTT automatically powers on the device (e.g., `setMode` on LED Matrix also sets `power: true`), so the agent doesn't need to send a separate `setPower` command
- **Layout**: LED Matrix occupies the left column; Rice Cooker, Fan, and Oven stack compactly on the right

### 6.2 Device Components

Each device component follows the same pattern:

```typescript
// 1. Local state management
const [power, setPower] = useState(false);

// 2. MQTT subscription on mount
useEffect(() => {
  const mqtt = MqttClient.getInstance();
  const handler = (topic, payload) => {
    switch (payload.action) {
      case 'setPower': setPower(payload.power); break;
      // ...
    }
  };
  mqtt.subscribe('smarthome/{device}/command', handler);
  return () => mqtt.unsubscribe(topic, handler);
}, []);

// 3. Visual rendering based on state
return <div>...</div>;
```

#### LED Matrix (LedMatrix.tsx)

The LED matrix is a 16x16 grid (256 pixels) rendered as individual `<div>` elements. Each pixel has independent color and glow effects.

**Animation System:**
- Uses `requestAnimationFrame` for 60fps rendering
- Each frame computes 256 pixel colors based on the current mode and tick counter
- Color computation is mode-specific:

| Mode | Algorithm |
|------|-----------|
| Rainbow | HSL hue shifts based on position + time |
| Breathing | Radial sine wave with color fade |
| Chase | Diagonal trail with rotating hue |
| Sparkle | Random pixels at 8% probability + ambient background |
| Fire | Bottom-up heat gradient with perlin-like noise |
| Ocean | Dual sine waves in blue-cyan range |
| Aurora | Multi-sine curtain effect with vertical fade |
| Solid | Uniform color with brightness scaling |

**Performance considerations:**
- `useCallback` memoizes the frame generator to avoid re-creating closures
- Pixel glow (`box-shadow`) only applied when pixel is lit
- CSS `transition: background-color 0.15s` smooths color changes between frames

#### Rice Cooker (RiceCooker.tsx)

| State | Behavior |
|-------|----------|
| idle | Display shows "Ready", temperature cools to 25C |
| cooking | Timer counts down, temperature rises to target, steam animation plays |
| keep_warm | Maintains 65C after cooking completes |
| done | Shows "DONE" on display |

Cook times per mode: White Rice 20min, Brown Rice 30min, Porridge 15min, Steam 10min.

#### Fan (Fan.tsx)

CSS-animated spinning fan with 4 blades. Speed maps to animation duration:

| Speed | Label | CSS Duration |
|-------|-------|-------------|
| 0 | Off | stopped |
| 1 | Low | 3.0s |
| 2 | Medium | 1.5s |
| 3 | High | 0.6s |

Oscillation adds a secondary `fan-osc` animation (4s ease-in-out oscillation).

#### Oven (Oven.tsx)

Visual elements:
- Control panel with knobs and digital display
- Glass window with visible heating elements (top and bottom)
- Gradient glow that rises proportionally to temperature

Temperature simulation: heats at 5% of remaining delta per tick (500ms), with +/-1F fluctuation at target. Auto-transitions from Preheat to Bake when within 5F of target.

### 6.3 Runtime Configuration

Configuration is injected at deploy time via a `config.js` file served from S3:

```javascript
// config.js (written by CDK custom resource)
window.__CONFIG__ = {
  iotEndpoint: "a1b2c3d4e5f6g7-ats.iot.us-east-1.amazonaws.com",
  region: "us-east-1",
  cognitoIdentityPoolId: "us-east-1:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
};
```

This approach avoids baking environment-specific values into the webpack bundle, enabling the same build to work across environments.

---

## 7. Chatbot Design

### 7.1 Authentication Flow

```
                           +-------------+
                           |  LoginPage  |
                           +------+------+
                                  |
                    +-------------+-------------+
                    |                           |
              Sign In                     Sign Up
                    |                           |
              +-----v-----+             +-------v-------+
              |  Cognito   |             |   Cognito     |
              |  authUser  |             |   signUp      |
              +-----+------+             +-------+-------+
                    |                           |
                    |                    +------v-------+
                    |                    | Confirm Code |
                    |                    | (email)      |
                    |                    +------+-------+
                    |                           |
                    +-------------+-------------+
                                  |
                           +------v------+
                           | AuthTokens  |
                           | stored in   |
                           | localStorage|
                           +------+------+
                                  |
                           +------v------+
                           |ChatInterface|
                           +-------------+
```

**Session management:**
- Tokens are managed by `amazon-cognito-identity-js` (stored automatically in localStorage)
- `getCurrentSession()` checks for valid session on app mount
- Tokens auto-refresh via Cognito SDK's built-in refresh flow

### 7.2 Message Architecture

The chatbot communicates with the AgentCore Runtime via HTTP POST:

```
Browser --HTTP POST--> AgentCore Runtime ---> Strands Agent (Kimi K2.5)
```

**HTTP POST Invocation:**
- Endpoint: `https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encodedArn}/invocations`
- Authentication: `Authorization: Bearer {jwt_token}` header
- CORS: Fully supported (`access-control-allow-origin: *`)

**Note:** The AgentCore Runtime does not expose a public WebSocket endpoint for browser connections. The chatbot uses synchronous HTTP POST to `/invocations` instead.

#### HTTP Message Protocol

**Client -> Server (POST):**
```json
{"prompt": "Turn on the LED to rainbow mode"}
```

**Server -> Client (Response):**
```json
{"response": "I'll set the LED matrix to rainbow mode. The command has been sent!", "status": "success"}
```

#### UI Pattern

The `ChatInterface` sends a prompt via `fetch()`, shows a typing indicator while waiting, then renders the complete response when it arrives.

### 7.3 Session Persistence

- **Auth tokens**: Managed by Cognito SDK in `localStorage`. Auto-refreshed.
- **Chat history**: Held in React state (not persisted). Refreshing the page clears chat history.
- **Stateless requests**: Each HTTP POST to `/invocations` is independent. The agent does not maintain conversation state between requests.

---

## 8. AI Agent Design

### 8.1 Strands Agent on AgentCore Runtime

The agent is a Strands Agent deployed to Amazon Bedrock AgentCore Runtime via CodeZip (managed Python runtime).

| Property | Value |
|----------|-------|
| Foundation Model | `moonshotai.kimi-k2.5` (Kimi K2.5 via Bedrock) |
| Runtime Framework | Strands Agents SDK + `bedrock-agentcore` Python package |
| Packaging | CodeZip (Python 3.13, managed runtime — no Docker) |
| Endpoints | `/invocations` (POST), `/ping` (health) on port 8080 |
| App Framework | `BedrockAgentCoreApp` from `bedrock-agentcore` |
| Memory | AgentCore Memory with semantic, summary, and user preference strategies |

**System instruction:**
> You are a smart home assistant that controls devices in the user's home. You can control: LED Matrix, Rice Cooker, Fan, and Oven. Be helpful, concise, and confirm actions taken. Suggest creative lighting scenes, cooking presets, and comfort settings. Use what you remember about the user's preferences to personalize your responses.

### 8.2 AgentCore Memory

The agent uses AgentCore Memory for short-term conversation persistence and long-term knowledge extraction via three configured strategies:

| Strategy | Purpose | Namespace |
|----------|---------|-----------|
| **Semantic** (FactExtractor) | Extracts and stores factual knowledge from conversations | `/facts/{actorId}/` |
| **Summary** (SessionSummarizer) | Generates session summaries for context continuity | `/summaries/{actorId}/{sessionId}/` |
| **User Preference** (PreferenceLearner) | Learns user preferences (e.g., "warm lighting in evening") | `/preferences/{actorId}/` |

**Integration pattern:**
```python
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

config = AgentCoreMemoryConfig(memory_id=MEMORY_ID, session_id=session_id, actor_id=actor_id)
session_manager = AgentCoreMemorySessionManager(agentcore_memory_config=config, region_name=REGION)
agent = Agent(model=model, system_prompt=..., session_manager=session_manager)
```

- **Short-term**: Conversation messages are stored automatically per session via `AgentCoreMemorySessionManager`
- **Long-term**: Strategies extract facts, preferences, and summaries asynchronously and make them available as context in future sessions
- **Session/Actor IDs**: Derived from AgentCore Runtime request context (`session_id`, `x-amzn-bedrock-agentcore-runtime-user-id` header)

### 8.3 Tool Access via AgentCore Gateway (MCP)

The Strands Agent discovers and invokes tools through an AgentCore Gateway, which acts as an MCP (Model Context Protocol) server:

```
Strands Agent (MCP Client)
    |
    | MCP tool discovery + invocation
    v
AgentCore Gateway (MCP Server)
    URL: https://{gateway-id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp
    Auth: NONE (internal — only called by the runtime)
    |
    | Lambda Target
    v
iot-control Lambda
    |
    | Validates command, publishes to IoT Core
    v
AWS IoT Core MQTT
```

The MCP protocol allows the agent to dynamically discover available tools (device control actions) and their schemas from the gateway, then invoke them as needed.

### 8.4 Agent Directory Structure

```
agent/
├── agent.py              # BedrockAgentCoreApp with @app.entrypoint handler
├── tools/
│   └── device_control.py # Fallback tool for local dev (Lambda invocation via boto3)
├── skills/
│   ├── led-control/      # SKILL.md with LED-specific instructions
│   ├── rice-cooker-control/
│   ├── fan-control/
│   └── oven-control/
├── pyproject.toml        # Dependencies for AgentCore CodeZip packaging
└── Dockerfile            # Optional, for local container testing
```

**Skills** provide on-demand specialized instructions. When the agent activates a skill, it loads the full SKILL.md instructions into context:

```python
# Example: agent activates led-control skill
# -> Loads instructions about rainbow, breathing, chase modes
# -> Knows exact command format: {"action": "setMode", "mode": "rainbow"}
# -> Has allowed-tools: device_control
```

### 8.5 Command Validation

The `iot-control` Lambda validates all commands before publishing:

```python
DEVICE_COMMANDS = {
    "led_matrix": {
        "setMode":       {"required": ["mode"],       "values": {"mode": [7 modes]}},
        "setPower":      {"required": ["power"],      "types": {"power": bool}},
        "setBrightness": {"required": ["brightness"], "ranges": {"brightness": (0, 100)}},
        "setColor":      {"required": ["color"]},
    },
    # ... rice_cooker, fan, oven
}
```

Invalid commands return 400 with descriptive error messages. The agent receives these errors and can self-correct.

---

## 9. Infrastructure Design

### 9.1 Two-Stack Architecture

The system deploys as two CloudFormation stacks:

**Stack 1: `SmartHomeAssistantStack`** (managed by CDK) — standard AWS resources:
```
Cognito User Pool
  +-> User Pool Client + Domain
  +-> Identity Pool + Auth/Unauth IAM Roles

IoT Endpoint (Custom Resource)
  +-> iot-control Lambda (env: IOT_ENDPOINT)
  +-> Device Simulator config.js

S3 Buckets + CloudFront
  +-> BucketDeployment (static assets)
  +-> Custom Resource (config.js injection)
```

**Stack 2: `AgentCore-smarthome-default`** (managed by `agentcore` CLI) — AgentCore resources:
```
AgentCore Gateway (MCP Server)
  +-> Auth: NONE (only called by the runtime internally)
  +-> Lambda Target (iot-control Lambda + tool schema)

AgentCore Runtime
  +-> CodeZip (Python 3.13, agent code from S3)
  +-> BedrockAgentCoreApp (Strands Agent)
  +-> JWT Auth (Cognito User Pool)
  +-> Env: AGENTCORE_GATEWAY_{NAME}_URL, MODEL_ID, AGENTCORE_MEMORY_ID

AgentCore Memory (created by setup script via MemoryClient API)
  +-> Semantic strategy (fact extraction)
  +-> Summary strategy (session summaries)
  +-> User preference strategy (preference learning)
```

**Why two stacks?** AgentCore resources cannot be created via CDK for two reasons:
1. The CDK JS SDK does not yet include `@aws-sdk/client-bedrockagentcorecontrol`, so `AwsCustomResource` fails
2. A Python Lambda custom resource also fails because the Lambda runtime's bundled boto3 is too old for the `bedrock-agentcore-control` API

The `agentcore` CLI solves both by using its own CDK stack with the native `AWS::BedrockAgentCore::Runtime` CloudFormation type and packaging agent code as CodeZip (no Docker required).

**Key `agentcore` CLI quirks discovered during deployment:**
- `agentcore create --defaults` must be used (not `--no-agent`) to create a deploy target
- `aws-targets.json` must be seeded with `[{"name": "default", "region": "...", "account": "..."}]` for non-interactive deploy
- `agentcore deploy -y --verbose` enables non-interactive mode (the default TUI requires a TTY)
- `--outbound-auth` is not applicable for `lambda-function-arn` targets (Lambda targets don't need credential provider config)
- Agent code must include `pyproject.toml` for the CLI to package it as CodeZip
- Gateway `authorizerType` cannot be changed after creation — must delete and recreate the CloudFormation stack
- Gateway auth should be `NONE` for runtime-to-gateway calls; the runtime strips the user's JWT before passing to the handler, so the agent cannot forward it to the gateway
- The `agentcore` CLI sets gateway URL env vars as `AGENTCORE_GATEWAY_{GATEWAYNAME}_URL` (not `AGENTCORE_GATEWAY_URL`); agent code must auto-detect the pattern
- `agentcore deploy` drops custom `environmentVariables` set in `agentcore.json` — must patch them post-deploy via `update_agent_runtime` boto3 API (requires passing `agentRuntimeArtifact`, `roleArn`, `networkConfiguration`, and `authorizerConfiguration` alongside)

### 9.2 Deployment Architecture

```
deploy.sh (one-click)
    |
    +---> [1-3] Build frontends (npm install + npm run build)
    |
    +---> [4] CDK Bootstrap
    |
    +---> [5] CDK Deploy --all
    |         CloudFormation: SmartHomeAssistantStack
    |         -> Cognito, IoT Things, Lambda, S3, CloudFront
    |         -> Outputs: UserPoolId, LambdaArn, BucketNames, URLs
    |
    +---> [6] Fix Cognito User Pool settings
    |         -> Enable self-service sign-up (AllowAdminCreateUserOnly=false)
    |         -> Enable email auto-verification (auto-verified-attributes=email)
    |         (CDK selfSignUpEnabled doesn't always propagate correctly)
    |
    +---> [7] scripts/setup-agentcore.py
              |
              +---> Create AgentCore Memory (semantic + summary + user preference)
              +---> agentcore create --name smarthome --defaults
              +---> Replace default agent code with agent/
              +---> Patch agentcore.json (entrypoint, JWT auth, env vars)
              +---> Seed aws-targets.json (required for CLI deploy)
              +---> agentcore add gateway (NONE auth)
              +---> agentcore add gateway-target (Lambda, tool schema)
              +---> agentcore deploy -y --verbose
              |         CloudFormation: AgentCore-smarthome-default
              |         -> IAM Role, AgentCore Runtime
              +---> Fetch Gateway URL + Runtime ARN (from CFN stack outputs)
              +---> Patch runtime env vars (AGENTCORE_MEMORY_ID, MODEL_ID)
              |     (agentcore CLI drops custom env vars during deploy)
              +---> Update chatbot config.js in S3
              +---> Invalidate CloudFront cache
```

### 9.3 Runtime Configuration Injection

A key design challenge: React apps need environment-specific values (API endpoints, Cognito IDs) that are only known after CDK deploys the resources. The solution:

1. **Build time**: Webpack bundles the React app. `config.ts` reads from `window.__CONFIG__`
2. **Deploy time**: CDK custom resource writes `config.js` to S3 with actual values
3. **Runtime**: `index.html` loads `<script src="/config.js">` before the app bundle
4. **Result**: Same build artifact works for any environment

---

## 10. API Reference

### 10.1 AgentCore Runtime Endpoints

The AgentCore Runtime exposes three endpoints on port 8080:

#### POST /invocations

Invoke the Strands Agent and get a complete response.

**Authorization:** JWT (Cognito User Pool token)

**Request Body:**
```json
{
  "prompt": "Turn on the LED matrix to rainbow mode"
}
```

**Response:**
```json
{
  "response": "I've set the LED matrix to rainbow mode. The colorful animation should now be visible on your LED panel.",
  "status": "success"
}
```

#### GET /ping

Health check endpoint. Returns 200 when the runtime is healthy.

### 10.2 AgentCore Gateway (MCP Server)

**Endpoint:** `https://{gateway-id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp`

**Authorization:** NONE (only called internally by the runtime)

The gateway exposes device control tools via MCP protocol. The Strands Agent connects as an MCP client to discover available tools and their schemas, then invokes them to control devices.

**Lambda Target:** `iot-control` Lambda with tool schema defining:
- `control_device(device_type, command)` - Send a control command to a smart home device

### 10.3 iot-control Lambda Response Format

**MCP tool response (via AgentCore Gateway):**
```json
{
  "message": "Command sent to led_matrix",
  "device": "led_matrix",
  "command": {"action": "setMode", "mode": "rainbow"},
  "topic": "smarthome/led_matrix/command"
}
```

**Error response:**
```json
{
  "error": "Invalid action 'xyz' for led_matrix. Valid: ['setMode', 'setPower', 'setBrightness', 'setColor']"
}
```

### 10.4 Stack Outputs

**CDK Stack outputs** (consumed by `scripts/setup-agentcore.py`):

| Output Key | Description | Example |
|------------|-------------|---------|
| `IoTEndpointOutput` | AWS IoT Core data endpoint | `a1b2c3-ats.iot.us-west-2.amazonaws.com` |
| `UserPoolId` | Cognito User Pool ID | `us-west-2_AbCdEfGhI` |
| `UserPoolClientId` | Cognito App Client ID | `1a2b3c4d5e6f7g8h9i0j` |
| `IdentityPoolId` | Cognito Identity Pool ID | `us-west-2:xxxxxxxx-xxxx-...` |
| `CognitoDomain` | Cognito hosted UI domain | `smarthome-123456789.auth.us-west-2.amazoncognito.com` |
| `IoTControlLambdaArn` | Lambda ARN for gateway target | `arn:aws:lambda:us-west-2:...:function:smarthome-iot-control` |
| `ChatbotBucketName` | S3 bucket for chatbot config.js update | `smarthome-chatbot-123456789` |
| `ChatbotDistributionId` | CloudFront ID for cache invalidation | `E1234567890` |
| `DeviceSimulatorUrl` | Device Simulator URL | `https://d1234567890.cloudfront.net` |
| `ChatbotUrl` | Chatbot URL | `https://d0987654321.cloudfront.net` |

**AgentCore Stack outputs** (from `agentcore deploy`):

| Output Key | Description | Example |
|------------|-------------|---------|
| `RuntimeId` | AgentCore Runtime ID | `smarthome_smarthome-fcbh7hBwc5` |
| `RuntimeArn` | AgentCore Runtime ARN | `arn:aws:bedrock-agentcore:us-west-2:...:runtime/...` |

**Gateway and Runtime IDs** are also saved to `agentcore-state.json` for teardown.

---

## 11. MQTT Topic & Command Reference

### 11.1 Topic Hierarchy

```
smarthome/
├── led_matrix/
│   └── command      # Commands to LED matrix
├── rice_cooker/
│   └── command      # Commands to rice cooker
├── fan/
│   └── command      # Commands to fan
└── oven/
    └── command      # Commands to oven
```

### 11.2 Command Message Schemas

#### LED Matrix (`smarthome/led_matrix/command`)

| Action | Parameters | Example |
|--------|-----------|---------|
| `setPower` | `power`: boolean | `{"action":"setPower","power":true}` |
| `setMode` | `mode`: rainbow, breathing, chase, sparkle, fire, ocean, aurora | `{"action":"setMode","mode":"rainbow"}` |
| `setBrightness` | `brightness`: 0-100 | `{"action":"setBrightness","brightness":75}` |
| `setColor` | `color`: hex string | `{"action":"setColor","color":"#FF00FF"}` |

#### Rice Cooker (`smarthome/rice_cooker/command`)

| Action | Parameters | Example |
|--------|-----------|---------|
| `start` | `mode`: white_rice, brown_rice, porridge, steam | `{"action":"start","mode":"white_rice"}` |
| `stop` | (none) | `{"action":"stop"}` |
| `keepWarm` | `enabled`: boolean | `{"action":"keepWarm","enabled":true}` |

#### Fan (`smarthome/fan/command`)

| Action | Parameters | Example |
|--------|-----------|---------|
| `setPower` | `power`: boolean | `{"action":"setPower","power":true}` |
| `setSpeed` | `speed`: 0 (off), 1 (low), 2 (medium), 3 (high) | `{"action":"setSpeed","speed":2}` |
| `setOscillation` | `enabled`: boolean | `{"action":"setOscillation","enabled":true}` |

#### Oven (`smarthome/oven/command`)

| Action | Parameters | Example |
|--------|-----------|---------|
| `setPower` | `power`: boolean | `{"action":"setPower","power":true}` |
| `setMode` | `mode`: bake, broil, convection | `{"action":"setMode","mode":"bake"}` |
| `setTemperature` | `temperature`: 200-500 (Fahrenheit) | `{"action":"setTemperature","temperature":375}` |

---

## 12. Error Handling Strategy

### 12.1 Lambda Error Handling

The iot-control Lambda returns structured error responses:

```python
# MCP tool error response
{"error": "Invalid action 'xyz' for led_matrix. Valid: ['setMode', 'setPower', 'setBrightness', 'setColor']"}
```

The agent receives error responses via MCP and can:
1. Understand what went wrong
2. Inform the user
3. Attempt a corrected command

### 12.2 HTTP Request Resilience

The chatbot uses HTTP POST for each message. If a request fails:
- Error message displayed to the user
- User can retry by sending the message again
- JWT token auto-refreshes via Cognito SDK

### 12.3 MQTT Resilience

```
MQTT disconnect
  -> connectionChange(false) notifies all listeners
  -> UI shows "Disconnected" status
  -> MQTT5 client auto-reconnects
  -> On reconnect: re-subscribe all topics
  -> connectionChange(true) restores UI
```

---

## 13. Frontend Build Pipeline

### 13.1 Device Simulator

```
TypeScript (TSX) -> ts-loader -> Webpack 5 -> dist/
                                     |
                                     +-> Polyfills: url, util (required by aws-iot-device-sdk-v2)
                                     +-> CSS: style-loader + css-loader
                                     +-> HTML: html-webpack-plugin
                                     +-> Output: bundle.[hash].js + index.html
```

### 13.2 Chatbot

```
TypeScript (TSX) -> ts-loader -> Webpack 5 -> dist/
                                     |
                                     +-> CSS: style-loader + css-loader
                                     +-> HTML: html-webpack-plugin
                                     +-> Output: bundle.[hash].js + index.html
```

Both apps use content-hash filenames for cache busting via CloudFront.

---

## 14. Technology Choices and Rationale

| Choice | Rationale |
|--------|-----------|
| **MQTT5** over MQTT 3.1.1 | Better error reporting, message properties, shared subscriptions |
| **Cognito Identity Pool** for device sim | No login required for device simulation; SigV4 auth for IoT Core |
| **Cognito User Pool** for chatbot | Standard user management with email verification |
| **AgentCore Runtime** for agent hosting | Managed hosting with built-in WebSocket support, JWT auth, and session management |
| **AgentCore Gateway (MCP)** for tool access | Standard MCP protocol for tool discovery and invocation; decouples agent from tool implementation |
| **Strands Agents SDK** | Python-native agent framework with skill system, MCP client support, and BedrockAgentCoreApp integration |
| **Kimi K2.5** model (`moonshotai.kimi-k2.5`) | User requirement; available via Bedrock model access |
| **CDK over CloudFormation/SAM** | TypeScript type safety, higher-level constructs, better developer experience |
| **Two-stack deploy** (CDK + agentcore CLI) | CDK JS SDK lacks AgentCore client; `agentcore` CLI has native `AWS::BedrockAgentCore::Runtime` support |
| **CodeZip** (not Docker) for agent | No Docker required; `agentcore` CLI packages Python code as zip, uses managed Python 3.13 runtime |
| **config.js injection** | Decouples build from deployment environment |
| **requestAnimationFrame** for LED | 60fps rendering without setInterval timing issues |

---

## 15. Scalability Considerations

| Component | Scaling | Notes |
|-----------|---------|-------|
| CloudFront | Global edge cache | Auto-scales for static content |
| AgentCore Runtime | Managed scaling | CodeZip-based, scales based on request load |
| AgentCore Gateway | Managed scaling | MCP server with Lambda targets, scales automatically |
| Lambda | Concurrent executions | 1,000 default, auto-scales |
| IoT Core | Per-account | 500,000 connections default |
| Bedrock (Kimi-2.5) | Per-model throttling | Dependent on Kimi-2.5 quota |
| Cognito | Per-region | 40 req/sec default for auth APIs |

For production use, consider:
- CloudFront custom domain with ACM certificate
- Cognito advanced security features
- IoT Core fleet provisioning for real devices
- DynamoDB for chat history persistence

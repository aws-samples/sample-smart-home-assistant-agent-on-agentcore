# Smart Home Assistant Agent - Architecture & Design

## Table of Contents

- [1. System Overview](#1-system-overview)
- [2. High-Level Architecture](#2-high-level-architecture)
- [3. Data Flow Diagrams](#3-data-flow-diagrams)
- [4. Network Architecture](#4-network-architecture)
- [5. Security Architecture](#5-security-architecture)
- [6. Device Simulator Design](#6-device-simulator-design)
- [7. Chatbot Design](#7-chatbot-design)
- [8. AI Agent Design](#8-ai-agent-design) (includes [8.6 Agent Internal Data Flow](#86-agent-internal-data-flow))
- [8.7. Skill Management](#87-skill-management)
- [8.8. Per-User Model Selection](#88-per-user-model-selection)
- [8.9. Fixed Session ID and Session Tracking](#89-fixed-session-id-and-session-tracking)
- [9. Infrastructure Design](#9-infrastructure-design)
- [9.4. Admin Console Design](#94-admin-console-design)
- [9.5. Per-User Tool Permission Management](#95-per-user-tool-permission-management)
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
| Tool Access | AgentCore Gateway (MCP Server) + Lambda | Device discovery, command routing, and device control via MCP |
| Admin Console | React + TypeScript + REST API | Agent Harness Management: skills, models, tool access, integrations, sessions, memories, guardrails |
| Infrastructure | AWS CDK (TypeScript) | One-click deployment of all resources |

---

## 2. High-Level Architecture

```
+--------------------------------------------------------------------------------------------+
|                                  AWS Cloud                                                 |
|                                                                                            |
|  +--------------+ +--------------+ +--------------+  +---------------------------+         |
|  | CloudFront   | | CloudFront   | | CloudFront   |  |     Cognito               |         |
|  | (Device Sim) | | (Chatbot)    | | (Admin)      |  |  +--------+ +--------+   |         |
|  +------+-------+ +------+-------+ +------+-------+  |  |  User  | |Identity|   |         |
|         |                |                |           |  |  Pool  | |  Pool  |   |         |
|  +------v-------+ +------v-------+ +------v-------+  |  +---+----+ +---+----+   |         |
|  |  S3 Bucket   | |  S3 Bucket   | |  S3 Bucket   |  +------+----------+--------+         |
|  +--------------+ +--------------+ +------+-------+         |          |                   |
|                          |                |                  |          |                   |
|  +-----------+ MQTT/WSS  | Bearer JWT     | Bearer JWT      |          |                   |
|  | Browser   +--------+  |                |                  |          |                   |
|  | (Device   |        |  +-----v----------v---+              |          |                   |
|  |  Sim App) |        |  | AgentCore Runtime   |  +----------v-------+  |                   |
|  +-----------+        |  | (Strands Agent)     |  | API Gateway      |  |                   |
|             |         |  +-----+---------------+  | (Admin API)      |  |                   |
|  Cognito    |         |        |                  +----+-------------+  |                   |
|  Identity   |         |        | MCP Client            |                |                   |
|  Pool       |         |  +-----v-----------------+     |                |                   |
|  (SigV4)    |         |  | AgentCore Gateway     |     |                |                   |
|             v         |  +-----+-----------------+  +--v-------------+  |                   |
|  +------------------+ |        |                    | admin-api      |  |                   |
|  | AWS IoT Core     | |        | Lambda Targets     | Lambda         |  |                   |
|  | MQTT Broker      |<+--------+                    +--+-------------+  |                   |
|  +------------------+ |  +-----v-----------------+     |                |                   |
|                       |  | iot-control Lambda     |  +--v-------------+  |                   |
|                       |  | Validates & publishes  |  | DynamoDB       |  |                   |
|                       +->| MQTT                   |  | (skills table) |<-+ Agent reads      |
|                       |  +------------------------+  +----------------+                     |
|                       |  +------------------------+  +----------------+                     |
|                       |  | iot-discovery Lambda   |  | S3 Bucket      |                     |
|                       |  | Returns device list    |  | (skill files)  |<-- Admin Lambda     |
|                       |  +------------------------+  +----------------+    (presigned URLs) |
+--------------------------------------------------------------------------------------------+
```

### Component Interaction Matrix

```
                    Cognito   Cognito    IoT     AgentCore  AgentCore   IoT Control  IoT Discovery  Admin   DynamoDB  S3 Skill
                    UserPool  Identity   Core    Runtime    Gateway     Lambda       Lambda         Lambda  Skills    Files
                              Pool
Device Simulator                 R        R/W
Chatbot App          R                            R/W
Admin Console        R                                                                              R/W
AgentCore Runtime    V                                       I (MCP)                                         R
AgentCore Gateway    V                                       (self)      I            I
IoT Control Lambda                        W
IoT Discovery Lambda                                                                 (self)
Admin Lambda                                                                                 (self)  R/W     R/W

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

### Flow 2: Device Discovery

```
User (Chatbot)
    |
    | "Turn on all my devices"
    v
AgentCore Runtime --> Strands Agent (activates all-devices-on skill)
                                       |
                                       | 1. MCP call: discover_devices()
                                       v
                                AgentCore Gateway
                                       |
                                       | Lambda Target
                                       v
                                iot-discovery Lambda
                                       |
                                       | Returns device list with powerOn/powerOff commands
                                       v
                                Agent receives device list
                                       |
                                       | 2. For each device (sequentially, 5s apart):
                                       |    MCP call: control_device(device_type, powerOn command)
                                       v
                                iot-control Lambda --> IoT Core --> Device Simulator
```

### Flow 3: User Authentication

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

### Flow 4: Device Simulator MQTT Connection

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
    +---> CloudFront (Admin Console) ---> S3 Bucket (static assets)
    |         |
    |         +---> /config.js (adminApiUrl, Cognito IDs)
    |
    +---> HTTPS: Admin API (API Gateway)
    |         https://{api-id}.execute-api.{region}.amazonaws.com/prod/skills
    |         |
    |         +---> Lambda Target: admin-api Lambda
    |         +---> Auth: Cognito User Pool Authorizer (admin group required)
    |         +---> DynamoDB: smarthome-skills table (skill CRUD, all spec fields)
    |         +---> S3: smarthome-skill-files bucket (file management via presigned URLs)
    |
    +---> HTTPS: AgentCore Gateway (MCP Server, internal to agent)
    |         https://{gateway-id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp
    |         |
    |         +---> Lambda Target: iot-control Lambda (control_device tool)
    |         +---> Lambda Target: iot-discovery Lambda (discover_devices tool)
    |         +---> Auth: CUSTOM_JWT (Cognito, validates user JWT for per-user Cedar policies)
    |         +---> Policy Engine: Cedar per-user tool access control (ENFORCE mode)
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
|  Layer 4: AgentCore Gateway (CUSTOM_JWT + Policy Engine)          |
|  +------------------------------------------------------------+  |
|  | - Auth: CUSTOM_JWT (same Cognito User Pool as runtime)      |  |
|  | - Runtime propagates user JWT to agent via                  |  |
|  |   requestHeaderAllowlist: ["Authorization"]                 |  |
|  | - Agent forwards JWT to gateway MCP client                  |  |
|  | - Policy Engine: Cedar per-user permit policies (ENFORCE)   |  |
|  |   principal.id from JWT sub claim for per-user control      |  |
|  +------------------------------------------------------------+  |
|                                                                   |
|  Layer 5: Admin API (Cognito + Group Check)                      |
|  +------------------------------------------------------------+  |
|  | - API Gateway Cognito User Pools Authorizer (validates JWT) |  |
|  | - Lambda checks cognito:groups claim for "admin" membership |  |
|  | - Protects: /skills CRUD endpoints                          |  |
|  | - Non-admin users receive 403 Forbidden                     |  |
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
| iot-discovery Lambda | (none) | Returns mock device list |
| admin-api Lambda | dynamodb:* | `arn:...:table/smarthome-skills` |
| admin-api Lambda | s3:GetObject, PutObject, DeleteObject, ListBucket | `arn:...:smarthome-skill-files-*` |
| admin-api Lambda | cognito-idp:ListUsers, AdminListGroupsForUser | Cognito User Pool |
| admin-api Lambda | bedrock-agentcore:ListActors, ListMemoryRecords | `*` (AgentCore Memory) |
| admin-api Lambda | bedrock-agentcore:Create/Get/Update/Delete Policy* | `*` (policy engine + gateway management) |
| admin-api Lambda | iam:PassRole, iam:PutRolePolicy | AgentCore roles |
| AgentCore Runtime Role | bedrock:InvokeModel | Kimi-2.5 model |
| AgentCore Runtime Role | dynamodb:Query, GetItem, Scan | `arn:...:table/smarthome-skills` |

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
- Session ID: `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: user-session-{cognito-sub}` (fixed per user)
- User ID: Passed in the POST body as `userId` (the runtime strips the `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` header before forwarding to the agent)
- CORS: Fully supported (`access-control-allow-origin: *`)

**Note:** The AgentCore Runtime does not expose a public WebSocket endpoint for browser connections. The chatbot uses synchronous HTTP POST to `/invocations` instead.

#### HTTP Message Protocol

**Client -> Server (POST):**
```json
{"prompt": "Turn on the LED to rainbow mode", "userId": "user@example.com"}
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
- **Session ID**: Fixed per user, derived from `cognito:sub` UUID. Sent via `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header. Same user always gets the same runtime session.
- **User ID**: User's email extracted from JWT and sent in the POST body as `userId`. Used for per-user skill loading, model selection, and session tracking.
- **AgentCore Memory**: Provides conversation persistence across sessions via semantic, summary, and preference extraction strategies.

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
> You are a smart home assistant that controls devices in the user's home. You can control: LED Matrix, Rice Cooker, Fan, and Oven. Be helpful, concise, and confirm actions taken. Suggest creative lighting scenes, cooking presets, and comfort settings. Use what you remember about the user's preferences to personalize your responses. Never fabricate tool results — if a tool call fails or is rejected, report the failure honestly to the user.

### 8.2 AgentCore Memory

The agent uses AgentCore Memory for short-term conversation persistence and long-term knowledge extraction. Memory is created and deployed via the `agentcore` CLI as a first-class project resource (`agentcore add memory`), which manages its lifecycle through the same CloudFormation stack as the runtime and gateway.

**Three configured strategies:**

| Strategy | Type | Namespace |
|----------|------|-----------|
| **Semantic** | `SEMANTIC` | `/users/{actorId}/facts` |
| **Summarization** | `SUMMARIZATION` | `/summaries/{actorId}/{sessionId}` |
| **User Preference** | `USER_PREFERENCE` | `/users/{actorId}/preferences` |

**CLI-managed lifecycle:**
```bash
# Memory is added to the agentcore project alongside gateway and runtime
agentcore add memory --name SmartHomeMemory --strategies SEMANTIC,SUMMARIZATION,USER_PREFERENCE
agentcore deploy -y --verbose
# CLI auto-sets MEMORY_SMARTHOMEMEMORY_ID env var on the runtime
```

**Agent integration** (`agent/memory/session.py`):
```python
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

MEMORY_ID = os.getenv("MEMORY_SMARTHOMEMEMORY_ID", "")  # auto-set by agentcore CLI

def get_memory_session_manager(session_id, actor_id):
    actor_id = _sanitize_actor_id(actor_id)  # replace @/. with _ for Memory API
    retrieval_config = {
        f"/users/{actor_id}/facts": RetrievalConfig(top_k=3, relevance_score=0.5),
        f"/summaries/{actor_id}/{session_id}": RetrievalConfig(top_k=3, relevance_score=0.5),
        f"/users/{actor_id}/preferences": RetrievalConfig(top_k=3, relevance_score=0.5),
    }
    return AgentCoreMemorySessionManager(
        AgentCoreMemoryConfig(memory_id=MEMORY_ID, session_id=session_id, actor_id=actor_id,
                              retrieval_config=retrieval_config),
        REGION,
    )
```

- **Short-term**: Conversation messages stored automatically per session via `AgentCoreMemorySessionManager`
- **Long-term**: Strategies extract facts, preferences, and summaries asynchronously and make them available as context in future sessions
- **Retrieval**: Each namespace has configurable `top_k` and `relevance_score` thresholds for context injection
- **Session/Actor IDs**: Derived from AgentCore Runtime request context (`session_id`, `x-amzn-bedrock-agentcore-runtime-user-id` header)
- **Actor ID sanitization**: AgentCore Memory requires actor IDs matching `[a-zA-Z0-9][a-zA-Z0-9-_/]*`. Since Cognito emails contain `@` and `.`, `_sanitize_actor_id()` replaces invalid characters with `_` (e.g., `user@example.com` → `user_example_com`).
- **Env var**: `MEMORY_SMARTHOMEMEMORY_ID` is auto-set by the `agentcore` CLI on deploy (follows `MEMORY_<NAME>_ID` convention)

### 8.3 Tool Access via AgentCore Gateway (MCP)

The Strands Agent discovers and invokes tools through an AgentCore Gateway, which acts as an MCP (Model Context Protocol) server:

```
Strands Agent (MCP Client)
    |
    | MCP tool discovery + invocation
    v
AgentCore Gateway (MCP Server)
    URL: https://{gateway-id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp
    Auth: CUSTOM_JWT (Cognito — user JWT forwarded by agent for per-user policy)
    Policy Engine: SmartHomeUserPermissions (ENFORCE mode, Cedar permit policies)
    |
    | Lambda Target (if permitted by policy)
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
├── memory/
│   ├── __init__.py
│   └── session.py        # AgentCoreMemorySessionManager factory (follows agentcore CLI pattern)
├── tools/
│   └── device_control.py # Fallback tool for local dev (Lambda invocation via boto3)
├── skills/
│   ├── led-control/      # SKILL.md with LED-specific instructions
│   ├── rice-cooker-control/
│   ├── fan-control/
│   ├── oven-control/
│   └── all-devices-on/   # Discovers devices then turns them on sequentially
├── pyproject.toml        # Dependencies for AgentCore CodeZip packaging
└── Dockerfile            # Optional, for local container testing
```

**Skills** provide on-demand specialized instructions. Skills are loaded dynamically from DynamoDB per invocation (`load_skills_from_dynamodb`), with the `./skills/` directory serving as a fallback when `SKILLS_TABLE_NAME` is not configured. When the agent activates a skill, it loads the full instructions into context:

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

### 8.6 Agent Internal Data Flow

The following diagram shows the complete lifecycle of a single user request as it flows through the agent's internal components:

```
                          HTTP POST /invocations
                          Authorization: Bearer {jwt}
                          {"prompt": "Set LED to rainbow and fan to medium"}
                                    |
                                    v
                    +-------------------------------+
                    |   BedrockAgentCoreApp          |
                    |   (bedrock_agentcore)          |
                    |                               |
                    |   1. JWT validation (Cognito)  |
                    |   2. Extract request context:  |
                    |      - session_id              |
                    |      - actor_id (from header)  |
                    +---------------+---------------+
                                    |
                                    v
                    +-------------------------------+
                    |   handle_invocation()          |
                    |   @app.entrypoint              |
                    |                               |
                    |   3. Parse prompt from payload  |
                    |   4. Extract session_id,        |
                    |      actor_id from context      |
                    +---------------+---------------+
                                    |
                                    v
                    +-------------------------------+
                    |   memory/session.py            |
                    |   get_memory_session_manager() |
                    |                               |
                    |   5. Read MEMORY_SMARTHOME-    |
                    |      MEMORY_ID env var          |
                    |   6. Build retrieval config     |
                    |      per namespace (top_k=3)   |
                    |   7. Return SessionManager      |
                    +---------------+---------------+
                                    |
                                    v
                    +-------------------------------+
                    |   invoke_agent()               |
                    |                               |
                    |   8. Connect MCP client to      |
                    |      AgentCore Gateway          |
                    |   9. Discover tools via MCP     |
                    |      (list_tools_sync with      |
                    |       pagination)               |
                    |  10. Create Strands Agent with:  |
                    |      - BedrockModel (Kimi K2.5) |
                    |      - MCP tools                |
                    |      - Skills plugin            |
                    |      - Session manager          |
                    +---------------+---------------+
                                    |
                                    v
                    +-------------------------------+
                    |   Strands Agent                |
                    |   agent(prompt)                |
                    |                               |
                    |  11. Session manager retrieves  |
                    |      memory context:            |
                    |      - /users/{actor}/facts     |
                    |      - /summaries/{actor}/{sid} |
                    |      - /users/{actor}/prefs     |
                    |                               |
                    |  12. LLM receives:             |
                    |      - System prompt            |
                    |      - Retrieved memory context |
                    |      - Activated skill prompts  |
                    |      - User prompt              |
                    |      - Available tool schemas   |
                    |                               |
                    |  13. LLM reasons and emits      |
                    |      tool_use blocks:           |
                    |      control_device(            |
                    |        device_type="led_matrix",|
                    |        command={action:"setMode"|
                    |                 mode:"rainbow"})|
                    |      control_device(            |
                    |        device_type="fan",       |
                    |        command={action:"setSpeed|
                    |                 speed:2})       |
                    +------+----------------+-------+
                           |                |
                    +------v------+  +------v------+
                    | MCP call #1 |  | MCP call #2 |
                    | (Gateway)   |  | (Gateway)   |
                    +------+------+  +------+------+
                           |                |
                    +------v------+  +------v------+
                    | Lambda      |  | Lambda      |
                    | iot-control |  | iot-control |
                    +------+------+  +------+------+
                           |                |
                    +------v------+  +------v------+
                    | IoT Core    |  | IoT Core    |
                    | led_matrix/ |  | fan/command |
                    | command     |  |             |
                    +------+------+  +------+------+
                           |                |
                           v                v
                    +-------------------------------+
                    |   Device Simulator (Browser)   |
                    |   MQTT subscribers update UI   |
                    +-------------------------------+
                                    |
                                    | (back in the agent)
                                    v
                    +-------------------------------+
                    |  14. LLM receives tool results |
                    |      and generates final text  |
                    |                               |
                    |  15. Session manager stores     |
                    |      conversation to memory     |
                    |      (async extraction runs     |
                    |       for facts, summaries,     |
                    |       preferences)              |
                    +---------------+---------------+
                                    |
                                    v
                          HTTP Response 200
                          {"response": "Done! I've set the
                           LED matrix to rainbow mode and
                           the fan to medium speed.",
                           "status": "success"}
```

**Key observations:**
- Steps 8-9 (MCP tool discovery) happen on every invocation — tools are not cached between requests
- Step 11 (memory retrieval) injects relevant context from prior conversations before the LLM sees the prompt
- Step 13 (tool calls) may involve multiple sequential or parallel tool invocations depending on the LLM's reasoning
- Step 15 (memory storage) is asynchronous — the response is returned before extraction strategies complete
- The MCP client connection is scoped to a single invocation (`with mcp_client:` context manager)
- Skills are loaded dynamically from DynamoDB per invocation (global + user-specific), with filesystem fallback

### 8.7 Skill Management

The agent's skills (specialized instruction sets) are stored in DynamoDB and managed via an admin console. This enables administrators to add, modify, or delete skills without redeploying the agent.

**Architecture:**

```
Admin Console (React)
    |
    | HTTPS (Bearer JWT)
    v
API Gateway (Cognito Authorizer)
    |
    | Lambda Proxy Integration
    v
admin-api Lambda
    |
    +--- CRUD operations ---> DynamoDB: smarthome-skills
    |                           PK: userId (__global__ or specific user)
    |                           SK: skillName
    |                           Fields: description, instructions, allowedTools,
    |                                   license, compatibility, metadata
    |
    +--- File management ---> S3: smarthome-skill-files-{accountId}
    |    (presigned URLs)       {userId}/{skillName}/scripts/...
    |                           {userId}/{skillName}/references/...
    |                           {userId}/{skillName}/assets/...
    |
    v (per invocation)
agent.py: load_skills_from_dynamodb(actor_id)
    |
    +-> Query userId = "__global__" (shared skills)
    +-> Query userId = actor_id (user-specific overrides)
    +-> Construct Skill objects (all spec fields) -> AgentSkills plugin
    v
Strands Agent (with dynamic skills)
```

**DynamoDB Table Schema (`smarthome-skills`):**

All fields from the [Agent Skills specification](https://agentskills.io/specification) are supported:

| Attribute | Type | Description |
|-----------|------|-------------|
| `userId` (PK) | String | `__global__` for shared skills, or Cognito username for per-user |
| `skillName` (SK) | String | Skill identifier (e.g., `led-control`). 1-64 chars, lowercase alphanumeric + hyphens |
| `description` | String | Skill description, max 1024 chars (shown in skill metadata) |
| `instructions` | String | Full markdown instructions (SKILL.md body, loaded on skill activation) |
| `allowedTools` | List\<String\> | Tools the skill can use (e.g., `["device_control"]`) |
| `license` | String | Optional. License name or reference (e.g., `Apache-2.0`) |
| `compatibility` | String | Optional, max 500 chars. Environment requirements (e.g., `Requires Python 3.12+`) |
| `metadata` | Map\<String, String\> | Optional. Arbitrary key-value pairs for additional metadata |
| `createdAt` | String | ISO 8601 timestamp |
| `updatedAt` | String | ISO 8601 timestamp |

**Skill File Storage (S3):**

Skill directory files (scripts, references, assets) are stored in S3:

```
S3: smarthome-skill-files-{accountId}
  {userId}/{skillName}/scripts/extract.py
  {userId}/{skillName}/references/REFERENCE.md
  {userId}/{skillName}/assets/template.json
```

- Files are managed via presigned URLs (browser uploads/downloads directly to S3)
- Admin API generates time-limited presigned URLs for PUT (upload) and GET (download)
- Only three directories are allowed: `scripts`, `references`, `assets`
- When a skill is deleted, all its S3 files are cascade-deleted

**Skill Override Rule:** When a user-specific skill has the same name as a global skill, the user-specific version takes precedence. Global skills are loaded first, then user-specific skills overwrite by name.

**Fallback:** If the `SKILLS_TABLE_NAME` environment variable is not set or the DynamoDB query fails, the agent falls back to loading skills from the local `./skills/` directory (the 5 built-in skills).

**Admin API Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/skills?userId=` | List skills for a user scope |
| `POST` | `/skills` | Create a new skill (all spec fields) |
| `GET` | `/skills/{userId}/{skillName}` | Get a single skill |
| `PUT` | `/skills/{userId}/{skillName}` | Update a skill (all spec fields) |
| `DELETE` | `/skills/{userId}/{skillName}` | Delete a skill (cascade-deletes S3 files) |
| `GET` | `/skills/users` | List distinct user scopes |
| `GET` | `/skills/{userId}/{skillName}/files` | List files in a skill directory |
| `POST` | `/skills/{userId}/{skillName}/files/upload-url` | Get presigned S3 upload URL |
| `POST` | `/skills/{userId}/{skillName}/files/download-url` | Get presigned S3 download URL |
| `DELETE` | `/skills/{userId}/{skillName}/files?path=` | Delete a skill file |
| `GET` | `/settings/{userId}` | Get user settings (modelId) |
| `PUT` | `/settings/{userId}` | Update user settings |
| `GET` | `/sessions` | List all user runtime sessions |
| `GET` | `/users` | List all Cognito users with groups |
| `GET` | `/tools` | List all gateway tools (reads tool schemas from targets) |
| `GET` | `/users/{userId}/permissions` | Get user's allowed tools |
| `PUT` | `/users/{userId}/permissions` | Update allowed tools + sync Cedar policies |
| `GET` | `/memories` | List all memory actors |
| `GET` | `/memories/{actorId}` | Get long-term memory records (facts + preferences) for an actor |

**Authorization:** All admin API endpoints require a valid Cognito JWT. The Lambda additionally checks that the caller belongs to the `admin` Cognito group (returns 403 if not).

### 8.8 Per-User Model Selection

Administrators can assign a different LLM model to each user (or set a global default) via the Admin Console. The setting is stored in DynamoDB using a reserved key `skillName = "__settings__"`.

**Storage format:**
```
PK: userId (e.g., "zihangh@amazon.com" or "__global__")
SK: "__settings__"
modelId: "us.anthropic.claude-sonnet-4-6"
```

**Resolution order:** User-specific setting > `__global__` setting > `MODEL_ID` env var (default: `moonshotai.kimi-k2.5`).

**Available models** (shown as dropdown in Admin Console):
- Moonshot: Kimi K2.5, Kimi K2 Thinking
- Claude 4.6: Sonnet, Opus
- Claude 4.5: Sonnet, Opus, Haiku
- Claude 4: Sonnet, Opus, Opus 4.1
- Claude 3.x: 3.7 Sonnet, 3.5 Haiku
- DeepSeek: V3.2, V3.1, R1
- Qwen: Qwen3 235B, Next 80B, 32B Dense, VL 235B, Coder 480B, Coder 30B
- GLM (Z.AI): GLM 5, GLM 4.7, GLM 4.7 Flash
- MiniMax: M2.5, M2.1, M2
- Meta Llama: Llama 4 Maverick/Scout, Llama 3.3 70B
- OpenAI: GPT OSS 120B/20B

### 8.9 Fixed Session ID and Session Tracking

Each user gets a **fixed runtime session ID** derived from their Cognito `sub` (UUID). This means the same user always uses the same session across invocations, enabling session persistence.

**Session ID format:** `user-session-{cognito-sub}` (set by the chatbot via the `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header).

**User identification:** The AgentCore Runtime strips the `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` header before forwarding to the agent process. To work around this, the chatbot passes the user's email in the POST body as `userId`, and the agent reads it from `payload.get("userId")`.

**Session tracking:** On each invocation, the agent records `{userId, sessionId, lastActiveAt}` to DynamoDB (key: `userId`, `skillName = "__session__"`). The Admin Console reads these records in the Sessions tab.

**Stop session:** The Admin Console calls the AgentCore `StopRuntimeSession` API directly from the browser using the admin's JWT token (the Lambda cannot do this because the runtime uses JWT auth, not SigV4).

---

## 9. Infrastructure Design

### 9.1 Two-Stack Architecture

The system deploys as two CloudFormation stacks:

**Stack 1: `SmartHomeAssistantStack`** (managed by CDK) — standard AWS resources:
```
Cognito User Pool
  +-> User Pool Client + Domain
  +-> Identity Pool + Auth/Unauth IAM Roles
  +-> Admin Group (for skill management access)

IoT Endpoint (Custom Resource)
  +-> iot-control Lambda (env: IOT_ENDPOINT) — validates & publishes MQTT commands
  +-> iot-discovery Lambda — returns available devices (mock)
  +-> Device Simulator config.js

DynamoDB Table (smarthome-skills)
  +-> Skill storage (global + per-user, full Agent Skills spec fields)
  +-> Admin Lambda read/write access

S3 Bucket (smarthome-skill-files)
  +-> Skill directory files: scripts/, references/, assets/
  +-> CORS enabled for presigned URL uploads from browser
  +-> Admin Lambda read/write access

Admin API (API Gateway + Lambda)
  +-> Cognito Authorizer (admin group check)
  +-> CRUD endpoints for skill management (all spec fields)
  +-> File management endpoints (presigned URL upload/download, list, delete)

S3 Buckets + CloudFront (x3)
  +-> Device Simulator, Chatbot, Admin Console
  +-> BucketDeployment (static assets)
  +-> Custom Resource (config.js injection)
```

**Stack 2: `AgentCore-smarthome-default`** (managed by `agentcore` CLI) — AgentCore resources:
```
AgentCore Gateway (MCP Server)
  +-> Auth: CUSTOM_JWT (Cognito — same User Pool as runtime)
  +-> Policy Engine: SmartHomeUserPermissions (ENFORCE mode)
  +-> Lambda Target: SmartHomeDeviceControl (iot-control Lambda + tool schema)
  +-> Lambda Target: SmartHomeDeviceDiscovery (iot-discovery Lambda + tool schema)

AgentCore Runtime
  +-> CodeZip (Python 3.13, agent code from S3)
  +-> BedrockAgentCoreApp (Strands Agent)
  +-> JWT Auth (Cognito User Pool)
  +-> requestHeaderAllowlist: ["Authorization"] (propagate user JWT to agent)
  +-> Env: AGENTCORE_GATEWAY_{NAME}_URL, MEMORY_SMARTHOMEMEMORY_ID, MODEL_ID, SKILLS_TABLE_NAME

AgentCore Memory (managed by agentcore CLI: `agentcore add memory`)
  +-> SEMANTIC strategy (fact extraction)
  +-> SUMMARIZATION strategy (session summaries)
  +-> USER_PREFERENCE strategy (preference learning)
  +-> Env var auto-set: MEMORY_SMARTHOMEMEMORY_ID
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
- Gateway auth should be `CUSTOM_JWT` for per-user tool control; the runtime propagates the user's JWT to agent code via `requestHeaderAllowlist: ["Authorization"]` (set in `UpdateAgentRuntime`), and the agent forwards it to the gateway MCP client
- The `agentcore` CLI sets gateway URL env vars as `AGENTCORE_GATEWAY_{GATEWAYNAME}_URL` (not `AGENTCORE_GATEWAY_URL`); agent code must auto-detect the pattern
- `agentcore deploy` drops custom `environmentVariables` set in `agentcore.json` — must patch them post-deploy via `update_agent_runtime` boto3 API (requires passing `agentRuntimeArtifact`, `roleArn`, `networkConfiguration`, and `authorizerConfiguration` alongside). CLI-managed env vars like `MEMORY_<NAME>_ID` and `AGENTCORE_GATEWAY_<NAME>_URL` are preserved.
- `agentcore add memory --name <Name> --strategies SEMANTIC,SUMMARIZATION,USER_PREFERENCE` adds memory as a project resource deployed via the same CFN stack; the CLI auto-sets `MEMORY_<NAME>_ID` env var on the runtime

### 9.2 Deployment Architecture

```
deploy.sh (one-click)
    |
    +---> [1-4] Build frontends (Device Sim, Chatbot, Admin Console)
    |
    +---> [5] CDK Bootstrap
    |
    +---> [6] CDK Deploy --all
    |         CloudFormation: SmartHomeAssistantStack
    |         -> Cognito, IoT Things, Lambda, S3, CloudFront
    |         -> DynamoDB (skills table), API Gateway (admin API)
    |         -> Outputs: UserPoolId, LambdaArn, BucketNames, URLs, AdminApiUrl
    |
    +---> [7] Fix Cognito User Pool settings
    |         -> Enable self-service sign-up (AllowAdminCreateUserOnly=false)
    |         -> Enable email auto-verification (auto-verified-attributes=email)
    |         (CDK selfSignUpEnabled doesn't always propagate correctly)
    |
    +---> [8] scripts/setup-agentcore.py
              |
              +---> [1/8] agentcore create --name smarthome --defaults
              +---> [2/8] Replace default agent code with agent/
              |           Patch agentcore.json (entrypoint, JWT auth, env vars)
              |           Seed aws-targets.json (required for CLI deploy)
              +---> [3/8] agentcore add memory --name SmartHomeMemory
              |           --strategies SEMANTIC,SUMMARIZATION,USER_PREFERENCE
              +---> [4/8] agentcore add gateway (CUSTOM_JWT auth, Cognito)
              +---> [5/8] agentcore add gateway-target SmartHomeDeviceControl
              |           (iot-control Lambda + control_device tool schema)
              +---> [5b/8] agentcore add gateway-target SmartHomeDeviceDiscovery
              |           (iot-discovery Lambda + discover_devices tool schema)
              +---> [6/8] agentcore add evaluator
              +---> [7/8] agentcore add online-eval
              +---> [8/8] agentcore deploy -y --verbose
              |         CloudFormation: AgentCore-smarthome-default
              |         -> Runtime, Gateway, Memory, IAM Role
              +---> Fetch resource IDs (from CFN stack outputs)
              +---> Patch runtime env vars (MODEL_ID, AWS_REGION, SKILLS_TABLE_NAME)
              +---> Patch runtime: requestHeaderAllowlist: ["Authorization"]
              |     (propagate user JWT to agent for gateway auth)
              +---> Patch admin Lambda env vars (AGENT_RUNTIME_ARN, SKILL_FILES_BUCKET,
              |     GATEWAY_ID, COGNITO_USER_POOL_ID)
              +---> Grant runtime role DynamoDB read access (inline IAM policy)
              |     (agentcore CLI drops custom env vars during deploy;
              |      MEMORY_SMARTHOMEMEMORY_ID is set by CLI automatically)
              +---> Update chatbot config.js in S3
              +---> Invalidate CloudFront cache
    |
    +---> scripts/seed-skills.py
              +---> Read 5 SKILL.md files from agent/skills/
              +---> Write to DynamoDB as __global__ skills
```

### 9.3 Runtime Configuration Injection

A key design challenge: React apps need environment-specific values (API endpoints, Cognito IDs) that are only known after CDK deploys the resources. The solution:

1. **Build time**: Webpack bundles the React app. `config.ts` reads from `window.__CONFIG__`
2. **Deploy time**: CDK custom resource writes `config.js` to S3 with actual values
3. **Runtime**: `index.html` loads `<script src="/config.js">` before the app bundle
4. **Result**: Same build artifact works for any environment

### 9.4 Admin Console Design

The admin console is an independent React + TypeScript frontend app for managing agent skills. It follows the same deployment pattern as the device simulator and chatbot (S3 + CloudFront + config.js injection).

**Directory Structure:**

```
admin-console/
├── src/
│   ├── index.tsx            # React DOM entry point
│   ├── App.tsx              # Auth routing + admin role gate
│   ├── App.css              # Dark theme styles (matches chatbot)
│   ├── config.ts            # Runtime config (adminApiUrl, Cognito IDs)
│   ├── auth/
│   │   ├── CognitoAuth.ts   # Sign in, session management, admin role check
│   │   └── LoginPage.tsx    # Login form
│   ├── api/
│   │   └── adminApi.ts      # REST client for admin API (skills, models, tool access, memories, sessions)
│   └── components/
│       └── AdminConsole.tsx  # 7-tab harness management (skills, models, tool access, integrations, sessions, memories, guardrails)
├── public/
│   ├── index.html           # HTML template with config.js loader
│   └── config.js            # Runtime config placeholder (overwritten by CDK)
├── webpack.config.js        # Webpack 5 (dev server on port 3002)
├── tsconfig.json
└── package.json
```

**Key Features:**

- **Admin role gate**: After Cognito login, decodes the JWT `cognito:groups` claim. Users not in the `admin` group see an "Access Denied" page.
- **Seven tabs** for comprehensive agent harness management:

| Tab | Purpose |
|-----|---------|
| **Skills** | Skill CRUD with all [Agent Skills spec](https://agentskills.io/specification) fields, file manager, metadata editor |
| **Models** | Global default model + per-user model override table (priority: per-user > global > env var) |
| **Tool Access** | Per-user tool permissions via Cedar policies, Policy Engine mode toggle (ENFORCE/LOG_ONLY) |
| **Integrations** | Tool source registry — Lambda targets (active), MCP servers, A2A agents, API Gateway (planned) |
| **Sessions** | Runtime session monitoring with User ID, Session ID, Last Active, and Stop button |
| **Memories** | Long-term memory viewer — per-user facts and preferences from AgentCore Memory |
| **Guardrails** | Links to AgentCore Evaluator, Bedrock Guardrails consoles, and Cedar Policy Engine settings |

**CDK Resources:**

| Resource | Description |
|----------|-------------|
| `smarthome-admin-console-{accountId}` S3 Bucket | Static assets |
| `smarthome-skill-files-{accountId}` S3 Bucket | Skill directory files (scripts, references, assets) with CORS |
| CloudFront Distribution | HTTPS CDN |
| `config.js` (written by setup script) | Injects `adminApiUrl`, `agentRuntimeArn`, `cognitoUserPoolId`, `cognitoClientId`, `region` |

### 9.5 Per-User Tool Permission Management

Administrators can control which gateway tools each user is allowed to invoke via the Admin Console's **Users** tab.

**Architecture:**

```
Admin Console (Users tab)
    |
    | 1. List Cognito users (GET /users)
    | 2. List gateway tools (GET /tools)
    | 3. Load user permissions (GET /users/{userId}/permissions)
    | 4. Save permissions (PUT /users/{userId}/permissions)
    v
admin-api Lambda
    |
    +--- Cognito ListUsers + AdminListGroupsForUser
    +--- AgentCore Control: ListGatewayTargets + GetGatewayTarget (tool schemas)
    +--- DynamoDB: user-tool mappings (userId, __permissions__)
    +--- AgentCore Policy Engine: Cedar per-user permit policies
    v
AgentCore Gateway (CUSTOM_JWT auth, ENFORCE mode)
    |
    +--- Per-tool permit policies (allow specific users per tool)
    +--- Default deny (no permit = tool hidden from user)
```

**Cedar Policy Model (Permit with principal.id):**

Uses the **permit model with default-deny**: each tool gets a Cedar `permit` policy listing authorized user IDs via `principal.id` (from JWT `sub` claim). Tools without a permit policy are hidden from users by the gateway.

```cedar
permit(
  principal,
  action == AgentCore::Action::"SmartHomeDeviceControl___control_device",
  resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:...:gateway/{id}"
) when {
  ((principal is AgentCore::OAuthUser) || (principal is AgentCore::IamEntity)) &&
  ((principal.id) == "cognito-sub-uuid-1" || (principal.id) == "cognito-sub-uuid-2")
};
```

**Key requirement for per-user control:** The gateway must use `CUSTOM_JWT` auth (Cognito), and the runtime must propagate the user's JWT to the agent via `requestHeaderAllowlist: ["Authorization"]`. The agent forwards the JWT to the gateway MCP client, enabling Cedar to evaluate `principal.id` from the JWT `sub` claim.

**Cedar Schema (discovered via `StartPolicyGeneration`):**

| Entity | Format | Example |
|--------|--------|---------|
| Action | `AgentCore::Action::"{TargetName}___{toolName}"` | `AgentCore::Action::"SmartHomeDeviceControl___control_device"` |
| Resource | `AgentCore::Gateway::"{gatewayArn}"` | `AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-west-2:...:gateway/..."` |
| Principal | `AgentCore::OAuthUser` or `AgentCore::IamEntity` | `principal.id == "78d153c0-7011-704b-fb6c-4e1a80cf55ce"` (Cognito sub) |

**Permission Save Flow:**

When the admin clicks "Save Permissions" for a user:

1. Save user-tool mapping to DynamoDB (`cognitoSub → __permissions__ → allowedTools[]`)
2. For each affected tool, scan DynamoDB for ALL users who have that tool
3. Build Cedar `permit` statement with the authorized user IDs (`principal.id`)
4. Create or update the tool's Cedar policy in the policy engine
5. If no users have a tool, delete its permit policy (default-deny blocks it)
6. On first save: create policy engine, grant gateway role IAM permissions (PolicyEngineAccess), associate with gateway (ENFORCE mode)

**User Identity:**

Cedar `principal.id` maps to the JWT `sub` claim (Cognito sub UUID). The Admin Console stores permissions keyed by `user.sub` to match.

**DynamoDB Records:**

| userId | skillName | Purpose |
|--------|-----------|---------|
| `{cognito-sub}` | `__permissions__` | User's allowed tools list |
| `__system__` | `__policy_engine__` | Policy engine ID and ARN |
| `__system__` | `__tool_policy_{toolName}__` | Policy ID for each tool |

**Scalability:** One Cedar policy per tool (not per user). Each tool policy lists authorized user IDs. Cedar statement limit is 153KB (~3,800 user IDs per tool). Suitable for most deployments.

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

**Lambda Targets:**
- `control_device(device_type, command)` — Send a control command to a smart home device (via `iot-control` Lambda)
- `discover_devices()` — List available smart home devices with their types, actions, and power-on/off commands (via `iot-discovery` Lambda)

### 10.3 iot-discovery Lambda Response Format

**MCP tool response (via AgentCore Gateway):**
```json
{
  "devices": [
    {
      "thingName": "smarthome-led_matrix",
      "deviceType": "led_matrix",
      "displayName": "LED Matrix",
      "actions": ["setPower", "setMode", "setBrightness", "setColor"],
      "powerOn": {"action": "setPower", "power": true},
      "powerOff": {"action": "setPower", "power": false}
    }
  ],
  "count": 4
}
```

Currently returns a mock list of 4 devices. In production, this would query IoT Core `listThings` to return user-specific devices.

### 10.4 iot-control Lambda Response Format

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

### 10.5 Stack Outputs

**CDK Stack outputs** (consumed by `scripts/setup-agentcore.py`):

| Output Key | Description | Example |
|------------|-------------|---------|
| `IoTEndpointOutput` | AWS IoT Core data endpoint | `a1b2c3-ats.iot.us-west-2.amazonaws.com` |
| `UserPoolId` | Cognito User Pool ID | `us-west-2_AbCdEfGhI` |
| `UserPoolClientId` | Cognito App Client ID | `1a2b3c4d5e6f7g8h9i0j` |
| `IdentityPoolId` | Cognito Identity Pool ID | `us-west-2:xxxxxxxx-xxxx-...` |
| `CognitoDomain` | Cognito hosted UI domain | `smarthome-123456789.auth.us-west-2.amazoncognito.com` |
| `IoTControlLambdaArn` | Lambda ARN for device control gateway target | `arn:aws:lambda:us-west-2:...:function:smarthome-iot-control` |
| `IoTDiscoveryLambdaArn` | Lambda ARN for device discovery gateway target | `arn:aws:lambda:us-west-2:...:function:smarthome-iot-discovery` |
| `ChatbotBucketName` | S3 bucket for chatbot config.js update | `smarthome-chatbot-123456789` |
| `ChatbotDistributionId` | CloudFront ID for cache invalidation | `E1234567890` |
| `DeviceSimulatorUrl` | Device Simulator URL | `https://d1234567890.cloudfront.net` |
| `ChatbotUrl` | Chatbot URL | `https://d0987654321.cloudfront.net` |
| `AdminApiUrl` | Admin API Gateway URL | `https://abc123.execute-api.us-west-2.amazonaws.com/prod/` |
| `SkillsTableName` | DynamoDB skills table name | `smarthome-skills` |
| `SkillFilesBucketName` | S3 bucket for skill directory files | `smarthome-skill-files-123456789` |
| `AdminConsoleBucketName` | S3 bucket for admin console | `smarthome-admin-console-123456789` |
| `AdminConsoleDistributionId` | CloudFront ID for admin console | `E2345678901` |
| `AdminConsoleUrl` | Admin Console URL | `https://d1122334455.cloudfront.net` |

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

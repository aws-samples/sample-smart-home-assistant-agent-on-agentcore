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
- [9.6. Enterprise Knowledge Base](#96-enterprise-knowledge-base)
- [9.7. Voice Mode (Nova Sonic Bi-directional Streaming)](#97-voice-mode-nova-sonic-bi-directional-streaming)
- [10. API Reference](#10-api-reference)
- [11. MQTT Topic & Command Reference](#11-mqtt-topic--command-reference)
- [12. Error Handling Strategy](#12-error-handling-strategy)
- [13. Frontend Build Pipeline](#13-frontend-build-pipeline)
- [14. Technology Choices and Rationale](#14-technology-choices-and-rationale)
- [15. Scalability Considerations](#15-scalability-considerations)

---

## 1. System Overview

The Smart Home Assistant Agent is a full-stack application that demonstrates AI-driven smart home device control on AWS. It consists of six main subsystems:

| Subsystem | Technology | Purpose |
|-----------|-----------|---------|
| Device Simulator | React + TypeScript + MQTT | Visual simulation of 4 smart home devices |
| Chatbot | React + TypeScript + HTTP POST | Natural-language interface to the AI agent |
| AI Agent | Strands Agent on AgentCore Runtime (Kimi-2.5) | Understands intent and orchestrates device commands |
| Tool Access | AgentCore Gateway (MCP Server) + Lambda | Device discovery, command routing, KB query, and device control via MCP |
| Admin Console | React + TypeScript + REST API | Agent Harness Management: skills, knowledge base, models, tool access, integrations, sessions, memories, quality evaluation |
| Enterprise Knowledge Base | Bedrock KB + OpenSearch Serverless + S3 | RAG retrieval with per-user document isolation via S3 prefix + metadata filtering |
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
    |         wss://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encodedArn}/ws
    |         |
    |         +---> Strands Agent (Kimi K2.5 text path + Nova Sonic voice path)
    |         +---> Auth: AWS SigV4 (Cognito Identity Pool authenticated role)
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
|  Layer 3: AgentCore Runtime AWS_IAM (SigV4) Authorization        |
|  +------------------------------------------------------------+  |
|  | - Browser signs with Cognito Identity Pool authenticated    |  |
|  |   role credentials (service=bedrock-agentcore)              |  |
|  | - SigV4 headers on POST /invocations                        |  |
|  | - SigV4 presigned URL (X-Amz-* in query) on WebSocket /ws   |  |
|  | - Protects: /invocations, /ping, /ws endpoints              |  |
|  +------------------------------------------------------------+  |
|                                                                   |
|  Layer 4: AgentCore Gateway (CUSTOM_JWT + Policy Engine)          |
|  +------------------------------------------------------------+  |
|  | - Auth: CUSTOM_JWT (same Cognito User Pool)                 |  |
|  | - Chatbot ships idToken in custom header                    |  |
|  |   X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken         |  |
|  |   (allowlisted via requestHeaderAllowlist)                  |  |
|  | - Agent forwards it as Bearer to gateway MCP client         |  |
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

**Login prefill via query param:** `LoginPage` reads `?username=<email>` (or `?email=<email>`) from `window.location.search` on mount and prefills the username field. The Admin Console's Tool Access tab uses this to launch pre-filled chatbot demos for any Cognito user, so administrators only need to enter the password.

### 7.2 Message Architecture

The chatbot communicates with the AgentCore Runtime via HTTP POST:

```
Browser --HTTP POST--> AgentCore Runtime ---> Strands Agent (Kimi K2.5)
```

**HTTP POST Invocation:**
- Endpoint: `https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encodedArn}/invocations`
- Authentication: AWS SigV4 (service `bedrock-agentcore`), signed in the browser with temporary credentials from the Cognito Identity Pool authenticated role
- Session ID: `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: user-session-{cognito-sub}` (fixed per user; signed into the request)
- User ID: Passed in the POST body as `userId` (the runtime strips the `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` header before forwarding to the agent)
- Gateway idToken passthrough: `X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken: {cognito_idToken}` header, forwarded by the agent as `Bearer` to the CUSTOM_JWT gateway MCP client
- CORS: Fully supported (`access-control-allow-origin: *`)

**WebSocket (voice mode):** See §9.7 for the full flow. Same host, path `/ws`, signed via SigV4 presigned URL; session-id + AuthToken travel as signed query parameters because browsers can't set custom headers on a WebSocket handshake.

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

**Per-session 7-day token usage:** The Sessions tab also shows each session's total token consumption over the last 7 days. AgentCore Runtime exports Strands/ADOT spans to the CloudWatch Logs `aws/spans` log group; each `chat` span (emitted by `strands.telemetry.tracer`) carries both `attributes.session.id` and `attributes.gen_ai.usage.total_tokens`. The admin Lambda runs a CloudWatch Logs Insights query on `GET /sessions` that sums `total_tokens` grouped by `session.id` for the last 7 days and joins the result onto the DynamoDB session rows as `totalTokens7d`. The query is the backing dataset for the CloudWatch "GenAI Observability → Bedrock AgentCore → All sessions" dashboard, so the numbers match what an admin sees in that console view. Permissions: the admin Lambda gets `logs:StartQuery`/`logs:StopQuery` scoped to `log-group:aws/spans:*` plus `logs:GetQueryResults` (required at `*` since GetQueryResults doesn't support resource-level scoping).

**Stop session:** The Admin Console calls the AgentCore `StopRuntimeSession` API from the browser using the admin's AWS credentials (obtained by exchanging the Cognito idToken for Identity Pool temporary credentials — the same SigV4 flow the chatbot uses for `/invocations` and `/ws`). `scripts/setup-agentcore.py` also invokes this API as a post-deploy step to invalidate any warm sessions so users pick up fresh code immediately instead of waiting for the idle timeout.

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
- Gateway auth should be `CUSTOM_JWT` for per-user tool control. The runtime is `AWS_IAM` (SigV4) because CUSTOM_JWT on the Runtime's `/ws` endpoint is not reliable today; the chatbot forwards the idToken in the custom header `X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken` (allowlisted via `requestHeaderAllowlist` in `UpdateAgentRuntime`), and the agent re-wraps it as `Bearer` on the gateway MCP client. `Authorization` **cannot** be allowlisted under AWS_IAM — `UpdateAgentRuntime` rejects it.
- The `agentcore` CLI sets gateway URL env vars as `AGENTCORE_GATEWAY_{GATEWAYNAME}_URL` (not `AGENTCORE_GATEWAY_URL`); agent code must auto-detect the pattern
- `agentcore deploy` drops custom `environmentVariables` set in `agentcore.json` — must patch them post-deploy via `update_agent_runtime` boto3 API (requires passing `agentRuntimeArtifact`, `roleArn`, `networkConfiguration`, and `authorizerConfiguration` alongside). CLI-managed env vars like `MEMORY_<NAME>_ID` and `AGENTCORE_GATEWAY_<NAME>_URL` are preserved.
- `agentcore add memory --name <Name> --strategies SEMANTIC,SUMMARIZATION,USER_PREFERENCE` adds memory as a project resource deployed via the same CFN stack; the CLI auto-sets `MEMORY_<NAME>_ID` env var on the runtime

### 9.2 Deployment Architecture

`deploy.sh` is a thin wrapper that runs 7 split scripts under `scripts/0[1-7]-*.sh` in order. Each split script is independently runnable and prints the AWS resources it creates at the top — handy for debugging or re-running a single step after a partial failure.

```
deploy.sh (one-click wrapper)
    |
    +---> [1/7] scripts/01-install-deps.sh
    |           npm install in cdk/; bundle latest boto3 into Lambda dirs
    |           (admin-api, user-init, kb-query) for AgentCore control-plane APIs
    |
    +---> [2/7] scripts/02-build-frontends.sh
    |           Build device-simulator, chatbot, admin-console React apps
    |
    +---> [3/7] scripts/03-cdk-bootstrap.sh
    |           cdk bootstrap (idempotent; CDKToolkit stack, asset bucket, ECR)
    |
    +---> [4/7] scripts/04-cdk-deploy.sh
    |           cdk deploy --all
    |           CloudFormation: SmartHomeAssistantStack
    |           -> Cognito (User Pool, Identity Pool, admin group, admin user)
    |           -> IoT Things + endpoint
    |           -> Lambda (iot-control, iot-discovery, admin-api, kb-query, user-init)
    |           -> DynamoDB smarthome-skills
    |           -> S3 (smarthome-skill-files, smarthome-kb-docs)
    |           -> OpenSearch Serverless collection smarthome-kb
    |           -> Bedrock Knowledge Base + S3 data source (Cohere multilingual)
    |           -> API Gateway with Cognito authorizer
    |           -> S3 + CloudFront for each of the 3 frontends
    |           -> Outputs written to cdk-outputs.json
    |
    +---> [5/7] scripts/05-fix-cognito.sh
    |           aws cognito-idp update-user-pool
    |           -> AllowAdminCreateUserOnly=false (self-service sign-up)
    |           -> auto-verified-attributes=email
    |           (CDK selfSignUpEnabled doesn't always propagate reliably)
    |
    +---> [6/7] scripts/06-deploy-agentcore.sh -> scripts/setup-agentcore.py
    |           |
    |           +---> agentcore create --name smarthome --defaults
    |           +---> Replace default agent code with agent/
    |           |     Patch agentcore.json (entrypoint, JWT auth, env vars)
    |           |     Seed aws-targets.json (required for CLI deploy)
    |           +---> agentcore add memory --name SmartHomeMemory
    |           |     --strategies SEMANTIC,SUMMARIZATION,USER_PREFERENCE
    |           +---> agentcore add gateway (CUSTOM_JWT auth, Cognito)
    |           +---> agentcore add gateway-target SmartHomeDeviceControl
    |           |     (iot-control Lambda + control_device tool schema)
    |           +---> agentcore add gateway-target SmartHomeDeviceDiscovery
    |           |     (iot-discovery Lambda + discover_devices tool schema)
    |           +---> agentcore add gateway-target SmartHomeKnowledgeBase
    |           |     (kb-query Lambda + query_knowledge_base tool schema)
    |           +---> agentcore add evaluator + online-eval
    |           +---> agentcore deploy -y --verbose
    |           |     CloudFormation: AgentCore-smarthome-default
    |           |     -> Runtime, Gateway, Memory, Policy Engine, IAM Role
    |           +---> Fetch resource IDs (from CFN stack outputs)
    |           +---> Initialize KB (AOSS index, Bedrock KB, S3 data source,
    |           |     default __shared__/ + admin@/ folders)
    |           +---> Patch runtime env vars (MODEL_ID, AWS_REGION, SKILLS_TABLE_NAME)
    |           +---> Patch runtime: requestHeaderAllowlist: ["Authorization"]
    |           |     (propagate user JWT to agent for gateway auth)
    |           +---> Patch admin Lambda env vars (AGENT_RUNTIME_ARN, GATEWAY_ID,
    |           |     SKILL_FILES_BUCKET, COGNITO_USER_POOL_ID)
    |           +---> Patch user-init Lambda env vars (GATEWAY_ID, KB_DOCS_BUCKET)
    |           +---> Grant runtime role DynamoDB + Bedrock Retrieve access
    |           +---> Update config.js in S3 for device-sim / chatbot / admin console
    |           |     (admin config.js also injects chatbotUrl + deviceSimulatorUrl
    |           |      so the Tool Access tab can deep-link per-user demo flows)
    |           +---> Invalidate CloudFront cache
    |
    +---> [7/7] scripts/07-seed-skills.sh -> scripts/seed-skills.py
                +---> Read SKILL.md files from agent/skills/
                +---> Write to DynamoDB as __global__ skills (idempotent)
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
│   │   └── adminApi.ts      # REST client for admin API (skills, models, tool access, knowledge base, memories, sessions)
│   └── components/
│       └── AdminConsole.tsx  # 8-tab harness management (skills, knowledge base, models, tool access, integrations, sessions, memories, quality evaluation)
├── public/
│   ├── index.html           # HTML template with config.js loader
│   └── config.js            # Runtime config placeholder (overwritten by CDK)
├── webpack.config.js        # Webpack 5 (dev server on port 3002)
├── tsconfig.json
└── package.json
```

**Key Features:**

- **Admin role gate**: After Cognito login, decodes the JWT `cognito:groups` claim. Users not in the `admin` group see an "Access Denied" page.
- **Eight tabs** for comprehensive agent harness management:

| Tab | Purpose |
|-----|---------|
| **Skills** | Skill CRUD with all [Agent Skills spec](https://agentskills.io/specification) fields, file manager, metadata editor |
| **Knowledge Base** | Enterprise KB document management, sync, and per-user access control via Bedrock KB |
| **Models** | Global default model + per-user model override table (priority: per-user > global > env var) |
| **Tool Access** | Per-user tool permissions via Cedar policies, Policy Engine mode toggle (ENFORCE/LOG_ONLY), and **Demo Links** column with one-click deep links to the chatbot (`?username=<email>` for login prefill) and device simulator for each user — optimized for admin-led demos |
| **Integrations** | Tool source registry — Lambda targets (active), MCP servers, A2A agents, API Gateway (planned) |
| **Sessions** | Runtime session monitoring with User ID, Session ID, Last Active, and Stop button |
| **Memories** | Long-term memory viewer — per-user facts and preferences from AgentCore Memory |
| **Quality Evaluation** | Links to AgentCore Evaluator, Bedrock Guardrails consoles, and Cedar Policy Engine settings |

**CDK Resources:**

| Resource | Description |
|----------|-------------|
| `smarthome-admin-console-{accountId}` S3 Bucket | Static assets |
| `smarthome-skill-files-{accountId}` S3 Bucket | Skill directory files (scripts, references, assets) with CORS |
| `smarthome-kb-docs-{accountId}` S3 Bucket | Knowledge base documents organized by scope prefix (`__shared__/`, `user@email/`) |
| `smarthome-kb` OpenSearch Serverless Collection | Vector store for KB document embeddings (VECTORSEARCH type) |
| Bedrock Knowledge Base (`SmartHomeEnterpriseKB`) | Semantic retrieval with `cohere.embed-multilingual-v3` embedding model |
| `smarthome-kb-query` Lambda | Gateway target for agent KB retrieval with JWT-based user identity extraction |
| CloudFront Distribution | HTTPS CDN |
| `config.js` (written by setup script) | Injects `adminApiUrl`, `agentRuntimeArn`, `cognitoUserPoolId`, `cognitoClientId`, `region`, `chatbotUrl`, `deviceSimulatorUrl` |

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

**Key requirement for per-user control:** The gateway must use `CUSTOM_JWT` auth (Cognito). The runtime is `AWS_IAM` (see §9.7 rationale) so the user's idToken cannot travel as a literal `Authorization` header; instead the chatbot sends it in the custom allowlisted header `X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken` and the agent re-wraps it as `Bearer` on the gateway MCP client, enabling Cedar to evaluate `principal.id` from the JWT `sub` claim.

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

### 9.6 Enterprise Knowledge Base

The enterprise knowledge base provides RAG (Retrieval-Augmented Generation) capabilities, allowing the AI agent to answer questions based on uploaded company documents.

**Architecture:**

```
Admin Console                          Agent (Chatbot)
     │                                       │
     │ Upload/Delete/Sync                    │ query_knowledge_base(query)
     │                                       │
     v                                       v
┌─────────────┐                    ┌──────────────────┐
│  Admin API  │                    │ AgentCore Gateway │ ← validates JWT
│  Lambda     │                    │  (MCP)            │
└──────┬──────┘                    └────────┬─────────┘
       │                                    │
       │ S3 PutObject                       │ Lambda invoke
       │ + metadata sidecar                 │
       v                                    v
┌──────────────────┐              ┌──────────────────┐
│  S3 Bucket       │              │  kb-query Lambda  │ ← extracts email from JWT
│  (kb-docs)       │              │                   │
│  __shared__/     │  Ingestion   │  bedrock:Retrieve │
│  user@email/     │◄────────────►│  + metadata filter│
└──────────────────┘              └──────────────────┘
       │                                    │
       │ StartIngestionJob                  │ filter: scope=__shared__ OR scope=user_email
       v                                    v
┌──────────────────┐              ┌──────────────────┐
│  Bedrock KB      │              │  OpenSearch       │
│  (SmartHome      │──────────────│  Serverless       │
│   EnterpriseKB)  │  knn_vector  │  (smarthome-kb)   │
└──────────────────┘              └──────────────────┘
       │
       │ cohere.embed-multilingual-v3
       │ (1024 dimensions, Chinese/English)
```

**Per-User Document Isolation:**

Documents are organized by S3 prefix, with metadata sidecar files enabling query-time filtering:

```
smarthome-kb-docs-{accountId}/
├── __shared__/                              # Shared documents (all users)
│   ├── product-guide.pdf
│   └── product-guide.pdf.metadata.json      # {"metadataAttributes": {"scope": "__shared__"}}
├── alice@example.com/                       # Alice's private documents
│   ├── notes.pdf
│   └── notes.pdf.metadata.json              # {"metadataAttributes": {"scope": "alice@example.com"}}
└── bob@example.com/                         # Bob's private documents
    └── ...
```

When the agent queries the KB, the `kb-query` Lambda applies a metadata filter:
```python
filter = {
    "orAll": [
        {"equals": {"key": "scope", "value": "__shared__"}},
        {"equals": {"key": "scope", "value": user_email}},  # from JWT
    ]
}
```

**Security — Secure Tool Wrapper (LLM-Proof Identity Injection):**

The LLM never controls the `user_id` parameter. Instead, the agent code replaces the MCP `query_knowledge_base` tool with a **local wrapper** that auto-injects the user identity:

```python
# In agent.py invoke_agent():
# 1. Filter out the MCP KB tool (which has user_id in its schema)
non_kb_tools = [t for t in mcp_tools if t.tool_name != "query_knowledge_base"]

# 2. Create a local wrapper — LLM only sees "query" parameter
@tool
def query_knowledge_base(query: str) -> str:
    # user_id injected from verified actor_id, NOT from LLM
    result = mcp_client.call_tool_sync("query_knowledge_base",
        {"query": query, "user_id": actor_id})  # actor_id from JWT → Runtime
    return result

# 3. Agent uses the wrapper instead of the MCP tool
agent = create_agent(tools=non_kb_tools + [query_knowledge_base], ...)
```

Identity chain: **Cognito JWT → AgentCore Runtime (verified `userId`) → `actor_id` (Python variable) → tool wrapper closure → `user_id` MCP parameter → kb-query Lambda → metadata filter**. The LLM cannot fabricate, omit, or alter the identity. If identity is unavailable, only `__shared__` documents are returned (safe default). The Gateway's CUSTOM_JWT + Cedar policy provides an additional layer ensuring only authenticated users can invoke the tool.

**DynamoDB Schema:**

| userId | skillName | Purpose |
|--------|-----------|---------|
| `__kb_config__` | `__default__` | KB ID, data source ID, creation timestamps |

**CDK Resources:**

| Resource | Purpose |
|----------|---------|
| `smarthome-kb-docs-{accountId}` S3 Bucket | Document storage with CORS for presigned URL uploads |
| `smarthome-kb` AOSS Collection (VECTORSEARCH) | Vector embeddings store |
| AOSS Encryption/Network/Data Access Policies | Collection security |
| `KBServiceRole` IAM Role | Bedrock KB service role (S3 read + AOSS access + Bedrock InvokeModel) |
| `smarthome-kb-query` Lambda | MCP tool target for agent KB retrieval |

**Setup Script Initialization:**

The `setup-agentcore.py` script handles one-time KB setup:
1. Adds deployer's IAM identity to AOSS data access policy
2. Creates AOSS vector index (`smarthome-kb-index`) via `opensearch-py`
3. Creates Bedrock Knowledge Base with AOSS storage configuration
4. Creates S3 data source pointing to the KB docs bucket
5. Stores KB config in DynamoDB
6. Creates default S3 folders (`__shared__/`, `admin@smarthome.local/`)
7. Registers `kb-query` Lambda as an AgentCore Gateway target
8. Post-deploy: patches Gateway target with inline tool schema (includes `user_id` parameter) to bypass S3 schema caching

**Admin API Endpoints (consolidated under `GET/POST /knowledge-bases`):**

| Method | Action | Purpose |
|--------|--------|---------|
| `GET` | `status` | KB status, scopes, document counts |
| `GET` | `documents` | List documents in a scope (filters out `.metadata.json`) |
| `GET` | `sync-status` | Latest ingestion job statuses |
| `POST` | `upload-url` | Generate presigned PUT URL + create metadata sidecar |
| `POST` | `delete` | Delete document + metadata sidecar |
| `POST` | `sync` | Start Bedrock KB ingestion job |

### 9.7 Voice Mode (Nova Sonic Bi-directional Streaming)

The chatbot exposes a voice mode that streams the user's microphone audio to
Amazon Nova Sonic (`amazon.nova-2-sonic-v1:0`) via the AgentCore Runtime's
`/ws` endpoint and plays the model's reply audio back in the browser.

**Runtime authorizer: AWS_IAM (SigV4).** The CUSTOM_JWT path on `/ws` is
broken at the runtime's edge (handshake rejected with HTTP 424; confirmed
identical container accepts locally). SigV4 works reliably, so the browser
signs both `/invocations` and `/ws` with temporary credentials obtained by
exchanging the user's Cognito idToken for the authenticated Identity Pool
role. The text path uses the same signing flow as voice; there is no longer
a separate JWT Bearer code path.

**Architecture:**

```
Browser (chatbot, logged-in user)
    |  1. Cognito User Pool login -> idToken
    |  2. fromCognitoIdentityPool({ logins: { <userPool>: idToken } })
    |     -> temporary AWS creds (authenticated role)
    |
    |  3. Warmup ping: POST /invocations {"prompt":"__warmup__"}   [SigV4]
    |
    |  4. Voice toggle on: getUserMedia -> AudioWorklet (16 kHz Int16 PCM)
    |     presign wss:// URL with SigV4 (5 min TTL)
    |     browser WebSocket(url) — credentials in query string
    v
AgentCore Runtime /ws  (AWS_IAM)
    v
BedrockAgentCoreApp (Starlette-based, uvicorn+websockets)
    @app.entrypoint  -> handle_invocation(payload, context)
    @app.websocket   -> voice_session.handle_voice_session(ws, context)
         1. Wait for {"type":"config", ...}
         2. Open MCPClient to the AgentCore Gateway with the user's JWT
            forwarded as Bearer (Cedar policy evaluation).
         3. List MCP tools, wrap them in a local composite tool
            (`turn_on_all_devices`, see below), inline one skill body.
         4. Create Strands BidiAgent(tools=[...], system_prompt=...,
            model=BidiNovaSonicModel).
         5. agent.run(inputs=[receive_from_ws], outputs=[send_to_ws])
            - inbound `bidi_audio_input` frames stream into Nova Sonic
            - outbound `bidi_audio_stream` PCM frames stream back to the browser
            - `tool_use_stream` fires Strands' tool dispatcher, which calls
              the MCP gateway; `tool_result` goes back into the model's turn
    v
Nova Sonic (amazon.nova-2-sonic-v1:0) bi-directional streaming
    v
MCP Gateway → iot-control Lambda → IoT Core (`smarthome/<device>/command`)
    v
Device Simulator (browser) receives MQTT messages, updates UI state
```

**Per-user gateway auth under AWS_IAM.** `requestHeaderAllowlist: ["Authorization"]`
is rejected by `UpdateAgentRuntime` when the runtime uses `AWS_IAM`, so the
chatbot ships the idToken in a **custom allowlisted header**
`X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken` (also passable as a WS
query parameter per the AgentCore contract). The agent reads it from
`context.request_headers`, re-wraps as `Authorization: Bearer <token>`, and
forwards to the CUSTOM_JWT-authed gateway MCP client — exact same Cedar
evaluation as before.

**IAM.** The CDK's Cognito authenticated role (`CognitoAuthRole`) gets, via
`setup-agentcore.py`, a scoped inline policy:

```
bedrock-agentcore:InvokeAgentRuntime
bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream
  Resource: arn:aws:bedrock-agentcore:<region>:<acct>:runtime/<id>*
```

The `/*` wildcard covers endpoint qualifiers (e.g. `/DEFAULT`). No other
runtime in the account is reachable by the authenticated role.

**Audio formats:**

| Direction | Format | Sample Rate | Source |
|-----------|--------|-------------|--------|
| Welcome clip | MP3 | 22050 Hz (Polly `neural`) | Pre-rendered by Polly (`Zhiyu` / `cmn-CN`) during `setup-agentcore.py` step 2, written to `agent/welcome-zh.mp3` **before** `agentcore deploy` so the CLI bakes it into the CodeZip. Agent loads into RAM at module import (`_WELCOME_BYTES`) so the first WS session has the bytes ready with no S3 round-trip. |
| Client → server | Int16 PCM, mono | 16 kHz | Browser `AudioWorkletNode` downsamples the native device rate |
| Server → client | Int16 PCM, mono | 16 kHz | Nova Sonic output stream |

**WebSocket message protocol (JSON text frames):**

Client → server (Strands BidiAgent native event types):
```json
{"type": "config", "voice": "matthew", "input_sample_rate": 16000,
 "output_sample_rate": 16000, "model_id": "amazon.nova-2-sonic-v1:0"}
{"type": "bidi_audio_input", "audio": "<base64 Int16 PCM>",
 "format": "pcm", "sample_rate": 16000, "channels": 1}
{"type": "bidi_text_input", "text": "..."}            // optional, text injection
```

Server → client (Strands BidiAgent emits these verbatim):
```json
{"type": "system", "message": "Agent ready."}
{"type": "bidi_connection_start", "connection_id": "...", "model": "..."}
{"type": "bidi_audio_stream", "audio": "<base64 pcm>",
 "format": "pcm", "sample_rate": 16000, "channels": 1}
{"type": "bidi_transcript_stream", "role": "user"|"assistant",
 "text": "...", "is_final": true}          // SPECULATIVE|FINAL per generationStage
{"type": "tool_use_stream", "delta": {"toolUse": {"toolUseId": "...",
 "name": "SmartHomeDeviceDiscovery___discover_devices", "input": {}}}}
{"type": "tool_result", "tool_result": {"toolUseId": "...",
 "status": "success", "content": [{"text": "<JSON result>"}]}}
{"type": "bidi_usage", "inputTokens": N, "outputTokens": N, "totalTokens": N}
{"type": "bidi_response_start", "response_id": "..."}
{"type": "bidi_response_complete"}
{"type": "bidi_interruption"}
{"type": "bidi_error", "message": "..."}
```

Plus a chatbot-specific welcome event (still a valid `bidi_audio_stream`,
tagged so the browser decodes the payload as MP3 instead of PCM):
```json
{"type": "bidi_audio_stream", "is_welcome": true, "format": "mp3",
 "seq": N, "total": M, "audio": "<base64 mp3 chunk>"}
```

**Welcome clip delivery — streamed through the BidiAgent pipeline.**

Direct `websocket.send_json(...)` calls from `voice_session.py` issued
**before** `agent.run(...)` starts reliably reach the browser for small
event payloads (`system`, `error`) but get dropped by the Runtime's WS
proxy for audio-sized JSON payloads — observed on several test runs with
chunks as small as 3 KB. Frames authored by Nova Sonic's internal pipeline
(`bidi_audio_stream`, `bidi_usage`, …) all pass through normally.

The welcome clip rides the same pipeline that works:

1. `setup-agentcore.py` pre-renders the Polly MP3 into `agent/welcome-zh.mp3`.
2. `agentcore deploy` packages the MP3 into the CodeZip (no S3 round-trip).
3. `voice_session.py` loads `_WELCOME_BYTES` once at module import.
4. On each WS connection, after `_wait_for_config` + `Agent ready.`,
   `_welcome_stream(websocket)` is started as a concurrent `asyncio.Task`
   alongside `agent.run(...)`. It chunks the base64 MP3 at 3 KB (aligned to
   4-char base64 boundaries) and sends each chunk as a
   `{"type":"bidi_audio_stream","is_welcome":true,"seq":N,"total":M,"audio":...}`
   frame with a 20 ms pace between frames. Reusing the `bidi_audio_stream`
   type makes the proxy treat the frames as legitimate model output; the
   `is_welcome` flag plus `format:"mp3"` tells the browser to decode as MP3
   (vs the PCM from Nova Sonic).
5. Browser `VoiceClient.handleServerMessage` collects `is_welcome` chunks by
   `seq`, concatenates once `total` chunks have arrived, decodes via
   `AudioContext.decodeAudioData`, and plays via `scheduleBuffer`.

Trade-off accepted: we depend on the Runtime's WS proxy continuing to pass
`bidi_audio_stream` frames. A future breaking change in the proxy's
filtering would need us to re-evaluate. The code is commented with this
reasoning so future maintainers understand why the event type is reused.

**Session auto-invalidation on redeploy.** AgentCore Runtime keeps a session
container warm for several minutes of idle time. After a `agentcore deploy`
the fresh CodeZip only reaches new sessions — existing sessions keep
running the old code until they time out. `setup-agentcore.py` closes this
gap automatically: after patching the runtime it scans DynamoDB's
`__session__` records (written by the agent on every invocation via
`_record_session`) and calls `bedrock-agentcore:StopRuntimeSession` on each.
Missing/expired sessions return `ResourceNotFoundException` which we ignore
as already-stopped. The number of invalidated sessions is printed as
`Stopped N active runtime session(s) so the fresh CodeZip takes effect`.

**Setup-agentcore additions:**

- Renders the welcome clip via Polly (Chinese neural voice `Zhiyu`) into `agent/welcome-zh.mp3` *before* the `agentcore deploy` call so the CLI packages the bytes into the CodeZip.
- Patches the runtime environment with `NOVA_SONIC_MODEL_ID` (`amazon.nova-2-sonic-v1:0`) and `AGENTCORE_GATEWAY_ARN`.
- Sets `protocolConfiguration.serverProtocol = "HTTP"` (required for `/ws` routing).
- Clears `authorizerConfiguration` (→ AWS_IAM) and sets `requestHeaderConfiguration.requestHeaderAllowlist = ["X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken"]` so the agent can read the forwarded idToken.
- Grants the runtime role `bedrock:InvokeModelWithBidirectionalStream` (no extra S3 permission needed — the welcome MP3 lives in the CodeZip).
- Grants the Cognito authenticated role `bedrock-agentcore:InvokeAgentRuntime` + `InvokeAgentRuntimeWithWebSocketStream` scoped to `arn:aws:bedrock-agentcore:<region>:<acct>:runtime/<id>*`.
- Stops all known runtime sessions (scanned from DynamoDB) so the fresh CodeZip is picked up immediately.

**MCP tool wiring (why `tools=[...]` is mandatory).** The Strands text `Agent`
uses the `AgentSkills` plugin and scans a directory of MCP tools implicitly.
`BidiAgent` does **not** expose a `plugins=` parameter, and our pinned
`BidiNovaSonicModel` build does not consume `mcp_gateway_arn=` — that kwarg
goes into `**kwargs` and is silently ignored. Without an explicit `tools=`
list, Nova Sonic receives an empty `toolConfiguration` and happily
hallucinates device lists from its training data. `voice_session.py` opens
an `MCPClient` against the gateway with the user's JWT, enumerates tools via
`list_tools_sync`, and passes them straight into
`BidiAgent(tools=[...])`. The names arrive prefixed by the gateway target
(`SmartHomeDeviceDiscovery___discover_devices`, etc.) — we reference those
exact names in the system prompt so Nova Sonic emits matching `toolUse`
events.

**Skills inlining.** `BidiAgent` can't register `AgentSkills` either, so
`voice_session.py` calls the text path's `load_skills_from_dynamodb(actor_id)`
helper and inlines the **operational** skill (`all-devices-on`) into the
system prompt. Inlining all five SKILL.md bodies blew the prompt up past the
threshold where Nova Sonic starts ignoring tools, so only the multi-step
orchestration skill makes the cut. Single-device commands are covered by
the base prompt's tool-name schema. Tool-name references inside the skill
markdown (`discover_devices`, `control_device`) are rewritten to their
MCP-prefixed forms on the way in via `_rewrite_tool_names`.

**Nova Sonic single-tool-per-turn limitation.** Nova Sonic's voice model
terminates a conversation turn after it speaks the tool result — unlike
text LLMs (Kimi/Claude/etc.) it does **not** auto-chain follow-up tool
calls within the same turn. In practice: saying "turn on all devices"
makes Nova Sonic call `discover_devices` and stop. Telling it via the
system prompt to "loop over each device and call `control_device`" does
not work — by the time the skill instruction would apply, the turn is
already over.

The fix is a server-side composite tool `turn_on_all_devices` defined in
`voice_session.py._build_turn_on_all_tool`. It's a plain `@tool`-decorated
Python function that uses the still-live `MCPClient` to:

1. Call `SmartHomeDeviceDiscovery___discover_devices` synchronously.
2. Parse the JSON device list (shape tolerance in `_extract_devices`).
3. For each device, call `SmartHomeDeviceControl___control_device` with the
   `powerOn` template from discovery.
4. Return a one-sentence summary (`"Turned on 4 devices: LED Matrix, Rice
   Cooker, Fan, Oven."`) — Nova Sonic reads it and speaks it.

From Nova Sonic's perspective this is exactly one tool call per turn. Per-
user Cedar policy still applies because all calls go through the same MCP
client (user's JWT as Bearer). If more multi-step flows are needed later
(e.g. "start a dinner scene"), wrap them the same way.

**Transcript dedupe (SPECULATIVE vs FINAL).** Nova Sonic emits every
transcript twice in its `bidi_transcript_stream`: `is_final=false`
(SPECULATIVE — interim text) then `is_final=true` (FINAL — refined text).
If the UI appends both the user sees each utterance duplicated. The
chatbot's `ChatInterface.tsx` tracks a pending message ID per role in a
`useRef`; the first arriving transcript creates a new bubble, subsequent
transcripts for the same role (until the final) replace that bubble in
place, and the final clears the pending slot so the next utterance starts
fresh. Refs are reset on `stopVoice()`.

**Defensive event serialiser.** BidiAgent can emit non-JSON-serialisable
objects through the `outputs=[send_output]` callback — most notably
`BidiModelTimeoutError` when Nova Sonic stalls. `websocket.send_json` raises
`TypeError` on those, and the original code just logged a warning, so model
timeouts silently vanished from the UI. The revised `send_output` first
tries `send_json`; on `TypeError` it falls back to `json.dumps(..., default=str)`
+ `send_text`; as a last resort it emits a synthetic `{"type":"bidi_error",
"message":"unserialisable <class>..."}` frame so the browser at least
surfaces the problem in the red status banner.

**Per-user skills.** Voice skills are loaded the same way as text-path
skills: `load_skills_from_dynamodb(actor_id)` returns global + per-user
overrides. `actor_id` is the email extracted from the forwarded idToken's
`email`/`cognito:username`/`sub` claim (decoded inline in
`voice_session.py` so we don't need to share state with the text path).

**Agent framework: BedrockAgentCoreApp (not FastAPI).** The Starlette-based
`BedrockAgentCoreApp` handles the runtime's protocol contract natively. The
`@app.websocket` decorator registers `/ws`; `@app.entrypoint` keeps
`/invocations`. Both paths share the same ADOT wrapper. Plain FastAPI was
tested on this branch and does not integrate cleanly with the managed
runtime's health/protocol handshake — an earlier commit on this branch
pivoted back to `BedrockAgentCoreApp`.

**Login warmup.** Immediately after successful Cognito login the chatbot
fires a SigV4-signed POST `/invocations` with `prompt="__warmup__"`. The
agent short-circuits this to `{"status":"warmup_ok"}` without invoking the
LLM, spinning up the runtime container so the user's first real message
doesn't pay the cold-start penalty.

---

## 10. API Reference

### 10.1 AgentCore Runtime Endpoints

The AgentCore Runtime exposes three endpoints on port 8080:

#### POST /invocations

Invoke the Strands Agent and get a complete response.

**Authorization:** AWS SigV4 (service `bedrock-agentcore`). The chatbot signs with temporary credentials obtained by exchanging the Cognito User Pool idToken for the Identity Pool authenticated role. The idToken is forwarded to the gateway via a custom allowlisted header `X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken` so per-user Cedar policies still apply.

**Request Body:**
```json
{
  "prompt": "Turn on the LED matrix to rainbow mode",
  "userId": "user@example.com"
}
```

**Response:**
```json
{
  "response": "I've set the LED matrix to rainbow mode. The colorful animation should now be visible on your LED panel.",
  "status": "success"
}
```

Special: `{"prompt": "__warmup__"}` returns `{"status": "warmup_ok"}` without invoking the LLM. The chatbot fires this immediately after login to pre-warm the runtime container.

#### GET /ws — WebSocket (voice mode)

**Authorization:** AWS SigV4 presigned URL. See §9.7 for the full protocol and event types. Voice sessions require the caller's IAM principal to hold `bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream` on the runtime ARN (the Cognito authenticated role is granted this by `setup-agentcore.py`).

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
| **AgentCore Runtime** for agent hosting | Managed hosting with built-in WebSocket support, SigV4/IAM + OAuth auth options, and session management (chatbot uses AWS_IAM so both `/invocations` and `/ws` work through SigV4) |
| **Amazon Nova Sonic** (`amazon.nova-2-sonic-v1:0`) for voice | Bi-directional speech-to-speech model with Strands `BidiAgent` integration and MCP tool calling in-session |
| **Cognito Identity Pool (authenticated role)** for chatbot | Exchanges the User Pool idToken for temporary AWS creds so the browser can SigV4-sign Runtime calls; the unauthenticated role keeps its IoT-only scope for the device simulator |
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

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
- [8.10. Agent System Prompts (Text & Voice)](#810-agent-system-prompts-text--voice)
- [8.11. Image Input (Vision Bypass Path)](#811-image-input-vision-bypass-path)
- [9. Infrastructure Design](#9-infrastructure-design)
- [9.4. Admin Console Design](#94-admin-console-design)
- [9.5. Per-User Tool Permission Management](#95-per-user-tool-permission-management)
- [9.6. Enterprise Knowledge Base](#96-enterprise-knowledge-base)
- [9.7. Voice Mode (Nova Sonic Bi-directional Streaming)](#97-voice-mode-nova-sonic-bi-directional-streaming)
- [9.8. Skill ERP & AgentCore Registry](#98-skill-erp--agentcore-registry)
- [9.9. Integration Registry & A2A Agents](#99-integration-registry--a2a-agents)
- [9.10. Remote Shell Commands per Session](#910-remote-shell-commands-per-session)
- [9.11. Browser Use — Live Agent Web Automation](#911-browser-use--live-agent-web-automation)
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
| Device Simulator | React + TypeScript + Cloudscape + MQTT + Cognito | Per-user authenticated visual simulation of 4 smart home devices; each user sees only their own scope via `smarthome/<userSub>/...` topics |
| Chatbot | React + TypeScript + Cloudscape + HTTP POST | Natural-language interface to the AI agent. Fully Cloudscape-native UI (including bubbles/input), renders agent replies as markdown, supports image attachments (≤3 images, ≤20 MB each) that route to a vision model |
| AI Agent (text) | Strands Agent on AgentCore Runtime `smarthome` (Kimi-2.5 default; per-user Bedrock model override) | Text chat via `POST /invocations`; wraps per-user MCP tools (control_device, discover_devices, query_knowledge_base) to inject the validated `user_id`; image turns bypass the text model and return the vision model's caption |
| AI Agent (voice) | Strands BidiAgent on AgentCore Runtime `smarthomevoice` (Nova Sonic) | Bi-directional voice streaming via `/ws`; finalized transcripts persisted to the same AgentCore Memory the text agent uses |
| AI Agent (vision) | Claude Haiku 4.5 default (per-user multimodal model override) via Bedrock Converse | Captions uploaded images; captions injected as prior assistant messages so the text agent can answer follow-up questions |
| AI Agent (browser) | `browser-use` + AgentCore Browser Tool (`aws.browser.v1`) driven by the text agent's `browse_web` Strands tool | Live web automation when the user asks something that needs a real site (product search, news, Wikipedia). Chatbot renders the live DCV stream + a Take/Release Control button; per-step screenshots land in the agent session's `/mnt/workspace/<sid>/browser/` (see §9.11). |
| Tool Access | AgentCore Gateway (MCP Server) + Lambda + curated Strands built-ins | Device discovery, command routing, KB query, and device control via MCP. Built-in Strands/AgentCore tools (`http_request`, `file_write`, etc.) also surfaced for admin per-user policy and for reference skills. |
| Admin Console | React + TypeScript + Cloudscape + REST API | Agent Harness Management with AWS-Console-style left-nav: Discover (Overview, Integration Registry), Build (Models, Skills, Prompt, Tool Policy, Memories, Knowledge Base, Identity), Deploy (Instance Type, Sessions), Assess (Agent Guardrails, Observability, Evaluations). Supports light/dark themes. |
| Skill ERP | React + TypeScript + Cloudscape + REST API | End-user skill + A2A agent publishing: authors SKILL.md and A2A records, publishes to AgentCore Registry for curator approval |
| Enterprise Knowledge Base | Bedrock KB + **S3 Vectors** + S3 | RAG retrieval with per-user document isolation via S3 prefix + metadata filtering. Vector store is the pay-per-vector S3 Vectors service (no fixed monthly floor). |
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
User (Chatbot, logged in — idToken carries Cognito sub)
    |
    | "Turn on the LED matrix to rainbow mode"
    v
AgentCore Runtime (HTTP POST /invocations) --> Strands Agent (Kimi K2.5)
                                       |
                                       | Agent wraps control_device / discover_devices so the
                                       | JWT-validated `sub` is injected as `user_id` before
                                       | the MCP call — LLM cannot forge this argument.
                                       |
                                       | MCP Client call (with user_id)
                                       v
                                AgentCore Gateway (MCP Server)
                                       |
                                       | Lambda Target
                                       v
                                iot-control Lambda
                                       |
                                       | Refuses requests without user_id.
                                       | iot-data:Publish scoped to the caller's sub.
                                       v
                                AWS IoT Core
                                Topic: smarthome/<userSub>/led_matrix/command
                                       |
                                       | MQTT over WebSocket (SigV4)
                                       v
                                Device Simulator (Browser, same signed-in user)
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
    | 1. Cognito User Pool sign-in -> idToken (JWT with `sub`)
    v
LoginPage (Cloudscape)
    |
    | 2. Attach `smarthome-device-sim-client` IoT policy to this identity
    |    (workaround: IoT refuses SigV4 MQTT over WS without an attached IoT policy)
    v
Cognito Identity Pool (authenticated, federated via User Pool login)
    |
    | 3. Temporary AWS credentials (authenticated role)
    v
MqttClient.ts
    |
    | 4. MQTT5 over WebSocket with SigV4
    |    ClientId: "device-sim-{random}"
    v
AWS IoT Core
    |
    | 5. Subscribe (scoped to the signed-in user's own sub) to:
    |      smarthome/<userSub>/led_matrix/command
    |      smarthome/<userSub>/rice_cooker/command
    |      smarthome/<userSub>/fan/command
    |      smarthome/<userSub>/oven/command
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

The device simulator runs entirely in the browser. Users sign in with the shared Cognito User Pool (same one the chatbot uses) and the resulting idToken federates authenticated Identity Pool credentials for SigV4 MQTT.

```
Browser
  |
  +-> LoginPage (Cognito User Pool: signIn / signUp / confirm)
  |     -> idToken (JWT with `sub` = user UUID)
  |
  +-> AttachPolicy(smarthome-device-sim-client) to the caller's Cognito identity
  |     (workaround: IoT refuses SigV4 WS connects without an attached IoT policy,
  |      even when IAM already allows iot:Connect)
  |
  +-> fromCognitoIdentityPool({ logins: { 'cognito-idp...': idToken } })
  |     -> authenticated temporary AWS credentials
  |
  +-> Mqtt5Client (aws-iot-device-sdk-v2)
        |-> WebSocket SigV4 auth
        |-> clientId: "device-sim-{random}"
        |-> keepAlive: 30s
        |-> auto-reconnect with re-subscribe, refresh every 45min
        |-> subscribes to:  smarthome/<userSub>/{led_matrix,rice_cooker,fan,oven}/command
```

**Note on the browser SDK:** The aws-iot-device-sdk-v2 has different APIs for Node.js and browser bundles. The browser build exports `auth.StaticCredentialProvider` (not `auth.AwsCredentialsProvider.newStatic()`). TypeScript uses `(auth as any).StaticCredentialProvider` since the Node.js type definitions don't include the browser API.

**Key design decisions:**
- **Authenticated access required**: The device simulator gates on Cognito sign-in. The unauthenticated Identity Pool role exists as a CDK remnant but the new client never uses it.
- **Per-user MQTT topics**: All subscriptions and publishes use `smarthome/<userSub>/<device>/command`. Each user's browser sees only their own simulated devices.
- **IoT AttachPolicy at login**: `ensureIotPolicyAttached()` runs on sign-in before the MQTT client starts. It attaches the `smarthome-device-sim-client` IoT policy (CDK-managed) to the user's Cognito identity ID. This is AWS's documented workaround for authenticated Cognito users getting `Connection refused: Not authorized` when trying to use MQTT over WebSockets.
- **Singleton MQTT client**: All 4 device components share one `MqttClient` instance to avoid multiple WebSocket connections.
- **All devices off by default**: Every device starts in the powered-off state when the page loads.
- **Auto-power-on**: Setting a mode, speed, temperature, or color via MQTT automatically powers on the device (e.g., `setMode` on LED Matrix also sets `power: true`), so the agent doesn't need to send a separate `setPower` command.
- **Layout**: LED Matrix occupies the left column; Rice Cooker, Fan, and Oven stack compactly on the right.

#### Security model & known limitation

Per-user isolation has two layers:
- **Agent → Lambda path (strict)**: the `iot-control` Lambda publishes only on `smarthome/<user_id>/...`. The `user_id` comes from the agent's wrapper, which decodes it from the runtime-validated idToken — the LLM cannot forge it.
- **Browser → IoT Core (best-effort)**: the authenticated Cognito IAM role currently allows IoT operations on `*` because mapping Identity Pool identity IDs back to User Pool subs is nontrivial. A determined user could subscribe to another user's topics with raw MQTT; closing that gap would require an IoT Core Policy keyed on a Cognito custom claim and is deferred as future hardening.

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
  cognitoIdentityPoolId: "us-east-1:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  // Added when the device simulator adopted Cognito User Pool sign-in:
  cognitoUserPoolId: "us-east-1_XXXXXXXXX",
  cognitoClientId: "abcdef1234567890"
};
```

This approach avoids baking environment-specific values into the webpack bundle, enabling the same build to work across environments. The CDK stack's `BucketDeployment` for the device-sim bundle uses `prune: false` so the `AwsCustomResource`-written `config.js` survives every deploy.

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
                                           \-> Vision model (Claude Haiku 4.5)  [when images present]
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

**Client -> Server (POST) — text only:**
```json
{"prompt": "Turn on the LED to rainbow mode", "userId": "user@example.com"}
```

**Client -> Server (POST) — with image attachments:**
```json
{
  "prompt": "describe this dashboard",
  "userId": "user@example.com",
  "images": [
    {"mediaType": "image/png",  "data": "<base64>"},
    {"mediaType": "image/jpeg", "data": "<base64>"}
  ]
}
```

- `images` is optional. When present it must be a list of ≤3 objects; oversize or wrong-type items are rejected client-side before send (≤20 MB raw per image; `image/png|jpeg|webp|gif`).
- When `images` is present the payload is handled by the agent's vision bypass path (see §8.11); Kimi is not called.

**Server -> Client (Response):**
```json
{"response": "I'll set the LED matrix to rainbow mode. The command has been sent!", "status": "success"}
```

The response shape is the same for text and image turns; for image turns the body is the vision model's description (plus any `Note: …` warnings from partial failures).

#### UI Pattern

The `ChatInterface` has a paperclip button next to the send button that opens a multi-select file picker (up to 3 images, ≤20 MB each, `image/png|jpeg|webp|gif`). Selected images render as 48×48 thumbnails above the textarea, each with a × to remove. On send, files are base64-encoded in parallel and included in the POST body's `images` array; the user bubble also renders the thumbnails alongside the text. Typing indicator and response rendering are identical to text-only turns.

To the right of the chat column the `BrowserPanel` surfaces live agent
browser sessions. It defaults to a 40-px-wide vertical rail ("Browser"
+ "Files" labels); clicking either expands the panel to 720 px on that
tab. While the agent is typing the chatbot polls
`/sessions?action=browser-active` every 1.5 s; once a session is running
it renders the DCV live-view stream, a Take/Release Control toggle, and
a Files tab that walks the text-agent runtime's `/mnt/workspace/<sid>/`
via direct `InvokeAgentRuntimeCommand` SDK calls. A maximize button in
the panel header flips to `flex: 1` so wide pages (Amazon, Wikipedia)
render without horizontal clipping. See §9.11 for the full design.

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

#### Tool-argument wrapping for per-user scoping

Some backend tools need the caller's identity (KB retrieval, device control, device discovery). AgentCore Gateway's `client_context.custom` does **not** forward the caller's JWT claims to Lambda targets — it only passes Gateway metadata (`bedrockAgentCoreToolName`, target/gateway IDs). So the agent wraps these MCP tools with a thin Strands `@tool` on the client side:

1. The agent decodes the user's `sub` from the runtime-validated idToken once per invocation.
2. For `query_knowledge_base`, `control_device`, and `discover_devices`, it replaces the raw MCP tool with a wrapper of the same short name. The wrapper calls `mcp_client.call_tool_sync(name=<prefixed MCP tool name>)` and injects `user_id=<sub>` into the arguments.
3. The LLM sees only the short name and the non-identity arguments. Whatever it tries to pass as `user_id` is overwritten by the wrapper before the Lambda call. Prompt-injection attacks can't escalate to another user's scope.
4. The Lambda refuses any request that arrives without a `user_id` argument, so requests that bypass the wrapper fail fast.

Tool suffixes to wrap are matched against both the bare name and the `<TargetName>___<suffix>` Gateway-prefixed form.

#### Strands built-in tools

In addition to MCP tools surfaced by the Gateway, the agent registers a small set of Strands `strands_tools` built-ins:

- `http_request` — used by the `weather-lookup` skill (Open-Meteo geocode + forecast).
- `file_write` — used by the `user-feedback` skill to persist records under `/mnt/workspace/feedback/`.

Loaded lazily so the runtime still boots if `strands-agents-tools` is absent. Built-ins default to interactive consent prompts that have no place in a hosted runtime, so `BYPASS_TOOL_CONSENT=true` is set as a runtime environment variable. `agent_core_memory` is **not** auto-registered — it is a provider-style tool (`AgentCoreMemoryToolProvider`) that needs per-session instantiation, and the agent's session manager already persists turns to Memory.

### 8.4 Agent Directory Structure

```
agent/
├── agent.py              # BedrockAgentCoreApp with @app.entrypoint handler
├── vision.py             # Claude Haiku 4.5 captioning via Bedrock Converse
│                          # (used for image-turn bypass; see §8.11)
├── session_storage.py    # Per-session filesystem helpers at /mnt/workspace
│                          # (atomic image writes + index.jsonl catalog)
├── memory/
│   ├── __init__.py
│   └── session.py        # AgentCoreMemorySessionManager factory (follows agentcore CLI pattern)
├── tools/
│   ├── device_control.py # Fallback tool for local dev (Lambda invocation via boto3)
│   └── browser_use.py    # `browse_web` Strands tool — drives AgentCore Browser Tool
│                          # via `browser-use` + DCV (see §9.11)
├── skills/
│   ├── led-control/      # SKILL.md with LED-specific instructions
│   ├── rice-cooker-control/
│   ├── fan-control/
│   ├── oven-control/
│   ├── all-devices-on/   # Discovers devices then turns them on sequentially
│   ├── weather-lookup/   # Open-Meteo geocoder + forecast via http_request (reference skill)
│   ├── user-feedback/    # Persists JSON records under /mnt/workspace/feedback/ via file_write (reference skill)
│   └── browser-use/      # Auto-routes live-web queries to `browse_web` (see §9.11)
├── tests/                # pytest unit tests (excluded from CodeZip deploy)
├── pyproject.toml        # Dependencies for AgentCore CodeZip packaging
└── Dockerfile            # Optional, for local container testing
```

The `tests/` directory is intentionally excluded from the CodeZip packaging — `scripts/setup-agentcore.py` filters it out (along with `__pycache__`/`*.pyc`) so the production container never ships test-only imports.

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
                    |  9a. Load system prompt from    |
                    |      DynamoDB (per-user +      |
                    |      __global__ rows, joined    |
                    |      with "\n\n"; §8.10) —     |
                    |      fall back to hardcoded    |
                    |      SYSTEM_PROMPT if both     |
                    |      rows are empty.           |
                    |  10. Create Strands Agent with: |
                    |      - BedrockModel (Kimi K2.5) |
                    |      - MCP tools                |
                    |      - Skills plugin            |
                    |      - Session manager          |
                    |      - Resolved system_prompt   |
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
- The system prompt is also loaded per invocation (global + per-user rows joined with `"\n\n"`), so admin edits take effect on the next request without a runtime redeploy — see [§8.10](#810-agent-system-prompts-text--voice)

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
| `GET` | `/skills?userId=` | List skills for a user scope. With `&promptBundle=1` returns the Agent Prompt bundle (text + voice) instead — see [§8.10](#810-agent-system-prompts-text--voice). |
| `POST` | `/skills` | Create a new skill (all spec fields) |
| `GET` | `/skills/{userId}/{skillName}` | Get a single skill (or prompt record when `skillName` matches `__prompt_text__` / `__prompt_voice__`) |
| `PUT` | `/skills/{userId}/{skillName}` | Update a skill, or upsert a prompt override when `skillName` starts with `__prompt_` |
| `DELETE` | `/skills/{userId}/{skillName}` | Delete a skill (cascade-deletes S3 files), or remove a prompt override when `skillName` starts with `__prompt_` |
| `GET` | `/skills/users` | List distinct user scopes |
| `GET` | `/skills/{userId}/{skillName}/files` | List files in a skill directory |
| `POST` | `/skills/{userId}/{skillName}/files/upload-url` | Get presigned S3 upload URL |
| `POST` | `/skills/{userId}/{skillName}/files/download-url` | Get presigned S3 download URL |
| `DELETE` | `/skills/{userId}/{skillName}/files?path=` | Delete a skill file |
| `GET` | `/settings/{userId}` | Get user settings (`modelId` + `visionModelId`) |
| `PUT` | `/settings/{userId}` | Update user settings (either field, independently patchable) |
| `GET` | `/sessions` | List all user runtime sessions |
| `GET` | `/users` | List all Cognito users with groups |
| `GET` | `/tools` | List all gateway tools (reads tool schemas from targets) |
| `GET` | `/users/{userId}/permissions` | Get user's allowed tools |
| `PUT` | `/users/{userId}/permissions` | Update allowed tools + sync Cedar policies |
| `GET` | `/memories` | List all memory actors |
| `GET` | `/memories/{actorId}` | Get long-term memory records (facts + preferences) for an actor |

**Authorization:** All admin API endpoints require a valid Cognito JWT. The Lambda additionally checks that the caller belongs to the `admin` Cognito group (returns 403 if not).

### 8.8 Per-User Model Selection

Administrators can assign different LLM models to each user (or set a global default) via the Admin Console for **both** the text agent and the vision agent. Settings are stored in DynamoDB using a reserved key `skillName = "__settings__"`.

**Storage format:**
```
PK: userId (e.g., "zihangh@amazon.com" or "__global__")
SK: "__settings__"
modelId: "us.anthropic.claude-sonnet-4-6"          # text agent model
visionModelId: "us.anthropic.claude-haiku-4-5-..."   # vision (multimodal) model
```

`PUT /settings/{userId}` accepts either field independently — absent fields retain their existing values so callers can patch one without clobbering the other.

**Resolution order (text):** user-specific `modelId` > `__global__` `modelId` > `MODEL_ID` env var (default: `moonshotai.kimi-k2.5`).

**Resolution order (vision):** user-specific `visionModelId` > `__global__` `visionModelId` > `VISION_MODEL_ID` env var (default: `us.anthropic.claude-haiku-4-5-20251001-v1:0`). The agent reads this via `load_user_settings(actor_id)` and threads it into `vision.caption_images(..., model_id=...)`.

**Available text models** (Admin Console dropdown):
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

**Available vision models** (separate dropdown — only multimodal models that accept images in Bedrock Converse):
- Claude: Haiku 4.5, Sonnet 4.5/4.6, Opus 4.5/4.6, 3.7 Sonnet, 3.5 Haiku
- Nova: Pro, Lite
- Qwen: Qwen3 VL 235B A22B

### 8.9 Fixed Session ID and Session Tracking

Each user gets a **fixed runtime session ID** derived from their Cognito `sub` (UUID). This means the same user always uses the same session across invocations, enabling session persistence.

**Session ID format:** `user-session-{cognito-sub}` (set by the chatbot via the `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header).

**User identification:** The AgentCore Runtime strips the `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` header before forwarding to the agent process. To work around this, the chatbot passes the user's email in the POST body as `userId`, and the agent reads it from `payload.get("userId")`.

**Session tracking:** On each invocation, the agent records `{userId, sessionId, lastActiveAt}` to DynamoDB. After the voice-runtime split (see [§9.7](#97-voice-mode-nova-sonic-bi-directional-streaming)) the records are split by sort key: `skillName = "__session_text__"` for the text runtime and `"__session_voice__"` for the voice runtime. The Admin Console's Sessions tab reads both keys and renders a `Kind` column so each row can be stopped against the right runtime ARN (admin-api `POST /sessions/{id}/stop?kind=text|voice`).

**Per-session 7-day token usage:** The Sessions tab also shows each session's total token consumption over the last 7 days. AgentCore Runtime exports Strands/ADOT spans to the CloudWatch Logs `aws/spans` log group; each `chat` span (emitted by `strands.telemetry.tracer`) carries both `attributes.session.id` and `attributes.gen_ai.usage.total_tokens`. The admin Lambda runs a CloudWatch Logs Insights query on `GET /sessions` that sums `total_tokens` grouped by `session.id` for the last 7 days and joins the result onto the DynamoDB session rows as `totalTokens7d`. The query is the backing dataset for the CloudWatch "GenAI Observability → Bedrock AgentCore → All sessions" dashboard, so the numbers match what an admin sees in that console view. Permissions: the admin Lambda gets `logs:StartQuery`/`logs:StopQuery` scoped to `log-group:aws/spans:*` plus `logs:GetQueryResults` (required at `*` since GetQueryResults doesn't support resource-level scoping).

**Stop session:** The Admin Console calls the AgentCore `StopRuntimeSession` API from the browser using the admin's AWS credentials (obtained by exchanging the Cognito idToken for Identity Pool temporary credentials — the same SigV4 flow the chatbot uses for `/invocations` and `/ws`). `scripts/setup-agentcore.py` also invokes this API as a post-deploy step to invalidate any warm sessions so users pick up fresh code immediately instead of waiting for the idle timeout.

### 8.10 Agent System Prompts (Text & Voice)

Administrators can override the system prompts used by both the **text agent** (Strands on HTTP `/invocations`) and the **voice agent** (BidiAgent on WebSocket `/ws`) from the Admin Console's **Agent Prompt** tab, without redeploying the runtime image.

**Two independent records, additive at runtime.** The global prompt and per-user prompt are stored as two separate DynamoDB rows and the agent concatenates them at invocation time:

```
effective_system_prompt = global_body + "\n\n" + user_body
```

Any empty part is omitted from the join. When both rows are empty, the agent falls back to the hardcoded `SYSTEM_PROMPT` / `VOICE_SYSTEM_PROMPT` constant shipped in the container image. This keeps shared guardrails in one editable place (Global) while letting per-user personalization be a short addendum rather than a full duplicate.

**Storage format** (reuses the existing `smarthome-skills` table):

| userId | skillName | Fields |
|--------|-----------|--------|
| `__global__` or `{cognito-email}` | `__prompt_text__` | `promptBody`, `updatedAt`, `updatedBy` |
| `__global__` or `{cognito-email}` | `__prompt_voice__` | `promptBody`, `updatedAt`, `updatedBy` |

`updatedBy` captures the admin's email from the Cognito JWT on each save, surfaced as the "Last edited by …" line in the UI.

**Agent runtime resolution** (`agent/agent.py:load_system_prompt`):

1. Read `(userId=actor_id, __prompt_{type}__)` — returns `""` if row missing.
2. Read `(userId=__global__, __prompt_{type}__)` — returns `""` if row missing.
3. Concatenate non-empty parts with `"\n\n"`. If both empty, return `None` → caller uses hardcoded constant.

The text agent calls this inside `invoke_agent()` on every request; the voice agent calls it inside `handle_voice_session()` after decoding the JWT `actor_id`. Both paths silently fall back to the hardcoded constant on DynamoDB errors (`logger.warning` + default) — a broken prompt-load must never break the invocation.

**Why the voice prompt has to stay editable separately.** The voice prompt isn't a stripped-down copy of the text one — it uses the MCP-gateway-prefixed tool names (e.g., `SmartHomeDeviceDiscovery___discover_devices`) that Nova Sonic needs to emit in `toolUse` events, plus voice-specific tone rules ("one short spoken sentence, no Markdown"). Forcing admins to edit them together would make it easy to break voice by assuming the text prompt's shorthand tool names still work.

**Admin UI.** At Global scope the tab shows two side-by-side editors (Text / Voice). At per-user scope each editor splits into three rows: a read-only Global base view, an editable per-user addendum, and a collapsible "Effective Prompt Preview" that concatenates them the same way the agent does. Badges indicate the state (`Built-in Default`, `Custom Global`, `No User Override`, `User Override`), and the reset button is labelled `Revert to Default` at global scope or `Remove Override` at per-user scope, so the semantic difference between "wipe everyone's custom prompt" and "drop this user's addendum" is never ambiguous.

**Transport (no new API Gateway methods).** The admin API carries prompts on the **existing** `/skills` routes to stay under the admin Lambda's 20 KB resource-policy cap. The CDK passes `allowTestInvoke: false` to the admin `LambdaIntegration` which suppresses AWS's per-method `/test-invoke-stage/*` permission (halving the policy — 20 KB → ~10 KB — so new routes and `browse_web`-related dispatch fit comfortably). Even with that headroom, the prompt API piggybacks on `/skills` and the browser-session-poll API piggybacks on `/sessions` via `?action=` dispatch because adding methods is still expensive and a fresh cold deploy would otherwise be within a few hundred bytes of the cap. Requests are dispatched to prompt handlers when `skillName` starts with the reserved `__prompt_` prefix:

| Method | Path | Behavior when skillName matches `__prompt_*__` |
|--------|------|------------------------------------------------|
| `GET` | `/skills?userId={scope}&promptBundle=1` | Returns `{text: PromptRecord, voice: PromptRecord, userId}` where each record = `{body, updatedAt, updatedBy, isOverride, globalBody, builtinDefault}`. `globalBody` is always populated (at user scope it's the read-only context; at global scope it equals `body`). |
| `PUT` | `/skills/{userId}/__prompt_text__` | Upsert the text prompt override; rejects empty or >16 KB bodies. |
| `PUT` | `/skills/{userId}/__prompt_voice__` | Same, for voice. |
| `DELETE` | `/skills/{userId}/__prompt_{type}__` | Remove the override; agent falls back to the next resolution level on the next invocation. |

**Defaults mirror.** The Lambda ships a local `agent_prompt_defaults.py` that duplicates the two hardcoded constants from `agent/agent.py` and `agent/voice_session.py`. The duplication is intentional: the admin Lambda and the agent runtime live in separate packages, and making the tab render "what the agent will use when no override exists" without a round-trip to the runtime is worth the copy. The module carries a short comment flagging that the constants must be updated in the same commit as the agent-side source of truth.

**Evo integration stub.** Each editor card renders a disabled "Optimization Suggestions (AgentCore Evo)" block. AgentCore Evo does not yet exist in this codebase; the card is a visual placeholder so future work can wire up an optimization endpoint without a tab-layout change.

**Image-awareness clause (text prompt).** The hardcoded text `SYSTEM_PROMPT` contains an `IMAGES IN THIS CONVERSATION:` section that tells Kimi image descriptions are injected into the conversation history as prior assistant messages (written by the vision bypass path in §8.11). Without this clause Kimi would respond to follow-up questions about past uploads by denying image access or fabricating contents. Admins editing the global prompt must keep this section when overriding, or follow-up turns referring to earlier images will regress.

---

### 8.11 Image Input (Vision Bypass Path)

Image turns use a dedicated path that bypasses Kimi entirely: the runtime calls a vision model (Claude Haiku 4.5) to caption the uploaded images and returns the description straight to the chatbot. This keeps latency low (one model call instead of two) and keeps Kimi's tool-calling / KB / skills loop focused on text.

```
POST /invocations
  {"prompt": "...", "userId": "...",
   "images": [{"mediaType": "image/png", "data": "<base64>"}]}
              │
              ▼
  handle_invocation (agent/agent.py)
              │
              ├── payload.images present? ── no ──► invoke_agent() → Kimi K2.5
              │                                    (normal text path)
              │
              └── yes: vision bypass
                     │
                     ├─ validate: isinstance(list) && len ≤ 3 → 400 on violation
                     │
                     ├─ 1. PERSIST raw bytes to /mnt/workspace/<sid>/uploads/images/
                     │    via session_storage.save_image (atomic tempfile + os.replace,
                     │    dedup by sha256, append index.jsonl entry). Non-fatal on error.
                     │
                     ├─ 2. vision.caption_images(images, prompt)
                     │       → bedrock-runtime.converse on VISION_MODEL_ID
                     │         (default us.anthropic.claude-haiku-4-5-20251001-v1:0;
                     │          env-overridable, e.g. us.amazon.nova-lite-v1:0).
                     │       → Per-image validation (MIME allowlist, ≤20 MB after decode)
                     │         is defense-in-depth mirroring the client cap.
                     │       → Partial-success: rejected images emit a "Note: …" warning;
                     │         ≥1 valid image → the call proceeds.
                     │       → One retry on Throttling/ServiceUnavailable; on final
                     │         failure returns a placeholder + service-unavailable note.
                     │
                     ├─ 3. PERSIST exchange to AgentCore Memory (short-term event):
                     │    (user_prompt, USER) + (haiku_description, ASSISTANT), each
                     │    wrapped in the Strands SessionMessage envelope so Kimi's
                     │    session manager can deserialize them on the next text turn.
                     │    Metadata carries a fingerprint per image (mime/bytes/sha16)
                     │    and image_N_path pointing to the session-storage file.
                     │
                     └─ 4. Return {"response": caption_text + warnings, "status": "success"}
```

**Why bypass Kimi.** Measured in `vision-latency-test/` (30 rounds × 2 models × 3 image counts), the bypass path lands at p50 ≈ 3.3 s (Haiku, 1 image, cold) / 2.0 s (Nova Lite, 1 image, cold). Routing the same turn through Kimi after pre-captioning would roughly double that (two LLM hops plus an extra token-pricing cost on Kimi for the long description).

**Model swap.** `VISION_MODEL_ID` is captured at module-load (so each container version is pinned to one vision model) and resolved via Bedrock `Converse` for whichever multimodal model the operator prefers. Tested paths: Claude Haiku 4.5 (default — stronger OCR / fine-detail) and Amazon Nova Lite (~1.7× faster, lower cost, lighter detail). Swapping is an `update_agent_runtime` env-var patch — no source-code change and no `agentcore deploy`. The update rolls a new container version; subsequent sessions start on the new model.

**Memory envelope.** Because the Strands `AgentCoreMemoryConverter` writes and reads events as JSON-serialized `SessionMessage` dicts, the vision bypass must mirror that format exactly. Plain-text events cause `JSONDecodeError` in `list_messages()` and silently drop the entire short-term history for the session, leaving Kimi with no memory of uploaded images. `agent/agent.py:_persist_vision_turn` serializes each message as `{"message": {"role": "...", "content": [{"text": "..."}]}, "message_id": N, "redact_message": null, "created_at": "...", "updated_at": "..."}` so later text turns round-trip cleanly.

**Per-session filesystem.** The runtime is configured with `filesystemConfigurations=[{sessionStorage: {mountPath: "/mnt/workspace"}}]` (set by `scripts/setup-agentcore.py`). Every `runtimeSessionId` gets an isolated writable volume at `/mnt/workspace/<session_id>/` that survives across invocations within the same session. The layout:

```
/mnt/workspace/<session_id>/
└── uploads/
    └── images/
        ├── <iso-timestamp>__<sha256[:16]>.<ext>   # raw bytes, atomic writes
        └── index.jsonl                            # append-only catalog
```

`index.jsonl` rows: `{ts, sha256, mime, bytes, path, user_prompt, caption_event_id?}`. The directory is reserved for the image subsystem today; the top-level `uploads/` namespace is intentional so future modalities (audio, documents) can coexist without churn. The volume is automatically torn down when the session is closed by AgentCore — no TTL sweep needed.

**Client-side limits (defense mirrored server-side):**

| Limit | Value | Rationale |
|-------|-------|-----------|
| Max images per turn | 3 | Fits AgentCore metadata cap (1 fingerprint + 1 path per image + `image_count` = 7 of 15 KV slots) |
| Max bytes per image | 20 MB raw | Fits well inside the 100 MB runtime payload cap even at the 3-image × base64 bloat worst case |
| MIME allowlist | `png \| jpeg \| webp \| gif` | Matches Bedrock Converse `image/format` support |

**Error surfaces:**

| Failure | Behavior |
|---------|----------|
| Client: >3 files / bad MIME / too large | Inline error under thumbnail strip; not sent |
| Server: `images` not a list or >3 items | 400 `{"error": "Invalid images payload (max 3)."}` |
| Server: per-image bad MIME or decoded >20 MB | Soft-reject in `caption_images`, warning lists indices, remaining images still captioned |
| Vision model throttled / unavailable after one retry | Placeholder caption + `Note: vision service was unavailable…`, user still gets a reply |
| Memory write fails after successful caption | Caption returned to user anyway; next turn simply won't see this one in memory (logged at WARN) |

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
- **`requestHeaderConfiguration` round-trip pitfall.** `get_agent_runtime` returns the allowlist as the top-level field `requestHeaderAllowlist`, but `update_agent_runtime` expects it nested under `requestHeaderConfiguration={"requestHeaderAllowlist": [...]}`. A naïve round-trip that re-passes the top-level value **silently drops the allowlist**, which strips the custom auth header at the edge proxy → the agent sees `context.request_headers = None` → MCP gateway returns 401. Any helper that calls `UpdateAgentRuntime` (latency-probe nonce bumper, `enable-welcome.py`, session redeploy helpers) must read from either location and always wrap back into the nested form. See `voice-latency-test/force-cold.py` + `enable-welcome.py` for the correct pattern.
- `agentcore add memory --name <Name> --strategies SEMANTIC,SUMMARIZATION,USER_PREFERENCE` adds memory as a project resource deployed via the same CFN stack; the CLI auto-sets `MEMORY_<NAME>_ID` env var on the runtime

### 9.2 Deployment Architecture

`deploy.sh` is a thin wrapper that runs 7 split scripts under `scripts/0[1-7]-*.sh` in order. Each split script is independently runnable and prints the AWS resources it creates at the top — handy for debugging or re-running a single step after a partial failure.

```
deploy.sh (one-click wrapper)
    |
    +---> [1/7] scripts/01-install-deps.sh
    |           npm install in cdk/; upgrade boto3 in the venv (Registry API needs
    |           >= 1.42.93); bundle latest boto3 into Lambda dirs (admin-api,
    |           user-init, kb-query, skill-erp-api) for AgentCore control-plane APIs
    |
    +---> [2/7] scripts/02-build-frontends.sh
    |           Build device-simulator, chatbot, admin-console, skill-erp React apps
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
    |           (S3 Vector bucket + index created later in step 6 by setup-agentcore.py)
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
    |           +---> Initialize KB (S3 Vector bucket + index, Bedrock KB, S3 data source,
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

The admin console is an independent React + TypeScript frontend app for managing the agent harness. It uses **Cloudscape Design System** (the same component library AWS uses for the AWS Console) with light/dark theme support; the main layout is Cloudscape `AppLayout` + `TopNavigation` + `SideNavigation`. Deployment follows the same pattern as the device simulator and chatbot (S3 + CloudFront + `config.js` injection).

**Directory Structure:**

```
admin-console/
├── src/
│   ├── index.tsx            # React DOM entry point; imports Cloudscape global styles + applies initial theme
│   ├── App.tsx              # TopNavigation + AppLayout + SideNavigation shell, auth + role gate, theme toggle
│   ├── App.css              # Minimal residual styles (most styling comes from Cloudscape tokens)
│   ├── config.ts            # Runtime config (adminApiUrl, Cognito IDs)
│   ├── theme/
│   │   └── applyTheme.ts    # applyMode wrapper + localStorage persistence (key: admin.theme)
│   ├── auth/
│   │   ├── CognitoAuth.ts   # Sign in, session management, admin role check
│   │   └── LoginPage.tsx    # Cloudscape Form / Input / Button login page
│   ├── api/
│   │   ├── adminApi.ts      # REST client for admin API
│   │   └── sanitizeActor.ts # Mirror of agent-side _sanitize_actor_id; resolves Memory actor IDs back to user emails
│   └── components/
│       ├── AdminConsole.tsx # Thin router — holds activeTab, shared error/success banners
│       ├── ShellModal.tsx   # Remote-shell modal (Cloudscape Modal frame; ANSI terminal body stays custom)
│       └── AnsiOutput.tsx   # ANSI color stream renderer used by ShellModal
├── public/
│   ├── index.html           # HTML template with config.js loader
│   └── config.js            # Runtime config placeholder (overwritten by CDK)
├── webpack.config.js        # Webpack 5 (dev server on port 3002)
├── tsconfig.json
└── package.json
```

**Key Features:**

- **Admin role gate**: After Cognito login, decodes the JWT `cognito:groups` claim. Users not in the `admin` group see a Cloudscape `Alert` "Access Denied" page.
- **AWS-Console-style side navigation** with four collapsible sections (Discover / Build / Deploy / Assess) plus a Docs link. This replaces the previous top tab-bar layout and matches the AWS Console's IA.
- **Light/Dark theme toggle** in the top-right, persisted to `localStorage` under `admin.theme`. Initial paint honors `prefers-color-scheme` on first visit.
- **Fourteen pages** organised under those four sections:

| Section | Page | Purpose |
|---|---|---|
| Discover | **Overview** | Intro card + architecture-diagram placeholder |
| Discover | **Integration Registry** | Sub-tabs: Overview (Lambda targets / MCP servers / API Gateway / A2A agents status table) and **A2A Agents** (lists approved A2A records from AgentCore Registry with publisher info; details modal shows the full agent card). MCP / API Gateway sub-tabs are "Coming soon" placeholders. See §9.9. |
| Build | **Models** | Global default model + per-user model override table for both text agent (`modelId`) and vision agent (`visionModelId`); resolution priority: per-user > global > env var |
| Build | **Skills** | Skill CRUD with all [Agent Skills spec](https://agentskills.io/specification) fields, file manager, metadata editor, and **"Add approved skill from AgentCore Registry"** import flow |
| Build | **Prompt** | Edit the text-agent and voice-agent system prompts per user or globally; agent runtime concatenates global + per-user addendum (see [§8.10](#810-agent-system-prompts-text--voice)) |
| Build | **Tool Policy** | Per-user tool permissions. Lists built-in Strands/AgentCore tools (default-allowed) and Gateway-scanned tools (opt-in) side-by-side with Cloudscape `Badge`s tagging the source. Cedar policy enforcement with ENFORCE/LOG_ONLY toggle. |
| Build | **Memories** | Long-term memory viewer — per-user facts and preferences from AgentCore Memory. Actor IDs are resolved back to the user's email via the sanitizer mirror. |
| Build | **Knowledge Base** | Enterprise KB document management, sync, and per-user access control via Bedrock KB |
| Build | **Identity** | Registered-users table (Cognito User Pool) |
| Deploy | **Instance Type** | Compute class configuration (MicroVM today, EC2 planned) |
| Deploy | **Sessions** | Runtime session monitoring with User ID, Kind (Text/Voice), Session ID, Last Active, Total Tokens (7d), **Remote Shell** button, and Stop button. Kind column distinguishes text-runtime vs voice-runtime sessions — clicking Stop passes `?kind=text\|voice` so the correct runtime ARN is targeted, and the DynamoDB record is deleted on success so the stopped row clears immediately. Remote Shell opens a modal that streams shell commands into the runtime container via `InvokeAgentRuntimeCommand` (see §9.10), including a row of example-command chips for common ops. |
| Assess | **Agent Guardrails** | Links to AgentCore Evaluator + Bedrock Guardrails consoles |
| Assess | **Observability** | Link to CloudWatch Gen-AI Observability |
| Assess | **Evaluations** | Link to AgentCore Evaluations console |
| (external) | **Docs** | Link to the public repo |

**CDK Resources:**

| Resource | Description |
|----------|-------------|
| `smarthome-admin-console-{accountId}` S3 Bucket | Static assets |
| `smarthome-skill-files-{accountId}` S3 Bucket | Skill directory files (scripts, references, assets) with CORS |
| `smarthome-kb-docs-{accountId}` S3 Bucket | Knowledge base documents organized by scope prefix (`__shared__/`, `user@email/`) |
| `smarthome-kb-vectors-{accountId}` S3 Vector bucket + `smarthome-kb-index` | Vector store for KB document embeddings (S3 Vectors, created imperatively by `setup-agentcore.py` — no CDK L1 construct exists yet) |
| Bedrock Knowledge Base (`SmartHomeEnterpriseKB`) | Semantic retrieval with `cohere.embed-multilingual-v3` embedding model, `storageConfiguration.type=S3_VECTORS` |
| `smarthome-kb-query` Lambda | Gateway target for agent KB retrieval with JWT-based user identity extraction |
| CloudFront Distribution | HTTPS CDN |
| `config.js` (written by setup script) | Injects `adminApiUrl`, `agentRuntimeArn`, `voiceAgentRuntimeArn`, `cognitoUserPoolId`, `cognitoClientId`, `cognitoIdentityPoolId`, `region`, `chatbotUrl`, `deviceSimulatorUrl`, `skillErpUrl` |

### 9.5 Per-User Tool Permission Management

Administrators can control which tools each user is allowed to invoke via the Admin Console's **Tool Policy** tab (previously labelled "Tool Access"; same page, renamed in the side-nav).

Two sources of tools are listed side-by-side, each tagged with a Cloudscape `Badge`:

- **Built-in** — Strands SDK / AgentCore tools that ship with the runtime (`calculator`, `current_time`, `http_request`, `think`, `sleep`, `handoff_to_user`, `retrieve`, `file_read`, `agent_core_memory`, `agent_core_browser`, `agent_core_code_interpreter`). Default-allowed for every user when no explicit permission record exists.
- **Gateway** — tools discovered from AgentCore Gateway targets (`control_device`, `discover_devices`, `query_knowledge_base`, etc.). Opt-in per user.

`GET /tools` returns both sets in one response, each item tagged with `source: "builtin" | "gateway"`. The UI renders a Badge per tool so admins can distinguish runtime-local surface from gateway-routed surface.

**Architecture:**

```
Admin Console (Tool Policy tab)
    |
    | 1. List Cognito users (GET /users)
    | 2. List built-in + gateway tools (GET /tools)
    | 3. Load user permissions (GET /users/{userId}/permissions)
    | 4. Save permissions (PUT /users/{userId}/permissions)
    v
admin-api Lambda
    |
    +--- Cognito ListUsers + AdminListGroupsForUser
    +--- Curated Strands/AgentCore built-in tools list (defaults allowed)
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
┌──────────────────┐              ┌──────────────────────┐
│  Bedrock KB      │              │  S3 Vectors          │
│  (SmartHome      │──────────────│  bucket:             │
│   EnterpriseKB)  │  PutVectors  │   smarthome-kb-      │
│  storage:        │  QueryVectors│   vectors-{acct}     │
│  S3_VECTORS      │              │  index:              │
└──────────────────┘              │   smarthome-kb-index │
       │                          │   (1024 dims, cosine)│
       │                          └──────────────────────┘
       │ cohere.embed-multilingual-v3
       │ (1024 dimensions, Chinese/English)
```

**Vector store: S3 Vectors** (serverless, pay-per-vector).
The KB was previously backed by OpenSearch Serverless, which carried a ~$350/month floor.
AWS's S3 Vectors service stores float32 vectors directly in a dedicated S3 bucket
type, exposing `PutVectors` / `QueryVectors` APIs that Bedrock calls on behalf
of the KB during ingestion and retrieval. There is no fixed cost — billing is
per vector stored and per query.

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
| `KBServiceRole` IAM Role | Bedrock KB service role (S3 read + `s3vectors:*` + Bedrock InvokeModel) |
| `smarthome-kb-query` Lambda | MCP tool target for agent KB retrieval |

The S3 Vector bucket + index are **not** CDK resources — no L1 construct
exists for `s3vectors` yet. They are created imperatively by
`setup-agentcore.py` (idempotent `create_vector_bucket` + `create_index`).

**Setup Script Initialization:**

The `setup-agentcore.py` script handles one-time KB setup:
1. Creates S3 Vector bucket `smarthome-kb-vectors-{accountId}` via
   `s3vectors.create_vector_bucket` (idempotent).
2. Creates the vector index `smarthome-kb-index` (dimension 1024 for
   Cohere multilingual v3, `distanceMetric=cosine`, `dataType=float32`).
3. Creates Bedrock Knowledge Base with `storageConfiguration.type=S3_VECTORS`
   and `s3VectorsConfiguration.indexArn` pointing at the index above. If a
   KB named `SmartHomeEnterpriseKB` already exists under a different storage
   type (e.g. a prior AOSS-backed deploy), the script flips the data source's
   `dataDeletionPolicy` to `RETAIN`, deletes the old KB, waits for removal,
   then recreates it on S3 Vectors.
4. Creates S3 data source pointing to the KB docs bucket.
5. Stores KB config in DynamoDB.
6. Creates default S3 folders (`__shared__/`, `admin@smarthome.local/`).
7. Registers `kb-query` Lambda as an AgentCore Gateway target.
8. Post-deploy: patches Gateway target with inline tool schema (includes
   `user_id` parameter) to bypass S3 schema caching.

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
Amazon Nova Sonic (`amazon.nova-2-sonic-v1:0`) via a dedicated AgentCore Runtime's
`/ws` endpoint and plays the model's reply audio back in the browser.

**Two runtimes, one agent codebase.** Voice runs on `smarthomevoice`, a second
AgentCore Runtime dedicated to the `/ws` path; text chat continues on the
original `smarthome` runtime's `/invocations`. Both runtimes package the same
`agent/` directory but use different entrypoints (`agent.py` for text,
`voice_agent.py` for voice) and share DynamoDB helpers via a plain module
import. Motivation, tradeoffs, and per-runtime configuration matrix live in
`docs/superpowers/specs/2026-04-23-voice-agent-split-design.md` — the short
version is: the login-time warmup ping now reliably heats the `strands.bidi`
import chain on the voice container, and the two runtimes can scale / redeploy
independently.

**ADOT on text only.** The voice runtime sets `DISABLE_ADOT=1` in its
environment, which gates the `agent.py` module-level
`sitecustomize.initialize()` + `StrandsTelemetry()` calls. Voice sessions are
long streams where per-event spans offer little triage value; skipping ADOT
saves 100-300ms of voice cold-start cost.

**Session key split.** The text runtime writes `skillName="__session_text__"`
in DynamoDB; the voice path writes `__session_voice__`. Redeploy-time session
invalidation (`setup-agentcore.py`) and admin stop-session both use the key
to pick the correct runtime ARN.

**Login warmup fans out to both runtimes.** The chatbot fetches Cognito /
Identity Pool creds once, then issues two parallel SigV4-signed
`POST /invocations {"prompt":"__warmup__"}` requests — one per runtime ARN.
Each runtime short-circuits the warmup without invoking its LLM, so the cost
is ~50ms per request but heats the Python process (imports, boto3 clients).
`voice_agent.py` additionally does module-level eager imports of
`strands.experimental.bidi.*` and `MCPClient` so the warmup request itself
triggers the heavy import chain.

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

**Welcome clip delivery — env-gated, default OFF.**

The welcome clip ("欢迎使用智能家居设备助手", Polly `Zhiyu`) can mask the
BidiAgent initialization latency from users, but it also masks it from us
when measuring real startup time. It is therefore disabled by default and
re-enabled by setting `VOICE_WELCOME_ENABLED=1` on the voice runtime.

The chatbot's `connection-banner` already provides the "is it alive?"
affordance (Connecting / Connected), so the audio greeting is UX-redundant
in most conditions.

When enabled, delivery uses the MP3 packaged in the CodeZip:

1. `setup-agentcore.py` pre-renders the Polly MP3 into `agent/welcome-zh.mp3`.
2. `agentcore deploy` packages the MP3 into the CodeZip (no S3 round-trip).
3. `voice_session.py` loads `_WELCOME_BYTES` at module import only when
   `VOICE_WELCOME_ENABLED=1`.
4. On each WS connection, after `_wait_for_config`, `_welcome_stream` runs
   concurrently with `agent.run(...)`. It chunks the base64 MP3 at 3 KB
   (aligned to 4-char base64 boundaries) and sends each chunk as a
   `{"type":"bidi_audio_stream","is_welcome":true,"seq":N,"total":M,"audio":...}`
   frame with a 20 ms pace. Reusing the `bidi_audio_stream` type makes the
   runtime's WS proxy treat the frames as legitimate model output — direct
   `websocket.send_json` for audio-sized payloads was observed to be dropped
   by the proxy prior to `agent.run` starting its pumps. The `is_welcome`
   flag plus `format:"mp3"` tells the browser to decode as MP3.
5. Browser `VoiceClient.handleServerMessage` collects `is_welcome` chunks by
   `seq`, concatenates once `total` chunks have arrived, decodes via
   `AudioContext.decodeAudioData`, and plays via `scheduleBuffer`.

Trade-off: we depend on the runtime's WS proxy continuing to pass
`bidi_audio_stream` frames unchanged. A future proxy change could force us
to re-evaluate the workaround.

**Session auto-invalidation on redeploy.** AgentCore Runtime keeps a session
container warm for several minutes of idle time. After a `agentcore deploy`
the fresh CodeZip only reaches new sessions — existing sessions keep
running the old code until they time out. `setup-agentcore.py` closes this
gap automatically: after patching each runtime it scans DynamoDB's session
records and calls `bedrock-agentcore:StopRuntimeSession` on each. Sessions
are tracked under two sort keys after the voice split:
`__session_text__` (written by `agent._record_session`) and
`__session_voice__` (written by `voice_session._record_voice_session`). The
script scans each key separately and routes the stop call to the matching
runtime ARN — calling `StopRuntimeSession` on the wrong ARN returns
`ResourceNotFoundException`. Admin-API's `POST /sessions/{id}/stop?kind=…`
uses the same key → ARN mapping (see [§9.4](#94-admin-console-design)).

**Setup-agentcore additions:**

- Renders the welcome clip via Polly (Chinese neural voice `Zhiyu`) into `agent/welcome-zh.mp3` *before* the `agentcore deploy` call so the CLI packages the bytes into the CodeZip.
- Patches the runtime environment with `NOVA_SONIC_MODEL_ID` (`amazon.nova-2-sonic-v1:0`) and `AGENTCORE_GATEWAY_ARN`.
- Sets `protocolConfiguration.serverProtocol = "HTTP"` (required for `/ws` routing).
- Clears `authorizerConfiguration` (→ AWS_IAM) and sets `requestHeaderConfiguration.requestHeaderAllowlist = ["X-Amzn-Bedrock-AgentCore-Runtime-Custom-AuthToken"]` so the agent can read the forwarded idToken.
- Grants the runtime role `bedrock:InvokeModelWithBidirectionalStream` (no extra S3 permission needed — the welcome MP3 lives in the CodeZip).
- Grants the Cognito authenticated role `bedrock-agentcore:InvokeAgentRuntime` + `InvokeAgentRuntimeWithWebSocketStream` scoped to `arn:aws:bedrock-agentcore:<region>:<acct>:runtime/<id>*`.
- Stops all known runtime sessions (scanned from DynamoDB) so the fresh CodeZip is picked up immediately.

**MCP tool wiring (why `tools=[...]` is mandatory and `mcp_gateway_arn=` is
actively harmful).** The Strands text `Agent` uses the `AgentSkills` plugin
and scans a directory of MCP tools implicitly. `BidiAgent` does **not**
expose a `plugins=` parameter. Without an explicit `tools=` list, Nova Sonic
receives an empty `toolConfiguration` and hallucinates device lists from its
training data. `voice_session.py` opens an `MCPClient` against the gateway
with the user's JWT, enumerates tools via `list_tools_sync`, and passes
them straight into `BidiAgent(tools=[...])`. Names arrive prefixed by the
gateway target (`SmartHomeDeviceDiscovery___discover_devices`, etc.) — the
system prompt references those exact names so Nova Sonic emits matching
`toolUse` events.

**Do NOT pass `mcp_gateway_arn=[...]` to `BidiNovaSonicModel`.** Older
Strands builds silently ignored the kwarg; current builds honor it and
register the gateway using the runtime's execution-role IAM credentials.
That parallel path conflicts with our explicit `BidiAgent(tools=...)`
pipeline: Nova Sonic invokes via the model's internal path, and the result
never reaches Strands' tool dispatcher — so the WS never emits
`bidi_tool_result` and Nova Sonic hangs waiting for a toolResult that will
never arrive. Keep a single source of truth: the MCPClient with the user
JWT as Bearer, feeding tools via `BidiAgent(tools=)`.

**Skills inlining.** `BidiAgent` can't register `AgentSkills` either, so
`voice_session.py` calls the text path's `load_skills_from_dynamodb(actor_id)`
helper and inlines the **operational** skill (`all-devices-on`) into the
system prompt. Inlining all five SKILL.md bodies blew the prompt up past the
threshold where Nova Sonic starts ignoring tools, so only the multi-step
orchestration skill makes the cut. Single-device commands are covered by
the base prompt's tool-name schema. Tool-name references inside the skill
markdown (`discover_devices`, `control_device`) are rewritten to their
MCP-prefixed forms on the way in via `_rewrite_tool_names`.

**Dynamic voice prompt override.** The base voice prompt (`VOICE_SYSTEM_PROMPT`)
is editable per-user and globally from the Admin Console's Agent Prompt tab —
see [§8.10](#810-agent-system-prompts-text--voice). `handle_voice_session`
calls `load_system_prompt(actor_id, "voice")` after resolving `actor_id` from
the JWT; the returned string (global + user addendum, joined with `"\n\n"`)
replaces the hardcoded constant as the base before the `all-devices-on` skill
block is appended. If the override load fails, the hardcoded constant is used
and a warning is logged — a broken DynamoDB read must not break the voice
session.

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

**Transcript dedupe — keyed by `completionId`.** Nova Sonic emits every
assistant transcript as **two distinct content blocks**: a SPECULATIVE
block (interim text) followed by a FINAL block (refined text). Per the
Nova Sonic event schema, each block has its own `contentStart` with a
unique `contentId`, but the two blocks share the enclosing `completionId`
that's set by `completionStart`. Prefix-based heuristics were unreliable
(Nova re-emits the same FINAL text on tool-result turns, producing false
negatives) and `contentId` alone can't merge the two blocks of one
utterance (they have different ids). The correct dedup key is
**`completionId`**: SPECULATIVE and FINAL of the same reply share it,
distinct utterances get distinct completionIds.

The voice agent ships a small `BidiNovaSonicModel` subclass —
`_TranscriptIdTaggingModel` in `voice_session.py` — that tracks
`completionStart.completionId` and `contentStart.contentId` in the
parent class's instance state and stamps both onto each
`BidiTranscriptStreamEvent` before Strands forwards it. The browser's
`ChatInterface.tsx` reducer keys on `(role, completionId)`:

- First sighting of a `completionId` → new bubble with `pending = !isFinal`.
- Repeat sighting of the same `completionId` → replace content in place
  and update `pending` from `isFinal`. Never regress a FINAL bubble back
  to pending even if a stray SPECULATIVE arrives late.
- Missing `completionId` (legacy agent builds) → fall back to the earlier
  prefix-match path so old deployments still render correctly during
  rollout.

React-reducer purity matters here: pending state lives on the message
object itself (not a ref) so React 18 StrictMode's double-invoked
setState produces identical results.

User-speech transcripts: Nova Sonic sometimes omits `generationStage`
for user transcripts — the dedup still works because `completionId` is
present on `completionStart` regardless of stage, and the reducer treats
a new user `completionId` as a fresh utterance.

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

**Startup-latency budget.** Measured against the deployed voice runtime
with the Playwright harness in `voice-latency-test/` (Tokyo /
ap-northeast-1, N=100, 2026-04-26). The solution folder has one-click
runners, raw JSONL data, and aggregate markdown reports — see
`voice-latency-test/README.md` and `voice-latency-test/test-modes.md` for
protocol details and how to reproduce.

Two scenarios, each measured independently:

*Session-cold* — long-lived login, loop of `stop_session → click voice`
each round. Models an already-logged-in user who comes back after idle:

| Phase | P50 |
|---|---|
| Click → `new WebSocket` | 101 ms |
| **WS handshake (TCP + TLS + 101)** | **5707 ms** ← AgentCore runs Python import chain for the fresh session here |
| 101 → server `ready` sentinel | 612 ms |
| 101 → first welcome audio chunk | 843 ms |
| **Click → first audio** | **6647 ms** |

*Fresh-login* — per-round: `stop_session + UpdateRuntime + fresh browser
context + Cognito login + click voice`. Models a new user opening the
chatbot for the first time:

| Phase | P50 |
|---|---|
| Cognito login + React render | 985 ms |
| Text warmup POST | 143 ms |
| **Voice warmup POST (cold)** | **5938 ms** |
| Click → `new WebSocket` | 107 ms |
| WS handshake | 325 ms |
| 101 → server `ready` sentinel | 408 ms |
| 101 → first welcome audio chunk | 638 ms |
| **Click → first audio** | **1066 ms** |
| Login → first audio | 7925 ms |

**Why the 5.4-second gap on WS handshake.** Earlier measurements
attributed this to the AgentCore edge proxy. The true explanation, exposed
by the two-scenario test, is the **runtime's per-session Python worker
cold start**: ~5.7 seconds of container + interpreter + import + boto3
init. In session-cold this cost lives on the WS handshake (no prior
warmup). In fresh-login the cost has already been paid during the parallel
`__warmup__` POST triggered immediately after Cognito sign-in — by the
time the user clicks the voice button, the pool has a warm worker ready
and the handshake collapses to TCP + TLS + 101.

The **5.5-second improvement** from session-cold to fresh-login (6647 ms
→ 1066 ms) is the net win from the frontend parallel-warmup optimization.
Infrastructure cold-start cost didn't actually shrink — it just got moved
behind the login that the user was already sitting through.

**Remaining optimization ceiling.** The Python worker cold start itself
(~5.7 s) is an AgentCore-managed cost we can't drive down from app code.
To keep latency below today's 1.1 s P50 at the user's perceived click →
audio point, warmup needs to reliably fire **before** the click —
i.e. the current post-login fan-out must keep working. See `voice-latency-test/`
for the harness to re-validate this after any chatbot or runtime change.

**Login warmup fires in parallel to both runtimes.** `ChatInterface.tsx`
fetches a single idToken + Identity-Pool-creds pair, then issues two
`Promise.all` `POST /invocations {"prompt":"__warmup__"}` requests — one
per runtime ARN. Each runtime short-circuits to `{"status":"warmup_ok"}`
without invoking its LLM; the point is to warm the Python process
(imports, boto3 client pool, TLS to Bedrock). `voice_agent.py` additionally
does module-level eager imports of `strands.experimental.bidi.*` +
`MCPClient` so the warmup request itself triggers the heavy import chain,
and the warmup handler calls `_preheat_boto3_clients()` which forces DDB
+ Bedrock endpoint resolution + TLS handshake + IAM creds fetch.

**Per-session IO is parallelized.** `voice_session.handle_voice_session`
launches three IO tasks concurrently via `asyncio.to_thread`:

1. `load_skills_from_dynamodb(actor_id)` — DDB Query for per-user + global skills.
2. `load_system_prompt(actor_id, "voice")` — two DDB GetItems for prompt overrides.
3. `mcp_client.list_tools_sync` (paginated) — MCP RPC over HTTPS to the gateway.

Previously these were sequential (~1 s). Now the slowest single task
(usually MCP at ~400 ms) sets the floor.

**WS URL pre-presign.** After the login warmup completes, `ChatInterface`
also presigns the voice WS URL with the same creds it already has in
scope and caches the result in a ref with a 4-minute TTL. The first tap
of the voice button skips the SigV4 presign + Identity-Pool-creds dance
(~200-400 ms saved). The cache is one-shot: presigned WS URLs can only be
consumed once, so subsequent taps re-presign on demand.

**TLS preconnect on the login page.** `LoginPage.tsx` injects a
`<link rel="preconnect" href="https://bedrock-agentcore.<region>.amazonaws.com">`
while the user is still typing credentials, so the TLS handshake to the
runtime endpoint is complete by the time the warmup requests fire.

**AudioWorklet prefetch.** `ChatInterface.tsx` fetches
`/pcm-recorder-processor.js` on mount to warm the browser HTTP cache
without triggering the mic permission prompt; the first tap on the voice
button no longer pays a CloudFront round-trip to download the worklet.

**Remaining opportunities (not implemented).**
- **Pre-open the WebSocket silently** during login warmup (just complete
  the handshake, don't send `config` until user taps voice). Would mask
  the 6 s proxy handshake from UX. Trade-off: one long-lived WS per
  logged-in user + idle-timeout bookkeeping.
- **MCP tool list cache** would require decoupling Strands tool objects
  from specific MCPClient instances (tools are bound to their live MCP
  connection today). Low ROI while list_tools is already parallelized
  off the critical path.
- **Bedrock control-plane warmup** via provisioned concurrency is not
  exposed by AgentCore Runtime at time of writing.

#### Voice transcripts persisted to AgentCore Memory

Nova Sonic emits `bidi_transcript_stream` events in two stages per
utterance: `SPECULATIVE` then `FINAL`. The voice session handler
forwards every transcript event to the browser for live display, AND,
when `is_final=True`, also calls `persist_voice_transcript()` which
writes the turn to the **same AgentCore Memory** (`MEMORY_SMARTHOMEMEMORY_ID`)
and **same actor namespace** the text agent uses. Event payload:

```
{
  "conversational": {
    "role": "USER" | "ASSISTANT",
    "content": { "text": "<the finalized transcript>" }
  }
}
```

Because text and voice share `MEMORY_ID`, `actor_id` (sanitized), and
`session_id` (both derived from the Cognito `sub`), a follow-up text
chat in the same session sees the voice turns as prior
user/assistant messages. The write is best-effort — failures are
logged and swallowed so a hiccup on the Memory API never kills the
live voice stream.

### 9.8 Skill ERP & AgentCore Registry

**Goal.** Give regular (non-admin) users a self-service surface to publish
their own skills, decouple authoring from curation, and let admins pull
vetted records into the skills catalog without ever opening raw YAML.

**Three moving parts:**

1. **Skill ERP site** (`skill-erp/` React + TypeScript, S3 + CloudFront at
   port 3003 for local dev). Uses the same Cognito User Pool as the chatbot
   — any confirmed user can sign in; no admin group required. The UI
   mirrors the admin console Skills form (name, description, instructions,
   allowed tools, license, compatibility, key/value metadata) but omits the
   file manager, because the AgentCore Registry `agentSkills` descriptor
   only carries SKILL.md + a schemaVersion 0.1.0 definition JSON — it has
   no provision for script/reference/asset attachments.

2. **`smarthome-skill-erp-api` Lambda + REST API Gateway** with a Cognito
   JWT authorizer. Routes:

   | Method | Path | Behavior |
   |--------|------|----------|
   | GET    | `/my-skills`              | Scan DynamoDB ownership rows (`userId=__erp_owner__`) for the caller's Cognito `sub`, then `GetRegistryRecord` each. Stale ownership rows (record was deleted out-of-band) are cleaned up on the fly. |
   | POST   | `/my-skills`              | Render SKILL.md from form → `CreateRegistryRecord(descriptorType="agentSkills", …)` → save ownership row → `SubmitRegistryRecordForApproval`. Name collisions fall back to `<name>-<6-hex>` (registry record names must be unique per registry). |
   | GET    | `/my-skills/{recordId}`   | Owner check + detail fetch (parses SKILL.md frontmatter and `_meta` from definition JSON). |
   | PUT    | `/my-skills/{recordId}`   | Owner check + merge + `UpdateRegistryRecord` + re-submit for approval. |
   | DELETE | `/my-skills/{recordId}`   | Owner check + `DeleteRegistryRecord` + remove ownership row. |

3. **Admin Console → Skills → "Add approved skill from AgentCore Registry"**.
   Opens a modal that calls `GET /registry/records?status=APPROVED` on the
   admin API (backed by `ListRegistryRecords` filtered by
   `descriptorType=agentSkills`), lets the admin multi-select records and
   pick a target scope (`__global__` or any known user), then
   `POST /registry/import` writes the selected records into DynamoDB as
   standard skill rows (`importedFromRegistry=<recordId>` for audit).

**Registry creation.** `scripts/setup-agentcore.py` creates a single
`SmartHomeSkillsRegistry` with `authorizerType=AWS_IAM` and
`approvalConfiguration.autoApproval=false` so the admin workflow has
something to curate, then patches both Lambdas with `REGISTRY_ID` env and
writes the registry id into `agentcore-state.json` for teardown. The
script is defensive on three edge cases that bit us during initial
deployment testing:
  1. **boto3 too old** — the Registry API requires boto3 ≥ 1.42.93.
     `scripts/01-install-deps.sh` runs `pip install --upgrade boto3` in
     the venv before any step invokes `setup-agentcore.py`. The setup
     script also fails loud with a clear error if `create_registry` is
     still missing, rather than silently leaving
     `REGISTRY_ID=PLACEHOLDER_SET_BY_SETUP_SCRIPT` in the Lambdas.
  2. **Account registry quota** — the default per-account quota is 5
     registries. If `create_registry` fails with
     `ServiceQuotaExceededException` (or any other error) and a registry
     named `SmartHomeSkillsRegistry` already exists from a prior deploy,
     the script reuses that existing registry instead of aborting.
  3. **Conflict exception** — existing-registry path also covers
     `ConflictException` when re-running deploys against an account that
     already has our registry.

**Submit-for-approval race.** `CreateRegistryRecord` returns as soon as
the create request is accepted, but the record lingers in `CREATING`
state for ~1s before it becomes submittable. `SubmitRegistryRecordForApproval`
against a `CREATING` record is silently rejected, so earlier versions of
this code left new records stuck in `DRAFT` forever. The current create
path polls `GetRegistryRecord` (≤ 10s) until the record leaves `CREATING`,
then calls `SubmitRegistryRecordForApproval` — new records land in
`PENDING_APPROVAL` on first try.

**Ownership model.** The registry's built-in ACL is per-registry, not
per-record, so we layer ownership on top in DynamoDB. Each create writes
`{userId: "__erp_owner__", skillName: <recordId>, ownerSub: <caller_sub>}`.
Every `GET/PUT/DELETE /my-skills/{recordId}` starts with a sub-match check
against that row. A malicious user who knows another record's id still
gets `403` because the sub won't match. (An alternative would be to
encode ownership as a registry tag, but tags aren't covered by the
Registry IAM schema yet.)

**SKILL.md rendering.** The Lambda serializes form fields into YAML
frontmatter (`name`, `description`, `allowed_tools`, `x-<metadata-key>`
for custom KV entries) followed by the instructions body. The
`skillDefinition` JSON carries `_meta.license` and `_meta.compatibility`
— they're schema-version 0.1.0 compatible extension points. Import on
the admin side parses the same frontmatter/JSON back out.

**Teardown.** `scripts/teardown-agentcore.py` lists all records in
`SmartHomeSkillsRegistry`, deletes each one, then calls `DeleteRegistry` —
the API rejects deletion of non-empty registries.

### 9.9 Integration Registry & A2A Agents

The Admin Console's **Integration Registry** tab (renamed from
"Integrations") surfaces external tool integrations. It has four sub-tabs:

| Sub-tab | Status |
|---|---|
| Overview | Active — a 4-row status table (Lambda Targets / MCP Servers / API Gateway / A2A Agents). Lambda Targets and A2A Agents are marked "active"; MCP Servers and API Gateway show "planned". |
| A2A Agents | Active — lists approved A2A records from AgentCore Registry with publisher info and a details drawer. See below. |
| MCP Servers | Disabled placeholder ("Coming soon"). |
| API Gateway | Disabled placeholder ("Coming soon"). |

**A2A records live in the same registry as skills.** `SmartHomeSkillsRegistry`
accepts both `AGENT_SKILLS` and `A2A` descriptor types. Skill ERP gains a
second tab "A2A Agents" for end-user publishing, and the Admin Console reads
the `A2A` records for display only (no import-to-DynamoDB in this release).

**A2A data model.** Each A2A record stores a canonical A2A **AgentCard**
as a JSON blob under `descriptors.a2a.agentCard.inlineContent`. The card's
`protocolVersion` field (required by the AgentCore Registry validator, tested
at `"0.3.0"`) and a `provider` object with both `organization` and `url`
fields are mandatory. Form fields (name / description / endpoint / version /
auth scheme / capabilities / tags / sub-skills + examples) are rendered into
the AgentCard JSON by `cdk/lambda/skill-erp-api/a2a_helpers.py`.

**Ownership.** The existing `__erp_owner__` partition in the skills
DynamoDB table holds both skill and A2A ownership rows. A `recordType` field
(`"a2a"` or `"skill"`) disambiguates, and the sort key is prefixed `a2a:` for
A2A rows so it can never collide with a skill recordId.

**API routes (Skill ERP Lambda).** Parallel to `/my-skills`:
`GET/POST/PUT/DELETE /my-a2a-agents[/{recordId}]`.

**Admin API route.** To stay under the admin Lambda's 20 KB resource-policy
cap, the A2A listing is consolidated onto the existing `/registry/records`
resource as a query-param dispatch: `GET /registry/records?action=a2a-list`
returns approved A2A records enriched with `publishedBy` from the
ownership-row scan.

**Deploy-time seed.** `setup-agentcore.py` idempotently seeds three demo
records after the registry is ensured to exist:
`energy-optimization-agent`, `home-security-agent`,
`appliance-maintenance-agent`. Each is created, ownership-rowed under the
deploy-time admin user, and submitted for approval. Approval itself is
manual (no control-plane approve API) — admins click Approve in the
AgentCore Registry console to make them appear in the admin A2A Agents
sub-tab.

### 9.10 Remote Shell Commands per Session

Every row in the Admin Console's Sessions tab carries a **Remote Shell**
button that opens a modal running a shell command inside the targeted
AgentCore Runtime container and streaming stdout/stderr back live —
functionally an SSH-style debug console for the `smarthome` and
`smarthomevoice` runtimes.

**Example-command chips.** The modal includes a row of one-click example
commands useful for agent-runtime ops: list uploaded images under
`/mnt/workspace/`, dump loaded skills, show memory/disk usage, list
running processes, print agent env vars, tail recent logs, print
Python + installed packages. Clicking a chip drops the command into the
textarea so admins can tweak it before running.

**Browser talks to AgentCore directly.** The modal calls
`bedrock-agentcore:InvokeAgentRuntimeCommand` via
`@aws-sdk/client-bedrock-agentcore` using temporary credentials from the
Cognito authenticated Identity Pool (the same role used for
`/invocations` and `/ws`). The response is an HTTP/2 event stream; the
browser consumes it as an async iterable. Zero Lambda, zero API Gateway —
the feature is purely frontend-plus-IAM.

**Architecture:**

```
Admin Console (browser, logged in as admin)
    │  1. Cognito authenticated-role creds (cached)
    │  2. lazy import @aws-sdk/client-bedrock-agentcore
    │  3. BedrockAgentCoreClient.send(
    │       InvokeAgentRuntimeCommandCommand({
    │         agentRuntimeArn, runtimeSessionId,
    │         body: { command, timeout }}))
    │     SigV4-signed HTTP/2,
    │     accept: application/vnd.amazon.eventstream
    v
AgentCore Runtime (smarthome or smarthomevoice)
    spawns the command in the running container,
    streams chunk.contentStart → contentDelta {stdout|stderr}
    → contentStop {exitCode, status}. Non-blocking to active
    agent invocations on the same session.
    │
    v
Browser (ShellModal + AnsiOutput)
    for await (const evt of resp.stream) {...}
    Append-only terminal pane (60vh, ANSI color subset,
    stderr in red, 5 MB accumulation cap). Stop button
    fires AbortController.abort().
```

**IAM.** `bedrock-agentcore:InvokeAgentRuntimeCommand` is appended to the
existing Cognito authenticated-role grant in `setup-agentcore.py`
(alongside `InvokeAgentRuntime` and `InvokeAgentRuntimeWithWebSocketStream`).

**Security caveat — acknowledged.** The permission lives on the shared
Cognito authenticated role, which every logged-in user carries. The
admin-only check is currently client-side (React route gate + button
visibility in the admin-only Sessions tab). A follow-up spec will split the
Identity Pool into role-mapped groups so that `InvokeAgentRuntimeCommand`
and other admin-only actions live on an `admin`-group role.

**Input contracts.**
- `command` must be 1 byte to 64 KB.
- `timeout` must be 1 to 3600 seconds (default 300).
- `runtimeSessionId` must be at least 33 characters (the Sessions tab
  already uses Cognito `sub` UUIDs = 36 chars).

**UX details.**
- Runtime selector (Text/Voice) defaults to the clicked row's `kind`
  but is overridable so admins can debug the voice container from a text
  session's row.
- Output pane renders a limited ANSI subset: reset, bold, 30-37 / 90-97
  foreground colors. Unknown sequences (cursor moves, 256-color, etc.) are
  stripped silently. Stderr chunks carry a red CSS class.
- Modal-local command history (last 20 commands, deduped, most-recent-
  first) resets on close. Copy button strips ANSI and writes the plain
  text to clipboard.
- The SDK module (~100 KB minified) is lazy-imported on modal open so it
  never loads for admins who don't use the feature.

### 9.11 Browser Use — Live Agent Web Automation

The agent can drive a real Chromium browser through the **AgentCore Browser
Tool** and stream it live to the user. This turns "look up current info
on the web" from a fragile `http_request` + HTML-scraping chain into an
explicit, auditable browse with a live video feed the user can watch and
even take over mid-run.

**User-visible flow.** The user asks a natural-language question like
*"What does example.com say right now?"* or *"淘宝上 iPhone 16 最便宜的是多少?"*
The agent's system prompt + the `browser-use` skill's description route
any live-web question to the `browse_web(goal)` tool. The tool opens a
Chrome session on AgentCore, the chatbot's right-side panel shows the
real browser streaming over DCV, per-step PNG screenshots land in the
agent session's `/mnt/workspace/<sid>/browser/` (visible in the same
panel's Files tab), and when the agent returns a summary it gets quoted
into the chat reply.

**Architecture:**

```
┌──────── Chatbot (React) ──────────────────────────────┐
│  ChatInterface                                        │
│   └─ BrowserPanel (right column, collapsed by default)│
│       Collapsed state: vertical rail with             │
│         • "Browser" → expand to Live view tab         │
│         • "Files"   → expand to Files tab             │
│       Expanded state: 720px default, maximize ⤢ to    │
│         fill chat column; restore back to 720px.      │
│       Live view tab:                                   │
│         • DCV viewer (<script src=/dcvjs/dcv.js>)      │
│         • "Take control" ↔ "Release control" button   │
│         • Pause indicator when human drives           │
│       Files tab:                                       │
│         • Walks /mnt/workspace/<agentSessionId>/       │
│         • Click file → download (blob URL)            │
│  Polling loop (1.5 s while agent is typing):          │
│    GET /sessions?action=browser-active&userId=X →     │
│    {sessionId, liveViewUrl, status, startedAt, ...}   │
│  Direct SDK calls (no Lambda hop):                    │
│    InvokeAgentRuntimeCommand  — file list + download  │
│    UpdateBrowserStream        — take/release control  │
└──────────────┬────────────────────────────────────────┘
               │ SigV4 (Identity Pool creds)
               ▼
┌──────── Text Agent Runtime (Strands) ──────────────────┐
│  agent.py registers `browse_web` iff the user's        │
│  effective skill set includes "browser-use".           │
│                                                        │
│  tools/browser_use.run_browse_web(goal, user_id, sid): │
│    1. BrowserClient.start(identifier="aws.browser.v1") │
│       → sessionId, ws_url, live_view_url, headers      │
│    2. PutItem DDB smarthome-browser-sessions           │
│         status="running", browserIdentifier, liveViewUrl│
│    3. browser_use.Agent(task=goal, llm=ChatAWSBedrock, │
│         browser_session=Browser(cdp_url=ws_url,        │
│             browser_profile=BrowserProfile(headers)),  │
│         register_new_step_callback=<screenshot each    │
│             step to /mnt/workspace/<sid>/browser/>)    │
│       .run()  (asyncio.wait_for 120s cap)              │
│    4. Success: PutItem status="idle"  (NOT stopped —    │
│       AgentCore reaps the session at 15 min idle cap;  │
│       the user can keep driving via Take Control).     │
│       Failure: BrowserClient.stop() + status="failed". │
│    5. Return summary text (+breadcrumb about Files)    │
└──────────────┬────────────────────────────────────────┘
               │ SigV4 headers (bc.generate_ws_headers)
               ▼
┌──────── AgentCore Browser Tool ────────────────────────┐
│  • aws.browser.v1 — AWS-managed Chrome, web-bot-auth   │
│    on by default.                                      │
│  • Automation stream  (CDP WebSocket) → browser-use    │
│  • Live-view stream   (DCV over HTTPS) → chatbot       │
└────────────────────────────────────────────────────────┘
```

**Core design choices:**

- **Direct SDK from the browser, not through Lambda.** The chatbot calls
  `InvokeAgentRuntimeCommand` and `UpdateBrowserStream` with Identity-Pool
  credentials — same pattern as §9.10 Remote Shell. Adding those three
  user-facing operations through the admin Lambda would have pushed its
  resource-based policy past the 20 KB cap (see §9.4 note). The file
  browser does a `bash -c 'ls -lA <path>'` and a `head -c CAP <path> |
  base64 -w0` via `InvokeAgentRuntimeCommand` and parses the output
  client-side — no per-file-operation Lambda code.
- **Use `bedrock_agentcore.tools.browser_client.BrowserClient` rather
  than raw boto3.** The helper builds SigV4-signed WebSocket upgrade
  headers that Playwright must forward on the CDP handshake — a bare
  `wss://` connect is rejected with HTTP 403 because the endpoint
  requires AWS4 auth on the upgrade. Raw `start_browser_session` returns
  a URL that is NOT accepted by Playwright.
- **Explicit `Browser + BrowserProfile(headers=...)` not `BrowserSession
  (headers=...)`.** The shortcut form silently drops the headers through
  pydantic field merging in some `browser-use` builds; the explicit form
  routes them through to `cdp_use.CDPClient(additional_headers=...)`
  reliably.
- **DDB row carries `browserIdentifier`** so `UpdateBrowserStream`
  (human take-control) targets the correct browser. Rows without it fall
  back to the default `aws.browser.v1`.
- **Per-step screenshots.** `register_new_step_callback` fires after
  every browse action; the callback writes a PNG to the text-agent
  runtime's `/mnt/workspace/<agentSessionId>/browser/step-NNN-HH-MM-SS.png`.
  Saving into the agent's session-storage (not the browser sandbox FS)
  means the Files tab already shows them via the existing workspace-probe
  path.
- **Session isolation.** DDB primary key is `(userId, sessionId)`. The
  chatbot polling endpoint enforces self-or-admin on the caller's Cognito
  claim. Polls use `ConsistentRead` because AgentCore writes
  `running → idle` within the tool's wall-clock cap and eventually-
  consistent reads were missing the short window.
- **Keep-alive after the tool returns.** On success the tool does **not**
  call `BrowserClient.stop()` — the AgentCore session survives until its
  `sessionTimeoutSeconds=900` (15 min) idle timeout. DDB row flips to
  `status="idle"`; the chatbot keeps rendering the live view and the Take
  Control toggle stays active. This lets the user finish the task by
  hand — scroll the page, click a filter the agent didn't think of,
  paste credentials — without a second tool invocation. Failure paths
  still call `stop()` so a half-broken session doesn't burn compute.
- **Fixed DCV canvas + scroll wrapper.** The `DcvViewer` component
  requests `1280×800` via `requestDisplayLayout([{rect, primary}])` on
  every `firstFrame` and `displayLayout` callback, and nests the DCV
  `<div>` inside a `overflow: auto` wrapper. When the panel is narrower
  than 1280 px or shorter than 800 px, browser-native scrollbars appear
  (horizontal + vertical) so the user can reach any part of the page
  without resizing.

**Agent-vs-human control toggle.** `BrowserClient.update_stream(
"DISABLED")` severs the CDP WebSocket; the browser-use loop inside the
tool errors out on its next action and the tool returns
`"Browsing failed: ..."`. From the user's perspective the DCV stream
stays alive and they can drive the browser manually (mouse/keyboard
forwarded through DCV). Releasing re-enables the automation stream but
since the tool has already returned, practical use is "take control to
finish what the agent started, then run a new prompt." When the tool
exits successfully without the user ever taking control, the DDB row
is `idle` and the same Take Control button remains clickable for the
15-minute session lifetime — no distinction between "agent was
interrupted" and "agent finished on its own".

**Default-collapsed panel + maximize.** The panel renders by default as a
40-px-wide vertical rail so the chat column remains the focus. Clicking
either "Browser" or "Files" expands the panel to 720 px on that tab. A
`⤢` button in the header flips the panel to `flex: 1` (fills the chat
column's remaining horizontal space) for reading full Amazon / Wikipedia
pages; `⤡` restores the 720 px default. The `×` button collapses the
panel back to the rail (it does not hide the rail itself — the rail is
always present, so re-expanding is always one click). Rail labels read
bottom-to-top via `writing-mode: vertical-rl` (standard for right-edge
IDE-style tab rails).

**Welcome-screen prompt chips grouped by capability.** The empty chat
state surfaces five labelled groups of starter prompts so users discover
the agent's surface area without reading docs:

- **Smart devices** — device discovery, power, LED/fan/oven/rice modes
  (`discover_devices` + `control_device` MCP tools)
- **Knowledge base** — product manuals, presets, troubleshooting codes
  (`query_knowledge_base` MCP tool → Bedrock KB over S3 Vectors)
- **Weather** — geocode + forecast for a free-text place
  (`weather-lookup` skill → `http_request` → Open-Meteo)
- **Live web browser** — `example.com`, shopping search, Wikipedia,
  current-IP lookups (`browser-use` skill → `browse_web` tool)
- **Image analysis** — attach-image-then-ask flow (vision bypass path,
  see §8.11)

**Skill auto-trigger.** `agent/skills/browser-use/SKILL.md`'s
`description` frontmatter explicitly enumerates trigger phrasings ("look
up", "search on X", "check current", "find online", product searches,
news headlines, current prices) so Kimi invokes `browse_web` without the
user naming the tool. The skill is included in the default seed list;
per-user overrides in `Admin Console → Skills` can remove or rename it.

**DynamoDB table `smarthome-browser-sessions`:**

| attr                | type | note                                               |
| ------------------- | ---- | -------------------------------------------------- |
| `userId` (PK)       | S    | Cognito email / username                           |
| `sessionId` (SK)    | S    | AgentCore browser session id (ULID)                |
| `agentSessionId`    | S    | Parent text-agent session id                       |
| `browserIdentifier` | S    | `aws.browser.v1` (or a custom browser ARN)         |
| `liveViewUrl`       | S    | SigV4-presigned URL (5 min expiry)                 |
| `status`            | S    | `running` (tool actively driving) / `idle` (tool done, browser kept alive for the user) / `failed` |
| `goal`              | S    | First 200 chars of the user-visible goal           |
| `startedAt`         | S    | ISO8601                                            |
| `endedAt`           | S    | Optional — set on failed only (idle rows stay open) |
| `lastError`         | S    | Optional — truncated to 500 chars                  |
| `ttl`               | N    | Epoch seconds, +1h; row auto-expires               |

**IAM.** Two roles gain additional permissions:

- **Text-agent runtime role** (`AgentCore-smarthome-*`): granted
  `bedrock-agentcore:*` on `*` (tightened to `Start/Stop/Get/List
  BrowserSession + UpdateBrowserStream + InvokeBrowser + UseBrowser`
  didn't work — the WebSocket upgrade itself needs a broader grant)
  plus DDB read/write on `smarthome-browser-sessions`.
- **Cognito authenticated role** (`SmartHomeAssistantStack-CognitoAuthRole*`):
  appended `bedrock-agentcore:UpdateBrowserStream` alongside the
  existing `InvokeAgentRuntime* + InvokeAgentRuntimeCommand` grants.

**Dependencies.** `agent/pyproject.toml` adds `browser-use>=0.1.40` and
`playwright>=1.50`. Both ride along with the runtime's CodeZip through
the agentcore-CLI scaffold. The chatbot ships the Amazon **DCV Web
Client SDK** under `public/dcvjs/`; it is EULA-licensed and fetched on
every deploy by `scripts/01-install-deps.sh` from a public AWS
CloudFront URL (not redistributed in this repo).

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

**Request Body (with image attachments — vision bypass path):**
```json
{
  "prompt": "describe this",
  "userId": "user@example.com",
  "images": [
    {"mediaType": "image/png",  "data": "<base64>"},
    {"mediaType": "image/jpeg", "data": "<base64>"}
  ]
}
```

`images` field rules (see §8.11 for the full flow):

| Field | Type | Constraint |
|-------|------|------------|
| `images` | array, optional | max 3 items; >3 returns `400 {"error": "Invalid images payload (max 3)."}` |
| `images[].mediaType` | string | one of `image/png`, `image/jpeg`, `image/webp`, `image/gif` |
| `images[].data` | string | base64-encoded raw bytes; decoded size must be ≤ 20 MB |

**Response:**
```json
{
  "response": "I've set the LED matrix to rainbow mode. The colorful animation should now be visible on your LED panel.",
  "status": "success"
}
```

Response shape is the same for text and image turns; image turns return the vision model's description in `response` (plus any `Note: …` partial-failure warnings appended).

Special: `{"prompt": "__warmup__"}` returns `{"status": "warmup_ok"}` without invoking the LLM. The chatbot fires this immediately after login to pre-warm the runtime container.

#### GET /ws — WebSocket (voice mode)

**Authorization:** AWS SigV4 presigned URL. See §9.7 for the full protocol and event types. Voice sessions require the caller's IAM principal to hold `bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream` on the runtime ARN (the Cognito authenticated role is granted this by `setup-agentcore.py`).

#### GET /ping

Health check endpoint. Returns 200 when the runtime is healthy.

#### Admin-API `/sessions?action=browser-*` (chatbot-facing)

The chatbot polls `GET /sessions?action=browser-active&userId=<email>` to
surface the user's live browser session in the right-side panel. This
endpoint piggybacks on the existing admin `/sessions` route (the admin
Lambda's resource-based policy is at the 20 KB cap — see §9.4 — so we
dispatch by `?action=` rather than add a new API Gateway method). The
handler enforces self-or-admin against the caller's Cognito claim:
non-admin callers may only query their own `userId`.

- **Request:** `Authorization: Bearer <idToken>`; query params `action`
  (one of `browser-active`), `userId`.
- **Response:** the latest DDB row (any status) sorted by `startedAt`
  descending, using `ConsistentRead` to avoid missing the brief
  `running` window. Empty `{}` when the user has never browsed.

Workspace file browsing and browser take/release control do **not** go
through the Lambda; the chatbot calls `bedrock-agentcore:
InvokeAgentRuntimeCommand` and `bedrock-agentcore:UpdateBrowserStream`
directly with Identity-Pool credentials (same pattern as §9.10 Remote
Shell).

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

All device topics are now **per-user scoped**. `<userSub>` is the Cognito User Pool `sub` (UUID) of the signed-in user. The device simulator publishes and subscribes only under its own prefix, and the `iot-control` Lambda only publishes to the caller's prefix — derived from the agent-wrapped tool argument, not from anything the LLM can forge.

```
smarthome/
└── <userSub>/
    ├── led_matrix/command    # Commands to that user's LED matrix
    ├── rice_cooker/command   # Commands to that user's rice cooker
    ├── fan/command           # Commands to that user's fan
    └── oven/command          # Commands to that user's oven
```

See §6.1 (Device Simulator MQTT Connection) and §8.3 (Tool Access via Gateway) for how `<userSub>` propagates from the browser's Cognito session and from the agent's validated idToken.

### 11.2 Command Message Schemas

#### LED Matrix (`smarthome/<userSub>/led_matrix/command`)

| Action | Parameters | Example |
|--------|-----------|---------|
| `setPower` | `power`: boolean | `{"action":"setPower","power":true}` |
| `setMode` | `mode`: rainbow, breathing, chase, sparkle, fire, ocean, aurora | `{"action":"setMode","mode":"rainbow"}` |
| `setBrightness` | `brightness`: 0-100 | `{"action":"setBrightness","brightness":75}` |
| `setColor` | `color`: hex string | `{"action":"setColor","color":"#FF00FF"}` |

#### Rice Cooker (`smarthome/<userSub>/rice_cooker/command`)

| Action | Parameters | Example |
|--------|-----------|---------|
| `start` | `mode`: white_rice, brown_rice, porridge, steam | `{"action":"start","mode":"white_rice"}` |
| `stop` | (none) | `{"action":"stop"}` |
| `keepWarm` | `enabled`: boolean | `{"action":"keepWarm","enabled":true}` |

#### Fan (`smarthome/<userSub>/fan/command`)

| Action | Parameters | Example |
|--------|-----------|---------|
| `setPower` | `power`: boolean | `{"action":"setPower","power":true}` |
| `setSpeed` | `speed`: 0 (off), 1 (low), 2 (medium), 3 (high) | `{"action":"setSpeed","speed":2}` |
| `setOscillation` | `enabled`: boolean | `{"action":"setOscillation","enabled":true}` |

#### Oven (`smarthome/<userSub>/oven/command`)

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
                                     +-> copy-webpack-plugin → dist/dcvjs/
                                     +-> Output: bundle.[hash].js + index.html
```

The chatbot ships the **Amazon DCV Web Client SDK** under `public/dcvjs/`
for the BrowserPanel live-view feature (§9.11). The SDK is **not**
committed to this repo (Amazon DCV EULA terms); `scripts/01-install-deps.sh`
downloads a fresh copy from a public AWS CloudFront URL on every deploy
before the webpack build. Once present under `public/dcvjs/`, Webpack's
static-asset copy plugin ships it into `dist/dcvjs/` alongside the bundle.
A fresh clone that skips the install script will build successfully but
the live-view panel will fall back to a "Failed to load /dcvjs/dcv.js"
error state.

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
| **Claude Haiku 4.5** for image captioning (default `VISION_MODEL_ID`) | Native multimodal on Bedrock Converse, strong OCR / fine-detail, same IAM footprint as Kimi. Latency bench (`vision-latency-test/`) vs Nova Lite: Haiku p50 3.3–5.4 s, Nova Lite p50 2.0–3.2 s depending on image count; the choice is env-flag-driven so operators can pick by workload |
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
| Bedrock vision (Haiku 4.5 / Nova Lite) | Per-model throttling | Called only on image turns; one retry on Throttling/ServiceUnavailable before falling back to a placeholder reply |
| Session storage `/mnt/workspace` | Per session | Managed by AgentCore; evicted when the session closes. One directory per `runtimeSessionId`, fully isolated across sessions |
| Cognito | Per-region | 40 req/sec default for auth APIs |

For production use, consider:
- CloudFront custom domain with ACM certificate
- Cognito advanced security features
- IoT Core fleet provisioning for real devices
- DynamoDB for chat history persistence

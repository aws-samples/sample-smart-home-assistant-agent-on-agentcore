interface AppConfig {
  cognitoUserPoolId: string;
  cognitoClientId: string;
  cognitoDomain: string;
  cognitoIdentityPoolId: string;
  agentRuntimeArn: string;
  // Bundles-mode runtime ARN (direct invocation for users in ab-bundles mode).
  // Empty string means the bundles runtime hasn't been provisioned yet
  // (transitional state — setup-agentcore.py injects this once deployed).
  bundlesRuntimeArn: string;
  // Voice runs on a dedicated AgentCore Runtime (see
  // docs/superpowers/specs/2026-04-23-voice-agent-split-design.md). Empty
  // string means the deploy hasn't been run post-split yet; the chatbot
  // falls back to agentRuntimeArn for voice in that transition case.
  voiceAgentRuntimeArn: string;
  // Text chat HTTP path goes through the dedicated optimization gateway
  // (target-based A/B routing). When unset (transitional state until the
  // first post-redesign deploy) the chatbot falls back to direct
  // runtime invocation. Voice WSS is NOT routed through this gateway —
  // AgentCore Gateway only proxies HTTP, not WebSocket.
  optimizationGatewayUrl: string;
  optimizationDefaultTarget: string;
  adminApiUrl: string;
  region: string;
}

export function getConfig(): AppConfig {
  const raw = (window as any).__CONFIG__ || {};
  return {
    cognitoUserPoolId: raw.cognitoUserPoolId ?? '',
    cognitoClientId: raw.cognitoClientId ?? '',
    cognitoDomain: raw.cognitoDomain ?? '',
    cognitoIdentityPoolId: raw.cognitoIdentityPoolId ?? '',
    agentRuntimeArn: raw.agentRuntimeArn ?? '',
    bundlesRuntimeArn: raw.bundlesRuntimeArn ?? '',
    voiceAgentRuntimeArn: raw.voiceAgentRuntimeArn ?? '',
    optimizationGatewayUrl: raw.optimizationGatewayUrl ?? '',
    optimizationDefaultTarget: raw.optimizationDefaultTarget ?? 'smarthome-control',
    adminApiUrl: raw.adminApiUrl ?? '',
    region: raw.region ?? 'us-west-2',
  };
}

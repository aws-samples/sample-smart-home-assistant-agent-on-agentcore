interface AppConfig {
  cognitoUserPoolId: string;
  cognitoClientId: string;
  cognitoDomain: string;
  cognitoIdentityPoolId: string;
  agentRuntimeArn: string;
  // Voice runs on a dedicated AgentCore Runtime (see
  // docs/superpowers/specs/2026-04-23-voice-agent-split-design.md). Empty
  // string means the deploy hasn't been run post-split yet; the chatbot
  // falls back to agentRuntimeArn for voice in that transition case.
  voiceAgentRuntimeArn: string;
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
    voiceAgentRuntimeArn: raw.voiceAgentRuntimeArn ?? '',
    region: raw.region ?? 'us-east-1',
  };
}

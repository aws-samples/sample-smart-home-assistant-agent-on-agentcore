interface AppConfig {
  cognitoUserPoolId: string;
  cognitoClientId: string;
  cognitoIdentityPoolId: string;
  adminApiUrl: string;
  agentRuntimeArn: string;
  voiceAgentRuntimeArn: string;
  region: string;
  chatbotUrl?: string;
  deviceSimulatorUrl?: string;
}

export function getConfig(): AppConfig {
  return (window as any).__CONFIG__ || {
    cognitoUserPoolId: '',
    cognitoClientId: '',
    cognitoIdentityPoolId: '',
    adminApiUrl: '',
    agentRuntimeArn: '',
    voiceAgentRuntimeArn: '',
    region: 'us-east-1',
    chatbotUrl: '',
    deviceSimulatorUrl: '',
  };
}

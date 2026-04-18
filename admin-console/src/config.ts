interface AppConfig {
  cognitoUserPoolId: string;
  cognitoClientId: string;
  adminApiUrl: string;
  agentRuntimeArn: string;
  region: string;
  chatbotUrl?: string;
  deviceSimulatorUrl?: string;
}

export function getConfig(): AppConfig {
  return (window as any).__CONFIG__ || {
    cognitoUserPoolId: '',
    cognitoClientId: '',
    adminApiUrl: '',
    agentRuntimeArn: '',
    region: 'us-east-1',
    chatbotUrl: '',
    deviceSimulatorUrl: '',
  };
}

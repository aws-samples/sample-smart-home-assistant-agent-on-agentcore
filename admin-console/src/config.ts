interface AppConfig {
  cognitoUserPoolId: string;
  cognitoClientId: string;
  adminApiUrl: string;
  agentRuntimeArn: string;
  region: string;
}

export function getConfig(): AppConfig {
  return (window as any).__CONFIG__ || {
    cognitoUserPoolId: '',
    cognitoClientId: '',
    adminApiUrl: '',
    agentRuntimeArn: '',
    region: 'us-east-1',
  };
}

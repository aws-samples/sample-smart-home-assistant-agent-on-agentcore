interface AppConfig {
  cognitoUserPoolId: string;
  cognitoClientId: string;
  cognitoDomain: string;
  agentRuntimeArn: string;
  region: string;
}

export function getConfig(): AppConfig {
  return (window as any).__CONFIG__ || {
    cognitoUserPoolId: '',
    cognitoClientId: '',
    cognitoDomain: '',
    agentRuntimeArn: '',
    region: 'us-east-1',
  };
}

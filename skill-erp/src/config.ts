interface AppConfig {
  cognitoUserPoolId: string;
  cognitoClientId: string;
  erpApiUrl: string;
  region: string;
}

export function getConfig(): AppConfig {
  return (window as any).__CONFIG__ || {
    cognitoUserPoolId: '',
    cognitoClientId: '',
    erpApiUrl: '',
    region: 'us-west-2',
  };
}

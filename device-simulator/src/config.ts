interface AppConfig {
  iotEndpoint: string;
  region: string;
  cognitoIdentityPoolId: string;
  cognitoUserPoolId: string;
  cognitoClientId: string;
}

export function getConfig(): AppConfig {
  return (window as any).__CONFIG__ || {
    iotEndpoint: 'localhost',
    region: 'us-east-1',
    cognitoIdentityPoolId: '',
    cognitoUserPoolId: '',
    cognitoClientId: '',
  };
}

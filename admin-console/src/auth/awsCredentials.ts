/**
 * AWS temporary credentials for admin-console browser calls.
 *
 * Uses the admin's Cognito User Pool idToken to exchange for credentials
 * from the Cognito Identity Pool's AUTHENTICATED role. The same role
 * already carries `bedrock-agentcore:InvokeAgentRuntime*`; the Remote Shell
 * spec adds `InvokeAgentRuntimeCommand` so the browser can call AgentCore
 * directly.
 *
 * Credentials are cached by the provider until near expiry.
 */
import { fromCognitoIdentityPool } from '@aws-sdk/credential-providers';
import type { AwsCredentialIdentity } from '@aws-sdk/types';

import { getConfig } from '../config';
import { getIdToken } from './CognitoAuth';

let cachedProvider: ReturnType<typeof fromCognitoIdentityPool> | null = null;

function ensureProvider() {
  if (cachedProvider) return cachedProvider;
  const config = getConfig();
  if (!config.cognitoIdentityPoolId || !config.cognitoUserPoolId) {
    throw new Error(
      'Cognito Identity Pool / User Pool not configured in admin config.js',
    );
  }
  cachedProvider = fromCognitoIdentityPool({
    clientConfig: { region: config.region },
    identityPoolId: config.cognitoIdentityPoolId,
    logins: {
      [`cognito-idp.${config.region}.amazonaws.com/${config.cognitoUserPoolId}`]:
        async () => getIdToken(),
    },
  });
  return cachedProvider;
}

export async function getAwsCredentials(): Promise<AwsCredentialIdentity> {
  return ensureProvider()();
}

export function resetAwsCredentials(): void {
  cachedProvider = null;
}

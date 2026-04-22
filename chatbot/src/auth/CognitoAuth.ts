import {
  CognitoUserPool,
  CognitoUser,
  AuthenticationDetails,
  CognitoUserAttribute,
  CognitoUserSession,
} from 'amazon-cognito-identity-js';
import { getConfig } from '../config';

function getUserPool(): CognitoUserPool {
  const config = getConfig();
  return new CognitoUserPool({
    UserPoolId: config.cognitoUserPoolId,
    ClientId: config.cognitoClientId,
  });
}

export interface AuthTokens {
  idToken: string;
  accessToken: string;
  refreshToken: string;
}

function sessionToTokens(session: CognitoUserSession): AuthTokens {
  return {
    idToken: session.getIdToken().getJwtToken(),
    accessToken: session.getAccessToken().getJwtToken(),
    refreshToken: session.getRefreshToken().getToken(),
  };
}

export function signIn(username: string, password: string): Promise<AuthTokens> {
  return new Promise((resolve, reject) => {
    const userPool = getUserPool();
    const cognitoUser = new CognitoUser({
      Username: username,
      Pool: userPool,
    });

    const authDetails = new AuthenticationDetails({
      Username: username,
      Password: password,
    });

    cognitoUser.authenticateUser(authDetails, {
      onSuccess(session: CognitoUserSession) {
        resolve(sessionToTokens(session));
      },
      onFailure(err: Error) {
        reject(err);
      },
    });
  });
}

export function signUp(
  username: string,
  password: string,
  email: string
): Promise<void> {
  return new Promise((resolve, reject) => {
    const userPool = getUserPool();
    const attributes = [
      new CognitoUserAttribute({ Name: 'email', Value: email }),
    ];

    userPool.signUp(username, password, attributes, [], (err, result) => {
      if (err) {
        reject(err);
        return;
      }
      resolve();
    });
  });
}

export function confirmSignUp(username: string, code: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const userPool = getUserPool();
    const cognitoUser = new CognitoUser({
      Username: username,
      Pool: userPool,
    });

    cognitoUser.confirmRegistration(code, true, (err, result) => {
      if (err) {
        reject(err);
        return;
      }
      resolve();
    });
  });
}

export function signOut(): void {
  const userPool = getUserPool();
  const currentUser = userPool.getCurrentUser();
  if (currentUser) {
    currentUser.signOut();
  }
}

export function getCurrentSession(): Promise<AuthTokens> {
  return new Promise((resolve, reject) => {
    const userPool = getUserPool();
    const currentUser = userPool.getCurrentUser();

    if (!currentUser) {
      reject(new Error('No current user'));
      return;
    }

    currentUser.getSession(
      (err: Error | null, session: CognitoUserSession | null) => {
        if (err || !session) {
          reject(err || new Error('No session'));
          return;
        }
        if (!session.isValid()) {
          reject(new Error('Session is not valid'));
          return;
        }
        resolve(sessionToTokens(session));
      }
    );
  });
}

export async function getIdToken(): Promise<string> {
  const tokens = await getCurrentSession();
  return tokens.idToken;
}

// ---------------------------------------------------------------------------
// Cognito Identity Pool credentials (federated from the User Pool session).
// Used for SigV4-signing /invocations and /ws calls to the AgentCore Runtime.
// ---------------------------------------------------------------------------
import { fromCognitoIdentityPool } from '@aws-sdk/credential-providers';
import type { AwsCredentialIdentity, AwsCredentialIdentityProvider } from '@aws-sdk/types';

let cachedProvider: AwsCredentialIdentityProvider | null = null;
let providerIdToken: string = '';

/**
 * Returns a cached credential provider that resolves to temporary AWS creds
 * for the currently logged-in user (authenticated Cognito role).
 * Rebuilt if the idToken changes (e.g. after refresh).
 */
function getCredentialProvider(idToken: string): AwsCredentialIdentityProvider {
  if (cachedProvider && providerIdToken === idToken) return cachedProvider;
  const config = getConfig();
  const providerName = `cognito-idp.${config.region}.amazonaws.com/${config.cognitoUserPoolId}`;
  cachedProvider = fromCognitoIdentityPool({
    clientConfig: { region: config.region },
    identityPoolId: config.cognitoIdentityPoolId,
    logins: { [providerName]: idToken },
  });
  providerIdToken = idToken;
  return cachedProvider;
}

/**
 * Resolves authenticated AWS credentials for the current user.
 * The SDK handles caching + refresh under the hood via the provider closure,
 * but we also invalidate the cached provider whenever the idToken changes.
 */
export async function getAwsCredentials(): Promise<AwsCredentialIdentity> {
  const idToken = await getIdToken();
  const provider = getCredentialProvider(idToken);
  return provider();
}

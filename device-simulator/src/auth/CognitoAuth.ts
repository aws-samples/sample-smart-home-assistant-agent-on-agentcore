import {
  CognitoUserPool,
  CognitoUser,
  AuthenticationDetails,
  CognitoUserAttribute,
  CognitoUserSession,
} from 'amazon-cognito-identity-js';
import { jwtDecode } from 'jwt-decode';
import { fromCognitoIdentityPool } from '@aws-sdk/credential-providers';
import type { AwsCredentialIdentity, AwsCredentialIdentityProvider } from '@aws-sdk/types';
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
    const cognitoUser = new CognitoUser({ Username: username, Pool: userPool });
    const authDetails = new AuthenticationDetails({ Username: username, Password: password });
    cognitoUser.authenticateUser(authDetails, {
      onSuccess(session: CognitoUserSession) {
        resolve(sessionToTokens(session));
      },
      onFailure(err: Error) { reject(err); },
    });
  });
}

export function signUp(username: string, password: string, email: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const userPool = getUserPool();
    const attrs = [new CognitoUserAttribute({ Name: 'email', Value: email })];
    userPool.signUp(username, password, attrs, [], (err) => {
      if (err) { reject(err); return; }
      resolve();
    });
  });
}

export function confirmSignUp(username: string, code: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const userPool = getUserPool();
    const cognitoUser = new CognitoUser({ Username: username, Pool: userPool });
    cognitoUser.confirmRegistration(code, true, (err) => {
      if (err) { reject(err); return; }
      resolve();
    });
  });
}

export function signOut(): void {
  const userPool = getUserPool();
  const currentUser = userPool.getCurrentUser();
  if (currentUser) currentUser.signOut();
}

export function getCurrentSession(): Promise<AuthTokens> {
  return new Promise((resolve, reject) => {
    const userPool = getUserPool();
    const currentUser = userPool.getCurrentUser();
    if (!currentUser) { reject(new Error('No current user')); return; }
    currentUser.getSession((err: Error | null, session: CognitoUserSession | null) => {
      if (err || !session) { reject(err || new Error('No session')); return; }
      if (!session.isValid()) { reject(new Error('Session is not valid')); return; }
      resolve(sessionToTokens(session));
    });
  });
}

export async function getIdToken(): Promise<string> {
  const tokens = await getCurrentSession();
  return tokens.idToken;
}

/** Returns the Cognito User Pool `sub` claim for the current session — the
 *  same value the agent-side Lambda will read from the forwarded JWT. This
 *  is what the device-simulator uses to scope its MQTT subscriptions. */
export async function getUserSub(): Promise<string> {
  const idToken = await getIdToken();
  const decoded = jwtDecode<{ sub: string }>(idToken);
  return decoded.sub;
}

// -----------------------------------------------------------------------
// Cognito Identity Pool credentials (federated from the User Pool session).
// IoT Core MQTT SigV4 uses these.
// -----------------------------------------------------------------------
let cachedProvider: AwsCredentialIdentityProvider | null = null;
let providerIdToken: string = '';

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

export async function getAwsCredentials(): Promise<AwsCredentialIdentity> {
  const idToken = await getIdToken();
  const provider = getCredentialProvider(idToken);
  return provider();
}

// ---------------------------------------------------------------------------
// IoT Core policy attachment — workaround for AWS IoT refusing MQTT
// connections from authenticated Cognito users unless an IoT *Policy* is
// attached to the caller's identity. CDK creates the policy; we attach it on
// demand when the user signs in.
// ---------------------------------------------------------------------------
import { CognitoIdentityClient, GetIdCommand } from '@aws-sdk/client-cognito-identity';
import { IoTClient, AttachPolicyCommand, ListAttachedPoliciesCommand } from '@aws-sdk/client-iot';

const IOT_CLIENT_POLICY = 'smarthome-device-sim-client';

export async function getCognitoIdentityId(): Promise<string> {
  const config = getConfig();
  const idToken = await getIdToken();
  const providerName = `cognito-idp.${config.region}.amazonaws.com/${config.cognitoUserPoolId}`;
  const client = new CognitoIdentityClient({ region: config.region });
  const resp = await client.send(new GetIdCommand({
    IdentityPoolId: config.cognitoIdentityPoolId,
    Logins: { [providerName]: idToken },
  }));
  if (!resp.IdentityId) throw new Error('Cognito GetId returned no identityId');
  return resp.IdentityId;
}

/**
 * Ensures the IoT Core client policy is attached to the caller's Cognito
 * identity. Idempotent — lists attachments first and no-ops if already set.
 * Must run before MQTT connect; otherwise IoT refuses the WebSocket with
 * "Not authorized".
 */
export async function ensureIotPolicyAttached(): Promise<void> {
  const config = getConfig();
  const credentials = await getAwsCredentials();
  const identityId = await getCognitoIdentityId();
  const iot = new IoTClient({ region: config.region, credentials });
  try {
    const existing = await iot.send(new ListAttachedPoliciesCommand({ target: identityId }));
    if ((existing.policies || []).some((p) => p.policyName === IOT_CLIENT_POLICY)) return;
  } catch {
    // Fall through to attach — List may be denied in some configurations
    // even when Attach is allowed.
  }
  await iot.send(new AttachPolicyCommand({ policyName: IOT_CLIENT_POLICY, target: identityId }));
}

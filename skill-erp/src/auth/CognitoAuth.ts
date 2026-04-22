import {
  CognitoUserPool,
  CognitoUser,
  AuthenticationDetails,
  CognitoUserSession,
} from 'amazon-cognito-identity-js';
import { jwtDecode } from 'jwt-decode';
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

interface CognitoIdTokenPayload {
  email?: string;
  sub?: string;
  'cognito:username'?: string;
  [key: string]: any;
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
      onFailure(err: Error) {
        reject(err);
      },
    });
  });
}

export function signUp(email: string, password: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const userPool = getUserPool();
    userPool.signUp(email, password, [], [], (err) => {
      if (err) {
        reject(err);
        return;
      }
      resolve();
    });
  });
}

export function confirmSignUp(email: string, code: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const userPool = getUserPool();
    const cognitoUser = new CognitoUser({ Username: email, Pool: userPool });
    cognitoUser.confirmRegistration(code, true, (err) => {
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

export async function getCurrentUserEmail(): Promise<string> {
  try {
    const token = await getIdToken();
    const decoded = jwtDecode<CognitoIdTokenPayload>(token);
    return decoded.email || decoded['cognito:username'] || decoded.sub || '';
  } catch {
    return '';
  }
}

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
  'cognito:groups'?: string[];
  'cognito:username'?: string;
  email?: string;
  sub?: string;
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

/**
 * Force a Cognito token refresh on page load/reload so the rest of the
 * session runs on a newly-issued idToken.
 */
export function refreshSession(): Promise<AuthTokens> {
  return new Promise((resolve, reject) => {
    const userPool = getUserPool();
    const currentUser = userPool.getCurrentUser();
    if (!currentUser) {
      reject(new Error('No current user'));
      return;
    }
    currentUser.getSession((err: Error | null, session: CognitoUserSession | null) => {
      if (err || !session) {
        reject(err || new Error('No session'));
        return;
      }
      currentUser.refreshSession(session.getRefreshToken(), (rerr, rsession) => {
        if (rerr || !rsession) {
          reject(rerr || new Error('Refresh failed'));
          return;
        }
        resolve(sessionToTokens(rsession));
      });
    });
  });
}

export async function getIsAdmin(): Promise<boolean> {
  try {
    const token = await getIdToken();
    const decoded = jwtDecode<CognitoIdTokenPayload>(token);
    return decoded['cognito:groups']?.includes('admin') ?? false;
  } catch {
    return false;
  }
}

export async function getCurrentUserEmail(): Promise<string> {
  try {
    const token = await getIdToken();
    const decoded = jwtDecode<CognitoIdTokenPayload>(token);
    return decoded.email || decoded['cognito:username'] || '';
  } catch {
    return '';
  }
}

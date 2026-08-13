import { getIdToken } from '../auth/CognitoAuth';
import { getConfig } from '../config';

/**
 * The caller's recent conversation, read back from AgentCore Memory on login.
 *
 * Why this exists: AgentCore short-term memory is scoped to
 * `(memoryId, actorId, sessionId)`, and the chatbot mints a fresh runtime session
 * id on every login. So the transcript was there all along and the chat window
 * always opened empty — the user refreshed the page and their conversation
 * appeared to be gone.
 *
 * `GET /sessions?action=history` rather than a route of its own, for the same
 * reason as `feedback.ts`: the admin Lambda dispatches user-facing session actions
 * by query param, which keeps the handler ahead of its admin gate. Ordinary users
 * are the ones who need their own history.
 *
 * There is deliberately NO userId parameter. The actor comes from the verified JWT
 * claims server-side; a userId here would let any signed-in user read anyone
 * else's conversation, and it would look like a convenience.
 */

export interface HistoryTurn {
  role: 'user' | 'assistant';
  text: string;
  timestamp: string;
}

export interface ChatHistory {
  turns: HistoryTurn[];
  /** How many login sessions this had to reach back through. A transcript
   *  stitched from four logins is not one conversation, and the UI says so. */
  sessionsRead: number;
}

function adminApiBase(): string {
  const url = getConfig().adminApiUrl;
  return url.endsWith('/') ? url.slice(0, -1) : url;
}

export async function fetchChatHistory(limit = 20): Promise<ChatHistory> {
  const base = adminApiBase();
  if (!base) return { turns: [], sessionsRead: 0 };
  const token = await getIdToken();
  const res = await fetch(
    `${base}/sessions?action=history&limit=${encodeURIComponent(String(limit))}`,
    { headers: { Authorization: token } },
  );
  if (!res.ok) {
    // Non-fatal by design: an empty chat window is a working chat window, and
    // blocking the first turn on an optional convenience would be worse than
    // showing no history.
    throw new Error(`history unavailable (${res.status})`);
  }
  const body = await res.json();
  return {
    turns: Array.isArray(body.turns) ? body.turns : [],
    sessionsRead: Number(body.sessionsRead) || 0,
  };
}

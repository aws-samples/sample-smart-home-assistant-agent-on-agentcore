import { getIdToken } from '../auth/CognitoAuth';
import { getConfig } from '../config';

/**
 * Submitting a thumbs up/down on one agent turn.
 *
 * Posts to `/sessions?action=feedback` rather than a route of its own: the admin
 * Lambda dispatches user-facing session actions by query param, and that keeps
 * the handler ahead of its admin gate — the voters here are ordinary users.
 *
 * `agentDim` carries the turn's delegation trace, which the chatbot already
 * collects for the "asking the …" indicator. It is what lets the dashboard
 * answer "which specialist draws the 👎", a question the mock satisfaction card
 * it replaces could never answer.
 */

export interface FeedbackVote {
  userId: string;
  vote: 'up' | 'down';
  turnId: string;
  sessionId?: string;
  reason?: string;
  turnPrompt?: string;
  agentDim?: string[];
}

function adminApiBase(): string {
  const url = getConfig().adminApiUrl;
  return url.endsWith('/') ? url.slice(0, -1) : url;
}

export async function submitFeedback(vote: FeedbackVote): Promise<void> {
  const token = await getIdToken();
  const res = await fetch(`${adminApiBase()}/sessions?action=feedback`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(vote),
  });
  if (!res.ok) {
    // Surfaced to the caller so the UI can revert its optimistic highlight. A
    // vote that silently failed would leave the user believing they were heard.
    const detail = await res.text().catch(() => '');
    throw new Error(`feedback failed: ${res.status} ${detail.slice(0, 200)}`);
  }
}

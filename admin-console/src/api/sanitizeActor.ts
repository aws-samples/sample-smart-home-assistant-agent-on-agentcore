// Mirror of agent/memory/session.py::_sanitize_actor_id so the admin console
// can resolve raw Cognito identifiers (email or sub) to the form that
// AgentCore Memory actually stores.
export function sanitizeActorId(id: string): string {
  let s = id.replace(/[^a-zA-Z0-9_/-]/g, '_');
  if (!s || !/^[a-zA-Z0-9]/.test(s)) s = 'u' + s;
  return s;
}

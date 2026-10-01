"""Shared code for the sample A2A agents.

- agents: the agent roster (names, slugs) — one definition,
  imported by deploy / teardown / demo_reset / smoke_test.
- server: assembles the Strands agent behind AgentCore's A2A app, rebuilds tools
  per request from the verified caller, and enforces the skill grant.
- user_identity: verifies the end user's idToken forwarded on an A2A hop
  (signature via JWKS, issuer, audience, token_use, expiry).
- card: AgentCard JSON rendering helpers.
"""

"""Shared code for the sample A2A agents.

- agents: the agent roster (names, slugs, Cognito identifiers) — one definition,
  imported by deploy / teardown / demo_reset / smoke_test.
- server: assembles the Strands agent behind AgentCore's A2A app, rebuilds tools
  per request from the verified caller, and enforces the skill grant.
- user_identity: verifies the end user's idToken forwarded on an A2A hop
  (signature via JWKS, issuer, audience, token_use, expiry).
- card: AgentCard JSON rendering helpers.
- jwt_verify: a Starlette middleware for the INBOUND m2m access token. NOT
  mounted, and the previous version of this docstring claiming otherwise was
  wrong: the Runtime's own CUSTOM_JWT authorizer validates that token before a
  request reaches this container, so mounting this would re-verify what the
  platform already checked. Kept as the reference for what that check covers.
  `user_identity` is the module that actually runs, and it covers what the
  platform cannot — which end user is behind a service-to-service call.
"""

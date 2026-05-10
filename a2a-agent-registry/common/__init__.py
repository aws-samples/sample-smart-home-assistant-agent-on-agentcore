"""Shared code for the three sample A2A agents.

- server: AgentExecutor + entrypoint wiring that plugs a Strands agent into
  Bedrock AgentCore's built-in A2A Starlette app.
- card: AgentCard JSON rendering helpers.
- jwt_verify: Cognito JWKS-backed JWT verifier used as a Starlette middleware.
"""

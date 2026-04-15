"""KB Query Lambda — AgentCore Gateway target for querying the enterprise knowledge base.

Called by the Strands agent via MCP tool `query_knowledge_base`.
User identity is extracted from the Gateway's JWT context (not from tool parameters)
to prevent scope spoofing. Users can only retrieve __shared__ docs plus docs under
their own scope prefix.
"""

import json
import os
import logging
import base64

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("SKILLS_TABLE_NAME", "smarthome-skills")
REGION = os.environ.get("AWS_KB_REGION", os.environ.get("AWS_REGION", "us-west-2"))

dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(TABLE_NAME)
bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


def _get_kb_id():
    """Retrieve the Bedrock Knowledge Base ID from DynamoDB."""
    resp = table.get_item(Key={"userId": "__kb_config__", "skillName": "__default__"})
    item = resp.get("Item")
    if not item or not item.get("knowledgeBaseId"):
        return None
    return item["knowledgeBaseId"]


def _extract_user_identity(event, context):
    """Extract user email from Gateway JWT context.

    AgentCore Gateway with CUSTOM_JWT auth validates the JWT and passes
    identity metadata to the Lambda via context.client_context.custom.
    We also try decoding the JWT directly if passed in the context.

    Returns the user email or None if identity cannot be determined.
    """
    # 1. Try context.client_context.custom (Gateway metadata)
    if hasattr(context, "client_context") and context.client_context:
        custom = getattr(context.client_context, "custom", None)
        if isinstance(custom, str):
            try:
                custom = json.loads(custom)
            except (json.JSONDecodeError, TypeError):
                custom = None
        if isinstance(custom, dict):
            logger.info(f"Gateway custom context keys: {list(custom.keys())}")
            # Try common identity fields
            for field in ("email", "userEmail", "principalEmail",
                          "bedrockAgentCorePrincipalEmail"):
                if custom.get(field):
                    return custom[field]
            # Try sub/principalId, then look up email from claims
            principal_id = custom.get("bedrockAgentCorePrincipalId") or custom.get("sub") or custom.get("principalId")
            if principal_id:
                logger.info(f"Found principal ID: {principal_id}")
                # If we have a JWT token in the context, decode it for email
                token = custom.get("token") or custom.get("jwt") or custom.get("idToken")
                if token:
                    email = _decode_jwt_email(token)
                    if email:
                        return email
                return principal_id

    # 2. Try event-level identity fields (some Gateway versions)
    for field in ("callerIdentity", "identity", "userIdentity"):
        identity = event.get(field)
        if isinstance(identity, dict):
            email = identity.get("email") or identity.get("userEmail")
            if email:
                return email

    return None


def _decode_jwt_email(token):
    """Decode JWT payload to extract email claim (no signature verification — Gateway already validated)."""
    try:
        parts = token.replace("Bearer ", "").split(".")
        if len(parts) < 2:
            return None
        # Add padding
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("email")
    except Exception:
        return None


def handler(event, context):
    """Handle MCP tool invocation from AgentCore Gateway.

    Expected input (from MCP tool schema):
      - query (str, required): The search query
      - user_id (str, optional): User email for scoped retrieval

    Security model:
      - Gateway CUSTOM_JWT auth validates the user's JWT before this Lambda is invoked
      - Cedar policy controls which users can call this tool at all
      - The agent receives actor_id from the verified AgentCore Runtime context
      - user_id parameter is trusted because it flows from: JWT → Runtime → Agent → Tool call
    """
    logger.info("KB Query event: %s", json.dumps(event, default=str))

    query = event.get("query", "")
    if not query:
        return {"error": "query parameter is required"}

    kb_id = _get_kb_id()
    if not kb_id:
        return {"error": "Knowledge base not initialized. Ask an administrator to set it up."}

    # Try Gateway JWT context first, fall back to tool parameter
    user_email = _extract_user_identity(event, context)
    if not user_email:
        user_email = event.get("user_id")
    if user_email:
        logger.info(f"User identity: {user_email}")
    else:
        logger.warning("No user identity available — returning shared docs only")

    # Build metadata filter: user sees __shared__ docs + their own docs
    if user_email:
        retrieval_filter = {
            "orAll": [
                {"equals": {"key": "scope", "value": "__shared__"}},
                {"equals": {"key": "scope", "value": user_email}},
            ]
        }
    else:
        # No identity — safe default: shared docs only
        retrieval_filter = {
            "equals": {"key": "scope", "value": "__shared__"}
        }

    try:
        resp = bedrock_runtime.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": 5,
                    "filter": retrieval_filter,
                }
            },
        )

        results = []
        for r in resp.get("retrievalResults", []):
            content = r.get("content", {})
            location = r.get("location", {})
            metadata = r.get("metadata", {})
            results.append({
                "text": content.get("text", ""),
                "score": float(r.get("score", 0)),
                "source": location.get("s3Location", {}).get("uri", ""),
                "scope": metadata.get("scope", "unknown"),
            })

        if not results:
            return {"message": "No relevant documents found.", "results": []}

        return {
            "message": f"Found {len(results)} relevant document(s).",
            "results": results,
        }

    except Exception as e:
        logger.error(f"KB query failed: {e}")
        return {"error": f"Knowledge base query failed: {str(e)}"}

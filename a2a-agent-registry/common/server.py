"""A2A server entrypoint for sample agents (Strands A2AServer on port 9000).

Usage (from an agent-specific ``agent.py``):

    from common.server import run_agent
    run_agent(
        system_prompt_path="system_prompt.md",
        card_json_path="card.json",
    )

AgentCore Runtime A2A contract (from AWS docs):
  - Port 9000
  - Mounted at ``/`` (not ``/invocations``)
  - Exposes AgentCard at ``/.well-known/agent-card.json``
  - Reads ``AGENTCORE_RUNTIME_URL`` env var for the card.url
"""

from __future__ import annotations

import json
import logging
import os
import uvicorn
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _build_strands_agent(system_prompt: str, model_id: str, name: str, description: str):
    from strands import Agent
    from strands.models.bedrock import BedrockModel

    model = BedrockModel(
        model_id=model_id,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    return Agent(
        name=name,
        description=description,
        model=model,
        system_prompt=system_prompt,
    )


def _build_skills(card_dict: dict[str, Any]):
    from a2a.types import AgentSkill
    return [
        AgentSkill(
            id=s["id"],
            name=s.get("name", s["id"]),
            description=s.get("description", ""),
            tags=list(s.get("tags") or []),
            examples=list(s.get("examples") or []),
        )
        for s in card_dict.get("skills", [])
    ]


def run_agent(system_prompt_path: str, card_json_path: str, port: int = 9000) -> None:
    """Load config files and start the A2A server. Blocks until killed."""
    logging.basicConfig(level=logging.INFO)

    sp = Path(system_prompt_path)
    cj = Path(card_json_path)
    if not sp.is_absolute() or not cj.is_absolute():
        base = Path(__file__).resolve().parent.parent
        if not sp.is_absolute():
            sp = base / sp
        if not cj.is_absolute():
            cj = base / cj

    card_dict = json.loads(cj.read_text(encoding="utf-8"))
    system_prompt = sp.read_text(encoding="utf-8")
    model_id = (
        os.environ.get("MODEL_ID")
        or card_dict.get("defaultModelId")
        or "us.amazon.nova-lite-v1:0"
    )
    runtime_url = os.environ.get("AGENTCORE_RUNTIME_URL", f"http://127.0.0.1:{port}/")

    strands_agent = _build_strands_agent(
        system_prompt=system_prompt,
        model_id=model_id,
        name=card_dict["name"],
        description=card_dict.get("description", ""),
    )

    from strands.multiagent.a2a import A2AServer

    a2a_server = A2AServer(
        agent=strands_agent,
        http_url=runtime_url,
        serve_at_root=True,
        skills=_build_skills(card_dict),
        version=card_dict.get("version", "1.0.0"),
    )

    from fastapi import FastAPI
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"status": "healthy"}

    app.mount("/", a2a_server.to_fastapi_app())

    host = "0.0.0.0" if (os.path.exists("/.dockerenv") or os.environ.get("DOCKER_CONTAINER")) else "127.0.0.1"
    uvicorn.run(app, host=host, port=port, log_level="info")

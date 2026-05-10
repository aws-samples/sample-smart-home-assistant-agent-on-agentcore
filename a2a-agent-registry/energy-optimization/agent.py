"""Entrypoint for the energy-optimization-agent A2A server."""

from common.server import run_agent


if __name__ == "__main__":
    run_agent(
        system_prompt_path="energy-optimization/system_prompt.md",
        card_json_path="energy-optimization/card.json",
    )

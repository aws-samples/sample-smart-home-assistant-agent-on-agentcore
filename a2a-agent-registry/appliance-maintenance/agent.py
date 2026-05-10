"""Entrypoint for the appliance-maintenance-agent A2A server."""

from common.server import run_agent


if __name__ == "__main__":
    run_agent(
        system_prompt_path="appliance-maintenance/system_prompt.md",
        card_json_path="appliance-maintenance/card.json",
    )

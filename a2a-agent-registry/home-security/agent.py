"""Entrypoint for the home-security-agent A2A server."""

from common.server import run_agent


if __name__ == "__main__":
    run_agent(
        system_prompt_path="home-security/system_prompt.md",
        card_json_path="home-security/card.json",
    )

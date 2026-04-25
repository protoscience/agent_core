"""Generic `build_options()` — applications pass in their tool list and prompt.

The framework wraps it with: per-sender memory preamble (when agent_name is
set), MCP server registration, allowed-tool list derivation, and standard
SDK options.
"""
from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

from agent_core.tools import memory as memory_mod


def _tool_name(t) -> str:
    return t.name if hasattr(t, "name") else t.__name__


def build_options(
    *,
    system_prompt: str,
    tools: list,
    agent_name: str | None = None,
    sender_key: str | None = None,
    sender_name: str | None = None,
    mcp_server_name: str = "tools",
    permission_mode: str = "acceptEdits",
) -> ClaudeAgentOptions:
    """Build SDK options for a single agent session.

    Args:
        system_prompt: The base system prompt for the agent.
        tools: List of `@tool`-decorated functions to expose to the agent.
        agent_name: When set, enables per-sender memory and soul prompts.
            The framework looks up the soul file at `<prompts>/<agent_name>_soul.md`
            and injects per-sender memory facts as a preamble.
        sender_key: Stable identity for the current user (e.g. WhatsApp E.164,
            "discord:<user_id>"). Used as the memory bucket key.
        sender_name: Display name (for greeting tone and memory headers).
        mcp_server_name: Logical name for the registered MCP server. Tool
            allow-list is derived as `mcp__<name>__<tool_name>`.
        permission_mode: SDK permission mode. Default "acceptEdits" lets
            agents call tools without per-call approval (the framework
            enforces application-level confirmation via the confirm callback).
    """
    prompt = system_prompt
    if agent_name:
        preamble = memory_mod.build_preamble(agent_name, sender_key, sender_name)
        if preamble:
            prompt = preamble + "\n\n---\n\n" + system_prompt

    server = create_sdk_mcp_server(
        name=mcp_server_name,
        version="1.0.0",
        tools=tools,
    )
    return ClaudeAgentOptions(
        system_prompt=prompt,
        mcp_servers={mcp_server_name: server},
        allowed_tools=[f"mcp__{mcp_server_name}__{_tool_name(t)}" for t in tools],
        permission_mode=permission_mode,
    )

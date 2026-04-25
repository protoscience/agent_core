"""agent_core — reusable framework for Claude agents on WhatsApp + Discord.

Public API:
    build_options(system_prompt, tools, agent_name=None, ...) -> ClaudeAgentOptions
    run_whatsapp_bridge(build_opts, port=4000)
    run_discord(build_opts)
    active_agent, active_sender   # ContextVars for tool implementations
    IMAGE_MARKER                  # marker tools use to attach generated images

Built-in tools (opt in by including in your tools= list):
    from agent_core.builtin_tools import search_web, remember,
        recall_about_me, create_analysis_image
"""

__version__ = "0.1.0"


from agent_core.context import active_agent, active_sender, IMAGE_MARKER
from agent_core.options import build_options
from agent_core.bridge import run_whatsapp_bridge
from agent_core.discord_runner import run_discord


__all__ = [
    "active_agent",
    "active_sender",
    "IMAGE_MARKER",
    "build_options",
    "run_whatsapp_bridge",
    "run_discord",
]

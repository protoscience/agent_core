"""Per-turn context shared between the runner and tool implementations.

The bridge / discord runner sets these before each `client.query()`; tools
like `remember` / `recall_about_me` read them to scope behaviour to the
current agent + sender.
"""
from contextvars import ContextVar


active_agent: ContextVar[str | None] = ContextVar("active_agent", default=None)
active_sender: ContextVar[str | None] = ContextVar("active_sender", default=None)


IMAGE_MARKER = "SAVED_IMAGE::"

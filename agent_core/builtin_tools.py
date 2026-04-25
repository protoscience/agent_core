"""@tool decorators for tools the framework provides.

These are generic — applications can include or omit them by passing them
into `build_options(tools=[...])`. Nothing here is domain-specific.
"""
from claude_agent_sdk import tool

from agent_core.context import active_agent, active_sender, IMAGE_MARKER
from agent_core.tools import imagegen_rich, memory as memory_mod, search as search_tool


@tool(
    "search_web",
    "Search the web via SearXNG. Returns recent articles, news, and pages.",
    {"query": str, "max_results": int},
)
async def search_web(args):
    results = await search_tool.search_web(
        args["query"], max_results=args.get("max_results", 5)
    )
    return {"content": [{"type": "text", "text": str(results)}]}


@tool(
    "remember",
    "Save a durable fact about the person you're talking to. Use when they share "
    "a preference, holding, style, constraint, or anything worth recalling in "
    "future conversations. Keep each fact short and specific. Silent operation — "
    "the user does not see confirmation.",
    {"fact": str},
)
async def remember(args):
    agent = active_agent.get()
    sender = active_sender.get()
    if not agent or not sender:
        return {"content": [{"type": "text", "text": "memory: no active sender"}]}
    ok = memory_mod.append_fact(agent, sender, args.get("fact", ""))
    return {"content": [{"type": "text", "text": "noted" if ok else "already known"}]}


@tool(
    "recall_about_me",
    "Return what you remember about the current user. Use when they ask "
    "'what do you remember about me?', 'what do you know about me?', or similar. "
    "Returns the raw memory markdown for you to format conversationally.",
    {},
)
async def recall_about_me(args):
    agent = active_agent.get()
    sender = active_sender.get()
    if not agent or not sender:
        return {"content": [{"type": "text", "text": "No active sender context."}]}
    mem = memory_mod.load_memory(agent, sender)
    if not mem:
        return {"content": [{"type": "text", "text": "(no memory saved yet)"}]}
    return {"content": [{"type": "text", "text": mem}]}


@tool(
    "create_analysis_image",
    """Render a rich shareable analysis card PNG. Use this to present research,
ideas, summaries, or reviews as a visual summary.

Fields:
  symbol (required): main subject identifier
  name: full subject name
  price: current value
  change_pct: today's percent change (e.g. 1.23 or -0.87)
  verdict: one of BULLISH, BEARISH, NEUTRAL, HOLD, WATCH, CAUTION, BUY, SELL
  headline: one-line summary next to the verdict badge
  metrics: list of {label, value, kind?}  (kind: "up"|"down" colors value)
  sections: list of {icon, title, kind?, bullets}
            kind: "bull"|"bear"|"risk"|"" — colors the left accent bar
            bullets: list of strings OR list of {icon, text} for per-bullet icons
  warnings: list of strings — shown in a red-outlined risk panel at the bottom

Keep bullets short (one line ideal). Prefer 2-4 sections with 2-5 bullets each.""",
    {
        "symbol": str,
        "name": str,
        "price": float,
        "change_pct": float,
        "verdict": str,
        "headline": str,
        "metrics": list,
        "sections": list,
        "warnings": list,
    },
)
async def create_analysis_image(args):
    path = await imagegen_rich.render_analysis_image(
        symbol=args["symbol"],
        name=args.get("name"),
        price=args.get("price"),
        change_pct=args.get("change_pct"),
        verdict=args.get("verdict"),
        headline=args.get("headline"),
        metrics=args.get("metrics") or [],
        sections=args.get("sections") or [],
        warnings=args.get("warnings") or [],
    )
    return {"content": [{"type": "text", "text": f"{IMAGE_MARKER}{path}"}]}

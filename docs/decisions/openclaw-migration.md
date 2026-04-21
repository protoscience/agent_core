# OpenClaw migration

**Status:** Paused — waiting on upstream fix in OpenClaw 2026.4.19+.

## Context

The WhatsApp path goes: WhatsApp → OpenClaw (on VPS) → reverse SSH tunnel → `whatsapp_bridge.py` (FastAPI OpenAI-compat) → Claude Agent SDK. Two capabilities were broken at different times on different OpenClaw releases.

### 2026-04 CLI media bug (now fixed)

On OpenClaw 2026.4.9, `openclaw message send --media <path-or-url>` silently dropped images — it accepted the argument, returned a successful messageId, but no image ever reached the recipient. Verified with local paths, `file://` URLs, and public HTTPS URLs. Gateway send returned in ~15 ms, far too fast for a real media upload.

Root cause: [issue #64478](https://github.com/openclaw/openclaw/issues/64478) — gateway-side WhatsApp send missed the `mediaLocalRoots` fix from an earlier security patch. Fixed by [PR #64492](https://github.com/openclaw/openclaw/pull/64492), shipped in **2026.4.10**.

**Upgraded to 2026.4.14** on 2026-04-20. Scratch-tested the fix end-to-end (pair, render PNG, `message send --media` → image arrived). CLI media is now working.

### Remaining blocker — LLM-generated media in WhatsApp replies

OpenClaw calls our bridge as a pure OpenAI-compatible LLM endpoint. The request contains **no sender identity** — `user=None` on the body, no identifying HTTP headers. We confirmed this by instrumenting the bridge and capturing one real request.

Consequence: the bridge cannot decide to ship a media attachment out-of-band because it doesn't know which WhatsApp number to target.

OpenClaw's intended mechanism is inline `MEDIA:/abs/path` markers in the LLM reply that OpenClaw auto-attaches — tracked as [issue #66635](https://github.com/openclaw/openclaw/issues/66635), still failing in 2026.4.14 for LLM-path replies (works for CLI). Expected fix in 2026.4.19+.

## Decision matrix considered

| Option | WhatsApp speed | LLM → media | Cost | Effort |
|---|---|---|---|---|
| Stay on OpenClaw | Fast (Baileys native) | Blocked pending 2026.4.19+ | Free | 0 |
| WAHA Free (WEBJS/Puppeteer) | Slow (per padhu0626 repro) | Not available in free tier | Free | Medium |
| WAHA Plus (~$19/mo donation) | Fast (NOWEB/GOWS) | Works | $19/mo | Medium |
| Extend our own `baileys/gateway.js` | Fast (under our control) | Works | Free | ~1 afternoon |

WAHA Free is ruled out — loses on both speed and media. If we migrate, it's WAHA Plus or DIY Baileys.

## Decision

**Option 1 — wait.** On 2026.4.14 everything currently shipping (text replies, CLI-invoked weather card) works. When 2026.4.19+ lands, verify inline-MEDIA LLM replies work; if yes, we just add a Sonic prompt line that allows `MEDIA:` markers on explicit user request. If no, revisit WAHA migration.

## Related open concern

Claude auth is subscription OAuth (`CLAUDE_CODE_USE_SUBSCRIPTION=1`). Anthropic's published ToS (code.claude.com/docs/en/authentication, /headless) prohibits using subscription OAuth for agents serving other users — the Agent SDK is explicitly named. Active enforcement began early 2026. Migration to `ANTHROPIC_API_KEY` is a two-line `.env` change; deferred by choice after risk flagged.

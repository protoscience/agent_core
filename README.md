# Business Magnet - AI Trading Research Agent

[![Security](https://github.com/protoscience/agent_core/actions/workflows/security.yml/badge.svg)](https://github.com/protoscience/agent_core/actions/workflows/security.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/protoscience/agent_core/badge)](https://scorecard.dev/viewer/?uri=github.com/protoscience/agent_core)

A Claude-powered trading research agent with Discord and WhatsApp interfaces. Uses Alpaca for paper trading and market data, SearXNG for web search, and generates rich visual analysis cards.

## Security

Every push and PR to `main` runs:

- **CodeQL** (Python SAST, security-and-quality queries)
- **Bandit** (Python-specific security linter, results uploaded as SARIF)
- **pip-audit** (CVE scan on `requirements.txt` — build fails on known vulnerabilities)
- **Gitleaks** (secret scanner across full history)
- **OpenSSF Scorecard** (public security-posture score, weekly)

Dependencies are kept current via **Dependabot** (weekly PRs for pip packages and GitHub Actions). Scan results are visible under the repo's *Security* tab; the badges above show the latest status.

## Architecture

```
Discord ──► Discord Bot (full trading + research)
                │
                ├──► Claude Agent SDK ──► Alpaca (paper trading)
                │                    ──► SearXNG (web search)
                │                    ──► Playwright (image gen)
                │
                └──► logs/cost.db (per-turn spend)

WhatsApp ──► OpenClaw ──► [ optional SSH tunnel ] ──► WA Bridge (research only)
                                                          │
                                                          ├──► Claude Agent SDK
                                                          │         ──► Alpaca
                                                          │         ──► SearXNG
                                                          │
                                                          └──► logs/cost.db

Shared:
  logs/cost.db  ◄── trading-cost-rollup.timer  (nightly: raw→daily→weekly, prune)
                    view with: python -m tools.cost_summary
```

**Discord**: Full access — stock quotes, bars, options chains, web search, analysis cards, price charts, and paper order placement (with button confirmation).

**WhatsApp**: Research only — no account info, no positions, no order placement. Designed for group discussions.

**Cost tracking**: Both services log `(channel, peer, turns, cost_usd)` to a local SQLite DB on every `ResultMessage`. A nightly systemd timer rolls raw turns into `daily`, rebuilds `weekly` from `daily`, and prunes daily rows older than a year.

## Deployment options

The WhatsApp path supports two layouts. Pick whichever matches your environment — the code is identical, only the OpenClaw location and the tunnel change.

|   | **Option A — same machine** | **Option B — VPS (recommended for 24/7)** |
|---|---|---|
| OpenClaw runs on | Your local PC | A remote VPS |
| WA bridge runs on | Your local PC | Your local PC |
| Reverse SSH tunnel | Not needed | Required (`-R 4000:127.0.0.1:4000`) |
| Extra systemd units | none | `trading-ssh-tunnel` + `trading-tunnel-healthcheck` |
| WhatsApp session stays online when | your local PC is online | the VPS is online (typically 24/7) |
| When to pick it | Always-on desktop/home server; simpler setup; no VPS cost | You want the WhatsApp session to survive your laptop sleeping / rebooting; willing to run a small VPS |

The rest of the setup (Alpaca keys, SearXNG, Discord bot, cost tracking) is identical either way.

## Prerequisites

- Python 3.12+
- Node.js 22+ (for OpenClaw on VPS)
- Docker (for SearXNG)
- Claude Code CLI with active subscription (`claude` must be logged in)
- [Alpaca](https://alpaca.markets/) paper trading account
- [Discord](https://discord.com/developers/applications) bot token

## Quick Setup

### 1. Clone and configure

```bash
git clone git@github.com:protoscience/agent_core.git
cd agent_core

cp .env.example .env
# Fill in your API keys in .env
```

### 2. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 3. SearXNG (web search)

```bash
sudo docker compose up -d
```

This starts SearXNG on `127.0.0.1:8080` (localhost only).

### 4. Run Discord bot (manual)

```bash
source .venv/bin/activate
python discord_bot.py
```

### 5. Run as systemd services (recommended)

Create these service files in `~/.config/systemd/user/`:

**trading-discord.service**
```ini
[Unit]
Description=Trading Agent - Discord Bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/agent_core
EnvironmentFile=/path/to/agent_core/.env
ExecStart=/path/to/agent_core/.venv/bin/python discord_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

**trading-wa-bridge.service**
```ini
[Unit]
Description=Trading Agent - WhatsApp Bridge
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/agent_core
EnvironmentFile=/path/to/agent_core/.env
Environment=BRIDGE_TOKEN=<generate-with-python3 -c "import secrets; print(secrets.token_hex(24))">
Environment=BRIDGE_PORT=4000
ExecStart=/path/to/agent_core/.venv/bin/python whatsapp_bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

**trading-ssh-tunnel.service** — *Option B (VPS) only; skip this file for Option A*
```ini
[Unit]
Description=Trading Agent - Reverse SSH Tunnel to VPS
After=network-online.target

[Service]
Type=simple
Environment=AUTOSSH_GATETIME=0
ExecStart=/usr/bin/autossh -M 0 -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -R 4000:127.0.0.1:4000 user@your-vps-host
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

**trading-watcher.service** (auto-restart on code changes)
```ini
[Unit]
Description=Trading Agent - File Watcher
After=trading-discord.service trading-wa-bridge.service

[Service]
Type=simple
ExecStart=/path/to/agent_core/watch-restart.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Enable and start:
```bash
# Allow services to run after logout
sudo loginctl enable-linger $USER

# For Option B (VPS): install autossh. inotify-tools is needed either way.
sudo apt install autossh inotify-tools

systemctl --user daemon-reload

# Option A (same machine): skip trading-ssh-tunnel
systemctl --user enable --now trading-discord trading-wa-bridge trading-watcher

# Option B (VPS): include the tunnel
systemctl --user enable --now trading-discord trading-wa-bridge trading-ssh-tunnel trading-watcher
```

## WhatsApp Setup (via OpenClaw)

WhatsApp integration uses [OpenClaw](https://github.com/openclaw/openclaw). Same steps for both deployment options — only **where** you run OpenClaw and whether you need an SSH tunnel differ.

### Install OpenClaw

Install on whichever host is running it (local PC for Option A, VPS for Option B):

```bash
sudo npm install -g openclaw@2026.4.14
```

### Configure and pair WhatsApp

Run these on the same host:

```bash
openclaw configure
openclaw channels login --channel whatsapp
# Scan the QR code with your WhatsApp → Settings → Linked Devices
```

### Point OpenClaw at the bridge

Edit `~/.openclaw/openclaw.json` on the OpenClaw host. The config is identical for both options — the bridge URL is always `http://127.0.0.1:4000` because:

- **Option A** — the bridge is literally on localhost.
- **Option B** — OpenClaw sees the bridge via the reverse SSH tunnel, so `127.0.0.1:4000` on the VPS is actually the bridge running on your local PC.
   ```json
   {
     "env": {
       "BRIDGE_TOKEN": "<same-token-as-wa-bridge-service>"
     },
     "models": {
       "providers": {
         "litellm": {
           "baseUrl": "http://127.0.0.1:4000",
           "apiKey": "${BRIDGE_TOKEN}",
           "api": "openai-completions",
           "models": [{
             "id": "trading-agent",
             "name": "Trading Agent (Claude)",
             "reasoning": false,
             "input": ["text"],
             "contextWindow": 200000,
             "maxTokens": 4096
           }]
         }
       }
     },
     "agents": {
       "defaults": { "model": { "primary": "litellm/trading-agent" } },
       "list": [
         { "id": "main" },
         { "id": "trading", "name": "trading", "model": "litellm/trading-agent" }
       ]
     },
     "bindings": [
       { "agentId": "trading", "match": { "channel": "whatsapp" } }
     ],
     "channels": {
       "whatsapp": {
         "dmPolicy": "allowlist",
         "allowFrom": ["+1XXXXXXXXXX"],
         "groupPolicy": "allowlist",
         "groupAllowFrom": ["+1XXXXXXXXXX"],
         "groups": { "*": { "requireMention": true } },
         "enabled": true
       }
     }
   }
   ```

   **Group behavior:** With `groups."*".requireMention=true`, Sonic only responds in
   group chats when explicitly @-mentioned (native WhatsApp tap-to-mention).
   `groupPolicy: "allowlist"` plus `groupAllowFrom` further restricts *who* can
   trigger a reply — non-allowlisted senders are silently ignored even if they
   @-mention Sonic. DMs use `dmPolicy`/`allowFrom` independently. Per OpenClaw
   docs, replying to a Sonic message satisfies mention gating but does NOT
   bypass the sender allowlist.

### Start the gateway

```bash
openclaw gateway
```

Or run it as a systemd user service so it survives reboots.

### Option B only — set up the reverse SSH tunnel

On your local PC, the `trading-ssh-tunnel.service` (systemd unit shown above) runs:

```bash
autossh -M 0 -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -o ExitOnForwardFailure=yes -R 4000:127.0.0.1:4000 user@your-vps-host
```

This makes the bridge reachable at `127.0.0.1:4000` on the VPS. All traffic is SSH-encrypted.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ALPACA_API_KEY` | Yes | Alpaca paper trading API key |
| `ALPACA_SECRET_KEY` | Yes | Alpaca paper trading secret |
| `ALPACA_PAPER` | Yes | Must be `true` |
| `SEARXNG_URL` | Yes | SearXNG URL (default: `http://localhost:8080`) |
| `CLAUDE_CODE_USE_SUBSCRIPTION` | Yes | Set to `1` for subscription auth |
| `DISCORD_BOT_TOKEN` | Yes | Discord bot token |
| `DISCORD_ALLOWED_USER_IDS` | Yes | Comma-separated Discord user IDs |
| `DISCORD_ALLOWED_CHANNEL_IDS` | No | Comma-separated channel IDs (bot responds without @mention) |

## Security Notes

- All services bind to `127.0.0.1` (localhost only)
- SearXNG is not exposed to the internet
- WhatsApp bridge uses bearer token auth
- SSH tunnel provides encryption between local machine and VPS
- `.env` should be `chmod 600`
- WhatsApp channel is research-only (no trading tools)
- Discord order placement requires button confirmation
- WhatsApp access controlled via OpenClaw allowlist

## Useful Commands

```bash
# Check service status
systemctl --user status trading-discord trading-wa-bridge trading-ssh-tunnel trading-watcher

# Tail logs
journalctl --user -u trading-discord -f

# Manual restart
systemctl --user restart trading-discord

# Reset conversation in Discord
# Type /reset or !reset in Discord
```

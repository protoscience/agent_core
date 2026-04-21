# WhatsApp group summary

**Status:** Parked — designed, not started.

## Context

Users (including the owner) sometimes ask Sonic "summarize what was discussed here today" in a WhatsApp group. Sonic currently and correctly replies that he can only see messages where he was @-mentioned — OpenClaw's `groups.*.requireMention=true` filter means the bridge never receives general group chatter.

## Rejected approaches

**Flip `requireMention=false`.** Sonic would ingest every group message and run it through Claude. Cost scales with group activity:

| Group activity | Added cost/day |
|---|---|
| 20 msgs/day | +$0.05 |
| 200 msgs/day | +$0.50 |
| 500 msgs/day | +$1.25 |

Also breaks UX — Sonic becomes tempted to chime in on conversations not addressed to him.

**Tail OpenClaw gateway logs for message content.** OpenClaw's journal lines only record message **size**, e.g. `[whatsapp] Inbound message ... (group, 2342 chars)`. No content available through log scraping.

## Recommended design — passive logger + on-demand summary

| Part | Approach |
|---|---|
| Capture | Pair a second Baileys-based linked-device session on the local PC (same pattern used for the 2026.4.14 scratch test). It listens to all group messages and writes rows to SQLite, but does not respond. |
| Storage | `logs/group_messages.db`, table `group_messages(ts, group_jid, sender, content, mentioned)`. ~150 bytes/row. Even 5 busy groups stay under ~150 MB/year. |
| Summary tool | New Claude Agent SDK tool `get_group_messages(group_jid, hours=24)`. Sonic calls it on demand when asked to summarize; Claude produces one summary in a single turn (~$0.05–0.15). |
| Retention | 30-day auto-purge via systemd timer, reusing the `trading-cost-rollup.timer` pattern. |

**Estimated build: 4–6 hours**, mostly the Baileys logger pairing and reliability tuning.

## Non-technical blocker

Logging other people's WhatsApp messages is socially and (depending on jurisdiction) legally sensitive. WhatsApp is end-to-end encrypted by design — group members don't consent to a local SQLite log of their chatter. Before shipping:

- Disclose to group members once per group ("fyi my bot logs this group for summaries, opt out and I'll disable it").
- Provide a per-sender or per-group opt-out at the capture layer (skip rows where `sender` is on an opt-out list).
- Consider restricting logging to groups where the owner explicitly opted in, rather than to all allowlisted groups.

Solo use or family-chat use may not need this friction; broader rollout does.

## Decision

**Parked** until there's a repeat ask from multiple group members. Design above is the starting point when we resume.

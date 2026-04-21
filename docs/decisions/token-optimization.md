# Token optimization

**Status:** Parked — researched, not started.

## Context

7-day measured spend on the two live bots: **$51.70 / 525 turns / 7.4 M tokens**, averaging **~$0.10/turn**. Token distribution is the interesting part:

| Bucket | 7-day tokens | % |
|---|---|---|
| Cache-read | 6.76 M | **91 %** |
| Cache-creation | 608 k | 8 % |
| Output | 71 k | ~1 % |
| Input (non-cached) | 933 | ~0 % |

The dominant cost driver is **cached context being re-fed every turn**. Output is a rounding error.

Data comes from `logs/cost.db` (per-turn log with token breakdown), written by both bridge and Discord bot on every `ResultMessage`. The schema already has `input_tokens / output_tokens / cache_read_tokens / cache_creation_tokens` columns.

## Caveman prompt technique — evaluated and rejected for user-facing replies

[Caveman prompting](https://github.com/JuliusBrussee/caveman) (viral April 2026) instructs the LLM to drop articles, fillers, and prose, e.g. *"Me get AAPL. $235. Up 2%."*

- Marketed: 65–75% output-token savings.
- Independent benchmarks (e.g. [Guzik, DEV](https://dev.to/jakguzik/i-benchmarked-the-viral-caveman-prompt-to-save-llm-tokens-then-my-6-line-version-beat-it-2o81)): actual savings **9–21%** on baselines that already say "be concise".
- Only compresses **output** tokens. Our output is 1% of spend, so maximum realistic impact is **~$0.40/week**.
- Breaks the WhatsApp styling we just shipped (bold headlines, emoji palette). Users expect prose.

Decision: **do not apply to user-facing replies.** The technique could work inside a planner-agent / formatter-agent split (reasoning in caveman, formatting in prose), but we don't have that architecture today and the payoff wouldn't justify the rewrite.

## Cheaper levers — try these first, in this order

Each requires measuring against `cost.db` before changing code.

1. **Query `cost.db` to find the hot path.** Split by peer, by time-of-day, by channel. Find which conversations and which tools dominate cache-read volume. Most optimization work is wasted without this.
2. **Trim `search_web` output.** Reduce `max_results` from 5 to 3; truncate each article's returned body to headline + first 2 sentences. Biggest expected impact if search is confirmed hot.
3. **Cap `get_bars` days.** Many queries use 5–10 days but we return 30. Lower default, let Claude request more when needed.
4. **Cache recent ticker lookups.** Same symbol asked twice within 5 min → serve cached quote/bars rather than re-calling Alpaca and re-feeding to Claude.
5. **Caveman in internal reasoning only** (advanced): planner in caveman + formatter in prose. Deferred until #1 proves sub-agent reasoning is expensive.

## Decision

**Parked — measure first.** Next time we resume, start with lever #1 (SQL query on `cost.db`) to confirm where the spend is going before changing anything.

## Caveat

These dollar figures are **estimates from list API pricing**. The bots currently run on subscription OAuth, so actual billing is the subscription tier, not per-token. Numbers would become real the day we migrate to `ANTHROPIC_API_KEY`.

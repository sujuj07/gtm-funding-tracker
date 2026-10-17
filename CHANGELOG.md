# Changelog

## v8 — backfill reliability and hourly cadence
- Scheduled historical funding backfill hourly at minute 10, processing up to 4 candidates per run.
- Scheduled crypto launch backfill hourly at minute 40, processing up to 2 candidates per run.
- Historical RSS requests now check HTTP status and malformed-feed failures.
- Backfills require at least 80% of Google News queries to return valid responses; if partial query failures occur, candidates can be saved but the date window is kept pending for retry.
- Apps Script registry lookup errors now fail closed rather than returning an empty dedupe set.
- Added `workflow_dispatch` input `reset_cursor` to restart the corresponding backfill at 2026-08-01 while preserving source-level deduplication in the hidden Apps Script registry.
- Existing live-monitoring workflows remain on their prior schedules.

## v5 — Automated historical catch-up

- Added daily schedules for funding and crypto historical backfills.
- Scheduled runs use conservative caps (20 funding candidates/day; 10 crypto candidates/day) to manage free-tier model usage.
- Manual workflow dispatch remains available and permits up to 60 candidates per run.
- Backfill end date remains dynamic, so the current UTC date is included automatically.
- Documented that full global coverage is limited by indexed source availability.


## v6 — 10-minute resumable backfill and table styling

- Changed historical catch-up from daily whole-range rescans to one resumable seven-day window per run, scheduled every 10 minutes with funding/crypto runs staggered. Scheduled runs process one candidate at a time to conserve model quota; manual runs process up to 10.
- Persisted each stream cursor in `.backfill-state/*.json` and only advance after the window is processed and webhook writes are confirmed.
- Retried windows with candidate overflow, model extraction errors, or unconfirmed webhook writes.
- Added self-disable at catch-up completion; hourly live collectors remain enabled.
- Aligned Crypto Launch Radar formatting with the Funding Intelligence charcoal header, filters, frozen key columns, readable widths, row fills, confidence highlights and URL styling.

## v2 — October 2026

- Split the system into a global funding/company-intelligence stream and a crypto launch radar stream.
- Added event classifications for funding, company formation/incorporation, reported hiring, and crypto launch types.
- Added broad offerings, niche presence, product type, target customers, more investors, valuation, evidence confidence and last-checked fields.
- Added optional DeFiLlama Raises API and X API v2 recent-search integrations.
- Added hourly GitHub Actions workflows plus separate historical backfill workflows beginning 2026-08-01.
- Reworked the webhook to route records into separate sheet tabs, persist processed sources, deduplicate at event level, and enrich existing events when new sources appear.
- Added source/evidence URLs and explicit limits around historic X account activation and global incorporation coverage.


## Strict crypto launch quality gate
- Excludes NFT/PFP/collectibles-first projects and meme/memecoin projects from both trackers.
- Crypto Launch Radar now requires a substantive launch, a publicly named founder, and an evidence-backed prior shipped Web3 builder track record.
- Adds founder identity/history evidence URLs and a quality-gate decision to the Crypto Launch Radar tab.
- Fixes the historical crypto backfill's `updated` counter scope.


## v4 — Crypto Launch Radar presentation

- Match Crypto Launch Radar to the Funding Intelligence table style.
- Use concise, user-facing column labels while preserving the existing column order.
- Add filter controls, frozen header/name columns, alternating row fills, comfortable widths and wrapped launch/founder/niche context.
- Highlight evidence confidence without adding a speculative project score.
- Hide the internal deduplication key while retaining source URLs and founder/track-record evidence.

- Add a final `Other Official Links` field populated only from links found on the supplied project homepage (no guessed handles).


## v7 — Quota-aware model routing

- Default all GitHub workflows to `openai/gpt-oss-20b` for routine extraction.
- Added automatic fallback to `qwen/qwen3.8-27b`, then `openai/gpt-oss-120b`, when a model is rate-limited or unavailable.
- The shared router observes 429 responses, skips depleted models for the remainder of the current Python process, and logs which model served each request. Crypto candidates remain retryable when the launch-founder verification stage cannot get a response from any model.
- Added `GROQ_MODEL_FALLBACKS` configuration. No Apps Script changes are required for model routing.

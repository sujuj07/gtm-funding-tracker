# Changelog

## v9 — Crypto launch discovery precision
- Replaced broad crypto news searches with launch-focused Google News queries, including targeted crypto press-release sources.
- Added a deterministic launch-signal prefilter to keep funding, executive, price, transfer and general crypto headlines from consuming model tokens unless the headline has a substantive launch cue.
- Log counts of raw sources, launch-like candidates and screened-out low-signal headlines for easier diagnosis.
- Rank strong launch-signal headlines before lower-confidence signals.
- Raise scheduled crypto backfill throughput from 2 to 6 candidates/hour (manual dispatch 8) after low-signal prefiltering.
- Keep strict NFT/meme exclusion and public-founder + prior-shipped-Web3-builder gate unchanged.

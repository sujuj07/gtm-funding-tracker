# Changelog

## v9 — Crypto launch discovery precision
- Replaced broad crypto news searches with launch-focused Google News queries, including targeted crypto press-release sources.
- Added a deterministic launch-signal prefilter to keep funding, executive, price, transfer and general crypto headlines from consuming model tokens unless the headline has a substantive launch cue.
- Log counts of raw sources, launch-like candidates and screened-out low-signal headlines for easier diagnosis.
- Rank strong launch-signal headlines before lower-confidence signals.
- Raise scheduled crypto backfill throughput from 2 to 6 candidates/hour (manual dispatch 8) after low-signal prefiltering.
- Keep strict NFT/meme exclusion and public-founder + prior-shipped-Web3-builder gate unchanged.


## v10 — Founder evidence retrieval and safe crypto recheck

- Fetches the launch article and bounded public HTML from the candidate's official homepage and same-site About/Team/Founder/Leadership pages before applying the strict founder/build-history gate.
- Adds disambiguated launch-title and official-domain searches, plus verification evidence counts and source URLs in Actions logs.
- Adds a one-time manual `recheck_processed_sources` input so previously rejected crypto candidates can be re-evaluated after verification improvements.
- Apps Script only bypasses the processed-source guard when an authenticated crypto POST explicitly asks to recheck; event keys/natural keys continue to prevent duplicate rows.
- Keeps the core quality bar unchanged: substantive launch, non-NFT/non-meme, publicly named founder, and evidence of prior shipped Web3 building.


## v11 — free crypto discovery sources
- Added no-key DeFiLlama `/protocols` listing-date leads to historical and live crypto discovery. These are leads only; the paid `/api/raises` endpoint is not used.
- Added best-effort public X profile timeline enrichment through the unauthenticated syndication page for handles identified from official/project sources. This unofficial endpoint covers recent posts on known profiles only; it is not keyword search or a full archive.
- Verification now asks more directly for independent launch evidence and can use official-site-linked X posts as supporting evidence.
- DeFiLlama `listedAt` is never copied into the sheet as a launch/announcement date.

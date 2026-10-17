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

## v12 — Backfill recheck cap and evidence enrichment
- Fixed the manual `recheck_processed_sources` run cap: when enabled, up to 60 candidates can be evaluated in that run, so the same historical week is not left pending simply because the recheck batch was capped at 8.
- Scheduled backfill cadence and per-run caps remain unchanged (hourly, 6 candidates/run).
- Crypto founder verification probes a limited number of official same-site about/team and blog/news/launch pages and follows up to two official launch links.
- Public GitHub metadata is fetched only for links found on official project pages and remains supplementary evidence, not standalone proof of founder identity.
- Added a concrete Apps Script formatting application step for the Crypto Launch Radar tab.
- Added a code-level guard requiring the selected independent launch evidence page itself to contain an explicit launch cue and Web3 context; registry listings and generic homepages no longer pass as launch evidence based solely on model output.
- Added a `Tracker Tools → Format Crypto Launch Radar` menu to apply sheet styling visibly on demand.


## v13 — RootData Basic, credit-capped enrichment
- Add optional RootData project search and Basic project metadata enrichment for shortlisted launch candidates.
- Enforce name-match confidence; never match ambiguous short names or treat registry metadata as launch/founder proof.
- Cache search/detail responses across workflow runs.
- Enforce account balance floor of 200 credits, tracker cap 800 credits/month, and 24 credits/day; detail records cost 2 credits and search/quota endpoints are used per official docs.
- Use only Basic-safe project description, tags, establishment date, RootData URL, and available social/site links; never request/include teams, investor, or funding-round data.
- Persist the cache/budget file through both crypto workflows; stagger live monitoring to minute 55 UTC to avoid concurrent state updates with backfill at minute 40 UTC.

## v14 — Crypto Review Queue and review retries

- Added a visible `Crypto Review Queue` for plausible candidates that fail the strict founder/track-record or independent-launch-evidence check. NFT/meme-first projects remain excluded.
- Added idempotent queue upserts keyed by project/source date/signal, preserving first-seen timestamps and user-entered manual decisions/notes.
- Extended the Apps Script POST contract with `reviewRows`; processed-source URLs are recorded only after main-sheet and review-queue writes succeed.
- Improved webhook failure diagnostics and buffering/retry behavior for live and historical crypto ingestion. The historical cursor remains pending if any unresolved model/search/write item remains.
- Added daily review rechecks for unresolved candidates last checked at least seven days ago. Qualified records are promoted into `Crypto Launch Radar`; the review row is marked promoted. The recheck job does not receive the RootData API key, so it doesn't spend RootData detail credits.
- Main-sheet launch dates are now written only when the independent evidence review verified an explicit date; DeFiLlama `listedAt` remains a discovery timestamp only.

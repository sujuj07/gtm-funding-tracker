# v10 Crypto Founder Verification Fix

This patch addresses why Crypto Launch Radar remained empty after v9. The run did discover launch-like headlines, but all candidates were either unrelated updates or could not pass the strict founder/prior-builder evidence gate. The verifier previously relied mostly on RSS snippets and Google News result snippets.

## Files changed
- `tracker_common.py`: fetch source article and official project pages, follow same-site About/Team/Founder pages, expand disambiguated founder search queries, and log evidence counts/URLs.
- `backfill_crypto.py`: support an explicit one-time recheck of source URLs already in the processed registry.
- `.github/workflows/backfill-crypto.yml`: adds `recheck_processed_sources` manual input.
- `apps_script/Code.gs`: allows authenticated reprocessed crypto URLs only when the POST explicitly sets `allowReprocessedSources=true`; existing event/source dedupe remains active.

## Deploy
1. Extract this patch into the existing `gtm-funding-tracker` repository folder, replacing these files. Keep `.git` untouched.
2. In the downloaded `apps_script/Code.gs`, preserve the live `SECRET_TOKEN` from your current Apps Script file; do not copy the placeholder over it.
3. Save Apps Script and deploy a **New version** (Deploy → Manage deployments → Edit → New version → Deploy). The Web App URL should remain the same.
4. In GitHub Desktop, commit the changes to `main` and Push origin.
5. GitHub → Actions → `backfill-crypto-launch-radar` → Run workflow. Set **both** `reset_cursor=true` and `recheck_processed_sources=true`. This makes the next window start at 2026-08-01 and re-evaluates sources marked processed by the earlier strict verifier.
6. After that one-time run, leave both inputs false for future manual runs; hourly scheduled runs continue with normal dedupe.

## What to expect
The strict filter is intentionally unchanged. This patch improves available evidence; it does not force rows into the sheet. A source still will not be added if the run cannot verify a named founder and a documented prior shipped Web3 project. The new logs show how many Google News results and official pages were included, with source titles and URLs, so a continued zero-row result can be diagnosed instead of guessed.

The page fetcher uses public HTTP(S) URLs only, bounded response sizes/timeouts, and same-site links for team/about pages. No token or credential value should be committed to GitHub.

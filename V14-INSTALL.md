# v14: Crypto Review Queue + reliable review retries

This patch implements a separate review queue for credible-looking crypto launch leads that do not yet meet the strict main-sheet gate. It also adds a daily review job that retries unresolved candidates after new evidence may have appeared.

## Files in this patch

- `backfill_crypto.py`: writes qualified launches to `Crypto Launch Radar` and plausible-but-unverified leads to `Crypto Review Queue`.
- `crypto_launches.py`: applies the same rule to ongoing live discoveries.
- `tracker_common.py`: transports review rows and exposes an authenticated `review_queue` read.
- `apps_script/Code.gs`: creates/upserts/formats the review queue and only records processed sources after sheet writes succeed.
- `recheck_crypto_review.py`: re-verifies up to five queue entries that have not been checked for at least seven days; it promotes successful entries to the main sheet.
- `.github/workflows/recheck-crypto-review.yml`: schedules the review pass once per day and can be run manually.

## Apply

1. Extract the patch ZIP into the existing repository folder, preserving `.git`.
2. In GitHub Desktop, inspect the changed files, commit to `main`, and push to origin.
3. In the Google Sheet, go to **Extensions → Apps Script**. Replace the editor contents with `apps_script/Code.gs` from the patch. Preserve the exact existing `SECRET_TOKEN` value so it remains identical to GitHub's `WEBHOOK_TOKEN` secret. Save.
4. In Apps Script choose **Deploy → Manage deployments → Edit → New version → Deploy**.
5. Reload the spreadsheet. Use **Tracker Tools → Format Crypto Review Queue** if you want the queue tab created/styled immediately. The tab is also created automatically on the first queued lead.
6. Open GitHub → Actions → `backfill-crypto-launch-radar`. Run it once with `reset_cursor=true` and `recheck_processed_sources=true` to seed the review queue from the current historical week. Since the current cursor is at August 8, the reset intentionally begins again at August 1. Do not leave either option enabled on scheduled runs.
7. Confirm the job logs `review_added` or `review_updated` and open the `Crypto Review Queue` tab. The main `Crypto Launch Radar` sheet remains strict.
8. The daily `recheck-crypto-review-queue` workflow checks unresolved rows at least seven days after their last check. It does not receive `ROOTDATA_API_KEY`, so it does not spend RootData project-detail credits. A manual decision of `Reject`, `Rejected`, `Ignore`, or `Ignored` excludes that row from automatic rechecks.

## Review queue columns

The queue records discovery/source dates separately from verified launch dates, source URLs, available founder names, evidence URLs, missing evidence, confidence, source title/type, a hidden idempotency key, and optional `Manual Decision` / `Reviewer Notes` fields. It preserves the first-seen date and manual fields when rows are refreshed.

## Qualification policy

The main sheet remains closed to NFT/meme-first projects and still requires (1) independent evidence of a substantive product/protocol launch, (2) a publicly named founder, and (3) evidence that a named founder previously shipped a Web3 product/protocol or founded a Web3 company with a real product. Missing or inaccessible evidence goes to the review queue rather than being described as proof the founder has no track record.

## Limits and testing

- Review rechecks process at most five due queue rows each day and wait seven days between checks for the same unresolved candidate.
- Review rechecks use the configured Groq model router and omit RootData detail API credentials.
- Python compilation, Apps Script JavaScript syntax, YAML syntax, review-row schema and review-row reconstruction were checked in a local test harness. This does not authenticate or deploy the code against the live Apps Script/Groq/RootData accounts; check the first live run logs.

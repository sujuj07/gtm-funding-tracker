# v12 hotfix: crypto backfill progress + founder/launch evidence

This patch addresses two observed issues:

1. A manual run with `recheck_processed_sources=true` still had a cap of 8 candidates. The first historical week had 22 launch-like candidates, so the same window stayed pending after each recheck. When the recheck option is enabled, the workflow now allows up to 60 candidates so a normal one-time recheck can finish that week in one run. Scheduled runs remain capped at 6, and ordinary manual runs at 8.
2. Founder verification relied heavily on the launch article and Google News RSS results. It now also probes a limited number of official same-site team/about and blog/news/launch pages, follows a maximum of two same-site launch links from those pages, and reads public GitHub metadata only for GitHub links the official pages actually expose. GitHub metadata is supplementary evidence, never founder proof by itself.

## Install in the existing repo

1. Extract the patch ZIP.
2. Copy these paths into the existing `gtm-funding-tracker` repo, replacing files:
   - `.github/workflows/backfill-crypto.yml`
   - `tracker_common.py`
   - `README.md`
   - `CHANGELOG.md`
3. Commit and push to `main` in GitHub Desktop.
4. For the spreadsheet formatting only, update Apps Script separately using the included `apps_script/Code.gs`:
   - Before pasting, replace `REPLACE_WITH_A_NEW_LONG_RANDOM_SECRET` with the exact `SECRET_TOKEN` value already in your working Apps Script. Keep GitHub's `WEBHOOK_TOKEN` unchanged.
   - Save; Deploy → Manage deployments → Edit → New version → Deploy.
   - Visit the existing Web App URL with `?token=YOUR_TOKEN&stream=crypto&action=format`. Expected JSON contains `"ok":true` and `"message":"sheet formatting applied"`.

## Run the backfill once

Go to GitHub → Actions → `backfill-crypto-launch-radar` → Run workflow. For the one-time catch-up, set both `reset_cursor` and `recheck_processed_sources` to `true`. This run gets the larger cap (60) and can evaluate the whole first-week candidate queue rather than repeatedly processing only the first 8. After that, do not manually set those checkboxes again; scheduled runs use the saved cursor and the normal cap of 6.

## Expected result and limitation

The first week may still append zero rows if all candidates fail the user's strict criteria (real launch, publicly named founder, proven prior Web3 builder, non-NFT/non-meme). That is an evidence result, not a webhook write failure. The cursor should, however, advance once all launch-like candidates in that window are evaluated and writes/registry updates are confirmed. Review the workflow log for `cursor advanced to 2026-08-08` or the next successful window boundary.

## Confirm the sheet style was actually deployed

After updating and deploying `apps_script/Code.gs`, reload the spreadsheet. A `Tracker Tools` menu should appear. Choose **Tracker Tools → Format Crypto Launch Radar**. The blank crypto tab should immediately get the dark header, frozen first row and first two columns, filter, column widths and clean row styling. This is independent of whether qualifying launch rows exist.

## What zero rows means

The backfill intentionally writes only verified launches. It will still write zero rows for the first week if all candidates lack an explicit launch source or a publicly named founder with a prior shipped Web3 product. The v12 code now enforces an explicit launch cue in the chosen evidence source, not just the model's asserted boolean.

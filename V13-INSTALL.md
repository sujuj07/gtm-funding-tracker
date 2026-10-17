# v13 — RootData Basic integration

## What this enables

The crypto workflows now use the RootData Basic API to look up already-shortlisted launch candidates and enrich them with a canonical name, short description, tags, establishment date, RootData profile URL, and public website/social links when available. RootData Basic is not used as proof of a dated launch, a named founder, or prior shipped Web3 work. The existing evidence gate remains strict.

## Configure the API key

1. Open the repository on GitHub.
2. Go to **Settings → Secrets and variables → Actions → New repository secret**.
3. Add secret name `ROOTDATA_API_KEY` and paste the API key from RootData. Do not put the key into code or commit it.
4. Push the updated workflow files to `main`.

Both crypto workflows run `rootdata_check.py` before processing. It uses the free quota endpoint plus one free project search, prints the plan/remaining credits, and never calls `/open/get_item`. If the key is missing or invalid, it warns and the tracker continues without RootData.

No Google Apps Script change is required for the RootData integration.

## Credits and limits

RootData's current API documentation says `/open/ser_inv` search is unlimited (60 requests/minute) and `/open/quotacredits` is free. `/open/get_item` costs 2 credits per request and is supported on Basic. This integration does not use fundraising-round, investor-detail, comprehensive people, team-member, ranking, or Pro-only endpoints. Any returned funding/investor/team fields are explicitly discarded.

Safety guards:
- At most 24 credits/day for this integration (12 uncached project detail requests).
- At most 800 credits/month for this integration, out of the 1,000-credit Basic allowance.
- Keep at least 200 credits in the live RootData account balance; if the quota endpoint cannot confirm balance, skip detail calls and continue without them.
- Cache positive project search matches for 30 days, negative search matches for 24 hours, and project detail records across workflow runs in `.backfill-state/rootdata-state.json`.
- The two crypto workflows share a concurrency group and persist the cache/usage file to the default branch.

The local ledger is a second guard. If you manually use the API outside this tracker, the live account balance check protects the reserve. Do not lower the 200-credit reserve or raise the caps unless you deliberately want to allocate more of the Basic quota to the tracker.

## Apply files

Copy the files in the v13 patch ZIP over the matching files in your existing repository, keep `.git` untouched, commit to `main`, then push. After adding the secret, run `backfill-crypto-launch-radar` once to test. The first log line should include `[rootdata]` only if a shortlist candidate matches the RootData project database and the secret is configured.

## Expected limitations

RootData Basic does not provide fundraising rounds, comprehensive people information, full update notifications, or a dedicated launch-event feed. Thus this integration enriches and resolves projects surfaced by news/DeFiLlama; it does not independently guarantee discovery of every launch or recover exact launch dates from a RootData listing/establishment date.

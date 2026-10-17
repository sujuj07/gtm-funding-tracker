> **v12 hotfix (Oct 17, 2026):** For a one-time historical recheck, `recheck_processed_sources=true` now raises the per-run candidate cap to 60 so the first week can finish in one run. Scheduled backfills remain capped at 6 candidates/hour. Founder verification now checks a small number of official team/about and blog/news pages and reads only publicly linked GitHub metadata. See `V12-INSTALL.md` before deploying.

# Startup Intelligence Tracker (v6)

Two Google Sheets data streams powered by Python, Groq, GitHub Actions, and a Google Apps Script webhook:

1. **Funding Intelligence** — global AI, deep-tech, crypto/Web3 and fintech funding rounds, explicit company formation/incorporation signals, and reported hiring/expansion signals.
2. **Crypto Launch Radar** — a selective list of substantive crypto/Web3 launches. NFT/PFP/collectibles-first and meme/memecoin projects are excluded; every tracked launch must have at least one publicly named founder with evidence of a previous shipped Web3 product/protocol or Web3 company-building track record.

The historical backfill defaults to **1 August 2026 through the current UTC date**. Live collection runs hourly; historical backfills run hourly until each stream has caught up through the current date, then each backfill workflow disables its own schedule. GitHub Actions timing is best-effort, not guaranteed real-time.

## Data coverage and limitations

- Current collection combines RSS feeds, Google News search RSS, and optional DeFiLlama Raises API data.
- DeFiLlama's public Raises dashboard is useful as a crypto funding source. Its documented `/api/raises` API endpoint requires a DeFiLlama API plan, so set `DEFILLAMA_API_KEY` only if you have access. The rest of the pipeline runs without it.
- Direct X discovery is optional. Set `X_BEARER_TOKEN` to use X API v2 recent search. The X recent-search endpoint searches the last 7 days; full-archive search is a separate paid capability. Without an X token, Crypto Launch Radar falls back to news/RSS coverage and cannot comprehensively discover projects that announce only on X.
- X account creation time is not the same as first activity or recent activation. The sheet records the profile's `created_at` date and an age bucket, but does not claim to know when an old account started posting.
- Global incorporation registries are fragmented. The tracker captures formation/incorporation signals that appear in indexed news/search feeds; it is **not** a complete registry of every company incorporated worldwide.
- Crypto Launch Radar uses a strict two-stage quality gate. It searches indexed news for the project team and named founders, then requires source URLs for public founder identity, a substantive launch, and at least one previous shipped Web3 project/role. Unnamed/pseudonymous teams, weakly evidenced histories, NFT/PFP/collectibles-first projects, meme coins/tokens, and token-only launches without substantive product utility are rejected. This intentionally prioritizes precision over recall; some legitimate but poorly indexed launches will be omitted.
- The gate relies on indexed search evidence and supplied source text; it is not a legal identity check or a guarantee of a founder's reputation. A rejected source is marked processed to avoid repeated quota use, so a candidate rejected for insufficient indexed evidence will not automatically be reconsidered unless the processed-source registry is cleared or the candidate is encountered through a new source URL.
- NFT/meme-first entries are also excluded from the global funding stream. The strict public-founder/prior-Web3-builder requirement applies to Crypto Launch Radar; AI, deep-tech and fintech funding entries do not need Web3 founder histories.
- Research accuracy depends on the material present in a feed or search result. Unknown facts should stay unknown. Source URLs and evidence confidence are saved with records.

## Crypto Launch Radar presentation

The Apps Script formats the Crypto Launch Radar to match the clean Funding Intelligence table style: charcoal/white headers, filters, frozen header and first two columns, alternating row backgrounds, readable widths, wrapped context fields, blue underlined evidence/social URL columns, and light confidence colors. The machine dedupe-key column is hidden; source and founder evidence remain visible. The final social/docs-links column extracts actual X, GitHub, Discord, Telegram, LinkedIn, YouTube, Farcaster and docs URLs linked from the project website. It does not construct guessed handles and stays blank if the site cannot be fetched or has no supported links.

After updating Apps Script, open the webhook once with `?token=YOUR_TOKEN&stream=crypto&action=format` to apply formatting to the existing tab; subsequent writes keep new rows formatted. The existing Python column positions are preserved; the social/docs link field is appended as the final column.

## 1. Configure the Google Sheet

Create/rename tabs exactly:

- `Funding Intelligence`
- `Crypto Launch Radar`

The Apps Script attempts to preserve existing headers, adds missing headers, and creates a hidden `__ProcessedSources` registry tab. That registry tracks even irrelevant items so repeated historical backfill runs can move forward without reprocessing the same source. Do not delete it during backfill.

The funding stream keeps the original first 19 columns (including Source URL in P and Dedupe Key in S) for compatibility, then adds richer enrichment columns to the right. If your current funding tab has a different name, either rename it or edit `FUNDING_SHEET_NAME` in `apps_script/Code.gs`.

## 2. Deploy the Apps Script webhook

1. Open **Extensions → Apps Script** from the target spreadsheet.
2. Replace the existing webhook code with `apps_script/Code.gs`.
3. Replace `REPLACE_WITH_A_NEW_LONG_RANDOM_SECRET` with a new private random secret. The previous token was pasted into a chat, so rotate it rather than reusing it.
4. Deploy as a **Web app**, execute as **Me**, and set access to **Anyone** (the webhook still checks the shared secret).
5. Copy the deployed Web app URL. If you edit code later, create a new deployment version or update the existing deployment so it serves the current code.

## 3. Configure GitHub Actions secrets

In the GitHub repository, go to **Settings → Secrets and variables → Actions** and add these repository secrets:

Required:

- `GROQ_API_KEY` — Groq API key.
- `WEBHOOK_URL` — deployed Apps Script Web app URL.
- `WEBHOOK_TOKEN` — the same new secret as `SECRET_TOKEN` in the Apps Script.

Optional:

- `X_BEARER_TOKEN` — X API v2 bearer token; enables direct recent-X launch search.
- `DEFILLAMA_API_KEY` — DeFiLlama API-plan key; enables its structured Raises endpoint.

Optional environment configuration:

- `GROQ_MODEL` — primary model. Funding workflows start with `openai/gpt-oss-20b`; crypto workflows start with `qwen/qwen3.8-27b` so the two streams use separate quota pools by default.
- `GROQ_MODEL_FALLBACKS` — comma-separated fallback order. Defaults to `qwen/qwen3.8-27b,openai/gpt-oss-120b`.
- The shared router tries the primary model first. If Groq returns a 429/rate-limit response, the router marks that model unavailable for the rest of the current run and tries the next configured model without waiting on the depleted quota. It also falls through to the next model if the configured model is unavailable or incompatible. Transient 5xx/timeouts get short retries first. Logs show which model actually succeeded.
- Model quotas are separate by model, as shown in the Groq console. Fallback spreads work across available quota pools; it cannot guarantee a request succeeds if all configured models have reached their limits. The cooldown memory is per Python process, so a newly scheduled GitHub Actions run will check the primary again before switching if its quota is still exhausted.

The hourly workflows are deliberately capped at four funding candidates and two crypto-launch candidates per hourly run. Funding starts with GPT-OSS 20B; crypto starts with Qwen 3.8 27B. Both automatically fall back to the other smaller model, then GPT-OSS 120B, when a model is rate-limited or unavailable. This uses separate model quota pools instead of relying on GPT-OSS 120B for every task. The Groq console’s exact limits are model/organization-specific; monitor usage in GroqCloud. Crypto candidates that pass first-stage launch triage undergo a second model review plus multiple Google News evidence searches, so accepted candidates consume more tokens/requests than rejected ones. If usage is comfortably below quota, increase `MAX_ITEMS_PER_RUN` / `CRYPTO_MAX_ITEMS_PER_RUN`; increasing them increases coverage and token use. The new strict gate applies to new rows; it does not automatically delete or retroactively vet crypto entries already present in the sheet.

## 4. Automated historical backfill and catch-up

After the webhook is deployed and GitHub secrets are set:

1. Push both workflow files to the repository's **default branch** (usually `main`). Click **Run workflow** once for each to start immediately; after that, the schedule continues automatically.
2. Funding backfill runs hourly at minute 10 UTC; crypto backfill runs hourly at minute 40 UTC. This staggering avoids starting both model-heavy runs together. Scheduled runs process up to 4 funding candidates and 2 crypto candidates per run; manual runs process up to 12 funding candidates and 6 crypto candidates.
3. Each run works on one seven-day window from 1 August 2026 onward. The cursor advances only when all candidates in that window are processed and webhook writes are confirmed. If there are more than the per-run cap, model errors, or webhook failures, that window is retried on the next run.
4. Cursor progress is stored in `.backfill-state/funding.json` and `.backfill-state/crypto.json`. The workflow commits these state files automatically. Do not delete them while the backfill is running.
5. When a stream reaches the end-exclusive date for today, its workflow commits the final cursor and disables its own schedule. The separate hourly live-collection workflows remain enabled. If you need to restart a historical run later, re-enable that workflow under Actions and run it manually.

The backfill uses `2026-08-01` as the start date and tomorrow's UTC date as the exclusive end, so the current UTC date is included. The source feeds have finite and changing coverage; completion means the configured queries and available sources have been processed, not that every company or launch worldwide has been found. GitHub scheduled runs are best-effort and may be delayed. If a workflow fails due to rate limits, API errors or a timeout, inspect the run logs; its cursor should not advance for an unresolved window.

## 5. Columns

### Funding Intelligence

Date, Company, Stage, Amount USD, Sector, HQ, One-line Summary, Customer Type, GTM Fit Score, GTM Fit Reason, Hiring Signal, Website, Careers Page, Founders, Lead Investor, Source URL, Status, Notes, Dedupe Key, Broad Offerings, Niche Presence, Product Type, Target Customers, Other Investors, Valuation USD, Event Type, Source Publisher, Evidence Confidence, Last Checked UTC, Source Published Date, Additional Evidence URLs.

GTM score columns are retained for compatibility with the prior tracker; the new pipeline prioritizes sector and evidence relevance instead of filtering out records for low GTM scores.

### Crypto Launch Radar

First Launch/Announcement Date, Project, Signal Type, Launch Date, X Account Created At, X Handle, X Profile URL, Website, Broad Offerings, Niche Presence, Chain/Ecosystem, Founders/Team, Launch Evidence URL, X Post URL, Status, Confidence, Last Checked UTC, Dedupe Key, X Account Age Category, Additional Evidence URLs, Founders Publicly Identified, Prior Web3 Builder Track Record, Previous Web3 Projects / Roles, Founder Evidence URL, Track Record Evidence URL, Project Category, Why It Passed, Social / Docs Links Found on Website.

## 6. Running locally (optional)

Install dependencies with `pip install -r requirements.txt`, set the same environment variables listed above, then run:

```bash
python main.py
python crypto_launches.py
python backfill.py
python backfill_crypto.py
```

The two backfill scripts can be run manually and use `.github/workflows/backfill.yml` and `.github/workflows/backfill-crypto.yml` for the hourly catch-up schedule. Each backfill workflow disables itself after it reaches the current date. Hourly collection remains managed by `.github/workflows/fetch.yml` and `.github/workflows/fetch-crypto.yml`.

## Historical backfill behavior (v8)

- Funding history is scheduled hourly at minute 10 UTC; crypto-launch history is scheduled hourly at minute 40 UTC. These are UTC times, not India local time.
- Each scheduled run processes a bounded batch (4 funding candidates or 2 crypto candidates). Manual dispatch processes a larger batch.
- A date window advances only when candidate processing and webhook writes have been confirmed, no model failures remain, and search coverage is sufficient. RSS/HTTP and webhook registry errors fail closed rather than being interpreted as empty data.
- If an earlier version advanced the cursor too far, manually run the relevant workflow and set `reset_cursor` to `true`. It restarts scanning at 2026-08-01. Existing processed source URLs remain stored in the hidden Apps Script registry, preventing duplicate processing.
- Once the backfill reaches today's target date, the workflow disables its own schedule; the separate hourly live-monitoring workflow continues. GitHub scheduled runs are best-effort and may be delayed.


## Crypto Launch Radar discovery quality (v9)

The crypto launch pipeline now searches more specific launch phrases and press-release queries, then applies a lightweight headline/body signal check before calling an LLM. This keeps general funding, price, transfer, executive, and market-news stories from consuming model quota unless they also contain a substantive product/network launch signal.

Logs now report the raw source count, how many new sources were found, how many lacked a clear launch cue, and how many launch-like candidates were sent to the model. The founder and previous-shipped-Web3-project verification requirements remain strict; NFTs and meme projects remain excluded.

To deploy: replace the matching repository files from the v9 patch, commit and push to `main`, then manually run `backfill-crypto-launch-radar` once. Do **not** reset the cursor for this patch. The current cursor remains at its pending week; the updated queries and prefilter will apply to that window. The backfill remains scheduled hourly, with a larger candidate cap now that irrelevant headlines are screened before model calls.


### Free crypto discovery sources (v11)
- DeFiLlama `https://api.llama.fi/protocols` is a no-key structured source. The pipeline uses `listedAt` only as a **discovery timestamp**, never as the actual launch date; independent launch evidence is still required.
- DeFiLlama's `https://api.llama.fi/api/raises` is not a free endpoint in the current official docs, so the tracker does not call it.
- For a project handle found in DeFiLlama or linked from the official website, the tracker may fetch its recent public X profile timeline via the unofficial syndication endpoint. This is best-effort, only covers known public profiles/recent posts, has no keyword search, and may stop working or rate-limit at any time. It is not used as a guaranteed search/archive source.
- Google News RSS and RSS feeds remain the broad discovery mechanism; no paid X token is required. If `X_BEARER_TOKEN` is configured, official Recent Search is used for posts from the last seven days.


## v11 free-source crypto discovery
This update supplements RSS/Google News with DeFiLlama's public no-key `/protocols` endpoint and uses public X profile syndication only when a project handle has already been discovered from an official/source record. It does not call DeFiLlama `/api/raises` (paid) or require an X API token. The syndication endpoint is unofficial, recent-profile-only, and may stop working. Neither route is a replacement for X keyword/full-archive search.

To apply v11, replace `tracker_common.py`, `backfill_crypto.py`, `crypto_launches.py`, `README.md`, and `CHANGELOG.md`, then commit/push to `main`. The Apps Script schema is unchanged, so no Apps Script redeploy is required. Re-run `backfill-crypto-launch-radar` with `reset_cursor=false`; existing processed URLs remain deduplicated. DeFiLlama `listedAt` is never written as the launch date.

### RootData Basic API integration (optional)

Set a GitHub Actions repository secret named `ROOTDATA_API_KEY` to enable RootData Basic metadata enrichment. The current official docs describe `/open/ser_inv` project/VC/person search as unlimited (still subject to 60 requests/minute), `/open/quotacredits` as free, and `/open/get_item` as 2 credits per project. This integration only uses project search and basic project details; it does not call fundraising-round, investor-detail, comprehensive people, or Pro-only endpoints.

Credit protection: each workflow checks the free RootData quota endpoint before a project-detail request; the tracker reserves at least 200 credits on the account, caps its own spend at 24 credits/day and 800 credits/month, and caches detail lookups in `.backfill-state/rootdata-state.json`. Search hits/misses are cached too. The live crypto and crypto-backfill workflows serialize access to this ledger and persist it to `main`. At most 12 uncached project-detail requests per UTC day are possible under the daily cap (2 credits each); cached lookups cost 0 additional credits. If the quota endpoint cannot confirm remaining credits, paid detail lookups are skipped, and the pipeline continues with existing sources.

RootData records are metadata only. A project listing/description never satisfies the independent launch-announcement, named-founder, or prior shipped Web3-builder evidence requirements. Funding and investor fields are intentionally ignored because they are not included in the Basic plan shown for this key. The tracker still works without `ROOTDATA_API_KEY`.

## Crypto Review Queue (v14)

Plausible launch candidates that fail strict verification are preserved in the visible `Crypto Review Queue` instead of vanishing from the workflow. The primary `Crypto Launch Radar` continues to require independent launch evidence, a publicly named founder, and a documented previous shipped Web3 build. NFT/meme-first projects are not queued for qualification.

The `recheck-crypto-review-queue` GitHub Actions workflow runs daily and rechecks at most five unresolved entries whose last check was at least seven days ago. Successful candidates are promoted to the main tracker; other entries retain an updated reason and evidence links. Set `Manual Decision` to `Reject`, `Rejected`, `Ignore`, or `Ignored` to stop automatic rechecks for a row.

See `V14-INSTALL.md` for deployment steps. Updating the repository alone does not update the deployed Apps Script; deploy a new Apps Script version while preserving the existing `SECRET_TOKEN`.

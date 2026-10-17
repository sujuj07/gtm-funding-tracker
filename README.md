# Startup Intelligence Tracker (v2)

Two Google Sheets data streams powered by Python, Groq, GitHub Actions, and a Google Apps Script webhook:

1. **Funding Intelligence** — global AI, deep-tech, crypto/Web3 and fintech funding rounds, explicit company formation/incorporation signals, and reported hiring/expansion signals.
2. **Crypto Launch Radar** — a selective list of substantive crypto/Web3 launches. NFT/PFP/collectibles-first and meme/memecoin projects are excluded; every tracked launch must have at least one publicly named founder with evidence of a previous shipped Web3 product/protocol or Web3 company-building track record.

The historical backfill defaults to **1 August 2026 through the current UTC date**. Scheduled collection runs hourly; GitHub Actions timing is best-effort, not guaranteed real-time.

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

The Apps Script formats the Crypto Launch Radar to match the clean Funding Intelligence table style: navy/white headers, filters, frozen header and first two columns, alternating row backgrounds, readable widths, wrapped context fields, clickable-looking evidence/social URL columns, and light confidence colors. The machine dedupe-key column is hidden; source and founder evidence remain visible. The final social/docs-links column extracts actual X, GitHub, Discord, Telegram, LinkedIn, YouTube, Farcaster and docs URLs linked from the project website. It does not construct guessed handles and stays blank if the site cannot be fetched or has no supported links.

After updating Apps Script, open the webhook once with `?token=YOUR_TOKEN&stream=crypto` to style the existing tab; subsequent writes keep new rows formatted. The existing Python column positions are preserved; the social/docs link field is appended as the final column.

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

Optional repository variable:

- `GROQ_MODEL` — defaults to `openai/gpt-oss-120b` for scheduled research. Historical backfill workflows use `openai/gpt-oss-20b` to conserve the larger model's token budget.

The hourly workflows are deliberately capped at four funding candidates and two crypto-launch candidates per hourly run to help stay within the 200k tokens/day and 1k requests/day model limits shown in the supplied Groq console screenshot. Crypto candidates that pass first-stage launch triage undergo a second model review plus multiple Google News evidence searches, so accepted candidates consume more tokens/requests than rejected ones. If usage is comfortably below quota, increase `MAX_ITEMS_PER_RUN` / `CRYPTO_MAX_ITEMS_PER_RUN`; increasing them increases coverage and token use. The new strict gate applies to new rows; it does not automatically delete or retroactively vet crypto entries already present in the sheet.

## 4. Run the initial historical backfill

After the webhook is deployed and GitHub secrets are set:

1. Open the repository's **Actions** tab.
2. Run **backfill-funding-intelligence** manually.
3. Run **backfill-crypto-launch-radar** manually.
4. Re-run each workflow while it continues to discover unprocessed indexed results. Each run processes up to 60 candidates by default. They may take several runs to exhaust the search results within the free model quota.
5. The scheduled hourly workflows continue collecting new signals independently.

Date range is controlled by `BACKFILL_DATE_FROM` and `BACKFILL_DATE_TO`. Defaults are `2026-08-01` and tomorrow's date (exclusive), meaning today is included. For a fresh replay, clear the hidden processed-source registry only after backing it up; otherwise the webhook correctly skips previously processed sources.

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

The two backfill scripts are historical search jobs and should be run manually. Scheduled collection is managed by `.github/workflows/fetch.yml` and `.github/workflows/fetch-crypto.yml`.

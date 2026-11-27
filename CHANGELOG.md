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

## v15 — Stop the blank-row corruption; qualify targets on first-party hiring data

### The crash, and why it was getting worse
`doPost` wrote new rows as two operations: `insertRowsBefore(2, n)` then
`setValues(...)`. When the script died between them the blank rows were already
committed and stayed in the sheet. 187 such rows had accumulated.

It was self-worsening. `getNaturalEventRowMap()` read the **whole sheet at full
width** on every POST (~1,000 rows x 31 columns = ~31,000 cells), `mergeEventRow()`
did a separate read *and* write per updated row, and every blank row raised
`lastRow` — so each run read more data, was likelier to hit the execution limit,
and left more blank rows behind.

- `getNaturalEventRowMap()` now reads only the three columns the key needs, roughly
  a tenth of the data.
- The insert is rolled back if the fill fails, so a failed run leaves the sheet
  unchanged instead of permanently scarred.
- `applyIntelUpdates()` does two reads and one write regardless of how many rows
  it touches.
- New **Tracker Tools > Remove Blank Rows (Funding)** menu item clears the rows
  already there (deletes in contiguous descending runs so row numbers stay valid).

### Values are validated before they reach the sheet (`validate.py`)
Groq's `json_object` mode guarantees parseable JSON, never valid *values*. These
all reached the sheet:

| Observed | Now |
|---|---|
| `stage = "Series A\|Series B\|Series C\|..."` (enum echoed) | rejected, re-inferred from source text |
| `amount_usd = "$10M"` (string, not number) | parsed to 10000000 |
| `"R$30M"` read as US$30M | converted from BRL (~$5.4M) |
| `"Rs 43.7 Cr operating revenue"` as a raise | rejected — revenue is not a raise |
| `hiring_signal = "Staff reduction of 20%"` in an enum column | flag `No`, prose moved to Notes |
| `website = "N/A"` | discarded |
| `"Series A"` with `$4B` | kept but flagged as a stage/amount mismatch |

Unparseable amounts stay **empty**. A number recovered from a headline is a guess,
and a guess does not belong in a column labelled USD. Problems are written to Notes,
so extraction failures are visible and measurable instead of silent.

### Careers Page column was fabricated
It was `f"{website}/careers"` — string concatenation, never fetched. That is why the
column was ~100% populated and ~0% real. `main.py` now leaves it blank; the
enrichment pass fills `Careers URL (verified)` with a board URL that has actually
been retrieved.

### Deduplication
`event_key` hashed `company|date|event_type|stage|amount`, so one raise reported over
several days by several outlets became several rows — 54 companies occupied 142 rows
(Griffin's $20M appeared twice, one day apart). `funding_event_key()` keys on
`company + event_type + month`. Trade-off: two genuine raises in one calendar month
merge; the Apps Script merge keeps the richer value per field and appends the extra
source URLs, so nothing is lost when it happens.

### Source Publisher held the search query
`fetch_google_news` passed `label="Google News: <query>"` and that went straight to the
sheet. Now the real outlet is read from the entry's `<source>` element (or the
"- Publisher" title suffix), and the query is kept separately as `discovery_query`.

### Qualification on first-party data (`ats_enrich.py`, `scoring.py`, `enrich_targets.py`)
Greenhouse, Lever, Ashby, Workable and Recruitee each publish an official keyless
public JSON board — the same endpoint the employer's own careers page calls. That makes
three things fact rather than inference: how many roles are open, whether a
growth/BD/RevOps role is open, and when it was last confirmed.

Scoring weights verified board data above news prose, because an open growth role is
the company stating the need rather than us guessing it:

    open growth/BD role   +6      small board (1-30)    +3
    prime stage (seed-A)  +5      raise $1-40M          +4
    large raise           -4      stage/amount mismatch -3

`None` (not checked) and `0` (checked, none open) are kept distinct — conflating them
would quietly reward companies nobody has looked at.

New columns are appended from **AF onward**, deliberately beyond the pipeline's
`maxCol` of 31, so the two writers cannot collide and A:AE never has to be reordered.
Human outreach columns (POC, status, notes) are never overwritten by automation.

New workflow `enrich-targets.yml` runs twice daily, shares the funding concurrency
group so it can never write while `fetch.yml` is writing, and is capped per run with
the queue served highest-score-first.

### Tests
`tests/test_validate.py` and `tests/test_scoring.py` — 27 cases, every one built from a
value or ranking failure actually observed in the sheet. Both run in CI before any
write.

## v16 — Remove the ATS enrichment; score from the row itself

The ATS board enrichment added in v15 is removed. It was unproven against real
sites, and it introduced a second workflow, a webhook action, 16 appended sheet
columns and ~820 lines of code to answer one question — how big is this team.

Deleted: `ats_enrich.py`, `enrich_targets.py`, `.github/workflows/enrich-targets.yml`,
the `enrichment_queue` GET action, `applyIntelUpdates`, the INTEL column block, and
`webhook_get_enrichment_queue` / `post_rows(intel_updates=...)`. Sheet columns AF:AU
removed. `Code.gs` is down from 811 to 658 lines.

Everything in v15 that fixed an actual bug is untouched: the blank-row rollback,
the narrowed sheet read, `cleanupBlankFundingRows`, `validate.py`, the month-bucket
dedupe key, and the publisher fix.

### Scoring moved into the write path
Scoring now runs in `scoring.py` at row-write time and fills **columns I and J**
(`GTM Fit Score` / `GTM Fit Reason`), which already existed and had been empty on 536
rows. No second pass, no queue, no new columns.

Signals: stated team size, stated founding year, stage, raise band, hiring mention,
B2B, freshness. Each one that fires is named in column J, e.g.
`[A] SMALL-TEAM(9) · NEW-CO(2025) · PRIME-STAGE · $1-40M · HIRING · B2B · FRESH`.

### Team size and founding year
`team_size` and `founded_year` were added to the extraction schema, with the prompt
instructing the model to return them **only when the source states them outright** and
never to estimate from the raise or the stage. Both are range-checked.

Expect them to be empty on most rows: funding articles rarely print headcount. When
headcount is unknown the score falls back to stage as a proxy and column J says
`team-size-unknown`, so an inferred "small" is distinguishable from a stated one.
This is a partial answer to "under 10 team members" — an honest one, not a guessed one.

### backfill.py had every bug main.py had
`backfill-funding-intelligence` is an active workflow writing to the same sheet, and it
was still building rows the old way: unvalidated stage and amount, prose in the hiring
column, the fabricated `website + "/careers"` value, and empty score columns. It now
runs the same `validate_extraction` + `score_clean_row` path as `main.py`, and its
dedupe key matches too (it was still using `event_key` with stage and amount, so
backfill rows carried a different key format than live rows).

## v17 — Throughput, backlog fairness, and SEC Form D

### The real constraint was never the number of sources
Per run the pipeline gathered ~244 candidates (13 RSS feeds x 8, 10 Google News
queries x 10, up to 40 from DeFiLlama) and processed **4**. The cron asks for hourly
but GitHub skips scheduled runs under load, so the real cadence was 3-4 runs a day:
roughly 12-16 items processed daily against a pool that refreshes to 244 every run.

Adding sources would have made that worse, not better. `MAX_ITEMS_PER_RUN` is now 25,
which is safe in a way it was not before: validation stops malformed values at the
source, which is part of what that cap was implicitly protecting against.

### Backlog starvation
`fresh` was sorted newest-first and truncated. An item that missed the cut was not
delayed — it was permanently outranked by every newer arrival and would never be
processed. `select_for_processing()` now reserves a quarter of each run's slots for
the oldest unprocessed candidates, so the tail drains.

### SEC Form D (`sec_form_d.py`)
The first source here that needs no model call. Form D is a filing, not a news story:
companies raising under Regulation D (most US seed and Series A rounds) must file one,
under penalty of perjury, and the SEC publishes it as structured XML.

    company name   <- entityName          raise amount  <- totalAmountSold
    HQ             <- issuerAddress       founding year <- yearOfInc
    officer names  <- relatedPersonsList  industry      <- industryGroup

No tokens, no extraction failures, no validation rejections. It also fills the two
fields news sources almost never carry: founding year (previously empty on nearly
every row) and founder names (previously empty on ~88%).

Deliberate limits, all visible in the output rather than papered over:

- **US only.** Form D adds nothing for India or Europe.
- **Stage stays `Unknown`.** A filing does not name a round stage, and inferring
  "Seed" from the amount would be a guess written into a validated column.
- **Amount is what has been *sold* at filing time**, so it can be blank on a real
  round. Zero is reported as unknown, never as $0.
- **Pooled investment funds are filtered out.** A VC raising its own vehicle files a
  Form D too.

Because Form D rows lack stage (+5) and B2B (+2), they score lower than news rows
despite being more reliable. Rather than fake those fields, scoring now credits what
Form D uniquely provides:

### New signal: `NAMED-CONTACTS(n)`
+2 when the row carries named people. A row with no contact needs a separate lookup
before it can be used for outreach at all, so this is a real difference in
usefulness, not a proxy for one.

### Shared row builder
`build_row()` is now used by both the news path and the Form D path, so the two
writers cannot drift apart in column order or content. A bug found during this
change: `seen_keys` was being re-initialised after the Form D pass, which would have
let a news article and a filing for the same company+month both land as separate rows
within one run.

### Sheet documentation
New **README & Glossary** tab: what the sheet is, how a row arrives, every column
A-AE explained, score and tier bands, all 19 signal tags, source inventory with
trust notes, throughput facts, and a "what to trust / what is usually empty / what to
distrust" section.

### Tests
41 cases across three files (`test_validate.py` 14, `test_scoring.py` 14,
`test_sec_form_d.py` 13), all running in CI before any write. Form D parsing is
tested against fixtures mirroring real EDGAR shapes — an operating company, a VC fund
that must be filtered out, and a filing with nothing sold yet — but **not against
live EDGAR**, since this environment has no outbound network.

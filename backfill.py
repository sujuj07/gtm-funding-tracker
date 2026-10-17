"""Historical startup funding / formation / hiring backfill from 2026-08-01 onward.

Run via GitHub Actions -> Backfill funding (manual). Re-run until it reports no new
source candidates. Processed sources (including rejected results) are saved in the
Apps Script so free-tier batches make forward progress rather than repeat work.
"""
import os
import time
from datetime import datetime, timedelta, timezone

from tracker_common import (
    MODEL, defillama_raises, event_key, extract_json, fetch_google_news,
    groq_client, post_rows, webhook_get, norm_company, parse_event_date, published_timestamp,
    excluded_nft_or_meme,
)
from backfill_state import advance_window, choose_window, end_exclusive_today, leave_window_pending

STREAM = "funding"
DATE_FROM = os.getenv("BACKFILL_DATE_FROM", "2026-08-01")
DATE_TO = os.getenv("BACKFILL_DATE_TO", end_exclusive_today())  # exclusive
WINDOW_DAYS = int(os.getenv("BACKFILL_WINDOW_DAYS", "7"))
MAX_ITEMS_PER_QUERY = int(os.getenv("BACKFILL_ITEMS_PER_QUERY", "60"))
MAX_ITEMS_PER_RUN = int(os.getenv("BACKFILL_MAX_ITEMS_PER_RUN", "60"))
SAVE_BATCH_SIZE = int(os.getenv("SAVE_BATCH_SIZE", "10"))
DELAY = float(os.getenv("MODEL_DELAY_SECONDS", "2.5"))

QUERIES = [
    'startup raises funding seed OR Series AI artificial intelligence',
    'AI startup funding round announced investors',
    'deep tech startup raises funding robotics semiconductor quantum',
    'deep technology company receives investment funding round',
    'fintech startup raises seed Series funding payments lending',
    'fintech company funding round investors global',
    'crypto startup raises funding round blockchain web3',
    'DeFi protocol raises funding round investors',
    'new company incorporated AI startup founded launched',
    'deep tech company founded incorporated startup launch',
    'fintech company incorporated startup founded launch',
    'crypto web3 company launch new startup protocol announced',
    'AI startup hiring expansion jobs funded company',
    'robotics startup funding round deep tech investment',
    'climate tech energy storage battery startup raises funding',
    'biotech tools scientific instruments startup raises funding',
]

PROMPT = r"""You are a careful startup intelligence analyst. Extract ONLY details supported by the supplied source. Do not infer exact funding amounts, dates, investors, founders, or product details without evidence. Return JSON only:
{
 "relevant": true,
 "event_type": "Funding round|Company formation/incorporation|Hiring announcement|Other",
 "event_date": "YYYY-MM-DD or null",
 "company": "official company/project name",
 "stage": "Pre-seed|Seed|Seed Extension|Series A|Series B|Series C|Series D+|Growth|Strategic|Grant|Debt|Unknown",
 "amount_usd": number or null,
 "valuation_usd": number or null,
 "sector": "specific sector and sub-sector",
 "crypto_project_category": "protocol/infrastructure|DeFi|developer tools|payments/stablecoin|NFT/collectibles|meme/memecoin|other|not applicable",
 "is_nft_or_meme_project": false,
 "broad_offerings": "products/services, 1-3 sentences",
 "niche_presence": "specific technical/business niche, use case, specialization",
 "product_type": "software|hardware|infrastructure|financial service|protocol|marketplace|other",
 "target_customers": "customer types",
 "one_liner": "concise summary",
 "is_b2b": true,
 "hq_country": "country or null",
 "hq_city": "city or null",
 "website": "official URL or null",
 "founders": ["names only if stated"],
 "lead_investor": "name or null",
 "other_investors": ["names only if stated"],
 "hiring_signal": "explicit hiring information or Unknown",
 "confidence": "High|Medium|Low",
 "evidence_note": "what this source establishes"
}
Include credible funding rounds, explicit company formation/incorporation, or substantive hiring announcements for AI, deep tech (robotics, semiconductors, quantum, biotech tooling, space, energy/climate hardware, scientific instruments, advanced materials), crypto/blockchain/Web3, or fintech globally. Include pre-seed and undisclosed rounds. For crypto/Web3 entries, exclude projects primarily built around NFTs, PFPs, collectibles, NFT marketplaces, meme coins or meme tokens. Exclude investor commentary, public market news, token prices, fund launches, and unrelated companies. Do not classify a company as NFT/meme-first just because it mentions those as a secondary use case. Return relevant=false for irrelevant stories. Never invent details; use null, empty arrays or Unknown where needed.

TITLE: {title}
DATE: {source_date}
PUBLISHER: {publisher}
URL: {link}
BODY: {body}
"""


def date_windows(start: str, end_exclusive: str, window_days: int):
    cur = datetime.strptime(start, "%Y-%m-%d").date()
    end = datetime.strptime(end_exclusive, "%Y-%m-%d").date()
    while cur < end:
        next_day = min(cur + timedelta(days=window_days), end)
        yield cur.isoformat(), next_day.isoformat()
        cur = next_day


def collect_items(window_start: str, window_end: str):
    gathered = {}
    for query in QUERIES:
        for item in fetch_google_news(query, after=window_start, before=window_end, limit=MAX_ITEMS_PER_QUERY):
            if item.get("link"):
                gathered.setdefault(item["link"], item)
        time.sleep(0.25)
    print(f"  fetched historical search window {window_start} to {window_end}")
    # DeFiLlama Raises is optional; only use rows that fall inside the active window.
    for item in defillama_raises():
        dt = parse_event_date(item)
        if dt != "Unknown" and window_start <= dt < window_end and item.get("link"):
            gathered.setdefault(item["link"], item)
    return sorted(gathered.values(), key=published_timestamp, reverse=True)


def main():
    window = choose_window(STREAM, DATE_FROM, DATE_TO, WINDOW_DAYS)
    if window is None:
        return
    window_start, window_end = window
    print(f"Historical funding backfill | {window_start} <= dates < {window_end} | current target end={DATE_TO} | model={MODEL}")
    items = collect_items(window_start, window_end)
    existing_sources = webhook_get(STREAM, "existing_sources")
    existing_keys = webhook_get(STREAM, "existing_keys")
    fresh_all = [item for item in items if item.get("link") and item["link"] not in existing_sources]
    more_candidates = len(fresh_all) > MAX_ITEMS_PER_RUN
    fresh = fresh_all[:MAX_ITEMS_PER_RUN]
    print(f"Collected {len(items)} distinct source URLs; {len(fresh_all)} unprocessed candidates, processing {len(fresh)} (cap={MAX_ITEMS_PER_RUN})")

    client = groq_client()
    pending, processed = [], []
    seen_keys = set(existing_keys)
    appended = 0
    updated = 0
    flush_unconfirmed = False
    failed_items = []

    def flush():
        nonlocal pending, processed, appended, updated, flush_unconfirmed
        if not pending and not processed:
            return
        result = post_rows(STREAM, pending, processed_sources=processed)
        if result and result.get("ok"):
            appended += result.get("appended", 0)
            updated += result.get("updated", 0)
            print(f"  [save] appended={result.get('appended', 0)} updated={result.get('updated', 0)} skipped={result.get('skipped', 0)}")
            pending, processed = [], []
            flush_unconfirmed = False
        else:
            print("  [warn] webhook did not confirm batch; leaving date window pending for retry")
            flush_unconfirmed = True

    for idx, item in enumerate(fresh, 1):
        date = parse_event_date(item)
        prompt = PROMPT.replace("{title}", item.get("title", "")[:400])
        prompt = prompt.replace("{source_date}", date).replace("{publisher}", item.get("publisher", "Unknown"))
        prompt = prompt.replace("{link}", item.get("link", "")).replace("{body}", item.get("body", "")[:1800])
        print(f"[{idx}/{len(fresh)}] {item.get('title', '')[:90]}")
        data = extract_json(client, prompt)
        if data is not None:
            processed.append(item["link"])
        else:
            failed_items.append(item["link"])
            print("  [warn] model extraction failed; this source will be retried")
        if data and data.get("relevant") and data.get("company"):
            if excluded_nft_or_meme(data):
                print(f"  - excluded NFT/meme-first project: {data.get('company')}")
            else:
                event_type = data.get("event_type") or "Funding round"
                event_date = data.get("event_date") or date
                key = event_key(data.get("company"), event_date, event_type, data.get("stage", ""), data.get("amount_usd", ""))
                seen_keys.add(key)
                hq = ", ".join(filter(None, [data.get("hq_city"), data.get("hq_country")]))
                website = str(data.get("website") or "").rstrip("/")
                pending.append([
                    event_date, data.get("company", ""), data.get("stage", "Unknown"), data.get("amount_usd") or "",
                    data.get("sector", ""), hq, data.get("one_liner", ""),
                    "B2B" if data.get("is_b2b") else "B2C/Consumer/Unknown", "", "", data.get("hiring_signal", "Unknown"),
                    website, f"{website}/careers" if website else "", ", ".join(data.get("founders", []) or []),
                    data.get("lead_investor") or "", item.get("link", ""), "New", data.get("evidence_note", ""), key,
                    data.get("broad_offerings", ""), data.get("niche_presence", ""), data.get("product_type", ""),
                    data.get("target_customers", ""), ", ".join(data.get("other_investors", []) or []), data.get("valuation_usd") or "",
                    event_type, item.get("publisher", ""), data.get("confidence", "Low"),
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), item.get("published", ""),
                ])
                print(f"  + {data.get('company')} | {event_type} | {data.get('sector')}")
        if len(pending) >= SAVE_BATCH_SIZE or len(processed) >= SAVE_BATCH_SIZE:
            flush()
        time.sleep(DELAY)
    flush()
    print(f"Backfill window finished. Rows appended: {appended}; events enriched: {updated}.")

    if failed_items:
        leave_window_pending(window_start, window_end, f"{len(failed_items)} source(s) had model errors")
    elif more_candidates:
        leave_window_pending(window_start, window_end, f"{len(fresh_all) - len(fresh)} fresh candidate(s) remain for this week")
    elif flush_unconfirmed:
        leave_window_pending(window_start, window_end, "webhook write was not confirmed")
    else:
        caught_up = advance_window(STREAM, window_start, window_end, DATE_TO)
        if caught_up:
            print("[backfill] HISTORICAL BACKFILL COMPLETE through today's target date; workflow can disable its 10-minute schedule.")


if __name__ == "__main__":
    main()

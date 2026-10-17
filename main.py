"""Near-real-time global funding / company-intelligence tracker."""
import os
import time
from datetime import datetime, timezone

from tracker_common import (
    MODEL_CHAIN_LABEL, defillama_raises, event_key, extract_json, fetch_google_news,
    fetch_rss_feed, groq_client, norm_company, parse_event_date, post_rows, published_timestamp,
    webhook_get, excluded_nft_or_meme,
)

STREAM = "funding"
MIN_DELAY = float(os.getenv("MODEL_DELAY_SECONDS", "2.5"))
MAX_ITEMS_PER_RUN = int(os.getenv("MAX_ITEMS_PER_RUN", "4"))
SAVE_BATCH_SIZE = int(os.getenv("SAVE_BATCH_SIZE", "12"))

RSS_SOURCES = [
    ("https://techcrunch.com/category/venture/feed/", "TechCrunch Venture"),
    ("https://news.crunchbase.com/feed/", "Crunchbase News"),
    ("https://inc42.com/buzz/feed/", "Inc42"),
    ("https://entrackr.com/feed/", "Entrackr"),
    ("https://sifted.eu/feed", "Sifted"),
    ("https://decrypt.co/feed", "Decrypt"),
    ("https://cointelegraph.com/rss", "Cointelegraph"),
    ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"),
    ("https://www.eu-startups.com/feed/", "EU-Startups"),
    ("https://venturebeat.com/feed/", "VentureBeat"),
    ("https://www.therobotreport.com/feed/", "The Robot Report"),
    ("https://www.fintechfutures.com/feed/", "FinTech Futures"),
    ("https://tech.eu/feed/", "Tech.eu"),
]

NEWS_QUERIES = [
    'startup funding round AI artificial intelligence raises seed OR Series',
    'deep tech startup raises funding round robotics semiconductor quantum materials',
    'fintech startup raises funding round payments lending banking infrastructure',
    'crypto web3 blockchain startup raises funding round seed Series',
    'AI startup funding announced venture capital October 2026',
    'deep tech company funding announced venture capital',
    'fintech company funding announced venture capital',
    'crypto protocol startup funding announced investors',
    'startup newly incorporated AI deep tech fintech crypto company launch',
    'startup hiring expansion AI deep tech fintech crypto funding',
]

PROMPT = r"""You are a careful startup intelligence analyst. Extract ONLY details supported by the supplied source. Do not infer exact funding amounts, founding dates, investors, founders, or product details without evidence. Return JSON only using this schema:
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
 "broad_offerings": "products/services in plain English, 1-3 sentences",
 "niche_presence": "specific technical/business niche, use cases, differentiated specialty",
 "product_type": "software|hardware|infrastructure|financial service|protocol|marketplace|other",
 "target_customers": "customer types/ICP",
 "one_liner": "concise company summary",
 "is_b2b": true,
 "hq_country": "country or null",
 "hq_city": "city or null",
 "website": "official URL or null",
 "founders": ["names if explicitly stated"],
 "lead_investor": "name or null",
 "other_investors": ["names if explicitly stated"],
 "hiring_signal": "explicit hiring information or Unknown",
 "confidence": "High|Medium|Low",
 "evidence_note": "briefly state what the source actually establishes"
}
Relevant means there is a credible reported funding round, explicit company formation/incorporation, or a substantive hiring announcement, AND the company belongs to at least one of these: AI, deep tech (robotics, advanced manufacturing, semiconductors, quantum, biotech tooling, space, energy/climate hardware, scientific instruments, advanced materials), crypto/blockchain/Web3, or fintech. It may be anywhere in the world. Include pre-seed and undisclosed rounds. For crypto/Web3 entries, exclude projects primarily built around NFTs, PFPs, collectibles, NFT marketplaces, meme coins or meme tokens. Exclude fund launches, investor opinions, public-market commentary, token price news, generic product updates without evidence of a new company launch, and unrelated companies. Do not classify a company as NFT/meme-first just because it mentions those as a secondary use case. Keep unsupported fields null/empty/Unknown; never invent a website.

SOURCE TITLE: {title}
SOURCE DATE: {source_date}
SOURCE PUBLISHER: {publisher}
SOURCE URL: {link}
SOURCE BODY: {body}
"""


def make_items():
    items = []
    for url, label in RSS_SOURCES:
        items.extend(fetch_rss_feed(url, limit=8, label=label))
    for query in NEWS_QUERIES:
        items.extend(fetch_google_news(query, limit=10))
        time.sleep(0.3)
    # Optional structured DeFiLlama crypto raises data (Pro API key required)
    items.extend(defillama_raises()[:40])
    uniq = {}
    for item in items:
        link = item.get("link")
        if link and link not in uniq:
            uniq[link] = item
    return list(uniq.values())


def main():
    print(f"Funding intelligence run | model-route={MODEL_CHAIN_LABEL} | UTC={datetime.now(timezone.utc).isoformat()}")
    existing_sources = webhook_get(STREAM, "existing_sources")
    existing_keys = webhook_get(STREAM, "existing_keys")
    items = make_items()
    fresh = [x for x in items if x.get("link") and x["link"] not in existing_sources]
    fresh.sort(key=published_timestamp, reverse=True)
    fresh = fresh[:MAX_ITEMS_PER_RUN]
    print(f"Fetched {len(items)} unique feed results; {len(fresh)} new sources to classify")

    client = groq_client()
    pending, processed = [], []
    seen_keys = set(existing_keys)
    total_saved = 0
    total_updated = 0

    def flush():
        nonlocal pending, processed, total_saved, total_updated
        if not pending and not processed:
            return
        result = post_rows(STREAM, pending, processed_sources=processed)
        if result and result.get("ok"):
            total_saved += result.get("appended", 0)
            total_updated += result.get("updated", 0)
            print(f"  [save] appended={result.get('appended', 0)} updated={result.get('updated', 0)} skipped={result.get('skipped', 0)}")
            pending, processed = [], []
        else:
            print("  [warn] batch not confirmed; keeping data pending for final retry")

    for idx, item in enumerate(fresh, 1):
        event_date = parse_event_date(item)
        title = item.get("title", "")
        prompt = PROMPT.replace("{title}", title[:400])
        prompt = prompt.replace("{source_date}", event_date)
        prompt = prompt.replace("{publisher}", item.get("publisher", "Unknown"))
        prompt = prompt.replace("{link}", item.get("link", ""))
        prompt = prompt.replace("{body}", item.get("body", "")[:1300])
        print(f"[{idx}/{len(fresh)}] {title[:90]}")
        data = extract_json(client, prompt)
        if data is not None:
            processed.append(item["link"])
        if data and data.get("relevant") and data.get("company"):
            if excluded_nft_or_meme(data):
                print(f"  - excluded NFT/meme-first project: {data.get('company')}")
                if len(processed) >= SAVE_BATCH_SIZE:
                    flush()
                time.sleep(MIN_DELAY)
                continue
            ev_type = data.get("event_type") or "Funding round"
            date = data.get("event_date") or event_date
            key = event_key(data.get("company"), date, ev_type, data.get("stage", ""), data.get("amount_usd", ""))
            seen_keys.add(key)
            hq = ", ".join(filter(None, [data.get("hq_city"), data.get("hq_country")]))
            website = str(data.get("website") or "").rstrip("/")
            row = [
                    date, data.get("company", ""), data.get("stage", "Unknown"), data.get("amount_usd") or "",
                    data.get("sector", ""), hq, data.get("one_liner", ""),
                    "B2B" if data.get("is_b2b") else "B2C/Consumer/Unknown", "", "",
                    data.get("hiring_signal", "Unknown"), website, f"{website}/careers" if website else "",
                    ", ".join(data.get("founders", []) or []), data.get("lead_investor") or "", item.get("link", ""),
                    "New", data.get("evidence_note", ""), key,
                    data.get("broad_offerings", ""), data.get("niche_presence", ""), data.get("product_type", ""),
                    data.get("target_customers", ""), ", ".join(data.get("other_investors", []) or []),
                    data.get("valuation_usd") or "", ev_type, item.get("publisher", ""),
                    data.get("confidence", "Low"), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    item.get("published", ""),
            ]
            pending.append(row)
            print(f"  + {data.get('company')} | {ev_type} | {data.get('sector')} [{data.get('confidence', 'Low')}]" )
        if len(pending) >= SAVE_BATCH_SIZE or len(processed) >= SAVE_BATCH_SIZE:
            flush()
        time.sleep(MIN_DELAY)
    flush()
    print(f"Completed. Rows appended: {total_saved}; existing events enriched: {total_updated}")


if __name__ == "__main__":
    main()

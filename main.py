"""Near-real-time global funding / company-intelligence tracker."""
import os
import time
from datetime import datetime, timezone

from tracker_common import (
    check_deployed_script_version,
    MODEL_CHAIN_LABEL, defillama_raises, extract_json, fetch_google_news,
    fetch_rss_feed, funding_event_key, groq_client, norm_company, parse_event_date, post_rows,
    published_timestamp, webhook_get, excluded_nft_or_meme,
)
from scoring import score_clean_row
from validate import is_usable, validate_extraction

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
 "team_size": number or null,
 "founded_year": number or null,
 "hiring_signal": "explicit hiring information or Unknown",
 "confidence": "High|Medium|Low",
 "evidence_note": "briefly state what the source actually establishes"
}
Relevant means there is a credible reported funding round, explicit company formation/incorporation, or a substantive hiring announcement, AND the company belongs to at least one of these: AI, deep tech (robotics, advanced manufacturing, semiconductors, quantum, biotech tooling, space, energy/climate hardware, scientific instruments, advanced materials), crypto/blockchain/Web3, or fintech. It may be anywhere in the world. Include pre-seed and undisclosed rounds. For crypto/Web3 entries, exclude projects primarily built around NFTs, PFPs, collectibles, NFT marketplaces, meme coins or meme tokens. Exclude fund launches, investor opinions, public-market commentary, token price news, generic product updates without evidence of a new company launch, and unrelated companies. Do not classify a company as NFT/meme-first just because it mentions those as a secondary use case. Keep unsupported fields null/empty/Unknown; never invent a website. For team_size give the company's total employee/headcount ONLY if the source states it explicitly (e.g. "the 12-person team", "grew to 40 staff"); never estimate it from the funding amount, the stage or the company's apparent size. For founded_year give the year the company was founded ONLY if the source states it. Both must be null when the source does not say.

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
    # Checked up front so a stale receiver is visible at the top of the log,
    # before any rows are written, rather than inferred later from damage.
    check_deployed_script_version()
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
            # Coerce and validate before anything is allowed near the sheet. The model
            # returns valid JSON but not valid values; see validate.py for the specific
            # failures this catches (enum echoes, "$10M" strings, prose in enum fields).
            clean, problems = validate_extraction(data)
            if not is_usable(clean):
                print("  - rejected: extraction produced no usable company name")
                if len(processed) >= SAVE_BATCH_SIZE:
                    flush()
                time.sleep(MIN_DELAY)
                continue
            if problems:
                for problem in problems:
                    print(f"  ! {problem}")

            ev_type = clean["event_type"]
            date = clean["event_date"] or event_date
            key = funding_event_key(clean["company"], date, ev_type)
            seen_keys.add(key)
            hq = ", ".join(filter(None, [clean["hq_city"], clean["hq_country"]]))

            # Notes carries the evidence note, any hiring prose that was moved out of
            # the enum column, and the validation problems -- so a bad extraction is
            # visible in the sheet instead of silent, and the model's failure rate is
            # measurable over time.
            notes = " | ".join(filter(None, [
                clean["evidence_note"],
                f"hiring note: {clean['hiring_text']}" if clean["hiring_text"] else "",
                f"data quality: {'; '.join(problems)}" if problems else "",
            ]))

            # Scored here rather than in a separate pass: every input is already in
            # `clean`, so each row is scored as it is written and columns I/J (which
            # had been empty on 536 rows) are populated from the start.
            gtm_score, gtm_why = score_clean_row(clean, fallback_date=event_date)

            row = [
                    date, clean["company"], clean["stage"], clean["amount_usd"] or "",
                    clean["sector"], hq, clean["one_liner"],
                    "B2B" if clean["is_b2b"] else "B2C/Consumer/Unknown",
                    gtm_score, gtm_why,
                    clean["hiring_flag"], clean["website"],
                    # Careers URL is left for the ATS enrichment pass to fill with a
                    # board URL that has actually been fetched. It used to be
                    # f"{website}/careers" -- a string built by concatenation and never
                    # verified, which is why this column was 100% populated and 0% real.
                    "",
                    ", ".join(clean["founders"]), clean["lead_investor"], item.get("link", ""),
                    "New", notes, key,
                    clean["broad_offerings"], clean["niche_presence"], clean["product_type"],
                    clean["target_customers"], ", ".join(clean["other_investors"]),
                    clean["valuation_usd"] or "", ev_type, item.get("publisher", ""),
                    clean["confidence"], datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    item.get("published", ""),
            ]
            pending.append(row)
            flag = f" [{len(problems)} data issue(s)]" if problems else ""
            print(f"  + {clean['company']} | {ev_type} | {clean['stage']} | "
                  f"score={gtm_score} {gtm_why[:60]} [{clean['confidence']}]{flag}")
        if len(pending) >= SAVE_BATCH_SIZE or len(processed) >= SAVE_BATCH_SIZE:
            flush()
        time.sleep(MIN_DELAY)
    flush()
    print(f"Completed. Rows appended: {total_saved}; existing events enriched: {total_updated}")


if __name__ == "__main__":
    main()

"""Historical crypto launch backfill with strict public-founder and prior-builder verification."""
import os
import time
from datetime import datetime, timedelta, timezone

from tracker_common import (
    MODEL, event_key, extract_json, fetch_google_news, fetch_official_social_links, fetch_rss_feed, groq_client,
    parse_event_date, post_rows, published_timestamp, verify_crypto_launch_quality, webhook_get,
)

STREAM = "crypto"
DATE_FROM = os.getenv("BACKFILL_DATE_FROM", "2026-08-01")
DATE_TO = os.getenv("BACKFILL_DATE_TO", (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat())
WINDOW_DAYS = int(os.getenv("BACKFILL_WINDOW_DAYS", "7"))
ITEMS_PER_QUERY = int(os.getenv("BACKFILL_ITEMS_PER_QUERY", "60"))
MAX_ITEMS_PER_RUN = int(os.getenv("BACKFILL_MAX_ITEMS_PER_RUN", "60"))
SAVE_BATCH_SIZE = int(os.getenv("SAVE_BATCH_SIZE", "10"))
DELAY = float(os.getenv("MODEL_DELAY_SECONDS", "2.5"))
QUERIES = [
    "crypto startup announces launch new protocol mainnet testnet",
    "web3 startup announces launch new platform project",
    "new blockchain protocol mainnet launch crypto",
    "new DeFi protocol launches crypto project",
    '"we are launching" crypto protocol web3',
    '"introducing" "mainnet" crypto startup',
    "new crypto project launch announcement protocol dApp",
    "new blockchain company launches wallet infrastructure",
    "new blockchain infrastructure startup launch Web3 developer tools wallet security",
]

PROMPT = r"""You are the first-stage triage analyst for a selective crypto/Web3 launch radar. Return JSON only:
{
 "qualifies": true,
 "project": "official project/company name",
 "signal_type": "New project launch|Mainnet launch|Testnet launch|Protocol launch|Product launch|Other",
 "launch_date": "YYYY-MM-DD or null",
 "website": "official URL or null",
 "broad_offerings": "what it offers in plain English",
 "niche_presence": "specific category, use case, or technical specialty",
 "chain_ecosystem": "chain/ecosystem or null",
 "project_category": "DeFi protocol|blockchain infrastructure|developer tools|wallet/security/custody|stablecoin/payments|gaming infrastructure|NFT/collectibles|meme/memecoin|other",
 "is_nft_or_meme_project": false,
 "founders": ["named founders only if source states them"],
 "confidence": "High|Medium|Low",
 "evidence_note": "evidence for launch status"
}
Qualifies only if the source explicitly announces a substantive new product/protocol/infrastructure launch. Exclude funding-only news, token-price news, airdrop speculation, routine feature updates, generic commentary and token-only launches without substantive product utility. Exclude projects primarily built around NFTs, PFPs, collectibles, NFT marketplaces, meme coins or meme tokens. A second stage will verify public founder identity and previous shipped Web3 builder experience. Do not invent facts. Return qualifies=false when irrelevant.

PUBLISHER: {publisher}
SOURCE URL: {link}
DATE: {date}
TITLE: {title}
BODY: {body}
"""


def windows(start, end, days):
    cur = datetime.strptime(start, "%Y-%m-%d").date()
    stop = datetime.strptime(end, "%Y-%m-%d").date()
    while cur < stop:
        nxt = min(cur + timedelta(days=days), stop)
        yield cur.isoformat(), nxt.isoformat()
        cur = nxt


def collect():
    items = {}
    for start, end in windows(DATE_FROM, DATE_TO, WINDOW_DAYS):
        for query in QUERIES:
            for item in fetch_google_news(query, after=start, before=end, limit=ITEMS_PER_QUERY):
                if item.get("link"):
                    items.setdefault(item["link"], item)
            time.sleep(0.25)
        print(f"  collected launch coverage {start} to {end}")
    for url, name in [
        ("https://decrypt.co/feed", "Decrypt"),
        ("https://cointelegraph.com/rss", "Cointelegraph"),
        ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"),
    ]:
        for item in fetch_rss_feed(url, limit=100, label=name):
            published_date = parse_event_date(item)
            if item.get("link") and published_date != "Unknown" and DATE_FROM <= published_date < DATE_TO:
                items.setdefault(item["link"], item)
    return sorted(items.values(), key=published_timestamp, reverse=True)


def main():
    print(f"Crypto launch historical backfill | strict-founder-gate=on | {DATE_FROM} <= dates < {DATE_TO} | model={MODEL}")
    all_items = collect()
    existing_sources = webhook_get(STREAM, "existing_sources")
    fresh = [x for x in all_items if x.get("link") and x["link"] not in existing_sources]
    fresh = fresh[:MAX_ITEMS_PER_RUN]
    print(f"Collected {len(all_items)} distinct source URLs; {len(fresh)} new this batch")
    client = groq_client()
    pending, processed, appended, updated = [], [], 0, 0

    def flush():
        nonlocal pending, processed, appended, updated
        if not pending and not processed:
            return
        result = post_rows(STREAM, pending, processed_sources=processed)
        if result and result.get("ok"):
            appended += result.get("appended", 0)
            updated += result.get("updated", 0)
            print(f"  [save] appended={result.get('appended', 0)} updated={result.get('updated', 0)} skipped={result.get('skipped', 0)}")
            pending, processed = [], []
        else:
            print("  [warn] webhook batch failed; pending batch can be retried by rerunning")

    for idx, item in enumerate(fresh, 1):
        date = parse_event_date(item)
        prompt = PROMPT.replace("{publisher}", item.get("publisher", "Unknown"))
        prompt = prompt.replace("{link}", item.get("link", "")).replace("{date}", date)
        prompt = prompt.replace("{title}", item.get("title", "")[:400]).replace("{body}", item.get("body", "")[:1800])
        print(f"[{idx}/{len(fresh)}] {item.get('title', '')[:90]}")
        data = extract_json(client, prompt)
        if data is None:
            continue
        if not data.get("qualifies") or not data.get("project"):
            processed.append(item["link"])
            print("  - rejected at launch/category triage")
        else:
            passed, vet, evidence_urls = verify_crypto_launch_quality(client, item, data, date)
            processed.append(item["link"])
            if not passed:
                print(f"  - rejected by strict founder/track-record gate: {vet.get('quality_gate_reason', 'not enough verifiable evidence')}")
            else:
                previous_projects = vet.get("previous_web3_projects", [])
                founders = vet.get("founders", [])
                if not isinstance(previous_projects, list): previous_projects = [str(previous_projects)] if previous_projects else []
                if not isinstance(founders, list): founders = [str(founders)] if founders else []
                key = event_key(data.get("project"), data.get("launch_date") or date, data.get("signal_type", "Other"))
                official_links = fetch_official_social_links(data.get("website") or "")
                pending.append([
                    data.get("launch_date") or date, data.get("project", ""), data.get("signal_type", "Other"),
                    data.get("launch_date") or "", "", "", "", data.get("website") or "",
                    data.get("broad_offerings", ""), data.get("niche_presence", ""), data.get("chain_ecosystem") or "",
                    "; ".join(founders), item.get("link", ""), "",
                    "Qualified: public founder + prior shipped Web3 work; NFT/meme excluded", vet.get("confidence", "Medium"),
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), key, "Unknown",
                    "\n".join(evidence_urls), True, True, "; ".join(previous_projects),
                    vet.get("founder_evidence_url", ""), vet.get("track_record_evidence_url", ""),
                    vet.get("project_category", data.get("project_category", "Other")),
                    vet.get("quality_gate_reason", "Passed all quality checks"),
                    "; ".join(official_links),
                ])
                print(f"  + QUALIFIED {data.get('project')} | {data.get('signal_type')} | founder history verified")
        if len(pending) >= SAVE_BATCH_SIZE or len(processed) >= SAVE_BATCH_SIZE:
            flush()
        time.sleep(DELAY)
    flush()
    print(f"Historical launch batch finished. Qualified launches appended: {appended}; events enriched: {updated}. Re-run for remaining indexed results.")


if __name__ == "__main__":
    main()

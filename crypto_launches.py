"""Crypto Launch Radar: substantive launches only; NFT/meme and unverified teams excluded."""
import os
import time
from datetime import datetime, timezone

from tracker_common import (
    MODEL, account_freshness, event_key, extract_json, fetch_google_news, fetch_rss_feed,
    fetch_official_social_links, groq_client, parse_event_date, post_rows, verify_crypto_launch_quality,
    webhook_get, x_recent_search,
)

STREAM = "crypto"
MIN_DELAY = float(os.getenv("MODEL_DELAY_SECONDS", "2.5"))
MAX_ITEMS_PER_RUN = int(os.getenv("CRYPTO_MAX_ITEMS_PER_RUN", "2"))
SAVE_BATCH_SIZE = int(os.getenv("SAVE_BATCH_SIZE", "12"))

RSS_SOURCES = [
    ("https://decrypt.co/feed", "Decrypt"),
    ("https://cointelegraph.com/rss", "Cointelegraph"),
    ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"),
]
NEWS_QUERIES = [
    "crypto startup announces launch new protocol mainnet testnet",
    "web3 startup announces launch crypto project new platform",
    "new blockchain protocol launches mainnet crypto startup",
    "new DeFi protocol launches crypto project website X",
    '"we are launching" crypto protocol web3',
    '"introducing" "mainnet" crypto startup',
    "new blockchain infrastructure startup launch Web3 developer tools wallet security",
]
X_QUERIES = [
    '(launching OR "we are live" OR "mainnet is live" OR "testnet is live" OR "introducing") (crypto OR web3 OR blockchain OR DeFi OR protocol OR wallet OR onchain) -is:retweet lang:en',
    '("launch announcement" OR "launching today" OR "now live") (crypto OR web3 OR protocol OR blockchain) -is:retweet lang:en',
]

PROMPT = r"""You are the first-stage triage analyst for a selective crypto/Web3 launch radar. Return JSON only:
{
 "qualifies": true,
 "project": "official project/company name",
 "signal_type": "New project launch|Mainnet launch|Testnet launch|Protocol launch|Product launch|Other",
 "launch_date": "YYYY-MM-DD or null",
 "website": "official URL or null",
 "broad_offerings": "what it offers in 1-3 plain-English sentences",
 "niche_presence": "specific category, users, use case, or technical specialization",
 "chain_ecosystem": "chain/ecosystem or null",
 "project_category": "DeFi protocol|blockchain infrastructure|developer tools|wallet/security/custody|stablecoin/payments|gaming infrastructure|NFT/collectibles|meme/memecoin|other",
 "is_nft_or_meme_project": false,
 "founders": ["named founders only if source states them"],
 "confidence": "High|Medium|Low",
 "evidence_note": "what evidence supports the actual launch"
}
Set qualifies=false for funding-only news, generic commentary, established projects' minor updates, token-price news, airdrop speculation, token-only launches with no substantive product utility, or stories that do not explicitly announce a new product/protocol/infrastructure launch. Exclude projects primarily built around NFTs, PFPs, collectibles, NFT marketplaces, meme coins or meme tokens. This first stage only identifies likely candidates; a second stage verifies founder identity and previous shipped Web3 builder track record. Do not invent facts, founder names, URLs, or dates. Use an empty founders array when no names appear in this source.

Source type: {source_type}
Publisher: {publisher}
URL: {link}
Account/post metadata (may be empty): {metadata}
Date: {date}
Title: {title}
Body: {body}
"""


def make_items():
    items = []
    for url, label in RSS_SOURCES:
        items.extend(fetch_rss_feed(url, limit=10, label=label))
    for query in NEWS_QUERIES:
        items.extend(fetch_google_news(query, limit=12))
        time.sleep(0.3)
    for query in X_QUERIES:
        items.extend(x_recent_search(query, max_results=70))
    unique = {}
    for item in items:
        link = item.get("link")
        if link and link not in unique:
            unique[link] = item
    return list(unique.values())


def main():
    print(f"Crypto launch radar | strict-founder-gate=on | model={MODEL} | UTC={datetime.now(timezone.utc).isoformat()}")
    existing_sources = webhook_get(STREAM, "existing_sources")
    items = make_items()
    fresh = [item for item in items if item.get("link") and item["link"] not in existing_sources]
    fresh.sort(key=lambda item: item.get("published", ""), reverse=True)
    fresh = fresh[:MAX_ITEMS_PER_RUN]
    print(f"Fetched {len(items)} unique signals; {len(fresh)} new signals to classify")

    client = groq_client()
    pending, processed = [], []
    total_saved = total_updated = 0

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
            print("  [warn] batch not confirmed; keeping pending data for retry")

    for idx, item in enumerate(fresh, 1):
        reported_date = parse_event_date(item)
        x_user = item.get("x_user", {})
        metadata = {
            "username": x_user.get("username"), "display_name": x_user.get("name"),
            "account_created_at": x_user.get("created_at"), "profile_description": x_user.get("description"),
            "profile_url": x_user.get("url"), "followers_count": x_user.get("public_metrics", {}).get("followers_count"),
        } if x_user else {}
        prompt = PROMPT.replace("{source_type}", item.get("source_type", "rss"))
        prompt = prompt.replace("{publisher}", item.get("publisher", "Unknown"))
        prompt = prompt.replace("{link}", item.get("link", ""))
        prompt = prompt.replace("{metadata}", str(metadata)[:900])
        prompt = prompt.replace("{date}", reported_date)
        prompt = prompt.replace("{title}", item.get("title", "")[:400])
        prompt = prompt.replace("{body}", item.get("body", "")[:1300])
        print(f"[{idx}/{len(fresh)}] {item.get('title', '')[:90]}")
        data = extract_json(client, prompt)
        if data is None:
            # Model failures are not marked processed, so the next hourly run may retry.
            continue
        if not data.get("qualifies") or not data.get("project"):
            processed.append(item["link"])
            print("  - rejected at launch/category triage")
        else:
            passed, vet, evidence_urls = verify_crypto_launch_quality(client, item, data, reported_date)
            processed.append(item["link"])
            if not passed:
                print(f"  - rejected by strict founder/track-record gate: {vet.get('quality_gate_reason', 'not enough verifiable evidence')}")
            else:
                handle = x_user.get("username", "") if x_user else ""
                account_created = x_user.get("created_at", "") if x_user else ""
                profile_url = f"https://x.com/{handle}" if handle else ""
                tweet_url = item.get("link", "") if item.get("source_type") == "x" else ""
                age_bucket = account_freshness(account_created)
                key = event_key(data.get("project"), data.get("launch_date") or reported_date, data.get("signal_type", "Other"))
                previous_projects = vet.get("previous_web3_projects", [])
                founders = vet.get("founders", [])
                if not isinstance(previous_projects, list): previous_projects = [str(previous_projects)] if previous_projects else []
                if not isinstance(founders, list): founders = [str(founders)] if founders else []
                official_links = fetch_official_social_links(data.get("website") or "")
                row = [
                    data.get("launch_date") or reported_date, data.get("project", ""), data.get("signal_type", "Other"),
                    data.get("launch_date") or "", account_created, f"@{handle}" if handle else "", profile_url,
                    data.get("website") or "", data.get("broad_offerings", ""), data.get("niche_presence", ""),
                    data.get("chain_ecosystem") or "", "; ".join(founders), item.get("link", ""), tweet_url,
                    "Qualified: public founder + prior shipped Web3 work; NFT/meme excluded", vet.get("confidence", data.get("confidence", "Medium")),
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), key, age_bucket,
                    "\n".join(evidence_urls), True, True, "; ".join(previous_projects),
                    vet.get("founder_evidence_url", ""), vet.get("track_record_evidence_url", ""),
                    vet.get("project_category", data.get("project_category", "Other")),
                    vet.get("quality_gate_reason", "Passed all quality checks"),
                    "; ".join(official_links),
                ]
                pending.append(row)
                print(f"  + QUALIFIED {data.get('project')} | {data.get('signal_type')} | founder history verified")
        if len(pending) >= SAVE_BATCH_SIZE or len(processed) >= SAVE_BATCH_SIZE:
            flush()
        time.sleep(MIN_DELAY)
    flush()
    print(f"Completed. Qualified launches appended: {total_saved}; existing events enriched: {total_updated}")


if __name__ == "__main__":
    main()

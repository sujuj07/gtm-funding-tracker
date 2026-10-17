"""Crypto Launch Radar: substantive launches only; NFT/meme and unverified teams excluded."""
import os
import time
from datetime import datetime, timedelta, timezone

from tracker_common import (
    MODEL_CHAIN_LABEL, account_freshness, event_key, excluded_nft_or_meme, extract_json, fetch_defillama_protocol_leads, fetch_google_news, fetch_rss_feed,
    fetch_official_social_links, groq_client, launch_signal_score, parse_event_date, post_rows, published_timestamp, verify_crypto_launch_quality,
    webhook_get, x_recent_search,
)
from backfill_crypto import _hard_excluded, build_review_row

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
    '"mainnet launch" crypto protocol',
    '"testnet launch" Web3 protocol',
    '"mainnet is live" blockchain project',
    '"testnet is live" blockchain project',
    '"launches new protocol" DeFi blockchain',
    '"introducing" "Web3 protocol" launch',
    '"new DeFi protocol" launches',
    '"public beta" Web3 protocol launch',
    'site:chainwire.org blockchain protocol launch',
    'site:globenewswire.com Web3 protocol launch',
    '"launching today" crypto protocol',
    '"protocol launch" blockchain startup',
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
For normal news sources, qualifies=false unless there is a substantive launch signal. For source_type=defillama_protocol, treat the record only as a discovery lead: qualify for second-stage research only if the protocol appears substantive and not obviously NFT/meme-only; do NOT treat DeFiLlama's listedAt date as launch date or the listing as launch proof. The second stage must find an independent launch announcement. Exclude funding-only news, generic commentary, established projects' minor updates, token-price news, airdrop speculation, token-only launches with no substantive product utility, or stories that do not evidence a new product/protocol/infrastructure launch. Exclude NFT/PFP/collectibles-first and meme projects. Do not invent facts, founder names, URLs, or dates.

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
    # Free structured listing data lets the live collector spot DeFi protocols with a recent
    # listedAt timestamp. This is a candidate lead, not proof that a project has launched.
    today = datetime.now(timezone.utc).date()
    try:
        leads = fetch_defillama_protocol_leads((today - timedelta(days=30)).isoformat(), (today + timedelta(days=1)).isoformat())
        items.extend(leads)
        print(f"[source] DeFiLlama public /protocols: {len(leads)} recent listing lead(s), no API key")
    except Exception as exc:
        print(f"[warn] free DeFiLlama /protocols source unavailable: {type(exc).__name__}")
    unique = {}
    for item in items:
        link = item.get("link")
        if link and link not in unique:
            unique[link] = item
    return list(unique.values())


def main():
    print(f"Crypto launch radar | strict-founder-gate=on | model-route={MODEL_CHAIN_LABEL} | UTC={datetime.now(timezone.utc).isoformat()}")
    existing_sources = webhook_get(STREAM, "existing_sources")
    items = make_items()
    raw_fresh = [item for item in items if item.get("link") and item["link"] not in existing_sources]
    fresh_all = [item for item in raw_fresh if launch_signal_score(item) > 0]
    fresh_all.sort(key=lambda item: (launch_signal_score(item), published_timestamp(item)), reverse=True)
    screened_out = len(raw_fresh) - len(fresh_all)
    fresh = fresh_all[:MAX_ITEMS_PER_RUN]
    print(
        f"Fetched {len(items)} unique signals; {len(raw_fresh)} new sources; "
        f"screened out {screened_out} without a clear launch cue; "
        f"{len(fresh_all)} launch-like signals, classifying {len(fresh)}"
    )

    client = groq_client()
    pending, processed, review_pending = [], [], []
    total_saved = total_updated = 0
    write_failed = False

    def flush():
        nonlocal pending, processed, review_pending, total_saved, total_updated, write_failed
        if not pending and not processed and not review_pending:
            return True
        for attempt in range(1, 4):
            result = post_rows(STREAM, pending, processed_sources=processed, review_rows=review_pending)
            if result and result.get("ok"):
                total_saved += result.get("appended", 0)
                total_updated += result.get("updated", 0)
                print(
                    f"  [save] main_appended={result.get('appended', 0)} main_updated={result.get('updated', 0)} "
                    f"review_added={result.get('reviewAppended', 0)} review_updated={result.get('reviewUpdated', 0)} "
                    f"skipped={result.get('skipped', 0)}"
                )
                pending, processed, review_pending = [], [], []
                write_failed = False
                return True
            write_failed = True
            print(f"  [warn] webhook batch not confirmed (attempt {attempt}/3); keeping buffered data")
            if attempt < 3:
                time.sleep(2 ** attempt)
        return False

    for idx, item in enumerate(fresh, 1):
        reported_date = parse_event_date(item)
        # A DeFiLlama `listedAt` is a registry timestamp, not the actual launch/announcement date.
        row_date_fallback = "" if item.get("source_type") == "defillama_protocol" else reported_date
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
        if item.get("source_type") == "defillama_protocol":
            data.setdefault("website", item.get("official_url") or "")
            if not data.get("chain_ecosystem") and item.get("chains"):
                data["chain_ecosystem"] = ", ".join(map(str, item.get("chains", [])))
            # The model must not copy the DeFiLlama listing date into launch-date fields.
            data["launch_date"] = None
        if not data.get("qualifies") or not data.get("project"):
            processed.append(item["link"])
            print("  - rejected at launch/category triage")
        else:
            passed, vet, evidence_urls = verify_crypto_launch_quality(client, item, data, reported_date)
            if vet.get("quality_gate_reason") == "No successful verification response.":
                # Preserve the lead for later review, but leave the source unprocessed so it
                # remains eligible for the next live fetch/backfill retry.
                if not _hard_excluded(data, vet):
                    review_pending.append(build_review_row(item, data, vet, evidence_urls, reported_date))
                    print("  [review-queue] retained candidate while verification service is unavailable")
                print("  [warn] founder verification unavailable; source remains unprocessed for retry")
                continue
            if not passed:
                print(f"  - rejected by strict founder/track-record gate: {vet.get('quality_gate_reason', 'not enough verifiable evidence')}")
                if not _hard_excluded(data, vet):
                    review_pending.append(build_review_row(item, data, vet, evidence_urls, reported_date))
                    print(f"  [review-queue] retained {data.get('project')} separately; main sheet remains strict")
                processed.append(item["link"])
            else:
                x_user = item.get("_verified_x_user") or x_user or {}
                handle = x_user.get("username") or item.get("x_handle", "") or ""
                account_created = x_user.get("created_at", "")
                profile_url = x_user.get("url") or (f"https://x.com/{handle}" if handle else "")
                tweet_url = item.get("link", "") if item.get("source_type") in {"x", "x_syndication"} else ""
                age_bucket = account_freshness(account_created)
                verified_launch_date = str(vet.get("verified_launch_date") or "").strip()
                key_date = verified_launch_date or (reported_date if item.get("source_type") != "defillama_protocol" else "Unknown")
                key = event_key(data.get("project"), key_date, data.get("signal_type", "Other"))
                previous_projects = vet.get("previous_web3_projects", [])
                founders = vet.get("founders", [])
                if not isinstance(previous_projects, list): previous_projects = [str(previous_projects)] if previous_projects else []
                if not isinstance(founders, list): founders = [str(founders)] if founders else []
                official_links = fetch_official_social_links(data.get("website") or "")
                launch_source = str(vet.get("launch_evidence_url") or item.get("link") or "")
                row = [
                    row_date_fallback, data.get("project", ""), data.get("signal_type", "Other"),
                    verified_launch_date, account_created, f"@{handle}" if handle else "", profile_url,
                    data.get("website") or "", data.get("broad_offerings", ""), data.get("niche_presence", ""),
                    data.get("chain_ecosystem") or "", "; ".join(founders), launch_source, tweet_url,
                    "Qualified: public founder + prior shipped Web3 work; NFT/meme excluded", vet.get("confidence", data.get("confidence", "Medium")),
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), key, age_bucket,
                    "\n".join(evidence_urls), True, True, "; ".join(previous_projects),
                    vet.get("founder_evidence_url", ""), vet.get("track_record_evidence_url", ""),
                    vet.get("project_category", data.get("project_category", "Other")),
                    vet.get("quality_gate_reason", "Passed all quality checks"),
                    "; ".join(official_links),
                ]
                pending.append(row)
                review_pending.append(build_review_row(item, data, vet, evidence_urls, reported_date, passed=True))
                processed.append(item["link"])
                print(f"  + QUALIFIED {data.get('project')} | {data.get('signal_type')} | founder history verified")
        if (len(pending) + len(review_pending) >= SAVE_BATCH_SIZE) or len(processed) >= SAVE_BATCH_SIZE:
            flush()
        time.sleep(MIN_DELAY)
    flush()
    if write_failed:
        raise RuntimeError("At least one webhook batch remained unconfirmed; candidates will be retried next run.")
    print(f"Completed. Qualified launches appended: {total_saved}; existing events enriched: {total_updated}")


if __name__ == "__main__":
    main()

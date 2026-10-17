"""Historical crypto launch backfill with strict public-founder and prior-builder verification."""
import os
import time
import math
from datetime import datetime, timedelta, timezone

from tracker_common import (
    MODEL_CHAIN_LABEL, event_key, extract_json, fetch_google_news, fetch_official_social_links, fetch_rss_feed, groq_client,
    launch_signal_score, parse_event_date, post_rows, published_timestamp, verify_crypto_launch_quality, webhook_get,
)
from backfill_state import advance_window, choose_window, end_exclusive_today, leave_window_pending

STREAM = "crypto"
DATE_FROM = os.getenv("BACKFILL_DATE_FROM", "2026-08-01")
DATE_TO = os.getenv("BACKFILL_DATE_TO", end_exclusive_today())
WINDOW_DAYS = int(os.getenv("BACKFILL_WINDOW_DAYS", "7"))
ITEMS_PER_QUERY = int(os.getenv("BACKFILL_ITEMS_PER_QUERY", "60"))
MAX_ITEMS_PER_RUN = int(os.getenv("BACKFILL_MAX_ITEMS_PER_RUN", "60"))
SAVE_BATCH_SIZE = int(os.getenv("SAVE_BATCH_SIZE", "10"))
DELAY = float(os.getenv("MODEL_DELAY_SECONDS", "2.5"))
QUERIES = [
    '"mainnet launch" crypto protocol',
    '"testnet launch" Web3 protocol',
    '"mainnet is live" blockchain project',
    '"testnet is live" blockchain project',
    '"launches new protocol" DeFi blockchain',
    '"launches a new blockchain" startup',
    '"introducing" "Web3 protocol" launch',
    '"new DeFi protocol" launches',
    '"public beta" Web3 protocol launch',
    'site:chainwire.org blockchain protocol launch',
    'site:globenewswire.com Web3 protocol launch',
    'site:prnewswire.com blockchain protocol launch',
    '"launching today" crypto protocol',
    '"protocol launch" blockchain startup',
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


def collect(window_start: str, window_end: str):
    items = {}
    successful_queries = 0
    query_errors = []
    for query in QUERIES:
        try:
            results = fetch_google_news(
                query, after=window_start, before=window_end,
                limit=ITEMS_PER_QUERY, strict=True,
            )
            successful_queries += 1
            for item in results:
                if item.get("link"):
                    items.setdefault(item["link"], item)
        except Exception as exc:
            query_errors.append((query, str(exc)))
            print(f"  [warn] crypto launch search failed for query={query!r}: {exc}")
        time.sleep(0.60)

    # Require valid HTTP results from >=80% of the search queries before considering
    # a launch-history window reliable enough to advance its cursor.
    required = max(1, math.ceil(len(QUERIES) * 0.80))
    print(f"  launch query coverage: {successful_queries}/{len(QUERIES)} successful; required={required}")
    if successful_queries < required:
        raise RuntimeError(
            f"Insufficient crypto-launch search coverage for {window_start} to {window_end}: "
            f"{successful_queries}/{len(QUERIES)} queries succeeded; cursor will not advance."
        )
    if query_errors:
        print(f"  [warn] {len(query_errors)} search query/queries failed; coverage threshold passed, results may be partial.")
    print(f"  collected launch coverage {window_start} to {window_end}")
    for url, name in [
        ("https://decrypt.co/feed", "Decrypt"),
        ("https://cointelegraph.com/rss", "Cointelegraph"),
        ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"),
    ]:
        for item in fetch_rss_feed(url, limit=100, label=name):
            published_date = parse_event_date(item)
            if item.get("link") and published_date != "Unknown" and window_start <= published_date < window_end:
                items.setdefault(item["link"], item)
    return sorted(items.values(), key=published_timestamp, reverse=True), bool(query_errors)


def main():
    window = choose_window(STREAM, DATE_FROM, DATE_TO, WINDOW_DAYS)
    if window is None:
        return
    window_start, window_end = window
    print(f"Crypto launch historical backfill | strict-founder-gate=on | {window_start} <= dates < {window_end} | current target end={DATE_TO} | model-route={MODEL_CHAIN_LABEL}")
    all_items, search_incomplete = collect(window_start, window_end)
    existing_sources = webhook_get(STREAM, "existing_sources")
    raw_fresh = [x for x in all_items if x.get("link") and x["link"] not in existing_sources]
    # Avoid wasting model quota triaging generic crypto news. Only articles with
    # a visible product/network launch cue proceed to LLM classification.
    fresh_all = [x for x in raw_fresh if launch_signal_score(x) > 0]
    fresh_all.sort(key=lambda x: (launch_signal_score(x), published_timestamp(x)), reverse=True)
    screened_out = len(raw_fresh) - len(fresh_all)
    more_candidates = len(fresh_all) > MAX_ITEMS_PER_RUN
    fresh = fresh_all[:MAX_ITEMS_PER_RUN]
    print(
        f"Collected {len(all_items)} distinct source URLs; {len(raw_fresh)} new/unprocessed; "
        f"screened out {screened_out} low-signal headlines before model triage; "
        f"{len(fresh_all)} launch-like candidates, processing {len(fresh)} (cap={MAX_ITEMS_PER_RUN})"
    )
    if screened_out:
        examples = [str(x.get("title", ""))[:90] for x in raw_fresh if launch_signal_score(x) == 0][:3]
        for example in examples:
            print(f"  [prefilter] no clear launch cue: {example}")
    client = groq_client()
    pending, processed, appended, updated = [], [], 0, 0
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
            print("  [warn] webhook batch failed; keeping the current date window pending")
            flush_unconfirmed = True

    for idx, item in enumerate(fresh, 1):
        date = parse_event_date(item)
        prompt = PROMPT.replace("{publisher}", item.get("publisher", "Unknown"))
        prompt = prompt.replace("{link}", item.get("link", "")).replace("{date}", date)
        prompt = prompt.replace("{title}", item.get("title", "")[:400]).replace("{body}", item.get("body", "")[:1800])
        print(f"[{idx}/{len(fresh)}] {item.get('title', '')[:90]}")
        data = extract_json(client, prompt)
        if data is None:
            failed_items.append(item["link"])
            print("  [warn] model extraction failed; this source will be retried")
            continue
        if not data.get("qualifies") or not data.get("project"):
            processed.append(item["link"])
            print("  - rejected at launch/category triage")
        else:
            passed, vet, evidence_urls = verify_crypto_launch_quality(client, item, data, date)
            if vet.get("quality_gate_reason") == "No successful verification response.":
                failed_items.append(item["link"])
                print("  [warn] founder verification could not reach a model; date window will be retried")
                continue
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
    print(f"Historical launch window finished. Qualified launches appended: {appended}; events enriched: {updated}.")

    if failed_items:
        leave_window_pending(window_start, window_end, f"{len(failed_items)} source(s) had model errors")
    elif search_incomplete:
        leave_window_pending(window_start, window_end, "one or more search queries failed; retrying the same date window for complete coverage")
    elif more_candidates:
        leave_window_pending(window_start, window_end, f"{len(fresh_all) - len(fresh)} launch-like candidate(s) remain for this week")
    elif flush_unconfirmed:
        leave_window_pending(window_start, window_end, "webhook write was not confirmed")
    else:
        caught_up = advance_window(STREAM, window_start, window_end, DATE_TO)
        if caught_up:
            print("[backfill] HISTORICAL BACKFILL COMPLETE through today's target date; workflow can disable its hourly schedule.")


if __name__ == "__main__":
    main()

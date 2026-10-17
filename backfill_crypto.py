"""Historical crypto launch backfill with strict public-founder and prior-builder verification."""
import os
import time
import math
from datetime import datetime, timedelta, timezone

from tracker_common import (
    MODEL_CHAIN_LABEL, event_key, excluded_nft_or_meme, extract_json, fetch_defillama_protocol_leads, fetch_google_news, fetch_official_social_links, fetch_rss_feed, groq_client,
    launch_signal_score, parse_event_date, post_rows, published_timestamp, verify_crypto_launch_quality, webhook_get,
)
from backfill_state import advance_window, choose_window, end_exclusive_today, leave_window_pending

STREAM = "crypto"
DATE_FROM = os.getenv("BACKFILL_DATE_FROM", "2026-08-01")
DATE_TO = os.getenv("BACKFILL_DATE_TO", end_exclusive_today())
WINDOW_DAYS = int(os.getenv("BACKFILL_WINDOW_DAYS", "7"))
ITEMS_PER_QUERY = int(os.getenv("BACKFILL_ITEMS_PER_QUERY", "60"))
MAX_ITEMS_PER_RUN = int(os.getenv("BACKFILL_MAX_ITEMS_PER_RUN", "60"))
RECHECK_PROCESSED_SOURCES = os.getenv("BACKFILL_RECHECK_PROCESSED_SOURCES", "false").strip().lower() in {"1", "true", "yes", "on"}
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
For normal news sources, qualifies only if the source explicitly announces a substantive new product/protocol/infrastructure launch. For a source_type=defillama_protocol registry listing, treat it only as a discovery lead: qualify for second-stage research if it appears to be a substantive protocol and is not obviously NFT/meme-only, but DO NOT treat listedAt as launch date or the registry as launch proof. The second stage must find a separate credible launch announcement before any row is accepted. Exclude funding-only news, token-price news, airdrop speculation, routine feature updates, generic commentary and token-only launches without substantive product utility. Exclude projects primarily built around NFTs, PFPs, collectibles, NFT marketplaces, meme coins or meme tokens. A second stage verifies public founder identity and previous shipped Web3 builder experience. Do not invent facts.

SOURCE TYPE: {source_type}
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

    # Additional free structured discovery source. /protocols is public and requires no key;
    # /api/raises is a paid endpoint and is intentionally not used. A registry listing is only
    # a lead and never enough, by itself, to pass the launch/founder gate.
    try:
        protocol_leads = fetch_defillama_protocol_leads(window_start, window_end)
        print(f"  DeFiLlama free /protocols leads listed in this window: {len(protocol_leads)}")
        for item in protocol_leads:
            items.setdefault(item["link"], item)
        # Public X timelines are fetched only during stage-two verification, after a source
        # passes candidate triage. This avoids wasting requests and keeps the source optional.
    except Exception as exc:
        query_errors.append(("DeFiLlama /protocols", str(exc)))
        print(f"  [warn] free DeFiLlama /protocols source unavailable: {type(exc).__name__}: {exc}")

    return sorted(items.values(), key=published_timestamp, reverse=True), bool(query_errors)


def _list_value(value):
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [x.strip() for x in value.split(";") if x.strip()]
    return []


def _hard_excluded(data: dict, vet: dict) -> bool:
    """Exclude explicit NFT/meme-first projects from both the main sheet and review queue."""
    merged = dict(data or {})
    for key in ("is_nft_or_meme_project", "project_category", "crypto_project_category", "project_type", "sector"):
        if vet.get(key) not in (None, ""):
            merged[key] = vet[key]
    return excluded_nft_or_meme(merged)


def build_review_row(item: dict, data: dict, vet: dict, evidence_urls: list[str], source_date: str,
                     *, passed: bool = False) -> list:
    """Build a transparent review-queue row for candidates failing only the evidence gate."""
    project = str(data.get("project") or item.get("project") or "").strip()
    signal_type = str(data.get("signal_type") or "Potential launch").strip()
    verified_launch_date = str(vet.get("verified_launch_date") or "").strip()
    if item.get("source_type") == "defillama_protocol":
        # listedAt is never a launch date.
        source_date = ""
        if verified_launch_date == str(item.get("listed_at") or ""):
            verified_launch_date = ""
    reason = str(vet.get("quality_gate_reason") or "Evidence did not yet meet all launch/founder requirements.").strip()
    if passed:
        status = "Qualified — promoted to main tracker"
        reason = "All strict qualification criteria verified; record written to Crypto Launch Radar."
    elif reason == "No successful verification response.":
        status = "Retry required — verification service unavailable"
    elif not vet.get("launch_verified") and item.get("source_type") == "defillama_protocol":
        status = "Needs independent launch evidence"
    elif not vet.get("founders_publicly_identified"):
        status = "Needs named founder evidence"
    elif not vet.get("prior_web3_builder_track_record"):
        status = "Needs prior Web3 build evidence"
    else:
        status = "Needs evidence review"

    founders = _list_value(vet.get("founders") or data.get("founders"))
    other_urls = []
    for url in [*(evidence_urls or []), item.get("link", ""), data.get("website", ""),
                vet.get("founder_evidence_url", ""), vet.get("track_record_evidence_url", ""),
                vet.get("launch_evidence_url", "")]:
        url = str(url or "").strip()
        if url.startswith(("http://", "https://")) and url not in other_urls:
            other_urls.append(url)
    # Keep the queue key stable across later verification improvements. The newly
    # verified launch date may appear on a recheck; it must not create a second queue row.
    key_date = source_date if item.get("source_type") != "defillama_protocol" else ""
    review_key = event_key(project, key_date, "review:" + signal_type)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return [
        now, project, signal_type, verified_launch_date, str(data.get("website") or item.get("official_url") or ""),
        str(data.get("broad_offerings") or ""), str(data.get("niche_presence") or ""),
        str(data.get("chain_ecosystem") or ", ".join(item.get("chains") or [])),
        "; ".join(founders), str(item.get("link") or ""), str(vet.get("founder_evidence_url") or ""),
        str(vet.get("track_record_evidence_url") or ""), "\n".join(other_urls), status, reason,
        str(vet.get("project_category") or data.get("project_category") or "Unknown"),
        str(vet.get("confidence") or data.get("confidence") or "Low"), now,
        str(item.get("source_type") or "unknown"), str(item.get("title") or ""),
        str(item.get("publisher") or ""), review_key, "", "",
        "" if item.get("source_type") == "defillama_protocol" or source_date == "Unknown" else source_date,
        str(item.get("body") or "")[:1800],
    ]


def main():
    window = choose_window(STREAM, DATE_FROM, DATE_TO, WINDOW_DAYS)
    if window is None:
        return
    window_start, window_end = window
    print(f"Crypto launch historical backfill | strict-founder-gate=on | {window_start} <= dates < {window_end} | current target end={DATE_TO} | model-route={MODEL_CHAIN_LABEL}")
    all_items, search_incomplete = collect(window_start, window_end)
    existing_sources = webhook_get(STREAM, "existing_sources")
    # A one-time manual recheck can bypass the remote processed registry after verification logic improves.
    # POST also receives allowReprocessedSources, while event keys/source dedupe still prevent duplicate rows.
    raw_fresh = [x for x in all_items if x.get("link") and (RECHECK_PROCESSED_SOURCES or x["link"] not in existing_sources)]
    if RECHECK_PROCESSED_SOURCES:
        print("  [recheck] re-evaluating sources already in the processed registry for this date window")
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
    pending, processed, review_pending, appended, updated = [], [], [], 0, 0
    flush_unconfirmed = False
    failed_items = []

    def flush():
        nonlocal pending, processed, review_pending, appended, updated, flush_unconfirmed
        if not pending and not processed and not review_pending:
            return True
        # Retry transient Apps Script failures while keeping every unconfirmed item buffered.
        # A successful retry sends the entire retained buffer, so the registry and review rows
        # are confirmed together; otherwise the historical cursor must remain stationary.
        for attempt in range(1, 4):
            result = post_rows(
                STREAM, pending, processed_sources=processed,
                allow_reprocessed_sources=RECHECK_PROCESSED_SOURCES,
                review_rows=review_pending,
            )
            if result and result.get("ok"):
                appended += result.get("appended", 0)
                updated += result.get("updated", 0)
                print(
                    f"  [save] main_appended={result.get('appended', 0)} main_updated={result.get('updated', 0)} "
                    f"review_added={result.get('reviewAppended', 0)} review_updated={result.get('reviewUpdated', 0)} "
                    f"skipped={result.get('skipped', 0)} processed_confirmed={result.get('processed', 0)}"
                )
                pending, processed, review_pending = [], [], []
                flush_unconfirmed = False
                return True
            flush_unconfirmed = True
            print(f"  [warn] webhook batch unconfirmed (attempt {attempt}/3); keeping records buffered")
            if attempt < 3:
                time.sleep(2 ** attempt)
        return False

    for idx, item in enumerate(fresh, 1):
        date = parse_event_date(item)
        row_date_fallback = "" if item.get("source_type") == "defillama_protocol" else date
        prompt = PROMPT.replace("{source_type}", item.get("source_type", "rss"))
        prompt = prompt.replace("{publisher}", item.get("publisher", "Unknown"))
        prompt = prompt.replace("{link}", item.get("link", "")).replace("{date}", date)
        prompt = prompt.replace("{title}", item.get("title", "")[:400]).replace("{body}", item.get("body", "")[:1800])
        print(f"[{idx}/{len(fresh)}] {item.get('title', '')[:90]}")
        data = extract_json(client, prompt)
        if data is None:
            failed_items.append(item["link"])
            print("  [warn] model extraction failed; this source will be retried")
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
            passed, vet, evidence_urls = verify_crypto_launch_quality(client, item, data, date)
            if vet.get("quality_gate_reason") == "No successful verification response.":
                if not _hard_excluded(data, vet):
                    review_pending.append(build_review_row(item, data, vet, evidence_urls, date))
                    print("  [review-queue] verification service unavailable; retained candidate for retry")
                failed_items.append(item["link"])
                print("  [warn] founder verification could not reach a model; source remains unprocessed and date window will be retried")
                if len(review_pending) >= SAVE_BATCH_SIZE:
                    flush()
                continue
            if not passed:
                print(f"  - rejected by strict founder/track-record gate: {vet.get('quality_gate_reason', 'not enough verifiable evidence')}")
                if not _hard_excluded(data, vet):
                    review_pending.append(build_review_row(item, data, vet, evidence_urls, date))
                    print(f"  [review-queue] retained {data.get('project')} separately; main sheet remains strict")
                processed.append(item["link"])
            else:
                previous_projects = vet.get("previous_web3_projects", [])
                founders = vet.get("founders", [])
                if not isinstance(previous_projects, list): previous_projects = [str(previous_projects)] if previous_projects else []
                if not isinstance(founders, list): founders = [str(founders)] if founders else []
                verified_launch_date = str(vet.get("verified_launch_date") or "").strip()
                # Do not write a model-inferred launch date as fact. Column D is populated
                # only when the independent launch evidence verified an explicit date.
                key = event_key(data.get("project"), verified_launch_date or (date if item.get("source_type") != "defillama_protocol" else "Unknown"), data.get("signal_type", "Other"))
                official_links = fetch_official_social_links(data.get("website") or "")
                x_user = item.get("_verified_x_user") or {}
                x_handle = x_user.get("username") or item.get("x_handle") or ""
                verified_launch_source = str(vet.get("launch_evidence_url") or item.get("link") or "")
                pending.append([
                    date if item.get("source_type") != "defillama_protocol" else "", data.get("project", ""), data.get("signal_type", "Other"),
                    verified_launch_date, x_user.get("created_at", ""), f"@{x_handle}" if x_handle else "", x_user.get("url", f"https://x.com/{x_handle}" if x_handle else ""), data.get("website") or "",
                    data.get("broad_offerings", ""), data.get("niche_presence", ""), data.get("chain_ecosystem") or "",
                    "; ".join(founders), verified_launch_source, "",
                    "Qualified: public founder + prior shipped Web3 work; NFT/meme excluded", vet.get("confidence", "Medium"),
                    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), key, "Unknown",
                    "\n".join(evidence_urls), True, True, "; ".join(previous_projects),
                    vet.get("founder_evidence_url", ""), vet.get("track_record_evidence_url", ""),
                    vet.get("project_category", data.get("project_category", "Other")),
                    vet.get("quality_gate_reason", "Passed all quality checks"),
                    "; ".join(official_links),
                ])
                # If this project already exists in the review queue, update its status to promoted.
                if vet.get("launch_evidence_url"):
                    review_pending.append(build_review_row(item, data, vet, evidence_urls, date, passed=True))
                processed.append(item["link"])
                print(f"  + QUALIFIED {data.get('project')} | {data.get('signal_type')} | founder history verified")
        if (len(pending) + len(review_pending) >= SAVE_BATCH_SIZE) or len(processed) >= SAVE_BATCH_SIZE:
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
